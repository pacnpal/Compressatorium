"""Regression tests for cross-mode parity fixes."""

import asyncio
import os
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import ANY, AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.models import (
    BatchJobCreateRequest,
    ConversionJob,
    ConversionMode,
    DeletePlanRequest,
    JobCreateRequest,
    JobStatus,
)
from app.routes import convert as convert_routes
from app.routes import files as files_routes
from app.services import job_manager as job_manager_module
from app.services.job_manager import JobManager, QueueBackpressureError
from app.utils.delete_plan import build_delete_snapshot


@pytest.fixture(name="files_env")
def _files_env(tmp_path: Path, monkeypatch):
    """Create file fixtures used by files route tests."""
    game_iso = tmp_path / "game.iso"
    game_rvz = tmp_path / "game.rvz"
    wia_iso = tmp_path / "wia.iso"
    wia_output = tmp_path / "wia.wia"
    other_iso = tmp_path / "other.iso"

    game_iso.write_bytes(b"iso")
    game_rvz.write_bytes(b"rvz")
    wia_iso.write_bytes(b"iso")
    wia_output.write_bytes(b"wia")
    other_iso.write_bytes(b"iso")

    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(tmp_path))

    return {
        "root": str(tmp_path),
        "game_iso": str(game_iso),
        "game_rvz": str(game_rvz),
        "wia_iso": str(wia_iso),
        "wia_output": str(wia_output),
        "other_iso": str(other_iso),
    }


def test_job_manager_default_max_concurrent_is_one():
    """JobManager should default to serial processing unless explicitly overridden."""
    manager = JobManager()
    assert manager.max_concurrent == 1


def _dolphin_output(outputs):
    """Return the dolphin OutputStatus (obj or dict) from an outputs list, or None."""
    for o in outputs:
        if (o.tool_id if hasattr(o, "tool_id") else o["tool_id"]) == "dolphin":
            return o
    return None


@pytest.mark.asyncio
async def test_list_files_populates_dolphin_output(files_env):
    """Directory listing should surface Dolphin output presence for inputs."""
    listing = await files_routes.list_files(path=files_env["root"])
    by_name = {entry.name: entry for entry in listing.entries}

    game = by_name["game.iso"]
    assert "dolphin" in game.convertible_by
    assert _dolphin_output(game.outputs).exists is True
    assert _dolphin_output(game.outputs).path == files_env["game_rvz"]
    wia = by_name["wia.iso"]
    assert _dolphin_output(wia.outputs).exists is True
    assert _dolphin_output(wia.outputs).path == files_env["wia_output"]
    other = by_name["other.iso"]
    assert "dolphin" in other.convertible_by
    assert _dolphin_output(other.outputs) is None
    assert _dolphin_output(by_name["game.rvz"].outputs).exists is True


@pytest.mark.asyncio
async def test_search_files_populates_dolphin_output(files_env):
    """Recursive search results should include Dolphin-product state consistently."""
    results = await files_routes.search_files(
        path=files_env["root"], recursive=True, include_archives=False
    )
    by_name = {Path(item["path"]).name: item for item in results["files"]}

    game = by_name["game.iso"]
    assert "dolphin" in game["convertible_by"]
    assert _dolphin_output(game["outputs"]).exists is True
    assert _dolphin_output(game["outputs"]).path == files_env["game_rvz"]
    wia = by_name["wia.iso"]
    assert _dolphin_output(wia["outputs"]).exists is True
    assert _dolphin_output(wia["outputs"]).path == files_env["wia_output"]
    other = by_name["other.iso"]
    assert "dolphin" in other["convertible_by"]
    assert _dolphin_output(other["outputs"]) is None


