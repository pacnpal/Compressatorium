"""Guards for the RomM views' save and warning wiring.

Three things these screens must not do, each of which costs the operator
something the UI had just told them was safe:

* the Metadata card's Save must not submit the connection buffers — the
  backend reads a changed URL or library root as a move to another instance,
  which clears the conversion history and retires every pending snapshot;
* enabling the master automation switch must commit pending rule edits first,
  or the scheduler runs the *server's* rules while the editor shows the
  operator's;
* the "we save your metadata and restore it" promise must not be made for a
  split build, whose numbered parts RomM cannot hash-match at all.

The first is asserted behaviourally by evaluating the component's `<script>`
with Node (runes reduced, sibling stores stubbed). The other two are wiring,
and are asserted against the source.
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
_SETTINGS = _SRC / "lib" / "components" / "views" / "RommSettings.svelte"
_AUTOMATION = _SRC / "lib" / "components" / "views" / "RommAutomation.svelte"


def _find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for cand in (os.environ.get("NODE"), "/opt/node22/bin/node", "/usr/bin/node"):
        if cand and os.path.exists(cand):
            return cand
    return None


def _script_body(src: str) -> str | None:
    """The text between the first `<script ...>` and its `</script ...>`.

    Located by index rather than by regular expression on purpose. A regex tag
    filter has to enumerate the spellings a tag can take — `<script lang="ts">`,
    `<SCRIPT>`, `</script\\t\\n foo>` — and the ones it forgets are the ones it
    silently mismatches. Here that would mean reading an *empty* component and
    passing. Two case-insensitive searches have no such gaps, and CodeQL flags
    the regex form (`py/bad-tag-filter`) for exactly this reason.
    """
    lowered = src.lower()
    start = lowered.find("<script")
    if start == -1:
        return None
    opened = src.find(">", start)
    if opened == -1:
        return None
    end = lowered.find("</script", opened)
    if end == -1:
        return None
    return src[opened + 1:end]


def _script_of(component: Path) -> str:
    """The component's `<script>` body as plain ESM.

    Runes reduce to their plain meaning for a single-shot run: `$state(x)` and
    `$derived(x)` are just `x` with nothing subscribed, `$props()` is an empty
    prop bag, and `$effect(fn)` runs once — which is what fills the edit
    buffers from the loaded settings.
    """
    body = _script_body(component.read_text(encoding="utf-8"))
    assert body, f"{component.name} has no <script> block"
    kept = [
        line for line in body.splitlines()
        if not (line.strip().startswith("import ") and line.strip().endswith(";"))
    ]
    text = "\n".join(kept)
    text = text.replace("$props()", "({})")
    text = text.replace("$effect(", "((__fn) => __fn())(")
    text = re.sub(r"\$state\.raw\(", "(", text)
    text = re.sub(r"\$state\(", "(", text)
    text = re.sub(r"\$derived\(", "(", text)
    leftover = [r for r in ("$state(", "$derived(", "$effect(", "$props(") if r in text]
    assert not leftover, f"{component.name} grew runes this harness cannot reduce: {leftover}"
    return text


_STUBS = """
const __saved = [];
const toast = { success() {}, error() {}, info() {}, warning() {} };
const romm = {
  settings: {
    url: 'http://romm:8080', library_root: '/library',
    repin_enabled: true, repin_on_load: true, repin_abandon_days: 7,
    env_defaults: [],
  },
  settingsSaving: false,
  testResult: null,
  async saveSettings(patch) { __saved.push(patch); return this.settings; },
  async testConnection(patch) { __saved.push(patch); return {}; },
};
"""


def _run(tmp_path: Path, component: Path, scenario: str) -> dict:
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate the RomM views")
    script = tmp_path / "romm_view_eval.mjs"
    script.write_text(_STUBS + _script_of(component) + scenario, encoding="utf-8")
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"Could not evaluate {component.name} via node:\n{proc.stderr}")
    return json.loads(proc.stdout)


_METADATA_SAVE = """
// The operator has half-typed a new URL in the Connection card above, and has
// not pressed its Save. Then they toggle a metadata checkbox and save *that*.
url = 'http://new-rom';
libraryRoot = '/somewhere/else';
token = 'rmm_half_typed';
repinOnLoad = false;

await handleSaveMetadata();

