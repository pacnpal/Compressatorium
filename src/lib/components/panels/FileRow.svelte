<script>
  import { fileBrowser } from '$lib/stores/fileBrowser.svelte.js';
  import { verification } from '$lib/stores/verification.svelte.js';
  import { datMatching } from '$lib/stores/datMatching.svelte.js';
  import { chdMetadata } from '$lib/stores/chdMetadata.svelte.js';
  import { formatSize } from '$lib/api/format.js';
  import { iconForEntry } from '$lib/util/fileIcon.js';
  import CircleCheck from '@lucide/svelte/icons/circle-check';
  import BadgeCheck from '@lucide/svelte/icons/badge-check';
  import Badge from '$lib/components/ui/Badge.svelte';
  import RowActionsMenu from './RowActionsMenu.svelte';

  /** @type {{ entry: any }} */
  let { entry } = $props();

  const path = $derived(entry?.path);
  const isDirectory = $derived(entry?.type === 'directory');
  const isArchive = $derived(entry?.type === 'archive');
  const isFile = $derived(!isDirectory && !isArchive);

  const selected = $derived(fileBrowser.selectedFiles.has(path));
  // Files are always selectable; archives only when the active mode takes the
  // archive directly (e.g. romz_extract on .7z/.zip); directories only under a
  // folder-input mode (makeps3iso) and only when the backend marked them
  // convertible. Drives the checkbox; the name button still navigates.
  const selectable = $derived(fileBrowser.isSelectable(entry));
  const datMatch = $derived(datMatching.matchFor(path));
  const chdMeta = $derived(chdMetadata.metadataFor(path));

  // A match resolved remotely (Hasheous) instead of from a locally imported
  // DAT. Both carry the same payload keys; `source` is what tells them apart.
  const remoteMatch = $derived(datMatch?.source === 'hasheous');

  // Hasheous returns platform / publisher / year / region / originating DAT
  // that a local match doesn't have. Build the tooltip from whichever of them
  // are present so one expression covers both sources. Multi-line, because a
  // remote match has enough to say that one run-on line stops being readable.
  const matchTooltip = $derived.by(() => {
    if (!datMatch?.matched) return null;
    const lines = [
      datMatch.dat_name ? `Matches ${datMatch.dat_name}` : 'Matches DAT entry',
    ];
    if (datMatch.game_name) lines.push(datMatch.game_name);
    const facts = [datMatch.platform, datMatch.year, datMatch.region, datMatch.publisher]
      .filter(Boolean)
      .join(' · ');
    if (facts) lines.push(facts);
    if (datMatch.rom_name && datMatch.rom_name !== datMatch.game_name) {
      lines.push(datMatch.rom_name);
    }
    const links = Array.isArray(datMatch.metadata_links) ? datMatch.metadata_links : [];
    // Gate on the sources, not on links.length: entries without a `source`
    // would otherwise render a dangling "Listed on ".
    const sources = links.map((l) => l?.source).filter(Boolean);
    if (sources.length) lines.push(`Listed on ${sources.join(', ')}`);
    return lines.join('\n');
  });

  const mediaType = $derived(chdMeta?.media_type ?? entry?.media_type ?? null);

  const convertibleBy = $derived(
    Array.isArray(entry?.convertible_by) ? entry.convertible_by : [],
  );

  const outputs = $derived(Array.isArray(entry?.outputs) ? entry.outputs : []);

  // OK badge surfaces when EITHER this file itself is verified OR
  // any of its declared outputs (entry.outputs[].path) is verified.
  // The verification store is keyed by output paths, so a source
  // .cue / .gdi row would otherwise lose the badge even though the
  // backend listing already reports its .chd as verified.
  const verified = $derived.by(() => {
    if (verification.isVerified(path)) return true;
    for (const out of outputs) {
      if (out?.path && verification.isVerified(out.path)) return true;
    }
    return false;
  });

  // Shared file-type icon map (src/lib/util/fileIcon.js) so new formats only
  // need to be added in one place.
  const Icon = $derived(iconForEntry(entry));

  function handleRowClick(e) {
    if (isDirectory) {
      fileBrowser.navigate(entry.path);
      return;
    }
    if (isArchive) {
      fileBrowser.browseArchive(entry.path);
      return;
    }
    fileBrowser.toggleSelect(entry, { shift: e.shiftKey });
  }

  function handleCheckboxChange(e) {
    e.stopPropagation();
    fileBrowser.toggleSelect(entry, { shift: e.shiftKey });
  }

  function handleNameKey(e) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      handleRowClick(e);
    }
  }

  function archiveItemSummary(e) {
    const total = e?.archive_items;
    const converted = e?.archive_has_output;
    if (typeof total !== 'number') return null;
    if (typeof converted === 'number' && converted > 0) return `${total} items · ${converted} converted`;
    return `${total} items`;
  }
