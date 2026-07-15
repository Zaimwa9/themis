"""Experimental batch review coordination.

This module intentionally keeps the first implementation in one place while
the batch API is being validated.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

logger = logging.getLogger(__name__)


class BatchState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ItemState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class BatchItem:
    repo: str
    pr_number: int
    installation_id: int
    state: ItemState = ItemState.PENDING
    attempts: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.pr_number}"

    def as_dict(self) -> dict[str, object]:
        return {
            "repo": self.repo,
            "pr_number": self.pr_number,
            "installation_id": self.installation_id,
            "state": self.state.value,
            "attempts": self.attempts,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


@dataclass
class Batch:
    name: str
    requested_by: str
    items: list[BatchItem]
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    state: BatchState = BatchState.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    callback_url: str | None = None
    concurrency: int = 3
    stop_on_failure: bool = False

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def completed(self) -> int:
        return sum(item.state == ItemState.COMPLETED for item in self.items)

    @property
    def failed(self) -> int:
        return sum(item.state == ItemState.FAILED for item in self.items)

    @property
    def pending(self) -> int:
        return sum(item.state == ItemState.PENDING for item in self.items)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "requested_by": self.requested_by,
            "state": self.state.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "callback_url": self.callback_url,
            "concurrency": self.concurrency,
            "stop_on_failure": self.stop_on_failure,
            "total": self.total,
            "completed": self.completed,
            "failed": self.failed,
            "pending": self.pending,
            "items": [item.as_dict() for item in self.items],
        }


class ReviewRunner(Protocol):
    async def review(
        self,
        repo: str,
        pr_number: int,
        installation_id: int,
        *,
        auto: bool,
    ) -> None: ...


class CallbackSender(Protocol):
    async def send(self, url: str, payload: dict[str, object]) -> None: ...


class BatchReviewService:
    """Create, execute, query, cancel, retry, and report review batches."""

    def __init__(
        self,
        review_runner: ReviewRunner,
        callback_sender: CallbackSender,
    ) -> None:
        self.review_runner = review_runner
        self.callback_sender = callback_sender
        self.batches: dict[str, Batch] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}

    def create(
        self,
        *,
        name: str,
        requested_by: str,
        repositories: list[dict[str, object]],
        callback_url: str | None = None,
        concurrency: int = 3,
        stop_on_failure: bool = False,
    ) -> Batch:
        items = [
            BatchItem(
                repo=str(entry["repo"]),
                pr_number=int(entry["pr_number"]),
                installation_id=int(entry["installation_id"]),
            )
            for entry in repositories
        ]
        batch = Batch(
            name=name,
            requested_by=requested_by,
            items=items,
            callback_url=callback_url,
            concurrency=concurrency,
            stop_on_failure=stop_on_failure,
        )
        self.batches[batch.id] = batch
        logger.info(
            "themis_batch_created id=%s requested_by=%s callback=%s items=%s",
            batch.id,
            requested_by,
            callback_url,
            len(items),
        )
        return batch

    def list_batches(self) -> list[Batch]:
        return sorted(
            self.batches.values(),
            key=lambda batch: batch.created_at,
            reverse=True,
        )

    def get(self, batch_id: str) -> Batch:
        return self.batches[batch_id]

    def start(self, batch_id: str) -> Batch:
        batch = self.get(batch_id)
        batch.state = BatchState.RUNNING
        batch.started_at = time.time()
        self.tasks[batch_id] = asyncio.create_task(self._run(batch))
        logger.info(
            "themis_batch_started id=%s requested_by=%s",
            batch.id,
            batch.requested_by,
        )
        return batch

    async def _run(self, batch: Batch) -> None:
        semaphore = asyncio.Semaphore(batch.concurrency)

        async def run_item(item: BatchItem) -> None:
            async with semaphore:
                if batch.state == BatchState.CANCELLED:
                    item.state = ItemState.CANCELLED
                    return
                item.state = ItemState.RUNNING
                item.started_at = time.time()
                item.attempts += 1
                try:
                    await self.review_runner.review(
                        item.repo,
                        item.pr_number,
                        item.installation_id,
                        auto=False,
                    )
                except Exception as error:
                    item.state = ItemState.FAILED
                    item.error = str(error)
                    logger.exception(
                        "themis_batch_item_failed id=%s repo=%s pr=%s error=%s",
                        batch.id,
                        item.repo,
                        item.pr_number,
                        error,
                    )
                    if batch.stop_on_failure:
                        batch.state = BatchState.FAILED
                else:
                    item.state = ItemState.COMPLETED
                finally:
                    item.finished_at = time.time()

        await asyncio.gather(*(run_item(item) for item in batch.items))
        if batch.state != BatchState.CANCELLED:
            batch.state = (
                BatchState.FAILED if batch.failed else BatchState.COMPLETED
            )
        batch.finished_at = time.time()
        logger.info(
            "themis_batch_finished id=%s state=%s completed=%s failed=%s",
            batch.id,
            batch.state,
            batch.completed,
            batch.failed,
        )
        if batch.callback_url:
            await self.callback_sender.send(batch.callback_url, batch.as_dict())

    async def wait(self, batch_id: str) -> Batch:
        await self.tasks[batch_id]
        return self.get(batch_id)

    def cancel(self, batch_id: str) -> Batch:
        batch = self.get(batch_id)
        batch.state = BatchState.CANCELLED
        batch.finished_at = time.time()
        task = self.tasks.get(batch_id)
        if task:
            task.cancel()
        for item in batch.items:
            if item.state in (ItemState.PENDING, ItemState.RUNNING):
                item.state = ItemState.CANCELLED
                item.finished_at = time.time()
        logger.info(
            "themis_batch_cancelled id=%s requested_by=%s",
            batch.id,
            batch.requested_by,
        )
        return batch

    async def retry_failed(self, batch_id: str) -> Batch:
        batch = self.get(batch_id)
        failed = [item for item in batch.items if item.state == ItemState.FAILED]
        for item in failed:
            item.state = ItemState.PENDING
            item.error = None
            item.started_at = None
            item.finished_at = None
        batch.state = BatchState.RUNNING
        batch.finished_at = None
        self.tasks[batch_id] = asyncio.create_task(self._run(batch))
        await self.tasks[batch_id]
        return batch

    def duplicate(self, batch_id: str, *, requested_by: str) -> Batch:
        source = self.get(batch_id)
        repositories = [
            {
                "repo": item.repo,
                "pr_number": item.pr_number,
                "installation_id": item.installation_id,
            }
            for item in source.items
        ]
        return self.create(
            name=f"Copy of {source.name}",
            requested_by=requested_by,
            repositories=repositories,
            callback_url=source.callback_url,
            concurrency=source.concurrency,
            stop_on_failure=source.stop_on_failure,
        )

    def purge_finished(self, *, older_than_seconds: int) -> int:
        cutoff = time.time() - older_than_seconds
        expired = [
            batch_id
            for batch_id, batch in self.batches.items()
            if batch.finished_at is not None and batch.finished_at < cutoff
        ]
        for batch_id in expired:
            self.batches.pop(batch_id)
            self.tasks.pop(batch_id, None)
        logger.info("themis_batches_purged count=%s", len(expired))
        return len(expired)

    def metrics(self) -> dict[str, int]:
        batches = list(self.batches.values())
        return {
            "batches": len(batches),
            "pending": sum(batch.state == BatchState.PENDING for batch in batches),
            "running": sum(batch.state == BatchState.RUNNING for batch in batches),
            "completed": sum(
                batch.state == BatchState.COMPLETED for batch in batches
            ),
            "failed": sum(batch.state == BatchState.FAILED for batch in batches),
            "cancelled": sum(
                batch.state == BatchState.CANCELLED for batch in batches
            ),
            "items": sum(batch.total for batch in batches),
        }
