"""Webhook + trigger API routes."""

import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from themis.config import Settings
from themis.queue import InMemoryJobQueue
from themis.router import create_router


def make_settings(**overrides) -> Settings:
    defaults = dict(
        gh_app_client_id="Iv1.test",
        gh_app_private_key_pem="-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
        gh_webhook_secret="hush",
        webhook_enabled=True,
        api_token=None,
        repos=None,
        codex_sandbox="workspace-write",
        workspace_root=Path("/tmp/themis-test"),
        public_url=None,
        tunnel_api=None,
    )
    return Settings(**{**defaults, **overrides})


class RecordingQueue(InMemoryJobQueue):
    def __init__(self):
        super().__init__()
        self.enqueued: list[str] = []

    def enqueue(self, job_id, run):
        if job_id in self.enqueued:
            return False
        self.enqueued.append(job_id)
        return True


def make_client(settings=None):
    settings = settings or make_settings()
    queue = RecordingQueue()
    app = FastAPI()
    app.state.bot_slug = "test-reviewer"
    app.include_router(create_router(settings, queue))
    return TestClient(app), queue


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --- webhook payload builders -------------------------------------------------


def pr_opened_payload(repo: str = "acme/widgets", number: int = 5) -> dict:
    return {
        "action": "opened",
        "pull_request": {"number": number, "draft": False},
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


def test_webhook_ignores_disallowed_repo(monkeypatch):
    monkeypatch.setattr("themis.router._ack", AsyncMock())
    settings = make_settings(repos=frozenset({"acme/allowed"}))
    client, queue = make_client(settings)
    payload = json.dumps(
        {
            "action": "opened",
            "pull_request": {"number": 1, "draft": False},
            "repository": {"full_name": "acme/other"},
            "installation": {"id": 42},
            "sender": {"type": "User"},
        }
    ).encode()
    response = client.post(
        "/webhook",
        content=payload,
        headers={
            "x-hub-signature-256": sign("hush", payload),
            "x-github-event": "pull_request",
        },
    )
    assert response.json() == {"status": "ignored"}
    assert queue.enqueued == []


# --- webhook: enqueue + ack --------------------------------------------------


def test_webhook_pr_opened_enqueues_review_and_acks(monkeypatch):
    ack = AsyncMock()
    monkeypatch.setattr("themis.router._ack", ack)
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
    ack.assert_awaited_once()
    assert ack.await_args.kwargs == {"issue_number": 5}


def test_webhook_mention_review_command_enqueues_review_and_acks_issue_comment(monkeypatch):
    ack = AsyncMock()
    monkeypatch.setattr("themis.router._ack", ack)
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
    ack.assert_awaited_once()
    assert ack.await_args.kwargs == {"issue_comment_id": 501}


def test_webhook_review_thread_reply_without_mention_enqueues_discuss_no_ack(monkeypatch):
    ack = AsyncMock()
    monkeypatch.setattr("themis.router._ack", ack)
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
    ack.assert_not_awaited()


def test_webhook_duplicate_enqueue_returns_duplicate(monkeypatch):
    monkeypatch.setattr("themis.router._ack", AsyncMock())
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


def test_api_review_403_on_disallowed_repo():
    client, _ = make_client(
        make_settings(api_token="sekret", repos=frozenset({"acme/allowed"}))
    )
    response = client.post(
        "/api/review",
        json={"repo": "acme/other", "pr_number": 7},
        headers={"Authorization": "Bearer sekret"},
    )
    assert response.status_code == 403


def test_api_discuss_enqueues_and_acks(monkeypatch):
    ack = AsyncMock()
    monkeypatch.setattr("themis.router._ack", ack)
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
    ack.assert_awaited_once()
    assert ack.await_args.kwargs == {"issue_comment_id": 99}


def test_api_discuss_mentioned_thread_reply_acks_review_comment(monkeypatch):
    ack = AsyncMock()
    monkeypatch.setattr("themis.router._ack", ack)
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
    assert ack.await_args.kwargs == {"review_comment_id": 99}


def test_api_discuss_unmentioned_thread_reply_skips_ack(monkeypatch):
    ack = AsyncMock()
    monkeypatch.setattr("themis.router._ack", ack)
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
    ack.assert_not_awaited()
