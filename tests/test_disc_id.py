"""
Tests for the disc_id extraction service.

Covers:
  - ISO 9660 source-file parsing (PS2, PSP, PS1)
  - Dreamcast IP.BIN parsing
  - PSP PARAM.SFO binary parsing
  - Dreamcast GDI track parsing
  - BIN sector-stream adapter
  - CD CHD sector extraction (Mode 1 and Mode 2 Form 1)
  - CHD extraction stubs (dumpmeta / companion-file paths)
"""

from __future__ import annotations

import io
import asyncio
import struct
import zlib
from unittest.mock import patch

import pytest

from app.services.disc_id import (
    TAG_GAME,
    TAG_NAME,
    _BinSectorStream,
    _CHDReader,
    _CHDSectorStream,
    _MAX_CHD_HUNK_BYTES,
    _RAW_SECTOR_HEADER_SIZE_MODE2,
    _dumpmeta_raw,
    _extract_cue,
    _extract_from_chd_sectors,
    _extract_gdi,
    _extract_iso,
    _normalize_ps_serial,
    _parse_ipbin,
    _parse_param_sfo,
    _parse_system_cnf,
    clear_embedded_disc_id,
    embed_in_chd,
    ensure_disc_id_embedded,
    extract_from_chd,
    extract_from_source,
    read_embedded_game_id,
)

# ---------------------------------------------------------------------------
# Helpers to build minimal disc images in memory
# ---------------------------------------------------------------------------

SECTOR = 2048
PVD_SECTOR = 16
PVD_MAGIC = b"\x01CD001\x01"


def _make_iso(files: dict[str, bytes]) -> bytes:
    """
    Build a minimal ISO 9660 image with the given {path: content} mapping.
    Supports one level of sub-directory (e.g. "PSP_GAME/PARAM.SFO").
    Returns raw bytes (2048-byte sectors).
    """
    # Allocate a mutable bytearray, grow lazily.
    sectors: list[bytearray] = [bytearray(SECTOR) for _ in range(20)]

    def ensure(n: int):
        while len(sectors) <= n:
            sectors.append(bytearray(SECTOR))

    def write_sector(idx: int, data: bytes):
        ensure(idx)
        sectors[idx][: len(data)] = data

    def dir_record(name: str, lba: int, size: int, is_dir: bool = False) -> bytes:
        name_enc = name.encode("ascii")
        name_len = len(name_enc)
        rec_len = 33 + name_len
        if rec_len % 2:
            rec_len += 1
        r = bytearray(rec_len)
        r[0] = rec_len
        # LBA (little-endian 4 bytes at offset 2)
        struct.pack_into("<I", r, 2, lba)
        struct.pack_into(">I", r, 6, lba)
        # Size (little-endian 4 bytes at offset 10)
        struct.pack_into("<I", r, 10, size)
        struct.pack_into(">I", r, 14, size)
        r[25] = 0x02 if is_dir else 0x00
        r[32] = name_len
        r[33 : 33 + name_len] = name_enc
        return bytes(r)

    # Place directories / files starting at sector 20
    next_sector = 20

    # Collect top-level names and sub-dirs
    top_files: dict[str, bytes] = {}
    sub_dirs: dict[str, dict[str, bytes]] = {}

    for path, content in files.items():
        parts = path.split("/")
        if len(parts) == 1:
            top_files[parts[0]] = content
        elif len(parts) == 2:
            sub_dirs.setdefault(parts[0], {})[parts[1]] = content

    # Write sub-directory sectors first so we know their LBAs
    sub_dir_lbas: dict[str, tuple[int, int]] = {}
    for dname, dfiles in sub_dirs.items():
        dir_start = next_sector
        dir_data = bytearray()
        # dot entry
        dot = dir_record("\x00", dir_start, 0, is_dir=True)
        dir_data.extend(dot)
        dotdot = dir_record("\x01", 19, 0, is_dir=True)
        dir_data.extend(dotdot)
        for fname, fcontent in dfiles.items():
            flba = next_sector + 100 + list(dfiles.keys()).index(fname)
            dir_data.extend(dir_record(fname.upper(), flba, len(fcontent)))
        ensure(dir_start)
        sectors[dir_start][: len(dir_data)] = dir_data
        next_sector += 1

        # Write file content
        for i, (fname, fcontent) in enumerate(dfiles.items()):
            flba = dir_start + 100 + i
            ensure(flba)
            sectors[flba][: len(fcontent)] = fcontent
            next_sector = max(next_sector, flba + 1)

        sub_dir_lbas[dname] = (dir_start, len(dir_data))

    # Write root directory (sector 19)
    root_data = bytearray()
    dot = dir_record("\x00", 19, 0, is_dir=True)
    root_data.extend(dot)
    dotdot = dir_record("\x01", 19, 0, is_dir=True)
    root_data.extend(dotdot)

    file_lba_map: dict[str, int] = {}
    for fname, fcontent in top_files.items():
        flba = next_sector
        next_sector += 1
        file_lba_map[fname] = flba
        root_data.extend(dir_record(fname.upper(), flba, len(fcontent)))

    for dname, (dlba, dsize) in sub_dir_lbas.items():
        root_data.extend(dir_record(dname.upper(), dlba, dsize, is_dir=True))

    ensure(19)
    sectors[19][: len(root_data)] = root_data

    # Write top-level files
    for fname, fcontent in top_files.items():
        flba = file_lba_map[fname]
        ensure(flba)
        sectors[flba][: len(fcontent)] = fcontent

    # Build PVD at sector 16
    pvd = bytearray(SECTOR)
    pvd[:7] = b"\x01CD001\x01"
    # Root directory record at offset 156 (34 bytes)
    root_rec = bytearray(34)
    struct.pack_into("<I", root_rec, 2, 19)   # root LBA
    struct.pack_into(">I", root_rec, 6, 19)
    struct.pack_into("<I", root_rec, 10, len(root_data))
    struct.pack_into(">I", root_rec, 14, len(root_data))
    root_rec[25] = 0x02  # directory flag
    root_rec[32] = 1
    root_rec[33] = 0x00  # name = \x00 (self)
    pvd[156 : 156 + 34] = root_rec
    write_sector(16, bytes(pvd))

    # Assemble all sectors into a flat bytes object
    total = max(len(sectors), next_sector + 1)
    ensure(total - 1)
    return b"".join(bytes(s) for s in sectors)


# ---------------------------------------------------------------------------
# _normalize_ps_serial (PCSX2-compatible ExecutablePathToSerial)
# ---------------------------------------------------------------------------

def test_normalize_ps_serial_ps2_from_full_path():
    """Full BOOT2 path → canonical PCSX2 form."""
    assert _normalize_ps_serial("cdrom0:\\SLUS_203.12;1") == "SLUS-20312"


def test_normalize_ps_serial_ps2_filename_only():
    """Filename already stripped → same normalization."""
    assert _normalize_ps_serial("SLUS_203.12") == "SLUS-20312"


