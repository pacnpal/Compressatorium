"""Behavioural guards for the RomM store's async bookkeeping.

``src/lib/stores/romm.svelte.js`` drives the Library tab, and its loads
overlap: a settings save re-derives the catalog while the first load of the
session may still be in flight. Which answer wins is not cosmetic -- the
selected platform is what the Convert panel narrows to and what the submit
path sends -- so it is asserted here rather than left to review.

Evaluated with Node, the same engine the app uses, with the runes reduced to
plain fields (``$state(x)`` is just ``x`` for a single-shot run) and the
sibling stores stubbed. Skipped when Node is unavailable.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
_ROMM_STORE_JS = _SRC / "lib" / "stores" / "romm.svelte.js"


def _find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for cand in (os.environ.get("NODE"), "/opt/node22/bin/node", "/usr/bin/node"):
        if cand and os.path.exists(cand):
            return cand
    return None


def _plain_js() -> str:
    """The store as plain ESM: imports dropped, runes reduced to their value.

    ``$state(x)`` is a reactive box around ``x``; outside a component nothing
    subscribes, so the initialiser alone is the whole behaviour under test.
    """
    src = _ROMM_STORE_JS.read_text(encoding="utf-8")
    kept = [
        line for line in src.splitlines()
        if not (line.strip().startswith("import ") and line.strip().endswith(";"))
    ]
    body = "\n".join(kept)
    body = re.sub(r"\$state\.raw\(", "(", body)
    body = re.sub(r"\$state\(", "(", body)
    leftover = [
        rune for rune in ("$state(", "$derived(", "$derived.by(", "$effect(")
        if rune in body
    ]
    assert not leftover, (
        f"romm.svelte.js grew runes this harness does not reduce: {leftover}"
    )
    return body


_STUBS = """
const __calls = [];
const api = {
  getRommPlatforms: () => new Promise((resolve, reject) => {
    __calls.push({ resolve, reject });
  }),
};
const fileBrowser = { rommPlatformId: null, exitRomm() {}, async enterRomm(id) {
  this.rommPlatformId = id;
} };
const conversion = {
  mode: 'createcd', primaryTool: 'chdman',
  setPrimaryTool(id) { this.primaryTool = id; }, setMode(m) { this.mode = m; },
};
const ui = { workspaceTool: 'chdman' };
const registry = { all: () => [], forTool: () => null };
const tick = () => new Promise((resolve) => setImmediate(resolve));
"""


def _run(tmp_path: Path, scenario: str) -> dict:
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate the RomM store")
    script = tmp_path / "romm_store_eval.mjs"
    script.write_text(_STUBS + _plain_js() + scenario, encoding="utf-8")
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"Could not evaluate the RomM store via node:\n{proc.stderr}")
    return json.loads(proc.stdout)


_OVERLAPPING_LOADS = """
const store = new RommStore();

// The load already in flight when the connection details change. It is asking
// the OLD instance.
const first = store.loadPlatforms();
// The save handler's forced reload, asking the new one.
const second = store.loadPlatforms({ force: true });

await tick();
// The new instance answers first...
__calls[1]?.resolve([{ id: 9, name: 'New', tool_ids: null, mode_ids: null }]);
await second;
// ...and the old one afterwards, which is the whole point: its answer is late,
// and it describes a server nobody is pointed at any more.
__calls[0].resolve([{ id: 1, name: 'Old', tool_ids: null, mode_ids: null }]);
await first;
await tick();

process.stdout.write(JSON.stringify({
  requests: __calls.length,
  platforms: store.platforms.map((p) => p.name),
  selected: store.selectedPlatformId,
  loading: store.platformsLoading,
  error: store.platformsError,
}));
"""


def test_a_superseded_platform_load_cannot_select_the_old_instance(tmp_path):
    """The forced reload after a settings save must win, whenever it lands.

    `loadPlatforms` bailed while another load was in flight, so the reload that
    runs when the URL/token changes did nothing and the in-flight request --
    aimed at the previous instance -- populated the catalog afterwards, then
    selected one of its platforms. The Convert panel narrows to that selection
    and the submit path sends it, so the user could queue conversions against a
    platform belonging to a server they had just navigated away from.
    """
    data = _run(tmp_path, _OVERLAPPING_LOADS)

    # The forced load actually asked, rather than deferring to the stale one.
    assert data["requests"] == 2, data
    assert data["platforms"] == ["New"], data
    assert data["selected"] == 9, data
    # And the late answer does not leave the spinner or an error behind.
    assert data["loading"] is False, data
    assert data["error"] is None, data


_UNFORCED_LOAD = """
const store = new RommStore();
const first = store.loadPlatforms();
const second = store.loadPlatforms();
await tick();
__calls[0].resolve([{ id: 1, name: 'Old', tool_ids: null, mode_ids: null }]);
await first;
await second;
process.stdout.write(JSON.stringify({ requests: __calls.length }));
"""


def test_an_unforced_platform_load_still_defers_to_the_one_in_flight(tmp_path):
    """Only `force` supersedes. The plain de-duplication has to survive.

    Several surfaces call `loadPlatforms()` on mount; turning every one of them
    into its own request would put a burst of full catalog listings on a large
    instance for one screen.
    """
    assert _run(tmp_path, _UNFORCED_LOAD)["requests"] == 1
