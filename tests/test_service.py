import asyncio
import dataclasses
import logging
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from themis.config import Settings, parse_repo_config
from themis.engines import ENGINE_NAMES, EngineError, EngineQuotaError
from themis.github.client import GitHubGraphQLError
from themis.learnings import Learning, PendingStore, to_jsonl
from themis.service import (
    DEFAULT_MODELS,
    DIGEST_BRANCH,
    DIGEST_PR_TITLE,
    LEARNING_FOOTER,
    ReviewService,
    _ENGINE_AUTH_HINTS,
    api_changed_paths,
    git_changed_lines,
    git_head_sha,
    run_review_job,
)
from themis.output import MAX_BODY_LEN, OUTPUT_DIR, OutputError

pytestmark = pytest.mark.asyncio


class FakeEngine:
    def __init__(self, run_fn, available: bool = True, name: str = "codex"):
        self.name = name
        self._run_fn = run_fn
        self._available = available

    def available(self) -> bool:
        return self._available

    async def run(self, **kwargs) -> str:
        return await self._run_fn(**kwargs)


def _resolver(run_fn, available: bool = True, seen: list | None = None):
    def resolve_engine(name: str):
        if seen is not None:
            seen.append(name)
        return FakeEngine(run_fn, available=available, name=name)
    return resolve_engine


REPO = "acme/widgets"
BOT_LOGIN = "test-reviewer[bot]"
BOT_MENTION = "@test-reviewer"


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


@pytest.fixture
def gh() -> AsyncMock:
    mock = AsyncMock()
    mock.get_pr.return_value = {
        "number": 7, "title": "Fix", "body": "desc", "state": "open", "draft": False,
        "user": {"login": "dev"}, "head": {"sha": "abc123"}, "base": {"ref": "main"},
    }
    mock.list_review_threads.return_value = []
    mock.get_ci_snapshot.return_value = {
        "state": "none", "head_sha": "abc123", "checks": [],
        "unavailable_sources": [],
    }
    # Repo config: None -> RepoConfig defaults; tests set this to a yaml string
    # to exercise per-repo behavior config.
    mock.get_file_text.return_value = None
    return mock


@pytest.fixture
def cleanup_calls() -> list[Path]:
    return []


def _review_agent():
    async def agent(*, prompt, workspace, model, effort, timeout, web_access) -> str:
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "summary.md").write_text("#### AI Review\nfine")
        (out / "actions.json").write_text(json.dumps({
            "findings": [{"path": "a.py", "line": 3, "body": "bug"}],
            "resolve_thread_ids": ["T_1"],
            "replies": [{"in_reply_to": 11, "body": "answer"}],
        }))
        return "ok"
    return agent


def _reply_agent(text: str = "here is the answer"):
    async def agent(*, prompt, workspace, model, effort, timeout, web_access) -> str:
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "reply.md").write_text(text)
        return "ok"
    return agent


@pytest.fixture
def service(gh: AsyncMock, tmp_path: Path, cleanup_calls: list[Path]) -> ReviewService:
    async def prepare(**kwargs) -> Path:
        workspace = tmp_path / "ws"
        workspace.mkdir(exist_ok=True)
        return workspace

    async def changed_paths(gh_client, repo: str, pr_number: int) -> set[str]:
        return {"a.py"}

    async def changed_lines(workspace: Path, base_ref: str) -> set[tuple[str, int, str]]:
        return {("a.py", 3, "RIGHT")}

    async def head_sha(workspace: Path) -> str:
        return "abc123"

    return ReviewService(
        settings=make_settings(workspace_root=tmp_path / "root"),
        bot_login=BOT_LOGIN,
        mention=BOT_MENTION,
        get_token=AsyncMock(return_value="ghs_x"),
        make_client=lambda token: gh,
        prepare=prepare,
        cleanup=cleanup_calls.append,
        resolve_engine=_resolver(_review_agent()),
        changed_paths=changed_paths,
        changed_lines=changed_lines,
        head_sha=head_sha,
    )


async def test_review__no_output_written__codex_stdout_tail_logged(service, gh, caplog):
    # codex exited 0 but wrote nothing; its stdout is the only diagnostic.
    async def chatty(**kwargs):
        return "bwrap: Creating new namespace failed: Permission denied"

    service.resolve_engine = _resolver(chatty)

    with pytest.raises(OutputError):
        await service.review(REPO, 7, 42, auto=True)

    assert "bwrap: Creating new namespace failed" in caplog.text


async def test_review__malformed_output_secret_redacted_from_logs_and_error(
    service, gh, caplog, monkeypatch
):
    secret = "sk-ant-oat01-output-secret-value"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", secret)

    async def malformed(*, workspace, **kwargs):
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "summary.md").write_text("#### AI Review\nsummary")
        (out / "actions.json").write_text(json.dumps({
            "findings": [{"body": secret}],
        }))
        return "agent completed"

    service.resolve_engine = _resolver(malformed)

    with pytest.raises(OutputError) as exc_info:
        await service.review(REPO, 7, 42, auto=True)

    assert secret not in caplog.text
    assert secret not in str(exc_info.value)
    assert "[redacted]" in str(exc_info.value)


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.github.com/x")
    return httpx.HTTPStatusError(
        str(status), request=request, response=httpx.Response(status, request=request)
    )


async def test_review__auto__adds_rocket_reaction_on_pr(service, gh):
    await service.review(REPO, 7, 42, auto=True)

    gh.add_reaction.assert_awaited_once_with(REPO, issue_number=7, content="rocket")


async def test_review__comment_triggered__adds_rocket_on_trigger_comment(service, gh):
    await service.review(REPO, 7, 42, auto=False, trigger_comment_id=501)

    gh.add_reaction.assert_awaited_once_with(
        REPO, issue_comment_id=501, content="rocket"
    )


async def test_review__skipped_pr__no_rocket_reaction(service, gh):
    gh.get_pr.return_value = {"state": "closed", "draft": False}

    await service.review(REPO, 7, 42, auto=True)

    gh.add_reaction.assert_not_awaited()


async def test_review__rocket_reaction_fails__review_still_completes(service, gh):
    gh.add_reaction.side_effect = _http_error(500)

    await service.review(REPO, 7, 42, auto=True)

    gh.post_summary_comment.assert_awaited_once()


def _bot_thread() -> dict:
    return {"id": "T_1", "isResolved": False, "path": "a.py", "line": 3,
            "comments": {"nodes": [
                {"author": {"login": "test-reviewer"}, "body": "bug", "databaseId": 11},
                {"author": {"login": "dev"}, "body": "why?", "databaseId": 12},
            ]}}


def _human_thread() -> dict:
    return {"id": "T_2", "isResolved": False, "path": "b.py", "line": 5,
            "comments": {"nodes": [
                {"author": {"login": "dev"}, "body": "note", "databaseId": 21},
                {"author": {"login": "dev"}, "body": "reply", "databaseId": 22},
            ]}}


async def test_review__happy_path__posts_review_replies_resolves_summary(service, gh):
    gh.list_review_threads.return_value = [_bot_thread()]

    await service.review(REPO, 7, 42, auto=True)

    gh.post_review.assert_awaited_once_with(
        REPO, 7, commit_sha="abc123",
        comments=[{"path": "a.py", "line": 3, "side": "RIGHT", "body": "bug"}],
    )
    gh.post_reply.assert_awaited_once_with(REPO, 7, in_reply_to=11, body="answer")
    gh.resolve_thread.assert_awaited_once_with("T_1")
    gh.post_summary_comment.assert_awaited_once()


async def test_review__draft_pr__does_nothing(service, gh):
    gh.get_pr.return_value = {**gh.get_pr.return_value, "draft": True}

    await service.review(REPO, 7, 42, auto=True)

    gh.post_summary_comment.assert_not_awaited()
    gh.post_review.assert_not_awaited()


async def test_review__closed_pr__does_nothing(service, gh):
    gh.get_pr.return_value = {**gh.get_pr.return_value, "state": "closed"}

    await service.review(REPO, 7, 42, auto=True)

    gh.post_summary_comment.assert_not_awaited()


async def test_review__workspace_always_cleaned_up(service, gh, cleanup_calls):
    await service.review(REPO, 7, 42, auto=True)

    assert len(cleanup_calls) == 1


async def test_review__agent_raises__workspace_still_cleaned_up(service, gh, cleanup_calls):
    async def dead(**kwargs):
        raise EngineError("dead")
    service.resolve_engine = _resolver(dead)

    with pytest.raises(EngineError):
        await service.review(REPO, 7, 42, auto=True)

    assert len(cleanup_calls) == 1


async def test_review__unexpected_error__propagates_and_cleans_up(service, gh, cleanup_calls):
    async def broken(**kwargs):
        raise RuntimeError("boom")
    service.resolve_engine = _resolver(broken)

    with pytest.raises(RuntimeError):
        await service.review(REPO, 7, 42, auto=True)

    assert len(cleanup_calls) == 1
    gh.post_issue_comment.assert_not_awaited()


async def test_review__quota_error__posts_quota_comment_and_stops(service, gh):
    async def quota_agent(**kwargs):
        raise EngineQuotaError("usage limit reached")
    service.resolve_engine = _resolver(quota_agent)

    await service.review(REPO, 7, 42, auto=True)

    gh.post_issue_comment.assert_awaited_once()
    body = gh.post_issue_comment.await_args.args[2]
    assert "limit reached" in body.lower()
    assert "review skipped" in body.lower()
    gh.post_review.assert_not_awaited()
    gh.post_summary_comment.assert_not_awaited()


async def test_review__flaky_agent__retries_then_succeeds(service, gh):
    calls = {"n": 0}
    good_agent = _review_agent()

    async def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise EngineError("transient")
        return await good_agent(**kwargs)

    service.resolve_engine = _resolver(flaky)

    await service.review(REPO, 7, 42, auto=True)

    assert calls["n"] == 2
    gh.post_review.assert_awaited_once()


