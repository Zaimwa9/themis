"""Review and discussion orchestration plus queue job runners."""

import asyncio
import ast
import contextlib
import hashlib
import hmac
import json
import logging
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import httpx

from themis.config import (
    REPO_CONFIG_PATH,
    RepoConfig,
    Settings,
    parse_repo_config,
    resolve_modules,
    skip_title_match,
)
from themis.engines import (
    Engine,
    EngineAuthError,
    EngineError,
    EngineQuotaError,
    EngineUnavailableError,
    NATIVE_SKILLS_ENGINES,
)
from themis.events import TRUSTED_ASSOCIATIONS
from themis.github.auth import get_installation_token, make_app_jwt
from themis.github.client import (
    SUMMARY_MARKER,
    CommentScanCapped,
    GitHubClient,
    GitHubGraphQLError,
)
from themis.learning_service import LEARNING_FOOTER, LearningService
from themis.linked_context import fetch_linked_context
from themis.learnings import Learning, PendingStore, to_jsonl
from themis.output import (
    MAX_BODY_LEN,
    OUTPUT_DIR,
    OutputError,
    ReviewActions,
    parse_output,
    parse_reply,
    parse_resolution,
)
from themis.prompts import DOCTRINE_PATH, build_discussion_prompt, build_review_prompt
from themis.trusted_context import apply_trusted_context
from themis.security import redact_outbound, sanitize_agent_text
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
    "kimi": "kimi-k3",
    "openrouter": "openrouter/auto",
}

