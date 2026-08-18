"""Tests for MAME Redump DAT file management routes."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.routes import dat as dat_routes

# Minimal valid Logiqx XML DAT for testing
SAMPLE_DAT_XML = """\
<?xml version="1.0"?>
<datafile>
  <header>
    <name>Test Redump DAT</name>
    <description>Test DAT for unit tests</description>
    <version>1.0</version>
  </header>
  <game name="Test Game">
    <rom name="test.iso" size="737280000"
         sha1="aabbccddaabbccddaabbccddaabbccddaabbccdd"
         md5="aabbccddaabbccddaabbccddaabbccdd"/>
  </game>
</datafile>
"""

SECOND_DAT_XML = """\
<?xml version="1.0"?>
<datafile>
  <header>
    <name>Second DAT</name>
    <description>Another DAT</description>
    <version>2.0</version>
  </header>
  <game name="Another Game">
    <rom name="another.iso" size="1024"
         sha1="1122334455667788990011223344556677889900"
         md5="11223344556677889900112233445566"/>
  </game>
</datafile>
"""


def _make_upload_file(content: str, filename: str = "test.dat"):
    """Build a minimal UploadFile-like mock."""
    encoded = content.encode("utf-8")
    chunks = [encoded[i:i + 65536] for i in range(0, len(encoded), 65536)] + [b""]
    mock_file = MagicMock()
    mock_file.filename = filename
    mock_file.read = AsyncMock(side_effect=chunks)
    return mock_file


@pytest.fixture
def isolated_dat_store(tmp_path, monkeypatch):
    """Provide a fresh DATStore backed by a temp file for each test."""
    from services.dat_store import DATStore
    store = DATStore(store_path=str(tmp_path / "dat_store.json"))
    monkeypatch.setattr(dat_routes, "dat_store", store)
    return store


# ---------------------------------------------------------------------------
# /dat/import
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_import_dat_happy_path(isolated_dat_store):
    """Importing a valid DAT returns summary and populates the store."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    result = await dat_routes.import_dat(file=upload)

    assert result["name"] == "Test Redump DAT"
    assert result["version"] == "1.0"
    assert result["file_count"] == 1
    assert result["hashes_added"] == 2  # 1 SHA1 + 1 MD5

    dats = isolated_dat_store.list_dats()
    assert len(dats) == 1
    assert dats[0]["name"] == "Test Redump DAT"


@pytest.mark.asyncio
async def test_import_dat_wrong_extension():
    """Uploading a file with a disallowed extension is rejected (400)."""
    upload = _make_upload_file(SAMPLE_DAT_XML, filename="test.txt")
    with pytest.raises(HTTPException) as exc_info:
        await dat_routes.import_dat(file=upload)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_import_dat_invalid_xml(isolated_dat_store):
    """Uploading malformed XML is rejected with 400."""
    upload = _make_upload_file("this is not xml", filename="bad.dat")
    with pytest.raises(HTTPException) as exc_info:
        await dat_routes.import_dat(file=upload)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_import_dat_clears_match_cache(isolated_dat_store):
    """Importing a new DAT must clear the stale _matches cache."""
    # Seed a stale "unmatched" cache entry
    await isolated_dat_store.set_match(
        "/some/file.iso", {"path": "/some/file.iso", "matched": False}
    )
    assert isolated_dat_store.get_match("/some/file.iso") is not None

    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    # Cache should have been cleared so the stale entry is gone
    assert isolated_dat_store.get_match("/some/file.iso") is None


# ---------------------------------------------------------------------------
# /dat/list  and  /dat/{dat_id}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_dats_empty(isolated_dat_store):
    """list_dats returns an empty list when no DATs are imported."""
    result = await dat_routes.list_dats()
    assert result == []


@pytest.mark.asyncio
async def test_list_dats_after_import(isolated_dat_store):
    """list_dats returns imported DAT entries."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    result = await dat_routes.list_dats()
    assert len(result) == 1
    assert result[0]["name"] == "Test Redump DAT"


@pytest.mark.asyncio
async def test_delete_dat_happy_path(isolated_dat_store):
    """Deleting an existing DAT returns success and removes it from the store."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    imported = await dat_routes.import_dat(file=upload)
    dat_id = imported["id"]

    result = await dat_routes.delete_dat(dat_id=dat_id)
    assert result["deleted"] is True
    assert result["id"] == dat_id
    assert isolated_dat_store.list_dats() == []


@pytest.mark.asyncio
async def test_delete_dat_not_found(isolated_dat_store):
    """Deleting a nonexistent DAT raises 404."""
    with pytest.raises(HTTPException) as exc_info:
        await dat_routes.delete_dat(dat_id="doesnotexist")
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# /dat/match  (single file)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_match_file_access_denied(tmp_path, isolated_dat_store, monkeypatch):
    """match_file raises 403 for paths outside configured volumes."""
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: False)
    request = dat_routes.MatchRequest(path="/outside/file.iso")
    with pytest.raises(HTTPException) as exc_info:
        await dat_routes.match_file(request)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_match_file_not_found(tmp_path, isolated_dat_store, monkeypatch):
    """match_file raises 404 when the file does not exist on disk."""
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    request = dat_routes.MatchRequest(path=str(tmp_path / "nonexistent.iso"))
    with pytest.raises(HTTPException) as exc_info:
        await dat_routes.match_file(request)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_match_file_hit(tmp_path, isolated_dat_store, monkeypatch):
    """match_file returns matched=True when file SHA1 is in the DAT index."""
    # Import the sample DAT
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    # Create a file whose SHA1 matches the DAT entry
    target_sha1 = "aabbccddaabbccddaabbccddaabbccddaabbccdd"
    iso = tmp_path / "test.iso"
    iso.write_bytes(b"fake content")

    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", AsyncMock(return_value=target_sha1))

    request = dat_routes.MatchRequest(path=str(iso))
    result = await dat_routes.match_file(request)

    assert result["matched"] is True
    assert result["game_name"] == "Test Game"
    assert result["match_type"] == "file_sha1"


@pytest.mark.asyncio
async def test_match_file_miss(tmp_path, isolated_dat_store, monkeypatch):
    """match_file returns matched=False when SHA1 is not in any DAT."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    iso = tmp_path / "unknown.iso"
    iso.write_bytes(b"unknown content")

    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    monkeypatch.setattr(
        dat_routes, "compute_file_sha1",
        AsyncMock(return_value="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"),
    )

    request = dat_routes.MatchRequest(path=str(iso))
    result = await dat_routes.match_file(request)

    assert result["matched"] is False


# ---------------------------------------------------------------------------
# /dat/match-batch  (cache + invalidation)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_match_batch_no_dats(isolated_dat_store, monkeypatch):
    """match-batch returns unmatched for all paths when no DATs are loaded."""
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    request = dat_routes.MatchBatchRequest(paths=["/foo/a.iso", "/foo/b.iso"])
    result = await dat_routes.match_batch(request)

    for path in ["/foo/a.iso", "/foo/b.iso"]:
        assert result["results"][path]["matched"] is False


@pytest.mark.asyncio
async def test_match_batch_uses_cache(tmp_path, isolated_dat_store, monkeypatch):
    """match-batch returns cached results and skips recomputing them."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    iso = tmp_path / "cached.iso"
    iso.write_bytes(b"x")
    # Use the normalized path as both cache key and request path
    path = os.path.normpath(os.path.abspath(str(iso)))

    # Seed the cache manually with the normalized path
    cached_result = {"path": path, "matched": True, "game_name": "Cached Game"}
    await isolated_dat_store.set_match(path, cached_result)

    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    compute_mock = AsyncMock(return_value="deadbeef" * 5)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", compute_mock)

    request = dat_routes.MatchBatchRequest(paths=[path])
    result = await dat_routes.match_batch(request)

    # SHA1 computation should NOT have been called, result came from cache
    compute_mock.assert_not_called()
    assert result["results"][path]["matched"] is True
    assert result["results"][path]["game_name"] == "Cached Game"


