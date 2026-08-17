"""Tests for the romz service (handheld ROM .7z/.zip packer).

The ``7z`` binary isn't available in the sandbox, so the subprocess paths
(compress / extract / verify) are exercised by stubbing the shared
``SubprocessRunner`` the service drives, while the pure read-side logic
(member listing, single-member validation, info, output paths) runs against
real ``zipfile`` archives created with the stdlib.
"""
from __future__ import annotations

import asyncio
import os
import zipfile
from pathlib import Path

import pytest

from app.services import archive_members
from app.services import romz as romz_mod
from app.services.romz import RomzService, _compress_flags, _parse_progress


def _make_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


# ----- pure helpers --------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "preset", "must_contain"),
    [
        ("romz_7z", "max", ["-t7z", "-m0=lzma2", "-mx=9", "-md=256m", "-mfb=273"]),
        ("romz_7z", "default", ["-t7z", "-m0=lzma2", "-mx=7", "-md=64m"]),
        ("romz_7z", "fast", ["-t7z", "-m0=lzma2", "-mx=1"]),
        ("romz_7z", None, ["-mx=9", "-md=256m"]),       # default token -> max
        ("romz_zip", "max", ["-tzip", "-mx=9"]),
        ("romz_zip", "fast", ["-tzip", "-mx=1"]),
    ],
)
def test_compress_flags(mode, preset, must_contain):
    flags = _compress_flags(mode, preset)
    for token in must_contain:
        assert token in flags
    # zip never uses the lzma2 dictionary knobs.
    if mode == "romz_zip":
        assert not any(f.startswith("-md=") for f in flags)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (" 45% 3 - game.gba", 45),
        ("100%", 100),
        ("Compressing game.gba", None),
        ("250%", None),  # out of range
    ],
)
def test_parse_progress(line, expected):
    assert _parse_progress(line) == expected


@pytest.mark.parametrize(
    ("mode", "name", "output_dir", "expected_name"),
    [
        ("romz_7z", "Game.gba", None, "Game.gba.7z"),
        ("romz_zip", "Game.nds", None, "Game.nds.zip"),
        ("romz_7z", "Game.gb", "/out", "Game.gb.7z"),
    ],
)
def test_compress_output_path_preserves_rom_extension(
    mode, name, output_dir, expected_name,
):
    out = RomzService.get_output_path_for_mode(mode, f"/data/{name}", output_dir)
    assert Path(out).name == expected_name
    if output_dir:
        assert out.startswith(output_dir)


# ----- archive read side (real zipfile) ------------------------------------


def test_single_rom_member_ok(tmp_path):
    archive = _make_zip(tmp_path / "Game.gba.7z.zip", {"Game.gba": b"ROMDATA"})
    # extension is .zip here so listing uses zipfile
    assert RomzService._single_rom_member(str(archive)) == "Game.gba"


def test_single_rom_member_ignores_os_sidecars(tmp_path):
    # A ROM zipped on macOS/Windows carries junk alongside the payload; the
    # shared junk filter means a single ROM still counts as one member.
    archive = _make_zip(
        tmp_path / "mac.zip",
        {
            "Game.gba": b"ROMDATA",
            "__MACOSX/._Game.gba": b"meta",
            ".DS_Store": b"x",
            "Thumbs.db": b"x",
        },
    )
    assert RomzService._single_rom_member(str(archive)) == "Game.gba"


def test_single_rom_member_enforces_archive_limits(tmp_path, monkeypatch):
    # Deployments that cap archive entries (zip-bomb guard) must apply to romz
    # extraction even though it shells out to 7z instead of the archive service.
    from app.services.archive import settings as arch_settings
    monkeypatch.setattr(arch_settings, "archive_max_entries", 1, raising=False)
    archive = _make_zip(tmp_path / "many.zip", {"Game.gba": b"ROM", "extra.dat": b"x"})
    with pytest.raises(ValueError, match="max entries"):
        RomzService._single_rom_member(str(archive))


