// DAT matching store, wraps /api/dat endpoints. Caches per-path match
// results so FileList rows can render badges without per-row fetches.

import { untrack } from 'svelte';
import { SvelteMap } from 'svelte/reactivity';
import { api } from '$lib/api/endpoints.js';

// How long a match attempt suppresses a retry for the same path. Must exceed
// the backend's Hasheous cooldown (60s) so the retry lands after the service
// has had a chance to recover, not during the outage.
const ATTEMPT_RETRY_MS = 90_000;

// How many times a hydration cycle will re-check itself on a timer before
// giving up until something else (navigation, a finished job, a DAT change)
// triggers it. Expiring the guard is not enough on its own: nothing reactive
// changes when the window elapses, so a static page would sit there unmatched.
// Bounded because a permanently-skipped file (over MATCH_MAX_FILE_SIZE) would
// otherwise re-spawn a no-op job every interval for as long as the tab is open.
const MAX_AUTO_RETRIES = 2;

// How often the workspace re-asks which sources can answer a hash lookup, so
// a provider toggled in another tab (or through the API) reaches this one.
// Both directions matter, so this runs for as long as the workspace is
// mounted: one small /dat/stats a minute, against a change that otherwise
// needs a reload to notice.
const AVAILABILITY_POLL_MS = 60_000;

class DATMatchingStore {
  matches = new SvelteMap();
  matchingAvailable = $state(false);
  panelOpen = $state(false);
  importingDat = $state(false);
  syncing = $state(false);
  syncStatus = $state(null);
  dats = $state([]);
  datsLoading = $state(false);
  datsError = $state(null);
  stats = $state(null);

  // Paths we've already kicked a match-job for, mapped to WHEN. The backend
  // may complete a job without caching a result, which would otherwise make
  // hydrate() see the same path as uncached forever and re-spawn jobs on every
  // hydration cycle. Plain object map (not Set/Map) for the membership lookup
  // so the svelte/prefer-svelte-reactivity rule doesn't flag it as a candidate
  // for SvelteSet; this guard is purely internal and reloading the page resets
  // it.
  //
  // The timestamps matter: an uncached result means either a permanent skip
  // (over MATCH_MAX_FILE_SIZE) or a *transient* failure (a Hasheous timeout,
  // which is deliberately non-cacheable). The client can't tell them apart, so
  // a permanent guard would strand the transient ones until a page reload —
  // exactly the files the retry is for. Entries expire instead, which retries
  // the transient ones and costs a permanently-skipped file one cheap no-op
  // job per interval (it re-checks the size cap without hashing).
  _attemptedPaths = Object.create(null);
  _retryTimer = null;
  _retryPaths = null;
  // Runs only while nothing can answer a lookup (see watchMatchingAvailability).
  _availabilityTimer = null;
  _autoRetries = 0;
  // The path set the last match job was started for, so a re-run of the same
  // visible page doesn't count as a new hydration and refill the retry budget.
  _lastJobPaths = null;
  // Bumped whenever the provider state changes or a /dat/stats request starts.
  // One counter, not two: both the availability refresh and the cache
  // hydration have to discard responses that were in flight across a change,
  // and giving them separate counters was how the second case got missed.
  _generation = 0;
  // Hydration ordering. _hydrateSeq numbers each request as it goes out;
  // _appliedSeq records the newest one that has actually written to the map.
  // Two counters, not one: discarding on "a newer request exists" would throw
  // away an in-order answer whenever two hydrations overlap, while discarding
  // on "a newer answer already applied" only drops the responses that are
  // genuinely stale.
  _hydrateSeq = 0;
  _appliedSeq = 0;

  matchFor(path) {
    return this.matches.get(path) ?? null;
  }

  /**
   * Keep re-checking availability for as long as nothing can answer a lookup.
   *
   * `refreshMatchingAvailability()` already knows how to absorb a provider
   * flip made elsewhere -- another tab, an API client -- but on the workspace
   * nothing was driving it after mount. A DAT-less install whose operator
   * enabled Hasheous in a second tab sat at `matchingAvailable === false`, so
   * every hydration returned before starting a job, until this tab happened to
   * visit the DAT view or was reloaded.
   *
   * Runs for as long as the workspace is mounted, in both directions. An
   * earlier version stopped itself once something could answer a lookup, on
   * the reasoning that `matchingAvailable === false` is the only state a stale
   * reading changes behaviour in. That was wrong: a provider *disabled*
   * elsewhere makes this tab merge the backend's now-unmatched rows into
   * `matches`, and stopping meant a later re-enable was never noticed -- those
   * paths count as known and are never matched again. One transition healed,
   * the other made permanent.
   *
   * A field plus methods rather than a closure returning its own teardown --
   * the same shape `_retryTimer` already uses, so both of this store's timers
   * are torn down the same way.
   */
  watchMatchingAvailability(intervalMs = AVAILABILITY_POLL_MS) {
    this.stopWatchingAvailability();
    this._availabilityTimer = setInterval(() => this._recheckAvailability(), intervalMs);
    this._recheckAvailability();
  }

