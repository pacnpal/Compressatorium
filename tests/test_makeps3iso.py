"""Tests for the makeps3iso folder->iso directory-input tool (issue #98 Phase 2).

Covers the end-to-end seam: detector->tool resolution, the file-listing /
search directory annotation, the service convert (mocked subprocess), and the
``plan_job`` directory branch (valid / invalid / trailing slash).
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models import ConversionJob, ConversionMode, InputKind, JobStatus
from app.routes import convert as convert_routes
from app.routes import files as files_routes
from app.services.makeps3iso import ConversionCancelled, makeps3iso_service
from app.services.tools import registry

from .ps3_helpers import make_ps3_folder as _make_ps3_folder


# --------------------------------------------------------------------------- #
# Detector -> tool resolution
# --------------------------------------------------------------------------- #


def test_tools_for_directory_resolves_makeps3iso(tmp_path):
    folder = _make_ps3_folder(tmp_path / "MyGame")
    assert [t.id for t in registry.tools_for_directory(str(folder))] == ["makeps3iso"]
    plain = tmp_path / "plain"
    plain.mkdir()
    assert registry.tools_for_directory(str(plain)) == []


def test_folder_to_iso_spec_is_directory_kind():
    spec = registry.spec("folder_to_iso")
    assert spec.tool_id == "makeps3iso"
    assert InputKind.DIRECTORY in spec.input_kinds
    assert spec.output_ext == ".iso"
    assert spec.supports_delete_on_verify is False


# --------------------------------------------------------------------------- #
# File-listing / search annotation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_files_annotates_convertible_directory(tmp_path, monkeypatch):
    _make_ps3_folder(tmp_path / "MyGame")
    (tmp_path / "plain").mkdir()
    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(tmp_path))

    listing = await files_routes.list_files(path=str(tmp_path))
    by_name = {e.name: e for e in listing.entries}

    game = by_name["MyGame"]
    assert game.type == "directory"
    assert game.convertible_by == ["makeps3iso"]
    # No sibling .iso yet, so no detected output.
    assert game.outputs == []

    plain = by_name["plain"]
    assert plain.type == "directory"
    assert plain.convertible_by == []


@pytest.mark.asyncio
async def test_list_files_directory_output_badge(tmp_path, monkeypatch):
    _make_ps3_folder(tmp_path / "MyGame")
    (tmp_path / "MyGame.iso").write_bytes(b"iso")  # sibling output already present
    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(tmp_path))

    listing = await files_routes.list_files(path=str(tmp_path))
    game = next(e for e in listing.entries if e.name == "MyGame")
    assert [o.tool_id for o in game.outputs] == ["makeps3iso"]
    assert game.outputs[0].exists is True
    assert game.outputs[0].path == str(tmp_path / "MyGame.iso")


@pytest.mark.asyncio
async def test_search_emits_convertible_directory(tmp_path, monkeypatch):
    # A PS3 folder nested in a subtree must surface as a selectable directory
    # row in recursive search (and not be recursed past as a job unit).
    sub = tmp_path / "sub"
    sub.mkdir()
    _make_ps3_folder(sub / "MyGame")
    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(tmp_path))

    result = await files_routes.search_files(path=str(tmp_path))
    dir_rows = [f for f in result["files"] if f.get("type") == "directory"]
    assert len(dir_rows) == 1
    row = dir_rows[0]
    assert row["name"] == "MyGame"
    assert row["convertible_by"] == ["makeps3iso"]
    # The inner PS3_GAME files were NOT emitted as separate convertible hits.
    assert all("PARAM.SFO" not in f["name"] for f in result["files"])


# --------------------------------------------------------------------------- #
# plan_job directory branch
# --------------------------------------------------------------------------- #


def _confine_to_volume(monkeypatch, root):
    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(root))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(root))


@pytest.mark.asyncio
async def test_plan_job_accepts_valid_ps3_dir(tmp_path, monkeypatch):
    _confine_to_volume(monkeypatch, tmp_path)
    folder = _make_ps3_folder(tmp_path / "MyGame")
    plan = await convert_routes.plan_job(
        str(folder),
        spec=registry.spec("folder_to_iso"),
        mode="folder_to_iso",
        output_dir=None,
        duplicate_action=convert_routes.DuplicateAction.SKIP,
        delete_on_verify=False,
    )
    assert plan.output_path == str(tmp_path / "MyGame.iso")
    assert plan.display_filename == "MyGame"
    assert plan.allow_overwrite is False


@pytest.mark.asyncio
async def test_plan_job_rejects_output_outside_volumes(tmp_path, monkeypatch):
    # The PS3 folder is itself the volume root, so the default sibling output
    # ("<root>.iso") lands outside all configured volumes -> rejected.
    vol = tmp_path / "vol"
    _make_ps3_folder(vol)
    _confine_to_volume(monkeypatch, vol)
    with pytest.raises(convert_routes.SkipFile) as exc:
        await convert_routes.plan_job(
            str(vol),
            spec=registry.spec("folder_to_iso"),
            mode="folder_to_iso",
            output_dir=None,
            duplicate_action=convert_routes.DuplicateAction.SKIP,
            delete_on_verify=False,
        )
    assert exc.value.reason is convert_routes.SkipReason.PS3_OUTPUT_OUTSIDE_VOLUMES


@pytest.mark.asyncio
async def test_plan_job_rejects_ps3_dir_with_symlinked_file(tmp_path, monkeypatch):
    _confine_to_volume(monkeypatch, tmp_path / "volume")
    volume = tmp_path / "volume"
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    folder = _make_ps3_folder(volume / "MyGame")
    (folder / "PS3_GAME" / "LEAK.TXT").symlink_to(outside)

    with pytest.raises(convert_routes.SkipFile) as exc:
        await convert_routes.plan_job(
            str(folder),
            spec=registry.spec("folder_to_iso"),
            mode="folder_to_iso",
            output_dir=None,
            duplicate_action=convert_routes.DuplicateAction.SKIP,
            delete_on_verify=False,
        )
    assert exc.value.reason is convert_routes.SkipReason.PS3_FOLDER_UNSAFE


@pytest.mark.asyncio
async def test_plan_job_rejects_symlinked_ps3_root_with_trailing_slash(tmp_path, monkeypatch):
    # A symlinked source root must be rejected even with a trailing separator: a
    # trailing slash makes os.path.islink/os.lstat follow the link, so
    # is_safe_directory_tree must lstat the trailing-slash-stripped path to keep
    # a request like "/volume/LinkGame/" from handing makeps3iso a symlinked root.
    _confine_to_volume(monkeypatch, tmp_path / "volume")
    volume = tmp_path / "volume"
    real_folder = _make_ps3_folder(volume / "MyGame")
    link_root = volume / "LinkGame"
    link_root.symlink_to(real_folder, target_is_directory=True)

    with pytest.raises(convert_routes.SkipFile) as exc:
        await convert_routes.plan_job(
            str(link_root) + os.sep,
            spec=registry.spec("folder_to_iso"),
            mode="folder_to_iso",
            output_dir=None,
            duplicate_action=convert_routes.DuplicateAction.SKIP,
            delete_on_verify=False,
        )
    assert exc.value.reason is convert_routes.SkipReason.PS3_FOLDER_UNSAFE


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [os.sep + ".", os.sep + "." + os.sep])
async def test_plan_job_rejects_symlinked_ps3_root_with_dot_component(
    tmp_path, monkeypatch, suffix,
):
    # A symlinked source root must be rejected even when hidden behind a trailing
    # "." or "./" component: those make os.lstat follow the link, so
    # is_safe_directory_tree must normalize them away before the pre-resolve lstat
    # to keep "/vol/link/." from handing makeps3iso a symlinked root.
    _confine_to_volume(monkeypatch, tmp_path / "volume")
    volume = tmp_path / "volume"
    real_folder = _make_ps3_folder(volume / "MyGame")
    link_root = volume / "LinkGame"
    link_root.symlink_to(real_folder, target_is_directory=True)

    with pytest.raises(convert_routes.SkipFile) as exc:
        await convert_routes.plan_job(
            str(link_root) + suffix,
            spec=registry.spec("folder_to_iso"),
            mode="folder_to_iso",
            output_dir=None,
            duplicate_action=convert_routes.DuplicateAction.SKIP,
            delete_on_verify=False,
        )
    assert exc.value.reason is convert_routes.SkipReason.PS3_FOLDER_UNSAFE


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("/vol/link", "/vol/link"),
        ("/vol/link/", "/vol/link"),
        ("/vol/link/.", "/vol/link"),
        ("/vol/link/./", "/vol/link"),
        ("/vol/link///", "/vol/link"),
        ("/", "/"),
        (".", "."),
        # Interior ".." and symlinked ancestors must survive untouched so lstat
        # resolves them the way the kernel does.
        ("/vol/anchor/../link", "/vol/anchor/../link"),
        ("/vol/game/subdir/..", "/vol/game/subdir/.."),
    ],
)
def test_strip_trailing_dot_and_seps(raw, expected):
    from app.utils.path_utils import _strip_trailing_dot_and_seps

    assert _strip_trailing_dot_and_seps(raw) == expected


@pytest.mark.asyncio
async def test_plan_job_rejects_symlinked_root_behind_dotdot_cancelled_ancestor(
    tmp_path, monkeypatch,
):
    # A symlinked root reached through a ".."-cancelled symlinked ancestor must
    # still be rejected. Submitting "<volume>/anchor/../LinkGame" (anchor is a
    # symlink) lexically normalizes to "<volume>/LinkGame" — a benign decoy dir —
    # but the kernel resolves ".." *after* following anchor, so the real final
    # component is a symlink. The pre-resolve lstat must probe the un-normalized
    # path so S_ISLNK catches it, rather than lstat'ing the lexical decoy.
    _confine_to_volume(monkeypatch, tmp_path / "volume")
    volume = tmp_path / "volume"
    real_folder = _make_ps3_folder(volume / "RealGame")
    outside = tmp_path / "outside"
    (outside / "x").mkdir(parents=True)
    (volume / "anchor").symlink_to(outside / "x", target_is_directory=True)
    (outside / "LinkGame").symlink_to(real_folder, target_is_directory=True)
    # Decoy: the path that lexical normalization ("anchor/.." cancels) would hit,
    # existing as a plain directory so an lstat on it would wrongly pass.
    (volume / "LinkGame").mkdir()

    submitted = str(volume / "anchor") + os.sep + ".." + os.sep + "LinkGame"
    with pytest.raises(convert_routes.SkipFile) as exc:
        await convert_routes.plan_job(
            submitted,
            spec=registry.spec("folder_to_iso"),
            mode="folder_to_iso",
            output_dir=None,
            duplicate_action=convert_routes.DuplicateAction.SKIP,
            delete_on_verify=False,
        )
    assert exc.value.reason is convert_routes.SkipReason.PS3_FOLDER_UNSAFE


@pytest.mark.asyncio
async def test_plan_job_rejects_ps3_dir_with_symlinked_directory(tmp_path, monkeypatch):
    _confine_to_volume(monkeypatch, tmp_path / "volume")
    volume = tmp_path / "volume"
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("secret", encoding="utf-8")
    folder = _make_ps3_folder(volume / "MyGame")
    (folder / "PS3_GAME" / "EXTRA").symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(convert_routes.SkipFile) as exc:
        await convert_routes.plan_job(
            str(folder),
            spec=registry.spec("folder_to_iso"),
            mode="folder_to_iso",
            output_dir=None,
            duplicate_action=convert_routes.DuplicateAction.SKIP,
            delete_on_verify=False,
        )
    assert exc.value.reason is convert_routes.SkipReason.PS3_FOLDER_UNSAFE


@pytest.mark.asyncio
async def test_plan_job_rejects_non_ps3_dir(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(convert_routes.SkipFile) as exc:
        await convert_routes.plan_job(
            str(plain),
            spec=registry.spec("folder_to_iso"),
            mode="folder_to_iso",
            output_dir=None,
            duplicate_action=convert_routes.DuplicateAction.SKIP,
            delete_on_verify=False,
        )
    assert exc.value.reason is convert_routes.SkipReason.PS3_FOLDER_INVALID


@pytest.mark.asyncio
async def test_plan_job_rejects_output_inside_source(tmp_path, monkeypatch):
    # An output_dir inside the source folder would make makeps3iso pack its own
    # in-progress ISO and corrupt the image — must be rejected. Confine the
    # source to a configured volume so the (safe) tree passes is_safe_directory_tree
    # and the output-inside-source guard is the one that fires.
    _confine_to_volume(monkeypatch, tmp_path)
    folder = _make_ps3_folder(tmp_path / "MyGame")
    with pytest.raises(convert_routes.SkipFile) as exc:
        await convert_routes.plan_job(
            str(folder),
            spec=registry.spec("folder_to_iso"),
            mode="folder_to_iso",
            output_dir=str(folder / "PS3_GAME"),
            duplicate_action=convert_routes.DuplicateAction.SKIP,
            delete_on_verify=False,
        )
    assert exc.value.reason is convert_routes.SkipReason.PS3_OUTPUT_INSIDE_SOURCE


@pytest.mark.asyncio
async def test_plan_job_skip_conflict_short_circuits_before_safety_walk(
    tmp_path, monkeypatch,
):
    # A SKIP request whose output already exists must short-circuit with
    # OUTPUT_EXISTS *without* running the recursive PS3 safety walk — otherwise a
    # batch skip over a large already-converted library stats every tree for
    # nothing. The worker still revalidates the tree for jobs that actually run.
    _confine_to_volume(monkeypatch, tmp_path)
    folder = _make_ps3_folder(tmp_path / "MyGame")
    (tmp_path / "MyGame.iso").write_bytes(b"iso")  # existing sibling output

    walked = False

    def _tracking_walk(path):
        nonlocal walked
        walked = True
        return True

    monkeypatch.setattr(convert_routes, "is_safe_directory_tree", _tracking_walk)

    with pytest.raises(convert_routes.SkipFile) as exc:
        await convert_routes.plan_job(
            str(folder),
            spec=registry.spec("folder_to_iso"),
            mode="folder_to_iso",
            output_dir=None,
            duplicate_action=convert_routes.DuplicateAction.SKIP,
            delete_on_verify=False,
        )
    assert exc.value.reason is convert_routes.SkipReason.OUTPUT_EXISTS
    assert walked is False


@pytest.mark.asyncio
async def test_plan_job_handles_trailing_slash(tmp_path, monkeypatch):
    _confine_to_volume(monkeypatch, tmp_path)
    _make_ps3_folder(tmp_path / "MyGame")
    plan = await convert_routes.plan_job(
        str(tmp_path / "MyGame") + "/",  # trailing slash
        spec=registry.spec("folder_to_iso"),
        mode="folder_to_iso",
        output_dir=None,
        duplicate_action=convert_routes.DuplicateAction.SKIP,
        delete_on_verify=False,
    )
    # Normalized basename, not "" -> the output is "<folder>.iso", never ".iso".
    assert plan.output_path == str(tmp_path / "MyGame.iso")
    assert plan.display_filename == "MyGame"


# --------------------------------------------------------------------------- #
# Service convert (mocked subprocess)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_service_convert_argv_and_progress(tmp_path, monkeypatch):
    folder = str(_make_ps3_folder(tmp_path / "MyGame"))
    out_iso = str(tmp_path / "MyGame.iso")
    captured: dict = {}

    async def fake_run(cmd, *, output_path, complete_message, **_kwargs):
        captured["cmd"] = cmd
        Path(output_path).write_bytes(b"not a real iso")  # simulated build
        yield {"progress": 1, "message": "Starting"}
        yield {"progress": 50, "message": "Writing... 50%"}
        yield {"progress": 100, "message": complete_message}

    monkeypatch.setattr(makeps3iso_service._runner, "run", fake_run)

    updates = [
        u
        async for u in makeps3iso_service.convert(folder, out_iso, "folder_to_iso")
    ]

    # argv is exactly `makeps3iso <folder> <out.iso>` (ignoring any ionice wrap).
    assert captured["cmd"][-3:] == [
        makeps3iso_service.makeps3iso_path, folder, out_iso,
    ]
    # Progress drains to 100, and the final update is the readback message the
    # service appends (replacing the runner's bare 100%).
    assert updates[-1]["progress"] == 100
    assert "ISO build complete" in updates[-1]["message"]
    assert Path(out_iso).exists()


@pytest.mark.asyncio
async def test_service_convert_cancel_cleans_partial(tmp_path, monkeypatch):
    folder = str(_make_ps3_folder(tmp_path / "MyGame"))
    out_iso = str(tmp_path / "MyGame.iso")

    async def fake_run(cmd, *, output_path, **_kwargs):
        Path(output_path).write_bytes(b"partial")  # half-written ISO
        yield {"progress": 10, "message": "Writing..."}
        raise ConversionCancelled("cancelled")

    monkeypatch.setattr(makeps3iso_service._runner, "run", fake_run)

    async def _drain():
        return [
            u
            async for u in makeps3iso_service.convert(folder, out_iso, "folder_to_iso")
        ]

    with pytest.raises(ConversionCancelled):
        await _drain()
    # The partial ISO is removed so a retry isn't blocked by a truncated image.
    assert not Path(out_iso).exists()


@pytest.mark.asyncio
async def test_service_convert_cleans_partial_on_task_cancellation(tmp_path, monkeypatch):
    # Task cancellation / generator close raise BaseException-derived
    # CancelledError / GeneratorExit, which an `except Exception` would miss —
    # the partial ISO must still be cleaned up.
    folder = str(_make_ps3_folder(tmp_path / "MyGame"))
    out_iso = str(tmp_path / "MyGame.iso")

    async def fake_run(cmd, *, output_path, **_kwargs):
        Path(output_path).write_bytes(b"partial")
        yield {"progress": 10, "message": "Writing..."}
        raise asyncio.CancelledError

    monkeypatch.setattr(makeps3iso_service._runner, "run", fake_run)

    async def _drain():
        return [
            u
            async for u in makeps3iso_service.convert(folder, out_iso, "folder_to_iso")
        ]

    with pytest.raises(asyncio.CancelledError):
        await _drain()
    assert not Path(out_iso).exists()


# --------------------------------------------------------------------------- #
# Lock-manager subtree protection
# --------------------------------------------------------------------------- #


def test_directory_job_protects_subtree(tmp_path):
    from app.services.job_manager import job_manager

    folder = _make_ps3_folder(tmp_path / "MyGame")
    job = ConversionJob(
        id="ps3test1",
        file_path=str(folder),
        filename="MyGame",
        mode=ConversionMode.FOLDER_TO_ISO,
        status=JobStatus.PROCESSING,
        created_at=datetime.now(timezone.utc),
        output_path=str(tmp_path / "MyGame.iso"),
        input_kind=InputKind.DIRECTORY,
    )
    job_manager.jobs[job.id] = job
    try:
        # A path INSIDE the active folder is treated as in use (its whole
        # subtree is being packed), even though it hashes to a different key.
        inside = str(folder / "PS3_GAME" / "PARAM.SFO")
        assert job_manager.find_active_job_for_path(inside) is job
        assert job_manager._is_path_in_use_by_other_job("other", inside) is True
        # A sibling outside the folder is unaffected.
        outside = str(tmp_path / "Unrelated.iso")
        assert job_manager.find_active_job_for_path(outside) is None
    finally:
        job_manager.jobs.pop(job.id, None)


def test_dir_lock_serializes_subtree(tmp_path):
    # A directory subtree lock makes any path inside the folder contend on the
    # lock exactly like an output collision, so a concurrent per-file job can't
    # write into the tree while makeps3iso packs it.
    from app.services.lock_manager import lock_manager

    folder = str(_make_ps3_folder(tmp_path / "MyGame"))
    inside = str(tmp_path / "MyGame" / "PS3_GAME" / "extra.chd")
    sibling = str(tmp_path / "MyGame.iso")

    assert lock_manager.acquire_dir_lock(folder) is True
    try:
        # The whole subtree reads as locked; acquiring a per-file lock inside
        # the folder is refused.
        _exists, is_locked = lock_manager.check_file_status(inside)
        assert is_locked is True
        assert lock_manager.acquire_lock(inside) is False
        # A sibling output (the job's own .iso) is outside the subtree, so it
        # locks normally.
        assert lock_manager.acquire_lock(sibling) is True
        lock_manager.release_lock(sibling)
        # A second packing of the same folder is refused while it's locked.
        assert lock_manager.acquire_dir_lock(folder) is False
    finally:
        lock_manager.release_dir_lock(folder)

    # Once released, the subtree is free again.
    assert lock_manager.acquire_lock(inside) is True
    lock_manager.release_lock(inside)


@pytest.mark.asyncio
async def test_blocked_job_requeues_instead_of_failing(tmp_path):
    # A job whose output is inside a folder being packed waits in the queue and
    # is re-dispatched, rather than being failed. job_manager resolves its
    # lock/concurrency managers via absolute ``services.*`` imports, so import
    # those from the same module graph (the ``app.services.*`` aliases are
    # distinct singletons under PYTHONPATH=app).
    from app.services.job_manager import job_manager
    from services.concurrency_manager import concurrency_manager
    from services.lock_manager import lock_manager

    folder = str(_make_ps3_folder(tmp_path / "MyGame"))
    out_inside = str(tmp_path / "MyGame" / "PS3_GAME" / "extra.chd")
    job = ConversionJob(
        id="ps3wait1",
        file_path=str(tmp_path / "src.cue"),
        filename="src.cue",
        mode=ConversionMode.CREATECD,
        status=JobStatus.QUEUED,
        created_at=datetime.now(timezone.utc),
        output_path=out_inside,
        input_kind=InputKind.FILE,
    )
    job_manager.jobs[job.id] = job
    assert lock_manager.acquire_dir_lock(folder) is True
    try:
        # The output lives inside the packed folder -> a transient block: wait.
        assert job_manager._blocked_by_dir_lock(job) is True
        # Re-dispatch (no delay) re-queues the job; it is never failed.
        job_manager._schedule_dir_lock_requeue(job.id, delay=0)
        await asyncio.sleep(0.05)
        assert job.status == JobStatus.QUEUED
        drained, found = [], False
        while not job_manager._queue.empty():
            item = job_manager._queue.get_nowait()
            if item[1] == job.id:
                found = True
            else:
                drained.append(item)
        for item in drained:
            job_manager._queue.put_nowait(item)
        assert found
    finally:
        lock_manager.release_dir_lock(folder)
        concurrency_manager.release_ticket(job.id)
        job_manager.jobs.pop(job.id, None)
        job_manager._cancelled.discard(job.id)
    # With the folder lock gone, the job is no longer blocked.
    assert job_manager._blocked_by_dir_lock(job) is False


# --------------------------------------------------------------------------- #
# Split (-s, 4 GB FAT32) — argv, dynamic part detection, readback, cleanup
# --------------------------------------------------------------------------- #


def test_split_parts_single_vs_multipart(tmp_path):
    base = str(tmp_path / "Game.iso")
    # A sub-4 GB build (split or not) leaves the plain file -> [base].
    Path(base).write_bytes(b"single")
    assert makeps3iso_service.split_parts(base) == [base]

    # A >4 GB split leaves only numbered parts (no plain base); returned in
    # numeric order — a double-digit part proves it's numeric, not lexical
    # (lexically ".10" would sort before ".2"). makeps3iso writes a contiguous
    # run from .0, so the set here is contiguous 0..10.
    Path(base).unlink()
    for n in range(11):
        Path(f"{base}.{n}").write_bytes(b"x")
    assert makeps3iso_service.split_parts(base) == [
        f"{base}.{n}" for n in range(11)
    ]


def test_split_parts_ignores_noncontiguous_siblings(tmp_path):
    """An unrelated numbered sibling is never treated as part of the split set.

    ``split_parts``/``remove_outputs`` only enumerate the contiguous run from
    ``.0``; a ``Game.iso.2024`` backup with no ``.0`` (or one beyond a gap) must
    be left alone so authorized-overwrite cleanup can't silently delete it.
    """
    base = str(tmp_path / "Game.iso")

    # No .0 at all -> the lone numbered sibling is not a split part.
    stray = Path(f"{base}.2024")
    stray.write_bytes(b"backup")
    assert makeps3iso_service.split_parts(base) == []
    makeps3iso_service.remove_outputs(base)
    assert stray.exists()

    # A gap stops enumeration: .0/.1 belong to the set, .2024 sits past the gap.
    for n in (0, 1):
        Path(f"{base}.{n}").write_bytes(b"x")
    assert makeps3iso_service.split_parts(base) == [f"{base}.0", f"{base}.1"]
    makeps3iso_service.remove_outputs(base)
    assert stray.exists()
    assert not Path(f"{base}.0").exists()
    assert not Path(f"{base}.1").exists()


@pytest.mark.asyncio
async def test_service_convert_split_argv_places_flag_first(tmp_path, monkeypatch):
    folder = str(_make_ps3_folder(tmp_path / "MyGame"))
    out_iso = str(tmp_path / "MyGame.iso")

    captured: dict = {}

    async def fake_run(cmd, *, output_path, complete_message, **_kwargs):
        captured["cmd"] = cmd
        Path(output_path).write_bytes(b"small iso")  # sub-4GB: single file
        yield {"progress": 100, "message": complete_message}

    monkeypatch.setattr(makeps3iso_service._runner, "run", fake_run)

    updates = [
        u
        async for u in makeps3iso_service.convert(
            folder, out_iso, "folder_to_iso", split=True,
        )
    ]
    # `-s` goes before the folder: `makeps3iso -s <folder> <out.iso>`.
    assert captured["cmd"][-4:] == [
        makeps3iso_service.makeps3iso_path, "-s", folder, out_iso,
    ]
    # A single-file (sub-4GB) result carries no split suffix.
    assert "split into" not in updates[-1]["message"]


@pytest.mark.asyncio
async def test_service_convert_split_multipart_message_and_readback(
    tmp_path, monkeypatch,
):
    from app.services import makeps3iso as mk_module

    folder = str(_make_ps3_folder(tmp_path / "MyGame"))
    out_iso = str(tmp_path / "MyGame.iso")

    async def fake_run(cmd, *, output_path, complete_message, **_kwargs):
        # Simulate a >4GB split: two numbered parts, no plain base file.
        Path(f"{output_path}.0").write_bytes(b"part0")
        Path(f"{output_path}.1").write_bytes(b"part1")
        yield {"progress": 100, "message": complete_message}

    monkeypatch.setattr(makeps3iso_service._runner, "run", fake_run)

    readback_paths: list[str] = []
    monkeypatch.setattr(mk_module.ps3, "ps3_title_id", lambda _f: "BLES00000")

    def _iso_id(path):
        readback_paths.append(path)
        return "BLES00000"

    monkeypatch.setattr(mk_module.ps3, "ps3_iso_title_id", _iso_id)

    updates = [
        u
        async for u in makeps3iso_service.convert(
            folder, out_iso, "folder_to_iso", split=True,
        )
    ]
    # Readback targets the .0 part (it holds the PVD / PARAM.SFO).
    assert readback_paths == [f"{out_iso}.0"]
    # The final message reports the verified id AND the part count.
    assert "split into 2 parts" in updates[-1]["message"]
    assert "BLES00000" in updates[-1]["message"]


@pytest.mark.asyncio
async def test_service_convert_split_cleanup_removes_all_parts(tmp_path, monkeypatch):
    folder = str(_make_ps3_folder(tmp_path / "MyGame"))
    out_iso = str(tmp_path / "MyGame.iso")

    async def fake_run(cmd, *, output_path, **_kwargs):
        Path(f"{output_path}.0").write_bytes(b"part0")
        Path(f"{output_path}.1").write_bytes(b"part1")
        yield {"progress": 40, "message": "Writing..."}
        raise ConversionCancelled("cancelled")

    monkeypatch.setattr(makeps3iso_service._runner, "run", fake_run)

    with pytest.raises(ConversionCancelled):
        _ = [
            u
            async for u in makeps3iso_service.convert(
                folder, out_iso, "folder_to_iso", split=True,
            )
        ]
    # Every split part is removed, not just the (never-written) base path.
    assert not Path(f"{out_iso}.0").exists()
    assert not Path(f"{out_iso}.1").exists()


# --------------------------------------------------------------------------- #
# plan_job: a prior split set counts as an output collision
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_plan_job_rejects_existing_split_set(tmp_path, monkeypatch):
    _confine_to_volume(monkeypatch, tmp_path)
    _make_ps3_folder(tmp_path / "MyGame")
    # A prior split build left only the .0 part (no plain MyGame.iso).
    (tmp_path / "MyGame.iso.0").write_bytes(b"part0")
    with pytest.raises(convert_routes.SkipFile) as exc:
        await convert_routes.plan_job(
            str(tmp_path / "MyGame"),
            spec=registry.spec("folder_to_iso"),
            mode="folder_to_iso",
            output_dir=None,
            duplicate_action=convert_routes.DuplicateAction.SKIP,
            delete_on_verify=False,
        )
    assert exc.value.reason is convert_routes.SkipReason.OUTPUT_EXISTS


@pytest.mark.asyncio
async def test_plan_job_rename_steps_past_split_set(tmp_path, monkeypatch):
    _confine_to_volume(monkeypatch, tmp_path)
    _make_ps3_folder(tmp_path / "MyGame")
    (tmp_path / "MyGame.iso.0").write_bytes(b"part0")
    plan = await convert_routes.plan_job(
        str(tmp_path / "MyGame"),
        spec=registry.spec("folder_to_iso"),
        mode="folder_to_iso",
        output_dir=None,
        duplicate_action=convert_routes.DuplicateAction.RENAME,
        delete_on_verify=False,
    )
    # RENAME bumps past the occupied split set to a free name.
    assert plan.output_path == str(tmp_path / "MyGame_1.iso")


# --------------------------------------------------------------------------- #
# File listing: fold a split ISO set into one logical entry
# --------------------------------------------------------------------------- #


def test_fold_split_iso_entries():
    from app.models import FileEntry

    entries = [
        FileEntry(name="A.iso.0", path="/d/A.iso.0", type="file",
                  size=4_000_000_000, extension=".0"),
        FileEntry(name="A.iso.1", path="/d/A.iso.1", type="file",
                  size=2_000_000_000, extension=".1"),
        FileEntry(name="B.chd", path="/d/B.chd", type="file", size=10,
                  extension=".chd", convertible_by=["chdman"]),
        FileEntry(name="orphan.iso.1", path="/d/orphan.iso.1", type="file",
                  size=5, extension=".1"),
        FileEntry(name="sub", path="/d/sub", type="directory"),
    ]
    folded = files_routes._fold_split_iso_entries(entries)
    names = [e.name for e in folded]

    # The A.iso set collapses into one entry; siblings dropped.
    assert "A.iso" in names
    assert "A.iso.0" not in names and "A.iso.1" not in names
    # An orphan .1 (no .0) and unrelated rows pass through untouched.
    assert "orphan.iso.1" in names
    assert "B.chd" in names and "sub" in names

    folded_iso = next(e for e in folded if e.name == "A.iso")
    assert folded_iso.path == "/d/A.iso.0"          # .0 is the mount target
    assert folded_iso.size == 6_000_000_000          # summed across parts
    assert folded_iso.extension == ".iso"
    assert folded_iso.split_parts == 2
    assert folded_iso.convertible_by == []           # a deliverable, not a source
    assert folded_iso.verifiable_by == []


def test_fold_split_iso_entries_noop_without_parts():
    from app.models import FileEntry

    entries = [
        FileEntry(name="Game.iso", path="/d/Game.iso", type="file", size=1,
                  extension=".iso"),
        FileEntry(name="x.chd", path="/d/x.chd", type="file", size=1,
                  extension=".chd"),
    ]
    # No .0 part present -> list returned unchanged (a plain .iso never matches).
    assert files_routes._fold_split_iso_entries(entries) == entries


def test_fold_split_iso_entries_requires_contiguous_parts():
    from app.models import FileEntry

    # A gap (.0 + .2, no .1) is an incomplete/corrupt set — don't fold it into a
    # single "valid" row; leave the raw parts visible so the gap is obvious.
    entries = [
        FileEntry(name="Game.iso.0", path="/d/Game.iso.0", type="file", size=1,
                  extension=".0"),
        FileEntry(name="Game.iso.2", path="/d/Game.iso.2", type="file", size=1,
                  extension=".2"),
    ]
    folded = files_routes._fold_split_iso_entries(entries)
    names = [e.name for e in folded]
    assert names == ["Game.iso.0", "Game.iso.2"]
    assert all(e.split_parts is None for e in folded)


def test_fold_split_iso_entries_requires_two_parts():
    from app.models import FileEntry

    # A lone .0 (no .1) is an interrupted split, not a finished one-file result
    # (that would be a plain Game.iso). Folding it would hide the raw partial row
    # and disable the single-row/bulk delete actions needed for recovery, so
    # leave it visible as its raw .0 row.
    entries = [
        FileEntry(name="Game.iso.0", path="/d/Game.iso.0", type="file", size=1,
                  extension=".0"),
    ]
    folded = files_routes._fold_split_iso_entries(entries)
    names = [e.name for e in folded]
    assert names == ["Game.iso.0"]
    assert all(e.split_parts is None for e in folded)


# --------------------------------------------------------------------------- #
# Split: overwrite cleanup, conflict detection, stall-probe widening
# --------------------------------------------------------------------------- #


def test_detect_output_surfaces_split_set(tmp_path):
    # A >4 GB ``-s`` build leaves only the split set (no bare <folder>.iso), so
    # detect_output must probe it and still badge the source folder's row,
    # pointing at the .0 part RPCS3 mounts.
    folder = str(_make_ps3_folder(tmp_path / "MyGame"))
    out = str(tmp_path / "MyGame.iso")
    tool = registry.get("makeps3iso")

    # No output yet -> no badge.
    assert tool.detect_output(folder) is None

    # A lone .0 (interrupted split) is not a finished output -> still no badge.
    Path(f"{out}.0").write_bytes(b"0")
    assert tool.detect_output(folder) is None

    # A complete split set (.0 + .1) -> badged, ready, pointing at .0.
    Path(f"{out}.1").write_bytes(b"1")
    status = tool.detect_output(folder)
    assert status is not None
    assert status.exists is True
    assert status.ready is True
    assert status.path == f"{out}.0"

    # A plain <folder>.iso still wins (bare path present).
    Path(f"{out}.0").unlink()
    Path(f"{out}.1").unlink()
    Path(out).write_bytes(b"iso")
    bare = tool.detect_output(folder)
    assert bare is not None and bare.path == out


@pytest.mark.asyncio
async def test_clear_existing_output_rejects_directory(tmp_path, monkeypatch):
    # When the overwrite target already exists as a *directory* named like the
    # output, remove_outputs can't clear it (os.remove fails on a dir and is
    # suppressed) and makeps3iso would write *inside* it. Reject instead.
    import app.services.job_manager as jm_module
    from app.services.job_manager import job_manager

    async def _noop_clear(_path):
        return None

    monkeypatch.setattr(jm_module.verification_store, "clear", _noop_clear)

    out = str(tmp_path / "MyGame.iso")
    Path(out).mkdir()  # a directory shadowing the output path
    job = ConversionJob(
        id="ps3dirout1",
        file_path=str(tmp_path / "MyGame"),
        filename="MyGame",
        mode=ConversionMode.FOLDER_TO_ISO,
        status=JobStatus.PROCESSING,
        created_at=datetime.now(timezone.utc),
        output_path=out,
        input_kind=InputKind.DIRECTORY,
        allow_overwrite=True,
    )
    with pytest.raises(RuntimeError, match="not a file"):
        await job_manager._clear_existing_output(job)
    # The directory is left intact, not partially clobbered.
    assert Path(out).is_dir()


@pytest.mark.asyncio
async def test_process_job_rejects_existing_split_set_without_overwrite(tmp_path):
    # A prior split build left Game.iso.0/.1 (no bare Game.iso), so the bare-path
    # output lock can't see the collision. A non-overwrite directory job reaching
    # the worker must fail like a normal output collision rather than clobber the
    # existing split set (and unlink its parts on a later failure cleanup).
    from app.services.job_manager import job_manager
    from services.concurrency_manager import concurrency_manager
    from services.lock_manager import lock_manager

    folder = str(_make_ps3_folder(tmp_path / "MyGame"))
    out = str(tmp_path / "MyGame.iso")
    Path(f"{out}.0").write_bytes(b"0")
    Path(f"{out}.1").write_bytes(b"1")
    job = ConversionJob(
        id="ps3wcol1",
        file_path=folder,
        filename="MyGame",
        mode=ConversionMode.FOLDER_TO_ISO,
        status=JobStatus.QUEUED,
        created_at=datetime.now(timezone.utc),
        output_path=out,
        input_kind=InputKind.DIRECTORY,
        allow_overwrite=False,
    )
    job_manager.jobs[job.id] = job
    try:
        await job_manager._process_job(job.id)
        assert job.status == JobStatus.FAILED
        assert "already exists" in (job.error_message or "")
        # The prior split set is left intact, never clobbered.
        assert Path(f"{out}.0").exists() and Path(f"{out}.1").exists()
        # The output lock taken for the collision check is released again.
        _, is_locked = lock_manager.check_file_status(out)
        assert is_locked is False
    finally:
        lock_manager.release_lock(out)
        concurrency_manager.release(job.id)
        job_manager.jobs.pop(job.id, None)
        job_manager._cancel_events.pop(job.id, None)
        job_manager._cancelled.discard(job.id)


@pytest.mark.asyncio
async def test_process_job_revalidates_ps3_tree_after_dir_lock(tmp_path, monkeypatch):
    # A queued folder_to_iso job can be planned while safe, then have a symlink
    # inserted before it reaches the worker. The worker must revalidate after
    # acquiring the directory subtree lock and before makeps3iso runs.
    from app.services import job_manager as job_manager_module
    from app.services.job_manager import job_manager
    from services.concurrency_manager import concurrency_manager
    from services.lock_manager import lock_manager

    folder_path = _make_ps3_folder(tmp_path / "volume" / "MyGame")
    out = str(tmp_path / "volume" / "MyGame.iso")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (folder_path / "PS3_GAME" / "LEAK.TXT").symlink_to(outside)

    # is_safe_directory_tree consults its own imported settings singleton; patch
    # that exact global so the temporary tree is treated as the configured volume.
    monkeypatch.setattr(
        job_manager_module.is_safe_directory_tree.__globals__["settings"],
        "chd_volumes",
        str(tmp_path / "volume"),
    )
    monkeypatch.setattr(
        job_manager_module.is_safe_directory_tree.__globals__["settings"],
        "data_mount_root",
        str(tmp_path / "volume"),
    )

    called = False

    async def fake_convert(*_args, **_kwargs):
        nonlocal called
        called = True
        yield {"progress": 100, "message": "should not run"}

    monkeypatch.setattr(
        job_manager_module.registry.for_mode("folder_to_iso"), "convert", fake_convert,
    )

    job = ConversionJob(
        id="ps3unsafe1",
        file_path=str(folder_path),
        filename="MyGame",
        mode=ConversionMode.FOLDER_TO_ISO,
        status=JobStatus.QUEUED,
        created_at=datetime.now(timezone.utc),
        output_path=out,
        input_kind=InputKind.DIRECTORY,
        allow_overwrite=False,
    )
    job_manager.jobs[job.id] = job
    try:
        await job_manager._process_job(job.id)
        assert job.status == JobStatus.FAILED
        assert "PS3 folder contains symlinks" in (job.error_message or "")
        assert called is False
        _, output_locked = lock_manager.check_file_status(out)
        assert output_locked is False
        assert lock_manager.dir_lock_would_conflict(str(folder_path)) is False
    finally:
        lock_manager.release_lock(out)
        lock_manager.release_dir_lock(str(folder_path))
        concurrency_manager.release(job.id)
        job_manager.jobs.pop(job.id, None)
        job_manager._cancel_events.pop(job.id, None)
        job_manager._cancelled.discard(job.id)


@pytest.mark.asyncio
async def test_process_job_revalidation_is_non_destructive_on_overwrite(tmp_path, monkeypatch):
    # An overwrite folder_to_iso job whose source turned unsafe after planning
    # must fail *without* deleting the user's prior output: the revalidation
    # runs before _clear_existing_output, so the existing ISO is preserved.
    from app.services import job_manager as job_manager_module
    from app.services.job_manager import job_manager
    from services.concurrency_manager import concurrency_manager
    from services.lock_manager import lock_manager

    folder_path = _make_ps3_folder(tmp_path / "volume" / "MyGame")
    out = str(tmp_path / "volume" / "MyGame.iso")
    # A prior output already exists — an overwrite job would normally clear it.
    with open(out, "wb") as prior:
        prior.write(b"previous-iso-contents")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (folder_path / "PS3_GAME" / "LEAK.TXT").symlink_to(outside)

    monkeypatch.setattr(
        job_manager_module.is_safe_directory_tree.__globals__["settings"],
        "chd_volumes",
        str(tmp_path / "volume"),
    )
    monkeypatch.setattr(
        job_manager_module.is_safe_directory_tree.__globals__["settings"],
        "data_mount_root",
        str(tmp_path / "volume"),
    )

    called = False

    async def fake_convert(*_args, **_kwargs):
        nonlocal called
        called = True
        yield {"progress": 100, "message": "should not run"}

    monkeypatch.setattr(
        job_manager_module.registry.for_mode("folder_to_iso"), "convert", fake_convert,
    )

    job = ConversionJob(
        id="ps3unsafe2",
        file_path=str(folder_path),
        filename="MyGame",
        mode=ConversionMode.FOLDER_TO_ISO,
        status=JobStatus.QUEUED,
        created_at=datetime.now(timezone.utc),
        output_path=out,
        input_kind=InputKind.DIRECTORY,
        allow_overwrite=True,
    )
    job_manager.jobs[job.id] = job
    try:
        await job_manager._process_job(job.id)
        assert job.status == JobStatus.FAILED
        assert "PS3 folder contains symlinks" in (job.error_message or "")
        assert called is False
        # The prior output must be untouched — rejection is non-destructive.
        assert os.path.exists(out)
        with open(out, "rb") as saved:
            assert saved.read() == b"previous-iso-contents"
    finally:
        lock_manager.release_lock(out)
        lock_manager.release_dir_lock(str(folder_path))
        concurrency_manager.release(job.id)
        job_manager.jobs.pop(job.id, None)
        job_manager._cancel_events.pop(job.id, None)
        job_manager._cancelled.discard(job.id)


@pytest.mark.asyncio
async def test_process_job_rejects_directory_output(tmp_path):
    # A directory shadowing the output path (MyGame.iso/) is never a valid
    # makeps3iso target. acquire_lock already rejects it (its ``not isfile``
    # clause fires for both overwrite states), so the worker fails the job rather
    # than writing *into* the directory. Regression guard against that clobber.
    from app.services.job_manager import job_manager
    from services.concurrency_manager import concurrency_manager
    from services.lock_manager import lock_manager

    folder = str(_make_ps3_folder(tmp_path / "MyGame"))
    out = str(tmp_path / "MyGame.iso")
    Path(out).mkdir()  # a directory occupying the output path
    job = ConversionJob(
        id="ps3wdir1",
        file_path=folder,
        filename="MyGame",
        mode=ConversionMode.FOLDER_TO_ISO,
        status=JobStatus.QUEUED,
        created_at=datetime.now(timezone.utc),
        output_path=out,
        input_kind=InputKind.DIRECTORY,
        allow_overwrite=False,
    )
    job_manager.jobs[job.id] = job
    try:
        await job_manager._process_job(job.id)
        assert job.status == JobStatus.FAILED
        # The directory is left intact, never written into.
        assert Path(out).is_dir()
        _, is_locked = lock_manager.check_file_status(out)
        assert is_locked is False
    finally:
        lock_manager.release_lock(out)
        concurrency_manager.release(job.id)
        job_manager.jobs.pop(job.id, None)
        job_manager._cancel_events.pop(job.id, None)
        job_manager._cancelled.discard(job.id)


def test_check_output_conflicts_split_aware(tmp_path):
    out = str(tmp_path / "Game.iso")
    # A prior split build left only the .0 part (no plain Game.iso).
    Path(out + ".0").write_bytes(b"p0")
    exists, _ = convert_routes.check_output_conflicts("folder_to_iso", out)
    assert exists is True
    # A non-directory mode ignores the .0 probe (no plain file present).
    exists_other, _ = convert_routes.check_output_conflicts("createcd", out)
    assert exists_other is False


@pytest.mark.asyncio
async def test_clear_existing_output_gated_on_overwrite(tmp_path, monkeypatch):
    # Overwrite cleanup of a prior split set is gated on allow_overwrite, so a
    # set that wasn't authorized for replacement (e.g. appeared post-plan) is
    # never deleted out from under a skip/rename decision.
    import app.services.job_manager as jm_module
    from app.services.job_manager import job_manager

    # The verification store needs a DB; stub its clear for this unit test.
    async def _noop_clear(_path):
        return None

    monkeypatch.setattr(jm_module.verification_store, "clear", _noop_clear)

    out = str(tmp_path / "MyGame.iso")
    Path(f"{out}.0").write_bytes(b"0")
    Path(f"{out}.1").write_bytes(b"1")
    job = ConversionJob(
        id="ps3ovw1",
        file_path=str(tmp_path / "MyGame"),
        filename="MyGame",
        mode=ConversionMode.FOLDER_TO_ISO,
        status=JobStatus.PROCESSING,
        created_at=datetime.now(timezone.utc),
        output_path=out,
        input_kind=InputKind.DIRECTORY,
        allow_overwrite=False,
    )
    await job_manager._clear_existing_output(job)
    assert Path(f"{out}.0").exists() and Path(f"{out}.1").exists()

    job.allow_overwrite = True
    await job_manager._clear_existing_output(job)
    assert not Path(f"{out}.0").exists() and not Path(f"{out}.1").exists()


def test_find_active_job_for_inflight_split_part(tmp_path):
    # While a split build runs, its numbered parts (Game.iso.0/.1/…) must read
    # as in-use even though they aren't in the static candidate paths, so a
    # rename/delete can't mutate a part mid-write.
    from app.services.job_manager import job_manager

    out = str(tmp_path / "MyGame.iso")
    job = ConversionJob(
        id="ps3split1",
        file_path=str(tmp_path / "src.iso"),
        filename="src.iso",
        mode=ConversionMode.FOLDER_TO_ISO,
        status=JobStatus.PROCESSING,
        created_at=datetime.now(timezone.utc),
        output_path=out,
        input_kind=InputKind.DIRECTORY,
        split=True,
    )
    job_manager.jobs[job.id] = job
    try:
        assert job_manager.find_active_job_for_path(f"{out}.0") is job
        assert job_manager.find_active_job_for_path(f"{out}.1") is job
        # An unrelated sibling is not in use.
        assert job_manager.find_active_job_for_path(str(tmp_path / "Other.iso")) is None
        # Without split, a numbered part is no longer matched.
        job.split = False
        assert job_manager.find_active_job_for_path(f"{out}.1") is None
    finally:
        job_manager.jobs.pop(job.id, None)


@pytest.mark.asyncio
async def test_service_convert_split_widens_stall_probe(tmp_path, monkeypatch):
    folder = str(_make_ps3_folder(tmp_path / "MyGame"))
    out_iso = str(tmp_path / "MyGame.iso")
    captured: dict = {}

    async def fake_run(
        cmd, *, output_path, complete_message, output_growth_paths=None, **_kwargs,
    ):
        captured["growth"] = output_growth_paths
        Path(f"{output_path}.0").write_bytes(b"a")
        Path(f"{output_path}.1").write_bytes(b"b")
        yield {"progress": 100, "message": complete_message}

    monkeypatch.setattr(makeps3iso_service._runner, "run", fake_run)
    _ = [
        u
        async for u in makeps3iso_service.convert(
            folder, out_iso, "folder_to_iso", split=True,
        )
    ]
    # A split run widens the stall probe to follow the renamed/added parts.
    assert callable(captured["growth"])
    assert set(captured["growth"]()) == {out_iso, f"{out_iso}.0", f"{out_iso}.1"}


@pytest.mark.asyncio
async def test_service_convert_no_split_leaves_stall_probe_default(tmp_path, monkeypatch):
    folder = str(_make_ps3_folder(tmp_path / "MyGame"))
    out_iso = str(tmp_path / "MyGame.iso")
    captured: dict = {}

    async def fake_run(
        cmd, *, output_path, complete_message, output_growth_paths=None, **_kwargs,
    ):
        captured["growth"] = output_growth_paths
        Path(output_path).write_bytes(b"iso")
        yield {"progress": 100, "message": complete_message}

    monkeypatch.setattr(makeps3iso_service._runner, "run", fake_run)
    _ = [
        u
        async for u in makeps3iso_service.convert(folder, out_iso, "folder_to_iso")
    ]
    # Non-split build uses the default single-path probe (None).
    assert captured["growth"] is None
