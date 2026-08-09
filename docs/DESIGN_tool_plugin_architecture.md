# Design: Tool Plugin Architecture

**Status:** Proposal · **Scope:** internal refactor, no user-facing behavior change ·
**Companion:** `ADDING_PLATFORMS_AND_TOOLS.md`

## 1. Problem

Adding one conversion tool today touches ~20 files, and the standing advice
is "copy what z3ds does" in each of them. The tool-service contract already
exists *implicitly*, every service (`chdman`, `dolphin_tool`,
`z3ds_compress`) exposes the same shape (`convert` / `verify` /
`verify_stream` / `info` / `is_convertible` / `get_output_path_for_mode`),
but it was never formalized, so tool-specific knowledge is hand-scattered into
hardcoded `if/elif` ladders and string-prefix checks across the codebase.

### 1.1 Where tool knowledge leaks today

| Concern | Location | Current shape |
|---------|----------|---------------|
| Which service handles a mode | `services/job_manager.py:1444` | `dolphin if startswith("dolphin_") else z3ds if == Z3DS_COMPRESS else chdman` |
| Output path on enqueue | `services/job_manager.py:139` | `if mode == Z3DS_COMPRESS … else chdman.get_chd_path` |
| Output path in routes | `routes/convert.py:100` `_get_output_path` | dolphin / z3ds / chdman branches |
| Verify dispatch | `services/job_manager.py:1556`, `:1591` | z3ds branch + dolphin/chdman pair |
| "is create mode?" etc. | `routes/convert.py` (`:87`, `:96`, `:305`–`:334`, `:407`) | `mode.startswith("create"/"extract"/"dolphin_")`, `== "z3ds_compress"` |
| Extension validation | `routes/convert.py:412`–`427` **and** `:656`–`666` | duplicated in `create_job` + `create_batch_jobs` |
| Archive-input guard | `routes/convert.py:357`, `:606` | per-mode allow/deny list |
| Convertibility flags | `routes/files.py:11`–`13`, `:169`–`204`, `:316`–`353` | one import + flag block per tool, twice |
| Info + verify endpoints | `routes/info.py` (~1500 lines) | three near-identical copies (chd / dolphin / z3ds) of verify, verify/events, verify-batch/events |
| Binary path | `config.py:111`–`121` | one `Field` per tool |
| Info model | `models.py:139`,`:176`,`:193` | `CHDInfo` / `DolphinDiscInfo` / `Z3DSInfo` |
| Mode enum | `models.py:7`–`25` | flat `ConversionMode` strings |
| Per-tool output flags | `models.py:42`–`63` | `has_chd`, `has_rvz`, `dolphin_ready`, `z3ds_ready`, … |
| Subprocess orchestration | `services/chdman.py`, `dolphin_tool.py`, `z3ds_compress.py` | ~150 near-identical lines each (spawn, nice/ionice, stall loop, `\r`/`\n` buffering, cancel watcher, PID tracking) |
| Frontend dispatch | `static/js/app.js` (~10 sites), `api.js` | tool branches for label, hint, filters, `MODE_GROUPS`, info modal, verify routing |

The rule of three is satisfied (three tools exist), so abstracting this is no
longer premature, it is overdue.

### 1.2 Goals / non-goals

**Goals**
- A new tool is defined in **one module** and registered in **one place**.
- Dispatch sites ask a **registry** instead of branching on tool identity.
- Eliminate the single-vs-batch and per-tool-endpoint duplication.
- Zero user-facing behavior change; ship incrementally, stay green.

**Non-goals**
- No dynamic/third-party plugin loading (no `importlib`/entry-points
  discovery). An in-process registry of first-party tools is the right scope.
- No change to the wire API (mode strings, endpoint paths, JSON shapes) until
  a later, optional cleanup phase.
- No change to the job queue / concurrency / locking model.

---


### 2.1 Web/API authentication boundary

The FastAPI app can apply a single HTTP middleware (`app/auth.py`) before static
files and all routers. The entire auth system is gated on
`COMPRESSATORIUM_ENABLE_AUTH` (default `false`): when disabled, the middleware is
not registered and no token is generated. When enabled, it protects the Web UI
and every `/api` route with the same token check, while leaving `/health` open
for container health checks. Tokens are read from headers only (HTTP Basic,
`Authorization: Bearer`, or `X-Compressatorium-Token`) — never the query string —
and state-changing requests (`POST`/`PUT`/`PATCH`/`DELETE`) are additionally
restricted to same-origin callers (`Sec-Fetch-Site`/`Origin`) so cached browser
Basic credentials can't be abused cross-site. Keep new routers behind that
app-level middleware rather than adding per-router auth branches.

## 2. Target architecture

```
                         ┌──────────────────────────────┐
                         │  tool registry (singleton)    │
                         │  register(plugin)             │
                         │  for_mode / spec / for_input  │
                         └───────────────┬───────────────┘
        ┌────────────────────────────────┼────────────────────────────────┐
        ▼                                 ▼                                 ▼
 ChdmanTool(BaseTool)            DolphinTool(BaseTool)            Z3dsTool(BaseTool)
   modes=[ModeSpec...]             modes=[ModeSpec...]              modes=[ModeSpec...]
   _build_command()                _build_command()                 _build_command()
   _parse_progress()               _parse_progress()                _parse_progress()
        └───────────────── shared via ─────────────────────────────────────┘
                         SubprocessRunner (spawn / stall / cancel / PID)

 consumers (no tool branching):
   job_manager   → registry.for_mode(mode).convert(...) / .verify(...)
   convert route → registry.spec(mode) for validation + .output_path(...)
   files route   → loop registry.tools_for_listing(...)
   info route    → routes generated from registry (one generic verify adapter)
   frontend      → TOOLS descriptor table drives generic components
```

Two new descriptor types, one base class, one runner, one registry.

---

## 3. Concrete interfaces

New package `app/services/tools/`:

```
app/services/tools/
  __init__.py        # builds the registry, registers all tools
  spec.py            # ModeSpec, ModeKind
  base.py            # ToolPlugin (Protocol) + BaseTool (ABC)
  runner.py          # SubprocessRunner
  registry.py        # ToolRegistry
  chdman.py          # ChdmanTool   (wraps/owns today's chdman logic)
  dolphin.py         # DolphinTool
  z3ds.py            # Z3dsTool
```

### 3.1 `spec.py`: modes carry metadata

```python
from dataclasses import dataclass
from enum import Enum

class ModeKind(str, Enum):
    CREATE = "create"      # source -> compressed container
    EXTRACT = "extract"    # compressed container -> source
    COPY = "copy"          # recompress in place
    COMPRESS = "compress"  # generic one-shot compressor (z3ds-style)

@dataclass(frozen=True)
class ModeSpec:
    mode: str                       # wire value, e.g. "createcd" (== ConversionMode value)
    tool_id: str                    # "chdman" | "dolphin" | "z3ds"
    kind: ModeKind
    label: str                      # UI label
    group: str                      # UI group id ("create","extract","copy","dolphin","z3ds")
    output_ext: str | None          # ".chd"/".rvz"/None when input-ext-mapped
    input_extensions: frozenset[str]
    supports_compression: bool = False
    supports_compression_level: bool = False   # dolphin rvz/wia only
    supports_delete_on_verify: bool = False
    allows_archive_input: bool = False         # opt-in; set True on every mode that can take a member straight from an archive (chdman create + extract, dolphin, z3ds). Only chdman copy (.chd recompress round-trip) leaves it False
```

**Archive browsing is global, scoped to known extensions; the convert gate is
narrower.** Two registry unions, two jobs:

- `convertible_extensions()` — every source extension any tool recognizes. The
  archive *browse* listing (`ArchiveService._listable_extensions`) uses this
  minus the archive containers, so anything we know how to compress shows up
  when you look inside an archive. A finished output a tool disowns as a source
  stays hidden for free (chdman drops `.chd` from its `input_extensions`).
- `archive_input_extensions()` — only the inputs of `allows_archive_input` modes.
  This is the *convert gate* in `plan_job` and the filter the recursive *search*
  path uses (`ArchiveService._convert_gate_extensions`).

A member is *listed* (browse) far more liberally than it is *convertible in
place* (gate). A romz ROM is a known source, so it shows when you browse into its
archive — fixing the "Empty folder / 0 items" bug — but no `allows_archive_input`
mode accepts it, so `tools_accepting_archive_member` returns nothing and it
carries no conversion affordance (recompressing an archived ROM would be
recursive). Search stays on the narrow gate so list-only members can't consume
the per-archive entry cap ahead of a genuine convertible member.

