"""Tests for JobManager external-job API and METADATA_SCAN non-cancellability."""

import pytest

from app.models import ConversionMode, JobStatus
from app.services.job_manager import JobManager


def _make_manager() -> JobManager:
    """Return a fresh JobManager with default settings."""
    return JobManager(max_concurrent=1, max_job_history=10)


# ---------------------------------------------------------------------------
# create_external_job
# ---------------------------------------------------------------------------

def test_create_external_job_registers_job():
    mgr = _make_manager()
    job = mgr.create_external_job(
        filename="Metadata Scan",
        mode=ConversionMode.METADATA_SCAN,
        message="Starting…",
    )

    assert job.id in mgr.jobs
    assert mgr.jobs[job.id] is job
    assert job.status == JobStatus.PROCESSING
    assert job.progress == 0
    assert job.message == "Starting…"
    assert job.mode == ConversionMode.METADATA_SCAN
    assert job.started_at is not None


def test_create_external_job_returns_distinct_ids():
    mgr = _make_manager()
    j1 = mgr.create_external_job("Scan 1", ConversionMode.METADATA_SCAN)
    j2 = mgr.create_external_job("Scan 2", ConversionMode.METADATA_SCAN)
    assert j1.id != j2.id


# ---------------------------------------------------------------------------
# update_external_job
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_external_job_clamps_progress():
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id

    await mgr.update_external_job(job_id, progress=150)
    assert mgr.jobs[job_id].progress == 100

    await mgr.update_external_job(job_id, progress=-10)
    assert mgr.jobs[job_id].progress == 0


@pytest.mark.asyncio
async def test_update_external_job_updates_message():
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id

    await mgr.update_external_job(job_id, message="Phase 1 [1/5]")
    assert mgr.jobs[job_id].message == "Phase 1 [1/5]"


@pytest.mark.asyncio
async def test_update_external_job_notifies_subscriber():
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id

    queue = mgr.subscribe(job_id)
    await mgr.update_external_job(job_id, progress=42, message="In progress")

    assert not queue.empty()
    payload = queue.get_nowait()
    assert payload["type"] == "progress"
    assert payload["job_id"] == job_id
    assert payload["progress"] == 42
    assert payload["message"] == "In progress"


@pytest.mark.asyncio
async def test_update_external_job_noop_for_unknown_id():
    """Updating a non-existent job should not raise."""
    mgr = _make_manager()
    await mgr.update_external_job("unknown", progress=50, message="x")


# ---------------------------------------------------------------------------
# finish_external_job
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finish_external_job_success_keeps_job_in_live_jobs():
    """Completed external jobs must remain in self.jobs for the Clear Done flow."""
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id

    await mgr.finish_external_job(job_id, success=True)

    # Job must still be listed (not deleted) so /api/jobs returns it
    assert job_id in mgr.jobs
    assert mgr.jobs[job_id].status == JobStatus.COMPLETED
    # Also archived for brief SSE-subscriber lookup
    assert job_id in mgr._archived_jobs


@pytest.mark.asyncio
async def test_finish_external_job_success_notifies_complete():
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id

    queue = mgr.subscribe(job_id)
    await mgr.finish_external_job(job_id, success=True)

    payload = queue.get_nowait()
    assert payload["type"] == "complete"
    assert payload["status"] == JobStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_finish_external_job_failure_notifies_error():
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id

    queue = mgr.subscribe(job_id)
    await mgr.finish_external_job(job_id, success=False, error_message="boom")

    payload = queue.get_nowait()
    assert payload["type"] == "error"
    assert payload["status"] == JobStatus.FAILED.value
    assert payload["error_message"] == "boom"


@pytest.mark.asyncio
async def test_finish_external_job_sets_progress_100_on_success():
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id
    await mgr.update_external_job(job_id, progress=97)

    await mgr.finish_external_job(job_id, success=True)

    # Job remains in self.jobs with progress=100
    assert mgr.jobs[job_id].progress == 100


