"""Tests for MAME Redump DAT sync service."""

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import sessionmaker

from services import db as _db
from services.dat_sync import DATSyncService

# Preload routes.dat so the post-sync rematch hook's lazy
# ``from routes.dat import schedule_match_job`` hits an already-imported
# module object. Without this, the first test that exercises the hook
# could import the module fresh and miss a pending ``patch()`` call that
# replaced the attribute on a module not yet in ``sys.modules``.
import routes.dat as _dat_routes_import  # noqa: F401  (side-effect import)


# ---------------------------------------------------------------------------
# DATSyncService unit tests
# ---------------------------------------------------------------------------

@pytest.fixture
def sync_service(tmp_path):
    """Create a DATSyncService with isolated state (bypasses lazy init).

    Each test gets its own SQLite file + sessionmaker so state is
    completely isolated.
    """
    engine = _db.make_engine(str(tmp_path / "dat_sync.db"))
    _db.Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    svc = DATSyncService.__new__(DATSyncService)
    svc._repo = "MetalSlug/MAMERedump"
    svc._state_path = tmp_path / "dat_sync.db"  # informational only now
    svc._explicit_state_path = str(svc._state_path)
    svc._lock = threading.Lock()
    svc._init_lock = threading.Lock()
    svc._initialized = True  # pre-initialised; skip lazy init in tests
    svc._syncing = False
    svc._cancel = False
    svc._progress = {}
    svc._state = {}
    svc._session_factory = session_factory
    return svc


def test_status_default(sync_service):
    """Default status has no sync in progress and empty fields."""
    status = sync_service.get_status()
    assert status["syncing"] is False
    assert status["last_sync_tag"] == ""
    assert status["last_sync_at"] == ""
    assert status["last_sync_files"] == 0


def test_state_persistence(sync_service):
    """State round-trips through save/load against the DB."""
    sync_service._state = {
        "last_sync_tag": "0.285",
        "last_sync_at": "2026-02-12T20:00:00Z",
        "last_sync_files": 69,
    }
    sync_service._save_state()

    loaded = sync_service._load_state()
    assert loaded["last_sync_tag"] == "0.285"
    assert loaded["last_sync_files"] == 69


def test_state_load_missing_row_returns_empty(sync_service):
    """Loading state with no row in the DB returns an empty dict."""
    state = sync_service._load_state()
    assert state == {}


def test_state_upsert_overwrites_previous_save(sync_service):
    """Saving twice leaves only the latest values in the singleton row."""
    sync_service._state = {"last_sync_tag": "0.280", "last_sync_files": 50}
    sync_service._save_state()

    sync_service._state = {"last_sync_tag": "0.285", "last_sync_files": 69}
    sync_service._save_state()

    loaded = sync_service._load_state()
    assert loaded["last_sync_tag"] == "0.285"
    assert loaded["last_sync_files"] == 69


def test_cancel_when_not_syncing(sync_service):
    """cancel() returns False when no sync is in progress."""
    assert sync_service.cancel() is False


def test_cancel_when_syncing(sync_service):
    """cancel() returns True and sets flag when syncing."""
    sync_service._syncing = True
    assert sync_service.cancel() is True
    assert sync_service._cancel is True


def test_github_api_url(sync_service):
    """_github_api_url builds correct URLs."""
    url = sync_service._github_api_url("MAME Redump", ref="0.285")
    assert "repos/MetalSlug/MAMERedump/contents/MAME%20Redump" in url
    assert "ref=0.285" in url


def test_raw_url(sync_service):
    """_raw_url builds correct raw content URLs."""
    url = sync_service._raw_url("MAME Redump/test.dat", ref="0.285")
    assert "raw.githubusercontent.com/MetalSlug/MAMERedump/0.285/MAME%20Redump/test.dat" in url


def test_require_https_rejects_non_https(sync_service):
    """_require_https raises ValueError for non-https schemes."""
    with pytest.raises(ValueError, match="Only https URLs"):
        sync_service._require_https("http://example.com/foo")
    with pytest.raises(ValueError, match="Only https URLs"):
        sync_service._require_https("file:///etc/passwd")
    with pytest.raises(ValueError, match="Only https URLs"):
        sync_service._require_https("ftp://example.com/foo")


