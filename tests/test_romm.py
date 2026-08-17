"""Tests for the RomM catalog overlay (client, routes, and the re-pin queue)."""

# ruff: noqa: S101

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import these UNPREFIXED. Application code imports its own modules as
# ``from services.x import y``, so ``app.services.romm`` and ``services.romm``
# are two distinct module objects holding two distinct ``RommClient`` classes.
# Patching the ``app.``-prefixed one would land on a class the routes never use,
# and every patch would silently no-op.
from routes import romm as romm_routes
from services import db as _db
from services import romm as romm_service
from services import romm_repin
from services.romm import DAT_SAFE_OUTPUT_EXTS, RommClient, RommError

# ----------------------------------------------------------------------
# client: transport
# ----------------------------------------------------------------------


def _response(payload: object) -> MagicMock:
    """A urlopen context manager yielding *payload* as JSON."""
    body = json.dumps(payload).encode()
    resp = MagicMock()
    resp.read.side_effect = lambda *a: body
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


@pytest.fixture(name="client")
def _client() -> RommClient:
    return RommClient(base_url="http://romm:8080", token="rmm_test")


def test_bearer_token_is_sent(client: RommClient) -> None:
    with patch("urllib.request.urlopen", return_value=_response([])) as urlopen:
        client.platforms()
    request = urlopen.call_args[0][0]
    assert request.get_header("Authorization") == "Bearer rmm_test"


def test_heartbeat_is_unauthenticated(client: RommClient) -> None:
    """The heartbeat probe must work before a token is configured.

    It is what separates "cannot reach RomM" from "token rejected", so sending
    credentials it does not need would defeat the diagnostic.
    """
    with patch(
        "urllib.request.urlopen", return_value=_response({"VERSION": "4.9.0"}),
    ) as urlopen:
        assert client.heartbeat() == {"VERSION": "4.9.0"}
    assert urlopen.call_args[0][0].get_header("Authorization") is None


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://host/x", "gopher://x"])
def test_non_http_scheme_is_refused(url: str) -> None:
    """A crafted ROMM_URL must not turn the client into a local-file reader."""
    client = RommClient(base_url=url, token=None)
    with pytest.raises(RommError, match="Only http/https"):
        client.platforms()


def test_plain_http_is_allowed(client: RommClient) -> None:
    """RomM is normally reached over http on a container network."""
    with patch("urllib.request.urlopen", return_value=_response([])):
        assert client.platforms() == []


def test_http_error_becomes_romm_error(client: RommClient) -> None:
    err = urllib.error.HTTPError(
        "http://romm:8080/api/platforms", 401, "Unauthorized", {},
        io.BytesIO(b"bad token"),
    )
    with patch("urllib.request.urlopen", side_effect=err), \
            pytest.raises(RommError, match="HTTP 401"):
        client.platforms()


def test_rom_by_sha1_treats_404_as_no_match(client: RommClient) -> None:
    """404 means "RomM has not scanned it yet", which is a normal state."""
    err = urllib.error.HTTPError(
        "http://romm:8080/api/roms/by-hash", 404, "Not Found", {}, io.BytesIO(b""),
    )
    with patch("urllib.request.urlopen", side_effect=err):
        assert client.rom_by_sha1("abc") is None


def test_roms_pages_until_short_page(client: RommClient) -> None:
    pages = [
        {"items": [{"id": i} for i in range(romm_service.PAGE_SIZE)]},
        {"items": [{"id": 9001}]},
    ]
    with patch.object(client, "_request", side_effect=pages) as req:
        result = client.roms(3)
    assert len(result) == romm_service.PAGE_SIZE + 1
    assert req.call_count == 2
    # The UI-only index payloads must be switched off for an API client.
    params = req.call_args_list[0].kwargs["params"]
    assert params["with_char_index"] == "false"
    assert params["with_rom_id_index"] == "false"
    assert params["with_filter_values"] == "false"
    assert params["with_files"] == "true"


# ----------------------------------------------------------------------
# client: path mapping (trust boundary)
# ----------------------------------------------------------------------


def test_local_path_joins_full_path(tmp_path: Path) -> None:
    client = RommClient(base_url="http://romm:8080")
    with patch.object(RommClient, "library_root", str(tmp_path)):
        got = client.local_path({"full_path": "roms/snes/Game.sfc"})
    assert got == str(tmp_path / "roms" / "snes" / "Game.sfc")