process.stdout.write(JSON.stringify({
  submitted: __saved,
  // What the Connection card would send is unchanged — this is about which
  // button sends which fields, not about forbidding connection edits.
  connection_keys: Object.keys(patch()).sort(),
}));
"""


def test_saving_metadata_does_not_submit_the_connection_buffers(tmp_path):
    """A metadata toggle must not commit a half-typed connection edit.

    Both cards shared one handler, and it always submitted the URL, token and
    library-root buffers. A URL or root that differs from the saved one is an
    identity change on the backend: it clears the conversion history and
    retires every pending re-pin snapshot. Toggling "re-apply on load" is not a
    reason to lose those — and if the settings load had failed, the buffers are
    still empty, so it would have submitted a *blank* URL and root.
    """
    data = _run(tmp_path, _SETTINGS, _METADATA_SAVE)

    assert len(data["submitted"]) == 1, data
    sent = data["submitted"][0]
    assert sorted(sent) == ["repin_abandon_days", "repin_enabled", "repin_on_load"], sent
    assert sent["repin_on_load"] is False, sent
    # The Connection card still owns those fields.
    assert "url" in data["connection_keys"], data
    assert "library_root" in data["connection_keys"], data


def test_both_save_buttons_wait_for_the_settings_to_load(tmp_path):
    """Empty buffers submitted as a connection read as a move to nowhere.

    Until the saved settings arrive the edit buffers are `''`. Saving then
    blanks the URL and library root, which is an identity change — the one
    that retires pending snapshots.
    """
    src = _SETTINGS.read_text(encoding="utf-8")
    buttons = re.findall(r"<Button\b[^>]*?onclick=\{(handleSave\w*)\}(.*?)/>", src, re.DOTALL)
    found = {name for name, _ in buttons}
    assert found == {"handleSave", "handleSaveMetadata"}, found
    for name, attrs in buttons:
        assert "disabled={!loaded}" in attrs, (
            f"{name}'s button can fire before the settings have loaded"
        )


def test_enabling_automation_commits_pending_rule_edits():
    """The scheduler reads the server's rules, not the editor's.

    Preview and Run now both save first. The master switch did not, so turning
    it on with unsaved edits started unattended runs against the previous
    configuration — which may still have the platform enabled, or
    delete-on-verify on, while the editor showed the safer one just typed.
    """
    src = _AUTOMATION.read_text(encoding="utf-8")
    body = re.search(
        r"async function toggleAuto\(enabled\) \{(.*?)\n  \}", src, re.DOTALL,
    )
    assert body, "toggleAuto is no longer where this guard can find it"
    assert "commitPendingEdits()" in body.group(1), (
        "toggleAuto enables the scheduler without saving pending rule edits; "
        "Preview and Run now both commit first"
    )
    # Guarded on `enabled`: switching automation OFF must stay immediate, and
    # must not commit edits the operator never saved.
    assert re.search(r"if \(enabled && !\(await commitPendingEdits\(\)\)\) return;",
                     body.group(1)), body.group(1)


def test_navigating_into_a_catalog_folder_leaves_the_romm_view():
    """Dropping catalog mode has to drop the screen that renders it.

    A RomM row can resolve to a directory (a decrypted PS3 game). Clicking it
    exits catalog mode and loads the folder, but the view stayed on `romm` --
    so the platform toolbar sat above unrelated directory entries, and that
    shell renders no breadcrumbs and no parent link. The folder was a one-way
    trip, with no route back to the catalog short of leaving and reopening the
    whole view.
    """
    src = (_SRC / "lib" / "stores" / "fileBrowser.svelte.js").read_text(encoding="utf-8")
    body = re.search(
        r"if \(this\.rommPlatformId !== null\) \{(.*?)\n    \}", src, re.DOTALL,
    )
    assert body, "the exit-RomM branch of navigate() is no longer where this guard looks"
    assert "ui.navigate('workspace')" in body.group(1), (
        "navigate() drops RomM mode without leaving the RomM view, which has "
        "no breadcrumbs to get back with"
    )


def test_the_repin_promise_is_qualified_for_split_builds():
    """A split build has no single file for RomM to hash.

    Past 4 GB the tool writes `Game.iso.0`, `.1`, … and the backend retires the
    re-pin row saying exactly that. The warning promised the metadata would be
    restored regardless, so an unattended conversion finished with no recovery
    and no warning that there would not be one.
    """
    src = _AUTOMATION.read_text(encoding="utf-8")
    block = re.search(
        r"\{#if romm\.losesDatMatch\(spec\)\}(.*?)\{/if\}", src, re.DOTALL,
    )
    assert block, "the lost-DAT-match warning is no longer where this guard can find it"
    warning = block.group(1)
    assert "restores it for you" in warning, warning
    assert "rule.split" in warning, (
        "the warning promises metadata restoration unconditionally; a split "
        "build's numbered parts cannot be hash-matched at all"
    )
