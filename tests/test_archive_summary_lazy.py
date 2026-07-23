"""Lazy archive summaries: the /archive-summary batch and its shared cache.

`/api/files?summarize_archives=false` returns archive rows without member counts
or `verifiable_by` so a folder of thousands of archives lists instantly; the
browser then hydrates those badges via `/api/archive-summary`. These tests pin:

1. The lazy listing really defers the per-archive fields.
2. The batch reproduces exactly what the inline `summarize_archives=true`
   listing would have reported (parity), so the deferred hydration is
   behaviour-preserving.
3. The batch degrades per-path (bad/missing/non-archive paths get an error
   field) instead of failing the whole request.
4. The shared `archive_members` reader opens an archive once and re-reads it
   only after its bytes change (mtime/size key).
"""
from __future__ import annotations

import zipfile

import pytest

from app.routes import files as files_routes
from app.services import archive_members
from app.services.archive import archive_service
from models import MetadataBatchRequest


@pytest.fixture(name="summary_env")
def _summary_env(tmp_path, monkeypatch):
    # chdman-convertible archive member (.iso) -> archive_items == 1.
    with zipfile.ZipFile(tmp_path / "Disc.zip", "w") as zf:
        zf.writestr("game.iso", b"iso-bytes")
    # Single handheld ROM -> archive_items == 1 (the .gba is a known source, so
    # browse lists it for visibility, though it's not convertible in place) and
    # verifiable_by == ["romz"].
    with zipfile.ZipFile(tmp_path / "Solo.zip", "w") as zf:
        zf.writestr("inner.gba", b"rom")
    # Multi-file archive: the ROM is listed but notes.txt isn't, so
    # archive_items == 1; still not romz-verifiable (more than one member).
    with zipfile.ZipFile(tmp_path / "Multi.zip", "w") as zf:
        zf.writestr("Game.gba", b"rom")
        zf.writestr("notes.txt", b"hello")

    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(tmp_path))
    archive_members.clear_cache()
    return {"root": str(tmp_path)}


@pytest.mark.asyncio
async def test_lazy_listing_defers_archive_fields(summary_env):
    listing = await files_routes.list_files(
        path=summary_env["root"], summarize_archives=False,
    )
    archives = {e.name: e for e in listing.entries if e.type == "archive"}
    assert set(archives) == {"Disc.zip", "Solo.zip", "Multi.zip"}
    for entry in archives.values():
        assert entry.archive_items is None
        assert entry.archive_has_output is None
        assert entry.archive_truncated is None
        assert entry.verifiable_by == []


@pytest.mark.asyncio
async def test_batch_matches_inline_summary(summary_env):
    root = summary_env["root"]
    inline = await files_routes.list_files(path=root, summarize_archives=True)
    inline_by_path = {e.path: e for e in inline.entries if e.type == "archive"}

    summary = await files_routes.archive_summary_batch(
        MetadataBatchRequest(paths=list(inline_by_path)),
    )

    for path, entry in inline_by_path.items():
        got = summary[path]
        assert "error" not in got
        assert got["archive_items"] == entry.archive_items
        assert got["archive_has_output"] == entry.archive_has_output
        assert got["archive_truncated"] == entry.archive_truncated
        assert got["verifiable_by"] == entry.verifiable_by

    # Spot-check the meaningful distinctions the badges rely on.
    disc = summary[f"{root}/Disc.zip"]
    assert disc["archive_items"] == 1
    solo = summary[f"{root}/Solo.zip"]
    assert solo["archive_items"] == 1
    assert solo["verifiable_by"] == ["romz"]
    multi = summary[f"{root}/Multi.zip"]
    assert multi["archive_items"] == 1
    assert multi["verifiable_by"] == []


@pytest.mark.asyncio
async def test_batch_reports_per_path_errors(summary_env):
    root = summary_env["root"]
    summary = await files_routes.archive_summary_batch(
        MetadataBatchRequest(paths=[
            f"{root}/missing.zip",
            f"{root}/Disc.zip",
            "/etc/passwd",
        ]),
    )
    assert summary[f"{root}/missing.zip"]["error"] == "file_not_found"
    assert "error" not in summary[f"{root}/Disc.zip"]
    assert summary["/etc/passwd"]["error"] == "path_outside_configured_volumes"


@pytest.mark.asyncio
async def test_batch_caps_path_count(summary_env, monkeypatch):
    root = summary_env["root"]
    # Cap at 1 so the second path is rejected without doing its archive work.
    monkeypatch.setattr(files_routes, "MAX_ARCHIVE_SUMMARY_PATHS", 1)
    summary = await files_routes.archive_summary_batch(
        MetadataBatchRequest(paths=[f"{root}/Disc.zip", f"{root}/Solo.zip"]),
    )
    assert "error" not in summary[f"{root}/Disc.zip"]
    assert summary[f"{root}/Solo.zip"]["error"] == "batch_limit_exceeded"


def test_oversized_listings_are_not_cached(tmp_path, monkeypatch):
    archive_members.clear_cache()
    archive_path = str(tmp_path / "Many.zip")
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("game.iso", b"iso")

    # Force the "too many members to cache" branch for any non-empty archive.
    monkeypatch.setattr(archive_members, "MAX_CACHED_MEMBERS", 0)
    calls = {"n": 0}
    original = archive_members._read_uncached

    def counting(path):
        calls["n"] += 1
        return original(path)

    monkeypatch.setattr(archive_members, "_read_uncached", counting)

    archive_members.read_archive_members(archive_path)
    archive_members.read_archive_members(archive_path)
    # Not cached (over the member cap), so each read re-opens the archive.
    assert calls["n"] == 2


def test_get_member_size_uses_last_duplicate(tmp_path):
    # A ZIP may carry duplicate member names; zipfile.open()/extraction resolves
    # to the LAST one, so the size guard must report that one's size — not the
    # first, smaller duplicate (which could let a size-limit check pass while a
    # larger trailing payload is extracted).
    archive_members.clear_cache()
    archive_path = str(tmp_path / "dup.zip")
    big = b"MUCH-LARGER-PAYLOAD"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("game.iso", b"sm")
        zf.writestr("game.iso", big)
    assert archive_service._get_member_size(archive_path, "game.iso") == len(big)


def test_shared_reader_caches_until_bytes_change(tmp_path, monkeypatch):
    archive_members.clear_cache()
    archive_path = str(tmp_path / "Pack.zip")
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("game.iso", b"v1")

    calls = {"n": 0}
    original = archive_members._read_uncached

    def counting(path):
        calls["n"] += 1
        return original(path)

    monkeypatch.setattr(archive_members, "_read_uncached", counting)

    first = archive_members.read_archive_members(archive_path)
    second = archive_members.read_archive_members(archive_path)
    assert calls["n"] == 1          # second call served from cache
    assert first == second

    # Rewriting with different bytes changes size -> cache key invalidates.
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("game.iso", b"v2-longer-content")
    archive_members.read_archive_members(archive_path)
    assert calls["n"] == 2