def test_require_https_allows_https(sync_service):
    """_require_https does not raise for https URLs."""
    sync_service._require_https("https://api.github.com/repos/foo/bar")
    sync_service._require_https("https://raw.githubusercontent.com/foo/bar/main/test.dat")


def test_list_dat_files_filters_non_dat(sync_service):
    """_list_dat_files only returns .dat files."""
    mock_contents = [
        {"name": "test.dat", "path": "MAME Redump/test.dat", "type": "file", "size": 100},
        {"name": "README.md", "path": "MAME Redump/README.md", "type": "file", "size": 50},
        {"name": "MAME", "path": "MAME Redump/MAME", "type": "dir"},
    ]
    with patch.object(sync_service, "_fetch_json", return_value=mock_contents):
        files = sync_service._list_dat_files("MAME Redump", "main")
    assert len(files) == 1
    assert files[0]["name"] == "test.dat"


# ---------------------------------------------------------------------------
# Async sync tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_already_synced(sync_service):
    """Sync returns early when already synced to the requested tag and DATs are present."""
    sync_service._state = {"last_sync_tag": "0.285"}
    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[
        {"id": "abc", "name": "Test DAT", "file_count": 42}
    ])
    mock_dat_store.delete_dats_bulk = AsyncMock(return_value=0)
    with patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store):
        result = await sync_service.sync(tag="0.285")
    assert result["status"] == "already_synced"


@pytest.mark.asyncio
async def test_sync_already_synced_requires_nonzero_file_counts(sync_service):
    """Tag matches + DATs present + every DAT has file_count>0 = fast-path."""
    sync_service._state = {"last_sync_tag": "0.285"}
    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[
        {"id": "a", "name": "DAT A", "file_count": 100},
        {"id": "b", "name": "DAT B", "file_count": 50},
    ])
    mock_dat_store.delete_dats_bulk = AsyncMock(return_value=0)
    with patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store):
        result = await sync_service.sync(tag="0.285")
    assert result["status"] == "already_synced"