def test_normalize_ps_serial_ps1():
    assert _normalize_ps_serial("cdrom:\\SLPS_123.45;1") == "SLPS-12345"


def test_normalize_ps_serial_already_dash():
    """If the separator is already a dash, still normalizes correctly."""
    assert _normalize_ps_serial("SCES-503.08") == "SCES-50308"


def test_normalize_ps_serial_no_dot_rejected():
    """Serial without the canonical dot is rejected (non-standard format)."""
    assert _normalize_ps_serial("SLUS_20312") is None


def test_normalize_ps_serial_empty():
    assert _normalize_ps_serial("") is None


def test_normalize_ps_serial_garbage():
    assert _normalize_ps_serial("not_a_serial") is None


# ---------------------------------------------------------------------------
# _parse_system_cnf, now returns normalized serials
# ---------------------------------------------------------------------------

def test_parse_system_cnf_ps2():
    # Realistic PS2 SYSTEM.CNF, serial includes the canonical dot
    data = b"BOOT2 = cdrom0:\\SLUS_203.12;1\r\nVER = 1.00\r\n"
    result = _parse_system_cnf(data)
    assert result["game_id"] == "SLUS-20312"
    assert result["platform"] == "ps2"


def test_parse_system_cnf_ps2_backslash_variants():
    for line, expected in (
        (b"BOOT2=cdrom0:\\SLUS_203.12;1", "SLUS-20312"),
        (b"BOOT2 = cdrom0:/SLUS_203.12;1", "SLUS-20312"),
        (b"BOOT2=cdrom:\\SLUS_203.12;1", "SLUS-20312"),
    ):
        result = _parse_system_cnf(line)
        assert result.get("game_id") == expected, f"failed for: {line}"


def test_parse_system_cnf_ps1():
    data = b"BOOT = cdrom:\\SLUS_123.45;1\n"
    result = _parse_system_cnf(data)
    assert result["game_id"] == "SLUS-12345"
    assert result["platform"] == "ps1"


def test_parse_system_cnf_empty():
    result = _parse_system_cnf(b"VMODE = NTSC\n")
    assert result == {}


# ---------------------------------------------------------------------------
# _parse_param_sfo
# ---------------------------------------------------------------------------

def _make_param_sfo(fields: dict[str, str]) -> bytes:
    """Build a minimal PARAM.SFO binary for the given string fields."""
    # Build key table and data table
    keys = list(fields.keys())
    values = list(fields.values())

    key_table = bytearray()
    key_offsets = []
    for k in keys:
        key_offsets.append(len(key_table))
        key_table.extend(k.encode("ascii") + b"\x00")

    data_table = bytearray()
    data_offsets = []
    data_lens = []
    for v in values:
        data_offsets.append(len(data_table))
        enc = v.encode("utf-8") + b"\x00"
        data_lens.append(len(enc))
        data_table.extend(enc)

    num_entries = len(keys)
    header_size = 20
    index_size = num_entries * 16
    key_table_offset = header_size + index_size
    data_table_offset = key_table_offset + len(key_table)

    header = struct.pack(
        "<4sHHIII",
        b"\x00PSF",
        0x01,
        0x01,
        key_table_offset,
        data_table_offset,
        num_entries,
    )

    index = bytearray()
    for i, (k, v) in enumerate(zip(keys, values)):
        index.extend(
            struct.pack(
                "<HHIII",
                key_offsets[i],  # key_offset
                0x0204,          # fmt = UTF8
                data_lens[i],    # data_len
                data_lens[i],    # data_max_len
                data_offsets[i], # data_offset
            )
        )

    return header + bytes(index) + bytes(key_table) + bytes(data_table)


def test_parse_param_sfo_basic():
    sfo = _make_param_sfo({"DISC_ID": "ULES00135", "TITLE": "Patapon"})
    result = _parse_param_sfo(sfo)
    assert result["game_id"] == "ULES00135"
    assert result["title"] == "Patapon"
    assert result["platform"] == "psp"


def test_parse_param_sfo_missing_title():
    sfo = _make_param_sfo({"DISC_ID": "UCUS98744"})
    result = _parse_param_sfo(sfo)
    assert result["game_id"] == "UCUS98744"
    assert "title" not in result


def test_parse_param_sfo_bad_magic():
    result = _parse_param_sfo(b"BADD" + b"\x00" * 100)
    assert result == {}


def test_parse_param_sfo_too_short():
    result = _parse_param_sfo(b"\x00PSF")
    assert result == {}


# ---------------------------------------------------------------------------
# _parse_ipbin
# ---------------------------------------------------------------------------

def _make_ipbin(product_number: str = "MK-51034  ", title: str = "TEST GAME") -> bytes:
    data = bytearray(0x200)
    data[0x00 : 0x10] = b"SEGA SEGAKATANA "
    data[0x10 : 0x20] = b"SEGA ENTERPRISES"
    pn = product_number.ljust(10).encode("ascii")[:10]
    data[0x40 : 0x4A] = pn
    t = title.ljust(128).encode("ascii")[:128]
    data[0x80 : 0x100] = t
    return bytes(data)


def test_parse_ipbin_basic():
    data = _make_ipbin("MK-51034  ", "DEAD OR ALIVE 2 HARDCORE")
    result = _parse_ipbin(data)
    assert result["game_id"] == "MK-51034"
    assert "DEAD OR ALIVE" in result["title"]
    assert result["platform"] == "dreamcast"


def test_parse_ipbin_wrong_hwid():
    data = bytearray(_make_ipbin())
    data[0x00 : 0x10] = b"NOTADREAMCAST   "
    result = _parse_ipbin(bytes(data))
    assert result == {}


def test_parse_ipbin_too_short():
    result = _parse_ipbin(b"SEGA SEGAKATANA " + b"\x00" * 10)
    assert result == {}


# ---------------------------------------------------------------------------
# ISO extraction from a real in-memory ISO
# ---------------------------------------------------------------------------

def test_extract_iso_ps2(tmp_path):
    # Use the canonical PS2 serial format with dot (SLUS_XXX.YY)
    cnf = b"BOOT2 = cdrom0:\\SLUS_203.12;1\nVER = 1.00\n"
    iso_bytes = _make_iso({"SYSTEM.CNF": cnf})
    iso_path = tmp_path / "game.iso"
    iso_path.write_bytes(iso_bytes)
    result = _extract_iso(str(iso_path))
    assert result is not None
    assert result["game_id"] == "SLUS-20312"
    assert result["platform"] == "ps2"


def test_extract_iso_psp(tmp_path):
    sfo = _make_param_sfo({"DISC_ID": "ULES00135", "TITLE": "Patapon"})
    iso_bytes = _make_iso({"PSP_GAME/PARAM.SFO": sfo})
    iso_path = tmp_path / "game.iso"
    iso_path.write_bytes(iso_bytes)
    result = _extract_iso(str(iso_path))
    assert result is not None
    assert result["game_id"] == "ULES00135"
    assert result["title"] == "Patapon"
    assert result["platform"] == "psp"


