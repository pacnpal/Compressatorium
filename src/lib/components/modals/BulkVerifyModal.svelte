<script>
  // Batch verify dialog. Sequences verify-batches per tool: a selection
  // of mixed file types (.chd + .rvz + .z3ds) runs as three back-to-back
  // batches, one per tool, because each backend endpoint speaks one
  // verify command. Mid-run Cancel calls verification.cancelBatch() which
  // aborts the active fetch and skips remaining groups.

  import { Dialog } from 'bits-ui';
  import { verification } from '$lib/stores/verification.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import { ui } from '$lib/stores/ui.svelte.js';
  import { toast } from 'svelte-sonner';
  import ProgressBar from '$lib/components/ui/ProgressBar.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import XIcon from '@lucide/svelte/icons/x';
  import ShieldCheck from '@lucide/svelte/icons/shield-check';
  import Play from '@lucide/svelte/icons/play';

  // Drive open/close off the ui store so any panel can launch the modal
  // via `ui.bulkVerifyItems = entries`.
  const open = $derived(!!ui.bulkVerifyItems);
  const items = $derived(ui.bulkVerifyItems ?? []);

  // Group selected paths by the tool that owns their verify extension.
  // When the row is a source file (`.cue`, `.iso`, `.3ds`, etc.) that
  // isn't itself a verify target, fall back to any existing generated
  // output in `entry.outputs`, same idea as RowActionsMenu's
  // verify-from-output. Lets a selection of source rows still get
  // bulk-verified against their replacement outputs.
  const groups = $derived.by(() => {
    const byId = Object.create(null);
    const order = [];
    const skipped = [];

    function pickVerifyTarget(entry) {
      const path = typeof entry === 'string' ? entry : entry?.path;
      if (!path) return null;
      // Narrow by the backend's per-file `verifiable_by` when we have the entry
      // object (romz: single-ROM .7z/.zip only), matching the row menu. A bare
      // path string carries no refinement, so it falls back to extension match.
      const verifiableBy = typeof entry === 'object' ? entry?.verifiable_by : undefined;
      const direct = registry.verifyToolForPath(path, verifiableBy);
      if (direct) return { tool: direct, path };
      const outputs = typeof entry === 'object' && Array.isArray(entry?.outputs)
        ? entry.outputs
        : [];
      for (const out of outputs) {
        if (!out?.exists || !out?.path) continue;
        const t = registry.toolForVerifyPath(out.path);
        if (t) return { tool: t, path: out.path };
      }
      return null;
    }

    for (const entry of items) {
      const target = pickVerifyTarget(entry);
      if (!target) {
        const path = typeof entry === 'string' ? entry : entry?.path;
        if (path) skipped.push(path);
        continue;
      }
      if (!byId[target.tool.id]) {
        byId[target.tool.id] = { tool: target.tool, paths: [] };
        order.push(target.tool.id);
      }
      byId[target.tool.id].paths.push(target.path);
    }
    return { groups: order.map((id) => byId[id]), skipped };
  });

  const totalPaths = $derived(
    groups.groups.reduce((n, g) => n + g.paths.length, 0),
  );

  let running = $state(false);
  let groupIndex = $state(0);
  let runResults = $state({ verified: 0, failed: 0 });
  const batch = $derived(verification.batchRun);

  // Per-tool progress aggregated across groups. While a batch is in
  // flight, `batch.done` reflects the current group only; runResults
  // accumulates verified/failed across already-finished groups.
  const overallDone = $derived(
    runResults.verified + runResults.failed + (batch?.done ?? 0),
  );

  function close() {
    ui.bulkVerifyItems = null;
    // Reset run state so reopening with a fresh selection starts clean.
    running = false;
    groupIndex = 0;
    runResults = { verified: 0, failed: 0 };
    // Drop the completed-run summary from the store. verifyBatch leaves
    // batchRun populated after a successful run, and App.svelte treats
    // a truthy batchRun as "a batch is active" to suppress auto-refresh,
    // leaving it set would permanently block listing refreshes.
    verification.clearBatch();
  }

  function handleOpenChange(value) {
    if (!value) {
      // Cancel any in-flight batch when the user dismisses the dialog;
      // cancelBatch() aborts the fetch and nulls batchRun. close()
      // clears any completed-run summary for the not-running case.
      if (running) verification.cancelBatch();
      close();
    }
  }

  async function startRun() {
    if (running || groups.groups.length === 0) return;
    running = true;
    runResults = { verified: 0, failed: 0 };
    // Track totals locally and only assign to runResults after each
    // group. Reading the just-assigned `runResults.verified` for the
    // success toast can see the previous tick's value in some
    // proxy-snapshot cases; the local count is unambiguous and the
    // store update still drives the in-flight UI.
    let verifiedTotal = 0;
    let failedTotal = 0;
    let aborted = null;
    try {
      for (let i = 0; i < groups.groups.length; i += 1) {
        groupIndex = i;
        const { tool, paths } = groups.groups[i];
        const result = await verification.verifyBatch(tool.id, paths);
        verifiedTotal += result?.verified ?? 0;
        failedTotal += result?.failed ?? 0;
        runResults = { verified: verifiedTotal, failed: failedTotal };
        if (result?.aborted) {
          // A verifier could not be stopped and is still running. The next
          // group is a different tool but the same storage, so starting it
          // would strand another process. Stop the whole run.
          aborted = result.message ?? 'Verification stopped early';
          break;
        }
      }
      if (aborted) {
        toast.error(aborted);
      } else {
        toast.success(
          `Verified ${verifiedTotal} of ${totalPaths} file${totalPaths === 1 ? '' : 's'}`,
        );
      }
    } catch (e) {
      if (e?.name !== 'AbortError') {
        toast.error(e?.message ?? 'Batch verify failed');
      }
    } finally {
      running = false;
    }
  }

  function handleCancel() {
    verification.cancelBatch();
    running = false;
    toast.info('Batch verify cancelled');
  }
