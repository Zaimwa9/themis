import hashlib
import hmac

from reviewbot.security import verify_signature

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
