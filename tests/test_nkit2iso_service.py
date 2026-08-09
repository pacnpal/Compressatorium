"""Tests for the nkit2iso tool (NKit-shrunk GC/Wii image -> full .iso).

Covers the pieces a mocked-subprocess test can actually prove: the compound
extension seam (``.nkit.iso`` / ``.nkit.gcz`` are distinct from the generic
``.iso`` / ``.gcz`` CHDMAN and Dolphin own), output-path naming, argv +
recovery-mode selection, progress parsing, the not-bit-exact message, cancel
cleanup, and the registry/listing wiring.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.routes import convert as convert_routes
from app.routes import files as files_routes
from app.services.nkit2iso import (
    NKIT2ISO_CONVERTIBLE_EXTENSIONS,
    ConversionCancelled,
    nkit2iso_service,
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
