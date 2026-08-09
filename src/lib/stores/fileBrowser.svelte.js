// File browser store, volumes, current path, listings, selection, sort,
// pagination, archive nav. No SSE; manual + on-demand refresh only.

import { SvelteMap } from 'svelte/reactivity';
import { api } from '$lib/api/endpoints.js';
import { jobs } from './jobs.svelte.js';
import { conversion } from './conversion.svelte.js';

const DEFAULT_PAGE_SIZE = 50;
const SORT_FIELDS = new Set(['name', 'size', 'extension', 'type']);

class FileBrowserStore {
  // Volumes
  volumes = $state([]);
  volumesLoading = $state(false);
  volumesError = $state(null);
  selectedVolume = $state(null);

  // Navigation
  currentPath = $state(null);
  currentArchivePath = $state(null);

  // Entries
  entries = $state([]);
  entriesError = $state(null);
  loading = $state(false);
  // Path of the directory listing currently in flight, used to collapse a
  // duplicate refresh of the same directory (non-reactive control state).
  _inflightListingPath = null;
  // Monotonic per-request token. Each proceeding refresh takes the next value;
  // only the request whose token is still the latest may apply results or clear
  // the spinner. This makes rapid A→B→A navigation safe even though A's path
  // repeats: the stale first A (older token) can't overwrite the newer A.
  _listingRequestSeq = 0;
  // Path of the last successfully-applied listing; lets a forced post-mutation
  // refresh tell itself apart from a duplicate (pre-load) navigation.
  _loadedPath = null;
  // A forced refresh that arrived while the same dir was already loading; re-run
  // it once the in-flight load settles so a mutation refresh isn't dropped.
  _pendingForcedReload = null;
  // Bumped each time a fresh listing replaces `entries`. A background archive
  // summary hydration only merges when this is unchanged, so a same-path refresh
  // can't be clobbered by an older hydration response.
  _listingGeneration = 0;

  // Selection
  selectedFiles = new SvelteMap();
  lastSelectedIndex = $state(-1);

  // Search
  searchMode = $state(false);
  searchResults = $state(null);
  searchQuery = $state('');
  searching = $state(false);

  // View state
  filter = $state(null);
  sortBy = $state('name');
  sortOrder = $state('asc');
  page = $state(1);
  pageSize = $state(DEFAULT_PAGE_SIZE);
  autoRefresh = $state(true);

  // ─── Derived ──────────────────────────────────────────────────────────
  get breadcrumbSegments() {
    if (!this.currentPath) return [];
    const segments = this.currentPath.split('/').filter(Boolean);
    let acc = '';
    return segments.map((name) => {
      acc += `/${name}`;
      return { name, path: acc };
    });
  }

  /**
   * Entries to render. When in search mode, flattens the
   * /api/files/search response (files + archive members) into a single
   * list and applies the client-side `searchQuery` filter (the backend
   * search endpoint takes no query, the query is purely client-side,
   * matching the legacy UI's behavior). Otherwise returns the current
   * directory's `entries`.
   */
  get sourceEntries() {
    if (!this.searchMode) return this.entries;
    if (!this.searchResults) return [];
    // Normalize search response rows. The /api/files/search payload's
    // `files` array is plain files from the directory tree, and the
    // `archives` array is files *inside* archives (the backend builds
    // each row at app/routes/files.py:357-379 with paths like
    // `archive.zip::dir/disc.cue` and `in_archive: true`). Both are
    // selectable file rows from the user's perspective, so stamp
    // `type: 'file'` on both arrays, marking the archive-member rows
    // as `type: 'archive'` would wrongly exclude them from Select All
    // (toggleSelectAll filters out archive containers) and break the
    // extension filter in filteredEntries (which only keeps files).
    const files = (this.searchResults.files ?? []).map((f) => ({
      ...f,
      type: f.type ?? 'file',
    }));
    const archived = (this.searchResults.archives ?? []).map((a) => ({
      ...a,
      type: a.type ?? 'file',
    }));
    const flat = [...files, ...archived];
    const q = (this.searchQuery ?? '').trim().toLowerCase();
    if (!q) return flat;
    return flat.filter((e) => {
      const name = (e.name ?? '').toLowerCase();
      const path = (e.path ?? '').toLowerCase();
      return name.includes(q) || path.includes(q);
    });
  }

