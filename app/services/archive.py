import os
import shutil
import tempfile
import zipfile
import time
import logging
from logging_setup import get_logger
from pathlib import Path, PurePosixPath
from typing import List, Tuple, Optional, Union, Dict

from config import settings
from services.archive_members import read_archive_members
from utils.junk import is_junk_path
from utils.path_utils import match_extension

try:
    import py7zr

    HAS_7Z = True
except ImportError:
    HAS_7Z = False

try:
    import rarfile

    HAS_RAR = True
except ImportError:
    HAS_RAR = False


ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar"}
# The listable / convert-gate extension sets come *only* from the tool registry
# (``_listable_extensions`` browse, ``_convert_gate_extensions`` search). There is
# deliberately no hand-maintained fallback copy: a stale literal silently hid new
# tools' members (3DS issue #113, romz's .zip "Empty folder" bug), so the registry
# is the single source of truth and an empty union surfaces a real bug loudly
# instead of being masked.

logger = get_logger("archive")


class ArchiveService:
    """Service for handling compressed archives."""

    def __init__(self):
        self._temp_dirs: dict = {}

    @staticmethod
    def _archive_limits() -> Tuple[int, int, int]:
        max_entries = max(0, int(getattr(settings, "archive_max_entries", 0) or 0))
        max_member_size = max(
            0, int(getattr(settings, "archive_max_member_size", 0) or 0)
        )
        max_total_size = max(
            0, int(getattr(settings, "archive_max_total_size", 0) or 0)
        )
        return max_entries, max_member_size, max_total_size

    @staticmethod
    def _format_size(size: int) -> str:
        return f"{size} bytes"

    @staticmethod
    def _coerce_size(value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        try:
            size = int(value)
        except (TypeError, ValueError):
            return None
        if size < 0:
            return None
        return size

    def _check_member_size(self, size: int, *, member: str) -> None:
        _, max_member_size, _ = self._archive_limits()
        if max_member_size > 0 and size > max_member_size:
            raise ValueError(
                f"Archive member exceeds max size: {member} ({self._format_size(size)})"
            )

    def _check_total_size(self, total_size: int) -> None:
        _, _, max_total_size = self._archive_limits()
        if max_total_size > 0 and total_size > max_total_size:
            raise ValueError(
                f"Archive extraction exceeds max total size ({self._format_size(total_size)})"
            )

    def _size_limits_enabled(self) -> bool:
        _, max_member_size, max_total_size = self._archive_limits()
        return max_member_size > 0 or max_total_size > 0

    def enforce_archive_limits(self, members: list[Tuple[str, int]]) -> None:
        """Apply the configured archive entry/size limits to a member listing.

        Shared with romz, which reads archives via its own member listing and
        runs ``7z`` directly rather than going through this service's extract
        path. Without this, deployments that set ``CHD_ARCHIVE_MAX_ENTRIES`` /
        ``CHD_ARCHIVE_MAX_MEMBER_SIZE`` / ``CHD_ARCHIVE_MAX_TOTAL_SIZE`` to guard
        against oversized archives / zip bombs would not have those limits
        applied to romz extract or verify. ``members`` is ``(name, size)`` with
        uncompressed sizes.
        """
        max_entries, _, _ = self._archive_limits()
        if max_entries > 0 and len(members) > max_entries:
            raise ValueError(f"Archive exceeds max entries ({max_entries})")
        total = 0
        for name, size in members:
            self._check_member_size(size, member=name)
            total += size
        self._check_total_size(total)

    def _create_temp_dir(self) -> str:
        base_dir = settings.temp_dir
        if base_dir is None:
            base_dir = str(Path(settings.data_dir) / "temp")
        if base_dir:
            try:
                Path(base_dir).mkdir(parents=True, exist_ok=True)
                return tempfile.mkdtemp(prefix="chd_extract_", dir=base_dir)
            except OSError as exc:
                logger.warning("Failed to create temp dir %s: %s", base_dir, exc)
        return tempfile.mkdtemp(prefix="chd_extract_")

    def is_archive(self, filename: str) -> bool:
        """Check if a file is a supported archive."""
        ext = Path(filename).suffix.lower()
        return ext in ARCHIVE_EXTENSIONS

    @staticmethod
    def _listable_extensions() -> frozenset:
        """Extensions surfaced when *browsing* the members of an archive.

        Global, scoped to known extensions: every source extension any tool
        recognizes (``registry.convertible_extensions()``) plus anything a mode
        can accept straight from an archive (``archive_input_extensions()`` —
        which adds ``.chd``, an output chdman disowns as a loose source but
        decompresses from inside an archive), minus the archive container
        extensions themselves (no point listing a ``.zip`` inside a ``.zip``).
        This is a superset of the convert-gate set, so it also shows members
        that are visible-only — a romz ROM appears when you browse into its
        archive even though no mode will re-convert it in place (its
        ``convertible_by`` stays empty, see ``tools_accepting_archive_member``).
        The registry is the single source of truth (it always loads in-app), so
        an empty union surfaces a registry bug loudly instead of being masked by
        a stale hardcoded fallback.
        """
        from services.tools import registry

        exts = (
            frozenset(registry.convertible_extensions())
            | frozenset(registry.archive_input_extensions())
        ) - ARCHIVE_EXTENSIONS
        if not exts:
            # An empty union means the registry is unloaded/broken, not that
            # nothing is convertible. Fail loudly so the archive listing surfaces
            # the bug rather than silently rendering an "empty archive".
            raise RuntimeError(
                "Tool registry produced no listable extensions — registry not loaded?"
            )
        return exts

    @staticmethod
    def _convert_gate_extensions() -> frozenset:
        """Extensions a member must have to be a real archive-conversion input.

        The narrower convert-gate subset (``registry.archive_input_extensions``):
        only members some mode can actually accept from an archive. Used by the
        recursive *search* path, which surfaces convertible hits — not the wider
        browse listing — so list-only members (romz ROMs) never count toward the
        per-archive entry cap before a genuine convertible member is reached.
        """
        from services.tools import registry

        exts = frozenset(registry.archive_input_extensions())
        if not exts:
            raise RuntimeError(
                "Tool registry produced no archive-input extensions — registry not loaded?"
            )
        return exts

    def list_archive_contents(
        self, archive_path: str, *, include_meta: bool = False,
        convertible_only: bool = False,
    ) -> Union[List[dict], Dict[str, Union[List[dict], bool]]]:
        """List the files inside an archive.

        Members are read through the shared, mtime-cached
        :func:`services.archive_members.read_archive_members`, so an archive is
        opened at most once per ``(path, mtime, size)`` no matter how many times
        the browser/summary path lists it (and the romz gate shares that same
        cached read). The extension/limit filtering and ``output_stem`` shaping
        are applied per call against the cached raw members, returning fresh dicts
        the archive route is free to annotate in place.

        ``convertible_only`` narrows the extension gate from the browse listing
        (every known source, :meth:`_listable_extensions`) to the convert-gate
        subset (:meth:`_convert_gate_extensions`). The recursive search path sets
        it so list-only members never consume the entry cap ahead of a genuine
        convertible member.
        """
        ext = Path(archive_path).suffix.lower()
        entries: List[dict] = []
        truncated = False
        allowed = (
            self._convert_gate_extensions()
            if convertible_only
            else self._listable_extensions()
        )
        if ext in ARCHIVE_EXTENSIONS:
            start = time.monotonic()
            try:
                raw_members = read_archive_members(archive_path)
                entries, truncated = self._filter_members(
                    archive_path, raw_members, allowed,
                )
            except Exception as e:
                logger.exception("Error listing archive %s: %s", archive_path, e)
            finally:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Listed archive %s entries=%d in %.2fs",
                        archive_path,
                        len(entries),
                        time.monotonic() - start,
                    )

        entries = self._filter_preferred_entries(entries)

        for entry in entries:
            entry["output_stem"] = self._output_stem_for_member(entry["internal_path"])

        if include_meta:
            return {"entries": entries, "truncated": truncated}
        return entries

    def _filter_members(
        self, archive_path: str, members: list, allowed: frozenset,
    ) -> Tuple[List[dict], bool]:
        """Filter raw archive members to entries within size limits.

        Format-agnostic replacement for the old per-format ``_list_zip`` /
        ``_list_7z`` / ``_list_rar`` triplet: every container now yields a
        uniform :class:`~services.archive_members.ArchiveMember` list, so a single
        pass applies the junk filter, member-path validation, the caller's
        ``allowed`` extension gate, and the configured entry/size limits. ``size``
        of ``None`` means the container didn't record an uncompressed size; that's
        a skip-and-truncate when any size limit is active, otherwise treated as 0.
        The entry cap counts only members that pass ``allowed``, so a caller
        gating on the convert-gate subset never burns the cap on list-only rows.
        """
        entries: List[dict] = []
        max_entries, max_member_size, max_total_size = self._archive_limits()
        convertible = allowed
        total_size = 0
        truncated = False
        for member in members:
            if member.is_dir:
                continue
            if is_junk_path(member.name):
                continue
            try:
                self._validate_member(member.name)
            except ValueError:
                continue
            # Suffix match, not Path.suffix equality, so a member declared with
            # a compound extension (nkit2iso's .nkit.iso) passes the gate. The
            # recorded ``extension`` stays the plain trailing suffix: it feeds
            # the synthetic-sibling path and the icon buckets, both of which key
            # on the generic tail, while convertibility is re-derived from the
            # member name via ``tools_accepting_archive_member``.
            if match_extension(member.name, convertible) is None:
                continue
            ext = Path(member.name).suffix.lower()
            size = member.size
            if size is None:
                if max_member_size > 0 or max_total_size > 0:
                    logger.warning(
                        "Skipping archive member %s (unknown size)", member.name,
                    )
                    truncated = True
                    continue
                size = 0
            if max_member_size > 0 and size > max_member_size:
                logger.warning(
                    "Skipping oversized archive member %s (%s)",
                    member.name,
                    self._format_size(size),
                )
                truncated = True
                continue
            if max_total_size > 0 and (total_size + size) > max_total_size:
                logger.warning(
                    "Archive %s hit max total size limit (%s)",
                    archive_path,
                    self._format_size(max_total_size),
                )
                truncated = True
                break
            total_size += size
            entries.append(
                {
                    "archive_path": archive_path,
                    "internal_path": member.name,
                    "name": os.path.basename(member.name),
                    "size": size,
                    "extension": ext,
                    # No "convertible" flag here: a member can be *listable*
                    # (e.g. a romz ROM, surfaced for visibility) without being a
                    # valid archive-conversion input. Convertibility is decided
                    # per consumer from the registry (route layer derives
                    # ``convertible_by`` via ``tools_accepting_archive_member``),
                    # so stamping a blanket True here would misreport list-only
                    # members to any direct caller of this service.
                }
            )
            if max_entries > 0 and len(entries) >= max_entries:
                logger.warning(
                    "Archive %s hit max entry limit (%s)",
                    archive_path,
                    max_entries,
                )
                truncated = True
                break
        return entries, truncated

    @staticmethod
    def _filter_preferred_entries(entries: List[dict]) -> List[dict]:
        if not entries:
            return entries

        exts_by_parent = {}
        for entry in entries:
            parent = PurePosixPath(entry["internal_path"]).parent
            exts_by_parent.setdefault(parent, set()).add(entry.get("extension"))

        filtered = []
        for entry in entries:
            ext = entry.get("extension")
            parent = PurePosixPath(entry["internal_path"]).parent
            exts = exts_by_parent.get(parent, set())
            if ext == ".bin" and (".cue" in exts or ".gdi" in exts):
                continue
            filtered.append(entry)

        return filtered

    def extract_file(self, archive_path: str, internal_path: str) -> Tuple[str, str]:
        """Extract a specific file from an archive into a temp directory."""
        ext = Path(archive_path).suffix.lower()
        temp_dir = self._create_temp_dir()

        try:
            destination = self._prepare_destination(temp_dir, internal_path)
            size = self._coerce_size(self._get_member_size(archive_path, internal_path))
            if size is None:
                if self._size_limits_enabled():
                    raise ValueError(
                        "Archive member size unknown; cannot enforce limits"
                    )
            else:
                self._check_member_size(size, member=internal_path)
                self._check_total_size(size)

            if ext == ".zip":
                extracted = self._extract_from_zip(
                    archive_path, internal_path, destination
                )
            elif ext == ".7z" and HAS_7Z:
                extracted = self._extract_from_7z(
                    archive_path, internal_path, destination
                )
            elif ext == ".rar" and HAS_RAR:
                extracted = self._extract_from_rar(
                    archive_path, internal_path, destination
                )
            else:
                raise ValueError(f"Unsupported archive format: {ext}")

            return extracted, temp_dir

        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    def _validate_member(self, member: str) -> List[str]:
        """Ensure an archive member path cannot escape the extraction directory."""
        if "\\" in member:
            raise ValueError("Backslashes are not allowed in archive members")
        normalized = member.strip()
        if normalized.startswith("/"):
            raise ValueError("Absolute paths are not allowed inside archives")
        segments = normalized.split("/")
        if not segments or any(part in ("", ".", "..") for part in segments):
            raise ValueError("Invalid archive member path")
        if any(":" in part for part in segments):
            raise ValueError("Drive letters are not allowed inside archives")
        return segments

    def _prepare_destination(self, temp_dir: str, member: str) -> str:
        segments = self._validate_member(member)
        base = Path(temp_dir).resolve()
        candidate = base.joinpath(*segments).resolve()
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise ValueError("Archive member resolves outside temp directory") from exc
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return str(candidate)

    def _extract_from_zip(
        self, archive_path: str, internal_path: str, destination: str
    ) -> str:
        with zipfile.ZipFile(archive_path, "r") as zf:
            try:
                with zf.open(internal_path) as src, open(destination, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            except KeyError as exc:
                raise FileNotFoundError(
                    f"{internal_path} not found in archive"
                ) from exc
        return destination

    def extract_related_files(
        self, archive_path: str, internal_path: str, temp_dir: str
    ):
        """Extract sibling files for multi-file formats like .cue or .gdi."""
        entry_ext = Path(internal_path).suffix.lower()
        if entry_ext not in {".cue", ".gdi"}:
            return

        parent = PurePosixPath(internal_path).parent
        if str(parent) == ".":
            parent = PurePosixPath("")

        initial_total_size = 0
        primary_size = self._coerce_size(
            self._get_member_size(archive_path, internal_path)
        )
        if primary_size is None:
            if self._size_limits_enabled():
                raise ValueError("Archive member size unknown; cannot enforce limits")
        else:
            initial_total_size = primary_size
            self._check_total_size(initial_total_size)

        archive_ext = Path(archive_path).suffix.lower()
        if archive_ext == ".zip":
            self._extract_related_from_zip(
                archive_path,
                parent,
                temp_dir,
                initial_total_size,
                primary_member=internal_path,
            )
        elif archive_ext == ".7z" and HAS_7Z:
            self._extract_related_from_7z(
                archive_path,
                parent,
                temp_dir,
                initial_total_size,
                primary_member=internal_path,
            )
        elif archive_ext == ".rar" and HAS_RAR:
            self._extract_related_from_rar(
                archive_path,
                parent,
                temp_dir,
                initial_total_size,
                primary_member=internal_path,
            )
        else:
            raise ValueError(f"Unsupported archive format: {archive_ext}")

    def _get_member_size(self, archive_path: str, internal_path: str) -> Optional[int]:
        # Reuse the shared, cached member read instead of re-opening the archive
        # for a single size lookup. Returns None when the member is absent or the
        # container didn't record an uncompressed size (mirrors the prior 7z
        # behaviour, which returned ``entry.uncompressed`` unmodified).
        #
        # For a ZIP with duplicate member names, ``zipfile.open(name)`` extracts
        # the *last* such entry (NameToInfo maps a name to its final occurrence),
        # so the size guard must read the last match too — otherwise a small
        # leading duplicate could let the limit check pass while a larger trailing
        # payload is the one actually extracted.
        size: Optional[int] = None
        try:
            for member in read_archive_members(archive_path):
                if member.name == internal_path:
                    size = member.size
        except Exception:
            return None
        return size

    def _extract_related_from_zip(
        self,
        archive_path: str,
        parent: PurePosixPath,
        temp_dir: str,
        initial_total_size: int = 0,
        *,
        primary_member: Optional[str] = None,
    ):
        total_size = max(0, int(initial_total_size))
        with zipfile.ZipFile(archive_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if is_junk_path(info.filename):
                    continue
                try:
                    self._validate_member(info.filename)
                except ValueError:
                    continue
                if not self._is_same_parent(info.filename, parent):
                    continue
                if primary_member and info.filename == primary_member:
                    continue
                self._check_member_size(info.file_size, member=info.filename)
                total_size += info.file_size
                self._check_total_size(total_size)
                destination = self._prepare_destination(temp_dir, info.filename)
                with zf.open(info.filename) as src, open(destination, "wb") as dst:
                    shutil.copyfileobj(src, dst)

    def _extract_related_from_7z(
        self,
        archive_path: str,
        parent: PurePosixPath,
        temp_dir: str,
        initial_total_size: int = 0,
        *,
        primary_member: Optional[str] = None,
    ):
        with py7zr.SevenZipFile(archive_path, "r") as zf:  # type: ignore[name-defined]
            targets = []
            total_size = max(0, int(initial_total_size))
            _, max_member_size, max_total_size = self._archive_limits()
            for entry in zf.list():
                if entry.is_directory:
                    continue
                name = entry.filename
                if is_junk_path(name):
                    continue
                try:
                    self._validate_member(name)
                except ValueError:
                    continue
                if self._is_same_parent(name, parent):
                    if primary_member and name == primary_member:
                        continue
                    size = self._coerce_size(entry.uncompressed)
                    if size is None:
                        if max_member_size > 0 or max_total_size > 0:
                            raise ValueError(
                                "Archive member size unknown; cannot enforce limits"
                            )
                        size = 0
                    self._check_member_size(size, member=name)
                    total_size += size
                    self._check_total_size(total_size)
                    targets.append(name)
            if targets:
                zf.extract(targets=targets, path=temp_dir)

    def _extract_related_from_rar(
        self,
        archive_path: str,
        parent: PurePosixPath,
        temp_dir: str,
        initial_total_size: int = 0,
        *,
        primary_member: Optional[str] = None,
    ):
        total_size = max(0, int(initial_total_size))
        with rarfile.RarFile(archive_path, "r") as rf:  # type: ignore[name-defined]
            for info in rf.infolist():
                if info.is_dir():
                    continue
                if is_junk_path(info.filename):
                    continue
                try:
                    self._validate_member(info.filename)
                except ValueError:
                    continue
                if not self._is_same_parent(info.filename, parent):
                    continue
                if primary_member and info.filename == primary_member:
                    continue
                self._check_member_size(info.file_size, member=info.filename)
                total_size += info.file_size
                self._check_total_size(total_size)
                destination = self._prepare_destination(temp_dir, info.filename)
                with rf.open(info.filename) as src, open(destination, "wb") as dst:
                    shutil.copyfileobj(src, dst)

    @staticmethod
    def _is_same_parent(member: str, parent: PurePosixPath) -> bool:
        member_parent = PurePosixPath(member).parent
        if str(parent) == "":
            return str(member_parent) in ("", ".")
        return member_parent == parent

    @staticmethod
    def _output_stem_for_member(member: str) -> str:
        member_path = PurePosixPath(member)
        parent = member_path.parent
        stem = member_path.stem
        if str(parent) in ("", "."):
            return stem
        safe_parent = "_".join([p for p in parent.parts if p not in ("", ".")])
        return f"{safe_parent}_{stem}"

    @staticmethod
    def _output_name_for_member(member: str) -> str:
        """Flattened *filename* (subdirectories collapsed) that preserves the
        member's original extension.

        Tools whose output extension is derived from the input, z3ds maps
        ``.3ds`` -> ``.z3ds``, ``.cci`` -> ``.zcci``, need the original
        extension to pick the right output name. ``_output_stem_for_member``
        drops it (it exists only for chd existing-output detection, which is
        always ``<stem>.chd``), so archive conversions route the output path
        through this helper instead. Each tool's ``get_output_path_for_mode``
        treats the result as a normal filename when ``treat_as_stem=True``.
        """
        stem = ArchiveService._output_stem_for_member(member)
        suffix = PurePosixPath(member).suffix
        return f"{stem}{suffix}"

    def _extract_from_7z(
        self, archive_path: str, internal_path: str, destination: str
    ) -> str:
        with py7zr.SevenZipFile(archive_path, "r") as zf:  # type: ignore[name-defined]
            try:
                parts = PurePosixPath(internal_path).parts
                if not parts:
                    raise FileNotFoundError(f"{internal_path} not found in archive")
                base_dir = Path(destination).parents[len(parts) - 1]
                zf.extract(targets=[internal_path], path=str(base_dir))
            except Exception as exc:
                raise FileNotFoundError(
                    f"{internal_path} not found in archive"
                ) from exc

        if not os.path.isfile(destination):
            raise FileNotFoundError(f"{internal_path} not found in archive")
        return destination

    def _extract_from_rar(
        self, archive_path: str, internal_path: str, destination: str
    ) -> str:
        with rarfile.RarFile(archive_path, "r") as rf:  # type: ignore[name-defined]
            try:
                with rf.open(internal_path) as src, open(destination, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            except (rarfile.Error, KeyError) as exc:  # type: ignore[name-defined]
                raise FileNotFoundError(
                    f"{internal_path} not found in archive"
                ) from exc
        return destination

    def cleanup_temp_dir(self, temp_dir: str):
        """Clean up a temporary extraction directory."""
        shutil.rmtree(temp_dir, ignore_errors=True)


archive_service = ArchiveService()
