"""Tests for the Hasheous remote hash-lookup fallback.

No HTTP mocking library is used (the suite has none): the seam is
``services.hasheous._fetch_json``, patched the same way
``tests/test_dat_sync.py`` patches ``sync_service._fetch_json``.
"""

import asyncio
import http.client
import json
import shutil
import signal
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import BackgroundTasks

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
    """A minimal stand-in for ``http.client.HTTPResponse``.

    ``read`` drains and then returns b"" the way a real response does. An
    earlier version returned the whole payload on every call, which never hit
    EOF -- so tests naming the JSON-parse path actually passed via the
    size-limit path instead.
    """

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, n=None):
        take, self._payload = self._payload[:n], self._payload[n:]
        return take

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

    matched, _ = await scan_phase_stubs._scan_phase_dat_match(
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

    matched, _ = await scan_phase_stubs._scan_phase_dat_match(
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


# ---------------------------------------------------------------------------
# Eighth review round (PR #273)
# ---------------------------------------------------------------------------


class _TruncatedResp:
    """A chunked response the server cuts short."""

    def read(self, _n=None):
        raise http.client.IncompleteRead(b"partial", 500)

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def test_a_truncated_response_is_a_service_failure(hasheous_on):
    """IncompleteRead is an HTTPException -- neither OSError nor URLError.

    Uncaught it escaped as a 500 and skipped the cooldown, so a bulk job
    re-contacted the failing server once per file.
    """
    with patch.object(hasheous._opener, "open", return_value=_TruncatedResp()):
        with pytest.raises(hasheous.HasheousUnavailable, match="IncompleteRead"):
            hasheous._fetch_json("https://hasheous.example/x")


def test_a_malformed_status_line_is_a_service_failure(hasheous_on):
    with patch.object(
        hasheous._opener, "open", side_effect=http.client.BadStatusLine("garbage"),
    ):
        with pytest.raises(hasheous.HasheousUnavailable):
            hasheous._fetch_json("https://hasheous.example/x")


def _tls_serve_once(handler, certdir) -> int:
    """Run a one-shot HTTPS server on a loopback port and return the port."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certdir / "c.pem", certdir / "k.pem")

    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)

    def _run():
        try:
            conn, _ = sock.accept()
            conn = ctx.wrap_socket(conn, server_side=True)
            conn.recv(4096)
            handler(conn)
            conn.close()
        except OSError:
            pass
        finally:
            sock.close()

    threading.Thread(target=_run, daemon=True).start()
    return sock.getsockname()[1]


@pytest.fixture(scope="module")
def tls_cert(tmp_path_factory):
    """A throwaway self-signed cert, or skip if openssl isn't on the box."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available")
    d = tmp_path_factory.mktemp("certs")
    subprocess.run(  # noqa: S603
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(d / "k.pem"), "-out", str(d / "c.pem"),
            "-days", "1", "-nodes", "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return d


def test_the_lookup_deadline_is_wired_into_the_opener():
    """The bound only exists if the opener actually uses our socket class.

    Rebuilding ``_opener`` without the context would silently remove the whole
    protection while every behavioural test still passed, so pin the wiring.
    """
    contexts = [
        h._context
        for h in hasheous._opener.handlers
        if isinstance(h, urllib.request.HTTPSHandler)
    ]
    assert contexts, "opener has no HTTPS handler"
    assert all(c.sslsocket_class is hasheous._DeadlineSSLSocket for c in contexts)
    # ...and it must still verify certificates.
    assert all(c.verify_mode is ssl.CERT_REQUIRED for c in contexts)


def test_an_expired_deadline_stops_the_read():
    sock = hasheous._DeadlineSSLSocket.__new__(hasheous._DeadlineSSLSocket)
    with hasheous._deadline_of(-1):
        with pytest.raises(TimeoutError, match="overall timeout"):
            sock.recv_into(bytearray(10))


def test_a_live_deadline_shrinks_the_socket_timeout(monkeypatch):
    """Each read gets only the time still left, not a fresh full timeout."""
    recorded = []
    monkeypatch.setattr(ssl.SSLSocket, "settimeout", lambda self, t: recorded.append(t))
    monkeypatch.setattr(ssl.SSLSocket, "recv_into", lambda self, *a, **k: 0)

    sock = hasheous._DeadlineSSLSocket.__new__(hasheous._DeadlineSSLSocket)
    with hasheous._deadline_of(10):
        sock.recv_into(bytearray(10))
        sock.recv_into(bytearray(10))

    assert len(recorded) == 2
    assert recorded[0] <= 10
    assert recorded[1] < recorded[0], "the deadline must not reset between reads"


def test_the_deadline_is_cleared_after_the_request():
    with hasheous._deadline_of(10):
        assert hasheous._deadline.at is not None
    assert hasheous._deadline.at is None


@pytest.mark.parametrize("mode", ["header", "body"])
def test_a_dripping_server_cannot_pin_a_lookup(
    hasheous_on, monkeypatch, tls_cert, mode, request
):
    """The end-to-end bound, against a real TLS server that never stops sending.

    ``urlopen(timeout=...)`` bounds each socket operation, and every byte that
    arrives resets it -- so a server dripping slower than the timeout pins the
    request forever without raising, and the cooldown never opens. Both drips
    matter: bounding only the body would leave the identical hole in the status
    line and headers.

    This runs against a real socket on purpose. The predecessor of this test
    used a fake response object, which returned promptly on every call and so
    passed against code that hung for real.
    """
    monkeypatch.setattr(settings, "hasheous_timeout", 2)

    def _drip(conn):
        if mode == "header":
            for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}":
                conn.sendall(bytes([byte]))
                time.sleep(0.3)
        else:
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
            for _ in range(100000):
                conn.sendall(b"x")
                time.sleep(0.3)

    # Hard guard: without the deadline this body drips for 30,000s, and a
    # hung job is a much worse CI failure than a red one. signal.alarm keeps
    # that bounded without adding a pytest-timeout dependency (tests run on the
    # main thread, so the handler fires).
    def _too_slow(_sig, _frm):
        raise AssertionError(f"{mode} drip was not bounded -- the deadline regressed")

    signal.signal(signal.SIGALRM, _too_slow)
    signal.alarm(60)
    request.addfinalizer(lambda: signal.alarm(0))  # noqa: PT021

    port = _tls_serve_once(_drip, tls_cert)

    # Trust the throwaway CA, keeping the deadline socket class under test.
    context = ssl.create_default_context(cafile=str(tls_cert / "c.pem"))
    context.sslsocket_class = hasheous._DeadlineSSLSocket
    monkeypatch.setattr(
        hasheous,
        "_opener",
        urllib.request.build_opener(
            hasheous._HTTPSOnlyRedirectHandler,
            urllib.request.HTTPSHandler(context=context),
        ),
    )

    started = time.monotonic()
    with pytest.raises(hasheous.HasheousUnavailable):
        hasheous._fetch_json(f"https://localhost:{port}/x")
    elapsed = time.monotonic() - started

    assert elapsed < 20, f"{mode} drip was not bounded ({elapsed:.1f}s)"


def test_a_normal_body_still_reads_whole(hasheous_on, tls_cert):
    """The deadline must not truncate or reject a legitimate response."""
    payload = json.dumps(SAMPLE_RESPONSE).encode()

    def _respond(conn):
        conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
        for i in range(0, len(payload), 64):
            part = payload[i : i + 64]
            conn.sendall(b"%x\r\n" % len(part) + part + b"\r\n")
        conn.sendall(b"0\r\n\r\n")

    port = _tls_serve_once(_respond, tls_cert)
    context = ssl.create_default_context(cafile=str(tls_cert / "c.pem"))
    context.sslsocket_class = hasheous._DeadlineSSLSocket
    with patch.object(
        hasheous,
        "_opener",
        urllib.request.build_opener(
            hasheous._HTTPSOnlyRedirectHandler,
            urllib.request.HTTPSHandler(context=context),
        ),
    ):
        data = hasheous._fetch_json(f"https://localhost:{port}/x")

    assert data["name"] == "Jumpman Junior"


# ---------------------------------------------------------------------------
# Eleventh review round (PR #273)
# ---------------------------------------------------------------------------


def test_the_handshake_also_honours_the_deadline():
    """The TLS handshake makes zero ``recv_into`` calls.

    Measured: wrapping a socket with a ``recv_into`` override records no calls
    at all -- the handshake reads through the C layer -- so ``do_handshake``
    has to apply the deadline itself or connect time is unbounded.
    """
    sock = hasheous._DeadlineSSLSocket.__new__(hasheous._DeadlineSSLSocket)
    with hasheous._deadline_of(-1):
        with pytest.raises(TimeoutError, match="overall timeout"):
            sock.do_handshake()


def test_the_lookup_url_follows_the_configured_base(monkeypatch):
    """One base-URL rule, so the request and the cache stamp cannot diverge.

    ``routes.dat.remote_stamp`` stamps cached misses with ``base_url()`` and
    ``cached_result_usable`` compares against that stamp; a second copy of the
    normalization in ``_lookup_url`` could drift from it and serve stale misses
    against a server that was never asked.
    """
    monkeypatch.setattr(settings, "hasheous_base_url", "https://self.hosted.example/")
    sha1 = "a" * 40

    assert hasheous._lookup_url(sha1).startswith(hasheous.base_url())
    assert hasheous._lookup_url(sha1) == (
        f"https://self.hosted.example/api/v1/Lookup/ByHash/sha1/{sha1}"
    )


# ---------------------------------------------------------------------------
# Twelfth review round (PR #273)
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """A fresh DATStore for the route under test, as tests/test_dat_routes.py does."""
    from services.dat_store import DATStore

    store = DATStore(store_path=str(tmp_path / "dat_store.json"))
    monkeypatch.setattr(dat_routes, "dat_store", store)
    return store


@pytest.mark.asyncio
async def test_a_single_file_match_always_recomputes(
    hasheous_on, tmp_path, isolated_store, monkeypatch
):
    """/dat/match means "match this file now" -- it must never serve a cache.

    Caching it was tried during review and reverted: DATMatch carries no
    freshness metadata, so a stored row would identify a replaced file as the
    game it used to be, for every existing caller, indefinitely.
    """
    iso = tmp_path / "game.iso"
    iso.write_bytes(b"content")
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)

    # A stale row for this path, as a previous match would have left.
    await isolated_store.set_match(str(iso), {
        "path": str(iso), "matched": True, "game_name": "Old Game",
        "match_type": "file_sha1", "file_hash": "a" * 40,
    })

    calls = []

    async def _fresh(path, **kwargs):
        calls.append(path)
        return {"path": path, "matched": True, "game_name": "New Game",
                "match_type": "file_sha1", "file_hash": "b" * 40}

    monkeypatch.setattr(dat_routes, "_match_single_file", _fresh)

    first = await dat_routes.match_file(dat_routes.MatchRequest(path=str(iso)))
    second = await dat_routes.match_file(dat_routes.MatchRequest(path=str(iso)))

    assert first["game_name"] == "New Game", "a stale cached row was served"
    assert second["game_name"] == "New Game"
    assert len(calls) == 2, "the route skipped a recompute"


@pytest.mark.asyncio
async def test_an_outage_does_not_cache_the_single_file_result(
    hasheous_on, tmp_path, isolated_store, monkeypatch
):
    """A transient failure must not be written to the cache by this route either."""
    iso = tmp_path / "game.iso"
    iso.write_bytes(b"content")
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)

    async def _fake_match(path, **kwargs):
        return {"path": path, "matched": False, "error": dat_routes.HASHEOUS_ERROR}

    monkeypatch.setattr(dat_routes, "_match_single_file", _fake_match)
    await dat_routes.match_file(dat_routes.MatchRequest(path=str(iso)))

    assert isolated_store.get_match(str(iso)) is None


