import os
import stat
from pathlib import Path

import pytest

from themis.engines.base import EngineError, EngineQuotaError
from themis.engines.glm import GlmEngine

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
        model="glm-5.2", effort="high", timeout=10,
    )
    kwargs.update(overrides)
    return await GlmEngine().run(**kwargs)


async def test_run__env__key_mapped_and_endpoint_baked(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, "env > env.txt")
    monkeypatch.setenv("GLM_API_KEY", "glm-key-123456")
    # A hostile/misconfigured host env must not redirect the provider key.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://attacker.example")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "host-leak")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake")

    await _run(workspace)

    env_dump = (workspace / "env.txt").read_text()
    assert "ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic" in env_dump
    assert "ANTHROPIC_AUTH_TOKEN=glm-key-123456" in env_dump
    assert "API_TIMEOUT_MS=3000000" in env_dump
    # The raw key var and the claude subscription token never cross over.
    assert "GLM_API_KEY" not in env_dump
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env_dump
    assert "attacker.example" not in env_dump
    assert "host-leak" not in env_dump


async def test_run__argv__hardening_flags_and_model(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, 'echo "$@" > args.txt')

    await _run(workspace)

    args = (workspace / "args.txt").read_text()
    assert "glm-5.2" in args
    assert "--dangerously-skip-permissions" in args
    assert "--safe-mode" in args
    assert "--setting-sources  --strict-mcp-config" in args
    assert '--mcp-config {"mcpServers":{}}' in args
    assert "--disallowedTools WebFetch,WebSearch" in args


async def test_run__native_context__project_setting_source(
    tmp_path, monkeypatch, workspace
):
    _fake_cli(tmp_path, monkeypatch, 'echo "$@" > args.txt')

    await _run(workspace, native_context=True)

    args = (workspace / "args.txt").read_text()
    assert "--setting-sources project" in args
    assert "--strict-mcp-config" in args


async def test_run__config_dir__isolated(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, "env > env.txt")

    await _run(workspace)

    env_dump = (workspace / "env.txt").read_text()
    config_dir = next(
        line.removeprefix("CLAUDE_CONFIG_DIR=")
        for line in env_dump.splitlines()
        if line.startswith("CLAUDE_CONFIG_DIR=")
    )
    assert config_dir != os.path.expanduser("~/.claude")


@pytest.mark.parametrize(
    "message",
    [
        # True plan exhaustion (Z.ai 1308/1310/1309): retryable by design until
        # quota can be classified from provider-structured output (#28) —
        # any text marker here could be echoed by a prompt-steered agent.
        "Usage limit reached for the past 5 hours. Resets at 18:00.",
        "Weekly/Monthly Limit Exhausted. Your limit will reset at Monday.",
        "Your GLM Coding Plan package has expired.",
        # Transient throttling (Z.ai 1302) and generic agent prose.
        "Rate limit reached for requests",
        "the retry limit exhausted while calling the API",
    ],
)
async def test_run__any_failure__is_retryable_engine_error(
    tmp_path, monkeypatch, workspace, message
):
    _fake_cli(tmp_path, monkeypatch, f'echo "{message}"; exit 1')

    with pytest.raises(EngineError) as exc_info:
        await _run(workspace)
    assert not isinstance(exc_info.value, EngineQuotaError)


def test_available__key_set__true(monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "glm-key-123456")

    assert GlmEngine().available() is True


def test_available__key_missing__false(monkeypatch):
    monkeypatch.delenv("GLM_API_KEY", raising=False)

    assert GlmEngine().available() is False
