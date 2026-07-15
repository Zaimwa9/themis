import json
from unittest.mock import AsyncMock

import pytest

from themis.learning_service import LearningService
from themis.learnings import Learning, PendingStore
from themis.output import OUTPUT_DIR

pytestmark = pytest.mark.asyncio

REPO = "acme/widgets"


def _learning() -> Learning:
    return Learning(
        id="lrn-aaaaaaaa",
        text="Prefer the manager method.",
        paths=("a.py",),
        learnt_from="dev",
        pr=3,
        created_at="2026-07-10T00:00:00+00:00",
    )


async def test_load__pending_entry_becomes_effective_context(tmp_path):
    store = PendingStore(tmp_path / "data")
    entry = _learning()
    await store.append(REPO, entry)
    gh = AsyncMock()
    gh.get_file_text.return_value = None

    effective, pending = await LearningService(store).load(gh, REPO)

    assert effective == [entry]
    assert pending == [entry]


async def test_capture__high_confidence_proposal_is_normalized(tmp_path):
    workspace = tmp_path / "workspace"
    output = workspace / OUTPUT_DIR
    output.mkdir(parents=True)
    (output / "learning.json").write_text(json.dumps({
        "text": "  Prefer the manager method.  ",
        "paths": ["src/manager.py"],
        "confidence": "high",
    }))
    service = LearningService(PendingStore(tmp_path / "data"))

    captured = service.capture(
        workspace, REPO, 7, "maintainer", effective=[], pending=[]
    )

    assert captured is not None
    assert captured.text == "Prefer the manager method."
    assert captured.paths == ("src/manager.py",)
    assert captured.learnt_from == "maintainer"
    assert captured.pr == 7


async def test_capture__low_confidence_proposal_is_ignored(tmp_path):
    workspace = tmp_path / "workspace"
    output = workspace / OUTPUT_DIR
    output.mkdir(parents=True)
    (output / "learning.json").write_text(json.dumps({
        "text": "Maybe use the manager method.",
        "confidence": "low",
    }))
    service = LearningService(PendingStore(tmp_path / "data"))

    captured = service.capture(
        workspace, REPO, 7, "maintainer", effective=[], pending=[]
    )

    assert captured is None


async def test_persist__stores_captured_learning(tmp_path):
    store = PendingStore(tmp_path / "data")
    service = LearningService(store)
    entry = _learning()

    assert await service.persist(REPO, entry) is True
    assert await store.load(REPO) == [entry]
