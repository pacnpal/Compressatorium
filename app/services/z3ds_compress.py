import asyncio
import contextlib
import logging
from logging_setup import get_logger
import os
import shutil
import struct
from concurrent.futures import ThreadPoolExecutor
from collections.abc import AsyncGenerator
from pathlib import Path

import aiofiles
from config import settings
from services.chdman import ConversionCancelled
from services.subprocess_runner import (
    SubprocessRunner,
    collect_verify,
    ioprio_prefix,
    ReadCancelled,
    resolve_verify_timeout,
    run_detached,
    verify_preflight,
)

# Compress inputs (raw 3DS ROMs). The upstream fork
# (https://github.com/pacnpal/z3ds_compress) added .cxi/.3dsx alongside the
# original .cci/.cia/.3ds.
Z3DS_CONVERTIBLE_EXTENSIONS = {".cci", ".cia", ".3ds", ".cxi", ".3dsx"}

# Compress map: raw ROM extension -> compressed (Z3DS) extension.
Z3DS_OUTPUT_FORMATS = {
    ".cci": ".zcci",
    ".cia": ".zcia",
    ".3ds": ".z3ds",
    ".cxi": ".zcxi",
    ".3dsx": ".z3dsx",
}

# Decompress inputs (compressed Z3DS containers) and the reverse extension map.
# The fork made 3DS round-trippable: it auto-detects direction from the "Z3DS"
# magic header and exposes -c/-d to force it (we always pass the explicit flag).
Z3DS_DECOMPRESS_EXTENSIONS = {".zcci", ".zcia", ".z3ds", ".zcxi", ".z3dsx"}

Z3DS_DECOMPRESS_FORMATS = {
    ".zcci": ".cci",
    ".zcia": ".cia",
    ".z3ds": ".3ds",
    ".zcxi": ".cxi",
    ".z3dsx": ".3dsx",
}

logger = get_logger("z3ds_compress")


