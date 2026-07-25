"""Linked issue/PR context: resolve references the PR under review mentions.

A PR description that says "Fixes #12" or links another PR carries review
signal the diff alone cannot: what the change claims to address. The
controller resolves those references with its installation token and hands
the result to the engine as a review input file; the engine itself never
gets GitHub access (issues #82, #79).
"""

import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MAX_LINKED_REFS = 5
MAX_LINKED_BODY_LEN = 4000
# The reference scan is a regex pass over untrusted text; the clamp bounds
# its work the way MAX_SKIP_TITLE_MATCH_LEN bounds title matching. Real
# references ("Fixes #12", a linked PR) live at the top of descriptions.
MAX_SCAN_LEN = 10_000
# GitHub issue numbers are far below this; anything larger is noise.
_MAX_REF_NUMBER = 10**8

# The three spellings GitHub itself autolinks: a full issue/PR URL, a
# cross-repo `owner/repo#N` shorthand, and a bare `#N`. Alternation order
# makes the URL branch win over the bare-`#N` branch at the same position.
# Every number requires a terminator (`/issues/12draft` is not issue 12);
# a skipped ambiguous spelling only costs context, a misfire costs a fetch.
_REF_PATTERN = re.compile(
    r"https://github\.com/(?P<url_owner>[A-Za-z0-9-]+)/(?P<url_repo>[\w.-]+)"
    r"/(?:issues|pull)/(?P<url_number>\d+)(?![\w-])"
    r"|(?<![\w.-])(?P<slug_owner>[A-Za-z0-9-]+)/(?P<slug_repo>[\w.-]+)"
    r"#(?P<slug_number>\d+)(?![\w-])"
    r"|(?<![\w/])#(?P<bare_number>\d+)(?![\w-])"
)


def extract_refs(repo: str, pr_number: int, text: str) -> list[tuple[str, int]]:
    """Ordered, deduplicated `(repo, number)` references found in `text`.

    Same-owner only: the installation token can only ever see repositories
    of its own installation, and the feature is scoped to the organisation
    on purpose — a reference must never make Themis fetch third-party
    content. The PR's own number is not a reference to follow.
    """
    owner = repo.split("/", 1)[0].casefold()
    refs: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = {(repo.casefold(), pr_number)}
    for match in _REF_PATTERN.finditer(text[:MAX_SCAN_LEN]):
        if match["bare_number"] is not None:
            ref_repo = repo
            number = int(match["bare_number"])
        else:
            ref_owner = match["url_owner"] or match["slug_owner"]
            ref_name = match["url_repo"] or match["slug_repo"]
            if ref_owner.casefold() != owner:
                continue
            ref_repo = f"{ref_owner}/{ref_name}"
            number = int(match["url_number"] or match["slug_number"])
        if not 0 < number < _MAX_REF_NUMBER:
            continue
        key = (ref_repo.casefold(), number)
        if key in seen:
            continue
        seen.add(key)
        refs.append((ref_repo, number))
    return refs


async def fetch_linked_context(
    gh: Any, repo: str, pr: dict[str, Any]
) -> list[dict[str, Any]]:
    """Referenced issues/PRs as JSON-ready objects, best effort.

    Context improves a review but must never delay or prevent one: an
    unresolvable reference (deleted, private, malformed payload) is skipped
    with a log line, and the caps bound the extra API calls a hostile
    description can cause.

    Cross-repository references additionally require the referenced
    repository to be **public**, fail closed. The installation token can
    reach private siblings, but the fetched content ends up in a review
    the reviewed repo's readers can see — a PR description must never move
    content across that confidentiality boundary."""
    text = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
    refs = extract_refs(repo, pr.get("number") or 0, text)
    if len(refs) > MAX_LINKED_REFS:
        logger.info(
            "themis_linked_refs_truncated repo=%s count=%d max=%d",
            repo, len(refs), MAX_LINKED_REFS,
        )
        refs = refs[:MAX_LINKED_REFS]
    linked: list[dict[str, Any]] = []
    cross_repo_public: dict[str, bool] = {}
    for ref_repo, number in refs:
        try:
            key = ref_repo.casefold()
            if key != repo.casefold():
                if key not in cross_repo_public:
                    cross_repo_public[key] = (
                        await gh.get_repo_private(ref_repo) is False
                    )
                if not cross_repo_public[key]:
                    logger.info(
                        "themis_linked_ref_skipped repo=%s ref=%s#%d reason=not_public",
                        repo, ref_repo, number,
                    )
                    continue
            issue = await gh.get_issue(ref_repo, number)
            if issue is None:
                logger.info(
                    "themis_linked_issue_missing repo=%s ref=%s#%d",
                    repo, ref_repo, number,
                )
                continue
            body = issue.get("body") or ""
            linked.append({
                "ref": f"{ref_repo}#{number}",
                "type": "pull_request" if issue.get("pull_request") else "issue",
                "repo": ref_repo,
                "number": number,
                "title": issue.get("title"),
                "state": issue.get("state"),
                "author": (issue.get("user") or {}).get("login"),
                "body": body[:MAX_LINKED_BODY_LEN],
                "body_truncated": len(body) > MAX_LINKED_BODY_LEN,
            })
        except (httpx.HTTPError, ValueError, AttributeError, TypeError) as error:
            logger.warning(
                "themis_linked_issue_fetch_failed repo=%s ref=%s#%d error=%s",
                repo, ref_repo, number, str(error)[:200],
            )
    return linked