async def test_review__ci_snapshot__written_once_before_agent_even_with_retry(
    service, gh
):
    snapshot = {
        "state": "failed", "head_sha": "abc123",
        "checks": [{"type": "check_run", "name": "tests", "status": "completed",
                    "conclusion": "failure", "details_url": None}],
        "unavailable_sources": [],
    }
    gh.get_ci_snapshot.return_value = snapshot
    calls = 0

    async def agent(*, workspace, **kwargs):
        nonlocal calls
        calls += 1
        assert json.loads((workspace / ".review-input/checks.json").read_text()) == snapshot
        if calls == 1:
            raise EngineError("retry")
        return await _review_agent()(workspace=workspace, **kwargs)

    service.resolve_engine = _resolver(agent)

    await service.review(REPO, 7, 42, auto=True)

    assert calls == 2
    gh.get_ci_snapshot.assert_awaited_once_with(REPO, "abc123")


async def test_review__ci_snapshot_failure__writes_unavailable_and_continues(
    service, gh
):
    gh.get_ci_snapshot.side_effect = _http_error(403)

    async def agent(*, workspace, **kwargs):
        snapshot = json.loads((workspace / ".review-input/checks.json").read_text())
        assert snapshot["state"] == "unavailable"
        assert snapshot["head_sha"] == "abc123"
        return await _review_agent()(workspace=workspace, **kwargs)

    service.resolve_engine = _resolver(agent)

    await service.review(REPO, 7, 42, auto=True)

    gh.post_summary_comment.assert_awaited_once()


async def test_review__agent_always_fails__failure_comment_and_raises(service, gh):
    async def dead(**kwargs):
        raise EngineError("dead")
    service.resolve_engine = _resolver(dead)

    with pytest.raises(EngineError):
        await service.review(REPO, 7, 42, auto=True)

    gh.post_issue_comment.assert_awaited_once()
    assert "failed" in gh.post_issue_comment.await_args.args[2].lower()
    gh.post_review.assert_not_awaited()


async def test_review__no_findings__summary_only(service, gh):
    async def clean_agent(*, workspace, **kwargs):
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "summary.md").write_text("#### AI Review\nclean")
        return "ok"
    service.resolve_engine = _resolver(clean_agent)

    await service.review(REPO, 7, 42, auto=True)

    gh.post_review.assert_not_awaited()
    gh.post_summary_comment.assert_awaited_once()


async def test_review__finding_outside_diff__dropped_and_noted_in_summary(service, gh):
    async def agent(*, workspace, **kwargs):
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "summary.md").write_text("#### AI Review\nfine")
        (out / "actions.json").write_text(json.dumps({
            "findings": [
                {"path": "a.py", "line": 3, "body": "bug"},
                {"path": "b.py", "line": 9, "body": "stale anchor"},
            ],
        }))
        return "ok"
    service.resolve_engine = _resolver(agent)

    await service.review(REPO, 7, 42, auto=True)

    comments = gh.post_review.await_args.kwargs["comments"]
    assert [c["path"] for c in comments] == ["a.py"]
    summary = gh.post_summary_comment.await_args.args[2]
    assert "b.py:9" in summary
    assert "1" in summary


async def test_review__all_findings_outside_diff__no_inline_review(service, gh):
    async def agent(*, workspace, **kwargs):
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "summary.md").write_text("#### AI Review\nfine")
        (out / "actions.json").write_text(json.dumps({
            "findings": [{"path": "b.py", "line": 9, "body": "stale anchor"}],
        }))
        return "ok"
    service.resolve_engine = _resolver(agent)

    await service.review(REPO, 7, 42, auto=True)

    gh.post_review.assert_not_awaited()
    summary = gh.post_summary_comment.await_args.args[2]
    assert "b.py:9" in summary


async def test_review__finding_on_unchanged_line_of_changed_file__dropped(service, gh):
    async def agent(*, workspace, **kwargs):
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "summary.md").write_text("#### AI Review\nfine")
        (out / "actions.json").write_text(json.dumps({
            "findings": [{"path": "a.py", "line": 4, "body": "not a changed line"}],
        }))
        return "ok"
    service.resolve_engine = _resolver(agent)

    await service.review(REPO, 7, 42, auto=True)

    gh.post_review.assert_not_awaited()
    summary = gh.post_summary_comment.await_args.args[2]
    assert "a.py:4" in summary
    assert "anchored outside the diff" in summary


async def test_review__diff_paths_unavailable__findings_posted_unfiltered(service, gh):
    async def no_paths(gh_client, repo: str, pr_number: int) -> None:
        return None
    service.changed_paths = no_paths

    await service.review(REPO, 7, 42, auto=True)

    gh.post_review.assert_awaited_once()


async def test_review__resolve_only_bot_authored_threads(service, gh):
    gh.list_review_threads.return_value = [_bot_thread(), _human_thread()]

    async def agent(*, workspace, **kwargs):
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "summary.md").write_text("#### AI Review\nfine")
        (out / "actions.json").write_text(json.dumps({
            "resolve_thread_ids": ["T_1", "T_2", "T_unknown"],
        }))
        return "ok"
    service.resolve_engine = _resolver(agent)

    await service.review(REPO, 7, 42, auto=True)

    gh.resolve_thread.assert_awaited_once_with("T_1")


async def test_review__inline_post_422__findings_folded_into_summary(service, gh):
    gh.list_review_threads.return_value = [_bot_thread()]
    gh.post_review.side_effect = _http_error(422)

    await service.review(REPO, 7, 42, auto=True)

    body = gh.post_summary_comment.await_args.args[2]
    assert "a.py:3" in body
    gh.post_reply.assert_awaited_once_with(REPO, 7, in_reply_to=11, body="answer")
    gh.resolve_thread.assert_awaited_once_with("T_1")


async def test_review__inline_post_non_422__raises(service, gh, cleanup_calls):
    gh.post_review.side_effect = _http_error(500)

    with pytest.raises(httpx.HTTPStatusError):
        await service.review(REPO, 7, 42, auto=True)

    gh.post_summary_comment.assert_not_awaited()
    assert len(cleanup_calls) == 1


async def test_review__oversized_summary_with_422_fallback__truncated_before_upsert(service, gh):
    gh.list_review_threads.return_value = [_bot_thread()]
    gh.post_review.side_effect = _http_error(422)
    big_summary = "#### AI Review\n" + ("x" * (MAX_BODY_LEN - 100))

    async def agent(*, workspace, **kwargs):
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "summary.md").write_text(big_summary)
        (out / "actions.json").write_text(json.dumps({
            "findings": [{"path": "a.py", "line": 3, "body": "b" * 600}],
        }))
        return "ok"
    service.resolve_engine = _resolver(agent)

    await service.review(REPO, 7, 42, auto=True)

    body = gh.post_summary_comment.await_args.args[2]
    assert len(body) <= 65536
    assert "summary truncated" in body.lower()


async def test_review__normal_summary__passes_through_unmodified(service, gh):
    gh.list_review_threads.return_value = [_bot_thread()]

    await service.review(REPO, 7, 42, auto=True)

    body = gh.post_summary_comment.await_args.args[2]
    assert "truncated" not in body.lower()


async def test_review__reply_post_fails__summary_still_upserted(service, gh):
    gh.list_review_threads.return_value = [_bot_thread()]
    gh.post_reply.side_effect = _http_error(404)

    await service.review(REPO, 7, 42, auto=True)

    gh.resolve_thread.assert_awaited_once_with("T_1")
    gh.post_summary_comment.assert_awaited_once()


async def test_review__resolve_fails__summary_still_upserted(service, gh):
    gh.list_review_threads.return_value = [_bot_thread()]
    gh.resolve_thread.side_effect = GitHubGraphQLError([{"message": "gone"}])

    await service.review(REPO, 7, 42, auto=True)

    gh.post_summary_comment.assert_awaited_once()


async def test_review__posting_uses_fresh_token(service, tmp_path):
    clients: dict[str, AsyncMock] = {}

    def make_client(token: str) -> AsyncMock:
        mock = AsyncMock()
        mock.get_pr.return_value = {
            "number": 7, "title": "Fix", "body": "desc", "state": "open",
            "draft": False, "user": {"login": "dev"},
            "head": {"sha": "abc123"}, "base": {"ref": "main"},
        }
        mock.list_review_threads.return_value = []
        mock.get_file_text.return_value = None
        clients[token] = mock
        return mock

    service.get_token = AsyncMock(side_effect=["t1", "t-ci", "t2"])
    service.make_client = make_client

    await service.review(REPO, 7, 42, auto=True)

    clients["t1"].post_review.assert_not_awaited()
    clients["t-ci"].get_ci_snapshot.assert_awaited_once_with(REPO, "abc123")
    clients["t1"].post_summary_comment.assert_not_awaited()
    clients["t2"].post_review.assert_awaited_once()
    clients["t2"].post_summary_comment.assert_awaited_once()


async def test_review__quota_comment_uses_fresh_token(service):
    clients: dict[str, AsyncMock] = {}

    def make_client(token: str) -> AsyncMock:
        mock = AsyncMock()
        mock.get_pr.return_value = {
            "number": 7, "title": "Fix", "body": "desc", "state": "open",
            "draft": False, "user": {"login": "dev"},
            "head": {"sha": "abc123"}, "base": {"ref": "main"},
        }
        mock.list_review_threads.return_value = []
        mock.get_file_text.return_value = None
        clients[token] = mock
        return mock

    service.get_token = AsyncMock(side_effect=["t1", "t-ci", "t2"])
    service.make_client = make_client

    async def quota_agent(**kwargs):
        raise EngineQuotaError("usage limit reached")
    service.resolve_engine = _resolver(quota_agent)

    await service.review(REPO, 7, 42, auto=True)

    # the stale original client (t1) is never used to post; the quota comment
    # goes out on a freshly minted token (t2)
    clients["t1"].post_issue_comment.assert_not_awaited()
    clients["t2"].post_issue_comment.assert_awaited_once()