def test_local_path_falls_back_to_fs_path_and_name(tmp_path: Path) -> None:
    client = RommClient(base_url="http://romm:8080")
    with patch.object(RommClient, "library_root", str(tmp_path)):
        got = client.local_path({"fs_path": "roms/gc", "fs_name": "Game.iso"})
    assert got == str(tmp_path / "roms" / "gc" / "Game.iso")


@pytest.mark.parametrize(
    "rel",
    [
        "../../etc/passwd",
        "roms/../../../etc/passwd",
        "/etc/passwd",
        "roms/snes/../../../../etc/shadow",
    ],
)
def test_local_path_rejects_library_escape(tmp_path: Path, rel: str) -> None:
    """``full_path`` comes from a remote service and becomes a filesystem op.

    Anything resolving outside the library root must be dropped, not resolved.
    """
    client = RommClient(base_url="http://romm:8080")
    with patch.object(RommClient, "library_root", str(tmp_path / "library")):
        assert client.local_path({"full_path": rel}) is None


def test_local_path_without_library_root_is_none() -> None:
    client = RommClient(base_url="http://romm:8080")
    with patch.object(RommClient, "library_root", ""):
        assert client.local_path({"full_path": "roms/snes/Game.sfc"}) is None


# ----------------------------------------------------------------------
# routes: listing
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_roms_route_drops_unresolvable_records(tmp_path: Path) -> None:
    """A record with no file on this host must not become a convertible row."""
    real = tmp_path / "Game.iso"
    real.write_bytes(b"x" * 16)

    roms = [
        {"id": 1, "name": "Real Game", "full_path": "Game.iso"},
        {"id": 2, "name": "Missing Game", "full_path": "Gone.iso"},
        {"id": 3, "name": "Escaping", "full_path": "../../etc/passwd"},
    ]

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(RommClient, "library_root", str(tmp_path)), \
            patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(romm_routes, "is_within_configured_volumes", return_value=True):
        listing = await romm_routes.romm_roms(platform_id=1)

    # The filename is the contract every reused row action depends on; the
    # curated title rides along separately.
    assert [e.name for e in listing.entries] == ["Game.iso"]
    assert [e.display_name for e in listing.entries] == ["Real Game"]
    assert listing.entries[0].path == str(real)
    assert listing.entries[0].size == 16


@pytest.mark.asyncio
async def test_roms_route_uses_romm_name_over_filename(tmp_path: Path) -> None:
    """The curated name is the whole point of the overlay."""
    (tmp_path / "smb_u_rev1.sfc").write_bytes(b"x")
    with patch.object(
        romm_routes.romm_client, "roms",
        return_value=[{"id": 1, "name": "Super Mario Bros.", "full_path": "smb_u_rev1.sfc"}],
    ), patch.object(RommClient, "library_root", str(tmp_path)), \
            patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(romm_routes, "is_within_configured_volumes", return_value=True):
        listing = await romm_routes.romm_roms(platform_id=1)
    # `name` must stay the real filename: Rename pre-fills from it, so seeding
    # it with the title would rename the file without its extension.
    assert listing.entries[0].name == "smb_u_rev1.sfc"
    assert listing.entries[0].display_name == "Super Mario Bros."


@pytest.mark.asyncio
async def test_routes_require_configuration() -> None:
    from fastapi import HTTPException

    with patch.object(RommClient, "base_url", ""), \
            pytest.raises(HTTPException) as exc:
        await romm_routes.romm_platforms()
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_status_does_not_leak_romm_response_body() -> None:
    """RomM's response body must not be echoed into our API response.

    The client puts it in the exception message for the log, but that text is
    remote-controlled; the response carries a message derived from the status
    code instead. (CodeQL: information exposure through an exception.)
    """
    exc = RommError(
        "RomM GET /api/heartbeat failed: HTTP 401 — "
        "Traceback: /srv/romm/backend/auth.py line 42, secret=hunter2",
        status=401,
    )
    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", ""), \
            patch.object(romm_routes.romm_client, "heartbeat", side_effect=exc), \
            patch.object(romm_repin, "count_pending", return_value=0):
        status = await romm_routes.romm_status()

    assert "hunter2" not in status["error"]
    assert "Traceback" not in status["error"]
    assert "/srv/romm" not in status["error"]
    # Still actionable: it names what to go fix.
    assert "ROMM_TOKEN" in status["error"]


