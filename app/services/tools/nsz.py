"""NszTool, thin plugin wrapper delegating to ``nsz_service``.

Two modes: ``nsz_compress`` (NSP/XCI -> NSZ/XCZ) and ``nsz_decompress``
(NSZ/XCZ -> NSP/XCI). ``nsz_service.info`` is synchronous; the async contract
is satisfied by running it in a threadpool.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

from fastapi.concurrency import run_in_threadpool
from services.subprocess_runner import run_detached

from models import NszInfo, OutputStatus
from services.lock_manager import lock_manager
from services.nsz import (
    NSZ_COMPRESS_EXTENSIONS,
    NSZ_DECOMPRESS_EXTENSIONS,
    NSZ_OUTPUT_FORMATS,
    nsz_service,
)

from .base import BaseTool
from .spec import ModeKind, ModeSpec


class NszTool(BaseTool):
    id = "nsz"
    platform_slugs = frozenset({"switch", "nintendo-switch"})
    policy_owner = "nsz"
    display_name = "Switch"
    modes = (
        ModeSpec(
            mode="nsz_compress",
            tool_id="nsz",
            kind=ModeKind.COMPRESS,
            label="Compress (NSP/XCI → NSZ/XCZ)",
            group="nsz",
            output_ext=None,  # mapped from the input extension
            input_extensions=frozenset(NSZ_COMPRESS_EXTENSIONS),
            # The UI sends "<solid|block>:<level>"; the route allows the ':'
            # token only when the spec advertises level support.
            supports_compression_level=True,
            supports_delete_on_verify=True,
            allows_archive_input=True,
        ),
        ModeSpec(
            mode="nsz_decompress",
            tool_id="nsz",
            kind=ModeKind.EXTRACT,
            label="Decompress (NSZ/XCZ → NSP/XCI)",
            group="nsz",
            output_ext=None,
            input_extensions=frozenset(NSZ_DECOMPRESS_EXTENSIONS),
            # No delete-on-verify: the output is an .nsp/.xci, which is not in
            # verify_extensions (we verify compressed containers), so we can't
            # confirm the output before deleting the source.
            supports_delete_on_verify=False,
            allows_archive_input=True,
        ),
    )
    # Everything the tool can produce (both directions), for output badges.
    output_extensions = frozenset({".nsz", ".xcz", ".nsp", ".xci"})
    # nsz -V validates the compressed containers.
    verify_extensions = frozenset({".nsz", ".xcz"})

    def __init__(self, binary_path: str) -> None:
        super().__init__(binary_path)
        self._service = nsz_service

    def detect_output(
        self, input_path: str, *, from_archive: bool = False,
    ) -> OutputStatus | None:
        # Compress direction only: badge "the .nsz/.xcz already exists" next to
        # an .nsp/.xci source. Decompress-direction badging is out of scope.
        source = Path(input_path)
        if source.suffix.lower() not in NSZ_COMPRESS_EXTENSIONS:
            return None
        expected_ext = NSZ_OUTPUT_FORMATS.get(source.suffix.lower())
        if not expected_ext:
            return None
        candidate = str(source.with_suffix(expected_ext))
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

    def info_model(self, raw: dict, path: str) -> NszInfo:
        return NszInfo(**self._basic_info_fields(raw))

    async def is_ready(self) -> bool:
        """Switch content is encrypted, so nsz is useless without ``prod.keys``.

        The app ships none (they're console-specific and copyrighted); the
        operator supplies their own. Until they do, report unavailable so the
        UI hides the tool rather than offering jobs that can only fail.

        ``keys_available`` resolves the key file, which when ``SWITCH_KEYS`` is
        unset means a **recursive walk of every configured volume**. On a
        remote mount that has stopped answering that blocks in uninterruptible
        I/O, and a pooled worker taken there is never given back -- a bound on
        the *caller* frees the caller, not the thread. Every readiness caller
        fans out over the whole registry (`GET /api/tools`, the RomM platform
        list, each sweep), so repeated probes would retire the shared pool one
        worker at a time until unrelated offloads had nowhere to run.

        Detached: the abandoned thread is disposable and holds no shared
        capacity. The bound belongs to the caller, which is
        `ToolRegistry.tool_is_ready`.
        """
        return await run_detached(self._service.keys_available)

    def active_pids(self) -> list[int]:
        return self._service.active_pids()
