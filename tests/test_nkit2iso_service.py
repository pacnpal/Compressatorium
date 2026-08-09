"""Tests for the nkit2iso tool (NKit-shrunk GC/Wii image -> full .iso).

Covers the pieces a mocked-subprocess test can actually prove: the compound
extension seam (``.nkit.iso`` / ``.nkit.gcz`` are distinct from the generic
``.iso`` / ``.gcz`` CHDMAN and Dolphin own), output-path naming, argv +
recovery-mode selection, progress parsing, the not-bit-exact message, cancel
cleanup, and the registry/listing wiring.
"""
from __future__ import annotations

import asyncio
import struct
import zlib
from pathlib import Path

import pytest

from app.routes import convert as convert_routes
from app.routes import files as files_routes
from app.services.nkit2iso import (
    NKIT2ISO_CONVERTIBLE_EXTENSIONS,
    ConversionCancelled,
    NkitHeaderError,
    nkit2iso_service,
    read_nkit_header,
)
from app.services.tools import registry


# --------------------------------------------------------------------------- #
# Compound-extension matching
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Game.nkit.iso", True),
        ("Game.nkit.gcz", True),
        ("GAME.NKIT.ISO", True),
        # The generic tails belong to CHDMAN / Dolphin / maxcso, not to us.
        ("Game.iso", False),
        ("Game.gcz", False),
        # ".nkit" alone is not a container we can read.
        ("Game.nkit", False),
        # A file literally named "nkit.iso" has no stem-plus-compound suffix.
        ("nkit.iso", False),
    ],
)
def test_is_convertible(name, expected):
    assert nkit2iso_service.is_convertible(name) is expected


def test_registry_claims_nkit_without_displacing_generic_tail():
    ids = sorted(t.id for t in registry.tools_for_input("Game.nkit.iso"))
    assert "nkit" in ids
    # The generic-tail owners still see it — the user picks a tool.
    assert {"chdman", "cso", "dolphin"} <= set(ids)
    # ...and a plain .iso never offers the NKit restore.
    assert "nkit" not in {t.id for t in registry.tools_for_input("Game.iso")}


def test_mode_spec():
    spec = registry.spec("nkit_restore")
    assert spec.tool_id == "nkit"
    assert spec.output_ext == ".iso"
    assert spec.input_extensions == frozenset(NKIT2ISO_CONVERTIBLE_EXTENSIONS)
    assert spec.allows_archive_input is True
    # No verify route, so the source can never be confirmed-then-deleted.
    assert spec.supports_delete_on_verify is False
    assert registry.get("nkit").verify_extensions == frozenset()


# --------------------------------------------------------------------------- #
# Output paths
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("inp", "expected"),
    [
        ("/data/Game.nkit.iso", "/data/Game.iso"),
        ("/data/Game.nkit.gcz", "/data/Game.iso"),
        # Archive members arrive flattened but keep their compound extension.
        ("games_Game.nkit.iso", "games_Game.iso"),
    ],
)
def test_output_path_strips_whole_compound_extension(inp, expected):
    assert nkit2iso_service.get_output_path(inp) == expected
    # Path.stem would leave "Game.nkit" and the "output" would be the input.
    assert nkit2iso_service.get_output_path(inp) != inp


def test_output_path_honours_output_dir():
    assert nkit2iso_service.get_output_path(
        "/data/Game.nkit.iso", "/out",
    ) == "/out/Game.iso"


def test_plugin_output_path_delegates():
    assert registry.for_mode("nkit_restore").output_path(
        "nkit_restore", "/data/Game.nkit.gcz",
    ) == "/data/Game.iso"


# --------------------------------------------------------------------------- #
# Command construction
# --------------------------------------------------------------------------- #


def test_build_command_defaults_to_offline_recovery(monkeypatch):
    monkeypatch.setattr(nkit2iso_service, "nkit2iso_path", "/usr/local/bin/nkit2iso")
    monkeypatch.setattr(convert_routes.settings, "nkit2iso_recovery", "none")
    cmd = nkit2iso_service._build_command("/in/Game.nkit.iso", "/out/Game.iso")
    # Ignore any ionice wrapper the shared priority policy prepends.
    assert cmd[-8:] == [
        "/usr/local/bin/nkit2iso",
        "-i", "/in/Game.nkit.iso",
        "-o", "/out/Game.iso",
        "-f",
        "-recovery", "none",
    ]


