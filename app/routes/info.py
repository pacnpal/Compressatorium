import asyncio
import contextlib
import json
from logging_setup import get_logger
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from config import settings
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from models import (
    BulkVerifyRequest,
    CHDInfo,
    ConversionMode,
    CsoInfo,
    DolphinDiscInfo,
    JwudInfo,
    MetadataBatchRequest,
    NkitInfo,
    NszInfo,
    RomzInfo,
    Z3DSInfo,
)
from services.archive import ARCHIVE_EXTENSIONS
from services.chd_metadata_store import chd_metadata_store
from services.chdman import chdman_service
from services.disc_id import (
    DiscIdStorageAbandoned,
    ensure_disc_id_embedded as disc_id_ensure_embedded,
    extract_from_chd as disc_id_extract_from_chd,
)
from services.dolphin_tool import (
    DOLPHIN_CONVERTIBLE_EXTENSIONS,
    dolphin_tool_service,
)
from services.jwudtool import (
    JWUD_COMPRESS_EXTENSIONS,
    JWUD_DECOMPRESS_EXTENSIONS,
    jwudtool_service,
)
from services.tools import registry
from services.tools.base import ToolPlugin
from services.subprocess_runner import (
    StorageAbandoned,
    bounded_path_check,
    collect_abandonment,
)
from services.workload_limiter import WorkloadToken, workload_limiter
from services.job_manager import ExternalJobCancelled, job_manager
from services.maxcso import (
    MAXCSO_COMPRESS_EXTENSIONS,
    MAXCSO_DECOMPRESS_EXTENSIONS,
    maxcso_service,
)
from services.nkit2iso import (
    NKIT2ISO_CONVERTIBLE_EXTENSIONS,
    NkitHeaderError,
    nkit2iso_service,
)
from services.nsz import (
    NSZ_COMPRESS_EXTENSIONS,
    NSZ_DECOMPRESS_EXTENSIONS,
    nsz_service,
)
from services.romz import (
    ROMZ_ARCHIVE_EXTENSIONS,
    ROMZ_COMPRESS_EXTENSIONS,
    romz_service,
)
from services.z3ds_compress import (
    Z3DS_CONVERTIBLE_EXTENSIONS,
    Z3DS_DECOMPRESS_EXTENSIONS,
    z3ds_compress_service,
)
from services.verification_store import verification_store
from sse_starlette.sse import EventSourceResponse
from utils.path_utils import is_within_configured_volumes, match_extension

DOLPHIN_INFO_EXTENSIONS = DOLPHIN_CONVERTIBLE_EXTENSIONS

logger = get_logger()
router = APIRouter()

# Lock for scanning to prevent concurrent scans
_scan_lock = asyncio.Lock()
_is_scanning = False


def _verification_backpressure_detail() -> str:
    in_use = workload_limiter.in_use("verify")
    limit = workload_limiter.limit("verify")
    return (
        "Verification lane is at capacity "
        f"({in_use}/{limit}). Retry later."
    )


# How many verify events may sit undelivered before progress starts being
# dropped. The producer runs independently of the socket (that is what makes the
# bound enforceable), so a peer that stays connected without reading would
# otherwise let a chatty verifier queue events for the whole of its bound --
# hours, at the default -- with nothing to stop the buffer growing.
_VERIFY_QUEUE_LIMIT = 64


async def _offer_verify_update(queue: asyncio.Queue, update: dict) -> None:
    """Enqueue a verify event without ever blocking or growing without bound.

    Progress events are a *level*, not a log: a reader that has fallen behind
    needs the latest one, not every one it missed, so under backpressure the
    newest is simply dropped. A terminal event is the opposite -- losing it
    would leave the consumer waiting on a verdict that already happened -- so it
    makes room by discarding buffered progress, which the verdict has just made
    stale anyway. Neither path awaits, because the producer must not be
    suspended by a peer that stopped reading; that is the whole reason the
    deadline lives in the producer.
    """
    if update.get("type") == "progress":
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(update)
        return
    while queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover - single producer
            break
    queue.put_nowait(update)


def _abandonment(abandoned: list) -> dict:
    """``{"abandoned": True}`` when this verify left something stuck behind.

    Two kinds of wreckage count, because both mean the same thing to a caller
    walking a list -- the storage just cost us a resource we cannot get back, so
    stop: a child that outlived SIGKILL, and a detached read whose awaiter gave
    up while it was still blocked (the only trace a verify with no child leaves).

    Both are reported into the sink opened by ``collect_abandonment()`` around
    *this* file's work, rather than read off global state, so a second request
    wedged on its own mount cannot make this batch abort files on a healthy one.
    An outer deadline is the case that needs it most: it cancels the verify
    before any terminal event exists, so there is nothing to carry a flag.
    """
    return {"abandoned": True} if abandoned else {}


async def _acquire_verify_lane_or_429() -> WorkloadToken:
    token = await workload_limiter.try_acquire("verify")
    if token is None:
        raise HTTPException(status_code=429, detail=_verification_backpressure_detail())
    return token


async def _scan_phase_dat_match(
    scan_job_id: str,
    all_paths: list[str],
    *,
    force: bool,
) -> int:
    """Prime the DAT-match cache for discovered outputs (scan Phase 3).

    Runs every discovered path (any registered format) through the shared
    per-file matcher and persists cacheable results, so RVZ/3DS/Switch
    libraries are matched by the scan just like CHDs. Already-cached paths are
    skipped unless ``force``. Non-cacheable outcomes (size-cap skips, transient
    errors) are intentionally not written, mirroring the /dat/match-batch job.
    Progress band: 65 % → 97 %. Returns the number of matched files.
    """
    # Lazy import keeps the routes modules import-order independent.
    from routes.dat import _match_single_file
    from services.dat_store import dat_store

    if not all_paths:
        return 0
    try:
        has_dats = await run_in_threadpool(dat_store.has_dats)
    except Exception as e:
        # No DAT store available (e.g. DB not initialised); nothing to prime.
        logger.debug("Phase 3: DAT store unavailable, skipping match priming: %s", e)
        has_dats = False
    if not has_dats:
        # Advance through the Phase 3 band so the job doesn't appear stuck at
        # the Phase 2 progress (and then jump straight to the flush) for
        # libraries with no DATs imported / no CHDs.
        await job_manager.update_external_job(
            scan_job_id,
            progress=97,
            message="Phase 3: no DATs imported — skipping DAT match",
        )
        return 0

    total = len(all_paths)
    logger.info("Phase 3: DAT-matching %d discovered file(s)...", total)
    await job_manager.update_external_job(
        scan_job_id,
        progress=65,
        message=f"Phase 3: DAT-matching {total} file(s)…",
    )

    # Forwarded into expensive embedded-hash hooks (e.g. dolphin-tool verify)
    # so cancelling the scan aborts the in-flight file promptly.
    cancel_event = job_manager.get_cancel_event(scan_job_id)

    # Skip paths already present in the match cache unless forced.
    cached: dict[str, dict | None] = {}
    if not force:
        cached = await run_in_threadpool(dat_store.get_matches_batch, all_paths)

    matched = 0
    for idx, path in enumerate(all_paths, start=1):
        if job_manager.is_cancelled(scan_job_id):
            raise ExternalJobCancelled()
        if not force and cached.get(path) is not None:
            if cached[path].get("matched"):
                matched += 1
        else:
            try:
                with collect_abandonment() as abandoned:
                    result = await _match_single_file(
                        path, cancel_event=cancel_event,
                    )
                # Abandonment before cancellation: a cancel is usually what
                # triggered the teardown that then could not kill the child, so
                # both are true at once and a clean CANCELLED would be the more
                # misleading report -- the process is still running.
                if abandoned:
                    # Not a fact about this file: the hash helper outlived
                    # SIGKILL and is still reading the volume the rest of the
                    # library is on, so every remaining path would strand
                    # another one (issue #268). Fail the scan here.
                    raise RuntimeError(
                        f"Phase 3: matching {os.path.basename(path)} left a "
                        f"process stuck on unresponsive storage "
                        f"({', '.join(abandoned)}); it is still running"
                    )
                # A cancellable hook (dolphin verify) may have been aborted
                # mid-file, returning a non-cacheable error. Treat that as
                # cancellation rather than deleting/keeping a stale row or
                # finishing "successfully" on the last/only file.
                if job_manager.is_cancelled(scan_job_id):
                    raise ExternalJobCancelled()
                # Don't cache size-cap skips or transient errors (same policy
                # as the /dat/match-batch job).
                if not result.get("reason") and not result.get("error"):
                    await dat_store.set_match(path, result)
                else:
                    # Non-cacheable recompute (size cap / hash unavailable):
                    # drop any stale prior row so /dat/matches/lookup doesn't
                    # keep showing an outdated match after a (forced) rescan.
                    await dat_store.delete_match(path)
                if result.get("matched"):
                    matched += 1
            except ExternalJobCancelled:
                raise
            except Exception:
                # _match_single_file turns expected file-level problems into
                # result dicts, so reaching here means an unexpected failure
                # (matcher bug, cache write/delete error). Surface it with a
                # full trace and fail the scan rather than silently "succeed"
                # with an un-primed cache.
                logger.exception("Phase 3: DAT match failed for %s", path)
                raise
        await job_manager.update_external_job(
            scan_job_id,
            progress=65 + int(32 * idx / total),
            message=f"Phase 3 [{idx}/{total}]: {os.path.basename(path)}",
        )

    logger.info("Phase 3 complete: %d/%d file(s) matched a DAT", matched, total)
    return matched