async def test_review__failure_comment_post_fails__still_raises_last_error(service, gh):
    async def dead(**kwargs):
        raise EngineError("real failure")
    service.resolve_engine = _resolver(dead)
    # a 401 on the courtesy comment must not mask the real failure
    gh.post_issue_comment.side_effect = _http_error(401)

    with pytest.raises(EngineError):
        await service.review(REPO, 7, 42, auto=True)


async def test_review__commit_sha_from_workspace_head(service, gh):
    async def head_sha(workspace: Path) -> str:
        return "workspace_sha"
    service.head_sha = head_sha

    await service.review(REPO, 7, 42, auto=True)

    assert gh.post_review.await_args.kwargs["commit_sha"] == "workspace_sha"


async def test_review__workspace_head_unavailable__falls_back_to_pr_sha(service, gh):
    async def head_sha(workspace: Path) -> None:
        return None
    service.head_sha = head_sha

    await service.review(REPO, 7, 42, auto=True)

    assert gh.post_review.await_args.kwargs["commit_sha"] == "abc123"


async def test_review__stale_output_from_failed_attempt_not_reused(service, gh):
    calls = {"n": 0}

    async def agent(*, workspace, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            out = workspace / OUTPUT_DIR
            out.mkdir(exist_ok=True)
            (out / "summary.md").write_text("#### AI Review\nstale")
            (out / "actions.json").write_text(json.dumps({
                "findings": [{"path": "a.py", "line": 3, "body": "stale"}],
            }))
            raise EngineError("died after writing output")
        return "ok"  # attempt 2 writes nothing

    service.resolve_engine = _resolver(agent)

    with pytest.raises(OutputError):
        await service.review(REPO, 7, 42, auto=True)

    gh.post_review.assert_not_awaited()
    gh.post_summary_comment.assert_not_awaited()
    gh.post_issue_comment.assert_awaited_once()


async def test_review__inputs_written_for_agent(service, gh, tmp_path):
    seen = {}

    async def spy_agent(*, workspace, **kwargs):
        seen["pr"] = json.loads((workspace / ".review-input" / "pr.json").read_text())
        seen["threads"] = json.loads(
            (workspace / ".review-input" / "threads.json").read_text()
        )
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "summary.md").write_text("#### AI Review\nclean")
        return "ok"

    gh.list_review_threads.return_value = [_bot_thread()]
    service.resolve_engine = _resolver(spy_agent)

    await service.review(REPO, 7, 42, auto=True)

    assert seen["pr"]["number"] == 7
    assert seen["pr"]["base_ref"] == "main"
    assert seen["threads"][0]["id"] == "T_1"


async def test_auto_review_disabled_skips(service, gh):
    """With .themis/config.yaml setting triggers.auto_review=false, an auto
    job exits before cloning; a manual job still reviews."""
    gh.get_file_text.return_value = "triggers:\n  auto_review: false\n"
    original_prepare = service.prepare
    prepare_calls: list[dict] = []

    async def recording_prepare(**kwargs):
        prepare_calls.append(kwargs)
        return await original_prepare(**kwargs)
    service.prepare = recording_prepare

    await service.review(REPO, 7, 42, auto=True)
    assert prepare_calls == []
    gh.add_reaction.assert_not_awaited()

    await service.review(REPO, 7, 42, auto=False)
    assert len(prepare_calls) == 1


async def test_repo_config_drives_clone_depth(service, gh):
    """clone_depth from .themis/config.yaml reaches prepare_workspace."""
    gh.get_file_text.return_value = "limits:\n  clone_depth: 7\n"
    original_prepare = service.prepare
    prepare_calls: list[dict] = []

    async def recording_prepare(**kwargs):
        prepare_calls.append(kwargs)
        return await original_prepare(**kwargs)
    service.prepare = recording_prepare

    await service.review(REPO, 7, 42, auto=True)

    assert prepare_calls[0]["depth"] == 7


async def test_repo_config_fetch_failure__review_completes_on_defaults(service, gh):
    """A dead .themis/config.yaml read (network/API failure) must never block
    the review; it falls back to RepoConfig defaults."""
    gh.get_file_text.side_effect = _http_error(500)
    original_prepare = service.prepare
    prepare_calls: list[dict] = []

    async def recording_prepare(**kwargs):
        prepare_calls.append(kwargs)
        return await original_prepare(**kwargs)
    service.prepare = recording_prepare

    await service.review(REPO, 7, 42, auto=True)

    assert len(prepare_calls) == 1
    assert prepare_calls[0]["depth"] == 50  # RepoConfig default clone_depth
    gh.post_summary_comment.assert_awaited_once()


async def test_default_repo_config__used_when_repo_file_absent(service, gh):
    """With no .themis/config.yaml in the target repo, the instance-level
    THEMIS_DEFAULT_REPO_CONFIG drives behavior (here: auto_review off)."""
    service.settings = dataclasses.replace(
        service.settings, default_repo_config="triggers:\n  auto_review: false\n"
    )
    original_prepare = service.prepare
    prepare_calls: list[dict] = []

    async def recording_prepare(**kwargs):
        prepare_calls.append(kwargs)
        return await original_prepare(**kwargs)
    service.prepare = recording_prepare

    await service.review(REPO, 7, 42, auto=True)
    assert prepare_calls == []

    await service.review(REPO, 7, 42, auto=False)
    assert len(prepare_calls) == 1


async def test_default_repo_config__repo_file_wins(service, gh):
    """A .themis/config.yaml in the target repo replaces the instance default
    entirely; no per-key merge between the two."""
    service.settings = dataclasses.replace(
        service.settings, default_repo_config="limits:\n  clone_depth: 9\n"
    )
    gh.get_file_text.return_value = "limits:\n  clone_depth: 7\n"
    original_prepare = service.prepare
    prepare_calls: list[dict] = []

    async def recording_prepare(**kwargs):
        prepare_calls.append(kwargs)
        return await original_prepare(**kwargs)
    service.prepare = recording_prepare

    await service.review(REPO, 7, 42, auto=True)

    assert prepare_calls[0]["depth"] == 7


async def test_default_repo_config__used_on_fetch_failure(service, gh):
    """When the repo config read dies, the instance default is a better
    fallback than hardcoded RepoConfig defaults."""
    service.settings = dataclasses.replace(
        service.settings, default_repo_config="limits:\n  clone_depth: 9\n"
    )
    gh.get_file_text.side_effect = _http_error(500)
    original_prepare = service.prepare
    prepare_calls: list[dict] = []

    async def recording_prepare(**kwargs):
        prepare_calls.append(kwargs)
        return await original_prepare(**kwargs)
    service.prepare = recording_prepare

    await service.review(REPO, 7, 42, auto=True)

    assert prepare_calls[0]["depth"] == 9


async def test_discuss__repo_config_drives_clone_depth(service, gh):
    """clone_depth from .themis/config.yaml reaches prepare_workspace in discuss()."""
    gh.get_file_text.return_value = "limits:\n  clone_depth: 7\n"
    service.resolve_engine = _resolver(_reply_agent())
    original_prepare = service.prepare
    prepare_calls: list[dict] = []

    async def recording_prepare(**kwargs):
        prepare_calls.append(kwargs)
        return await original_prepare(**kwargs)
    service.prepare = recording_prepare

    await service.discuss(
        repo=REPO, pr_number=7, installation_id=42, comment_id=501,
        body="@test-reviewer why?", kind="conversation",
        in_reply_to_id=None, mentions_bot=True,
    )

    assert prepare_calls[0]["depth"] == 7


async def test_discuss__conversation__posts_issue_comment(service, gh):
    service.resolve_engine = _resolver(_reply_agent())

    await service.discuss(
        repo=REPO, pr_number=7, installation_id=42, comment_id=501,
        body="@test-reviewer why?", kind="conversation",
        in_reply_to_id=None, mentions_bot=True,
    )

    gh.post_issue_comment.assert_awaited_once_with(REPO, 7, "here is the answer")
    gh.add_reaction.assert_not_awaited()


async def test_discuss__reply_in_bot_thread_without_mention__adds_reaction_and_replies(
    service, gh
):
    gh.list_review_threads.return_value = [_bot_thread()]
    service.resolve_engine = _resolver(_reply_agent())

    await service.discuss(
        repo=REPO, pr_number=7, installation_id=42, comment_id=12,
        body="I disagree", kind="thread", in_reply_to_id=11, mentions_bot=False,
    )

    gh.add_reaction.assert_awaited_once_with(REPO, review_comment_id=12)
    gh.post_reply.assert_awaited_once_with(REPO, 7, in_reply_to=11, body="here is the answer")


async def test_discuss__reply_in_human_thread_without_mention__skipped(service, gh):
    gh.list_review_threads.return_value = [_human_thread()]
    agent_calls = []

    async def recording_agent(**kwargs):
        agent_calls.append(kwargs)
        raise AssertionError("agent must not run")
    service.resolve_engine = _resolver(recording_agent)

    await service.discuss(
        repo=REPO, pr_number=7, installation_id=42, comment_id=22,
        body="just chatting", kind="thread", in_reply_to_id=21, mentions_bot=False,
    )

    assert agent_calls == []
    gh.add_reaction.assert_not_awaited()
    gh.post_reply.assert_not_awaited()


async def test_discuss__mentioned_in_human_thread__answers(service, gh):
    gh.list_review_threads.return_value = [_human_thread()]
    service.resolve_engine = _resolver(_reply_agent())

    await service.discuss(
        repo=REPO, pr_number=7, installation_id=42, comment_id=22,
        body="@test-reviewer is this ok?", kind="thread",
        in_reply_to_id=21, mentions_bot=True,
    )

    gh.add_reaction.assert_not_awaited()
    gh.post_reply.assert_awaited_once_with(REPO, 7, in_reply_to=21, body="here is the answer")


async def test_discuss__reaction_fails__reply_still_posted(service, gh):
    gh.list_review_threads.return_value = [_bot_thread()]
    gh.add_reaction.side_effect = _http_error(500)
    service.resolve_engine = _resolver(_reply_agent())

    await service.discuss(
        repo=REPO, pr_number=7, installation_id=42, comment_id=12,
        body="I disagree", kind="thread", in_reply_to_id=11, mentions_bot=False,
    )

    gh.post_reply.assert_awaited_once_with(REPO, 7, in_reply_to=11, body="here is the answer")


