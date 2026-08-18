"""SQLite persistence layer for Compressatorium.

This module consolidates the four legacy JSON stores (``dat_store.json``,
``chd_metadata.json``, ``verified_chds.json``, ``dat_sync.json``) into a
single SQLite database (``compressatorium.db`` by default).

Design notes
------------
* Uses SQLAlchemy 2.0 **sync** sessions, every store call goes through
  ``run_in_threadpool`` already, so sync sessions drop in without any
  async refactor of callers.
* WAL journal mode, ``synchronous=NORMAL``, ``foreign_keys=ON``,
  30-second busy timeout. Tuned for this workload (occasional writes,
  frequent reads) and safe under the single-worker uvicorn deployment.
* One engine per process. ``SessionLocal`` is a module-level
  ``sessionmaker`` bound to that engine.  Stores may be re-pointed at a
  different engine in tests by assigning to their module-level
  ``SessionLocal`` attribute (see ``tests/conftest.py``).
* JSON-to-SQLite migration runs at startup via :func:`init_and_migrate`.
  The source JSON file is **never deleted**, on success it is renamed
  to ``<name>.migrated.bak`` so a user can always roll back.
"""

from __future__ import annotations

import json
from logging_setup import get_logger
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    create_engine,
    event,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

logger = get_logger("db")


# ---------------------------------------------------------------------------
# ORM models
# ---------------------------------------------------------------------------