@pytest.mark.asyncio
async def test_an_outage_keeps_a_cached_hit_whose_file_is_unchanged(tmp_path):
    """The round-7 rule: an outage says nothing about the file, so keep the row."""
    path = str(tmp_path / "game.chd")
    stored = {"file_hash": "a" * 40, "match_type": "file_sha1", "matched": True}
    with patch("services.dat_store.dat_store.get_match", return_value=stored), \
         patch("services.dat_store.dat_store.delete_match", new=AsyncMock()) as delete:
        await dat_routes.drop_if_content_changed(
            path, {"error": "hasheous unavailable", "file_hash": "a" * 40}
        )
    delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_outage_drops_a_cached_hit_whose_file_changed(tmp_path):
    """...but a file that demonstrably changed must not keep its old badge.

    Nothing would ever re-check it: cached_result_usable() accepts hits
    unconditionally, so the stale row would name the previous game forever.
    """
    path = str(tmp_path / "game.chd")
    stored = {"file_hash": "a" * 40, "match_type": "file_sha1", "matched": True}
    with patch("services.dat_store.dat_store.get_match", return_value=stored), \
         patch("services.dat_store.dat_store.delete_match", new=AsyncMock()) as delete:
        await dat_routes.drop_if_content_changed(
            path, {"error": "hasheous unavailable", "file_hash": "b" * 40}
        )
    delete.assert_awaited_once_with(path)


@pytest.mark.asyncio
async def test_an_unverifiable_outage_result_leaves_the_row_alone(tmp_path):
    """No recomputed hash (size cap, embedded-only) means no proof, so no delete."""
    path = str(tmp_path / "big.chd")
    stored = {"file_hash": "a" * 40, "match_type": "file_sha1", "matched": True}
    with patch("services.dat_store.dat_store.get_match", return_value=stored), \
         patch("services.dat_store.dat_store.delete_match", new=AsyncMock()) as delete:
        await dat_routes.drop_if_content_changed(
            path, {"error": "hasheous unavailable"}
        )
    delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_outage_result_carries_the_recomputed_hash(
    hasheous_on, tmp_path, isolated_store, monkeypatch
):
    """_match_single_file must surface the hash the freshness check needs.

    Without it the scan cannot tell "the service is down" from "the file
    changed", and falls back to keeping a possibly-stale badge.
    """
    iso = tmp_path / "game.iso"
    iso.write_bytes(b"content")
    sha1 = "c" * 40

    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", AsyncMock(return_value=sha1))
    monkeypatch.setattr(dat_routes.registry, "tool_for_verify", lambda _p: None)
    monkeypatch.setattr(
        dat_routes.dat_store, "has_dats", lambda: True,
    )
    monkeypatch.setattr(dat_routes.dat_store, "lookup_sha1", lambda _h: None)

    async def _down(_sha1):
        raise hasheous.HasheousUnavailable("down")

    monkeypatch.setattr(hasheous, "lookup", _down)

    result = await dat_routes._match_single_file(str(iso))

    assert result["error"] == dat_routes.HASHEOUS_ERROR
    assert result["file_hash"] == sha1, "outage result dropped the recomputed hash"


# ---------------------------------------------------------------------------
# Thirteenth review round (PR #273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_outage_keeps_a_chd_hit_matched_on_an_embedded_hash(tmp_path):
    """A CHD hit stores the *embedded* hash; the rescan recomputes the container.

    Those are different hash domains and differ for a perfectly unchanged file,
    so comparing them treats every such CHD as "changed" and deletes a valid
    cached hit during an outage -- the exact data loss the preserve rule exists
    to prevent, now aimed at hits specifically.
    """
    path = str(tmp_path / "game.chd")
    stored = {"file_hash": "a" * 40, "match_type": "chd_sha1", "matched": True}
    with patch("services.dat_store.dat_store.get_match", return_value=stored), \
         patch("services.dat_store.dat_store.delete_match", new=AsyncMock()) as delete:
        await dat_routes.drop_if_content_changed(
            path,
            {"error": "hasheous unavailable", "file_hash": "b" * 40},
        )
    delete.assert_not_awaited()


# ---------------------------------------------------------------------------
# Fourteenth review round (PR #273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dat_landing_mid_hash_still_wins_over_the_remote(
    hasheous_on, tmp_path, isolated_store, monkeypatch
):
    """Candidates are checked as they appear; hashing a file takes minutes.

    A DAT import or sync landing during that window can make an already-missed
    embedded hash locally known. Without a final local pass over the complete
    candidate set, it would be disclosed to Hasheous and a remote hit would win
    over an available local one.
    """
    chd = tmp_path / "game.chd"
    chd.write_bytes(b"content")
    embedded = "e" * 40
    file_level = "f" * 40

    class _Tool:
        embedded_hash_is_exhaustive = False

        async def embedded_hashes(self, _path, **_kw):
            return [(embedded, "chd_sha1")]

    monkeypatch.setattr(dat_routes.registry, "tool_for_verify", lambda _p: _Tool())
    monkeypatch.setattr(dat_routes, "compute_file_sha1", AsyncMock(return_value=file_level))
    monkeypatch.setattr(dat_routes.dat_store, "has_dats", lambda: True)

    # The DAT library gains the embedded hash only after the file hash is in.
    seen: list[str] = []

    def _lookup(h):
        seen.append(h)
        # Nothing known until the file-level hash has been computed...
        if file_level not in seen:
            return None
        # ...after which the import has landed and the embedded hash resolves.
        if h == embedded:
            return {"game_name": "Local Game", "dat_name": "Late Import"}
        return None

    monkeypatch.setattr(dat_routes.dat_store, "lookup_sha1", _lookup)

    async def _never(_sha1):
        raise AssertionError("went remote despite a local hit being available")

    monkeypatch.setattr(hasheous, "lookup", _never)

    result = await dat_routes._match_single_file(str(chd))

    assert result["matched"] is True
    assert result["game_name"] == "Local Game"
    assert result["source"] == "dat"


# ---------------------------------------------------------------------------
# Fifteenth review round (PR #273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelling_a_job_stops_the_remote_pass_between_candidates(hasheous_on):
    """A cancelled scan must not keep disclosing a CHD's remaining hashes.

    The outer job only notices cancellation once _match_single_file returns,
    so without a check between candidates it kept asking -- and paying a full
    timeout each -- after the operator hit stop.
    """
    cancel = asyncio.Event()
    asked: list[str] = []

    async def _lookup(sha1):
        asked.append(sha1)
        cancel.set()  # cancelled while the first request is in flight
        return None

    with patch.object(hasheous, "lookup", _lookup):
        match, consulted = await dat_routes._remote_lookup_match(
            "/vol/game.chd",
            [("a" * 40, "chd_sha1"), ("b" * 40, "chd_data_sha1"), ("c" * 40, "file_sha1")],
            cancel_event=cancel,
        )

    assert match is None
    assert len(asked) == 1, f"kept asking after cancellation: {asked}"
    # An incomplete pass must not be stamped as a completed remote check, or
    # cached_result_usable() would serve the miss forever.
    assert consulted is None


@pytest.mark.asyncio
async def test_concurrent_toggles_leave_stored_and_live_state_agreeing(monkeypatch):
    """The persisted value and the live override must not diverge.

    The write runs in a thread pool, so two concurrent toggles can commit in
    one order and resume in the other. For a privacy control that means a
    client who just switched the fallback OFF could keep sending hashes.
    """
    stored: dict = {}

    async def _slow_put(key, value):
        # Commit first, then yield. That is the real thread-pool shape: the
        # write lands, and only afterwards does the coroutine resume to apply
        # the override. It makes commit order and resume order disagree, which
        # is exactly the divergence the lock has to prevent.
        stored[key] = value
        await asyncio.sleep(0.01 if value["enabled"] else 0)

    monkeypatch.setattr(dat_routes.preferences_store, "put", _slow_put)

    await asyncio.gather(
        dat_routes.put_hasheous_settings(dat_routes.HasheousSettingsRequest(enabled=True)),
        dat_routes.put_hasheous_settings(dat_routes.HasheousSettingsRequest(enabled=False)),
    )

    assert stored[dat_routes.HASHEOUS_PREF_KEY]["enabled"] is hasheous.override(), (
        "persisted value and live override disagree"
    )


# ---------------------------------------------------------------------------
# Sixteenth review round (PR #273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_batch_routes_serve_cached_hits_with_matching_unavailable(
    tmp_path, isolated_store, monkeypatch
):
    """Turning Hasheous off must not hide badges a DAT-less library already has.

    Both batch entry points returned "unmatched" for every path *before*
    reading the cache, even though cached_result_usable() keeps hits valid
    regardless of the current provider and /dat/matches/lookup returns them.
    The client-side half of this was fixed earlier; this is the server half.
    """
    iso = tmp_path / "game.iso"
    iso.write_bytes(b"content")
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    # No DATs imported and Hasheous off: nothing can answer a NEW lookup.
    monkeypatch.setattr(dat_routes.dat_store, "has_dats", lambda: False)
    hasheous.set_enabled_override(False)

    await isolated_store.set_match(str(iso), {
        "path": str(iso), "matched": True, "game_name": "Cached Game",
        "match_type": "file_sha1", "file_hash": "a" * 40, "source": "hasheous",
    })

    batch = await dat_routes.match_batch(dat_routes.MatchBatchRequest(paths=[str(iso)]))
    assert batch["results"][str(iso)]["game_name"] == "Cached Game", (
        "match-batch hid a valid cached hit"
    )

    job = await dat_routes.match_batch_job(
        dat_routes.MatchBatchRequest(paths=[str(iso)]), BackgroundTasks(),
    )
    assert job["status"] == "idle"
    assert job["results"][str(iso)]["game_name"] == "Cached Game", (
        "match-batch/job hid a valid cached hit"
    )