@pytest.mark.asyncio
async def test_delete_on_verify_error_messages_include_dolphin_and_3ds():
    """Delete-on-verify validation message should reflect all supported tool modes."""
    request = DeletePlanRequest(
        file_paths=["/tmp/any.chd"],
        mode=ConversionMode.EXTRACTCD,
    )
    with pytest.raises(HTTPException) as exc_info:
        await convert_routes.delete_plan(request)
    assert exc_info.value.status_code == 400
    assert "create/copy/Dolphin/3DS/Switch-compress/CSO/CSO2/ZSO/DAX-compress" in str(exc_info.value.detail)

    create_request = JobCreateRequest(
        file_path="/tmp/any.chd",
        mode=ConversionMode.EXTRACTCD,
        delete_on_verify=True,
    )
    with pytest.raises(HTTPException) as create_exc:
        await convert_routes.create_job(create_request)
    assert create_exc.value.status_code == 400
    assert "create/copy/Dolphin/3DS/Switch-compress/CSO/CSO2/ZSO/DAX-compress" in str(create_exc.value.detail)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", [ConversionMode.METADATA_SCAN, ConversionMode.DAT_MATCH],
)
async def test_delete_plan_rejects_external_modes_without_crashing(mode):
    """External (non-conversion) modes are unregistered; delete-plan must still
    return a clean 400 rather than surfacing a registry KeyError as a 500."""
    request = DeletePlanRequest(file_paths=["/tmp/any.chd"], mode=mode)
    with pytest.raises(HTTPException) as exc_info:
        await convert_routes.delete_plan(request)
    assert exc_info.value.status_code == 400
    assert "create/copy/Dolphin/3DS/Switch-compress/CSO/CSO2/ZSO/DAX-compress" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_nsz_compress_accepts_level_token(tmp_path: Path, monkeypatch):
    """nsz compression is '<solid|block>:<level>'; the route must allow the ':'
    token for Switch (regression: validation previously only allowed Dolphin)."""
    source_path = tmp_path / "game.nsp"
    source_path.write_bytes(b"nsp")

    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))
    create_job_mock = AsyncMock()
    monkeypatch.setattr(convert_routes.job_manager, "create_job", create_job_mock)

    request = JobCreateRequest(
        file_path=str(source_path),
        mode=ConversionMode.NSZ_COMPRESS,
        compression="solid:18",
    )
    # Must not raise the "levels only for Dolphin" 400; the job is created.
    await convert_routes.create_job(request)
    create_job_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_job_returns_429_when_queue_is_full(tmp_path: Path, monkeypatch):
    """Single-job submissions should apply queue backpressure with HTTP 429."""
    source_path = tmp_path / "disc.iso"
    source_path.write_bytes(b"iso")

    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 1)
    monkeypatch.setattr(convert_routes.job_manager, "get_queue_depth", lambda: 1)

    create_job_mock = AsyncMock()
    monkeypatch.setattr(convert_routes.job_manager, "create_job", create_job_mock)

    request = JobCreateRequest(
        file_path=str(source_path),
        mode=ConversionMode.CREATECD,
    )

    with pytest.raises(HTTPException) as exc_info:
        await convert_routes.create_job(request)

    assert exc_info.value.status_code == 429
    create_job_mock.assert_not_called()


