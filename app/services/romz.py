"""Wrapper for the ``7z`` CLI, packing handheld ROM dumps into archives.

This tool losslessly compresses Game Boy, Game Boy Color, Game Boy Advance and
Nintendo DS ROM dumps (``.gb`` / ``.gbc`` / ``.gba`` / ``.nds``) into standard
``.7z`` (LZMA2) or ``.zip`` (deflate) archives, and extracts them back to the
exact original ROM. The ``7z`` binary ships in the image already (``p7zip-full``),
so no extra build step is needed; the read/metadata side reads members through
the shared, mtime-cached ``services.archive_members`` reader.

Three modes:

- ``romz_7z``      ``.gb``/``.gbc``/``.gba``/``.nds`` -> ``<name>.7z``
- ``romz_zip``     ``.gb``/``.gbc``/``.gba``/``.nds`` -> ``<name>.zip``
- ``romz_extract`` ``.7z``/``.zip`` (single ROM member) -> original ROM

Output names preserve the ROM extension (``Game.gba`` -> ``Game.gba.7z``), so the
round-trip is deterministic in both directions: extract simply restores the
single member. Compression strength is a Fast / Default / Max effort preset; Max
mirrors the reference ``-mx=9 -md=256m -mfb=273 -m0=lzma2`` profile.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import tempfile
from collections.abc import AsyncGenerator
from pathlib import Path

from config import settings
from logging_setup import get_logger
from services.archive import archive_service
from services.archive_members import read_archive_members
from services.subprocess_runner import (
    ConversionCancelled,
    SubprocessRunner,
    collect_verify,
    ioprio_prefix,
)
from utils.junk import is_junk_path

# SubprocessRunner "owner" for the shared priority/timeout policy. An optional
# COMPRESSATORIUM_ROMZ_* override takes precedence over the tool-neutral
# COMPRESSATORIUM_TOOL_* default (see services/subprocess_runner.py).
_OWNER = "romz"

# Compress takes a loose ROM; extract takes one of the archives this tool writes.
ROMZ_COMPRESS_EXTENSIONS = {".gb", ".gbc", ".gba", ".nds"}
ROMZ_ARCHIVE_EXTENSIONS = {".7z", ".zip"}

# Output container is decided by the mode, not the input ROM. The ROM extension
# is preserved *in front of* the archive suffix (Game.gba -> Game.gba.7z) so the
# extract direction is a pure suffix strip and same-stem ROMs of different
# platforms (Game.gb vs Game.gba) never collide.
ROMZ_OUTPUT_BY_MODE = {
    "romz_7z": ".7z",
    "romz_zip": ".zip",
}
ROMZ_COMPRESS_MODES = frozenset(ROMZ_OUTPUT_BY_MODE)
ROMZ_EXTRACT_MODE = "romz_extract"

_ROM_FORMAT_LABELS = {
    ".gb": "Game Boy ROM",
    ".gbc": "Game Boy Color ROM",
    ".gba": "Game Boy Advance ROM",
    ".nds": "Nintendo DS ROM",
}
_ARCHIVE_FORMAT_LABELS = {
    ".7z": "7-Zip archive",
    ".zip": "ZIP archive",
}
_ARCHIVE_COMPRESSION_LABELS = {
    ".7z": "7-Zip (LZMA2)",
    ".zip": "ZIP (Deflate)",
}

# 7z prints progress as a leading "NN%" token when -bsp1 is set; parse it for the
# streamed progress bar. Output-file growth is the fallback the runner watches.
_PCT_RE = re.compile(r"(\d{1,3})%")

logger = get_logger("romz")


def _parse_progress(line: str) -> int | None:
    match = _PCT_RE.search(line)
    if not match:
        return None
    value = int(match.group(1))
    return value if 0 <= value <= 100 else None


def _compress_flags(mode: str, compression: str | None) -> list[str]:
    """7z switches for an effort preset (fast | default | max)."""
    effort = (compression or "max").strip().lower()
    if mode == "romz_zip":
        level = {"fast": "1", "default": "7", "max": "9"}.get(effort, "9")
        return ["-tzip", f"-mx={level}", "-mmt=on"]
    # romz_7z (LZMA2)
    if effort == "fast":
        return ["-t7z", "-m0=lzma2", "-mx=1", "-mmt=on"]
    if effort == "default":
        return ["-t7z", "-m0=lzma2", "-mx=7", "-md=64m", "-mmt=on"]
    # "max" (and any unknown token) -> the reference best-settings profile.
    return ["-t7z", "-m0=lzma2", "-mx=9", "-md=256m", "-mfb=273", "-mmt=on"]


class RomzService:
    """Wrapper for the 7z binary, packing/unpacking handheld ROM dumps."""

    def __init__(self):
        self.sevenzip_path = settings.sevenzip_path
        self._runner = SubprocessRunner(owner=_OWNER)

    # ----- archive member listing (read side; shared cached reader) ---------

    @staticmethod
    def _list_entries(archive_path: str) -> list[tuple[str, int, bool]]:
        """Return ``(internal_path, uncompressed_size, is_dir)`` for every member.

        Reads through the shared, mtime-cached
        :func:`services.archive_members.read_archive_members` so the file listing
        doesn't re-open an archive this tool's single-ROM gate already inspected
        (the archive summary path reads the same cache). Only ``.zip``/``.7z`` are
        supported here — the only containers romz produces and reverts — so a
        ``.rar`` raises rather than being silently treated as romz-verifiable.

        Rejects archives containing a **symlink** member up front. This tool only
        ever packs a single loose ROM file, so a symlink masquerading as the ROM
        (e.g. ``Game.gba -> /etc/passwd``) is malicious: ``7z x`` would restore it
        as a link and the later move / ``7z t`` would treat the link as the ROM.

        Directory entries are returned (``is_dir=True``) so callers can count them
        against the archive-entry limits — ``7z x`` materializes every directory
        under the temp root, so they must count toward CHD_ARCHIVE_MAX_ENTRIES.
        """
        ext = Path(archive_path).suffix.lower()
        if ext not in (".zip", ".7z"):
            raise ValueError(f"Unsupported archive extension: {ext}")
        entries: list[tuple[str, int, bool]] = []
        for member in read_archive_members(archive_path):
            if member.is_symlink:
                raise ValueError("Archive contains a symlink member")
            # size is None only when the container omits an uncompressed size;
            # treat that as 0 for the entry/limit accounting (matches the prior
            # ``entry.uncompressed or 0``).
            entries.append((member.name, int(member.size or 0), member.is_dir))
        return entries

    @classmethod
    def _list_members(cls, archive_path: str) -> list[tuple[str, int]]:
        """File members ``(internal_path, uncompressed_size)``.

        Directories are dropped and symlink members are rejected (see
        :meth:`_list_entries`); the ROM payload is always a regular file.
        """
        return [
            (name, size)
            for name, size, is_dir in cls._list_entries(archive_path)
            if not is_dir
        ]

    @staticmethod
    def _resolve_single_rom(
        raw_members: list[tuple[str, int]],
    ) -> tuple[str, int] | None:
        """Return ``(name, size)`` of the sole handheld-ROM payload, or ``None``.

        Applies the shared junk filter then enforces the single-ROM invariant:
        exactly one non-junk member and it is a ROM. Pure (no I/O, no limit
        enforcement) so extract validation, member naming, and ``info()`` all
        agree on what counts as a single-ROM archive. OS/NAS clutter that zip
        tools tuck alongside the ROM (``__MACOSX/._Game.gba``, ``.DS_Store``,
        ``@eaDir/…``, …) is ignored via the shared junk filter.
        """
        members = [
            (name, size) for name, size in raw_members
            if not is_junk_path(name)
        ]
        roms = [
            (name, size) for name, size in members
            if Path(name).suffix.lower() in ROMZ_COMPRESS_EXTENSIONS
        ]
        if len(roms) == 1 and len(members) == 1:
            return roms[0]
        return None

    @classmethod
    def is_single_rom_archive(cls, archive_path: str) -> bool:
        """True when ``archive_path`` is one romz can actually verify/extract.

        The quiet, boolean form of the single-ROM invariant verify/extract
        enforce, used by the file listing to gate the romz Verify/Info
        row-actions to archives this tool can actually handle — instead of
        offering them on *every* ``.7z``/``.zip`` purely on extension. It
        delegates to :meth:`_single_rom_member` so the gate applies the **same**
        acceptance criteria as the verify/extract paths: exactly one handheld-ROM
        payload, no symlink member, no traversal/absolute member path, and within
        the shared archive entry/size limits. Anything those paths would reject
        (and any unreadable/corrupt archive) is simply not romz-ready — never
        raises, returns ``False``. The underlying member read is mtime-cached by
        :func:`services.archive_members.read_archive_members`, so repeated listing
        calls don't re-open the archive.
        """
        try:
            cls._single_rom_member(archive_path)
        except Exception:  # unreadable/corrupt, multi-file, unsafe, over-limit
            return False
        return True

    @staticmethod
    def _reject_traversal(name: str) -> None:
        """Reject a member that could escape or collide inside the temp dir.

        Deliberately narrower than ``ArchiveService._validate_member`` (which
        also bans ``:`` and ``\\`` for Windows portability): on a POSIX volume a
        loose ROM may legally be named e.g. ``Game:1.gba`` or ``Game\\x.gba``,
        and this tool must be able to round-trip the archive it produced
        (verify / extract reuse this gate). ``7z x`` on POSIX treats those as
        ordinary filename characters.

        Reject an absolute path or **any** ``..`` component — not just paths
        whose normalized form still escapes the root. ``7z x -y`` recreates the
        literal path, so a sidecar like ``__MACOSX/../Game.gba`` (which
        normalizes to ``Game.gba``) would resolve onto the validated ROM's temp
        path and overwrite it before it is published.
        """
        stripped = name.rstrip("/")
        # POSIX separator only: `\\` is a legal filename character here, not a
        # path component boundary.
        if stripped.startswith("/") or ".." in stripped.split("/"):
            raise ValueError(f"Unsafe archive member path: {name}")

    @classmethod
    def _single_rom_member(cls, archive_path: str) -> str:
        """Return the sole ROM member of ``archive_path`` or raise a clear error.

        The extract mode reverts this tool's own single-ROM archives, so anything
        but exactly one handheld-ROM payload is a hard error rather than a silent
        partial extract.
        """
        entries = cls._list_entries(archive_path)  # raises on a symlink member
        # Guard against oversized archives / zip bombs with the same limits the
        # archive service applies, before extracting via 7z. Count EVERY entry
        # (junk and directories included): `7z x` materializes the whole tree
        # under the temp root, so directory entries consume the inode budget too.
        archive_service.enforce_archive_limits([(n, s) for n, s, _ in entries])
        # Extraction runs `7z x` (preserve paths) over EVERY member, so any
        # member — the ROM or an ignored junk entry like
        # ``__MACOSX/../../victim.gba`` — with an absolute or ``..`` path could
        # write outside the temp dir. Reject such archives up front (this also
        # gates verify, which reuses this method).
        for name, _s, _d in entries:
            cls._reject_traversal(name)
        file_members = [(n, s) for n, s, is_dir in entries if not is_dir]
        resolved = cls._resolve_single_rom(file_members)
        if resolved is not None:
            return resolved[0]
        # Distinguish "no ROM" from "more than one payload" for a clear message.
        has_rom = any(
            Path(name).suffix.lower() in ROMZ_COMPRESS_EXTENSIONS
            for name, _ in file_members
            if not is_junk_path(name)
        )
        if not has_rom:
            raise ValueError(
                "Archive holds no Game Boy / GBA / DS ROM to extract",
            )
        raise ValueError(
            "Archive holds more than one file; the extract mode only reverts "
            "single-ROM archives produced by this tool",
        )

    # ----- command ----------------------------------------------------------

    def _build_command(
        self, input_arg: str, output_target: str, mode: str,
        compression: str | None,
    ) -> list[str]:
        # All switches go before a literal ``--`` so positional names that begin
        # with ``-`` (an archive/member/ROM whose filename starts with a dash)
        # are treated as filenames, not 7-Zip switches. For compress
        # ``output_target`` is the archive file and ``input_arg`` is the ROM's
        # basename (the caller runs 7z with cwd set to the ROM's directory so the
        # archive stores just ``Game.gba``, not the absolute volume path); for
        # extract ``output_target`` is the extraction ROOT directory and
        # ``input_arg`` is the archive path.
        if mode in ROMZ_COMPRESS_MODES:
            switches = _compress_flags(mode, compression) + ["-bsp1", "-y"]
            cmd = [self.sevenzip_path, "a", *switches, "--", output_target, input_arg]
        elif mode == ROMZ_EXTRACT_MODE:
            # `-o` is the extraction ROOT: `7z x` recreates each member's own
            # relative path under it, so the validated member lands at
            # ``<root>/<member>`` (the caller's runner_output) — including ROMs
            # stored under a top-level folder.
            switches = [f"-o{output_target}", "-bsp1", "-y"]
            # Extract the whole archive (no positional member selector) with
            # `x`, which PRESERVES member paths. Two reasons we don't name the
            # member or use `e` (flatten):
            #  * 7-Zip reads a leading ``@`` as a list-file reference even after
            #    ``--``, so a ROM named e.g. ``@Game.gba`` would be misread.
            #  * `e` flattens every member to its basename, so an ignored junk
            #    member sharing the ROM's basename (``__MACOSX/Game.gba``,
            #    ``@eaDir/Game.gba``) would, under ``-y``, overwrite the real
            #    ROM at the same temp path before we move it.
            # Member paths are validated safe in `_single_rom_member`, so `x`
            # cannot write outside the temp root. The caller moves the one
            # validated member out and discards the rest with the temp dir.
            cmd = [self.sevenzip_path, "x", *switches, "--", input_arg]
        else:
            raise ValueError(f"Unsupported romz mode: {mode}")
        # Apply ionice via a command wrapper (the shared nice level is applied by
        # SubprocessRunner.run's preexec); both honor the COMPRESSATORIUM_ROMZ_*
        # overrides, falling back to the tool-neutral policy.
        return ioprio_prefix(_OWNER) + cmd

    def active_pids(self) -> list[int]:
        return self._runner.active_pids()

    # ----- output paths -----------------------------------------------------

    @classmethod
    def get_output_path_for_mode(
        cls,
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        """Output path for a romz mode.

        Compress preserves the ROM extension in front of the archive suffix
        (``Game.gba`` -> ``Game.gba.7z``). Extract restores the archived ROM's
        own basename (peeked from the archive, falling back to a suffix strip so
        a caller can still compute a path for an unreadable file).
        ``treat_as_stem`` is accepted for interface parity; romz never takes
        archive members as input.
        """
        input_p = Path(input_path)
        if mode in ROMZ_COMPRESS_MODES:
            filename = f"{input_p.name}{ROMZ_OUTPUT_BY_MODE[mode]}"
        elif mode == ROMZ_EXTRACT_MODE:
            try:
                filename = os.path.basename(cls._single_rom_member(input_path))
            except Exception:
                # Fall back to stripping the archive suffix (matches this tool's
                # own naming convention) when the archive can't be read or is
                # corrupt. Broad by design: zipfile.BadZipFile / py7zr errors
                # don't derive from OSError, and this is a best-effort path
                # computation, not the place to surface a read failure.
                filename = input_p.stem
        else:
            raise ValueError(f"Unsupported romz mode: {mode}")
        if output_dir:
            return str(Path(output_dir) / filename)
        return str(input_p.parent / filename)

    # ----- convert ----------------------------------------------------------

    async def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str = "romz_7z",
        *,
        compression: str | None = None,  # effort preset: fast | default | max
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        member: str | None = None
        # Where the 7z subprocess actually writes (and what the runner watches).
        # For compress this is output_path; for extract it's a temp file we then
        # move to output_path.
        runner_output = output_path
        # What `_build_command` targets: the archive file for compress, the
        # extraction ROOT dir for extract.
        cmd_target = output_path
        # The positional name 7z adds/reads, and the cwd it runs from.
        cmd_input = input_path
        run_cwd: str | None = None
        extract_tmp_dir: str | None = None
        if mode in ROMZ_COMPRESS_MODES:
            complete_message = "Compression complete"
            # `7z a` APPENDS to an existing archive, so clear any stale/partial
            # output first to avoid silently merging into it. suppress(OSError)
            # covers the already-absent case (FileNotFoundError) without a
            # separate stat on the event loop.
            with contextlib.suppress(OSError):
                await asyncio.to_thread(os.remove, output_path)
            # 7z stores the path *as given on the command line* (minus the
            # root). Run it from the ROM's directory and add only the basename so
            # the archive holds a single root-level ``Game.gba`` instead of the
            # absolute volume layout (``games/gba/Game.gba``). output_path stays
            # absolute, so the archive is still written where planned. Prefix the
            # basename with ``./``: 7z reads an argument starting with ``@`` as a
            # list-file (even after ``--``), so a ROM literally named ``@Game.gba``
            # would otherwise be misread; ``./`` keeps it a plain path.
            run_cwd = os.path.dirname(input_path) or "."
            cmd_input = os.path.join(".", os.path.basename(input_path))
        elif mode == ROMZ_EXTRACT_MODE:
            complete_message = "Extraction complete"
            member = await asyncio.to_thread(self._single_rom_member, input_path)
            # `7z x` writes each member at its own relative path under the output
            # dir, so it can't honor a renamed target (duplicate_action=rename
            # -> Game_1.gba) and could collide with a sibling. Extract into an
            # isolated temp dir on the same filesystem, then atomically move the
            # validated member (at its preserved relative path) to output_path.
            final_dir = os.path.dirname(output_path) or "."
            await asyncio.to_thread(os.makedirs, final_dir, exist_ok=True)
            extract_tmp_dir = await asyncio.to_thread(
                tempfile.mkdtemp, prefix=".romz-extract-", dir=final_dir,
            )
            # `-o` is the temp ROOT; `7z x` recreates the member's relative path
            # under it, so the ROM lands exactly at runner_output.
            cmd_target = extract_tmp_dir
            runner_output = os.path.join(extract_tmp_dir, member)
        else:
            raise ValueError(f"Unsupported romz mode: {mode}")

        cmd = self._build_command(
            cmd_input, cmd_target, mode, compression,
        )

        try:
            async for update in self._runner.run(
                cmd,
                input_path=input_path,
                output_path=runner_output,
                parse_progress=_parse_progress,
                cancel_event=cancel_event,
                cwd=run_cwd,
                fail_label="7z",
                complete_message=complete_message,
                mode=mode,
            ):
                yield update
            if mode == ROMZ_EXTRACT_MODE:
                # Defense in depth: never publish a symlink as the ROM. Symlink
                # members are already rejected at listing time, but re-check the
                # actual extracted path so a crafted archive whose metadata hid
                # the link type can't slip a link into the library. The except
                # below cleans up the link.
                if await asyncio.to_thread(os.path.islink, runner_output):
                    raise ValueError("Extracted member is a symlink, not a ROM")
                # Move the extracted ROM to the planned (possibly renamed)
                # destination. os.replace is atomic within the same filesystem.
                await asyncio.to_thread(os.replace, runner_output, output_path)
        except BaseException:
            # Any failure/cancel can leave a partial archive or ROM on disk; the
            # runner doesn't own output_path, so clean it up here so a retry
            # isn't blocked by (or silently trusts) a truncated file. Off the
            # event loop like the rest of this method's filesystem ops.
            # suppress(OSError) covers the already-absent case.
            with contextlib.suppress(OSError):
                await asyncio.to_thread(os.remove, runner_output)
            if runner_output != output_path:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(os.remove, output_path)
            raise
        finally:
            if extract_tmp_dir is not None:
                await asyncio.to_thread(
                    shutil.rmtree, extract_tmp_dir, ignore_errors=True,
                )

    # ----- info -------------------------------------------------------------

    def info(self, file_path: str) -> dict:
        """Filesystem-level info plus, for archives, the contained ROM + ratio.

        Synchronous; wrap callers in a threadpool.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        file_size = os.path.getsize(file_path)
        ext = Path(file_path).suffix.lower()
        is_archive = ext in ROMZ_ARCHIVE_EXTENSIONS

        contained_name: str | None = None
        original_size: int | None = None
        ratio: str | None = None
        if is_archive:
            try:
                members = self._list_members(file_path)
            except Exception as exc:  # unreadable/corrupt archive
                logger.debug("romz info: failed to list %s: %s", file_path, exc)
                members = []
            # Only surface ROM-specific fields when this is actually a single-ROM
            # archive (same invariant as extract/verify). An ordinary archive
            # (readme.txt + artwork, multi-file dumps) falls back to basic
            # archive info rather than mislabelling its first member as a ROM.
            rom = self._resolve_single_rom(members)
            if rom is not None:
                name, rom_size = rom
                contained_name = os.path.basename(name)
                if rom_size > 0:
                    original_size = rom_size
                    ratio = f"{file_size / rom_size * 100:.1f}%"

        size_mb = file_size / (1024 * 1024)
        size_display = (
            f"{size_mb:.2f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB"
        )

        return {
            "file": file_path,
            "size": file_size,
            "size_display": size_display,
            "format": (
                _ARCHIVE_FORMAT_LABELS.get(ext)
                if is_archive
                else _ROM_FORMAT_LABELS.get(ext)
            ),
            "extension": ext,
            "compressed": is_archive,
            "compression_type": (
                _ARCHIVE_COMPRESSION_LABELS.get(ext) if is_archive else None
            ),
            "contained_name": contained_name,
            "original_size": original_size,
            "ratio": ratio,
        }

    @staticmethod
    def is_convertible(filename: str) -> bool:
        ext = Path(filename).suffix.lower()
        return ext in ROMZ_COMPRESS_EXTENSIONS or ext in ROMZ_ARCHIVE_EXTENSIONS

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
        """Verify an archive by running ``7z t`` (tests every member's CRC).

        A clean (exit 0) run proves the archive decompresses intact, the analog
        of maxcso ``--crc`` / nsz ``-V`` / z3ds ``zstd -t``. The run is the
        shared :meth:`SubprocessRunner.capture_verify`, which applies the shared
        nice/ionice policy, the size-scaled verify bound, and ``cancel_event``.
        """
        if not os.path.exists(file_path):
            yield {"type": "error", "valid": False, "message": "File not found"}
            return
        try:
            is_empty = os.path.getsize(file_path) == 0
        except OSError as e:
            yield {"type": "error", "valid": False, "message": f"Error reading file: {e}"}
            return
        if is_empty:
            yield {"type": "error", "valid": False, "message": "File is empty"}
            return
        ext = Path(file_path).suffix.lower()
        if ext not in ROMZ_ARCHIVE_EXTENSIONS:
            yield {"type": "error", "valid": False, "message": f"Invalid extension: {ext}"}
            return

        # Verify only certifies this tool's own single-ROM archives. Routing is
        # extension-based, so without this an arbitrary multi-file/source zip
        # would pass `7z t` and persist a misleading "Verified" state. This also
        # applies the shared archive size/entry/path limits before testing.
        # Off the event loop: listing a large/NAS-backed archive must not stall
        # the sync, SSE, and batch verify routes that consume this on the loop.
        try:
            await asyncio.to_thread(self._single_rom_member, file_path)
        except ValueError as exc:
            yield {"type": "error", "valid": False, "message": str(exc)}
            return
        except Exception as exc:  # unreadable/corrupt archive
            yield {"type": "error", "valid": False, "message": f"Cannot read archive: {exc}"}
            return

        # `--` keeps an archive name beginning with `-` a positional filename.
        async for update in self._runner.capture_verify(
            [self.sevenzip_path, "t", "--", file_path],
            path=file_path,
            success_message="File verified successfully",
            cancel_event=cancel_event,
        ):
            yield update


# Global service instance
romz_service = RomzService()


# Re-export for callers that catch cancellation from this module.
__all__ = [
    "ROMZ_ARCHIVE_EXTENSIONS",
    "ROMZ_COMPRESS_EXTENSIONS",
    "ROMZ_OUTPUT_BY_MODE",
    "ConversionCancelled",
    "romz_service",
]
