"""Tests for the JWUDTool service (Wii U .wud <-> .wux).

These mock ``asyncio.create_subprocess_exec``, so they need neither a Java
runtime, the JWUDTool jar, nor a 25 GB disc image. The WUX fixtures are built
from the real container layout (JNUSLib ``WUDImageCompressedInfo``), just with
a tiny sector size so a full, structurally valid container fits in a few KB.
"""
from __future__ import annotations

import asyncio
import struct
from pathlib import Path

import pytest

from app.services import jwudtool as jwud_module
from app.services.chdman import ConversionCancelled

service = jwud_module.jwudtool_service

SECTOR = jwud_module.WUD_IMAGE_SIZE // 1024  # a whole number of sectors


def _build_wux(
    tmp_path: Path,
    name: str = "game.wux",
    *,
    sector_size: int = SECTOR,
    uncompressed_size: int | None = None,
    magic: bytes = jwud_module.WUX_MAGIC,
    stored_sectors: int = 1,
    truncate_table: bool = False,
    truncate_sectors: bool = False,
) -> Path:
    """Write a structurally real (if tiny-sectored) WUX container."""
    uncompressed = (
        jwud_module.WUD_IMAGE_SIZE if uncompressed_size is None else uncompressed_size
    )
    header = struct.pack(
        "<4sIIIQ", magic, jwud_module.WUX_MAGIC_1, sector_size, 0, uncompressed,
    ) + b"\0" * (jwud_module.WUX_HEADER_SIZE - 0x18)

    entry_count = (uncompressed + sector_size - 1) // sector_size
    # Every logical sector maps into the stored range, round-robin, so the
    # highest referenced index is exactly stored_sectors - 1.
    table = b"".join(
        struct.pack("<I", i % stored_sectors) for i in range(entry_count)
    )
    path = tmp_path / name
    if truncate_table:
        # Stop right here: padding out to the sector array would hand the
        # verifier a *complete* table and it would fail on the missing sectors
        # instead, never exercising the index-table check.
        path.write_bytes(header + table[: len(table) // 2])
        return path

    body = header + table
    sector_array_offset = len(header) + entry_count * 4 + sector_size - 1
    sector_array_offset -= sector_array_offset % sector_size
    body += b"\0" * (sector_array_offset - len(body))
    stored = stored_sectors - 1 if truncate_sectors else stored_sectors
    body += b"S" * (sector_size * stored)

    path.write_bytes(body)
    return path


class _FakeProcess:
    """Reads preset stdout chunks then EOF; wait() sets the return code."""

    def __init__(self, pid: int, chunks: list[bytes], returncode: int = 0):
        self.pid = pid
        self._chunks = list(chunks)
        self.returncode = None
        self._final_rc = returncode
        self.killed = False
        self.stdout = self

    async def read(self, _n: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = -9 if self.killed else self._final_rc
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def communicate(self):
        if self.returncode is None:
            self.returncode = self._final_rc
        return b"", b""


def _produced_path(argv: list[str]) -> Path:
    """Where the real tool would write: <-out folder>/game.wux|game.wud."""
    out_dir = Path(argv[argv.index("-out") + 1])
    name = "game.wud" if "-decompress" in argv else "game.wux"
    return out_dir / name


async def _drain(agen) -> list[dict]:
    return [u async for u in agen]


@pytest.fixture(name="fake_binary")
def _fake_binary(monkeypatch):
    """Point the service at an executable path so its readiness guard passes."""
    monkeypatch.setattr(service, "jwudtool_path", "/bin/sh")


def _install_fake_exec(monkeypatch, chunks, *, returncode=0, write_output=True,
                       recorded=None):
    def fake_exec(program, *args, **_kwargs):
        argv = [program, *args]
        if recorded is not None:
            recorded.append(argv)
        if write_output:
            _produced_path(argv).write_bytes(b"converted")
        return _make(argv)

    async def _make(_argv):
        return _FakeProcess(4242, chunks, returncode=returncode)

    async def _fake_exec(program, *args, **kwargs):
        return await fake_exec(program, *args, **kwargs)

    monkeypatch.setattr(jwud_module.asyncio, "create_subprocess_exec", _fake_exec)


# --- command / output paths --------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "flag"),
    [("jwud_compress", "-compress"), ("jwud_decompress", "-decompress")],
)
def test_build_command_passes_a_folder_and_the_direction(mode, flag, fake_binary):
    cmd = service._build_command("/data/game.wud", "/data/.jwud-tmp", mode)

    # nice/ionice may be prefixed by the shared priority policy; the tool's own
    # argv is the tail.
    assert cmd[-6:] == [
        "/bin/sh", "-in", "/data/game.wud", "-out", "/data/.jwud-tmp", flag,
    ]
    # The verification pass is deliberately left on (no -noVerify).
    assert "-noVerify" not in cmd


@pytest.mark.parametrize(
    ("mode", "src", "out"),
    [
        ("jwud_compress", "/data/Game.wud", "/data/Game.wux"),
        ("jwud_decompress", "/data/Game.wux", "/data/Game.wud"),
    ],
)
def test_output_path_for_mode(mode, src, out):
    assert service.get_output_path_for_mode(mode, src) == out
    assert service.get_output_path_for_mode(mode, src, "/out") == str(
        Path("/out") / Path(out).name,
    )
    # Archive members arrive as flattened names that keep their extension, so
    # treat_as_stem needs no separate branch.
    assert service.get_output_path_for_mode(
        mode, src, "/out", treat_as_stem=True,
    ) == str(Path("/out") / Path(out).name)


def test_output_path_rejects_unknown_extension():
    with pytest.raises(ValueError, match="Unsupported file extension"):
        service.get_output_path_for_mode("jwud_compress", "/data/game.iso")


# --- convert -----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "src", "out"),
    [
        ("jwud_compress", "game.wud", "game.wux"),
        ("jwud_decompress", "game.wux", "game.wud"),
    ],
)
async def test_convert_moves_the_produced_file_onto_the_output_path(
    tmp_path, monkeypatch, fake_binary, mode, src, out,
):
    """JWUDTool names the file itself inside -out; we move it into place."""
    source = tmp_path / src
    source.write_bytes(b"image")
    target = tmp_path / "renamed" / out
    recorded: list[list[str]] = []
    _install_fake_exec(monkeypatch, [b"Compression successful!\n"], recorded=recorded)

    updates = await _drain(service.convert(str(source), str(target), mode))

    assert target.read_bytes() == b"converted"
    assert updates[-1]["progress"] == 100
    # The private work dir is removed, so the destination holds only the output.
    assert [p.name for p in target.parent.iterdir()] == [out]
    # It ran inside a temp dir under the destination, not the destination itself.
    out_arg = Path(recorded[0][recorded[0].index("-out") + 1])
    assert out_arg.parent == target.parent
    assert out_arg.name.startswith(".jwud-")


@pytest.mark.asyncio
async def test_convert_maps_both_phases_onto_one_bar(tmp_path, monkeypatch, fake_binary):
    """Conversion runs 1-50 %, JWUDTool's own verification pass 51-99 %."""
    source = tmp_path / "game.wud"
    source.write_bytes(b"image")
    _install_fake_exec(monkeypatch, [
        b"Compressing into .wux | Progress 50.00% | Ratio: 1:2.00 | Read: 1MB\r",
        b"Compressing into .wux | Progress 100.00% | Ratio: 1:2.00 | Read: 2MB\r",
        b"Compression successful!\n",
        b"Verification: 1.00MB done (50.00%)\r",
        b"Verification: 2.00MB done (100.00%)\r",
        b"Compressed files is valid.\n",
    ])

    updates = await _drain(
        service.convert(str(source), str(tmp_path / "game.wux"), "jwud_compress"),
    )

    progress = [u["progress"] for u in updates]
    assert progress[0] == 1
    assert 25 in progress          # conversion at half way
    assert 50 in progress          # conversion complete
    assert 75 in progress          # verification at half way
    assert 99 in progress          # verification complete
    assert progress[-1] == 100     # emitted once the file is in place
    assert progress == sorted(progress)


@pytest.mark.asyncio
async def test_convert_fails_when_the_tools_own_verification_reports_invalid(
    tmp_path, monkeypatch, fake_binary,
):
    """JWUDTool prints the warning but still exits 0, so we must catch it."""
    source = tmp_path / "game.wud"
    source.write_bytes(b"image")
    _install_fake_exec(monkeypatch, [
        b"Compression successful!\n",
        b"Warning! (De)Compressed file is INVALID!\n",
    ])

    with pytest.raises(RuntimeError, match="invalid"):
        await _drain(
            service.convert(str(source), str(tmp_path / "game.wux"), "jwud_compress"),
        )


@pytest.mark.asyncio
async def test_convert_fails_when_a_clean_exit_produced_nothing(
    tmp_path, monkeypatch, fake_binary,
):
    """A refusal ("wrong filesize", "already compressed") also exits 0."""
    source = tmp_path / "game.wud"
    source.write_bytes(b"image")
    _install_fake_exec(
        monkeypatch,
        [b"Given WUD has not the expected filesize\n"],
        write_output=False,
    )

    with pytest.raises(RuntimeError, match="no output file"):
        await _drain(
            service.convert(str(source), str(tmp_path / "game.wux"), "jwud_compress"),
        )


@pytest.mark.asyncio
async def test_convert_cleans_up_the_work_dir_on_failure(
    tmp_path, monkeypatch, fake_binary,
):
    source = tmp_path / "game.wud"
    source.write_bytes(b"image")
    _install_fake_exec(monkeypatch, [b"boom\n"], returncode=1)

    with pytest.raises(RuntimeError):
        await _drain(
            service.convert(str(source), str(tmp_path / "game.wux"), "jwud_compress"),
        )

    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".jwud-")]


