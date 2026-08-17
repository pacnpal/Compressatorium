"""An abandoned child must stop the walk, not fail one file (issue #268).

``run_capture()`` used to return ``(None, b"", b"")`` for both "the child was
killed after a timeout" and "the child outlived SIGKILL and is still running".
Callers reasonably read that as "this attempt didn't work, move on" -- so a
batch against unresponsive storage stranded **one live process per file**, each
invisible to the app because its PID had already been untracked.

These tests lock the two halves of the fix: the runner raises
``SubprocessAbandoned`` instead of returning an ordinary abort, and every loop
that walks a list of files stops when it sees one.
"""
import asyncio
import contextlib
import json
import sys
from unittest.mock import AsyncMock, Mock

import pytest

from app.routes import dat as dat_routes
from app.routes import info as info_routes
from services.subprocess_runner import (
    SubprocessAbandoned,
    SubprocessRunner,
    reraise_if_abandoned,
)


def _abandoned(label: str = "tool") -> SubprocessAbandoned:
    return SubprocessAbandoned(f"{label} could not be killed", owner="test", pid=4242)


@pytest.fixture
def isolated_dat_store(tmp_path, monkeypatch):
    """A fresh DATStore backed by a temp file, bound into the dat routes."""
    from services.dat_store import DATStore
    store = DATStore(store_path=str(tmp_path / "dat_store.json"))
    monkeypatch.setattr(dat_routes, "dat_store", store)
    return store


# ---------------------------------------------------------------------------
# The signal itself
# ---------------------------------------------------------------------------


def test_subprocess_abandoned_is_a_runtime_error():
    """Existing handlers keep working; only callers that care name the type.

    The contract change is additive by design: every ``except RuntimeError`` /
    ``except Exception`` around a conversion behaves exactly as it did before.
    """
    assert issubclass(SubprocessAbandoned, RuntimeError)


def test_reraise_if_abandoned_unwraps_a_wrapped_cause():
    """A layer that wraps the failure in its own type can't hide the abandonment.

    ``raise EmbeddedHashUnavailable(...) from exc`` is the documented plugin
    contract for "the attempt failed", and that wrapper reads as an ordinary
    per-file problem. The chain walk is what keeps the stop policy tool-agnostic.
    """
    original = _abandoned()
    try:
        try:
            raise original
        except SubprocessAbandoned as exc:
            raise ValueError("could not derive hash") from exc
    except ValueError as wrapped:
        with pytest.raises(SubprocessAbandoned) as excinfo:
            reraise_if_abandoned(wrapped)
        assert excinfo.value is original


def test_reraise_if_abandoned_is_a_noop_for_other_failures():
    """Handlers keep their existing behaviour by calling it first."""
    assert reraise_if_abandoned(OSError("disk gone")) is None


# ---------------------------------------------------------------------------
# run_capture: an ordinary abort vs an abandonment
# ---------------------------------------------------------------------------


def _py_cmd(source: str) -> list[str]:
    return [sys.executable, "-c", source]


def test_run_capture_raises_when_the_child_cannot_be_killed(monkeypatch):
    """reap() giving up must not look like an ordinary timeout."""
    runner = SubprocessRunner(owner="test")

    async def unkillable(process, *, exit_timeout=0.0):
        # Report abandonment, but really kill the child so this test doesn't
        # leak one; the escalation ladder itself is covered by the reap() tests.
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        return False

    monkeypatch.setattr(runner, "reap", unkillable)

    with pytest.raises(SubprocessAbandoned) as excinfo:
        asyncio.run(
            runner.run_capture(
                _py_cmd("import time; time.sleep(30)"),
                timeout=0.2,
                fail_label="pretend-tool",
            ),
        )

    assert "pretend-tool" in str(excinfo.value)
    assert excinfo.value.owner == "test"
    assert excinfo.value.pid is not None
    # The PID is untracked either way -- which is why the exception has to carry
    # it: once this returns, nothing in the app can see that process any more.
    assert not runner.active_pids()


def test_run_capture_timeout_still_reports_an_ordinary_abort():
    """A child that *did* die keeps the cheap (None, b"", b"") signal."""
    runner = SubprocessRunner(owner="test")
    rc, _stdout, _stderr = asyncio.run(
        runner.run_capture(_py_cmd("import time; time.sleep(30)"), timeout=0.2),
    )
    assert rc is None
    assert not runner.active_pids()


