<script>
  // Pre-submit confirmation for delete-on-verify. Backend
  // /api/jobs/delete-plan returns the exact files that would be
  // removed after each conversion's output verifies, sources plus
  // sidecar files (cue + bin set, gdi + raw tracks, etc.). Users
  // shouldn't trip the destructive flow without seeing the list.
  //
  // Per-submit promise-resolver pattern, same as DuplicateModal.

  import { conversion } from '$lib/stores/conversion.svelte.js';
  import { summarizeMessages } from '$lib/util/messages.js';
  import BaseModal from './BaseModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
  import Trash2 from '@lucide/svelte/icons/trash-2';

  /** @type {{ open: boolean, onResolve: (proceed: boolean) => void }} */
  let { open, onResolve } = $props();

  const plan = $derived(conversion.deletePlan);
  const items = $derived(Array.isArray(plan?.items) ? plan.items : []);
  const totalDelete = $derived(plan?.total_delete_count ?? 0);
  const blocked = $derived(!!plan?.blocked);
  const disallowedArchives = $derived(
    Array.isArray(plan?.disallowed_archives) ? plan.disallowed_archives : [],
  );

  // Flatten blocking reasons across items for the warning line.
  //
  // Summarized, not raw: items commonly fail the *same* way (two members of one
  // archive each get the identical "multiple selections from the same archive"
  // rejection), and the list below is keyed by the message string — duplicate
  // keys crash the view. See summarizeMessages.
  const blockingMessages = $derived.by(() => {
    const out = [];
    for (const item of items) {
      for (const m of item.errors ?? []) out.push(m);
      for (const m of item.unsafe_paths ?? []) out.push(`Unsafe: ${m}`);
      for (const m of item.missing_paths ?? []) out.push(`Missing: ${m}`);
    }
    return summarizeMessages(out);
  });

  // Non-blocking warnings the backend wants the user to see before
  // confirming, e.g. "Archive input detected; delete-on-verify will
  // remove the entire archive". These don't disable the Confirm
  // button but the user needs to read them.
  //
  // Summarized for the same reason: that archive warning is a fixed string
  // emitted once per archive source, so any multi-archive selection would
  // otherwise hand the keyed list below one duplicate key per source.
  const planWarnings = $derived.by(() => {
    const out = [];
    for (const item of items) {
      for (const m of item.warnings ?? []) out.push(m);
    }
    return summarizeMessages(out);
  });

  function shortName(p) {
    return p?.split(/[/\\]/).pop() ?? p ?? '';
  }
</script>

<BaseModal {open} onClose={() => onResolve(false)} title="Confirm delete-on-verify" size="md">
  {#snippet titleIcon()}<Trash2 size={18} aria-hidden="true" />{/snippet}
  {#snippet body()}
    <p class="dp-lead">
      <Badge tone={blocked ? 'error' : 'warning'}>{totalDelete}</Badge>
      file{totalDelete === 1 ? '' : 's'} will be deleted across {items.length}
      conversion{items.length === 1 ? '' : 's'}, but only after each
      output is verified. This cannot be undone.
    </p>

    {#if blocked}
      <div class="dp-block" role="alert">
        <TriangleAlert size={14} aria-hidden="true" />
        <div>
          <strong>Blocked.</strong>
          {#if blockingMessages.length > 0}
            <ul class="dp-block-list">
              {#each blockingMessages.slice(0, 8) as m (m)}<li>{m}</li>{/each}
              {#if blockingMessages.length > 8}<li>…and {blockingMessages.length - 8} more</li>{/if}
            </ul>
          {:else}
            Backend rejected the delete plan for one or more sources.
          {/if}
        </div>
      </div>
    {/if}

    {#if disallowedArchives.length > 0}
      <div class="dp-block" role="alert">
        <TriangleAlert size={14} aria-hidden="true" />
        <div>
          Delete-on-verify is not supported for multiple selections from the same archive
          ({disallowedArchives.length}).
        </div>
      </div>
    {/if}

    {#if planWarnings.length > 0}
      <div class="dp-warn" role="status">
        <TriangleAlert size={14} aria-hidden="true" />
        <div>
          <strong>Heads up:</strong>
          <ul class="dp-warn-list">
            {#each planWarnings.slice(0, 8) as m (m)}<li>{m}</li>{/each}
            {#if planWarnings.length > 8}<li>…and {planWarnings.length - 8} more</li>{/if}
          </ul>
        </div>
      </div>
    {/if}

    <ul class="dp-list">
      {#each items.slice(0, 20) as item, idx (item.source_path ?? idx)}
        <li class="dp-item">
          <div class="dp-source" title={item.source_path}>
            {shortName(item.source_path)}
          </div>
          {#if item.delete_paths?.length}
            <div class="dp-targets">
              → {item.delete_paths.length} file{item.delete_paths.length === 1 ? '' : 's'}
              {#each item.delete_paths.slice(0, 4) as p (p)}
                <span class="dp-target" title={p}>{shortName(p)}</span>
              {/each}
              {#if item.delete_paths.length > 4}
                <span class="dp-more">+{item.delete_paths.length - 4}</span>
              {/if}
            </div>
          {/if}
        </li>
      {/each}
      {#if items.length > 20}
        <li class="dp-more-row">…and {items.length - 20} more</li>
      {/if}
    </ul>
  {/snippet}
  {#snippet footer()}
    <Button variant="secondary" onclick={() => onResolve(false)}>Cancel</Button>
    <Button variant="destructive" disabled={blocked || disallowedArchives.length > 0} onclick={() => onResolve(true)}>
      Confirm and queue
    </Button>
  {/snippet}
</BaseModal>

<style>
  .dp-lead { margin: 0; font-size: var(--text-sm); color: var(--text-1); display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
  .dp-block { display: flex; gap: var(--space-2); background: var(--error-muted); color: var(--error); border-radius: var(--radius-md); padding: var(--space-2) var(--space-3); font-size: var(--text-sm); align-items: flex-start; }
  .dp-block strong { font-weight: var(--weight-semibold); }
  .dp-block-list { margin: var(--space-1) 0 0; padding-left: var(--space-4); font-size: var(--text-xs); }
  .dp-warn { display: flex; gap: var(--space-2); background: var(--warning-muted); color: var(--warning); border-radius: var(--radius-md); padding: var(--space-2) var(--space-3); font-size: var(--text-sm); align-items: flex-start; }
  .dp-warn strong { font-weight: var(--weight-semibold); }
  .dp-warn-list { margin: var(--space-1) 0 0; padding-left: var(--space-4); font-size: var(--text-xs); }
  .dp-list { list-style: none; margin: 0; padding: 0; max-height: 280px; overflow-y: auto; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--surface-2); }
  .dp-item { padding: 6px var(--space-2); border-bottom: 1px solid var(--border-subtle); display: flex; flex-direction: column; gap: 2px; }
  .dp-item:last-child { border-bottom: none; }
  .dp-source { color: var(--text-1); font-size: var(--text-sm); font-weight: var(--weight-medium); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--font-mono); }
  .dp-targets { display: flex; gap: var(--space-1); align-items: center; flex-wrap: wrap; color: var(--text-3); font-size: var(--text-xs); font-family: var(--font-mono); }
  .dp-target { color: var(--text-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 220px; }
  .dp-more { color: var(--text-3); font-size: var(--text-xs); }
  .dp-more-row { padding: 4px var(--space-2); color: var(--text-3); font-size: var(--text-xs); font-style: italic; }
</style>
