import json
from pathlib import Path

import pytest

from reviewbot.output import OutputError, ReviewActions, _read_capped, parse_output, parse_reply


def _write(workspace: Path, summary: str = "ok", actions: dict | None = None) -> None:
    out = workspace / ".review-output"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.md").write_text(summary)
    if actions is not None:
        (out / "actions.json").write_text(json.dumps(actions))


def _outside_secret(tmp_path: Path, content: str) -> Path:
    secret = tmp_path.parent / f"{tmp_path.name}-secret.txt"
    secret.write_text(content)
    return secret


def test_parse_output__summary_and_actions__returns_all(tmp_path: Path):
    _write(tmp_path, "all good", {
        "findings": [{"path": "a.py", "line": 3, "body": "bug here"}],
        "resolve_thread_ids": ["T_1"],
        "replies": [{"in_reply_to": 11, "body": "answered"}],
    })

    actions = parse_output(tmp_path)

    assert actions == ReviewActions(
        summary="all good",
        findings=[{"path": "a.py", "line": 3, "side": "RIGHT", "body": "bug here"}],
        resolve_thread_ids=["T_1"],
        replies=[{"in_reply_to": 11, "body": "answered"}],
    )


def test_parse_output__summary_only__empty_actions(tmp_path: Path):
    _write(tmp_path, "clean PR")

    actions = parse_output(tmp_path)

    assert actions.findings == []
    assert actions.resolve_thread_ids == []
    assert actions.replies == []


def test_parse_output__missing_summary__raises(tmp_path: Path):
    with pytest.raises(OutputError, match="summary.md"):
        parse_output(tmp_path)


def test_parse_output__empty_summary__raises(tmp_path: Path):
    _write(tmp_path, "")
    with pytest.raises(OutputError, match="empty"):
        parse_output(tmp_path)


def test_parse_output__finding_missing_line__raises(tmp_path: Path):
    _write(tmp_path, "s", {"findings": [{"path": "a.py", "body": "x"}]})
    with pytest.raises(OutputError, match="line"):
        parse_output(tmp_path)


def test_parse_output__invalid_json__raises(tmp_path: Path):
    _write(tmp_path, "s")
    (tmp_path / ".review-output" / "actions.json").write_text("{nope")
    with pytest.raises(OutputError, match="JSON"):
        parse_output(tmp_path)


def test_parse_output__multiline_range__keeps_start_line(tmp_path: Path):
    _write(tmp_path, "s", {
        "findings": [{"path": "a.py", "line": 9, "start_line": 5, "body": "x"}],
    })

    actions = parse_output(tmp_path)

    assert actions.findings[0]["start_line"] == 5
    assert actions.findings[0]["start_side"] == "RIGHT"


def test_parse_output__multiline_range_left_side__defaults_start_side_to_side(tmp_path: Path):
    _write(tmp_path, "s", {
        "findings": [{"path": "a.py", "line": 9, "start_line": 5, "side": "LEFT", "body": "x"}],
    })

    actions = parse_output(tmp_path)

    assert actions.findings[0]["start_side"] == "LEFT"


def test_parse_reply__present__returns_text(tmp_path: Path):
    out = tmp_path / ".review-output"
    out.mkdir()
    (out / "reply.md").write_text("the answer")

    assert parse_reply(tmp_path) == "the answer"


def test_parse_reply__missing__raises(tmp_path: Path):
    with pytest.raises(OutputError, match="reply.md"):
        parse_reply(tmp_path)


def test_parse_reply__empty__raises(tmp_path: Path):
    out = tmp_path / ".review-output"
    out.mkdir()
    (out / "reply.md").write_text("   ")

    with pytest.raises(OutputError, match="empty"):
        parse_reply(tmp_path)


def test_parse_output__extra_keys__stripped(tmp_path: Path):
    _write(tmp_path, "s", {
        "findings": [{"path": "a.py", "line": 1, "body": "x", "event": "APPROVE", "commit_id": "abc"}],
    })

    actions = parse_output(tmp_path)

    assert actions.findings[0] == {"path": "a.py", "line": 1, "side": "RIGHT", "body": "x"}


def test_parse_output__non_dict_root__raises(tmp_path: Path):
    _write(tmp_path, "s")
    (tmp_path / ".review-output" / "actions.json").write_text("[1, 2, 3]")

    with pytest.raises(OutputError, match="object"):
        parse_output(tmp_path)


