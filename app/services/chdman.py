from __future__ import annotations
import asyncio
import logging
from logging_setup import get_logger
import re
import time
from collections.abc import AsyncGenerator
from pathlib import Path

from config import settings
from services.subprocess_runner import (
    ConversionCancelled,
    SubprocessRunner,
    info_timeout,
    ioprio_prefix,
    verify_timeout,
)

# Re-exported for backwards compatibility: ``ConversionCancelled`` historically
# lived here and is imported as ``from services.chdman import ConversionCancelled``
# by job_manager, dolphin_tool and z3ds_compress.  Keeping the import above (not
# redefining the class) preserves its identity so those ``except`` clauses still
# catch it.
__all__ = ["ConversionCancelled", "ChdmanService", "chdman_service"]

CHDMAN_CONVERTIBLE_EXTENSIONS = {".gdi", ".iso", ".cue", ".bin"}
CONVERTIBLE_EXTENSIONS = CHDMAN_CONVERTIBLE_EXTENSIONS

logger = get_logger("chdman")


class ChdmanService:
    """Wrapper for chdman binary."""

    def __init__(self):
        self.chdman_path = settings.chdman_path
        self._runner = SubprocessRunner(owner="chdman")

    def _build_command(
        self,
        mode: str,
        input_path: str,
        output_path: str,
        compression: str | None = None,
    ) -> list[str]:
        cmd = [self.chdman_path, mode, "-f", "-i", input_path, "-o", output_path]
        if mode == "createdvd":
            # Insert -hs 2048 after mode for PSP compatibility
            cmd = [
                self.chdman_path,
                mode,
                "-hs",
                "2048",
                "-f",
                "-i",
                input_path,
                "-o",
                output_path,
            ]

        if compression and mode in {
            "createcd",
            "createdvd",
            "createraw",
            "createhd",
            "createld",
            "copy",
        }:
            cmd = cmd[:2] + ["-c", compression] + cmd[2:]

        prefix = ioprio_prefix(self._runner.owner)
        if prefix:
            cmd = prefix + cmd
        elif logger.isEnabledFor(logging.DEBUG):
            logger.debug("ionice not found; skipping I/O priority settings")

        return cmd

    def active_pids(self) -> list[int]:
        return self._runner.active_pids()

    async def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str = "createcd",
        *,
        compression: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Run chdman conversion and yield ``{"progress", "message"}`` updates."""
        cmd = self._build_command(
            mode, input_path, output_path, compression=compression,
        )
        async for update in self._runner.run(
            cmd,
            input_path=input_path,
            output_path=output_path,
            parse_progress=self._parse_progress,
            cancel_event=cancel_event,
            fail_label="chdman",
            mode=mode,
        ):
            yield update

    async def info(self, chd_path: str) -> dict:
        """Get information about a CHD file."""
        process = await asyncio.create_subprocess_exec(
            self.chdman_path,
            "info",
            "-i",
            chd_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timeout = info_timeout(self._runner.owner)
        try:
            if timeout:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout,
                )
            else:
                stdout, stderr = await process.communicate()
        except asyncio.TimeoutError as exc:
            # Bounded teardown via the shared ladder: an unresponsive child must
            # not turn this timeout into an unbounded wait. If it survives
            # SIGKILL, reap_or_raise reports the abandonment instead -- that is
            # the fact the caller has to act on, not the timeout.
            await self._runner.reap_or_raise(
                process, fail_label="chdman info", wait_for_exit=False,
            )
            raise RuntimeError(f"chdman info timed out after {timeout}s") from exc

        if process.returncode != 0:
            raise RuntimeError(
                stderr.decode() or f"chdman info failed with code {process.returncode}",
            )

        return self._parse_info(stdout.decode())

    async def verify(self, chd_path: str) -> dict:
        """Verify the integrity of a CHD file.

        Returns:
            dict: {"valid": bool, "message": str}

        """
        final = {"valid": False, "message": "CHD verification failed"}
        async for update in self.verify_stream(chd_path):
            if update.get("type") in ("complete", "error"):
                final = update
        return {
            "valid": bool(final.get("valid", False)),
            "message": final.get("message") or "CHD verification failed",
        }

    async def verify_stream(self, chd_path: str) -> AsyncGenerator[dict, None]:
        process = await asyncio.create_subprocess_exec(
            self.chdman_path,
            "verify",
            "-i",
            chd_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._runner.track_pid(process.pid)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Starting chdman verify pid=%s path=%s", process.pid, chd_path)

        output_lines = []
        buffer = ""
        last_output_at = time.monotonic()
        start = last_output_at
        stall_timeout = max(
            0, int(getattr(settings, "verify_progress_timeout", 0) or 0),
        )
        overall_timeout = verify_timeout(self._runner.owner)
        timeout_error = None
        async def _check_timeouts(now: float) -> bool:
            nonlocal timeout_error
            if overall_timeout > 0 and now - start >= overall_timeout:
                timeout_error = f"Verification timed out after {overall_timeout}s"
                await self._runner.reap(process, exit_timeout=0)
                return True
            if stall_timeout > 0 and now - last_output_at >= stall_timeout:
                timeout_error = (
                    "Verification stalled: no output for "
                    f"{stall_timeout}s"
                )
                await self._runner.reap(process, exit_timeout=0)
                return True
            return False

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(process.stdout.read(100), timeout=2)
                except asyncio.TimeoutError:
                    if await _check_timeouts(time.monotonic()):
                        break
                    continue
                if not chunk:
                    break

                buffer += chunk.decode("utf-8", errors="replace")
                last_output_at = time.monotonic()

                while "\r" in buffer or "\n" in buffer:
                    if "\r" in buffer:
                        parts = buffer.split("\r")
                        for part in parts[:-1]:
                            line = part.strip()
                            if line:
                                output_lines.append(line)
                                progress = self._parse_progress(line) or 0
                                yield {
                                    "type": "progress",
                                    "progress": progress,
                                    "message": line,
                                }
                        buffer = parts[-1]
                    elif "\n" in buffer:
                        parts = buffer.split("\n")
                        for part in parts[:-1]:
                            line = part.strip()
                            if line:
                                output_lines.append(line)
                                progress = self._parse_progress(line) or 0
                                yield {
                                    "type": "progress",
                                    "progress": progress,
                                    "message": line,
                                }
                        buffer = parts[-1]
                if await _check_timeouts(time.monotonic()):
                    break

            if buffer.strip():
                line = buffer.strip()
                output_lines.append(line)
                progress = self._parse_progress(line) or 0
                yield {"type": "progress", "progress": progress, "message": line}

            # Bounded, and it raises rather than reporting a verdict when the
            # child outlived SIGKILL: that verifier is still reading the image,
            # so a batch has to stop instead of moving to the next file (issue
            # #268). A timeout above already ran the ladder, so skip the
            # voluntary-exit grace in that case; if it gave up there, the runner
            # remembers and this call re-reports it without re-signalling.
            await self._runner.reap_or_raise(
                process,
                fail_label="chdman verify",
                wait_for_exit=not timeout_error,
            )
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "chdman verify pid=%s exit=%s", process.pid, process.returncode,
                )

            if timeout_error:
                yield {"type": "error", "valid": False, "message": timeout_error}
                return

            output_lines = output_lines[-20:]
            output = "\n".join(output_lines).strip()
            if process.returncode == 0:
                yield {
                    "type": "complete",
                    "valid": True,
                    "message": "CHD file verified successfully",
                }
            else:
                yield {
                    "type": "error",
                    "valid": False,
                    "message": output or "CHD verification failed",
                }
        finally:
            # Plain reap(), never reap_or_raise(): an exception raised from a
            # finally replaces whatever was already propagating. Repeating it
            # after the ladder already gave up is free (the runner remembers).
            await self._runner.reap(process, exit_timeout=0)
            self._runner.untrack_pid(process.pid)

    def _parse_progress(self, line: str) -> int | None:
        """Parse chdman output for progress percentage, None if the line has none.

        None -- not 0 -- for a non-progress line (a banner, a status message).
        The runner reads "a percent was parsed" as proof the tool reports its own
        progress and stands its size-growth fallback down; a 0 sentinel made the
        very first banner line look like a report and disabled the fallback for
        the whole run (issue #263).
        """
        # chdman outputs: "Compressing, 45.2% complete..."
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
        if match:
            return min(99, int(float(match.group(1))))
        return None

    def _parse_info(self, output: str) -> dict:
        """Parse chdman info output into structured data."""
        info = {"raw_data": output}
        metadata_lines = []

        # Parse key-value pairs
        for line in output.split("\n"):
            line = line.strip()
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip().lower().replace(" ", "_")
                value = value.strip()
                info[key] = value
                if key == "metadata":
                    metadata_lines.append(value)

        if metadata_lines:
            info["metadata_lines"] = metadata_lines

        return info

    @staticmethod
    def is_convertible(filename: str) -> bool:
        """Check if a file is convertible to CHD."""
        ext = Path(filename).suffix.lower()
        return ext in CONVERTIBLE_EXTENSIONS

    @staticmethod
    def get_chd_path(
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        """Get the output CHD path for an input file or stem."""
        input_p = Path(input_path)
        # ``treat_as_stem`` inputs are synthetic flattened archive-member
        # filenames (e.g. "games_disc.cue"); strip the extension like a real
        # source so the CHD name is "games_disc.chd", not "games_disc.cue.chd".
        chd_name = input_p.stem + ".chd"

        if output_dir:
            return str(Path(output_dir) / chd_name)
        return str(input_p.parent / chd_name)

    @staticmethod
    def get_output_path_for_mode(
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        input_p = Path(input_path)
        # ``treat_as_stem`` inputs are synthetic flattened archive-member
        # filenames; treat them like real sources and strip the extension so
        # the output base matches the on-disk path (Path.stem also drops the
        # trailing ".chd" for extract modes).
        stem = input_p.stem

        if mode == "copy":
            filename = f"{stem}_copy.chd"
        elif mode in {"createcd", "createdvd", "createraw", "createhd", "createld"}:
            filename = f"{stem}.chd"
        elif mode == "extractcd":
            filename = stem if stem.lower().endswith(".cue") else f"{stem}.cue"
        elif mode == "extractdvd":
            filename = stem if stem.lower().endswith(".iso") else f"{stem}.iso"
        elif mode in {"extractraw", "extracthd"}:
            filename = stem if stem.lower().endswith(".raw") else f"{stem}.raw"
        elif mode == "extractld":
            filename = stem if stem.lower().endswith(".avi") else f"{stem}.avi"
        else:
            filename = f"{stem}.out"

        if output_dir:
            return str(Path(output_dir) / filename)
        return str(input_p.parent / filename)


chdman_service = ChdmanService()