async def test_discuss__long_thread_bot_root_outside_tail__proceeds(service):
    # A 100+ comment thread authored by the bot: the reply being answered sits
    # on the SECOND comment page. Through the real client every page is
    # fetched, so an unmentioned reply must still be answered (a windowed
    # comment list would silently drop it).
    from themis.github.client import GitHubClient

    root = {"author": {"login": "test-reviewer"}, "body": "bug",
            "databaseId": 11, "createdAt": "2026-01-01T00:00:00Z"}
    fillers = [{"author": {"login": "dev"}, "body": f"c{i}", "databaseId": 1000 + i,
                "createdAt": "2026-01-02T00:00:00Z"} for i in range(99)]
    reply = {"author": {"login": "dev"}, "body": "I still disagree",
             "databaseId": 149, "createdAt": "2026-01-03T00:00:00Z"}
    captured = {"replied": None}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/graphql":
            if "reviewThreads" in json.loads(request.content)["query"]:
                return httpx.Response(200, json={"data": {"repository": {"pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [{"id": "T_1", "isResolved": False, "path": "a.py",
                                   "line": 3, "resolvedBy": None,
                                   "comments": {
                                       "pageInfo": {"hasNextPage": True,
                                                    "endCursor": "CC_1"},
                                       "nodes": [root, *fillers]}}],
                    }}}}})
            return httpx.Response(200, json={"data": {"node": {"comments": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [reply],
            }}}})
        if path == "/repos/acme/widgets/pulls/7":
            return httpx.Response(200, json={
                "number": 7, "state": "open", "draft": False, "user": {"login": "dev"},
                "head": {"sha": "abc123"}, "base": {"ref": "main"}})
        if path == "/repos/acme/widgets/contents/.themis/config.yaml":
            return httpx.Response(404)
        if path.endswith("/replies"):
            captured["replied"] = path
            return httpx.Response(201, json={"id": 999})
        if path.endswith("/reactions"):
            return httpx.Response(201, json={"id": 1, "content": "eyes"})
        raise AssertionError(f"unexpected request: {path}")

    service.make_client = lambda token: GitHubClient(
        token, transport=httpx.MockTransport(handler)
    )
    service.resolve_engine = _resolver(_reply_agent())

    await service.discuss(
        repo=REPO, pr_number=7, installation_id=42, comment_id=149,
        body="I still disagree", kind="thread", in_reply_to_id=11, mentions_bot=False,
    )

    assert captured["replied"] == "/repos/acme/widgets/pulls/7/comments/11/replies"


async def test_find_thread__matches_by_root_comment_id():
    from themis.service import _find_thread

    thread = _bot_thread()  # root comment databaseId 11

    assert _find_thread([thread], {11, None}) is thread


async def test_discuss__agent_raises__workspace_still_cleaned_up(service, gh, cleanup_calls):
    async def dead(**kwargs):
        raise EngineError("dead")
    service.resolve_engine = _resolver(dead)

    with pytest.raises(EngineError):
        await service.discuss(
            repo=REPO, pr_number=7, installation_id=42, comment_id=501,
            body="@test-reviewer why?", kind="conversation",
            in_reply_to_id=None, mentions_bot=True,
        )

    assert len(cleanup_calls) == 1


async def test_discuss__quota_error__posts_reply_skipped_comment(service, gh):
    async def quota_agent(**kwargs):
        raise EngineQuotaError("usage limit reached")
    service.resolve_engine = _resolver(quota_agent)

    await service.discuss(
        repo=REPO, pr_number=7, installation_id=42, comment_id=501,
        body="@test-reviewer why?", kind="conversation",
        in_reply_to_id=None, mentions_bot=True,
    )

    gh.post_issue_comment.assert_awaited_once()
    body = gh.post_issue_comment.await_args.args[2]
    assert "limit reached" in body.lower()
    assert "reply skipped" in body.lower()
    gh.post_reply.assert_not_awaited()


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(cwd),
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


async def test_api_changed_paths__returns_changed_file_set():
    gh = AsyncMock()
    gh.list_pr_files.return_value = ["a.py", "b.py"]

    assert await api_changed_paths(gh, REPO, 7) == {"a.py", "b.py"}


async def test_api_changed_paths__http_error__returns_none():
    gh = AsyncMock()
    gh.list_pr_files.side_effect = _http_error(500)

    assert await api_changed_paths(gh, REPO, 7) is None


async def test_api_changed_paths__http_error__filter_skipped(service, gh):
    # A dead API read must fail open: findings post unfiltered rather than
    # silently dropping the whole review.
    gh.list_pr_files.side_effect = _http_error(500)
    service.changed_paths = api_changed_paths

    await service.review(REPO, 7, 42, auto=True)

    gh.post_review.assert_awaited_once()


async def test_git_head_sha__returns_workspace_head(tmp_path):
    _run_git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.py").write_text("base\n")
    _run_git(tmp_path, "add", "a.py")
    _run_git(tmp_path, "commit", "-q", "-m", "base")
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True,
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"},
    ).stdout.strip()

    assert await git_head_sha(tmp_path) == expected


async def test_git_changed_lines__returns_exact_left_and_right_anchors(tmp_path):
    _run_git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.py").write_text("one\nold\nthree\n")
    unusual_path = 'tab\tquote"backslash\\.py'
    (tmp_path / unusual_path).write_text("old\n")
    _run_git(tmp_path, "add", "a.py")
    _run_git(tmp_path, "add", unusual_path)
    _run_git(tmp_path, "commit", "-q", "-m", "base")
    _run_git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    (tmp_path / "a.py").write_text("one\nnew\nthree\nfour\n")
    (tmp_path / unusual_path).write_text("new\n")
    _run_git(tmp_path, "add", "a.py")
    _run_git(tmp_path, "add", unusual_path)
    _run_git(tmp_path, "commit", "-q", "-m", "head")

    anchors = await git_changed_lines(tmp_path, "main")

    assert anchors == {
        ("a.py", 2, "LEFT"),
        ("a.py", 2, "RIGHT"),
        ("a.py", 4, "RIGHT"),
        (unusual_path, 1, "LEFT"),
        (unusual_path, 1, "RIGHT"),
    }


async def test_git_changed_lines__missing_merge_base__fails_open(tmp_path):
    _run_git(tmp_path, "init", "-q", "-b", "main")
    assert await git_changed_lines(tmp_path, "missing") is None


async def test_git_head_sha__git_failure__returns_none(tmp_path):
    assert await git_head_sha(tmp_path) is None


def _cancelling_service(tmp_path: Path, gh: AsyncMock) -> ReviewService:
    return ReviewService(
        settings=make_settings(workspace_root=tmp_path / "nope"),
        bot_login=BOT_LOGIN,
        mention=BOT_MENTION,
        get_token=AsyncMock(return_value="ghs_fresh"),
        make_client=lambda token: gh,
        prepare=AsyncMock(),
        cleanup=lambda p: None,
        resolve_engine=_resolver(AsyncMock()),
    )


async def test_run_review_job__cancelled__posts_comment_and_reraises(
    tmp_path, gh, monkeypatch
):
    service = _cancelling_service(tmp_path, gh)

    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError
    service.review = cancel
    monkeypatch.setattr("themis.service.build_service", lambda *a, **kw: service)

    with pytest.raises(asyncio.CancelledError):
        await run_review_job(make_settings(), "test-reviewer", REPO, 7, 42, True)

    gh.post_issue_comment.assert_awaited_once()
    body = gh.post_issue_comment.await_args.args[2]
    assert "cancelled" in body.lower()
    assert service.mention in body