@pytest.mark.asyncio
async def test_route_error_does_not_leak_romm_response_body() -> None:
    """Same guarantee on the raising routes, not just the status endpoint."""
    from fastapi import HTTPException

    exc = RommError("RomM GET /api/platforms failed: HTTP 500 — secret=hunter2",
                    status=500)
    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", "/library"), \
            patch.object(romm_routes.romm_client, "platforms", side_effect=exc), \
            pytest.raises(HTTPException) as caught:
        await romm_routes.romm_platforms()

    assert caught.value.status_code == 502
    assert "hunter2" not in caught.value.detail
    assert "500" in caught.value.detail


@pytest.mark.asyncio
async def test_status_reports_unreachable_romm_as_data() -> None:
    """The view renders the problem, so an unreachable RomM is not a 500."""
    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", ""), \
            patch.object(
                romm_routes.romm_client, "heartbeat",
                side_effect=RommError("Connection refused"),
            ), \
            patch.object(romm_repin, "count_pending", return_value=0):
        status = await romm_routes.romm_status()

    assert status["configured"] is True
    assert status["connected"] is False
    # No HTTP status on the error means we never reached RomM at all.
    assert "Could not reach RomM" in status["error"]
    # SSOT: the frontend renders its warning from this, not its own copy.
    assert status["dat_safe_output_exts"] == sorted(DAT_SAFE_OUTPUT_EXTS)


# ----------------------------------------------------------------------
# re-pin queue
# ----------------------------------------------------------------------


@pytest.fixture(name="repin_db")
def _repin_db(tmp_path: Path):
    """A real SQLite DB wired into the module-level session factory."""
    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    yield
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


def test_record_repin_is_idempotent_per_output(repin_db) -> None:
    """Re-submitting the same batch must not stack duplicate rows."""
    rom = {"id": 7, "name": "Game", "fs_name": "Game.iso"}
    romm_repin.record(rom, "/vol/Game.rvz", {"igdb_id": 42})
    romm_repin.record(rom, "/vol/Game.rvz", {"igdb_id": 42})
    assert romm_repin.count_pending() == 1


def test_record_repin_clears_stale_hash_on_resubmit(repin_db) -> None:
    """A re-run rewrites the output, so a cached hash of the old one is wrong."""
    rom = {"id": 7, "name": "Game"}
    romm_repin.record(rom, "/vol/Game.rvz", {"igdb_id": 42})
    romm_repin.store_sha1("/vol/Game.rvz", "deadbeef")
    assert romm_repin.pending_rows(10)[0][1] == "deadbeef"

    romm_repin.record(rom, "/vol/Game.rvz", {"igdb_id": 42})
    assert romm_repin.pending_rows(10)[0][1] is None


def test_settled_rows_are_not_revisited(repin_db) -> None:
    romm_repin.record({"id": 7}, "/vol/Game.rvz", {"igdb_id": 42})
    romm_repin.settle(7, "/vol/Game.rvz", "done", None, 99)
    assert romm_repin.count_pending() == 0
    assert romm_repin.pending_rows(10) == []


@pytest.mark.asyncio
async def test_repin_leaves_unscanned_rows_pending(repin_db, tmp_path: Path) -> None:
    """RomM not having scanned yet is the normal case, not a failure."""
    output = tmp_path / "Game.rvz"
    output.write_bytes(b"x" * 8)
    romm_repin.record({"id": 7}, str(output), {"igdb_id": 42})

    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(tmp_path)), \
            patch.object(
                romm_routes, "run_detached", AsyncMock(return_value="abc123"),
            ), \
            patch.object(romm_routes.romm_client, "rom_by_sha1", return_value=None), \
            patch.object(
                romm_routes.romm_client, "update_rom_metadata",
            ) as update:
        result = await romm_routes.settle_romm_repins()

    assert result["waiting"] == 1
    assert result["repinned"] == 0
    assert result["pending"] == 1
    update.assert_not_called()
    # The hash is cached so the next pass does not re-read a multi-GB file.
    assert romm_repin.pending_rows(10)[0][1] == "abc123"


@pytest.mark.asyncio
async def test_repin_applies_metadata_once_romm_has_scanned(
    repin_db, tmp_path: Path,
) -> None:
    output = tmp_path / "Game.rvz"
    output.write_bytes(b"x" * 8)
    romm_repin.record({"id": 7}, str(output), {"igdb_id": 42, "ra_id": 5})

    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(tmp_path)), \
            patch.object(
                romm_routes, "run_detached", AsyncMock(return_value="abc123"),
            ), \
            patch.object(
                romm_routes.romm_client, "rom_by_sha1", return_value={"id": 108},
            ), \
            patch.object(romm_routes.romm_client, "update_rom_metadata") as update:
        result = await romm_routes.settle_romm_repins()

    assert result["repinned"] == 1
    assert result["pending"] == 0
    update.assert_called_once_with(108, {"igdb_id": 42, "ra_id": 5})

    # Running again is free: the row is settled, so nothing is re-applied.
    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(tmp_path)), \
            patch.object(romm_routes.romm_client, "update_rom_metadata") as update2:
        again = await romm_routes.settle_romm_repins()
    assert again["repinned"] == 0
    update2.assert_not_called()


