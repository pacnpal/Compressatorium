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
    } finally {
      this.platformsLoading = false;
    }
  }

  async selectPlatform(platformId) {
    const id = Number(platformId);
    if (!Number.isFinite(id)) return;
    this.selectedPlatformId = id;
    await fileBrowser.enterRomm(id);
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
      await this.loadRules();
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
