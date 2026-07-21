"""Parse and validate the files codex writes to .review-output/."""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OUTPUT_DIR = ".review-output"
# Every file the engine may write under OUTPUT_DIR. The agent service redacts
# exactly this set before results cross back to the controller; extend it in
# lockstep with any new parsed file.
OUTPUT_FILES = ("summary.md", "actions.json", "reply.md", "learning.json")
MAX_BODY_LEN = 65000
MAX_FILE_SIZE = 1_000_000
VALID_SIDES = ("LEFT", "RIGHT")
MAX_LEARNING_TEXT = 500
VALID_CONFIDENCE = ("high", "low")
_LEARNING_ID_RE = re.compile(r"lrn-[0-9a-f]{8}")


class OutputError(Exception):
    pass


@dataclass
class ReviewActions:
    summary: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    resolve_thread_ids: list[str] = field(default_factory=list)
    replies: list[dict[str, Any]] = field(default_factory=list)


def _read_capped(path: Path, workspace: Path) -> str:
    try:
        resolved_path = path.resolve(strict=True)
        resolved_workspace = workspace.resolve(strict=True)
    except OSError as error:
        raise OutputError(f"{path.name} could not be resolved: {error}") from error
    # Blocks symlink escapes. Hardlinks are invisible to resolve(); the codex
    # sandbox (and workspace_root living on its own tree) is the boundary there.
    if not resolved_path.is_relative_to(resolved_workspace):
        raise OutputError(f"{path.name} escapes the workspace directory")
    if resolved_path.stat().st_size > MAX_FILE_SIZE:
        raise OutputError(f"{path.name} exceeds maximum size of {MAX_FILE_SIZE} bytes")
    return resolved_path.read_text(errors="replace")


def _check_body_len(label: str, body: str) -> None:
    if len(body) > MAX_BODY_LEN:
        raise OutputError(f"{label} exceeds maximum length of {MAX_BODY_LEN} characters")


def parse_output(workspace: Path) -> ReviewActions:
    out_dir = workspace / OUTPUT_DIR
    summary_path = out_dir / "summary.md"
    if not summary_path.exists():
        raise OutputError(f"agent did not write {OUTPUT_DIR}/summary.md")
    summary = _read_capped(summary_path, workspace).strip()
    if not summary:
        raise OutputError("summary.md is empty")
    _check_body_len("summary.md", summary)

    actions_path = out_dir / "actions.json"
    if not actions_path.exists():
        return ReviewActions(summary=summary)
    try:
        raw = json.loads(_read_capped(actions_path, workspace))
    except json.JSONDecodeError as error:
        raise OutputError(f"actions.json is not valid JSON: {error}") from error

    if not isinstance(raw, dict):
        raise OutputError(f"actions.json root must be an object, got {type(raw).__name__}")

    findings_raw = raw.get("findings", [])
    if not isinstance(findings_raw, list):
        raise OutputError(f"actions.json 'findings' must be a list, got {type(findings_raw).__name__}")

    resolve_raw = raw.get("resolve_thread_ids", [])
    if not isinstance(resolve_raw, list):
        raise OutputError(
            f"actions.json 'resolve_thread_ids' must be a list, got {type(resolve_raw).__name__}"
        )
    for thread_id in resolve_raw:
        if not isinstance(thread_id, str):
            raise OutputError(f"resolve_thread_ids entry must be a string: {thread_id!r}")

    replies_raw = raw.get("replies", [])
    if not isinstance(replies_raw, list):
        raise OutputError(f"actions.json 'replies' must be a list, got {type(replies_raw).__name__}")

    return ReviewActions(
        summary=summary,
        findings=[_validate_finding(f) for f in findings_raw],
        resolve_thread_ids=list(resolve_raw),
        replies=[_validate_reply(r) for r in replies_raw],
    )


def parse_reply(workspace: Path) -> str:
    reply_path = workspace / OUTPUT_DIR / "reply.md"
    if not reply_path.exists():
        raise OutputError(f"agent did not write {OUTPUT_DIR}/reply.md")
    reply = _read_capped(reply_path, workspace).strip()
    if not reply:
        raise OutputError("reply.md is empty")
    _check_body_len("reply.md", reply)
    return reply