  /** Stop the availability watch. Idempotent; the workspace calls it on unmount. */
  stopWatchingAvailability() {
    if (this._availabilityTimer) clearInterval(this._availabilityTimer);
    this._availabilityTimer = null;
  }

  async _recheckAvailability() {
    // Swallowed: /dat/stats failing is not this poll's problem to report, and
    // refreshMatchingAvailability() has already recorded what it could.
    await this.refreshMatchingAvailability().catch(() => {});
  }

  async refreshMatchingAvailability() {
    // App.svelte and DATView both refresh on mount, so one of those can be
    // in flight when a toggle PUT lands. Its older /dat/stats answer would
    // otherwise arrive last and overwrite the authoritative state the PUT
    // just applied -- the panel snaps back to "off" and matchingAvailable
    // stays false, so browsing schedules nothing until some later refresh.
    const generation = ++this._generation;
    try {
      const stats = await api.getDATStats();
      if (generation !== this._generation) return this.matchingAvailable;
      const wasEnabled = this.stats?.hasheous_enabled;
      this.stats = stats;
      // Another tab (or an API client) can flip the provider under us. The
      // backend then withholds cached misses that predate the stronger
      // source, but hydrate() only adds rows and never removes them, so those
      // paths would sit in `matches` as known-unmatched and never be
      // scheduled. Same reset setHasheousEnabled() does for a local toggle.
      if (wasEnabled !== undefined && wasEnabled !== stats?.hasheous_enabled) {
        // Bump first: a hydrate already in flight answered under the old
        // policy and must not repopulate what this clear is dropping.
        this._generation += 1;
        this.matches.clear();
        this._resetAttempts();
      }
      // /api/dat/stats returns total_dats (legacy UI checked this exact field);
      // `total` / `imported_count` are not part of the response.
      //
      // This gate is "can anything answer a hash lookup?", NOT "are there
      // DATs" -- with Hasheous enabled a library with zero imported DATs still
      // matches, and gating on total_dats alone would mean ordinary browsing
      // never kicks a match job in exactly that case.
      this.matchingAvailable =
        (stats?.total_dats ?? 0) > 0 || Boolean(stats?.hasheous_enabled);
      return this.matchingAvailable;
    } catch (_e) {
      if (generation !== this._generation) return this.matchingAvailable;
      this.matchingAvailable = false;
      return false;
    }
  }

  /**
   * Flip the Hasheous fallback and re-read availability.
   *
   * Enabling it can turn previously-unmatched files into matches, so the
   * cached badges and the session's "already attempted" set are both dropped
   * — the backend re-checks those rows against the newly available source
   * (see cached_result_usable), but only if the client asks again.
   */
  async setHasheousEnabled(enabled) {
    const state = await api.setHasheousEnabled(enabled);
    // Apply the authoritative response NOW, before the counters refresh.
    // That refresh swallows its own errors, so if /dat/stats happened to fail
    // right after a successful PUT we would report success while the panel
    // still showed the old state and matchingAvailable stayed stale -- with
    // the backend already switched over.
    this._applyHasheousState(state);
    // Bump first: a hydrate already in flight answered under the old
    // policy and must not repopulate what this clear is dropping.
    this._generation += 1;
    this.matches.clear();
    this._resetAttempts();
    await this.refreshMatchingAvailability();
    // ...and re-apply it afterwards. refreshMatchingAvailability() swallows a
    // failed /dat/stats and falls back to `matchingAvailable = false`, which
    // would bury the state the PUT just established -- reporting success while
    // the panel shows off and browsing stays gated, with the backend already
    // switched over. The merge is idempotent, so this is a no-op when the
    // refresh succeeded.
    this._applyHasheousState(state);
    return state;
  }

