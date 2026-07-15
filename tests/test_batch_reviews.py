import asyncio

import pytest

from themis.batch_reviews import BatchReviewService, BatchState, ItemState

pytestmark = pytest.mark.asyncio


class Runner:
    def __init__(self, failures: set[int] | None = None) -> None:
        self.failures = failures or set()
        self.calls: list[tuple[str, int, int]] = []

    async def review(
        self,
        repo: str,
        pr_number: int,
        installation_id: int,
        *,
        auto: bool,
    ) -> None:
        self.calls.append((repo, pr_number, installation_id))
        if pr_number in self.failures:
            raise RuntimeError(f"review failed for {pr_number}")


class Callbacks:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def send(self, url: str, payload: dict[str, object]) -> None:
        self.calls.append((url, payload))


def entries(*numbers: int) -> list[dict[str, object]]:
    return [
        {
            "repo": "acme/widgets",
            "pr_number": number,
            "installation_id": 42,
        }
        for number in numbers
    ]


async def test_create_and_run_batch():
    runner = Runner()
    callbacks = Callbacks()
    service = BatchReviewService(runner, callbacks)
    batch = service.create(
        name="release train",
        requested_by="octocat",
        repositories=entries(1, 2, 3),
        callback_url="https://example.com/done",
    )

    service.start(batch.id)
    result = await service.wait(batch.id)

    assert result.state == BatchState.COMPLETED
    assert result.completed == 3
    assert result.failed == 0
    assert runner.calls == [
        ("acme/widgets", 1, 42),
        ("acme/widgets", 2, 42),
        ("acme/widgets", 3, 42),
    ]
    assert callbacks.calls[0][0] == "https://example.com/done"


async def test_failed_item_marks_batch_failed():
    service = BatchReviewService(Runner({2}), Callbacks())
    batch = service.create(
        name="failure",
        requested_by="octocat",
        repositories=entries(1, 2, 3),
    )

    service.start(batch.id)
    result = await service.wait(batch.id)

    assert result.state == BatchState.FAILED
    assert result.completed == 2
    assert result.failed == 1
    assert result.items[1].error == "review failed for 2"


async def test_retry_failed_items():
    runner = Runner({2})
    service = BatchReviewService(runner, Callbacks())
    batch = service.create(
        name="retry",
        requested_by="octocat",
        repositories=entries(1, 2),
    )
    service.start(batch.id)
    await service.wait(batch.id)
    runner.failures.clear()

    result = await service.retry_failed(batch.id)

    assert result.state == BatchState.COMPLETED
    assert result.items[1].state == ItemState.COMPLETED
    assert result.items[1].attempts == 2


async def test_duplicate_copies_batch_options():
    service = BatchReviewService(Runner(), Callbacks())
    original = service.create(
        name="original",
        requested_by="first",
        repositories=entries(7),
        callback_url="https://example.com/done",
        concurrency=9,
        stop_on_failure=True,
    )

    duplicate = service.duplicate(original.id, requested_by="second")

    assert duplicate.id != original.id
    assert duplicate.name == "Copy of original"
    assert duplicate.requested_by == "second"
    assert duplicate.callback_url == original.callback_url
    assert duplicate.concurrency == 9
    assert duplicate.stop_on_failure is True


async def test_cancel_marks_items_cancelled():
    class SlowRunner(Runner):
        async def review(
            self,
            repo: str,
            pr_number: int,
            installation_id: int,
            *,
            auto: bool,
        ) -> None:
            await asyncio.sleep(10)

    service = BatchReviewService(SlowRunner(), Callbacks())
    batch = service.create(
        name="cancel",
        requested_by="octocat",
        repositories=entries(1, 2),
    )
    service.start(batch.id)
    await asyncio.sleep(0)

    result = service.cancel(batch.id)

    assert result.state == BatchState.CANCELLED
    assert all(item.state == ItemState.CANCELLED for item in result.items)


async def test_metrics_and_purge():
    service = BatchReviewService(Runner(), Callbacks())
    batch = service.create(
        name="metrics",
        requested_by="octocat",
        repositories=entries(1, 2),
    )
    service.start(batch.id)
    await service.wait(batch.id)

    assert service.metrics() == {
        "batches": 1,
        "pending": 0,
        "running": 0,
        "completed": 1,
        "failed": 0,
        "cancelled": 0,
        "items": 2,
    }
    assert service.purge_finished(older_than_seconds=0) == 1
    assert service.list_batches() == []