async def scan_metadata_task(
    force: bool = False,
    lane_token: WorkloadToken | None = None,
):
    """Background task to scan all volumes for library metadata.

    Discovery is registry-driven (every registered tool's output / verify
    extensions). Phase 1 refreshes the chdman info cache and Phase 2 embeds
    disc-ID tags, both CHD-only by design; Phase 3 primes the DAT-match cache
    for every discovered output (all formats).
    """
    global _is_scanning
    # Note: _is_scanning is already set to True by the trigger endpoint
    scan_start = time.monotonic()
    count = 0
    embed_count = 0
    scan_token = lane_token
    if scan_token is None:
        scan_token = await workload_limiter.acquire("metadata_scan")
    _is_scanning = True

    volumes = settings.volumes
    logger.info(
        "Metadata scan starting (force=%s) across %d volume(s): %s",
        force,
        len(volumes),
        ", ".join(volumes) if volumes else "(none)",
    )

    # Create a job entry so the scan is visible in the Jobs panel.
    scan_job = job_manager.create_external_job(
        filename="Metadata Scan",
        mode=ConversionMode.METADATA_SCAN,
        message=f"Starting scan across {len(volumes)} volume(s)\u2026",
    )
    scan_job_id = scan_job.id

    # Discovery is registry-driven: walk every extension any registered tool
    # produces or can verify (issue #131) so Dolphin/3DS/Switch outputs are
    # eligible for the scan, not just CHDs. Archive containers (.zip/.7z/.rar)
    # are excluded: they're browse-into containers handled by the archive
    # subsystem, never DAT-matchable as a whole, so the romz tool advertising
    # .7z/.zip outputs must not make the scan enumerate and hash every unrelated
    # archive under the configured volumes.
    scan_extensions = frozenset(registry.scannable_extensions()) - ARCHIVE_EXTENSIONS

    def collect_scannable_paths():
        """Collect all scannable output paths from volumes (runs in thread pool)."""
        paths = []
        seen: set[str] = set()
        for volume in volumes:
            if not os.path.exists(volume):
                logger.warning("Volume not found, skipping: %s", volume)
                continue
            for root, _, files in os.walk(volume):
                for file in files:
                    # Cheap pre-filter on the (possibly symlink) name's suffix,
                    # compared as a real suffix not a string ending so a ".ciso"
                    # isn't admitted just because it ends with ".iso".
                    if os.path.splitext(file)[1].lower() not in scan_extensions:
                        continue
                    # Resolve symlinks so discovered paths share the cache key
                    # space of the on-demand DAT-match endpoints (which realpath
                    # before caching) and the CHD metadata store, and so symlink
                    # aliases are de-duplicated.
                    real = os.path.realpath(os.path.join(root, file))
                    if real in seen:
                        continue
                    # Re-check the suffix on the *resolved* path: a symlink like
                    # ``alias.iso -> actual.ciso`` passes the name filter above
                    # but resolves to a non-scannable type the later phases
                    # shouldn't touch.
                    if os.path.splitext(real)[1].lower() not in scan_extensions:
                        continue
                    # A symlink can resolve outside the configured volumes;
                    # Phase 3 would otherwise hash/cache that target without the
                    # ACL gate the DAT endpoints apply. Drop it here.
                    if not is_within_configured_volumes(
                        real, treat_archives=False,
                    ):
                        continue
                    # Per-file veto on top of the type filter: a name a tool
                    # knows is a *slice* of an artifact rather than one itself
                    # (jwud's game_partN.wud, twelve 2 GiB pieces of one disc)
                    # can never match a DAT, so hashing it is pure waste.
                    if not registry.scannable_path(real):
                        continue
                    seen.add(real)
                    paths.append(real)
        return paths

    # Tri-state: True = success, False = failure, None = cancelled.
    scan_success: bool | None = False
    scan_error: str | None = None
    try:
        # Run blocking filesystem traversal in thread pool
        loop = asyncio.get_running_loop()
        all_paths = await loop.run_in_executor(None, collect_scannable_paths)
        total_files = len(all_paths)
        # The chdman-specific phases (info cache + disc ID) stay CHD-only; the
        # rest of the discovered outputs participate in DAT matching (Phase 3).
        chd_all_paths = [p for p in all_paths if p.lower().endswith(".chd")]
        logger.info(
            "Discovery complete: found %d output file(s) (%d CHD) across %d volume(s)",
            total_files,
            len(chd_all_paths),
            len(volumes),
        )
        await job_manager.update_external_job(
            scan_job_id,
            progress=5,
            message=f"Found {total_files} file(s) \u2014 starting metadata refresh\u2026",
        )

        # Filter stales asynchronously (CHD metadata cache is CHD-only)
        chd_paths = []
        for path in chd_all_paths:
            if force or await chd_metadata_store.is_stale(path):
                chd_paths.append(path)

        if force:
            logger.info(
                "Phase 1: Force-refreshing metadata for all %d CHD file(s)",
                len(chd_paths),
            )
        else:
            cached_count = len(chd_all_paths) - len(chd_paths)
            logger.info(
                "Phase 1: %d CHD file(s) need metadata refresh, %d already up-to-date",
                len(chd_paths),
                cached_count,
            )

        # Phase 1: Update chdman info cache for stale / forced CHDs.
        # Progress band: 5 % → 45 %.
        phase1_total = len(chd_paths)
        for idx, path in enumerate(chd_paths, start=1):
            if job_manager.is_cancelled(scan_job_id):
                raise ExternalJobCancelled()
            logger.info(
                "Phase 1 [%d/%d]: Extracting metadata from %s",
                idx,
                phase1_total,
                os.path.basename(path),
            )
            # The sink wraps the handler, not the reverse: a corrupt or
            # unreadable CHD stays isolated to its own file, but an info child
            # we could not kill has to escape it. That child is still reading
            # the volume the rest of the library is on, so continuing costs one
            # stuck process per remaining file (issue #268).
            with collect_abandonment() as abandoned:
                try:
                    info = await chdman_service.info(path)
                    record = await chd_metadata_store.set_metadata(
                        path, info, persist=False,
                    )
                    count += 1
                    logger.info(
                        "Phase 1 [%d/%d]: Metadata cached for %s",
                        idx,
                        phase1_total,
                        os.path.basename(path),
                    )
                    logger.debug(
                        "Phase 1 [%d/%d]: Metadata for %s: game_id=%r, title=%r, info=%s",
                        idx,
                        phase1_total,
                        os.path.basename(path),
                        record.get("game_id"),
                        record.get("title"),
                        info,
                    )
                except Exception as e:
                    logger.warning(
                        "Phase 1 [%d/%d]: Failed to extract metadata from %s: %s",
                        idx,
                        phase1_total,
                        path,
                        e,
                    )
            if abandoned:
                raise RuntimeError(
                    f"Phase 1: reading {os.path.basename(path)} left a process "
                    f"stuck on unresponsive storage ({', '.join(abandoned)}); "
                    "it is still running"
                )
            if phase1_total > 0:
                await job_manager.update_external_job(
                    scan_job_id,
                    progress=5 + int(40 * idx / phase1_total),
                    message=f"Phase 1 [{idx}/{phase1_total}]: {os.path.basename(path)}",
                )

        logger.info(
            "Phase 1 complete: metadata refreshed for %d/%d CHD file(s)",
            count,
            phase1_total,
        )

        # Phase 2: Retroactively embed GAME / NAME tags in any CHD that lacks
        # them.  Covers CHDs created before conversion-time tagging was added.
        # Skip CHDs where disc ID has already been checked and the file has not
        # changed since, avoids spawning a chdman subprocess on every scan.
        # CHD-only by design (disc-ID embedding is a CHDMAN feature).
        # Progress band: 45 % → 65 %.
        phase2_total = len(chd_all_paths)
        already_checked = 0
        newly_checked = 0
        logger.info(
            "Phase 2: Checking GAME/NAME disc ID tags for %d CHD file(s)...",
            phase2_total,
        )
        await job_manager.update_external_job(
            scan_job_id,
            progress=45,
            message=f"Phase 2: Checking disc ID tags for {phase2_total} CHD file(s)\u2026",
        )
        for idx2, path in enumerate(chd_all_paths, start=1):
            if job_manager.is_cancelled(scan_job_id):
                raise ExternalJobCancelled()
            # Sink outside the handler: this one only logs at *debug*, so an
            # abandoned chdman child would otherwise leave no trace at all
            # while the scan walked the rest of the library (issue #268).
            with collect_abandonment() as abandoned:
                try:
                    if await chd_metadata_store.is_disc_id_checked(path):
                        already_checked += 1
                        # Still update progress so the scan doesn't appear stuck at 45%
                        if phase2_total > 0:
                            await job_manager.update_external_job(
                                scan_job_id,
                                progress=45 + int(20 * idx2 / phase2_total),
                                message=(
                                    f"Phase 2 [{idx2}/{phase2_total}]: "
                                    f"{os.path.basename(path)} (already checked)"
                                ),
                            )
                        continue
                    logger.info("Phase 2: Scanning disc ID for %s", os.path.basename(path))
                    result = await disc_id_ensure_embedded(path, settings.chdman_path)
                    if result and result.get("game_id"):
                        await chd_metadata_store.update_disc_id_info(
                            path, result["game_id"], result.get("title"), persist=False
                        )
                    await chd_metadata_store.mark_disc_id_checked(path)
                    newly_checked += 1
                    if result:
                        embed_count += 1
                        logger.info(
                            "Phase 2: Disc ID found for %s (game_id=%r)",
                            os.path.basename(path),
                            result.get("game_id"),
                        )
                    else:
                        logger.info(
                            "Phase 2: No disc ID found for %s, file marked as checked",
                            os.path.basename(path),
                        )
                except Exception as e:
                    logger.debug("Phase 2: disc_id ensure skipped for %s: %s", path, e)
            if abandoned:
                raise RuntimeError(
                    f"Phase 2: reading {os.path.basename(path)} left a process "
                    f"stuck on unresponsive storage ({', '.join(abandoned)}); "
                    "it is still running"
                )
            if phase2_total > 0:
                await job_manager.update_external_job(
                    scan_job_id,
                    progress=45 + int(20 * idx2 / phase2_total),
                    message=f"Phase 2 [{idx2}/{phase2_total}]: {os.path.basename(path)}",
                )

        logger.info(
            "Phase 2 complete: %d already checked, %d newly checked, %d disc ID(s) found",
            already_checked,
            newly_checked,
            embed_count,
        )

        # Phase 3: Prime the DAT-match cache for every discovered output
        # (registry-driven, all formats). This is what lets non-CHD libraries
        # participate in the scan-driven DAT-matching workflow: each file goes
        # through the per-tool embedded-hash fast path (CHD header SHA1, Dolphin
        # disc SHA1, ...) and falls back to a file-level SHA1. CHD metadata and
        # disc IDs are untouched here. Progress band: 65 % → 97 %.
        await _scan_phase_dat_match(scan_job_id, all_paths, force=force)

        # Flush all accumulated changes once at the end (async, non-blocking)
        logger.info("Flushing metadata store to disk...")
        await job_manager.update_external_job(
            scan_job_id,
            progress=97,
            message="Flushing metadata store to disk\u2026",
        )
        await chd_metadata_store.flush_async()
        logger.info("Metadata store flushed.")
        scan_success = True

    except ExternalJobCancelled:
        # Clean cancellation path: partial metadata already written to the
        # store is kept (by design, see chd_metadata_store callers above).
        scan_success = None
    except Exception as e:
        logger.error("Metadata scan failed: %s", e)
        scan_error = str(e)
    finally:
        if scan_token:
            scan_token.release()
        _is_scanning = False
        elapsed = time.monotonic() - scan_start
        logger.info(
            "Metadata scan complete: %d metadata refreshed, %d disc ID(s) found, elapsed %.1fs",
            count,
            embed_count,
            elapsed,
        )
        if scan_success is None:
            final_msg = (
                f"Cancelled \u2014 {count} refreshed, {embed_count} disc ID(s) found "
                f"({elapsed:.1f}s)"
            )
            await job_manager.finish_external_job_cancelled(
                scan_job_id,
                message=final_msg,
            )
        else:
            if scan_success:
                final_msg = (
                    f"{count} refreshed, {embed_count} disc ID(s) found \u2014 {elapsed:.1f}s"
                )
            else:
                final_msg = f"Scan failed: {scan_error or 'unknown error'}"
            await job_manager.update_external_job(
                scan_job_id,
                progress=100 if scan_success else scan_job.progress,
                message=final_msg,
            )
            await job_manager.finish_external_job(
                scan_job_id,
                success=scan_success,
                error_message=scan_error,
            )