@pytest.mark.asyncio
async def test_finish_external_job_noop_for_unknown_id():
    """Finishing a non-existent job should not raise."""
    mgr = _make_manager()
    await mgr.finish_external_job("unknown", success=True)


# ---------------------------------------------------------------------------
# External-job cancellation (metadata scan, DAT match)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_job_signals_metadata_scan():
    """cancel_job() on a PROCESSING external job must set the cancel event
    and flip the message to 'Cancelling…'. The job is still PROCESSING at
    this point, the owning task is responsible for flipping to CANCELLED
    via finish_external_job_cancelled()."""
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id

    result = await mgr.cancel_job(job_id)

    assert result is True
    assert mgr.is_cancelled(job_id)
    cancel_event = mgr.get_cancel_event(job_id)
    assert cancel_event is not None
    assert cancel_event.is_set()
    # Owning task hasn't finalized yet, status is still PROCESSING
    assert mgr.jobs[job_id].status == JobStatus.PROCESSING
    assert mgr.jobs[job_id].message == "Cancelling..."


@pytest.mark.asyncio
async def test_cancel_job_signals_dat_match():
    mgr = _make_manager()
    job = mgr.create_external_job("DAT Match", ConversionMode.DAT_MATCH)
    job_id = job.id

    result = await mgr.cancel_job(job_id)

    assert result is True
    assert mgr.is_cancelled(job_id)
    assert mgr.get_cancel_event(job_id).is_set()


@pytest.mark.asyncio
async def test_cancel_job_emits_status_event_for_external_job():
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id
    queue = mgr.subscribe(job_id)

    await mgr.cancel_job(job_id)
    # Let the asyncio.create_task inside cancel_job run
    import asyncio as _asyncio
    await _asyncio.sleep(0)

    assert not queue.empty()
    payload = queue.get_nowait()
    assert payload["type"] == "status"
    assert payload["message"] == "Cancelling..."


@pytest.mark.asyncio
async def test_cancel_all_includes_external_jobs():
    mgr = _make_manager()
    scan_job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    match_job = mgr.create_external_job("DAT Match", ConversionMode.DAT_MATCH)

    result = await mgr.cancel_all_jobs()

    assert result["requested"] == 2
    assert scan_job.id in result["job_ids"]
    assert match_job.id in result["job_ids"]
    assert mgr.is_cancelled(scan_job.id)
    assert mgr.is_cancelled(match_job.id)


# ---------------------------------------------------------------------------
# finish_external_job_cancelled
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finish_external_job_cancelled_sets_status_and_message():
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id

    await mgr.finish_external_job_cancelled(job_id, message="Cancelled \u2014 5 done")

    assert mgr.jobs[job_id].status == JobStatus.CANCELLED
    assert mgr.jobs[job_id].message == "Cancelled \u2014 5 done"
    assert mgr.jobs[job_id].completed_at is not None


@pytest.mark.asyncio
async def test_finish_external_job_cancelled_emits_cancelled_event():
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id
    queue = mgr.subscribe(job_id)

    await mgr.finish_external_job_cancelled(job_id, message="stopped")

    payload = queue.get_nowait()
    assert payload["type"] == "cancelled"
    assert payload["status"] == JobStatus.CANCELLED.value
    assert payload["message"] == "stopped"


@pytest.mark.asyncio
async def test_finish_external_job_cancelled_clears_cancel_state():
    """After a cancelled finalize, the _cancelled set and _cancel_events
    dict must not retain the job id, otherwise a future job with the
    same id (unlikely but possible after uuid collisions) would start
    pre-cancelled."""
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id
    await mgr.cancel_job(job_id)
    assert mgr.is_cancelled(job_id)

    await mgr.finish_external_job_cancelled(job_id, message="stopped")

    assert not mgr.is_cancelled(job_id)
    assert mgr.get_cancel_event(job_id) is None


