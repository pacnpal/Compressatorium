"""Tests for the Hasheous remote hash-lookup fallback.

No HTTP mocking library is used (the suite has none): the seam is
``services.hasheous._fetch_json``, patched the same way
``tests/test_dat_sync.py`` patches ``sync_service._fetch_json``.
"""

import urllib.error
from unittest.mock import AsyncMock, patch

import pytest

# Import the SAME module object ``routes.dat`` holds, not ``app.services.hasheous``.
# Those are two distinct module objects under this suite's import layout, and
# patching the wrong one silently proves nothing (see tests/test_nsz_service.py).
from services import hasheous

from app.routes import dat as dat_routes
from config import settings

# A real ``GET /api/v1/Lookup/ByHash/sha1/{sha1}`` response, trimmed to the
# fields the normalizer reads. Values are verbatim from the live API, including
# the empty-dict ``country`` that some signature sources return instead of a
# string.
SAMPLE_RESPONSE = {
    "id": 13633,
    "name": "Jumpman Junior",
    "platform": {"name": "Commodore 64"},
    "publisher": {"name": "Epyx"},
    "signature": {
        "game": {
            "name": "Jumpman Junior",
            "year": "1983",
            "publisher": "Epyx",
            "country": {},
        },
        "rom": {
            "name": "Jumpman Junior (1983)(Epyx).bin",
            "size": 16384,
            "crc": "6e89d59c",
            "md5": "5d7550788a4d1b47ad81fbbbf5c615a9",
            "sha1": "274ed5c2ea2ddc855f67d4c4e61c9d9b7eb68403",
            "country": {},
            "signatureSource": "TOSEC",
        },
    },
    "metadata": [
        {
            "source": "IGDB",
            "id": "12296",
            "link": "https://www.igdb.com/games/jumpman-junior",
            "status": "Mapped",
        },
        {
            "source": "TheGamesDb",
            "id": "24326",
            "link": "https://thegamesdb.net/game.php?id=24326",
            "status": "Mapped",
        },
        {"source": "RetroAchievements", "id": "", "link": "", "status": "NotMapped"},
    ],
}

SAMPLE_SHA1 = "274ed5c2ea2ddc855f67d4c4e61c9d9b7eb68403"


def test_patches_the_module_the_routes_actually_use():
    """Guard for the import trap above.

    If these ever diverge, every ``patch.object(hasheous, ...)`` below would
    quietly stop affecting the match path -- and the tests would still pass
    while hitting the real network.
    """
    assert hasheous is dat_routes.hasheous


@pytest.fixture
def hasheous_on(monkeypatch):
    """Enable remote lookup for the duration of a test."""
    monkeypatch.setattr(settings, "hasheous_enabled", True)
    monkeypatch.setattr(settings, "hasheous_base_url", "https://hasheous.example")


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_normalizes_real_response(hasheous_on):
    with patch.object(hasheous, "_fetch_json", return_value=SAMPLE_RESPONSE):
        record = await hasheous.lookup(SAMPLE_SHA1)

    assert record["game_name"] == "Jumpman Junior"
    assert record["rom_name"] == "Jumpman Junior (1983)(Epyx).bin"
    # The originating preservation DAT, not the literal string "Hasheous".
    assert record["dat_name"] == "TOSEC"
    assert record["source"] == "hasheous"
    assert record["platform"] == "Commodore 64"
    assert record["publisher"] == "Epyx"
    assert record["year"] == "1983"
    assert record["hasheous_id"] == 13633
    # Empty-dict country normalizes to None rather than "{}" or a crash.
    assert record["region"] is None


@pytest.mark.asyncio
async def test_lookup_keeps_only_mapped_metadata_links(hasheous_on):
    with patch.object(hasheous, "_fetch_json", return_value=SAMPLE_RESPONSE):
        record = await hasheous.lookup(SAMPLE_SHA1)

    sources = [entry["source"] for entry in record["metadata_links"]]
    assert sources == ["IGDB", "TheGamesDb"]  # the NotMapped one is dropped


@pytest.mark.asyncio
async def test_lookup_dat_id_is_always_none(hasheous_on):
    """``dat_id`` is a FK into the local ``dats`` table; a remote hit has no row.

    Writing a non-null value here would either violate the FK or be silently
    nulled by dat_store, making the cached row differ between runs.
    """
    with patch.object(hasheous, "_fetch_json", return_value=SAMPLE_RESPONSE):
        record = await hasheous.lookup(SAMPLE_SHA1)

    assert record["dat_id"] is None


def test_normalize_survives_a_bare_response():
    """A hit with no signature block must not raise."""
    record = hasheous._normalize({"id": 1, "name": "Some Game"})

    assert record["game_name"] == "Some Game"
    assert record["rom_name"] is None
    assert record["dat_name"] == "Hasheous"  # fallback when no source is named
    assert record["metadata_links"] == []


def test_normalize_joins_a_country_map():
    record = hasheous._normalize(
        {"signature": {"rom": {"country": {"US": "United States", "EU": "Europe"}}}}
    )

    assert record["region"] == "United States, Europe"


