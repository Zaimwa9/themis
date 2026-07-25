import httpx
from unittest.mock import AsyncMock

from themis.linked_context import (
    MAX_LINKED_BODY_LEN,
    MAX_LINKED_REFS,
    MAX_SCAN_LEN,
    extract_refs,
    fetch_linked_context,
)

REPO = "acme/widgets"


# --- extract_refs -------------------------------------------------------------


def test_extract__bare_hash__resolves_to_own_repo():
    assert extract_refs(REPO, 7, "Fixes #12") == [(REPO, 12)]


def test_extract__own_pr_number__excluded():
    assert extract_refs(REPO, 7, "supersedes #7, see #8") == [(REPO, 8)]


def test_extract__duplicates__deduplicated_in_order():
    text = "Fixes #12, see #34 and again #12 and acme/widgets#34"
    assert extract_refs(REPO, 7, text) == [(REPO, 12), (REPO, 34)]


def test_extract__cross_repo_slug__same_owner_only():
    text = "see acme/gadgets#3 and evil/mallory#4"
    assert extract_refs(REPO, 7, text) == [("acme/gadgets", 3)]


def test_extract__owner_case_insensitive():
    assert extract_refs(REPO, 7, "see Acme/gadgets#3") == [("Acme/gadgets", 3)]


def test_extract__urls__issue_and_pull_same_owner_only():
    text = (
        "https://github.com/acme/widgets/issues/5 then "
        "https://github.com/acme/gadgets/pull/6#issuecomment-1 then "
        "https://github.com/evil/mallory/issues/9"
    )
    assert extract_refs(REPO, 7, text) == [(REPO, 5), ("acme/gadgets", 6)]


def test_extract__url_and_bare_spellings_of_same_ref__one_entry():
    text = "#5 and https://github.com/acme/widgets/pull/5"
    assert extract_refs(REPO, 7, text) == [(REPO, 5)]


def test_extract__word_adjacent_hash__not_a_reference():
    # GitHub does not autolink `sha#1` or css anchors; neither do we.
    assert extract_refs(REPO, 7, "deadbeef#1 and path/to#2") == []


def test_extract__zero_and_absurd_numbers__ignored():
    assert extract_refs(REPO, 7, "#0 and #999999999999") == []


def test_extract__scan_clamped__late_refs_ignored():
    text = "x" * MAX_SCAN_LEN + " #12"
    assert extract_refs(REPO, 7, text) == []


def test_extract__no_text__empty():
    assert extract_refs(REPO, 7, "") == []


# --- fetch_linked_context -----------------------------------------------------


def _pr(body: str, number: int = 7, title: str = "Fix") -> dict:
    return {"number": number, "title": title, "body": body}


def _issue(number: int, *, body: str = "details", pull: bool = False) -> dict:
    payload = {
        "number": number,
        "title": f"Issue {number}",
        "state": "open",
        "body": body,
        "user": {"login": "dev"},
    }
    if pull:
        payload["pull_request"] = {"url": "..."}
    return payload


async def test_fetch__issue_and_pr__typed_objects():
    gh = AsyncMock()
    gh.get_issue.side_effect = [_issue(12), _issue(34, pull=True)]

    linked = await fetch_linked_context(gh, REPO, _pr("Fixes #12, follows #34"))

    assert [item["ref"] for item in linked] == ["acme/widgets#12", "acme/widgets#34"]
    assert [item["type"] for item in linked] == ["issue", "pull_request"]
    assert linked[0]["title"] == "Issue 12"
    assert linked[0]["author"] == "dev"
    assert linked[0]["body_truncated"] is False


async def test_fetch__missing_reference__skipped():
    gh = AsyncMock()
    gh.get_issue.side_effect = [None, _issue(34)]

    linked = await fetch_linked_context(gh, REPO, _pr("#12 #34"))

    assert [item["ref"] for item in linked] == ["acme/widgets#34"]


async def test_fetch__http_error__skips_that_ref_and_continues():
    gh = AsyncMock()
    gh.get_issue.side_effect = [httpx.ConnectError("boom"), _issue(34)]

    linked = await fetch_linked_context(gh, REPO, _pr("#12 #34"))

    assert [item["ref"] for item in linked] == ["acme/widgets#34"]


async def test_fetch__malformed_payload__skipped():
    gh = AsyncMock()
    gh.get_issue.side_effect = [["not", "a", "dict"], _issue(34)]

    linked = await fetch_linked_context(gh, REPO, _pr("#12 #34"))

    assert [item["ref"] for item in linked] == ["acme/widgets#34"]


async def test_fetch__long_body__truncated_and_flagged():
    gh = AsyncMock()
    gh.get_issue.return_value = _issue(12, body="x" * (MAX_LINKED_BODY_LEN + 1))

    linked = await fetch_linked_context(gh, REPO, _pr("#12"))

    assert len(linked[0]["body"]) == MAX_LINKED_BODY_LEN
    assert linked[0]["body_truncated"] is True


async def test_fetch__ref_flood__capped_fetches():
    gh = AsyncMock()
    gh.get_issue.side_effect = [_issue(n) for n in range(1, MAX_LINKED_REFS + 1)]
    body = " ".join(f"#{n}" for n in range(1, MAX_LINKED_REFS + 4))

    linked = await fetch_linked_context(gh, REPO, _pr(body, number=99))

    assert len(linked) == MAX_LINKED_REFS
    assert gh.get_issue.await_count == MAX_LINKED_REFS


async def test_fetch__no_refs__no_api_calls():
    gh = AsyncMock()

    assert await fetch_linked_context(gh, REPO, _pr("plain description")) == []
    gh.get_issue.assert_not_awaited()
