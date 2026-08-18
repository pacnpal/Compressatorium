// RomM integration state: connection, settings, platforms, automation rules,
// and the metadata re-pin queue.
//
// The ROM rows themselves deliberately live in `fileBrowser` (RomM is a third
// listing source there, alongside search and archive views) because the backend
// returns the same DirectoryListing/FileEntry shape a directory does — so
// FileList, FileRow and ConvertPanel render the catalog unchanged. This store
// owns only what is genuinely RomM-specific.

import { api } from '$lib/api/endpoints.js';
import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
import { conversion } from '$lib/stores/conversion.svelte.js';
import { ui } from '$lib/stores/ui.svelte.js';
import { registry } from '$lib/tools/registry.js';

/** Weekday labels for the schedule editor; index === Date.getDay() - 1 (Mon=0). */
export const DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

export const ORDER_LABELS = {
  name: 'Name (A–Z)',
  size_desc: 'Largest first',
  size_asc: 'Smallest first',
  id: 'Oldest added',
  newest: 'Newest added',
};

class RommStore {
  // Connection / configuration
  status = $state(null);
  statusLoading = $state(false);
  statusError = $state(null);

  settings = $state(null);
  settingsSaving = $state(false);
  testResult = $state(null);
  testing = $state(false);

  // Platforms
  platforms = $state([]);
  platformsLoading = $state(false);
  platformsError = $state(null);
  selectedPlatformId = $state(null);

  // Automation
  rules = $state({});
  ruleDefaults = $state(null);
  ruleOptions = $state({ orders: [], duplicate_actions: [], days: [] });
  ruleState = $state({});
  rulesLoading = $state(false);
  rulesSaving = $state(false);
  rulesError = $state(null);
  sweepResult = $state(null);
  sweeping = $state(false);

  // Re-pin queue
  repinning = $state(false);

  get configured() {
    return this.status?.configured === true;
  }

  get connected() {
    return this.status?.connected === true;
  }

  /** True only when RomM answers AND its library is actually visible here. */
  get usable() {
    return this.connected && this.status?.libraryRootMounted === true;
  }

  get pendingRepins() {
    return this.status?.pendingRepins ?? 0;
  }

  /**
   * Output extensions that keep RomM's DAT match through a conversion.
   *
   * Served by the backend rather than hardcoded here: which formats RomM can
   * hash is RomM's knowledge, and duplicating the list in the frontend is
   * exactly the second source of truth that goes stale when RomM adds one.
   */
  get datSafeOutputExts() {
    return this.status?.datSafeOutputExts ?? [];
  }

  /** True when *modeEntry* produces a format RomM cannot hash-match itself. */
  losesDatMatch(modeEntry) {
    // Mirrors the backend gate (`romm_repin.mode_needs_repin`) exactly: an
    // extension outside the DAT-safe set loses the match, and that includes
    // the input-mapped modes whose outputExt is null (Z3DS, NSZ) — their
    // outputs are .z3ds / .nsz, which RomM cannot hash either. Guessing "safe"
    // there hid the warning on the two formats that most need it.
    if (!modeEntry || this.datSafeOutputExts.length === 0) return false;
    const ext = (modeEntry.outputExt ?? '').toLowerCase();
    return !this.datSafeOutputExts.includes(ext);
  }

  /**
   * Expected output/input size ratio for a mode, or null when unknown.
   *
   * Served by the backend (SIZE_RATIOS) rather than duplicated here, so the
   * savings estimate and the progress estimator can never disagree.
   */
  sizeRatioFor(mode) {
    const ratio = this.status?.sizeRatios?.[mode];
    return typeof ratio === 'number' ? ratio : null;
  }

  get selectedPlatform() {
    return this.platforms.find((p) => p.id === this.selectedPlatformId) ?? null;
  }

  platformName(platformId) {
    const id = Number(platformId);
    return this.platforms.find((p) => p.id === id)?.name ?? `Platform ${platformId}`;
  }

  // ─── status / settings ────────────────────────────────────────────────

