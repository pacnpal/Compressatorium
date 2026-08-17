"""Wrapper for JWUDTool (https://www.gamebrew.org/wiki/JWUDTool_Wii_U).

JWUDTool is Maschell's Java front-end to JNUSLib. It converts a Wii U disc
image between the raw ``.wud`` dump and Exzap's compressed ``.wux`` container
(a sector-deduplicating format Cemu reads directly). Only those two lossless
directions are wired up here — JWUDTool's decrypt/extract features need the
console's common key plus a per-disc title key and are out of scope for a
compression app, so this wrapper never passes a key and never asks for one.

Four things about the real tool (0.4) drive this wrapper:

- **It writes into a *directory* with a fixed filename.** ``-out`` names a
  folder and JNUSLib's ``WUDService`` always writes ``game.wux`` /
  ``game.wud`` inside it. We hand it a private temp dir on the destination
  filesystem and move the single result onto the exact ``output_path`` the job
  expects, so duplicate handling stays in the job layer (same trick as nsz).
- **It exits 0 even when it refuses.** A wrong-sized image, an already-
  compressed input or an existing target just prints a line and returns
  cleanly, so ``require_output`` turns "no file produced" into the error, with
  the tool's own reason attached.
- **It verifies its own output, and still exits 0 when that fails.** The
  default post-conversion pass re-reads both images and compares them byte for
  byte, printing ``Warning! (De)Compressed file is INVALID!`` on a mismatch
  without a non-zero exit — so the wrapper watches stdout for that line and
  fails the job itself.
- **Progress arrives in two phases.** The conversion prints its own percentage
  and the verification pass prints a second one, so the bar maps the conversion
  to 1-50 % and the verification to 51-99 %.

``verify()`` is ours, not JWUDTool's: the tool's ``-verify`` *compares two
images*, which has no meaning for a single finished output. Instead we validate
the WUX container structurally (magic, header fields, and every sector-index
entry against the file's real length), which is what catches the truncation and
index corruption a 25 GB file actually suffers. WUX stores no content checksums,
so there is nothing deeper to check — and there does not need to be: the
conversion that produced the file already ran JWUDTool's full byte-for-byte
comparison against the source.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import struct
import tempfile
from collections.abc import AsyncGenerator
from pathlib import Path

from config import settings
from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from services.subprocess_runner import (
    SubprocessRunner,
    collect_verify,
    ioprio_prefix,
    nice_prefix,
    verify_preflight,
)

# Compress takes the raw dump, decompress takes the compressed container.
JWUD_COMPRESS_EXTENSIONS = {".wud"}
JWUD_DECOMPRESS_EXTENSIONS = {".wux"}
# Input extension -> output extension, both directions in one unambiguous map.
JWUD_OUTPUT_FORMATS = {".wud": ".wux", ".wux": ".wud"}

# The fixed filename JNUSLib writes inside the -out folder, per direction.
_PRODUCED_NAME = {"jwud_compress": "game.wux", "jwud_decompress": "game.wud"}

# Split dumps (what FIX94's wudump writes). JNUSLib's WUDDiscReaderSplitted
# hard-codes the filename template and compares it with String.equals, so the
# names really are exactly `game_part1.wud` … `game_part12.wud`, case included,
# all in one directory: 11 parts of exactly 2 GiB plus a 1,402,994,688-byte
# twelfth, summing to WUD_IMAGE_SIZE. Selecting part 1 converts the whole set —
# JWUDTool joins them itself — so part 1 is the only convertible member and the
# rest are its `source_companions`.
WUD_SPLIT_PART_TEMPLATE = "game_part%d.wud"
WUD_SPLIT_PART_SIZE = 0x100000 * 0x800  # 2 GiB
WUD_SPLIT_MAX_PARTS = 12
_WUD_SPLIT_PART_RE = re.compile(r"^game_part(?P<idx>[0-9]+)\.wud$")

# Per-job verification choice, carried on the `compression` field the same way
# nsz carries solid/block and CSO/romz carry an effort preset. JWUDTool verifies
# its output against the source by default; `-noVerify` trades that guarantee
# for roughly half the runtime.
JWUD_VERIFY = "verify"
JWUD_NO_VERIFY = "noverify"

# WUX container layout (JNUSLib ``WUDImageCompressedInfo``), little-endian:
#   0x00 u32 magic "WUX0" │ 0x04 u32 0x1099d02e │ 0x08 u32 sectorSize
#   0x0C u32 flags        │ 0x10 u64 uncompressedSize   (header is 0x20 bytes)
# The sector index table follows at 0x20 — one u32 per logical sector — and the
# deduplicated sector array starts at the next sectorSize boundary after it.
WUX_HEADER_SIZE = 0x20
WUX_MAGIC = b"WUX0"
WUX_MAGIC_1 = 0x1099D02E
# sectorSize is stored in the header, but JNUSLib's WUDImageCompressedInfo only
# ever writes 0x8000 and the real 25 GB image confirms it (763,712 entries,
# sector array at 0x2F0000). Pinning it is a hard bound on the work `verify`
# will do: the index table is one u32 per logical sector, so a crafted header
# declaring a 4-byte sector would claim ~6.25 billion entries (a ~25 GB table)
# and tie up the verification lane unpacking a sparse file for hours.
WUX_SECTOR_SIZE = 0x8000
# Every Wii U disc image is exactly this size. JNUSLib refuses to compress or
# decompress anything else, so it doubles as a container-validity check.
WUD_IMAGE_SIZE = 0x5D3A00000  # 25,025,314,816 bytes

# Progress lines JWUDTool prints (stdout redraws with \r; the shared runner
# already normalizes those into lines). The flag marks the tool's own
# verification pass, which runs *after* the conversion and shares the bar.
_PROGRESS_PATTERNS = (
    # "Compressing into .wux | Progress 12.34% | Ratio: 1:2.10 | Read: ..."
    (re.compile(r"Progress\s+([0-9.]+)%"), False),
    # "Decompressing: 123.45MB done (12.34%)"
    (re.compile(r"Decompressing:\s*[0-9.]+MB done \(([0-9.]+)%\)"), False),
    # "Verification: 123.45MB done (12.34%)"
    (re.compile(r"Verification:\s*[0-9.]+MB done \(([0-9.]+)%\)"), True),
)
_INVALID_MARKER = "(De)Compressed file is INVALID"

logger = get_logger("jwudtool")


def split_part_index(file_path: str) -> int | None:
    """The 1-based part number if ``file_path`` is named like a split member.

    Pure name math against JNUSLib's exact template — no disk access, so it is
    safe on synthetic paths (archive members) and deterministic.
    """
    match = _WUD_SPLIT_PART_RE.match(os.path.basename(file_path))
    if match is None:
        return None
    index = int(match.group("idx"))
    if 1 <= index <= WUD_SPLIT_MAX_PARTS:
        return index
    return None


def split_set_parts(primary_path: str) -> list[str]:
    """Existing sibling parts 2…12 for a ``game_part1.wud`` primary.

    Returns ``[]`` for anything that isn't part 1, and stops at the first gap:
    JNUSLib reads the parts in order, so a set missing part 3 is broken rather
    than an 11-part set, and we must not report the stragglers as belonging to
    a usable source.
    """
    if split_part_index(primary_path) != 1:
        return []
    directory = os.path.dirname(primary_path)
    parts: list[str] = []
    for index in range(2, WUD_SPLIT_MAX_PARTS + 1):
        candidate = os.path.join(directory, WUD_SPLIT_PART_TEMPLATE % index)
        if not os.path.isfile(candidate):
            break
        parts.append(candidate)
    return parts


def split_set_is_complete(primary_path: str) -> bool:
    """Whether ``game_part1.wud``'s parts add up to a whole disc image on disk.

    A set is usable only with the exact layout JNUSLib's
    ``WUDDiscReaderSplitted`` assumes: parts running 1…N with no gap, every part
    but the last exactly ``WUD_SPLIT_PART_SIZE``, and the last one the remainder
    that brings the total to ``WUD_IMAGE_SIZE``. Half a dump (parts 1 and 2 of
    12) is named like a set but cannot produce the disc, so it must not claim
    the set's output name.

    Checking each part rather than only the total matters because the reader
    computes a part's offset from its *index*, not from the sizes of the parts
    before it: an undersized part balanced by an oversized later one sums
    correctly while every byte past the short part is misaddressed.

    Returns ``False`` for anything that isn't part 1, and for a part 1 that
    doesn't exist (an archive member's synthetic path), which is what keeps
    output detection agreeing with the archive planner.
    """
    if split_part_index(primary_path) != 1:
        return False
    total = 0
    for path in [primary_path, *split_set_parts(primary_path)]:
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        # A full 2 GiB until the remainder is smaller, which happens exactly
        # once — on the last part of a whole image. A short part anywhere else
        # leaves `total` below WUD_IMAGE_SIZE and fails the check below.
        if size != min(WUD_SPLIT_PART_SIZE, WUD_IMAGE_SIZE - total):
            return False
        total += size
    return total == WUD_IMAGE_SIZE


def is_split_secondary(file_path: str) -> bool:
    """Whether this is a non-primary member of a split set that exists on disk.

    Part 7 on its own is not a disc image; it is only meaningful alongside
    ``game_part1.wud``. A stray ``game_part7.wud`` with no part 1 beside it is
    left alone (reported convertible) so it isn't silently hidden — the
    conversion then fails with JWUDTool's own size complaint.
    """
    index = split_part_index(file_path)
    if index is None or index == 1:
        return False
    primary = os.path.join(
        os.path.dirname(file_path), WUD_SPLIT_PART_TEMPLATE % 1,
    )
    return os.path.isfile(primary)


def verification_enabled(compression: str | None) -> bool:
    """Resolve the per-job verification choice off the ``compression`` field.

    Anything other than an explicit ``noverify`` keeps JWUDTool's default
    (verify), so an absent, empty or stale preset can never silently drop the
    integrity guarantee. A ``codec:level`` shaped value is tolerated because the
    shared picker may append a level the tool ignores.
    """
    if not compression:
        return True
    return compression.partition(":")[0].strip().lower() != JWUD_NO_VERIFY


def read_wux_header(file_path: str) -> dict:
    """Parse and sanity-check a WUX container header.

    Returns the decoded header plus the derived index-table / sector-array
    geometry. Raises ``ValueError`` when the file is not a structurally valid
    WUX container (the caller turns that into a verify failure or, for
    ``info``, into "no extra detail available").

    Both size fields are pinned to the format's only real values rather than
    merely sanity-checked, which is what keeps the derived ``entry_count``
    constant: a header is free to *claim* a tiny sector size, and the index
    table's size scales inversely with it, so anything looser would let a
    crafted file dictate how much work ``verify`` does.
    """
    with open(file_path, "rb") as fh:
        header = fh.read(WUX_HEADER_SIZE)
    if len(header) < WUX_HEADER_SIZE:
        raise ValueError("file is too short to hold a WUX header")

    magic, magic1, sector_size, flags, uncompressed_size = struct.unpack(
        "<4sIIIQ", header[:0x18],
    )
    if magic != WUX_MAGIC or magic1 != WUX_MAGIC_1:
        raise ValueError("missing the WUX0 magic")
    if sector_size != WUX_SECTOR_SIZE:
        raise ValueError(
            f"declares a {sector_size}-byte sector size; WUX sectors are always "
            f"{WUX_SECTOR_SIZE} bytes",
        )
    if uncompressed_size != WUD_IMAGE_SIZE:
        raise ValueError(
            f"declares an uncompressed size of {uncompressed_size} bytes; a Wii U "
            f"disc image is always {WUD_IMAGE_SIZE} bytes",
        )

    entry_count = (uncompressed_size + sector_size - 1) // sector_size
    # The sector array begins at the next sector_size boundary past the table.
    sector_array_offset = WUX_HEADER_SIZE + entry_count * 4 + sector_size - 1
    sector_array_offset -= sector_array_offset % sector_size
    return {
        "sector_size": sector_size,
        "flags": flags,
        "uncompressed_size": uncompressed_size,
        "entry_count": entry_count,
        "index_table_offset": WUX_HEADER_SIZE,
        "sector_array_offset": sector_array_offset,
    }


def _scan_index_table(file_path: str, header: dict) -> int:
    """Return the highest sector index referenced by the WUX index table.

    Reads the table in bounded chunks (it is ~3 MB for a full disc) so a
    corrupt header can't drive an unbounded allocation. Raises ``ValueError``
    if the table itself is truncated.
    """
    entry_count = header["entry_count"]
    remaining = entry_count * 4
    highest = 0
    chunk_entries = 256 * 1024  # 1 MiB per read
    with open(file_path, "rb") as fh:
        fh.seek(header["index_table_offset"])
        while remaining > 0:
            want = min(remaining, chunk_entries * 4)
            chunk = fh.read(want)
            if len(chunk) < want:
                raise ValueError(
                    "sector index table is truncated "
                    f"({entry_count} entries declared, file ends early)",
                )
            highest = max(highest, *struct.unpack(f"<{len(chunk) // 4}I", chunk))
            remaining -= want
    return highest


class JwudToolService:
    """Wrapper for the JWUDTool CLI."""

    def __init__(self):
        self.jwudtool_path = settings.jwudtool_path
        # convert() delegates its whole streaming loop to the runner; verify is
        # pure Python (no child process), so this PID set only ever holds the
        # JWUDTool process.
        self._runner = SubprocessRunner(owner="jwud")

    # ----- command ----------------------------------------------------------

    def _build_command(
        self,
        input_path: str,
        work_dir: str,
        mode: str,
        compression: str | None = None,
    ) -> list[str]:
        """Build the JWUDTool argv.

        Format: ``jwudtool -in <image> -out <folder> <-compress|-decompress>``.
        ``-out`` is a *folder*; JNUSLib picks the filename inside it. The
        verification pass stays on unless the job explicitly asks for
        ``-noVerify``: it is the only integrity guarantee the WUX format offers,
        and it is what makes delete-on-verify safe for this tool.
        """
        cmd = [
            self.jwudtool_path,
            "-in", input_path,
            "-out", work_dir,
            "-decompress" if mode == "jwud_decompress" else "-compress",
        ]
        if not verification_enabled(compression):
            cmd.append("-noVerify")
        # Apply nice/ionice via command wrappers, NOT preexec_fn: forking a
        # Python callable in this multithreaded app can deadlock the child
        # before exec (same reason nsz/maxcso do it this way).
        return nice_prefix("jwud") + ioprio_prefix("jwud") + cmd

    def active_pids(self) -> list[int]:
        return self._runner.active_pids()

    def binary_available(self) -> bool:
        """Whether the JWUDTool launcher is present and executable.

        JWUDTool is a .jar behind a small launcher script that needs a Java
        runtime, so unlike the self-contained binaries this one can genuinely be
        absent in a local checkout. ``JwudTool.is_ready`` reports it so the UI
        hides the tool instead of offering jobs that can only fail.
        """
        path = self.jwudtool_path
        if os.sep in path:
            return os.path.isfile(path) and os.access(path, os.X_OK)
        return shutil.which(path) is not None

    # ----- output paths -----------------------------------------------------

    @staticmethod
    def output_stem(input_path: str, *, from_archive: bool = False) -> str:
        """The output filename stem for an input.

        Normally the input's own stem, but the primary of a *complete* split
        set (``game_part1.wud``) names its product after the disc, ``game.wux``,
        not ``game_part1.wux`` — the parts are one image, and the part number
        has no meaning once they're joined.

        Everything else keeps its own stem, always for the same reason: it names
        an input that cannot produce the whole-set output, so claiming
        ``game.wux`` would let an authorized overwrite unlink an unrelated
        finished image before the conversion fails.

        * A stray ``game_part7.wud`` with no part 1 beside it (deliberately
          still convertible, so JWUDTool answers with its own size complaint).
        * An incomplete set — parts 1 and 2 of a 12-part dump, or a gap in the
          middle. Named like a set, but JWUDTool will refuse to join it.
        * Any part pulled from an archive: extraction hands the converter that
          one member, never the sibling parts (``extract_related_files``
          expands ``.cue``/``.gdi`` only), so an archived set can't convert.

        Reads the sizes of the sibling parts, so call it off the event loop —
        the same constraint ``converts_path`` already carries.
        """
        stem = Path(input_path).stem
        if not from_archive and split_set_is_complete(input_path):
            return stem.rsplit("_part", 1)[0]
        return stem

    def get_output_path(self, input_path: str, output_dir: str | None = None) -> str:
        input_file = Path(input_path)
        ext = input_file.suffix.lower()
        if ext not in JWUD_OUTPUT_FORMATS:
            raise ValueError(f"Unsupported file extension: {ext}")
        output_name = self.output_stem(input_path) + JWUD_OUTPUT_FORMATS[ext]
        if output_dir:
            return str(Path(output_dir) / output_name)
        return str(input_file.parent / output_name)

    @staticmethod
    def get_output_path_for_mode(
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        """Output path for a JWUDTool mode; both directions map purely on the
        input extension via ``JWUD_OUTPUT_FORMATS``.

        ``treat_as_stem`` is accepted for interface parity with z3ds/nsz and
        needs no separate branch: archive members arrive as flattened filenames
        that keep their original extension (see
        ``ArchiveService._output_name_for_member``), so the suffix lookup maps
        them exactly like an on-disk file (.wud -> .wux and back).
        """
        input_p = Path(input_path)
        ext = input_p.suffix.lower()
        output_ext = JWUD_OUTPUT_FORMATS.get(ext)
        if output_ext is None:
            raise ValueError(f"Unsupported file extension: {ext}")
        filename = (
            f"{JwudToolService.output_stem(input_path, from_archive=treat_as_stem)}"
            f"{output_ext}"
        )
        if output_dir:
            return str(Path(output_dir) / filename)
        return str(input_p.parent / filename)

    @staticmethod
    def is_convertible(filename: str) -> bool:
        return Path(filename).suffix.lower() in JWUD_OUTPUT_FORMATS

    # ----- convert ----------------------------------------------------------

    async def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str = "jwud_compress",
        *,
        # WUX has no codec or level; the field carries the per-job verification
        # choice instead ("verify" / "noverify"), like nsz's solid/block.
        compression: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Run JWUDTool on ``input_path``, yielding progress dicts.

        ``mode`` selects the direction (``jwud_compress`` packs a .wud into a
        .wux; ``jwud_decompress`` restores the raw .wud). Honors
        ``cancel_event`` and the shared stall timeout via the runner, and yields
        a final 100 % once the produced file is in place.
        """
        produced_name = _PRODUCED_NAME.get(mode)
        if produced_name is None:
            raise ValueError(f"Unsupported JWUDTool mode: {mode}")
        if not self.binary_available():
            raise RuntimeError(
                f"JWUDTool is not available (looked for {self.jwudtool_path}). It "
                "needs a Java runtime; set JWUDTOOL_PATH to the launcher if it "
                "lives elsewhere.",
            )

        verb = "decompression" if mode == "jwud_decompress" else "compression"
        out_dir = os.path.dirname(output_path) or "."
        await asyncio.to_thread(os.makedirs, out_dir, exist_ok=True)

        # Private temp dir on the destination filesystem: JWUDTool names the
        # file itself, so moving the finished image onto output_path stays a
        # cheap rename rather than a 25 GB copy.
        work_dir = await asyncio.to_thread(
            tempfile.mkdtemp, prefix=".jwud-", dir=out_dir,
        )
        produced_path = os.path.join(work_dir, produced_name)
        try:
            async for update in self._run_convert(
                input_path, produced_path, work_dir, mode, verb, cancel_event,
                compression,
            ):
                yield update
            await asyncio.to_thread(os.replace, produced_path, output_path)
            yield {"progress": 100, "message": f"Wii U {verb} complete"}
        finally:
            await asyncio.to_thread(shutil.rmtree, work_dir, True)

    async def _run_convert(
        self, input_path, produced_path, work_dir, mode, verb, cancel_event,
        compression=None,
    ) -> AsyncGenerator[dict, None]:
        cmd = self._build_command(input_path, work_dir, mode, compression)
        verifying = verification_enabled(compression)
        # JWUDTool keeps a 0 exit code when its own verification pass fails, so
        # the marker line is the only signal; collect it while parsing progress
        # and raise after the run rather than trusting the return code alone.
        invalid: list[str] = []

        def _parse_progress(line: str) -> int | None:
            if _INVALID_MARKER in line:
                invalid.append(line)
                return None
            for pattern, is_verify_phase in _PROGRESS_PATTERNS:
                match = pattern.search(line)
                if match is None:
                    continue
                try:
                    pct = float(match.group(1))
                except ValueError:
                    return None
                pct = min(100.0, max(0.0, pct))
                # Two phases share one bar when verification is on: the
                # conversion runs 1-50 %, the tool's byte-for-byte pass 51-99 %.
                # With -noVerify there is only one phase, so the conversion gets
                # the whole 1-99 % rather than stopping dead at half. The runner
                # emits the terminal 100 % itself (suppressed below, since
                # convert() only reaches 100 % after the file is moved).
                if not verifying:
                    return 1 + int(pct * 98 / 100)
                base, span = (51, 48) if is_verify_phase else (1, 49)
                return base + int(pct * span / 100)
            return None

        yield {"progress": 1, "message": f"Starting Wii U {verb}..."}

        # require_output makes a clean exit that produced nothing an error
        # carrying JWUDTool's own explanation ("Given WUD has not the expected
        # filesize", "Given image is already compressed", ...).
        async for update in self._runner.run(
            cmd,
            input_path=input_path,
            output_path=produced_path,
            parse_progress=_parse_progress,
            initial_progress=1,
            cancel_event=cancel_event,
            fail_label="JWUDTool",
            complete_message=f"Wii U {verb} complete",
            nice_via_wrapper=True,
            require_output=True,
            mode=mode,
        ):
            if update.get("progress", 0) >= 100:
                continue
            yield update

        if invalid:
            raise RuntimeError(
                "JWUDTool's verification pass reported the output as invalid "
                f"(the {'de' if mode == 'jwud_decompress' else ''}compressed image "
                f"does not match the source): {invalid[-1]}",
            )

    # ----- info -------------------------------------------------------------

    def info(self, file_path: str) -> dict:
        """Filesystem-level info plus, for a .wux, its decoded header.

        JWUDTool exposes no offline metadata dump that doesn't need keys, so the
        format/compression summary comes from the extension and the WUX header
        (which carries the original image size, hence a real compression ratio).
        Synchronous; wrap callers in a threadpool.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        file_size = os.path.getsize(file_path)
        ext = Path(file_path).suffix.lower()
        is_compressed = ext in JWUD_DECOMPRESS_EXTENSIONS
        base_format = {
            ".wud": "WUD (Wii U disc image)",
            ".wux": "WUX (compressed Wii U disc image)",
        }.get(ext)

        original_size = None
        ratio = None
        if is_compressed:
            # A malformed header only costs the extras; verify is where a bad
            # container is reported as such.
            with contextlib.suppress(OSError, ValueError):
                original_size = read_wux_header(file_path)["uncompressed_size"]
            if original_size:
                ratio = f"{file_size / original_size * 100:.1f}%"

        size_mb = file_size / (1024 * 1024)
        size_display = f"{size_mb:.2f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB"

        return {
            "file": file_path,
            "size": file_size,
            "size_display": size_display,
            "format": base_format,
            "extension": ext,
            "compressed": is_compressed,
            "compression_type": "WUX (sector deduplication)" if is_compressed else None,
            "original_size": original_size,
            "ratio": ratio,
        }

    # ----- verify -----------------------------------------------------------

    async def verify(
        self, file_path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> dict:
        return await collect_verify(
            self.verify_stream(file_path, cancel_event=cancel_event),
            fallback_message="Verification failed",
        )

    async def verify_stream(
        self, file_path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Validate a .wux container structurally.

        Checks the magic and header fields, then walks every sector-index entry
        and confirms the highest one it references actually exists in the file.
        That is what catches the two failure modes a 25 GB image really hits — a
        truncated copy and a corrupt index table — and it is the deepest check
        the format allows: WUX carries no content checksums.

        The two heavy steps run in the threadpool and cannot be interrupted
        mid-read, so ``cancel_event`` is honoured *between* them: a cancel is
        observed within one index scan rather than at the end of the job.
        """
        def _cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        cancelled_event = {
            "type": "error",
            "valid": False,
            "cancelled": True,
            "message": "Verification cancelled",
        }

        # Bounded, off the event loop: an unresponsive volume must fail this
        # verify, not freeze every task in the process (see verify_preflight).
        # It also hands back the size the truncation check below compares against.
        problem, file_size = await verify_preflight(
            file_path, JWUD_DECOMPRESS_EXTENSIONS,
        )
        if problem is not None:
            yield problem
            return

        try:
            if _cancelled():
                yield cancelled_event
                return
            yield {"type": "progress", "progress": 0, "message": "Reading WUX header..."}
            try:
                header = await run_in_threadpool(read_wux_header, file_path)
            except ValueError as e:
                yield {
                    "type": "error",
                    "valid": False,
                    "message": f"Not a valid WUX container: {e}",
                }
                return

            if _cancelled():
                yield cancelled_event
                return
            yield {
                "type": "progress",
                "progress": 25,
                "message": f"Checking {header['entry_count']} sector index entries...",
            }
            try:
                highest = await run_in_threadpool(_scan_index_table, file_path, header)
            except ValueError as e:
                yield {"type": "error", "valid": False, "message": f"Corrupt WUX: {e}"}
                return

            if _cancelled():
                yield cancelled_event
                return

            required = header["sector_array_offset"] + (highest + 1) * header["sector_size"]
            if file_size < required:
                yield {
                    "type": "error",
                    "valid": False,
                    "message": (
                        "WUX is truncated: the index table references sector "
                        f"{highest}, which needs at least {required} bytes, but the "
                        f"file is {file_size} bytes"
                    ),
                }
                return

            yield {"type": "progress", "progress": 100, "message": "Integrity check passed"}
            yield {
                "type": "complete",
                "valid": True,
                "message": (
                    f"WUX container verified: {header['entry_count']} sector index "
                    f"entries over {highest + 1} stored sectors"
                ),
            }
        except Exception as e:
            logger.exception("Error during Wii U verification: %s", e)
            yield {"type": "error", "valid": False, "message": f"Verification error: {e}"}


# Global service instance
jwudtool_service = JwudToolService()
