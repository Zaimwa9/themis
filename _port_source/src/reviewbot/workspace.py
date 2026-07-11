"""Temp git workspaces for PR review: shallow clone of PR head + base ref."""

import asyncio
import contextlib
import os
import re
import shutil
import signal
import time
import uuid
from pathlib import Path


class WorkspaceError(Exception):
    pass


def clone_url_for(repo: str, token: str) -> str:
    return f"https://x-access-token:{token}@github.com/{repo}.git"


def _scrub(output: str) -> str:
    # scrub the token before any slicing so it can't straddle the cut
    return re.sub(r"x-access-token:[^@]+", "x-access-token:***", output)


async def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    """SIGKILL the whole group; git children hold the token-bearing argv."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        # Group already gone; the direct child may have been reaped too.
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    # Best effort reap; shield so a repeated cancellation cannot interrupt it.
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(process.wait()), 5)


async def run_git(*args: str, cwd: Path, timeout: float = 300) -> tuple[int, str]:
    """Run git hardened against hangs and cancellation.

    Returns (returncode, combined_output) without raising on a non-zero exit
    (for callers that must not raise). Raises WorkspaceError on timeout. On
    timeout or cancellation the whole process group is SIGKILLed so a stalled
    fetch cannot burn the ARQ budget and no token-bearing child survives.
    """
    process = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        start_new_session=True,
    )
    try:
        output_bytes, _ = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError as error:
        await _kill_process_group(process)
        raise WorkspaceError(f"git {args[0]} timed out after {timeout}s") from error
    except asyncio.CancelledError:
        # ARQ job cancellation: the git process group must not outlive the task.
        await _kill_process_group(process)
        raise
    return process.returncode, output_bytes.decode(errors="replace")


async def _git(*args: str, cwd: Path, timeout: float = 300) -> None:
    returncode, output = await run_git(*args, cwd=cwd, timeout=timeout)
    if returncode != 0:
        raise WorkspaceError(f"git {args[0]} failed: {_scrub(output)[-500:]}")


async def prepare_workspace(
    root: Path, clone_url: str, pr_number: int, base_ref: str, depth: int = 50
) -> Path:
    workspace = root / uuid.uuid4().hex[:12]
    workspace.mkdir(parents=True)
    try:
        await _git("init", "-q", cwd=workspace)
        # Fetch by URL (no `remote add`) so the token-bearing clone URL is
        # never persisted in .git/config, where the review agent running on
        # untrusted PR content could read it.
        await _git(
            "fetch", "-q", f"--depth={depth}", clone_url,
            f"refs/pull/{pr_number}/head:refs/heads/pr",
            f"{base_ref}:refs/remotes/origin/{base_ref}",
            cwd=workspace,
        )
        await _git("checkout", "-q", "pr", cwd=workspace)
        # git fetch records the token-bearing URL in FETCH_HEAD and reflogs.
        shutil.rmtree(workspace / ".git" / "logs", ignore_errors=True)
        (workspace / ".git" / "FETCH_HEAD").unlink(missing_ok=True)
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        raise
    return workspace


def remove_workspace(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def sweep_stale(root: Path, max_age_seconds: int = 86_400) -> None:
    """Remove leftover workspaces older than max_age (crash safety net)."""
    if not root.exists():
        return
    cutoff = time.time() - max_age_seconds
    # Outer suppress: iterdir itself can race a concurrent delete of root.
    with contextlib.suppress(OSError):
        for child in root.iterdir():
            with contextlib.suppress(OSError):
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
