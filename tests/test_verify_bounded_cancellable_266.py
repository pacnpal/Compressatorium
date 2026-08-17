"""The verify stage is bounded and cancellable (issue #266).

Issue #265 bounded every wait in the *conversion* path; the verify path had the
same class of gap and was left out of scope there. Out of the box a verify had
no timeout at all, and the delete-on-verify call site passed no ``cancel_event``
-- so a verify that never returned never ended, and with ``MAX_CONCURRENT_JOBS``
defaulting to 1 (jobs run inline in the dispatcher) it froze every job queued
behind it, while the UI sat on *Cancelling...*.

These tests pin the three halves of the fix: a non-zero, size-scaled default
bound; ``cancel_event`` threaded through ``ToolPlugin.verify()`` to every tool;
and a job manager that applies the bound itself and treats a cancelled verify as
a cancellation rather than a verification failure.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.models import ConversionMode, JobStatus
from app.services import job_manager as job_manager_module
from app.services.chdman import ChdmanService
from app.services.concurrency_manager import ConcurrencyManager
from app.services.job_manager import JobManager
from app.services.lock_manager import LockManager
from app.services.subprocess_runner import collect_verify
from app.services.timeout_policy import compute_size_scaled_timeout
from app.services.tools import registry
from app.utils.delete_plan import build_delete_snapshot

VERIFYING_TOOLS = [t for t in registry.all() if hasattr(t, "verify")]


def _runner_module(service):
    """The ``subprocess_runner`` module copy this service's runner came from.

    Intra-project imports are written ``from services.x import y`` while the
    tests import ``app.services.x``, so the two paths produce distinct module
    objects. Patch the one the running code actually reads.
    """
    return sys.modules[type(service._runner).__module__]


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# --- the bound -----------------------------------------------------------


def test_verify_bound_scales_with_the_file_being_read(tmp_path):
    """One flat number cannot serve a 400 MB CIA and a 90 GB disc image."""
    small = tmp_path / "small.chd"
    small.write_bytes(b"x" * 1024)
    big = tmp_path / "big.chd"
    big.write_bytes(b"x" * (4 * 1024 * 1024))

    def bound(path: Path) -> int:
        return compute_size_scaled_timeout(
            path=str(path),
            base_timeout=1800,
            timeout_per_gib=600,
            timeout_cap=86400,
        )

    assert bound(small) == 1800  # under a GiB: the baseline dominates
    assert bound(big) >= bound(small)
    # A 40 GiB image would ask for 1800 + 24000s, which the cap holds at 24h.
    assert compute_size_scaled_timeout(
        path=str(big), base_timeout=1800, timeout_per_gib=10 ** 9, timeout_cap=86400,
    ) == 86400


def test_verify_bound_of_zero_still_disables_it(tmp_path):
    """An operator who sets the baseline to 0 gets no bound, as documented."""
    f = tmp_path / "f.chd"
    f.write_bytes(b"x" * 4096)
    assert compute_size_scaled_timeout(
        path=str(f), base_timeout=0, timeout_per_gib=600, timeout_cap=86400,
    ) == 0


@pytest.mark.parametrize("tool", VERIFYING_TOOLS, ids=lambda t: t.id)
def test_every_verifying_tool_reports_a_nonzero_bound(tool, tmp_path):
    """The contract every tool now owes: verify(path) ends on its own.

    Resolved through the tool so a per-tool COMPRESSATORIUM_<OWNER>_VERIFY_TIMEOUT
    override is what applies, not a shared default that would quietly overrule it.
    """
    target = tmp_path / "out.bin"
    target.write_bytes(b"x" * 4096)
    assert asyncio.run(tool.verify_timeout(str(target))) > 0


# --- cancellation --------------------------------------------------------


@pytest.mark.parametrize("tool", VERIFYING_TOOLS, ids=lambda t: t.id)
def test_every_verifying_tool_accepts_a_cancel_event(tool):
    """Cancel has to reach the verifier for every tool, not just the streaming ones."""
    params = inspect.signature(tool.verify).parameters
    assert "cancel_event" in params
    assert params["cancel_event"].kind is inspect.Parameter.KEYWORD_ONLY
    if hasattr(tool, "verify_stream"):
        assert "cancel_event" in inspect.signature(tool.verify_stream).parameters


def test_collect_verify_preserves_the_cancelled_flag():
    """A cancelled verify proved nothing; it must not read as a failed one."""

    async def _stream():
        yield {"type": "progress", "progress": 10, "message": "Verifying..."}
        yield {
            "type": "error",
            "valid": False,
            "cancelled": True,
            "message": "Verification cancelled",
        }

    result = asyncio.run(collect_verify(_stream(), fallback_message="nope"))
    assert result == {
        "valid": False,
        "message": "Verification cancelled",
        "cancelled": True,
    }


def _fake_tool_binary(path: Path, body: str) -> str:
    path.write_text("#!/usr/bin/env python3\nimport sys, time\n" + body)
    path.chmod(0o755)
    return str(path)


def test_cancelling_a_verify_terminates_the_verifier(tmp_path):
    """Pressing Cancel during verify stops the child, promptly.

    Before the fix the delete-on-verify call site passed no event at all, so the
    await never observed one: the UI showed *Cancelling...* and the verifier ran
    to completion (or forever).
    """
    asyncio.run(_cancel_terminates_verifier(tmp_path))


async def _cancel_terminates_verifier(tmp_path: Path):
    service = ChdmanService()
    service.chdman_path = _fake_tool_binary(
        tmp_path / "fake_chdman.py",
        "print('Verifying, 1% complete')\nsys.stdout.flush()\ntime.sleep(60)\n",
    )
    cancel_event = asyncio.Event()
    task = asyncio.create_task(
        service.verify(str(tmp_path / "sample.chd"), cancel_event=cancel_event),
    )

    for _ in range(100):
        if service.active_pids():
            break
        await asyncio.sleep(0.05)
    assert service.active_pids(), "verify did not start in time"
    pid = service.active_pids()[0]

    cancel_event.set()
    result = await asyncio.wait_for(task, timeout=10)

    assert result["cancelled"] is True
    assert result["valid"] is False
    assert service.active_pids() == []
    assert not _pid_exists(pid)


def test_a_silent_verifier_trips_the_stall_bound(tmp_path, monkeypatch):
    """A streaming verify that goes quiet is caught long before the overall bound."""
    asyncio.run(_silent_verifier_stalls(tmp_path, monkeypatch))


async def _silent_verifier_stalls(tmp_path: Path, monkeypatch):
    service = ChdmanService()
    service.chdman_path = _fake_tool_binary(
        tmp_path / "silent_chdman.py",
        "print('chdman - MAME Compressed Hunks of Data')\n"
        "sys.stdout.flush()\ntime.sleep(60)\n",
    )
    runner_mod = _runner_module(service)
    monkeypatch.setattr(runner_mod.settings, "tool_verify_progress_timeout", 1)

    result = await asyncio.wait_for(
        service.verify(str(tmp_path / "sample.chd")), timeout=20,
    )

    assert result["valid"] is False
    assert "stalled" in result["message"].lower()
    assert service.active_pids() == []


# --- the job pipeline ----------------------------------------------------


async def _run_delete_on_verify_job(tmp_path: Path, monkeypatch, fake_verify):
    """Drive one z3ds delete-on-verify job with ``fake_verify`` as the verifier."""
    source_path = tmp_path / "game.3ds"
    output_path = tmp_path / "game.z3ds"
    source_path.write_bytes(b"source")

    monkeypatch.setattr(job_manager_module.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(job_manager_module.settings, "data_mount_root", str(tmp_path))
    # Fresh, tmp-bound coordinators: these suites run in-process, so a FIFO
    # ticket leaked by another test would wedge this job before it ever reached
    # the verify stage under test.
    lock_dir = tmp_path / "locks"
    monkeypatch.setattr(
        job_manager_module.settings, "concurrency_lock_dir", str(lock_dir),
    )
    monkeypatch.setattr(
        job_manager_module,
        "concurrency_manager",
        ConcurrencyManager(1, str(lock_dir / "conc")),
    )
    monkeypatch.setattr(job_manager_module, "lock_manager", LockManager())

    async def fake_convert(
        input_path: str,
        destination_path: str,
        mode: str = "z3ds_compress",
        compression: str | None = None,
        cancel_event=None,
    ):
        Path(destination_path).write_bytes(b"converted")
        yield {"progress": 100, "message": "Done"}

    service = job_manager_module.registry.for_mode("z3ds_compress")._service
    monkeypatch.setattr(service, "convert", fake_convert)
    monkeypatch.setattr(service, "verify", fake_verify)
    monkeypatch.setattr(
        job_manager_module.verification_store, "mark_verified", AsyncMock(),
    )
    monkeypatch.setattr(
        job_manager_module,
        "build_delete_plan",
        lambda path: {
            "delete_paths": [os.path.realpath(str(source_path))],
            "missing_paths": [],
            "unsafe_paths": [],
            "errors": [],
        },
    )

    manager = JobManager(max_concurrent=1, max_job_history=5)
    job = await manager.create_job(
        str(source_path),
        ConversionMode.Z3DS_COMPRESS,
        output_path=str(output_path),
        delete_on_verify=True,
        delete_snapshot=build_delete_snapshot(str(source_path)),
    )
    await manager._process_job(job.id)
    return manager, job, source_path


@pytest.mark.asyncio
async def test_job_passes_its_cancel_event_into_verify(tmp_path: Path, monkeypatch):
    """The job's own event reaches the verifier -- the gap issue #266 opens with."""
    seen: dict[str, object] = {}

    async def fake_verify(path: str, *, cancel_event=None):
        seen["event"] = cancel_event
        return {"valid": True, "message": "ok"}

    _manager, job, _source = await _run_delete_on_verify_job(
        tmp_path, monkeypatch, fake_verify,
    )

    assert job.status == JobStatus.COMPLETED
    assert isinstance(seen["event"], asyncio.Event)


