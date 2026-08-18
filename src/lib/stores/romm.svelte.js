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
  // Monotonic ticket for `loadPlatforms`; see there. Not $state -- nothing
  // renders it, and it changes on every load.
  #platformsTicket = 0;
  // Bumped by every rule edit; see `saveRules`. Not $state -- nothing renders
  // it, and it changes on every keystroke in the editor.
  #rulesRevision = 0;
  // One ticket per read that a settings save re-issues. Every one of these can
  // still be waiting on the OLD instance -- a slow or unreachable URL is
  // exactly why the operator is changing it -- and a late answer overwriting
  // the new one leaves the view describing a server nobody is pointed at.
  // `loadPlatforms` has its own (`#platformsTicket`) because it is also the
  // one that can be superseded mid-flight by `cancelPlatformLoad`.
  #statusTicket = 0;
  #settingsTicket = 0;
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
  // Unsaved edits live here, next to the rules they describe, not in the
  // editor component: the automation tab unmounts every time the operator
  // looks at the library, and a component-local flag came back false while
  // `rules` still held the edits — so the save bar vanished and Preview / Run
  // now silently reported on the *server's* rules while showing the new ones.
  rulesDirty = $state(false);
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
    const ticket = ++this.#statusTicket;
    this.statusLoading = true;
    this.statusError = null;
    try {
      const data = await api.getRommStatus();
      if (ticket !== this.#statusTicket) return;
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
      if (ticket !== this.#statusTicket) return;
      this.statusError = e?.message ?? 'Failed to get RomM status';
      this.status = null;
    } finally {
      // Only the newest read clears the spinner, or a superseded one returning
      // last would report "done" while its replacement is still running.
      if (ticket === this.#statusTicket) this.statusLoading = false;
    }
  }

  async loadSettings() {
    const ticket = ++this.#settingsTicket;
    try {
      const data = await api.getRommSettings();
      if (ticket !== this.#settingsTicket) return this.settings;
      this.settings = data;
    } catch (e) {
      if (ticket !== this.#settingsTicket) return this.settings;
      this.statusError = e?.message ?? 'Failed to load RomM settings';
    }
    return this.settings;
  }

  async saveSettings(patch) {
    this.settingsSaving = true;
    try {
      const saved = await api.saveRommSettings(patch);
      // Supersede any read still waiting on the previous connection before
      // adopting the answer: one of them landing afterwards would replace the
      // settings this save just installed with the ones it replaced.
      this.#settingsTicket += 1;
      this.settings = saved;
      // Connection details may have changed; re-derive everything downstream.
      await this.loadStatus();
      if (this.usable) {
        await this.loadPlatforms({ force: true });
      } else {
        // Saving an unreachable URL or an unmounted library leaves nothing to
        // browse, and the previous instance's catalog must not outlive it:
        // returning to the Library tab would show those rows, still selected
        // and still wired to the Convert panel, against a connection that no
        // longer exists. Same cleanup the failed-reload path does.
        this.platforms = [];
        this.selectedPlatformId = null;
        fileBrowser.exitRomm();
      }
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
    // `force` supersedes rather than waits. It is what runs after the
    // connection details change, and the load already in flight is asking the
    // OLD instance: bailing here let that answer arrive afterwards and select
    // a platform belonging to a server nobody is pointed at any more.
    if (this.platformsLoading && !force) return;
    // Every load takes a ticket, and only the newest one is allowed to write.
    // Two loads can be in flight at once now, and the slower is not
    // necessarily the older.
    const ticket = ++this.#platformsTicket;
    this.platformsLoading = true;
    this.platformsError = null;
    try {
      const data = await api.getRommPlatforms();
      if (ticket !== this.#platformsTicket) return;
      this.platforms = Array.isArray(data) ? data : [];
      // Re-enter whichever platform is selected, not only on first load: the
      // view clears the catalog on exit, so returning to it with a platform
      // still selected must re-fetch rather than show an empty list.
      const stillThere = this.platforms.some((p) => p.id === this.selectedPlatformId);
      if (this.platforms.length === 0) {
        // A successful call that lists nothing is still a change of world —
        // an instance with no platforms, or one whose library was emptied.
        // Leaving the selection and the previous instance's rows on screen
        // showed "No platforms in RomM" above a live catalog, and with no
        // selected platform to narrow it the target picker fell back to every
        // mode in the registry, wired to those stale paths.
        this.selectedPlatformId = null;
        fileBrowser.exitRomm();
      } else if (force || !stillThere || !fileBrowser.rommPlatformId) {
        const target = stillThere ? this.selectedPlatformId : this.platforms[0].id;
        await this.selectPlatform(target);
      }
    } catch (e) {
      if (ticket !== this.#platformsTicket) return;
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
      // Only the newest load clears the flag: a superseded one returning last
      // would otherwise report "done" while its replacement is still running.
      if (ticket === this.#platformsTicket) this.platformsLoading = false;
    }
  }

  /**
   * Abandon whatever platform load is in flight, writing nothing.
   *
   * For leaving the RomM view. The load's tail selects a platform, which
   * calls `fileBrowser.enterRomm()` — so a request still outstanding when the
   * view unmounts would repopulate the browser with catalog rows *after* the
   * cleanup put it back on the ordinary listing, and the workspace would open
   * showing a directory heading above RomM entries. The request itself cannot
   * be recalled; superseding its ticket is what makes its answer inert.
   */
  cancelPlatformLoad() {
    this.#platformsTicket += 1;
    // The abandoned load's `finally` will not clear this — it no longer holds
    // the newest ticket — and leaving it set would make every later unforced
    // load bail as "one is already running".
    this.platformsLoading = false;
  }

  /**
   * True when the selected platform is one nothing installed can convert.
   *
   * `tool_ids`/`mode_ids` absent means "no opinion" (unknown slug, older
   * server) and everything stays offered; an empty array is a decision. The
   * distinction matters because the convert panel has no valid tool to fall
   * back to in the second case, so the view hides it instead.
   */
  get selectedPlatformUnsupported() {
    const platform = this.platforms.find((p) => p.id === this.selectedPlatformId);
    if (!platform) return false;
    return (
      (Array.isArray(platform.tool_ids) && platform.tool_ids.length === 0)
      || (Array.isArray(platform.mode_ids) && platform.mode_ids.length === 0)
    );
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
    const platform = this.platforms.find((p) => p.id === platformId);
    const allowed = platform?.tool_ids;
    const allowedModes = platform?.mode_ids;
    // No opinion from the backend (unknown slug, older server) changes nothing,
    // matching narrow_to_platform's conservative contract.
    if (!Array.isArray(allowed)) return;
    // An explicitly empty list is the opposite of no opinion: the backend
    // knows this platform and no *installed* tool serves it. There is nothing
    // to adopt, so the workspace keeps whatever it had — which is only safe
    // because the view refuses to render the convert panel for such a
    // platform (`selectedPlatformUnsupported`). Treating [] as "no opinion"
    // left the panel wired to the previous tool, and a PS2 ISO could be
    // submitted to Dolphin RVZ from a platform Dolphin does not serve.
    if (allowed.length === 0) return;

    // The tool being allowed is not enough. Two platforms can both allow the
    // chain tool while allowing *different* chain modes, so moving from
    // GameCube to PS2 would keep `nkit_to_rvz` selected: the picker stops
    // offering it, but the Convert panel would still submit it, and a catalog
    // row with a matching extension would be converted to the other console's
    // format. The mode has to be re-checked, not just its tool.
    const modeAllowed = !Array.isArray(allowedModes)
      || allowedModes.includes(conversion.mode);
    if (allowed.includes(conversion.primaryTool) && modeAllowed) return;

    if (!allowed.includes(conversion.primaryTool)) {
      const next = registry.all().find((t) => allowed.includes(t.id));
      if (next) {
        conversion.setPrimaryTool(next.id);
        ui.workspaceTool = next.id;
      }
    }
    // setPrimaryTool resets to the tool's default mode, which may itself be
    // one this platform disallows; and when the tool did not change, nothing
    // has moved off the stale mode yet. Either way, land on an allowed one.
    if (Array.isArray(allowedModes) && !allowedModes.includes(conversion.mode)) {
      const tool = registry.forTool(conversion.primaryTool);
      const fallback = (tool?.modes ?? []).find((m) => allowedModes.includes(m.mode));
      if (fallback) conversion.setMode(fallback.mode);
    }
  }

  // ─── automation rules ─────────────────────────────────────────────────

  async loadRules() {
    this.rulesLoading = true;
    this.rulesError = null;
    try {
      const data = await api.getRommRules();
      // Server truth, unless the editor is holding edits nobody has saved: a
      // reload can be triggered by something else entirely (a settings save on
      // the next tab, a sweep finishing), and overwriting there would discard
      // what the operator typed with no way back.
      if (!this.rulesDirty) this.rules = data?.rules ?? {};
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
    this.rulesDirty = true;
    this.#rulesRevision += 1;
  }

  removeRule(platformId) {
    const next = { ...this.rules };
    delete next[String(platformId)];
    this.rules = next;
    this.rulesDirty = true;
    this.#rulesRevision += 1;
  }

  async saveRules() {
    this.rulesSaving = true;
    // The editor stays live while this is in flight, so anything typed after
    // this point is NOT in what we just submitted.
    const revision = this.#rulesRevision;
    try {
      const data = await api.saveRommRules(this.rules);
      if (revision !== this.#rulesRevision) {
        // Edited mid-save. Adopting the server's copy would replace those
        // edits with the snapshot that was submitted before them — silently,
        // and then clear the dirty flag so the Save bar stopped asking. The
        // newer edits stay, still dirty, for the next save to normalize.
        return this.rules;
      }
      // Take the server's normalized copy: it clamps numbers and drops rules
      // whose mode no longer exists, so the editor shows what will actually run.
      this.rules = data?.rules ?? this.rules;
      this.rulesDirty = false;
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
      this.setPendingRepins(this.sweepResult?.pending_repins);
      // The sweep already succeeded; a failing state refresh must not be
      // reported as a failed run. `rulesError` carries that failure instead.
      await this.loadRules().catch(() => {});
      return this.sweepResult;
    } finally {
      this.sweeping = false;
    }
  }

  // ─── re-pin queue ─────────────────────────────────────────────────────

  /**
   * Set the badge from the count the backend just measured.
   *
   * Assigned, never added to: `record()` *supersedes* the pending row for a
   * destination rather than stacking one, so re-recording after a failed or
   * redirected conversion leaves the total unchanged. Adding each newly
   * recorded row inflated the badge on every retry, and it stayed wrong until
   * a status reload or a settle pass. Both the plan response and the sweep
   * result carry the authoritative figure, which costs them one COUNT.
   */
  setPendingRepins(pending) {
    if (!this.status || typeof pending !== 'number') return;
    this.status.pendingRepins = Math.max(0, pending);
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