@pytest.mark.asyncio
async def test_sync_auto_forces_when_any_dat_has_zero_entries(sync_service, tmp_path, caplog):
    """One file_count=0 DAT must trip the auto-force path (pre-parser-upgrade state)."""
    sync_service._state = {"last_sync_tag": "0.285"}
    dat_file = tmp_path / "sample.dat"
    dat_file.write_text("<datafile></datafile>")

    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[
        {"id": "a", "name": "OK DAT", "file_count": 100},
        {"id": "b", "name": "Stale DAT", "file_count": 0},
    ])
    mock_dat_store.delete_dats_bulk = AsyncMock(return_value=0)
    mock_dat_store.import_dat_no_persist = AsyncMock(
        return_value={"id": "new1", "name": "Fresh", "file_count": 1, "hashes_added": 1}
    )
    mock_dat_store.persist = AsyncMock()
    mock_dat_store.list_match_paths = MagicMock(return_value=[])

    with caplog.at_level("WARNING", logger="compressatorium.dat_sync"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             [{"name": "test.dat", "path": "MAME Redump/test.dat", "size": 100}],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", return_value=str(dat_file)), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store):
        result = await sync_service.sync(tag="0.285")

    assert result["status"] == "complete"
    assert any(
        "have file_count=0" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_sync_force_bypasses_already_synced_guard(sync_service, tmp_path):
    """force=True re-syncs even when tag matches and all DATs are healthy."""
    sync_service._state = {"last_sync_tag": "0.285"}
    dat_file = tmp_path / "sample.dat"
    dat_file.write_text("<datafile></datafile>")

    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[
        {"id": "a", "name": "DAT A", "file_count": 100},
    ])
    mock_dat_store.delete_dats_bulk = AsyncMock(return_value=0)
    mock_dat_store.import_dat_no_persist = AsyncMock(
        return_value={"id": "new1", "name": "Fresh", "file_count": 1, "hashes_added": 1}
    )
    mock_dat_store.persist = AsyncMock()
    mock_dat_store.list_match_paths = MagicMock(return_value=[])

    with patch.object(sync_service, "_list_dat_files", side_effect=[
             [{"name": "test.dat", "path": "MAME Redump/test.dat", "size": 100}],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", return_value=str(dat_file)), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store):
        result = await sync_service.sync(tag="0.285", force=True)

    assert result["status"] == "complete"
    assert result["files_imported"] == 1
    mock_dat_store.import_dat_no_persist.assert_called_once()


@pytest.mark.asyncio
async def test_sync_resyncs_when_dats_empty(sync_service, tmp_path):
    """Sync re-runs when tag matches but DAT store is empty (e.g., after manual deletion)."""
    sync_service._state = {"last_sync_tag": "0.285"}
    dat_file = tmp_path / "sample.dat"
    dat_file.write_text("<datafile></datafile>")
    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[])
    mock_dat_store.delete_dats_bulk = AsyncMock(return_value=0)
    mock_dat_store.import_dat_no_persist = AsyncMock(
        return_value={"id": "abc", "name": "Test", "file_count": 1, "hashes_added": 1}
    )
    mock_dat_store.persist = AsyncMock()
    mock_dat_store.list_match_paths = MagicMock(return_value=[])

    with patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             [{"name": "test.dat", "path": "MAME Redump/test.dat", "size": 100}],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", return_value=str(dat_file)), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store):
        result = await sync_service.sync(tag="0.285")

    assert result["status"] == "complete"
    assert result["files_imported"] == 1


@pytest.mark.asyncio
async def test_sync_prevents_concurrent(sync_service):
    """Sync raises when already in progress."""
    sync_service._syncing = True
    with pytest.raises(RuntimeError, match="already in progress"):
        await sync_service.sync()


@pytest.mark.asyncio
async def test_sync_downloads_and_imports(sync_service, tmp_path):
    """Sync downloads DAT files and imports them into the store."""
    dat_file = tmp_path / "sample.dat"
    dat_file.write_text("""\
<?xml version="1.0"?>
<datafile>
  <header><name>Test</name><version>0.285</version></header>
  <game name="G"><rom name="g.iso" size="1024"
    sha1="aabbccddaabbccddaabbccddaabbccddaabbccdd"/></game>
</datafile>
""")

    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[])
    mock_dat_store.delete_dats_bulk = AsyncMock(return_value=0)
    mock_dat_store.import_dat_no_persist = AsyncMock(return_value={
        "id": "abc123", "name": "Test", "file_count": 1, "hashes_added": 1,
    })
    mock_dat_store.persist = AsyncMock()
    mock_dat_store.list_match_paths = MagicMock(return_value=[])

    # _list_dat_files is called once per _DAT_DIRS entry (2 dirs).
    # Return 1 file for first dir, empty for second.
    with patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             [{"name": "test.dat", "path": "MAME Redump/test.dat", "size": 100}],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", return_value=str(dat_file)), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store):
        result = await sync_service.sync()

    assert result["status"] == "complete"
    assert result["files_imported"] == 1
    assert result["tag"] == "0.285"
    assert sync_service._state["last_sync_tag"] == "0.285"
    mock_dat_store.import_dat_no_persist.assert_called_once()
    mock_dat_store.persist.assert_called_once()
    # No existing DATs → delete_dats_bulk not called.
    mock_dat_store.delete_dats_bulk.assert_not_called()


@pytest.mark.asyncio
async def test_sync_handles_download_error(sync_service, tmp_path):
    """Sync continues when individual file download/import fails."""
    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[])
    mock_dat_store.delete_dats_bulk = AsyncMock(return_value=0)
    mock_dat_store.import_dat_no_persist = AsyncMock()
    mock_dat_store.persist = AsyncMock()
    mock_dat_store.list_match_paths = MagicMock(return_value=[])
    mock_dat_store.discard_pending = AsyncMock()

    with patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             # _list_dat_files is called once per _DAT_DIRS entry (2 dirs).
             [
                 {"name": "good.dat", "path": "MAME Redump/good.dat", "size": 100},
                 {"name": "bad.dat", "path": "MAME Redump/bad.dat", "size": 100},
             ],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", side_effect=[
             OSError("Network error for good.dat"),
             OSError("Network error for bad.dat"),
         ]), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store):
        result = await sync_service.sync()

    assert result["status"] == "complete_with_errors"
    assert len(result["errors"]) == 2
    assert result["error"] is not None
    mock_dat_store.import_dat_no_persist.assert_not_called()
    # All downloads failed, staged DATs are discarded (no DB writes).
    mock_dat_store.discard_pending.assert_called_once()
    mock_dat_store.delete_dats_bulk.assert_not_called()


