"""An abandoned hash child must stop the walk, not fail one file (issue #268).

#266's fix (PR #271) bounded and cancelled the *verify* stage and gave the
runner its abandonment sink: ``reap()`` records a child that outlived SIGKILL
into whatever ``collect_abandonment()`` block is open, and the batch-verify
route stops when its sink is non-empty.

The DAT-match and library-scan paths never opened a sink. They walk a list of
files exactly the same way, so an unkillable ``dolphin-tool verify`` in
``disc_hashes`` came back as an ordinary "no embedded hash" and the scan moved
to the next file on the same unresponsive volume -- one stranded full-disc
reconstruction per file, each invisible because its PID had been untracked.
That is the scenario #268 was filed for.

These tests lock two things: the capture paths those loops depend on really do
report into the sink, and each loop stops when it sees one.
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, Mock

import pytest

from app.routes import dat as dat_routes
from app.routes import info as info_routes

# The app is imported both as ``services.x`` (its own intra-project style, with
# ``app`` on PYTHONPATH) and as ``app.services.x``, which are two distinct module
# objects -- and therefore two distinct ContextVars. The sink only works if the
# test opens the *same* one the routes do, so resolve it from the route module
# rather than importing it by a name that merely looks right.
runner_mod = sys.modules[info_routes.collect_abandonment.__module__]


class _Unkillable:
    """A child that ignores every signal, like one wedged in D state.

    Same shape as the double in ``test_verify_bounded_cancellable_266``: a real
    process in uninterruptible I/O cannot be simulated, since SIGKILL always
    works on a healthy one. ``pid`` is this process's own so the runner's
    liveness check reads it as still alive.
    """

    returncode = None
    pid = os.getpid()

    def terminate(self):
        pass

    def kill(self):
        pass

    async def wait(self):
        await asyncio.sleep(3600)

    async def communicate(self):
        await asyncio.sleep(3600)   # never returns; the caller must bound it
        return b"", b""


def _note(detail: str = "pid 4242") -> None:
    """Report an abandonment the way ``reap()`` does, without a real child.

    The loop tests are about policy -- does this walk stop -- so they drive the
    sink directly rather than wedging a subprocess per case. That the capture
    paths genuinely feed it is covered by the plumbing tests above them.
    """
    runner_mod._note_abandoned(detail)


@pytest.fixture
def isolated_dat_store(tmp_path, monkeypatch):
    from services.dat_store import DATStore
    store = DATStore(store_path=str(tmp_path / "dat_store.json"))
    monkeypatch.setattr(dat_routes, "dat_store", store)
    monkeypatch.setattr(store, "has_dats", lambda: True)
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    return store


# ---------------------------------------------------------------------------
# Plumbing: the capture paths these loops sit on report into the sink
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_capture_reports_abandonment_into_the_sink(monkeypatch):
    """The fact every loop below keys off, with no hook passed.

    ``run_capture`` calls ``reap()`` unconditionally and ``reap()`` notes into
    the open sink, so a caller needs only the ``with`` block -- no ``on_abandoned``
    callback and no new exception type.
    """
    monkeypatch.setattr(runner_mod, "_TERM_GRACE", 0.01)
    monkeypatch.setattr(runner_mod, "_KILL_GRACE", 0.01)
    runner = runner_mod.SubprocessRunner(owner="test")

    async def fake_exec(*_args, **_kwargs):
        return _Unkillable()

    monkeypatch.setattr(runner_mod.asyncio, "create_subprocess_exec", fake_exec)

    with runner_mod.collect_abandonment() as abandoned:
        rc, _out, _err = await runner.run_capture(["/bin/true"], timeout=0.01)

    assert rc is None, "an aborted capture still reports None"
    assert abandoned, "the unkillable child has to reach the sink"


@pytest.mark.asyncio
async def test_chdman_info_uses_the_shared_capture(monkeypatch):
    """The hand-rolled spawn is gone: it ended in an unbounded wait after kill().

    Going through ``run_capture`` is what gives ``info`` a bounded teardown, PID
    tracking, the tool priority policy, and the sink report.
    """
    from services.chdman import chdman_service

    seen = []

    async def fake_capture(cmd, **kwargs):
        seen.append((cmd, kwargs.get("nice_via_wrapper")))
        return 0, b"", b""

    monkeypatch.setattr(chdman_service.runner, "run_capture", fake_capture)
    monkeypatch.setattr(chdman_service, "_parse_info", lambda _text: {"ok": True})

    assert await chdman_service.info("/data/g.chd") == {"ok": True}
    assert len(seen) == 1
    cmd, via_wrapper = seen[0]
    assert "info" in cmd
    # Never preexec_fn -- see the note in the disc_id case above.
    assert via_wrapper is True


@pytest.mark.asyncio
async def test_dolphin_header_uses_the_shared_capture(monkeypatch):
    """Same for dolphin-tool header."""
    from services.dolphin_tool import dolphin_tool_service

    seen = []

    async def fake_capture(cmd, **kwargs):
        seen.append((cmd, kwargs.get("nice_via_wrapper")))
        return 0, b"", b""

    monkeypatch.setattr(dolphin_tool_service._runner, "run_capture", fake_capture)
    monkeypatch.setattr(
        dolphin_tool_service, "_parse_header", lambda _text: {"ok": True},
    )

    assert await dolphin_tool_service.header("/data/g.iso") == {"ok": True}
    assert len(seen) == 1
    cmd, via_wrapper = seen[0]
    assert "header" in cmd
    assert via_wrapper is True


@pytest.mark.asyncio
async def test_disc_id_helpers_use_chdmans_runner(tmp_path, monkeypatch):
    """Phase 2's children were the last hand-rolled spawns, and the worst.

    ``_dumpmeta_raw`` killed its child and then waited for it with no limit,
    which could hang a library scan outright. All three now share
    ``chdman_service.runner`` -- deliberately that instance, so one PID set
    still describes every chdman child.
    """
    from services import disc_id
    from services.chdman import chdman_service

    calls = []
    wrapper_only = []

    async def fake_capture(cmd, **kwargs):
        for sub in ("addmeta", "delmeta", "dumpmeta"):
            if sub in cmd:
                calls.append(sub)
        wrapper_only.append(kwargs.get("nice_via_wrapper"))
        return 0, b"", b""

    monkeypatch.setattr(chdman_service.runner, "run_capture", fake_capture)
    chd = tmp_path / "game.chd"
    chd.write_bytes(b"x")

    await disc_id._addmeta_text(str(chd), "GAME", "SLUS-123", "chdman")
    await disc_id._delmeta(str(chd), "GAME", "chdman")
    await disc_id._dumpmeta_raw(str(chd), "GAME", "chdman")

    assert calls == ["addmeta", "delmeta", "dumpmeta"]
    # Priority as command wrappers, never run_capture's preexec_fn: forking a
    # Python callable from this multithreaded parent can deadlock the child
    # before exec, and create_subprocess_exec then never returns to apply the
    # bound at all. The spawns these replaced used no preexec_fn either.
    assert wrapper_only == [True, True, True]


# ---------------------------------------------------------------------------
# The loops that must stop walking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_match_file_reports_503_rather_than_a_cheerful_miss(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """A single file has no walk to stop, so it has to say so instead.

    The match result cannot carry the fact -- an embedded-hash miss and an
    abandoned verify are both "unmatched" -- so a 200 would hide it entirely.
    """
    from fastapi import HTTPException

    target = tmp_path / "g.rvz"
    target.write_bytes(b"x")

    async def fake_match(path, *, cancel_event=None):
        _note()
        return {"path": path, "matched": False}

    monkeypatch.setattr(dat_routes, "_match_single_file", fake_match)

    with pytest.raises(HTTPException) as excinfo:
        await dat_routes.match_file(dat_routes.MatchRequest(path=str(target)))

    assert excinfo.value.status_code == 503
    assert "still running" in excinfo.value.detail


@pytest.mark.asyncio
async def test_match_batch_stops_walking_and_flags_the_rest(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """One stuck child must not become one per remaining file."""
    paths = []
    for name in ("a.rvz", "b.rvz", "c.rvz"):
        target = tmp_path / name
        target.write_bytes(b"x")
        paths.append(str(target))

    calls = []

    async def fake_match(path, *, cancel_event=None):
        calls.append(path)
        if len(calls) == 2:
            _note()
        return {"path": path, "matched": False}

    monkeypatch.setattr(dat_routes, "_match_single_file", fake_match)

    response = await dat_routes.match_batch(dat_routes.MatchBatchRequest(paths=paths))

    assert calls == paths[:2], "the third file lives on the same storage"
    results = response["results"]
    assert not results[paths[0]].get("error")
    assert "aborted" in results[paths[2]]["error"]
    # Nothing about the aborted tail is cached, so a retry re-hashes it.
    assert isolated_dat_store.get_match(paths[2]) is None


@pytest.mark.asyncio
async def test_match_job_fails_instead_of_working_through_the_list(
    tmp_path, monkeypatch,
):
    """The match job isolates every per-file failure -- except this one.

    Returning an error result here is what let it release the workload lane and
    step to the next file while the previous verifier was still alive.
    """
    paths = []
    for name in ("a.rvz", "b.rvz", "c.rvz"):
        target = tmp_path / name
        target.write_bytes(b"x")
        paths.append(str(target))

    finished = {}

    async def fake_finish(job_id, *, success=True, error_message=None, **_kw):
        finished["success"] = success
        finished["error"] = error_message or ""

    job_manager = Mock()
    job_manager.is_cancelled.return_value = False
    job_manager.get_cancel_event.return_value = None
    job_manager.update_external_job = AsyncMock()
    job_manager.finish_external_job = fake_finish
    job_manager.finish_external_job_cancelled = AsyncMock()
    monkeypatch.setattr(dat_routes, "job_manager", job_manager)

    calls = []

    async def fake_hash_one(path, *, cancel_event=None):
        calls.append(path)
        _note()
        return {"path": path, "matched": False}, False

    monkeypatch.setattr(dat_routes, "_hash_one_for_job", fake_hash_one)

    await dat_routes._run_match_job(job_id="job-1", paths_to_compute=paths)

    assert calls == paths[:1], "the job must stop at the first stranded process"
    assert finished["success"] is False
    assert "still running" in finished["error"]


@pytest.mark.asyncio
async def test_scan_phase1_aborts_the_scan(tmp_path, monkeypatch):
    """Phase 1 isolates a corrupt CHD per-file; an unkillable info child is not that."""
    for name in ("a.chd", "b.chd", "c.chd"):
        (tmp_path / name).write_text("x")
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))

    calls = []

    async def fake_info(path):
        calls.append(path)
        _note()
        return {"raw_data": ""}

    async def fake_stale(_path):
        return True

    monkeypatch.setattr(info_routes.chdman_service, "info", fake_info)
    monkeypatch.setattr(info_routes.chd_metadata_store, "is_stale", fake_stale)
    monkeypatch.setattr(
        info_routes.chd_metadata_store, "set_metadata", AsyncMock(return_value={}),
    )

    finished = {}

    async def fake_finish(job_id, *, success=True, error_message=None, **_kw):
        finished["success"] = success
        finished["error"] = error_message or ""

    monkeypatch.setattr(info_routes.job_manager, "finish_external_job", fake_finish)

    # scan_metadata_task catches at the top level and finalises the job, so the
    # abandonment surfaces as a failed scan rather than a raise.
    await info_routes.scan_metadata_task(force=True)

    assert len(calls) == 1, "the scan must stop at the first stranded process"
    assert finished["success"] is False
    assert "still running" in finished["error"]


@pytest.mark.asyncio
async def test_scan_phase2_aborts_the_scan(tmp_path, monkeypatch):
    """Phase 2's handler only logs at debug, so this would leave no trace at all."""
    for name in ("a.chd", "b.chd", "c.chd"):
        (tmp_path / name).write_text("x")
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))

    async def fake_fresh(_path):
        return False          # Phase 1 has nothing to refresh

    async def fake_unchecked(_path):
        return False          # ...so Phase 2 runs for every file

    calls = []

    async def fake_ensure(path, chdman_path):
        calls.append(path)
        _note()

    monkeypatch.setattr(info_routes.chd_metadata_store, "is_stale", fake_fresh)
    monkeypatch.setattr(
        info_routes.chd_metadata_store, "is_disc_id_checked", fake_unchecked,
    )
    monkeypatch.setattr(
        info_routes.chd_metadata_store, "mark_disc_id_checked", AsyncMock(),
    )
    monkeypatch.setattr(info_routes, "disc_id_ensure_embedded", fake_ensure)

    finished = {}

    async def fake_finish(job_id, *, success=True, error_message=None, **_kw):
        finished["success"] = success
        finished["error"] = error_message or ""

    monkeypatch.setattr(info_routes.job_manager, "finish_external_job", fake_finish)

    await info_routes.scan_metadata_task(force=False)

    assert len(calls) == 1, "the scan must stop at the first stranded process"
    assert finished["success"] is False
    assert "Phase 2" in finished["error"]


