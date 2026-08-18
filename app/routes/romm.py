"""API routes for the RomM catalog overlay.

RomM answers "what platform is this, and what is it called" for files that are
already on a configured volume.  Everything downstream — convertibility,
already-converted detection, queueing, progress — is existing machinery: the
ROM listing is returned as an ordinary :class:`DirectoryListing` of
:class:`FileEntry`, so the browser renders it with the same ``FileList`` /
``ConvertPanel`` it uses for a normal directory and submits conversions through
the existing ``POST /api/jobs/batch``.

The one piece of state here is the metadata re-pin queue.  See
``services.db.RommRepin`` for why it exists and why it is a table.
"""

from __future__ import annotations

import asyncio
import os
import stat as stat_module
import time
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from config import settings
from models import DirectoryListing, FileEntry, RommDiagnostics
from pydantic import BaseModel, Field
from routes.files import (
    detect_directory_outputs,
    detect_file_outputs,
    verifiable_tools,
)
from services.file_hasher import compute_file_sha1_sync
from services.job_manager import job_manager
from services.lock_manager import lock_manager
from services.output_conflicts import (
    QUEUE,
    collapse_to_winners,
    resolve_destination,
)
from services.preferences_store import preferences_store
from services.romm import (
    DAT_SAFE_OUTPUT_EXTS,
    RommClient,
    RommError,
    RommNotConfigured,
    romm_client,
)
from services.romm import (
    auto as romm_auto,
)
from services.romm import (
    repin as romm_repin,
)
from services.romm import (
    settings as romm_settings,
)
from services.subprocess_runner import (
    SIZE_RATIOS,
    bounded_path_check,
    run_detached,
)
from services.tools import registry
from services.workload_limiter import workload_limiter
from utils.path_utils import is_within_configured_volumes

router = APIRouter()
logger = get_logger("romm")

# A pending row whose output never appeared is a conversion that was planned but
# never ran (the batch submit failed, or the job was cancelled). Left alone it
# would be retried forever, so retire it once it is clearly not coming. How long
# to wait is the operator's call (`repin_abandon_days`, editable in the app);
# this is only the fallback for a settings store that has not been primed.
_ABANDON_AFTER_DAYS_DEFAULT = 7

# Ceiling on rows *settled* in one pass. Each hit costs a full-file SHA-1, so a
# large backlog is drained over several calls rather than in one request that
# runs for an hour.
_MAX_SETTLE_PER_CALL = 25

# Where the settle pass records how far it got, so progress survives the call.
_SETTLE_CURSOR_KEY = "romm.repin_cursor"
# Ceiling on rows *examined*. A row that is merely waiting costs one cheap
# probe, but a backlog of thousands of them would still walk forever looking
# for work, so the pass gives up scanning long before that.
_MAX_EXAMINE_PER_CALL = 250
# Rows fetched per query while walking the pending set.
_SETTLE_PAGE = 50
# Wall-clock budget for one settle pass. The per-row work is a full-file
# SHA-1, so on a slow array a full page of them would hold the request open
# for the better part of an hour. Stopping early costs nothing: every row not
# reached is still pending, and the cursor means the next call resumes there.
_MAX_SETTLE_SECONDS = 120
# How often the background settler looks for rows the bounded pass left
# behind. Minutes, not seconds: RomM has to rescan the output before there is
# anything to match, which is never immediate.
_SETTLE_TICK_SECONDS = 300
# Floor on how long one hash may take, and the throughput assumed above it.
# A multi-gigabyte image on a slow array is legitimately slow, so the budget
# scales with the file rather than failing big outputs on a fixed timeout.
# How long one platform's catalog scan may take. It stats every ROM, so the
# budget grows with the count; the ceiling is what a listing may cost before
# the answer is "this mount is not healthy" rather than "this library is big".
_CATALOG_SCAN_BASE_S = 30
_CATALOG_SCAN_PER_ROM_S = 0.2
_CATALOG_SCAN_CEILING_S = 300
_HASH_TIMEOUT_FLOOR_S = 300
_HASH_MIN_BYTES_PER_S = 2 * 1024 * 1024
# How long the identity comparison may spend resolving the two library roots
# before the save proceeds on their spelling alone. See `_identity_moved`.
_IDENTITY_PROBE_SECONDS = 20
# The re-pin plan resolves every submitted path (a realpath plus a stat of each
# configured volume). Scaled like the catalog scan, for the same reason: a
# hundred-file batch legitimately takes longer than a one-file one.
_PLAN_BASE_S = 20
_PLAN_PER_PATH_S = 0.5
_PLAN_CEILING_S = 300


async def _probe(func, *args, default):
    """One blocking filesystem answer, bounded, with the safe answer on timeout.

    Every stat, resolve and containment check in this module goes through here
    rather than `run_in_threadpool`. The pool is shared and has no deadline, so
    one unresponsive NFS/SMB/rclone mount both hangs the request and writes off
    a worker per retry -- and several of these run while `_settle_lock` or the
    sweep pause is held, which puts every other RomM operation behind them.
    `bounded_path_check` gives each call its own disposable thread and a hard
    bound.

    *default* is what "the volume did not answer" means at that call site, and
    it is always the conservative reading: not inside the volumes, still
    locked, still queued -- never "go ahead".
    """
    try:
        return await bounded_path_check(func, *args)
    except (asyncio.TimeoutError, OSError):
        logger.warning(
            "romm: %s did not answer in time; treating it as %r "
            "(a volume is not answering)",
            getattr(func, "__name__", func), default,
        )
        return default


def _plan_deadline(paths: int) -> float:
    """Seconds the plan may spend resolving *paths* submitted paths."""
    return min(_PLAN_BASE_S + _PLAN_PER_PATH_S * max(0, paths), _PLAN_CEILING_S)

# One settle pass at a time, process-wide. The view settles on load, so two
# tabs (or a reload mid-pass) otherwise walk the same cursor and pay the same
# multi-gigabyte hashes twice for one row's worth of progress.
_settle_lock = asyncio.Lock()


def _require_configured() -> None:
    if not romm_client.configured:
        raise HTTPException(
            status_code=503,
            detail="RomM is not configured. Set ROMM_URL (and ROMM_TOKEN).",
        )
    if not romm_client.library_root:
        raise HTTPException(
            status_code=503,
            detail=(
                "ROMM_LIBRARY_ROOT is not set. Point it at the local mount of "
                "RomM's library directory."
            ),
        )


def _library_root_usable(path: str) -> bool:
    """Is *path* a directory AND inside a configured volume?

    One predicate, so the whole answer goes through a single bounded probe.
    ``is_within_configured_volumes`` resolves the candidate *and* stats every
    configured volume, so on an unresponsive network mount it blocks exactly
    like the ``isdir`` beside it -- bounding only the first half left the event
    loop just as exposed.
    """
    return os.path.isdir(path) and is_within_configured_volumes(path)


def _safe_error(exc: RommError) -> str:
    """A message describing *exc* without echoing the exception text.

    The exception message can quote RomM's response body and OS-level detail.
    That belongs in our log, not in an HTTP response — so the wording here is
    derived from ``RommError.status``, a value we set ourselves. It is also
    more useful than the raw text: it names the thing to go fix.
    """
    status = getattr(exc, "status", None)
    if status in (401, 403):
        return "RomM rejected the API token. Check ROMM_TOKEN and its scopes."
    if status == 404:
        return "RomM returned 404. Check that ROMM_URL points at the API root."
    if status is not None:
        return f"RomM returned HTTP {status}."
    return "Could not reach RomM. Check ROMM_URL and that the instance is up."


def _romm_call(exc: RommError, *, context: str) -> HTTPException:
    """Translate a client error into the right status.

    502, not 500: the failure is upstream, and a caller retrying against a
    healthy RomM would succeed.
    """
    logger.warning("romm: %s failed: %s", context, exc)
    if isinstance(exc, RommNotConfigured):
        return HTTPException(
            status_code=503,
            detail="RomM is not configured. Set ROMM_URL (and ROMM_TOKEN).",
        )
    return HTTPException(status_code=502, detail=_safe_error(exc))


# ----------------------------------------------------------------------
# status / catalog
# ----------------------------------------------------------------------


