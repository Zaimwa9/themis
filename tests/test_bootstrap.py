"""GitHub App manifest bootstrap."""

import base64
import json
import stat
import threading
from pathlib import Path

import httpx
import pytest
import yaml

from themis import bootstrap
from themis.bootstrap import (
    BootstrapError,
    BootstrapOptions,
    BootstrapSession,
    build_manifest,
    convert_manifest,
    manifest_registration_url,
    run_bootstrap,
    verify_repo_installation,
    write_deployment,
)


def options(tmp_path: Path, **overrides) -> BootstrapOptions:
    values = {
        "repo": "acme/widgets",
        "output": tmp_path,
        "organization": "acme",
        "public_url": "https://themis.example.com",
        "tunnel": False,
        "ngrok_authtoken": None,
        "engine": "codex",
        "callback_url": "http://127.0.0.1:8976",
        "bind_host": "127.0.0.1",
        "bind_port": 8976,
        "codex_auth": None,
        "image": "ghcr.io/example/themis:1.2.3",
        "timeout": 1,
        "open_browser": False,
    }
    values.update(overrides)
    return BootstrapOptions(**values)


def credentials() -> dict[str, object]:
    return {
        "client_id": "Iv1.abc",
        "pem": "-----BEGIN PRIVATE KEY-----\nsecret-key\n-----END PRIVATE KEY-----\n",
        "webhook_secret": "hook-secret",
        "slug": "themis-acme-123",
    }


def test_build_manifest_contains_exact_permissions_events_and_callbacks(tmp_path):
    manifest = build_manifest(options(tmp_path), "csrf-state")

    assert manifest["redirect_url"] == "http://127.0.0.1:8976/manifest/callback"
    assert manifest["setup_url"] == "http://127.0.0.1:8976/install/callback"
    assert manifest["hook_attributes"] == {
        "url": "https://themis.example.com/webhook",
        "active": True,
    }
    assert manifest["default_permissions"] == {
        "checks": "read",
        "contents": "write",
        "pull_requests": "write",
        "issues": "write",
        "statuses": "read",
    }
    assert manifest["default_events"] == [
        "pull_request",
        "issue_comment",
        "pull_request_review_comment",
    ]
    assert manifest["public"] is False


def test_build_manifest_uses_placeholder_that_startup_replaces_for_tunnel(tmp_path):
    manifest = build_manifest(
        options(tmp_path, public_url=None, tunnel=True, ngrok_authtoken="ngrok-token"),
        "state",
    )
    assert manifest["hook_attributes"]["url"] == "https://example.com/webhook"


def test_manifest_registration_url_supports_personal_and_org_owners():
    assert manifest_registration_url(None) == "https://github.com/settings/apps/new"
    assert manifest_registration_url("acme inc") == (
        "https://github.com/organizations/acme%20inc/settings/apps/new"
    )
    assert manifest_registration_url(None, "csrf/state") == (
        "https://github.com/settings/apps/new?state=csrf%2Fstate"
    )


def test_convert_manifest_returns_credentials(monkeypatch):
    def post(url, **kwargs):
        assert url.endswith("/app-manifests/one%2Ftwo/conversions")
        assert kwargs["timeout"] == 30
        return httpx.Response(201, json=credentials(), request=httpx.Request("POST", url))

    monkeypatch.setattr(bootstrap.httpx, "post", post)
    assert convert_manifest("one/two") == credentials()


