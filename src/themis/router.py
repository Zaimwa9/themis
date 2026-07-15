"""Webhook + trigger API endpoints: verify, parse, enqueue. No heavy work here."""

import json
import logging
import secrets
from typing import Any, Literal

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
        # A manual request needs its own job so that its running-status
        # reaction lands on the comment that triggered it. Re-deliveries of
        # the same webhook still share this key and remain deduplicated.
        if job.trigger_comment_id is not None:
            return f"review:{job.repo}#{job.pr_number}:comment:{job.trigger_comment_id}"
        return f"review:{job.repo}#{job.pr_number}"
    return f"discuss:{job.comment_id}"


def _enqueue(
    settings: Settings, queue: InMemoryJobQueue, slug: str, job: ReviewJob | DiscussJob
) -> bool:
    if isinstance(job, ReviewJob):
        async def run() -> None:
            await run_review_job(
                settings, slug, job.repo, job.pr_number, job.installation_id, job.auto,
                trigger_comment_id=job.trigger_comment_id,
                extra_context=job.extra_context,
            )
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
    return queue.enqueue(_job_id(job), run)


def _skip_ack(job: ReviewJob | DiscussJob) -> bool:
    # Unmentioned thread replies are relevance-checked by the worker (it may
    # not be a bot thread at all); acking here would falsely acknowledge
    # replies the bot ends up ignoring. The worker reacts once it confirms
    # relevance.
    return isinstance(job, DiscussJob) and job.kind == "thread" and not job.mentions_bot


async def _ack(settings: Settings, job: ReviewJob | DiscussJob, **target: int) -> None:
    """Best-effort eyes reaction on the trigger; never blocks enqueueing."""
    try:
        app_jwt = make_app_jwt(settings.gh_app_client_id, settings.gh_app_private_key_pem)
        async with httpx.AsyncClient(timeout=30) as auth_client:
            token = await get_installation_token(auth_client, job.installation_id, app_jwt)
        async with GitHubClient(token) as gh:
            await gh.add_reaction(job.repo, **target)
    except Exception as error:
        logger.warning("themis_ack_failed repo=%s error=%s", job.repo, error)


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
            enqueued = _enqueue(settings, queue, slug, job)
            logger.info(
                "themis_enqueued job=%s repo=%s pr=%s duplicate=%s",
                type(job).__name__, job.repo, job.pr_number, not enqueued,
            )
            if not _skip_ack(job):
                await _ack(settings, job, **_webhook_reaction_target(event, payload, job))
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
        enqueued = _enqueue(settings, queue, request.app.state.bot_slug, job)
        if not _skip_ack(job):
            target = (
                {"issue_comment_id": job.comment_id}
                if job.kind == "conversation"
                else {"review_comment_id": job.comment_id}
            )
            await _ack(settings, job, **target)
        return {"status": "queued" if enqueued else "duplicate"}

    return router