# ---------------------------------------------------------------------------
# Eighteenth review round (PR #273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dat_sync_keeps_remote_hits(tmp_path):
    """A MAMERedump sync invalidates DAT-derived matches, not Hasheous ones.

    A remote hit owes nothing to the local DAT set. Wiping it meant the
    post-sync recompute missed locally and cached "unmatched", so the badge was
    gone for good unless the operator re-enabled remote disclosure -- even
    though cached_result_usable() keeps such hits valid with the provider off.

    The import has to be staged for real: `_persist_sync` returns early when
    nothing is pending, so seeding rows and calling persist() on an empty
    staging area exercises none of this.
    """
    from tests.test_dat_routes import SAMPLE_DAT_XML

    from services.dat_store import DATStore

    store = DATStore(store_path=str(tmp_path / "dat_store.json"))

    # Phase 1: a committed DAT, so a local hit can carry a real dat_id past the
    # FK guard in _upsert_match_sync.
    await store.import_dat_no_persist(SAMPLE_DAT_XML)
    await store.persist()
    dat_id = store.list_dats()[0]["id"]

    remote_path, local_path, miss_path = "/vol/remote.chd", "/vol/local.iso", "/vol/miss.iso"
    await store.set_match(remote_path, {
        "path": remote_path, "matched": True, "game_name": "Remote Game",
        "match_type": "file_sha1", "file_hash": "a" * 40, "source": "hasheous",
    })
    await store.set_match(local_path, {
        "path": local_path, "matched": True, "game_name": "Local Game",
        "dat_id": dat_id, "match_type": "file_sha1", "file_hash": "b" * 40,
    })
    await store.set_match(miss_path, {
        "path": miss_path, "matched": False, "file_hash": "c" * 40,
    })

    # Phase 2: a second sync, which is what invalidates the match cache.
    await store.import_dat_no_persist(SAMPLE_DAT_XML)
    await store.persist()

    survived = store.get_match(remote_path)
    assert survived is not None, "the sync destroyed a remote hit"
    assert survived["game_name"] == "Remote Game"
    assert store.get_match(local_path) is None, "a DAT-derived hit survived the sync"
    assert store.get_match(miss_path) is None, "a cached miss survived the sync"


# ---------------------------------------------------------------------------
# Nineteenth review round (PR #273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_user_dat_import_keeps_remote_hits(tmp_path):
    """The other import path was still wiping everything.

    Round 18 fixed `_persist_sync` (the MAMERedump sync path) and left
    `_import_dat_sync` (a user-uploaded DAT) deleting every row, so the same
    bug survived via the other door.
    """
    from tests.test_dat_routes import SAMPLE_DAT_XML

    from services.dat_store import DATStore

    store = DATStore(store_path=str(tmp_path / "dat_store.json"))
    remote_path, miss_path = "/vol/remote.chd", "/vol/miss.iso"

    await store.import_dat(SAMPLE_DAT_XML)
    await store.set_match(remote_path, {
        "path": remote_path, "matched": True, "game_name": "Remote Game",
        "match_type": "file_sha1", "file_hash": "a" * 40, "source": "hasheous",
    })
    await store.set_match(miss_path, {"path": miss_path, "matched": False})

    await store.import_dat(SAMPLE_DAT_XML)  # second import triggers invalidation

    survived = store.get_match(remote_path)
    assert survived is not None, "a user DAT import destroyed a remote hit"
    assert survived["game_name"] == "Remote Game"
    assert store.get_match(miss_path) is None, "a cached miss survived the import"


@pytest.mark.asyncio
async def test_a_local_miss_cannot_overwrite_a_remote_hit(tmp_path):
    """The post-sync rematch re-runs every previously-matched path.

    With Hasheous off, a path whose identity came only from the remote source
    misses locally and would be written back as "unmatched" -- undoing the
    selective invalidation. Preserving the row was not enough end to end.
    """
    from services.dat_store import DATStore

    store = DATStore(store_path=str(tmp_path / "dat_store.json"))
    path = "/vol/remote.chd"
    await store.set_match(path, {
        "path": path, "matched": True, "game_name": "Remote Game",
        "match_type": "file_sha1", "file_hash": "a" * 40, "source": "hasheous",
    })

    # What _match_single_file returns with Hasheous off and no local hit.
    await store.set_match(path, {"path": path, "matched": False, "checked_remote": None})

    kept = store.get_match(path)
    assert kept is not None and kept["matched"] is True, (
        "an unconsulted local miss clobbered a remote hit"
    )
    assert kept["game_name"] == "Remote Game"

    # ...but a miss recorded while the remote source WAS consulted supersedes it.
    await store.set_match(path, {
        "path": path, "matched": False, "checked_remote": "https://hasheous.org",
    })
    assert store.get_match(path)["matched"] is False, (
        "a genuine remote miss failed to supersede the stale hit"
    )


# ---------------------------------------------------------------------------
# Twentieth review round (PR #273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_manual_dat_import_schedules_a_rematch(tmp_path, isolated_store, monkeypatch):
    """Preserving remote hits requires re-running them against the new DAT.

    Rounds 18-19 stopped a DAT import from destroying remote hits. That alone
    inverts local-first: if the imported DAT *does* know the hash, the
    preserved remote row would be served forever. The MAMERedump sync path
    already snapshots and reschedules; the manual-import path did not.
    """
    from tests.test_dat_routes import SAMPLE_DAT_XML, _make_upload_file

    cached_path = "/vol/remote.chd"
    await isolated_store.set_match(cached_path, {
        "path": cached_path, "matched": True, "game_name": "Remote Game",
        "match_type": "file_sha1", "file_hash": "a" * 40, "source": "hasheous",
    })

    scheduled: list[list[str]] = []

    async def _capture(paths, **_kw):
        scheduled.append(list(paths))
        return "job-1"

    monkeypatch.setattr(dat_routes, "schedule_match_job", _capture)

    await dat_routes.import_dat(file=_make_upload_file(SAMPLE_DAT_XML))

    assert scheduled, "the import did not schedule a rematch"
    assert cached_path in scheduled[0], (
        "the preserved remote hit was not re-checked against the new DAT"
    )


@pytest.mark.asyncio
async def test_a_failed_rematch_schedule_does_not_fail_the_import(
    tmp_path, isolated_store, monkeypatch
):
    """The DAT is already committed by then; scheduling is best-effort."""
    from tests.test_dat_routes import SAMPLE_DAT_XML, _make_upload_file

    await isolated_store.set_match("/vol/x.chd", {"path": "/vol/x.chd", "matched": True})

    async def _boom(_paths, **_kw):
        raise RuntimeError("job queue full")

    monkeypatch.setattr(dat_routes, "schedule_match_job", _boom)

    result = await dat_routes.import_dat(file=_make_upload_file(SAMPLE_DAT_XML))
    assert result["name"] == "Test Redump DAT"


# ---------------------------------------------------------------------------
# Twenty-first review round (PR #273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_changed_file_loses_its_remote_badge(tmp_path):
    """The round-19 guard refused every unconsulted miss, including honest ones.

    With Hasheous off, replacing a file and running a forced scan produces an
    unmatched local result with no `checked_remote` -- which the guard rejected
    unconditionally, so the old hit kept naming the replaced file forever.
    """
    from services.dat_store import DATStore

    store = DATStore(store_path=str(tmp_path / "dat_store.json"))
    path = "/vol/game.iso"
    await store.set_match(path, {
        "path": path, "matched": True, "game_name": "Old Game",
        "match_type": "file_sha1", "file_hash": "a" * 40, "source": "hasheous",
    })

    # The file was replaced: same path, different content, no remote consulted.
    await store.set_match(path, {
        "path": path, "matched": False, "file_hash": "b" * 40, "checked_remote": None,
    })

    assert store.get_match(path)["matched"] is False, (
        "a replaced file kept its stale remote badge"
    )


@pytest.mark.asyncio
async def test_an_unchanged_file_keeps_its_remote_badge(tmp_path):
    """...but an identical hash is not proof of anything, so the hit stays."""
    from services.dat_store import DATStore

    store = DATStore(store_path=str(tmp_path / "dat_store.json"))
    path = "/vol/game.iso"
    await store.set_match(path, {
        "path": path, "matched": True, "game_name": "Old Game",
        "match_type": "file_sha1", "file_hash": "a" * 40, "source": "hasheous",
    })
    await store.set_match(path, {
        "path": path, "matched": False, "file_hash": "a" * 40, "checked_remote": None,
    })

    assert store.get_match(path)["matched"] is True


@pytest.mark.asyncio
async def test_a_chd_hit_is_not_judged_by_the_container_hash(tmp_path):
    """A CHD hit is stored against its embedded hash; the rescan computes the
    container's. Comparing across domains would call every unchanged CHD
    'changed' -- the same mistake round 13 fixed in the scan path."""
    from services.dat_store import DATStore

    store = DATStore(store_path=str(tmp_path / "dat_store.json"))
    path = "/vol/game.chd"
    await store.set_match(path, {
        "path": path, "matched": True, "game_name": "CHD Game",
        "match_type": "chd_sha1", "file_hash": "a" * 40, "source": "hasheous",
    })
    await store.set_match(path, {
        "path": path, "matched": False, "file_hash": "b" * 40, "checked_remote": None,
    })

    assert store.get_match(path)["matched"] is True, (
        "an unchanged CHD lost its badge to a cross-domain hash comparison"
    )


@pytest.mark.asyncio
async def test_the_batch_writer_honours_the_same_guard(tmp_path):
    """_set_matches_batch_sync updated rows unconditionally."""
    from services.dat_store import DATStore

    store = DATStore(store_path=str(tmp_path / "dat_store.json"))
    path = "/vol/game.iso"
    await store.set_match(path, {
        "path": path, "matched": True, "game_name": "Remote Game",
        "match_type": "file_sha1", "file_hash": "a" * 40, "source": "hasheous",
    })

    await store.set_matches_batch({
        path: {"path": path, "matched": False, "checked_remote": None},
    })

    assert store.get_match(path)["matched"] is True, (
        "a batch write erased a remote hit the single-path writer would refuse"
    )


