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
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from models import DirectoryListing, FileEntry
from pydantic import BaseModel
from routes.files import detect_file_outputs, verifiable_tools
from services import romm_auto, romm_repin, romm_settings
from services.file_hasher import compute_file_sha1_sync
from services.job_manager import job_manager
from services.lock_manager import lock_manager
from services.output_conflicts import QUEUE, resolve_destination
from services.preferences_store import preferences_store
from services.romm import (
    DAT_SAFE_OUTPUT_EXTS,
    RommClient,
    RommError,
    RommNotConfigured,
    romm_client,
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
            inside = is_dir and is_within_configured_volumes(library_root)
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
    ready = await asyncio.gather(*(t.is_ready() for t in registry.all()))
    all_tool_ids = [
        tool.id for tool, ok in zip(registry.all(), ready, strict=True) if ok
    ]
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
    entries = await run_in_threadpool(_build_entries, roms, slug)
    return DirectoryListing(
        volume="RomM", path=f"romm://platform/{platform_id}", entries=entries,
    )


def _build_entries(roms: list[dict], platform_slug: str | None) -> list[FileEntry]:
    """Turn RomM records into FileEntry rows. Runs off the event loop."""
    entries: list[FileEntry] = []
    for rom in roms:
        path = romm_client.local_path(rom)
        if not path or not is_within_configured_volumes(path):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            # missing_from_fs, a permissions problem, or a stale record.
            continue
        if not stat_module.S_ISREG(stat.st_mode):
            continue
        convertible_by, outputs, _ = detect_file_outputs(path)
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
                type="file",
                size=stat.st_size,
                extension=os.path.splitext(name)[1].lower() or None,
                convertible_by=convertible_by,
                outputs=outputs,
                verifiable_by=verifiable_tools(path),
            ),
        )
    # Deterministic ordering, independent of RomM's paging. Sorts on the title
    # the user actually reads, falling back to the filename.
    entries.sort(key=lambda e: ((e.display_name or e.name).lower(), e.path))
    return entries


# ----------------------------------------------------------------------
# metadata re-pin
# ----------------------------------------------------------------------


class RepinPlanRequest(BaseModel):
    """Rows to record before a batch of RomM conversions is submitted."""

    paths: list[str]
    mode: str
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

    # One catalog read for the whole batch, indexed by local path, instead of a
    # by-hash lookup per file.
    try:
        platform_roms = await run_in_threadpool(romm_repin.roms_by_local_path, payload.paths)
    except RommError as exc:
        raise _romm_call(exc, context="reading the catalog") from exc

    recorded = 0
    skipped = 0
    for path in payload.paths:
        if not is_within_configured_volumes(path):
            skipped += 1
            continue
        # Same canonical key the index was built with (see roms_by_local_path).
        rom = platform_roms.get(os.path.realpath(path))
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
        destination, decision = await run_in_threadpool(
            resolve_destination,
            tool, path, payload.mode, payload.output_dir,
            payload.duplicate_action,
        )
        if decision != QUEUE or not destination:
            skipped += 1
            continue
        if await run_in_threadpool(romm_repin.record, rom, destination, ids):
            recorded += 1
        else:
            skipped += 1
    return {"recorded": recorded, "skipped": skipped}


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


