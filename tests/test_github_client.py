import base64
import json

import httpx
import pytest

from themis.github.client import (
    MAX_COMMENT_PAGES,
    MAX_COMMENT_PAGES_TOTAL,
    SUMMARY_MARKER,
    GitHubClient,
    GitHubGraphQLError,
)

pytestmark = pytest.mark.asyncio


def _client(handler) -> GitHubClient:
    return GitHubClient(token="ghs_abc", transport=httpx.MockTransport(handler))


async def test_get_pr__ok__returns_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/acme/widgets/pulls/7"
        assert request.headers["Authorization"] == "Bearer ghs_abc"
        return httpx.Response(
            200,
            json={"number": 7, "state": "open", "head": {"sha": "abc123"}, "base": {"ref": "main"}},
        )

    pr = await _client(handler).get_pr("acme/widgets", 7)

    assert pr["head"]["sha"] == "abc123"


async def test_post_review__findings__posts_batched_comment_review():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 1})

    comments = [{"path": "a.py", "line": 3, "side": "RIGHT", "body": "bug"}]
    await _client(handler).post_review("acme/widgets", 7, commit_sha="abc123", comments=comments)

    assert captured["path"] == "/repos/acme/widgets/pulls/7/reviews"
    assert captured["json"] == {
        "commit_id": "abc123", "event": "COMMENT", "body": "", "comments": comments,
    }


async def test_post_issue_comment__body__posts():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["json"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 2})

    await _client(handler).post_issue_comment("acme/widgets", 7, "hello")

    assert captured["path"] == "/repos/acme/widgets/issues/7/comments"
    assert captured["json"] == {"body": "hello"}


async def test_post_summary_comment__always_creates_new_comment_with_marker():
    # Every review request produces a fresh summary comment; never edit old ones.
    requests_seen = []
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request.method)
        captured["path"] = request.url.path
        captured["json"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 2})

    await _client(handler).post_summary_comment("acme/widgets", 7, "#### 🤖 AI Review\nok")

    assert requests_seen == ["POST"]
    assert captured["path"] == "/repos/acme/widgets/issues/7/comments"
    assert captured["json"]["body"].startswith(SUMMARY_MARKER)
    assert "#### 🤖 AI Review" in captured["json"]["body"]


async def test_list_pr_files__two_pages__returns_all_filenames():
    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        if page == "1":
            return httpx.Response(200, json=[{"filename": f"file_{i}.py"} for i in range(100)])
        return httpx.Response(200, json=[{"filename": "last.py"}])

    files = await _client(handler).list_pr_files("acme/widgets", 7)

    assert len(files) == 101
    assert files[0] == "file_0.py"
    assert files[-1] == "last.py"


async def test_list_pr_files__renamed_file__includes_previous_filename():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[{"filename": "new_name.py", "previous_filename": "old_name.py"}],
        )

    files = await _client(handler).list_pr_files("acme/widgets", 7)

    assert files == ["new_name.py", "old_name.py"]


async def test_list_pr_files__http_error__raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    with pytest.raises(httpx.HTTPStatusError):
        await _client(handler).list_pr_files("acme/widgets", 7)


@pytest.mark.parametrize(
    ("check_run", "status", "expected"),
    [
        ({"name": "tests", "status": "completed", "conclusion": "success"}, None,
         "passed"),
        ({"name": "tests", "status": "completed", "conclusion": "failure"}, None,
         "failed"),
        ({"name": "tests", "status": "in_progress", "conclusion": None}, None,
         "pending"),
        (None, None, "none"),
    ],
)
async def test_get_ci_snapshot__states__aggregates_without_polling(
    check_run, status, expected
):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": [check_run] if check_run else []})
        if request.url.path.endswith("/statuses"):
            return httpx.Response(200, json=[status] if status else [])
        raise AssertionError(request.url.path)

    snapshot = await _client(handler).get_ci_snapshot("acme/widgets", "abc123")

    assert snapshot["state"] == expected
    assert snapshot["head_sha"] == "abc123"
    assert requests == [
        "/repos/acme/widgets/commits/abc123/check-runs",
        "/repos/acme/widgets/commits/abc123/statuses",
    ]