@pytest.mark.asyncio
async def test_cancelled_verify_cancels_the_job_and_spares_the_source(
    tmp_path: Path, monkeypatch,
):
    """A verify stopped by Cancel is not a verification failure.

    It reached no verdict, so the job is CANCELLED (not FAILED) and the source
    survives -- deleting it on the strength of a run that never finished would
    risk the only copy.
    """

    async def fake_verify(path: str, *, cancel_event=None):
        return {
            "valid": False,
            "cancelled": True,
            "message": "Verification cancelled",
        }

    _manager, job, source_path = await _run_delete_on_verify_job(
        tmp_path, monkeypatch, fake_verify,
    )

    assert job.status == JobStatus.CANCELLED
    assert source_path.exists()


@pytest.mark.asyncio
async def test_a_verify_that_never_returns_is_bounded_by_the_job(
    tmp_path: Path, monkeypatch,
):
    """The backstop: the job applies the bound even if the tool ignores it.

    A tool whose verify never spawns a subprocess (the Wii U container walk, the
    PS3 PARAM.SFO readback) has nothing for a subprocess timeout to bound, so the
    guarantee has to live at the call site too.
    """

    async def hanging_verify(path: str, *, cancel_event=None):
        await asyncio.sleep(60)
        return {"valid": True, "message": "never"}

    async def tiny_bound(_path):
        return 0.2

    tool = job_manager_module.registry.for_mode("z3ds_compress")
    monkeypatch.setattr(tool, "verify_timeout", tiny_bound)

    _manager, job, source_path = await asyncio.wait_for(
        _run_delete_on_verify_job(tmp_path, monkeypatch, hanging_verify),
        timeout=20,
    )

    assert job.status == JobStatus.FAILED
    assert "timed out" in (job.error_message or "").lower()
    assert source_path.exists()
