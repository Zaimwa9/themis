"""GitHub REST + GraphQL client for themis posting and reads."""

from typing import Any

import httpx

from themis.github.auth import GITHUB_API_URL

SUMMARY_MARKER = "<!-- themis:summary -->"
_PER_PAGE = 100
_FAILED_CHECK_CONCLUSIONS = {
    "action_required",
    "cancelled",
    "failure",
    "stale",
    "startup_failure",
    "timed_out",
}


class GitHubGraphQLError(Exception):
    """GraphQL request returned an errors array (HTTP 200)."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__(f"GitHub GraphQL errors: {errors}")
        self.errors = errors

_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          resolvedBy { login }
          path
          line
          rootComments: comments(first: 1) {
            nodes { author { login } body databaseId createdAt }
          }
          comments(last: 50) {
            nodes { author { login } body databaseId createdAt }
          }
        }
      }
    }
  }
}
"""

def _merge_thread_comments(thread: dict[str, Any]) -> dict[str, Any]:
    """Collapse the rootComments+comments aliases into a single comment list.

    reviewThreads fetches the root comment (first: 1) and the recent tail
    (last: 50) separately so neither is lost in long threads. Consumers read
    thread["comments"]["nodes"]; normalize to root-first + tail, deduped by
    databaseId (the root reappears in the tail on short threads)."""
    root_nodes = thread.pop("rootComments", {}).get("nodes", [])
    tail_nodes = thread.get("comments", {}).get("nodes", [])
    merged: list[dict[str, Any]] = []
    seen: set[int] = set()
    for node in (*root_nodes, *tail_nodes):
        database_id = node.get("databaseId")
        if database_id is not None:
            if database_id in seen:
                continue
            seen.add(database_id)
        merged.append(node)
    thread["comments"] = {"nodes": merged}
    return thread


