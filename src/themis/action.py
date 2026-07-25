"""GitHub Actions entrypoint: one event, one synchronous job, no server.

Zero-infrastructure alternative to the webhook service (issue #13): the
workflow's GITHUB_TOKEN replaces App auth, the runner replaces the queue
(one job per workflow run), and engines run in-process — the ephemeral
runner is the credential-isolation boundary that the controller/agent
split provides in server mode. Engine subprocesses still receive
`allowlisted_env` only.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from themis.config import (
    VALID_SANDBOXES,
    Settings,
    SettingsError,
    _decode_default_repo_config,
)
from themis.engines import ENGINE_NAMES, resolve
from themis.events import DiscussJob, ReviewJob, parse_event
from themis.github.client import GitHubClient
from themis.review_service import ReviewService
from themis.trusted_context import apply_trusted_context
from themis.workspace import prepare_workspace, remove_workspace

logger = logging.getLogger(__name__)

DEFAULT_MENTION = "@themis"
DEFAULT_BOT_LOGIN = "github-actions[bot]"


class ActionError(Exception):
    """The Actions environment is missing or malformed; fail the run."""


def load_event() -> tuple[str, dict]:
    """The event name and parsed payload from the standard Actions env."""
    name = os.environ.get("GITHUB_EVENT_NAME")
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not name or not path:
        raise ActionError(
            "GITHUB_EVENT_NAME and GITHUB_EVENT_PATH are required; "
            "is this running inside GitHub Actions?"
        )
    try:
        payload = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ActionError(f"cannot read event payload at {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ActionError("event payload is not a JSON object")
    return name, payload


def action_settings() -> Settings:
    """Instance settings for action mode, from THEMIS_* env.

    This is deliberately not load_settings(): App credentials, webhook and
    agent-service wiring do not exist here. Values are trusted operator
    input (workflow yaml), so invalid ones fail fast like server settings.
    """
    engine = os.environ.get("THEMIS_ENGINE") or "claude"
    if engine not in ENGINE_NAMES:
        raise SettingsError(
            f"invalid engine {engine!r}; expected one of {ENGINE_NAMES}"
        )
    sandbox = os.environ.get("THEMIS_CODEX_SANDBOX") or "workspace-write"
    if sandbox not in VALID_SANDBOXES:
        raise SettingsError(
            f"invalid codex sandbox {sandbox!r}; expected one of {VALID_SANDBOXES}"
        )
    runner_temp = Path(os.environ.get("RUNNER_TEMP") or "/tmp")
    workspace_root = Path(
        os.environ.get("THEMIS_WORKSPACE_ROOT") or runner_temp / "themis"
    )
    return Settings(
        gh_app_client_id="",
        gh_app_private_key_pem="",
        gh_webhook_secret=None,
        webhook_enabled=False,
        api_token=None,
        codex_sandbox=sandbox,
        engine=engine,
        workspace_root=workspace_root,
        public_url=None,
        tunnel_api=None,
        agent_url="",
        agent_token="",
        data_root=runner_temp / "themis-data",
        default_repo_config=(
            _decode_default_repo_config(raw)
            if (raw := os.environ.get("THEMIS_DEFAULT_REPO_CONFIG") or None)
            else None
        ),
    )


def build_action_service(
    settings: Settings, token: str, bot_login: str, mention: str
) -> ReviewService:
    async def get_token(installation_id: int) -> str:
        # Static workflow token: "re-minting" (done around long engine runs
        # in server mode) is a harmless no-op here.
        return token

    return ReviewService(
        settings=settings,
        bot_login=bot_login,
        mention=mention,
        get_token=get_token,
        make_client=GitHubClient,
        prepare=prepare_workspace,
        cleanup=remove_workspace,
        resolve_engine=lambda name: resolve(
            name, codex_sandbox=settings.codex_sandbox
        ),
        trust_context=apply_trusted_context,
        # Learnings need cross-run persistence and a digest branch; both are
        # out of scope on an ephemeral runner.
        learning_service=None,
    )


def materialize_codex_auth() -> None:
    """Write THEMIS_CODEX_AUTH_JSON to CODEX_HOME/auth.json, then drop the
    env var so the secret does not linger in the process environment.

    The codex CLI authenticates from a file, not an env var, so action users
    pass the auth.json content as a secret and this writes it where the
    engine (and outbound redaction) already look. An operator-provided
    CODEX_HOME wins; otherwise a private directory under RUNNER_TEMP is
    created and exported for the engine subprocess env allowlist.
    """
    raw = os.environ.pop("THEMIS_CODEX_AUTH_JSON", None)
    if not raw:
        return
    home = Path(
        os.environ.get("CODEX_HOME")
        or Path(os.environ.get("RUNNER_TEMP") or "/tmp") / "codex-home"
    )
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    auth_path = home / "auth.json"
    auth_path.touch(mode=0o600, exist_ok=True)
    auth_path.chmod(0o600)
    auth_path.write_text(raw)
    os.environ["CODEX_HOME"] = str(home)


async def run_action() -> str:
    """Route the triggering event to one review/discussion run.

    Returns the outcome ("review", "discuss", "skipped") for logging and
    tests. Job failures propagate: a non-zero exit marks the workflow run
    red, after the pipeline's own courtesy comments have posted.
    """
    event, payload = load_event()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ActionError(
            "GITHUB_TOKEN is required (pass the workflow token or a PAT)"
        )
    materialize_codex_auth()
    mention = os.environ.get("THEMIS_MENTION") or DEFAULT_MENTION
    bot_login = os.environ.get("THEMIS_BOT_LOGIN") or DEFAULT_BOT_LOGIN
    job = parse_event(event, payload, mention)
    if job is None:
        logger.info("themis_action_skipped event=%s", event)
        return "skipped"
    service = build_action_service(action_settings(), token, bot_login, mention)
    if isinstance(job, ReviewJob):
        logger.info(
            "themis_action_review repo=%s pr=%s auto=%s",
            job.repo, job.pr_number, job.auto,
        )
        await service.review(
            job.repo, job.pr_number, job.installation_id, job.auto,
            trigger_comment_id=job.trigger_comment_id,
            extra_context=job.extra_context,
        )
        return "review"
    assert isinstance(job, DiscussJob)
    logger.info(
        "themis_action_discuss repo=%s pr=%s kind=%s",
        job.repo, job.pr_number, job.kind,
    )
    await service.discuss(
        repo=job.repo, pr_number=job.pr_number,
        installation_id=job.installation_id, comment_id=job.comment_id,
        body=job.body, kind=job.kind, in_reply_to_id=job.in_reply_to_id,
        mentions_bot=job.mentions_bot,
        author_association=job.author_association,
        author_login=job.author_login,
    )
    return "discuss"


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    asyncio.run(run_action())