def test_is_single_rom_archive_true_for_single_rom(tmp_path):
    archive = _make_zip(tmp_path / "Game.gba.zip", {"Game.gba": b"ROMDATA"})
    assert RomzService.is_single_rom_archive(str(archive)) is True


def test_is_single_rom_archive_ignores_os_sidecars(tmp_path):
    # Same junk tolerance as _single_rom_member: a ROM zipped on macOS still
    # counts as one member, so it stays romz-ready in the listing.
    archive = _make_zip(
        tmp_path / "mac.zip",
        {"Game.gba": b"ROMDATA", "__MACOSX/._Game.gba": b"meta", ".DS_Store": b"x"},
    )
    assert RomzService.is_single_rom_archive(str(archive)) is True


def test_is_single_rom_archive_false_for_multifile(tmp_path):
    archive = _make_zip(tmp_path / "two.zip", {"a.gba": b"a", "b.txt": b"b"})
    assert RomzService.is_single_rom_archive(str(archive)) is False


def test_is_single_rom_archive_false_for_no_rom(tmp_path):
    archive = _make_zip(tmp_path / "doc.zip", {"readme.txt": b"hi"})
    assert RomzService.is_single_rom_archive(str(archive)) is False


def test_is_single_rom_archive_false_for_corrupt(tmp_path):
    # Unreadable/non-archive bytes must not raise — just "not romz-ready".
    bogus = tmp_path / "broken.zip"
    bogus.write_bytes(b"not a real zip")
    assert RomzService.is_single_rom_archive(str(bogus)) is False


def test_is_single_rom_archive_false_for_traversal_member(tmp_path):
    # A single ROM member, but with a `..` path verify/extract would reject:
    # the gate must agree (mustn't offer Verify on an archive verify rejects).
    archive = _make_zip(tmp_path / "evil.zip", {"../Game.gba": b"ROM"})
    assert RomzService.is_single_rom_archive(str(archive)) is False


def test_is_single_rom_archive_false_when_over_limit(tmp_path, monkeypatch):
    # One ROM member (junk ignored by the single-ROM resolve) but the archive
    # trips the shared entry limit. verify/extract enforce the limit before
    # running 7z, so the listing gate must too.
    from app.services.archive import settings as arch_settings
    monkeypatch.setattr(arch_settings, "archive_max_entries", 1, raising=False)
    archive = _make_zip(
        tmp_path / "limited.zip",
        {"Game.gba": b"ROM", "__MACOSX/._Game.gba": b"meta"},
    )
    assert RomzService.is_single_rom_archive(str(archive)) is False


def test_info_ignores_sidecars(tmp_path):
    archive = _make_zip(
        tmp_path / "mac.zip",
        {"__MACOSX/._Game.gba": b"meta", "Game.gba": b"ROMDATA"},
    )
    info = romz_mod.romz_service.info(str(archive))
    assert info["contained_name"] == "Game.gba"


def test_single_rom_member_rejects_no_rom(tmp_path):
    archive = _make_zip(tmp_path / "doc.zip", {"readme.txt": b"hi"})
    with pytest.raises(ValueError, match="no Game Boy"):
        RomzService._single_rom_member(str(archive))


def test_single_rom_member_rejects_multifile(tmp_path):
    archive = _make_zip(
        tmp_path / "two.zip", {"a.gba": b"a", "b.txt": b"b"},
    )
    with pytest.raises(ValueError, match="more than one file"):
        RomzService._single_rom_member(str(archive))


def test_single_rom_member_rejects_traversal_rom(tmp_path):
    """A ROM member with a `..`/absolute path is rejected before `7z x` runs,
    so extraction can't write outside the temp dir."""
    archive = _make_zip(tmp_path / "evil.zip", {"../evil.gba": b"ROM"})
    with pytest.raises(ValueError):
        RomzService._single_rom_member(str(archive))