@router.get("/version")
async def get_app_version() -> dict:
    """Get the application version."""
    # Use importlib for reliable import regardless of how app is started
    import importlib
    try:
        main_module = importlib.import_module("main")
    except ModuleNotFoundError:
        main_module = importlib.import_module("app.main")
    return {
        "version": main_module.get_version(),
        "search_auto_return_to_file_list": settings.search_auto_return_to_file_list,
    }


# ============ Z3DS endpoints ============


# The verify-class files are the compressed containers (== decompress inputs).
Z3DS_VERIFY_EXTENSIONS = set(Z3DS_DECOMPRESS_EXTENSIONS)
Z3DS_INFO_EXTENSIONS = Z3DS_CONVERTIBLE_EXTENSIONS | Z3DS_VERIFY_EXTENSIONS


def _is_z3ds_info_file(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in Z3DS_INFO_EXTENSIONS


NSZ_INFO_EXTENSIONS = NSZ_COMPRESS_EXTENSIONS | NSZ_DECOMPRESS_EXTENSIONS


def _is_nsz_info_file(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in NSZ_INFO_EXTENSIONS


CSO_INFO_EXTENSIONS = MAXCSO_COMPRESS_EXTENSIONS | MAXCSO_DECOMPRESS_EXTENSIONS


def _is_cso_info_file(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in CSO_INFO_EXTENSIONS


ROMZ_INFO_EXTENSIONS = ROMZ_COMPRESS_EXTENSIONS | ROMZ_ARCHIVE_EXTENSIONS


def _is_romz_info_file(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in ROMZ_INFO_EXTENSIONS


JWUD_INFO_EXTENSIONS = JWUD_COMPRESS_EXTENSIONS | JWUD_DECOMPRESS_EXTENSIONS


def _is_jwud_info_file(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in JWUD_INFO_EXTENSIONS


# NKit sources only: this endpoint describes a *shrunk* image, and the restored
# .iso it produces is an ordinary disc image other tools already report on. The
# extensions are compound, so this uses the shared suffix match rather than
# os.path.splitext (which would only ever see the generic ".iso"/".gcz").
def _is_nkit_info_file(path: str) -> bool:
    return match_extension(path, NKIT2ISO_CONVERTIBLE_EXTENSIONS) is not None


@router.get("/info", response_model=CHDInfo)
async def get_chd_info(path: str = Query(..., description="Path to CHD file")):
    """Get information about a CHD file (cached with mtime-based invalidation)."""
    if not await run_in_threadpool(is_within_configured_volumes, path, treat_archives=False):
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes",
        )

    if not await run_in_threadpool(os.path.isfile, path):
        raise HTTPException(status_code=404, detail="File not found")

    if not path.lower().endswith(".chd"):
        raise HTTPException(status_code=400, detail="Not a CHD file")

    try:
        # Check cache first
        cached_info = None
        cached_media_type = None
        if not await chd_metadata_store.is_stale(path):
            cached_info, cached_media_type = await chd_metadata_store.get_full_info(path)

        if cached_info:
            info = cached_info
            # cached_media_type is initialised to None on line 683 and only
            # rebound by the tuple-unpack on line 685; pylint's flow analyser
            # occasionally misses the unconditional default, so silence the
            # spurious possibly-used-before-assignment warning explicitly.
            media_type = cached_media_type  # pylint: disable=possibly-used-before-assignment
        else:
            # Run chdman info and cache the result (non-blocking persist)
            info = await chdman_service.info(path)
            record = await chd_metadata_store.set_metadata(path, info, persist=False)

            # Schedule async flush in background with error handling
            async def safe_flush():
                try:
                    await chd_metadata_store.flush_async()
                except Exception as e:
                    logger.warning(f"Background metadata flush failed: {e}")

            # Use BackgroundTasks if available, but here we are in a route without it passed
            # explicitly for flush
            # We can spawn a task
            asyncio.create_task(safe_flush())
            media_type = record.get("media_type")

        # Extract game ID / title.  Prefer the cached value in the metadata
        # store (written by Phase 2 of the scan or by a prior /api/info call)
        # to avoid spawning chdman subprocesses on every request.
        cached_game_id, cached_title = await chd_metadata_store.get_disc_id_info(path)
        disc_info: dict = {}
        if cached_game_id is not None:
            disc_info = {"game_id": cached_game_id}
            if cached_title is not None:
                disc_info["title"] = cached_title
        elif not await chd_metadata_store.is_disc_id_checked(path):
            # Not yet attempted, run the extractor and cache the outcome (even
            # "nothing found") so subsequent /api/info calls skip the subprocess.
            # Caching the outcome is what makes later requests skip the
            # subprocess -- including a "nothing found" outcome. That is right
            # for a CHD the extractor actually inspected, and wrong for one it
            # never got to look at: an abandoned chdman child is a fact about the
            # storage, not the file, so marking it checked would suppress
            # extraction for good after the share recovers, until the mtime
            # changes (issue #268). Raising past the cache write below is what
            # keeps that case retryable.
            try:
                disc_info = await disc_id_extract_from_chd(path, settings.chdman_path) or {}
            except DiscIdStorageAbandoned as e:
                # Not cached (above), which on its own would make every refresh
                # retry and strand another child while the client saw a perfectly
                # ordinary response with no game ID. So say it: 503, matching
                # /dat/match, rather than presenting an abandoned read as a
                # successful partial lookup (issue #268).
                logger.error("disc_id extraction abandoned for %s: %s", path, e)
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"Reading the disc ID left a process stuck on "
                        f"unresponsive storage: {e}"
                    ),
                ) from None
            except Exception as e:
                logger.debug("disc_id extraction failed for %s: %s", path, e)
            await chd_metadata_store.update_disc_id_info(
                path, disc_info.get("game_id"), disc_info.get("title")
            )
            await chd_metadata_store.mark_disc_id_checked(path)

        game_id = disc_info.get("game_id")
        # Only surface a distinct human-readable title; skip when it equals
        # the serial (e.g. PS2/PS1 CHDs where we wrote serial as both tags).
        raw_title = disc_info.get("title")
        title = raw_title if raw_title and raw_title != game_id else None

        return CHDInfo(
            file=path,
            input_file=info.get("input_file"),
            file_version=info.get("file_version"),
            logical_size=info.get("logical_size"),
            hunk_size=info.get("hunk_size"),
            total_hunks=info.get("total_hunks"),
            unit_size=info.get("unit_size"),
            total_units=info.get("total_units"),
            compression=info.get("compression"),
            chd_size=info.get("chd_size"),
            ratio=info.get("ratio"),
            sha1=info.get("sha1"),
            data_sha1=info.get("data_sha1"),
            raw_data=info.get("raw_data", ""),
            media_type=media_type,
            game_id=game_id,
            title=title,
        )

    except HTTPException:
        # Already carries its own status (e.g. the 503s above); don't relabel it.
        raise
    except StorageAbandoned as e:
        # A metadata read that left a child running is transient, not a broken
        # CHD: 500 would invite a refresh that strands another one (issue #268).
        logger.error("CHD info abandoned for %s: %s", path, e)
        raise HTTPException(status_code=503, detail=str(e)) from None
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to read CHD info: {e!s}",
        ) from None


@router.post("/chd-metadata")
async def get_chd_metadata_batch(
    request: MetadataBatchRequest,
) -> dict:
    """Get cached metadata for multiple CHD files.

    For every requested path, an entry is returned. Invalid or non-CHD paths
    will have media_type=None, cached=False, and an error field.
    """
    result = {}

    for path in request.paths:
        # Default entry for each requested path
        entry = {"media_type": None, "cached": False}

        if not await run_in_threadpool(is_within_configured_volumes, path, treat_archives=False):
            entry["error"] = "path_outside_configured_volumes"
            result[path] = entry
            continue

        if not await run_in_threadpool(os.path.isfile, path):
            entry["error"] = "file_not_found"
            result[path] = entry
            continue

        if not path.lower().endswith(".chd"):
            entry["error"] = "not_a_chd_file"
            result[path] = entry
            continue

        # Only return cached data, don't run chdman info
        if not await chd_metadata_store.is_stale(path):
            # Optimize: use get_full_info to avoid extra lock/lookup
            _, media_type = await chd_metadata_store.get_full_info(path)
            entry["media_type"] = media_type
            entry["cached"] = True
            # no error field for successful, cached entries
        else:
            # Not cached yet - frontend can trigger individual info calls
            entry["error"] = "not_cached"

        result[path] = entry

    return result


@router.post("/chd-metadata/scan")
async def trigger_metadata_scan(
    background_tasks: BackgroundTasks,
    force: bool = Query(False, description="Rescan all CHD files, ignoring cache"),
):
    """Trigger a background scan of all volumes for missing CHD metadata."""
    global _is_scanning

    # Use lock to prevent race condition between check and set
    async with _scan_lock:
        if _is_scanning:
            return {"status": "scanning", "message": "Scan already in progress"}
        scan_token = await workload_limiter.try_acquire("metadata_scan")
        if scan_token is None:
            return {"status": "scanning", "message": "Scan already in progress"}
        _is_scanning = True

    background_tasks.add_task(scan_metadata_task, force, scan_token)
    if force:
        return {
            "status": "started",
            "message": "Forced metadata scan started in background",
        }
    return {"status": "started", "message": "Metadata scan started in background"}


@router.get("/chd-metadata/scan/status")
async def get_scan_status():
    """Get the status of the metadata scan."""
    return {"scanning": _is_scanning}


@router.get("/verified")
async def list_verified() -> dict:
    """List verified output paths."""
    await verification_store.prune_missing()
    verified = []
    for record in await verification_store.all_records():
        chd_path = record.get("chd_path")
        if chd_path and await run_in_threadpool(
            is_within_configured_volumes, chd_path, treat_archives=False
        ):
            verified.append(chd_path)
    return {"verified": verified}


# ============ Dolphin disc image endpoints ============


def _is_dolphin_file(path: str) -> bool:
    from pathlib import Path as _P
    return _P(path).suffix.lower() in DOLPHIN_INFO_EXTENSIONS


@router.get("/dolphin-info", response_model=DolphinDiscInfo)
async def get_dolphin_info(
    path: str = Query(..., description="Path to disc image"),
):
    """Get header information about a GameCube/Wii disc image."""
    if not await run_in_threadpool(
        is_within_configured_volumes, path, treat_archives=False,
    ):
        raise HTTPException(
            status_code=403,
            detail="Access denied: path outside configured volumes",
        )
    if not await run_in_threadpool(os.path.isfile, path):
        raise HTTPException(status_code=404, detail="File not found")
    if not _is_dolphin_file(path):
        raise HTTPException(
            status_code=400, detail="Not a supported disc image format",
        )

    try:
        info = await dolphin_tool_service.header(path)
        return DolphinDiscInfo(
            file=path,
            game_id=info.get("game_id"),
            # dolphin-tool outputs "Internal Name:" in its plain-text format;
            # "game_name" / "name" cover any future JSON-mode keys.
            game_name=info.get("game_name") or info.get("internal_name") or info.get("name"),
            title_id=info.get("title_id"),
            disc_number=info.get("disc_number") or info.get("disc"),
            revision=info.get("revision"),
            region=info.get("region"),
            country=info.get("country"),
            format=info.get("format"),
            # dolphin-tool outputs "Compression Method:" in plain-text mode;
            # "compression" covers any future JSON-mode key.
            compression=info.get("compression") or info.get("compression_method"),
            compression_level=info.get("compression_level"),
            block_size=info.get("block_size"),
            file_size=info.get("file_size"),
            raw_data=info.get("raw_data", ""),
        )
    except StorageAbandoned as e:
        # Transient, not a broken disc image: a 500 invites a refresh that
        # strands another child (issue #268). Ordered before the broad handler.
        logger.error("Dolphin header abandoned for %s: %s", path, e)
        raise HTTPException(status_code=503, detail=str(e)) from None
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read disc info: {e!s}",
        ) from None


