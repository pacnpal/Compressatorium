"""Guards for the shared suffix-based extension match (``match_extension``).

Extension matching used to be ``Path(name).suffix.lower() in declared``, which
can only ever see one trailing component. nkit2iso's sources are *compound*
(``.nkit.iso`` / ``.nkit.gcz``) and their trailing component is the generic
``.iso`` / ``.gcz`` that CHDMAN, Dolphin and maxcso already own, so the match
is now a longest-suffix match against each tool's declared set.

These tests pin both halves of that: the helper itself, and the invariant that
every *existing* single-component declaration behaves exactly as before.
"""
from __future__ import annotations

import pytest

from app.services.tools import registry
from app.utils.path_utils import match_extension


@pytest.mark.parametrize(
    ("name", "declared", "expected"),
    [
        # Plain single-component behaviour, unchanged.
        ("game.iso", {".iso", ".cue"}, ".iso"),
        ("/data/sub/game.CUE", {".iso", ".cue"}, ".cue"),
        ("game.txt", {".iso", ".cue"}, None),
        ("game", {".iso"}, None),
        # Longest match wins, so the specific format beats the generic tail.
        ("game.nkit.iso", {".iso", ".nkit.iso"}, ".nkit.iso"),
        ("game.nkit.gcz", {".gcz", ".nkit.gcz"}, ".nkit.gcz"),
        # ...but a plain file never matches a compound declaration.
        ("game.iso", {".nkit.iso"}, None),
        ("game.gcz", {".nkit.gcz"}, None),
        # A bare extension string is a valid subject, so a caller holding only a
        # member's recorded extension can use the same helper.
        (".nkit.iso", {".iso"}, ".iso"),
        (".iso", {".nkit.iso"}, None),
        # The compound must be a real suffix, not a coincidental tail.
        ("nkit.iso", {".nkit.iso"}, None),
        ("mygameiso", {".iso"}, None),
        # Empty declaration set (a tool with no verify class).
        ("game.iso", set(), None),
    ],
)
def test_match_extension(name, declared, expected):
    assert match_extension(name, declared) == expected


def test_every_other_tool_declares_only_single_component_extensions():
    """nkit2iso is the only compound-extension tool today.

    If a second one appears, the sites that still record a plain
    ``Path.suffix`` for display (the archive listing's ``extension`` field, the
    frontend icon buckets) need a look — this test is the tripwire, not a ban.
    """
    compound = {
        (tool.id, ext)
        for tool in registry.all()
        for ext in tool.input_extensions | tool.output_extensions
        if ext.count(".") > 1
    }
    assert compound == {
        ("nkit", ".nkit.iso"), ("nkit", ".nkit.gcz"),
        # The nkit_to_rvz chain declares step 1's inputs as its own.
        ("chain", ".nkit.iso"), ("chain", ".nkit.gcz"),
    }
