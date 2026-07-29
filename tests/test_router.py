"""Webhook + trigger API routes."""

import asyncio
import hashlib
import hmac
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from themis.config import Settings
from themis.events import ReviewJob
from themis.queue import InMemoryJobQueue
from themis.router import _repo_allowed, create_router


def make_settings(**overrides) -> Settings:
    defaults = dict(
        gh_app_client_id="Iv1.test",
        gh_app_private_key_pem="-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
        gh_webhook_secret="hush",
        webhook_enabled=True,
        api_token=None,
        codex_sandbox="workspace-write",
        engine="codex",
        workspace_root=Path("/tmp/themis-test"),
        public_url=None,
        tunnel_api=None,
        agent_url="http://agent:8001",
        agent_token="agent-secret",
    )
    return Settings(**{**defaults, **overrides})


class RecordingQueue(InMemoryJobQueue):
    """Never-drained stand-in: every enqueued job stays "in flight", so the
    real queue's dedup rules apply to everything a test posts."""

    def __init__(self):
        super().__init__()
        self.enqueued: list[str] = []
        self.revisions: list[str | None] = []
        self.followups: list[bool] = []
        self.runs: list = []

    def enqueue(self, job_id, run, followup=False, revision=None):
        self.followups.append(followup)
        accepted = super().enqueue(job_id, run, followup=followup, revision=revision)
        if accepted:
            self.enqueued.append(job_id)
            self.revisions.append(revision)
            self.runs.append(run)
        return accepted


def prepared_trigger(monkeypatch, head_sha: str | None = None) -> list[dict | None]:
    """Replace the network side of `_prepare_trigger`: record the ack target
    each call asked for, and hand back the job carrying the PR head GitHub
    would have reported. Returns the recorded ack targets (None = no ack)."""
    acks: list[dict | None] = []

    async def prepare(settings, job, ack):
        acks.append(ack)
        if isinstance(job, ReviewJob) and job.head_sha is None and head_sha:
            return replace(job, head_sha=head_sha)
        return job

    monkeypatch.setattr("themis.router._prepare_trigger", prepare)
    return acks


def make_client(settings=None):
    settings = settings or make_settings()
    queue = RecordingQueue()
    app = FastAPI()
    app.state.bot_slug = "test-reviewer"
    app.include_router(create_router(settings, queue))
    return TestClient(app), queue


def _make_client_capturing(settings=None):
    """make_client variant that also retains the enqueued run callables."""
    settings = settings or make_settings()
    queue = RecordingQueue()
    runs = []
    original_enqueue = queue.enqueue

    def enqueue(job_id, run, followup=False, revision=None):
        accepted = original_enqueue(job_id, run, followup=followup, revision=revision)
        if accepted:
            runs.append(run)
        return accepted

    queue.enqueue = enqueue
    app = FastAPI()
    app.state.bot_slug = "test-reviewer"
    app.include_router(create_router(settings, queue))
    return TestClient(app), runs


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --- webhook payload builders -------------------------------------------------


def pr_opened_payload(
    repo: str = "acme/widgets", number: int = 5, head_sha: str = "deadbeef"
) -> dict:
    return {
        "action": "opened",
        "pull_request": {"number": number, "draft": False, "head": {"sha": head_sha}},
        "repository": {"full_name": repo},
        "installation": {"id": 42},
        "sender": {"type": "User"},
    }


def issue_comment_payload(
    repo: str = "acme/widgets", pr_number: int = 5, comment_id: int = 501, body: str = "hi"
) -> dict:
    return {
        "action": "created",
        "issue": {"number": pr_number, "pull_request": {"url": "https://x"}},
        "comment": {"id": comment_id, "body": body},
        "repository": {"full_name": repo},
        "installation": {"id": 42},
        "sender": {"type": "User"},
    }


def review_comment_payload(
    repo: str = "acme/widgets",
    pr_number: int = 5,
    comment_id: int = 601,
    body: str = "hi",
    in_reply_to: int | None = None,
) -> dict:
    comment: dict = {"id": comment_id, "body": body}
    if in_reply_to is not None:
        comment["in_reply_to_id"] = in_reply_to
    return {
        "action": "created",
        "pull_request": {"number": pr_number},
        "comment": comment,
        "repository": {"full_name": repo},
        "installation": {"id": 42},
        "sender": {"type": "User"},
    }


# --- webhook: signature ---------------------------------------------------


