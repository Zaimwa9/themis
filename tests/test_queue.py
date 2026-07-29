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
async def test_followup_rejected_while_active_runs_after_completion():
    # A push during a running review must be re-examined once the review
    # finishes, not silently dropped (issue #11 delta reviews).
    queue = InMemoryJobQueue()
    release = asyncio.Event()
    followup_ran = asyncio.Event()

    async def job():
        await release.wait()

    async def followup():
        followup_ran.set()

    queue.start()
    assert queue.enqueue("review:a/b#1", job) is True
    await asyncio.sleep(0.05)  # running
    assert queue.enqueue("review:a/b#1", followup, followup=True) is False
    assert not followup_ran.is_set()
    release.set()
    await asyncio.wait_for(followup_ran.wait(), 2)
    await queue.stop()


@pytest.mark.asyncio
async def test_followup_coalesces_newest_wins():
    queue = InMemoryJobQueue()
    release = asyncio.Event()
    ran: list[str] = []

    async def job():
        await release.wait()

    def make(tag: str):
        async def followup():
            ran.append(tag)
        return followup

    queue.start()
    queue.enqueue("review:a/b#1", job)
    await asyncio.sleep(0.05)
    queue.enqueue("review:a/b#1", make("first"), followup=True)
    queue.enqueue("review:a/b#1", make("second"), followup=True)
    release.set()
    await asyncio.sleep(0.05)
    assert ran == ["second"]
    await queue.stop()


@pytest.mark.asyncio
async def test_followup_id_stays_deduplicated_until_it_runs():
    # Rejections arriving after the follow-up is requeued but before it runs
    # must keep coalescing instead of stacking extra jobs.
    queue = InMemoryJobQueue()
    release = asyncio.Event()
    runs: list[str] = []

    async def job():
        await release.wait()

    async def followup():
        runs.append("followup")

    queue.start()
    queue.enqueue("review:a/b#1", job)
    await asyncio.sleep(0.05)
    queue.enqueue("review:a/b#1", followup, followup=True)
    release.set()
    await asyncio.sleep(0.05)
    assert runs == ["followup"]
    assert queue.enqueue("review:a/b#1", followup, followup=True) is True
    await queue.stop()


@pytest.mark.asyncio
async def test_same_revision_is_dropped_even_when_followup():
    # Issue #77: a second trigger for code already under review is redundant
    # work, so it is discarded rather than kept for a follow-up run.
    queue = InMemoryJobQueue()
    release = asyncio.Event()
    runs: list[str] = []

    async def job():
        runs.append("first")
        await release.wait()

    async def again():
        runs.append("again")

    queue.start()
    assert queue.enqueue("review:a/b#1", job, followup=True, revision="sha:aaa") is True
    await asyncio.sleep(0.05)
    assert queue.enqueue("review:a/b#1", again, followup=True, revision="sha:aaa") is False
    release.set()
    await asyncio.sleep(0.05)
    assert runs == ["first"]
    await queue.stop()


@pytest.mark.asyncio
async def test_new_revision_still_runs_after_the_active_job():
    queue = InMemoryJobQueue()
    release = asyncio.Event()
    runs: list[str] = []

    async def job():
        runs.append("first")
        await release.wait()

    async def pushed():
        runs.append("pushed")

    queue.start()
    queue.enqueue("review:a/b#1", job, followup=True, revision="sha:aaa")
    await asyncio.sleep(0.05)
    assert queue.enqueue("review:a/b#1", pushed, followup=True, revision="sha:bbb") is False
    release.set()
    await asyncio.sleep(0.05)
    assert runs == ["first", "pushed"]
    await queue.stop()


@pytest.mark.asyncio
async def test_revision_matching_a_stored_followup_is_dropped():
    # A re-delivery of the push already waiting its turn must not displace it
    # with an identical job, nor stack a third run.
    queue = InMemoryJobQueue()
    release = asyncio.Event()
    runs: list[str] = []

    async def job():
        await release.wait()

    def make(tag: str):
        async def followup():
            runs.append(tag)
        return followup

    queue.start()
    queue.enqueue("review:a/b#1", job, followup=True, revision="sha:aaa")
    await asyncio.sleep(0.05)
    queue.enqueue("review:a/b#1", make("stored"), followup=True, revision="sha:bbb")
    assert queue.enqueue(
        "review:a/b#1", make("redelivered"), followup=True, revision="sha:bbb"
    ) is False
    release.set()
    await asyncio.sleep(0.05)
    assert runs == ["stored"]
    await queue.stop()


