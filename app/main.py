import json
import logging
from logging_setup import get_logger
import os
from contextlib import asynccontextmanager
from pathlib import Path

from auth import ensure_auth_token, require_auth_middleware
from config import settings
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from routes import convert, dat, files, info, preferences, romm
from services.job_manager import job_manager
from services.nsz import nsz_service


# package.json (repo root) is the single source of truth for the version.
_PACKAGE_JSON = Path(__file__).resolve().parent.parent / "package.json"


def _version_from_package_json() -> str | None:
    """Return the ``version`` string from the repo-root ``package.json``, or ``None``.

    ``get_version()`` runs while the FastAPI app is constructed, so this never
    raises: it returns ``None`` (caller falls back to ``"dev"``) when the file is
    missing, unparseable, not a JSON object, or carries a missing / non-string /
    empty ``version`` -- e.g. a slim image that does not ship ``package.json``, or
    a top-level array/null/number that would otherwise ``AttributeError`` on
    ``.get()``.
    """
    try:
        with _PACKAGE_JSON.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) and version.strip() else None


def get_version() -> str:
    """Return the application version.

    ``APP_VERSION`` (the Docker build arg, derived from the release tag) wins so
    container builds stay authoritative. When it is unset or empty (local
    ``./run_dev.sh`` runs, tests, CI) fall back to the ``version`` field in the
    repo-root ``package.json`` -- which ``AGENTS.md`` names the single source of
    truth -- so the served version cannot silently drift from the file.
    ``"dev"`` is the last resort when neither is available.
    """
    env_version = os.environ.get("APP_VERSION")
    if env_version:
        return env_version
    return _version_from_package_json() or "dev"


_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# ANSI SGR codes per level; reset after levelname only so message bodies
# stay uncolored (copy-paste from `docker logs` remains clean).
_LEVEL_COLORS = {
    "DEBUG": "\x1b[2m",       # dim
    "INFO": "\x1b[32m",       # green
    "WARNING": "\x1b[33m",    # yellow
    "ERROR": "\x1b[31m",      # red
    "CRITICAL": "\x1b[1;31m", # bold red
}
_RESET = "\x1b[0m"


class ColorFormatter(logging.Formatter):
    """Formatter that wraps only ``%(levelname)s`` in ANSI color codes."""

    def format(self, record: logging.LogRecord) -> str:
        color = _LEVEL_COLORS.get(record.levelname)
        if not color:
            return super().format(record)
        original = record.levelname
        record.levelname = f"{color}{original}{_RESET}"
        try:
            return super().format(record)
        finally:
            record.levelname = original