def test_single_rom_member_rejects_symlink(tmp_path):
    """A symlink member masquerading as the ROM is rejected before `7z x`, so a
    crafted archive can't produce a link to an arbitrary path as the output."""
    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        info = zipfile.ZipInfo("Game.gba")
        info.external_attr = 0o120777 << 16  # S_IFLNK
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(ValueError, match="symlink"):
        RomzService._single_rom_member(str(archive))


def test_single_rom_member_counts_directory_entries(tmp_path, monkeypatch):
    """Directory entries count toward the archive-entry limit: `7z x`
    materializes the whole tree, so a single-ROM archive padded with many
    directories must not slip past CHD_ARCHIVE_MAX_ENTRIES."""
    from app.services.archive import settings as arch_settings
    monkeypatch.setattr(arch_settings, "archive_max_entries", 2, raising=False)
    archive = tmp_path / "dirs.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Game.gba", b"ROM")
        for d in ("a/", "b/", "c/"):  # 1 file + 3 dirs = 4 entries > limit 2
            zf.writestr(d, b"")
    with pytest.raises(ValueError, match="max entries"):
        RomzService._single_rom_member(str(archive))


def test_single_rom_member_resolves_with_directory_entries(tmp_path):
    """A ROM stored under a directory still resolves (directories are not
    payloads); the nested path is returned for `7z x` to recreate."""
    archive = tmp_path / "nested.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("roms/", b"")
        zf.writestr("roms/Game.gba", b"ROM")
    assert RomzService._single_rom_member(str(archive)) == "roms/Game.gba"


def test_single_rom_member_rejects_traversal_junk(tmp_path):
    """A junk member with a traversal path is rejected too: `7z x` extracts
    every member, so an unsafe sidecar can't be silently ignored."""
    archive = _make_zip(
        tmp_path / "g.zip",
        {"Game.gba": b"ROM", "__MACOSX/../../victim": b"junk"},
    )
    with pytest.raises(ValueError):
        RomzService._single_rom_member(str(archive))


def test_single_rom_member_rejects_interior_dotdot(tmp_path):
    """A `..` component is rejected even when it normalizes back inside the root
    (`__MACOSX/../Game.gba` -> `Game.gba`): `7z x` recreates the literal path,
    which would resolve onto the validated ROM and overwrite it."""
    archive = _make_zip(
        tmp_path / "g.zip",
        {"Game.gba": b"ROM", "__MACOSX/../Game.gba": b"evil"},
    )
    with pytest.raises(ValueError):
        RomzService._single_rom_member(str(archive))


def test_single_rom_member_allows_posix_special_chars(tmp_path):
    """Legal POSIX filename characters (`:`/`\\`) are not traversal escapes, so
    a ROM named with them must still resolve — otherwise an archive this tool
    produced from such a source could never be verified/extracted."""
    # Distinct archive paths per case: the shared member reader is cached by
    # (path, mtime, ctime, size), so rewriting one path twice with same-size
    # contents could, on a coarse-timestamp filesystem, return the first
    # listing for the second read. Separate paths keep this test about the
    # special-char resolution, independent of cache/timestamp granularity.
    for idx, name in enumerate(("Game:1.gba", "Game\\x.gba")):
        archive = _make_zip(tmp_path / f"weird{idx}.zip", {name: b"ROM"})
        assert RomzService._single_rom_member(str(archive)) == name


def test_extract_output_path_uses_archived_member_basename(tmp_path):
    archive = _make_zip(tmp_path / "Game.zip", {"Real Name.gba": b"ROM"})
    out = RomzService.get_output_path_for_mode(
        "romz_extract", str(archive), str(tmp_path / "out"),
    )
    assert Path(out).name == "Real Name.gba"


def test_extract_output_path_falls_back_when_unreadable(tmp_path):
    missing = tmp_path / "Game.gba.7z"  # no such file
    out = RomzService.get_output_path_for_mode("romz_extract", str(missing))
    # Falls back to stripping the archive suffix (this tool's own convention).
    assert Path(out).name == "Game.gba"


