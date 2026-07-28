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

import httpx

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
from themis.security import redact_outbound, register_secret
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


def resolve_github_token() -> str:
    """The GitHub-facing token, kept out of every live process environment.

    action.yml hands the token over as a file written by a step whose shell
    exits before any engine can run, because an exec-time environment is
    readable through /proc/<pid>/environ by any same-UID process — in
    action mode that includes the engine child, and the review runs on
    attacker-influenced PR content. From here the token exists only in
    this process's heap (cross-process memory reads are blocked by Yama
    ptrace restrictions on GitHub-hosted runners) and in the redaction
    registry. The GITHUB_TOKEN env fallback covers direct invocation
    outside action.yml; it is popped so the engine's ancestor chain below
    this process is clean either way.
    """
    path = os.environ.pop("THEMIS_GITHUB_TOKEN_FILE", None)
    if path:
        token_file = Path(path)
        try:
            token = token_file.read_text().strip()
        except OSError as error:
            raise ActionError(f"cannot read token file: {error}") from error
        token_file.unlink(missing_ok=True)
        os.environ.pop("GITHUB_TOKEN", None)
    else:
        token = os.environ.pop("GITHUB_TOKEN", None) or ""
    if not token:
        raise ActionError(
            "a GitHub token is required: THEMIS_GITHUB_TOKEN_FILE (action.yml) "
            "or GITHUB_TOKEN (direct invocation)"
        )
    register_secret(token)
    return token


FORK_SKIPPED_COMMENT = (
    "Fork pull requests are not reviewed in GitHub Action mode: "
    "comment-triggered workflows run with this repository's secrets, and "
    "the review engine must not execute fork-controlled code with an engine "
    "credential in reach. Push the branch to this repository, or use the "
    "server deployment."
)


async def _refuse_foreign_head(service: ReviewService, token: str, job) -> bool:
    """True when the PR head lives outside the base repository (fail closed).

    The `pull_request` no-secrets protection does not cover comment events:
    those run in the base-repo context with secrets even for fork PRs. The
    provenance check must come from the API, not the event payload — and
    before any clone or engine start. A head without a repo (deleted fork)
    is unknowable provenance, so it refuses too."""
    gh = service.make_client(token)
    async with gh:
        pr = await gh.get_pr(job.repo, job.pr_number)
        head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
        if head_repo == job.repo:
            return False
        logger.warning(
            "themis_action_fork_refused repo=%s pr=%s head=%s",
            job.repo, job.pr_number, head_repo,
        )
        try:
            await gh.post_issue_comment(
                job.repo, job.pr_number, redact_outbound(FORK_SKIPPED_COMMENT)
            )
        except httpx.HTTPError as error:
            # Best effort: fork-triggered `pull_request` runs hold a
            # read-only token and cannot comment.
            logger.warning(
                "themis_action_fork_comment_failed repo=%s pr=%s error=%s",
                job.repo, job.pr_number, error,
            )
        return True


async def run_action() -> str:
    """Route the triggering event to one review/discussion run.

    Returns the outcome ("review", "discuss", "skipped") for logging and
    tests. Job failures propagate: a non-zero exit marks the workflow run
    red, after the pipeline's own courtesy comments have posted.
    """
    event, payload = load_event()
    token = resolve_github_token()
    materialize_codex_auth()
    mention = os.environ.get("THEMIS_MENTION") or DEFAULT_MENTION
    bot_login = os.environ.get("THEMIS_BOT_LOGIN") or DEFAULT_BOT_LOGIN
    job = parse_event(event, payload, mention)
    if job is None:
        logger.info("themis_action_skipped event=%s", event)
        return "skipped"
    service = build_action_service(action_settings(), token, bot_login, mention)
    if await _refuse_foreign_head(service, token, job):
        return "skipped"
    if isinstance(job, ReviewJob):
        logger.info(
            "themis_action_review repo=%s pr=%s auto=%s",
            job.repo, job.pr_number, job.auto,
        )
        await service.review(
            job.repo, job.pr_number, job.installation_id, job.auto,
            trigger_comment_id=job.trigger_comment_id,
            extra_context=job.extra_context,
            delta=job.delta,
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
    try:
        asyncio.run(run_action())
    except (ActionError, SettingsError) as error:
        # Operator mistakes read as one message in the workflow log, not a
        # traceback; the run still fails. Engine/pipeline errors keep their
        # tracebacks — those are diagnostics, not configuration.
        raise SystemExit(f"themis action: {error}") from error