def _resolve_color(mode: str, stream) -> bool:
    """Decide whether to emit ANSI colors on *stream*.

    ``mode`` is the ``log_color`` setting: ``auto`` / ``always`` / ``never``.
    ``auto`` enables color iff *stream* is a TTY and the ``NO_COLOR`` env
    var (https://no-color.org) is unset.
    """
    normalized = (mode or "").strip().lower()
    if normalized == "always":
        return True
    if normalized == "never":
        return False
    # "auto" and any unrecognised value: fall back to TTY detection.
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def configure_logging() -> None:
    # getLevelName() returns an int for known level names (DEBUG, INFO, WARNING,
    # ERROR, CRITICAL, plus legacy aliases WARN=WARNING and FATAL=CRITICAL)
    # and a "Level <name>" string for unknown values, unlike
    # getattr(logging, name, None) which can return non-integer logging
    # attributes (classes, format strings, etc.) for non-level names.
    level_str = settings.log_level.strip().upper()
    level = logging.getLevelName(level_str)
    invalid_level = not isinstance(level, int)
    if invalid_level:
        level = logging.INFO

    color_mode = settings.log_color.strip().lower()
    invalid_color = color_mode not in {"auto", "always", "never"}

    logger = get_logger()
    # Always update the level so re-calls (e.g. test isolation or reloads)
    # respect the current LOGLEVEL setting, even when handlers already exist.
    logger.setLevel(level)
    if logger.handlers:
        return

    logger.propagate = False
    plain_formatter = logging.Formatter(_LOG_FORMAT)

    stream_handler = logging.StreamHandler()
    if _resolve_color(color_mode, stream_handler.stream):
        stream_handler.setFormatter(ColorFormatter(_LOG_FORMAT))
    else:
        stream_handler.setFormatter(plain_formatter)
    logger.addHandler(stream_handler)

    if settings.log_path:
        log_dir = os.path.dirname(settings.log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.FileHandler(settings.log_path)
        # File logs are never colored, keeps grep / log aggregators sane.
        file_handler.setFormatter(plain_formatter)
        logger.addHandler(file_handler)

    if invalid_level:
        logger.warning(
            "Unknown LOGLEVEL %r, defaulting to INFO. "
            "Valid values: DEBUG, INFO, WARNING, ERROR, CRITICAL",
            settings.log_level,
        )
    if invalid_color:
        logger.warning(
            "Unknown LOG_COLOR %r, defaulting to auto. "
            "Valid values: auto, always, never",
            settings.log_color,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background job processor and run startup migrations."""
    import asyncio

    configure_logging()
    logger = get_logger()

    # Initialise the SQLite DB and run any pending JSON → SQLite migrations
    # BEFORE any store is touched (dat_store.has_dats() below needs it).
    from pathlib import Path
    from services import db as _db

    db_path = _db.resolve_db_path(settings.db_path, data_dir=settings.data_dir)
    # Initialise the engine *without* create_all, Alembic owns the
    # schema on the production path.  apply_migrations then stamps
    # pre-Alembic DBs or upgrades the rest.  Only after the schema is
    # guaranteed at head do we run the JSON→SQLite importers.
    _db.init_engine(db_path, create_schema=False)
    _db.apply_migrations()

    _config_dir = Path(settings.data_dir)

    def _resolve_legacy_json_store(env_var: str, default_filename: str) -> Path:
        """Return the JSON migration source path, honouring any legacy env-var override."""
        default_path = _config_dir / default_filename
        legacy_override = os.environ.get(env_var)
        if not legacy_override:
            return default_path
        resolved_path = Path(legacy_override)
        if not resolved_path.exists():
            logger.warning(
                "db.migrate: legacy JSON store override %s=%s does not exist; "
                "falling back to default store %s",
                env_var,
                resolved_path,
                default_path,
            )
            return default_path
        if not resolved_path.is_file() or not os.access(resolved_path, os.R_OK):
            logger.warning(
                "db.migrate: legacy JSON store override %s=%s is not a readable file; "
                "falling back to default store %s",
                env_var,
                resolved_path,
                default_path,
            )
            return default_path
        logger.info(
            "db.migrate: using legacy JSON store override for migration %s=%s",
            env_var,
            resolved_path,
        )
        if resolved_path != default_path and default_path.exists():
            logger.warning(
                "db.migrate: legacy JSON store override %s=%s will be used for migration; "
                "default store %s also exists and will not be imported in this run",
                env_var,
                resolved_path,
                default_path,
            )
        return resolved_path

    _db.init_and_migrate(
        db_path,
        dat_store_json=_resolve_legacy_json_store(
            "CHD_DAT_STORE", "dat_store.json",
        ),
        verification_json=_resolve_legacy_json_store(
            "CHD_VERIFICATION_STORE", "verified_chds.json",
        ),
        chd_metadata_json=_resolve_legacy_json_store(
            "CHD_METADATA_STORE", "chd_metadata.json",
        ),
        dat_sync_json=_resolve_legacy_json_store(
            "CHD_DAT_SYNC_STORE", "dat_sync.json",
        ),
    )
    explicit_volumes = [v.strip() for v in str(settings.chd_volumes).split(",") if v.strip()]
    discovered_volumes = [] if explicit_volumes else settings.scan_data_mounts_on_startup()
    logger.info(f"Compressatorium v{get_version()} starting...")
    auth_token = ensure_auth_token()
    if not settings.enable_auth:
        logger.info(
            "Web UI authentication disabled; set COMPRESSATORIUM_ENABLE_AUTH=true "
            "to require a token"
        )
    elif auth_token:
        logger.info("Web UI authentication enabled for user %s", settings.auth_username)
    logger.info(
        "Runtime limits pid=%s max_concurrent_jobs=%s max_job_history=%s lock_dir=%s",
        os.getpid(),
        settings.max_concurrent_jobs,
        settings.max_job_history,
        settings.concurrency_lock_dir,
    )
    logger.info(
        "Volume discovery mount_root=%s explicit=%s discovered=%s effective=%s",
        settings.data_mount_root,
        explicit_volumes,
        discovered_volumes,
        settings.volumes,
    )
    # Surface the Switch (nsz) key-discovery decision so operators can see
    # whether the tool is enabled and, if not, where keys were looked for.
    nsz_service.log_startup_status()
    # Initialise the set used to hold strong references to background tasks so
    # they are not garbage-collected before they finish.
    app.state.background_tasks = set()

    process_queue_task = asyncio.create_task(job_manager.process_queue())
    app.state.background_tasks.add(process_queue_task)
    process_queue_task.add_done_callback(app.state.background_tasks.discard)

    def _log_process_queue_error(t: asyncio.Task) -> None:
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                logger.error(
                    "process_queue task exited unexpectedly",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

    process_queue_task.add_done_callback(_log_process_queue_error)

    # Prime the RomM settings cache before anything can serve a request.  The
    # RomM client reads it synchronously from worker threads, so it must be
    # populated first; without this the first catalog call would fall back to
    # the environment and ignore whatever the operator saved in the app.
    from services.romm import auto as romm_auto, settings as romm_settings

    try:
        romm_cfg = await romm_settings.load()
    except Exception:
        logger.exception("Failed to load RomM settings; using environment defaults")
        romm_cfg = {}
    if romm_cfg.get("url"):
        logger.info("RomM integration configured (%s)", romm_cfg["url"])

    # A change of RomM instance or library root clears the records that
    # belonged to the old one. If the process died between installing the new
    # identity and finishing that cleanup, the marker written with it says so,
    # and the stale rows would otherwise stay live against the new instance
    # forever. Best-effort: a failure here must not stop the app from starting,
    # and the marker survives for the next attempt.
    try:
        await romm.replay_identity_cleanup()
    except Exception:
        logger.exception("Failed to finish a pending RomM identity cleanup")

    # How a conversion *ended* is only knowable while the queue remembers the
    # job, and its history is capped and in-memory. An automation rule needs
    # that answer weeks later to know whether a ROM was really converted, so
    # it listens for the outcome and writes it down as each job finishes.
    job_manager.add_terminal_listener(romm_auto.note_job_finished)

    # The unattended-conversion scheduler always runs; it checks the master
    # switch each tick, so toggling auto-convert in the app takes effect
    # without a restart.  It sleeps first, so startup never queues anything.
    romm_auto_task = asyncio.create_task(romm_auto.run_forever())
    app.state.background_tasks.add(romm_auto_task)
    romm_auto_task.add_done_callback(app.state.background_tasks.discard)

    def _log_romm_auto_error(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            logger.error(
                "RomM auto-convert scheduler exited unexpectedly",
                exc_info=t.exception(),
            )

    romm_auto_task.add_done_callback(_log_romm_auto_error)

    # The re-pin settler runs beside it: the request-driven pass skips rows it
    # cannot finish inside a browser request, and without this nothing would
    # ever finish a large output's hash.
    romm_settle_task = asyncio.create_task(romm.settle_forever())
    app.state.background_tasks.add(romm_settle_task)
    romm_settle_task.add_done_callback(app.state.background_tasks.discard)

    def _log_romm_settle_error(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            logger.error(
                "RomM re-pin settler exited unexpectedly", exc_info=t.exception(),
            )

    romm_settle_task.add_done_callback(_log_romm_settle_error)

    # Auto-sync MAMERedump DATs on startup.  Two independent triggers:
    #   1. MAMEREDUMP_AUTO_SYNC=true AND the store is empty  → fresh-install sync.
    #   2. Any DAT has file_count=0 (regardless of MAMEREDUMP_AUTO_SYNC) →
    #      self-heal.  Stale rows are broken state from a pre-parser-upgrade
    #      import (see PR #49); the sync service's _do_sync() detects the
    #      zero-count rows and auto-forces a full re-import.
    from fastapi.concurrency import run_in_threadpool
    from services.dat_store import dat_store
    from services.dat_sync import dat_sync_service

    def _spawn_sync_task(reason: str) -> None:
        logger.info("%s, starting background sync", reason)

        async def _run_sync():
            try:
                await dat_sync_service.sync()
            except Exception:
                logger.exception("Background DAT sync failed (%s)", reason)

        # Strong reference: without this the Task could be GC'd before it
        # finishes.  The done callback removes it once complete.
        task = asyncio.create_task(_run_sync())
        app.state.background_tasks.add(task)
        task.add_done_callback(app.state.background_tasks.discard)

    has_any_dats = await run_in_threadpool(dat_store.has_dats)
    if settings.mameredump_auto_sync and not has_any_dats:
        _spawn_sync_task("MAMEREDUMP_AUTO_SYNC enabled and no DATs loaded")
    elif has_any_dats and await run_in_threadpool(dat_store.has_stale_dats):
        _spawn_sync_task(
            "DAT store contains stale rows (file_count=0), self-healing",
        )

    yield


app = FastAPI(
    title="Compressatorium",
    description=(
        "Web UI for converting game files with chdman, dolphin-tool, "
        "z3ds_compressor, nsz, maxcso, 7z (handheld ROM archives), "
        "makeps3iso (PS3 folder to ISO), nkit2iso (NKit to ISO), "
        "and JWUDTool (Wii U WUD/WUX)"
    ),
    version=get_version(),
    lifespan=lifespan,
)

if settings.enable_auth:
    @app.middleware("http")
    async def require_auth(request, call_next):
        return await require_auth_middleware(request, call_next)


# Include routers
app.include_router(files.router, prefix="/api", tags=["files"])
app.include_router(convert.router, prefix="/api", tags=["convert"])
app.include_router(info.router, prefix="/api", tags=["info"])
app.include_router(dat.router, prefix="/api", tags=["dat"])
app.include_router(preferences.router, prefix="/api", tags=["preferences"])
app.include_router(romm.router, prefix="/api", tags=["romm"])


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "version": get_version()}


# Serve static files
static_dir = os.environ.get("STATIC_DIR", "/static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    """Serve the main HTML page."""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, headers={"Cache-Control": "no-store"})
    return {"message": "Compressatorium API", "docs": "/docs"}