@pytest.mark.asyncio
async def test_batch_create_returns_429_when_queue_capacity_would_be_exceeded(
    tmp_path: Path, monkeypatch,
):
    """Batch submissions should fail before partial enqueue when over capacity."""
    first = tmp_path / "a.iso"
    second = tmp_path / "b.iso"
    first.write_bytes(b"a")
    second.write_bytes(b"b")

    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 2)
    monkeypatch.setattr(convert_routes.job_manager, "get_queue_depth", lambda: 1)

    create_job_mock = AsyncMock()
    monkeypatch.setattr(convert_routes.job_manager, "create_job", create_job_mock)

    request = BatchJobCreateRequest(
        file_paths=[str(first), str(second)],
        mode=ConversionMode.CREATECD,
    )

    with pytest.raises(HTTPException) as exc_info:
        await convert_routes.create_batch_jobs(request)

    assert exc_info.value.status_code == 429
    create_job_mock.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_all_jobs_endpoint_delegates_to_manager(monkeypatch):
    """Cancel-all route should return JobManager summary payload."""
    payload = {
        "requested": 2,
        "queued": 1,
        "processing": 1,
        "job_ids": ["a1b2c3d4", "e5f6g7h8"],
    }
    cancel_all_mock = AsyncMock(return_value=payload)
    monkeypatch.setattr(convert_routes.job_manager, "cancel_all_jobs", cancel_all_mock)

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/jobs/cancel-all",
            "headers": [(b"x-chd-action-confirm", b"cancel-all-jobs")],
        }
    )

    result = await convert_routes.cancel_all_jobs(request)

    assert result == payload
    cancel_all_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_all_jobs_marks_queued_jobs_cancelled(tmp_path: Path):
    """JobManager cancel-all should cancel all queued jobs in one call."""
    first = tmp_path / "a.iso"
    second = tmp_path / "b.iso"
    first.write_bytes(b"a")
    second.write_bytes(b"b")

    manager = JobManager(max_concurrent=1, max_job_history=10)
    first_job = await manager.create_job(
        str(first), ConversionMode.CREATECD, output_path=str(tmp_path / "a.chd"),
    )
    second_job = await manager.create_job(
        str(second), ConversionMode.CREATECD, output_path=str(tmp_path / "b.chd"),
    )

    result = await manager.cancel_all_jobs()

    assert result["queued"] == 2
    assert result["processing"] == 0
    assert result["requested"] == 2
    assert set(result["job_ids"]) == {first_job.id, second_job.id}
    assert manager.get_job(first_job.id).status == JobStatus.CANCELLED
    assert manager.get_job(second_job.id).status == JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_all_jobs_endpoint_requires_confirmation_header():
    """Cancel-all route should reject calls missing the explicit confirmation header."""
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/jobs/cancel-all",
            "headers": [],
        }
    )

    with pytest.raises(HTTPException) as exc_info:
        await convert_routes.cancel_all_jobs(request)

    assert exc_info.value.status_code == 400
    assert "Missing confirmation header" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_delete_completed_endpoint_requires_confirmation_header():
    """Clear-completed route should reject calls missing confirmation header."""
    request = Request(
        {
            "type": "http",
            "method": "DELETE",
            "path": "/api/jobs/completed",
            "headers": [],
        }
    )

    with pytest.raises(HTTPException) as exc_info:
        await convert_routes.delete_completed_jobs(request)

    assert exc_info.value.status_code == 400
    assert "Missing confirmation header" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_get_job_lookup_returns_archived_job_after_delete(tmp_path: Path):
    """Deleted jobs should remain temporarily retrievable to avoid stale-client 404 loops."""
    source_path = tmp_path / "disc.iso"
    source_path.write_bytes(b"iso")

    manager = JobManager(max_concurrent=1, max_job_history=10)
    job = await manager.create_job(
        str(source_path),
        ConversionMode.CREATECD,
        output_path=str(tmp_path / "disc.chd"),
    )
    job.status = JobStatus.COMPLETED
    job.completed_at = datetime.now(timezone.utc)

    assert await manager.delete_job(job.id) is True
    assert manager.get_job(job.id) is None

    archived = manager.get_job_for_lookup(job.id)
    assert archived is not None
    assert archived.id == job.id
    assert archived.status == JobStatus.COMPLETED


def test_get_job_lookup_refreshes_archive_timestamp(monkeypatch):
    """Archived lookup should refresh recency so TTL pruning reflects latest access."""
    manager = JobManager(max_concurrent=1, max_job_history=10)
    archived_job = ConversionJob(
        id="stale1234",
        file_path="/tmp/game.iso",
        filename="game.iso",
        mode=ConversionMode.CREATECD,
        status=JobStatus.COMPLETED,
        progress=100,
        message="Done",
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        output_path="/tmp/game.chd",
    )
    manager._archived_jobs[archived_job.id] = (archived_job, 100.0)

    monkeypatch.setattr(job_manager_module.time, "monotonic", lambda: 250.0)
    looked_up = manager.get_job_for_lookup(archived_job.id)

    assert looked_up is archived_job
    assert manager._archived_jobs[archived_job.id][1] == 250.0


