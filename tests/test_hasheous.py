"""Tests for the Hasheous remote hash-lookup fallback.

No HTTP mocking library is used (the suite has none): the seam is
``services.hasheous._fetch_json``, patched the same way
``tests/test_dat_sync.py`` patches ``sync_service._fetch_json``.
"""

import urllib.error
import urllib.request
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


@pytest.fixture(autouse=True)
def _reset_cooldown():
    """Clear the circuit breaker around every test.

    It is module-level state, so one failing-transport test would otherwise
    make every later test skip its lookup and see "unavailable" instead of
    whatever it was actually asserting.
    """
    hasheous._clear_cooldown()
    yield
    hasheous._clear_cooldown()


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
    with patch.object(hasheous._opener, "open", side_effect=_http_error(code)):
        with pytest.raises(hasheous.HasheousUnavailable):
            hasheous._fetch_json("https://hasheous.example/x")


def test_fetch_json_maps_404_to_none(hasheous_on):
    with patch.object(hasheous._opener, "open", side_effect=_http_error(404)):
        assert hasheous._fetch_json("https://hasheous.example/x") is None


def test_timeout_raises_unavailable(hasheous_on):
    with patch.object(hasheous._opener, "open", side_effect=TimeoutError("timed out")):
        with pytest.raises(hasheous.HasheousUnavailable):
            hasheous._fetch_json("https://hasheous.example/x")


