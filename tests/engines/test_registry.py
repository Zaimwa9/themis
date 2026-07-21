import pytest

from themis.engines import ENGINE_NAMES, resolve
from themis.engines.claude import ClaudeEngine
from themis.engines.codex import CodexEngine
from themis.engines.glm import GlmEngine
from themis.engines.kimi import KimiEngine
from themis.engines.openrouter import OpenRouterEngine


def test_engine_names():
    assert ENGINE_NAMES == ("codex", "claude", "glm", "kimi", "openrouter")


def test_resolve_codex_carries_sandbox():
    engine = resolve("codex", codex_sandbox="danger-full-access")
    assert isinstance(engine, CodexEngine)
    assert engine._sandbox == "danger-full-access"


def test_resolve_claude():
    assert isinstance(resolve("claude"), ClaudeEngine)


def test_resolve_glm():
    assert isinstance(resolve("glm"), GlmEngine)


def test_resolve_kimi():
    assert isinstance(resolve("kimi"), KimiEngine)


def test_resolve_openrouter():
    assert isinstance(resolve("openrouter"), OpenRouterEngine)


def test_resolve_unknown_raises():
    with pytest.raises(ValueError, match="unknown engine"):
        resolve("gemini")


def test_native_skills_engines_are_claude_harness_only():
    # The skills bridge (issue #49) keys off this set: engines outside it
    # get the synthesized index instead of native discovery.
    from themis.engines import ENGINE_NAMES, NATIVE_SKILLS_ENGINES

    assert NATIVE_SKILLS_ENGINES == frozenset({"claude", "glm", "kimi", "openrouter"})
    assert NATIVE_SKILLS_ENGINES <= frozenset(ENGINE_NAMES)
