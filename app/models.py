from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict


class InputKind(str, Enum):
    """The unit of work a conversion mode consumes.

    The codebase historically keyed every input seam off
    ``Path(filename).suffix``, which assumes a file. A directory has no suffix,
    so a mode that packages a folder (makeps3iso folder->iso) declares
    ``DIRECTORY`` and relies on a detector predicate
    (``ToolPlugin.accepts_directory``) instead of an extension match.

    Defined here (rather than in ``services.tools.spec``) so ``ConversionJob``
    can type its ``input_kind`` field against the enum without importing the
    ``services.tools`` package — that package imports ``models`` while building
    the registry, so the reverse import would be a cycle. ``spec.py`` re-exports
    it, keeping ``from services.tools.spec import InputKind`` working.
    """

    FILE = "file"
    DIRECTORY = "directory"


class ConversionMode(str, Enum):
    CREATERAW = "createraw"
    CREATEHD = "createhd"
    CREATECD = "createcd"
    CREATEDVD = "createdvd"
    CREATELD = "createld"
    EXTRACTRAW = "extractraw"
    EXTRACTHD = "extracthd"
    EXTRACTCD = "extractcd"
    EXTRACTDVD = "extractdvd"
    EXTRACTLD = "extractld"
    COPY = "copy"
    DOLPHIN_RVZ = "dolphin_rvz"
    DOLPHIN_WIA = "dolphin_wia"
    DOLPHIN_GCZ = "dolphin_gcz"
    DOLPHIN_ISO = "dolphin_iso"
    Z3DS_COMPRESS = "z3ds_compress"
    Z3DS_DECOMPRESS = "z3ds_decompress"
    NSZ_COMPRESS = "nsz_compress"
    NSZ_DECOMPRESS = "nsz_decompress"
    CSO_COMPRESS = "cso_compress"
    CSO2_COMPRESS = "cso2_compress"
    ZSO_COMPRESS = "zso_compress"
    DAX_COMPRESS = "dax_compress"
    CSO_DECOMPRESS = "cso_decompress"
    CSO_TO_CHD = "cso_to_chd"
    ROMZ_7Z = "romz_7z"
    ROMZ_ZIP = "romz_zip"
    ROMZ_EXTRACT = "romz_extract"
    JWUD_COMPRESS = "jwud_compress"
    JWUD_DECOMPRESS = "jwud_decompress"
    FOLDER_TO_ISO = "folder_to_iso"
    NKIT_RESTORE = "nkit_restore"
    NKIT_TO_RVZ = "nkit_to_rvz"
    METADATA_SCAN = "metadata_scan"
    DAT_MATCH = "dat_match"


class DuplicateAction(str, Enum):
    SKIP = "skip"
    OVERWRITE = "overwrite"
    RENAME = "rename"


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OutputStatus(BaseModel):
    """A sibling output a tool could produce for a given input file."""
    tool_id: str
    exists: bool          # finished output file present on disk
    ready: bool           # present and not mid-conversion
    path: str | None = None


class FileEntry(BaseModel):
    name: str
    # Human-facing label when it differs from the filename: a library manager's
    # curated game title ("Super Mario Bros." for smb_u_rev1.sfc). Display only
    # -- `name` remains the real filename that rename/delete/convert act on, so
    # nothing that touches the filesystem may read this. None for ordinary
    # directory listings.
    display_name: str | None = None
    path: str
    type: str  # "file", "directory", or "archive"
    size: int | None = None
    extension: str | None = None
    archive_items: int | None = None
    # Count of archive members that already have an existing output from any
    # registered tool (.chd/.rvz/.z3ds/.nsz/…), finished or mid-conversion.
    archive_has_output: int | None = None
    archive_truncated: bool | None = None
    media_type: str | None = None  # "dvd", "cd", or None - for CHD files
    convertible_by: list[str] = []   # tool ids whose input_extensions accept this file
    outputs: list[OutputStatus] = []  # detected sibling outputs, one per producing tool
    # Tool ids whose Verify/Info apply to THIS concrete path (per-file refinement
    # of verify_extensions). Mostly an extension match, but romz inspects an
    # archive's members so only single-ROM .7z/.zip surface its row-actions —
    # not every archive. Empty when no tool can verify/info the path.
    verifiable_by: list[str] = []
    # For a folded split-ISO set (makeps3iso -s output: Game.iso.0, .1, ...): the
    # number of part files this single logical entry represents. None for an
    # ordinary single-file entry. The entry's ``path`` points at the ``.0`` part
    # (what RPCS3 mounts / verify reads) and ``size`` is the summed part size.
    split_parts: int | None = None


