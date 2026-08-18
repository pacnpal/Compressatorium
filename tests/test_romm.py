"""Tests for the RomM catalog overlay (client, routes, and the re-pin queue)."""

# ruff: noqa: S101

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import these UNPREFIXED. Application code imports its own modules as
# ``from services.x import y``, so ``app.services.romm`` and ``services.romm``
# are two distinct module objects holding two distinct ``RommClient`` classes.
# Patching the ``app.``-prefixed one would land on a class the routes never use,
# and every patch would silently no-op.
from models import JobStatus
from routes import romm as romm_routes
from services import db as _db
from services.romm import client as romm_service
from services.romm import repin as romm_repin
from services.tools import registry
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
    with patch("services.romm.client._urlopen", return_value=_response([])) as urlopen:
        client.platforms()
    request = urlopen.call_args[0][0]
    assert request.get_header("Authorization") == "Bearer rmm_test"


def test_heartbeat_is_unauthenticated(client: RommClient) -> None:
    """The heartbeat probe must work before a token is configured.

    It is what separates "cannot reach RomM" from "token rejected", so sending
    credentials it does not need would defeat the diagnostic.
    """
    with patch(
        "services.romm.client._urlopen", return_value=_response({"VERSION": "4.9.0"}),
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
    with patch("services.romm.client._urlopen", return_value=_response([])):
        assert client.platforms() == []


def test_http_error_becomes_romm_error(client: RommClient) -> None:
    err = urllib.error.HTTPError(
        "http://romm:8080/api/platforms", 401, "Unauthorized", {},
        io.BytesIO(b"bad token"),
    )
    with patch("services.romm.client._urlopen", side_effect=err), \
            pytest.raises(RommError, match="HTTP 401"):
        client.platforms()


def test_rom_by_sha1_treats_404_as_no_match(client: RommClient) -> None:
    """404 means "RomM has not scanned it yet", which is a normal state."""
    err = urllib.error.HTTPError(
        "http://romm:8080/api/roms/by-hash", 404, "Not Found", {}, io.BytesIO(b""),
    )
    with patch("services.romm.client._urlopen", side_effect=err):
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


@pytest.fixture(name="sqlite_db")
def _sqlite_db(tmp_path: Path):
    """A real SQLite DB wired into the module-level session factory."""
    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    yield
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


@pytest.fixture(name="repin_db")
def _repin_db(sqlite_db):
    """The bare database, for the re-pin queue tests."""
    yield sqlite_db


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
    row_id = romm_repin.pending_rows(10)[0][5]
    romm_repin.store_sha1(row_id, "deadbeef")
    assert romm_repin.pending_rows(10)[0][1] == "deadbeef"

    romm_repin.record(rom, "/vol/Game.rvz", {"igdb_id": 42})
    assert romm_repin.pending_rows(10)[0][1] is None


def test_store_sha1_does_not_touch_a_later_row_for_the_same_output(
    repin_db,
) -> None:
    """The hash belongs to one attempt, not to a path.

    A row can be settled and the same output re-recorded by a later conversion
    while a settle pass is mid-flight. Keying on the path would stamp the new
    row with the old file's digest -- and because the hash is cached, that ROM
    could never recover from it.
    """
    romm_repin.record({"id": 7}, "/vol/Game.rvz", {"igdb_id": 42})
    stale_row = romm_repin.pending_rows(10)[0][5]
    romm_repin.settle(stale_row, "done", None, 99)

    romm_repin.record({"id": 8}, "/vol/Game.rvz", {"igdb_id": 43})
    fresh_row = romm_repin.pending_rows(10)[0][5]
    assert fresh_row != stale_row

    # The in-flight pass finishes hashing the file it started on.
    romm_repin.store_sha1(stale_row, "deadbeef")
    assert romm_repin.pending_rows(10)[0][1] is None


def test_settled_rows_are_not_revisited(repin_db) -> None:
    romm_repin.record({"id": 7}, "/vol/Game.rvz", {"igdb_id": 42})
    row_id = romm_repin.pending_rows(10)[0][5]
    romm_repin.settle(row_id, "done", None, 99)
    assert romm_repin.count_pending() == 0
    assert romm_repin.pending_rows(10) == []


@pytest.mark.asyncio
async def test_repin_leaves_unscanned_rows_pending(repin_db, tmp_path: Path) -> None:
    """RomM not having scanned yet is the normal case, not a failure."""
    output = tmp_path / "Game.rvz"
    # Recorded first, then produced -- the order the real flow uses, since the
    # provider ids are only readable while the source is still what RomM knows.
    romm_repin.record({"id": 7}, str(output), {"igdb_id": 42})
    output.write_bytes(b"x" * 8)

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
async def test_repin_waits_until_an_overwritten_output_actually_changes(
    repin_db, tmp_path: Path,
) -> None:
    """Under `overwrite` the destination is occupied when the row is written.

    Existence therefore proves nothing. If the batch is never submitted, the
    file sitting there is still the artifact the conversion was going to
    replace -- hashing it would push this ROM's provider ids onto whatever
    RomM identifies the *old* file as.
    """
    output = tmp_path / "Game.rvz"
    output.write_bytes(b"old" * 8)
    romm_repin.record({"id": 7}, str(output), {"igdb_id": 42})

    hasher = AsyncMock(return_value="abc123")
    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(tmp_path)), \
            patch.object(romm_routes, "run_detached", hasher), \
            patch.object(
                romm_routes.romm_client, "rom_by_sha1", return_value={"id": 108},
            ), \
            patch.object(romm_routes.romm_client, "update_rom_metadata") as update:
        waiting = await romm_routes.settle_romm_repins()
        # Not even hashed: the pass can tell it is looking at the old file.
        hasher.assert_not_awaited()

        # The conversion lands, replacing what was there.
        output.write_bytes(b"new" * 32)
        settled = await romm_routes.settle_romm_repins()

    assert waiting["waiting"] == 1
    assert waiting["repinned"] == 0
    update.assert_called_once()
    assert settled["repinned"] == 1


@pytest.mark.asyncio
async def test_repin_plan_reads_only_the_platform_it_was_given(
    repin_db, tmp_path: Path,
) -> None:
    """The rows came from one platform's listing; say so and skip the scan.

    Without it the backend walks every platform's full paginated catalog until
    each path is found -- hundreds of serialised calls on a large instance
    before a small batch is even queued.
    """
    lib = tmp_path / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    rom = {"id": 7, "name": "Game", "igdb_id": 42,
           "full_path": "roms/gc/Game.iso", "fs_name": "Game.iso"}

    asked: list = []

    def _roms(pid):
        asked.append(pid)
        return [rom] if pid == 9 else []

    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(tmp_path)), \
            patch.object(romm_routes.romm_client, "roms", _roms), \
            patch.object(
                romm_routes.romm_client, "platforms",
                lambda: [{"id": i} for i in range(1, 40)],
            ), \
            patch.object(
                romm_routes, "is_within_configured_volumes", return_value=True,
            ):
        result = await romm_routes.romm_repin_plan(
            romm_routes.RepinPlanRequest(
                paths=[str(lib / "Game.iso")], mode="dolphin_rvz", platform_id=9,
            ),
        )

    assert result["recorded"] == 1, result
    assert asked == [9], "walked other platforms despite being told which one"


@pytest.mark.asyncio
async def test_repin_plan_does_not_scan_when_a_path_is_stale(
    repin_db, tmp_path: Path,
) -> None:
    """One dead selection must not send the lookup across every platform.

    That is the expensive case the platform hint exists to prevent, and
    falling through on a partial match reintroduced it exactly there.
    """
    lib = tmp_path / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    rom = {"id": 7, "name": "Game", "igdb_id": 42,
           "full_path": "roms/gc/Game.iso", "fs_name": "Game.iso"}

    asked: list = []

    def _roms(pid):
        asked.append(pid)
        return [rom] if pid == 9 else []

    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(tmp_path)), \
            patch.object(romm_routes.romm_client, "roms", _roms), \
            patch.object(
                romm_routes.romm_client, "platforms",
                lambda: [{"id": i} for i in range(1, 40)],
            ), \
            patch.object(
                romm_routes, "is_within_configured_volumes", return_value=True,
            ):
        result = await romm_routes.romm_repin_plan(
            romm_routes.RepinPlanRequest(
                # The second path is not in this platform (or any).
                paths=[str(lib / "Game.iso"), str(lib / "Gone.iso")],
                mode="dolphin_rvz", platform_id=9,
            ),
        )

    assert result["recorded"] == 1, result
    assert result["skipped"] == 1
    assert asked == [9], "one stale path sent the lookup across every platform"


@pytest.mark.asyncio
async def test_repin_plan_records_the_destination_the_caller_states(
    repin_db, tmp_path: Path,
) -> None:
    """The queue's answer beats planning's prediction.

    Between resolving a destination and the batch being accepted, another job
    can take the path Rename picked -- the batch then writes elsewhere, and a
    row left on the predicted path would be settled against whatever landed
    there.
    """
    lib = tmp_path / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    rom = {"id": 7, "name": "Game", "igdb_id": 42,
           "full_path": "roms/gc/Game.iso", "fs_name": "Game.iso"}
    actual = str(lib / "Game_2.rvz")

    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(tmp_path)), \
            patch.object(
                romm_routes.romm_repin, "roms_by_local_path",
                return_value={os.path.realpath(lib / "Game.iso"): rom},
            ), \
            patch.object(
                romm_routes, "is_within_configured_volumes", return_value=True,
            ):
        result = await romm_routes.romm_repin_plan(
            romm_routes.RepinPlanRequest(
                paths=[str(lib / "Game.iso")], mode="dolphin_rvz",
                output_paths={str(lib / "Game.iso"): actual},
            ),
        )

    assert result["recorded"] == 1, result
    assert result["recorded_paths"] == {str(lib / "Game.iso"): actual}
    assert romm_repin.pending_rows(10)[0][0] == actual


@pytest.mark.asyncio
async def test_repin_names_a_split_output_instead_of_waiting_it_out(
    repin_db, tmp_path: Path,
) -> None:
    """A split build produced parts, not a file RomM can hash-match.

    makeps3iso only splits past 4 GB, so this cannot be refused when the row is
    written -- nobody knows yet. From the recorded path alone it looks exactly
    like a conversion that never ran, and the row would age out reporting
    "output never appeared", which is false and unactionable.
    """
    output = tmp_path / "Game.iso"
    romm_repin.record({"id": 7}, str(output), {"igdb_id": 42}, "folder_to_iso")
    # The `-s` build crossed 4 GB: numbered parts, no bare ISO.
    (tmp_path / "Game.iso.0").write_bytes(b"x" * 8)
    (tmp_path / "Game.iso.1").write_bytes(b"y" * 8)

    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(tmp_path)), \
            patch.object(
                romm_routes, "run_detached", AsyncMock(return_value="abc123"),
            ) as hasher, \
            patch.object(romm_routes.romm_client, "update_rom_metadata") as update:
        result = await romm_routes.settle_romm_repins()

    assert result["abandoned"] == 1, result
    hasher.assert_not_awaited()
    update.assert_not_called()
    rows = romm_repin.pending_rows(10)
    assert rows == []


@pytest.mark.asyncio
async def test_repin_still_settles_a_split_rule_that_did_not_split(
    repin_db, tmp_path: Path,
) -> None:
    """Under 4 GB a `-s` build writes the bare ISO, which re-pins normally."""
    output = tmp_path / "Game.iso"
    romm_repin.record({"id": 7}, str(output), {"igdb_id": 42}, "folder_to_iso")
    output.write_bytes(b"x" * 8)

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

    assert result["repinned"] == 1, result
    update.assert_called_once_with(108, {"igdb_id": 42})


@pytest.mark.asyncio
async def test_repin_cancel_retires_rows_for_a_batch_that_never_ran(
    repin_db, tmp_path: Path,
) -> None:
    """A rejected batch must not leave a week's worth of phantom backlog."""
    output = tmp_path / "Game.rvz"
    row_id = romm_repin.record({"id": 7}, str(output), {"igdb_id": 42})
    assert romm_repin.count_pending() == 1

    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(tmp_path)):
        result = await romm_routes.romm_repin_cancel(
            romm_routes.RepinCancelRequest(ids=[row_id]),
        )

    assert result["cancelled"] == 1
    assert romm_repin.count_pending() == 0
    # Retired, not deleted: the history of what was planned survives.
    assert romm_repin.cancel([row_id]) == 0


@pytest.mark.asyncio
async def test_repin_plan_reports_each_source_s_recorded_destination(
    repin_db, tmp_path: Path,
) -> None:
    """Keyed by source, so the caller can retire what its batch did not queue.

    `create_batch_jobs` legitimately returns fewer jobs than requested, and the
    paths it dropped must not keep a pending row.
    """
    lib = tmp_path / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    rom = {"id": 7, "name": "Game", "igdb_id": 42}

    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(tmp_path)), \
            patch.object(
                romm_routes.romm_repin, "roms_by_local_path",
                return_value={os.path.realpath(lib / "Game.iso"): rom},
            ), \
            patch.object(
                romm_routes, "is_within_configured_volumes", return_value=True,
            ):
        result = await romm_routes.romm_repin_plan(
            romm_routes.RepinPlanRequest(
                paths=[str(lib / "Game.iso")], mode="dolphin_rvz",
            ),
        )

    assert result["recorded"] == 1
    assert result["recorded_paths"] == {
        str(lib / "Game.iso"): str(lib / "Game.rvz"),
    }
    # And the handle for retiring it, one id per recorded source.
    assert list(result["recorded_ids"]) == [str(lib / "Game.iso")]
    assert romm_repin.cancel(list(result["recorded_ids"].values())) == 1
    assert romm_repin.count_pending() == 0


@pytest.mark.asyncio
async def test_repin_settle_refuses_to_run_two_passes_at_once(
    repin_db, tmp_path: Path,
) -> None:
    """Two tabs settling on load must not hash the same outputs twice."""
    output = tmp_path / "Game.rvz"
    romm_repin.record({"id": 7}, str(output), {"igdb_id": 42})
    output.write_bytes(b"x" * 8)

    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_hash(*_args, **_kwargs):
        started.set()
        await release.wait()
        return "abc123"

    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(tmp_path)), \
            patch.object(romm_routes, "run_detached", _slow_hash), \
            patch.object(romm_routes.romm_client, "rom_by_sha1", return_value=None):
        first = asyncio.create_task(romm_routes.settle_romm_repins())
        await started.wait()
        second = await romm_routes.settle_romm_repins()
        release.set()
        await first

    assert second["busy"] is True
    assert second["waiting"] == 0


