"""RomzTool, thin plugin wrapper delegating to ``romz_service``.

Packs handheld ROM dumps (GB/GBC/GBA/NDS) into ``.7z``/``.zip`` archives and
extracts them back. ``romz_service.info`` is synchronous; the async contract is
satisfied by running it in a threadpool.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

from fastapi.concurrency import run_in_threadpool

from models import OutputStatus, RomzInfo
from services.lock_manager import lock_manager
from services.romz import (
    ROMZ_ARCHIVE_EXTENSIONS,
    ROMZ_COMPRESS_EXTENSIONS,
    ROMZ_OUTPUT_BY_MODE,
    romz_service,
)

from .base import BaseTool
from .spec import ModeKind, ModeSpec

# The compress-direction badge prefers .7z (the default product), then .zip.
_PRIMARY_OUTPUT_EXT = ".7z"


class RomzTool(BaseTool):
    id = "romz"
    # Cartridge/handheld ROMs, where plain .zip/.7z is the archival norm.
    # RomM's canonical slugs first (`gamegear`, `virtualboy`, `neo-geo-pocket`,
    # …). The shorter aliases stay: matching is exact, and a slug no tool claims
    # simply falls back to extension-only narrowing, so an extra name costs
    # nothing while a missing canonical one is the actual bug.
    platform_slugs = frozenset({
        "gb", "gbc", "gba", "nds", "nes", "snes", "n64",
        "sms", "sega-master-system",
        "genesis", "segamd", "genesis-slash-megadrive",
        "gamegear", "gg", "game-gear",
        "virtualboy", "vb",
        "wonderswan",
        "neo-geo-pocket", "ngp",
        "lynx", "c64", "atari2600", "atari7800",
    })
    policy_owner = "romz"
    display_name = "Handheld ROM"
    modes = (
        ModeSpec(
            mode="romz_7z",
            tool_id="romz",
            kind=ModeKind.COMPRESS,
            label="Compress ROM → 7z",
            group="romz",
            output_ext=".7z",
            input_extensions=frozenset(ROMZ_COMPRESS_EXTENSIONS),
            # The UI sends an effort preset (fast|default|max), no numeric level.
            supports_compression=True,
            supports_delete_on_verify=True,
            # ROMs are compressed loose, not from inside another archive (that
            # would be recursive), so a ROM member is never offered as a
            # conversion input. It is still *visible* when browsing into a romz
            # archive — the listing surfaces every known source extension, and
            # ROM extensions are romz sources — but its convertible_by stays
            # empty (no allows_archive_input mode accepts it).
            allows_archive_input=False,
        ),
        ModeSpec(
            mode="romz_zip",
            tool_id="romz",
            kind=ModeKind.COMPRESS,
            label="Compress ROM → zip",
            group="romz",
            output_ext=".zip",
            input_extensions=frozenset(ROMZ_COMPRESS_EXTENSIONS),
            supports_compression=True,
            supports_delete_on_verify=True,
            allows_archive_input=False,
        ),
        ModeSpec(
            mode="romz_extract",
            tool_id="romz",
            kind=ModeKind.EXTRACT,
            label="Extract ROM ← 7z/zip",
            group="romz",
            output_ext=None,  # restored from the single archived member
            input_extensions=frozenset(ROMZ_ARCHIVE_EXTENSIONS),
            # No delete-on-verify: the output is a raw ROM, which is not in
            # verify_extensions (we verify archives), so we can't confirm the
            # output before deleting the source.
            supports_delete_on_verify=False,
            allows_archive_input=False,
        ),
    )
    # Everything the tool can produce (both directions), for output badges and
    # the registry-driven library scan.
    output_extensions = frozenset(
        set(ROMZ_OUTPUT_BY_MODE.values()) | ROMZ_COMPRESS_EXTENSIONS,
    )
    # `7z t` validates the archives this tool writes.
    verify_extensions = frozenset(ROMZ_ARCHIVE_EXTENSIONS)

    def __init__(self, binary_path: str) -> None:
        super().__init__(binary_path)
        self._service = romz_service

    def detect_output(
        self, input_path: str, *, from_archive: bool = False,
    ) -> OutputStatus | None:
        # Compress direction only: badge "the .7z/.zip already exists" next to a
        # ROM source. Extract-direction badging is out of scope.
        source = Path(input_path)
        if source.suffix.lower() not in ROMZ_COMPRESS_EXTENSIONS:
            return None
        # Output names preserve the ROM extension (Game.gba -> Game.gba.7z), so
        # the candidate is the full filename plus the archive suffix.
        for ext in (_PRIMARY_OUTPUT_EXT, ".zip"):
            candidate = f"{input_path}{ext}"
            file_exists, is_converting = lock_manager.check_file_status(candidate)
            if is_converting:
                # Mid-conversion: badge the in-progress output without inspecting
                # the (still-incomplete) archive.
                return OutputStatus(
                    tool_id=self.id, exists=file_exists, ready=False, path=candidate,
                )
            # A *finished* sibling only counts as a romz output when it's actually
            # one of the single-ROM archives romz produces — not an unrelated
            # multi-file/corrupt archive that merely matches the Game.gba.7z
            # naming convention. Without this the source row's verify-from-output
            # flow (RowActionsMenu) would offer a romz Verify on a non-romz
            # archive whose own listing reports verifiable_by == [] (issue #146).
            if file_exists and self._service.is_single_rom_archive(candidate):
                return OutputStatus(
                    tool_id=self.id, exists=True, ready=True, path=candidate,
                )
        return None

    def verifies_path(self, path: str) -> bool:
        # romz claims .7z/.zip broadly (extract-mode inputs), but verify/info
        # only apply to the single-ROM archives it produces. Confirm exactly
        # one handheld-ROM member instead of trusting the extension, so the
        # Verify/Info row-actions aren't offered on unrelated multi-file
        # archives the user happens to have.
        if Path(path).suffix.lower() not in self.verify_extensions:
            return False
        return self._service.is_single_rom_archive(path)

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

    async def verify(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> dict:
        return await self._service.verify(path, cancel_event=cancel_event)

    def verify_stream(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        return self._service.verify_stream(path, cancel_event=cancel_event)

    async def info(self, path: str) -> dict:
        return await run_in_threadpool(self._service.info, path)

    def info_model(self, raw: dict, path: str) -> RomzInfo:
        return RomzInfo(
            **self._basic_info_fields(raw),
            contained_name=raw.get("contained_name"),
            original_size=raw.get("original_size"),
            ratio=raw.get("ratio"),
        )

    def active_pids(self) -> list[int]:
        return self._service.active_pids()