def test_parse_output__findings_not_list__raises(tmp_path: Path):
    _write(tmp_path, "s", {"findings": {"path": "a.py"}})

    with pytest.raises(OutputError, match="findings"):
        parse_output(tmp_path)


def test_parse_output__finding_entry_not_dict__raises(tmp_path: Path):
    _write(tmp_path, "s", {"findings": ["not-a-dict"]})

    with pytest.raises(OutputError, match="finding"):
        parse_output(tmp_path)


def test_parse_output__replies_not_list__raises(tmp_path: Path):
    _write(tmp_path, "s", {"replies": {"in_reply_to": 1, "body": "x"}})

    with pytest.raises(OutputError, match="replies"):
        parse_output(tmp_path)


def test_parse_output__reply_entry_not_dict__raises(tmp_path: Path):
    _write(tmp_path, "s", {"replies": ["not-a-dict"]})

    with pytest.raises(OutputError, match="reply"):
        parse_output(tmp_path)


def test_parse_output__invalid_side__raises(tmp_path: Path):
    _write(tmp_path, "s", {"findings": [{"path": "a.py", "line": 1, "body": "x", "side": "UP"}]})

    with pytest.raises(OutputError, match="side"):
        parse_output(tmp_path)


def test_parse_output__invalid_start_side__raises(tmp_path: Path):
    _write(tmp_path, "s", {
        "findings": [{"path": "a.py", "line": 5, "start_line": 2, "start_side": "UP", "body": "x"}],
    })

    with pytest.raises(OutputError, match="start_side"):
        parse_output(tmp_path)


def test_parse_output__line_bool__raises(tmp_path: Path):
    _write(tmp_path, "s", {"findings": [{"path": "a.py", "line": True, "body": "x"}]})

    with pytest.raises(OutputError, match="line"):
        parse_output(tmp_path)


def test_parse_output__line_zero__raises(tmp_path: Path):
    _write(tmp_path, "s", {"findings": [{"path": "a.py", "line": 0, "body": "x"}]})

    with pytest.raises(OutputError, match="line"):
        parse_output(tmp_path)


def test_parse_output__start_line_wrong_type__raises(tmp_path: Path):
    _write(tmp_path, "s", {"findings": [{"path": "a.py", "line": 5, "start_line": "3", "body": "x"}]})

    with pytest.raises(OutputError, match="start_line"):
        parse_output(tmp_path)


def test_parse_output__start_line_not_less_than_line__raises(tmp_path: Path):
    _write(tmp_path, "s", {"findings": [{"path": "a.py", "line": 5, "start_line": 5, "body": "x"}]})

    with pytest.raises(OutputError, match="start_line"):
        parse_output(tmp_path)


def test_parse_output__resolve_thread_ids_not_list__raises(tmp_path: Path):
    _write(tmp_path, "s", {"resolve_thread_ids": "T_1"})

    with pytest.raises(OutputError, match="resolve_thread_ids"):
        parse_output(tmp_path)


def test_parse_output__resolve_thread_id_not_str__raises(tmp_path: Path):
    _write(tmp_path, "s", {"resolve_thread_ids": [1]})

    with pytest.raises(OutputError, match="resolve_thread_ids"):
        parse_output(tmp_path)


def test_parse_output__in_reply_to_bool__raises(tmp_path: Path):
    _write(tmp_path, "s", {"replies": [{"in_reply_to": True, "body": "x"}]})

    with pytest.raises(OutputError, match="in_reply_to"):
        parse_output(tmp_path)


def test_parse_output__oversized_summary_body__raises(tmp_path: Path):
    _write(tmp_path, "x" * 65001)

    with pytest.raises(OutputError, match="exceeds"):
        parse_output(tmp_path)


def test_parse_output__oversized_finding_body__raises(tmp_path: Path):
    _write(tmp_path, "s", {"findings": [{"path": "a.py", "line": 1, "body": "x" * 65001}]})

    with pytest.raises(OutputError, match="exceeds"):
        parse_output(tmp_path)


def test_parse_output__oversized_reply_body__raises(tmp_path: Path):
    _write(tmp_path, "s", {"replies": [{"in_reply_to": 1, "body": "x" * 65001}]})

    with pytest.raises(OutputError, match="exceeds"):
        parse_output(tmp_path)