def test_extract_output_path_falls_back_on_corrupt_archive(tmp_path):
    # A corrupt archive raises zipfile.BadZipFile / py7zr errors, which do NOT
    # derive from OSError/ValueError/RuntimeError; the broad fallback must still
    # produce a path (stripped suffix) instead of crashing.
    corrupt = tmp_path / "Game.gba.zip"
    corrupt.write_bytes(b"not a real zip file at all")
    out = RomzService.get_output_path_for_mode("romz_extract", str(corrupt))
    assert Path(out).name == "Game.gba"


def test_info_for_archive_reports_contained_rom_and_ratio(tmp_path):
    archive = _make_zip(tmp_path / "Game.zip", {"Game.gba": b"X" * 1000})
    info = romz_mod.romz_service.info(str(archive))
    assert info["compressed"] is True
    assert info["extension"] == ".zip"
    assert info["contained_name"] == "Game.gba"
    assert info["original_size"] == 1000
    assert info["ratio"] is not None


def test_info_for_loose_rom(tmp_path):
    rom = tmp_path / "Game.gba"
    rom.write_bytes(b"ROM")
    info = romz_mod.romz_service.info(str(rom))
    assert info["compressed"] is False
    assert info["format"] == "Game Boy Advance ROM"
    assert info["contained_name"] is None


def test_info_non_rom_archive_falls_back_to_basic(tmp_path):
    """An ordinary archive (no single ROM payload) must not report ROM fields.

    Routing is extension-based, so File Info is asked about every .zip/.7z; a
    readme + artwork archive should fall back to basic archive info instead of
    mislabelling its first member as the contained ROM.
    """
    archive = _make_zip(
        tmp_path / "stuff.zip", {"readme.txt": b"hi", "art.png": b"x" * 50},
    )
    info = romz_mod.romz_service.info(str(archive))
    assert info["compressed"] is True
    assert info["contained_name"] is None
    assert info["original_size"] is None
    assert info["ratio"] is None


@pytest.mark.skipif(not archive_members.HAS_7Z, reason="py7zr not installed")
def test_single_rom_member_reads_real_7z(tmp_path):
    """Exercise the py7zr listing branch with a genuine .7z archive."""
    import py7zr

    archive = tmp_path / "Game.7z"
    with py7zr.SevenZipFile(archive, "w") as zf:
        zf.writestr(b"ROMDATA", "Game.nds")
    assert RomzService._single_rom_member(str(archive)) == "Game.nds"
    info = romz_mod.romz_service.info(str(archive))
    assert info["compressed"] is True
    assert info["contained_name"] == "Game.nds"


# ----- convert / verify (stubbed runner) -----------------------------------


@pytest.fixture
def stub_runner(monkeypatch):
    """Replace the service's SubprocessRunner.run/run_capture with recorders."""
    calls: dict[str, object] = {}

    async def fake_run(cmd, *, input_path, output_path, parse_progress, mode=None,
                       cancel_event=None, cwd=None, fail_label="",
                       complete_message=""):
        calls["mode"] = mode
        calls["run_cmd"] = cmd
        calls["output_path"] = output_path
        calls["cwd"] = cwd
        # Simulate the binary producing the output file.
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"OUT")
        yield {"progress": 100, "message": complete_message}

    async def fake_capture(cmd, *, timeout=None, cancel_event=None,
                           stderr_to_stdout=False, fail_label=None):
        calls["capture_cmd"] = cmd
        return calls.get("capture_rc", 0), calls.get("capture_out", b"Everything is Ok"), b""

    svc = romz_mod.romz_service
    monkeypatch.setattr(svc._runner, "run", fake_run)
    monkeypatch.setattr(svc._runner, "run_capture", fake_capture)
    return svc, calls


