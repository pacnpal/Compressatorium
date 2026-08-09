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
    if (!this.allowsInput(entry.path)) return false;
    // The extension match stays the source of truth (it encodes the mode's
    // declared inputs); the listing's `convertible_by` may only NARROW it, the
    // same rule `registry.verifyToolForPath` applies on the verify side. This is
    // what keeps the non-primary members of a multi-file source unselectable —
    // a split Wii U dump's game_part2.wud is a .wud by extension, but only
    // game_part1.wud drives the set, and plan_job rejects the rest.
    if (Array.isArray(entry.convertible_by)
        && !entry.convertible_by.includes(this.currentTool?.id)) {
      return false;
    }
    return true;
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
  CHDMAN_MAX_CODECS = 4;

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
  async submit(filePaths, { duplicateAction = 'skip' } = {}) {
    if (!filePaths?.length) return null;
    this.converting = true;
    try {
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
      return result;
    } catch (e) {
      toast.error(e?.message ?? 'Failed to create jobs');
      throw e;
    } finally {
      this.converting = false;
    }
  }
}

export const conversion = new ConversionStore();
