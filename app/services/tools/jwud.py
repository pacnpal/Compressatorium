"""JwudTool, thin plugin wrapper delegating to ``jwudtool_service``.

``jwudtool_service.info`` and its readiness probe are synchronous; the async
contract is satisfied by running them in a threadpool.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

from fastapi.concurrency import run_in_threadpool

from models import JwudInfo, OutputStatus
from services.jwudtool import (
    JWUD_COMPRESS_EXTENSIONS,
    JWUD_DECOMPRESS_EXTENSIONS,
    JWUD_OUTPUT_FORMATS,
    is_split_secondary,
    jwudtool_service,
    split_set_parts,
    verification_enabled,
)
from services.lock_manager import lock_manager

from .base import BaseTool
from .spec import ModeKind, ModeSpec


class JwudTool(BaseTool):
    id = "jwud"
    display_name = "Wii U"
    modes = (
        ModeSpec(
            mode="jwud_compress",
            tool_id="jwud",
            kind=ModeKind.COMPRESS,
            label="Compress Wii U (WUD → WUX)",
            group="jwud",
            output_ext=".wux",
            input_extensions=frozenset(JWUD_COMPRESS_EXTENSIONS),
            # Not a codec: the picker carries JWUDTool's verify / -noVerify
            # choice, the way nsz's carries solid/block. No numeric level.
            supports_compression=True,
            # Safe: the .wux output is itself verifiable, and the conversion
            # runs JWUDTool's byte-for-byte comparison against the source unless
            # the job explicitly opted out of it.
            supports_delete_on_verify=True,
            allows_archive_input=True,
        ),
        ModeSpec(
            mode="jwud_decompress",
            tool_id="jwud",
            kind=ModeKind.EXTRACT,
            label="Decompress Wii U (WUX → WUD)",
            group="jwud",
            output_ext=".wud",
            input_extensions=frozenset(JWUD_DECOMPRESS_EXTENSIONS),
            # Decompress verifies its output against the source too, so it takes
            # the same verify / -noVerify choice.
            supports_compression=True,
            # No delete-on-verify: the output is a raw .wud, which is not in
            # verify_extensions (only the WUX container carries structure to
            # check), so the output can't be confirmed before deleting the source.
            supports_delete_on_verify=False,
            allows_archive_input=True,
        ),
    )
    # Both directions produce a tracked output; only the compressed container is
    # verifiable (a raw .wud is just 25 GB of bytes with nothing to check).
    output_extensions = frozenset(JWUD_OUTPUT_FORMATS.values())
    verify_extensions = frozenset(JWUD_DECOMPRESS_EXTENSIONS)

    def __init__(self, binary_path: str) -> None:
        super().__init__(binary_path)
        self._service = jwudtool_service

    async def is_ready(self) -> bool:
        # JWUDTool is a .jar behind a launcher that needs a Java runtime, so it
        # can genuinely be absent outside the image. Hide the tool rather than
        # offering jobs that can only fail. Threadpooled: it stats the disk.
        return await run_in_threadpool(self._service.binary_available)

    def converts_path(self, path: str) -> bool:
        # A split dump is one disc image spread over game_part1…12.wud, and
        # JWUDTool joins it from part 1. The other parts stay listed (you may
        # want to see or delete them) but are not independently convertible.
        if is_split_secondary(path):
            return False
        return super().converts_path(path)

    def source_companions(self, path: str) -> list[str]:
        # Parts 2…N of a split set, so delete-on-verify takes the whole source
        # instead of orphaning ~23 GB of parts it never named.
        return split_set_parts(path)

    def delete_on_verify_is_safe(self, mode: str, compression: str | None) -> bool:
        # Our verify() walks the WUX container's structure; the format carries
        # no content checksums, so it can prove the geometry is intact but not
        # that the stored sectors match the disc. What makes delete-on-verify
        # safe is JWUDTool's byte-for-byte comparison *during* the conversion —
        # so if the job passed -noVerify, nothing ever compared the two images
        # and deleting the source would risk the only copy.
        return verification_enabled(compression)

    def detect_output(self, input_path: str) -> OutputStatus | None:
        # Compress direction only: badge "the .wux already exists" next to a
        # .wud source (mirrors z3ds/maxcso). The job pipeline's
        # check_output_conflicts still guards the decompress direction.
        source = Path(input_path)
        if source.suffix.lower() not in JWUD_COMPRESS_EXTENSIONS:
            return None
        if is_split_secondary(input_path):
            # Part 7's product is part 1's product; badging it here would show
            # the same .wux against every member of the set.
            return None
        # Resolve through the same output-path math the job uses, so a complete
        # split set badges game.wux rather than game_part1.wux — and an archive
        # member (whose synthetic path has no set behind it on disk) badges the
        # game_part1.wux the archive planner actually targets.
        candidate = self.output_path("jwud_compress", input_path)
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

    async def verify(self, path: str) -> dict:
        return await self._service.verify(path)

    def verify_stream(self, path: str) -> AsyncGenerator[dict, None]:
        return self._service.verify_stream(path)

    async def info(self, path: str) -> dict:
        return await run_in_threadpool(self._service.info, path)

    def info_model(self, raw: dict, path: str) -> JwudInfo:
        return JwudInfo(
            **self._basic_info_fields(raw),
            original_size=raw.get("original_size"),
            ratio=raw.get("ratio"),
        )

    def active_pids(self) -> list[int]:
        return self._service.active_pids()
