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

from logging_setup import get_logger
from services import db as _db
from services.romm import DAT_SAFE_OUTPUT_EXTS, METADATA_ID_FIELDS, romm_client
from services.tools import registry
from sqlalchemy.exc import IntegrityError

logger = get_logger("romm_repin")


# One UTC timestamp shape for every persisted date, owned by the module that
# owns the columns. Re-exported here because this module writes `created_at`
# and `settled_at` and its callers read them back.
utcnow_iso = _db.utcnow_iso


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


def roms_by_local_path(
    paths: list[str], platform_id: int | None = None,
) -> dict[str, dict]:
    """Index the ROMs covering *paths* by resolved local path.

    One catalog read for the whole batch rather than a lookup per file.

    *platform_id* is the platform the caller is actually browsing, and giving
    it turns this into a single request. Without it every platform's full
    paginated catalog is downloaded in turn until each path is found -- and if
    one selected path has gone stale, *all* of them are, so submitting a
    handful of ROMs from a large library could mean hundreds of serialised API
    calls before the batch is even queued. The scan remains the fallback for a
    caller that genuinely does not know (a path handed in from elsewhere).
    """
    # realpath, to match what `local_path()` returns. Comparing an abspath key
    # against a realpath value made a symlinked library silently miss every
    # lookup, so those ROMs lost their metadata without a word.
    wanted = {os.path.realpath(p) for p in paths}
    index: dict[str, dict] = {}

    def _absorb(pid: int) -> None:
        for rom in romm_client.roms(pid):
            local = romm_client.local_path(rom)
            if local and local in wanted:
                index[local] = rom

    if platform_id is not None:
        # Unconditionally, found everything or not. A path the caller listed
        # that this platform does not have is a stale selection, and falling
        # through to the scan on its account is the expensive case this
        # parameter exists to avoid -- one dead row would send a small batch
        # through every platform's paginated catalog.
        _absorb(int(platform_id))
        return index

    for platform in romm_client.platforms():
        pid = platform.get("id")
        if pid is None or pid == platform_id:
            continue
        _absorb(pid)
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


def path_fingerprint(path: str) -> str:
    """``"size:mtime_ns"`` for whatever is at *path*, or ``""`` if nothing is.

    Cheap enough to take on every recorded row (one stat), and precise enough
    for the only question asked of it: is the file here still the one that was
    here before the conversion ran? Not a content hash -- it never needs to be,
    because a converter that rewrites a path always changes both fields, and a
    false "changed" only costs one hash that then fails to match.
    """
    try:
        st = os.stat(path)
    except OSError:
        return ""
    return f"{st.st_size}:{st.st_mtime_ns}"


def _insert_pending(
    session, rom: dict, output_path: str, ids: dict, mode: str | None,
    pre_fingerprint: str | None,
) -> None:
    session.add(
        _db.RommRepin(
            source_rom_id=rom.get("id"),
            source_name=rom.get("name") or rom.get("fs_name"),
            output_path=output_path,
            pre_fingerprint=(
                path_fingerprint(output_path)
                if pre_fingerprint is None else pre_fingerprint
            ),
            mode=mode,
            metadata_ids=ids,
            state="pending",
            created_at=utcnow_iso(),
        ),
    )


def record(
    rom: dict, output_path: str, ids: dict, mode: str | None = None,
    pre_fingerprint: str | None = None,
) -> bool:
    """Insert a pending row unless one already covers this output.

    The dedupe on ``output_path`` is what makes re-submitting the same batch
    harmless -- the second submit updates the existing row instead of stacking
    another.

    Re-recording supersedes rather than mutates: the old pending row is retired
    and a fresh one inserted, so exactly one pending row per output survives
    (which is what makes re-submitting a batch harmless) while a settle pass
    already in flight cannot settle the new attempt on the old one's behalf.

    *pre_fingerprint* lets a caller supply the destination's state as of when
    it planned the conversion. The automation path must: it records after the
    queue accepts the batch, by which point a fast job on an idle queue may
    already have written the output, and stating it here would save the
    finished file as the "before" picture. Omitted, it is taken now, which is
    correct for the manual path -- that records before the batch is submitted.

    A partial unique index (``ux_romm_repin_pending_output``) is the actual
    guarantee that only one pending row exists, so a manual submit racing an
    automation sweep cannot stack two. Losing that race is not an error: retry
    once, superseding whichever row won.
    """
    with _session() as session:
        _supersede_pending(session, output_path)
        _insert_pending(session, rom, output_path, ids, mode, pre_fingerprint)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            _supersede_pending(session, output_path)
            _insert_pending(session, rom, output_path, ids, mode, pre_fingerprint)
            session.commit()
        return True