@pytest.mark.asyncio
async def test_repin_plan_skips_dat_safe_modes(repin_db) -> None:
    """CHD keeps its own DAT identity, so recording a row would be noise."""
    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", "/library"):
        result = await romm_routes.romm_repin_plan(
            romm_routes.RepinPlanRequest(paths=["/vol/Game.iso"], mode="createdvd"),
        )
    assert result["recorded"] == 0
    assert result["reason"] == "dat_safe"
    assert romm_repin.count_pending() == 0


def test_update_rom_metadata_sends_only_set_provider_ids(client: RommClient) -> None:
    """PUT /api/roms/{id} is multipart, and must carry only real ids.

    Sending a provider the source had no id for would overwrite whatever RomM
    worked out for the converted file with an empty value.
    """
    with patch("urllib.request.urlopen", return_value=_response({"id": 500})) as urlopen:
        client.update_rom_metadata(500, {"igdb_id": 1122, "ra_id": 44, "moby_id": None})
    request = urlopen.call_args[0][0]
    body = request.data.decode()
    assert request.get_full_url().endswith("/api/roms/500")
    assert request.get_method() == "PUT"
    assert request.get_header("Content-type", "").startswith("multipart/form-data; boundary=")
    assert "1122" in body and "44" in body
    assert "moby_id" not in body


def test_update_rom_metadata_skips_the_request_when_nothing_to_send(
    client: RommClient,
) -> None:
    with patch("urllib.request.urlopen") as urlopen:
        client.update_rom_metadata(500, {"moby_id": None})
    urlopen.assert_not_called()


def test_update_rom_metadata_ignores_unknown_fields(client: RommClient) -> None:
    """Only the known provider ids are forwarded, never arbitrary keys."""
    with patch("urllib.request.urlopen", return_value=_response({})) as urlopen:
        client.update_rom_metadata(1, {"igdb_id": 5, "fs_name": "evil.iso"})
    body = urlopen.call_args[0][0].data.decode()
    assert "igdb_id" in body
    assert "fs_name" not in body


def test_dat_safe_set_matches_romm_lookup_hashes() -> None:
    """RomM matches CHDs on the header SHA-1 and archives on the largest member.

    Every other output we produce is matched on the container hash, which
    conversion changes — so this set is exactly the formats that need no re-pin.
    """
    assert DAT_SAFE_OUTPUT_EXTS == {".chd", ".zip", ".7z"}


# ----------------------------------------------------------------------
# platform narrowing (the feature the whole overlay exists for)
# ----------------------------------------------------------------------


def test_platform_narrows_ambiguous_iso() -> None:
    """A bare .iso is the case extensions cannot resolve.

    chdman, dolphin and maxcso all accept `.iso`; only the platform says
    whether it is a GameCube disc (RVZ) or a PS2 disc (CHD/CSO).
    """
    from services.tools import registry as reg

    candidates = ["chdman", "dolphin", "cso", "nkit"]
    gamecube = reg.narrow_to_platform(candidates, "ngc")
    ps2 = reg.narrow_to_platform(candidates, "ps2")

    assert "dolphin" in gamecube and "nkit" in gamecube
    assert "chdman" not in gamecube and "cso" not in gamecube
    assert "chdman" in ps2 and "cso" in ps2
    assert "dolphin" not in ps2


def test_unknown_platform_never_narrows_to_nothing() -> None:
    """An unrecognised RomM slug must degrade to extension-only behaviour.

    Wrongly excluding every tool would make the row unconvertible, which is far
    worse than showing one option too many.
    """
    from services.tools import registry as reg

    candidates = ["chdman", "dolphin", "cso"]
    assert reg.narrow_to_platform(candidates, "some-new-console-2031") == candidates
    assert reg.narrow_to_platform(candidates, None) == candidates
    assert reg.narrow_to_platform(candidates, "") == candidates


