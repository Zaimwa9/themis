"""Learnings model, JSONL codec, pending store, and set operations."""

import json

import pytest

from themis.learnings import (
    Learning,
    PendingStore,
    compose_digest,
    effective_set,
    is_duplicate,
    new_learning,
    parse_jsonl,
    prune_merged,
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


@pytest.mark.asyncio
async def test_pending_store__discard__removes_matching_ids(tmp_path):
    store = PendingStore(tmp_path)
    await store.append("acme/widgets", _entry())
    await store.append("acme/widgets", _entry(id="lrn-bbbbbbbb", text="two"))

    await store.discard("acme/widgets", {"lrn-aaaaaaaa"})

    assert [e.id for e in await store.load("acme/widgets")] == ["lrn-bbbbbbbb"]


@pytest.mark.asyncio
async def test_pending_store__discard__no_match__noop(tmp_path):
    store = PendingStore(tmp_path)
    await store.append("acme/widgets", _entry())

    await store.discard("acme/widgets", {"lrn-zzzzzzzz"})

    assert [e.id for e in await store.load("acme/widgets")] == ["lrn-aaaaaaaa"]


@pytest.mark.asyncio
async def test_pending_store__record_flushed_then_load__roundtrip(tmp_path):
    store = PendingStore(tmp_path)

    await store.record_flushed(
        "acme/widgets", ["lrn-aaaaaaaa", "lrn-bbbbbbbb"], 42, sha="digest-tip"
    )

    assert await store.load_flushed("acme/widgets") == {
        "ids": ["lrn-aaaaaaaa", "lrn-bbbbbbbb"], "pr": 42, "sha": "digest-tip",
    }
    on_disk = tmp_path / "learnings" / "acme__widgets" / "flushed.json"
    assert on_disk.exists()


@pytest.mark.asyncio
async def test_pending_store__load_flushed__marker_without_sha__sha_none(tmp_path):
    """Markers written before the sha field existed stay readable."""
    store = PendingStore(tmp_path)
    path = tmp_path / "learnings" / "acme__widgets" / "flushed.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"ids": ["lrn-aaaaaaaa"], "pr": 42}')

    assert await store.load_flushed("acme/widgets") == {
        "ids": ["lrn-aaaaaaaa"], "pr": 42, "sha": None,
    }


@pytest.mark.asyncio
async def test_pending_store__load_flushed__missing__none(tmp_path):
    store = PendingStore(tmp_path)
    assert await store.load_flushed("acme/widgets") is None


@pytest.mark.asyncio
async def test_pending_store__load_flushed__malformed_json__none_and_warns(tmp_path, caplog):
    store = PendingStore(tmp_path)
    path = tmp_path / "learnings" / "acme__widgets"
    path.mkdir(parents=True)
    (path / "flushed.json").write_text("{not json")

    assert await store.load_flushed("acme/widgets") is None
    assert "themis_learnings_flushed_invalid" in caplog.text


@pytest.mark.asyncio
async def test_pending_store__load_flushed__wrong_shape__none_and_warns(tmp_path, caplog):
    store = PendingStore(tmp_path)
    path = tmp_path / "learnings" / "acme__widgets"
    path.mkdir(parents=True)
    (path / "flushed.json").write_text(json.dumps({"ids": "not-a-list", "pr": 42}))

    assert await store.load_flushed("acme/widgets") is None
    assert "themis_learnings_flushed_invalid" in caplog.text


@pytest.mark.asyncio
async def test_pending_store__clear_flushed__missing_ok(tmp_path):
    store = PendingStore(tmp_path)
    await store.clear_flushed("acme/widgets")  # no raise


@pytest.mark.asyncio
async def test_pending_store__clear_flushed__removes_marker(tmp_path):
    store = PendingStore(tmp_path)
    await store.record_flushed("acme/widgets", ["lrn-aaaaaaaa"], 42)

    await store.clear_flushed("acme/widgets")

    assert await store.load_flushed("acme/widgets") is None


def test_effective_set__dedupes_by_id_repo_wins():
    repo = [_entry(text="repo version")]
    pending = [_entry(text="pending version"), _entry(id="lrn-bbbbbbbb", text="new")]

    merged = effective_set(repo, pending)

    assert [e.id for e in merged] == ["lrn-aaaaaaaa", "lrn-bbbbbbbb"]
    assert merged[0].text == "repo version"


def test_effective_set__supersedes_removes_target():
    repo = [_entry()]
    pending = [_entry(id="lrn-bbbbbbbb", text="replacement", supersedes="lrn-aaaaaaaa")]

    merged = effective_set(repo, pending)

    assert [e.id for e in merged] == ["lrn-bbbbbbbb"]


def test_effective_set__caps_entries_dropping_oldest_first(caplog):
    repo = [
        _entry(id=f"lrn-{i:08d}", text=f"rule {i}", created_at=f"2026-01-{(i % 28) + 1:02d}")
        for i in range(250)
    ]

    merged = effective_set(repo, [])

    assert len(merged) == 200
    assert "themis_learnings_capped" in caplog.text


def test_effective_set__caps_total_bytes(caplog):
    repo = [_entry(id=f"lrn-{i:08d}", text="x" * 490) for i in range(150)]

    merged = effective_set(repo, [])

    from themis.learnings import to_jsonl as _to
    assert len(_to(merged).encode()) <= 50_000
    assert "themis_learnings_capped" in caplog.text


def test_prune_merged__drops_pending_present_in_repo():
    pending = [_entry(), _entry(id="lrn-bbbbbbbb")]
    repo = [_entry()]

    assert [e.id for e in prune_merged(pending, repo)] == ["lrn-bbbbbbbb"]


def test_is_duplicate__normalizes_whitespace_and_case():
    existing = [_entry(text="Prefer   X over Y.")]
    assert is_duplicate("prefer x OVER y.", existing) is True
    assert is_duplicate("prefer z", existing) is False


def test_compose_digest__merges_and_applies_supersedes():
    repo_text = to_jsonl([_entry()])
    pending = [_entry(id="lrn-bbbbbbbb", text="replacement", supersedes="lrn-aaaaaaaa")]

    out = compose_digest(repo_text, pending)

    entries = parse_jsonl(out)
    assert [e.id for e in entries] == ["lrn-bbbbbbbb"]
    assert out.endswith("\n")


def test_compose_digest__no_repo_file__pending_only():
    entries = parse_jsonl(compose_digest(None, [_entry()]))
    assert [e.id for e in entries] == ["lrn-aaaaaaaa"]


def test_effective_set__equal_timestamps__earliest_positions_dropped_first():
    entries = [
        _entry(id=f"lrn-{i:08d}", text=f"rule {i}", created_at="2026-01-01T00:00:00+00:00")
        for i in range(201)
    ]

    merged = effective_set(entries, [])

    assert [e.id for e in merged] == [f"lrn-{i:08d}" for i in range(1, 201)]
