// Conversion config store, selected tool, mode, compression, output dir,
// delete-on-verify, duplicate-check / delete-plan results. Drives the
// ConversionConfig panel and the submission path into JobsStore.

import { toast } from 'svelte-sonner';
import { api } from '$lib/api/endpoints.js';
import { registry, DEFAULT_COMPRESSION_LEVEL_RANGE } from '$lib/tools/registry.js';
import { STORAGE_KEYS, readString, writeString } from '$lib/util/localStorage.js';
import { jobs } from './jobs.svelte.js';

function loadPrimaryTool() {
  const raw = readString(STORAGE_KEYS.PRIMARY_TOOL, 'chdman') ?? 'chdman';
  return registry.forTool(raw) ? raw : 'chdman';
}

// Each tool declares its own `defaultMode` in the registry descriptor.
// chdman picks `createcd` (legacy/backend default for cue/bin/ISO), dolphin
// picks rvz, z3ds picks compress. No tool-specific branching here.
function defaultModeFor(toolId) {
  return registry.defaultMode(toolId) ?? 'createcd';
}

// Per-tool default compression seed. chdman accepts a comma-separated codec
// list (zlib is the historical default); Dolphin RVZ/WIA codecs are a
// disjoint set (`dolphin-tool -c [none|zstd|bzip|lzma|lzma2]`), so a chdman
// codec like 'zlib' would be rejected by the dolphin binary if the user
// submitted a Dolphin job without first opening the codec picker. Seed with
// a tool-appropriate default so the first submission always works.
function defaultCompressionFor(toolId) {
  // The per-tool default codec seed now lives on each registry descriptor
  // (`defaultCompression`), so this is a lookup instead of an
  // `if (toolId === ...)` ladder. Each tool's rationale (Dolphin zstd, Switch
  // 'solid', CSO/romz 'max' effort preset) is documented inline in registry.js.
  return registry.forTool(toolId)?.defaultCompression ?? ['zlib'];
}

// The slider level a tool boots / resets to: its registry range default, or the
// shared fallback for tools that declare no range (they hide the slider). Used
// at construction so the very first submit after a reload — before the async
// `loadServerPrefs()` resolves — already matches the selected tool's default
// (e.g. Switch's 18, not Dolphin's 19).
function defaultLevelFor(toolId) {
  const range = registry.forTool(toolId)?.compressionLevelRange ?? DEFAULT_COMPRESSION_LEVEL_RANGE;
  return String(range.default ?? DEFAULT_COMPRESSION_LEVEL_RANGE.default);
}

const PREF_SAVE_DEBOUNCE_MS = 500;
const isBrowser = typeof window !== 'undefined';

const INITIAL_TOOL = loadPrimaryTool();

class ConversionStore {
  primaryTool = $state(INITIAL_TOOL);
  // Mode must initialize from the persisted tool, defaulting to a chdman
  // mode (createcd) when the persisted tool is dolphin/z3ds would submit
  // wrong duplicate checks and compression flags before setPrimaryTool runs.
  mode = $state(defaultModeFor(INITIAL_TOOL));
  compressionSelection = $state(defaultCompressionFor(INITIAL_TOOL));
  compressionLevel = $state(defaultLevelFor(INITIAL_TOOL));
  outputDir = $state('');
  deleteOnVerify = $state(false);
  split = $state(false);
  customFilterMode = $state(false);

  // How many re-pin rows the last submit recorded, for the caller to surface.
  lastRepinRecorded = $state(0);
  // The backend's own count of rows still waiting, as of the last plan. The
  // badge is set from this rather than added to, because `record()` supersedes
  // the row for a destination instead of stacking one — so a retry records a
  // row without changing the total. Null when nothing was planned.
  lastRepinPending = $state(null);

  duplicateCheck = $state(null);
  deletePlan = $state(null);
  converting = $state(false);

  // Server-saved per-tool compression defaults ({ [toolId]: "<wire value>" }).
  // Loaded once on boot; updated and PUT (debounced) whenever the user changes
  // compression, so the choice follows them across sessions and browsers.
  #compressionPrefs = {};
  #saveTimer = null;
  // Set once the user changes any compression control, so a late boot
  // preference fetch can't revert an edit made while it was in flight.
  #compressionTouched = false;

