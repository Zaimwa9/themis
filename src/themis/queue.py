"""In-memory job queue: dedup by id, N consumers (default one), bounded runtime.

The queue seam for a future durable backend: keep this surface (enqueue ->
bool, start, stop) and swap the implementation.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 2 * default codex timeout + clone/posting headroom. Fixed ceiling: a repo
# config raising limits.timeout_seconds is still capped by this, because repo
# config is only fetched inside the job.
DEFAULT_JOB_TIMEOUT = 2700.0

JobFactory = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class _Job:
    id: str
    run: JobFactory


class InMemoryJobQueue:
    """Asyncio queue drained by `concurrency` consumer tasks (default one).

    Dedup: an id that is queued or currently running is rejected as a
    duplicate; the id frees up when the job finishes (success, failure,
    timeout, or cancellation). Queued-but-not-started jobs are lost on
    restart; re-triggering (mention) is the documented recovery path.
    """

    def __init__(
        self, job_timeout: float = DEFAULT_JOB_TIMEOUT, concurrency: int = 1
    ) -> None:
        self._timeout = job_timeout
        self._concurrency = concurrency
        self._queue: asyncio.Queue[_Job] = asyncio.Queue()
        self._active_ids: set[str] = set()
        self._consumers: list[asyncio.Task[None]] = []

    def enqueue(self, job_id: str, run: JobFactory) -> bool:
        """True when queued, False when a job with this id is already active."""
        if job_id in self._active_ids:
            logger.info("themis_job_duplicate id=%s", job_id)
            return False
        self._active_ids.add(job_id)
        self._queue.put_nowait(_Job(job_id, run))
        return True

    def start(self) -> None:
        if not self._consumers:
            loop = asyncio.get_running_loop()
            self._consumers = [
                loop.create_task(self._consume()) for _ in range(self._concurrency)
            ]

    async def stop(self) -> None:
        """Cancel every consumer (and any running jobs) and wait for them to die."""
        if not self._consumers:
            return
        for consumer in self._consumers:
            consumer.cancel()
        for consumer in self._consumers:
            with contextlib.suppress(asyncio.CancelledError):
                await consumer
        self._consumers = []

    async def _consume(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await asyncio.wait_for(job.run(), self._timeout)
            except TimeoutError:
                # wait_for cancelled the job; its CancelledError handlers
                # (cancelled-comment) already ran inside.
                logger.warning("themis_job_timeout id=%s", job.id, exc_info=True)
            except asyncio.CancelledError:
                raise  # shutdown
            except Exception:
                # Failure comments are the job's responsibility; this is the
                # backstop so one bad job cannot kill the consumer.
                logger.exception("themis_job_failed id=%s", job.id)
            finally:
                self._active_ids.discard(job.id)
