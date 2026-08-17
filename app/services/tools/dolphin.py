"""DolphinTool, thin plugin wrapper delegating to ``dolphin_tool_service``."""
from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator
from pathlib import Path

from fastapi.concurrency import run_in_threadpool

from config import settings
from models import DolphinDiscInfo, OutputStatus
from services.dolphin_tool import (
    DOLPHIN_CONVERTIBLE_EXTENSIONS,
    DOLPHIN_OUTPUT_FORMATS,
    dolphin_tool_service,
)
from services.lock_manager import lock_manager
from services.subprocess_runner import reraise_if_abandoned
from services.workload_limiter import workload_limiter

from .base import BaseTool, EmbeddedHashUnavailable
from .spec import ModeKind, ModeSpec

# mode -> (label, output_ext, kind, supports_compression, supports_level)
_MODES = {
    "dolphin_rvz": ("Dolphin RVZ", ".rvz", ModeKind.COMPRESS, True, True),
    "dolphin_wia": ("Dolphin WIA", ".wia", ModeKind.COMPRESS, True, True),
    "dolphin_gcz": ("Dolphin GCZ", ".gcz", ModeKind.COMPRESS, False, False),
    "dolphin_iso": ("Dolphin ISO", ".iso", ModeKind.EXTRACT, False, False),
}

# Ordered, de-duplicated output extensions; iteration order decides which
# sibling wins when several candidates exist.
_DOLPHIN_OUTPUT_EXTENSIONS = tuple(
    dict.fromkeys(ext for _, ext in DOLPHIN_OUTPUT_FORMATS.values()),
)


def _build_modes() -> list[ModeSpec]:
    return [
        ModeSpec(
            mode=mode,
            tool_id="dolphin",
            kind=kind,
            label=label,
            group="dolphin",
            output_ext=ext,
            input_extensions=frozenset(DOLPHIN_CONVERTIBLE_EXTENSIONS),
            supports_compression=supports_compression,
            supports_compression_level=supports_level,
            supports_delete_on_verify=True,
            allows_archive_input=True,
        )
        for mode, (label, ext, kind, supports_compression, supports_level)
        in _MODES.items()
    ]


