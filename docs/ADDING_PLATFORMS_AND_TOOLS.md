# Adding Platforms and Compression/Decompression Tools

This is a developer guide for extending Compressatorium with **new conversion
tools** (a brand-new compressor binary) and **new platforms / formats** (a new
chdman create mode, or a new file type a tool can handle). It walks the full
vertical slice, from the binary in the Docker image, through the Python service
and tool plugin, the job pipeline, the FastAPI routes, and finally the Svelte
web UI, and shows how to add a tool and a platform in tandem.

It is written against the codebase as it stands today, with seven real tools
plus one synthetic pipeline tool already wired up:

| Tool id | Binary | Service module | Plugin | Handles |
|---------|--------|----------------|--------|---------|
| **`chdman`** | `mame-tools` (`/usr/bin/chdman`) | `app/services/chdman.py` | `app/services/tools/chdman.py` | CD/DVD/HD/Raw/LaserDisc game images to/from `.chd` |
| **`dolphin`** | `dolphin-emu` (`/usr/local/bin/dolphin-tool`) | `app/services/dolphin_tool.py` | `app/services/tools/dolphin.py` | GameCube/Wii images to/from `.rvz/.wia/.gcz/.iso` |
| **`z3ds`** | built from source (`/usr/local/bin/z3ds_compressor`) | `app/services/z3ds_compress.py` | `app/services/tools/z3ds.py` | Nintendo 3DS ROMs to/from `.zcci/.zcia/.z3ds/.zcxi/.z3dsx` |
| **`nsz`** | `nsz` pip package (on PATH) | `app/services/nsz.py` | `app/services/tools/nsz.py` | Nintendo Switch `.nsp`/`.xci` to/from `.nsz`/`.xcz` |
| **`cso`** | maxcso, built from source (`/usr/local/bin/maxcso`) | `app/services/maxcso.py` | `app/services/tools/maxcso.py` | PSP/PS2 `.iso` to/from `.cso` (v1/v2) / `.zso` / `.dax` |
| **`romz`** | `p7zip-full` (`7z` on PATH) | `app/services/romz.py` | `app/services/tools/romz.py` | Handheld ROM `.gb`/`.gbc`/`.gba`/`.nds` to/from `.7z`/`.zip` |
| **`makeps3iso`** | built from source (`/usr/local/bin/makeps3iso`) | `app/services/makeps3iso.py` | `app/services/tools/makeps3iso.py` | A decrypted PS3 folder (`PS3_GAME/` layout) packed to `.iso` — **directory input** |
| **`chain`** | *(none — drives the tools above)* | *(none)* | `app/services/tools/chain.py` | Composite `cso_to_chd`: `.cso/.zso/.dax` → `.iso` → `.chd` as one job |

Note the **tool id is not always the binary name**: maxcso registers as `cso`,
7z registers as `romz`. The id is what the registry, the API and the frontend
key on; use it consistently.

> **Three tool shapes.** Most tools take a file matched by suffix, and that is
> the path this guide walks. Two other shapes sit on the same plugin/registry
> contract and have their own seams: a **directory-input** tool (`makeps3iso`)
> selects a folder through `accepts_directory` / `InputKind` instead of a
> suffix, and a **chain** tool (`chain`, whose only mode is `cso_to_chd`) runs
> two existing tools as one job through a `ChainSpec`. If you're adding either
> of those, read the file-suffix walkthrough below for the shared plumbing
> (routes, job pipeline, frontend registry), then see the two seam writeups in
> `docs/DESIGN_tool_plugin_architecture.md` (§3.3.3 cross-tool chaining and
> §3.3.4 directory inputs) for the parts that differ. §7 below is a "which of
> these should I copy?" decision table.

> **`romz` is the "produces archives / reuses an existing binary" example.** It
> needs no Dockerfile build step (the `7z` CLI already ships via `p7zip-full`)
> and reuses `app/services/archive.py`'s `zipfile`/`py7zr` for the read side
> (member listing, info, single-member validation), shelling `7z` only for the
> write/extract/test paths via the shared `SubprocessRunner`. Its `.7z`/`.zip`
> inputs overlap the archive-browse extensions, so its modes set
> `allows_archive_input=False` and `routes/files.py`'s existing `is_archive`
> guard keeps those files classified as browseable archives, not convertible
> sources. And the ROM packed inside a romz archive shows up free when you browse
> in — no special flag needed, since the archive listing is global, scoped to
> *known* extensions, and handheld-ROM extensions are romz sources (otherwise a
> single-ROM `.zip` reads as an empty folder despite the ARC/OK badge). It's
> visible only, mind: never offered for in-place recompression, which'd be plumb
> recursive.

`z3ds` is the cleanest, most self-contained example of "a new tool that handles
a new platform," so this guide uses it as the reference implementation
throughout. When in doubt, **copy what z3ds does.**

> **The `nszip` example below is now real.** This guide was written before
> Switch support existed and uses a *fictional* `nszip` tool as its §5
> walkthrough. That tool now ships for real as **`nsz`** (the `nsz` row above). The
> walkthrough still teaches the generic pattern, but the real `nsz`
> implementation differs from the sketch in three ways worth knowing:
>
> 1. **Packaging is pip, not a g++ build.** `nsz` is a Python package added to
>    `requirements.txt`; it lands on PATH in the venv. No builder-stage compile,
>    no `.local-bin/` copy. `nsz_path` defaults to the bare name `nsz`.
> 2. **It needs user-supplied `prod.keys`.** Switch content is encrypted, so nsz
>    decrypts (losslessly, reversibly) before compressing. The app ships no
>    keys; the operator mounts their own and sets `SWITCH_KEYS` to the directory
>    holding them (else the app best-effort searches `~/.switch` and the
>    volumes). nsz loads keys at import from `~/.switch/prod.keys` with no
>    `--keys` flag, so the service runs it with a temp `$HOME` symlinked to the
>    resolved key file, and fails fast with a clear message when keys are
>    missing. No other tool takes user keys, so this is the one genuinely new
>    pattern.
> 3. **It has two modes:** `nsz_compress` (`COMPRESS`) and `nsz_decompress`
>    (`EXTRACT`). Verify is scoped to the compressed outputs (`.nsz/.xcz`) via
>    nsz's own `-V`, so delete-on-verify is offered for compress only.
>
> Read `app/services/nsz.py` and `app/services/tools/nsz.py` for the real thing.
>
> **The big idea: there is a tool registry.** Adding a tool used to mean editing
> `if/elif` ladders in ~20 files. It doesn't anymore. The backend has a tool
> registry (`app/services/tools/`) that mirrors the frontend one. You write a
> small plugin class, register it once, and the generic dispatch in
> `job_manager` and `convert.py` picks it up by mode. Most of the old per-tool
> branches are gone. The sections below reflect that.

---

## 1. Architecture overview

A conversion request flows through these layers. Adding a tool/platform means
touching the same layers in the same order:

```
            ┌─────────────────── Web UI (Svelte 5 + Vite, under src/) ───────────────┐
            │  src/lib/tools/registry.js: one TOOLS entry per tool, drives all UI   │
            │  src/lib/api/endpoints.js:  HTTP / SSE / batch-verify client methods   │
            └───────────────────────────────────┬───────────────────────────────────┘
                                                 │  POST /api/jobs   (mode=...)
            ┌────────────────────────────────────▼──────────────────────────────────────┐
            │  Routes (app/routes)                                                        │
            │   convert.py  – validate mode/extension (via registry.spec), enqueue job    │
            │   files.py    – mark files convertible in directory listings + search       │
            │   info.py     – per-tool info + verify (single + batch + SSE) endpoints      │
            └────────────────────────────────────┬──────────────────────────────────────┘
                                                  │  job_manager.create_job(mode=...)
            ┌─────────────────────────────────────▼─────────────────────────────────────┐
            │  Job pipeline (app/services/job_manager.py)                                 │
            │   _queue_job_locked  – registry.for_mode(mode).output_path(...)              │
            │   _process_job       – registry.for_mode(mode).convert(...) / .verify(...)   │
            └─────────────────────────────────────┬─────────────────────────────────────┘
                                                   │  plugin.convert(...) async generator
            ┌──────────────────────────────────────▼────────────────────────────────────┐
            │  Tool plugin (app/services/tools/<tool>.py) + service (app/services/<tool>.py)│
            │   plugin holds ModeSpec rows; service spawns the binary, parses progress     │
            └──────────────────────────────────────┬────────────────────────────────────┘
                                                    │  subprocess
            ┌────────────────────────────────────────▼───────────────────────────────────┐
            │  Binary in the image (Dockerfile) + path in app/config.py                    │
            └──────────────────────────────────────────────────────────────────────────────┘
```

Two supporting layers:

- **`app/models.py`** holds the `ConversionMode` enum (every mode string lives
  here), the `InputKind` enum, the `OutputStatus` / `FileEntry` listing models,
  and the Pydantic info models (`CHDInfo`, `DolphinDiscInfo`, `Z3DSInfo`,
  `NszInfo`, `CsoInfo`, `RomzInfo`, `Ps3IsoInfo`).
- **`app/config.py`** `Settings` holds the binary path for each tool
  (`chdman_path`, `dolphin_tool_path`, `z3ds_compressor_path`, `nsz_path`,
  `maxcso_path`, `sevenzip_path`, `makeps3iso_path`) plus the shared and
  per-tool nice/ioprio/timeout policy knobs.

### The tool registry

`app/services/tools/` is the heart of the backend. It has five parts:

- **`spec.py`** defines `ModeKind` (`CREATE` / `EXTRACT` / `COPY` / `COMPRESS`),
  the frozen `ModeSpec` dataclass (per-mode metadata that the routes and
  pipeline read instead of branching on the mode string), and `ChainSpec` /
  `ChainStep` for composite modes. It re-exports `InputKind` from `models` (it
  lives there to avoid an import cycle).
- **`base.py`** defines the `ToolPlugin` protocol (the contract), the
  `EmbeddedHashUnavailable` exception, and a `BaseTool` helper that supplies
  sensible defaults so concrete plugins stay tiny.
- **`registry.py`** defines `ToolRegistry`, the lookup object. It indexes tools
  by id and by mode and answers `all()`, `get(tool_id)`, `for_mode(mode)`,
  `spec(mode)`, `mode_specs()`, `convertible_extensions()`,
  `archive_input_extensions()`, `tools_accepting_archive_member(ext)`,
  `tools_for_input(filename)`, `tools_for_directory(path)`,
  `tool_for_verify(path)`, `tools_verifying_path(path)`, `verify_extensions()`,
  `output_extensions()`, and `scannable_extensions()` (which drives the library
  scan / DAT-match discovery).
- **`chdman.py` / `dolphin.py` / `z3ds.py` / `nsz.py` / `maxcso.py` / `romz.py` /
  `makeps3iso.py`** are the seven real plugins. Each is a thin `BaseTool`
  subclass that holds `ModeSpec` rows and delegates the real work to the
  underlying service singleton (`makeps3iso.py` is the directory-input one; see
  §3.3.4 of the design doc). **`chain.py`** is the eighth registration: a
  synthetic `ChainTool` with no binary and no service, which drives the others
  through the registry (design doc §3.3.3).
- **`__init__.py`** builds the `registry` singleton and registers all eight.
  This is the single wiring point. Order matters at the end: `ChainTool` takes
  the registry itself and must register *after* the component tools it drives.

### The plugin contract: `ModeSpec` and `BaseTool`

A `ModeSpec` row (`spec.py`) describes one mode:

```python
@dataclass(frozen=True)
class ModeSpec:
    mode: str                                 # wire value == a ConversionMode value
    tool_id: str                              # must equal the owning tool's `id`
    kind: ModeKind                            # CREATE | EXTRACT | COPY | COMPRESS
    label: str                                # UI label
    group: str                                # UI group id
    output_ext: str | None                    # ".chd"/".rvz"/None (None = mapped from input)
    input_extensions: frozenset[str]
    supports_compression: bool = False
    supports_compression_level: bool = False  # dolphin rvz/wia only
    supports_delete_on_verify: bool = False
    allows_archive_input: bool = False        # True for convertible-source modes; see §17
    # Sibling outputs written beside the primary output, as suffix swaps off it
    # (extractcd's .cue -> its .bin data track). Read by BaseTool.companion_outputs
    # so conflict detection, overwrite cleanup, size accounting and in-use
    # tracking all enumerate companions from one place. Leave empty and override
    # companion_outputs() instead when the set is dynamic (makeps3iso's split parts).
    companion_exts: tuple[str, ...] = ()
    # FILE by default. A folder-input mode (makeps3iso) sets {InputKind.DIRECTORY}.
    input_kinds: frozenset[InputKind] = frozenset({InputKind.FILE})
```

A composite mode uses **`ChainSpec`** instead, which is a structural superset of
`ModeSpec`: every field above exists with the same meaning, so all the registry,
route and `job_manager` consumers work unchanged. The extras (`steps`,
`intermediate_exts`, `verify_step`) are read only by `ChainTool`. You need this
only when your "tool" is really an ordered pipeline over existing modes.

A plugin subclasses `BaseTool` and provides:

| Member | Purpose |
|--------|---------|
| `id`, `display_name`, `binary_path` | identity + binary path (from settings) |
| `modes` | a tuple of `ModeSpec` rows |
| `output_extensions`, `verify_extensions` | produced extensions and verify-accepted extensions. `output_extensions` drives the "output exists" badges **and** the registry-driven library scan / DAT-matching discovery (`registry.scannable_extensions()` = the union of output + verify), so list every extension your tool actually writes (including sidecars like CHDMAN's extractcd `.bin`). |
| `convert(input_path, output_path, mode, *, compression=None, split=False, cancel_event=None)` | async generator yielding `{"progress": int, "message": str}`, raising `ConversionCancelled` when `cancel_event` fires. `split` is honored only by modes that declare it (makeps3iso's `-s` 4 GB FAT32 split); every other tool accepts and ignores it, exactly as it does a `compression` it doesn't use. |
| `verify(path)` / `verify_stream(path)` | deep integrity check, one-shot and streaming. **Optional in practice**: a tool with no user-facing verify (makeps3iso) simply registers no verify route. |
| `info(path)` / `info_model(raw, path)` | metadata dict + the Pydantic model it maps to. `BaseTool._basic_info_fields(raw)` maps the shared `BasicFileInfo` keys (`file`, `size`, `size_display`, `format`, `extension`, `compressed`, `compression_type`) in one place — the five "what is this file" tools (z3ds, nsz, cso, romz, makeps3iso) build their model from it instead of re-typing the mapping. |
| `output_path(mode, input_path, output_dir=None, *, treat_as_stem=False)` | compute the output path |
| `accepts_directory(path)` | the directory analogue of `ext in input_extensions`. Default `False`; a tool with an `InputKind.DIRECTORY` mode overrides it to run its source-layout detector. May do disk I/O — it runs off the event loop. |
| `companion_outputs(output_path, mode)` | sibling paths this mode writes *besides* `output_path` (never including it). `BaseTool` derives them from `ModeSpec.companion_exts` as pure path math; override only when the set is dynamic (makeps3iso's size-dependent split parts). |
| `detect_output(input_path)` | optional, returns an `OutputStatus` so the file list can badge "output already exists". May content-validate the candidate before claiming it: `romz` only reports a `.7z`/`.zip` sibling as its output when it's a genuine single-ROM archive (not just any file matching the `Game.gba.7z` naming), so the badge and the source row's verify-from-output flow track real outputs. |
| `verifies_path(path)` | optional per-file refinement of `verify_extensions`. Default (in `BaseTool`) is a plain extension match; override when your tool claims a broad container extension but only handles a subset (`romz` claims `.7z`/`.zip` yet only verifies single-ROM archives). `routes/files.py` materializes the result into `FileEntry.verifiable_by`, which the frontend gates the Verify/Info row-actions on. May do disk I/O — it runs inside the threadpool scan. |
| `active_pids()` | PIDs for the debug heartbeat |
| `post_convert(input_path, output_path, mode)` | optional hook, default no-op |
| `embedded_hashes(path, *, cancel_event=None)` | **optional** DAT-match fast path. Return `(sha1, match_type)` tuples your format already carries / can derive cheaply (so matching skips a full file hash); default `[]` → the caller falls back to a file-level SHA1, which is correct for raw formats. Raise `EmbeddedHashUnavailable` (from `services.tools.base`) when you *should* yield a hash but the attempt failed transiently, so it's recorded as a non-cacheable miss instead of a false negative. |
| `embedded_hash_is_exhaustive` | optional flag (default `False`). Set `True` only when your container bytes can never appear in a DAT (e.g. a recompressed image), so a content-hash miss is definitive and the file-level fallback is skipped; leave `False` if your file's own SHA1 might be indexed. |

`BaseTool` fills in `input_extensions` (the union of every mode's
`input_extensions`), `spec(mode)`, no-op `detect_output` / `post_convert`,
`accepts_directory` (`False`), `companion_outputs` (from `companion_exts`), the
extension-match `verifies_path` default, and the `embedded_hashes` default
(`[]`, `embedded_hash_is_exhaustive=False`), so a real plugin only overrides
what differs. See `app/services/tools/z3ds.py` for the smallest complete
example (~130 lines, mostly delegation). For one-shot
subprocess work (info / header / hash extraction), reuse the shared
`SubprocessRunner.run_capture()` (cancel/timeout-aware, applies the tool
nice/ioprio policy) rather than re-implementing the spawn loop — see
`app/services/dolphin_tool.py:disc_hashes` for the pattern.

`ConversionCancelled` is defined once in `app/services/subprocess_runner.py` and
re-exported through `services.chdman` and `services.tools.runner`. Import it,
don't define your own.

---

## 2. Two kinds of extension

### Scenario A: a new platform on an *existing* tool

The lightweight case: the binary already ships and the plugin already exists.
You add a mode string or an input extension.

Examples:
- A new chdman create variant. Add a `ConversionMode`, add a `ModeSpec` row to
  `ChdmanTool.modes`, teach the underlying `chdman.get_output_path_for_mode` the
  new suffix if it differs, and surface it in the UI registry.
- Letting chdman accept a new input extension: add it to
  `CHDMAN_CONVERTIBLE_EXTENSIONS` in `app/services/chdman.py` and to the
  relevant `ModeSpec.input_extensions`.

Go to **§4**.

### Scenario B: a brand-new tool (and usually a new platform with it)

A new binary, a new service module, a new plugin, a new `ConversionMode`, new
info/verify endpoints, and new UI wiring. This is the z3ds-shaped case.

Go to **§5**.

### Scenario C: a tool and a platform in tandem

The common real-world request: "support platform X, which needs new binary Y."
It is just **Scenario B**, because adding the tool is what makes the platform
reachable. The platform shows up as:

- the input extensions in `*_CONVERTIBLE_EXTENSIONS` and `ModeSpec.input_extensions`,
- the `ConversionMode` value(s) the tool exposes,
- a UI entry in the registry plus a primary-tool option,
- (optionally) platform-specific flags inside the service's `_build_command`.

Follow §5 end to end. §6 is the concrete tandem checklist.

---

## 3. Inventory: every file a tool/platform touches

This is the exhaustive list. Not every item is required for every change (the
right-hand column says when it applies), but check every row. The order is the
recommended implementation order (bottom of the stack to top). Deeper detail for
the non-obvious rows is in §8 to §14.

### 3.1 Binary / packaging

| # | File | What you do | When |
|---|------|-------------|------|
| 1 | `Dockerfile` | Build/install the binary into the image (builder stage + `COPY`, or apt install); add runtime shared libs; multi-arch (amd64+arm64). | New tool |
| 2 | `.dockerignore` | Verify the binary/source you reference isn't excluded. `app/`, `static/`, `migrations/` are copied; `tests/` and most `*.md` are excluded. | New tool (check only) |
| 3 | `requirements.txt` | Add any new **Python** runtime dependency the service imports. | If service needs a new pip dep |
| 4 | `requirements-dev.txt` | Add test-only Python deps. | Rare |
| 5 | `.local-bin/` + `.env.local` | Local-dev copy of a from-source binary. Only `z3ds_compressor` is committed here today. Dropping a binary in `.local-bin/` is **not** enough: `run_dev.sh` doesn't add it to `PATH`, and from-source tools default to an absolute `/usr/local/bin/...` path, so you must set `<TOOL>_PATH` in `.env.local` (which `run_dev.sh` sources). See §8. | New from-source tool |
| 6 | `entrypoint.sh` | Add the binary path env passthrough; extend the **CLI batch mode** loop if the tool should run headless (see §9). | New tool, CLI support optional |

### 3.2 Backend code

| # | File | What you do | When |
|---|------|-------------|------|
| 7 | `app/config.py` | Add a `<tool>_path` `Field` with an env alias (`<TOOL>_PATH`). | New tool |
| 8 | `app/services/<tool>.py` | The underlying service: `_build_command`, the subprocess spawn, progress parsing, cancel handling, the `*_CONVERTIBLE_EXTENSIONS` set, `*_OUTPUT_FORMATS`, and the module singleton. | New tool |
| 9 | `app/services/tools/<tool>.py` | The plugin: a `BaseTool` subclass with `id`, `display_name`, `modes` (`ModeSpec` rows), `output_extensions`, `verify_extensions`, delegating `convert`/`verify`/`info`/`output_path`/`detect_output` to the service. Optionally override `embedded_hashes` (DAT-match fast path) / `embedded_hash_is_exhaustive`; the default falls back to file-level SHA1. | New tool |
| 10 | `app/services/tools/__init__.py` | One line: `registry.register(<Tool>(settings.<tool>_path))` (plus the import). This is the only dispatch wiring. | New tool |
| 11 | `app/models.py` | Add `ConversionMode` value(s) and a `<Tool>Info` model. **No `FileEntry` change** — listings are tool-neutral (`convertible_by` / `outputs` / `verifiable_by`), see §5.8. | New mode and/or tool |
| 12 | `app/routes/convert.py` | Add `SkipReason.<TOOL>_BAD_EXTENSION`, its `_SKIP_HTTP` message, and one `_BAD_EXTENSION_REASON["<tool>"]` entry. No `if`-block: validation is table-driven. | New tool |
| 13 | `app/routes/files.py` | **Nothing.** The directory scan and `search_files` loop over `registry.all()`; a registered tool is annotated automatically. | Never (registry-driven) |
| 14 | `app/routes/info.py` | A `_VERIFY_CONFIG["<tool>"]` entry + one `register_verify_routes(router, registry.get("<tool>"))` call, and a hand-written `GET /<tool>-info` endpoint. Both are optional for a tool with no user-facing verify/info (makeps3iso registers neither). | New tool |
| 15 | `app/services/job_manager.py` | Usually nothing: convert and verify dispatch through the registry. Touch only for special post-processing (disc-id tagging, multi-file sidecars). | Rare |
| 16 | `app/services/disc_id.py` | Add a serial/title parser if the new **disc** platform should get GAME/NAME tags embedded (chdman create modes only). | New disc platform, optional |
| 17 | `app/services/archive.py` | Nothing for a new tool, and that's by design: browse lists every *known* source (`convertible_extensions()` minus archives), and the convert gate is `archive_input_extensions()` — both come straight from the registry. Just declare your `input_extensions` and your members show up when folks browse in; set `allows_archive_input=True` only when a mode should also convert them in place (see §17.5). | Rare |
| 18 | `app/services/dat_*.py` / `app/routes/dat.py` | Touch only if the platform participates in DAT (MAMERedump) hash-matching. | Rare |
| 19 | `migrations/versions/*.py` | New Alembic migration **only** if you add DB-persisted columns/tables (use `scripts/new_migration.sh`). The verification/metadata stores are keyed by path and need no migration for a new tool. | If schema changes |

### 3.3 Frontend (Svelte 5 + Vite: one new entry in the registry)

| # | File | What you do | When |
|---|------|-------------|------|
| 20 | `src/lib/api/endpoints.js` | `get<Tool>Info`, `verify<Tool>`, `verifyBatch<Tool>` client methods alongside the existing ones. | New tool |
| 21 | `src/lib/tools/registry.js` | **One new entry** in the `TOOLS` array. Everything downstream (sidebar, workspace, badges, modals, verify dispatch, SSE URL building, compression defaults, icons, Help) looks up this registry. | New mode and/or tool |
| 22 | `src/styles/tokens.css` | Add a semantic token only if you need a new tool accent / badge color. Most tools reuse existing tokens via `accent: 'var(--badge-<token>)'`. Add it under **both** `:root` and `:root.dark`. | If new visual identity |
| 22a | `src/lib/util/fileIcon.js` | **One `TOOL_MEDIA` entry** mapping your tool id to `'disc'` or `'game'`. The `DISC_EXTS`/`GAME_EXTS` sets are *derived* from the registry, so you never re-type extensions — you only say which bucket your media reads as. The coverage guard (`tests/test_frontend_registry_derives_186.py`) fails until every registered tool is classified. | New tool |
| 22b | `src/lib/stores/conversion.svelte.js` | **Nothing.** `defaultCompressionFor()` / `defaultLevelFor()` read `defaultCompression` and `compressionLevelRange` off your registry descriptor. Declare them in `registry.js` (item 21) and the initial value, the slider default, and the shared **Reset to default** button in `CompressionPicker.svelte` all work for free. | Never (registry-driven) |
| 22c | `src/lib/tools/helpModes.js` | One `MODE_BLURBS` line per new mode (and a `MODE_OUTPUT` override only when a single `outputExt` can't tell the story — a reversible mode whose output depends on the input, or a companion pair like extractcd's `.cue` + `.bin`). The same guard test fails on a mode with no blurb or a stale key. | New mode |
| 22d | `src/lib/components/views/HelpView.svelte` | One curated **tool blurb**. The per-tool mode reference *table* is generated via `helpModeSections(registry)`, so modes and output columns can't drift — only the prose is hand-written. | New tool (docs) |

### 3.4 Tests

| # | File | What you do | When |
|---|------|-------------|------|
| 23 | `tests/test_<tool>_routes.py` | Info + verify endpoint tests (copy `test_z3ds_routes.py`). | New tool |
| 24 | `tests/test_<tool>_service.py` | `convert`/`verify`/cancel/bad-extension tests (copy `test_z3ds_verification_service.py`). | New tool |
| 25 | `tests/test_tool_registry.py` | Assert your modes resolve to your tool and the spec flags are right. Bump the resolved-mode count (currently `29`), extend the `_legacy_tool_for_mode` ladder, the `convertible_extensions` union, `tools_for_input`/`tool_for_verify` cases, and the `output_path` + `output_extensions` parametrize lists. | New tool/mode |
| 26 | `tests/test_mode_parity_fixes.py` | Add the mode so single-vs-batch validation parity is enforced; update the delete-on-verify error-message assertions if your compress mode supports it. See also `tests/test_single_batch_plan_parity.py`. | New mode |
| 26a | `tests/test_dispatch_routing.py` | Extend the convert/verify dispatch ladder (`_legacy_dispatch_id`) and the patched-tool tuple so your modes route to your service. | New tool |
| 26b | `tests/test_files_outputs_parity.py` | **Usually nothing.** It asserts the *exact* tool-neutral `FileEntry` JSON surface (`convertible_by` / `outputs` / `verifiable_by` / …). Touch it only if you change that surface — adding a tool must not. | Rare |
| 26c | `tests/test_frontend_registry_derives_186.py` | Runs the real JS under Node and fails if the registry-derived frontend facts drift: an unclassified tool in `TOOL_MEDIA`, an extension with no icon bucket, a mode with no `MODE_BLURBS` entry, a stale key, or a null-output mode with no `MODE_OUTPUT` override. Nothing to edit — it's the guard that tells you what you forgot. | New tool/mode (guard) |
| 26d | `tests/test_archive_conversion_e2e.py` + `tests/test_archive_preference.py` | Add `MATRIX` rows per direction and assert your source exts are in `registry.archive_input_extensions()` (see §17.7). Make parametrize `ids` unique if an extension repeats across modes. | New archive-aware tool |
| 26e | `tests/test_companion_outputs_182.py` | Assert `companion_outputs()` for your mode if it writes sidecars. | If `companion_exts` set |
| 26f | `tests/test_verify_routes_factory.py` | Covers the generated verify trio generically; extend if your `_VERIFY_CONFIG` entry has unusual naming. | Rare |
| 27 | `tests/conftest.py` | **No change for a tool binary.** `conftest.py` is DB-only; it does *not* stub tool binaries. Per-tool route/service tests define their own mocks (monkeypatch `info_routes.<service>` or `app.services.<tool>.asyncio.create_subprocess_exec`). | Rare |

### 3.5 CI / quality gates (must stay green)

| # | File | What it enforces | Action |
|---|------|------------------|--------|
| 28 | *(no workflow file)* — **Codacy** runs as a GitHub App checks integration ("Codacy Static Code Analysis"), configured in the Codacy UI plus the in-repo config in rows 33–39. There is **no** `.github/workflows/codacy.yml`. | ruff, eslint, pylint, bandit, etc. | New code must pass; see §11 |
| 29 | `.github/workflows/codeql.yml` | Push/PR/weekly CodeQL. Its matrix is exactly two languages: `python` and `javascript-typescript`. (PRs also show an `Analyze (actions)` job — that one is configured in the repo's code-scanning settings, not in this file, so don't edit this matrix expecting to change it.) | Auto-covers new code |
| 30 | `.github/workflows/docker-image.yml` | On release: hadolint, multi-arch build, Trivy scan, SBOM/attestation. | Dockerfile must pass hadolint; see §8/§10 |
| 31 | `.github/workflows/label.yml` + `.github/labeler.yml` | PR auto-labeling by path. | Add a path glob if you want a label |
| 31a | `.github/workflows/screenshots.yml` | **Take screenshots**: rebuilds the README/docs captures from `shots.yml` and commits them. | See §3.6 item 49 |
| 31b | `.github/workflows/stale.yml` | Marks stale issues/PRs. | Irrelevant |
| 32 | `renovate.json` | Dependency bumps are handled by **Renovate**, not Dependabot — there is no `.github/dependabot.yml`. | Only if adding a new ecosystem |
| 33 | `pyproject.toml` | pylint + ruff config (Codacy reads this). | Respect line-length 100, py310 |
| 34 | `.pylintrc`, `.prospector.yaml`, `.bandit` | Python lint/security (bandit flags `shell=True`, predictable tmp). | Follow the no-shell rule (§15) |
| 35 | `eslint.config.js`, `.eslintrc.json`, `.jshintrc` | ESLint for the frontend (`src/`). | New JS and Svelte must pass |
| 36 | `.stylelintrc.json`, `.csslintrc` | CSS lint. | If you edit CSS |
| 37 | `.markdownlint.json` | Markdown lint. | If you add docs |
| 38 | `.codacy.yml`, `.codacy/` | Codacy tool config/excludes. | Rarely |
| 39 | `config/pmd/ruleset.xml` | PMD ruleset (Codacy). | Rarely |

### 3.6 Deployment & docs

| # | File | What you do | When |
|---|------|-------------|------|
| 40 | `docker-compose.yml` / `.cli.yml` / `.multi-volume.yml` | Document/override the new `<TOOL>_PATH` env or CLI mode env if relevant. | If ops needs the knob |
| 41 | `package.json` | The version lives here. A release bumps `package.json` and publishes a GitHub Release; there is no `.version` file or `sync-version.sh` script. | Every release |
| 42 | `README.md` | Supported-formats/tool table, feature list. Also the **Docker Hub** description (published from README by CI). | New tool/platform |
| 43 | `docs/RELEASE_NOTES.md` | Changelog entry. | Every change |
| 44 | `docs/DEPLOYMENT.md`, `docs/DOCKER-COMPOSE.md` | New env vars / volumes / tool requirements. | If deploy surface changes |
| 45 | `AGENTS.md`, `.github/copilot-instructions.md` | Runbook / AI guidance, update if conventions change. | Optional |
| 46 | `app/main.py` | The FastAPI `description=` (shown at `/docs`) names the tools. Add the new tool so it stays accurate. | New tool |
| 47 | `docs/DESIGN_tool_plugin_architecture.md` | The plugin contract + shared infrastructure. Update it whenever you change a *shared seam* (a new `ModeSpec` field, a new `ToolPlugin` method, a new registry query) — not for a tool that merely uses the existing contract. | If the contract changes |
| 48 | **This file** | If you changed a shared seam above, the step that adds a tool changed too. Update the affected section, the §3 inventory, the §6 checklist and the §14 map together — they are four views of the same list and drift as a set. | If the contract changes |
| 49 | `shots.yml` + `docs/screenshots/` | Screenshots are generated by shot-scraper, never hand-captured. A new tool adds a sidebar entry, so refresh them (push triggers the **Take screenshots** workflow); add a `shots.yml` entry if you introduce a genuinely new surface. See `docs/SCREENSHOTS.md`. | If the UI changes |

---

## 4. Scenario A: add a platform/mode to an existing tool

Worked example: add a hypothetical **`createcd_audio`** chdman mode (pretend it
produces `.chd` with a platform-specific flag). The same pattern applies to any
new chdman/dolphin sub-format.

### 4.1 Add the mode to the enum: `app/models.py`

```python
class ConversionMode(str, Enum):
    ...
    CREATECD = "createcd"
    CREATECD_AUDIO = "createcd_audio"   # NEW
    ...
```

The string value is what travels over the API and what the registry indexes on.
Pick a value consistent with the tool's existing prefix convention (`create*`,
`extract*`, `dolphin_*`, `z3ds_compress`), because a few places still key off
these prefixes (see §15).

### 4.2 Add a `ModeSpec` row: `app/services/tools/chdman.py`

Add a `ModeSpec` to `ChdmanTool.modes`, modeled on the existing `createcd` row:

```python
ModeSpec(
    mode="createcd_audio", tool_id="chdman", kind=ModeKind.CREATE,
    label="Create CD CHD (Audio)", group="create",
    output_ext=".chd", input_extensions=CHDMAN_CONVERTIBLE_EXTENSIONS,
    supports_compression=True, supports_delete_on_verify=True,
    allows_archive_input=True,
),
```

Because `kind=CREATE` and `output_ext=".chd"`, the route validation, output-path
computation, archive-input handling, and delete-on-verify all work off this row
with no further code. Only touch the underlying service if the mode needs
special binary flags (branch in `chdman._build_command`) or a different output
suffix (`chdman.get_output_path_for_mode`).

### 4.3 Validation: `app/routes/convert.py`

Usually **nothing**. `convert.py` reads `registry.spec(mode)` and validates off
the spec flags (`spec.kind`, `spec.allows_archive_input`,
`spec.supports_delete_on_verify`). A new `create*` chdman mode inherits the
create-mode rules (for example the `.chd`-input rejection for `kind == CREATE`).
Add an explicit check only if your mode breaks a spec assumption.

### 4.4 Surface it in the UI: `src/lib/tools/registry.js`

Append one mode entry to the chdman descriptor's `modes` array (the group's
human label is already declared via `groups.create: 'Create'`):

```js
// src/lib/tools/registry.js, inside the chdman TOOLS entry
modes: [
  // …existing entries…
  { mode: 'createcd_audio', kind: 'create', label: 'Create CD CHD (Audio)',
    group: 'create',
    outputExt: '.chd', inputExtensions: CHDMAN_SOURCE_EXTS,
    supportsCompression: true, supportsCompressionLevel: false,
    supportsDeleteOnVerify: true, allowsArchiveInput: true },
],
```

`CHDMAN_SOURCE_EXTS` is a local `const` in `registry.js` (not exported). Because
the mode reuses the chdman tool and `.chd` output, file badges, the conversion
config panel, the info modal, and the verify path all already work. The mode
shows up wherever `registry.modesByGroup('chdman')` or
`registry.specFor('createcd_audio')` is consulted.

### 4.5 Test

Add a case to `tests/test_mode_parity_fixes.py` (validation parity between single
+ batch create), `tests/test_tool_registry.py` (the mode resolves to chdman with
the right flags), and `tests/test_chdman_annotations.py` if relevant.

---

## 5. Scenario B/C: add a brand-new tool (with its platform)

Worked example throughout: a fictional **`nszip`** tool that compresses Nintendo
Switch `.nsp`/`.xci` dumps into `.nsz`/`.xcz`. Replace names as needed. This
mirrors z3ds exactly.

### 5.1 Install the binary: `Dockerfile`

Three patterns exist in the current `Dockerfile` (a five-stage build — see §8
for the stage list). Pick the one that fits:

- **Distro package** (chdman via `mame-tools`, pinned to a `snapshot.debian.org`
  `.deb` with per-arch SHA256 checks).
- **Distro package, best-effort** (dolphin-emu, amd64-only, non-fatal). A wrapper
  script at `/usr/local/bin/dolphin-tool` is created only if the binary exists.
- **Build from source in the `builder` stage** (z3ds), then copy the artifact
  into the runtime image.

For `nszip` built from source, add to the `builder` stage. Note the base image
is **digest-pinned** — copy the `FROM` line from the repo's `Dockerfile`
verbatim rather than writing a bare `debian:trixie-slim` tag, so the supply-chain
pin holds (see §8):

```dockerfile
FROM debian:trixie-slim@sha256:<the digest already used in the Dockerfile> AS builder
RUN apt-get update -o Acquire::Retries=3 && \
    apt-get install -y --no-install-recommends \
      git build-essential libzstd-dev ca-certificates && \
    git clone https://github.com/example/nszip.git /tmp/nszip
WORKDIR /tmp/nszip
RUN g++ -O3 src/*.cpp -o nszip -lzstd && chmod +x nszip
```

…and copy it into the runtime stage next to the existing z3ds copy:

```dockerfile
COPY --from=builder /tmp/nszip/nszip /usr/local/bin/nszip
```

If the tool needs a runtime shared lib, add it to the runtime `apt-get install`
list. The `zstd` CLI is already installed (z3ds's `verify_stream` shells out to
`zstd -t`), so if your tool's output is a zstd container you can reuse it.

> Multi-arch: the image builds `linux/amd64` and `linux/arm64`. A source build
> compiles per arch automatically; a downloaded `.deb` needs a per-arch URL/SHA
> like the `mame-tools` block.

### 5.2 Add the binary path setting: `app/config.py`

Add next to the other tool paths (`chdman_path`, `dolphin_tool_path`,
`z3ds_compressor_path`, `nsz_path`, `maxcso_path`, `sevenzip_path`,
`makeps3iso_path`):

```python
nszip_path: str = Field(
    default="/usr/local/bin/nszip", alias="NSZIP_PATH",
)
```

The default may be a bare binary name (`nsz`, `7z`) when the tool arrives on
`PATH` rather than at a fixed location. Note the setting is named after the
**binary**, while the plugin is named after the **tool id** — `RomzTool` is
constructed from `settings.sevenzip_path`, `MaxcsoTool` from
`settings.maxcso_path` under tool id `cso`. Keep the pairing obvious in
`services/tools/__init__.py`.

This lets ops override the path/env without code changes, and is what the plugin
passes to its service in `__init__`.

Optionally, add per-tool priority/timeout override fields so ops can diverge from
the shared `COMPRESSATORIUM_TOOL_*` policy for just your tool. They default to
`None` (fall back to the shared `tool_*` setting), and the `subprocess_runner`
helpers resolve them by `<owner>_<key>` (see §5.3). Mirror `nsz`/`z3ds`/`maxcso`:

```python
nszip_nice: int | None = Field(default=None, alias="COMPRESSATORIUM_NSZIP_NICE")
nszip_ioprio_class: int | None = Field(default=None, alias="COMPRESSATORIUM_NSZIP_IOPRIO_CLASS")
nszip_ioprio_level: int | None = Field(default=None, alias="COMPRESSATORIUM_NSZIP_IOPRIO_LEVEL")
nszip_verify_timeout: int | None = Field(default=None, alias="COMPRESSATORIUM_NSZIP_VERIFY_TIMEOUT")
# add nszip_info_timeout only if the tool's info() runs a subprocess (chdman/Dolphin do; nsz/z3ds/maxcso don't)
```

### 5.3 Write the service + the plugin

There are two files. The **service** (`app/services/nszip.py`) owns the
subprocess: build the command, spawn it, parse progress, handle cancel. Copy
`app/services/z3ds_compress.py` and adapt. The **plugin**
(`app/services/tools/nszip.py`) is a thin `BaseTool` subclass that holds the
`ModeSpec` rows and delegates to the service. Copy `app/services/tools/z3ds.py`.

The service skeleton:

```python
import asyncio, contextlib, logging, os, shutil, threading, time
from collections.abc import AsyncGenerator
from pathlib import Path

from config import settings
from services.subprocess_runner import ConversionCancelled   # reuse the shared exception
from services.timeout_policy import compute_progress_stall_timeout

NSZIP_CONVERTIBLE_EXTENSIONS = {".nsp", ".xci"}
NSZIP_OUTPUT_FORMATS = {".nsp": ".nsz", ".xci": ".xcz"}    # input ext -> output ext

logger = logging.getLogger("chd.nszip")


class NszipService:
    def __init__(self):
        self.nszip_path = settings.nszip_path
        self._active_pids: set[int] = set()
        self._pid_lock = threading.Lock()

    def _build_command(self, input_path: str, output_path: str) -> list[str]:
        cmd = [self.nszip_path, input_path, output_path]
        # I/O priority: mirror the other services so heavy jobs stay nice.
        # The shared SubprocessRunner owns the priority policy; pass your tool's
        # owner id so a per-tool override (COMPRESSATORIUM_NSZIP_IOPRIO_*) can
        # diverge from the shared COMPRESSATORIUM_TOOL_IOPRIO_* default.
        cmd = ioprio_prefix("nszip") + cmd
        return cmd

    def active_pids(self):
        with self._pid_lock:
            return list(self._active_pids)

    async def convert(self, input_path, output_path, mode="nszip_compress",
                      *, compression=None, split=False,
                      cancel_event=None) -> AsyncGenerator[dict, None]:
        # `split` is accepted and ignored unless your tool implements it —
        # job_manager passes it unconditionally, so omitting it is a TypeError
        # at runtime. Same rule as a `compression` your tool doesn't use.
        # 1. mkdir -p output dir
        # 2. build cmd, spawn with asyncio.create_subprocess_exec (NEVER shell=True)
        # 3. apply the shared nice level via preexec_fn (posix only):
        #    `from services.subprocess_runner import apply_nice` then
        #    `apply_nice("nszip")` inside the preexec callback
        # 4. stream stdout, parse progress, yield {"progress": int, "message": str}
        # 5. honor cancel_event -> terminate/kill, clean partial output,
        #    raise ConversionCancelled
        # 6. apply a stall timeout via compute_progress_stall_timeout(...)
        # 7. on non-zero exit raise RuntimeError(tail of output)
        # 8. finally yield {"progress": 100, "message": "Compression complete"}
        ...

    async def verify(self, path) -> dict: ...
    async def verify_stream(self, path) -> AsyncGenerator[dict, None]: ...
    def info(self, path) -> dict: ...

    def get_output_path(self, input_path, output_dir=None) -> str:
        input_p = Path(input_path)
        out_ext = NSZIP_OUTPUT_FORMATS.get(input_p.suffix.lower(), ".nsz")
        filename = f"{input_p.stem}{out_ext}"
        return str(Path(output_dir) / filename) if output_dir else str(input_p.parent / filename)


nszip_service = NszipService()   # module-level singleton
```

The plugin skeleton (copy `app/services/tools/z3ds.py`):

```python
from fastapi.concurrency import run_in_threadpool
from models import NszipInfo, OutputStatus
from services.nszip import (
    NSZIP_CONVERTIBLE_EXTENSIONS, NSZIP_OUTPUT_FORMATS, nszip_service,
)
from .base import BaseTool
from .spec import ModeKind, ModeSpec


class NszipTool(BaseTool):
    id = "nszip"
    display_name = "Switch"
    modes = (
        ModeSpec(
            mode="nszip_compress", tool_id="nszip", kind=ModeKind.COMPRESS,
            label="Compress to NSZ/XCZ", group="nszip",
            output_ext=None,  # mapped from the input extension
            input_extensions=frozenset(NSZIP_CONVERTIBLE_EXTENSIONS),
            supports_delete_on_verify=True,
        ),
    )
    output_extensions = frozenset(NSZIP_OUTPUT_FORMATS.values())
    verify_extensions = output_extensions

    def __init__(self, binary_path):
        super().__init__(binary_path)

    def output_path(self, mode, input_path, output_dir=None, *, treat_as_stem=False):
        return nszip_service.get_output_path(input_path, output_dir)

    def convert(self, input_path, output_path, mode, *, compression=None,
                split=False, cancel_event=None):
        return nszip_service.convert(input_path, output_path, mode,
                                     compression=compression, split=split,
                                     cancel_event=cancel_event)

    async def verify(self, path): return await nszip_service.verify(path)
    def verify_stream(self, path): return nszip_service.verify_stream(path)
    async def info(self, path): return await run_in_threadpool(nszip_service.info, path)
    def info_model(self, raw, path): return NszipInfo(**raw)
    def active_pids(self): return nszip_service.active_pids()
```

**Critical service rules** (all enforced by the existing services, copy them):

- **Never** use `shell=True`. Build an argv list and use
  `asyncio.create_subprocess_exec(cmd[0], *cmd[1:], ...)`. The binary path comes
  from validated settings; inputs are never shell-interpreted.
- **Accept the full keyword set, even the parts you ignore.** `job_manager`
  calls `plugin.convert(..., compression=…, split=…, cancel_event=…)`
  unconditionally, so a `convert()` missing `split` (or `compression`) raises
  `TypeError` the first time a job runs — something unit tests that call your
  service directly will not catch. Take the kwarg and drop it on the floor.
- **Always** support `cancel_event`: spawn a watcher task that
  `process.terminate()`s (then `kill()`s after a timeout), clean up the partial
  output file, and raise `ConversionCancelled`. See `z3ds_compress.py`.
- **Always** apply a stall timeout via `compute_progress_stall_timeout(...)` so a
  wedged binary can't hang a job forever.
- **Progress** is a 0 to 100 int. If the binary doesn't report percentages,
  estimate from output-file growth like z3ds does, or emit an elapsed-seconds
  heartbeat via the shared `SubprocessRunner` like dolphin does.
- Respect the shared priority policy via the `services.subprocess_runner`
  helpers (`ioprio_prefix(owner)`, `nice_prefix(owner)`, `apply_nice(owner)`,
  `info_timeout(owner)`, `verify_timeout(owner)`). These read the tool-neutral
  `COMPRESSATORIUM_TOOL_*` settings (with optional per-tool
  `COMPRESSATORIUM_<TOOL>_*` overrides) in one place, so you never re-read a
  chdman-named setting in your service.
- Track PIDs so the debug heartbeat can report them.

### 5.4 Register the plugin: `app/services/tools/__init__.py`

This is the single dispatch wiring step. Add the import and one `register` call:

```python
from .nszip import NszipTool
...
registry.register(NszipTool(settings.nszip_path))
# ...then the existing ChainTool line, which must stay last:
registry.register(ChainTool(registry))
```

Put your `register` call **above** the `ChainTool(registry)` line at the bottom
of the file. `ChainTool` is handed the registry itself and resolves its
component tools at construction, so everything it drives must already be
registered.

Once registered, `job_manager` and `convert.py` dispatch your mode through the
registry with no further edits. The registry validates on register: duplicate
ids or mode names raise, and every `ModeSpec.tool_id` must equal the tool's `id`.

### 5.5 Add the mode + info model: `app/models.py`

```python
class ConversionMode(str, Enum):
    ...
    Z3DS_COMPRESS = "z3ds_compress"
    NSZIP_COMPRESS = "nszip_compress"        # NEW
    ...

class NszipInfo(BaseModel):                  # NEW, shape it like Z3DSInfo
    file: str
    size: int
    size_display: str
    format: str | None = None
    extension: str
    compressed: bool
    compression_type: str | None = None
```

Every `ModeSpec.mode` must equal a `ConversionMode` value: the route layer
validates the incoming `request.mode` against `ConversionMode`, so an unlisted
mode never reaches dispatch. Keep a consistent prefix (`nszip_`) if you add a
second mode (for example `NSZIP_EXTRACT = "nszip_extract"`).

### 5.6 The job pipeline: usually nothing

`job_manager._process_job` dispatches convert and verify generically:
`registry.for_mode(job.mode.value).convert(...)` and
`registry.for_mode(job.mode.value).verify(...)`. `_queue_job_locked` computes the
output path via `registry.for_mode(mode.value).output_path(...)`. So once your
plugin is registered, **the pipeline needs no edits** for a standard
compress-style tool.

Touch `job_manager` only for special post-processing. The current special cases
are chdman-specific: GAME/NAME disc-id tagging after `createcd`/`createdvd`, and
`.bin` sidecar handling for multi-track CD inputs. A clean single-file tool like
nszip needs none of that.

> **Exception: a *second* directory-input tool is not drop-in.** "The pipeline
> needs no edits" holds for file-based tools. The directory path is currently
> written for the only directory tool that exists, so a new one would silently
> inherit PS3 behavior until these are generalized into shared seams:
>
> - `job_manager._clear_existing_output` calls
>   `makeps3iso_service.remove_outputs(...)` for **every** `InputKind.DIRECTORY`
>   job, so overwrite cleanup for your tool would run makeps3iso's split-part
>   logic. This is the one that silently corrupts rather than errors.
> - `routes/convert.py::_plan_directory_job` raises PS3-specific skip reasons
>   (`PS3_FOLDER_INVALID`, `PS3_OUTPUT_INSIDE_SOURCE`,
>   `PS3_OUTPUT_OUTSIDE_VOLUMES`, `PS3_FOLDER_UNSAFE`), and the worker's
>   post-lock safety re-walk records a PS3-specific error.
>
> Generalize those first (a plugin-level cleanup hook and tool-neutral skip
> reasons), then add your tool. Per the repo's modularity rule, that
> generalization is the work — not a per-tool branch alongside the existing one.

### 5.7 Validation + output dispatch: `app/routes/convert.py`

Output paths and the generic spec-flag validation are handled by the registry,
so you add **only three data lines**, no `if`-block. Plan-time input validation
is table-driven: the generic check is "the input extension must be one the mode
declares", and `_BAD_EXTENSION_REASON` maps a tool id to the skip reason used
when it fails. So:

```python
# 1. app/routes/convert.py — the reason
class SkipReason(Enum):
    ...
    NSZIP_BAD_EXTENSION = auto()

# 2. its HTTP status + message
_SKIP_HTTP: dict[SkipReason, tuple[int, str]] = {
    ...
    SkipReason.NSZIP_BAD_EXTENSION: (400, "File is not a supported Switch dump"),
}

# 3. opt into the generic extension check
_BAD_EXTENSION_REASON: dict[str, SkipReason] = {
    ...
    "nszip": SkipReason.NSZIP_BAD_EXTENSION,
}
```

`chdman` is deliberately **absent** from `_BAD_EXTENSION_REASON`: it validates
by `.chd` presence (create needs a non-`.chd`, extract/copy need a `.chd`)
because it drops `.chd` from its `input_extensions`. Write a bespoke check only
if your tool has that kind of structural rule; otherwise the table entry is the
whole job.

Everything else is spec-driven:

- **Output path:** `_get_output_path` delegates to
  `registry.for_mode(mode).output_path(...)`. No branch to add.
- **Delete-on-verify:** `supports_delete_on_verify(mode)` reads
  `registry.spec(mode).supports_delete_on_verify`. Set the flag on your
  `ModeSpec`, not in `convert.py`.
- **Archive inputs:** the single guard in `plan_job` rejects archive (`::`)
  members for any mode whose `spec.allows_archive_input` is `False` (the
  default). Two separate concerns, don't get 'em crossed: *browsing* an archive
  lists every known source (`convertible_extensions()` minus the containers), so
  your members show up just by bein' declared `input_extensions` — no flag, no
  fuss. *Converting* a member in place is the gate: set `allows_archive_input=True`
  only when a mode should accept the member straight from the archive (that's
  what `archive_input_extensions()` gathers, and it's what `plan_job` and the
  recursive search path ride on). Leave it `False` when reprocessing the member
  from an archive is a pointless round trip (chdman *copy*, which would re-CHD a
  `.chd`) or when a member's only meant to be *seen*, not reprocessed (romz ROMs
  — visible in browse, never recompressed). Note chdman *extract* DOES opt in:
  pullin' a `.chd` out of an archive to decompress it back to a game image is a
  real, useful conversion.
  See §17.5.