def test_tool_without_platform_opinion_is_never_dropped() -> None:
    from services.tools import registry as reg

    # romz declares cartridge platforms, so it is droppable; a tool declaring
    # nothing must survive every slug.
    for tool in reg.all():
        if not tool.platform_slugs:
            assert tool.id in reg.narrow_to_platform([tool.id], "ps2")


# ----------------------------------------------------------------------
# runtime settings
# ----------------------------------------------------------------------


@pytest.fixture(name="settings_db")
def _settings_db(tmp_path: Path):
    from services import romm_settings

    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    romm_settings.reset_for_tests()
    yield romm_settings
    romm_settings.reset_for_tests()
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


@pytest.mark.asyncio
async def test_saved_settings_override_env(settings_db, monkeypatch) -> None:
    monkeypatch.setenv("ROMM_URL", "http://from-env:8080")
    await settings_db.load(force=True)
    assert settings_db.effective()["url"] == "http://from-env:8080"

    await settings_db.save({"url": "http://from-app:8080"})
    assert settings_db.effective()["url"] == "http://from-app:8080"


@pytest.mark.asyncio
async def test_token_is_never_returned_by_the_api(settings_db) -> None:
    await settings_db.save({"token": "rmm_" + "b" * 64})
    public = settings_db.public()
    assert "token" not in public
    assert public["token_set"] is True
    assert settings_db.token() == "rmm_" + "b" * 64


@pytest.mark.asyncio
async def test_empty_token_leaves_the_stored_one_alone(settings_db) -> None:
    """The form never shows the secret, so submitting it must not blank it."""
    await settings_db.save({"token": "rmm_" + "c" * 64})
    await settings_db.save({"url": "http://romm:8080", "token": ""})
    assert settings_db.token() == "rmm_" + "c" * 64

    await settings_db.save({"clear_token": True})
    assert settings_db.token() is None


@pytest.mark.asyncio
async def test_partial_save_does_not_clobber_other_fields(settings_db) -> None:
    await settings_db.save({"url": "http://romm:8080", "auto_convert": True})
    await settings_db.save({"auto_convert_max_per_run": 5})
    values = settings_db.effective()
    assert values["url"] == "http://romm:8080"
    assert values["auto_convert"] is True
    assert values["auto_convert_max_per_run"] == 5


@pytest.mark.asyncio
async def test_numeric_settings_are_bounded(settings_db) -> None:
    await settings_db.save({"auto_convert_interval_minutes": 1})
    assert settings_db.effective()["auto_convert_interval_minutes"] == 5
    await settings_db.save({"auto_convert_max_per_run": 99999})
    assert settings_db.effective()["auto_convert_max_per_run"] == 1000

# ----------------------------------------------------------------------
# per-platform automation rules
# ----------------------------------------------------------------------


def test_rule_normalizes_to_full_schema() -> None:
    from services import romm_auto

    rule = romm_auto.normalize_rule({"mode": "dolphin_rvz", "enabled": True})
    assert rule is not None
    # Every field the engine reads is present, so a sweep never KeyErrors on a
    # rule written by an older version of the UI.
    for field in romm_auto.default_rule():
        assert field in rule


def test_rule_with_unknown_mode_is_dropped() -> None:
    from services import romm_auto

    assert romm_auto.normalize_rule({"mode": "not_a_real_mode"}) is None
    assert romm_auto.normalize_rules({"7": {"mode": "nope"}}) == {}


def test_rule_numeric_fields_are_bounded() -> None:
    from services import romm_auto

    rule = romm_auto.normalize_rule({
        "mode": "dolphin_rvz", "interval_minutes": 1, "max_per_run": 99999,
    })
    assert rule["interval_minutes"] == 5
    assert rule["max_per_run"] == 1000


def test_invalid_filter_pattern_is_ignored_not_fatal() -> None:
    from services import romm_auto

    rule = romm_auto.normalize_rule({"mode": "dolphin_rvz", "include_pattern": "([a"})
    assert rule["include_pattern"] is None


def test_compression_only_kept_where_the_mode_supports_it() -> None:
    from services import romm_auto

    # dolphin_gcz takes no compression setting; storing one would be dropped
    # at submit time anyway, so it must not be persisted as if it applied.
    rule = romm_auto.normalize_rule({"mode": "dolphin_gcz", "compression": "zstd"})
    assert rule["compression"] is None


def test_contradictory_match_filters_cancel_out() -> None:
    from services import romm_auto

    rule = romm_auto.normalize_rule({
        "mode": "dolphin_rvz", "only_matched": True, "only_unmatched": True,
    })
    assert rule["only_matched"] is False
    assert rule["only_unmatched"] is False