@pytest.mark.asyncio
async def test_sync_defers_deletion_until_all_imports_succeed(sync_service, tmp_path):
    """Existing DATs are only deleted after ALL new files are imported successfully."""
    dat_file = tmp_path / "sample.dat"
    dat_file.write_text("<datafile></datafile>")

    existing_dat = {"id": "old-dat", "name": "Old DAT"}
    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[existing_dat])
    mock_dat_store.delete_dats_bulk = AsyncMock(return_value=1)
    mock_dat_store.import_dat_no_persist = AsyncMock(return_value={
        "id": "new-dat", "name": "New DAT", "file_count": 1, "hashes_added": 1,
    })
    mock_dat_store.persist = AsyncMock()
    mock_dat_store.list_match_paths = MagicMock(return_value=[])
    mock_dat_store.discard_pending = AsyncMock()

    with patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             [{"name": "test.dat", "path": "MAME Redump/test.dat", "size": 100}],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", return_value=str(dat_file)), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store):
        result = await sync_service.sync(tag="0.285")

    assert result["status"] == "complete"
    # New DATs committed first, then old DAT deleted.
    mock_dat_store.persist.assert_called_once()
    mock_dat_store.delete_dats_bulk.assert_called_once_with(["old-dat"])
    assert sync_service._state.get("last_sync_tag") == "0.285"


@pytest.mark.asyncio
async def test_sync_preserves_existing_dats_on_partial_failure(sync_service, tmp_path):
    """If some imports fail, existing DATs are preserved and last_sync_tag is NOT saved."""
    dat_file = tmp_path / "good.dat"
    dat_file.write_text("<datafile></datafile>")

    existing_dat = {"id": "old-dat", "name": "Old DAT"}
    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[existing_dat])
    mock_dat_store.delete_dats_bulk = AsyncMock(return_value=1)
    mock_dat_store.import_dat_no_persist = AsyncMock(side_effect=[
        {"id": "new-dat", "name": "New DAT", "file_count": 1, "hashes_added": 1},
        OSError("Import failed for second file"),
    ])
    mock_dat_store.persist = AsyncMock()
    mock_dat_store.list_match_paths = MagicMock(return_value=[])
    mock_dat_store.discard_pending = AsyncMock()

    with patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             [
                 {"name": "good.dat", "path": "MAME Redump/good.dat", "size": 100},
                 {"name": "bad.dat", "path": "MAME Redump/bad.dat", "size": 100},
             ],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", return_value=str(dat_file)), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store):
        result = await sync_service.sync(tag="0.285")

    assert result["status"] == "complete_with_errors"
    assert len(result["errors"]) == 1
    assert result["error"] is not None
    # Partial failure: staged new DATs discarded, old DAT preserved.
    mock_dat_store.discard_pending.assert_called_once()
    mock_dat_store.delete_dats_bulk.assert_not_called()
    # last_sync_tag must NOT be saved so next sync can retry the missing file.
    assert "last_sync_tag" not in sync_service._state


