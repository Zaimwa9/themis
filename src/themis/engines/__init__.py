"""Review engines. ENGINE_NAMES is the single source of truth for the valid
values of THEMIS_ENGINE and the repo config engine: key."""

from themis.engines.base import Engine, EngineError, EngineQuotaError
from themis.engines.claude import ClaudeEngine
from themis.engines.codex import CodexEngine

ENGINE_NAMES = ("codex", "claude")

__all__ = ["ENGINE_NAMES", "Engine", "EngineError", "EngineQuotaError", "resolve"]


def resolve(name: str, *, codex_sandbox: str = "workspace-write") -> Engine:
    if name == "codex":
        return CodexEngine(sandbox=codex_sandbox)
    if name == "claude":
        return ClaudeEngine()
    raise ValueError(f"unknown engine {name!r}")