async def test_get_ci_snapshot__legacy_statuses__keeps_latest_per_context():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": []})
        return httpx.Response(200, json=[
            {"context": "deploy", "state": "success", "target_url": "new"},
            {"context": "deploy", "state": "failure", "target_url": "old"},
        ])

    snapshot = await _client(handler).get_ci_snapshot("acme/widgets", "abc123")

    assert snapshot["state"] == "passed"
    assert snapshot["checks"] == [{
        "type": "status", "name": "deploy", "status": "completed",
        "conclusion": "success", "details_url": "new",
    }]


async def test_get_ci_snapshot__both_sources_fail__is_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "permission denied"})

    snapshot = await _client(handler).get_ci_snapshot("acme/widgets", "abc123")

    assert snapshot["state"] == "unavailable"
    assert snapshot["checks"] == []
    assert snapshot["unavailable_sources"] == ["check_runs", "statuses"]


async def test_get_ci_snapshot__one_source_invisible__is_not_reported_as_passed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(403, json={"message": "missing permission"})
        return httpx.Response(200, json=[{"context": "build", "state": "success"}])

    snapshot = await _client(handler).get_ci_snapshot("acme/widgets", "abc123")

    assert snapshot["state"] == "unavailable"
    assert snapshot["checks"][0]["conclusion"] == "success"
    assert snapshot["unavailable_sources"] == ["check_runs"]


async def test_list_review_threads__single_page__returns_nodes():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/graphql"
        return httpx.Response(200, json={"data": {"repository": {"pullRequest": {
            "reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"id": "T_1", "isResolved": False, "path": "a.py", "line": 3,
                           "resolvedBy": None,
                           "comments": {"nodes": [{"author": {"login": "themis-reviewer"},
                                                   "body": "bug", "databaseId": 11,
                                                   "createdAt": "2026-01-01T00:00:00Z"}]}}],
            }}}}})

    threads = await _client(handler).list_review_threads("acme/widgets", 7)

    assert threads[0]["id"] == "T_1"


async def test_list_review_threads__query_requests_acknowledgment_fields():
    queries = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(json.loads(request.content)["query"])
        return httpx.Response(200, json={"data": {"repository": {"pullRequest": {
            "reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [],
            }}}}})

    await _client(handler).list_review_threads("acme/widgets", 7)

    # The acknowledgment rule (resolved threads, maintainer acceptance replies)
    # reads these straight from threads.json; dropping any of them silently
    # disables it.
    assert "isResolved" in queries[0]
    assert "resolvedBy { login }" in queries[0]
    assert "authorAssociation" in queries[0]


async def test_list_review_threads__two_pages__follows_cursor_and_returns_all():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        variables = json.loads(request.content)["variables"]
        requests.append(variables)
        if variables["cursor"] is None:
            return httpx.Response(200, json={"data": {"repository": {"pullRequest": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "CUR_1"},
                    "nodes": [{"id": "T_1"}],
                }}}}})
        return httpx.Response(200, json={"data": {"repository": {"pullRequest": {
            "reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"id": "T_2"}],
            }}}}})

    threads = await _client(handler).list_review_threads("acme/widgets", 7)

    assert [t["id"] for t in threads] == ["T_1", "T_2"]
    assert requests[1]["cursor"] == "CUR_1"


async def test_list_review_threads__graphql_errors__raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": None, "errors": [{"message": "Could not resolve PR"}]}
        )

    with pytest.raises(GitHubGraphQLError):
        await _client(handler).list_review_threads("acme/widgets", 7)


async def test_resolve_thread__graphql_errors__raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": None, "errors": [{"message": "Thread not found"}]}
        )

    with pytest.raises(GitHubGraphQLError):
        await _client(handler).resolve_thread("T_missing")