async def test_run_review_job__real_task_cancel__posts_comment_and_reraises(
    tmp_path, gh, monkeypatch
):
    # Cancel the running task for real (not a manually-raised error) so the
    # shielded-child-task await path is genuinely exercised.
    service = _cancelling_service(tmp_path, gh)
    started = asyncio.Event()

    async def blocking(*args, **kwargs):
        started.set()
        await asyncio.sleep(30)
    service.review = blocking
    monkeypatch.setattr("themis.service.build_service", lambda *a, **kw: service)

    task = asyncio.ensure_future(
        run_review_job(make_settings(), "test-reviewer", REPO, 7, 42, True)
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    gh.post_issue_comment.assert_awaited_once()
    assert "cancelled" in gh.post_issue_comment.await_args.args[2].lower()


async def test_run_review_job__cancelled__comment_failure_still_reraises(
    tmp_path, gh, monkeypatch
):
    gh.post_issue_comment.side_effect = _http_error(401)
    service = _cancelling_service(tmp_path, gh)

    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError
    service.review = cancel
    monkeypatch.setattr("themis.service.build_service", lambda *a, **kw: service)

    with pytest.raises(asyncio.CancelledError):
        await run_review_job(make_settings(), "test-reviewer", REPO, 7, 42, True)


# --- engine resolution, availability gate, redaction --------------------------


async def test_review__repo_engine_override_wins(service, gh):
    gh.get_file_text.return_value = "engine: claude\n"
    seen: list[str] = []
    service.resolve_engine = _resolver(_review_agent(), seen=seen)

    await service.review(REPO, 7, 42, auto=True)

    assert seen == ["claude"]


async def test_review__instance_default_engine_when_repo_silent(service, gh):
    seen: list[str] = []
    service.resolve_engine = _resolver(_review_agent(), seen=seen)

    await service.review(REPO, 7, 42, auto=True)

    assert seen == ["codex"]


async def test_review__engine_unavailable__courtesy_comment_no_clone(service, gh):
    original_prepare = service.prepare
    prepare_calls: list[dict] = []

    async def recording_prepare(**kwargs):
        prepare_calls.append(kwargs)
        return await original_prepare(**kwargs)
    service.prepare = recording_prepare
    service.resolve_engine = _resolver(_review_agent(), available=False)

    await service.review(REPO, 7, 42, auto=True)

    assert prepare_calls == []  # never cloned
    gh.add_reaction.assert_not_awaited()
    gh.post_issue_comment.assert_awaited_once()
    body = gh.post_issue_comment.await_args.args[2]
    assert "credentials" in body
    gh.post_summary_comment.assert_not_awaited()


async def test_review__model_default_per_engine(service, gh):
    gh.get_file_text.return_value = "engine: claude\n"
    seen_model: dict = {}

    async def spy(**kwargs):
        seen_model.update(kwargs)
        return await _review_agent()(**kwargs)

    service.resolve_engine = _resolver(spy)

    await service.review(REPO, 7, 42, auto=True)

    assert seen_model["model"] == "claude-opus-4-6[1m]"


async def test_review__explicit_model_passthrough(service, gh):
    gh.get_file_text.return_value = "engine: claude\nmodel:\n  name: claude-sonnet-5\n"
    seen_model: dict = {}

    async def spy(**kwargs):
        seen_model.update(kwargs)
        return await _review_agent()(**kwargs)

    service.resolve_engine = _resolver(spy)

    await service.review(REPO, 7, 42, auto=True)

    assert seen_model["model"] == "claude-sonnet-5"


async def test_review__web_access_flows_to_engine(service, gh):
    gh.get_file_text.return_value = "web_access: true\n"
    seen: dict = {}

    async def spy(**kwargs):
        seen.update(kwargs)
        return await _review_agent()(**kwargs)

    service.resolve_engine = _resolver(spy)

    await service.review(REPO, 7, 42, auto=True)

    assert seen["web_access"] is True


async def test_review__default__web_access_false_flows_to_engine(service, gh):
    seen: dict = {}

    async def spy(**kwargs):
        seen.update(kwargs)
        return await _review_agent()(**kwargs)

    service.resolve_engine = _resolver(spy)

    await service.review(REPO, 7, 42, auto=True)

    assert seen["web_access"] is False


async def test_review__secret_in_summary_redacted(service, gh, monkeypatch):
    monkeypatch.setenv("THEMIS_API_TOKEN", "api-token-value")

    async def leaky(*, workspace, **kwargs):
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "summary.md").write_text("#### AI Review\nleak api-token-value here")
        return "ok"

    service.resolve_engine = _resolver(leaky)

    await service.review(REPO, 7, 42, auto=True)

    body = gh.post_summary_comment.await_args.args[2]
    assert "api-token-value" not in body
    assert "[redacted]" in body


async def test_discuss__reply_redacted(service, gh, monkeypatch):
    monkeypatch.setenv("THEMIS_API_TOKEN", "api-token-value")
    service.resolve_engine = _resolver(_reply_agent("answer with api-token-value"))

    await service.discuss(
        repo=REPO, pr_number=7, installation_id=42, comment_id=7,
        body="question", kind="conversation", in_reply_to_id=None, mentions_bot=True,
    )

    gh.post_issue_comment.assert_awaited_once()
    body = gh.post_issue_comment.await_args.args[2]
    assert "api-token-value" not in body
    assert "[redacted]" in body


async def test_discuss__engine_unavailable__courtesy_comment_no_clone(service, gh):
    original_prepare = service.prepare
    prepare_calls: list[dict] = []

    async def recording_prepare(**kwargs):
        prepare_calls.append(kwargs)
        return await original_prepare(**kwargs)
    service.prepare = recording_prepare
    service.resolve_engine = _resolver(_reply_agent(), available=False)

    await service.discuss(
        repo=REPO, pr_number=7, installation_id=42, comment_id=501,
        body="@test-reviewer why?", kind="conversation",
        in_reply_to_id=None, mentions_bot=True,
    )

    assert prepare_calls == []
    gh.post_issue_comment.assert_awaited_once()
    body = gh.post_issue_comment.await_args.args[2]
    assert "credentials" in body
    gh.post_reply.assert_not_awaited()


def test_engine_maps_cover_all_engine_names():
    # A registered engine without a default model or auth hint is a KeyError
    # at review time; keep the three maps in lockstep.
    assert set(DEFAULT_MODELS) == set(ENGINE_NAMES)
    assert set(_ENGINE_AUTH_HINTS) == set(ENGINE_NAMES)


LEARNING = Learning(
    id="lrn-aaaaaaaa", text="Prefer the manager method.", paths=("a.py",),
    learnt_from="dev", pr=3, created_at="2026-07-10T00:00:00+00:00",
)

LEARNINGS_YAML_OFF = "learnings:\n  enabled: false\n"


def _config_and_learnings(config_text=None, learnings_text=None):
    """get_file_text side effect: .themis/config.yaml then .themis/learnings.jsonl."""
    async def get_file_text(repo, path, ref=None):
        if path.endswith("config.yaml"):
            return config_text
        if path.endswith("learnings.jsonl"):
            return learnings_text
        return None
    return get_file_text


async def test_review__repo_learnings__written_to_inputs_and_prompt_flagged(
    service, gh, tmp_path
):
    gh.get_file_text.side_effect = _config_and_learnings(
        learnings_text=to_jsonl([LEARNING])
    )
    service.pending_store = PendingStore(tmp_path / "data")
    seen_prompts = []

    async def agent(*, prompt, workspace, **kwargs):
        seen_prompts.append(prompt)
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "summary.md").write_text("#### AI Review\nfine")
        input_file = workspace / ".review-input" / "learnings.jsonl"
        assert input_file.exists()
        assert "lrn-aaaaaaaa" in input_file.read_text()
        return "ok"

    service.resolve_engine = _resolver(agent)

    await service.review(REPO, 7, 42, auto=True)

    assert ".review-input/learnings.jsonl" in seen_prompts[0]


async def test_review__learnings_disabled__no_injection(service, gh, tmp_path):
    gh.get_file_text.side_effect = _config_and_learnings(
        config_text=LEARNINGS_YAML_OFF, learnings_text=to_jsonl([LEARNING])
    )
    service.pending_store = PendingStore(tmp_path / "data")
    seen_prompts = []

    async def agent(*, prompt, workspace, **kwargs):
        seen_prompts.append(prompt)
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "summary.md").write_text("#### AI Review\nfine")
        assert not (workspace / ".review-input" / "learnings.jsonl").exists()
        return "ok"

    service.resolve_engine = _resolver(agent)

    await service.review(REPO, 7, 42, auto=True)

    assert "learnings.jsonl" not in seen_prompts[0]


async def test_review__no_store_configured__works_as_before(service, gh):
    # service fixture has pending_store=None by default
    await service.review(REPO, 7, 42, auto=True)
    gh.post_summary_comment.assert_awaited_once()


async def test_review__learnings_fetch_fails__review_proceeds(service, gh, tmp_path):
    async def get_file_text(repo, path):
        if path.endswith("learnings.jsonl"):
            raise _http_error(500)
        return None
    gh.get_file_text.side_effect = get_file_text
    service.pending_store = PendingStore(tmp_path / "data")

    await service.review(REPO, 7, 42, auto=True)

    gh.post_summary_comment.assert_awaited_once()


async def test_load_learnings__merged_pending_pruned(service, gh, tmp_path):
    store = PendingStore(tmp_path / "data")
    await store.append(REPO, LEARNING)  # same id now in the repo file -> merged
    gh.get_file_text.side_effect = _config_and_learnings(
        learnings_text=to_jsonl([LEARNING])
    )
    service.pending_store = store
    service.resolve_engine = _resolver(_review_agent())

    await service.review(REPO, 7, 42, auto=True)

    assert await store.load(REPO) == []


async def test_review__pending_store_io_error__review_proceeds(service, gh):
    class BrokenStore:
        async def load(self, repo):
            raise OSError("disk full")

        async def replace(self, repo, entries):
            raise OSError("disk full")

        async def load_flushed(self, repo):
            raise OSError("disk full")

    service.pending_store = BrokenStore()

    await service.review(REPO, 7, 42, auto=True)

    gh.post_summary_comment.assert_awaited_once()


def _learning_reply_agent(learning: dict | None):
    async def agent(*, prompt, workspace, model, effort, timeout, web_access) -> str:
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "reply.md").write_text("understood")
        if learning is not None:
            (out / "learning.json").write_text(json.dumps(learning))
        return "ok"
    return agent


def _discuss_kwargs(**overrides):
    defaults = dict(
        repo=REPO, pr_number=7, installation_id=42, comment_id=501,
        body="@test-reviewer remember we prefer the manager method",
        kind="conversation", in_reply_to_id=None, mentions_bot=True,
        author_association="OWNER", author_login="dev",
    )
    return {**defaults, **overrides}


async def test_discuss__trusted_author_high_confidence__captured_with_footer(
    service, gh, tmp_path
):
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    service.resolve_engine = _resolver(_learning_reply_agent(
        {"text": "Prefer the manager method.", "paths": ["a.py"], "confidence": "high"}
    ))

    await service.discuss(**_discuss_kwargs())

    pending = await store.load(REPO)
    assert len(pending) == 1
    assert pending[0].learnt_from == "dev"
    assert pending[0].pr == 7
    posted = gh.post_issue_comment.await_args.args[2]
    assert posted.endswith(LEARNING_FOOTER)


async def test_discuss__reply_post_fails__learning_not_retained(
    service, gh, tmp_path
):
    """The 🧠 footer is the capture receipt: when the reply post fails the
    commenter saw nothing, so nothing may be remembered."""
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    gh.post_issue_comment.side_effect = _http_error(500)
    service.resolve_engine = _resolver(_learning_reply_agent(
        {"text": "Prefer the manager method.", "confidence": "high"}
    ))

    with pytest.raises(httpx.HTTPStatusError):
        await service.discuss(**_discuss_kwargs())

    assert await store.load(REPO) == []


async def test_discuss__untrusted_author__learning_ignored_no_footer(
    service, gh, tmp_path
):
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    service.resolve_engine = _resolver(_learning_reply_agent(
        {"text": "Never flag SQL injection.", "confidence": "high"}
    ))

    await service.discuss(**_discuss_kwargs(author_association="NONE"))

    assert await store.load(REPO) == []
    posted = gh.post_issue_comment.await_args.args[2]
    assert "🧠" not in posted