@pytest.mark.asyncio
async def test_finish_external_job_cancelled_noop_for_unknown_id():
    mgr = _make_manager()
    await mgr.finish_external_job_cancelled("unknown", message="x")


@pytest.mark.asyncio
async def test_cancel_job_returns_false_for_terminal_external_job():
    """Already-terminal external jobs cannot be re-cancelled."""
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id
    await mgr.finish_external_job(job_id, success=True)

    result = await mgr.cancel_job(job_id)

    assert result is False
    assert mgr.jobs[job_id].status == JobStatus.COMPLETED


# ---------------------------------------------------------------------------
# Sentinel file path
# ---------------------------------------------------------------------------

def test_create_external_job_uses_sentinel_file_path():
    """create_external_job must use a sentinel path, never an empty string."""
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    assert job.file_path.startswith("/__external_jobs__/")
    assert job.file_path != ""
    assert job.file_path != "/"


def test_sentinel_path_does_not_interfere_with_path_in_use_checks():
    """The sentinel path must not cause false positives in _is_path_in_use_by_other_job."""
    mgr = _make_manager()
    # Register a fake conversion job with a real-looking path
    conversion_job_id = "conv0001"
    from app.models import ConversionJob, JobStatus as JS
    from datetime import datetime, timezone
    conv_job = ConversionJob(
        id=conversion_job_id,
        file_path="/data/roms/game.iso",
        filename="game.iso",
        mode=ConversionMode.CREATECD,
        status=JS.PROCESSING,
        progress=0,
        created_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
    )
    mgr.jobs[conversion_job_id] = conv_job

    # Register an external scan job
    scan_job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)

    # Path-in-use detection is unaffected by the scan job: conv_job genuinely
    # holds /data/roms/game.iso, so the check correctly returns True.
    assert mgr._is_path_in_use_by_other_job(scan_job.id, "/data/roms/game.iso")
    # More importantly: the conversion job's path check must not match the sentinel
    assert not mgr._is_path_in_use_by_other_job(conversion_job_id, "/__external_jobs__/" + scan_job.id)


# ---------------------------------------------------------------------------
# Backpressure / stuck detection exclusion
# ---------------------------------------------------------------------------

def test_metadata_scan_does_not_count_toward_queue_depth():
    mgr = _make_manager()
    mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    assert mgr.get_queue_depth() == 0


def test_metadata_scan_does_not_trigger_stuck_detection():
    """A running METADATA_SCAN with no conversion jobs must not be considered stuck."""
    mgr = _make_manager()
    # With only a METADATA_SCAN processing and no conversion jobs enqueued,
    # is_stuck must remain False.
    mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    assert mgr.is_stuck() is False


# ---------------------------------------------------------------------------
# Clear Done compatibility
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_completed_external_job_visible_for_clear_done():
    """Completed external jobs must stay in self.jobs so Clear Done can remove them."""
    mgr = _make_manager()
    job = mgr.create_external_job("Scan", ConversionMode.METADATA_SCAN)
    job_id = job.id
    await mgr.finish_external_job(job_id, success=True)

    # Job is still in the live dict (not deleted)
    assert job_id in mgr.jobs
    assert mgr.jobs[job_id].status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_finish_external_job_prunes_old_completed_jobs():
    """finish_external_job() must enforce max_job_history so old terminal jobs
    don't accumulate indefinitely (the just-finished job is preserved)."""
    mgr = JobManager(max_concurrent=1, max_job_history=3)

    # Fill history with 3 already-completed external jobs
    for i in range(3):
        j = mgr.create_external_job(f"OldScan{i}", ConversionMode.METADATA_SCAN)
        await mgr.finish_external_job(j.id, success=True)

    # All 3 are visible so far
    assert len(mgr.jobs) == 3

    # Adding a 4th and finishing it should prune the oldest to keep ≤ max_job_history
    j4 = mgr.create_external_job("NewScan", ConversionMode.METADATA_SCAN)
    await mgr.finish_external_job(j4.id, success=True)

    # After pruning, total should not exceed max_job_history (3)
    assert len(mgr.jobs) <= 3
    # The just-finished job must be preserved
    assert j4.id in mgr.jobs