def test_extract_iso_no_match(tmp_path):
    iso_bytes = _make_iso({"README.TXT": b"nothing here"})
    iso_path = tmp_path / "game.iso"
    iso_path.write_bytes(iso_bytes)
    result = _extract_iso(str(iso_path))
    assert result is None


def test_extract_iso_not_iso9660(tmp_path):
    iso_path = tmp_path / "game.iso"
    iso_path.write_bytes(b"\x00" * (34 * 2048))
    result = _extract_iso(str(iso_path))
    assert result is None


# ---------------------------------------------------------------------------
# extract_from_source dispatch
# ---------------------------------------------------------------------------

def test_extract_from_source_iso(tmp_path):
    cnf = b"BOOT2 = cdrom0:\\SLUS_203.12;1\n"
    iso_bytes = _make_iso({"SYSTEM.CNF": cnf})
    p = tmp_path / "game.iso"
    p.write_bytes(iso_bytes)
    result = extract_from_source(str(p))
    assert result and result["game_id"] == "SLUS-20312"


def test_extract_from_source_unknown_ext(tmp_path):
    p = tmp_path / "game.z64"
    p.write_bytes(b"\x80\x37\x12\x40" + b"\x00" * 100)
    result = extract_from_source(str(p))
    assert result is None


def _make_mode1_bin(iso_bytes: bytes) -> bytes:
    """
    Wrap a flat ISO 9660 image (2048-byte sectors) in Mode 1 2352-byte sectors.

    Each physical sector = 16-byte header + 2048-byte payload + 288-byte ECC pad.
    This produces a BIN that ``_extract_bin`` can parse (it probes for the PVD at
    sector 16 with a 16-byte offset).
    """
    SECTOR = 2352
    HEADER = 16  # Mode 1
    DATA = 2048
    TAIL = SECTOR - HEADER - DATA  # 288
    num = (len(iso_bytes) + DATA - 1) // DATA
    buf = bytearray()
    for i in range(num):
        buf += b"\x00" * HEADER
        chunk = iso_bytes[i * DATA : (i + 1) * DATA]
        buf += chunk.ljust(DATA, b"\x00")
        buf += b"\x00" * TAIL
    return bytes(buf)


# ---------------------------------------------------------------------------
# CUE extraction
# ---------------------------------------------------------------------------

def test_extract_cue_ps1(tmp_path):
    """CUE sheet pointing to a Mode 1 BIN with a PS1 SYSTEM.CNF is extracted."""
    cnf = b"BOOT = cdrom:\\SLPS_123.45;1\n"
    bin_path = tmp_path / "track01.bin"
    bin_path.write_bytes(_make_mode1_bin(_make_iso({"SYSTEM.CNF": cnf})))

    cue_path = tmp_path / "game.cue"
    cue_path.write_text(
        f'FILE "{bin_path.name}" BINARY\n'
        "  TRACK 01 MODE1/2352\n"
        "    INDEX 01 00:00:00\n"
    )

    result = _extract_cue(str(cue_path))
    assert result is not None
    assert result["game_id"] == "SLPS-12345"


def test_extract_cue_bin_missing(tmp_path):
    """CUE sheet whose BIN file is absent returns None without raising."""
    cue_path = tmp_path / "game.cue"
    cue_path.write_text('FILE "nonexistent.bin" BINARY\n  TRACK 01 MODE1/2352\n')

    result = _extract_cue(str(cue_path))
    assert result is None


def test_extract_cue_fallback_to_second_file(tmp_path):
    """When the first FILE entry is missing, the second FILE entry is tried."""
    cnf = b"BOOT = cdrom:\\SLPS_123.45;1\n"
    bin_path = tmp_path / "track02.bin"
    bin_path.write_bytes(_make_mode1_bin(_make_iso({"SYSTEM.CNF": cnf})))

    cue_path = tmp_path / "game.cue"
    cue_path.write_text(
        'FILE "nonexistent.bin" BINARY\n'
        "  TRACK 01 MODE1/2352\n"
        f'FILE "{bin_path.name}" BINARY\n'
        "  TRACK 02 MODE1/2352\n"
        "    INDEX 01 00:00:00\n"
    )

    result = _extract_cue(str(cue_path))
    assert result is not None
    assert result["game_id"] == "SLPS-12345"


def test_extract_from_source_cue(tmp_path):
    """extract_from_source dispatches to _extract_cue for .cue files."""
    cnf = b"BOOT2 = cdrom0:\\SLUS_203.12;1\n"
    bin_path = tmp_path / "track01.bin"
    bin_path.write_bytes(_make_mode1_bin(_make_iso({"SYSTEM.CNF": cnf})))

    cue_path = tmp_path / "game.cue"
    cue_path.write_text(
        f'FILE "{bin_path.name}" BINARY\n'
        "  TRACK 01 MODE1/2352\n"
        "    INDEX 01 00:00:00\n"
    )

    result = extract_from_source(str(cue_path))
    assert result is not None
    assert result["game_id"] == "SLUS-20312"


# ---------------------------------------------------------------------------
# GDI extraction
# ---------------------------------------------------------------------------

def test_extract_gdi(tmp_path):
    ipbin = _make_ipbin("T-1209N   ", "SONIC ADVENTURE")
    # Build a minimal track file (2352-byte sectors, raw header = 16 bytes)
    # IP.BIN is at physical byte offset 16 (16-byte sector header)
    track_data = b"\x00" * 16 + ipbin  # minimal: one sector worth
    track1 = tmp_path / "track01.bin"
    track1.write_bytes(track_data)
    gdi = tmp_path / "game.gdi"
    gdi.write_text(
        "3\n"
        f"1 0 4 2352 {track1.name} 0\n"
        "2 300 0 2352 track02.raw 0\n"
        "3 45000 4 2048 track03.bin 0\n"
    )
    result = _extract_gdi(str(gdi))
    assert result is not None
    assert result["game_id"] == "T-1209N"
    assert "SONIC ADVENTURE" in result["title"]
    assert result["platform"] == "dreamcast"


def test_extract_gdi_missing_track(tmp_path):
    gdi = tmp_path / "game.gdi"
    gdi.write_text("1\n1 0 4 2352 nonexistent.bin 0\n")
    result = _extract_gdi(str(gdi))
    assert result is None


# ---------------------------------------------------------------------------
# _BinSectorStream
# ---------------------------------------------------------------------------

def test_bin_sector_stream_read():
    # Build a fake 2352-byte-sector image: first 16 bytes = raw header, then 2048 bytes data
    payload = b"A" * 2048
    raw_sector = b"\x00" * 16 + payload  # sector 0
    stream = _BinSectorStream(io.BytesIO(raw_sector * 10), sector_size=2352, header_size=16)
    stream.seek(0)
    data = stream.read(2048)
    assert data == payload


