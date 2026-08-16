from logging_setup import get_logger
import os
import re
import shutil
from pathlib import Path

from config import settings
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from models import (
    BulkDeleteRequest,
    DirectoryListing,
    FileEntry,
    MetadataBatchRequest,
    OutputStatus,
    Volume,
)
from services.archive import ARCHIVE_EXTENSIONS, archive_service
from services.chd_metadata_store import chd_metadata_store
from services.job_manager import job_manager
from services.lock_manager import lock_manager
from services.tools import registry
from services.verification_store import verification_store
from utils.junk import is_junk_entry
from utils.path_utils import (
    ensure_path_within_volumes,
    get_volume_name_for_path,
    is_configured_volume_root,
    is_within_configured_volumes,
)

router = APIRouter()
logger = get_logger("files")

# Upper bound on archives summarized in one /archive-summary request. The browser
# hydrates the visible page, so this is a safety ceiling, not the normal size.
MAX_ARCHIVE_SUMMARY_PATHS = 1000
ACTION_CONFIRM_HEADER = "x-chd-action-confirm"
CONFIRM_RENAME_FILE = "rename-file"
CONFIRM_DELETE_FILE = "delete-file"
CONFIRM_RECURSIVE_DELETE = "recursive-delete"


# A makeps3iso ``-s`` split set: ``Game.iso.0``, ``Game.iso.1``, … The numeric
# suffix follows a real ``.iso`` so a plain ``.iso`` never matches.
_SPLIT_ISO_PART_RE = re.compile(r"^(?P<base>.+\.iso)\.(?P<idx>\d+)$", re.IGNORECASE)


def _fold_split_iso_entries(entries: list[FileEntry]) -> list[FileEntry]:
    """Collapse a makeps3iso split set into one logical ISO entry.

    A ``-s`` build over 4 GB writes ``Game.iso.0`` / ``.1`` / … and no plain
    ``Game.iso``. Listing them raw would scatter a single game across several
    ``.0`` / ``.1`` rows, so fold each set (keyed on the ``.0`` part) into one
    entry: display name ``Game.iso``, ``path`` pointing at the ``.0`` part (what
    RPCS3 mounts), ``size`` summed across parts, and ``split_parts`` = the count.
    The set is a final FAT32 deliverable, not a convertible source, so it carries
    no ``convertible_by`` / ``verifiable_by`` (you'd re-pack from the folder, not
    a partial first chunk). An orphan ``.1`` with no ``.0`` — or a lone ``.0``
    with no ``.1`` (an interrupted split) — is left untouched.
    """
    groups: dict[str, dict[int, FileEntry]] = {}
    for entry in entries:
        if entry.type != "file":
            continue
        match = _SPLIT_ISO_PART_RE.match(entry.name)
        if match:
            groups.setdefault(match.group("base"), {})[int(match.group("idx"))] = entry
    # Fold only a *complete* multi-part set: ≥2 indices contiguous from 0
    # (0,1,…,N-1). An interrupted copy/write can leave a gap (e.g. .0 + .2, no
    # .1); folding that would render one "valid" Game.iso hiding a
    # corrupt/incomplete set. A lone ``.0`` is likewise the state left by a split
    # interrupted before ``.1`` exists — a valid one-file result is the plain
    # ``.iso``, never a bare ``.0`` — so folding it would hide the raw partial row
    # and disable the single-row/bulk delete actions needed for recovery. Leave
    # such parts visible as their raw .N rows instead.
    foldable = {
        base: parts
        for base, parts in groups.items()
        # ≥2 parts, contiguous from 0 (no gaps, no lone first chunk).
        if len(parts) >= 2 and set(parts) == set(range(len(parts)))
    }
    if not foldable:
        return entries

    result: list[FileEntry] = []
    emitted: set[str] = set()
    for entry in entries:
        match = _SPLIT_ISO_PART_RE.match(entry.name) if entry.type == "file" else None
        base = match.group("base") if match else None
        if base in foldable:
            if base in emitted:
                continue  # a sibling part of an already-folded set — drop it
            parts = foldable[base]
            result.append(
                FileEntry(
                    name=base,
                    path=parts[0].path,  # the .0 part is the mount/readback target
                    type="file",
                    size=sum(p.size or 0 for p in parts.values()),
                    extension=".iso",
                    split_parts=len(parts),
                ),
            )
            emitted.add(base)
            continue
        result.append(entry)
    return result


