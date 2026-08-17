"""Nkit2IsoTool, thin plugin wrapper delegating to ``nkit2iso_service``.

Registers an **info** route but no **verify** one, which is not an oversight:

* Info is real and free — the NKit header names the console, the game and the
  size the restore will produce, all from one small read.
* Verify is deliberately absent. nkit2iso has no verify subcommand (integrity
  is the header CRC32 it checks inline, so a completed job *is* the
  verification), and claiming ``.iso`` as a verify extension would be actively
  harmful: ``tool_for_verify`` returns the *first* tool matching an extension,
  so every CD/DVD ISO in a library would start routing its Verify action here.
  That is the same trap Dolphin documents when it keeps ``.iso`` out of
  ``DOLPHIN_VERIFY_EXTS``. No verify also means no delete-on-verify.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from fastapi.concurrency import run_in_threadpool
from models import NkitInfo, OutputStatus
from services.lock_manager import lock_manager
from services.nkit2iso import (
    NKIT2ISO_CONVERTIBLE_EXTENSIONS,
    NKIT2ISO_OUTPUT_EXTENSION,
    nkit2iso_service,
)

from .base import BaseTool
from .spec import ModeKind, ModeSpec


class Nkit2IsoTool(BaseTool):
    id = "nkit"
    # NKit is a GameCube/Wii shrink format.
    platform_slugs = frozenset({"ngc", "gamecube", "wii"})
    policy_owner = "nkit2iso"
    display_name = "NKit"
    modes = (
        ModeSpec(
            mode="nkit_restore",
            tool_id="nkit",
            kind=ModeKind.EXTRACT,
            label="Restore ISO from NKit",
            group="nkit",
            output_ext=NKIT2ISO_OUTPUT_EXTENSION,
            input_extensions=frozenset(NKIT2ISO_CONVERTIBLE_EXTENSIONS),
            # No delete-on-verify: the restored .iso is not in verify_extensions
            # (this tool registers no verify route at all), so the source could
            # never be deleted against a confirmed output.
            supports_delete_on_verify=False,
            # Restoring an NKit image pulled straight out of a .zip/.7z/.rar is
            # a genuine conversion, not a round trip — the member is a shrunk
            # image and the output is the full one.
            allows_archive_input=True,
        ),
    )
    # Declared so an already-restored sibling badges as an existing output and
    # the library scan discovers it. `.iso` is shared with chdman extractiso /
    # dolphin_to_iso / cso_decompress / makeps3iso, which is fine: the set is a
    # union across tools.
    output_extensions = frozenset({NKIT2ISO_OUTPUT_EXTENSION})
    verify_extensions = frozenset()

    def __init__(self, binary_path: str) -> None:
        super().__init__(binary_path)
        self._service = nkit2iso_service

    def detect_output(
        self, input_path: str, *, from_archive: bool = False,
    ) -> OutputStatus | None:
        if not self._service.is_convertible(input_path):
            return None
        candidate = self._service.get_output_path(input_path)
        file_exists, is_converting = lock_manager.check_file_status(candidate)
        if not (file_exists or is_converting):
            return None
        return OutputStatus(
            tool_id=self.id,
            exists=file_exists,
            ready=file_exists and not is_converting,
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

    async def info(self, path: str) -> dict:
        return await run_in_threadpool(self._service.info, path)

    def info_model(self, raw: dict, path: str) -> NkitInfo:
        return NkitInfo(
            **self._basic_info_fields(raw),
            platform=raw.get("platform"),
            game_id=raw.get("game_id"),
            title=raw.get("title"),
            disc_number=raw.get("disc_number"),
            disc_version=raw.get("disc_version"),
            restored_size=raw.get("restored_size"),
            restored_size_display=raw.get("restored_size_display"),
            crc32=raw.get("crc32"),
            ratio=raw.get("ratio"),
        )

    def expected_output_size(self, input_path: str, mode: str) -> int | None:
        # The NKit header carries the restored image's exact size, so a chain
        # preflight never has to guess it from a shrink ratio.
        return self._service.restored_size(input_path)

    def active_pids(self) -> list[int]:
        return self._service.active_pids()
