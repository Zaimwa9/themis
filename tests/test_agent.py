from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from themis.agent import create_agent_app
from themis.engines import EngineAuthError, EngineError


class FakeEngine:
    name = "claude"
    last_kwargs: dict = {}

    def available(self):
        return True

    async def run(self, *, workspace: Path, **kwargs):
        type(self).last_kwargs = kwargs
        (workspace / "ran").write_text("yes")
        if kwargs.get("prompt") == "auth-dead":
            raise EngineAuthError("claude credentials expired: run /login")
        if kwargs.get("prompt") == "leak":
            output = workspace / ".review-output"
            output.mkdir()
            (output / "summary.md").write_text("oauth-secret-value")
            return "oauth-secret-value"
        return "done"


def client(monkeypatch, tmp_path):
    monkeypatch.setenv("THEMIS_AGENT_TOKEN", "agent-secret")
    monkeypatch.setenv("THEMIS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("themis.agent.resolve", lambda *args, **kwargs: FakeEngine())
    return TestClient(create_agent_app())


def test_invalid_codex_sandbox_fails_at_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("THEMIS_AGENT_TOKEN", "agent-secret")
    monkeypatch.setenv("THEMIS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("THEMIS_CODEX_SANDBOX", "workspce-write")
    with pytest.raises(RuntimeError, match="invalid THEMIS_CODEX_SANDBOX"):
        create_agent_app()


def test_engine_slot_sized_from_concurrency_env(monkeypatch, tmp_path):
    monkeypatch.setenv("THEMIS_AGENT_TOKEN", "agent-secret")
    monkeypatch.setenv("THEMIS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("THEMIS_CONCURRENCY", "2")
    assert create_agent_app().state.slot._value == 2


def test_engine_slot_invalid_concurrency_degrades_to_one(monkeypatch, tmp_path):
    monkeypatch.setenv("THEMIS_AGENT_TOKEN", "agent-secret")
    monkeypatch.setenv("THEMIS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("THEMIS_CONCURRENCY", "99")  # over the cap: degrade to 1
    assert create_agent_app().state.slot._value == 1


def test_run_requires_agent_token(monkeypatch, tmp_path):
    response = client(monkeypatch, tmp_path).post("/run", json={
        "engine": "claude", "workspace": "job123", "prompt": "p",
        "model": "opus", "effort": "high", "timeout": 10,
    })
    assert response.status_code == 401


def test_run_non_ascii_token_is_401_not_500(monkeypatch, tmp_path):
    # Raw latin-1 header bytes, as any non-httpx client can send them;
    # compare_digest on str raises TypeError (a 500) for non-ASCII input.
    response = client(monkeypatch, tmp_path).post(
        "/run",
        headers={b"Authorization": "Bearer sécrét".encode("latin-1")},
        json={
            "engine": "claude", "workspace": "job123", "prompt": "p",
            "model": "opus", "effort": "high", "timeout": 10,
        },
    )
    assert response.status_code == 401


def test_run_rejects_workspace_traversal(monkeypatch, tmp_path):
    response = client(monkeypatch, tmp_path).post(
        "/run",
        headers={"Authorization": "Bearer agent-secret"},
        json={
            "engine": "claude", "workspace": "../outside", "prompt": "p",
            "model": "opus", "effort": "high", "timeout": 10,
        },
    )
    assert response.status_code == 400


def test_run_executes_inside_shared_workspace(monkeypatch, tmp_path):
    workspace = tmp_path / "job123"
    workspace.mkdir()
    response = client(monkeypatch, tmp_path).post(
        "/run",
        headers={"Authorization": "Bearer agent-secret"},
        json={
            "engine": "claude", "workspace": "job123", "prompt": "p",
            "model": "opus", "effort": "high", "timeout": 10,
        },
    )
    assert response.status_code == 200
    assert response.json() == {"output": "done"}
    assert (workspace / "ran").read_text() == "yes"


def test_run_passes_native_capability_flags_to_engine(monkeypatch, tmp_path):
    (tmp_path / "job123").mkdir()
    response = client(monkeypatch, tmp_path).post(
        "/run",
        headers={"Authorization": "Bearer agent-secret"},
        json={
            "engine": "claude", "workspace": "job123", "prompt": "p",
            "model": "opus", "effort": "high", "timeout": 10,
            "native_context": True, "native_skills": True,
        },
    )
    assert response.status_code == 200
    assert FakeEngine.last_kwargs["native_context"] is True
    assert FakeEngine.last_kwargs["native_skills"] is True


def test_run_defaults_native_capability_flags_off(monkeypatch, tmp_path):
    (tmp_path / "job123").mkdir()
    response = client(monkeypatch, tmp_path).post(
        "/run",
        headers={"Authorization": "Bearer agent-secret"},
        json={
            "engine": "claude", "workspace": "job123", "prompt": "p",
            "model": "opus", "effort": "high", "timeout": 10,
        },
    )
    assert response.status_code == 200
    assert FakeEngine.last_kwargs["native_context"] is False
    assert FakeEngine.last_kwargs["native_skills"] is False


def test_run_redacts_engine_secret_before_crossing_boundary(monkeypatch, tmp_path):
    workspace = tmp_path / "job123"
    workspace.mkdir()
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-secret-value")
    response = client(monkeypatch, tmp_path).post(
        "/run",
        headers={"Authorization": "Bearer agent-secret"},
        json={
            "engine": "claude", "workspace": "job123", "prompt": "leak",
            "model": "opus", "effort": "high", "timeout": 10,
        },
    )
    assert response.json() == {"output": "[redacted]"}
    assert (workspace / ".review-output" / "summary.md").read_text() == "[redacted]"


def test_missing_credentials_return_machine_readable_code(monkeypatch, tmp_path):
    class UnavailableEngine(FakeEngine):
        def available(self):
            return False

    workspace = tmp_path / "job123"
    workspace.mkdir()
    monkeypatch.setenv("THEMIS_AGENT_TOKEN", "agent-secret")
    monkeypatch.setenv("THEMIS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("themis.agent.resolve", lambda *args, **kwargs: UnavailableEngine())
    response = TestClient(create_agent_app()).post(
        "/run",
        headers={"Authorization": "Bearer agent-secret"},
        json={
            "engine": "claude", "workspace": "job123", "prompt": "p",
            "model": "opus", "effort": "high", "timeout": 10,
        },
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "engine_credentials_unavailable"


class AuthDeadEngine(FakeEngine):
    """Credentials are actually dead: every run fails, including the probe."""

    calls: list[dict] = []

    async def run(self, *, workspace: Path, **kwargs):
        type(self).calls.append({"workspace": workspace, **kwargs})
        raise EngineAuthError("claude credentials expired: run /login")


class ForgedAuthEngine(FakeEngine):
    """Only the PR-driven run 'fails' with an auth marker; the out-of-band
    probe (trusted prompt, scratch workspace) succeeds — forged."""

    async def run(self, *, workspace: Path, **kwargs):
        if kwargs.get("prompt") == "auth-dead":
            raise EngineAuthError("claude credentials expired: run /login")
        return "ok"


class FlakyProbeEngine(FakeEngine):
    """Auth marker on the job run, but the probe fails for another reason."""

    async def run(self, *, workspace: Path, **kwargs):
        if kwargs.get("prompt") == "auth-dead":
            raise EngineAuthError("claude credentials expired: run /login")
        raise EngineError("claude exited 1: transient")


def _auth_dead_request(monkeypatch, tmp_path, engine):
    workspace = tmp_path / "job123"
    workspace.mkdir()
    monkeypatch.setenv("THEMIS_AGENT_TOKEN", "agent-secret")
    monkeypatch.setenv("THEMIS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("themis.agent.resolve", lambda *args, **kwargs: engine)
    return TestClient(create_agent_app()).post(
        "/run",
        headers={"Authorization": "Bearer agent-secret"},
        json={
            "engine": "claude", "workspace": "job123", "prompt": "auth-dead",
            "model": "opus", "effort": "high", "timeout": 10,
        },
    )


def test_run_confirmed_auth_death_returns_503_with_auth_code(monkeypatch, tmp_path):
    AuthDeadEngine.calls = []
    response = _auth_dead_request(monkeypatch, tmp_path, AuthDeadEngine())
    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "engine_auth_expired",
        "message": "claude credentials expired: run /login",
    }


def test_auth_probe_runs_trusted_prompt_in_scratch_workspace(monkeypatch, tmp_path):
    AuthDeadEngine.calls = []
    _auth_dead_request(monkeypatch, tmp_path, AuthDeadEngine())
    assert len(AuthDeadEngine.calls) == 2
    probe = AuthDeadEngine.calls[1]
    # The probe must be immune to PR-controlled content: its own prompt, a
    # workspace outside the job tree, and no web access.
    assert probe["prompt"] != "auth-dead"
    assert probe["workspace"] != tmp_path / "job123"
    assert not probe["workspace"].is_relative_to(tmp_path)
    assert probe["web_access"] is False


def test_run_forged_auth_marker_downgrades_to_502(monkeypatch, tmp_path):
    response = _auth_dead_request(monkeypatch, tmp_path, ForgedAuthEngine())
    assert response.status_code == 502
    assert "claude credentials expired" in response.json()["detail"]


def test_run_ambiguous_probe_failure_downgrades_to_502(monkeypatch, tmp_path):
    response = _auth_dead_request(monkeypatch, tmp_path, FlakyProbeEngine())
    assert response.status_code == 502
