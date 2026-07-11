import hashlib
import hmac
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from reviewbot.router import create_router

SECRET = "s3cret"
REPO = "Zaimwa9/bookia-v2"  # must match reviewbot.yaml


@pytest.fixture
def pool() -> AsyncMock:
    mock = AsyncMock()
    mock.enqueue_job.return_value = object()
    return mock


@pytest.fixture
def client(pool: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("REVIEWBOT_GH_APP_CLIENT_ID", "Iv1.abc")
    monkeypatch.setenv(
        "REVIEWBOT_GH_APP_PRIVATE_KEY",
        "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
    )
    monkeypatch.setenv("REVIEWBOT_GH_WEBHOOK_SECRET", SECRET)
    app = FastAPI()
    app.include_router(create_router(lambda: pool))
    return TestClient(app)


def _post(client: TestClient, event: str, payload: dict, secret: str = SECRET):
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/api/v1/reviewbot/webhook",
        content=body,
        headers={
            "x-hub-signature-256": signature,
            "x-github-event": event,
            "content-type": "application/json",
        },
    )


def _post_raw(client: TestClient, event: str, body: bytes, secret: str = SECRET):
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/api/v1/reviewbot/webhook",
        content=body,
        headers={
            "x-hub-signature-256": signature,
            "x-github-event": event,
            "content-type": "application/json",
        },
    )


def _pr_payload() -> dict:
    return {
        "action": "opened",
        "installation": {"id": 42},
        "sender": {"type": "User"},
        "repository": {"full_name": REPO},
        "pull_request": {"number": 7, "draft": False},
    }


def _comment_payload(body: str) -> dict:
    return {
        "action": "created",
        "installation": {"id": 42},
        "sender": {"type": "User"},
        "repository": {"full_name": REPO},
        "issue": {"number": 7, "pull_request": {"url": "https://x"}},
        "comment": {"id": 501, "body": body},
    }


def _review_comment_payload(body: str, in_reply_to: int | None = None) -> dict:
    comment: dict = {"id": 601, "body": body}
    if in_reply_to is not None:
        comment["in_reply_to_id"] = in_reply_to
    return {
        "action": "created",
        "installation": {"id": 42},
        "sender": {"type": "User"},
        "repository": {"full_name": REPO},
        "pull_request": {"number": 7},
        "comment": comment,
    }


class _FakeGitHubClient:
    instances: list["_FakeGitHubClient"] = []

    def __init__(self, token: str) -> None:
        self.token = token
        self.add_reaction = AsyncMock()
        _FakeGitHubClient.instances.append(self)

    async def __aenter__(self) -> "_FakeGitHubClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


@pytest.fixture
def gh_reaction(monkeypatch: pytest.MonkeyPatch) -> type[_FakeGitHubClient]:
    monkeypatch.setattr("reviewbot.router.make_app_jwt", lambda client_id, pem: "jwt")
    monkeypatch.setattr(
        "reviewbot.router.get_installation_token", AsyncMock(return_value="ghs_tok")
    )
    _FakeGitHubClient.instances = []
    monkeypatch.setattr("reviewbot.router.GitHubClient", _FakeGitHubClient)
    return _FakeGitHubClient


def test_webhook__pr_opened__enqueues_review(client, pool):
    response = _post(client, "pull_request", _pr_payload())

    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    name, *args = pool.enqueue_job.await_args.args
    assert name == "reviewbot_review_task"
    assert args == [REPO, 7, 42]
    assert pool.enqueue_job.await_args.kwargs["_job_id"] == f"reviewbot:review:{REPO}#7"


def test_webhook__mention_question__enqueues_discussion(client, pool):
    response = _post(client, "issue_comment", _comment_payload("@bookia-reviewer why?"))

    assert response.status_code == 200
    name, *args = pool.enqueue_job.await_args.args
    assert name == "reviewbot_discussion_task"
    assert args[:4] == [REPO, 7, 42, 501]
    assert pool.enqueue_job.await_args.kwargs["_job_id"] == "reviewbot:discuss:501"


def test_webhook__bad_signature__401(client, pool):
    response = _post(client, "pull_request", _pr_payload(), secret="wrong")

    assert response.status_code == 401
    pool.enqueue_job.assert_not_awaited()


def test_webhook__irrelevant_event__ignored_with_200(client, pool):
    response = _post(client, "push", {"repository": {"full_name": REPO}})

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    pool.enqueue_job.assert_not_awaited()


