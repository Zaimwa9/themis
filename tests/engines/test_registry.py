import pytest

from themis.engines import ENGINE_NAMES, resolve
from themis.engines.claude import ClaudeEngine
from themis.engines.codex import CodexEngine


def test_engine_names():
    assert ENGINE_NAMES == ("codex", "claude")


def test_resolve_codex_carries_sandbox():
    engine = resolve("codex", codex_sandbox="danger-full-access")
    assert isinstance(engine, CodexEngine)
    assert engine._sandbox == "danger-full-access"


def test_resolve_claude():
    assert isinstance(resolve("claude"), ClaudeEngine)


def test_resolve_unknown_raises():
    with pytest.raises(ValueError, match="unknown engine"):
        resolve("gemini")
