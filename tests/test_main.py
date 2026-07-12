"""`python -m themis` entry point."""

import pytest

from themis import __main__ as main_module
from themis.config import SettingsError

REQUIRED_ENV = {
    "THEMIS_GH_APP_CLIENT_ID": "Iv1.abc",
    "THEMIS_GH_APP_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
    "THEMIS_GH_WEBHOOK_SECRET": "hush",
}

THEMIS_ENV_KEYS = (
    "THEMIS_GH_APP_CLIENT_ID", "THEMIS_GH_APP_PRIVATE_KEY", "THEMIS_GH_WEBHOOK_SECRET",
    "THEMIS_CODEX_SANDBOX", "THEMIS_PUBLIC_URL", "THEMIS_TUNNEL_API",
    "THEMIS_WEBHOOK_ENABLED", "THEMIS_API_TOKEN", "THEMIS_WORKSPACE_ROOT", "PORT",
)


def _clear_env(monkeypatch):
    for key in THEMIS_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _set_required_env(monkeypatch):
    _clear_env(monkeypatch)
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


def test_main_runs_uvicorn_with_port_from_env(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("PORT", "1234")
    captured = {}

    def fake_run(app, **kwargs):
        captured["kwargs"] = kwargs

    monkeypatch.setattr(main_module.uvicorn, "run", fake_run)
    main_module.main()

    assert captured["kwargs"]["host"] == "0.0.0.0"
    assert captured["kwargs"]["port"] == 1234


def test_main_defaults_to_port_8000(monkeypatch):
    _set_required_env(monkeypatch)
    captured = {}

    def fake_run(app, **kwargs):
        captured["kwargs"] = kwargs

    monkeypatch.setattr(main_module.uvicorn, "run", fake_run)
    main_module.main()

    assert captured["kwargs"]["port"] == 8000


def test_main_fails_fast_when_settings_missing(monkeypatch):
    _clear_env(monkeypatch)

    def fail_if_called(app, **kwargs):
        raise AssertionError("uvicorn.run should not be called")

    monkeypatch.setattr(main_module.uvicorn, "run", fail_if_called)

    with pytest.raises(SettingsError):
        main_module.main()