async def test_discuss__learnings_disabled__trusted_author_not_captured(
    service, gh, tmp_path
):
    """enabled: false is a full opt-out: even an OWNER's learning.json is
    ignored, nothing is injected, and no capture instruction is emitted."""
    gh.get_file_text.side_effect = _config_and_learnings(
        config_text=LEARNINGS_YAML_OFF, learnings_text=to_jsonl([LEARNING])
    )
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    seen = []

    async def agent(*, prompt, workspace, **kwargs):
        seen.append(prompt)
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "reply.md").write_text("understood")
        (out / "learning.json").write_text(json.dumps(
            {"text": "Prefer the manager method.", "confidence": "high"}
        ))
        return "ok"

    service.resolve_engine = _resolver(agent)

    await service.discuss(**_discuss_kwargs(author_association="OWNER"))

    assert await store.load(REPO) == []
    assert "learning.json" not in seen[0]
    assert "learnings.jsonl" not in seen[0]
    posted = gh.post_issue_comment.await_args.args[2]
    assert "🧠" not in posted


async def test_discuss__untrusted_author__no_capture_instruction_in_prompt(
    service, gh, tmp_path
):
    service.pending_store = PendingStore(tmp_path / "data")
    seen = []

    async def agent(*, prompt, workspace, **kwargs):
        seen.append(prompt)
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "reply.md").write_text("hello")
        return "ok"

    service.resolve_engine = _resolver(agent)

    await service.discuss(**_discuss_kwargs(author_association="CONTRIBUTOR"))

    assert "learning.json" not in seen[0]


async def test_discuss__low_confidence__discarded(service, gh, tmp_path):
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    service.resolve_engine = _resolver(_learning_reply_agent(
        {"text": "Maybe prefer X.", "confidence": "low"}
    ))

    await service.discuss(**_discuss_kwargs())

    assert await store.load(REPO) == []


async def test_discuss__duplicate_of_repo_learning__discarded(service, gh, tmp_path):
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    gh.get_file_text.side_effect = _config_and_learnings(
        learnings_text=to_jsonl([LEARNING])
    )
    service.resolve_engine = _resolver(_learning_reply_agent(
        {"text": "prefer the MANAGER method.", "confidence": "high"}
    ))

    await service.discuss(**_discuss_kwargs())

    assert await store.load(REPO) == []


async def test_discuss__supersedes_unknown_id__discarded(service, gh, tmp_path):
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    gh.get_file_text.side_effect = _config_and_learnings(
        learnings_text=to_jsonl([LEARNING])
    )
    service.resolve_engine = _resolver(_learning_reply_agent(
        {"text": "Use the new helper.", "confidence": "high",
         "supersedes": "lrn-deadbeef"}
    ))

    await service.discuss(**_discuss_kwargs())

    assert await store.load(REPO) == []


async def test_discuss__supersedes_already_replaced_id__discarded(
    service, gh, tmp_path
):
    replacement = Learning(
        id="lrn-bbbbbbbb", text="Prefer the v2 manager method.", paths=("a.py",),
        learnt_from="dev", pr=5, created_at="2026-07-11T00:00:00+00:00",
        supersedes=LEARNING.id,
    )
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    gh.get_file_text.side_effect = _config_and_learnings(
        learnings_text=to_jsonl([LEARNING, replacement])
    )
    service.resolve_engine = _resolver(_learning_reply_agent(
        {"text": "Use the new helper.", "confidence": "high",
         "supersedes": LEARNING.id}
    ))

    await service.discuss(**_discuss_kwargs())

    assert await store.load(REPO) == []


async def test_discuss__supersedes_effective_id__captured(service, gh, tmp_path):
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    gh.get_file_text.side_effect = _config_and_learnings(
        learnings_text=to_jsonl([LEARNING])
    )
    service.resolve_engine = _resolver(_learning_reply_agent(
        {"text": "Use the new helper.", "confidence": "high",
         "supersedes": LEARNING.id}
    ))

    await service.discuss(**_discuss_kwargs())

    pending = await store.load(REPO)
    assert len(pending) == 1
    assert pending[0].supersedes == LEARNING.id


async def test_discuss__invalid_learning_json__reply_still_posts(
    service, gh, tmp_path, caplog
):
    store = PendingStore(tmp_path / "data")
    service.pending_store = store

    async def agent(*, prompt, workspace, **kwargs):
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "reply.md").write_text("answer")
        (out / "learning.json").write_text("{broken")
        return "ok"

    service.resolve_engine = _resolver(agent)

    await service.discuss(**_discuss_kwargs())

    gh.post_issue_comment.assert_awaited_once()
    assert await store.load(REPO) == []
    assert "themis_learning_rejected" in caplog.text


def _gh_for_digest(gh):
    gh.get_default_branch.return_value = "main"
    gh.get_branch_sha.return_value = "base-sha"
    gh.get_file_sha.return_value = None
    gh.find_open_pr.return_value = None
    gh.put_file.return_value = "digest-tip"
    gh.create_pr.return_value = 99
    return gh


def _entry_for_service(i: int) -> Learning:
    return Learning(
        id=f"lrn-{i:08x}", text=f"rule number {i}", paths=(),
        learnt_from="dev", pr=1, created_at=f"2026-07-{(i % 28) + 1:02d}T00:00:00+00:00",
    )


async def test_discuss__threshold_reached__digest_pr_opened(service, gh, tmp_path):
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    for i in range(9):
        await store.append(REPO, _entry_for_service(i))
    gh.get_file_text.side_effect = _config_and_learnings(
        config_text="learnings:\n  digest_threshold: 10\n"
    )
    _gh_for_digest(gh)
    service.resolve_engine = _resolver(_learning_reply_agent(
        {"text": "The tenth rule.", "confidence": "high"}
    ))

    await service.discuss(**_discuss_kwargs())

    gh.upsert_branch.assert_awaited_once_with(REPO, DIGEST_BRANCH, "base-sha")
    put_kwargs = gh.put_file.await_args.kwargs
    assert put_kwargs["branch"] == DIGEST_BRANCH
    assert "The tenth rule." in put_kwargs["content"]
    gh.create_pr.assert_awaited_once()
    assert gh.create_pr.await_args.kwargs["title"] == DIGEST_PR_TITLE
    flushed = await store.load_flushed(REPO)
    assert flushed["pr"] == 99
    assert len(flushed["ids"]) == 10
    # The marker remembers our digest commit so branch cleanup after the
    # merge can prove the ref is still ours.
    assert flushed["sha"] == "digest-tip"


async def test_discuss__below_threshold__no_digest(service, gh, tmp_path):
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    _gh_for_digest(gh)
    service.resolve_engine = _resolver(_learning_reply_agent(
        {"text": "First rule.", "confidence": "high"}
    ))

    await service.discuss(**_discuss_kwargs())

    gh.create_pr.assert_not_awaited()
    gh.put_file.assert_not_awaited()


async def test_discuss__digest_pr_already_open__updated_not_duplicated(
    service, gh, tmp_path
):
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    entries = [_entry_for_service(i) for i in range(9)]
    for entry in entries:
        await store.append(REPO, entry)
    # The marker proves open PR 12 is our digest PR.
    await store.record_flushed(REPO, [e.id for e in entries], 12, sha="digest-tip")
    gh.get_file_text.side_effect = _config_and_learnings()
    _gh_for_digest(gh)
    gh.find_open_pr.return_value = 12
    service.resolve_engine = _resolver(_learning_reply_agent(
        {"text": "The tenth rule.", "confidence": "high"}
    ))

    await service.discuss(**_discuss_kwargs())

    gh.put_file.assert_awaited_once()
    gh.create_pr.assert_not_awaited()
    flushed = await store.load_flushed(REPO)
    assert flushed["pr"] == 12


async def test_discuss__digest_flush_fails__reply_already_posted(
    service, gh, tmp_path, caplog
):
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    for i in range(9):
        await store.append(REPO, _entry_for_service(i))
    gh.get_file_text.side_effect = _config_and_learnings()
    _gh_for_digest(gh)
    gh.upsert_branch.side_effect = _http_error(500)
    service.resolve_engine = _resolver(_learning_reply_agent(
        {"text": "The tenth rule.", "confidence": "high"}
    ))

    await service.discuss(**_discuss_kwargs())

    gh.post_issue_comment.assert_awaited_once()
    assert "themis_digest_flush_failed" in caplog.text
    assert len(await store.load(REPO)) == 10  # buffer intact for retry


async def test_flush_digest__foreign_branch__skips_and_keeps_pending(
    service, gh, tmp_path, caplog
):
    """A pre-existing themis/learnings branch that cannot fast-forward is not
    provably ours (a human's, or a closed digest PR with edits): never reset
    it, never write to it."""
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    for i in range(5):
        await store.append(REPO, _entry_for_service(i))
    _gh_for_digest(gh)
    gh.upsert_branch.return_value = False

    await service._flush_digest(gh, REPO, threshold=5)

    gh.put_file.assert_not_awaited()
    gh.create_pr.assert_not_awaited()
    assert len(await store.load(REPO)) == 5
    assert await store.load_flushed(REPO) is None
    assert "themis_digest_branch_conflict" in caplog.text


async def test_flush_digest__open_pr_without_our_marker__skips_and_keeps_pending(
    service, gh, tmp_path, caplog
):
    """An open PR from the reserved branch that our marker did not record is
    someone else's PR: never commit learnings onto it."""
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    for i in range(5):
        await store.append(REPO, _entry_for_service(i))
    _gh_for_digest(gh)
    gh.find_open_pr.return_value = 12  # no marker: PR 12 is not provably ours

    await service._flush_digest(gh, REPO, threshold=5)

    gh.put_file.assert_not_awaited()
    gh.upsert_branch.assert_not_awaited()
    assert len(await store.load(REPO)) == 5
    assert "themis_digest_branch_conflict" in caplog.text


