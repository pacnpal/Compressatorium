"""Job-start "recognize prior success" fast path (issue #184, site 1).

A re-queued job whose verified artifact is already on disk completes as a no-op
instead of re-spawning the converter — but only when a ``produced_meta``
snapshot (written by the prior delete-on-verify conversion) proves the on-disk
output is the *exact* result of the current request. The tests below pin every
guard: matching mode/compression/split, an unchanged complete source set
(including a ``.cue``'s ``.bin`` track), and an unchanged output.

The tool's ``convert`` is stubbed with a recorder that fails the test if it is
ever invoked on the fast path. Isolation: a fresh ``JobManager`` plus fresh,
tmp-dir-bound ``concurrency_manager`` / ``lock_manager`` are bound in place so a
ticket leaked by another suite test can't wedge these (they run in-process).
"""
from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest

import app.services.job_manager as jm_mod
from app.models import ConversionMode, JobStatus
from app.services.concurrency_manager import ConcurrencyManager
from app.services.job_manager import JobManager
from app.services.lock_manager import LockManager
from app.services.verification_store import VerificationStore


class _StubVerStore:
    """Stand-in for the verification store returning one fixed record."""

    def __init__(self, record: dict | None) -> None:
        self._record = record

    async def get_record(self, chd_path: str) -> dict | None:
        return self._record

    async def clear(self, chd_path: str) -> None:
        # Exercised by _clear_existing_output on the re-convert (non-fast) path.
        return None

    async def mark_verified(self, chd_path: str, **kwargs):
        return None


@pytest.fixture(name="noop_env")
def _noop_env(tmp_path: Path, monkeypatch):
    """Fresh, isolated JobManager + coordinators; record any convert call."""
    monkeypatch.setattr(jm_mod.settings, "max_queue_depth", 0)
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