@pytest.mark.asyncio
async def test_repin_survives_an_output_the_operator_moved(
    repin_db, tmp_path: Path,
) -> None:
    """A cached digest outlives the path it was taken from.

    RomM matches on the hash, so a renamed output is still re-pinnable --
    abandoning the row there would throw the metadata away for nothing.
    """
    output = tmp_path / "Game.rvz"
    romm_repin.record({"id": 7}, str(output), {"igdb_id": 42})
    output.write_bytes(b"x" * 8)

    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(tmp_path)), \
            patch.object(
                romm_routes, "run_detached", AsyncMock(return_value="abc123"),
            ), \
            patch.object(romm_routes.romm_client, "rom_by_sha1", return_value=None):
        await romm_routes.settle_romm_repins()

    assert romm_repin.pending_rows(10)[0][1] == "abc123"

    # The operator files it away under a different name before RomM scans.
    output.rename(tmp_path / "Renamed.rvz")

    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(tmp_path)), \
            patch.object(
                romm_routes.romm_client, "rom_by_sha1", return_value={"id": 108},
            ), \
            patch.object(romm_routes.romm_client, "update_rom_metadata") as update:
        result = await romm_routes.settle_romm_repins()

    assert result["repinned"] == 1
    update.assert_called_once_with(108, {"igdb_id": 42})


@pytest.mark.asyncio
async def test_repin_applies_metadata_once_romm_has_scanned(
    repin_db, tmp_path: Path,
) -> None:
    output = tmp_path / "Game.rvz"
    romm_repin.record({"id": 7}, str(output), {"igdb_id": 42, "ra_id": 5})
    output.write_bytes(b"x" * 8)

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
    with patch("services.romm.client._urlopen", return_value=_response({"id": 500})) as urlopen:
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
    with patch("services.romm.client._urlopen") as urlopen:
        client.update_rom_metadata(500, {"moby_id": None})
    urlopen.assert_not_called()


def test_update_rom_metadata_ignores_unknown_fields(client: RommClient) -> None:
    """Only the known provider ids are forwarded, never arbitrary keys."""
    with patch("services.romm.client._urlopen", return_value=_response({})) as urlopen:
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



class _FakeJob:
    """The shape `create_batch_jobs` returns, as much of it as the sweep reads.

    The sweep pairs each queued source with its job id so the converted-history
    record can later ask that job how it ended -- a plain object() has neither
    attribute.
    """

    def __init__(self, file_path: str, job_id: str = "") -> None:
        self.file_path = file_path
        self.id = job_id or f"job-{abs(hash(file_path)) % 100000}"


def _fake_jobs(paths):
    return [_FakeJob(p) for p in paths]


@pytest.fixture(name="settings_db")
def _settings_db(sqlite_db):
    """The same database, plus a settings cache reset around the test."""
    from services.romm import settings as romm_settings

    romm_settings.reset_for_tests()
    yield romm_settings
    romm_settings.reset_for_tests()


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
    from services.romm import auto as romm_auto

    rule = romm_auto.normalize_rule({"mode": "dolphin_rvz", "enabled": True})
    assert rule is not None
    # Every field the engine reads is present, so a sweep never KeyErrors on a
    # rule written by an older version of the UI.
    for field in romm_auto.default_rule():
        assert field in rule


def test_rule_with_unknown_mode_is_dropped() -> None:
    from services.romm import auto as romm_auto

    assert romm_auto.normalize_rule({"mode": "not_a_real_mode"}) is None
    assert romm_auto.normalize_rules({"7": {"mode": "nope"}}) == {}


def test_rule_numeric_fields_are_bounded() -> None:
    from services.romm import auto as romm_auto

    rule = romm_auto.normalize_rule({
        "mode": "dolphin_rvz", "interval_minutes": 1, "max_per_run": 99999,
    })
    assert rule["interval_minutes"] == 5
    assert rule["max_per_run"] == 1000


def test_invalid_filter_pattern_is_ignored_not_fatal() -> None:
    from services.romm import auto as romm_auto

    rule = romm_auto.normalize_rule({"mode": "dolphin_rvz", "include_pattern": "([a"})
    assert rule["include_pattern"] is None


def test_compression_only_kept_where_the_mode_supports_it() -> None:
    from services.romm import auto as romm_auto

    # dolphin_gcz takes no compression setting; storing one would be dropped
    # at submit time anyway, so it must not be persisted as if it applied.
    rule = romm_auto.normalize_rule({"mode": "dolphin_gcz", "compression": "zstd"})
    assert rule["compression"] is None


def test_contradictory_match_filters_cancel_out() -> None:
    from services.romm import auto as romm_auto

    rule = romm_auto.normalize_rule({
        "mode": "dolphin_rvz", "only_matched": True, "only_unmatched": True,
    })
    assert rule["only_matched"] is False
    assert rule["only_unmatched"] is False


def test_overnight_window_wraps_midnight() -> None:
    from datetime import datetime, timezone
    from services.romm import auto as romm_auto

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
    from services.romm import auto as romm_auto

    rule = romm_auto.default_rule("dolphin_rvz")
    rule["days"] = [5, 6]  # weekends only
    monday = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    saturday = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    assert not romm_auto._in_window(rule, monday)
    assert romm_auto._in_window(rule, saturday)


def test_disabled_rule_is_never_due() -> None:
    from datetime import datetime, timezone
    from services.romm import auto as romm_auto

    rule = romm_auto.default_rule("dolphin_rvz")
    rule["enabled"] = False
    assert not romm_auto._is_due(rule, {}, datetime.now(timezone.utc))


def test_interval_gates_a_second_run() -> None:
    from datetime import datetime, timedelta, timezone
    from services.romm import auto as romm_auto

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
    from services.romm import auto as romm_auto

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
    from services.romm import auto as romm_auto

    # Sizes chosen so every order produces a *different* sequence. With sizes
    # that happen to descend in name order, a size assertion passes even if the
    # sort key ignores the setting entirely.
    roms = [
        {"id": 3, "name": "Charlie", "fs_size_bytes": 300},
        {"id": 1, "name": "alpha", "fs_size_bytes": 200},
        {"id": 2, "name": "Bravo", "fs_size_bytes": 100},
    ]
    rule = romm_auto.default_rule("dolphin_rvz")

    def order(name: str) -> list[int]:
        rule["order"] = name
        return [r["id"] for r in sorted(roms, key=romm_auto._rom_sort_key(rule))]

    assert order("name") == [1, 2, 3]          # alpha, Bravo, Charlie
    assert order("size_desc") == [3, 1, 2]     # 300, 200, 100
    assert order("size_asc") == [2, 1, 3]      # 100, 200, 300
    assert order("id") == [1, 2, 3]
    assert order("newest") == [3, 2, 1]
    # An unknown order falls back to name rather than to RomM's arbitrary one.
    assert order("nonsense") == [1, 2, 3]


@pytest.mark.asyncio
async def test_sweep_is_idempotent_against_the_filesystem(
    settings_db, tmp_path: Path,
) -> None:
    """The second sweep must queue nothing once the output exists.

    This is the property the whole engine rests on: state lives on disk, not in
    a table, so a sweep that runs twice — or after a restart, or racing a
    manual conversion — converges instead of duplicating work.
    """
    from services.romm import auto as romm_auto

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
        return _fake_jobs(paths)

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
    from services.romm import auto as romm_auto

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
    from services.romm import auto as romm_auto

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
    from services.romm import auto as romm_auto

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
    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    out = tmp_path / "library" / "converted"
    out.mkdir()

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc",
    }]

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        # Saved inside the patch: normalize_rule refuses an output_dir outside
        # the configured volumes, and this tmp path is outside them.
        await romm_auto.set_rules({"7": {
            "mode": "dolphin_rvz", "enabled": True, "output_dir": str(out),
        }})
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
    from services.romm import auto as romm_auto

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
        return _fake_jobs(paths)

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
    from services.romm import auto as romm_auto

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
        return _fake_jobs(paths)

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
    from services.romm import auto as romm_auto

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
        return _fake_jobs(paths)

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
    from services.romm import auto as romm_auto

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
    from services.romm import auto as romm_auto

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
    from services.romm import auto as romm_auto

    rule = romm_auto.normalize_rule({
        "mode": "dolphin_rvz", "timezone": "Mars/Olympus_Mons",
    })
    assert rule["timezone"] == "UTC"


# ----------------------------------------------------------------------
# second review pass: each test fails on the bug, not merely on a crash
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clearing_the_token_beats_the_environment(
    settings_db, monkeypatch,
) -> None:
    """Clearing in the app must not fall straight back to ROMM_TOKEN.

    Without a persisted marker the UI reports the token gone while every
    request keeps authenticating with the environment's.
    """
    monkeypatch.setenv("ROMM_TOKEN", "rmm_from_env")
    await settings_db.load(force=True)
    assert settings_db.token() == "rmm_from_env"

    await settings_db.save({"token": "rmm_from_app"})
    assert settings_db.token() == "rmm_from_app"

    await settings_db.save({"clear_token": True})
    assert settings_db.token() is None
    assert settings_db.public()["token_set"] is False

    # It survives a restart, and setting a token again undoes it.
    await settings_db.load(force=True)
    assert settings_db.token() is None
    await settings_db.save({"token": "rmm_again"})
    assert settings_db.token() == "rmm_again"


@pytest.mark.asyncio
async def test_concurrent_saves_do_not_drop_each_others_fields(
    settings_db,
) -> None:
    """Two cards saving at once must not read-modify-write over each other."""
    await settings_db.save({"url": "http://romm:8080"})
    await asyncio.gather(
        settings_db.save({"library_root": "/data/library"}),
        settings_db.save({"auto_convert_max_per_run": 7}),
    )
    values = settings_db.effective()
    assert values["library_root"] == "/data/library"
    assert values["auto_convert_max_per_run"] == 7
    assert values["url"] == "http://romm:8080"


@pytest.mark.asyncio
async def test_connection_test_with_a_cleared_token_does_not_reuse_the_saved_one(
    settings_db,
) -> None:
    """Testing a cleared token must probe unauthenticated, not report success."""
    seen: list[str] = []

    class _Probe:
        def __init__(self, *, base_url: str, token: str) -> None:
            seen.append(token)

        def heartbeat(self) -> dict:
            return {"VERSION": "3.0"}

        def platforms(self) -> list:
            return []

    await settings_db.save({"url": "http://romm:8080", "token": "rmm_saved"})
    with patch.object(romm_routes, "RommClient", _Probe):
        await romm_routes.test_romm_connection(
            romm_routes.RommSettingsPatch(clear_token=True),
        )
        await romm_routes.test_romm_connection(romm_routes.RommSettingsPatch())
    assert seen == ["", "rmm_saved"], seen


def test_multipart_refuses_control_characters_in_a_value() -> None:
    """A remote-supplied value must not be able to forge a part header."""
    from services.romm.client import RommError, _encode_multipart

    body, content_type = _encode_multipart({"igdb_id": "42"})
    assert b'name="igdb_id"' in body
    assert content_type.startswith("multipart/form-data; boundary=")

    with pytest.raises(RommError):
        _encode_multipart({"igdb_id": "42\r\nContent-Disposition: form-data"})


def test_rom_by_sha1_reads_the_status_not_the_message() -> None:
    """A 500 whose body mentions "HTTP 404" must not be read as "no match"."""
    from services.romm import RommClient, RommError

    client = RommClient(base_url="http://romm:8080", token="t")
    with patch.object(
        RommClient, "_request",
        side_effect=RommError("upstream said: HTTP 404 somewhere", status=500),
    ), pytest.raises(RommError):
        client.rom_by_sha1("deadbeef")

    with patch.object(
        RommClient, "_request", side_effect=RommError("HTTP 404", status=404),
    ):
        assert client.rom_by_sha1("deadbeef") is None


def test_naive_last_run_does_not_abort_the_schedule_check() -> None:
    """A hand-edited state row must not TypeError the whole sweep."""
    from datetime import datetime, timezone

    from services.romm import auto as romm_auto

    rule = romm_auto.normalize_rule({
        "mode": "dolphin_rvz", "enabled": True, "interval_minutes": 60,
    })
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    # No trailing Z: parses to a naive datetime.
    stale = {"last_run_at": "2026-05-01T09:00:00"}
    assert romm_auto._is_due(rule, stale, now) is True
    recent = {"last_run_at": "2026-05-01T11:30:00"}
    assert romm_auto._is_due(rule, recent, now) is False


@pytest.mark.asyncio
async def test_rename_policy_queues_a_free_path_instead_of_skipping(
    settings_db, tmp_path: Path,
) -> None:
    """`rename` must mean rename, not degrade to `skip`.

    The sweep used to consult the duplicate policy only to decide whether to
    look for an existing output, so `overwrite` and `rename` both silently
    behaved like `skip` once one existed.
    """
    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    # The output this rule would derive already exists.
    (lib / "Game.rvz").write_bytes(b"\0" * 16)

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc",
    }]

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        await romm_auto.set_rules({"7": {
            "mode": "dolphin_rvz", "enabled": True, "duplicate_action": "skip",
        }})
        skipped = await romm_auto.sweep(ignore_schedule=True, dry_run=True)
        assert skipped["queued"] == 0
        assert skipped["skipped_existing"] == 1

        await romm_auto.set_rules({"7": {
            "mode": "dolphin_rvz", "enabled": True, "duplicate_action": "rename",
        }})
        queued = AsyncMock(return_value=[MagicMock()])
        with patch.object(romm_auto.job_manager, "create_batch_jobs", queued):
            renamed = await romm_auto.sweep(ignore_schedule=True)

    assert renamed["queued"] == 1, renamed
    assert renamed["skipped_existing"] == 0
    # The point of the fix: the sweep resolved the free name itself and handed
    # it down. Queueing without it let the job re-derive the taken path and
    # collide, so "rename" produced a failed job instead of a renamed output.
    kwargs = queued.await_args.kwargs
    assert kwargs["output_paths"] == {str(lib / "Game.iso"): str(lib / "Game_1.rvz")}
    assert kwargs["allow_overwrite"] is False


@pytest.mark.asyncio
async def test_failed_queueing_leaves_no_pending_repin_rows(
    settings_db, tmp_path: Path,
) -> None:
    """A re-pin row must never outlive the conversion it was recorded for.

    Recorded before the queue call, a row survives a failed submit and then
    re-pins whatever later lands on that path.
    """
    from services.romm import auto as romm_auto, repin as romm_repin

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc", "igdb_id": 99,
    }]

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(
                romm_auto.job_manager, "create_batch_jobs",
                AsyncMock(side_effect=RuntimeError("queue exploded")),
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        await romm_auto.set_rules({
            "7": {"mode": "dolphin_rvz", "enabled": True},
        })
        result = await romm_auto.sweep(ignore_schedule=True)

    assert result["queued"] == 0
    assert result["repins_recorded"] == 0
    assert romm_repin.count_pending() == 0


@pytest.mark.asyncio
async def test_repin_plan_honours_the_repin_switch(settings_db, tmp_path: Path) -> None:
    """Turning re-pinning off must silence the manual path too, not just the sweep."""
    await settings_db.save({
        "url": "http://romm:8080",
        "library_root": str(tmp_path),
        "repin_enabled": False,
    })
    payload = romm_routes.RepinPlanRequest(
        paths=[str(tmp_path / "Game.iso")], mode="dolphin_rvz",
    )
    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(tmp_path)), \
            patch.object(romm_repin, "roms_by_local_path") as fetch:
        body = await romm_routes.romm_repin_plan(payload)
    assert body["recorded"] == 0
    assert body["reason"] == "disabled"
    # The switch short-circuits before the catalog read, not after it.
    fetch.assert_not_called()