  // ─── Derived ──────────────────────────────────────────────────────────
  get currentTool() {
    return registry.forTool(this.primaryTool);
  }

  get currentSpec() {
    return registry.specFor(this.mode);
  }

  get supportsCompression() {
    return !!this.currentSpec?.supportsCompression;
  }

  get supportsCompressionLevel() {
    return !!this.currentSpec?.supportsCompressionLevel;
  }

  get supportsDeleteOnVerify() {
    return !!this.currentSpec?.supportsDeleteOnVerify;
  }

  get supportsSplit() {
    return !!this.currentSpec?.supportsSplit;
  }

  get allowsArchiveInput() {
    return !!this.currentSpec?.allowsArchiveInput;
  }

  /**
   * True when the active mode's unit of work is a directory (makeps3iso
   * folder_to_iso), declared via the registry mode's `inputKinds`. A
   * directory mode accepts only convertible folders, never files.
   */
  get isDirectoryMode() {
    const kinds = this.currentSpec?.inputKinds;
    return Array.isArray(kinds) && kinds.includes('directory');
  }

  /**
   * Whether the given listing entry is a valid input for the active mode.
   * The entry-aware companion to `allowsInput(path)`: it can see `entry.type`
   * and the backend's `entry.convertible_by`, which a bare path can't. Used by
   * fileBrowser for both selection gating and the convertible subset, so a
   * directory mode only ever submits convertible folders and a file mode never
   * submits a folder.
   *
   * - Directory mode: only a directory row the backend marked convertible by
   *   THIS tool qualifies (folders the detector accepts).
   * - File/archive mode: a directory never qualifies; otherwise fall back to
   *   the path-based `allowsInput`.
   */
  allowsInputEntry(entry) {
    if (!entry) return false;
    if (this.isDirectoryMode) {
      return (
        entry.type === 'directory'
        && Array.isArray(entry.convertible_by)
        && entry.convertible_by.includes(this.currentTool?.id)
      );
    }
    if (entry.type === 'directory') return false;
    // NOTE: deliberately does NOT narrow by `entry.convertible_by`. That flag is
    // a tool-level, mode-agnostic listing annotation, while this gate is
    // mode-specific, and the two disagree by design: chdman drops `.chd` from
    // `input_extensions` (so a finished `.chd` isn't badged as a convertible
    // source and its `convertible_by` is empty), yet `.chd` is exactly what
    // extract/copy take. Gating on it made every CHDMAN extract/copy row
    // unselectable. The mode-aware rejection lives in `plan_job`, which asks the
    // owning tool's `converts_path` — so a split Wii U dump's `game_part2.wud`
    // can still be ticked here but is rejected on submit with a clear message,
    // the same way the other per-file plan rejections behave. A mode-aware
    // listing annotation would let this gate narrow correctly; that's a
    // listing-surface change, not a selection-store one.
    return this.allowsInput(entry.path);
  }

  /**
   * True when the given path is a valid input for the currently
   * selected mode. Used by fileBrowser to gate selection so users
   * can't queue jobs the worker would just reject, e.g. selecting a
   * `.rvz` while in CHDMAN `createcd` mode.
   *
   * Paths inside archives (`archive.zip::dir/disc.cue`) are matched
   * against their internal extension, but only when the current spec
   * actually accepts archive input. Every convertible-source mode
   * (CHDMAN create, Dolphin, 3DS) now sets `allowsArchiveInput: true`,
   * so any supported file inside an archive can be converted. Only
   * CHDMAN extract/copy (which take a finished `.chd`, not a source)
   * keep `allowsArchiveInput: false`; submitting an archive member
   * there just makes `plan_job()` skip it and the user ends up with
   * zero queued jobs, so reject those up-front.
   *
   * When no mode is active or the spec has no `inputExtensions`
   * declared (the registry guarantees one, but defensively), we accept
   * anything to avoid silently blocking selection.
   */
  allowsInput(path) {
    if (!path) return false;
    const isArchiveMember = path.includes('::');
    if (isArchiveMember && !this.allowsArchiveInput) return false;
    const exts = this.currentSpec?.inputExtensions;
    if (!Array.isArray(exts) || exts.length === 0) return true;
    const member = isArchiveMember ? path.split('::').pop() : path;
    const lower = (member ?? '').toLowerCase();
    return exts.some((ext) => lower.endsWith(ext));
  }

