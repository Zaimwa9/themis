"""Reviewbot orchestration: ReviewService + ARQ task entry points."""

import asyncio
import contextlib
import json
import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import httpx

from reviewbot.codex import CodexError, CodexQuotaError, run_codex
from reviewbot.config import ReviewBotConfig, get_config, load_credentials
from reviewbot.github.auth import get_installation_token, make_app_jwt
from reviewbot.github.client import GitHubClient, GitHubGraphQLError
from reviewbot.output import (
    MAX_BODY_LEN,
    OUTPUT_DIR,
    OutputError,
    ReviewActions,
    parse_output,
    parse_reply,
)
from reviewbot.prompts import build_discussion_prompt, build_review_prompt
from reviewbot.workspace import (
    clone_url_for,
    prepare_workspace,
    remove_workspace,
    run_git,
    sweep_stale,
)

logger = logging.getLogger(__name__)

INPUT_DIR = ".review-input"

T = TypeVar("T")

QUOTA_COMMENT = (
    "Codex subscription usage limit reached, {noun} skipped. "
    "Mention me with `review` later to retry."
)
FAILURE_COMMENT = (
    "{noun_title} failed after {attempts} attempt(s) ({reason}). Check the worker logs."
)
CANCELLED_COMMENT = (
    "Review was cancelled before completing (worker timeout or shutdown). "
    "Mention {mention} with `review` to retry."
)

# One codex run at a time so reviews never monopolize the shared ARQ worker.
# Note: this bounds codex runs per worker process only; running several worker
# processes yields one concurrent codex run per process.
_codex_slot = asyncio.Semaphore(1)


async def api_changed_paths(gh: Any, repo: str, pr_number: int) -> set[str] | None:
    """Paths changed by the PR from the GitHub API (authoritative merge-base
    diff), or None when the API read fails so the caller can fail open."""
    try:
        return set(await gh.list_pr_files(repo, pr_number))
    except httpx.HTTPStatusError as error:
        logger.warning(
            "reviewbot_changed_paths_failed repo=%s pr=%s error=%s",
            repo, pr_number, error,
        )
        return None


async def git_head_sha(workspace: Path) -> str | None:
    """Sha of the checked-out PR head, or None when it cannot be determined."""
    returncode, output = await run_git("rev-parse", "HEAD", cwd=workspace)
    if returncode != 0:
        logger.warning("reviewbot_head_sha_failed output=%s", output[-200:])
        return None
    return output.strip()