def test_overnight_window_wraps_midnight() -> None:
    from datetime import datetime, timezone
    from services import romm_auto

    rule = romm_auto.default_rule("dolphin_rvz")
    rule["window_start"], rule["window_end"] = "22:00", "04:00"
    inside = datetime(2026, 8, 17, 23, 30, tzinfo=timezone.utc)   # Monday
    outside = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    early = datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc)
    assert romm_auto._in_window(rule, inside)
    assert romm_auto._in_window(rule, early)
    assert not romm_auto._in_window(rule, outside)


def test_day_mask_excludes_other_days() -> None:
    from datetime import datetime, timezone
    from services import romm_auto

    rule = romm_auto.default_rule("dolphin_rvz")
    rule["days"] = [5, 6]  # weekends only
    monday = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    saturday = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    assert not romm_auto._in_window(rule, monday)
    assert romm_auto._in_window(rule, saturday)


def test_disabled_rule_is_never_due() -> None:
    from datetime import datetime, timezone
    from services import romm_auto

    rule = romm_auto.default_rule("dolphin_rvz")
    rule["enabled"] = False
    assert not romm_auto._is_due(rule, {}, datetime.now(timezone.utc))


def test_interval_gates_a_second_run() -> None:
    from datetime import datetime, timedelta, timezone
    from services import romm_auto

    rule = romm_auto.default_rule("dolphin_rvz")
    rule["enabled"] = True
    rule["interval_minutes"] = 60
    now = datetime.now(timezone.utc)
    just_ran = {"last_run_at": (now - timedelta(minutes=5)).isoformat()}
    long_ago = {"last_run_at": (now - timedelta(hours=3)).isoformat()}
    assert not romm_auto._is_due(rule, just_ran, now)
    assert romm_auto._is_due(rule, long_ago, now)
    assert romm_auto._is_due(rule, {}, now)   # never run


def test_size_and_name_filters() -> None:
    from services import romm_auto

    rule = romm_auto.default_rule("dolphin_rvz")
    rule["min_size_mb"] = 100
    big = {"fs_name": "Big Game (USA).iso", "fs_size_bytes": 4_400_000_000}
    small = {"fs_name": "Tiny.iso", "fs_size_bytes": 1024}
    assert romm_auto._passes_filters(big, rule)
    assert not romm_auto._passes_filters(small, rule)

    rule = romm_auto.default_rule("dolphin_rvz")
    rule["exclude_pattern"] = r"\(Japan\)"
    assert not romm_auto._passes_filters({"fs_name": "Game (Japan).iso"}, rule)
    assert romm_auto._passes_filters({"fs_name": "Game (USA).iso"}, rule)


def test_rom_ordering_is_deterministic() -> None:
    from services import romm_auto

    roms = [
        {"id": 3, "name": "Charlie", "fs_size_bytes": 10},
        {"id": 1, "name": "alpha", "fs_size_bytes": 300},
        {"id": 2, "name": "Bravo", "fs_size_bytes": 200},
    ]
    rule = romm_auto.default_rule("dolphin_rvz")
    by_name = [r["id"] for r in sorted(roms, key=romm_auto._rom_sort_key(rule))]
    assert by_name == [1, 2, 3]

    rule["order"] = "size_desc"
    by_size = [r["id"] for r in sorted(roms, key=romm_auto._rom_sort_key(rule))]
    assert by_size == [1, 2, 3]


@pytest.mark.asyncio
async def test_sweep_is_idempotent_against_the_filesystem(
    settings_db, tmp_path: Path,
) -> None:
    """The second sweep must queue nothing once the output exists.

    This is the property the whole engine rests on: state lives on disk, not in
    a table, so a sweep that runs twice — or after a restart, or racing a
    manual conversion — converges instead of duplicating work.
    """
    from services import romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    await romm_auto.set_rules({
        "7": {"mode": "dolphin_rvz", "enabled": True, "max_per_run": 10},
    })

    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "fs_size_bytes": 32, "platform_slug": "ngc",
    }]
    queued: list[list[str]] = []

    async def _fake_batch(paths, mode, **kwargs):
        queued.append(list(paths))
        return [object() for _ in paths]

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(romm_auto.job_manager, "create_batch_jobs", _fake_batch), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        first = await romm_auto.sweep(ignore_schedule=True)
        assert first["queued"] == 1, first
        assert queued == [[str(lib / "Game.iso")]]

        # The conversion lands: the output now sits beside the source, which is
        # the only thing the next sweep consults.
        (lib / "Game.rvz").write_bytes(b"\0" * 16)
        second = await romm_auto.sweep(ignore_schedule=True)

    assert second["queued"] == 0, second
    assert second["skipped_existing"] == 1
    assert len(queued) == 1, "the second sweep must not enqueue anything"


