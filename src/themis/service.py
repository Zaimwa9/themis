"""Themis orchestration: ReviewService + queue job runners."""

import asyncio
import ast
import contextlib
import json
import logging
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import httpx

from themis.config import (
    REPO_CONFIG_PATH,
    RepoConfig,
    Settings,
    parse_repo_config,
    resolve_modules,
)
from themis.engines import Engine, EngineError, EngineQuotaError, EngineUnavailableError
from themis.events import TRUSTED_ASSOCIATIONS
from themis.github.auth import get_installation_token, make_app_jwt
from themis.github.client import GitHubClient, GitHubGraphQLError
from themis.learnings import (
    LEARNINGS_REPO_PATH,
    Learning,
    PendingStore,
    compose_digest,
    effective_set,
    is_duplicate,
    new_learning,
    parse_jsonl,
    prune_merged,
    to_jsonl,
)
from themis.output import (
    MAX_BODY_LEN,
    OUTPUT_DIR,
    OutputError,
    ReviewActions,
    parse_learning,
    parse_output,
    parse_reply,
)
from themis.prompts import DOCTRINE_PATH, build_discussion_prompt, build_review_prompt
from themis.security import redact_outbound
from themis.remote import RemoteEngine
from themis.workspace import (
    clone_url_for,
    prepare_workspace,
    remove_workspace,
    run_git,
    sweep_stale,
)

logger = logging.getLogger(__name__)

INPUT_DIR = ".review-input"

T = TypeVar("T")

DEFAULT_MODELS = {
    "codex": "gpt-5.4",
    "claude": "claude-opus-4-6[1m]",
    "glm": "glm-5.2",
}

QUOTA_COMMENT = (
    "{engine_title} subscription usage limit reached, {noun} skipped. "
    "Mention me with `review` later to retry."
)
FAILURE_COMMENT = (
    "{noun_title} failed after {attempts} attempt(s) ({reason}). Check the worker logs."
)
CANCELLED_COMMENT = (
    "Review was cancelled before completing (worker timeout or shutdown). "
    "Mention {mention} with `review` to retry."
)
ENGINE_UNAVAILABLE_COMMENT = (
    "This Themis instance has no {engine} credentials configured ({hint}), "
    "so the {noun} was skipped. Configure it or set a different `engine` in "
    "`.themis/config.yaml`."
)
_ENGINE_AUTH_HINTS = {
    "codex": "auth.json in CODEX_HOME",
    "claude": "CLAUDE_CODE_OAUTH_TOKEN",
    "glm": "GLM_API_KEY",
}
LEARNING_FOOTER = (
    "\n\n🧠 Learning recorded — lands in `.themis/learnings.jsonl` "
    "via the next digest PR."
)
DIGEST_BRANCH = "themis/learnings"
DIGEST_PR_TITLE = "chore: sync review learnings"
DIGEST_PR_BODY = (
    "Review learnings captured from PR discussions, landing into "
    "`.themis/learnings.jsonl`.\n\n"
    "Edit or delete lines before merging — the merged file is what future "
    "reviews read. Closing without merging leaves the entries pending.\n\n"
    "See `docs/learnings.md` in the Themis repository for how this works."
)

# One agent run at a time so reviews never monopolize the shared worker.
# Note: this bounds agent runs per worker process only; running several worker
# processes yields one concurrent agent run per process.
_agent_slot = asyncio.Semaphore(1)


async def api_changed_paths(gh: Any, repo: str, pr_number: int) -> set[str] | None:
    """Paths changed by the PR from the GitHub API (authoritative merge-base
    diff), or None when the API read fails so the caller can fail open."""
    try:
        return set(await gh.list_pr_files(repo, pr_number))
    except httpx.HTTPStatusError as error:
        logger.warning(
            "themis_changed_paths_failed repo=%s pr=%s error=%s",
            repo, pr_number, error,
        )
        return None


async def git_head_sha(workspace: Path) -> str | None:
    """Sha of the checked-out PR head, or None when it cannot be determined."""
    returncode, output = await run_git("rev-parse", "HEAD", cwd=workspace)
    if returncode != 0:
        logger.warning("themis_head_sha_failed output=%s", output[-200:])
        return None
    return output.strip()


_DIFF_HUNK = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)


def _diff_path(header: str, prefix: str) -> str | None:
    path = header[4:]
    if path.startswith('"'):
        try:
            decoded = ast.literal_eval(path)
        except (SyntaxError, ValueError) as error:
            raise ValueError("invalid quoted path in git diff") from error
        if not isinstance(decoded, str):
            raise ValueError("non-string path in git diff")
        path = decoded
    if path == "/dev/null":
        return None
    return path.removeprefix(prefix)


