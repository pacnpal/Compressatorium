"""ChainTool: a synthetic tool that runs an ordered pipeline of existing modes.

The chaining seam sits **above** the per-tool plugin contract rather than
rewriting it: ``ChainTool`` owns composite ``ChainSpec`` modes and orchestrates
them by calling the existing tools' ``convert`` through the registry. Its first
user is ``cso_to_chd`` (maxcso ``cso_decompress`` -> chdman ``createdvd``), which
needs no new binary.

Responsibilities unique to a chain:

* the intermediate (``.iso``) lives in a private temp work dir, cleaned up
  whichever way the job ends;
* a disk-headroom preflight, because a chain holds source + full intermediate +
  partial final at once (see ``services.disk``);
* weighted progress aggregation into one 0-100 bar (chd compression dominates
  cso decompression, so an even split misreports);
* the final verify and disc-ID tagging delegate to the last step's tool
  (chdman), so a ``cso_to_chd`` CHD is verified and tagged exactly like a direct
  ``createdvd``.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING

from config import settings
from models import OutputStatus
from services.disk import create_scratch_dir, ensure_headroom
from services.lock_manager import lock_manager
from services.maxcso import uncompressed_iso_size

from .base import BaseTool
from .spec import ChainSpec, ChainStep, ModeKind

if TYPE_CHECKING:
    from pydantic import BaseModel

    from .registry import ToolRegistry

# cso/zso/dax -> .iso (maxcso, lossless) -> .chd (chdman createdvd).
# createdvd is pinned: a cso/zso/dax is an ISO/data-only image with no CD audio
# tracks to preserve, so the createcd (cue/gdi multitrack) path never applies.
CSO_TO_CHD = ChainSpec(
    mode="cso_to_chd",
    tool_id="chain",
    kind=ModeKind.CREATE,
    label="CSO/ZSO/DAX → CHD",
    group="chain",
    output_ext=".chd",
    input_extensions=frozenset({".cso", ".zso", ".dax"}),
    steps=(
        # weights reflect that chd compression is far slower than cso decompress.
        ChainStep(tool_id="cso", mode="cso_decompress", weight=0.20, output_ratio=2.0),
        ChainStep(tool_id="chdman", mode="createdvd", weight=0.80, output_ratio=1.2),
    ),
    intermediate_exts=(".iso",),
    verify_step=1,
    # No compression knob for now: the CSO tool's UI offers maxcso effort
    # presets, which are meaningless to the chdman step that compresses the
    # .chd. The final CHD uses chdman's default codecs. Exposing chdman codecs
    # for the chain is deferred to the UI-placement decision.
    supports_compression=False,
    supports_delete_on_verify=True,
    allows_archive_input=True,
)


class ChainTool(BaseTool):
    id = "chain"
    display_name = "Pipeline"
    modes = (CSO_TO_CHD,)
    # The chain's outputs/verify are owned by the final step's tool (chdman
    # already claims .chd), so the chain claims neither set — it must not
    # double-register .chd in verify_extensions / output_extensions.
    output_extensions = frozenset()
    verify_extensions = frozenset()

    def __init__(self, registry: "ToolRegistry") -> None:
        # No binary of its own; it drives the component tools via the registry,
        # including the final step's post_convert hook for disc-ID tagging.
        super().__init__("")
        self._registry = registry

    # ------------------------------------------------------------------ paths
    def output_path(
        self,
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        final = self.spec(mode).steps[-1]
        return self._registry.for_mode(final.mode).output_path(
            final.mode, input_path, output_dir, treat_as_stem=treat_as_stem,
        )

    def detect_output(
        self, input_path: str, *, from_archive: bool = False,
    ) -> OutputStatus | None:
        """Badge the source when the chain's own product already exists.

        Resolved per chain spec rather than against a literal ``.chd``: the
        candidate extension comes from whichever mode accepts this input, so a
        second chain with a different final format badges its own output
        instead of silently probing the first chain's.
        """
        source = Path(input_path)
        ext = source.suffix.lower()
        for spec in self.modes:
            if ext not in spec.input_extensions or not spec.output_ext:
                continue
            candidate = str(source.with_suffix(spec.output_ext))
            file_exists, is_locked = lock_manager.check_file_status(candidate)
            if not (file_exists or is_locked):
                continue
            return OutputStatus(
                tool_id=self.id,
                exists=file_exists,
                ready=file_exists and not is_locked,
                path=candidate,
            )
        return None

    # --------------------------------------------------------------- convert
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
        return self._run(
            input_path, output_path, mode,
            compression=compression, cancel_event=cancel_event,
        )

    async def _run(
        self,
        input_path: str,
        output_path: str,
        mode: str,
        *,
        compression: str | None,
        cancel_event: asyncio.Event | None,
    ) -> AsyncGenerator[dict, None]:
        spec = self.spec(mode)
        work_dir = create_scratch_dir("cmptr-chain-")
        try:
            self._preflight_headroom(input_path, output_path, spec, work_dir)

            total_weight = sum(s.weight for s in spec.steps) or 1.0
            weights = [s.weight / total_weight for s in spec.steps]
            n = len(spec.steps)
            stem = Path(input_path).stem
            current_in = input_path
            cumulative = 0.0
            intermediate_source = input_path  # what feeds the final (for tagging)

            for i, step in enumerate(spec.steps):
                tool = self._registry.for_mode(step.mode)
                if i == n - 1:
                    step_out = output_path
                else:
                    ext = (
                        spec.intermediate_exts[i]
                        if i < len(spec.intermediate_exts)
                        else ".tmp"
                    )
                    step_out = os.path.join(work_dir, f"{stem}{ext}")
                    intermediate_source = step_out
                # Only forward compression when the chain itself advertises it
                # AND the sub-step supports it. cso_to_chd advertises no
                # compression (the CSO UI's effort presets are meaningless to
                # chdman), so a stale preset from an API/batch client is dropped
                # rather than smuggled in as a chdman codec.
                step_compression = (
                    compression
                    if spec.supports_compression
                    and self._registry.spec(step.mode).supports_compression
                    else None
                )

                base = cumulative * 100.0
                span = weights[i] * 100.0
                last_message = ""
                async for update in tool.convert(
                    current_in, step_out, step.mode,
                    compression=step_compression, cancel_event=cancel_event,
                ):
                    last_message = update.get("message") or last_message
                    raw = update.get("progress") or 0
                    aggregate = int(round(base + span * (raw / 100.0)))
                    yield {
                        "progress": min(max(aggregate, 0), 100),
                        "message": f"[{i + 1}/{n}] {last_message}",
                    }
                cumulative += weights[i]
                current_in = step_out

            # Tag the final CHD via the last step's post_convert hook — the same
            # disc-ID path a direct createdvd job takes — while the intermediate
            # source still exists (work_dir is removed in the finally below).
            final_step = spec.steps[-1]
            await self._registry.for_mode(final_step.mode).post_convert(
                intermediate_source, output_path, final_step.mode,
            )
            yield {"progress": 100, "message": "Conversion complete"}
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _preflight_headroom(
        self, input_path: str, output_path: str, spec: ChainSpec, work_dir: str,
    ) -> None:
        try:
            input_size = os.path.getsize(input_path)
        except OSError:
            return  # can't size the input; skip rather than block the job
        if input_size <= 0:
            return
        # Prefer the true uncompressed ISO size from the container header: a
        # highly compressed .cso/.zso/.dax can be a small fraction of the ISO
        # maxcso will write, so a ratio on the compressed size badly under-counts
        # the intermediate. The final .chd is at most the ISO size (chdman
        # compresses), so the uncompressed size is a safe bound for both.
        uncompressed = uncompressed_iso_size(input_path)
        if uncompressed and uncompressed > 0:
            intermediate_bytes = uncompressed
            final_bytes = uncompressed
        else:
            n = len(spec.steps)
            intermediate_bytes = int(
                input_size * sum(s.output_ratio for s in spec.steps[: n - 1])
            )
            final_bytes = int(input_size * spec.steps[-1].output_ratio)
        margin = int(getattr(settings, "chain_disk_margin_mb", 512)) * 1024 * 1024
        targets: list[tuple[str, int]] = [(output_path, final_bytes)]
        if intermediate_bytes > 0:
            targets.append((work_dir, intermediate_bytes))
        ensure_headroom(targets, margin_bytes=margin)

    # ------------------------------------------------------ verify / info
    def _spec_for_output(self, path: str):
        """The chain spec whose final product ``path`` is.

        Verify/info arrive with a path and no mode, so the chain is identified
        by its output extension. Falls back to the first spec when nothing
        matches, which is also the single-chain case today.
        """
        ext = Path(path).suffix.lower()
        for spec in self.modes:
            if spec.output_ext and spec.output_ext.lower() == ext:
                return spec
        return self.modes[0]

    def _final_tool(self, path: str):
        """The registered tool that owns verify/info for ``path``.

        Resolved from the matching spec's ``verify_step`` rather than
        ``modes[0]``, so a second chain ending in a different tool delegates to
        *its* final tool instead of the first chain's.
        """
        spec = self._spec_for_output(path)
        step = spec.steps[spec.verify_step]
        return self._registry.get(step.tool_id)

    async def verify(self, path: str) -> dict:
        return await self._final_tool(path).verify(path)

    def verify_stream(self, path: str) -> AsyncGenerator[dict, None]:
        return self._final_tool(path).verify_stream(path)

    async def info(self, path: str) -> dict:
        return await self._final_tool(path).info(path)

    def info_model(self, raw: dict, path: str) -> "BaseModel":
        return self._final_tool(path).info_model(raw, path)

    def active_pids(self) -> list[int]:
        seen: set[int] = set()
        pids: list[int] = []
        tool_ids = {step.tool_id for m in self.modes for step in m.steps}
        for tool_id in tool_ids:
            for pid in self._registry.get(tool_id).active_pids():
                if pid not in seen:
                    seen.add(pid)
                    pids.append(pid)
        return pids