def test_build_command_honours_download_recovery(monkeypatch):
    monkeypatch.setattr(convert_routes.settings, "nkit2iso_recovery", "download")
    assert nkit2iso_service._build_command("/in/a.nkit.iso", "/out/a.iso")[-1] == (
        "download"
    )


def test_build_command_never_passes_the_interactive_ask_mode(monkeypatch):
    # nkit2iso's own default is "ask", which prompts on a terminal and would
    # hang a job worker behind a pipe. A bypassed/invalid setting must fall back
    # to the offline mode, never reach the binary as-is.
    monkeypatch.setattr(convert_routes.settings, "nkit2iso_recovery", "ask")
    assert nkit2iso_service._build_command("/in/a.nkit.iso", "/out/a.iso")[-1] == "none"


# --------------------------------------------------------------------------- #
# Progress parsing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # The tool draws "\r  ' 42%"; SubprocessRunner normalizes \r to \n and
        # strips, so each redraw lands here as a bare percentage.
        ("0%", 0),
        ("42%", 42),
        ("100%", 100),
        ("Restoring a.nkit.iso -> a.iso", None),
        ("CRC32 099E2C6D  MATCH (redump-verified)", None),
        ("", None),
    ],
)
def test_parse_progress(line, expected):
    assert nkit2iso_service._parse_progress(line) == expected


def test_parse_progress_reads_the_binarys_real_redraw_bytes():
    """End-to-end on the *byte stream* nkit2iso actually writes.

    The tool draws its bar with ``fmt.Fprintf(os.Stderr, "\\r  %3d%%", pct)`` —
    carriage-return redraws with no newline until the run ends. The runner folds
    stderr into stdout and segments with ``_split_stream_lines``, so this pins
    the two halves together: change either and a silent 0%-forever bar would
    otherwise be the only symptom.
    """
    from app.services.subprocess_runner import _split_stream_lines

    raw = "Restoring a.nkit.iso -> a.iso\n" + "".join(
        f"\r  {pct:3d}%" for pct in (0, 7, 42, 100)
    ) + "\n"
    lines, remainder = _split_stream_lines(raw)
    assert remainder == ""
    assert [nkit2iso_service._parse_progress(line) for line in lines] == [
        None, 0, 7, 42, 100,
    ]


# --------------------------------------------------------------------------- #
# Convert (mocked subprocess)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_convert_streams_progress_and_completes(tmp_path, monkeypatch):
    source = tmp_path / "Game.nkit.iso"
    source.write_bytes(b"nkit")
    out_iso = str(tmp_path / "Game.iso")
    captured: dict = {}

    async def fake_run(cmd, *, output_path, parse_progress, complete_message,
                       **_kwargs):
        captured["cmd"] = cmd
        Path(output_path).write_bytes(b"restored")
        for line in ("Restoring", "10%", "100%"):
            pct = parse_progress(line)
            yield {"progress": pct if pct is not None else 1, "message": line}
        yield {"progress": 100, "message": "CRC32 099E2C6D  MATCH (redump-verified)"}
        yield {"progress": 100, "message": complete_message}

    monkeypatch.setattr(nkit2iso_service._runner, "run", fake_run)

    updates = [
        u async for u in nkit2iso_service.convert(str(source), out_iso, "nkit_restore")
    ]

    assert captured["cmd"][-1] in {"none", "download"}
    assert updates[0] == {"progress": 1, "message": "Starting NKit restore..."}
    assert [u["progress"] for u in updates] == [1, 1, 10, 100, 100, 100]
    assert "CRC32 verified" in updates[-1]["message"]
    assert Path(out_iso).exists()