@router.get("/z3ds-info", response_model=Z3DSInfo)
async def get_z3ds_info(
    path: str = Query(..., description="Path to 3DS ROM file"),
):
    """Get basic information about a Nintendo 3DS ROM file."""
    if not await run_in_threadpool(
        is_within_configured_volumes, path, treat_archives=False,
    ):
        raise HTTPException(
            status_code=403,
            detail="Access denied: path outside configured volumes",
        )
    if not await run_in_threadpool(os.path.isfile, path):
        raise HTTPException(status_code=404, detail="File not found")
    if not _is_z3ds_info_file(path):
        raise HTTPException(
            status_code=400,
            detail=(
                "Not a supported 3DS ROM format "
                "(.3ds, .cci, .cia, .cxi, .3dsx, .z3ds, .zcci, .zcia, .zcxi, .z3dsx)"
            ),
        )

    try:
        info = await run_in_threadpool(z3ds_compress_service.info, path)
        return Z3DSInfo(
            file=info["file"],
            size=info["size"],
            size_display=info["size_display"],
            format=info.get("format"),
            extension=info["extension"],
            compressed=info["compressed"],
            compression_type=info.get("compression_type"),
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read 3DS ROM info: {e!s}",
        ) from None


@router.get("/nkit-info", response_model=NkitInfo)
async def get_nkit_info(
    path: str = Query(..., description="Path to an NKit-shrunk GC/Wii image"),
):
    """Describe an NKit image: which disc it is and what it restores to.

    Read straight from the NKit/disc header (no subprocess — nkit2iso has no
    ``info`` subcommand), so it answers the questions worth asking *before*
    spending a restore: GameCube or Wii, which game, how big the output will be,
    and the CRC32 the restore will be checked against.
    """
    if not await run_in_threadpool(
        is_within_configured_volumes, path, treat_archives=False,
    ):
        raise HTTPException(
            status_code=403,
            detail="Access denied: path outside configured volumes",
        )
    if not await run_in_threadpool(os.path.isfile, path):
        raise HTTPException(status_code=404, detail="File not found")
    if not _is_nkit_info_file(path):
        raise HTTPException(
            status_code=400,
            detail="Not an NKit-shrunk GameCube/Wii image (.nkit.iso, .nkit.gcz)",
        )

    try:
        info = await run_in_threadpool(nkit2iso_service.info, path)
    except NkitHeaderError as e:
        # The name says .nkit.iso but the bytes disagree — a client error about
        # the file, not a server fault, so 422 rather than 500.
        raise HTTPException(status_code=422, detail=str(e)) from None
    except Exception as e:
        # Broad by design at the file-read boundary (OSError, struct/zlib
        # errors, ...); logged here so the 500 is traceable.
        logger.exception("Failed to read NKit info for %s", path)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read NKit info: {e!s}",
        ) from None
    return registry.get("nkit").info_model(info, path)