AUTH_EXPIRED_COMMENT = (
    "{engine_title} credentials have expired and must be re-authenticated "
    "by the operator ({hint}); the {noun} was skipped. Reviews stay paused "
    "until then."
)
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
TITLE_SKIP_MARKER = "<!-- themis:title-skip -->"
# Embedded in every summary comment so a later push can delta-review
# `<last-reviewed-sha>..HEAD` (issue #11). A forged sha would silently shrink
# - or, matching the head, entirely skip - the delta under review, so a
# checkpoint only counts when the comment is bot-authored, the marker sits at
# the fixed prefix the controller prepends, and the tag verifies under the
# controller's key. Backing all three: no agent-written body can contain
# marker text at all (`sanitize_agent_text` defangs it), which is what stops
# a discussion reply - engine prose from position 0 - from opening with a
# checkpoint of its own. Exactly 40 hex on both sides: one shape, so no short
# checkpoint can prefix-match a head sha.
_SHA_RE = r"[0-9a-f]{40}"
_CHECKPOINT_TAG_LEN = 32
REVIEWED_SHA_MARKER = "<!-- themis:reviewed-sha {sha} {tag} -->"
_SUMMARY_CHECKPOINT_RE = re.compile(
    re.escape(SUMMARY_MARKER)
    + rf"\n<!-- themis:reviewed-sha ({_SHA_RE}) ([0-9a-f]{{{_CHECKPOINT_TAG_LEN}}}) -->\n"
)
TITLE_SKIPPED_COMMENT = (
    "Automatic review skipped: the PR title matches the `triggers.skip_titles` "
    "rule `{pattern}` in `.themis/config.yaml`. Mention {mention} with `review` "
    "to request one anyway."
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
    "kimi": "KIMI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def _code_span_safe(text: str) -> str:
    """Neutralize config text for a Markdown code span: a backtick or
    newline would break out of the span and render as live Markdown (team
    @-mentions would ping as the bot)."""
    return text.replace("`", "'").replace("\r", " ").replace("\n", " ")


# Engine-run gate, sized to THEMIS_CONCURRENCY at startup (default one) so
# the queue's parallel consumers get matching engine slots instead of
# serializing here. Note: this bounds agent runs per worker process only;
# running several worker processes yields that many slots per process.
_agent_slot = asyncio.Semaphore(1)


def configure_agent_slot(concurrency: int) -> None:
    """Rebind the engine-run gate to admit `concurrency` holders.

    Called once at startup, before any job runs; jobs read the module
    global at acquire time, so the rebind is race-free."""
    global _agent_slot
    _agent_slot = asyncio.Semaphore(concurrency)


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


async def git_is_ancestor(workspace: Path, sha: str) -> bool:
    """Whether sha exists in the clone and is an ancestor of HEAD.

    False covers every unusable case alike - unknown object (force-push
    discarded it, or the shallow clone is too old to contain it), rewritten
    history, git failure - because the caller's fallback for all of them is
    the same safe direction: a full review instead of a delta."""
    returncode, _ = await run_git(
        "merge-base", "--is-ancestor", sha, "HEAD", cwd=workspace
    )
    return returncode == 0


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


class DeltaBaseUnknown(Exception):
    """A prior review may exist but the commit it covered cannot be
    established, so a delta cannot be scoped. Distinct from "never reviewed":
    the recovery is a full review, not a skip."""


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
    is_ancestor: Callable[[Path, str], Awaitable[bool]] = git_is_ancestor
    trust_context: Callable[..., Awaitable[tuple[bool, bool]]] = apply_trusted_context
    learning_service: LearningService | None = None

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

    async def _learning_context(
        self, gh: Any, repo: str, repo_config: RepoConfig
    ) -> tuple[list[Learning], list[Learning]]:
        if self.learning_service is None or not repo_config.learnings.enabled:
            return [], []
        return await self.learning_service.load(gh, repo)

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

    def _checkpoint_key(self) -> str:
        """Secret the checkpoint tag is keyed with, or "" when there is none.

        Server mode has the App private key. Action mode deliberately has no
        App credentials at all, and the workflow token it does hold is
        per-run, so nothing there can sign a checkpoint one run and verify it
        the next - delta re-reviews are simply unavailable in that mode."""
        return self.settings.gh_app_private_key_pem

    def _checkpoint_tag(self, repo: str, pr_number: int, sha: str) -> str:
        """Keyed binding of a checkpoint to its repo, PR and commit.

        The key is the GitHub App private key: controller-held, required in
        every deployment, and never in an engine's allowlisted env - so a
        checkpoint cannot be manufactured by an agent body, nor replayed from
        another PR. Prevention (`sanitize_agent_text` keeps marker text out
        of agent-written comments) and this verification are deliberately
        both in place: one missed sanitisation point must not be enough to
        skip a push's review."""
        return hmac.new(
            self._checkpoint_key().encode(),
            f"themis-checkpoint\0{repo}\0{pr_number}\0{sha}".encode(),
            hashlib.sha256,
        ).hexdigest()[:_CHECKPOINT_TAG_LEN]

    def _checkpoint_match(
        self, comment: dict[str, Any], repo: str, pr_number: int
    ) -> tuple[str | None, bool]:
        """`(verified sha or None, carried an unverifiable checkpoint)`.

        Three conditions, all necessary for a sha: the bot authored it, the
        marker sits at the fixed prefix the controller prepends (never
        mid-body), and the tag verifies. The second element separates "this
        comment is not a checkpoint" from "this comment claims to be one and
        I cannot confirm it" - the caller must not read the latter as a PR
        that was never reviewed."""
        if ((comment.get("user") or {}).get("login") or "") not in _bot_logins(
            self.bot_login
        ):
            return None, False
        match = _SUMMARY_CHECKPOINT_RE.match(comment.get("body") or "")
        if match is None:
            return None, False
        sha, tag = match.group(1), match.group(2)
        if not hmac.compare_digest(tag, self._checkpoint_tag(repo, pr_number, sha)):
            # Loud: a key rotation reads the same as a forgery attempt here,
            # and both must be diagnosable. The scan keeps going, so an older
            # genuine checkpoint still wins.
            logger.warning(
                "themis_delta_checkpoint_unverified repo=%s pr=%s sha=%s",
                repo, pr_number, sha,
            )
            return None, True
        return sha, False

    def _checkpoint_sha(
        self, comment: dict[str, Any], repo: str, pr_number: int
    ) -> str | None:
        return self._checkpoint_match(comment, repo, pr_number)[0]

    async def _resolve_delta_base(
        self, gh: Any, repo: str, pr_number: int, head_sha: str
    ) -> str | None:
        """Sha the last themis review covered, or None when no delta should run.

        The marker is only trusted in comments the bot itself authored; the
        newest one wins, so the scan walks the conversation newest-first -
        a bounded oldest-first read would go blind on a busy PR and silently
        stop delta reviews there. The checkpoint predicate doubles as the
        pagination stop, so the fetch ends at the latest checkpoint however
        deep later conversation traffic buried it. A failed read skips the
        delta rather than degrading to a full review: synchronize fires on
        every push, and a transient GitHub error must not buy a full-cost
        review nobody asked for. DeltaBaseUnknown is raised instead when a
        prior review may well exist but its base cannot be established (the
        scan ran out of pages, or every checkpoint on record fails to
        verify): the caller reviews in full rather than skipping."""
        # One acceptance rule behind both the pagination stop and the parser:
        # a stop looser than the parser would end the scan on a comment the
        # parser then rejects, hiding the genuine checkpoint behind it. An
        # unverifiable checkpoint deliberately does not stop the scan - an
        # older genuine one still counts.
        try:
            comments = await gh.list_issue_comments_newest(
                repo,
                pr_number,
                stop=lambda c: self._checkpoint_sha(c, repo, pr_number) is not None,
            )
        except CommentScanCapped as error:
            raise DeltaBaseUnknown(str(error)) from None
        except (httpx.HTTPError, GitHubGraphQLError) as error:
            logger.warning(
                "themis_delta_comments_failed repo=%s pr=%s error=%s",
                repo, pr_number, error,
            )
            return None
        last_sha: str | None = None
        unverifiable = False
        for comment in comments:  # newest first: the first checkpoint is the latest
            last_sha, seen_unverifiable = self._checkpoint_match(
                comment, repo, pr_number
            )
            unverifiable = unverifiable or seen_unverifiable
            if last_sha is not None:
                break
        if last_sha is None:
            if unverifiable:
                # Rotated app key, or a second instance holding a different
                # one: this PR *was* reviewed, we just cannot say from where.
                raise DeltaBaseUnknown(f"{repo}#{pr_number}: no verifiable checkpoint")
            logger.info(
                "themis_delta_no_prior_review repo=%s pr=%s", repo, pr_number
            )
            return None
        if head_sha == last_sha:
            # The queue collapsed rapid pushes, or the webhook was redelivered:
            # the head on record is already reviewed.
            logger.info(
                "themis_delta_head_unchanged repo=%s pr=%s sha=%s",
                repo, pr_number, last_sha,
            )
            return None
        return last_sha

    async def review(
        self, repo: str, pr_number: int, installation_id: int, auto: bool,
        trigger_comment_id: int | None = None,
        extra_context: str | None = None,
        delta: bool = False,
    ) -> None:
        token = await self.get_token(installation_id)
        gh = self.make_client(token)
        async with gh:
            pr = await gh.get_pr(repo, pr_number)
            # Draft status gates first reviews only: an explicit request
            # (mention command, /api/review) is a deliberate ask and runs on a
            # draft (issue #70), and so does a delta re-review - a draft
            # carrying a themis review was already asked about, and the pushes
            # that follow answer its findings. Drafts are where the
            # iterate-on-findings loop actually happens, so skipping them would
            # leave delta unreachable for teams that review before marking
            # ready. A push to a draft with no prior review still gets nothing:
            # the delta base resolves to None below. Closed PRs are always
            # skipped.
            if pr.get("state") != "open" or (auto and not delta and pr.get("draft")):
                logger.info(
                    "themis_skip_pr repo=%s pr=%s state=%s draft=%s auto=%s",
                    repo, pr_number, pr.get("state"), bool(pr.get("draft")), auto,
                )
                return
            repo_config = await self._fetch_repo_config(gh, repo)
            # auto_review governs first reviews only. A delta re-review is
            # gated by triggers.delta_review and by a prior review existing on
            # the PR, which is consent enough: whoever asked for that review
            # (or the repo, by enabling auto_review then) also wants to hear
            # about the commits that answer it. Coupling the two would make
            # delta unreachable for mention-only repos, whose iterate-on-
            # findings loop is exactly what delta serves.
            if auto and not delta and not repo_config.triggers.auto_review:
                logger.info("themis_auto_review_disabled repo=%s pr=%s", repo, pr_number)
                return
            if auto and (
                pattern := skip_title_match(repo_config, pr.get("title") or "")
            ):
                # %r: patterns may hold spaces; repr keeps the key=value
                # line un-forgeable.
                logger.info(
                    "themis_auto_review_title_skipped repo=%s pr=%s pattern=%r",
                    repo, pr_number, pattern,
                )
                await self._post_title_skip_comment(
                    gh, installation_id, repo, pr_number, pattern
                )
                return
            delta_base: str | None = None
            if delta:
                if not repo_config.triggers.delta_review:
                    logger.info(
                        "themis_delta_review_disabled repo=%s pr=%s", repo, pr_number
                    )
                    return
                if not self._checkpoint_key():
                    # No controller secret to sign with (action mode holds no
                    # App key): an unkeyed tag is computable by anyone, so a
                    # checkpoint would be decorative. Skip rather than run an
                    # unrequested full review on every push.
                    logger.warning(
                        "themis_delta_unavailable_no_checkpoint_key repo=%s pr=%s",
                        repo, pr_number,
                    )
                    return
                try:
                    delta_base = await self._resolve_delta_base(
                        gh, repo, pr_number, pr["head"]["sha"]
                    )
                except DeltaBaseUnknown as error:
                    # A prior review exists (or may) but its base is not
                    # establishable. Skipping would silently drop the
                    # promised re-review; a full review is the safe recovery
                    # and re-seeds a verifiable checkpoint at the
                    # conversation tail, so the next push deltas again.
                    logger.warning(
                        "themis_delta_base_unknown_fallback_full repo=%s pr=%s reason=%s",
                        repo, pr_number, error,
                    )
                else:
                    if delta_base is None:
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
            learnings, _ = await self._learning_context(gh, repo, repo_config)
            # Issues/PRs the description references, resolved here because
            # the engine has no GitHub access (issues #82, #79). Best effort.
            linked_issues = await fetch_linked_context(gh, repo, pr)
            workspace = await self.prepare(
                root=self.settings.workspace_root,
                clone_url=clone_url_for(repo, token),
                pr_number=pr_number,
                base_ref=pr["base"]["ref"],
                depth=repo_config.limits.clone_depth,
            )
            try:
                # Trusted native context (issue #9). Runs on EVERY review:
                # even with no opt-in, PR-head instruction files must be
                # masked from the working tree because codex discovers
                # AGENTS.md natively with no CLI flag against it. Opted-in
                # capabilities additionally materialize the PR-base versions.
                # Returns the *effective* capabilities; materialization
                # fails closed per capability.
                # Engines without native skill discovery get the skills
                # bridge instead: a synthesized index of the base-revision
                # skills, plus one static prompt sentence (issue #49).
                skills_bridge = engine.name not in NATIVE_SKILLS_ENGINES
                native_context, native_skills = await self.trust_context(
                    workspace, pr["base"]["ref"],
                    context=repo_config.agent.context,
                    skills=repo_config.agent.skills,
                    skills_index=skills_bridge,
                )
                if delta_base is not None and not await self.is_ancestor(
                    workspace, delta_base
                ):
                    # Force-push rewrote the reviewed commit away, or the
                    # shallow clone no longer reaches it: there is no
                    # trustworthy delta, so review the whole PR again.
                    logger.info(
                        "themis_delta_fallback_full repo=%s pr=%s base=%s",
                        repo, pr_number, delta_base,
                    )
                    delta_base = None
                _write_inputs(
                    workspace, pr, threads, learnings=learnings,
                    linked_issues=linked_issues,
                )

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
                modules = resolve_modules(repo_config)
                prompt = build_review_prompt(
                    repo, pr_number, pr["base"]["ref"], extra_context=extra_context,
                    has_learnings=bool(learnings),
                    has_linked_issues=bool(linked_issues), modules=modules,
                    use_default_doctrine=use_default_doctrine,
                    skills_index=native_skills and skills_bridge,
                    delta_base=delta_base,
                )
                actions = await self._attempt(
                    repo, pr_number, installation_id, workspace, repo_config, engine, prompt,
                    parse_output, noun="review", before_first_run=snapshot_ci,
                    native_context=native_context, native_skills=native_skills,
                )
                if actions is None:
                    return
                # Codex runs can outlive the 60-min installation token; do every
                # post-codex GitHub read/write on a freshly minted one.
                # Redact before anything measures text: redaction can EXPAND
                # (a short secret becomes the longer marker), so budgets
                # computed on pre-redaction lengths would overflow the posting
                # cap and drop tail findings. _post_review_results sanitizes
                # again as the posting-path backstop; that is idempotent.
                _sanitize_actions(actions)
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
                    # After the human-thread guard, before the disposition
                    # backstop: a verified fix counts as addressing its thread.
                    _reconcile_fixed_threads(
                        actions, threads, self.bot_login, repo, pr_number
                    )
                    if delta_base is not None:
                        _note_unaddressed_threads(
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
            learnings, pending = await self._learning_context(gh, repo, repo_config)
            capture = (
                self.learning_service is not None
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
                # Discussions keep the fully-disabled baseline, but the mask
                # still runs: codex's native AGENTS.md discovery has no CLI
                # off-switch, so head instruction files must not be present.
                await self.trust_context(
                    workspace, pr["base"]["ref"], context=False, skills=False
                )
                _write_inputs(
                    workspace, pr, [thread] if thread else [], learnings=learnings
                )
                prompt = build_discussion_prompt(
                    question=body,
                    kind=kind,
                    thread_context=json.dumps(thread, indent=2) if thread else "",
                    has_learnings=bool(learnings),
                    capture=capture,
                    use_default_doctrine=not (workspace / DOCTRINE_PATH).exists(),
                )
                reply = await self._attempt(
                    repo, pr_number, installation_id, workspace, repo_config, engine, prompt,
                    parse_reply, noun="reply",
                )
                if reply is None:
                    return
                captured = None
                if capture:
                    assert self.learning_service is not None
                    captured = self.learning_service.capture(
                        workspace, repo, pr_number, author_login, learnings, pending
                    )
                # A discussion answer is an issue comment whose whole body the
                # engine wrote from a hostile PR's text: without defanging it
                # could open with a checkpoint prefix and suppress the delta
                # review of a planned push.
                reply = sanitize_agent_text(reply)
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
                    if (
                        captured is not None
                        and self.learning_service is not None
                        and await self.learning_service.persist(repo, captured)
                    ):
                        await self.learning_service.flush(
                            post_gh, repo, repo_config.learnings.digest_threshold
                        )
                    if thread is not None:
                        await self._resolve_answered_thread(
                            post_gh, workspace, thread, repo, pr_number
                        )
            finally:
                self.cleanup(workspace)

    async def _resolve_answered_thread(
        self, gh: Any, workspace: Path, thread: dict[str, Any],
        repo: str, pr_number: int,
    ) -> None:
        """Close the replied-to thread when the agent verified its fix.

        Runs after the reply is posted and never raises: the answer is the
        job's deliverable, and an unresolved thread is a far smaller failure
        than a lost reply. Only the bot's own threads qualify - resolving a
        thread a human opened would close their question on their behalf."""
        try:
            if not parse_resolution(workspace):
                return
        except OutputError as error:
            logger.warning(
                "themis_resolution_invalid repo=%s pr=%s error=%s",
                repo, pr_number, error,
            )
            return
        nodes = thread.get("comments", {}).get("nodes", [])
        author = (nodes[0].get("author") or {}).get("login", "") if nodes else ""
        if author not in _bot_logins(self.bot_login):
            logger.warning(
                "themis_resolution_dropped_not_bot_thread repo=%s pr=%s author=%s",
                repo, pr_number, author,
            )
            return
        try:
            await gh.resolve_thread(thread["id"])
        except (httpx.HTTPError, GitHubGraphQLError, KeyError) as error:
            logger.warning(
                "themis_resolve_thread_failed repo=%s pr=%s error=%s",
                repo, pr_number, error,
            )
            return
        logger.info(
            "themis_thread_resolved_on_reply repo=%s pr=%s thread=%s",
            repo, pr_number, thread.get("id"),
        )

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
        native_context: bool = False,
        native_skills: bool = False,
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
                        native_context=native_context,
                        native_skills=native_skills,
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
            except EngineAuthError as error:
                logger.warning(
                    "themis_engine_auth_failed repo=%s pr=%s engine=%s error=%s",
                    repo, pr_number, engine.name,
                    redact_outbound(str(error))[:200],
                )
                await self._post_courtesy_comment(
                    installation_id, repo, pr_number,
                    AUTH_EXPIRED_COMMENT.format(
                        engine_title=engine.name.capitalize(),
                        hint=_ENGINE_AUTH_HINTS[engine.name],
                        noun=noun,
                    ),
                )
                return None
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

    async def _post_title_skip_comment(
        self, gh: Any, installation_id: int, repo: str, pr_number: int, pattern: str
    ) -> None:
        """One explanatory comment per PR, found again by its marker.

        Unlike the repo-wide auto_review opt-out, a title skip is per-PR:
        without a trace on the PR itself it is indistinguishable from a
        crashed bot. But draft/ready toggles re-fire ready_for_review, so
        the comment must not accumulate duplicates."""
        try:
            comments = await gh.list_issue_comments(repo, pr_number)
            already_explained = any(
                TITLE_SKIP_MARKER in (comment.get("body") or "")
                for comment in comments
            )
        except (httpx.HTTPError, ValueError, AttributeError, TypeError) as error:
            # Best effort, shape included (a non-JSON 200 or a non-list body
            # must not kill the job): a failed dedup check may duplicate the
            # comment, never suppress the explanation entirely.
            logger.warning(
                "themis_title_skip_check_failed repo=%s pr=%s error=%s",
                repo, pr_number, error,
            )
            already_explained = False
        if already_explained:
            return
        await self._post_courtesy_comment(
            installation_id, repo, pr_number,
            TITLE_SKIP_MARKER + "\n" + TITLE_SKIPPED_COMMENT.format(
                pattern=_code_span_safe(pattern), mention=self.mention
            ),
        )

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
        _sanitize_actions(actions)
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
        # Before the resolutions below, which include these threads: a thread
        # that closes says why it closed, in the place the finding was raised.
        for entry in actions.fixed:
            if not entry["evidence"] or entry.get("in_reply_to") is None:
                continue
            try:
                await gh.post_reply(
                    repo, pr_number,
                    in_reply_to=entry["in_reply_to"], body=entry["evidence"],
                )
            except (httpx.HTTPStatusError, GitHubGraphQLError) as error:
                logger.warning(
                    "themis_fix_evidence_post_failed repo=%s pr=%s thread=%s error=%s",
                    repo, pr_number, entry["thread_id"], error,
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
        # After truncation, so the marker a later delta review reads back can
        # never be cut. Guarded on shape: the sha reached GitHub as-is, and a
        # non-hex value would break the marker's un-forgeable format.
        if self._checkpoint_key() and re.fullmatch(_SHA_RE, commit_sha):
            summary = (
                REVIEWED_SHA_MARKER.format(
                    sha=commit_sha,
                    tag=self._checkpoint_tag(repo, pr_number, commit_sha),
                )
                + "\n"
                + summary
            )
        await gh.post_summary_comment(repo, pr_number, summary)


def _write_inputs(
    workspace: Path, pr: dict[str, Any], threads: list[dict[str, Any]],
    learnings: list[Learning] | None = None,
    linked_issues: list[dict[str, Any]] | None = None,
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
    if linked_issues:
        (input_dir / "linked_issues.json").write_text(
            json.dumps(linked_issues, indent=2)
        )


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


def _sanitize_actions(actions: ReviewActions) -> None:
    # Engine-written every one of them, so control markers are defanged too:
    # the summary body is what a later delta review scans for a checkpoint.
    actions.summary = sanitize_agent_text(actions.summary)
    for finding in actions.findings:
        finding["body"] = sanitize_agent_text(finding["body"])
    for reply in actions.replies:
        reply["body"] = sanitize_agent_text(reply["body"])
    for entry in actions.fixed:
        entry["evidence"] = sanitize_agent_text(entry["evidence"])


def _strip_suggestion_blocks(text: str) -> tuple[str, int]:
    """Remove real GitHub suggestion blocks; leave quoted ones alone.

    Line-based fence tracking rather than a regex: a suggestion fence
    inside an enclosing longer fence (a Markdown example quoting one) is
    prose about a suggestion, not a suggestion block. A closing fence must
    match its opener's character, per GFM. An unclosed suggestion fence
    extends to end of text, per GFM, so it still renders apply-able and is
    stripped like a closed one."""
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
    if suggestion:  # unclosed fence runs to EOF: everything pending is inside
        removed += 1
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
                # A suggestion-only body must not strip to empty: GitHub
                # rejects the whole review batch on an empty comment body.
                finding["body"] = body.rstrip() or (
                    "Suggested change omitted (`code_suggestions` is disabled"
                    " for this repository); the fix targets the lines this"
                    " comment anchors to."
                )
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


def _note_unaddressed_threads(
    actions: ReviewActions, threads: list[dict[str, Any]], bot_login: str,
    repo: str, pr_number: int,
) -> None:
    """Delta backstop: the prompt requires a disposition (resolve or reply)
    for every open bot thread, but the engine is free-form and can omit one.
    Dispositions stay best-effort - a rerun for a missed thread would double
    engine cost and re-reply threads already answered - so an omission is
    surfaced in the summary instead of vanishing: the thread stays open and
    the reader sees that it was not re-checked."""
    logins = _bot_logins(bot_login)
    resolved = set(actions.resolve_thread_ids)
    replied = {reply["in_reply_to"] for reply in actions.replies}
    missed = []
    for thread in threads:
        if thread.get("isResolved"):
            continue
        nodes = thread.get("comments", {}).get("nodes", [])
        author = (nodes[0].get("author") or {}).get("login", "") if nodes else ""
        if author not in logins:
            continue
        if thread.get("id") in resolved:
            continue
        if any(node.get("databaseId") in replied for node in nodes):
            continue
        missed.append(thread)
    if not missed:
        return
    logger.warning(
        "themis_delta_threads_unaddressed repo=%s pr=%s count=%d ids=%s",
        repo, pr_number, len(missed), [t.get("id") for t in missed],
    )
    lines = "\n".join(
        f"- `{thread.get('path')}:{thread.get('line')}`" for thread in missed
    )
    actions.summary += (
        f"\n\n##### {len(missed)} earlier finding(s) were not re-checked in"
        f" this delta review\n{lines}\n\nThese threads stay open; the next"
        " review checks them again."
    )


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


def _reconcile_fixed_threads(
    actions: ReviewActions, threads: list[dict[str, Any]], bot_login: str,
    repo: str, pr_number: int,
) -> None:
    """Make "verified fixed" and "resolve that thread" one statement (issue #91).

    Every surviving `fixed` entry both posts its evidence in the thread and
    resolves it, so a review cannot say fixed in prose while the thread stays
    open. Claims this controller will not act on - someone else's thread, an id
    that is not on this PR, a thread already resolved - are dropped and named in
    `themis_thread_drift`: that combination is exactly the silent case, a review
    that reads as closing a finding and a thread that stays open. Resolutions
    arriving with no fix claim are logged the same way; they still apply, they
    just carry no recorded verification. Neither direction resolves anything on
    its own - surfacing drift is the point, never widening what gets closed.
    """
    logins = _bot_logins(bot_login)
    # None = the thread is ours to resolve but carries no comment to reply
    # under, so its evidence has nowhere to go; the resolution still stands.
    anchors: dict[str, int | None] = {}
    for thread in threads:
        if thread.get("isResolved"):
            continue
        nodes = thread.get("comments", {}).get("nodes", [])
        author = (nodes[0].get("author") or {}).get("login", "") if nodes else ""
        if author not in logins:
            continue
        anchor = nodes[0].get("databaseId")
        anchors[thread.get("id")] = anchor if isinstance(anchor, int) else None

    kept: list[dict[str, Any]] = []
    unresolvable: list[str] = []
    for entry in actions.fixed:
        if entry["thread_id"] not in anchors:
            unresolvable.append(entry["thread_id"])
            continue
        kept.append({**entry, "in_reply_to": anchors[entry["thread_id"]]})
    actions.fixed = kept

    claimed = [entry["thread_id"] for entry in kept]
    unclaimed = [t for t in actions.resolve_thread_ids if t not in set(claimed)]
    if unresolvable or unclaimed:
        logger.warning(
            "themis_thread_drift repo=%s pr=%s fixed_not_resolvable=%s"
            " resolved_without_claim=%s",
            repo, pr_number, unresolvable, unclaimed,
        )
    # dict.fromkeys: a thread named by both shapes is resolved once.
    actions.resolve_thread_ids = list(
        dict.fromkeys(actions.resolve_thread_ids + claimed)
    )


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
        learning_service=LearningService(PendingStore(settings.data_root)),
    )


async def run_review_job(
    settings: Settings, bot_slug: str, repo: str, pr_number: int,
    installation_id: int, auto: bool, trigger_comment_id: int | None = None,
    extra_context: str | None = None, delta: bool = False,
) -> None:
    service = build_service(settings, bot_slug)
    await asyncio.to_thread(sweep_stale, settings.workspace_root)
    try:
        await service.review(
            repo, pr_number, installation_id, auto,
            trigger_comment_id=trigger_comment_id,
            extra_context=extra_context,
            delta=delta,
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
