"""Temporary compatibility shim over themis.engines; removed once service.py
resolves engines directly."""

from pathlib import Path

from themis.engines.base import EngineError as CodexError
from themis.engines.base import EngineQuotaError as CodexQuotaError
from themis.engines.codex import CodexEngine

__all__ = ["CodexError", "CodexQuotaError", "run_codex"]


async def run_codex(
    *, prompt: str, workspace: Path, model: str, effort: str,
    timeout: float, sandbox: str = "workspace-write",
) -> str:
    return await CodexEngine(sandbox=sandbox).run(
        prompt=prompt, workspace=workspace, model=model, effort=effort, timeout=timeout,
    )