@pytest.mark.asyncio
async def test_scan_phase3_aborts_the_scan(tmp_path, monkeypatch):
    """The DAT-match phase: the headline scenario of the issue."""
    # Phase 3 resolves its matcher through a lazy ``from routes.dat import ...``,
    # a different module object than the ``app.routes.dat`` the rest of this file
    # drives. Patch the one the scan actually reaches.
    import routes.dat as scanned_dat_routes
    import services.dat_store as dat_store_module

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

    calls = []

    async def fake_match(path, *, cancel_event=None):
        calls.append(path)
        _note()
        return {"path": path, "matched": False}

    monkeypatch.setattr(scanned_dat_routes, "_match_single_file", fake_match)

    with pytest.raises(RuntimeError, match="still running"):
        await info_routes._scan_phase_dat_match("scan-job", paths, force=True)

    assert calls == paths[:1], "the scan must stop at the first stranded process"


# ---------------------------------------------------------------------------
# Checking the sink only at the loop is too late for compound operations
# ---------------------------------------------------------------------------


def test_checkpoint_forwards_to_the_enclosing_sink():
    """A mid-flight check must not hide the fact from the caller's loop.

    ``collect_abandonment()`` shadows an enclosing sink, so a naive nested block
    would let a step abort locally while the outer walk sailed on none the wiser.
    """
    with runner_mod.collect_abandonment() as outer:
        with runner_mod.abandonment_checkpoint() as inner:
            _note("pid 1")
        assert inner == ["pid 1"], "the step sees it immediately"
    assert outer == ["pid 1"], "and the loop still sees it afterwards"


