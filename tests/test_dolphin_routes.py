"""Tests for Dolphin disc image info and verification routes."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.routes import info as info_routes


@pytest.fixture
def dolphin_test_env(tmp_path, monkeypatch):
    """Set up test environment with a fake Dolphin disc image."""
    # Create a fake .iso file
    iso_path = tmp_path / "game.iso"
    iso_path.write_text("fake disc content")

    # Configure volumes to allow access
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))

    return {
        "iso_path": str(iso_path),
        "tmp_path": tmp_path,
    }


@pytest.fixture
def mock_dolphin_service(monkeypatch):
    """Mock the dolphin_tool_service for testing."""
    mock_service = Mock()

    # Mock header method - all values should be strings
    async def fake_header(path):
        return {
            "game_id": "GALE01",
            "game_name": "Super Smash Bros. Melee",
            "disc_number": "0",
            "revision": "2",
            "region": "USA",
            "format": "ISO",
            "compression": None,
            "block_size": "0",
            "file_size": "1459978240",
            "raw_data": "Game ID: GALE01\nGame Name: Super Smash Bros. Melee",
        }

    # Mock verify method
    async def fake_verify(path):
        return {"valid": True, "message": "Disc image verified successfully"}

    # Mock verify_stream method
    async def fake_verify_stream(path):
        yield {"type": "progress", "progress": 50, "message": "Verifying..."}
        await asyncio.sleep(0.01)
        yield {"type": "complete", "valid": True, "message": "Verification complete"}

    mock_service.header = fake_header
    mock_service.verify = fake_verify
    mock_service.verify_stream = fake_verify_stream

    monkeypatch.setattr(info_routes, "dolphin_tool_service", mock_service)
    return mock_service


@pytest.fixture
def mock_verification_store(monkeypatch):
    """Mock the verification_store for testing."""
    mock_store = Mock()
    mock_store.mark_verified = AsyncMock()
    monkeypatch.setattr(info_routes, "verification_store", mock_store)
    return mock_store


@pytest.mark.asyncio
async def test_dolphin_info_happy_path(dolphin_test_env, mock_dolphin_service):
    """Test successful retrieval of Dolphin disc info."""
    result = await info_routes.get_dolphin_info(path=dolphin_test_env["iso_path"])

    assert result.file == dolphin_test_env["iso_path"]
    assert result.game_id == "GALE01"
    assert result.game_name == "Super Smash Bros. Melee"
    assert result.disc_number == "0"
    assert result.revision == "2"
    assert result.region == "USA"
    assert result.format == "ISO"


@pytest.mark.asyncio
async def test_dolphin_info_file_not_found(dolphin_test_env, mock_dolphin_service):
    """Test dolphin-info with non-existent file."""
    from fastapi import HTTPException

    nonexistent_path = str(dolphin_test_env["tmp_path"] / "nonexistent.iso")

    with pytest.raises(HTTPException) as exc_info:
        await info_routes.get_dolphin_info(path=nonexistent_path)

    assert exc_info.value.status_code == 404
    assert "File not found" in exc_info.value.detail


@pytest.mark.asyncio
async def test_dolphin_info_outside_volume(dolphin_test_env, mock_dolphin_service):
    """Test dolphin-info with path outside configured volumes."""
    from fastapi import HTTPException

    outside_path = "/tmp/outside/game.iso"

    with pytest.raises(HTTPException) as exc_info:
        await info_routes.get_dolphin_info(path=outside_path)

    assert exc_info.value.status_code == 403
    assert "Access denied" in exc_info.value.detail


@pytest.mark.asyncio
async def test_dolphin_info_unsupported_extension(dolphin_test_env, mock_dolphin_service):
    """Test dolphin-info with unsupported file extension."""
    from fastapi import HTTPException

    # Create a file with unsupported extension
    txt_path = dolphin_test_env["tmp_path"] / "game.txt"
    txt_path.write_text("not a disc")

    with pytest.raises(HTTPException) as exc_info:
        await info_routes.get_dolphin_info(path=str(txt_path))

    assert exc_info.value.status_code == 400
    assert "Not a supported disc image format" in exc_info.value.detail


@pytest.mark.asyncio
async def test_dolphin_verify_happy_path(
    dolphin_test_env, mock_dolphin_service, mock_verification_store
):
    """Test successful Dolphin disc verification."""
    result = await info_routes.verify_dolphin(path=dolphin_test_env["iso_path"])

    assert result["valid"] is True
    assert "successfully" in result["message"]

    # Verify that mark_verified was called
    mock_verification_store.mark_verified.assert_called_once_with(
        dolphin_test_env["iso_path"]
    )


@pytest.mark.asyncio
async def test_dolphin_verify_returns_429_when_verify_lane_is_saturated(
    dolphin_test_env, mock_dolphin_service, mock_verification_store, monkeypatch,
):
    """Verification requests should fail fast when verify lane has no capacity."""
    from fastapi import HTTPException

    monkeypatch.setattr(
        info_routes.workload_limiter,
        "try_acquire",
        AsyncMock(return_value=None),
    )

    with pytest.raises(HTTPException) as exc_info:
        await info_routes.verify_dolphin(path=dolphin_test_env["iso_path"])

    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_dolphin_verify_file_not_found(
    dolphin_test_env, mock_dolphin_service, mock_verification_store
):
    """Test dolphin-verify with non-existent file."""
    from fastapi import HTTPException

    nonexistent_path = str(dolphin_test_env["tmp_path"] / "nonexistent.iso")

    with pytest.raises(HTTPException) as exc_info:
        await info_routes.verify_dolphin(path=nonexistent_path)

    assert exc_info.value.status_code == 404
    assert "File not found" in exc_info.value.detail


@pytest.mark.asyncio
async def test_dolphin_verify_outside_volume(
    dolphin_test_env, mock_dolphin_service, mock_verification_store
):
    """Test dolphin-verify with path outside configured volumes."""
    from fastapi import HTTPException

    outside_path = "/tmp/outside/game.iso"

    with pytest.raises(HTTPException) as exc_info:
        await info_routes.verify_dolphin(path=outside_path)

    assert exc_info.value.status_code == 403
    assert "Access denied" in exc_info.value.detail


@pytest.mark.asyncio
async def test_dolphin_verify_unsupported_extension(
    dolphin_test_env, mock_dolphin_service, mock_verification_store
):
    """Test dolphin-verify with unsupported file extension."""
    from fastapi import HTTPException

    # Create a file with unsupported extension
    txt_path = dolphin_test_env["tmp_path"] / "game.txt"
    txt_path.write_text("not a disc")

    with pytest.raises(HTTPException) as exc_info:
        await info_routes.verify_dolphin(path=str(txt_path))

    assert exc_info.value.status_code == 400
    assert "Not a supported disc image format" in exc_info.value.detail


@pytest.mark.asyncio
async def test_dolphin_verify_stream_happy_path(
    dolphin_test_env, mock_dolphin_service, mock_verification_store
):
    """Test successful streaming verification of Dolphin disc."""
    # Get the event generator
    response = await info_routes.verify_dolphin_events(
        path=dolphin_test_env["iso_path"]
    )

    # Collect events
    events = []
    async for event in response.body_iterator:
        events.append(event)

    # Should have at least progress and complete events
    assert len(events) >= 2

    # Check that we got progress and complete events
    progress_found = False
    complete_found = False

    for event in events:
        # Events are dict-like objects with 'event' and 'data' keys
        if isinstance(event, dict):
            if event.get("event") == "verify_progress":
                progress_found = True
            if event.get("event") == "verify_complete":
                complete_found = True

    assert progress_found, "Expected to find verify_progress event"
    assert complete_found, "Expected to find verify_complete event"


@pytest.mark.asyncio
async def test_dolphin_verify_stream_returns_verify_error_when_lane_is_saturated(
    dolphin_test_env, mock_dolphin_service, mock_verification_store, monkeypatch,
):
    """Streaming verify should emit verify_error with lane-capacity detail."""
    monkeypatch.setattr(
        info_routes.workload_limiter,
        "try_acquire",
        AsyncMock(return_value=None),
    )

    response = await info_routes.verify_dolphin_events(
        path=dolphin_test_env["iso_path"]
    )

    events = []
    async for event in response.body_iterator:
        events.append(event)

    assert any(e.get("event") == "verify_error" for e in events if isinstance(e, dict))
    payloads = [
        e.get("data")
        for e in events
        if isinstance(e, dict) and e.get("event") == "verify_error"
    ]
    assert payloads
    assert "capacity" in payloads[0].lower()


@pytest.mark.asyncio
async def test_dolphin_verify_stream_file_not_found(
    dolphin_test_env, mock_dolphin_service, mock_verification_store
):
    """Test streaming verify with non-existent file."""
    from fastapi import HTTPException

    nonexistent_path = str(dolphin_test_env["tmp_path"] / "nonexistent.iso")

    with pytest.raises(HTTPException) as exc_info:
        await info_routes.verify_dolphin_events(path=nonexistent_path)

    assert exc_info.value.status_code == 404
    assert "File not found" in exc_info.value.detail


@pytest.mark.asyncio
async def test_dolphin_info_service_error(dolphin_test_env, monkeypatch):
    """Test dolphin-info when service raises an error."""
    from fastapi import HTTPException

    # Mock service to raise an error
    mock_service = Mock()

    async def fake_header_error(path):
        raise RuntimeError("Failed to read disc header")

    mock_service.header = fake_header_error
    monkeypatch.setattr(info_routes, "dolphin_tool_service", mock_service)
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(dolphin_test_env["tmp_path"]))
    monkeypatch.setattr(
        info_routes.settings, "data_mount_root", str(dolphin_test_env["tmp_path"])
    )

    with pytest.raises(HTTPException) as exc_info:
        await info_routes.get_dolphin_info(path=dolphin_test_env["iso_path"])

    assert exc_info.value.status_code == 500
    assert "Failed to read disc info" in exc_info.value.detail


@pytest.mark.asyncio
async def test_dolphin_verify_service_error(
    dolphin_test_env, monkeypatch, mock_verification_store
):
    """Test dolphin-verify when service raises an error."""
    from fastapi import HTTPException

    # Mock service to raise an error
    mock_service = Mock()

    async def fake_verify_error(path):
        raise RuntimeError("Verification failed")

    mock_service.verify = fake_verify_error
    monkeypatch.setattr(info_routes, "dolphin_tool_service", mock_service)
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(dolphin_test_env["tmp_path"]))
    monkeypatch.setattr(
        info_routes.settings, "data_mount_root", str(dolphin_test_env["tmp_path"])
    )

    with pytest.raises(HTTPException) as exc_info:
        await info_routes.verify_dolphin(path=dolphin_test_env["iso_path"])

    assert exc_info.value.status_code == 500
    assert "Failed to verify disc image" in exc_info.value.detail


# ---------------------------------------------------------------------------
# disc_hashes  (redump-style content SHA1 via `dolphin-tool verify`)
# ---------------------------------------------------------------------------


class _FakeProc:
    """Minimal asyncio subprocess stand-in for disc_hashes()."""

    def __init__(self, stdout: bytes, returncode: int = 0):
        self._stdout = stdout
        self.returncode = returncode
        self.pid = 4321

    async def communicate(self):
        return self._stdout, b""


@pytest.mark.asyncio
async def test_disc_hashes_parses_sha1(monkeypatch):
    """disc_hashes extracts the 40-char hex SHA1 from verify output."""
    from app.services.dolphin_tool import dolphin_tool_service

    sha1 = "aabbccddaabbccddaabbccddaabbccddaabbccdd"
    out = f"Problems Found: No\nSHA-1: {sha1}\n".encode()

    async def fake_exec(*args, **kwargs):
        assert "--algorithm" in args and "sha1" in args
        return _FakeProc(out)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await dolphin_tool_service.disc_hashes("/data/game.rvz")
    assert result == [sha1]


@pytest.mark.asyncio
async def test_disc_hashes_empty_on_nonzero_exit(monkeypatch):
    """A failed verify yields no hashes (caller falls back to file SHA1)."""
    from app.services.dolphin_tool import dolphin_tool_service

    async def fake_exec(*args, **kwargs):
        return _FakeProc(b"error: bad image\n", returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert await dolphin_tool_service.disc_hashes("/data/bad.rvz") == []


@pytest.mark.asyncio
async def test_disc_hashes_empty_on_abort(monkeypatch):
    """When run_capture reports an abort (returncode None), disc_hashes is empty."""
    from app.services.dolphin_tool import dolphin_tool_service

    async def fake_run_capture(cmd, *, timeout=None, cancel_event=None,
                               stderr_to_stdout=False, fail_label=None):
        return None, b"", b""

    monkeypatch.setattr(dolphin_tool_service._runner, "run_capture", fake_run_capture)
    assert await dolphin_tool_service.disc_hashes("/data/x.rvz") == []


@pytest.mark.asyncio
async def test_disc_hashes_forwards_cancel_event(monkeypatch):
    """disc_hashes threads its cancel_event into the shared run_capture call."""
    from app.services.dolphin_tool import dolphin_tool_service

    seen = {}
    event = asyncio.Event()

    async def fake_run_capture(cmd, *, timeout=None, cancel_event=None,
                               stderr_to_stdout=False, fail_label=None):
        seen["cancel_event"] = cancel_event
        return 0, b"SHA-1: " + b"a" * 40, b""

    monkeypatch.setattr(dolphin_tool_service._runner, "run_capture", fake_run_capture)
    result = await dolphin_tool_service.disc_hashes("/data/x.rvz", cancel_event=event)

    assert result == ["a" * 40]
    assert seen["cancel_event"] is event
