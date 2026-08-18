<script>
  /**
   * RomM library view.
   *
   * Three tabs: browse and convert the catalog, configure per-platform
   * automation, and set up the connection. The Library tab is the ordinary
   * workspace — the backend returns a RomM platform's ROMs in the same
   * DirectoryListing/FileEntry shape a directory listing uses, so FileList,
   * FileRow, RowActionsMenu, ConvertPanel and JobsPanel are reused verbatim.
   */
  import { onMount } from 'svelte';
  import { toast } from 'svelte-sonner';
  import { romm } from '$lib/stores/romm.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { conversion } from '$lib/stores/conversion.svelte.js';
  import { layout } from '$lib/stores/layout.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import { ui } from '$lib/stores/ui.svelte.js';
  import { formatSize } from '$lib/api/format.js';
  import FileList from '$lib/components/panels/FileList.svelte';
  import ConvertPanel from '$lib/components/panels/ConvertPanel.svelte';
  import JobsPanel from '$lib/components/panels/JobsPanel.svelte';
  import RommSettings from '$lib/components/views/RommSettings.svelte';
  import RommAutomation from '$lib/components/views/RommAutomation.svelte';
  import Splitter from '$lib/components/ui/Splitter.svelte';
  import Select from '$lib/components/ui/Select.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import RefreshCw from '@lucide/svelte/icons/refresh-cw';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';

  let dragStartRight = 0;
  let tab = $state('library');

  const status = $derived(romm.status);
  const entries = $derived(fileBrowser.entries);

  /**
   * Every target format this platform can actually use.
   *
   * Selecting a platform points the workspace at *a* tool the platform allows,
   * but a platform usually allows several — PS2 takes chdman and maxcso — and
   * without this the only way to reach the others was to leave RomM for the
   * sidebar and come back. Narrowed by the backend's own `mode_ids`/`tool_ids`
   * for this platform, so the list can never offer a format for the wrong
   * console, and grouped by tool so the choice reads as "what do I want out".
   */
  const modeOptions = $derived.by(() => {
    const platform = romm.platforms.find((p) => p.id === romm.selectedPlatformId);
    const allowedTools = platform?.tool_ids;
    const allowedModes = platform?.mode_ids;
    const out = [];
    for (const tool of registry.all()) {
      if (Array.isArray(allowedTools) && !allowedTools.includes(tool.id)) continue;
      for (const m of tool.modes ?? []) {
        if (Array.isArray(allowedModes) && !allowedModes.includes(m.mode)) continue;
        out.push({ value: m.mode, label: `${tool.label} → ${m.label}` });
      }
    }
    return out;
  });

  /** Switch the workspace to a chosen target format, tool included.
   *
   * `setPrimaryTool` resets the mode to that tool's default, so it has to run
   * first — and it has to run at all, because the Convert panel and the
   * sidebar both key off the primary tool, not the mode.
   */
  function chooseMode(mode) {
    const tool = registry.toolForMode(mode);
    if (tool && tool.id !== conversion.primaryTool) {
      conversion.setPrimaryTool(tool.id);
      ui.workspaceTool = tool.id;
    }
    conversion.setMode(mode);
  }

  const selectedPlatformName = $derived(
    romm.platforms.find((p) => p.id === romm.selectedPlatformId)?.name
      ?? 'this platform',
  );

  const platformOptions = $derived(
    romm.platforms.map((p) => ({
      value: String(p.id),
      label: p.rom_count ? `${p.name} (${p.rom_count})` : p.name,
    })),
  );

  const activeMode = $derived(registry.specFor(conversion.mode) ?? null);
  const losesMatch = $derived(romm.losesDatMatch(activeMode));

  /**
   * Library health for the selected platform.
   *
   * `outputs` alone is not "converted": detect_output also reports an output a
   * job is still writing (exists=false, ready=false). Counting those as done
   * would shrink the pending total while the work is still queued, so only a
   * `ready` output counts.
   *
   * "To go" is what the panel could actually submit, asked through the same
   * `allowsInputEntry` gate that decides whether a row is selectable. The
   * row's `convertible_by` is a tool-level, mode-agnostic annotation and the
   * two disagree by design — a PS2 ISO is annotated convertible by CHDMAN, but
   * CHDMAN's extract modes take a `.chd` and cannot consume it — so counting
   * from the annotation promised savings on files the button would refuse.
   *
   * The savings figure uses the mode's expected output/input ratio — the same
   * SIZE_RATIOS table the progress bar estimates from — and is labelled as an
   * estimate, because real ratios vary per title.
   */
  const summary = $derived.by(() => {
    let converted = 0;
    let pending = 0;
    let pendingBytes = 0;
    let convertedBytes = 0;
    // Judge each ROM against the target the panel is actually configured to
    // produce. Counting any ready output from any tool called a PS2 ISO
    // "converted" because a CSO sat beside it, even with CHD selected — so the
    // count and the savings estimate described a different conversion than the
    // one the button would run.
    //
    // The tool alone is still too coarse: dolphin-tool emits RVZ, WIA and GCZ,
    // so a stray .gcz beside a GameCube ISO reported it converted while RVZ was
    // selected. Match the mode's own extension when the mode declares one, and
    // fall back to the tool for the modes that do not (z3ds and nsz derive
    // their suffix from the input).
    const activeToolId = conversion.currentTool?.id ?? null;
    const activeExt = (activeMode?.outputExt ?? null)?.toLowerCase() ?? null;
    const forActiveTool = (o) => {
      if (activeToolId && o.tool_id !== activeToolId) return false;
      if (!activeExt) return true;
      return (o.path ?? '').toLowerCase().endsWith(activeExt);
    };
    for (const e of entries) {
      const outs = (e.outputs ?? []).filter(forActiveTool);
      const done = outs.some((o) => o.ready);
      // Any non-ready status means work is under way. A detector returns None
      // — and so contributes no status at all — when there is nothing there,
      // so the only way to see one is for a job to hold the destination. It
      // reports exists=false until the tool creates the file, and requiring
      // `exists` counted that whole window as still to do.
      const working = outs.some((o) => !o.ready);
      if (done) {
        converted += 1;
        convertedBytes += e.size ?? 0;
      } else if (!working && conversion.allowsInputEntry(e)) {
        pending += 1;
        pendingBytes += e.size ?? 0;
      }
    }
    const ratio = romm.sizeRatioFor(conversion.mode);
    const estimatedSaving = ratio !== null ? Math.max(0, pendingBytes * (1 - ratio)) : null;
    return {
      converted, pending, pendingBytes, convertedBytes,
      total: entries.length, estimatedSaving,
    };
  });

  onMount(() => {
    // The async init below outlives a fast navigation away, and its tail
    // calls enterRomm() — which would replace the ordinary workspace's
    // listing with RomM rows after this view is gone.
    let alive = true;
    (async () => {
      await Promise.all([romm.loadStatus(), romm.loadSettings()]);
      if (!alive) return;
      if (!romm.configured) {
        tab = 'settings';
        return;
      }
      // Only touch the library once RomM answers AND its folder is visible
      // here: listing ROMs stats every file, and doing that against a mount
      // that is missing or unresponsive would tie up a worker for nothing.
      if (!romm.usable) return;
      // allSettled: each store records its own error, and one failing must
      // not skip the other load or the re-pin pass below.
      if (!alive) return;
      await Promise.allSettled([romm.loadPlatforms(), romm.loadRules()]);
      if (!alive) return;
      if (romm.pendingRepins > 0 && romm.settings?.repin_on_load !== false) {
        try {
          await romm.runRepin();
        } catch {
          // Surfaced by the pending badge; a failed settle is retried later.
        }
      }
    })();
    // Leaving the view returns the browser to ordinary filesystem listing, or
    // the workspace would open showing RomM rows under a directory heading.
    return () => {
      alive = false;
      fileBrowser.exitRomm();
    };
  });

  /** Refresh what the new connection settings changed. Errors land in the store.
   *
   * Rules only: `saveSettings` already force-reloads the platforms and
   * re-enters the selected one, which fetches its whole catalog and stats
   * every file in it. Reloading here as well meant even a metadata toggle
   * cost two remote catalog reads and two filesystem scans of a large
   * library. The store owns the platform reload; this owns what it does not.
   */
  async function reloadAfterSave() {
    await romm.loadRules().catch(() => {});
  }

  async function handleRepin() {
    try {
      const result = await romm.runRepin();
      const done = result?.repinned ?? 0;
      if (done > 0) {
        toast.success(`Re-matched ${done} ROM${done === 1 ? '' : 's'} in RomM`);
      } else if ((result?.waiting ?? 0) > 0) {
        toast.info('RomM has not rescanned these yet — try again after a scan');
      } else {
        toast.info('Nothing waiting to be re-matched');
      }
    } catch (e) {
      toast.error(e?.message ?? 'Re-match failed');
    }
  }
