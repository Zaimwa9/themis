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


MODULE_STATES = ("always", "auto", "off")


class ReviewModulesConfig(BaseModel):
    """Tri-state presence control per optional review section.

    `always` = include, `auto` = module-specific compatibility/adaptive mode,
    `off` = omit. Presentation categories treat `auto` as enabled: only `off`
    suppresses them. None = unset, resolved by resolve_modules(). Booleans are
    lenient aliases (true -> auto, false -> off); yaml 1.1 also reads a bare
    `off` as False, which lands on the same state."""

    scorecard: str | None = None
    walkthrough: str | None = None
    product_impact: str | None = None
    verification_steps: str | None = None
    assumptions: str | None = None
    sign_off: str | None = None
    ci_context: str | None = None
    inline_findings: str | None = None
    code_suggestions: str | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _tri_state_or_unset(cls, value: object) -> object:
        """An invalid state must not void the rest of the repo config."""
        if value is True:
            return "auto"
        if value is False:
            return "off"
        if value is None or value in MODULE_STATES:
            return value
        logger.warning("themis_invalid_review_module value=%s", str(value)[:50])
        return None


MODULE_NAMES = tuple(ReviewModulesConfig.model_fields)
PRESENTATION_MODULE_NAMES = (
    "scorecard",
    "walkthrough",
    "product_impact",
    "verification_steps",
    "assumptions",
    "sign_off",
)

# Complete built-in presentation profile. Repository config overlays this
# field by field: omitted or invalid values retain their defaults, while each
# explicit valid value wins independently of doctrine selection.
DEFAULT_REVIEW_MODULES = {
    **dict.fromkeys(MODULE_NAMES, "auto"),
    **dict.fromkeys(PRESENTATION_MODULE_NAMES, "always"),
}


class ReviewConfig(BaseModel):
    modules: ReviewModulesConfig = ReviewModulesConfig()

    @field_validator("modules", mode="before")
    @classmethod
    def _modules_mapping_or_default(cls, value: object) -> object:
        """A malformed modules container must not void the rest of the repo
        config (engine, limits, ...); presentation settings degrade alone."""
        if value is None:
            return ReviewModulesConfig()  # yaml null: `modules:` with no keys
        if isinstance(value, dict | ReviewModulesConfig):
            return value
        logger.warning("themis_invalid_review_modules value=%s", str(value)[:50])
        return ReviewModulesConfig()


def resolve_modules(config: "RepoConfig") -> dict[str, str]:
    """Overlay valid fields; only `off` suppresses presentation categories."""
    configured = config.review.modules.model_dump(exclude_none=True)
    for name in PRESENTATION_MODULE_NAMES:
        if configured.get(name) == "auto":
            configured[name] = "always"
    return {**DEFAULT_REVIEW_MODULES, **configured}


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


class AgentConfig(BaseModel):
    """Opt-in trusted native context for the review agent (issue #9).

    Both default off: the safe baseline is an agent that loads nothing from
    the repository. Opting in loads instruction files (context) and skill
    packages (skills) from the PR *base* revision only, never the PR head."""

    context: bool = False
    skills: bool = False

    @field_validator("*", mode="before")
    @classmethod
    def _bool_or_default(cls, value: object) -> object:
        """An invalid capability value must not void the rest of the config,
        and must fail toward the safe default (off)."""
        if isinstance(value, bool):
            return value
        logger.warning("themis_invalid_agent_capability value=%s", str(value)[:50])
        return False


class RepoConfig(BaseModel):
    engine: str | None = None
    web_access: bool = False
    model: ModelConfig = ModelConfig()
    limits: LimitsConfig = LimitsConfig()
    triggers: TriggersConfig = TriggersConfig()
    learnings: LearningsConfig = LearningsConfig()
    review: ReviewConfig = ReviewConfig()
    agent: AgentConfig = AgentConfig()

    @field_validator("engine", mode="before")
    @classmethod
    def _engine_or_instance_default(cls, value: object) -> object:
        """A typo'd engine must not reject the rest of the repo's config;
        fall back to the instance default (None) with a warning."""
        if value is None or value in ENGINE_NAMES:
            return value
        logger.warning("themis_invalid_repo_engine value=%s", str(value)[:50])
        return None

    @field_validator("review", mode="before")
    @classmethod
    def _review_mapping_or_default(cls, value: object) -> object:
        """Same isolation as the modules container: a malformed review
        section degrades to defaults without taking engine/limits with it."""
        if value is None:
            return ReviewConfig()  # yaml null: `review:` with no keys
        if isinstance(value, dict | ReviewConfig):
            return value
        logger.warning("themis_invalid_review_config value=%s", str(value)[:50])
        return ReviewConfig()

    @field_validator("agent", mode="before")
    @classmethod
    def _agent_mapping_or_default(cls, value: object) -> object:
        """Same isolation again; a malformed agent section degrades to the
        all-off defaults without taking engine/limits with it."""
        if value is None:
            return AgentConfig()  # yaml null: `agent:` with no keys
        if isinstance(value, dict | AgentConfig):
            return value
        logger.warning("themis_invalid_agent_config value=%s", str(value)[:50])
        return AgentConfig()


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
    default_repo_config: str | None = None  # fallback .themis/config.yaml text

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


def _decode_default_repo_config(raw: str) -> str:
    """THEMIS_DEFAULT_REPO_CONFIG accepts config yaml raw or base64-encoded
    (compose-friendly). Real yaml mappings contain ':' or newlines, which
    base64's strict alphabet rejects, so a successful decode is unambiguous.
    Unlike per-repo files this is trusted operator input: fail fast on a
    value that doesn't parse, or the operator's intent is silently lost.
    GNU base64 wraps output at 76 chars, so whitespace is stripped before
    the strict decode rather than letting the wrap fail it."""
    try:
        text = base64.b64decode("".join(raw.split()), validate=True).decode()
    except (ValueError, UnicodeDecodeError):
        text = raw
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise SettingsError(
            f"THEMIS_DEFAULT_REPO_CONFIG is not valid YAML: {error}"
        ) from error
    if not isinstance(data, dict):
        raise SettingsError("THEMIS_DEFAULT_REPO_CONFIG must be a YAML mapping")
    return text


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
        default_repo_config=(
            _decode_default_repo_config(raw_default)
            if (raw_default := os.getenv("THEMIS_DEFAULT_REPO_CONFIG") or None)
            else None
        ),
    )