  get sortedEntries() {
    const list = this.sourceEntries.slice();
    const order = this.sortOrder === 'asc' ? 1 : -1;
    list.sort((a, b) => {
      // Directories first
      if (a.type === 'directory' && b.type !== 'directory') return -1;
      if (a.type !== 'directory' && b.type === 'directory') return 1;
      let av, bv;
      switch (this.sortBy) {
        case 'size':
          av = a.size ?? 0;
          bv = b.size ?? 0;
          break;
        case 'extension':
          av = (a.extension ?? '').toLowerCase();
          bv = (b.extension ?? '').toLowerCase();
          break;
        case 'type':
          av = a.type;
          bv = b.type;
          break;
        default:
          av = a.name.toLowerCase();
          bv = b.name.toLowerCase();
      }
      if (av < bv) return -1 * order;
      if (av > bv) return 1 * order;
      // Stable final tiebreaker: rows with equal primary keys keep a
      // deterministic order across refreshes, so they don't jump when the
      // [...files, ...archives] input order shifts (issue #183). Follows the
      // sort direction so toggling asc/desc fully inverts the view.
      const ap = (a.path ?? '').toLowerCase();
      const bp = (b.path ?? '').toLowerCase();
      if (ap < bp) return -1 * order;
      if (ap > bp) return 1 * order;
      // Case-sensitive final tiebreaker: on a case-sensitive volume /Foo.iso and
      // /foo.iso are distinct rows that tie on the lowercased compare above, so
      // fall through to the raw path for a total, stable order (issue #183).
      const aps = a.path ?? '';
      const bps = b.path ?? '';
      if (aps < bps) return -1 * order;
      if (aps > bps) return 1 * order;
      return 0;
    });
    return list;
  }

  get filteredEntries() {
    if (!this.filter) return this.sortedEntries;
    const f = this.filter.toLowerCase();
    return this.sortedEntries.filter((e) => {
      if (e.type !== 'file') return true;
      // Suffix match on the row's NAME, not equality against `extension`.
      // The filter values come from `registry.allFilterableExts()`, which
      // includes compound extensions (`.nkit.iso`), while the backend
      // deliberately records only the trailing `Path.suffix` (`.iso`) in
      // `extension` — so an equality test hides every row the compound filter
      // is meant to show. The extension equality is kept as a fallback for
      // rows that carry no name. A plain `.iso` filter still matches
      // `Game.nkit.iso`, exactly as it did before: that row's `extension`
      // was already `.iso`.
      const name = (e.name ?? e.path ?? '').toLowerCase();
      return name.endsWith(f) || (e.extension ?? '').toLowerCase() === f;
    });
  }

  get pageCount() {
    return Math.max(1, Math.ceil(this.filteredEntries.length / this.pageSize));
  }

  get visibleEntries() {
    const start = (this.page - 1) * this.pageSize;
    return this.filteredEntries.slice(start, start + this.pageSize);
  }

  get allVisibleSelected() {
    // Match the toggleSelectAll predicate: directories, archive
    // containers, and rows not accepted by the active conversion mode
    // are never selectable.
    const visible = this.visibleEntries.filter((e) => this._isSelectable(e));
    if (visible.length === 0) return false;
    return visible.every((e) => this.selectedFiles.has(e.path));
  }

  // ─── Volumes ──────────────────────────────────────────────────────────
  async loadVolumes() {
    this.volumesLoading = true;
    this.volumesError = null;
    try {
      const data = await api.getVolumes();
      this.volumes = Array.isArray(data) ? data : [];
      if (!this.selectedVolume && this.volumes.length > 0) {
        await this.selectVolume(this.volumes[0]);
      }
    } catch (e) {
      this.volumesError = e?.message ?? 'Failed to load volumes';
    } finally {
      this.volumesLoading = false;
    }
  }

  async selectVolume(volume) {
    if (!volume) return;
    this.exitSearch();
    this.selectedVolume = volume;
    this.currentPath = volume.path;
    this.currentArchivePath = null;
    this.clearSelection();
    this.page = 1;                       // reset paging, new listing
    // Force so navigation isn't suppressed while jobs are running.
    await this.refresh({ force: true });
  }