</script>

<tr
  class="row"
  class:selected
  class:dir={isDirectory}
  class:archive={isArchive}
>
  <td class="sel">
    {#if selectable}
      <input
        type="checkbox"
        checked={selected}
        onclick={handleCheckboxChange}
        aria-label={selected ? `Deselect ${entry.name}` : `Select ${entry.name}`}
      />
    {/if}
  </td>
  <td class="name">
    <button
      type="button"
      class="name-button"
      onclick={handleRowClick}
      onkeydown={handleNameKey}
      title={entry?.path}
    >
      <Icon size={16} class="ftype" />
      <span class="name-text">{entry?.name}</span>
      {#if isArchive}
        {@const summary = archiveItemSummary(entry)}
        {#if summary}<span class="archive-summary">{summary}</span>{/if}
      {/if}
    </button>
  </td>
  <td class="size">{isFile ? formatSize(entry?.size) : ''}</td>
  <td class="ext">{entry?.extension ?? ''}</td>
  <td class="badges">
    {#if mediaType === 'cd'}<Badge tone="cd">CD</Badge>{/if}
    {#if mediaType === 'dvd'}<Badge tone="dvd">DVD</Badge>{/if}
    {#if isArchive}<Badge tone="archive">ARC</Badge>{/if}
    {#if verified}
      <Badge tone="verified" title="Verified">
        <CircleCheck size={11} aria-hidden="true" /> OK
      </Badge>
    {/if}
    {#if datMatch && datMatch.matched}
      <Badge tone={remoteMatch ? 'info' : 'dat-match'} title={matchTooltip}>
        <BadgeCheck size={11} aria-hidden="true" /> {remoteMatch ? 'HASH' : 'DAT'}
      </Badge>
    {/if}
    {#each outputs as out (out.tool_id)}
      {#if out.exists}
        <Badge tone="success" title={`${out.tool_id} output ready (${out.path ?? ''})`}>
          {out.tool_id}{out.ready ? '' : '*'}
        </Badge>
      {/if}
    {/each}
    {#if (isFile || isDirectory) && convertibleBy.length > 0}
      <span class="convertible-hint" title={`Convertible by: ${convertibleBy.join(', ')}`}>
        {convertibleBy.join('·')}
      </span>
    {/if}
  </td>
  <td class="actions">
    <RowActionsMenu {entry} />
  </td>
</tr>

<style>
  .row {
    border-top: 1px solid var(--border-subtle);
    transition: background var(--dur-fast) var(--ease-out);
  }
  .row:hover { background: var(--surface-2); }
  .selected { background: var(--accent-muted); }
  .selected:hover { background: var(--accent-muted); }

  td {
    padding: var(--space-2) var(--space-3);
    vertical-align: middle;
    font-size: var(--text-sm);
    color: var(--text-1);
  }
  .sel { width: 32px; text-align: center; }
  .sel input { cursor: pointer; }

  .name { min-width: 0; }
  .name-button {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    width: 100%;
    background: none;
    border: none;
    color: inherit;
    text-align: left;
    cursor: pointer;
    font: inherit;
    padding: 0;
    min-width: 0;
  }
  .name-button :global(.ftype) {
    color: var(--text-2);
    flex-shrink: 0;
  }
  .dir .name-button :global(.ftype) { color: var(--accent); }
  .archive .name-button :global(.ftype) { color: var(--badge-archive); }
  .name-text {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
  }
  .archive-summary {
    color: var(--text-3);
    font-size: var(--text-xs);
    margin-left: var(--space-2);
  }

  .size {
    width: 80px;
    text-align: right;
    color: var(--text-2);
    font-variant-numeric: tabular-nums;
  }
  .ext {
    width: 60px;
    color: var(--text-3);
    font-family: var(--font-mono);
    font-size: var(--text-xs);
  }

  .badges {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
    align-items: center;
    min-width: 0;
  }
  .convertible-hint {
    color: var(--text-3);
    font-size: var(--text-xs);
    font-family: var(--font-mono);
  }

  .actions { width: 40px; text-align: center; }
</style>