async def test_resolve_thread__thread_id__posts_mutation():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content"] = request.content
        return httpx.Response(
            200, json={"data": {"resolveReviewThread": {"thread": {"id": "T_1"}}}}
        )

    await _client(handler).resolve_thread("T_1")

    assert b"resolveReviewThread" in captured["content"]


async def test_post_reply__comment_id__posts_to_replies_endpoint():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(201, json={"id": 12})

    await _client(handler).post_reply("acme/widgets", 7, in_reply_to=11, body="answer")

    assert captured["path"] == "/repos/acme/widgets/pulls/7/comments/11/replies"


async def test_async_context_manager__exit__closes_client():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"number": 7})

    async with _client(handler) as client:
        await client.get_pr("acme/widgets", 7)

    assert client._client.is_closed


async def test_get_pr__http_error__raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "not found"})

    with pytest.raises(httpx.HTTPStatusError):
        await _client(handler).get_pr("acme/widgets", 7)


async def test_add_reaction__issue_comment__posts_to_issues_comments_endpoint():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["json"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 1})

    await _client(handler).add_reaction("acme/widgets", issue_comment_id=501)

    assert captured["path"] == "/repos/acme/widgets/issues/comments/501/reactions"
    assert captured["json"] == {"content": "eyes"}


async def test_add_reaction__review_comment__posts_to_pulls_comments_endpoint():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json={"id": 1})

    await _client(handler).add_reaction("acme/widgets", review_comment_id=601)

    assert captured["path"] == "/repos/acme/widgets/pulls/comments/601/reactions"


async def test_add_reaction__issue_number__posts_to_issues_endpoint():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(201, json={"id": 1})

    await _client(handler).add_reaction("acme/widgets", issue_number=7)

    assert captured["path"] == "/repos/acme/widgets/issues/7/reactions"


async def test_add_reaction__rocket_content__posts_rocket():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["json"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 1})

    await _client(handler).add_reaction("acme/widgets", issue_number=7, content="rocket")

    assert captured["path"] == "/repos/acme/widgets/issues/7/reactions"
    assert captured["json"] == {"content": "rocket"}


async def test_add_reaction__no_target__raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with pytest.raises(ValueError):
        await _client(handler).add_reaction("acme/widgets")


async def test_add_reaction__multiple_targets__raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with pytest.raises(ValueError):
        await _client(handler).add_reaction(
            "acme/widgets", issue_comment_id=1, review_comment_id=2
        )


async def test_add_reaction__http_error__raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    with pytest.raises(httpx.HTTPStatusError):
        await _client(handler).add_reaction("acme/widgets", issue_number=7)


async def test_get_file_text_returns_raw_content():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/acme/widgets/contents/.themis/config.yaml"
        assert request.headers["Accept"] == "application/vnd.github.raw+json"
        return httpx.Response(200, text="model:\n  name: gpt-5.4\n")

    text = await _client(handler).get_file_text("acme/widgets", ".themis/config.yaml")

    assert text == "model:\n  name: gpt-5.4\n"


async def test_get_file_text_with_ref__queries_that_ref():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["ref"] == "themis/learnings"
        assert request.headers["Accept"] == "application/vnd.github.raw+json"
        return httpx.Response(200, text='{"id": "lrn-00000001"}')

    text = await _client(handler).get_file_text(
        "acme/widgets", ".themis/learnings.jsonl", ref="themis/learnings"
    )

    assert text == '{"id": "lrn-00000001"}'


async def test_get_file_text_without_ref__no_ref_param():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "ref" not in request.url.params
        return httpx.Response(200, text="x")

    assert await _client(handler).get_file_text("acme/widgets", "f.txt") == "x"


async def test_get_file_text_missing_file_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    assert await _client(handler).get_file_text("acme/widgets", ".themis/config.yaml") is None


async def test_get_file_text_server_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    with pytest.raises(httpx.HTTPStatusError):
        await _client(handler).get_file_text("acme/widgets", ".themis/config.yaml")


