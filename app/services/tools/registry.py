"""In-process registry of first-party conversion tools.

Dispatch sites (job_manager, convert/files/info routes) ask the registry for
the tool that handles a mode / input / verify target instead of branching on
tool identity. No dynamic or third-party plugin discovery: tools are registered
explicitly in ``__init__.py``.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from logging_setup import get_logger
from models import InputKind
from utils.path_utils import match_extension

if TYPE_CHECKING:
    from .base import ToolPlugin
    from .spec import ModeSpec


logger = get_logger("tools.registry")

# How long a tool gets to answer "can I run here?" before it is treated as
# unavailable. Generous, because the honest answer for NSZ is a recursive walk
# of every configured volume looking for `prod.keys`, which on a healthy but
# large SMB share is not instant. Still a bound: see `tool_is_ready`.
READY_PROBE_SECONDS = 20.0


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

    async def tool_is_ready(self, tool: ToolPlugin) -> bool:
        """Can this tool run here — without hanging the caller if a mount cannot say?

        ``is_ready()`` is a coroutine, which makes it look safe, and it is not.
        NSZ's delegates to a pooled ``keys_available()`` that walks every
        configured volume looking for ``prod.keys``, and that walk has no
        deadline of its own: on an NFS/SMB/rclone mount that has stopped
        answering it blocks in uninterruptible I/O, holding a shared-pool
        worker, and every caller waits behind it forever.

        The callers are all user-facing and all fan out over the whole
        registry, so the cost is the whole screen: ``GET /api/tools`` (the
        sidebar's tool list) awaits each tool in turn, and
        ``GET /api/romm/platforms`` gathers them, so the Library and the
        automation editor never finish loading. Repeat the request and each
        attempt strands another worker.

        Expiring answers **not ready**, which is the same answer a missing
        binary gets and the right one here: a tool whose prerequisites cannot
        be read cannot convert. The blocked thread is not freed — Python can
        abandon the awaiter, never the OS thread — but the caller is, which is
        the part a person is waiting on.

        One seam rather than a bound per call site, so a future caller of
        ``is_ready()`` is covered by using the registry the way every other
        dispatch already does.
        """
        try:
            return bool(await asyncio.wait_for(tool.is_ready(), READY_PROBE_SECONDS))
        except (asyncio.TimeoutError, OSError):
            logger.warning(
                "readiness check for %s did not finish in %ss; treating it as "
                "not ready (a volume is not answering)",
                getattr(tool, "id", tool), READY_PROBE_SECONDS,
            )
            return False

    async def ready_tool_ids(self) -> list[str]:
        """The ids of every registered tool that can actually run here.

        Concurrent rather than sequential: these are independent probes, and
        run in turn they add up — one slow volume delayed every tool behind it
        even when nothing was wrong with them.
        """
        tools = self.all()
        ready = await asyncio.gather(*(self.tool_is_ready(t) for t in tools))
        return [tool.id for tool, ok in zip(tools, ready, strict=True) if ok]

    def mode_supports_verify(self, mode: str) -> bool:
        """Whether this mode's output can be verified after conversion.

        Wider than :attr:`ModeSpec.supports_delete_on_verify`, which answers
        "may this job delete its source once the check passes". A mode can be
        verifiable and still never be allowed to delete -- makeps3iso's
        folder->iso is the case -- so asking the delete flag put the operator's
        "verify each converted file" out of reach for it.
        """
        spec = self.spec(mode)
        return spec.supports_delete_on_verify or spec.supports_verify

    def mode_is_automatable(self, mode: str) -> bool:
        """May an unattended rule run this mode repeatedly over a library?

        About *termination*, not capability: a mode excluded here still works
        perfectly by hand, and this deliberately does not encode the editor's
        separate choice not to offer unpacking (extract modes are accepted in
        a rule and always have been -- they terminate).

        A mode that turns its own product into a *differently named* file does
        not terminate. CHDMAN's `copy` is the case: a rule over `Game.chd`
        writes `Game_copy.chd`, RomM rescans and lists it, the same rule
        accepts it as a source, and the next sweep writes
        `Game_copy_copy.chd` -- once per sweep, full size each time, until the
        volume fills. Nothing downstream stops it, because the destination is
        new every round: the duplicate policy and the converted history both
        agree it is work that has not been done.

        A mode that maps its own product back onto *itself* is fine --
        `dolphin_rvz` over a `.rvz` resolves to the same path, which the
        queue's same-path guard refuses, so it settles instead of multiplying.
        The question is therefore not "does it accept its own output" but
        "where would it write it", which the tool answers.

        Derived rather than listed, so a future tool with the same shape is
        excluded without anyone remembering to add it.
        """
        spec = self.spec(mode)
        output_ext = (spec.output_ext or "").lower()
        if not output_ext:
            return True
        if output_ext not in {ext.lower() for ext in spec.input_extensions}:
            return True  # cannot re-consume what it makes
        probe = f"/romm-automation-probe{output_ext}"
        try:
            return self.for_mode(mode).output_path(mode, probe) == probe
        except (KeyError, ValueError, OSError):
            return False

    def mode_input_kind(self, mode: str) -> InputKind:
        """The kind of thing this mode takes: one file, or one directory.

        The single answer to "what is a source here", asked by the queue (which
        carries it end-to-end so the pipeline skips the file-only assumptions),
        by the automation sweep's declaration check, and by the sweep's
        existence check -- which has to confirm what is on disk is that kind,
        not merely that something is there.
        """
        return (
            InputKind.DIRECTORY
            if InputKind.DIRECTORY in self.spec(mode).input_kinds
            else InputKind.FILE
        )

    def default_compression(self, mode: str) -> str | None:
        """The codec to send when a level is set and the codec is "tool default".

        Only for modes that offer a codec choice. Where a mode declares
        ``supports_compression=False`` its dropdown is not a codec list at all
        (nsz picks a solid/block layout) and the empty part of ``":18"`` is
        meaningful to the tool -- naming a codec there would override a working
        choice, so this answers None and the caller leaves the string as it is.
        """
        if not self.spec(mode).supports_compression:
            return None
        return self.for_mode(mode).default_compression

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

    def mode_platform_slugs(self, mode: str) -> frozenset[str]:
        """Platforms this mode serves: its own if declared, else its tool's.

        Most modes inherit -- chdman's modes are as PS2/PSX/Dreamcast as chdman
        is. A composite mode has to declare, because its tool is a shell that
        belongs to no system.
        """
        spec = self.spec(mode)
        return spec.platform_slugs or self.for_mode(mode).platform_slugs

    def mode_allows_platform(self, mode: str, slug: str | None) -> bool:
        """Whether *mode* is applicable to *slug*, on the same terms as
        :meth:`narrow_to_platform`: conservative, so an unknown slug or a mode
        with no opinion is always kept."""
        if not slug:
            return True
        key = slug.strip().lower()
        if not key:
            return True
        claimed = self.mode_platform_slugs(mode)
        if not claimed:
            return True
        # Same guard as narrow_to_platform: a slug nothing claims must not
        # delete every opinionated mode.
        if not any(
            key in self.mode_platform_slugs(m.mode) for m in self.mode_specs()
        ):
            return True
        return key in claimed

    def modes_for_platform(self, slug: str | None) -> list[str]:
        """Every registered mode applicable to *slug*, narrowed per mode.

        The mode-level companion to :meth:`narrow_to_platform`. Narrowing by
        tool alone cannot separate a composite tool's modes: the chain tool
        legitimately survives on both PS2 and GameCube, because it has a mode
        for each -- but offering *both* modes on *both* platforms is how a
        GameCube disc ends up with a rule targeting a PS2 format.
        """
        return [
            m.mode for m in self.mode_specs()
            if self.mode_allows_platform(m.mode, slug)
        ]

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
