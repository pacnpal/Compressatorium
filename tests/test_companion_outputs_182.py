"""Tests for the ``companion_outputs`` plugin hook (#182).

A mode that produces more than one file declares its extra outputs in one place —
via ``ModeSpec.companion_exts`` (a static suffix swap, e.g. extractcd's ``.bin``)
or a tool override (makeps3iso's disk-probed split parts) — so every consumer
(conflict-check, unique-name probing, overwrite-clear, size-sum, in-use tracking)
enumerates them from the hook instead of re-hardcoding the suffix per site.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models import ConversionJob, ConversionMode, InputKind, JobStatus
from app.routes import convert as convert_routes
from app.services.tools import registry


# --- extractcd: static .bin sidecar (BaseTool.companion_outputs default) ------

def test_extractcd_companion_is_bin_sidecar():
    assert registry.for_mode("extractcd").companion_outputs(
        "/data/Game.cue", "extractcd",
    ) == ["/data/Game.bin"]


def test_extractcd_companion_tracks_renamed_stem():
    # The .bin companion follows a renamed primary (Game_1.cue -> Game_1.bin),
    # and is returned regardless of on-disk existence (it's a predicted path).
    assert registry.for_mode("extractcd").companion_outputs(
        "/data/Game_1.cue", "extractcd",
    ) == ["/data/Game_1.bin"]


@pytest.mark.parametrize(
    "mode",
    ["extractdvd", "extractraw", "extracthd", "extractld",
     "createcd", "createdvd", "copy"],
)
def test_modes_without_companions_return_empty(mode):
    assert registry.for_mode(mode).companion_outputs("/data/Game.out", mode) == []


def test_cso_to_chd_chain_has_no_companions():
    # ChainTool uses the BaseTool default; cso_to_chd's single .chd has none.
    assert registry.for_mode("cso_to_chd").companion_outputs(
        "/data/Game.chd", "cso_to_chd",
    ) == []


# --- folder_to_iso: disk-probed split parts (MakePs3IsoTool override) ---------

def test_folder_to_iso_no_companions_for_bare_iso(tmp_path):
    out = tmp_path / "Game.iso"
    out.write_bytes(b"iso")  # bare sub-4 GB build: the .iso *is* the primary
    assert registry.for_mode("folder_to_iso").companion_outputs(
        str(out), "folder_to_iso",
    ) == []


def test_folder_to_iso_companions_are_ordered_numbered_parts(tmp_path):
    base = tmp_path / "Game.iso"  # a -s split build leaves no bare .iso
    # Write parts out of order to prove the result is deterministically ordered.
    (tmp_path / "Game.iso.1").write_bytes(b"p1")
    (tmp_path / "Game.iso.0").write_bytes(b"p0")
    (tmp_path / "Game.iso.2").write_bytes(b"p2")
    assert registry.for_mode("folder_to_iso").companion_outputs(
        str(base), "folder_to_iso",
    ) == [
        str(tmp_path / "Game.iso.0"),
        str(tmp_path / "Game.iso.1"),
        str(tmp_path / "Game.iso.2"),
    ]


def test_folder_to_iso_no_companions_before_anything_written(tmp_path):
    # Queue time: neither the bare .iso nor any part exists yet.
    base = tmp_path / "Game.iso"
    assert registry.for_mode("folder_to_iso").companion_outputs(
        str(base), "folder_to_iso",
    ) == []


def test_folder_to_iso_missing_output_does_not_scan_siblings(tmp_path, monkeypatch):
    # Duplicate preflight can be called for many attacker-supplied nonexistent
    # paths. A missing split set must be a constant-time .0 probe, not a full
    # scan of every sibling in a large output directory.
    base = tmp_path / "Fake.iso"
    for idx in range(10):
        (tmp_path / f"unrelated-{idx}").write_bytes(b"x")

    def fail_scandir(path):  # noqa: ARG001 - proves the vulnerable path is gone
        raise AssertionError("folder_to_iso duplicate check scanned siblings")

    monkeypatch.setattr("app.services.makeps3iso.os.scandir", fail_scandir)

    exists, locked = convert_routes.check_output_conflicts(
        "folder_to_iso", str(base),
    )

    assert exists is False
    assert locked is False


# --- extractcd sibling: every consumer derives the .bin from companion_outputs.
# (The folder_to_iso companion side is covered by test_makeps3iso.py.)

def test_extractcd_conflict_detects_bin_sibling(tmp_path):
    # Only the .bin sibling exists (the .cue primary is absent): the duplicate
    # preflight must still report a conflict, sourced from companion_outputs.
    (tmp_path / "Game.bin").write_bytes(b"data")
    exists, _locked = convert_routes.check_output_conflicts(
        "extractcd", str(tmp_path / "Game.cue"),
    )
    assert exists


def test_extractcd_unique_name_steps_past_bin_sibling(tmp_path):
    # Game.cue is free but Game.bin is taken, so a rename must advance to
    # Game_1.cue (whose Game_1.bin is also free) instead of reusing Game.cue and
    # clobbering the existing .bin.
    (tmp_path / "Game.bin").write_bytes(b"data")
    out = convert_routes.get_unique_output_path(
        str(tmp_path / "Game.cue"), "extractcd",
    )
    assert out == str(tmp_path / "Game_1.cue")


def test_extractcd_job_protects_bin_companion_in_use(tmp_path):
    # An active extractcd job's .bin companion counts as in-use even though only
    # the .cue is its recorded output_path — in-use tracking reads companions.
    from app.services.job_manager import job_manager

    cue = str(tmp_path / "Game.cue")
    bin_path = str(tmp_path / "Game.bin")
    (tmp_path / "Game.bin").write_bytes(b"data")
    job = ConversionJob(
        id="extractcd-sibling",
        file_path=str(tmp_path / "Game.chd"),
        filename="Game.chd",
        mode=ConversionMode.EXTRACTCD,
        status=JobStatus.PROCESSING,
        created_at=datetime.now(timezone.utc),
        output_path=cue,
        input_kind=InputKind.FILE,
    )
    job_manager.jobs[job.id] = job
    try:
        assert job_manager.find_active_job_for_path(bin_path) is job
        assert job_manager._is_path_in_use_by_other_job("other", bin_path) is True
    finally:
        job_manager.jobs.pop(job.id, None)


def test_candidate_paths_skips_directory_companion_scan(tmp_path):
    # A folder_to_iso job's split parts must NOT be enumerated by _candidate_paths
    # (the directory companion scan is skipped — the parts are covered I/O-free by
    # _split_output_blocks), keeping this sync helper event-loop-safe.
    from app.services.job_manager import job_manager

    out = str(tmp_path / "Game.iso")
    (tmp_path / "Game.iso.0").write_bytes(b"p0")
    (tmp_path / "Game.iso.1").write_bytes(b"p1")
    job = ConversionJob(
        id="fti-candidates",
        file_path=str(tmp_path / "MyGame"),
        filename="MyGame",
        mode=ConversionMode.FOLDER_TO_ISO,
        status=JobStatus.PROCESSING,
        created_at=datetime.now(timezone.utc),
        output_path=out,
        input_kind=InputKind.DIRECTORY,
    )

    paths = job_manager._candidate_paths(job)

    assert out in paths
    assert str(tmp_path / "Game.iso.0") not in paths
    assert str(tmp_path / "Game.iso.1") not in paths


@pytest.mark.asyncio
async def test_clear_existing_output_removes_lone_companion(tmp_path):
    # Overwrite was authorized because a stray .bin exists even though the .cue
    # primary is gone; the clear must still remove the lone .bin so it can't
    # collide with the new output.
    from app.services.job_manager import job_manager

    bin_path = tmp_path / "Game.bin"
    bin_path.write_bytes(b"data")
    job = ConversionJob(
        id="extractcd-lone-clear",
        file_path=str(tmp_path / "Game.chd"),
        filename="Game.chd",
        mode=ConversionMode.EXTRACTCD,
        status=JobStatus.PROCESSING,
        created_at=datetime.now(timezone.utc),
        output_path=str(tmp_path / "Game.cue"),  # absent
        input_kind=InputKind.FILE,
        allow_overwrite=True,
    )

    await job_manager._clear_existing_output(job)

    assert not bin_path.exists()


@pytest.mark.asyncio
async def test_clear_existing_output_rejects_non_file_companion(tmp_path):
    # A directory squatting on the .bin companion name can't be unlinked, so the
    # overwrite-clear must reject up front — without removing the primary .cue.
    from app.services.job_manager import job_manager

    cue = tmp_path / "Game.cue"
    cue.write_bytes(b"cue")
    (tmp_path / "Game.bin").mkdir()  # non-file occupant on the companion name
    job = ConversionJob(
        id="extractcd-nonfile-companion",
        file_path=str(tmp_path / "Game.chd"),
        filename="Game.chd",
        mode=ConversionMode.EXTRACTCD,
        status=JobStatus.PROCESSING,
        created_at=datetime.now(timezone.utc),
        output_path=str(cue),
        input_kind=InputKind.FILE,
        allow_overwrite=True,
    )

    with pytest.raises(RuntimeError):
        await job_manager._clear_existing_output(job)
    # Rejected before any unlink: the primary is untouched.
    assert cue.exists()