@dataclass
class ReviewService:
    config: ReviewBotConfig
    get_token: Callable[[int], Awaitable[str]]
    make_client: Callable[[str], Any]
    prepare: Callable[..., Awaitable[Path]]
    cleanup: Callable[[Path], None]
    agent: Callable[..., Awaitable[str]]
    changed_paths: Callable[..., Awaitable[set[str] | None]] = api_changed_paths
    head_sha: Callable[[Path], Awaitable[str | None]] = git_head_sha

    async def review(self, repo: str, pr_number: int, installation_id: int) -> None:
        token = await self.get_token(installation_id)
        gh = self.make_client(token)
        async with gh:
            pr = await gh.get_pr(repo, pr_number)
            if pr.get("draft") or pr.get("state") != "open":
                logger.info("reviewbot_skip_pr repo=%s pr=%s", repo, pr_number)
                return
            # 👀 on the trigger = queued (router); 🚀 on the PR = job running.
            try:
                await gh.add_reaction(repo, issue_number=pr_number, content="rocket")
            except httpx.HTTPError as error:
                logger.warning(
                    "reviewbot_rocket_reaction_failed repo=%s pr=%s error=%s",
                    repo, pr_number, error,
                )
            threads = await gh.list_review_threads(repo, pr_number)
            workspace = await self.prepare(
                root=self.config.workspace_root,
                clone_url=clone_url_for(repo, token),
                pr_number=pr_number,
                base_ref=pr["base"]["ref"],
                depth=self.config.limits.clone_depth,
            )
            try:
                _write_inputs(workspace, pr, threads)
                prompt = build_review_prompt(repo, pr_number, pr["base"]["ref"])
                actions = await self._attempt(
                    repo, pr_number, installation_id, workspace, prompt,
                    parse_output, noun="review",
                )
                if actions is None:
                    return
                # Codex runs can outlive the 60-min installation token; do every
                # post-codex GitHub read/write on a freshly minted one.
                post_gh = self.make_client(await self.get_token(installation_id))
                async with post_gh:
                    await self._drop_findings_outside_diff(
                        actions, post_gh, repo, pr_number
                    )
                    _keep_bot_authored_resolutions(
                        actions, threads, self.config.bot_login, repo, pr_number
                    )
                    # Anchor to the tree codex actually reviewed: the author may
                    # have pushed between the webhook and the clone.
                    commit_sha = await self.head_sha(workspace) or pr["head"]["sha"]
                    await self._post_review_results(
                        post_gh, repo, pr_number, commit_sha, actions
                    )
            finally:
                self.cleanup(workspace)

    async def discuss(
        self,
        *,
        repo: str,
        pr_number: int,
        installation_id: int,
        comment_id: int,
        body: str,
        kind: str,
        in_reply_to_id: int | None,
        mentions_bot: bool,
    ) -> None:
        token = await self.get_token(installation_id)
        gh = self.make_client(token)
        async with gh:
            thread: dict[str, Any] | None = None
            reply_anchor: int | None = None
            if kind == "thread":
                threads = await gh.list_review_threads(repo, pr_number)
                thread = _find_thread(threads, {comment_id, in_reply_to_id})
                if thread is None:
                    return
                if not mentions_bot and not _bot_in_thread(thread, self.config.bot_login):
                    return
                if not mentions_bot:
                    # The router skips the ack for unmentioned thread replies
                    # (relevance unknown until now); react here now that the
                    # bot is confirmed to be part of the thread.
                    try:
                        await gh.add_reaction(repo, review_comment_id=comment_id)
                    except httpx.HTTPError as error:
                        logger.warning(
                            "reviewbot_discuss_reaction_failed repo=%s pr=%s comment=%s error=%s",
                            repo, pr_number, comment_id, error,
                        )
                reply_anchor = thread["comments"]["nodes"][0]["databaseId"]
            pr = await gh.get_pr(repo, pr_number)
            workspace = await self.prepare(
                root=self.config.workspace_root,
                clone_url=clone_url_for(repo, token),
                pr_number=pr_number,
                base_ref=pr["base"]["ref"],
                depth=self.config.limits.clone_depth,
            )
            try:
                _write_inputs(workspace, pr, [thread] if thread else [])
                prompt = build_discussion_prompt(
                    question=body,
                    kind=kind,
                    thread_context=json.dumps(thread, indent=2) if thread else "",
                )
                reply = await self._attempt(
                    repo, pr_number, installation_id, workspace, prompt,
                    parse_reply, noun="reply",
                )
                if reply is None:
                    return
                # Codex runs can outlive the 60-min installation token; post with
                # a fresh one.
                post_gh = self.make_client(await self.get_token(installation_id))
                async with post_gh:
                    if reply_anchor is not None:
                        await post_gh.post_reply(
                            repo, pr_number, in_reply_to=reply_anchor, body=reply
                        )
                    else:
                        await post_gh.post_issue_comment(repo, pr_number, reply)
            finally:
                self.cleanup(workspace)

    async def _attempt(
        self,
        repo: str,
        pr_number: int,
        installation_id: int,
        workspace: Path,
        prompt: str,
        parser: Callable[[Path], T],
        noun: str,
    ) -> T | None:
        """Run codex + parse, with retries. Returns None when the quota is exhausted."""
        last_error: Exception = CodexError("no attempts ran")
        for attempt in range(1, self.config.limits.max_attempts + 1):
            output_dir = workspace / OUTPUT_DIR
            if output_dir.exists():
                shutil.rmtree(output_dir)
            try:
                async with _codex_slot:
                    codex_output = await self.agent(
                        prompt=prompt,
                        workspace=workspace,
                        model=self.config.model.name,
                        effort=self.config.model.reasoning_effort,
                        timeout=self.config.limits.timeout_seconds,
                        sandbox=self.config.model.sandbox,
                    )
                try:
                    return parser(workspace)
                except OutputError:
                    # codex exited 0 but its files are missing/invalid; its
                    # stdout is the only clue to why.
                    logger.warning(
                        "reviewbot_codex_output_tail repo=%s pr=%s tail=%s",
                        repo, pr_number, str(codex_output)[-1000:],
                    )
                    raise
            except CodexQuotaError:
                logger.warning("reviewbot_quota_reached repo=%s pr=%s", repo, pr_number)
                await self._post_courtesy_comment(
                    installation_id, repo, pr_number, QUOTA_COMMENT.format(noun=noun)
                )
                return None
            except (CodexError, OutputError) as error:
                last_error = error
                logger.warning(
                    "reviewbot_attempt_failed repo=%s pr=%s attempt=%d error=%s",
                    repo, pr_number, attempt, str(error)[:200],
                )
        await self._post_courtesy_comment(
            installation_id, repo, pr_number,
            FAILURE_COMMENT.format(
                noun_title=noun.capitalize(),
                attempts=self.config.limits.max_attempts,
                reason=type(last_error).__name__,
            ),
        )
        raise last_error

    async def _post_courtesy_comment(
        self, installation_id: int, repo: str, pr_number: int, body: str
    ) -> None:
        """Post a quota/failure status comment on a fresh token, best effort.

        Codex can outlive the 60-min token minted before the semaphore wait, so
        the original client is likely dead. A failed courtesy comment must never
        mask the real outcome (quota returns None; failure re-raises last_error).
        """
        try:
            gh = self.make_client(await self.get_token(installation_id))
            async with gh:
                await gh.post_issue_comment(repo, pr_number, body)
        except (httpx.HTTPStatusError, httpx.HTTPError) as error:
            logger.warning(
                "reviewbot_courtesy_comment_failed repo=%s pr=%s error=%s",
                repo, pr_number, error,
            )

    async def _drop_findings_outside_diff(
        self, actions: ReviewActions, gh: Any, repo: str, pr_number: int,
    ) -> None:
        """GitHub 422s the whole review when one finding anchors outside the diff."""
        if not actions.findings:
            return
        allowed = await self.changed_paths(gh, repo, pr_number)
        if allowed is None:
            return
        dropped = [f for f in actions.findings if f["path"] not in allowed]
        if not dropped:
            return
        actions.findings = [f for f in actions.findings if f["path"] in allowed]
        logger.warning(
            "reviewbot_findings_outside_diff repo=%s pr=%s count=%d paths=%s",
            repo, pr_number, len(dropped), [f["path"] for f in dropped],
        )
        lines = "\n".join(f"- `{f['path']}:{f['line']}` {f['body']}" for f in dropped)
        actions.summary += (
            f"\n\n##### {len(dropped)} finding(s) anchored outside the diff"
            f" (not posted inline)\n{lines}"
        )

    async def _post_review_results(
        self, gh: Any, repo: str, pr_number: int, commit_sha: str, actions: ReviewActions
    ) -> None:
        summary = actions.summary
        if actions.findings:
            try:
                await gh.post_review(
                    repo, pr_number, commit_sha=commit_sha, comments=actions.findings
                )
            except httpx.HTTPStatusError as error:
                # Anchoring 422s when a line is outside the diff; keep the findings.
                # Anything else (401/403/500) is a real posting failure: propagate.
                if error.response.status_code != 422:
                    raise
                logger.warning(
                    "reviewbot_inline_post_failed repo=%s pr=%s error=%s",
                    repo, pr_number, error,
                )
                lines = "\n".join(
                    f"- `{f['path']}:{f['line']}` {f['body']}" for f in actions.findings
                )
                summary += f"\n\n##### Findings (inline posting failed)\n{lines}"
        # Replies and resolutions are best effort (comments/threads can vanish);
        # a failure here must never kill the job, or an ARQ rerun would post the
        # non-idempotent inline review twice. The idempotent summary upsert
        # always runs last.
        for reply in actions.replies:
            try:
                await gh.post_reply(
                    repo, pr_number, in_reply_to=reply["in_reply_to"], body=reply["body"]
                )
            except (httpx.HTTPStatusError, GitHubGraphQLError) as error:
                logger.warning(
                    "reviewbot_reply_post_failed repo=%s pr=%s in_reply_to=%s error=%s",
                    repo, pr_number, reply["in_reply_to"], error,
                )
        for thread_id in actions.resolve_thread_ids:
            try:
                await gh.resolve_thread(thread_id)
            except (httpx.HTTPStatusError, GitHubGraphQLError) as error:
                logger.warning(
                    "reviewbot_resolve_failed repo=%s pr=%s thread=%s error=%s",
                    repo, pr_number, thread_id, error,
                )
        # actions.summary alone is capped by output.py, but the outside-diff
        # note and the 422 fold can push past GitHub's 65,536-char limit.
        if len(summary) > MAX_BODY_LEN:
            summary = summary[:64000] + "\n\n[summary truncated: GitHub comment length limit]"
        await gh.post_summary_comment(repo, pr_number, summary)