def _detect_file_outputs(
    item_path: str,
    *,
    from_archive: bool = False,
) -> tuple[list[str], list[OutputStatus], dict[str, OutputStatus]]:
    """Drive convertibility + output detection off the tool registry.

    Returns the tool ids that accept this input, the detected sibling
    outputs, and a ``{tool_id: OutputStatus}`` index used to derive the
    legacy per-tool booleans from a single source of truth.

    Convertibility is matched against the whole filename rather than a
    pre-computed ``Path.suffix``, so a tool declaring a compound extension
    (nkit2iso's ``.nkit.iso``) is recognised — see
    ``utils.path_utils.match_extension``.

    ``from_archive`` marks ``item_path`` as the synthesised location an archive
    member would occupy rather than a real file, and is forwarded to each
    tool's ``detect_output``.
    """
    convertible_by: list[str] = []
    outputs: list[OutputStatus] = []
    by_tool: dict[str, OutputStatus] = {}
    for tool in registry.all():
        # ``converts_path`` defaults to that same whole-filename match; a tool
        # whose source is a *set* of files narrows it per-file (jwud leaves the
        # secondary members of a split Wii U dump visible but non-convertible,
        # since only game_part1.wud drives the set).
        if tool.converts_path(item_path):
            convertible_by.append(tool.id)
        status = tool.detect_output(item_path, from_archive=from_archive)
        if status is not None:
            outputs.append(status)
            by_tool[tool.id] = status
    return convertible_by, outputs, by_tool


def _detect_directory_outputs(
    item_path: str,
) -> tuple[list[str], list[OutputStatus]]:
    """Registry-driven convertibility + output detection for a directory row.

    The directory analogue of :func:`_detect_file_outputs`: ask the registry
    which tools accept this folder as their input unit (``tools_for_directory``
    runs each tool's source-layout detector) and collect their sibling outputs
    (``<folder>.iso``). Cheap but does disk I/O (a stat for ``PS3_GAME/`` and a
    small SFO header read) — call it inside the threadpool scan, never on the
    event loop.
    """
    convertible_by: list[str] = []
    outputs: list[OutputStatus] = []
    for tool in registry.tools_for_directory(item_path):
        convertible_by.append(tool.id)
        status = tool.detect_output(item_path)
        if status is not None:
            outputs.append(status)
    return convertible_by, outputs


def _detect_archive_member_outputs(
    entry: dict, archive_dir: str,
) -> tuple[str, list[str], list[OutputStatus], dict[str, OutputStatus]]:
    """Registry-driven convertibility + output detection for an archive member.

    The member doesn't exist on disk yet, but each tool's ``detect_output``
    resolves its sibling output by swapping the suffix on the input path. So we
    synthesise the path the extracted member *would* occupy next to its archive
    (``<archive_dir>/<output_stem><ext>``) and reuse the same on-disk detection
    the regular file scan uses. This keeps the listing in lock-step with the
    job pipeline — which extracts the member into the archive's directory before
    converting — and means any newly registered archive-aware tool surfaces in
    the browse/search views without another CHD-style special case.

    The path is synthetic, so detection is told so (``from_archive=True``, the
    listing-side counterpart of the ``treat_as_stem=True`` ``plan_job`` passes
    for the same member). Without it a tool whose output name depends on the
    input's *neighbours* would read this directory's real contents as if they
    belonged to the member: a jwud member named ``game_part1.wud`` beside a
    genuine extracted split set would badge the set's ``game.wux`` while the
    planner targeted ``game_part1.wux``.
    """
    output_stem = (
        entry.get("output_stem") or Path(entry["internal_path"]).stem
    )
    # Extension-PRESERVING flattened name (not stem + trailing suffix): a
    # compound-extension source keeps its whole ".nkit.iso", which is what the
    # owning tool's detect_output needs to resolve its sibling output. For a
    # plain extension the two are identical.
    synthetic_input = os.path.join(
        archive_dir, archive_service._output_name_for_member(entry["internal_path"]),
    )
    _, outputs, by_tool = _detect_file_outputs(synthetic_input, from_archive=True)
    # `_detect_file_outputs` derives convertibility from each tool's
    # `converts_path` — but a member is only convertible in *place* when a tool
    # can accept
    # it straight from an archive. Re-derive against `allows_archive_input` so a
    # listing-only member (a romz ROM) is shown without offering a conversion
    # `plan_job` would reject (ARCHIVE_INPUT_NOT_ALLOWED).
    # Pass the member's NAME, not just its extension: a compound-extension
    # source (.nkit.iso) is only distinguishable from the generic tail the
    # listing records (.iso) by the full name.
    convertible_by = registry.tools_accepting_archive_member(entry["internal_path"])
    return output_stem, convertible_by, outputs, by_tool