def test_webhook_missing_signature_401():
    client, queue = make_client()
    payload = json.dumps(pr_opened_payload()).encode()
    response = client.post(
        "/webhook", content=payload, headers={"x-github-event": "pull_request"}
    )
    assert response.status_code == 401
    assert queue.enqueued == []


def test_webhook_invalid_signature_401():
    client, queue = make_client()
    payload = json.dumps(pr_opened_payload()).encode()
    response = client.post(
        "/webhook",
        content=payload,
        headers={
            "x-hub-signature-256": sign("wrong-secret", payload),
            "x-github-event": "pull_request",
        },
    )
    assert response.status_code == 401
    assert queue.enqueued == []


# --- webhook: ignored paths -------------------------------------------------


def test_webhook_non_pr_issue_comment_ignored():
    client, queue = make_client()
    payload = json.dumps(
        {
            "action": "created",
            "issue": {"number": 5},  # no pull_request key: not a PR comment
            "comment": {"id": 501, "body": "@test-reviewer hi"},
            "repository": {"full_name": "acme/widgets"},
            "installation": {"id": 42},
            "sender": {"type": "User"},
        }
    ).encode()
    response = client.post(
        "/webhook",
        content=payload,
        headers={
            "x-hub-signature-256": sign("hush", payload),
            "x-github-event": "issue_comment",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert queue.enqueued == []


def test_webhook_malformed_json_ignored():
    client, queue = make_client()
    body = b"not json at all"
    response = client.post(
        "/webhook",
        content=body,
        headers={"x-hub-signature-256": sign("hush", body), "x-github-event": "pull_request"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert queue.enqueued == []


def test_webhook_missing_expected_fields_ignored():
    client, queue = make_client()
    payload = json.dumps(
        {
            "action": "opened",
            "installation": {"id": 42},
            "sender": {"type": "User"},
            "repository": {"full_name": "acme/widgets"},
            # "pull_request" key intentionally absent
        }
    ).encode()
    response = client.post(
        "/webhook",
        content=payload,
        headers={"x-hub-signature-256": sign("hush", payload), "x-github-event": "pull_request"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert queue.enqueued == []


def test_webhook_unknown_event_ignored():
    client, queue = make_client()
    payload = json.dumps({"repository": {"full_name": "acme/widgets"}}).encode()
    response = client.post(
        "/webhook",
        content=payload,
        headers={"x-hub-signature-256": sign("hush", payload), "x-github-event": "push"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert queue.enqueued == []


# --- webhook: enqueue + ack --------------------------------------------------


def test_webhook_pr_opened_enqueues_review_and_acks(monkeypatch):
    acks = prepared_trigger(monkeypatch)
    client, queue = make_client()
    payload = json.dumps(pr_opened_payload()).encode()
    response = client.post(
        "/webhook",
        content=payload,
        headers={
            "x-hub-signature-256": sign("hush", payload),
            "x-github-event": "pull_request",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    assert queue.enqueued == ["review:acme/widgets#5"]
    assert queue.revisions == ["sha:deadbeef"]
    assert acks == [{"issue_number": 5}]


def test_webhook_pr_synchronize_enqueues_followup_delta_without_ack(monkeypatch):
    # Delta candidates skip the eyes ack (most pushes trigger no review) and
    # ask the queue to keep the newest rejected push for a follow-up run.
    acks = prepared_trigger(monkeypatch)
    client, queue = make_client()
    payload = json.dumps({**pr_opened_payload(), "action": "synchronize"}).encode()
    response = client.post(
        "/webhook",
        content=payload,
        headers={
            "x-hub-signature-256": sign("hush", payload),
            "x-github-event": "pull_request",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    assert queue.enqueued == ["review:acme/widgets#5"]
    assert queue.followups == [True]
    assert acks == [None]


def test_webhook_mention_review_command_enqueues_review_and_acks_issue_comment(monkeypatch):
    acks = prepared_trigger(monkeypatch, head_sha="cafe1234")
    client, queue = make_client()
    payload = json.dumps(issue_comment_payload(body="@test-reviewer review")).encode()
    response = client.post(
        "/webhook",
        content=payload,
        headers={
            "x-hub-signature-256": sign("hush", payload),
            "x-github-event": "issue_comment",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    assert queue.enqueued == ["review:acme/widgets#5"]
    assert queue.revisions == ["sha:cafe1234"]
    assert acks == [{"issue_comment_id": 501}]


def test_webhook_review_thread_reply_without_mention_enqueues_discuss_no_ack(monkeypatch):
    acks = prepared_trigger(monkeypatch)
    client, queue = make_client()
    payload = json.dumps(review_comment_payload(body="continuing", in_reply_to=555)).encode()
    response = client.post(
        "/webhook",
        content=payload,
        headers={
            "x-hub-signature-256": sign("hush", payload),
            "x-github-event": "pull_request_review_comment",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    assert queue.enqueued == ["discuss:601"]
    assert acks == [None]


def test_webhook_duplicate_enqueue_returns_duplicate(monkeypatch):
    prepared_trigger(monkeypatch)
    client, queue = make_client()
    payload = json.dumps(pr_opened_payload()).encode()
    headers = {
        "x-hub-signature-256": sign("hush", payload),
        "x-github-event": "pull_request",
    }
    first = client.post("/webhook", content=payload, headers=headers)
    second = client.post("/webhook", content=payload, headers=headers)
    assert first.json() == {"status": "queued"}
    assert second.json() == {"status": "duplicate"}


def _post_mention(
    client, comment_id: int, body: str = "@test-reviewer review", association: str = "NONE"
):
    payload = issue_comment_payload(comment_id=comment_id, body=body)
    payload["comment"]["author_association"] = association
    payload = json.dumps(payload).encode()
    return client.post(
        "/webhook",
        content=payload,
        headers={
            "x-github-event": "issue_comment",
            "x-hub-signature-256": sign("hush", payload),
        },
    )


def test_webhook_second_mention_on_the_same_head_is_a_duplicate(monkeypatch):
    # Issue #77: two people mentioning the bot on the same PR used to buy two
    # full engine runs over byte-identical code.
    prepared_trigger(monkeypatch, head_sha="cafe1234")
    client, queue = make_client()

    assert _post_mention(client, 501).json() == {"status": "queued"}
    assert _post_mention(client, 502).json() == {"status": "duplicate"}
    assert queue.enqueued == ["review:acme/widgets#5"]
    assert queue.revisions == ["sha:cafe1234"]


def test_webhook_mention_after_a_push_reviews_the_new_head(monkeypatch):
    # ... while a mention naming code the queued review has not seen must
    # still run once that review frees the PR's slot.
    prepared_trigger(monkeypatch, head_sha="cafe1234")
    client, queue = make_client()
    assert _post_mention(client, 501).json() == {"status": "queued"}

    prepared_trigger(monkeypatch, head_sha="f00dfeed")
    assert _post_mention(client, 502).json() == {"status": "duplicate"}
    assert queue.enqueued == ["review:acme/widgets#5"]  # not a second parallel run
    assert set(queue._followups) == {"review:acme/widgets#5"}  # runs once the slot frees


def test_webhook_steered_mention_is_never_a_duplicate(monkeypatch):
    # The request text is part of what gets reviewed, so an owner asking for a
    # specific angle is not the review already in flight.
    prepared_trigger(monkeypatch, head_sha="cafe1234")
    client, queue = make_client()
    steer = "@test-reviewer review focus on the retry path and its timeouts"

    assert _post_mention(client, 501).json() == {"status": "queued"}
    steered = _post_mention(client, 502, body=steer, association="OWNER")
    assert steered.json() == {"status": "duplicate"}
    assert set(queue._followups) == {"review:acme/widgets#5"}  # stored, not dropped


def test_review_run_corrects_the_revision_to_what_it_reviewed(monkeypatch):
    # The trigger-time head is a guess: the author may push between the webhook
    # and the clone, and the worker reviews whatever it finds.
    prepared_trigger(monkeypatch, head_sha="cafe1234")
    monkeypatch.setattr(
        "themis.router.run_review_job", AsyncMock(return_value="f00dfeed")
    )
    client, queue = make_client()
    assert _post_mention(client, 501).json() == {"status": "queued"}
    assert queue.revisions == ["sha:cafe1234"]

    asyncio.run(queue.runs[0]())

    assert queue._active["review:acme/widgets#5"] == "sha:f00dfeed"


def test_review_run_that_posted_nothing_leaves_the_revision_alone(monkeypatch):
    # A skip (closed PR, disabled auto-review, title skip) is not coverage:
    # a duplicate waiting behind it must still get its chance.
    prepared_trigger(monkeypatch, head_sha="cafe1234")
    monkeypatch.setattr("themis.router.run_review_job", AsyncMock(return_value=None))
    client, queue = make_client()
    _post_mention(client, 501)

    asyncio.run(queue.runs[0]())

    assert queue._active["review:acme/widgets#5"] == "sha:cafe1234"


def test_webhook_unresolvable_head_falls_back_to_per_comment_dedup(monkeypatch):
    # GitHub unreachable when the trigger arrived: re-deliveries of one comment
    # still collapse, but distinct comments are no longer provably redundant.
    prepared_trigger(monkeypatch, head_sha=None)
    client, queue = make_client()

    assert _post_mention(client, 501).json() == {"status": "queued"}
    assert queue.revisions == ["comment:501"]
    assert _post_mention(client, 501).json() == {"status": "duplicate"}
    assert queue._followups == {}  # re-delivery dropped, not stored


def test_webhook_route_absent_when_disabled():
    client, _ = make_client(
        make_settings(webhook_enabled=False, gh_webhook_secret=None, api_token="sekret")
    )
    response = client.post("/webhook", content=b"{}")
    assert response.status_code in (404, 405)


# --- trigger API -------------------------------------------------------------


def test_api_routes_absent_without_token():
    client, _ = make_client(make_settings(api_token=None))
    response = client.post("/api/review", json={"repo": "a/b", "pr_number": 1})
    assert response.status_code == 404


def test_api_review_rejects_bad_token():
    client, _ = make_client(make_settings(api_token="sekret"))
    response = client.post(
        "/api/review",
        json={"repo": "a/b", "pr_number": 1},
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401


def test_api_review_rejects_schemeless_token():
    client, _ = make_client(make_settings(api_token="sekret"))
    response = client.post(
        "/api/review", json={"repo": "a/b", "pr_number": 1},
        headers={"Authorization": "sekret"},
    )
    assert response.status_code == 401


def test_api_review_non_ascii_token_is_401_not_500():
    client, _ = make_client(make_settings(api_token="sekret"))
    response = client.post(
        "/api/review", json={"repo": "a/b", "pr_number": 1},
        headers={"Authorization": "Bearer café".encode("latin-1")},
    )
    assert response.status_code == 401


def test_api_review_enqueues(monkeypatch):
    monkeypatch.setattr("themis.router.make_app_jwt", lambda client_id, pem: "jwt")
    monkeypatch.setattr(
        "themis.router.get_repo_installation_id", AsyncMock(return_value=42)
    )
    client, queue = make_client(make_settings(api_token="sekret"))
    response = client.post(
        "/api/review",
        json={"repo": "acme/widgets", "pr_number": 7},
        headers={"Authorization": "Bearer sekret"},
    )
    assert response.status_code == 202
    assert response.json() == {"status": "queued"}
    assert queue.enqueued == ["review:acme/widgets#7"]


def test_api_review_403_when_app_not_installed(monkeypatch):
    monkeypatch.setattr("themis.router.make_app_jwt", lambda client_id, pem: "jwt")
    monkeypatch.setattr(
        "themis.router.get_repo_installation_id", AsyncMock(return_value=None)
    )
    client, _ = make_client(make_settings(api_token="sekret"))
    response = client.post(
        "/api/review",
        json={"repo": "acme/widgets", "pr_number": 7},
        headers={"Authorization": "Bearer sekret"},
    )
    assert response.status_code == 403


def test_api_discuss_enqueues_and_acks(monkeypatch):
    acks = prepared_trigger(monkeypatch)
    monkeypatch.setattr("themis.router.make_app_jwt", lambda client_id, pem: "jwt")
    monkeypatch.setattr(
        "themis.router.get_repo_installation_id", AsyncMock(return_value=42)
    )
    client, queue = make_client(make_settings(api_token="sekret"))
    response = client.post(
        "/api/discuss",
        json={
            "repo": "acme/widgets",
            "pr_number": 7,
            "comment_id": 99,
            "body": "why this approach?",
            "kind": "conversation",
        },
        headers={"Authorization": "Bearer sekret"},
    )
    assert response.status_code == 202
    assert queue.enqueued == ["discuss:99"]
    assert acks == [{"issue_comment_id": 99}]


def test_api_discuss_mentioned_thread_reply_acks_review_comment(monkeypatch):
    acks = prepared_trigger(monkeypatch)
    monkeypatch.setattr("themis.router.make_app_jwt", lambda client_id, pem: "jwt")
    monkeypatch.setattr(
        "themis.router.get_repo_installation_id", AsyncMock(return_value=42)
    )
    client, queue = make_client(make_settings(api_token="sekret"))
    response = client.post(
        "/api/discuss",
        json={"repo": "acme/widgets", "pr_number": 7, "comment_id": 99,
              "body": "what about this line?", "kind": "thread",
              "in_reply_to_id": 55, "mentions_bot": True},
        headers={"Authorization": "Bearer sekret"},
    )
    assert response.status_code == 202
    assert queue.enqueued == ["discuss:99"]
    assert acks == [{"review_comment_id": 99}]


def test_api_discuss_unmentioned_thread_reply_skips_ack(monkeypatch):
    acks = prepared_trigger(monkeypatch)
    monkeypatch.setattr("themis.router.make_app_jwt", lambda client_id, pem: "jwt")
    monkeypatch.setattr(
        "themis.router.get_repo_installation_id", AsyncMock(return_value=42)
    )
    client, queue = make_client(make_settings(api_token="sekret"))
    response = client.post(
        "/api/discuss",
        json={
            "repo": "acme/widgets",
            "pr_number": 7,
            "comment_id": 99,
            "body": "hm",
            "kind": "thread",
            "in_reply_to_id": 55,
            "mentions_bot": False,
        },
        headers={"Authorization": "Bearer sekret"},
    )
    assert response.status_code == 202
    assert queue.enqueued == ["discuss:99"]
    assert acks == [None]


# --- author trust fields -----------------------------------------------------


def test_webhook_thread_reply_forwards_author_trust_to_discussion_job(monkeypatch):
    job = AsyncMock()
    monkeypatch.setattr("themis.router.run_discussion_job", job)
    prepared_trigger(monkeypatch)
    client, runs = _make_client_capturing()
    payload_dict = review_comment_payload(
        body="@test-reviewer remember: use the manager", in_reply_to=555
    )
    payload_dict["comment"]["author_association"] = "MEMBER"
    payload_dict["comment"]["user"] = {"login": "dev"}
    payload = json.dumps(payload_dict).encode()

    response = client.post(
        "/webhook",
        content=payload,
        headers={
            "x-hub-signature-256": sign("hush", payload),
            "x-github-event": "pull_request_review_comment",
        },
    )

    assert response.status_code == 200
    asyncio.run(runs[0]())
    assert job.await_args.kwargs["author_association"] == "MEMBER"
    assert job.await_args.kwargs["author_login"] == "dev"


def test_api_discuss_association_defaults_untrusted(monkeypatch):
    job = AsyncMock()
    monkeypatch.setattr("themis.router.run_discussion_job", job)
    prepared_trigger(monkeypatch)
    monkeypatch.setattr("themis.router.make_app_jwt", lambda client_id, pem: "jwt")
    monkeypatch.setattr(
        "themis.router.get_repo_installation_id", AsyncMock(return_value=42)
    )
    client, runs = _make_client_capturing(make_settings(api_token="tok"))

    response = client.post(
        "/api/discuss",
        json={"repo": "acme/widgets", "pr_number": 7, "comment_id": 1,
              "body": "hi", "kind": "conversation"},
        headers={"Authorization": "Bearer tok"},
    )

    assert response.status_code == 202
    asyncio.run(runs[0]())
    assert job.await_args.kwargs["author_association"] == "NONE"
    assert job.await_args.kwargs["author_login"] == ""


@pytest.mark.parametrize(
    "repo, allowlist, expected",
    [
        ("acme/widgets", None, True),
        ("acme/widgets", frozenset({"acme/widgets"}), True),
        ("acme/gadgets", frozenset({"acme/widgets"}), False),
        ("acme/gadgets", frozenset({"acme/*"}), True),
        ("other/gadgets", frozenset({"acme/*"}), False),
        ("acme/widgets", frozenset({"acme/*", "other/thing"}), True),
        ("acme/widgets", frozenset(), False),
    ],
)
def test_repo_allowed__various_allowlists__matches_exact_and_owner_wildcard(
    repo: str, allowlist: "frozenset[str] | None", expected: bool
) -> None:
    # Given / When / Then
    assert _repo_allowed(repo, allowlist) is expected