  // ─── Navigation ───────────────────────────────────────────────────────
  async navigate(path) {
    if (!path) return;
    // Refresh() short-circuits when searchMode is true, so we'd be
    // stuck on the old search results under the new currentPath if we
    // didn't drop search state first.
    this.exitSearch();
    this.currentPath = path;
    this.currentArchivePath = null;
    this.clearSelection();
    this.page = 1;                       // smaller folder shouldn't strand on page N>1
    await this.refresh({ force: true });
  }

  /**
   * Internal helper: fetch the archive listing into `entries`. Does
   * NOT clear selection, reset paging, or flip currentArchivePath.
   * Used by both browseArchive() (entering anew) and refresh() (when
   * already inside an archive) so refresh doesn't trash the user's
   * archive-member selections.
   */
  async _loadArchiveEntries(archivePath) {
    const data = await api.listArchive(archivePath);
    return (data.files ?? []).map((f) => {
      // Archive members in subdirectories (e.g. "games/disc.cue") expose
      // the full subpath via `internal_path`. Falling back to `name` (the
      // basename) would build `archive::disc.cue` and the backend would
      // fail to locate the member during conversion.
      const member = f.internal_path ?? f.name;
      return {
        ...f,
        type: 'file',
        path: `${archivePath}::${member}`,
      };
    });
  }

  /**
   * Enter an archive view. Used when the user clicks an archive row;
   * clears any parent-directory selection, resets paging, and only
   * commits the archive view after the listing loads successfully.
   */
  async browseArchive(archivePath) {
    if (!archivePath) return;
    try {
      const next = await this._loadArchiveEntries(archivePath);
      // Success, commit atomically: clear stale parent-dir selections,
      // reset paging, swap entries, then flip into archive mode.
      this.clearSelection();
      this.page = 1;
      this.entries = next;
      this.currentArchivePath = archivePath;
      this.entriesError = null;
    } catch (e) {
      // Leave the previous listing in place; surface the error.
      this.entriesError = e?.message ?? 'Failed to read archive';
    }
  }

  leaveArchive() {
    // Drop archive-member selections (now-invisible archive::member
    // entries) so they can't tag along with parent-folder selections
    // on the next submission.
    this.clearSelection();
    this.currentArchivePath = null;
    this.page = 1;
    return this.refresh({ force: true });
  }