@router.get("/romm/status")
async def romm_status() -> dict:
    """Report whether RomM is reachable, and what the UI needs to know.

    Never raises for an unreachable RomM — the view uses this to *render* the
    problem, so an error here is data, not an exception.
    """
    configured = romm_client.configured
    library_root = romm_client.library_root
    result: dict = {
        "configured": configured,
        "library_root": library_root,
        "library_root_mounted": False,
        "token_set": bool(romm_settings.token()),
        # SSOT for which targets keep RomM's DAT match. The frontend renders the
        # warning from this rather than carrying its own copy of the list.
        "dat_safe_output_exts": sorted(DAT_SAFE_OUTPUT_EXTS),
        # Expected output/input size ratio per mode, so the view can estimate
        # what converting would save. Served rather than duplicated in JS:
        # SIZE_RATIOS is the same table the progress estimator reads.
        "size_ratios": dict(SIZE_RATIOS),
        # Which modes may run unattended. Served rather than re-derived in JS
        # because the answer needs the tool's own `output_path` -- a mode that
        # turns its product into a *differently named* file never terminates
        # under automation, while one that maps it onto itself is refused by
        # the queue's same-path guard and settles. See
        # `registry.mode_is_automatable`.
        "automatable_modes": sorted(
            spec.mode for spec in registry.mode_specs()
            if registry.mode_is_automatable(spec.mode)
        ),
        "connected": False,
        "version": None,
        "error": None,
    }
    if library_root:
        # A dead NFS/SMB mount blocks in uninterruptible I/O, so this stat goes
        # through the shared bounded probe like every other path check.
        try:
            is_dir = bool(await bounded_path_check(os.path.isdir, library_root))
            # Existing on disk is not enough. Every ROM path is gated by
            # `is_within_configured_volumes`, so a library root outside all of
            # them yields a connection that looks healthy and a catalog where
            # every single row is silently dropped. Report it here instead.
            inside = is_dir and bool(
                await bounded_path_check(_library_root_usable, library_root),
            )
            result["library_root_mounted"] = inside
            if is_dir and not inside:
                result["error"] = (
                    "The library path exists but is outside the configured "
                    "Compressatorium volumes, so none of its ROMs can be read. "
                    "Mount it under one of them."
                )
        except (asyncio.TimeoutError, OSError):
            # A dead mount is exactly what this probe exists to survive: report
            # it as status rather than failing the request. The reason goes to
            # the log; the response carries a fixed message, since OSError text
            # can include host paths and errno detail.
            logger.warning(
                "romm: could not stat ROMM_LIBRARY_ROOT", exc_info=True,
            )
            result["error"] = (
                "Could not read ROMM_LIBRARY_ROOT. Check the mount is present "
                "and readable."
            )
    if configured:
        try:
            heartbeat = await run_in_threadpool(romm_client.heartbeat)
            result["connected"] = True
            result["version"] = (heartbeat.get("VERSION") or heartbeat.get("version"))
        except RommError as exc:
            logger.warning("romm: heartbeat failed: %s", exc)
            result["error"] = _safe_error(exc)
    result["pending_repins"] = await run_in_threadpool(romm_repin.count_pending)
    return result


@router.get("/romm/platforms")
async def romm_platforms() -> list[dict]:
    """List RomM's platforms, slimmed to what the picker needs."""
    _require_configured()
    try:
        platforms = await run_in_threadpool(romm_client.platforms)
    except RommError as exc:
        raise _romm_call(exc, context="listing platforms") from exc
    # Only tools that can actually run here. A Switch install without
    # prod.keys reports NSZ unavailable everywhere else (GET /api/tools hides
    # it in the sidebar), so offering an NSZ rule in the automation editor
    # would queue jobs that fail at runtime, on a schedule.
    # Through the registry's bounded seam, not `is_ready()` directly: NSZ's
    # walks every configured volume for `prod.keys` with no deadline, so one
    # unresponsive mount left the Library and the automation editor loading
    # forever and stranded a shared-pool worker per attempt.
    all_tool_ids = await registry.ready_tool_ids()
    out = [
        {
            "id": p.get("id"),
            "name": p.get("display_name") or p.get("name") or p.get("slug"),
            "slug": p.get("slug"),
            "rom_count": p.get("rom_count"),
            # The tools this platform does not rule out, narrowed by the same
            # registry call the conversion path uses -- so the automation
            # editor offers PS2 chdman/maxcso and GameCube dolphin/nkit
            # instead of every mode for every platform.
            "tool_ids": registry.narrow_to_platform(all_tool_ids, p.get("slug")),
            # Narrowed per MODE as well, because a tool can be right for the
            # platform while one of its modes is not: the chain tool has a
            # GameCube mode and a PS2 mode, so it survives on both and the
            # tool list alone would offer each mode on the wrong system.
            "mode_ids": registry.modes_for_platform(p.get("slug")),
        }
        for p in platforms
        if p.get("id") is not None
    ]
    # Deterministic order: RomM's own ordering is not guaranteed stable.
    out.sort(key=lambda p: ((p["name"] or "").lower(), p["id"]))
    return out


@router.get("/romm/roms", response_model=DirectoryListing)
async def romm_roms(
    platform_id: int = Query(..., description="RomM platform id"),
) -> DirectoryListing:
    """List a RomM platform's ROMs as a directory listing.

    Returning the shape ``GET /files`` returns is the whole trick: the browser
    reuses ``FileList`` / ``FileRow`` / ``ConvertPanel`` unchanged, and the
    convert submit is the existing batch endpoint.

    Records are dropped when they do not resolve to a real file inside a
    configured volume — a RomM library that is not mounted here, a ROM whose
    file is missing, or a path trying to escape the library root. Offering a
    row we cannot convert would only produce a job that fails.
    """
    _require_configured()
    try:
        roms = await run_in_threadpool(romm_client.roms, platform_id)
    except RommError as exc:
        raise _romm_call(exc, context="listing roms") from exc

    # RomM stamps the platform on every ROM record, so the slug that narrows
    # the tool list costs no extra request. `platform_slug` is the canonical
    # one (`platform_fs_slug` is the on-disk folder, which the operator may
    # have renamed and which therefore does not identify the system).
    slug = next((r.get("platform_slug") for r in roms if r.get("platform_slug")), None)
    # `run_detached`, not the shared threadpool: this stats every ROM in the
    # platform, and on the NFS/SMB/rclone mounts this integration exists for a
    # mount that stops answering blocks the whole scan in uninterruptible I/O.
    # In a shared pool that strands one worker per load, and a few tabs
    # reloading a large platform would starve unrelated API offloads -- the
    # same reasoning AGENTS.md applies to whole-file reads.
    # `run_detached` is unbounded by design -- it says so -- so the deadline
    # has to come from here, or the 504 below is unreachable and the request
    # (and the spinner behind it) waits on a dead mount forever. The budget
    # scales with the catalog: a stat per ROM is fast on a healthy mount and
    # a 5000-ROM platform must not fail on a limit sized for 50.
    budget = min(
        _CATALOG_SCAN_CEILING_S,
        _CATALOG_SCAN_BASE_S + _CATALOG_SCAN_PER_ROM_S * len(roms),
    )
    try:
        entries, diag = await asyncio.wait_for(
            run_detached(_build_entries, roms, slug), budget,
        )
    except (asyncio.TimeoutError, OSError) as exc:
        logger.warning("romm: listing platform %s timed out", platform_id)
        raise HTTPException(
            status_code=504,
            detail=(
                "The RomM library did not respond while reading this platform. "
                "Check that its mount is healthy."
            ),
        ) from exc
    return DirectoryListing(
        volume="RomM", path=f"romm://platform/{platform_id}", entries=entries,
        diagnostics=diag,
    )