def _verifiable_by(path: str) -> list[str]:
    """Tool ids whose Verify/Info apply to this concrete on-disk path.

    Registry-driven per-file refinement of ``verify_extensions``: romz inspects
    an archive's members so only single-ROM ``.7z``/``.zip`` claim it, not every
    archive. The frontend gates the Verify/Info row-actions on this flag instead
    of an extension match, so the affordance is only offered where a tool can
    actually handle the file. Called inside the threadpool scan because
    ``verifies_path`` may list an archive's members (blocking I/O).
    """
    return [t.id for t in registry.tools_verifying_path(path)]


def _summarize_archive(item_path: str) -> dict:
    """Per-archive summary fields for the file listing / archive-summary batch.

    Reads the archive once (via the shared mtime-cached member reader) and
    derives: the member count, how many members already have a sibling output
    from any tool, whether the listing hit the archive limits, and the
    per-archive ``verifiable_by`` gate (romz only claims single-ROM .7z/.zip).
    Shared by the inline ``summarize_archives=True`` path and the lazy
    ``/archive-summary`` batch the browser hydrates with, so both report
    identical badges. Blocking I/O — call it off the event loop.
    """
    archive_result = archive_service.list_archive_contents(
        item_path, include_meta=True,
    )
    contents = archive_result["entries"]
    archive_dir = os.path.dirname(item_path)
    archive_has_output = 0
    for entry in contents:
        _, _, _, member_by_tool = _detect_archive_member_outputs(entry, archive_dir)
        # A member counts as "converted" once any tool already has a sibling
        # output for it, not just CHDMAN.
        if member_by_tool:
            archive_has_output += 1
    return {
        "archive_items": len(contents),
        "archive_has_output": archive_has_output,
        "archive_truncated": bool(archive_result["truncated"]),
        "verifiable_by": _verifiable_by(item_path),
    }


async def _assert_path_not_in_use(path: str, *, is_dir: bool = False) -> None:
    _, is_locked = await run_in_threadpool(lock_manager.check_file_status, path)
    if is_locked:
        raise HTTPException(
            status_code=409, detail="Path is locked by an active conversion",
        )

    candidates = job_manager.get_active_job_candidates()

    def _match_job_id() -> str | None:
        try:
            target = Path(path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return None

        for job_id, paths in candidates:
            for candidate in paths:
                try:
                    cand_path = Path(candidate).expanduser().resolve(strict=False)
                except (OSError, RuntimeError):
                    continue
                if cand_path == target:
                    return job_id
                if is_dir:
                    try:
                        cand_path.relative_to(target)
                        return job_id
                    except ValueError:
                        continue
        return None

    job_id = await run_in_threadpool(_match_job_id)
    if job_id:
        raise HTTPException(
            status_code=409, detail=f"Path is in use by active job {job_id}",
        )

    # Also reject when the target is a descendant of an active directory job's
    # source folder (makeps3iso packing the whole subtree). The candidate-path
    # match above can't see that containment, so delegate to the registry-aware
    # central lookup which knows each job's input_kind.
    blocking = await run_in_threadpool(
        job_manager.find_active_job_for_path, path, is_dir=is_dir,
    )
    if blocking is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Path is in use by active job {blocking.id}",
        )


@router.get("/volumes", response_model=list[Volume])
async def list_volumes():
    """List all configured volume mount points."""
    volumes = []
    for vol_path in settings.volumes:
        if os.path.isdir(vol_path):
            volumes.append(
                Volume(name=settings.get_volume_name(vol_path), path=vol_path),
            )
    return volumes


