"""Credential-isolated engine execution service."""

import asyncio
import logging
import os
import secrets
import tempfile
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from themis.config import VALID_SANDBOXES, _env_concurrency
from themis.engines import (
    AUTH_PROBE_BUDGET,
    ENGINE_NAMES,
    EngineAuthError,
    EngineError,
    EngineQuotaError,
    resolve,
)
from themis.output import OUTPUT_DIR, OUTPUT_FILES
from themis.security import redact_outbound

logger = logging.getLogger(__name__)

# Auth markers match the agent-visible output tail, so a prompt-steered agent
# inside a hostile PR can echo one and forge a terminal auth failure (which
# skips retries and posts an operator alarm). The probe re-runs the engine
# with this fixed trusted prompt in an empty scratch workspace — no PR
# content, nothing to steer — and only a probe that itself raises
# EngineAuthError confirms the credentials are really dead.
_PROBE_PROMPT = "Reply with the single word: ok"
_PROBE_TIMEOUT = 120.0


async def _confirm_auth_dead(engine, model: str, effort: str) -> bool:
    with tempfile.TemporaryDirectory(prefix="themis-auth-probe-") as scratch:
        try:
            await engine.run(
                prompt=_PROBE_PROMPT,
                workspace=Path(scratch),
                model=model,
                effort=effort,
                timeout=_PROBE_TIMEOUT,
                web_access=False,
            )
        except EngineAuthError:
            return True
        except EngineError:
            # Ambiguous (quota, transient): stay retryable rather than
            # falsely telling the operator to re-authenticate.
            return False
    return False


def _redact_agent_outputs(workspace: Path) -> None:
    """Remove exact engine credentials before files cross to the controller."""
    output = workspace / OUTPUT_DIR
    for name in OUTPUT_FILES:
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
    native_context: bool = False
    native_skills: bool = False


def create_agent_app() -> FastAPI:
    token = os.getenv("THEMIS_AGENT_TOKEN") or ""
    if not token:
        raise RuntimeError("THEMIS_AGENT_TOKEN is required for the agent role")
    root = Path(os.getenv("THEMIS_WORKSPACE_ROOT") or "/tmp/themis").resolve()
    sandbox = os.getenv("THEMIS_CODEX_SANDBOX") or "workspace-write"
    if sandbox not in VALID_SANDBOXES:
        raise RuntimeError(
            f"invalid THEMIS_CODEX_SANDBOX {sandbox!r}; expected one of {VALID_SANDBOXES}"
        )
    # Engine-run slots, same lenient THEMIS_CONCURRENCY clamp as the
    # controller so both sides of the split agree on parallelism.
    slot = asyncio.Semaphore(_env_concurrency())
    app = FastAPI(title="themis-agent")
    app.state.slot = slot

    def authorize(authorization: str | None) -> None:
        scheme, _, supplied = (authorization or "").partition(" ")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str,
        # which would turn a garbage token into a 500 instead of a 401.
        if scheme != "Bearer" or not secrets.compare_digest(
            supplied.encode(), token.encode()
        ):
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
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "engine_credentials_unavailable",
                    "message": f"{request.engine} credentials unavailable",
                },
            )
        try:
            async with slot:
                output = await engine.run(
                    prompt=request.prompt,
                    workspace=workspace,
                    model=request.model,
                    effort=request.effort,
                    timeout=request.timeout,
                    web_access=request.web_access,
                    native_context=request.native_context,
                    native_skills=request.native_skills,
                )
            _redact_agent_outputs(workspace)
            return {"output": redact_outbound(output)}
        except EngineAuthError as error:
            try:
                # The budget bounds slot wait + probe run: the controller's
                # HTTP allowance is timeout + 30 + AUTH_PROBE_BUDGET, so an
                # unbounded wait here would make it hang up mid-probe and
                # misread a genuine auth death as a transient agent error.
                async with asyncio.timeout(AUTH_PROBE_BUDGET):
                    # Probe holds the slot: it is a real engine run and must
                    # respect the same parallelism budget as the job.
                    async with slot:
                        confirmed = await _confirm_auth_dead(
                            engine, request.model, request.effort
                        )
            except TimeoutError:
                confirmed = False
            logger.warning(
                "themis_auth_probe engine=%s confirmed=%s",
                request.engine, confirmed,
            )
            if confirmed:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "engine_auth_expired", "message": str(error)},
                ) from error
            raise HTTPException(status_code=502, detail=str(error)) from error
        except EngineQuotaError as error:
            raise HTTPException(status_code=429, detail=str(error)) from error
        except EngineError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    return app