def utcnow_iso() -> str:
    """UTC timestamp in the ``...Z`` form every string date column here uses.

    Public and shared: the stores that write these columns must agree on the
    shape, or a reader parsing one module's output against another's assumption
    silently mis-sorts or fails to compare.
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Base(DeclarativeBase):
    pass


class DAT(Base):
    __tablename__ = "dats"
    id = Column(String(16), primary_key=True)
    name = Column(String, nullable=False, default="")
    description = Column(String, nullable=False, default="")
    version = Column(String, nullable=False, default="")
    imported_at = Column(String, nullable=False, default="")
    file_count = Column(Integer, nullable=False, default=0)


class DATHash(Base):
    __tablename__ = "dat_hashes"
    # Composite PK lets the same hex value live under both "sha1" and
    # "md5" types without collision.
    hash = Column(String(64), primary_key=True)
    hash_type = Column(String(8), primary_key=True)  # 'sha1' | 'md5'
    dat_id = Column(
        String(16),
        ForeignKey("dats.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    game_name = Column(String, nullable=False, default="")
    rom_name = Column(String, nullable=False, default="")
    size = Column(Integer, nullable=False, default=0)


class DATMatch(Base):
    __tablename__ = "dat_matches"
    path = Column(String, primary_key=True)
    matched = Column(Boolean, nullable=False, default=False)
    dat_id = Column(
        String(16),
        ForeignKey("dats.id", ondelete="SET NULL"),
        nullable=True,
    )
    game_name = Column(String, nullable=True)
    rom_name = Column(String, nullable=True)
    match_type = Column(String, nullable=True)
    file_hash = Column(String, nullable=True)
    payload = Column(JSON, nullable=False, default=dict)


class DATSyncState(Base):
    __tablename__ = "dat_sync_state"
    # Singleton row, always id=1.
    id = Column(Integer, primary_key=True, default=1)
    last_sync_tag = Column(String, nullable=True)
    last_sync_at = Column(String, nullable=True)
    last_sync_files = Column(Integer, nullable=False, default=0)


class RommRepin(Base):
    """One pending metadata re-pin for a converted RomM ROM.

    RomM matches a CHD by the raw+meta SHA-1 embedded in its v5 header, and an
    archive by its largest member, so those keep their Redump/No-Intro identity
    across a conversion.  Every other format we emit (RVZ/CSO/NSZ/WUX/Z3DS) is
    matched on the container's own hash, which conversion necessarily changes --
    the ROM goes unidentified until something re-attaches the metadata.

    A row is written *before* the conversion (the source's provider ids are only
    readable while the source is still the file RomM knows about) and settled
    after RomM has rescanned.  It is a queue, not a preference, which is why it
    is a table rather than a ``preferences`` blob: the settle step may be minutes
    or hours later, across restarts, and the read-modify-write of a JSON blob
    would race the next conversion.

    ``output_sha1`` is the join key.  RomM computes the converted file's own
    SHA-1 on scan and ``file_hasher.compute_file_sha1`` computes the same one, so
    ``GET /api/roms/by-hash`` identifies the new record exactly -- no filename
    guessing, and correct even when the user renamed it in between.  It is
    cached here because hashing a multi-GB image is the expensive half of the
    settle pass and a row may be retried many times before RomM rescans.
    """

    __tablename__ = "romm_repin"
    __table_args__ = (
        Index("ix_romm_repin_state", "state"),
        Index("ix_romm_repin_output_path", "output_path"),
        # At most one *pending* row per output. Partial, because settled rows
        # are history and the same path is legitimately converted again later.
        # Enforced in the schema rather than only by a read-then-insert, so two
        # callers recording the same output concurrently (a manual submit
        # racing a sweep) cannot both win the check and stack two pending rows
        # that would each try to re-pin the same file.
        Index(
            "ux_romm_repin_pending_output",
            "output_path",
            unique=True,
            sqlite_where=text("state = 'pending'"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # RomM's id for the *source* ROM, captured before conversion.
    source_rom_id = Column(Integer, nullable=False)
    source_name = Column(String, nullable=True)
    # Absolute local path of the conversion output this row is waiting on.
    output_path = Column(String, nullable=False)
    output_sha1 = Column(String, nullable=True)
    # {"igdb_id": 123, "moby_id": ..., ...} -- only the providers that were set.
    metadata_ids = Column(JSON, nullable=False, default=dict)
    # "pending" -> "done" | "abandoned"
    state = Column(String, nullable=False, default="pending")
    detail = Column(String, nullable=True)
    # Callable default, not "": the abandonment clock reads this, and an empty
    # string parses as "no age", so a row inserted without one could never be
    # retired however long its output failed to appear.
    created_at = Column(String, nullable=False, default=utcnow_iso)
    settled_at = Column(String, nullable=True)


class CHDMetadata(Base):
    __tablename__ = "chd_metadata"
    chd_path = Column(String, primary_key=True)
    metadata_json = Column("metadata", JSON, nullable=True)
    media_type = Column(String, nullable=True)
    mtime = Column(Float, nullable=True)
    cached_at = Column(String, nullable=True)
    disc_id_checked = Column(Boolean, nullable=False, default=False)
    disc_id_checked_mtime = Column(Float, nullable=True)
    game_id = Column(String, nullable=True)
    title = Column(String, nullable=True)


class Verification(Base):
    __tablename__ = "verifications"
    chd_path = Column(String, primary_key=True)
    source_path = Column(String, nullable=True)
    verified_at = Column(String, nullable=False, default="")
    # Optional snapshot of what produced this verified artifact: the conversion
    # mode + output-shaping settings and a stat fingerprint of the complete
    # source set and the output. Populated only by a delete-on-verify
    # conversion (a manual /info verify leaves it NULL). Lets the job-start
    # re-run fast path prove an on-disk output is the exact result of the
    # current request before completing as a no-op. See
    # docs/DESIGN_tool_plugin_architecture.md §3.3.6.
    produced_meta = Column(JSON, nullable=True)


class Preference(Base):
    """Generic key/value store for app preferences (UI layout, etc.).

    A single row per key holds an arbitrary JSON blob.  Kept generic so
    future preferences reuse the same table instead of growing a column
    per setting.
    """

    __tablename__ = "preferences"
    key = Column(String, primary_key=True)
    value = Column(JSON, nullable=False, default=dict)
    updated_at = Column(String, nullable=False, default="")


# Additional index on dat_matches.dat_id so cascade-on-DAT-delete is
# O(rows-in-that-DAT) rather than a full table scan.
Index("ix_dat_matches_dat_id", DATMatch.dat_id)


# ---------------------------------------------------------------------------
# Engine / session
# ---------------------------------------------------------------------------


# Module-level singletons. Populated lazily by ``init_engine``.  Tests
# rebind these in ``conftest.py`` to point at an in-memory DB.
engine: Engine | None = None
SessionLocal: sessionmaker[Session] | None = None


def _apply_pragmas(dbapi_conn, _record) -> None:
    """Apply SQLite PRAGMAs on every new connection.

    - WAL mode: readers don't block writers.
    - synchronous=NORMAL: durable enough, ~2× faster than FULL.
    - foreign_keys=ON: required for ON DELETE CASCADE.
    - busy_timeout=30s: wait out transient writer locks instead of
      raising ``database is locked``.
    """
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
    finally:
        cur.close()


def make_engine(db_path: str) -> Engine:
    """Create a SQLite engine with the standard pragma set applied.

    ``db_path`` may be ``":memory:"`` for tests; in that case a
    ``StaticPool`` is used so every session sees the same in-memory DB.
    """
    if db_path == ":memory:":
        eng = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            future=True,
        )
    else:
        eng = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False, "timeout": 30.0},
            poolclass=NullPool,
            future=True,
        )
    event.listen(eng, "connect", _apply_pragmas)
    return eng


def ensure_schema(target: Engine) -> None:
    """Emit CREATE TABLE for every model on *target* (idempotent).

    Used by helpers that need a ready-to-use schema without invoking
    Alembic, including module-level engine initialization in tests and
    other non-migration code paths.  Production startup should prefer
    :func:`apply_migrations` instead.
    """
    Base.metadata.create_all(target)


def init_engine(db_path: str, *, create_schema: bool = True) -> Engine:
    """Initialize the module-level engine + ``SessionLocal``.

    Idempotent: calling twice with the same path returns the existing
    engine.  Calling with a different path re-creates both.

    ``create_schema`` controls whether ``create_all`` runs.  Production
    startup passes ``False`` and then calls :func:`apply_migrations`,
    which drives schema from Alembic (and stamps baseline for
    pre-Alembic installs).  Tests and the per-store
    ``store_path=...`` constructors default to ``True`` so they get a
    usable schema without needing an Alembic config.
    """
    global engine, SessionLocal  # noqa: PLW0603, intentional module-level state
    if engine is not None:
        # If the caller re-inits with a different URL, dispose the old.
        current_url = str(engine.url)
        wanted_url = "sqlite:///:memory:" if db_path == ":memory:" else f"sqlite:///{db_path}"
        if current_url == wanted_url:
            return engine
        engine.dispose()

    engine = make_engine(db_path)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    if create_schema:
        ensure_schema(engine)
    return engine


# ---------------------------------------------------------------------------
# Alembic integration
# ---------------------------------------------------------------------------


# Baseline table set used to detect "pre-Alembic" SQLite DBs that were
# populated by ``create_all`` before Alembic was introduced.  Updated
# only when the baseline revision grows new tables.
_BASELINE_TABLES: frozenset[str] = frozenset({
    "dats", "dat_hashes", "dat_matches",
    "dat_sync_state", "chd_metadata", "verifications",
})


def _alembic_config(target: Engine):
    """Build an ``alembic.config.Config`` pointed at *target*.

    The sqlalchemy URL is injected here so it matches the already-open
    engine exactly, there is no separate DB resolution path.

    Alembic assets may live in different places depending on how the app
    is packaged (development repo vs. Docker image).  We therefore support
    explicit environment overrides and a small set of runtime-relative
    fallback locations instead of assuming a single directory layout.

    Search order:
    1. ``ALEMBIC_INI_PATH`` + ``ALEMBIC_SCRIPT_LOCATION`` env vars (explicit
       override, useful in containers or custom deployments).
    2. ``<repo_root>/migrations/``, resolved relative to this file (dev
       environment where ``app/services/db.py`` sits three levels down from
       the repo root).
    3. ``<cwd>/migrations/``, works when the app is started from the repo
       root or a directory that contains a ``migrations/`` sub-directory.
    4. ``<db.py parent>/migrations/``, last-resort sibling lookup.
    """
    from alembic.config import Config

    def _candidate_locations() -> list[tuple[Path, Path]]:
        here = Path(__file__).resolve()
        repo_root = here.parent.parent.parent
        cwd = Path.cwd()

        candidates: list[tuple[Path, Path]] = []

        # Explicit env-var override comes first so operators can redirect
        # migrations to a custom location (e.g. in containers) while still
        # going through the same existence-check as the built-in candidates.
        env_ini = os.getenv("ALEMBIC_INI_PATH")
        env_script = os.getenv("ALEMBIC_SCRIPT_LOCATION")
        if env_ini and env_script:
            candidates.append((Path(env_ini), Path(env_script)))

        candidates.extend([
            (repo_root / "migrations" / "alembic.ini", repo_root / "migrations"),
            (cwd / "migrations" / "alembic.ini", cwd / "migrations"),
            (here.parent / "migrations" / "alembic.ini", here.parent / "migrations"),
        ])
        return candidates

    for ini_path, script_location in _candidate_locations():
        if ini_path.is_file() and script_location.is_dir():
            cfg = Config(str(ini_path))
            cfg.set_main_option("script_location", str(script_location))
            cfg.set_main_option("sqlalchemy.url", str(target.url))
            return cfg

    searched = ", ".join(
        f"(ini={ini_path}, scripts={script_location})"
        for ini_path, script_location in _candidate_locations()
    )
    raise RuntimeError(
        "Alembic configuration not found. Set ALEMBIC_INI_PATH and "
        "ALEMBIC_SCRIPT_LOCATION to the runtime locations of alembic.ini "
        f"and the migrations directory. Searched: {searched}",
    )


def apply_migrations(target: Engine | None = None) -> None:
    """Bring the schema to head via Alembic.

    Three paths, in order of detection:

    1. **Already-stamped DB** (``alembic_version`` row present).  Run
       ``alembic upgrade head``; no-op when already at head.
    2. **Pre-Alembic DB** (no ``alembic_version`` row but the
       baseline tables exist).  This is the case for anyone who
       installed the SQLite-migration release before Alembic existed.
       We first ``alembic stamp 0001`` to record the baseline revision,
       then run ``alembic upgrade head`` so any post-baseline migrations
       are applied.  Alembic will create the ``alembic_version`` table
       if it doesn't already exist.
    3. **Fresh DB** (no ``alembic_version`` row, no baseline tables).
       Run ``alembic upgrade head`` to build the schema from scratch.

    A fresh DB is normally produced by :func:`init_engine` with
    ``create_schema=False``.  If the caller already ran
    ``ensure_schema`` / ``create_all`` before calling this, path 2
    applies and we stamp, which is what we want.
    """
    from alembic import command
    from alembic.migration import MigrationContext
    from sqlalchemy import inspect

    eng = target if target is not None else engine
    if eng is None:
        raise RuntimeError(
            "apply_migrations called before init_engine, no engine available.",
        )

    cfg = _alembic_config(eng)

    with eng.connect() as conn:
        # Direct MigrationContext read, not an Alembic command, so no
        # cfg.attributes injection needed here.
        ctx = MigrationContext.configure(conn)
        current_rev = ctx.get_current_revision()

    if current_rev is not None:
        logger.info("db.alembic: current revision %s; running upgrade head", current_rev)
        with eng.connect() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
        return

    inspector = inspect(eng)
    existing_tables = set(inspector.get_table_names())
    if _BASELINE_TABLES.issubset(existing_tables):
        logger.info(
            "db.alembic: baseline schema present without alembic_version; "
            "stamping as revision 0001 (pre-Alembic install)",
        )
        # Stamp the explicit baseline revision first, then upgrade to head.
        # Stamping "head" directly would skip any post-baseline migrations that
        # have since been added; stamping "0001" + upgrading is always correct.
        # Both operations share the same connection so they see a consistent DB state.
        with eng.connect() as conn:
            cfg.attributes["connection"] = conn
            command.stamp(cfg, "0001")
            command.upgrade(cfg, "head")
        return

    logger.info("db.alembic: fresh database; running upgrade head")
    with eng.connect() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


def get_session() -> Session:
    """Return a new session bound to the current engine.

    Raises ``RuntimeError`` if ``init_engine`` has not been called yet,
    that would indicate a store was touched before startup wired up the
    DB, which is a programmer error we want to fail loud on.
    """
    if SessionLocal is None:
        raise RuntimeError(
            "db.SessionLocal not initialized, call db.init_engine() or "
            "db.init_and_migrate() before using any store."
        )
    return SessionLocal()


# ---------------------------------------------------------------------------
# DB path resolution (matches the legacy stores' /tmp fallback behaviour)
# ---------------------------------------------------------------------------


def _is_dir_writable(directory: Path) -> bool:
    """Return True if *directory* is writable by creating and deleting a probe file."""
    try:
        with tempfile.NamedTemporaryFile(dir=str(directory)):
            pass
        return True
    except OSError:
        return False


def resolve_db_path(explicit_path: str | None, data_dir: str) -> str:
    """Resolve the SQLite file path, mirroring legacy fallback logic.

    - If ``explicit_path`` is set, use it verbatim.  The parent
      directory is created if needed; if it cannot be created *or* is
      not writable, raise ``OSError`` (explicit path = user intent, no
      silent fallback).
    - Otherwise, use ``<data_dir>/compressatorium.db``.  If the data
      directory cannot be created or is not writable (e.g. read-only
      ``/config``), fall back to
      ``<TMPDIR>/compressatorium/compressatorium.db`` (same pattern the
      JSON stores use today).
    """
    if explicit_path:
        target = Path(explicit_path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OSError(
                f"Cannot create parent directory for explicit DB path {target}: {exc}"
            ) from exc
        if not _is_dir_writable(target.parent):
            raise OSError(
                f"Explicit DB path parent directory is not writable: {target.parent}"
            )
        return str(target)

    target = Path(data_dir) / "compressatorium.db"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not _is_dir_writable(target.parent):
            raise OSError("data_dir not writable")
    except OSError:
        fallback_root = Path(os.environ.get("TMPDIR", tempfile.gettempdir())) / "compressatorium"
        fallback_root.mkdir(parents=True, exist_ok=True)
        target = fallback_root / "compressatorium.db"
    return str(target)


# ---------------------------------------------------------------------------
# JSON → SQLite migration
# ---------------------------------------------------------------------------


MIGRATED_SUFFIX = ".migrated.bak"
CORRUPT_SUFFIX = ".corrupt"


def _load_json(path: Path) -> Any | None:
    """Read JSON from *path*. Rename to ``.corrupt`` and return None on parse error."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        corrupt_path = path.with_suffix(path.suffix + CORRUPT_SUFFIX)
        try:
            path.rename(corrupt_path)
            logger.error(
                "db.migrate: corrupt JSON at %s renamed to %s: %s",
                path, corrupt_path, exc,
            )
        except OSError:
            logger.exception("db.migrate: could not rename corrupt JSON %s", path)
        return None
    except OSError:
        logger.exception("db.migrate: failed to read %s", path)
        return None