async def test_get_default_branch__returns_field():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/acme/widgets"
        return httpx.Response(200, json={"default_branch": "main"})

    assert await _client(handler).get_default_branch("acme/widgets") == "main"


async def test_get_branch_sha__returns_object_sha():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/acme/widgets/git/ref/heads/main"
        return httpx.Response(200, json={"object": {"sha": "abc123"}})

    assert await _client(handler).get_branch_sha("acme/widgets", "main") == "abc123"


async def test_upsert_branch__exists__fast_forwards_without_force():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={})

    moved = await _client(handler).upsert_branch(
        "acme/widgets", "themis/learnings", "abc123"
    )

    assert moved is True
    assert captured["method"] == "PATCH"
    assert captured["path"] == "/repos/acme/widgets/git/refs/heads/themis/learnings"
    assert captured["json"] == {"sha": "abc123", "force": False}


async def test_upsert_branch__missing__creates_ref():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "PATCH":
            return httpx.Response(422, json={"message": "Reference does not exist"})
        assert json.loads(request.content) == {
            "ref": "refs/heads/themis/learnings", "sha": "abc123",
        }
        return httpx.Response(201, json={})

    moved = await _client(handler).upsert_branch(
        "acme/widgets", "themis/learnings", "abc123"
    )

    assert moved is True
    assert calls[-1] == ("POST", "/repos/acme/widgets/git/refs")


