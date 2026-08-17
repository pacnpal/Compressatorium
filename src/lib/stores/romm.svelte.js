// RomM catalog overlay: connection status, platform list, and the metadata
// re-pin queue.
//
// Deliberately thin. The ROM rows themselves live in `fileBrowser` (RomM is a
// third listing source there, alongside search and archive views) because the
// backend returns the same DirectoryListing/FileEntry shape a directory does —
// so FileList, FileRow and ConvertPanel render the catalog unchanged. This
// store owns only what is genuinely RomM-specific.

import { api } from '$lib/api/endpoints.js';
import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';

class RommStore {
  // Connection / configuration
  status = $state(null);
  statusLoading = $state(false);
  statusError = $state(null);

  // Platforms
  platforms = $state([]);
  platformsLoading = $state(false);
  platformsError = $state(null);
  selectedPlatformId = $state(null);

  // Re-pin queue
  repinning = $state(false);

  get configured() {
    return this.status?.configured === true;
  }

  get connected() {
    return this.status?.connected === true;
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

  /** True when *mode* produces a format RomM cannot hash-match on its own. */
  losesDatMatch(modeEntry) {
    const ext = modeEntry?.outputExt;
    // Unknown output extension (input-ext-mapped modes) → don't cry wolf.
    if (!ext || this.datSafeOutputExts.length === 0) return false;
    return !this.datSafeOutputExts.includes(ext.toLowerCase());
  }

  get selectedPlatform() {
    return this.platforms.find((p) => p.id === this.selectedPlatformId) ?? null;
  }

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

  async loadPlatforms() {
    this.platformsLoading = true;
    this.platformsError = null;
    try {
      const data = await api.getRommPlatforms();
      this.platforms = Array.isArray(data) ? data : [];
      // Auto-select so the view lands on content rather than an empty picker.
      if (this.selectedPlatformId === null && this.platforms.length > 0) {
        await this.selectPlatform(this.platforms[0].id);
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

  /**
   * Settle any conversions RomM has since rescanned.
   *
   * Idempotent server-side, so it is safe to call on every view mount; a row
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