def _write_inputs(
    workspace: Path, pr: dict[str, Any], threads: list[dict[str, Any]]
) -> None:
    input_dir = workspace / INPUT_DIR
    input_dir.mkdir(exist_ok=True)
    (input_dir / "pr.json").write_text(json.dumps({
        "number": pr.get("number"),
        "title": pr.get("title"),
        "body": pr.get("body"),
        "author": (pr.get("user") or {}).get("login"),
        "base_ref": pr["base"]["ref"],
        "head_sha": pr["head"]["sha"],
    }, indent=2))
    (input_dir / "threads.json").write_text(json.dumps(threads, indent=2))


def _find_thread(
    threads: list[dict[str, Any]], comment_ids: set[int | None]
) -> dict[str, Any] | None:
    ids = {i for i in comment_ids if i is not None}
    for thread in threads:
        nodes = thread.get("comments", {}).get("nodes", [])
        if any(node.get("databaseId") in ids for node in nodes):
            return thread
    return None


def _bot_logins(bot_login: str) -> set[str]:
    # GraphQL reports App authors without the [bot] suffix; REST includes it.
    return {bot_login, bot_login.removesuffix("[bot]")}


def _bot_in_thread(thread: dict[str, Any], bot_login: str) -> bool:
    logins = _bot_logins(bot_login)
    for node in thread.get("comments", {}).get("nodes", []):
        if (node.get("author") or {}).get("login", "") in logins:
            return True
    return False