  /** Merge a /dat/hasheous response into the cached stats. */
  _applyHasheousState(state) {
    if (!state) return;
    this.stats = {
      ...(this.stats ?? {}),
      hasheous_enabled: state.enabled,
      hasheous_url: state.url,
      hasheous_overridden: state.overridden,
      hasheous_env_default: state.env_default,
    };
    this.matchingAvailable =
      (this.stats.total_dats ?? 0) > 0 || Boolean(state.enabled);
  }

  async testHasheous() {
    return api.testHasheous();
  }

  async loadDATs() {
    this.datsLoading = true;
    this.datsError = null;
    try {
      // /api/dat/list returns a bare array of DAT records
      // (app/services/dat_store.py:list_dats), not an envelope. Each
      // record: { id, name, description, version, imported_at, file_count }.
      const data = await api.listDATs();
      this.dats = Array.isArray(data) ? data : (Array.isArray(data?.dats) ? data.dats : []);
      await this.refreshMatchingAvailability();
    } catch (e) {
      this.datsError = e?.message ?? 'Failed to load DATs';
    } finally {
      this.datsLoading = false;
    }
  }

  /** Fetch cached matches (never hashes) for a batch of paths. */
  async hydrate(paths) {
    if (!paths?.length) return;
    // A lookup that started before a provider change answers with the OLD
    // policy. Landing after matches.clear() it would put a stale local-only
    // miss back in the map, and hydrateAndMatch() treats any key in the map as
    // known -- so with local DATs present (matchingAvailable never flips, the
    // effect need not re-run) the newly enabled fallback stayed dead until a
    // reload.
    const generation = this._generation;
    // Responses can land out of order, and the reconcile below made that
    // observable: hydration A reads a hit, a rescan deletes it, hydration B
    // correctly evicts it -- and then A's delayed answer put the obsolete hit
    // back, indefinitely, because returned rows were merged unconditionally.
    // Only apply an answer if nothing newer has already applied.
    const seq = ++this._hydrateSeq;
    // Snapshot the entries this hydration can invalidate. Overlaying returned
    // rows is not enough: the server omits a path it no longer has a usable
    // row for -- one dropped because a rescan proved the file changed, or one
    // withheld by cached_result_usable() because it predates a now-enabled
    // lookup source -- and an omitted path used to keep its old entry
    // indefinitely. That is worse than a stale badge, because
    // hydrateAndMatch() treats any key in the map as known: a replaced file
    // was never re-matched and went on showing the previous file's game until
    // a reload. Every other invalidation site (import, delete, sync, provider
    // toggle) clears the whole map; this reconciles the per-path case.
    // Plain array of pairs, not a Map: this is a throwaway local snapshot with
    // no lookups, and svelte/prefer-svelte-reactivity flags built-in Maps (the
    // same reason _attemptedPaths is a plain object).
    //
    // untrack: this reads the reactive map, and hydrate() is entered
    // synchronously from the file list's $effect -- so without it the effect
    // subscribes to exactly the entries this function is about to overwrite.
    // The response installs freshly deserialized objects (new identities even
    // when logically identical), which invalidates that subscription, re-runs
    // the effect, and hydrates again: a folder holding any cached match
    // requested in a loop for as long as it stayed on screen. The effect
    // already tracks what it means to track -- `datMatchTerminalCount` and
    // `matchingAvailable` -- and must not also track the cache it is filling.
    const before = untrack(() => {
      const snapshot = [];
      for (const path of paths) {
        if (this.matches.has(path)) snapshot.push([path, this.matches.get(path)]);
      }
      return snapshot;
    });
    try {
      const data = await api.getMatchCache(paths);
      if (generation !== this._generation) return;
      if (seq < this._appliedSeq) return;
      this._appliedSeq = seq;
      const results = data?.results ?? {};
      // Compare-and-delete against the snapshotted value, not just the key: a
      // match job can land between the request and this response, and its
      // result is newer than the answer we are holding. Dropping it would
      // erase a fresh badge and re-queue a file the backend had just done.
      for (const [path, previous] of before) {
        if (!Object.hasOwn(results, path) && this.matches.get(path) === previous) {
          this.matches.delete(path);
        }
      }
      for (const [path, result] of Object.entries(results)) {
        this.matches.set(path, result);
      }
    } catch (_e) {
      // non-fatal
    }
  }