@pytest.mark.asyncio
async def test_platform_listing_carries_the_narrowed_tool_ids() -> None:
    """The automation editor gets its per-platform tool list from the registry."""
    platforms = [
        {"id": 1, "name": "GameCube", "slug": "ngc"},
        {"id": 2, "name": "PlayStation 2", "slug": "ps2"},
    ]
    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", "/data/library"), \
            patch.object(romm_routes.romm_client, "platforms", return_value=platforms):
        rows = await romm_routes.romm_platforms()

    by_name = {row["name"]: row["tool_ids"] for row in rows}
    assert "dolphin" in by_name["GameCube"]
    assert "chdman" not in by_name["GameCube"]
    assert "chdman" in by_name["PlayStation 2"]
    assert "dolphin" not in by_name["PlayStation 2"]


@pytest.mark.asyncio
async def test_rule_settings_reach_the_queue(settings_db, tmp_path: Path) -> None:
    """Every switch the editor offers must arrive at the job, not stop at the rule.

    `split`, the compression *level*, and `verify_after` were all stored and
    then dropped on the way to `create_batch_jobs`, so toggling them in the UI
    changed nothing about the conversion that ran.
    """
    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc",
    }]

    queued = AsyncMock(return_value=[MagicMock()])
    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto.job_manager, "create_batch_jobs", queued), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        await romm_auto.set_rules({"7": {
            "mode": "dolphin_rvz",
            "enabled": True,
            "compression": "zstd",
            "compression_level": 7,
            "split": True,
            "verify_after": True,
        }})
        await romm_auto.sweep(ignore_schedule=True)

    kwargs = queued.await_args.kwargs
    # The level is not a separate parameter downstream; it rides on the string.
    assert kwargs["compression"] == "zstd:7"
    assert kwargs["split"] is True
    assert kwargs["verify_after"] is True


def test_verify_after_is_refused_on_a_mode_that_cannot_verify() -> None:
    """The switch is registry-gated, exactly like delete-on-verify."""
    from services.romm import auto as romm_auto

    # dolphin_rvz supports the verify step; chdman's extract direction does not.
    rvz = romm_auto.normalize_rule({"mode": "dolphin_rvz", "verify_after": True})
    assert rvz["verify_after"] is True

    extract = romm_auto.normalize_rule({"mode": "extractcd", "verify_after": True})
    assert extract["verify_after"] is False
    assert extract["delete_on_verify"] is False


# ----------------------------------------------------------------------
# third review pass
# ----------------------------------------------------------------------


def test_delete_on_verify_refused_when_the_verify_is_only_structural() -> None:
    """A mode that *can* verify is not always safely deletable.

    jwud's verify is a structural WUX walk, backed only by JWUDTool's own
    byte-for-byte pass — which `noverify` turns off. The manual route refuses
    that combination; an unattended rule must too, or it deletes a 25 GB source
    on the strength of a geometry check.
    """
    from services.romm import auto as romm_auto

    unsafe = romm_auto.normalize_rule({
        "mode": "jwud_compress", "compression": "noverify", "delete_on_verify": True,
    })
    assert unsafe["delete_on_verify"] is False
    assert unsafe["unsafe_delete_on_verify"] is True

    safe = romm_auto.normalize_rule({
        "mode": "jwud_compress", "delete_on_verify": True,
    })
    assert safe["delete_on_verify"] is True
    assert safe["unsafe_delete_on_verify"] is False


@pytest.mark.asyncio
async def test_overwrite_never_targets_the_rule_s_own_source(
    settings_db, tmp_path: Path,
) -> None:
    """A conversion must not be authorised to write over its own input.

    RomM rescans what we produce, so a persistent `dolphin_rvz` rule eventually
    sees the .rvz it made — dolphin accepts .rvz as input and `output_path` maps
    it straight back onto itself. An `overwrite` rule would then unlink the file
    before reading it, destroying the only copy once the source ISO was gone.
    """
    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.rvz").write_bytes(b"\0" * 32)

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.rvz",
        "fs_name": "Game.rvz", "platform_slug": "ngc",
    }]

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        await romm_auto.set_rules({"7": {
            "mode": "dolphin_rvz", "enabled": True, "duplicate_action": "overwrite",
        }})
        result = await romm_auto.sweep(ignore_schedule=True, dry_run=True)

    assert result["queued"] == 0, result
    assert result["skipped_existing"] == 1


@pytest.mark.asyncio
async def test_rule_defaults_come_from_the_configured_settings(
    settings_db,
) -> None:
    """The documented first-run env defaults must actually reach a new rule."""
    await settings_db.save({
        "auto_convert_interval_minutes": 240,
        "auto_convert_max_per_run": 5,
        "verify_after_convert": True,
        "delete_source_after_verify": True,
    })
    from services.romm import auto as romm_auto

    fresh = romm_auto.default_rule("dolphin_rvz")
    assert fresh["interval_minutes"] == 240
    assert fresh["max_per_run"] == 5
    assert fresh["verify_after"] is True
    assert fresh["delete_on_verify"] is True

    # A stored rule that simply omits the field inherits it too, rather than
    # having it silently forced back off.
    inherited = romm_auto.normalize_rule({"mode": "dolphin_rvz"})
    assert inherited["verify_after"] is True
    assert inherited["delete_on_verify"] is True

    # An explicit False still wins.
    explicit = romm_auto.normalize_rule({
        "mode": "dolphin_rvz", "verify_after": False, "delete_on_verify": False,
    })
    assert explicit["verify_after"] is False
    assert explicit["delete_on_verify"] is False


@pytest.mark.asyncio
async def test_abandonment_uses_the_configured_period(settings_db) -> None:
    """`repin_abandon_days` was documented, persisted, and read by nobody."""
    from datetime import datetime, timedelta, timezone

    def stamp(days_ago: int) -> str:
        when = datetime.now(timezone.utc) - timedelta(days=days_ago)
        return when.isoformat().replace("+00:00", "Z")

    await settings_db.save({"repin_abandon_days": 30})
    assert romm_routes._is_stale(stamp(10)) is False
    assert romm_routes._is_stale(stamp(31)) is True

    await settings_db.save({"repin_abandon_days": 3})
    assert romm_routes._is_stale(stamp(10)) is True


@pytest.mark.asyncio
async def test_connection_test_treats_a_cleared_url_as_cleared(
    settings_db,
) -> None:
    """Clearing the URL and pressing Test must not probe the saved one."""
    await settings_db.save({
        "url": "http://romm:8080", "library_root": "/data/library",
    })
    result = await romm_routes.test_romm_connection(
        romm_routes.RommSettingsPatch(url=""),
    )
    assert result["reachable"] is False
    assert result["error"] == "Set the RomM URL first."


def test_re_recording_supersedes_rather_than_mutating(repin_db) -> None:
    """A settle pass in flight must not settle the row that replaced its own.

    The settler detaches a row id before hashing, which takes minutes for a
    multi-GB image. Mutating that row meanwhile left the settler marking it
    done and the re-planned conversion with no pending row at all.
    """
    romm_repin.record({"id": 7}, "/vol/Game.rvz", {"igdb_id": 42})
    first = romm_repin.pending_rows(10)[0][5]

    romm_repin.record({"id": 8}, "/vol/Game.rvz", {"igdb_id": 43})
    rows = romm_repin.pending_rows(10)
    # Still exactly one pending row, but it is a *new* one.
    assert len(rows) == 1
    second = rows[0][5]
    assert second != first
    assert rows[0][3] == {"igdb_id": 43}

    # The in-flight settler finishes against the row it started on: a no-op.
    romm_repin.settle(first, "done", None, 99)
    still_pending = romm_repin.pending_rows(10)
    assert len(still_pending) == 1
    assert still_pending[0][5] == second


# ----------------------------------------------------------------------
# fourth review pass
# ----------------------------------------------------------------------


def test_cross_origin_redirect_is_refused_before_the_token_travels() -> None:
    """urllib copies Authorization onto a redirected GET.

    A RomM that 302s elsewhere — misconfigured, compromised, or behind a hostile
    proxy — would otherwise hand the `rmm_` token to that host.
    """
    from email.message import Message

    from services.romm.client import _SameOriginRedirectHandler

    handler = _SameOriginRedirectHandler("http://romm:8080/api/roms")
    request = urllib.request.Request(
        "http://romm:8080/api/roms", headers={"Authorization": "Bearer rmm_secret"},
    )

    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(
            request, None, 302, "Found", Message(), "http://attacker.example/collect",
        )
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(
            request, None, 302, "Found", Message(), "file:///etc/passwd",
        )

    # Same origin still follows, or ordinary RomM redirects would break.
    same = handler.redirect_request(
        request, None, 302, "Found", Message(), "http://romm:8080/api/roms/",
    )
    assert same is not None
    assert same.get_header("Authorization") == "Bearer rmm_secret"


def test_nsz_layout_and_level_both_reach_the_job() -> None:
    """nsz declares only `supports_compression_level`, but offers a layout too.

    Gating the codec on `supports_compression` alone dropped the solid/block
    choice and — because the level rides on the same string — the level with it.
    """
    from services.romm import auto as romm_auto

    rule = romm_auto.normalize_rule({
        "mode": "nsz_compress", "compression": "block", "compression_level": 12,
    })
    assert rule["compression"] == "block"
    assert romm_auto._compression_arg(rule) == "block:12"

    # A level with no layout is still expressible: nsz reads the empty layout
    # part as "tool default" and honours the level.
    level_only = romm_auto.normalize_rule({
        "mode": "nsz_compress", "compression_level": 12,
    })
    assert romm_auto._compression_arg(level_only) == ":12"


def test_a_level_without_a_codec_names_the_tools_default() -> None:
    """"Tool default" plus a level must not reach dolphin-tool as `-c ""`.

    The level rides on the codec string, so a level with no codec serialises as
    ":19". nsz reads that empty part as its own default layout; dolphin does
    not — `DolphinTool._build_command` splits it into an empty codec and emits
    `-c "" -l 19`, which fails every queued job. The mode says which case it is.
    """
    from services.romm import auto as romm_auto
    from services.tools import registry

    rule = romm_auto.normalize_rule({
        "mode": "dolphin_rvz", "compression_level": 19,
    })
    assert rule["compression"] is None
    assert romm_auto._compression_arg(rule) == "zstd:19"
    # A codec the operator did pick is never second-guessed.
    chosen = romm_auto.normalize_rule({
        "mode": "dolphin_rvz", "compression": "lzma2", "compression_level": 9,
    })
    assert romm_auto._compression_arg(chosen) == "lzma2:9"
    # And the mode that has no codec picker keeps its meaningful empty part.
    assert registry.default_compression("nsz_compress") is None


def test_a_codec_mode_with_no_declared_default_drops_the_level(monkeypatch) -> None:
    """Rather than queue a job the tool will refuse.

    Inventing a codec would convert the library with a setting the operator did
    not choose; sending an empty one fails at the tool. Neither is acceptable,
    so the level is dropped and logged.
    """
    from services.romm import auto as romm_auto
    from services.tools import registry

    monkeypatch.setattr(
        registry.for_mode("dolphin_rvz"), "default_compression", None,
    )
    rule = romm_auto.normalize_rule({
        "mode": "dolphin_rvz", "compression_level": 19,
    })
    assert romm_auto._compression_arg(rule) is None


def test_a_polynomial_filter_pattern_is_refused_too() -> None:
    """Nested quantifiers are not the only shape that wedges the sweep.

    `a*a*a*a*a*a*b` has no group at all, so the nested-shape test passes it —
    but on a long run of `a` with no `b` the engine tries every way to split
    that run between six stars, and `re` cannot be interrupted while
    `_sweep_lock` is held. Adjacency plus an overlapping atom is the tell.
    """
    from services.romm import auto as romm_auto

    assert romm_auto._valid_pattern("a*a*a*a*a*a*b") == (None, True)
    assert romm_auto._valid_pattern(r"\w*\d*x") == (None, True)
    assert romm_auto._valid_pattern("(a*)(a*)b") == (None, True)

    # And the filters an operator actually writes still go through: two
    # quantifiers that cannot both take the same character are not a hazard,
    # nor are quantifiers with something fixed between them.
    for good in (
        r"^Metroid.*\.iso$", "(USA)", ".*rev1.*", r"\w*\s*Disc", r"^\d{4}-.*",
    ):
        assert romm_auto._valid_pattern(good) == (good, False), good


def test_deselecting_every_weekday_is_honoured_not_widened() -> None:
    """An empty day list used to come back as all seven.

    Which is the worst possible reading: a rule the operator parked by
    unticking every day would run every day instead of none — unattended, and
    with delete-on-verify if that was set.
    """
    from datetime import datetime, timezone

    from services.romm import auto as romm_auto

    rule = romm_auto.normalize_rule({
        "mode": "dolphin_rvz", "enabled": True, "days": [],
    })
    assert rule["days"] == []
    # And the scheduler agrees: no day is ever this rule's day.
    now = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
    assert romm_auto._in_window(rule, now) is False

    # Omitting the field still inherits every day, so an older stored rule (or
    # one written by hand) is not silently switched off.
    inherited = romm_auto.normalize_rule({"mode": "dolphin_rvz", "enabled": True})
    assert inherited["days"] == list(romm_auto.ALL_DAYS)


def test_a_mistyped_filter_pauses_the_rule_instead_of_widening_it() -> None:
    """An uncompilable regex must never come back as "no filter".

    The filter is what keeps a rule to a subset; dropping it silently would let
    the next unattended sweep queue the whole platform, delete-on-verify and all.
    """
    from services.romm import auto as romm_auto

    rule = romm_auto.normalize_rule({
        "mode": "dolphin_rvz", "enabled": True, "include_pattern": "(USA",
    })
    assert rule["enabled"] is False
    assert rule["invalid_pattern"] is True
    assert rule["include_pattern"] is None


def test_a_rejected_output_dir_pauses_the_rule() -> None:
    """Clearing output_dir without pausing would silently relocate the writes.

    The rule would then fill whichever filesystem holds the sources, rather
    than the one the operator chose.
    """
    from services.romm import auto as romm_auto

    with patch.object(romm_auto, "is_within_configured_volumes", return_value=False):
        rule = romm_auto.normalize_rule({
            "mode": "dolphin_rvz", "enabled": True, "output_dir": "/somewhere/else",
        })
    assert rule["enabled"] is False
    assert rule["output_dir"] is None
    assert rule["invalid_output_dir"] == "/somewhere/else"


