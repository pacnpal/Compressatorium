<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import { layout } from '$lib/stores/layout.svelte.js';
  import VolumeList from '$lib/components/panels/VolumeList.svelte';
  import FileList from '$lib/components/panels/FileList.svelte';
  import ConvertPanel from '$lib/components/panels/ConvertPanel.svelte';
  import JobsPanel from '$lib/components/panels/JobsPanel.svelte';
  import Splitter from '$lib/components/ui/Splitter.svelte';

  const tool = $derived(registry.forTool(ui.workspaceTool));

  // Snapshot the panel width at drag start; each move applies the
  // cumulative delta. The left divider grows .side as it moves right;
  // the right divider shrinks .right as it moves right (so the middle
  // file list always absorbs the slack).
  let dragStartLeft = 0;
  let dragStartRight = 0;
</script>

<section class="view" aria-labelledby="workspace-title">
  <header class="header">
    <div>
      <h1 id="workspace-title">{tool?.label ?? 'Workspace'}</h1>
      <p class="hint">{tool?.hint ?? ''}</p>
    </div>
  </header>

  <div class="grid" style="--ws-left: {layout.panels.left}px; --ws-right: {layout.panels.right}px;">
    <aside class="side">
      <VolumeList />
    </aside>

    <Splitter
      variant="panel"
      label="Resize volumes panel"
      value={layout.panels.left}
      min={160}
      max={360}
      onstart={() => (dragStartLeft = layout.panels.left)}
      onmove={(d) => layout.setPanelWidth('left', dragStartLeft + d)}
      onstep={(d) => layout.setPanelWidth('left', layout.panels.left + d)}
      onreset={() => layout.resetPanel('left')}
    />

    <article class="main">
      <FileList />
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
</section>

<style>
  .view {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    padding: var(--space-5);
    /* Wider than the default --container-max: the file table needs the
       room so all its columns show on a big monitor instead of scrolling
       horizontally. Other views keep the narrower reading-width cap. */
    max-width: 1760px;
    margin: 0 auto;
    width: 100%;
    min-width: 0;
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

  .grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--space-4);
    min-width: 0;
  }
  /* The panel splitters only make sense in the 3-column desktop layout.
     Hidden (and so removed from grid flow) below 1280px, where the right
     panel stacks and the left rail is a fixed width. Direct-child only:
     the file table's column handles are also .splitter elements but live
     deep inside .main, and must stay usable at every width. */
  .grid > :global(.splitter) { display: none; }
  @media (min-width: 900px) {
    .grid { grid-template-columns: 220px minmax(0, 1fr); }
    /* Two-column mode: volumes rail + file list share the first row, and
       the convert/jobs panel stacks below. Without the explicit span,
       auto-placement drops .right into the 220px rail column, leaving it
       a narrow strip beside a huge empty cell. */
    .right { grid-column: 1 / -1; }
  }
  @media (min-width: 1280px) {
    .grid {
      grid-template-columns:
        var(--ws-left, 220px) auto minmax(0, 1fr) auto var(--ws-right, 360px);
      gap: var(--space-2);
    }
    .grid > :global(.splitter) { display: block; }
    .right { grid-column: auto; }
  }

  .side, .main, .right {
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