@router.get("/cso-info", response_model=CsoInfo)
async def get_cso_info(
    path: str = Query(..., description="Path to PSP/PS2 ISO/CSO/ZSO/DAX file"),
):
    """Get basic information about a PSP/PS2 ISO/CSO/ZSO/DAX file."""
    if not await run_in_threadpool(
        is_within_configured_volumes, path, treat_archives=False,
    ):
        raise HTTPException(
            status_code=403,
            detail="Access denied: path outside configured volumes",
        )
    if not await run_in_threadpool(os.path.isfile, path):
        raise HTTPException(status_code=404, detail="File not found")
    if not _is_cso_info_file(path):
        raise HTTPException(
            status_code=400,
            detail="Not a supported CSO format (.iso, .cso, .zso, .dax)",
        )

    try:
        info = await run_in_threadpool(maxcso_service.info, path)
        return CsoInfo(
            file=info["file"],
            size=info["size"],
            size_display=info["size_display"],
            format=info.get("format"),
            extension=info["extension"],
            compressed=info["compressed"],
            compression_type=info.get("compression_type"),
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read CSO info: {e!s}",
        ) from None


@router.get("/romz-info", response_model=RomzInfo)
async def get_romz_info(
    path: str = Query(..., description="Path to a ROM (.gb/.gbc/.gba/.nds) or .7z/.zip"),
):
    """Get basic information about a handheld ROM or its .7z/.zip archive."""
    if not await run_in_threadpool(
        is_within_configured_volumes, path, treat_archives=False,
    ):
        raise HTTPException(
            status_code=403,
            detail="Access denied: path outside configured volumes",
        )
    if not await run_in_threadpool(os.path.isfile, path):
        raise HTTPException(status_code=404, detail="File not found")
    if not _is_romz_info_file(path):
        raise HTTPException(
            status_code=400,
            detail="Not a supported ROM format (.gb, .gbc, .gba, .nds, .7z, .zip)",
        )

    try:
        info = await run_in_threadpool(romz_service.info, path)
        return RomzInfo(
            file=info["file"],
            size=info["size"],
            size_display=info["size_display"],
            format=info.get("format"),
            extension=info["extension"],
            compressed=info["compressed"],
            compression_type=info.get("compression_type"),
            contained_name=info.get("contained_name"),
            original_size=info.get("original_size"),
            ratio=info.get("ratio"),
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read ROM info: {e!s}",
        ) from None


@router.get("/jwud-info", response_model=JwudInfo)
async def get_jwud_info(
    path: str = Query(..., description="Path to a Wii U .wud or .wux image"),
):
    """Get basic information about a Wii U disc image."""
    if not await run_in_threadpool(
        is_within_configured_volumes, path, treat_archives=False,
    ):
        raise HTTPException(
            status_code=403,
            detail="Access denied: path outside configured volumes",
        )
    if not await run_in_threadpool(os.path.isfile, path):
        raise HTTPException(status_code=404, detail="File not found")
    if not _is_jwud_info_file(path):
        raise HTTPException(
            status_code=400,
            detail="Not a supported Wii U disc image format (.wud, .wux)",
        )

    try:
        info = await run_in_threadpool(jwudtool_service.info, path)
        return JwudInfo(
            file=info["file"],
            size=info["size"],
            size_display=info["size_display"],
            format=info.get("format"),
            extension=info["extension"],
            compressed=info["compressed"],
            compression_type=info.get("compression_type"),
            original_size=info.get("original_size"),
            ratio=info.get("ratio"),
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read Wii U image info: {e!s}",
        ) from None