@pytest.mark.asyncio
async def test_a_failed_queue_does_not_advance_the_schedule(
    settings_db, tmp_path: Path,
) -> None:
    """A transient queue error must not cost the platform a whole interval."""
    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc",
    }]

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(
                romm_auto.job_manager, "create_batch_jobs",
                AsyncMock(side_effect=RuntimeError("queue exploded")),
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        await romm_auto.set_rules({"7": {"mode": "dolphin_rvz", "enabled": True}})
        await romm_auto.sweep(ignore_schedule=True)

    # No run recorded, so the next scheduled tick retries immediately.
    assert await romm_auto.get_state() == {}


def test_rename_probe_is_bounded(tmp_path: Path) -> None:
    """An unbounded probe is quadratic on a directory full of numbered outputs."""
    from services.output_conflicts import (
        MAX_RENAME_ATTEMPTS,
        OutputPathExhausted,
        get_unique_output_path,
    )

    base = tmp_path / "Game.chd"
    base.write_bytes(b"x")
    with patch(
        "services.output_conflicts.lock_manager.check_file_status",
        return_value=(True, False),
    ), pytest.raises(OutputPathExhausted):
        get_unique_output_path(str(base))
    assert MAX_RENAME_ATTEMPTS == 1000


def test_repin_index_and_lookup_share_one_canonical_key(tmp_path: Path) -> None:
    """`local_path()` returns a realpath, so the index must be keyed the same.

    An abspath key against a realpath value made a symlinked library miss every
    lookup, losing that ROM's metadata without a word.
    """
    real = tmp_path / "real"
    real.mkdir()
    (real / "Game.iso").write_bytes(b"\0" * 8)
    link = tmp_path / "library"
    link.symlink_to(real)

    rom = {"id": 1, "name": "Game", "full_path": "Game.iso", "fs_name": "Game.iso"}
    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(link)), \
            patch.object(
                romm_repin.romm_client, "platforms", return_value=[{"id": 7}],
            ), \
            patch.object(romm_repin.romm_client, "roms", return_value=[rom]):
        index = romm_repin.roms_by_local_path([str(link / "Game.iso")])

    assert index, "the symlinked library path must still resolve to its ROM"
    assert str(real / "Game.iso") in index


@pytest.mark.asyncio
async def test_repin_plan_records_the_path_the_batch_will_write(
    settings_db, repin_db, tmp_path: Path,
) -> None:
    """The row must name the destination, not the one the batch renames away from.

    Under Rename the batch writes `Game_1.rvz`; recording the occupied base path
    would re-pin the file already sitting there and leave the new one
    unidentified. Under Skip the source is never queued at all, so a row would
    wait for a conversion that never runs.
    """
    lib = tmp_path / "roms"
    lib.mkdir()
    source = lib / "Game.iso"
    source.write_bytes(b"\0" * 32)
    (lib / "Game.rvz").write_bytes(b"\0" * 8)  # the base output is taken

    await settings_db.save({"url": "http://romm:8080", "library_root": str(tmp_path)})
    rom = {"id": 7, "name": "Game", "fs_name": "Game.iso", "igdb_id": 42}

    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(tmp_path)), \
            patch.object(
                romm_repin, "roms_by_local_path",
                return_value={os.path.realpath(str(source)): rom},
            ), \
            patch.object(romm_routes, "is_within_configured_volumes", return_value=True):
        skipped = await romm_routes.romm_repin_plan(
            romm_routes.RepinPlanRequest(
                paths=[str(source)], mode="dolphin_rvz", duplicate_action="skip",
            ),
        )
        assert skipped["recorded"] == 0, skipped
        assert romm_repin.count_pending() == 0

        renamed = await romm_routes.romm_repin_plan(
            romm_routes.RepinPlanRequest(
                paths=[str(source)], mode="dolphin_rvz", duplicate_action="rename",
            ),
        )
    assert renamed["recorded"] == 1, renamed
    rows = romm_repin.pending_rows(10)
    assert rows[0][0] == str(lib / "Game_1.rvz"), rows


# ----------------------------------------------------------------------
# fifth review pass
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_rule_converts_each_source_exactly_once(
    settings_db, tmp_path: Path,
) -> None:
    """A standing `rename` rule must not reconvert forever.

    Once a sweep writes an output the base path is occupied, so the next sweep
    would pick `Game_1`, then `Game_2`, and a scheduled rule would reconvert
    the same ROM until the destination filled up. The filesystem cannot answer
    this on its own -- `rename` means "write alongside", so a free suffix is
    always available -- which is why the rule records what it has converted.
    """
    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    (lib / "Game.rvz").write_bytes(b"\0" * 8)  # something already at the base

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc",
    }]
    queued: list[list[str]] = []

    async def _fake_batch(paths, mode, **kwargs):
        queued.append(list(paths))
        return _fake_jobs(paths)

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(romm_auto.job_manager, "create_batch_jobs", _fake_batch), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        await romm_auto.set_rules({"7": {
            "mode": "dolphin_rvz", "enabled": True, "duplicate_action": "rename",
        }})
        first = await romm_auto.sweep(ignore_schedule=True)
        assert first["queued"] == 1, first

        # That conversion completes, taking the next free suffix.
        (lib / "Game_1.rvz").write_bytes(b"\0" * 8)
        second = await romm_auto.sweep(ignore_schedule=True)

        # A preview reads the same history, so what the operator is shown
        # matches what a run would do.
        preview = await romm_auto.sweep(ignore_schedule=True, dry_run=True)

    assert second["queued"] == 0, second
    assert second["skipped_existing"] == 1
    assert preview["queued"] == 0, preview
    assert queued == [[str(lib / "Game.iso")]]


@pytest.mark.asyncio
async def test_overwrite_rule_converts_each_source_exactly_once(
    settings_db, tmp_path: Path,
) -> None:
    """The same guarantee for `overwrite`, which the filesystem cannot give.

    An occupied destination is queueable *by definition* under this policy, so
    without the recorded history a scheduled rule rewrites the same
    multi-gigabyte image every interval, forever.
    """
    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc",
    }]
    queued: list[list[str]] = []

    async def _fake_batch(paths, mode, **kwargs):
        queued.append(list(paths))
        return _fake_jobs(paths)

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(romm_auto.job_manager, "create_batch_jobs", _fake_batch), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        await romm_auto.set_rules({"7": {
            "mode": "dolphin_rvz", "enabled": True, "duplicate_action": "overwrite",
        }})
        assert (await romm_auto.sweep(ignore_schedule=True))["queued"] == 1
        (lib / "Game.rvz").write_bytes(b"\0" * 8)
        second = await romm_auto.sweep(ignore_schedule=True)

        # Retargeting the rule invalidates that history: the new format has
        # not been produced for anything yet.
        await romm_auto.set_rules({"7": {
            "mode": "dolphin_gcz", "enabled": True, "duplicate_action": "overwrite",
        }})
        retargeted = await romm_auto.sweep(ignore_schedule=True)

    assert second["queued"] == 0, second
    assert second["skipped_existing"] == 1
    assert retargeted["queued"] == 1, retargeted


@pytest.mark.asyncio
async def test_forget_converted_lets_a_rule_run_again(
    settings_db, tmp_path: Path,
) -> None:
    """The operator's escape hatch: restoring a backup must be recoverable."""
    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc",
    }]

    async def _fake_batch(paths, mode, **kwargs):
        return _fake_jobs(paths)

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(romm_auto.job_manager, "create_batch_jobs", _fake_batch), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        await romm_auto.set_rules({"7": {
            "mode": "dolphin_rvz", "enabled": True, "duplicate_action": "overwrite",
        }})
        assert (await romm_auto.sweep(ignore_schedule=True))["queued"] == 1
        # The conversion lands. Only now is the ROM recorded as converted --
        # queueing is not producing.
        (lib / "Game.rvz").write_bytes(b"\0" * 8)
        assert (await romm_auto.sweep(ignore_schedule=True))["queued"] == 0

        assert await romm_auto.forget_converted(["7"]) == 1
        assert (await romm_auto.sweep(ignore_schedule=True))["queued"] == 1

    # The schedule clock survives forgetting the conversion history: they
    # share one state entry, and losing "last run" would restart the interval.
    state = await romm_auto.get_state()
    assert state["7"].get("last_run_at")


@pytest.mark.asyncio
async def test_job_manager_rechecks_tool_delete_safety() -> None:
    """The last line of defence before a source is unlinked.

    Both plan sites check `delete_on_verify_is_safe`, but a job can reach the
    manager without passing either -- restored queue state, a hand-edited rule
    blob. A Wii U `noverify` conversion passing only the structural check must
    not be allowed to delete a 25 GB source.
    """
    from services.tools import registry as tool_registry

    tool = tool_registry.for_mode("jwud_compress")
    assert tool.delete_on_verify_is_safe("jwud_compress", "noverify") is False
    assert tool.delete_on_verify_is_safe("jwud_compress", None) is True
    # The mode itself advertises the capability -- which is exactly why the
    # spec check alone was not enough to stop the delete.
    assert tool_registry.spec("jwud_compress").supports_delete_on_verify is True


@pytest.mark.asyncio
async def test_a_queued_conversion_that_never_ran_is_retried(
    settings_db, tmp_path: Path,
) -> None:
    """Queueing is not producing.

    A job can be cancelled, or interrupted by a restart, or fail in the
    converter. Recording the ROM as converted the moment it was queued would
    skip it on every later sweep, and only clearing the history by hand would
    bring it back.
    """
    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc",
    }]

    async def _fake_batch(paths, mode, **kwargs):
        return _fake_jobs(paths)

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(romm_auto.job_manager, "create_batch_jobs", _fake_batch), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        await romm_auto.set_rules({"7": {
            "mode": "dolphin_rvz", "enabled": True, "duplicate_action": "overwrite",
        }})
        assert (await romm_auto.sweep(ignore_schedule=True))["queued"] == 1

        # The job never produced anything -- cancelled, or the process
        # restarted while it waited. The destination is untouched.
        retried = await romm_auto.sweep(ignore_schedule=True)
        assert retried["queued"] == 1, retried

        # Now it lands, and the rule stops.
        (lib / "Game.rvz").write_bytes(b"\0" * 8)
        assert (await romm_auto.sweep(ignore_schedule=True))["queued"] == 0


@pytest.mark.asyncio
async def test_romm_listing_includes_directory_records(tmp_path: Path) -> None:
    """A PS3 record resolves to a folder, which is makeps3iso's input unit.

    Dropping it made `folder_to_iso` reachable from automation but not by
    hand, for the very same records, while the platform advertised the tool.
    """
    lib = tmp_path / "roms" / "ps3"
    folder = lib / "MyGame"
    (folder / "PS3_GAME" / "USRDIR").mkdir(parents=True)
    (folder / "PS3_GAME" / "PARAM.SFO").write_bytes(b"\0" * 16)

    roms = [{
        "id": 1, "name": "My Game", "full_path": "roms/ps3/MyGame",
        "fs_name": "MyGame", "platform_slug": "ps3", "fs_size_bytes": 1234,
    }]

    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(tmp_path)), \
            patch.object(
                romm_routes, "is_within_configured_volumes", return_value=True,
            ):
        entries = romm_routes._build_entries(roms, "ps3")

    assert len(entries) == 1, entries
    assert entries[0].type == "directory"
    assert entries[0].size == 1234
    assert "makeps3iso" in (entries[0].convertible_by or [])


def test_redirect_origin_ignores_a_spelled_out_default_port() -> None:
    """`https://romm` and `https://romm:443` are the same origin.

    Refusing that would break an ordinary RomM behind a proxy that spells the
    port out, while a scheme downgrade must still be refused.
    """
    origin_of = romm_service._SameOriginRedirectHandler._origin_of

    assert origin_of("https://romm/api") == origin_of("https://romm:443/api")
    assert origin_of("http://romm/api") == origin_of("http://romm:80/api")
    # Still different origins.
    assert origin_of("https://romm/api") != origin_of("http://romm:80/api")
    assert origin_of("https://romm:8443/api") != origin_of("https://romm:443/api")
    assert origin_of("https://romm/api") != origin_of("https://evil/api")


def test_catastrophic_filter_patterns_are_refused_at_save_time() -> None:
    """A pattern that backtracks must not reach the sweep.

    `re` cannot be interrupted, and the sweep evaluates filters while holding
    the sweep lock — so previews, manual runs and even editing the rule to
    remove the pattern would all queue behind it, leaving a restart as the only
    way out.

    Detected by reading the pattern, never by running it: executing an
    operator-supplied regex to time it is the thing CodeQL flags, and a timing
    test would also have to survive the very blow-up it is looking for.
    """
    from services.romm import auto as romm_auto

    good = (
        r"\(USA\)", ".*Beta.*", "^Super", r"^.*\(USA\).*(Rev [0-9])?$",
        "(USA|Europe)", r"\d{4}", "[a-z]+", "Disc [0-9] of [0-9]",
        "(?i)final.*fantasy", r"^\(USA\).*\[!\]$", "Pokemon.*(Red|Blue)",
        r"(?:USA|Japan)", r"Final Fantasy (I{1,3}|IV)",
    )
    for pattern in good:
        assert romm_auto._has_nested_quantifier(pattern) is False, pattern
        re.compile(pattern)  # and they are all real regexes

    for pattern in ("(a+)+$", "(x+x+)+y", "(a|a)+$", r"^(\w+\s?)*$", "(a*)*b"):
        assert romm_auto._has_nested_quantifier(pattern) is True, pattern

    # And the rule that carries one is paused rather than silently unfiltered.
    pattern, invalid = romm_auto._valid_pattern("(a+)+$")
    assert pattern is None and invalid is True


def test_composite_modes_are_narrowed_per_platform() -> None:
    """A tool that belongs to no system needs its modes narrowed individually.

    The chain tool has a GameCube mode and a PS2 mode, so a tool-level check
    keeps it on both -- and then offered each mode on the wrong console.
    """
    assert registry.narrow_to_platform(["chain"], "ps2") == ["chain"]
    assert registry.narrow_to_platform(["chain"], "ngc") == ["chain"]

    assert registry.mode_allows_platform("cso_to_chd", "ps2") is True
    assert registry.mode_allows_platform("cso_to_chd", "ngc") is False
    assert registry.mode_allows_platform("nkit_to_rvz", "ngc") is True
    assert registry.mode_allows_platform("nkit_to_rvz", "ps2") is False

    assert registry.mode_allows_platform("createdvd", "ps2") is True
    assert registry.mode_allows_platform("createdvd", "ngc") is False
    # A mode with no opinion of its own inherits its tool's: recompressing a
    # finished .chd is the same operation on every system chdman serves.
    assert registry.mode_allows_platform("copy", "ps2") is True
    assert registry.mode_allows_platform("copy", "psx") is True
    assert registry.mode_allows_platform("copy", "ngc") is False
    # An unrecognised slug narrows nothing, matching narrow_to_platform.
    assert registry.mode_allows_platform("cso_to_chd", "not-a-console") is True


