<script>
  import { conversion } from '$lib/stores/conversion.svelte.js';
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { romm } from '$lib/stores/romm.svelte.js';
  import { ui } from '$lib/stores/ui.svelte.js';
  import Button from '$lib/components/ui/Button.svelte';
  import Checkbox from '$lib/components/ui/Checkbox.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import ModeSelect from './ModeSelect.svelte';
  import CompressionPicker from './CompressionPicker.svelte';
  import DuplicateModal from '$lib/components/modals/DuplicateModal.svelte';
  import DeletePlanModal from '$lib/components/modals/DeletePlanModal.svelte';
  import Settings2 from '@lucide/svelte/icons/settings-2';
  import Play from '@lucide/svelte/icons/play';
  import Loader from '@lucide/svelte/icons/loader-circle';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';

  // The full selection (drives the selection-count display) and the
  // subset that the active conversion mode would actually accept. The
  // two sets diverge when the user has e.g. verified `.chd` files
  // selected, those rows are valid Verify / Delete targets, but
  // CHDMAN createcd would reject them as conversion inputs. We only
  // submit `convertibleSelection`.
  const selectedCount = $derived(fileBrowser.selectedFiles.size);
  const convertibleFiles = $derived(fileBrowser.convertibleSelection);
  const selectedPaths = $derived(convertibleFiles.map((f) => f.path));
  const convertibleCount = $derived(selectedPaths.length);
  const incompatibleCount = $derived(selectedCount - convertibleCount);
  const tool = $derived(conversion.currentTool);
  const supportsDeleteOnVerify = $derived(conversion.supportsDeleteOnVerify);
  const supportsSplit = $derived(conversion.supportsSplit);
  const converting = $derived(conversion.converting);

  // Delete-on-verify is validated by the backend against a snapshot of
  // the selected sources, then the newly written output is verified before
  // deletion. The verification store only tracks existing verified outputs,
  // so source inputs must not be pre-filtered or block normal create/compress
  // flows here.
  const submitDisabled = $derived(convertibleCount === 0 || converting);

  // Duplicate preflight: open the modal when the backend reports any
  // output collision; await the user's pick (skip / overwrite / null
  // for cancel) and only then submit. Skips the modal entirely when
  // there are no collisions.
  let duplicatePrompt = $state(null); // resolver fn while modal is open
  const duplicateModalOpen = $derived(!!duplicatePrompt);

  function resolveDuplicate(action) {
    if (duplicatePrompt) {
      const resolver = duplicatePrompt;
      duplicatePrompt = null;
      ui.duplicatePromptOpen = false;
      resolver(action);
    }
  }

  async function awaitDuplicateChoice() {
    ui.duplicatePromptOpen = true;
    return new Promise((resolve) => {
      duplicatePrompt = resolve;
    });
  }

  // Delete-on-verify preflight: when the checkbox is on, fetch the
  // backend's delete-plan snapshot and show it for confirmation
  // before destructive submission. Same resolver pattern.
  let deletePlanPrompt = $state(null);
  const deletePlanModalOpen = $derived(!!deletePlanPrompt);

  function resolveDeletePlan(proceed) {
    if (deletePlanPrompt) {
      const resolver = deletePlanPrompt;
      deletePlanPrompt = null;
      ui.deletePlanPromptOpen = false;
      resolver(proceed);
    }
  }

  async function awaitDeletePlanChoice() {
    ui.deletePlanPromptOpen = true;
    return new Promise((resolve) => {
      deletePlanPrompt = resolve;
    });
  }

  async function handleSubmit() {
    if (submitDisabled) return;
    try {
      const check = await conversion.checkDuplicates(selectedPaths);
      const conflicts = Array.isArray(check) ? check.filter((d) => d?.exists) : [];
      let duplicateAction = 'skip';
      if (conflicts.length > 0) {
        const choice = await awaitDuplicateChoice();
        if (!choice) return;   // user cancelled
        duplicateAction = choice;
      }
      // Delete-on-verify destruction preview. We fetch the
      // backend's delete plan (cue+bin sets, gdi+raw tracks, etc.)
      // and require explicit confirmation before queueing, users
      // shouldn't trip the destructive flow without seeing what
      // disappears after each output verifies.
      //
      // Filter out duplicate-conflict paths when the user chose
      // skip: those inputs won't be converted, so the plan should
      // not include them. Otherwise the confirmation lists files
      // that won't be touched, and a sidecar/missing-source error
      // on a skipped input could block the whole submit.
      if (conversion.deleteOnVerify && supportsDeleteOnVerify) {
        const skippedConflicts = duplicateAction === 'skip'
          ? new Set(conflicts.map((c) => c?.file_path).filter(Boolean))
          : new Set();
        const planInputs = selectedPaths.filter((p) => !skippedConflicts.has(p));
        if (planInputs.length > 0) {
          try {
            await conversion.fetchDeletePlan(planInputs);
          } catch (_e) {
            // toast already raised in fetchDeletePlan
            return;
          }
          const proceed = await awaitDeletePlanChoice();
          conversion.clearDeletePlan();
          if (!proceed) return;
        }
        // planInputs empty means every input is a skipped duplicate:
        // the submit will queue zero jobs and conversion.submit's
        // toast will surface that. No need to show a plan modal.
      }
      const fromRomm = fileBrowser.rommPlatformId !== null;
      await conversion.submit(selectedPaths, {
        duplicateAction,
        // Only rows that came from the RomM catalog have RomM metadata to carry
        // across; a normal directory browse has nothing to snapshot.
        rommRepin: fromRomm,
        rommPlatformId: fileBrowser.rommPlatformId,
      });
      // Surface the follow-up straight away: the "Re-match in RomM" badge is
      // driven by this count, and leaving it stale hides the one action these
      // conversions will need once RomM rescans.
      if (fromRomm) romm.setPendingRepins(conversion.lastRepinPending);
      conversion.clearDuplicateCheck();
      fileBrowser.clearSelection();
      // Honor the per-deployment auto-return-from-search setting
      // (/api/version → ui.searchAutoReturnToFileList). The legacy
      // flow dropped users back to the pre-search directory after a
      // successful submit so they could see the new output in
      // context; otherwise the UI stays stuck in the recursive
      // search result set even though those files are now queued.
      if (fileBrowser.searchMode && ui.searchAutoReturnToFileList) {
        fileBrowser.exitSearch();
        // exitSearch only flips the flag; force a refresh so the
        // entries actually swap back to the current directory.
        fileBrowser.refresh({ force: true }).catch(() => {});
      }
    } catch (_e) {
      // toast already raised in conversion.submit / checkDuplicates
    }
  }

  function setOutputDir(value) {
    conversion.outputDir = value;
  }