# ---------------------------------------------------------------------------
# The deadline covers the pre-TLS phases too (raw connect, proxy CONNECT).
# ---------------------------------------------------------------------------


def test_the_opener_uses_the_deadline_aware_connection():
    """Pin the second half of the wiring.

    The socket class alone only covers what happens *after* the raw connection
    exists. Rebuilding the opener with a stock ``HTTPSHandler`` would silently
    hand connect and proxy CONNECT back to the stdlib while every behavioural
    test still passed.
    """
    handlers = [
        h for h in hasheous._opener.handlers
        if isinstance(h, urllib.request.HTTPSHandler)
    ]
    assert handlers, "opener has no HTTPS handler"
    assert all(isinstance(h, hasheous._DeadlineHTTPSHandler) for h in handlers)


def test_connect_spends_one_budget_across_several_addresses(monkeypatch):
    """A host resolving to N blackholes must not cost N x the timeout.

    ``socket.create_connection`` applies its timeout to each address in turn,
    so the documented whole-request bound did not survive a multi-address
    host -- measured against the real helper, three dropped addresses took
    9.0s under a 3s timeout.
    """
    addrs = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"192.0.2.{i}", 443))
        for i in (1, 2, 3)
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: addrs)

    budgets = []
    real_settimeout = socket.socket.settimeout

    def _record(self, value):
        budgets.append(value)
        real_settimeout(self, value)

    monkeypatch.setattr(socket.socket, "settimeout", _record)

    def _blackhole(self, sockaddr):
        time.sleep(0.2)
        raise TimeoutError("blackholed")

    monkeypatch.setattr(hasheous._DeadlineSocket, "connect", _blackhole)

    with hasheous._deadline_of(0.3):
        with pytest.raises(OSError):
            hasheous._connect_with_deadline(("multi.invalid", 443), 30)

    # Two attempts, not three: the budget is gone before the third address,
    # and the second attempt inherits only what the first left.
    assert len(budgets) == 2, budgets
    assert budgets[0] <= 0.3
    assert budgets[1] < budgets[0]


def _tcp_serve_once(handler) -> int:
    """Run a one-shot plain-TCP server on a loopback port and return the port."""
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)

    def _run():
        try:
            conn, _ = sock.accept()
            handler(conn)
            conn.close()
        except OSError:
            pass
        finally:
            sock.close()

    threading.Thread(target=_run, daemon=True).start()
    return sock.getsockname()[1]


def test_a_dripping_proxy_cannot_pin_a_lookup(request):
    """The CONNECT tunnel is read off the raw socket, before TLS exists.

    A proxy answering one byte at a time was the same failure the deadline was
    built to prevent -- it never raises -- and it was outside the TLS socket's
    reach, so it needed the raw socket to be deadline-aware too.
    """
    def _drip(conn):
        conn.recv(4096)  # the CONNECT request line + headers
        try:
            for _ in range(200):
                conn.sendall(b"X")
                time.sleep(0.5)
        except OSError:
            pass

    # A regressed deadline hangs rather than fails; a hung job is a much worse
    # CI failure than a red one.
    signal.alarm(60)
    request.addfinalizer(lambda: signal.alarm(0))  # noqa: PT021

    port = _tcp_serve_once(_drip)
    conn = hasheous._DeadlineHTTPSConnection("127.0.0.1", port, timeout=30)
    conn.set_tunnel("example.invalid", 443)

    started = time.monotonic()
    with hasheous._deadline_of(2):
        with pytest.raises(OSError):
            conn.connect()
    elapsed = time.monotonic() - started
    conn.close()

    assert elapsed < 10, f"the CONNECT drip was not bounded ({elapsed:.1f}s)"


@pytest.mark.asyncio
async def test_a_local_hit_from_a_deleted_dat_is_not_mistaken_for_a_remote_one(tmp_path):
    """"Remote hit" has to be recorded, not inferred from a null FK.

    ``_upsert_match_sync`` nulls a ``dat_id`` whose DAT no longer exists rather
    than violating the FK -- a local hit written while the operator was
    deleting that DAT. Preserving on ``dat_id IS NULL`` therefore classed it as
    remote: it dodged the ``WHERE dat_id = :id`` cascade at write time and then
    survived every later import, so browsing kept serving an identity from a
    DAT that had been removed.
    """
    from tests.test_dat_routes import SAMPLE_DAT_XML

    from services.dat_store import DATStore

    store = DATStore(store_path=str(tmp_path / "dat_store.json"))
    orphan, remote = "/vol/orphan.iso", "/vol/remote.chd"

    await store.import_dat(SAMPLE_DAT_XML)
    # A local hit whose DAT vanished before the write: the FK is nulled, but
    # the payload still says where the identity came from.
    await store.set_match(orphan, {
        "path": orphan, "matched": True, "game_name": "From A Deleted DAT",
        "dat_id": "gone1234", "match_type": "file_sha1", "file_hash": "c" * 40,
        "source": "dat",
    })
    await store.set_match(remote, {
        "path": remote, "matched": True, "game_name": "Remote Game",
        "match_type": "file_sha1", "file_hash": "a" * 40, "source": "hasheous",
    })
    assert store.get_match(orphan) is not None

    await store.import_dat(SAMPLE_DAT_XML)  # triggers invalidation

    assert store.get_match(orphan) is None, (
        "a local hit from a deleted DAT was preserved as if it were a remote hit"
    )
    assert store.get_match(remote) is not None, "the remote hit was not preserved"


@pytest.mark.asyncio
async def test_a_busy_matcher_queues_the_rematch_instead_of_dropping_it(
    tmp_path, monkeypatch,
):
    """"Best-effort" has to mean "later", not "never".

    The matcher is single-flight, so a DAT change landing while a match job
    runs got ``None`` from ``schedule_match_job`` -- and both callers stopped
    there. Nothing recovered those paths afterwards, so the files kept their
    old verdicts until someone browsed or rescanned them.
    """
    target = tmp_path / "queued.iso"
    target.write_bytes(b"x")
    path = str(target)

    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    monkeypatch.setattr(dat_routes, "_deferred_rematch_paths", set())

    # A match job is already running.
    monkeypatch.setattr(dat_routes, "_active_match_job_id", "busy-job")
    status, job_id = await dat_routes.rematch_after_dat_change(
        [path], source="test",
    )
    assert (status, job_id) == ("deferred", None)
    assert dat_routes._deferred_rematch_paths == {path}, "the rematch was dropped"

    # ...and when that job releases the slot, the queued work actually starts.
    monkeypatch.setattr(dat_routes, "_active_match_job_id", None)
    scheduled: list[list[str]] = []

    async def _fake_schedule(paths, **kwargs):
        scheduled.append(list(paths))
        return "drained-job"

    monkeypatch.setattr(dat_routes, "schedule_match_job", _fake_schedule)
    await dat_routes._drain_deferred_rematch()

    assert scheduled == [[path]], "the deferred rematch never started"
    assert not dat_routes._deferred_rematch_paths, "the queue was not cleared"


@pytest.mark.asyncio
async def test_a_dat_import_during_the_remote_await_still_wins(
    hasheous_on, tmp_path, isolated_store, monkeypatch,
):
    """Local-first has to hold across the whole call, not up to the last look.

    The remote request is an await of its own: an import committing inside it
    left the Hasheous answer authoritative, and because a cached hit is served
    unconditionally nothing ever recomputed it.
    """
    sha1 = "d" * 40
    imported: dict[str, dict | None] = {"row": None}

    async def _local(hash_value):
        return imported["row"]

    async def _remote(hash_value):
        # The import lands while the lookup is in flight.
        imported["row"] = {
            "dat_id": "dat1", "dat_name": "Local.dat", "game_name": "Local Name",
            "rom_name": "local.bin", "source": "dat",
        }
        return {"game_name": "Remote Name", "source": "hasheous"}

    monkeypatch.setattr(dat_routes, "_local_dat_record", _local)
    monkeypatch.setattr(dat_routes.hasheous, "lookup", _remote)

    match, consulted = await dat_routes._remote_lookup_match(
        "/vol/game.chd", [(sha1, "file_sha1")],
    )

    assert match is not None
    assert match["game_name"] == "Local Name", (
        "the remote answer outranked a DAT that had just been imported"
    )
    assert match["source"] == "dat"
    assert consulted  # the hash did go out; that part is unavoidable


@pytest.mark.asyncio
async def test_a_scan_reports_that_its_remote_phase_failed(
    hasheous_on, scan_phase_stubs, monkeypatch,
):
    """A rescan during an outage must not look like a clean "nothing matched".

    Every remote-only path lands on a non-cacheable HASHEOUS_ERROR, which is
    the right thing to do with the row -- but the phase used to swallow it and
    finish "0 matched", telling the operator nothing about the identities the
    rescan they asked for did not refresh.
    """
    import routes.dat as dat_internal
    from services.dat_store import dat_store as global_dat_store

    monkeypatch.setattr(global_dat_store, "has_dats", lambda: True)

    async def _unavailable(path, *, cancel_event=None):
        return {"path": path, "matched": False, "error": dat_routes.HASHEOUS_ERROR}

    monkeypatch.setattr(dat_internal, "_match_single_file", _unavailable)
    monkeypatch.setattr(dat_internal, "drop_if_content_changed", AsyncMock())
    monkeypatch.setattr(
        global_dat_store, "get_matches_batch", lambda paths: {p: None for p in paths},
    )

    matched, hasheous_errors = await scan_phase_stubs._scan_phase_dat_match(
        "job-1", ["/vol/a.iso", "/vol/b.iso"], force=True,
    )

    assert matched == 0
    assert hasheous_errors == 2, "the phase did not count its remote failures"


@pytest.mark.asyncio
async def test_an_empty_library_still_completes_its_scan(scan_phase_stubs):
    """Every return path has to carry the new (matched, errors) shape.

    The counter changed the signature, and the discovery-found-nothing path
    kept returning a bare int -- so an empty library failed its whole metadata
    scan with a TypeError instead of finishing quietly.
    """
    assert await scan_phase_stubs._scan_phase_dat_match("job-1", [], force=True) == (0, 0)


