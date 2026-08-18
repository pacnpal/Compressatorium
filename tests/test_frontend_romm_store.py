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
const __defer = () => new Promise((resolve, reject) => {
  __calls.push({ resolve, reject });
});
const __reloads = [];
const api = {
  getRommPlatforms: (...a) => { __reloads.push('platforms'); return __defer(...a); },
  saveRommRules: __defer,
  getRommStatus: (...a) => { __reloads.push('status'); return __defer(...a); },
  saveRommSettings: async (patch) => ({ ...patch, saved: true }),
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


_ABANDONED_LOAD = """
const store = new RommStore();
const load = store.loadPlatforms();
await tick();

// The view unmounts while the request is outstanding.
store.cancelPlatformLoad();
fileBrowser.rommPlatformId = null;   // what exitRomm() leaves behind

// ...and only now does RomM answer.
__calls[0].resolve([{ id: 9, name: 'New', tool_ids: null, mode_ids: null }]);
await load;
await tick();
// Sampled here, before anything legitimate re-enters: this is the value the
// workspace would open with.
const entered_after_cancel = fileBrowser.rommPlatformId;

// A later visit must still be able to load.
const again = store.loadPlatforms();
await tick();
__calls[1]?.resolve([{ id: 9, name: 'New', tool_ids: null, mode_ids: null }]);
await again;

process.stdout.write(JSON.stringify({
  entered_after_cancel,
  requests: __calls.length,
  entered_after_revisit: fileBrowser.rommPlatformId,
  platforms: store.platforms.map((p) => p.name),
}));
"""


def test_an_abandoned_platform_load_cannot_re_enter_romm_mode(tmp_path):
    """Leaving the RomM view has to invalidate the load still in flight.

    A load ends by selecting a platform, which calls `enterRomm()`. The view's
    cleanup restores the ordinary directory listing — and the outstanding
    request then put the catalog straight back into the browser, so the
    workspace opened showing RomM rows under a directory heading. The view's
    own `alive` flag cannot cover it: the side effect happens inside the store.
    """
    data = _run(tmp_path, _ABANDONED_LOAD)

    # Nothing re-entered RomM mode on the abandoned load's behalf.
    assert data["entered_after_cancel"] is None, data
    # The next deliberate visit still works — the cancel must not leave the
    # store wedged as "a load is already running".
    assert data["requests"] == 2, data
    assert data["entered_after_revisit"] == 9, data
    assert data["platforms"] == ["New"], data


_SAVE_RACE = """
const store = new RommStore();
store.rules = { '7': { mode: 'dolphin_rvz', enabled: true } };

// The operator saves...
const saving = store.saveRules();
await tick();
// ...and keeps editing while the request is in flight. This one turns a
// platform OFF, which is exactly the edit that must not be lost.
store.setRule('9', { mode: 'dolphin_rvz', enabled: false });

// The server answers with its normalized copy of what was SUBMITTED.
__calls[0].resolve({ rules: { '7': { mode: 'dolphin_rvz', enabled: true } } });
await saving;

process.stdout.write(JSON.stringify({
  rules: store.rules,
  dirty: store.rulesDirty,
}));
"""


def test_a_rule_edited_during_a_save_is_not_silently_discarded(tmp_path):
    """The editor stays live while the save is in flight.

    The response carries the server's copy of what was *submitted*, and
    adopting it replaced anything typed since — then cleared the dirty flag, so
    the Save bar stopped asking and the edit was gone with no warning. The one
    that matters most is the edit that makes a rule safer.
    """
    data = _run(tmp_path, _SAVE_RACE)

    assert "9" in data["rules"], data
    assert data["rules"]["9"]["enabled"] is False, data
    # Still dirty, so the next save normalizes it rather than losing it.
    assert data["dirty"] is True, data


_QUIET_SAVE = """
const store = new RommStore();
store.rules = { '7': { mode: 'dolphin_rvz', enabled: true } };
const saving = store.saveRules();
await tick();
__calls[0].resolve({ rules: { '7': { mode: 'dolphin_rvz', enabled: true, order: 'largest' } } });
await saving;
process.stdout.write(JSON.stringify({ rules: store.rules, dirty: store.rulesDirty }));
"""


def test_an_undisturbed_save_still_adopts_the_server_copy(tmp_path):
    """The server clamps numbers and drops stale modes; the editor must show it."""
    data = _run(tmp_path, _QUIET_SAVE)
    assert data["rules"]["7"]["order"] == "largest", data
    assert data["dirty"] is False, data


_BADGE = """
const store = new RommStore();
store.status = { pendingRepins: 3 };
// A retry records a row that SUPERSEDES the existing one, so the backend's
// count is unchanged. Adding the newly recorded row inflated the badge.
store.setPendingRepins(3);
const afterRetry = store.status.pendingRepins;
store.setPendingRepins(0);
const afterSettle = store.status.pendingRepins;
// Nothing to report leaves it alone rather than zeroing a real backlog.
store.setPendingRepins(undefined);
process.stdout.write(JSON.stringify({
  afterRetry, afterSettle, afterUnknown: store.status.pendingRepins,
}));
"""


def test_the_repin_badge_is_set_from_the_backend_count(tmp_path):
    """Recording is not addition.

    `record()` supersedes the pending row for a destination rather than
    stacking one, so re-recording a retried or redirected conversion leaves the
    total unchanged — and adding each recorded row inflated the badge on every
    retry, staying wrong until a status reload or a settle pass.
    """
    data = _run(tmp_path, _BADGE)
    assert data["afterRetry"] == 3, data
    assert data["afterSettle"] == 0, data
    assert data["afterUnknown"] == 0, data


_STALE_STATUS = """
const store = new RommStore();

// The first status read is still waiting on the old, unreachable URL.
const first = store.loadStatus();
await tick();
// The operator saves a new connection, which reads status again.
const second = store.loadStatus();
await tick();

// The new instance answers first: reachable, library mounted.
__calls[1].resolve({ configured: true, connected: true, library_root: '/new',
                     library_root_mounted: true });
await second;
// ...and the old one answers afterwards, reporting the failure that made the
// operator change it in the first place.
__calls[0].resolve({ configured: true, connected: false, library_root: '/old',
                     library_root_mounted: false, error: 'unreachable' });
await first;
await tick();

process.stdout.write(JSON.stringify({
  requests: __calls.length,
  libraryRoot: store.status?.libraryRoot,
  connected: store.status?.connected,
  error: store.status?.error,
  loading: store.statusLoading,
}));
"""


def test_a_stale_status_answer_cannot_overwrite_the_new_connection(tmp_path):
    """The read that is still waiting is asking the server being replaced.

    A slow or unreachable URL is exactly why the operator is changing it, so
    that request is the one most likely to be outstanding when the save runs.
    Letting its answer land afterwards left the view reporting the new instance
    as unusable while its platforms and catalog had already loaded.
    """
    data = _run(tmp_path, _STALE_STATUS)

    assert data["requests"] == 2, data
    assert data["libraryRoot"] == "/new", data
    assert data["connected"] is True, data
    assert data["error"] is None, data
    assert data["loading"] is False, data


_POLICY_SAVE = """
const store = new RommStore();
const before = __reloads.length;
// Toggling automation, or saving a metadata preference. Neither says anything
// about which server or which files are being described.
await store.saveSettings({ auto_convert: true });
await store.saveSettings({ repin_enabled: false, repin_abandon_days: 14 });
const afterPolicy = __reloads.length - before;

// ...whereas any of these is a different instance, or a different view of it.
await Promise.race([store.saveSettings({ url: 'http://other:8080' }), tick()]);
const afterUrl = __reloads.length - before - afterPolicy;

process.stdout.write(JSON.stringify({ afterPolicy, afterUrl, reloads: __reloads }));
"""


def test_a_policy_only_save_does_not_rescan_the_catalog(tmp_path):
    """Only an identity change invalidates what is on screen.

    `saveSettings` re-read the status and force-reloaded the platform list
    after *every* save, and reloading re-enters the selected platform, whose
    catalog load stats every ROM in it. So toggling automation on a large
    remote library held the Save action for the catalog scan's multi-minute
    bound and discarded a listing that was still correct.

    Keyed off what the patch carried rather than a diff of the saved settings,
    because the token is never returned to the browser: a credential change is
    only ever visible here as a submitted field.
    """
    data = _run(tmp_path, _POLICY_SAVE)

    assert data["afterPolicy"] == 0, data
    # The connection save still re-derives everything downstream.
    assert data["afterUrl"] > 0, data
    assert "status" in data["reloads"], data
