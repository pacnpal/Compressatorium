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

import os
from pathlib import Path

from services.lock_manager import lock_manager
from services.tools import ModeKind, registry

# Upper bound on the `name_1`, `name_2`, ... search. Each probe stats the disk
# (a directory mode's companion lookup scans), so an unbounded walk over a
# directory already full of numbered outputs is quadratic on a sweep's hot path.
MAX_RENAME_ATTEMPTS = 1000


class OutputPathExhausted(RuntimeError):
    """No free ``name_N`` within :data:`MAX_RENAME_ATTEMPTS`."""


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
    the caller must not write to. Raises :class:`OutputPathExhausted` when the
    bounded search finds no free name.
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
    for counter in range(1, MAX_RENAME_ATTEMPTS + 1):
        candidate = str(parent / f"{stem}_{counter}{suffix}")
        if not _taken(candidate):
            return candidate
    raise OutputPathExhausted(
        f"No free output path after {MAX_RENAME_ATTEMPTS} attempts: {base_path}",
    )


# What the duplicate policy decided for one candidate. Returned rather than
# branched on at the call site so a caller reads as one table lookup.
QUEUE = "queue"
SKIP_EXISTING = "skip_existing"
SKIP_LOCKED = "skip_locked"


def _same_file(path_a: str, path_b: str) -> bool:
    """Whether two paths name the same file once symlinks are resolved."""
    try:
        return os.path.realpath(path_a) == os.path.realpath(path_b)
    except OSError:
        # Unreadable either way: treat as "same" so the caller refuses rather
        # than authorises an overwrite it could not rule out.
        return True


def resolve_destination(
    tool, path: str, mode: str, output_dir: str | None, duplicate_action: str,
) -> tuple[str | None, str]:
    """Where a conversion of *path* should write, and whether to queue it.

    Returns ``(destination, decision)``. Shared by the RomM automation sweep and
    the re-pin planner so both agree on the path a conversion will actually
    produce — recording metadata against a destination the batch then renames
    would re-pin the wrong file.

    ``detect_output()`` is the wrong tool for this: it only ever looks *beside
    the source* and takes no output directory, so a rule with ``output_dir`` set
    never found its own output. ``tool.output_path()`` derives the real
    destination instead, the same SSOT the manual path uses.

    The duplicate policy is then applied through the helpers ``/api/jobs`` uses,
    so ``overwrite`` and ``rename`` mean here exactly what they mean there
    rather than silently degrading to ``skip``:

    * ``skip`` -- an occupied destination drops the candidate;
    * ``overwrite`` -- reuses it, unless a job holds it right now;
    * ``rename`` -- probes ``name_1``, ``name_2``, ... for a free one.

    A locked destination is never queued: the next pass picks it up once the
    lock clears, because the filesystem still reports the source unconverted.
    """
    try:
        destination = tool.output_path(mode, path, output_dir)
    except (KeyError, ValueError, OSError):
        return None, SKIP_EXISTING

    # A conversion must never be authorised to write over its own input. A
    # library manager rescans what we produce, so a standing "convert to RVZ"
    # rule eventually sees the .rvz it made: dolphin accepts .rvz as input,
    # output_path maps it straight back onto itself, and an `overwrite` policy
    # would then unlink the file before reading it -- destroying the only copy
    # when the source was already deleted. Only a COPY mode (chdman .chd ->
    # .chd) rewrites in place on purpose.
    if registry.spec(mode).kind is not ModeKind.COPY and _same_file(destination, path):
        return None, SKIP_EXISTING

    exists, locked = check_output_conflicts(mode, destination)
    if not exists:
        return destination, QUEUE
    if duplicate_action == "overwrite":
        return (None, SKIP_LOCKED) if locked else (destination, QUEUE)
    if duplicate_action == "rename":
        try:
            return get_unique_output_path(destination, mode), QUEUE
        except (OutputPathLocked, OutputPathExhausted):
            return None, SKIP_LOCKED
    return None, SKIP_EXISTING