  /**
   * Fetch cached matches AND kick a background match job for any
   * paths the cache lookup didn't return. /api/dat/matches/lookup is
   * read-only (it never hashes), so files that have never been
   * matched by any session would otherwise never get a DAT badge.
   * The startMatchJob handler de-dupes against the existing job queue
   * server-side, so re-firing for the same path during a page
   * navigation is cheap. Skipped when no DATs are imported, the
   * match job would just no-op.
   */
  async hydrateAndMatch(paths, { fromRetry = false } = {}) {
    if (!paths?.length) return;
    // Read BEFORE the first await. $effect tracks only synchronous reads, and
    // this runs inside the file list's effect -- reading matchingAvailable
    // after `await this.hydrate()` left the effect unsubscribed from it, so
    // the false->true flip when /dat/stats resolves never re-ran it. On a
    // fresh Hasheous-only install that meant nothing matched until the user
    // navigated or reloaded. (The gate itself has to stay below the hydrate:
    // cached hits must load even when matching is unavailable.)
    const canMatch = this.matchingAvailable;
    // A pending retry belongs to the set it was armed with. _scheduleRetry()
    // retargets when it runs again, but the branch that starts a job never
    // reaches it -- so navigating from a folder whose lookups failed to one
    // with work to do left the old timer running, and it later hydrated (and
    // could start a remote match job for) files nobody is looking at any more.
    // Retiring it here covers every entry, not just the one that re-arms.
    if (!fromRetry) this._cancelRetryUnless(paths);
    await this.hydrate(paths);
    if (!canMatch) return;
    // Drop paths the backend attempted recently: if they came back uncached
    // after a completed dat_match job they were skipped (over
    // MATCH_MAX_FILE_SIZE, unreadable) or failed transiently, and re-spawning
    // immediately would loop. The guard expires so a transient failure — a
    // Hasheous outage is non-cacheable by design, and its cooldown is 60s —
    // gets retried once the service recovers, without a page reload.
    const now = Date.now();
    const stillUnknown = paths.filter((p) => !this.matches.has(p));
    const uncached = stillUnknown.filter((p) => !this._recentlyAttempted(p, now));
    if (uncached.length === 0) {
      // Everything left is inside its retry window. Come back when it expires,
      // otherwise a transient failure (a Hasheous outage) stays on screen as
      // "no badge" until the user navigates or reloads.
      if (stillUnknown.length) this._scheduleRetry(paths);
      return;
    }
    // Only a genuinely NEW path set refills the budget. `!fromRetry` alone was
    // not enough: the file list re-runs this on every dat_match completion with
    // the same visible paths, so a job outliving the 90s attempt window let the
    // terminal-job pass reset the budget and start another job, and each of
    // those completions did it again -- MAX_AUTO_RETRIES capped nothing. Keying
    // on the path set means navigating somewhere new still gets a full budget.
    if (!fromRetry && !this._samePaths(uncached, this._lastJobPaths)) {
      this._autoRetries = 0;
    } else if (!fromRetry) {
      // The same visible set again, driven by a terminal job event rather than
      // by the retry timer. `_autoRetries` is only incremented inside
      // _scheduleRetry(), so a job that outlives ATTEMPT_RETRY_MS reaches here
      // with the attempt guard already expired, skips the timer entirely, and
      // starts another full job -- for as long as the tab stays open. It is a
      // retry in everything but name, so it consumes the budget like one.
      if (this._autoRetries >= MAX_AUTO_RETRIES) return;
      this._autoRetries += 1;
    }
    this._lastJobPaths = uncached;
    const generation = this._generation;
    try {
      await this.startMatchJob(uncached);
      // Mark attempts only AFTER the backend accepted the job. A 409
      // (another match job already active) would otherwise strand the
      // paths permanently, and any other failure should also leave
      // them eligible for the next hydration to retry.
      //
      // ...and only while the verdict still belongs to the current policy.
      // startMatchJob() drops a stale response's results, but returning
      // normally is not the same as succeeding: every bump of _generation is
      // paired with a _resetAttempts() whose whole purpose is to make these
      // paths eligible again, and stamping them here wrote the suppression
      // straight back into the freshly cleared map -- for the full 90s
      // window, with no terminal job event coming to shake it loose.
      if (generation !== this._generation) return;
      for (const p of uncached) this._attemptedPaths[p] = now;
    } catch (_e) {
      // non-fatal, the file list still renders without badges. The
      // next hydration cycle will try these paths again once any
      // currently-active dat_match job has cleared.
    }
  }

  /**
   * Drop the session-scoped "already attempted" set. Called whenever
   * the DAT library state changes (import, delete, MAMERedump sync
   * finish) so files that were previously uncached against the old
   * DAT set get re-considered against the new one.
   */
  _resetAttempts() {
    this._attemptedPaths = Object.create(null);
    this._autoRetries = 0;
    this._lastJobPaths = null;
    if (this._retryTimer) {
      clearTimeout(this._retryTimer);
      this._retryTimer = null;
      this._retryPaths = null;
    }
  }

