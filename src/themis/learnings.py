"""Per-repo learnings memory: model, JSONL codec, pending store, set ops.

The repo file .themis/learnings.jsonl (default branch) is the read truth;
the server-side pending buffer holds captured-but-not-yet-merged entries.
Everything here is deterministic; the LLM only proposes candidates.
"""

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

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


class PendingStore:
    """Durable buffer of captured-but-not-yet-merged learnings, per repo.

    One directory per repo under <root>/learnings/. A single lock serializes
    read-modify-write within this process; the queue's single-concurrency
    worker is the writer, so cross-process races are out of scope."""

    def __init__(self, root: Path) -> None:
        self._root = root / "learnings"
        self._lock = asyncio.Lock()

    def _path(self, repo: str) -> Path:
        return self._root / repo.replace("/", "__") / "pending.jsonl"

    async def load(self, repo: str) -> list[Learning]:
        async with self._lock:
            return self._read(repo)

    async def append(self, repo: str, learning: Learning) -> None:
        async with self._lock:
            entries = self._read(repo)
            entries.append(learning)
            self._write(repo, entries)

    async def replace(self, repo: str, entries: list[Learning]) -> None:
        async with self._lock:
            self._write(repo, entries)

    def _read(self, repo: str) -> list[Learning]:
        path = self._path(repo)
        if not path.exists():
            return []
        return parse_jsonl(path.read_text(errors="replace"))

    def _write(self, repo: str, entries: list[Learning]) -> None:
        path = self._path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text(to_jsonl(entries))
        # Atomic rename: torn writes must not destroy previously persisted learnings.
        os.replace(tmp, path)
