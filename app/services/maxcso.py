"""Wrapper for the ``maxcso`` tool (https://github.com/unknownbrackets/maxcso).

maxcso losslessly compresses PSP/PS2 ``.iso`` disc images into every format it
writes -- CSO v1, CSO v2, ZSO and DAX -- and decompresses them back. PPSSPP and
PCSX2 read these directly, so this is a native, emulator-friendly compression
target that needs no keys. CSO v1 and v2 share the ``.cso`` extension; the
version differs internally and is selected by the ``--format`` flag.

Five modes:

- ``cso_compress``   ``.iso`` -> ``.cso``  (``maxcso <in> -o <out>``; cso1 default)
- ``cso2_compress``  ``.iso`` -> ``.cso``  (``maxcso --format=cso2 <in> -o <out>``)
- ``zso_compress``   ``.iso`` -> ``.zso``  (``maxcso --format=zso <in> -o <out>``)
- ``dax_compress``   ``.iso`` -> ``.dax``  (``maxcso --format=dax <in> -o <out>``)
- ``cso_decompress`` ``.cso``/``.zso``/``.dax`` -> ``.iso`` (``maxcso --decompress``)

maxcso writes the output file directly (``-o``), so unlike nsz there is no work
dir to manage. Progress is estimated from the growth of the output file (maxcso
draws a TTY progress bar that goes silent on a pipe), like the z3ds service.
Verification runs ``maxcso --crc`` which decompresses the whole file and logs
its CRC32, ignoring output; a clean exit means the container decompresses
intact (the analog of nsz ``-V`` / z3ds ``zstd -t``).
"""
import asyncio
import os
import struct
from collections.abc import AsyncGenerator
from pathlib import Path

from config import settings
from logging_setup import get_logger
from services.chdman import ConversionCancelled
from services.subprocess_runner import (
    SubprocessRunner,
    collect_verify,
    ioprio_prefix,
    nice_prefix,
    remove_partial_output,
    verify_preflight,
)

# SubprocessRunner "owner" for the shared priority/timeout policy. An optional
# COMPRESSATORIUM_MAXCSO_* override takes precedence over the tool-neutral
# COMPRESSATORIUM_TOOL_* default (see services/subprocess_runner.py).
_OWNER = "maxcso"

# Compress takes a raw .iso; decompress takes any maxcso-produced container.
MAXCSO_COMPRESS_EXTENSIONS = {".iso"}
MAXCSO_DECOMPRESS_EXTENSIONS = {".cso", ".zso", ".dax"}


