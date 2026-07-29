"""Webhook + trigger API endpoints: verify, parse, enqueue. No heavy work here."""

import json
import logging
import secrets
from dataclasses import replace
from typing import Any, Literal, TypeVar

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from themis.config import Settings
from themis.events import DiscussJob, ReviewJob, parse_event
from themis.github.auth import (
    get_installation_token,
    get_repo_installation_id,
    make_app_jwt,
)
from themis.github.client import GitHubClient
from themis.queue import InMemoryJobQueue
from themis.security import verify_signature
from themis.review_service import run_discussion_job, run_review_job

logger = logging.getLogger(__name__)


class ReviewRequest(BaseModel):
    repo: str
    pr_number: int


class DiscussRequest(BaseModel):
    repo: str
    pr_number: int
    comment_id: int
    body: str
    kind: Literal["conversation", "thread"]
    in_reply_to_id: int | None = None
    mentions_bot: bool = True
    author_association: str = "NONE"
    author_login: str = ""


def _job_id(job: ReviewJob | DiscussJob) -> str:
    if isinstance(job, ReviewJob):
        # Every trigger for a PR shares one key, so at most one review of it
        # is ever in flight: two people mentioning the bot no longer buy two
        # full engine runs over identical code, and a push mid-review cannot
        # double-post alongside it under THEMIS_CONCURRENCY. Which of the
        # colliding requests is redundant is `_revision`'s call, not the id's.
        return f"review:{job.repo}#{job.pr_number}"
    return f"discuss:{job.comment_id}"


def _scope(job: ReviewJob) -> str:
    """Which kind of review a revision names.

    A delta re-review covers only the commits pushed since the last review and
    may decline to run at all (delta disabled, no prior review, no checkpoint
    key); a full review covers the PR. Two triggers on one commit are the same
    work only when they are the same kind of work, so an unqualified sha would
    let a queued delta swallow a mention asking for a full review - and answer
    it with nothing when the delta then declines."""
    return "delta" if job.delta else "sha"


def _revision(job: ReviewJob) -> str | None:
    """What this review would look at, or None when that cannot be told.

    Two triggers with the same revision produce the same review, so the queue
    keeps one and drops the other. Steered requests are never redundant - the
    context text is part of what gets reviewed - so they key on their own
    comment. An unresolved head falls back to the comment id, which still
    deduplicates webhook re-deliveries; None (no head, no comment) never
    matches anything and always queues.
    """
    if job.extra_context:
        return f"context:{job.trigger_comment_id}"
    if job.head_sha:
        return f"{_scope(job)}:{job.head_sha}"
    if job.trigger_comment_id is not None:
        return f"comment:{job.trigger_comment_id}"
    return None


def _enqueue(
    settings: Settings, queue: InMemoryJobQueue, slug: str, job: ReviewJob | DiscussJob
) -> bool:
    if isinstance(job, ReviewJob):
        async def run() -> None:
            reviewed = await run_review_job(
                settings, slug, job.repo, job.pr_number, job.installation_id, job.auto,
                trigger_comment_id=job.trigger_comment_id,
                extra_context=job.extra_context,
                delta=job.delta,
            )
            # The head resolved at trigger time is a guess about what this run
            # would cover; this is what it actually covered. Correcting it lets
            # the queue drop triggers - including one already waiting behind
            # this job - for a commit this review has now answered.
            if reviewed is not None:
                queue.reviewed(_job_id(job), f"{_scope(job)}:{reviewed}")
    else:
        async def run() -> None:
            await run_discussion_job(
                settings, slug, repo=job.repo, pr_number=job.pr_number,
                installation_id=job.installation_id, comment_id=job.comment_id,
                body=job.body, kind=job.kind, in_reply_to_id=job.in_reply_to_id,
                mentions_bot=job.mentions_bot,
                author_association=job.author_association,
                author_login=job.author_login,
            )
    # A trigger arriving during a running review must not vanish: the review's
    # summary records the sha it cloned, so commits pushed after that clone
    # would otherwise stay unreviewed until the next push, and a mention that
    # asked for something the running review is not doing would go unanswered.
    # Review jobs re-resolve the PR from GitHub when they start, so running one
    # late is always safe; the revision keeps the genuinely redundant ones out.
    if isinstance(job, ReviewJob):
        return queue.enqueue(_job_id(job), run, followup=True, revision=_revision(job))
    return queue.enqueue(_job_id(job), run)


def _skip_ack(job: ReviewJob | DiscussJob) -> bool:
    # Unmentioned thread replies are relevance-checked by the worker (it may
    # not be a bot thread at all); acking here would falsely acknowledge
    # replies the bot ends up ignoring. The worker reacts once it confirms
    # relevance. Same doctrine for delta candidates: most pushes produce no
    # re-review (no prior themis review, delta disabled), so the worker's
    # rocket reaction is the first visible signal.
    if isinstance(job, ReviewJob):
        return job.delta
    return job.kind == "thread" and not job.mentions_bot


def _repo_allowed(repo: str, allowlist: frozenset[str] | None) -> bool:
    """None allows everything. Entries match exactly, or as an ``owner/*``
    wildcard covering every repo under that owner."""
    if allowlist is None:
        return True
    if repo in allowlist:
        return True
    owner = repo.split("/", 1)[0]
    return f"{owner}/*" in allowlist


JobT = TypeVar("JobT", bound=ReviewJob | DiscussJob)


