from pathlib import Path

from fastapi.testclient import TestClient

from themis.agent import create_agent_app


class FakeEngine:
    name = "claude"

    def available(self):
        return True

    async def run(self, *, workspace: Path, **kwargs):
        (workspace / "ran").write_text("yes")
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


def test_run_requires_agent_token(monkeypatch, tmp_path):
    response = client(monkeypatch, tmp_path).post("/run", json={
        "engine": "claude", "workspace": "job123", "prompt": "p",
        "model": "opus", "effort": "high", "timeout": 10,
    })
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
