"""GitHub REST + GraphQL client for themis posting and reads."""

import base64
import logging
from typing import Any

import httpx

from themis.github.auth import GITHUB_API_URL

logger = logging.getLogger(__name__)

SUMMARY_MARKER = "<!-- themis:summary -->"
_PER_PAGE = 100
# Ceilings on extra comment pages fetched (100 comments each, on top of the
# 100 inlined in the threads query): per thread, and across the whole review
# so many busy threads cannot add up to an unbounded pre-review fetch either.
# High enough that no real conversation hits them; low enough that reply
# volume can never stall the review job before it posts.
MAX_COMMENT_PAGES = 10
MAX_COMMENT_PAGES_TOTAL = 50
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
          comments(first: 100) {
            pageInfo { hasNextPage endCursor }
            nodes { author { login } authorAssociation body databaseId createdAt }
          }
        }
      }
    }
  }
}
"""

_THREAD_COMMENTS_QUERY = """
query($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { author { login } authorAssociation body databaseId createdAt }
      }
    }
  }
}
"""


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
        budget = [MAX_COMMENT_PAGES_TOTAL]  # shared across every thread
        while True:
            data = await self._graphql(
                _THREADS_QUERY,
                {"owner": owner, "name": name, "number": number, "cursor": cursor},
            )
            page = data["repository"]["pullRequest"]["reviewThreads"]
            for node in page["nodes"]:
                threads.append(await self._fill_thread_comments(node, budget))
            if not page["pageInfo"]["hasNextPage"]:
                return threads
            cursor = page["pageInfo"]["endCursor"]

    async def _fill_thread_comments(
        self, thread: dict[str, Any], budget: list[int]
    ) -> dict[str, Any]:
        """Fetch every comment page of a thread, root-first.

        The acknowledgment rule reads acceptance replies straight from
        threads.json; a windowed comment list would silently drop an
        acceptance in the middle of a long thread and keep its finding
        open forever. The traversal is still bounded - per thread
        (MAX_COMMENT_PAGES) and across the review (the shared budget) -
        so reply volume can never stall the review job."""
        comments = thread.setdefault("comments", {"nodes": []})
        page_info = comments.get("pageInfo") or {}
        pages = 0
        while page_info.get("hasNextPage"):
            if pages >= MAX_COMMENT_PAGES or budget[0] <= 0:
                logger.warning(
                    "themis_thread_comments_truncated thread=%s pages=%d budget=%d",
                    thread.get("id"), pages, budget[0],
                )
                break
            budget[0] -= 1
            data = await self._graphql(
                _THREAD_COMMENTS_QUERY,
                {"id": thread["id"], "cursor": page_info.get("endCursor")},
            )
            page = data["node"]["comments"]
            comments["nodes"].extend(page["nodes"])
            page_info = page.get("pageInfo") or {}
            pages += 1
        comments.pop("pageInfo", None)
        return thread

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

    async def get_file_text(
        self, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        """File content, or None when absent.

        Defaults to the repo's default branch on purpose: per-repo behavior
        config must not be overridable from inside the PR under review. An
        explicit ref is for bot-owned branches (the learnings digest).
        """
        response = await self._client.get(
            f"{self._api_url}/repos/{repo}/contents/{path}",
            headers={"Accept": "application/vnd.github.raw+json"},
            params={"ref": ref} if ref is not None else None,
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

    async def find_branch_sha(self, repo: str, branch: str) -> str | None:
        """Branch tip sha, or None when the branch does not exist."""
        response = await self._client.get(
            f"{self._api_url}/repos/{repo}/git/ref/heads/{branch}"
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return str(response.json()["object"]["sha"])

    async def upsert_branch(self, repo: str, branch: str, sha: str) -> bool:
        """Fast-forward branch to sha, creating it when absent.

        Never force: a branch holding commits that are not on the default
        branch — a human's branch with the same name, or a closed digest
        PR's edits — must not be reset. GitHub refuses the non-fast-forward
        move atomically, so no racing push can be lost. Returns False when
        the branch exists but cannot be fast-forwarded."""
        response = await self._client.patch(
            f"{self._api_url}/repos/{repo}/git/refs/heads/{branch}",
            json={"sha": sha, "force": False},
        )
        if response.status_code in (404, 422):
            create = await self._client.post(
                f"{self._api_url}/repos/{repo}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": sha},
            )
            if create.status_code == 422:
                # Creation refused because the ref exists, so the PATCH 422
                # was a rejected non-fast-forward move, not a missing ref.
                return False
            create.raise_for_status()
            return True
        response.raise_for_status()
        return True

    async def delete_branch(self, repo: str, branch: str) -> None:
        """Delete a ref; already-absent (e.g. auto-delete on merge) is fine."""
        response = await self._client.delete(
            f"{self._api_url}/repos/{repo}/git/refs/heads/{branch}"
        )
        if response.status_code in (404, 422):
            return
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
    ) -> str:
        """Returns the sha of the commit the write created."""
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
        return str(response.json()["commit"]["sha"])

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
