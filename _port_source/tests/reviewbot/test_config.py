import pytest

from reviewbot.config import load_config, load_credentials

PEM = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n"


def _set_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEWBOT_GH_APP_CLIENT_ID", "Iv1.abc123")
    monkeypatch.setenv("REVIEWBOT_GH_APP_PRIVATE_KEY", PEM)
    monkeypatch.setenv("REVIEWBOT_GH_WEBHOOK_SECRET", "s3cret")


def test_load_config__committed_yaml__parses(monkeypatch):
    monkeypatch.delenv("REVIEWBOT_CONFIG", raising=False)

    config = load_config()

    assert config.repo == "Zaimwa9/bookia-v2"
    assert config.model.name == "gpt-5.4"
    assert config.model.reasoning_effort == "high"
    assert config.limits.timeout_seconds == 1200
    assert config.limits.max_attempts == 2


def test_load_config__bot_login__derived_from_mention(monkeypatch):
    monkeypatch.delenv("REVIEWBOT_CONFIG", raising=False)

    config = load_config()

    assert config.bot.mention == "@bookia-reviewer"
    assert config.bot_login == "bookia-reviewer[bot]"


def test_load_config__custom_file__overrides_and_keeps_defaults(tmp_path):
    path = tmp_path / "rb.yaml"
    path.write_text('repo: "a/b"\nlimits:\n  max_attempts: 3\n')

    config = load_config(path)

    assert config.repo == "a/b"
    assert config.limits.max_attempts == 3
    assert config.limits.timeout_seconds == 1200


def test_load_config__sandbox__defaults_to_workspace_write(monkeypatch):
    monkeypatch.delenv("REVIEWBOT_CONFIG", raising=False)
    monkeypatch.delenv("REVIEWBOT_CODEX_SANDBOX", raising=False)

    config = load_config()

    assert config.model.sandbox == "workspace-write"


def test_load_config__sandbox_env_override__wins(monkeypatch):
    monkeypatch.delenv("REVIEWBOT_CONFIG", raising=False)
    monkeypatch.setenv("REVIEWBOT_CODEX_SANDBOX", "danger-full-access")

    config = load_config()

    assert config.model.sandbox == "danger-full-access"


def test_load_config__sandbox_invalid_value__raises(monkeypatch):
    monkeypatch.delenv("REVIEWBOT_CONFIG", raising=False)
    monkeypatch.setenv("REVIEWBOT_CODEX_SANDBOX", "yolo")

    with pytest.raises(ValueError, match="sandbox"):
        load_config()


def test_load_config__empty_file__raises_value_error(tmp_path):
    path = tmp_path / "rb.yaml"
    path.write_text("")

    with pytest.raises(ValueError, match="empty or not a mapping"):
        load_config(path)


def test_load_credentials__missing_env__returns_none(monkeypatch):
    monkeypatch.delenv("REVIEWBOT_GH_APP_CLIENT_ID", raising=False)
    monkeypatch.delenv("REVIEWBOT_GH_APP_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("REVIEWBOT_GH_WEBHOOK_SECRET", raising=False)

    assert load_credentials() is None


def test_load_credentials__pem_key__returned_verbatim(monkeypatch):
    _set_credentials(monkeypatch)

    credentials = load_credentials()

    assert credentials is not None
    assert credentials.private_key_pem == PEM
    assert credentials.client_id == "Iv1.abc123"
    assert credentials.webhook_secret == "s3cret"


def test_load_credentials__base64_key__decoded(monkeypatch):
    import base64

    _set_credentials(monkeypatch)
    monkeypatch.setenv("REVIEWBOT_GH_APP_PRIVATE_KEY", base64.b64encode(PEM.encode()).decode())

    credentials = load_credentials()

    assert credentials is not None
    assert credentials.private_key_pem == PEM


def test_load_credentials__invalid_base64__raises_value_error(monkeypatch):
    _set_credentials(monkeypatch)
    monkeypatch.setenv("REVIEWBOT_GH_APP_PRIVATE_KEY", "not-pem-and-not-base64!!")

    with pytest.raises(ValueError, match="neither PEM nor valid base64"):
        load_credentials()


def test_credentials__repr__hides_secrets(monkeypatch):
    _set_credentials(monkeypatch)

    credentials = load_credentials()

    assert credentials is not None
    assert PEM not in repr(credentials)
    assert "s3cret" not in repr(credentials)
