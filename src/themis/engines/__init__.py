"""Review engines. ENGINE_NAMES is the single source of truth for the valid
values of THEMIS_ENGINE and the repo config engine: key."""

from themis.engines.base import Engine, EngineError, EngineQuotaError, EngineUnavailableError
from themis.engines.claude import ClaudeEngine
from themis.engines.codex import CodexEngine
from themis.engines.glm import GlmEngine

ENGINE_NAMES = ("codex", "claude", "glm")
# Engines with native skill discovery (the claude harness reads
# .claude/skills itself). Anything outside this set gets the skills
# bridge: a synthesized index of the base-revision skills (issue #49).
NATIVE_SKILLS_ENGINES = frozenset({"claude", "glm"})

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
    raise ValueError(f"unknown engine {name!r}")