@pytest.mark.asyncio
async def test_disc_hashes_does_not_spawn_after_an_abandoned_size_probe(monkeypatch):
    """Sizing the file reads the same storage the verify would reconstruct.

    When that probe has to be abandoned the resolver falls back to the flat
    baseline, so spawning anyway buys a full verify timeout against a mount
    already known to be unresponsive -- and likely a second written-off resource.
    """
    from app.services import dolphin_tool as dolphin_mod

    spawned = []

    async def fake_resolve(path, owner, *, cancel_event=None):
        _note("detached read")          # what run_detached records
        return 1800

    async def fake_capture(cmd, **kwargs):
        spawned.append(cmd)
        return 0, b"", b""

    monkeypatch.setattr(dolphin_mod, "resolve_verify_timeout", fake_resolve)
    monkeypatch.setattr(
        dolphin_mod.dolphin_tool_service._runner, "run_capture", fake_capture,
    )

    with runner_mod.collect_abandonment() as abandoned:
        result = await dolphin_mod.dolphin_tool_service.disc_hashes("/data/g.rvz")

    assert result == []
    assert not spawned, "must not spawn the verifier into dead storage"
    assert abandoned, "and the caller's loop still learns about it"


@pytest.mark.asyncio
async def test_reading_a_tag_that_was_abandoned_is_not_reported_as_untagged(
    tmp_path, monkeypatch,
):
    """The conflation that let post_convert write to a CHD still being read.

    ``read_embedded_game_id`` returning None means "no GAME tag", and
    ``post_convert`` answers that by firing ``addmeta`` at the file. An abandoned
    read must therefore not come back as None.
    """
    from services import disc_id

    async def fake_dumpmeta(chd_path, tag, chdman_path):
        _note("pid 99")

    monkeypatch.setattr(disc_id, "_dumpmeta_text", fake_dumpmeta)
    chd = tmp_path / "game.chd"
    chd.write_bytes(b"x")

    with pytest.raises(disc_id.DiscIdStorageAbandoned):
        await disc_id.read_embedded_game_id(str(chd), "chdman")

    # ensure_disc_id_embedded guards the same read, before the strategies that
    # write a tag or fall through to the unbounded sector read.
    with pytest.raises(disc_id.DiscIdStorageAbandoned):
        await disc_id.ensure_disc_id_embedded(str(chd), "chdman")