  /**
   * @param {{ force?: boolean }} [opts] - `force: true` overrides the
   *   "skip while jobs active and autoRefresh off" guard, so explicit
   *   user navigation (volume switch, directory change, leave archive)
   *   always updates the listing.
   */
  async refresh({ force = false } = {}) {
    // Search mode keeps the recursive result set, not the current
    // directory. A vanilla refresh against currentPath would clobber
    // the user's search view. But modals (rename/delete) DO call
    // refresh({ force: true }) after a successful mutation expecting
    // the listing to reflect the change, silently skipping in search
    // mode leaves stale rows pointing at paths that no longer exist
    // and the user can re-act on them. When forced, re-run the
    // search instead of dropping the call entirely.
    if (this.searchMode) {
      // Re-run the recursive scan, preserving the active filter, which
      // is an empty string for a one-click "Search all" view, so we
      // re-fetch on any forced refresh rather than gating on a non-empty
      // query (the legacy "Search All" behaviour).
      if (force && this.currentPath) {
        await this._enterSearch(this.searchQuery);
      }
      return;
    }
    if (!force && jobs.hasActive && !this.autoRefresh) return;
    // When the user is inside an archive view, a vanilla refresh against
    // currentPath would replace the archive members with the parent
    // directory listing while keeping currentArchivePath set, so
    // selections/conversions would suddenly operate on the wrong rows.
    // Re-fetch the archive contents via the internal loader (which does
    // NOT clear selection, so the user's archive-member picks survive
    // manual/auto refresh).
    if (this.currentArchivePath) {
      this.loading = true;
      this.entriesError = null;
      try {
        this.entries = await this._loadArchiveEntries(this.currentArchivePath);
      } catch (e) {
        this.entriesError = e?.message ?? 'Failed to read archive';
      } finally {
        this.loading = false;
        this._clampPage();
      }
      return;
    }
    if (!this.currentPath) {
      this.entries = [];
      this._clampPage();
      return;
    }
    const requested = this.currentPath;
    // Collapse a duplicate in-flight load of the same directory (e.g. a
    // double-fired navigate). A listing can take a while in huge folders and
    // the browser caps connections per host, so a redundant identical request
    // just starves the pool behind the one already running. A load for a
    // different path still proceeds. Exception: a *forced* refresh of a dir we
    // have already loaded once is a deliberate re-load (rename/delete call
    // refresh({ force: true }) to reflect a mutation), so it must not be dropped
    // — queue it to re-run when the in-flight load settles. A forced refresh
    // that races the dir's *initial* load is still just a duplicate navigation
    // and is collapsed.
    if (this._inflightListingPath === requested) {
      if (force && this._loadedPath === requested) this._pendingForcedReload = requested;
      return;
    }
    this._inflightListingPath = requested;
    // Token for this specific request. Compared against the latest token (not
    // the path) so an A→B→A sequence — where the older and newer A share a path
    // — can't let the stale older A apply results or clear the spinner.
    this._listingRequestSeq += 1;
    const myReq = this._listingRequestSeq;
    this.loading = true;
    this.entriesError = null;
    try {
      const data = await api.listFiles(requested, true);
      // Drop a stale response if this request was superseded, or the user
      // navigated away (into another dir, an archive, or search) while it was
      // in flight.
      if (
        this._listingRequestSeq !== myReq || this.currentPath !== requested
        || this.currentArchivePath || this.searchMode
      ) {
        return;
      }
      this.entries = data?.entries ?? [];
      this._loadedPath = requested;
      // New listing generation so a late hydration from the previous listing
      // can't merge into these rows. The visible page's archive badges are then
      // hydrated by FileList's effect (scoped to the visible page, re-running on
      // pagination/sort/filter) — the listing itself renders without waiting.
      this._listingGeneration += 1;
    } catch (e) {
      if (this._listingRequestSeq !== myReq || this.currentPath !== requested) return;
      this.entriesError = e?.message ?? 'Failed to load files';
      this.entries = [];
    } finally {
      // Only the latest request clears the spinner / in-flight path. A stale
      // request finishing later must not hide a newer request's spinner.
      const active = this._listingRequestSeq === myReq;
      if (active) {
        this._inflightListingPath = null;
        this.loading = false;
      }
      this._clampPage();
      // A forced refresh arrived for this dir while it was loading; run it now
      // against fresh server state.
      if (active && this._pendingForcedReload === requested) {
        this._pendingForcedReload = null;
        if (this.currentPath === requested && !this.searchMode && !this.currentArchivePath) {
          this.refresh({ force: true });
        }
      }
    }
  }

  /**
   * Hydrate archive-summary badges for the archives on the CURRENT visible page.
   * Public entry point for components to call on pagination / sort / filter
   * changes (it reads `visibleEntries` synchronously, so a Svelte `$effect` that
   * invokes it will re-run when those change). Idempotent: rows already hydrated
   * (archive_items set) are skipped.
   */
  hydrateVisibleArchiveSummaries() {
    if (this.searchMode || this.currentArchivePath) return;
    this._hydrateArchiveSummaries(this._listingGeneration);
  }

  /**
   * Fetch per-archive summaries (member counts + verifiable_by) for the archive
   * rows on the visible page and merge them into `entries` (and any selected copy
   * of those rows), so the badges appear once the background batch resolves.
   * Mirrors the chdMetadata / datMatching hydration pattern, but merges into the
   * entry rows (FileRow/RowActionsMenu read these fields straight off the entry)
   * rather than a side store. Scoping to the visible page bounds the batch size
   * and the server-side archive opening. No-op when there are no un-hydrated
   * archives on screen; failures are non-fatal (rows just stay un-summarized).
   * @param {number} generation - the listing generation these rows belong to
   */
  async _hydrateArchiveSummaries(generation) {
    // Only the visible page: keeps the batch small and the server from opening
    // every archive in a huge folder at once.
    const archivePaths = this.visibleEntries
      .filter((e) => e?.type === 'archive' && e?.path && e.archive_items == null)
      .map((e) => e.path);
    if (archivePaths.length === 0) return;
    let summary;
    try {
      summary = await api.getArchiveSummaryBatch(archivePaths);
    } catch (_e) {
      return;
    }
    // Bail if the listing was replaced (navigation or same-path refresh) while
    // the summaries were in flight, so an older response can't clobber newer rows.
    if (this._listingGeneration !== generation || this.searchMode || this.currentArchivePath) {
      return;
    }
    let changed = false;
    const next = this.entries.map((e) => {
      const merged = this._mergeArchiveSummary(e, summary);
      if (merged !== e) {
        changed = true;
        // Keep any selected copy of this row in sync, the bulk Verify gate
        // reads verifiable_by from selectedFiles, not from `entries`.
        if (this.selectedFiles.has(merged.path)) this.selectedFiles.set(merged.path, merged);
      }
      return merged;
    });
    if (changed) this.entries = next;
  }