def test_webhook__duplicate_job__reports_duplicate(client, pool):
    pool.enqueue_job.return_value = None  # arq returns None when job_id exists

    response = _post(client, "pull_request", _pr_payload())

    assert response.json() == {"status": "duplicate"}


def test_webhook__missing_credentials__503(pool, monkeypatch):
    for var in (
        "REVIEWBOT_GH_APP_CLIENT_ID",
        "REVIEWBOT_GH_APP_PRIVATE_KEY",
        "REVIEWBOT_GH_WEBHOOK_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    app = FastAPI()
    app.include_router(create_router(lambda: pool))

    response = TestClient(app).post("/api/v1/reviewbot/webhook", content=b"{}")

    assert response.status_code == 503


def test_webhook__malformed_private_key__503(pool, monkeypatch):
    monkeypatch.setenv("REVIEWBOT_GH_APP_CLIENT_ID", "Iv1.abc")
    monkeypatch.setenv("REVIEWBOT_GH_APP_PRIVATE_KEY", "not-pem-not-base64!!")
    monkeypatch.setenv("REVIEWBOT_GH_WEBHOOK_SECRET", SECRET)
    app = FastAPI()
    app.include_router(create_router(lambda: pool))

    response = TestClient(app).post("/api/v1/reviewbot/webhook", content=b"{}")

    assert response.status_code == 503
    assert response.json()["detail"] == "reviewbot misconfigured"


def test_main_app__mounts_reviewbot_webhook():
    from book_ia.api.app import app as main_app

    paths = {getattr(route, "path", "") for route in main_app.routes}
    assert "/api/v1/reviewbot/webhook" in paths


def test_webhook__garbage_json_body__ignored_with_200(client, pool):
    response = _post_raw(client, "pull_request", b"not json at all")

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    pool.enqueue_job.assert_not_awaited()


def test_webhook__valid_json_missing_expected_fields__ignored_with_200(client, pool):
    payload = {
        "action": "opened",
        "installation": {"id": 42},
        "sender": {"type": "User"},
        "repository": {"full_name": REPO},
        # "pull_request" key intentionally absent
    }

    response = _post(client, "pull_request", payload)

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    pool.enqueue_job.assert_not_awaited()


def test_webhook__mention_comment__adds_eyes_reaction_to_comment(client, pool, gh_reaction):
    _post(client, "issue_comment", _comment_payload("@bookia-reviewer why?"))

    reaction = gh_reaction.instances[0].add_reaction
    reaction.assert_awaited_once_with(REPO, issue_comment_id=501)


def test_webhook__pr_opened__adds_eyes_reaction_to_pr(client, pool, gh_reaction):
    _post(client, "pull_request", _pr_payload())

    reaction = gh_reaction.instances[0].add_reaction
    reaction.assert_awaited_once_with(REPO, issue_number=7)


def test_webhook__duplicate_enqueue__still_adds_reaction(client, pool, gh_reaction):
    pool.enqueue_job.return_value = None

    response = _post(client, "pull_request", _pr_payload())

    assert response.json() == {"status": "duplicate"}
    gh_reaction.instances[0].add_reaction.assert_awaited_once_with(REPO, issue_number=7)


def test_webhook__reaction_mint_fails__still_returns_queued(client, pool, monkeypatch):
    monkeypatch.setattr("reviewbot.router.make_app_jwt", lambda client_id, pem: "jwt")

    async def _boom(*args: object, **kwargs: object) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr("reviewbot.router.get_installation_token", _boom)

    response = _post(client, "pull_request", _pr_payload())

    assert response.status_code == 200
    assert response.json() == {"status": "queued"}


def test_webhook__irrelevant_event__no_reaction_attempted(client, pool, gh_reaction):
    _post(client, "push", {"repository": {"full_name": REPO}})

    assert gh_reaction.instances == []


def test_webhook__unmentioned_thread_reply__enqueued_without_reaction(client, pool, gh_reaction):
    response = _post(
        client,
        "pull_request_review_comment",
        _review_comment_payload("continuing", in_reply_to=555),
    )

    assert response.json() == {"status": "queued"}
    name, *args = pool.enqueue_job.await_args.args
    assert name == "reviewbot_discussion_task"
    assert gh_reaction.instances == []


def test_webhook__mentioned_thread_reply__still_adds_reaction(client, pool, gh_reaction):
    _post(
        client,
        "pull_request_review_comment",
        _review_comment_payload("@bookia-reviewer continuing", in_reply_to=555),
    )

    reaction = gh_reaction.instances[0].add_reaction
    reaction.assert_awaited_once_with(REPO, review_comment_id=601)
