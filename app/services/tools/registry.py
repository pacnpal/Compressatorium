"""In-process registry of first-party conversion tools.

Dispatch sites (job_manager, convert/files/info routes) ask the registry for
the tool that handles a mode / input / verify target instead of branching on
tool identity. No dynamic or third-party plugin discovery: tools are registered
explicitly in ``__init__.py``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from utils.path_utils import match_extension

if TYPE_CHECKING:
    from .base import ToolPlugin
    from .spec import ModeSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolPlugin] = {}
        self._by_mode: dict[str, ToolPlugin] = {}

    def register(self, tool: ToolPlugin) -> None:
        # Validate fully before mutating so a bad tool can't leave the registry
        # in a partially-registered state (future phases iterate registry.all()).
        if tool.id in self._tools:
            raise ValueError(f"duplicate tool id {tool.id}")
        seen: set[str] = set()
        for m in tool.modes:
            if m.tool_id != tool.id:
                raise ValueError(
                    f"mode {m.mode} declares tool_id {m.tool_id!r} "
                    f"but belongs to tool {tool.id!r}"
                )
            if m.mode in self._by_mode or m.mode in seen:
                raise ValueError(f"duplicate mode {m.mode}")
            seen.add(m.mode)
        self._tools[tool.id] = tool
        for m in tool.modes:
            self._by_mode[m.mode] = tool

    def all(self) -> list[ToolPlugin]:
        return list(self._tools.values())

    def get(self, tool_id: str) -> ToolPlugin:
        return self._tools[tool_id]

    def for_mode(self, mode: str) -> ToolPlugin:
        return self._by_mode[mode]

    def spec(self, mode: str) -> ModeSpec:
        return self.for_mode(mode).spec(mode)

    def mode_specs(self) -> list[ModeSpec]:
        return [m for t in self._tools.values() for m in t.modes]

    def convertible_extensions(self) -> tuple[str, ...]:
        # Sorted tuple, not a frozenset: hash-seeded set iteration order varies
        # across processes, so any consumer that serializes this into an ordered
        # list or filter would churn run-to-run (issue #183).
        return tuple(sorted(
            set().union(*(t.input_extensions for t in self._tools.values()))
        ))

    def archive_input_extensions(self) -> tuple[str, ...]:
        """Input extensions accepted by at least one mode that allows
        archive members as input.

        Used by the archive listing to decide which members inside a
        ``.zip`` / ``.7z`` / ``.rar`` are worth surfacing. Driving this off
        the same mode specs that gate ``plan_job`` keeps the listing and the
        conversion path in lockstep: a member is only listed as convertible
        when some mode could actually accept it from an archive. Previously
        the listing hard-coded CHDMAN's source set, so 3DS members were
        silently hidden even though z3ds could compress them (issue #113).
        """
        exts: set[str] = set()
        for tool in self._tools.values():
            for mode in tool.modes:
                if mode.allows_archive_input:
                    exts.update(mode.input_extensions)
        return tuple(sorted(exts))  # sorted for cross-run determinism (issue #183)

    def tools_accepting_archive_member(self, member: str) -> list[str]:
        """Tool ids with an ``allows_archive_input`` mode accepting ``member``.

        The archive-member analogue of :meth:`tools_for_input`: a tool is only
        convertible-in-place when some mode of it both takes the member's
        extension AND allows archive input. A listing-only member (a romz ROM,
        surfaced because it's a known source extension) has no
        ``allows_archive_input`` mode, so it is correctly excluded — visible,
        but no in-place conversion affordance.

        ``member`` may be the member's path/name or just its extension: the
        shared :func:`~utils.path_utils.match_extension` suffix match handles
        both, so a compound-extension source (``.nkit.iso``) resolves whether
        the caller kept the full name or only the generic ``.iso`` tail the
        listing recorded.
        """
        return [
            tool.id
            for tool in self._tools.values()
            if any(
                mode.allows_archive_input
                and match_extension(member, mode.input_extensions) is not None
                for mode in tool.modes
            )
        ]

    def tools_for_input(self, filename: str) -> list[ToolPlugin]:
        return [
            t for t in self._tools.values()
            if match_extension(filename, t.input_extensions) is not None
        ]

    def narrow_to_platform(self, tool_ids: list[str], slug: str | None) -> list[str]:
        """Drop tool ids that a library manager's platform rules out.

        Extension matching decides *convertibility*; this only removes
        candidates the platform contradicts — a GameCube ``.iso`` keeping
        dolphin/nkit while losing chdman and maxcso.

        Conservative by construction, because a wrong exclusion is worse than a
        missing one: with no slug, an unrecognised slug, or a slug no tool
        claims, the list is returned untouched, and a tool that declares no
        ``platform_slugs`` is never dropped. So a RomM platform we have never
        heard of degrades to today's extension-only behaviour instead of an
        empty list.
        """
        if not slug:
            return tool_ids
        key = slug.strip().lower()
        if not key:
            return tool_ids
        # Only tools that actually claim a platform can be narrowed away, and
        # only when some tool claims THIS one -- otherwise an unknown slug
        # would silently delete every opinionated tool.
        if not any(key in t.platform_slugs for t in self._tools.values()):
            return tool_ids
        kept = []
        for tool_id in tool_ids:
            tool = self._tools.get(tool_id)
            if tool is None or not tool.platform_slugs or key in tool.platform_slugs:
                kept.append(tool_id)
        return kept

    def tools_for_directory(self, path: str) -> list[ToolPlugin]:
        """Tools that accept ``path`` (a directory) as their input unit.

        The directory analogue of :meth:`tools_for_input`. Each tool decides
        via ``accepts_directory`` (default ``False``), which runs the tool's
        source-layout detector. May do disk I/O — call off the event loop.
        """
        return [t for t in self._tools.values() if t.accepts_directory(path)]

    def tools_converting_path(self, path: str) -> list[ToolPlugin]:
        """Tools that can convert a concrete file path on its own.

        The per-file companion to :meth:`tools_for_input`: each tool refines
        the plain extension match via ``converts_path``. jwud uses it to keep
        the secondary members of a split Wii U dump (``game_part2.wud`` …)
        visible but non-convertible — only ``game_part1.wud`` drives the set.
        May do disk I/O; call it off the event loop.
        """
        return [t for t in self._tools.values() if t.converts_path(path)]

    def source_companions(self, path: str) -> list[str]:
        """Every sibling file the tools consuming ``path`` also read.

        Union across tools (deduped, registration order), never including
        ``path`` itself. The delete-on-verify plan adds these so removing a
        multi-file source takes the whole set. Empty for ordinary single-file
        inputs. May do disk I/O; call it off the event loop.
        """
        seen: list[str] = []
        for tool in self._tools.values():
            for companion in tool.source_companions(path):
                if companion != path and companion not in seen:
                    seen.append(companion)
        return seen

    def tool_for_verify(self, path: str) -> ToolPlugin | None:
        return next(
            (
                t for t in self._tools.values()
                if match_extension(path, t.verify_extensions) is not None
            ),
            None,
        )

    def tools_verifying_path(self, path: str) -> list[ToolPlugin]:
        """Tools whose verify/info applies to a concrete file path.

        The per-file companion to :meth:`tool_for_verify`: each tool refines
        the plain extension match via ``verifies_path`` (romz inspects archive
        members so a non-single-ROM ``.7z``/``.zip`` is excluded). Returns
        every claiming tool, in registration order, so the file listing can
        surface a tool-neutral ``verifiable_by`` flag the frontend gates the
        Verify/Info row-actions on. May do disk I/O (archive inspection); call
        it off the event loop.
        """
        return [t for t in self._tools.values() if t.verifies_path(path)]

    def verify_extensions(self) -> tuple[str, ...]:
        """Union of every registered tool's verify_extensions.

        Used by file rename/delete handlers to decide whether the path
        carries a verification record worth clearing, historically the
        check hard-coded `.chd`, which left .rvz / .z3ds / etc. records
        orphaned in the persistent store when the file was removed.
        """
        return tuple(sorted(
            set().union(*(t.verify_extensions for t in self._tools.values()))
        ))

    def output_extensions(self) -> tuple[str, ...]:
        """Union of every registered tool's output_extensions.

        These are the file types the tools *produce* (.chd, .rvz, .nsz, ...).
        """
        return tuple(sorted(
            set().union(*(t.output_extensions for t in self._tools.values()))
        ))

    def scannable_path(self, path: str) -> bool:
        """Whether the metadata scan should read this concrete path.

        The per-file companion to :meth:`scannable_extensions`, which selects
        the walk by type only. A single tool vetoing the path drops it: a file
        that some tool knows is not a standalone artifact cannot match a DAT
        whatever the other tools think. Runs once per candidate across the whole
        library, so implementations stay cheap (a stat at most); call it off the
        event loop with the rest of the scan walk.
        """
        return all(t.scannable_path(path) for t in self._tools.values())

    def scannable_extensions(self) -> tuple[str, ...]:
        """Extensions the library metadata scan should walk for.

        The union of produced outputs and verifiable inputs, so every
        registered tool's outputs are eligible for discovery rather than
        the historical hard-coded ``.chd`` walk (issue #131). Sorted tuple
        for determinism (issue #183); the two inputs are already sorted
        tuples, so re-union via sets before sorting.
        """
        return tuple(sorted(
            set(self.output_extensions()) | set(self.verify_extensions())
        ))