@pytest.mark.asyncio
async def test_sync_preserves_existing_dats_when_all_downloads_fail(sync_service, tmp_path):
    """If all downloads fail, existing DATs are preserved (not deleted)."""
    existing_dat = {"id": "old-dat", "name": "Old DAT"}
    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[existing_dat])
    mock_dat_store.delete_dats_bulk = AsyncMock(return_value=0)
    mock_dat_store.import_dat_no_persist = AsyncMock()
    mock_dat_store.persist = AsyncMock()
    mock_dat_store.list_match_paths = MagicMock(return_value=[])
    mock_dat_store.discard_pending = AsyncMock()

    with patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             [{"name": "test.dat", "path": "MAME Redump/test.dat", "size": 100}],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", side_effect=OSError("Network failure")), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store):
        result = await sync_service.sync(tag="0.285")

    assert result["status"] == "complete_with_errors"
    assert len(result["errors"]) == 1
    assert result["error"] is not None
    # No successful imports → staged DATs discarded (no-op), old DAT preserved.
    mock_dat_store.discard_pending.assert_called_once()
    mock_dat_store.delete_dats_bulk.assert_not_called()


@pytest.mark.asyncio
async def test_sync_skips_oversized_files(sync_service, tmp_path):
    """Files whose reported size exceeds _MAX_DAT_SIZE are skipped with an error."""
    from app.services.dat_sync import _MAX_DAT_SIZE

    dat_file = tmp_path / "small.dat"
    dat_file.write_text('<datafile><header><name>Small</name></header></datafile>')
    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[])
    mock_dat_store.import_dat_no_persist = AsyncMock(return_value={"id": "small-dat"})
    mock_dat_store.delete_dats_bulk = AsyncMock(return_value=1)
    mock_dat_store.persist = AsyncMock()
    mock_dat_store.list_match_paths = MagicMock(return_value=[])
    mock_dat_store.discard_pending = AsyncMock()

    with patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             [
                 # This file is too large and should be skipped without downloading.
                 {"name": "huge.dat", "path": "MAME Redump/huge.dat", "size": _MAX_DAT_SIZE + 1},
                 # This file is fine and will be imported.
                 {"name": "small.dat", "path": "MAME Redump/small.dat", "size": 100},
             ],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", return_value=str(dat_file)) as mock_download, \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store):
        result = await sync_service.sync(tag="0.285")

    # Oversized file causes a partial-failure result.
    assert result["status"] == "complete_with_errors"
    assert len(result["errors"]) == 1
    assert "huge.dat" in result["errors"][0]
    assert result["error"] is not None
    # _download_dat called only for the non-oversized file.
    mock_download.assert_called_once()
    # small.dat was imported (files_imported=1), but staged DATs discarded on partial failure.
    assert result["files_imported"] == 1
    mock_dat_store.discard_pending.assert_called_once()
    mock_dat_store.delete_dats_bulk.assert_not_called()


@pytest.mark.asyncio
async def test_sync_sets_error_progress_on_exception(sync_service):
    """sync() sets progress status=error when _do_sync raises."""
    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[])

    with patch.object(sync_service, "_fetch_latest_tag", side_effect=OSError("DNS failure")), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store):
        with pytest.raises(OSError):
            await sync_service.sync()

    assert sync_service._progress["status"] == "error"
    assert "DNS failure" in sync_service._progress["error"]


# ---------------------------------------------------------------------------
# Post-sync rematch hook (v3.7.4): snapshot match cache, kick a fresh scan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_sync_schedules_rematch_after_success(sync_service, tmp_path):
    """After a successful sync, schedule_match_job runs with the pre-wipe path list."""
    dat_file = tmp_path / "sample.dat"
    dat_file.write_text("<datafile></datafile>")

    snapshot = ["/data/a.chd", "/data/b.chd", "/data/c.chd"]
    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[])
    mock_dat_store.delete_dats_bulk = AsyncMock(return_value=0)
    mock_dat_store.import_dat_no_persist = AsyncMock(return_value={
        "id": "new1", "name": "Fresh", "file_count": 1, "hashes_added": 1,
    })
    mock_dat_store.persist = AsyncMock()
    mock_dat_store.list_match_paths = MagicMock(return_value=snapshot)

    mock_schedule = AsyncMock(return_value="rematch-job-42")

    with patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             [{"name": "test.dat", "path": "MAME Redump/test.dat", "size": 100}],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", return_value=str(dat_file)), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store), \
         patch("routes.dat.schedule_match_job", mock_schedule):
        result = await sync_service.sync(tag="0.285")

    assert result["status"] == "complete"
    mock_schedule.assert_awaited_once_with(snapshot, defer_if_busy=True)


