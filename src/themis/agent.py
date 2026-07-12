"""Credential-isolated engine execution service."""

import asyncio
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from themis.engines import ENGINE_NAMES, EngineError, EngineQuotaError, resolve
from themis.security import redact_outbound

_OUTPUT_FILES = ("summary.md", "actions.json", "reply.md")


def _redact_agent_outputs(workspace: Path) -> None:
    """Remove exact engine credentials before files cross to the controller."""
    output = workspace / ".review-output"
    for name in _OUTPUT_FILES:
        path = output / name
        if not path.exists():
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(workspace.resolve()) or not resolved.is_file():
            continue
        resolved.write_text(redact_outbound(resolved.read_text(errors="replace")))


class RunRequest(BaseModel):
    engine: str
    workspace: str
    prompt: str
    model: str
    effort: str
    timeout: float
    web_access: bool = False


def create_agent_app() -> FastAPI:
    token = os.getenv("THEMIS_AGENT_TOKEN") or ""
    if not token:
        raise RuntimeError("THEMIS_AGENT_TOKEN is required for the agent role")
    root = Path(os.getenv("THEMIS_WORKSPACE_ROOT") or "/tmp/themis").resolve()
    sandbox = os.getenv("THEMIS_CODEX_SANDBOX") or "workspace-write"
    slot = asyncio.Semaphore(1)
    app = FastAPI(title="themis-agent")

    def authorize(authorization: str | None) -> None:
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme != "Bearer" or not secrets.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="invalid agent token")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/run")
    async def run(request: RunRequest, authorization: str | None = Header(default=None)):
        authorize(authorization)
        if request.engine not in ENGINE_NAMES:
            raise HTTPException(status_code=400, detail="unknown engine")
        if not request.workspace or Path(request.workspace).name != request.workspace:
            raise HTTPException(status_code=400, detail="invalid workspace")
        workspace = (root / request.workspace).resolve()
        if not workspace.is_relative_to(root) or not workspace.is_dir():
            raise HTTPException(status_code=404, detail="workspace not found")
        engine = resolve(request.engine, codex_sandbox=sandbox)
        if not engine.available():
            raise HTTPException(status_code=503, detail=f"{request.engine} credentials unavailable")
        try:
            async with slot:
                output = await engine.run(
                    prompt=request.prompt,
                    workspace=workspace,
                    model=request.model,
                    effort=request.effort,
                    timeout=request.timeout,
                    web_access=request.web_access,
                )
            _redact_agent_outputs(workspace)
            return {"output": redact_outbound(output)}
        except EngineQuotaError as error:
            raise HTTPException(status_code=429, detail=str(error)) from error
        except EngineError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    return app