def parse_learning(workspace: Path) -> dict[str, Any] | None:
    """Validated learning candidate from learning.json, or None when absent.

    Raises OutputError on invalid content; the caller treats that as
    no-capture, never as a job failure."""
    path = workspace / OUTPUT_DIR / "learning.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(_read_capped(path, workspace))
    except json.JSONDecodeError as error:
        raise OutputError(f"learning.json is not valid JSON: {error}") from error
    if not isinstance(raw, dict):
        raise OutputError(f"learning.json root must be an object, got {type(raw).__name__}")

    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        raise OutputError(f"learning missing or invalid 'text': {raw!r}")
    text = text.strip()
    if len(text) > MAX_LEARNING_TEXT:
        raise OutputError(f"learning 'text' exceeds {MAX_LEARNING_TEXT} characters")

    paths_raw = raw.get("paths", [])
    if not isinstance(paths_raw, list):
        raise OutputError(f"learning 'paths' must be a list, got {type(paths_raw).__name__}")
    for entry in paths_raw:
        if not isinstance(entry, str):
            raise OutputError(f"learning path must be a string: {entry!r}")
        _validate_path(entry)

    supersedes = raw.get("supersedes")
    if supersedes is not None and (
        not isinstance(supersedes, str) or not _LEARNING_ID_RE.fullmatch(supersedes)
    ):
        raise OutputError(f"learning has invalid 'supersedes': {supersedes!r}")

    confidence = raw.get("confidence", "low")
    if confidence not in VALID_CONFIDENCE:
        raise OutputError(f"learning has invalid 'confidence': {confidence!r}")

    return {"text": text, "paths": list(paths_raw), "supersedes": supersedes, "confidence": confidence}


def _is_valid_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_path(path: str) -> None:
    if not path.strip() or path.startswith("/") or ".." in path.split("/"):
        raise OutputError(f"finding path is not allowed: {path!r}")


def _validate_finding(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise OutputError(f"finding must be an object: {raw!r}")

    for key, kind in (("path", str), ("body", str)):
        if not isinstance(raw.get(key), kind):
            raise OutputError(f"finding missing or invalid '{key}': {raw}")

    line = raw.get("line")
    if not _is_valid_int(line) or line < 1:
        raise OutputError(f"finding missing or invalid 'line': {raw}")

    path = raw["path"]
    _validate_path(path)

    body = raw["body"]
    if not body.strip():
        raise OutputError(f"finding has empty 'body': {raw}")
    _check_body_len("finding body", body)

    side = raw.get("side", "RIGHT")
    if side not in VALID_SIDES:
        raise OutputError(f"finding has invalid 'side': {raw}")

    finding: dict[str, Any] = {"path": path, "line": line, "side": side, "body": body}

    if "start_line" in raw:
        start_line = raw.get("start_line")
        if not _is_valid_int(start_line) or start_line < 1 or start_line > line:
            raise OutputError(f"finding has invalid 'start_line': {raw}")
        if start_line < line:
            start_side = raw.get("start_side", side)
            if start_side not in VALID_SIDES:
                raise OutputError(f"finding has invalid 'start_side': {raw}")
            finding["start_line"] = start_line
            finding["start_side"] = start_side
        # start_line == line is a degenerate range; normalise to a single-line
        # comment by dropping start_line/start_side rather than discarding
        # the whole finding.

    return finding


def _validate_reply(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise OutputError(f"reply must be an object: {raw!r}")

    in_reply_to = raw.get("in_reply_to")
    if not _is_valid_int(in_reply_to) or in_reply_to < 1:
        raise OutputError(f"reply missing or invalid 'in_reply_to': {raw}")

    body = raw.get("body")
    if not isinstance(body, str):
        raise OutputError(f"reply missing or invalid 'body': {raw}")
    if not body.strip():
        raise OutputError(f"reply has empty 'body': {raw}")
    _check_body_len("reply body", body)

    return {"in_reply_to": in_reply_to, "body": body}