# ---------------------------------------------------------------------------
# dolphin: disc_hashes -> embedded_hashes -> _match_single_file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disc_hashes_propagates_instead_of_reporting_no_hash(monkeypatch):
    """An abandoned verify is not "this disc has no hash"."""
    from app.services.dolphin_tool import dolphin_tool_service

    async def fake_run_capture(cmd, *, timeout=None, cancel_event=None,
                               stderr_to_stdout=False, fail_label=None):
        raise _abandoned("dolphin-tool verify")

    monkeypatch.setattr(dolphin_tool_service._runner, "run_capture", fake_run_capture)

    with pytest.raises(SubprocessAbandoned):
        await dolphin_tool_service.disc_hashes("/data/x.rvz")


@pytest.mark.asyncio
async def test_dolphin_hook_does_not_wrap_it_as_embedded_hash_unavailable(
    tmp_path, monkeypatch,
):
    """EmbeddedHashUnavailable means "record a miss and move on"; this must not."""
    from services.tools import registry

    rvz = tmp_path / "g.rvz"
    rvz.write_bytes(b"x")
    dolphin = registry.get("dolphin")
    monkeypatch.setattr(
        dolphin._service, "disc_hashes", AsyncMock(side_effect=_abandoned()),
    )

    with pytest.raises(SubprocessAbandoned):
        await dolphin.embedded_hashes(str(rvz))


