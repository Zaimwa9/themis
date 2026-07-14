"""Themis configuration: env for identity/infrastructure, .themis/ for behavior."""

import base64
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError, field_validator

from themis.engines import ENGINE_NAMES

logger = logging.getLogger(__name__)

# Codex --sandbox modes. workspace-write needs kernel namespace support
# (Landlock/bubblewrap); container runtimes without it need danger-full-access,
# where the container itself is the isolation boundary.
VALID_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")

REPO_CONFIG_PATH = ".themis/config.yaml"


class SettingsError(Exception):
    """Missing or invalid instance configuration; fail fast at startup."""


# --- per-repo behavior (.themis/config.yaml in the target repo) -------------


class ModelConfig(BaseModel):
    name: str | None = None   # None = engine default, resolved in the service
    reasoning_effort: str = "high"


class LimitsConfig(BaseModel):
    timeout_seconds: int = 1200
    max_attempts: int = 2
    clone_depth: int = 50


class TriggersConfig(BaseModel):
    auto_review: bool = True


class LearningsConfig(BaseModel):
    enabled: bool = True
    digest_threshold: int = 10

    @field_validator("digest_threshold", mode="before")
    @classmethod
    def _threshold_at_least_one(cls, value: object) -> object:
        """A nonsense threshold must not void the rest of the repo config."""
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            return value
        logger.warning("themis_invalid_digest_threshold value=%s", str(value)[:50])
        return 10


class RepoConfig(BaseModel):
    engine: str | None = None
    web_access: bool = False
    model: ModelConfig = ModelConfig()
    limits: LimitsConfig = LimitsConfig()
    triggers: TriggersConfig = TriggersConfig()
    learnings: LearningsConfig = LearningsConfig()

    @field_validator("engine", mode="before")
    @classmethod
    def _engine_or_instance_default(cls, value: object) -> object:
        """A typo'd engine must not reject the rest of the repo's config;
        fall back to the instance default (None) with a warning."""
        if value is None or value in ENGINE_NAMES:
            return value
        logger.warning("themis_invalid_repo_engine value=%s", str(value)[:50])
        return None


def parse_repo_config(text: str | None) -> RepoConfig:
    """RepoConfig from .themis/config.yaml text; full defaults when the file
    is absent, empty, or malformed. A broken yaml in a target repo must never
    kill reviews. Partial files deep-merge per key (pydantic nested defaults).
    """
    if text is None:
        return RepoConfig()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        logger.warning("themis_repo_config_invalid error=%s", str(error)[:200])
        return RepoConfig()
    if data is None:
        return RepoConfig()
    if not isinstance(data, dict):
        logger.warning("themis_repo_config_invalid error=not a mapping")
        return RepoConfig()
    try:
        return RepoConfig(**data)
    except (ValidationError, TypeError) as error:
        logger.warning("themis_repo_config_invalid error=%s", str(error)[:200])
        return RepoConfig()


# --- instance settings (env) -------------------------------------------------


@dataclass(frozen=True)
class Settings:
    gh_app_client_id: str
    gh_app_private_key_pem: str = field(repr=False)
    gh_webhook_secret: str | None = field(repr=False)
    webhook_enabled: bool
    api_token: str | None = field(repr=False)
    codex_sandbox: str
    engine: str
    workspace_root: Path
    public_url: str | None
    tunnel_api: str | None
    agent_url: str
    agent_token: str = field(repr=False)
    data_root: Path = field(default_factory=lambda: Path.home() / ".themis")

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _decode_private_key(raw: str) -> str:
    if raw.lstrip().startswith("-----BEGIN"):
        return raw
    try:
        return base64.b64decode(raw, validate=True).decode()
    except (ValueError, UnicodeDecodeError) as error:
        raise SettingsError(
            "THEMIS_GH_APP_PRIVATE_KEY is neither PEM nor valid base64"
        ) from error


def load_settings() -> Settings:
    missing = [
        name
        for name in (
            "THEMIS_GH_APP_CLIENT_ID", "THEMIS_GH_APP_PRIVATE_KEY", "THEMIS_AGENT_TOKEN"
        )
        if not os.getenv(name)
    ]
    if missing:
        raise SettingsError(f"missing required environment variables: {', '.join(missing)}")

    webhook_enabled = _env_bool("THEMIS_WEBHOOK_ENABLED", True)
    webhook_secret = os.getenv("THEMIS_GH_WEBHOOK_SECRET") or None
    api_token = os.getenv("THEMIS_API_TOKEN") or None
    if webhook_enabled and not webhook_secret:
        raise SettingsError(
            "THEMIS_GH_WEBHOOK_SECRET is required while the webhook is enabled "
            "(set THEMIS_WEBHOOK_ENABLED=false for headless mode)"
        )
    if not webhook_enabled and not api_token:
        raise SettingsError(
            "no entrypoint configured: webhook disabled and THEMIS_API_TOKEN unset"
        )

    sandbox = os.getenv("THEMIS_CODEX_SANDBOX") or "workspace-write"
    if sandbox not in VALID_SANDBOXES:
        raise SettingsError(
            f"invalid codex sandbox {sandbox!r}; expected one of {VALID_SANDBOXES}"
        )

    engine = os.getenv("THEMIS_ENGINE") or "codex"
    if engine not in ENGINE_NAMES:
        raise SettingsError(
            f"invalid engine {engine!r}; expected one of {ENGINE_NAMES}"
        )

    public_url = (os.getenv("THEMIS_PUBLIC_URL") or "").rstrip("/") or None

    return Settings(
        gh_app_client_id=os.environ["THEMIS_GH_APP_CLIENT_ID"],
        gh_app_private_key_pem=_decode_private_key(os.environ["THEMIS_GH_APP_PRIVATE_KEY"]),
        gh_webhook_secret=webhook_secret,
        webhook_enabled=webhook_enabled,
        api_token=api_token,
        codex_sandbox=sandbox,
        engine=engine,
        workspace_root=Path(os.getenv("THEMIS_WORKSPACE_ROOT") or "/tmp/themis"),
        public_url=public_url,
        tunnel_api=os.getenv("THEMIS_TUNNEL_API") or None,
        agent_url=os.getenv("THEMIS_AGENT_URL") or "http://agent:8001",
        agent_token=os.environ["THEMIS_AGENT_TOKEN"],
        data_root=Path(os.getenv("THEMIS_DATA_ROOT") or "~/.themis").expanduser(),
    )