@pytest.mark.asyncio
async def test_convert_honours_cancellation(tmp_path, monkeypatch, fake_binary):
    source = tmp_path / "game.wud"
    source.write_bytes(b"image")
    cancel = asyncio.Event()
    cancel.set()
    _install_fake_exec(monkeypatch, [b"Compressing into .wux | Progress 1.00%\r"])

    with pytest.raises(ConversionCancelled):
        await _drain(
            service.convert(
                str(source), str(tmp_path / "game.wux"), "jwud_compress",
                cancel_event=cancel,
            ),
        )


@pytest.mark.asyncio
async def test_convert_rejects_an_unknown_mode(tmp_path, fake_binary):
    with pytest.raises(ValueError, match="Unsupported JWUDTool mode"):
        await _drain(
            service.convert(str(tmp_path / "a.wud"), str(tmp_path / "a.wux"), "nope"),
        )


@pytest.mark.asyncio
async def test_convert_reports_a_missing_binary_up_front(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "jwudtool_path", str(tmp_path / "absent-jwudtool"))

    with pytest.raises(RuntimeError, match="not available"):
        await _drain(
            service.convert(
                str(tmp_path / "a.wud"), str(tmp_path / "a.wux"), "jwud_compress",
            ),
        )


def test_binary_available_tracks_the_configured_path(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "jwudtool_path", "/bin/sh")
    assert service.binary_available() is True
    monkeypatch.setattr(service, "jwudtool_path", str(tmp_path / "absent"))
    assert service.binary_available() is False