@router.get("/tools")
async def list_tools():
    """Which tools the frontend should show.

    A tool is unavailable when its own ``is_ready()`` says its runtime
    prerequisites are missing — today that's Switch (nsz), which needs the
    operator's prod.keys, but the check is the plugin's, not this route's, so
    any future gated tool is covered by implementing the hook.

    Asked through the registry's bounded seam. nsz's answer is a recursive walk
    of every configured volume, so an NFS/SMB/rclone mount that stops answering
    used to hang this route outright — and this one is the sidebar, awaited
    once per tool in turn, so a single dead volume held up the tools that were
    perfectly fine behind it. A probe that expires reports the tool
    unavailable, which is what a tool whose prerequisites cannot be read is.
    """
    ready = set(await registry.ready_tool_ids())
    available = [t.id for t in registry.all() if t.id in ready]
    unavailable = [t.id for t in registry.all() if t.id not in ready]
    return {"available": available, "unavailable": unavailable}


@router.get("/nsz-info", response_model=NszInfo)
async def get_nsz_info(
    path: str = Query(..., description="Path to Switch NSP/XCI/NSZ/XCZ file"),
):
    """Get basic information about a Nintendo Switch file."""
    if not await run_in_threadpool(
        is_within_configured_volumes, path, treat_archives=False,
    ):
        raise HTTPException(
            status_code=403,
            detail="Access denied: path outside configured volumes",
        )
    if not await run_in_threadpool(os.path.isfile, path):
        raise HTTPException(status_code=404, detail="File not found")
    if not _is_nsz_info_file(path):
        raise HTTPException(
            status_code=400,
            detail="Not a supported Switch format (.nsp, .xci, .nsz, .xcz)",
        )

    try:
        info = await run_in_threadpool(nsz_service.info, path)
        return NszInfo(
            file=info["file"],
            size=info["size"],
            size_display=info["size_display"],
            format=info.get("format"),
            extension=info["extension"],
            compressed=info["compressed"],
            compression_type=info.get("compression_type"),
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read Switch file info: {e!s}",
        ) from None


# ============ Generic verify routes (registry-driven) ============
#
# The three tools expose identical verify machinery (sync verify, an SSE
# progress stream, and a batch SSE stream). The only per-tool differences are
# the URL prefix, the underlying service object, and a couple of error strings.
# ``register_verify_routes`` generates the trio for any registered tool, and
# the two ``_sse_*`` adapters hold the queue/heartbeat loop that used to be
# copy-pasted once per endpoint.


@dataclass(frozen=True)
class _VerifyRouteConfig:
    """Per-tool data the verify-route factory needs (no behavior branching)."""

    url_prefix: str          # "" -> /verify, "dolphin-" -> /dolphin-verify, ...
    service: Callable        # returns the verify service (read at call time)
    sync_name: str           # FastAPI route name == legacy function name
    events_name: str
    batch_name: str
    bad_ext_detail: str      # 400 detail when the extension is unsupported
    verify_error_prefix: str  # 500 detail prefix raised by the sync endpoint


# Each ``service`` getter resolves the module-global service name at call time
# (not at import time), so the existing route tests can rebind
# ``info_routes.<service>`` to a mock and have the factory pick it up.
_VERIFY_CONFIG: dict[str, _VerifyRouteConfig] = {
    "chdman": _VerifyRouteConfig(
        url_prefix="",
        service=lambda: chdman_service,
        sync_name="verify_chd",
        events_name="verify_chd_events",
        batch_name="verify_batch_events",
        bad_ext_detail="Not a CHD file",
        verify_error_prefix="Failed to verify CHD",
    ),
    "dolphin": _VerifyRouteConfig(
        url_prefix="dolphin-",
        service=lambda: dolphin_tool_service,
        sync_name="verify_dolphin",
        events_name="verify_dolphin_events",
        batch_name="verify_dolphin_batch_events",
        bad_ext_detail="Not a supported disc image format",
        verify_error_prefix="Failed to verify disc image",
    ),
    "z3ds": _VerifyRouteConfig(
        url_prefix="z3ds-",
        service=lambda: z3ds_compress_service,
        sync_name="verify_z3ds",
        events_name="verify_z3ds_events",
        batch_name="verify_z3ds_batch_events",
        bad_ext_detail=(
            "Not a supported compressed 3DS format "
            "(.z3ds, .zcci, .zcia, .zcxi, .z3dsx)"
        ),
        verify_error_prefix="Failed to verify 3DS ROM",
    ),
    "nsz": _VerifyRouteConfig(
        url_prefix="nsz-",
        service=lambda: nsz_service,
        sync_name="verify_nsz",
        events_name="verify_nsz_events",
        batch_name="verify_nsz_batch_events",
        bad_ext_detail="Not a supported compressed Switch format (.nsz, .xcz)",
        verify_error_prefix="Failed to verify Switch file",
    ),
    "cso": _VerifyRouteConfig(
        url_prefix="cso-",
        service=lambda: maxcso_service,
        sync_name="verify_cso",
        events_name="verify_cso_events",
        batch_name="verify_cso_batch_events",
        bad_ext_detail="Not a supported compressed CSO format (.cso, .zso, .dax)",
        verify_error_prefix="Failed to verify CSO file",
    ),
    "jwud": _VerifyRouteConfig(
        url_prefix="jwud-",
        service=lambda: jwudtool_service,
        sync_name="verify_jwud",
        events_name="verify_jwud_events",
        batch_name="verify_jwud_batch_events",
        bad_ext_detail="Not a compressed Wii U disc image (.wux)",
        verify_error_prefix="Failed to verify Wii U image",
    ),
    "romz": _VerifyRouteConfig(
        url_prefix="romz-",
        service=lambda: romz_service,
        sync_name="verify_romz",
        events_name="verify_romz_events",
        batch_name="verify_romz_batch_events",
        bad_ext_detail="Not a supported ROM archive (.7z, .zip)",
        verify_error_prefix="Failed to verify ROM archive",
    ),
}


async def _guard_verify_path(
    path: str, tool: ToolPlugin, cfg: _VerifyRouteConfig,
) -> None:
    """403 (outside volumes) -> 404 (missing) -> 400 (unsupported extension).

    Both filesystem checks run through the shared **bounded** seam rather than
    the app's thread pool. They land ahead of every bound the verify itself
    carries, so on a volume that stopped answering they would hang the request
    before any of that applied — and, in a shared pool, take one worker per
    attempt with them (issue #266). A volume that does not answer is a 503:
    unlike 403/404 it says nothing about the path, only that the storage is
    unreachable right now.
    """
    try:
        within_volumes = await bounded_path_check(
            is_within_configured_volumes, path, treat_archives=False,
        )
        if not within_volumes:
            raise HTTPException(
                status_code=403,
                detail="Access denied: path outside configured volumes",
            )
        if not await bounded_path_check(os.path.isfile, path):
            raise HTTPException(status_code=404, detail="File not found")
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="Storage did not respond; the volume may be offline",
        ) from None
    if os.path.splitext(path)[1].lower() not in tool.verify_extensions:
        raise HTTPException(status_code=400, detail=cfg.bad_ext_detail)