class DolphinTool(BaseTool):
    id = "dolphin"
    display_name = "Dolphin"
    modes = _build_modes()
    output_extensions = frozenset({".rvz", ".wia", ".gcz", ".iso"})
    verify_extensions = frozenset(DOLPHIN_CONVERTIBLE_EXTENSIONS)
    # The compressed/container formats (.rvz/.wia/.gcz/.wbfs) are recompressed
    # by dolphin-tool, so their on-disk bytes never appear in a DAT — only the
    # reconstructed disc SHA1 does. A disc-hash miss is therefore definitive;
    # don't fall back to a (meaningless) file-level hash of the container.
    embedded_hash_is_exhaustive = True

    def __init__(self, binary_path: str) -> None:
        super().__init__(binary_path)
        self._service = dolphin_tool_service

    def detect_output(
        self, input_path: str, *, from_archive: bool = False,
    ) -> OutputStatus | None:
        source = Path(input_path)
        source_ext = source.suffix.lower()
        if source_ext not in self.input_extensions:
            return None

        candidate_paths: list[str] = []
        if source_ext in _DOLPHIN_OUTPUT_EXTENSIONS and source_ext != ".iso":
            candidate_paths.append(str(source))
        for output_ext in _DOLPHIN_OUTPUT_EXTENSIONS:
            if output_ext == source_ext:
                continue
            candidate_paths.append(str(source.with_suffix(output_ext)))

        converting_path: str | None = None
        for candidate_path in candidate_paths:
            file_exists, is_converting = lock_manager.check_file_status(candidate_path)
            if file_exists:
                return OutputStatus(
                    tool_id=self.id,
                    exists=True,
                    ready=not is_converting,
                    path=candidate_path,
                )
            if is_converting and converting_path is None:
                converting_path = candidate_path

        if converting_path:
            return OutputStatus(
                tool_id=self.id, exists=False, ready=False, path=converting_path,
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
        return await self._service.header(path)

    async def embedded_hashes(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> list[tuple[str, str]]:
        # A plain .iso is already the raw redump image, its file-level SHA1
        # *is* the disc hash, so skip the expensive verify pass and let the
        # caller fall back to cheap file hashing. The compressed/container
        # formats (.rvz/.wia/.gcz/.wbfs) must be reconstructed by dolphin-tool
        # before hashing, so only those go through verify.
        if Path(path).suffix.lower() == ".iso":
            return []
        # Respect the operator-configured match size cap: dolphin-tool verify
        # reconstructs and hashes the entire disc, which is exactly the
        # expensive work MATCH_MAX_FILE_SIZE exists to bound. Over-cap files
        # report no hash so the caller's size-cap handling (file-level path)
        # short-circuits instead of paying for a full verify. CHD's hook reads
        # cached hashes and is intentionally not capped.
        size_cap = max(0, int(getattr(settings, "match_max_file_size", 0) or 0))
        if size_cap > 0:
            try:
                size_bytes = await run_in_threadpool(os.path.getsize, path)
            except OSError as e:
                # Couldn't stat the file: for an exhaustive tool this is an
                # inability to derive the disc hash, not "no embedded hash".
                # Surface it as non-cacheable so the caller doesn't fall back
                # to a (meaningless) container SHA1 and cache a false negative.
                raise EmbeddedHashUnavailable(
                    f"could not stat {path}: {e}",
                ) from e
            if size_bytes > size_cap:
                # Over the cap: report no embedded hash and let the caller's
                # own size-cap handling return a non-cacheable "file too large"
                # rather than paying for a full-disc verify.
                return []
        # Gate the verify under the "match" workload lane so MAX_MATCH_CONCURRENCY
        # bounds how many full-disc reconstructions run at once, even across a
        # metadata scan and concurrent /dat/match requests.
        try:
            async with await workload_limiter.acquire("match"):
                hashes = await self._service.disc_hashes(
                    path, cancel_event=cancel_event,
                )
        except Exception as e:
            # One exception is not a fact about this file: a verify child that
            # outlived SIGKILL is still running and still hammering the same
            # mount, so folding it into EmbeddedHashUnavailable would tell the
            # caller to record an error and move on, stranding another full-disc
            # reconstruction on every remaining file (issue #268).
            reraise_if_abandoned(e)
            # Spawn failure (e.g. binary missing) or unexpected error: treat as
            # a transient inability to derive the hash, not "no hash exists".
            raise EmbeddedHashUnavailable(
                f"dolphin-tool verify failed for {path}: {e}",
            ) from e
        if not hashes:
            # A compressed Dolphin container must yield a disc hash; an empty
            # result means verify failed / produced nothing. Signal a
            # non-cacheable miss rather than collapsing to the container's
            # (meaningless-against-Redump) file SHA1 and caching it.
            raise EmbeddedHashUnavailable(
                f"dolphin-tool verify produced no hash for {path}",
            )
        return [(h, "dolphin_disc_sha1") for h in hashes]

    def info_model(self, raw: dict, path: str) -> DolphinDiscInfo:
        return DolphinDiscInfo(
            file=path,
            game_id=raw.get("game_id"),
            game_name=(
                raw.get("game_name")
                or raw.get("internal_name")
                or raw.get("name")
            ),
            title_id=raw.get("title_id"),
            disc_number=raw.get("disc_number") or raw.get("disc"),
            revision=raw.get("revision"),
            region=raw.get("region"),
            country=raw.get("country"),
            format=raw.get("format"),
            compression=raw.get("compression") or raw.get("compression_method"),
            compression_level=raw.get("compression_level"),
            block_size=raw.get("block_size"),
            file_size=raw.get("file_size"),
            raw_data=raw.get("raw_data", ""),
        )

    def active_pids(self) -> list[int]:
        return self._service.active_pids()