@pytest.mark.asyncio
async def test_match_batch_cache_cleared_after_new_import(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """Stale 'unmatched' cache entries are cleared when a new DAT is imported."""
    iso = tmp_path / "game.iso"
    iso.write_bytes(b"content")
    path = str(iso)

    # Import first DAT and cache "unmatched" for the file
    upload1 = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload1)
    await isolated_dat_store.set_match(path, {"path": path, "matched": False})

    assert isolated_dat_store.get_match(path) is not None

    # Import a second DAT, this must clear the match cache
    upload2 = _make_upload_file(SECOND_DAT_XML)
    await dat_routes.import_dat(file=upload2)

    # The stale cache entry should be gone
    assert isolated_dat_store.get_match(path) is None

    # match-batch should now recompute (not serve the stale miss)
    target_sha1 = "1122334455667788990011223344556677889900"
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", AsyncMock(return_value=target_sha1))

    request = dat_routes.MatchBatchRequest(paths=[path])
    result = await dat_routes.match_batch(request)

    assert result["results"][path]["matched"] is True
    assert result["results"][path]["game_name"] == "Another Game"


# ---------------------------------------------------------------------------
# DATStore.get_dat_name  (O(1) lookup)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_dat_name_returns_name(isolated_dat_store):
    """get_dat_name returns the correct name for a known DAT ID."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    imported = await dat_routes.import_dat(file=upload)
    dat_id = imported["id"]

    assert isolated_dat_store.get_dat_name(dat_id) == "Test Redump DAT"


@pytest.mark.asyncio
async def test_get_dat_name_unknown(isolated_dat_store):
    """get_dat_name returns 'Unknown' for an unrecognized DAT ID."""
    assert isolated_dat_store.get_dat_name("nonexistent-id") == "Unknown"


# ---------------------------------------------------------------------------
# _try_embedded_hash_match  (per-tool embedded-hash fast path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chd_header_match_hit(tmp_path, isolated_dat_store, monkeypatch):
    """CHD files are matched via their cached header SHA1 (chdman hook)."""
    from services.tools import registry

    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake chd")
    chd_path = str(chd)

    # Mock chd_metadata_store.get_fresh_metadata to return sha1 matching the DAT
    # entry. ChdmanTool.embedded_hashes imports chd_metadata_store at call time,
    # so the patch on the module-level singleton applies.
    mock_metadata_store = MagicMock()
    mock_metadata_store.get_fresh_metadata = AsyncMock(
        return_value={"sha1": "aabbccddaabbccddaabbccddaabbccddaabbccdd", "data_sha1": ""}
    )
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)

    with patch("services.chd_metadata_store.chd_metadata_store", mock_metadata_store):
        result, candidates = await dat_routes._try_embedded_hash_match(
            chd_path, registry.get("chdman"),
        )

    assert result is not None
    assert candidates
    assert result["matched"] is True
    assert result["match_type"] == "chd_sha1"
    assert result["game_name"] == "Test Game"


@pytest.mark.asyncio
async def test_chd_hook_stale_metadata_suppresses_embedded_hashes(tmp_path):
    """A changed CHD (stale cache) reports no embedded candidates so its
    outdated header/data SHA1 aren't matched, but the file-level SHA1 fallback
    is preserved (chdman is non-exhaustive)."""
    from services.tools import registry

    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake chd")

    # A stale CHD: the atomic snapshot returns None (file changed since cached),
    # so no embedded candidates are reported.
    mock_metadata_store = MagicMock()
    mock_metadata_store.get_fresh_metadata = AsyncMock(return_value=None)

    with patch("services.chd_metadata_store.chd_metadata_store", mock_metadata_store):
        result = await registry.get("chdman").embedded_hashes(str(chd))

    assert result == []


@pytest.mark.asyncio
async def test_match_single_file_stale_chd_falls_back_to_file_sha1(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """A stale CHD still matches via the file-level SHA1 fallback (the stale
    embedded disc hash is suppressed, the current container hash is used)."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake chd")

    mock_metadata_store = MagicMock()
    mock_metadata_store.is_stale = AsyncMock(return_value=True)
    mock_metadata_store.get_metadata = AsyncMock(
        return_value={"sha1": "9" * 40, "data_sha1": "8" * 40}
    )
    target_sha1 = "aabbccddaabbccddaabbccddaabbccddaabbccdd"
    monkeypatch.setattr(dat_routes, "compute_file_sha1", AsyncMock(return_value=target_sha1))

    with patch("services.chd_metadata_store.chd_metadata_store", mock_metadata_store):
        result = await dat_routes._match_single_file(str(chd))

    assert result["matched"] is True
    assert result["match_type"] == "file_sha1"


@pytest.mark.asyncio
async def test_chd_header_match_no_metadata(tmp_path, isolated_dat_store):
    """Uncached CHD reports no candidates (and does not raise as 'stale')."""
    from services.tools import registry

    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake chd")

    mock_metadata_store = MagicMock()
    # is_stale is True for an uncached CHD (no row); get_metadata None must be
    # checked first so this is treated as "no candidates", not a hash failure.
    mock_metadata_store.is_stale = AsyncMock(return_value=True)
    mock_metadata_store.get_metadata = AsyncMock(return_value=None)

    with patch("services.chd_metadata_store.chd_metadata_store", mock_metadata_store):
        result, candidates = await dat_routes._try_embedded_hash_match(
            str(chd), registry.get("chdman"),
        )

    assert result is None
    assert not candidates  # -> caller falls back to file-level SHA1


@pytest.mark.asyncio
async def test_dolphin_disc_hash_match_hit(tmp_path, isolated_dat_store, monkeypatch):
    """Compressed Dolphin outputs match via the dolphin disc SHA1 hook."""
    from services.tools import registry

    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    rvz = tmp_path / "game.rvz"
    rvz.write_bytes(b"fake rvz")

    dolphin = registry.get("dolphin")
    target_sha1 = "aabbccddaabbccddaabbccddaabbccddaabbccdd"
    monkeypatch.setattr(
        dolphin._service, "disc_hashes", AsyncMock(return_value=[target_sha1]),
    )

    result, candidates = await dat_routes._try_embedded_hash_match(str(rvz), dolphin)

    assert result is not None
    assert candidates
    assert result["matched"] is True
    assert result["match_type"] == "dolphin_disc_sha1"
    assert result["file_hash"] == target_sha1


@pytest.mark.asyncio
async def test_dolphin_iso_skips_disc_hash(tmp_path):
    """A plain .iso reports no embedded hash (falls back to file-level SHA1)."""
    from services.tools import registry

    dolphin = registry.get("dolphin")
    assert await dolphin.embedded_hashes(str(tmp_path / "game.iso")) == []


@pytest.mark.asyncio
async def test_dolphin_disc_hash_respects_size_cap(tmp_path, monkeypatch):
    """Over-cap containers skip the expensive verify and report no hash."""
    from services.tools import dolphin as dolphin_module
    from services.tools import registry

    rvz = tmp_path / "big.rvz"
    rvz.write_bytes(b"x" * 1000)

    dolphin = registry.get("dolphin")
    disc_hashes_mock = AsyncMock(return_value=["a" * 40])
    monkeypatch.setattr(dolphin._service, "disc_hashes", disc_hashes_mock)
    # Cap below the file size: the verify pass must be skipped entirely.
    monkeypatch.setattr(dolphin_module.settings, "match_max_file_size", 100)

    result = await dolphin.embedded_hashes(str(rvz))

    assert result == []
    disc_hashes_mock.assert_not_called()