  /**
   * Re-run hydration once the attempt window has expired.
   *
   * Only while a remote source is in play: with Hasheous off, a path still
   * uncached after a completed job is a permanent skip (size cap), not a
   * transient failure, so retrying it would never produce anything.
   */
  _scheduleRetry(paths) {
    if (!this.stats?.hasheous_enabled) return;

    // A pending timer holds the paths it was scheduled with. If the user has
    // navigated since, those are off-screen now: retarget rather than letting
    // the stale set win, or the retry rehydrates a folder nobody is looking at
    // and the visible failures never get revisited.
    if (this._retryTimer) {
      if (this._samePaths(paths, this._retryPaths)) return;
      clearTimeout(this._retryTimer);
      this._retryTimer = null;
    }
    if (this._autoRetries >= MAX_AUTO_RETRIES) return;

    this._autoRetries += 1;
    this._retryPaths = paths;
    this._retryTimer = setTimeout(() => {
      this._retryTimer = null;
      this._retryPaths = null;
      this.hydrateAndMatch(paths, { fromRetry: true }).catch(() => {});
    }, ATTEMPT_RETRY_MS);
  }

  /**
   * Retire a pending retry that was armed for some other set of paths.
   *
   * `paths` is the set now on screen; a timer holding exactly that set is
   * still wanted and survives. Anything else is stale.
   */
  _cancelRetryUnless(paths) {
    if (!this._retryTimer) return;
    if (this._samePaths(paths, this._retryPaths)) return;
    clearTimeout(this._retryTimer);
    this._retryTimer = null;
    this._retryPaths = null;
    // The budget belongs to the set that just went away, not to this one.
    this._autoRetries = 0;
  }

  /**
   * Drop any pending retry outright, and hand the next visit a clean slate.
   * Called when the file list goes away: nothing is visible, so a timer that
   * fires would hydrate and potentially hash for a view that no longer exists.
   *
   * The budget goes with it, and unconditionally -- an early return on "no
   * timer" would skip exactly the case that needs it. A folder whose budget is
   * spent has no timer left, so leaving and coming back to it would find
   * `_lastJobPaths` still holding that same set and `_autoRetries` still at
   * the cap, and refuse to match. Navigating away and back is a deliberate act
   * by the operator; it refills the budget for the same reason navigating
   * somewhere new does.
   */
  cancelRetry() {
    if (this._retryTimer) clearTimeout(this._retryTimer);
    this._retryTimer = null;
    this._retryPaths = null;
    this._autoRetries = 0;
    this._lastJobPaths = null;
  }

  _samePaths(a, b) {
    if (!a || !b || a.length !== b.length) return false;
    return a.every((p, i) => p === b[i]);
  }

  /**
   * True while `path`'s last match attempt is still recent enough to skip.
   * Comfortably longer than the backend's 60s Hasheous cooldown, so a retry
   * lands after the service has had a chance to recover rather than during
   * the outage it is waiting out.
   */
  _recentlyAttempted(path, now = Date.now()) {
    const at = this._attemptedPaths[path];
    return at !== undefined && now - at < ATTEMPT_RETRY_MS;
  }

  async matchBatch(paths) {
    if (!paths?.length) return null;
    try {
      const data = await api.matchBatch(paths);
      const results = data?.results ?? {};
      for (const [path, result] of Object.entries(results)) {
        this.matches.set(path, result);
      }
      return data;
    } catch (_e) {
      return null;
    }
  }

  async startMatchJob(paths) {
    if (!paths?.length) return null;
    // Same guard as hydrate(): a provider toggle clears the map and bumps the
    // generation, and the idle response below carries verdicts reached under
    // the OLD policy. Merging them unconditionally put a local-only miss back
    // after the clear -- and because that path then counts as known and the
    // idle response fires no terminal job event, Hasheous was never tried for
    // it until some later cache reset.
    const generation = this._generation;
    const response = await api.startMatchJob(paths);
    if (generation !== this._generation) return response;
    // The endpoint answers {status: "idle", results} and creates NO job when
    // every path was cached between our hydrate() and this call (another
    // client, or a scan, got there first). Dropping that response left the
    // badges missing with no terminal job event to trigger another hydration,
    // so a static page stayed blank until the user navigated.
    const results = response?.results;
    if (results) {
      for (const [path, match] of Object.entries(results)) {
        if (match) this.matches.set(path, match);
      }
    }
    return response;
  }