def _seed_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A .cue descriptor + its .bin track + a pre-existing verified output."""
    src = tmp_path / "Game.cue"
    src.write_text('FILE "Game.bin" BINARY\n', encoding="utf-8")
    track = tmp_path / "Game.bin"
    track.write_bytes(b"track-bytes")
    out = tmp_path / "Game.chd"
    out.write_bytes(b"VERIFIED-ARTIFACT")
    return src, track, out


async def _job_with_meta(noop_env, **create_kwargs):
    """Create a CREATECD job for the seeded .cue and the produced_meta that
    matches the current on-disk state."""
    tmp_path: Path = noop_env["tmp_path"]
    src, track, out = _seed_files(tmp_path)
    mgr: JobManager = noop_env["mgr"]
    job = await mgr.create_job(
        str(src), ConversionMode.CREATECD, allow_overwrite=True, **create_kwargs,
    )
    meta = mgr._build_produced_meta(job)
    assert meta is not None  # sanity: source set + output are fingerprintable
    return mgr, job, src, track, out, meta


def _record(meta):
    return {
        "source_path": "irrelevant-superseded-by-produced_meta",
        "verified_at": "2026-01-01T00:00:00Z",
        "produced_meta": meta,
    }


@pytest.mark.asyncio
async def test_reconvert_completes_as_noop_when_meta_matches(noop_env):
    mgr, job, _src, _track, out, meta = await _job_with_meta(noop_env)
    original = out.read_bytes()
    _set_record(noop_env["monkeypatch"], _record(meta))

    await mgr._process_job(job.id)

    assert noop_env["calls"] == []          # converter never re-spawned
    assert out.read_bytes() == original     # verified artifact untouched
    assert job.status.value == JobStatus.COMPLETED.value, job.error_message
    assert job.progress == 100
    assert job.output_size == len(original)


@pytest.mark.asyncio
async def test_reconvert_runs_when_compression_differs(noop_env):
    # Prior artifact was produced with explicit compression; this request uses
    # the default. Same source, but a different output shape → must re-convert
    # (P1: avoid reusing output without matching prior conversion settings).
    mgr, job, _src, _track, _out, meta = await _job_with_meta(noop_env)
    tampered = copy.deepcopy(meta)
    tampered["compression"] = "cd_lzma"
    _set_record(noop_env["monkeypatch"], _record(tampered))

    await mgr._process_job(job.id)
    assert len(noop_env["calls"]) == 1


@pytest.mark.asyncio
async def test_reconvert_runs_when_mode_differs(noop_env):
    mgr, job, _src, _track, _out, meta = await _job_with_meta(noop_env)
    tampered = copy.deepcopy(meta)
    tampered["mode"] = "createdvd"
    _set_record(noop_env["monkeypatch"], _record(tampered))

    await mgr._process_job(job.id)
    assert len(noop_env["calls"]) == 1


@pytest.mark.asyncio
async def test_reconvert_runs_when_track_file_changed(noop_env):
    # The .cue descriptor is untouched but its .bin track is replaced. A
    # descriptor-only mtime check would miss this; the full source fingerprint
    # catches it (P1: include CUE/GDI track files in the freshness check).
    mgr, job, _src, track, _out, meta = await _job_with_meta(noop_env)
    _set_record(noop_env["monkeypatch"], _record(meta))
    track.write_bytes(b"different-track-bytes-entirely")
    later = os.stat(track).st_mtime + 50
    os.utime(track, (later, later))

    await mgr._process_job(job.id)
    assert len(noop_env["calls"]) == 1


@pytest.mark.asyncio
async def test_reconvert_runs_when_output_replaced(noop_env):
    # The output is replaced after verification (e.g. corruption/tamper) while
    # the source is unchanged. The output fingerprint no longer matches, so the
    # job must not report the replacement as verified (P1: revalidate the
    # artifact before reporting the no-op as verified).
    mgr, job, _src, _track, out, meta = await _job_with_meta(noop_env)
    _set_record(noop_env["monkeypatch"], _record(meta))
    out.write_bytes(b"REPLACED-WITH-SOMETHING-ELSE")
    later = os.stat(out).st_mtime + 50
    os.utime(out, (later, later))

    await mgr._process_job(job.id)
    assert len(noop_env["calls"]) == 1


@pytest.mark.asyncio
async def test_reconvert_runs_when_no_produced_meta(noop_env):
    # A record without produced_meta (e.g. a manual /info verify) can't prove
    # prior success → re-convert.
    mgr, job, _src, _track, _out, _meta = await _job_with_meta(noop_env)
    _set_record(noop_env["monkeypatch"], {
        "source_path": os.path.realpath(str(_src)),
        "verified_at": "2026-01-01T00:00:00Z",
        "produced_meta": None,
    })

    await mgr._process_job(job.id)
    assert len(noop_env["calls"]) == 1


@pytest.mark.asyncio
async def test_reconvert_runs_when_no_record(noop_env):
    mgr, job, *_ = await _job_with_meta(noop_env)
    _set_record(noop_env["monkeypatch"], None)

    await mgr._process_job(job.id)
    assert len(noop_env["calls"]) == 1


def test_produced_meta_round_trips_through_store(tmp_path):
    """The store persists and returns produced_meta unchanged (JSON column)."""
    store = VerificationStore(store_path=str(tmp_path / "v.db"))
    out = tmp_path / "Game.chd"
    out.write_bytes(b"x")
    meta = {
        "mode": "createcd",
        "compression": None,
        "split": False,
        "source": {"/vol/Game.cue": {"size": 12, "mtime_ns": 123456789}},
        "output": {"size": 1, "mtime_ns": 111},
    }
    import asyncio

    asyncio.run(store.mark_verified(str(out), source_path=str(out), produced_meta=meta))
    record = asyncio.run(store.get_record(str(out)))
    assert record is not None
    assert record["produced_meta"] == meta
    # A verify with no meta leaves the column NULL.
    other = tmp_path / "Other.chd"
    other.write_bytes(b"y")
    asyncio.run(store.mark_verified(str(other)))
    other_rec = asyncio.run(store.get_record(str(other)))
    assert other_rec is not None
    assert other_rec["produced_meta"] is None
