<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import { jobs } from '$lib/stores/jobs.svelte.js';
  import { toast } from 'svelte-sonner';
  import ConfirmModal from './ConfirmModal.svelte';
  import Trash2 from '@lucide/svelte/icons/trash-2';

  const open = $derived(ui.showClearDone);
  // /api/jobs/completed is a global op, it deletes every terminal
  // job server-side, including hidden metadata-scan and dat-match
  // history. Use the unfiltered total here (and call out hidden
  // counts in the description) so users aren't surprised when
  // background-job history disappears too. Visible-count gating
  // stays in JobsPanel's Clear button surface.
  const visibleTotal = $derived(
    jobs.visibleCompletedCount + jobs.visibleFailedCount,
  );
  const total = $derived(jobs.completedCount + jobs.failedCount + jobs.cancelledCount);
  const hiddenCount = $derived(Math.max(0, total - visibleTotal));

  function close() { ui.showClearDone = false; }

  async function handleConfirm() {
    // Snapshot the pre-clear count for the toast fallback. jobs.clearCompleted
    // optimistically drops terminal jobs from the local store before the
    // request resolves, so `total` would re-derive to 0 by the time the
    // toast evaluates and we'd report "Removed 0 job(s)" on a real success.
    const pending = total;
    try {
      const r = await jobs.clearCompleted();
      // Backend /api/jobs/completed returns { deleted, count, total_cleared }.
      // Prefer `total_cleared`: past MAX_JOB_HISTORY, `count` is only the rows
      // still held, so it would report "Removed 500" under a "Remove 1,153?"
      // prompt. Fall back to `count`, then to the pre-clear snapshot.
      const removed = typeof r?.total_cleared === 'number'
        ? r.total_cleared
        : typeof r?.count === 'number' ? r.count : pending;
      toast.success(`Removed ${removed} job(s) from history`);
      close();
    } catch (e) {
      toast.error(e?.message ?? 'Failed to clear completed jobs');
    }
  }
</script>

<ConfirmModal
  {open}
  onClose={close}
  onConfirm={handleConfirm}
  title="Clear completed jobs?"
  description={total === 0
    ? 'No completed, failed, or cancelled jobs to clear.'
    : hiddenCount > 0
      ? `Remove all ${total} terminal job(s), including ${hiddenCount} hidden metadata/DAT background job(s). Output files are not affected.`
      : `Remove ${total} completed, failed, and cancelled job(s) from the history. Output files are not affected.`}
  confirmLabel="Clear"
  cancelLabel="Keep"
  confirmVariant="primary"
  busy={jobs.clearingCompleted}
>
  {#snippet titleIcon()}<Trash2 size={18} aria-hidden="true" />{/snippet}
</ConfirmModal>