@pytest.mark.asyncio
async def test_dolphin_hook_stat_failure_raises(tmp_path, monkeypatch):
    """A stat failure under the size cap is non-cacheable (EmbeddedHashUnavailable),
    not a silent fall-through to the container SHA1, for the exhaustive tool."""
    from services.tools import dolphin as dolphin_module
    from services.tools import registry
    from services.tools.base import EmbeddedHashUnavailable

    dolphin = registry.get("dolphin")
    # Cap must be enabled for the getsize() path to run.
    monkeypatch.setattr(dolphin_module.settings, "match_max_file_size", 100)
    # Missing file -> getsize() raises (FileNotFoundError is an OSError).
    with pytest.raises(EmbeddedHashUnavailable):
        await dolphin.embedded_hashes(str(tmp_path / "gone.rvz"))


@pytest.mark.asyncio
async def test_dolphin_hook_runs_under_match_lane(tmp_path, monkeypatch):
    """The Dolphin verify pass holds the 'match' workload lane while running."""
    from services.tools import registry
    from services.workload_limiter import workload_limiter

    rvz = tmp_path / "g.rvz"
    rvz.write_bytes(b"x")
    dolphin = registry.get("dolphin")

    seen = {}

    async def fake_disc_hashes(path, *, cancel_event=None):
        seen["in_use"] = workload_limiter.in_use("match")
        return ["a" * 40]

    monkeypatch.setattr(dolphin._service, "disc_hashes", fake_disc_hashes)

    await dolphin.embedded_hashes(str(rvz))

    # The lane was held (>=1 in use) during the expensive verify.
    assert seen["in_use"] >= 1


@pytest.mark.asyncio
async def test_dolphin_hook_raises_on_verify_failure(tmp_path, monkeypatch):
    """A failed verify (empty hash output) raises EmbeddedHashUnavailable."""
    from services.tools import registry
    from services.tools.base import EmbeddedHashUnavailable

    rvz = tmp_path / "bad.rvz"
    rvz.write_bytes(b"x")
    dolphin = registry.get("dolphin")
    monkeypatch.setattr(dolphin._service, "disc_hashes", AsyncMock(return_value=[]))

    with pytest.raises(EmbeddedHashUnavailable):
        await dolphin.embedded_hashes(str(rvz))