@pytest.mark.asyncio
async def test_convert_reports_not_bit_exact_restore(tmp_path, monkeypatch):
    # A Wii image whose update partition was removed at shrink time restores to
    # a playable but NOT redump-verifiable ISO, and nkit2iso exits 0. The job's
    # final message has to say so instead of a bare "complete".
    source = tmp_path / "Wii.nkit.iso"
    source.write_bytes(b"nkit")
    out_iso = str(tmp_path / "Wii.iso")

    async def fake_run(cmd, *, output_path, complete_message, **_kwargs):
        Path(output_path).write_bytes(b"restored")
        yield {
            "progress": 100,
            "message": (
                "CRC32 check skipped — output is playable but NOT bit-exact "
                "(update partition zero-filled)"
            ),
        }
        yield {"progress": 100, "message": complete_message}

    monkeypatch.setattr(nkit2iso_service._runner, "run", fake_run)

    updates = [
        u async for u in nkit2iso_service.convert(str(source), out_iso, "nkit_restore")
    ]

    final = updates[-1]
    assert final["progress"] == 100
    assert "NOT bit-exact" in final["message"]
    assert "NKIT2ISO_RECOVERY=download" in final["message"]
    # The restored image is kept: it's playable, just not redump-verified.
    assert Path(out_iso).exists()


@pytest.mark.asyncio
async def test_convert_rejects_a_non_nkit_source(tmp_path):
    source = tmp_path / "Game.iso"
    source.write_bytes(b"plain iso")
    with pytest.raises(ValueError, match="Not an NKit image"):
        async for _ in nkit2iso_service.convert(
            str(source), str(tmp_path / "out.iso"), "nkit_restore",
        ):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boom", [ConversionCancelled("cancelled"), asyncio.CancelledError(), RuntimeError("x")],
)
async def test_convert_cleans_partial_output(tmp_path, monkeypatch, boom):
    source = tmp_path / "Game.nkit.iso"
    source.write_bytes(b"nkit")
    out_iso = tmp_path / "Game.iso"

    async def fake_run(cmd, *, output_path, **_kwargs):
        Path(output_path).write_bytes(b"partial")
        yield {"progress": 10, "message": "10%"}
        raise boom

    monkeypatch.setattr(nkit2iso_service._runner, "run", fake_run)

    with pytest.raises(type(boom)):
        async for _ in nkit2iso_service.convert(
            str(source), str(out_iso), "nkit_restore",
        ):
            pass

    # A truncated ISO must not block the retry.
    assert not out_iso.exists()


# --------------------------------------------------------------------------- #
# Listing wiring
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_files_annotates_nkit_source_and_output_badge(
    tmp_path, monkeypatch,
):
    (tmp_path / "Game.nkit.iso").write_bytes(b"nkit")
    (tmp_path / "Restored.nkit.iso").write_bytes(b"nkit")
    (tmp_path / "Restored.iso").write_bytes(b"already restored")
    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(tmp_path))

    listing = await files_routes.list_files(path=str(tmp_path))
    by_name = {e.name: e for e in listing.entries}

    assert "nkit" in by_name["Game.nkit.iso"].convertible_by
    assert [o for o in by_name["Game.nkit.iso"].outputs if o.tool_id == "nkit"] == []

    badge = [o for o in by_name["Restored.nkit.iso"].outputs if o.tool_id == "nkit"]
    assert len(badge) == 1
    assert badge[0].exists is True
    assert badge[0].path == str(tmp_path / "Restored.iso")

    # No verify/info affordance: nkit2iso has no verify subcommand.
    assert "nkit" not in by_name["Restored.iso"].verifiable_by


# --------------------------------------------------------------------------- #
# NKit header parsing / info
# --------------------------------------------------------------------------- #


def _disc_header(*, wii: bool, game_id=b"GALE01", title=b"Melee",
                 crc=0x099E2C6D, size_field=0x1D26_0000, marker=b"NKIT v01",
                 disc_no=0, version=2) -> bytes:
    """A synthetic 0x440 GC/Wii disc header carrying NKit's metadata window."""
    head = bytearray(b"\x00" * 0x440)
    head[0x00:0x06] = game_id
    head[0x06] = disc_no
    head[0x07] = version
    struct.pack_into(">I", head, 0x18, 0x5D1C9EA3 if wii else 0)
    struct.pack_into(">I", head, 0x1C, 0 if wii else 0xC2339F3D)
    head[0x20:0x20 + len(title)] = title
    head[0x200:0x200 + len(marker)] = marker
    struct.pack_into(">I", head, 0x208, crc)
    struct.pack_into(">I", head, 0x210, size_field)
    return bytes(head)


