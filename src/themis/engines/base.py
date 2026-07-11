"""Engine contract and shared subprocess plumbing for agent CLIs."""

import asyncio
import contextlib
import os
import signal
from pathlib import Path
from typing import Protocol


class EngineError(Exception):
    """Agent attempt failed; the caller may retry."""


class EngineQuotaError(EngineError):
    """The subscription usage window is exhausted; do not retry."""


class Engine(Protocol):
    name: str

    def available(self) -> bool:
        """Cheap local auth-presence check; no network."""
        ...

    async def run(
        self, *, prompt: str, workspace: Path, model: str, effort: str,
        timeout: float, web_access: bool = False,
    ) -> str: ...


# Agents run on untrusted PR content: pass only what the CLI needs, never the
# worker's secrets (DB, GitHub, THEMIS_*). Adapters extend this set.
BASE_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
})


def allowlisted_env(extra: frozenset[str]) -> dict[str, str]:
    allowed = BASE_ENV_ALLOWLIST | extra
    return {k: v for k, v in os.environ.items() if k in allowed}


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


async def run_cli(
    *,
    name: str,
    command: list[str],
    workspace: Path,
    env: dict[str, str],
    timeout: float,
    quota_markers: tuple[str, ...],
) -> str:
    """Run an agent CLI in the workspace with hardened subprocess semantics:
    own process group, timeout and cancellation kill the whole group, quota
    markers in the output tail map to EngineQuotaError."""
    process = await asyncio.create_subprocess_exec(
        *command,
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
        raise EngineError(f"{name} timed out after {timeout}s") from error
    except asyncio.CancelledError:
        # Queue job cancellation: the process group must not outlive the task.
        await _kill_process_group(process)
        raise
    output = output_bytes.decode(errors="replace")
    if process.returncode != 0:
        # Only the tail is diagnostic; earlier output may echo the prompt/diff.
        lowered = output[-2000:].lower()
        if any(marker in lowered for marker in quota_markers):
            raise EngineQuotaError(f"{name} usage limit reached: {output[-500:]}")
        raise EngineError(f"{name} exited {process.returncode}: {output[-2000:]}")
    return output