def test_bin_sector_stream_cross_sector():
    # Read across two sectors
    payload0 = b"A" * 2048
    payload1 = b"B" * 2048
    raw = b"\x00" * 16 + payload0 + b"\x00" * 16 + payload1
    stream = _BinSectorStream(io.BytesIO(raw), sector_size=2352, header_size=16)
    stream.seek(2040)  # 8 bytes before end of sector 0
    data = stream.read(16)  # 8 from sector 0 + 8 from sector 1
    assert data == b"A" * 8 + b"B" * 8


def test_bin_sector_stream_sector1_read():
    """Verify that reading starting at logical sector 1 maps to the correct physical position."""
    header = b"\x00" * 16
    padding = b"\x00" * (2352 - 16 - 2048)  # trailing ECC bytes in a 2352-byte sector
    payload0 = b"A" * 2048
    payload1 = b"B" * 2048
    raw = header + payload0 + padding + header + payload1 + padding
    assert len(raw) == 2 * 2352
    stream = _BinSectorStream(io.BytesIO(raw), sector_size=2352, header_size=16)
    # Seek to start of logical sector 1 (2048 bytes in logical space)
    stream.seek(2048)
    data = stream.read(2048)
    assert data == payload1


# ---------------------------------------------------------------------------
# _CHDReader / _CHDSectorStream / _extract_from_chd_sectors
# ---------------------------------------------------------------------------

def _make_chd_v5(iso_bytes: bytes, unit_bytes: int = 2048) -> bytes:
    """
    Build a minimal CHD v5 binary with uncompressed (COMP_NONE = type 4) hunks
    wrapping the given ISO/disc data.  Used to test _CHDReader without needing
    a real CHD file.
    """
    hunk_bytes = unit_bytes  # one sector per hunk for simplicity
    # Pad to a multiple of hunk_bytes
    pad = (-len(iso_bytes)) % hunk_bytes
    padded = iso_bytes + b"\x00" * pad
    num_hunks = len(padded) // hunk_bytes

    map_offset = 124
    data_offset = map_offset + num_hunks * 12  # 12 bytes per map entry

    header = bytearray(124)
    header[:8] = b"MComprHD"
    struct.pack_into(">I", header, 8,  124)               # header_len
    struct.pack_into(">I", header, 12, 5)                 # version
    struct.pack_into(">Q", header, 16, len(padded))       # logical_bytes
    struct.pack_into(">Q", header, 24, map_offset)        # map_offset
    struct.pack_into(">Q", header, 32, 0)                 # meta_offset
    struct.pack_into(">I", header, 40, hunk_bytes)        # hunk_bytes
    struct.pack_into(">I", header, 44, unit_bytes)        # unit_bytes
    # SHA1 fields + codec fields remain zero (COMP_NONE does not use codecs)

    hunk_map = bytearray()
    for i in range(num_hunks):
        entry = bytearray(12)
        entry[0] = 4  # COMP_NONE (uncompressed)
        # file offset (6 bytes, big-endian, at bytes 4-9)
        foff = data_offset + i * hunk_bytes
        foff_be = foff.to_bytes(8, "big")
        entry[4:10] = foff_be[2:]  # 6 LSBs of the 8-byte value
        hunk_map.extend(entry)

    return bytes(header) + bytes(hunk_map) + padded


def test_chd_reader_opens_valid_chd(tmp_path):
    iso = _make_iso({"SYSTEM.CNF": b"BOOT2 = cdrom0:\\SLUS_203.12;1\n"})
    chd_bytes = _make_chd_v5(iso)
    chd_path = tmp_path / "game.chd"
    chd_path.write_bytes(chd_bytes)

    with _CHDReader(str(chd_path)) as reader:
        assert reader.open() is True
        assert reader.unit_bytes == 2048


def test_chd_reader_rejects_non_chd(tmp_path):
    p = tmp_path / "fake.chd"
    p.write_bytes(b"not a chd file" + b"\x00" * 200)
    with _CHDReader(str(p)) as reader:
        assert reader.open() is False


def test_chd_reader_read_sector(tmp_path):
    """_CHDReader.read_sector(16) should return the PVD sector bytes."""
    iso = _make_iso({"SYSTEM.CNF": b"BOOT2 = cdrom0:\\SLUS_203.12;1\n"})
    chd_bytes = _make_chd_v5(iso)
    chd_path = tmp_path / "game.chd"
    chd_path.write_bytes(chd_bytes)

    pvd_magic = b"\x01CD001\x01"
    with _CHDReader(str(chd_path)) as reader:
        assert reader.open()
        sector = reader.read_sector(16)
        assert sector is not None
        assert sector[:7] == pvd_magic


def test_chd_sector_stream_read(tmp_path):
    """_CHDSectorStream must present the same bytes as reading the raw ISO."""
    iso = _make_iso({"SYSTEM.CNF": b"BOOT2 = cdrom0:\\SLUS_203.12;1\n"})
    chd_bytes = _make_chd_v5(iso)
    chd_path = tmp_path / "game.chd"
    chd_path.write_bytes(chd_bytes)

    with _CHDReader(str(chd_path)) as reader:
        assert reader.open()
        stream = _CHDSectorStream(reader, data_offset=0)
        stream.seek(16 * 2048)
        pvd = stream.read(2048)

    assert pvd[:7] == b"\x01CD001\x01"


def test_extract_from_chd_sectors_ps2(tmp_path):
    """DVD CHD with PS2 SYSTEM.CNF → normalized serial returned."""
    cnf = b"BOOT2 = cdrom0:\\SLUS_203.12;1\n"
    iso = _make_iso({"SYSTEM.CNF": cnf})
    chd_path = tmp_path / "game.chd"
    chd_path.write_bytes(_make_chd_v5(iso))

    result = _extract_from_chd_sectors(str(chd_path))
    assert result is not None
    assert result["game_id"] == "SLUS-20312"
    assert result["platform"] == "ps2"


def test_extract_from_chd_sectors_psp(tmp_path):
    """DVD CHD with PSP PARAM.SFO → game_id extracted."""
    sfo = _make_param_sfo({"DISC_ID": "ULES-00135", "TITLE": "Patapon"})
    iso = _make_iso({"PSP_GAME/PARAM.SFO": sfo})
    chd_path = tmp_path / "game.chd"
    chd_path.write_bytes(_make_chd_v5(iso))

    result = _extract_from_chd_sectors(str(chd_path))
    assert result is not None
    assert result["game_id"] == "ULES-00135"
    assert result["title"] == "Patapon"


def test_extract_from_chd_sectors_ps1_cd_mode1(tmp_path):
    """CD CHD with PS1 SYSTEM.CNF in Mode 1 (2352-byte) sectors → serial extracted."""
    cnf = b"BOOT = cdrom:\\SLPS_123.45;1\n"
    iso = _make_iso({"SYSTEM.CNF": cnf})
    mode1_bin = _make_mode1_bin(iso)  # wraps ISO in 2352-byte Mode 1 sectors
    chd_path = tmp_path / "ps1_game.chd"
    chd_path.write_bytes(_make_chd_v5(mode1_bin, unit_bytes=2352))

    result = _extract_from_chd_sectors(str(chd_path))
    assert result is not None
    assert result["game_id"] == "SLPS-12345"
    assert result["platform"] == "ps1"