class DirectoryListing(BaseModel):
    volume: str
    path: str
    entries: list[FileEntry]


class Volume(BaseModel):
    name: str
    path: str


class ConversionJob(BaseModel):
    id: str
    file_path: str
    filename: str
    mode: ConversionMode
    status: JobStatus
    progress: int = 0
    message: str = ""
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    output_path: str | None = None
    output_size: int | None = None
    temp_dir: str | None = None
    allow_overwrite: bool = False
    compression: str | None = None
    delete_on_verify: bool = False
    # Verify the output and keep the source. Independent of delete_on_verify,
    # which verifies as a precondition for deleting; this asks for the check on
    # its own, so an unattended conversion can prove its result without the
    # destructive half.
    verify_after: bool = False
    # Split the output into ~4 GB parts for FAT32 targets (makeps3iso -s).
    # Only meaningful for the folder->iso directory mode; ignored elsewhere.
    split: bool = False
    # The unit of work this job consumes. FILE for every existing mode; a
    # directory-input mode (makeps3iso folder->iso) carries DIRECTORY so the
    # pipeline skips the archive-extract / file-only assumptions and the lock
    # manager can protect the whole source subtree. Serialized to its string
    # value at the API/persistence edge (InputKind is a str enum).
    input_kind: InputKind = InputKind.FILE


class JobCreateRequest(BaseModel):
    file_path: str
    mode: ConversionMode = ConversionMode.CREATECD
    output_dir: str | None = None  # If None, output alongside source
    duplicate_action: DuplicateAction = (
        DuplicateAction.SKIP
    )  # What to do if output exists
    compression: str | None = None  # Comma-separated list (e.g. "zlib,lzma")
    delete_on_verify: bool = False
    split: bool = False  # makeps3iso folder->iso: split output into 4 GB parts


class BatchJobCreateRequest(BaseModel):
    file_paths: list[str]
    mode: ConversionMode = ConversionMode.CREATECD
    output_dir: str | None = None  # If None, output alongside source
    duplicate_action: DuplicateAction = (
        DuplicateAction.SKIP
    )  # What to do if output exists
    compression: str | None = None  # Comma-separated list (e.g. "zlib,lzma")
    delete_on_verify: bool = False
    split: bool = False  # makeps3iso folder->iso: split output into 4 GB parts


class CheckDuplicatesRequest(BaseModel):
    file_paths: list[str]
    output_dir: str | None = None
    mode: ConversionMode = ConversionMode.CREATECD


class DuplicateInfo(BaseModel):
    file_path: str
    output_path: str
    exists: bool


class ArchiveEntry(BaseModel):
    archive_path: str
    internal_path: str
    name: str
    size: int
    extension: str
    convertible: bool = False


class CHDInfo(BaseModel):
    file: str
    input_file: str | None = None
    file_version: str | None = None
    logical_size: str | None = None
    hunk_size: str | None = None
    total_hunks: str | None = None
    unit_size: str | None = None
    total_units: str | None = None
    compression: str | None = None
    chd_size: str | None = None
    ratio: str | None = None
    sha1: str | None = None
    data_sha1: str | None = None
    raw_data: str = ""
    media_type: str | None = None  # "dvd", "cd", or None
    game_id: str | None = None    # disc serial / title ID (e.g. SLUS-20312)
    title: str | None = None      # human-readable game title (when available)


class BulkDeleteRequest(BaseModel):
    paths: list[str]


