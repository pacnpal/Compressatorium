"""Regression: unique-name probing must not spin-loop inside a locked subtree.

When a ``folder_to_iso`` job holds a directory subtree lock (it is packing a
PS3 folder), every path *inside* that folder reports ``is_locked=True`` via the
subtree-aware ``check_file_status``. A concurrent per-file job using
``duplicate_action="rename"`` whose output lands in the folder would probe
``name_1``, ``name_2``, … — all inside the same locked subtree, all locked — so
the ``while True`` loops with no sleep, burning a thread until the dir lock
releases, then returns an arbitrarily numbered name.

A rename whose every candidate is inside a held subtree can never succeed, so it
must be rejected up front (``SkipFile(OUTPUT_LOCKED)``) like skip/overwrite do,
not spin-looped.
"""

from __future__ import annotations

import threading

import pytest

# Use the exact module-level ``lock_manager`` binding the helpers reference, so
# the dir lock we acquire is the same singleton they consult. Under PYTHONPATH=app
# the ``app.services.lock_manager`` and ``services.lock_manager`` aliases are
# distinct singletons (see test_makeps3iso), so going through convert is required.
from app.routes import convert as convert_routes


def _call_with_watchdog(fn, *args, timeout: float = 4.0) -> dict:
    """Run ``fn(*args)`` in a daemon thread; capture its result or exception.

    Raises ``TimeoutError`` if it does not return within ``timeout`` — i.e. it
    spin-looped — so the bug surfaces as a clean test failure instead of hanging
    the whole suite.
    """
    box: dict = {}

    def run() -> None:
        try:
            box["result"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001 - capture SkipFile et al.
            box["exc"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError("unique-path helper did not return — spin-looped under dir lock")
    return box


# mode=None is the plain single-file probe; "folder_to_iso" exercises the
# companion-aware path (a split set's numbered parts). After #182 both run
# through the single ``get_unique_output_path`` helper.
@pytest.mark.parametrize("mode", [None, "folder_to_iso"])
def test_rename_inside_locked_subtree_rejected_not_looped(tmp_path, mode):
    folder = tmp_path / "MyGame"
    (folder / "PS3_GAME").mkdir(parents=True)
    from services.lock_manager import lock_manager  # noqa: PLC0415

    assert lock_manager.acquire_dir_lock(str(folder)) is True
    try:
        # A per-file output (e.g. chdman create) that lands inside the locked
        # PS3 folder being packed.
        output_path = str(folder / "PS3_GAME" / "game.iso")

        box = _call_with_watchdog(
            convert_routes.get_unique_output_path, output_path, mode,
        )

        exc = box.get("exc")
        assert isinstance(exc, convert_routes.SkipFile), (
            f"expected SkipFile, got result={box.get('result')!r} exc={exc!r}"
        )
        assert exc.reason is convert_routes.SkipReason.OUTPUT_LOCKED
    finally:
        lock_manager.release_dir_lock(str(folder))
