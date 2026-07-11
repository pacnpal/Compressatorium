"""File browser: junk filtering + delete (recursive opt-in for non-empty dirs)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers

from app.routes import files as files_routes
from app.utils.junk import is_junk_entry, is_junk_path


def _request(headers: dict[str, str] | None = None):
    return SimpleNamespace(headers=Headers(headers or {}))


@pytest.fixture(name="vol")
def _vol(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize(
    "junk",
    [".DS_Store", "Thumbs.db", "thumbs.db", "._game.nsp", "desktop.ini", ".nfs0001"],
)
def test_is_junk_entry(junk):
    assert is_junk_entry(junk) is True


@pytest.mark.parametrize("keep", ["game.nsp", "disc.cue", ".switch", "Game (USA).iso"])
def test_is_not_junk(keep):
    assert is_junk_entry(keep) is False


@pytest.mark.parametrize(
    "junk_path",
    ["__MACOSX/._Game.gba", "sub/.DS_Store", "Thumbs.db", "._Game.gba"],
)
def test_is_junk_path_flags_junk_in_any_component(junk_path):
    assert is_junk_path(junk_path) is True


@pytest.mark.parametrize("keep", ["Game.gba", "roms/Game (USA).gba", "sub/disc.cue"])
def test_is_junk_path_keeps_real_members(keep):
    assert is_junk_path(keep) is False


@pytest.mark.asyncio
async def test_listing_hides_junk(vol: Path):
    (vol / "game.nsp").write_bytes(b"x")
    (vol / ".DS_Store").write_bytes(b"x")
    (vol / "Thumbs.db").write_bytes(b"x")
    (vol / "._game.nsp").write_bytes(b"x")
    (vol / "@eaDir").mkdir()
    (vol / "lost+found").mkdir()
    (vol / "#recycle").mkdir()

    listing = await files_routes.list_files(path=str(vol))
    names = {e.name for e in listing.entries}
    assert names == {"game.nsp"}


@pytest.mark.asyncio
async def test_rename_requires_confirmation_header(vol: Path):
    f = vol / "x.bin"
    f.write_bytes(b"x")

    with pytest.raises(HTTPException) as exc:
        await files_routes.rename_file(
            _request(), path=str(f), new_name="y.bin"
        )
    assert exc.value.status_code == 400
    assert "confirmation header" in exc.value.detail.lower()
    assert f.exists()
    assert not (vol / "y.bin").exists()

    result = await files_routes.rename_file(
        _request({"x-chd-action-confirm": "rename-file"}),
        path=str(f),
        new_name="y.bin",
    )
    assert result["success"] is True
    assert not f.exists()
    assert (vol / "y.bin").exists()


@pytest.mark.asyncio
async def test_delete_file_requires_confirmation_header(vol: Path):
    f = vol / "x.bin"
    f.write_bytes(b"x")

    with pytest.raises(HTTPException) as exc:
        await files_routes.delete_file(_request(), path=str(f), recursive=False)
    assert exc.value.status_code == 400
    assert "confirmation header" in exc.value.detail.lower()
    assert f.exists()


@pytest.mark.asyncio
async def test_delete_nonempty_dir_requires_recursive(vol: Path):
    d = vol / "folder"
    d.mkdir()
    (d / "a.txt").write_bytes(b"x")
    (d / "sub").mkdir()

    # recursive=False is what HTTP resolves when the param is absent; pass it
    # explicitly here since a direct call leaves Query(...) defaults unresolved.
    with pytest.raises(HTTPException) as exc:
        await files_routes.delete_file(
            _request({"x-chd-action-confirm": "delete-file"}),
            path=str(d),
            recursive=False,
        )
    assert exc.value.status_code == 409
    assert "not empty" in exc.value.detail.lower()
    assert d.exists()  # nothing deleted on the refusal

    with pytest.raises(HTTPException) as exc:
        await files_routes.delete_file(_request(), path=str(d), recursive=True)
    assert exc.value.status_code == 400
    assert "confirmation header" in exc.value.detail.lower()
    assert d.exists()

    result = await files_routes.delete_file(
        _request({"x-chd-action-confirm": "recursive-delete"}),
        path=str(d),
        recursive=True,
    )
    assert result["success"] is True
    assert not d.exists()


@pytest.mark.asyncio
async def test_delete_configured_volume_root_is_blocked(vol: Path):
    (vol / "a.txt").write_bytes(b"x")

    with pytest.raises(HTTPException) as exc:
        await files_routes.delete_file(
            _request({"x-chd-action-confirm": "recursive-delete"}),
            path=str(vol),
            recursive=True,
        )
    assert exc.value.status_code == 400
    assert "volume root" in exc.value.detail.lower()
    assert vol.exists()
    assert (vol / "a.txt").exists()


@pytest.mark.asyncio
async def test_delete_batch_blocks_configured_volume_root(vol: Path):
    # An empty configured volume root must not be removable through the batch
    # endpoint, which deletes empty directories with os.rmdir.
    for child in vol.iterdir():
        child.unlink()
    assert not any(vol.iterdir())

    response = await files_routes.delete_files_batch(
        files_routes.BulkDeleteRequest(paths=[str(vol)]),
    )
    assert response["success"] == 0
    assert response["failed"] == 1
    result = response["results"][0]
    assert result["success"] is False
    assert "volume root" in result["error"].lower()
    assert vol.exists()


@pytest.mark.asyncio
async def test_delete_empty_dir_needs_no_recursive(vol: Path):
    d = vol / "empty"
    d.mkdir()
    result = await files_routes.delete_file(
        _request({"x-chd-action-confirm": "delete-file"}),
        path=str(d),
        recursive=False,
    )
    assert result["success"] is True
    assert not d.exists()


@pytest.mark.asyncio
async def test_delete_file(vol: Path):
    f = vol / "x.bin"
    f.write_bytes(b"x")
    result = await files_routes.delete_file(
        _request({"x-chd-action-confirm": "delete-file"}),
        path=str(f),
        recursive=False,
    )
    assert result["success"] is True
    assert not f.exists()