def test_url_error_raises_unavailable(hasheous_on):
    with patch.object(
        hasheous._opener, "open", side_effect=urllib.error.URLError("no route"),
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
    with patch.object(hasheous._opener, "open", return_value=_Resp(b"<html>nope</html>")):
        with pytest.raises(hasheous.HasheousUnavailable):
            hasheous._fetch_json("https://hasheous.example/x")


def test_non_object_body_raises_unavailable(hasheous_on):
    """A bare list is not the documented shape; refuse rather than guess."""
    with patch.object(hasheous._opener, "open", return_value=_Resp(b"[1, 2, 3]")):
        with pytest.raises(hasheous.HasheousUnavailable):
            hasheous._fetch_json("https://hasheous.example/x")


def test_oversized_body_raises_unavailable(hasheous_on):
    huge = b"x" * (hasheous._MAX_RESPONSE_BYTES + 1)
    with patch.object(hasheous._opener, "open", return_value=_Resp(huge)):
        with pytest.raises(hasheous.HasheousUnavailable):
            hasheous._fetch_json("https://hasheous.example/x")


def test_plain_http_is_refused(monkeypatch):
    """File hashes must not go out in the clear.

    Surfaces as HasheousUnavailable, not a bare ValueError: the match path only
    catches the former, so a ValueError would escape as a 500 and skip the
    cooldown, making a bulk match re-raise it once per file.
    """
    monkeypatch.setattr(settings, "hasheous_base_url", "http://hasheous.example")
    with pytest.raises(hasheous.HasheousUnavailable, match="https"):
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
        result = await dat_routes._lookup_match("/x.iso", [(SAMPLE_SHA1, "file_sha1")])

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
            result = await dat_routes._lookup_match(
                "/x.iso", [(SAMPLE_SHA1, "file_sha1")],
            )

    assert result["source"] == "dat"
    assert result["dat_id"] == "abc"
    remote.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_fallback_on_local_miss(hasheous_on):
    with patch.object(dat_routes, "_local_dat_record", AsyncMock(return_value=None)):
        with patch.object(hasheous, "_fetch_json", return_value=SAMPLE_RESPONSE):
            result = await dat_routes._lookup_match(
                "/x.iso", [(SAMPLE_SHA1, "file_sha1")],
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


# ---------------------------------------------------------------------------
# Review findings (PR #273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_candidates_checked_locally_before_any_remote_call(hasheous_on):
    """A CHD reports header SHA1 then data SHA1.

    If only the *second* is in a local DAT, the first must never be sent
    remotely: interleaving would disclose a hash the local DATs could identify
    on their own, and a remote timeout on it would mask the local hit.
    """
    header, data = "a" * 40, "b" * 40
    local = {
        "dat_id": "d1", "dat_name": "Local DAT", "game_name": "G",
        "rom_name": "g.chd", "source": "dat",
    }

    async def _local(sha1):
        return local if sha1 == data else None

    with patch.object(dat_routes, "_local_dat_record", _local):
        with patch.object(hasheous, "lookup", new=AsyncMock()) as remote:
            result = await dat_routes._lookup_match(
                "/g.chd", [(header, "chd_sha1"), (data, "chd_data_sha1")],
            )

    assert result["source"] == "dat"
    assert result["match_type"] == "chd_data_sha1"
    assert result["file_hash"] == data
    remote.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_outage_cannot_mask_a_later_local_candidate(hasheous_on):
    """The failure mode the two-pass ordering exists to prevent."""
    header, data = "a" * 40, "b" * 40
    local = {
        "dat_id": "d1", "dat_name": "Local DAT", "game_name": "G",
        "rom_name": "g.chd", "source": "dat",
    }

    async def _local(sha1):
        return local if sha1 == data else None

    async def _boom(_sha1):
        raise hasheous.HasheousUnavailable("timed out")

    with patch.object(dat_routes, "_local_dat_record", _local):
        with patch.object(hasheous, "lookup", _boom):
            result = await dat_routes._lookup_match(
                "/g.chd", [(header, "chd_sha1"), (data, "chd_data_sha1")],
            )

    assert result["matched"] is True
    assert result["source"] == "dat"


@pytest.mark.asyncio
async def test_remote_tried_for_every_candidate_once_all_miss_locally(hasheous_on):
    header, data = "a" * 40, "b" * 40
    seen = []

    async def _remote(sha1):
        seen.append(sha1)
        return None

    with patch.object(dat_routes, "_local_dat_record", AsyncMock(return_value=None)):
        with patch.object(hasheous, "lookup", _remote):
            result = await dat_routes._lookup_match(
                "/g.chd", [(header, "chd_sha1"), (data, "chd_data_sha1")],
            )

    assert result is None
    assert seen == [header, data]


def test_cached_miss_from_before_hasheous_is_not_reused(monkeypatch):
    """An existing install that switches Hasheous on must re-check old misses.

    Otherwise the rows cached by the weaker local-only matcher are served
    forever and the feature silently does nothing for the very library it
    exists to identify.
    """
    stale_miss = {"path": "/a.iso", "matched": False, "checked_remote": None}
    fresh_miss = {
        "path": "/a.iso", "matched": False, "checked_remote": "https://hasheous.example",
    }
    hit = {"path": "/a.iso", "matched": True, "source": "dat"}

    monkeypatch.setattr(settings, "hasheous_enabled", True)
    monkeypatch.setattr(settings, "hasheous_base_url", "https://hasheous.example")
    assert dat_routes.cached_result_usable(stale_miss) is False
    assert dat_routes.cached_result_usable(fresh_miss) is True
    # A local hit is already the strongest answer; local is consulted first.
    assert dat_routes.cached_result_usable(hit) is True
    assert dat_routes.cached_result_usable(None) is False

    # With the feature off, a local-only miss is still the correct verdict.
    monkeypatch.setattr(settings, "hasheous_enabled", False)
    assert dat_routes.cached_result_usable(stale_miss) is True


@pytest.mark.asyncio
async def test_misses_record_which_sources_were_consulted(hasheous_on, monkeypatch):
    """The stamp cached_result_usable reads has to actually be written."""
    monkeypatch.setattr(dat_routes.dat_store, "has_dats", lambda: False)
    monkeypatch.setattr(
        dat_routes, "compute_file_sha1", AsyncMock(return_value=SAMPLE_SHA1),
    )
    monkeypatch.setattr(dat_routes, "_local_dat_record", AsyncMock(return_value=None))
    monkeypatch.setattr(hasheous, "lookup", AsyncMock(return_value=None))

    result = await dat_routes._match_single_file("/x.iso")

    assert result["matched"] is False
    # The stamp is the server URL, not a flag, so repointing at a different
    # Hasheous also invalidates the row.
    assert result["checked_remote"] == "https://hasheous.example"


@pytest.mark.asyncio
async def test_outage_short_circuits_instead_of_timing_out_per_file(hasheous_on):
    """A 1,000-file scan must not pay hasheous_timeout a thousand times."""
    calls = []

    def _boom(_url):
        calls.append(_url)
        raise hasheous.HasheousUnavailable("timed out")

    with patch.object(hasheous, "_fetch_json", _boom):
        with pytest.raises(hasheous.HasheousUnavailable):
            await hasheous.lookup(SAMPLE_SHA1)
        # Every subsequent file still fails (non-cacheable), but without
        # another network round trip.
        for _ in range(5):
            with pytest.raises(hasheous.HasheousUnavailable):
                await hasheous.lookup(SAMPLE_SHA1)

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cooldown_clears_after_a_success(hasheous_on):
    with patch.object(
        hasheous, "_fetch_json", side_effect=hasheous.HasheousUnavailable("down"),
    ):
        with pytest.raises(hasheous.HasheousUnavailable):
            await hasheous.lookup(SAMPLE_SHA1)

    assert hasheous._cooldown_remaining() > 0
    hasheous._clear_cooldown()

    with patch.object(hasheous, "_fetch_json", return_value=SAMPLE_RESPONSE):
        assert await hasheous.lookup(SAMPLE_SHA1) is not None
    assert hasheous._cooldown_remaining() == 0


def test_redirect_to_http_is_refused():
    """urlopen follows redirects itself, so the guard must run on each hop.

    Without it a misconfigured or hostile server could bounce the lookup to
    http:// and put the file's SHA1 on the wire in the clear.
    """
    handler = hasheous._HTTPSOnlyRedirectHandler()
    with pytest.raises(ValueError, match="https"):
        handler.redirect_request(
            None, None, 302, "Found", {}, "http://evil.example/leak",
        )


def test_redirect_to_https_is_allowed():
    handler = hasheous._HTTPSOnlyRedirectHandler()
    req = urllib.request.Request("https://hasheous.example/a")
    out = handler.redirect_request(
        req, None, 302, "Found", {}, "https://hasheous.example/b",
    )
    assert out is not None


# ---------------------------------------------------------------------------
# Web UI toggle + connection test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_override():
    """The override is module-level state; don't leak it between tests."""
    hasheous.set_enabled_override(None)
    yield
    hasheous.set_enabled_override(None)


def test_override_beats_the_env_var(monkeypatch):
    """The toggle has to work without a container restart."""
    monkeypatch.setattr(settings, "hasheous_enabled", False)
    assert hasheous.enabled() is False

    hasheous.set_enabled_override(True)
    assert hasheous.enabled() is True
    assert hasheous.env_default() is False  # the env var itself is unchanged

    # ...and the reverse: the UI can switch it off even when the env says on.
    monkeypatch.setattr(settings, "hasheous_enabled", True)
    hasheous.set_enabled_override(False)
    assert hasheous.enabled() is False


def test_clearing_the_override_falls_back_to_the_env_var(monkeypatch):
    monkeypatch.setattr(settings, "hasheous_enabled", True)
    hasheous.set_enabled_override(False)
    assert hasheous.enabled() is False

    hasheous.set_enabled_override(None)
    assert hasheous.enabled() is True
    assert hasheous.override() is None


def test_toggling_clears_an_active_cooldown(monkeypatch):
    """Flipping the switch is the operator saying "try again"."""
    monkeypatch.setattr(settings, "hasheous_enabled", True)
    hasheous._begin_cooldown()
    assert hasheous._cooldown_remaining() > 0

    hasheous.set_enabled_override(True)

    assert hasheous._cooldown_remaining() == 0


@pytest.mark.asyncio
async def test_put_settings_persists_and_applies(monkeypatch):
    monkeypatch.setattr(settings, "hasheous_enabled", False)
    saved = {}

    async def _put(key, value):
        saved[key] = value
        return value

    monkeypatch.setattr(dat_routes.preferences_store, "put", _put)

    state = await dat_routes.put_hasheous_settings(
        dat_routes.HasheousSettingsRequest(enabled=True),
    )

    assert state["enabled"] is True
    assert state["overridden"] is True
    assert state["env_default"] is False
    assert saved[dat_routes.HASHEOUS_PREF_KEY] == {"enabled": True}
    # Applied immediately, not just persisted.
    assert hasheous.enabled() is True


@pytest.mark.asyncio
async def test_saved_toggle_is_restored_at_startup(monkeypatch):
    monkeypatch.setattr(settings, "hasheous_enabled", False)

    async def _get(_key):
        return {"enabled": True}

    monkeypatch.setattr(dat_routes.preferences_store, "get", _get)

    await dat_routes.load_hasheous_override()

    assert hasheous.enabled() is True


@pytest.mark.asyncio
async def test_startup_survives_an_unreadable_preference(monkeypatch):
    """A preferences failure must not stop the app booting."""
    monkeypatch.setattr(settings, "hasheous_enabled", False)

    async def _boom(_key):
        raise RuntimeError("db down")

    monkeypatch.setattr(dat_routes.preferences_store, "get", _boom)

    await dat_routes.load_hasheous_override()  # must not raise

    assert hasheous.enabled() is False


@pytest.mark.asyncio
async def test_health_probe_reports_success(hasheous_on):
    with patch.object(hasheous._opener, "open", return_value=_Resp(b"OK")):
        result = await hasheous.health()

    assert result["ok"] is True
    assert result["url"] == "https://hasheous.example/api/v1/Healthcheck"
    assert "latency_ms" in result


@pytest.mark.asyncio
async def test_health_probe_reports_failure_without_raising(hasheous_on):
    """An unreachable server is a result to display, not an API error."""
    with patch.object(
        hasheous._opener, "open", side_effect=urllib.error.URLError("no route"),
    ):
        result = await hasheous.health()

    assert result["ok"] is False
    assert "no route" in result["error"]


@pytest.mark.asyncio
async def test_health_probe_refuses_a_plain_http_base_url(monkeypatch):
    monkeypatch.setattr(settings, "hasheous_base_url", "http://hasheous.example")

    result = await hasheous.health()

    assert result["ok"] is False
    assert "https" in result["error"]


# ---------------------------------------------------------------------------
# Second review round (PR #273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_base_url_is_a_service_failure_not_a_crash(monkeypatch):
    """Misconfiguration must degrade, not 500.

    _require_https raises ValueError; nothing in the match path catches that,
    so it would escape as a 500 and never open the cooldown.
    """
    monkeypatch.setattr(settings, "hasheous_enabled", True)
    monkeypatch.setattr(settings, "hasheous_base_url", "http://insecure.example")

    with pytest.raises(hasheous.HasheousUnavailable):
        await hasheous.lookup(SAMPLE_SHA1)

    # ...and the cooldown opened, so a bulk match doesn't repeat it per file.
    assert hasheous._cooldown_remaining() > 0


@pytest.mark.asyncio
async def test_match_reports_a_bad_url_as_non_cacheable(monkeypatch):
    """End to end: the misconfiguration reaches the caller as an error result."""
    monkeypatch.setattr(settings, "hasheous_enabled", True)
    monkeypatch.setattr(settings, "hasheous_base_url", "http://insecure.example")
    monkeypatch.setattr(dat_routes.dat_store, "has_dats", lambda: False)
    monkeypatch.setattr(
        dat_routes, "compute_file_sha1", AsyncMock(return_value=SAMPLE_SHA1),
    )
    monkeypatch.setattr(dat_routes, "_local_dat_record", AsyncMock(return_value=None))

    result = await dat_routes._match_single_file("/x.iso")

    assert result["matched"] is False
    assert result["error"] == "hasheous unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"error": "upstream failed"}, {"id": 7}])
async def test_a_200_without_game_identity_is_not_a_hit(body, hasheous_on):
    """A proxy error envelope must not be cached as an authoritative match.

    Hasheous answers an unknown hash with 404, so a 200 carrying no game name
    is something else answering for it.
    """
    with patch.object(hasheous, "_fetch_json", return_value=body):
        with pytest.raises(hasheous.HasheousUnavailable):
            await hasheous.lookup(SAMPLE_SHA1)


@pytest.mark.asyncio
async def test_a_200_with_only_a_rom_name_still_counts(hasheous_on):
    """Identity can come from either side; don't over-reject."""
    body = {"id": 1, "signature": {"rom": {"name": "thing.bin"}}}
    with patch.object(hasheous, "_fetch_json", return_value=body):
        record = await hasheous.lookup(SAMPLE_SHA1)

    assert record["rom_name"] == "thing.bin"


def test_cached_miss_is_dropped_when_the_server_url_changes(monkeypatch):
    """Repointing at a self-hosted instance changes which database answers."""
    monkeypatch.setattr(settings, "hasheous_enabled", True)
    monkeypatch.setattr(settings, "hasheous_base_url", "https://hasheous.org")
    miss = {"path": "/a.iso", "matched": False, "checked_remote": "https://hasheous.org"}

    assert dat_routes.cached_result_usable(miss) is True

    monkeypatch.setattr(settings, "hasheous_base_url", "https://my-hasheous.lan")
    assert dat_routes.cached_result_usable(miss) is False


def test_remote_stamp_is_the_url_not_a_flag(monkeypatch):
    monkeypatch.setattr(settings, "hasheous_enabled", True)
    monkeypatch.setattr(settings, "hasheous_base_url", "https://my-hasheous.lan/")
    assert dat_routes.remote_stamp() == "https://my-hasheous.lan"

    monkeypatch.setattr(settings, "hasheous_enabled", False)
    assert dat_routes.remote_stamp() is None


# ---------------------------------------------------------------------------
# Third review round (PR #273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identityless_200_opens_the_cooldown(hasheous_on):
    """The breaker has to cover validation failures, not just transport ones.

    A proxy answering every hash with `{}` would otherwise be re-requested once
    per file -- recreating the hours-long outage the cooldown exists to stop.
    """
    calls = []

    def _empty(url):
        calls.append(url)
        return {}

    with patch.object(hasheous, "_fetch_json", _empty):
        with pytest.raises(hasheous.HasheousUnavailable):
            await hasheous.lookup(SAMPLE_SHA1)
        for _ in range(5):
            with pytest.raises(hasheous.HasheousUnavailable):
                await hasheous.lookup(SAMPLE_SHA1)

    assert len(calls) == 1
    assert hasheous._cooldown_remaining() > 0


@pytest.mark.asyncio
async def test_a_404_still_counts_as_healthy(hasheous_on):
    """A miss means the server answered; it must not open the breaker."""
    hasheous._begin_cooldown()
    hasheous._clear_cooldown()

    with patch.object(hasheous, "_fetch_json", return_value=None):
        assert await hasheous.lookup(SAMPLE_SHA1) is None

    assert hasheous._cooldown_remaining() == 0


@pytest.mark.asyncio
async def test_toggle_is_persisted_before_it_is_applied(monkeypatch):
    """A failed write must not leave the process quietly sending hashes.

    Applying first would enable remote lookups for the running process while
    the endpoint returned an error and the UI still showed the switch off.
    """
    monkeypatch.setattr(settings, "hasheous_enabled", False)

    async def _boom(_key, _value):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(dat_routes.preferences_store, "put", _boom)

    with pytest.raises(RuntimeError, match="locked"):
        await dat_routes.put_hasheous_settings(
            dat_routes.HasheousSettingsRequest(enabled=True),
        )

    assert hasheous.enabled() is False
    assert hasheous.override() is None


@pytest.mark.asyncio
async def test_file_level_sha1_is_checked_locally_before_any_remote_call(
    hasheous_on, monkeypatch,
):
    """The non-exhaustive (CHD) case of "all local before any remote".

    A CHD reports header + data SHA1s, but its *container* bytes may be what
    the local DAT indexes. Sending the embedded hashes out before checking that
    container SHA1 locally both discloses them needlessly and lets a remote
    outage mask the local hit.
    """
    header, data, container = "a" * 40, "b" * 40, "c" * 40

    class _Chd:
        embedded_hash_is_exhaustive = False

        async def embedded_hashes(self, path, *, cancel_event=None):
            return [(header, "chd_sha1"), (data, "chd_data_sha1")]

    monkeypatch.setattr(dat_routes.dat_store, "has_dats", lambda: True)
    monkeypatch.setattr(dat_routes.registry, "tool_for_verify", lambda _p: _Chd())
    monkeypatch.setattr(
        dat_routes, "compute_file_sha1", AsyncMock(return_value=container),
    )

    async def _local(sha1):
        if sha1 != container:
            return None
        return {
            "dat_id": "d1", "dat_name": "Local DAT", "game_name": "G",
            "rom_name": "g.chd", "source": "dat",
        }

    monkeypatch.setattr(dat_routes, "_local_dat_record", _local)
    remote = AsyncMock(side_effect=hasheous.HasheousUnavailable("down"))
    monkeypatch.setattr(hasheous, "lookup", remote)

    result = await dat_routes._match_single_file("/g.chd")

    assert result["matched"] is True
    assert result["source"] == "dat"
    assert result["match_type"] == "file_sha1"
    # The embedded hashes were never disclosed, and the outage never mattered.
    remote.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_pass_covers_embedded_and_file_level_together(
    hasheous_on, monkeypatch,
):
    """Once everything misses locally, one remote pass sees the whole set."""
    header, container = "a" * 40, "c" * 40

    class _Chd:
        embedded_hash_is_exhaustive = False

        async def embedded_hashes(self, path, *, cancel_event=None):
            return [(header, "chd_sha1")]

    monkeypatch.setattr(dat_routes.dat_store, "has_dats", lambda: True)
    monkeypatch.setattr(dat_routes.registry, "tool_for_verify", lambda _p: _Chd())
    monkeypatch.setattr(
        dat_routes, "compute_file_sha1", AsyncMock(return_value=container),
    )
    monkeypatch.setattr(dat_routes, "_local_dat_record", AsyncMock(return_value=None))

    seen = []

    async def _remote(sha1):
        seen.append(sha1)
        return None

    monkeypatch.setattr(hasheous, "lookup", _remote)

    result = await dat_routes._match_single_file("/g.chd")

    assert result["matched"] is False
    assert seen == [header, container]


@pytest.mark.asyncio
async def test_exhaustive_tool_never_reads_the_whole_file(hasheous_on, monkeypatch):
    """Dolphin's disc SHA1 is definitive, so no file-level fallback."""
    disc = "d" * 40

    class _Dolphin:
        embedded_hash_is_exhaustive = True

        async def embedded_hashes(self, path, *, cancel_event=None):
            return [(disc, "dolphin_disc_sha1")]

    monkeypatch.setattr(dat_routes.dat_store, "has_dats", lambda: True)
    monkeypatch.setattr(dat_routes.registry, "tool_for_verify", lambda _p: _Dolphin())
    monkeypatch.setattr(dat_routes, "_local_dat_record", AsyncMock(return_value=None))
    hashed = AsyncMock(return_value="e" * 40)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", hashed)

    seen = []

    async def _remote(sha1):
        seen.append(sha1)
        return None

    monkeypatch.setattr(hasheous, "lookup", _remote)

    result = await dat_routes._match_single_file("/g.rvz")

    assert result["matched"] is False
    assert seen == [disc]
    hashed.assert_not_awaited()


@pytest.mark.asyncio
async def test_size_capped_file_still_gets_its_embedded_hashes_checked(
    hasheous_on, monkeypatch,
):
    """The cap skips reading the file, not the hashes already in hand."""
    header = "a" * 40

    class _Chd:
        embedded_hash_is_exhaustive = False

        async def embedded_hashes(self, path, *, cancel_event=None):
            return [(header, "chd_sha1")]

    monkeypatch.setattr(dat_routes.dat_store, "has_dats", lambda: True)
    monkeypatch.setattr(dat_routes.registry, "tool_for_verify", lambda _p: _Chd())
    monkeypatch.setattr(dat_routes, "_local_dat_record", AsyncMock(return_value=None))
    monkeypatch.setattr(dat_routes.settings, "match_max_file_size", 1024)
    monkeypatch.setattr(dat_routes.os.path, "getsize", lambda _p: 99_999)
    hashed = AsyncMock(return_value="e" * 40)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", hashed)
    remote = AsyncMock(return_value=None)
    monkeypatch.setattr(hasheous, "lookup", remote)

    result = await dat_routes._match_single_file("/big.chd")

    assert result["reason"] == "file too large"  # non-cacheable, as before
    remote.assert_awaited_once_with(header)
    hashed.assert_not_awaited()


# ---------------------------------------------------------------------------
# Fourth review round (PR #273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"signature": "unavailable"},
    {"platform": "Commodore 64"},
    {"publisher": "Epyx"},
    {"signature": {"rom": "nope", "game": "nope"}},
    {"metadata": "not-a-list"},
])
async def test_malformed_nested_fields_do_not_crash_the_match_path(body, hasheous_on):
    """A 200 with a string where an object belongs used to raise AttributeError.

    That is not HasheousUnavailable, so it escaped as a 500 and skipped the
    cooldown -- once per file in a bulk match.
    """
    with patch.object(hasheous, "_fetch_json", return_value=body):
        with pytest.raises(hasheous.HasheousUnavailable):
            await hasheous.lookup(SAMPLE_SHA1)

    assert hasheous._cooldown_remaining() > 0


