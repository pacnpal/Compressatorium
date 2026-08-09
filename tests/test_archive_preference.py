import sys
import zipfile
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1] / "app"))

from services import archive_members
from services.archive import archive_service
from services.tools import registry


def test_filters_bin_when_cue_present():
    entries = [
        {"internal_path": "Game/Disc 1.bin", "extension": ".bin"},
        {"internal_path": "Game/Disc 1.cue", "extension": ".cue"},
    ]
    filtered = archive_service._filter_preferred_entries(entries)
    exts = sorted(e["extension"] for e in filtered)
    assert exts == [".cue"]


def test_keeps_bin_when_no_cue_or_gdi():
    entries = [
        {"internal_path": "Game/Disc 1.bin", "extension": ".bin"},
    ]
    filtered = archive_service._filter_preferred_entries(entries)
    exts = sorted(e["extension"] for e in filtered)
    assert exts == [".bin"]


def test_archive_input_extensions_cover_all_source_tools():
    # The convert gate must accept every source convertible straight from an
    # archive: chdman create sources, Dolphin sources, 3DS sources (issue #113),
    # Switch (nsz) sources, and .chd (chdman extract decompresses a .chd pulled
    # out of an archive back to a disc image).
    exts = set(registry.archive_input_extensions())
    assert {".3ds", ".cci", ".cia"} <= exts          # 3DS (the reported gap)
    assert {".rvz", ".gcz", ".wia", ".wbfs"} <= exts  # Dolphin
    assert {".gdi", ".iso", ".cue", ".bin"} <= exts   # chdman create
    assert {".nsp", ".xci", ".nsz", ".xcz"} <= exts   # Switch (nsz)
    assert {".cso", ".zso", ".dax"} <= exts           # CSO (maxcso decompress)
    assert {".wud", ".wux"} <= exts                   # Wii U (JWUDTool)
    assert ".chd" in exts                             # chdman extract-from-archive
    assert {".nkit.iso", ".nkit.gcz"} <= exts         # NKit restore-from-archive
    # The compound source does NOT pull the generic tails in on its own: `.gcz`
    # is here because Dolphin declares it, and `.iso` because chdman/cso do.
    assert ".nkit" not in exts
    # Handheld ROMs are browse-only, not convertible in place, so they stay OUT
    # of the convert-gate set (recompressing an archived ROM would be recursive).
    assert not ({".gb", ".gbc", ".gba", ".nds"} & exts)


def test_browse_listing_is_global_known_superset_of_convert_gate():
    # The browse listing is global, scoped to known extensions: every known
    # source extension shows (so romz ROMs aren't an "Empty folder"), and it's a
    # superset of the convert-gate set the search/conversion path uses.
    listable = archive_service._listable_extensions()
    convertible = set(registry.archive_input_extensions())
    assert convertible <= listable
    # Handheld ROMs surface in browse because they're known sources...
    assert {".gb", ".gbc", ".gba", ".nds"} <= listable
    # ...but stay out of the convert-gate (browse-only, no in-place conversion).
    assert not ({".gb", ".gbc", ".gba", ".nds"} & convertible)
    # A .chd shows in browse AND is convertible from an archive: chdman extract
    # decompresses it. (It's still not a loose source — chdman drops it from its
    # input_extensions — so a .chd on disk isn't badged convertible.)
    assert ".chd" in listable
    assert ".chd" in convertible
    # Archive containers themselves aren't listed as members of an archive.
    assert not ({".zip", ".7z", ".rar"} & listable)


def test_list_zip_surfaces_handheld_rom_member(tmp_path):
    # A single-ROM archive must list its ROM (the "Empty folder" bug fix), but
    # the member is not convertible in place.
    archive = tmp_path / "Solo.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("inner.gba", b"rom")

    entries = archive_service.list_archive_contents(str(archive))
    members = {e["internal_path"]: e for e in entries}
    assert "inner.gba" in members
    assert members["inner.gba"]["extension"] == ".gba"


@pytest.mark.skipif(not archive_members.HAS_7Z, reason="py7zr not installed")
def test_list_7z_surfaces_handheld_rom_member(tmp_path):
    # Same as the .zip case, but through the py7zr handler — archive-member
    # listing goes through format-specific code, so .7z needs its own guard.
    import py7zr

    archive = tmp_path / "Solo.7z"
    with py7zr.SevenZipFile(archive, "w") as zf:
        zf.writestr(b"rom", "inner.gba")

    entries = archive_service.list_archive_contents(str(archive))
    members = {e["internal_path"]: e for e in entries}
    assert "inner.gba" in members
    assert members["inner.gba"]["extension"] == ".gba"


def test_list_zip_surfaces_chd_member_as_convertible(tmp_path):
    # A .chd packed in an archive shows when you browse in, and unlike a loose
    # .chd it's convertible there — chdman extract decompresses it back to a
    # disc image.
    archive = tmp_path / "Disc.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("disc.chd", b"chd")

    entries = archive_service.list_archive_contents(str(archive))
    members = {e["internal_path"]: e for e in entries}
    assert "disc.chd" in members
    assert members["disc.chd"]["extension"] == ".chd"
    assert "chdman" in registry.tools_accepting_archive_member(".chd")


def test_convertible_only_skips_list_only_members_before_entry_cap(
    tmp_path, monkeypatch,
):
    # Regression: a ZIP whose first rows are list-only ROMs followed by a real
    # convertible source must still cough up that source under the entry cap.
    # The convert-gated search path strips the ROMs out *before* the cap, so a
    # pile of .gba can't crowd out the .iso the way browse would have shown it.
    from services import archive as archive_module

    monkeypatch.setattr(archive_module.settings, "archive_max_entries", 2)
    archive_members.clear_cache()

    archive = tmp_path / "Pack.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for i in range(5):
            zf.writestr(f"rom{i}.gba", b"rom")  # list-only, ahead of the source
        zf.writestr("disc.iso", b"iso")          # the convertible member

    result = archive_service.list_archive_contents(
        str(archive), include_meta=True, convertible_only=True,
    )
    names = {e["internal_path"] for e in result["entries"]}
    assert "disc.iso" in names
    assert not any(n.endswith(".gba") for n in names)


def test_list_zip_surfaces_3ds_member(tmp_path):
    # A zip containing a .3ds file must list it as a convertible member,
    # mirroring how an on-disk .3ds is detected (issue #113).
    archive = tmp_path / "roms.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Game/cart.3ds", b"3ds-bytes")
        zf.writestr("readme.txt", b"ignore me")

    entries = archive_service.list_archive_contents(str(archive))
    members = {e["internal_path"]: e for e in entries}
    assert "Game/cart.3ds" in members
    assert members["Game/cart.3ds"]["extension"] == ".3ds"
    # Non-convertible members (e.g. readme.txt) must be filtered out.
    assert "readme.txt" not in members
    # output_stem flattens the subdir; the z3ds output extension is resolved
    # later from the member's real extension via _output_name_for_member.
    assert members["Game/cart.3ds"]["output_stem"] == "Game_cart"
    assert archive_service._output_name_for_member("Game/cart.3ds") == "Game_cart.3ds"
