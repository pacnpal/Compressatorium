<script>
  import { onMount, tick } from 'svelte';
  import { ui } from '$lib/stores/ui.svelte.js';
  import { startRouter } from '$lib/router/hashRouter.js';
  import Sidebar from '$lib/components/layout/Sidebar.svelte';
  import MobileDrawer from '$lib/components/layout/MobileDrawer.svelte';
  import TopBar from '$lib/components/layout/TopBar.svelte';
  import Dashboard from '$lib/components/views/Dashboard.svelte';
  import WorkArea from '$lib/components/views/WorkArea.svelte';
  import DATView from '$lib/components/views/DATView.svelte';
  import HelpView from '$lib/components/views/HelpView.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { jobToasts } from '$lib/stores/jobToasts.svelte.js';
  import { conversion } from '$lib/stores/conversion.svelte.js';
  import { api } from '$lib/api/endpoints.js';
  import { verification } from '$lib/stores/verification.svelte.js';
  import { datMatching } from '$lib/stores/datMatching.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { ModeWatcher, mode } from 'mode-watcher';
  import { Toaster, toast } from 'svelte-sonner';
  import { STORAGE_KEYS } from '$lib/util/localStorage.js';
  import BulkVerifyModal from '$lib/components/modals/BulkVerifyModal.svelte';
  import BulkDeleteModal from '$lib/components/modals/BulkDeleteModal.svelte';
  import DeleteModal from '$lib/components/modals/DeleteModal.svelte';
  import RenameModal from '$lib/components/modals/RenameModal.svelte';
  import FileInfoModal from '$lib/components/modals/FileInfoModal.svelte';
  import CancelAllJobsModal from '$lib/components/modals/CancelAllJobsModal.svelte';
  import ClearDoneModal from '$lib/components/modals/ClearDoneModal.svelte';

  let mainEl;

  $effect(() => {
    ui.focusBump;
    if (!mainEl) return;
    tick().then(() => {
      try {
        mainEl.focus({ preventScroll: false });
      } catch (_e) {
        // ignore, focus may fail on disconnected nodes mid-route swap
      }
    });
  });

  $effect(() => {
    conversion.setPrimaryTool(ui.workspaceTool);
  });

  // Refresh the file listing when terminal job events arrive, newly
  // written outputs need to appear, delete-on-verify removals need to
  // disappear, sibling badges flip.
  //
  // Refresh interferes with whatever the user was doing if we don't
  // gate it carefully. Skip while any of the following hold; the
  // counter advances only after refresh actually fires so the gates
  // clearing (modal closes, batch finishes, etc.) immediately re-runs
  // the effect and lets the deferred refresh through.
  //   - User toggled auto-refresh off explicitly.
  //   - Any modal that reads file-listing state is open.
  //   - A conversion submit is mid-flight.
  //   - A verify batch is running.
  //   - fileBrowser is already loading.
  //   - We're in search mode (refresh() short-circuits anyway).
  let _lastTerminal = $state(0);
  $effect(() => {
    const total = jobs.completedCount + jobs.failedCount + jobs.cancelledCount;
    // The count can drop when the user clears terminal history
    // (jobs.clearCompleted). Track the watermark down too, otherwise a
    // post-clear conversion whose new total is still below the old
    // high-water mark would never trigger a refresh.
    if (total < _lastTerminal) {
      _lastTerminal = total;
      return;
    }
    if (total <= _lastTerminal) return;
    if (!fileBrowser.autoRefresh) return;
    if (fileBrowser.loading) return;
    if (fileBrowser.searchMode) return;
    if (ui.anyEntryModalOpen) return;
    if (conversion.converting) return;
    if (verification.batchRun) return;
    _lastTerminal = total;
    fileBrowser.refresh().catch(() => {});
  });

  // Surface a live toast for every RUNNING job and resolve it on
  // completion. Iterating the list (and touching status/progress/message)
  // subscribes the effect at index granularity, so SSE progress frames,
  // which replace a job slot via index assignment in jobs._applyJob,
  // re-run the reconcile. jobToasts keys each toast by job id, so this is
  // idempotent across re-runs.
  $effect(() => {
    const list = jobs.jobs;
    const showExternalScan = jobs.showExternalScanJobs;
    for (const j of list) {
      void j?.status;
      void j?.progress;
      void j?.message;
    }
    jobToasts.reconcile(list, { showExternalScan });
  });

  function handleSkip(e) {
    e.preventDefault();
    mainEl?.focus({ preventScroll: false });
  }

  function onBoundaryError(err, _reset) {
    console.error('View boundary caught error:', err);
    toast.error('Something went wrong. Try again.', { duration: 6000 });
  }

  onMount(() => {
    ui.loadVersion();
    // Ask the backend which tools to show. Switch (nsz) is hidden until
    // prod.keys are configured. Fire and forget, on failure nothing is hidden.
    api.getTools()
      .then((t) => ui.applyToolAvailability(t?.available))
      .catch(() => {});
    // Load remembered per-tool compression settings from the server.
    conversion.loadServerPrefs();
    // Rehydrate the verified set + DAT-library state so OK / DAT badges
    // survive reloads. Fire and forget, failure leaves the cache empty.
    verification.loadVerified();
    datMatching.refreshMatchingAvailability();
    jobs.connect();
    const stopRouter = startRouter();
    return () => {
      stopRouter();
      jobs.dispose();
      jobToasts.dispose();
    };
  });