def test_convert_compress_builds_7z_command_and_clears_stale_output(
    tmp_path, stub_runner,
):
    svc, calls = stub_runner
    rom = tmp_path / "Game.gba"
    rom.write_bytes(b"ROM")
    out = tmp_path / "Game.gba.7z"
    out.write_bytes(b"STALE")  # must be removed before `7z a` appends

    async def _drain():
        return [u async for u in svc.convert(str(rom), str(out), "romz_7z",
                                             compression="max")]

    updates = asyncio.run(_drain())
    assert updates[-1]["progress"] == 100
    cmd = calls["run_cmd"]
    assert "a" in cmd and str(out) in cmd
    # 7z runs from the ROM's directory and adds only the basename, so the
    # archive stores a root-level `Game.gba`, not the absolute volume path.
    assert calls["cwd"] == str(tmp_path)
    assert calls["mode"] == "romz_7z"
    # The basename is added with a `./` prefix (literal-safe vs 7z's `@`
    # list-file syntax) and never the absolute path.
    assert os.path.join(".", "Game.gba") in cmd and str(rom) not in cmd
    assert "-mx=9" in cmd and "-mfb=273" in cmd
    # The stale archive was replaced (not appended to).
    assert out.read_bytes() == b"OUT"


def test_convert_compress_at_prefixed_rom_is_literal_safe(tmp_path, stub_runner):
    """A ROM literally named `@Game.gba` is added as `./@Game.gba`, so 7z can't
    mistake it for a `@list-file` argument."""
    svc, calls = stub_runner
    rom = tmp_path / "@Game.gba"
    rom.write_bytes(b"ROM")
    out = tmp_path / "@Game.gba.7z"

    async def _drain():
        return [u async for u in svc.convert(str(rom), str(out), "romz_7z")]

    asyncio.run(_drain())
    cmd = calls["run_cmd"]
    assert os.path.join(".", "@Game.gba") in cmd
    # No bare positional begins with `@` (which 7z reads as a list-file).
    assert not any(isinstance(a, str) and a.startswith("@") for a in cmd)


def test_convert_extract_resolves_member_and_runs_7z_x(tmp_path, stub_runner):
    svc, calls = stub_runner
    archive = _make_zip(tmp_path / "Game.zip", {"Game.gba": b"ROM"})
    out = tmp_path / "out" / "Game.gba"

    async def _drain():
        return [u async for u in svc.convert(str(archive), str(out), "romz_extract")]

    asyncio.run(_drain())
    cmd = calls["run_cmd"]
    # `x` (preserve paths), not `e` (flatten): the member name is NOT passed as
    # a positional selector (a leading "@" would be read as a 7-Zip list-file
    # even after "--"), and preserving paths stops a same-basename junk member
    # from overwriting the ROM at the temp path. We extract the whole (already
    # validated single-ROM) archive into an isolated temp dir.
    assert "x" in cmd and "e" not in cmd
    assert "Game.gba" not in cmd
    assert str(archive) in cmd          # the archive is the only positional
    assert any(c.startswith("-o") for c in cmd)
    # Switches are separated from positional names by a literal "--".
    assert "--" in cmd
    assert cmd.index("--") < cmd.index(str(archive))
    # The runner writes the member into a temp extract dir, not the final path.
    assert ".romz-extract-" in calls["output_path"]
    # The extracted ROM still ends up at the requested output path.
    assert out.read_bytes() == b"OUT"


def test_convert_extract_renamed_output_does_not_clobber(tmp_path, stub_runner):
    """duplicate_action=rename: extract to the renamed path, never the sibling."""
    svc, _ = stub_runner
    archive = _make_zip(tmp_path / "Game.zip", {"Game.gba": b"NEW"})
    existing = tmp_path / "Game.gba"
    existing.write_bytes(b"OLD")          # pre-existing sibling must survive
    renamed = tmp_path / "Game_1.gba"     # the planned (deduped) output

    async def _drain():
        return [u async for u in svc.convert(str(archive), str(renamed), "romz_extract")]

    asyncio.run(_drain())
    assert existing.read_bytes() == b"OLD"   # untouched, not clobbered
    assert renamed.read_bytes() == b"OUT"    # extracted content moved here
    # No leftover temp dirs in the output directory.
    assert not any(p.name.startswith(".romz-extract-") for p in tmp_path.iterdir())


