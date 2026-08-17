from __future__ import annotations
import asyncio
import logging
from logging_setup import get_logger
import re
from collections.abc import AsyncGenerator
from pathlib import Path

from config import settings
from services.subprocess_runner import (
    StorageAbandoned,
    abandonment_checkpoint,
    ConversionCancelled,
    SubprocessRunner,
    collect_verify,
    info_timeout,
    ioprio_prefix,
    nice_prefix,
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

    def abandoned_pids(self) -> list[int]:
        """Children that outlived SIGKILL; see ``SubprocessRunner``."""
        return self._runner.abandoned_pids()

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

    @property
    def runner(self) -> SubprocessRunner:
        """This tool's runner, shared with helpers that spawn chdman children.

        ``services.disc_id`` shells out to chdman for the GAME/NAME tag work, so
        it goes through this same instance rather than one of its own: one PID
        set that ``active_pids()`` fully describes, and one place that records a
        child the teardown ladder had to give up on.
        """
        return self._runner

    async def info(self, chd_path: str) -> dict:
        """Get information about a CHD file.

        Goes through the shared capture rather than a hand-rolled spawn: that
        tracks the PID, bounds the teardown (the old path ended in an unbounded
        ``wait()`` after ``kill()``), applies the tool priority policy, and
        reports an unkillable child into any open ``collect_abandonment()`` sink
        so a scan walking a library can stop rather than strand one per file
        (issue #268).
        """
        timeout = info_timeout(self._runner.owner)
        # Priority via command wrappers, never a preexec_fn: this process is
        # multithreaded, and forking a Python callable from a multithreaded
        # parent can deadlock the child before it exec()s -- and a child wedged
        # there never returns from create_subprocess_exec at all, so the bound
        # and the PID tracking below it would never be reached. `run_verify` and
        # the wrapper-nice tools avoid preexec for the same reason; `nice` and
        # `ionice` only exec. The hand-rolled spawn this replaces used no
        # preexec_fn, so this keeps that property.
        owner = self._runner.owner
        with abandonment_checkpoint() as abandoned:
            returncode, stdout, stderr = await self._runner.run_capture(
                nice_prefix(owner) + ioprio_prefix(owner)
                + [self.chdman_path, "info", "-i", chd_path],
                timeout=timeout or None,
                nice_via_wrapper=True,
            )
        # An ordinary timeout leaves the child dead; this one does not, and the
        # caller's answer differs (retry vs. tell the client the storage is not
        # answering). Folding both into one RuntimeError makes it a generic 500
        # and invites a refresh that strands another child (issue #268).
        if abandoned:
            raise StorageAbandoned(
                f"chdman info on the file left {', '.join(abandoned)} stuck on "
                "unresponsive storage; it is still running"
            )
        if returncode is None:
            raise RuntimeError(f"chdman info timed out after {timeout}s")

        if returncode != 0:
            raise RuntimeError(
                stderr.decode(errors="replace").strip()
                or f"chdman info failed with code {returncode}",
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
