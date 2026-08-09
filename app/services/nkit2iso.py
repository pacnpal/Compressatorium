"""nkit2iso service: restore an NKit-shrunk GameCube/Wii image to a plain ISO.

NKit (Nanook's format) shrinks a GC/Wii disc image by dropping everything that
is reproducible — the pseudo-random "junk" padding, inter-file gaps, all-junk
files, and for Wii the AES encryption and H0-H3 hash tree. ``nkit2iso``
(DonMikone/nkit2iso, MIT) replays that removal in reverse and CRC32-checks the
result against the value stored in the NKit header, so a clean exit means the
restored image is bit-exact.

One direction only: NKit is produced by NKit itself, not by this app, so there
is no "shrink to NKit" mode to pair with the restore. Once restored, the ``.iso``
is an ordinary Dolphin/CHDMAN source like any other.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import struct
import zlib
from collections.abc import AsyncGenerator
from pathlib import Path

from config import settings
from logging_setup import get_logger
from services.subprocess_runner import (
    ConversionCancelled,
    SubprocessRunner,
    ioprio_prefix,
)
from utils.path_utils import match_extension

# Compound source extensions: the meaningful part is ``.nkit``, but the
# container it rides in decides how nkit2iso reads it (a plain nkit byte stream
# vs. a zlib-compressed GCZ block container). Declared in full so the registry's
# suffix match keeps them distinct from the generic ``.iso`` / ``.gcz`` that
# CHDMAN and Dolphin own — see utils.path_utils.match_extension.
NKIT2ISO_CONVERTIBLE_EXTENSIONS = {".nkit.iso", ".nkit.gcz"}

# Every source restores to the same thing: the original, full-size disc image.
NKIT2ISO_OUTPUT_EXTENSION = ".iso"

# Recovery modes we expose (the tool's own "ask" prompts on a terminal, which a
# job worker cannot answer). See settings.nkit2iso_recovery.
NKIT2ISO_RECOVERY_MODES = frozenset({"none", "download"})

# The tool draws "\r  ' 42%" progress redraws on stderr; SubprocessRunner folds
# stderr into stdout and normalizes \r to \n, so each redraw arrives as a line.
_PROGRESS_RE = re.compile(r"^(\d{1,3})%$")

# Printed on a clean exit when the CRC32 could NOT be checked, because the image
# lost its Wii update partition and was restored without the recovery file. The
# result is playable but not redump-verifiable, which the job should say out loud
# rather than reporting a bare "complete".
_NOT_EXACT_MARKER = "CRC32 check skipped"

# --- NKit / GameCube-Wii disc header -------------------------------------
#
# Reading the header is NOT a reimplementation of the restore: it is the same
# documented disc header every GC/Wii tool reads, plus NKit's own 0x200 metadata
# window, and the app already does this shape of thing elsewhere (the Z3DS
# container header in z3ds_compress, PARAM.SFO in ps3.py, disc serials in
# disc_id.py). It buys the Info panel real answers — GameCube vs Wii, the game
# ID and title, and how big the restored ISO will be — instead of a bare file
# size, and it gives the chain preflight a true intermediate size rather than a
# guessed ratio.
_DISC_HEADER_SIZE = 0x440       # what nkit2iso itself reads before deciding
_NKIT_MARKER = b"NKIT v01"      # at 0x200, the "this really is NKit" proof
_WII_MAGIC = 0x5D1C9EA3         # be32 at 0x18
_GC_MAGIC = 0xC2339F3D          # be32 at 0x1C
_GCZ_MAGIC = 0xB10BC001         # le32 at 0 of a .nkit.gcz container

# Dolphin GCZ container: a 32-byte header, then one u64 pointer per block, then
# one u32 Adler32 per block, then the block data. Only the FIRST block is ever
# inflated here — it is 16 KiB or larger, so it always covers the 0x440-byte
# disc header we need.
_GCZ_HEADER = struct.Struct("<IIQQII")  # magic, sub_type, comp_size, data_size,
#                                          block_size, num_blocks
# Sanity bounds on header-supplied lengths (Dolphin writes 16 KiB blocks; 32 MiB
# is far beyond anything real). A stored block can exceed block_size only by
# zlib's worst-case expansion, so a small slack covers the legitimate case.
_GCZ_MAX_BLOCK_SIZE = 32 * 1024 * 1024
_GCZ_BLOCK_SLACK = 64 * 1024

logger = get_logger("nkit2iso")


class NkitHeaderError(ValueError):
    """``path`` is not a readable NKit v01 GameCube/Wii image."""


def _format_size(size: int) -> str:
    """MB/GB display string, matching the other tools' info payloads."""
    size_mb = size / (1024 * 1024)
    return f"{size_mb:.2f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB"


