import hashlib
import hmac
import json
import logging

from themis.security import redact_outbound, verify_signature

SECRET = "s3cret"
BODY = b'{"action": "opened"}'


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature__valid__returns_true():
    assert verify_signature(BODY, SECRET, _sign(BODY, SECRET)) is True


def test_verify_signature__wrong_secret__returns_false():
    assert verify_signature(BODY, SECRET, _sign(BODY, "other")) is False


def test_verify_signature__missing_header__returns_false():
    assert verify_signature(BODY, SECRET, None) is False


def test_verify_signature__malformed_header__returns_false():
    assert verify_signature(BODY, SECRET, "sha1=deadbeef") is False


def test_verify_signature__non_ascii_header__returns_false():
    assert verify_signature(BODY, SECRET, "sha256=\xe9\xe9") is False


def test_verify_signature__empty_secret__returns_false():
    assert verify_signature(BODY, "", _sign(BODY, "")) is False


def test_verify_signature__tampered_body__returns_false():
    assert verify_signature(b'{"action": "closed"}', SECRET, _sign(BODY, SECRET)) is False


def test_verify_signature__empty_header__returns_false():
    assert verify_signature(BODY, SECRET, "") is False


# --- outbound redaction ---------------------------------------------------------


def test_redact__env_secret_value(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-verysecretvalue")

    out = redact_outbound("leaked: sk-ant-oat01-verysecretvalue done")

    assert "verysecretvalue" not in out
    assert "[redacted]" in out


def test_redact__webhook_secret_and_api_token(monkeypatch):
    monkeypatch.setenv("THEMIS_GH_WEBHOOK_SECRET", "hook-secret-value")
    monkeypatch.setenv("THEMIS_API_TOKEN", "api-token-value")

    out = redact_outbound("a hook-secret-value b api-token-value c")

    assert "hook-secret-value" not in out
    assert "api-token-value" not in out


def test_redact__private_key_base64_form(monkeypatch):
    import base64
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIfake\n-----END RSA PRIVATE KEY-----"
    monkeypatch.setenv("THEMIS_GH_APP_PRIVATE_KEY", pem)

    encoded = base64.b64encode(pem.encode()).decode()
    assert "[redacted]" in redact_outbound(f"raw {pem} end")
    assert "[redacted]" in redact_outbound(f"b64 {encoded} end")


def test_redact__short_env_value_not_redacted(monkeypatch):
    # A placeholder like "x" must never redact half a comment.
    monkeypatch.setenv("THEMIS_API_TOKEN", "x")

    assert redact_outbound("x marks the spot") == "x marks the spot"


def test_redact__credential_patterns():
    body = (
        "a sk-ant-oat01-abcdefgh12 "
        "b gho_0123456789abcdef0123 "
        "c ghs_0123456789abcdef0123 "
        "d github_pat_0123456789abcdef01_more "
        "e eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjMifQ.sflKxwRJSMeKKF2QT4"
    )

    out = redact_outbound(body)

    for leaked in ("sk-ant-", "gho_", "ghs_", "github_pat_", "eyJ"):
        assert leaked not in out
    assert out.count("[redacted]") == 5


def test_redact__codex_auth_file_values_and_base64(tmp_path, monkeypatch):
    import base64

    home = tmp_path / "codex"
    home.mkdir()
    access_token = "opaque-access-token-with-unknown-shape"
    refresh_token = "opaque-refresh-token-with-unknown-shape"
    (home / "auth.json").write_text(json.dumps({
        "tokens": {"access_token": access_token, "refresh_token": refresh_token},
    }))
    monkeypatch.setenv("CODEX_HOME", str(home))

    encoded = base64.b64encode(refresh_token.encode()).decode()
    out = redact_outbound(f"raw {access_token}; encoded {encoded}")

    assert access_token not in out
    assert encoded not in out
    assert out.count("[redacted]") == 2


def test_redact__clean_text_untouched_no_log(caplog):
    body = "This PR looks good. The rate limit handling in api.py is correct."
    with caplog.at_level(logging.WARNING):
        assert redact_outbound(body) == body
    assert "themis_outbound_redacted" not in caplog.text


def test_redact__logs_count_not_value(monkeypatch, caplog):
    monkeypatch.setenv("THEMIS_API_TOKEN", "api-token-value")
    with caplog.at_level(logging.WARNING):
        redact_outbound("leak api-token-value")
    assert "themis_outbound_redacted" in caplog.text
    assert "api-token-value" not in caplog.text
