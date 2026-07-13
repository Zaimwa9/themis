"""Review engines. ENGINE_NAMES is the single source of truth for the valid
values of THEMIS_ENGINE and the repo config engine: key."""

from themis.engines.base import Engine, EngineError, EngineQuotaError, EngineUnavailableError
from themis.engines.claude import ClaudeEngine
from themis.engines.codex import CodexEngine
from themis.engines.glm import GlmEngine
from themis.engines.qwen import QwenEngine

ENGINE_NAMES = ("codex", "claude", "glm", "qwen")

__all__ = [
    "ENGINE_NAMES", "Engine", "EngineError", "EngineQuotaError",
    "EngineUnavailableError", "resolve",
]


def resolve(name: str, *, codex_sandbox: str = "workspace-write") -> Engine:
    if name == "codex":
        return CodexEngine(sandbox=codex_sandbox)
    if name == "claude":
        return ClaudeEngine()
    if name == "glm":
        return GlmEngine()
    if name == "qwen":
        return QwenEngine()
    raise ValueError(f"unknown engine {name!r}")