async def test_upsert_branch__exists_not_fast_forward__returns_false():
    """A branch holding commits that are not on the default branch (a human's,
    or a closed digest PR's edits) must never be moved."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            return httpx.Response(422, json={"message": "Update is not a fast forward"})
        return httpx.Response(422, json={"message": "Reference already exists"})

    moved = await _client(handler).upsert_branch(
        "acme/widgets", "themis/learnings", "abc123"
    )

    assert moved is False


async def test_delete_branch__sends_delete():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(204)

    await _client(handler).delete_branch("acme/widgets", "themis/learnings")

    assert captured["method"] == "DELETE"
    assert captured["path"] == "/repos/acme/widgets/git/refs/heads/themis/learnings"


async def test_delete_branch__already_gone__tolerated():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "Reference does not exist"})

    await _client(handler).delete_branch("acme/widgets", "themis/learnings")


async def test_get_file_sha__missing__none():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["ref"] == "themis/learnings"
        return httpx.Response(404, json={})

    sha = await _client(handler).get_file_sha(
        "acme/widgets", ".themis/learnings.jsonl", ref="themis/learnings"
    )
    assert sha is None


async def test_put_file__encodes_content_and_sha():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"commit": {"sha": "digest-tip"}})

    commit_sha = await _client(handler).put_file(
        "acme/widgets", ".themis/learnings.jsonl",
        content='{"id": "lrn-aaaaaaaa"}\n', message="chore: sync review learnings",
        branch="themis/learnings", sha="f00",
    )

    assert commit_sha == "digest-tip"
    assert captured["path"] == "/repos/acme/widgets/contents/.themis/learnings.jsonl"
    body = captured["json"]
    assert base64.b64decode(body["content"]).decode() == '{"id": "lrn-aaaaaaaa"}\n'
    assert body["branch"] == "themis/learnings"
    assert body["sha"] == "f00"


async def test_find_branch_sha__present_and_absent():
    def some(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": {"sha": "abc123"}})

    def none(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    assert await _client(some).find_branch_sha("acme/widgets", "themis/learnings") == (
        "abc123"
    )
    assert await _client(none).find_branch_sha("acme/widgets", "themis/learnings") is None


async def test_find_open_pr__present_and_absent():
    def some(request: httpx.Request) -> httpx.Response:
        assert request.url.params["head"] == "acme:themis/learnings"
        assert request.url.params["state"] == "open"
        return httpx.Response(200, json=[{"number": 12}])

    def none(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    assert await _client(some).find_open_pr("acme/widgets", "themis/learnings") == 12
    assert await _client(none).find_open_pr("acme/widgets", "themis/learnings") is None


async def test_create_pr__returns_number():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["head"] == "themis/learnings" and body["base"] == "main"
        return httpx.Response(201, json={"number": 13})

    number = await _client(handler).create_pr(
        "acme/widgets", title="chore: sync review learnings", body="digest",
        head="themis/learnings", base="main",
    )
    assert number == 13


async def test_list_review_threads__long_thread__follows_comment_pages():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if "reviewThreads" in payload["query"]:
            return httpx.Response(200, json={"data": {"repository": {"pullRequest": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [{
                        "id": "T_1", "isResolved": False, "resolvedBy": None,
                        "path": "a.py", "line": 3,
                        "comments": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "CC_1"},
                            "nodes": [
                                {"databaseId": 1, "body": "finding"},
                                {"databaseId": 2, "body": "accepted - wont fix",
                                 "authorAssociation": "OWNER"},
                            ],
                        },
                    }],
                }}}}})
        return httpx.Response(200, json={"data": {"node": {"comments": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{"databaseId": 3, "body": "tail"}],
        }}}})

    threads = await _client(handler).list_review_threads("acme/widgets", 7)

    # An early acceptance must never be windowed out: the acknowledgment rule
    # reads it from threads.json, so every comment page is fetched.
    nodes = threads[0]["comments"]["nodes"]
    assert [n["databaseId"] for n in nodes] == [1, 2, 3]
    assert requests[1]["variables"] == {"id": "T_1", "cursor": "CC_1"}
    assert "authorAssociation" in requests[1]["query"]


async def test_list_review_threads__runaway_thread__comment_pages_bounded():
    node_queries = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal node_queries
        payload = json.loads(request.content)
        if "reviewThreads" in payload["query"]:
            return httpx.Response(200, json={"data": {"repository": {"pullRequest": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [{
                        "id": "T_1", "isResolved": False, "resolvedBy": None,
                        "path": "a.py", "line": 3,
                        "comments": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "C_0"},
                            "nodes": [{"databaseId": 0, "body": "root"}],
                        },
                    }],
                }}}}})
        node_queries += 1
        return httpx.Response(200, json={"data": {"node": {"comments": {
            "pageInfo": {"hasNextPage": True, "endCursor": f"C_{node_queries}"},
            "nodes": [{"databaseId": node_queries, "body": "reply"}],
        }}}})

    threads = await _client(handler).list_review_threads("acme/widgets", 7)

    # A reply-heavy PR must not stall the review job on one thread: the
    # traversal is bounded, keeping what was fetched so far.
    assert node_queries == MAX_COMMENT_PAGES
    assert len(threads[0]["comments"]["nodes"]) == 1 + MAX_COMMENT_PAGES


async def test_list_review_threads__many_runaway_threads__review_wide_budget():
    node_queries = 0
    threads_nodes = [{
        "id": f"T_{i}", "isResolved": False, "resolvedBy": None,
        "path": "a.py", "line": 3,
        "comments": {
            "pageInfo": {"hasNextPage": True, "endCursor": f"C_{i}_0"},
            "nodes": [{"databaseId": i, "body": "root"}],
        },
    } for i in range(8)]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal node_queries
        if "reviewThreads" in json.loads(request.content)["query"]:
            return httpx.Response(200, json={"data": {"repository": {"pullRequest": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": threads_nodes,
                }}}}})
        node_queries += 1
        return httpx.Response(200, json={"data": {"node": {"comments": {
            "pageInfo": {"hasNextPage": True, "endCursor": f"N_{node_queries}"},
            "nodes": [{"databaseId": 10_000 + node_queries, "body": "reply"}],
        }}}})

    threads = await _client(handler).list_review_threads("acme/widgets", 7)

    # 8 runaway threads x 10-page per-thread cap would be 80 extra calls;
    # the review-wide budget keeps the whole fetch bounded.
    assert node_queries == MAX_COMMENT_PAGES_TOTAL
    assert len(threads) == 8
