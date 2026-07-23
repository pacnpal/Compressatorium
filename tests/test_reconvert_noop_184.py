"""Job-start "recognize prior success" fast path (issue #184, site 1).

A re-queued job whose verified artifact is already on disk must complete as a
no-op instead of re-spawning the converter. The verification store is
monkeypatched (no DB needed) and the tool's ``convert`` is stubbed with a
recorder that fails the test if it is ever invoked on the fast path.

The guard is deliberately narrow: only plain file conversions with default
output shaping (``compression is None``, ``split`` off) and a verification
record whose *source* matches the job and whose output is no older than that
source qualify. The negative tests pin each of those escape hatches so a
future change can't silently start skipping conversions it shouldn't.

Isolation: the pipeline coordinates through the process-global
``concurrency_manager`` / ``lock_manager`` FIFO + file locks. A fresh
``JobManager`` plus fresh, tmp-dir-bound coordinators are bound in place so a
ticket leaked by another suite test can't wedge these (they run in-process).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import app.services.job_manager as jm_mod
from app.models import ConversionMode, JobStatus
from app.services.concurrency_manager import ConcurrencyManager
from app.services.job_manager import JobManager
from app.services.lock_manager import LockManager


class _StubVerStore:
    """Minimal stand-in for the verification store.

    ``get_record`` returns the configured record for the output path so the
    fast path can consult "was this output verified, and from what source".
    """

    def __init__(self, record: dict | None) -> None:
        self._record = record

    async def get_record(self, chd_path: str) -> dict | None:
        return self._record

    async def clear(self, chd_path: str) -> None:
        # Exercised by _clear_existing_output on the re-convert (non-fast) path.
        return None

    async def mark_verified(self, chd_path: str, *, source_path: str | None = None):
        return None


@pytest.fixture(name="noop_env")
def _noop_env(tmp_path: Path, monkeypatch):
    """Fresh, isolated JobManager + coordinators; record any convert call."""
    monkeypatch.setattr(jm_mod.settings, "max_queue_depth", 0)
    # Bind fresh, tmp-dir-scoped coordinators so the shared cross-process FIFO
    # and output locks from other suite tests can't interfere.
    lock_dir = tmp_path / "locks"
    monkeypatch.setattr(jm_mod.settings, "concurrency_lock_dir", str(lock_dir))
    monkeypatch.setattr(
        jm_mod, "concurrency_manager", ConcurrencyManager(1, str(lock_dir / "conc")),
    )
    monkeypatch.setattr(jm_mod, "lock_manager", LockManager())

    calls: list[dict] = []

    async def recording_convert(input_path, output_path, mode, *, compression=None,
                                split=False, cancel_event=None):
        calls.append({"input": input_path, "output": output_path})
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        Path(output_path).write_bytes(b"RECONVERTED")
        yield {"progress": 100, "message": "done"}

    async def noop_post_convert(input_path, output_path, mode):
        return None

    tool = jm_mod.registry.get("chdman")
    monkeypatch.setattr(tool, "convert", recording_convert)
    monkeypatch.setattr(tool, "post_convert", noop_post_convert)

    return {
        "tmp_path": tmp_path,
        "calls": calls,
        "monkeypatch": monkeypatch,
        "mgr": JobManager(max_concurrent=1),
    }


def _set_record(monkeypatch, record: dict | None) -> None:
    monkeypatch.setattr(jm_mod, "verification_store", _StubVerStore(record))


def _seed_verified_output(tmp_path: Path) -> tuple[Path, Path]:
    """Create a source + a pre-existing verified output (output not older)."""
    src = tmp_path / "Game.cue"
    src.write_bytes(b"source")
    out = tmp_path / "Game.chd"
    out.write_bytes(b"VERIFIED-ARTIFACT")
    # Output not older than the source (source unchanged since it was verified)
    # — the freshness half of the guard.
    src_mtime = src.stat().st_mtime
    os.utime(out, (src_mtime, src_mtime))
    return src, out


@pytest.mark.asyncio
async def test_reconvert_completes_as_noop_when_verified_output_exists(noop_env):
    tmp_path: Path = noop_env["tmp_path"]
    mgr: JobManager = noop_env["mgr"]
    src, out = _seed_verified_output(tmp_path)
    original_bytes = out.read_bytes()

    _set_record(noop_env["monkeypatch"], {
        "source_path": os.path.realpath(str(src)),
        "verified_at": "2026-01-01T00:00:00Z",
    })

    job = await mgr.create_job(str(src), ConversionMode.CREATECD, allow_overwrite=True)
    await mgr._process_job(job.id)

    # The converter was never re-spawned and the verified artifact is untouched.
    assert noop_env["calls"] == []
    assert out.read_bytes() == original_bytes
    assert job.status.value == JobStatus.COMPLETED.value, job.error_message
    assert job.progress == 100
    assert job.output_size == len(original_bytes)


@pytest.mark.asyncio
async def test_reconvert_runs_when_source_is_newer_than_output(noop_env):
    tmp_path: Path = noop_env["tmp_path"]
    mgr: JobManager = noop_env["mgr"]
    src, out = _seed_verified_output(tmp_path)
    # Source modified after the output was verified: the verified output is
    # stale, so the converter must run.
    newer = out.stat().st_mtime + 10
    os.utime(src, (newer, newer))

    _set_record(noop_env["monkeypatch"], {
        "source_path": os.path.realpath(str(src)),
        "verified_at": "2026-01-01T00:00:00Z",
    })

    job = await mgr.create_job(str(src), ConversionMode.CREATECD, allow_overwrite=True)
    await mgr._process_job(job.id)

    assert len(noop_env["calls"]) == 1
    assert job.status.value == JobStatus.COMPLETED.value, job.error_message


@pytest.mark.asyncio
async def test_reconvert_runs_when_record_source_mismatches(noop_env):
    tmp_path: Path = noop_env["tmp_path"]
    mgr: JobManager = noop_env["mgr"]
    src, _out = _seed_verified_output(tmp_path)

    # The verified output was produced from a *different* source, so it is not
    # this job's prior success — re-convert.
    _set_record(noop_env["monkeypatch"], {
        "source_path": str(tmp_path / "OtherSource.cue"),
        "verified_at": "2026-01-01T00:00:00Z",
    })

    job = await mgr.create_job(str(src), ConversionMode.CREATECD, allow_overwrite=True)
    await mgr._process_job(job.id)

    assert len(noop_env["calls"]) == 1


@pytest.mark.asyncio
async def test_reconvert_runs_when_no_verification_record(noop_env):
    tmp_path: Path = noop_env["tmp_path"]
    mgr: JobManager = noop_env["mgr"]
    src, _out = _seed_verified_output(tmp_path)

    _set_record(noop_env["monkeypatch"], None)

    job = await mgr.create_job(str(src), ConversionMode.CREATECD, allow_overwrite=True)
    await mgr._process_job(job.id)

    assert len(noop_env["calls"]) == 1


@pytest.mark.asyncio
async def test_reconvert_runs_when_record_has_no_source(noop_env):
    tmp_path: Path = noop_env["tmp_path"]
    mgr: JobManager = noop_env["mgr"]
    src, _out = _seed_verified_output(tmp_path)

    # A record from a manual /info verify carries no source_path, so it can't
    # prove this job's prior success.
    _set_record(noop_env["monkeypatch"], {
        "source_path": None, "verified_at": "2026-01-01T00:00:00Z",
    })

    job = await mgr.create_job(str(src), ConversionMode.CREATECD, allow_overwrite=True)
    await mgr._process_job(job.id)

    assert len(noop_env["calls"]) == 1


@pytest.mark.asyncio
async def test_reconvert_runs_when_compression_requested(noop_env):
    tmp_path: Path = noop_env["tmp_path"]
    mgr: JobManager = noop_env["mgr"]
    src, _out = _seed_verified_output(tmp_path)

    # A non-default compression could change the output bytes, so the fast path
    # must step aside and let the converter run.
    _set_record(noop_env["monkeypatch"], {
        "source_path": os.path.realpath(str(src)),
        "verified_at": "2026-01-01T00:00:00Z",
    })

    job = await mgr.create_job(
        str(src), ConversionMode.CREATECD, allow_overwrite=True, compression="cd_lzma",
    )
    await mgr._process_job(job.id)

    assert len(noop_env["calls"]) == 1
