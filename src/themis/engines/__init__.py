"""Review engines. ENGINE_NAMES is the single source of truth for the valid
values of THEMIS_ENGINE and the repo config engine: key."""

from themis.engines.base import Engine, EngineError, EngineQuotaError, EngineUnavailableError
from themis.engines.claude import ClaudeEngine
from themis.engines.codex import CodexEngine
from themis.engines.glm import GlmEngine
from themis.engines.kimi import KimiEngine
from themis.engines.openrouter import OpenRouterEngine

ENGINE_NAMES = ("codex", "claude", "glm", "kimi", "openrouter")
# Engines with native skill discovery (the claude harness reads
# .claude/skills itself). Anything outside this set gets the skills
# bridge: a synthesized index of the base-revision skills (issue #49).
NATIVE_SKILLS_ENGINES = frozenset({"claude", "glm", "kimi", "openrouter"})

__all__ = [
    "ENGINE_NAMES", "Engine", "EngineError", "EngineQuotaError",
    "EngineUnavailableError", "NATIVE_SKILLS_ENGINES", "resolve",
]


def resolve(name: str, *, codex_sandbox: str = "workspace-write") -> Engine:
    if name == "codex":
        return CodexEngine(sandbox=codex_sandbox)
    if name == "claude":
        return ClaudeEngine()
    if name == "glm":
        return GlmEngine()
    if name == "kimi":
        return KimiEngine()
    if name == "openrouter":
        return OpenRouterEngine()
    raise ValueError(f"unknown engine {name!r}")