# --- header / info -----------------------------------------------------------


def test_read_wux_header_matches_the_container_geometry(tmp_path):
    wux = _build_wux(tmp_path, sector_size=SECTOR, stored_sectors=3)

    header = jwud_module.read_wux_header(str(wux))

    assert header["sector_size"] == SECTOR
    assert header["uncompressed_size"] == jwud_module.WUD_IMAGE_SIZE
    assert header["entry_count"] == jwud_module.WUD_IMAGE_SIZE // SECTOR
    assert header["sector_array_offset"] % SECTOR == 0


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"magic": b"WUX1"}, "WUX0 magic"),
        ({"uncompressed_size": 1234}, "uncompressed size"),
    ],
)
def test_read_wux_header_rejects_malformed_containers(tmp_path, kwargs, match):
    wux = _build_wux(tmp_path, **kwargs)

    with pytest.raises(ValueError, match=match):
        jwud_module.read_wux_header(str(wux))


def test_read_wux_header_rejects_a_stub_file(tmp_path):
    stub = tmp_path / "game.wux"
    stub.write_bytes(b"WUX0")

    with pytest.raises(ValueError, match="too short"):
        jwud_module.read_wux_header(str(stub))


def test_info_reports_the_ratio_from_the_wux_header(tmp_path):
    wux = _build_wux(tmp_path, stored_sectors=2)

    info = service.info(str(wux))

    assert info["compressed"] is True
    assert info["format"].startswith("WUX")
    assert info["original_size"] == jwud_module.WUD_IMAGE_SIZE
    assert info["ratio"].endswith("%")