@pytest.mark.asyncio
async def test_match_single_file_non_cacheable_on_dolphin_failure(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """A Dolphin verify failure yields a non-cacheable error, not a false miss."""
    from services.tools import registry

    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    rvz = tmp_path / "bad.rvz"
    rvz.write_bytes(b"x")
    monkeypatch.setattr(
        registry.get("dolphin")._service, "disc_hashes", AsyncMock(return_value=[]),
    )
    # The container's file SHA1 must NOT be used as a fallback here.
    file_sha1_mock = AsyncMock(return_value="deadbeef" * 5)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", file_sha1_mock)

    result = await dat_routes._match_single_file(str(rvz))

    assert result["matched"] is False
    assert result.get("error") == "embedded hash unavailable"
    file_sha1_mock.assert_not_called()


@pytest.mark.asyncio
async def test_match_single_file_uses_dolphin_hook(tmp_path, isolated_dat_store, monkeypatch):
    """_match_single_file routes .rvz through the dolphin hook before file SHA1."""
    from services.tools import registry

    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    rvz = tmp_path / "game.rvz"
    rvz.write_bytes(b"fake rvz")

    target_sha1 = "aabbccddaabbccddaabbccddaabbccddaabbccdd"
    monkeypatch.setattr(
        registry.get("dolphin")._service,
        "disc_hashes",
        AsyncMock(return_value=[target_sha1]),
    )
    # file-level SHA1 must NOT be reached when the disc hash already matches.
    file_sha1_mock = AsyncMock(return_value="deadbeef" * 5)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", file_sha1_mock)

    result = await dat_routes._match_single_file(str(rvz))

    assert result["matched"] is True
    assert result["match_type"] == "dolphin_disc_sha1"
    file_sha1_mock.assert_not_called()


@pytest.mark.asyncio
async def test_match_single_file_dolphin_miss_skips_container_hash(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """A Dolphin disc-hash that misses the DAT is a definitive non-match; the
    meaningless container file-SHA1 fallback must be skipped."""
    from services.tools import registry

    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    rvz = tmp_path / "absent.rvz"
    rvz.write_bytes(b"fake rvz")

    # Disc hash computed fine but not present in the DAT.
    monkeypatch.setattr(
        registry.get("dolphin")._service,
        "disc_hashes",
        AsyncMock(return_value=["dead" * 10]),
    )
    file_sha1_mock = AsyncMock(return_value="beef" * 10)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", file_sha1_mock)

    result = await dat_routes._match_single_file(str(rvz))

    assert result["matched"] is False
    assert "error" not in result and "reason" not in result  # cacheable
    file_sha1_mock.assert_not_called()


@pytest.mark.asyncio
async def test_match_single_file_chd_hash_miss_falls_back_to_file_sha1(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """A CHD whose cached header/data SHA1 miss the DAT still falls back to the
    .chd file-level SHA1 (a CHD-container DAT may index it): chdman is not
    'exhaustive', unlike Dolphin."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake chd")

    mock_metadata_store = MagicMock()
    mock_metadata_store.is_stale = AsyncMock(return_value=False)
    # Cached hashes that are NOT in the imported DAT.
    mock_metadata_store.get_metadata = AsyncMock(
        return_value={"sha1": "1" * 40, "data_sha1": "2" * 40}
    )
    target_sha1 = "aabbccddaabbccddaabbccddaabbccddaabbccdd"
    monkeypatch.setattr(dat_routes, "compute_file_sha1", AsyncMock(return_value=target_sha1))

    with patch("services.chd_metadata_store.chd_metadata_store", mock_metadata_store):
        result = await dat_routes._match_single_file(str(chd))

    assert result["matched"] is True
    assert result["match_type"] == "file_sha1"


@pytest.mark.asyncio
async def test_match_single_file_uncached_chd_falls_back_to_file_sha1(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """An uncached CHD has no embedded candidates, so it uses the file-level
    SHA1 fallback (rather than being reported hash-unavailable)."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    chd = tmp_path / "fresh.chd"
    chd.write_bytes(b"fake chd")

    mock_metadata_store = MagicMock()
    mock_metadata_store.is_stale = AsyncMock(return_value=True)  # no row -> stale
    mock_metadata_store.get_metadata = AsyncMock(return_value=None)

    target_sha1 = "aabbccddaabbccddaabbccddaabbccddaabbccdd"
    monkeypatch.setattr(dat_routes, "compute_file_sha1", AsyncMock(return_value=target_sha1))

    with patch("services.chd_metadata_store.chd_metadata_store", mock_metadata_store):
        result = await dat_routes._match_single_file(str(chd))

    assert result["matched"] is True
    assert result["match_type"] == "file_sha1"


# ---------------------------------------------------------------------------
# Path normalization in match_batch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_match_batch_normalizes_paths(tmp_path, isolated_dat_store, monkeypatch):
    """match-batch normalizes paths before cache lookup and file checks."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    iso = tmp_path / "game.iso"
    iso.write_bytes(b"x")
    normal_path = os.path.normpath(os.path.abspath(str(iso)))
    # Provide a non-normalized variant (double slash)
    unnorm_path = str(iso).replace(str(tmp_path), str(tmp_path) + "/")

    target_sha1 = "aabbccddaabbccddaabbccddaabbccddaabbccdd"
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", AsyncMock(return_value=target_sha1))

    request = dat_routes.MatchBatchRequest(paths=[unnorm_path])
    result = await dat_routes.match_batch(request)

    # Result should be accessible via the original (input) key
    assert result["results"][unnorm_path]["matched"] is True
    # The path field in the result should be the normalized form
    assert result["results"][unnorm_path]["path"] == normal_path


@pytest.mark.asyncio
async def test_match_batch_duplicate_normalized_paths(tmp_path, isolated_dat_store, monkeypatch):
    """Two input paths that normalize to the same file share a single result."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    iso = tmp_path / "game.iso"
    iso.write_bytes(b"x")
    normal_path = os.path.normpath(os.path.abspath(str(iso)))
    # Two spelling variants that normalize to the same path
    variant_a = normal_path
    variant_b = str(iso).replace(str(tmp_path), str(tmp_path) + "/")

    target_sha1 = "aabbccddaabbccddaabbccddaabbccddaabbccdd"
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    compute_mock = AsyncMock(return_value=target_sha1)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", compute_mock)

    request = dat_routes.MatchBatchRequest(paths=[variant_a, variant_b])
    result = await dat_routes.match_batch(request)

    # Both original keys should appear in results
    assert result["results"][variant_a]["matched"] is True
    assert result["results"][variant_b]["matched"] is True
    # SHA1 should only have been computed once (shared result)
    assert compute_mock.call_count == 1


# ---------------------------------------------------------------------------
# Security: symlink-based path traversal and symlink loops
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_match_file_symlink_escaping_volume_denied(tmp_path, monkeypatch):
    """Symlink inside a configured volume that resolves outside the volume is rejected with 403."""
    from config import settings as app_settings

    volume = tmp_path / "volume"
    volume.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.iso"
    target.write_bytes(b"secret content")

    # Symlink inside the volume that points to a file outside the volume
    link = volume / "link.iso"
    link.symlink_to(target)

    # Limit configured volumes to only `volume`; the resolved target lies outside it
    monkeypatch.setattr(app_settings, "chd_volumes", str(volume))
    request = dat_routes.MatchRequest(path=str(link))
    with pytest.raises(HTTPException) as exc_info:
        await dat_routes.match_file(request)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_match_file_symlink_loop_returns_4xx_not_500(tmp_path, monkeypatch):
    """A symlink loop is rejected with a 4xx status, not an unhandled 500."""
    from config import settings as app_settings

    volume = tmp_path / "volume"
    volume.mkdir()

    # Create a self-referential symlink loop inside the volume
    loop = volume / "loop.iso"
    loop.symlink_to(loop)

    monkeypatch.setattr(app_settings, "chd_volumes", str(volume))
    request = dat_routes.MatchRequest(path=str(loop))
    with pytest.raises(HTTPException) as exc_info:
        await dat_routes.match_file(request)
    # os.path.realpath returns the unresolved symlink path for a loop (handles
    # ELOOP without raising). is_within_configured_volumes then calls
    # _resolve_path → Path.resolve() raises RuntimeError → caught → returns
    # None → volume check returns False → 403 (not an unhandled 500).
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_match_batch_symlink_escaping_volume_denied(
    tmp_path, isolated_dat_store, monkeypatch
):
    """Batch: symlink inside volume resolving outside is denied per-path with an error."""
    from config import settings as app_settings

    # Load a DAT so the batch endpoint proceeds past the early-exit check
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    volume = tmp_path / "volume"
    volume.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.iso"
    target.write_bytes(b"secret content")

    link = volume / "link.iso"
    link.symlink_to(target)

    monkeypatch.setattr(app_settings, "chd_volumes", str(volume))
    request = dat_routes.MatchBatchRequest(paths=[str(link)])
    result = await dat_routes.match_batch(request)

    path_result = result["results"][str(link)]
    assert path_result["matched"] is False
    assert "access denied" in path_result.get("error", "").lower()


@pytest.mark.asyncio
async def test_match_batch_symlink_loop_returns_denied_not_500(
    tmp_path, isolated_dat_store, monkeypatch
):
    """Batch: a symlink loop path is denied per-path, not an unhandled 500."""
    from config import settings as app_settings

    # Load a DAT so the batch endpoint proceeds past the early-exit check
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    volume = tmp_path / "volume"
    volume.mkdir()

    loop = volume / "loop.iso"
    loop.symlink_to(loop)

    monkeypatch.setattr(app_settings, "chd_volumes", str(volume))
    request = dat_routes.MatchBatchRequest(paths=[str(loop)])
    result = await dat_routes.match_batch(request)

    # Must not raise; the symlink loop is caught by is_within_configured_volumes:
    # os.path.realpath returns the unresolved symlink path (handles ELOOP
    # gracefully), then is_within_configured_volumes calls _resolve_path →
    # Path.resolve() raises RuntimeError for the loop → caught → returns None
    # → volume check returns False → access denied.
    assert str(loop) in result["results"]
    path_result = result["results"][str(loop)]
    assert path_result["matched"] is False
    assert "access denied" in path_result.get("error", "").lower()


# ---------------------------------------------------------------------------
# /dat/sync  /dat/sync/status  /dat/sync/cancel
# ---------------------------------------------------------------------------

def _make_mock_request():
    """Return a minimal mock HTTP Request with app.state.background_tasks."""
    mock_req = MagicMock()
    mock_req.app.state.background_tasks = set()
    return mock_req


@pytest.mark.asyncio
async def test_sync_mameredump_starts_sync(monkeypatch):
    """POST /dat/sync starts a background sync and returns status=started."""
    mock_svc = MagicMock()
    mock_svc.is_syncing = False
    mock_svc.sync = AsyncMock(return_value={"status": "complete"})
    monkeypatch.setattr(dat_routes, "_get_sync_service", lambda: mock_svc)

    result = await dat_routes.sync_mameredump(http_request=_make_mock_request(), request=None)
    assert result["status"] == "started"


@pytest.mark.asyncio
async def test_sync_mameredump_409_on_race_condition(monkeypatch):
    """POST /dat/sync returns 409 when the background task detects a concurrent sync."""
    mock_svc = MagicMock()
    mock_svc.is_syncing = False  # passes early check
    mock_svc.sync = AsyncMock(side_effect=RuntimeError("Sync already in progress"))
    monkeypatch.setattr(dat_routes, "_get_sync_service", lambda: mock_svc)

    with pytest.raises(HTTPException) as exc_info:
        await dat_routes.sync_mameredump(http_request=_make_mock_request(), request=None)
    assert exc_info.value.status_code == 409

@pytest.mark.asyncio
async def test_sync_mameredump_409_when_already_syncing(monkeypatch):
    """POST /dat/sync returns 409 when a sync is already in progress."""
    mock_svc = MagicMock()
    mock_svc.is_syncing = True
    monkeypatch.setattr(dat_routes, "_get_sync_service", lambda: mock_svc)

    with pytest.raises(HTTPException) as exc_info:
        await dat_routes.sync_mameredump(http_request=_make_mock_request(), request=None)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_sync_status_returns_shape(monkeypatch):
    """GET /dat/sync/status returns a dict with expected fields."""
    mock_svc = MagicMock()
    mock_svc.get_status = MagicMock(return_value={
        "syncing": False,
        "progress": {"status": "complete", "files_imported": 3},
        "last_sync_tag": "0.285",
        "last_sync_at": "2026-04-01T00:00:00Z",
        "last_sync_files": 3,
        "error": "",
    })
    monkeypatch.setattr(dat_routes, "_get_sync_service", lambda: mock_svc)

    result = await dat_routes.sync_status()
    assert result["syncing"] is False
    assert result["last_sync_tag"] == "0.285"
    assert result["progress"]["files_imported"] == 3


@pytest.mark.asyncio
async def test_sync_cancel_success(monkeypatch):
    """POST /dat/sync/cancel returns status=cancelling when sync is running."""
    mock_svc = MagicMock()
    mock_svc.cancel = MagicMock(return_value=True)
    monkeypatch.setattr(dat_routes, "_get_sync_service", lambda: mock_svc)

    result = await dat_routes.sync_cancel()
    assert result["status"] == "cancelling"


@pytest.mark.asyncio
async def test_sync_cancel_409_when_not_syncing(monkeypatch):
    """POST /dat/sync/cancel returns 409 when no sync is in progress."""
    mock_svc = MagicMock()
    mock_svc.cancel = MagicMock(return_value=False)
    monkeypatch.setattr(dat_routes, "_get_sync_service", lambda: mock_svc)

    with pytest.raises(HTTPException) as exc_info:
        await dat_routes.sync_cancel()
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_sync_mameredump_passes_tag(monkeypatch):
    """POST /dat/sync forwards the tag from the request body."""
    mock_svc = MagicMock()
    mock_svc.is_syncing = False
    mock_svc.sync = AsyncMock(return_value={"status": "complete"})
    monkeypatch.setattr(dat_routes, "_get_sync_service", lambda: mock_svc)

    await dat_routes.sync_mameredump(
        http_request=_make_mock_request(),
        request=dat_routes.SyncRequest(tag="0.285"),
    )
    # Give the background task a chance to start
    await asyncio.sleep(0)
    mock_svc.sync.assert_called_once_with(tag="0.285", force=False)


# ---------------------------------------------------------------------------
# Match concurrency + size cap (Deliverable 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_match_skips_oversized_file(tmp_path, isolated_dat_store, monkeypatch):
    """When MATCH_MAX_FILE_SIZE is set, files larger than the cap are
    reported as unmatched *without* running the SHA1 hasher."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    huge = tmp_path / "huge.iso"
    huge.write_bytes(b"x" * 1024)

    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    # Tiny cap (10 bytes) guarantees the 1 KB file trips it.
    monkeypatch.setattr(dat_routes.settings, "match_max_file_size", 10)

    hash_mock = AsyncMock(return_value="dead" * 10)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", hash_mock)

    request = dat_routes.MatchRequest(path=str(huge))
    result = await dat_routes.match_file(request)

    assert result["matched"] is False
    assert result.get("reason") == "file too large"
    # Critical: the hasher must NOT have been invoked.
    hash_mock.assert_not_called()


@pytest.mark.asyncio
async def test_match_respects_concurrency_cap(tmp_path, isolated_dat_store, monkeypatch):
    """With MAX_MATCH_CONCURRENCY=1, simultaneous match_file calls
    execute compute_file_sha1 serially (never two at once)."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    # Rebuild the workload_limiter with match_limit=1 for this test.
    # (Default is already 1, but we set it explicitly so the test is
    # self-documenting and robust to future default changes.)
    # Use __class__ to reuse the exact class already loaded by dat_routes,
    # avoiding any duplicate module-singleton risk from a separate import.
    WorkloadLimiter = dat_routes.workload_limiter.__class__
    new_limiter = WorkloadLimiter(
        verify_limit=1, metadata_scan_limit=1, match_limit=1,
    )
    monkeypatch.setattr(dat_routes, "workload_limiter", new_limiter)

    active = 0
    peak = 0
    barrier = asyncio.Event()

    async def _fake_hash(path):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            # Wait until all callers are in-flight before returning, so
            # if the limiter is NOT serialising we'd observe peak > 1.
            await asyncio.wait_for(barrier.wait(), timeout=0.2)
        except asyncio.TimeoutError:
            # Expected: under serial execution, only one caller ever
            # enters this function at a time, so the barrier is never
            # set by anyone else.
            pass
        active -= 1
        return "a" * 40  # a sha1 that is NOT in the loaded DAT

    monkeypatch.setattr(dat_routes, "compute_file_sha1", _fake_hash)
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)

    paths = []
    for i in range(5):
        iso_path = tmp_path / f"f{i}.iso"
        iso_path.write_bytes(b"x")
        paths.append(iso_path)

    requests = [
        dat_routes.match_file(dat_routes.MatchRequest(path=str(path)))
        for path in paths
    ]
    await asyncio.gather(*requests)

    # Under match_limit=1 the hasher runs strictly one at a time.
    assert peak == 1, f"concurrency cap breached: peak={peak}"


@pytest.mark.asyncio
async def test_match_batch_job_creates_external_job_with_progress(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """match-batch/job registers an external job, hashes serially, writes
    per-path results into the match cache, and reports progress."""
    from fastapi import BackgroundTasks
    from services.job_manager import job_manager
    from models import JobStatus

    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)
    target_sha1 = "aabbccddaabbccddaabbccddaabbccddaabbccdd"

    files = []
    for i in range(3):
        iso = tmp_path / f"game{i}.iso"
        iso.write_bytes(b"x")
        files.append(str(iso))

    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    # Only one of three hashes returns a match, verifies counting.
    monkeypatch.setattr(
        dat_routes, "compute_file_sha1",
        AsyncMock(side_effect=[target_sha1, "deadbeef" * 5, "cafebabe" * 5]),
    )
    # Reset any leftover active-job sentinel from prior tests.
    monkeypatch.setattr(dat_routes, "_active_match_job_id", None)

    bg = BackgroundTasks()
    request = dat_routes.MatchBatchRequest(paths=files)
    response = await dat_routes.match_batch_job(request, bg)

    assert response["status"] == "started"
    job_id = response["job_id"]
    assert job_manager.jobs[job_id].mode.value == "dat_match"

    # Execute the registered background tasks (FastAPI would do this
    # after sending the response in production).
    for task in bg.tasks:
        await task()

    final_job = job_manager.get_job_for_lookup(job_id)
    assert final_job.status == JobStatus.COMPLETED
    assert final_job.progress == 100

    # All three paths should now be in the match cache.
    for p in files:
        cached = isolated_dat_store.get_match(os.path.realpath(p))
        assert cached is not None
    # Exactly one is matched.
    matched_count = sum(
        1 for p in files if isolated_dat_store.get_match(os.path.realpath(p)).get("matched")
    )
    assert matched_count == 1


@pytest.mark.asyncio
async def test_match_batch_job_cache_hits_short_circuit(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """When every path is already cached, no job is created (status=idle)."""
    from fastapi import BackgroundTasks

    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    iso = tmp_path / "cached.iso"
    iso.write_bytes(b"x")
    path = os.path.realpath(str(iso))
    await isolated_dat_store.set_match(path, {"path": path, "matched": True})

    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    compute_mock = AsyncMock()
    monkeypatch.setattr(dat_routes, "compute_file_sha1", compute_mock)
    monkeypatch.setattr(dat_routes, "_active_match_job_id", None)

    bg = BackgroundTasks()
    request = dat_routes.MatchBatchRequest(paths=[path])
    response = await dat_routes.match_batch_job(request, bg)

    assert response["status"] == "idle"
    assert response["results"][path]["matched"] is True
    assert bg.tasks == []
    compute_mock.assert_not_called()


@pytest.mark.asyncio
async def test_match_batch_job_concurrent_rejected(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """A second match-batch/job while one is active returns 409."""
    from fastapi import BackgroundTasks
    from services.job_manager import job_manager

    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    iso = tmp_path / "a.iso"
    iso.write_bytes(b"x")
    path = str(iso)

    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", AsyncMock(return_value="a" * 40))
    monkeypatch.setattr(dat_routes, "_active_match_job_id", None)

    bg1 = BackgroundTasks()
    response1 = await dat_routes.match_batch_job(
        dat_routes.MatchBatchRequest(paths=[path]), bg1,
    )
    assert response1["status"] == "started"
    job_id = response1["job_id"]
    # Do NOT run the background task yet, the job stays "processing",
    # so the second request should be rejected.
    assert job_manager.jobs[job_id].status.value == "processing"

    bg2 = BackgroundTasks()
    iso2 = tmp_path / "b.iso"
    iso2.write_bytes(b"x")
    with pytest.raises(HTTPException) as exc_info:
        await dat_routes.match_batch_job(
            dat_routes.MatchBatchRequest(paths=[str(iso2)]), bg2,
        )
    assert exc_info.value.status_code == 409

    # Drain the first job so subsequent tests aren't polluted.
    for task in bg1.tasks:
        await task()


@pytest.mark.asyncio
async def test_match_cache_lookup_is_read_only(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """/dat/matches/lookup never invokes the hasher, even for uncached paths."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    cached_iso = tmp_path / "cached.iso"
    cached_iso.write_bytes(b"x")
    cached_path = os.path.realpath(str(cached_iso))
    await isolated_dat_store.set_match(
        cached_path, {"path": cached_path, "matched": True, "game_name": "Hit"},
    )

    uncached_iso = tmp_path / "uncached.iso"
    uncached_iso.write_bytes(b"x")
    uncached_path = str(uncached_iso)

    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)

    def _boom(*_a, **_kw):
        raise AssertionError("hasher must not be called from cache-only lookup")

    monkeypatch.setattr(dat_routes, "compute_file_sha1", _boom)

    request = dat_routes.MatchCacheLookupRequest(paths=[cached_path, uncached_path])
    response = await dat_routes.match_cache_lookup(request)

    assert cached_path in response["results"]
    assert response["results"][cached_path]["game_name"] == "Hit"
    # Uncached path is simply absent from the results map, the frontend
    # leaves the pending sentinel in place until the job writes it.
    assert uncached_path not in response["results"]


@pytest.mark.asyncio
async def test_match_file_hit_respects_size_cap_off(tmp_path, isolated_dat_store, monkeypatch):
    """With MATCH_MAX_FILE_SIZE=0 (disabled), any file is hashed and matched."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    target_sha1 = "aabbccddaabbccddaabbccddaabbccddaabbccdd"
    iso = tmp_path / "any-size.iso"
    iso.write_bytes(b"whatever")

    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    monkeypatch.setattr(dat_routes.settings, "match_max_file_size", 0)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", AsyncMock(return_value=target_sha1))

    request = dat_routes.MatchRequest(path=str(iso))
    result = await dat_routes.match_file(request)

    assert result["matched"] is True


@pytest.mark.asyncio
async def test_match_file_oserror_returns_redacted_error(tmp_path, isolated_dat_store, monkeypatch):
    """OSError during hashing returns a constant error string, not the raw exception message."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    iso = tmp_path / "unreadable.iso"
    iso.write_bytes(b"data")

    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)

    internal_msg = "Permission denied: '/secret/path'"

    async def _raise_oserror(*_a, **_kw):
        raise OSError(internal_msg)

    monkeypatch.setattr(dat_routes, "compute_file_sha1", _raise_oserror)

    request = dat_routes.MatchRequest(path=str(iso))
    result = await dat_routes.match_file(request)

    assert result["matched"] is False
    assert result["error"] == "Unable to process file"
    assert internal_msg not in str(result)



# ---------------------------------------------------------------------------
# _run_match_job failure counting + error-level logging for broad excepts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_match_job_reports_errors_in_final_message(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """Mix of (matched, unmatched, error) outcomes, final message surfaces the error count."""
    from services.job_manager import job_manager

    monkeypatch.setattr(dat_routes, "_active_match_job_id", "manual-test-job")

    scan_job = job_manager.create_external_job(
        filename="DAT Match",
        mode=dat_routes.ConversionMode.DAT_MATCH,
        message="test",
    )
    monkeypatch.setattr(dat_routes, "_active_match_job_id", scan_job.id)

    hash_outcomes = [
        ({"path": "/a", "matched": True, "game_name": "A"}, True),
        ({"path": "/b", "matched": False}, True),
        ({"path": "/c", "matched": False, "error": "boom"}, False),
    ]

    async def fake_hash_one(path, *, cancel_event=None, local_only=False):
        return hash_outcomes.pop(0)

    monkeypatch.setattr(dat_routes, "_hash_one_for_job", fake_hash_one)

    await dat_routes._run_match_job(
        job_id=scan_job.id, paths_to_compute=["/a", "/b", "/c"],
    )

    final = job_manager.jobs[scan_job.id]
    assert final.status.value == "completed"
    assert "1 error(s)" in final.message
    assert "3/3 processed" in final.message


@pytest.mark.asyncio
async def test_run_match_job_marks_failure_when_all_files_error(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """When every file errors, the job is marked failed so the user sees a red signal."""
    from services.job_manager import job_manager

    scan_job = job_manager.create_external_job(
        filename="DAT Match",
        mode=dat_routes.ConversionMode.DAT_MATCH,
        message="test",
    )
    monkeypatch.setattr(dat_routes, "_active_match_job_id", scan_job.id)

    async def always_error(path, *, cancel_event=None, local_only=False):
        return {"path": path, "matched": False, "error": "mount offline"}, False

    monkeypatch.setattr(dat_routes, "_hash_one_for_job", always_error)

    await dat_routes._run_match_job(
        job_id=scan_job.id, paths_to_compute=["/a", "/b", "/c"],
    )

    final = job_manager.jobs[scan_job.id]
    assert final.status.value == "failed"
    assert "all 3 file(s) failed" in final.message
    assert "check volume accessibility" in final.message


@pytest.mark.asyncio
async def test_run_match_job_names_hasheous_instead_of_blaming_the_volume(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """A remote outage fails every file, but the volume is fine.

    Reporting "check volume accessibility" would send the operator off
    debugging storage for a network condition that recovers on its own.
    """
    from services.job_manager import job_manager

    scan_job = job_manager.create_external_job(
        filename="DAT Match",
        mode=dat_routes.ConversionMode.DAT_MATCH,
        message="test",
    )
    monkeypatch.setattr(dat_routes, "_active_match_job_id", scan_job.id)

    async def remote_down(path, *, cancel_event=None, local_only=False):
        return (
            {"path": path, "matched": False, "error": dat_routes.HASHEOUS_ERROR},
            False,
        )

    monkeypatch.setattr(dat_routes, "_hash_one_for_job", remote_down)

    await dat_routes._run_match_job(
        job_id=scan_job.id, paths_to_compute=["/a", "/b", "/c"],
    )

    final = job_manager.jobs[scan_job.id]
    assert final.status.value == "failed"
    assert "all 3 file(s) failed" in final.message
    assert "Hasheous is unreachable" in final.message
    assert "check volume accessibility" not in final.message


@pytest.mark.asyncio
async def test_run_match_job_mixed_errors_still_blame_the_volume(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """Only an all-Hasheous failure is attributable to Hasheous."""
    from services.job_manager import job_manager

    scan_job = job_manager.create_external_job(
        filename="DAT Match",
        mode=dat_routes.ConversionMode.DAT_MATCH,
        message="test",
    )
    monkeypatch.setattr(dat_routes, "_active_match_job_id", scan_job.id)

    seen = []

    async def mixed(path, *, cancel_event=None, local_only=False):
        seen.append(path)
        error = dat_routes.HASHEOUS_ERROR if len(seen) == 1 else "mount offline"
        return {"path": path, "matched": False, "error": error}, False

    monkeypatch.setattr(dat_routes, "_hash_one_for_job", mixed)

    await dat_routes._run_match_job(
        job_id=scan_job.id, paths_to_compute=["/a", "/b"],
    )

    final = job_manager.jobs[scan_job.id]
    assert final.status.value == "failed"
    assert "check volume accessibility" in final.message


@pytest.mark.asyncio
async def test_run_match_job_outer_exception_includes_counter_context(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """When the outer except fires mid-loop, the final error message must
    include the processed/errors/skips counts so operators know how far
    the job got before the fault, not just the raw exception string.
    """
    from services.job_manager import job_manager

    scan_job = job_manager.create_external_job(
        filename="DAT Match",
        mode=dat_routes.ConversionMode.DAT_MATCH,
        message="test",
    )
    monkeypatch.setattr(dat_routes, "_active_match_job_id", scan_job.id)

    # First two iterations: one success, one error.  Third iteration:
    # update_external_job raises, tripping the outer except.
    hash_calls = 0

    async def fake_hash_one(path, *, cancel_event=None, local_only=False):
        nonlocal hash_calls
        hash_calls += 1
        if hash_calls == 1:
            return {"path": path, "matched": True}, True
        return {"path": path, "matched": False, "error": "transient"}, False

    monkeypatch.setattr(dat_routes, "_hash_one_for_job", fake_hash_one)

    update_calls = 0
    original_update = dat_routes.job_manager.update_external_job

    async def flaky_update(job_id, **kwargs):
        nonlocal update_calls
        update_calls += 1
        # Fail exactly on the 3rd call (the 3rd loop iteration's progress
        # tick) so the outer except trips mid-loop. Calls 4 and 5 (the
        # finally's progress update + any internal fan-out) go through.
        if update_calls == 3:
            raise RuntimeError("update API down")
        return await original_update(job_id, **kwargs)

    monkeypatch.setattr(dat_routes.job_manager, "update_external_job", flaky_update)

    await dat_routes._run_match_job(
        job_id=scan_job.id, paths_to_compute=["/a", "/b", "/c", "/d"],
    )

    final = job_manager.jobs[scan_job.id]
    assert final.status.value == "failed"
    # Counter context must be in the error message for operator visibility.
    assert "processed 2/4" in final.message
    assert "1 error(s)" in final.message
    assert "update API down" in final.message


@pytest.mark.asyncio
async def test_run_match_job_skip_count_does_not_trip_failure(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """Size-cap / non-regular skips are NOT errors, they don't flip job_success."""
    from services.job_manager import job_manager

    scan_job = job_manager.create_external_job(
        filename="DAT Match",
        mode=dat_routes.ConversionMode.DAT_MATCH,
        message="test",
    )
    monkeypatch.setattr(dat_routes, "_active_match_job_id", scan_job.id)

    hash_outcomes = [
        ({"path": "/a", "matched": True}, True),
        # Size-cap skip: cacheable=False but no "error" key, pure policy outcome.
        ({"path": "/b", "matched": False, "reason": "file too large"}, False),
        ({"path": "/c", "matched": False}, False),  # non-regular file shape
    ]

    async def fake_hash_one(path, *, cancel_event=None, local_only=False):
        return hash_outcomes.pop(0)

    monkeypatch.setattr(dat_routes, "_hash_one_for_job", fake_hash_one)

    await dat_routes._run_match_job(
        job_id=scan_job.id, paths_to_compute=["/a", "/b", "/c"],
    )

    final = job_manager.jobs[scan_job.id]
    assert final.status.value == "completed"
    assert "2 skipped" in final.message
    assert "error" not in final.message.lower()


@pytest.mark.asyncio
async def test_run_match_job_cache_write_failure_counts_as_error(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """A raising dat_store.set_match is captured as an error, not silently swallowed."""
    from services.job_manager import job_manager

    scan_job = job_manager.create_external_job(
        filename="DAT Match",
        mode=dat_routes.ConversionMode.DAT_MATCH,
        message="test",
    )
    monkeypatch.setattr(dat_routes, "_active_match_job_id", scan_job.id)

    async def fake_hash_one(path, *, cancel_event=None, local_only=False):
        return {"path": path, "matched": True}, True

    monkeypatch.setattr(dat_routes, "_hash_one_for_job", fake_hash_one)

    async def failing_set_match(*_a, **_kw):
        raise RuntimeError("DB is down")

    monkeypatch.setattr(dat_routes.dat_store, "set_match", failing_set_match)

    await dat_routes._run_match_job(
        job_id=scan_job.id, paths_to_compute=["/a", "/b"],
    )

    final = job_manager.jobs[scan_job.id]
    # Every cache write failed; every result was "cacheable" so errors == total → fail.
    assert final.status.value == "failed"
    assert "all 2 file(s) failed" in final.message


@pytest.mark.asyncio
async def test_run_match_job_cancellation_ends_in_cancelled_status(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """Mid-loop cancellation flips the job to CANCELLED (not FAILED) and
    preserves the partial cache for files already hashed before cancel."""
    from services.job_manager import job_manager

    scan_job = job_manager.create_external_job(
        filename="DAT Match",
        mode=dat_routes.ConversionMode.DAT_MATCH,
        message="test",
    )
    monkeypatch.setattr(dat_routes, "_active_match_job_id", scan_job.id)

    # After the 2nd hash completes, request cancel.  The 3rd iteration's
    # cancel-check at the top of the loop then fires ExternalJobCancelled.
    hash_calls = 0

    async def fake_hash_one(path, *, cancel_event=None, local_only=False):
        nonlocal hash_calls
        hash_calls += 1
        if hash_calls == 2:
            await job_manager.cancel_job(scan_job.id)
        return {"path": path, "matched": True, "game_name": "X"}, True

    monkeypatch.setattr(dat_routes, "_hash_one_for_job", fake_hash_one)

    await dat_routes._run_match_job(
        job_id=scan_job.id, paths_to_compute=["/a", "/b", "/c", "/d"],
    )

    final = job_manager.jobs[scan_job.id]
    assert final.status.value == "cancelled"
    assert "Cancelled" in final.message
    assert "2/4 processed" in final.message
    # Match-job lock must be released so a subsequent job isn't blocked.
    assert dat_routes._active_match_job_id is None


@pytest.mark.asyncio
async def test_run_match_job_cancellation_keeps_partial_cache(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """Per-file cache writes already made before cancel must remain in the
    dat_store, the cache is intentionally durable across cancellation."""
    from services.job_manager import job_manager

    scan_job = job_manager.create_external_job(
        filename="DAT Match",
        mode=dat_routes.ConversionMode.DAT_MATCH,
        message="test",
    )
    monkeypatch.setattr(dat_routes, "_active_match_job_id", scan_job.id)

    hash_calls = 0

    async def fake_hash_one(path, *, cancel_event=None, local_only=False):
        nonlocal hash_calls
        hash_calls += 1
        if hash_calls == 2:
            await job_manager.cancel_job(scan_job.id)
        return {"path": path, "matched": True, "game_name": "X"}, True

    monkeypatch.setattr(dat_routes, "_hash_one_for_job", fake_hash_one)

    await dat_routes._run_match_job(
        job_id=scan_job.id, paths_to_compute=["/a", "/b", "/c"],
    )

    # Both /a and /b finished hashing before cancel → both cached.
    assert isolated_dat_store.get_match("/a") is not None
    assert isolated_dat_store.get_match("/b") is not None
    # /c never made it past the cancel check.
    assert isolated_dat_store.get_match("/c") is None


@pytest.mark.asyncio
async def test_run_match_job_cancel_on_last_file_ends_cancelled(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """A cancel observed only on the last/only path (via a non-raising
    cancellable hook) still finalizes the job as cancelled, not complete."""
    from services.job_manager import job_manager

    scan_job = job_manager.create_external_job(
        filename="DAT Match",
        mode=dat_routes.ConversionMode.DAT_MATCH,
        message="test",
    )
    monkeypatch.setattr(dat_routes, "_active_match_job_id", scan_job.id)

    async def fake_hash_one(path, *, cancel_event=None, local_only=False):
        # Simulate an aborted cancellable hook on the only path: cancel is
        # requested during the call and a non-cacheable error is returned.
        await job_manager.cancel_job(scan_job.id)
        return {"path": path, "matched": False, "error": "embedded hash unavailable"}, False

    monkeypatch.setattr(dat_routes, "_hash_one_for_job", fake_hash_one)

    await dat_routes._run_match_job(job_id=scan_job.id, paths_to_compute=["/only"])

    final = job_manager.jobs[scan_job.id]
    assert final.status.value == "cancelled"
    assert dat_routes._active_match_job_id is None


@pytest.mark.asyncio
async def test_try_embedded_hash_match_exhaustive_tool_raises_on_failure(tmp_path):
    """An unexpected hook failure on an exhaustive tool surfaces as
    EmbeddedHashUnavailable (no file-level fallback / false-negative cache)."""
    from services.tools.base import EmbeddedHashUnavailable

    class _ExhaustiveBoom:
        embedded_hash_is_exhaustive = True

        async def embedded_hashes(self, path, *, cancel_event=None):
            raise RuntimeError("boom")

    with pytest.raises(EmbeddedHashUnavailable):
        await dat_routes._try_embedded_hash_match(str(tmp_path / "x.rvz"), _ExhaustiveBoom())


@pytest.mark.asyncio
async def test_try_embedded_hash_match_non_exhaustive_tool_falls_back(tmp_path):
    """A non-exhaustive tool's unexpected failure falls back (no candidates)."""

    class _NonExhaustiveBoom:
        embedded_hash_is_exhaustive = False

        async def embedded_hashes(self, path, *, cancel_event=None):
            raise RuntimeError("boom")

    result, candidates = await dat_routes._try_embedded_hash_match(
        str(tmp_path / "x.chd"), _NonExhaustiveBoom(),
    )
    assert result is None
    assert not candidates


@pytest.mark.asyncio
async def test_hash_one_for_job_logs_match_error_with_traceback(
    tmp_path, isolated_dat_store, monkeypatch, caplog,
):
    """A KeyError from _match_single_file emerges as ERROR-level with exc_info."""
    iso = tmp_path / "a.iso"
    iso.write_bytes(b"x")

    async def raise_keyerror(_path, *, cancel_event=None, local_only=False):
        raise KeyError("missing_column")

    monkeypatch.setattr(dat_routes, "_match_single_file", raise_keyerror)

    with caplog.at_level("ERROR", logger="compressatorium.dat"):
        result, cacheable = await dat_routes._hash_one_for_job(str(iso))

    assert result["matched"] is False
    assert result["error"] == "'missing_column'"
    assert cacheable is False
    error_records = [r for r in caplog.records if r.levelname == "ERROR" and r.exc_info]
    assert error_records, "expected ERROR-level log with traceback, got: " + str(caplog.records)
    assert "DAT match failed" in error_records[0].message


# ---------------------------------------------------------------------------
# schedule_match_job, reusable entry-point for HTTP + post-sync rematch hook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_match_job_returns_none_on_empty_paths(monkeypatch):
    """Empty path list short-circuits before touching the job manager."""
    from services.job_manager import job_manager

    monkeypatch.setattr(dat_routes, "_active_match_job_id", None)

    before = set(job_manager.jobs.keys())
    result = await dat_routes.schedule_match_job([])
    after = set(job_manager.jobs.keys())

    assert result is None
    assert before == after  # No new job created.


@pytest.mark.asyncio
async def test_schedule_match_job_returns_none_when_active(monkeypatch):
    """Concurrency guard: a non-None _active_match_job_id blocks a new job."""
    monkeypatch.setattr(dat_routes, "_active_match_job_id", "already-running")
    # Bypass the volume ACL so the return None can only be attributed to
    # the concurrency guard, not to a "no admitted paths" short-circuit.
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)

    result = await dat_routes.schedule_match_job(["/tmp/x.chd"])

    assert result is None


@pytest.mark.asyncio
async def test_schedule_match_job_returns_none_when_all_paths_denied(monkeypatch, caplog):
    """Every path fails the volume ACL → no job created, warning logged."""
    from services.job_manager import job_manager

    monkeypatch.setattr(dat_routes, "_active_match_job_id", None)
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: False)

    before = set(job_manager.jobs.keys())
    with caplog.at_level("WARNING", logger="compressatorium.dat"):
        result = await dat_routes.schedule_match_job(["/x", "/y", "/z"])
    after = set(job_manager.jobs.keys())

    assert result is None
    assert before == after  # No job created when allow-list is empty.
    assert any("dropped 3 path(s) outside configured volumes" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_schedule_match_job_filters_denied_paths_but_schedules_remainder(
    tmp_path, isolated_dat_store, monkeypatch, caplog,
):
    """Mix of allowed and denied paths: denied are dropped, job runs on the rest."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    allowed_file = tmp_path / "allowed.iso"
    allowed_file.write_bytes(b"x")
    allowed_path = str(allowed_file)

    monkeypatch.setattr(dat_routes, "_active_match_job_id", None)

    def _acl(path: str) -> bool:
        return "allowed" in path

    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", _acl)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", AsyncMock(return_value="a" * 40))

    captured: list[asyncio.Task] = []
    real_create_task = asyncio.create_task
    monkeypatch.setattr(
        dat_routes.asyncio, "create_task",
        lambda coro: captured.append(real_create_task(coro)) or captured[-1],
    )

    with caplog.at_level("WARNING", logger="compressatorium.dat"):
        job_id = await dat_routes.schedule_match_job(
            [allowed_path, "/tmp/denied-a", "/tmp/denied-b"],
        )

    assert isinstance(job_id, str)
    assert any("dropped 2 path(s) outside configured volumes" in r.message for r in caplog.records)
    # Drain the scheduled task so it doesn't pollute later tests.
    if captured:
        await asyncio.wait_for(captured[0], timeout=5)
    # Give the done-callback one extra loop tick to discard from the set.
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_schedule_match_job_rolls_back_active_id_on_scheduling_failure(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """If asyncio.create_task raises after _active_match_job_id was set,
    the helper must roll the id back so subsequent match jobs aren't
    permanently locked out."""
    from services.job_manager import job_manager

    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    iso = tmp_path / "a.iso"
    iso.write_bytes(b"x")
    path = str(iso)

    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    monkeypatch.setattr(dat_routes, "_active_match_job_id", None)

    def _explode(_coro):
        # Close the coroutine so Python doesn't warn about it being
        # unawaited, we are deliberately short-circuiting before run.
        _coro.close()
        raise RuntimeError("no running loop")

    monkeypatch.setattr(dat_routes.asyncio, "create_task", _explode)

    with pytest.raises(RuntimeError, match="no running loop"):
        await dat_routes.schedule_match_job([path])

    # Lock rolled back → future calls are not 409'd.
    assert dat_routes._active_match_job_id is None

    # The phantom external job was finished with success=False. The job
    # still exists in job_manager (for history) but is not in-progress.
    finished_jobs = [
        j for j in job_manager.jobs.values()
        if j.filename == "DAT Match" and j.status.value != "processing"
    ]
    assert finished_jobs, "phantom match job should be finalized after scheduling failure"


@pytest.mark.asyncio
async def test_schedule_match_job_uses_create_task_without_background_tasks(
    tmp_path, isolated_dat_store, monkeypatch,
):
    """With no BackgroundTasks the helper schedules the job via asyncio.create_task,
    verified by capturing the task and awaiting it to completion.
    """
    from services.job_manager import job_manager

    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    iso = tmp_path / "a.iso"
    iso.write_bytes(b"x")
    path = str(iso)

    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", AsyncMock(return_value="a" * 40))
    monkeypatch.setattr(dat_routes, "_active_match_job_id", None)

    # Capture the coroutine the helper hands to asyncio.create_task so we can
    # await it deterministically (avoids flaky sleep-loops while the loop
    # drains the background task's many thread-pool hops).
    created: list[asyncio.Task] = []
    real_create_task = asyncio.create_task

    def _capture(coro):
        task = real_create_task(coro)
        created.append(task)
        return task

    monkeypatch.setattr(dat_routes.asyncio, "create_task", _capture)

    # Baseline: the strong-ref set is empty before scheduling.
    assert len(dat_routes._background_match_tasks) == 0

    job_id = await dat_routes.schedule_match_job([path])

    assert isinstance(job_id, str) and job_id
    assert job_id in job_manager.jobs
    # The helper took the asyncio.create_task branch and retained the task in its
    # strong-ref set. (Registering the external job also spawns a job_manager
    # prune task through the same asyncio.create_task, so identify the match task
    # by the set it lives in rather than a raw count of created tasks.)
    assert len(dat_routes._background_match_tasks) == 1
    match_task = next(iter(dat_routes._background_match_tasks))
    assert match_task in created  # it was scheduled via create_task
    await asyncio.wait_for(match_task, timeout=5)
    # The done-callback runs on the next loop tick; yield once to drain it.
    await asyncio.sleep(0)
    assert dat_routes._active_match_job_id is None
    # The done-callback should have removed the task from the set.
    assert match_task not in dat_routes._background_match_tasks
    assert len(dat_routes._background_match_tasks) == 0
    # Drain any incidental tasks (e.g. the prune) so none are left pending.
    await asyncio.gather(*created, return_exceptions=True)
