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
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING

from config import settings
from models import OutputStatus
from fastapi.concurrency import run_in_threadpool
from services.disk import create_scratch_dir, ensure_headroom
from services.lock_manager import lock_manager
from services.subprocess_runner import remove_partial_tree
from utils.path_utils import match_extension

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
    # The chain tool serves no single system, so each mode names its own --
    # these match maxcso's, the tool that owns step 1.
    platform_slugs=frozenset({"psp", "ps2"}),
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


# .nkit.iso/.nkit.gcz -> .iso (nkit2iso, bit-exact) -> .rvz (dolphin_rvz).
# NKit is a shrink format no emulator reads, so the restored ISO is almost never
# the thing a body wants to keep — it's a 4.7 GB stepping stone to RVZ, which
# Dolphin reads natively and which compresses better than NKit anyway. Chaining
# means the full-size ISO lives in the scratch dir and never lands in the
# library. dolphin_rvz is pinned as the target because RVZ is the format the
# README already recommends over WIA/GCZ.
NKIT_TO_RVZ = ChainSpec(
    mode="nkit_to_rvz",
    tool_id="chain",
    kind=ModeKind.COMPRESS,
    label="NKit → RVZ",
    group="chain",
    output_ext=".rvz",
    input_extensions=frozenset({".nkit.iso", ".nkit.gcz"}),
    # nkit2iso's platforms: NKit is a GameCube/Wii shrink format.
    platform_slugs=frozenset({"ngc", "gamecube", "wii"}),
    steps=(
        # RVZ compression dominates: the NKit restore is a linear rebuild, while
        # dolphin-tool re-compresses the whole disc.
        ChainStep(tool_id="nkit", mode="nkit_restore", weight=0.35, output_ratio=3.0),
        ChainStep(tool_id="dolphin", mode="dolphin_rvz", weight=0.65, output_ratio=1.0),
    ),
    intermediate_exts=(".iso",),
    verify_step=1,
    # No compression knob, same as cso_to_chd and for a sharper reason: the
    # RVZ codec/level guards in ``_validate_request_compression`` are keyed on
    # ``spec.tool_id == "dolphin"``, and a chain's tool_id is "chain", so
    # advertising the picker here would accept a comma-joined codec list the
    # route can't reject and dolphin can't honor. Making that guard chain-aware
    # means reading ``ChainSpec.steps`` from the route, which the design keeps
    # exclusive to ChainTool. The final RVZ therefore uses dolphin's defaults;
    # pick a level by running nkit_restore and dolphin_rvz as two jobs.
    supports_compression=False,
    supports_compression_level=False,
    # NO delete-on-verify, even though dolphin can verify the final .rvz. That
    # verify is *structural* — it confirms the RVZ container, not that its
    # contents match the original disc. When the source is a Wii image whose
    # update partition was removed and NKIT2ISO_RECOVERY is "none", step 1
    # zero-fills the gap and skips its CRC32 check, yet the resulting RVZ still
    # verifies cleanly. Deleting the source on that evidence would destroy the
    # only file a later NKIT2ISO_RECOVERY=download run could restore bit-exact.
    # The flag is a static ModeSpec field read at plan time, before the restore
    # can report whether it was exact, so there is no safe "only when exact"
    # setting — and the failure mode is silent data loss. Off.
    supports_delete_on_verify=False,
    allows_archive_input=True,
)