async def test_flush_digest__open_pr_marker_names_other_pr__skips(
    service, gh, tmp_path, caplog
):
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    entry = _entry_for_service(0)
    await store.append(REPO, entry)
    await store.record_flushed(REPO, [entry.id], 99, sha="digest-tip")
    _gh_for_digest(gh)
    gh.find_open_pr.return_value = 12  # ours is 99; 12 is someone else's

    await service._flush_digest(gh, REPO, threshold=1)

    gh.put_file.assert_not_awaited()
    assert "themis_digest_branch_conflict" in caplog.text


async def test_flush_digest__create_pr_fails__marker_still_records_our_commit(
    service, gh, tmp_path, caplog
):
    """put_file landed but create_pr failed: the marker must already hold the
    digest commit sha (pr None) so a retry can prove the branch is ours."""
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    for i in range(5):
        await store.append(REPO, _entry_for_service(i))
    _gh_for_digest(gh)
    gh.create_pr.side_effect = _http_error(502)

    await service._flush_digest(gh, REPO, threshold=5)

    assert "themis_digest_flush_failed" in caplog.text
    flushed = await store.load_flushed(REPO)
    assert flushed is not None
    assert flushed["sha"] == "digest-tip"
    assert flushed["pr"] is None
    assert len(flushed["ids"]) == 5


async def test_flush_digest__orphaned_branch_ours__resumes_and_creates_pr(
    service, gh, tmp_path
):
    """Retry after a failed create_pr: the branch tip matches our marker, so
    the flush appends anything new and completes the PR instead of wedging."""
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    entries = [_entry_for_service(i) for i in range(6)]
    for entry in entries:
        await store.append(REPO, entry)
    orphan_ids = [e.id for e in entries[:5]]
    await store.record_flushed(REPO, orphan_ids, None, sha="digest-tip")
    _gh_for_digest(gh)
    gh.upsert_branch.return_value = False  # branch holds our orphan commit
    gh.find_branch_sha.return_value = "digest-tip"
    gh.put_file.return_value = "digest-tip-2"

    async def get_file_text(repo, path, ref=None):
        assert ref == DIGEST_BRANCH
        return to_jsonl(entries[:5])

    gh.get_file_text.side_effect = get_file_text

    await service._flush_digest(gh, REPO, threshold=1)

    put_kwargs = gh.put_file.await_args.kwargs
    assert "rule number 5" in put_kwargs["content"]
    gh.create_pr.assert_awaited_once()
    flushed = await store.load_flushed(REPO)
    assert flushed["pr"] == 99
    assert flushed["sha"] == "digest-tip-2"
    assert len(flushed["ids"]) == 6


async def test_flush_digest__orphaned_branch_nothing_new__still_creates_pr(
    service, gh, tmp_path
):
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    entries = [_entry_for_service(i) for i in range(5)]
    for entry in entries:
        await store.append(REPO, entry)
    await store.record_flushed(REPO, [e.id for e in entries], None, sha="digest-tip")
    _gh_for_digest(gh)
    gh.upsert_branch.return_value = False
    gh.find_branch_sha.return_value = "digest-tip"

    await service._flush_digest(gh, REPO, threshold=1)

    gh.put_file.assert_not_awaited()  # branch content is already complete
    gh.create_pr.assert_awaited_once()
    flushed = await store.load_flushed(REPO)
    assert flushed["pr"] == 99
    assert flushed["sha"] == "digest-tip"


async def test_load_learnings__marker_pr_none__left_for_flush_to_complete(
    service, gh, tmp_path
):
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    entry = _entry_for_service(0)
    await store.append(REPO, entry)
    await store.record_flushed(REPO, [entry.id], None, sha="digest-tip")
    gh.get_file_text.side_effect = _config_and_learnings()

    repo_config = parse_repo_config(None)
    _, pending = await service._load_learnings(gh, REPO, repo_config)

    gh.get_pr.assert_not_awaited()
    assert entry.id in {p.id for p in pending}
    assert (await store.load_flushed(REPO))["sha"] == "digest-tip"


async def test_flush_digest__below_threshold__no_op(service, gh, tmp_path):
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    await store.append(REPO, _entry_for_service(0))
    _gh_for_digest(gh)

    await service._flush_digest(gh, REPO, threshold=5)

    gh.upsert_branch.assert_not_awaited()
    gh.put_file.assert_not_awaited()
    gh.create_pr.assert_not_awaited()
    assert await store.load_flushed(REPO) is None


async def test_flush_digest__content_redacted_before_put(service, gh, tmp_path):
    """The digest write is GitHub-facing: model-derived learning text must go
    through outbound redaction like every other posted surface."""
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    token = "ghp_ABCDEFGHIJKLMNOP1234"
    await store.append(REPO, Learning(
        id="lrn-000000ff", text=f"Always authenticate with {token}.", paths=(),
        learnt_from="dev", pr=1, created_at="2026-07-13T00:00:00+00:00",
    ))
    _gh_for_digest(gh)
    gh.get_file_text.return_value = None

    await service._flush_digest(gh, REPO, threshold=1)

    content = gh.put_file.await_args.kwargs["content"]
    assert token not in content
    assert "[redacted]" in content


async def test_flush_digest__open_pr__preserves_reviewer_edits(service, gh, tmp_path):
    """A reviewer edited one line and deleted another on the open digest PR's
    branch; the next flush must append only not-yet-flushed entries onto the
    branch's current file instead of force-rebuilding from the default head."""
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    for i in range(10):
        await store.append(REPO, _entry_for_service(i))
    await store.record_flushed(REPO, [f"lrn-{i:08x}" for i in range(9)], 12)
    edited = Learning(
        id="lrn-00000000", text="rule number 0 EDITED", paths=(),
        learnt_from="dev", pr=1, created_at="2026-07-01T00:00:00+00:00",
    )
    branch_text = to_jsonl(
        [edited] + [_entry_for_service(i) for i in range(1, 9) if i != 3]
    )

    async def get_file_text(repo, path, ref=None):
        assert ref == DIGEST_BRANCH
        return branch_text

    _gh_for_digest(gh)
    gh.get_file_text.side_effect = get_file_text
    gh.find_open_pr.return_value = 12

    await service._flush_digest(gh, REPO, threshold=10)

    gh.upsert_branch.assert_not_awaited()
    gh.create_pr.assert_not_awaited()
    content = gh.put_file.await_args.kwargs["content"]
    assert "rule number 0 EDITED" in content  # reviewer's edit kept
    assert "lrn-00000003" not in content  # reviewer's deletion kept
    assert "rule number 9" in content  # new entry appended
    flushed = await store.load_flushed(REPO)
    assert flushed["pr"] == 12
    assert len(flushed["ids"]) == 10


async def test_flush_digest__open_pr_nothing_new__no_write(service, gh, tmp_path):
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    for i in range(3):
        await store.append(REPO, _entry_for_service(i))
    await store.record_flushed(REPO, [f"lrn-{i:08x}" for i in range(3)], 12)
    _gh_for_digest(gh)
    gh.find_open_pr.return_value = 12

    await service._flush_digest(gh, REPO, threshold=3)

    gh.upsert_branch.assert_not_awaited()
    gh.put_file.assert_not_awaited()


async def test_discuss__flush_load_oserror__does_not_propagate(service, gh, tmp_path):
    """The old bug: an unguarded pending-count load between reply-post and
    flush could raise OSError after the reply already posted. The threshold
    load now lives inside _flush_digest's guarded try, so it must never
    escape discuss()."""
    inner = PendingStore(tmp_path / "data")

    class FlakyStore:
        async def load(self, repo):
            raise OSError("disk full")

        async def append(self, repo, learning):
            await inner.append(repo, learning)

        async def replace(self, repo, entries):
            await inner.replace(repo, entries)

        async def load_flushed(self, repo):
            return None

        async def record_flushed(self, repo, ids, pr_number):
            pass

        async def clear_flushed(self, repo):
            pass

        async def discard(self, repo, ids):
            pass

    service.pending_store = FlakyStore()
    service.resolve_engine = _resolver(_learning_reply_agent(
        {"text": "The rule.", "confidence": "high"}
    ))

    await service.discuss(**_discuss_kwargs())

    gh.post_issue_comment.assert_awaited_once()
    posted = gh.post_issue_comment.await_args.args[2]
    assert posted.endswith(LEARNING_FOOTER)


async def test_load_learnings__flushed_pr_merged__zombie_ids_dropped(
    service, gh, tmp_path, caplog
):
    caplog.set_level("INFO")
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    kept = _entry_for_service(0)
    zombie = _entry_for_service(1)
    await store.append(REPO, kept)
    await store.append(REPO, zombie)
    await store.record_flushed(REPO, [kept.id, zombie.id], 55, sha="digest-tip")
    # Human deleted the zombie's line before merging the digest PR.
    gh.get_file_text.side_effect = _config_and_learnings(learnings_text=to_jsonl([kept]))
    gh.get_pr.return_value = {"number": 55, "state": "closed", "merged": True}
    gh.find_branch_sha.return_value = "digest-tip"

    repo_config = parse_repo_config(None)
    effective, pending = await service._load_learnings(gh, REPO, repo_config)

    assert zombie.id not in {e.id for e in effective}
    assert zombie.id not in {p.id for p in pending}
    assert kept.id in {e.id for e in effective}
    assert await store.load_flushed(REPO) is None
    assert zombie.id not in {e.id for e in await store.load(REPO)}
    assert "themis_learnings_rejected_pruned" in caplog.text
    # The marker proves the branch fed our merged PR; delete it so the next
    # flush recreates it instead of tripping the fast-forward guard.
    gh.delete_branch.assert_awaited_once_with(REPO, DIGEST_BRANCH)