async def git_changed_lines(
    workspace: Path, base_ref: str
) -> set[tuple[str, int, str]] | None:
    """Exact line anchors accepted by GitHub for the reviewed base...HEAD diff.

    The workspace contains both the PR head and origin/<base_ref>. A local
    zero-context diff is authoritative and is not subject to the REST Files
    API's patch truncation. Fail open when the shallow clone lacks a merge base.
    """
    returncode, output = await run_git(
        "-c", "core.quotePath=false", "diff", "--unified=0", "--no-color",
        "--no-ext-diff", f"origin/{base_ref}...HEAD", "--", cwd=workspace,
    )
    if returncode != 0:
        logger.warning(
            "themis_changed_lines_failed base=%s output=%s", base_ref, output[-200:]
        )
        return None

    anchors: set[tuple[str, int, str]] = set()
    old_path: str | None = None
    new_path: str | None = None
    for line in output.splitlines():
        try:
            if line.startswith("--- "):
                old_path = _diff_path(line, "a/")
                continue
            if line.startswith("+++ "):
                new_path = _diff_path(line, "b/")
                continue
        except ValueError as error:
            logger.warning("themis_changed_lines_failed error=%s", error)
            return None
        match = _DIFF_HUNK.match(line)
        if match is None:
            continue
        old_start, old_count, new_start, new_count = match.groups()
        if old_path:
            for number in range(int(old_start), int(old_start) + int(old_count or 1)):
                anchors.add((old_path, number, "LEFT"))
        if new_path:
            for number in range(int(new_start), int(new_start) + int(new_count or 1)):
                anchors.add((new_path, number, "RIGHT"))
    return anchors


