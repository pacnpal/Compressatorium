"""MaxcsoTool, thin plugin wrapper delegating to ``maxcso_service``.

Five modes: ``cso_compress`` (.iso -> CSO v1), ``cso2_compress`` (.iso -> CSO v2),
``zso_compress`` (.iso -> .zso), ``dax_compress`` (.iso -> .dax), and
``cso_decompress`` (.cso/.zso/.dax -> .iso). CSO v1 and v2 share the .cso
extension; the version differs internally. ``maxcso_service.info`` is
synchronous; the async contract is satisfied by running it in a threadpool.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

from fastapi.concurrency import run_in_threadpool

from models import CsoInfo, OutputStatus
from services.lock_manager import lock_manager
from services.maxcso import (
    MAXCSO_COMPRESS_EXTENSIONS,
    MAXCSO_DECOMPRESS_EXTENSIONS,
    maxcso_service,
    uncompressed_iso_size,
)

from .base import BaseTool
from .spec import ModeKind, ModeSpec

# The .iso a file-list row offers maps to its .cso sibling for the "output
# already exists" badge (the common, default product).
_PRIMARY_OUTPUT_EXT = ".cso"


class MaxcsoTool(BaseTool):
    id = "cso"
    display_name = "CSO"
    modes = (
        ModeSpec(
            mode="cso_compress",
            tool_id="cso",
            kind=ModeKind.COMPRESS,
            label="Compress ISO → CSO",
            group="cso",
            output_ext=".cso",
            input_extensions=frozenset(MAXCSO_COMPRESS_EXTENSIONS),
            # The UI sends an effort preset (fast|default|max), no numeric level.
            supports_compression=True,
            supports_delete_on_verify=True,
            allows_archive_input=True,
        ),
        ModeSpec(
            mode="cso2_compress",
            tool_id="cso",
            kind=ModeKind.COMPRESS,
            label="Compress ISO → CSO v2",
            group="cso",
            output_ext=".cso",
            input_extensions=frozenset(MAXCSO_COMPRESS_EXTENSIONS),
            supports_compression=True,
            supports_delete_on_verify=True,
            allows_archive_input=True,
        ),
        ModeSpec(
            mode="zso_compress",
            tool_id="cso",
            kind=ModeKind.COMPRESS,
            label="Compress ISO → ZSO",
            group="cso",
            output_ext=".zso",
            input_extensions=frozenset(MAXCSO_COMPRESS_EXTENSIONS),
            supports_compression=True,
            supports_delete_on_verify=True,
            allows_archive_input=True,
        ),
        ModeSpec(
            mode="dax_compress",
            tool_id="cso",
            kind=ModeKind.COMPRESS,
            label="Compress ISO → DAX",
            group="cso",
            output_ext=".dax",
            input_extensions=frozenset(MAXCSO_COMPRESS_EXTENSIONS),
            supports_compression=True,
            supports_delete_on_verify=True,
            allows_archive_input=True,
        ),
        ModeSpec(
            mode="cso_decompress",
            tool_id="cso",
            kind=ModeKind.EXTRACT,
            label="Decompress CSO/ZSO/DAX → ISO",
            group="cso",
            output_ext=".iso",
            input_extensions=frozenset(MAXCSO_DECOMPRESS_EXTENSIONS),
            # No delete-on-verify: the output is an .iso, which is not in
            # verify_extensions (we verify compressed containers), so we can't
            # confirm the output before deleting the source.
            supports_delete_on_verify=False,
            allows_archive_input=True,
        ),
    )
    # Everything the tool can produce (both directions), for output badges.
    output_extensions = frozenset({".cso", ".zso", ".dax", ".iso"})
    # maxcso --crc validates the compressed containers.
    verify_extensions = frozenset(MAXCSO_DECOMPRESS_EXTENSIONS)

    def __init__(self, binary_path: str) -> None:
        super().__init__(binary_path)
        self._service = maxcso_service

    def detect_output(self, input_path: str) -> OutputStatus | None:
        # Compress direction only: badge "the .cso/.zso/.dax already exists" next
        # to an .iso source. Decompress-direction badging is out of scope.
        source = Path(input_path)
        if source.suffix.lower() not in MAXCSO_COMPRESS_EXTENSIONS:
            return None
        # Any compress target counts (.cso covers both CSO v1 and v2); .cso (the
        # default) is checked first, then the other formats this tool can write.
        for ext in (_PRIMARY_OUTPUT_EXT, ".zso", ".dax"):
            candidate = str(source.with_suffix(ext))
            file_exists, is_converting = lock_manager.check_file_status(candidate)
            if file_exists or is_converting:
                return OutputStatus(
                    tool_id=self.id,
                    exists=file_exists,
                    ready=file_exists and not is_converting,
                    path=candidate,
                )
        return None

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

    async def verify(self, path: str) -> dict:
        return await self._service.verify(path)

    def verify_stream(self, path: str) -> AsyncGenerator[dict, None]:
        return self._service.verify_stream(path)

    async def info(self, path: str) -> dict:
        return await run_in_threadpool(self._service.info, path)

    def info_model(self, raw: dict, path: str) -> CsoInfo:
        return CsoInfo(**self._basic_info_fields(raw))

    def expected_output_size(self, input_path: str, mode: str) -> int | None:
        # Decompress only: the CSO/ZSO/DAX header states the ISO size, which is
        # what a chain preflight needs (a highly compressed container is a small
        # fraction of the ISO maxcso writes, so a ratio badly under-counts). The
        # compress direction's output size isn't knowable up front.
        if mode != "cso_decompress":
            return None
        return uncompressed_iso_size(input_path)

    def active_pids(self) -> list[int]:
        return self._service.active_pids()
