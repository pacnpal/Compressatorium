"""The RomM metadata re-pin queue.

RomM identifies a CHD by the SHA-1 embedded in its header and an archive by its
largest member, so converting to those keeps the Redump/No-Intro match. Every
other format we emit (RVZ/CSO/NSZ/WUX/Z3DS) is matched on the container's own
hash, which conversion necessarily changes -- the ROM goes unidentified and
loses the artwork and metadata the user curated.

This module records the provider ids *before* a conversion runs (they are only
readable while the source is still the file RomM knows about) so they can be
re-applied once RomM has rescanned.

It lives in ``services`` rather than in the route because **both** conversion
paths need it: the manual submit in ``routes.romm`` and the unattended sweep in
``services.romm_auto``. A service importing from a route would be backwards, and
duplicating the recording logic is exactly what let the automation path silently
opt out of the guarantee the manual path makes.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from logging_setup import get_logger
from services import db as _db
from services.romm import DAT_SAFE_OUTPUT_EXTS, METADATA_ID_FIELDS, romm_client
from sqlalchemy.exc import IntegrityError

logger = get_logger("romm_repin")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _session():
    if _db.SessionLocal is None:
        raise RuntimeError("db.SessionLocal not initialized")
    return _db.SessionLocal()


def mode_needs_repin(output_ext: str | None) -> bool:
    """True when this output format loses RomM's DAT match on conversion.

    The single gate both conversion paths ask, so neither can disagree about
    which formats need their metadata carried across.
    """
    return (output_ext or "").lower() not in DAT_SAFE_OUTPUT_EXTS


def metadata_ids(rom: dict) -> dict:
    """The provider ids worth carrying across, dropping the ones RomM has not set."""
    return {f: rom.get(f) for f in METADATA_ID_FIELDS if rom.get(f)}


def roms_by_local_path(paths: list[str]) -> dict[str, dict]:
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


def _supersede_pending(session, output_path: str) -> int:
    """Retire any pending row for *output_path*. Returns how many were retired.

    Deliberately *not* an in-place refresh. A settle pass detaches a row's id,
    provider ids and timestamp before hashing the output, which for a multi-GB
    image takes minutes. Mutating that row meanwhile would leave the settler
    working from its old snapshot and then marking the row done -- so the
    re-planned conversion silently ends up with no pending row at all and its
    metadata is never restored.

    Retiring the old row and inserting a fresh one gives the new attempt its own
    identity. The settler's ``store_sha1``/``settle`` both filter on
    ``state == "pending"``, so they no-op against the superseded row instead of
    settling the newcomer.
    """
    return (
        session.query(_db.RommRepin)
        .filter(
            _db.RommRepin.output_path == output_path,
            _db.RommRepin.state == "pending",
        )
        .update(
            {
                "state": "abandoned",
                "detail": "Superseded by a re-planned conversion",
                "settled_at": utcnow_iso(),
            },
        )
    )


def _insert_pending(session, rom: dict, output_path: str, ids: dict) -> None:
    session.add(
        _db.RommRepin(
            source_rom_id=rom.get("id"),
            source_name=rom.get("name") or rom.get("fs_name"),
            output_path=output_path,
            metadata_ids=ids,
            state="pending",
            created_at=utcnow_iso(),
        ),
    )


def record(rom: dict, output_path: str, ids: dict) -> bool:
    """Insert a pending row unless one already covers this output.

    The dedupe on ``output_path`` is what makes re-submitting the same batch
    harmless -- the second submit updates the existing row instead of stacking
    another.

    Re-recording supersedes rather than mutates: the old pending row is retired
    and a fresh one inserted, so exactly one pending row per output survives
    (which is what makes re-submitting a batch harmless) while a settle pass
    already in flight cannot settle the new attempt on the old one's behalf.

    A partial unique index (``ux_romm_repin_pending_output``) is the actual
    guarantee that only one pending row exists, so a manual submit racing an
    automation sweep cannot stack two. Losing that race is not an error: retry
    once, superseding whichever row won.
    """
    with _session() as session:
        _supersede_pending(session, output_path)
        _insert_pending(session, rom, output_path, ids)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            _supersede_pending(session, output_path)
            _insert_pending(session, rom, output_path, ids)
            session.commit()
        return True


def pending_rows(limit: int, *, after_id: int = 0) -> list[tuple]:
    """A page of pending rows, oldest first, starting after *after_id*.

    The cursor matters: rows whose job was cancelled (or whose output RomM never
    scans) stay pending until they age out, and always taking the oldest N would
    let such a prefix occupy the whole page forever, so conversions behind it
    would never be examined. The caller walks past them.
    """
    with _session() as session:
        rows = (
            session.query(_db.RommRepin)
            .filter(_db.RommRepin.state == "pending")
            .filter(_db.RommRepin.id > after_id)
            .order_by(_db.RommRepin.id)
            .limit(limit)
            .all()
        )
        # Detach into plain tuples: the session closes before the caller awaits.
        return [
            (r.output_path, r.output_sha1, r.source_rom_id, dict(r.metadata_ids or {}),
             r.created_at, r.id)
            for r in rows
        ]


def store_sha1(row_id: int, sha1: str) -> None:
    """Cache the output's hash on the row the settle pass is working on.

    Keyed by primary key, not by ``output_path``: a row can be settled and the
    same path re-recorded by a later conversion while a pass is mid-flight, and
    matching on the path would then stamp the *new* row with the old file's
    digest -- which, because the hash is cached, that ROM could never recover
    from.
    """
    with _session() as session:
        session.query(_db.RommRepin).filter(
            _db.RommRepin.id == row_id,
            _db.RommRepin.state == "pending",
        ).update({"output_sha1": sha1})
        session.commit()


def settle(
    row_id: int, state: str, detail: str | None = None, new_id: int | None = None,
) -> None:
    """Close out one re-pin row. Keyed by primary key, for the reason above."""
    with _session() as session:
        values: dict = {"state": state, "settled_at": utcnow_iso()}
        if detail:
            values["detail"] = detail
        elif new_id is not None:
            values["detail"] = f"Re-pinned to RomM rom {new_id}"
        session.query(_db.RommRepin).filter(
            _db.RommRepin.id == row_id,
            _db.RommRepin.state == "pending",
        ).update(values)
        session.commit()


def count_pending() -> int:
    if _db.SessionLocal is None:
        return 0
    with _session() as session:
        return (
            session.query(_db.RommRepin)
            .filter(_db.RommRepin.state == "pending")
            .count()
        )
