import json

import httpx
import pytest

from themis.github.client import SUMMARY_MARKER, GitHubClient, GitHubGraphQLError

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


async def test_list_review_threads__root_outside_tail__merges_root_first_no_dupes():
    root = {"author": {"login": "themis-reviewer"}, "body": "A",
            "databaseId": 1, "createdAt": "2026-01-01T00:00:00Z"}
    tail = [{"author": {"login": "dev"}, "body": body, "databaseId": did,
             "createdAt": "2026-01-02T00:00:00Z"}
            for body, did in (("B", 2), ("C", 3))]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"repository": {"pullRequest": {
            "reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"id": "T_1", "isResolved": False, "path": "a.py", "line": 3,
                           "resolvedBy": None,
                           "rootComments": {"nodes": [root]},
                           "comments": {"nodes": tail}}],
            }}}}})

    threads = await _client(handler).list_review_threads("acme/widgets", 7)

    nodes = threads[0]["comments"]["nodes"]
    assert [n["databaseId"] for n in nodes] == [1, 2, 3]
    assert "rootComments" not in threads[0]


async def test_list_review_threads__root_also_in_tail__deduped():
    root = {"author": {"login": "themis-reviewer"}, "body": "A",
            "databaseId": 1, "createdAt": "2026-01-01T00:00:00Z"}
    tail = [root, {"author": {"login": "dev"}, "body": "B", "databaseId": 2,
                   "createdAt": "2026-01-02T00:00:00Z"}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"repository": {"pullRequest": {
            "reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"id": "T_1", "isResolved": False, "path": "a.py", "line": 3,
                           "resolvedBy": None,
                           "rootComments": {"nodes": [root]},
                           "comments": {"nodes": tail}}],
            }}}}})

    threads = await _client(handler).list_review_threads("acme/widgets", 7)

    nodes = threads[0]["comments"]["nodes"]
    assert [n["databaseId"] for n in nodes] == [1, 2]


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


async def test_get_file_text_missing_file_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    assert await _client(handler).get_file_text("acme/widgets", ".themis/config.yaml") is None


async def test_get_file_text_server_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    with pytest.raises(httpx.HTTPStatusError):
        await _client(handler).get_file_text("acme/widgets", ".themis/config.yaml")