def test_parse_output__finding_empty_body__raises(tmp_path: Path):
    _write(tmp_path, "s", {"findings": [{"path": "a.py", "line": 1, "body": ""}]})

    with pytest.raises(OutputError, match="empty"):
        parse_output(tmp_path)


def test_parse_output__finding_whitespace_body__raises(tmp_path: Path):
    _write(tmp_path, "s", {"findings": [{"path": "a.py", "line": 1, "body": "   "}]})

    with pytest.raises(OutputError, match="empty"):
        parse_output(tmp_path)


def test_parse_output__reply_empty_body__raises(tmp_path: Path):
    _write(tmp_path, "s", {"replies": [{"in_reply_to": 1, "body": ""}]})

    with pytest.raises(OutputError, match="empty"):
        parse_output(tmp_path)


def test_parse_output__oversized_file__raises(tmp_path: Path):
    _write(tmp_path, "x" * 1_000_001)

    with pytest.raises(OutputError, match="exceeds"):
        parse_output(tmp_path)


def test_parse_output__path_traversal__raises(tmp_path: Path):
    _write(tmp_path, "s", {"findings": [{"path": "../../etc/passwd", "line": 1, "body": "x"}]})

    with pytest.raises(OutputError, match="path"):
        parse_output(tmp_path)


def test_parse_output__path_absolute__raises(tmp_path: Path):
    _write(tmp_path, "s", {"findings": [{"path": "/etc/passwd", "line": 1, "body": "x"}]})

    with pytest.raises(OutputError, match="path"):
        parse_output(tmp_path)


def test_parse_output__path_empty__raises(tmp_path: Path):
    _write(tmp_path, "s", {"findings": [{"path": "", "line": 1, "body": "x"}]})

    with pytest.raises(OutputError, match="path"):
        parse_output(tmp_path)


def test_parse_output__path_whitespace__raises(tmp_path: Path):
    _write(tmp_path, "s", {"findings": [{"path": "   ", "line": 1, "body": "x"}]})

    with pytest.raises(OutputError, match="path"):
        parse_output(tmp_path)


def test_parse_output__summary_symlink_outside_workspace__raises(tmp_path: Path):
    secret = _outside_secret(tmp_path, "TOP-SECRET-CREDENTIAL")
    out = tmp_path / ".review-output"
    out.mkdir(parents=True)
    (out / "summary.md").symlink_to(secret)

    with pytest.raises(OutputError) as exc_info:
        parse_output(tmp_path)

    assert "TOP-SECRET-CREDENTIAL" not in str(exc_info.value)


def test_parse_output__actions_symlink_outside_workspace__raises(tmp_path: Path):
    secret = _outside_secret(tmp_path, '{"findings": []}')
    _write(tmp_path, "s")
    (tmp_path / ".review-output" / "actions.json").symlink_to(secret)

    with pytest.raises(OutputError) as exc_info:
        parse_output(tmp_path)

    assert "TOP-SECRET-CREDENTIAL" not in str(exc_info.value)


def test_parse_reply__symlink_outside_workspace__raises(tmp_path: Path):
    secret = _outside_secret(tmp_path, "TOP-SECRET-CREDENTIAL")
    out = tmp_path / ".review-output"
    out.mkdir(parents=True)
    (out / "reply.md").symlink_to(secret)

    with pytest.raises(OutputError) as exc_info:
        parse_reply(tmp_path)

    assert "TOP-SECRET-CREDENTIAL" not in str(exc_info.value)


def test_parse_output__summary_symlink_inside_workspace__parses_fine(tmp_path: Path):
    out = tmp_path / ".review-output"
    out.mkdir(parents=True)
    real = out / "real_summary.md"
    real.write_text("the real content")
    (out / "summary.md").symlink_to(real)

    actions = parse_output(tmp_path)

    assert actions.summary == "the real content"


def test_read_capped__dangling_symlink__raises_output_error(tmp_path: Path):
    workspace = tmp_path / "workspace"
    out = workspace / ".review-output"
    out.mkdir(parents=True)
    dangling = out / "summary.md"
    dangling.symlink_to(out / "does-not-exist.md")

    with pytest.raises(OutputError):
        _read_capped(dangling, workspace)