def _mark_migrated(path: Path) -> None:
    """Rename ``path`` → ``path.migrated.bak``.

    If a backup already exists (e.g., from a previous migration run), the
    source JSON is left untouched and a warning is logged so that the
    operator can manually resolve the conflict, the source is **never**
    deleted automatically.
    """
    target = Path(str(path) + MIGRATED_SUFFIX)
    if target.exists():
        # A prior run already produced a backup; leave the source JSON intact
        # so no data is lost, and require manual resolution.
        logger.warning(
            "db.migrate: backup already exists at %s; preserving JSON source %s "
            "unchanged, manual resolution required",
            target, path,
        )
        return
    if not path.exists():
        logger.warning(
            "db.migrate: migration source %s no longer exists; nothing to rename",
            path,
        )
        return
    try:
        path.rename(target)
    except OSError:
        logger.exception(
            "db.migrate: imported data from %s into SQLite but could not rename "
            "the legacy JSON to %s; the source file was left in place, fix "
            "filesystem permissions and restart to retry the rename",
            path,
            target,
        )


def _is_migration_source(path: Path) -> bool:
    """True if *path* is a JSON file we should try to migrate."""
    if not path.exists():
        return False
    # Never migrate a .migrated.bak or .corrupt file.
    return not (str(path).endswith(MIGRATED_SUFFIX) or str(path).endswith(CORRUPT_SUFFIX))


