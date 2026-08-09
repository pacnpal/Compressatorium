"""Nkit2IsoTool, thin plugin wrapper delegating to ``nkit2iso_service``.

Registers no info or verify routes. nkit2iso has no info or verify subcommand:
integrity is the header CRC32 the binary checks inline during the restore, so a
completed job *is* the verification and there is nothing to re-run against a
finished ``.iso``. ``makeps3iso`` is the precedent for a route-less tool.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from models import OutputStatus
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

    def detect_output(self, input_path: str) -> OutputStatus | None:
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

    def active_pids(self) -> list[int]:
        return self._service.active_pids()
