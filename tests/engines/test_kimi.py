import os
import stat
from pathlib import Path

import pytest

from themis.engines.base import EngineError, EngineQuotaError
from themis.engines.kimi import KimiEngine

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
        model="kimi-k3", effort="high", timeout=10,
    )
    kwargs.update(overrides)
    return await KimiEngine().run(**kwargs)


async def test_run__env__key_mapped_and_endpoint_baked(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, "env > env.txt")
    monkeypatch.setenv("KIMI_API_KEY", "kimi-key-123456")
    # A hostile/misconfigured host env must not redirect the provider key,
    # and sibling engine credentials must stay invisible.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://attacker.example")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "host-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "host-api-key-leak")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake")
    monkeypatch.setenv("GLM_API_KEY", "glm-key-sibling")

    await _run(workspace)

    env_dump = (workspace / "env.txt").read_text()
    assert "ANTHROPIC_BASE_URL=https://api.moonshot.ai/anthropic" in env_dump
    assert "ANTHROPIC_AUTH_TOKEN=kimi-key-123456" in env_dump
    assert "API_TIMEOUT_MS=3000000" in env_dump
    # Explicitly blank per provider guidance, so the harness can never fall
    # back to direct Anthropic API-key auth; the host value must not leak.
    assert "ANTHROPIC_API_KEY=" in env_dump.splitlines()
    assert "host-api-key-leak" not in env_dump
    # The raw key var, sibling keys, and the claude subscription token
    # never cross over.
    assert "KIMI_API_KEY" not in env_dump
    assert "GLM_API_KEY" not in env_dump
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env_dump
    assert "attacker.example" not in env_dump
    assert "host-leak" not in env_dump


async def test_run__argv__hardening_flags_and_model(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, 'echo "$@" > args.txt')

    await _run(workspace)

    args = (workspace / "args.txt").read_text()
    assert "kimi-k3" in args
    assert "--dangerously-skip-permissions" in args
    assert "--safe-mode" in args
    assert "--setting-sources  --strict-mcp-config" in args
    assert '--mcp-config {"mcpServers":{}}' in args
    assert "--disallowedTools WebFetch,WebSearch" in args


@pytest.mark.parametrize(
    "message",
    [
        # Moonshot balance/limit prose: retryable by design — no text quota
        # markers (spoofable; pay-as-you-go exhaustion never auto-resets, so
        # the "retry later" quota comment would mislead). See spec.
        "Your account balance is insufficient. Please top up.",
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
    monkeypatch.setenv("KIMI_API_KEY", "kimi-key-123456")

    assert KimiEngine().available() is True


def test_available__key_missing__false(monkeypatch):
    monkeypatch.delenv("KIMI_API_KEY", raising=False)

    assert KimiEngine().available() is False