Every prefix check in the codebase maps to a field:

| Today | After |
|-------|-------|
| `mode.startswith("create")` | `spec.kind == ModeKind.CREATE` |
| `mode.startswith("extract")` | `spec.kind == ModeKind.EXTRACT` |
| `mode.startswith("dolphin_")` | `spec.tool_id == "dolphin"` |
| `mode == "z3ds_compress"` | `spec.tool_id == "z3ds"` (or `kind == COMPRESS`) |
| `supports_delete_on_verify(mode)` (`convert.py:87`) | `spec.supports_delete_on_verify` |
| GCZ/ISO compression special-cases (`convert.py:315`–`324`) | `spec.supports_compression` |

`ConversionMode` (the Pydantic enum in `models.py`) **stays**, it remains the
validated wire type. `ModeSpec.mode` equals `ConversionMode(...).value`. The
registry is the bridge between the two.

### 3.2 `base.py`: the plugin contract

```python
from collections.abc import AsyncGenerator, Sequence
from typing import Protocol, runtime_checkable
from pathlib import Path
from pydantic import BaseModel
import asyncio

@runtime_checkable
class ToolPlugin(Protocol):
    id: str
    display_name: str
    binary_path: str
    modes: Sequence["ModeSpec"]
    input_extensions: frozenset[str]    # convertible-from
    output_extensions: frozenset[str]   # produced (badges + scan discovery)
    verify_extensions: frozenset[str]   # accepted by verify()
    embedded_hash_is_exhaustive: bool   # miss => definitive (skip file SHA1)

    def output_path(self, mode: str, input_path: str, output_dir: str | None = None,
                    *, treat_as_stem: bool = False) -> str: ...

    def convert(self, input_path: str, output_path: str, mode: str, *,
                compression: str | None = None,
                cancel_event: asyncio.Event | None = None) -> AsyncGenerator[dict, None]: ...

    async def verify(self, path: str) -> dict: ...                  # {"valid","message"}
    def verify_stream(self, path: str) -> AsyncGenerator[dict, None]: ...
    async def info(self, path: str) -> dict: ...                    # raw dict
    def info_model(self, raw: dict, path: str) -> BaseModel: ...    # typed model for the API
    # The five simple "what is this file" models (z3ds/nsz/cso/romz/makeps3iso)
    # subclass models.BasicFileInfo (file/size/size_display/format/extension/
    # compressed/compression_type); their info_model() builds the shared fields
    # via BaseTool._basic_info_fields(raw) and adds only their extras, so the
    # raw->model mapping is defined once.

    # DAT-match fast path: (sha1, match_type) pairs the tool can report
    # cheaply (chdman header/data SHA1 from the metadata cache, dolphin disc
    # SHA1 via verify). Default empty -> caller falls back to file-level SHA1.
    # Raise EmbeddedHashUnavailable when the tool *should* yield a hash but the
    # attempt failed / the cache is stale, so the caller skips the meaningless
    # file-level fallback and does NOT cache a false negative. cancel_event lets
    # a background scan/match job abort an expensive derivation promptly.
    async def embedded_hashes(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> list[tuple[str, str]]: ...

    def active_pids(self) -> list[int]: ...

    # Post-convert hook, run once after a successful conversion. Default no-op
    # (BaseTool); ChdmanTool overrides it to embed disc-ID GAME/NAME tags into a
    # freshly created CD/DVD CHD. job_manager calls it generically for every
    # mode, and ChainTool routes its final step through the same hook, so a
    # cso_to_chd CHD is tagged exactly like a direct createdvd.
    async def post_convert(self, input_path: str, output_path: str, mode: str) -> None: ...

    # Sibling outputs a multi-file mode writes beside its primary output_path
    # (extractcd's .cue + .bin data track; a split folder_to_iso's .iso.0/.1/…).
    # The single source conflict-check, unique-name probing, overwrite-clear,
    # size-sum and in-use tracking enumerate, instead of each re-encoding the
    # per-mode suffix. output_path itself is never included.
    def companion_outputs(self, output_path: str, mode: str) -> list[str]: ...

    # Every path an authorized overwrite must sweep, primary FIRST. A superset
    # of companion_outputs: it includes output_path, and it enumerates states
    # only a *failed* run leaves (a makeps3iso -s build interrupted mid-split
    # leaves the not-yet-renamed base AND numbered parts, which never coexist
    # on success — so companion_outputs, which describes a finished output,
    # would under-report). Enumeration only; the pipeline validates and unlinks.
    def overwrite_targets(self, output_path: str, mode: str) -> list[str]: ...

    # Runtime prerequisites met? False => GET /api/tools reports the tool
    # unavailable and the frontend hides it, instead of offering jobs that can
    # only fail. Default True; nsz overrides (prod.keys). Async because the
    # probe may touch disk.
    async def is_ready(self) -> bool: ...
```

`BaseTool(ABC)` provides shared defaults so concrete tools stay tiny:

```python
class BaseTool:
    id: str
    display_name: str
    modes: Sequence[ModeSpec] = ()

    def __init__(self, binary_path: str):
        self.binary_path = binary_path
        self._runner = SubprocessRunner(owner=self.id)

    # derived sets (no per-tool duplication)
    @property
    def input_extensions(self) -> frozenset[str]:
        return frozenset().union(*(m.input_extensions for m in self.modes))

    def spec(self, mode: str) -> ModeSpec:
        for m in self.modes:
            if m.mode == mode:
                return m
        raise KeyError(mode)

    # default verify() wraps verify_stream(), identical in all 3 services today
    async def verify(self, path: str) -> dict:
        final = {"valid": False, "message": "Verification failed"}
        async for u in self.verify_stream(path):
            if u.get("type") in ("complete", "error"):
                final = u
        return {"valid": bool(final.get("valid")), "message": final.get("message") or "Verification failed"}

    def active_pids(self) -> list[int]:
        return self._runner.active_pids()

    async def post_convert(self, *a, **k) -> None:
        return None

    # Default: suffix-swap off output_path for each ModeSpec.companion_exts
    # (extractcd .cue -> .bin). makeps3iso overrides with its disk-probed
    # contiguous split parts using direct .0/.1/... probes rather than sibling
    # directory scans; modes with no companion_exts return [].
    def companion_outputs(self, output_path, mode) -> list[str]:
        return [str(Path(output_path).with_suffix(e)) for e in self.spec(mode).companion_exts]

    # Default: the finished output set. Correct for every mode whose failed
    # runs leave nothing a successful run wouldn't; makeps3iso overrides.
    def overwrite_targets(self, output_path, mode) -> list[str]:
        return [output_path, *self.companion_outputs(output_path, mode)]

    # Default: nothing to check.
    async def is_ready(self) -> bool:
        return True

    # subclasses implement: output_path, convert (via self._runner.run),
    # verify_stream, info, info_model
```

> **Normalization note:** `info()` is async on the contract.
> `chdman.info`/`dolphin.header` are already async; `z3ds.info` is sync, so
> `Z3dsTool.info` wraps it in `run_in_threadpool`. `dolphin` exposes header
> data via `header()` today, `DolphinTool.info` simply calls it.

### 3.3 `runner.py`: shared subprocess orchestration

This collapses the ~150 near-identical lines that were duplicated in every
tool's `convert()`. All eight conversion tools now delegate their streaming loop
here: `chdman`, `dolphin_tool`, `romz`, `makeps3iso`, `jwudtool` directly, and
`z3ds_compress`, `maxcso`, `nsz` via the size-based-progress seam below (their
CLIs print no parseable percent, so the growing output file is the progress
signal).