def test_chdman_media_commands_are_narrowed_per_platform() -> None:
    """`createcd` and `createdvd` are not interchangeable.

    Narrowing at tool granularity offered a PS2 catalog every chdman create
    mode, and since `createcd` is the first of them it is the one an ISO
    submission defaults to -- the wrong media command, chosen for the user by
    a list that was only ever narrowed to the right *tool*.
    """
    dvd = ("ps2", "psp")
    cd = ("psx", "ps", "dc", "saturn", "3do", "philips-cd-i")

    for slug in dvd:
        modes = registry.modes_for_platform(slug)
        assert "createdvd" in modes, slug
        assert "createcd" not in modes, slug
        assert "createhd" not in modes and "createld" not in modes, slug
        # And the default a platform-narrowed panel lands on is the DVD one.
        assert modes[0] == "createdvd", slug

    for slug in cd:
        modes = registry.modes_for_platform(slug)
        assert "createcd" in modes, slug
        assert "createdvd" not in modes, slug
        assert modes[0] == "createcd", slug

    # MAME ships CD, hard-disk and raw CHDs alike, so arcade keeps all three:
    # a wrong exclusion is worse than a missing one.
    arcade = registry.modes_for_platform("arcade")
    for mode in ("createcd", "createhd", "createraw"):
        assert mode in arcade, mode

    # The extract direction splits the same way -- extracting a PS2 CHD to a
    # .cue track sheet is the same mistake in reverse.
    assert registry.mode_allows_platform("extractdvd", "ps2") is True
    assert registry.mode_allows_platform("extractcd", "ps2") is False

    # The tool-level set stays the union, so nothing chdman serves is lost.
    chdman = registry.for_mode("createcd")
    for slug in dvd + cd + ("arcade",):
        assert slug in chdman.platform_slugs, slug


@pytest.mark.asyncio
async def test_sweep_refuses_a_composite_mode_for_the_wrong_platform(
    settings_db, tmp_path: Path,
) -> None:
    """The saved rule is checked against the mode, not its tool."""
    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.cso").write_bytes(b"\0" * 32)

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.cso",
        "fs_name": "Game.cso", "platform_slug": "ngc",
    }]

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        await romm_auto.set_rules({"7": {"mode": "cso_to_chd", "enabled": True}})
        result = await romm_auto.sweep(ignore_schedule=True, dry_run=True)

    assert result["queued"] == 0, result
    assert result["errors"] == [
        {"platform_id": 7, "error": "tool_wrong_for_platform"},
    ]


@pytest.mark.asyncio
async def test_changing_the_romm_instance_forgets_the_conversion_history(
    settings_db, tmp_path: Path,
) -> None:
    """A different RomM database reuses the same platform and ROM ids.

    Carried over, an `overwrite` rule would treat unrelated games in the new
    library as already converted and skip them permanently. The rules survive
    -- they are the operator's configuration -- but what they believe they
    produced does not.
    """
    from services.romm import auto as romm_auto

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path),
    })
    await romm_auto.set_rules({"7": {
        "mode": "dolphin_rvz", "enabled": True, "duplicate_action": "overwrite",
    }})
    await romm_auto._mark_converted(
        "7",
        romm_auto._converted_entries([(1, str(tmp_path / "Game.rvz"), "", None)]),
    )
    assert (await romm_auto.get_state())["7"]["converted"]

    await romm_routes.put_romm_settings(
        romm_routes.RommSettingsPatch(url="http://other-romm:8080"),
    )

    assert not (await romm_auto.get_state())["7"].get("converted")
    # The rule itself is untouched: a mistyped URL must not delete the config.
    assert (await romm_auto.get_rules())["7"]["mode"] == "dolphin_rvz"


@pytest.mark.asyncio
async def test_retargeting_a_rule_cannot_race_a_running_sweep(
    settings_db, tmp_path: Path,
) -> None:
    """`set_rules` clears history a sweep may still be about to write back."""
    from services.romm import auto as romm_auto

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path),
    })
    await romm_auto.set_rules({"7": {"mode": "dolphin_rvz", "enabled": True}})

    # Hold the sweep lock and confirm a save cannot slip past it.
    async with romm_auto._sweep_lock:
        saving = asyncio.create_task(
            romm_auto.set_rules({"7": {"mode": "dolphin_gcz", "enabled": True}}),
        )
        await asyncio.sleep(0)
        assert not saving.done(), "set_rules must serialise with sweeps"
    await saving
    assert (await romm_auto.get_rules())["7"]["mode"] == "dolphin_gcz"


@pytest.mark.asyncio
async def test_automation_repin_records_the_pre_conversion_fingerprint(
    settings_db, repin_db, tmp_path: Path,
) -> None:
    """The snapshot must describe the destination *before* the job ran.

    The sweep records after the queue accepts the batch, and on an idle queue
    a fast conversion can land in between. Stating the destination at that
    point would save the finished output as the "before" picture, after which
    the settler sees nothing change and abandons the row.
    """
    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    (lib / "Game.rvz").write_bytes(b"old")  # overwrite target, pre-existing

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    before = romm_repin.path_fingerprint(str(lib / "Game.rvz"))
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc", "igdb_id": 42,
    }]

    async def _fast_batch(paths, mode, **kwargs):
        # The conversion completes before `record()` gets its turn.
        (lib / "Game.rvz").write_bytes(b"brand new output")
        return _fake_jobs(paths)

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(romm_auto.job_manager, "create_batch_jobs", _fast_batch), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        await romm_auto.set_rules({"7": {
            "mode": "dolphin_rvz", "enabled": True, "duplicate_action": "overwrite",
        }})
        result = await romm_auto.sweep(ignore_schedule=True)

    assert result["repins_recorded"] == 1, result
    row = romm_repin.pending_rows(10)[0]
    assert row[6] == before, "recorded the post-conversion state as the pre-image"
    assert row[6] != romm_repin.path_fingerprint(str(lib / "Game.rvz"))


@pytest.mark.asyncio
async def test_a_failed_job_does_not_count_as_converted(
    settings_db, tmp_path: Path,
) -> None:
    """A changed destination is not proof of success.

    A failed or cancelled `overwrite` job can unlink the previous artifact or
    leave a partial one, which from the outside looks exactly like a fresh
    output — and the ROM would then be skipped forever. The job's own outcome
    decides.
    """
    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    (lib / "Game.rvz").write_bytes(b"previous")

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc",
    }]

    async def _fake_batch(paths, mode, **kwargs):
        return _fake_jobs(paths)

    failed = SimpleNamespace(id="job-1", status=JobStatus.FAILED)

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(romm_auto.job_manager, "create_batch_jobs", _fake_batch), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        await romm_auto.set_rules({"7": {
            "mode": "dolphin_rvz", "enabled": True, "duplicate_action": "overwrite",
        }})
        assert (await romm_auto.sweep(ignore_schedule=True))["queued"] == 1

        # The job died after mangling the destination — the fingerprint changed,
        # but nothing usable was produced.
        (lib / "Game.rvz").unlink()
        with patch.object(
            romm_auto.job_manager, "get_job", return_value=failed,
        ), patch.object(
            romm_auto.job_manager, "get_active_job_candidates", return_value=[],
        ):
            retried = await romm_auto.sweep(ignore_schedule=True)
        assert retried["queued"] == 1, retried

        # And a job that completed does stop the rule.
        done = SimpleNamespace(id="job-1", status=JobStatus.COMPLETED)
        (lib / "Game.rvz").write_bytes(b"the real output")
        with patch.object(
            romm_auto.job_manager, "get_job", return_value=done,
        ), patch.object(
            romm_auto.job_manager, "get_active_job_candidates", return_value=[],
        ):
            settled = await romm_auto.sweep(ignore_schedule=True)
        assert settled["queued"] == 0, settled


@pytest.mark.asyncio
async def test_a_pruned_failed_job_still_does_not_count_as_converted(
    settings_db, tmp_path: Path,
) -> None:
    """The outcome has to outlive the queue's memory of the job.

    Job history is capped and in-memory, so `get_job()` answers "unknown" after
    a prune or a restart — and the fingerprint fallback cannot tell a finished
    conversion from a failed overwrite that unlinked the old artifact. The
    verdict is written down when the job ends, and that record wins.
    """
    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    (lib / "Game.rvz").write_bytes(b"previous")

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc",
    }]

    async def _fake_batch(paths, mode, **kwargs):
        return [_FakeJob(p, "job-77") for p in paths]

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(romm_auto.job_manager, "create_batch_jobs", _fake_batch), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        await romm_auto.set_rules({"7": {
            "mode": "dolphin_rvz", "enabled": True, "duplicate_action": "overwrite",
        }})
        assert (await romm_auto.sweep(ignore_schedule=True))["queued"] == 1

        # The job fails and announces it; the queue then forgets the job
        # entirely, which is what a prune or a restart looks like from here.
        await romm_auto.note_job_finished(
            SimpleNamespace(id="job-77", status=JobStatus.FAILED),
        )
        (lib / "Game.rvz").unlink()
        with patch.object(
            romm_auto.job_manager, "get_job", return_value=None,
        ), patch.object(
            romm_auto.job_manager, "get_active_job_candidates", return_value=[],
        ):
            retried = await romm_auto.sweep(ignore_schedule=True)
        assert retried["queued"] == 1, retried


@pytest.mark.asyncio
async def test_a_completed_job_is_remembered_after_the_queue_forgets_it(
    settings_db, tmp_path: Path,
) -> None:
    """The other direction: a success recorded at the time is not re-run.

    Without the persisted verdict this leaned on the destination having
    changed, so an operator who moved the output aside got the whole platform
    reconverted.
    """
    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc",
    }]

    async def _fake_batch(paths, mode, **kwargs):
        return [_FakeJob(p, "job-78") for p in paths]

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(romm_auto.job_manager, "create_batch_jobs", _fake_batch), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        await romm_auto.set_rules({"7": {
            "mode": "dolphin_rvz", "enabled": True, "duplicate_action": "overwrite",
        }})
        assert (await romm_auto.sweep(ignore_schedule=True))["queued"] == 1
        await romm_auto.note_job_finished(
            SimpleNamespace(id="job-78", status=JobStatus.COMPLETED),
        )
        # The output is produced and then moved away by hand; the queue has
        # forgotten the job. The record still says it happened.
        with patch.object(
            romm_auto.job_manager, "get_job", return_value=None,
        ), patch.object(
            romm_auto.job_manager, "get_active_job_candidates", return_value=[],
        ):
            again = await romm_auto.sweep(ignore_schedule=True)
        assert again["queued"] == 0, again


@pytest.mark.asyncio
async def test_the_sweep_writes_down_an_outcome_the_queue_can_still_answer_for(
    sqlite_db,
) -> None:
    """The backstop for a listener that never fired.

    A job that finished before its record was written — a fast conversion on an
    idle queue — is not covered by the completion listener, so the sweep asks
    the queue for anything it does not yet know and freezes the answer.
    """
    from services.romm import auto as romm_auto

    record = {"path": "/x/Game.rvz", "pre": "1:2", "job_id": "job-9"}
    await romm_auto.preferences_store.put(romm_auto.STATE_KEY, {
        "7": {"converted": {"1": dict(record)}},
    })
    with patch.object(
        romm_auto.job_manager, "get_job",
        return_value=SimpleNamespace(id="job-9", status=JobStatus.FAILED),
    ):
        returned = await romm_auto._persist_known_outcomes("7", {"1": dict(record)})

    assert returned["1"]["done"] is False
    state = await romm_auto.get_state()
    assert state["7"]["converted"]["1"]["done"] is False


@pytest.mark.asyncio
async def test_a_hung_catalog_scan_answers_504_instead_of_hanging(
    settings_db, tmp_path: Path, monkeypatch,
) -> None:
    """`run_detached` is unbounded by design; the deadline is the caller's job.

    Without one the 504 below was unreachable: a mount that stops answering
    mid-scan left the request — and the spinner behind it — waiting forever,
    which is exactly the failure the detached thread was supposed to contain.
    """
    import asyncio as _asyncio

    from fastapi import HTTPException

    (tmp_path / "library").mkdir()
    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc",
    }]

    async def _never_returns(*_args, **_kwargs):
        await _asyncio.sleep(3600)

    monkeypatch.setattr(romm_routes, "run_detached", _never_returns)
    monkeypatch.setattr(romm_routes, "_CATALOG_SCAN_BASE_S", 0.05)
    monkeypatch.setattr(romm_routes, "_CATALOG_SCAN_PER_ROM_S", 0)

    # The outer wait_for is the test's own guard: without the deadline under
    # test this call never returns, and a hanging test says much less than a
    # failing one.
    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            pytest.raises(HTTPException) as excinfo:
        await _asyncio.wait_for(romm_routes.romm_roms(platform_id=7), 10)
    assert excinfo.value.status_code == 504


@pytest.mark.asyncio
async def test_the_settle_pass_bounds_the_hash_by_its_own_budget() -> None:
    """A request-driven pass must not hold the lock for the length of a hash.

    The pass advertises a 120-second budget but only checked it between rows,
    while one row could wait unboundedly for the heavy-IO lane and then hash
    for 300 seconds or more. The budget now reaches the hash itself; the
    background settler is what still gives large outputs their full timeout.
    """
    import asyncio as _asyncio
    import time

    seen = {}

    async def _slow_hash(*_args, **_kwargs):
        await _asyncio.sleep(3600)

    async def _record_timeout(coro, timeout):
        seen["timeout"] = timeout
        coro.close()
        raise _asyncio.TimeoutError

    with patch.object(romm_routes, "bounded_path_check", return_value=10 * 1024**3), \
            patch.object(romm_routes, "run_detached", _slow_hash), \
            patch.object(romm_routes.asyncio, "wait_for", _record_timeout):
        # A 10 GB output would otherwise ask for ~5000 seconds; the pass has
        # two left.
        deadline = time.monotonic() + 2
        assert await romm_routes._hash_output("/x/Game.rvz", deadline) is None
    assert seen["timeout"] <= 2, seen

    # With no deadline (the background settler) the file's own budget stands.
    with patch.object(romm_routes, "bounded_path_check", return_value=10 * 1024**3), \
            patch.object(romm_routes, "run_detached", _slow_hash), \
            patch.object(romm_routes.asyncio, "wait_for", _record_timeout):
        assert await romm_routes._hash_output("/x/Game.rvz") is None
    assert seen["timeout"] > romm_routes._HASH_TIMEOUT_FLOOR_S, seen