# ---------------------------------------------------------------------------
# Transport: a miss is not a failure, and a failure is not a miss
# ---------------------------------------------------------------------------


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://hasheous.example", code, "boom", {}, None,
    )


@pytest.mark.asyncio
async def test_404_is_a_clean_miss(hasheous_on):
    """Hasheous answers an unknown hash with 404, not 200-and-null."""
    with patch.object(hasheous, "_fetch_json", return_value=None):
        assert await hasheous.lookup(SAMPLE_SHA1) is None


@pytest.mark.parametrize("code", [500, 502, 503, 429])
def test_non_404_http_errors_raise_unavailable(code, hasheous_on):
    with patch("urllib.request.urlopen", side_effect=_http_error(code)):
        with pytest.raises(hasheous.HasheousUnavailable):
            hasheous._fetch_json("https://hasheous.example/x")


def test_fetch_json_maps_404_to_none(hasheous_on):
    with patch("urllib.request.urlopen", side_effect=_http_error(404)):
        assert hasheous._fetch_json("https://hasheous.example/x") is None


def test_timeout_raises_unavailable(hasheous_on):
    with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        with pytest.raises(hasheous.HasheousUnavailable):
            hasheous._fetch_json("https://hasheous.example/x")


def test_url_error_raises_unavailable(hasheous_on):
    with patch(
        "urllib.request.urlopen", side_effect=urllib.error.URLError("no route"),
    ), pytest.raises(hasheous.HasheousUnavailable):
        hasheous._fetch_json("https://hasheous.example/x")


class _Resp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, _n=None):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def test_unparseable_body_raises_unavailable(hasheous_on):
    with patch("urllib.request.urlopen", return_value=_Resp(b"<html>nope</html>")):
        with pytest.raises(hasheous.HasheousUnavailable):
            hasheous._fetch_json("https://hasheous.example/x")


def test_non_object_body_raises_unavailable(hasheous_on):
    """A bare list is not the documented shape; refuse rather than guess."""
    with patch("urllib.request.urlopen", return_value=_Resp(b"[1, 2, 3]")):
        with pytest.raises(hasheous.HasheousUnavailable):
            hasheous._fetch_json("https://hasheous.example/x")


def test_oversized_body_raises_unavailable(hasheous_on):
    huge = b"x" * (hasheous._MAX_RESPONSE_BYTES + 1)
    with patch("urllib.request.urlopen", return_value=_Resp(huge)):
        with pytest.raises(hasheous.HasheousUnavailable):
            hasheous._fetch_json("https://hasheous.example/x")


def test_plain_http_is_refused(monkeypatch):
    """File hashes must not go out in the clear."""
    monkeypatch.setattr(settings, "hasheous_base_url", "http://hasheous.example")
    with pytest.raises(ValueError, match="https"):
        hasheous._fetch_json("http://hasheous.example/x")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "not-a-hash", "ZZ" * 20, "abc"])
async def test_non_sha1_input_never_reaches_the_network(bad, hasheous_on):
    with patch.object(hasheous, "_fetch_json") as fetch:
        assert await hasheous.lookup(bad) is None
    fetch.assert_not_called()


@pytest.mark.asyncio
async def test_lookup_url_shape(hasheous_on):
    with patch.object(hasheous, "_fetch_json", return_value=None) as fetch:
        await hasheous.lookup(SAMPLE_SHA1.upper())

    fetch.assert_called_once_with(
        f"https://hasheous.example/api/v1/Lookup/ByHash/sha1/{SAMPLE_SHA1}"
    )


# ---------------------------------------------------------------------------
# Wiring into the match path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_by_default_makes_no_call(monkeypatch):
    monkeypatch.setattr(settings, "hasheous_enabled", False)
    monkeypatch.setattr(
        dat_routes, "_local_dat_record", AsyncMock(return_value=None),
    )
    with patch.object(hasheous, "lookup", new=AsyncMock()) as remote:
        result = await dat_routes._lookup_sha1_match("/x.iso", SAMPLE_SHA1, "file_sha1")

    assert result is None
    remote.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_hit_short_circuits_the_remote_lookup(hasheous_on):
    """Local-first keeps a covered library fully offline."""
    local = {
        "dat_id": "abc", "dat_name": "Local DAT", "game_name": "Local Game",
        "rom_name": "local.iso", "source": "dat",
    }
    with patch.object(dat_routes, "_local_dat_record", AsyncMock(return_value=local)):
        with patch.object(hasheous, "lookup", new=AsyncMock()) as remote:
            result = await dat_routes._lookup_sha1_match(
                "/x.iso", SAMPLE_SHA1, "file_sha1",
            )

    assert result["source"] == "dat"
    assert result["dat_id"] == "abc"
    remote.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_fallback_on_local_miss(hasheous_on):
    with patch.object(dat_routes, "_local_dat_record", AsyncMock(return_value=None)):
        with patch.object(hasheous, "_fetch_json", return_value=SAMPLE_RESPONSE):
            result = await dat_routes._lookup_sha1_match(
                "/x.iso", SAMPLE_SHA1, "file_sha1",
            )

    assert result["matched"] is True
    assert result["source"] == "hasheous"
    assert result["path"] == "/x.iso"
    assert result["match_type"] == "file_sha1"
    assert result["file_hash"] == SAMPLE_SHA1
    assert result["dat_id"] is None


