<script>
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { ui } from '$lib/stores/ui.svelte.js';
  import { chdMetadata } from '$lib/stores/chdMetadata.svelte.js';
  import { datMatching } from '$lib/stores/datMatching.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import FileRow from './FileRow.svelte';
  import Breadcrumb from './Breadcrumb.svelte';
  import Pager from '$lib/components/ui/Pager.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import IconButton from '$lib/components/ui/IconButton.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Splitter from '$lib/components/ui/Splitter.svelte';
  import { layout } from '$lib/stores/layout.svelte.js';
  import RefreshCw from '@lucide/svelte/icons/refresh-cw';
  import Search from '@lucide/svelte/icons/search';
  import FolderSearch from '@lucide/svelte/icons/folder-search';
  import XIcon from '@lucide/svelte/icons/x';
  import ChevronUp from '@lucide/svelte/icons/chevron-up';
  import ChevronDown from '@lucide/svelte/icons/chevron-down';
  import ChevronsUpDown from '@lucide/svelte/icons/chevrons-up-down';
  import Filter from '@lucide/svelte/icons/funnel';
  import Loader from '@lucide/svelte/icons/loader-circle';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
  import ArrowLeft from '@lucide/svelte/icons/arrow-left';
  import ShieldCheck from '@lucide/svelte/icons/shield-check';
  import Trash2 from '@lucide/svelte/icons/trash-2';

  const entries = $derived(fileBrowser.visibleEntries);
  const pageCount = $derived(fileBrowser.pageCount);
  const page = $derived(fileBrowser.page);
  const sortBy = $derived(fileBrowser.sortBy);
  const sortOrder = $derived(fileBrowser.sortOrder);
  const filter = $derived(fileBrowser.filter);
  const loading = $derived(fileBrowser.loading);
  const searching = $derived(fileBrowser.searching);
  const pageSize = $derived(fileBrowser.pageSize);
  const pageSizeOptions = $derived(fileBrowser.pageSizeOptions);

  // Resizable table columns, stored per tool. `cols` carries the
  // effective widths (defaults filled in); `nameSet` is the explicit
  // Name width if the user has dragged it, when unset, Name has no fixed
  // width so it absorbs the table's slack. The table min-width forces the
  // horizontal scrollbar once the fixed columns outgrow the container.
  const colTool = $derived(ui.workspaceTool);
  const cols = $derived(layout.columnsFor(colTool));
  const nameSet = $derived(layout.columns[colTool]?.name ?? null);
  const tableMinWidth = $derived(32 + 84 + cols.size + cols.ext + cols.status + (nameSet ?? 160));

  // Snapshot of a column's width at drag start; the move handler applies
  // the cumulative delta on top of it.
  let dragColStart = 0;
  let tableEl = $state(null);
  function startColDrag(col) {
    // Measure the actual rendered header width rather than trusting the
    // stored value. The Name column flexes when unset, so its real width
    // is wider than the default, without this it would jump to the
    // default on the first drag. Pin that measured width so Name keeps
    // its place and resizing continues smoothly from there.
    const th = tableEl?.querySelector(`th[data-col="${col}"]`);
    const measured = th?.offsetWidth;
    dragColStart = measured ?? cols[col];
    if (col === 'name' && nameSet == null && measured) {
      layout.setColumnWidth(colTool, 'name', measured);
    }
  }
  const error = $derived(fileBrowser.entriesError);
  const allSelected = $derived(fileBrowser.allVisibleSelected);
  const selectedCount = $derived(fileBrowser.selectedFiles.size);
  const searchMode = $derived(fileBrowser.searchMode);
  const archiveMode = $derived(!!fileBrowser.currentArchivePath);

  // Cross-page "select all" affordance. The header checkbox only ever covers
  // the visible page, so in a folder of 2000+ discs the user would have to tick
  // it once per page. Once anything is selected we offer the whole current view
  // — every page of it — in one click.
  //
  // Everything here is derived from the store's own mode-aware predicate
  // (`selectableEntries` → `_isSelectable`), so it is not tied to any tool,
  // extension, or view: it counts exactly the rows the active mode accepts,
  // whether that's .iso files under CHDMAN, folders under makeps3iso
  // folder_to_iso, or archive members inside a "Search all" result set.
  const selectableTotal = $derived(fileBrowser.selectableEntries.length);
  // Whether every selectable row is a plain file. `filteredEntries` passes
  // non-file rows (directories, archive containers) through the extension filter
  // unconditionally — the filter only narrows files — so under a directory-input
  // mode like makeps3iso `folder_to_iso` the selectable set is entirely folders
  // even with `.iso` picked. Naming the extension there would promise ".iso
  // items" and hand back folders, so the label only claims the extension when
  // the rows really are files it applies to.
  const selectableAllFiles = $derived(
    fileBrowser.selectableEntries.every((e) => e?.type === 'file'),
  );
  const selectableOnPage = $derived(
    fileBrowser.visibleEntries.filter((e) => fileBrowser.isSelectable(e)).length,
  );
  const allFilteredSelected = $derived(fileBrowser.allFilteredSelected);
  // Only worth showing when there is genuinely something off-page to gain.
  const canSelectAllPages = $derived(
    selectedCount > 0 && !allFilteredSelected && selectableTotal > selectableOnPage,
  );
  // Name the scope honestly: an active extension filter means "all N" is the
  // filtered set, not the whole folder, and search/archive views aren't folders.
  const selectAllScope = $derived(
    searchMode ? 'in these search results' : archiveMode ? 'in this archive' : 'in this folder',
  );
  const selectAllExt = $derived(filter && selectableAllFiles ? ` ${filter}` : '');
  const selectAllLabel = $derived(
    `Select all ${selectableTotal}${selectAllExt} ${selectableTotal === 1 ? 'item' : 'items'} ${selectAllScope}`,
  );
  const autoRefresh = $derived(fileBrowser.autoRefresh);
  const jobsActive = $derived(jobs.hasActive);

  // Extension filter options come from the registry, every tool's
  // source + verify extensions, deduped, in declaration order. Adding
  // a new tool surfaces its inputs/outputs in this dropdown automatically.
  const filterableExts = $derived(registry.allFilterableExts());

  // Selection bulk actions: "Verify selected" surfaces only when at
  // least one selected file has a verify-extension match in the
  // registry AND lives on the real filesystem. Archive-member paths
  // (`archive.zip::disc.iso`) are rejected by the verify-batch
  // endpoint (treat_archives=false + os.path.isfile on the literal
  // string), so a selection of only archive members would surface the
  // button and then report zero verifications.
  const selectedHasVerifiable = $derived.by(() => {
    const sel = Array.from(fileBrowser.selectedFiles.values());
    return sel.some(
      (e) =>
        e?.path &&
        !e.path.includes('::') &&
        // Honor the backend's per-file gate (romz: single-ROM .7z/.zip only),
        // matching the row menu and the bulk-verify modal, so a non-single-ROM
        // archive selection doesn't surface a "Verify selected" that the batch
        // endpoint would then reject.
        registry.verifyToolForPath(e.path, e?.verifiable_by),
    );
  });

  // Bulk Delete has the same archive-member problem: /files/delete-batch
  // validates each path with treat_archives=false + os.path.exists, so
  // forwarding `archive::member` paths always fails. Hide the button
  // when the selection is purely archive members.
  const selectedHasFilesystemPath = $derived.by(() => {
    const sel = Array.from(fileBrowser.selectedFiles.values());
    // A folded makeps3iso split set has a real .0 path but is excluded from
    // bulk delete (openBulkDelete drops split_parts != null rows, matching the
    // disabled single-row Delete). Gate on the same predicate so a selection of
    // *only* split rows doesn't surface a Delete button that opens an empty
    // "Delete 0 files?" modal; a mixed selection still shows it for the rest.
    return sel.some((e) => e?.path && !e.path.includes('::') && e.split_parts == null);
  });

  let searchInput = $state('');

  // Count of terminated dat_match background jobs. Tracked as a
  // dep of the hydration effect so we re-pull the match cache when
  // a backend dat_match job (kicked by hydrateAndMatch) finishes,
  // without it, newly-browsed files keep an empty DAT badge until
  // some unrelated navigation re-runs the effect.
  const datMatchTerminalCount = $derived(
    jobs.jobs.reduce(
      (n, j) =>
        j.mode === 'dat_match' &&
        (j.status === 'completed' || j.status === 'failed' || j.status === 'cancelled')
          ? n + 1
          : n,
      0,
    ),
  );

  // Hydrate badge stores for the paths actually on screen. /api/files
  // doesn't pre-populate DAT matches or CHD media_type, the legacy UI
  // ran these hydration calls after every listing change. We mirror
  // that here so badges show up without per-row fetches. Effect runs
  // again whenever the visible page changes (navigation, pagination,
  // search, filter) OR a dat_match background job completes.
  // hydrateAndMatch also kicks a background match job for uncached
  // visible paths, so newly browsed/converted files pick up a DAT
  // badge after one round-trip, the cache lookup alone never hashes.
  $effect(() => {
    // Track explicitly so the effect re-runs on dat_match completion.
    datMatchTerminalCount;
    const allPaths = entries.map((e) => e?.path).filter(Boolean);
    if (allPaths.length === 0) return;
    // Hydrate archive-summary badges (member counts, verifiable_by) for the
    // archives on this visible page. Re-runs with this effect on page / sort /
    // filter changes because it reads the same visible `entries`.
    fileBrowser.hydrateVisibleArchiveSummaries();
    // chdMetadata.hydrate is cheap for any path (no jobs spawned), so
    // the full list is fine. DAT match jobs need filtering on two
    // axes:
    //   1. Skip non-regular files (directories/archive containers),
    //      the backend dat_match job skips them without caching, so
    //      re-firing the effect would re-pick them forever.
    //   2. Restrict to extensions the DAT matcher can plausibly
    //      identify (registered convertible source + verify
    //      extensions). .cue / .gdi manifests, artwork, log files,
    //      etc. would otherwise be hashed-and-stored-as-unmatched on
    //      every page change, polluting the unmatched count and
    //      wasting I/O.
    chdMetadata.hydrate(allPaths).catch(() => {});
    if (datMatching.matchingAvailable) {
      const matchableExts = registry.allFilterableExts();
      const matchExtSet = new Set(matchableExts.map((e) => e.toLowerCase()));
      const filePaths = entries
        .filter((e) => {
          if (!e?.path || e.type === 'directory' || e.type === 'archive') return false;
          // A folded split set advertises `.iso` but its path is only the .0
          // part; hashing that as a whole ISO yields a false unmatched badge
          // (and wastes a 4 GB hash pass), so skip it.
          if (e.split_parts != null) return false;
          const ext = (e.extension ?? '').toLowerCase();
          return ext && matchExtSet.has(ext);
        })
        .map((e) => e.path);
      if (filePaths.length > 0) {
        datMatching.hydrateAndMatch(filePaths).catch(() => {});
      }
    }
  });

  function openBulkVerify() {
    // Forward only filesystem paths; the verify-batch endpoint
    // rejects `archive::member` paths (treat_archives=false +
    // os.path.isfile on the literal string). Folded split sets are excluded
    // too — their path is only the .0 part, so a verify would inspect a
    // partial image.
    ui.bulkVerifyItems = Array.from(fileBrowser.selectedFiles.values())
      .filter((e) => e?.path && !e.path.includes('::') && e.split_parts == null);
  }

  function openBulkDelete() {
    // Same filter as Verify, /files/delete-batch fails on archive
    // members for the same treat_archives=false reason. Folded makeps3iso
    // split sets are also excluded: their row path is only the .0 part, so a
    // single-path delete-batch would orphan .1+ (matching the disabled
    // single-row Delete action).
    ui.bulkDeleteEntries = Array.from(fileBrowser.selectedFiles.values())
      .filter((e) => e?.path && !e.path.includes('::') && e.split_parts == null);
  }

  function sortIcon(field) {
    if (sortBy !== field) return ChevronsUpDown;
    return sortOrder === 'asc' ? ChevronUp : ChevronDown;
  }

  function sortAria(field) {
    if (sortBy !== field) return 'none';
    return sortOrder === 'asc' ? 'ascending' : 'descending';
  }

  function submitSearch(e) {
    e?.preventDefault?.();
    fileBrowser.search(searchInput.trim());
  }

  function clearSearch() {
    searchInput = '';
    // Drop any selection picked up from the (now-invisible) search
    // result set. The recursive results may live in any subdirectory,
    // so leaving them in selectedFiles would let the selection bar /
    // Convert panel act on rows the user can no longer see.
    fileBrowser.clearSelection();
    fileBrowser.exitSearch();
  }