@pytest.mark.asyncio
async def test_a_dat_import_mid_lookup_wins_on_any_candidate(
    hasheous_on, monkeypatch,
):
    """The post-await recheck covers the whole candidate set, not one hash.

    A CHD sends up to three hashes. Checking only the one that happened to
    match remotely left the case where the DAT landing mid-flight knows a
    *later* candidate -- and the all-missed path had no recheck at all, so a
    miss could be stamped over an index that had just learned the answer.
    """
    header, data = "a" * 40, "b" * 40
    imported: dict[str, dict | None] = {}

    async def _local(hash_value):
        return imported.get(hash_value)

    async def _remote(hash_value):
        # The import lands during the first request and covers the *second*
        # candidate only.
        imported[data] = {
            "dat_id": "dat1", "dat_name": "Local.dat", "game_name": "Local Name",
            "rom_name": "local.bin", "source": "dat",
        }
        return {"game_name": "Remote Name", "source": "hasheous"} if hash_value == header else None

    monkeypatch.setattr(dat_routes, "_local_dat_record", _local)
    monkeypatch.setattr(dat_routes.hasheous, "lookup", _remote)

    match, consulted = await dat_routes._remote_lookup_match(
        "/vol/game.chd", [(header, "chd_sha1"), (data, "chd_data_sha1")],
    )

    assert match is not None
    assert match["game_name"] == "Local Name", (
        "a remote hit on one candidate hid a local match on another"
    )
    assert match["file_hash"] == data
    assert consulted


@pytest.mark.asyncio
async def test_a_dat_import_mid_lookup_beats_an_all_miss_pass(hasheous_on, monkeypatch):
    """The exit where nothing matched remotely needs the recheck too."""
    sha1 = "c" * 40
    imported: dict[str, dict | None] = {}

    async def _local(hash_value):
        return imported.get(hash_value)

    async def _remote(hash_value):
        imported[sha1] = {
            "dat_id": "dat1", "dat_name": "Local.dat", "game_name": "Local Name",
            "rom_name": "local.bin", "source": "dat",
        }
        return None  # a clean remote miss

    monkeypatch.setattr(dat_routes, "_local_dat_record", _local)
    monkeypatch.setattr(dat_routes.hasheous, "lookup", _remote)

    match, _ = await dat_routes._remote_lookup_match(
        "/vol/game.iso", [(sha1, "file_sha1")],
    )

    assert match is not None, "a miss was stamped over a DAT that had just landed"
    assert match["game_name"] == "Local Name"


@pytest.mark.asyncio
async def test_a_dat_landing_before_the_write_is_not_overwritten(tmp_path):
    """The decision and the write are separate operations.

    The route resolves local-first, but a DAT import can commit between that
    decision and ``set_match``. For a path with no prior row the import's
    rematch snapshot cannot cover it either, so the remote hit would be
    written *after* invalidation and then served unconditionally. The store
    re-checks inside the writing transaction and leaves the path uncached, so
    the next match resolves it locally.
    """
    from tests.test_dat_routes import SAMPLE_DAT_XML

    from services.dat_store import DATStore

    store = DATStore(store_path=str(tmp_path / "dat_store.json"))
    await store.import_dat(SAMPLE_DAT_XML)

    # A hash the imported DAT knows -- i.e. the import has landed by the time
    # this remote result is being persisted.
    known_sha1 = "aabbccddaabbccddaabbccddaabbccddaabbccdd"
    known = store.lookup_sha1(known_sha1)
    assert known is not None, "fixture DAT does not contain the expected hash"

    path = "/vol/late.chd"
    await store.set_match(path, {
        "path": path, "matched": True, "game_name": "Remote Name",
        "match_type": "file_sha1", "file_hash": known_sha1, "source": "hasheous",
    })

    assert store.get_match(path) is None, (
        "a remote hit was cached over a DAT that had already landed"
    )

    # The batch writer takes the same rule.
    await store.set_matches_batch({path: {
        "path": path, "matched": True, "game_name": "Remote Name",
        "match_type": "file_sha1", "file_hash": known_sha1, "source": "hasheous",
    }})
    assert store.get_match(path) is None, "the batch writer bypassed the guard"


@pytest.mark.asyncio
async def test_a_stale_failure_cannot_reopen_a_cleared_cooldown(hasheous_on, monkeypatch):
    """A Test press (or a toggle) outranks a lookup that started before it.

    Both clear the cooldown deliberately. An older in-flight lookup failing
    afterwards used to reopen it, so the panel said "reachable" while every
    match short-circuited as unavailable for the next 60 seconds.
    """
    started = asyncio.Event()
    release = asyncio.Event()

    def _slow_failure(url):
        # Signal that the lookup is in flight, then fail once released.
        started.set()
        asyncio.run_coroutine_threadsafe(_wait(), loop).result(timeout=5)
        raise hasheous.HasheousUnavailable("timed out")

    async def _wait():
        await release.wait()

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(hasheous, "_fetch_json", _slow_failure)

    lookup = asyncio.create_task(hasheous.lookup("a" * 40))
    await asyncio.wait_for(started.wait(), timeout=5)

    # The probe succeeds while that lookup is still out.
    hasheous._clear_cooldown()

    release.set()
    with pytest.raises(hasheous.HasheousUnavailable):
        await lookup

    assert hasheous._cooldown_remaining() == 0, (
        "a failure that started before the successful probe reopened the cooldown"
    )


def test_a_failure_still_opens_the_cooldown_normally(hasheous_on, monkeypatch):
    """The ordering guard must not disarm the breaker in the ordinary case."""
    def _boom(url):
        raise hasheous.HasheousUnavailable("down")

    monkeypatch.setattr(hasheous, "_fetch_json", _boom)
    with pytest.raises(hasheous.HasheousUnavailable):
        asyncio.run(hasheous.lookup("b" * 40))
    assert hasheous._cooldown_remaining() > 0, "the breaker stopped tripping"


# ---------------------------------------------------------------------------
# Twenty-eighth review round (PR #273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_skipped_file_cannot_hide_a_total_outage(monkeypatch):
    """Skips are not survivors, so they must not sit in the failure denominator.

    A batch of remotely-unreachable files plus a single oversized ISO made
    ``errors == total`` false, so a complete provider outage was downgraded to
    a green "complete" carrying a generic error count -- the exact misreading
    the all-failed branch exists to prevent.
    """
    from services.job_manager import job_manager

    scan_job = job_manager.create_external_job(
        filename="DAT Match",
        mode=dat_routes.ConversionMode.DAT_MATCH,
        message="test",
    )
    monkeypatch.setattr(dat_routes, "_active_match_job_id", scan_job.id)

    async def _outage_plus_one_skip(path, *, cancel_event=None, local_only=False):
        if path == "/big.iso":
            # A policy skip: over the size cap, no error, nothing cacheable.
            return {"path": path, "matched": False, "reason": "too large"}, False
        return (
            {"path": path, "matched": False, "error": dat_routes.HASHEOUS_ERROR},
            False,
        )

    monkeypatch.setattr(dat_routes, "_hash_one_for_job", _outage_plus_one_skip)

    await dat_routes._run_match_job(
        job_id=scan_job.id, paths_to_compute=["/a.chd", "/b.chd", "/big.iso"],
    )

    final = job_manager.jobs[scan_job.id]
    assert final.status.value == "failed", (
        "one skipped file turned a total outage into a green job"
    )
    assert "all 2 file(s) failed" in final.message, (
        "the skipped file was counted as a survivor"
    )
    assert "Hasheous is unreachable" in final.message


@pytest.mark.asyncio
async def test_a_skip_alone_is_still_a_completed_job(monkeypatch):
    """...but a batch of nothing *but* skips has not failed at all.

    Excluding skips from the denominator must not make an empty checkable set
    divide into a failure: nothing was attempted, so nothing went wrong.
    """
    from services.job_manager import job_manager

    scan_job = job_manager.create_external_job(
        filename="DAT Match",
        mode=dat_routes.ConversionMode.DAT_MATCH,
        message="test",
    )
    monkeypatch.setattr(dat_routes, "_active_match_job_id", scan_job.id)

    async def _all_skips(path, *, cancel_event=None, local_only=False):
        return {"path": path, "matched": False, "reason": "too large"}, False

    monkeypatch.setattr(dat_routes, "_hash_one_for_job", _all_skips)

    await dat_routes._run_match_job(
        job_id=scan_job.id, paths_to_compute=["/big1.iso", "/big2.iso"],
    )

    final = job_manager.jobs[scan_job.id]
    assert final.status.value == "completed"
    assert "2 skipped" in final.message


@pytest.mark.asyncio
async def test_a_stamped_miss_is_not_written_over_a_dat_that_just_landed(tmp_path):
    """The write-boundary guard covers misses, not only hits.

    A clean remote miss is cacheable and its ``checked_remote`` stamp stays
    valid, so a DAT import committing between the route's last local pass and
    the write left the fresh local match hidden until the *next* import
    happened to invalidate the row.
    """
    from tests.test_dat_routes import SAMPLE_DAT_XML

    from services.dat_store import CANDIDATE_HASHES_KEY, DATStore

    store = DATStore(store_path=str(tmp_path / "dat_store.json"))
    await store.import_dat(SAMPLE_DAT_XML)

    known_sha1 = "aabbccddaabbccddaabbccddaabbccddaabbccdd"
    assert store.lookup_sha1(known_sha1) is not None, "fixture DAT changed"

    path = "/vol/late.chd"
    miss = {
        "path": path, "matched": False, "checked_remote": "https://hasheous.org",
        CANDIDATE_HASHES_KEY: [(known_sha1, "file_sha1")],
    }
    await store.set_match(path, dict(miss))
    assert store.get_match(path) is None, (
        "a stamped miss was cached over a DAT that had already landed"
    )

    await store.set_matches_batch({path: dict(miss)})
    assert store.get_match(path) is None, "the batch writer bypassed the guard"


@pytest.mark.asyncio
async def test_a_remote_hit_yields_to_a_dat_covering_another_candidate(tmp_path):
    """A CHD sends up to three hashes; the guard has to check all of them.

    Examining only the hash that happened to match remotely left the case where
    the DAT landing mid-flight knows a *different* candidate -- a local
    identity exists, and the remote answer would have outranked it for good.
    """
    from tests.test_dat_routes import SAMPLE_DAT_XML

    from services.dat_store import CANDIDATE_HASHES_KEY, DATStore

    store = DATStore(store_path=str(tmp_path / "dat_store.json"))
    await store.import_dat(SAMPLE_DAT_XML)

    known_sha1 = "aabbccddaabbccddaabbccddaabbccddaabbccdd"
    unknown_sha1 = "f" * 40
    assert store.lookup_sha1(unknown_sha1) is None

    path = "/vol/late.chd"
    hit = {
        "path": path, "matched": True, "game_name": "Remote Name",
        "match_type": "chd_sha1", "file_hash": unknown_sha1, "source": "hasheous",
        # The header hash matched remotely; the DAT that landed knows the data
        # hash instead.
        CANDIDATE_HASHES_KEY: [(unknown_sha1, "chd_sha1"),
                               (known_sha1, "chd_data_sha1")],
    }
    await store.set_match(path, dict(hit))
    assert store.get_match(path) is None, (
        "a remote hit was cached while the local index covered another candidate"
    )

    await store.set_matches_batch({path: dict(hit)})
    assert store.get_match(path) is None, "the batch writer bypassed the guard"