@pytest.mark.asyncio
async def test_match_single_file_propagates_rather_than_erroring_the_file(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """The per-file error result is the exact behaviour that stranded processes."""
    from services.tools import registry

    monkeypatch.setattr(isolated_dat_store, "has_dats", lambda: True)
    rvz = tmp_path / "g.rvz"
    rvz.write_bytes(b"x")
    monkeypatch.setattr(
        registry.get("dolphin")._service,
        "disc_hashes",
        AsyncMock(side_effect=_abandoned()),
    )
    # If the abandonment were swallowed, the code would fall through to hashing
    # the container -- proof the file had been treated as an ordinary failure.
    file_sha1 = AsyncMock(return_value="ab" * 20)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", file_sha1)

    with pytest.raises(SubprocessAbandoned):
        await dat_routes._match_single_file(str(rvz))
    file_sha1.assert_not_called()


# ---------------------------------------------------------------------------
# The loops that must stop walking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hash_one_for_job_does_not_isolate_it_per_path(tmp_path, monkeypatch):
    """_hash_one_for_job isolates every per-file failure -- except this one.

    Returning an error result here is what let the match job release the
    workload lane and step to the next file while the previous verifier was
    still alive.
    """
    target = tmp_path / "g.rvz"
    target.write_bytes(b"x")
    monkeypatch.setattr(
        dat_routes, "_match_single_file", AsyncMock(side_effect=_abandoned()),
    )

    with pytest.raises(SubprocessAbandoned):
        await dat_routes._hash_one_for_job(str(target))


@pytest.mark.asyncio
async def test_match_batch_stops_walking_and_reports_the_rest(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """One stuck child must not become one per remaining file."""
    from app.routes.dat import MatchBatchRequest

    monkeypatch.setattr(isolated_dat_store, "has_dats", lambda: True)
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    paths = []
    for name in ("a.rvz", "b.rvz", "c.rvz"):
        target = tmp_path / name
        target.write_bytes(b"x")
        paths.append(str(target))

    calls: list[str] = []

    async def fake_match(path, *, cancel_event=None):
        calls.append(path)
        if len(calls) == 2:
            raise _abandoned()
        return {"path": path, "matched": False}

    monkeypatch.setattr(dat_routes, "_match_single_file", fake_match)

    response = await dat_routes.match_batch(MatchBatchRequest(paths=paths))

    # The third file was never opened: it lives on the same storage and would
    # have stranded another process.
    assert calls == paths[:2]
    results = response["results"]
    assert not results[paths[0]].get("error")
    for stranded in paths[1:]:
        assert "aborted" in results[stranded]["error"]
    # Nothing about the aborted paths is cached (each result carries "error"),
    # so a retry re-hashes them rather than being served a stale negative.
    assert isolated_dat_store.get_match(paths[2]) is None


@pytest.mark.asyncio
async def test_scan_phase_dat_match_aborts_the_scan(tmp_path, monkeypatch):
    """The headline case: a metadata scan must not keep walking the library."""
    import services.dat_store as dat_store_module
    # The scan resolves its matcher through a lazy ``from routes.dat import ...``,
    # which is a different module object than the ``app.routes.dat`` the rest of
    # this file drives. Patch the one the scan actually reaches.
    import routes.dat as scanned_dat_routes

    paths = [str(tmp_path / f"{n}.rvz") for n in ("a", "b", "c")]

    job_manager = Mock()
    job_manager.is_cancelled.return_value = False
    job_manager.get_cancel_event.return_value = None
    job_manager.update_external_job = AsyncMock()
    monkeypatch.setattr(info_routes, "job_manager", job_manager)

    store = Mock()
    store.has_dats.return_value = True
    store.get_matches_batch.return_value = {}
    store.set_match = AsyncMock()
    store.delete_match = AsyncMock()
    monkeypatch.setattr(dat_store_module, "dat_store", store)

    calls: list[str] = []

    async def fake_match(path, *, cancel_event=None):
        calls.append(path)
        raise _abandoned()

    monkeypatch.setattr(scanned_dat_routes, "_match_single_file", fake_match)

    with pytest.raises(SubprocessAbandoned):
        await info_routes._scan_phase_dat_match("scan-job", paths, force=True)

    assert calls == paths[:1], "the scan must stop at the first stranded process"


@pytest.mark.asyncio
async def test_batch_verify_stops_and_flags_the_stream(tmp_path, monkeypatch):
    """Batch verify walks a file list too, so it needs the same policy."""
    from models import BulkVerifyRequest

    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(
        info_routes, "verification_store", Mock(mark_verified=AsyncMock()),
    )

    paths = []
    for name in ("a.chd", "b.chd"):
        target = tmp_path / name
        target.write_text("payload")
        paths.append(str(target))

    seen: list[str] = []

    async def fake_verify_stream(path):
        seen.append(path)
        raise _abandoned("chdman verify")
        yield  # pragma: no cover - only here to make this an async generator

    service = Mock()
    service.verify_stream = fake_verify_stream
    monkeypatch.setattr(info_routes, "chdman_service", service)

    response = await info_routes.verify_batch_events(BulkVerifyRequest(paths=paths))
    events = [e async for e in response.body_iterator if isinstance(e, dict)]

    assert seen == paths[:1], "the second file must never be opened"
    assert events[-1]["event"] == "verify_batch_complete"
    final = json.loads(events[-1]["data"])
    assert final["aborted"] is True
    assert final["skipped"] == 1
    assert "chdman verify" in final["message"]


# ---------------------------------------------------------------------------
# romz: an abandoned `7z t` is not a timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_romz_verify_does_not_report_an_abandoned_child_as_a_timeout(
    tmp_path, monkeypatch,
):
    """Reporting "timed out" would be the same lie run_capture used to tell."""
    import services.romz as romz_mod

    archive = tmp_path / "game.7z"
    archive.write_bytes(b"payload")
    svc = romz_mod.romz_service
    monkeypatch.setattr(svc, "_single_rom_member", lambda p: "game.iso")

    async def fake_capture(cmd, *, timeout=None, cancel_event=None,
                           stderr_to_stdout=False, fail_label=None):
        raise _abandoned("7z t")

    monkeypatch.setattr(svc._runner, "run_capture", fake_capture)

    with pytest.raises(SubprocessAbandoned):
        async for _update in svc.verify_stream(str(archive)):
            pass



# ---------------------------------------------------------------------------
# The verify loops signal it for real (not only via an injected mock)
# ---------------------------------------------------------------------------


class _UnkillableVerifier:
    """A verifier whose output ends but which never dies.

    Stands in for a child wedged in uninterruptible I/O: a real one cannot be
    simulated, since SIGKILL always works on a healthy process.
    """

    def __init__(self, pid: int = -7):
        self.pid = pid
        self.returncode = None
        self.signals: list[str] = []
        self.stdout = self

    async def read(self, _n: int) -> bytes:
        return b""  # immediate EOF: the read loop falls through to teardown

    def terminate(self) -> None:
        self.signals.append("TERM")

    def kill(self) -> None:
        self.signals.append("KILL")

    async def wait(self) -> int:
        await asyncio.sleep(3600)  # never returns; every caller must bound it
        return 0


@pytest.mark.parametrize(
    ("module_name", "service_attr", "suffix"),
    [
        ("services.chdman", "chdman_service", ".chd"),
        ("services.dolphin_tool", "dolphin_tool_service", ".iso"),
    ],
)
@pytest.mark.asyncio
async def test_streaming_verify_signals_abandonment(
    module_name, service_attr, suffix, tmp_path, monkeypatch,
):
    """The real chdman/dolphin verify loops raise it, so the batch policy fires.

    These loops used to finish with a bare ``await process.wait()`` and their
    own TERM/KILL ladder, so an unkillable verifier hung the stream and could
    never reach the batch route's abort branch. They now go through the shared
    bounded teardown.
    """
    import importlib

    from services import subprocess_runner as runner_module

    monkeypatch.setattr(runner_module, "_EXIT_GRACE", 0.01)
    monkeypatch.setattr(runner_module, "_TERM_GRACE", 0.01)
    monkeypatch.setattr(runner_module, "_KILL_GRACE", 0.01)

    module = importlib.import_module(module_name)
    service = getattr(module, service_attr)
    process = _UnkillableVerifier()

    async def fake_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", fake_exec)
    target = tmp_path / f"game{suffix}"
    target.write_bytes(b"x")

    with pytest.raises(SubprocessAbandoned):
        async for _update in service.verify_stream(str(target)):
            pass

    assert process.signals == ["TERM", "KILL"], "expected the shared ladder"
    # Teardown must not run the ladder a second time on a child already known
    # to be unkillable, and must still release the PID.
    assert process.pid not in service.active_pids()


class _UnkillableHang(_UnkillableVerifier):
    """Never produces output and never dies: trips the verify timeout first."""

    async def communicate(self):
        await asyncio.sleep(3600)
        return b"", b""


@pytest.mark.asyncio
async def test_capture_style_verify_does_not_swallow_abandonment(
    tmp_path, monkeypatch,
):
    """maxcso's outer ``except Exception`` must not turn it into an error dict.

    The capture-shaped verifiers (maxcso/nsz/z3ds) wrap the whole body in a
    broad handler that reports "Verification error: ..." and returns normally.
    That is the shape that let a batch keep walking; ``reraise_if_abandoned``
    is what stops it.
    """
    import services.maxcso as maxcso_module
    from services import subprocess_runner as runner_module

    monkeypatch.setattr(runner_module, "_TERM_GRACE", 0.01)
    monkeypatch.setattr(runner_module, "_KILL_GRACE", 0.01)
    monkeypatch.setattr(maxcso_module, "verify_timeout", lambda _owner=None: 0.05)

    process = _UnkillableHang(pid=-9)

    async def fake_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(maxcso_module.asyncio, "create_subprocess_exec", fake_exec)
    target = tmp_path / "game.cso"
    target.write_bytes(b"x")

    with pytest.raises(SubprocessAbandoned):
        async for _update in maxcso_module.maxcso_service.verify_stream(str(target)):
            pass

    assert process.signals == ["TERM", "KILL"], "expected the shared ladder, once"
    assert process.pid not in maxcso_module.maxcso_service.active_pids()


@pytest.mark.asyncio
async def test_scan_phase1_metadata_aborts_the_scan(tmp_path, monkeypatch):
    """Phase 1 isolates every per-file metadata failure -- except this one.

    ``chdman info`` now reports an unkillable child, and Phase 1's broad handler
    would otherwise log it as an ordinary "couldn't read this CHD" and walk on
    to the next file on the same unresponsive volume.
    """
    for name in ("a.chd", "b.chd", "c.chd"):
        (tmp_path / name).write_text("x")
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))

    calls: list[str] = []

    async def fake_info(path):
        calls.append(path)
        raise _abandoned("chdman info")

    async def fake_stale(_path):
        return True

    monkeypatch.setattr(info_routes.chdman_service, "info", fake_info)
    monkeypatch.setattr(info_routes.chd_metadata_store, "is_stale", fake_stale)

    finished: dict = {}

    async def fake_finish(job_id, *, success=True, error_message=None, **_kw):
        finished["success"] = success
        finished["error"] = error_message or ""

    monkeypatch.setattr(info_routes.job_manager, "finish_external_job", fake_finish)

    # scan_metadata_task catches at the top level and finalises the job, so the
    # abandonment surfaces as a failed scan rather than a raise.
    await info_routes.scan_metadata_task(force=True)

    assert len(calls) == 1, "the scan must stop at the first stranded process"
    assert finished.get("success") is False
    assert "could not be killed" in finished.get("error", "")