  /**
   * Merge an archive's hydrated summary into a single entry row. Returns a NEW
   * object when a (non-error) summary applies to this archive path, otherwise
   * returns the same entry unchanged — callers detect a real change by identity
   * (`merged !== entry`).
   * @param {any} entry - a listing row
   * @param {Record<string, any>} summary - path -> summary (or {error}) map
   */
  _mergeArchiveSummary(entry, summary) {
    if (entry?.type !== 'archive' || !entry?.path) return entry;
    const s = summary[entry.path];
    if (!s || s.error) return entry;
    return {
      ...entry,
      archive_items: s.archive_items,
      archive_has_output: s.archive_has_output,
      archive_truncated: s.archive_truncated,
      verifiable_by: Array.isArray(s.verifiable_by) ? s.verifiable_by : entry.verifiable_by,
    };
  }

  /**
   * Clamp `page` into the new pageCount after entries shrink, a
   * background refresh that drops the visible row count could
   * otherwise strand the user on an empty page they can't see is
   * empty. Never moves the page if the current value is still valid.
   */
  _clampPage() {
    const max = this.pageCount;
    if (this.page > max) this.page = max;
  }

  /**
   * Run the recursive `/api/files/search` scan (subdirectories + inside
   * archives) and flip into search mode on success, applying `query` as
   * the client-side filter. An empty `query` is valid, it surfaces every
   * convertible file in the tree (the legacy "Search All" view); see
   * sourceEntries, which returns the full flattened set when searchQuery
   * is blank.
   */
  async _enterSearch(query) {
    if (!this.currentPath) return;
    // Drop overlapping searches, a second in-flight call would clear the
    // `searching` flag when it finishes and let the busy state lift while
    // an earlier request is still running.
    if (this.searching) return;
    this.searching = true;
    // Clear any stale error from a prior failed search/load, symmetric
    // with the directory load paths. Otherwise the error banner survives
    // through a retry and even past a later successful search.
    this.entriesError = null;
    try {
      const data = await api.searchFiles(this.currentPath, true, true);
      // Flip into search mode only on success. If the request fails,
      // leaving searchMode false keeps the user looking at the current
      // directory's entries instead of an empty search view.
      this.searchResults = data;
      this.searchQuery = query;
      this.searchMode = true;
      this.page = 1;
      // Drop the current directory's selection set, those paths
      // aren't necessarily in the recursive search results, so
      // leaving them selected would let the bulk-action bar / Convert
      // panel act on rows the user can't see. Symmetric with the
      // cleanup the X button does on exitSearch.
      this.clearSelection();
    } catch (e) {
      this.entriesError = e?.message ?? 'Search failed';
      // Stay out of search mode so the directory listing remains visible.
    } finally {
      this.searching = false;
    }
  }

  /**
   * Text-box search: filters the recursive result set by `query`. An
   * empty query exits search and returns to the plain directory listing
   * (the X / clear-search contract).
   */
  async search(query) {
    // /api/files/search has no query parameter, it returns every
    // convertible file under the current path. The `query` is used
    // client-side to filter the returned set (see sourceEntries).
    if (!query) {
      this.exitSearch();
      return;
    }
    await this._enterSearch(query);
  }

  /**
   * One-click "Search all", recursively list every convertible file
   * under the current path, including files inside archives, with no
   * text filter. Restores the legacy "🔍 Search All" action.
   */
  async searchAll() {
    await this._enterSearch('');
  }

  exitSearch() {
    this.searchMode = false;
    this.searchResults = null;
  }

