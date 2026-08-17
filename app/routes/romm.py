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
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from models import DirectoryListing, FileEntry
from pydantic import BaseModel
from routes.files import detect_file_outputs, verifiable_tools
from services import db as _db
from services.file_hasher import compute_file_sha1
from services.romm import (
    DAT_SAFE_OUTPUT_EXTS,
    METADATA_ID_FIELDS,
    RommError,
    RommNotConfigured,
    romm_client,
)
from services.subprocess_runner import bounded_path_check
from services.tools import registry
from services.workload_limiter import workload_limiter
from utils.path_utils import is_within_configured_volumes

router = APIRouter()
logger = get_logger("romm")

# A pending row whose output never appeared is a conversion that was planned but
# never ran (the batch submit failed, or the job was cancelled). Left alone it
# would be retried forever, so retire it once it is clearly not coming.
_ABANDON_AFTER = timedelta(days=7)

# Ceiling on rows settled in one pass. Each miss costs a RomM round trip and each
# hit costs a full-file SHA-1, so a large backlog is drained over several calls
# rather than in one request that runs for an hour.
_MAX_SETTLE_PER_CALL = 25


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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


def _romm_call(exc: RommError) -> HTTPException:
    """Translate a client error into the right status.

    502, not 500: the failure is upstream, and a caller retrying against a
    healthy RomM would succeed.
    """
    if isinstance(exc, RommNotConfigured):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


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
        "token_set": bool(os.environ.get("ROMM_TOKEN")),
        # SSOT for which targets keep RomM's DAT match. The frontend renders the
        # warning from this rather than carrying its own copy of the list.
        "dat_safe_output_exts": sorted(DAT_SAFE_OUTPUT_EXTS),
        "connected": False,
        "version": None,
        "error": None,
    }
    if library_root:
        # A dead NFS/SMB mount blocks in uninterruptible I/O, so this stat goes
        # through the shared bounded probe like every other path check.
        try:
            result["library_root_mounted"] = bool(
                await bounded_path_check(os.path.isdir, library_root),
            )
        except (asyncio.TimeoutError, OSError) as exc:
            # A dead mount is exactly what this probe exists to survive: report
            # it as status rather than failing the request.
            result["error"] = f"Could not stat ROMM_LIBRARY_ROOT: {exc}"
    if configured:
        try:
            heartbeat = await run_in_threadpool(romm_client.heartbeat)
            result["connected"] = True
            result["version"] = (heartbeat.get("VERSION") or heartbeat.get("version"))
        except RommError as exc:
            result["error"] = str(exc)
    result["pending_repins"] = await run_in_threadpool(_count_pending)
    return result