def test_convert_manifest_rejects_incomplete_response(monkeypatch):
    def post(url, **kwargs):
        return httpx.Response(
            201, json={"client_id": "Iv1.abc"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(bootstrap.httpx, "post", post)
    with pytest.raises(BootstrapError, match="pem, webhook_secret, slug"):
        convert_manifest("code")


def test_verify_repo_installation_checks_repo_and_callback_id(monkeypatch):
    monkeypatch.setattr(bootstrap, "make_app_jwt", lambda client_id, pem: "signed-jwt")

    def get(url, **kwargs):
        assert url.endswith("/repos/acme/widgets/installation")
        assert kwargs["headers"]["Authorization"] == "Bearer signed-jwt"
        return httpx.Response(200, json={"id": 42}, request=httpx.Request("GET", url))

    monkeypatch.setattr(bootstrap.httpx, "get", get)
    verify_repo_installation(credentials(), "acme/widgets", 42)

    with pytest.raises(BootstrapError, match="did not match"):
        verify_repo_installation(credentials(), "acme/widgets", 43)


def test_verify_repo_installation_explains_missing_repo(monkeypatch):
    monkeypatch.setattr(bootstrap, "make_app_jwt", lambda client_id, pem: "signed-jwt")

    def get(url, **kwargs):
        return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr(bootstrap.httpx, "get", get)
    with pytest.raises(BootstrapError, match="select that repository"):
        verify_repo_installation(credentials(), "acme/widgets", 42)


def test_write_deployment_keeps_secrets_out_of_compose_and_sets_modes(tmp_path):
    source_auth = tmp_path / "source-auth.json"
    source_auth.write_text('{"token":"model-secret"}')
    output = tmp_path / "deployment"

    write_deployment(options(output, codex_auth=source_auth), credentials())

    env_text = (output / ".env").read_text()
    compose_text = (output / "compose.yaml").read_text()
    assert "THEMIS_GH_APP_CLIENT_ID='Iv1.abc'" in env_text
    encoded_pem = base64.b64encode(str(credentials()["pem"]).encode()).decode()
    assert f"THEMIS_GH_APP_PRIVATE_KEY='{encoded_pem}'" in env_text
    assert "hook-secret" in env_text
    assert "GLM_API_KEY=''" in env_text
    assert "KIMI_API_KEY=''" in env_text
    assert "OPENROUTER_API_KEY=''" in env_text
    assert "secret-key" not in compose_text
    assert "hook-secret" not in compose_text
    assert "image: ${THEMIS_IMAGE:-ghcr.io/example/themis:1.2.3}" in compose_text
    compose = yaml.safe_load(compose_text)
    assert set(compose["services"]) == {"themis", "agent", "codex-init", "ngrok"}
    assert compose["services"]["themis"]["environment"] == {
        "THEMIS_GH_APP_CLIENT_ID": "${THEMIS_GH_APP_CLIENT_ID}",
        "THEMIS_GH_APP_PRIVATE_KEY": "${THEMIS_GH_APP_PRIVATE_KEY}",
        "THEMIS_GH_WEBHOOK_SECRET": "${THEMIS_GH_WEBHOOK_SECRET}",
        "THEMIS_AGENT_TOKEN": "${THEMIS_AGENT_TOKEN}",
        "THEMIS_AGENT_URL": "http://agent:8001",
        "THEMIS_ENGINE": "${THEMIS_ENGINE:-codex}",
        "THEMIS_DEFAULT_REPO_CONFIG": "${THEMIS_DEFAULT_REPO_CONFIG:-}",
        "THEMIS_PUBLIC_URL": "${THEMIS_PUBLIC_URL:-}",
        "THEMIS_TUNNEL_API": "${THEMIS_TUNNEL_API:-}",
        "THEMIS_WEBHOOK_ENABLED": "${THEMIS_WEBHOOK_ENABLED:-true}",
        "THEMIS_API_TOKEN": "${THEMIS_API_TOKEN:-}",
        "THEMIS_DATA_ROOT": "/data/themis",
    }
    # Pending learnings must survive container recreation (data lives under
    # THEMIS_DATA_ROOT), so the controller needs a named volume, like codex-home.
    assert "themis-data:/data/themis" in compose["services"]["themis"]["volumes"]
    assert "themis-data" in compose["volumes"]
    assert compose["services"]["agent"]["environment"] == {
        "THEMIS_AGENT_TOKEN": "${THEMIS_AGENT_TOKEN}",
        "THEMIS_WORKSPACE_ROOT": "/tmp/themis",
        "THEMIS_CODEX_SANDBOX": "${THEMIS_CODEX_SANDBOX:-workspace-write}",
        "CLAUDE_CODE_OAUTH_TOKEN": "${CLAUDE_CODE_OAUTH_TOKEN:-}",
        "GLM_API_KEY": "${GLM_API_KEY:-}",
        "KIMI_API_KEY": "${KIMI_API_KEY:-}",
        "OPENROUTER_API_KEY": "${OPENROUTER_API_KEY:-}",
        "HTTP_PROXY": "${HTTP_PROXY:-}",
        "HTTPS_PROXY": "${HTTPS_PROXY:-}",
    }
    assert compose["services"]["agent"]["volumes"][1] == "codex-home:/data/codex"
    assert compose["services"]["codex-init"]["user"] == "0:0"
    assert stat.S_IMODE((output / ".env").stat().st_mode) == 0o600
    assert stat.S_IMODE((output / "codex-seed" / "auth.json").stat().st_mode) == 0o600
    assert json.loads((output / "codex-seed" / "auth.json").read_text())["token"] == (
        "model-secret"
    )
    assert json.loads((output / "themis-info.json").read_text()) == {
        "github_app_slug": "themis-acme-123",
        "mention": "@themis-acme-123",
        "repository": "acme/widgets",
    }


def test_write_deployment_refuses_to_overwrite_existing_files(tmp_path):
    (tmp_path / ".env").write_text("user-owned")
    with pytest.raises(BootstrapError, match="refusing to overwrite"):
        write_deployment(options(tmp_path), credentials())
    assert (tmp_path / ".env").read_text() == "user-owned"


def test_bootstrap_http_flow_converts_writes_and_verifies(monkeypatch, tmp_path):
    converted = credentials()
    writes = []
    verifications = []
    monkeypatch.setattr(bootstrap, "convert_manifest", lambda code: converted)
    monkeypatch.setattr(
        bootstrap, "write_deployment", lambda opts, creds: writes.append((opts, creds))
    )
    monkeypatch.setattr(
        bootstrap,
        "verify_repo_installation",
        lambda creds, repo, installation_id: verifications.append(
            (creds, repo, installation_id)
        ),
    )

    session = BootstrapSession(options(tmp_path))
    server = bootstrap.ThreadingHTTPServer(("127.0.0.1", 0), session.handler())
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        start = httpx.get(f"{base}/")
        assert start.status_code == 200
        assert 'method="post"' in start.text
        assert "default_permissions" in start.text

        callback = httpx.get(
            f"{base}/manifest/callback", params={"code": "manifest-code", "state": session.state},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert callback.headers["location"].startswith(
            "https://github.com/apps/themis-acme-123/installations/new?state="
        )
        assert writes == [(session.options, converted)]

        repeated = httpx.get(
            f"{base}/manifest/callback", params={"code": "manifest-code", "state": session.state},
            follow_redirects=False,
        )
        assert repeated.status_code == 303
        assert writes == [(session.options, converted)]

        installed = httpx.get(
            f"{base}/install/callback",
            params={"installation_id": "42", "state": session.state},
        )
        assert installed.status_code == 200
        assert "@themis-acme-123" in installed.text
        assert "@themis-acme-123 review" in installed.text
        assert session.done.is_set()
        assert verifications == [(converted, "acme/widgets", 42)]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_bootstrap_http_flow_rejects_wrong_state(tmp_path):
    session = BootstrapSession(options(tmp_path))
    server = bootstrap.ThreadingHTTPServer(("127.0.0.1", 0), session.handler())
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        response = httpx.get(
            f"http://127.0.0.1:{server.server_port}/manifest/callback",
            params={"code": "code", "state": "attacker"},
        )
        assert response.status_code == 400
        assert "state did not match" in response.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"repo": "not-a-repo"}, "owner/name"),
        ({"public_url": None}, "--public-url or --tunnel"),
        ({"tunnel": True}, "choose either"),
        (
            {"public_url": None, "tunnel": True, "ngrok_authtoken": None},
            "NGROK_AUTHTOKEN",
        ),
        ({"image": "image:${INJECT}"}, "container image"),
        ({"public_url": "http://insecure.example.com"}, "https origin"),
        ({"bind_port": 0}, "port and timeout"),
    ],
)
def test_run_bootstrap_validates_before_listening(tmp_path, overrides, message):
    with pytest.raises(BootstrapError, match=message):
        run_bootstrap(options(tmp_path, **overrides))


def test_run_bootstrap_prints_bot_mention_and_info_path(monkeypatch, tmp_path, capsys):
    opts = options(tmp_path, bind_port=9999)
    fake_server = type(
        "FakeServer",
        (),
        {
            "serve_forever": lambda self: None,
            "shutdown": lambda self: None,
            "server_close": lambda self: None,
        },
    )()

    def session_factory(options):
        session = type("FakeSession", (), {})()
        session.done = type("Done", (), {"wait": lambda self, timeout: True})()
        session.error = None
        session.credentials = credentials()
        session.handler = lambda: object
        return session

    monkeypatch.setattr(bootstrap, "BootstrapSession", session_factory)
    monkeypatch.setattr(bootstrap, "ThreadingHTTPServer", lambda address, handler: fake_server)
    monkeypatch.setattr(bootstrap.threading, "Thread", lambda **kwargs: type(
        "Thread", (), {"start": lambda self: None, "join": lambda self, timeout: None}
    )())

    run_bootstrap(opts)

    output = capsys.readouterr().out
    assert "GitHub bot: @themis-acme-123" in output
    assert "@themis-acme-123 review" in output
    assert str(tmp_path / "themis-info.json") in output