class ChainTool(BaseTool):
    id = "chain"
    display_name = "Pipeline"
    modes = (CSO_TO_CHD, NKIT_TO_RVZ)
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
        """Name the chain's product: the FIRST step's stem, the chain's suffix.

        The first step owns the input, so only its tool knows how to strip the
        source name correctly — ``nkit_restore`` has to drop a whole compound
        ``.nkit.iso``, which the final step's tool (dolphin) would leave as
        ``Game.nkit.rvz``. Taking the first step's own output path and swapping
        in the chain's declared ``output_ext`` gets both right, and is identical
        to delegating to the last step for a plain-suffix chain like
        ``cso_to_chd`` (``Game.cso`` -> ``Game.iso`` -> ``Game.chd``).
        """
        spec = self.spec(mode)
        first = spec.steps[0]
        intermediate = self._registry.for_mode(first.mode).output_path(
            first.mode, input_path, output_dir, treat_as_stem=treat_as_stem,
        )
        if not spec.output_ext:
            return intermediate
        # The intermediate always carries a plain single suffix (it is a
        # tool-written file, not a user-named one), so with_suffix is safe here.
        return str(Path(intermediate).with_suffix(spec.output_ext))

    def detect_output(
        self, input_path: str, *, from_archive: bool = False,
    ) -> OutputStatus | None:
        """Badge the source when the chain's own product already exists.

        Resolved per chain spec rather than against a literal ``.chd``: the
        candidate comes from whichever mode accepts this input, so a second
        chain with a different final format badges its own output instead of
        silently probing the first chain's.
        """
        for spec in self.modes:
            if (
                match_extension(input_path, spec.input_extensions) is None
                or not spec.output_ext
            ):
                continue
            candidate = self.output_path(spec.mode, input_path)
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
            # Off the event loop: the preflight stats the source, asks the
            # first step's plugin for a header-derived size (a read, and a zlib
            # block for a .nkit.gcz), and calls statvfs on two volumes — all
            # blocking, and all potentially slow on a network mount.
            await run_in_threadpool(
                self._preflight_headroom, input_path, output_path, spec, work_dir,
            )

            total_weight = sum(s.weight for s in spec.steps) or 1.0
            weights = [s.weight / total_weight for s in spec.steps]
            n = len(spec.steps)
            # Name the intermediate off the FIRST step's own output path, not
            # Path(input_path).stem: a compound-extension source (.nkit.iso)
            # would otherwise leave "Game.nkit" as the stem and write the
            # scratch ISO as "Game.nkit.iso".
            stem = Path(
                self._registry.for_mode(spec.steps[0].mode).output_path(
                    spec.steps[0].mode, input_path,
                )
            ).stem
            current_in = input_path
            cumulative = 0.0
            intermediate_source = input_path  # what feeds the final (for tagging)
            # Caveats a step raised about its own output. A later step's
            # messages replace the earlier ones in the job row, so anything a
            # step needs the operator to KNOW (nkit2iso restoring a Wii image
            # without its update partition: playable, but not bit-exact) has to
            # be carried to the terminal message or it is lost. Steps opt in by
            # marking an update with ``warning``; job_manager reads only
            # progress/message, so the extra key is inert everywhere else.
            caveats: list[str] = []

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
                    if update.get("warning") and last_message not in caveats:
                        caveats.append(last_message)
                    raw = update.get("progress") or 0
                    aggregate = int(round(base + span * (raw / 100.0)))
                    step_update = {
                        "progress": min(max(aggregate, 0), 100),
                        "message": f"[{i + 1}/{n}] {last_message}",
                    }
                    # Forward the runner's liveness signal. A chained job runs
                    # far longer than a single one, so dropping it here would
                    # have every chain reported stalled once it passed
                    # debug_progress_timeout while making steady progress.
                    if update.get("activity"):
                        step_update["activity"] = True
                    yield step_update
                cumulative += weights[i]
                current_in = step_out

            # Tag the final CHD via the last step's post_convert hook — the same
            # disc-ID path a direct createdvd job takes — while the intermediate
            # source still exists (work_dir is removed in the finally below).
            final_step = spec.steps[-1]
            await self._registry.for_mode(final_step.mode).post_convert(
                intermediate_source, output_path, final_step.mode,
            )
            complete = "Conversion complete"
            if caveats:
                # Keep the caveat AND the fact that the chain finished: the
                # output is real and usable, it just isn't what an unqualified
                # "complete" would imply.
                yield {
                    "progress": 100,
                    "message": f"{complete} — {' '.join(caveats)}",
                    "warning": True,
                }
            else:
                yield {"progress": 100, "message": complete}
        finally:
            # Holds the intermediate a failed step left behind, so this is the
            # chain's partial-output sweep: bounded like every tool's, and off
            # the event loop, so a wedged scratch volume can't keep the job (and
            # the inline queue) from finalising.
            await remove_partial_tree(work_dir, label="chain work dir")

    def _preflight_headroom(
        self, input_path: str, output_path: str, spec: ChainSpec, work_dir: str,
    ) -> None:
        try:
            input_size = os.path.getsize(input_path)
        except OSError:
            return  # can't size the input; skip rather than block the job
        if input_size <= 0:
            return
        # Prefer the true intermediate size the FIRST step's tool can read out
        # of the source header, instead of a ratio on the (possibly tiny)
        # compressed input: a heavily compressed .cso/.zso/.dax, or a scrubbed
        # .nkit.iso, can be a small fraction of the .iso its tool will write.
        # Asked through the plugin contract rather than importing one tool's
        # reader here, so a new chain's first step brings its own answer.
        first = spec.steps[0]
        expected = self._registry.for_mode(first.mode).expected_output_size(
            input_path, first.mode,
        )
        if expected and expected > 0:
            # Every chain so far ends in a compressing step, so the final output
            # is at most the intermediate — a safe bound for both targets.
            intermediate_bytes = expected
            final_bytes = expected
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

    async def verify(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> dict:
        return await self._final_tool(path).verify(path, cancel_event=cancel_event)

    def verify_stream(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        return self._final_tool(path).verify_stream(path, cancel_event=cancel_event)

    async def verify_timeout(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> int:
        # The final step's tool runs the verify, so its policy owner (and any
        # per-tool override) sets the bound -- not the synthetic "chain" owner.
        return await self._final_tool(path).verify_timeout(
            path, cancel_event=cancel_event,
        )

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