# --- Per-store migrators -----------------------------------------------------


def _migrate_dat_store(engine: Engine, path: Path) -> None:
    """Migrate the legacy dat_store.json into dats/dat_hashes/dat_matches."""
    if not _is_migration_source(path):
        return
    data = _load_json(path)
    if data is None:
        return  # corrupt or unreadable
    if not isinstance(data, dict):
        logger.error("db.migrate: %s is not a dict; skipping", path)
        return

    dats_json = data.get("dats", {}) if isinstance(data.get("dats"), dict) else {}
    hashes = data.get("hashes", {}) if isinstance(data.get("hashes"), dict) else {}
    sha1_map = hashes.get("sha1", {}) if isinstance(hashes.get("sha1"), dict) else {}
    md5_map = hashes.get("md5", {}) if isinstance(hashes.get("md5"), dict) else {}
    matches_json = data.get("matches", {}) if isinstance(data.get("matches"), dict) else {}

    expected_dats = len(dats_json)
    expected_hashes = len(sha1_map) + len(md5_map)
    expected_matches = len(matches_json)

    with Session(engine) as session:
        # Only migrate when the target tables are empty.  If any row
        # exists we assume a prior migration already handled this store.
        if session.scalar(select(DAT).limit(1)) is not None:
            logger.info(
                "db.migrate: dats table non-empty; skipping import from %s "
                "(leaving JSON in place, manual cleanup may be required)",
                path,
            )
            return

        try:
            # Bulk insert DATs.
            dat_rows = [
                {
                    "id": dat_id,
                    "name": info.get("name", "") or "",
                    "description": info.get("description", "") or "",
                    "version": info.get("version", "") or "",
                    "imported_at": info.get("imported_at", "") or "",
                    "file_count": int(info.get("file_count", 0) or 0),
                }
                for dat_id, info in dats_json.items()
                if isinstance(info, dict)
            ]
            if dat_rows:
                session.bulk_insert_mappings(DAT, dat_rows)

            # Collect valid dat_ids so we can drop orphaned hashes/matches
            # rather than crashing on FK violation.
            valid_dat_ids = {row["id"] for row in dat_rows}

            hash_rows: list[dict[str, Any]] = []
            for hex_hash, record in sha1_map.items():
                if not isinstance(record, dict):
                    continue
                dat_id = record.get("dat_id")
                if dat_id not in valid_dat_ids:
                    continue
                hash_rows.append({
                    "hash": str(hex_hash).lower(),
                    "hash_type": "sha1",
                    "dat_id": dat_id,
                    "game_name": record.get("game_name", "") or "",
                    "rom_name": record.get("rom_name", "") or "",
                    "size": int(record.get("size", 0) or 0),
                })
            for hex_hash, record in md5_map.items():
                if not isinstance(record, dict):
                    continue
                dat_id = record.get("dat_id")
                if dat_id not in valid_dat_ids:
                    continue
                hash_rows.append({
                    "hash": str(hex_hash).lower(),
                    "hash_type": "md5",
                    "dat_id": dat_id,
                    "game_name": record.get("game_name", "") or "",
                    "rom_name": record.get("rom_name", "") or "",
                    "size": int(record.get("size", 0) or 0),
                })
            if hash_rows:
                session.bulk_insert_mappings(DATHash, hash_rows)

            match_rows: list[dict[str, Any]] = []
            for match_path, record in matches_json.items():
                if not isinstance(record, dict):
                    continue
                # dat_id may point at a DAT that no longer exists (e.g.,
                # the DAT was deleted after the match was cached). Fall
                # back to NULL rather than drop the match, UI still
                # wants to show "not matched" style results.
                dat_id = record.get("dat_id")
                if dat_id is not None and dat_id not in valid_dat_ids:
                    dat_id = None
                match_rows.append({
                    "path": match_path,
                    "matched": bool(record.get("matched", False)),
                    "dat_id": dat_id,
                    "game_name": record.get("game_name"),
                    "rom_name": record.get("rom_name"),
                    "match_type": record.get("match_type"),
                    "file_hash": record.get("file_hash"),
                    "payload": record,
                })
            if match_rows:
                session.bulk_insert_mappings(DATMatch, match_rows)

            session.flush()

            # Validate counts.  Hashes may legitimately be fewer than
            # expected if some pointed at nonexistent DATs (orphans
            # dropped), warn but don't abort.  DAT and match counts
            # must match exactly.
            dat_count = session.query(DAT).count()
            hash_count = session.query(DATHash).count()
            match_count = session.query(DATMatch).count()

            if dat_count != expected_dats:
                raise RuntimeError(
                    f"dat_store migration: dat count mismatch "
                    f"(expected {expected_dats}, got {dat_count})"
                )
            if match_count != expected_matches:
                raise RuntimeError(
                    f"dat_store migration: match count mismatch "
                    f"(expected {expected_matches}, got {match_count})"
                )
            if hash_count != expected_hashes:
                logger.warning(
                    "db.migrate: dat_store: %d/%d hashes migrated "
                    "(some referenced unknown DATs and were dropped)",
                    hash_count, expected_hashes,
                )

            session.commit()
        except Exception:
            session.rollback()
            logger.exception("db.migrate: dat_store import failed; JSON preserved at %s", path)
            raise

    _mark_migrated(path)
    logger.info(
        "db.migrate: dat_store imported, %d DATs, %d hashes, %d matches → %s.migrated.bak",
        expected_dats, expected_hashes, expected_matches, path.name,
    )


