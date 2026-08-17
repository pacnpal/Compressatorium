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

    assert [e.name for e in listing.entries] == ["Real Game"]
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
    assert listing.entries[0].name == "Super Mario Bros."


@pytest.mark.asyncio
async def test_routes_require_configuration() -> None:
    from fastapi import HTTPException

    with patch.object(RommClient, "base_url", ""), \
            pytest.raises(HTTPException) as exc:
        await romm_routes.romm_platforms()
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_status_reports_unreachable_romm_as_data() -> None:
    """The view renders the problem, so an unreachable RomM is not a 500."""
    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", ""), \
            patch.object(
                romm_routes.romm_client, "heartbeat",
                side_effect=RommError("Connection refused"),
            ), \
            patch.object(romm_routes, "_count_pending", return_value=0):
        status = await romm_routes.romm_status()

    assert status["configured"] is True
    assert status["connected"] is False
    assert "Connection refused" in status["error"]
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
    romm_routes._record_repin(rom, "/vol/Game.rvz", {"igdb_id": 42})
    romm_routes._record_repin(rom, "/vol/Game.rvz", {"igdb_id": 42})
    assert romm_routes._count_pending() == 1


def test_record_repin_clears_stale_hash_on_resubmit(repin_db) -> None:
    """A re-run rewrites the output, so a cached hash of the old one is wrong."""
    rom = {"id": 7, "name": "Game"}
    romm_routes._record_repin(rom, "/vol/Game.rvz", {"igdb_id": 42})
    romm_routes._store_sha1("/vol/Game.rvz", "deadbeef")
    assert romm_routes._pending_rows(10)[0][1] == "deadbeef"

    romm_routes._record_repin(rom, "/vol/Game.rvz", {"igdb_id": 42})
    assert romm_routes._pending_rows(10)[0][1] is None


def test_settled_rows_are_not_revisited(repin_db) -> None:
    romm_routes._record_repin({"id": 7}, "/vol/Game.rvz", {"igdb_id": 42})
    romm_routes._settle(7, "/vol/Game.rvz", "done", None, 99)
    assert romm_routes._count_pending() == 0
    assert romm_routes._pending_rows(10) == []


@pytest.mark.asyncio
async def test_repin_leaves_unscanned_rows_pending(repin_db, tmp_path: Path) -> None:
    """RomM not having scanned yet is the normal case, not a failure."""
    output = tmp_path / "Game.rvz"
    output.write_bytes(b"x" * 8)
    romm_routes._record_repin({"id": 7}, str(output), {"igdb_id": 42})

    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(tmp_path)), \
            patch.object(
                romm_routes, "compute_file_sha1", AsyncMock(return_value="abc123"),
            ), \
            patch.object(romm_routes.romm_client, "rom_by_sha1", return_value=None), \
            patch.object(
                romm_routes.romm_client, "update_rom_metadata",
            ) as update:
        result = await romm_routes.romm_repin()

    assert result["waiting"] == 1
    assert result["repinned"] == 0
    assert result["pending"] == 1
    update.assert_not_called()
    # The hash is cached so the next pass does not re-read a multi-GB file.
    assert romm_routes._pending_rows(10)[0][1] == "abc123"


@pytest.mark.asyncio
async def test_repin_applies_metadata_once_romm_has_scanned(
    repin_db, tmp_path: Path,
) -> None:
    output = tmp_path / "Game.rvz"
    output.write_bytes(b"x" * 8)
    romm_routes._record_repin({"id": 7}, str(output), {"igdb_id": 42, "ra_id": 5})

    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(tmp_path)), \
            patch.object(
                romm_routes, "compute_file_sha1", AsyncMock(return_value="abc123"),
            ), \
            patch.object(
                romm_routes.romm_client, "rom_by_sha1", return_value={"id": 108},
            ), \
            patch.object(romm_routes.romm_client, "update_rom_metadata") as update:
        result = await romm_routes.romm_repin()

    assert result["repinned"] == 1
    assert result["pending"] == 0
    update.assert_called_once_with(108, {"igdb_id": 42, "ra_id": 5})

    # Running again is free: the row is settled, so nothing is re-applied.
    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(tmp_path)), \
            patch.object(romm_routes.romm_client, "update_rom_metadata") as update2:
        again = await romm_routes.romm_repin()
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
    assert romm_routes._count_pending() == 0


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