def _gcz_wrap(payload: bytes, *, block_size=0x8000) -> bytes:
    """Wrap ``payload`` in a single-block Dolphin GCZ container."""
    body = zlib.compress(payload.ljust(block_size, b"\x00"))
    header = struct.pack(
        "<IIQQII", 0xB10BC001, 0, len(body), block_size, block_size, 1,
    )
    return header + struct.pack("<Q", 0) + struct.pack("<I", 0) + body


def test_read_header_gamecube(tmp_path):
    src = tmp_path / "Melee.nkit.iso"
    src.write_bytes(_disc_header(wii=False) + b"\x00" * 64)

    header = read_nkit_header(str(src))
    assert header["platform"] == "GameCube"
    assert header["game_id"] == "GALE01"
    assert header["title"] == "Melee"
    assert header["crc32"] == "099E2C6D"
    assert header["disc_version"] == 2
    # GameCube stores the image size in plain bytes.
    assert header["restored_size"] == 0x1D26_0000
    assert header["container"] == "NKit stream"


def test_read_header_wii_scales_size_by_four(tmp_path):
    src = tmp_path / "Galaxy.nkit.iso"
    src.write_bytes(_disc_header(wii=True, size_field=0x1000) + b"\x00" * 64)

    header = read_nkit_header(str(src))
    assert header["platform"] == "Wii"
    # Wii stores the size in 4-byte units — the classic way to get this wrong.
    assert header["restored_size"] == 0x1000 * 4


def test_read_header_through_a_gcz_container(tmp_path):
    src = tmp_path / "Melee.nkit.gcz"
    src.write_bytes(_gcz_wrap(_disc_header(wii=False)))

    header = read_nkit_header(str(src))
    assert header["platform"] == "GameCube"
    assert header["game_id"] == "GALE01"
    assert header["container"] == "GCZ (zlib block container)"


def test_read_header_rejects_a_plain_iso(tmp_path):
    # A GC disc header with no NKit marker: a plain ISO someone renamed.
    src = tmp_path / "Melee.nkit.iso"
    src.write_bytes(_disc_header(wii=False, marker=b"\x00" * 8))
    with pytest.raises(NkitHeaderError, match="already a plain ISO"):
        read_nkit_header(str(src))


def test_read_header_rejects_a_non_disc_file(tmp_path):
    src = tmp_path / "junk.nkit.iso"
    src.write_bytes(b"\x00" * 0x100)
    with pytest.raises(NkitHeaderError, match="too small"):
        read_nkit_header(str(src))


def test_info_reports_the_restore_target(tmp_path):
    src = tmp_path / "Melee.nkit.iso"
    body = _disc_header(wii=False, size_field=1_000_000) + b"\x00" * 1000
    src.write_bytes(body)

    info = nkit2iso_service.info(str(src))
    assert info["platform"] == "GameCube"
    assert info["restored_size"] == 1_000_000
    assert info["restored_size_display"] == "0.95 MB"
    assert info["crc32"] == "099E2C6D"
    # The compound extension, not the generic `.iso` tail.
    assert info["extension"] == ".nkit.iso"
    assert info["compressed"] is True
    assert info["ratio"] == f"{len(body) / 1_000_000 * 100:.1f}%"

    model = registry.get("nkit").info_model(info, str(src))
    assert model.platform == "GameCube"
    assert model.game_id == "GALE01"
    assert model.crc32 == "099E2C6D"
    assert model.restored_size == 1_000_000


def test_restored_size_is_none_for_an_unreadable_source(tmp_path):
    # Feeds the chain preflight, which must degrade to a ratio, never raise.
    assert nkit2iso_service.restored_size(str(tmp_path / "absent.nkit.iso")) is None
    plain = tmp_path / "plain.nkit.iso"
    plain.write_bytes(b"\x00" * 0x500)
    assert nkit2iso_service.restored_size(str(plain)) is None