@pytest.mark.asyncio
async def test_the_candidate_list_is_a_check_input_not_a_cached_field(tmp_path):
    """It is revalidated at the write boundary, then dropped.

    Persisting it would ship the file's other hashes back to the UI on every
    lookup and bloat the payload with something no consumer reads.
    """
    from services.dat_store import CANDIDATE_HASHES_KEY, DATStore

    store = DATStore(store_path=str(tmp_path / "dat_store.json"))
    path = "/vol/game.chd"
    await store.set_match(path, {
        "path": path, "matched": False, "checked_remote": "https://hasheous.org",
        CANDIDATE_HASHES_KEY: [("a" * 40, "chd_sha1"), ("b" * 40, "chd_data_sha1")],
    })

    cached = store.get_match(path)
    assert cached is not None, "an unrelated candidate set blocked the write"
    assert CANDIDATE_HASHES_KEY not in cached


@pytest.mark.asyncio
async def test_the_route_hands_its_candidates_to_the_write_boundary(
    hasheous_on, monkeypatch,
):
    """The guard is only as good as what the route attaches to the verdict.

    Both cacheable remote exits -- the stamped miss and the hit -- carry the
    complete candidate set, so the store can re-check every one of them inside
    the writing transaction.
    """
    from services.dat_store import CANDIDATE_HASHES_KEY

    header, data = "a" * 40, "b" * 40

    class _Chd:
        embedded_hash_is_exhaustive = True

        async def embedded_hashes(self, path, *, cancel_event=None):
            return [(header, "chd_sha1"), (data, "chd_data_sha1")]

    monkeypatch.setattr(dat_routes.dat_store, "has_dats", lambda: True)
    monkeypatch.setattr(dat_routes.registry, "tool_for_verify", lambda _p: _Chd())
    monkeypatch.setattr(dat_routes, "_local_dat_record", AsyncMock(return_value=None))

    # Every candidate misses remotely: the stamped-miss exit.
    monkeypatch.setattr(hasheous, "lookup", AsyncMock(return_value=None))
    miss = await dat_routes._match_single_file("/vol/game.chd")
    assert miss["matched"] is False
    assert miss[CANDIDATE_HASHES_KEY] == [(header, "chd_sha1"), (data, "chd_data_sha1")]

    # The first candidate hits: the remote-hit exit still carries both.
    async def _hit(sha1):
        return {"game_name": "Remote Name", "source": "hasheous"} if sha1 == header else None

    monkeypatch.setattr(hasheous, "lookup", _hit)
    hit = await dat_routes._match_single_file("/vol/game.chd")
    assert hit["matched"] is True
    assert hit[CANDIDATE_HASHES_KEY] == [(header, "chd_sha1"), (data, "chd_data_sha1")]


# ---------------------------------------------------------------------------
# Twenty-ninth review round (PR #273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_hit_written_during_an_import_is_still_rematched(monkeypatch):
    """The snapshot is not transactional with the invalidation it precedes.

    A match already in flight can persist a remote hit between the two. The
    import preserves it (remote hits owe nothing to the local DATs), the
    snapshot cannot contain it, and a cached hit is always usable -- so the
    DAT just imported would never get to say it knows that hash.
    """
    scheduled: list[list[str]] = []

    async def _schedule(paths, **_kwargs):
        scheduled.append(list(paths))
        return "job-1"

    monkeypatch.setattr(dat_routes, "schedule_match_job", _schedule)
    monkeypatch.setattr(
        dat_routes.dat_store, "list_match_paths", lambda: ["/vol/late.chd"],
    )

    status, job_id = await dat_routes.rematch_after_dat_change(
        [], source="import_dat",
    )

    assert (status, job_id) == ("scheduled", "job-1")
    assert scheduled == [["/vol/late.chd"]], (
        "a row that survived the import was never scheduled for rematch"
    )


@pytest.mark.asyncio
async def test_the_rematch_set_is_the_union_of_before_and_after(monkeypatch):
    """Survivors are added to the snapshot, not substituted for it.

    The snapshot still carries the rows invalidation is about to *delete*,
    which the after-listing by definition cannot.
    """
    scheduled: list[list[str]] = []

    async def _schedule(paths, **_kwargs):
        scheduled.append(list(paths))
        return "job-1"

    monkeypatch.setattr(dat_routes, "schedule_match_job", _schedule)
    monkeypatch.setattr(
        dat_routes.dat_store, "list_match_paths",
        lambda: ["/vol/kept.chd", "/vol/late.chd"],
    )

    await dat_routes.rematch_after_dat_change(
        ["/vol/gone.iso", "/vol/kept.chd"], source="dat_sync",
    )

    assert scheduled == [
        ["/vol/gone.iso", "/vol/kept.chd", "/vol/late.chd"],
    ], "the union dropped, duplicated or mis-ordered a path"


@pytest.mark.asyncio
async def test_a_failed_survivor_listing_still_rematches_the_snapshot(monkeypatch):
    """Best-effort, like the caller: the DATs are committed either way.

    Rematching the snapshot alone is strictly better than rematching nothing,
    so a broken store must not cost the part that would have worked.
    """
    scheduled: list[list[str]] = []

    async def _schedule(paths, **_kwargs):
        scheduled.append(list(paths))
        return "job-1"

    def _boom():
        raise RuntimeError("db not initialised")

    monkeypatch.setattr(dat_routes, "schedule_match_job", _schedule)
    monkeypatch.setattr(dat_routes.dat_store, "list_match_paths", _boom)

    status, _job_id = await dat_routes.rematch_after_dat_change(
        ["/vol/a.chd"], source="import_dat",
    )

    assert status == "scheduled"
    assert scheduled == [["/vol/a.chd"]]


@pytest.mark.asyncio
async def test_an_import_leaves_its_surviving_hit_where_the_rematch_looks(tmp_path):
    """The assumption the union rests on, pinned.

    Invalidation preserves remote hits, so listing the match cache *after* an
    import returns exactly the rows that need re-checking against the new
    index -- including one written too late for the snapshot.
    """
    from tests.test_dat_routes import SAMPLE_DAT_XML

    from services.dat_store import DATStore

    store = DATStore(store_path=str(tmp_path / "dat_store.json"))
    path = "/vol/late.chd"
    await store.set_match(path, {
        "path": path, "matched": True, "game_name": "Remote Name",
        "match_type": "file_sha1", "file_hash": "f" * 40, "source": "hasheous",
    })
    # A local miss, which invalidation is meant to drop.
    await store.set_match("/vol/gone.iso", {
        "path": "/vol/gone.iso", "matched": False,
    })

    await store.import_dat(SAMPLE_DAT_XML)

    assert store.list_match_paths() == [path], (
        "the surviving remote hit is not visible to the post-import listing"
    )


# ---------------------------------------------------------------------------
# Thirtieth review round (PR #273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dat_change_does_not_re_ask_hasheous_about_every_file(
    hasheous_on, monkeypatch,
):
    """The rematch is local-only, and this is the claim the whole cost story rests on.

    A DAT change alters the local index and nothing else. Re-running the full
    pipeline meant every previously-verdicted file went back out to Hasheous --
    thousands of hash disclosures per MAMERedump sync, for answers that cannot
    differ from the ones already cached.
    """
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

    sent = []

    async def _remote(sha1):
        sent.append(sha1)
        return None

    monkeypatch.setattr(hasheous, "lookup", _remote)

    result = await dat_routes._match_single_file("/vol/game.chd", local_only=True)

    assert sent == [], "a local-only rematch still disclosed hashes to Hasheous"
    assert result["matched"] is False
    assert result.get("checked_remote") is None, (
        "an unasked pass must not be stamped as a completed remote check"
    )

    # The control: the same file, ordinary mode, does consult it.
    sent.clear()
    await dat_routes._match_single_file("/vol/game.chd")
    assert sent == [header, container]


@pytest.mark.asyncio
async def test_a_local_only_miss_cannot_erase_the_badge_it_could_not_check(tmp_path):
    """...and the file keeps the identity the rematch had no way to re-derive.

    An unstamped miss is exactly what _would_downgrade_remote_hit() refuses to
    write over a remote hit, so "don't ask again" costs nothing: the row that
    the new DATs still don't cover simply stays.
    """
    from services.dat_store import DATStore

    store = DATStore(store_path=str(tmp_path / "dat_store.json"))
    path = "/vol/game.chd"
    await store.set_match(path, {
        "path": path, "matched": True, "game_name": "Remote Name",
        "match_type": "chd_sha1", "file_hash": "a" * 40, "source": "hasheous",
    })

    # What a local-only rematch produces when the new DATs still miss.
    await store.set_match(path, {"path": path, "matched": False, "checked_remote": None})

    cached = store.get_match(path)
    assert cached is not None and cached["matched"] is True, (
        "the local-only rematch erased a hit it never re-checked"
    )
    assert cached["game_name"] == "Remote Name"


@pytest.mark.asyncio
async def test_the_rematch_asks_for_a_local_only_job(monkeypatch):
    """The flag has to reach the scheduler, not just exist on the job."""
    seen: list[dict] = []

    async def _schedule(paths, **kwargs):
        seen.append(kwargs)
        return "job-1"

    monkeypatch.setattr(dat_routes, "schedule_match_job", _schedule)
    monkeypatch.setattr(dat_routes.dat_store, "list_match_paths", lambda: [])

    await dat_routes.rematch_after_dat_change(["/vol/a.chd"], source="import_dat")

    assert seen == [{"defer_if_busy": True, "local_only": True}]


