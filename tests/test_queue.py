"""InMemoryJobQueue: dedup, consumer count, timeout, cancellation, shutdown."""

import asyncio

import pytest

from themis.queue import InMemoryJobQueue


@pytest.mark.asyncio
async def test_runs_enqueued_job():
    queue = InMemoryJobQueue()
    done = asyncio.Event()

    async def job():
        done.set()

    queue.start()
    assert queue.enqueue("review:a/b#1", job) is True
    await asyncio.wait_for(done.wait(), 2)
    await queue.stop()


@pytest.mark.asyncio
async def test_duplicate_id_rejected_while_active():
    queue = InMemoryJobQueue()
    release = asyncio.Event()

    async def job():
        await release.wait()

    queue.start()
    assert queue.enqueue("review:a/b#1", job) is True
    assert queue.enqueue("review:a/b#1", job) is False  # queued: duplicate
    await asyncio.sleep(0.05)                            # now running
    assert queue.enqueue("review:a/b#1", job) is False  # running: duplicate
    release.set()
    await asyncio.sleep(0.05)
    assert queue.enqueue("review:a/b#1", job) is True   # finished: accepted
    await queue.stop()


@pytest.mark.asyncio
async def test_jobs_run_one_at_a_time():
    queue = InMemoryJobQueue()
    running = 0
    peak = 0
    release = asyncio.Event()

    async def job():
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await release.wait()
        running -= 1

    queue.start()
    queue.enqueue("j1", job)
    queue.enqueue("j2", job)
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.sleep(0.05)
    assert peak == 1
    await queue.stop()


@pytest.mark.asyncio
async def test_concurrency_two_runs_jobs_in_parallel():
    queue = InMemoryJobQueue(concurrency=2)
    both_running = asyncio.Event()
    release = asyncio.Event()
    running = 0

    async def job():
        nonlocal running
        running += 1
        if running == 2:
            both_running.set()
        await release.wait()

    queue.start()
    queue.enqueue("j1", job)
    queue.enqueue("j2", job)
    await asyncio.wait_for(both_running.wait(), 2)  # parallel, not serial
    release.set()
    await asyncio.sleep(0.05)
    assert queue.enqueue("j1", job) is True  # both ids freed
    await queue.stop()


@pytest.mark.asyncio
async def test_stop_cancels_all_consumers_and_running_jobs():
    queue = InMemoryJobQueue(concurrency=2)
    started = 0
    cancelled = 0

    async def stuck():
        nonlocal started, cancelled
        started += 1
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled += 1
            raise

    queue.start()
    queue.enqueue("s1", stuck)
    queue.enqueue("s2", stuck)
    await asyncio.sleep(0.05)
    assert started == 2
    await queue.stop()
    assert cancelled == 2


@pytest.mark.asyncio
async def test_job_timeout_cancels_and_frees_the_slot():
    queue = InMemoryJobQueue(job_timeout=0.05)
    cancelled = asyncio.Event()
    ran_after = asyncio.Event()

    async def stuck():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def quick():
        ran_after.set()

    queue.start()
    queue.enqueue("stuck", stuck)
    queue.enqueue("quick", quick)
    await asyncio.wait_for(cancelled.wait(), 2)
    await asyncio.wait_for(ran_after.wait(), 2)   # consumer survived the timeout
    assert queue.enqueue("stuck", stuck) is True  # id freed after timeout
    await queue.stop()


@pytest.mark.asyncio
async def test_job_exception_does_not_kill_consumer():
    queue = InMemoryJobQueue()
    done = asyncio.Event()

    async def bad():
        raise RuntimeError("boom")

    async def good():
        done.set()

    queue.start()
    queue.enqueue("bad", bad)
    queue.enqueue("good", good)
    await asyncio.wait_for(done.wait(), 2)
    await queue.stop()


@pytest.mark.asyncio
async def test_stop_cancels_running_job():
    queue = InMemoryJobQueue()
    cancelled = asyncio.Event()

    async def stuck():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    queue.start()
    queue.enqueue("stuck", stuck)
    await asyncio.sleep(0.05)
    await queue.stop()
    assert cancelled.is_set()