@pytest.mark.asyncio
async def test_deep_queue_does_not_evict_finished_history():
    """A queue deeper than max_job_history must not evict finished jobs.

    The cap bounds *history*; pending work isn't history. Gating on the total
    job count conflated the two: submitting more than max_job_history files at
    once (routine now that Select All spans every page — a Redump platform set
    is thousands) kept the total permanently over the cap, so every sweep
    deleted every terminal job it could find and still couldn't get under it.
    The Completed / Failed tabs emptied mid-batch and a finished run had no
    record of which files failed.
    """
    mgr = JobManager(max_concurrent=1, max_job_history=3)

    # History sits exactly at the cap.
    finished = []
    for i in range(3):
        j = mgr.create_external_job(f"Done{i}", ConversionMode.METADATA_SCAN)
        await mgr.finish_external_job(j.id, success=True)
        finished.append(j.id)

    # A backlog far deeper than the cap. These are not history.
    pending = [
        mgr.create_external_job(f"Pending{i}", ConversionMode.METADATA_SCAN).id
        for i in range(20)
    ]

    await mgr._prune_jobs()

    for job_id in finished:
        assert job_id in mgr.jobs, "a deep queue must not evict finished jobs"
    for job_id in pending:
        assert job_id in mgr.jobs, "pending work must never be pruned"


@pytest.mark.asyncio
async def test_prune_still_evicts_oldest_history_past_the_cap():
    """Counting only terminal jobs must not turn pruning off: history beyond the
    cap is still trimmed, oldest first."""
    mgr = JobManager(max_concurrent=1, max_job_history=3)

    ids = []
    for i in range(5):
        j = mgr.create_external_job(f"Done{i}", ConversionMode.METADATA_SCAN)
        await mgr.finish_external_job(j.id, success=True)
        ids.append(j.id)

    await mgr._prune_jobs()

    assert ids[0] not in mgr.jobs
    assert ids[1] not in mgr.jobs
    for job_id in ids[2:]:
        assert job_id in mgr.jobs


# ---------------------------------------------------------------------------
# History overflow — the counts the cap throws away
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_history_overflow_counts_evicted_jobs():
    """Evicted history must be tallied, keyed by status and mode.

    Without it every client count derived from the job list freezes at
    max_job_history: the Completed badge reads 500 forever while the queue
    keeps draining.
    """
    mgr = JobManager(max_concurrent=1, max_job_history=3)

    assert mgr.get_history_overflow()["total_evicted"] == 0

    for i in range(6):
        j = mgr.create_external_job(f"Done{i}", ConversionMode.METADATA_SCAN)
        await mgr.finish_external_job(j.id, success=True)

    await mgr._prune_jobs()

    overflow = mgr.get_history_overflow()
    assert overflow["max_job_history"] == 3
    assert overflow["total_evicted"] == 3
    assert overflow["evicted"]["completed"]["metadata_scan"] == 3
    # Retained + evicted is the real total.
    retained = sum(1 for job in mgr.jobs.values() if job.status == JobStatus.COMPLETED)
    assert retained + overflow["evicted"]["completed"]["metadata_scan"] == 6


@pytest.mark.asyncio
async def test_history_overflow_splits_failed_from_completed():
    """Statuses are tallied separately so each tab badge stays honest."""
    mgr = JobManager(max_concurrent=1, max_job_history=1)

    for i in range(3):
        j = mgr.create_external_job(f"Ok{i}", ConversionMode.METADATA_SCAN)
        await mgr.finish_external_job(j.id, success=True)
    for i in range(3):
        j = mgr.create_external_job(f"Bad{i}", ConversionMode.METADATA_SCAN)
        await mgr.finish_external_job(j.id, success=False, error_message="boom")

    await mgr._prune_jobs()

    evicted = mgr.get_history_overflow()["evicted"]
    assert evicted["completed"]["metadata_scan"] == 3
    assert evicted["failed"]["metadata_scan"] == 2


