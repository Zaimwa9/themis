"""Per-repo learnings memory: model, JSONL codec, pending store, set ops.

The repo file .themis/learnings.jsonl (default branch) is the read truth;
the server-side pending buffer holds captured-but-not-yet-merged entries.
Everything here is deterministic; the LLM only proposes candidates.
"""

import json
import logging
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)

LEARNINGS_REPO_PATH = ".themis/learnings.jsonl"
MAX_TEXT_LEN = 500
MAX_ENTRIES = 200
MAX_TOTAL_BYTES = 50_000


@dataclass(frozen=True)
class Learning:
    id: str
    text: str
    paths: tuple[str, ...] = ()
    learnt_from: str = ""
    pr: int | None = None
    created_at: str = ""
    supersedes: str | None = None


def parse_jsonl(text: str | None) -> list[Learning]:
    """Learnings from JSONL text; malformed lines are skipped with a warning.

    Humans edit the repo file by hand: a broken line must never block a
    review (same stance as parse_repo_config)."""
    if not text:
        return []
    entries: list[Learning] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("themis_learnings_line_skipped line=%d reason=not-json", lineno)
            continue
        if not isinstance(raw, dict):
            logger.warning("themis_learnings_line_skipped line=%d reason=not-object", lineno)
            continue
        id_ = raw.get("id")
        text_ = raw.get("text")
        if not isinstance(id_, str) or not id_.strip() or not isinstance(text_, str) or not text_.strip():
            logger.warning("themis_learnings_line_skipped line=%d reason=missing-id-or-text", lineno)
            continue
        paths_raw = raw.get("paths", [])
        paths = tuple(p for p in paths_raw if isinstance(p, str)) if isinstance(paths_raw, list) else ()
        pr = raw.get("pr")
        supersedes = raw.get("supersedes")
        entries.append(Learning(
            id=id_.strip(),
            text=text_.strip()[:MAX_TEXT_LEN],
            paths=paths,
            learnt_from=str(raw.get("learnt_from") or ""),
            pr=pr if isinstance(pr, int) and not isinstance(pr, bool) else None,
            created_at=str(raw.get("created_at") or ""),
            supersedes=supersedes if isinstance(supersedes, str) and supersedes else None,
        ))
    return entries


def to_jsonl(entries: list[Learning]) -> str:
    lines = []
    for entry in entries:
        record: dict[str, object] = {
            "id": entry.id,
            "text": entry.text,
            "paths": list(entry.paths),
            "learnt_from": entry.learnt_from,
            "pr": entry.pr,
            "created_at": entry.created_at,
        }
        if entry.supersedes:
            record["supersedes"] = entry.supersedes
        lines.append(json.dumps(record, ensure_ascii=False))
    return "\n".join(lines) + ("\n" if lines else "")


def new_learning(
    *,
    text: str,
    paths: tuple[str, ...],
    learnt_from: str,
    pr: int | None,
    created_at: str,
    supersedes: str | None = None,
) -> Learning:
    return Learning(
        id=f"lrn-{uuid.uuid4().hex[:8]}",
        text=text.strip()[:MAX_TEXT_LEN],
        paths=paths,
        learnt_from=learnt_from,
        pr=pr,
        created_at=created_at,
        supersedes=supersedes,
    )