class BulkVerifyRequest(BaseModel):
    paths: list[str]


class MetadataBatchRequest(BaseModel):
    paths: list[str]


class DeletePlanRequest(BaseModel):
    file_paths: list[str]
    mode: ConversionMode = ConversionMode.CREATECD


class DolphinDiscInfo(BaseModel):
    file: str
    game_id: str | None = None
    game_name: str | None = None
    title_id: str | None = None
    disc_number: str | None = None
    revision: str | None = None
    region: str | None = None
    country: str | None = None
    format: str | None = None
    compression: str | None = None
    compression_level: str | None = None
    block_size: str | None = None
    file_size: str | None = None
    raw_data: str = ""


class BasicFileInfo(BaseModel):
    """Shared shape for the simple per-file info models: identity, size, and the
    format/compression summary.

    The five tools whose /info just reports "what is this file" (z3ds, nsz, cso,
    romz, makeps3iso) return this exact shape; the two that add fields (romz's
    archive ratio, makeps3iso's title) subclass it instead of repeating it.
    """
    file: str
    size: int
    size_display: str
    format: str | None = None
    extension: str
    compressed: bool
    compression_type: str | None = None


class Z3DSInfo(BasicFileInfo):
    """Information about a Nintendo 3DS ROM file."""


class NszInfo(BasicFileInfo):
    """Information about a Nintendo Switch NSP/XCI/NSZ/XCZ file."""


class CsoInfo(BasicFileInfo):
    """Information about a PSP/PS2 ISO/CSO/ZSO/DAX file."""


class RomzInfo(BasicFileInfo):
    """Information about a handheld ROM (GB/GBC/GBA/NDS) or its .7z/.zip archive."""
    # Archive-only extras: the single ROM inside, its uncompressed size, and the
    # archive-to-original size ratio (None for a loose ROM source).
    contained_name: str | None = None
    original_size: int | None = None
    ratio: str | None = None


class JwudInfo(BasicFileInfo):
    """Information about a Wii U disc image (.wud) or its .wux container."""
    # WUX-only extras, read straight from the container header: the size of the
    # image it decompresses back to, and the resulting compression ratio (None
    # for a raw .wud source, which carries no such header).
    original_size: int | None = None
    ratio: str | None = None


class Ps3IsoInfo(BasicFileInfo):
    """Information about a decrypted PS3 disc/JB folder (makeps3iso source)."""
    title: str | None = None
    title_id: str | None = None


class NkitInfo(BasicFileInfo):
    """Information about an NKit-shrunk GameCube/Wii image (nkit2iso source).

    Everything below the shared fields comes from the NKit/disc header the
    binary itself reads: which console the disc is for, how it identifies
    itself, how big the restored ISO will be, and the CRC32 the restore is
    checked against.
    """
    platform: str | None = None          # "GameCube" | "Wii"
    game_id: str | None = None           # 6-char game + maker code, e.g. GALE01
    title: str | None = None
    disc_number: int | None = None
    disc_version: int | None = None
    restored_size: int | None = None     # bytes the restored .iso will occupy
    restored_size_display: str | None = None
    crc32: str | None = None             # of the ORIGINAL image, stored by NKit
    ratio: str | None = None             # shrunk size as a % of the original


class LayoutPreferences(BaseModel):
    """Workspace layout widths persisted across sessions.

    ``panels`` holds the global panel-split widths (px); ``columns`` holds
    per-tool table column widths keyed by tool id. Extra keys are allowed
    so the client can evolve the shape without a schema change here; the
    server just stores and returns the JSON.
    """

    model_config = ConfigDict(extra="allow")

    panels: dict | None = None
    columns: dict | None = None


class ConversionPreferences(BaseModel):
    """Per-tool compression defaults, keyed by tool id.

    Values are the wire-format compression string the convert pipeline expects
    (e.g. ``"solid:18"`` for nsz, ``"zstd:19"`` for dolphin, ``"zlib,lzma"`` for
    chdman). Extra keys are allowed so new tools persist without a schema change.
    """

    model_config = ConfigDict(extra="allow")