@pytest.mark.asyncio
async def test_get_job_endpoint_returns_archived_job(monkeypatch):
    """Route lookup should return recently archived jobs instead of immediate 404."""
    archived_job = convert_routes.ConversionJob(
        id="deadbeef",
        file_path="/tmp/game.iso",
        filename="game.iso",
        mode=ConversionMode.CREATECD,
        status=JobStatus.COMPLETED,
        progress=100,
        message="Done",
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        output_path="/tmp/game.chd",
    )
    monkeypatch.setattr(
        convert_routes.job_manager,
        "get_job_for_lookup",
        lambda job_id: archived_job if job_id == "deadbeef" else None,
    )

    result = await convert_routes.get_job("deadbeef")
    assert result.id == "deadbeef"
    assert result.status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_dispatcher_keeps_processing_serial_when_max_concurrent_one(
    tmp_path: Path, monkeypatch,
):
    """The dispatcher should never run more than one conversion at a time in serial mode."""
    source_a = tmp_path / "a.iso"
    source_b = tmp_path / "b.iso"
    output_a = tmp_path / "a.chd"
    output_b = tmp_path / "b.chd"
    source_a.write_bytes(b"a")
    source_b.write_bytes(b"b")

    active = 0
    peak = 0

    async def fake_convert(
        input_path: str,
        output_path: str,
        mode: str = "createcd",
        compression: str | None = None,
        cancel_event=None,
    ):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            Path(output_path).write_bytes(b"out")
            yield {"progress": 25, "message": "working"}
            await asyncio.sleep(0.05)
            yield {"progress": 100, "message": "done"}
        finally:
            active -= 1

    monkeypatch.setattr(job_manager_module.chdman_service, "convert", fake_convert)
    monkeypatch.setattr(
        job_manager_module.chdman_service,
        "verify",
        AsyncMock(return_value={"valid": True, "message": "ok"}),
    )

    manager = JobManager(max_concurrent=1, max_job_history=10)
    first = await manager.create_job(
        str(source_a),
        ConversionMode.CREATECD,
        output_path=str(output_a),
    )
    second = await manager.create_job(
        str(source_b),
        ConversionMode.CREATECD,
        output_path=str(output_b),
    )

    worker_task = asyncio.create_task(manager.process_queue())
    try:
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            first_job = manager.get_job(first.id)
            second_job = manager.get_job(second.id)
            if (
                first_job and first_job.status == JobStatus.COMPLETED
                and second_job and second_job.status == JobStatus.COMPLETED
            ):
                break
            await asyncio.sleep(0.02)

        first_job = manager.get_job(first.id)
        second_job = manager.get_job(second.id)
        assert first_job and first_job.status == JobStatus.COMPLETED
        assert second_job and second_job.status == JobStatus.COMPLETED
        assert peak == 1
    finally:
        manager._running = False
        if manager._dispatcher_task:
            manager._dispatcher_task.cancel()
        with suppress(asyncio.CancelledError):
            await worker_task


