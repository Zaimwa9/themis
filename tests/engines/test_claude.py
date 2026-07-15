import os
import stat
from pathlib import Path

import pytest

from themis.engines.base import EngineError, EngineQuotaError
from themis.engines.claude import ClaudeEngine

pytestmark = pytest.mark.asyncio


def _fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / "claude"
    exe.write_text(f"#!/bin/sh\n{script}\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


async def _run(workspace: Path, **overrides) -> str:
    kwargs = dict(
        prompt="review this", workspace=workspace,
        model="claude-opus-4-6[1m]", effort="high", timeout=10,
    )
    kwargs.update(overrides)
    return await ClaudeEngine().run(**kwargs)


async def test_run__exit_zero__returns_output(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, "echo done")

    assert "done" in await _run(workspace)


async def test_run__argv__model_passthrough_and_flags(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, 'echo "$@" > args.txt')

    await _run(workspace)

    args = (workspace / "args.txt").read_text()
    assert "claude-opus-4-6[1m]" in args
    assert "--dangerously-skip-permissions" in args
    assert "--safe-mode" in args
    assert "--setting-sources  --strict-mcp-config" in args
    assert '--mcp-config {"mcpServers":{}}' in args
    assert "--output-format text" in args
    assert "review this" in args
    # effort has no claude CLI flag; it must not leak into argv
    assert "high" not in args


async def test_run__native_context__project_setting_source(
    tmp_path, monkeypatch, workspace
):
    _fake_cli(tmp_path, monkeypatch, 'echo "$@" > args.txt')

    await _run(workspace, native_context=True)

    args = (workspace / "args.txt").read_text()
    assert "--setting-sources project" in args
    # Native discovery must not weaken the MCP/permissions hardening.
    assert "--strict-mcp-config" in args
    assert '--mcp-config {"mcpServers":{}}' in args
    assert "--safe-mode" in args


async def test_run__native_skills_only__project_setting_source(
    tmp_path, monkeypatch, workspace
):
    _fake_cli(tmp_path, monkeypatch, 'echo "$@" > args.txt')

    await _run(workspace, native_skills=True)

    assert "--setting-sources project" in (workspace / "args.txt").read_text()


async def test_run__default__web_tools_disallowed(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, 'echo "$@" > args.txt')

    await _run(workspace)

    assert "--disallowedTools WebFetch,WebSearch" in (workspace / "args.txt").read_text()


async def test_run__web_access__no_disallowed_tools(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, 'echo "$@" > args.txt')

    await _run(workspace, web_access=True)

    assert "disallowedTools" not in (workspace / "args.txt").read_text()


async def test_run__env__token_passed_secrets_stripped(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, "env > env.txt")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex_home"))
    monkeypatch.setenv("THEMIS_GH_WEBHOOK_SECRET", "topsecret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@host/db")

    await _run(workspace)

    env_dump = (workspace / "env.txt").read_text()
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-fake" in env_dump
    assert "CODEX_HOME" not in env_dump
    assert "THEMIS_GH_WEBHOOK_SECRET" not in env_dump
    assert "DATABASE_URL" not in env_dump
    assert "DISABLE_AUTOUPDATER=1" in env_dump
    assert "DISABLE_TELEMETRY=1" in env_dump
    assert "DISABLE_ERROR_REPORTING=1" in env_dump
    assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1" in env_dump
    config_dir = next(
        line.removeprefix("CLAUDE_CONFIG_DIR=")
        for line in env_dump.splitlines()
        if line.startswith("CLAUDE_CONFIG_DIR=")
    )
    assert config_dir != os.path.expanduser("~/.claude")


async def test_run__subscription_limit__raises_quota_error(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, "echo \"You've hit your session limit\"; exit 1")

    with pytest.raises(EngineQuotaError):
        await _run(workspace)


async def test_run__generic_rate_limit__is_retryable_engine_error(
    tmp_path, monkeypatch, workspace
):
    _fake_cli(tmp_path, monkeypatch, 'echo "rate limit exceeded"; exit 1')

    with pytest.raises(EngineError) as exc_info:
        await _run(workspace)
    assert not isinstance(exc_info.value, EngineQuotaError)


async def test_run__nonzero_exit__raises_engine_error(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, "echo broken >&2; exit 3")

    with pytest.raises(EngineError, match="exited 3"):
        await _run(workspace)


async def test_run__timeout__raises_engine_error(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, "sleep 5 & sleep 5")

    with pytest.raises(EngineError, match="timed out"):
        await _run(workspace, timeout=1)


def test_available__token_set__true(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake")

    assert ClaudeEngine().available() is True


def test_available__token_missing__false(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    assert ClaudeEngine().available() is False