@router.get("/files", response_model=DirectoryListing)
async def list_files(
    path: str = Query(..., description="Directory path to list"),
    show_archives: bool = Query(True, description="Show archive contents"),
    summarize_archives: bool = Query(True, description="Include archive summaries"),
):
    """List files in a directory."""
    try:
        resolved = ensure_path_within_volumes(path, treat_archives=False)
    except ValueError:
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes",
        ) from None

    resolved_str = str(resolved)
    if not os.path.isdir(resolved_str):
        raise HTTPException(status_code=404, detail="Directory not found")

    # Determine which volume this path belongs to
    volume_name = get_volume_name_for_path(path) or ""

    def scan_directory(
        target_path: str, show_archives_flag: bool, summarize_archives_flag: bool,
    ):
        entries = []
        try:
            # This outer try-except catches errors from os.listdir() itself
            for item in sorted(os.listdir(target_path)):
                if is_junk_entry(item):
                    continue
                item_path = os.path.join(target_path, item)
                ext = Path(item).suffix.lower()

                try:
                    if os.path.isdir(item_path):
                        # Annotate a convertible folder (a PS3 disc/JB folder
                        # makeps3iso can pack) just like a file row, so the
                        # browser can badge + select it. A plain folder gets
                        # empty lists and stays navigation-only.
                        dir_convertible_by, dir_outputs = (
                            _detect_directory_outputs(item_path)
                        )
                        entries.append(
                            FileEntry(
                                name=item,
                                path=item_path,
                                type="directory",
                                convertible_by=dir_convertible_by,
                                outputs=dir_outputs,
                            ),
                        )
                    elif os.path.isfile(item_path):
                        size = os.path.getsize(item_path)
                        is_archive = ext in ARCHIVE_EXTENSIONS
                        if is_archive and not show_archives_flag:
                            continue

                        # Registry-driven convertibility + sibling-output
                        # detection. Archives are never directly convertible
                        # (only their members are), so skip detection for them.
                        if is_archive:
                            convertible_by, outputs = [], []
                        else:
                            convertible_by, outputs, _ = _detect_file_outputs(
                                item_path,
                            )

                        archive_items = None
                        archive_has_output = None
                        archive_truncated = None
                        # verifiable_by for an archive requires opening it (romz's
                        # single-ROM gate); for a plain file it's a cheap extension
                        # check. So only the archive case is deferred when
                        # summaries are lazy — the browser then hydrates both the
                        # member counts and verifiable_by via /archive-summary.
                        verifiable_by: list[str] = []
                        if is_archive:
                            if summarize_archives_flag:
                                summary = _summarize_archive(item_path)
                                archive_items = summary["archive_items"]
                                archive_has_output = summary["archive_has_output"]
                                archive_truncated = summary["archive_truncated"]
                                verifiable_by = summary["verifiable_by"]
                        else:
                            verifiable_by = _verifiable_by(item_path)

                        entry = FileEntry(
                            name=item,
                            path=item_path,
                            type="archive" if is_archive else "file",
                            size=size,
                            extension=ext,
                            archive_items=archive_items,
                            archive_has_output=archive_has_output,
                            archive_truncated=archive_truncated,
                            convertible_by=convertible_by,
                            outputs=outputs,
                            verifiable_by=verifiable_by,
                        )
                        entries.append(entry)
                except OSError:
                    # Skip items that cannot be accessed (permissions, missing, etc.)
                    continue
        except PermissionError:
            raise HTTPException(status_code=403, detail="Permission denied") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Directory not found") from None
        return _fold_split_iso_entries(entries)

    entries = await run_in_threadpool(
        scan_directory, path, show_archives, summarize_archives,
    )

    return DirectoryListing(volume=volume_name, path=path, entries=entries)