@router.get("/romm/platforms")
async def romm_platforms() -> list[dict]:
    """List RomM's platforms, slimmed to what the picker needs."""
    _require_configured()
    try:
        platforms = await run_in_threadpool(romm_client.platforms)
    except RommError as exc:
        raise _romm_call(exc) from exc
    out = [
        {
            "id": p.get("id"),
            "name": p.get("display_name") or p.get("name") or p.get("slug"),
            "slug": p.get("slug"),
            "rom_count": p.get("rom_count"),
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
        raise _romm_call(exc) from exc

    entries = await run_in_threadpool(_build_entries, roms)
    return DirectoryListing(
        volume="RomM", path=f"romm://platform/{platform_id}", entries=entries,
    )


def _build_entries(roms: list[dict]) -> list[FileEntry]:
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
        if not os.path.isfile(path):
            continue
        convertible_by, outputs, _ = detect_file_outputs(path)
        name = os.path.basename(path)
        entries.append(
            FileEntry(
                # RomM's curated game name is the point of the overlay; fall
                # back to the filename when a ROM is unidentified.
                name=rom.get("name") or name,
                path=path,
                type="file",
                size=stat.st_size,
                extension=os.path.splitext(name)[1].lower() or None,
                convertible_by=convertible_by,
                outputs=outputs,
                verifiable_by=verifiable_tools(path),
            ),
        )
    # Deterministic ordering, independent of RomM's paging.
    entries.sort(key=lambda e: (e.name.lower(), e.path))
    return entries


# ----------------------------------------------------------------------
# metadata re-pin
# ----------------------------------------------------------------------


class RepinPlanRequest(BaseModel):
    """Rows to record before a batch of RomM conversions is submitted."""

    paths: list[str]
    mode: str
    output_dir: str | None = None


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

    if (spec.output_ext or "").lower() in DAT_SAFE_OUTPUT_EXTS:
        return {"recorded": 0, "skipped": len(payload.paths), "reason": "dat_safe"}

    # One catalog read for the whole batch, indexed by local path, instead of a
    # by-hash lookup per file.
    try:
        platform_roms = await run_in_threadpool(_roms_by_local_path, payload.paths)
    except RommError as exc:
        raise _romm_call(exc) from exc

    recorded = 0
    skipped = 0
    for path in payload.paths:
        if not is_within_configured_volumes(path):
            skipped += 1
            continue
        rom = platform_roms.get(os.path.abspath(path))
        if not rom:
            skipped += 1
            continue
        ids = {f: rom.get(f) for f in METADATA_ID_FIELDS if rom.get(f)}
        if not ids:
            # Unidentified in RomM already — nothing to carry across.
            skipped += 1
            continue
        output_path = tool.output_path(payload.mode, path, payload.output_dir)
        if await run_in_threadpool(_record_repin, rom, output_path, ids):
            recorded += 1
        else:
            skipped += 1
    return {"recorded": recorded, "skipped": skipped}


@router.post("/romm/repin")
async def romm_repin() -> dict:
    """Re-attach metadata to converted ROMs RomM has since rescanned.

    Idempotent by construction: a settled row is never revisited, and a row
    whose output RomM has not scanned yet simply stays pending for the next
    call.  Safe to invoke on every view load.
    """
    _require_configured()
    rows = await run_in_threadpool(_pending_rows, _MAX_SETTLE_PER_CALL)
    repinned = 0
    waiting = 0
    abandoned = 0
    failed = 0

    for row in rows:
        output_path, sha1, rom_id, ids, created_at = row
        try:
            exists = await bounded_path_check(os.path.isfile, output_path)
        except (asyncio.TimeoutError, OSError):
            # Unresponsive volume: treat as "not yet", never as abandoned.
            waiting += 1
            continue
        if not exists:
            if _is_stale(created_at):
                await run_in_threadpool(
                    _settle, rom_id, output_path, "abandoned",
                    "Output never appeared", None,
                )
                abandoned += 1
            else:
                waiting += 1
            continue

        try:
            if not sha1:
                # Hashing a multi-GB image is heavy disk work: take the same
                # lane the DAT matcher uses so a re-pin pass cannot compete
                # with a running conversion for the array.
                async with await workload_limiter.acquire("match"):
                    sha1 = await compute_file_sha1(output_path)
                # Cache it: a row may be retried many times before RomM scans.
                await run_in_threadpool(_store_sha1, output_path, sha1)

            match = await run_in_threadpool(romm_client.rom_by_sha1, sha1)
            if not match:
                waiting += 1
                continue
            await run_in_threadpool(
                romm_client.update_rom_metadata, match["id"], ids,
            )
            await run_in_threadpool(
                _settle, rom_id, output_path, "done", None, match["id"],
            )
            repinned += 1
        except RommError as exc:
            # Upstream trouble: leave the row pending and stop the pass rather
            # than burning the rest of the backlog against a sick RomM.
            logger.warning("romm: re-pin failed for %s: %s", output_path, exc)
            failed += 1
            break
        except OSError as exc:
            logger.warning("romm: could not hash %s: %s", output_path, exc)
            failed += 1
            continue

    return {
        "repinned": repinned,
        "waiting": waiting,
        "abandoned": abandoned,
        "failed": failed,
        "pending": await run_in_threadpool(_count_pending),
    }


def _is_stale(created_at: str) -> bool:
    if not created_at:
        return False
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(timezone.utc) - created > _ABANDON_AFTER


# ----------------------------------------------------------------------
# DB helpers (sync; always called through run_in_threadpool)
# ----------------------------------------------------------------------


def _session():
    if _db.SessionLocal is None:
        raise RuntimeError("db.SessionLocal not initialized")
    return _db.SessionLocal()


def _roms_by_local_path(paths: list[str]) -> dict[str, dict]:
    """Index the platforms covering *paths* by resolved local path.

    Reads the whole catalog for each platform involved rather than one lookup
    per file: a batch is normally a single platform, making this one request.
    """
    wanted = {os.path.abspath(p) for p in paths}
    index: dict[str, dict] = {}
    for platform in romm_client.platforms():
        pid = platform.get("id")
        if pid is None:
            continue
        for rom in romm_client.roms(pid):
            local = romm_client.local_path(rom)
            if local and local in wanted:
                index[local] = rom
        if len(index) == len(wanted):
            break
    return index


def _record_repin(rom: dict, output_path: str, ids: dict) -> bool:
    """Insert a pending row unless one already covers this output.

    The dedupe on ``output_path`` is what makes re-submitting the same batch
    harmless — the second submit updates the existing row instead of stacking
    another.
    """
    with _session() as session:
        existing = (
            session.query(_db.RommRepin)
            .filter(_db.RommRepin.output_path == output_path)
            .filter(_db.RommRepin.state == "pending")
            .one_or_none()
        )
        if existing is not None:
            existing.source_rom_id = rom.get("id")
            existing.metadata_ids = ids
            # The output is about to be rewritten, so any cached hash is stale.
            existing.output_sha1 = None
            session.commit()
            return True
        session.add(
            _db.RommRepin(
                source_rom_id=rom.get("id"),
                source_name=rom.get("name") or rom.get("fs_name"),
                output_path=output_path,
                metadata_ids=ids,
                state="pending",
                created_at=_utcnow_iso(),
            ),
        )
        session.commit()
        return True


def _pending_rows(limit: int) -> list[tuple]:
    with _session() as session:
        rows = (
            session.query(_db.RommRepin)
            .filter(_db.RommRepin.state == "pending")
            .order_by(_db.RommRepin.id)
            .limit(limit)
            .all()
        )
        # Detach into plain tuples: the session closes before the caller awaits.
        return [
            (r.output_path, r.output_sha1, r.source_rom_id, dict(r.metadata_ids or {}),
             r.created_at)
            for r in rows
        ]


def _store_sha1(output_path: str, sha1: str) -> None:
    with _session() as session:
        session.query(_db.RommRepin).filter(
            _db.RommRepin.output_path == output_path,
            _db.RommRepin.state == "pending",
        ).update({"output_sha1": sha1})
        session.commit()


def _settle(
    rom_id: int, output_path: str, state: str, detail: str | None, new_id: int | None,
) -> None:
    with _session() as session:
        values: dict = {"state": state, "settled_at": _utcnow_iso()}
        if detail:
            values["detail"] = detail
        elif new_id is not None:
            values["detail"] = f"Re-pinned to RomM rom {new_id}"
        session.query(_db.RommRepin).filter(
            _db.RommRepin.output_path == output_path,
            _db.RommRepin.state == "pending",
        ).update(values)
        session.commit()


def _count_pending() -> int:
    if _db.SessionLocal is None:
        return 0
    with _session() as session:
        return (
            session.query(_db.RommRepin)
            .filter(_db.RommRepin.state == "pending")
            .count()
        )