@pytest.mark.asyncio
async def test_do_sync_skips_rematch_when_no_previous_matches(sync_service, tmp_path):
    """Empty match cache → no schedule_match_job call."""
    dat_file = tmp_path / "sample.dat"
    dat_file.write_text("<datafile></datafile>")

    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[])
    mock_dat_store.delete_dats_bulk = AsyncMock(return_value=0)
    mock_dat_store.import_dat_no_persist = AsyncMock(return_value={
        "id": "new1", "name": "Fresh", "file_count": 1, "hashes_added": 1,
    })
    mock_dat_store.persist = AsyncMock()
    mock_dat_store.list_match_paths = MagicMock(return_value=[])

    mock_schedule = AsyncMock(return_value=None)

    with patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             [{"name": "test.dat", "path": "MAME Redump/test.dat", "size": 100}],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", return_value=str(dat_file)), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store), \
         patch("routes.dat.schedule_match_job", mock_schedule):
        result = await sync_service.sync(tag="0.285")

    assert result["status"] == "complete"
    mock_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_do_sync_skips_rematch_on_partial_failure(sync_service, tmp_path):
    """Partial-failure branch does not wipe the cache → no rematch scheduled."""
    dat_file = tmp_path / "good.dat"
    dat_file.write_text("<datafile></datafile>")

    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[])
    mock_dat_store.delete_dats_bulk = AsyncMock(return_value=0)
    mock_dat_store.import_dat_no_persist = AsyncMock(side_effect=[
        {"id": "new-dat", "name": "New DAT", "file_count": 1, "hashes_added": 1},
        OSError("Import failed"),
    ])
    mock_dat_store.persist = AsyncMock()
    mock_dat_store.discard_pending = AsyncMock()
    # list_match_paths must never be consulted on the partial-failure branch.
    mock_dat_store.list_match_paths = MagicMock(
        side_effect=AssertionError("list_match_paths called on partial-failure branch"),
    )

    mock_schedule = AsyncMock()

    with patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             [
                 {"name": "good.dat", "path": "MAME Redump/good.dat", "size": 100},
                 {"name": "bad.dat", "path": "MAME Redump/bad.dat", "size": 100},
             ],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", return_value=str(dat_file)), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store), \
         patch("routes.dat.schedule_match_job", mock_schedule):
        result = await sync_service.sync(tag="0.285")

    assert result["status"] == "complete_with_errors"
    mock_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_do_sync_logs_when_rematch_is_skipped_due_to_active_job(sync_service, tmp_path, caplog):
    """schedule_match_job returning None logs the skip reason but doesn't fail the sync."""
    dat_file = tmp_path / "sample.dat"
    dat_file.write_text("<datafile></datafile>")

    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[])
    mock_dat_store.delete_dats_bulk = AsyncMock(return_value=0)
    mock_dat_store.import_dat_no_persist = AsyncMock(return_value={
        "id": "new1", "name": "Fresh", "file_count": 1, "hashes_added": 1,
    })
    mock_dat_store.persist = AsyncMock()
    mock_dat_store.list_match_paths = MagicMock(return_value=["/data/a.chd"])

    mock_schedule = AsyncMock(return_value=None)

    # The busy case is reported by the shared helper in routes.dat now, so
    # capture that logger rather than dat_sync's.
    with caplog.at_level("INFO", logger="compressatorium.dat"), \
         patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             [{"name": "test.dat", "path": "MAME Redump/test.dat", "size": 100}],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", return_value=str(dat_file)), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store), \
         patch("routes.dat.schedule_match_job", mock_schedule):
        result = await sync_service.sync(tag="0.285")

    assert result["status"] == "complete"
    assert any("deferred rematch" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_do_sync_surfaces_rematch_scheduled_status(sync_service, tmp_path):
    """When the rematch is scheduled, progress + result payload carry the job id."""
    dat_file = tmp_path / "sample.dat"
    dat_file.write_text("<datafile></datafile>")

    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[])
    mock_dat_store.delete_dats_bulk = AsyncMock(return_value=0)
    mock_dat_store.import_dat_no_persist = AsyncMock(return_value={
        "id": "new1", "name": "Fresh", "file_count": 1, "hashes_added": 1,
    })
    mock_dat_store.persist = AsyncMock()
    mock_dat_store.list_match_paths = MagicMock(return_value=["/data/a.chd"])

    mock_schedule = AsyncMock(return_value="rematch-job-777")

    with patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             [{"name": "test.dat", "path": "MAME Redump/test.dat", "size": 100}],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", return_value=str(dat_file)), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store), \
         patch("routes.dat.schedule_match_job", mock_schedule):
        result = await sync_service.sync(tag="0.285")

    assert result["rematch_status"] == "scheduled"
    assert result["rematch_job_id"] == "rematch-job-777"
    # Progress dict is what /dat/sync/status exposes to the UI.
    assert sync_service._progress["rematch_status"] == "scheduled"
    assert sync_service._progress["rematch_job_id"] == "rematch-job-777"


