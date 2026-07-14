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

    def _flushed_path(self, repo: str) -> Path:
        return self._path(repo).parent / "flushed.json"

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

    async def discard(self, repo: str, ids: set[str]) -> None:
        """Drop pending entries by id (e.g. deleted by a human in the digest PR)."""
        async with self._lock:
            entries = self._read(repo)
            remaining = [e for e in entries if e.id not in ids]
            if len(remaining) != len(entries):
                self._write(repo, remaining)

    async def record_flushed(
        self, repo: str, ids: list[str], pr_number: int | None, sha: str | None = None
    ) -> None:
        """Remember which pending ids were sent to which digest PR, so the next
        read can tell deletions (human-edited out in the PR) from still-pending.
        sha is the digest commit we pushed: post-merge branch cleanup deletes
        the branch only while it still points exactly there. pr_number None
        marks a flush whose branch write landed but whose PR does not exist
        yet (recorded before create_pr, completed by the retry)."""
        async with self._lock:
            path = self._flushed_path(repo)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"ids": ids, "pr": pr_number, "sha": sha}))
            os.replace(tmp, path)

    async def load_flushed(self, repo: str) -> dict | None:
        async with self._lock:
            return self._read_flushed(repo)

    async def clear_flushed(self, repo: str) -> None:
        async with self._lock:
            self._flushed_path(repo).unlink(missing_ok=True)

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

    def _read_flushed(self, repo: str) -> dict | None:
        path = self._flushed_path(repo)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(errors="replace"))
        except json.JSONDecodeError:
            logger.warning("themis_learnings_flushed_invalid repo=%s reason=not-json", repo)
            return None
        ids = raw.get("ids") if isinstance(raw, dict) else None
        pr = raw.get("pr") if isinstance(raw, dict) else None
        valid = (
            isinstance(raw, dict)
            and isinstance(ids, list)
            and all(isinstance(i, str) for i in ids)
            and (pr is None or (isinstance(pr, int) and not isinstance(pr, bool)))
        )
        if not valid:
            logger.warning("themis_learnings_flushed_invalid repo=%s reason=bad-shape", repo)
            return None
        sha = raw.get("sha")
        # Markers written before the sha field (or corrupted): sha None just
        # skips branch cleanup, never invalidates the marker.
        raw["sha"] = sha if isinstance(sha, str) else None
        return raw


def _dedupe_by_id(repo_entries: list[Learning], pending: list[Learning]) -> list[Learning]:
    """Repo-first union: the human-merged repo file wins on id collisions."""
    seen: set[str] = set()
    merged: list[Learning] = []
    for entry in (*repo_entries, *pending):
        if entry.id in seen:
            continue
        seen.add(entry.id)
        merged.append(entry)
    return merged


def _apply_supersedes(entries: list[Learning]) -> list[Learning]:
    superseded = {e.supersedes for e in entries if e.supersedes}
    return [e for e in entries if e.id not in superseded]


def effective_set(repo_entries: list[Learning], pending: list[Learning]) -> list[Learning]:
    """What reviews see: repo + pending, superseded removed, size-capped.

    The cap drops oldest-first (created_at, then original position) and logs
    the count — silent truncation would read as full coverage."""
    merged = _apply_supersedes(_dedupe_by_id(repo_entries, pending))
    position = {entry.id: index for index, entry in enumerate(merged)}
    by_age = sorted(merged, key=lambda e: (e.created_at, position[e.id]))
    dropped = 0
    while len(by_age) > MAX_ENTRIES:
        by_age.pop(0)
        dropped += 1
    while by_age and len(to_jsonl(by_age).encode()) > MAX_TOTAL_BYTES:
        by_age.pop(0)
        dropped += 1
    if dropped:
        logger.warning("themis_learnings_capped dropped=%d", dropped)
    kept = {entry.id for entry in by_age}
    return [entry for entry in merged if entry.id in kept]


def prune_merged(pending: list[Learning], repo_entries: list[Learning]) -> list[Learning]:
    """Pending entries whose id already reached the repo file have merged."""
    repo_ids = {e.id for e in repo_entries}
    return [p for p in pending if p.id not in repo_ids]


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def is_duplicate(text: str, existing: list[Learning]) -> bool:
    norm = _normalize(text)
    return any(_normalize(e.text) == norm for e in existing)


def compose_digest(repo_text: str | None, pending: list[Learning]) -> str:
    """New repo-file content for the digest PR: full merge, no size cap."""
    return to_jsonl(_apply_supersedes(_dedupe_by_id(parse_jsonl(repo_text), pending)))