def _gcz_first_block(handle, file_size: int) -> bytes:
    """Inflate the first block of a Dolphin GCZ container.

    Every length here comes from the file, so every length is bounded against
    the file's real size before it reaches a ``read()``: ``num_blocks`` alone
    could otherwise ask for tens of gigabytes from a 40-byte crafted file and
    turn an intended 422 into a ``MemoryError``. Only the first block is ever
    needed (16 KiB or larger, so it always covers the 0x440-byte disc header),
    so only the one or two pointers bounding it are read — never the whole
    table.
    """
    raw = handle.read(_GCZ_HEADER.size)
    if len(raw) < _GCZ_HEADER.size:
        raise NkitHeaderError("Truncated GCZ header")
    magic, _sub_type, comp_size, data_size, block_size, num_blocks = (
        _GCZ_HEADER.unpack(raw)
    )
    if magic != _GCZ_MAGIC or not block_size or not num_blocks or not data_size:
        raise NkitHeaderError("Invalid GCZ header")
    if block_size > _GCZ_MAX_BLOCK_SIZE:
        raise NkitHeaderError(f"Implausible GCZ block size ({block_size})")

    # The block table is one u64 pointer plus one u32 hash per block. If it
    # can't fit in the file, the header is lying and nothing below is safe.
    data_offset = _GCZ_HEADER.size + 12 * num_blocks
    if data_offset >= file_size:
        raise NkitHeaderError("GCZ block table does not fit in the file")

    # Just the pointers bounding block 0.
    pointers = handle.read(16 if num_blocks > 1 else 8)
    if len(pointers) < 8:
        raise NkitHeaderError("Truncated GCZ block table")
    first = struct.unpack_from("<Q", pointers, 0)[0]
    stored_raw = bool(first & (1 << 63))
    start = first & ~(1 << 63)
    # The next pointer (or the total compressed size for a single-block image)
    # bounds this block's stored bytes.
    end = comp_size
    if num_blocks > 1 and len(pointers) >= 16:
        end = struct.unpack_from("<Q", pointers, 8)[0] & ~(1 << 63)
    if end <= start or data_offset + start >= file_size:
        raise NkitHeaderError("Invalid GCZ block table")

    # Clamp to what the file actually holds *and* to what one block can inflate
    # from, so a bogus comp_size / pointer pair can't drive a huge allocation.
    available = file_size - (data_offset + start)
    stored_len = min(end - start, available, block_size + _GCZ_BLOCK_SLACK)
    handle.seek(data_offset + start)
    stored = handle.read(stored_len)
    # Every block inflates to block_size except a truncated final block.
    want = min(block_size, data_size)
    if stored_raw:
        return stored[:want]
    try:
        return zlib.decompressobj().decompress(stored, want)
    except zlib.error as exc:
        raise NkitHeaderError(f"Corrupt GCZ block: {exc}") from exc


def read_nkit_header(path: str) -> dict:
    """Parse the NKit/disc header of ``path``.

    Blocking (one small read, plus a single zlib block for a ``.nkit.gcz``);
    call it off the event loop. Raises :class:`NkitHeaderError` when the file
    is not an NKit v01 image — which is also the honest answer for a plain
    ``.iso`` someone renamed.
    """
    file_size = os.path.getsize(path)
    with open(path, "rb") as handle:
        gcz = handle.read(4) == struct.pack("<I", _GCZ_MAGIC)
        handle.seek(0)
        if gcz:
            head = _gcz_first_block(handle, file_size)[:_DISC_HEADER_SIZE]
        else:
            head = handle.read(_DISC_HEADER_SIZE)

    if len(head) < _DISC_HEADER_SIZE:
        raise NkitHeaderError("File is too small to hold a disc header")
    if head[0x200:0x208] != _NKIT_MARKER:
        raise NkitHeaderError(
            "Not an NKit v01 image (marker missing at 0x200) — "
            "is it already a plain ISO?"
        )

    wii = struct.unpack_from(">I", head, 0x18)[0] == _WII_MAGIC
    gamecube = struct.unpack_from(">I", head, 0x1C)[0] == _GC_MAGIC
    if not (wii or gamecube):
        raise NkitHeaderError("Not a GameCube or Wii disc image")

    # Wii stores the image size in 4-byte units; GameCube stores plain bytes.
    raw_size = struct.unpack_from(">I", head, 0x210)[0]
    restored_size = raw_size * 4 if wii else raw_size

    return {
        "platform": "Wii" if wii else "GameCube",
        "game_id": head[0x00:0x06].decode("ascii", errors="replace").strip("\x00"),
        "title": head[0x20:0x60].decode("ascii", errors="replace").split("\x00")[0].strip(),
        "disc_number": head[0x06],
        "disc_version": head[0x07],
        "restored_size": restored_size,
        # The CRC32 of the ORIGINAL image, which nkit2iso checks the restore
        # against. Stored, not computed — it costs nothing to surface.
        "crc32": f"{struct.unpack_from('>I', head, 0x208)[0]:08X}",
        "container": "GCZ (zlib block container)" if gcz else "NKit stream",
    }


