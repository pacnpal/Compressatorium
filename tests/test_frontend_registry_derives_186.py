"""Guard the registry-derived frontend fact lists (issue #186, site 3).

Two frontend surfaces used to hand-maintain copies of registry facts:

* ``src/lib/util/fileIcon.js`` re-typed every game/disc extension, so a new
  tool's files silently fell through to the generic File glyph.
* ``src/lib/components/views/HelpView.svelte`` re-typed every mode with its
  label and output extension, so a new or removed mode silently drifted out of
  the Help table.

Both now derive from ``src/lib/tools/registry.js``: fileIcon builds its
disc/game buckets from a small ``TOOL_MEDIA`` map over ``registry.all()``, and
HelpView generates its table via ``helpModeSections(registry)`` keyed by the
curated blurbs in ``src/lib/tools/helpModes.js``. This test evaluates those JS
modules with Node — the same engine the app uses, so the ext constants and
spreads resolve exactly — and fails if the derived coverage drifts from the
registry: a filterable extension with no icon bucket, an unclassified tool, a
mode with no blurb, a stale blurb/override key, or a mode with no output.
Skipped when Node is unavailable.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
_REGISTRY_JS = _SRC / "lib" / "tools" / "registry.js"
_HELP_MODES_JS = _SRC / "lib" / "tools" / "helpModes.js"
_FILE_ICON_JS = _SRC / "lib" / "util" / "fileIcon.js"


def _find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for cand in (os.environ.get("NODE"), "/opt/node22/bin/node", "/usr/bin/node"):
        if cand and os.path.exists(cand):
            return cand
    return None


def _strip_imports(src: str) -> str:
    """Drop every top-level ``import ...;`` line so the modules can be
    concatenated and evaluated as one plain-Node ESM. The only import with a
    runtime value (registry's ``api``) is stubbed by the caller; every other
    import (Lucide icon components, the intra-module ``registry`` reference) is
    unused by the pure data this dump reads."""
    kept = []
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") and stripped.endswith(";"):
            continue
        kept.append(line)
    return "\n".join(kept)


def _dump(tmp_path: Path) -> dict:
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate the frontend registry modules")

    # registry.js is the source of truth; its only import (`api`) is stubbed.
    registry_src = _REGISTRY_JS.read_text(encoding="utf-8")
    needle = "import { api } from '$lib/api/endpoints.js';"
    assert needle in registry_src, "registry.js import shape changed; update the stub"
    registry_src = "const api = {};\n" + _strip_imports(registry_src)

    # fileIcon.js imports registry (now in scope) + Lucide glyphs (unused here).
    file_icon_src = _strip_imports(_FILE_ICON_JS.read_text(encoding="utf-8"))
    # helpModes.js is pure (no imports).
    help_modes_src = _strip_imports(_HELP_MODES_JS.read_text(encoding="utf-8"))

    dump = (
        "\nconst __out = {"
        " filterable: registry.allFilterableExts(),"
        " icon_categories: ICON_EXT_CATEGORIES,"
        " tool_ids: registry.all().map((t) => t.id),"
        " tool_media: Object.keys(TOOL_MEDIA),"
        " modes: registry.all().flatMap((t) => t.modes.map((m) => ({"
        "   mode: m.mode, output_ext: m.outputExt ?? null }))),"
        " blurb_keys: Object.keys(MODE_BLURBS),"
        " output_keys: Object.keys(MODE_OUTPUT),"
        " sections: helpModeSections(registry).map((s) => ({"
        "   title: s.title, rows: s.rows })),"
        "};\n"
        "process.stdout.write(JSON.stringify(__out));\n"
    )

    script = tmp_path / "derives_eval.mjs"
    script.write_text(registry_src + "\n" + file_icon_src + "\n" + help_modes_src + dump)
    proc = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        pytest.fail(f"Could not evaluate the frontend registry modules via node:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_file_icon_covers_every_registry_extension(tmp_path):
    data = _dump(tmp_path)

    # Every registered tool must be classified, so a new tool forces an icon
    # decision instead of falling through to the generic File glyph.
    assert set(data["tool_ids"]) == set(data["tool_media"]), (
        "fileIcon.js TOOL_MEDIA is out of sync with the registry tools:\n"
        f"  registry-only (unclassified): {sorted(set(data['tool_ids']) - set(data['tool_media']))}\n"
        f"  TOOL_MEDIA-only (stale):      {sorted(set(data['tool_media']) - set(data['tool_ids']))}"
    )

    cats = data["icon_categories"]
    covered = set(cats["disc"]) | set(cats["game"]) | set(cats["archive"]) | {".chd"}
    uncovered = [ext for ext in data["filterable"] if ext not in covered]
    assert not uncovered, (
        "fileIcon.js renders these registry extensions with the generic File "
        f"glyph (add the owning tool to TOOL_MEDIA): {sorted(uncovered)}"
    )


def test_help_table_covers_exactly_the_registry_modes(tmp_path):
    data = _dump(tmp_path)

    registry_modes = {m["mode"] for m in data["modes"]}
    output_ext = {m["mode"]: m["output_ext"] for m in data["modes"]}

    # The curated blurb map must cover exactly the registry's mode set.
    assert set(data["blurb_keys"]) == registry_modes, (
        "helpModes.js MODE_BLURBS drift from registry modes:\n"
        f"  missing a blurb: {sorted(registry_modes - set(data['blurb_keys']))}\n"
        f"  stale blurb key: {sorted(set(data['blurb_keys']) - registry_modes)}"
    )

    # Output overrides may only name real modes.
    stale_overrides = set(data["output_keys"]) - registry_modes
    assert not stale_overrides, f"helpModes.js MODE_OUTPUT names unknown modes: {sorted(stale_overrides)}"

    # Every mode must resolve to a non-empty Output cell (override or registry
    # outputExt), so a future null-output mode can't render a blank column.
    overrides = set(data["output_keys"])
    missing_output = [
        m for m in registry_modes if m not in overrides and not output_ext.get(m)
    ]
    assert not missing_output, (
        "these modes have a null registry outputExt and no MODE_OUTPUT override, "
        f"so their Help Output column would be blank: {sorted(missing_output)}"
    )

    # The generated sections must reproduce the full mode set exactly once each,
    # with the blurb and output wired through.
    section_modes = [r["mode"] for s in data["sections"] for r in s["rows"]]
    assert sorted(section_modes) == sorted(registry_modes), (
        "helpModeSections() does not reproduce the registry mode set exactly once each:\n"
        f"  rendered: {sorted(section_modes)}"
    )
    for section in data["sections"]:
        for row in section["rows"]:
            assert row["note"], f"mode {row['mode']} rendered an empty blurb"
            assert row["out"], f"mode {row['mode']} rendered an empty output"