@pytest.mark.asyncio
async def test_create_jobs_atomic_rejects_batch_without_partial_enqueue(
    tmp_path: Path, monkeypatch,
):
    """Atomic job creation should reject over-capacity batches before queuing any new job."""
    first = tmp_path / "a.iso"
    second = tmp_path / "b.iso"
    third = tmp_path / "c.iso"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    third.write_bytes(b"c")

    monkeypatch.setattr(job_manager_module.settings, "max_queue_depth", 2)
    manager = JobManager(max_concurrent=1, max_job_history=5)

    # Seed one queued job so adding two more would exceed max_queue_depth=2.
    seeded_job = await manager.create_job(
        str(first),
        ConversionMode.CREATECD,
        output_path=str(tmp_path / "a.chd"),
    )
    try:
        initial_jobs = manager.get_all_jobs()
        assert len(initial_jobs) == 1

        with pytest.raises(QueueBackpressureError):
            await manager.create_jobs_atomic(
                [
                    {
                        "file_path": str(second),
                        "output_path": str(tmp_path / "b.chd"),
                    },
                    {
                        "file_path": str(third),
                        "output_path": str(tmp_path / "c.chd"),
                    },
                ],
                ConversionMode.CREATECD,
            )

        final_jobs = manager.get_all_jobs()
        assert len(final_jobs) == 1
        assert final_jobs[0].file_path == str(first)
    finally:
        # Avoid leaking a FIFO ticket in the shared global concurrency manager.
        job_manager_module.concurrency_manager.release(seeded_job.id)


