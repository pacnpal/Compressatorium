"""Characterization tests for the tool plugin registry (design Phase 0).

These lock the *current* behavior (mode -> tool, capability flags, output
paths) into tests before later phases move dispatch logic onto the registry.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.models import ConversionMode
from app.routes import convert as convert_routes
from app.services.chdman import (
    CHDMAN_CONVERTIBLE_EXTENSIONS,
    chdman_service,
)
from app.services.dolphin_tool import (
    DOLPHIN_CONVERTIBLE_EXTENSIONS,
    dolphin_tool_service,
)
from app.services.jwudtool import (
    JWUD_COMPRESS_EXTENSIONS,
    JWUD_DECOMPRESS_EXTENSIONS,
    jwudtool_service,
)
from app.services.tools import registry
from app.services.tools.registry import ToolRegistry
from app.services.tools.spec import ModeKind, ModeSpec
from app.services.maxcso import (
    MAXCSO_COMPRESS_EXTENSIONS,
    MAXCSO_DECOMPRESS_EXTENSIONS,
    maxcso_service,
)
from app.services.nsz import (
    NSZ_COMPRESS_EXTENSIONS,
    NSZ_DECOMPRESS_EXTENSIONS,
    nsz_service,
)
from app.services.romz import (
    ROMZ_ARCHIVE_EXTENSIONS,
    ROMZ_COMPRESS_EXTENSIONS,
    romz_service,
)
from app.services.z3ds_compress import (
    Z3DS_CONVERTIBLE_EXTENSIONS,
    Z3DS_DECOMPRESS_EXTENSIONS,
    z3ds_compress_service,
)

# ConversionMode values that are NOT conversion modes, handled by the
# external job API, not by any of the three conversion services.
EXTERNAL_MODES = {ConversionMode.METADATA_SCAN, ConversionMode.DAT_MATCH}
CONVERSION_MODES = [m for m in ConversionMode if m not in EXTERNAL_MODES]


# Composite/pipeline modes (tool_id="chain") are a newer construct that
# orchestrates several single-tool modes; they predate neither the legacy ladder
# below nor its prefix rules, so they're enumerated explicitly.
COMPOSITE_MODES = {"cso_to_chd"}


def _legacy_tool_for_mode(mode: str) -> str:
    """Replicates the dispatch ladder at job_manager.py:1444."""
    if mode in COMPOSITE_MODES:
        return "chain"
    if mode == "folder_to_iso":
        return "makeps3iso"
    if mode.startswith("dolphin_"):
        return "dolphin"
    if mode.startswith("z3ds_"):
        return "z3ds"
    if mode.startswith("nsz_"):
        return "nsz"
    if mode.startswith(("cso_", "cso2_", "zso_", "dax_")):
        return "cso"
    if mode.startswith("romz_"):
        return "romz"
    if mode.startswith("jwud_"):
        return "jwud"
    return "chdman"


def test_every_conversion_mode_resolves_to_exactly_one_tool():
    resolved = {m.value: registry.for_mode(m.value).id for m in CONVERSION_MODES}
    assert len(resolved) == 31
    # Each registered mode is owned by exactly one tool (no duplicates).
    assert sorted(s.mode for s in registry.mode_specs()) == sorted(resolved)


def test_external_modes_are_not_registered():
    for mode in EXTERNAL_MODES:
        with pytest.raises(KeyError):
            registry.for_mode(mode.value)


@pytest.mark.parametrize("mode", [m.value for m in CONVERSION_MODES])
def test_mode_to_tool_matches_legacy_ladder(mode):
    assert registry.for_mode(mode).id == _legacy_tool_for_mode(mode)


@pytest.mark.parametrize("mode", [m.value for m in CONVERSION_MODES])
def test_supports_delete_on_verify_parity(mode):
    assert (
        registry.spec(mode).supports_delete_on_verify
        == convert_routes.supports_delete_on_verify(mode)
    )


def test_kind_classification():
    spec = registry.spec
    assert spec("createcd").kind is ModeKind.CREATE
    assert spec("extractcd").kind is ModeKind.EXTRACT
    assert spec("copy").kind is ModeKind.COPY
    assert spec("z3ds_compress").kind is ModeKind.COMPRESS
    assert spec("z3ds_decompress").kind is ModeKind.EXTRACT
    # dolphin compress vs extract
    assert spec("dolphin_rvz").kind is ModeKind.COMPRESS
    assert spec("dolphin_iso").kind is ModeKind.EXTRACT
    # nsz compress vs decompress
    assert spec("nsz_compress").kind is ModeKind.COMPRESS
    assert spec("nsz_decompress").kind is ModeKind.EXTRACT
    # cso/cso2/zso/dax compress vs decompress
    assert spec("cso_compress").kind is ModeKind.COMPRESS
    assert spec("cso2_compress").kind is ModeKind.COMPRESS
    assert spec("zso_compress").kind is ModeKind.COMPRESS
    assert spec("dax_compress").kind is ModeKind.COMPRESS
    assert spec("cso_decompress").kind is ModeKind.EXTRACT
    # romz 7z/zip compress vs extract
    assert spec("romz_7z").kind is ModeKind.COMPRESS
    assert spec("romz_zip").kind is ModeKind.COMPRESS
    assert spec("romz_extract").kind is ModeKind.EXTRACT
    # jwud compress vs decompress
    assert spec("jwud_compress").kind is ModeKind.COMPRESS
    assert spec("jwud_decompress").kind is ModeKind.EXTRACT


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        # chdman create/copy accept -c codec selection; extract does not.
        ("createcd", True),
        ("createdvd", True),
        ("copy", True),
        ("extractcd", False),
        # dolphin rvz/wia take a compression codec (+ level); gcz/iso don't.
        ("dolphin_rvz", True),
        ("dolphin_wia", True),
        ("dolphin_gcz", False),
        ("dolphin_iso", False),
        ("z3ds_compress", False),
        # cso/cso2/zso/dax compress expose an effort preset; decompress doesn't.
        ("cso_compress", True),
        ("cso2_compress", True),
        ("zso_compress", True),
        ("dax_compress", True),
        ("cso_decompress", False),
        # romz 7z/zip compress expose an effort preset; extract doesn't.
        ("romz_7z", True),
        ("romz_zip", True),
        ("romz_extract", False),
        # WUX is a fixed sector-dedup format: neither direction takes a codec.
        ("jwud_compress", False),
        ("jwud_decompress", False),
    ],
)
def test_supports_compression_matches_current_behavior(mode, expected):
    assert registry.spec(mode).supports_compression is expected


def test_compression_level_only_for_dolphin_rvz_wia():
    # Dolphin RVZ/WIA and Switch compress expose a numeric level.
    for mode in ("dolphin_rvz", "dolphin_wia", "nsz_compress"):
        assert registry.spec(mode).supports_compression_level is True
    for mode in ("createcd", "copy", "dolphin_gcz", "dolphin_iso", "z3ds_compress",
                 "nsz_decompress", "cso_compress", "cso2_compress", "zso_compress",
                 "dax_compress", "cso_decompress", "romz_7z", "romz_zip",
                 "romz_extract", "jwud_compress", "jwud_decompress"):
        assert registry.spec(mode).supports_compression_level is False


def test_convertible_extensions_match_service_constants():
    expected = (
        set(CHDMAN_CONVERTIBLE_EXTENSIONS)
        | set(DOLPHIN_CONVERTIBLE_EXTENSIONS)
        | set(Z3DS_CONVERTIBLE_EXTENSIONS)
        | set(Z3DS_DECOMPRESS_EXTENSIONS)
        | set(NSZ_COMPRESS_EXTENSIONS)
        | set(NSZ_DECOMPRESS_EXTENSIONS)
        | set(MAXCSO_COMPRESS_EXTENSIONS)
        | set(MAXCSO_DECOMPRESS_EXTENSIONS)
        | set(ROMZ_COMPRESS_EXTENSIONS)
        | set(ROMZ_ARCHIVE_EXTENSIONS)
        | set(JWUD_COMPRESS_EXTENSIONS)
        | set(JWUD_DECOMPRESS_EXTENSIONS)
    )
    assert set(registry.convertible_extensions()) == expected


def test_tools_for_input_representative():
    assert sorted(t.id for t in registry.tools_for_input("game.iso")) == [
        "chdman",
        "cso",
        "dolphin",
    ]
    assert [t.id for t in registry.tools_for_input("rom.3ds")] == ["z3ds"]
    assert [t.id for t in registry.tools_for_input("game.nsp")] == ["nsz"]
    assert [t.id for t in registry.tools_for_input("game.nsz")] == ["nsz"]
    assert [t.id for t in registry.tools_for_input("disc.gdi")] == ["chdman"]
    # .cso/.zso/.dax are convertible-from sources for the maxcso decompress mode
    # and for the cso_to_chd chain (which packages them straight to .chd).
    assert sorted(t.id for t in registry.tools_for_input("game.cso")) == ["chain", "cso"]
    assert sorted(t.id for t in registry.tools_for_input("game.zso")) == ["chain", "cso"]
    assert sorted(t.id for t in registry.tools_for_input("game.dax")) == ["chain", "cso"]
    # Handheld ROM sources + the archives romz can extract from.
    assert [t.id for t in registry.tools_for_input("Game.gba")] == ["romz"]
    assert [t.id for t in registry.tools_for_input("Game.gb")] == ["romz"]
    assert [t.id for t in registry.tools_for_input("Game.nds")] == ["romz"]
    assert [t.id for t in registry.tools_for_input("Game.7z")] == ["romz"]
    assert [t.id for t in registry.tools_for_input("Game.zip")] == ["romz"]
    # Wii U disc images, both directions.
    assert [t.id for t in registry.tools_for_input("Game.wud")] == ["jwud"]
    assert [t.id for t in registry.tools_for_input("Game.wux")] == ["jwud"]
    # A finished .chd is not a "convertible-from" source in the listing.
    assert registry.tools_for_input("out.chd") == []


def test_tool_for_verify_representative():
    assert registry.tool_for_verify("out.chd").id == "chdman"
    assert registry.tool_for_verify("rom.z3ds").id == "z3ds"
    assert registry.tool_for_verify("disc.rvz").id == "dolphin"
    assert registry.tool_for_verify("game.nsz").id == "nsz"
    assert registry.tool_for_verify("game.xcz").id == "nsz"
    assert registry.tool_for_verify("game.cso").id == "cso"
    assert registry.tool_for_verify("game.zso").id == "cso"
    assert registry.tool_for_verify("game.dax").id == "cso"
    assert registry.tool_for_verify("Game.7z").id == "romz"
    assert registry.tool_for_verify("Game.zip").id == "romz"
    assert registry.tool_for_verify("Game.wux").id == "jwud"
    # A raw .wud carries nothing to check, so it is not a verify target.
    assert registry.tool_for_verify("Game.wud") is None
    assert registry.tool_for_verify("nope.txt") is None


def test_tools_verifying_path_refines_extension_match(tmp_path):
    # Non-archive verify targets fall back to the plain extension match, so
    # tools_verifying_path agrees with tool_for_verify there.
    assert [t.id for t in registry.tools_verifying_path("out.chd")] == ["chdman"]
    assert [t.id for t in registry.tools_verifying_path("disc.rvz")] == ["dolphin"]
    assert registry.tools_verifying_path("nope.txt") == []

    # romz refines the .7z/.zip claim: only single-ROM archives surface it.
    single = tmp_path / "Game.gba.zip"
    with zipfile.ZipFile(single, "w") as zf:
        zf.writestr("Game.gba", b"ROMDATA")
    assert [t.id for t in registry.tools_verifying_path(str(single))] == ["romz"]

    multi = tmp_path / "Bundle.zip"
    with zipfile.ZipFile(multi, "w") as zf:
        zf.writestr("a.gba", b"a")
        zf.writestr("readme.txt", b"b")
    assert registry.tools_verifying_path(str(multi)) == []


@pytest.mark.parametrize("output_dir", [None, "/data/out"])
@pytest.mark.parametrize("treat_as_stem", [False, True])
@pytest.mark.parametrize(
    ("mode", "service", "inp"),
    [
        ("createcd", chdman_service, "/data/game.cue"),
        ("createdvd", chdman_service, "/data/game.iso"),
        ("extractcd", chdman_service, "/data/game.chd"),
        ("extractdvd", chdman_service, "/data/game.chd"),
        ("copy", chdman_service, "/data/game.chd"),
        ("dolphin_rvz", dolphin_tool_service, "/data/game.iso"),
        ("dolphin_iso", dolphin_tool_service, "/data/game.rvz"),
        ("z3ds_compress", z3ds_compress_service, "/data/rom.3ds"),
        ("nsz_compress", nsz_service, "/data/game.nsp"),
        ("nsz_compress", nsz_service, "/data/game.xci"),
        ("nsz_decompress", nsz_service, "/data/game.nsz"),
        ("nsz_decompress", nsz_service, "/data/game.xcz"),
        ("cso_compress", maxcso_service, "/data/game.iso"),
        ("cso2_compress", maxcso_service, "/data/game.iso"),
        ("zso_compress", maxcso_service, "/data/game.iso"),
        ("dax_compress", maxcso_service, "/data/game.iso"),
        ("cso_decompress", maxcso_service, "/data/game.cso"),
        ("cso_decompress", maxcso_service, "/data/game.zso"),
        ("cso_decompress", maxcso_service, "/data/game.dax"),
        ("romz_7z", romz_service, "/data/Game.gba"),
        ("romz_zip", romz_service, "/data/Game.nds"),
        ("jwud_compress", jwudtool_service, "/data/Game.wud"),
        ("jwud_decompress", jwudtool_service, "/data/Game.wux"),
    ],
)
def test_output_path_delegation_matches_service(
    mode, service, inp, output_dir, treat_as_stem,
):
    expected = service.get_output_path_for_mode(
        mode, inp, output_dir, treat_as_stem=treat_as_stem,
    )
    actual = registry.for_mode(mode).output_path(
        mode, inp, output_dir, treat_as_stem=treat_as_stem,
    )
    assert actual == expected


def test_duplicate_mode_registration_raises():
    class _StubTool:
        id = "stub"
        modes = (
            ModeSpec(
                mode="createcd",  # collides with chdman
                tool_id="stub",
                kind=ModeKind.CREATE,
                label="Stub",
                group="create",
                output_ext=".chd",
                input_extensions=frozenset({".iso"}),
            ),
        )

    fresh = ToolRegistry()
    fresh.register(registry.get("chdman"))
    with pytest.raises(ValueError, match="duplicate mode createcd"):
        fresh.register(_StubTool())


def test_duplicate_tool_id_registration_raises():
    fresh = ToolRegistry()
    fresh.register(registry.get("chdman"))
    with pytest.raises(ValueError, match="duplicate tool id chdman"):
        fresh.register(registry.get("chdman"))


def _spec(mode: str, tool_id: str = "stub") -> ModeSpec:
    return ModeSpec(
        mode=mode,
        tool_id=tool_id,
        kind=ModeKind.CREATE,
        label="Stub",
        group="create",
        output_ext=".x",
        input_extensions=frozenset({".y"}),
    )


def test_duplicate_mode_within_single_tool_raises():
    class _StubTool:
        id = "stub"
        modes = (_spec("foo"), _spec("foo"))

    with pytest.raises(ValueError, match="duplicate mode foo"):
        ToolRegistry().register(_StubTool())


def test_mode_spec_tool_id_must_match_owner():
    class _StubTool:
        id = "stub"
        modes = (_spec("foo", tool_id="other"),)

    with pytest.raises(ValueError, match="tool_id 'other'"):
        ToolRegistry().register(_StubTool())


@pytest.mark.parametrize(
    "tool_id", ["chdman", "dolphin", "z3ds", "nsz", "cso", "romz", "jwud"],
)
def test_output_extensions_cover_mode_output_exts(tool_id):
    tool = registry.get(tool_id)
    declared = {m.output_ext for m in tool.modes if m.output_ext is not None}
    assert declared <= tool.output_extensions


def test_output_extensions_union_covers_every_tool():
    """registry.output_extensions() is the union of all tools' outputs."""
    union = set(registry.output_extensions())
    for tool in registry.all():
        assert tool.output_extensions <= union
    # Representative outputs from each tool.
    assert {".chd", ".rvz", ".nsz"} <= union


