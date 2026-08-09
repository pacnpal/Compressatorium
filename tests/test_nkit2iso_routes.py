"""Route tests for the NKit info endpoint (``GET /api/nkit-info``).

There is deliberately no verify trio to test: nkit2iso has no verify
subcommand, so ``NkitTool`` registers none (see the plugin docstring).
"""
from __future__ import annotations

import struct

import pytest
from fastapi import HTTPException

from app.routes import info as info_routes


def _nkit_bytes(*, wii: bool = False, marker: bytes = b"NKIT v01") -> bytes:
    head = bytearray(b"\x00" * 0x440)
    head[0x00:0x06] = b"GALE01"
    head[0x07] = 2
    struct.pack_into(">I", head, 0x18, 0x5D1C9EA3 if wii else 0)
    struct.pack_into(">I", head, 0x1C, 0 if wii else 0xC2339F3D)
    head[0x20:0x25] = b"Melee"
    head[0x200:0x200 + len(marker)] = marker
    struct.pack_into(">I", head, 0x208, 0x099E2C6D)
    struct.pack_into(">I", head, 0x210, 1_000_000)
    return bytes(head)


@pytest.fixture(name="volume")
def _volume(tmp_path, monkeypatch):
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_nkit_info_returns_header_fields(volume):
    src = volume / "Melee.nkit.iso"
    src.write_bytes(_nkit_bytes())

    result = await info_routes.get_nkit_info(path=str(src))

    assert result.platform == "GameCube"
    assert result.game_id == "GALE01"
    assert result.title == "Melee"
    assert result.crc32 == "099E2C6D"
    assert result.restored_size == 1_000_000
    assert result.extension == ".nkit.iso"
    assert result.compressed is True


@pytest.mark.asyncio
async def test_nkit_info_rejects_a_plain_iso_by_name(volume):
    # The compound extension is the gate: a plain .iso belongs to the tools
    # that own the generic tail, not here.
    src = volume / "Melee.iso"
    src.write_bytes(_nkit_bytes())

    with pytest.raises(HTTPException) as exc:
        await info_routes.get_nkit_info(path=str(src))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_nkit_info_reports_a_mislabelled_file_as_422(volume):
    # Named .nkit.iso, but the bytes carry no NKit marker — a client error
    # about the file, not a server fault.
    src = volume / "Melee.nkit.iso"
    src.write_bytes(_nkit_bytes(marker=b"\x00" * 8))

    with pytest.raises(HTTPException) as exc:
        await info_routes.get_nkit_info(path=str(src))
    assert exc.value.status_code == 422
    assert "plain ISO" in exc.value.detail


@pytest.mark.asyncio
async def test_nkit_info_missing_file_is_404(volume):
    with pytest.raises(HTTPException) as exc:
        await info_routes.get_nkit_info(path=str(volume / "absent.nkit.iso"))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_nkit_info_outside_volumes_is_403(volume, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "Melee.nkit.iso"
    outside.write_bytes(_nkit_bytes())

    with pytest.raises(HTTPException) as exc:
        await info_routes.get_nkit_info(path=str(outside))
    assert exc.value.status_code == 403