async def _prepare_trigger(
    settings: Settings, job: JobT, ack: dict[str, int] | None
) -> JobT:
    """Fill in what the payload did not carry and acknowledge the trigger.

    One installation token covers both: the eyes reaction on the trigger, and
    - for a review whose payload has no head sha (a mention, `/api/review`) -
    the PR head the dedup revision keys on. Everything here is best-effort and
    never blocks enqueueing: on failure the job keeps the head it arrived with
    (usually None), which degrades to per-comment dedup rather than dropping
    work.
    """
    needs_head = isinstance(job, ReviewJob) and job.head_sha is None
    if not needs_head and ack is None:
        return job
    try:
        app_jwt = make_app_jwt(settings.gh_app_client_id, settings.gh_app_private_key_pem)
        async with httpx.AsyncClient(timeout=30) as auth_client:
            token = await get_installation_token(auth_client, job.installation_id, app_jwt)
        async with GitHubClient(token) as gh:
            if needs_head:
                try:
                    pr = await gh.get_pr(job.repo, job.pr_number)
                    head = (pr.get("head") or {}).get("sha")
                    if head:
                        job = replace(job, head_sha=head)
                except Exception as error:
                    logger.warning(
                        "themis_head_sha_unresolved repo=%s pr=%s error=%s",
                        job.repo, job.pr_number, error,
                    )
            if ack is not None:
                await gh.add_reaction(job.repo, **ack)
    except Exception as error:
        logger.warning(
            "themis_trigger_prepare_failed repo=%s pr=%s error=%s",
            job.repo, job.pr_number, error,
        )
    return job


def _webhook_reaction_target(
    event: str, payload: dict[str, Any], job: ReviewJob | DiscussJob
) -> dict[str, int]:
    comment = payload.get("comment")
    if comment is not None:
        if event == "issue_comment":
            return {"issue_comment_id": comment["id"]}
        if event == "pull_request_review_comment":
            return {"review_comment_id": comment["id"]}
    return {"issue_number": job.pr_number}


def create_router(settings: Settings, queue: InMemoryJobQueue) -> APIRouter:
    router = APIRouter()

    if settings.webhook_enabled:

        @router.post("/webhook")
        async def webhook(request: Request) -> dict[str, str]:
            body = await request.body()
            signature = request.headers.get("x-hub-signature-256")
            if not verify_signature(body, settings.gh_webhook_secret or "", signature):
                logger.warning("themis_webhook_bad_signature")
                raise HTTPException(status_code=401, detail="invalid signature")
            slug: str = request.app.state.bot_slug
            event = request.headers.get("x-github-event", "")
            try:
                payload = json.loads(body)
                job = parse_event(event, payload, f"@{slug}")
            except (json.JSONDecodeError, KeyError) as error:
                logger.warning("themis_malformed_payload event=%s error=%s", event, error)
                return {"status": "ignored"}
            if job is None:
                logger.debug("themis_event_ignored event=%s action=%s", event, payload.get("action"))
                return {"status": "ignored"}
            if not _repo_allowed(job.repo, settings.repos):
                logger.info("themis_repo_not_allowlisted repo=%s", job.repo)
                return {"status": "ignored"}
            # Before enqueueing: the dedup revision needs the PR head, which a
            # comment payload does not carry.
            ack = None if _skip_ack(job) else _webhook_reaction_target(event, payload, job)
            job = await _prepare_trigger(settings, job, ack)
            enqueued = _enqueue(settings, queue, slug, job)
            logger.info(
                "themis_enqueued job=%s repo=%s pr=%s duplicate=%s",
                type(job).__name__, job.repo, job.pr_number, not enqueued,
            )
            return {"status": "queued" if enqueued else "duplicate"}

    def _require_api_token(authorization: str | None) -> None:
        if not settings.api_token:
            raise HTTPException(status_code=404)
        scheme, _, token = (authorization or "").partition(" ")
        token = token.strip()
        if scheme != "Bearer" or not token or not secrets.compare_digest(
            token.encode(), settings.api_token.encode()
        ):
            raise HTTPException(status_code=401, detail="invalid token")

    async def _resolve_installation(repo: str) -> int:
        app_jwt = make_app_jwt(settings.gh_app_client_id, settings.gh_app_private_key_pem)
        async with httpx.AsyncClient(timeout=30) as client:
            installation_id = await get_repo_installation_id(client, repo, app_jwt)
        if installation_id is None:
            raise HTTPException(status_code=403, detail="app not installed on repo")
        return installation_id

    @router.post("/api/review", status_code=202)
    async def api_review(
        request: Request, body: ReviewRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        _require_api_token(authorization)
        installation_id = await _resolve_installation(body.repo)
        job = ReviewJob(
            repo=body.repo, pr_number=body.pr_number,
            installation_id=installation_id, auto=False,
        )
        job = await _prepare_trigger(settings, job, None)
        enqueued = _enqueue(settings, queue, request.app.state.bot_slug, job)
        return {"status": "queued" if enqueued else "duplicate"}

    @router.post("/api/discuss", status_code=202)
    async def api_discuss(
        request: Request, body: DiscussRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        _require_api_token(authorization)
        installation_id = await _resolve_installation(body.repo)
        job = DiscussJob(
            repo=body.repo, pr_number=body.pr_number, installation_id=installation_id,
            comment_id=body.comment_id, body=body.body, kind=body.kind,
            in_reply_to_id=body.in_reply_to_id, mentions_bot=body.mentions_bot,
            author_association=body.author_association, author_login=body.author_login,
        )
        ack = None
        if not _skip_ack(job):
            ack = (
                {"issue_comment_id": job.comment_id}
                if job.kind == "conversation"
                else {"review_comment_id": job.comment_id}
            )
        await _prepare_trigger(settings, job, ack)
        enqueued = _enqueue(settings, queue, request.app.state.bot_slug, job)
        return {"status": "queued" if enqueued else "duplicate"}

    return router
