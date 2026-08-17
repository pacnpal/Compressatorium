"""Wrapper for ``makeps3iso`` (https://github.com/bucanero/ps3iso-utils).

makeps3iso packages an already-decrypted PS3 disc/JB folder (a ``PS3_GAME/``
root, plus ``PS3_DISC.SFB`` for disc rips) into a single ``.iso`` that RPCS3
mounts directly. Decryption and keys are out of scope — this only repackages a
folder the user already decrypted.

This is the first **directory-as-input** tool: its unit of work is a folder, not
a file with a suffix, so it relies on the ``services.ps3`` source-layout detector
(``ToolPlugin.accepts_directory``) instead of an extension match.

CLI: ``makeps3iso [-s] <input_folder> [<output.iso>]``. With the optional ``-s``
**split** flag (opt-in per job, for FAT32 targets that can't hold a >4 GB file)
makeps3iso writes the image in ~4 GB parts: it only splits once the image
crosses the threshold (``0x1FFFE0`` sectors ≈ 4 GiB), renaming the first part to
``<output>.iso.0`` and writing ``<output>.iso.1``, ``.2`` … RPCS3 mounts the
``.0``. A sub-4 GB title still emits a single ``<output>.iso`` even with ``-s``,
so the produced filenames are only known **after** the build — the service
discovers them by probing for ``<output>`` then ``<output>.0``/``.1``/… on disk.

makeps3iso has no native verify, so after a successful build the service does a
light **PARAM.SFO ``TITLE_ID`` readback** from the produced ISO (reusing
``services.ps3`` + the shared ISO 9660 reader). The PVD / PARAM.SFO live near the
start, so the readback always targets the **first** produced file (the ``.0``
part when split). The readback is advisory: a mismatch / unreadable header logs a
warning but does not fail the job (and never deletes the curated source folder —
``supports_delete_on_verify`` is ``False``).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from collections.abc import AsyncGenerator
from pathlib import Path

from config import settings
from logging_setup import get_logger
from services import ps3
from services.subprocess_runner import (
    ConversionCancelled,
    SubprocessRunner,
    ioprio_prefix,
    run_detached,
)

# SubprocessRunner "owner" for the shared priority/timeout policy. An optional
# COMPRESSATORIUM_MAKEPS3ISO_* override takes precedence over the tool-neutral
# COMPRESSATORIUM_TOOL_* default (see services/subprocess_runner.py).
_OWNER = "makeps3iso"

# makeps3iso draws a percentage as it writes the ISO; pull the last "NN%" token
# off a line. When a build emits no parseable percentage the SubprocessRunner
# falls back to output-file growth for stall detection, so progress just holds
# at its last value until the final 100% — it never false-stalls.
_PROGRESS_RE = re.compile(r"(\d{1,3})\s*%")

logger = get_logger("makeps3iso")


class MakePs3IsoService:
    """Wrapper for the makeps3iso binary."""

    def __init__(self) -> None:
        self.makeps3iso_path = settings.makeps3iso_path
        self._runner = SubprocessRunner(owner=_OWNER)

    # ----- command ----------------------------------------------------------

    def _build_command(
        self, folder: str, output_path: str, *, split: bool = False,
    ) -> list[str]:
        # ``-s`` (split for FAT32) goes BEFORE the input folder per makeps3iso's
        # arg parser (``makeps3iso [-s] <folder> [<output>]``).
        cmd = [self.makeps3iso_path]
        if split:
            cmd.append("-s")
        cmd += [folder, output_path]
        # run() applies nice via preexec but not ionice, so wrap with the shared
        # ionice prefix here (mirrors chdman) — packing an ISO is I/O-heavy.
        prefix = ioprio_prefix(self._runner.owner)
        return prefix + cmd if prefix else cmd

    @staticmethod
    def _numbered_parts(output_path: str) -> list[str]:
        """Ordered ``output_path.0`` / ``.1`` / … split parts that exist on disk.

        Taken only as a contiguous run starting at ``.0`` — an unrelated numbered
        sibling (e.g. ``Game.iso.2024`` with no ``.0``, or one beyond a gap) is
        never enumerated, so it is never summed, probed, or deleted as part of
        this split set. This mirrors the file-browser fold, which likewise only
        folds a contiguous ``.0``-based set.
        """
        parts: list[str] = []
        idx = 0
        while True:
            candidate = f"{output_path}.{idx}"
            if not os.path.isfile(candidate):
                break
            parts.append(candidate)
            idx += 1
        return parts

    @classmethod
    def split_parts(cls, output_path: str) -> list[str]:
        """Files makeps3iso actually produced for ``output_path``, in order.

        A single ``[output_path]`` for a sub-4 GB build (split or not), or the
        ordered ``[output_path.0, output_path.1, ...]`` parts once a ``-s`` build
        crossed the 4 GB threshold. Empty if nothing exists. Probing the disk is
        the only reliable signal — whether a ``-s`` build split is size-dependent
        and unknown until it finishes.
        """
        if os.path.isfile(output_path):
            return [output_path]
        return cls._numbered_parts(output_path)

    def _parse_progress(self, line: str) -> int | None:
        # Take the LAST percentage on the line (makeps3iso may redraw several on
        # one carriage-return-joined chunk; the last is the current value).
        matches = _PROGRESS_RE.findall(line)
        if not matches:
            return None
        value = int(matches[-1])
        return value if 0 <= value <= 100 else None

    def active_pids(self) -> list[int]:
        return self._runner.active_pids()

    # ----- output paths -----------------------------------------------------

    @staticmethod
    def get_output_path(
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,  # noqa: ARG004 - interface parity
    ) -> str:
        """Output ``.iso`` path for a folder input.

        Derived from the folder's **normalized basename**, not a suffix swap: a
        trailing slash would make ``os.path.basename("/a/MyGame/")`` return
        ``""`` and yield an invalid ``.../MyGame/.iso``. ``treat_as_stem`` is
        accepted only for interface parity with the file-based tools (a folder
        never arrives as a flattened archive member).
        """
        normalized = os.path.normpath(input_path)
        filename = f"{os.path.basename(normalized)}.iso"
        if output_dir:
            return str(Path(output_dir) / filename)
        return str(Path(normalized).parent / filename)

    # ----- convert ----------------------------------------------------------

    async def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str = "folder_to_iso",
        *,
        compression: str | None = None,  # noqa: ARG002 - no compression knob
        split: bool = False,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        cmd = self._build_command(input_path, output_path, split=split)

        # A split build renames the base .iso to .iso.0 and writes .iso.1/…, so
        # the bare output_path stops growing mid-run; widen the stall probe to
        # the whole set (summed size) so a healthy split isn't killed as stalled
        # when makeps3iso's percent plateaus during a large part write.
        def _growth_paths() -> list[str]:
            return [output_path, *self._numbered_parts(output_path)]

        growth_paths = _growth_paths if split else None
        try:
            async for update in self._runner.run(
                cmd,
                input_path=input_path,
                output_path=output_path,
                parse_progress=self._parse_progress,
                cancel_event=cancel_event,
                fail_label="makeps3iso",
                complete_message="ISO build complete",
                output_growth_paths=growth_paths,
                mode=mode,
            ):
                # Hold back the runner's terminal 100% so the readback message
                # is the final update the job records.
                if update.get("progress", 0) >= 100:
                    continue
                yield update
        except BaseException:
            # makeps3iso writes the .iso in place, so any abnormal exit leaves a
            # partial behind: a non-zero exit / stall (RuntimeError), a cancel
            # (ConversionCancelled), AND task cancellation / generator close,
            # which raise the BaseException-derived CancelledError / GeneratorExit
            # — caught here too so the partial is never orphaned. Remove it
            # *synchronously* (local unlinks): awaiting during a cancellation
            # could be re-cancelled and skip the cleanup. A split run may have
            # left several parts (output.0, .1, …) plus the not-yet-renamed base,
            # so clear them all. Re-raise so cancellation semantics are preserved.
            # (run() does not clean the output itself.)
            self.remove_outputs(output_path)
            raise

        # ``-s`` only splits past 4 GB and the part names aren't known until now,
        # so discover what was actually written. Readback targets the first file
        # (the .0 part holds the PVD / PARAM.SFO).
        parts = await asyncio.to_thread(self.split_parts, output_path)
        readback_target = parts[0] if parts else output_path
        message = await asyncio.to_thread(
            self._readback_message, input_path, readback_target,
        )
        if len(parts) > 1:
            message = f"{message} — split into {len(parts)} parts"
        yield {"progress": 100, "message": message}

    @classmethod
    def output_artifacts(cls, output_path: str) -> list[str]:
        """The base output *and* any numbered split parts, base first.

        Everything a run of this tool may have put on disk for ``output_path``,
        including the states only a *failed* run produces: a mid-split failure
        can leave both the not-yet-renamed base and some numbered parts, which
        never coexist after a successful build. Deliberately not
        ``split_parts`` (which stops at the base when it exists) — that one
        describes a finished output, this one describes everything to sweep.

        Backs both the failure cleanup here and the plugin's
        ``overwrite_targets``, so the two can't drift.
        """
        return [output_path, *cls._numbered_parts(output_path)]

    @classmethod
    def remove_outputs(cls, output_path: str) -> None:
        """Synchronously unlink the base output *and* any split parts.

        Failure cleanup. Authorized-overwrite cleanup goes through the job
        pipeline's tool-neutral path instead (``overwrite_targets``), which
        additionally rejects non-file occupants rather than silently skipping
        them the way this ``suppress(OSError)`` does.
        """
        for target in cls.output_artifacts(output_path):
            with contextlib.suppress(OSError):
                os.remove(target)

    @staticmethod
    def _readback_message(folder: str, iso_path: str) -> str:
        """Light PARAM.SFO TITLE_ID readback; advisory, never fatal."""
        try:
            source_id = ps3.ps3_title_id(folder)
            built_id = ps3.ps3_iso_title_id(iso_path)
        except Exception as exc:  # never let the readback fail a good build
            logger.debug("makeps3iso readback skipped for %s: %s", iso_path, exc)
            return "ISO build complete"
        if source_id and built_id:
            if source_id == built_id:
                return f"ISO build complete (verified TITLE_ID {built_id})"
            logger.warning(
                "makeps3iso TITLE_ID mismatch for %s: source=%s built=%s",
                iso_path, source_id, built_id,
            )
            return "ISO build complete (warning: TITLE_ID mismatch)"
        if not built_id:
            logger.warning(
                "makeps3iso could not read back PARAM.SFO TITLE_ID from %s",
                iso_path,
            )
        return "ISO build complete"

    # ----- info -------------------------------------------------------------

    def info(self, folder_path: str) -> dict:
        """Filesystem-level info for a PS3 folder (PARAM.SFO title/id).

        Synchronous; wrap callers in a threadpool.
        """
        if not os.path.isdir(folder_path):
            raise FileNotFoundError(f"Folder not found: {folder_path}")

        total = 0
        for root, _dirs, names in os.walk(folder_path):
            for name in names:
                with contextlib.suppress(OSError):
                    total += os.path.getsize(os.path.join(root, name))

        keys = ps3.ps3_folder_sfo_keys(folder_path)
        size_mb = total / (1024 * 1024)
        size_display = (
            f"{size_mb:.2f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB"
        )
        return {
            "file": folder_path,
            "size": total,
            "size_display": size_display,
            "format": "PS3 disc/JB folder",
            "extension": "",
            "compressed": False,
            "compression_type": None,
            "title": keys.get("TITLE") or None,
            "title_id": keys.get("TITLE_ID") or None,
        }

    # ----- verify -----------------------------------------------------------

    async def verify(
        self, iso_path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> dict:
        """Confirm a built ``.iso`` carries a readable PS3 PARAM.SFO TITLE_ID.

        makeps3iso has no native verify; this is the light readback. It is not
        wired to delete-on-verify (deleting a curated source folder is
        destructive), so it only runs when explicitly requested.

        ``cancel_event`` is checked before the readback rather than during it:
        the read is a single PARAM.SFO lookup in a threadpool, seconds at worst,
        with nothing to interrupt in between.
        """
        if cancel_event is not None and cancel_event.is_set():
            return {
                "valid": False,
                "cancelled": True,
                "message": "Verification cancelled",
            }
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("makeps3iso verify (TITLE_ID readback) for %s", iso_path)
        # Detached, not pooled: an ISO on a dead mount can only be abandoned,
        # and a shared worker abandoned per cancelled verify would starve the
        # pool every other offload in the process depends on.
        title_id = await run_detached(ps3.ps3_iso_title_id, iso_path)
        if title_id:
            return {"valid": True, "message": f"PS3 ISO TITLE_ID {title_id}"}
        return {
            "valid": False,
            "message": "No readable PS3 PARAM.SFO TITLE_ID in ISO",
        }


# Re-exported so callers can `except ConversionCancelled` symmetrically with the
# other services even though makeps3iso raises it via the shared runner.
__all__ = ["ConversionCancelled", "MakePs3IsoService", "makeps3iso_service"]

# Global service instance
makeps3iso_service = MakePs3IsoService()
