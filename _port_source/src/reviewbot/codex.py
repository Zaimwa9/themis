"""codex exec subprocess runner (Codex subscription via CODEX_HOME auth)."""

import asyncio
import contextlib
import os
import signal
from pathlib import Path


class CodexError(Exception):
    pass


class CodexQuotaError(CodexError):
    """The Codex subscription usage window is exhausted; do not retry."""


_QUOTA_MARKERS = ("usage limit",)

# Codex runs on untrusted PR content: pass only what the CLI needs, never
# the worker's secrets (DB, Stripe, S3, LLM keys, REVIEWBOT_*).
_CODEX_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "CODEX_HOME", "LANG", "LC_ALL", "TERM", "TMPDIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
})


def build_command(
    prompt: str, model: str, effort: str, sandbox: str = "workspace-write"
) -> list[str]:
    return [
        "codex", "exec",
        "--sandbox", sandbox,
        "-c", "approval_policy=never",
        "-c", f"model_reasoning_effort={effort}",
        "-m", model,
        "--color", "never",
        prompt,
    ]


async def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    """SIGKILL the whole group; children may hold the stdout pipe open."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        # Group already gone; the direct child may have been reaped too.
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    # Best effort reap; shield so a repeated cancellation cannot interrupt it.
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(process.wait()), 5)


async def run_codex(
    *,
    prompt: str,
    workspace: Path,
    model: str,
    effort: str,
    timeout: float,
    sandbox: str = "workspace-write",
) -> str:
    env = {k: v for k, v in os.environ.items() if k in _CODEX_ENV_ALLOWLIST}
    process = await asyncio.create_subprocess_exec(
        *build_command(prompt, model, effort, sandbox),
        cwd=workspace,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        output_bytes, _ = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError as error:
        await _kill_process_group(process)
        raise CodexError(f"codex timed out after {timeout}s") from error
    except asyncio.CancelledError:
        # ARQ job cancellation: the codex process group must not outlive the task.
        await _kill_process_group(process)
        raise
    output = output_bytes.decode(errors="replace")
    if process.returncode != 0:
        # Only the tail is diagnostic; earlier output may echo the prompt/diff.
        lowered = output[-2000:].lower()
        if any(marker in lowered for marker in _QUOTA_MARKERS):
            raise CodexQuotaError(f"codex usage limit reached: {output[-500:]}")
        raise CodexError(f"codex exited {process.returncode}: {output[-2000:]}")
    return output
