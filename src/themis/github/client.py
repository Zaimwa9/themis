"""GitHub REST + GraphQL client for themis posting and reads."""

import base64
from typing import Any

import httpx

from themis.github.auth import GITHUB_API_URL

SUMMARY_MARKER = "<!-- themis:summary -->"
_PER_PAGE = 100


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

    async def get_default_branch(self, repo: str) -> str:
        response = await self._client.get(f"{self._api_url}/repos/{repo}")
        response.raise_for_status()
        return str(response.json()["default_branch"])

    async def get_branch_sha(self, repo: str, branch: str) -> str:
        response = await self._client.get(
            f"{self._api_url}/repos/{repo}/git/ref/heads/{branch}"
        )
        response.raise_for_status()
        return str(response.json()["object"]["sha"])

    async def upsert_branch(self, repo: str, branch: str, sha: str) -> None:
        """Force-move branch to sha, creating it when absent.

        Force on purpose: the digest branch is bot-owned and always rebuilt
        from the default branch head; stale digest commits are disposable."""
        response = await self._client.patch(
            f"{self._api_url}/repos/{repo}/git/refs/heads/{branch}",
            json={"sha": sha, "force": True},
        )
        if response.status_code in (404, 422):
            response = await self._client.post(
                f"{self._api_url}/repos/{repo}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": sha},
            )
        response.raise_for_status()

    async def get_file_sha(self, repo: str, path: str, ref: str) -> str | None:
        response = await self._client.get(
            f"{self._api_url}/repos/{repo}/contents/{path}", params={"ref": ref}
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return str(response.json()["sha"])

    async def put_file(
        self, repo: str, path: str, *, content: str, message: str,
        branch: str, sha: str | None = None,
    ) -> None:
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if sha is not None:
            body["sha"] = sha
        response = await self._client.put(
            f"{self._api_url}/repos/{repo}/contents/{path}", json=body
        )
        response.raise_for_status()

    async def find_open_pr(self, repo: str, head_branch: str) -> int | None:
        owner = repo.split("/", 1)[0]
        response = await self._client.get(
            f"{self._api_url}/repos/{repo}/pulls",
            params={"head": f"{owner}:{head_branch}", "state": "open"},
        )
        response.raise_for_status()
        pulls = response.json()
        return int(pulls[0]["number"]) if pulls else None

    async def create_pr(
        self, repo: str, *, title: str, body: str, head: str, base: str
    ) -> int:
        response = await self._client.post(
            f"{self._api_url}/repos/{repo}/pulls",
            json={"title": title, "body": body, "head": head, "base": base},
        )
        response.raise_for_status()
        return int(response.json()["number"])