@pytest.mark.asyncio
async def test_two_sources_for_one_output_record_the_source_the_queue_keeps(
    settings_db, tmp_path: Path,
) -> None:
    """The batch collapses them into one job; the snapshot must agree.

    Two selected ROMs can resolve to one destination — repeated `Game.iso`
    names aimed at a single output folder, or a `.cue` beside its `.bin`.
    `/jobs/batch` keeps one source; recording per source in submission order
    left the row holding whichever came *last*, so the conversion that actually
    ran could be re-pinned with the identity of the ROM the queue skipped.
    """
    from services.romm import repin as romm_repin
    from services.output_conflicts import collapse_to_winners, input_priority

    # The shared rule the batch route uses: the disc description outranks the
    # data track, and equal claims keep the first submitted.
    assert input_priority("/x/Game.cue") > input_priority("/x/Game.bin")
    assert collapse_to_winners({
        "/x/Game.bin": "/out/Game.chd", "/x/Game.cue": "/out/Game.chd",
    }) == {"/x/Game.cue": "/out/Game.chd"}

    lib = tmp_path / "library"
    (lib / "a").mkdir(parents=True)
    (lib / "b").mkdir(parents=True)
    (lib / "a" / "Game.iso").write_bytes(b"\0" * 32)
    (lib / "b" / "Game.iso").write_bytes(b"\0" * 32)
    out = tmp_path / "out"
    out.mkdir()

    await settings_db.save({"url": "http://romm:8080", "library_root": str(lib)})
    roms = [
        {"id": 1, "name": "Game A", "full_path": "a/Game.iso",
         "fs_name": "Game.iso", "platform_slug": "ngc", "igdb_id": 11},
        {"id": 2, "name": "Game B", "full_path": "b/Game.iso",
         "fs_name": "Game.iso", "platform_slug": "ngc", "igdb_id": 22},
    ]

    payload = romm_routes.RepinPlanRequest(
        paths=[str(lib / "a" / "Game.iso"), str(lib / "b" / "Game.iso")],
        mode="dolphin_rvz", platform_id=7, output_dir=str(out),
        duplicate_action="overwrite",
    )
    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(romm_routes, "is_within_configured_volumes", return_value=True), \
            patch.object(
                romm_routes, "_destination_has_pending_job", return_value=False,
            ):
        result = await romm_routes.romm_repin_plan(payload)

    # One destination, one row — and the second source counted as skipped
    # rather than silently overwriting the first one's snapshot.
    assert result["recorded"] == 1, result
    assert result["skipped"] == 1, result
    rows = romm_repin.pending_rows(10)
    assert len(rows) == 1, rows
    assert rows[0][3] == {"igdb_id": 11}, rows[0]


def test_a_claim_its_holder_never_released_is_taken_back(sqlite_db, tmp_path: Path) -> None:
    """A process that dies mid-write must not park the row forever.

    `pending_rows` hands a stale claim back out, so refusing to re-claim it
    meant every later pass fetched that row and failed on it — and
    `count_pending` hid it from the badge and from the background settler, so
    nothing ever restored its metadata.
    """
    from services.romm import repin as romm_repin

    out = tmp_path / "Game.rvz"
    romm_repin.record({"id": 5, "igdb_id": 42}, str(out), {"igdb_id": 42})
    row_id = romm_repin.pending_rows(10)[0][5]
    assert romm_repin.claim(row_id) is True

    # Fresh claim: invisible to another pass, but still counted as outstanding.
    assert [r for r in romm_repin.pending_rows(10) if r[5] == row_id] == []
    assert romm_repin.count_pending() == 1

    # Aged past the stale window, it comes back and can be taken again.
    with patch.object(
        romm_repin, "_iso_seconds_ago", return_value="2999-01-01T00:00:00Z",
    ):
        assert [r[5] for r in romm_repin.pending_rows(10)] == [row_id]
        assert romm_repin.claim(row_id) is True


def test_claiming_a_row_is_atomic(sqlite_db, tmp_path: Path) -> None:
    """Whoever claims the row is the only one that may write to RomM.

    The previous shape was a read (`is_pending`) followed by a write, and the
    window between them is exactly where a re-plan lands: the row is
    superseded, and the pass — already past its check — pushes the previous
    generation's provider ids anyway.
    """
    from services.romm import repin as romm_repin

    out = tmp_path / "Game.rvz"
    romm_repin.record({"id": 5, "igdb_id": 42}, str(out), {"igdb_id": 42})
    row_id = romm_repin.pending_rows(10)[0][5]

    # Two passes racing for the same row: exactly one may win. Run them in
    # threads, because a read-then-write implementation passes a sequential
    # check and fails this one.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: romm_repin.claim(row_id), range(8)))
    assert results.count(True) == 1, results

    # And a re-plan cannot supersede a row that is already being applied.
    assert romm_repin.claim(row_id) is False
    romm_repin.record({"id": 6, "igdb_id": 43}, str(out), {"igdb_id": 43})
    assert romm_repin.claim(row_id) is False

    # The claimed row still settles — claiming is how it stopped being
    # superseded — and the re-planned row is untouched by that.
    assert romm_repin.settle(row_id, "done", None, 99) is True
    fresh = [r for r in romm_repin.pending_rows(10) if r[5] != row_id]
    assert len(fresh) == 1, fresh

    # And a write that never happened hands the row back rather than parking it.
    second_id = fresh[0][5]
    assert romm_repin.claim(second_id) is True
    assert romm_repin.release(second_id) is True
    assert romm_repin.claim(second_id) is True


def test_cancelling_a_plan_cannot_retire_the_row_that_superseded_it(
    sqlite_db, tmp_path: Path,
) -> None:
    """Plan-then-cancel is two requests, and another client fits between them.

    Cancelling by *destination* retired whatever pending row that path held.
    `record()` supersedes, so a second client planning the same output owns the
    row by then — and its conversion, already queued, would run with no
    snapshot at all because the first client tidied up after itself by path.
    """
    from services.romm import repin as romm_repin

    out = tmp_path / "Game.rvz"
    mine = romm_repin.record({"id": 5, "igdb_id": 42}, str(out), {"igdb_id": 42})
    theirs = romm_repin.record({"id": 6, "igdb_id": 43}, str(out), {"igdb_id": 43})
    assert theirs != mine

    # My submit failed, so I retire what I recorded. Their row is not mine.
    assert romm_repin.cancel([mine]) == 0
    live = romm_repin.pending_rows(10)
    assert [row[5] for row in live] == [theirs]
    assert live[0][3] == {"igdb_id": 43}, live

    # And my own live row still cancels, or the endpoint would do nothing.
    assert romm_repin.cancel([theirs]) == 1
    assert romm_repin.count_pending() == 0


@pytest.mark.asyncio
async def test_the_identity_comparison_cannot_hang_the_settings_save() -> None:
    """The dead mount is usually the one being replaced.

    Comparing identity resolves both library roots, and the *old* root is the
    unresponsive share as often as not — that is why the operator is here
    changing it. On a pooled worker with no deadline, `realpath` blocks forever
    while this request holds `_settle_lock` and the sweep pause, so the one
    request that would have fixed the mount is also the one that wedges every
    re-pin pass, sweep and rule edit behind it.
    """
    import threading

    stuck = threading.Event()
    real = os.path.realpath
    loop_thread = threading.get_ident()

    def _realpath(path):
        assert threading.get_ident() != loop_thread, (
            f"realpath({path}) ran on the event loop"
        )
        if "/dead-mount/" in str(path):
            stuck.wait(30)
        return real(path)

    ticks = 0

    async def _tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(_tick())
    try:
        with patch.object(romm_routes.os.path, "realpath", _realpath), \
                patch.object(romm_routes, "_IDENTITY_PROBE_SECONDS", 0.2):
            moved = await asyncio.wait_for(
                romm_routes._identity_moved(
                    {"url": "http://romm:8080", "library_root": "/dead-mount/old"},
                    {"url": "http://romm:8080", "library_root": "/dead-mount/new"},
                ),
                timeout=5,
            )
    finally:
        stuck.set()
        ticker.cancel()

    # It answered, from the spellings, and the loop kept running throughout.
    assert moved is True
    assert ticks > 0

    # And the safe degradation is "moved": two spellings of one directory cost
    # a redone conversion history, where the opposite mistake writes one
    # library's provider ids onto another library's game.
    with patch.object(romm_routes, "_IDENTITY_PROBE_SECONDS", 0.2):
        same = await romm_routes._identity_moved(
            {"url": "http://romm:8080/", "library_root": "/tmp"},
            {"url": "http://romm:8080", "library_root": "/tmp/"},
        )
    assert same is False


@pytest.mark.asyncio
async def test_saving_rules_never_validates_volumes_on_the_event_loop(
    settings_db,
) -> None:
    """The rescue edit waits behind the check it is trying to undo.

    Validating an output directory resolves it and stats every configured
    volume. `set_rules` runs inside `_sweep_lock`, so on an unresponsive mount
    that check freezes the API *and* everything queued behind the lock —
    including the request to disable the rule naming the bad path.
    """
    import threading

    from services.romm import auto as romm_auto

    stuck = threading.Event()
    loop_thread = threading.get_ident()

    def _within(path):
        assert threading.get_ident() != loop_thread, (
            f"is_within_configured_volumes({path}) ran on the event loop"
        )
        stuck.wait(30)
        return True

    ticks = 0

    async def _tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(_tick())
    try:
        with patch.object(romm_auto, "is_within_configured_volumes", _within), \
                patch.object(romm_auto, "_VOLUME_PROBE_SECONDS", 0.2):
            # The real entry point, so this covers the wiring and not just the
            # helper: `set_rules` is what holds `_sweep_lock`.
            rules = await asyncio.wait_for(
                romm_auto.set_rules({
                    "7": {"mode": "dolphin_rvz", "enabled": True,
                          "output_dir": "/dead-mount/out"},
                }),
                timeout=5,
            )
    finally:
        stuck.set()
        ticker.cancel()

    assert ticks > 0
    # Paused, with the path recorded — never accepted unchecked, and never
    # silently rewritten to "beside the source", which would fill a filesystem
    # the operator did not choose.
    assert rules["7"]["enabled"] is False, rules
    assert rules["7"]["output_dir"] is None, rules
    assert rules["7"]["invalid_output_dir"] == "/dead-mount/out", rules


@pytest.mark.asyncio
async def test_planning_a_batch_keeps_the_paths_that_answered(
    repin_db, tmp_path: Path,
) -> None:
    """A submit against a dying library must not hold the request open.

    Resolving each submitted path stats every component and every configured
    volume. On a pooled worker with no deadline the conversion request never
    reached queueing, and repeated submissions stranded one shared worker each
    until nothing else in the process could offload anything.
    """
    import threading

    lib = tmp_path / "roms" / "gc"
    lib.mkdir(parents=True)
    for name in ("Good.iso", "Slow.iso"):
        (lib / name).write_bytes(b"\0" * 32)
    good, slow = str(lib / "Good.iso"), str(lib / "Slow.iso")
    rom = {"id": 7, "name": "Good", "igdb_id": 42}

    stuck = threading.Event()
    loop_thread = threading.get_ident()

    def _within(path):
        if path == slow:
            assert threading.get_ident() != loop_thread, (
                "the volume check ran on the event loop"
            )
            stuck.wait(30)
        return True

    ticks = 0

    async def _tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(_tick())
    try:
        with patch.object(RommClient, "base_url", "http://romm:8080"), \
                patch.object(RommClient, "library_root", str(tmp_path)), \
                patch.object(
                    romm_routes.romm_repin, "roms_by_local_path",
                    return_value={os.path.realpath(good): rom},
                ), \
                patch.object(romm_routes, "is_within_configured_volumes", _within), \
                patch.object(romm_routes, "_PLAN_BASE_S", 0.2), \
                patch.object(romm_routes, "_PLAN_PER_PATH_S", 0), \
                patch.object(
                    romm_routes, "_plan_deadline", lambda _n: 0.2,
                ):
            result = await asyncio.wait_for(
                romm_routes.romm_repin_plan(
                    romm_routes.RepinPlanRequest(
                        # Good first: the resolution walks the list, so this
                        # is the path that answers before the mount stops
                        # answering — and what a bound that expires part-way
                        # must keep rather than discard along with the rest.
                        paths=[good, slow], mode="dolphin_rvz",
                    ),
                ),
                timeout=5,
            )
    finally:
        stuck.set()
        ticker.cancel()

    assert ticks > 0
    # The path that answered is recorded; the one that did not is skipped,
    # which is the same answer an out-of-volume path gets — and what an
    # unreachable path effectively is.
    assert result["recorded"] == 1, result
    assert list(result["recorded_paths"]) == [good], result


@pytest.mark.asyncio
async def test_a_dead_output_dir_stops_one_platform_not_the_whole_sweep(
    settings_db, tmp_path: Path,
) -> None:
    """The saved directory can go unresponsive long after it was validated.

    That check ran on a pooled worker with no deadline while the sweep held
    `_sweep_lock`, so it stranded a shared worker and blocked previews, manual
    runs, rule edits and settings saves until a restart.
    """
    import threading

    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc",
    }]

    stuck = threading.Event()
    loop_thread = threading.get_ident()

    def _within(path):
        if "/dead-mount/" in str(path):
            assert threading.get_ident() != loop_thread, (
                f"is_within_configured_volumes({path}) ran on the event loop"
            )
            stuck.wait(30)
        return True

    ticks = 0

    async def _tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(_tick())
    try:
        # Stored with a directory that was valid when it was saved and has
        # since died — written straight to the blob, which is also how a
        # hand-edited database reaches the sweep. `get_rules` reads it back
        # without re-validating, by design, so the sweep is the check.
        await romm_auto.preferences_store.put(romm_auto.RULES_KEY, {
            "7": romm_auto.normalize_rule(
                {"mode": "dolphin_rvz", "enabled": True,
                 "output_dir": "/dead-mount/out"},
                check_volumes=False,
            ),
        })
        with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
                patch.object(
                    romm_auto.job_manager, "get_active_job_candidates", return_value=[],
                ), \
                patch.object(romm_auto, "is_within_configured_volumes", _within), \
                patch.object(romm_auto, "_VOLUME_PROBE_SECONDS", 0.2):
            result = await asyncio.wait_for(
                romm_auto.sweep(ignore_schedule=True, dry_run=True), timeout=10,
            )
    finally:
        stuck.set()
        ticker.cancel()

    assert ticks > 0
    assert result["queued"] == 0, result
    assert result["errors"] == [
        {"platform_id": 7, "error": "output_dir_outside_volumes"},
    ], result