def test_obj_coerces_non_objects():
    assert hasheous._obj({"a": 1}) == {"a": 1}
    for bad in ("str", 5, None, [1, 2]):
        assert hasheous._obj(bad) == {}


@pytest.mark.asyncio
async def test_normalization_crash_becomes_unavailable(hasheous_on, monkeypatch):
    """Belt and braces for a shape _obj didn't foresee."""
    def _boom(_data):
        raise TypeError("something unforeseen")

    monkeypatch.setattr(hasheous, "_normalize", _boom)

    with patch.object(hasheous, "_fetch_json", return_value={"id": 1}):
        with pytest.raises(hasheous.HasheousUnavailable, match="unreadable"):
            await hasheous.lookup(SAMPLE_SHA1)

    assert hasheous._cooldown_remaining() > 0


def test_hasheous_error_marker_is_shared():
    """The job reads this back to tell a network outage from a dead volume."""
    assert dat_routes.HASHEOUS_ERROR == "hasheous unavailable"


@pytest.mark.asyncio
async def test_all_files_failing_on_hasheous_does_not_blame_the_volume(
    hasheous_on, monkeypatch,
):
    """The operator must not be sent to debug storage over a network outage."""
    monkeypatch.setattr(dat_routes.dat_store, "has_dats", lambda: False)
    monkeypatch.setattr(
        dat_routes, "compute_file_sha1", AsyncMock(return_value=SAMPLE_SHA1),
    )
    monkeypatch.setattr(dat_routes, "_local_dat_record", AsyncMock(return_value=None))
    monkeypatch.setattr(
        hasheous, "lookup",
        AsyncMock(side_effect=hasheous.HasheousUnavailable("timed out")),
    )

    result = await dat_routes._match_single_file("/x.iso")

    # This exact marker is what the all-failed branch keys off.
    assert result["error"] == dat_routes.HASHEOUS_ERROR