### 5.8 Listings: nothing to do

**This step no longer exists.** `files.py` used to carry a hand-written flag
block per tool; it is now a single loop over `registry.all()`, and `FileEntry`
is tool-neutral. There are **no** `<tool>_convertible` / `has_<tool>` /
`<tool>_ready` / `<tool>_path` fields to add anywhere — in the model, the
directory scan, `search_files`, or the frontend. Adding them back would fail
`tests/test_files_outputs_parity.py`, which pins the exact JSON surface.

What the listing reports instead, entirely from your plugin:

| `FileEntry` field | Filled from |
|-------------------|-------------|
| `convertible_by: list[str]` | tool ids whose `input_extensions` accept the file (or `accepts_directory(path)` for a folder row) |
| `outputs: list[OutputStatus]` | each tool's `detect_output(path)` — this is what drives the "output already exists" badge |
| `verifiable_by: list[str]` | each tool's `verifies_path(path)` — gates the Verify/Info row actions |
| `archive_has_output`, `split_parts` | derived by `files.py` itself; no per-tool input |

So: declare accurate `input_extensions`, implement `detect_output` if you want
the "output exists" badge, and override `verifies_path` only if your tool
over-claims a container extension. The listing follows automatically, in both
the directory scan and recursive search.

### 5.9 Info + verify endpoints: `app/routes/info.py`