  async loadStatus() {
    this.statusLoading = true;
    this.statusError = null;
    try {
      const data = await api.getRommStatus();
      this.status = {
        configured: data?.configured === true,
        connected: data?.connected === true,
        version: data?.version ?? null,
        libraryRoot: data?.library_root ?? '',
        libraryRootMounted: data?.library_root_mounted === true,
        tokenSet: data?.token_set === true,
        datSafeOutputExts: data?.dat_safe_output_exts ?? [],
        sizeRatios: data?.size_ratios ?? {},
        pendingRepins: data?.pending_repins ?? 0,
        error: data?.error ?? null,
      };
    } catch (e) {
      this.statusError = e?.message ?? 'Failed to get RomM status';
      this.status = null;
    } finally {
      this.statusLoading = false;
    }
  }

  async loadSettings() {
    try {
      this.settings = await api.getRommSettings();
    } catch (e) {
      this.statusError = e?.message ?? 'Failed to load RomM settings';
    }
    return this.settings;
  }

  async saveSettings(patch) {
    this.settingsSaving = true;
    try {
      this.settings = await api.saveRommSettings(patch);
      // Connection details may have changed; re-derive everything downstream.
      await this.loadStatus();
      if (this.usable) await this.loadPlatforms({ force: true });
      return this.settings;
    } finally {
      this.settingsSaving = false;
    }
  }

  async testConnection(patch = {}) {
    this.testing = true;
    this.testResult = null;
    try {
      this.testResult = await api.testRommConnection(patch);
      return this.testResult;
    } finally {
      this.testing = false;
    }
  }

  // ─── platforms ────────────────────────────────────────────────────────

  async loadPlatforms({ force = false } = {}) {
    if (this.platformsLoading) return;
    this.platformsLoading = true;
    this.platformsError = null;
    try {
      const data = await api.getRommPlatforms();
      this.platforms = Array.isArray(data) ? data : [];
      // Re-enter whichever platform is selected, not only on first load: the
      // view clears the catalog on exit, so returning to it with a platform
      // still selected must re-fetch rather than show an empty list.
      const stillThere = this.platforms.some((p) => p.id === this.selectedPlatformId);
      if (this.platforms.length > 0 && (force || !stillThere || !fileBrowser.rommPlatformId)) {
        const target = stillThere ? this.selectedPlatformId : this.platforms[0].id;
        await this.selectPlatform(target);
      }
    } catch (e) {
      this.platformsError = e?.message ?? 'Failed to list RomM platforms';
      this.platforms = [];
      this.selectedPlatformId = null;
      // Leave no catalog behind. Clearing `platforms` alone left the previous
      // platform's rows on the Library tab, still selected and still wired to
      // the Convert panel — so the user could submit conversions against a
      // server or platform that is no longer the one selected, with the error
      // shown right next to them. Leaving RomM mode also restores the ordinary
      // directory listing, which is a working screen rather than a stale one.
      fileBrowser.exitRomm();
    } finally {
      this.platformsLoading = false;
    }
  }

  async selectPlatform(platformId) {
    const id = Number(platformId);
    if (!Number.isFinite(id)) return;
    this.selectedPlatformId = id;
    this.#adoptPlatformTool(id);
    await fileBrowser.enterRomm(id);
  }

