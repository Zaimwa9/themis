"""GitHub webhook HMAC-SHA256 signature verification."""

import hashlib
import hmac


def verify_signature(payload: bytes, secret: str, signature_header: str | None) -> bool:
    if not secret:
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature_header.encode())