</script>

<Dialog.Root {open} onOpenChange={handleOpenChange}>
  <Dialog.Portal>
    <Dialog.Overlay class="bv-overlay" />
    <Dialog.Content class="bv-content">
      <header class="bv-header">
        <Dialog.Title class="bv-title">
          <ShieldCheck size={18} aria-hidden="true" />
          Batch verify
        </Dialog.Title>
        <Dialog.Close class="bv-close" aria-label="Close">
          <XIcon size={16} />
        </Dialog.Close>
      </header>

      <Dialog.Description class="bv-desc">
        {totalPaths} file{totalPaths === 1 ? '' : 's'} across
        {groups.groups.length} tool{groups.groups.length === 1 ? '' : 's'}.
        {#if groups.skipped.length > 0}
          {groups.skipped.length} skipped, no matching verify tool.
        {/if}
      </Dialog.Description>

      <ul class="bv-groups">
        {#each groups.groups as g, i (g.tool.id)}
          <li class="bv-group" class:active={running && i === groupIndex} class:done={running && i < groupIndex}>
            <Badge tone="info" size="sm">{g.tool.label}</Badge>
            <span class="bv-group-count">{g.paths.length}</span>
            {#if running && i === groupIndex}
              <span class="bv-group-status">running</span>
            {:else if running && i < groupIndex}
              <span class="bv-group-status done">done</span>
            {:else}
              <span class="bv-group-status">queued</span>
            {/if}
          </li>
        {/each}
      </ul>

      {#if running && batch}
        <div class="bv-progress">
          <div class="bv-progress-row">
            <span class="bv-current" title={batch.currentPath ?? ''}>
              {batch.currentFilename ?? batch.currentPath ?? 'Preparing…'}
            </span>
            <span class="bv-counts">
              {overallDone} / {totalPaths}
            </span>
          </div>
          <ProgressBar
            value={typeof batch.currentPercent === 'number' ? batch.currentPercent : null}
            size="sm"
          />
          {#if batch.message}
            <div class="bv-msg">{batch.message}</div>
          {/if}
        </div>
      {:else if !running && (runResults.verified + runResults.failed) > 0}
        <div class="bv-summary" role="status">
          <Badge tone="success">{runResults.verified} verified</Badge>
          {#if runResults.failed > 0}
            <Badge tone="error">{runResults.failed} failed</Badge>
          {/if}
          {#if groups.skipped.length > 0}
            <Badge tone="warning">{groups.skipped.length} skipped</Badge>
          {/if}
        </div>
      {/if}

      <footer class="bv-footer">
        {#if running}
          <Button variant="destructive" onclick={handleCancel}>
            {#snippet icon()}<XIcon size={14} />{/snippet}
            Cancel
          </Button>
        {:else if (runResults.verified + runResults.failed) > 0}
          <Button variant="secondary" onclick={close}>Close</Button>
        {:else}
          <Button variant="secondary" onclick={close}>Dismiss</Button>
          <Button variant="primary" disabled={groups.groups.length === 0} onclick={startRun}>
            {#snippet icon()}<Play size={14} />{/snippet}
            Start
          </Button>
        {/if}
      </footer>
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>

<style>
  :global(.bv-overlay) {
    position: fixed;
    inset: 0;
    background: var(--surface-overlay);
    z-index: var(--z-modal-backdrop);
  }
  :global(.bv-content) {
    position: fixed;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    width: min(560px, calc(100vw - var(--space-5)));
    max-height: calc(100dvh - var(--space-6));
    overflow-y: auto;
    background: var(--surface-raised);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    box-shadow: var(--elev-3);
    padding: var(--space-5);
    z-index: var(--z-modal);
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  :global(.bv-header) {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
  }
  :global(.bv-title) {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    margin: 0;
    font-size: var(--text-lg);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
  }
  :global(.bv-close) {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    background: transparent;
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    color: var(--text-2);
    cursor: pointer;
  }
  :global(.bv-close:hover) {
    background: var(--surface-2);
    color: var(--text-1);
    border-color: var(--border-subtle);
  }
  :global(.bv-desc) {
    color: var(--text-2);
    font-size: var(--text-sm);
    margin: 0;
  }

  .bv-groups {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }
  .bv-group {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-1) var(--space-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    background: var(--surface-2);
  }
  .bv-group.active { border-color: var(--accent); background: var(--accent-muted); }
  .bv-group.done { opacity: 0.7; }
  .bv-group-count {
    font-variant-numeric: tabular-nums;
    color: var(--text-2);
    font-size: var(--text-xs);
  }
  .bv-group-status {
    margin-left: auto;
    color: var(--text-3);
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .bv-group-status.done { color: var(--success); }

  .bv-progress { display: flex; flex-direction: column; gap: var(--space-1); }
  .bv-progress-row { display: flex; justify-content: space-between; gap: var(--space-2); }
  .bv-current {
    flex: 1;
    min-width: 0;
    color: var(--text-1);
    font-size: var(--text-sm);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: var(--font-mono);
  }
  .bv-counts {
    color: var(--text-2);
    font-size: var(--text-xs);
    font-variant-numeric: tabular-nums;
  }
  .bv-msg { color: var(--text-3); font-size: var(--text-xs); }

  .bv-summary { display: flex; gap: var(--space-2); flex-wrap: wrap; }

  :global(.bv-footer) {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-2);
  }
</style>
