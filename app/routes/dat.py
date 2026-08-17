"""API routes for MAME Redump DAT file management and hash matching."""

import asyncio
from logging_setup import get_logger
import os
import stat
import tempfile
import time

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from config import settings
from models import ConversionMode
from services.dat_store import dat_store
from services.file_hasher import compute_file_sha1
from services.job_manager import ExternalJobCancelled, job_manager
from services.subprocess_runner import SubprocessAbandoned, reraise_if_abandoned
from services.tools import registry
from services.tools.base import EmbeddedHashUnavailable
from services.workload_limiter import workload_limiter
from utils.path_utils import is_within_configured_volumes

router = APIRouter()
logger = get_logger("dat")


# Guards concurrent bulk match jobs. Only one DAT-match background job runs
# at a time; concurrent requests return 409 (matches the /dat/sync pattern).
#
# Scope of this guard: **single-process** only.  The container entrypoint
# pins uvicorn to ``--workers 1`` (see entrypoint.sh), so module-level
# state is the authoritative source of truth across all requests hitting
# this app.  If the deployment ever moves to multi-worker / multi-pod,
# replace this with a distributed lock (Redis, SQLite advisory lock, or
# a dedicated matches-scheduler process), every worker would otherwise
# get its own independent lock and the 409 guard would no longer hold.
#
# Lazy-initialised: on Python 3.10+ ``asyncio.Lock()`` no longer binds a loop
# at construction (the deprecated ``loop=`` kwarg is gone), but its internal
# waiter state still ties to whichever loop first touches it.  pytest-asyncio
# creates a fresh event loop per test, so a module-level Lock constructed at
# import time can end up wedged to a stale loop across test runs.  Binding
# on first ``async with`` keeps the Lock local to the *current* loop and
# sidesteps any cold-import-order surprises.
_match_job_lock: asyncio.Lock | None = None
_active_match_job_id: str | None = None

# Strong refs for tasks scheduled via asyncio.create_task from non-HTTP
# callers (e.g. the post-sync rematch hook in services.dat_sync).
# asyncio will GC un-referenced Tasks mid-run; the done-callback removes
# the entry when the task finishes.
_background_match_tasks: set[asyncio.Task] = set()