def _migrate_verification_store(engine: Engine, path: Path) -> None:
    """Migrate verified_chds.json into the verifications table."""
    if not _is_migration_source(path):
        return
    data = _load_json(path)
    if data is None:
        return
    if not isinstance(data, dict):
        logger.error("db.migrate: %s is not a dict; skipping", path)
        return

    expected = len(data)

    with Session(engine) as session:
        if session.scalar(select(Verification).limit(1)) is not None:
            logger.info(
                "db.migrate: verifications table non-empty; skipping import from %s",
                path,
            )
            return

        try:
            rows = []
            for chd_path, record in data.items():
                if not isinstance(record, dict):
                    continue
                rows.append({
                    "chd_path": chd_path,
                    "source_path": record.get("source_path"),
                    "verified_at": record.get("verified_at", "") or "",
                })
            if rows:
                session.bulk_insert_mappings(Verification, rows)
            session.flush()

            got = session.query(Verification).count()
            if got != expected:
                raise RuntimeError(
                    f"verification_store migration: count mismatch "
                    f"(expected {expected}, got {got})"
                )
            session.commit()
        except Exception:
            session.rollback()
            logger.exception(
                "db.migrate: verification_store import failed; JSON preserved at %s", path
            )
            raise

    _mark_migrated(path)
    logger.info(
        "db.migrate: verification_store imported, %d entries → %s.migrated.bak",
        expected, path.name,
    )