def _make_mode2_bin(iso_bytes: bytes) -> bytes:
    """
    Wrap a flat ISO 9660 image (2048-byte sectors) in Mode 2 Form 1 2352-byte
    sectors (24-byte header + 2048-byte payload + 280-byte ECC pad).

    This is the most common format for PS1 discs burned from BIN/CUE images.
    """
    SECTOR = 2352
    HEADER = _RAW_SECTOR_HEADER_SIZE_MODE2  # 24 bytes (Mode 2 Form 1)
    DATA = 2048
    TAIL = SECTOR - HEADER - DATA  # 280
    num = (len(iso_bytes) + DATA - 1) // DATA
    buf = bytearray()
    for i in range(num):
        buf += b"\x00" * HEADER
        chunk = iso_bytes[i * DATA : (i + 1) * DATA]
        buf += chunk.ljust(DATA, b"\x00")
        buf += b"\x00" * TAIL
    return bytes(buf)


def test_extract_from_chd_sectors_ps1_cd_mode2(tmp_path):
    """CD CHD with PS1 SYSTEM.CNF in Mode 2 Form 1 (2352-byte) sectors → serial extracted."""
    cnf = b"BOOT = cdrom:\\SLPS_678.90;1\n"
    iso = _make_iso({"SYSTEM.CNF": cnf})
    mode2_bin = _make_mode2_bin(iso)  # wraps ISO in 2352-byte Mode 2 Form 1 sectors
    chd_path = tmp_path / "ps1_mode2.chd"
    chd_path.write_bytes(_make_chd_v5(mode2_bin, unit_bytes=2352))

    result = _extract_from_chd_sectors(str(chd_path))
    assert result is not None
    assert result["game_id"] == "SLPS-67890"
    assert result["platform"] == "ps1"


