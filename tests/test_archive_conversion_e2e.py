"""End-to-end archive-conversion test for every convertible extension.

Drives the *real* pipeline for each supported input format:

    POST /jobs (convert route)  ->  plan_job (output-path naming)
        ->  job_manager.create_job  ->  _process_job
            ->  archive_service.extract_file   (REAL extraction from a
                                                 real on-disk .zip)
            ->  registry.for_mode(mode).convert (the only stubbed piece,
                                                 stands in for the external
                                                 chdman / dolphin-tool /
                                                 z3ds_compressor binary)

The stub binary reads the extracted member (proving extraction actually
ran and handed over the right file) and writes the output the way the real
tool would. The test then asserts the output landed next to the archive
with the correct, input-derived extension and that the temp dir was cleaned
up. This is the regression guard for issue #113 across the whole matrix.
"""
from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from app.models import ConversionMode, DuplicateAction, JobCreateRequest, JobStatus
from app.routes import convert as convert_routes

# (input extension, mode, expected output extension). Covers every member of
# registry.archive_input_extensions(): chdman create sources, Dolphin sources,
# 3DS sources, and Switch (nsz) sources. z3ds and nsz are the interesting
# cases, their output extension is derived from the input, so each input
# extension must map distinctly.
MATRIX = [
    (".gdi", ConversionMode.CREATECD, ".chd"),
    (".cue", ConversionMode.CREATECD, ".chd"),
    (".bin", ConversionMode.CREATECD, ".chd"),
    (".iso", ConversionMode.CREATECD, ".chd"),
    (".gcz", ConversionMode.DOLPHIN_RVZ, ".rvz"),
    (".wia", ConversionMode.DOLPHIN_RVZ, ".rvz"),
    (".wbfs", ConversionMode.DOLPHIN_RVZ, ".rvz"),
    (".rvz", ConversionMode.DOLPHIN_ISO, ".iso"),
    (".cci", ConversionMode.Z3DS_COMPRESS, ".zcci"),
    (".cia", ConversionMode.Z3DS_COMPRESS, ".zcia"),
    (".3ds", ConversionMode.Z3DS_COMPRESS, ".z3ds"),
    (".nsp", ConversionMode.NSZ_COMPRESS, ".nsz"),
    (".xci", ConversionMode.NSZ_COMPRESS, ".xcz"),
    (".nsz", ConversionMode.NSZ_DECOMPRESS, ".nsp"),
    (".xcz", ConversionMode.NSZ_DECOMPRESS, ".xci"),
    (".iso", ConversionMode.CSO_COMPRESS, ".cso"),
    (".iso", ConversionMode.CSO2_COMPRESS, ".cso"),
    (".iso", ConversionMode.ZSO_COMPRESS, ".zso"),
    (".iso", ConversionMode.DAX_COMPRESS, ".dax"),
    (".cso", ConversionMode.CSO_DECOMPRESS, ".iso"),
    (".zso", ConversionMode.CSO_DECOMPRESS, ".iso"),
    (".dax", ConversionMode.CSO_DECOMPRESS, ".iso"),
    # chdman extract-from-archive: a .chd pulled out of an archive decompresses
    # back to its disc image. The output extension is mode-derived.
    (".chd", ConversionMode.EXTRACTCD, ".cue"),
    (".chd", ConversionMode.EXTRACTDVD, ".iso"),
    (".chd", ConversionMode.EXTRACTRAW, ".raw"),
    (".chd", ConversionMode.EXTRACTHD, ".raw"),
    (".chd", ConversionMode.EXTRACTLD, ".avi"),
    # nkit2iso: the only COMPOUND-extension source. The flattened member name
    # keeps ".nkit.iso"/".nkit.gcz", and the whole compound suffix (not just the
    # trailing ".iso"/".gcz") must be stripped, or the output would collide with
    # the member's own name.
    (".nkit.iso", ConversionMode.NKIT_RESTORE, ".iso"),
    (".nkit.gcz", ConversionMode.NKIT_RESTORE, ".iso"),
]