@router.post("/archive-summary")
async def archive_summary_batch(request: MetadataBatchRequest) -> dict:
    """Per-archive summaries for the lazy file listing.

    ``/api/files`` with ``summarize_archives=false`` returns archive rows without
    member counts or ``verifiable_by`` so a directory with thousands of archives
    lists instantly instead of opening every archive on the request thread. The
    browser then calls this with the visible archive paths to hydrate those
    badges — mirroring how CHD ``media_type`` is hydrated via
    ``/api/chd-metadata``. Each path opens its archive at most once (shared
    mtime-cached reader); a non-archive / missing / out-of-volume / unreadable
    path gets an ``error`` field instead of failing the whole batch. The full
    batch runs in one threadpool hop to keep the blocking archive I/O off the
    event loop without flooding the pool with one task per path.
    """
    def _compute() -> dict:
        result: dict = {}
        # Bound how much archive-opening work one request can queue onto a single
        # threadpool worker. The browser hydrates the visible page (~one page of
        # rows), so this cap is only a defense-in-depth ceiling against an
        # oversized/hostile batch; paths beyond it get an error rather than
        # silently monopolizing the worker.
        for index, path in enumerate(request.paths):
            if index >= MAX_ARCHIVE_SUMMARY_PATHS:
                result[path] = {"error": "batch_limit_exceeded"}
                continue
            if not is_within_configured_volumes(path, treat_archives=False):
                result[path] = {"error": "path_outside_configured_volumes"}
                continue
            if not os.path.isfile(path):
                result[path] = {"error": "file_not_found"}
                continue
            if Path(path).suffix.lower() not in ARCHIVE_EXTENSIONS:
                result[path] = {"error": "not_an_archive"}
                continue
            try:
                result[path] = _summarize_archive(path)
            except Exception:
                logger.exception("Failed to summarize archive %s", path)
                result[path] = {"error": "summary_failed"}
        return result

    return await run_in_threadpool(_compute)