@pytest.mark.asyncio
async def test_a_dat_import_mid_lookup_stops_the_next_candidate_going_out(
    hasheous_on, monkeypatch,
):
    """Local-first is about disclosure, not only about the verdict.

    The complete local pass sat after the whole remote loop, so a DAT import
    landing during candidate 1's request could not stop candidate 2 being sent
    -- the recheck ran once the hash was already gone.
    """
    header, data = "a" * 40, "b" * 40
    imported: dict[str, dict | None] = {}

    async def _local(hash_value):
        return imported.get(hash_value)

    sent = []

    async def _remote(hash_value):
        sent.append(hash_value)
        # The import lands during the first request and covers the second
        # candidate -- the one about to go out.
        imported[data] = {
            "dat_id": "dat1", "dat_name": "Local.dat", "game_name": "Local Name",
            "rom_name": "local.bin", "source": "dat",
        }
        return None

    monkeypatch.setattr(dat_routes, "_local_dat_record", _local)
    monkeypatch.setattr(dat_routes.hasheous, "lookup", _remote)

    match, consulted = await dat_routes._remote_lookup_match(
        "/vol/game.chd", [(header, "chd_sha1"), (data, "chd_data_sha1")],
    )

    assert sent == [header], "the second candidate was disclosed after the DAT landed"
    assert match is not None and match["game_name"] == "Local Name"
    assert consulted, "the first hash did go out; the stamp must say so"


@pytest.mark.asyncio
async def test_a_cancelled_job_does_not_start_the_rematch_it_was_holding(monkeypatch):
    """"Stop everything" has to mean it.

    The deferred drain ran from the cancelled job's own teardown, and
    /jobs/cancel-all snapshots the job list before that replacement exists --
    so the rematch escaped the cancellation and kept hashing.
    """
    from services.job_manager import job_manager

    scan_job = job_manager.create_external_job(
        filename="DAT Match",
        mode=dat_routes.ConversionMode.DAT_MATCH,
        message="test",
    )
    monkeypatch.setattr(dat_routes, "_active_match_job_id", scan_job.id)
    monkeypatch.setattr(dat_routes, "_deferred_rematch_paths", {"/vol/queued.chd"})

    started: list[list[str]] = []

    async def _schedule(paths, **_kwargs):
        started.append(list(paths))
        return "job-2"

    monkeypatch.setattr(dat_routes, "schedule_match_job", _schedule)

    async def _cancelled(path, *, cancel_event=None, local_only=False):
        raise dat_routes.ExternalJobCancelled()

    monkeypatch.setattr(dat_routes, "_hash_one_for_job", _cancelled)

    await dat_routes._run_match_job(
        job_id=scan_job.id, paths_to_compute=["/vol/a.chd"],
    )

    assert job_manager.jobs[scan_job.id].status.value == "cancelled"
    assert started == [], "a cancelled job spawned the rematch it was holding"
    assert not dat_routes._deferred_rematch_paths, (
        "the dropped queue must not linger for an unrelated job to pick up"
    )


@pytest.mark.asyncio
async def test_a_completed_job_still_hands_over_its_deferred_rematch(monkeypatch):
    """...but only cancellation drops it. A normal finish still drains."""
    from services.job_manager import job_manager

    scan_job = job_manager.create_external_job(
        filename="DAT Match",
        mode=dat_routes.ConversionMode.DAT_MATCH,
        message="test",
    )
    monkeypatch.setattr(dat_routes, "_active_match_job_id", scan_job.id)
    monkeypatch.setattr(dat_routes, "_deferred_rematch_paths", {"/vol/queued.chd"})

    started: list[list[str]] = []

    async def _schedule(paths, **_kwargs):
        started.append(list(paths))
        return "job-2"

    monkeypatch.setattr(dat_routes, "schedule_match_job", _schedule)

    async def _ok(path, *, cancel_event=None, local_only=False):
        return {"path": path, "matched": False}, True

    monkeypatch.setattr(dat_routes, "_hash_one_for_job", _ok)
    monkeypatch.setattr(dat_routes.dat_store, "set_match", AsyncMock())

    await dat_routes._run_match_job(
        job_id=scan_job.id, paths_to_compute=["/vol/a.chd"],
    )

    assert started == [["/vol/queued.chd"]]


# ---------------------------------------------------------------------------
# Thirty-first review round (PR #273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_local_only_miss_carries_the_hash_that_proves_a_swap(
    hasheous_on, monkeypatch,
):
    """"Still unknown" and "different file now" both arrive as an unmatched result.

    Only the recomputed file-level hash tells them apart, and the local-only
    exit dropped it -- so the very guard that protects a remote badge (round 30)
    would protect it on a file that had been replaced.
    """
    container = "c" * 40

    monkeypatch.setattr(dat_routes.dat_store, "has_dats", lambda: True)
    monkeypatch.setattr(dat_routes.registry, "tool_for_verify", lambda _p: None)
    monkeypatch.setattr(
        dat_routes, "compute_file_sha1", AsyncMock(return_value=container),
    )
    monkeypatch.setattr(dat_routes, "_local_dat_record", AsyncMock(return_value=None))

    result = await dat_routes._match_single_file("/vol/game.iso", local_only=True)

    assert result["matched"] is False
    assert result["file_hash"] == container, (
        "the local-only miss dropped the proof that the file changed"
    )


@pytest.mark.asyncio
async def test_a_replaced_file_loses_a_remote_badge_it_no_longer_earns(tmp_path):
    """The end of that chain: the store must be able to act on the hash.

    A cached remote hit plus an unmatched recompute whose file-level hash
    differs is the one case `_would_downgrade_remote_hit` is meant to let
    through -- and it can only see it if the hash arrived.
    """
    from services.dat_store import DATStore

    store = DATStore(store_path=str(tmp_path / "dat_store.json"))
    path = "/vol/game.iso"
    await store.set_match(path, {
        "path": path, "matched": True, "game_name": "Old Game",
        "match_type": "file_sha1", "file_hash": "a" * 40, "source": "hasheous",
    })

    # What the fixed local-only rematch produces for a file that was swapped.
    await store.set_match(path, {
        "path": path, "matched": False, "checked_remote": None,
        "file_hash": "b" * 40,
    })
    assert store.get_match(path)["matched"] is False, (
        "a replaced file kept the previous game's badge"
    )

    # ...and the unchanged file still keeps its badge, which is the whole
    # point of the guard.
    other = "/vol/kept.iso"
    await store.set_match(other, {
        "path": other, "matched": True, "game_name": "Kept Game",
        "match_type": "file_sha1", "file_hash": "c" * 40, "source": "hasheous",
    })
    await store.set_match(other, {
        "path": other, "matched": False, "checked_remote": None,
        "file_hash": "c" * 40,
    })
    assert store.get_match(other)["game_name"] == "Kept Game"


@pytest.mark.asyncio
async def test_an_outage_miss_still_carries_its_hash_through_the_shared_helper(
    hasheous_on, monkeypatch,
):
    """The behaviour that was already right must survive being factored out."""
    container = "c" * 40

    monkeypatch.setattr(dat_routes.dat_store, "has_dats", lambda: True)
    monkeypatch.setattr(dat_routes.registry, "tool_for_verify", lambda _p: None)
    monkeypatch.setattr(
        dat_routes, "compute_file_sha1", AsyncMock(return_value=container),
    )
    monkeypatch.setattr(dat_routes, "_local_dat_record", AsyncMock(return_value=None))
    monkeypatch.setattr(
        hasheous, "lookup",
        AsyncMock(side_effect=hasheous.HasheousUnavailable("timed out")),
    )

    result = await dat_routes._match_single_file("/vol/game.iso")

    assert result["error"] == dat_routes.HASHEOUS_ERROR
    assert result["file_hash"] == container


# ---------------------------------------------------------------------------
# Thirty-second review round (PR #273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_exhaustive_format_carries_its_own_typed_hash(
    hasheous_on, monkeypatch,
):
    """Round 31 kept only ``file_sha1``, which an exhaustive tool never has.

    Dolphin RVZ/WIA/GCZ report one disc SHA1 and the matcher deliberately
    never reads the container, so the "carry the proof" fix skipped exactly
    the formats whose embedded hash *is* the identity.
    """
    from services.dat_store import CANDIDATE_HASHES_KEY

    disc = "d" * 40

    class _Dolphin:
        embedded_hash_is_exhaustive = True

        async def embedded_hashes(self, path, *, cancel_event=None):
            return [(disc, "dolphin_disc_sha1")]

    monkeypatch.setattr(dat_routes.dat_store, "has_dats", lambda: True)
    monkeypatch.setattr(dat_routes.registry, "tool_for_verify", lambda _p: _Dolphin())
    monkeypatch.setattr(dat_routes, "_local_dat_record", AsyncMock(return_value=None))
    # Nothing may read the container for an exhaustive format.
    monkeypatch.setattr(
        dat_routes, "compute_file_sha1",
        AsyncMock(side_effect=AssertionError("read the whole file")),
    )

    result = await dat_routes._match_single_file("/vol/game.rvz", local_only=True)

    assert result["matched"] is False
    assert result[CANDIDATE_HASHES_KEY] == [(disc, "dolphin_disc_sha1")]
    assert "file_hash" not in result, "an exhaustive format has no file-level hash"


@pytest.mark.asyncio
async def test_a_replaced_rvz_loses_the_badge_its_disc_hash_no_longer_earns(tmp_path):
    """...and the store can now act on it, because the domains agree.

    The guard demanded ``match_type == "file_sha1"``, which is right for a CHD
    (embedded hash stored, container hash recomputed -- different domains) and
    wrong for an exhaustive tool, which recomputes the very hash it matched on.
    """
    from services.dat_store import CANDIDATE_HASHES_KEY, DATStore

    store = DATStore(store_path=str(tmp_path / "dat_store.json"))
    path = "/vol/game.rvz"
    await store.set_match(path, {
        "path": path, "matched": True, "game_name": "Old Game",
        "match_type": "dolphin_disc_sha1", "file_hash": "a" * 40,
        "source": "hasheous",
    })

    # A local-only rematch of the replaced file: same domain, different hash.
    await store.set_match(path, {
        "path": path, "matched": False, "checked_remote": None,
        CANDIDATE_HASHES_KEY: [("b" * 40, "dolphin_disc_sha1")],
    })
    assert store.get_match(path)["matched"] is False, (
        "a replaced RVZ kept the previous game's badge"
    )

    # The unchanged file keeps its badge -- the guard must not become "always
    # downgrade" now that it accepts more domains.
    other = "/vol/kept.rvz"
    await store.set_match(other, {
        "path": other, "matched": True, "game_name": "Kept Game",
        "match_type": "dolphin_disc_sha1", "file_hash": "c" * 40,
        "source": "hasheous",
    })
    await store.set_match(other, {
        "path": other, "matched": False, "checked_remote": None,
        CANDIDATE_HASHES_KEY: [("c" * 40, "dolphin_disc_sha1")],
    })
    assert store.get_match(other)["game_name"] == "Kept Game"