def _verify_timed_out(bound: int) -> dict:
    """The terminal event/result for a route-level verify that outran its bound.

    The verify routes are a second entry point into the same verifiers the job
    pipeline drives, and they hold the "verify" workload lane while they run —
    so the bound has to apply here too, not only to delete-on-verify jobs
    (issue #266). Tools that spawn a subprocess enforce it themselves; this
    outer bound is what covers a verify with no child process to time out (the
    Wii U container walk, the PS3 PARAM.SFO readback) and any tool added later.
    Shaped like a tool's own timeout report, so callers need no new branch: the
    ``{"valid", "message"}`` the sync route returns, which the SSE paths widen
    with ``"type": "error"`` exactly as a tool's own terminal event carries it.
    """
    return {"valid": False, "message": f"Verification timed out after {bound}s"}


def _sse_from_verify_stream(
    tool: ToolPlugin, cfg: _VerifyRouteConfig, path: str,
) -> EventSourceResponse:
    """Single-file verify SSE stream (verify_progress / verify_complete / verify_error)."""

    async def event_generator():
        verify_token = await workload_limiter.try_acquire("verify")
        if verify_token is None:
            yield {
                "event": "verify_error",
                "data": json.dumps(
                    {
                        "type": "error",
                        "valid": False,
                        "message": _verification_backpressure_detail(),
                    },
                ),
            }
            return

        queue: asyncio.Queue = asyncio.Queue(maxsize=_VERIFY_QUEUE_LIMIT)
        done = asyncio.Event()
        start = time.monotonic()
        bound = 0

        async def run_verify():
            """Produce the verify events, under the bound, whoever is reading.

            The deadline lives here rather than in the consuming loop below: a
            client that stops draining suspends that loop at its `yield`, and a
            deadline it cannot evaluate is no deadline at all -- the verifier
            would run on with the verify lane held. Bounding the producer also
            means the elapsed clock measures verification instead of SSE
            delivery backpressure, and no terminal result can arrive after the
            timeout has been reported, because the same task decides both.
            """
            async def _pump() -> None:
                async for update in cfg.service().verify_stream(path):
                    await _offer_verify_update(queue, update)

            try:
                if bound > 0:
                    await asyncio.wait_for(_pump(), timeout=bound)
                else:
                    await _pump()
            except asyncio.TimeoutError:
                await _offer_verify_update(
                    queue,
                    {
                        "type": "error",
                        **_verify_timed_out(bound),
                        **_abandonment(abandoned),
                    },
                )
            except Exception as exc:
                await _offer_verify_update(
                    queue, {"type": "error", "valid": False, "message": str(exc)},
                )
            finally:
                done.set()
                # The lane covers verification, and verification is over: the
                # consumer below may still be parked at its `yield` on a peer
                # that stopped reading, and holding the one-slot lane until it
                # resumes would refuse every later verification for good.
                # Releasing twice is a no-op, so the generator's own finally
                # stays as the backstop for the paths that never start this.
                verify_token.release()

        # Everything after the token is acquired runs inside the try, including
        # resolving the bound: that await is a filesystem probe, and a client
        # disconnecting during it would otherwise unwind this generator with the
        # verify lane's only token still held -- permanently, so every later
        # verification would be refused as at-capacity.
        verify_task = None
        try:
            bound = await tool.verify_timeout(path)
            # Started inside the sink's block so the task's copied context
            # reports into `abandoned` -- and only into it.
            with collect_abandonment() as abandoned:
                verify_task = asyncio.create_task(run_verify())
            while True:
                try:
                    update = await asyncio.wait_for(queue.get(), timeout=2)
                except asyncio.TimeoutError:
                    elapsed = int(time.monotonic() - start)
                    yield {
                        "event": "verify_progress",
                        "data": json.dumps(
                            {"progress": None, "message": f"Verifying... ({elapsed}s)"},
                        ),
                    }
                    if done.is_set() and queue.empty():
                        break
                    continue

                if update.get("type") == "progress":
                    yield {"event": "verify_progress", "data": json.dumps(update)}
                elif update.get("type") == "complete":
                    if update.get("valid"):
                        await verification_store.mark_verified(path)
                    yield {"event": "verify_complete", "data": json.dumps(update)}
                    break
                elif update.get("type") == "error":
                    yield {"event": "verify_error", "data": json.dumps(update)}
                    break
        finally:
            if verify_task is not None:
                verify_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await verify_task
            verify_token.release()

    return EventSourceResponse(event_generator())


def _sse_batch_from_verify_stream(
    tool: ToolPlugin,
    cfg: _VerifyRouteConfig,
    valid_paths: list[str],
    verify_token: WorkloadToken,
) -> EventSourceResponse:
    """Batch verify SSE stream over an already-validated list of paths."""

    # The lane is held per *file*, not for the whole walk. The walk only
    # advances when events are delivered, so a peer that stays connected
    # without reading parks this generator between files -- with no verifier
    # running and, if the token were held for the batch, the one-slot lane
    # locked out for good. Between files nothing is verifying, so nothing is
    # owed a slot; the next file waits for the lane again, which terminates
    # because every verify is now bounded. The route hands us the slot it
    # already took for the first file.
    lane: dict[str, WorkloadToken | None] = {"token": verify_token}

    async def event_generator():
        # The route's token gated admission (429 when the lane is full); hand it
        # straight back rather than carrying it into the walk. Otherwise it
        # spans the suspensions before the first file's verifier even starts,
        # which is the same lost-lane shape on a peer that never reads.
        if lane["token"] is not None:
            lane["token"].release()
            lane["token"] = None

        total = len(valid_paths)
        verified_count = 0
        failed_count = 0

        # Send initial status
        yield {
            "event": "verify_batch_start",
            "data": json.dumps({"total": total, "paths": valid_paths}),
        }

        for idx, path in enumerate(valid_paths):
            filename = os.path.basename(path)
            # Set again just before the verify task starts (below): the clock
            # must measure verification, not the time this generator spends
            # suspended on a slow client between events.
            start = time.monotonic()

            # Send file start event
            yield {
                "event": "verify_batch_progress",
                "data": json.dumps(
                    {
                        "index": idx,
                        "total": total,
                        "path": path,
                        "filename": filename,
                        "status": "verifying",
                        "verified": verified_count,
                        "failed": failed_count,
                    },
                ),
            }

            try:
                # Use the streaming verify to get progress updates
                queue: asyncio.Queue = asyncio.Queue(maxsize=_VERIFY_QUEUE_LIMIT)
                done = asyncio.Event()
                final_result = {"valid": False, "message": "Unknown error"}

                bound = await tool.verify_timeout(path)

                async def run_verify(
                    path=path, bound=bound, queue=queue, done=done,
                ) -> None:
                    """Same producer-side bound as the single-file stream.

                    A batch client that stops draining suspends the loop below,
                    so a deadline evaluated there would stop being evaluated
                    exactly when it matters. Here it holds regardless of the
                    reader, the clock measures verification rather than delivery
                    backpressure, and the terminal result cannot disagree with
                    the timeout because one task produces both.
                    """
                    nonlocal final_result

                    async def _pump() -> None:
                        nonlocal final_result
                        async for update in cfg.service().verify_stream(path):
                            # Record the terminal result before enqueueing so a
                            # consumer that breaks on the "complete"/"error"
                            # event can't cancel this task mid-update.
                            if update.get("type") in ("complete", "error"):
                                final_result = update
                            await _offer_verify_update(queue, update)

                    try:
                        if bound > 0:
                            await asyncio.wait_for(_pump(), timeout=bound)
                        else:
                            await _pump()
                    except asyncio.TimeoutError:
                        final_result = {
                            **_verify_timed_out(bound),
                            **_abandonment(abandoned),
                        }
                        await _offer_verify_update(
                            queue, {"type": "error", **final_result},
                        )
                    except Exception as exc:
                        final_result = {
                            "type": "error",
                            "valid": False,
                            "message": str(exc),
                        }
                        await _offer_verify_update(queue, final_result)
                    finally:
                        done.set()
                        # Released here, not in the consumer's `finally`: this
                        # file's verification is over, and the consumer may be
                        # parked mid-file at a `yield` on a peer that stopped
                        # reading -- which is exactly the case where holding
                        # the one-slot lane refuses every later verification
                        # for good. Same rule as the single-file route.
                        if lane["token"] is not None:
                            lane["token"].release()
                            lane["token"] = None

                if lane["token"] is None:
                    lane["token"] = await workload_limiter.acquire("verify")

                start = time.monotonic()
                # Started inside the sink's block so the task's copied context
                # reports into this file's `abandoned` -- and only into it.
                with collect_abandonment() as abandoned:
                    verify_task = asyncio.create_task(run_verify())
                try:
                    while not done.is_set() or not queue.empty():
                        try:
                            update = await asyncio.wait_for(queue.get(), timeout=2)
                            if update.get("type") == "progress":
                                yield {
                                    "event": "verify_batch_file_progress",
                                    "data": json.dumps(
                                        {
                                            "index": idx,
                                            "path": path,
                                            "filename": filename,
                                            "progress": update.get("progress"),
                                            "message": update.get("message"),
                                        },
                                    ),
                                }
                            elif update.get("type") in ("complete", "error"):
                                break
                        except asyncio.TimeoutError:
                            elapsed = int(time.monotonic() - start)
                            yield {
                                "event": "verify_batch_file_progress",
                                "data": json.dumps(
                                    {
                                        "index": idx,
                                        "path": path,
                                        "filename": filename,
                                        "progress": None,
                                        "message": f"Verifying... ({elapsed}s)",
                                    },
                                ),
                            }
                finally:
                    verify_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await verify_task
                    # Backstop: the producer above releases as soon as the
                    # verification ends, but it never runs if the task was
                    # cancelled before it started.
                    if lane["token"] is not None:
                        lane["token"].release()
                        lane["token"] = None

                if final_result.get("valid"):
                    await verification_store.mark_verified(path)
                    verified_count += 1
                    status = "verified"
                else:
                    failed_count += 1
                    status = "failed"

                yield {
                    "event": "verify_batch_file_complete",
                    "data": json.dumps(
                        {
                            "index": idx,
                            "path": path,
                            "filename": filename,
                            "status": status,
                            "valid": final_result.get("valid", False),
                            "message": final_result.get("message"),
                            "verified": verified_count,
                            "failed": failed_count,
                        },
                    ),
                }

                if final_result.get("abandoned") or abandoned:
                    # The verifier outlived SIGKILL, so it is still reading the
                    # storage this batch is walking. Every remaining file lives
                    # on that same storage, so continuing would spawn one more
                    # unkillable verifier per file. Stop and say why.
                    yield {
                        "event": "verify_batch_complete",
                        "data": json.dumps(
                            {
                                "total": total,
                                "verified": verified_count,
                                "failed": failed_count,
                                "aborted": True,
                                "message": (
                                    "Batch stopped: a verifier could not be "
                                    "stopped and is still holding the storage."
                                ),
                            },
                        ),
                    }
                    return

            except Exception as exc:
                failed_count += 1
                yield {
                    "event": "verify_batch_file_complete",
                    "data": json.dumps(
                        {
                            "index": idx,
                            "path": path,
                            "filename": filename,
                            "status": "failed",
                            "valid": False,
                            "message": str(exc),
                            "verified": verified_count,
                            "failed": failed_count,
                        },
                    ),
                }

        # Send final completion event
        yield {
            "event": "verify_batch_complete",
            "data": json.dumps(
                {"total": total, "verified": verified_count, "failed": failed_count},
            ),
        }

    async def wrapped_event_generator():
        try:
            async for event in event_generator():
                yield event
        finally:
            # Backstop for the paths that never reach a per-file release (an
            # empty list, an early return, a disconnect before the first file).
            # Releasing twice is a no-op.
            if lane["token"] is not None:
                lane["token"].release()
                lane["token"] = None
            verify_token.release()

    return EventSourceResponse(wrapped_event_generator())