@pytest.mark.asyncio
async def test_sweep_preview_queues_nothing_and_does_not_move_the_clock(
    settings_db, tmp_path: Path,
) -> None:
    """Looking at what *would* run must not postpone the run that should."""
    from services import romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    await romm_auto.set_rules({"7": {"mode": "dolphin_rvz", "enabled": True}})

    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc",
    }]
    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(
                romm_auto.job_manager, "create_batch_jobs",
            ) as create, \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        preview = await romm_auto.sweep(ignore_schedule=True, dry_run=True)

    create.assert_not_called()
    assert preview["queued"] == 1
    assert preview["dry_run"] is True
    # No run recorded, so the real sweep is still due.
    assert await romm_auto.get_state() == {}


@pytest.mark.asyncio
async def test_sweep_respects_the_overall_cap(settings_db, tmp_path: Path) -> None:
    from services import romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    roms = []
    for i in range(10):
        (lib / f"Game{i}.iso").write_bytes(b"\0" * 8)
        roms.append({
            "id": i, "name": f"Game{i}", "full_path": f"roms/gc/Game{i}.iso",
            "fs_name": f"Game{i}.iso", "platform_slug": "ngc",
        })
    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    await romm_auto.set_rules({"7": {"mode": "dolphin_rvz", "enabled": True}})

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        preview = await romm_auto.sweep(
            ignore_schedule=True, dry_run=True, overall_limit=3,
        )
    assert preview["queued"] == 3


@pytest.mark.asyncio
async def test_sweep_skips_sources_already_being_converted(
    settings_db, tmp_path: Path,
) -> None:
    """A job already working on a source must not get a second one queued."""
    from services import romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    source = lib / "Game.iso"
    source.write_bytes(b"\0" * 32)
    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    await romm_auto.set_rules({"7": {"mode": "dolphin_rvz", "enabled": True}})

    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc",
    }]
    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates",
                return_value=[("job-1", [str(source)])],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        result = await romm_auto.sweep(ignore_schedule=True, dry_run=True)

    assert result["queued"] == 0
    assert result["skipped_active"] == 1


# ----------------------------------------------------------------------
# review findings: each test fails on the bug, not merely on a crash
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_with_output_dir_is_idempotent(settings_db, tmp_path: Path) -> None:
    """A rule writing elsewhere must still see its own output.

    `detect_output()` only looks beside the source, so an `output_dir` rule used
    to re-queue the same ROM on every sweep and fail each job on a collision.
    """
    from services import romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    out = tmp_path / "library" / "converted"
    out.mkdir()

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    await romm_auto.set_rules({"7": {
        "mode": "dolphin_rvz", "enabled": True, "output_dir": str(out),
    }})
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc",
    }]

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        first = await romm_auto.sweep(ignore_schedule=True, dry_run=True)
        assert first["queued"] == 1, first

        # The conversion lands in the configured output directory, not beside
        # the source -- which is exactly what the old check could not see.
        (out / "Game.rvz").write_bytes(b"\0" * 16)
        second = await romm_auto.sweep(ignore_schedule=True, dry_run=True)

    assert second["queued"] == 0, second
    assert second["skipped_existing"] == 1


@pytest.mark.asyncio
async def test_sweep_records_repin_rows_for_unsafe_formats(
    settings_db, tmp_path: Path,
) -> None:
    """Automatic RVZ conversion must preserve metadata like the manual path."""
    from services import romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    await romm_auto.set_rules({"7": {"mode": "dolphin_rvz", "enabled": True}})

    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc", "igdb_id": 1122,
    }]

    async def _fake_batch(paths, mode, **kwargs):
        return [object() for _ in paths]

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(romm_auto.job_manager, "create_batch_jobs", _fake_batch), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        result = await romm_auto.sweep(ignore_schedule=True)

    assert result["repins_recorded"] == 1, result
    assert romm_repin.count_pending() == 1
    # Recorded against the *output*, which is what RomM will scan.
    assert romm_repin.pending_rows(10)[0][0].endswith("Game.rvz")