def test_scannable_extensions_are_output_plus_verify():
    """Discovery walks produced outputs *and* verifiable inputs (issue #131)."""
    scannable = set(registry.scannable_extensions())
    assert scannable == set(registry.output_extensions()) | set(
        registry.verify_extensions()
    )
    # Non-CHD outputs that historically were never scanned are now eligible.
    assert {".chd", ".rvz", ".wia", ".gcz", ".nsz", ".xcz"} <= scannable
    # The extractcd .bin data-track sidecar (what Redump DATs index) is in too.
    assert ".bin" in scannable


# z3ds/nsz/jwud have no embedded-hash source, so they use the BaseTool default.
# (chdman's hook is exercised in tests/test_dat_routes.py with a mocked
# metadata store; dolphin's would otherwise spawn dolphin-tool.)
@pytest.mark.parametrize("tool_id", ["z3ds", "nsz", "jwud"])
@pytest.mark.asyncio
async def test_embedded_hashes_default_empty_for_no_source_tools(tool_id, tmp_path):
    tool = registry.get(tool_id)
    ext = next(iter(tool.output_extensions))
    result = await tool.embedded_hashes(str(tmp_path / f"sample{ext}"))
    assert result == []


def _mode_output_exts(tool, mode) -> set[str]:
    """Every output extension a mode can produce, derived from its declared
    inputs (covers tools like nsz/z3ds whose output ext is input-dependent)."""
    return {
        Path(
            tool.output_path(mode.mode, f"/data/sample{in_ext}", None, treat_as_stem=False),
        ).suffix.lower()
        for in_ext in mode.input_extensions
    }


def test_delete_on_verify_iff_output_is_verifiable():
    """Any platform/file that supports verify should support verify-and-delete.

    Delete-on-verify removes the source only after the produced output passes
    verification, so it is safe exactly when *every* output a mode can produce
    is itself verifiable (its extension is in the tool's verify set). This locks
    that invariant in registry-wide, so a future tool can't add a verifiable
    output without also enabling delete-on-verify (or vice versa).
    """
    for tool in registry.all():
        for mode in tool.modes:
            outs = _mode_output_exts(tool, mode)
            # A composite/chain mode deliberately claims no verify_extensions of
            # its own (chdman already owns .chd verify); its output is verified
            # by the final step's tool, which ChainTool.verify delegates to.
            if getattr(mode, "steps", None):
                vtool = registry.get(mode.steps[mode.verify_step].tool_id)
                vexts = vtool.verify_extensions
            else:
                vexts = tool.verify_extensions
            verifiable = bool(outs) and outs <= vexts
            assert mode.supports_delete_on_verify == verifiable, (
                f"{mode.mode}: supports_delete_on_verify={mode.supports_delete_on_verify} "
                f"but outputs {sorted(outs)} verifiable against {sorted(vexts)} = {verifiable}"
            )
