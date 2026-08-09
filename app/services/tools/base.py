"""The plugin contract (``ToolPlugin``) and a minimal shared base.

Phase 0 keeps ``BaseTool`` deliberately small: it only hoists the
mode-lookup and derived-extension helpers that every tool needs. The shared
``SubprocessRunner`` orchestration and the ``verify()``-wraps-``verify_stream()``
default described in the design land in a later phase, once real logic is moved
out of the existing service singletons.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from models import OutputStatus
from utils.path_utils import match_extension

from .spec import ModeSpec


class EmbeddedHashUnavailable(Exception):
    """A tool could not derive its embedded content hash for a file.

    Raised by ``embedded_hashes`` when the tool *should* be able to report a
    content hash for this file type but the attempt failed transiently (e.g.
    ``dolphin-tool verify`` timed out / exited non-zero / the binary is
    missing). The DAT-match path treats this as a non-cacheable error rather
    than collapsing to a meaningless file-level hash and caching a false
    "unmatched" result. Tools that legitimately have no embedded hash for a
    file (so a file-level SHA1 fallback is correct) return ``[]`` instead.
    """


@runtime_checkable
class ToolPlugin(Protocol):
    id: str
    display_name: str
    binary_path: str
    modes: Sequence[ModeSpec]
    input_extensions: frozenset[str]    # convertible-from
    output_extensions: frozenset[str]   # produced ("output exists" badges + scan discovery)
    verify_extensions: frozenset[str]   # accepted by verify()
    # When the tool's embedded_hashes are *exhaustive*: a miss of every
    # reported content hash means the file definitively isn't in the DAT, so
    # the DAT matcher must NOT fall back to a file-level SHA1 of the container
    # (which can't be indexed anyway and would re-read the whole file). True
    # only for formats whose container bytes never appear in a DAT, e.g.
    # Dolphin's recompressed RVZ/WIA/GCZ. False (default) for formats whose own
    # file SHA1 may legitimately be indexed (e.g. a CHD-container DAT).
    embedded_hash_is_exhaustive: bool

    def spec(self, mode: str) -> ModeSpec:
        """Return the ModeSpec for a mode this tool owns."""

    def output_path(
        self,
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        """Resolve the output path for a conversion."""

    def detect_output(self, input_path: str) -> OutputStatus | None:
        """Detect an existing sibling output this tool could produce.

        Returns ``None`` when the tool cannot produce an output for this
        input or no output is present (neither finished nor mid-conversion).
        """

    def accepts_directory(self, path: str) -> bool:
        """Whether this tool can take ``path`` (a directory) as its input unit.

        The directory analogue of ``ext in input_extensions``. Default
        ``False``; a tool with an ``InputKind.DIRECTORY`` mode (makeps3iso
        folder->iso) overrides this to run its source-layout detector. May do
        disk I/O — call it off the event loop.
        """

    def verifies_path(self, path: str) -> bool:
        """Whether this tool's verify/info applies to a concrete file.

        The per-file refinement of ``verify_extensions``: the default is a
        plain extension match, but a tool that claims a broad container
        extension yet only handles a narrow subset (romz claims ``.7z``/
        ``.zip`` but only verifies single-ROM archives it produced) overrides
        this with per-file inspection. Listing code calls it so the frontend
        gates the Verify/Info row-actions on what the tool can actually
        handle, not on extension alone.
        """

    def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str,
        *,
        compression: str | None = None,
        split: bool = False,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Run a conversion, yielding ``{"progress", "message"}`` updates.

        ``split`` is honored only by tools whose mode declares it (makeps3iso
        folder->iso, ``-s`` 4 GB FAT32 split); other tools accept and ignore it,
        exactly as they do ``compression`` they don't use.
        """

    async def verify(self, path: str) -> dict:
        """Verify an output file; returns ``{"valid", "message"}``."""

    def verify_stream(self, path: str) -> AsyncGenerator[dict, None]:
        """Verify with streaming progress updates."""

    async def info(self, path: str) -> dict:
        """Return raw tool info for a file."""

    def info_model(self, raw: dict, path: str) -> BaseModel:
        """Map raw info into the typed API model."""

    async def embedded_hashes(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> list[tuple[str, str]]:
        """Report verifiable hashes embedded in / derivable from ``path``.

        Each entry is ``(sha1_hex, match_type)``: a content SHA1 the file
        carries (CHD header / data SHA1, Dolphin disc SHA1, ...) plus a
        label describing where it came from. The DAT-match fast path tries
        these against the imported DATs before falling back to a full
        file-level SHA1. Return an empty list when the tool has no cheap or
        format-meaningful hash to offer (file-level SHA1 fallback is correct).

        Raise :class:`EmbeddedHashUnavailable` when the tool *should* yield a
        hash for this file type but the attempt failed transiently, so the
        caller skips the (meaningless) file-level fallback and does not cache
        a false negative.

        ``cancel_event`` lets a background job (metadata scan / DAT match)
        abort an expensive derivation (e.g. ``dolphin-tool verify``) promptly
        instead of blocking until the current file finishes.
        """

    async def is_ready(self) -> bool:
        """Whether this tool's runtime prerequisites are met.

        A tool that cannot run at all in the current deployment — a missing
        user-supplied key file, an absent optional binary — reports ``False``
        so ``GET /api/tools`` lists it as unavailable and the frontend hides
        it entirely, rather than offering a converter that can only fail at
        job time. Default ``True``: most tools ship with everything they need.

        Async because a readiness probe may touch the disk (nsz searches the
        configured volumes for ``prod.keys``); implementations that block
        should hop to a threadpool rather than stall the event loop.
        """

    def active_pids(self) -> list[int]:
        """Return PIDs of in-flight subprocesses for this tool."""

    async def post_convert(
        self, input_path: str, output_path: str, mode: str,
    ) -> None:
        """Optional post-processing hook after a successful conversion."""

    def overwrite_targets(self, output_path: str, mode: str) -> list[str]:
        """Every path to clear before an **authorized** overwrite, primary first.

        The superset of ``companion_outputs``: it includes ``output_path``
        itself, and it enumerates artifacts a prior *failed* run may have left
        behind even when they can't coexist with a finished output (a
        makeps3iso ``-s`` build interrupted mid-split leaves both the
        not-yet-renamed base and some numbered parts). ``companion_outputs``
        describes a *successful* output set and is the wrong list to delete
        from; this one describes everything the overwrite must sweep.

        The job pipeline calls this instead of branching on tool identity, and
        rejects the whole overwrite if any entry exists as a non-file, so an
        override must list paths, never remove them.
        """

    def expected_output_size(self, input_path: str, mode: str) -> int | None:
        """Bytes this mode will write for ``input_path``, if cheaply knowable.

        Only for *preflight sizing* — the chain's disk-headroom check, which
        must hold source + full intermediate + partial final at once and cannot
        estimate the intermediate from a ratio when a container's shrink factor
        varies by an order of magnitude (a scrubbed Wii NKit, a highly
        compressed CSO). Return ``None`` when the answer isn't in a header the
        tool can read for free; callers fall back to ``ChainStep.output_ratio``.
        Never a correctness input, so an unreadable file is ``None``, not an
        error. Blocking (a small header read) — call it off the event loop.
        """

    def companion_outputs(self, output_path: str, mode: str) -> list[str]:
        """Sibling output paths this mode writes beside ``output_path``.

        A mode that produces more than one file (extractcd's ``.cue`` + ``.bin``
        sidecar, a split makeps3iso ``.iso.0``/``.1``/…) returns the *extra*
        paths here so conflict detection, overwrite cleanup, size accounting and
        in-use tracking enumerate them from one place instead of each re-encoding
        the per-mode suffix. ``output_path`` itself is never included.
        """


class BaseTool:
    """Shared helpers so concrete tools stay tiny."""

    id: str
    display_name: str
    modes: Sequence[ModeSpec] = ()
    output_extensions: frozenset[str] = frozenset()
    verify_extensions: frozenset[str] = frozenset()
    # Default False: a tool whose container file SHA1 might be DAT-indexed
    # still falls back to a file-level hash after an embedded-hash miss.
    embedded_hash_is_exhaustive: bool = False

    def __init__(self, binary_path: str) -> None:
        self.binary_path = binary_path

    @property
    def input_extensions(self) -> frozenset[str]:
        if not self.modes:
            return frozenset()
        return frozenset().union(*(m.input_extensions for m in self.modes))

    def spec(self, mode: str) -> ModeSpec:
        for m in self.modes:
            if m.mode == mode:
                return m
        raise KeyError(mode)

    @staticmethod
    def _basic_info_fields(raw: dict) -> dict:
        """The shared ``BasicFileInfo`` fields pulled from a tool's raw info dict.

        The five "what is this file" tools (z3ds, nsz, cso, romz, makeps3iso)
        build their info model from this, so the identical ``raw[...] -> field``
        mapping lives in one place. Required fields are indexed directly (the
        services always populate them); the optional pair uses ``.get`` so a
        missing key becomes ``None`` rather than a KeyError.
        """
        return {
            "file": raw["file"],
            "size": raw["size"],
            "size_display": raw["size_display"],
            "format": raw.get("format"),
            "extension": raw["extension"],
            "compressed": raw["compressed"],
            "compression_type": raw.get("compression_type"),
        }

    def detect_output(self, input_path: str) -> OutputStatus | None:
        return None

    def accepts_directory(self, path: str) -> bool:
        # Default: file-only tool. Directory-input tools (makeps3iso) override.
        return False

    def verifies_path(self, path: str) -> bool:
        # Default: a declared-extension match (suffix-based, so a compound
        # extension resolves — see utils.path_utils.match_extension). Tools that
        # over-claim a container extension (e.g. romz on .7z/.zip) override with
        # per-file inspection.
        return match_extension(path, self.verify_extensions) is not None

    async def embedded_hashes(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> list[tuple[str, str]]:
        # Default: no embedded hash; callers fall back to file-level SHA1.
        return []

    async def is_ready(self) -> bool:
        # Default: nothing to check. Tools gated on a user-supplied secret or
        # an optional binary override this (see nsz / prod.keys).
        return True

    def expected_output_size(self, input_path: str, mode: str) -> int | None:
        # Default: unknown. Tools whose source header states the output size
        # (maxcso's CSO/ZSO/DAX, nkit2iso's NKit) override it so the chain
        # preflight sizes the intermediate exactly instead of by ratio.
        return None

    async def post_convert(
        self, input_path: str, output_path: str, mode: str,
    ) -> None:
        return None

    def companion_outputs(self, output_path: str, mode: str) -> list[str]:
        # Default: derive the sibling paths from the mode's ``companion_exts``
        # as suffix swaps off ``output_path`` (e.g. extractcd ``.cue`` -> the
        # ``.bin`` data track). Pure path math, no disk I/O, returned regardless
        # of on-disk existence — callers guard existence/lock as needed. Tools
        # whose companion set is dynamic (makeps3iso's size-dependent split
        # parts) override this with their own disk-aware probe.
        exts = self.spec(mode).companion_exts
        return [str(Path(output_path).with_suffix(ext)) for ext in exts]

    def overwrite_targets(self, output_path: str, mode: str) -> list[str]:
        # Default: the finished output set — the primary plus its companions.
        # Correct for every mode whose failed runs leave nothing a successful
        # run wouldn't. makeps3iso overrides (a mid-split failure can leave the
        # base *and* numbered parts, which never coexist on success).
        return [output_path, *self.companion_outputs(output_path, mode)]