@pytest.mark.asyncio
async def test_a_chd_container_hash_still_cannot_disprove_an_embedded_hit(tmp_path):
    """The cross-domain refusal this widening must not lose.

    A CHD hit is stored against its embedded ``chd_sha1``; a rescan recomputes
    the *container* ``file_sha1``. Those differ for a file nobody touched, so
    comparing them would delete valid badges -- which is the bug the original
    ``match_type == "file_sha1"`` restriction was added to fix.
    """
    from services.dat_store import CANDIDATE_HASHES_KEY, DATStore

    store = DATStore(store_path=str(tmp_path / "dat_store.json"))
    path = "/vol/game.chd"
    await store.set_match(path, {
        "path": path, "matched": True, "game_name": "Kept Game",
        "match_type": "chd_sha1", "file_hash": "a" * 40, "source": "hasheous",
    })

    # The recompute offers only a container hash -- a different domain.
    await store.set_match(path, {
        "path": path, "matched": False, "checked_remote": None,
        "file_hash": "f" * 40,
        CANDIDATE_HASHES_KEY: [("f" * 40, "file_sha1")],
    })

    assert store.get_match(path)["game_name"] == "Kept Game", (
        "a container hash was compared against an embedded one"
    )


# ---------------------------------------------------------------------------
# Thirty-third review round (PR #273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_outage_rescan_prunes_a_replaced_rvz(tmp_path):
    """The scan's pruner compares in the row's own domain too.

    It demanded a stored ``file_sha1``, which an exhaustive format never has --
    so a forced rescan during an outage recomputed a fresh
    ``dolphin_disc_sha1``, held the proof in its hand, and left the previous
    game's badge in place.
    """
    from services.dat_store import CANDIDATE_HASHES_KEY

    path = "/vol/game.rvz"
    stored = {
        "matched": True, "game_name": "Old Game",
        "match_type": "dolphin_disc_sha1", "file_hash": "a" * 40,
    }
    with patch("services.dat_store.dat_store.get_match", return_value=stored), \
         patch("services.dat_store.dat_store.delete_match", new=AsyncMock()) as delete:
        await dat_routes.drop_if_content_changed(path, {
            "error": dat_routes.HASHEOUS_ERROR,
            CANDIDATE_HASHES_KEY: [("b" * 40, "dolphin_disc_sha1")],
        })
    delete.assert_awaited_once_with(path)


@pytest.mark.asyncio
async def test_an_outage_rescan_keeps_an_unchanged_rvz(tmp_path):
    """...and an identical disc hash is still not proof of anything."""
    from services.dat_store import CANDIDATE_HASHES_KEY

    path = "/vol/game.rvz"
    stored = {
        "matched": True, "game_name": "Kept Game",
        "match_type": "dolphin_disc_sha1", "file_hash": "a" * 40,
    }
    with patch("services.dat_store.dat_store.get_match", return_value=stored), \
         patch("services.dat_store.dat_store.delete_match", new=AsyncMock()) as delete:
        await dat_routes.drop_if_content_changed(path, {
            "error": dat_routes.HASHEOUS_ERROR,
            CANDIDATE_HASHES_KEY: [("a" * 40, "dolphin_disc_sha1")],
        })
    delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_container_hash_still_cannot_prune_an_embedded_chd_hit():
    """The cross-domain refusal the shared helper must keep.

    A CHD hit is stored against ``chd_sha1``; a rescan recomputes the container
    ``file_sha1``. Comparing them deletes valid badges, which is the bug the
    original narrow rule existed to prevent.
    """
    path = "/vol/game.chd"
    stored = {
        "matched": True, "game_name": "Kept Game",
        "match_type": "chd_sha1", "file_hash": "a" * 40,
    }
    with patch("services.dat_store.dat_store.get_match", return_value=stored), \
         patch("services.dat_store.dat_store.delete_match", new=AsyncMock()) as delete:
        await dat_routes.drop_if_content_changed(
            path, {"error": dat_routes.HASHEOUS_ERROR, "file_hash": "f" * 40},
        )
    delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_result_with_no_hashes_never_touches_the_store():
    """No evidence means no comparison, and the check runs per file per rescan."""
    with patch("services.dat_store.dat_store.get_match") as get_match, \
         patch("services.dat_store.dat_store.delete_match", new=AsyncMock()) as delete:
        await dat_routes.drop_if_content_changed(
            "/vol/big.iso", {"reason": "file too large"},
        )
    get_match.assert_not_called()
    delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_capped_file_still_carries_the_hashes_it_did_recompute(
    hasheous_on, monkeypatch,
):
    """A size-capped result is non-cacheable, not evidence-free.

    A large CHD's container SHA1 is never read, but its embedded hashes are --
    and those are exactly what proves a swap to the scan's pruner. The
    local-only exit returned the bare ``reason`` result and threw them away.
    """
    from services.dat_store import CANDIDATE_HASHES_KEY

    header = "a" * 40

    class _Chd:
        embedded_hash_is_exhaustive = False

        async def embedded_hashes(self, path, *, cancel_event=None):
            return [(header, "chd_sha1")]

    monkeypatch.setattr(dat_routes.dat_store, "has_dats", lambda: True)
    monkeypatch.setattr(dat_routes.registry, "tool_for_verify", lambda _p: _Chd())
    monkeypatch.setattr(dat_routes, "_local_dat_record", AsyncMock(return_value=None))
    monkeypatch.setattr(settings, "match_max_file_size", 1)
    monkeypatch.setattr(dat_routes.os.path, "getsize", lambda _p: 10_000_000_000)

    result = await dat_routes._match_single_file("/vol/big.chd", local_only=True)

    assert result["reason"] == "file too large"
    assert result[CANDIDATE_HASHES_KEY] == [(header, "chd_sha1")], (
        "the capped result dropped the embedded hashes it did recompute"
    )


# ---------------------------------------------------------------------------
# Thirty-fourth review round (PR #273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switching_off_during_the_local_recheck_still_stops_the_pass(
    hasheous_on, monkeypatch,
):
    """The recheck added in round 32 is an await, and opened a new window.

    A guard that only preceded it let the operator switch the fallback off
    while the local pass was running and still have the next hash go out --
    one await later, the same gap the recheck itself was added to close.
    """
    header, data = "a" * 40, "b" * 40
    sent = []

    async def _remote(sha1):
        sent.append(sha1)
        return None

    async def _local_recheck(_file_path, _candidates):
        # The operator flips the toggle while this await is in flight.
        monkeypatch.setattr(settings, "hasheous_enabled", False)
        hasheous.set_enabled_override(None)
        return None

    monkeypatch.setattr(dat_routes.hasheous, "lookup", _remote)
    monkeypatch.setattr(dat_routes, "_local_lookup_match", _local_recheck)

    match, consulted = await dat_routes._remote_lookup_match(
        "/vol/game.chd", [(header, "chd_sha1"), (data, "chd_data_sha1")],
    )

    assert sent == [header], "a hash went out after the fallback was switched off"
    assert match is None
    assert consulted is None, "a pass stopped part-way must not be stamped complete"


@pytest.mark.asyncio
async def test_cancelling_during_the_local_recheck_still_stops_the_pass(
    hasheous_on, monkeypatch,
):
    """Cancellation gets the same treatment, for the same reason."""
    header, data = "a" * 40, "b" * 40
    sent = []
    cancel = asyncio.Event()

    async def _remote(sha1):
        sent.append(sha1)
        return None

    async def _local_recheck(_file_path, _candidates):
        cancel.set()
        return None

    monkeypatch.setattr(dat_routes.hasheous, "lookup", _remote)
    monkeypatch.setattr(dat_routes, "_local_lookup_match", _local_recheck)

    match, consulted = await dat_routes._remote_lookup_match(
        "/vol/game.chd",
        [(header, "chd_sha1"), (data, "chd_data_sha1")],
        cancel_event=cancel,
    )

    assert sent == [header]
    assert match is None and consulted is None


@pytest.mark.asyncio
async def test_a_capped_rescan_keeps_a_hit_it_cannot_disprove(
    scan_phase_stubs, monkeypatch,
):
    """A size cap is not evidence that the file changed.

    With Hasheous off, a large CHD carrying a HASH badge produces a
    non-cacheable size-cap result -- and that branch used to delete the row
    outright, throwing away a valid identity for a file nobody had touched
    just because the container was over the cap.
    """
    import routes.dat as dat_internal
    from services.dat_store import CANDIDATE_HASHES_KEY, dat_store as global_dat_store

    monkeypatch.setattr(global_dat_store, "has_dats", lambda: True)
    monkeypatch.setattr(
        global_dat_store, "get_matches_batch", lambda paths: {p: None for p in paths},
    )

    async def _capped(path, *, cancel_event=None):
        return {
            "path": path, "matched": False, "reason": "file too large",
            CANDIDATE_HASHES_KEY: [("a" * 40, "chd_sha1")],
        }

    monkeypatch.setattr(dat_internal, "_match_single_file", _capped)
    deleted = AsyncMock()
    monkeypatch.setattr(global_dat_store, "delete_match", deleted)
    # The row it must not lose: same embedded hash, so nothing is disproved.
    monkeypatch.setattr(
        global_dat_store, "get_match",
        lambda _p: {"matched": True, "game_name": "Kept Game",
                    "match_type": "chd_sha1", "file_hash": "a" * 40},
    )

    await scan_phase_stubs._scan_phase_dat_match("job-1", ["/vol/big.chd"], force=True)

    deleted.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_capped_rescan_still_drops_a_hit_it_can_disprove(
    scan_phase_stubs, monkeypatch,
):
    """...and a capped file whose embedded hash *did* change still loses it."""
    import routes.dat as dat_internal
    from services.dat_store import CANDIDATE_HASHES_KEY, dat_store as global_dat_store

    monkeypatch.setattr(global_dat_store, "has_dats", lambda: True)
    monkeypatch.setattr(
        global_dat_store, "get_matches_batch", lambda paths: {p: None for p in paths},
    )

    async def _capped(path, *, cancel_event=None):
        return {
            "path": path, "matched": False, "reason": "file too large",
            CANDIDATE_HASHES_KEY: [("b" * 40, "chd_sha1")],
        }

    monkeypatch.setattr(dat_internal, "_match_single_file", _capped)
    deleted = AsyncMock()
    monkeypatch.setattr(global_dat_store, "delete_match", deleted)
    monkeypatch.setattr(
        global_dat_store, "get_match",
        lambda _p: {"matched": True, "game_name": "Old Game",
                    "match_type": "chd_sha1", "file_hash": "a" * 40},
    )

    await scan_phase_stubs._scan_phase_dat_match("job-1", ["/vol/big.chd"], force=True)

    deleted.assert_awaited_once_with("/vol/big.chd")