</script>

<section class="view" aria-labelledby="romm-title">
  <header class="header">
    <div>
      <h1 id="romm-title">RomM library</h1>
      <p class="hint">
        Convert your RomM library in place. Game names and platforms come from
        RomM; the files are read from the mounted library.
      </p>
    </div>
    <div class="header-right">
      {#if status?.version}<Badge tone="info">RomM {status.version}</Badge>{/if}
      {#if romm.pendingRepins > 0}
        <Badge tone="warning">{romm.pendingRepins} awaiting re-match</Badge>
        <Button
          size="sm" variant="secondary" loading={romm.repinning}
          onclick={handleRepin} icon={refreshIcon}
        >Re-match in RomM</Button>
      {/if}
    </div>
  </header>

  <!-- A real tab set, not a nav: `aria-current="page"` marks the current item
       among navigation links, which does not tell a screen-reader user that
       these three form one group with one selected. Keyboard access already
       works (they are buttons), so this is semantics only. -->
  <div class="tabs" role="tablist" aria-label="RomM sections">
    {#each [['library', 'Library'], ['automation', 'Automation'], ['settings', 'Settings']] as [id, label] (id)}
      <button
        type="button" class="tab" class:active={tab === id}
        role="tab"
        id={`romm-tab-${id}`}
        aria-selected={tab === id}
        aria-controls={`romm-panel-${id}`}
        onclick={() => (tab = id)}
      >{label}</button>
    {/each}
  </div>

  <div
    role="tabpanel"
    id={`romm-panel-${tab}`}
    aria-labelledby={`romm-tab-${tab}`}
    class="panel"
  >
  {#if tab === 'settings'}
    <RommSettings onsaved={() => { if (romm.usable) { reloadAfterSave(); } }} />
  {:else if tab === 'automation'}
    <RommAutomation />
  {:else if romm.statusLoading}
    <div class="notice"><Spinner /> Checking RomM connection…</div>
  {:else if !romm.configured}
    <EmptyState
      title="RomM is not connected yet"
      description="Add your RomM URL, API token and library path in Settings — it takes about a minute."
    />
  {:else if !romm.connected}
    <div class="notice error">
      <TriangleAlert size={16} />
      <span>Could not reach RomM{status?.error ? `: ${status.error}` : '.'}</span>
    </div>
  {:else if status && !status.libraryRootMounted}
    <div class="notice error">
      <TriangleAlert size={16} />
      <span>
        The library path ({status.libraryRoot || 'unset'}) is not a directory
        here. Mount RomM's library folder into this container and check the path
        in Settings.
      </span>
    </div>
  {:else}
    <div class="toolbar">
      <div class="picker">
        {#if romm.platformsLoading}
          <Spinner />
        {:else if romm.platformsError}
          <!-- The heartbeat is unauthenticated, so a bad token surfaces here
               rather than as a connection failure. Saying "no platforms" would
               make a broken token look like an empty library. -->
          <span class="error-text">{romm.platformsError}</span>
        {:else if platformOptions.length > 0}
          <Select
            label="Platform"
            value={String(romm.selectedPlatformId ?? '')}
            options={platformOptions}
            onchange={(v) => romm.selectPlatform(v)}
          />
        {:else}
          <span class="muted">No platforms in RomM.</span>
        {/if}
        {#if modeOptions.length > 0}
          <Select
            label="Convert to"
            value={conversion.mode ?? ''}
            options={modeOptions}
            onchange={(v) => chooseMode(v)}
          />
        {/if}
      </div>

      {#if summary.total > 0}
        <div class="summary" aria-live="polite">
          <span><strong>{summary.converted}</strong> converted</span>
          <span><strong>{summary.pending}</strong> to go</span>
          {#if summary.pendingBytes > 0}
            <span class="muted">({formatSize(summary.pendingBytes)} of sources)</span>
          {/if}
          {#if summary.estimatedSaving}
            <span class="saving">
              ≈ {formatSize(summary.estimatedSaving)} could be saved
            </span>
          {/if}
        </div>
      {/if}
    </div>

    {#if losesMatch}
      <div class="notice warn">
        <TriangleAlert size={16} />
        <span>
          RomM cannot hash-match <strong>{activeMode?.outputExt}</strong> files.
          Their metadata is saved before converting and restored by
          <em>Re-match in RomM</em> once RomM rescans. CHD and ZIP/7z keep their
          match automatically.
        </span>
      </div>
    {/if}

    <div class="grid" style="--ws-right: {layout.panels.right}px;">
      <article class="main">
        {#if fileBrowser.entriesError}
          <div class="notice error">
            <TriangleAlert size={16} />
            <span>{fileBrowser.entriesError}</span>
          </div>
        {:else if !fileBrowser.loading && entries.length === 0 && romm.selectedPlatformId !== null}
          <EmptyState
            title="No matching ROMs on this volume"
            description="RomM lists ROMs for this platform, but none of them resolve to a file inside a configured Compressatorium volume."
          />
        {:else}
          <FileList />
        {/if}
      </article>

      <Splitter
        variant="panel"
        label="Resize convert and jobs panel"
        value={layout.panels.right}
        min={280}
        max={640}
        onstart={() => (dragStartRight = layout.panels.right)}
        onmove={(d) => layout.setPanelWidth('right', dragStartRight - d)}
        onstep={(d) => layout.setPanelWidth('right', layout.panels.right - d)}
        onreset={() => layout.resetPanel('right')}
      />

      <article class="right">
        {#if romm.selectedPlatformUnsupported || modeOptions.length === 0}
          <!-- Nothing installed converts this platform. The panel would still
               submit against whatever tool the workspace was last left on —
               a PS2 ISO to Dolphin RVZ — so it is replaced rather than
               disabled, and the reason is named. -->
          <div class="notice warn">
            <TriangleAlert size={16} />
            <span>
              No installed tool converts <strong>{selectedPlatformName}</strong>.
              Install the tool for this system, or pick another platform.
            </span>
          </div>
        {:else}
          <ConvertPanel />
        {/if}
        <div class="separator" aria-hidden="true"></div>
        <JobsPanel />
      </article>
    </div>
  {/if}
  </div>
</section>

{#snippet refreshIcon()}<RefreshCw size={14} />{/snippet}

<style>
  /* The tab panel is a passthrough wrapper: it exists for the ARIA
     relationship, so it must not introduce a layout box of its own. */
  .panel {
    display: contents;
  }

  .view {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    padding: var(--space-5);
    /* Matches WorkArea: the file table needs the room on a big monitor. */
    max-width: 1760px;
    margin: 0 auto;
    width: 100%;
    min-width: 0;
  }
  .header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-3);
    flex-wrap: wrap;
  }
  .header h1 {
    margin: 0;
    font-size: var(--text-2xl);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
  }
  .header-right { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
  .hint { color: var(--text-2); margin-top: var(--space-1); }

  .tabs {
    display: flex;
    gap: var(--space-1);
    border-bottom: 1px solid var(--border-subtle);
  }
  .tab {
    padding: var(--space-2) var(--space-3);
    background: none;
    border: 0;
    border-bottom: 2px solid transparent;
    color: var(--text-2);
    font: inherit;
    font-weight: var(--weight-medium);
    cursor: pointer;
  }
  .tab.active { color: var(--text-1); border-bottom-color: var(--accent); }

  .toolbar {
    display: flex;
    align-items: flex-end;
    gap: var(--space-4);
    flex-wrap: wrap;
  }
  .picker { min-width: 240px; }
  .summary {
    display: flex;
    gap: var(--space-3);
    align-items: baseline;
    flex-wrap: wrap;
    color: var(--text-2);
  }
  .summary strong { color: var(--text-1); }
  .saving { color: var(--accent); font-weight: var(--weight-medium); }
  .muted { color: var(--text-2); }
  .error-text { color: var(--danger-text, var(--text-1)); }

  .notice {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-3);
    border-radius: var(--radius-md);
    border: 1px solid var(--border-subtle);
    background: var(--surface-2);
    color: var(--text-2);
  }
  .notice.error {
    border-color: var(--danger-border, var(--border-subtle));
    color: var(--danger-text, var(--text-1));
  }
  .notice.warn {
    border-color: var(--warning-border, var(--border-subtle));
    color: var(--warning-text, var(--text-1));
  }

  .grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--space-4);
    min-width: 0;
  }
  .grid > :global(.splitter) { display: none; }
  @media (min-width: 900px) {
    /* Two-column mode stacks the convert/jobs panel full-width below the file
       list; without the span it auto-places into a narrow column. */
    .right { grid-column: 1 / -1; }
  }
  @media (min-width: 1280px) {
    .grid {
      grid-template-columns: minmax(0, 1fr) auto var(--ws-right, 360px);
      gap: var(--space-2);
    }
    .grid > :global(.splitter) { display: block; }
    .right { grid-column: auto; }
  }

  .main, .right {
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
    box-shadow: var(--elev-1);
    min-width: 0;
  }
  .right { display: flex; flex-direction: column; gap: var(--space-4); }
  .separator {
    height: 1px;
    background: var(--border-subtle);
    margin: var(--space-2) 0;
  }
</style>