@pytest.mark.asyncio
async def test_do_sync_surfaces_rematch_deferred_status(sync_service, tmp_path):
    """Another match job active → rematch deferred; UI sees the signal."""
    dat_file = tmp_path / "sample.dat"
    dat_file.write_text("<datafile></datafile>")

    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[])
    mock_dat_store.delete_dats_bulk = AsyncMock(return_value=0)
    mock_dat_store.import_dat_no_persist = AsyncMock(return_value={
        "id": "new1", "name": "Fresh", "file_count": 1, "hashes_added": 1,
    })
    mock_dat_store.persist = AsyncMock()
    mock_dat_store.list_match_paths = MagicMock(return_value=["/data/a.chd"])

    mock_schedule = AsyncMock(return_value=None)

    with patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             [{"name": "test.dat", "path": "MAME Redump/test.dat", "size": 100}],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", return_value=str(dat_file)), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store), \
         patch("routes.dat.schedule_match_job", mock_schedule):
        result = await sync_service.sync(tag="0.285")

    assert result["rematch_status"] == "deferred"
    assert result["rematch_job_id"] is None


@pytest.mark.asyncio
async def test_do_sync_surfaces_rematch_failed_status(sync_service, tmp_path):
    """schedule_match_job raising → rematch_status=failed, sync still complete."""
    dat_file = tmp_path / "sample.dat"
    dat_file.write_text("<datafile></datafile>")

    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[])
    mock_dat_store.delete_dats_bulk = AsyncMock(return_value=0)
    mock_dat_store.import_dat_no_persist = AsyncMock(return_value={
        "id": "new1", "name": "Fresh", "file_count": 1, "hashes_added": 1,
    })
    mock_dat_store.persist = AsyncMock()
    mock_dat_store.list_match_paths = MagicMock(return_value=["/data/a.chd"])

    mock_schedule = AsyncMock(side_effect=RuntimeError("scheduler exploded"))

    with patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             [{"name": "test.dat", "path": "MAME Redump/test.dat", "size": 100}],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", return_value=str(dat_file)), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store), \
         patch("routes.dat.schedule_match_job", mock_schedule):
        result = await sync_service.sync(tag="0.285")

    assert result["status"] == "complete"
    assert result["rematch_status"] == "failed"
    assert result["rematch_job_id"] is None