@dataclass
class ReviewService:
    settings: Settings
    bot_login: str    # "<app-slug>[bot]"
    mention: str      # "@<app-slug>"
    get_token: Callable[[int], Awaitable[str]]
    make_client: Callable[[str], Any]
    prepare: Callable[..., Awaitable[Path]]
    cleanup: Callable[[Path], None]
    resolve_engine: Callable[[str], Engine]
    changed_paths: Callable[..., Awaitable[set[str] | None]] = api_changed_paths
    changed_lines: Callable[[Path, str], Awaitable[set[tuple[str, int, str]] | None]] = (
        git_changed_lines
    )
    head_sha: Callable[[Path], Awaitable[str | None]] = git_head_sha
    pending_store: PendingStore | None = None

    async def _fetch_repo_config(self, gh: Any, repo: str) -> RepoConfig:
        """Behavior config from the target repo's default branch. When the repo
        has none (or the read fails — a missing or broken .themis/ must never
        block reviews), the instance-level THEMIS_DEFAULT_REPO_CONFIG applies,
        then hardcoded defaults. A repo file replaces the instance default
        wholesale; the two are never merged per key."""
        try:
            text = await gh.get_file_text(repo, REPO_CONFIG_PATH)
        except httpx.HTTPError as error:
            logger.warning(
                "themis_repo_config_fetch_failed repo=%s error=%s", repo, error
            )
            text = None
        if text is None and self.settings.default_repo_config is not None:
            logger.info("themis_repo_config_default_used repo=%s", repo)
            text = self.settings.default_repo_config
        return parse_repo_config(text)

    async def _load_learnings(
        self, gh: Any, repo: str, repo_config: RepoConfig
    ) -> tuple[list[Learning], list[Learning]]:
        """(effective, pending) for injection; prunes merged pending entries.

        Empty when the feature is off or unconfigured. Any failure degrades
        to no learnings: memory must never block a review."""
        if self.pending_store is None or not repo_config.learnings.enabled:
            return [], []
        try:
            repo_text = await gh.get_file_text(repo, LEARNINGS_REPO_PATH)
        except httpx.HTTPError as error:
            logger.warning(
                "themis_learnings_fetch_failed repo=%s error=%s", repo, error
            )
            repo_text = None
        repo_entries = parse_jsonl(repo_text)
        try:
            pending = await self.pending_store.load(repo)
            pruned = prune_merged(pending, repo_entries)
            if len(pruned) != len(pending):
                await self.pending_store.replace(repo, pruned)
                pending = pruned
        except OSError as error:
            logger.warning(
                "themis_learnings_store_failed repo=%s error=%s", repo, error
            )
            pending = []
        try:
            flushed = await self.pending_store.load_flushed(repo)
            # pr None = branch written but create_pr failed; the marker stays
            # so the next flush can prove the orphaned branch is ours.
            if flushed is not None and flushed["pr"] is not None:
                pr = await gh.get_pr(repo, flushed["pr"])
                if pr.get("merged"):
                    repo_ids = {e.id for e in repo_entries}
                    zombies = {i for i in flushed["ids"] if i not in repo_ids}
                    if zombies:
                        await self.pending_store.discard(repo, zombies)
                        pending = [p for p in pending if p.id not in zombies]
                        logger.info(
                            "themis_learnings_rejected_pruned repo=%s count=%d",
                            repo, len(zombies),
                        )
                    # Delete our merged digest branch so the next flush can
                    # recreate it (a squash merge leaves it diverged, which
                    # the flush's fast-forward guard would refuse) — but only
                    # while it still points at the exact commit we pushed:
                    # a recreated or taken-over branch is not ours to remove.
                    # Ordered before clear_flushed: a failed delete keeps the
                    # marker and retries on the next load.
                    ours = flushed["sha"]
                    if (
                        ours is not None
                        and await gh.find_branch_sha(repo, DIGEST_BRANCH) == ours
                    ):
                        await gh.delete_branch(repo, DIGEST_BRANCH)
                    await self.pending_store.clear_flushed(repo)
                elif pr.get("state") == "closed":
                    # Closed without merging: entries stay pending and re-flush
                    # at the next threshold crossing.
                    await self.pending_store.clear_flushed(repo)
        except (httpx.HTTPError, OSError) as error:
            logger.warning(
                "themis_learnings_flushed_check_failed repo=%s error=%s", repo, error
            )
        return effective_set(repo_entries, pending), pending

    def _gate_learning(
        self,
        workspace: Path,
        repo: str,
        pr_number: int,
        author_login: str,
        effective: list[Learning],
        pending: list[Learning],
    ) -> Learning | None:
        """Gate the agent's learning proposal; persistence happens separately,
        only after the 🧠-footered reply lands (see _persist_learning).

        Every rejection is logged, never raised: a bad proposal must not
        fail the discussion job whose reply already exists."""
        try:
            proposal = parse_learning(workspace)
        except OutputError as error:
            logger.warning(
                "themis_learning_rejected repo=%s reason=invalid error=%s",
                repo, redact_outbound(str(error))[:200],
            )
            return None
        if proposal is None:
            return None
        if proposal["confidence"] != "high":
            logger.info("themis_learning_rejected repo=%s reason=low-confidence", repo)
            return None
        if is_duplicate(proposal["text"], effective):
            logger.info("themis_learning_rejected repo=%s reason=duplicate", repo)
            return None
        supersedes = proposal["supersedes"]
        if supersedes and supersedes not in {e.id for e in effective}:
            # The model names the replacement target; never let it retire a
            # convention that is not currently live (unknown, already
            # superseded, or hallucinated ids would silently drop rules).
            logger.info(
                "themis_learning_rejected repo=%s reason=supersede-not-effective",
                repo,
            )
            return None
        if supersedes and any(p.supersedes == supersedes for p in pending):
            logger.info("themis_learning_rejected repo=%s reason=supersede-race", repo)
            return None
        return new_learning(
            text=proposal["text"],
            paths=tuple(proposal["paths"]),
            learnt_from=author_login,
            pr=pr_number,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            supersedes=supersedes,
        )

    async def _persist_learning(self, repo: str, learning: Learning) -> bool:
        """Store a gated learning; called only after its reply posted, so a
        failed reply never retains memory the commenter was not shown.

        The inverse failure (footer posted, store write fails) merely
        over-promises: the warning below surfaces it and the human can
        re-state the rule."""
        assert self.pending_store is not None  # gated by caller
        try:
            await self.pending_store.append(repo, learning)
        except OSError as error:
            logger.warning(
                "themis_learning_store_failed repo=%s error=%s", repo, error
            )
            return False
        logger.info("themis_learning_captured repo=%s id=%s", repo, learning.id)
        return True

    async def _flush_digest(self, gh: Any, repo: str, threshold: int) -> None:
        """Land pending learnings as one digest PR; best-effort.

        With no open digest PR the branch is created at (or fast-forwarded
        to) the default head so the PR diff is always exactly the learnings
        file; a same-named branch that cannot fast-forward is not provably
        ours and is left untouched, deferring the flush. An open PR from the
        reserved branch counts as our digest PR only when the flushed marker
        recorded its number — anything else is a human's PR and is never
        committed onto. While our digest PR is open its branch belongs to
        reviewers: only entries not already flushed are appended onto the
        branch's current file, so manual edits and deletions survive later
        flushes. The digest is a
        GitHub-facing write, so it goes through outbound redaction like
        every posted surface. Failures leave the buffer intact and never
        fail the job that triggered the flush. The threshold check lives
        here (not in the caller) so an I/O error while loading the pending
        count can never escape unguarded."""
        assert self.pending_store is not None
        try:
            pending = await self.pending_store.load(repo)
            if len(pending) < threshold:
                return
            default_branch = await gh.get_default_branch(repo)
            pr_number = await gh.find_open_pr(repo, DIGEST_BRANCH)
            flushed = await self.pending_store.load_flushed(repo)
            flushed_ids: set[str] = set()
            commit_sha: str | None = None
            if pr_number is None:
                base_sha = await gh.get_branch_sha(repo, default_branch)
                if await gh.upsert_branch(repo, DIGEST_BRANCH, base_sha):
                    base_text = await gh.get_file_text(repo, LEARNINGS_REPO_PATH)
                    to_flush = pending
                else:
                    # Not fast-forwardable. If the tip is the very commit our
                    # marker recorded, this is our own orphan (an earlier
                    # flush wrote the branch but create_pr failed): resume it.
                    # Anything else is not provably ours — leave it alone.
                    tip = await gh.find_branch_sha(repo, DIGEST_BRANCH)
                    if flushed is None or tip is None or flushed["sha"] != tip:
                        logger.warning(
                            "themis_digest_branch_conflict repo=%s branch=%s",
                            repo, DIGEST_BRANCH,
                        )
                        return
                    commit_sha = tip
                    flushed_ids = set(flushed["ids"])
                    to_flush = [e for e in pending if e.id not in flushed_ids]
                    base_text = await gh.get_file_text(
                        repo, LEARNINGS_REPO_PATH, ref=DIGEST_BRANCH
                    )
            else:
                if flushed is None or flushed["pr"] != pr_number:
                    # An open PR from the reserved branch that our marker did
                    # not record is someone else's PR — never commit onto it.
                    logger.warning(
                        "themis_digest_branch_conflict repo=%s branch=%s",
                        repo, DIGEST_BRANCH,
                    )
                    return
                flushed_ids = set(flushed["ids"])
                to_flush = [e for e in pending if e.id not in flushed_ids]
                if not to_flush:
                    return
                base_text = await gh.get_file_text(
                    repo, LEARNINGS_REPO_PATH, ref=DIGEST_BRANCH
                )
            if to_flush:
                content = compose_digest(base_text, to_flush)
                file_sha = await gh.get_file_sha(
                    repo, LEARNINGS_REPO_PATH, ref=DIGEST_BRANCH
                )
                commit_sha = await gh.put_file(
                    repo, LEARNINGS_REPO_PATH, content=redact_outbound(content),
                    message=DIGEST_PR_TITLE, branch=DIGEST_BRANCH, sha=file_sha,
                )
            all_ids = sorted(flushed_ids | {e.id for e in to_flush})
            if pr_number is None:
                # Marker before create_pr: if PR creation fails, the sha lets
                # the next flush prove the orphaned branch is ours and resume.
                await self.pending_store.record_flushed(
                    repo, all_ids, None, sha=commit_sha
                )
                pr_number = await gh.create_pr(
                    repo, title=DIGEST_PR_TITLE, body=DIGEST_PR_BODY,
                    head=DIGEST_BRANCH, base=default_branch,
                )
            await self.pending_store.record_flushed(
                repo, all_ids, pr_number, sha=commit_sha
            )
            logger.info("themis_digest_flushed repo=%s count=%d", repo, len(to_flush))
        except (httpx.HTTPError, GitHubGraphQLError, OSError) as error:
            logger.warning("themis_digest_flush_failed repo=%s error=%s", repo, error)

    def _engine_for(self, repo_config: RepoConfig) -> Engine:
        return self.resolve_engine(repo_config.engine or self.settings.engine)

    async def _ensure_engine_available(
        self, engine: Engine, installation_id: int, repo: str, pr_number: int, noun: str
    ) -> bool:
        if engine.available():
            return True
        logger.warning(
            "themis_engine_unavailable engine=%s repo=%s pr=%s",
            engine.name, repo, pr_number,
        )
        await self._post_courtesy_comment(
            installation_id, repo, pr_number,
            ENGINE_UNAVAILABLE_COMMENT.format(
                engine=engine.name, hint=_ENGINE_AUTH_HINTS[engine.name], noun=noun
            ),
        )
        return False

    async def review(
        self, repo: str, pr_number: int, installation_id: int, auto: bool,
        trigger_comment_id: int | None = None,
        extra_context: str | None = None,
    ) -> None:
        token = await self.get_token(installation_id)
        gh = self.make_client(token)
        async with gh:
            pr = await gh.get_pr(repo, pr_number)
            if pr.get("draft") or pr.get("state") != "open":
                logger.info("themis_skip_pr repo=%s pr=%s", repo, pr_number)
                return
            repo_config = await self._fetch_repo_config(gh, repo)
            if auto and not repo_config.triggers.auto_review:
                logger.info("themis_auto_review_disabled repo=%s pr=%s", repo, pr_number)
                return
            engine = self._engine_for(repo_config)
            if not await self._ensure_engine_available(
                engine, installation_id, repo, pr_number, "review"
            ):
                return
            # 👀 on the trigger = queued (router); 🚀 = job running, on the
            # trigger comment when there is one, else on the PR body.
            rocket_target = (
                {"issue_comment_id": trigger_comment_id}
                if trigger_comment_id is not None
                else {"issue_number": pr_number}
            )
            try:
                await gh.add_reaction(repo, content="rocket", **rocket_target)
            except httpx.HTTPError as error:
                logger.warning(
                    "themis_rocket_reaction_failed repo=%s pr=%s error=%s",
                    repo, pr_number, error,
                )
            threads = await gh.list_review_threads(repo, pr_number)
            learnings, _ = await self._load_learnings(gh, repo, repo_config)
            workspace = await self.prepare(
                root=self.settings.workspace_root,
                clone_url=clone_url_for(repo, token),
                pr_number=pr_number,
                base_ref=pr["base"]["ref"],
                depth=repo_config.limits.clone_depth,
            )
            try:
                _write_inputs(workspace, pr, threads, learnings=learnings)

                async def snapshot_ci() -> None:
                    snapshot = _unavailable_ci_snapshot(pr["head"]["sha"])
                    try:
                        ci_gh = self.make_client(await self.get_token(installation_id))
                        async with ci_gh:
                            fetched = await ci_gh.get_ci_snapshot(repo, pr["head"]["sha"])
                            if not isinstance(fetched, dict):
                                raise TypeError("CI snapshot is not an object")
                            snapshot = fetched
                    except Exception as error:
                        # CI context improves a review but must never delay or
                        # prevent it. Cancellation still propagates because
                        # asyncio.CancelledError is a BaseException.
                        logger.warning(
                            "themis_ci_snapshot_failed repo=%s pr=%s error=%s",
                            repo, pr_number, redact_outbound(str(error))[:200],
                        )
                    _write_checks_input(workspace, snapshot)

                # The doctrine is read from the PR checkout on purpose (see
                # docs/configuration.md); without one, the packaged default
                # doctrine applies and raises the presence profile.
                use_default_doctrine = not (workspace / DOCTRINE_PATH).exists()
                if use_default_doctrine:
                    logger.info(
                        "themis_default_doctrine_used repo=%s pr=%s", repo, pr_number
                    )
                modules = resolve_modules(
                    repo_config, default_doctrine=use_default_doctrine
                )
                prompt = build_review_prompt(
                    repo, pr_number, pr["base"]["ref"], extra_context=extra_context,
                    has_learnings=bool(learnings), modules=modules,
                    use_default_doctrine=use_default_doctrine,
                )
                actions = await self._attempt(
                    repo, pr_number, installation_id, workspace, repo_config, engine, prompt,
                    parse_output, noun="review", before_first_run=snapshot_ci,
                )
                if actions is None:
                    return
                # Codex runs can outlive the 60-min installation token; do every
                # post-codex GitHub read/write on a freshly minted one.
                post_gh = self.make_client(await self.get_token(installation_id))
                async with post_gh:
                    await self._drop_findings_outside_diff(
                        actions, post_gh, repo, pr_number, workspace, pr["base"]["ref"]
                    )
                    # After anchor validation, so a finding on unreviewed code
                    # keeps its outside-the-diff caveat instead of being folded
                    # as if it pointed at the diff.
                    _enforce_delivery_modules(actions, modules, repo, pr_number)
                    _keep_bot_authored_resolutions(
                        actions, threads, self.bot_login, repo, pr_number
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
        author_association: str = "NONE",
        author_login: str = "",
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
                if not mentions_bot and not _bot_in_thread(thread, self.bot_login):
                    return
                if not mentions_bot:
                    # The router skips the ack for unmentioned thread replies
                    # (relevance unknown until now); react here now that the
                    # bot is confirmed to be part of the thread.
                    try:
                        await gh.add_reaction(repo, review_comment_id=comment_id)
                    except httpx.HTTPError as error:
                        logger.warning(
                            "themis_discuss_reaction_failed repo=%s pr=%s comment=%s error=%s",
                            repo, pr_number, comment_id, error,
                        )
                reply_anchor = thread["comments"]["nodes"][0]["databaseId"]
            pr = await gh.get_pr(repo, pr_number)
            repo_config = await self._fetch_repo_config(gh, repo)
            learnings, pending = await self._load_learnings(gh, repo, repo_config)
            capture = (
                self.pending_store is not None
                and repo_config.learnings.enabled
                and author_association in TRUSTED_ASSOCIATIONS
            )
            engine = self._engine_for(repo_config)
            if not await self._ensure_engine_available(
                engine, installation_id, repo, pr_number, "reply"
            ):
                return
            workspace = await self.prepare(
                root=self.settings.workspace_root,
                clone_url=clone_url_for(repo, token),
                pr_number=pr_number,
                base_ref=pr["base"]["ref"],
                depth=repo_config.limits.clone_depth,
            )
            try:
                _write_inputs(
                    workspace, pr, [thread] if thread else [], learnings=learnings
                )
                prompt = build_discussion_prompt(
                    question=body,
                    kind=kind,
                    thread_context=json.dumps(thread, indent=2) if thread else "",
                    has_learnings=bool(learnings),
                    capture=capture,
                )
                reply = await self._attempt(
                    repo, pr_number, installation_id, workspace, repo_config, engine, prompt,
                    parse_reply, noun="reply",
                )
                if reply is None:
                    return
                captured = None
                if capture:
                    captured = self._gate_learning(
                        workspace, repo, pr_number, author_login, learnings, pending
                    )
                reply = redact_outbound(reply)
                if captured is not None:
                    reply += LEARNING_FOOTER
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
                    if captured is not None and await self._persist_learning(
                        repo, captured
                    ):
                        await self._flush_digest(
                            post_gh, repo, repo_config.learnings.digest_threshold
                        )
            finally:
                self.cleanup(workspace)

    async def _attempt(
        self,
        repo: str,
        pr_number: int,
        installation_id: int,
        workspace: Path,
        repo_config: RepoConfig,
        engine: Engine,
        prompt: str,
        parser: Callable[[Path], T],
        noun: str,
        before_first_run: Callable[[], Awaitable[None]] | None = None,
    ) -> T | None:
        """Run the engine + parse, with retries. Returns None when the quota is exhausted."""
        last_error: Exception = EngineError("no attempts ran")
        for attempt in range(1, repo_config.limits.max_attempts + 1):
            output_dir = workspace / OUTPUT_DIR
            if output_dir.exists():
                shutil.rmtree(output_dir)
            try:
                async with _agent_slot:
                    if before_first_run is not None:
                        await before_first_run()
                        before_first_run = None
                    agent_output = await engine.run(
                        prompt=prompt,
                        workspace=workspace,
                        model=repo_config.model.name or DEFAULT_MODELS[engine.name],
                        effort=repo_config.model.reasoning_effort,
                        timeout=repo_config.limits.timeout_seconds,
                        web_access=repo_config.web_access,
                    )
                try:
                    return parser(workspace)
                except OutputError as error:
                    # the agent exited 0 but its files are missing/invalid; its
                    # stdout is the only clue to why.
                    logger.warning(
                        "themis_agent_output_tail repo=%s pr=%s tail=%s",
                        repo, pr_number, redact_outbound(str(agent_output)[-1000:]),
                    )
                    # OutputError can embed malformed agent-controlled JSON.
                    # Replace it so the final queue traceback is safe too.
                    raise OutputError(redact_outbound(str(error))) from None
            except EngineQuotaError:
                logger.warning("themis_quota_reached repo=%s pr=%s", repo, pr_number)
                await self._post_courtesy_comment(
                    installation_id, repo, pr_number,
                    QUOTA_COMMENT.format(engine_title=engine.name.capitalize(), noun=noun),
                )
                return None
            except EngineUnavailableError:
                await self._post_courtesy_comment(
                    installation_id, repo, pr_number,
                    ENGINE_UNAVAILABLE_COMMENT.format(
                        engine=engine.name, hint=_ENGINE_AUTH_HINTS[engine.name], noun=noun
                    ),
                )
                return None
            except (EngineError, OutputError) as error:
                last_error = error
                logger.warning(
                    "themis_attempt_failed repo=%s pr=%s attempt=%d error=%s",
                    repo, pr_number, attempt, redact_outbound(str(error))[:200],
                )
        await self._post_courtesy_comment(
            installation_id, repo, pr_number,
            FAILURE_COMMENT.format(
                noun_title=noun.capitalize(),
                attempts=repo_config.limits.max_attempts,
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
        body = redact_outbound(body)
        try:
            gh = self.make_client(await self.get_token(installation_id))
            async with gh:
                await gh.post_issue_comment(repo, pr_number, body)
        except (httpx.HTTPStatusError, httpx.HTTPError) as error:
            logger.warning(
                "themis_courtesy_comment_failed repo=%s pr=%s error=%s",
                repo, pr_number, error,
            )

    async def _drop_findings_outside_diff(
        self, actions: ReviewActions, gh: Any, repo: str, pr_number: int,
        workspace: Path, base_ref: str,
    ) -> None:
        """GitHub 422s the whole review when one finding anchors outside the diff."""
        if not actions.findings:
            return
        allowed_paths = await self.changed_paths(gh, repo, pr_number)
        allowed_lines = await self.changed_lines(workspace, base_ref)
        if allowed_paths is None and allowed_lines is None:
            return

        def allowed(finding: dict[str, Any]) -> bool:
            if allowed_paths is not None and finding["path"] not in allowed_paths:
                return False
            if allowed_lines is None:
                return True
            start = finding.get("start_line", finding["line"])
            return all(
                (finding["path"], line, finding["side"]) in allowed_lines
                for line in range(start, finding["line"] + 1)
            )

        dropped = [f for f in actions.findings if not allowed(f)]
        if not dropped:
            return
        actions.findings = [f for f in actions.findings if allowed(f)]
        logger.warning(
            "themis_findings_outside_diff repo=%s pr=%s count=%d anchors=%s",
            repo, pr_number, len(dropped),
            [(f["path"], f["line"], f["side"]) for f in dropped],
        )
        lines = "\n".join(f"- `{f['path']}:{f['line']}` {f['body']}" for f in dropped)
        actions.summary += (
            f"\n\n##### {len(dropped)} finding(s) anchored outside the diff"
            f" (not posted inline)\n{lines}"
        )

    async def _post_review_results(
        self, gh: Any, repo: str, pr_number: int, commit_sha: str, actions: ReviewActions
    ) -> None:
        actions.summary = redact_outbound(actions.summary)
        for finding in actions.findings:
            finding["body"] = redact_outbound(finding["body"])
        for reply in actions.replies:
            reply["body"] = redact_outbound(reply["body"])
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
                    "themis_inline_post_failed repo=%s pr=%s error=%s response=%s",
                    repo, pr_number, error, redact_outbound(error.response.text[:500]),
                )
                lines = "\n".join(
                    f"- `{f['path']}:{f['line']}` {f['body']}" for f in actions.findings
                )
                summary += f"\n\n##### Findings (inline posting failed)\n{lines}"
        # Replies and resolutions are best effort (comments/threads can vanish);
        # a failure here must never kill the job, or a rerun would post the
        # non-idempotent inline review twice. The idempotent summary upsert
        # always runs last.
        for reply in actions.replies:
            try:
                await gh.post_reply(
                    repo, pr_number, in_reply_to=reply["in_reply_to"], body=reply["body"]
                )
            except (httpx.HTTPStatusError, GitHubGraphQLError) as error:
                logger.warning(
                    "themis_reply_post_failed repo=%s pr=%s in_reply_to=%s error=%s",
                    repo, pr_number, reply["in_reply_to"], error,
                )
        for thread_id in actions.resolve_thread_ids:
            try:
                await gh.resolve_thread(thread_id)
            except (httpx.HTTPStatusError, GitHubGraphQLError) as error:
                logger.warning(
                    "themis_resolve_failed repo=%s pr=%s thread=%s error=%s",
                    repo, pr_number, thread_id, error,
                )
        # actions.summary alone is capped by output.py, but the outside-diff
        # note and the 422 fold can push past GitHub's 65,536-char limit.
        if len(summary) > MAX_BODY_LEN:
            summary = summary[:64000] + "\n\n[summary truncated: GitHub comment length limit]"
        await gh.post_summary_comment(repo, pr_number, summary)


def _write_inputs(
    workspace: Path, pr: dict[str, Any], threads: list[dict[str, Any]],
    learnings: list[Learning] | None = None,
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
    if learnings:
        (input_dir / "learnings.jsonl").write_text(to_jsonl(learnings))


def _unavailable_ci_snapshot(head_sha: str) -> dict[str, Any]:
    return {
        "state": "unavailable",
        "head_sha": head_sha,
        "checks": [],
        "unavailable_sources": ["check_runs", "statuses"],
    }


def _write_checks_input(workspace: Path, snapshot: dict[str, Any]) -> None:
    input_dir = workspace / INPUT_DIR
    input_dir.mkdir(exist_ok=True)
    (input_dir / "checks.json").write_text(json.dumps(snapshot, indent=2))


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


# GFM allows fence lines to be indented up to three spaces; a fence is a run
# of backticks or tildes.
_FENCE_LINE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*?)[ \t]*\r?\n?$")


def _strip_suggestion_blocks(text: str) -> tuple[str, int]:
    """Remove real GitHub suggestion blocks; leave quoted ones alone.

    Line-based fence tracking rather than a regex: a suggestion fence
    inside an enclosing longer fence (a Markdown example quoting one) is
    prose about a suggestion, not a suggestion block. A closing fence must
    match its opener's character, per GFM. An unclosed suggestion fence is
    kept verbatim - never strip what cannot be parsed."""
    out: list[str] = []
    pending: list[str] = []  # lines of a suggestion block until it closes
    open_fence: tuple[str, int] | None = None  # enclosing non-suggestion fence
    suggestion: tuple[str, int] | None = None  # opening fence of pending block
    removed = 0
    for line in text.splitlines(keepends=True):
        match = _FENCE_LINE.match(line)
        fence: tuple[str, int, str] | None = None
        if match:
            # GFM strips whitespace around the info string ("```  suggestion"
            # still renders as a suggestion block), and a backtick fence's
            # info string cannot itself contain backticks.
            run, info = match.group(1), match.group(2).strip()
            if not (run[0] == "`" and "`" in info):
                fence = (run[0], len(run), info)
        if suggestion:
            pending.append(line)
            if (
                fence
                and not fence[2]
                and fence[0] == suggestion[0]
                and fence[1] >= suggestion[1]
            ):
                pending.clear()
                suggestion = None
                removed += 1
            continue
        if fence:
            char, length, info = fence
            if open_fence:
                if not info and char == open_fence[0] and length >= open_fence[1]:
                    open_fence = None
            elif info == "suggestion":
                suggestion = (char, length)
                pending.append(line)
                continue
            else:
                open_fence = (char, length)
        out.append(line)
    out.extend(pending)  # unclosed suggestion fence: keep, strip nothing
    return "".join(out), removed


def _enforce_delivery_modules(
    actions: ReviewActions, modules: dict[str, str], repo: str, pr_number: int
) -> None:
    """Backstop for disabled delivery modules: the prompt already forbids
    them, but an agent that emits them anyway must not reach GitHub with
    a surface the repo turned off. Findings are folded, never dropped."""
    if modules.get("code_suggestions") == "off":
        stripped = 0
        for finding in actions.findings:
            body, count = _strip_suggestion_blocks(finding["body"])
            if count:
                finding["body"] = body.rstrip()
                stripped += count
        summary, count = _strip_suggestion_blocks(actions.summary)
        if count:
            actions.summary = summary.rstrip()
            stripped += count
        if stripped:
            logger.warning(
                "themis_suggestions_stripped repo=%s pr=%s count=%d",
                repo, pr_number, stripped,
            )
    if modules.get("inline_findings") == "off" and actions.findings:
        logger.warning(
            "themis_inline_findings_folded repo=%s pr=%s count=%d",
            repo, pr_number, len(actions.findings),
        )
        heading = (
            "\n\n##### Findings (inline comments are disabled for this"
            " repository)\n"
        )
        # The summary is the only delivery surface here and it is one GitHub
        # comment: findings outrank prose on it. Reserve room for every
        # finding first - trimming the assessment if it hogs the budget -
        # then give each an equal share so the final length cap can never
        # drop a whole finding from the tail. A pathological finding count
        # still falls back to the global truncation guard at posting time.
        floor = 600
        marker = "\n[finding truncated to fit the summary comment]"
        pointers = [f"- `{f['path']}:{f['line']}` " for f in actions.findings]
        # Real pointer lengths, not an estimate: long paths shrink the share,
        # they never push tail findings past the cap. `fixed` is everything a
        # worst-case entry costs beyond its body share.
        fixed = sum(len(p) + 1 for p in pointers) + len(pointers) * len(marker)
        reserve = len(heading) + fixed + len(pointers) * floor + 512
        if len(actions.summary) > MAX_BODY_LEN - reserve:
            keep = max(0, MAX_BODY_LEN - reserve)
            actions.summary = (
                actions.summary[:keep]
                + "\n[assessment truncated to keep folded findings visible]"
            )
        budget = MAX_BODY_LEN - len(actions.summary) - len(heading) - 512
        # Equal body share of what remains after the fixed costs; with enough
        # findings it drops to zero and entries degrade to bare pointers -
        # every finding stays addressable up to far beyond any real review.
        share = max(0, (budget - fixed) // len(pointers))
        lines = []
        for pointer, finding in zip(pointers, actions.findings, strict=True):
            body = finding["body"]
            if len(body) > share:
                body = body[:share] + marker
            lines.append(pointer + body)
        actions.summary += heading + "\n".join(lines)
        actions.findings = []


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
            "themis_resolutions_dropped repo=%s pr=%s ids=%s",
            repo, pr_number, dropped,
        )
    actions.resolve_thread_ids = [
        t for t in actions.resolve_thread_ids if t in bot_thread_ids
    ]


async def _post_cancelled_comment(
    service: ReviewService, repo: str, pr_number: int, installation_id: int
) -> None:
    """Best-effort PR comment when the queue cancels the job (job timeout, no retry).

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
                redact_outbound(CANCELLED_COMMENT.format(mention=service.mention)),
            )

    post_task = asyncio.ensure_future(_post())
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(asyncio.shield(post_task), 10)


# --- queue job runners --------------------------------------------------------


def build_service(settings: Settings, bot_slug: str) -> ReviewService:
    async def get_token(installation_id: int) -> str:
        app_jwt = make_app_jwt(settings.gh_app_client_id, settings.gh_app_private_key_pem)
        async with httpx.AsyncClient(timeout=30) as client:
            return await get_installation_token(client, installation_id, app_jwt)

    return ReviewService(
        settings=settings,
        bot_login=f"{bot_slug}[bot]",
        mention=f"@{bot_slug}",
        get_token=get_token,
        make_client=GitHubClient,
        prepare=prepare_workspace,
        cleanup=remove_workspace,
        resolve_engine=lambda name: RemoteEngine(name, settings.agent_url, settings.agent_token),
        pending_store=PendingStore(settings.data_root),
    )


async def run_review_job(
    settings: Settings, bot_slug: str, repo: str, pr_number: int,
    installation_id: int, auto: bool, trigger_comment_id: int | None = None,
    extra_context: str | None = None,
) -> None:
    service = build_service(settings, bot_slug)
    await asyncio.to_thread(sweep_stale, settings.workspace_root)
    try:
        await service.review(
            repo, pr_number, installation_id, auto,
            trigger_comment_id=trigger_comment_id,
            extra_context=extra_context,
        )
    except asyncio.CancelledError:
        # The queue timeout also covers time spent behind the codex semaphore;
        # a cancelled review must not vanish with no PR comment.
        await _post_cancelled_comment(service, repo, pr_number, installation_id)
        raise


async def run_discussion_job(
    settings: Settings, bot_slug: str, *, repo: str, pr_number: int,
    installation_id: int, comment_id: int, body: str, kind: str,
    in_reply_to_id: int | None, mentions_bot: bool,
    author_association: str = "NONE", author_login: str = "",
) -> None:
    service = build_service(settings, bot_slug)
    await asyncio.to_thread(sweep_stale, settings.workspace_root)
    try:
        await service.discuss(
            repo=repo, pr_number=pr_number, installation_id=installation_id,
            comment_id=comment_id, body=body, kind=kind,
            in_reply_to_id=in_reply_to_id, mentions_bot=mentions_bot,
            author_association=author_association, author_login=author_login,
        )
    except asyncio.CancelledError:
        await _post_cancelled_comment(service, repo, pr_number, installation_id)
        raise
