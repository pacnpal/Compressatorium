"""Guard the delete-plan confirmation modal against duplicate `{#each}` keys.

``DeletePlanModal.svelte`` renders the plan's blocking reasons and warnings as
keyed each blocks keyed by the message *string*. A Svelte key must uniquely
identify its item, so a repeated message is a hard runtime error
(``each_key_duplicate``) that unmounts the whole workspace view through
``<svelte:boundary>`` — the modal that exists to warn about a destructive
action crashes instead.

The backend routinely produces repeats: ``build_delete_plan`` appends a fixed
"Archive input detected…" warning once per archive source, and the convert
route appends an identical "multiple selections from the same archive" error
once per offending member. So *any* multi-archive delete-on-verify selection
used to crash the view.

These tests pin both halves of the contract:

* the backend really does emit byte-identical messages across items (the
  precondition — if that ever stops being true the frontend guard still holds,
  but the test should be revisited); and
* ``summarizeMessages`` — the shared helper the modal now runs those flattened
  lists through — collapses them to unique keys, evaluated with Node so the
  real module runs, not a Python re-implementation.

A static check keeps the modal wired to the helper, so a future edit can't
reintroduce a raw flattened list under a message key. Node-dependent tests skip
when Node is unavailable.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from utils.delete_plan import build_delete_plan

_SRC = Path(__file__).resolve().parents[1] / "src"
_MESSAGES_JS = _SRC / "lib" / "util" / "messages.js"
_MODAL = _SRC / "lib" / "components" / "modals" / "DeletePlanModal.svelte"

_ARCHIVE_WARNING = "Archive input detected; delete-on-verify will remove the entire archive"


def _find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for cand in (os.environ.get("NODE"), "/opt/node22/bin/node", "/usr/bin/node"):
        if cand and os.path.exists(cand):
            return cand
    return None


def _summarize(tmp_path: Path, messages: list[str]) -> list[str]:
    """Run the real `summarizeMessages` over `messages` via Node."""
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate the frontend message helper")

    src = _MESSAGES_JS.read_text(encoding="utf-8").replace("export function", "function")
    script = tmp_path / "summarize_eval.mjs"
    script.write_text(
        src
        + "\nconst __input = "
        + json.dumps(messages)
        + ";\nprocess.stdout.write(JSON.stringify(summarizeMessages(__input)));\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"Could not evaluate src/lib/util/messages.js via node:\n{proc.stderr}")
    return json.loads(proc.stdout)


def _archive_member_plans(tmp_path: Path, count: int) -> list[dict]:
    """Delete plans for one member of each of `count` real archives."""
    plans = []
    for i in range(count):
        archive = tmp_path / f"Game{i} (2000)(Sega)[!].zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(f"Game{i}.iso", b"FAKEISO")
        plans.append(build_delete_plan(f"{archive}::Game{i}.iso"))
    return plans


def test_multi_archive_plan_repeats_the_same_warning(tmp_path):
    """Precondition: the backend emits one identical warning per archive."""
    warnings = [w for plan in _archive_member_plans(tmp_path, 3) for w in plan["warnings"]]

    assert warnings == [_ARCHIVE_WARNING] * 3
    # The duplicate-key hazard itself: as a flat list these are not unique.
    assert len(set(warnings)) < len(warnings)


def test_repeated_warnings_summarize_to_unique_keys(tmp_path):
    """The keys the modal renders must be unique for a multi-archive plan."""
    warnings = [w for plan in _archive_member_plans(tmp_path, 3) for w in plan["warnings"]]

    summarized = _summarize(tmp_path, warnings)

    assert len(summarized) == len(set(summarized)), (
        "DeletePlanModal keys its warning list by the message string; these "
        f"keys collide and would crash the view: {summarized}"
    )
    assert summarized == [f"{_ARCHIVE_WARNING} (×3)"]


def test_summarize_counts_repeats_and_keeps_first_seen_order(tmp_path):
    assert _summarize(tmp_path, ["b", "a", "b", "c", "b"]) == ["b (×3)", "a", "c"]
    assert _summarize(tmp_path, ["only once"]) == ["only once"]
    assert _summarize(tmp_path, []) == []


def test_modal_keyed_message_lists_run_through_the_helper():
    """Static guard: a message-keyed each block must iterate a summarized list.

    Keying by the message is fine *because* the list is deduped first. This
    fails if a future edit reintroduces a raw flattened list under that key.
    """
    src = _MODAL.read_text(encoding="utf-8")

    keyed_lists = set(re.findall(r"{#each\s+(\w+)\.slice\([^)]*\)\s+as\s+m\s+\(m\)}", src))
    assert keyed_lists, "DeletePlanModal no longer renders message lists keyed by the message"

    for name in sorted(keyed_lists):
        derived = re.search(
            rf"const {name} = \$derived\.by\(\(\) => {{(.*?)\n  }}\);",
            src,
            re.DOTALL,
        )
        assert derived, f"could not locate the `{name}` derivation in DeletePlanModal"
        assert "summarizeMessages(" in derived.group(1), (
            f"`{name}` is rendered keyed by the message string but is not deduped "
            "through summarizeMessages — duplicate messages would crash the view"
        )
