<script>
  import { onMount } from 'svelte';
  import { datMatching } from '$lib/stores/datMatching.svelte.js';
  import { toast } from 'svelte-sonner';
  import Button from '$lib/components/ui/Button.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import ProgressBar from '$lib/components/ui/ProgressBar.svelte';
  import IconButton from '$lib/components/ui/IconButton.svelte';
  import Upload from '@lucide/svelte/icons/upload';
  import Download from '@lucide/svelte/icons/download';
  import Trash2 from '@lucide/svelte/icons/trash-2';
  import RefreshCw from '@lucide/svelte/icons/refresh-cw';
  import XIcon from '@lucide/svelte/icons/x';
  import Globe from '@lucide/svelte/icons/globe';
  import PlugZap from '@lucide/svelte/icons/plug-zap';

  const dats = $derived(datMatching.dats);
  const loading = $derived(datMatching.datsLoading);
  const error = $derived(datMatching.datsError);
  const stats = $derived(datMatching.stats);
  const importing = $derived(datMatching.importingDat);
  const syncing = $derived(datMatching.syncing);
  const syncStatus = $derived(datMatching.syncStatus);

  const hasheousOn = $derived(Boolean(stats?.hasheous_enabled));
  // Interpolated straight into prose, so it needs a value before /dat/stats
  // lands -- otherwise the panel reads "looking up misses at ." while loading.
  const hasheousUrl = $derived(stats?.hasheous_url || 'the configured Hasheous server');

  let fileInputEl;
  let syncPollTimer = null;
  let savingHasheous = $state(false);
  let testingHasheous = $state(false);
  let hasheousTest = $state(null);

  async function handleToggleHasheous() {
    const next = !hasheousOn;
    savingHasheous = true;
    hasheousTest = null;
    try {
      await datMatching.setHasheousEnabled(next);
      toast.success(
        next
          ? 'Hasheous fallback on — unmatched files will be looked up remotely'
          : 'Hasheous fallback off — nothing is sent anywhere',
      );
    } catch (e) {
      toast.error(e?.message ?? 'Could not change the Hasheous setting');
    } finally {
      savingHasheous = false;
    }
  }

  async function handleTestHasheous() {
    testingHasheous = true;
    try {
      hasheousTest = await datMatching.testHasheous();
    } catch (e) {
      hasheousTest = { ok: false, error: e?.message ?? 'Request failed' };
    } finally {
      testingHasheous = false;
    }
  }

  onMount(() => {
    datMatching.loadDATs();
    // Poll sync status whenever syncing flips on; an $effect handles
    // restart, this onMount just primes the initial status.
    datMatching.pollSyncStatus();
    return () => {
      if (syncPollTimer) {
        clearInterval(syncPollTimer);
        syncPollTimer = null;
      }
    };
  });

  $effect(() => {
    if (syncing && !syncPollTimer) {
      syncPollTimer = setInterval(() => {
        datMatching.pollSyncStatus();
      }, 2000);
    } else if (!syncing && syncPollTimer) {
      clearInterval(syncPollTimer);
      syncPollTimer = null;
    }
  });

  async function handleImport(event) {
    const file = event.currentTarget?.files?.[0];
    if (!file) return;
    try {
      const result = await datMatching.importDAT(file);
      const added = result?.imported ?? result?.added ?? 1;
      toast.success(`Imported DAT (${added} entr${added === 1 ? 'y' : 'ies'})`);
    } catch (e) {
      toast.error(e?.message ?? 'DAT import failed');
    } finally {
      // Reset so the same file can be re-selected later.
      if (fileInputEl) fileInputEl.value = '';
    }
  }

  async function handleSync() {
    try {
      await datMatching.syncMAMERedump();
      toast.info('MAMERedump sync started');
    } catch (e) {
      toast.error(e?.message ?? 'Failed to start sync');
    }
  }

  async function handleCancelSync() {
    try {
      await datMatching.cancelSync();
      toast.info('Sync cancellation requested');
    } catch (e) {
      toast.error(e?.message ?? 'Failed to cancel sync');
    }
  }

  async function handleDeleteDat(dat) {
    if (!dat?.id) return;
    if (!window.confirm(`Delete DAT "${dat.name ?? dat.id}"? This removes its match data.`)) return;
    try {
      await datMatching.deleteDAT(dat.id);
      toast.success(`Deleted DAT: ${dat.name ?? dat.id}`);
    } catch (e) {
      toast.error(e?.message ?? 'Failed to delete DAT');
    }
  }

  // Sync status fields come from `_progress` on the backend (see
  // app/services/dat_sync.py): `{ status, files_total, files_imported,
  // current_file, file_index, error }`. Percent is files_imported /
  // files_total, there is no top-level numeric progress field.
  const syncProgress = $derived(syncStatus?.progress ?? null);
  const syncPercent = $derived.by(() => {
    const total = syncProgress?.files_total ?? 0;
    const done = syncProgress?.files_imported ?? 0;
    if (total <= 0) return null;
    return Math.round((done / total) * 100);
  });
  const syncPhase = $derived(syncProgress?.status ?? 'starting');
  const syncCurrentFile = $derived(syncProgress?.current_file ?? '');