@pytest.mark.asyncio
async def test_post_convert_skips_the_write_and_says_so(tmp_path, monkeypatch):
    """Best-effort still holds -- the job must not fail -- but not silently.

    The harm was `addmeta` landing on a CHD an abandoned reader still holds; the
    secondary harm was reporting that at debug, where nobody would find it.
    """
    from services import disc_id
    from services.tools import registry

    chd = tmp_path / "game.chd"
    chd.write_bytes(b"x")
    chdman = registry.get("chdman")
    # The plugin the registry holds lives in `services.tools.chdman`, which is a
    # different module object than `app.services.tools.chdman`; patching the
    # latter would make every assertion below pass vacuously.
    plugin_mod = sys.modules[type(chdman).__module__]

    monkeypatch.setattr(
        disc_id, "_dumpmeta_text", AsyncMock(side_effect=lambda *a, **k: _note("pid 7")),
    )
    wrote = []
    monkeypatch.setattr(
        plugin_mod, "embed_in_chd",
        AsyncMock(side_effect=lambda *a, **k: wrote.append(a)),
    )
    monkeypatch.setattr(
        plugin_mod, "extract_from_source", lambda _p: {"game_id": "SLUS-123"},
    )

    # The project logger does not propagate to caplog, so watch it directly.
    errors: list[str] = []
    monkeypatch.setattr(
        plugin_mod.logger, "error",
        lambda msg, *args, **kw: errors.append(str(msg) % args if args else str(msg)),
    )

    # Never raises: tagging is best-effort and must not fail the job.
    await chdman.post_convert(str(chd), str(chd), "createcd")

    assert not wrote, "must not write to a CHD an abandoned reader still holds"
    assert errors and "still running" in errors[0], (
        "the skip must be visible, not debug-only"
    )
