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
import contextlib
import inspect
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from app.models import ConversionMode, JobStatus
from app.routes import info as info_routes
from app.services import z3ds_compress as z3ds_module
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


# --- review follow-ups ---------------------------------------------------
#
# Three gaps found reviewing the change: two spawn-then-await windows where a
# cancellation (the verify SSE route cancels its task on client disconnect)
# would strand a running child, and the verify *routes* — a second entry point
# into the same verifiers, holding the same workload lane — never applying the
# bound at all.


def test_cancelling_during_bound_resolution_strands_no_verifier(tmp_path, monkeypatch):
    """No await may sit between spawning the verifier and its cleanup block.

    The bound is resolved from a filesystem probe. Resolving it after the spawn
    left a window where cancellation unwound the coroutine with the child
    running and tracked but nothing to reap or untrack it, so a client that
    disconnected repeatedly could pile up full-disc verifiers.
    """
    asyncio.run(_cancel_during_bound_resolution(tmp_path, monkeypatch))


async def _cancel_during_bound_resolution(tmp_path: Path, monkeypatch):
    service = ChdmanService()
    service.chdman_path = _fake_tool_binary(
        tmp_path / "fake_chdman.py", "time.sleep(60)\n",
    )
    runner_mod = _runner_module(service)
    resolving = asyncio.Event()

    async def _slow_bound(_path, _owner=None):
        resolving.set()
        await asyncio.sleep(30)
        return 0

    monkeypatch.setattr(runner_mod, "resolve_verify_timeout", _slow_bound)

    task = asyncio.create_task(service.verify(str(tmp_path / "sample.chd")))
    await asyncio.wait_for(resolving.wait(), timeout=5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert service.active_pids() == []


def test_z3ds_cancelling_during_bound_resolution_strands_no_zstd(tmp_path, monkeypatch):
    """Same window, same rule, for the tool that feeds zstd on stdin."""
    asyncio.run(_z3ds_cancel_during_bound_resolution(tmp_path, monkeypatch))


async def _z3ds_cancel_during_bound_resolution(tmp_path: Path, monkeypatch):
    rom = tmp_path / "game.z3ds"
    rom.write_bytes(b"Z3DS" + b"\0" * 4096)
    resolving = asyncio.Event()

    async def _slow_bound(_path, _owner=None):
        resolving.set()
        await asyncio.sleep(30)
        return 0

    monkeypatch.setattr(z3ds_module.shutil, "which", lambda _name: "/usr/bin/zstd")
    monkeypatch.setattr(z3ds_module, "resolve_verify_timeout", _slow_bound)
    async def _offset(_path, *, cancel_event=None):
        return 0

    monkeypatch.setattr(z3ds_module.z3ds_compress_service, "_get_verify_payload_offset", _offset)

    service = z3ds_module.z3ds_compress_service
    before = set(service.active_pids())

    async def _drain():
        async for _update in service.verify_stream(str(rom)):
            pass

    task = asyncio.create_task(_drain())
    await asyncio.wait_for(resolving.wait(), timeout=5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert set(service.active_pids()) == before


@pytest.mark.asyncio
async def test_verify_route_applies_the_bound(tmp_path, monkeypatch):
    """The routes bound the verify too, not just delete-on-verify jobs.

    ``/jwud-verify`` and friends call the service directly, so without this a
    wedged pure-Python verifier (nothing for a subprocess timeout to stop) holds
    the verify workload lane forever and the configured bound does nothing.
    """
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))
    store = Mock()
    store.mark_verified = AsyncMock()
    monkeypatch.setattr(info_routes, "verification_store", store)

    target = tmp_path / "game.wux"
    target.write_bytes(b"WUX0" + b"\0" * 1024)

    service = Mock()

    async def _hanging_verify(path, *, cancel_event=None):
        await asyncio.sleep(60)
        return {"valid": True, "message": "never"}

    service.verify = _hanging_verify
    monkeypatch.setattr(info_routes, "jwudtool_service", service)

    async def _tiny_bound(_path):
        return 0.2

    # The route module resolves its own registry copy (intra-project imports are
    # written `from services.x import y`, the tests use `app.services.x`), so
    # patch the plugin object the route actually holds.
    monkeypatch.setattr(info_routes.registry.get("jwud"), "verify_timeout", _tiny_bound)

    result = await asyncio.wait_for(
        info_routes.verify_jwud(path=str(target)), timeout=20,
    )

    assert result["valid"] is False
    assert "timed out" in result["message"].lower()
    # A verify that never finished proves nothing, so nothing is recorded.
    store.mark_verified.assert_not_called()


