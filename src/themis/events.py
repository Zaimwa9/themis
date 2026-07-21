"""Parse GitHub webhook payloads into themis jobs."""

import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

_PR_ACTIONS = {"opened", "ready_for_review"}

# Comment authors whose `review <context>` text may steer the review prompt.
# Anyone allowed to comment can still trigger a review; only these roles can
# shape it — the context text becomes engine instructions.
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


@dataclass(frozen=True)
class ReviewJob:
    repo: str
    pr_number: int
    installation_id: int
    auto: bool
    # Issue comment that asked for the review; None for auto-reviews.
    trigger_comment_id: int | None = None
    # Optional request text supplied after the `review` command.
    extra_context: str | None = None


@dataclass(frozen=True)
class DiscussJob:
    repo: str
    pr_number: int
    installation_id: int
    comment_id: int
    body: str
    kind: Literal["conversation", "thread"]
    in_reply_to_id: int | None
    mentions_bot: bool
    author_association: str = "NONE"
    author_login: str = ""


def parse_event(
    event: str, payload: dict[str, Any], mention: str
) -> ReviewJob | DiscussJob | None:
    # Bot senders (Dependabot etc.) never trigger an auto-review of their own PRs/comments.
    if payload.get("sender", {}).get("type") == "Bot":
        return None
    if event == "pull_request":
        return _parse_pull_request(payload)
    if event == "issue_comment":
        return _parse_issue_comment(payload, mention)
    if event == "pull_request_review_comment":
        return _parse_review_comment(payload, mention)
    return None


def _parse_pull_request(payload: dict[str, Any]) -> ReviewJob | None:
    if payload.get("action") not in _PR_ACTIONS:
        return None
    pr = payload["pull_request"]
    # Automatic triggers skip drafts. Explicit requests (mention commands,
    # /api/review) carry auto=False and bypass draft status downstream.
    if pr.get("draft"):
        return None
    return ReviewJob(
        repo=payload["repository"]["full_name"],
        pr_number=pr["number"],
        installation_id=payload["installation"]["id"],
        auto=True,
    )


def _find_mention(body: str, mention: str) -> re.Match[str] | None:
    """Locate the mention in body, case-insensitively, on both word boundaries.

    `@test-reviewer` matches `@Test-Reviewer` but not `@test-reviewer-v2`
    or `foo@test-reviewer` (email-like local part).
    """
    return re.search(
        r"(?<![\w-])" + re.escape(mention) + r"(?![\w-])", body, re.IGNORECASE
    )


def _strip_mention(body: str, mention: str) -> str | None:
    """Text around the first mention, or None if the bot is not mentioned."""
    match = _find_mention(body, mention)
    if match is None:
        return None
    return (body[: match.start()] + body[match.end() :]).strip()


def _parse_issue_comment(
    payload: dict[str, Any], mention: str
) -> ReviewJob | DiscussJob | None:
    if payload.get("action") != "created":
        return None
    issue = payload.get("issue", {})
    if "pull_request" not in issue:
        return None
    body = payload.get("comment", {}).get("body", "")
    rest = _strip_mention(body, mention)
    if not rest:
        return None
    repo = payload["repository"]["full_name"]
    pr_number = issue["number"]
    installation_id = payload["installation"]["id"]
    command, *remainder = rest.split(maxsplit=1)
    if command.lower().strip(".!?") == "review":
        extra_context = remainder[0].strip() if remainder else ""
        association = payload["comment"].get("author_association", "")
        if extra_context and association not in TRUSTED_ASSOCIATIONS:
            logger.info(
                "themis_extra_context_dropped association=%s", association or "unknown"
            )
            extra_context = ""
        return ReviewJob(
            repo=repo, pr_number=pr_number, installation_id=installation_id,
            auto=False, trigger_comment_id=payload["comment"]["id"],
            extra_context=extra_context if len(extra_context) > 20 else None,
        )
    return DiscussJob(
        repo=repo,
        pr_number=pr_number,
        installation_id=installation_id,
        comment_id=payload["comment"]["id"],
        body=body,
        kind="conversation",
        in_reply_to_id=None,
        mentions_bot=True,
        author_association=payload["comment"].get("author_association") or "NONE",
        author_login=(payload["comment"].get("user") or {}).get("login", ""),
    )


def _parse_review_comment(payload: dict[str, Any], mention: str) -> DiscussJob | None:
    if payload.get("action") != "created":
        return None
    comment = payload["comment"]
    body = comment.get("body", "")
    mentions = _find_mention(body, mention) is not None
    in_reply_to = comment.get("in_reply_to_id")
    # Replies inside a thread are candidates even without a mention: the worker
    # checks whether the bot authored the thread and drops the job otherwise.
    if not mentions and in_reply_to is None:
        return None
    return DiscussJob(
        repo=payload["repository"]["full_name"],
        pr_number=payload["pull_request"]["number"],
        installation_id=payload["installation"]["id"],
        comment_id=comment["id"],
        body=body,
        kind="thread",
        in_reply_to_id=in_reply_to,
        mentions_bot=mentions,
        author_association=comment.get("author_association") or "NONE",
        author_login=(comment.get("user") or {}).get("login", ""),
    )
