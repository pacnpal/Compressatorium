"""ChdmanTool, thin plugin wrapper delegating to ``chdman_service``.

Phase 0 moves no logic: every call forwards to the existing singleton. The
``ModeSpec`` rows formalize the mode metadata that is currently scattered as
prefix checks across the codebase.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator
from pathlib import Path

from fastapi.concurrency import run_in_threadpool

from logging_setup import get_logger
from models import CHDInfo, OutputStatus
from services.chdman import CHDMAN_CONVERTIBLE_EXTENSIONS, chdman_service
from services.disc_id import (
    DiscIdStorageAbandoned,
    clear_embedded_disc_id,
    embed_in_chd,
    extract_from_source,
    read_embedded_game_id,
)
from services.lock_manager import lock_manager

from .base import BaseTool
from .spec import ModeKind, ModeSpec

logger = get_logger()

# Only CD/DVD images carry a readable disc serial: they hold an ISO 9660
# filesystem with SYSTEM.CNF (PS1/PS2) or PSP_GAME/PARAM.SFO (PSP), or a
# Dreamcast IP.BIN. createraw/createhd (hard-disk images) and createld
# (laserdisc) have no such structure, so disc-ID tagging is scoped to these two.
_DISC_ID_MODES = frozenset({"createcd", "createdvd"})

_CREATE_MODES = {
    "createraw": "Create Raw",
    "createhd": "Create HD",
    "createcd": "Create CD",
    "createdvd": "Create DVD",
    "createld": "Create LD",
}
# mode -> (label, primary output ext, companion exts). extractcd is the only
# multi-file extract: chdman writes the .cue track sheet plus a sibling .bin
# data track, so its companion set is (".bin",). The rest produce one file.
_EXTRACT_MODES = {
    "extractraw": ("Extract Raw", ".raw", ()),
    "extracthd": ("Extract HD", ".raw", ()),
    "extractcd": ("Extract CD", ".cue", (".bin",)),
    "extractdvd": ("Extract DVD", ".iso", ()),
    "extractld": ("Extract LD", ".avi", ()),
}
_CHD = frozenset({".chd"})


def _build_modes() -> list[ModeSpec]:
    modes: list[ModeSpec] = [
        ModeSpec(
            mode=mode,
            tool_id="chdman",
            kind=ModeKind.CREATE,
            label=label,
            group="create",
            output_ext=".chd",
            input_extensions=frozenset(CHDMAN_CONVERTIBLE_EXTENSIONS),
            supports_compression=True,
            supports_delete_on_verify=True,
            allows_archive_input=True,
        )
        for mode, label in _CREATE_MODES.items()
    ]
    modes += [
        ModeSpec(
            mode=mode,
            tool_id="chdman",
            kind=ModeKind.EXTRACT,
            label=label,
            group="extract",
            output_ext=ext,
            input_extensions=_CHD,
            # A .chd packed in an archive can be pulled out and decompressed
            # back to its disc image — the job pipeline extracts the member to a
            # temp file before chdman runs, same as the create direction. (copy
            # stays off below: recompressing a .chd straight out of an archive
            # is a pointless round trip.)
            allows_archive_input=True,
            companion_exts=companions,
        )
        for mode, (label, ext, companions) in _EXTRACT_MODES.items()
    ]
    modes.append(
        ModeSpec(
            mode="copy",
            tool_id="chdman",
            kind=ModeKind.COPY,
            label="Copy / Recompress",
            group="copy",
            output_ext=".chd",
            input_extensions=_CHD,
            supports_compression=True,
            supports_delete_on_verify=True,
        )
    )
    return modes


class ChdmanTool(BaseTool):
    id = "chdman"
    policy_owner = "chdman"
    display_name = "CHDMAN"
    modes = _build_modes()
    # All extensions chdman produces: .chd from create/copy, plus the extract
    # targets (.cue/.iso/.raw/.avi) and the .bin data-track sidecar that
    # extractcd writes alongside the .cue. The .bin is what Redump-style DATs
    # index (the tiny .cue isn't), so it must be discoverable by the scan.
    # verify only applies to finished CHDs.
    output_extensions = frozenset({".chd", ".cue", ".bin", ".iso", ".raw", ".avi"})
    verify_extensions = frozenset({".chd"})

    def __init__(self, binary_path: str) -> None:
        super().__init__(binary_path)
        self._service = chdman_service

    # chdman's extract/copy modes take .chd input, but a .chd is not a
    # "convertible-from" source in the file listing, only the create sources
    # are. Override the derived union so convertible_extensions() matches
    # CHDMAN_CONVERTIBLE_EXTENSIONS.
    @property
    def input_extensions(self) -> frozenset[str]:
        return frozenset(CHDMAN_CONVERTIBLE_EXTENSIONS)

    def detect_output(
        self, input_path: str, *, from_archive: bool = False,
    ) -> OutputStatus | None:
        if Path(input_path).suffix.lower() not in self.input_extensions:
            return None
        candidate = str(Path(input_path).with_suffix(".chd"))
        file_exists, is_locked = lock_manager.check_file_status(candidate)
        if not (file_exists or is_locked):
            return None
        return OutputStatus(
            tool_id=self.id,
            exists=file_exists,
            ready=file_exists and not is_locked,
            path=candidate,
        )

    def output_path(
        self,
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        return self._service.get_output_path_for_mode(
            mode, input_path, output_dir, treat_as_stem=treat_as_stem,
        )

    def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str,
        *,
        compression: str | None = None,
        split: bool = False,  # noqa: ARG002 - split applies only to makeps3iso
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        return self._service.convert(
            input_path, output_path, mode,
            compression=compression, cancel_event=cancel_event,
        )

    async def post_convert(
        self, input_path: str, output_path: str, mode: str,
    ) -> None:
        """Embed disc-ID GAME/NAME tags into a freshly created CD/DVD CHD.

        This is the single home for conversion-time disc-ID tagging: a direct
        ``createcd`` / ``createdvd`` job reaches it via
        ``job_manager._process_job`` and a ``cso_to_chd`` chain reaches it via
        ``ChainTool``'s final step, so both paths tag identically.

        Only ``createcd`` / ``createdvd`` carry a disc serial, so every other
        chdman mode is a no-op. Best-effort, with one exception: a *tagging*
        failure never fails the job, but a chdman child that outlived SIGKILL
        does (``DiscIdStorageAbandoned``). That is not a fact about the tag, it
        is the output's storage no longer answering -- and every step after this
        hook reads that storage, so continuing buys a hung job instead of an
        untagged one (issue #268).
        Idempotent: if the CHD already carries a GAME tag equal to the source's
        serial the embed is skipped, so re-invoking the hook never appends a
        duplicate tag.

        ``GAME`` = normalized serial (emulator DB lookup key);
        ``NAME`` = human-readable title when available, serial otherwise.
        """
        if mode not in _DISC_ID_MODES:
            return
        if Path(output_path).suffix.lower() != ".chd":
            return
        if not await run_in_threadpool(os.path.exists, output_path):
            return
        try:
            disc_info = await run_in_threadpool(extract_from_source, input_path)
            if not (disc_info and disc_info.get("game_id")):
                return
            game_id = disc_info["game_id"]
            existing = await read_embedded_game_id(output_path, self.binary_path)
            if existing == game_id:
                return
            if existing is not None:
                # chdman addmeta appends rather than replaces, so a CHD that
                # already carries a *different* serial must have the stale
                # GAME/NAME stripped first — otherwise the old tag persists and
                # the embed never converges. A freshly created CHD has no tag,
                # so this is skipped in the normal path.
                await clear_embedded_disc_id(output_path, self.binary_path)
            title = disc_info.get("title") or game_id
            embedded = await embed_in_chd(output_path, game_id, title, self.binary_path)
            if embedded:
                logger.info(
                    "Embedded disc ID %r in %s", game_id, Path(output_path).name,
                )
            else:
                logger.debug(
                    "Failed to embed disc ID %r in %s",
                    game_id, Path(output_path).name,
                )
        except DiscIdStorageAbandoned as exc:
            # The one failure that is *not* best-effort. A chdman child is still
            # running against this output, and everything the caller does next
            # touches the same storage -- `_compute_output_size` is an unbounded
            # `run_in_threadpool`, and delete-on-verify starts a verifier -- so
            # swallowing this trades a skipped tag for a hung job, which is
            # #263's original failure. Propagate and let the job fail (#268).
            logger.error("Disc ID embed abandoned for %s: %s", output_path, exc)
            raise
        except Exception as exc:  # best effort; tagging never fails the job
            logger.debug("Disc ID embed skipped for %s: %s", output_path, exc)

    async def verify(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> dict:
        return await self._service.verify(path, cancel_event=cancel_event)

    def verify_stream(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        return self._service.verify_stream(path, cancel_event=cancel_event)

    async def info(self, path: str) -> dict:
        return await self._service.info(path)

    async def embedded_hashes(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> list[tuple[str, str]]:
        # CHDs carry an overall SHA1 (header) and a data SHA1 (uncompressed
        # content) in their metadata. Read them from the metadata store
        # (primed by the library scan / a prior /info call) so matching a CHD
        # against a DAT never has to re-hash the whole file. The read is
        # instant, so ``cancel_event`` is accepted for contract parity but
        # unused.
        from services.chd_metadata_store import chd_metadata_store

        # One atomic snapshot (row + current mtime in a single read) instead of
        # get_metadata() + is_stale(), whose two separate reads could see the
        # hashes at one mtime and judge staleness at another.
        metadata = await chd_metadata_store.get_fresh_metadata(path)
        if not metadata:
            # No cached metadata, or the file changed since it was cached so the
            # stored header/data SHA1 describe an older disc. Either way report
            # *no embedded candidates* (rather than raise): chdman is
            # non-exhaustive, so the caller still falls back to a file-level
            # SHA1 of the current .chd, which is valid for any DAT that indexes
            # the container bytes. Only the stale embedded hashes are
            # suppressed, not the whole match.
            return []
        out: list[tuple[str, str]] = []
        sha1 = (metadata.get("sha1") or "").strip().lower()
        if sha1:
            out.append((sha1, "chd_sha1"))
        data_sha1 = (metadata.get("data_sha1") or "").strip().lower()
        if data_sha1:
            out.append((data_sha1, "chd_data_sha1"))
        return out

    def info_model(self, raw: dict, path: str) -> CHDInfo:
        return CHDInfo(
            file=path,
            input_file=raw.get("input_file"),
            file_version=raw.get("file_version"),
            logical_size=raw.get("logical_size"),
            hunk_size=raw.get("hunk_size"),
            total_hunks=raw.get("total_hunks"),
            unit_size=raw.get("unit_size"),
            total_units=raw.get("total_units"),
            compression=raw.get("compression"),
            chd_size=raw.get("chd_size"),
            ratio=raw.get("ratio"),
            sha1=raw.get("sha1"),
            data_sha1=raw.get("data_sha1"),
            raw_data=raw.get("raw_data", ""),
            media_type=raw.get("media_type"),
            game_id=raw.get("game_id"),
            title=raw.get("title"),
        )

    def active_pids(self) -> list[int]:
        return self._service.active_pids()