@pytest.mark.asyncio
async def test_sweep_records_nothing_for_dat_safe_formats(
    settings_db, tmp_path: Path,
) -> None:
    """CHD keeps its own DAT identity, so a row would be pure noise."""
    from services import romm_auto

    lib = tmp_path / "library" / "roms" / "ps2"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    await romm_auto.set_rules({"9": {"mode": "createdvd", "enabled": True}})

    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/ps2/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ps2", "igdb_id": 42,
    }]

    async def _fake_batch(paths, mode, **kwargs):
        return [object() for _ in paths]

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(romm_auto.job_manager, "create_batch_jobs", _fake_batch), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        result = await romm_auto.sweep(ignore_schedule=True)

    assert result["queued"] == 1
    assert result["repins_recorded"] == 0
    assert romm_repin.count_pending() == 0


@pytest.mark.asyncio
async def test_sweep_supplies_delete_snapshots(settings_db, tmp_path: Path) -> None:
    """delete_on_verify without a snapshot fails every job after converting."""
    from services import romm_auto

    lib = tmp_path / "library" / "roms" / "ps2"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    await romm_auto.set_rules({"9": {
        "mode": "createdvd", "enabled": True, "delete_on_verify": True,
    }})
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/ps2/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ps2",
    }]
    captured: dict = {}

    async def _fake_batch(paths, mode, **kwargs):
        captured.update(kwargs)
        return [object() for _ in paths]

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(romm_auto.job_manager, "create_batch_jobs", _fake_batch), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        await romm_auto.sweep(ignore_schedule=True)

    assert captured["delete_on_verify"] is True
    assert captured["delete_snapshots"], "job_manager refuses to delete without one"
    assert str(lib / "Game.iso") in captured["delete_snapshots"]


@pytest.mark.asyncio
async def test_disabled_rule_is_paused_even_for_run_now(
    settings_db, tmp_path: Path,
) -> None:
    """Run now must not convert a platform the operator explicitly paused."""
    from services import romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    await romm_auto.set_rules({"7": {"mode": "dolphin_rvz", "enabled": False}})
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc",
    }]

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        # The global button: paused stays paused.
        globally = await romm_auto.sweep(ignore_schedule=True, dry_run=True)
        # Naming the platform is a deliberate per-platform action, so it runs.
        targeted = await romm_auto.sweep(
            platform_ids=[7], ignore_schedule=True, dry_run=True,
        )

    assert globally["queued"] == 0, globally
    assert targeted["queued"] == 1, targeted


@pytest.mark.asyncio
async def test_library_root_outside_volumes_is_not_usable(tmp_path: Path) -> None:
    """An existing folder outside every volume must not look healthy.

    Every ROM path is gated by `is_within_configured_volumes`, so reporting the
    root as mounted yields a connection that looks fine and a catalog in which
    every row is silently dropped.
    """
    outside = tmp_path / "not-a-volume"
    outside.mkdir()

    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(outside)), \
            patch.object(
                romm_routes.romm_client, "heartbeat", return_value={"VERSION": "4.9.0"},
            ), \
            patch.object(romm_repin, "count_pending", return_value=0), \
            patch.object(
                romm_routes, "is_within_configured_volumes", return_value=False,
            ):
        status = await romm_routes.romm_status()

    assert status["library_root_mounted"] is False
    assert "outside the configured" in status["error"]


def test_schedule_window_uses_the_rules_timezone() -> None:
    """A 22:00-04:00 window means the operator's evening, not UTC's."""
    from datetime import datetime, timezone as dt_timezone
    from services import romm_auto

    rule = romm_auto.default_rule("dolphin_rvz")
    rule["window_start"], rule["window_end"] = "22:00", "04:00"
    rule["timezone"] = "America/New_York"

    # 03:00 UTC Tuesday is 23:00 Monday in New York -> inside the window.
    assert romm_auto._in_window(
        rule, datetime(2026, 8, 18, 3, 0, tzinfo=dt_timezone.utc),
    )
    # 22:00 UTC is 18:00 in New York -> outside it, though it would have
    # matched when the window was compared against UTC.
    assert not romm_auto._in_window(
        rule, datetime(2026, 8, 17, 22, 0, tzinfo=dt_timezone.utc),
    )


def test_unknown_timezone_falls_back_to_utc() -> None:
    """A zone the container has no data for must not break the schedule."""
    from services import romm_auto

    rule = romm_auto.normalize_rule({
        "mode": "dolphin_rvz", "timezone": "Mars/Olympus_Mons",
    })
    assert rule["timezone"] == "UTC"