Both info and verify are **optional**. A tool whose unit of work has no path to
route on, or that has no meaningful user-facing integrity check, registers
neither — `makeps3iso` is the precedent (its only check is a backend PARAM.SFO
`TITLE_ID` readback, and a folder has no extension to route Info on). If you
skip them, also leave `getInfo` / `verify` / `verifyBatch` **undefined** in the
frontend descriptor (§5.11) so the row-action helpers skip your tool.

Verify endpoints are **factory-generated**. You don't hand-write them, but there
are **two** edits, not one:

1. **Add a `_VERIFY_CONFIG["<tool>"]` entry** (a `_VerifyRouteConfig` dataclass) near
   the top of `info.py`. It carries the per-tool facts the factory has no other way
   to know: `url_prefix` (e.g. `"cso-"` → `/api/cso-verify`; `""` for chdman),
   `service=lambda: <service_singleton>` (resolved at call time so tests can rebind
   it), the three FastAPI route names (`verify_<tool>` / `_events` / `_batch_events`),
   `bad_ext_detail` (the 400 message), and `verify_error_prefix` (the 500 message).
   The factory reads accepted extensions off `tool.verify_extensions`, so there's no
   per-tool extension constant here.
2. **Add the (2-arg) register call** alongside the existing tool registrations. The
   config is looked up from `_VERIFY_CONFIG` by `tool.id`; it is **not** passed in:

```python
verify_cso, verify_cso_events, verify_cso_batch_events = register_verify_routes(
    router, registry.get("cso"),
)
```

`register_verify_routes(router, tool)` generates the trio
(`/api/nszip-verify`, `/api/nszip-verify/events`,
`/api/nszip-verify-batch/events` — the `url_prefix` prepended to `verify`) from
the plugin's `verify_extensions`, acquires the global verify lane
(`_acquire_verify_lane_or_429`, bounded by `workload_limiter`), and calls
`verification_store.mark_verified(path)` on success. No per-tool extension
constants are needed; the factory reads them off the plugin.

Info is hand-written per tool. Add a `GET /api/nszip-info` endpoint modeled on
`get_z3ds_info`, returning your `NszipInfo` model, plus its small
`_is_<tool>_info_file(path)` guard and `<TOOL>_INFO_EXTENSIONS` constant (mirror
the z3ds/nsz pair just above the endpoints). No router registration is needed;
`info.router` is already mounted under `/api` in `app/main.py`. Also import your
service singleton at the top of `info.py` so the `_VERIFY_CONFIG` lambda can
resolve it by module global.

### 5.10 Frontend: `src/lib/api/endpoints.js`

Add client methods on the `api` object next to the z3ds ones. The naming
convention is `get<Tool>Info` / `verify<Tool>` / `verifyBatch<Tool>`:

- `getNszipInfo(path)`, like `getZ3DSInfo`.
- `verifyNszip(path, {onProgress})`, single-file SSE. Routes through
  `verifyEventSource` from `sse.js`.
- `verifyBatchNszip(paths, {onProgress, onFileComplete, signal})`, batch SSE.
  Routes through the module-local `runBatchVerify` helper, which wraps
  `sseFetchPost` from `sseFetch.js`.

`createJob` / `createBatchJobs` are generic over `mode`, so no change there: the
registry's submit path passes `mode: 'nszip_compress'`. The verify URL is
**derived automatically** from `verifyPrefix: 'nszip'` in the registry
descriptor; there's no URL map to edit.

### 5.11 Frontend: `src/lib/tools/registry.js`

The frontend is a **Svelte 5 + Vite SPA** under `src/`, driven by a single
declarative tool registry (`src/lib/tools/registry.js`). The registry is the only
place that knows tool identity; there are no `if (tool === ...)` branches
downstream. Adding `nszip` is **one new entry** appended to the `TOOLS` array:

```js
// src/lib/tools/registry.js
{
  id: 'nszip',
  label: 'Switch',
  hint: 'Compress Nintendo Switch dumps (NSP / XCI).',
  // URL segment for /api/{prefix}-verify and /api/{prefix}-verify-batch
  verifyPrefix: 'nszip',
  sourceExts: ['.nsp', '.xci'],
  verifyExts: ['.nsz', '.xcz'],
  modeGroups: ['nszip'],
  groups: { nszip: 'Nintendo Switch' },   // human labels for the tool's group ids
  defaultMode: 'nszip_compress',
  glyph: 'NSW',                           // 2-3 char affordance for sidebar / dashboard
  accent: 'var(--badge-dat-match)',       // CSS color or token

  // nszip is a FIXED compressor: no codec dropdown, no level slider, so it
  // declares no compression fields at all. To expose compression controls,
  // see "Compression UI fields" right after this block.
  compressionStyle: 'none',

  modes: [
    { mode: 'nszip_compress', kind: 'compress', label: 'Compress to NSZ/XCZ',
      group: 'nszip',
      outputExt: null,                    // mapped from input extension
      inputExtensions: ['.nsp', '.xci'],
      // inputKinds: ['directory'],       // only for a folder-input mode
      supportsCompression: false,         // must match the ModeSpec in §5.3
      supportsCompressionLevel: false,    // ditto — parity is enforced by tests
      supportsDeleteOnVerify: true,
      allowsArchiveInput: false,
      // supportsSplit: true,             // frontend-only capability flag;
                                          // there is NO ModeSpec.supports_split
                                          // on the backend — `split` rides the
                                          // job request and every convert()
                                          // accepts it (see §5.3)
    },
  ],
  getInfo:     (path) => api.getNszipInfo(path),
  verify:      (path, opts) => api.verifyNszip(path, opts),
  verifyBatch: (paths, opts) => api.verifyBatchNszip(paths, opts),
  productPath: (path) => path.replace(/\.nsp$/i, '.nsz').replace(/\.xci$/i, '.xcz'),
},
```

#### Compression UI fields

The example above is a **fixed compressor** and declares none of these. Add them
only when your tool really exposes compression choices:

| Field | Meaning |
|-------|---------|
| `compressionStyle` | `'none'` / `'multi'` (chdman: a comma-joined codec list) / `'single-with-level'` (one codec + a numeric level). Drives which picker `CompressionPicker.svelte` renders. |
| `compressionCodecs` | `[{ value, label, hint }]` for the dropdown. |
| `compressionLevelRange` | `{ min, max, default }` for the slider. Falls back to `DEFAULT_COMPRESSION_LEVEL_RANGE` when absent. |
| `defaultCompression` | The initial selection **and** what the shared "Reset to default" button restores. Read by `conversion.svelte.js` — declaring it here is the whole wiring; there is no per-tool branch in the store. |

**Two invariants, both enforced by tests — get them wrong and CI fails:**

1. **Frontend and backend flags must match.**
   `tests/test_frontend_parity_186.py` compares each mode's
   `supportsCompression` / `supportsCompressionLevel` against the Python
   `ModeSpec`'s `supports_compression` / `supports_compression_level`. Flip a
   flag in one registry and you must flip it in the other.
2. **The declared flags must match what the service actually does.** Turning on
   `supportsCompressionLevel` gives the user a slider whose value arrives as
   `compression` in `convert()`; if your `_build_command` ignores it, the
   control is a lie. Wire the flag and the command together.

Live references: `chdman` (`'multi'`), `dolphin` and `nsz`
(`'single-with-level'`), `z3ds` (`'none'`).

The `ToolDescriptor` / `ModeEntry` JSDoc typedefs at the top of `registry.js`
are the authoritative schema — read them before adding an entry. Two shapes
worth knowing:

- **No Info/Verify.** Leave `getInfo` / `verify` / `verifyBatch` as `undefined`
  and `verifyExts` empty; the row-action helpers (`infoToolsForPath`,
  `toolForVerifyPath`) then skip the tool. **Keep `sourceExts` populated** for a
  file-based tool. `makeps3iso` empties it only because its input is a
  *directory* with no extension to match. `sourceExts` is a separate fact from
  your modes' `inputExtensions` and is what feeds `registry.allFilterableExts()`
  (the file-list filter dropdown), `registry.toolsForSourcePath()`, and the
  `fileIcon.js` disc/game buckets — emptying it on a file tool makes its inputs
  unfilterable and unstyled even though conversion still works.
- **A chain mode lives on an existing tool's entry**, not its own: `cso_to_chd`
  is a mode inside the `cso` descriptor with `group: 'chain'`. The backend's
  synthetic `chain` tool has no frontend descriptor of its own.

That's the entire frontend change apart from the two one-line registrations in
§3.3 items 22a and 22c. You do **NOT** need to:

- Edit a `VERIFY_URL` map. `registry.verifyUrl(toolId, kind)` derives both single
  and batch URLs from `verifyPrefix` (`''` for chdman, `'<segment>'` otherwise).
- Edit a `VALID_TOOLS` set. `ui.svelte.js` reads `registry.ids()`.
- Edit a `groupLabel()` switch. Group labels live on `tool.groups`; the lookup is
  `registry.groupLabel(group, toolId)`.
- Edit the Sidebar, Workspace, file list, conversion config, or any modal. They
  call `registry.specFor(mode)`, `registry.forTool(id)`,
  `registry.modesByGroup(id)`, `registry.toolForVerifyPath(path)`, and so on.
- Edit `conversion.svelte.js` for compression defaults. It reads
  `defaultCompression` / `compressionLevelRange` off the descriptor.
- Retype extension lists in `fileIcon.js` or mode rows in `HelpView.svelte`.
  Both derive from the registry (see §3.3 items 22a and 22d).
- Add a `<tool>_convertible` fallback to `FileRow.svelte`. It reads
  `entry.convertible_by` only; the old per-tool booleans are gone.

> **Build step.** The SPA compiles with Vite. Run `npm run dev` for HMR against
> the FastAPI backend, or `npm run build` to emit `static/index.html` +
> `static/assets/*` (used by FastAPI in production and by the Docker
> `frontend-builder` stage). When editing any `.svelte` file, run it through the
> Svelte MCP `svelte-autofixer` per the project contract.

### 5.12 Tests

Add, modeled on the existing suites:

- `tests/test_nszip_routes.py`, info + verify endpoint behavior (copy
  `tests/test_z3ds_routes.py`).
- `tests/test_nszip_service.py`, `convert`/`verify` happy path, cancel path,
  bad-extension rejection (copy `tests/test_z3ds_verification_service.py`).
- Extend `tests/test_tool_registry.py` so your mode resolves to your tool with
  the right spec flags (and bump the resolved-mode count).
- Extend `tests/test_mode_parity_fixes.py` so single-job and batch-job validation
  stay in lockstep for the new mode.
- Extend `tests/test_dispatch_routing.py` so convert + verify dispatch to your
  service.

Run them:

```bash
# from the repo root, with app/ on PYTHONPATH
PYTHONPATH=app python -m pytest -q tests/test_nszip_routes.py \
    tests/test_tool_registry.py tests/test_mode_parity_fixes.py
```

**`tests/conftest.py` does *not* stub tool binaries** — it is DB-only. Each
per-tool suite mocks what it needs: route tests monkeypatch
`info_routes.<service>`, service tests monkeypatch
`app.services.<tool>.asyncio.create_subprocess_exec`. Copy the mocking style
from the suite you're cloning rather than expecting a shared fixture.

Then run the whole suite (`PYTHONPATH=app python -m pytest -q tests`) — the
guard tests are designed to tell you what you missed. In particular
`tests/test_frontend_registry_derives_186.py` fails on a tool with no
`TOOL_MEDIA` bucket or a mode with no Help blurb, and
`tests/test_files_outputs_parity.py` fails if you added a per-tool field to
`FileEntry`.

### 5.13 Docs + version

- Update `README.md` (tool table / supported formats) and
  `docs/RELEASE_NOTES.md`.
- Update the FastAPI app description in `app/main.py` (the `description=` shown
  at `/docs`) so it names the new tool. It lists every tool, so keep it current.
- Refresh the screenshots: a new tool changes the sidebar, and the images in
  `docs/screenshots/` are generated from `shots.yml` by shot-scraper, not
  captured by hand. Pushing runs the **Take screenshots** workflow, which
  regenerates and commits them; see `docs/SCREENSHOTS.md` to run it locally.
- If you changed a shared seam (not just used it), update
  `docs/DESIGN_tool_plugin_architecture.md` **and this guide** — see §3.6 items
  47–48.
- The version lives in `package.json`. A release bumps it and publishes a GitHub
  Release tagged `vX.Y.Z`, which is what triggers the image build (see §10).

---

## 6. Tandem checklist (copy/paste)

A new tool **is** a new platform, so do these together:

```
BINARY
[ ] Dockerfile: build/install binary into runtime image (+ runtime libs)
[ ] config.py: add <tool>_path Field with env alias

SERVICE (app/services/<tool>.py)
[ ] *_CONVERTIBLE_EXTENSIONS, *_OUTPUT_FORMATS, module-level singleton
[ ] convert(): exec (no shell), progress yields, cancel_event, stall timeout,
    nice/ionice, PID tracking, ConversionCancelled, final 100%
[ ] verify() + verify_stream(), info(), get_output_path()

PLUGIN (app/services/tools/<tool>.py)
[ ] BaseTool subclass: id, display_name, modes (ModeSpec rows),
    output_extensions, verify_extensions
[ ] delegate convert/verify/verify_stream/info/info_model/output_path/active_pids
[ ] optional: detect_output() for "output exists" badges
[ ] optional: verifies_path() to refine verify_extensions per-file when the
    tool over-claims a container extension (drives FileEntry.verifiable_by)
[ ] optional: embedded_hashes()/embedded_hash_is_exhaustive for the DAT-match
    fast path (default falls back to file-level SHA1)
[ ] optional: companion_exts (or override companion_outputs) if the mode writes
    sidecars beside its primary output
[ ] optional: accepts_directory() + input_kinds={InputKind.DIRECTORY} for a
    folder-input tool (design doc §3.3.4)
[ ] optional: allows_archive_input=True on source modes (both registries) — see §17.
    (Your members already show up in browse just by bein' declared input_extensions;
    this flag's only for *converting* them straight out of the archive.)

REGISTER (app/services/tools/__init__.py)
[ ] registry.register(<Tool>(settings.<tool>_path))
    (before the ChainTool registration at the end of the file)

MODE + MODELS (app/models.py)
[ ] ConversionMode.<TOOL>_<ACTION> with a consistent prefix
[ ] <Tool>Info model (if it has an info modal)
[ ] FileEntry: NOTHING — listings are tool-neutral (§5.8)

ROUTES
[ ] convert.py: SkipReason.<TOOL>_BAD_EXTENSION + _SKIP_HTTP entry +
    _BAD_EXTENSION_REASON["<tool>"] entry (three data lines, no if-block)
[ ] files.py: NOTHING — the scan loops over registry.all()
[ ] info.py (optional, skip if no user-facing verify/info):
    _VERIFY_CONFIG["<tool>"] entry + register_verify_routes(...) +
    /<tool>-info + _is_<tool>_info_file + service import

PIPELINE (app/services/job_manager.py)
[ ] usually nothing (registry dispatches convert + verify)
[ ] only for special post-processing (disc-id tags, multi-file sidecars)
[ ] NOT true for a SECOND directory-input tool: _clear_existing_output runs
    makeps3iso's cleanup for every InputKind.DIRECTORY job, and the directory
    skip reasons are PS3-specific. Generalize those seams first — see §5.6
[ ] NOT true for a SECOND keys-gated tool: GET /api/tools branches on
    tool.id == "nsz", so yours is never hidden — see §16.5

FRONTEND
[ ] src/lib/api/endpoints.js: get<Tool>Info, verify<Tool>, verifyBatch<Tool>
    (skip if the tool has no info/verify routes)
[ ] src/lib/tools/registry.js: one new entry in the TOOLS array — including
    defaultCompression / compressionStyle / compressionLevelRange if it has a
    compression UI
[ ] src/lib/util/fileIcon.js: one TOOL_MEDIA entry ('disc' or 'game')
[ ] src/lib/tools/helpModes.js: MODE_BLURBS line per mode (+ MODE_OUTPUT
    override for input-derived / companion-pair outputs)
[ ] src/lib/components/views/HelpView.svelte: the curated tool blurb only
[ ] src/lib/stores/conversion.svelte.js: NOTHING — reads the descriptor
[ ] src/lib/components/panels/FileRow.svelte: NOTHING — reads convertible_by
[ ] src/styles/tokens.css: --badge-<tool> (only if new accent), both :root + .dark

TESTS + DOCS
[ ] tests/test_<tool>_routes.py, tests/test_<tool>_service.py
[ ] extend: test_tool_registry (bump mode count), test_dispatch_routing,
    test_mode_parity_fixes, and the archive matrix if archive-aware
[ ] tests/conftest.py: NOTHING — it's DB-only; mock per test file
[ ] full suite green: PYTHONPATH=app python -m pytest -q tests
[ ] README / RELEASE_NOTES / package.json version bump
[ ] app/main.py: add the tool to the FastAPI description= string (shown at /docs)

PERIPHERAL (don't forget)
[ ] .dockerignore: confirm nothing new is excluded
[ ] requirements.txt: new pip deps (if any)
[ ] .local-bin/<binary>: local-dev copy (from-source tools)
[ ] entrypoint.sh: CLI-mode loop (only if headless batch support wanted)
[ ] disc_id.py: serial/title parser (only for new disc platforms w/ tagging)
[ ] migrations/: new Alembic rev (only if persisting new columns/tables)
[ ] docker-compose*.yml + docs/DEPLOYMENT.md + docs/DOCKER-COMPOSE.md: env/volume docs
[ ] screenshots refreshed (shots.yml / Take screenshots workflow) — a new tool
    changes the sidebar
[ ] docs/DESIGN_tool_plugin_architecture.md + this guide, IF you changed a
    shared seam rather than just using it
[ ] lint clean: ruff, pylint, eslint, hadolint (Dockerfile), markdownlint
[ ] CI awareness: hadolint + Trivy gate the release build; CodeQL scans new code
```

---

## 7. Which existing tool should I copy?

Every tool in the repo is a working reference implementation. Find the row that
matches the shape of what you're adding and copy that one — it will already
have solved the awkward part.

| Your tool… | Copy | Because it is the reference for |
|------------|------|---------------------------------|
| …is a plain compressor: one file in, one file out, both directions | **z3ds** (`services/z3ds_compress.py` + `tools/z3ds.py`) | The smallest complete plugin (~130 lines, mostly delegation). Progress estimated from output-file growth when the binary reports no percentage. **This is the default answer.** |
| …exposes several output formats from one binary | **cso** (maxcso) | Five modes on one service (`cso_compress`, `cso2_compress`, `zso_compress`, `dax_compress`, `cso_decompress`), with an effort-preset compression UI rather than a codec list. |
| …reuses a binary that already ships, and/or produces archives | **romz** (7z) | No Dockerfile work. Reuses `services/archive.py` for the read side. Shows `verifies_path()` / content-validating `detect_output()` for a tool that over-claims `.7z`/`.zip`, and the visible-but-not-convertible archive case (§17.5). |
| …needs user-supplied keys or secrets | **nsz** | pip-packaged binary, `SWITCH_KEYS` resolution, the throwaway-`$HOME` trick for a binary with no `--keys` flag, and UI gating via `GET /api/tools`. See §16 — **note §16.5**: the backend readiness check is still nsz-specific, so a second gated tool needs a shared seam first. |
| …takes a **folder**, not a file | **makeps3iso** | `accepts_directory()`, `input_kinds={InputKind.DIRECTORY}`, a dynamic `companion_outputs()` for split parts, the `split` convert kwarg, and a tool that registers *no* info/verify routes at all. Design doc §3.3.4. **Read the §5.6 warning first** — the directory job path still hard-codes makeps3iso's overwrite cleanup. |
| …writes sidecar files beside its output | **chdman** (`extractcd`) | `companion_exts` driving conflict detection, cleanup and size accounting from one place. |
| …runs an existing tool's output through another tool | **chain** (`tools/chain.py`) | `ChainSpec` / `ChainStep`: a synthetic tool with no binary that drives registered tools in order. Design doc §3.3.3. |
| …can report a content hash cheaply for DAT matching | **dolphin** | `embedded_hashes()` via `SubprocessRunner.run_capture()`, plus `embedded_hash_is_exhaustive=True` for recompressed containers. |

For a **binary-backed** tool — every row above except the last two — the *shape*
of the work is the same: a service that owns the subprocess, a plugin that owns
the metadata, one `registry.register(...)` line, and one entry in the frontend
`TOOLS` array. The last two rows are the exceptions:

- **A chain tool has no service and no new frontend entry.** `ChainTool` spawns
  nothing itself; it drives already-registered tools through the registry, and
  its mode lives *inside* an existing tool's descriptor (`cso_to_chd` sits in
  the `cso` entry under `group: 'chain'`). Adding a second chain mode means a
  new `ChainSpec` and a mode row on an existing descriptor — not a new service
  layer and not a new `TOOLS` entry, both of which would duplicate surface.
- **A second directory-input tool needs pipeline work first.** See the warning
  in §5.6.

---

## 8. The Docker image in depth: `Dockerfile`, `.dockerignore`, deps

The image is a **five-stage** multi-arch build. Four stages build things and
the fifth is the runtime:

| Stage | Base | Builds |
|-------|------|--------|
| `builder` | `debian:trixie-slim` | z3ds_compressor (from source) |
| `maxcso-builder` | `debian:trixie-slim` | maxcso (from source) |
| `makeps3iso-builder` | `debian:trixie-slim` | makeps3iso (from source) |
| `frontend-builder` | `node:lts-slim` | the Svelte UI (`npm ci && npm run build`) |
| *(runtime)* | `debian:trixie-slim` | the final image — no Node, no toolchain |

Every base image is **pinned by digest** (`debian:trixie-slim@sha256:…`), so
adding a stage means copying an existing `FROM` line verbatim rather than
writing a bare tag. A tool with a heavy or slow build gets its own stage (the
three source-built tools each have one) so it caches independently; a small
build can join the existing `builder` stage.

**Build args & arch.** `ARG TARGETARCH` (set in the runtime stage) is
`amd64`/`arm64`. A from-source build (g++) compiles per-arch automatically. A
downloaded `.deb`/binary needs a **per-arch URL + SHA256**, like the `mame-tools`
block which branches on `TARGETARCH` and verifies with `sha256sum -c`. Reproduce
that pattern for any pinned download.

**Builder stage:** install toolchain (`build-essential`, `libzstd-dev`, …),
`git clone`, compile, `chmod +x`. Add your build here.

**Runtime stage:**
- Add any runtime shared libraries to the `apt-get install` list. The CLI `zstd`
  is already present (z3ds verify uses `zstd -t`); `ionice`/`nice` come from
  `util-linux`.
- `COPY --from=builder /tmp/<tool>/<binary> /usr/local/bin/<binary>` next to the
  z3ds copy.
- Optional/best-effort installs (a tool that only exists on one arch) should
  follow the dolphin-emu pattern: install with `|| echo WARNING` and create a
  wrapper only `if command -v` succeeds. The service must then tolerate a missing
  binary gracefully (surface a clear runtime error).
- The image runs as **uid/gid 999 (`converter`)**. Install to a world-readable
  path (`/usr/local/bin`) and `chmod +x`.
- `HEALTHCHECK` only probes the web UI; no change needed.

**hadolint** runs in CI (`docker-image.yml` `lint` job) with
`failure-threshold: error` and `ignore: DL3008,DL3013`. Keep your `RUN` layers
clean (combine `apt-get update && install`, `rm -rf /var/lib/apt/lists/*`, pin
where the existing file pins).

**`.dockerignore`:** `app/`, `static/`, `migrations/`, `entrypoint.sh`,
`requirements.txt` are copied in. `tests/` and most `*.md` (except `README.md`)
are excluded, so this guide and your service tests never enter the image. If you
reference a new top-level file from the Dockerfile, confirm it isn't ignored.

**Python deps:** anything your service `import`s that isn't stdlib or already in
`requirements.txt` must be added there (picked up by `pip install -r` in the
runtime stage). Dev/test-only deps go in `requirements-dev.txt`.

**Local dev (`run_dev.sh`):** it bootstraps a `.venv` and runs uvicorn against
your host. From-source binaries are kept in `.local-bin/` (the repo ships
`.local-bin/z3ds_compressor`) so the app works without Docker.

Dropping a binary in `.local-bin/` is **not** sufficient on its own:
`run_dev.sh` does not add `.local-bin` to `PATH`, and every from-source tool's
setting defaults to an absolute image path (`/usr/local/bin/...`) that won't
exist on your host. Putting the binary on `PATH` doesn't help either — the
absolute default is what gets executed. You must set the override explicitly.
`run_dev.sh` sources `.env.local` first, so that's the place:

```bash
# .env.local — not committed
NSZIP_PATH=/abs/path/to/compressatorium/.local-bin/nszip
MAXCSO_PATH=/abs/path/to/maxcso
MAKEPS3ISO_PATH=/abs/path/to/makeps3iso
```

The two tools whose defaults are a bare binary name (`nsz`, `7z`) *do* resolve
from `PATH` and need no override.

---

## 9. Headless CLI mode: `entrypoint.sh`

`entrypoint.sh` supports `CHD_MODE=cli` (batch-convert a volume with no web UI;
see `docker-compose.cli.yml`). This path is **independent of the Python app**: it
shells out to `chdman` directly and currently only knows `createcd`/`createdvd`
over `*.gdi *.iso *.cue`, validating `CHDMAN_MODE` against that allow-list with a
`case` statement.

If your new tool/platform should be usable headlessly:
- Extend the `CHDMAN_MODE` case statement (or add a parallel `CONVERT_TOOL` env)
  to accept your mode.
- Add a globbing loop for your input extensions and invoke your binary, mirroring
  the existing `for i in *.gdi *.iso *.cue` block.