@router.get("/files/search")
async def search_files(
    path: str = Query(..., description="Root path to search"),
    recursive: bool = Query(True, description="Search subdirectories"),
    include_archives: bool = Query(True, description="Search inside archives"),
) -> dict:
    """Search for convertible files in a directory tree."""
    try:
        resolved = ensure_path_within_volumes(path, treat_archives=False)
    except ValueError:
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes",
        ) from None

    resolved_str = str(resolved)
    if not os.path.isdir(resolved_str):
        raise HTTPException(status_code=404, detail="Directory not found")

    def scan_directory(
        root_path: str, recursive_scan: bool, include_archive_scan: bool,
    ):
        files = []
        archives = []
        visited_dirs = set()
        archives_truncated = set()

        def _scan(dir_path: str):
            real_dir = os.path.realpath(dir_path)
            if real_dir in visited_dirs:
                return
            visited_dirs.add(real_dir)
            try:
                # Sorted for deterministic search order, matching scan_directory's
                # sorted walk above; raw readdir order varies by FS (issue #183).
                for item in sorted(os.listdir(dir_path)):
                    if is_junk_entry(item):
                        continue
                    item_path = os.path.join(dir_path, item)
                    ext = Path(item).suffix.lower()

                    try:
                        if os.path.isdir(item_path):
                            # A convertible folder (PS3 disc/JB folder) is itself
                            # a job unit: emit it as a selectable directory row so
                            # it's discoverable in search / batch-select, and do
                            # NOT recurse past it (its inner files aren't separate
                            # jobs). scan_directory alone would leave it invisible
                            # to recursive search. A plain folder still recurses.
                            dir_convertible_by, dir_outputs = (
                                _detect_directory_outputs(item_path)
                            )
                            if dir_convertible_by:
                                files.append(
                                    {
                                        "name": item,
                                        "path": item_path,
                                        "type": "directory",
                                        "size": None,
                                        "extension": "",
                                        "in_archive": False,
                                        "convertible_by": dir_convertible_by,
                                        "outputs": dir_outputs,
                                        "verifiable_by": [],
                                    },
                                )
                            elif recursive_scan and not os.path.islink(item_path):
                                _scan(item_path)
                        elif os.path.isfile(item_path):
                            is_archive = ext in ARCHIVE_EXTENSIONS
                            if is_archive:
                                convertible_by, outputs = [], []
                            else:
                                convertible_by, outputs, _ = _detect_file_outputs(
                                    item_path,
                                )
                            if convertible_by:
                                files.append(
                                    {
                                        "name": item,
                                        "path": item_path,
                                        "size": os.path.getsize(item_path),
                                        "extension": ext,
                                        "in_archive": False,
                                        "convertible_by": convertible_by,
                                        "outputs": outputs,
                                        "verifiable_by": _verifiable_by(item_path),
                                    },
                                )
                            elif include_archive_scan and is_archive:
                                # Emit the archive container itself so modes that
                                # take an archive directly (e.g. romz_extract on
                                # .7z/.zip) can select it from recursive search;
                                # the frontend gates selectability on the active
                                # mode, so it stays browse-only for member-input
                                # tools. Members are still flattened below for
                                # archive-member modes. The dict mirrors the
                                # on-disk file shape (so the JSON contract stays
                                # uniform) plus a ``type`` marker; archives never
                                # emit tool outputs, so every flag is empty/false.
                                files.append(
                                    {
                                        "name": item,
                                        "path": item_path,
                                        "type": "archive",
                                        "size": os.path.getsize(item_path),
                                        "extension": ext,
                                        "in_archive": False,
                                        "convertible_by": [],
                                        "outputs": [],
                                        # Per-archive gate: romz Verify/Info only
                                        # surface on single-ROM .7z/.zip, not on
                                        # every archive container.
                                        "verifiable_by": _verifiable_by(item_path),
                                    },
                                )
                                # List archive contents. Search surfaces
                                # convertible hits, so gate to the convert-gate
                                # subset (not the wider browse listing): that
                                # both drops list-only members (a romz ROM is
                                # browse-only) and keeps them from consuming the
                                # per-archive entry cap ahead of a genuine
                                # convertible member.
                                archive_result = archive_service.list_archive_contents(
                                    item_path, include_meta=True,
                                    convertible_only=True,
                                )
                                archive_contents = archive_result["entries"]
                                if archive_result["truncated"]:
                                    archives_truncated.add(item_path)
                                archive_dir = os.path.dirname(item_path)
                                for entry in archive_contents:
                                    (
                                        output_stem,
                                        member_convertible_by,
                                        member_outputs,
                                        _,
                                    ) = _detect_archive_member_outputs(
                                        entry, archive_dir,
                                    )
                                    archives.append(
                                        {
                                            "name": entry["name"],
                                            "path": f"{item_path}::{entry['internal_path']}",
                                            "archive_path": item_path,
                                            "internal_path": entry["internal_path"],
                                            "size": entry["size"],
                                            "extension": entry["extension"],
                                            "output_stem": output_stem,
                                            "in_archive": True,
                                            "convertible_by": member_convertible_by,
                                            "outputs": member_outputs,
                                        },
                                    )
                    except OSError:
                        # Skip archive entries that cannot be accessed
                        continue
            except (PermissionError, FileNotFoundError, OSError):
                # Directory is not accessible or disappeared during scanning; skip it.
                pass

        _scan(root_path)
        return files, archives, sorted(archives_truncated)

    files, archives, archives_truncated = await run_in_threadpool(
        scan_directory, path, recursive, include_archives,
    )

    return {
        "root": path,
        "files": files,
        "archives": archives,
        "total_files": len(files),
        "total_in_archives": len(archives),
        "archives_truncated": archives_truncated,
    }


@router.get("/files/archive")
async def list_archive(
    path: str = Query(..., description="Path to archive file"),
) -> dict:
    """List convertible files inside an archive."""
    try:
        resolved = ensure_path_within_volumes(path, treat_archives=False)
    except ValueError:
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes",
        ) from None

    resolved_str = str(resolved)
    if not os.path.isfile(resolved_str):
        raise HTTPException(status_code=404, detail="Archive not found")

    if not archive_service.is_archive(resolved_str):
        raise HTTPException(status_code=400, detail="Not a supported archive format")

    contents_result = await run_in_threadpool(
        archive_service.list_archive_contents, path, include_meta=True,
    )
    contents = contents_result["entries"]
    truncated = bool(contents_result["truncated"])

    # Registry-driven convertibility + sibling-output detection for each
    # member, mirroring the on-disk file scan so every archive-aware tool
    # (chdman, dolphin, z3ds, nsz, …) gets badged — not just CHDMAN. Each
    # member probes every tool's sibling output via lock_manager (blocking
    # disk I/O), so run the whole loop off the event loop.
    archive_dir = os.path.dirname(path)

    def _annotate_members() -> None:
        for file_entry in contents:
            output_stem, convertible_by, outputs, _ = (
                _detect_archive_member_outputs(file_entry, archive_dir)
            )
            file_entry["output_stem"] = output_stem
            file_entry["convertible_by"] = convertible_by
            file_entry["outputs"] = outputs

    await run_in_threadpool(_annotate_members)

    return {
        "archive": path,
        "files": contents,
        "total": len(contents),
        "truncated": truncated,
    }