@pytest.mark.asyncio
async def test_followup_superseded_when_the_running_job_covered_its_revision():
    # The head moves between trigger and clone: a review requested at A can
    # start after B is pushed and review B, which makes the follow-up stored
    # for B a second full run over code already reviewed.
    queue = InMemoryJobQueue()
    release = asyncio.Event()
    runs: list[str] = []

    async def job():
        runs.append("first")
        await release.wait()
        queue.reviewed("review:a/b#1", "sha:bbb")  # cloned B, not the A it was queued at

    async def pushed():
        runs.append("pushed")

    queue.start()
    queue.enqueue("review:a/b#1", job, followup=True, revision="sha:aaa")
    await asyncio.sleep(0.05)
    queue.enqueue("review:a/b#1", pushed, followup=True, revision="sha:bbb")
    release.set()
    await asyncio.sleep(0.05)
    assert runs == ["first"]
    await queue.stop()


@pytest.mark.asyncio
async def test_reviewed_revision_deduplicates_later_triggers():
    # Same correction, seen from the other side: a trigger arriving for the
    # commit the running job turned out to be reviewing is redundant too.
    queue = InMemoryJobQueue()
    release = asyncio.Event()
    runs: list[str] = []

    async def job():
        runs.append("first")
        queue.reviewed("review:a/b#1", "sha:bbb")
        await release.wait()

    async def later():
        runs.append("later")

    queue.start()
    queue.enqueue("review:a/b#1", job, followup=True, revision="sha:aaa")
    await asyncio.sleep(0.05)
    assert queue.enqueue("review:a/b#1", later, followup=True, revision="sha:bbb") is False
    release.set()
    await asyncio.sleep(0.05)
    assert runs == ["first"]
    await queue.stop()


@pytest.mark.asyncio
async def test_reviewed_is_ignored_once_the_job_is_gone():
    queue = InMemoryJobQueue()
    ran = asyncio.Event()

    async def job():
        ran.set()

    queue.start()
    queue.enqueue("review:a/b#1", job, revision="sha:aaa")
    await asyncio.wait_for(ran.wait(), 2)
    await asyncio.sleep(0.05)
    queue.reviewed("review:a/b#1", "sha:bbb")  # late report, id already freed
    assert queue.enqueue("review:a/b#1", job, revision="sha:bbb") is True
    await queue.stop()


@pytest.mark.asyncio
async def test_unknown_revision_never_counts_as_a_duplicate():
    # None means "cannot tell what this job would process"; guessing "same"
    # would drop real work, so it is always queued or stored.
    queue = InMemoryJobQueue()
    release = asyncio.Event()
    runs: list[str] = []

    async def job():
        await release.wait()

    async def unknown():
        runs.append("unknown")

    queue.start()
    queue.enqueue("review:a/b#1", job, followup=True, revision=None)
    await asyncio.sleep(0.05)
    assert queue.enqueue("review:a/b#1", unknown, followup=True, revision=None) is False
    release.set()
    await asyncio.sleep(0.05)
    assert runs == ["unknown"]
    await queue.stop()


@pytest.mark.asyncio
async def test_revision_frees_up_with_the_id():
    queue = InMemoryJobQueue()
    ran = asyncio.Event()

    async def job():
        ran.set()

    queue.start()
    queue.enqueue("review:a/b#1", job, revision="sha:aaa")
    await asyncio.wait_for(ran.wait(), 2)
    # Re-running the same commit is a legitimate request once nothing is in
    # flight - the engine may have flaked, or the reviewer wants another pass.
    assert queue.enqueue("review:a/b#1", job, revision="sha:aaa") is True
    await queue.stop()


@pytest.mark.asyncio
async def test_duplicate_without_followup_never_reruns():
    queue = InMemoryJobQueue()
    release = asyncio.Event()
    runs = 0

    async def job():
        nonlocal runs
        runs += 1
        await release.wait()

    queue.start()
    queue.enqueue("review:a/b#1", job)
    await asyncio.sleep(0.05)
    queue.enqueue("review:a/b#1", job)  # webhook redelivery: plain duplicate
    release.set()
    await asyncio.sleep(0.05)
    assert runs == 1
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