# ---------------------------------------------------------------------------
# Fifth review round (PR #273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url", ["", "hasheous.org", "://broken"])
async def test_an_unusable_base_url_is_a_service_failure(base_url, monkeypatch):
    """Request() itself raises on a schemeless URL, before _require_https runs.

    From outside the guard that escaped as a 500 with no cooldown, so a bulk
    job repeated it once per file.
    """
    monkeypatch.setattr(settings, "hasheous_enabled", True)
    monkeypatch.setattr(settings, "hasheous_base_url", base_url)

    with pytest.raises(hasheous.HasheousUnavailable):
        await hasheous.lookup(SAMPLE_SHA1)

    assert hasheous._cooldown_remaining() > 0


@pytest.mark.asyncio
async def test_turning_hasheous_off_stops_the_current_remote_pass(
    hasheous_on, monkeypatch,
):
    """"Off means nothing is sent" has to bind the very next request.

    A CHD sends up to three hashes and each can take seconds; checking the
    switch once before the loop let the rest of an in-flight lookup go out
    after the operator had already opted out.
    """
    sent = []

    async def _remote(sha1):
        sent.append(sha1)
        # The operator hits "Turn off" while this first request is in flight.
        hasheous.set_enabled_override(False)
        return None

    monkeypatch.setattr(hasheous, "lookup", _remote)

    result, consulted = await dat_routes._remote_lookup_match(
        "/g.chd", [("a" * 40, "chd_sha1"), ("b" * 40, "chd_data_sha1")],
    )

    assert result is None
    assert sent == ["a" * 40]  # the second candidate never left the machine
    # Cut short, so this was NOT a complete remote check and must not be
    # stamped as one.
    assert consulted is None