def test_expected_output_size_feeds_the_chain_preflight(tmp_path):
    src = tmp_path / "Melee.nkit.iso"
    src.write_bytes(_disc_header(wii=False, size_field=4_000_000))
    assert registry.get("nkit").expected_output_size(
        str(src), "nkit_restore",
    ) == 4_000_000


# --------------------------------------------------------------------------- #
# Malformed-GCZ hardening (Codex review, PR #257)
# --------------------------------------------------------------------------- #


def _gcz_header(*, comp_size, data_size, block_size, num_blocks) -> bytes:
    return struct.pack(
        "<IIQQII", 0xB10BC001, 0, comp_size, data_size, block_size, num_blocks,
    )


@pytest.mark.parametrize(
    ("name", "blob"),
    [
        # num_blocks claims ~4 billion entries: 48 GB of block table that isn't
        # there. Must be rejected from the header alone, never read.
        ("huge_block_table",
         _gcz_header(comp_size=64, data_size=1 << 20, block_size=1 << 15,
                     num_blocks=0xFFFFFFFF) + b"\x00" * 64),
        # block_size beyond anything real.
        ("huge_block_size",
         _gcz_header(comp_size=64, data_size=1 << 20, block_size=1 << 31,
                     num_blocks=1) + b"\x00" * 64),
        # comp_size lies about how many stored bytes follow block 0.
        ("lying_comp_size",
         _gcz_header(comp_size=1 << 40, data_size=1 << 20, block_size=1 << 15,
                     num_blocks=1) + struct.pack("<Q", 0) + struct.pack("<I", 0)
         + b"\x00" * 32),
        # A block pointer past the end of the file.
        ("pointer_past_eof",
         _gcz_header(comp_size=1 << 20, data_size=1 << 20, block_size=1 << 15,
                     num_blocks=1) + struct.pack("<Q", 1 << 30)
         + struct.pack("<I", 0) + b"\x00" * 32),
        ("zero_blocks",
         _gcz_header(comp_size=64, data_size=1 << 20, block_size=1 << 15,
                     num_blocks=0) + b"\x00" * 64),
        ("truncated_header", b"\x01\xc0\x0b\xb1" + b"\x00" * 8),
    ],
)
def test_malformed_gcz_is_rejected_not_allocated(tmp_path, name, blob):
    """A crafted container must surface NkitHeaderError (-> 422), not blow up.

    Every length in a GCZ header comes from the file; without bounds checks a
    ~40-byte file can drive a multi-gigabyte read and MemoryError the worker.
    """
    src = tmp_path / f"{name}.nkit.gcz"
    src.write_bytes(blob)
    with pytest.raises(NkitHeaderError):
        read_nkit_header(str(src))
    # The chain preflight must degrade quietly on the same input.
    assert nkit2iso_service.restored_size(str(src)) is None


def test_valid_gcz_still_reads_after_hardening(tmp_path):
    # The bounds must not reject a legitimate single-block container.
    src = tmp_path / "Melee.nkit.gcz"
    src.write_bytes(_gcz_wrap(_disc_header(wii=False)))
    assert read_nkit_header(str(src))["platform"] == "GameCube"


@pytest.mark.asyncio
async def test_inexact_restore_update_is_marked_as_a_warning(tmp_path, monkeypatch):
    """The caveat carries a ``warning`` flag so a chain can preserve it.

    A later chain step's messages replace earlier ones, so without the marker
    the "not bit-exact" caveat is silently lost in a nkit_to_rvz job.
    """
    source = tmp_path / "Wii.nkit.iso"
    source.write_bytes(b"nkit")
    out_iso = str(tmp_path / "Wii.iso")

    async def fake_run(cmd, *, output_path, complete_message, **_kwargs):
        Path(output_path).write_bytes(b"restored")
        yield {"progress": 100, "message": "CRC32 check skipped — not bit-exact"}
        yield {"progress": 100, "message": complete_message}

    monkeypatch.setattr(nkit2iso_service._runner, "run", fake_run)

    updates = [
        u async for u in nkit2iso_service.convert(str(source), out_iso, "nkit_restore")
    ]
    assert updates[-1]["warning"] is True
    # A clean restore carries no warning marker.
    assert not any(u.get("warning") for u in updates[:-1])