def uncompressed_iso_size(path: str) -> int | None:
    """Read the uncompressed ISO size (bytes) from a CSO/ZSO/DAX header.

    A highly compressed container can be a small fraction of the ISO maxcso
    will write, so estimating disk headroom from the compressed file size badly
    under-counts. The uncompressed size lives in the container header:

    - CSO (``CISO``) / ZSO (``ZISO``): ``uint64`` at offset 8.
    - DAX (``DAX\\0``): ``uint32`` at offset 4.

    Returns ``None`` for an unknown/unreadable header (caller falls back to a
    ratio-based estimate).
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return None
    if len(head) < 16:
        return None
    try:
        if head[:4] in (b"CISO", b"ZISO"):
            return int(struct.unpack_from("<Q", head, 8)[0]) or None
        if head[:4] == b"DAX\x00":
            return int(struct.unpack_from("<I", head, 4)[0]) or None
    except struct.error:
        return None
    return None

# Output extension is decided by the mode, not the input extension (an .iso can
# become .cso/.zso/.dax), so the map is keyed by mode rather than suffix. CSO v1
# and CSO v2 share the .cso container extension; the version differs internally
# and is selected by the --format flag in _build_command.
MAXCSO_OUTPUT_BY_MODE = {
    "cso_compress": ".cso",
    "cso2_compress": ".cso",
    "zso_compress": ".zso",
    "dax_compress": ".dax",
    "cso_decompress": ".iso",
}

# Rough output:input size ratios, used only to smooth the progress bar.

# Compression-effort presets. The UI sends one of these tokens (see the `cso`
# entry in src/lib/tools/registry.js); each maps to the maxcso trial flags that
# trade speed for ratio. "default" keeps maxcso's own default (zlib + 7zdeflate
# for the deflate-based CSO/CSO2/DAX formats, lz4hc for ZSO) and adds nothing.
# lz4-based ZSO can't use the deflate trials, so "max" bruteforces lz4 there
# instead; the deflate-based formats get Zopfli + libdeflate trials.
_EFFORT_FAST = "fast"
_EFFORT_MAX = "max"


def _effort_flags(mode: str, compression: str | None) -> list[str]:
    """maxcso trial flags for a compression-effort token (compress modes only)."""
    if mode == "cso_decompress" or not compression:
        return []
    effort = compression.strip().lower()
    if effort == _EFFORT_FAST:
        return ["--fast"]
    if effort == _EFFORT_MAX:
        if mode == "zso_compress":
            return ["--use-lz4brute"]
        return ["--use-zopfli", "--use-libdeflate"]
    return []  # "default"/"none"/unknown -> maxcso defaults


logger = get_logger("maxcso")


class MaxcsoService:
    """Wrapper for the maxcso binary."""

    def __init__(self):
        self.maxcso_path = settings.maxcso_path
        # convert() delegates its streaming loop to the runner; verify_stream()
        # still spawns maxcso --crc directly but tracks its PID in the same set,
        # so active_pids() reflects both.
        self._runner = SubprocessRunner(owner=_OWNER)

    # ----- command ----------------------------------------------------------

    def _build_command(
        self, input_path: str, output_path: str, mode: str,
        compression: str | None = None,
    ) -> list[str]:
        if mode not in MAXCSO_OUTPUT_BY_MODE:
            raise ValueError(f"Unsupported maxcso mode: {mode}")
        cmd = [self.maxcso_path]
        if mode == "cso_decompress":
            cmd.append("--decompress")
        elif mode == "cso2_compress":
            cmd += ["--format=cso2"]
        elif mode == "zso_compress":
            cmd += ["--format=zso"]
        elif mode == "dax_compress":
            cmd += ["--format=dax"]
        # cso_compress uses the default cso1 format (no flag).
        cmd += _effort_flags(mode, compression)
        cmd += [input_path, "-o", output_path]

        # Apply nice/ionice via command wrappers, NOT preexec_fn: forking a
        # Python callable in a multithreaded app (this one uses threadpools) can
        # deadlock the child before exec. `nice`/`ionice` are exec-only. The
        # priority policy is the shared tool-neutral one (with optional
        # COMPRESSATORIUM_MAXCSO_* overrides), resolved in subprocess_runner.
        prefix = nice_prefix(_OWNER) + ioprio_prefix(_OWNER)
        return prefix + cmd

    def active_pids(self) -> list[int]:
        return self._runner.active_pids()

    def abandoned_pids(self) -> list[int]:
        """Children that outlived SIGKILL; see ``SubprocessRunner``."""
        return self._runner.abandoned_pids()

    # ----- output paths -----------------------------------------------------

    @staticmethod
    def get_output_path_for_mode(
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        """Output path for a maxcso mode.

        The output extension is fixed by the mode (``cso_compress`` -> ``.cso``,
        ``zso_compress`` -> ``.zso``, ``cso_decompress`` -> ``.iso``), so this
        ignores the input suffix. ``treat_as_stem`` is accepted for interface
        parity with the other tools; archive members arrive as flattened
        filenames that keep their original extension, and only the stem is used
        here, so no separate branch is needed.
        """
        output_ext = MAXCSO_OUTPUT_BY_MODE.get(mode)
        if output_ext is None:
            raise ValueError(f"Unsupported maxcso mode: {mode}")
        input_p = Path(input_path)
        filename = f"{input_p.stem}{output_ext}"
        if output_dir:
            return str(Path(output_dir) / filename)
        return str(input_p.parent / filename)

    # ----- convert ----------------------------------------------------------

    async def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str = "cso_compress",
        *,
        compression: str | None = None,  # effort preset: fast | default | max
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        decompress = mode == "cso_decompress"
        verb = "decompression" if decompress else "compression"
        # Build the argv and size estimate up front, outside the cleanup guard:
        # a setup failure (e.g. _build_command rejecting the mode) must never
        # delete a pre-existing output_path, since maxcso has written nothing.
        cmd = self._build_command(input_path, output_path, mode, compression)

        yield {"progress": 1, "message": f"Starting CSO {verb}..."}

        # maxcso applies nice/ionice as command wrappers in _build_command
        # (nice_via_wrapper) to avoid preexec_fn, and prints no parseable percent
        # (its TTY bar goes silent on a pipe), so the runner's size-growth
        # fallback reports status from the growing -o file.
        try:
            async for update in self._runner.run(
                cmd,
                input_path=input_path,
                output_path=output_path,
                parse_progress=lambda _line: None,
                initial_progress=1,
                cancel_event=cancel_event,
                fail_label="maxcso",
                complete_message=f"CSO {verb} complete",
                mode=mode,
                nice_via_wrapper=True,
            ):
                yield update
        except (ConversionCancelled, RuntimeError, asyncio.CancelledError, GeneratorExit):
            # maxcso writes straight to output_path. These are exactly the
            # abnormal exits the runner can raise *after* it spawned the child — a
            # mid-run cancel, a non-zero/stall RuntimeError, or a task-cancellation
            # / generator close — so a partial may be on disk. The shared helper
            # bounds the unlink (a dead output mount must not freeze the queue
            # here, after the runner already gave up) while still dispatching it
            # even if this coroutine is torn down mid-wait. A setup error
            # (build_command, above this try) or a pre-spawn failure
            # (FileNotFoundError from the runner) is NOT caught here, so a
            # pre-existing output is never deleted for a conversion that wrote
            # nothing.
            await remove_partial_output(output_path, label="maxcso output")
            raise

    # ----- info -------------------------------------------------------------

    def info(self, file_path: str) -> dict:
        """Filesystem-level info (size, format label, compression state). maxcso
        exposes no offline metadata dump. Synchronous; wrap callers in a
        threadpool."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        file_size = os.path.getsize(file_path)
        ext = Path(file_path).suffix.lower()

        is_compressed = ext in MAXCSO_DECOMPRESS_EXTENSIONS
        base_format = {
            ".iso": "ISO (disc image)",
            ".cso": "CSO (compressed ISO)",
            ".zso": "ZSO (lz4-compressed ISO)",
            ".dax": "DAX (compressed ISO)",
        }.get(ext)
        compression_type = {
            ".cso": "CSO (deflate)",
            ".zso": "ZSO (lz4)",
            ".dax": "DAX",
        }.get(ext)

        size_mb = file_size / (1024 * 1024)
        size_display = f"{size_mb:.2f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB"

        return {
            "file": file_path,
            "size": file_size,
            "size_display": size_display,
            "format": base_format,
            "extension": ext,
            "compressed": is_compressed,
            "compression_type": compression_type,
        }

    @staticmethod
    def is_convertible(filename: str) -> bool:
        ext = Path(filename).suffix.lower()
        return ext in MAXCSO_COMPRESS_EXTENSIONS or ext in MAXCSO_DECOMPRESS_EXTENSIONS

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
        """Verify a compressed CSO/ZSO/DAX by running ``maxcso --crc`` on it.

        ``--crc`` reads and decompresses the whole container, logging its CRC32
        and ignoring output, so a clean (exit 0) run proves the file decompresses
        intact end to end. The run itself is the shared
        :meth:`SubprocessRunner.capture_verify`, which applies the size-scaled
        verify bound and honours ``cancel_event``.
        """
        # Bounded, off the event loop: an unresponsive volume must fail this
        # verify, not freeze every task in the process (see verify_preflight).
        problem, _size = await verify_preflight(
            file_path, MAXCSO_DECOMPRESS_EXTENSIONS, cancel_event=cancel_event,
        )
        if problem is not None:
            yield problem
            return

        # Throttle verify the same way conversions are: `maxcso --crc` fully
        # decompresses the container and is just as disk/CPU-heavy as a convert,
        # so it must honor the same nice/ionice policy (incl. the optional
        # COMPRESSATORIUM_MAXCSO_* overrides) via command wrappers -- maxcso
        # avoids preexec_fn, hence nice_via_wrapper below.
        verify_cmd = (
            nice_prefix(_OWNER)
            + ioprio_prefix(_OWNER)
            + [self.maxcso_path, "--crc", file_path]
        )
        try:
            async for update in self._runner.capture_verify(
                verify_cmd,
                path=file_path,
                success_message="File verified successfully",
                cancel_event=cancel_event,
                nice_via_wrapper=True,
            ):
                yield update
        except Exception as e:
            # NOTE (#268): this catch-all turns any runner-raised failure into a
            # per-file "verification error". Once abandonment is its own signal,
            # re-raise that one — a child that outlived SIGKILL is still reading
            # this storage, so a batch has to stop rather than open the next file.
            logger.exception("Error during CSO verification: %s", e)
            yield {"type": "error", "valid": False, "message": f"Verification error: {e}"}


# Global service instance
maxcso_service = MaxcsoService()