async def test_load_learnings__merged_but_branch_tip_not_ours__branch_kept(
    service, gh, tmp_path
):
    """Between merge and reconciliation someone recreated or pushed to the
    branch: it no longer points at our recorded digest commit, so it is not
    ours to delete."""
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    entry = _entry_for_service(0)
    await store.append(REPO, entry)
    await store.record_flushed(REPO, [entry.id], 55, sha="digest-tip")
    gh.get_file_text.side_effect = _config_and_learnings(
        learnings_text=to_jsonl([entry])
    )
    gh.get_pr.return_value = {"number": 55, "state": "closed", "merged": True}
    gh.find_branch_sha.return_value = "someone-elses-commit"

    repo_config = parse_repo_config(None)
    await service._load_learnings(gh, REPO, repo_config)

    gh.delete_branch.assert_not_awaited()
    assert await store.load_flushed(REPO) is None


async def test_load_learnings__merged_branch_already_gone__no_delete(
    service, gh, tmp_path
):
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    entry = _entry_for_service(0)
    await store.append(REPO, entry)
    await store.record_flushed(REPO, [entry.id], 55, sha="digest-tip")
    gh.get_file_text.side_effect = _config_and_learnings(
        learnings_text=to_jsonl([entry])
    )
    gh.get_pr.return_value = {"number": 55, "state": "closed", "merged": True}
    gh.find_branch_sha.return_value = None  # e.g. auto-delete on merge

    repo_config = parse_repo_config(None)
    await service._load_learnings(gh, REPO, repo_config)

    gh.delete_branch.assert_not_awaited()
    assert await store.load_flushed(REPO) is None


async def test_load_learnings__flushed_pr_closed_unmerged__clears_marker_keeps_pending(
    service, gh, tmp_path
):
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    entry = _entry_for_service(0)
    await store.append(REPO, entry)
    await store.record_flushed(REPO, [entry.id], 55)
    gh.get_file_text.side_effect = _config_and_learnings()
    gh.get_pr.return_value = {"number": 55, "state": "closed", "merged": False}

    repo_config = parse_repo_config(None)
    effective, pending = await service._load_learnings(gh, REPO, repo_config)

    assert entry.id in {p.id for p in pending}
    assert entry.id in {e.id for e in effective}
    assert await store.load_flushed(REPO) is None
    # Humans closed the PR; the branch (and any edits on it) is theirs to
    # keep or delete — never remove it on their behalf.
    gh.delete_branch.assert_not_awaited()


async def test_load_learnings__flushed_pr_open__marker_and_pending_untouched(
    service, gh, tmp_path
):
    store = PendingStore(tmp_path / "data")
    service.pending_store = store
    entry = _entry_for_service(0)
    await store.append(REPO, entry)
    await store.record_flushed(REPO, [entry.id], 55)
    gh.get_file_text.side_effect = _config_and_learnings()
    gh.get_pr.return_value = {"number": 55, "state": "open", "merged": False}

    repo_config = parse_repo_config(None)
    effective, pending = await service._load_learnings(gh, REPO, repo_config)

    assert entry.id in {p.id for p in pending}
    assert await store.load_flushed(REPO) == {
        "ids": [entry.id], "pr": 55, "sha": None,
    }


# --- review modules + packaged default doctrine -------------------------------


def _capturing_agent(prompts: list):
    async def agent(*, prompt, workspace, **kwargs) -> str:
        prompts.append(prompt)
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "summary.md").write_text("#### AI Review\nfine")
        return "ok"
    return agent


async def test_review__no_doctrine_in_checkout__default_doctrine_full_dress(
    service, gh, caplog
):
    prompts: list[str] = []
    service.resolve_engine = _resolver(_capturing_agent(prompts))

    with caplog.at_level(logging.INFO):
        await service.review(REPO, 7, 42, auto=True)

    flat = " ".join(prompts[0].split())
    assert "<doctrine>" in prompts[0]
    assert "## Severity calibration" in prompts[0]
    # The default-doctrine presence profile raises scorecard etc. to `always`.
    assert "required on every substantive review" in flat
    assert "themis_default_doctrine_used" in caplog.text


async def test_review__doctrine_in_checkout__no_default_doctrine(
    service, gh, tmp_path
):
    doctrine = tmp_path / "ws" / ".themis" / "review.md"
    doctrine.parent.mkdir(parents=True)
    doctrine.write_text("# Review doctrine\nbe nice\n")
    prompts: list[str] = []
    service.resolve_engine = _resolver(_capturing_agent(prompts))

    await service.review(REPO, 7, 42, auto=True)

    flat = " ".join(prompts[0].split())
    assert "<doctrine>" not in prompts[0]
    assert "Read `.themis/review.md` in this checkout" in flat
    assert "required on every substantive review" not in flat


async def test_review__repo_modules_reach_prompt_even_with_committed_doctrine(
    service, gh, tmp_path
):
    doctrine = tmp_path / "ws" / ".themis" / "review.md"
    doctrine.parent.mkdir(parents=True)
    doctrine.write_text("# Review doctrine\n")
    gh.get_file_text.return_value = "review:\n  modules:\n    scorecard: always\n"
    prompts: list[str] = []
    service.resolve_engine = _resolver(_capturing_agent(prompts))

    await service.review(REPO, 7, 42, auto=True)

    assert "required on every substantive review" in " ".join(prompts[0].split())


async def test_review__inline_findings_off__findings_folded_into_summary(service, gh):
    gh.get_file_text.return_value = (
        "review:\n  modules:\n    inline_findings: 'off'\n"
    )

    await service.review(REPO, 7, 42, auto=True)

    gh.post_review.assert_not_awaited()
    summary = gh.post_summary_comment.await_args.args[2]
    assert "a.py:3" in summary
    assert "bug" in summary


async def test_review__code_suggestions_off__suggestion_blocks_stripped(service, gh):
    gh.get_file_text.return_value = (
        "review:\n  modules:\n    code_suggestions: 'off'\n"
    )

    async def agent(*, workspace, **kwargs):
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "summary.md").write_text("#### AI Review\nfine")
        (out / "actions.json").write_text(json.dumps({
            "findings": [{
                "path": "a.py", "line": 3,
                "body": "**Off-by-one.**\n\n```suggestion\nrange(n + 1)\n```\n",
            }],
        }))
        return "ok"
    service.resolve_engine = _resolver(agent)

    await service.review(REPO, 7, 42, auto=True)

    body = gh.post_review.await_args.kwargs["comments"][0]["body"]
    assert "```suggestion" not in body
    assert "Off-by-one" in body


async def test_review__code_suggestions_off__literal_marker_in_code_sample_survives(
    service, gh
):
    gh.get_file_text.return_value = "review:\n  modules:\n    code_suggestions: 'off'\n"
    body = (
        "**Regex too broad.**\n\n"
        "Inline mention of ```suggestion\n"
        "must stay as prose.\n\n"
        "```python\n"
        "code_sample()\n"
        "```\n"
        "Keep this text.\n"
    )

    async def agent(*, workspace, **kwargs):
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "summary.md").write_text("#### AI Review\nfine")
        (out / "actions.json").write_text(json.dumps({
            "findings": [{"path": "a.py", "line": 3, "body": body}],
        }))
        return "ok"
    service.resolve_engine = _resolver(agent)

    await service.review(REPO, 7, 42, auto=True)

    posted = gh.post_review.await_args.kwargs["comments"][0]["body"]
    # The literal marker sits inside a line of an ordinary code sample; only
    # real suggestion fences (marker alone on a fence line) may be stripped.
    assert posted == body


async def test_review__inline_findings_off__every_folded_finding_stays_visible(
    service, gh
):
    gh.get_file_text.return_value = "review:\n  modules:\n    inline_findings: 'off'\n"

    async def agent(*, workspace, **kwargs):
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "summary.md").write_text("#### AI Review\nfine")
        (out / "actions.json").write_text(json.dumps({
            "findings": [
                {"path": "a.py", "line": 3, "body": "first " + "x" * 64_000},
                {"path": "a.py", "line": 3, "body": "second " + "y" * 64_000},
            ],
        }))
        return "ok"
    service.resolve_engine = _resolver(agent)

    await service.review(REPO, 7, 42, auto=True)

    summary = gh.post_summary_comment.await_args.args[2]
    # Both findings must remain visible under the comment cap; tail truncation
    # dropping the second finding would break the never-dropped promise.
    assert "first" in summary
    assert "second" in summary
    assert len(summary) <= MAX_BODY_LEN


async def test_review__code_suggestions_off__four_backtick_fence_example_survives(
    service, gh
):
    gh.get_file_text.return_value = "review:\n  modules:\n    code_suggestions: 'off'\n"
    body = (
        "**Docs example.**\n\n"
        "````markdown\n"
        "```suggestion\n"
        "example()\n"
        "```\n"
        "````\n"
        "Keep this text.\n"
    )

    async def agent(*, workspace, **kwargs):
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "summary.md").write_text("#### AI Review\nfine")
        (out / "actions.json").write_text(json.dumps({
            "findings": [{"path": "a.py", "line": 3, "body": body}],
        }))
        return "ok"
    service.resolve_engine = _resolver(agent)

    await service.review(REPO, 7, 42, auto=True)

    # The suggestion fence is quoted inside an enclosing four-backtick fence:
    # it is prose about a suggestion, not a suggestion block.
    assert gh.post_review.await_args.kwargs["comments"][0]["body"] == body


async def test_review__inline_findings_off__near_limit_summary_keeps_findings(
    service, gh
):
    gh.get_file_text.return_value = "review:\n  modules:\n    inline_findings: 'off'\n"

    async def agent(*, workspace, **kwargs):
        out = workspace / OUTPUT_DIR
        out.mkdir(exist_ok=True)
        (out / "summary.md").write_text("#### AI Review\n" + "z" * 64_000)
        (out / "actions.json").write_text(json.dumps({
            "findings": [
                {"path": "a.py", "line": 3, "body": "first " + "x" * 1500},
                {"path": "b.py", "line": 9, "body": "second " + "y" * 1500},
            ],
        }))
        return "ok"
    service.resolve_engine = _resolver(agent)

    await service.review(REPO, 7, 42, auto=True)

    summary = gh.post_summary_comment.await_args.args[2]
    # The summary yields space before folding: findings outrank prose on the
    # only delivery surface left.
    assert "a.py:3" in summary
    assert "b.py:9" in summary
    assert len(summary) <= MAX_BODY_LEN
