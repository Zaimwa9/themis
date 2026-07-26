"""GitHub webhook HMAC verification, outbound secret redaction, and
neutralisation of themis control markers in agent-authored text."""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

REDACTED = "[redacted]"

# Env vars whose values must never appear in anything posted to GitHub. A
# hostile PR can instruct the agent to echo secrets it legitimately holds.
_SECRET_ENV_VARS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "GLM_API_KEY",
    "KIMI_API_KEY",
    "OPENROUTER_API_KEY",
    "THEMIS_GH_WEBHOOK_SECRET",
    "THEMIS_API_TOKEN",
    "THEMIS_GH_APP_PRIVATE_KEY",
    "THEMIS_AGENT_TOKEN",
)

# Credential shapes that never occur in legitimate review prose.
_TOKEN_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"gh[opsu]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{16,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
)

_MIN_SECRET_LEN = 8  # a short placeholder value must never redact real prose

# Themis reads its own HTML-comment markers back off GitHub to make control
# decisions (which commit a delta review starts from, whether a title-skip
# notice was already posted). Those decisions are only sound while the
# controller is the sole author of marker text: an agent body is written
# from a hostile PR's content and can be induced to reproduce any literal
# string. Deliberately permissive about spacing and case - the readers match
# exact strings, so over-defanging agent prose is the safe direction.
_CONTROL_MARKER_RE = re.compile(r"<!--(\s*)themis:", re.IGNORECASE)
_DEFANGED_MARKER = r"<!--\1themis-quoted:"


def verify_signature(payload: bytes, secret: str, signature_header: str | None) -> bool:
    if not secret:
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature_header.encode())


def _secret_values() -> list[str]:
    raw_values = []
    for var in _SECRET_ENV_VARS:
        value = os.environ.get(var) or ""
        if len(value) < _MIN_SECRET_LEN:
            continue
        raw_values.append(value)

    # Codex authenticates from a file rather than an env var. Treat every
    # sufficiently long string in this dedicated auth document as sensitive;
    # its exact schema and token shapes can change across CLI versions.
    codex_home = Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
    try:
        auth = json.loads((codex_home / "auth.json").read_text())
    except (OSError, json.JSONDecodeError):
        auth = None

    def collect_strings(value: object) -> None:
        if isinstance(value, str) and len(value) >= _MIN_SECRET_LEN:
            raw_values.append(value)
        elif isinstance(value, dict):
            for child in value.values():
                collect_strings(child)
        elif isinstance(value, list):
            for child in value:
                collect_strings(child)

    collect_strings(auth)

    values = []
    for value in dict.fromkeys(raw_values):
        values.append(value)
        # Secrets may circulate in their single-line base64 form too.
        values.append(base64.b64encode(value.encode()).decode())
    return values


def redact_outbound(text: str) -> str:
    """Scrub known secret values and credential-shaped strings from any body
    that leaves the instance (PR comments, findings, replies, log tails)."""
    count = 0
    for value in _secret_values():
        if value in text:
            text = text.replace(value, REDACTED)
            count += 1
    for pattern in _TOKEN_PATTERNS:
        text, replaced = pattern.subn(REDACTED, text)
        count += replaced
    if count:
        logger.warning("themis_outbound_redacted count=%d", count)
    return text


def strip_control_markers(text: str) -> str:
    """Defang themis control markers so agent text cannot impersonate the
    controller. The marker stays readable (`<!-- themis-quoted:...`) - an
    agent quoting themis's own source in a finding should still show what it
    quoted; it just no longer parses as a marker on the way back in."""
    text, count = _CONTROL_MARKER_RE.subn(_DEFANGED_MARKER, text)
    if count:
        logger.warning("themis_agent_marker_defanged count=%d", count)
    return text


def sanitize_agent_text(text: str) -> str:
    """The full outbound filter for anything an engine wrote: secrets out,
    control markers defanged. Controller-authored bodies use
    `redact_outbound` alone - they are allowed to carry real markers."""
    return strip_control_markers(redact_outbound(text))