@pytest.mark.asyncio
async def test_a_finished_job_updates_one_record_not_the_whole_history(
    settings_db, tmp_path: Path,
) -> None:
    """Every completion scanned every remembered ROM of every platform.

    The listener fires for *every* job in the app, most of them manual, and it
    ran that scan on the event loop while holding `_sweep_lock` — so a batch of
    completions against a large history delayed every unrelated request.
    """
    from services.romm import auto as romm_auto

    class _Job:
        def __init__(self, job_id, status):
            self.id = job_id
            self.status = status

    # A history big enough that a scan is obvious, and one real record in it.
    big = {
        str(pid): {"converted": {
            str(rid): {"path": f"/l/{pid}/{rid}.rvz", "pre": "", "job_id": f"j{pid}-{rid}"}
            for rid in range(50)
        }}
        for pid in range(20)
    }
    await romm_auto.preferences_store.put(romm_auto.STATE_KEY, big)

    reads = 0
    real_get_state = romm_auto.get_state

    async def _counted_get_state():
        nonlocal reads
        reads += 1
        return await real_get_state()

    # A job this process never queued: nothing to write, and nothing to read.
    with patch.object(romm_auto, "get_state", _counted_get_state):
        await romm_auto.note_job_finished(_Job("not-ours", JobStatus.COMPLETED))
    assert reads == 0, "a manual job made the listener read the whole history"

    # One this process did queue, indexed when it was marked.
    await romm_auto._mark_converted(
        "3", romm_auto._converted_entries([(7, "/l/3/7.rvz", "", "job-7")]),
    )
    await romm_auto.note_job_finished(_Job("job-7", JobStatus.FAILED))
    state = await romm_auto.get_state()
    assert state["3"]["converted"]["7"]["done"] is False, state["3"]["converted"]["7"]

    # And only that record moved.
    assert "done" not in state["4"]["converted"]["7"], state["4"]["converted"]["7"]

    # The index entry is consumed, so a repeated announcement is a no-op and
    # the map cannot grow without bound.
    assert "job-7" not in romm_auto._job_owners


@pytest.mark.asyncio
async def test_a_verdict_that_arrives_before_its_record_is_not_lost(
    settings_db, tmp_path: Path,
) -> None:
    """Ownership is registered at acceptance; the record lands a few awaits on.

    The listener fires from the queue worker, so a fast conversion on an idle
    queue can end while the post-queue bookkeeping is still running. It finds
    its owner and no record to write to — and dropping the answer there leaves
    the row with no verdict at all, after which the only evidence is the
    destination having changed, which a *failed* overwrite produces just as
    convincingly as a real conversion.
    """
    from services.romm import auto as romm_auto

    class _Job:
        def __init__(self, job_id, status):
            self.id = job_id
            self.status = status

    entries = romm_auto._converted_entries([(11, "/l/7/11.rvz", "", "job-fast")])
    # Acceptance: owner registered, record not written yet.
    romm_auto._own_jobs("7", entries)

    # The conversion fails before `_mark_converted` gets to run.
    await romm_auto.note_job_finished(_Job("job-fast", JobStatus.FAILED))

    # ...and the record, when it lands, carries the verdict anyway.
    await romm_auto._mark_converted("7", entries)
    state = await romm_auto.get_state()
    assert state["7"]["converted"]["11"]["done"] is False, state["7"]["converted"]

    # The stash is drained, so it cannot grow without bound.
    assert "job-fast" not in romm_auto._late_verdicts


@pytest.mark.asyncio
async def test_an_identity_change_leaves_a_marker_until_its_cleanup_runs(
    settings_db, tmp_path: Path,
) -> None:
    """Save and cleanup are two operations; either can be the one that survives.

    Cleanup-then-save leaves the old identity live with its history gone and
    its snapshots retired. Save-then-cleanup leaves the retry comparing the new
    values with themselves, so the previous instance's ids stay live against
    the new one forever. The marker rides in the same row as the new identity,
    so whichever half is interrupted, the other replays.
    """
    from services.romm import repin as romm_repin, settings as romm_settings

    romm_repin.record({"id": 5, "igdb_id": 42}, str(tmp_path / "A.rvz"), {"igdb_id": 42})
    assert romm_repin.count_pending() == 1

    # The save half lands, the cleanup half does not.
    await romm_settings.save(
        {"url": "http://elsewhere:8080", "library_root": str(tmp_path)},
        cleanup_pending=True,
    )
    assert romm_settings.cleanup_owed() is True
    # The new identity is in force, so a naive retry would see nothing moved.
    assert romm_settings.effective()["url"] == "http://elsewhere:8080"

    # Startup finishes what the interrupted request owed.
    await romm_routes.replay_identity_cleanup()
    assert romm_repin.count_pending() == 0
    assert romm_settings.cleanup_owed() is False

    # Idempotent, and it does not run again once the marker is cleared.
    romm_repin.record({"id": 6, "igdb_id": 43}, str(tmp_path / "B.rvz"), {"igdb_id": 43})
    await romm_routes.replay_identity_cleanup()
    assert romm_repin.count_pending() == 1


@pytest.mark.asyncio
async def test_a_plan_refuses_to_record_across_an_identity_change(
    settings_db, tmp_path: Path,
) -> None:
    """The plan holds neither the settle lock nor the sweep pause.

    It reads the catalog, then writes rows carrying that catalog's provider
    ids, with awaits in between. A change landing in that window retires the
    existing rows and installs the new instance — and the plan would then
    insert the *old* instance's ids into a fresh row the cleanup never saw, so
    the conversion is re-pinned as a game from a library it does not belong to.
    """
    from fastapi import HTTPException

    from services.romm import settings as romm_settings

    lib = tmp_path / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    rom = {"id": 7, "name": "Game", "igdb_id": 42}

    def _roms_and_swap(*_args, **_kwargs):
        # The identity moves while the catalog is being read.
        asyncio.run(romm_settings.save(
            {"url": "http://elsewhere:8080"}, cleanup_pending=True,
        ))
        return {os.path.realpath(lib / "Game.iso"): rom}

    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(tmp_path)), \
            patch.object(
                romm_routes.romm_repin, "roms_by_local_path", _roms_and_swap,
            ), \
            patch.object(
                romm_routes, "is_within_configured_volumes", return_value=True,
            ), \
            pytest.raises(HTTPException) as caught:
        await romm_routes.romm_repin_plan(
            romm_routes.RepinPlanRequest(
                paths=[str(lib / "Game.iso")], mode="dolphin_rvz",
            ),
        )

    assert caught.value.status_code == 409
    # Nothing recorded: a batch converted without a snapshot needs a manual
    # re-match, which is recoverable; a row holding another library's ids is not.
    assert romm_routes.romm_repin.count_pending() == 0


def test_a_filter_pattern_too_long_to_store_is_refused_not_trimmed() -> None:
    """A prefix of a regex is usually a valid regex that means something else.

    Cutting `^(Alpha|Beta|...)` mid-alternation can leave something that
    compiles and matches a different set. An *include* trimmed that way widens
    the selection; an *exclude* trimmed that way stops protecting the titles
    the operator meant to skip — unattended, with delete-on-verify possibly
    attached. Refusing pauses the rule and says so, which is the whole contract
    of this validator.
    """
    from services.romm import auto as romm_auto

    # A bare alternation of literals: every prefix of it is still a valid
    # regex, which is exactly what makes trimming dangerous rather than noisy.
    overlong = "|".join(f"Game{i:03d}" for i in range(80))
    assert len(overlong) > romm_auto._MAX_PATTERN
    trimmed = overlong[:romm_auto._MAX_PATTERN]
    re.compile(trimmed)  # the premise: the trimmed form compiles...
    assert trimmed != overlong
    # ...and means something else. The cut lands mid-name, leaving a fragment
    # that matches titles the operator never listed: as an include, that queues
    # ROMs they excluded; as an exclude, it silently skips ROMs they wanted.
    assert re.search(overlong, "Game999") is None
    assert re.search(trimmed, "Game999") is not None

    pattern, invalid = romm_auto._valid_pattern(overlong)
    assert pattern is None and invalid is True, pattern

    # And the rule carrying it is paused rather than run with a filter nobody
    # wrote, which is what an invalid pattern means everywhere else here.
    rule = romm_auto.normalize_rule({
        "mode": "dolphin_rvz", "enabled": True, "exclude_pattern": overlong,
    })
    assert rule["invalid_pattern"] is True
    assert rule["exclude_pattern"] is None

    # A pattern at the limit is still accepted: this refuses the overlong ones,
    # it does not tighten the limit.
    at_limit = "a" * romm_auto._MAX_PATTERN
    assert romm_auto._valid_pattern(at_limit) == (at_limit, False)


def test_an_identity_swap_retires_a_claim_its_holder_never_released(
    sqlite_db, tmp_path: Path,
) -> None:
    """A stale claim is re-issued later, so leaving one behind is not neutral.

    Pointing at a different RomM instance (or library root) retires the rows
    that hold the old instance's provider ids. A row whose holder died
    mid-write is `settling`, not `pending` — and both `pending_rows` and
    `claim` deliberately hand a stale claim back out — so one left behind here
    is picked up 15 minutes later and stamps the old library's identity onto
    whatever the new one matches its digest to. Which is the one outcome this
    cleanup exists to prevent.
    """
    from services.romm import repin as romm_repin

    kept = romm_repin.record(
        {"id": 5, "igdb_id": 42}, str(tmp_path / "A.rvz"), {"igdb_id": 42},
    )
    claimed = romm_repin.record(
        {"id": 6, "igdb_id": 43}, str(tmp_path / "B.rvz"), {"igdb_id": 43},
    )
    assert romm_repin.claim(claimed) is True
    assert romm_repin.count_pending() == 2

    assert romm_repin.retire_all_pending("The RomM instance changed") == 2
    assert romm_repin.count_pending() == 0

    # Neither comes back, however long the claim has been outstanding.
    with patch.object(
        romm_repin, "_iso_seconds_ago", return_value="2999-01-01T00:00:00Z",
    ):
        assert romm_repin.pending_rows(10) == []
        assert romm_repin.claim(claimed) is False
        assert romm_repin.claim(kept) is False


def test_releasing_a_superseded_claim_retires_it_instead_of_restoring_it(
    sqlite_db, tmp_path: Path,
) -> None:
    """`pending` is a unique slot per output, and a re-plan can already hold it.

    A claimed row is `settling`, which the partial unique index does not see,
    so `record()` inserts a second row for the same output. Restoring the claim
    to `pending` then violates `ux_romm_repin_pending_output` — inside the
    failure handler that called release, replacing the real error with a 500
    and parking the claim as `settling` until it aged out.
    """
    from services.romm import repin as romm_repin

    out = tmp_path / "Game.rvz"
    mine = romm_repin.record({"id": 5, "igdb_id": 42}, str(out), {"igdb_id": 42})
    assert romm_repin.claim(mine) is True
    theirs = romm_repin.record({"id": 6, "igdb_id": 43}, str(out), {"igdb_id": 43})

    # The RomM write failed, so the claim goes back — and finds its slot taken.
    assert romm_repin.release(mine) is False
    # No exception, no stranded claim, and the newer row is untouched: it is
    # the one describing the conversion that is actually going to happen.
    assert [row[5] for row in romm_repin.pending_rows(10)] == [theirs]
    assert romm_repin.count_pending() == 1
    assert romm_repin.claim(mine) is False

    # With nothing else holding the slot, release still hands the row back.
    assert romm_repin.claim(theirs) is True
    assert romm_repin.release(theirs) is True
    assert romm_repin.claim(theirs) is True


def test_a_split_build_is_verified_where_it_actually_landed(tmp_path: Path) -> None:
    """A `-s` build past 4 GB writes parts, not the planned bare ISO.

    Verifying the path the job planned therefore read a file that was never
    created, and failed a conversion that had worked — which an overwrite rule
    then repeated, and failed, on every later sweep.
    """
    from services.tools import registry

    tool = registry.for_mode("folder_to_iso")
    planned = tmp_path / "Game.iso"

    # Split: only the numbered parts exist.
    (tmp_path / "Game.iso.0").write_bytes(b"a")
    (tmp_path / "Game.iso.1").write_bytes(b"b")
    assert tool.verify_target(str(planned), "folder_to_iso") == str(
        tmp_path / "Game.iso.0",
    )

    # Unsplit: the planned path is the product, as for every other mode.
    planned.write_bytes(b"whole")
    assert tool.verify_target(str(planned), "folder_to_iso") == str(planned)

    # And nothing at all is an honest None rather than a path to a missing file.
    for leftover in tmp_path.iterdir():
        leftover.unlink()
    assert tool.verify_target(str(planned), "folder_to_iso") is None

    # Every other tool keeps the default: what the job planned.
    chdman = registry.for_mode("createcd")
    assert chdman.verify_target("/x/Game.chd", "createcd") == "/x/Game.chd"


@pytest.mark.asyncio
async def test_an_unreadable_destination_leaves_an_overwrite_row_waiting(
    sqlite_db, tmp_path: Path,
) -> None:
    """"The stat did not answer" is not "the file changed".

    Reading a timeout as a change sends the row on to hash whatever is at the
    destination — under `overwrite` that is the artifact the conversion was
    going to replace — and stamps this ROM's ids onto it.
    """
    from services.romm import repin as romm_repin

    out = tmp_path / "Game.rvz"
    out.write_bytes(b"the previous artifact")
    romm_repin.record(
        {"id": 5, "igdb_id": 42}, str(out), {"igdb_id": 42}, "dolphin_rvz",
        romm_repin.path_fingerprint(str(out)),
    )
    row = romm_repin.pending_rows(10)[0]

    async def _no_answer(func, *args, **kwargs):
        if func is romm_repin.path_fingerprint:
            raise asyncio.TimeoutError
        return func(*args, **kwargs)

    hasher = AsyncMock()
    with patch.object(romm_routes, "bounded_path_check", _no_answer), \
            patch.object(romm_routes, "_hash_output", hasher):
        outcome = await romm_routes._settle_one_repin(row)

    assert outcome is romm_routes._Outcome.WAITING
    hasher.assert_not_awaited()


def test_verify_is_offered_where_deleting_is_refused() -> None:
    """Two capabilities, not one.

    makeps3iso reads PARAM.SFO back out of the ISO it built, but refuses to
    delete the curated game folder that produced it. Gating "verify each
    converted file" on the delete flag therefore made the only check that mode
    offers unreachable — for the one conversion whose source is irreplaceable.
    """
    from services.romm import auto as romm_auto
    from services.tools import registry

    assert registry.mode_supports_verify("folder_to_iso") is True
    assert registry.spec("folder_to_iso").supports_delete_on_verify is False

    rule = romm_auto.normalize_rule({
        "mode": "folder_to_iso", "enabled": True, "verify_after": True,
        "delete_on_verify": True,
    })
    assert rule["verify_after"] is True
    # And deleting is still refused, which is the half that must not move.
    assert rule["delete_on_verify"] is False

    # A mode that offers neither keeps both off: chdman's extractcd produces a
    # cue/bin pair the tool does not verify.
    plain = romm_auto.normalize_rule({
        "mode": "extractcd", "enabled": True,
        "verify_after": True, "delete_on_verify": True,
    })
    assert registry.mode_supports_verify("extractcd") is False
    assert plain["verify_after"] is False
    assert plain["delete_on_verify"] is False


