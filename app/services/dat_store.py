"""SQLite-backed DAT store + match cache.

Public API is identical to the legacy JSON-backed implementation so that
route code and tests require no changes to their call sites.  Internally
every method dispatches to a short SQLAlchemy query executed in a
thread pool (matches the existing ``run_in_threadpool`` pattern).
"""

from __future__ import annotations

from logging_setup import get_logger
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import and_, delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from services import db as _db
from services.dat_parser import parse_dat

logger = get_logger("dat_store")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _remote_hit_clause():
    """SQL for "this row is a remote (Hasheous) hit"."""
    return and_(
        _db.DATMatch.matched.is_(True),
        func.coalesce(_db.DATMatch.payload["source"].as_string(), "") == "hasheous",
    )


class DATStore:
    """Persists imported DAT files and hash lookup index."""

    def __init__(
        self,
        store_path: str | None = None,
        *,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        """Construct a store.

        * ``session_factory``, if provided, use this ``sessionmaker``
          directly.  Preferred in tests that build an isolated engine.
        * ``store_path``, legacy constructor argument.  If given,
          treat it as a SQLite database file and build a private
          engine for this store (isolated from the module-level DB).
          This preserves the test fixture pattern
          ``DATStore(store_path=tmp_path / "dat_store.json")``, the
          path is still unique-per-test, it just happens to be a
          SQLite file now.
        * Neither, use the process-wide ``db.SessionLocal``.
        """
        if session_factory is not None:
            self._session_factory = session_factory
            self._own_engine: Engine | None = None
        elif store_path is not None:
            self._own_engine = _db.make_engine(str(store_path))
            _db.Base.metadata.create_all(self._own_engine)
            self._session_factory = sessionmaker(
                bind=self._own_engine, expire_on_commit=False, future=True,
            )
        else:
            self._own_engine = None
            self._session_factory = None  # resolved lazily from db.SessionLocal
        # Staging area for import_dat_no_persist(), committed atomically by persist().
        self._pending_imports: list[tuple[dict, list[dict[str, Any]]]] = []
        # Lock serialises concurrent append (import_dat_no_persist) / read+clear
        # (_persist_sync) / clear (discard_pending) calls from different threads.
        self._pending_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Session plumbing
    # ------------------------------------------------------------------

    def _session(self) -> Session:
        if self._session_factory is not None:
            return self._session_factory()
        if _db.SessionLocal is None:
            raise RuntimeError(
                "DATStore: db.SessionLocal not initialized, call "
                "db.init_engine() before using the store.",
            )
        return _db.SessionLocal()

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    def _import_dat_sync(self, source: str) -> tuple[dict, dict]:
        """Parse *source* and insert all entries into the DB in one transaction."""
        header, entries = parse_dat(source)

        dat_id = str(uuid.uuid4())[:8]
        now = _utcnow_iso()

        dat_info = {
            "id": dat_id,
            "name": header.get("name", "Unknown DAT"),
            "description": header.get("description", ""),
            "version": header.get("version", ""),
            "imported_at": now,
            "file_count": len(entries),
        }

        added = 0
        with self._session() as session:
            dat_row = _db.DAT(
                id=dat_id,
                name=dat_info["name"],
                description=dat_info["description"],
                version=dat_info["version"],
                imported_at=now,
                file_count=len(entries),
            )
            session.add(dat_row)
            # bulk_insert_mappings bypasses the unit-of-work, so flush
            # the DAT row first or the FK from dat_hashes will fail.
            session.flush()

            hash_rows: list[dict[str, Any]] = []
            for entry in entries:
                if entry.get("sha1"):
                    hash_rows.append({
                        "hash": entry["sha1"],
                        "hash_type": "sha1",
                        "dat_id": dat_id,
                        "game_name": entry["game_name"],
                        "rom_name": entry["rom_name"],
                        "size": entry["size"],
                    })
                    added += 1
                if entry.get("md5"):
                    hash_rows.append({
                        "hash": entry["md5"],
                        "hash_type": "md5",
                        "dat_id": dat_id,
                        "game_name": entry["game_name"],
                        "rom_name": entry["rom_name"],
                        "size": entry["size"],
                    })
                    added += 1

            if hash_rows:
                # Use INSERT OR REPLACE (ON CONFLICT DO UPDATE) so that
                # importing an updated DAT whose hashes overlap with an
                # existing DAT set never raises a UNIQUE constraint error.
                chunk_size = 900
                for i in range(0, len(hash_rows), chunk_size):
                    chunk = hash_rows[i:i + chunk_size]
                    stmt = sqlite_insert(_db.DATHash).values(chunk)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["hash", "hash_type"],
                        set_={
                            "dat_id": stmt.excluded.dat_id,
                            "game_name": stmt.excluded.game_name,
                            "rom_name": stmt.excluded.rom_name,
                            "size": stmt.excluded.size,
                        },
                    )
                    session.execute(stmt)

            # Importing a new DAT invalidates the match cache (a
            # previously-"unmatched" file may now match, or a previously
            # matched file may now match against a different DAT) -- except
            # for remote hits, which the local DATs did not produce.
            self._invalidate_dat_derived_matches(session)

            session.commit()

        result = {
            "id": dat_id,
            "name": dat_info["name"],
            "version": dat_info["version"],
            "file_count": len(entries),
            "hashes_added": added,
            "message": f"Imported {len(entries)} entries from {dat_info['name']}",
        }
        return dat_info, result

    async def import_dat(self, source: str) -> dict:
        """Parse and import a DAT file (path or XML string)."""
        _dat_info, result = await run_in_threadpool(self._import_dat_sync, source)
        return result

    async def import_dat_no_persist(self, source: str) -> dict:
        """Stage a DAT in memory; call :meth:`persist` to atomically commit all staged DATs."""
        def _stage() -> tuple[dict, dict]:
            header, entries = parse_dat(source)
            dat_id = str(uuid.uuid4())[:8]
            now = _utcnow_iso()
            dat_info = {
                "id": dat_id,
                "name": header.get("name", "Unknown DAT"),
                "description": header.get("description", ""),
                "version": header.get("version", ""),
                "imported_at": now,
                "file_count": len(entries),
            }
            added = 0
            hash_rows: list[dict[str, Any]] = []
            for entry in entries:
                if entry.get("sha1"):
                    hash_rows.append({
                        "hash": entry["sha1"],
                        "hash_type": "sha1",
                        "dat_id": dat_id,
                        "game_name": entry["game_name"],
                        "rom_name": entry["rom_name"],
                        "size": entry["size"],
                    })
                    added += 1
                if entry.get("md5"):
                    hash_rows.append({
                        "hash": entry["md5"],
                        "hash_type": "md5",
                        "dat_id": dat_id,
                        "game_name": entry["game_name"],
                        "rom_name": entry["rom_name"],
                        "size": entry["size"],
                    })
                    added += 1
            result = {
                "id": dat_id,
                "name": dat_info["name"],
                "version": dat_info["version"],
                "file_count": len(entries),
                "hashes_added": added,
                "message": f"Imported {len(entries)} entries from {dat_info['name']}",
            }
            with self._pending_lock:
                self._pending_imports.append((dat_info, hash_rows))
            return dat_info, result

        _dat_info, result = await run_in_threadpool(_stage)
        return result

    def _persist_sync(self) -> None:
        """Commit all staged DATs in a single transaction and clear the staging area."""
        with self._pending_lock:
            pending = list(self._pending_imports)
            self._pending_imports = []
        if not pending:
            return
        with self._session() as session:
            for dat_info, hash_rows in pending:
                dat_row = _db.DAT(
                    id=dat_info["id"],
                    name=dat_info["name"],
                    description=dat_info["description"],
                    version=dat_info["version"],
                    imported_at=dat_info["imported_at"],
                    file_count=dat_info["file_count"],
                )
                session.add(dat_row)
                session.flush()
                if hash_rows:
                    # Use INSERT OR REPLACE so that staging a new DAT set
                    # while old DATs are still present never raises a UNIQUE
                    # constraint error on overlapping (hash, hash_type) pairs.
                    chunk_size = 900
                    for i in range(0, len(hash_rows), chunk_size):
                        chunk = hash_rows[i:i + chunk_size]
                        stmt = sqlite_insert(_db.DATHash).values(chunk)
                        stmt = stmt.on_conflict_do_update(
                            index_elements=["hash", "hash_type"],
                            set_={
                                "dat_id": stmt.excluded.dat_id,
                                "game_name": stmt.excluded.game_name,
                                "rom_name": stmt.excluded.rom_name,
                                "size": stmt.excluded.size,
                            },
                        )
                        session.execute(stmt)
            self._invalidate_dat_derived_matches(session)
            session.commit()

    async def persist(self) -> None:
        """Commit all DATs staged by :meth:`import_dat_no_persist` in a single transaction."""
        await run_in_threadpool(self._persist_sync)

    async def discard_pending(self) -> None:
        """Discard all staged DATs without writing them to the database."""
        with self._pending_lock:
            self._pending_imports = []

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def _delete_dat_sync(self, dat_id: str) -> bool:
        with self._session() as session:
            row = session.get(_db.DAT, dat_id)
            if row is None:
                return False
            # Matches don't cascade (FK is SET NULL), drop them
            # explicitly so the behaviour matches the legacy store.
            session.execute(delete(_db.DATMatch).where(_db.DATMatch.dat_id == dat_id))
            # Hashes cascade via FK ON DELETE CASCADE.
            session.delete(row)
            session.commit()
        return True

    async def delete_dat(self, dat_id: str) -> bool:
        return await run_in_threadpool(self._delete_dat_sync, dat_id)

    def _delete_dats_bulk_sync(self, dat_ids: list[str]) -> int:
        if not dat_ids:
            return 0
        # Chunk to stay under SQLite's bind-parameter limit (default 999).
        chunk_size = 900
        with self._session() as session:
            existing: list[str] = []
            for i in range(0, len(dat_ids), chunk_size):
                chunk = dat_ids[i:i + chunk_size]
                existing.extend(session.scalars(
                    select(_db.DAT.id).where(_db.DAT.id.in_(chunk))
                ).all())
            if not existing:
                return 0
            for i in range(0, len(existing), chunk_size):
                chunk = existing[i:i + chunk_size]
                session.execute(delete(_db.DATMatch).where(_db.DATMatch.dat_id.in_(chunk)))
                session.execute(delete(_db.DAT).where(_db.DAT.id.in_(chunk)))
            session.commit()
            return len(existing)

    async def delete_dats_bulk(self, dat_ids: list[str]) -> int:
        return await run_in_threadpool(self._delete_dats_bulk_sync, list(dat_ids))

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def list_dats(self) -> list[dict]:
        with self._session() as session:
            rows = session.scalars(select(_db.DAT)).all()
            return [
                {
                    "id": r.id,
                    "name": r.name,
                    "description": r.description,
                    "version": r.version,
                    "imported_at": r.imported_at,
                    "file_count": r.file_count,
                }
                for r in rows
            ]

    def get_dat_name(self, dat_id: str) -> str:
        with self._session() as session:
            row = session.get(_db.DAT, dat_id)
            return row.name if row is not None else "Unknown"

    def _lookup_hash(self, hex_hash: str, hash_type: str) -> dict | None:
        with self._session() as session:
            row = session.execute(
                select(_db.DATHash).where(
                    _db.DATHash.hash == hex_hash.lower(),
                    _db.DATHash.hash_type == hash_type,
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "dat_id": row.dat_id,
                "game_name": row.game_name,
                "rom_name": row.rom_name,
                "size": row.size,
            }

    def lookup_sha1(self, sha1: str) -> dict | None:
        return self._lookup_hash(sha1, "sha1")

    def lookup_md5(self, md5: str) -> dict | None:
        return self._lookup_hash(md5, "md5")

    # ------------------------------------------------------------------
    # Match cache
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(path: str) -> str:
        return os.path.normpath(path)

    def get_match(self, file_path: str) -> dict | None:
        normalized = self._normalize(file_path)
        with self._session() as session:
            row = session.get(_db.DATMatch, normalized)
            return dict(row.payload) if row is not None else None

    @staticmethod
    def _would_downgrade_remote_hit(existing, match: dict) -> bool:
        """True when writing *match* would replace a remote hit with a non-answer.

        "Non-answer" is the operative word: an unmatched result only supersedes
        a remote hit if it actually knows better. That means either the remote
        source was consulted this time (``checked_remote``), or the recompute
        can *prove* the file changed underneath the cached row.

        The proof compares like with like. A CHD hit is stored against its
        embedded hash (``chd_sha1``), while a rescan recomputes the container
        ``file_sha1`` -- different domains that differ for an unchanged file,
        so only a stored ``file_sha1`` match can be compared. Without this the
        guard kept a stale badge on a replaced file forever, since cached hits
        are always accepted on read.
        """
        if existing is None or not existing.matched:
            return False
        if (existing.payload or {}).get("source") != "hasheous":
            return False
        if match.get("matched"):
            return False
        if match.get("checked_remote"):
            return False
        return not DATStore._proves_content_changed(existing, match)

    @staticmethod
    def _proves_content_changed(existing, match: dict) -> bool:
        """True only when the recomputed hash demonstrably contradicts the row."""
        new_hash = match.get("file_hash")
        if not new_hash or existing.match_type != "file_sha1":
            return False
        return bool(existing.file_hash) and existing.file_hash != new_hash

    # A row is a *remote* hit only if it says so. This is the one place the
    # question is answered, and it is answered from the payload the writer
    # recorded -- never inferred from ``dat_id IS NULL``. A local hit whose DAT
    # was deleted between the match and the write has its dangling FK nulled by
    # ``_upsert_match_sync``, so the FK proxy called that row remote, preserved
    # it through every later import, and went on serving an identity from a DAT
    # the operator had removed. ``coalesce`` because SQL NULL is not False: a
    # payload with no ``source`` must compare unequal, not unknown, or the
    # NOT below would spare exactly the rows it is meant to drop.
    def _invalidate_dat_derived_matches(self, session) -> None:
        """Drop the part of the match cache the local DATs are responsible for.

        A new/refreshed DAT set can change any verdict it produced, and can
        turn a previous miss into a hit -- so DAT-derived hits and cached
        misses both go. A *remote* (Hasheous) hit owes nothing to the local
        DATs, so it stays: deleting it stranded the badge permanently once the
        provider was switched off, since the recompute would then miss locally
        and cache "unmatched".

        Shared by both import paths on purpose. It lived only in
        ``_persist_sync`` at first, which left ``_import_dat_sync`` (the
        user-uploaded-DAT path) still wiping everything.
        """
        session.execute(delete(_db.DATMatch).where(~_remote_hit_clause()))

    @staticmethod
    def _local_index_now_covers(session, match: dict) -> bool:
        """True when a remote hit's hash has since been learned locally.

        Read inside the writing transaction, which is the point: the route
        decides local-first, but the decision and the write are separate
        operations, and a DAT import can commit in between -- for a path with
        no prior row the import's rematch snapshot cannot cover it either, so
        the remote hit would be written *after* invalidation and then served
        unconditionally. Checking here closes that window at the boundary where
        the write actually happens.

        Skipping the write (rather than rewriting the payload) keeps DAT-record
        shape out of the store: the path is simply left uncached, and the next
        match recomputes it local-first. That costs nothing extra remotely,
        because this only fires when the local index *does* know the hash.
        """
        if not match.get("matched") or match.get("source") != "hasheous":
            return False
        sha1 = match.get("file_hash")
        if not sha1:
            return False
        return session.get(_db.DATHash, (sha1, "sha1")) is not None

    def _upsert_match_sync(self, file_path: str, match: dict) -> None:
        normalized = self._normalize(file_path)
        with self._session() as session:
            dat_id = match.get("dat_id")
            # Guard against dat_id referencing a non-existent DAT (e.g.,
            # deleted between match and persist).  Null it out rather
            # than violate the FK.
            if dat_id is not None:
                if session.get(_db.DAT, dat_id) is None:
                    dat_id = None
            if self._local_index_now_covers(session, match):
                # A DAT covering this hash landed while the match was running.
                # Leave the path uncached so the next one resolves it locally.
                return
            payload = dict(match)
            existing = session.get(_db.DATMatch, normalized)
            if self._would_downgrade_remote_hit(existing, match):
                # The post-sync rematch re-runs every previously-matched path.
                # With Hasheous off, a path whose only identity came from the
                # remote source misses locally and would be written back as
                # "unmatched", undoing the preservation above -- so the
                # selective invalidation alone was not enough end to end.
                #
                # Refused here rather than at each caller: the batch route, the
                # background job and the scan all write through this one place.
                # A file that genuinely changed is removed by
                # routes.dat.drop_if_content_changed(), which deletes rather
                # than downgrading, and a miss recorded while the remote source
                # *was* consulted carries `checked_remote` and is allowed
                # through.
                return
            if existing is not None:
                existing.matched = bool(match.get("matched", False))
                existing.dat_id = dat_id
                existing.game_name = match.get("game_name")
                existing.rom_name = match.get("rom_name")
                existing.match_type = match.get("match_type")
                existing.file_hash = match.get("file_hash")
                existing.payload = payload
            else:
                session.add(_db.DATMatch(
                    path=normalized,
                    matched=bool(match.get("matched", False)),
                    dat_id=dat_id,
                    game_name=match.get("game_name"),
                    rom_name=match.get("rom_name"),
                    match_type=match.get("match_type"),
                    file_hash=match.get("file_hash"),
                    payload=payload,
                ))
            session.commit()

    async def set_match(self, file_path: str, match: dict) -> None:
        await run_in_threadpool(self._upsert_match_sync, file_path, match)

    def _delete_match_sync(self, file_path: str) -> bool:
        normalized = self._normalize(file_path)
        with self._session() as session:
            row = session.get(_db.DATMatch, normalized)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    async def delete_match(self, file_path: str) -> bool:
        """Remove the cached match row for ``file_path`` (no-op if absent).

        Used when a forced recompute produces a non-cacheable result so a
        stale prior match doesn't keep showing in ``/dat/matches/lookup``.
        Returns True when a row was removed.
        """
        return await run_in_threadpool(self._delete_match_sync, file_path)

    def _set_matches_batch_sync(self, matches: dict[str, dict]) -> None:
        if not matches:
            return
        normalized_matches = {
            self._normalize(raw_path): match
            for raw_path, match in matches.items()
        }

        # Resolve valid dat_ids once and prefetch existing match rows for the
        # whole batch to avoid an N+1 session.get() loop.
        with self._session() as session:
            valid_dat_ids = set(session.scalars(select(_db.DAT.id)).all())
            # Prefetch existing rows in chunks to stay under SQLite's
            # 999 bind-parameter limit.
            chunk_size = 900
            norm_keys = list(normalized_matches.keys())
            existing_matches: dict[str, _db.DATMatch] = {}
            for i in range(0, len(norm_keys), chunk_size):
                chunk = norm_keys[i:i + chunk_size]
                stmt = select(_db.DATMatch).where(_db.DATMatch.path.in_(chunk))
                for row in session.scalars(stmt).all():
                    existing_matches[row.path] = row

            for normalized, match in normalized_matches.items():
                dat_id = match.get("dat_id")
                if dat_id is not None and dat_id not in valid_dat_ids:
                    dat_id = None
                if self._local_index_now_covers(session, match):
                    # Same rule as _upsert_match_sync.
                    continue
                payload = dict(match)
                existing = existing_matches.get(normalized)
                if self._would_downgrade_remote_hit(existing, match):
                    # Same rule as _upsert_match_sync. This path updated rows
                    # unconditionally, so a concurrent batch match could still
                    # erase a remote hit the single-path writer would refuse to
                    # touch -- and the guard's comment claimed the batch route
                    # wrote through it, which was simply untrue.
                    continue
                if existing is not None:
                    existing.matched = bool(match.get("matched", False))
                    existing.dat_id = dat_id
                    existing.game_name = match.get("game_name")
                    existing.rom_name = match.get("rom_name")
                    existing.match_type = match.get("match_type")
                    existing.file_hash = match.get("file_hash")
                    existing.payload = payload
                else:
                    session.add(_db.DATMatch(
                        path=normalized,
                        matched=bool(match.get("matched", False)),
                        dat_id=dat_id,
                        game_name=match.get("game_name"),
                        rom_name=match.get("rom_name"),
                        match_type=match.get("match_type"),
                        file_hash=match.get("file_hash"),
                        payload=payload,
                    ))
            session.commit()

    async def set_matches_batch(self, matches: dict[str, dict]) -> None:
        await run_in_threadpool(self._set_matches_batch_sync, dict(matches))

    def get_matches_batch(self, file_paths: list[str]) -> dict[str, dict | None]:
        if not file_paths:
            return {}
        normalized_map = {p: self._normalize(p) for p in file_paths}
        unique_norms = list(set(normalized_map.values()))
        # Chunk to stay under SQLite's bind-parameter limit (default 999).
        chunk_size = 900
        by_norm: dict[str, dict] = {}
        with self._session() as session:
            for i in range(0, len(unique_norms), chunk_size):
                chunk = unique_norms[i:i + chunk_size]
                rows = session.scalars(
                    select(_db.DATMatch).where(_db.DATMatch.path.in_(chunk))
                ).all()
                by_norm.update({r.path: dict(r.payload) for r in rows})
        return {orig: by_norm.get(norm) for orig, norm in normalized_map.items()}

    # ------------------------------------------------------------------
    # Stats / housekeeping
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        with self._session() as session:
            total_dats = session.query(_db.DAT).count()
            total_sha1 = session.query(_db.DATHash).filter(
                _db.DATHash.hash_type == "sha1"
            ).count()
            total_md5 = session.query(_db.DATHash).filter(
                _db.DATHash.hash_type == "md5"
            ).count()
            matched = session.query(_db.DATMatch).filter(
                _db.DATMatch.matched.is_(True)
            ).count()
            unmatched = session.query(_db.DATMatch).filter(
                _db.DATMatch.matched.is_(False)
            ).count()
            return {
                "total_dats": total_dats,
                "total_sha1_hashes": total_sha1,
                "total_md5_hashes": total_md5,
                "total_matches": matched,
                "total_unmatched": unmatched,
                "total_scanned": matched + unmatched,
            }

    def has_dats(self) -> bool:
        with self._session() as session:
            return session.query(_db.DAT).limit(1).first() is not None

    def has_stale_dats(self) -> bool:
        """Return True if any DAT row has file_count=0.

        Flags pre-parser-upgrade imports that wrote rows with zero entries
        (see PR #49 / self-heal in dat_sync._do_sync). Cheap enough for
        startup, single-row existence check.
        """
        with self._session() as session:
            return session.execute(
                select(_db.DAT.id).where(_db.DAT.file_count == 0).limit(1)
            ).first() is not None

    def list_match_paths(self) -> list[str]:
        """Return every path currently cached in DATMatch.

        Used by dat_sync._do_sync to snapshot scanned paths before the
        post-sync persist() wipes the cache, so a background rematch job
        can re-hash those files against the freshly-imported DAT set.

        Order is undefined; callers that need stable ordering must sort.
        """
        with self._session() as session:
            return list(session.scalars(select(_db.DATMatch.path)).all())

    def _prune_missing_sync(self) -> int:
        with self._session() as session:
            paths = session.scalars(select(_db.DATMatch.path)).all()
            missing = [p for p in paths if not os.path.exists(p)]
            if not missing:
                return 0
            # Chunk to stay under SQLite's bind-parameter limit (default 999).
            chunk_size = 900
            for i in range(0, len(missing), chunk_size):
                chunk = missing[i:i + chunk_size]
                session.execute(delete(_db.DATMatch).where(_db.DATMatch.path.in_(chunk)))
            session.commit()
            return len(missing)

    async def prune_missing(self) -> int:
        return await run_in_threadpool(self._prune_missing_sync)


# ---------------------------------------------------------------------------
# Module-level singleton.  Lazy, only resolves ``db.SessionLocal`` on first
# method call, so import order is irrelevant.
# ---------------------------------------------------------------------------

dat_store = DATStore()
