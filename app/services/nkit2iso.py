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
from collections.abc import AsyncGenerator
from pathlib import Path

from config import settings
from logging_setup import get_logger
from services.subprocess_runner import (
    ConversionCancelled,
    SubprocessRunner,
    ioprio_prefix,
)

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

logger = get_logger("nkit2iso")


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
            # a redump checksum.
            yield {
                "progress": 100,
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
