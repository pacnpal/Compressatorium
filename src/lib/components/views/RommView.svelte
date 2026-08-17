<script>
  /**
   * RomM catalog view.
   *
   * A platform picker in place of the volumes rail, and then the ordinary
   * workspace: the backend returns a RomM platform's ROMs as the same
   * DirectoryListing/FileEntry shape a directory listing uses, so FileList,
   * FileRow, RowActionsMenu, ConvertPanel and JobsPanel are reused verbatim and
   * conversions go through the normal batch endpoint.
   */
  import { onMount } from 'svelte';
  import { toast } from 'svelte-sonner';
  import { romm } from '$lib/stores/romm.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { conversion } from '$lib/stores/conversion.svelte.js';
  import { layout } from '$lib/stores/layout.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import FileList from '$lib/components/panels/FileList.svelte';
  import ConvertPanel from '$lib/components/panels/ConvertPanel.svelte';
  import JobsPanel from '$lib/components/panels/JobsPanel.svelte';
  import Splitter from '$lib/components/ui/Splitter.svelte';
  import Select from '$lib/components/ui/Select.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import RefreshCw from '@lucide/svelte/icons/refresh-cw';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';

  let dragStartRight = 0;

  const status = $derived(romm.status);
  const platforms = $derived(romm.platforms);
  const entries = $derived(fileBrowser.entries);

  const platformOptions = $derived(
    platforms.map((p) => ({
      value: String(p.id),
      label: p.rom_count ? `${p.name} (${p.rom_count})` : p.name,
    })),
  );

  // The mode currently selected in ConvertPanel, so the DAT-match notice
  // tracks what the user is actually about to run.
  const activeMode = $derived(registry.specFor(conversion.mode) ?? null);
  const losesMatch = $derived(romm.losesDatMatch(activeMode));

  // Savings report, derived rather than fetched: RomM gives the sizes and the
  // registry-driven `outputs` already says what is converted.
  const summary = $derived.by(() => {
    let converted = 0;
    let pending = 0;
    let pendingBytes = 0;
    for (const e of entries) {
      if (e.outputs?.length) converted += 1;
      else if (e.convertible_by?.length) {
        pending += 1;
        pendingBytes += e.size ?? 0;
      }
    }
    return { converted, pending, pendingBytes, total: entries.length };
  });

  function formatBytes(n) {
    if (!n) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let v = n;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
  }

  onMount(() => {
    (async () => {
      await romm.loadStatus();
      if (!romm.configured) return;
      await romm.loadPlatforms();
      // Settle anything RomM has rescanned since we last looked. Idempotent
      // server-side, so calling it on every mount is free.
      if (romm.pendingRepins > 0) {
        try {
          await romm.runRepin();
        } catch {
          // Surfaced by the pending badge; a failed settle is retried later.
        }
      }
    })();
    // Leaving the view returns the browser to ordinary filesystem listing, or
    // the workspace would open showing RomM rows under a directory heading.
    return () => fileBrowser.exitRomm();
  });

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
    {#if status?.version}
      <Badge tone="info">RomM {status.version}</Badge>
    {/if}
  </header>

  {#if romm.statusLoading}
    <div class="notice"><Spinner /> Checking RomM connection…</div>
  {:else if !romm.configured}
    <EmptyState
      title="RomM is not configured"
      description="Set ROMM_URL to your RomM instance and ROMM_LIBRARY_ROOT to the local mount of its library folder, then restart."
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
        ROMM_LIBRARY_ROOT ({status.libraryRoot || 'unset'}) is not a directory
        here. Mount RomM's library folder into this container.
      </span>
    </div>
  {:else}
    <div class="toolbar">
      <div class="picker">
        {#if romm.platformsLoading}
          <Spinner />
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
      </div>

      {#if summary.total > 0}
        <div class="summary" aria-live="polite">
          <span><strong>{summary.converted}</strong> converted</span>
          <span><strong>{summary.pending}</strong> not yet converted</span>
          {#if summary.pendingBytes > 0}
            <span class="muted">({formatBytes(summary.pendingBytes)} of source files)</span>
          {/if}
        </div>
      {/if}

      {#if romm.pendingRepins > 0}
        <div class="repin">
          <Badge tone="warning">{romm.pendingRepins} awaiting re-match</Badge>
          <Button
            size="sm"
            variant="secondary"
            loading={romm.repinning}
            onclick={handleRepin}
            icon={refreshIcon}
          >
            Re-match in RomM
          </Button>
        </div>
      {/if}
    </div>

    {#if losesMatch}
      <div class="notice warn">
        <TriangleAlert size={16} />
        <span>
          RomM cannot hash-match <strong>{activeMode?.outputExt}</strong> files, so
          converted ROMs show as unidentified until RomM rescans. Their metadata
          is saved first and re-applied by <em>Re-match in RomM</em>.
          CHD and ZIP/7z keep their match automatically.
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
        <ConvertPanel />
        <div class="separator" aria-hidden="true"></div>
        <JobsPanel />
      </article>
    </div>
  {/if}
</section>

{#snippet refreshIcon()}<RefreshCw size={14} />{/snippet}

<style>
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
  .hint {
    color: var(--text-2);
    margin-top: var(--space-1);
  }

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
  .repin {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin-left: auto;
  }
  .muted { color: var(--text-2); }

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