def register_verify_routes(router_: APIRouter, tool: ToolPlugin) -> tuple:
    """Register the verify trio for ``tool`` and return ``(sync, events, batch)``.

    Paths stay byte-identical via the ``tool_id -> url_prefix`` alias map, and
    each route is named after its legacy handler so OpenAPI operation ids and
    the module-level attribute names the tests rely on are preserved.
    """
    cfg = _VERIFY_CONFIG[tool.id]
    prefix = cfg.url_prefix

    @router_.get(f"/{prefix}verify", name=cfg.sync_name)
    async def _verify(path: str = Query(..., description="Path to file to verify")) -> dict:
        await _guard_verify_path(path, tool, cfg)
        verify_token = await _acquire_verify_lane_or_429()
        bound = 0
        try:
            # Inside the try, so a client that disconnects while this
            # filesystem probe is pending cannot strand the verify lane's token.
            bound = await tool.verify_timeout(path)
            result = await asyncio.wait_for(
                cfg.service().verify(path), timeout=bound or None,
            )
            if result.get("valid"):
                await verification_store.mark_verified(path)
            return result
        except asyncio.TimeoutError:
            # A verdict-shaped answer, not a 500: the file is not known bad, the
            # check simply did not finish inside its bound. Must precede the
            # generic handler -- asyncio.TimeoutError is an Exception.
            return _verify_timed_out(bound)
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"{cfg.verify_error_prefix}: {e!s}",
            ) from None
        finally:
            verify_token.release()

    @router_.get(f"/{prefix}verify/events", name=cfg.events_name)
    async def _verify_events(
        path: str = Query(..., description="Path to file to verify"),
    ) -> EventSourceResponse:
        await _guard_verify_path(path, tool, cfg)
        return _sse_from_verify_stream(tool, cfg, path)

    @router_.post(f"/{prefix}verify-batch/events", name=cfg.batch_name)
    async def _verify_batch(request: BulkVerifyRequest) -> EventSourceResponse:
        if not request.paths:
            raise HTTPException(status_code=400, detail="No paths provided")

        valid_paths = []
        for path in request.paths:
            # Bounded, like the single-file guard: one unreachable path in a
            # batch must not hang the whole request before any verify starts.
            try:
                if not await bounded_path_check(
                    is_within_configured_volumes, path, treat_archives=False,
                ):
                    continue
                if not await bounded_path_check(os.path.isfile, path):
                    continue
            except asyncio.TimeoutError as exc:
                # The *first* timeout ends validation rather than dropping this
                # path and probing the next. A batch is a list of paths on the
                # same storage: carrying on means one more abandoned thread and
                # one more probe bound per path -- minutes of delay before any
                # verify starts, and enough of them to exhaust the process-wide
                # probe ceiling, which then fails path checks on volumes that
                # are answering perfectly well.
                logger.warning(
                    "Batch verify: storage did not respond for %s; "
                    "abandoning validation of %d path(s)",
                    path, len(request.paths),
                )
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Storage is not responding. Verification was not "
                        "started for any of the selected files."
                    ),
                ) from exc
            if os.path.splitext(path)[1].lower() not in tool.verify_extensions:
                continue
            valid_paths.append(path)

        verify_token = await _acquire_verify_lane_or_429()
        return _sse_batch_from_verify_stream(tool, cfg, valid_paths, verify_token)

    return _verify, _verify_events, _verify_batch


# Explicit (registry-driven) registration. The factory body has no per-tool
# branching; binding the returned handlers to their legacy names keeps the
# ``info_routes.verify_*`` attributes the route tests call directly.
verify_chd, verify_chd_events, verify_batch_events = register_verify_routes(
    router, registry.get("chdman"),
)
verify_dolphin, verify_dolphin_events, verify_dolphin_batch_events = register_verify_routes(
    router, registry.get("dolphin"),
)
verify_z3ds, verify_z3ds_events, verify_z3ds_batch_events = register_verify_routes(
    router, registry.get("z3ds"),
)
verify_nsz, verify_nsz_events, verify_nsz_batch_events = register_verify_routes(
    router, registry.get("nsz"),
)
verify_cso, verify_cso_events, verify_cso_batch_events = register_verify_routes(
    router, registry.get("cso"),
)
verify_romz, verify_romz_events, verify_romz_batch_events = register_verify_routes(
    router, registry.get("romz"),
)
verify_jwud, verify_jwud_events, verify_jwud_batch_events = register_verify_routes(
    router, registry.get("jwud"),
)