```python
class SubprocessRunner:
    def __init__(self, owner: str):
        self._owner = owner
        self._active_pids: set[int] = set()
        self._lock = threading.Lock()

    def active_pids(self) -> list[int]: ...

    async def run(self, cmd: list[str], *, input_path: str, output_path: str,
                  parse_progress, cancel_event=None, heartbeat=False,
                  fail_label="process", complete_message="Conversion complete",
                  cwd=None, output_growth_paths=None, size_progress=None,
                  nice_via_wrapper=False, env=None,
                  require_output=False) -> AsyncGenerator[dict, None]:
        """Spawn cmd, stream stdout, yield {"progress","message"}.
        Handles: nice/ionice wrap, stdbuf, PID tracking, deterministic \\r/\\n
        line buffering (CRLF/CR normalized to LF in one pass via
        `_split_stream_lines` so segmentation is a pure function of the byte
        stream, not chunk boundaries -- issue #183),
        stall timeout via compute_progress_stall_timeout, cancel-watcher
        (terminate->kill), ConversionCancelled, non-zero exit -> RuntimeError(tail),
        final 100%. `parse_progress(line) -> int|None` is the only per-tool knob
        in the common path. `cwd` sets the working directory (romz runs `7z a`
        from the ROM dir).

        Opt-in seams, each defaulting off so the direct callers are unaffected:
        - `heartbeat` — dolphin's 2s "Converting... (Ns)" keep-alive.
        - `output_growth_paths()` — widen the stall probe to a set of files whose
          summed size grows even as filenames change mid-run (makeps3iso split).
        - `size_progress(output_size) -> dict|None` — turn output growth into the
          emitted progress for tools with no parseable percent (maxcso/nsz/z3ds);
          see `output_size_progress()` for the shared 5–95% estimate. Also raises
          the progress floor so a later non-parseable line can't reset the bar.
        - `nice_via_wrapper` — skip preexec_fn when the caller prefixed cmd with
          nice/ionice command wrappers (maxcso/nsz avoid forking a Python callable
          in this multithreaded process before exec). `env` — forwarded to the
          subprocess (nsz's private keys-home).
        - `require_output` — treat a clean exit (return code 0) that left no file
          at `output_path` as a failure, raised as RuntimeError(tail) like the
          non-zero-exit path, so a tool that exits 0 without producing output
          still reports the reason it printed first (nsz, whose `output_path` is
          the temp file the runner already watches).
        """

    async def run_capture(self, cmd: list[str], *, timeout=None,
                          cancel_event=None, stderr_to_stdout=False
                          ) -> tuple[int | None, bytes, bytes]:
        """One-shot counterpart to run(): buffered (returncode, stdout, stderr)
        for tools that need a result rather than streamed lines (info / header /
        embedded-hash extraction). Same PID tracking; races communicate()
        against cancel_event + timeout and terminates (TERM->KILL) on either,
        reporting returncode None to signal the abort. Used by
        `dolphin_tool.disc_hashes` so `dolphin-tool verify --algorithm sha1`
        (the Dolphin disc-hash source for `embedded_hashes`) aborts promptly
        when a scan/match job is cancelled.
        """
```

Per-tool `convert()` becomes ~15 lines: build argv, then
`async for u in self._runner.run(cmd, ..., parse_progress=self._parse_progress): yield u`.
Tools with no parseable percent (maxcso/nsz/z3ds) pass `size_progress=` to drive
the bar from output growth and `nice_via_wrapper=True` to keep their
command-wrapper nice; nsz also forwards its keys-home `env=`, sets
`require_output=True`, and writes into a private work dir, moving the result onto
`output_path` after `run()` returns.
dolphin's heartbeat and the makeps3iso split-stall probe are likewise opt-in
flags. One-shot subprocess work (info / header / embedded-hash extraction)
shares `run_capture()` rather than re-implementing the spawn / cancel / timeout /
terminate dance per tool.

Directory-input tools that hand a source tree to a native recursive reader must
validate both at planning time and again in `JobManager._process_job` after the
source subtree lock is acquired, immediately before invoking the tool. The PS3
`folder_to_iso` path uses `is_safe_directory_tree` for both checks so a queued
job cannot be made unsafe by adding a symlink, special file, or volume-escaping
entry after planning but before `makeps3iso` starts. The worker runs the
re-check *before* `_clear_existing_output`, so a safety rejection on an
overwrite job is non-destructive — the prior output is only cleared once the
source is confirmed still safe. Because that walk can be slow on a large tree,
the worker also re-checks the cancel event immediately after it (before
clearing outputs), so a job cancelled mid-walk keeps its prior output. To keep
the planning-time walk off the hot rejection paths, `_plan_directory_job` runs
it only after the output is derived and skip/locked collisions short-circuit,
and the create/batch routes apply queue-depth backpressure (HTTP 429) *before*
planning when the queue is already full — so a doomed submit never pays for the
tree walk.

### 3.3.1 Shared archive-limit enforcement (`services/archive.py`)

`ArchiveService.enforce_archive_limits(members)` is the shared seam any
archive-backed tool MUST call before shelling out to its own extractor.
`ArchiveService`'s own extract path already applies the configured
`CHD_ARCHIVE_MAX_ENTRIES` / `CHD_ARCHIVE_MAX_MEMBER_SIZE` /
`CHD_ARCHIVE_MAX_TOTAL_SIZE` guards (zip-bomb / oversized-archive protection),
but tools that read archives via their own member listing and run a CLI
directly (e.g. `romz` shelling out to `7z` for extract/verify) would otherwise
bypass those limits. `members` is a list of `(name, uncompressed_size)` from the
tool's raw listing — pass the *unfiltered* listing so junk entries still count
against the entry/size budget. New archive-backed tools should route through
this helper rather than re-checking limits per tool; do not duplicate the size
arithmetic.

Tools that extract with **preserved member paths** (e.g. `romz` running
`7z x`) MUST reject unsafe member paths up front on **every** member before
shelling out — not just the member they intend to keep. A CLI extractor
recreates the full archive tree, so an absolute or `..`-escaping path on any
member (including ignored junk sidecars like `__MACOSX/../../victim`) could
write outside the temp dir. `ArchiveService._validate_member` is the strict,
Windows-portable guard, but it also bans `:` and `\\` — characters a loose ROM
on a POSIX volume may legally contain — so a tool that must **round-trip** the
archive it produced (verify/extract its own output) should instead block only
the actual escape vectors (absolute path, `..` component) via a local check
like `RomzService._reject_traversal`, which `os.path.normpath`s the member and
rejects only absolute / `..`-leading results. Such tools must also forbid
symlink members (zip `external_attr` `S_ISLNK` / py7zr `FileInfo.is_symlink`),
since `7z x` restores symlinks as links.

### 3.3.2 Shared cached archive member reader (`services/archive_members.py`)