@pytest.mark.asyncio
async def test_do_sync_list_match_paths_failure_is_best_effort(sync_service, tmp_path, caplog):
    """If the snapshot SELECT raises, the sync must still commit, aborting
    here would leak the staged DATs from import_dat_no_persist into the next
    sync and corrupt the store."""
    dat_file = tmp_path / "sample.dat"
    dat_file.write_text("<datafile></datafile>")

    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[])
    mock_dat_store.delete_dats_bulk = AsyncMock(return_value=0)
    mock_dat_store.import_dat_no_persist = AsyncMock(return_value={
        "id": "new1", "name": "Fresh", "file_count": 1, "hashes_added": 1,
    })
    mock_dat_store.persist = AsyncMock()
    mock_dat_store.list_match_paths = MagicMock(
        side_effect=RuntimeError("SELECT failed: db locked"),
    )

    mock_schedule = AsyncMock()

    with caplog.at_level("ERROR", logger="compressatorium.dat_sync"), \
         patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             [{"name": "test.dat", "path": "MAME Redump/test.dat", "size": 100}],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", return_value=str(dat_file)), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store), \
         patch("routes.dat.schedule_match_job", mock_schedule):
        result = await sync_service.sync(tag="0.285")

    # Sync succeeded, persist() still ran, DATs are committed.
    assert result["status"] == "complete"
    mock_dat_store.persist.assert_awaited_once()
    # Rematch was NOT scheduled because the snapshot failed.
    mock_schedule.assert_not_awaited()
    # Operator can see the snapshot failure in the logs.
    assert any("failed to snapshot match paths" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_do_sync_rematch_schedule_exception_does_not_poison_sync(sync_service, tmp_path, caplog):
    """If schedule_match_job raises, the sync still reports complete, DAT
    data was already committed, a rematch-scheduling failure must not mark
    an otherwise-successful sync as errored."""
    dat_file = tmp_path / "sample.dat"
    dat_file.write_text("<datafile></datafile>")

    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[])
    mock_dat_store.delete_dats_bulk = AsyncMock(return_value=0)
    mock_dat_store.import_dat_no_persist = AsyncMock(return_value={
        "id": "new1", "name": "Fresh", "file_count": 1, "hashes_added": 1,
    })
    mock_dat_store.persist = AsyncMock()
    mock_dat_store.list_match_paths = MagicMock(return_value=["/data/a.chd"])

    mock_schedule = AsyncMock(side_effect=RuntimeError("job manager explosion"))

    with caplog.at_level("ERROR", logger="compressatorium.dat_sync"), \
         patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             [{"name": "test.dat", "path": "MAME Redump/test.dat", "size": 100}],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", return_value=str(dat_file)), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store), \
         patch("routes.dat.schedule_match_job", mock_schedule):
        result = await sync_service.sync(tag="0.285")

    assert result["status"] == "complete"
    assert sync_service._state.get("last_sync_tag") == "0.285"
    assert any("failed to schedule a rematch" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _download_dat streaming size guard
# ---------------------------------------------------------------------------

def test_download_dat_rejects_large_content_length(sync_service, monkeypatch):
    """_download_dat raises ValueError when Content-Length header exceeds _MAX_DAT_SIZE."""
    from app.services.dat_sync import _MAX_DAT_SIZE
    import urllib.request

    # Use a whitespace-padded value to also exercise the .strip() handling.
    large_cl = f"  {_MAX_DAT_SIZE + 1}  "

    class MockResp:
        headers = {"Content-Length": large_cl}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: MockResp())

    with pytest.raises(ValueError, match="Content-Length"):
        sync_service._download_dat("MAME Redump/test.dat", "0.285")


def test_download_dat_aborts_mid_stream_if_oversized(sync_service, monkeypatch, tmp_path):
    """_download_dat raises ValueError and cleans up temp file when streamed bytes exceed
    _MAX_DAT_SIZE.
    """
    from app.services.dat_sync import _MAX_DAT_SIZE
    import urllib.request

    # Simulate a response that omits Content-Length but streams oversized data.
    chunk = b"x" * 65536
    # We need more chunks than fit in _MAX_DAT_SIZE to reliably cross the limit:
    # one extra to push bytes_written over the cap, plus one more as a safety margin.
    chunks_needed = (_MAX_DAT_SIZE // len(chunk)) + 2
    call_count = [0]

    class MockResp:
        headers = {}  # No Content-Length

        def read(self, size):
            call_count[0] += 1
            if call_count[0] <= chunks_needed:
                return chunk
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: MockResp())

    with pytest.raises(ValueError, match="exceeded size limit"):
        sync_service._download_dat("MAME Redump/test.dat", "0.285")