@pytest.mark.asyncio
async def test_disconnecting_during_bound_resolution_frees_the_verify_lane(
    tmp_path, monkeypatch,
):
    """The verify lane's token survives a client disconnect at any point.

    The SSE generator takes the token, then resolves the bound (a filesystem
    probe), then installs its cleanup. Resolving outside that cleanup meant a
    disconnect landing on the probe leaked the token — permanently, with the
    default one-slot lane, so every later verification was refused as
    at-capacity.
    """
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))
    target = tmp_path / "game.wux"
    target.write_bytes(b"WUX0" + b"\0" * 1024)

    resolving = asyncio.Event()

    async def _slow_bound(_path):
        resolving.set()
        await asyncio.sleep(30)
        return 0

    monkeypatch.setattr(info_routes.registry.get("jwud"), "verify_timeout", _slow_bound)

    limiter = info_routes.workload_limiter
    before = limiter.in_use("verify")

    response = await info_routes.verify_jwud_events(path=str(target))

    async def _consume():
        async for _event in response.body_iterator:
            pass

    task = asyncio.create_task(_consume())
    await asyncio.wait_for(resolving.wait(), timeout=5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert limiter.in_use("verify") == before


@pytest.mark.asyncio
async def test_verify_preflight_is_bounded_and_shared(tmp_path, monkeypatch):
    """The missing/empty/extension gate every verify opens with runs off the loop.

    These checks used to `os.path.exists`/`getsize` inline. On an unresponsive
    mount that blocks the event loop itself, so the `wait_for` meant to bound the
    verify never gets to fire — the bound is only as good as the first syscall.
    """
    from app.services import subprocess_runner as runner_mod

    target = tmp_path / "game.wux"
    target.write_bytes(b"x" * 16)

    # Fine: a real file passes the gate and reports its size.
    problem, size = await runner_mod.verify_preflight(str(target), {".wux"})
    assert problem is None and size == 16

    # Missing / empty / wrong extension keep their existing verdicts.
    missing, _ = await runner_mod.verify_preflight(str(tmp_path / "nope.wux"), {".wux"})
    assert missing["message"] == "File not found"
    (tmp_path / "empty.wux").write_bytes(b"")
    empty, _ = await runner_mod.verify_preflight(str(tmp_path / "empty.wux"), {".wux"})
    assert empty["message"] == "File is empty"
    wrong, _ = await runner_mod.verify_preflight(str(target), {".chd"})
    assert "extension" in wrong["message"]

    # A stat that never answers gives up instead of blocking the loop forever.
    # Narrow to this one path: `runner_mod.os` is the process-wide os module,
    # so an unconditional fake would sleep out every other getsize in the
    # interpreter (pytest plugins, logging, tasks left by earlier tests).
    real_getsize = runner_mod.os.path.getsize

    def _never_returns_for_target(path):
        import time as _time

        if str(path) != str(target):
            return real_getsize(path)
        _time.sleep(30)
        return 0

    monkeypatch.setattr(runner_mod, "_STAT_TIMEOUT", 0.2)
    monkeypatch.setattr(runner_mod.os.path, "getsize", _never_returns_for_target)
    wedged, _ = await asyncio.wait_for(
        runner_mod.verify_preflight(str(target), {".wux"}), timeout=10,
    )
    assert "stopped responding" in wedged["message"]


class _StubStdin:
    def write(self, _chunk: bytes) -> None:
        return None

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class _HangingChild:
    """Accepts the whole payload, then never finishes."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.stdin = _StubStdin()
        self.stdout = None
        self.stderr = None
        self.returncode = None

    async def communicate(self):
        await asyncio.sleep(120)
        return b"", b""

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = -15
        return self.returncode


def test_z3ds_disconnect_leaves_no_orphan_feeder(tmp_path, monkeypatch):
    """Cancelling the z3ds verify tears down its stdin feeder too.

    The feeder is a task of its own. Cancellation entering the wait's cleanup
    used to stop only the cancel watcher, leaving the feeder reading the image
    (or failing later against a closed stdin with nobody awaiting it) — one
    orphan per disconnect.
    """
    asyncio.run(_z3ds_disconnect_leaves_no_orphan_feeder(tmp_path, monkeypatch))


async def _z3ds_disconnect_leaves_no_orphan_feeder(tmp_path: Path, monkeypatch):
    rom = tmp_path / "game.z3ds"
    rom.write_bytes(b"Z3DS" + b"\0" * 65536)
    child = _HangingChild()

    async def _fake_exec(*_args, **_kwargs):
        return child

    async def _offset(_path, *, cancel_event=None):
        return 0

    monkeypatch.setattr(z3ds_module.shutil, "which", lambda _name: "/usr/bin/zstd")
    monkeypatch.setattr(z3ds_module.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(
        z3ds_module.z3ds_compress_service, "_get_verify_payload_offset", _offset,
    )

    service = z3ds_module.z3ds_compress_service

    async def _drain():
        async for _update in service.verify_stream(str(rom)):
            pass

    before = {t for t in asyncio.all_tasks()}
    task = asyncio.create_task(_drain())
    for _ in range(100):
        if child.pid in set(service.active_pids()):
            break
        await asyncio.sleep(0.05)
    assert child.pid in set(service.active_pids()), "verify did not start in time"

    # The feeder must be observably alive *before* the cancel, so this can never
    # pass vacuously because the helper was renamed or inlined.
    helpers = {t for t in asyncio.all_tasks() if t not in before and t is not task}
    assert helpers, "the stdin feeder task was never started"

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert [t for t in helpers if not t.done()] == []
    assert child.pid not in set(service.active_pids())


@pytest.mark.asyncio
async def test_verify_reads_do_not_occupy_shared_pool_workers(tmp_path, monkeypatch):
    """A wedged verify read must not cost a shared worker.

    A thread cannot be cancelled, only abandoned — so the question is *whose*
    thread. Cancelling a `run_in_threadpool` read abandons one of the process's
    small fixed set of workers, and now that verify is genuinely cancellable and
    bounded, a client disconnecting repeatedly against a dead mount would strand
    one per attempt until unrelated offloads had none left.
    """
    from app.services import subprocess_runner as runner_mod

    started = asyncio.Event()
    release = __import__("threading").Event()
    finished: list[str] = []

    def _blocking_read() -> str:
        started.set()
        release.wait(30)
        finished.append("done")
        return "done"

    task = asyncio.create_task(runner_mod.run_detached(_blocking_read))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # The awaiter is free immediately, and the abandoned work runs on its own
    # throwaway thread rather than holding a pooled slot.
    assert finished == []
    release.set()


def test_verify_paths_never_use_the_shared_threadpool():
    """The verify reads stay off `run_in_threadpool` / `asyncio.to_thread`.

    Pinned by inspection rather than behaviour: the failure this prevents (a
    starved shared pool) only shows up under a dead mount plus repeated
    cancellation, which no unit test can stage honestly.
    """
    import inspect as _inspect

    from app.services import jwudtool, makeps3iso, romz
    from app.services import z3ds_compress as z3ds

    sources = {
        "jwud": _inspect.getsource(jwudtool.JwudToolService.verify_stream),
        "romz": _inspect.getsource(romz.RomzService.verify_stream),
        "z3ds": _inspect.getsource(z3ds.Z3DSCompressService._get_verify_payload_offset),
        "makeps3iso": _inspect.getsource(makeps3iso.MakePs3IsoService.verify),
    }
    for tool, source in sources.items():
        assert "run_in_threadpool" not in source, tool
        assert "to_thread" not in source, tool
        assert "run_detached" in source, tool


@pytest.mark.asyncio
async def test_a_verdict_within_the_bound_is_reported_not_timed_out(
    tmp_path, monkeypatch,
):
    """A verify that finishes inside its bound reports its verdict.

    This replaces an earlier test for a race the design has since removed. When
    the deadline lived in the *consuming* loop, a `complete` already sitting in
    the queue could be discarded by an expiry checked a moment later; the test
    pinned that the verdict won. The deadline now lives in the producing task,
    so one task decides both and that disagreement cannot arise — what is worth
    pinning is the outcome: finish in time and your verdict stands, overrun and
    you get a timeout with nothing recorded.
    """
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))
    store = Mock()
    store.mark_verified = AsyncMock()
    monkeypatch.setattr(info_routes, "verification_store", store)

    target = tmp_path / "game.wux"
    target.write_bytes(b"WUX0" + b"\0" * 1024)

    delay = 0.05

    async def _verify_stream(path, *, cancel_event=None):
        await asyncio.sleep(delay)
        yield {"type": "complete", "valid": True, "message": "verified"}

    service = Mock()
    service.verify_stream = _verify_stream
    monkeypatch.setattr(info_routes, "jwudtool_service", service)

    async def _bound(_path):
        return 1

    monkeypatch.setattr(info_routes.registry.get("jwud"), "verify_timeout", _bound)

    response = await info_routes.verify_jwud_events(path=str(target))
    events = [e async for e in response.body_iterator if isinstance(e, dict)]

    assert events[-1]["event"] == "verify_complete"
    store.mark_verified.assert_called_once_with(str(target))

    # And the other side of the line: overrun the bound and the verdict never
    # arrives, because the producer itself was stopped.
    store.mark_verified.reset_mock()
    delay = 5

    response = await info_routes.verify_jwud_events(path=str(target))
    events = [e async for e in response.body_iterator if isinstance(e, dict)]

    assert events[-1]["event"] == "verify_error"
    assert "timed out" in events[-1]["data"].lower()
    store.mark_verified.assert_not_called()


def test_cancel_reaches_a_detached_verify_read(tmp_path, monkeypatch):
    """Cancel must not wait out a blocking read that has already started.

    jwud's index scan is the case: minutes of reading on a 25 GB image with no
    interruption point. The read itself cannot be stopped, but the await on it
    can, and that is what frees the job (and the single-slot dispatcher).
    """
    asyncio.run(_cancel_reaches_detached_read(tmp_path, monkeypatch))


async def _cancel_reaches_detached_read(tmp_path: Path, monkeypatch):
    import threading

    from app.services import jwudtool as jwud_module

    target = tmp_path / "game.wux"
    target.write_bytes(b"WUX0" + b"\0" * 4096)

    reading = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def _endless_header(_path):
        loop.call_soon_threadsafe(reading.set)
        release.wait(30)
        return {}

    monkeypatch.setattr(jwud_module, "read_wux_header", _endless_header)

    cancel_event = asyncio.Event()
    task = asyncio.create_task(
        jwud_module.jwudtool_service.verify(str(target), cancel_event=cancel_event),
    )
    await asyncio.wait_for(reading.wait(), timeout=5)

    cancel_event.set()
    result = await asyncio.wait_for(task, timeout=5)

    assert result["cancelled"] is True
    assert result["valid"] is False
    release.set()


def test_an_unkillable_verifier_is_not_reaped_twice(tmp_path, monkeypatch):
    """One exhausted TERM->KILL ladder is enough.

    A child that survived SIGKILL will survive it again, so repeating the ladder
    in teardown just holds the verify lane — and the single-slot queue behind it
    — for another 15 seconds with no possible new outcome.
    """
    asyncio.run(_unkillable_verifier_reaped_once(tmp_path, monkeypatch))


async def _unkillable_verifier_reaped_once(tmp_path: Path, monkeypatch):
    from app.services.chdman import ChdmanService

    service = ChdmanService()
    service.chdman_path = _fake_tool_binary(tmp_path / "quick.py", "pass\n")
    runner = service._runner
    calls: list[float] = []

    async def _never_reaped(_self, _process, *, exit_timeout=None):
        calls.append(exit_timeout if exit_timeout is not None else -1)
        return False

    monkeypatch.setattr(type(runner), "reap", _never_reaped)

    result = await asyncio.wait_for(
        service.verify(str(tmp_path / "sample.chd")), timeout=20,
    )

    assert result["valid"] is False
    assert "could not be killed" in result["message"]
    assert len(calls) == 1, f"the ladder ran {len(calls)}x on an abandoned child"


@pytest.mark.asyncio
async def test_a_chatty_verifier_still_hits_the_batch_bound(tmp_path, monkeypatch):
    """A verifier that never stops talking must still expire.

    Guarding the batch deadline on an empty queue — added so a verdict arriving
    at the same instant would win — let a verifier printing progress faster than
    the route drains it keep the queue non-empty and never expire at all.
    """
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(info_routes, "verification_store", Mock(mark_verified=AsyncMock()))

    target = tmp_path / "game.wux"
    target.write_bytes(b"WUX0" + b"\0" * 1024)

    service = Mock()

    async def _chatty(path, *, cancel_event=None):
        while True:
            # Slow enough that the 0.2s bound expires within a handful of
            # events, fast enough that the queue is never empty when checked.
            yield {"type": "progress", "progress": 1, "message": "still going"}
            await asyncio.sleep(0.01)

    service.verify_stream = _chatty
    monkeypatch.setattr(info_routes, "jwudtool_service", service)

    async def _tiny_bound(_path):
        return 0.2

    monkeypatch.setattr(info_routes.registry.get("jwud"), "verify_timeout", _tiny_bound)

    token = await info_routes.workload_limiter.try_acquire("verify")
    response = info_routes._sse_batch_from_verify_stream(
        info_routes.registry.get("jwud"),
        info_routes._VERIFY_CONFIG["jwud"],
        [str(target)],
        token,
    )
    events = []
    async for event in response.body_iterator:
        if isinstance(event, dict):
            events.append(event)
        if len(events) > 200:  # a runaway loop would never reach the end
            break

    completions = [e for e in events if e["event"] == "verify_batch_file_complete"]
    assert completions, "the batch never finished the file"
    assert "timed out" in completions[-1]["data"].lower()


@pytest.mark.asyncio
async def test_cancel_during_preflight_reports_a_cancellation(tmp_path, monkeypatch):
    """A cancel while the volume is not answering is a cancel, not a failure.

    The preflight stat is bounded at 10s; without the event it reported "stopped
    responding" — a verification *failure* — for a job the operator had already
    cancelled.
    """
    import threading

    from app.services import subprocess_runner as runner_mod

    target = tmp_path / "game.wux"
    target.write_bytes(b"WUX0" + b"\0" * 64)

    probing = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    real_getsize = runner_mod.os.path.getsize

    def _wedged_for_target(path):
        if str(path) != str(target):
            return real_getsize(path)
        loop.call_soon_threadsafe(probing.set)
        release.wait(30)
        return 0

    monkeypatch.setattr(runner_mod.os.path, "getsize", _wedged_for_target)

    cancel_event = asyncio.Event()
    task = asyncio.create_task(
        runner_mod.verify_preflight(
            str(target), {".wux"}, cancel_event=cancel_event,
        ),
    )
    await asyncio.wait_for(probing.wait(), timeout=5)
    cancel_event.set()

    problem, _size = await asyncio.wait_for(task, timeout=5)
    assert problem["cancelled"] is True
    assert problem["valid"] is False
    release.set()


@pytest.mark.asyncio
async def test_route_guard_is_bounded_and_off_the_shared_pool(tmp_path, monkeypatch):
    """The path checks that precede a verify are bounded too.

    They land ahead of every bound the verify itself carries, so on a volume
    that stopped answering the request hung before any of that applied — and in
    the app's shared pool, one worker went with each attempt.
    """
    import threading

    # The module the route's helper actually came from (see _runner_module).
    runner_mod = sys.modules[info_routes.bounded_path_check.__module__]

    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))
    target = tmp_path / "game.wux"
    target.write_bytes(b"WUX0" + b"\0" * 64)

    release = threading.Event()

    def _wedged(*_args, **_kwargs):
        release.wait(30)
        return True

    monkeypatch.setattr(runner_mod, "_STAT_TIMEOUT", 0.2)
    monkeypatch.setattr(info_routes, "is_within_configured_volumes", _wedged)

    with pytest.raises(info_routes.HTTPException) as excinfo:
        await asyncio.wait_for(
            info_routes.verify_jwud(path=str(target)), timeout=10,
        )

    # 503, not 403/404: the path was never judged, the storage just went quiet.
    assert excinfo.value.status_code == 503
    release.set()


def test_streaming_verify_spawns_without_a_preexec_fork_hook():
    """No `preexec_fn` on the verify spawn.

    Forking a Python callable from this multithreaded process can deadlock the
    child before it reaches exec — inside `create_subprocess_exec`, before the
    PID is tracked and before any bound is installed, which is the one failure
    none of this could rescue. nice/ionice are applied as exec-only wrappers
    instead, the same way maxcso and nsz already do it.
    """
    import inspect as _inspect

    from app.services.subprocess_runner import SubprocessRunner

    source = _inspect.getsource(SubprocessRunner.run_verify)
    # The argument, not the word: the comment above the spawn explains why it
    # is absent, and should keep explaining it.
    assert "preexec_fn=" not in source
    assert "nice_prefix(self._owner)" in source


def test_cancelling_the_z3ds_feeder_does_not_close_behind_a_stuck_read(
    tmp_path, monkeypatch,
):
    """A cancelled feeder abandons its handle instead of closing it.

    Only `ReadCancelled` marked the handle abandoned, so a *task* cancellation
    (SSE disconnect, the timeout race) fell through to the close in the finally
    — and that close waits on the lock the abandoned read still holds, which is
    the unbounded wait again, now in cleanup.
    """
    asyncio.run(_z3ds_feeder_cancel_abandons_handle(tmp_path, monkeypatch))


async def _z3ds_feeder_cancel_abandons_handle(tmp_path: Path, monkeypatch):
    import threading

    rom = tmp_path / "game.z3ds"
    rom.write_bytes(b"Z3DS" + b"\0" * 65536)
    child = _HangingChild(pid=5150)

    reading = asyncio.Event()
    release = threading.Event()
    closed: list[str] = []
    loop = asyncio.get_running_loop()

    class _StuckHandle:
        def seek(self, _offset):
            return 0

        def read(self, _size):
            loop.call_soon_threadsafe(reading.set)
            release.wait(30)  # the read that never comes back
            return b""

        def close(self):
            # Would block behind the read on a real handle; recorded so the test
            # can assert the cancelled path never gets here.
            closed.append("closed")

    monkeypatch.setattr(z3ds_module, "open", lambda *_a, **_k: _StuckHandle(), raising=False)

    async def _fake_exec(*_args, **_kwargs):
        return child

    async def _offset(_path, *, cancel_event=None):
        return 0

    monkeypatch.setattr(z3ds_module.shutil, "which", lambda _name: "/usr/bin/zstd")
    monkeypatch.setattr(z3ds_module.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(
        z3ds_module.z3ds_compress_service, "_get_verify_payload_offset", _offset,
    )

    service = z3ds_module.z3ds_compress_service

    async def _drain():
        async for _update in service.verify_stream(str(rom)):
            pass

    task = asyncio.create_task(_drain())
    await asyncio.wait_for(reading.wait(), timeout=5)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)

    assert closed == [], "the cancelled feeder closed a handle a stuck read still holds"
    release.set()


def test_post_eof_grace_respects_the_remaining_bound(tmp_path, monkeypatch):
    """A verifier that closes stdout without exiting doesn't get a free minute.

    The teardown reap used its default 60s voluntary-exit grace regardless of
    the verify's own (possibly much shorter) deadline or a cancel that had
    already fired, so the lane stayed held well past both.
    """
    asyncio.run(_post_eof_grace_bounded(tmp_path, monkeypatch))


async def _post_eof_grace_bounded(tmp_path: Path, monkeypatch):
    service = ChdmanService()
    # Closes the pipe outright, then lingers well past the grace. Both fds: the
    # runner points stderr at the same pipe, so closing stdout alone leaves the
    # write end open and the parent never sees EOF.
    service.chdman_path = _fake_tool_binary(
        tmp_path / "quiet_chdman.py",
        "import os\nos.close(1)\nos.close(2)\ntime.sleep(60)\n",
    )
    runner_mod = _runner_module(service)

    async def _short_bound(_path, _owner=None):
        return 2

    monkeypatch.setattr(runner_mod, "resolve_verify_timeout", _short_bound)

    graces: list[float] = []
    real_reap = type(service._runner).reap

    _DEFAULTED = object()

    async def _recording_reap(self, process, *, exit_timeout=_DEFAULTED):
        graces.append(exit_timeout)
        if exit_timeout is _DEFAULTED:
            return await real_reap(self, process)
        return await real_reap(self, process, exit_timeout=exit_timeout)

    monkeypatch.setattr(type(service._runner), "reap", _recording_reap)

    await asyncio.wait_for(service.verify(str(tmp_path / "sample.chd")), timeout=30)

    # Every reap states its grace explicitly — falling back to the 60s default
    # is the regression — and none exceeds what was left of the 2s bound.
    assert graces, "reap was never called"
    assert _DEFAULTED not in graces, "a reap used the default 60s grace"
    assert max(graces) <= 2, f"grace exceeded the remaining bound: {graces}"


def test_stuck_probes_are_capped_rather_than_accumulating(monkeypatch):
    """A dead volume must not turn into an unbounded pile of stuck threads.

    Abandoning one thread per wedged syscall is the deliberate trade — nothing
    can cancel a syscall — but a request-driven probe can be retried forever,
    and one leaked thread per attempt eventually takes the process with it.
    Past the ceiling the next probe is refused instead of started.
    """
    asyncio.run(_stuck_probes_are_capped(monkeypatch))


async def _stuck_probes_are_capped(monkeypatch):
    import threading

    from app.services import subprocess_runner as runner_mod

    release = threading.Event()
    started = threading.Semaphore(0)

    def _wedged():
        started.release()
        release.wait(30)
        return 0

    monkeypatch.setattr(runner_mod, "_MAX_DETACHED_PROBES", 4)
    monkeypatch.setattr(runner_mod, "_probe_slots", threading.Semaphore(4))

    futures = [runner_mod._probe_in_daemon_thread(_wedged) for _ in range(4)]
    for _ in range(4):
        assert started.acquire(timeout=5), "probe threads did not start"

    # The fifth is refused, promptly, instead of adding another stuck thread.
    with pytest.raises(runner_mod.ProbeCapacityExceeded):
        runner_mod._probe_in_daemon_thread(_wedged)

    # And it reads as a timeout to every existing handler.
    assert issubclass(runner_mod.ProbeCapacityExceeded, asyncio.TimeoutError)

    release.set()
    for future in futures:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(future, timeout=5)

    # Slots come back once the calls actually return.
    runner_mod._probe_in_daemon_thread(lambda: 0)


@pytest.mark.asyncio
async def test_the_bound_holds_when_the_sse_client_stops_reading(tmp_path, monkeypatch):
    """A stalled reader must not suspend the deadline along with the loop.

    The consuming loop is suspended at its `yield` whenever the client stops
    draining — so a deadline evaluated there stops being evaluated exactly when
    it matters, and the verifier runs on holding the verify lane. The producer
    task carries the bound now, so it fires with nobody reading.
    """
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(
        info_routes, "verification_store", Mock(mark_verified=AsyncMock()),
    )

    target = tmp_path / "game.wux"
    target.write_bytes(b"WUX0" + b"\0" * 1024)

    stopped = asyncio.Event()

    async def _endless(path, *, cancel_event=None):
        try:
            while True:
                yield {"type": "progress", "progress": 1, "message": "working"}
                await asyncio.sleep(0.01)
        finally:
            # Reached when the producer's own deadline cancels the pump.
            stopped.set()

    service = Mock()
    service.verify_stream = _endless
    monkeypatch.setattr(info_routes, "jwudtool_service", service)

    async def _tiny_bound(_path):
        return 0.3

    monkeypatch.setattr(info_routes.registry.get("jwud"), "verify_timeout", _tiny_bound)

    response = await info_routes.verify_jwud_events(path=str(target))
    iterator = response.body_iterator.__aiter__()

    # Read one event, then stop reading entirely — the loop is now parked at its
    # yield and cannot check any clock.
    await iterator.__anext__()
    await asyncio.wait_for(stopped.wait(), timeout=5)
