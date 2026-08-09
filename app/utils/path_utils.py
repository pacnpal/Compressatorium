from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Optional

from config import settings


def strip_archive_path(path: str) -> str:
    """Return the filesystem portion of an archive path (archive::internal)."""
    return path.split("::", 1)[0] if "::" in path else path


def match_extension(name: str, extensions: Iterable[str]) -> Optional[str]:
    """Return the longest declared extension ``name`` ends with, else ``None``.

    The shared primitive behind every "does this tool handle this file?"
    check (``input_extensions`` / ``verify_extensions`` / the archive-member
    gate). It is a **suffix** match rather than ``Path(name).suffix``
    equality so a tool can declare a *compound* extension: nkit2iso's sources
    are ``.nkit.iso`` / ``.nkit.gcz``, whose single ``Path.suffix`` is the
    generic ``.iso`` / ``.gcz`` that CHDMAN and Dolphin already own. Matching
    on the full suffix lets the specific format be declared without either
    tool having to claim (or disclaim) the generic one.

    For the single-component extensions every other tool declares this is
    exactly the old behaviour: ``game.iso`` ends with ``.iso`` and nothing
    else. Longest-match resolution means a name that matches both a compound
    and its generic tail reports the compound one, so a caller that needs the
    concrete key (the archive listing) gets the most specific answer.

    ``name`` may be a full path, a bare filename, or an extension string
    (``".nkit.iso"`` still ends with ``".iso"``), so callers holding only a
    member's extension can use the same helper.
    """
    lower = name.lower()
    best: Optional[str] = None
    for ext in extensions:
        if lower.endswith(ext) and (best is None or len(ext) > len(best)):
            best = ext
    return best


def _resolve_path(raw_path: str, *, strict: bool = False) -> Optional[Path]:
    """Safely resolve a user-supplied path without following non-existent segments.

    Explicitly rejects symlink loops: prior to Python 3.13, ``Path.resolve()``
    raised :class:`RuntimeError` for an infinite-loop symlink, which this
    helper caught and treated as "cannot be safely resolved".  Python 3.13+
    instead returns the path unchanged from ``resolve()``, which would let a
    dangling loop slip past the volume-containment check as if it were a real
    file.  We restore the pre-3.13 semantics by probing the path with
    ``os.stat`` (follows symlinks), ELOOP surfaces as ``OSError`` and we
    return ``None`` so the caller rejects the path.
    """
    try:
        path_obj = Path(raw_path).expanduser()
        resolved = path_obj.resolve(strict=strict)
    except (OSError, RuntimeError):
        return None

    # Additional ELOOP probe for Python 3.13+ where ``resolve()`` no longer
    # raises for symlink loops.
    try:
        os.stat(resolved)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None
        # ENOENT / EACCES / other, not necessarily a security issue; fall
        # through and let the caller decide.  strict=False callers expect
        # non-existent paths to be resolvable (used for "would this output
        # path be valid?" checks).
    return resolved