def _build_entries(
    roms: list[dict], platform_slug: str | None,
) -> tuple[list[FileEntry], RommDiagnostics]:
    """Turn RomM records into FileEntry rows. Runs off the event loop."""
    entries: list[FileEntry] = []
    _MAX_SAMPLES = 5
    diag = RommDiagnostics(
        total=len(roms),
        library_root=romm_client.library_root,
        volumes=list(settings.volumes),
    )
    for rom in roms:
        path = romm_client.local_path(rom)
        if not path:
            diag.no_path += 1
            if len(diag.sample_paths) < _MAX_SAMPLES:
                rel = (rom.get("full_path") or "").strip()
                if not rel:
                    fs_p = (rom.get("fs_path") or "").strip()
                    fs_n = (rom.get("fs_name") or "").strip()
                    rel = f"{fs_p}/{fs_n}" if fs_p else fs_n
                if rel:
                    diag.sample_paths.append(rel)
            continue
        if not is_within_configured_volumes(path):
            diag.outside_volumes += 1
            if len(diag.sample_paths) < _MAX_SAMPLES:
                diag.sample_paths.append(path)
            continue
        try:
            stat = os.stat(path)
        except FileNotFoundError:
            diag.not_found += 1
            if len(diag.sample_paths) < _MAX_SAMPLES:
                diag.sample_paths.append(path)
            continue
        except OSError:
            diag.not_readable += 1
            if len(diag.sample_paths) < _MAX_SAMPLES:
                diag.sample_paths.append(path)
            continue
        is_dir = stat_module.S_ISDIR(stat.st_mode)
        if not is_dir and not stat_module.S_ISREG(stat.st_mode):
            diag.not_regular += 1
            continue
        if is_dir:
            # A RomM record can resolve to a directory: a decrypted PS3 game
            # folder is the input unit makeps3iso takes, and the platform
            # advertises that tool. Dropping it made `folder_to_iso` reachable
            # from automation but not by hand, for the same records. The
            # frontend already renders selectable directory rows, so this is
            # the same registry-driven detection an ordinary listing uses.
            convertible_by, outputs = detect_directory_outputs(path)
            verifiable: list[str] = []
        else:
            convertible_by, outputs, _ = detect_file_outputs(path)
            verifiable = verifiable_tools(path)
        # This is what the platform buys us. Extensions alone cannot tell a
        # GameCube .iso from a PS2 .iso, so an unnarrowed list offers chdman and
        # maxcso on a GameCube disc -- conversions that are wrong for the
        # system. The registry decides; see ToolRegistry.narrow_to_platform.
        convertible_by = registry.narrow_to_platform(convertible_by, platform_slug)
        name = os.path.basename(path)
        entries.append(
            FileEntry(
                # `name` stays the real filename: it is the filename contract
                # every reused row action depends on -- Rename pre-fills from it,
                # and seeding that with "Super Mario Bros." would rename the file
                # without its extension. RomM's curated title rides along in
                # `display_name`, which the row shows and nothing acts on.
                name=name,
                display_name=rom.get("name") or None,
                path=path,
                type="directory" if is_dir else "file",
                # A folder's own inode size is meaningless as a ROM size, and
                # RomM already knows what the set weighs.
                size=(rom.get("fs_size_bytes") or None) if is_dir else stat.st_size,
                extension=None if is_dir else (
                    os.path.splitext(name)[1].lower() or None
                ),
                convertible_by=convertible_by,
                outputs=outputs,
                verifiable_by=verifiable,
            ),
        )
    # Deterministic ordering, independent of RomM's paging. Sorts on the title
    # the user actually reads, falling back to the filename.
    entries.sort(key=lambda e: ((e.display_name or e.name).lower(), e.path))
    diag.resolved = len(entries)
    dropped = diag.total - diag.resolved
    if dropped:
        logger.info(
            "romm: %d/%d ROMs dropped (no_path=%d outside_volumes=%d "
            "not_found=%d not_readable=%d not_regular=%d)",
            dropped, diag.total, diag.no_path, diag.outside_volumes,
            diag.not_found, diag.not_readable, diag.not_regular,
        )
    return entries, diag


# ----------------------------------------------------------------------
# metadata re-pin
# ----------------------------------------------------------------------


class RepinPlanRequest(BaseModel):
    """Rows to record before a batch of RomM conversions is submitted."""

    paths: list[str]
    mode: str
    # The platform the browser is showing. Turns the catalog lookup into one
    # request instead of a scan across every platform in the instance.
    platform_id: int | None = None
    # {source: destination} when the caller already knows where each job is
    # writing -- used to re-record after a batch resolved a different path
    # than planning did. Given, it replaces the duplicate-policy resolution
    # below, since the queue has already had the final say.
    output_paths: dict[str, str] | None = None
    output_dir: str | None = None
    # The policy the batch will apply, so the row is recorded against the path
    # the conversion actually writes rather than the one it would have.
    duplicate_action: str = "skip"