def _log_background_match_task_error(task: asyncio.Task) -> None:
    """Surface exceptions from background match tasks via the project logger.

    Without this, any exception that escapes ``_run_match_job``'s own
    try/finally (e.g. ``finish_external_job`` raising from the finally)
    would only appear as asyncio's "Task exception was never retrieved"
    warning on GC, which is easy to miss in production log streams.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "Background match task %s raised",
            task.get_name(),
            exc_info=(type(exc), exc, exc.__traceback__),
        )


def _get_match_job_lock() -> asyncio.Lock:
    """Return the module-level match-job lock, creating it on first use.

    Must be called from a running event loop so the Lock binds to it.
    """
    global _match_job_lock  # noqa: PLW0603, intentional module-level state
    if _match_job_lock is None:
        _match_job_lock = asyncio.Lock()
    return _match_job_lock


class MatchRequest(BaseModel):
    path: str


class MatchBatchRequest(BaseModel):
    paths: list[str]


class SyncRequest(BaseModel):
    tag: str | None = None
    force: bool = False


@router.post("/dat/import")
async def import_dat(file: UploadFile = File(...)):
    """Import a MAME Redump DAT file (Logiqx XML format)."""
    if not file.filename or not file.filename.lower().endswith((".dat", ".xml")):
        raise HTTPException(
            status_code=400,
            detail="File must be a .dat or .xml file",
        )

    # Stream upload to a temp file to avoid holding the full content in memory
    max_size = 100 * 1024 * 1024  # 100MB
    total = 0
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".dat") as tmp:
            tmp_path = tmp.name
            while True:
                chunk = await file.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_size:
                    raise HTTPException(
                        status_code=400, detail="DAT file too large (max 100MB)"
                    )
                await run_in_threadpool(tmp.write, chunk)

        try:
            # Pass the temp file path (not its contents) so parse_dat() can
            # iterparse directly from disk without a second in-memory copy.
            result = await dat_store.import_dat(tmp_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return result


@router.get("/dat/list")
async def list_dats():
    """List all imported DATs."""
    return await run_in_threadpool(dat_store.list_dats)


@router.delete("/dat/{dat_id}")
async def delete_dat(dat_id: str):
    """Delete an imported DAT."""
    deleted = await dat_store.delete_dat(dat_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="DAT not found")
    return {"deleted": True, "id": dat_id}


@router.get("/dat/stats")
async def get_dat_stats():
    """Get DAT store statistics."""
    return await run_in_threadpool(dat_store.get_stats)


@router.post("/dat/match")
async def match_file(request: MatchRequest):
    """Match a single file against imported DATs."""
    # Resolve symlinks in a thread pool to avoid blocking the async event loop.
    # os.path.realpath and is_within_configured_volumes both perform filesystem
    # I/O; running them in the thread pool also ensures resolution errors (e.g.
    # on network filesystems) surface as a 4xx rather than an unhandled 500.
    normalized_path = await run_in_threadpool(os.path.realpath, request.path)

    if not await run_in_threadpool(is_within_configured_volumes, normalized_path):
        raise HTTPException(status_code=403, detail="Access denied")

    if not await run_in_threadpool(os.path.isfile, normalized_path):
        raise HTTPException(status_code=404, detail="File not found")

    try:
        result = await _match_single_file(normalized_path)
    except SubprocessAbandoned as exc:
        # The hash helper for this file could not be killed and is still
        # running, so the machine is in a state a retry cannot fix yet. 503
        # rather than a bare 500: it is a transient resource condition, and the
        # message names the pid an operator has to deal with (issue #268).
        logger.error("DAT match aborted for %s: %s", normalized_path, exc)
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return result


def _resolve_and_group_paths(
    paths: list[str],
) -> tuple[dict[str, list[str]], set[str]]:
    """Resolve paths and group by resolved form; identify denied paths.

    Both os.path.realpath and is_within_configured_volumes perform filesystem
    I/O, so this helper is intended to be called inside run_in_threadpool.

    os.path.realpath handles symlink loops gracefully by detecting the loop
    (via ELOOP) and returning a best-effort absolute path rather than raising.
    is_within_configured_volumes internally uses pathlib.Path.resolve(), which
    raises RuntimeError for symlink loops; that exception is caught inside
    path_utils._resolve_path (which returns None), causing the volume check to
    return False and the path to be added to denied_normalized.

    Returns:
        normalized_to_originals: resolved_path → list of original input paths
            that resolve to it (so alias inputs share one cache lookup).
        denied_normalized: resolved paths that lie outside configured volumes.
    """
    normalized_to_originals: dict[str, list[str]] = {}
    for p in paths:
        normalized = os.path.realpath(p)
        normalized_to_originals.setdefault(normalized, []).append(p)
    denied_normalized = {
        norm
        for norm in normalized_to_originals
        if not is_within_configured_volumes(norm)
    }
    return normalized_to_originals, denied_normalized


@router.post("/dat/match-batch")
async def match_batch(request: MatchBatchRequest):
    """Match multiple files against imported DATs."""
    if not await run_in_threadpool(dat_store.has_dats):
        return {"results": {p: {"path": p, "matched": False} for p in request.paths}}

    # Resolve all paths and check volume membership in a single thread-pool
    # call to avoid blocking the async event loop with filesystem I/O.
    normalized_to_originals, denied_normalized = await run_in_threadpool(
        _resolve_and_group_paths, request.paths
    )

    # Check cached matches using normalized paths
    cached = await run_in_threadpool(
        dat_store.get_matches_batch, list(normalized_to_originals.keys()),
    )
    results: dict[str, dict] = {}
    to_compute: list[str] = []  # normalized paths

    for normalized_path, original_paths in normalized_to_originals.items():
        if normalized_path in denied_normalized:
            result = {"path": normalized_path, "matched": False, "error": "access denied"}
            for original_path in original_paths:
                results[original_path] = result
            continue
        cached_result = cached.get(normalized_path)
        if cached_result is not None:
            for original_path in original_paths:
                results[original_path] = cached_result
        else:
            to_compute.append(normalized_path)

    # Compute matches for uncached files
    new_matches: dict[str, dict] = {}
    abandoned: SubprocessAbandoned | None = None
    stopped_at = len(to_compute)
    for idx, normalized_path in enumerate(to_compute):
        exists = await run_in_threadpool(os.path.isfile, normalized_path)
        if not exists:
            result = {"path": normalized_path, "matched": False}
            # Don't cache missing-file results: the file may appear later and
            # a stale negative entry would not be cleared by prune_missing.
        else:
            try:
                result = await _match_single_file(normalized_path)
            except SubprocessAbandoned as exc:
                # A hash subprocess outlived SIGKILL and is still running. Every
                # remaining path would go to the same unresponsive storage and
                # strand another one, so stop here and report the rest as errors
                # rather than working through the list (issue #268).
                logger.error("DAT match batch aborted: %s", exc)
                abandoned = exc
                stopped_at = idx
                break
            # Don't cache size-cap skips: the result is configuration-dependent.
            # If MATCH_MAX_FILE_SIZE is later raised or disabled the file must
            # be re-hashed rather than being served a stale "too large" entry.
            # Don't cache hash errors either: a transient OSError must not be
            # persisted as a permanent negative entry.
            if not result.get("reason") and not result.get("error"):
                new_matches[normalized_path] = result
        for original_path in normalized_to_originals[normalized_path]:
            results[original_path] = result

    if abandoned is not None:
        # Non-cacheable by construction (they carry "error"), so the abort
        # leaves nothing stale behind and a retry re-hashes them.
        for skipped in to_compute[stopped_at:]:
            result = {
                "path": skipped,
                "matched": False,
                "error": "aborted: a hash subprocess could not be stopped",
            }
            for original_path in normalized_to_originals[skipped]:
                results[original_path] = result

    # Cache new results using normalized path keys
    if new_matches:
        await dat_store.set_matches_batch(new_matches)

    return {"results": results}


class MatchCacheLookupRequest(BaseModel):
    paths: list[str]


@router.post("/dat/matches/lookup")
async def match_cache_lookup(request: MatchCacheLookupRequest):
    """Read-only cache lookup for DAT matches.

    Returns whatever is already cached in ``dat_matches`` for the given
    paths. Does **not** hash uncached files. Used by the frontend to
    progressively populate match badges while a background
    ``/dat/match-batch/job`` is running.
    """
    if not request.paths:
        return {"results": {}}

    normalized_to_originals, denied_normalized = await run_in_threadpool(
        _resolve_and_group_paths, request.paths,
    )
    cached = await run_in_threadpool(
        dat_store.get_matches_batch, list(normalized_to_originals.keys()),
    )
    results: dict[str, dict] = {}
    for normalized_path, original_paths in normalized_to_originals.items():
        if normalized_path in denied_normalized:
            for original_path in original_paths:
                results[original_path] = {
                    "path": original_path,
                    "matched": False,
                    "error": "access denied",
                }
            continue
        cached_entry = cached.get(normalized_path)
        if cached_entry is None:
            continue
        for original_path in original_paths:
            results[original_path] = cached_entry
    return {"results": results}


@router.post("/dat/match-batch/job")
async def match_batch_job(request: MatchBatchRequest, background_tasks: BackgroundTasks):
    """Start a background DAT-match job for a batch of files.

    Mirrors the metadata-scan UX: registers an external job in the Jobs
    panel, hashes uncached files serially under the ``match`` workload
    lane, persists cacheable results to the ``dat_matches`` cache
    incrementally as each file completes, and emits progress via
    :func:`job_manager.update_external_job`. The frontend polls
    ``/dat/matches/lookup`` as progress ticks arrive so badges can flip
    progressively from "DAT …" to a concrete cached result rather than
    all-at-once at the end.

    Note:
      ``/dat/matches/lookup`` is a read-only view of persisted
      ``dat_matches`` entries. Not every processed path is guaranteed to
      become available there: non-cacheable outcomes (for example missing
      files, ``reason == "file too large"``, or transient hash/stat
      errors) are intentionally not persisted in the cache, so those
      paths may never appear in ``/dat/matches/lookup``.
    Returns:
      * ``{"status": "idle", "results": <cached>}`` when every
        requested path is already cached (fast path, no job created).
      * ``{"status": "started", "job_id": "..."}`` when at least one
        path needs hashing.
      * HTTP 409 when another match job is already active.
    """
    if not request.paths:
        return {"status": "idle", "results": {}}

    if not await run_in_threadpool(dat_store.has_dats):
        return {
            "status": "idle",
            "results": {p: {"path": p, "matched": False} for p in request.paths},
        }

    normalized_to_originals, denied_normalized = await run_in_threadpool(
        _resolve_and_group_paths, request.paths,
    )

    cached = await run_in_threadpool(
        dat_store.get_matches_batch, list(normalized_to_originals.keys()),
    )

    results: dict[str, dict] = {}
    to_compute: list[str] = []
    for normalized_path, original_paths in normalized_to_originals.items():
        if normalized_path in denied_normalized:
            for original_path in original_paths:
                results[original_path] = {
                    "path": original_path,
                    "matched": False,
                    "error": "access denied",
                }
            continue
        cached_entry = cached.get(normalized_path)
        if cached_entry is not None:
            for original_path in original_paths:
                results[original_path] = cached_entry
            continue
        # Existence check happens inside the job loop so a missing file
        # doesn't fail the whole request, it just gets a matched=false
        # entry without being cached (same behaviour as sync /match-batch).
        to_compute.append(normalized_path)

    if not to_compute:
        return {"status": "idle", "results": results}

    job_id = await schedule_match_job(to_compute, background_tasks=background_tasks)
    if job_id is None:
        raise HTTPException(
            status_code=409,
            detail="DAT match job already in progress",
        )

    return {
        "status": "started",
        "job_id": job_id,
        "results": results,
    }


def _filter_paths_within_volumes(paths: list[str]) -> tuple[list[str], int]:
    """Re-resolve via ``realpath`` and keep only paths within configured volumes.

    The HTTP ``/dat/match-batch/job`` handler pre-filters paths through
    ``_resolve_and_group_paths``; non-HTTP callers (post-sync rematch hook)
    feed stored paths from ``DATMatch`` that were ACL-approved at write
    time, but a later ``CONFIGURED_VOLUMES`` tightening or a retargeted
    symlink can drift the admit set. Re-filter here so ``schedule_match_job``
    is the single authoritative ACL gate, idempotent when called on
    already-filtered paths.
    """
    allowed: list[str] = []
    denied = 0
    for p in paths:
        real = os.path.realpath(p)
        if is_within_configured_volumes(real):
            allowed.append(real)
        else:
            denied += 1
    return allowed, denied


async def schedule_match_job(
    paths: list[str],
    *,
    background_tasks: BackgroundTasks | None = None,
) -> str | None:
    """Start a background DAT-match job for *paths*.

    Returns the newly-created job id, or ``None`` if another match job is
    already active, *paths* is empty, or every path lies outside the
    configured volumes. When ``background_tasks`` is supplied (HTTP
    context) the task is registered with FastAPI so it runs after
    response-send; otherwise it is scheduled via ``asyncio.create_task``,
    used by non-HTTP callers such as the post-sync rematch hook in
    :mod:`services.dat_sync`.

    The ``_active_match_job_id`` + ``_match_job_lock`` guard is the
    authoritative "a hash loop is still executing" signal.  Do NOT
    fall back to inspecting ``job_manager`` status: jobs can be reaped
    via Clear-Done or a history prune while the underlying task is
    still alive, which would let a second job race the first on the
    "match" workload lane.
    """
    global _active_match_job_id
    if not paths:
        return None
    # ACL gate (see _filter_paths_within_volumes). Runs in a threadpool
    # because realpath + pathlib.resolve touch the filesystem.
    allowed, denied = await run_in_threadpool(
        _filter_paths_within_volumes, list(paths),
    )
    if denied:
        logger.warning(
            "schedule_match_job: dropped %d path(s) outside configured volumes",
            denied,
        )
    if not allowed:
        return None
    async with _get_match_job_lock():
        if _active_match_job_id is not None:
            return None
        scan_job = job_manager.create_external_job(
            filename="DAT Match",
            mode=ConversionMode.DAT_MATCH,
            message=f"Hashing {len(allowed)} file(s)\u2026",
        )
        _active_match_job_id = scan_job.id
    # Lock released before scheduling the task on purpose: the
    # authoritative "job in progress" signal is _active_match_job_id
    # (set inside the lock above), not the lock itself. A second caller
    # arriving in this window re-enters the lock, sees the id set, and
    # returns None, no race.
    #
    # If scheduling the actual task fails after we've claimed the slot,
    # roll back _active_match_job_id and mark the phantom external job
    # as failed, otherwise the lock stays held forever and every future
    # match request returns 409 until process restart.
    # except BaseException (not Exception): event-loop shutdown surfaces
    # as CancelledError / KeyboardInterrupt, and those must still roll
    # the id back, leaving the process-level match lock held after a
    # Ctrl-C would deadlock the next run.
    try:
        if background_tasks is not None:
            background_tasks.add_task(
                _run_match_job, job_id=scan_job.id, paths_to_compute=allowed,
            )
        else:
            # Invariant: _background_match_tasks holds at most one task
            # at a time because _active_match_job_id gates re-entry. If
            # this ever prints a warning, the concurrency guard above
            # has been bypassed, investigate before shipping.
            if _background_match_tasks:
                logger.warning(
                    "schedule_match_job: _background_match_tasks was non-empty"
                    " at schedule time (size=%d), concurrency guard may be compromised",
                    len(_background_match_tasks),
                )
            task = asyncio.create_task(
                _run_match_job(job_id=scan_job.id, paths_to_compute=allowed),
            )
            _background_match_tasks.add(task)
            task.add_done_callback(_background_match_tasks.discard)
            # If _run_match_job raises past its own try/finally (rare,
            # only if finish_external_job itself throws from the finally
            # block) the exception is stored on the Task and logged by
            # asyncio as "Task exception was never retrieved" on GC.
            # Surface it through the project logger instead so operators
            # see it in the normal log stream.
            task.add_done_callback(_log_background_match_task_error)
    except BaseException:
        async with _get_match_job_lock():
            if _active_match_job_id == scan_job.id:
                _active_match_job_id = None
        try:
            await job_manager.finish_external_job(
                scan_job.id,
                success=False,
                error_message="Failed to schedule DAT match job",
            )
        except Exception:
            logger.exception(
                "Failed to finalize phantom match job %s after scheduling error",
                scan_job.id,
            )
        raise
    return scan_job.id


async def _hash_one_for_job(
    normalized_path: str, *, cancel_event: asyncio.Event | None = None,
) -> tuple[dict, bool]:
    """Compute a match result for one path inside the background job loop.

    Returns ``(result, cacheable)``.  ``cacheable`` is ``False`` for
    transient failures (missing file, stat error, hasher exception) and
    configuration-dependent skips (size-cap hit); those must not be
    persisted to ``dat_matches`` because they would stick around after
    the underlying condition changes (file appears, cap is raised,
    transient OSError clears).

    ``cancel_event`` is forwarded so an expensive embedded-hash derivation
    (e.g. ``dolphin-tool verify``) aborts promptly when the job is cancelled.
    """
    try:
        st = await run_in_threadpool(os.stat, normalized_path)
    except FileNotFoundError:
        return {"path": normalized_path, "matched": False}, False
    except OSError as exc:
        logger.warning("Failed to stat %s: %s", normalized_path, exc)
        return {"path": normalized_path, "matched": False, "error": str(exc)}, False

    if not stat.S_ISREG(st.st_mode):
        return {"path": normalized_path, "matched": False}, False

    try:
        result = await _match_single_file(normalized_path, cancel_event=cancel_event)
    except Exception as exc:  # pragma: no cover, isolated per-path
        # One failure is deliberately NOT isolated per-path: a hash subprocess
        # that survived SIGKILL is still running, so continuing the loop would
        # strand another against the same unresponsive storage for every
        # remaining file. Let it out to _run_match_job, which fails the job
        # (issue #268).
        reraise_if_abandoned(exc)
        # logger.exception rather than logger.warning: a KeyError /
        # AttributeError from a refactor bug should surface with a full
        # traceback at ERROR level, not be buried as a one-line warning.
        logger.exception("DAT match failed for %s", normalized_path)
        return {"path": normalized_path, "matched": False, "error": str(exc)}, False

    if result.get("reason") == "file too large" or result.get("error"):
        return result, False
    return result, True


async def _run_match_job(
    *,
    job_id: str,
    paths_to_compute: list[str],
) -> None:
    """Background task: hash paths serially, cache results, tick progress."""
    global _active_match_job_id

    start = time.monotonic()
    total = len(paths_to_compute)
    processed = 0
    hashed = 0
    matched = 0
    # Per-file outcomes that are NOT "matched vs unmatched":
    #   errors, real failures the caller should know about: anything
    #            _hash_one_for_job labels with result["error"] (stat
    #            OSError other than FileNotFoundError, hasher exception,
    #            _match_single_file exception) OR a cache-write exception.
    #            If every file errors we fail the whole job so the user
    #            sees the volume-offline-style problem rather than a
    #            misleading "complete, 0 matched" that looks like a DAT
    #            coverage gap.
    #   skips , policy/classification non-errors: size cap hit,
    #            non-regular file, FileNotFoundError (result has no
    #            "error" key in these cases). Informational only.
    errors = 0
    skips = 0
    # Tri-state: True = success, False = failure, None = cancelled.
    job_success: bool | None = False
    job_error: str | None = None

    # Set when the job is cancelled; forwarded into expensive embedded-hash
    # hooks (e.g. dolphin-tool verify) so the in-flight file aborts promptly
    # rather than after it finishes.
    cancel_event = job_manager.get_cancel_event(job_id)

    try:
        for idx, normalized_path in enumerate(paths_to_compute, start=1):
            if job_manager.is_cancelled(job_id):
                raise ExternalJobCancelled()
            display_name = os.path.basename(normalized_path) or normalized_path
            await job_manager.update_external_job(
                job_id,
                progress=int(100 * (idx - 1) / total) if total else 0,
                message=f"[{idx}/{total}] {display_name}",
            )

            result, cacheable = await _hash_one_for_job(
                normalized_path, cancel_event=cancel_event,
            )
            if cacheable:
                hashed += 1
                # Per-file writes are intentional: persist each completed
                # result immediately with the single-row API so the cache
                # remains durable if the job is cancelled or the process
                # crashes mid-run, without paying the extra preload/prefetch
                # work of the batch upsert path for a one-item write.
                try:
                    await dat_store.set_match(normalized_path, result)
                except Exception:  # pragma: no cover, best-effort cache write
                    logger.exception("Failed to cache match for %s", normalized_path)
                    errors += 1
            else:
                # Non-cacheable outcomes split into errors (something went
                # wrong, see _hash_one_for_job) vs skips (policy: file
                # too large, not a regular file). `result.get("error")`
                # is the discriminator that _hash_one_for_job sets.
                if result.get("error"):
                    errors += 1
                else:
                    skips += 1

            processed += 1
            if result.get("matched"):
                matched += 1

            # A cancellable embedded-hash hook (e.g. dolphin run_capture) may
            # have been aborted mid-file, returning a non-cacheable error
            # rather than raising. Re-check after the per-file work (the
            # completed result is already persisted, the cache is durable
            # across cancellation by design) so a cancel during the last/only
            # path finalizes the job as cancelled instead of failed/complete.
            if job_manager.is_cancelled(job_id):
                raise ExternalJobCancelled()

        # If every single file errored, something structural is wrong
        # (volume unmounted, DB down, etc.).  Flip the job to failure so
        # the user sees a red signal rather than a misleading "complete,
        # 0 matched" that looks like a DAT-coverage gap.
        if total > 0 and errors == total:
            job_success = False
            job_error = (
                f"all {errors} file(s) failed, check volume accessibility"
            )
        else:
            job_success = True
    except ExternalJobCancelled:
        # Cancellation is a clean exit path, not a failure. The loop has
        # already persisted each completed per-file result (see the
        # "durable if the job is cancelled" comment in the success branch),
        # so partial cache entries stay by design.
        job_success = None
    except Exception as exc:
        logger.exception("DAT match job %s failed", job_id)
        # Include counters so the final-status line tells the operator
        # how far the job got before the mid-loop failure.  Without
        # this, the user only sees the raw exception string and loses
        # the "processed 12/69 before failure" context.
        parts = [f"processed {processed}/{total}"]
        if errors:
            parts.append(f"{errors} error(s)")
        if skips:
            parts.append(f"{skips} skipped")
        context = ", ".join(parts)
        job_error = f"{exc} ({context} before failure)"
    finally:
        # Release the match-job lock BEFORE the finalize calls: if
        # finish_external_job / finish_external_job_cancelled itself raises
        # (e.g. from _notify_subscribers), _active_match_job_id would stay
        # pinned to this dead job id and every subsequent match request
        # would 409 until process restart.
        async with _get_match_job_lock():
            if _active_match_job_id == job_id:
                _active_match_job_id = None
        elapsed = time.monotonic() - start
        if job_success is None:
            parts = [f"{processed}/{total} processed, {hashed} hashed, {matched} matched"]
            if errors:
                parts.append(f"{errors} error(s)")
            if skips:
                parts.append(f"{skips} skipped")
            final_msg = "Cancelled \u2014 " + ", ".join(parts) + f" ({elapsed:.1f}s)"
            await job_manager.finish_external_job_cancelled(
                job_id,
                message=final_msg,
            )
        else:
            if job_success:
                parts = [f"{processed}/{total} processed, {hashed} hashed, {matched} matched"]
                if errors:
                    parts.append(f"{errors} error(s)")
                if skips:
                    parts.append(f"{skips} skipped")
                final_msg = ", ".join(parts) + f" \u2014 {elapsed:.1f}s"
            else:
                final_msg = f"DAT match failed: {job_error or 'unknown error'}"
            await job_manager.update_external_job(
                job_id,
                progress=100 if job_success else None,
                message=final_msg,
            )
            await job_manager.finish_external_job(
                job_id,
                success=job_success,
                error_message=job_error,
            )


@router.post("/dat/prune")
async def prune_missing():
    """Remove match cache entries for files that no longer exist."""
    removed = await dat_store.prune_missing()
    return {"removed": removed}


# ---------------------------------------------------------------------------
# MAMERedump sync endpoints
# ---------------------------------------------------------------------------

def _get_sync_service():
    from services.dat_sync import dat_sync_service
    return dat_sync_service


@router.post("/dat/sync")
async def sync_mameredump(http_request: Request, request: SyncRequest | None = None):
    """Trigger a sync of all DAT files from the MAMERedump GitHub repo.

    The sync runs in the background. Poll ``/dat/sync/status`` for progress.
    """
    svc = _get_sync_service()
    if svc.is_syncing:
        raise HTTPException(status_code=409, detail="Sync already in progress")

    tag = request.tag if request else None
    force = bool(request.force) if request else False

    async def _run_sync():
        await svc.sync(tag=tag, force=force)

    task = asyncio.create_task(_run_sync())

    # Keep a strong reference so the Task isn't garbage-collected before it
    # finishes; the done callback removes it from the shared app-level set.
    bg_tasks: set[asyncio.Task] = http_request.app.state.background_tasks
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)

    def _log_bg_error(t: asyncio.Task) -> None:
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                logger.error(
                    "dat_sync background task failed",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

    task.add_done_callback(_log_bg_error)

    # Yield to the event loop so the background task has an opportunity to enter
    # svc.sync() and claim the syncing lock before we respond.  svc.sync()
    # acquires _syncing under a threading.Lock (no awaits before that point),
    # so in practice a single yield is sufficient, but asyncio scheduling order
    # is not a documented guarantee, so this is a best-effort check rather than
    # a hard guarantee.  After asyncio.sleep(0), if the task is already done it
    # raised immediately (e.g. a near-simultaneous request already held the
    # lock) and we can return the correct status code.
    await asyncio.sleep(0)

    if task.done():
        try:
            task.result()
        except RuntimeError as exc:
            if "Sync already in progress" in str(exc):
                raise HTTPException(
                    status_code=409, detail="Sync already in progress"
                ) from exc
            logger.exception("dat_sync background task failed at startup")
            raise HTTPException(status_code=500, detail="Failed to start sync") from exc
        except Exception as exc:
            logger.exception("dat_sync background task failed at startup")
            raise HTTPException(status_code=500, detail="Failed to start sync") from exc

    return {"status": "started", "message": "Sync started"}


@router.get("/dat/sync/status")
async def sync_status():
    """Return the current sync status."""
    return _get_sync_service().get_status()


@router.post("/dat/sync/cancel")
async def sync_cancel():
    """Cancel an in-progress sync."""
    if _get_sync_service().cancel():
        return {"status": "cancelling"}
    raise HTTPException(status_code=409, detail="No sync in progress")


async def _lookup_sha1_match(file_path: str, sha1: str, match_type: str) -> dict | None:
    """Look ``sha1`` up in the imported DATs and build a match-result dict.

    Shared by every match path (per-tool embedded hashes and the file-level
    SHA1 fallback) so the DAT lookup + result-dict shape lives in one place.
    Returns ``None`` when the hash isn't in any DAT.
    """
    record = await run_in_threadpool(dat_store.lookup_sha1, sha1)
    if not record:
        return None
    dat_name = await run_in_threadpool(dat_store.get_dat_name, record.get("dat_id", ""))
    return {
        "path": file_path,
        "matched": True,
        "dat_id": record.get("dat_id"),
        "dat_name": dat_name,
        "game_name": record.get("game_name"),
        "rom_name": record.get("rom_name"),
        "match_type": match_type,
        "file_hash": sha1,
    }


async def _match_single_file(
    file_path: str, *, cancel_event: asyncio.Event | None = None,
) -> dict:
    """Match a file against all imported DATs.

    Fast path: ask the tool that owns this file type for any embedded /
    derivable content hashes (CHD header & data SHA1, Dolphin disc SHA1,
    ...) and try those against the DAT index first. Falls back to a
    full file-level SHA1 (format-agnostic) when no tool hash matches.

    ``cancel_event`` is forwarded to the tool's (potentially expensive)
    embedded-hash hook so a background scan/match job can abort it promptly.
    """
    base_result = {"path": file_path, "matched": False}

    if not await run_in_threadpool(dat_store.has_dats):
        return base_result

    # Per-tool embedded-hash fast path (already cached / cheap where the tool
    # can manage it, e.g. CHD header hashes from the metadata store).
    tool = registry.tool_for_verify(file_path)
    if tool is not None:
        try:
            match, had_candidates = await _try_embedded_hash_match(
                file_path, tool, cancel_event=cancel_event,
            )
        except EmbeddedHashUnavailable as e:
            # The tool couldn't derive its content hash (e.g. dolphin-tool
            # verify failed). For these formats the file-level SHA1 of the
            # container is meaningless against a DAT, so return a
            # non-cacheable error instead of a false "unmatched".
            logger.info("Embedded hash unavailable for %s: %s", file_path, e)
            return {**base_result, "error": "embedded hash unavailable"}
        if match:
            return match
        if had_candidates and tool.embedded_hash_is_exhaustive:
            # The tool's content hashes are exhaustive (e.g. Dolphin's disc
            # SHA1): a miss is definitive and the container's file-level SHA1
            # can never match the DAT, so record a (cacheable) unmatched result
            # without re-reading the whole file. Tools whose own container
            # bytes may be DAT-indexed (e.g. CHD) deliberately fall through to
            # the file-level SHA1 below.
            return base_result

    # Defense-in-depth: respect the operator-configured size cap so
    # browsing a folder of 8 GB Wii ISOs doesn't stampede the hasher.
    size_cap = max(0, int(getattr(settings, "match_max_file_size", 0) or 0))
    if size_cap > 0:
        try:
            size_bytes = await run_in_threadpool(os.path.getsize, file_path)
        except OSError:
            size_bytes = 0
        if size_bytes > size_cap:
            return {
                **base_result,
                "reason": "file too large",
                "file_size": size_bytes,
            }

    # File-level SHA1 (works for any format). Gate under the "match"
    # workload lane so ``MAX_MATCH_CONCURRENCY`` bounds how many full-
    # file hashes run at once when a directory of uncached files is
    # browsed.
    try:
        async with await workload_limiter.acquire("match"):
            file_sha1 = await compute_file_sha1(file_path)
    except OSError:
        logger.warning("Failed to hash %s", file_path, exc_info=True)
        return {**base_result, "error": "Unable to process file"}

    return await _lookup_sha1_match(file_path, file_sha1, "file_sha1") or base_result


async def _try_embedded_hash_match(
    file_path: str, tool, *, cancel_event: asyncio.Event | None = None,
) -> tuple[dict | None, bool]:
    """Try matching ``file_path`` using the hashes ``tool`` reports for it.

    Returns ``(match, had_candidates)``. ``match`` is the DAT hit (or ``None``).
    ``had_candidates`` is True when the tool produced at least one embedded
    hash. It is only an *input* to the caller's fallback decision, not the
    decision itself: the caller skips the file-level SHA1 fallback after a miss
    only when ``had_candidates`` AND ``tool.embedded_hash_is_exhaustive`` (the
    container bytes can never be DAT-indexed, e.g. Dolphin RVZ/WIA/GCZ). Tools
    whose own file SHA1 may be indexed (e.g. CHD) still fall back even though
    they reported candidates. When ``had_candidates`` is False the tool has no
    embedded hash and the file-level fallback is always the correct next step.
    """
    try:
        candidates = await tool.embedded_hashes(file_path, cancel_event=cancel_event)
    except EmbeddedHashUnavailable as exc:
        # Transient "couldn't derive the hash" — let the caller decide it's a
        # non-cacheable error rather than falling back to a file-level hash.
        # Unless it wraps an abandonment: a child that survived SIGKILL is not a
        # fact about *this* file, it is still running against the same storage,
        # and the caller must stop walking rather than log a miss (issue #268).
        reraise_if_abandoned(exc)
        raise
    except Exception as exc:  # pragma: no cover - unexpected tool failure
        # Same rule for an unexpected failure, and it matters more here: the
        # non-exhaustive branch below *swallows* the error and falls back to a
        # file-level hash, which would hide the stranded process completely.
        reraise_if_abandoned(exc)
        logger.warning("embedded_hashes failed for %s", file_path, exc_info=True)
        if tool.embedded_hash_is_exhaustive:
            # For exhaustive tools (e.g. Dolphin) the container's file-level
            # SHA1 can never match the DAT, so falling back would cache a false
            # negative. Surface it as non-cacheable instead.
            raise EmbeddedHashUnavailable("embedded hash derivation failed") from exc
        return None, False

    had_candidates = False
    for raw_hash, match_type in candidates:
        sha1 = (raw_hash or "").strip().lower()
        if not sha1:
            continue
        had_candidates = True
        match = await _lookup_sha1_match(file_path, sha1, match_type)
        if match:
            return match, True

    return None, had_candidates
