import os
import re
import shlex
from pathlib import Path
from typing import Dict, List, Set

from utils.path_utils import strip_archive_path


_WIN_ABS_RE = re.compile(r"^[a-zA-Z]:[\\/]")


def _is_absolute_reference(ref: str) -> bool:
    ref = ref.strip()
    if not ref:
        return False
    if ref.startswith("~"):
        return True
    if ref.startswith("\\\\"):
        return True
    if _WIN_ABS_RE.match(ref):
        return True
    return Path(ref).is_absolute()


def _resolve_track_path(base_dir: Path, ref: str) -> Path:
    raw = ref.strip().strip('"')
    if not raw:
        raise ValueError("Empty track reference")
    normalized_ref = raw.replace("\\", "/")
    if _is_absolute_reference(normalized_ref):
        raise ValueError(f"Absolute track reference is not allowed: {raw}")

    base_resolved = Path(os.path.normpath(str(base_dir.absolute())))
    candidate = Path(os.path.normpath(os.path.join(str(base_resolved), normalized_ref)))
    try:
        candidate.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(f"Track reference escapes source directory: {raw}") from exc

    return candidate


def _parse_cue_tracks(cue_path: Path) -> List[str]:
    tracks: List[str] = []
    with cue_path.open("r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.upper().startswith("REM"):
                continue
            if not stripped.upper().startswith("FILE"):
                continue
            try:
                parts = shlex.split(stripped, comments=True, posix=True)
            except ValueError:
                continue
            if len(parts) < 3 or parts[0].upper() != "FILE":
                continue
            name = parts[1].strip()
            if name:
                tracks.append(name)
    return tracks


def _parse_gdi_tracks(gdi_path: Path) -> List[str]:
    tracks: List[str] = []
    with gdi_path.open("r", encoding="utf-8", errors="ignore") as fh:
        lines = [line.strip() for line in fh if line.strip()]

    if not lines:
        return tracks

    def _split(line: str) -> List[str]:
        try:
            return shlex.split(line, comments=True, posix=True)
        except ValueError:
            return line.split()

    head = _split(lines[0])
    start_idx = 1 if head and head[0].isdigit() else 0
    for line in lines[start_idx:]:
        parts = _split(line)
        if len(parts) < 5:
            continue
        name = parts[4].strip('"').strip()
        if name:
            tracks.append(name)
    return tracks


def build_delete_plan(source_path: str) -> Dict[str, object]:
    original_source = source_path
    is_archive_member = "::" in source_path
    plan_source = strip_archive_path(source_path) if is_archive_member else source_path
    source = Path(plan_source)
    base_dir = source.parent
    resolved: Set[str] = set()
    delete_paths: List[str] = []
    missing_paths: List[str] = []
    unsafe_paths: List[str] = []
    errors: List[str] = []
    warnings: List[str] = []

    def _add_path(path: Path) -> None:
        path_str = str(path)
        if os.path.islink(path_str):
            errors.append(f"Delete path is a symlink: {path_str}")
        real = os.path.realpath(path)
        if real in resolved:
            return
        resolved.add(real)
        delete_paths.append(real)
        if not os.path.exists(real):
            missing_paths.append(real)
        elif not os.path.isfile(real):
            errors.append(f"Delete path is not a file: {real}")

    try:
        _add_path(source)
        if is_archive_member:
            warnings.append(
                "Archive input detected; delete-on-verify will remove the entire archive"
            )
            return {
                "source_path": original_source,
                "delete_paths": delete_paths,
                "missing_paths": missing_paths,
                "unsafe_paths": unsafe_paths,
                "errors": errors,
                "warnings": warnings,
            }
        ext = source.suffix.lower()
        track_refs: List[str] = []
        if ext == ".cue":
            track_refs = _parse_cue_tracks(source)
        elif ext == ".gdi":
            track_refs = _parse_gdi_tracks(source)
        if ext in {".cue", ".gdi"} and not track_refs:
            errors.append("No track references found")

        for ref in track_refs:
            try:
                track_path = _resolve_track_path(base_dir, ref)
            except ValueError as exc:
                unsafe_paths.append(str(exc))
                continue
            _add_path(track_path)

        # Sources that are really a *set* of files report the rest of the set
        # through the registry (jwud: a split Wii U dump's game_part2.wud …
        # game_part12.wud). Without this, deleting the source after a verified
        # conversion would remove the part the job named and orphan ~23 GB of
        # siblings. Imported lazily so this leaf utility keeps no import-time
        # dependency on the tool package that builds the registry.
        from services.tools import registry

        for companion in registry.source_companions(str(source)):
            _add_path(Path(companion))
    except Exception as exc:
        errors.append(str(exc))

    return {
        "source_path": original_source,
        "delete_paths": delete_paths,
        "missing_paths": missing_paths,
        "unsafe_paths": unsafe_paths,
        "errors": errors,
        "warnings": warnings,
    }


def collect_delete_paths(source_path: str) -> List[str]:
    plan = build_delete_plan(source_path)
    errors = list(plan.get("errors", []))
    unsafe = list(plan.get("unsafe_paths", []))
    missing = list(plan.get("missing_paths", []))

    if errors or unsafe:
        details = "; ".join(errors + unsafe)
        raise ValueError(details or "Unsafe delete plan")
    if missing:
        raise ValueError("Missing companion files; refusing to delete")

    return list(plan.get("delete_paths", []))


def build_delete_snapshot(source_path: str) -> Dict[str, object]:
    plan = build_delete_plan(source_path)
    errors = list(plan.get("errors", []))
    unsafe = list(plan.get("unsafe_paths", []))
    missing = list(plan.get("missing_paths", []))

    if errors or unsafe:
        details = "; ".join(errors + unsafe)
        raise ValueError(details or "Unsafe delete plan")
    if missing:
        raise ValueError("Missing companion files; refusing to delete")

    fingerprints: Dict[str, Dict[str, int]] = {}
    for path in plan.get("delete_paths", []):
        try:
            st = os.stat(path, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise ValueError("Missing companion files; refusing to delete") from exc
        if os.path.islink(path):
            raise ValueError(f"Delete path is a symlink: {path}")
        if not os.path.isfile(path):
            raise ValueError(f"Delete path is not a file: {path}")
        fingerprints[path] = {
            "size": int(st.st_size),
            "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
            "inode": int(getattr(st, "st_ino", 0)),
            "device": int(getattr(st, "st_dev", 0)),
        }

    return {
        "paths": list(plan.get("delete_paths", [])),
        "fingerprints": fingerprints,
    }