@pytest.mark.asyncio
async def test_z3ds_delete_on_verify_marks_output_verified(tmp_path: Path, monkeypatch):
    """3DS delete-on-verify should persist verified state like CHD/Dolphin modes."""
    source_path = tmp_path / "game.3ds"
    output_path = tmp_path / "game.z3ds"
    source_path.write_bytes(b"source")

    monkeypatch.setattr(job_manager_module.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(job_manager_module.settings, "data_mount_root", str(tmp_path))

    async def fake_convert(
        input_path: str,
        destination_path: str,
        mode: str = "z3ds_compress",
        compression: str | None = None,
        cancel_event=None,
    ):
        # Simulate conversion writing an output file.
        Path(destination_path).write_bytes(b"converted")
        yield {"progress": 75, "message": "Compressing..."}
        yield {"progress": 100, "message": "Done"}

    async def fake_verify(path: str):
        return {"valid": True, "message": "File verified successfully"}

    mark_verified = AsyncMock()
    clear_verified = AsyncMock()
    clear_metadata = AsyncMock()

    _z3ds_service = job_manager_module.registry.for_mode("z3ds_compress")._service
    monkeypatch.setattr(_z3ds_service, "convert", fake_convert)
    monkeypatch.setattr(_z3ds_service, "verify", fake_verify)
    monkeypatch.setattr(job_manager_module.verification_store, "mark_verified", mark_verified)
    monkeypatch.setattr(job_manager_module.verification_store, "clear", clear_verified)
    monkeypatch.setattr(job_manager_module.chd_metadata_store, "clear", clear_metadata)
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
    snapshot = build_delete_snapshot(str(source_path))
    job = await manager.create_job(
        str(source_path),
        ConversionMode.Z3DS_COMPRESS,
        output_path=str(output_path),
        delete_on_verify=True,
        delete_snapshot=snapshot,
    )

    await manager._process_job(job.id)

    assert job.status == JobStatus.COMPLETED
    # Delete-on-verify also records a produced_meta snapshot (mode + shaping +
    # source/output fingerprints) so a later re-queue can no-op safely (#184).
    mark_verified.assert_called_once_with(
        str(output_path),
        source_path=str(source_path),
        produced_meta=ANY,
    )
    produced = mark_verified.call_args.kwargs["produced_meta"]
    assert produced is not None and produced["mode"] == "z3ds_compress"
    assert not source_path.exists()
    assert output_path.exists()
    # A non-verify-class source (.3ds) leaves the tool-wide verification_store
    # untouched on delete.
    clear_verified.assert_not_called()
    clear_metadata.assert_not_called()


@pytest.mark.asyncio
async def test_delete_on_verify_clears_verification_store_for_non_chd_source(
    tmp_path: Path, monkeypatch,
):
    """A non-CHD verify-class source (.rvz) must clear its tool-wide
    verification_store record on delete-on-verify, while chd_metadata_store
    (CHDMAN-specific) is left untouched."""
    source_path = tmp_path / "game.rvz"
    output_path = tmp_path / "game.wia"
    source_path.write_bytes(b"source")

    monkeypatch.setattr(job_manager_module.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(job_manager_module.settings, "data_mount_root", str(tmp_path))

    async def fake_convert(
        input_path: str,
        destination_path: str,
        mode: str = "dolphin_wia",
        compression: str | None = None,
        cancel_event=None,
    ):
        Path(destination_path).write_bytes(b"converted")
        yield {"progress": 100, "message": "Done"}

    async def fake_verify(path: str):
        return {"valid": True, "message": "File verified successfully"}

    mark_verified = AsyncMock()
    clear_verified = AsyncMock()
    clear_metadata = AsyncMock()

    _dolphin_service = job_manager_module.registry.for_mode("dolphin_wia")._service
    monkeypatch.setattr(_dolphin_service, "convert", fake_convert)
    monkeypatch.setattr(_dolphin_service, "verify", fake_verify)
    monkeypatch.setattr(job_manager_module.verification_store, "mark_verified", mark_verified)
    monkeypatch.setattr(job_manager_module.verification_store, "clear", clear_verified)
    monkeypatch.setattr(job_manager_module.chd_metadata_store, "clear", clear_metadata)
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
    snapshot = build_delete_snapshot(str(source_path))
    job = await manager.create_job(
        str(source_path),
        ConversionMode.DOLPHIN_WIA,
        output_path=str(output_path),
        delete_on_verify=True,
        delete_snapshot=snapshot,
    )

    await manager._process_job(job.id)

    assert job.status == JobStatus.COMPLETED
    assert not source_path.exists()
    assert output_path.exists()
    # The .rvz source is verify-class, so its tool-wide record is cleared...
    clear_verified.assert_called_once_with(str(source_path))
    # ...but chd_metadata_store stays CHDMAN-only and is not touched.
    clear_metadata.assert_not_called()


@pytest.mark.asyncio
async def test_delete_on_verify_rejects_inode_device_fingerprint_mismatch(
    tmp_path: Path, monkeypatch,
):
    """Delete-on-verify must refuse deleting a path whose fingerprint changed."""
    source_path = tmp_path / "game.3ds"
    output_path = tmp_path / "game.z3ds"
    source_path.write_bytes(b"source")
    snapshot = build_delete_snapshot(str(source_path))

    source_stat = source_path.stat()
    replacement = tmp_path / "replacement.3ds"
    replacement.write_bytes(b"source")
    os.utime(
        replacement,
        ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
    )
    os.replace(replacement, source_path)

    monkeypatch.setattr(job_manager_module.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(job_manager_module.settings, "data_mount_root", str(tmp_path))

    async def fake_convert(
        input_path: str,
        destination_path: str,
        mode: str = "z3ds_compress",
        compression: str | None = None,
        cancel_event=None,
    ):
        Path(destination_path).write_bytes(b"converted")
        yield {"progress": 100, "message": "Done"}

    async def fake_verify(path: str):
        return {"valid": True, "message": "File verified successfully"}

    _z3ds_service = job_manager_module.registry.for_mode("z3ds_compress")._service
    monkeypatch.setattr(_z3ds_service, "convert", fake_convert)
    monkeypatch.setattr(_z3ds_service, "verify", fake_verify)
    monkeypatch.setattr(job_manager_module.verification_store, "mark_verified", AsyncMock())
    monkeypatch.setattr(job_manager_module.verification_store, "clear", AsyncMock())
    monkeypatch.setattr(job_manager_module.chd_metadata_store, "clear", AsyncMock())
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
        delete_snapshot=snapshot,
    )

    await manager._process_job(job.id)

    assert job.status == JobStatus.FAILED
    assert "fingerprint mismatch" in (job.error_message or "").lower()
    assert source_path.exists()