def test_info_for_a_raw_wud_has_no_ratio(tmp_path):
    wud = tmp_path / "game.wud"
    wud.write_bytes(b"raw image")

    info = service.info(str(wud))

    assert info["compressed"] is False
    assert info["compression_type"] is None
    assert info["original_size"] is None
    assert info["ratio"] is None


def test_info_survives_a_malformed_wux_header(tmp_path):
    """A bad header costs the extras only; verify is where it is reported."""
    wux = _build_wux(tmp_path, magic=b"NOPE")

    info = service.info(str(wux))

    assert info["compressed"] is True
    assert info["original_size"] is None


def test_info_rejects_a_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        service.info(str(tmp_path / "absent.wux"))


# --- verify ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_accepts_a_well_formed_container(tmp_path):
    wux = _build_wux(tmp_path, stored_sectors=4)

    result = await service.verify(str(wux))

    assert result["valid"] is True
    assert "4 stored sectors" in result["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        # A short index table is caught by the table walk itself...
        ({"truncate_table": True}, "sector index table is truncated"),
        # ...a short sector array by the length check that follows it.
        ({"truncate_sectors": True}, "wux is truncated"),
        ({"magic": b"NOPE"}, "not a valid wux container"),
    ],
)
async def test_verify_rejects_corrupt_containers(tmp_path, kwargs, match):
    wux = _build_wux(tmp_path, stored_sectors=4, **kwargs)

    result = await service.verify(str(wux))

    assert result["valid"] is False
    assert match in result["message"].lower()


@pytest.mark.asyncio
async def test_verify_rejects_the_raw_source_extension(tmp_path):
    wud = tmp_path / "game.wud"
    wud.write_bytes(b"raw image")

    result = await service.verify(str(wud))

    assert result["valid"] is False
    assert "invalid extension" in result["message"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("make", "match"),
    [
        (lambda p: p, "not found"),
        (lambda p: (p.write_bytes(b"") or p), "empty"),
    ],
)
async def test_verify_rejects_missing_and_empty_files(tmp_path, make, match):
    result = await service.verify(str(make(tmp_path / "game.wux")))

    assert result["valid"] is False
    assert match in result["message"].lower()


# --- split dumps -------------------------------------------------------------


def _make_split_set(tmp_path: Path, parts: int = 4) -> list[Path]:
    """A wudump-style split set: game_part1.wud … game_partN.wud."""
    return [
        _write(tmp_path / (jwud_module.WUD_SPLIT_PART_TEMPLATE % i), b"part")
        for i in range(1, parts + 1)
    ]


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("game_part1.wud", 1),
        ("game_part12.wud", 12),
        # Out of JNUSLib's 1..12 range, so not a part at all.
        ("game_part13.wud", None),
        ("game_part0.wud", None),
        # JNUSLib compares the filename with String.equals, so the template is
        # literal: anything else is an ordinary image.
        ("MyGame_part1.wud", None),
        ("game_part1.wux", None),
        ("game.wud", None),
    ],
)
def test_split_part_index_matches_the_jnuslib_template(name, expected):
    assert jwud_module.split_part_index(f"/data/{name}") == expected


def test_split_set_parts_reports_the_rest_of_the_set(tmp_path):
    parts = _make_split_set(tmp_path, 4)

    assert jwud_module.split_set_parts(str(parts[0])) == [str(p) for p in parts[1:]]
    # Only part 1 owns the set; the others report nothing.
    assert jwud_module.split_set_parts(str(parts[2])) == []


