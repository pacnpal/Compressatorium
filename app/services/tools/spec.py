"""Mode metadata for the tool plugin registry.

A ``ModeSpec`` carries everything the dispatch sites need to know about a
conversion mode without branching on tool identity. Each ``ModeSpec.mode``
equals a ``ConversionMode`` value (the validated wire type in ``models.py``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# ``InputKind`` lives in ``models`` so ``ConversionJob`` can type its field
# against it without importing the ``services.tools`` package (which imports
# ``models`` to build the registry — the reverse import would be a cycle). It is
# re-exported here so ``from services.tools.spec import InputKind`` and the
# ``ModeSpec.input_kinds`` default keep working unchanged.
from models import InputKind


class ModeKind(str, Enum):
    CREATE = "create"      # source -> compressed container
    EXTRACT = "extract"    # compressed container -> source
    COPY = "copy"          # recompress in place
    COMPRESS = "compress"  # generic one-shot compressor (z3ds-style)


@dataclass(frozen=True)
class ModeSpec:
    mode: str                       # wire value, == ConversionMode value
    tool_id: str                    # "chdman" | "dolphin" | "z3ds"
    kind: ModeKind
    label: str                      # UI label
    group: str                      # UI group id
    output_ext: str | None          # ".chd"/".rvz"/None when input-ext-mapped
    input_extensions: frozenset[str]
    supports_compression: bool = False
    supports_compression_level: bool = False  # dolphin rvz/wia only
    supports_delete_on_verify: bool = False
    # Whether the tool can verify this mode's product *without* the question of
    # deleting anything. Implied by `supports_delete_on_verify` (deleting needs
    # a verify first), so only a mode that can be checked but must never delete
    # its source declares it: makeps3iso's folder->iso reads PARAM.SFO back out
    # of the ISO it built, while deleting a curated game folder on the strength
    # of that is not something to offer. Gating "verify each result" on the
    # delete flag made that check unreachable for exactly those modes.
    supports_verify: bool = False
    allows_archive_input: bool = False         # chdman create modes only
    # Sibling outputs this mode writes beside its primary output_path, expressed
    # as suffix swaps off the primary (e.g. extractcd's ``.cue`` -> ``.bin``
    # data-track sidecar). Read by ``BaseTool.companion_outputs`` so conflict
    # detection, overwrite cleanup, size accounting and in-use tracking enumerate
    # companions from one place instead of re-hardcoding the suffix per consumer.
    # Modes whose companion set is dynamic (makeps3iso's size-dependent split
    # parts) leave this empty and override ``companion_outputs`` instead.
    companion_exts: tuple[str, ...] = ()
    # Default keeps every existing mode FILE-based (zero behavior change). A
    # directory mode (folder->iso) overrides to {InputKind.DIRECTORY}.
    input_kinds: frozenset[InputKind] = frozenset({InputKind.FILE})
    # Platforms this MODE serves, when they differ from its tool's. Empty means
    # "ask the tool", which is right for every single-system tool: chdman's
    # modes are all as PS2/PSX/Dreamcast as chdman is. It exists for composite
    # modes, where the tool is a shell and each mode belongs to a different
    # system -- the chain tool declares no platforms of its own, so without
    # this its GameCube NKit->RVZ mode was offered on PS2 and its PS2 CSO->CHD
    # mode on GameCube, and a saved rule would convert a disc to a format for
    # the wrong console.
    platform_slugs: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ChainStep:
    """One step of a :class:`ChainSpec`: a sub-mode run by an existing tool."""

    tool_id: str          # owner of this step's mode ("cso", "chdman")
    mode: str             # the sub-mode wire value ("cso_decompress", "createdvd")
    weight: float         # progress weight; normalized across steps at runtime
    output_ratio: float   # est. (this step's output size) / (chain input size)


@dataclass(frozen=True)
class ChainSpec:
    """A composite mode that runs an ordered pipeline of existing modes.

    Structurally a superset of :class:`ModeSpec`, so every registry / route /
    job_manager consumer that reads a spec's fields (``mode``, ``tool_id``,
    ``kind``, ``output_ext``, ``input_extensions``, ``supports_*``,
    ``allows_archive_input``, ``input_kinds``) works unchanged. The chain
    extras (``steps``, ``intermediate_exts``, ``verify_step``) are only read by
    the synthetic ``ChainTool`` that orchestrates the pipeline.
    """

    mode: str                       # composite wire value, e.g. "cso_to_chd"
    tool_id: str                    # synthetic owner ("chain")
    kind: ModeKind
    label: str
    group: str
    output_ext: str | None          # final output extension (".chd")
    input_extensions: frozenset[str]  # == step 1's accepted inputs
    steps: tuple[ChainStep, ...]    # ordered: first owns input, last owns output
    intermediate_exts: tuple[str, ...] = ()   # one per non-final step (".iso",)
    verify_step: int = -1           # index of the step whose tool verifies the final output
    supports_compression: bool = False
    supports_compression_level: bool = False
    supports_delete_on_verify: bool = True   # of the ORIGINAL source, gated on final verify
    # Structural parity with ModeSpec: a chain's final step verifies the
    # output, and its delete flag already implies that, so this stays False and
    # `registry.mode_supports_verify` reads them together.
    supports_verify: bool = False
    allows_archive_input: bool = True        # == step 1's
    input_kinds: frozenset[InputKind] = field(
        default_factory=lambda: frozenset({InputKind.FILE})
    )
    # Structural parity with ModeSpec.companion_exts so BaseTool.companion_outputs
    # works for chain modes too (cso_to_chd's single .chd has no companion).
    companion_exts: tuple[str, ...] = ()
    # Structural parity with ModeSpec.platform_slugs -- and the reason that
    # field exists. The chain tool is a shell with no platform of its own, so
    # each composite mode has to name its own system or it is offered on every
    # one of them.
    platform_slugs: frozenset[str] = frozenset()
