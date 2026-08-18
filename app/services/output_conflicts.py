"""Output-path conflict detection, shared by every path that queues a job.

Whether a mode's output is already taken is not a property of the HTTP layer:
the manual conversion endpoints and the RomM automation sweep both have to ask
it, and they must get the same answer or "skip existing" means two different
things depending on how the job was started.

Both helpers are companion-aware — an ``extractcd`` ``.bin`` sidecar or a split
``folder_to_iso`` build's numbered parts count as occupying the destination —
because the owning tool enumerates them through ``companion_outputs`` rather
than each caller re-deriving the set.

They touch the disk (a directory mode's companion lookup scans), so call them
off the event loop.
"""

from __future__ import annotations

from pathlib import Path

from services.lock_manager import lock_manager
from services.tools import registry


class OutputPathLocked(RuntimeError):
    """A rename would have to land inside a directory another job holds.

    Raised instead of returning a path so no caller can silently write into a
    subtree that is being packed. The API layer translates it into its own
    skip reason; the sweep simply drops the candidate.
    """


def check_output_conflicts(mode: str, output_path: str) -> tuple[bool, bool]:
    """``(exists, locked)`` for an output path *and all of its companion outputs*."""
    file_exists, is_locked = lock_manager.check_file_status(output_path)
    exists = file_exists or is_locked
    locked = is_locked
    for companion in registry.for_mode(mode).companion_outputs(output_path, mode):
        c_exists, c_locked = lock_manager.check_file_status(companion)
        exists = exists or c_exists or c_locked
        locked = locked or c_locked
    return exists, locked


def get_unique_output_path(base_path: str, mode: str | None = None) -> str:
    """Unique output path, appending ``_N`` until the file — and, when ``mode``
    is supplied, that mode's companion outputs — are all free.

    ``mode=None`` checks the bare path only (the plain single-file case). A mode
    routes the probe through :func:`check_output_conflicts`, so a sibling output
    (extractcd's ``.bin``, a split ``folder_to_iso``'s numbered parts) can't be
    silently clobbered by a rename. This subsumes the former per-mode
    ``get_unique_*`` helpers — one companion-aware probe for every mode.

    Raises :class:`OutputPathLocked` when the base path sits inside a locked
    directory tree: incrementing a counter there would loop forever over paths
    the caller must not write to.
    """
    def _taken(candidate: str) -> bool:
        if mode is None:
            file_exists, is_locked = lock_manager.check_file_status(candidate)
            return file_exists or is_locked
        exists, _locked = check_output_conflicts(mode, candidate)
        return exists

    if not _taken(base_path):
        return base_path

    if lock_manager.is_within_locked_dir(base_path):
        raise OutputPathLocked(base_path)

    path = Path(base_path)
    stem, suffix, parent = path.stem, path.suffix, path.parent
    counter = 1
    while True:
        candidate = str(parent / f"{stem}_{counter}{suffix}")
        if not _taken(candidate):
            return candidate
        counter += 1
