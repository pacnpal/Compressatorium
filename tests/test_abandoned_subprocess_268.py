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

