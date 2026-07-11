from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Optional

from config import settings


def strip_archive_path(path: str) -> str:
    """Return the filesystem portion of an archive path (archive::internal)."""
    return path.split("::", 1)[0] if "::" in path else path


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