  /**
   * Point the workspace at a tool this platform can actually use.
   *
   * Narrowing the row's `convertible_by` is only an annotation: the shared
   * submit path deliberately does not gate on it (chdman's extract/copy modes
   * take a `.chd` that is badged convertible by nothing). So a workspace left
   * on CHDMAN from a previous session would happily accept a GameCube `.iso`
   * and produce a CHD — exactly the disambiguation this view exists to remove.
   * Switching the active tool is the enforcement point the picker respects.
   */
  #adoptPlatformTool(platformId) {
    const allowed = this.platforms.find((p) => p.id === platformId)?.tool_ids;
    // No opinion from the backend (unknown slug, older server) changes nothing,
    // matching narrow_to_platform's conservative contract.
    if (!Array.isArray(allowed) || allowed.length === 0) return;
    if (allowed.includes(conversion.primaryTool)) return;
    const next = registry.all().find((t) => allowed.includes(t.id));
    if (next) {
      conversion.setPrimaryTool(next.id);
      ui.workspaceTool = next.id;
    }
  }

  // ─── automation rules ─────────────────────────────────────────────────

  async loadRules() {
    this.rulesLoading = true;
    this.rulesError = null;
    try {
      const data = await api.getRommRules();
      this.rules = data?.rules ?? {};
      this.ruleDefaults = data?.defaults ?? null;
      this.ruleOptions = data?.options ?? this.ruleOptions;
      this.ruleState = data?.state ?? {};
    } catch (e) {
      // Surfaced, not swallowed: without this the editor renders an empty rule
      // set that looks like "nothing is configured" when the request failed.
      this.rulesError = e?.message ?? 'Failed to load automation rules';
      throw e;
    } finally {
      this.rulesLoading = false;
    }
    return this.rules;
  }

  /** A rule for *platformId*, falling back to the backend-supplied defaults. */
  ruleFor(platformId) {
    const key = String(platformId);
    if (this.rules[key]) return { ...this.rules[key] };
    return { ...(this.ruleDefaults ?? {}) };
  }

  lastRunFor(platformId) {
    return this.ruleState[String(platformId)] ?? null;
  }

  setRule(platformId, rule) {
    this.rules = { ...this.rules, [String(platformId)]: rule };
  }

  removeRule(platformId) {
    const next = { ...this.rules };
    delete next[String(platformId)];
    this.rules = next;
  }

  async saveRules() {
    this.rulesSaving = true;
    try {
      const data = await api.saveRommRules(this.rules);
      // Take the server's normalized copy: it clamps numbers and drops rules
      // whose mode no longer exists, so the editor shows what will actually run.
      this.rules = data?.rules ?? this.rules;
      return this.rules;
    } finally {
      this.rulesSaving = false;
    }
  }

  /** How many sources this rule remembers having produced an output for. */
  convertedCountFor(platformId) {
    const entry = this.ruleState[String(platformId)] ?? {};
    if (entry.converted && typeof entry.converted === 'object') {
      return Object.keys(entry.converted).length;
    }
    // The earlier shape, kept readable so an upgraded install still shows a
    // count (and a Forget history button) for what it recorded before.
    return Array.isArray(entry.converted_ids) ? entry.converted_ids.length : 0;
  }

  /**
   * Forget the "already converted" history for one platform, or all of them.
   *
   * Overwrite and rename rules cannot tell "already done" from the
   * destination alone, so they remember what they produced. Restore a library
   * from a backup, or move outputs aside by hand, and that memory is the only
   * thing standing between the operator and a rerun.
   */
  async forgetConverted(platformIds = null) {
    const data = await api.forgetRommConverted(platformIds);
    this.ruleState = data?.state ?? this.ruleState;
    return data?.cleared ?? 0;
  }

  async previewSweep(platformIds = null) {
    this.sweeping = true;
    try {
      this.sweepResult = await api.previewRommAutoConvert(
        platformIds ? { platform_ids: platformIds } : {},
      );
      return this.sweepResult;
    } finally {
      this.sweeping = false;
    }
  }

  async runSweep(platformIds = null) {
    this.sweeping = true;
    try {
      this.sweepResult = await api.runRommAutoConvert(
        platformIds ? { platform_ids: platformIds } : {},
      );
      // A sweep that queued formats RomM cannot hash-match has just written
      // pending rows. Without this the header badge keeps its old count and
      // the Re-match action stays hidden until the page is reloaded, which
      // hides the follow-up step from exactly the run that created the need.
      this.notePendingRepins(this.sweepResult?.repins_recorded ?? 0);
      // The sweep already succeeded; a failing state refresh must not be
      // reported as a failed run. `rulesError` carries that failure instead.
      await this.loadRules().catch(() => {});
      return this.sweepResult;
    } finally {
      this.sweeping = false;
    }
  }

  // ─── re-pin queue ─────────────────────────────────────────────────────

  /** Bump the badge after a conversion recorded rows, without a full reload. */
  notePendingRepins(count) {
    if (!this.status || !count) return;
    this.status.pendingRepins = (this.status.pendingRepins ?? 0) + count;
  }

  /**
   * Settle any conversions RomM has since rescanned.
   *
   * Idempotent server-side, so it is safe to call on every view load; a row
   * RomM has not scanned yet simply stays pending for the next call.
   */
  async runRepin() {
    if (this.repinning) return null;
    this.repinning = true;
    try {
      const result = await api.runRommRepin();
      if (this.status) this.status.pendingRepins = result?.pending ?? 0;
      return result;
    } finally {
      this.repinning = false;
    }
  }
}

export const romm = new RommStore();
