"""Registry-driven FileEntry output detection across the file/search paths.

Phase 7 replaced the six hand-written per-tool flag blocks in
``routes/files.py`` with a single registry loop, and Phase 9 (issue #186,
site 6) removed the legacy ``has_*`` / ``*_ready`` / ``*_convertible`` /
``*_path`` booleans entirely — the frontend reads ``convertible_by`` /
``outputs`` / ``verifiable_by`` exclusively. These tests are the safety net for
the detection itself: every branch (no output, finished output, mid-conversion
lock, self-format input, per-tool convertibility, the archive-member summary)
must be reported correctly through the registry-driven fields, and the JSON
contract must stay exactly those fields with no legacy holdovers.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.routes import files as files_routes
from app.services.lock_manager import lock_manager

# The exact FileEntry field surface the frontend reads.
FILEENTRY_KEYS = {
    "name", "path", "type", "size", "extension",
    "archive_items", "archive_has_output", "archive_truncated", "media_type",
    "convertible_by", "outputs", "verifiable_by", "split_parts",
}
# On-disk search hits mirror the file schema minus the listing-only archive
# summary / media / split fields, plus the ``in_archive`` marker.
SEARCH_FILE_KEYS = {
    "name", "path", "size", "extension", "in_archive",
    "convertible_by", "outputs", "verifiable_by",
}
# Archive containers surface from search with the same schema plus a ``type``.
SEARCH_ARCHIVE_CONTAINER_KEYS = SEARCH_FILE_KEYS | {"type"}
# Archive members carry the registry-driven flags plus archive locator keys,
# but no per-file verify gate (they can't be verified in place).
ARCHIVE_MEMBER_KEYS = {
    "name", "path", "size", "extension", "in_archive",
    "convertible_by", "outputs", "archive_path", "internal_path", "output_stem",
}


@pytest.fixture(name="parity_env")
def _parity_env(tmp_path: Path, monkeypatch):
    """A tree exercising every detection branch across the three tools."""
    # chdman-only source with no output.
    (tmp_path / "lonely.cue").write_bytes(b"cue")
    # chdman-only source with a finished output.
    (tmp_path / "done.cue").write_bytes(b"cue")
    (tmp_path / "done.chd").write_bytes(b"chd")
    # chdman-only source with a mid-conversion (locked, no file) output.
    (tmp_path / "prog.cue").write_bytes(b"cue")
    # dolphin-only source with a finished sibling output.
    (tmp_path / "disc.wbfs").write_bytes(b"wbfs")
    (tmp_path / "disc.rvz").write_bytes(b"rvz")
    # self-format dolphin input (.rvz is itself a dolphin output format).
    (tmp_path / "movie.rvz").write_bytes(b"rvz")
    # z3ds source with a finished output.
    (tmp_path / "rom.3ds").write_bytes(b"3ds")
    (tmp_path / "rom.z3ds").write_bytes(b"z3ds")

    # Archive whose member maps to an existing sibling .chd.
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("inner.iso", b"iso-bytes")
    (tmp_path / "inner.chd").write_bytes(b"chd")

    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(tmp_path))

    lock_path = str(tmp_path / "prog.chd")
    assert lock_manager.acquire_lock(lock_path) is True
    try:
        yield {"root": str(tmp_path)}
    finally:
        lock_manager.release_lock(lock_path)


# Both the listing (FileEntry.outputs) and the search dicts ("outputs" key)
# carry OutputStatus objects, so every field is attribute access.
def _by_tool(outputs: list) -> dict:
    return {o.tool_id: o for o in outputs}


@pytest.mark.asyncio
async def test_list_files_output_detection(parity_env):
    listing = await files_routes.list_files(path=parity_env["root"])
    by_name = {e.name: e for e in listing.entries}
    root = parity_env["root"]

    # No output present.
    lonely = by_name["lonely.cue"]
    assert lonely.convertible_by == ["chdman"]
    assert lonely.outputs == []

    # Finished chdman output.
    done = by_name["done.cue"]
    assert [o.tool_id for o in done.outputs] == ["chdman"]
    assert done.outputs[0].exists is True and done.outputs[0].ready is True

    # Mid-conversion chdman output (locked, file absent).
    prog = by_name["prog.cue"]
    assert prog.outputs[0].tool_id == "chdman"
    assert prog.outputs[0].exists is False and prog.outputs[0].ready is False
    assert prog.outputs[0].path == str(Path(root) / "prog.chd")

    # Finished dolphin output for a dolphin-only input (.wbfs is not
    # chdman-convertible).
    disc = by_name["disc.wbfs"]
    assert "chdman" not in disc.convertible_by
    assert _by_tool(disc.outputs)["dolphin"].exists is True
    assert _by_tool(disc.outputs)["dolphin"].path == str(Path(root) / "disc.rvz")

    # Self-format dolphin input detects itself.
    movie = by_name["movie.rvz"]
    assert _by_tool(movie.outputs)["dolphin"].path == str(Path(root) / "movie.rvz")

    # Finished z3ds output.
    rom = by_name["rom.3ds"]
    assert [o.tool_id for o in rom.outputs] == ["z3ds"]
    assert _by_tool(rom.outputs)["z3ds"].path == str(Path(root) / "rom.z3ds")

    # Archive: never emits tool outputs itself, but the registry-driven summary
    # counts the single member with a sibling output (inner.iso -> inner.chd)
    # regardless of which tool produced it — the replacement for the old
    # per-archive has_chd badge.
    bundle = by_name["bundle.zip"]
    assert bundle.type == "archive"
    assert bundle.convertible_by == []
    assert bundle.outputs == []
    assert bundle.archive_has_output == 1


@pytest.mark.asyncio
async def test_search_files_output_detection(parity_env):
    results = await files_routes.search_files(
        path=parity_env["root"], recursive=True, include_archives=True,
    )
    by_name = {Path(item["path"]).name: item for item in results["files"]}

    assert by_name["lonely.cue"]["outputs"] == []
    assert by_name["lonely.cue"]["convertible_by"] == ["chdman"]
    assert _by_tool(by_name["done.cue"]["outputs"])["chdman"].ready is True
    assert _by_tool(by_name["prog.cue"]["outputs"])["chdman"].exists is False
    assert _by_tool(by_name["disc.wbfs"]["outputs"])["dolphin"].path.endswith("disc.rvz")
    assert _by_tool(by_name["movie.rvz"]["outputs"])["dolphin"].ready is True
    assert "z3ds" in _by_tool(by_name["rom.3ds"]["outputs"])
    # The archive container surfaces as a top-level result (browse-only unless an
    # archive-direct mode like romz_extract is active) alongside its members.
    assert by_name["bundle.zip"]["type"] == "archive"
    assert by_name["bundle.zip"]["convertible_by"] == []


@pytest.mark.asyncio
async def test_list_files_json_keys(parity_env):
    """Every FileEntry carries exactly the registry-driven field surface."""
    listing = await files_routes.list_files(path=parity_env["root"])
    for entry in listing.entries:
        assert set(entry.model_dump().keys()) == FILEENTRY_KEYS


@pytest.mark.asyncio
async def test_search_files_json_keys(parity_env):
    """Search dicts carry only the registry-driven fields — no legacy holdovers."""
    results = await files_routes.search_files(
        path=parity_env["root"], recursive=True, include_archives=True,
    )
    for item in results["files"]:
        keys = set(item.keys())
        if item.get("type") == "archive":
            assert keys == SEARCH_ARCHIVE_CONTAINER_KEYS
        else:
            assert keys == SEARCH_FILE_KEYS

    for item in results["archives"]:
        assert set(item.keys()) == ARCHIVE_MEMBER_KEYS
        assert item["in_archive"] is True

    # bundle.zip::inner.iso has a sibling inner.chd, so the member surfaces as
    # CHDMAN-convertible with a finished output — and as Dolphin- and
    # CSO-convertible (.iso is accepted by all three) even though only the .chd
    # sibling exists in the fixture.
    inner = next(i for i in results["archives"] if i["name"] == "inner.iso")
    assert "chdman" in inner["convertible_by"]
    assert _by_tool(inner["outputs"])["chdman"].ready is True
    assert "dolphin" in inner["convertible_by"]
    assert "dolphin" not in _by_tool(inner["outputs"])
    assert "cso" in inner["convertible_by"]
    assert "cso" not in _by_tool(inner["outputs"])