def _resolve_volume(volume_path: str) -> Optional[Path]:
    try:
        return Path(volume_path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def is_within_configured_volumes(path: str, *, treat_archives: bool = True) -> bool:
    """Check whether the given path lies inside one of the configured CHD volumes."""
    base_path = strip_archive_path(path) if treat_archives else path
    real_path = _resolve_path(base_path, strict=False)
    if real_path is None:
        return False

    for volume in settings.volumes:
        real_volume = _resolve_volume(volume)
        if real_volume is None:
            continue

        try:
            real_path.relative_to(real_volume)
            return True
        except ValueError:
            if real_path == real_volume:
                return True
            continue

    return False


def _strip_trailing_dot_and_seps(path: str) -> str:
    """Strip only *trailing* separators and ``.`` components from ``path``.

    Used to find the submitted root entry to ``lstat`` before resolution. Unlike
    :func:`os.path.normpath`, interior ``..`` and symlinked components are left
    intact: normalizing ``/vol/anchor/../LinkGame`` to ``/vol/LinkGame`` would
    lexically cancel a symlinked ``anchor`` against the following ``..``, so a
    pre-resolve ``lstat`` would probe a benign lexical path instead of the real
    final component the kernel reaches (``/outside/LinkGame``). Keeping the
    interior intact lets ``lstat`` resolve the ancestors exactly as the kernel
    does and report the true final entry (catching a symlinked root), while the
    trailing ``/`` / ``/.`` / ``/./`` are removed so the probe lands on that
    entry itself rather than following it.
    """
    seps = os.sep + (os.altsep or "")
    drive, tail = os.path.splitdrive(path)
    prev = None
    while tail != prev:
        prev = tail
        tail = tail.rstrip(seps)
        # Drop a standalone trailing "." component (".", ".../.") — but never the
        # "." inside a ".." component, which stays meaningful.
        if tail.endswith(".") and (len(tail) == 1 or tail[-2] in seps):
            tail = tail[:-1]
    return (drive + tail) or path


def source_companions_are_safe(file_path: str, mode: str) -> bool:
    """Whether every sibling input this mode's tool also consumes stays in-volume.

    The single-file analogue of :func:`is_safe_directory_tree`, and it exists
    for the same reason: a multi-file source drags in files the request never
    named, and the native converter opens them *itself* — JNUSLib enumerates a
    split Wii U set's parts from part 1 rather than taking a list from us — so
    the volume boundary the route enforced on ``file_path`` has to be enforced
    on the companions too. Rejects a symlinked companion outright rather than
    following it, plus anything resolving outside the configured volumes.

    Scoped to ``mode``'s own tool, not the registry-wide union: another
    plugin's multi-file semantics must not veto a job it has no part in (a
    chdman ``createhd`` on a raw file that happens to be named
    ``game_part1.wud`` never reads the sibling parts). A no-op for the tools
    whose ``source_companions`` is ``[]``.

    Like the directory check this is a *point-in-time* answer, so both callers
    matter: ``plan_job`` rejects at queue time, and the worker re-checks after
    taking the job's locks, since a queued job's companion can be swapped for a
    symlink in between.

    Blocking I/O (an ``lstat`` and a ``realpath`` per companion); call it off
    the event loop.
    """
    from services.tools import registry  # local: avoids an import cycle

    try:
        tool = registry.for_mode(mode)
    except KeyError:
        return True
    for companion in tool.source_companions(file_path):
        if os.path.islink(companion):
            return False
        if not is_within_configured_volumes(
            os.path.realpath(companion), treat_archives=False,
        ):
            return False
    return True


def is_safe_directory_tree(path: str) -> bool:
    """Return whether a directory tree is safe for native recursive readers.

    Native tools such as makeps3iso recursively read the source tree outside of
    Python's path guards.  Reject symlinks and non-regular filesystem entries so
    the confined root's real subtree is the only thing the native reader sees.
    """
    # Reject a symlinked source root before resolving it. A trailing separator
    # or "." component (".../LinkGame/", ".../LinkGame/.", ".../LinkGame/./")
    # makes ``os.path.islink``/``os.lstat`` follow the link to its target, so a
    # request like ``/volume/LinkGame/`` would otherwise resolve the symlink
    # away and hand the native packer a root pointing outside the configured
    # volume. Strip only those trailing components (see the helper) and ``lstat``
    # the resulting root: the kernel resolves any symlinked ancestors while
    # ``lstat`` reports the final entry without following it, so a symlinked
    # root is caught even behind a "." or a ".."-cancelled symlinked ancestor.
    raw_root = _strip_trailing_dot_and_seps(path)
    try:
        raw_lstat = os.lstat(raw_root)
    except OSError:
        return False
    if stat.S_ISLNK(raw_lstat.st_mode):
        return False

    root = _resolve_path(path, strict=True)
    if root is None or not root.is_dir():
        return False

    root_str = str(root)
    if not is_within_configured_volumes(root_str, treat_archives=False):
        return False

    walk_errors: list[OSError] = []

    def _record_walk_error(error: OSError) -> None:
        walk_errors.append(error)

    for current, dirs, files in os.walk(
        root_str, topdown=True, followlinks=False, onerror=_record_walk_error,
    ):
        if walk_errors:
            return False
        entries = [*dirs, *files]
        for name in entries:
            entry = os.path.join(current, name)
            try:
                entry_lstat = os.lstat(entry)
            except OSError:
                return False

            mode = entry_lstat.st_mode
            if stat.S_ISLNK(mode):
                return False
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                return False
            # No per-entry resolve/volume re-check: ``os.walk(followlinks=False)``
            # never descends through a symlink, and every symlink or special
            # entry is rejected above, so each visited entry is a genuine child
            # of the already volume-confined ``root``. Resolving and
            # re-verifying containment for every file would add tens of
            # thousands of redundant syscalls on a large PS3 tree while proving
            # something the traversal already guarantees.

    return not walk_errors


def is_configured_volume_root(path: str, *, treat_archives: bool = True) -> bool:
    """Check whether the given path is exactly one of the configured volume roots."""
    base_path = strip_archive_path(path) if treat_archives else path
    real_path = _resolve_path(base_path, strict=False)
    if real_path is None:
        return False

    for volume in settings.volumes:
        real_volume = _resolve_volume(volume)
        if real_volume is not None and real_path == real_volume:
            return True

    return False


def ensure_path_within_volumes(path: str, *, treat_archives: bool = True) -> Path:
    """Return the resolved path if it is within configured volumes, else raise ValueError."""
    if not is_within_configured_volumes(path, treat_archives=treat_archives):
        raise ValueError("Path outside configured volumes")
    resolved = _resolve_path(
        strip_archive_path(path) if treat_archives else path, strict=False
    )
    if resolved is None:
        raise ValueError("Path could not be resolved")
    return resolved


def get_volume_name_for_path(path: str) -> Optional[str]:
    """Return the configured volume name that contains the given path, if any."""
    base_path = strip_archive_path(path)
    real_path = _resolve_path(base_path, strict=False)
    if real_path is None:
        return None

    for volume in settings.volumes:
        real_volume = _resolve_volume(volume)
        if real_volume is None:
            continue

        try:
            real_path.relative_to(real_volume)
            return settings.get_volume_name(volume)
        except ValueError:
            if real_path == real_volume:
                return settings.get_volume_name(volume)
            continue

    return None


def safe_join(base_dir: str, *parts: str) -> Path:
    """Join parts to a base directory while ensuring the result stays inside the base."""
    resolved_base = _resolve_path(base_dir, strict=True)
    if resolved_base is None:
        raise ValueError("Base directory does not exist")

    candidate = resolved_base.joinpath(*parts).resolve(strict=False)
    try:
        candidate.relative_to(resolved_base)
    except ValueError as exc:
        raise ValueError("Resulting path escapes base directory") from exc

    return candidate


def cleanup_orphan_lock(lock_path: str):
    """Best-effort removal of leftover lock files."""
    try:
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except OSError:
        pass