def produced_companions(output_path: str, mode: str | None) -> list[str]:
    """Files the owning tool actually produced for *output_path*, if not itself.

    A makeps3iso ``-s`` build only splits past 4 GB, and until it finishes
    nobody knows whether it will: under 4 GB it writes the bare output and the
    row settles normally, over it writes ``<name>.iso.0``, ``.1``, ... and no
    bare file at all. From the recorded path alone that second case is
    indistinguishable from a conversion that never ran, so the row would wait
    out ``repin_abandon_days`` and retire with a message that is simply untrue.

    Registry-driven rather than a suffix guess: ``companion_outputs`` is the
    tool's own answer, and reports the parts only when there is no primary
    file, which is exactly the case worth naming.
    """
    if not mode:
        return []
    try:
        tool = registry.for_mode(mode)
    except KeyError:
        return []
    try:
        return list(tool.companion_outputs(output_path, mode))
    except (OSError, KeyError, ValueError):
        return []


def cancel(output_paths: list[str]) -> int:
    """Retire the pending rows for *output_paths*. Returns how many were retired.

    The plan-then-submit pair is two requests, and the second one can fail
    (backpressure, a validation error, a closed tab). Without this the rows sit
    pending until they age out, counting against the badge and describing a
    conversion that is never going to happen.

    Retire, never delete: a settled row is the history of what was planned, and
    the partial unique index only constrains *pending* rows, so retiring frees
    the path for the next attempt.
    """
    if not output_paths:
        return 0
    with _session() as session:
        retired = (
            session.query(_db.RommRepin)
            .filter(
                _db.RommRepin.output_path.in_(list(output_paths)),
                _db.RommRepin.state == "pending",
            )
            .update(
                {
                    "state": "abandoned",
                    "detail": "The conversion was never submitted",
                    "settled_at": utcnow_iso(),
                },
                synchronize_session=False,
            )
        )
        session.commit()
        return int(retired)


def retire_all_pending(detail: str) -> int:
    """Retire every pending row. Returns how many were retired.

    For when the rows stop meaning anything: pointing Compressatorium at a
    different RomM instance (or a different library root) leaves rows holding
    the *old* instance's provider ids and an output digest taken from the old
    library. The settle pass would then hand those ids to whatever ROM the new
    instance happens to match that digest to -- stamping one library's identity
    onto another's game. There is no way to re-home them, so they are retired
    with the reason.

    Retire, never delete, like :func:`cancel`: the row is the record of what
    was planned, and only *pending* rows are constrained by the unique index.
    """
    with _session() as session:
        retired = (
            session.query(_db.RommRepin)
            .filter(_db.RommRepin.state == "pending")
            .update(
                {
                    "state": "abandoned",
                    "detail": detail,
                    "settled_at": utcnow_iso(),
                },
                synchronize_session=False,
            )
        )
        session.commit()
        return int(retired)


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
             r.created_at, r.id, r.pre_fingerprint or "", r.mode or "")
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
) -> bool:
    """Close out one re-pin row. Keyed by primary key, for the reason above.

    Returns whether a row was actually closed, so a caller cannot count a
    settle that hit a superseded row as a success.
    """
    with _session() as session:
        values: dict = {"state": state, "settled_at": utcnow_iso()}
        if detail:
            values["detail"] = detail
        elif new_id is not None:
            values["detail"] = f"Re-pinned to RomM rom {new_id}"
        updated = session.query(_db.RommRepin).filter(
            _db.RommRepin.id == row_id,
            _db.RommRepin.state == "pending",
        ).update(values)
        session.commit()
        return bool(updated)


def is_pending(row_id: int) -> bool:
    """Whether this re-pin attempt is still the live one for its output.

    Checked immediately before the outbound metadata write. A settle pass holds
    a detached row for as long as hashing a multi-GB output takes; if the same
    output is re-planned in that window the row is superseded, and pushing its
    provider ids to RomM afterwards would overwrite the *new* conversion's
    identity with the previous generation's.
    """
    with _session() as session:
        return (
            session.query(_db.RommRepin)
            .filter(_db.RommRepin.id == row_id, _db.RommRepin.state == "pending")
            .count()
            > 0
        )


def count_pending() -> int:
    if _db.SessionLocal is None:
        return 0
    with _session() as session:
        return (
            session.query(_db.RommRepin)
            .filter(_db.RommRepin.state == "pending")
            .count()
        )
