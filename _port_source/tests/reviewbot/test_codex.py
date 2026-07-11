import asyncio
import os
import stat
import time
from pathlib import Path

import pytest

from reviewbot.codex import CodexError, CodexQuotaError, run_codex

pytestmark = pytest.mark.asyncio


def _fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / "codex"
    exe.write_text(f"#!/bin/sh\n{script}\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


async def test_run_codex__exit_zero__returns_output(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, "echo done")

    output = await run_codex(
        prompt="review this", workspace=workspace, model="gpt-5.4", effort="high", timeout=10
    )

    assert "done" in output


async def test_run_codex__argv__contains_exec_model_effort_and_prompt(
    tmp_path, monkeypatch, workspace
):
    _fake_cli(tmp_path, monkeypatch, 'echo "$@" > args.txt')

    await run_codex(
        prompt="review this", workspace=workspace, model="gpt-5.4", effort="high", timeout=10
    )

    args = (workspace / "args.txt").read_text()
    assert "exec" in args
    assert "gpt-5.4" in args
    assert "model_reasoning_effort=high" in args
    assert "workspace-write" in args
    assert "review this" in args


async def test_run_codex__sandbox_override__lands_in_argv(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, 'echo "$@" > args.txt')

    await run_codex(
        prompt="p", workspace=workspace, model="gpt-5.4", effort="high",
        timeout=10, sandbox="danger-full-access",
    )

    assert "danger-full-access" in (workspace / "args.txt").read_text()


async def test_run_codex__nonzero_exit__raises_codex_error(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, "echo broken >&2; exit 3")

    with pytest.raises(CodexError, match="exited 3"):
        await run_codex(
            prompt="p", workspace=workspace, model="gpt-5.4", effort="high", timeout=10
        )


async def test_run_codex__usage_limit_message__raises_quota_error(
    tmp_path, monkeypatch, workspace
):
    _fake_cli(tmp_path, monkeypatch, 'echo "You have hit your usage limit."; exit 1')

    with pytest.raises(CodexQuotaError):
        await run_codex(
            prompt="p", workspace=workspace, model="gpt-5.4", effort="high", timeout=10
        )


async def test_run_codex__rate_limit_message__raises_plain_codex_error(
    tmp_path, monkeypatch, workspace
):
    # A per-minute rate limit is transient and must be retried, not treated as quota exhaustion.
    _fake_cli(tmp_path, monkeypatch, 'echo "you hit a rate limit, please retry"; exit 1')

    with pytest.raises(CodexError, match="exited 1") as excinfo:
        await run_codex(
            prompt="p", workspace=workspace, model="gpt-5.4", effort="high", timeout=10
        )
    assert not isinstance(excinfo.value, CodexQuotaError)


async def test_run_codex__quota_marker_only_in_early_output__raises_plain_codex_error(
    tmp_path, monkeypatch, workspace
):
    # "rate limit" appears early (e.g. echoed prompt), tail is 3000 chars of padding.
    script = 'echo "the diff mentions rate limit handling"; head -c 3000 /dev/zero | tr "\\0" x; exit 7'
    _fake_cli(tmp_path, monkeypatch, script)

    with pytest.raises(CodexError, match="exited 7"):
        await run_codex(
            prompt="p", workspace=workspace, model="gpt-5.4", effort="high", timeout=10
        )


async def test_run_codex__quota_markers_with_exit_zero__returns_output(
    tmp_path, monkeypatch, workspace
):
    _fake_cli(tmp_path, monkeypatch, 'echo "usage limit and rate limit discussed"')

    output = await run_codex(
        prompt="p", workspace=workspace, model="gpt-5.4", effort="high", timeout=10
    )

    assert "usage limit" in output


async def test_run_codex__env__allowlists_only_safe_vars(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, "env > env.txt")
    monkeypatch.setenv("REVIEWBOT_GH_WEBHOOK_SECRET", "topsecret")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_fake")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@host/db")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex_home"))

    await run_codex(
        prompt="p", workspace=workspace, model="gpt-5.4", effort="high", timeout=10
    )

    env_dump = (workspace / "env.txt").read_text()
    assert "REVIEWBOT_GH_WEBHOOK_SECRET" not in env_dump
    assert "STRIPE_SECRET_KEY" not in env_dump
    assert "DATABASE_URL" not in env_dump
    assert "PATH=" in env_dump
    assert "CODEX_HOME=" in env_dump


async def test_run_codex__timeout__kills_process_group_promptly(
    tmp_path, monkeypatch, workspace
):
    # Background child inherits the stdout pipe; without a process-group kill,
    # run_codex would block until the child exits (~5s).
    _fake_cli(tmp_path, monkeypatch, "sleep 5 & sleep 5")

    start = time.monotonic()
    with pytest.raises(CodexError, match="timed out"):
        await run_codex(
            prompt="p", workspace=workspace, model="gpt-5.4", effort="high", timeout=1
        )
    assert time.monotonic() - start < 4


async def test_run_codex__cancelled__kills_process_group(tmp_path, monkeypatch, workspace):
    # ARQ can cancel jobs; the codex process group must not outlive the task.
    pid_file = workspace / "child.pid"
    _fake_cli(tmp_path, monkeypatch, f'sleep 30 & echo $! > "{pid_file}"; wait')

    task = asyncio.create_task(run_codex(
        prompt="p", workspace=workspace, model="gpt-5.4", effort="high", timeout=60
    ))
    for _ in range(500):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("fake codex never started")
    child_pid = int(pid_file.read_text())

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    for _ in range(300):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        os.kill(child_pid, 9)
        pytest.fail("background child survived cancellation")