Reading an archive's member list is the hot path behind two unrelated features:
`ArchiveService.list_archive_contents` (the file browser's per-archive summary)
and `RomzService.is_single_rom_archive` (the romz Verify/Info gate, §3.6). Each
used to open and parse the archive itself, so one archive row on screen was
opened **twice**, and a directory of thousands of single-ROM archives turned a
single listing into thousands of redundant `zipfile`/`py7zr`/`rarfile` opens —
the listing endpoint blocked for ~40 s and starved the browser's connection pool
behind it.

`services/archive_members.read_archive_members(path)` is the single seam every
member-listing consumer MUST go through. It returns a uniform
`list[ArchiveMember]` (`name`, `size | None`, `is_dir`, `is_symlink`) for `.zip`
/ `.7z` / `.rar`, behind **one module-global** `utils.mtime_cache.MtimeCache`
keyed by `(path, mtime_ns, size)`. Because the cache is global and format-blind,
every consumer and every supported container benefits for free, and the
invalidation is automatic: a conversion that rewrites an archive bumps its
mtime/size and the next read recomputes. Adding a new archive format is one
`_read_<ext>` plus the extension — all consumers pick it up with no further
change.

Consumers keep their own *policy* over the shared *read*: `ArchiveService`
filters members to a caller-supplied extension set — the global known-source set
(`_listable_extensions`, browse) or the narrower convert-gate set
(`_convert_gate_extensions`, search) — and enforces the size limits (§3.3.1) in
`_filter_members` (one format-agnostic pass that replaced the old per-format
`_list_zip`/`_list_7z`/`_list_rar` triplet); `romz` rejects symlink members as a
security gate. A listed member is only badged convertible-in-place when a tool can
accept it straight from an archive — the archive route derives that via
`registry.tools_accepting_archive_member(ext)` rather than a bare
`ext in input_extensions`, so a listing-only romz ROM shows with no conversion
affordance the convert route would reject. "Raw" means unfiltered — directories, junk, and symlinks are all
returned so each policy can decide. Only **metadata listing** routes through the
reader; byte-extraction paths (`ArchiveService._extract_*`) and the `7z t`
integrity check still open the archive directly because they read content, which
isn't cacheable. The cache returns a shared list, so `list_archive_contents`
builds fresh dicts per call (the archive route annotates entries in place).

The directory listing is also **lazy by default**: `GET /files` takes
`summarize_archives` (default `True` for API back-compat) but the web UI calls it
with `summarize_archives=false`. In that lazy mode an archive row's counters
(`archive_items` / `archive_has_output` / `archive_truncated`) come back `null`
(Python `None`) and `verifiable_by` comes back as an empty list `[]` (the field is
always a list, never null), so the listing renders instantly. The browser then
hydrates those badges from `POST /archive-summary` (a batch keyed by archive path,
computed by the shared `_summarize_archive` helper so it's byte-identical to the
inline `summarize_archives=true` path) — mirroring how CHD `media_type` is
hydrated via `/chd-metadata`. To keep that hydration bounded, the browser
summarizes only the **visible page** of archive rows (re-hydrating on
page/sort/filter changes), and the endpoint additionally caps each batch at
`MAX_ARCHIVE_SUMMARY_PATHS`. The summary batch and the romz gate share the
reader's cache, so hydrating a folder opens each archive at most once.

### 3.3.3 Cross-tool chaining (`ChainSpec` / `ChainTool`, `services/disk.py`)

Some conversions are a pipeline of existing single-tool modes (issue #98:
`cso_to_chd` = maxcso `cso_decompress` → chdman `createdvd`). The chaining seam
sits **above** the plugin contract rather than rewriting it.

- **`ChainStep` / `ChainSpec` (`services/tools/spec.py`)** — a `ChainSpec` is a
  structural superset of `ModeSpec` (same `mode`/`tool_id`/`kind`/`output_ext`/
  `input_extensions`/`supports_*`/`allows_archive_input` fields every registry
  consumer reads), plus `steps` (ordered `ChainStep(tool_id, mode, weight,
  output_ratio)`), `intermediate_exts`, and `verify_step`. Because it quacks
  like a `ModeSpec`, `registry.spec(mode)` / `mode_specs()` / `convertible_extensions()`
  need no changes.
- **`ChainTool(BaseTool)` (`services/tools/chain.py`)** — a synthetic tool
  (`id="chain"`) registered **after** its component tools and handed the
  `registry`. Its `convert` orchestrates the steps via
  `registry.for_mode(step.mode).convert(...)`, writing intermediates into a
  private temp dir (cleaned in `finally`), aggregating each step's 0–100 into a
  **normalized-weight** slice of one bar, and routing `compression` only to a
  step whose sub-mode supports it. `verify`/`info`/`output_path` delegate to the
  `verify_step` (final) tool, so `job_manager`'s existing verify gate and
  delete-on-verify drive the chain with no special-casing — the original source
  is deleted only after the **final** output verifies. The chain claims no
  `verify_extensions`/`output_extensions` of its own (the final tool already
  owns them). Disc-ID tagging that `job_manager` only runs for literal
  `createcd`/`createdvd` is re-applied by the chain for its final step.
- **`services/disk.ensure_headroom`** — the shared free-space preflight a chain
  runs before step 1 (a chain holds source + full intermediate + partial final
  at once, which the single-step model never did). It is multi-volume aware:
  requirements are grouped by mount (`st_dev` of the nearest existing ancestor,
  since the output dir may not exist yet) and each mount is checked once, summing
  co-located targets. Cushion is `COMPRESSATORIUM_CHAIN_DISK_MARGIN_MB`.

New chained conversions (xci→nsp, wud→wua, a future folder→iso→chd) are a new
`ChainSpec` row + its component modes — no new machinery.

### 3.3.4 Directory inputs (`InputKind`, `accepts_directory`)

Every input seam keys off `Path(filename).suffix`; a folder has none. For
directory-unit conversions (issue #98 Phase 2, makeps3iso folder→iso) the seam
is an `InputKind` enum (defined in `models.py` so `ConversionJob` can type its
field without an import cycle; re-exported from `services.tools.spec`) +
`ModeSpec.input_kinds` (default `{FILE}`, zero behavior change), a
`ToolPlugin.accepts_directory(path)` predicate (default `False`; the directory
analogue of `ext in input_extensions`), and `registry.tools_for_directory(path)`.
The per-source-type detector for PS3 lives in `services/ps3.py` (requires the
`PS3_GAME/` disc/JB layout, rejecting bare installed-game folders).

The first user, **`MakePs3IsoTool`** (`folder_to_iso`, the only
`InputKind.DIRECTORY` mode), is wired end-to-end:

- **Tool/service.** `MakePs3IsoTool.accepts_directory` runs `ps3.is_ps3_iso_source`;
  `makeps3iso_service` shells out via `SubprocessRunner.run(["makeps3iso", folder,
  out.iso], …)`, derives the output from the folder's **normalized basename**
  (`<folder>.iso`, never `.iso` from a trailing slash), removes a partial ISO on
  cancel/failure, and runs a light **PARAM.SFO `TITLE_ID` readback** from the
  built ISO (reusing the shared `disc_id.read_iso_file` ISO 9660 reader) as an
  advisory check — `supports_delete_on_verify=False`, so a curated source folder
  is never auto-deleted. Before queuing the native packer, `plan_job` also walks
  the whole source tree with `utils.path_utils.is_safe_directory_tree`: the root
  is `lstat`ed after stripping only trailing separators and `.` components
  (interior `..`/symlinked ancestors are left intact, so the kernel resolves
  them and `lstat` reports the true final entry — a symlinked root cannot hide
  behind a trailing `/`, `/.`, `/./`, or a `..`-cancelled symlinked ancestor)
  and confined to a configured volume, then every
  entry is `lstat`ed and any symlink or non-regular entry is rejected. Because
  `os.walk(followlinks=False)` never descends through a link, the surviving
  entries are all genuine children of the confined root — no per-entry resolve
  is needed — so makeps3iso cannot dereference a link and embed files outside
  the volume boundary. The directory job is also **canonicalized to the resolved
  real source path** (`os.path.realpath`) before being queued, so makeps3iso is
  handed a symlink-free path: a concurrent swap of a symlinked *ancestor*
  component after validation cannot retarget the native reader to an unchecked
  tree. (A symlinked *root* is still rejected outright, since the safety walk
  runs on the original submitted path.)
- **Job model.** `ConversionJob.input_kind: InputKind` is threaded end-to-end
  (derived from the mode spec at queue time, serialized to a string only at the
  API/persistence edge). The generic `_process_job` flow already handles a
  directory source — the archive-extract branch is gated on `"::"`, which a
  folder never carries — so no directory-specific convert branch is needed.
- **Listing + search.** `files.py` annotates convertible directory rows
  (`_detect_directory_outputs` → `registry.tools_for_directory`) with
  `convertible_by` + the sibling `.iso` output in **both** `scan_directory` and
  the recursive search `_scan` (the latter emits the folder as a job unit and
  does not recurse past it, so it's discoverable / batch-selectable).
- **Lock manager.** A directory job protects its whole subtree two ways:
  - *Filesystem mutation* (rename / delete / delete-on-verify cleanup): the
    files-route `_assert_path_not_in_use` (→ `find_active_job_for_path`) rejects
    a rename or delete of any path that is a **descendant** of an active
    `InputKind.DIRECTORY` job's source, and delete-on-verify cleanup applies the
    same descendant check via `_is_path_in_use_by_other_job`. (New-job *output*
    collisions are handled separately by `check_output_conflicts` in `plan_job`,
    not these helpers.)
  - *Runtime* (concurrent conversions): the lock manager exposes directory-lock
    APIs — `acquire_dir_lock` / `release_dir_lock` plus the read-only
    `is_within_locked_dir` / `dir_lock_would_conflict`. `acquire_dir_lock` takes
    an `fcntl` lock on the **directory path itself** (so the exact same path is
    cross-process exclusive) and records the resolved path in an in-memory
    `_dir_locks` set; **subtree containment is enforced in-process** — a per-file
    `acquire_lock` checks `_dir_locks` and contends on a child output, and the
    code notes this is sound because `MAX_CONCURRENT_JOBS` concurrency all lives
    in one process (it is *not* a cross-process subtree lock — a child output in
    a second process hashes to a different lock file). A directory job holds the
    lock for its whole run. When `MAX_CONCURRENT_JOBS > 1`, a job whose
    input/output falls inside a locked subtree is **deferred, not failed**:
    `_process_job` releases the concurrency slot (and any output lock taken) and
    `_schedule_dir_lock_requeue` re-queues it after a fixed short delay, so it
    runs once the folder job releases the lock — a `Set[asyncio.Task]` holds
    strong refs so a requeue task can't be GC'd mid-sleep. (A *new* submission
    whose output is already inside the locked subtree never reaches this path: it
    is rejected at plan time by `check_output_conflicts`, which reads the dir lock
    through `check_file_status`.) All containment comparisons resolve
    symlinks via `os.path.realpath`, so a symlinked path into a locked folder
    can't bypass them; resolution is gated on a dir lock actually being held to
    keep the hot listing path cheap. `_plan_directory_job` additionally rejects a
    derived output that resolves inside the source or outside the configured
    volumes, so a folder-at-volume-root build can't write a sibling `.iso`
    outside all volumes or ingest its own in-progress ISO.
- **Plan + UI.** `plan_job` branches on `InputKind.DIRECTORY in spec.input_kinds`
  (validate `isdir` + the detector, normalized basename for output + display
  name). The frontend mode carries `inputKinds: ['directory']`; a *convertible*
  directory row becomes checkbox-selectable (`fileBrowser._isSelectable` /
  `conversion.allowsInputEntry`) while the name-click still navigates — a plain
  directory stays navigation-only.

- **4 GB FAT32 split (`-s`).** An opt-in per-job toggle exposes makeps3iso's
  split mode for FAT32 targets. `split` rides the shared `ToolPlugin.convert`
  contract beside `compression` (file tools ignore it) and threads
  request → `create_job`/`create_jobs_atomic` → `ConversionJob.split` →
  `convert(split=…)`; the frontend gates the checkbox on a `supportsSplit` mode
  flag. The split is **size-dependent and its part names are unknown until the
  build finishes**: makeps3iso only splits past ~4 GB (`0x1FFFE0` sectors),
  renaming the first part to `<output>.iso.0` and writing `.1`/`.2`/…, but emits
  a single `<output>.iso` below the threshold. So the service never assumes a
  shape — `split_parts()` probes the base then `.0`/`.1`/… post-build; the
  `TITLE_ID` readback reads the first part; failure cleanup unlinks the base and
  every numbered part. `_plan_directory_job` counts an existing split set as an
  output collision (RENAME steps past it via `get_unique_ps3_iso_output_path`),
  and `files.py` folds a set into one logical listing entry
  (`_fold_split_iso_entries`: name `<base>.iso`, `path` = the `.0` part,
  summed size, `FileEntry.split_parts` = count, no convertible/verify
  affordances — it's a final deliverable, not a source). The recursive *search*
  path only emits convertible sources, so split parts never surface there.
  Lifecycle completeness across the multi-file model — every site below
  enumerates the extra files from the `companion_outputs` hook (§ the plugin
  contract) rather than re-encoding the per-mode suffix, the same single source
  extractcd's `.cue` + `.bin` flows through:
  - **Conflicts:** `check_output_conflicts` enumerates the set via the owning
    tool's `companion_outputs` (no per-mode `mode ==` branch), so the
    `/jobs/check-duplicates` preflight and the plan agree a set occupies the
    target; `get_unique_output_path(base, mode)` reuses it to step a rename past
    the whole set.
  - **Overwrite:** `JobManager._clear_existing_output` removes the prior output
    (single `.iso` **or** the whole split set) **only when `allow_overwrite` was
    granted** — so a set that appears after planning is never deleted out from
    under a skip/rename decision (the per-path `acquire_lock` only sees the bare
    name). The sweep is **tool-neutral**: it asks the owning plugin for
    `overwrite_targets()` and applies the same validate-then-unlink pass to
    every mode, file or directory. `MakePs3IsoTool` overrides that hook to
    return the base plus every numbered part (via the service's
    `output_artifacts`, which also backs its failure cleanup so the two can't
    drift) — wider than `companion_outputs`, which reports only the finished
    set. This used to be an `input_kind == DIRECTORY` branch calling
    `makeps3iso_service.remove_outputs` directly, which meant a *second*
    directory-input tool would have had its outputs swept by makeps3iso's
    part logic.
  - **Completed size:** the completion block sums `output_path` plus
    `companion_outputs()`, so a split build (whose numbered parts replace the
    bare `.iso`) and an extractcd `.cue` + `.bin` both report a real
    `output_size` instead of `None`.
  - **Stall detection:** widened via `SubprocessRunner.run(output_growth_paths=…)`
    to the summed size of the set, so a split build isn't killed as stalled once
    the base is renamed away.
  - **In-flight protection:** `_split_output_blocks` marks a running split job's
    numbered parts in-use in `find_active_job_for_path` /
    `_is_path_in_use_by_other_job`, so a part can't be renamed/deleted mid-write.
  - **Row actions:** `RowActionsMenu` disables single-path rename/delete and
    `FileList.openBulkDelete` excludes a folded set (its `path` is only the `.0`
    part, so a single-path op would orphan `.1+`).

makeps3iso (GPL-3.0) is built unmodified from a pinned `bucanero/ps3iso-utils`
commit in the multi-stage `Dockerfile`, mirroring maxcso.

### 3.3.5 Metadata freshness (`chd_metadata_store.get_fresh_metadata`)

A consumer that reads cached CHD metadata to act on it — `ChdmanTool.embedded_hashes`
matches a CHD's stored header/data SHA1 against a DAT without re-hashing the file —
needs the hashes and the "is this still the current file?" verdict to describe **one**
observation. `get_metadata()` followed by `is_stale()` does not: two separate awaits,
each its own stat + row read, so the hashes can reflect one mtime while the staleness
verdict reflects another (a conversion rewriting the CHD in between). That is the single
seam every freshness-gated metadata read MUST go through instead.

`get_fresh_metadata(path)` returns the cached `metadata_json` **only if it still
describes the file**, in one `run_in_threadpool` call with no `await` between the row
read and the stat. The ordering matters: it reads the **row first, then re-stats**, and
returns the row only when the file's current mtime equals the row's captured mtime. The
writer (`_set_metadata_sync`) always stats then stores `(metadata_json, mtime)` together,
so the row's mtime lags the file; reading the row first and comparing against a *later*
stat means a concurrent rewrite (new bytes on disk, row not yet re-scanned) shows
`mtime != row.mtime` and the stale hashes are refused. A stat-**then**-read ordering would
re-use an already-captured old mtime that can still match the old row for a file that was
replaced in the window — the TOCTOU this seam exists to close. On any miss (no row, file
gone, or changed) it returns `None`, and the caller falls back to a file-level SHA1, which
is valid for any DAT that indexes the container bytes. New cache/tool code that needs
freshness-gated metadata must call this rather than reintroducing the two-read pattern.

### 3.3.6 Re-run fast path (`JobManager._output_already_verified`)

`_process_job` recognizes a prior success before it re-spawns the converter: a
re-queued job whose target artifact is already on disk **and provably the exact
result of the current request** completes as a no-op instead of re-running the
tool (issue #184, site 1). The check runs at the top of the conversion `try`
block — before archive extraction and before `_clear_existing_output` — so the
existing verified output is neither re-extracted-from nor deleted; on a hit the
job jumps straight to `COMPLETED` (progress 100, size via the shared
`_compute_output_size`, a `complete` event with `verified=True`,
`source_deleted=False`). A cancel is honored on both sides of the awaited
lookups: checked before, and re-checked after `_output_already_verified` returns
(a cancel that lands mid-lookup raises `ConversionCancelled` rather than
completing the no-op).

**The evidence is a `produced_meta` snapshot, not a bare "verified" flag.** A
plain verification record proves only that *some* file at that path passed
integrity verification at *some* time — not that the current file is that
artifact, nor that it matches the current request's output-shaping settings, nor
that a multi-file source (a `.cue`/`.gdi` and its tracks) is unchanged. So the
fast path trusts **only** records carrying a `produced_meta` blob, written by a
prior **delete-on-verify** conversion right after its verify passed (the
`verifications.produced_meta` JSON column, Alembic migration `0003`). A manual
`/info` verify records **no** meta and never qualifies. `_build_produced_meta`
captures, off the event loop:

- `mode`, `compression`, `split` — the producing conversion's output shape.
- `source` — a stat fingerprint (`{realpath: {size, mtime_ns, inode, device}}`)
  of the **complete** source set. It is the job's **pre-conversion** delete
  snapshot (captured at planning, before the converter read the source),
  re-validated against the on-disk source at record time: if the source changed
  between planning and verify — e.g. mutated while the converter was running —
  the snapshot no longer matches and **no** meta is recorded, so a later
  re-queue can't no-op against an output built from now-stale bytes.
- `output` — a `{size, mtime_ns, inode, device}` fingerprint of the produced
  artifact (taken after the disc-ID embed, so it reflects the final bytes).

The `inode`/`device` fields are the same four stat fields the delete-safety
snapshot trusts to authorize an *irreversible* delete (`build_delete_snapshot`),
so a reuse — a strictly less destructive decision — is held to no weaker a bar:
a copy-restore lands on a new inode, a moved mount on a new device, and either
forces a re-convert.

`_produced_meta_matches` (off the event loop) then admits the no-op only when
**all** hold: `mode`/`compression`/`split` equal the current request's, the
current output fingerprint equals the recorded one (so a replaced/corrupted file
can't pass), and the current complete-source-set fingerprint equals the recorded
one (so a changed track — not just the descriptor — forces a re-convert).
`_output_already_verified` additionally excludes directory (folder→ISO) jobs
(split-set outputs) and `delete_on_verify` *requests* (they must run their
guarded source deletion), and treats any store hiccup, missing record, absent
`produced_meta`, or un-fingerprintable source (archive members, unsafe/missing
tracks) as "no match" — always falling through to a full re-convert rather than
risk a surprising output.

**Known limitation.** `compression` is stored as the request declared it, so
`None` means "the tool's default." A tool that resolves its default from mutable
settings (e.g. nsz's `NSZ_COMPRESSION_LEVEL`) could, after an operator changes
that setting *and* a prior delete-on-verify run was cancelled after verify,
deliver the earlier-default artifact for a `None`-compression re-queue. That is a
compression-*level* difference on an otherwise-correct, verified artifact of the
exact source — not a content mismatch — and is accepted rather than have every
tool persist its resolved defaults.

### 3.4 `registry.py`

```python
class ToolRegistry:
    def __init__(self): self._tools: dict[str, ToolPlugin] = {}; self._by_mode: dict[str, ToolPlugin] = {}
    def register(self, tool: ToolPlugin) -> None:
        self._tools[tool.id] = tool
        for m in tool.modes:
            if m.mode in self._by_mode: raise ValueError(f"duplicate mode {m.mode}")
            self._by_mode[m.mode] = tool
    def all(self) -> list[ToolPlugin]: return list(self._tools.values())
    def get(self, tool_id: str) -> ToolPlugin: return self._tools[tool_id]
    def for_mode(self, mode: str) -> ToolPlugin: return self._by_mode[mode]
    def spec(self, mode: str) -> ModeSpec: return self.for_mode(mode).spec(mode)
    def mode_specs(self) -> list[ModeSpec]: return [m for t in self._tools.values() for m in t.modes]
    def convertible_extensions(self) -> tuple[str, ...]:   # sorted (issue #183)
        return tuple(sorted(set().union(*(t.input_extensions for t in self._tools.values()))))
    def tools_for_input(self, filename: str) -> list[ToolPlugin]:
        ext = Path(filename).suffix.lower()
        return [t for t in self._tools.values() if ext in t.input_extensions]
    def tool_for_verify(self, path: str) -> ToolPlugin | None:
        ext = Path(path).suffix.lower()
        return next((t for t in self._tools.values() if ext in t.verify_extensions), None)
    # Discovery helpers (issue #131): the union of every tool's produced /
    # verifiable extensions drives the registry-driven library scan, so a new
    # tool's outputs become scannable for free.
    def output_extensions(self) -> tuple[str, ...]:
        return tuple(sorted(set().union(*(t.output_extensions for t in self._tools.values()))))
    def verify_extensions(self) -> tuple[str, ...]:
        return tuple(sorted(set().union(*(t.verify_extensions for t in self._tools.values()))))
    def scannable_extensions(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.output_extensions()) | set(self.verify_extensions())))
```

> **Ordering (issue #183):** the extension-union helpers return a **sorted
> `tuple`**, not a `frozenset`. Hash-seeded set iteration order varies across
> processes, so any consumer that serializes an extension list (or uses it as an
> ordered filter) would churn run-to-run; sorting at the seam makes every
> consumer deterministic for free. The few consumers that still need set algebra
> (`registry.scannable_extensions() - ARCHIVE_EXTENSIONS`, the archive
> listable/gate unions) wrap the result in `set(...)` / `frozenset(...)` locally.

`__init__.py`:

```python
from config import settings
from .registry import ToolRegistry
from .chdman import ChdmanTool
from .dolphin import DolphinTool
from .z3ds import Z3dsTool

registry = ToolRegistry()
registry.register(ChdmanTool(settings.chdman_path))
registry.register(DolphinTool(settings.dolphin_tool_path))
registry.register(Z3dsTool(settings.z3ds_compressor_path))
```

### 3.5 Generic info/verify route factory (`routes/info.py`)

The three verify endpoint trios collapse into one factory driven by the
registry. Pseudocode:

```python
def register_verify_routes(router, tool: ToolPlugin):
    base = tool.id                                  # "chd" stays "chd" via alias map
    @router.get(f"/{base}-verify")
    async def _verify(path: str = Query(...)):
        _guard(path, tool.verify_extensions)
        token = await _acquire_verify_lane_or_429()
        try:
            r = await tool.verify(path)
            if r.get("valid"): await verification_store.mark_verified(path)
            return r
        finally: token.release()
    @router.get(f"/{base}-verify/events")
    async def _verify_events(path: str = Query(...)):
        return _sse_from_verify_stream(tool, path)   # the shared adapter
    @router.post(f"/{base}-verify-batch/events")
    async def _verify_batch(req: BulkVerifyRequest):
        return _sse_batch_from_verify_stream(tool, req.paths)
```

`_sse_from_verify_stream` / `_sse_batch_from_verify_stream` contain the
queue/done/2-second-heartbeat machinery that is copy-pasted ~6× in `info.py`
today. Endpoint **paths stay identical** (a `tool_id → url_prefix` alias map
keeps `chd`, `dolphin`, `z3ds`), so the frontend and API are unchanged.

#### Tool availability (`is_ready` / `GET /api/tools`)

`GET /api/tools` splits `registry.all()` into `{"available", "unavailable"}` by
awaiting each plugin's `is_ready()`; `App.svelte` feeds the result to
`ui.applyToolAvailability()`, which hides unavailable tools everywhere (the
sidebar and dashboard both derive from `registry.all()` minus `ui.hiddenTools`,
and the active tool falls back to a visible one).

The readiness check belongs to the plugin, not the route. `BaseTool.is_ready`
returns `True`; `NszTool` overrides it with a threadpooled
`keys_available()` because Switch content is encrypted and the operator must
supply their own `prod.keys` (§16 of `ADDING_PLATFORMS_AND_TOOLS.md`). The
route previously computed `nsz_service.keys_available()` itself and branched on
`tool.id == "nsz"`, so no *other* tool could ever be gated — a second
secret-dependent tool would have been advertised in the UI while being unable
to run.

### 3.6 Generic `FileEntry` outputs (`models.py`, `routes/files.py`)

```python
class OutputStatus(BaseModel):
    tool_id: str
    exists: bool          # finished file present
    ready: bool           # present and not mid-conversion
    path: str | None = None

class FileEntry(BaseModel):
    ...
    convertible_by: list[str] = []           # tool ids that accept this input
    outputs: list[OutputStatus] = []         # detected sibling outputs
    verifiable_by: list[str] = []            # tool ids whose Verify/Info apply here
    # legacy fields kept during migration (see Phase 7), removed at the end
```

`files.py` `scan_directory`/`search_files` stop hardcoding three flag blocks
and instead loop `for tool in registry.all()` calling a tool-provided
`detect_output(item_path)`.

#### Per-file verify gate (`verifies_path` / `verifiable_by`)

`verify_extensions` is a coarse, extension-level claim. Most tools verify
every file with a matching extension, but a tool can claim a **broad container
extension while only handling a narrow subset** of it — `romz` claims `.7z`/
`.zip` (its extract-mode inputs) yet its Verify/Info only apply to the
single-ROM archives it produced, not to an arbitrary multi-file `.zip` the user
happens to have. Gating the frontend's Verify/Info row-actions on the
extension alone would offer the affordance where the tool can't actually use it
(issue #146).

The plugin contract closes that gap with `ToolPlugin.verifies_path(path) ->
bool`: the per-file refinement of `verify_extensions`. `BaseTool` defaults it to
the plain extension match, so existing tools are unaffected; `RomzTool`
overrides it to inspect the archive's members (exactly one handheld-ROM member —
the same invariant verify/extract enforce, via
`RomzService.is_single_rom_archive`). The registry exposes
`tools_verifying_path(path)` (the per-file companion to `tool_for_verify`), and
`routes/files.py` materializes the result into the tool-neutral
`FileEntry.verifiable_by` list for on-disk files and archive containers. The
frontend (`RowActionsMenu`) gates Verify/Info on `verifiable_by` when present,
falling back to the registry's extension match for rows the listing doesn't
annotate (e.g. archive members, which can't be verified in place). `verifies_path`
may do disk I/O (archive inspection), so call it off the event loop — the
`files.py` scans already run in a threadpool.

Two consumer-side notes keep the gate airtight:

- **Frontend precedence.** The registry's extension match (`toolForVerifyPath`)
  stays the source of truth, because it encodes deliberate UX exclusions the
  backend's broader `verify_extensions` don't — e.g. Dolphin's
  `verify_extensions` include `.iso`, but the frontend intentionally omits
  `.iso` from `DOLPHIN_VERIFY_EXTS` so CD/DVD ISOs aren't routed to a Dolphin
  verify that fails. `verifiable_by` may therefore only **narrow** that match,
  never broaden it. This rule lives in one place — `registry.verifyToolForPath`
  (resolve the extension match, then drop it when a present `verifiable_by`
  excludes it) — and every verify entry point uses it: the per-row menu
  (`RowActionsMenu`), the "Verify selected" gate (`FileList`), and the bulk
  target picker (`BulkVerifyModal`), so a non-single-ROM archive can't slip a
  romz Verify through the batch path either.
- **Output targets.** A source row can verify *from* an existing sibling output
  (`entry.outputs[].path`), so that path needs the same gate. Rather than
  re-listing the output inside the row, the producing tool's `detect_output`
  validates the candidate before claiming it: `RomzTool.detect_output` only
  reports a finished `.7z`/`.zip` as a romz output when it is a genuine
  single-ROM archive (a mid-conversion placeholder still badges as
  in-progress). A coincidental multi-file `Game.gba.7z` thus never enters
  `outputs`, so the verify-from-output flow can't offer romz Verify on it.

### 3.7 Frontend descriptor (`src/lib/tools/registry.js`)

> **Status: implemented and extended.** The original §3.7 sketched a minimal
> descriptor on `static/js/tools.js`. The legacy Preact frontend was retired
> in favor of a Svelte 5 + Vite SPA (see README §"Frontend Development"), and
> the registry now owns every tool fact: verify URLs derive from
> `verifyPrefix`, per-tool `groups` map carries group labels (no hardcoded
> switch), `defaultMode` replaces the old chdman special-case, and optional
> `glyph` / `accent` give the sidebar / dashboard a visual escape hatch.
> See `src/lib/tools/registry.js` for the source of truth.

```js
// src/lib/tools/registry.js (abridged, see file for full schema)
export const TOOLS = [
  { id: 'chdman', label: 'CHDMAN', hint: '…',
    verifyPrefix: '',                                    // URL segment, '' → /api/verify
    sourceExts: ['.gdi','.iso','.cue','.bin'], verifyExts: ['.chd'],
    modeGroups: ['create','extract','copy'],
    groups: { create: 'Create', extract: 'Extract', copy: 'Copy' },
    defaultMode: 'createcd',
    glyph: 'CD', accent: 'var(--badge-cd)',
    modes: [/* ModeSpec rows mirroring app/services/tools/spec.py */],
    getInfo: api.getCHDInfo, verify: api.verifyCHD, verifyBatch: api.verifyBatchCHDs,
    productPath: (p) => p.replace(/\.[^.]+$/, '.chd') },
  { id: 'dolphin', verifyPrefix: 'dolphin', /* … */ },
  { id: 'z3ds',    verifyPrefix: 'z3ds',    /* … */ },
];
```

`getPrimaryToolLabel`/`getPrimaryToolHint`/`getFilterOptions`/`MODE_GROUPS`/
`CHDInfoModal` routing/verify-batch routing (all in `app.js`) become lookups
into `TOOLS` instead of `if (tool === 'dolphin') …` chains.

**Parity guard (`tests/test_frontend_parity_186.py`).** `registry.js` is a
hand-maintained mirror of the backend `ModeSpec`s, and there is no shared build
step, so a drift in `allows_archive_input` / `supports_delete_on_verify` /
`input_extensions` / `output_ext` / `kind` would silently make the UI offer a
job `plan_job` rejects ("0 queued") or fail on submit. The test evaluates
`registry.js` with Node and asserts its mode rows match `registry.mode_specs()`
field-by-field (skipped when Node is unavailable). `tool_id` is excluded: the
synthetic `cso_to_chd` chain mode is owned by the `chain` tool on the backend
but grouped under `cso` in the UI. The deliberate `.iso`-not-in-Dolphin-verify
divergence lives on `verifyExts` (a verify-routing fact), not on a `ModeSpec`
field, so it is outside the guard's scope.

**Registry-derived frontend fact lists (issue #186, site 3).** Two presentation
surfaces used to re-type extension/mode facts the registry already owns, so a
new tool or mode silently missed them:

- `src/lib/util/fileIcon.js` builds its disc/game extension buckets from
  `registry.all()` via a small `TOOL_MEDIA` map — the one presentation fact the
  registry can't express (whether a tool's media reads as an optical disc or a
  cartridge/handheld "game"). `.chd` keeps its own Disc3 glyph and the
  handheld-ROM archives (`.7z`/`.zip`) get the archive glyph.
- `src/lib/components/views/HelpView.svelte` generates its per-mode reference
  table from `registry.all()` (one section per tool + mode-group, in registry
  order) via `helpModeSections()`. The only hand-authored content lives in
  `src/lib/tools/helpModes.js`: `MODE_BLURBS` (the human one-liner per mode) and
  `MODE_OUTPUT` (a display override for reversible/companion outputs the
  registry stores as a single or `null` `outputExt`).

A single Node-evaluated guard, `tests/test_frontend_registry_derives_186.py`,
fails if either drifts: a filterable extension with no icon bucket, an
unclassified tool, a mode with no blurb, a stale blurb/override key, or a mode
that would render a blank Output cell.

---

## 4. The payoff: what a new tool looks like *after*

Before: ~20 files (see `ADDING_PLATFORMS_AND_TOOLS.md` §3). After:

1. `app/services/tools/nszip.py`, one `BaseTool` subclass with `modes`,
   `_build_command`, `_parse_progress`, `verify_stream`, `info`, `info_model`.
2. `app/services/tools/__init__.py`, one `registry.register(NszipTool(...))`.
3. `app/config.py`, one `nszip_path` Field.
4. `static/js/tools.js`, one entry in `TOOLS`.
5. `Dockerfile`, install the binary (irreducible).
6. tests + docs.

Dispatch in `job_manager`, `convert`, `files`, `info`, **no edits**, because
they iterate the registry. That is the whole point.

---

## 5. Migration sequence

Each phase is independently shippable, preserves behavior, and ends green
(`pytest -q tests`, with special attention to `test_mode_parity_fixes.py`,
`test_z3ds_routes.py`, `test_dolphin_routes.py`, `test_chdman_annotations.py`).
Phases are ordered so the **registry exists first** and consumers migrate one
at a time. Nothing here changes the wire API until Phase 9 (optional).

### Phase 0: Scaffold the registry around existing services (no behavior change)
- Add `app/services/tools/` with `spec.py`, `base.py`, `registry.py`.
- Write `ModeSpec` rows for **every** current `ConversionMode` value.
- Create thin `ChdmanTool`/`DolphinTool`/`Z3dsTool` that **delegate** to the
  existing `chdman_service`/`dolphin_tool_service`/`z3ds_compress_service`
  singletons (no logic moved yet).
- Add `registry` singleton.
- **Tests:** new `tests/test_tool_registry.py`, assert every
  `ConversionMode` resolves to exactly one tool; assert
  `registry.spec(m).supports_delete_on_verify` matches today's
  `supports_delete_on_verify(m)` for all modes (characterization).
- Risk: ~none (additive).

### Phase 1: Replace prefix checks with `ModeSpec` in `convert.py`
- Swap `mode.startswith(...)` / `== "z3ds_compress"` and
  `supports_delete_on_verify` for `registry.spec(mode)` field reads.
- Keep the duplicated single/batch structure for now.
- **Tests:** existing `test_mode_parity_fixes.py` + convert route tests must
  pass unchanged.
- Risk: low; pure substitution guarded by characterization tests from Phase 0.

### Phase 2: Route output-path resolution through the registry
- `convert._get_output_path` (`:100`) → `registry.for_mode(mode).output_path(...)`.
- `job_manager._queue_job_locked` (`:139`) → same.
- Implement `output_path()` on each tool by calling the existing
  `get_output_path_for_mode` / `get_chd_path`.
- **Tests:** add path-equivalence tests (old vs new) for a matrix of modes ×
  archive/non-archive × output_dir/none.
- Risk: low-medium (path edge cases: `extractcd` `.cue`, archive stems). The
  equivalence test is the safety net.

### Phase 3: Route conversion + verify dispatch through the registry
- `job_manager._process_job` `_convert_service` ladder (`:1444`) →
  `registry.for_mode(job.mode.value)`.
- Verify branch (`:1556`, `:1591`) → `plugin.verify(output_path)` uniformly
  (drops the z3ds special-case).
- Disc-ID embed now lives in `ChdmanTool.post_convert`; `_process_job` calls
  `registry.for_mode(mode).post_convert(...)` generically (default no-op) and
  `ChainTool` routes its final step through the same hook (done, #181).
- **Tests:** job-pipeline tests (`test_external_job_api.py` neighbors), plus a
  conversion smoke test per tool with the binary stubbed.
- Risk: medium (hot path). Ship behind close review; the behavior is a 1:1
  re-route.

### Phase 4: Deduplicate single vs batch in `convert.py`
- Extract `plan_job(file_path, mode, output_dir, duplicate_action, …) -> JobPlan|Skip`.
- `create_job` and `create_batch_jobs` both call it.
- **Tests:** `test_mode_parity_fixes.py` becomes near-trivial (one code path);
  keep it as a regression guard.
- Risk: medium; well-covered by the existing parity suite.

### Phase 5: Extract `SubprocessRunner`; slim the three services
- Add `runner.py`; refactor `convert()` in each tool to use it.
- Move the real `chdman`/`dolphin`/`z3ds` logic *into*
  `services/tools/{chdman,dolphin,z3ds}.py` (Phase 0 left them delegating).
  The old `services/chdman.py` etc. become shims re-exporting the singleton
  for any stragglers, or are deleted once imports are updated.
- **Tests:** service-level tests (`test_z3ds_verification_service.py` etc.),
  plus a cancel-path test (cancel_event → `ConversionCancelled` + partial
  output cleaned).
- Risk: medium-high (subprocess timing/cancel/stall). Do tool-by-tool, not all
  at once; keep `ConversionCancelled` in its current import location to avoid
  breaking `job_manager`'s `except`.

### Phase 6: Generic info/verify routes from the registry
- Replace the three endpoint trios in `info.py` with the factory (§3.5) +
  shared SSE adapters. Maintain the `tool_id → url_prefix` alias map so paths
  (`/api/chd-verify`? no, keep `/api/verify`, `/api/dolphin-verify`,
  `/api/z3ds-verify`) are byte-for-byte identical.
- **Tests:** `test_z3ds_routes.py`, `test_dolphin_routes.py`, and CHD verify
  route tests must pass without edits (same URLs, same events).
- Risk: medium; the SSE event names/shapes are load-bearing for the frontend,
  assert them explicitly.

### Phase 7: Generic `FileEntry` outputs + registry loop in `files.py`
- Add `convertible_by` / `outputs`; populate from `registry.all()`.
- Keep legacy booleans (`has_chd`, `dolphin_ready`, …) populated **in
  parallel** so the frontend keeps working.
- **Tests:** `test_volume_discovery.py` + a listing test asserting both legacy
  flags and new `outputs` agree.
- Risk: low (additive on the model).

### Phase 8: Frontend descriptor table
- Add `static/js/tools.js`; migrate the ~10 `app.js` branch sites + `api.js`
  to consume it.
- Switch `FileList` badges to read `entry.outputs`/`convertible_by`.
- **Tests:** manual UI pass (no JS test harness in repo), browse, convert,
  verify, info modal, for each tool; confirm SSE progress + badges.
- Risk: medium (no automated FE tests). Verify in a browser per
  `ADDING_PLATFORMS_AND_TOOLS.md` §15 (no build step, edit JS directly).

### Phase 9: Cleanup (optional, behavior-preserving)
- ~~Remove the now-unused legacy `FileEntry` booleans once the frontend reads
  `outputs` exclusively.~~ **Done (issue #186, site 6).** The ~23 legacy
  per-tool flags (`has_*` / `*_ready` / `*_convertible` / `*_path` and the bare
  `convertible`) are gone from `FileEntry`, the `_legacy_output_fields` helper
  and the per-archive `has_chd` special-case are removed from `routes/files.py`,
  and the two remaining frontend fallbacks (`FileRow` `convertibleBy`,
  `fileBrowser` `has_chd` merge) now read `convertible_by` / `outputs` /
  `verifiable_by` exclusively. The per-archive "converted" badge derives from
  the registry-driven `archive_has_output` count.
- The verify-route factory (`routes/info.py`) is already registry-driven with no
  per-tool branching; its six explicit `register_verify_routes(...)` calls are
  retained deliberately to preserve the module-level `verify_*` handler names the
  route tests call directly. Folding them into a `registry.all()` loop would
  require moving the per-tool prefix/detail strings onto the plugin contract and
  rewriting those tests, for no behavior change, so it stays as-is.
- Delete the old `services/{chdman,dolphin,z3ds_compress}.py` shims; update
  imports.
- Consider deriving `config` binary paths from the registry.
- **Tests:** full suite + manual UI pass.

---

## 6. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Hot-path regression in `_process_job` (Phase 3/5) | 1:1 re-route; characterization tests from Phase 0; tool-by-tool rollout |
| Subprocess cancel/stall behavior drift (Phase 5) | Keep `ConversionCancelled` import location; add explicit cancel + stall tests before refactoring |
| SSE event-shape drift breaks UI (Phase 6) | Snapshot-test event names/payloads; keep identical URLs via alias map |
| No automated frontend tests (Phase 8) | Gate on a manual browser checklist; ship FE last, after backend is stable |
| Output-path edge cases (`extractcd` `.cue`, archive stems) (Phase 2) | Old-vs-new path equivalence matrix test |
| Over-abstraction | Stop at an in-process registry; no dynamic loading; `BaseTool` only hoists code that is *already* duplicated 3× |
| Scope creep / long-lived branch | Each phase is a separate, shippable PR behind the existing test suite |

## 7. Testing strategy summary

- **Characterization first (Phase 0):** lock current behavior (mode→tool,
  capability flags, output paths) into tests *before* moving code.
- **Equivalence tests** for the mechanical re-routes (paths, dispatch).
- **Lean on existing suites:** `test_mode_parity_fixes.py` (single/batch),
  `test_*_routes.py` (endpoints + SSE), `test_*_service.py` (subprocess),
  `test_volume_discovery.py` (listing).
- **Manual UI checklist** for Phase 8 (no JS harness).
- Every PR: `ruff check app tests && pylint app && pytest -q tests`
  (+ `hadolint Dockerfile` if touched), per `ADDING_PLATFORMS_AND_TOOLS.md` §11.

## 8. Estimated impact

| Area | LOC delta (rough) |
|------|-------------------|
| New `services/tools/` (registry, spec, base, runner) | +400 |
| `services/{chdman,dolphin,z3ds}` after `SubprocessRunner` | −300 (dedup) |
| `routes/info.py` after verify factory | −600 (dedup) |
| `routes/convert.py` after single/batch merge + spec checks | −120 |
| `routes/files.py` after registry loop | −60 |
| Frontend `app.js`/`api.js` + new `tools.js` | net ~0 (moved, not added) |

Net: a smaller, flatter codebase where the cost of the *next* tool drops from
~20 files to ~5.
