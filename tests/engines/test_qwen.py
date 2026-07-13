import os
import stat
from pathlib import Path

import pytest

from themis.engines.base import EngineError, EngineQuotaError
from themis.engines.qwen import QwenEngine

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
        model="qwen3.7-plus", effort="high", timeout=10,
    )
    kwargs.update(overrides)
    return await QwenEngine().run(**kwargs)


async def test_run__env__key_mapped_and_endpoint_baked(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, "env > env.txt")
    monkeypatch.setenv("QWEN_API_KEY", "sk-sp-fake123456")
    monkeypatch.setenv("GLM_API_KEY", "glm-key-123456")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake")

    await _run(workspace)

    env_dump = (workspace / "env.txt").read_text()
    assert (
        "ANTHROPIC_BASE_URL=https://coding-intl.dashscope.aliyuncs.com/apps/anthropic"
        in env_dump
    )
    assert "ANTHROPIC_AUTH_TOKEN=sk-sp-fake123456" in env_dump
    # Sibling engines' credentials never cross over.
    assert "QWEN_API_KEY" not in env_dump
    assert "GLM_API_KEY" not in env_dump
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env_dump


async def test_run__argv__hardening_flags_and_model(tmp_path, monkeypatch, workspace):
    _fake_cli(tmp_path, monkeypatch, 'echo "$@" > args.txt')

    await _run(workspace)

    args = (workspace / "args.txt").read_text()
    assert "qwen3.7-plus" in args
    assert "--dangerously-skip-permissions" in args
    assert "--safe-mode" in args
    assert "--setting-sources  --strict-mcp-config" in args


@pytest.mark.parametrize(
    "message",
    [
        "hour allocated quota exceeded",
        "week allocated quota exceeded",
        "month allocated quota exceeded",
    ],
)
async def test_run__plan_exhausted__raises_quota_error(
    tmp_path, monkeypatch, workspace, message
):
    _fake_cli(tmp_path, monkeypatch, f'echo "{message}"; exit 1')

    with pytest.raises(EngineQuotaError):
        await _run(workspace)


@pytest.mark.parametrize(
    "message",
    [
        # Documented as retryable: the platform adjusts concurrency dynamically.
        "concurrency allocated quota exceeded",
        # Throttling.* family resolves in ~60s.
        "Requests rate limit exceeded, please try again later.",
        # Billing arrears never auto-reset; the quota comment ("mention the
        # bot once it resets") would mislead, so it stays a plain failure.
        "Access denied, please make sure your account is in good standing.",
    ],
)
async def test_run__transient_or_billing__is_plain_engine_error(
    tmp_path, monkeypatch, workspace, message
):
    _fake_cli(tmp_path, monkeypatch, f'echo "{message}"; exit 1')

    with pytest.raises(EngineError) as exc_info:
        await _run(workspace)
    assert not isinstance(exc_info.value, EngineQuotaError)


def test_available__key_set__true(monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "sk-sp-fake123456")

    assert QwenEngine().available() is True


def test_available__key_missing__false(monkeypatch):
    monkeypatch.delenv("QWEN_API_KEY", raising=False)

    assert QwenEngine().available() is False