def test_extract_from_chd_sectors_cd_no_recognizable_content(tmp_path):
    """CD CHD whose sectors contain no ISO 9660 filesystem → returns None."""
    # 2352-byte sectors filled with garbage (no PVD magic at any offset)
    garbage = b"\xAB\xCD" * (2352 * 32 // 2)
    chd_path = tmp_path / "unknown_cd.chd"
    chd_path.write_bytes(_make_chd_v5(garbage, unit_bytes=2352))

    result = _extract_from_chd_sectors(str(chd_path))
    assert result is None


def test_extract_from_chd_sectors_fake_chd_returns_none(tmp_path):
    """Non-CHD file → open() fails → returns None gracefully."""
    p = tmp_path / "game.chd"
    p.write_bytes(b"fake")
    assert _extract_from_chd_sectors(str(p)) is None


def _make_chd_v5_mini(iso_bytes: bytes) -> bytes:
    """
    Build a minimal CHD v5 binary where all hunks use COMP_MINI (type 7).
    The 8-byte fill value is stored directly in map entry bytes 4-11.
    Only useful for all-zero ISO data (fill = b'\\x00' * 8).
    """
    unit_bytes = 2048
    hunk_bytes = unit_bytes
    pad = (-len(iso_bytes)) % hunk_bytes
    padded = iso_bytes + b"\x00" * pad
    num_hunks = len(padded) // hunk_bytes

    map_offset = 124

    header = bytearray(124)
    header[:8] = b"MComprHD"
    struct.pack_into(">I", header, 8,  124)
    struct.pack_into(">I", header, 12, 5)
    struct.pack_into(">Q", header, 16, len(padded))
    struct.pack_into(">Q", header, 24, map_offset)
    struct.pack_into(">Q", header, 32, 0)
    struct.pack_into(">I", header, 40, hunk_bytes)
    struct.pack_into(">I", header, 44, unit_bytes)

    hunk_map = bytearray()
    for _ in range(num_hunks):
        entry = bytearray(12)
        entry[0] = 7          # CTYPE_MINI
        # bytes 1-3: compressed length = 0 (unused for MINI)
        # bytes 4-11: the 8-byte fill value stored inline (all zeros here)
        hunk_map.extend(entry)

    # No data section, MINI hunks store their fill value in the map entry itself
    return bytes(header) + bytes(hunk_map)


def test_chd_reader_ctype_mini_fill(tmp_path):
    """CTYPE_MINI hunks: fill value is read directly from map entry bytes 4-11."""
    # Build a CHD where every hunk is MINI-compressed with an all-zero fill.
    # The CHD sector-read should return all-zero sectors (not garbage from a bad seek).
    iso = bytearray(2048 * 20)  # 20 sectors of zeros
    chd_path = tmp_path / "mini.chd"
    chd_path.write_bytes(_make_chd_v5_mini(bytes(iso)))

    with _CHDReader(str(chd_path)) as reader:
        assert reader.open() is True
        sector = reader.read_sector(0)
        assert sector is not None
        assert sector == b"\x00" * 2048, "MINI hunk must return the fill value, not file bytes"


def test_chd_reader_rejects_oversized_hunk(tmp_path):
    """Reject crafted CHDs before MINI hunks can allocate attacker-sized buffers."""
    chd = bytearray(124 + 12)
    chd[:8] = b"MComprHD"
    struct.pack_into(">I", chd, 8, 124)
    struct.pack_into(">I", chd, 12, 5)
    struct.pack_into(">Q", chd, 24, 124)
    struct.pack_into(">I", chd, 40, _MAX_CHD_HUNK_BYTES + 1)
    struct.pack_into(">I", chd, 44, 2048)
    chd[124] = 7  # CTYPE_MINI

    chd_path = tmp_path / "oversized-mini.chd"
    chd_path.write_bytes(chd)

    with _CHDReader(str(chd_path)) as reader:
        assert reader.open() is False


def test_chd_reader_rejects_hunk_smaller_than_unit(tmp_path):
    """Reject invalid hunk/unit geometry instead of dividing by zero in read_sector."""
    chd = bytearray(124)
    chd[:8] = b"MComprHD"
    struct.pack_into(">I", chd, 8, 124)
    struct.pack_into(">I", chd, 12, 5)
    struct.pack_into(">Q", chd, 24, 124)
    struct.pack_into(">I", chd, 40, 1)
    struct.pack_into(">I", chd, 44, 2048)

    chd_path = tmp_path / "bad-geometry.chd"
    chd_path.write_bytes(chd)

    with _CHDReader(str(chd_path)) as reader:
        assert reader.open() is False


def test_chd_reader_rejects_zlib_output_larger_than_hunk(tmp_path):
    """Bound zlib decompression output to hunk_bytes."""
    payload = zlib.compress(b"A" * 4096)
    chd = bytearray(124 + 12 + len(payload))
    chd[:8] = b"MComprHD"
    struct.pack_into(">I", chd, 8, 124)
    struct.pack_into(">I", chd, 12, 5)
    struct.pack_into(">Q", chd, 24, 124)
    struct.pack_into(">I", chd, 40, 2048)
    struct.pack_into(">I", chd, 44, 2048)
    struct.pack_into(">I", chd, 108, _CHDReader._CODEC_ZLIB)
    chd[124] = 0  # CTYPE_CODEC0
    chd[125:128] = len(payload).to_bytes(4, "big")[1:]
    chd[128:134] = (136).to_bytes(8, "big")[2:]
    chd[136:] = payload

    chd_path = tmp_path / "zlib-overrun.chd"
    chd_path.write_bytes(chd)

    with _CHDReader(str(chd_path)) as reader:
        assert reader.open() is True
        assert reader.read_sector(0) is None


def test_dumpmeta_raw_times_out(tmp_path, monkeypatch):
    """chdman dumpmeta must not hang indefinitely.

    The spawn goes through the shared runner now, so the timeout is enforced by
    run_capture: it reports the abort as a None return code, which this helper
    turns into "no metadata" rather than a hang.
    """
    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake")

    class FakeProc:
        pid = 4242
        returncode = None
        killed = False

        async def communicate(self):
            await asyncio.sleep(30)
            return b"", b""

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            if self.returncode is None:
                self.returncode = -9
            return self.returncode

    proc = FakeProc()

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(
        "app.services.disc_id._DUMPMETA_TIMEOUT_SECONDS", 0.01
    )
    monkeypatch.setattr(
        "app.services.subprocess_runner.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    assert asyncio.run(_dumpmeta_raw(str(chd), TAG_GAME, "chdman")) is None
    # Proves the timeout path ran rather than the helper's catch-all: the child
    # was signalled and reaped, not left behind.
    assert proc.returncode is not None


@pytest.mark.asyncio
async def test_extract_from_chd_game_tag(tmp_path):
    """GAME tag found via dumpmeta → returned as game_id."""
    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake")

    async def fake_dumpmeta_text(chd_path, tag, chdman_path):
        if tag == TAG_GAME:
            return "SLUS_20312"
        return None

    with patch("app.services.disc_id._dumpmeta_text", side_effect=fake_dumpmeta_text):
        result = await extract_from_chd(str(chd), "chdman")

    assert result is not None
    assert result["game_id"] == "SLUS_20312"


@pytest.mark.asyncio
async def test_extract_from_chd_game_and_name_tags(tmp_path):
    """GAME + NAME tags → game_id and title both returned."""
    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake")

    async def fake_dumpmeta_text(chd_path, tag, chdman_path):
        return {"GAME": "ULES00135", "NAME": "Patapon"}.get(tag)

    with patch("app.services.disc_id._dumpmeta_text", side_effect=fake_dumpmeta_text):
        result = await extract_from_chd(str(chd), "chdman")

    assert result["game_id"] == "ULES00135"
    assert result["title"] == "Patapon"


@pytest.mark.asyncio
async def test_extract_from_chd_gdro_fallback(tmp_path):
    """No GAME tag → fall back to GDRO IP.BIN parsing."""
    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake")
    ipbin = _make_ipbin("MK-51034  ", "DEAD OR ALIVE")

    async def fake_dumpmeta_text(chd_path, tag, chdman_path):
        return None  # no GAME tag

    async def fake_dumpmeta_bin(chd_path, tag, chdman_path):
        if tag == "GDRO":
            return ipbin
        return None

    with (
        patch("app.services.disc_id._dumpmeta_text", side_effect=fake_dumpmeta_text),
        patch("app.services.disc_id._dumpmeta_bin", side_effect=fake_dumpmeta_bin),
    ):
        result = await extract_from_chd(str(chd), "chdman")

    assert result is not None
    assert result["game_id"] == "MK-51034"
    assert result["platform"] == "dreamcast"


@pytest.mark.asyncio
async def test_extract_from_chd_companion_iso_fallback(tmp_path):
    """No tags, no GDRO → look for companion ISO → normalized serial returned."""
    # Use the canonical PS2 format with the dot (SLUS_XXX.YY)
    cnf = b"BOOT2 = cdrom0:\\SLUS_209.99;1\n"
    iso_bytes = _make_iso({"SYSTEM.CNF": cnf})
    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake")
    iso = tmp_path / "game.iso"
    iso.write_bytes(iso_bytes)

    async def fake_dumpmeta_text(chd_path, tag, chdman_path):
        return None

    async def fake_dumpmeta_bin(chd_path, tag, chdman_path):
        return None

    with (
        patch("app.services.disc_id._dumpmeta_text", side_effect=fake_dumpmeta_text),
        patch("app.services.disc_id._dumpmeta_bin", side_effect=fake_dumpmeta_bin),
    ):
        result = await extract_from_chd(str(chd), "chdman")

    assert result is not None
    assert result["game_id"] == "SLUS-20999"


@pytest.mark.asyncio
async def test_extract_from_chd_none_when_nothing(tmp_path):
    """All strategies fail → None."""
    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake")

    async def fake_dumpmeta_text(chd_path, tag, chdman_path):
        return None

    async def fake_dumpmeta_bin(chd_path, tag, chdman_path):
        return None

    with (
        patch("app.services.disc_id._dumpmeta_text", side_effect=fake_dumpmeta_text),
        patch("app.services.disc_id._dumpmeta_bin", side_effect=fake_dumpmeta_bin),
    ):
        result = await extract_from_chd(str(chd), "chdman")

    assert result is None


# ---------------------------------------------------------------------------
# embed_in_chd
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_embed_in_chd_success(tmp_path):
    calls: list[tuple[str, str]] = []

    async def fake_addmeta(chd_path, tag, value, chdman_path):
        calls.append((tag, value))
        return True

    with patch("app.services.disc_id._addmeta_text", side_effect=fake_addmeta):
        ok = await embed_in_chd("/fake/game.chd", "SLUS_20312", "God of War", "chdman")

    assert ok is True
    assert any(t == TAG_GAME and v == "SLUS_20312" for t, v in calls)
    assert any(t == TAG_NAME and v == "God of War" for t, v in calls)


@pytest.mark.asyncio
async def test_embed_in_chd_no_title(tmp_path):
    calls: list[tuple[str, str]] = []

    async def fake_addmeta(chd_path, tag, value, chdman_path):
        calls.append((tag, value))
        return True

    with patch("app.services.disc_id._addmeta_text", side_effect=fake_addmeta):
        ok = await embed_in_chd("/fake/game.chd", "MK-51034", None, "chdman")

    assert ok is True
    tags_written = [t for t, _ in calls]
    assert TAG_GAME in tags_written
    assert TAG_NAME not in tags_written


@pytest.mark.asyncio
async def test_embed_in_chd_addmeta_failure():
    async def fake_addmeta(chd_path, tag, value, chdman_path):
        return False  # simulate chdman failure

    with patch("app.services.disc_id._addmeta_text", side_effect=fake_addmeta):
        ok = await embed_in_chd("/fake/game.chd", "SLUS_20312", None, "chdman")

    assert ok is False


# ---------------------------------------------------------------------------
# read_embedded_game_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_embedded_game_id_returns_trimmed_serial():
    async def fake_dump(chd_path, tag, chdman_path):
        assert tag == TAG_GAME
        return "  SLUS-20312\n"

    with patch("app.services.disc_id._dumpmeta_text", side_effect=fake_dump):
        result = await read_embedded_game_id("/fake/game.chd", "chdman")

    assert result == "SLUS-20312"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [None, "", "   \n"])
async def test_read_embedded_game_id_none_when_absent_or_blank(raw):
    async def fake_dump(chd_path, tag, chdman_path):
        return raw

    with patch("app.services.disc_id._dumpmeta_text", side_effect=fake_dump):
        assert await read_embedded_game_id("/fake/game.chd", "chdman") is None


# ---------------------------------------------------------------------------
# clear_embedded_disc_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clear_embedded_disc_id_deletes_game_and_name():
    deleted: list[str] = []

    async def fake_delmeta(chd_path, tag, chdman_path):
        deleted.append(tag)
        return True

    with patch("app.services.disc_id._delmeta", side_effect=fake_delmeta):
        await clear_embedded_disc_id("/fake/game.chd", "chdman")

    # Both GAME and NAME are stripped so a later addmeta converges on a single
    # current pair instead of appending to a stale one (chdman addmeta appends).
    assert deleted == [TAG_GAME, TAG_NAME]


# ---------------------------------------------------------------------------
# ensure_disc_id_embedded (retroactive tagging for existing CHDs)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_disc_id_embedded_already_tagged(tmp_path):
    """GAME tag already present → returns existing info, no addmeta call."""
    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake")
    addmeta_calls: list[str] = []

    async def fake_dumpmeta_text(chd_path, tag, chdman_path):
        return {"GAME": "SLUS_20312", "NAME": "God of War"}.get(tag)

    async def fake_addmeta(chd_path, tag, value, chdman_path):
        addmeta_calls.append(tag)
        return True

    with (
        patch("app.services.disc_id._dumpmeta_text", side_effect=fake_dumpmeta_text),
        patch("app.services.disc_id._addmeta_text", side_effect=fake_addmeta),
    ):
        result = await ensure_disc_id_embedded(str(chd), "chdman")

    assert result is not None
    assert result["game_id"] == "SLUS_20312"
    assert result["title"] == "God of War"
    # No addmeta calls, tag already existed
    assert addmeta_calls == []


@pytest.mark.asyncio
async def test_ensure_disc_id_embedded_gdro_fallback(tmp_path):
    """No GAME tag, but GDRO present → embeds serial as both GAME and NAME."""
    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake")
    ipbin = _make_ipbin("MK-51034  ", "DEAD OR ALIVE")
    addmeta_calls: list[tuple[str, str]] = []

    async def fake_dumpmeta_text(chd_path, tag, chdman_path):
        return None  # no GAME tag yet

    async def fake_dumpmeta_bin(chd_path, tag, chdman_path):
        return ipbin if tag == "GDRO" else None

    async def fake_addmeta(chd_path, tag, value, chdman_path):
        addmeta_calls.append((tag, value))
        return True

    with (
        patch("app.services.disc_id._dumpmeta_text", side_effect=fake_dumpmeta_text),
        patch("app.services.disc_id._dumpmeta_bin", side_effect=fake_dumpmeta_bin),
        patch("app.services.disc_id._addmeta_text", side_effect=fake_addmeta),
    ):
        result = await ensure_disc_id_embedded(str(chd), "chdman")

    assert result is not None
    assert result["game_id"] == "MK-51034"
    # Human-readable title from IP.BIN is used as the NAME tag when available
    assert any(t == TAG_GAME and "MK-51034" in v for t, v in addmeta_calls)
    assert any(t == TAG_NAME and "DEAD OR ALIVE" in v for t, v in addmeta_calls)


@pytest.mark.asyncio
async def test_ensure_disc_id_embedded_gdro_embed_failure(tmp_path):
    """embed_in_chd failure (addmeta error) → disc ID still returned for caching, not None."""
    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake")
    ipbin = _make_ipbin("MK-51034  ", "DEAD OR ALIVE")

    async def fake_dumpmeta_text(chd_path, tag, chdman_path):
        return None

    async def fake_dumpmeta_bin(chd_path, tag, chdman_path):
        return ipbin if tag == "GDRO" else None

    async def fake_addmeta(chd_path, tag, value, chdman_path):
        return False  # simulate chdman failure

    with (
        patch("app.services.disc_id._dumpmeta_text", side_effect=fake_dumpmeta_text),
        patch("app.services.disc_id._dumpmeta_bin", side_effect=fake_dumpmeta_bin),
        patch("app.services.disc_id._addmeta_text", side_effect=fake_addmeta),
    ):
        result = await ensure_disc_id_embedded(str(chd), "chdman")

    # Even though embedding failed, the extracted disc ID is returned so it
    # can be cached in the metadata store and displayed in the UI.
    assert result is not None
    assert result["game_id"] == "MK-51034"


@pytest.mark.asyncio
async def test_ensure_disc_id_embedded_companion_iso(tmp_path):
    """No GAME tag, no GDRO, companion ISO present → embeds normalized serial as GAME and NAME."""
    # Use the canonical PS2 format with the dot (SLUS_XXX.YY)
    cnf = b"BOOT2 = cdrom0:\\SLUS_209.99;1\n"
    iso_bytes = _make_iso({"SYSTEM.CNF": cnf})
    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake")
    (tmp_path / "game.iso").write_bytes(iso_bytes)
    addmeta_calls: list[tuple[str, str]] = []

    async def fake_dumpmeta_text(chd_path, tag, chdman_path):
        return None

    async def fake_dumpmeta_bin(chd_path, tag, chdman_path):
        return None

    async def fake_addmeta(chd_path, tag, value, chdman_path):
        addmeta_calls.append((tag, value))
        return True

    with (
        patch("app.services.disc_id._dumpmeta_text", side_effect=fake_dumpmeta_text),
        patch("app.services.disc_id._dumpmeta_bin", side_effect=fake_dumpmeta_bin),
        patch("app.services.disc_id._addmeta_text", side_effect=fake_addmeta),
    ):
        result = await ensure_disc_id_embedded(str(chd), "chdman")

    assert result is not None
    assert result["game_id"] == "SLUS-20999"
    assert any(t == TAG_GAME and v == "SLUS-20999" for t, v in addmeta_calls)
    # Serial used as the NAME (title) tag for emulator lookup
    assert any(t == TAG_NAME and v == "SLUS-20999" for t, v in addmeta_calls)


@pytest.mark.asyncio
async def test_ensure_disc_id_embedded_companion_embed_failure(tmp_path):
    """embed_in_chd failure for companion file → disc ID still returned for caching."""
    cnf = b"BOOT2 = cdrom0:\\SLUS_209.99;1\n"
    iso_bytes = _make_iso({"SYSTEM.CNF": cnf})
    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake")
    (tmp_path / "game.iso").write_bytes(iso_bytes)

    async def fake_dumpmeta_text(chd_path, tag, chdman_path):
        return None

    async def fake_dumpmeta_bin(chd_path, tag, chdman_path):
        return None

    async def fake_addmeta(chd_path, tag, value, chdman_path):
        return False  # simulate chdman failure

    with (
        patch("app.services.disc_id._dumpmeta_text", side_effect=fake_dumpmeta_text),
        patch("app.services.disc_id._dumpmeta_bin", side_effect=fake_dumpmeta_bin),
        patch("app.services.disc_id._addmeta_text", side_effect=fake_addmeta),
    ):
        result = await ensure_disc_id_embedded(str(chd), "chdman")

    # Embedding failed but the extracted serial is still returned so callers
    # can cache it in the metadata store and show it in the UI.
    assert result is not None
    assert result["game_id"] == "SLUS-20999"


@pytest.mark.asyncio
async def test_ensure_disc_id_embedded_nothing_found(tmp_path):
    """No GAME tag, no GDRO, no companion file → returns None, no addmeta."""
    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake")
    addmeta_calls: list[str] = []

    async def fake_dumpmeta_text(chd_path, tag, chdman_path):
        return None

    async def fake_dumpmeta_bin(chd_path, tag, chdman_path):
        return None

    async def fake_addmeta(chd_path, tag, value, chdman_path):
        addmeta_calls.append(tag)
        return True

    with (
        patch("app.services.disc_id._dumpmeta_text", side_effect=fake_dumpmeta_text),
        patch("app.services.disc_id._dumpmeta_bin", side_effect=fake_dumpmeta_bin),
        patch("app.services.disc_id._addmeta_text", side_effect=fake_addmeta),
    ):
        result = await ensure_disc_id_embedded(str(chd), "chdman")

    assert result is None
    assert addmeta_calls == []


@pytest.mark.asyncio
async def test_ensure_disc_id_embedded_from_chd_sectors(tmp_path):
    """No GAME tag → CHD sector reading finds SYSTEM.CNF → embeds normalized serial."""
    cnf = b"BOOT2 = cdrom0:\\SLUS_203.12;1\n"
    iso = _make_iso({"SYSTEM.CNF": cnf})
    chd = tmp_path / "game.chd"
    chd.write_bytes(_make_chd_v5(iso))
    addmeta_calls: list[tuple[str, str]] = []

    async def fake_dumpmeta_text(chd_path, tag, chdman_path):
        return None  # no pre-existing GAME tag

    async def fake_dumpmeta_bin(chd_path, tag, chdman_path):
        return None

    async def fake_addmeta(chd_path, tag, value, chdman_path):
        addmeta_calls.append((tag, value))
        return True

    with (
        patch("app.services.disc_id._dumpmeta_text", side_effect=fake_dumpmeta_text),
        patch("app.services.disc_id._dumpmeta_bin", side_effect=fake_dumpmeta_bin),
        patch("app.services.disc_id._addmeta_text", side_effect=fake_addmeta),
    ):
        result = await ensure_disc_id_embedded(str(chd), "chdman")

    assert result is not None
    assert result["game_id"] == "SLUS-20312"
    assert any(t == TAG_GAME and v == "SLUS-20312" for t, v in addmeta_calls)
    assert any(t == TAG_NAME and v == "SLUS-20312" for t, v in addmeta_calls)


@pytest.mark.asyncio
async def test_ensure_disc_id_embedded_sector_embed_failure(tmp_path):
    """Sector extraction finds disc ID but embed_in_chd fails → disc ID still returned.

    This covers the case where the CHD is on a read-only filesystem or chdman
    addmeta is unavailable.  The extracted game_id must still be returned so
    Phase 2 can cache it in the metadata store for display in the UI.
    """
    cnf = b"BOOT2 = cdrom0:\\SLUS_203.12;1\n"
    iso = _make_iso({"SYSTEM.CNF": cnf})
    chd = tmp_path / "game.chd"
    chd.write_bytes(_make_chd_v5(iso))

    async def fake_dumpmeta_text(chd_path, tag, chdman_path):
        return None  # no pre-existing GAME tag

    async def fake_dumpmeta_bin(chd_path, tag, chdman_path):
        return None

    async def fake_addmeta(chd_path, tag, value, chdman_path):
        return False  # simulate chdman / filesystem failure

    with (
        patch("app.services.disc_id._dumpmeta_text", side_effect=fake_dumpmeta_text),
        patch("app.services.disc_id._dumpmeta_bin", side_effect=fake_dumpmeta_bin),
        patch("app.services.disc_id._addmeta_text", side_effect=fake_addmeta),
    ):
        result = await ensure_disc_id_embedded(str(chd), "chdman")

    # The game_id is still returned even though embedding failed, so it can
    # be cached in the metadata store and shown in the CHD inspector.
    assert result is not None
    assert result["game_id"] == "SLUS-20312"


@pytest.mark.asyncio
async def test_ensure_disc_id_embedded_psp_companion(tmp_path):
    """PSP companion ISO → serial used as GAME and NAME (title) for emulator lookup."""
    sfo = _make_param_sfo({"DISC_ID": "ULES00135", "TITLE": "Patapon"})
    iso_bytes = _make_iso({"PSP_GAME/PARAM.SFO": sfo})
    chd = tmp_path / "patapon.chd"
    chd.write_bytes(b"fake")
    (tmp_path / "patapon.iso").write_bytes(iso_bytes)
    addmeta_calls: list[tuple[str, str]] = []

    async def fake_dumpmeta_text(chd_path, tag, chdman_path):
        return None

    async def fake_dumpmeta_bin(chd_path, tag, chdman_path):
        return None

    async def fake_addmeta(chd_path, tag, value, chdman_path):
        addmeta_calls.append((tag, value))
        return True

    with (
        patch("app.services.disc_id._dumpmeta_text", side_effect=fake_dumpmeta_text),
        patch("app.services.disc_id._dumpmeta_bin", side_effect=fake_dumpmeta_bin),
        patch("app.services.disc_id._addmeta_text", side_effect=fake_addmeta),
    ):
        result = await ensure_disc_id_embedded(str(chd), "chdman")

    assert result is not None
    assert result["game_id"] == "ULES00135"
    assert any(t == TAG_GAME and v == "ULES00135" for t, v in addmeta_calls)
    # Human-readable title "Patapon" from PARAM.SFO is used as the NAME tag
    assert any(t == TAG_NAME and v == "Patapon" for t, v in addmeta_calls)
