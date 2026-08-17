"""
Disc ID / Title extractor for game disc images.

Supports extracting game serial numbers and titles from:
  - CHD files: reads disc sectors directly via a minimal CHD v5 reader
    (the same mechanism as libchdr used by PCSX2 / AetherSX2 / NetherSX2)
  - ISO 9660 source files (.iso) used for PS2, PSP, and PS1
  - Dreamcast GDI images (.gdi + track files)
  - CHD files via chdman dumpmeta (reads embedded GAME/NAME tags or GDRO IP.BIN)

Serial normalization matches PCSX2's ExecutablePathToSerial:
  SYSTEM.CNF "BOOT2 = cdrom0:\\SLUS_203.12;1"  →  "SLUS-20312"
Emulator frontends and game database scrapers key on this normalized form.

Extraction strategy for CHDs (in priority order):
  1. Read our embedded GAME / NAME tags written by addmeta at conversion time.
  2. Read disc sectors directly from the CHD (via a minimal CHD v5 reader):
       DVD CHDs (unit_bytes=2048): plain 2048-byte ISO sectors.
       CD CHDs (unit_bytes=2352/2448): Mode 1 and Mode 2 Form 1 framing are
       both probed automatically, so PS1 BIN/CUE-sourced CHDs are supported.
  3. For CD CHDs: parse the Dreamcast GDRO (IP.BIN) metadata standard tag.
  4. Look for a companion source file (.iso / .gdi / .cue / .bin) next to the CHD.
  5. Return None if nothing is found.

Source-file extraction:
  - PS2 / PS1 .iso: ISO 9660 → root/SYSTEM.CNF → BOOT2= / BOOT= line.
  - PSP .iso:       ISO 9660 → /PSP_GAME/PARAM.SFO → DISC_ID + TITLE keys.
  - Dreamcast .gdi: reads Track01.bin IP.BIN header (product number + title).

Retroactive tagging (ensure_disc_id_embedded):
  Existing CHDs that were created before conversion-time tagging was added can
  be back-filled with GAME / NAME tags by calling ensure_disc_id_embedded().
  It checks for an existing GAME tag first (fast, no modification) and only
  runs disc-sector / GDRO / companion-file extraction + chdman addmeta when
  the tag is absent.  This is called automatically during every metadata scan.
"""

from __future__ import annotations

import asyncio
import lzma as _lzma
import logging
from logging_setup import get_logger
import os
import re
import struct
import tempfile
import zlib as _zlib
from pathlib import Path
from typing import Optional

from services.subprocess_runner import reraise_if_abandoned

logger = get_logger("disc_id")

# ---------------------------------------------------------------------------
# CHD addmeta / dumpmeta tag names used by this application
# ---------------------------------------------------------------------------
TAG_GAME = "GAME"  # game / disc serial number (text)
TAG_NAME = "NAME"  # game / disc title         (text)

# Resource limits for CHD metadata/disc-ID probing. CHD files live in user
# volumes, so treat header/map fields and chdman output as untrusted input.
_MAX_CHD_HUNK_BYTES = 8 * 1024 * 1024
_MAX_CHD_COMPRESSED_BYTES = 8 * 1024 * 1024
_MAX_CHD_LZMA_DICT_BYTES = 16 * 1024 * 1024
_MAX_DUMPMETA_BYTES = 1 * 1024 * 1024
_DUMPMETA_TIMEOUT_SECONDS = 15


def _chdman_runner():
    """The chdman service's ``SubprocessRunner``, imported lazily.

    These helpers shell out to chdman, so they belong on that tool's runner: one
    PID set ``active_pids()` fully describes, one nice/ionice policy, and one
    bounded teardown ladder instead of three hand-rolled spawns whose waits after
    ``kill()`` were unbounded -- which could hang the library scan's Phase 2 on
    unresponsive storage. The import is deferred to keep this module free of a
    load-order dependency on the service singletons.
    """
    from services.chdman import chdman_service
    return chdman_service.runner

# ---------------------------------------------------------------------------
# ISO 9660 constants
# ---------------------------------------------------------------------------
_SECTOR_SIZE = 2048
_PVD_SECTOR = 16
_PVD_MAGIC = b"\x01CD001\x01"

# Raw CD sector header sizes (bytes prepended before user-data in .bin files)
_RAW_SECTOR_HEADER_SIZE_MODE1 = 16   # Mode 1 / CD-ROM XA sector header
_RAW_SECTOR_HEADER_SIZE_MODE2 = 24   # Mode 2 Form 1 sector header

# ---------------------------------------------------------------------------
# Regex patterns for SYSTEM.CNF parsing
# ---------------------------------------------------------------------------
_BOOT2_RE = re.compile(
    r"BOOT2\s*=\s*cdrom0?[:\\\/]+([A-Z0-9_.]+)",
    re.IGNORECASE,
)
_BOOT_RE = re.compile(
    r"BOOT\s*=\s*cdrom[:\\\/]+([A-Z0-9_.]+)",
    re.IGNORECASE,
)

# PS1/PS2 disc serial pattern: 4 letters + _ or - + 3 digits + . + 2 digits
# Matches the format validated by PCSX2's ExecutablePathToSerial.
_PS_SERIAL_RE = re.compile(r"^[A-Z]{4}[_-][0-9]{3}\.[0-9]{2}", re.ASCII)

# ---------------------------------------------------------------------------
# SFO (PARAM.SFO) constants
# ---------------------------------------------------------------------------
_SFO_MAGIC = b"\x00PSF"
_SFO_FMT_UTF8 = 0x0204

# ---------------------------------------------------------------------------
# Dreamcast IP.BIN offsets (all values ASCII, null/space-padded)
# Based on the official Dreamcast Bootstrap Specification:
#   Hardware ID   : 0x00 – 0x0F  (16 bytes, "SEGA SEGAKATANA ")
#   Maker ID      : 0x10 – 0x1F  (16 bytes, "SEGA ENTERPRISES")
#   Product Number: 0x40 – 0x49  (10 bytes, e.g. "MK-51034  ")
#   Version       : 0x4A – 0x4F  ( 6 bytes, e.g. "V1.001")
#   Release Date  : 0x50 – 0x57  ( 8 bytes, "20011115")
#   Publisher     : 0x60 – 0x6F  (16 bytes)
#   Game Title    : 0x80 – 0xFF  (128 bytes)
# ---------------------------------------------------------------------------
_DC_HWID_OFF = 0x00
_DC_HWID_LEN = 16
_DC_PROD_OFF = 0x40
_DC_PROD_LEN = 10
_DC_VER_OFF = 0x4A
_DC_VER_LEN = 6
_DC_TITLE_OFF = 0x80
_DC_TITLE_LEN = 128
_DC_HWID_NEEDLE = b"SEGA SEGAKATANA"


# ===========================================================================
# Low-level ISO 9660 helpers
# ===========================================================================