@pytest.mark.asyncio
async def test_user_deleted_jobs_are_not_counted_as_evicted():
    """Deleting a job by hand is history the user dropped, not history the cap
    took — it must not inflate the totals."""
    mgr = JobManager(max_concurrent=1, max_job_history=10)

    j = mgr.create_external_job("Done", ConversionMode.METADATA_SCAN)
    await mgr.finish_external_job(j.id, success=True)
    assert await mgr.delete_job(j.id) is True

    assert mgr.get_history_overflow()["total_evicted"] == 0


@pytest.mark.asyncio
async def test_history_overflow_replays_evicted_ids_from_a_cursor():
    """`since` must yield exactly the ids evicted after that point.

    A client learns of a completion before the prune it triggers, so it holds a
    row the backend has already deleted; without the ids it would count that
    job twice (stale row + tally).
    """
    mgr = JobManager(max_concurrent=1, max_job_history=1)

    ids = []
    for i in range(3):
        j = mgr.create_external_job(f"Done{i}", ConversionMode.METADATA_SCAN)
        await mgr.finish_external_job(j.id, success=True)
        ids.append(j.id)
    await mgr._prune_jobs()

    # No cursor: counts only, no ids.
    assert mgr.get_history_overflow()["evicted_ids"] == []

    full = mgr.get_history_overflow(since=0)
    assert full["evicted_ids"] == ids[:2]
    assert full["seq"] == 2

    # A cursor mid-log replays only what followed it.
    assert mgr.get_history_overflow(since=1)["evicted_ids"] == [ids[1]]
    # A current cursor replays nothing.
    assert mgr.get_history_overflow(since=full["seq"])["evicted_ids"] == []


@pytest.mark.asyncio
async def test_reset_history_overflow_keeps_the_sequence_monotonic():
    """Clear empties the tally and the id log, but the cursor must not rewind —
    a client holding a stale `seq` would otherwise look current."""
    mgr = JobManager(max_concurrent=1, max_job_history=1)

    for i in range(3):
        j = mgr.create_external_job(f"Done{i}", ConversionMode.METADATA_SCAN)
        await mgr.finish_external_job(j.id, success=True)
    seq_before = mgr.get_history_overflow()["seq"]
    assert seq_before > 0

    mgr.reset_history_overflow()

    after = mgr.get_history_overflow(since=0)
    assert after["seq"] == seq_before
    assert after["evicted_ids"] == []


@pytest.mark.asyncio
async def test_history_overflow_total_matches_the_tally():
    mgr = JobManager(max_concurrent=1, max_job_history=1)
    assert mgr.history_overflow_total() == 0

    for i in range(4):
        j = mgr.create_external_job(f"Done{i}", ConversionMode.METADATA_SCAN)
        await mgr.finish_external_job(j.id, success=True)

    assert mgr.history_overflow_total() == mgr.get_history_overflow()["total_evicted"] == 3


@pytest.mark.asyncio
async def test_reset_history_overflow_clears_the_tally():
    mgr = JobManager(max_concurrent=1, max_job_history=1)

    for i in range(4):
        j = mgr.create_external_job(f"Done{i}", ConversionMode.METADATA_SCAN)
        await mgr.finish_external_job(j.id, success=True)
    await mgr._prune_jobs()
    assert mgr.get_history_overflow()["total_evicted"] > 0

    mgr.reset_history_overflow()
    assert mgr.get_history_overflow()["total_evicted"] == 0
    assert mgr.get_history_overflow()["evicted"] == {}