@router.post("/romm/repin/plan")
async def romm_repin_plan(payload: RepinPlanRequest) -> dict:
    """Record the metadata to carry across a conversion, before it runs.

    Called by the UI immediately before ``POST /api/jobs/batch``.  It must
    happen first: the provider ids are read from the RomM record for the
    *source* file, and once the source is converted (and possibly deleted) that
    record is what goes stale.

    Modes whose output keeps RomM's DAT identity (CHD, ZIP/7z) record nothing —
    RomM re-matches them by itself, so a row would be pure noise.
    """
    _require_configured()
    try:
        spec = registry.spec(payload.mode)
        tool = registry.for_mode(payload.mode)
    except KeyError as exc:
        raise HTTPException(
            status_code=400, detail=f"Unknown mode: {payload.mode}",
        ) from exc

    if not romm_settings.effective().get("repin_enabled"):
        # Re-pinning is switched off, so the automation path records nothing;
        # the manual path must not quietly disagree with it.
        return {"recorded": 0, "skipped": len(payload.paths), "reason": "disabled"}

    if not romm_repin.mode_needs_repin(spec.output_ext):
        return {"recorded": 0, "skipped": len(payload.paths), "reason": "dat_safe"}

    # Which RomM this plan is about. Re-checked before the rows are written:
    # this route holds neither `_settle_lock` nor the sweep pause, so an
    # identity change in another tab can retire the existing rows and install a
    # new instance while this one is still resolving -- after which recording
    # would insert the *old* instance's provider ids into a fresh row the
    # cleanup never saw, and the conversion would be re-pinned as a game from a
    # library it does not belong to.
    generation = romm_settings.identity_generation()
    if romm_settings.cleanup_owed():
        # The identity moved and the rows belonging to the old one are still in
        # the table. Recording now adds a row that the pending cleanup will
        # retire the moment it lands -- or, worse, one it has already passed
        # over. Refusing costs a manual re-match; recording costs a ROM its
        # identity.
        raise HTTPException(
            status_code=409,
            detail="The RomM connection changed and its cleanup has not "
                   "finished; submit again in a moment to save the metadata",
        )

    # One catalog read for the whole batch, indexed by local path, instead of a
    # by-hash lookup per file.
    try:
        # Bounded: this resolves every submitted path against the platform's
        # catalog, and the resolve half is a stat chain per ROM. The deadline
        # scales with the batch, like the path resolution below it.
        platform_roms = await asyncio.wait_for(
            run_detached(
                romm_repin.roms_by_local_path,
                payload.paths,
                payload.platform_id,
            ),
            _plan_deadline(len(payload.paths)),
        )
    except RommError as exc:
        raise _romm_call(exc, context="reading the catalog") from exc
    except (asyncio.TimeoutError, OSError) as exc:
        # The library stopped answering mid-resolve. Saying so beats recording
        # a partial set and reporting it as complete.
        logger.warning("romm: resolving the submitted paths timed out")
        raise HTTPException(
            status_code=504,
            detail="The library did not respond while matching these files",
        ) from exc

    recorded = 0
    skipped = 0
    # Source -> destination for every row written, returned so the caller can
    # retire the ones its batch does not end up queueing. Keyed by source
    # because that is what the caller submits and what the created jobs report
    # back; the destination is what a row is identified by.
    recorded_paths: dict[str, str] = {}
    # Source -> the id of the row written for it. The handle the caller passes
    # back to /romm/repin/cancel: the destination alone would retire whatever
    # row holds that path by then, which after a re-plan is somebody else's.
    recorded_ids: dict[str, int] = {}
    # Filled by the resolve loop below, then collapsed per destination before
    # anything is written: recording is per source, the queue is per output.
    planned: dict[str, str] = {}
    roms_by_path: dict[str, tuple[dict, dict]] = {}

    # Every key present before the resolution starts, so a bound that expires
    # part-way keeps the paths that did answer instead of discarding the lot.
    resolved_keys: dict[str, str | None] = dict.fromkeys(payload.paths)

    def _resolve_batch(paths: list[str]) -> None:
        """Volume check and canonical key for each path, in one worker hop.

        Both stat every path component, and the volume check stats each
        configured volume on top -- per submitted path. Done inline in an
        `async def`, one unresponsive NFS/SMB/rclone mount blocks the event
        loop for the whole request instead of one worker, which is the failure
        every other probe in this file already avoids.
        """
        for candidate in paths:
            resolved_keys[candidate] = (
                os.path.realpath(candidate)
                if is_within_configured_volumes(candidate) else None
            )

    # Detached and bounded, not pooled: this cannot be cancelled once it is
    # inside a dead mount, only abandoned, and abandoning a shared worker per
    # submit would eventually starve every unrelated offload. A path left
    # unresolved is skipped -- the same answer as one outside the volumes,
    # which is what an unreachable path effectively is.
    try:
        await asyncio.wait_for(
            run_detached(_resolve_batch, payload.paths),
            _plan_deadline(len(payload.paths)),
        )
    except (asyncio.TimeoutError, OSError):
        logger.warning(
            "romm: resolving %d submitted path(s) did not finish in time; "
            "recording only the ones that answered (a volume is not answering)",
            len(payload.paths),
        )
    for path in payload.paths:
        # Same canonical key the index was built with (see roms_by_local_path).
        key = resolved_keys.get(path)
        if key is None:
            skipped += 1
            continue
        rom = platform_roms.get(key)
        if not rom:
            skipped += 1
            continue
        ids = romm_repin.metadata_ids(rom)
        if not ids:
            # Unidentified in RomM already — nothing to carry across.
            skipped += 1
            continue
        # Resolve the destination exactly as the batch will. Recording against
        # the *base* path was wrong twice over: under Rename the batch writes
        # `Game_1.rvz`, so the row would re-pin the file already sitting at the
        # base path and leave the new one unidentified; under Skip the source is
        # never queued at all, leaving a pending row for a conversion that never
        # runs. Same helper, same answer.
        #
        # Unless the caller states the destination. Resolution is a prediction,
        # and between predicting and queueing another job can take the path the
        # rename policy picked -- the batch then writes somewhere else and the
        # row would point at whatever landed on the predicted path. A caller
        # that has the queue's answer passes it here instead.
        if payload.output_paths and path in payload.output_paths:
            destination, decision = payload.output_paths[path], QUEUE
        else:
            # Bounded: this looks for an existing output and, under Rename,
            # walks suffixes until one is free -- both filesystem work. An
            # unreadable answer skips the row, which is what `decision != QUEUE`
            # already means everywhere below.
            destination, decision = await _probe(
                resolve_destination,
                tool, path, payload.mode, payload.output_dir,
                payload.duplicate_action,
                default=(None, None),
            )
        if decision != QUEUE or not destination:
            skipped += 1
            continue
        # Off the loop and bounded, for the same reason as the batch resolve
        # above. Unreachable reads as "outside", which skips the row.
        if not await _probe(
            is_within_configured_volumes, destination, default=False,
        ):
            skipped += 1
            continue
        # A live job already writing here owns the pending row for it, and
        # recording would supersede that row. This submit is then going to be
        # rejected anyway -- the queue refuses a destination another job holds
        # -- and cancelling ours would retire the replacement too, leaving the
        # conversion that really is queued with no metadata to come back to.
        #
        # Not when the caller passed `output_paths`: that is the re-record after
        # a batch was accepted, so the job holding this destination is the very
        # one being recorded for.
        if not payload.output_paths and await _probe(
            _destination_has_pending_job, destination, default=True,
        ):
            skipped += 1
            continue
        planned[path] = destination
        roms_by_path[path] = (rom, ids)

    # Two selected ROMs can resolve to one destination -- repeated `disc.iso`
    # names aimed at a single output folder, a `.cue` beside its `.bin`. The
    # batch route collapses those into ONE job and keeps the highest-priority
    # source; recording per source in submission order instead left the row
    # holding whichever came last, because each `record()` supersedes the
    # previous one for that path. The conversion that ran could then be
    # re-pinned with the identity of the ROM the queue skipped. Same rule as
    # the batch, from the same helper.
    winners = collapse_to_winners(planned)
    skipped += len(planned) - len(winners)
    ready: list[tuple] = []
    if romm_settings.identity_generation() != generation:
        # The catalog these ids came from belongs to an instance nobody is
        # pointed at any more. Nothing is recorded, and the caller is told --
        # a batch converted without a snapshot needs a manual re-match, which
        # is recoverable; a row holding another library's ids is not.
        logger.warning(
            "romm: the RomM identity changed while planning; recording nothing",
        )
        raise HTTPException(
            status_code=409,
            detail="The RomM connection changed while this batch was being "
                   "planned; submit it again to save the metadata",
        )
    for path, destination in winners.items():
        rom, ids = roms_by_path[path]
        # Taken here, bounded, rather than inside `record()`:
        #
        # * a re-record for a path the queue chose runs AFTER the jobs were
        #   accepted, and on an idle queue a fast conversion can finish in
        #   between -- stating the destination then saves the *finished* output
        #   as the "before" picture, the settler sees nothing change, and the
        #   row is abandoned. The caller only supplies `output_paths` for the
        #   redirect case, where the queue picked a free name, so the state
        #   before this conversion is "nothing there": "".
        # * and a stat that cannot be read must not become `""`. Under
        #   `overwrite` that would tell the settler the destination changed the
        #   moment it reads it again, sending it off to hash the file this
        #   conversion has not replaced yet. No row is better than a row
        #   pointing at the wrong artifact, so the source is skipped instead.
        if payload.output_paths:
            pre = ""
        else:
            pre = await _probe(
                romm_repin.path_fingerprint, destination, default=None,
            )
            if pre is None:
                skipped += 1
                continue
        ready.append((path, rom, destination, ids, pre))

    # The write phase, and the only part that has to be atomic with respect to
    # an identity change.
    #
    # Checking the generation before the loop was a check-before-write: a save
    # landing in between could install the new URL and finish its cleanup
    # while these rows were still going in, so they survived it -- holding the
    # OLD instance's provider ids, against the new library, invisible to the
    # pass that was supposed to retire them. `_run_identity_cleanup` runs under
    # `_settle_lock`, so holding it here is what makes "the identity has not
    # moved" true for the duration of the inserts rather than at one instant
    # before them.
    #
    # Only the inserts are inside it. The fingerprint probes above are
    # filesystem reads with their own bound, and holding a lock the settler and
    # the settings route both need across a dead mount, once per destination,
    # would trade this race for a much longer stall.
    async with _settle_lock:
        if (
            romm_settings.identity_generation() != generation
            or romm_settings.cleanup_owed()
        ):
            logger.warning(
                "romm: the RomM identity changed while planning; recording nothing",
            )
            raise HTTPException(
                status_code=409,
                detail="The RomM connection changed while this batch was being "
                       "planned; submit it again to save the metadata",
            )
        for path, rom, destination, ids, pre in ready:
            row_id = await run_in_threadpool(
                romm_repin.record, rom, destination, ids, payload.mode, pre,
                path,
            )
            if row_id:
                recorded += 1
                recorded_paths[path] = destination
                recorded_ids[path] = row_id
            else:
                skipped += 1
    return {
        "recorded": recorded,
        "skipped": skipped,
        "recorded_paths": recorded_paths,
        "recorded_ids": recorded_ids,
        # The authoritative backlog, for the badge. Recording is not addition:
        # `record()` supersedes the pending row for a destination rather than
        # stacking one, so a re-submitted or retried batch records rows without
        # changing this figure. One COUNT, against the count the caller would
        # otherwise have to infer.
        "pending": await run_in_threadpool(romm_repin.count_pending),
    }


class RepinCancelRequest(BaseModel):
    """The rows a caller recorded and is no longer going to write.

    Row ids, not output paths: ``/romm/repin/plan`` returns them for exactly
    this, and a path names whichever row holds it *now* -- which, once another
    client has re-planned the same destination, is that client's live row.
    """

    ids: list[int]


@router.post("/romm/repin/cancel")
async def romm_repin_cancel(payload: RepinCancelRequest) -> dict:
    """Retire re-pin rows for conversions that were planned but never submitted.

    ``/romm/repin/plan`` runs before ``POST /api/jobs/batch``, because the
    provider ids are only readable while the source is still the file RomM
    knows about. When the batch is then rejected -- backpressure, a validation
    error -- those rows describe work that will never happen. They are already
    safe (a row whose output never changes is never settled, and ages out on
    its own), but leaving them pending for a week would report a backlog that
    does not exist.
    """
    _require_configured()
    # ...unless the job writing there is this row's own conversion. Two tabs
    # can plan the same destination before either submits, and the second
    # `record()` supersedes the first row. If the *first* tab then wins job
    # creation, the second is told the destination is taken and tidies up after
    # itself -- retiring the row the accepted conversion now depends on, since
    # its own was already superseded.
    #
    # "Something is writing there" is not enough to keep a row, though, and
    # keeping one on those grounds was its own bug: when the two tabs planned
    # *different sources* onto one destination, the surviving row held the
    # second source's ids while the queued job converts the first, so the
    # settle pass hashed one game's output and stamped the other game's
    # identity onto it. The row has to name its source, and the job writing
    # there has to be converting that source.
    rows = await run_in_threadpool(romm_repin.outputs_for, payload.ids)
    keep = set()
    for row_id, (output_path, source_path) in rows.items():
        if await _probe(
            _destination_job_matches_source, output_path, source_path, default=True,
        ):
            keep.add(row_id)
    if keep:
        logger.info(
            "romm: keeping %d re-pin row(s) a queued job is writing to", len(keep),
        )
    cancelled = await run_in_threadpool(
        romm_repin.cancel, [i for i in payload.ids if i not in keep],
    )
    # The backlog *after* the retirement, from the same source `/romm/repin/plan`
    # and `/romm/status` report. The badge is set from a count, never adjusted by
    # one: `record()` supersedes the pending row for a destination rather than
    # stacking one, so neither what was recorded nor what was cancelled is a
    # delta anyone can apply. Returning it here is what lets the submit path
    # show the reconciled number instead of the one it measured before the
    # rows that never became jobs were retired.
    return {"cancelled": cancelled, "pending": await run_in_threadpool(
        romm_repin.count_pending,
    )}