_RESOLVE_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) { thread { id } }
}
"""


class GitHubClient:
    def __init__(
        self,
        token: str,
        api_url: str = GITHUB_API_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_url = api_url
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "GitHubClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(
            f"{self._api_url}/graphql",
            json={"query": query, "variables": variables},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise GitHubGraphQLError(payload["errors"])
        return payload["data"]

    async def _paginate(self, url: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            response = await self._client.get(url, params={"per_page": _PER_PAGE, "page": page})
            response.raise_for_status()
            batch = response.json()
            items.extend(batch)
            if len(batch) < _PER_PAGE:
                return items
            page += 1

    async def get_pr(self, repo: str, number: int) -> dict[str, Any]:
        response = await self._client.get(f"{self._api_url}/repos/{repo}/pulls/{number}")
        response.raise_for_status()
        return dict(response.json())

    async def get_ci_snapshot(self, repo: str, commit_sha: str) -> dict[str, Any]:
        """Return one non-blocking snapshot of checks and legacy statuses.

        Each API is allowed to fail independently. A partial read is marked
        `unavailable` unless the visible evidence already establishes failure;
        no retry or polling happens here.
        """
        checks: list[dict[str, Any]] = []
        unavailable_sources: list[str] = []

        try:
            page = 1
            while True:
                response = await self._client.get(
                    f"{self._api_url}/repos/{repo}/commits/{commit_sha}/check-runs",
                    params={"filter": "latest", "per_page": _PER_PAGE, "page": page},
                )
                response.raise_for_status()
                batch = response.json().get("check_runs", [])
                checks.extend({
                    "type": "check_run",
                    "name": item.get("name"),
                    "status": item.get("status"),
                    "conclusion": item.get("conclusion"),
                    "details_url": item.get("details_url"),
                } for item in batch)
                if len(batch) < _PER_PAGE:
                    break
                page += 1
        except (httpx.HTTPError, ValueError, AttributeError):
            unavailable_sources.append("check_runs")

        try:
            statuses = await self._paginate(
                f"{self._api_url}/repos/{repo}/commits/{commit_sha}/statuses"
            )
            # GitHub returns newest first. Keep only the latest status for each
            # context so an old failure cannot override a later success.
            seen_contexts: set[str] = set()
            for item in statuses:
                context = item.get("context") or "unnamed status"
                if context in seen_contexts:
                    continue
                seen_contexts.add(context)
                state = item.get("state")
                checks.append({
                    "type": "status",
                    "name": context,
                    "status": "in_progress" if state == "pending" else "completed",
                    "conclusion": state,
                    "details_url": item.get("target_url"),
                })
        except (httpx.HTTPError, ValueError, AttributeError):
            unavailable_sources.append("statuses")

        if any(
            check["conclusion"] in _FAILED_CHECK_CONCLUSIONS | {"error"}
            for check in checks
        ):
            state = "failed"
        elif unavailable_sources:
            # Do not call the aggregate passed/pending/none when one source is
            # invisible: an unread check may carry the opposite result.
            state = "unavailable"
        elif any(
            check["status"] != "completed" or check["conclusion"] is None
            for check in checks
        ):
            state = "pending"
        elif checks:
            state = "passed"
        else:
            state = "none"

        return {
            "state": state,
            "head_sha": commit_sha,
            "checks": checks,
            "unavailable_sources": unavailable_sources,
        }

    async def post_review(
        self, repo: str, number: int, commit_sha: str, comments: list[dict[str, Any]]
    ) -> None:
        response = await self._client.post(
            f"{self._api_url}/repos/{repo}/pulls/{number}/reviews",
            json={"commit_id": commit_sha, "event": "COMMENT", "body": "", "comments": comments},
        )
        response.raise_for_status()

    async def post_issue_comment(self, repo: str, number: int, body: str) -> None:
        response = await self._client.post(
            f"{self._api_url}/repos/{repo}/issues/{number}/comments",
            json={"body": body},
        )
        response.raise_for_status()

    async def post_summary_comment(self, repo: str, number: int, body: str) -> None:
        """Every review posts a fresh summary comment; old ones stay as history."""
        await self.post_issue_comment(repo, number, f"{SUMMARY_MARKER}\n{body}")

    async def list_pr_files(self, repo: str, number: int) -> list[str]:
        """All changed file paths in the PR (paginated; authoritative merge-base diff)."""
        files = await self._paginate(f"{self._api_url}/repos/{repo}/pulls/{number}/files")
        paths: list[str] = []
        for f in files:
            paths.append(f["filename"])
            previous_filename = f.get("previous_filename")
            if previous_filename:
                paths.append(previous_filename)
        return paths

    async def list_review_threads(self, repo: str, number: int) -> list[dict[str, Any]]:
        owner, name = repo.split("/", 1)
        threads: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            data = await self._graphql(
                _THREADS_QUERY,
                {"owner": owner, "name": name, "number": number, "cursor": cursor},
            )
            page = data["repository"]["pullRequest"]["reviewThreads"]
            threads.extend(_merge_thread_comments(node) for node in page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return threads
            cursor = page["pageInfo"]["endCursor"]

    async def resolve_thread(self, thread_id: str) -> None:
        await self._graphql(_RESOLVE_MUTATION, {"threadId": thread_id})

    async def post_reply(self, repo: str, number: int, in_reply_to: int, body: str) -> None:
        response = await self._client.post(
            f"{self._api_url}/repos/{repo}/pulls/{number}/comments/{in_reply_to}/replies",
            json={"body": body},
        )
        response.raise_for_status()

    async def add_reaction(
        self,
        repo: str,
        *,
        content: str = "eyes",
        issue_comment_id: int | None = None,
        review_comment_id: int | None = None,
        issue_number: int | None = None,
    ) -> None:
        targets = (issue_comment_id, review_comment_id, issue_number)
        if sum(target is not None for target in targets) != 1:
            raise ValueError(
                "exactly one of issue_comment_id, review_comment_id, issue_number is required"
            )
        if issue_comment_id is not None:
            url = f"{self._api_url}/repos/{repo}/issues/comments/{issue_comment_id}/reactions"
        elif review_comment_id is not None:
            url = f"{self._api_url}/repos/{repo}/pulls/comments/{review_comment_id}/reactions"
        else:
            url = f"{self._api_url}/repos/{repo}/issues/{issue_number}/reactions"
        response = await self._client.post(url, json={"content": content})
        response.raise_for_status()

    async def get_file_text(self, repo: str, path: str) -> str | None:
        """File content from the repo's default branch, or None when absent.

        Default branch on purpose: per-repo behavior config must not be
        overridable from inside the PR under review.
        """
        response = await self._client.get(
            f"{self._api_url}/repos/{repo}/contents/{path}",
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.text