# ---------------------------------------------------------------------------
# Sixth review round (PR #273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_miss_is_not_stamped_with_a_server_never_asked(
    hasheous_on, monkeypatch,
):
    """The toggle can flip while an expensive hash is still being computed.

    Stamping at function entry meant a miss could claim a remote check that
    never happened -- and cached_result_usable would then serve it forever
    once the feature was switched back on.
    """
    monkeypatch.setattr(dat_routes.dat_store, "has_dats", lambda: True)
    monkeypatch.setattr(dat_routes, "_local_dat_record", AsyncMock(return_value=None))
    monkeypatch.setattr(dat_routes.registry, "tool_for_verify", lambda _p: None)

    async def _slow_hash(_path):
        # The operator turns Hasheous off while the file is being hashed.
        hasheous.set_enabled_override(False)
        return SAMPLE_SHA1

    monkeypatch.setattr(dat_routes, "compute_file_sha1", _slow_hash)
    remote = AsyncMock()
    monkeypatch.setattr(hasheous, "lookup", remote)

    result = await dat_routes._match_single_file("/x.iso")

    assert result["matched"] is False
    remote.assert_not_awaited()
    # Nothing was asked, so nothing is claimed.
    assert result["checked_remote"] is None
    # ...and re-enabling therefore re-checks this file rather than trusting it.
    hasheous.set_enabled_override(True)
    assert dat_routes.cached_result_usable(result) is False


