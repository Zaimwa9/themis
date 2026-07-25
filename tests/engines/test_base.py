"""Shared runner classification: auth markers beat quota markers."""

from pathlib import Path

import pytest

from themis.engines.base import (
    EngineAuthError,
    EngineError,
    EngineQuotaError,
    run_cli,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


async def _run(workspace: Path, script: str, **kwargs) -> str:
    defaults = dict(
        name="fake",
        command=["sh", "-c", script],
        workspace=workspace,
        env={"PATH": "/usr/bin:/bin"},
        timeout=10,
        quota_markers=(),
    )
    defaults.update(kwargs)
    return await run_cli(**defaults)


async def test_run_cli__auth_marker_on_failure__raises_engine_auth_error(workspace):
    with pytest.raises(EngineAuthError):
        await _run(
            workspace, "echo 'please log out and sign in again'; exit 1",
            auth_markers=("log out and sign in again",),
        )


async def test_run_cli__auth_marker_beats_quota_marker(workspace):
    # An auth-dead CLI can emit secondary quota-looking noise; auth wins.
    with pytest.raises(EngineAuthError):
        await _run(
            workspace,
            "echo 'usage limit'; echo 'log out and sign in again'; exit 1",
            auth_markers=("log out and sign in again",),
            quota_markers=("usage limit",),
        )


async def test_run_cli__auth_marker_in_successful_output__is_ignored(workspace):
    output = await _run(
        workspace, "echo 'log out and sign in again'",
        auth_markers=("log out and sign in again",),
    )
    assert "log out" in output


async def test_run_cli__no_marker_match__stays_engine_error(workspace):
    with pytest.raises(EngineError) as excinfo:
        await _run(
            workspace, "echo 'boom'; exit 1",
            auth_markers=("log out and sign in again",),
        )
    assert not isinstance(excinfo.value, (EngineAuthError, EngineQuotaError))