# ---------------------------------------------------------------------------
# The runner's abandonment memory must not outlive the PID
# ---------------------------------------------------------------------------


def test_abandonment_memory_is_cleared_when_the_pid_is_reused():
    """A later child on a recycled PID must still get the full ladder.

    reap() remembers the PIDs it gave up on so a repeat call is free. That memory
    is only correct while the PID still refers to that process -- once the kernel
    reuses it, a new (killable) child would otherwise be written off unreaped.
    Every spawn path registers through track_pid(), which is what clears it.
    """
    runner = SubprocessRunner(owner="test")
    doomed = _UnkillableVerifier(pid=1234)

    import services.subprocess_runner as runner_module

    async def go():
        assert await runner.reap(doomed, exit_timeout=0) is False
        # Same PID, different process: the ladder must run for real again.
        runner.track_pid(1234)
        fresh = _UnkillableVerifier(pid=1234)
        await runner.reap(fresh, exit_timeout=0)
        return fresh.signals

    original = (runner_module._TERM_GRACE, runner_module._KILL_GRACE)
    runner_module._TERM_GRACE = runner_module._KILL_GRACE = 0.01
    try:
        signals = asyncio.run(go())
    finally:
        runner_module._TERM_GRACE, runner_module._KILL_GRACE = original

    assert signals == ["TERM", "KILL"], "a reused PID must not inherit the write-off"