def test_convert_extract_moves_member_from_preserved_relpath(tmp_path, stub_runner):
    """`7z x` preserves member paths and we move from that relpath, so a
    same-basename junk member can't overwrite the ROM at a flattened temp path.
    """
    svc, calls = stub_runner
    # ROM nested under a directory; the move source must keep that relative path.
    archive = _make_zip(tmp_path / "Game.zip", {"roms/Game.gba": b"ROM"})
    out = tmp_path / "out" / "Game.gba"

    async def _drain():
        return [u async for u in svc.convert(str(archive), str(out), "romz_extract")]

    asyncio.run(_drain())
    cmd = calls["run_cmd"]
    assert "x" in cmd and "e" not in cmd
    # `-o` is the temp ROOT; `7z x` recreates the member's relative path under
    # it, so the ROM lands at <root>/roms/Game.gba (== runner_output). If `-o`
    # were <root>/roms the file would land at <root>/roms/roms/Game.gba and the
    # later os.replace would miss it.
    out_root = next(c[2:] for c in cmd if c.startswith("-o"))
    assert calls["output_path"] == os.path.join(out_root, "roms", "Game.gba")
    assert ".romz-extract-" in calls["output_path"]
    assert out.read_bytes() == b"OUT"


def test_convert_cleans_partial_output_on_failure(tmp_path, monkeypatch):
    svc = romz_mod.romz_service
    rom = tmp_path / "Game.gba"
    rom.write_bytes(b"ROM")
    out = tmp_path / "Game.gba.7z"

    async def boom(cmd, *, input_path, output_path, parse_progress, mode=None,
                   cancel_event=None, cwd=None, fail_label="",
                   complete_message=""):
        assert mode == "romz_7z"
        Path(output_path).write_bytes(b"PARTIAL")
        raise RuntimeError("7z failed")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(svc._runner, "run", boom)

    async def _drain():
        return [u async for u in svc.convert(str(rom), str(out), "romz_7z")]

    with pytest.raises(RuntimeError, match="7z failed"):
        asyncio.run(_drain())
    assert not out.exists()  # partial archive removed


def test_verify_pass(tmp_path, stub_runner):
    svc, calls = stub_runner
    archive = _make_zip(tmp_path / "Game.zip", {"Game.gba": b"ROM"})
    result = asyncio.run(svc.verify(str(archive)))
    assert result["valid"] is True
    assert calls["capture_cmd"][:2] == [svc.sevenzip_path, "t"]


def test_verify_fail(tmp_path, stub_runner):
    svc, calls = stub_runner
    calls["capture_rc"] = 2
    calls["capture_out"] = b"ERROR: CRC failed"
    archive = _make_zip(tmp_path / "Game.zip", {"Game.gba": b"ROM"})
    result = asyncio.run(svc.verify(str(archive)))
    assert result["valid"] is False
    assert "failed" in result["message"].lower()


def test_verify_rejects_non_archive(tmp_path, stub_runner):
    svc, _ = stub_runner
    rom = tmp_path / "Game.gba"
    rom.write_bytes(b"ROM")
    result = asyncio.run(svc.verify(str(rom)))
    assert result["valid"] is False
    assert "extension" in result["message"].lower()


def test_verify_rejects_multifile_archive(tmp_path, stub_runner):
    # Routing is extension-based, so an arbitrary multi-file/source zip must not
    # be marked "Verified": reject before `7z t` runs.
    svc, calls = stub_runner
    archive = _make_zip(tmp_path / "two.zip", {"a.gba": b"a", "b.txt": b"b"})
    result = asyncio.run(svc.verify(str(archive)))
    assert result["valid"] is False
    assert "capture_cmd" not in calls  # 7z t never invoked


def test_verify_passes_dash_safe_separator(tmp_path, stub_runner):
    svc, calls = stub_runner
    archive = _make_zip(tmp_path / "Game.zip", {"Game.gba": b"ROM"})
    asyncio.run(svc.verify(str(archive)))
    assert calls["capture_cmd"][:3] == [svc.sevenzip_path, "t", "--"]
