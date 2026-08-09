"""Tests for the Wii U (JWUDTool) info and verification routes."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.routes import info as info_routes


@pytest.fixture
def jwud_test_env(tmp_path, monkeypatch):
    """Set up a test environment with fake Wii U source/output files."""
    source_path = tmp_path / "game.wud"
    source_path.write_text("fake source")

    compressed_path = tmp_path / "game.wux"
    compressed_path.write_text("fake compressed")

    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))

    return {
        "source_path": str(source_path),
        "compressed_path": str(compressed_path),
        "tmp_path": tmp_path,
    }


@pytest.fixture
def mock_jwud_service(monkeypatch):
    """Mock jwudtool_service so the routes never touch the real binary."""
    mock_service = Mock()

    def fake_info(path):
        return {
            "file": path,
            "size": 3112960,
            "size_display": "2.97 MB",
            "format": "WUX (compressed Wii U disc image)",
            "extension": ".wux",
            "compressed": True,
            "compression_type": "WUX (sector deduplication)",
            "original_size": 25025314816,
            "ratio": "0.0%",
        }

    async def fake_verify(path):
        return {"valid": True, "message": "WUX container verified"}

    async def fake_verify_stream(path):
        yield {"type": "progress", "progress": 25, "message": "Reading WUX header..."}
        await asyncio.sleep(0.01)
        yield {"type": "complete", "valid": True, "message": "WUX container verified"}

    mock_service.info = fake_info
    mock_service.verify = fake_verify
    mock_service.verify_stream = fake_verify_stream

    monkeypatch.setattr(info_routes, "jwudtool_service", mock_service)
    return mock_service


@pytest.fixture
def mock_verification_store(monkeypatch):
    mock_store = Mock()
    mock_store.mark_verified = AsyncMock()
    monkeypatch.setattr(info_routes, "verification_store", mock_store)
    return mock_store


@pytest.mark.asyncio
async def test_jwud_verify_happy_path(
    jwud_test_env, mock_jwud_service, mock_verification_store,
):
    """Verify a compressed Wii U output successfully."""
    result = await info_routes.verify_jwud(path=jwud_test_env["compressed_path"])

    assert result["valid"] is True
    assert "verified" in result["message"].lower()
    mock_verification_store.mark_verified.assert_called_once_with(
        jwud_test_env["compressed_path"],
    )


@pytest.mark.asyncio
async def test_jwud_verify_rejects_source_extension(jwud_test_env, mock_jwud_service):
    """A raw .wud carries nothing to check, so verify rejects it."""
    with pytest.raises(HTTPException) as exc_info:
        await info_routes.verify_jwud(path=jwud_test_env["source_path"])

    assert exc_info.value.status_code == 400
    assert ".wux" in exc_info.value.detail


@pytest.mark.asyncio
async def test_jwud_verify_stream_happy_path(
    jwud_test_env, mock_jwud_service, mock_verification_store,
):
    """Stream Wii U verification progress and completion events."""
    response = await info_routes.verify_jwud_events(path=jwud_test_env["compressed_path"])

    events = [event async for event in response.body_iterator]

    assert len(events) >= 2
    assert any(e.get("event") == "verify_progress" for e in events if isinstance(e, dict))
    assert any(e.get("event") == "verify_complete" for e in events if isinstance(e, dict))
    mock_verification_store.mark_verified.assert_called_once_with(
        jwud_test_env["compressed_path"],
    )


@pytest.mark.asyncio
async def test_jwud_verify_stream_returns_verify_error_when_lane_is_saturated(
    jwud_test_env, mock_jwud_service, mock_verification_store, monkeypatch,
):
    """Streaming verify emits verify_error when the shared verify lane is full."""
    monkeypatch.setattr(
        info_routes.workload_limiter, "try_acquire", AsyncMock(return_value=None),
    )

    response = await info_routes.verify_jwud_events(path=jwud_test_env["compressed_path"])

    events = [event async for event in response.body_iterator]

    payloads = [
        e.get("data")
        for e in events
        if isinstance(e, dict) and e.get("event") == "verify_error"
    ]
    assert payloads
    assert "capacity" in payloads[0].lower()


@pytest.mark.asyncio
async def test_jwud_info_reports_the_wux_ratio(jwud_test_env, mock_jwud_service):
    """jwud-info surfaces the WUX header's original size and ratio."""
    result = await info_routes.get_jwud_info(path=jwud_test_env["compressed_path"])

    assert result.file == jwud_test_env["compressed_path"]
    assert result.compressed is True
    assert result.original_size == 25025314816
    assert result.ratio == "0.0%"


@pytest.mark.asyncio
async def test_jwud_info_accepts_the_raw_source(jwud_test_env, mock_jwud_service):
    """Info covers both directions; only verify is narrowed to .wux."""
    result = await info_routes.get_jwud_info(path=jwud_test_env["source_path"])

    assert result.file == jwud_test_env["source_path"]


@pytest.mark.asyncio
async def test_jwud_info_rejects_other_extensions(jwud_test_env, mock_jwud_service):
    other = jwud_test_env["tmp_path"] / "game.iso"
    other.write_text("not a wii u image")

    with pytest.raises(HTTPException) as exc_info:
        await info_routes.get_jwud_info(path=str(other))

    assert exc_info.value.status_code == 400
