"""Guard the delete-plan confirmation modal against duplicate `{#each}` keys.

``DeletePlanModal.svelte`` renders the plan's blocking reasons and warnings as
keyed each blocks. A Svelte key must uniquely identify its item, so a repeated
key is a hard runtime error (``each_key_duplicate``) that unmounts the whole
workspace view through ``<svelte:boundary>`` — the modal that exists to warn
about a destructive action crashes instead.

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
  lists through — collapses them to one entry per distinct message, evaluated
  with Node so the real module runs, not a Python re-implementation.

Identity is separate from presentation there, and the tests hold that line: the
key is the raw message, while the rendered text carries the ``(×N)`` count and
is *not* safe to key on (a message ending in that suffix collides with a
different message repeated that many times). A static check keeps the modal
wired to the helper and keyed on the identity field. Node-dependent tests skip
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
    """Locate a Node binary, or None when the suite must skip."""
    found = shutil.which("node")
    if found:
        return found
    for cand in (os.environ.get("NODE"), "/opt/node22/bin/node", "/usr/bin/node"):
        if cand and os.path.exists(cand):
            return cand
    return None


def _summarize(tmp_path: Path, messages: list[str]) -> list[dict]:
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
    keys = [entry["key"] for entry in summarized]

    assert len(keys) == len(set(keys)), (
        f"these keys collide and would crash the view: {keys}"
    )
    assert summarized == [{"key": _ARCHIVE_WARNING, "text": f"{_ARCHIVE_WARNING} (×3)", "count": 3}]


def test_summarize_counts_repeats_and_keeps_first_seen_order(tmp_path):
    """Distinct messages collapse in first-seen order, repeats carry a count."""
    assert _summarize(tmp_path, ["b", "a", "b", "c", "b"]) == [
        {"key": "b", "text": "b (×3)", "count": 3},
        {"key": "a", "text": "a", "count": 1},
        {"key": "c", "text": "c", "count": 1},
    ]
    assert _summarize(tmp_path, ["only once"]) == [
        {"key": "only once", "text": "only once", "count": 1},
    ]
    assert _summarize(tmp_path, []) == []


def test_rendered_text_is_never_used_as_the_key(tmp_path):
    """A message that already ends in the count suffix must not collide.

    The rendered text is presentation, not identity: `["a", "a", "a (×2)"]`
    renders two identical `a (×2)` lines, so keying on the rendered form would
    still crash. Messages embed user-controlled paths (a file named
    `game (×2).iso` is enough to construct this pair), so the key is the raw
    message — unique because that is what the helper deduplicates on.
    """
    summarized = _summarize(tmp_path, ["a", "a", "a (×2)"])

    keys = [entry["key"] for entry in summarized]
    texts = [entry["text"] for entry in summarized]

    assert keys == ["a", "a (×2)"]
    assert len(keys) == len(set(keys)), f"keys collide: {keys}"
    # The hazard this guards: the *rendered* forms are the colliding pair.
    assert len(set(texts)) < len(texts), (
        "expected the rendered text to collide here; if it no longer does, this "
        "test has stopped covering the suffix-collision case"
    )


def test_modal_keyed_message_lists_use_a_summarized_identity_key():
    """Static guard: message lists must be summarized and keyed on `.key`.

    Two ways to reintroduce the crash: drop `summarizeMessages` (duplicate raw
    messages), or key on the rendered text instead of the identity field. This
    fails on either.
    """
    src = _MODAL.read_text(encoding="utf-8")

    keyed_lists = re.findall(r"{#each\s+(\w+)\.slice\([^)]*\)\s+as\s+m\s+\(([^)]*)\)}", src)
    assert keyed_lists, "DeletePlanModal no longer renders keyed message lists"

    for name, key_expr in keyed_lists:
        assert key_expr.strip() == "m.key", (
            f"`{name}` is keyed by `{key_expr.strip()}`; key on the raw-message "
            "identity (`m.key`), never the rendered text, which can collide"
        )
        derived = re.search(
            rf"const {name} = \$derived\.by\(\(\) => {{(.*?)\n  }}\);",
            src,
            re.DOTALL,
        )
        assert derived, f"could not locate the `{name}` derivation in DeletePlanModal"
        assert "summarizeMessages(" in derived.group(1), (
            f"`{name}` feeds a keyed list but is not deduped through "
            "summarizeMessages — duplicate messages would crash the view"
        )