def _migrate_chd_metadata_store(engine: Engine, path: Path) -> None:
    """Migrate chd_metadata.json into the chd_metadata table."""
    if not _is_migration_source(path):
        return
    data = _load_json(path)
    if data is None:
        return
    if not isinstance(data, dict):
        logger.error("db.migrate: %s is not a dict; skipping", path)
        return

    expected = len(data)

    with Session(engine) as session:
        if session.scalar(select(CHDMetadata).limit(1)) is not None:
            logger.info(
                "db.migrate: chd_metadata table non-empty; skipping import from %s",
                path,
            )
            return

        try:
            rows = []
            for chd_path, record in data.items():
                if not isinstance(record, dict):
                    continue
                rows.append({
                    "chd_path": chd_path,
                    "metadata_json": record.get("metadata"),
                    "media_type": record.get("media_type"),
                    "mtime": record.get("mtime"),
                    "cached_at": record.get("cached_at"),
                    "disc_id_checked": bool(record.get("disc_id_checked", False)),
                    "disc_id_checked_mtime": record.get("disc_id_checked_mtime"),
                    "game_id": record.get("game_id"),
                    "title": record.get("title"),
                })
            if rows:
                session.bulk_insert_mappings(CHDMetadata, rows)
            session.flush()

            got = session.query(CHDMetadata).count()
            if got != expected:
                raise RuntimeError(
                    f"chd_metadata migration: count mismatch "
                    f"(expected {expected}, got {got})"
                )
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("db.migrate: chd_metadata import failed; JSON preserved at %s", path)
            raise

    _mark_migrated(path)
    logger.info(
        "db.migrate: chd_metadata imported, %d entries → %s.migrated.bak",
        expected, path.name,
    )


