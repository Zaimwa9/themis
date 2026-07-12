"""Instance Settings (env) and per-repo RepoConfig parsing."""

import base64
import logging

import pytest

from themis.config import (
    RepoConfig,
    SettingsError,
    load_settings,
    parse_repo_config,
)

REQUIRED = {
    "THEMIS_GH_APP_CLIENT_ID": "Iv1.abc",
    "THEMIS_GH_APP_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
    "THEMIS_GH_WEBHOOK_SECRET": "hush",
}


def _set_env(monkeypatch, extra=None, omit=()):
    for key in (
        "THEMIS_GH_APP_CLIENT_ID", "THEMIS_GH_APP_PRIVATE_KEY", "THEMIS_GH_WEBHOOK_SECRET",
        "THEMIS_CODEX_SANDBOX", "THEMIS_PUBLIC_URL", "THEMIS_TUNNEL_API",
        "THEMIS_WEBHOOK_ENABLED", "THEMIS_API_TOKEN", "THEMIS_WORKSPACE_ROOT", "THEMIS_ENGINE",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**REQUIRED, **(extra or {})}.items():
        if key not in omit:
            monkeypatch.setenv(key, value)


def test_load_settings_happy_path(monkeypatch):
    _set_env(monkeypatch)
    settings = load_settings()
    assert settings.gh_app_client_id == "Iv1.abc"
    assert settings.gh_app_private_key_pem.startswith("-----BEGIN")
    assert settings.webhook_enabled is True
    assert settings.api_token is None
    assert settings.codex_sandbox == "workspace-write"
    assert str(settings.workspace_root) == "/tmp/themis"


def test_load_settings_missing_required_names_them(monkeypatch):
    _set_env(monkeypatch, omit=("THEMIS_GH_APP_CLIENT_ID",))
    with pytest.raises(SettingsError, match="THEMIS_GH_APP_CLIENT_ID"):
        load_settings()


def test_load_settings_base64_key_decoded(monkeypatch):
    pem = "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----"
    _set_env(monkeypatch, extra={
        "THEMIS_GH_APP_PRIVATE_KEY": base64.b64encode(pem.encode()).decode(),
    })
    assert load_settings().gh_app_private_key_pem == pem


def test_load_settings_garbage_key_rejected(monkeypatch):
    _set_env(monkeypatch, extra={"THEMIS_GH_APP_PRIVATE_KEY": "not pem not base64 !!!"})
    with pytest.raises(SettingsError, match="PEM"):
        load_settings()


def test_webhook_disabled_needs_no_secret_but_needs_api_token(monkeypatch):
    _set_env(monkeypatch, extra={
        "THEMIS_WEBHOOK_ENABLED": "false", "THEMIS_API_TOKEN": "tok",
    }, omit=("THEMIS_GH_WEBHOOK_SECRET",))
    settings = load_settings()
    assert settings.webhook_enabled is False
    assert settings.gh_webhook_secret is None
    assert settings.api_token == "tok"


def test_no_entrypoint_at_all_is_an_error(monkeypatch):
    _set_env(monkeypatch, extra={"THEMIS_WEBHOOK_ENABLED": "false"},
             omit=("THEMIS_GH_WEBHOOK_SECRET",))
    with pytest.raises(SettingsError, match="entrypoint"):
        load_settings()


def test_webhook_enabled_requires_secret(monkeypatch):
    _set_env(monkeypatch, omit=("THEMIS_GH_WEBHOOK_SECRET",))
    with pytest.raises(SettingsError, match="THEMIS_GH_WEBHOOK_SECRET"):
        load_settings()


def test_blank_workspace_root_env_falls_back_to_default(monkeypatch):
    _set_env(monkeypatch, extra={"THEMIS_WORKSPACE_ROOT": ""})
    assert str(load_settings().workspace_root) == "/tmp/themis"


def test_invalid_sandbox_rejected(monkeypatch):
    _set_env(monkeypatch, extra={"THEMIS_CODEX_SANDBOX": "yolo"})
    with pytest.raises(SettingsError, match="sandbox"):
        load_settings()


def test_public_url_trailing_slash_stripped(monkeypatch):
    _set_env(monkeypatch, extra={"THEMIS_PUBLIC_URL": "https://x.example.com/"})
    assert load_settings().public_url == "https://x.example.com"


# --- RepoConfig ------------------------------------------------------------

def test_repo_config_defaults():
    config = parse_repo_config(None)
    assert config.model.name is None
    assert config.model.reasoning_effort == "high"
    assert config.limits.timeout_seconds == 1200
    assert config.limits.max_attempts == 2
    assert config.limits.clone_depth == 50
    assert config.triggers.auto_review is True


def test_repo_config_partial_deep_merges():
    config = parse_repo_config("model:\n  name: gpt-6\nlimits:\n  clone_depth: 10\n")
    assert config.model.name == "gpt-6"
    assert config.model.reasoning_effort == "high"   # untouched default
    assert config.limits.clone_depth == 10
    assert config.limits.timeout_seconds == 1200     # untouched default


def test_repo_config_malformed_yaml_falls_back_to_defaults():
    assert parse_repo_config("model: [unclosed") == RepoConfig()


def test_repo_config_wrong_shape_falls_back_to_defaults():
    assert parse_repo_config("- just\n- a list\n") == RepoConfig()
    assert parse_repo_config("model:\n  name: [1, 2]\n") == RepoConfig()


def test_repo_config_empty_file_is_defaults():
    assert parse_repo_config("") == RepoConfig()


# --- engine settings ----------------------------------------------------------


def test_load_settings__engine_default_codex(monkeypatch):
    _set_env(monkeypatch)

    assert load_settings().engine == "codex"


def test_load_settings__engine_claude(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("THEMIS_ENGINE", "claude")

    assert load_settings().engine == "claude"


def test_load_settings__engine_unknown__raises(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("THEMIS_ENGINE", "gemini")

    with pytest.raises(SettingsError, match="invalid engine"):
        load_settings()


# --- repo engine + web_access -------------------------------------------------


def test_repo_config__engine_default_none():
    config = parse_repo_config("model:\n  reasoning_effort: low\n")
    assert config.engine is None
    assert config.web_access is False


def test_repo_config__engine_claude():
    assert parse_repo_config("engine: claude\n").engine == "claude"


def test_repo_config__engine_invalid__coerces_to_none_and_keeps_rest(caplog):
    text = "engine: caude\nlimits:\n  max_attempts: 5\n"
    with caplog.at_level(logging.WARNING):
        config = parse_repo_config(text)
    assert config.engine is None
    assert config.limits.max_attempts == 5  # rest of the config preserved
    assert "themis_invalid_repo_engine" in caplog.text


def test_repo_config__web_access_true():
    assert parse_repo_config("web_access: true\n").web_access is True


def test_repo_config__model_name_default_is_none():
    # Engine-aware defaults resolve in the service, not here.
    assert parse_repo_config(None).model.name is None
