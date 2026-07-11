"""App factory: startup identity, healthz, signed-webhook smoke, self-registration."""

import hashlib
import hmac
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from themis.app import _discover_tunnel_url, _register_webhook, create_app
from themis.config import Settings, SettingsError

THEMIS_ENV_KEYS = (
    "THEMIS_GH_APP_CLIENT_ID", "THEMIS_GH_APP_PRIVATE_KEY", "THEMIS_GH_WEBHOOK_SECRET",
    "THEMIS_CODEX_SANDBOX", "THEMIS_REPOS", "THEMIS_PUBLIC_URL", "THEMIS_TUNNEL_API",
    "THEMIS_WEBHOOK_ENABLED", "THEMIS_API_TOKEN", "THEMIS_WORKSPACE_ROOT",
)


def _mock_async_client(handler):
    """httpx.AsyncClient subclass with a fixed transport, for monkeypatching
    httpx.AsyncClient itself so code that builds its own client picks it up."""

    class _Client(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    return _Client


def make_settings(**overrides) -> Settings:
    defaults = dict(
        gh_app_client_id="Iv1.test",
        gh_app_private_key_pem="-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
        gh_webhook_secret="hush",
        webhook_enabled=True,
        api_token=None,
        repos=None,
        codex_sandbox="workspace-write",
        workspace_root=Path("/tmp/themis-test"),
        public_url=None,
        tunnel_api=None,
    )
    return Settings(**{**defaults, **overrides})


@pytest.fixture
def quiet_github(monkeypatch):
    monkeypatch.setattr("themis.app.make_app_jwt", lambda *a: "jwt")
    monkeypatch.setattr("themis.app.get_app_slug", AsyncMock(return_value="test-reviewer"))
    monkeypatch.setattr("themis.router._ack", AsyncMock())
    return monkeypatch


def test_healthz_and_slug_resolved(quiet_github):
    with TestClient(create_app(make_settings())) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.app.state.bot_slug == "test-reviewer"


def test_signed_webhook_reaches_queue(quiet_github):
    payload = json.dumps({
        "action": "opened",
        "pull_request": {"number": 5, "draft": False},
        "repository": {"full_name": "acme/widgets"},
        "installation": {"id": 42},
        "sender": {"type": "User"},
    }).encode()
    signature = "sha256=" + hmac.new(b"hush", payload, hashlib.sha256).hexdigest()
    with TestClient(create_app(make_settings())) as client:
        response = client.post(
            "/webhook", content=payload,
            headers={"x-hub-signature-256": signature, "x-github-event": "pull_request"},
        )
    assert response.json()["status"] == "queued"


def test_webhook_self_registration_with_public_url(quiet_github):
    update = AsyncMock()
    quiet_github.setattr("themis.app.update_webhook_url", update)
    settings = make_settings(public_url="https://reviews.example.com")
    with TestClient(create_app(settings)):
        pass
    update.assert_awaited_once()
    assert update.await_args.args[1] == "https://reviews.example.com/webhook"


def test_no_registration_when_unconfigured(quiet_github):
    update = AsyncMock()
    quiet_github.setattr("themis.app.update_webhook_url", update)
    with TestClient(create_app(make_settings())):
        pass
    update.assert_not_awaited()


def test_registration_failure_does_not_kill_startup(quiet_github):
    update = AsyncMock(side_effect=httpx.ConnectError("boom"))
    quiet_github.setattr("themis.app.update_webhook_url", update)
    settings = make_settings(public_url="https://reviews.example.com")
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200


def test_tunnel_discovery_registers_discovered_url(quiet_github):
    update = AsyncMock()
    quiet_github.setattr("themis.app.update_webhook_url", update)
    quiet_github.setattr(
        "themis.app._discover_tunnel_url", AsyncMock(return_value="https://abc.ngrok.app")
    )
    settings = make_settings(tunnel_api="http://ngrok:4040")
    with TestClient(create_app(settings)):
        pass
    assert update.await_args.args[1] == "https://abc.ngrok.app/webhook"


def test_public_url_wins_over_tunnel(quiet_github):
    update = AsyncMock()
    discover = AsyncMock(return_value="https://abc.ngrok.app")
    quiet_github.setattr("themis.app.update_webhook_url", update)
    quiet_github.setattr("themis.app._discover_tunnel_url", discover)
    settings = make_settings(
        public_url="https://reviews.example.com", tunnel_api="http://ngrok:4040"
    )
    with TestClient(create_app(settings)):
        pass
    discover.assert_not_awaited()
    assert update.await_args.args[1] == "https://reviews.example.com/webhook"


def test_started_log_includes_slug_and_flags(quiet_github, caplog):
    with caplog.at_level(logging.INFO, logger="themis.app"):
        with TestClient(create_app(make_settings())):
            pass
    assert "themis_started slug=test-reviewer mention=@test-reviewer" in caplog.text
    assert "webhook_enabled=True api_enabled=False" in caplog.text


# --- _discover_tunnel_url ----------------------------------------------------


async def test_discover_tunnel_url_returns_first_https_tunnel(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tunnels": [
            {"public_url": "tcp://0.tcp.ngrok.io:12345"},
            {"public_url": "https://abc.ngrok.app/"},
        ]})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))
    url = await _discover_tunnel_url("http://ngrok:4040", attempts=1)
    assert url == "https://abc.ngrok.app"


async def test_discover_tunnel_url_exhausts_without_https_tunnel(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tunnels": [
            {"public_url": "tcp://0.tcp.ngrok.io:12345"},
        ]})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))
    monkeypatch.setattr("themis.app.asyncio.sleep", AsyncMock())
    url = await _discover_tunnel_url("http://ngrok:4040", attempts=2)
    assert url is None


async def test_discover_tunnel_url_swallows_http_errors_until_exhausted(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))
    monkeypatch.setattr("themis.app.asyncio.sleep", AsyncMock())
    url = await _discover_tunnel_url("http://ngrok:4040", attempts=2)
    assert url is None


async def test_register_webhook_logs_and_skips_when_discovery_fails(monkeypatch, caplog):
    update = AsyncMock()
    monkeypatch.setattr("themis.app.update_webhook_url", update)
    monkeypatch.setattr("themis.app._discover_tunnel_url", AsyncMock(return_value=None))
    settings = make_settings(tunnel_api="http://ngrok:4040")
    with caplog.at_level(logging.WARNING, logger="themis.app"):
        await _register_webhook(settings, "test-jwt")
    update.assert_not_awaited()
    assert "themis_tunnel_discovery_failed api=http://ngrok:4040" in caplog.text


# --- create_app contract -----------------------------------------------------


def test_create_app_missing_settings_raises(monkeypatch):
    for key in THEMIS_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SettingsError):
        create_app()


def test_create_app_with_settings_skips_load_settings(quiet_github):
    def _boom():
        raise AssertionError("load_settings should not be called")

    quiet_github.setattr("themis.app.load_settings", _boom)
    with TestClient(create_app(make_settings())) as client:
        assert client.get("/healthz").status_code == 200


def test_slug_resolution_failure_fails_startup(monkeypatch):
    monkeypatch.setattr("themis.app.make_app_jwt", lambda *a: "jwt")
    request = httpx.Request("GET", "https://api.github.com/app")
    response = httpx.Response(401, request=request)
    error = httpx.HTTPStatusError("unauthorized", request=request, response=response)
    monkeypatch.setattr("themis.app.get_app_slug", AsyncMock(side_effect=error))
    with pytest.raises(httpx.HTTPStatusError):
        with TestClient(create_app(make_settings())):
            pass