</script>

<!--
  mode-watcher manages the dark/light class on <html> + color-scheme. The
  matching FOUC-prevention inline script lives in index.html (runs
  synchronously before this component mounts), so we disable mode-watcher's
  own head-script injection to avoid double work. Storage key matches
  STORAGE_KEYS.THEME so existing users' preferences survive the swap.
-->
<ModeWatcher
  modeStorageKey={STORAGE_KEYS.THEME}
  defaultMode="system"
  disableHeadScriptInjection
/>

<!-- Toast surface. theme prop tracks the resolved mode so toasts match. -->
<Toaster
  theme={mode.current ?? 'system'}
  richColors
  closeButton
  position="bottom-right"
/>

<!-- Modal portal, each component self-renders based on its ui store target. -->
<BulkVerifyModal />
<BulkDeleteModal />
<DeleteModal />
<RenameModal />
<FileInfoModal />
<CancelAllJobsModal />
<ClearDoneModal />

<a class="skip-link" href="#main-content" onclick={handleSkip}>Skip to main content</a>

<div class="shell">
  <div class="sidebar-host"><Sidebar /></div>
  <MobileDrawer />
  <div class="main-col">
    <TopBar />
    <main id="main-content" bind:this={mainEl} class="main" tabindex="-1" aria-label="Main content">
      <svelte:boundary onerror={onBoundaryError}>
        {#if ui.activeView === 'dashboard'}<Dashboard />
        {:else if ui.activeView === 'workspace'}<WorkArea />
        {:else if ui.activeView === 'dat'}<DATView />
        {:else if ui.activeView === 'help'}<HelpView />
        {/if}
        {#snippet failed(error, reset)}
          <div class="error-frame">
            <EmptyState title="This view crashed" description={error?.message ?? 'An unexpected error occurred while rendering.'} glyph="!">
              {#snippet actions()}
                <Button variant="primary" onclick={reset}>Try again</Button>
              {/snippet}
            </EmptyState>
          </div>
        {/snippet}
      </svelte:boundary>
    </main>
  </div>
</div>

<style>
  .shell {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    height: 100dvh;
    background: var(--surface-2);
  }
  .sidebar-host { display: none; }
  @media (min-width: 900px) { .sidebar-host { display: block; } }
  .main-col {
    display: flex;
    flex-direction: column;
    min-width: 0;
    background: var(--surface-2);
  }
  .main { flex: 1; overflow-y: auto; outline: none; }
  .main:focus-visible { box-shadow: inset 0 0 0 3px var(--accent); }

  .skip-link {
    position: fixed;
    top: var(--space-2);
    left: var(--space-2);
    z-index: calc(var(--z-notification) + 1);
    padding: var(--space-2) var(--space-4);
    background: var(--accent);
    color: var(--accent-contrast);
    border-radius: var(--radius-md);
    font-size: var(--text-sm);
    font-weight: var(--weight-semibold);
    text-decoration: none;
    transform: translateY(-150%);
    transition: transform var(--dur-base) var(--ease-out);
  }
  .skip-link:focus-visible {
    transform: translateY(0);
    box-shadow: var(--elev-3);
  }
  .error-frame { padding: var(--space-6); }
</style>