- Skip-if-output-exists logic (`[[ -e "${i%.*}.<ext>" ]]`) should match your
  output suffix.

If headless mode is out of scope, skip this: the web UI path (`exec uvicorn
main:app …`) already dispatches your tool via the Python pipeline.

The same file handles **PUID/PGID remap** and the privilege drop to `converter`
(`exec gosu converter`). No changes needed there.

---

## 10. CI/CD & GitHub workflows

| Workflow | Trigger | Relevance to a new tool |
|----------|---------|-------------------------|
| `docker-image.yml` | **release published** | Builds/pushes multi-arch image to Docker Hub + GHCR, runs **hadolint** (Dockerfile must pass), **Trivy** CRITICAL/HIGH scan (a vulnerable new dep can fail this), generates SBOM + provenance attestation, and **publishes `README.md` as the Docker Hub description**. Also syncs `package.json` from the release tag. |
| *Codacy* — **not a workflow** | PR | Runs as a GitHub App check ("Codacy Static Code Analysis"): ruff, eslint, pylint, bandit, etc. New Python/JS must lint clean. Honors `pyproject.toml`, `.pylintrc`, `eslint.config.js`, `.codacy.yml`. Configured in the Codacy UI — there is no `.github/workflows/codacy.yml` to edit. |
| `codeql.yml` | push, PR, weekly | CodeQL SAST. The matrix in this file is **python** and **javascript-typescript** only; the `Analyze (actions)` job you'll also see on PRs comes from the repo's code-scanning settings, not from here. New code is scanned automatically, no config change, but don't introduce flagged patterns (for example command injection, another reason for the no-`shell=True` rule). |
| `label.yml` + `labeler.yml` | PR | Path-based auto-labels. Add a glob if you want your area labeled. |
| `screenshots.yml` | push / manual | **Take screenshots**: rebuilds the README/docs captures from `shots.yml` and commits them. Relevant whenever your tool changes the UI. |
| `stale.yml` | schedule | Marks stale issues/PRs. Irrelevant. |
| *Renovate* (`renovate.json`) | schedule (Mondays) | Dependency PRs, via the Renovate app — **not** Dependabot; there is no `.github/dependabot.yml`. Adjust only if you introduce a new manifest. |

Key takeaways for a contributor:
- **The image only builds on a GitHub Release.** There is no build-on-push. To
  ship your tool you (or a maintainer) publish a release tagged `vX.Y.Z`; CI then
  builds `linux/amd64,linux/arm64` and tags `latest`/`beta` accordingly.
- **Trivy can block a release** if a new system/python package pulls a
  CRITICAL/HIGH CVE (`ignore-unfixed: true` softens this). Prefer pinned, patched
  packages; the `mame-tools` snapshot pin is the model.
- **Dockerfile changes must pass hadolint** locally before you push
  (`hadolint Dockerfile`).

---

## 11. Lint & quality gates you must satisfy

Run these before pushing (they mirror Codacy):

```bash
# from the repo root
ruff check .                             # config in pyproject.toml ([tool.ruff], py310)
pylint app                               # config in .pylintrc / pyproject.toml
PYTHONPATH=app python -m pytest -q tests # full suite
npm run lint                             # ESLint over JS + .svelte, eslint.config.js
hadolint Dockerfile                      # if you touched the Dockerfile
```

`PYTHONPATH=app` is not optional: intra-project imports are written
`from services.x import y`, so `app/` must be on the path (this is also why
`run_dev.sh` sets it).

Conventions baked into the configs that affect new tool code:

- **Line length 100** (`pyproject.toml [tool.pylint.format]`, ruff `py310`).
- **`bandit`** (`.bandit`) flags `subprocess` with `shell=True` and predictable
  temp paths. The services use `create_subprocess_exec` with argv lists and
  document the temp-dir reasoning in `config.py`. Follow suit or bandit/CodeQL
  will flag you.
- **Broad `except Exception`** at subprocess boundaries is allowed by
  `.pylintrc`/`pyproject.toml` *if* you log at the call site (review enforces
  this). The existing services do `logger.exception(...)`.
- **Import order** is owned by ruff/isort (pylint's `wrong-import-order` is
  disabled). Keep imports sorted.
- Markdown is linted by `.markdownlint.json`; CSS by `.stylelintrc.json`.

---

## 12. Database, migrations & persistence

The app uses **SQLite via SQLAlchemy + Alembic** (`app/services/db.py`,
`migrations/`). On startup, the `lifespan` handler in `app/main.py` calls
`apply_migrations()` (defined in the db module) to bring the schema to head and
then imports any legacy JSON stores.

For a **new tool**, you almost certainly need **no migration**, because the
persistence layers are generic over file paths:

- **`verification_store`** records "this output path was verified" keyed by path.
  Your verify endpoints call `verification_store.mark_verified(path)` and it just
  works for any extension.
- **`chd_metadata_store`** caches CHD-specific metadata; a non-CHD tool doesn't
  use it.

You need a migration **only** if you add new persisted columns/tables (for
example a tool-specific metadata cache). In that case:

```bash
./scripts/new_migration.sh "add nszip metadata table"
# edit the generated migrations/versions/000X_*.py (upgrade/downgrade)
PYTHONPATH=app python -m pytest -q \
    tests/test_alembic_migrations.py tests/test_db_migration.py
```

`migrations/env.py` and `migrations/versions/0001_baseline_schema.py` are the
references. `tests/test_alembic_migrations.py` validates head consistency.

---

## 13. Supporting services: disc_id, archive, DAT

These are **disc-format-specific** and usually irrelevant to a non-disc tool, but
matter when your *platform* is a game image processed by chdman.

- **`app/services/disc_id.py`** extracts a game serial/title (for example
  `SLUS-20312`) from PS1/PS2/PSP/Dreamcast sources and embeds GAME/NAME tags into
  the CHD after `createcd`/`createdvd` (driven from `job_manager._process_job`).
  To make a **new disc platform** get tags, add a parser branch in
  `extract_from_source` and a normalizer like `_normalize_ps_serial`. Not needed
  for RVZ/3DS/etc.
- **`app/services/archive.py`** lists the inputs that can be converted from
  inside a `.zip/.7z/.rar`. It no longer hardcodes a tool-specific set:
  `ArchiveService` reads `registry.archive_input_extensions()` (the union of
  `input_extensions` for every mode with `allows_archive_input=True`), with the
  module-level `CONVERTIBLE_EXTENSIONS` kept only as a fallback. So once your
  mode opts in via `allows_archive_input=True`, its members are surfaced
  automatically; there's nothing to edit here. You still own extraction +
  related-file handling in `job_manager._process_job` for multi-file formats
  (single-file inputs like `.3ds`/`.rvz` need nothing extra). Output paths for
  archive members flow through `archive_service._output_name_for_member`, which
  preserves the original extension so input-derived outputs (for example z3ds
  `.3ds` to `.z3ds`) map correctly. **§17** walks the whole archive-input path
  end to end.
- **DAT / MAMERedump** (`app/services/dat_*.py`, `app/routes/dat.py`,
  `app/services/file_hasher.py`) is for hash-matching dumps against DAT
  databases. Touch only if your platform participates in DAT verification.

---

## 14. End-to-end "in tandem" example map

For the `nszip` (Nintendo Switch) tandem example used in §5, here is every edit
on one screen:

```
Dockerfile                          builder: clone+g++ nszip; runtime: COPY binary, libs
.dockerignore                       (verify nothing new is excluded)
requirements.txt                    (only if service imports a new pip pkg)
.local-bin/nszip                    local-dev binary for run_dev.sh
entrypoint.sh                       (optional) CLI-mode loop for .nsp/.xci
app/config.py                       nszip_path Field (NSZIP_PATH)
app/services/nszip.py               NszipService + NSZIP_CONVERTIBLE_EXTENSIONS + singleton
app/services/tools/nszip.py         NszipTool plugin (BaseTool + ModeSpec rows)
app/services/tools/__init__.py      registry.register(NszipTool(settings.nszip_path))
app/models.py                       ConversionMode.NSZIP_COMPRESS; NszipInfo  (FileEntry: nothing)
app/routes/convert.py               SkipReason + _SKIP_HTTP entry + _BAD_EXTENSION_REASON entry
app/routes/files.py                 NOTHING (registry-driven scan)
app/routes/info.py                  _VERIFY_CONFIG entry + register_verify_routes(get("nszip")) + /nszip-info + _is_nszip_info_file + service import
app/services/job_manager.py         usually nothing (registry dispatches)
src/lib/api/endpoints.js            getNszipInfo, verifyNszip, verifyBatchNszip
src/lib/tools/registry.js           one new entry in TOOLS (+ defaultCompression etc.)
src/lib/util/fileIcon.js            one TOOL_MEDIA entry ('disc' or 'game')
src/lib/tools/helpModes.js          MODE_BLURBS line per mode (+ MODE_OUTPUT if needed)
src/lib/components/views/HelpView.svelte   the curated tool blurb only
src/lib/stores/conversion.svelte.js NOTHING (reads defaultCompression off the descriptor)
src/lib/components/panels/FileRow.svelte   NOTHING (reads entry.convertible_by)
src/styles/tokens.css               (only if new badge classes; both :root + .dark)
tests/test_nszip_routes.py          info+verify endpoint tests
tests/test_nszip_service.py         convert/verify/cancel/bad-ext tests
tests/test_tool_registry.py         mode resolves to nszip; counts + ext unions + matrices
tests/test_dispatch_routing.py      extend convert/verify dispatch ladder
tests/test_frontend_registry_derives_186.py  guard only — run it, don't edit it
tests/test_mode_parity_fixes.py     add nszip_compress to the parity matrix
tests/test_archive_conversion_e2e.py + test_archive_preference.py  archive matrix (if archive-aware)
tests/conftest.py                   DB-only; no tool-binary stub (mocks live per-test-file)
tests/test_files_outputs_parity.py  nothing to add — it pins the tool-neutral FileEntry surface
docker-compose*.yml                 (optional) NSZIP_PATH / CLI env docs
package.json                        version bump (release)
app/main.py                         add the tool to the FastAPI description= (shown at /docs)
README.md / RELEASE_NOTES.md        supported-formats table + changelog
DEPLOYMENT.md / DOCKER-COMPOSE.md    new env var, if any
```

---

## 15. Gotchas & conventions

- **The registry dispatches; don't add ladders.** Convert and verify run through
  `registry.for_mode(mode)`; output paths through the plugin's `output_path()`;
  validation through `registry.spec(mode)` flags. The old `_convert_service`
  if/elif ladders are gone. Add behavior by setting `ModeSpec` flags, not by
  branching on the mode string.
- **A few prefixes are still load-bearing.** Some code still branches on
  `mode.startswith("create"|"extract"|"dolphin_")` and on equality with
  `z3ds_compress` for special-casing (disc-id tagging, `.bin` sidecars,
  compression nuances). Choose a clear, unique prefix and grep for prefix checks
  when you add a mode that doesn't fit an existing family.
- **Single-job and batch endpoints must validate identically.** `create_job` and
  `create_batch_jobs` in `convert.py` share `plan_job`, and
  `tests/test_mode_parity_fixes.py` exists to catch drift. Keep them in lockstep.
- **The verify lane is global.** All verification across all tools shares
  `MAX_VERIFY_CONCURRENCY` via `workload_limiter`. The verify-route factory
  acquires the token for you (`_acquire_verify_lane_or_429`); don't bypass it.
- **Reuse `ConversionCancelled`.** It lives in
  `app/services/subprocess_runner.py` (re-exported via `services.chdman` and
  `services.tools.runner`). The pipeline's cancel handling in
  `job_manager._process_job` catches that specific class.
- **`MAX_CONCURRENT_JOBS` defaults to 1.** Conversions are I/O-heavy; the
  dispatcher runs them serially unless host capacity is validated. Don't assume
  parallelism in a service.
- **The frontend has a build step.** The Svelte 5 + Vite source lives in `src/`;
  `npm run build` emits the bundle into `static/`. Run `npm run lint` (ESLint
  flat config in `eslint.config.js`) and keep the style consistent.
- **The image runs as uid 999 (`converter`).** Binaries must be executable by a
  non-root user; install them to a world-readable path like `/usr/local/bin`.
- **Most convertible-source modes are archive-aware.** chdman *create* and
  *extract*, Dolphin, 3DS, Switch (nsz), CSO (all five modes) and the
  `cso_to_chd` chain accept members straight out of `.zip/.7z/.rar` (chdman
  extract decompresses a `.chd` pulled from an archive). Three opt out, each for
  its own reason: chdman *copy* (re-CHD'ing a `.chd` from inside an archive is a
  pointless round trip), *romz* (recompressing an already-archived ROM is
  recursive — its members stay visible but non-convertible, §17.5), and
  *makeps3iso* (its input is a folder, not a member). See **§17** for how to
  wire archive input into a new tool. If your tool genuinely can't read its inputs from inside
  an archive, leave `allows_archive_input=False` on its `ModeSpec` (the default)
  and the single guard in `plan_job` blocks archive (`::`) members automatically.
- **Binary path is config, not hardcoded.** Always read `settings.<tool>_path`
  (passed into the plugin, stored on the service in `__init__`) so deployments
  can relocate/override via env.

---

## 16. Tools that need user-supplied keys or secrets

Some platforms encrypt their content (Nintendo Switch is the first here). To
compress that content meaningfully a tool has to decrypt it first, which means
it needs cryptographic keys. Those keys are copyrighted and console-specific:
**the app must never ship them, and neither should the tool.** The operator
supplies their own, dumped from hardware they own, for content they own.

