from __future__ import annotations
import asyncio
import logging
from logging_setup import get_logger
import re
from collections.abc import AsyncGenerator
from pathlib import Path

from config import settings
from services.subprocess_runner import (
    ConversionCancelled,
    SubprocessRunner,
    collect_verify,
    info_timeout,
    ioprio_prefix,
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
            await self._terminate_process(process)
            raise RuntimeError(f"chdman info timed out after {timeout}s") from exc

        if process.returncode != 0:
            raise RuntimeError(
                stderr.decode() or f"chdman info failed with code {process.returncode}",
            )

        return self._parse_info(stdout.decode())

    async def verify(
        self, chd_path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> dict:
        """Verify the integrity of a CHD file.

        Returns:
            dict: {"valid": bool, "message": str}, plus "cancelled": True when
            ``cancel_event`` stopped the run before it reached a verdict.

        """
        return await collect_verify(
            self.verify_stream(chd_path, cancel_event=cancel_event),
            fallback_message="CHD verification failed",
        )

    def verify_stream(
        self, chd_path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Stream ``chdman verify`` progress.

        The loop itself is the shared :meth:`SubprocessRunner.run_verify` --
        bounds, cancellation, line segmentation and the reap ladder are the same
        for every streaming verifier, so chdman contributes only its command and
        its progress parser.
        """
        return self._runner.run_verify(
            [self.chdman_path, "verify", "-i", chd_path],
            path=chd_path,
            parse_progress=self._parse_progress,
            success_message="CHD file verified successfully",
            failure_message="CHD verification failed",
            cancel_event=cancel_event,
        )

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> None:
        try:
            if process.returncode is not None:
                return
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        except ProcessLookupError:
            # The process has already exited or no longer exists; nothing left to terminate.
            pass

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