def _require_action_confirmation(request: Request, expected: str, action: str) -> None:
    confirmation = request.headers.get(ACTION_CONFIRM_HEADER, "")
    if confirmation != expected:
        raise HTTPException(
            status_code=400,
            detail=f"Missing confirmation header for {action}",
        )


@router.post("/files/rename")
async def rename_file(
    request: Request,
    path: str = Query(..., description="Path to file or directory to rename"),
    new_name: str = Query(..., description="New name for the file or directory"),
) -> dict:
    """Rename a file or directory."""
    _require_action_confirmation(request, CONFIRM_RENAME_FILE, "rename action")

    # is_within_configured_volumes uses os.path.realpath which hits disk
    if not await run_in_threadpool(is_within_configured_volumes, path, treat_archives=False):
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes",
        )

    if not await run_in_threadpool(os.path.exists, path):
        raise HTTPException(status_code=404, detail="File or directory not found")

    is_dir = await run_in_threadpool(os.path.isdir, path)
    await _assert_path_not_in_use(path, is_dir=is_dir)

    # Validate new name (no path separators, no empty, no special chars that could be problematic)
    if not new_name or "/" in new_name or "\\" in new_name or new_name in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid new name")

    directory = os.path.dirname(path)
    new_path = os.path.join(directory, new_name)

    # Check if new path is also within allowed volumes
    if not await run_in_threadpool(is_within_configured_volumes, new_path, treat_archives=False):
        raise HTTPException(
            status_code=403,
            detail="Access denied: target path outside configured volumes",
        )

    # Check if target already exists
    if await run_in_threadpool(os.path.exists, new_path):
        raise HTTPException(
            status_code=409, detail="A file or directory with that name already exists",
        )
    await _assert_path_not_in_use(new_path, is_dir=False)

    try:
        await run_in_threadpool(os.rename, path, new_path)
        # Path-bookkeeping that mirrors the rename. verification_store
        # records carry verify-class outputs from any registered tool
        # (.chd, .rvz, .wia, .z3ds, .zcci, .zcia, …) so we have to do
        # this for the union, not just CHD. chd_metadata_store is
        # CHD-specific by design.
        #
        # Only carry the verification record across renames within the
        # SAME extension. Cross-format renames (.chd → .rvz, .rvz →
        # .wia) don't actually convert the file content, so the new
        # path was never verified under its new tool/format; carrying
        # the record would teach /api/verified to lie. Clear the old
        # record in that case and require re-verification.
        verify_exts = registry.verify_extensions()
        old_ext = os.path.splitext(path)[1].lower()
        new_ext = os.path.splitext(new_path)[1].lower()
        old_is_verify = old_ext in verify_exts
        new_is_verify = new_ext in verify_exts
        old_is_chd = old_ext == ".chd"
        new_is_chd = new_ext == ".chd"
        if old_is_verify and new_is_verify and old_ext == new_ext:
            await verification_store.move(path, new_path)
        elif old_is_verify:
            # Cross-format rename, or new ext is not a verify class.
            await verification_store.clear(path)
        if old_is_chd and new_is_chd:
            await chd_metadata_store.move(path, new_path)
        elif old_is_chd and not new_is_chd:
            await chd_metadata_store.clear(path)
        return {
            "success": True,
            "old_path": path,
            "new_path": new_path,
            "message": f"Successfully renamed to {new_name}",
        }
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to rename: {e!s}") from None