</script>

<section class="filelist" aria-label="Files">
  <div class="toolbar">
    <div class="crumbs">
      {#if archiveMode}
        <IconButton label="Leave archive" size="sm" onclick={() => fileBrowser.leaveArchive()}>
          <ArrowLeft size={14} />
        </IconButton>
      {/if}
      <Breadcrumb />
    </div>

    <form class="search" onsubmit={submitSearch} role="search">
      <Search size={14} class="search-icon" />
      <input
        type="search"
        placeholder="Search this folder…"
        bind:value={searchInput}
        aria-label="Search files"
      />
      {#if searchMode}
        <IconButton label="Clear search" size="sm" onclick={clearSearch}>
          <XIcon size={12} />
        </IconButton>
      {/if}
    </form>

    <div class="actions">
      <Button
        variant="secondary"
        size="sm"
        loading={searching}
        title="Recursively list every convertible file under this folder, including inside archives"
        onclick={() => fileBrowser.searchAll()}
      >
        {#snippet icon()}
          {#if searching}<Loader class="spin" size={14} />{:else}<FolderSearch size={14} />{/if}
        {/snippet}
        {searching ? 'Searching…' : 'Search all'}
      </Button>
      <label class="filter">
        <Filter size={12} />
        <select
          aria-label="Filter by extension"
          value={filter ?? ''}
          onchange={(e) => fileBrowser.setFilter(e.currentTarget.value || null)}
        >
          <option value="">All files</option>
          {#each filterableExts as ext (ext)}
            <option value={ext}>{ext}</option>
          {/each}
        </select>
      </label>
      <IconButton
        label="Refresh"
        size="sm"
        title={jobsActive && !autoRefresh ? 'Auto-refresh paused while jobs active' : 'Refresh listing'}
        onclick={() => fileBrowser.refresh({ force: true })}
      >
        {#if loading}<Loader class="spin" size={14} />{:else}<RefreshCw size={14} />{/if}
      </IconButton>
    </div>
  </div>

  {#if selectedCount > 0}
    <div class="selection-bar" role="status">
      <span class="count-label">{selectedCount} selected</span>
      {#if selectedHasVerifiable}
        <button type="button" class="bulk-action" onclick={openBulkVerify}>
          <ShieldCheck size={12} aria-hidden="true" /> Verify
        </button>
      {/if}
      {#if selectedHasFilesystemPath}
        <button type="button" class="bulk-action danger" onclick={openBulkDelete}>
          <Trash2 size={12} aria-hidden="true" /> Delete
        </button>
      {/if}
      <button type="button" class="link" onclick={() => fileBrowser.clearSelection()}>
        Clear
      </button>
    </div>
    {#if canSelectAllPages}
      <div class="select-all-bar">
        {#if allSelected}
          <span>All {selectableOnPage} on this page are selected.</span>
        {/if}
        <button type="button" class="link-strong" onclick={() => fileBrowser.selectAllFiltered()}>
          {selectAllLabel}
        </button>
        <span class="muted-inline">(all {pageCount} pages)</span>
      </div>
    {:else if allFilteredSelected && pageCount > 1}
      <div class="select-all-bar">
        <span>All {selectableTotal} {selectAllScope} are selected, across all {pageCount} pages.</span>
        <button type="button" class="link-strong" onclick={() => fileBrowser.deselectAllFiltered()}>
          Deselect all
        </button>
      </div>
    {/if}
  {/if}

  {#if error}
    <div class="error-state"><TriangleAlert size={14} /> {error}</div>
  {/if}

  {#if entries.length === 0 && !loading}
    {#if searchMode}
      <EmptyState
        title="No matches"
        description={fileBrowser.searchQuery
          ? `Nothing here matched "${fileBrowser.searchQuery}". Try a different query, or clear search to browse normally.`
          : 'No convertible files were found anywhere under this folder (including inside archives). Clear search to browse normally.'}
        glyph="∅"
      />
    {:else}
      <EmptyState
        title="Empty folder"
        description="No convertible files here. Use the breadcrumb to navigate up, or pick a different volume."
        glyph="∅"
      />
    {/if}
  {:else}
    <div class="table-wrap">
      {#snippet colHandle(col, label)}
        <span class="col-resize">
          <Splitter
            variant="column"
            label={label}
            value={cols[col]}
            min={layout.columnLimit(col).min}
            max={layout.columnLimit(col).max}
            onstart={() => startColDrag(col)}
            onmove={(d) => layout.setColumnWidth(colTool, col, dragColStart + d)}
            onstep={(d) => layout.setColumnWidth(colTool, col, cols[col] + d)}
            onreset={() => layout.resetColumn(colTool, col)}
          />
        </span>
      {/snippet}
      <table bind:this={tableEl} class="table" style="min-width: {tableMinWidth}px;" aria-label={searchMode ? 'Search results' : 'Files'}>
        <colgroup>
          <col class="col-sel" />
          <col style={nameSet ? `width: ${nameSet}px` : undefined} />
          <col style="width: {cols.size}px" />
          <col style="width: {cols.ext}px" />
          <col style="width: {cols.status}px" />
          <col class="col-actions" />
        </colgroup>
        <thead>
          <tr>
            <th class="sel">
              <input
                type="checkbox"
                checked={allSelected}
                onchange={() => fileBrowser.toggleSelectAll()}
                title={pageCount > 1
                  ? 'Selects this page. Shift-click (or use the banner) to select every page.'
                  : undefined}
                aria-label={allSelected ? 'Deselect all on this page' : 'Select all on this page'}
                onclick={(e) => {
                  // Shift-click the header box = select the whole view, every page.
                  // Keeps the one-click-per-page default while giving power users a
                  // shortcut that mirrors the row-level shift-click range select.
                  if (!e.shiftKey) return;
                  e.preventDefault();
                  if (allFilteredSelected) fileBrowser.deselectAllFiltered();
                  else fileBrowser.selectAllFiltered();
                }}
              />
            </th>
            <th class="resizable" data-col="name" aria-sort={sortAria('name')}>
              <button type="button" class="th-button" onclick={() => fileBrowser.setSort('name')}>
                Name {#if sortBy === 'name'}{@const Icon = sortIcon('name')}<Icon size={12} class="sort-i" />{:else}<ChevronsUpDown size={12} class="sort-i muted" />{/if}
              </button>
              {@render colHandle('name', 'Resize Name column')}
            </th>
            <th class="size resizable" data-col="size" aria-sort={sortAria('size')}>
              <button type="button" class="th-button" onclick={() => fileBrowser.setSort('size')}>
                Size {#if sortBy === 'size'}{@const Icon = sortIcon('size')}<Icon size={12} class="sort-i" />{:else}<ChevronsUpDown size={12} class="sort-i muted" />{/if}
              </button>
              {@render colHandle('size', 'Resize Size column')}
            </th>
            <th class="ext resizable" data-col="ext" aria-sort={sortAria('extension')}>
              <button type="button" class="th-button" onclick={() => fileBrowser.setSort('extension')}>
                Ext {#if sortBy === 'extension'}{@const Icon = sortIcon('extension')}<Icon size={12} class="sort-i" />{:else}<ChevronsUpDown size={12} class="sort-i muted" />{/if}
              </button>
              {@render colHandle('ext', 'Resize Ext column')}
            </th>
            <th class="resizable" data-col="status">
              Status
              {@render colHandle('status', 'Resize Status column')}
            </th>
            <th class="actions">Actions</th>
          </tr>
        </thead>
        <tbody>
          {#each entries as entry (entry.path)}
            <FileRow {entry} />
          {/each}
        </tbody>
      </table>
    </div>
  {/if}

  <div class="footer">
    <Pager {page} {pageCount} onpage={(p) => fileBrowser.setPage(p)} />
    <div class="footer-end">
      {#if !searchMode && entries.length > 0}
        <span class="muted">{entries.length} shown</span>
      {/if}
      <!-- Rows per page. Sits outside the Pager (which hides itself at a single
           page) so the user can always get back to a smaller page size after
           picking one big enough to collapse the pagination. -->
      <label class="page-size">
        <span class="muted">Rows</span>
        <select
          aria-label="Rows per page"
          value={pageSize}
          onchange={(e) => fileBrowser.setPageSize(e.currentTarget.value)}
        >
          {#each pageSizeOptions as size (size)}
            <option value={size}>{size}</option>
          {/each}
        </select>
      </label>
    </div>
  </div>
</section>

<style>
  .filelist { display: flex; flex-direction: column; min-width: 0; gap: var(--space-3); }

  .toolbar {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto;
    gap: var(--space-3);
    align-items: center;
  }
  @media (max-width: 700px) {
    .toolbar { grid-template-columns: 1fr; }
  }
  .crumbs { display: flex; align-items: center; gap: var(--space-1); min-width: 0; }

  .search {
    position: relative;
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    padding: 2px var(--space-2);
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    min-width: 220px;
  }
  .search :global(.search-icon) { color: var(--text-3); flex-shrink: 0; }
  .search input {
    background: none;
    border: none;
    padding: var(--space-2) var(--space-1);
    color: var(--text-1);
    font-size: var(--text-sm);
    width: 100%;
    outline: none;
  }
  .search input::placeholder { color: var(--text-3); }
  .search:focus-within {
    border-color: var(--accent);
    box-shadow: var(--focus-ring);
  }

  .actions { display: inline-flex; align-items: center; gap: var(--space-2); }
  .filter {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    padding: 0 var(--space-2);
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-2);
    font-size: var(--text-xs);
  }
  .filter select {
    background: none;
    border: none;
    color: var(--text-1);
    font-size: var(--text-sm);
    padding: var(--space-2) 0;
    cursor: pointer;
  }
  .filter select:focus-visible { outline: none; }

  .selection-bar {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-3);
    background: var(--accent-muted);
    border-radius: var(--radius-md);
    color: var(--accent);
    font-size: var(--text-sm);
    font-weight: var(--weight-medium);
    flex-wrap: wrap;
  }
  .count-label { margin-right: var(--space-2); }
  .bulk-action {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    background: var(--surface-1);
    border: 1px solid var(--accent);
    color: var(--accent);
    padding: 4px 10px;
    border-radius: var(--radius-sm);
    font-size: var(--text-xs);
    cursor: pointer;
  }
  .bulk-action:hover { background: var(--accent); color: var(--accent-contrast); }
  .bulk-action.danger { border-color: var(--error); color: var(--error); }
  .bulk-action.danger:hover { background: var(--error); color: var(--text-inverse); }
  .link {
    background: none;
    border: none;
    color: inherit;
    cursor: pointer;
    text-decoration: underline;
    font: inherit;
    padding: 0;
    margin-left: auto;
  }

  /* Cross-page select-all prompt, sits directly under the selection bar so it
     reads as a continuation of it. */
  .select-all-bar {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-2);
    flex-wrap: wrap;
    padding: var(--space-2) var(--space-3);
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-2);
    font-size: var(--text-sm);
    text-align: center;
  }
  .link-strong {
    background: none;
    border: none;
    color: var(--accent);
    cursor: pointer;
    text-decoration: underline;
    font: inherit;
    font-weight: var(--weight-semibold);
    padding: 0;
  }
  .link-strong:hover { text-decoration: none; }
  .muted-inline { color: var(--text-3); font-size: var(--text-xs); }

  .error-state {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-3);
    background: var(--error-muted);
    border-radius: var(--radius-md);
    color: var(--error);
    font-size: var(--text-sm);
  }

  .table-wrap {
    overflow-x: auto;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--surface-1);
  }
  .table {
    width: 100%;
    border-collapse: collapse;
    /* Fixed layout so column widths come from the <colgroup> (driven by
       the per-tool layout store) instead of content. min-width on the
       table (set inline) lets .table-wrap scroll horizontally once the
       columns outgrow the panel. */
    table-layout: fixed;
  }
  .table col.col-sel { width: 32px; }
  /* Wide enough for the visible "Actions" header (uppercased) plus the
     row's action buttons. */
  .table col.col-actions { width: 84px; }
  thead th {
    position: relative;
    text-align: left;
    padding: var(--space-2) var(--space-3);
    background: var(--surface-2);
    color: var(--text-2);
    font-weight: var(--weight-semibold);
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    border-bottom: 1px solid var(--border-subtle);
    white-space: nowrap;
  }
  thead th.sel { text-align: center; padding: var(--space-2); }
  thead th.size { text-align: right; }
  thead th.actions { text-align: center; }
  /* The resize handle straddles the right border of a header cell. */
  .col-resize {
    position: absolute;
    top: 0;
    right: -5px;
    height: 100%;
    display: flex;
    z-index: 2;
  }
  /* The Size column's sort button is right-aligned, so its handle would
     overlap the label; nudge the button clear of the grab area. */
  thead th.size .th-button { margin-right: var(--space-1); }
  .th-button {
    background: none;
    border: none;
    color: inherit;
    cursor: pointer;
    font: inherit;
    padding: 0;
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
  }
  .th-button:hover { color: var(--text-1); }
  .th-button :global(.sort-i) { flex-shrink: 0; }
  .th-button :global(.sort-i.muted) { opacity: 0.4; }

  .footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    padding: var(--space-1);
  }
  .muted { color: var(--text-3); font-size: var(--text-sm); }
  /* Keeps the row count and the rows-per-page picker together on the right,
     so the footer reads pager | … | "50 shown  Rows [50]". */
  .footer-end {
    display: inline-flex;
    align-items: center;
    gap: var(--space-3);
    margin-left: auto;
  }
  .page-size {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    padding: 0 var(--space-2);
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
  }
  .page-size select {
    background: none;
    border: none;
    color: var(--text-1);
    font-size: var(--text-sm);
    padding: 4px 0;
    cursor: pointer;
  }
  .page-size select:focus-visible { outline: none; }
  .page-size:focus-within { border-color: var(--accent); box-shadow: var(--focus-ring); }

  :global(.filelist .spin) { animation: spin 0.9s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