class Nkit2IsoService:
    """Wrapper for the ``nkit2iso`` binary."""

    def __init__(self) -> None:
        self.nkit2iso_path = settings.nkit2iso_path
        self._runner = SubprocessRunner(owner="nkit2iso")

    def _build_command(self, input_path: str, output_path: str) -> list[str]:
        """Build the nkit2iso argv.

        Format: ``nkit2iso -i <in> -o <out> -f -recovery <none|download>``.

        ``-f`` is safe here and not a policy decision of ours: the job pipeline
        has already resolved duplicate handling (skip / rename / authorized
        overwrite, which unlinks the target first), so anything still sitting at
        ``output_path`` is a partial left by a crashed run. Without ``-f`` the
        tool would refuse to start and the retry could never succeed.
        """
        recovery = settings.nkit2iso_recovery
        if recovery not in NKIT2ISO_RECOVERY_MODES:
            # Settings validation pins this, so a bad value means the field was
            # bypassed; fall back to the offline default rather than letting the
            # tool drop into its interactive "ask" prompt on a pipe.
            logger.warning(
                "Invalid NKIT2ISO_RECOVERY %r; falling back to 'none'", recovery,
            )
            recovery = "none"
        cmd = [
            self.nkit2iso_path,
            "-i", input_path,
            "-o", output_path,
            "-f",
            "-recovery", recovery,
        ]
        return ioprio_prefix("nkit2iso") + cmd

    def active_pids(self) -> list[int]:
        return self._runner.active_pids()

    @staticmethod
    def is_convertible(filename: str) -> bool:
        """Whether ``filename`` is an NKit image nkit2iso can restore."""
        lower = filename.lower()
        return any(lower.endswith(ext) for ext in NKIT2ISO_CONVERTIBLE_EXTENSIONS)

    @staticmethod
    def _output_stem(name: str) -> str:
        """Strip the compound NKit extension: ``Game.nkit.iso`` -> ``Game``.

        ``Path.stem`` would leave ``Game.nkit`` and the restored image would be
        named ``Game.nkit.iso`` — the input path itself. Falls back to plain
        suffix removal for a name that carries no recognised NKit extension
        (an archive member the caller flattened, say).
        """
        lower = name.lower()
        for ext in sorted(NKIT2ISO_CONVERTIBLE_EXTENSIONS, key=len, reverse=True):
            if lower.endswith(ext):
                return name[: -len(ext)]
        return str(Path(name).with_suffix(""))

    def get_output_path(
        self,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,  # noqa: ARG002 - archive members keep their ext
    ) -> str:
        """Resolve the restored ``.iso`` path for an NKit source.

        ``treat_as_stem`` is accepted for interface parity and ignored: archive
        members arrive as flattened filenames that keep their original extension
        (``games_Game.nkit.iso``), so they map exactly like an on-disk file.
        """
        input_p = Path(input_path)
        filename = f"{self._output_stem(input_p.name)}{NKIT2ISO_OUTPUT_EXTENSION}"
        if output_dir:
            return str(Path(output_dir) / filename)
        return str(input_p.parent / filename)

    def get_output_path_for_mode(
        self,
        mode: str,  # noqa: ARG002 - single-mode tool, kept for interface parity
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        return self.get_output_path(
            input_path, output_dir, treat_as_stem=treat_as_stem,
        )

    @staticmethod
    def restored_size(path: str) -> int | None:
        """Size of the ISO this source will restore to, or ``None`` if unknown.

        Read from the NKit header rather than guessed from a ratio: NKit shrink
        ratios vary enormously (a scrubbed Wii disc can be a twentieth of its
        restored size), so a ratio-based disk preflight would badly under-count.
        Returns ``None`` on any unreadable/!NKit input so callers fall back.
        """
        try:
            size = read_nkit_header(path).get("restored_size") or 0
        except (NkitHeaderError, OSError):
            return None
        return size or None

    def info(self, file_path: str) -> dict:
        """Describe an NKit source: what disc it is and what it restores to.

        Synchronous (small header read); the plugin threadpools it. nkit2iso
        has no ``info`` subcommand, so this reads the header directly — the same
        one the binary reads — rather than shelling out.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        file_size = os.path.getsize(file_path)
        header = read_nkit_header(file_path)
        restored = header["restored_size"]
        # Percentage of the original the shrunk file occupies — the number a
        # body actually wants when deciding whether to restore.
        ratio = f"{file_size / restored * 100:.1f}%" if restored else None

        return {
            "file": file_path,
            "size": file_size,
            "size_display": _format_size(file_size),
            "format": f"NKit v01 ({header['platform']})",
            "extension": (
                match_extension(file_path, NKIT2ISO_CONVERTIBLE_EXTENSIONS)
                or Path(file_path).suffix.lower()
            ),
            "compressed": True,
            "compression_type": header["container"],
            "platform": header["platform"],
            "game_id": header["game_id"] or None,
            "title": header["title"] or None,
            "disc_number": header["disc_number"],
            "disc_version": header["disc_version"],
            "restored_size": restored or None,
            "restored_size_display": _format_size(restored) if restored else None,
            "crc32": header["crc32"],
            "ratio": ratio,
        }

    @staticmethod
    def _parse_progress(line: str) -> int | None:
        match = _PROGRESS_RE.match(line)
        if not match:
            return None
        return min(100, max(0, int(match.group(1))))

    async def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str = "nkit_restore",  # noqa: ARG002 - single-mode tool
        *,
        compression: str | None = None,  # noqa: ARG002 - NKit restore has no codec
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Restore ``input_path`` to ``output_path``, yielding progress dicts.

        Honors ``cancel_event`` (terminate + clean the partial output, raising
        ``ConversionCancelled``) and the shared stall timeout. Yields
        ``{"progress": int, "message": str}``; the final message reports the
        CRC32 outcome, including the "playable but not bit-exact" case a Wii
        image restored without its recovery partition lands in.
        """
        if not self.is_convertible(input_path):
            raise ValueError(f"Not an NKit image: {input_path}")

        cmd = self._build_command(input_path, output_path)
        yield {"progress": 1, "message": "Starting NKit restore..."}

        not_exact = False
        try:
            async for update in self._runner.run(
                cmd,
                input_path=input_path,
                output_path=output_path,
                parse_progress=self._parse_progress,
                initial_progress=1,
                cancel_event=cancel_event,
                fail_label="nkit2iso",
                complete_message="NKit restore complete (CRC32 verified)",
                require_output=True,
            ):
                if _NOT_EXACT_MARKER in update.get("message", ""):
                    not_exact = True
                yield update
        except (
            ConversionCancelled, RuntimeError, asyncio.CancelledError, GeneratorExit,
        ):
            # nkit2iso removes its own half-written output on a hard error, but
            # not when we kill it mid-run (cancel / stall), so sweep the partial
            # here too. Setup failures above this try are not caught, so a
            # pre-existing file is never removed for a run that wrote nothing.
            with contextlib.suppress(OSError):
                if os.path.exists(output_path):
                    os.remove(output_path)
            raise

        if not_exact:
            # Overrides the runner's terminal 100% message: the restore
            # succeeded, but the operator needs to know this ISO will not match
            # a redump checksum. ``warning`` marks it as a caveat a *chain* must
            # carry into its own terminal message (a later step's messages would
            # otherwise bury it); job_manager reads only progress/message, so
            # the extra key is inert on a direct job.
            yield {
                "progress": 100,
                "warning": True,
                "message": (
                    "NKit restore complete — playable, but NOT bit-exact: the "
                    "Wii update partition was removed at shrink time and was "
                    "zero-filled. Set NKIT2ISO_RECOVERY=download to splice in "
                    "the archived recovery partition for a redump-verified "
                    "restore."
                ),
            }


# Global service instance
nkit2iso_service = Nkit2IsoService()