async def _settle_one_repin(row: tuple) -> _Outcome:
    """Drive one pending re-pin row as far as it can go this pass.

    Extracted from the loop so the loop owns only paging and budgets: the
    exists/lock/hash/match/update/settle chain is a single state machine and
    reads as one.
    """
    output_path, sha1, _rom_id, ids, created_at, row_id = row

    try:
        exists = await bounded_path_check(os.path.isfile, output_path)
    except (asyncio.TimeoutError, OSError):
        # Unresponsive volume: treat as "not yet", never as abandoned.
        return _Outcome.WAITING
    if not exists:
        if not _is_stale(created_at):
            return _Outcome.WAITING
        await run_in_threadpool(
            romm_repin.settle, row_id, "abandoned", "Output never appeared",
        )
        return _Outcome.ABANDONED

    # Never hash a file a converter is still writing. An output becomes a
    # regular file the moment the tool creates it, so without this the pass
    # could cache the SHA-1 of a partial file -- and because the hash is
    # cached, every later attempt would reuse that wrong digest and the ROM
    # could never be matched again. A locked source means the job is still
    # running, which is simply "not yet".
    try:
        _, locked = await run_in_threadpool(
            lock_manager.check_file_status, output_path,
        )
    except OSError:
        locked = False
    if locked:
        return _Outcome.WAITING

    # A *queued* job holds no lock yet, so under the overwrite policy the file
    # sitting at this destination is still the OLD artifact. Hashing it now
    # would cache the wrong digest -- and because the hash is cached, the ROM
    # could never be matched once the real output replaced it.
    if await run_in_threadpool(_destination_has_pending_job, output_path):
        return _Outcome.WAITING

    try:
        if not sha1:
            # Hashing a multi-GB image is heavy disk work: take the same lane
            # the DAT matcher uses so a re-pin pass cannot compete with a
            # running conversion for the array.
            #
            # `run_detached`, not the shared threadpool: this reads the whole
            # file, and on a mount that stops answering mid-read the thread
            # cannot be cancelled. AGENTS.md is explicit that such a read must
            # never take a pooled worker -- repeated attempts would strand one
            # each time and starve unrelated API work.
            async with await workload_limiter.acquire("match"):
                sha1 = await run_detached(compute_file_sha1_sync, output_path)
            # Cache it: a row may be retried many times before RomM scans.
            await run_in_threadpool(romm_repin.store_sha1, row_id, sha1)

        match = await run_in_threadpool(romm_client.rom_by_sha1, sha1)
        if not match:
            return _Outcome.WAITING

        # Re-check ownership immediately before the outbound write. Hashing the
        # output above can take minutes, and a conversion re-planned in that
        # window supersedes this row -- pushing these ids to RomM afterwards
        # would stamp the previous generation's identity onto the file the new
        # conversion just produced.
        if not await run_in_threadpool(romm_repin.is_pending, row_id):
            logger.info(
                "romm: re-pin row %s was superseded while hashing; skipping", row_id,
            )
            return _Outcome.FAILED

        await run_in_threadpool(romm_client.update_rom_metadata, match["id"], ids)
        # Count only what actually settled: losing the row between the re-check
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
    except OSError as exc:
        logger.warning("romm: could not hash %s: %s", output_path, exc)
        return _Outcome.FAILED


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
    counts = {outcome: 0 for outcome in _Outcome}

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
        row = rows.pop(0)
        examined += 1
        cursor = row[5]
        if not rows:
            rows = await run_in_threadpool(
                romm_repin.pending_rows, _SETTLE_PAGE, after_id=cursor,
            )

        outcome = await _settle_one_repin(row)
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
        "pending": await run_in_threadpool(romm_repin.count_pending),
    }


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
    """Save settings and apply them immediately (no restart)."""
    values = await romm_settings.save(patch.model_dump(exclude_unset=True))
    return romm_settings.public(values)


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
            result["library_root_mounted"] = (
                is_dir and is_within_configured_volumes(library_root)
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


@router.post("/romm/auto-convert/preview")
async def preview_auto_convert(payload: dict | None = None) -> dict:
    """What a sweep would queue right now, without queueing anything.

    Ignores each rule's schedule so the operator can see the effect of a rule
    they just wrote instead of waiting for its next window.
    """
    _require_configured()
    payload = payload or {}
    return await romm_auto.sweep(
        platform_ids=payload.get("platform_ids"),
        ignore_schedule=True,
        dry_run=True,
        overall_limit=payload.get("limit"),
    )


@router.post("/romm/auto-convert/run")
async def run_auto_convert(payload: dict | None = None) -> dict:
    """Run a sweep now, queueing real jobs.

    Manual runs ignore the schedule -- pressing the button means "now" -- but
    still honour every other part of each rule (filters, caps, ordering).
    """
    _require_configured()
    payload = payload or {}
    return await romm_auto.sweep(
        platform_ids=payload.get("platform_ids"),
        ignore_schedule=True,
        dry_run=False,
        overall_limit=payload.get("limit"),
    )