@pytest.mark.asyncio
async def test_a_completed_remote_pass_is_stamped(hasheous_on, monkeypatch):
    monkeypatch.setattr(dat_routes.dat_store, "has_dats", lambda: True)
    monkeypatch.setattr(dat_routes, "_local_dat_record", AsyncMock(return_value=None))
    monkeypatch.setattr(dat_routes.registry, "tool_for_verify", lambda _p: None)
    monkeypatch.setattr(
        dat_routes, "compute_file_sha1", AsyncMock(return_value=SAMPLE_SHA1),
    )
    monkeypatch.setattr(hasheous, "lookup", AsyncMock(return_value=None))

    result = await dat_routes._match_single_file("/x.iso")

    assert result["checked_remote"] == "https://hasheous.example"
    assert dat_routes.cached_result_usable(result) is True


@pytest.mark.asyncio
async def test_a_successful_test_clears_the_cooldown(hasheous_on):
    """Otherwise the button says "Reachable" while browsing still says down."""
    hasheous._begin_cooldown()
    assert hasheous._cooldown_remaining() > 0

    with patch.object(hasheous._opener, "open", return_value=_Resp(b"OK")):
        result = await hasheous.health()

    assert result["ok"] is True
    assert hasheous._cooldown_remaining() == 0


@pytest.mark.asyncio
async def test_a_failed_test_opens_the_cooldown(hasheous_on):
    """The probe just established the outage; don't pay a timeout to re-learn it."""
    with patch.object(
        hasheous._opener, "open", side_effect=urllib.error.URLError("no route"),
    ):
        result = await hasheous.health()

    assert result["ok"] is False
    assert hasheous._cooldown_remaining() > 0