def _read_pvd(f) -> Optional[bytes]:
    """Return the 2048-byte PVD sector from a seekable ISO 9660 stream, or None."""
    try:
        f.seek(_PVD_SECTOR * _SECTOR_SIZE)
        pvd = f.read(_SECTOR_SIZE)
    except OSError:
        return None
    if len(pvd) < 190 or pvd[:7] != _PVD_MAGIC:
        return None
    return pvd


def _pvd_root_dir(pvd: bytes) -> tuple[int, int]:
    """Return (lba, size) of the root directory from a PVD."""
    root = pvd[156:190]
    lba = struct.unpack_from("<I", root, 2)[0]
    size = struct.unpack_from("<I", root, 10)[0]
    return lba, size


def _list_dir(f, lba: int, size: int) -> list[tuple[str, int, int, bool]]:
    """
    Yield (name, lba, size, is_dir) for each entry in an ISO 9660 directory.
    Skips the '.' and '..' self-referencing entries.
    """
    try:
        f.seek(lba * _SECTOR_SIZE)
        data = f.read(size)
    except OSError:
        return []

    entries: list[tuple[str, int, int, bool]] = []
    pos = 0
    while pos < len(data):
        rec_len = data[pos]
        if rec_len == 0:
            # Advance to next sector boundary
            next_pos = ((pos // _SECTOR_SIZE) + 1) * _SECTOR_SIZE
            if next_pos >= len(data):
                break
            pos = next_pos
            continue
        if pos + rec_len > len(data):
            break

        rec = data[pos : pos + rec_len]
        if len(rec) < 34:
            pos += rec_len
            continue

        flags = rec[25]
        name_len = rec[32]
        if 33 + name_len > len(rec):
            pos += rec_len
            continue

        name_raw = rec[33 : 33 + name_len]
        if name_raw not in (b"\x00", b"\x01"):
            try:
                name = name_raw.decode("ascii", errors="replace").upper()
                if ";" in name:
                    name = name[: name.index(";")]
                entry_lba = struct.unpack_from("<I", rec, 2)[0]
                entry_size = struct.unpack_from("<I", rec, 10)[0]
                is_dir = bool(flags & 0x02)
                entries.append((name, entry_lba, entry_size, is_dir))
            except Exception:
                logger.debug(
                    "Skipping malformed ISO 9660 directory entry at offset %d",
                    pos,
                    exc_info=True,
                )

        pos += rec_len
    return entries


def read_iso_file(iso_path: str, path_parts: list[str]) -> Optional[bytes]:
    """Read one file's bytes from an ISO 9660 image by path, or ``None``.

    The shared, format-only ISO 9660 path lookup: open the image, walk the PVD
    root directory to ``path_parts`` (e.g. ``["PS3_GAME", "PARAM.SFO"]``), and
    return the file bytes. Callers that need a specific embedded file from a
    built ISO (PS3 ``TITLE_ID`` readback in ``services.ps3``) reuse this rather
    than re-implementing the ISO 9660 walk, per the repo's "modularity is key"
    rule. Returns ``None`` for a non-ISO / unreadable image or a missing path.
    """
    try:
        with open(iso_path, "rb") as f:
            pvd = _read_pvd(f)
            if not pvd:
                return None
            root_lba, root_size = _pvd_root_dir(pvd)
            return _find_file(f, root_lba, root_size, path_parts)
    except OSError:
        return None


def _find_file(f, root_lba: int, root_size: int, path_parts: list[str]) -> Optional[bytes]:
    """
    Walk an ISO 9660 directory tree and return the raw bytes of the file at
    *path_parts* (e.g. ["PSP_GAME", "PARAM.SFO"]), or None if not found.
    """
    cur_lba, cur_size = root_lba, root_size
    for i, part in enumerate(path_parts):
        entries = _list_dir(f, cur_lba, cur_size)
        hit = next((e for e in entries if e[0] == part.upper()), None)
        if hit is None:
            return None
        _name, lba, size, is_dir = hit
        if i == len(path_parts) - 1:
            # Final component, read and return the file
            try:
                f.seek(lba * _SECTOR_SIZE)
                return f.read(size)
            except OSError:
                return None
        if not is_dir:
            return None
        cur_lba, cur_size = lba, size
    return None


# ===========================================================================
# Format-specific parsers
# ===========================================================================

def _normalize_ps_serial(raw: str) -> Optional[str]:
    """
    Normalize a PlayStation disc serial to the canonical form used by PCSX2,
    AetherSX2, and NetherSX2.

    Mirrors PCSX2's ``ExecutablePathToSerial`` function:
      1. Strip the path prefix up to and including the last \\ (or : if no \\).
      2. Strip the ;N version suffix.
      3. Validate against the PS1/PS2 serial pattern:
         4 letters + (_ or -) + 3 digits + . + 2 digits.
      4. Remove the dot, replace _ with -.

    Examples::
      cdrom0:\\SLUS_203.12;1  →  SLUS-20312
      SCES_503.08             →  SCES-50308
      SLPS_123.45;2           →  SLPS-12345
      SLUS_20312              →  None  (no dot, non-standard, rejected)
    """
    s = raw.strip().upper()
    # Strip path prefix (everything up to and including the last \\ or :)
    bslash = s.rfind("\\")
    if bslash >= 0:
        s = s[bslash + 1:]
    else:
        colon = s.rfind(":")
        if colon >= 0:
            s = s[colon + 1:]
    # Strip ;N version suffix
    semi = s.find(";")
    if semi >= 0:
        s = s[:semi]
    s = s.strip()
    # Validate: must match the PS1/PS2 serial format ????_???.??
    if not _PS_SERIAL_RE.match(s):
        return None
    # Remove the dot, replace _ with -
    return s.replace(".", "").replace("_", "-")


def _parse_system_cnf(data: bytes) -> dict:
    """Parse SYSTEM.CNF and return a dict with game_id and platform."""
    try:
        text = data.decode("ascii", errors="replace")
    except Exception:
        return {}
    # PS2
    m = _BOOT2_RE.search(text)
    if m:
        normalized = _normalize_ps_serial(m.group(1))
        if normalized:
            return {"game_id": normalized, "platform": "ps2"}
    # PS1
    m = _BOOT_RE.search(text)
    if m:
        normalized = _normalize_ps_serial(m.group(1))
        if normalized:
            return {"game_id": normalized, "platform": "ps1"}
    return {}


def parse_sfo_keys(data: bytes) -> dict[str, str]:
    """Return every UTF-8 string key of a PSF (PARAM.SFO) blob.

    The shared, format-only PARAM.SFO table reader. Both the PSP/CHD tagging
    path (``_parse_param_sfo`` below) and PS3 folder detection
    (``services.ps3``) build on this, so the binary table parsing lives in one
    place (per the repo's "modularity is key" rule) rather than being
    copy-pasted per platform.

    SFO format (little-endian):
      0x00  4   magic "\\x00PSF"
      0x08  4   key_table_offset
      0x0C  4   data_table_offset
      0x10  4   num_entries
    Then num_entries x 16-byte index entries:
      0x00  2   key_offset (from key_table_offset)
      0x02  2   data_format  (0x0204 = utf8 string)
      0x04  4   data_len
      0x0C  4   data_offset (from data_table_offset)
    """
    if len(data) < 20 or data[:4] != _SFO_MAGIC:
        return {}
    try:
        key_table_off = struct.unpack_from("<I", data, 8)[0]
        data_table_off = struct.unpack_from("<I", data, 12)[0]
        num_entries = struct.unpack_from("<I", data, 16)[0]
    except struct.error:
        return {}

    out: dict[str, str] = {}
    for i in range(num_entries):
        entry_off = 20 + i * 16
        if entry_off + 16 > len(data):
            break
        key_off = struct.unpack_from("<H", data, entry_off)[0]
        fmt = struct.unpack_from("<H", data, entry_off + 2)[0]
        data_len = struct.unpack_from("<I", data, entry_off + 4)[0]
        val_off = struct.unpack_from("<I", data, entry_off + 12)[0]

        key_start = key_table_off + key_off
        key_end = data.find(b"\x00", key_start)
        if key_end < 0:
            continue
        key = data[key_start:key_end].decode("ascii", errors="replace")
        if fmt != _SFO_FMT_UTF8:
            continue
        val_start = data_table_off + val_off
        val_end = val_start + data_len
        if val_end > len(data):
            continue
        out[key] = data[val_start:val_end].rstrip(b"\x00").decode(
            "utf-8", errors="replace",
        )
    return out


def _parse_param_sfo(data: bytes) -> dict:
    """Parse a PSP PARAM.SFO blob into game_id, title, platform."""
    keys = parse_sfo_keys(data)
    result: dict = {}
    if "DISC_ID" in keys:
        result["game_id"] = keys["DISC_ID"]
    if "TITLE" in keys:
        result["title"] = keys["TITLE"]
    if result:
        result["platform"] = "psp"
    return result


def _parse_ipbin(data: bytes) -> dict:
    """
    Parse a Dreamcast IP.BIN binary (from the GDRO CHD metadata tag or from
    the first track of a GDI image) and return game_id, title, platform.
    """
    if len(data) < _DC_PROD_OFF + _DC_PROD_LEN:
        return {}
    try:
        hwid = data[_DC_HWID_OFF : _DC_HWID_OFF + _DC_HWID_LEN]
        if _DC_HWID_NEEDLE not in hwid:
            return {}
        prod_raw = data[_DC_PROD_OFF : _DC_PROD_OFF + _DC_PROD_LEN]
        game_id = prod_raw.decode("ascii", errors="replace").strip()

        title = ""
        if len(data) >= _DC_TITLE_OFF + _DC_TITLE_LEN:
            title_raw = data[_DC_TITLE_OFF : _DC_TITLE_OFF + _DC_TITLE_LEN]
            title = title_raw.decode("ascii", errors="replace").strip()

        result: dict = {}
        if game_id:
            result["game_id"] = game_id
        if title:
            result["title"] = title
        if result:
            result["platform"] = "dreamcast"
        return result
    except Exception:
        return {}


# ===========================================================================
# Public synchronous extractor: source files
# ===========================================================================

def extract_from_source(path: str) -> Optional[dict]:
    """
    Extract game ID (and optionally title) from a source disc image file.

    Supported formats:
      .iso  → PS2 (SYSTEM.CNF/BOOT2), PS1 (SYSTEM.CNF/BOOT),
               PSP (PSP_GAME/PARAM.SFO)
      .gdi  → Dreamcast (reads the first data track for IP.BIN)
      .bin  → Tries ISO 9660 with 2352-byte sector layout (Mode 2 / raw)
      .cue  → Follows the DATA track .bin reference

    Returns a dict with at least ``game_id`` and ``platform`` keys, or None.
    """
    ext = Path(path).suffix.lower()
    if ext == ".iso":
        return _extract_iso(path)
    if ext == ".gdi":
        return _extract_gdi(path)
    if ext == ".cue":
        return _extract_cue(path)
    if ext == ".bin":
        return _extract_bin(path)
    return None


def _extract_iso(path: str) -> Optional[dict]:
    """ISO 9660 extraction (2048-byte sectors, PS2, PSP, PS1 DVD-based)."""
    try:
        with open(path, "rb") as f:
            pvd = _read_pvd(f)
            if not pvd:
                return None
            root_lba, root_size = _pvd_root_dir(pvd)

            # PS2 / PS1, SYSTEM.CNF in root
            cnf = _find_file(f, root_lba, root_size, ["SYSTEM.CNF"])
            if cnf:
                result = _parse_system_cnf(cnf)
                if result.get("game_id"):
                    return result

            # PSP, /PSP_GAME/PARAM.SFO
            sfo = _find_file(f, root_lba, root_size, ["PSP_GAME", "PARAM.SFO"])
            if sfo:
                result = _parse_param_sfo(sfo)
                if result.get("game_id"):
                    return result

    except Exception as e:
        logger.debug("disc_id: ISO extraction failed for %s: %s", path, e)
    return None


def _extract_bin(path: str) -> Optional[dict]:
    """
    Try to extract from a raw BIN file.
    Dreamcast / PS1 discs use 2352-byte sectors (Mode 1/2).  The user-data
    for a Mode 1 sector starts at byte 16; for Mode 2 Form 1 at byte 24.
    We try the ISO 9660 PVD by probing both offsets.
    """
    try:
        with open(path, "rb") as f:
            # Try Mode 2 XA (24-byte header) then Mode 1 (16-byte header)
            for header_size in (_RAW_SECTOR_HEADER_SIZE_MODE2, _RAW_SECTOR_HEADER_SIZE_MODE1):
                sector_size = 2352
                pvd_offset = _PVD_SECTOR * sector_size + header_size
                try:
                    f.seek(pvd_offset)
                    pvd_candidate = f.read(_SECTOR_SIZE)
                except OSError:
                    continue
                if len(pvd_candidate) >= 7 and pvd_candidate[:7] == _PVD_MAGIC:
                    # Wrap as a virtual seekable stream that maps logical sectors
                    # to physical sectors with the given header size.
                    stream = _BinSectorStream(f, sector_size, header_size)
                    pvd = _read_pvd(stream)
                    if pvd:
                        root_lba, root_size = _pvd_root_dir(pvd)
                        cnf = _find_file(stream, root_lba, root_size, ["SYSTEM.CNF"])
                        if cnf:
                            result = _parse_system_cnf(cnf)
                            if result.get("game_id"):
                                return result
    except Exception as e:
        logger.debug("disc_id: BIN extraction failed for %s: %s", path, e)
    return None


class _BinSectorStream:
    """
    Adapts a raw-sector BIN file (2352-byte physical sectors) to look like a
    2048-byte logical ISO 9660 stream so the standard _read_pvd / _find_file
    helpers can operate on it transparently.
    """

    def __init__(self, f, sector_size: int = 2352, header_size: int = 24):
        self._f = f
        self._sector_size = sector_size
        self._header_size = header_size
        self._pos = 0  # logical position

    def seek(self, pos: int) -> None:
        self._pos = pos

    def read(self, size: int) -> bytes:
        result = bytearray()
        remaining = size
        pos = self._pos
        while remaining > 0:
            logical_sector = pos // _SECTOR_SIZE
            offset_in_sector = pos % _SECTOR_SIZE
            physical_offset = (
                logical_sector * self._sector_size
                + self._header_size
                + offset_in_sector
            )
            chunk = min(remaining, _SECTOR_SIZE - offset_in_sector)
            try:
                self._f.seek(physical_offset)
                data = self._f.read(chunk)
            except OSError:
                break
            if not data:
                break
            result.extend(data)
            pos += len(data)
            remaining -= len(data)
        self._pos = pos
        return bytes(result)


# ===========================================================================
# CHD v5 direct disc-sector reader
# ===========================================================================
# Implements the same low-level sector reading as libchdr, the C library used
# by PCSX2, AetherSX2, NetherSX2, and Argosy Launcher, so we can extract
# SYSTEM.CNF from the CHD's own disc content without a full extraction pass.

class _CHDReader:
    """
    Minimal CHD v5 reader.

    Reads individual disc sectors from a CHD file using the same hunk-map
    lookup and decompression mechanism as libchdr.

    Supported compression codecs:
      • ZLIB (0x7A6C6962), common for older CHD files
      • LZMA (0x6C7A6D61), default for ``chdman createdvd`` on modern MAME
      • Uncompressed (hunk compression type 4)

    CHD v5 uses big-endian byte order for all multi-byte header and map fields.
    """

    _MAGIC = b"MComprHD"
    _HEADER_SIZE = 124
    _VERSION = 5
    _MAP_ENTRY_SIZE = 12   # bytes per hunk-map entry

    # Hunk compression types (byte 0 of each 12-byte map entry)
    _CTYPE_CODEC0 = 0    # compressed with codec[0]
    _CTYPE_CODEC1 = 1    # compressed with codec[1]
    _CTYPE_CODEC2 = 2    # compressed with codec[2]
    _CTYPE_CODEC3 = 3    # compressed with codec[3]
    _CTYPE_NONE   = 4    # uncompressed (raw hunk stored verbatim)
    _CTYPE_SELF   = 5    # back-reference to an earlier hunk (not implemented)
    _CTYPE_PARENT = 6    # reference to parent CHD           (not implemented)
    _CTYPE_MINI   = 7    # 8-byte value replicated to fill the hunk

    # Compression codec 4-char tags stored as big-endian uint32 in the header
    _CODEC_ZLIB = 0x7A6C6962   # b'zlib'
    _CODEC_LZMA = 0x6C7A6D61   # b'lzma'
    _CODEC_ZSTD = 0x7A737464   # b'zstd'

    def __init__(self, path: str) -> None:
        self._path = path
        self._f = None
        self._hunk_bytes = 0
        self._unit_bytes = 0
        self._map_offset = 0
        self._codecs: list[int] = [0, 0, 0, 0]
        self._cache: dict[int, bytes] = {}

    @property
    def unit_bytes(self) -> int:
        """Bytes per disc unit (sector): 2048 for DVD, 2352/2448 for CD."""
        return self._unit_bytes

    def open(self) -> bool:
        """Open and validate the CHD v5 header.  Returns False on failure."""
        try:
            self._f = open(self._path, "rb")
            hdr = self._f.read(self._HEADER_SIZE)
            if len(hdr) < self._HEADER_SIZE or hdr[:8] != self._MAGIC:
                logger.debug(
                    "disc_id: CHD open failed, bad magic or short header in %s",
                    self._path,
                )
                return False
            # All multi-byte fields are big-endian in CHD v5
            hdr_len = struct.unpack_from(">I", hdr, 8)[0]
            version = struct.unpack_from(">I", hdr, 12)[0]
            if version != self._VERSION or hdr_len < self._HEADER_SIZE:
                logger.debug(
                    "disc_id: CHD open failed, version=%d hdr_len=%d in %s",
                    version,
                    hdr_len,
                    self._path,
                )
                return False
            self._map_offset = struct.unpack_from(">Q", hdr, 24)[0]
            self._hunk_bytes = struct.unpack_from(">I", hdr, 40)[0]
            self._unit_bytes = struct.unpack_from(">I", hdr, 44)[0]
            self._codecs = list(struct.unpack_from(">4I", hdr, 108))
            if not (
                0 < self._unit_bytes <= self._hunk_bytes <= _MAX_CHD_HUNK_BYTES
                and self._hunk_bytes % self._unit_bytes == 0
            ):
                logger.debug(
                    "disc_id: CHD open failed, hunk_bytes=%d unit_bytes=%d in %s",
                    self._hunk_bytes,
                    self._unit_bytes,
                    self._path,
                )
                return False
            logger.debug(
                "disc_id: CHD opened unit_bytes=%d hunk_bytes=%d in %s",
                self._unit_bytes,
                self._hunk_bytes,
                self._path,
            )
            return True
        except OSError as e:
            logger.debug("disc_id: CHD open OSError for %s: %s", self._path, e)
            return False

    def close(self) -> None:
        if self._f:
            self._f.close()
            self._f = None

    def __enter__(self) -> "_CHDReader":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def read_sector(self, lba: int) -> Optional[bytes]:
        """
        Read one disc unit (``unit_bytes``) at the given LBA.
        Returns None on error or unsupported compression.
        """
        units_per_hunk = self._hunk_bytes // self._unit_bytes
        if units_per_hunk <= 0:
            return None
        hunk_idx = lba // units_per_hunk
        hunk_off = (lba % units_per_hunk) * self._unit_bytes
        hunk = self._get_hunk(hunk_idx)
        if hunk is None or hunk_off + self._unit_bytes > len(hunk):
            return None
        return hunk[hunk_off : hunk_off + self._unit_bytes]

    def _get_hunk(self, idx: int) -> Optional[bytes]:
        if idx in self._cache:
            return self._cache[idx]
        data = self._decode_hunk(idx)
        if data is not None and len(self._cache) < 64:
            self._cache[idx] = data
        return data

    def _decode_hunk(self, idx: int) -> Optional[bytes]:
        try:
            self._f.seek(self._map_offset + idx * self._MAP_ENTRY_SIZE)
            entry = self._f.read(self._MAP_ENTRY_SIZE)
            if len(entry) < self._MAP_ENTRY_SIZE:
                return None
            ctype = entry[0]
            # Compressed length: bytes 1–3, big-endian
            clen = struct.unpack_from(">I", b"\x00" + entry[1:4])[0]
            if clen > _MAX_CHD_COMPRESSED_BYTES:
                return None
            # File offset: bytes 4–9, big-endian
            foff = struct.unpack_from(">Q", b"\x00\x00" + entry[4:10])[0]

            if ctype == self._CTYPE_NONE:
                self._f.seek(foff)
                raw = self._f.read(self._hunk_bytes)
                return raw if len(raw) == self._hunk_bytes else None

            if ctype == self._CTYPE_MINI:
                # For COMP_MINI hunks in CHD v5, the 8-byte fill value is stored
                # directly in bytes 4–11 of the map entry, not at a file offset.
                mini = entry[4:12]
                return (mini * ((self._hunk_bytes + 7) // 8))[: self._hunk_bytes]

            if ctype in (self._CTYPE_CODEC0, self._CTYPE_CODEC1,
                         self._CTYPE_CODEC2, self._CTYPE_CODEC3):
                codec = self._codecs[ctype]
                self._f.seek(foff)
                compressed = self._f.read(clen)
                if len(compressed) != clen:
                    return None
                return self._decompress(codec, compressed)

            # COMP_SELF, COMP_PARENT, require CHD linkage; not needed here
            return None
        except Exception:
            return None

    def _decompress(self, codec: int, data: bytes) -> Optional[bytes]:
        try:
            if codec == self._CODEC_ZLIB:
                decompressor = _zlib.decompressobj()
                out = decompressor.decompress(data, self._hunk_bytes)
                if (
                    len(out) != self._hunk_bytes
                    or not decompressor.eof
                    or decompressor.unconsumed_tail
                ):
                    return None
                return out
            if codec == self._CODEC_LZMA:
                # MAME stores LZMA as 5-byte prop header + raw LZMA1 stream,
                # matching libchdr's lzma_decompress implementation.
                if len(data) < 5:
                    return None
                lc = data[0] % 9
                tmp = data[0] // 9
                lp = tmp % 5
                pb = tmp // 5
                dict_size = struct.unpack_from("<I", data, 1)[0]
                if dict_size > _MAX_CHD_LZMA_DICT_BYTES:
                    return None
                filters = [
                    {
                        "id": _lzma.FILTER_LZMA1,
                        "lc": lc,
                        "lp": lp,
                        "pb": pb,
                        "dict_size": dict_size,
                    }
                ]
                decompressor = _lzma.LZMADecompressor(
                    format=_lzma.FORMAT_RAW, filters=filters
                )
                out = decompressor.decompress(data[5:], max_length=self._hunk_bytes)
                if (
                    len(out) != self._hunk_bytes
                    or not decompressor.eof
                    or decompressor.unused_data
                ):
                    return None
                return out
            if codec == self._CODEC_ZSTD:
                try:
                    import zstandard  # optional; not required for PS2/PS1
                    out = zstandard.ZstdDecompressor().decompress(
                        data, max_output_size=self._hunk_bytes
                    )
                    return out if len(out) == self._hunk_bytes else None
                except ImportError:
                    return None
            return None  # unsupported codec (e.g. FLAC for audio tracks)
        except Exception:
            return None


class _CHDSectorStream:
    """
    Wraps a ``_CHDReader`` as a file-like object exposing 2048-byte logical
    sectors, the same interface that ``_read_pvd`` and ``_find_file`` expect.

    ``data_offset`` is the byte offset within each raw CHD sector where the
    2048-byte user-data payload begins:
      0 , DVD CHDs (unit_bytes=2048): pure ISO data, no framing
      16, Mode 1 CD sector (sync + header)
      24, Mode 2 Form 1 CD sector (sync + header + sub-header)
    """

    def __init__(self, reader: "_CHDReader", data_offset: int = 0) -> None:
        self._reader = reader
        self._data_offset = data_offset
        self._pos = 0

    def seek(self, pos: int) -> None:
        self._pos = pos

    def read(self, size: int) -> bytes:
        result = bytearray()
        remaining = size
        pos = self._pos
        while remaining > 0:
            logical_lba = pos // _SECTOR_SIZE
            offset_in = pos % _SECTOR_SIZE
            chunk_sz = min(remaining, _SECTOR_SIZE - offset_in)
            sector = self._reader.read_sector(logical_lba)
            if sector is None:
                break
            user_data = sector[self._data_offset : self._data_offset + _SECTOR_SIZE]
            if len(user_data) < _SECTOR_SIZE:
                break
            chunk = user_data[offset_in : offset_in + chunk_sz]
            if not chunk:
                break
            result.extend(chunk)
            pos += len(chunk)
            remaining -= len(chunk)
        self._pos = pos
        return bytes(result)


def _extract_from_chd_sectors(chd_path: str) -> Optional[dict]:
    """
    Extract game identity by reading disc sectors directly from a CHD file.

    This mirrors the approach used by PCSX2, AetherSX2, NetherSX2, and Argosy
    Launcher, all of which use libchdr to read the CHD's raw disc sectors and
    then walk the ISO 9660 filesystem to find SYSTEM.CNF or PARAM.SFO.

    Handles both DVD CHDs (unit_bytes=2048, plain ISO sectors) and CD CHDs
    (unit_bytes=2352/2448, Mode 1 or Mode 2 Form 1 framing with 16- or 24-byte
    sector headers).  For CD CHDs, both header sizes are probed automatically so
    PS1 discs compressed from Mode 2 Form 1 BINs and those from Mode 1 BINs are
    both supported.

    Returns a dict with a normalized ``game_id`` on success, or None.
    """
    logger.debug("disc_id: _extract_from_chd_sectors starting for %s", chd_path)
    try:
        with _CHDReader(chd_path) as reader:
            if not reader.open():
                logger.debug("disc_id: CHD reader failed to open %s", chd_path)
                return None

            if reader.unit_bytes == _SECTOR_SIZE:
                # DVD CHDs: plain 2048-byte ISO sectors, no CD framing.
                data_offsets: list[int] = [0]
                logger.debug(
                    "disc_id: %s detected as DVD CHD (unit_bytes=%d)",
                    chd_path,
                    reader.unit_bytes,
                )
            elif reader.unit_bytes in (2352, 2448):
                # CD CHDs: 2352- or 2448-byte physical sectors with a CD sector
                # frame header preceding the 2048-byte user-data payload.
                # Try Mode 2 Form 1 (24-byte header) first, the most common PS1
                # format, then fall back to Mode 1 (16-byte header).
                data_offsets = [
                    _RAW_SECTOR_HEADER_SIZE_MODE2,
                    _RAW_SECTOR_HEADER_SIZE_MODE1,
                ]
                logger.debug(
                    "disc_id: %s detected as CD CHD (unit_bytes=%d), probing"
                    " Mode 2 Form 1 (offset=%d) then Mode 1 (offset=%d)",
                    chd_path,
                    reader.unit_bytes,
                    _RAW_SECTOR_HEADER_SIZE_MODE2,
                    _RAW_SECTOR_HEADER_SIZE_MODE1,
                )
            else:
                logger.debug(
                    "disc_id: %s has unsupported unit_bytes=%d, skipping sector extraction",
                    chd_path,
                    reader.unit_bytes,
                )
                return None

            for data_offset in data_offsets:
                stream = _CHDSectorStream(reader, data_offset=data_offset)
                pvd = _read_pvd(stream)
                if not pvd:
                    logger.debug(
                        "disc_id: no ISO 9660 PVD at data_offset=%d in %s",
                        data_offset,
                        chd_path,
                    )
                    continue
                logger.debug(
                    "disc_id: ISO 9660 PVD found at data_offset=%d in %s",
                    data_offset,
                    chd_path,
                )
                root_lba, root_size = _pvd_root_dir(pvd)

                # PS2 / PS1, SYSTEM.CNF in root
                cnf = _find_file(stream, root_lba, root_size, ["SYSTEM.CNF"])
                if cnf:
                    result = _parse_system_cnf(cnf)
                    if result.get("game_id"):
                        logger.debug(
                            "disc_id: game_id=%r (platform=%r) from SYSTEM.CNF in %s",
                            result["game_id"],
                            result.get("platform"),
                            chd_path,
                        )
                        return result
                    logger.debug(
                        "disc_id: SYSTEM.CNF found but no valid game_id in %s",
                        chd_path,
                    )

                # PSP, /PSP_GAME/PARAM.SFO
                sfo = _find_file(stream, root_lba, root_size, ["PSP_GAME", "PARAM.SFO"])
                if sfo:
                    result = _parse_param_sfo(sfo)
                    if result.get("game_id"):
                        logger.debug(
                            "disc_id: game_id=%r (platform=%r) from PARAM.SFO in %s",
                            result["game_id"],
                            result.get("platform"),
                            chd_path,
                        )
                        return result
                    logger.debug(
                        "disc_id: PARAM.SFO found but no valid game_id in %s",
                        chd_path,
                    )

    except Exception as e:
        logger.debug("disc_id: CHD sector extraction failed for %s: %s", chd_path, e)
    logger.debug("disc_id: no game_id found via sector extraction in %s", chd_path)
    return None


def _extract_cue(path: str) -> Optional[dict]:
    """
    Parse a .cue sheet, find the first DATA track's BIN file, and extract.
    """
    try:
        cue_dir = Path(path).parent
        with open(path, encoding="latin-1") as cue:
            for line in cue:
                line = line.strip()
                if line.upper().startswith("FILE "):
                    # FILE "track.bin" BINARY
                    parts = line.split('"')
                    if len(parts) >= 2:
                        bin_path = cue_dir / parts[1]
                        if bin_path.exists():
                            result = _extract_bin(str(bin_path))
                            if result:
                                return result
                            break
    except Exception as e:
        logger.debug("disc_id: CUE extraction failed for %s: %s", path, e)
    return None


def _extract_gdi(path: str) -> Optional[dict]:
    """
    Parse a Dreamcast .gdi file and extract game ID from the first track's
    IP.BIN header.  The .gdi format is a plain-text index:
        <num_tracks>
        <track_num> <start_lba> <track_type> <sector_size> <filename> <UNKNOWN>
    Track 1 is always the first data area (IP.BIN).
    """
    try:
        gdi_dir = Path(path).parent
        with open(path, encoding="latin-1") as f:
            lines = [line.strip() for line in f if line.strip()]

        for line in lines[1:]:  # skip track count on first line
            parts = line.split()
            if len(parts) < 5:
                continue
            track_num = parts[0]
            sector_size = int(parts[3]) if parts[3].isdigit() else 2352
            filename = parts[4].strip('"')
            bin_path = gdi_dir / filename
            if not bin_path.exists():
                continue
            if track_num == "1":
                # Track 1 is the IP.BIN area (first 16 sectors at sector 0)
                try:
                    with open(bin_path, "rb") as bf:
                        # IP.BIN header at start of track; skip raw-sector
                        # header if sector_size > 2048
                        offset = _RAW_SECTOR_HEADER_SIZE_MODE1 if sector_size == 2352 else 0
                        bf.seek(offset)
                        data = bf.read(0x200)
                    result = _parse_ipbin(data)
                    if result.get("game_id"):
                        return result
                except OSError:
                    pass
                break  # only check track 1
    except Exception as e:
        logger.debug("disc_id: GDI extraction failed for %s: %s", path, e)
    return None


# ===========================================================================
# Public async extractor: CHD files
# ===========================================================================

async def extract_from_chd(chd_path: str, chdman_path: str = "chdman") -> Optional[dict]:
    """
    Extract game ID / title from an existing CHD file.

    Strategy (in order):
      1. Read our custom GAME / NAME tags (written by addmeta at conversion).
      2. Read disc sectors directly from the CHD via _CHDReader:
         walks the ISO 9660 filesystem to find SYSTEM.CNF (PS2/PS1) or
         PSP_GAME/PARAM.SFO (PSP).  Handles both DVD CHDs (2048-byte sectors)
         and CD CHDs (2352/2448-byte sectors with Mode 1 or Mode 2 Form 1
         framing), the same approach used by libchdr-based emulators.
      3. Read the Dreamcast GDRO IP.BIN tag (standard CD CHD tag).
      4. Look for a companion source file (.iso / .gdi / .bin / .cue) beside
         the CHD and extract from that.

    Returns a dict containing at least ``game_id`` when successful. The dict
    may also include ``title`` and/or ``platform`` depending on the extraction
    strategy, ``platform`` is only present when the result comes from disc
    sector / GDRO / source-file extraction (Strategies 2–4), not from the
    embedded GAME tag (Strategy 1). Returns None if no ID could be found.
    """
    # --- Strategy 1: our embedded GAME tag -----------------------------------
    result = await _dumpmeta_text(chd_path, TAG_GAME, chdman_path)
    if result:
        game_id = result.strip()
        if game_id:
            name_result = await _dumpmeta_text(chd_path, TAG_NAME, chdman_path)
            title = (name_result or "").strip() or None
            logger.debug(
                "disc_id: Strategy 1 (GAME tag) found game_id=%r title=%r in %s",
                game_id,
                title,
                chd_path,
            )
            out: dict = {"game_id": game_id}
            if title:
                out["title"] = title
            return out
    logger.debug("disc_id: Strategy 1 (GAME tag) not present in %s", chd_path)

    # --- Strategy 2: read disc sectors directly (like PCSX2 / AetherSX2) ----
    # For DVD CHDs (unit_bytes=2048) this reads SYSTEM.CNF / PARAM.SFO from
    # the CHD's own ISO 9660 filesystem without any extraction pass.
    _loop = asyncio.get_running_loop()
    disc_result = await _loop.run_in_executor(None, _extract_from_chd_sectors, chd_path)
    if disc_result and disc_result.get("game_id"):
        logger.debug(
            "disc_id: Strategy 2 (CHD sectors) found game_id=%r in %s",
            disc_result["game_id"],
            chd_path,
        )
        return disc_result
    logger.debug("disc_id: Strategy 2 (CHD sectors) found no game_id in %s", chd_path)

    # --- Strategy 3: Dreamcast GDRO (IP.BIN) ---------------------------------
    gdro = await _dumpmeta_bin(chd_path, "GDRO", chdman_path)
    if gdro:
        parsed = _parse_ipbin(gdro)
        if parsed.get("game_id"):
            logger.debug(
                "disc_id: Strategy 3 (GDRO/IP.BIN) found game_id=%r in %s",
                parsed["game_id"],
                chd_path,
            )
            return parsed
        logger.debug(
            "disc_id: Strategy 3 (GDRO/IP.BIN) tag present but no valid game_id in %s",
            chd_path,
        )
    else:
        logger.debug("disc_id: Strategy 3 (GDRO) tag not found in %s", chd_path)

    # --- Strategy 4: companion source file -----------------------------------
    chd_p = Path(chd_path)
    for ext in (".iso", ".gdi", ".cue", ".bin"):
        candidate = chd_p.with_suffix(ext)
        if candidate.exists():
            res = await _loop.run_in_executor(None, extract_from_source, str(candidate))
            if res and res.get("game_id"):
                logger.debug(
                    "disc_id: Strategy 4 (companion %s) found game_id=%r in %s",
                    candidate.name,
                    res["game_id"],
                    chd_path,
                )
                return res
    logger.debug("disc_id: Strategy 4 (companion file) found no game_id for %s", chd_path)

    logger.debug("disc_id: all strategies exhausted, no game_id found for %s", chd_path)
    return None


async def embed_in_chd(
    chd_path: str,
    game_id: str,
    title: Optional[str] = None,
    chdman_path: str = "chdman",
) -> bool:
    """
    Embed *game_id* (and optionally *title*) into a CHD file using
    ``chdman addmeta``.  Returns True on success, False on failure.

    Tags written:
      GAME, the normalized disc serial (e.g. "SLUS-20312", "ULES-00135",
             "MK-51034") in the form emulator frontends use for DB lookup
      NAME, the human-readable game title (optional)
    """
    logger.debug(
        "disc_id: embed_in_chd game_id=%r title=%r in %s",
        game_id,
        title,
        chd_path,
    )
    ok = await _addmeta_text(chd_path, TAG_GAME, game_id, chdman_path)
    if not ok:
        return False
    if title:
        # NAME tag is best-effort; don't fail the whole operation if it errors
        await _addmeta_text(chd_path, TAG_NAME, title, chdman_path)
    return True


async def read_embedded_game_id(
    chd_path: str,
    chdman_path: str = "chdman",
) -> Optional[str]:
    """Return the GAME serial already embedded in *chd_path*, or ``None``.

    Reads only our own embedded GAME tag (no disc-sector / GDRO / companion-file
    fallback, unlike :func:`extract_from_chd`), so callers can cheaply check
    whether a CHD is *already* tagged before writing. This is what keeps the
    conversion-time embed idempotent: re-running it on an already-tagged CHD
    can be skipped instead of appending a duplicate GAME tag.
    """
    raw = await _dumpmeta_text(chd_path, TAG_GAME, chdman_path)
    if raw and raw.strip():
        return raw.strip()
    return None


async def clear_embedded_disc_id(
    chd_path: str,
    chdman_path: str = "chdman",
) -> None:
    """Strip embedded GAME/NAME tags so a later :func:`embed_in_chd` converges.

    ``chdman addmeta`` *appends* a new metadata item rather than replacing an
    existing one, so re-tagging a CHD that already carries a *different* serial
    would leave the stale GAME/NAME behind (and accumulate duplicates on repeat).
    Callers that detect a serial mismatch call this first to delete the old pair
    before writing the new one. Best-effort: a freshly created CHD has nothing
    to delete (``delmeta`` simply reports the tag absent).
    """
    await _delmeta(chd_path, TAG_GAME, chdman_path)
    await _delmeta(chd_path, TAG_NAME, chdman_path)


async def ensure_disc_id_embedded(
    chd_path: str,
    chdman_path: str = "chdman",
) -> Optional[dict]:
    """
    Ensure a CHD file has GAME / NAME metadata tags embedded, back-filling
    existing CHDs that were created before conversion-time tagging was added.

    Algorithm (standards-compliant, all tags follow the 4-char MAME CHD
    metadata format and are written via ``chdman addmeta``):

      1. Fast-path: read the GAME tag.  If it is already present, return the
         existing info without touching the file.
      2. Read disc sectors directly from the CHD via _CHDReader: walks the
         ISO 9660 filesystem to find SYSTEM.CNF / PARAM.SFO, then embeds
         GAME / NAME.  Handles both DVD CHDs (2048-byte sectors) and CD CHDs
         (2352/2448-byte sectors with Mode 1 or Mode 2 Form 1 framing).
         The serial is normalized to the canonical emulator form (SLUS-20312).
      3. Try the standard Dreamcast GDRO (IP.BIN) tag that chdman embeds for
         GDI-sourced CHDs.  Parse product number + title from the IP.BIN
         binary, then embed GAME / NAME via addmeta.
      4. Scan for a companion source file (.iso / .gdi / .cue / .bin) next to
         the CHD, extract the disc serial, then embed GAME / NAME.
      5. Return None if no identity information can be found.

    The GAME tag always stores the normalized game serial. The NAME (title) tag
    stores a human-readable game title when one is available (e.g., from PSP
    PARAM.SFO or Dreamcast IP.BIN), falling back to the serial when no title
    can be extracted. Emulator frontends and database scrapers key on the serial
    for game lookup, so this fallback ensures maximum compatibility while
    preserving real titles when possible.

    Returns a dict with at least ``game_id`` when disc identity information was
    found, regardless of whether the GAME/NAME tags were successfully embedded
    into the CHD file.  When embedding fails (e.g. the file is read-only or
    ``chdman`` is unavailable), the disc ID is still returned so callers can
    cache it for display; a ``WARNING`` is logged in that case.
    Returns None only when no disc ID could be found at all.
    """
    # --- Fast path: GAME tag already present ---------------------------------
    existing = await _dumpmeta_text(chd_path, TAG_GAME, chdman_path)
    if existing and existing.strip():
        game_id = existing.strip()
        name_raw = await _dumpmeta_text(chd_path, TAG_NAME, chdman_path)
        title = (name_raw or "").strip() or None
        logger.debug(
            "disc_id: GAME tag already present game_id=%r title=%r in %s",
            game_id,
            title,
            chd_path,
        )
        out: dict = {"game_id": game_id}
        if title:
            out["title"] = title
        return out

    # --- Strategy 2: read disc sectors directly (like PCSX2 / AetherSX2) ----
    _loop = asyncio.get_running_loop()
    disc_result = await _loop.run_in_executor(None, _extract_from_chd_sectors, chd_path)
    if disc_result and disc_result.get("game_id"):
        logger.info(
            "disc_id: embedding game_id=%r from CHD sectors in %s",
            disc_result["game_id"],
            chd_path,
        )
        ok = await embed_in_chd(
            chd_path,
            disc_result["game_id"],
            disc_result.get("title") or disc_result["game_id"],
            chdman_path,
        )
        if not ok:
            logger.warning(
                "disc_id: failed to embed GAME/NAME tags in %s"
                ", disc ID will be cached for display only",
                chd_path,
            )
        return disc_result
    logger.debug(
        "disc_id: ensure Strategy 2 (CHD sectors) found no game_id in %s", chd_path
    )

    # --- Strategy 3: standard Dreamcast GDRO tag -----------------------------
    gdro = await _dumpmeta_bin(chd_path, "GDRO", chdman_path)
    if gdro:
        parsed = _parse_ipbin(gdro)
        if parsed.get("game_id"):
            logger.info(
                "disc_id: embedding game_id=%r from GDRO in %s",
                parsed["game_id"],
                chd_path,
            )
            ok = await embed_in_chd(
                chd_path,
                parsed["game_id"],
                parsed.get("title") or parsed["game_id"],
                chdman_path,
            )
            if not ok:
                logger.warning(
                    "disc_id: failed to embed GAME/NAME tags in %s"
                    ", disc ID will be cached for display only",
                    chd_path,
                )
            return parsed
        logger.debug(
            "disc_id: ensure Strategy 3 (GDRO) tag present but no valid game_id in %s",
            chd_path,
        )
    else:
        logger.debug(
            "disc_id: ensure Strategy 3 (GDRO) tag not found in %s", chd_path
        )

    # --- Strategy 4: companion source file -----------------------------------
    chd_p = Path(chd_path)
    for ext in (".iso", ".gdi", ".cue", ".bin"):
        candidate = chd_p.with_suffix(ext)
        if candidate.exists():
            res = await _loop.run_in_executor(None, extract_from_source, str(candidate))
            if res and res.get("game_id"):
                logger.info(
                    "disc_id: embedding game_id=%r from companion %s in %s",
                    res["game_id"],
                    candidate.name,
                    chd_path,
                )
                ok = await embed_in_chd(
                    chd_path,
                    res["game_id"],
                    res.get("title") or res["game_id"],
                    chdman_path,
                )
                if not ok:
                    logger.warning(
                        "disc_id: failed to embed GAME/NAME tags in %s"
                        ", disc ID will be cached for display only",
                        chd_path,
                    )
                return res

    logger.debug(
        "disc_id: ensure_disc_id_embedded exhausted all strategies, no game_id found for %s",
        chd_path,
    )
    return None


# ===========================================================================
# Private chdman subprocess helpers
# ===========================================================================

async def _addmeta_text(
    chd_path: str, tag: str, value: str, chdman_path: str
) -> bool:
    """Write a text metadata tag to *chd_path* using chdman addmeta."""
    try:
        logger.debug(
            "disc_id: addmeta tag=%s value=%r in %s",
            tag,
            value,
            chd_path,
        )
        returncode, _, stderr = await _chdman_runner().run_capture(
            [chdman_path, "addmeta", "-i", chd_path, "-t", tag, "-vt", value],
            timeout=_DUMPMETA_TIMEOUT_SECONDS,
            fail_label="chdman addmeta",
        )
        if returncode != 0:
            logger.warning(
                "disc_id: addmeta tag=%s failed (rc=%s): %s",
                tag,
                returncode,
                stderr.decode(errors="replace").strip(),
            )
            return False
        logger.debug("disc_id: addmeta tag=%s written successfully in %s", tag, chd_path)
        return True
    except Exception as e:
        # An unkillable child is not "this tag couldn't be written": it is still
        # running against the same storage, and Phase 2 of the scan walks a whole
        # library through here (issue #268).
        reraise_if_abandoned(e)
        logger.warning("disc_id: addmeta tag=%s error: %s", tag, e)
        return False


async def _delmeta(chd_path: str, tag: str, chdman_path: str) -> bool:
    """Delete a metadata tag from *chd_path* via chdman delmeta (best-effort).

    Returns True on success. A missing tag (nothing to delete) reports a non-zero
    exit, which is treated as a benign no-op by callers, the point is only to
    guarantee the tag is absent before a fresh addmeta writes the current value.
    """
    try:
        returncode, _, stderr = await _chdman_runner().run_capture(
            [chdman_path, "delmeta", "-i", chd_path, "-t", tag],
            timeout=_DUMPMETA_TIMEOUT_SECONDS,
            fail_label="chdman delmeta",
        )
        if returncode != 0:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "disc_id: delmeta tag=%s not present or failed (rc=%s) in %s: %s",
                    tag,
                    returncode,
                    chd_path,
                    stderr.decode(errors="replace").strip(),
                )
            return False
        logger.debug("disc_id: delmeta tag=%s removed from %s", tag, chd_path)
        return True
    except Exception as e:
        reraise_if_abandoned(e)
        logger.warning("disc_id: delmeta tag=%s error: %s", tag, e)
        return False


async def _dumpmeta_text(
    chd_path: str, tag: str, chdman_path: str
) -> Optional[str]:
    """Read a text metadata tag from *chd_path* via chdman dumpmeta."""
    raw = await _dumpmeta_raw(chd_path, tag, chdman_path, suffix=".txt")
    if raw is None:
        return None
    return raw.decode("utf-8", errors="replace")


async def _dumpmeta_bin(
    chd_path: str, tag: str, chdman_path: str
) -> Optional[bytes]:
    """Read a binary metadata tag from *chd_path* via chdman dumpmeta."""
    return await _dumpmeta_raw(chd_path, tag, chdman_path, suffix=".bin")


async def _dumpmeta_raw(
    chd_path: str, tag: str, chdman_path: str, suffix: str = ".bin"
) -> Optional[bytes]:
    """Run ``chdman dumpmeta`` and return the output bytes, or None on failure."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp_path = tmp.name
    tmp.close()
    try:
        returncode, _, stderr = await _chdman_runner().run_capture(
            [chdman_path, "dumpmeta", "-i", chd_path, "-t", tag, "-o", tmp_path],
            timeout=_DUMPMETA_TIMEOUT_SECONDS,
            fail_label="chdman dumpmeta",
        )
        if returncode is None:
            logger.debug(
                "disc_id: dumpmeta tag=%s timed out after %ss in %s",
                tag,
                _DUMPMETA_TIMEOUT_SECONDS,
                chd_path,
            )
            return None
        if returncode != 0:
            if logger.isEnabledFor(logging.DEBUG):
                stderr_text = stderr.decode(errors="replace").strip()
                logger.debug(
                    "disc_id: dumpmeta tag=%s not found or failed (rc=%d) in %s: %s",
                    tag,
                    returncode,
                    chd_path,
                    stderr_text,
                )
            return None
        if os.path.getsize(tmp_path) > _MAX_DUMPMETA_BYTES:
            logger.debug(
                "disc_id: dumpmeta tag=%s exceeded %d bytes in %s",
                tag,
                _MAX_DUMPMETA_BYTES,
                chd_path,
            )
            return None
        with open(tmp_path, "rb") as f:
            return f.read(_MAX_DUMPMETA_BYTES + 1)
    except Exception as e:
        reraise_if_abandoned(e)
        logger.debug("disc_id: dumpmeta tag=%s error: %s", tag, e)
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
