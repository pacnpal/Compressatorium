"""SQLite-backed verification store.

Records which CHD/Dolphin image files have been successfully integrity-
verified.  Public API matches the legacy JSON store 1:1.
"""

from __future__ import annotations

from logging_setup import get_logger
import os
from datetime import datetime, timezone

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from services import db as _db

logger = get_logger("verification_store")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class VerificationStore:
    """Persists disc/image verification results across application restarts."""

    def __init__(
        self,
        store_path: str | None = None,
        *,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
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
            self._session_factory = None

    def _session(self) -> Session:
        if self._session_factory is not None:
            return self._session_factory()
        if _db.SessionLocal is None:
            raise RuntimeError(
                "VerificationStore: db.SessionLocal not initialized, call "
                "db.init_engine() before using the store.",
            )
        return _db.SessionLocal()

    @staticmethod
    def _normalize(path: str) -> str:
        return os.path.realpath(path)

    # ------------------------------------------------------------------

    def _mark_sync(
        self,
        chd_path: str,
        source_path: str | None,
        produced_meta: dict | None = None,
    ) -> None:
        normalized = self._normalize(chd_path)
        normalized_source = self._normalize(source_path) if source_path else None
        stmt = sqlite_insert(_db.Verification).values(
            chd_path=normalized,
            source_path=normalized_source,
            verified_at=_utcnow_iso(),
            produced_meta=produced_meta,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["chd_path"],
            set_={
                "source_path": stmt.excluded.source_path,
                "verified_at": stmt.excluded.verified_at,
                "produced_meta": stmt.excluded.produced_meta,
            },
        )
        with self._session() as session:
            session.execute(stmt)
            session.commit()

    async def mark_verified(
        self,
        chd_path: str,
        *,
        source_path: str | None = None,
        produced_meta: dict | None = None,
    ) -> None:
        await run_in_threadpool(
            self._mark_sync, chd_path, source_path, produced_meta,
        )

    def _clear_sync(self, chd_path: str) -> None:
        normalized = self._normalize(chd_path)
        with self._session() as session:
            session.execute(
                delete(_db.Verification).where(_db.Verification.chd_path == normalized)
            )
            session.commit()

    async def clear(self, chd_path: str) -> None:
        await run_in_threadpool(self._clear_sync, chd_path)

    def _move_sync(self, old_path: str, new_path: str) -> None:
        old_normalized = self._normalize(old_path)
        new_normalized = self._normalize(new_path)
        with self._session() as session:
            old = session.get(_db.Verification, old_normalized)
            if old is None:
                return
            # Preserve source_path/verified_at across the rename.
            source_path = old.source_path
            verified_at = old.verified_at
            session.delete(old)
            session.flush()
            # Upsert under the new key.
            existing = session.get(_db.Verification, new_normalized)
            if existing is not None:
                existing.source_path = source_path
                existing.verified_at = verified_at
            else:
                session.add(_db.Verification(
                    chd_path=new_normalized,
                    source_path=source_path,
                    verified_at=verified_at,
                ))
            session.commit()

    async def move(self, old_path: str, new_path: str) -> None:
        await run_in_threadpool(self._move_sync, old_path, new_path)

    # ------------------------------------------------------------------

    def _is_verified_sync(self, chd_path: str) -> bool:
        normalized = self._normalize(chd_path)
        with self._session() as session:
            return session.get(_db.Verification, normalized) is not None

    async def is_verified(self, chd_path: str) -> bool:
        return await run_in_threadpool(self._is_verified_sync, chd_path)

    def _get_record_sync(self, chd_path: str) -> dict[str, str | None] | None:
        normalized = self._normalize(chd_path)
        with self._session() as session:
            row = session.get(_db.Verification, normalized)
            if row is None:
                return None
            return {
                "chd_path": row.chd_path,
                "source_path": row.source_path,
                "verified_at": row.verified_at,
                "produced_meta": row.produced_meta,
            }

    async def get_record(self, chd_path: str) -> dict[str, str | None] | None:
        return await run_in_threadpool(self._get_record_sync, chd_path)

    async def all_records(self) -> list[dict[str, str | None]]:
        return await run_in_threadpool(self._all_records_sync)

    def _all_records_sync(self) -> list[dict[str, str | None]]:
        """Synchronous fetch of every verification record (internal seam for tests)."""
        with self._session() as session:
            rows = session.scalars(select(_db.Verification)).all()
            return [
                {
                    "chd_path": r.chd_path,
                    "source_path": r.source_path,
                    "verified_at": r.verified_at,
                }
                for r in rows
            ]

    def _prune_missing_sync(self) -> int:
        with self._session() as session:
            paths = session.scalars(select(_db.Verification.chd_path)).all()
            missing = [p for p in paths if not os.path.exists(p)]
            if not missing:
                return 0
            # Chunk to stay under SQLite's bind-parameter limit (default 999).
            chunk_size = 900
            for i in range(0, len(missing), chunk_size):
                batch = missing[i:i + chunk_size]
                session.execute(
                    delete(_db.Verification).where(_db.Verification.chd_path.in_(batch))
                )
            session.commit()
            return len(missing)

    async def prune_missing(self) -> int:
        return await run_in_threadpool(self._prune_missing_sync)


verification_store = VerificationStore()
