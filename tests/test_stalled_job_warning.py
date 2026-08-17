"""Stalled-job visibility and the native-progress sentinel (issue #263).

Both guards here cover regressions found in review of the #263 fix: a warning
that only fired when DEBUG logging was on (so nobody ever saw it), and a
progress parser whose 0-for-no-match sentinel silently disabled the
size-growth fallback for the whole run.
"""

import logging
import time

from app.models import ConversionMode, JobStatus
from app.services.chdman import ChdmanService
from app.services.job_manager import JobManager


def _processing_job(mgr: JobManager, job_id: str = "job-1"):
    """Register a PROCESSING conversion job that last reported long ago."""
    job = mgr.jobs[job_id] = type(
        "J", (), {
            "id": job_id,
            "status": JobStatus.PROCESSING,
            "mode": ConversionMode.DOLPHIN_RVZ,
            "progress": 0,
            "message": "Converting...",
            "file_path": "/data/in.iso",
            "output_path": None,
            "started_at": None,
        },
    )()
    # Idle far longer than debug_progress_timeout (300s default).
    mgr._last_progress_at[job_id] = time.monotonic() - 100_000
    return job


def test_stalled_job_warns_with_debug_logging_disabled(caplog):
    """The warning must reach a default-level log.

    It used to sit below `if not logger.isEnabledFor(DEBUG): continue`, so at the
    default INFO level a wedged job produced no trace at all -- which is why the
    reporting user's container log looked clean while their queue was frozen.
    """
    mgr = JobManager(max_concurrent=1, max_job_history=10)
    _processing_job(mgr)

    logging.getLogger("chd.job_manager").setLevel(logging.INFO)
    with caplog.at_level(logging.WARNING):
        mgr._log_stalled_jobs()

    assert any("Stalled job" in r.message for r in caplog.records)


def test_verifying_job_is_reported_as_verifying_not_stalled(caplog):
    """A job in verify is reported in its own words, not as stalled.

    Verification emits no progress and legitimately runs for many minutes, so
    calling it stalled would be wrong -- but staying silent hid a job genuinely
    wedged *in* verify, which is what issue #266 is about. It gets its own line,
    timed from when the verify phase began.
    """
    mgr = JobManager(max_concurrent=1, max_job_history=10)
    _processing_job(mgr)
    mgr._verifying["job-1"] = time.monotonic() - 100_000

    with caplog.at_level(logging.WARNING):
        mgr._log_stalled_jobs()

    assert not [r for r in caplog.records if "Stalled job" in r.message]
    assert any("Verifying job job-1" in r.message for r in caplog.records)


def test_verify_phase_is_timed_from_when_verification_started(caplog):
    """A verify that just began is not reported at all.

    The progress clock stopped at the end of the conversion, so timing the
    verify by it would report a verify as long-running the instant it starts.
    """
    mgr = JobManager(max_concurrent=1, max_job_history=10)
    _processing_job(mgr)
    mgr._verifying["job-1"] = time.monotonic()

    with caplog.at_level(logging.WARNING):
        mgr._log_stalled_jobs()

    assert not caplog.records


def test_chdman_parse_progress_returns_none_for_non_progress_lines():
    """A non-percentage line is None, not 0.

    The runner reads "a percent was parsed" as proof the tool reports its own
    progress and stands the size-growth fallback down. Returning 0 for a banner
    line made the very first line of output look like a report, disabling the
    fallback for the entire run.
    """
    svc = ChdmanService()

    assert svc._parse_progress("chdman - MAME Compressed Hunks of Data") is None
    assert svc._parse_progress("Compressing, 45.2% complete...") == 45
    assert svc._parse_progress("Compressing, 99.9% complete...") == 99