  async deleteDAT(datId) {
    const result = await api.deleteDAT(datId);
    // The backend cascades DATMatch rows for the deleted DAT, so any
    // cached match entry that survives in the client map is now
    // stale, files that were only matched by this DAT would keep
    // showing the badge until next reload because hydrate() only adds
    // returned rows, never removes absent ones. Drop the whole cache
    // AND the session-scoped attempt set so the next FileList hydration
    // re-runs against the now-smaller DAT library and re-establishes
    // truth.
    // Bump first: a hydrate already in flight answered under the old
    // policy and must not repopulate what this clear is dropping.
    this._generation += 1;
    this.matches.clear();
    this._resetAttempts();
    await this.loadDATs();
    return result;
  }

  async importDAT(file) {
    this.importingDat = true;
    try {
      const result = await api.importDAT(file);
      // The backend's _import_dat_sync wipes the DATMatch cache because
      // newly-imported hashes may flip match results for files the
      // user already has. Mirror that on the client so stale badges
      // don't survive until the next visit, hydrate() only adds rows,
      // it never removes absent ones. Reset attempted paths too so
      // files previously deemed "uncached" against the old DAT get
      // re-considered against the new one.
      // Bump first: a hydrate already in flight answered under the old
      // policy and must not repopulate what this clear is dropping.
      this._generation += 1;
      this.matches.clear();
      this._resetAttempts();
      await this.loadDATs();
      return result;
    } finally {
      this.importingDat = false;
    }
  }

  async syncMAMERedump(tag = null) {
    // /api/dat/sync only schedules a background task and returns
    // immediately. Do NOT clear `syncing` in a finally, the sync is
    // still running on the backend. The store stays in the syncing
    // state until pollSyncStatus() observes the backend reporting
    // syncing=false, or cancelSync() is called explicitly.
    this.syncing = true;
    try {
      return await api.syncMAMERedump(tag);
    } catch (e) {
      // 409 means a MAMERedump sync is already running on the
      // backend. The legacy UI treated that as "good, keep polling
      // and observe progress"; clearing `syncing` here would freeze
      // any consumer poll/progress UI even though the backend is
      // actively working. Stay in the syncing state.
      if (e?.status === 409) return null;
      // Any other error means the start request itself failed,
      // there is no background work to wait for, so clear the flag
      // and re-raise.
      this.syncing = false;
      throw e;
    }
  }

  async pollSyncStatus() {
    const wasSyncing = this.syncing;
    try {
      this.syncStatus = await api.getSyncStatus();
      const stillSyncing = !!this.syncStatus?.syncing;
      this.syncing = stillSyncing;
      if (!stillSyncing && wasSyncing) {
        // Sync just finished (syncing → done transition). The backend
        // has persisted the new DAT set and may have dropped the old
        // one, so reload the full list, not just availability, and clear
        // the stale match cache (new hashes can flip prior matches).
        // Reset attempts so previously-uncached paths get tried
        // against the new DAT set. loadDATs() refreshes availability
        // internally.
        // Bump first: a hydrate already in flight answered under the old
        // policy and must not repopulate what this clear is dropping.
        this._generation += 1;
        this.matches.clear();
        this._resetAttempts();
        await this.loadDATs();
      } else if (!stillSyncing) {
        // Cold poll (e.g. on mount) with no sync running: cheap
        // An availability refresh is enough; no need to reload the whole list.
        await this.refreshMatchingAvailability();
      }
      return this.syncStatus;
    } catch (_e) {
      // Transient poll failures (brief backend restart, network blip)
      // should not tear down the polling loop while we believe a sync
      // is active. The legacy self-scheduling poller stayed armed
      // across single failures; mirror that by leaving `syncing` set
      // so the next poll attempt still fires. Only the cold-start
      // case where we never observed a sync (wasSyncing=false) flips
      // the flag off here.
      this.syncStatus = null;
      if (!wasSyncing) this.syncing = false;
      return null;
    }
  }

  async cancelSync() {
    // Don't swallow failures. /api/dat/sync/cancel returns 409 with
    // "No sync in progress" when there's nothing to cancel; swallowing
    // that would make DATView.handleCancelSync() always take the
    // success path and tell the user cancellation was requested when
    // it wasn't. Let the caller decide the UX.
    const res = await api.cancelSync();
    this.syncing = false;
    return res;
  }
}

export const datMatching = new DATMatchingStore();