@pytest.fixture(name="e2e_env")
def _e2e_env(tmp_path: Path, monkeypatch):
    """Confine the route to ``tmp_path`` and stub the converter binary.

    Returns the recorded ``(input_path, output_path)`` the stub was invoked
    with so tests can assert the real extracted file reached the tool.
    """
    # Confine path validation + extraction temp dir to the test directory.
    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "temp_dir", str(tmp_path / "temp"))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 0)

    calls: list[dict] = []

    async def fake_convert(input_path, output_path, mode, *, compression=None,
                           split=False, cancel_event=None):
        # The member must have been extracted to a real temp file before the
        # tool is invoked, this is the core of the archive-conversion path.
        assert os.path.isfile(input_path), f"member not extracted: {input_path}"
        # Capture the extracted bytes now: the temp dir is removed during the
        # job's cleanup, so it can't be re-read after _process_job returns.
        calls.append({
            "input": input_path,
            "output": output_path,
            "content": Path(input_path).read_bytes(),
        })
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        # Stand in for the binary: produce the output file.
        Path(output_path).write_bytes(Path(input_path).read_bytes())
        yield {"progress": 50, "message": "working"}
        yield {"progress": 100, "message": "done"}

    async def noop_post_convert(input_path, output_path, mode):
        # createcd/createdvd would otherwise read a disc serial off the (junk)
        # source via ChdmanTool.post_convert; stub the hook so the test stays
        # deterministic and offline.
        return None

    # registry is the shared singleton job_manager dispatches through.
    for tool in convert_routes.registry.all():
        monkeypatch.setattr(tool, "convert", fake_convert)
        monkeypatch.setattr(tool, "post_convert", noop_post_convert)

    return {"tmp_path": tmp_path, "calls": calls}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ext", "mode", "out_ext"), MATRIX,
    ids=[f"{m[0]}-{m[1].value}" for m in MATRIX],
)
async def test_archive_member_converts_end_to_end(e2e_env, ext, mode, out_ext):
    tmp_path: Path = e2e_env["tmp_path"]

    # A real archive with the member nested in a subdirectory so we also
    # exercise the subdir->flattened-name path.
    archive = tmp_path / "library.zip"
    member = f"games/rom{ext}"
    payload = b"PAYLOAD-" + ext.encode()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(member, payload)

    request = JobCreateRequest(
        file_path=f"{archive}::{member}",
        mode=mode,
        duplicate_action=DuplicateAction.SKIP,
        delete_on_verify=False,
    )

    # Real route planning + queue (returns the job with the planned output_path).
    job = await convert_routes.create_job(request)

    # Real extraction + stubbed conversion + cleanup.
    await convert_routes.job_manager._process_job(job.id)

    # Output lands next to the archive, subdir flattened, with the
    # input-derived extension (the .cci/.cia/.3ds distinction is the #113 fix).
    expected_output = tmp_path / f"games_rom{out_ext}"
    assert job.output_path == str(expected_output)
    assert expected_output.is_file()
    assert job.status.value == JobStatus.COMPLETED.value, job.error_message

    # The stub binary received the genuinely-extracted member, not the
    # "archive::member" pseudo-path.
    assert len(e2e_env["calls"]) == 1
    call = e2e_env["calls"][0]
    assert os.path.basename(call["input"]) == f"rom{ext}"
    assert call["content"] == payload

    # Temp extraction dir is cleaned up after the job finishes (the job nulls
    # its temp_dir handle, so verify via the now-removed extracted file).
    assert job.temp_dir is None
    assert not os.path.exists(call["input"])


@pytest.mark.asyncio
async def test_invalid_nsz_archive_member_returns_bad_extension(e2e_env):
    """Invalid Switch archive members should fail validation before output
    path mapping tries to derive .nsz/.xcz names from the member suffix."""
    from fastapi import HTTPException

    tmp_path: Path = e2e_env["tmp_path"]
    archive = tmp_path / "switch.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("bad.txt", b"not-a-switch-container")

    request = JobCreateRequest(
        file_path=f"{archive}::bad.txt",
        mode=ConversionMode.NSZ_COMPRESS,
        duplicate_action=DuplicateAction.SKIP,
        delete_on_verify=False,
    )
    with pytest.raises(HTTPException) as exc:
        await convert_routes.create_job(request)

    status, detail = convert_routes._SKIP_HTTP[
        convert_routes.SkipReason.NSZ_BAD_EXTENSION
    ]
    assert exc.value.status_code == status == 400
    assert exc.value.detail == detail


@pytest.mark.asyncio
async def test_archive_chd_member_rejected_for_recompress(e2e_env):
    """Recompressing a .chd straight out of an archive is a pointless round
    trip, so chdman's copy mode keeps allows_archive_input=False and the route
    must reject it — even though the extract modes now accept a .chd from an
    archive (decompress) and copy shares the same .chd input extension."""
    from fastapi import HTTPException

    tmp_path: Path = e2e_env["tmp_path"]
    archive = tmp_path / "chds.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("disc.chd", b"chd-bytes")

    request = JobCreateRequest(
        file_path=f"{archive}::disc.chd",
        mode=ConversionMode.COPY,
        duplicate_action=DuplicateAction.SKIP,
        delete_on_verify=False,
    )
    with pytest.raises(HTTPException) as exc:
        await convert_routes.create_job(request)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [ConversionMode.ROMZ_7Z, ConversionMode.ROMZ_ZIP])
async def test_romz_member_listed_but_not_recompressed(e2e_env, mode):
    """A handheld ROM inside an archive is surfaced for visibility (it's a known
    source extension, so browse lists it) but is NOT convertible in place: romz
    compress keeps allows_archive_input=False, so recompressing it would be
    recursive. The route must reject it even though it appears in the listing."""
    from fastapi import HTTPException

    tmp_path: Path = e2e_env["tmp_path"]
    archive = tmp_path / "Solo.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("inner.gba", b"rom")

    request = JobCreateRequest(
        file_path=f"{archive}::inner.gba",
        mode=mode,
        duplicate_action=DuplicateAction.SKIP,
        delete_on_verify=False,
    )
    with pytest.raises(HTTPException) as exc:
        await convert_routes.create_job(request)
    # Assert the *reason* (archive-input rejection), not just any 400, so this
    # guards the listing-vs-convertibility split rather than an unrelated
    # validation failure.
    status, detail = convert_routes._SKIP_HTTP[
        convert_routes.SkipReason.ARCHIVE_INPUT_NOT_ALLOWED
    ]
    assert exc.value.status_code == status == 400
    assert exc.value.detail == detail