`nsz` (Switch) is the reference implementation. If you add another tool with the
same shape (keys, DRM, firmware blobs, account tokens), follow this pattern.

### 16.1 Never ship the secret

- **`.gitignore`** every common key/secret filename so a stray copy can't be
  committed (`prod.keys`, `*.keys`, `keys.txt`, `.switch/`, …).
- **`.dockerignore`** the same names so they can't be baked into the image even
  if present in the build context.
- **Never log the secret's contents.** Log the *path* at most. Review your
  service's debug logging for this before merging.
- The image runs as uid 999; the operator mounts the secret **read-only**, and
  it only needs to be world-readable, never writable.

### 16.2 Detection: one env var is the source of truth, with best-effort fallback

Add a single setting that points at the secret. Prefer a **directory** the
operator mounts (matches how most key tools already organize files) over a
single file path:

```python
# app/config.py
switch_keys_dir: str | None = Field(default=None, alias="SWITCH_KEYS")
```

The service resolves the actual file with a `resolved_keys_file()` /
`keys_available()` pair (see `app/services/nsz.py`):

- When the env var **is set**, it is authoritative: look only inside that
  directory; if the key isn't there, report unavailable (don't silently fall
  back, or the operator can't tell their config is wrong).
- When it is **unset**, do a best-effort search: first the tool's standard
  locations (`~/.switch`, `~/.config/...`) as cheap `os.path.isfile` checks, then
  a **bounded recursive walk** of the configured game volumes (`settings.volumes`)
  and the data dir. Wrap volume access in `try/except` so key discovery can never
  break a job.

**Bound the recursive walk.** A full unbounded walk of multi-TB volumes could
stall startup, so cap the directories visited (`_MAX_KEY_SEARCH_DIRS`), prune
junk dirs with the shared `utils.junk.is_junk_entry`, stop at the first hit, and
log a warning if the cap is reached telling the operator to set the env var. The
walk only runs as a fallback: when keys sit in a standard location or
`SWITCH_KEYS` is set, the cheap path returns first.

### 16.3 Supplying the secret to the binary

Check how the binary actually consumes keys before wiring this up; it is easy
to get wrong, and unit tests that mock the subprocess won't catch it. **Run the
real `--help`.** Two cases:

- **The binary takes a flag** (e.g. `--keys /path`): just append it in
  `_build_command`.
- **The binary loads keys at import/startup from a fixed location** (nsz reads
  `~/.switch/prod.keys` via `$HOME` and has *no* `--keys` flag. It even
  `input()`-prompts and exits if none is found, which hangs under a pipe): you
  cannot pass a flag. Instead run the child with a throwaway `$HOME` whose
  `.switch/prod.keys` is a symlink to the resolved key file. See
  `NszService._keys_home()`. Pass the modified env via `create_subprocess_exec(...,
  env={**os.environ, "HOME": tmp})` and clean the temp dir up in `finally`.

### 16.4 Fail safe, with an actionable message

Guard **before** spawning. If keys aren't available, raise a `RuntimeError`
whose message tells the operator exactly what to do (which env var, where to put
the file). The job fails cleanly with that text instead of the binary dying with
a cryptic error. Verify needs the same guard.

### 16.5 Gate the UI so the tool is hidden without keys

A tool that can never run without keys shouldn't clutter the UI. The backend
exposes availability and the frontend hides unavailable tools entirely:

- **Backend:** `GET /api/tools` (in `app/routes/info.py`) returns
  `{"available": [...], "unavailable": [...]}`. A tool lands in `unavailable`
  when its readiness check fails (for nsz, `keys_available()` is false).
  **This half is *not* generic yet:** `list_tools()` awaits
  `nsz_service.keys_available()` and branches on `tool.id == "nsz"` — there is
  no plugin readiness callback. A second gated tool stays permanently
  "available" and advertises a converter that can only fail at runtime, so you
  must either extend that endpoint or (better, per the modularity rule) add a
  readiness seam to the plugin contract — e.g. an `is_ready()` defaulting to
  `True` on `BaseTool` — and have `list_tools()` loop over it.
- **Frontend:** `App.svelte` fetches it on mount and calls
  `ui.applyToolAvailability(available)`, which stores `ui.hiddenTools`. Sidebar
  and the dashboard derive their tool list as
  `registry.all().filter((t) => !ui.hiddenTools.has(t.id))`, and the active tool
  falls back to a visible one if it gets hidden. No registry edits needed; this
  is generic over tool id.

### 16.6 Copyright posture

State it plainly in the docs (README has a "Legal note"): the project ships no
keys, firmware, or copyrighted content; the operator provides their own for
hardware and content they own. Keep dual-use framing honest: this compresses
backups the user already owns; it is not a circumvention tool, and the format
preserves the original protection measures.

### 16.7 Testing without the real secret

- **Unit tests** mock `create_subprocess_exec` and a dummy key file on disk, so
  they exercise the wiring (argv, `_keys_home`, the missing-keys guard,
  availability gating) without real keys. See `tests/test_nsz_service.py` and
  `tests/test_nsz_routes.py`.
- **A real round-trip test** (`tests/test_nsz_roundtrip.py`) runs the actual
  binary through the service on operator-supplied inputs and **skips** when they
  aren't present, so CI stays green. Inputs live in a git-ignored
  `testdata/<platform>/` scratch dir (only its README is tracked); the operator
  drops their keys and a sample dump there or points env vars at them.

---

## 17. Tools that read inputs from archives (ZIP/7z/RAR)

Users keep dumps inside `.zip`/`.7z`/`.rar` archives, so every tool that takes a
*convertible source* can convert a member straight out of the archive without a
manual unzip first. Today chdman *create* and *extract*, Dolphin, 3DS, Switch
(nsz), CSO and the `cso_to_chd` chain all support this — chdman extract even
decompresses a `.chd` pulled from an archive back to a game image. The three
opt-outs are chdman *copy* (a pointless round trip), *romz* (recursive; see
§17.5) and *makeps3iso* (folder input, so there is no member to convert).

The pipeline is **tool-agnostic and registry-driven**: a member arrives as a
`"<archive>::<member>"` pseudo-path, the job layer extracts it to a real temp
file before your `convert()` ever runs, and a single flag decides whether your
mode participates. You almost never write archive-specific code; you set one
flag and (for multi-file formats only) declare your sidecars. nsz is the
reference: enabling it was literally two flag flips plus tests (see
`app/services/tools/nsz.py` and the §17.7 test matrix).

### 17.1 Opt in: one flag, in both registries

Set `allows_archive_input=True` on each `ModeSpec` whose input is a convertible
*source*, in **both** the Python spec and the JS registry (they have no automated
cross-language parity test, so keep them in sync by hand):

```python
# app/services/tools/<tool>.py
ModeSpec(mode="nszip_compress", tool_id="nszip", kind=ModeKind.COMPRESS,
         ..., allows_archive_input=True),
```

```js
// src/lib/tools/registry.js, in your tool's modes[]
{ mode: 'nszip_compress', kind: 'compress', ..., allowsArchiveInput: true },
```

That is the entire opt-in for a single-file format. Everything below is either
free or only applies to multi-file (CD) inputs.

### 17.2 What you get for free

Once the flag is `True`, the registry and job pipeline do the rest:

- **Listing.** You get this whether or not you set the flag — browsin' an archive
  is global, scoped to known extensions. `ArchiveService` surfaces members by
  readin' `registry.convertible_extensions()` (every tool's `input_extensions`)
  minus the archive containers, so just declarin' your `input_extensions` puts
  your members in the browse results; there's nothin' to register in `archive.py`.
  (The narrower `archive_input_extensions()` — only the `allows_archive_input`
  modes — is what gates *conversion* in `plan_job` and the recursive search path;
  see §17.5 for visible-but-not-convertible members.)
- **Validation.** The single guard in `plan_job` (`convert.py`) rejects `::`
  members for any mode whose `spec.allows_archive_input` is `False`, and accepts
  them otherwise. Per-tool extension checks use `_input_extension(file_path)`,
  which is archive-aware (it reads the member's extension, not `.zip`), so your
  existing validation block needs no change.
- **Extraction + cleanup.** `job_manager._process_job` splits the pseudo-path,
  calls `archive_service.extract_file(archive, member)` to drop the member into a
  private temp dir, hands the **real on-disk path** to your `convert()`, and
  removes the temp dir when the job ends. Your service sees an ordinary file
  path; it never needs to know it came from an archive.

### 17.3 Output paths for archive members

The output is computed *before* extraction, from the member name, via
`archive_service._output_name_for_member(member)`. That helper flattens
subdirectories but **preserves the original extension** (`games/cart.nsp` ->
`games_cart.nsp`), then passes it to your plugin's
`output_path(mode, name, dir, treat_as_stem=True)`.

Because the flattened name keeps its real extension, your existing
`get_output_path_for_mode` maps it exactly like an on-disk file — there is no
separate archive branch to write. z3ds and nsz both ignore `treat_as_stem` for
this reason; they just look up `suffix` in their `*_OUTPUT_FORMATS` map. Accept
the `treat_as_stem` kwarg for interface parity and move on. (`_output_stem_for_member`,
which *drops* the extension, exists only for chdman's always-`.chd` existing-output
badging — don't route input-derived outputs through it.)

### 17.4 Multi-file (CD) inputs: declare your sidecars

Single-file formats (`.nsp`, `.xci`, `.3ds`, `.rvz`, `.iso`, …) need nothing
beyond the flag. A format whose "file" is really several files — a `.cue`/`.gdi`
that references `.bin`/track files — must extract its siblings too, or the
converter sees a dangling reference. `job_manager` handles this by calling
`archive_service.extract_related_files(archive, member, temp_dir)`, which
early-returns for everything except `.cue`/`.gdi` today. If you add a new
multi-file source, extend that helper (parse the manifest, extract referenced
members into the same temp dir). Switch/3DS/Dolphin are all single-file, so this
does not apply to them.

### 17.5 When *not* to opt in

Leave `allows_archive_input=False` (the default) when reprocessing the member
from an archive is a **pointless round trip** — e.g. chdman `copy`, which would
re-CHD a finished `.chd` back into another `.chd`. The guard rejects it (and
`tests/test_archive_conversion_e2e.py::test_archive_chd_member_rejected_for_recompress`
locks that in). Note the distinction from chdman `extract`: extract takes the
same `.chd` input but *opts in*, because decompressing a `.chd` pulled from an
archive back to its game image is a genuine, useful conversion — "output class"
alone doesn't disqualify a mode, only a no-op round trip does.

**Visible-but-not-convertible (no flag, that's the point).** A tool that
*produces* archives (romz packs a ROM into `.7z`/`.zip`) has a third case: the
packed member oughta be *seen* when a body browses into the archive, but
convertin' it in place would be plumb recursive (recompressing an
already-archived ROM). You don't do anything special for this — just leave
`allows_archive_input=False`. The ROM still shows in browse because the listing
is global over known source extensions and ROM extensions are romz sources; and
`plan_job` still rejects `archive.zip::game.gb` because no `allows_archive_input`
mode accepts it. The archive route derives `convertible_by` from
`registry.tools_accepting_archive_member(ext)` (not a bare extension match), so a
visible-only member is badged non-convertible and no rejectable conversion is
ever offered. The recursive *search* path rides the narrow convert-gate set, so
list-only members never even show up there. See
`tests/test_archive_conversion_e2e.py::test_romz_member_listed_but_not_recompressed`
and `tests/test_archive_preference.py::test_browse_listing_is_global_known_superset_of_convert_gate`.

### 17.6 Delete-on-verify from an archive

If your mode also sets `supports_delete_on_verify=True`, the source-deletion
snapshot is archive-aware: `build_delete_plan` calls
`utils.path_utils.strip_archive_path`, so the plan targets the archive container
on disk, never a (non-deletable) member inside it. chdman *create* and z3ds
already pair both flags; nsz *compress* now does too. No extra work — just don't
assume the source is a plain file in your own code.

### 17.7 Testing

Two registry-driven suites cover the whole path; extend both:

- **`tests/test_archive_conversion_e2e.py`** — add a row per direction to the
  `MATRIX` (input ext, mode, expected output ext). The fixture stubs every
  tool's `convert`, then drives the real route -> `plan_job` -> real extraction
  from a real on-disk `.zip` -> stubbed convert -> output-naming -> temp cleanup.
  This proves the member was genuinely extracted to a temp file and that the
  output lands next to the archive with the input-derived extension. Because
  `convert` is stubbed, no real binary (or keys, for nsz) is needed.
- **`tests/test_archive_preference.py`** — assert your source extensions are in
  `registry.archive_input_extensions()` (and that output-only extensions like
  `.chd` are *not*).

```python
# tests/test_archive_conversion_e2e.py — MATRIX rows for a Switch-shaped tool
(".nsp", ConversionMode.NSZ_COMPRESS,   ".nsz"),
(".xci", ConversionMode.NSZ_COMPRESS,   ".xcz"),
(".nsz", ConversionMode.NSZ_DECOMPRESS, ".nsp"),
(".xcz", ConversionMode.NSZ_DECOMPRESS, ".xci"),
```

Run them:

```bash
# from the repo root
PYTHONPATH=app python -m pytest -q \
    tests/test_archive_conversion_e2e.py tests/test_archive_preference.py
```
