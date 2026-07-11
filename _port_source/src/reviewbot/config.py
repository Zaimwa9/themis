"""Reviewbot configuration: committed yaml for behavior, env for credentials."""

import base64
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel

# config.py lives at backend/src/reviewbot/, yaml at backend/reviewbot.yaml
_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "reviewbot.yaml"


class BotConfig(BaseModel):
    mention: str = "@bookia-reviewer"


# Codex --sandbox modes. workspace-write needs kernel namespace support
# (bubblewrap); container runtimes without it need danger-full-access, where
# the container itself is the isolation boundary.
VALID_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")


class ModelConfig(BaseModel):
    name: str = "gpt-5.4"
    reasoning_effort: str = "high"
    sandbox: str = "workspace-write"


class LimitsConfig(BaseModel):
    timeout_seconds: int = 1200
    max_attempts: int = 2
    clone_depth: int = 50


class ReviewBotConfig(BaseModel):
    repo: str
    bot: BotConfig = BotConfig()
    model: ModelConfig = ModelConfig()
    limits: LimitsConfig = LimitsConfig()
    workspace_root: Path = Path("/tmp/reviewbot")

    @property
    def bot_login(self) -> str:
        return self.bot.mention.lstrip("@") + "[bot]"


def load_config(path: Path | None = None) -> ReviewBotConfig:
    config_path = path or Path(os.getenv("REVIEWBOT_CONFIG", str(_DEFAULT_CONFIG_PATH)))
    data = yaml.safe_load(config_path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{config_path} is empty or not a mapping")
    config = ReviewBotConfig(**data)
    sandbox_override = os.getenv("REVIEWBOT_CODEX_SANDBOX")
    if sandbox_override:
        config.model.sandbox = sandbox_override
    if config.model.sandbox not in VALID_SANDBOXES:
        raise ValueError(
            f"invalid codex sandbox {config.model.sandbox!r}; expected one of {VALID_SANDBOXES}"
        )
    return config


@lru_cache(maxsize=1)
def get_config() -> ReviewBotConfig:
    return load_config()


@dataclass(frozen=True)
class Credentials:
    client_id: str
    private_key_pem: str = field(repr=False)
    webhook_secret: str = field(repr=False)


def load_credentials() -> Credentials | None:
    client_id = os.getenv("REVIEWBOT_GH_APP_CLIENT_ID", "")
    private_key = os.getenv("REVIEWBOT_GH_APP_PRIVATE_KEY", "")
    webhook_secret = os.getenv("REVIEWBOT_GH_WEBHOOK_SECRET", "")
    if not (client_id and private_key and webhook_secret):
        return None
    if not private_key.lstrip().startswith("-----BEGIN"):
        try:
            private_key = base64.b64decode(private_key, validate=True).decode()
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(
                "REVIEWBOT_GH_APP_PRIVATE_KEY is neither PEM nor valid base64"
            ) from exc
    return Credentials(client_id, private_key, webhook_secret)