@router.delete("/files/delete")
async def delete_file(
    request: Request,
    path: str = Query(..., description="Path to file or directory to delete"),
    recursive: bool = Query(
        False,
        description=(
            "Allow deleting a non-empty directory and everything inside it. "
            "The Web UI only sets this after a double confirmation."
        ),
    ),
) -> dict:
    """Delete a file, an empty directory, or (with ``recursive``) a non-empty one."""
    if recursive:
        _require_action_confirmation(request, CONFIRM_RECURSIVE_DELETE, "recursive delete")
    else:
        _require_action_confirmation(request, CONFIRM_DELETE_FILE, "delete action")

    if not await run_in_threadpool(is_within_configured_volumes, path, treat_archives=False):
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes",
        )

    if not await run_in_threadpool(os.path.exists, path):
        raise HTTPException(status_code=404, detail="File or directory not found")

    is_dir = await run_in_threadpool(os.path.isdir, path)
    if is_dir and await run_in_threadpool(
        is_configured_volume_root, path, treat_archives=False,
    ):
        raise HTTPException(status_code=400, detail="Cannot delete a configured volume root")

    # For a recursive directory delete this also rejects the request if any
    # active job's path lives under the directory (see _assert_path_not_in_use).
    await _assert_path_not_in_use(path, is_dir=is_dir)

    try:
        if is_dir:
            contents = await run_in_threadpool(os.listdir, path)
            if contents and not recursive:
                # Non-empty without explicit opt-in: the UI re-requests with
                # recursive=true after a second confirmation.
                raise HTTPException(
                    status_code=409,
                    detail="Directory is not empty; confirm recursive delete to remove it",
                )
            if contents:
                await run_in_threadpool(shutil.rmtree, path)
            else:
                await run_in_threadpool(os.rmdir, path)
        else:
            await run_in_threadpool(os.remove, path)
            ext = os.path.splitext(path)[1].lower()
            if ext in registry.verify_extensions():
                await verification_store.clear(path)
            if ext == ".chd":
                await chd_metadata_store.clear(path)

        return {"success": True, "path": path, "message": "Successfully deleted"}
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete: {e!s}") from None


@router.post("/files/delete-batch")
async def delete_files_batch(request: Request, payload: BulkDeleteRequest) -> dict:
    """Delete multiple files at once."""
    # Batch delete shares the single-delete gate: without this the same
    # confirmation could be bypassed by wrapping a path in a one-item batch.
    _require_action_confirmation(request, CONFIRM_DELETE_FILE, "delete action")

    if not payload.paths:
        raise HTTPException(status_code=400, detail="No paths provided")

    results = []
    success_count = 0
    failed_count = 0

    async def delete_single_file(path: str) -> dict:
        """Delete a single file and return result."""
        # Validate path is within configured volumes
        if not await run_in_threadpool(is_within_configured_volumes, path, treat_archives=False):
            return {
                "path": path,
                "success": False,
                "error": "Access denied: path outside configured volumes",
            }

        if not await run_in_threadpool(os.path.exists, path):
            return {
                "path": path,
                "success": False,
                "error": "File or directory not found",
            }

        is_dir = await run_in_threadpool(os.path.isdir, path)
        try:
            await _assert_path_not_in_use(path, is_dir=is_dir)
        except HTTPException as exc:
            return {"path": path, "success": False, "error": exc.detail}

        try:
            if is_dir:
                if await run_in_threadpool(
                    is_configured_volume_root, path, treat_archives=False,
                ):
                    return {
                        "path": path,
                        "success": False,
                        "error": "Cannot delete a configured volume root",
                    }
                # Only delete empty directories for safety
                # os.listdir can be blocking
                contents = await run_in_threadpool(os.listdir, path)
                if contents:
                    return {
                        "path": path,
                        "success": False,
                        "error": "Cannot delete non-empty directory",
                    }
                await run_in_threadpool(os.rmdir, path)
            else:
                await run_in_threadpool(os.remove, path)
                ext = os.path.splitext(path)[1].lower()
                if ext in registry.verify_extensions():
                    await verification_store.clear(path)
                if ext == ".chd":
                    await chd_metadata_store.clear(path)

            return {"path": path, "success": True}
        except OSError as e:
            logger.warning("Delete failed for %s: %s", path, e)
            return {"path": path, "success": False, "error": "File operation failed"}

    # Process all files
    for path in payload.paths:
        result = await delete_single_file(path)
        results.append(result)
        if result["success"]:
            success_count += 1
        else:
            failed_count += 1

    return {
        "total": len(payload.paths),
        "success": success_count,
        "failed": failed_count,
        "results": results,
    }
