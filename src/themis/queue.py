"""In-memory job queue: dedup by id, N consumers (default one), bounded runtime.

The queue seam for a future durable backend: keep this surface (enqueue ->
bool, start, stop) and swap the implementation.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

# 2 * default codex timeout + clone/posting headroom. Fixed ceiling: a repo
# config raising limits.timeout_seconds is still capped by this, because repo
# config is only fetched inside the job.
DEFAULT_JOB_TIMEOUT = 2700.0

JobFactory = Callable[[], Awaitable[None]]

# What to do with a job whose id is already active and whose revision is not
# already in flight:
#   "drop"     - discard it (the caller has nothing to re-examine later)
#   "coalesce" - hold it until the slot frees, replacing any other coalescing
#                job held for that id: each one asks for "the current state",
#                so the newest subsumes its predecessors
#   "queue"    - hold it in arrival order and never let another job replace it
# This is a property of the request, not of what it would turn out to process:
# `revision` answers "is this provably the same work?", and a caller that
# cannot tell passes None there while still stating what kind of ask this is.
OnConflict = Literal["drop", "coalesce", "queue"]


@dataclass(frozen=True)
class _Job:
    id: str
    run: JobFactory
    revision: str | None = None
    coalescing: bool = False


class InMemoryJobQueue:
    """Asyncio queue drained by `concurrency` consumer tasks (default one).

    Dedup: an id that is queued or currently running is rejected as a
    duplicate; the id frees up when the job finishes (success, failure,
    timeout, or cancellation). A rejected job is not necessarily discarded -
    see `on_conflict` - so an event that arrived mid-run (a push during a
    review) is re-examined instead of silently lost. Queued-but-not-started
    jobs are lost on restart; re-triggering (mention) is the documented
    recovery path.

    The id is a serialization key, not an identity: callers that share one id
    across triggers (every review of a PR, say) pass a `revision` naming what
    the job would process, and a revision already in flight under that id is
    dropped instead of being held (issue #77).
    """

    def __init__(
        self, job_timeout: float = DEFAULT_JOB_TIMEOUT, concurrency: int = 1
    ) -> None:
        self._timeout = job_timeout
        self._concurrency = concurrency
        self._queue: asyncio.Queue[_Job] = asyncio.Queue()
        self._active: dict[str, str | None] = {}
        self._followups: dict[str, list[_Job]] = {}
        self._consumers: list[asyncio.Task[None]] = []

    def enqueue(
        self,
        job_id: str,
        run: JobFactory,
        on_conflict: OnConflict = "drop",
        revision: str | None = None,
    ) -> bool:
        """True when queued, False when a job with this id is already active.

        `on_conflict` decides what rejection means. "drop" discards. The other
        two hold the job and run it, in arrival order, once the id frees up;
        only jobs that re-check their own preconditions belong there, since a
        held job runs unconditionally when the slot opens. "coalesce" marks the
        job supersedable: a later coalescing job takes its place (in position,
        so a stream of pushes cannot reorder itself ahead of an explicit
        request). "queue" marks it not supersedable - two people asking for two
        different things are two pieces of work, and neither answers the other.

        A non-None `revision` matching one already in flight under this id -
        the running job's or any held one's - is redundant work by definition
        and is dropped whatever `on_conflict` says. None means "cannot tell",
        and never matches: an unidentifiable job is queued or held, never
        discarded.
        """
        if job_id in self._active:
            if revision is not None and revision in self._in_flight_revisions(job_id):
                logger.info(
                    "themis_job_duplicate id=%s revision=%s", job_id, revision
                )
                return False
            if on_conflict == "drop":
                logger.info("themis_job_duplicate id=%s", job_id)
                return False
            self._hold(_Job(job_id, run, revision, on_conflict == "coalesce"))
            return False
        self._active[job_id] = revision
        self._queue.put_nowait(_Job(job_id, run, revision))
        return True

    def _hold(self, job: _Job) -> None:
        """Keep `job` for when its id frees up, replacing a superseded one.

        A coalescing job replaces the first coalescing job already held, in
        place: it asks for the same thing (the PR's current state), so running
        both would be a redundant pass, but the jobs held around it asked for
        something else and keep both their place and their run."""
        pending = self._followups.setdefault(job.id, [])
        if job.coalescing:
            for index, held in enumerate(pending):
                if held.coalescing:
                    pending[index] = job
                    break
            else:
                pending.append(job)
        else:
            pending.append(job)
        logger.info(
            "themis_job_followup_stored id=%s pending=%d", job.id, len(pending)
        )

    def reviewed(self, job_id: str, revision: str) -> None:
        """A running job reporting what it turned out to process.

        A revision captured at trigger time only names the head as it looked
        then; the job resolves the real head when it starts, and the two differ
        whenever someone pushed in between. Reporting the real one keeps later
        triggers for that same commit - and the follow-up already waiting for
        it - from buying a second identical run."""
        if job_id in self._active:
            self._active[job_id] = revision

    def _in_flight_revisions(self, job_id: str) -> set[str]:
        """Revisions this id is already going to process: the running job's and
        every held one's. All of them, so a re-delivered trigger matches the
        held job it duplicates however deep that one sits."""
        held = self._followups.get(job_id, ())
        revisions = [self._active.get(job_id), *(job.revision for job in held)]
        return {revision for revision in revisions if revision is not None}

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
                processed = self._active.pop(job.id, None)
                pending = self._followups.pop(job.id, [])
                if processed is not None:
                    # The job that just finished turned out to cover exactly
                    # what these held jobs were waiting to do.
                    kept = [held for held in pending if held.revision != processed]
                    if len(kept) < len(pending):
                        logger.info(
                            "themis_job_followup_superseded id=%s revision=%s count=%d",
                            job.id, processed, len(pending) - len(kept),
                        )
                    pending = kept
                if pending:
                    next_job, *rest = pending
                    # Re-activate immediately so rejections arriving between
                    # this requeue and the next job's run keep coalescing, and
                    # keep the rest held: they are distinct requests, and
                    # dropping them here would lose the work the queue just
                    # promised to do.
                    self._active[next_job.id] = next_job.revision
                    if rest:
                        self._followups[next_job.id] = rest
                    self._queue.put_nowait(next_job)
                    logger.info(
                        "themis_job_followup_enqueued id=%s pending=%d",
                        job.id, len(rest),
                    )