def _keep_bot_authored_resolutions(
    actions: ReviewActions, threads: list[dict[str, Any]], bot_login: str,
    repo: str, pr_number: int,
) -> None:
    """Never resolve a thread a human opened, whatever the agent asked for."""
    if not actions.resolve_thread_ids:
        return
    logins = _bot_logins(bot_login)
    bot_thread_ids = set()
    for thread in threads:
        nodes = thread.get("comments", {}).get("nodes", [])
        author = (nodes[0].get("author") or {}).get("login", "") if nodes else ""
        if author in logins:
            bot_thread_ids.add(thread.get("id"))
    dropped = [t for t in actions.resolve_thread_ids if t not in bot_thread_ids]
    if dropped:
        logger.warning(
            "reviewbot_resolutions_dropped repo=%s pr=%s ids=%s",
            repo, pr_number, dropped,
        )
    actions.resolve_thread_ids = [
        t for t in actions.resolve_thread_ids if t in bot_thread_ids
    ]


# --- ARQ entry points -------------------------------------------------------
#
# Worker registration requirements (Task 12): register both tasks with a
# per-function timeout greater than 2 * limits.timeout_seconds plus headroom
# for clone/posting, and max_tries=1 (posting is not idempotent across reruns).


def _build_service() -> ReviewService:
    config = get_config()
    credentials = load_credentials()
    if credentials is None:
        raise RuntimeError("reviewbot credentials not configured")

    async def get_token(installation_id: int) -> str:
        app_jwt = make_app_jwt(credentials.client_id, credentials.private_key_pem)
        async with httpx.AsyncClient(timeout=30) as client:
            return await get_installation_token(client, installation_id, app_jwt)

    return ReviewService(
        config=config,
        get_token=get_token,
        make_client=GitHubClient,
        prepare=prepare_workspace,
        cleanup=remove_workspace,
        agent=run_codex,
    )


async def _post_cancelled_comment(
    service: ReviewService, repo: str, pr_number: int, installation_id: int
) -> None:
    """Best-effort PR comment when ARQ cancels the job (max_tries=1, no retry).

    Runs inside a cancelled task, so awaiting requires a separately created
    task shielded from the cancellation, capped at 10s. Any failure (post
    error, timeout, re-cancellation) is swallowed; the caller re-raises
    CancelledError so the courtesy comment can never mask or delay it.
    """
    async def _post() -> None:
        gh = service.make_client(await service.get_token(installation_id))
        async with gh:
            await gh.post_issue_comment(
                repo, pr_number,
                CANCELLED_COMMENT.format(mention=service.config.bot.mention),
            )

    post_task = asyncio.ensure_future(_post())
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(asyncio.shield(post_task), 10)


async def reviewbot_review_task(
    ctx: dict, repo: str, pr_number: int, installation_id: int
) -> None:
    service = _build_service()
    await asyncio.to_thread(sweep_stale, service.config.workspace_root)
    try:
        await service.review(repo, pr_number, installation_id)
    except asyncio.CancelledError:
        # ARQ's timeout also covers time queued behind the codex semaphore; a
        # cancelled review would otherwise vanish with no PR comment.
        await _post_cancelled_comment(service, repo, pr_number, installation_id)
        raise


async def reviewbot_discussion_task(
    ctx: dict,
    repo: str,
    pr_number: int,
    installation_id: int,
    comment_id: int,
    body: str,
    kind: str,
    in_reply_to_id: int | None,
    mentions_bot: bool,
) -> None:
    service = _build_service()
    await asyncio.to_thread(sweep_stale, service.config.workspace_root)
    try:
        await service.discuss(
            repo=repo, pr_number=pr_number, installation_id=installation_id,
            comment_id=comment_id, body=body, kind=kind,
            in_reply_to_id=in_reply_to_id, mentions_bot=mentions_bot,
        )
    except asyncio.CancelledError:
        await _post_cancelled_comment(service, repo, pr_number, installation_id)
        raise
