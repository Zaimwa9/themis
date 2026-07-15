"""Learning capture and digest-PR orchestration."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from themis.github.client import GitHubGraphQLError
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
)
from themis.output import OutputError, parse_learning
from themis.security import redact_outbound

logger = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class LearningService:
    """Owns the lifecycle from an agent proposal to a reviewed digest PR."""

    store: PendingStore

    async def load(self, gh: Any, repo: str) -> tuple[list[Learning], list[Learning]]:
        """Return effective and pending learnings, pruning completed state.

        Any failure degrades to partial or empty context: memory must never
        block a review or discussion.
        """
        try:
            repo_text = await gh.get_file_text(repo, LEARNINGS_REPO_PATH)
        except httpx.HTTPError as error:
            logger.warning(
                "themis_learnings_fetch_failed repo=%s error=%s", repo, error
            )
            repo_text = None
        repo_entries = parse_jsonl(repo_text)
        try:
            pending = await self.store.load(repo)
            pruned = prune_merged(pending, repo_entries)
            if len(pruned) != len(pending):
                await self.store.replace(repo, pruned)
                pending = pruned
        except OSError as error:
            logger.warning(
                "themis_learnings_store_failed repo=%s error=%s", repo, error
            )
            pending = []
        try:
            flushed = await self.store.load_flushed(repo)
            # pr None = branch written but create_pr failed; the marker stays
            # so the next flush can prove the orphaned branch is ours.
            if flushed is not None and flushed["pr"] is not None:
                pr = await gh.get_pr(repo, flushed["pr"])
                if pr.get("merged"):
                    repo_ids = {entry.id for entry in repo_entries}
                    zombies = {item for item in flushed["ids"] if item not in repo_ids}
                    if zombies:
                        await self.store.discard(repo, zombies)
                        pending = [item for item in pending if item.id not in zombies]
                        logger.info(
                            "themis_learnings_rejected_pruned repo=%s count=%d",
                            repo,
                            len(zombies),
                        )
                    # Only delete the digest branch while it still points at
                    # the exact commit we recorded; a replacement is not ours.
                    ours = flushed["sha"]
                    if (
                        ours is not None
                        and await gh.find_branch_sha(repo, DIGEST_BRANCH) == ours
                    ):
                        await gh.delete_branch(repo, DIGEST_BRANCH)
                    await self.store.clear_flushed(repo)
                elif pr.get("state") == "closed":
                    # Closed without merging: retain entries for a later digest.
                    await self.store.clear_flushed(repo)
        except (httpx.HTTPError, OSError) as error:
            logger.warning(
                "themis_learnings_flushed_check_failed repo=%s error=%s", repo, error
            )
        return effective_set(repo_entries, pending), pending

    def capture(
        self,
        workspace: Path,
        repo: str,
        pr_number: int,
        author_login: str,
        effective: list[Learning],
        pending: list[Learning],
    ) -> Learning | None:
        """Validate and gate the agent's proposed learning."""
        try:
            proposal = parse_learning(workspace)
        except OutputError as error:
            logger.warning(
                "themis_learning_rejected repo=%s reason=invalid error=%s",
                repo,
                redact_outbound(str(error))[:200],
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
        if supersedes and supersedes not in {entry.id for entry in effective}:
            logger.info(
                "themis_learning_rejected repo=%s reason=supersede-not-effective",
                repo,
            )
            return None
        if supersedes and any(item.supersedes == supersedes for item in pending):
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

    async def persist(self, repo: str, learning: Learning) -> bool:
        """Store a gated learning after its footer-bearing reply was posted."""
        try:
            await self.store.append(repo, learning)
        except OSError as error:
            logger.warning(
                "themis_learning_store_failed repo=%s error=%s", repo, error
            )
            return False
        logger.info("themis_learning_captured repo=%s id=%s", repo, learning.id)
        return True

    async def flush(self, gh: Any, repo: str, threshold: int) -> None:
        """Land pending learnings in a bot-owned digest PR, best effort.

        Branch ownership is proven through the flushed marker before any
        existing branch or PR is updated. Failures retain pending entries and
        never fail the discussion that triggered the flush.
        """
        try:
            pending = await self.store.load(repo)
            if len(pending) < threshold:
                return
            default_branch = await gh.get_default_branch(repo)
            pr_number = await gh.find_open_pr(repo, DIGEST_BRANCH)
            flushed = await self.store.load_flushed(repo)
            flushed_ids: set[str] = set()
            commit_sha: str | None = None
            if pr_number is None:
                base_sha = await gh.get_branch_sha(repo, default_branch)
                if await gh.upsert_branch(repo, DIGEST_BRANCH, base_sha):
                    base_text = await gh.get_file_text(repo, LEARNINGS_REPO_PATH)
                    to_flush = pending
                else:
                    # Resume only an orphan whose exact tip our marker records.
                    tip = await gh.find_branch_sha(repo, DIGEST_BRANCH)
                    if flushed is None or tip is None or flushed["sha"] != tip:
                        logger.warning(
                            "themis_digest_branch_conflict repo=%s branch=%s",
                            repo,
                            DIGEST_BRANCH,
                        )
                        return
                    commit_sha = tip
                    flushed_ids = set(flushed["ids"])
                    to_flush = [item for item in pending if item.id not in flushed_ids]
                    base_text = await gh.get_file_text(
                        repo, LEARNINGS_REPO_PATH, ref=DIGEST_BRANCH
                    )
            else:
                if flushed is None or flushed["pr"] != pr_number:
                    logger.warning(
                        "themis_digest_branch_conflict repo=%s branch=%s",
                        repo,
                        DIGEST_BRANCH,
                    )
                    return
                flushed_ids = set(flushed["ids"])
                to_flush = [item for item in pending if item.id not in flushed_ids]
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
                    repo,
                    LEARNINGS_REPO_PATH,
                    content=redact_outbound(content),
                    message=DIGEST_PR_TITLE,
                    branch=DIGEST_BRANCH,
                    sha=file_sha,
                )
            all_ids = sorted(flushed_ids | {item.id for item in to_flush})
            if pr_number is None:
                # Record before create_pr so a failed creation can be resumed.
                await self.store.record_flushed(repo, all_ids, None, sha=commit_sha)
                pr_number = await gh.create_pr(
                    repo,
                    title=DIGEST_PR_TITLE,
                    body=DIGEST_PR_BODY,
                    head=DIGEST_BRANCH,
                    base=default_branch,
                )
            await self.store.record_flushed(
                repo, all_ids, pr_number, sha=commit_sha
            )
            logger.info("themis_digest_flushed repo=%s count=%d", repo, len(to_flush))
        except (httpx.HTTPError, GitHubGraphQLError, OSError) as error:
            logger.warning("themis_digest_flush_failed repo=%s error=%s", repo, error)
