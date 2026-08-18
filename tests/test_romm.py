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
from services import romm as romm_service
from services import romm_repin
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
    with patch("services.romm._urlopen", return_value=_response([])) as urlopen:
        client.platforms()
    request = urlopen.call_args[0][0]
    assert request.get_header("Authorization") == "Bearer rmm_test"


def test_heartbeat_is_unauthenticated(client: RommClient) -> None:
    """The heartbeat probe must work before a token is configured.

    It is what separates "cannot reach RomM" from "token rejected", so sending
    credentials it does not need would defeat the diagnostic.
    """
    with patch(
        "services.romm._urlopen", return_value=_response({"VERSION": "4.9.0"}),
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
    with patch("services.romm._urlopen", return_value=_response([])):
        assert client.platforms() == []


def test_http_error_becomes_romm_error(client: RommClient) -> None:
    err = urllib.error.HTTPError(
        "http://romm:8080/api/platforms", 401, "Unauthorized", {},
        io.BytesIO(b"bad token"),
    )
    with patch("services.romm._urlopen", side_effect=err), \
            pytest.raises(RommError, match="HTTP 401"):
        client.platforms()


def test_rom_by_sha1_treats_404_as_no_match(client: RommClient) -> None:
    """404 means "RomM has not scanned it yet", which is a normal state."""
    err = urllib.error.HTTPError(
        "http://romm:8080/api/roms/by-hash", 404, "Not Found", {}, io.BytesIO(b""),
    )
    with patch("services.romm._urlopen", side_effect=err):
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
    romm_repin.record({"id": 7}, str(output), {"igdb_id": 42})
    assert romm_repin.count_pending() == 1

    with patch.object(RommClient, "base_url", "http://romm:8080"), \
            patch.object(RommClient, "library_root", str(tmp_path)):
        result = await romm_routes.romm_repin_cancel(
            romm_routes.RepinCancelRequest(paths=[str(output)]),
        )

    assert result["cancelled"] == 1
    assert romm_repin.count_pending() == 0
    # Retired, not deleted: the history of what was planned survives.
    assert romm_repin.cancel([str(output)]) == 0


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
    assert romm_repin.cancel(list(result["recorded_paths"].values())) == 1
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
    with patch("services.romm._urlopen", return_value=_response({"id": 500})) as urlopen:
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
    with patch("services.romm._urlopen") as urlopen:
        client.update_rom_metadata(500, {"moby_id": None})
    urlopen.assert_not_called()


def test_update_rom_metadata_ignores_unknown_fields(client: RommClient) -> None:
    """Only the known provider ids are forwarded, never arbitrary keys."""
    with patch("services.romm._urlopen", return_value=_response({})) as urlopen:
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
    from services import romm_settings

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
    from services.romm import RommError, _encode_multipart

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

    from services import romm_auto

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
    from services import romm_auto

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
    from services import romm_auto, romm_repin

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
    from services import romm_auto

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
    from services import romm_auto

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
    from services import romm_auto

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
    from services import romm_auto

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
    from services import romm_auto

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

    from services.romm import _SameOriginRedirectHandler

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
    from services import romm_auto

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


def test_a_mistyped_filter_pauses_the_rule_instead_of_widening_it() -> None:
    """An uncompilable regex must never come back as "no filter".

    The filter is what keeps a rule to a subset; dropping it silently would let
    the next unattended sweep queue the whole platform, delete-on-verify and all.
    """
    from services import romm_auto

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
    from services import romm_auto

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
    from services import romm_auto

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
    from services import romm_auto

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
    from services import romm_auto

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
    from services import romm_auto

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
    from services import romm_auto

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
    way out. The probe draws its characters from the pattern, because the
    trigger is pattern-specific: `(x+x+)+y` only blows up on a run of `x`.
    """
    from services import romm_auto

    for good in (r"\(USA\)", ".*Beta.*", "^Super", r"^.*\(USA\).*(Rev [0-9])?$"):
        assert romm_auto._backtracks_catastrophically(re.compile(good)) is False, good

    for bad in ("(a+)+$", "(x+x+)+y", "(a|a)+$", r"^(\w+\s?)*$"):
        assert romm_auto._backtracks_catastrophically(re.compile(bad)) is True, bad

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

    # A mode with no opinion of its own inherits its tool's.
    assert registry.mode_allows_platform("createdvd", "ps2") is True
    assert registry.mode_allows_platform("createdvd", "ngc") is False
    # An unrecognised slug narrows nothing, matching narrow_to_platform.
    assert registry.mode_allows_platform("cso_to_chd", "not-a-console") is True


@pytest.mark.asyncio
async def test_sweep_refuses_a_composite_mode_for_the_wrong_platform(
    settings_db, tmp_path: Path,
) -> None:
    """The saved rule is checked against the mode, not its tool."""
    from services import romm_auto

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
    from services import romm_auto

    await settings_db.save({
        "url": "http://romm:8080", "library_root": str(tmp_path),
    })
    await romm_auto.set_rules({"7": {
        "mode": "dolphin_rvz", "enabled": True, "duplicate_action": "overwrite",
    }})
    await romm_auto._mark_converted(
        "7", [(1, str(tmp_path / "Game.rvz"), "", None)],
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
    from services import romm_auto

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
    from services import romm_auto

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
    from services import romm_auto

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
async def test_sweep_skips_a_platform_whose_tool_is_not_installed(
    settings_db, tmp_path: Path,
) -> None:
    """A saved rule outlives its install; queueing would fail every job."""
    from services import romm_auto

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
    from services import romm_auto

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
async def test_sweep_never_sends_two_sources_to_one_destination(
    settings_db, tmp_path: Path,
) -> None:
    """Two catalog rows for one file must not both be queued.

    Under `overwrite` an occupied destination is queueable, so both resolve to
    the same output -- and with delete_on_verify both sources are deleted for
    one surviving file.
    """
    from services import romm_auto

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
    from services import romm_auto

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
    from services import romm_auto

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
