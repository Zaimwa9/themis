from themis.events import DiscussJob, ReviewJob, parse_event

MENTION = "@test-reviewer"
REPO = "acme/widgets"


def _pr_payload(action: str = "opened", draft: bool = False) -> dict:
    return {
        "action": action,
        "installation": {"id": 42},
        "sender": {"type": "User"},
        "repository": {"full_name": REPO},
        "pull_request": {"number": 7, "draft": draft},
    }


def _issue_comment_payload(body: str, sender_type: str = "User") -> dict:
    return {
        "action": "created",
        "installation": {"id": 42},
        "sender": {"type": sender_type},
        "repository": {"full_name": REPO},
        "issue": {"number": 7, "pull_request": {"url": "https://x"}},
        "comment": {"id": 501, "body": body},
    }


def _review_comment_payload(body: str, in_reply_to: int | None = None) -> dict:
    comment = {"id": 601, "body": body}
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


def test_parse_event__pr_opened__review_job():
    job = parse_event("pull_request", _pr_payload(), MENTION)
    assert job == ReviewJob(repo=REPO, pr_number=7, installation_id=42, auto=True)
    assert job.auto is True


def test_parse_event__pr_ready_for_review__review_job():
    assert parse_event("pull_request", _pr_payload("ready_for_review"), MENTION) is not None


def test_parse_event__pr_draft__none():
    assert parse_event("pull_request", _pr_payload(draft=True), MENTION) is None


def test_parse_event__pr_synchronize__none():
    assert parse_event("pull_request", _pr_payload("synchronize"), MENTION) is None


def test_parse_event__review_command__review_job():
    payload = _issue_comment_payload(f"{MENTION} review")
    job = parse_event("issue_comment", payload, MENTION)
    assert job == ReviewJob(repo=REPO, pr_number=7, installation_id=42, auto=False)
    assert job.auto is False


def test_parse_event__review_command_with_punctuation__review_job():
    payload = _issue_comment_payload(f"{MENTION} review!")
    assert isinstance(parse_event("issue_comment", payload, MENTION), ReviewJob)


def test_parse_event__capitalized_mention__review_job():
    payload = _issue_comment_payload("@Test-Reviewer review")
    assert isinstance(parse_event("issue_comment", payload, MENTION), ReviewJob)


def test_parse_event__mention_of_similar_login__none():
    payload = _issue_comment_payload("cc @test-reviewer-v2 hello")
    assert parse_event("issue_comment", payload, MENTION) is None


def test_parse_event__mention_as_email_local_part__none():
    payload = _issue_comment_payload("foo@test-reviewer review")
    assert parse_event("issue_comment", payload, MENTION) is None


def test_parse_event__mention_in_email_like_text__none():
    payload = _issue_comment_payload("email me at abc@test-reviewer")
    assert parse_event("issue_comment", payload, MENTION) is None


def test_parse_event__mention_wrapped_in_parens__still_recognized():
    payload = _issue_comment_payload(f"({MENTION} review)")
    job = parse_event("issue_comment", payload, MENTION)
    assert isinstance(job, DiscussJob)
    assert job.mentions_bot is True


def test_parse_event__review_with_extra_text__discuss_job():
    payload = _issue_comment_payload(f"{MENTION} review the auth changes")
    assert isinstance(parse_event("issue_comment", payload, MENTION), DiscussJob)


def test_parse_event__bare_mention__none():
    payload = _issue_comment_payload(MENTION)
    assert parse_event("issue_comment", payload, MENTION) is None


def test_parse_event__edited_comment__none():
    payload = _issue_comment_payload(f"{MENTION} review")
    payload["action"] = "edited"
    assert parse_event("issue_comment", payload, MENTION) is None


def test_parse_event__mention_with_question__discuss_job():
    payload = _issue_comment_payload(f"{MENTION} why is this using a semaphore?")
    job = parse_event("issue_comment", payload, MENTION)

    assert isinstance(job, DiscussJob)
    assert job.kind == "conversation"
    assert job.comment_id == 501
    assert job.mentions_bot is True
    assert "semaphore" in job.body


def test_parse_event__comment_without_mention__none():
    assert parse_event("issue_comment", _issue_comment_payload("lgtm"), MENTION) is None


def test_parse_event__comment_from_bot__none():
    payload = _issue_comment_payload(f"{MENTION} review", sender_type="Bot")
    assert parse_event("issue_comment", payload, MENTION) is None


def test_parse_event__comment_on_plain_issue__none():
    payload = _issue_comment_payload(f"{MENTION} review")
    del payload["issue"]["pull_request"]
    assert parse_event("issue_comment", payload, MENTION) is None


def test_parse_event__review_comment_with_mention__thread_discuss_job():
    payload = _review_comment_payload(f"{MENTION} is this really a bug?")
    job = parse_event("pull_request_review_comment", payload, MENTION)

    assert isinstance(job, DiscussJob)
    assert job.kind == "thread"
    assert job.comment_id == 601
    assert job.mentions_bot is True
    assert job.in_reply_to_id is None


def test_parse_event__review_comment_reply_without_mention__thread_discuss_job():
    payload = _review_comment_payload("I disagree, see the guard above", in_reply_to=11)
    job = parse_event("pull_request_review_comment", payload, MENTION)

    assert isinstance(job, DiscussJob)
    assert job.mentions_bot is False
    assert job.in_reply_to_id == 11


def test_parse_event__top_level_review_comment_without_mention__none():
    payload = _review_comment_payload("just a note")
    assert parse_event("pull_request_review_comment", payload, MENTION) is None


def test_parse_event__unknown_event__none():
    assert parse_event("push", {"repository": {"full_name": REPO}}, MENTION) is None
