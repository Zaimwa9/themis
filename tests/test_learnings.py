"""Learnings model, JSONL codec, pending store, and set operations."""

import json

import pytest

from themis.learnings import (
    Learning,
    PendingStore,
    new_learning,
    parse_jsonl,
    to_jsonl,
)


def _entry(id: str = "lrn-aaaaaaaa", text: str = "Prefer X over Y.", **kw) -> Learning:
    defaults = dict(paths=(), learnt_from="dev", pr=7, created_at="2026-07-13T09:00:00+00:00")
    return Learning(id=id, text=text, **{**defaults, **kw})


def test_parse_jsonl__none_or_empty__empty_list():
    assert parse_jsonl(None) == []
    assert parse_jsonl("") == []
    assert parse_jsonl("\n\n") == []


def test_parse_jsonl__valid_line__parsed():
    line = json.dumps({
        "id": "lrn-aaaaaaaa", "text": "Prefer X.", "paths": ["src/a.py"],
        "learnt_from": "dev", "pr": 7, "created_at": "2026-07-13T09:00:00+00:00",
    })
    entries = parse_jsonl(line)
    assert entries == [_entry(text="Prefer X.", paths=("src/a.py",))]


def test_parse_jsonl__malformed_lines_skipped__valid_kept(caplog):
    text = "\n".join([
        "not json at all",
        json.dumps({"text": "missing id"}),
        json.dumps({"id": "lrn-bbbbbbbb"}),  # missing text
        json.dumps({"id": "lrn-cccccccc", "text": "kept"}),
        json.dumps({"id": "lrn-dddddddd", "text": "", "paths": "notalist"}),
    ])
    entries = parse_jsonl(text)
    assert [e.id for e in entries] == ["lrn-cccccccc"]
    assert "themis_learnings_line_skipped" in caplog.text


def test_parse_jsonl__non_string_paths_filtered():
    line = json.dumps({"id": "lrn-eeeeeeee", "text": "t", "paths": ["ok.py", 3, None]})
    assert parse_jsonl(line)[0].paths == ("ok.py",)


def test_parse_jsonl__overlong_text__truncated():
    line = json.dumps({"id": "lrn-ffffffff", "text": "x" * 600})
    assert len(parse_jsonl(line)[0].text) == 500


def test_to_jsonl__roundtrip():
    entries = [
        _entry(),
        _entry(id="lrn-bbbbbbbb", text="Second rule.", supersedes="lrn-aaaaaaaa"),
    ]
    assert parse_jsonl(to_jsonl(entries)) == entries


def test_to_jsonl__omits_null_supersedes():
    assert "supersedes" not in to_jsonl([_entry()])


def test_new_learning__generates_id_and_carries_fields():
    learning = new_learning(
        text="Rule.", paths=("src/a.py",), learnt_from="dev", pr=9,
        created_at="2026-07-13T10:00:00+00:00", supersedes="lrn-aaaaaaaa",
    )
    assert learning.id.startswith("lrn-") and len(learning.id) == 12
    assert learning.supersedes == "lrn-aaaaaaaa"
    assert learning.pr == 9


pytestmark_async = pytest.mark.asyncio  # module already has sync tests; mark per-test


@pytest.mark.asyncio
async def test_pending_store__load_missing_file__empty(tmp_path):
    store = PendingStore(tmp_path)
    assert await store.load("acme/widgets") == []


@pytest.mark.asyncio
async def test_pending_store__append_then_load__roundtrip(tmp_path):
    store = PendingStore(tmp_path)
    await store.append("acme/widgets", _entry())
    await store.append("acme/widgets", _entry(id="lrn-bbbbbbbb", text="two"))

    entries = await store.load("acme/widgets")

    assert [e.id for e in entries] == ["lrn-aaaaaaaa", "lrn-bbbbbbbb"]
    on_disk = tmp_path / "learnings" / "acme__widgets" / "pending.jsonl"
    assert on_disk.exists()


@pytest.mark.asyncio
async def test_pending_store__repos_isolated(tmp_path):
    store = PendingStore(tmp_path)
    await store.append("acme/widgets", _entry())

    assert await store.load("acme/gadgets") == []


@pytest.mark.asyncio
async def test_pending_store__replace__overwrites(tmp_path):
    store = PendingStore(tmp_path)
    await store.append("acme/widgets", _entry())
    await store.replace("acme/widgets", [])

    assert await store.load("acme/widgets") == []
