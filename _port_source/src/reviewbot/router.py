"""Reviewbot webhook endpoint: verify, parse, enqueue. No heavy work here."""

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request

from reviewbot.config import get_config, load_credentials
from reviewbot.events import DiscussJob, ReviewJob, parse_event
from reviewbot.github.auth import get_installation_token, make_app_jwt
from reviewbot.github.client import GitHubClient
from reviewbot.security import verify_signature

logger = logging.getLogger(__name__)


def _reaction_target(
    event: str, payload: dict[str, Any], job: ReviewJob | DiscussJob
) -> dict[str, int]:
    comment = payload.get("comment")
    if comment is not None:
        if event == "issue_comment":
            return {"issue_comment_id": comment["id"]}
        if event == "pull_request_review_comment":
            return {"review_comment_id": comment["id"]}
    return {"issue_number": job.pr_number}


def create_router(get_pool: Callable[[], Any]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/reviewbot", tags=["reviewbot"])

    @router.post("/webhook")
    async def webhook(request: Request) -> dict[str, str]:
        try:
            credentials = load_credentials()
        except ValueError as error:
            raise HTTPException(status_code=503, detail="reviewbot misconfigured") from error
        if credentials is None:
            raise HTTPException(status_code=503, detail="reviewbot not configured")
        body = await request.body()
        signature = request.headers.get("x-hub-signature-256")
        if not verify_signature(body, credentials.webhook_secret, signature):
            raise HTTPException(status_code=401, detail="invalid signature")
        config = get_config()
        event = request.headers.get("x-github-event", "")
        try:
            payload = json.loads(body)
            job = parse_event(event, payload, config.bot.mention, config.repo)
        except (json.JSONDecodeError, KeyError) as error:
            logger.warning("reviewbot_malformed_payload event=%s error=%s", event, error)
            return {"status": "ignored"}
        if job is None:
            return {"status": "ignored"}
        pool = get_pool()
        if pool is None:
            raise HTTPException(status_code=503, detail="queue unavailable")
        if isinstance(job, ReviewJob):
            enqueued = await pool.enqueue_job(
                "reviewbot_review_task",
                job.repo, job.pr_number, job.installation_id,
                _job_id=f"reviewbot:review:{job.repo}#{job.pr_number}",
            )
        else:
            enqueued = await pool.enqueue_job(
                "reviewbot_discussion_task",
                job.repo, job.pr_number, job.installation_id,
                job.comment_id, job.body, job.kind,
                job.in_reply_to_id, job.mentions_bot,
                _job_id=f"reviewbot:discuss:{job.comment_id}",
            )
        logger.info("reviewbot_enqueued job=%s duplicate=%s", type(job).__name__, enqueued is None)
        # Unmentioned thread replies are relevance-checked by the worker (it may
        # not be a bot thread at all); reacting here would falsely ack replies
        # the bot ends up ignoring. The worker reacts once it confirms relevance.
        skip_reaction = (
            isinstance(job, DiscussJob) and job.kind == "thread" and not job.mentions_bot
        )
        if not skip_reaction:
            try:
                app_jwt = make_app_jwt(credentials.client_id, credentials.private_key_pem)
                async with httpx.AsyncClient(timeout=30) as auth_client:
                    token = await get_installation_token(auth_client, job.installation_id, app_jwt)
                async with GitHubClient(token) as gh:
                    await gh.add_reaction(job.repo, **_reaction_target(event, payload, job))
            except Exception as error:
                logger.warning("reviewbot_reaction_failed event=%s error=%s", event, error)
        return {"status": "queued" if enqueued else "duplicate"}

    return router