def _migrate_dat_sync_state(engine: Engine, path: Path) -> None:
    """Migrate dat_sync.json into the dat_sync_state singleton row."""
    if not _is_migration_source(path):
        return
    data = _load_json(path)
    if data is None:
        return
    if not isinstance(data, dict):
        logger.error("db.migrate: %s is not a dict; skipping", path)
        return

    with Session(engine) as session:
        existing = session.get(DATSyncState, 1)
        if existing is not None:
            logger.info(
                "db.migrate: dat_sync_state already present; skipping import from %s",
                path,
            )
            return

        try:
            row = DATSyncState(
                id=1,
                last_sync_tag=data.get("last_sync_tag") or None,
                last_sync_at=data.get("last_sync_at") or None,
                last_sync_files=int(data.get("last_sync_files", 0) or 0),
            )
            session.add(row)
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("db.migrate: dat_sync_state import failed; JSON preserved at %s", path)
            raise

    _mark_migrated(path)
    logger.info("db.migrate: dat_sync_state imported → %s.migrated.bak", path.name)


# --- Top-level entry point ---------------------------------------------------


def init_and_migrate(
    db_path: str,
    *,
    dat_store_json: str | os.PathLike[str] | None = None,
    verification_json: str | os.PathLike[str] | None = None,
    chd_metadata_json: str | os.PathLike[str] | None = None,
    dat_sync_json: str | os.PathLike[str] | None = None,
) -> Engine:
    """Initialize the DB and migrate any legacy JSON stores found on disk.

    Every migration runs independently.  A failure in one store logs a
    loud WARNING but does not block the others, and leaves that store's
    JSON source file untouched for later inspection / retry.

    Parameters
    ----------
    db_path:
        Target SQLite file (or ``":memory:"``).
    dat_store_json / verification_json / chd_metadata_json / dat_sync_json:
        Source JSON paths.  ``None`` skips that store.

    Returns
    -------
    The initialized SQLAlchemy ``Engine``.
    """
    eng = init_engine(db_path)
    logger.info("db: engine ready at %s", db_path)

    migrators: Iterable[tuple[str, Any, Path | None]] = (
        ("dat_store", _migrate_dat_store, Path(dat_store_json) if dat_store_json else None),
        (
            "verification_store",
            _migrate_verification_store,
            Path(verification_json) if verification_json else None,
        ),
        (
            "chd_metadata",
            _migrate_chd_metadata_store,
            Path(chd_metadata_json) if chd_metadata_json else None,
        ),
        ("dat_sync_state", _migrate_dat_sync_state, Path(dat_sync_json) if dat_sync_json else None),
    )

    failures: list[str] = []
    for name, fn, src in migrators:
        if src is None:
            continue
        try:
            fn(eng, src)
        except Exception:  # noqa: BLE001, each migration is isolated
            failures.append(name)
            # Exception already logged inside the migrator.

    if failures:
        logger.warning(
            "db.migrate: %d store(s) failed migration and will retry next startup: %s",
            len(failures), ", ".join(failures),
        )

    return eng