@pytest.mark.asyncio
async def test_transient_failure_is_not_cached_as_unmatched(hasheous_on, monkeypatch):
    """The whole point of HasheousUnavailable.

    A network blip must produce a result carrying ``error`` (which the caller
    refuses to cache), never ``matched: False`` -- otherwise one bad minute
    permanently marks every in-flight file as being in no DAT.
    """
    monkeypatch.setattr(
        dat_routes.dat_store, "has_dats", lambda: False,
    )
    monkeypatch.setattr(
        dat_routes, "compute_file_sha1", AsyncMock(return_value=SAMPLE_SHA1),
    )
    monkeypatch.setattr(dat_routes, "_local_dat_record", AsyncMock(return_value=None))
    monkeypatch.setattr(
        hasheous, "lookup",
        AsyncMock(side_effect=hasheous.HasheousUnavailable("timed out")),
    )

    result = await dat_routes._match_single_file("/x.iso")

    assert result["matched"] is False
    assert result["error"] == "hasheous unavailable"


@pytest.mark.asyncio
async def test_match_reaches_hasheous_with_no_dats_imported(hasheous_on, monkeypatch):
    """The has_dats gates used to short-circuit before the remote lookup."""
    monkeypatch.setattr(dat_routes.dat_store, "has_dats", lambda: False)
    monkeypatch.setattr(
        dat_routes, "compute_file_sha1", AsyncMock(return_value=SAMPLE_SHA1),
    )
    monkeypatch.setattr(dat_routes, "_local_dat_record", AsyncMock(return_value=None))
    with patch.object(hasheous, "_fetch_json", return_value=SAMPLE_RESPONSE):
        result = await dat_routes._match_single_file("/x.iso")

    assert result["matched"] is True
    assert result["game_name"] == "Jumpman Junior"


@pytest.fixture
def scan_phase_stubs(monkeypatch):
    """Stub the job-manager plumbing ``_scan_phase_dat_match`` talks to."""
    from routes import info as info_routes

    async def _noop_update(_job_id, **_kw):
        return None

    monkeypatch.setattr(info_routes.job_manager, "update_external_job", _noop_update)
    monkeypatch.setattr(info_routes.job_manager, "is_cancelled", lambda _j: False)
    monkeypatch.setattr(info_routes.job_manager, "get_cancel_event", lambda _j: None)
    return info_routes


@pytest.mark.asyncio
async def test_scan_phase3_skips_when_the_dat_store_is_broken(
    hasheous_on, scan_phase_stubs, monkeypatch,
):
    """Enabling Hasheous must not turn a dead DB into a failed scan.

    Phase 3 exists to prime the match cache, and every write goes through the
    same store, so a store that can't answer ``has_dats`` still skips the phase
    -- rather than proceeding and hard-failing on the next dat_store call.
    """
    import routes.dat as dat_internal
    from services.dat_store import dat_store as global_dat_store

    def _boom():
        raise RuntimeError("db not initialised")

    monkeypatch.setattr(global_dat_store, "has_dats", _boom)

    called = []

    async def _fake_match(path, *, cancel_event=None):
        called.append(path)
        return {"path": path, "matched": False}

    monkeypatch.setattr(dat_internal, "_match_single_file", _fake_match)

    matched = await scan_phase_stubs._scan_phase_dat_match(
        "job-1", ["/vol/a.iso"], force=True,
    )

    assert matched == 0
    assert called == []


@pytest.mark.asyncio
async def test_scan_phase3_runs_with_no_dats_when_hasheous_is_on(
    hasheous_on, scan_phase_stubs, monkeypatch,
):
    """A healthy but empty store still gets scanned once Hasheous can answer."""
    import routes.dat as dat_internal
    from services.dat_store import dat_store as global_dat_store

    monkeypatch.setattr(global_dat_store, "has_dats", lambda: False)

    stored = {}

    async def _fake_set_match(path, result):
        stored[path] = result

    monkeypatch.setattr(global_dat_store, "set_match", _fake_set_match)

    async def _fake_match(path, *, cancel_event=None):
        return {"path": path, "matched": True, "source": "hasheous"}

    monkeypatch.setattr(dat_internal, "_match_single_file", _fake_match)

    matched = await scan_phase_stubs._scan_phase_dat_match(
        "job-1", ["/vol/a.iso"], force=True,
    )

    assert matched == 1
    assert stored["/vol/a.iso"]["source"] == "hasheous"


def test_matching_available_gate(monkeypatch):
    monkeypatch.setattr(settings, "hasheous_enabled", False)
    assert dat_routes.matching_available(True) is True
    assert dat_routes.matching_available(False) is False

    monkeypatch.setattr(settings, "hasheous_enabled", True)
    assert dat_routes.matching_available(False) is True