class _Outcome(str, Enum):
    """What one re-pin row's pass concluded.

    Named rather than inlined so the five endings read as a set: the loop maps
    each to a counter and only ``UPSTREAM_ERROR`` stops the pass.
    """

    WAITING = "waiting"          # output not there yet, or still being written
    ABANDONED = "abandoned"      # aged out; the output never appeared
    REPINNED = "repinned"        # metadata re-applied
    FAILED = "failed"            # local failure; skip this row, keep going
    UPSTREAM_ERROR = "upstream"  # RomM is unwell; stop the whole pass


def _destination_has_pending_job(output_path: str) -> bool:
    """Whether a queued or running job is going to write *output_path*.

    ``check_file_status`` only reports a lock once a job starts, so it cannot
    see a conversion that is merely queued -- exactly the window in which an
    overwrite job leaves the previous artifact in place.
    """
    try:
        target = str(Path(output_path).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return False
    for _job_id, paths in job_manager.get_active_job_candidates():
        for candidate in paths:
            try:
                if str(Path(candidate).expanduser().resolve(strict=False)) == target:
                    return True
            except (OSError, RuntimeError):
                continue
    return False


def _destination_job_matches_source(
    output_path: str, source_path: str | None,
) -> bool:
    """Is the job writing *output_path* converting *source_path*?

    The stronger form of :func:`_destination_has_pending_job`, and the one a
    decision about a row's *ownership* needs. Both questions have a use: "is
    anything writing here" is right for "may I treat what is on disk as
    settled", while "is the conversion here the one this row describes" is the
    only thing that justifies keeping a row whose ids belong to one source when
    another source may be producing the file.

    A row with no ``source_path`` cannot answer, and answers **no**: it was
    written before the column existed, so keeping it would restore exactly the
    behaviour this replaced. Retiring it costs a manual re-match; keeping it
    can cost a game its identity.
    """
    if not source_path:
        return False
    try:
        target = str(Path(output_path).expanduser().resolve(strict=False))
        wanted = str(Path(source_path).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return False
    for _job_id, paths in job_manager.get_active_job_candidates():
        resolved = set()
        for candidate in paths:
            try:
                resolved.add(
                    str(Path(candidate).expanduser().resolve(strict=False)),
                )
            except (OSError, RuntimeError):
                continue
        # `get_active_job_candidates` reports a job's input *and* output paths
        # together, so one job carrying both is the job this row describes.
        if target in resolved and wanted in resolved:
            return True
    return False


async def _settle_one_repin(row: tuple, deadline: float | None = None) -> _Outcome:
    """Drive one pending re-pin row as far as it can go this pass.

    Extracted from the loop so the loop owns only paging and budgets: the
    exists/lock/hash/match/update/settle chain is a single state machine and
    reads as one.
    """
    (output_path, sha1, _rom_id, ids, created_at, row_id, pre_fingerprint,
     mode) = row

    try:
        exists = await bounded_path_check(os.path.isfile, output_path)
    except (asyncio.TimeoutError, OSError):
        # Unresponsive volume: treat as "not yet", never as abandoned.
        return _Outcome.WAITING
    if exists and pre_fingerprint:
        # Something was already here when the row was recorded -- an overwrite
        # conversion. Existence therefore proves nothing: until the file
        # actually changes, what is here is the artifact the conversion was
        # going to replace, and hashing it would stamp this ROM's provider ids
        # onto whatever RomM identifies the *old* file as. That happens for
        # real whenever a batch is planned and then never submitted.
        try:
            # Bounded like the `isfile` probe above it, and for the same
            # reason: a mount can stop answering between the two, and a stat
            # with no deadline holds `_settle_lock` (and a pooled worker) for
            # as long as the mount stays dead -- which the pass budget cannot
            # interrupt, because it is only checked between rows.
            current = await bounded_path_check(
                romm_repin.path_fingerprint, output_path,
            )
        except (asyncio.TimeoutError, OSError):
            current = None
        if not current:
            # No answer is not "the file changed". Reading it that way would
            # send a row whose conversion never ran on to hash whatever is at
            # the destination -- the artifact the overwrite was going to
            # replace -- and stamp this ROM's ids onto it. A mount that is not
            # answering means "not yet"; the next pass asks again.
            return _Outcome.WAITING
        if current == pre_fingerprint:
            exists = False
    if not exists:
        # A cached digest outlives the path it was taken from. Operators do
        # move and rename outputs after a conversion, and abandoning the row
        # there would throw away metadata RomM can still be handed -- it
        # matches on the hash, not on where the file sits. So a row that has
        # been hashed keeps going even when nothing is at the recorded path.
        if not sha1:
            # Nothing is retired while a job still means to write this path. A
            # queued conversion that has waited out a long backlog has not
            # failed, and -- the case that made this check have to come first
            # -- numbered parts at the destination may be the *previous* run's,
            # with the queued retry (splitting switched off) about to replace
            # them with a single matchable ISO. Reading those old parts as this
            # attempt's final output retired the row for a conversion that then
            # succeeded.
            # Unreachable reads as "a job still means to write here", which
            # waits. Retiring a row on a guess is the irreversible direction.
            if await _probe(
                _destination_has_pending_job, output_path, default=True,
            ):
                return _Outcome.WAITING
            # A split build produced numbered parts and no bare output. The
            # conversion did run -- but RomM matches a ROM on one file's hash,
            # and a set of parts has no single digest to join on, so this row
            # can never settle. Say that now rather than waiting out the
            # abandon period and then reporting "output never appeared".
            if await _probe(
                romm_repin.produced_companions, output_path, mode, default=[],
            ):
                await run_in_threadpool(
                    romm_repin.settle, row_id, "abandoned",
                    "Output was split into parts; RomM matches on a single "
                    "file's hash, so re-pin this ROM by hand",
                )
                return _Outcome.ABANDONED
            if not _is_stale(created_at):
                return _Outcome.WAITING
            await run_in_threadpool(
                romm_repin.settle, row_id, "abandoned", "Output never appeared",
            )
            return _Outcome.ABANDONED
        return await _match_and_settle(
            row_id, sha1, ids, output_path, created_at,
        )

    # Never hash a file a converter is still writing. An output becomes a
    # regular file the moment the tool creates it, so without this the pass
    # could cache the SHA-1 of a partial file -- and because the hash is
    # cached, every later attempt would reuse that wrong digest and the ROM
    # could never be matched again. A locked source means the job is still
    # running, which is simply "not yet".
    # Unreachable reads as "still being written", which waits rather than
    # hashing something that may be partial.
    _, locked = await _probe(
        lock_manager.check_file_status, output_path, default=(False, True),
    )
    if locked:
        return _Outcome.WAITING

    # A *queued* job holds no lock yet, so under the overwrite policy the file
    # sitting at this destination is still the OLD artifact. Hashing it now
    # would cache the wrong digest -- and because the hash is cached, the ROM
    # could never be matched once the real output replaced it.
    if await _probe(_destination_has_pending_job, output_path, default=True):
        return _Outcome.WAITING

    try:
        if not sha1:
            sha1 = await _hash_output(output_path, deadline)
            if sha1 is None:
                # Storage stopped answering. Nothing is wrong with the row --
                # leave it pending and let a later pass try again.
                return _Outcome.WAITING
            # Cache it: a row may be retried many times before RomM scans.
            await run_in_threadpool(romm_repin.store_sha1, row_id, sha1)
    except OSError as exc:
        logger.warning("romm: could not hash %s: %s", output_path, exc)
        return _Outcome.FAILED

    return await _match_and_settle(row_id, sha1, ids, output_path, created_at)


async def _hash_output(output_path: str, deadline: float | None = None) -> str | None:
    """SHA-1 of *output_path*, or None if the read did not finish in time.

    Heavy disk work, so it takes the same lane the DAT matcher uses and a
    re-pin pass cannot compete with a running conversion for the array.

    ``run_detached``, not the shared threadpool: this reads the whole file,
    and on a mount that stops answering mid-read the thread cannot be
    cancelled. AGENTS.md is explicit that such a read must never take a pooled
    worker -- repeated attempts would strand one each time and starve
    unrelated API work. Detaching is also what makes the timeout safe to
    apply: abandoning the read costs a thread that was never shared.

    The budget scales with the file. A fixed timeout either fails a legitimate
    50 GB hash or lets a hung mount hold the pass for hours; assuming a floor
    throughput does neither.
    """
    # Bounded, like every other stat that touches this path: on a mount that
    # has stopped answering, `getsize` blocks the event loop indefinitely --
    # and it runs *before* the timeout it is being used to compute, so the
    # timeout could never have protected this half.
    try:
        size = await bounded_path_check(os.path.getsize, output_path) or 0
    except (asyncio.TimeoutError, OSError):
        size = 0
    timeout = max(_HASH_TIMEOUT_FLOOR_S, size / _HASH_MIN_BYTES_PER_S)
    if deadline is not None:
        # A caller with a deadline (the HTTP pass) gets the smaller of the
        # two. Both halves are bounded by it: waiting for the heavy-IO lane
        # behind a running conversion is exactly as good at holding the
        # request open as the hash itself, and it had no bound at all.
        timeout = min(timeout, max(0.0, deadline - time.monotonic()))
        if timeout <= 0:
            return None
    try:
        async with await asyncio.wait_for(
            workload_limiter.acquire("match"), timeout,
        ):
            if deadline is not None:
                # Charged against the same budget, not granted a second one:
                # waiting most of the pass behind a running conversion and then
                # hashing for the full timeout would take twice as long as the
                # budget promises.
                timeout = max(0.0, deadline - time.monotonic())
                if timeout <= 0:
                    return None
            return await asyncio.wait_for(
                run_detached(compute_file_sha1_sync, output_path), timeout,
            )
    except asyncio.TimeoutError:
        logger.warning(
            "romm: hashing %s exceeded %.0fs; leaving the row pending",
            output_path, timeout,
        )
        return None


async def _match_and_settle(
    row_id: int, sha1: str, ids: dict, output_path: str, created_at: str,
) -> _Outcome:
    """Ask RomM which ROM this digest is, and stamp the ids onto it."""
    try:
        match = await run_in_threadpool(romm_client.rom_by_sha1, sha1)
        if not match:
            # RomM may simply not have rescanned yet -- the normal case. But it
            # may also never index this file at all (scanning disabled for the
            # extension, a folder it does not watch), and this branch is
            # reached *after* the digest is cached, so the age check in the
            # no-digest path above can never retire it. The row would then sit
            # in the badge forever, costing a by-hash request every pass, while
            # the settings screen promises it is given up on after
            # `repin_abandon_days`. Honour that promise here too.
            if _is_stale(created_at):
                await run_in_threadpool(
                    romm_repin.settle, row_id, "abandoned",
                    "RomM never matched this output; re-pin it by hand",
                )
                return _Outcome.ABANDONED
            return _Outcome.WAITING

        # Claim the row before the outbound write, in one conditional update.
        # Hashing the output above can take minutes, and a conversion
        # re-planned in that window supersedes this row -- pushing these ids to
        # RomM afterwards would stamp the previous generation's identity onto
        # the file the new conversion just produced. Reading the state and then
        # writing left exactly that window open; claiming closes it, because
        # whoever flips the row to `settling` is the only writer, and a re-plan
        # arriving after that gets its own row instead of silently replacing
        # one already being applied.
        if not await run_in_threadpool(romm_repin.claim, row_id):
            logger.info(
                "romm: re-pin row %s was superseded while hashing; skipping", row_id,
            )
            return _Outcome.FAILED

        try:
            await run_in_threadpool(
                romm_client.update_rom_metadata, match["id"], ids,
            )
        except BaseException:
            # Nothing was written, so the claim must not outlive the attempt --
            # otherwise the row waits out `_CLAIM_STALE_SECONDS` before anyone
            # retries it. Includes cancellation: the request going away is the
            # commonest way to get here.
            await run_in_threadpool(romm_repin.release, row_id)
            raise
        # Count only what actually settled: losing the row between the claim
        # and here must not inflate the reported total.
        if await run_in_threadpool(
            romm_repin.settle, row_id, "done", None, match["id"],
        ):
            return _Outcome.REPINNED
        return _Outcome.FAILED
    except RommError as exc:
        # Upstream trouble: leave the row pending and stop the pass rather than
        # burning the rest of the backlog against a sick RomM.
        logger.warning("romm: re-pin failed for %s: %s", output_path, exc)
        return _Outcome.UPSTREAM_ERROR


@router.post("/romm/repin")
async def settle_romm_repins() -> dict:
    """Re-attach metadata to converted ROMs RomM has since rescanned.

    Idempotent by construction: a settled row is never revisited, and a row
    whose output RomM has not scanned yet simply stays pending for the next
    call.  Safe to invoke on every view load.

    This owns paging and budgets only; :func:`_settle_one_repin` owns what
    happens to a row.
    """
    _require_configured()
    # Refuse rather than queue behind a pass already running: this endpoint is
    # called on view load, so waiting would leave a second tab holding a
    # request open for the length of someone else's backlog, and then do the
    # whole walk again for rows the first pass had already settled.
    if _settle_lock.locked():
        return {
            "repinned": 0, "waiting": 0, "abandoned": 0, "failed": 0,
            "busy": True,
            "pending": await run_in_threadpool(romm_repin.count_pending),
        }
    async with _settle_lock:
        return await _settle_pass()


async def _settle_pass(*, bounded: bool = True) -> dict:
    """One pass over the pending rows. Always called with ``_settle_lock``.

    ``bounded`` is what separates the two callers. A request-driven pass keeps
    every row inside the pass budget -- the hash and the wait for the heavy-IO
    lane included -- so the browser never holds a request open for the length
    of a multi-gigabyte SHA-1, and stops starting rows once the budget is out.
    The background pass has nobody waiting, so it gives each row the full
    size-scaled timeout: that is what keeps a genuinely large output settling
    at all rather than being cut short on every attempt forever.
    """
    counts = {outcome: 0 for outcome in _Outcome}
    deadline = time.monotonic() + _MAX_SETTLE_SECONDS

    # Walk forward through the pending rows rather than re-reading the oldest
    # page each time: rows that are merely waiting stay pending, and without a
    # cursor a prefix of them would occupy every page and starve the rows
    # behind. The cursor is persisted, so progress survives the call -- reset
    # to zero each time, a prefix of waiters would fill the examine budget on
    # every invocation and the rows behind them could never be reached.
    cursor = await _load_settle_cursor()
    settled = 0
    examined = 0
    rows = await run_in_threadpool(romm_repin.pending_rows, _SETTLE_PAGE, after_id=cursor)
    if not rows and cursor:
        # Past the tail: wrap so rows added before the cursor are seen again.
        cursor = 0
        rows = await run_in_threadpool(
            romm_repin.pending_rows, _SETTLE_PAGE, after_id=cursor,
        )

    while rows and settled < _MAX_SETTLE_PER_CALL and examined < _MAX_EXAMINE_PER_CALL:
        if time.monotonic() >= deadline:
            # Out of time, not out of work. The cursor is stored below, so the
            # next call picks up exactly here.
            break
        row = rows.pop(0)
        examined += 1
        cursor = row[5]
        if not rows:
            rows = await run_in_threadpool(
                romm_repin.pending_rows, _SETTLE_PAGE, after_id=cursor,
            )

        outcome = await _settle_one_repin(row, deadline if bounded else None)
        counts[outcome] += 1
        if outcome is _Outcome.UPSTREAM_ERROR:
            break
        # `settled` counts only work that actually finished, so a page full of
        # waiters still advances to the next page.
        if outcome in (_Outcome.ABANDONED, _Outcome.REPINNED, _Outcome.FAILED):
            settled += 1

    # `rows` empty means the pass reached the tail, so the next call starts over
    # from the oldest; otherwise it picks up exactly where this one stopped.
    await _store_settle_cursor(0 if not rows else cursor)

    return {
        "repinned": counts[_Outcome.REPINNED],
        "waiting": counts[_Outcome.WAITING],
        "abandoned": counts[_Outcome.ABANDONED],
        "failed": counts[_Outcome.FAILED] + counts[_Outcome.UPSTREAM_ERROR],
        "busy": False,
        "pending": await run_in_threadpool(romm_repin.count_pending),
    }


async def settle_forever() -> None:
    """Background re-pin settler. Ticks every few minutes.

    The request-driven pass is deliberately impatient: it will not hold a
    browser request open hashing a 40 GB output, so it skips whatever does not
    fit its budget. Something has to finish those rows, and it cannot be the
    next request either -- it would cut the same row short every time and the
    metadata would never be restored. This loop is that something: no client
    is waiting on it, so each row gets its full size-scaled timeout.

    Shares ``_settle_lock`` with the endpoint, so a tick never runs beside a
    request-driven pass, and skips entirely when one is in progress.
    """
    logger.info("romm: re-pin settler started")
    while True:
        try:
            await asyncio.sleep(_SETTLE_TICK_SECONDS)
            if not romm_settings.effective().get("repin_enabled", True):
                continue
            # Rows recorded against an instance nobody is pointed at any more
            # are still in the table until the cleanup lands. Settling one
            # stamps the previous library's ids onto whatever the current one
            # matches -- so pause, and retry the cleanup while paused.
            if await identity_cleanup_is_owed():
                continue
            if _settle_lock.locked():
                continue
            pending = await run_in_threadpool(romm_repin.count_pending)
            if not pending:
                continue
            async with _settle_lock:
                result = await _settle_pass(bounded=False)
            if result["repinned"]:
                logger.info(
                    "romm: re-matched %s ROM(s) in the background", result["repinned"],
                )
        except Exception:
            # One bad pass must never end the loop; the next tick retries.
            # CancelledError derives from BaseException, so shutdown still
            # stops it.
            logger.warning("romm: background re-pin pass failed", exc_info=True)


async def _load_settle_cursor() -> int:
    """The row id the last settle pass stopped after, or 0."""
    stored = await preferences_store.get(_SETTLE_CURSOR_KEY)
    try:
        return max(0, int(stored))
    except (TypeError, ValueError):
        return 0


async def _store_settle_cursor(value: int) -> None:
    await preferences_store.put(_SETTLE_CURSOR_KEY, int(value))


def _is_stale(created_at: str) -> bool:
    """Whether a pending re-pin row has waited past the configured period.

    Reads `repin_abandon_days` rather than a constant: a slow array or a long
    conversion backlog is exactly when an operator raises it, and retiring a row
    early means that ROM's metadata can never be restored.
    """
    if not created_at:
        return False
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    days = romm_settings.effective().get(
        "repin_abandon_days", _ABANDON_AFTER_DAYS_DEFAULT,
    )
    try:
        cutoff = timedelta(days=int(days))
    except (TypeError, ValueError):
        cutoff = timedelta(days=_ABANDON_AFTER_DAYS_DEFAULT)
    return datetime.now(timezone.utc) - created > cutoff


# ----------------------------------------------------------------------
# DB helpers (sync; always called through run_in_threadpool)
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# settings, rules, and unattended conversion
# ----------------------------------------------------------------------
#
# Everything about the integration is editable here rather than only through
# the environment, so an operator can connect RomM, tune per-platform policy
# and watch a preview without redeploying the container.


class RommSettingsPatch(BaseModel):
    """A partial settings update. Omitted fields keep their current value."""

    url: str | None = None
    token: str | None = None
    # Explicitly drop the stored token. A blank `token` means "unchanged".
    clear_token: bool | None = None
    library_root: str | None = None
    auto_convert: bool | None = None
    auto_convert_interval_minutes: int | None = None
    auto_convert_max_per_run: int | None = None
    repin_enabled: bool | None = None
    repin_on_load: bool | None = None
    repin_abandon_days: int | None = None
    verify_after_convert: bool | None = None
    delete_source_after_verify: bool | None = None


@router.get("/romm/settings")
async def get_romm_settings() -> dict:
    """Current settings. The token is never returned, only whether one is set."""
    return romm_settings.public()


@router.put("/romm/settings")
async def put_romm_settings(patch: RommSettingsPatch) -> dict:
    """Save settings and apply them immediately (no restart).

    Pointing the integration at a different RomM instance, or at a different
    library on disk, invalidates the automation's conversion history: it is
    keyed by RomM's own platform and ROM ids, and a different RomM database
    reuses those numbers for entirely different games. Carried over, an
    `overwrite` or `rename` rule would treat unrelated ROMs in the new library
    as already done and skip them for good. The rules themselves are kept --
    they are the operator's configuration, and a mistyped URL must not delete
    it -- but what those rules believe they have produced starts over.

    The orchestration lives here rather than in ``romm_settings``: the service
    layer's dependency runs the other way (``romm_auto`` reads settings), and
    a route is where cross-service consequences belong.
    """
    # The save itself is inside the pause, not just the cleanup after it. A
    # sweep resolves each ROM's local path lazily from the *current* library
    # root, so swapping that root mid-sweep makes it queue whatever unrelated
    # files sit at the same relative paths in the new library -- and with
    # delete-on-verify on, delete them.
    # `_settle_lock` as well as the sweep pause: a settle pass in flight has
    # already read the old configuration and is about to ask *some* RomM to
    # match a digest, so the identity must not change underneath it.
    async with _settle_lock, romm_auto.paused():
        submitted = patch.model_dump(exclude_unset=True)
        before = romm_settings.effective()
        # Decided, and acted on, BEFORE the new identity is persisted. The
        # other order looked natural and was not recoverable: the save lands,
        # the cleanup then fails (a locked database, a full volume), the
        # request returns an error -- and a retry now compares the new values
        # with themselves, concludes nothing moved, and leaves the previous
        # instance's history and provider-id rows live against the new one,
        # permanently. This way a failed cleanup persists nothing, so the retry
        # sees the same move and does the same work.
        #
        # Compare what the client and the path mapper actually use, not the
        # spelling: `RommClient` strips a trailing slash from the URL and the
        # library root is resolved with `realpath`, so saving
        # `http://romm:8080/` over `http://romm:8080` -- or the same directory
        # via a symlink -- pointed at the identical instance while looking like
        # a move, and threw away the conversion history and every pending
        # metadata snapshot for nothing.
        proposed = {
            **before,
            # `None` means "leave it alone" to `save()`, so it has to mean the
            # same here. Reading an explicit null as an empty identity made
            # `{"url": null}` look like a move to nowhere, and the request then
            # cleared the conversion history and irreversibly retired every
            # pending snapshot while changing no connection setting at all.
            **{
                k: v for k, v in submitted.items()
                if k in _IDENTITY_FIELDS and v is not None
            },
        }
        changed = await _identity_moved(before, proposed)
        # ...or a previous attempt already installed an identity and then
        # failed to clean up after it. Replaying only at startup was not
        # enough: retrying the same request finds the new identity already
        # cached, computes `changed == False`, and returns success while the
        # marker still says the old instance's history and re-pin rows are
        # live against the new one.
        owed = changed or romm_settings.cleanup_owed()
        # The new identity and the promise to clean up after it go into one
        # row, so neither half can commit alone: whichever is interrupted, the
        # marker survives and `_run_identity_cleanup` replays the other. Both
        # halves of the cleanup are idempotent, so replaying costs nothing.
        values = await romm_settings.save(submitted, cleanup_pending=changed)
        cleared = await _run_identity_cleanup() if owed else 0
        if cleared:
            logger.info(
                "romm: RomM instance or library changed; cleared the conversion "
                "history for %s platform(s)", cleared,
            )
    return romm_settings.public(values)


async def _run_identity_cleanup() -> int:
    """Drop everything the previous RomM instance or library owned.

    Two records, one reason: both name things that only mean something against
    the instance and root they were written for.

    The pending re-pin rows are the sharper case -- each holds provider ids
    read from the *old* instance and a destination under the *old* library
    root, so the next settle pass would hash whatever now sits at that path and
    hand the previous instance's ids to whichever ROM the new instance matches.
    One library's identity written onto another's game. They cannot be
    re-homed, so they are retired.

    Idempotent, and called from two places: the settings route, and startup
    (for the change whose cleanup a crash interrupted). The marker that says it
    is owed is cleared only once this returns.

    The caller must hold `_settle_lock` and the sweep pause.
    """
    cleared = await romm_auto.forget_converted_locked()
    retired = await run_in_threadpool(
        romm_repin.retire_all_pending,
        "The RomM instance or library path changed before this could "
        "be re-matched; re-pin this ROM by hand",
    )
    if retired:
        logger.info(
            "romm: retired %s pending re-pin row(s) belonging to the "
            "previous RomM instance", retired,
        )
    await romm_settings.clear_cleanup_owed()
    return cleared


async def identity_cleanup_is_owed() -> bool:
    """Is a previous instance's data still live against the current identity?

    The marker is written in the same row as the new URL and library root, so
    while it is set the two records that belong to the *old* instance -- the
    conversion history and every pending re-pin row -- have outlived it. Acting
    on either is the failure the marker exists to prevent: the settler would
    hand the old library's provider ids to whatever the new one matches, and a
    sweep would read the old library's history and decide a ROM it has never
    seen is already converted.

    Startup replays the cleanup, but that attempt can fail transiently -- a
    locked SQLite file is enough -- and the app deliberately starts anyway. So
    the workers ask *here*, every tick, rather than trusting that one attempt:
    each tick retries the cleanup and, until it succeeds, does nothing else.
    Self-healing, and it keeps the app available, which not starting them would
    not.
    """
    if not romm_settings.cleanup_owed():
        return False
    # Retry it, under the lock it requires, before reporting the pause. The
    # caller must not already hold `_settle_lock`.
    try:
        # Same order as every other site takes these two (`_settle_lock`
        # first, then the sweep pause). Taking them the other way round here
        # would be the one place that could deadlock against the settings
        # route.
        async with _settle_lock, romm_auto.paused():
            if romm_settings.cleanup_owed():
                await _run_identity_cleanup()
    except Exception:
        logger.exception("romm: retrying the pending identity cleanup failed")
    return romm_settings.cleanup_owed()


async def replay_identity_cleanup() -> None:
    """Finish an identity change whose cleanup did not complete.

    Called at startup. A crash between installing a new URL/library root and
    clearing the records that belonged to the old one leaves the marker set;
    without this the stale rows stay live against the new instance forever.
    """
    if not romm_settings.cleanup_owed():
        return
    logger.info("romm: finishing the cleanup an identity change left owed")
    async with _settle_lock, romm_auto.paused():
        await _run_identity_cleanup()


# The settings that decide *which* RomM and *which* library these records
# belong to. A change to either invalidates the conversion history and every
# pending re-pin row; nothing else does.
_IDENTITY_FIELDS = ("url", "library_root")


def _identity_changed(before: dict, after: dict, *, resolve: bool = True) -> bool:
    """Whether the RomM instance or library these settings name really moved.

    Blocking when *resolve* (``realpath`` stats every component), so it runs
    off the loop. With ``resolve=False`` it compares normalised spellings and
    touches nothing -- the answer when the volume will not say; see
    :func:`_identity_moved`.
    """
    def _url(value: object) -> str:
        return str(value or "").strip().rstrip("/")

    def _root(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        expanded = os.path.expanduser(text)
        if not resolve:
            return os.path.normpath(os.path.abspath(expanded))
        try:
            return os.path.realpath(expanded)
        except (OSError, ValueError):
            return text

    return (
        _url(before.get("url")) != _url(after.get("url"))
        or _root(before.get("library_root")) != _root(after.get("library_root"))
    )


async def _identity_moved(before: dict, after: dict) -> bool:
    """:func:`_identity_changed`, under a bound and off the event loop.

    The comparison resolves both library roots, and the *old* one is the mount
    that has just gone unresponsive as often as not -- that is why the operator
    is here changing it. On a pooled worker with no deadline, `realpath` then
    blocks forever while this request holds `_settle_lock` and the sweep pause:
    re-pin passes, sweeps, rule edits and every later settings save wait behind
    the one request that would have fixed the mount.

    Falling back to the spelling comparison is the safe degradation. Two
    spellings of one directory then read as a *move*, which retires the pending
    rows and clears the converted history -- work redone, nothing lost. The
    opposite mistake writes one library's provider ids onto another library's
    game.
    """
    try:
        return bool(await asyncio.wait_for(
            run_detached(_identity_changed, before, after),
            _IDENTITY_PROBE_SECONDS,
        ))
    except (asyncio.TimeoutError, OSError):
        logger.warning(
            "romm: resolving the library root did not finish in %ss; comparing "
            "the paths as written (a volume is not answering)",
            _IDENTITY_PROBE_SECONDS,
        )
        return _identity_changed(before, after, resolve=False)


@router.post("/romm/settings/test")
async def test_romm_connection(patch: RommSettingsPatch | None = None) -> dict:
    """Probe a RomM instance and report what works, without saving anything.

    Deliberately granular: "it doesn't work" is useless when there are three
    independent things to get right. This separates *reachable* (the public
    heartbeat), *authorised* (a scoped call the token must pass), and *mounted*
    (the library visible to this container), so the answer names the one that
    is wrong.
    """
    override = patch.model_dump(exclude_unset=True) if patch else {}
    saved = romm_settings.effective()

    def _field(name: str) -> str:
        """The value to probe with: an explicitly submitted one wins, blank included.

        `exclude_unset` distinguishes "not in this form" from "cleared by the
        operator". Falling back on falsiness reported a working connection for a
        form that, once saved, would switch the integration off.
        """
        if name in override:
            return str(override[name] or "")
        return str(saved.get(name) or "")

    url = _field("url").rstrip("/")
    token = override.get("token")
    if not token:
        # `clear_token` means "test with no token at all" -- falling back to
        # the saved one would report success for a configuration the operator
        # is about to save as unauthenticated.
        token = "" if override.get("clear_token") else romm_settings.token()
    library_root = _field("library_root")

    result: dict = {
        "reachable": False,
        "authorized": False,
        "library_root_mounted": False,
        "version": None,
        "platform_count": None,
        "error": None,
    }
    if not url:
        result["error"] = "Set the RomM URL first."
        return result

    probe = RommClient(base_url=url, token=token or "")
    try:
        heartbeat = await run_in_threadpool(probe.heartbeat)
        result["reachable"] = True
        result["version"] = heartbeat.get("VERSION") or heartbeat.get("version")
    except RommError as exc:
        logger.warning("romm: connection test failed: %s", exc)
        result["error"] = _safe_error(exc)
        return result

    try:
        platforms = await run_in_threadpool(probe.platforms)
        result["authorized"] = True
        result["platform_count"] = len(platforms)
    except RommError as exc:
        logger.warning("romm: connection test auth failed: %s", exc)
        result["error"] = _safe_error(exc)

    outside_volumes = False
    if library_root:
        try:
            is_dir = bool(await bounded_path_check(os.path.isdir, library_root))
            result["library_root_mounted"] = is_dir and bool(
                await bounded_path_check(_library_root_usable, library_root),
            )
            outside_volumes = is_dir and not result["library_root_mounted"]
        except (asyncio.TimeoutError, OSError):
            result["library_root_mounted"] = False
    if result["authorized"] and not result["library_root_mounted"]:
        # Two different fixes, so two different messages: mounting a folder and
        # moving it inside a configured volume are not the same job.
        result["error"] = result["error"] or (
            "Connected to RomM, but its library folder is outside the configured "
            "Compressatorium volumes, so no ROM in it can be read."
            if outside_volumes else
            "Connected to RomM, but its library folder is not mounted here. "
            "Check the library path and the volume mount."
        )
    return result


@router.get("/romm/rules")
async def get_romm_rules() -> dict:
    """Per-platform automation rules, plus the schema the editor renders from.

    ``defaults`` and ``options`` ship with the rules so the UI never carries a
    second copy of what a valid rule looks like.
    """
    rules = await romm_auto.get_rules()
    state = await romm_auto.get_state()
    return {
        "rules": rules,
        "state": state,
        "defaults": romm_auto.default_rule(),
        "options": {
            "orders": list(romm_auto.ORDERS),
            "duplicate_actions": list(romm_auto.DUPLICATE_ACTIONS),
            "days": list(romm_auto.ALL_DAYS),
        },
    }


@router.put("/romm/rules")
async def put_romm_rules(payload: dict) -> dict:
    """Replace the rule set. Rules naming an unknown mode are dropped."""
    rules = await romm_auto.set_rules(payload.get("rules", payload))
    return {"rules": rules}


@router.post("/romm/rules/forget-converted")
async def forget_romm_converted(payload: dict | None = None) -> dict:
    """Forget which ROMs the automation has already converted.

    ``overwrite`` and ``rename`` rules remember what they have produced, since
    neither policy can tell "already done" from the destination alone. Restore
    a library from backup, or move outputs aside by hand, and that memory is
    the only thing standing between the operator and a rerun -- so it is
    clearable, per platform or wholesale.
    """
    payload = payload or {}
    ids = payload.get("platform_ids")
    # `is not None` for the same reason the sweep uses it: an explicit empty
    # list selects no platforms, and reading it as "no filter" cleared the
    # conversion history for *every* one of them. This is the escape hatch
    # after a restore -- wiping it wholesale on an empty selection makes the
    # next sweep reconvert the entire library.
    cleared = await romm_auto.forget_converted(
        [str(p) for p in ids] if ids is not None else None,
    )
    return {"cleared": cleared, "state": await romm_auto.get_state()}


class SweepRequest(BaseModel):
    """What a manual Preview / Run now may ask for.

    Typed rather than a bare dict because both fields reach the sweep: an
    unvalidated `limit` became the overall cap verbatim, so a number larger
    than `auto_convert_max_per_run` queued past the configured safety limit and
    a string failed the comparison inside the sweep with a 500. Here it can
    only ever narrow: the sweep clamps it down to the configured maximum.
    """

    platform_ids: list[int] | None = None
    limit: int | None = Field(default=None, ge=1)


@router.post("/romm/auto-convert/preview")
async def preview_auto_convert(payload: SweepRequest | None = None) -> dict:
    """What a sweep would queue right now, without queueing anything.

    Ignores each rule's schedule so the operator can see the effect of a rule
    they just wrote instead of waiting for its next window.
    """
    _require_configured()
    payload = payload or SweepRequest()
    return await romm_auto.sweep(
        platform_ids=payload.platform_ids,
        ignore_schedule=True,
        dry_run=True,
        overall_limit=payload.limit,
    )


@router.post("/romm/auto-convert/run")
async def run_auto_convert(payload: SweepRequest | None = None) -> dict:
    """Run a sweep now, queueing real jobs.

    Manual runs ignore the schedule -- pressing the button means "now" -- but
    still honour every other part of each rule (filters, caps, ordering).
    """
    _require_configured()
    payload = payload or SweepRequest()
    return await romm_auto.sweep(
        platform_ids=payload.platform_ids,
        ignore_schedule=True,
        dry_run=False,
        overall_limit=payload.limit,
    )