def test_split_set_parts_stops_at_the_first_gap(tmp_path):
    _make_split_set(tmp_path, 2)
    # part3 missing, part4 present: JNUSLib reads the parts in order, so the
    # set ends at the gap rather than silently skipping it.
    _write(tmp_path / (jwud_module.WUD_SPLIT_PART_TEMPLATE % 4), b"part")

    parts = jwud_module.split_set_parts(str(tmp_path / "game_part1.wud"))

    assert [Path(p).name for p in parts] == ["game_part2.wud"]


def test_split_secondary_needs_the_primary_beside_it(tmp_path):
    parts = _make_split_set(tmp_path, 3)

    assert jwud_module.is_split_secondary(str(parts[1])) is True
    assert jwud_module.is_split_secondary(str(parts[0])) is False

    # A stray part with no part 1 is left alone rather than silently hidden.
    orphan_dir = tmp_path / "orphan"
    orphan_dir.mkdir()
    orphan = _write(orphan_dir / "game_part7.wud", b"part")
    assert jwud_module.is_split_secondary(str(orphan)) is False


def test_split_primary_names_its_output_after_the_disc():
    # game_part1.wud is one member of one image, so the product is game.wux --
    # naming it game_part1.wux would imply a per-part output.
    assert service.get_output_path_for_mode(
        "jwud_compress", "/data/game_part1.wud",
    ) == "/data/game.wux"
    assert service.output_stem("/data/game_part1.wud") == "game"
    assert service.output_stem("/data/game.wud") == "game"


# --- verification toggle -----------------------------------------------------


@pytest.mark.parametrize(
    ("compression", "expected"),
    [
        (None, True),
        ("", True),
        ("verify", True),
        ("noverify", False),
        ("NoVerify", False),
        # A level the picker may append is ignored.
        ("noverify:19", False),
        # Anything unrecognised keeps the guarantee rather than dropping it.
        ("zlib", True),
    ],
)
def test_verification_enabled_defaults_to_on(compression, expected):
    assert jwud_module.verification_enabled(compression) is expected


@pytest.mark.parametrize(
    ("compression", "expect_flag"),
    [("verify", False), ("noverify", True)],
)
def test_build_command_passes_noverify_only_when_asked(
    compression, expect_flag, fake_binary,
):
    cmd = service._build_command(
        "/data/game.wud", "/data/.jwud-tmp", "jwud_compress", compression,
    )

    assert ("-noVerify" in cmd) is expect_flag


@pytest.mark.asyncio
async def test_convert_uses_the_whole_bar_when_verification_is_off(
    tmp_path, monkeypatch, fake_binary,
):
    """With one phase instead of two, the conversion shouldn't stall at 50 %."""
    source = tmp_path / "game.wud"
    source.write_bytes(b"image")
    _install_fake_exec(monkeypatch, [
        b"Compressing into .wux | Progress 50.00% | Ratio: 1:2.00 | Read: 1MB\r",
        b"Compressing into .wux | Progress 100.00% | Ratio: 1:2.00 | Read: 2MB\r",
        b"Compression successful!\n",
    ])

    updates = await _drain(
        service.convert(
            str(source), str(tmp_path / "game.wux"), "jwud_compress",
            compression="noverify",
        ),
    )

    progress = [u["progress"] for u in updates]
    assert 50 in progress   # half way through the conversion, not the whole job
    assert 99 in progress   # conversion complete
    assert progress[-1] == 100
    assert progress == sorted(progress)


@pytest.mark.asyncio
async def test_convert_still_fails_on_an_invalid_verification_result(
    tmp_path, monkeypatch, fake_binary,
):
    """The decompress direction reports the marker too, and must also fail."""
    source = tmp_path / "game.wux"
    source.write_bytes(b"image")
    _install_fake_exec(monkeypatch, [
        b"Decompression successful!\n",
        b"Warning! (De)Compressed file is INVALID!\n",
    ])

    with pytest.raises(RuntimeError, match="decompressed image does not match"):
        await _drain(
            service.convert(
                str(source), str(tmp_path / "game.wud"), "jwud_decompress",
            ),
        )