# ---------------------------------------------------------------------------
# Seventh review round (PR #273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_forced_rescan_during_an_outage_keeps_existing_matches(
    hasheous_on, scan_phase_stubs, monkeypatch,
):
    """Data loss, not cosmetics.

    A forced rescan recomputes every path. With Hasheous down each one comes
    back non-cacheable, and the delete-stale-row branch would erase every
    previously-matched file -- fast, because the breaker makes each failure
    instant, and the scan would still finish looking normal. The remote being
    down says nothing about the file, whose earlier match is still correct.
    """
    import routes.dat as dat_internal
    from services.dat_store import dat_store as global_dat_store

    monkeypatch.setattr(global_dat_store, "has_dats", lambda: True)
    monkeypatch.setattr(
        global_dat_store, "get_matches_batch", lambda _paths: {},
    )

    deleted, stored = [], []

    async def _delete(path):
        deleted.append(path)

    async def _set(path, result):
        stored.append(path)

    monkeypatch.setattr(global_dat_store, "delete_match", _delete)
    monkeypatch.setattr(global_dat_store, "set_match", _set)

    async def _outage(path, *, cancel_event=None):
        return {
            "path": path, "matched": False,
            "error": dat_internal.HASHEOUS_ERROR,
        }

    monkeypatch.setattr(dat_internal, "_match_single_file", _outage)

    await scan_phase_stubs._scan_phase_dat_match(
        "job-1", ["/vol/a.chd", "/vol/b.chd"], force=True,
    )

    assert deleted == []   # the previously-good rows survive the outage
    assert stored == []    # ...and nothing bogus is written either


@pytest.mark.asyncio
async def test_a_file_level_failure_still_drops_its_stale_row(
    hasheous_on, scan_phase_stubs, monkeypatch,
):
    """The distinction is service-vs-file: a file we can no longer verify
    should not keep showing an old badge."""
    import routes.dat as dat_internal
    from services.dat_store import dat_store as global_dat_store

    monkeypatch.setattr(global_dat_store, "has_dats", lambda: True)
    monkeypatch.setattr(global_dat_store, "get_matches_batch", lambda _paths: {})

    deleted = []

    async def _delete(path):
        deleted.append(path)

    monkeypatch.setattr(global_dat_store, "delete_match", _delete)
    monkeypatch.setattr(global_dat_store, "set_match", AsyncMock())

    async def _unreadable(path, *, cancel_event=None):
        return {"path": path, "matched": False, "error": "Unable to process file"}

    monkeypatch.setattr(dat_internal, "_match_single_file", _unreadable)

    await scan_phase_stubs._scan_phase_dat_match(
        "job-1", ["/vol/a.chd"], force=True,
    )

    assert deleted == ["/vol/a.chd"]