@pytest.mark.asyncio
async def test_info_and_header_route_through_run_capture(monkeypatch):
    """The last two hand-rolled capture spawns now use the shared one.

    That is what gives them PID tracking -- and tracking is the only thing that
    clears a stale abandonment when the kernel recycles a PID, so a spawn that
    skipped it could write off a later, killable child (see the test above).
    """
    from services.chdman import chdman_service
    from services.dolphin_tool import dolphin_tool_service

    cases = [
        (chdman_service.runner, lambda: chdman_service.info("/data/g.chd"), "info"),
        (
            dolphin_tool_service.runner,
            lambda: dolphin_tool_service.header("/data/g.iso"),
            "header",
        ),
    ]
    for runner, call, subcommand in cases:
        seen: list[list[str]] = []

        async def fake_capture(cmd, *, timeout=None, cancel_event=None,
                               stderr_to_stdout=False, fail_label=None, _seen=seen):
            _seen.append(cmd)
            return 0, b"", b""

        monkeypatch.setattr(runner, "run_capture", fake_capture)
        await call()

        assert len(seen) == 1, "the hand-rolled spawn must be gone"
        assert subcommand in seen[0]


@pytest.mark.asyncio
async def test_disc_id_helpers_propagate_abandonment(tmp_path, monkeypatch):
    """Phase 2's chdman children signal it, so the scan's guard can fire.

    These helpers convert every failure to a False/None "best effort" result --
    the shape that would hide a stranded child completely.
    """
    import services.chdman as chdman_module
    import services.disc_id as disc_id_module

    async def fake_capture(cmd, *, timeout=None, cancel_event=None,
                           stderr_to_stdout=False, fail_label=None):
        raise _abandoned(fail_label or "chdman")

    monkeypatch.setattr(
        chdman_module.chdman_service.runner, "run_capture", fake_capture,
    )
    chd = tmp_path / "game.chd"
    chd.write_bytes(b"x")

    for helper in (
        lambda: disc_id_module._addmeta_text(str(chd), "GAME", "SLUS-123", "chdman"),
        lambda: disc_id_module._delmeta(str(chd), "GAME", "chdman"),
        lambda: disc_id_module._dumpmeta_raw(str(chd), "GAME", "chdman"),
    ):
        with pytest.raises(SubprocessAbandoned):
            await helper()
