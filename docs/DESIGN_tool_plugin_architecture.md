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
    supports_delete_on_verify: bool = False  # may this job delete its source?
    supports_verify: bool = False            # can its output be checked at all?
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

    # The file to verify once a job finishes, or None when the product cannot
    # be found. Nearly always `output_path`; a split makeps3iso build writes
    # `<iso>.0`/`.1`/… and no bare `.iso`, so verifying the planned path failed
    # a conversion that had worked. BaseTool returns `output_path`.
    def verify_target(self, output_path: str, mode: str) -> str | None: ...

    # Whether deleting the source is justified for *this* job's settings, not
    # just whether the mode offers the option (`supports_delete_on_verify`).
    # BaseTool returns True; a tool whose verification can be weakened by a
    # compression choice overrides it — jwud returns False for `noverify`,
    # whose structural WUX check proves the geometry but not the bytes. Asked
    # at both plan sites and again in `_process_job`, right before the delete.
    def delete_on_verify_is_safe(self, mode: str, compression: str | None) -> bool: ...

    def convert(self, input_path: str, output_path: str, mode: str, *,
                compression: str | None = None,
                cancel_event: asyncio.Event | None = None) -> AsyncGenerator[dict, None]: ...

    # Verify is bounded and cancellable on the same terms as convert (issue
    # #266): cancel_event is the job's own event, so Cancel stops the verifier
    # instead of being observed only after it finishes; a run cut short by it
    # returns {"valid": False, "cancelled": True} — it reached no verdict, so no
    # caller may record it as a verification failure or delete a source on it.
    async def verify(self, path: str, *,                             # {"valid","message"}
                     cancel_event: asyncio.Event | None = None) -> dict: ...
    def verify_stream(self, path: str, *,
                      cancel_event: asyncio.Event | None = None
                      ) -> AsyncGenerator[dict, None]: ...
    # Wall-clock bound for verifying this path (0 = unbounded). Default:
    # BaseTool resolves the shared size-scaled policy for `policy_owner`, so a
    # per-tool COMPRESSATORIUM_<OWNER>_VERIFY_TIMEOUT applies. Exposed so
    # job_manager can bound the whole verify() call — including a tool whose
    # verify spawns nothing (jwud's container walk, makeps3iso's PARAM.SFO
    # readback) — without re-deriving which knob that tool reads.
    async def verify_timeout(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> int: ...
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
        # `policy_owner or id`, never bare `id`: the owner keys the shared
        # nice/ioprio/timeout policy's per-tool overrides, and it differs from
        # the plugin id wherever the service kept its historical name
        # (dolphin/dolphin_tool, cso/maxcso). In the tree today each *service*
        # constructs its own runner with that owner as a literal.
        self._runner = SubprocessRunner(owner=self.policy_owner or self.id)

    # derived sets (no per-tool duplication)
    @property
    def input_extensions(self) -> frozenset[str]:
        return frozenset().union(*(m.input_extensions for m in self.modes))

    def spec(self, mode: str) -> ModeSpec:
        for m in self.modes:
            if m.mode == mode:
                return m
        raise KeyError(mode)

    # The SubprocessRunner "owner" this tool's service runs under, which keys
    # the shared nice/ioprio/timeout policy's per-tool overrides. NOT always
    # `id`: the plugin id is the routing/UI name ("dolphin", "cso") while the
    # owner is the historical service name ("dolphin_tool", "maxcso").
    policy_owner: str | None = None

    # Default: the shared size-scaled verify bound for this tool's owner.
    async def verify_timeout(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> int:
        return await resolve_verify_timeout(
            path, self.policy_owner, cancel_event=cancel_event,
        )

    # verify() is the same drain-verify_stream()-and-keep-the-terminal-event
    # wrapper in every service, so it lives once in subprocess_runner as the
    # shared `collect_verify(stream, fallback_message=...)` (it also carries the
    # "cancelled" flag through to the result).
    async def verify(self, path, *, cancel_event=None) -> dict:
        return await collect_verify(self.verify_stream(path, cancel_event=cancel_event),
                                    fallback_message="Verification failed")

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
tool's `convert()`. All nine conversion tools now delegate their streaming loop
here: `chdman`, `dolphin_tool`, `romz`, `makeps3iso`, `nkit2iso`, `jwudtool`
directly, and
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
                  cwd=None, output_growth_paths=None, mode=None,
                  nice_via_wrapper=False, env=None,
                  require_output=False) -> AsyncGenerator[dict, None]:
        """Spawn cmd, stream stdout, yield {"progress","message"}.
        Handles: nice/ionice wrap, stdbuf, PID tracking, deterministic \\r/\\n
        line buffering (CRLF/CR normalized to LF in one pass via
        `_split_stream_lines` so segmentation is a pure function of the byte
        stream, not chunk boundaries -- issue #183),
        stall timeout via compute_progress_stall_timeout, cancel-watcher
        (terminate->kill via the bounded `reap()` ladder), ConversionCancelled,
        non-zero exit -> RuntimeError(tail),
        final 100%. `parse_progress(line) -> int|None` is the only per-tool knob
        in the common path. `cwd` sets the working directory (romz runs `7z a`
        from the ROM dir).

        Opt-in seams, each defaulting off so the direct callers are unaffected:
        - `heartbeat` — dolphin's 2s "Converting... (Ns)" keep-alive.
        - `output_growth_paths()` — widen the stall probe to a set of files whose
          summed size grows even as filenames change mid-run (makeps3iso split).
        - `mode` — the conversion mode, used to look up an expected output/input
          size ratio in `SIZE_RATIOS` so the size-growth fallback (below) can
          emit a percentage as well as a message. Optional: a mode with no row
          still reports bytes and rate.
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

    async def reap(self, process, *, exit_timeout=_EXIT_GRACE) -> bool:
        """Bounded teardown for any spawned child. True if reaped, False if
        abandoned. Escalates: wait exit_timeout for a voluntary exit, then
        SIGTERM + grace, then SIGKILL + grace, then give up. A child blocked in
        uninterruptible I/O (D state) survives SIGKILL, so every wait must be
        bounded -- an unbounded one blocks the job forever and, at
        MAX_CONCURRENT_JOBS=1 (the default, which runs jobs inline in the
        dispatcher), every job queued behind it (issue #263). The single
        teardown path for run(), run_capture() and their finally blocks.
        """

    async def run_capture(self, cmd: list[str], *, timeout=None,
                          cancel_event=None, stderr_to_stdout=False,
                          nice_via_wrapper=False, env=None
                          ) -> tuple[int | None, bytes, bytes]:
        """One-shot counterpart to run(): buffered (returncode, stdout, stderr)
        for tools that need a result rather than streamed lines (info / header /
        embedded-hash extraction). Same PID tracking; races communicate()
        against cancel_event + timeout and terminates (TERM->KILL) on either,
        reporting returncode None to signal the abort. Used by
        `dolphin_tool.disc_hashes` so `dolphin-tool verify --algorithm sha1`
        (the Dolphin disc-hash source for `embedded_hashes`) aborts promptly
        when a scan/match job is cancelled. `nice_via_wrapper` / `env` mirror
        run()'s, for the tools that must avoid preexec_fn or need a private
        environment.
        """

    # --- verify: two shapes, no per-tool loops (issue #266) ---------------

    async def run_verify(self, cmd: list[str], *, path: str, parse_progress,
                         success_message: str, failure_message: str,
                         cancel_event=None) -> AsyncGenerator[dict, None]:
        """Streaming verifier (chdman, dolphin-tool): spawn, segment lines with
        the shared `_split_stream_lines`, yield {"type","progress","message"}
        and one terminal complete/error. Owns every bound the conversion path
        has — the size-scaled overall bound from `resolve_verify_timeout(path)`,
        the no-output stall bound, `cancel_event`, and the `reap()` ladder
        instead of a bare `process.wait()` (which on a D-state child never
        returns and freezes the queue). The post-EOF voluntary-exit grace is
        capped by whatever is left of the verify's own bound — and skipped
        entirely once a cancel has fired — so a verifier that closes its pipe
        without exiting cannot hold the lane past its deadline. `parse_progress` is the only per-tool
        knob; it replaced two near-identical ~120-line loops.
        """

    async def capture_verify(self, cmd: list[str], *, path: str,
                             success_message: str, cancel_event=None,
                             nice_via_wrapper=False, env=None,
                             start_message="Verifying integrity..."
                             ) -> AsyncGenerator[dict, None]:
        """One-shot verifier (maxcso --crc, nsz -V, 7z t): the same event shape
        around a single run_capture() with the same bound and cancel_event.
        run_capture reports both an abort and a timeout as returncode None, so
        the two are told apart by asking the cancel event which happened.
        """


# Module-level, not methods: cleanup runs after run() has already given up, and
# chains (which own no runner) need the same bound. See "Cleaning up partial
# output is bounded too" below.
async def remove_partial_output(*paths: str, discover=None,
                                label="partial output", timeout=None) -> bool:
    """Bounded unlink of every file a failed run may have left behind.
    True if the sweep finished, False if it timed out or failed. Raises only
    CancelledError. `discover()` enumerates further paths from inside the
    bounded worker, for a set only knowable by probing the disk.
    """

async def remove_partial_tree(path: str, *, label="work directory",
                              timeout=None) -> bool:
    """The same bound for a private work/scratch dir (rmtree, errors ignored)."""
```

`collect_verify(stream, *, fallback_message)` is the module-level reducer every
`verify()` uses to turn one of those streams into `{"valid","message"}`
(plus `"cancelled"`).

Per-tool `convert()` becomes ~15 lines: build argv, then
`async for u in self._runner.run(cmd, ..., parse_progress=self._parse_progress): yield u`.
Tools with no parseable percent (maxcso/nsz/z3ds) additionally pass
`nice_via_wrapper=True` to keep their command-wrapper nice; nsz also forwards its
keys-home `env=`, sets `require_output=True`, and writes into a private work dir,
moving the result onto `output_path` after `run()` returns.

### Every tool reports status (issue #263)

Progress reporting is **not** a per-tool responsibility, and no tool hand-rolls
it. The runner owns both signals and picks between them:

1. **Native, preferred.** `parse_progress(line) -> int|None` parses the tool's
   own percentage. The moment it returns a real percent, that tool is reporting
   for itself and the fallback stands down for the rest of the run — the two
   never compete for the message line.
2. **Output growth, automatic fallback.** When native parsing never yields a
   percent — the common case, because most of these CLIs draw a TTY bar that
   goes silent on a pipe — the runner reports from the output file growing on
   disk: `output_size_message()` emits **MB written and a MB/min rate**. This
   needs no per-tool wiring at all, so *a tool that does nothing gets it*.
   Passing `mode` adds a percentage when `SIZE_RATIOS` knows that mode's
   expected output/input ratio.

The rate matters as much as the byte count: it is what distinguishes a job that
is merely slow (bytes climbing, MB/min collapsing) from one that has stopped
dead. Before this, dolphin-tool reported only elapsed seconds against a 0% bar,
and users could not tell a crawling conversion from a hung one.

### Verify is bounded and cancellable (issue #266)

The conversion path was bounded first (#263/#265); verify had the same gap. A
verify that never returned never ended, and because `MAX_CONCURRENT_JOBS`
defaults to 1 and runs jobs inline in the dispatcher, it froze the whole queue —
reachable through **Delete sources after verification**, a commonly used option.
The delete-on-verify call site also passed no `cancel_event`, so Cancel set an
event that the `await` never observed.

Three pieces, none of them per-tool:

1. **A bound, on by default.** `resolve_verify_timeout(path, owner)` is the one
   source of truth: `COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT` (baseline, 1800s) plus
   `…_PER_GIB` (600) for the file actually being read, capped by `…_CAP`
   (86400) — the same baseline+per-GiB+cap shape as the conversion stall
   watchdog, sharing `timeout_policy.compute_size_scaled_timeout`. Verify reads
   the whole file, so its runtime scales with size and a flat number cannot
   serve both a 400 MB CIA and a 90 GB PS3 ISO. Streaming verifiers additionally
   get `COMPRESSATORIUM_TOOL_VERIFY_PROGRESS_TIMEOUT` (600s of no output at
   all), which catches a wedge far sooner. The two are independent: zeroing the
   overall baseline does not zero the stall bound, deliberately — an operator
   who removes the overall bound for a huge image still wants a verifier that
   has gone completely silent to be caught. Zero both for no bound at all.
2. **`cancel_event` through the contract.** `ToolPlugin.verify()` /
   `verify_stream()` take it and `job_manager` passes the job's own event.
   Tools that spawn a child get cancellation from `run_verify` /
   `capture_verify`; the pure-Python verifies (jwud's WUX walk, makeps3iso's
   PARAM.SFO readback) check it between steps. A cancelled run returns
   `cancelled: True`, which `job_manager` turns into a CANCELLED job — never a
   verification failure, and never a reason to delete a source.
3. **A backstop at every call site.** `job_manager` resolves the bound first
   (`bound = await tool.verify_timeout(path, cancel_event=…)`) and then wraps
   the whole `verify()` in `asyncio.wait_for(tool.verify(…), timeout=bound or
   None)`, so the guarantee holds even
   for a tool whose verify spawns nothing for a subprocess timeout to bound.
   Resolving the bound sizes the file, which is itself a stat on the storage in
   question, so it takes the cancel event too: it runs *before* the verify that
   would otherwise observe one. The
   generated verify routes (`register_verify_routes`: sync, SSE, batch SSE) are
   the *other* entry point into the same verifiers, and they hold the `verify`
   workload lane while they run — so they apply the same per-path bound, once,
   in the shared factory rather than per tool. On the two SSE routes that bound
   lives in the **producing task**, never in the consuming loop: a client that
   stops draining suspends the consumer at its `yield`, and a deadline it cannot
   evaluate is no deadline at all. Bounding the producer also makes the clock
   measure verification instead of delivery backpressure, and makes a verdict
   disagreeing with a timeout impossible — one task decides both. The other half
   of that independence is a **bounded** hand-off (`_offer_verify_update` over a
   capped queue): a producer that no longer waits for the socket would otherwise
   let a peer that stays connected without reading buffer events for the whole
   of the bound. Progress is dropped under backpressure — it is a level, not a
   log — while a terminal event always lands, making room by discarding progress
   the verdict has just made stale. The **workload token follows the same rule**:
   it is released **beside the producer's `done.set()`**, not from the consumer's
   `finally`, which a parked reader never reaches; and the batch route takes a
   slot per file rather than carrying one across the walk (the route's admission
   token is handed back before the first file, since a token held across the
   suspensions *before* a verifier starts is lost the same way). A peer that
   stays connected without reading parks the consumer indefinitely, and a
   one-slot lane released only on that path is a lane lost for the life of the
   process. A route-level timeout reports the
   tool's own timeout shape (`{"valid": False, "message": "Verification timed
   out after Ns"}`, widened with `"type": "error"` on the SSE paths), not a 500:
   the file is not known bad, the check just did not finish.

Three rules follow for any code that runs a verifier:

- **Never offload a verify's blocking read to a shared pool** — including the
  event loop's *default* executor (what `aiofiles` uses), the route guards that
  run *before* the verify (their own bounded seam is `bounded_path_check`), and
  the searches a verify does before it reads anything: nsz resolves `prod.keys`
  through `run_detached` because with `SWITCH_KEYS` unset that walks the game
  volumes, inline and ahead of every bound meant to survive them. Nor wrap such a read in a context manager whose exit
  awaits a close: on a stuck read that close waits on the same lock, turning
  cleanup into a second unbounded wait, so an abandoned handle is left
  unclosed with its thread. Use `run_detached`, and pass it the `cancel_event`: cancellation abandons the
  thread either way, a pooled one is capacity the whole process shares, and
  without the event a Cancel pressed mid-read is not observed until the read
  finishes. Translate its `ReadCancelled` into the tool's own
  `{"cancelled": True}` terminal event.
- **Resolve the bound before the spawn.** It stats the file, and an `await`
  between the spawn and the `try/finally` is a window where a cancelled SSE
  request unwinds the coroutine with the child running, tracked, and nothing
  left to reap it.
- **Never record a verification result from a run that did not reach a
  verdict.** A verifier that outlived SIGKILL is one of those runs, and it is
  worse than a failure: it is still holding the storage. Every verify path
  builds that terminal event from one place (`abandoned_verify_error`) and gives
  it **precedence over the reason the stop was asked for** — a cancel or a
  timeout that ends with an unkillable child is an abandonment first, because
  the child is still running either way. The flag reaches callers through
  `collect_verify` (alongside `cancelled`), and the batch route stops the walk
  on it rather than opening the next file against the same mount and abandoning
  one more process per file. The captured verifiers get there through
  `run_capture`'s `on_abandoned` hook, since a `None` return code alone cannot
  tell an abort from a failed ladder. A verify with **no child at all** still
  leaves a trace when an outer deadline cancels it — the detached read it
  abandoned — so `run_detached` reports those too. Both report into the sink
  `collect_abandonment()` opens around *one file's* work rather than into global
  state: a context variable, because `asyncio.create_task` copies the context,
  so the producer task reports into its own caller's sink and no other. Global
  tallies cannot answer "did **this** verify abandon something" once
  `MAX_VERIFY_CONCURRENCY > 1` — one request's dead mount would stop another
  request's batch on healthy storage. Both kinds of wreckage mean the same
  thing to a caller walking a list: the storage just cost us something we cannot
  get back, so stop. An *outer* deadline (the route's or the
  job's) is the one case that cannot carry the flag on an event at all — it
  cancels the generator rather than letting it reach one — so `reap` records the
  pid it gave up on (`SubprocessRunner.abandoned_pids`) and the routes fold that
  into the timeout verdict. Issue #268 proposed replacing this with an exception;
  it was tried and rejected, because an exception cannot be raised from the
  `finally` an outer deadline unwinds through without masking what is already
  propagating — the very case the sink exists for. **The sink is the mechanism;
  there is no second one.**
- **Every loop that walks a list opens a sink (#268).** The verify routes were
  the first, but they are not special: the DAT-match paths and all three library
  scan phases walk a caller-supplied list of files on one storage in exactly the
  same way, and none of their return values can carry the fact — `disc_hashes`
  returns `list[str]`, `embedded_hashes` returns `list[tuple]`, and a match
  result has no field for it, so an unkillable `dolphin-tool verify` was
  indistinguishable from "this disc has no embedded hash". Because `reap()`
  reports into whatever sink is open and `run_capture` always reaps, the loop
  needs only the `with` block — no hook argument, and no change to the four
  layers between it and the child:

  ```python
  with collect_abandonment() as abandoned:
      result = await _match_single_file(path)
  if abandoned:
      ...  # stop walking; the next file is on the same mount
  ```

  Where the loop has a per-file `except` that swallows failures, the sink goes
  **outside** the handler, not inside: a corrupt file stays isolated to itself,
  while an unkillable child escapes. Scan Phase 2 is the sharpest case — its
  handler only logs at `debug`, so a stranded child there previously left no
  trace at all. A single-file endpoint has no walk to stop, so it reports instead
  (`/dat/match` answers 503).
- **A sink checked only at the loop is too late for a compound operation
  (#268).** The loop-level `with` is enough when the operation just succeeds or
  fails, but not when it *acts* on an intermediate result: two steps here decided
  something on a value that no longer meant what it said. Sizing the file for the
  verify bound is a read of the same storage, and when that probe is abandoned
  the resolver falls back to the flat baseline — so `disc_hashes` would spawn
  `dolphin-tool verify` into a mount already proven unresponsive, buying a full
  verify timeout and likely a second written-off resource. And an abandoned
  `chdman dumpmeta` reports "no GAME tag", which is indistinguishable from a
  genuinely untagged CHD — so `post_convert` answered it by firing `addmeta` at a
  file the abandoned reader still held, and `ensure_disc_id_embedded` fell
  through to a whole-disc sector read on the default executor with no bound of
  its own.

  `abandonment_checkpoint()` is the seam for this: it yields a list for the
  immediate decision and then forwards whatever it caught to the enclosing sink,
  so the step aborts *and* the caller's walk still stops. A naive nested
  `collect_abandonment()` would shadow the outer sink and silently defeat the
  loop. Where the value itself is the trap, the deciding helper raises instead of
  returning it — `services.disc_id` raises `DiscIdStorageAbandoned` rather than
  the `None` that invites a write. This is the design rule *"re-check the cancel
  after resolving the bound, before spawning"* extended to abandonment, and the
  reward for ignoring it is larger: a child that blocks immediately and may
  outlive SIGKILL.
- **`post_convert` is best-effort about tagging, not about storage.** Its
  contract is that a tagging failure never fails the job, because a missing
  disc-ID tag is cosmetic. An abandoned chdman child is not that: it says the
  output's storage stopped answering, and every step after the hook reads that
  storage -- `_compute_output_size` is an unbounded `run_in_threadpool`, and
  delete-on-verify spawns a verifier. Swallowing it trades a skipped tag for a
  hung job, which is #263's original failure, so `DiscIdStorageAbandoned` alone
  propagates and `JobManager` lets it reach the handler that fails the job.
- **One volume's failure stops the whole pass, deliberately.** A scan or batch is
  one user-initiated walk, and the loops do not resolve which configured volume
  each path belongs to, so the first abandonment ends all of it — including
  paths on volumes that are answering fine. That is the safe direction: stopping
  early costs a re-run, while carrying on costs one stranded process per
  remaining file. Note this does **not** transfer to the job queue, which is why
  the dispatcher deliberately keeps going (#265): queue entries are independent
  jobs that may target unrelated volumes, whereas one scan is a single pass the
  user asked for as a unit.
- **One-shot chdman/dolphin captures go through `run_capture`.** `chdman info`,
  `dolphin-tool header`, and the `chdman addmeta` / `delmeta` / `dumpmeta`
  helpers in `services.disc_id` used to spawn directly, untracked, and finish
  with an unbounded wait after `kill()` — `dumpmeta` could hang the scan's Phase
  2 outright. They now share the capture path, so they are PID-tracked, bounded,
  subject to the tool priority policy, and able to report abandonment.
  `services.disc_id` uses `chdman_service.runner` rather than a runner of its
  own, so `active_pids()` still describes every chdman child.
- **Stop a list at the first unresponsive path, don't survey them all.** The
  batch route's path validation bounds each check, but bounding is not enough on
  its own: a batch is a list of paths on *one* storage, so carrying on after a
  timeout costs another probe bound and another written-off thread per path —
  minutes before any verify starts, and enough abandoned threads to exhaust the
  process-wide ceiling, which then fails path checks on volumes that are
  answering. The first timeout ends validation with a 503.
- **Re-check the cancel after resolving the bound, before spawning.** The
  resolution is itself a probe that can take its full bound on storage that has
  stopped answering, and spawning into that means a child that blocks
  immediately and may outlive SIGKILL — a prompt cancellation turned into an
  abandoned process, for work nobody wanted.
- **Every bound resolution takes the `cancel_event` too.** Sizing the file is a
  stat of the same storage the verify is about to read, and it happens *before*
  the verify that would observe a cancel — at the call site
  (`tool.verify_timeout(path, cancel_event=…)`) and again inside each verifier
  (`run_verify`, `capture_verify`, z3ds). Miss one and a Cancel waits out the
  probe bound with the dispatcher slot still held.
- **Keep every wait under the same live checks.** A wait decided once, up front,
  answers only for the instant it was decided. `run_verify`'s post-EOF grace —
  waiting for a verifier that closed stdout to exit on its own — is therefore
  sliced, and each slice re-asks what the read loop asked: has the cancel fired,
  has either deadline passed. Capping that grace by the *remaining* overall
  bound is not the same thing: it sees neither a cancel pressed a second later
  nor the stall bound, which is the only bound a stall-only configuration has.

With verify genuinely bounded, the stalled-job warning no longer *skips* jobs in
the verify phase (it did, to avoid calling a long checksum stalled — at the cost
of logging nothing for a job wedged **in** verify). It now reports them in their
own words, timed from when the verify phase began rather than from the progress
clock, which stopped when the conversion ended.

**Adding a tool:** do nothing and it already reports bytes + rate. Add a
`SIZE_RATIOS` row to also get a percentage bar. Implement `parse_progress` if
the tool prints a percentage that survives being piped — and verify that it
actually does, because several do not.
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

### Cleaning up partial output is bounded too (issue #267)

Every tool that writes its output in place sweeps the partial away when a run
ends abnormally — a cancel, a stall, a non-zero exit, a task cancellation or
generator close. That sweep is **not** a per-tool block: it goes through
`remove_partial_output(*paths)` (files) or `remove_partial_tree(path)` (a
private work dir), both module-level in `services/subprocess_runner.py` and
re-exported from `services/tools/runner.py`.

Why they exist at all: the sweep runs *after* the runner has given up, on the
same storage the run just failed on. `stat`/`unlink`/`rmtree` on an
unresponsive mount block in uninterruptible I/O and cannot be cancelled, so a
hand-rolled `os.remove` there defeated the bounded reap ladder — the child was
abandoned promptly (#263/#265) and then the job hung on cleanup instead, and at
`MAX_CONCURRENT_JOBS=1` (the default, jobs inline in the dispatcher) the queue
froze anyway. These helpers run the blocking call on a throwaway daemon thread
(the `_bounded_probe` shape) with a hard `_CLEANUP_TIMEOUT`.

The contract, which is what makes them safe to use everywhere:

- **They never raise, except to cancel.** A cleanup problem must not replace the
  exception that explains the job's outcome on an error path, and must not fail
  an already-published conversion from the `finally` blocks that also run on
  success. It is a logged warning and a `False` return — the job finishes with a
  leftover partial file, a much smaller problem than a frozen queue. That
  includes being unable to start the cleanup thread at all, which is plausible
  precisely here since a run of dead-mount sweeps deliberately writes threads
  off. `CancelledError` is the one exception that propagates, like from any
  other `await`: swallowing it would defeat cancellation outright for the
  `finally` callers, which have no pending exception to re-raise and would go on
  to be marked complete.
- **The removal is dispatched before the wait**, so a cancellation arriving
  afterwards cannot take back work the sweep already did — the useful half of
  what the old blocks bought by unlinking *synchronously* on the event loop
  (awaiting during a cancellation could be re-cancelled and skip the cleanup),
  without the unbounded block.
- **Giving up stops the rest of the sweep.** Once the helper returns, the job
  finalises and stops owning those paths, so an abandoned thread must not keep
  deleting: the mount can recover minutes later, by which point a retry may own
  the names, and the thread would delete the *new* job's good output. The
  worker checks an `abandoned` flag before each unlink. The one unlink already
  in flight when the bound expired can't be recalled — an in-flight syscall is
  not cancellable — so that single path stays at risk and every path after it
  does not. On responsive storage the sweep finishes long before any of this,
  so the normal case pays nothing.
- **An absent path is success**, so no caller needs a separate `os.path.exists`
  round trip on the mount in question.
- **One bad path does not stop the sweep**: the rest are still removed, and
  everything that failed is named in one warning.

Rules for a new tool:

- Catch the abnormal exits *after* the spawn only. A setup or pre-spawn failure
  wrote nothing, so deleting a pre-existing output there would destroy a good
  file.
- Pass **every** path the run could have written. When the set is only knowable
  by probing the disk, pass `discover=` instead of enumerating first — it runs
  *inside* the bounded worker. makeps3iso's split parts are the case: those
  `isfile` probes hit the same volume the sweep is about to unlink from, so
  enumerating on the event loop would reintroduce the exact unbounded block
  this seam removes. It passes `output_artifacts()` as the discovery callable,
  keeping the one enumeration that also backs `overwrite_targets`.
- Where the result is load-bearing rather than best-effort, check it. romz
  clears a stale archive *before* running `7z a` (which appends) and refuses to
  start if that sweep reports failure. Note what the propagating `CancelledError`
  buys that call: a job cancelled mid-sweep raises out of the helper instead of
  reaching the "could not clear the existing archive" error, so it still reports
  as cancelled rather than as that failure.

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

#### A step can flag a caveat the chain must not bury (`warning`)

Each step's updates overwrite the job's `message`, so a caveat an *earlier* step
raised about its own output is gone by the time the chain finishes — and the
terminal `{"progress": 100, "message": "Conversion complete"}` says nothing.
That matters when the caveat changes what the output *is*: nkit2iso restoring a
Wii image whose update partition was removed produces a playable but **not**
bit-exact ISO, and `nkit_to_rvz` would otherwise report an unqualified success.

A step opts in by marking that update with `warning: True`; `ChainTool` collects
them and appends them to its own terminal message (itself marked `warning`).
`job_manager` reads only `progress` and `message`, so the extra key is inert for
a direct job and needs no contract change.

Note what this does **not** buy: the flag arrives mid-run, long after
`supports_delete_on_verify` was read at plan time. It informs the operator; it
cannot gate the pipeline. That is why `nkit_to_rvz` declines delete-on-verify
outright rather than conditionally — see the flag's comment in `chain.py`.

#### `expected_output_size`: preflight sizing without a tool-specific import

`ChainTool._preflight_headroom` has to bound three things at once (source, full
intermediate, partial final) before it starts, and `ChainStep.output_ratio` is
too blunt for it: a heavily compressed `.cso` or a scrubbed `.nkit.iso` can be
an order of magnitude smaller than the `.iso` its step will write, so a ratio on
the input size under-counts badly and the preflight passes on a volume that then
fills up mid-job.

It used to solve that by importing `services.maxcso.uncompressed_iso_size`
directly — a single-tool special case sitting on code every chain runs through,
the same shape as the `overwrite_targets` / `is_ready` branches removed earlier.
A second chain whose first step was a different tool would silently get the
ratio fallback with no way to do better.

The contract now carries it:

```python
def expected_output_size(self, input_path: str, mode: str) -> int | None:
    """Bytes this mode will write, if cheaply knowable from a header."""
```

`BaseTool` returns `None` (fall back to `output_ratio`). `MaxcsoTool` returns
`uncompressed_iso_size` for `cso_decompress`; `Nkit2IsoTool` returns the image
size the NKit header states. `ChainTool` asks `steps[0]`'s tool and never names
a tool itself. It is **preflight only** — never a correctness input — so an
unreadable file is `None`, not an error. Blocking (a small header read), so call
it off the event loop.

#### A chain's output name comes from its FIRST step

`ChainTool.output_path` used to delegate to the *last* step's tool, which is
correct only while every chain source has a plain single suffix. The first step
is the one that owns the input, so only its tool knows how to strip the source
name: `nkit_restore` must drop a whole compound `.nkit.iso`, where dolphin (the
final step of `nkit_to_rvz`) would leave `Game.nkit.rvz`. The rule is now "the
first step's own output path, with the chain's declared `output_ext` swapped
in", which is identical to the old behaviour for `cso_to_chd` (`Game.cso` ->
`Game.iso` -> `Game.chd`). `detect_output` and the scratch intermediate's name
follow the same rule.

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
    makeps3iso's own part-removal helper directly, which meant a *second*
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

### 3.3.5.1 Remote hash fallback (`services/hasheous/`)

Hash matching has two sources, and the ordering between them lives in
`routes.dat._lookup_match`, which runs `_local_lookup_match` and only then
`_remote_lookup_match`. Every match path — `POST /dat/match`,
`/dat/match-batch`, the background match job, and `info._scan_phase_dat_match`
— reaches the sources through those helpers, so the fallback was added there
rather than by introducing a provider registry. Two sources do not need an
interface; a third would.

One caller does not go through `_lookup_match`: `_match_single_file` calls
`_local_lookup_match` and `_remote_lookup_match` directly, because it has to
check *every* local candidate a file offers (embedded hashes, then the
file-level SHA1) before any of them may go out remotely. `_match_result`
builds the record both paths return.

The order is **fixed and local-first**: `_local_dat_record` (the imported DATs)
and only on a miss, and only when the operator opted in, `hasheous.lookup`. That
keeps a covered library fully offline and makes a given hash resolve the same way
regardless of network weather. Both helpers return the **same record shape** —
`dat_id` / `dat_name` / `game_name` / `rom_name` / `source`, plus whatever extra
identity fields the source carries — so `_match_result` builds the
result dict once and splats the record into it. Adding a field to a remote match
means adding a key to that record, not touching the builder.

#### Candidate ordering: every local lookup before any remote one

This rule has to hold across the *whole* of `_match_single_file`, not just
within one helper, and it took three passes to get right — so the shape is
deliberate.

The lookup is split into `_local_lookup_match` and `_remote_lookup_match`, with
`_lookup_match` as the local-then-remote composition. `_match_single_file` calls
the halves directly, because its candidates do not all exist at the same time:

1. `_try_embedded_hash_match` gets the tool's embedded hashes (a CHD reports a
   header SHA1 *and* a data SHA1) and checks them **locally only**.
2. Unless the tool's hashes are exhaustive, the file-level SHA1 is computed —
   expensive, hence lazy and size-capped — and checked **locally**.
3. Only once every one of those has missed does the **complete** candidate set
   go to Hasheous, in a single remote pass.

Two orderings that look reasonable are both wrong, and each was a real bug here:

- *Remote-checking candidate 1 before local-checking candidate 2.* Discloses a
  hash the local DATs could have identified, and a remote timeout on the first
  raises before the second — the one that would have matched — is tried.
- *Letting the embedded-hash helper go remote before the caller has hashed the
  file.* Same failure, one level up: for a non-exhaustive tool like CHD the
  container's own bytes may be exactly what the local DAT indexes, so the
  embedded hashes must not leave the machine until that has been checked.

`_try_embedded_hash_match` is therefore local-only by contract, and returns its
candidates so the caller can carry them into the single remote pass.

**The one place the rule is best-effort rather than absolute is a size-capped
file**, and it is a deliberate trade. `MATCH_MAX_FILE_SIZE` forbids reading the
file, so its container SHA1 can never be computed — meaning the local candidate
set *cannot* be exhausted, and "we never disclose a hash the local DATs could
have identified" is unachievable for that file by construction. The choice is
between disclosing the (already-in-hand, free) embedded hashes and identifying
the file at all. It still gets its remote pass, because the cap says "don't read
this file's bytes", not "don't identify this file" — and for a CHD the hash a
DAT actually indexes *is* the header SHA1, which is an embedded candidate and so
is still checked locally first. The residual exposure needs an unusual DAT that
indexes raw `.chd` container bytes rather than the header, plus a cap, plus a
file over it. A size-capped miss keeps its non-cacheable `reason`.

#### Cache entries are scoped to the sources that produced them

Every cacheable miss carries `checked_remote`, and `cached_result_usable(payload)`
is the single gate every cache read goes through (`/dat/match-batch`,
`/dat/matches/lookup`, `/dat/match-batch/job`, and the scan's Phase 3 skip).

A miss recorded before Hasheous was enabled came from a strictly weaker matcher,
so it must not be served once the stronger one is available — otherwise an
existing install switches the feature on and nothing happens, because every path
in the library already has a cached "not in any DAT" row. Hits are always usable:
local DATs are consulted first, so a remote source could not have improved on one.
This is a payload field, not a schema change.

#### Other load-bearing properties

- **A failure is not a miss.** `hasheous.lookup` raises `HasheousUnavailable` for
  timeouts, 5xx and unparseable bodies, and returns `None` **only** for the
  documented 404. `_match_single_file` converts the exception into
  `{**base_result, "error": ...}`, which the existing rule (see
  `_abandoned_match_result`) refuses to cache. Without this, one network blip
  would permanently record every in-flight file as being in no DAT.
- **An outage is learned once, not once per file.** A failure opens a 60-second
  module-level cooldown during which `lookup` raises immediately without a
  request. Files still come back non-cacheable — just without paying
  `hasheous_timeout` each. A 1,000-file scan against a dead endpoint would
  otherwise burn over four hours rediscovering the same fact. **Everything that
  can judge the server unhealthy sits inside the one `try`** — transport errors
  *and* response validation — because a proxy returning an identity-less `200`
  for every hash is exactly as much of an outage as a timeout, and validating
  after the guarded call left that case re-requesting once per file. A clean
  404 is the one non-failure: the server answered, so it clears the cooldown.
- **The timeout bounds the request, not each socket read.** `urlopen(timeout=)`
  restarts on every byte that arrives, so a server dripping slower than the
  timeout but never stopping pins the lookup — and the scan job around it —
  without ever raising, which also means the cooldown never opens. Measured:
  an 18.5s `open()` under a 2s timeout, and unbounded for a longer header.
  One `_DeadlineMixin` enforces one monotonic deadline (`_deadline_of`) inside
  `recv_into`, and it is mixed into **both** socket classes a lookup can hold:
  `_DeadlineSocket` (the raw socket) and `_DeadlineSSLSocket` (installed via
  `SSLContext.sslsocket_class`). Two classes over one mixin, because
  `wrap_socket` detaches the raw socket and rebuilds from its file descriptor,
  so the raw class cannot carry through. Doing it at the socket rather than
  around the body read is what makes the bound real: an earlier body-only
  version left the identical hole in the headers, where a hostile or broken
  server can drip just as easily.

  The connection is deadline-aware from the first packet:
  `_DeadlineHTTPSConnection` swaps in `_connect_with_deadline`, which loops the
  `getaddrinfo` results itself and gives each attempt only what is *left* of
  the budget. `socket.create_connection` gives each address the full timeout —
  measured, three blackholed addresses cost 9.0s under a 3s timeout, and a
  redirect opens a fresh connection with a fresh budget. `_DeadlineHTTPSHandler`
  is what puts that connection in `_opener`'s hands; both halves of the wiring
  (the handler class and the context's `sslsocket_class`) are pinned by tests,
  because rebuilding the opener with stock parts removes the protection
  silently while every behavioural test still passes.

  That covers all four phases through one mechanism — raw connect, the proxy
  `CONNECT` tunnel (`http.client._tunnel` reads it off the raw socket, so a
  dripping proxy used to pin the request and never raise), the TLS handshake
  (which makes **zero** `recv_into` calls, so `do_handshake` applies the
  deadline itself), and the response.

  Two things remain outside it, both accepted. A peer that drips one TLS
  *handshake record* at a time inside the remaining budget is not bounded, and
  **DNS is not bounded by `hasheous_timeout` at all** — `getaddrinfo` runs
  before any socket exists and ignores socket timeouts. The resolver bounds
  itself (`/etc/resolv.conf`: glibc defaults to 5s x 2 attempts per nameserver)
  and, unlike a drip, it *raises* — so it opens the cooldown and the rest of a
  bulk job short-circuits instead of stalling one file at a time. Closing
  either means leaving urllib or resolving on an abandonable thread.
- **The toggle persists before it applies.** `PUT /api/dat/hasheous` writes the
  preference first and only then flips the in-process override. The other order
  meant a failed write (locked SQLite, full disk) left the process sending
  hashes remotely while the endpoint reported failure and the UI still showed
  the switch off — a privacy-relevant lie, not just a stale display.
- **`dat_id` is always `None` on a remote hit.** It is a FK into the local `dats`
  table and a remote match has no row there. `dat_store` already nulls unknown
  values before writing, so this keeps the cached row byte-identical across
  re-runs rather than depending on that guard.
- **"Remote hit" is recorded, never inferred.** `_remote_hit_clause()` in
  `dat_store` is the one place the question is answered, and it answers from
  `payload.source`, not from `dat_id IS NULL`. The FK proxy was wrong for one
  real row: a *local* hit whose DAT was deleted between the match and the write
  has its dangling FK nulled by `_upsert_match_sync`, which also dodges the
  `WHERE dat_id = :id` cascade — so it was preserved through every later import
  and went on serving an identity from a DAT the operator had removed.
  `coalesce` is load-bearing there: SQL `NULL` is not `False`, so a payload with
  no `source` must compare unequal rather than unknown, or the negation spares
  exactly the rows it is meant to drop.
- **Local-first holds across the whole call — for the disclosure, not only the
  verdict.** The remote request is an await of its own, and a DAT import commits
  in its own transaction, so `_remote_lookup_match` re-checks the local index
  *between* candidate requests as well as once more after the loop. The post-loop
  pass alone was not enough: it protects the answer, but by the time it runs the
  hash the newly-imported DAT could have identified has already gone out. A CHD
  sends up to three, so that is up to two avoidable disclosures per file. Each
  check is over the whole candidate set, and each is one indexed lookup.
- **Concurrent DAT imports have a boundary, and it is the transaction.** A match
  runs local-first, but the decision and the write are separate operations, so
  three windows exist between them: the file hash (seconds), the remote request
  (seconds), and decide-then-write (milliseconds). The first two are closed by
  re-checking the local index — before the remote pass, and again over the whole
  candidate set before a remote answer is accepted. The third is closed in
  `dat_store._local_index_now_covers()`, which re-checks inside the writing
  transaction and skips the write when the local index has since learned any of
  the candidates (leaving the path uncached, so the next match resolves it
  locally). What remains is the transaction boundary itself: an import
  committing between that read and the write's commit leaves a remote hit,
  which invalidation preserves by design — and which is now *scheduled for
  rematch* unless its commit also lands after the post-change listing below,
  so the residual window is the narrower one and sits entirely after the new
  DATs are live.

  That third guard covers **every verdict the remote source took part in, over
  the whole candidate set** — not just the hit, and not just the hash that
  matched. A stamped *miss* is cacheable too, and its `checked_remote` stamp
  stays valid, so restricting the guard to hits let a fresh local match stay
  hidden until the next import happened to invalidate the row; and a CHD offers
  up to three hashes, so the DAT that landed mid-flight may know a candidate
  other than the one that hit. The route attaches the candidate SHA1s under
  `dat_store.CANDIDATE_HASHES_KEY` (`_carrying_candidates()`, on both cacheable
  remote exits); the store revalidates them and strips the key in
  `_persistable_payload()`, so it never reaches a persisted row. It is a hash
  re-check rather than a DAT-index generation on purpose: a generation would
  skip the write on any unrelated import and send the file straight back out to
  the remote source, which is the same re-disclosure cost rejected just below.

  That last one is deliberately *not* chased. Closing it read-side means
  stamping remote hits with the DAT-set generation and rejecting stale ones,
  which invalidates every remote hit on every DAT change — the recompute misses
  locally (the new DAT usually doesn't cover the file that needed Hasheous) and
  re-queries, re-disclosing thousands of hashes per sync, forever. That is a
  worse trade than the thing it fixes, whose worst outcome is a HASH badge where
  a DAT badge would do: same game, self-correcting on the next rescan. Closing it
  write-side means serialising matches against imports, i.e. holding a lock
  across a network call.
- **A cancelled match job does not hand its queue to a successor.** The
  deferred-rematch drain lives in `_run_match_job`'s `finally`, which a
  cancellation reaches like any other exit — so the cancelled job started a
  *replacement* job on its way out. `/jobs/cancel-all` snapshots the job list
  before that replacement exists, so the rematch escaped the cancellation
  entirely and went on hashing moments after the operator asked for silence.
  Cancellation now discards the queue instead (`_discard_deferred_rematch`),
  logged rather than silent: those files keep their current verdicts until they
  are browsed or a rescan runs. Dropped rather than left queued because nothing
  but a finishing job drains the set, so holding them would mean waiting on an
  unrelated job that may never come.
- **A match job fails on its *checkable* files, not its total.** Policy skips
  (over `MATCH_MAX_FILE_SIZE`, not a regular file) are files the job
  deliberately did not check, so they are excluded from the all-failed
  denominator instead of counting as survivors. With `errors == total` a single
  oversized ISO in the batch downgraded a complete Hasheous outage to a green
  "complete" carrying a generic error count — exactly the misread that branch
  exists to prevent. A batch of nothing but skips still completes: nothing was
  attempted, so nothing went wrong.
- **A DAT change re-matches through one helper, over before *and* after.**
  `rematch_after_dat_change()` is shared by the manual upload (`import_dat`) and
  the MAMERedump sync; both want "recompute what already had a verdict against
  the new index", and it had been written twice. The matcher is single-flight,
  so when a job is already running those paths go into
  `_deferred_rematch_paths` and the finishing job drains them — "best-effort"
  means *later*, not *never*, which is what dropping the `None` return used to
  mean.

  The caller's snapshot is taken *before* the change, because the change is
  what invalidates — but it is not transactional with it, and a match already
  in flight can persist a remote hit inside that window. Invalidation preserves
  remote hits by design, so such a row survives while being absent from the
  snapshot, and since a cached hit is always usable nothing would ever
  recompute it: the DAT just imported could know that hash and never get the
  chance to say so. `_paths_needing_rematch()` therefore unions the snapshot
  with a listing taken *after* the change — anything still cached once the new
  index is live either predates the change or was written during it, and both
  need recomputing. A row written after the listing is redundant to include
  rather than wrong. This is why the sync calls the hook unconditionally now:
  an empty snapshot is exactly the case where the surviving row is the *only*
  thing to rematch, and the old `if previous_match_paths:` guard skipped it.

  **The rematch job is local-only** (`schedule_match_job(..., local_only=True)`,
  threaded down to `_match_single_file`). What a DAT change alters is the local
  index; the remote source is exactly as it was, so re-asking it once per
  previously-verdicted file cannot yield an answer different from the cached
  one — it only re-discloses every hash, thousands per MAMERedump sync, on a
  schedule the operator never chose. This was the single largest disclosure
  path in the feature and it was unintended: `_hash_one_for_job()` calls
  `_match_single_file()` with no cache consult, so *every* rematched path ran
  the full local-then-remote pipeline. The three outcomes fall out of guards
  that already existed:

  | after the change | result |
  |---|---|
  | new DATs cover the file | local hit written, badge upgrades HASH → DAT |
  | they don't, row was a remote hit | unstamped miss, which `_would_downgrade_remote_hit()` refuses to write — badge stays |
  | they don't, row was a remote-checked miss | that row was dropped by invalidation, so the unstamped miss replaces it and `cached_result_usable()` finds no stamp — re-checked remotely when the file is next browsed |

  The third row is the residual: a previously-unmatched file *is* re-disclosed,
  but lazily, one file at a time, on the operator's navigation rather than in a
  burst at sync time. Closing it too means keeping remote-checked misses across
  an invalidation, which would serve a stale negative for any file the new DAT
  does cover — the trade the invalidation exists to avoid.

  The unstamped miss must still **carry the recomputed hashes**
  (`_carrying_file_hash()`). "The DATs still don't know it" and "this is a
  different file now" both reach the store as an unmatched result, and a
  recomputed hash is the only thing that separates them:
  `_proves_content_changed()` compares against the stored one, and without it
  the row-two guard protects the badge of a file that has been *replaced*,
  permanently — the job path never calls `drop_if_content_changed()`, that is
  the scan's. The outage exit had carried the file-level hash for this reason
  since round 7; the local-only exit was written without it, so one helper now
  serves both.

  It carries the **whole typed candidate set**, not just `file_sha1`, and the
  comparison keys on the *stored row's own* `match_type` rather than demanding
  `file_sha1`. That restriction existed for a real reason — a CHD hit is stored
  against its embedded `chd_sha1` while a rescan recomputes the container's
  `file_sha1`, and those differ for a file nobody touched, so comparing them
  deleted valid badges. But it was too blunt: an *exhaustive* tool (Dolphin
  RVZ/WIA/GCZ) recomputes the very hash it matched on, so the domains agree and
  the comparison is sound — and such a format never computes a `file_sha1` at
  all, so under the old rule a replaced RVZ could never be proven changed and
  kept its previous game's badge for good. Keying on the stored domain admits
  that case and still refuses the cross-domain one.
- **The workspace re-checks matchability only while it has none.**
  `refreshMatchingAvailability()` already absorbs a provider flip made
  elsewhere (another tab, an API client) — it resets the match cache and the
  attempt guard when `hasheous_enabled` changed under it — but nothing drove it
  after mount, so a DAT-less install whose operator enabled Hasheous in a
  second tab sat at `matchingAvailable === false` and started no jobs until
  this tab visited the DAT view or reloaded.
  `watchMatchingAvailability()` polls it, and *stops itself* the moment
  something can answer a lookup: that is the only state where a stale reading
  changes behaviour, so a configured install makes no request at all. The
  workspace owns the teardown.
- **`matching_available(has_dats)` replaces the bare `has_dats` gates.** Those
  gates predate the remote source and would otherwise short-circuit before it is
  ever reached for an operator who imported no DATs at all. The frontend has the
  same gate — `datMatching.matchingAvailable`, derived from `total_dats > 0 ||
  hasheous_enabled` — and it is deliberately *not* named `hasDats`, because
  reading it as "are there DATs" is exactly what would silently stop the browse
  path from ever scheduling a match job.
- **A broken store still skips Phase 3.** `_scan_phase_dat_match` tracks store
  health separately from `has_dats`: the phase exists to prime the match cache
  and every write goes through that store, so proceeding on a dead DB would fail
  the whole scan instead of degrading quietly.
- **A scan says when its remote phase failed.** `_scan_phase_dat_match` returns
  `(matched, hasheous_errors)` and the scan's final line carries the second
  number. The errors are still non-cacheable and still retried, but a rescan
  run during an outage used to finish "0 matched" — indistinguishable from a
  library genuinely in no DAT. The scan is *not* reported as failed: phases 1
  and 2 succeeded and metadata was collected, so the honest signal is a
  qualified success, not a flipped boolean.

`services/hasheous/` is a two-module package: `__init__.py` is the client (the
toggle, the cooldown, `_fetch_json`, `lookup`, `health`, normalization) and
`transport.py` is the deadline-aware HTTPS stack described above. They are
split because the transport is a self-contained concern with its own failure
modes, and because the four phases it now covers were each found a review round
apart and patched separately before being made one mechanism. The package
re-exports the transport names, so `from services import hasheous` still
reaches the whole surface and `_fetch_json` remains the single patch seam —
important, because a second module object here is exactly how a test suite ends
up silently hitting the real network.

The client is stdlib-only (`urllib.request`), mirroring `services/dat_sync.py`:
`_require_https`, an explicit `User-Agent`, a hard timeout, a response size cap,
and `_fetch_json` as the single seam tests patch. Redirects go through
`_HTTPSOnlyRedirectHandler`, which re-runs the scheme check on every hop —
`urlopen` follows redirects itself, so validating only the initial URL would let
a misconfigured or hostile server bounce a lookup to `http://` and put the file's
SHA1 on the wire in the clear. Extra fields ride in the existing
`dat_matches.payload` JSON column, so no migration is involved.

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

### 3.3.7 Finished-job history overflow (`JobManager.get_history_overflow`)

`MAX_JOB_HISTORY` (default 500) caps how many **terminal** jobs `JobManager`
retains; `_prune_jobs` deletes the oldest past the cap. That makes the retained
job list a bounded window, not a record of the run — so **no count may be
derived from the job list alone**. Doing so is what froze every finished-job
count at the cap: the Jobs panel read *"Completed 500"* while a 1,153-file batch
kept draining, and the dashboard's Done / Failed tiles did the same.

The contract, tool-agnostic and owned by `JobManager`:

- **`_record_history_eviction(job)`** tallies each automatically evicted job
  under `status → mode → count`, and appends its id to a bounded log stamped
  with a monotonic `seq`. **Only automatic eviction is tallied.** A job deleted
  by the user (single row, or Clear) is history *they* dropped; counting it
  would make a badge outlive every list that could show it.
- **`get_history_overflow(since=None, generation=None)`** returns
  `{generation, max_job_history, evicted, total_evicted, seq, evicted_ids,
  cursor_expired}` — absolute totals, never deltas, so a dropped frame or a
  reconnect self-heals. Pass a previously returned `seq` as `since` to also
  receive the ids evicted after that point, **and the `generation` that cursor
  came from**: a cursor minted by an earlier process is meaningless here, and
  reading it literally would return no ids at all, so it is rewound to the
  start of this log instead. `cursor_expired` says the cursor fell off the end
  of the bounded id log (sized to `max(2000, 4 × max_job_history)`), so the ids
  cannot be complete and the client must re-sync against the job list rather
  than trust the rows it holds — that includes a cursor from before a Clear,
  which throws the log away and deletes every finished job with it.
  `history_eviction_seq()` opens a cursor without reading counts.
- **`reset_history_overflow()`** clears the tally and the id log, and returns
  the *advanced* `seq`. Never rewind it: it is a cursor clients hold, so
  rewinding would make a stale cursor look current, while advancing makes every
  read taken before the reset recognizably older. `DELETE /api/jobs/completed`
  calls it and reports `history_forgotten` / `total_cleared` / `history_seq` /
  `history_generation` alongside the deleted row count — feed those back through
  the same apply path as any other payload, so the Clear result obeys the same
  staleness and generation rules instead of a hand-rolled copy of them.

**Every change to the tally advances `seq`** — evictions and resets alike. That
is what makes an out-of-order payload detectable: equal sequences carry equal
counts, so a client can reject anything older and re-apply an equal one as a
no-op. The sequence is in-memory, so every payload also carries a
**`generation`** for the process that produced it: a restart rewinds `seq` to 0,
and a client comparing across that boundary would reject everything the new
process sends. A changed generation means *drop the cursor*, not *reject*.

Transport: `GET /api/jobs/history-overflow` for hydration and the polling
fallback, plus a `history` event on `/api/jobs/events`, emitted on connect and
whenever the totals change. Additive — clients that don't listen for it drop it
silently, as with `snapshot`.

The stream opens its cursor **before** emitting the snapshot, not after:
snapshot emission suspends the generator between jobs, so a row already handed
to the client can be evicted moments later, and the first `history` frame has to
be able to retract it.

**The three invariants a client must preserve** (`src/lib/stores/jobs.svelte.js`):

1. **Displayed total = retained + evicted.** Apply the same filters to both
   halves; the tally is keyed by mode precisely so the external-scan filter
   (`metadata_scan` / `dat_match` hidden unless asked for) can be applied to
   evicted jobs too.
2. **Drop `evicted_ids` before taking the new tally.** A completion reaches the
   client over SSE *before* the prune it triggers, so the client briefly holds a
   row the backend has already deleted. Counting it as retained *and* as evicted
   inflates every badge by one per completion for as long as a batch runs past
   the cap.
3. **Never apply a payload older than the one already applied**, and never
   compare across generations. The REST read races the stream; without a `seq`
   check a hydration response captured before a newer event can land after it
   and roll the totals backwards, and since the backend only re-emits on change
   they would stay wrong until the next poll. Adopt `history_seq` from the Clear
   response for the same reason.

**Reading both halves without a stream** (the 30 s poll, and every reconnect —
a fresh stream opens its cursor at *now*, so its first frame cannot name what
was evicted while the client was away): capture the cursor, read `/api/jobs`,
then read the overflow **with that cursor**. In that order the two reads
reconcile exactly — anything evicted before the list read is already absent from
it and counted in the totals, and anything evicted after is named in
`evicted_ids` and dropped. Skip the overflow read entirely if the list read
failed: the halves are only coherent together.

Where retained rows exist but the total exceeds them, say so in the UI rather
than let a badge and a list disagree — `JobsPanel` renders *"Showing the 500
most recent of 1,153"*, and a distinct empty state for a tab whose jobs have all
aged out.

### 3.3.8 Output-conflict resolution (`services/output_conflicts.py`)

"Is this mode's output already taken, and what do I do about it?" is asked by
every path that queues a job — `/api/jobs`, `/api/jobs/batch`, and the RomM
automation sweep. It is not an HTTP concern, so it does not live in a route:
both helpers sit in `services/output_conflicts.py` and every caller gets the
same answer, or `duplicate_action` would mean different things depending on how
the job was started.

- **`check_output_conflicts(mode, output_path) -> (exists, locked)`** —
  companion-aware. An `extractcd` `.bin` sidecar or a split `folder_to_iso`
  build's numbered parts count as occupying the destination, enumerated through
  the owning tool's `companion_outputs` hook rather than re-derived per caller.
- **`get_unique_output_path(base_path, mode=None)`** — probes `name_1`,
  `name_2`, … until the file *and* that mode's companions are all free. Raises
  **`OutputPathLocked`** when `base_path` sits inside a locked directory subtree:
  every numbered sibling it would try shares the same held parent, so probing on
  would spin with no sleep until the dir lock released and then return an
  arbitrary name.

The *failure shape* stays a caller concern. `routes/convert.py` keeps a thin
`get_unique_output_path` wrapper that translates `OutputPathLocked` into
`SkipFile(SkipReason.OUTPUT_LOCKED)`, so the job pipeline defers and requeues
the file like any other locked output; the sweep simply drops the candidate and
picks it up next time. Both touch the disk (a directory mode's companion lookup
scans), so call them off the event loop.

#### Destination reservation is linear, and does no I/O (`_canonical_path`)

`JobManager._reject_claimed_destinations_locked` runs on the event loop while
`_create_lock` is held, so its cost is the cost of accepting a batch. Every
destination is canonicalised **once** (`_canonical_path`) and compared through a
dict, and the live-job map is built once per batch (`_active_output_map`) rather
than re-derived per spec. The pairwise form re-resolved every earlier
destination for every new one — quadratic in the batch size, in blocking
`realpath` stat chains, against the remote mounts this integration exists for.

Linear was not enough: *one* `realpath` into an unresponsive NFS/SMB/rclone
mount blocks in uninterruptible I/O, on the event loop, under the lock — which
is every unrelated API request and all job creation, frozen for as long as the
mount stays quiet. So the reservation now does **no filesystem work at all**.
`create_job` / `create_jobs_atomic` call **`_reservation_keys(job_specs, mode)`**
*before* taking the lock: it derives each destination with `_derive_output`
(pure string work — every tool's `output_path()` is stem + suffix, no stat),
collects them with the sources and every live job's output, and resolves the lot
in one `run_detached` thread under `asyncio.wait_for(_CANONICAL_PROBE_SECONDS)`.
The result is a `{path: canonical}` map, pre-seeded lexically
(`normpath(abspath())`). `_canonical_path(path, resolved)` reads that map and
never touches the disk.

**An expired bound refuses the submit — it does not degrade to the seed.**
Completing on lexical keys reads as the conservative choice and is the
opposite. The lexical seed is the path's *spelling*, and `realpath` is the only
thing that makes two spellings of one file compare equal; so a destination named
through a symlinked library root and one named through the path underneath it
get two different keys, and both submissions pass a check whose entire purpose
is to reject the second. When the mount comes back they write the same file —
concurrently once `MAX_CONCURRENT_JOBS` is above one, otherwise one over the
other — and with delete-on-verify both sources are removed for the single
artifact that survives. `_resolve_paths_bounded` therefore raises
**`DestinationUnresolvableError`**, which the submit routes answer **503**: the
volume is not answering, nothing was queued, come back. A volume that cannot
complete a `realpath` inside the bound is one the conversion would have failed
on anyway, and being told at submit time costs a retry rather than a file. The
partial result is not merged either — a half-applied map compares one
destination lexically against an earlier spec and canonically against a later
one, which is the same hole with extra steps.

**A new caller of the reservation must pass a map from `_reservation_keys`.**
Calling `_canonical_path` without one is the blocking form, and it is only
correct off the event loop.

`companion_outputs` is enumerated in that same detached pass, not under the
lock: it is **not** pure path math for every tool -- makeps3iso probes the disk
for its numbered split parts -- so the pre-flight returns a `_Reservation`
carrying both the canonical map and each destination's full key set, and the
locked check reads them. That pass refuses on the same terms: reserving the
primaries alone leaves the sidecars unclaimed, so a second submission whose
primary differs but whose companions land on these gets through, and a cue sheet
or a numbered split part written by two jobs is the same lost data as the image.

`_derive_output` is pure string work only because **every tool's `output_path()`
is stem + suffix arithmetic with no stat**, and three hot paths now depend on
that: this pre-flight, `registry.mode_is_automatable`, and the status route that
serves it. A tool whose `output_path` probed the disk would put filesystem I/O
back under `_create_lock` and on the event loop -- so keep it pure, and put the
probing in `detect_output` / `verify_target`, which have bounded seams.

Moving the resolution ahead of the lock opens one gap the map alone cannot
close: two submissions can both pre-resolve before either takes the lock, so
the second's map cannot contain a job the first queues in between. Falling back
to lexical for that job would miss that a symlinked spelling and the real path
name one file — and with overwrite plus delete-on-verify, both sources are
deleted for one surviving output. So `_queue_job_locked` records the key it
claimed the destination under in **`_output_keys[job_id]`**, and
`_active_output_map` prefers it over the (possibly stale) map. The entry is
dropped when the job leaves the queue; only live jobs are consulted.

#### One destination, one source (`output_conflicts.collapse_to_winners`)

Two inputs can resolve to the same output — a `.cue` beside its `.bin`, two
`Game.iso` files aimed at one output folder — and `/jobs/batch` collapses them
into a single job, keeping the highest-priority source (`input_priority`; first
wins a tie). Anything that records *per source* has to reach the same answer or
it describes a job that was never created: the RomM metadata snapshot was
written for whichever source came last, so the conversion that ran could be
re-pinned with the skipped ROM's identity. Both the batch route and the re-pin
plan call the same helper.

#### A row is keyed by its destination, and owned by its source

The dedupe key is `output_path`, which is right — one destination is produced
once, so re-recording *supersedes* rather than stacking, and re-submitting a
batch is harmless. But the key alone cannot say **which conversion** a row
belongs to, and that is a different question with a real answer.

It matters when a planned batch is not the one the queue accepts. Two clients
can plan different sources onto one destination; the second `record()`
supersedes the first's row, and if the *first* then wins job creation, the
second's failed submit tidies up. Retiring its row would take the accepted
conversion's only remaining snapshot with it — which is why the cancel route
keeps a row a queued job is writing to. Keeping it on *those* grounds was its
own bug: the surviving row holds the second source's provider ids while the
queued job converts the first, so the settle pass hashes one game's output and
stamps the other game's identity onto it, confidently, with nothing to notice.

So the row records `source_path` (migration `0007`), and the test is
`_destination_job_matches_source`: keep the row only when a queued job carries
**both** this destination and this source — `get_active_job_candidates` reports
a job's input and output together, so one job holding both is this row's
conversion. `_destination_has_pending_job` remains for the weaker question,
which still has uses ("may I treat what is on disk as settled"); the two are
distinct and both are needed.

A row with no `source_path` — written before the column existed — cannot answer
and answers **no**. Retiring it costs a manual re-match; keeping it can cost a
game its identity, which is the whole failure being removed. The column rides
through the unique-index retry path too, or a row that lost that race would come
back unable to prove ownership.

### 3.3.9 Re-pin queue and conversion provenance (`services/romm/repin.py`)

Two questions the filesystem cannot answer on its own, both owned here so the
manual submit (`routes/romm.py`) and the unattended sweep (`services/romm/auto.py`)
give the same answer.

**"Has the conversion actually produced this output yet?"** A re-pin row is
written *before* the conversion runs — the source's provider ids are only
readable while the source is still the file RomM knows about — and under the
`overwrite` policy the destination is occupied at that moment by definition. So
`record()` also stores **`pre_fingerprint`** (`"size:mtime_ns"`, or `""` when the
path was free) and the settle pass treats an unchanged destination as "not yet".
Without it, a batch that was planned and then rejected leaves a row that hashes
the *previous* artifact and pushes this ROM's ids onto whatever RomM identifies
that as. `cancel(row_ids)` retires such rows immediately rather than waiting out
`repin_abandon_days`; the fingerprint is what makes leaving them safe until then.

Cancelling is keyed by **row id**, which is why `record()` returns one and
`/romm/repin/plan` answers with `recorded_ids` beside `recorded_paths`. A
destination names whichever row holds it *now*: `record()` supersedes, so a
second client planning the same output between this caller's plan and its
cancel owns that path — and cancelling by path would retire the live row its
queued conversion needs. The same asymmetry governs `release()`: a claimed row
is `settling`, which the partial unique index does not see, so a re-plan can
insert a new `pending` row for the same output while the claim is out.
Restoring the claim to `pending` then violates
`ux_romm_repin_pending_output` — inside the failure handler that called
`release`, replacing the real error with a 500 and stranding the claim until it
ages out — so `release()` retires a superseded claim instead, and returns
whether the row went back to `pending`.

`retire_all_pending()` (the identity swap) covers `settling` as well as
`pending` for the same reason `claim` re-issues a stale one: a claim whose
holder died mid-write comes back after `_CLAIM_STALE_SECONDS`, and a row left
behind by the swap would then apply the *old* instance's provider ids to
whatever the new one matches its digest to.

Every remaining filesystem check on these paths is bounded the same way:
`normalize_rules_bounded` (the save-time output-directory validation, inside
`_sweep_lock`), `_within_volumes_bounded` (the sweep's re-check of a stored
directory), and the re-pin plan's batch resolve, which is scaled by batch size
like the catalog scan. Each degrades toward *refuse*: an output directory
nobody can resolve pauses its rule or skips its platform, and a submitted path
that does not answer is skipped — the same answer a path outside the volumes
gets, which is what an unreachable one effectively is.

The change and its cleanup **commit together or not at all**. They are two
operations and either can be the one that survives a crash: cleanup-then-save
leaves the old identity live with its history gone and its snapshots retired,
while save-then-cleanup leaves the retry comparing the new values with
themselves — concluding nothing moved, and leaving the previous instance's ids
live against the new one forever. So `romm_settings.save(patch,
cleanup_pending=True)` writes the marker in the *same row* as the new identity,
`_run_identity_cleanup()` clears it only once the work is done, and
`replay_identity_cleanup()` finishes an interrupted change at startup. Both
halves of the cleanup are idempotent, so replaying costs nothing.

Startup is not the only replay point: the settings route runs the cleanup when
`changed` **or** `romm_settings.cleanup_owed()`. Retrying the same save
otherwise finds the new identity already cached, computes no change, and
returns success while the marker still says the old instance's records are
live.

The re-pin **plan** reads the catalog and then writes rows carrying that
catalog's provider ids, so it has to be sure the catalog is still the one in
use. `romm_settings.identity_generation()` is read up front and re-checked
before recording: a change landing in that window means the ids belong to an
instance nobody is pointed at, and the plan answers 409 rather than recording
them. A batch converted without a snapshot needs a manual re-match, which is
recoverable; a row holding another library's ids is not.

A re-check is not a guarantee, though — it is true at an instant, and the
inserts come after it. A save landing in between could install the new identity
*and finish its cleanup* while the rows were still going in, leaving them alive
against the new library, holding the old one's ids, and invisible to the pass
that was supposed to retire them. So the plan is split: the fingerprint probes
run unlocked (they are bounded filesystem reads, and holding a lock the settler
and the settings route both need across a dead mount, once per destination,
would trade this race for a much longer stall), and **only the inserts run
under `_settle_lock`** — the lock `_run_identity_cleanup` requires — with the
generation re-read inside it. That is what makes "the identity has not moved"
true *for the duration of* the writes.

**And while a cleanup is owed, nothing acts on the records it has not reached
yet.** The marker can outlive startup: `replay_identity_cleanup()` is
best-effort, a briefly locked SQLite file is enough to fail it, and the app
starts anyway by design. Until it lands, the conversion history and the pending
re-pin rows still belong to the previous instance — so `sweep()` refuses
(`identity_cleanup_pending`) rather than reading the old library's history, the
plan route answers 409 rather than adding a row the cleanup has already passed
over, and the settler skips its tick. The settler is also where the **retry**
lives: it takes `_settle_lock` and the sweep pause, runs the cleanup, and only
then proceeds. A sweep cannot retry it — it holds `_sweep_lock` and so is the
one caller that cannot take those — which is why the refusal and the retry sit
in different places.

Deciding *whether* the identity moved is itself bounded (`_identity_moved`),
because it resolves both library roots and the old one is the unresponsive
mount as often as not — that is why the operator is changing it. It runs
detached under `_IDENTITY_PROBE_SECONDS` and falls back to comparing the
normalised spellings, while the request holds `_settle_lock` and the sweep
pause. The fallback errs toward *moved*: two spellings of one directory then
cost a redone conversion history, where the opposite mistake writes one
library's provider ids onto another library's game.

**"Has this rule already converted this source?"** `skip` is idempotent from the
destination alone, but the other two policies are not: `overwrite` resolves an
occupied destination as queueable, and `rename` always finds a free suffix. Both
would therefore reconvert the whole library every interval.

What is recorded is **production, not queueing**. `converted` on the platform's
`romm.auto_state` entry maps each RomM id to `{path, pre}` — the destination the
rule chose and a fingerprint of whatever occupied it at planning time — and
`_was_produced()` answers it in three steps, strongest evidence first: the
verdict written down when the job ended (`done`), then the job's live status if
the queue still remembers it, then — only for a record predating either — the
fingerprint having changed.

The written-down verdict is what makes the rest trustworthy. A *failed*
`overwrite` job changes the destination just as visibly as a successful one, by
unlinking the old artifact or leaving a partial file, so the fingerprint alone
cannot tell them apart and the ROM would be skipped by every later sweep until
**Forget history**. `job_manager.add_terminal_listener` (§3.4) hands
`note_job_finished` the outcome at the moment it is certain; the sweep also
freezes any outcome the queue can still answer for, which covers a job that
finished before its record existed. Neither has to be kept in sync with the
queue: a missing verdict degrades to the older evidence rather than to a wrong
answer.

It is invalidated where it stops being true: `set_rules` drops it for any
platform whose `OUTPUT_IDENTITY_FIELDS` changed — a rule retargeted from RVZ to
GCZ is asking for a different file — and does so **under `_sweep_lock`**, since
a sweep running concurrently finishes by writing its own ids and schedule stamp
under the same key. `routes/romm.py` also clears it when `url` or `library_root`
changes, because a different RomM database reuses the same integer ids for
different games. `forget_converted()` (exposed as
`POST /api/romm/rules/forget-converted`, with a per-platform button in the
automation editor) is the operator's escape hatch after restoring a backup.
`_record_run` merges into the entry rather than replacing it, or every run would
forget the history it shares a key with.

**"What did the tool actually produce?"** A row also stores its `mode`, so the
settle pass can ask the owning tool through `companion_outputs`. makeps3iso's
`-s` build only splits past 4 GB — under it, the bare `.iso` appears and the row
settles normally; over it, only `<name>.iso.0`, `.1`, … exist, which from the
recorded path alone is indistinguishable from a conversion that never ran. RomM
matches a ROM on one file's hash, so a part set has no digest to join on: the
row is retired immediately with that as its reason rather than waiting out
`repin_abandon_days` and reporting "output never appeared".

### 3.3.9 Verify without delete (`ConversionJob.verify_after`)

`delete_on_verify` verifies as a *precondition* for deleting the source. A
caller that wants the check on its own — an unattended sweep proving its output
before it walks away — sets **`verify_after`** instead. One block in
`_process_job` runs the verification for either flag, so a verified output means
the same thing however it was requested, and only `delete_on_verify` reaches the
delete half.

The capability gate applies to the delete, not to the check. `verify_after`
simply runs the tool's verification and keeps the source, so any mode whose tool
can verify may ask for it. `delete_on_verify` must additionally satisfy
**both** `ModeSpec.supports_delete_on_verify` ("can this mode's output be
verified at all") and `ToolPlugin.delete_on_verify_is_safe(mode, compression)`
("can *this* job's settings prove it") — re-asked in `_process_job` itself,
because that is where the source is unlinked and a job can arrive there without
having passed either plan-time check.

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
        return [t for t in self._tools.values()
                if match_extension(filename, t.input_extensions) is not None]
    def tool_for_verify(self, path: str) -> ToolPlugin | None:
        return next((t for t in self._tools.values()
                     if match_extension(path, t.verify_extensions) is not None), None)
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

#### Platform narrowing (`narrow_to_platform`, `ToolPlugin.platform_slugs`)

Extension matching decides *convertibility*, which is all a bare filesystem
listing can know. A library manager knows more: RomM stamps a `platform_slug` on
every ROM record, and that is the only thing able to tell a GameCube `.iso` from
a PS2 one.

Each tool declares the platforms it serves as `platform_slugs: frozenset[str]`
(dolphin: `{"ngc", "gamecube", "wii"}`; maxcso: `{"psp", "ps2"}`; the default is
an empty set, meaning "no opinion"). `narrow_to_platform(tool_ids, slug)` then
*removes* candidates the platform contradicts — it never adds any.

It is conservative by construction, because a wrong exclusion is worse than a
missing one. With no slug, an unrecognised slug, or a slug no tool claims, the
list is returned untouched; a tool declaring no `platform_slugs` is never
dropped. So a platform we have never heard of degrades to plain extension-only
behaviour rather than to an empty list. Both consumers go through it: the RomM
catalog listing narrows each row's `convertible_by`, and `/romm/platforms`
returns the narrowed `tool_ids` so the automation editor offers a platform only
the modes it can actually use.

#### Operator-authored filter patterns

The automation rules take real regexes — "convert only `(USA)`" is the feature —
which makes them the one place user input becomes executable. Two bounds, and
one accepted alert.

`_valid_pattern` caps the source at `_MAX_PATTERN` characters, refuses one that
does not compile (refuses, never drops: a rule whose filter silently vanished
would come back as *unfiltered*, and the next unattended sweep could queue the
whole platform with delete-on-verify attached), and then refuses one that
backtracks. `re` cannot be interrupted, so a pattern like `(a+)+$` would hold
`_sweep_lock` indefinitely — blocking previews, manual runs, and the very rule
edit that would remove it.

**`_has_nested_quantifier` reads the pattern rather than running it.** The
obvious implementation times the regex against an adversarial string, and that
was the first attempt; it is wrong three ways. It has to survive the very
blow-up it is looking for (the runtime doubles per character, so measuring at
full length hangs the check). It is load-dependent, so a busy machine rejects
patterns that are fine. And executing an operator-supplied regex is exactly what
CodeQL's `py/regex-injection` flags — the query's only sanitizer is `re.escape`,
which would turn "convert only `(USA)`" into a literal search and delete the
feature. A structural test avoids all three: it looks for a quantified group
whose body itself repeats (`(a+)+`, `(\w+\s?)*`) or is an alternation
(`(a|a)+`), which are the shapes with exponentially many ways to split one
input. Escapes and character classes are stripped first, so a literal `\+` or a
`[+|]` class is not mistaken for a quantifier.

**The two quantifier sets are not the same set.** What may follow a *group*
is `*+{` — `(...)?` cannot blow up, because the outer level consumes at most
once. What counts *inside* a group that is already repeated unboundedly is
`*+{?`, because there every optional atom doubles the ways the engine can split
the same input: `^(a?){30}a{30}b$` compiles, looks tame, and walks
exponentially many choices against a long run of `a`. Bounded repetition is not
a reprieve — `{30}` is thirty levels of doubling. Getting this wrong is not just
a slow sweep: the probe thread is abandoned rather than stopped, so repeated
scheduled retries eat into the process-wide probe capacity that unrelated
filesystem checks share.

A group's leading `?` is stripped before that test, because in `(?:…)`, `(?=…)`,
`(?<!…)`, `(?P<name>…)` and inline-flag groups it is *syntax*. Reading it as an
optional atom would refuse every non-capturing group — and a refused filter
disables the rule, so an operator who cannot write `(?:USA|Europe)` writes
something looser instead, which is the widening this validator exists to stop.

It is conservative on purpose — `(USA|Europe)+` is harmless and still refused —
because being wrong at save time costs one clear message, while being wrong the
other way wedges the scheduler.

#### Composite modes narrow per mode (`ModeSpec.platform_slugs`)

Narrowing by tool cannot separate a composite tool's modes. `ChainTool` is a
shell that belongs to no system: it owns `nkit_to_rvz` (GameCube/Wii) and
`cso_to_chd` (PS2/PSP), so it legitimately survives `narrow_to_platform` on
both — and offering *both* modes on *both* is how a GameCube disc gets a rule
targeting a PS2 format.

**`ModeSpec.platform_slugs`** (default empty, meaning "ask the tool") lets a
mode name its own systems. `mode_allows_platform(mode, slug)` applies the same
conservative rules as `narrow_to_platform` one level down, and
`modes_for_platform(slug)` returns the whole applicable set — served to the
browser as each platform's `mode_ids` beside its `tool_ids`, so neither the
automation editor nor the RomM target picker carries a second copy of the
platform table. The sweep checks the *mode*, not `spec.tool_id`.

Composite tools are not the only case. A tool whose modes are *different
commands over different media* has to declare per mode too: CHDMAN's
`createcd` writes a CD track layout and `createdvd` a flat DVD image, and
inheriting the tool's combined slug set offered a PS2 catalog both — with
`createcd`, the first CREATE mode, as the default an ISO submission landed on.
`chdman.py` therefore splits its platforms into `_CD_PLATFORMS` /
`_DVD_PLATFORMS` / `_HD_PLATFORMS` / `_LD_PLATFORMS`, gives each create and
extract mode the set its command actually serves, and derives the tool-level
`platform_slugs` as their union so the two levels cannot drift. `copy` declares
nothing and inherits: recompressing a finished `.chd` is media-agnostic.

The rule for a new tool: declare `platform_slugs` on the *mode* whenever two of
a tool's modes would be wrong for each other's platforms, and let the tool-level
set be the union.

#### What may run unattended (`registry.mode_is_automatable`)

A rule fires repeatedly over a library RomM rescans, so a mode is only
automatable if that loop **terminates**. The one that does not is CHDMAN's
`copy`: `Game.chd` becomes `Game_copy.chd`, RomM lists it, the same rule
accepts it as a source, and the next sweep writes `Game_copy_copy.chd` — full
size, once per sweep, until the volume fills. Nothing downstream stops it,
because the destination is new every round: the duplicate policy and the
converted history both agree it is work not yet done.

The test is not "does it accept its own output" but **where would it write
it**. `dolphin_rvz` over a `.rvz` resolves to the *same* path, which the
queue's same-path guard refuses — so it settles rather than multiplying. The
registry therefore asks the tool: derive the output path for a synthetic source
carrying the mode's own output extension, and refuse the mode when the answer
differs from it. Derived, so a future tool of the same shape is excluded
without anyone remembering.

Enforced in two places, because a rules blob can be written straight into the
database: `normalize_rule` drops such a rule, and the editor omits the mode
(served through `/romm/status`'s `automatable_modes`, since the derivation
needs the tool's `output_path` and cannot be mirrored in JS). Extract modes are
a *separate*, UI-side exclusion — they terminate, and rules using them work.

#### Asking a tool whether it can run (`registry.tool_is_ready`)

`ToolPlugin.is_ready()` is a coroutine, which makes it look bounded and does
not make it so. NSZ's delegates to a pooled `keys_available()` that walks every
configured volume looking for `prod.keys`, with no deadline of its own: on an
NFS/SMB/rclone mount that has stopped answering it blocks in uninterruptible
I/O and holds a shared-pool worker while every caller waits behind it.

The callers all fan out over the whole registry, so the cost is a whole screen.
`GET /api/tools` — the sidebar's tool list — awaited each tool *in turn*, so one
dead volume held up every tool that was fine behind it.
`GET /api/romm/platforms` gathered them, leaving the Library and the automation
editor loading forever. Retry either and each attempt stranded another worker.

**Ask through `registry.tool_is_ready(tool)`, never `tool.is_ready()`
directly.** It bounds the probe at `READY_PROBE_SECONDS` (20s — generous,
because the honest answer for NSZ is a real walk of a possibly large share) and
reports **not ready** when it expires: the same answer a missing binary gets,
and the right one, since a tool whose prerequisites cannot be read cannot
convert. `registry.ready_tool_ids()` is the fan-out form, concurrent rather
than sequential. The RomM sweep's `_ready_bounded` is a thin alias kept for its
local consequence: the sweep holds `_sweep_lock` throughout, so a hang there
also blocks previews, manual runs, rule edits and settings saves.

**And the other half is the plugin's.** The bound frees the *caller*, and only
the caller — Python can abandon an awaiter, never an OS thread. Whose thread is
abandoned is decided by the implementation, so **an `is_ready()` that touches
the filesystem must use `run_detached`, not `run_in_threadpool`.** A pooled
worker wedged on a dead mount is never handed back, and since every caller fans
out over the whole registry, repeated probes retire the pool one worker at a
time until unrelated offloads have nowhere to run — the bound turns a hang into
a leak rather than removing it. `nsz` (a recursive walk for `prod.keys`) and
`jwud` (a stat for the `.jar`) are the two that touch the disk today; the
requirement is stated on `ToolPlugin.is_ready` so a new gated tool inherits it.

#### Resolving what to verify (`_verify_target_bounded`)

`verify_target()` answers "which file actually holds this conversion's output"
— for makeps3iso, a stat of the bare ISO plus a scan for numbered split parts.
Cheap, and dangerously placed: it runs after the conversion and *before*
`verify_timeout()` is resolved, so it sits ahead of every bound the verify
itself carries, on storage that has just taken a multi-gigabyte write and is
therefore the likeliest thing in the process to have stopped answering.

Blocked there the job holds its output lock and its source-directory lock,
`_verifying` is still empty so the watchdog has nothing to report, and at the
default `MAX_CONCURRENT_JOBS` of 1 the whole queue stops. So it goes through
`run_detached` under `asyncio.wait_for` — an abandoned disposable thread rather
than a pooled worker — and takes the job's `cancel_event`, because a Cancel
pressed while a mount is silent must return now rather than after the probe
bound. An expired bound fails the job with a message naming the storage; a
cancel raises `ConversionCancelled`, the same translation the verify makes, so
nothing is judged and the source is not deleted on it.

#### A platform claim is a promise about extensions

`platform_slugs` and the modes' `input_extensions` are two halves of one
statement, and nothing enforces the join — so they can disagree, and the
disagreement is invisible until an operator hits it. ROMZ claimed NES, SNES,
N64, Genesis, Game Gear and a dozen more while accepting only `.gb`, `.gbc`,
`.gba` and `.nds`. RomM offered the ROMZ modes on an NES platform, every `.nes`
row was unselectable, and an enabled automation rule skipped the whole platform
as unconvertible on every sweep — silently, because "nothing to do" and
"nothing I *can* do" report the same way.

**Widen the extensions rather than dropping the slugs**, whenever the tool can
honestly serve them: ROMZ is a `7z` wrapper with no per-format logic, so a
`.nes` packs exactly as a `.gba` does. Dropping the claim would have been the
smaller diff and the worse answer — it removes a capability to make a doc
string true. And keep the two lists collision-free against other tools:
`.bin` stays chdman's, because a Genesis dump sharing an extension with a CD
track is not a collision worth creating.

The frontend mirrors these lists in `src/lib/tools/registry.js`, and
`tests/test_frontend_parity_186.py` fails when the two drift — so widening a
tool's inputs is a two-file change by construction.

#### What counts as a source (`registry.mode_input_kind`)

`ModeSpec.input_kinds` is a set, and "does this mode take a file or a
directory" was re-derived from it at each seam. **`mode_input_kind(mode)`** is
the one answer, used by the queue (which carries it end-to-end so the pipeline
skips the file-only assumptions), by the sweep's declaration check, and by the
sweep's existence check.

That last one matters because existing is not the same as being the right kind.
The declaration check is extensions and the tool's predicate, so a directory
whose *name* carries an accepted extension passes it, and a plain
`os.path.exists()` then let it into the queue. The manual batch route stats for
a regular file; automation has to as well, because nothing is watching an
unattended sweep — an `overwrite` rule authorises the job,
`_clear_existing_output` removes the previous artifact, and only then does the
converter fail on a directory it cannot open.

#### How a job ended, after the queue forgets (`add_terminal_listener`)

`JobManager.jobs` is capped (`max_job_history`) and lives in memory, so
`get_job(id)` answers "unknown" for anything pruned or predating a restart.
Any consumer that needs to know how a job *ended* — not whether it exists —
therefore cannot ask later; it has to be told at the time.

Ownership is registered the instant the queue accepts a batch (`_own_jobs`),
before the first await after acceptance — the listener fires from the queue
worker, so a fast conversion on an idle queue can end while the post-queue
bookkeeping is still running. The *record* it writes into does not exist until
a few awaits later, so a verdict arriving in between is stashed in
`_late_verdicts` and applied by `_mark_converted`. Dropping it instead left the
row with no verdict at all, after which the only evidence is the destination
having changed — which a *failed* overwrite produces just as convincingly.

**`job_manager.add_terminal_listener(callback)`** registers a callback for every
job reaching `COMPLETED` / `FAILED` / `CANCELLED`. Delivery is **at least once**,
not exactly once. It fires from
`_process_job`'s `finally` (every runner outcome), from both
`finish_external_job*` paths, from `cancel_job`'s QUEUED branch, and from the
four early returns that precede `_process_job`'s try block (two cancels, the
output-lock failure and the split-set collision) — which is why a job cancelled
before it starts is announced twice. The callback
may be sync or async, exceptions are logged and swallowed (a listener must
never fail a conversion), and the same job may be announced twice — listeners
are required to be idempotent.

The RomM automation is the first consumer: `romm.auto.note_job_finished` writes
the verdict into the rule's converted-history record (`done: true|false`), and
`_was_produced` reads that first. Without it, provenance fell back to "the
destination changed since planning", which a *failed* overwrite produces just
as well as a success — by unlinking the old artifact or leaving a partial one
— and the ROM would be skipped by every later sweep until **Forget history**.
The sweep also freezes any outcome the queue can still answer for
(`_persist_known_outcomes`), covering the window before a record exists.

The listener fires for **every** job in the app, most of them manual, so it
cannot afford to search: `_mark_converted` files the `job id -> (platform,
rom)` pairing in `_job_owners` as it queues, and `note_job_finished` pops that
one entry. A miss means the job is not the automation's — a record can only
name a job this process queued, since the queue is in memory — so there is
nothing to write and nothing to scan. The alternative walked every remembered
ROM of every platform per completion, on the event loop under `_sweep_lock`.

#### A codec for "tool default" (`ToolPlugin.default_compression`)

The compression *level* is not a separate argument anywhere in the pipeline: it
rides on the compression string as `"codec:level"`, which the tool splits back
apart. A level with no codec therefore serialises as `":19"`, and whether that
is meaningful is a per-tool fact:

- Where a mode declares `supports_compression=False` but
  `supports_compression_level=True` — nsz, whose dropdown picks a solid/block
  *layout* rather than a codec — the tool reads the empty part as its own
  default and honours the level.
- Where a mode offers codecs (`supports_compression=True`), the empty part is a
  blank, not a default. `DolphinTool._build_convert_command` emits
  `-c "" -l 19`, which dolphin-tool refuses.

**`ToolPlugin.default_compression: str | None`** names the codec to use in the
second case, so the resolution stays a per-tool declaration instead of an
if-ladder on tool identity at the call site.
`registry.default_compression(mode)` applies it *only* to codec-offering modes,
returning `None` elsewhere so nsz's meaningful empty part survives. The RomM
sweep (`services/romm/auto._compression_arg`) is the current consumer: with no
declared default it drops the level and logs, rather than queue a job the tool
will reject or invent a codec the operator did not choose.

The value mirrors `defaultCompression` in `src/lib/tools/registry.js`, which
seeds the same choice in the manual picker — `tests/test_frontend_parity_186.py`
fails on drift, and on a codec+level mode that declares no default at all.

#### Extension matching is a suffix match (`utils.path_utils.match_extension`)

Every "does this tool handle this file?" decision goes through one helper:

```python
def match_extension(name: str, extensions: Iterable[str]) -> str | None:
    """The longest declared extension ``name`` ends with, else None."""
```

It replaced the `Path(name).suffix.lower() in declared` test that used to be
re-typed at each site (`tools_for_input`, `tool_for_verify`,
`BaseTool.verifies_path`, the archive-member gate in `services/archive.py`, and
the plan-time input gate in `routes/convert.py`). The reason is **compound
extensions**: nkit2iso's sources are `.nkit.iso` and `.nkit.gcz`, whose trailing
component is the generic `.iso` / `.gcz` that CHDMAN, Dolphin and maxcso already
own. A single-component match can only see the generic tail, so the specific
format could not be declared without either over-claiming every ISO or
hard-coding a content sniff.

Three properties make this safe to apply everywhere:

- **Single-component declarations are unchanged.** `game.iso` ends with `.iso`
  and nothing else, so every pre-existing tool matches exactly what it did
  before. (`tests/test_compound_extension_matching.py` pins this.)
- **Per-tool, not global.** Each tool is asked independently, so an NKit image is
  claimed by nkit *and* by the generic-tail owners, the same way a raw `.iso` is
  already claimed by chdman, dolphin and cso. The user picks; nothing is
  displaced.
- **Longest match wins within a tool**, so a caller that needs the concrete key
  (the archive listing) gets the most specific answer rather than an arbitrary
  one.

The subject may be a full path, a bare filename, or an extension string
(`".nkit.iso"` still ends with `".iso"`), so a caller holding only a member's
recorded extension uses the same helper.

Two display-side sites deliberately still record the plain `Path.suffix`: the
archive listing's `entry["extension"]` and, downstream of it, the frontend icon
buckets. They want the generic tail. Convertibility for those rows is re-derived
from the member's *name* via `registry.tools_accepting_archive_member(member)`,
which takes a name-or-extension for exactly this reason.

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
            # Every await that follows the token lives inside this try, so the
            # finally is already registered when the first one runs: a client
            # disconnecting during tool.verify_timeout(path) still releases the
            # lane's only slot.
            bound = await tool.verify_timeout(path)          # issue #266
            r = await asyncio.wait_for(tool.verify(path), timeout=bound or None)
            # `valid` alone, so neither a timeout nor a cancelled run (which
            # reach no verdict) is ever recorded as a verification.
            if r.get("valid"): await verification_store.mark_verified(path)
            return r
        except asyncio.TimeoutError:
            return _verify_timed_out(bound)   # a verdict-shaped answer, not a 500
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
the declared-extension match (`match_extension`, §3.4), so existing tools are
unaffected; `RomzTool`
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

#### Multi-file sources (`converts_path` / `source_companions`)

The two hooks above describe the *output* side per-file. `jwud` needed the same
treatment on the **input** side, because a split Wii U dump is one disc image
spread over twelve files: JNUSLib's `WUDDiscReaderSplitted` reads
`game_part1.wud` … `game_part12.wud` (11 parts of exactly 2 GiB plus a
1,402,994,688-byte twelfth) and joins them when you hand it part 1. Two shared
paths assumed a source is exactly one file, and both got it wrong for a set:

- **The listing** derived `convertible_by` from `ext in tool.input_extensions`,
  so all twelve parts were offered as sources. Eleven of them can only fail —
  a part on its own is not a disc image.
- **The delete planner** (`utils/delete_plan.py`) collected the source plus, for
  a `.cue`/`.gdi`, its parsed track files. A verified conversion with
  delete-on-verify would therefore remove the part the job named and orphan the
  other eleven — ~23 GB of silent leftovers.

`ToolPlugin.converts_path(path) -> bool` is the input-side mirror of
`verifies_path`: `BaseTool` defaults it to the plain `input_extensions` match
(so the seven pre-existing tools are unchanged), and `JwudTool` returns `False`
for a secondary part that has its primary beside it. The registry exposes
`tools_converting_path(path)` next to `tools_verifying_path`, and
`routes/files.py` feeds it into the same tool-neutral `FileEntry.convertible_by`
list — so the secondary parts stay **visible** (you can still see and delete
them) but are never offered for conversion, the same shape as the
visible-but-not-convertible archive members in §17.5 of the adding-a-tool guide.

`ToolPlugin.source_companions(path) -> list[str]` is the input-side mirror of
`companion_outputs`: the sibling files this input also consumes, never including
`path` itself, `[]` by default. `build_delete_plan` adds
`registry.source_companions(source)` to its delete set, so removing a split
source takes the whole set. A gap in the set truncates it — JNUSLib reads the
parts in order, so a set missing part 3 is broken rather than an 11-part set,
and the stragglers must not be reported as belonging to a usable source.

Both hooks may touch the disk (they look for the sibling primary / enumerate the
set), so they run in the `files.py` threadpool scan and inside
`build_delete_plan`, never on the event loop.

A set also costs two things a single-file source never did, and both are
registry-driven rather than jwud branches:

- **Containment.** `source_companions` is *name*-derived, so a companion can be
  whatever the filesystem puts under that name. The converter opens them itself
  — JNUSLib enumerates the parts from part 1 rather than taking a list from us —
  so the volume boundary the route enforces on the submitted path has to be
  enforced on the companions too. `utils.path_utils.source_companions_are_safe`
  rejects a symlinked companion outright (rather than following it) and any
  companion resolving outside the configured volumes.

  Two properties make it correct, and both were review findings:

  * **Checked twice, like the directory case.** `plan_job` rejects at queue time
    with `SOURCE_COMPANION_UNSAFE`, and `_process_job` re-checks after taking
    the job's locks — a queued job's `game_part2.wud` can be swapped for a
    symlink in between, and the plan-time answer would be stale. The re-check
    runs *before* clearing any existing output, so a rejection on an overwrite
    job leaves the user's prior file intact. This is the same shape (and the
    same ordering rationale) as the `is_safe_directory_tree` re-check that
    guards makeps3iso's recursive read.
  * **Scoped to the mode's own tool**, not the registry-wide union that
    `build_delete_plan` uses. Deletion should take everything any tool considers
    part of the source; conversion should consult only the tool that will read
    it, or a chdman `createhd` on a raw file that happens to be named
    `game_part1.wud` would be rejected over a sibling chdman never opens. A
    no-op for the tools whose `source_companions` is `[]`.
- **Scan cost.** `.wud` is a produced output, so `scannable_extensions` puts
  every `game_partN.wud` in the DAT-match walk — twelve 2 GiB files SHA1'd in
  full, ~25 GB per set, to match nothing, because a part is a slice of a disc
  image rather than one. `ToolPlugin.scannable_path(path) -> bool` (default
  `True`) is the per-file veto on that walk, the scan-side analogue of
  `converts_path`; the registry drops a path any tool rejects, since a file some
  tool knows is not a standalone artifact cannot match a DAT whatever the others
  think. It may do *bounded* disk I/O: the veto is on membership of a real set
  rather than on the name, so a lone `game_part1.wud` stays scannable and the
  same path stops being scannable once its siblings appear — which a pure
  filename check could not express, and which keeps the hook agreeing with
  `converts_path` and `output_stem`. It runs once per candidate across the whole
  library, so implementations stay to a stat or two and never a read, inside the
  threadpool scan walk rather than on the event loop.

**Naming the set's product is a third disk-reading decision.** Part 1 of a set
produces `game.wux`, not `game_part1.wux` — the parts are one image and the part
number is meaningless once they're joined. But that rewrite is only correct when
the set can actually *be* joined, so `JwudToolService.output_stem` gates it on
`split_set_is_complete()`: parts 1…N present with no gap **and** summing to
exactly `WUD_IMAGE_SIZE`, which is what JNUSLib itself requires. A half-finished
dump, a set with a hole in it, an orphan part, and an archive member (whose
synthesised path has no set behind it on disk) all keep their own stem. The
reason is `allow_overwrite`: an input that claims `game.wux` but cannot produce
it would let the worker unlink an unrelated finished image *before* JWUDTool
failed.

Disk state answers that for a real path, but an **archive member's path is
synthesised** — the location it would occupy once extracted — so reading its
directory reads someone else's files. `ToolPlugin.detect_output` therefore takes
`from_archive` (default `False`), the listing-side counterpart of the
`treat_as_stem` `plan_job` already passes for the same member;
`_detect_archive_member_outputs` sets it and `JwudTool` forwards it to
`output_path`. The other seven tools ignore it, because they swap a suffix and a
synthesised path resolves like a real one. It lives on the seam rather than in
jwud because the property is general: extraction hands over one member and never
its siblings, so a member can only ever produce a single-file output — a future
tool whose output name depends on its input's neighbours would otherwise inherit
the wrong answer silently. Without it, an archive holding `game_part1.wud` next
to a genuinely extracted set badges the set's `game.wux` while the planner
targets `game_part1.wux`.

#### Per-job delete-on-verify guard (`delete_on_verify_is_safe`)

`ModeSpec.supports_delete_on_verify` answers "can this *mode* offer it?", which
was enough while every tool's `verify()` read the whole output. jwud breaks that
assumption: WUX carries no content checksums, so its verify walks the container's
structure, and what actually justifies deleting a 25 GB source is JWUDTool's
byte-for-byte comparison *during* the conversion — which the job can switch off
with `-noVerify`. The two settings together would delete the only copy of a disc
image on the strength of a geometry check.

`ToolPlugin.delete_on_verify_is_safe(mode, compression)` answers the narrower
"can this *job* offer it?". `BaseTool` returns `True`, so every other tool is
unchanged; `JwudTool` returns whether the job kept verification on.
`routes/convert.py::_validate_request_compression` — already the shared
request-level validator for both the single and batch endpoints — rejects the
unsafe combination with a 400, so the two paths can't drift.

It lives on the plugin rather than as a `spec.tool_id == "jwud"` branch for the
same reason `is_ready` did: a second tool whose verify is weaker than its
conversion-time check would otherwise silently inherit "safe".

> **Not yet folded in: `.cue`/`.gdi` tracks.** `build_delete_plan` still parses
> those out of the file's *contents*, with its own unsafe-reference handling
> (absolute paths, refs escaping the source directory) that the naming-derived
> hook has no equivalent for. Routing chdman's track files through
> `source_companions` is the obvious next consolidation, but it would move that
> security surface — and the `unsafe_paths` wire strings tests pin — so it is
> deliberately left for its own change.
#### Source-tool ordering is most-specific-first

`registry.toolsForSourcePath` / `infoToolsForPath` rank claiming tools by the
**length of the matched `sourceExts` entry**, not declaration order. The Info
modal walks that list and keeps the first `getInfo()` that returns, so with a
plain order a `.nkit.iso` would reach Dolphin's header reader first — and that
reader *succeeds*, because an NKit image carries a genuine GC/Wii disc header,
reporting the shrunk file as if it were an ordinary disc. Ranking the compound
claim above the generic tail it shares (`.iso`) puts the tool that actually
understands the container first. This mirrors the backend's longest-match
`match_extension` (§3.4).

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