  // ─── Selection ────────────────────────────────────────────────────────
  /**
   * Predicate for whether a row is checkable at all, directories and
   * archive containers are out (the backend rejects archive paths as
   * conversion inputs; directories aren't files). Conversion-mode
   * eligibility is NOT enforced here because the same selection set
   * also drives bulk Verify and bulk Delete; gating it at toggle time
   * would hide checkboxes on rows the user might want to verify or
   * delete (e.g. a verified `.chd` while in CHDMAN createcd). The
   * convert path filters its inputs separately via
   * `conversion.allowsInput()`.
   */
  _isSelectable(entry) {
    if (!entry) return false;
    if (entry.type === 'directory') {
      // Directories are navigation-only EXCEPT under a directory-input mode
      // (makeps3iso folder_to_iso), where a folder the backend marked
      // convertible becomes a selectable job unit. The name-click still
      // navigates (handled in FileRow); only the checkbox selects, mirroring
      // how archive rows browse-on-name / select-on-checkbox.
      return conversion.allowsInputEntry(entry);
    }
    if (entry.type === 'archive') {
      // Archives are normally browse-into containers, not selectable rows.
      // They become selectable only when the active mode takes the archive
      // itself as direct input (e.g. romz_extract on a .7z/.zip), in which
      // case the worker extracts the contained ROM. Name-click still browses
      // into the archive; the checkbox handles selection.
      return conversion.allowsInput(entry.path);
    }
    return true;
  }

  /** Public predicate: may this row be selected under the active mode? */
  isSelectable(entry) {
    return this._isSelectable(entry);
  }

  /**
   * The subset of the current selection that the active conversion
   * mode would accept as input. ConvertPanel filters through this
   * before calling `conversion.submit()` so the worker never sees
   * paths it would reject.
   */
  get convertibleSelection() {
    const out = [];
    for (const entry of this.selectedFiles.values()) {
      // Entry-aware gate (not just the path) so a directory mode submits only
      // convertible folders and a file mode never submits a stale folder
      // selection left over from switching tools.
      if (conversion.allowsInputEntry(entry)) out.push(entry);
    }
    return out;
  }

  toggleSelect(entry, { shift = false } = {}) {
    if (!this._isSelectable(entry)) return;
    // Range-selectable rows are non-directory, non-archive AND match
    // the active mode's input extensions. Same predicate as
    // toggleSelectAll so shift-click never picks up rows the worker
    // would reject.
    const visible = this.visibleEntries.filter((e) => this._isSelectable(e));
    const idx = visible.findIndex((e) => e.path === entry.path);

    if (shift && this.lastSelectedIndex >= 0 && idx >= 0) {
      const [start, end] = idx > this.lastSelectedIndex
        ? [this.lastSelectedIndex, idx]
        : [idx, this.lastSelectedIndex];
      for (let i = start; i <= end; i += 1) {
        this.selectedFiles.set(visible[i].path, visible[i]);
      }
    } else if (this.selectedFiles.has(entry.path)) {
      this.selectedFiles.delete(entry.path);
    } else {
      this.selectedFiles.set(entry.path, entry);
    }
    this.lastSelectedIndex = idx;
  }

  toggleSelectAll() {
    // Archive containers can't be submitted as conversion inputs, the
    // backend expects either a regular file path or `archive::member`
    // form. Same gate for mode-incompatible inputs (e.g. .rvz under
    // CHDMAN createcd). Filter to rows the active conversion mode
    // actually accepts.
    const visible = this.visibleEntries.filter((e) => this._isSelectable(e));
    if (this.allVisibleSelected) {
      for (const e of visible) this.selectedFiles.delete(e.path);
    } else {
      for (const e of visible) this.selectedFiles.set(e.path, e);
    }
  }

  clearSelection() {
    this.selectedFiles.clear();
    this.lastSelectedIndex = -1;
  }

  // ─── Sorting / paging / filter ────────────────────────────────────────
  setSort(field) {
    if (!SORT_FIELDS.has(field)) return;
    if (this.sortBy === field) {
      this.sortOrder = this.sortOrder === 'asc' ? 'desc' : 'asc';
    } else {
      this.sortBy = field;
      this.sortOrder = 'asc';
    }
    this.page = 1;
  }

  setFilter(ext) {
    this.filter = ext ? ext.toLowerCase() : null;
    this.page = 1;
  }

  setPage(p) {
    this.page = Math.max(1, Math.min(p, this.pageCount));
  }
}

export const fileBrowser = new FileBrowserStore();
