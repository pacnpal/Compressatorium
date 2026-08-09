"""Single-vs-batch planning parity tests for Phase 4.

Phase 4 collapses the per-file validation / output-path / duplicate-handling
pipeline that ``create_job`` and ``create_batch_jobs`` used to re-implement
independently into one shared ``plan_job``. The two endpoints intentionally
react differently to the *same* per-file failure: ``create_job`` raises an
``HTTPException`` while ``create_batch_jobs`` drops the file from the batch
("skip"). These tests lock that divergence in place: for a representative
matrix of modes x archive/non-archive x ``DuplicateAction``, a file that
``create_job`` rejects with a 4xx is exactly the file ``create_batch_jobs``
skips, and an accepted file resolves to an identical ``output_path`` /
``allow_overwrite`` on both paths.
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.models import (
    BatchJobCreateRequest,
    ConversionMode,
    DuplicateAction,
    JobCreateRequest,
)
from app.routes import convert as convert_routes


@pytest.fixture(name="parity_env")
def _parity_env(tmp_path: Path, monkeypatch):
    """Confine routes to ``tmp_path``, disable backpressure, capture sinks."""
    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 0)
    monkeypatch.setattr(convert_routes.job_manager, "get_queue_depth", lambda: 0)

    single_mock = AsyncMock(return_value="job")
    monkeypatch.setattr(convert_routes.job_manager, "create_job", single_mock)

    captured: dict = {}

    async def fake_atomic(job_specs, _mode, **_kwargs):
        captured["job_specs"] = job_specs
        return []

    monkeypatch.setattr(convert_routes.job_manager, "create_jobs_atomic", fake_atomic)

    return {
        "tmp_path": tmp_path,
        "single_mock": single_mock,
        "captured": captured,
    }


async def _run_single(env, *, file_path, mode, duplicate_action, delete_on_verify):
    env["single_mock"].reset_mock()
    request = JobCreateRequest(
        file_path=file_path,
        mode=mode,
        duplicate_action=duplicate_action,
        delete_on_verify=delete_on_verify,
    )
    try:
        await convert_routes.create_job(request)
    except HTTPException as exc:
        return {"status": exc.status_code, "detail": exc.detail}
    call = env["single_mock"].call_args
    return {
        "output_path": call.kwargs["output_path"],
        "allow_overwrite": call.kwargs["allow_overwrite"],
    }


async def _run_batch(env, *, file_path, mode, duplicate_action, delete_on_verify):
    env["captured"].clear()
    request = BatchJobCreateRequest(
        file_paths=[file_path],
        mode=mode,
        duplicate_action=duplicate_action,
        delete_on_verify=delete_on_verify,
    )
    try:
        await convert_routes.create_batch_jobs(request)
    except HTTPException as exc:
        return {"status": exc.status_code, "detail": exc.detail}
    specs = env["captured"]["job_specs"]
    if not specs:
        return {"skipped": True}
    spec = specs[0]
    return {
        "output_path": spec["output_path"],
        "allow_overwrite": spec["allow_overwrite"],
    }


def _make_zip(tmp_path: Path, name: str) -> str:
    archive = tmp_path / name
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("placeholder.txt", "x")
    return str(archive)


# Each case: (id, builder(tmp_path)->file_path, mode, duplicate_action, expect).
# ``expect`` is "reject" (single 4xx / batch skip) or "accept" (job on both).
def _cases():
    def write(tmp_path: Path, name: str) -> str:
        path = tmp_path / name
        path.write_bytes(b"data")
        return str(path)

    return [
        # --- acceptances (single creates a job; batch enqueues a spec) ---
        (
            "createcd_iso_accept",
            lambda t: write(t, "game.iso"),
            ConversionMode.CREATECD,
            DuplicateAction.SKIP,
            "accept",
        ),
        (
            "extractcd_chd_accept",
            lambda t: write(t, "game.chd"),
            ConversionMode.EXTRACTCD,
            DuplicateAction.SKIP,
            "accept",
        ),
        (
            "copy_chd_accept",
            lambda t: write(t, "game.chd"),
            ConversionMode.COPY,
            DuplicateAction.SKIP,
            "accept",
        ),
        (
            "dolphin_rvz_iso_accept",
            lambda t: write(t, "game.iso"),
            ConversionMode.DOLPHIN_RVZ,
            DuplicateAction.SKIP,
            "accept",
        ),
        (
            "dolphin_iso_rvz_accept",
            lambda t: write(t, "game.rvz"),
            ConversionMode.DOLPHIN_ISO,
            DuplicateAction.SKIP,
            "accept",
        ),
        (
            "z3ds_3ds_accept",
            lambda t: write(t, "game.3ds"),
            ConversionMode.Z3DS_COMPRESS,
            DuplicateAction.SKIP,
            "accept",
        ),
        (
            "nkit_restore_nkit_iso_accept",
            lambda t: write(t, "game.nkit.iso"),
            ConversionMode.NKIT_RESTORE,
            DuplicateAction.SKIP,
            "accept",
        ),
        (
            "nkit_restore_nkit_gcz_accept",
            lambda t: write(t, "game.nkit.gcz"),
            ConversionMode.NKIT_RESTORE,
            DuplicateAction.SKIP,
            "accept",
        ),
        (
            "nkit_to_rvz_accept",  # the chain takes the same NKit sources
            lambda t: write(t, "game.nkit.iso"),
            ConversionMode.NKIT_TO_RVZ,
            DuplicateAction.SKIP,
            "accept",
        ),
        # --- rejections / skips ---
        (
            "createcd_chd_reject",  # CREATE_REQUIRES_NON_CHD
            lambda t: write(t, "game.chd"),
            ConversionMode.CREATECD,
            DuplicateAction.SKIP,
            "reject",
        ),
        (
            "extractcd_iso_reject",  # EXTRACT_COPY_REQUIRES_CHD
            lambda t: write(t, "game.iso"),
            ConversionMode.EXTRACTCD,
            DuplicateAction.SKIP,
            "reject",
        ),
        (
            "copy_iso_reject",  # EXTRACT_COPY_REQUIRES_CHD
            lambda t: write(t, "game.iso"),
            ConversionMode.COPY,
            DuplicateAction.SKIP,
            "reject",
        ),
        (
            "dolphin_rvz_bad_ext_reject",  # DOLPHIN_BAD_EXTENSION
            lambda t: write(t, "game.txt"),
            ConversionMode.DOLPHIN_RVZ,
            DuplicateAction.SKIP,
            "reject",
        ),
        (
            "nkit_to_rvz_bad_ext_reject",  # CHAIN_BAD_EXTENSION
            lambda t: write(t, "game.iso"),
            ConversionMode.NKIT_TO_RVZ,
            DuplicateAction.SKIP,
            "reject",
        ),
        (
            "nkit_restore_bad_ext_reject",  # NKIT_BAD_EXTENSION
            # A PLAIN .iso must be rejected: nkit2iso declares the compound
            # `.nkit.iso`, so the generic tail alone is not one of its sources.
            lambda t: write(t, "game.iso"),
            ConversionMode.NKIT_RESTORE,
            DuplicateAction.SKIP,
            "reject",
        ),
        (
            "z3ds_bad_ext_reject",  # Z3DS_BAD_EXTENSION
            lambda t: write(t, "game.txt"),
            ConversionMode.Z3DS_COMPRESS,
            DuplicateAction.SKIP,
            "reject",
        ),
        (
            "createcd_missing_reject",  # FILE_NOT_FOUND
            lambda t: str(t / "absent.iso"),
            ConversionMode.CREATECD,
            DuplicateAction.SKIP,
            "reject",
        ),
        (
            "dolphin_iso_same_path_reject",  # DOLPHIN_SAME_PATH (overwrite)
            lambda t: write(t, "game.iso"),
            ConversionMode.DOLPHIN_ISO,
            DuplicateAction.OVERWRITE,
            "reject",
        ),
        # --- archive inputs ---
        (
            "archive_createcd_accept",  # archive allowed for create
            lambda t: f"{_make_zip(t, 'arch.zip')}::game.iso",
            ConversionMode.CREATECD,
            DuplicateAction.SKIP,
            "accept",
        ),
        (
            "archive_extractcd_reject",  # ARCHIVE_INPUT_NOT_ALLOWED
            lambda t: f"{_make_zip(t, 'arch.zip')}::game.iso",
            ConversionMode.EXTRACTCD,
            DuplicateAction.SKIP,
            "reject",
        ),
        (
            "archive_dolphin_accept",  # archive allowed for Dolphin compress
            lambda t: f"{_make_zip(t, 'arch.zip')}::game.iso",
            ConversionMode.DOLPHIN_RVZ,
            DuplicateAction.SKIP,
            "accept",
        ),
        (
            "archive_z3ds_accept",  # archive allowed for 3DS compress (issue #113)
            lambda t: f"{_make_zip(t, 'arch.zip')}::game.3ds",
            ConversionMode.Z3DS_COMPRESS,
            DuplicateAction.SKIP,
            "accept",
        ),
        (
            "archive_nkit_accept",  # archive allowed for the NKit restore
            lambda t: f"{_make_zip(t, 'arch.zip')}::game.nkit.iso",
            ConversionMode.NKIT_RESTORE,
            DuplicateAction.SKIP,
            "accept",
        ),
        (
            "archive_nkit_plain_iso_reject",  # NKIT_BAD_EXTENSION on a member
            lambda t: f"{_make_zip(t, 'arch.zip')}::game.iso",
            ConversionMode.NKIT_RESTORE,
            DuplicateAction.SKIP,
            "reject",
        ),
        (
            "archive_missing_reject",  # ARCHIVE_NOT_FOUND
            lambda t: f"{t / 'absent.zip'}::game.iso",
            ConversionMode.CREATECD,
            DuplicateAction.SKIP,
            "reject",
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_id", "builder", "mode", "duplicate_action", "expect"),
    _cases(),
    ids=[c[0] for c in _cases()],
)
async def test_single_and_batch_agree(
    parity_env, case_id, builder, mode, duplicate_action, expect,
):
    file_path = builder(parity_env["tmp_path"])
    kwargs = {
        "file_path": file_path,
        "mode": mode,
        "duplicate_action": duplicate_action,
        "delete_on_verify": False,
    }
    single = await _run_single(parity_env, **kwargs)
    batch = await _run_batch(parity_env, **kwargs)

    if expect == "reject":
        assert single.get("status") is not None, (case_id, single)
        assert 400 <= single["status"] < 500, (case_id, single)
        # The file create_job rejects is exactly the file the batch skips.
        assert batch == {"skipped": True}, (case_id, batch)
    else:
        assert "status" not in single, (case_id, single)
        assert "skipped" not in batch, (case_id, batch)
        assert single["output_path"] == batch["output_path"], case_id
        assert single["allow_overwrite"] == batch["allow_overwrite"], case_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expect"),
    [
        (DuplicateAction.SKIP, "reject"),
        (DuplicateAction.OVERWRITE, "accept"),
        (DuplicateAction.RENAME, "accept"),
    ],
)
async def test_existing_output_duplicate_actions_agree(parity_env, action, expect):
    """Existing-output handling must agree across single and batch per action."""
    tmp_path = parity_env["tmp_path"]
    source = tmp_path / "game.iso"
    source.write_bytes(b"iso")
    # The createcd output already exists, triggering duplicate handling.
    (tmp_path / "game.chd").write_bytes(b"chd")

    kwargs = {
        "file_path": str(source),
        "mode": ConversionMode.CREATECD,
        "duplicate_action": action,
        "delete_on_verify": False,
    }
    single = await _run_single(parity_env, **kwargs)
    batch = await _run_batch(parity_env, **kwargs)

    if expect == "reject":
        assert single["status"] == 409
        assert batch == {"skipped": True}
    else:
        assert single["output_path"] == batch["output_path"]
        assert single["allow_overwrite"] == batch["allow_overwrite"]
        if action == DuplicateAction.OVERWRITE:
            assert single["allow_overwrite"] is True
            assert single["output_path"] == str(tmp_path / "game.chd")
        else:  # RENAME picks a fresh sibling path on both paths
            assert single["allow_overwrite"] is False
            assert single["output_path"] == str(tmp_path / "game_1.chd")


@pytest.mark.asyncio
async def test_locked_output_overwrite_agree(parity_env, monkeypatch):
    """An OVERWRITE onto a locked output is a 409 single / skip in batch."""
    tmp_path = parity_env["tmp_path"]
    source = tmp_path / "game.iso"
    source.write_bytes(b"iso")
    locked_output = str(tmp_path / "game.chd")

    def fake_status(path: str):
        # Report the resolved output target as present-and-locked.
        if path == locked_output:
            return True, True
        return False, False

    monkeypatch.setattr(
        convert_routes.lock_manager, "check_file_status", fake_status,
    )

    kwargs = {
        "file_path": str(source),
        "mode": ConversionMode.CREATECD,
        "duplicate_action": DuplicateAction.OVERWRITE,
        "delete_on_verify": False,
    }
    single = await _run_single(parity_env, **kwargs)
    batch = await _run_batch(parity_env, **kwargs)

    assert single["status"] == 409
    assert "currently being converted" in single["detail"]
    assert batch == {"skipped": True}


@pytest.mark.asyncio
async def test_delete_on_verify_snapshot_failure_detail_differs(
    parity_env, monkeypatch,
):
    """Delete-snapshot failures abort BOTH paths, but the detail text differs."""
    tmp_path = parity_env["tmp_path"]
    source = tmp_path / "game.iso"
    source.write_bytes(b"iso")

    def boom(_path: str):
        raise ValueError("nope")

    monkeypatch.setattr(convert_routes, "build_delete_snapshot", boom)

    kwargs = {
        "file_path": str(source),
        "mode": ConversionMode.CREATECD,
        "duplicate_action": DuplicateAction.SKIP,
        "delete_on_verify": True,
    }
    single = await _run_single(parity_env, **kwargs)
    batch = await _run_batch(parity_env, **kwargs)

    assert single["status"] == 400
    assert batch["status"] == 400
    assert single["detail"] == "Delete-on-verify blocked: nope"
    assert batch["detail"] == f"Delete-on-verify blocked for {source}: nope"


@pytest.mark.asyncio
async def test_delete_on_verify_snapshot_success_agrees(parity_env, monkeypatch):
    """A successful snapshot is attached identically on single and batch."""
    tmp_path = parity_env["tmp_path"]
    source = tmp_path / "game.iso"
    source.write_bytes(b"iso")

    monkeypatch.setattr(
        convert_routes, "build_delete_snapshot", lambda path: {"snap": path},
    )

    kwargs = {
        "file_path": str(source),
        "mode": ConversionMode.CREATECD,
        "duplicate_action": DuplicateAction.SKIP,
        "delete_on_verify": True,
    }
    single = await _run_single(parity_env, **kwargs)
    batch = await _run_batch(parity_env, **kwargs)

    assert single["output_path"] == batch["output_path"]
    assert single["allow_overwrite"] == batch["allow_overwrite"]
    # The snapshot reaches the job sink on both paths.
    snap = {"snap": str(source)}
    assert parity_env["single_mock"].call_args.kwargs["delete_snapshot"] == snap
    assert parity_env["captured"]["job_specs"][0]["delete_snapshot"] == snap


@pytest.mark.asyncio
async def test_romz_extract_invalid_archive_rejected_before_overwrite(parity_env):
    """An invalid romz_extract archive is rejected during planning, so the job
    is never created and a same-stem sibling can't be overwritten/deleted.

    Regression: get_output_path_for_mode falls back to the suffix-stripped stem
    for unreadable/invalid archives; with duplicate_action=overwrite that would
    otherwise plan deletion of an unrelated ``Game`` next to ``Game.zip``.
    """
    tmp_path = parity_env["tmp_path"]
    # Archive holds only a placeholder (no ROM) -> not a single-ROM archive.
    archive = _make_zip(tmp_path, "Game.zip")
    sibling = tmp_path / "Game"  # the suffix-stripped fallback output path
    sibling.write_bytes(b"PRECIOUS")

    result = await _run_single(
        parity_env,
        file_path=archive,
        mode=ConversionMode.ROMZ_EXTRACT,
        duplicate_action=DuplicateAction.OVERWRITE,
        delete_on_verify=False,
    )

    assert result["status"] == 422
    parity_env["single_mock"].assert_not_called()
    # The unrelated sibling was never planned for overwrite/deletion.
    assert sibling.read_bytes() == b"PRECIOUS"