class Z3DSCompressService:
    """Wrapper for z3ds_compressor binary."""

    def __init__(self):
        self.z3ds_compressor_path = settings.z3ds_compressor_path
        # convert() and verify_stream() share the runner's PID set so a single
        # active_pids() sees both; convert() delegates its whole streaming loop
        # to the runner (verify_stream still spawns zstd directly).
        self._runner = SubprocessRunner(owner="z3ds")

    def _build_command(
        self,
        input_path: str,
        output_path: str,
        mode: str = "z3ds_compress",
    ) -> list[str]:
        """Build command for z3ds_compressor.

        The fork auto-detects direction from the "Z3DS" magic header, but we
        always pass an explicit ``-c`` (compress) / ``-d`` (decompress) flag so
        the job's mode, not the file contents, decides the direction. The tool
        takes input and output paths as positional arguments.
        Format: ``z3ds_compressor <-c|-d> <input> <output>``
        """
        flag = "-d" if mode == "z3ds_decompress" else "-c"
        cmd = [
            self.z3ds_compressor_path,
            flag,
            input_path,
            output_path,
        ]

        prefix = ioprio_prefix("z3ds")
        if prefix:
            cmd = prefix + cmd

        return cmd

    def active_pids(self) -> list[int]:
        return self._runner.active_pids()

    @staticmethod
    async def _get_verify_payload_offset(
        file_path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> int:
        """Return the byte offset where the seekable zstd payload begins.

        Read on a detached thread rather than a pooled one: on an unresponsive
        mount this open/read cannot be stopped, only abandoned, and abandoning a
        *shared* worker for every cancelled verify would eventually starve the
        pool the rest of the app offloads to.
        """

        def _read_offset() -> int:
            with open(file_path, "rb") as fh:
                header = fh.read(0x20)

            if len(header) < 0x20:
                raise ValueError("Invalid Z3DS file: header is too short")

            (
                magic,
                _underlying_magic,
                _version,
                _reserved,
                header_size,
                metadata_size,
                _compressed_size,
                _uncompressed_size,
            ) = struct.unpack(
                "<4s4sBBHIQQ",
                header,
            )
            if magic != b"Z3DS":
                raise ValueError("Invalid Z3DS file: missing Z3DS header")

            payload_offset = int(header_size) + int(metadata_size)
            file_size = os.path.getsize(file_path)
            if payload_offset <= 0 or payload_offset >= file_size:
                raise ValueError("Invalid Z3DS file: payload offset is out of range")
            return payload_offset

        return await run_detached(_read_offset, cancel_event=cancel_event)

    def get_output_path(self, input_path: str, output_dir: str | None = None) -> str:
        """Calculate output path for a 3DS file.

        Args:
            input_path: Path to input .cci or .cia file
            output_dir: Optional output directory. If None, uses same directory as input.

        Returns:
            Path for output .zcci or .zcia file
        """
        input_file = Path(input_path)
        ext = input_file.suffix.lower()

        if ext not in Z3DS_OUTPUT_FORMATS:
            raise ValueError(f"Unsupported file extension: {ext}")

        output_ext = Z3DS_OUTPUT_FORMATS[ext]
        output_name = input_file.stem + output_ext

        if output_dir:
            return str(Path(output_dir) / output_name)
        return str(input_file.parent / output_name)

    async def convert(
        self,
        input_path: str,
        output_path: str,
        # `compression` is unused (3DS has no codec/level picker) but kept for
        # interface consistency with chdman/dolphin services. `mode` selects the
        # direction: "z3ds_compress" (default) or "z3ds_decompress".
        mode: str = "z3ds_compress",
        *,
        compression: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Run z3ds_compressor on ``input_path``, yielding progress dicts.

        ``mode`` selects the direction (``z3ds_compress`` packs a raw ROM into a
        Z3DS container; ``z3ds_decompress`` restores the original ROM). The
        explicit ``-c``/``-d`` flag is passed so the job's mode, not the file's
        magic header, decides direction. Honors ``cancel_event`` (terminate +
        clean partial output, raising ``ConversionCancelled``) and a stall
        timeout. Yields ``{"progress": int, "message": str}`` and a final 100%.
        """
        decompress = mode == "z3ds_decompress"
        verb = "decompression" if decompress else "compression"
        cmd = self._build_command(input_path, output_path, mode)
        yield {"progress": 5, "message": f"Starting 3DS {verb}..."}

        # Delegate the streaming spawn / stall / cancel / PID loop to the shared
        # runner. z3ds keeps preexec nice (owner "z3ds") and folds ionice into
        # _build_command, so nice_via_wrapper stays False. parse_progress is a
        # no-op — there is no parseable percent — so the runner's size-growth
        # fallback drives the bar from the growing output file.
        try:
            async for update in self._runner.run(
                cmd,
                input_path=input_path,
                output_path=output_path,
                parse_progress=lambda _line: None,
                initial_progress=5,
                cancel_event=cancel_event,
                fail_label="z3ds_compressor",
                complete_message=f"3DS {verb} complete",
                mode=mode,
            ):
                yield update
        except (ConversionCancelled, RuntimeError, asyncio.CancelledError, GeneratorExit):
            # z3ds_compressor writes the container in place. These are the
            # abnormal exits the runner can raise after it spawned the child — a
            # mid-run cancel, a non-zero/stall RuntimeError, or a task-cancellation
            # / generator close — so a partial may be on disk. Remove it
            # synchronously so a retry isn't blocked by a truncated file. Setup
            # (above this try) and pre-spawn failures are not caught here, so a
            # pre-existing output is never deleted for a conversion that wrote
            # nothing.
            with contextlib.suppress(OSError):
                if os.path.exists(output_path):
                    os.remove(output_path)
            raise

    def info(self, file_path: str) -> dict:
        """Get basic information about a 3DS ROM file.

        Since z3ds_compressor doesn't provide metadata extraction, this method
        returns basic file system information: size, format, compression status.

        Note: This is a synchronous method. Callers should wrap with run_in_threadpool
        if calling from async context.

        Args:
            file_path: Path to a raw ROM (.cci/.cia/.3ds/.cxi/.3dsx) or a
                compressed container (.zcci/.zcia/.z3ds/.zcxi/.z3dsx)

        Returns:
            dict with file info (file, size, format, compressed, etc.)
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        file_size = os.path.getsize(file_path)
        ext = Path(file_path).suffix.lower()

        # Determine format and compression status
        is_compressed = ext in Z3DS_DECOMPRESS_EXTENSIONS
        base_format = None
        if ext in {".cci", ".zcci"}:
            base_format = "CCI (Cart Image)"
        elif ext in {".cia", ".zcia"}:
            base_format = "CIA (Installable Archive)"
        elif ext in {".3ds", ".z3ds"}:
            base_format = "3DS (Cart Image)"
        elif ext in {".cxi", ".zcxi"}:
            base_format = "CXI (Executable Image)"
        elif ext in {".3dsx", ".z3dsx"}:
            base_format = "3DSX (Homebrew)"

        # Format size for display
        size_mb = file_size / (1024 * 1024)
        size_display = f"{size_mb:.2f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB"

        return {
            "file": file_path,
            "size": file_size,
            "size_display": size_display,
            "format": base_format,
            "extension": ext,
            "compressed": is_compressed,
            "compression_type": "Seekable ZStandard" if is_compressed else None,
        }

    @staticmethod
    def is_convertible(filename: str) -> bool:
        """Check if a file is convertible by z3ds_compress.

        Args:
            filename: Name of the file to check

        Returns:
            True if the file has a .cci or .cia extension
        """
        ext = Path(filename).suffix.lower()
        return ext in Z3DS_CONVERTIBLE_EXTENSIONS

    @staticmethod
    def get_output_path_for_mode(
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        """Get the output path for a z3ds mode.

        Args:
            mode: Conversion mode ("z3ds_compress" or "z3ds_decompress")
            input_path: Path to input file or stem
            output_dir: Optional output directory
            treat_as_stem: If True, treat input_path as stem without extension

        Returns:
            Path for output file

        Note:
            ``treat_as_stem=True`` is used for archive members. The member's
            original extension is preserved in the synthetic filename (see
            ``ArchiveService._output_name_for_member``), so it maps the same
            way as an on-disk file: compress maps .3ds -> .z3ds, .cci -> .zcci,
            etc.; decompress reverses it (.z3ds -> .3ds, .zcci -> .cci). It only
            falls back to a default extension when the input extension is
            missing or unrecognised.
        """
        input_p = Path(input_path)

        # Both branches treat the input as a filename: archive members arrive
        # as flattened filenames that keep their original extension, so the
        # output mapping is identical to the on-disk case.
        stem = input_p.stem
        ext = input_p.suffix.lower()
        if mode == "z3ds_decompress":
            output_ext = Z3DS_DECOMPRESS_FORMATS.get(ext, ".3ds")
        else:
            output_ext = Z3DS_OUTPUT_FORMATS.get(ext, ".zcci")

        filename = f"{stem}{output_ext}"

        if output_dir:
            return str(Path(output_dir) / filename)
        return str(input_p.parent / filename)


    async def verify(
        self, file_path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> dict:
        """Verify the integrity of a compressed 3DS file.

        Performs deep verification by streaming the compressed Z3DS/ZCCI/ZCIA file
        through `zstd -t` to validate the ZStandard stream integrity.

        Returns:
            dict: {"valid": bool, "message": str}
        """
        return await collect_verify(
            self.verify_stream(file_path, cancel_event=cancel_event),
            fallback_message="Verification failed",
        )

    async def verify_stream(
        self, file_path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Stream verification progress for a compressed 3DS file.

        Performs deep integrity verification by piping the file through `zstd -t`
        to validate the ZStandard compression stream. This ensures the compressed
        data is not corrupted and can be successfully decompressed.
        """
        # Bounded, off the event loop: an unresponsive volume must fail this
        # verify, not freeze every task in the process (see verify_preflight).
        problem, _size = await verify_preflight(
            file_path, Z3DS_DECOMPRESS_EXTENSIONS, cancel_event=cancel_event,
        )
        if problem is not None:
            yield problem
            return

        # Perform deep verification using zstd -t.
        # Container metadata length is variable, so compute the payload offset
        # from the on-disk Z3DS header fields.

        try:
            zstd_path = shutil.which("zstd")
            if not zstd_path:
                yield {
                    "type": "error",
                    "valid": False,
                    "message": "zstd not found; full integrity verification is unavailable",
                }
                return

            payload_offset = await self._get_verify_payload_offset(
                file_path, cancel_event=cancel_event,
            )

            # Size-scaled bound, resolved from the file actually being read, so
            # this verify ends even if zstd never does (issue #266). Resolved
            # *before* the spawn: it stats the file, and an await between the
            # spawn and the try/finally below is a window where a cancellation
            # (the verify SSE route cancels its task on client disconnect)
            # unwinds this generator with zstd already running and tracked, but
            # with nothing to reap or untrack it.
            overall_timeout = await resolve_verify_timeout(file_path, "z3ds")

            # Start zstd -t process reading from stdin
            process = await asyncio.create_subprocess_exec(
                zstd_path,
                "-t",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._runner.track_pid(process.pid)

            # Set once the TERM -> KILL ladder has run and given up; repeating
            # it on a child that survived SIGKILL only burns another 15s of the
            # verify lane for no possible new outcome.
            reap_failed = False
            payload_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="z3ds-verify-read",
            )
            try:
                yield {"type": "progress", "progress": 0, "message": "Verifying integrity..."}

                # Stream the payload to zstd and wait for it to finish. Wrapped
                # in one coroutine so an overall verify timeout can bound the
                # whole feed-and-test cycle, not just the final wait.
                async def _stream_and_wait():
                    chunk_size = 1024 * 1024  # 1MB chunks
                    try:
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug(
                                "Starting z3ds verification stream for %s (offset=%d)",
                                file_path,
                                payload_offset,
                            )

                        # aiofiles runs its blocking reads in an executor; give
                        # it a private single-worker one rather than the event
                        # loop's shared default. A read wedged on a dead volume
                        # can only be abandoned, and abandoning a *shared*
                        # worker per cancelled verify would eventually starve
                        # every other default-executor user in the process
                        # (issue #266, same rule as run_detached). The executor
                        # is shut down without joining below, so a wedged worker
                        # costs one written-off thread and nothing more.
                        async with aiofiles.open(
                            file_path, "rb", executor=payload_executor,
                        ) as f:
                            await f.seek(payload_offset)

                            while True:
                                if cancel_event is not None and cancel_event.is_set():
                                    # Stop feeding immediately; the waiter below
                                    # reports the cancel and the finally reaps
                                    # zstd. One chunk of latency at most.
                                    break
                                chunk = await f.read(chunk_size)
                                if not chunk:
                                    break
                                try:
                                    if process.stdin is None:
                                        raise RuntimeError("zstd stdin is unavailable")
                                    process.stdin.write(chunk)
                                    await process.stdin.drain()
                                except BrokenPipeError:
                                    # zstd closed stdin early due to integrity failure.
                                    break

                        if process.stdin is not None:
                            process.stdin.close()
                            await process.stdin.wait_closed()

                    except Exception as stream_err:
                        logger.error("Error streaming to zstd: %s", stream_err)
                        raise

                    # Wait for process to finish
                    return await process.communicate()

                # This tool feeds the child on stdin, so it cannot use the shared
                # capture_verify; it races the same three outcomes by hand.
                feed = asyncio.ensure_future(_stream_and_wait())
                cancel_wait = (
                    asyncio.ensure_future(cancel_event.wait())
                    if cancel_event is not None
                    else None
                )
                try:
                    done, _pending = await asyncio.wait(
                        [feed] + ([cancel_wait] if cancel_wait is not None else []),
                        timeout=overall_timeout or None,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    # Both helper tasks are torn down here, not just the cancel
                    # watcher: if this generator is closed while the wait is
                    # pending (the verify SSE route cancels its task on client
                    # disconnect), the normal cleanup below never runs, and a
                    # feeder left reading the image -- or failing later against
                    # a closed stdin with nobody awaiting it -- accumulates one
                    # orphan per disconnect. A task that already completed is
                    # unaffected, so the success path still reads feed.result().
                    for helper in (cancel_wait, feed):
                        if helper is not None and not helper.done():
                            helper.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await helper

                if feed not in done:
                    # Cancelled or timed out: stop zstd, drain the feeder, and
                    # report which one it was. Remember whether the ladder gave
                    # up, so the finally below doesn't spend another TERM/KILL
                    # cycle on a child already known to be unkillable.
                    if not await self._runner.reap(process, exit_timeout=0):
                        reap_failed = True
                    feed.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await feed
                    if cancel_event is not None and cancel_event.is_set():
                        yield {
                            "type": "error",
                            "valid": False,
                            "cancelled": True,
                            "message": "Verification cancelled",
                        }
                    else:
                        yield {
                            "type": "error",
                            "valid": False,
                            "message": f"Verification timed out after {overall_timeout}s",
                        }
                    return

                _stdout, stderr = feed.result()

                if process.returncode == 0:
                    yield {"type": "progress", "progress": 100, "message": "Integrity check passed"}
                    yield {
                        "type": "complete",
                        "valid": True,
                        "message": "File verified successfully"
                    }
                else:
                    stderr_text = stderr.decode("utf-8", errors="replace").strip()
                    yield {
                        "type": "error",
                        "valid": False,
                        "message": f"Integrity check failed: {stderr_text}"
                    }
            finally:
                # Bounded TERM -> KILL ladder rather than kill()+wait(): a child
                # blocked in uninterruptible I/O never answers either, and an
                # unbounded wait here would hold the queue's only slot forever.
                # Skipped when that ladder already exhausted itself above.
                if not reap_failed:
                    await self._runner.reap(process, exit_timeout=0)
                self._runner.untrack_pid(process.pid)
                # wait=False: never join. A worker still blocked on an
                # unresponsive volume must not hold up this coroutine (or, at
                # interpreter exit, the container restart).
                payload_executor.shutdown(wait=False)

        except ReadCancelled:
            # Cancelled while reading the header: no verdict, so report the
            # cancellation rather than a verification error.
            yield {
                "type": "error",
                "valid": False,
                "cancelled": True,
                "message": "Verification cancelled",
            }
        except Exception as e:
            logger.exception("Error during 3DS verification: %s", e)
            yield {
                "type": "error",
                "valid": False,
                "message": f"Verification error: {str(e)}"
            }


# Global service instance
z3ds_compress_service = Z3DSCompressService()