</script>

<section class="view" aria-labelledby="dat-title">
  <header class="header">
    <div>
      <h1 id="dat-title">DAT Library</h1>
      <p class="hint">Match converted files against No-Intro / Redump / MAMERedump datasets.</p>
    </div>
    <div class="header-actions">
      <input
        type="file"
        accept=".dat,.xml"
        class="sr-only"
        bind:this={fileInputEl}
        onchange={handleImport}
        aria-label="Import DAT file"
      />
      <Button
        variant="secondary"
        disabled={importing}
        loading={importing}
        onclick={() => fileInputEl?.click()}
      >
        {#snippet icon()}<Upload size={14} />{/snippet}
        Import DAT
      </Button>
      {#if syncing}
        <Button variant="destructive" onclick={handleCancelSync}>
          {#snippet icon()}<XIcon size={14} />{/snippet}
          Cancel sync
        </Button>
      {:else}
        <Button variant="primary" onclick={handleSync}>
          {#snippet icon()}<Download size={14} />{/snippet}
          Sync MAMERedump
        </Button>
      {/if}
    </div>
  </header>

  <!-- Hasheous state rides on /api/dat/stats (app/routes/dat.py:get_dat_stats)
       rather than a second endpoint, since this view already fetches it. -->
  <article class="panel hasheous" class:is-on={hasheousOn}>
    <div class="hasheous-head">
      <Globe size={16} aria-hidden="true" />
      <div class="hasheous-title">
        <strong>Hasheous fallback</strong>
        <span class="hasheous-sub">
          Match anything your DATs don't recognise against 14 preservation
          databases — Redump, No-Intro, TOSEC, MAMERedump, RetroAchievements
          and more. No account or API key needed.
        </span>
      </div>
      <div class="hasheous-actions">
        {#if hasheousOn}
          <Button
            variant="secondary"
            size="sm"
            loading={testingHasheous}
            disabled={testingHasheous}
            onclick={handleTestHasheous}
          >
            {#snippet icon()}<PlugZap size={14} />{/snippet}
            Test
          </Button>
        {/if}
        <Button
          variant={hasheousOn ? 'destructive' : 'primary'}
          size="sm"
          loading={savingHasheous}
          disabled={savingHasheous}
          onclick={handleToggleHasheous}
        >
          {hasheousOn ? 'Turn off' : 'Turn on'}
        </Button>
      </div>
    </div>

    <p class="hasheous-detail">
      {#if hasheousOn}
        On — looking up misses at <code>{hasheousUrl}</code>. Your own
        DATs are always checked first, so a library they already cover makes no
        network calls. These matches show a <strong>HASH</strong> badge, with
        platform, year, region and source DAT in the tooltip.
      {:else}
        Off — nothing is sent anywhere. Turning it on sends each file's SHA1
        (and nothing else, no names or paths) to
        <code>{hasheousUrl}</code> when your DATs come up empty.
        Takes effect immediately; no restart needed.
      {/if}
      {#if stats?.hasheous_overridden}
        <span class="hasheous-note">
          Set here, overriding <code>COMPRESSATORIUM_HASHEOUS_ENABLED</code>.
        </span>
      {/if}
    </p>

    {#if hasheousTest}
      <p class="hasheous-test" class:bad={!hasheousTest.ok} role="status">
        {#if hasheousTest.ok}
          Reachable{hasheousTest.latency_ms != null
            ? ` — responded in ${hasheousTest.latency_ms} ms`
            : ''}.
        {:else}
          Not reachable: {hasheousTest.error}
        {/if}
      </p>
    {/if}
  </article>

  <!-- Stats response shape (app/services/dat_store.py:get_stats):
       total_dats, total_sha1_hashes, total_md5_hashes,
       total_matches, total_unmatched. -->
  <article class="panel stats">
    <div class="stat">
      <span class="stat-label">DATs</span>
      <span class="stat-value">{stats?.total_dats ?? 0}</span>
    </div>
    <div class="stat">
      <span class="stat-label">SHA1 hashes</span>
      <span class="stat-value">{stats?.total_sha1_hashes ?? 0}</span>
    </div>
    <div class="stat">
      <span class="stat-label">MD5 hashes</span>
      <span class="stat-value">{stats?.total_md5_hashes ?? 0}</span>
    </div>
    <div class="stat">
      <span class="stat-label">Matched</span>
      <span class="stat-value">{stats?.total_matches ?? 0}</span>
    </div>
    <div class="stat">
      <span class="stat-label">Unmatched</span>
      <span class="stat-value">{stats?.total_unmatched ?? 0}</span>
    </div>
  </article>

  {#if syncing}
    <article class="panel sync-status" role="status" aria-live="polite">
      <div class="sync-header">
        <strong>Syncing MAMERedump…</strong>
        <Badge tone="info">{syncPhase}</Badge>
        {#if syncProgress?.files_total}
          <span class="sync-counter">
            {syncProgress.files_imported ?? 0} / {syncProgress.files_total}
          </span>
        {/if}
      </div>
      <ProgressBar value={syncPercent} size="sm" />
      {#if syncCurrentFile}
        <div class="sync-msg">Importing {syncCurrentFile}</div>
      {:else if syncProgress?.error}
        <div class="sync-msg">{syncProgress.error}</div>
      {/if}
    </article>
  {/if}

  <article class="panel">
    <header class="panel-head">
      <h2 class="panel-title">Imported DATs</h2>
      <IconButton label="Refresh" size="sm" onclick={() => datMatching.loadDATs()}>
        <RefreshCw size={14} />
      </IconButton>
    </header>

    {#if loading}
      <div class="loading"><Spinner size="md" /> Loading…</div>
    {:else if error}
      <div class="error" role="alert">{error}</div>
    {:else if dats.length === 0}
      <EmptyState
        title="No DATs imported"
        description="Import a .dat / .xml file from No-Intro, Redump, or MAMERedump, or sync MAMERedump above."
        glyph="≣"
      />
    {:else}
      <ul class="dat-list">
        {#each dats as dat (dat.id ?? dat.name)}
          <li class="dat-row">
            <div class="dat-meta">
              <div class="dat-name">{dat.name ?? '(unnamed)'}</div>
              <div class="dat-sub">
                {#if dat.version}<span>v{dat.version}</span>{/if}
                {#if dat.file_count != null}<span>{dat.file_count} entries</span>{/if}
                {#if dat.description}<span class="dat-desc" title={dat.description}>{dat.description}</span>{/if}
              </div>
            </div>
            <IconButton label="Delete DAT" size="sm" onclick={() => handleDeleteDat(dat)}>
              <Trash2 size={14} />
            </IconButton>
          </li>
        {/each}
      </ul>
    {/if}
  </article>
</section>

<style>
  .view { display: flex; flex-direction: column; gap: var(--space-4); padding: var(--space-5); max-width: var(--container-max); margin: 0 auto; width: 100%; min-width: 0; }
  .header { display: flex; justify-content: space-between; align-items: flex-start; gap: var(--space-3); flex-wrap: wrap; }
  .header h1 { margin: 0; font-size: var(--text-2xl); font-weight: var(--weight-semibold); color: var(--text-1); }
  .hint { color: var(--text-2); margin-top: var(--space-1); }
  .header-actions { display: inline-flex; gap: var(--space-2); flex-wrap: wrap; }

  .panel { background: var(--surface-1); border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); padding: var(--space-4); box-shadow: var(--elev-1); }
  .panel-head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-2); margin-bottom: var(--space-3); }
  .panel-title { margin: 0; font-size: var(--text-base); font-weight: var(--weight-semibold); color: var(--text-1); text-transform: uppercase; letter-spacing: 0.05em; }

  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: var(--space-3); }
  .hasheous { display: flex; flex-direction: column; gap: var(--space-2); font-size: var(--text-sm); color: var(--text-2); line-height: 1.5; }
  .hasheous-head { display: flex; align-items: flex-start; gap: var(--space-3); }
  .hasheous-head > :global(svg) { flex: none; margin-top: 0.15em; color: var(--text-3); }
  .hasheous.is-on .hasheous-head > :global(svg) { color: var(--accent); }
  .hasheous-title { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1; }
  .hasheous-title strong { color: var(--text-1); }
  .hasheous-sub { color: var(--text-2); }
  .hasheous-actions { display: flex; gap: var(--space-2); flex: none; }
  .hasheous-detail { margin: 0; }
  .hasheous-note { display: block; margin-top: var(--space-1); color: var(--text-3); font-size: var(--text-xs); }
  .hasheous-test { margin: 0; padding: var(--space-2) var(--space-3); border-radius: var(--radius-sm); background: var(--surface-2); color: var(--text-1); }
  .hasheous-test.bad { color: var(--error); background: var(--error-muted); }
  .hasheous code { font-family: var(--font-mono); font-size: 0.92em; padding: 0.05em 0.35em; border-radius: var(--radius-sm); background: var(--surface-2); color: var(--text-1); }
  @media (max-width: 620px) {
    .hasheous-head { flex-wrap: wrap; }
    .hasheous-actions { width: 100%; }
  }
  .stat { display: flex; flex-direction: column; gap: 2px; }
  .stat-label { color: var(--text-3); font-size: var(--text-xs); text-transform: uppercase; letter-spacing: 0.05em; }
  .stat-value { color: var(--text-1); font-size: var(--text-xl); font-weight: var(--weight-semibold); font-variant-numeric: tabular-nums; }

  .sync-status { display: flex; flex-direction: column; gap: var(--space-2); }
  .sync-header { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
  .sync-counter { color: var(--text-3); font-size: var(--text-xs); font-variant-numeric: tabular-nums; }
  .sync-msg { color: var(--text-3); font-size: var(--text-xs); font-family: var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .dat-list { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: var(--space-1); }
  .dat-row { display: flex; align-items: center; gap: var(--space-2); padding: var(--space-2); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--surface-2); }
  .dat-meta { flex: 1; min-width: 0; }
  .dat-name { color: var(--text-1); font-size: var(--text-sm); font-weight: var(--weight-medium); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dat-sub { color: var(--text-3); font-size: var(--text-xs); display: flex; gap: var(--space-2); flex-wrap: wrap; }
  .dat-desc { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 320px; }

  .loading { display: flex; align-items: center; gap: var(--space-2); color: var(--text-2); font-size: var(--text-sm); }
  .error { color: var(--error); background: var(--error-muted); padding: var(--space-2) var(--space-3); border-radius: var(--radius-sm); font-size: var(--text-sm); }

  .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
</style>
