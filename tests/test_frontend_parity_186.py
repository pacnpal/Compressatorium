"""Guard the backend↔frontend tool-registry mirror (issue #186, site 1).

``src/lib/tools/registry.js`` (frontend) and the backend ``ModeSpec`` rows
(``registry.mode_specs()``) are a hand-maintained mirror: the UI offers exactly
the modes / extensions the backend will accept. There is no shared build step,
so a drift in ``allows_archive_input`` / ``supports_delete_on_verify`` /
``input_extensions`` / ``output_ext`` / ``kind`` would silently make the UI
offer a job ``plan_job`` rejects (→ "0 queued") or fail on submit. This test
fails loudly on any such drift.

It evaluates ``registry.js`` with Node — the same engine the app uses, so the JS
ext constants and spreads resolve exactly — and compares each mode row
field-by-field to the backend spec, including ``input_kinds`` (directory vs file
input) and the owning ``tool_id``. The only ``tool_id`` exception is the
synthetic ``cso_to_chd`` chain mode, owned by the ``chain`` tool on the backend
but grouped under ``cso`` in the UI. Skipped when Node is unavailable.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from services.tools import registry

_REGISTRY_JS = Path(__file__).resolve().parents[1] / "src" / "lib" / "tools" / "registry.js"

# Fields whose drift would actually mis-route a job. Frontend camelCase is
# normalized to these snake_case keys in the Node dump below. `input_kinds` is
# what makes folder_to_iso a directory-input mode (fileBrowser offers folders,
# not files), so it must be compared even though its extensions are empty on
# both sides.
_COMPARED_FIELDS = (
    "kind",
    "output_ext",
    "input_extensions",
    "input_kinds",
    "supports_compression",
    "supports_compression_level",
    "supports_delete_on_verify",
    "allows_archive_input",
)

# `tool_id` is compared too — a mode filed under the wrong frontend tool
# descriptor drives the wrong menu / compression style / verify bindings even
# when its per-mode fields still match — EXCEPT for these modes, whose frontend
# owner intentionally differs from the backend. `cso_to_chd` is the synthetic
# chain mode: owned by the `chain` tool on the backend, grouped under `cso` in
# the UI.
_TOOL_ID_EXCEPTIONS = frozenset({"cso_to_chd", "nkit_to_rvz"})


def _find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for cand in (os.environ.get("NODE"), "/opt/node22/bin/node", "/usr/bin/node"):
        if cand and os.path.exists(cand):
            return cand
    return None


def _eval_registry(tmp_path: Path, dump: str, name: str) -> list[dict]:
    """Evaluate ``registry.js`` under Node with *dump* appended, as JSON.

    One helper for both parity views (per mode and per tool): the Node lookup,
    the `$lib` stub, the temporary module and the subprocess are identical, and
    only the trailing snippet that shapes the output differs.
    """
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate registry.js")

    src = _REGISTRY_JS.read_text(encoding="utf-8")
    # registry.js imports the SvelteKit `$lib` alias only for the getInfo/verify
    # bindings, which these dumps never call — stub it so plain Node can
    # evaluate the module as-is (preserving every ext constant and spread).
    needle = "import { api } from '$lib/api/endpoints.js';"
    assert needle in src, "registry.js import shape changed; update the parity stub"
    src = src.replace(needle, "const api = {};") + dump
    script = tmp_path / name
    script.write_text(src, encoding="utf-8")

    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"Could not evaluate registry.js via node:\n{proc.stderr}")
    return json.loads(proc.stdout)


def _frontend_rows(tmp_path: Path) -> dict[str, dict]:
    dump = (
        "\nconst __rows = TOOLS.flatMap((t) => t.modes.map((m) => ({"
        " mode: m.mode,"
        " tool_id: t.id,"
        " kind: m.kind,"
        " output_ext: m.outputExt ?? null,"
        " input_extensions: [...m.inputExtensions].sort(),"
        " input_kinds: [...(m.inputKinds ?? ['file'])].sort(),"
        " supports_compression: !!m.supportsCompression,"
        " supports_compression_level: !!m.supportsCompressionLevel,"
        " supports_delete_on_verify: !!m.supportsDeleteOnVerify,"
        " allows_archive_input: !!m.allowsArchiveInput,"
        "})));\n"
        "process.stdout.write(JSON.stringify(__rows));\n"
    )
    rows = _eval_registry(tmp_path, dump, "registry_eval.mjs")
    return {row["mode"]: row for row in rows}


def _frontend_tools(tmp_path: Path) -> dict[str, dict]:
    """Tool-level rows from registry.js: `{id: {"default_compression": str|None}}`."""
    dump = (
        "\nconst __tools = TOOLS.map((t) => ({"
        " id: t.id,"
        " default_compression: (t.defaultCompression ?? [])[0] ?? null,"
        "}));\n"
        "process.stdout.write(JSON.stringify(__tools));\n"
    )
    rows = _eval_registry(tmp_path, dump, "registry_tools_eval.mjs")
    return {row["id"]: row for row in rows}


def _backend_rows() -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for spec in registry.mode_specs():
        rows[spec.mode] = {
            "mode": spec.mode,
            "tool_id": spec.tool_id,
            "kind": spec.kind.value,
            "output_ext": spec.output_ext,
            "input_extensions": sorted(spec.input_extensions),
            "input_kinds": sorted(k.value for k in spec.input_kinds),
            "supports_compression": spec.supports_compression,
            "supports_compression_level": spec.supports_compression_level,
            "supports_delete_on_verify": spec.supports_delete_on_verify,
            "allows_archive_input": spec.allows_archive_input,
        }
    return rows


def test_frontend_registry_mirrors_backend_mode_specs(tmp_path):
    frontend = _frontend_rows(tmp_path)
    backend = _backend_rows()

    assert set(frontend) == set(backend), (
        "Mode set drift between registry.js and registry.mode_specs():\n"
        f"  frontend-only: {sorted(set(frontend) - set(backend))}\n"
        f"  backend-only:  {sorted(set(backend) - set(frontend))}"
    )

    mismatches = []
    for mode in sorted(backend):
        fe, be = frontend[mode], backend[mode]
        for field in _COMPARED_FIELDS:
            if fe.get(field) != be.get(field):
                mismatches.append(
                    f"  {mode}.{field}: registry.js={fe.get(field)!r} "
                    f"mode_specs()={be.get(field)!r}"
                )
        if mode not in _TOOL_ID_EXCEPTIONS and fe.get("tool_id") != be.get("tool_id"):
            mismatches.append(
                f"  {mode}.tool_id: registry.js={fe.get('tool_id')!r} "
                f"mode_specs()={be.get('tool_id')!r}"
            )
    assert not mismatches, (
        "registry.js ↔ registry.mode_specs() field drift "
        "(update src/lib/tools/registry.js or the backend ModeSpec to match):\n"
        + "\n".join(mismatches)
    )


def test_default_compression_mirrors_the_frontend_seed(tmp_path):
    """`BaseTool.default_compression` ↔ `registry.js` `defaultCompression`.

    The backend resolves this codec when an automation rule sets a compression
    *level* but leaves the codec on "tool default" — the level rides on the
    codec string, so ``":19"`` would reach dolphin-tool as ``-c "" -l 19`` and
    fail every job. The manual picker seeds the same choice from
    ``defaultCompression``, so the two must name the same codec or the same
    rule would compress differently depending on which surface queued it.

    Declaring one is *required* for every tool with a mode that takes both a
    codec and a level (there is no other way to express such a rule); optional
    elsewhere, where an unset level makes the question moot.
    """
    frontend = _frontend_tools(tmp_path)
    problems = []
    for tool in registry.all():
        # The chain tool has no descriptor of its own: its composite modes are
        # grouped under the descriptors of the tools they start from
        # (`cso_to_chd` under cso, `nkit_to_rvz` under nkit), the same
        # exception `_TOOL_ID_EXCEPTIONS` records above. Nothing to mirror.
        fe = frontend.get(tool.id)
        needs_default = any(
            m.supports_compression and m.supports_compression_level
            for m in tool.modes
        )
        if needs_default and not tool.default_compression:
            problems.append(
                f"  {tool.id}: has a codec+level mode but declares no "
                "default_compression, so a level with no codec cannot be sent"
            )
        if (
            fe is not None
            and tool.default_compression is not None
            and tool.default_compression != fe["default_compression"]
        ):
            problems.append(
                f"  {tool.id}.default_compression: backend="
                f"{tool.default_compression!r} registry.js="
                f"{fe['default_compression']!r}"
            )
    assert not problems, (
        "tool-level compression-default drift:\n" + "\n".join(problems)
    )