  /**
   * The wire-format compression value sent to the backend.
   *
   * - chdman create/copy modes: comma-separated codec list (e.g. "zlib,lzma").
   * - Dolphin RVZ/WIA: "<codec>:<level>" (e.g. "zstd:19"). The backend's
   *   dolphin_tool service splits on `:` and passes the codec to
   *   `dolphin-tool -c <codec> -l <level>`. Sending only the level (e.g.
   *   "19") would be interpreted as the codec name and fail.
   * - "none" selection: the literal string "none".
   * - null when compression is not configurable for the current mode.
   */
  get compressionValue() {
    if (!this.supportsCompression && !this.supportsCompressionLevel) return null;
    const selection = this.compressionSelection.filter((v) => v && v !== 'none');
    if (this.compressionSelection.includes('none') && selection.length === 0) {
      return 'none';
    }
    if (this.supportsCompressionLevel) {
      // Dolphin RVZ/WIA: pick the first non-'none' codec and pair with level.
      const codec = selection[0];
      if (!codec) return 'none';
      // Normalize the level, the picker's number input lets the user
      // clear it temporarily (browsers allow empty string). If we
      // forwarded that we'd build "<codec>:" and the backend would
      // reject the token. Fall back to the registered default range
      // value when the field is empty or non-numeric, then clamp into
      // [min, max] so out-of-range edits also stay safe.
      const range = this.currentTool?.compressionLevelRange ?? DEFAULT_COMPRESSION_LEVEL_RANGE;
      const rawLevel = this.compressionLevel;
      const parsed = Number.parseInt(rawLevel, 10);
      const safeLevel = Number.isFinite(parsed)
        ? Math.min(range.max, Math.max(range.min, parsed))
        : (range.default ?? range.min);
      return `${codec}:${safeLevel}`;
    }
    if (selection.length === 0) return null;
    return selection.join(',');
  }

  // ─── Setters ──────────────────────────────────────────────────────────
  setPrimaryTool(toolId) {
    const tool = registry.forTool(toolId);
    if (!tool) return;
    // No-op guard so the App.svelte $effect that bridges
    // ui.workspaceTool → this store can fire whenever Svelte considers
    // the dependency dirty without resetting the user's customized
    // mode / compression selection on every reactive trigger.
    if (this.primaryTool === toolId) return;
    this.primaryTool = toolId;
    writeString(STORAGE_KEYS.PRIMARY_TOOL, toolId);
    // Same default-mode logic as the initial load, chdman keeps createcd
    // as its default, others use their first registered mode.
    this.mode = defaultModeFor(toolId);
    // Seed compression from the saved server preference for this tool, falling
    // back to the per-tool default.
    this.#applyCompressionPref(toolId);
  }

  // ─── Server-saved compression preference ────────────────────────────────
  /** Fetch saved per-tool compression defaults and apply the current tool's. */
  async loadServerPrefs() {
    try {
      const prefs = await api.getConversionPrefs();
      if (prefs && typeof prefs === 'object') {
        this.#compressionPrefs = prefs;
        // Don't clobber a selection the user made while the fetch was in flight.
        if (!this.#compressionTouched) this.#applyCompressionPref(this.primaryTool);
      }
    } catch {
      // Best-effort; leave the local defaults in place.
    }
  }

  /** Apply a saved compression string for `toolId`, else the tool default. */
  #applyCompressionPref(toolId) {
    const value = this.#compressionPrefs[toolId];
    const tool = registry.forTool(toolId);
    const isLevel = (tool?.compressionStyle ?? 'none') === 'single-with-level';
    // Seed the level from this tool's own default first, so a value from a
    // previously selected single-with-level tool can't leak across (e.g.
    // Dolphin's 19 bleeding into Switch, which defaults to 18).
    if (isLevel) {
      this.compressionLevel = defaultLevelFor(toolId);
    }
    if (!value) {
      this.compressionSelection = defaultCompressionFor(toolId);
      return;
    }
    if (value === 'none') {
      this.compressionSelection = ['none'];
      return;
    }
    if (isLevel) {
      const [codec, lvl] = value.split(':');
      this.compressionSelection = codec ? [codec] : defaultCompressionFor(toolId);
      if (lvl) this.compressionLevel = String(lvl);
      return;
    }
    this.compressionSelection = value.split(',').filter(Boolean);
  }