</script>

<section class="panel" aria-label="Conversion configuration">
  <h2 class="panel-title">
    <Settings2 size={14} aria-hidden="true" />
    Convert with {tool?.label ?? '…'}
  </h2>

  {#if !tool}
    <EmptyState
      title="No tool selected"
      description="Pick a tool from the sidebar to configure conversions."
      glyph="◈"
    />
  {:else}
    <div class="fields">
      <ModeSelect />
      <CompressionPicker />

      <label class="field">
        <span class="label">Output directory</span>
        <input
          type="text"
          value={conversion.outputDir}
          placeholder="Leave blank to write next to source"
          oninput={(e) => setOutputDir(e.currentTarget.value)}
        />
        <span class="hint">
          Optional. Absolute paths or paths inside a mounted volume.
        </span>
      </label>

      {#if supportsDeleteOnVerify}
        <Checkbox
          bind:checked={conversion.deleteOnVerify}
          label="Delete sources after successful verification"
          description="The created output is verified before sources are deleted. Skipped if verify fails."
        />
      {/if}

      {#if supportsSplit}
        <Checkbox
          bind:checked={conversion.split}
          label="Split into 4 GB parts (FAT32)"
          description="Writes Game.iso.0, Game.iso.1, … so the image fits on FAT32. RPCS3 mounts the .0 part. Single .iso below 4 GB."
        />
      {/if}
    </div>

    {#if incompatibleCount > 0}
      <div class="warning info" role="status">
        <TriangleAlert size={14} aria-hidden="true" />
        <div>
          {incompatibleCount} selected file{incompatibleCount === 1 ? '' : 's'} {incompatibleCount === 1 ? 'is' : 'are'} not valid for this mode and will be skipped.
        </div>
      </div>
    {/if}

    <div class="actions">
      <span class="status" aria-live="polite">
        {#if selectedCount === 0}
          Select files to enable conversion.
        {:else if convertibleCount === 0}
          No selected files match this mode.
        {:else if incompatibleCount > 0}
          {convertibleCount} of {selectedCount} file{selectedCount === 1 ? '' : 's'} ready.
        {:else}
          {convertibleCount} file{convertibleCount === 1 ? '' : 's'} ready.
        {/if}
      </span>
      <Button variant="primary" disabled={submitDisabled} onclick={handleSubmit}>
        {#snippet icon()}
          {#if converting}<Loader size={14} class="spin" />{:else}<Play size={14} />{/if}
        {/snippet}
        {converting ? 'Queuing…' : 'Start conversion'}
      </Button>
    </div>
  {/if}
</section>

<DuplicateModal open={duplicateModalOpen} onResolve={resolveDuplicate} />
<DeletePlanModal open={deletePlanModalOpen} onResolve={resolveDeletePlan} />

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    min-width: 0;
  }
  .panel-title {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin: 0;
    font-size: var(--text-base);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .fields {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    min-width: 0;
  }
  .label {
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-2);
    font-weight: var(--weight-semibold);
  }
  .field input {
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-1);
    font-size: var(--text-sm);
    padding: var(--space-2) var(--space-3);
  }
  .field input:focus-visible {
    outline: none;
    border-color: var(--accent);
    box-shadow: var(--focus-ring);
  }
  .hint {
    color: var(--text-3);
    font-size: var(--text-xs);
  }

  .warning {
    display: flex;
    gap: var(--space-2);
    background: var(--warning-muted);
    color: var(--warning);
    border-radius: var(--radius-md);
    padding: var(--space-2) var(--space-3);
    font-size: var(--text-sm);
    align-items: flex-start;
  }
  .warning.info { background: var(--info-muted); color: var(--info); }

  .actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    flex-wrap: wrap;
  }
  .status {
    color: var(--text-2);
    font-size: var(--text-sm);
  }

  :global(.panel .spin) { animation: spin 0.9s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