@pytest.mark.asyncio
async def test_a_failed_repin_write_still_records_what_was_queued(
    settings_db, tmp_path: Path,
) -> None:
    """Post-queue bookkeeping must not be reported as a failed queue.

    The jobs are already accepted and running by then. Treating a locked
    database as "the batch failed" skipped the production record, so the next
    sweep queued every one of those ROMs a second time — with `overwrite`,
    rewriting the files the first batch was still producing.
    """
    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc", "igdb_id": 42,
    }]

    async def _fake_batch(paths, mode, **kwargs):
        return _fake_jobs(paths)

    recorder = MagicMock(side_effect=RuntimeError("database is locked"))

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(romm_auto.job_manager, "create_batch_jobs", _fake_batch), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True), \
            patch.object(romm_auto.romm_repin, "record", recorder):
        await romm_auto.set_rules({"7": {
            "mode": "dolphin_rvz", "enabled": True, "duplicate_action": "overwrite",
        }})
        first = await romm_auto.sweep(ignore_schedule=True)

    assert first["queued"] == 1, first
    # The failing write really was reached, or this proves nothing.
    assert recorder.called
    # Not reported as a queue failure — the jobs went in.
    assert first["errors"] == [], first
    # And the production record exists, so the next sweep leaves it alone —
    # which is the point: without it the same ROMs are converted twice.
    state = await romm_auto.get_state()
    assert "1" in state["7"]["converted"], state

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(romm_auto.job_manager, "create_batch_jobs", _fake_batch), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True), \
            patch.object(
                romm_auto.job_manager, "get_job",
                return_value=SimpleNamespace(id="x", status=JobStatus.COMPLETED),
            ):
        second = await romm_auto.sweep(ignore_schedule=True)
    assert second["queued"] == 0, second


def test_half_a_schedule_window_pauses_the_rule() -> None:
    """One endpoint filled in must not mean "no time restriction".

    An operator narrowing a rule to 22:00–04:00 types the start first. Reading
    that as "any time on the selected days" started unattended conversions —
    delete-on-verify included — in the middle of the working day, at the moment
    they were trying to restrict them.
    """
    from datetime import datetime, timezone

    from services.romm import auto as romm_auto

    half = romm_auto.normalize_rule({
        "mode": "dolphin_rvz", "enabled": True, "window_start": "22:00",
    })
    assert half["invalid_window"] is True
    assert half["enabled"] is False
    # And the clock refuses it even if the blob is edited straight in the
    # database, where normalization never ran.
    forced = dict(half, enabled=True)
    assert romm_auto._in_window(
        forced, datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc),
    ) is False

    # A complete window is untouched, and so is a rule with no window at all.
    whole = romm_auto.normalize_rule({
        "mode": "dolphin_rvz", "enabled": True,
        "window_start": "22:00", "window_end": "04:00",
    })
    assert whole["invalid_window"] is False
    assert whole["enabled"] is True
    always = romm_auto.normalize_rule({"mode": "dolphin_rvz", "enabled": True})
    assert always["invalid_window"] is False
    assert always["enabled"] is True


@pytest.mark.asyncio
async def test_a_hung_candidate_probe_stops_the_platform(
    settings_db, tmp_path: Path, monkeypatch,
) -> None:
    """One unresponsive ROM must not hold `_sweep_lock` forever.

    The probe resolves a path, checks containment and stats the destination —
    all on the remote mounts this integration exists for. In the shared pool
    with no deadline, a mount that stops answering strands a worker per
    candidate and blocks previews, manual runs, and the rule edit that would
    switch the platform off, until a restart.
    """
    import asyncio as _asyncio

    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc",
    }]

    async def _never_returns(*_args, **_kwargs):
        await _asyncio.sleep(3600)

    monkeypatch.setattr(romm_auto, "run_detached", _never_returns)
    monkeypatch.setattr(romm_auto, "_CANDIDATE_PROBE_SECONDS", 0.05)

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ):
        await romm_auto.set_rules({"7": {"mode": "dolphin_rvz", "enabled": True}})
        # The test's own guard: without the deadline this never returns.
        result = await _asyncio.wait_for(
            romm_auto.sweep(ignore_schedule=True), 10,
        )

    assert result["queued"] == 0, result
    assert {"platform_id": 7, "error": "library_unresponsive"} in result["errors"]


@pytest.mark.asyncio
async def test_a_queued_retry_keeps_its_row_when_old_split_parts_remain(
    settings_db, tmp_path: Path,
) -> None:
    """Numbered parts at the destination may be the *previous* attempt's.

    A makeps3iso run that split its output leaves `Game.iso.0`, `.1`, … A retry
    with splitting switched off is queued to write a single matchable ISO over
    them — and reading the old parts as this attempt's final output retired the
    row before the conversion that would have settled it even started.
    """
    from services.romm import repin as romm_repin

    out = tmp_path / "Game.iso"
    (tmp_path / "Game.iso.0").write_bytes(b"part")

    romm_repin.record(
        {"id": 5, "igdb_id": 42}, str(out), {"igdb_id": 42}, "folder_to_iso",
    )
    row = romm_repin.pending_rows(10)[0]

    with patch.object(romm_routes, "_destination_has_pending_job", return_value=True):
        outcome = await romm_routes._settle_one_repin(row)
    assert outcome is romm_routes._Outcome.WAITING

    # With nothing queued for it, the parts are this conversion's output and
    # the row is retired with that as the reason.
    with patch.object(romm_routes, "_destination_has_pending_job", return_value=False):
        outcome = await romm_routes._settle_one_repin(row)
    assert outcome is romm_routes._Outcome.ABANDONED


@pytest.mark.asyncio
async def test_a_manual_limit_can_only_narrow_the_run(
    settings_db, tmp_path: Path,
) -> None:
    """Preview and Run now accept a limit; it must not raise the safety cap.

    `auto_convert_max_per_run` is the configured ceiling on how much unattended
    work one sweep may queue. A caller-supplied limit was taken verbatim, so a
    button could queue far past it.
    """
    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    for name in ("A", "B", "C"):
        (lib / f"{name}.iso").write_bytes(b"\0" * 32)

    await settings_db.save({
        "url": "http://romm:8080",
        "library_root": str(tmp_path / "library"),
        "auto_convert_max_per_run": 1,
    })
    roms = [
        {"id": i, "name": name, "full_path": f"roms/gc/{name}.iso",
         "fs_name": f"{name}.iso", "platform_slug": "ngc"}
        for i, name in enumerate(("A", "B", "C"), start=1)
    ]

    async def _fake_batch(paths, mode, **kwargs):
        return _fake_jobs(paths)

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(romm_auto.job_manager, "create_batch_jobs", _fake_batch), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        await romm_auto.set_rules({"7": {"mode": "dolphin_rvz", "enabled": True}})
        widened = await romm_auto.sweep(ignore_schedule=True, overall_limit=500)
        assert widened["queued"] == 1, widened
        # Three ROMs were eligible and the platform stopped at one: the
        # configured maximum decided, not the number the caller asked for.
        assert len(roms) == 3


@pytest.mark.asyncio
async def test_sweep_skips_a_platform_whose_tool_is_not_installed(
    settings_db, tmp_path: Path,
) -> None:
    """A saved rule outlives its install; queueing would fail every job."""
    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc",
    }]

    tool = registry.for_mode("dolphin_rvz")
    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(type(tool), "is_ready", AsyncMock(return_value=False)), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        await romm_auto.set_rules({"7": {"mode": "dolphin_rvz", "enabled": True}})
        result = await romm_auto.sweep(ignore_schedule=True, dry_run=True)

    assert result["queued"] == 0, result
    assert result["errors"] == [{"platform_id": 7, "error": "tool_not_ready"}]


@pytest.mark.asyncio
async def test_sweep_refuses_a_catalog_entry_whose_file_is_gone(
    settings_db, tmp_path: Path,
) -> None:
    """RomM's catalog outlives the files it describes."""
    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    # Deliberately not created on disk.

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Gone.iso",
        "fs_name": "Gone.iso", "platform_slug": "ngc",
    }]

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        await romm_auto.set_rules({"7": {"mode": "dolphin_rvz", "enabled": True}})
        result = await romm_auto.sweep(ignore_schedule=True, dry_run=True)

    assert result["queued"] == 0, result
    assert result["skipped_missing"] == 1


@pytest.mark.asyncio
async def test_sweep_refuses_a_directory_where_the_mode_takes_a_file(
    settings_db, tmp_path: Path,
) -> None:
    """Existing is not the same as being the kind of thing the mode consumes.

    The declaration check ahead of this is extensions and the tool's own
    predicate, so a directory whose *name* carries an accepted extension
    passes it — and plain `exists()` then let it into the queue. Nothing is
    watching an unattended sweep: an `overwrite` rule authorises the job,
    `_clear_existing_output` removes the previous artifact, and only then does
    the converter fail on a directory it cannot open.
    """
    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    # A *directory* named like a disc image, which is what RomM lists.
    (lib / "Game.iso").mkdir()
    # And the output an overwrite rule would clear before failing.
    (lib / "Game.rvz").write_bytes(b"\0" * 16)

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    roms = [{
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "platform_slug": "ngc",
    }]

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        await romm_auto.set_rules({"7": {
            "mode": "dolphin_rvz", "enabled": True, "duplicate_action": "overwrite",
        }})
        result = await romm_auto.sweep(ignore_schedule=True, dry_run=True)

    assert result["queued"] == 0, result
    # Refused by the target format, not reported as gone from disk: it is
    # right there, it is just not a file.
    assert result["skipped_unconvertible"] == 1, result
    assert result["skipped_missing"] == 0, result
    # And the existing output is untouched, because nothing was ever queued.
    assert (lib / "Game.rvz").exists()

    # The directory mode's own source is still accepted, or the check would
    # have closed the PS3 folder path along with the bug.
    assert (
        registry.mode_input_kind("folder_to_iso") is romm_auto.InputKind.DIRECTORY
    )
    assert registry.mode_input_kind("dolphin_rvz") is romm_auto.InputKind.FILE


@pytest.mark.asyncio
async def test_sweep_never_sends_two_sources_to_one_destination(
    settings_db, tmp_path: Path,
) -> None:
    """Two catalog rows for one file must not both be queued.

    Under `overwrite` an occupied destination is queueable, so both resolve to
    the same output -- and with delete_on_verify both sources are deleted for
    one surviving file.
    """
    from services.romm import auto as romm_auto

    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    link = lib / "Game.link.iso"
    link.symlink_to(lib / "Game.iso")

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    roms = [
        {"id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
         "fs_name": "Game.iso", "platform_slug": "ngc"},
        {"id": 2, "name": "Game again", "full_path": "roms/gc/Game.iso",
         "fs_name": "Game.iso", "platform_slug": "ngc"},
    ]
    queued: list[list[str]] = []

    async def _fake_batch(paths, mode, **kwargs):
        queued.append(list(paths))
        return _fake_jobs(paths)

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(romm_auto.job_manager, "create_batch_jobs", _fake_batch), \
            patch.object(
                romm_auto.job_manager, "get_active_job_candidates", return_value=[],
            ), \
            patch.object(romm_auto, "is_within_configured_volumes", return_value=True):
        await romm_auto.set_rules({"7": {
            "mode": "dolphin_rvz", "enabled": True, "duplicate_action": "overwrite",
        }})
        result = await romm_auto.sweep(ignore_schedule=True)

    assert result["queued"] == 1, result
    assert queued == [[str(lib / "Game.iso")]]


def test_modes_validate_sources_against_their_own_inputs() -> None:
    """The mode's inputs, not the tool's.

    chdman drops `.chd` from its tool-level extensions so a finished CHD is
    not badged as a convertible source -- but `.chd` is exactly what its
    extract and copy modes take. Asking the tool rejected every real `copy`
    source and accepted `.iso` files that mode cannot consume.
    """
    from services.romm import auto as romm_auto

    tool = registry.for_mode("copy")
    spec = registry.spec("copy")
    assert tool.converts_path("/vol/Game.chd") is False  # the old, wrong gate
    assert romm_auto._accepts_source(tool, spec, "/vol/Game.chd") is True
    assert romm_auto._accepts_source(tool, spec, "/vol/Game.iso") is False

    # The create direction is unaffected: it takes the disc images.
    create, create_spec = registry.for_mode("createcd"), registry.spec("createcd")
    assert romm_auto._accepts_source(create, create_spec, "/vol/Game.cue") is True
    assert romm_auto._accepts_source(create, create_spec, "/vol/Game.chd") is False


def test_directory_modes_use_the_directory_predicate(tmp_path: Path) -> None:
    """makeps3iso declares no input extensions, so `converts_path` rejects all.

    Gating the sweep on it made an advertised PS3 rule report every decrypted
    folder "unconvertible" and queue nothing, ever.
    """
    from services import ps3
    from services.romm import auto as romm_auto

    folder = tmp_path / "MyGame"
    (folder / "PS3_GAME" / "USRDIR").mkdir(parents=True)
    (folder / "PS3_GAME" / "PARAM.SFO").write_bytes(b"\0" * 16)
    assert ps3.is_ps3_iso_source(str(folder))

    tool = registry.for_mode("folder_to_iso")
    spec = registry.spec("folder_to_iso")
    assert tool.converts_path(str(folder)) is False  # the old, wrong gate
    assert romm_auto._accepts_source(tool, spec, str(folder)) is True

    # A file mode is still judged on its extensions.
    dolphin, dolphin_spec = registry.for_mode("dolphin_rvz"), registry.spec("dolphin_rvz")
    assert romm_auto._accepts_source(dolphin, dolphin_spec, "/vol/Game.iso") is True
    assert romm_auto._accepts_source(dolphin, dolphin_spec, "/vol/Game.txt") is False


@pytest.mark.asyncio
async def test_settle_waits_while_a_job_is_queued_for_the_output(
    repin_db, tmp_path: Path,
) -> None:
    """An overwrite job leaves the OLD artifact in place until it starts.

    `check_file_status` reports no lock while a job is merely queued, so the
    pass would hash the previous file and cache that digest — after which the
    real output could never be matched.
    """
    output = tmp_path / "Game.rvz"
    output.write_bytes(b"stale" * 8)
    romm_repin.record({"id": 7}, str(output), {"igdb_id": 42})

    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(tmp_path)), \
            patch.object(
                romm_routes.job_manager, "get_active_job_candidates",
                return_value=[("job1", [str(output)])],
            ), \
            patch.object(romm_routes, "run_detached", AsyncMock()) as hashed:
        result = await romm_routes.settle_romm_repins()

    assert result["waiting"] == 1, result
    assert result["repinned"] == 0
    hashed.assert_not_awaited()  # the stale file was never hashed
    assert romm_repin.count_pending() == 1


@pytest.mark.asyncio
async def test_platform_tool_ids_exclude_unavailable_tools() -> None:
    """A tool the deployment cannot run must not be offered as a rule target."""
    platforms = [{"id": 9, "name": "Switch", "slug": "switch"}]

    nsz = registry.for_mode("nsz_compress")
    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", "/data/library"), \
            patch.object(romm_routes.romm_client, "platforms", return_value=platforms), \
            patch.object(type(nsz), "is_ready", AsyncMock(return_value=False)):
        rows = await romm_routes.romm_platforms()

    assert "nsz" not in rows[0]["tool_ids"], rows
