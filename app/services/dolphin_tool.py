import asyncio
from logging_setup import get_logger
import re
import shutil
from collections.abc import AsyncGenerator
from pathlib import Path

from config import settings
from services.subprocess_runner import (
    SubprocessRunner,
    collect_verify,
    info_timeout,
    ioprio_prefix,
    nice_prefix,
    resolve_verify_timeout,
)

DOLPHIN_CONVERTIBLE_EXTENSIONS = {".iso", ".gcz", ".wia", ".rvz", ".wbfs"}

DOLPHIN_OUTPUT_FORMATS = {
    "dolphin_rvz": ("rvz", ".rvz"),
    "dolphin_wia": ("wia", ".wia"),
    "dolphin_gcz": ("gcz", ".gcz"),
    "dolphin_iso": ("iso", ".iso"),
}
DEFAULT_DOLPHIN_COMPRESSION_LEVEL = "19"

logger = get_logger("dolphin_tool")


class DolphinToolService:
    """Wrapper for dolphin-tool binary."""

    def __init__(self):
        self.dolphin_tool_path = settings.dolphin_tool_path
        self._runner = SubprocessRunner(owner="dolphin_tool")

    def _build_convert_command(
        self,
        mode: str,
        input_path: str,
        output_path: str,
        compression: str | None = None,
    ) -> list[str]:
        fmt_name, _ = DOLPHIN_OUTPUT_FORMATS.get(mode, ("rvz", ".rvz"))

        cmd = [
            self.dolphin_tool_path,
            "convert",
            "-i", input_path,
            "-o", output_path,
            "-f", fmt_name,
        ]

        if fmt_name == "rvz":
            cmd.extend(["-b", "131072"])

        if compression and fmt_name in ("rvz", "wia"):
            if "," in compression:
                raise ValueError(
                    "dolphin-tool supports a single compression codec at a time",
                )
            codec = compression
            level = None
            if ":" in compression:
                codec, level = compression.split(":", 1)
            if codec == "none":
                level = None
            elif level is None:
                level = DEFAULT_DOLPHIN_COMPRESSION_LEVEL
            cmd.extend(["-c", codec])
            if level:
                cmd.extend(["-l", level])

        prefix = ioprio_prefix(self._runner.owner)
        if prefix:
            cmd = prefix + cmd

        return cmd

    def _wrap_with_stdbuf(self, cmd: list[str]) -> list[str]:
        """Wrap command with stdbuf if available to reduce stdout buffering."""
        stdbuf = shutil.which("stdbuf")
        if not stdbuf:
            return cmd
        try:
            idx = cmd.index(self.dolphin_tool_path)
        except ValueError:
            return [stdbuf, "-oL", "-eL"] + cmd
        return cmd[:idx] + [stdbuf, "-oL", "-eL"] + cmd[idx:]

    def active_pids(self) -> list[int]:
        return self._runner.active_pids()

    def abandoned_pids(self) -> list[int]:
        """Children that outlived SIGKILL; see ``SubprocessRunner``."""
        return self._runner.abandoned_pids()

    async def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str = "dolphin_rvz",
        *,
        compression: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Run dolphin-tool conversion and yield progress updates."""
        cmd = self._build_convert_command(
            mode, input_path, output_path, compression=compression,
        )
        cmd = self._wrap_with_stdbuf(cmd)
        async for update in self._runner.run(
            cmd,
            input_path=input_path,
            output_path=output_path,
            parse_progress=self._parse_progress,
            cancel_event=cancel_event,
            heartbeat=True,
            fail_label="dolphin-tool",
            mode=mode,
        ):
            yield update

    async def header(self, path: str) -> dict:
        """Get header information about a disc image.

        Goes through the shared capture rather than a hand-rolled spawn, for the
        same reasons as ``chdman.info``: PID tracking, a bounded teardown in
        place of an unbounded ``wait()`` after ``kill()``, the tool priority
        policy, and an unkillable child reported into any open
        ``collect_abandonment()`` sink (issue #268).
        """
        timeout = info_timeout(self._runner.owner)
        # Wrapper-based priority, not preexec_fn -- see the note in
        # ``chdman.info``: a fork of a Python callable from this multithreaded
        # parent can deadlock the child before exec, and then
        # create_subprocess_exec never returns to apply the bound at all.
        owner = self._runner.owner
        returncode, stdout, stderr = await self._runner.run_capture(
            nice_prefix(owner) + ioprio_prefix(owner)
            + [self.dolphin_tool_path, "header", "-i", path],
            timeout=timeout or None,
            nice_via_wrapper=True,
        )
        if returncode is None:
            raise RuntimeError(f"dolphin-tool header timed out after {timeout}s")

        if returncode != 0:
            raise RuntimeError(
                stderr.decode()
                or f"dolphin-tool header failed with code {returncode}",
            )

        return self._parse_header(stdout.decode())

    async def disc_hashes(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> list[str]:
        """Return the disc image's content SHA1(s) via ``dolphin-tool verify``.

        ``dolphin-tool verify -i <path> --algorithm sha1`` reconstructs the
        full disc image and prints its SHA1 (the hash redump DATs index for
        GameCube/Wii discs). For the compressed/container formats this is the
        only hash that can match a DAT, file-level SHA1 of an ``.rvz``/``.wia``
        is meaningless against redump.

        Best-effort: returns the 40-char hex tokens parsed from the tool's
        output, or an empty list on any failure / unsupported input. When
        ``cancel_event`` fires (a background scan/match job was cancelled) the
        verify subprocess is terminated promptly and an empty list returned,
        rather than blocking until the disc finishes reconstructing. The
        cancel/timeout/terminate handling lives in the shared
        ``SubprocessRunner.run_capture``.
        """
        cmd = [
            self.dolphin_tool_path, "verify", "-i", path, "--algorithm", "sha1",
        ]
        # Same size-scaled bound as verify(): this *is* a verify run, just one
        # whose output we read for a hash instead of a verdict.
        timeout = await resolve_verify_timeout(
            path, self._runner.owner, cancel_event=cancel_event,
        )
        returncode, stdout, _ = await self._runner.run_capture(
            cmd, timeout=timeout or None, cancel_event=cancel_event,
        )
        if returncode is None:
            logger.warning(
                "dolphin-tool verify (hash) aborted (cancel/timeout) for %s", path,
            )
            return []
        if returncode != 0:
            return []
        # The hash line is formatted as "SHA-1: <hex>" / "<hex>"; pull every
        # standalone 40-char hex token so format tweaks don't break matching.
        text = stdout.decode("utf-8", "replace")
        return [m.lower() for m in re.findall(r"\b[0-9a-fA-F]{40}\b", text)]

    async def verify(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> dict:
        """Verify the integrity of a disc image."""
        return await collect_verify(
            self.verify_stream(path, cancel_event=cancel_event),
            fallback_message="Disc verification failed",
        )

    def verify_stream(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Stream disc image verification progress.

        The loop is the shared :meth:`SubprocessRunner.run_verify`, so this
        verify is bounded and cancellable on the same terms as every other
        tool's; dolphin contributes the stdbuf wrap (its progress bar is
        block-buffered on a pipe) and its progress parser.
        """
        return self._runner.run_verify(
            self._wrap_with_stdbuf([self.dolphin_tool_path, "verify", "-i", path]),
            path=path,
            parse_progress=self._parse_progress,
            success_message="Disc image verified successfully",
            failure_message="Disc verification failed",
            cancel_event=cancel_event,
        )

    @staticmethod
    async def _terminate_process(
        process: asyncio.subprocess.Process,
    ) -> None:
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
            # Process is already gone; nothing left to terminate.
            logger.debug("Process already exited before termination completed.")

    def _parse_progress(self, line: str) -> int | None:
        """Parse dolphin-tool output for progress percentage."""
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
        if match:
            return min(99, int(float(match.group(1))))
        return None

    def _parse_header(self, output: str) -> dict:
        """Parse dolphin-tool header output into structured data."""
        info = {"raw_data": output}
        for line in output.split("\n"):
            line = line.strip()
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip().lower().replace(" ", "_")
                value = value.strip()
                info[key] = value
        return info

    @staticmethod
    def is_convertible(filename: str) -> bool:
        """Check if a file is convertible by dolphin-tool."""
        ext = Path(filename).suffix.lower()
        return ext in DOLPHIN_CONVERTIBLE_EXTENSIONS

    @staticmethod
    def get_output_path_for_mode(
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        """Get the output path for a dolphin-tool conversion."""
        input_p = Path(input_path)
        # ``treat_as_stem`` inputs are synthetic flattened archive-member
        # filenames (e.g. "games_disc.iso"); strip the extension like a real
        # source so the output is "games_disc.rvz", not "games_disc.iso.rvz".
        stem = input_p.stem
        _, ext = DOLPHIN_OUTPUT_FORMATS.get(mode, ("rvz", ".rvz"))
        filename = f"{stem}{ext}"

        if output_dir:
            return str(Path(output_dir) / filename)
        return str(input_p.parent / filename)


dolphin_tool_service = DolphinToolService()