  /** Remember the current tool's compression value on the server (debounced). */
  #persistCompression() {
    this.#compressionTouched = true;
    const value = this.compressionValue;
    if (value == null) return; // mode without compression, nothing to save
    this.#compressionPrefs = { ...this.#compressionPrefs, [this.primaryTool]: value };
    if (!isBrowser) return;
    if (this.#saveTimer) clearTimeout(this.#saveTimer);
    const snapshot = { ...this.#compressionPrefs };
    this.#saveTimer = setTimeout(() => {
      this.#saveTimer = null;
      api.putConversionPrefs(snapshot).catch(() => {});
    }, PREF_SAVE_DEBOUNCE_MS);
  }

  setMode(mode) {
    if (!registry.specFor(mode)) return;
    this.mode = mode;
    // If the new mode doesn't support delete-on-verify (e.g. switching
    // from a chdman create mode to an extract mode), clear the flag.
    // The backend rejects the combination outright, so a sticky flag
    // would fail the next submission instead of silently degrading.
    if (!this.supportsDeleteOnVerify) this.deleteOnVerify = false;
    // Likewise clear the split flag when leaving a mode that offers it.
    if (!this.supportsSplit) this.split = false;
  }

  setCompression(list) {
    this.compressionSelection = Array.isArray(list) ? list.slice() : [];
    this.#persistCompression();
  }

  setCompressionLevel(level) {
    this.compressionLevel = String(level);
    this.#persistCompression();
  }

  /**
   * Toggle a chdman-style codec on/off. Selecting "none" clears the
   * rest; selecting any other codec removes "none" if present.
   *
   * chdman accepts up to 4 codecs in `-c` (per its CLI help and the
   * legacy UI). Refuse to add a 5th, the conversion would queue and
   * then fail at runtime. Removing a codec from an existing 4-long
   * selection still works.
   */
  get CHDMAN_MAX_CODECS() {
    // The registry owns the cap (chdman declares `maxCodecs`), so the manual
    // picker and the RomM automation editor enforce the same number.
    return registry.maxCodecsFor(this.mode);
  }

  toggleCodec(codec) {
    if (codec === 'none') {
      this.compressionSelection = ['none'];
      this.#persistCompression();
      return;
    }
    const current = this.compressionSelection.filter((c) => c !== 'none');
    if (current.includes(codec)) {
      this.compressionSelection = current.filter((c) => c !== codec);
      this.#persistCompression();
      return;
    }
    if (current.length >= this.CHDMAN_MAX_CODECS) {
      // Silently ignore, the picker chip is rendered with
      // pointer-events still active for visual feedback; the cap
      // message lives in the picker UI (CompressionPicker reads
      // CHDMAN_MAX_CODECS for the disabled/limit hint).
      return;
    }
    this.compressionSelection = [...current, codec];
    this.#persistCompression();
  }

  /** Replace selection with a single codec (dolphin RVZ/WIA, nsz solid/block). */
  setSingleCodec(codec) {
    this.compressionSelection = codec ? [codec] : [];
    this.#persistCompression();
  }

  /**
   * Reset the current tool's compression settings to its registry defaults.
   * Works for every compression-capable tool (chdman codec list, Dolphin/Switch
   * codec+level, CSO effort preset) with no tool-specific branching, and
   * persists the reset like any other change so it sticks across sessions.
   */
  resetCompression() {
    const toolId = this.primaryTool;
    const tool = registry.forTool(toolId);
    this.compressionSelection = defaultCompressionFor(toolId);
    if ((tool?.compressionStyle ?? 'none') === 'single-with-level') {
      this.compressionLevel = defaultLevelFor(toolId);
    }
    this.#persistCompression();
  }

  /**
   * True when the current tool's compression selection (and level, for modes
   * that expose one) already equals its defaults. Drives the disabled state of
   * the Reset-to-default button so it only lights up when there's something to
   * undo.
   */
  get isCompressionDefault() {
    const toolId = this.primaryTool;
    const def = defaultCompressionFor(toolId);
    const sel = this.compressionSelection;
    if (sel.length !== def.length || !sel.every((v, i) => v === def[i])) return false;
    if (this.currentSpec?.supportsCompressionLevel) {
      return String(this.compressionLevel) === defaultLevelFor(toolId);
    }
    return true;
  }

  // ─── Preflight ────────────────────────────────────────────────────────
  async checkDuplicates(filePaths) {
    if (!filePaths?.length) return null;
    try {
      this.duplicateCheck = await api.checkDuplicates(
        filePaths,
        this.outputDir || null,
        this.mode,
      );
      return this.duplicateCheck;
    } catch (e) {
      toast.error(e?.message ?? 'Failed to check duplicates');
      this.duplicateCheck = null;
      throw e;
    }
  }

  clearDuplicateCheck() {
    this.duplicateCheck = null;
  }

  async fetchDeletePlan(filePaths) {
    if (!filePaths?.length) return null;
    try {
      this.deletePlan = await api.getDeletePlan(filePaths, this.mode);
      return this.deletePlan;
    } catch (e) {
      toast.error(e?.message ?? 'Failed to build delete plan');
      this.deletePlan = null;
      throw e;
    }
  }

  clearDeletePlan() {
    this.deletePlan = null;
  }

  // ─── Submission ───────────────────────────────────────────────────────
  /**
   * @param {string[]} filePaths
   * @param {{ duplicateAction?: string, rommRepin?: boolean }} [opts]
   *   `rommRepin` records each source's RomM metadata before the jobs are
   *   queued, so a format RomM cannot hash-match (RVZ/CSO/NSZ/WUX/Z3DS) can be
   *   re-identified after its rescan. It must happen BEFORE the conversion:
   *   the provider ids are read from the RomM record for the source file, and
   *   that record is what goes stale once the source is converted or deleted.
   */
  async submit(
    filePaths,
    { duplicateAction = 'skip', rommRepin = false, rommPlatformId = null } = {},
  ) {
    if (!filePaths?.length) return null;
    this.converting = true;
    this.lastRepinRecorded = 0;
    this.lastRepinPending = null;
    // The metadata snapshot is taken before the batch is submitted, so any
    // path the batch does not end up queueing — a rejected submit, or one the
    // backend filters out during per-file validation — leaves a row describing
    // a conversion that will never run. Source path -> recorded destination.
    let recorded = {};
    // Source path -> the id of the row recorded for it. Retiring is keyed by
    // id, not by destination: `record()` supersedes, so between this plan and
    // its cancel another client can own the row that path now holds, and
    // cancelling by path would retire its live conversion's metadata.
    let recordedIds = {};
    try {
      if (rommRepin) {
        try {
          // The duplicate policy goes with it: the plan has to record the
          // path the batch will actually write. Under Rename the batch resolves
          // to `Game_1.rvz`, so recording the occupied base path would re-pin
          // the OLD file and leave the new one unidentified; under Skip the
          // conflicting sources are never queued at all.
          // The platform goes with it: the backend would otherwise walk every
          // platform's full catalog looking for these paths, which on a large
          // instance is hundreds of serialised requests before a small batch
          // is even queued. These rows all came from one platform's listing.
          const planned = await api.planRommRepin(
            filePaths, this.mode, this.outputDir || null, duplicateAction,
            rommPlatformId,
          );
          // Reported back to the caller rather than pushed into the RomM store
          // from here: conversion is imported by fileBrowser, which the RomM
          // store imports, so a static import back would be a cycle.
          this.lastRepinRecorded = planned?.recorded ?? 0;
          this.lastRepinPending = planned?.pending ?? null;
          recorded = planned?.recorded_paths ?? {};
          recordedIds = planned?.recorded_ids ?? {};
        } catch (e) {
          // Best-effort, like the disc-ID tagging hook: losing the metadata
          // snapshot costs a re-match in RomM, while refusing to convert costs
          // the user the thing they actually asked for.
          toast.warning(
            `Could not save RomM metadata (${e?.message ?? 'unknown error'}); `
            + 'converted files may need re-matching in RomM',
          );
        }
      }
      const result = await jobs.createBatch(filePaths, this.mode, {
        outputDir: this.outputDir || null,
        duplicateAction,
        compression: this.compressionValue,
        // Mask the flag at submit time too, defense in depth against
        // any code path that might set deleteOnVerify true without
        // going through setMode (the backend rejects the combination
        // for extract/raw modes).
        deleteOnVerify: this.supportsDeleteOnVerify && this.deleteOnVerify,
        // Same masking for the makeps3iso 4 GB FAT32 split toggle.
        split: this.supportsSplit && this.split,
      });
      // Report what the backend actually created. createBatch can
      // legitimately return fewer jobs than requested when the user
      // chose duplicateAction: 'skip', or when create_batch_jobs
      // rejects inputs during per-file validation, or [] when
      // everything was filtered out. Telling the user "Queued N" when
      // N rows were skipped is misleading.
      const created = Array.isArray(result) ? result.length : 0;
      const requested = filePaths.length;
      if (created === 0) {
        toast.warning(`No jobs queued (all ${requested} skipped)`);
      } else if (created < requested) {
        toast.success(`Queued ${created} of ${requested} job(s); ${requested - created} skipped`);
      } else {
        toast.success(`Queued ${created} job(s)`);
      }
      // Reconcile the planned rows against what the queue actually did.
      //
      // Planning resolves a destination by predicting the duplicate policy's
      // answer, and between predicting and queueing the prediction can go
      // stale: another job or an outside process takes the path Rename picked,
      // so the batch writes somewhere else. A row left pointing at the
      // predicted path would then be settled against whatever landed there —
      // this ROM's identity stamped on an unrelated file. So compare against
      // each job's real `output_path`, not just its source.
      const created_jobs = (Array.isArray(result) ? result : []).filter(Boolean);
      // The queue reports each job's source as the backend resolved it, which
      // is the symlink-free path; the row was recorded against the path the
      // browser submitted. For a library reached through a symlinked ancestor
      // those differ, and matching on the submitted spelling alone would find
      // no job for a source that was queued — retiring a row the conversion
      // still needs. A destination that some job is writing means the row is
      // live whatever the source is spelled like.
      const actualBySource = new Map(
        created_jobs.map((j) => [j.file_path, j.output_path]),
      );
      const queuedDestinations = new Set(
        created_jobs.map((j) => j.output_path).filter(Boolean),
      );
      const misdirected = {};
      for (const [source, planned] of Object.entries(recorded)) {
        const actual = actualBySource.get(source);
        if (!actual) continue;
        if (actual !== planned) misdirected[source] = actual;
      }
      if (Object.keys(misdirected).length) {
        // Re-record first, retire second. The old row is only wrong once the
        // new one exists: if the re-record fails — RomM went away between the
        // first plan and now — cancelling first would leave the conversion
        // running with no snapshot at all, and the operator none the wiser.
        // Losing this ordering is how metadata disappears silently.
        let replanned = null;
        try {
          replanned = await api.planRommRepin(
            Object.keys(misdirected), this.mode, this.outputDir || null,
            duplicateAction, rommPlatformId, misdirected,
          );
        } catch (e) {
          // Say so rather than swallow it: the conversion is already queued,
          // so this metadata now needs the manual path. The count is corrected
          // below so the badge does not promise a re-match that is not coming.
          toast.warning(
            `Metadata could not be saved for ${Object.keys(misdirected).length} `
            + `file(s) the queue redirected: ${e?.message ?? 'the request failed'}. `
            + 'Re-match those in RomM by hand after converting.',
          );
        }
        // Truthy is not enough: the plan endpoint skips a path it cannot
        // record (RomM no longer lists it, the destination is outside the
        // volumes) and still answers 200, so a partial result would retire
        // the old rows and report metadata that was never saved. Every
        // redirected source has to come back mapped to the path the queue
        // actually chose.
        const replannedPaths = replanned?.recorded_paths ?? {};
        const replannedIds = replanned?.recorded_ids ?? {};
        const replanComplete = Object.entries(misdirected).every(
          ([source, destination]) => replannedPaths[source] === destination,
        );
        if (replanned && !replanComplete) {
          toast.warning(
            'Metadata could not be saved for every file the queue redirected. '
            + 'Re-match those in RomM by hand after converting.',
          );
        }
        if (replanComplete) {
          await api
            .cancelRommRepin(
              Object.keys(misdirected)
                .map((k) => recordedIds[k])
                .filter((id) => id != null),
            )
            .catch(() => {});
          for (const [source, actual] of Object.entries(misdirected)) {
            recorded[source] = actual;
            recordedIds[source] = replannedIds[source];
          }
        } else {
          // Retire the old rows anyway. They point at a path this batch is no
          // longer writing — under Overwrite that is the file the conversion
          // was going to replace — and a row aimed at the wrong file is worse
          // than no row at all. The `finally` block cannot do it: these
          // sources did become jobs, so they are filtered out of `recorded`
          // below and would be left behind.
          await api
            .cancelRommRepin(
              Object.keys(misdirected)
                .map((k) => recordedIds[k])
                .filter((id) => id != null),
            )
            .catch(() => {});
          for (const source of Object.keys(misdirected)) {
            delete recorded[source];
            delete recordedIds[source];
          }
          // Only the ones that really were not re-recorded: a partial answer
          // still saved metadata for the sources it mapped, and counting
          // those as lost would under-report just as misleadingly.
          const lost = Object.entries(misdirected).filter(
            ([source, destination]) => replannedPaths[source] !== destination,
          ).length;
          this.lastRepinRecorded = Math.max(0, this.lastRepinRecorded - lost);
        }
      }
      // What is left in `recorded` after this is the set the `finally` block
      // retires: rows whose source did NOT become a job at all.
      //
      // Retire by destination, but decide by destination too: two sources can
      // resolve to one output (duplicate basenames landing in a single output
      // folder), the batch collapses them into one job, and cancelling on
      // behalf of the source that lost would delete the row the winner needs.
      // A destination any queued source claims is never retired.
      const queuedSources = new Set(created_jobs.map((job) => job.file_path));
      const claimed = new Set(
        Object.entries(recorded)
          .filter(([source]) => queuedSources.has(source))
          .map(([, destination]) => destination),
      );
      recorded = Object.fromEntries(
        Object.entries(recorded).filter(
          ([source, destination]) =>
            !queuedSources.has(source)
            && !claimed.has(destination)
            // ...and not a row whose destination a job is writing under a
            // source spelled differently (a symlinked ancestor, resolved by
            // the backend). Retiring that row loses the metadata for a
            // conversion that is running.
            && !queuedDestinations.has(destination),
        ),
      );
      // Report only the rows that survive, or the toast would claim metadata
      // was saved for conversions that are not happening.
      this.lastRepinRecorded = Math.max(
        0, this.lastRepinRecorded - Object.keys(recorded).length,
      );
      return result;
    } catch (e) {
      toast.error(e?.message ?? 'Failed to create jobs');
      throw e;
    } finally {
      this.converting = false;
      // Retire the rows for everything that did not become a job. They are
      // harmless if this fails — a row whose output never changes is never
      // settled and ages out on its own — so it must not mask a real error.
      // By id: `recorded` decides *which* rows (its keys are the sources that
      // did not become jobs), `recordedIds` names them.
      const orphaned = Object.keys(recorded)
        .map((source) => recordedIds[source])
        .filter((id) => id != null);
      if (orphaned.length) {
        // Awaited for its *count*, not for its success. `lastRepinPending` was
        // measured by the plan call, before these rows were retired, so leaving
        // it there showed the pre-reconciliation backlog on the badge — and a
        // fire-and-forget retirement can never correct it, so it stayed wrong
        // until a status reload or a settle pass. The reply carries the
        // authoritative count; a failure leaves the old one, which is the
        // existing best-effort behaviour and still ages out on its own.
        try {
          const reconciled = await api.cancelRommRepin(orphaned);
          if (typeof reconciled?.pending === 'number') {
            this.lastRepinPending = reconciled.pending;
          }
        } catch (_e) {
          // Harmless: a row whose output never changes is never settled.
        }
      }
    }
  }
}

export const conversion = new ConversionStore();
