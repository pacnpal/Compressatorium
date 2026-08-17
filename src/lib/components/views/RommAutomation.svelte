<script>
  /**
   * Per-platform automation rules.
   *
   * Each RomM platform gets its own rule — target format, compression, output
   * location, schedule, queueing limits and selection filters — because a
   * library is not homogeneous and one global setting cannot describe it.
   *
   * The editor renders from the schema the backend ships alongside the rules
   * (`defaults` + `options`), so the UI never carries a second copy of what a
   * valid rule looks like.
   */
  import { SvelteSet } from 'svelte/reactivity';
  import { toast } from 'svelte-sonner';
  import { romm, DAY_LABELS, ORDER_LABELS } from '$lib/stores/romm.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import Button from '$lib/components/ui/Button.svelte';
  import Checkbox from '$lib/components/ui/Checkbox.svelte';
  import Select from '$lib/components/ui/Select.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import EmptyState from '$lib/components/ui/EmptyState.svelte';
  import Play from '@lucide/svelte/icons/play';
  import Eye from '@lucide/svelte/icons/eye';
  import Save from '@lucide/svelte/icons/save';
  import ChevronDown from '@lucide/svelte/icons/chevron-down';

  const platforms = $derived(romm.platforms);
  const autoOn = $derived(romm.settings?.auto_convert === true);

  // SvelteSet: mutation must be reactive so an expanded row re-renders.
  const expanded = new SvelteSet();
  let dirty = $state(false);

  // Every convertible target the registry knows, grouped by tool. Built from
  // the registry so a new tool or mode appears here with no change.
  const modeOptions = $derived.by(() => {
    const out = [{ value: '', label: 'Off — do not convert this platform' }];
    for (const tool of registry.all()) {
      for (const m of tool.modes ?? []) {
        if (m.kind === 'extract') continue; // automation converts, not unpacks
        out.push({ value: m.mode, label: `${tool.label} → ${m.label}` });
      }
    }
    return out;
  });

  function toggle(platformId) {
    if (expanded.has(platformId)) expanded.delete(platformId);
    else expanded.add(platformId);
  }

  function update(platformId, patch) {
    const rule = { ...romm.ruleFor(platformId), ...patch };
    if (!rule.mode) romm.removeRule(platformId);
    else romm.setRule(platformId, rule);
    dirty = true;
  }

  function toggleDay(platformId, day) {
    const rule = romm.ruleFor(platformId);
    const days = new SvelteSet(rule.days ?? []);
    if (days.has(day)) days.delete(day);
    else days.add(day);
    update(platformId, { days: [...days].sort((a, b) => a - b) });
  }

  function specFor(mode) {
    return mode ? registry.specFor(mode) : null;
  }

  async function save() {
    try {
      await romm.saveRules();
      dirty = false;
      toast.success('Automation rules saved');
    } catch (e) {
      toast.error(e?.message ?? 'Failed to save rules');
    }
  }

  async function preview() {
    try {
      const r = await romm.previewSweep();
      const n = r?.queued ?? 0;
      toast.info(n ? `Would queue ${n} conversion${n === 1 ? '' : 's'}` : 'Nothing to convert');
    } catch (e) {
      toast.error(e?.message ?? 'Preview failed');
    }
  }

  async function runNow() {
    try {
      const r = await romm.runSweep();
      const n = r?.queued ?? 0;
      if (n) toast.success(`Queued ${n} conversion${n === 1 ? '' : 's'}`);
      else toast.info('Nothing to convert right now');
    } catch (e) {
      toast.error(e?.message ?? 'Run failed');
    }
  }

  async function toggleAuto(enabled) {
    try {
      await romm.saveSettings({ auto_convert: enabled });
      toast.success(enabled ? 'Automatic conversion on' : 'Automatic conversion off');
    } catch (e) {
      toast.error(e?.message ?? 'Failed to save');
    }
  }

  function lastRunLabel(platformId) {
    const state = romm.lastRunFor(platformId);
    if (!state?.last_run_at) return 'never run';
    const when = new Date(state.last_run_at);
    if (Number.isNaN(when.getTime())) return 'never run';
    return `last run ${when.toLocaleString()} · queued ${state.last_queued ?? 0}`;
  }
</script>

<div class="automation">
  <section class="master">
    <div class="master-text">
      <h2>Automatic conversion</h2>
      <p>
        Each platform runs on its own schedule. A ROM is only queued when its
        target format is genuinely missing, so a sweep that runs twice — or
        after a restart — never duplicates work.
      </p>
    </div>
    <div class="master-actions">
      <Checkbox
        checked={autoOn}
        label="Run automatically"
        onchange={(v) => toggleAuto(v)}
      />
      <Button variant="secondary" onclick={preview} loading={romm.sweeping} icon={eyeIcon}>
        Preview
      </Button>
      <Button onclick={runNow} loading={romm.sweeping} icon={playIcon}>Run now</Button>
    </div>
  </section>

  {#if romm.sweepResult}
    {@const r = romm.sweepResult}
    <section class="sweep">
      <strong>{r.dry_run ? 'Preview' : 'Last run'}:</strong>
      {r.queued} queued
      <span class="muted">
        · {r.skipped_existing} already converted
        · {r.skipped_filtered} filtered out
        {#if r.skipped_active}· {r.skipped_active} already in the queue{/if}
        {#if r.stopped_reason === 'limit'}· stopped at the per-run limit{/if}
        {#if r.stopped_reason === 'queue_full'}· stopped, queue full{/if}
        {#if r.stopped_reason === 'no_rules'}· no rules configured{/if}
      </span>
      {#each r.platforms ?? [] as p (p.platform_id)}
        {#if p.candidates?.length}
          <details class="candidates">
            <summary>{romm.platformName(p.platform_id)}: {p.queued}</summary>
            <ul>{#each p.candidates as c (c)}<li>{c}</li>{/each}</ul>
          </details>
        {/if}
      {/each}
    </section>
  {/if}

  {#if romm.rulesLoading}
    <div class="loading"><Spinner /> Loading rules…</div>
  {:else if platforms.length === 0}
    <EmptyState
      title="No platforms yet"
      description="Connect RomM in Settings, then your platforms appear here."
    />
  {:else}
    <div class="rules">
      {#each platforms as platform (platform.id)}
        {@const rule = romm.ruleFor(platform.id)}
        {@const configured = !!romm.rules[String(platform.id)]}
        {@const spec = specFor(rule.mode)}
        {@const open = expanded.has(platform.id)}
        <article class="rule" class:configured>
          <header>
            <button class="row-toggle" type="button" onclick={() => toggle(platform.id)}>
              <ChevronDown size={16} class={open ? 'chev open' : 'chev'} />
              <span class="pname">{platform.name}</span>
              {#if platform.rom_count}<span class="muted">{platform.rom_count} ROMs</span>{/if}
            </button>
            <div class="row-summary">
              {#if configured && rule.enabled}
                <Badge tone="success">{spec?.label ?? rule.mode}</Badge>
                <span class="muted">every {rule.interval_minutes} min</span>
              {:else if configured}
                <Badge tone="neutral">paused</Badge>
              {:else}
                <span class="muted">not configured</span>
              {/if}
            </div>
          </header>

          {#if open}
            <div class="body">
              <div class="grid">
                <label class="field wide">
                  <span>Convert to</span>
                  <Select
                    value={rule.mode ?? ''}
                    options={modeOptions}
                    ariaLabel="Target format"
                    onchange={(v) => update(platform.id, { mode: v })}
                  />
                </label>
              </div>

              {#if rule.mode}
                <div class="grid">
                  <label class="field">
                    <span>Run every (minutes)</span>
                    <input
                      type="number" min="5" max="10080"
                      value={rule.interval_minutes}
                      onchange={(e) => update(platform.id, {
                        interval_minutes: Number(e.currentTarget.value),
                      })}
                    />
                  </label>

                  <label class="field">
                    <span>Max jobs per run</span>
                    <input
                      type="number" min="1" max="1000"
                      value={rule.max_per_run}
                      onchange={(e) => update(platform.id, {
                        max_per_run: Number(e.currentTarget.value),
                      })}
                    />
                  </label>

                  <label class="field">
                    <span>Only between</span>
                    <div class="pair">
                      <input
                        type="time" value={rule.window_start ?? ''}
                        onchange={(e) => update(platform.id, {
                          window_start: e.currentTarget.value || null,
                        })}
                      />
                      <span class="muted">and</span>
                      <input
                        type="time" value={rule.window_end ?? ''}
                        onchange={(e) => update(platform.id, {
                          window_end: e.currentTarget.value || null,
                        })}
                      />
                    </div>
                    <span class="hint">Leave blank to run at any hour.</span>
                  </label>

                  <div class="field wide">
                    <span>On these days</span>
                    <div class="days">
                      {#each DAY_LABELS as label, day (label)}
                        <button
                          type="button"
                          class="day"
                          class:on={(rule.days ?? []).includes(day)}
                          onclick={() => toggleDay(platform.id, day)}
                        >{label}</button>
                      {/each}
                    </div>
                  </div>

                  <label class="field">
                    <span>Convert in this order</span>
                    <Select
                      value={rule.order}
                      options={(romm.ruleOptions.orders ?? []).map((o) => ({
                        value: o, label: ORDER_LABELS[o] ?? o,
                      }))}
                      ariaLabel="Order"
                      onchange={(v) => update(platform.id, { order: v })}
                    />
                  </label>

                  <label class="field">
                    <span>Priority</span>
                    <input
                      type="number" min="-100" max="100"
                      value={rule.priority}
                      onchange={(e) => update(platform.id, {
                        priority: Number(e.currentTarget.value),
                      })}
                    />
                    <span class="hint">Lower runs first when several are due.</span>
                  </label>

                  <label class="field wide">
                    <span>Output folder</span>
                    <input
                      type="text"
                      placeholder="Leave blank to write beside the source"
                      value={rule.output_dir ?? ''}
                      onchange={(e) => update(platform.id, {
                        output_dir: e.currentTarget.value || null,
                      })}
                    />
                  </label>

                  <label class="field">
                    <span>Smallest ROM (MB)</span>
                    <input
                      type="number" min="0"
                      value={rule.min_size_mb}
                      onchange={(e) => update(platform.id, {
                        min_size_mb: Number(e.currentTarget.value),
                      })}
                    />
                  </label>

                  <label class="field">
                    <span>Largest ROM (MB, 0 = any)</span>
                    <input
                      type="number" min="0"
                      value={rule.max_size_mb}
                      onchange={(e) => update(platform.id, {
                        max_size_mb: Number(e.currentTarget.value),
                      })}
                    />
                  </label>

                  <label class="field">
                    <span>Only names matching</span>
                    <input
                      type="text" placeholder="e.g. \\(USA\\)"
                      value={rule.include_pattern ?? ''}
                      onchange={(e) => update(platform.id, {
                        include_pattern: e.currentTarget.value || null,
                      })}
                    />
                  </label>

                  <label class="field">
                    <span>Skip names matching</span>
                    <input
                      type="text" placeholder="e.g. \\(Beta\\)"
                      value={rule.exclude_pattern ?? ''}
                      onchange={(e) => update(platform.id, {
                        exclude_pattern: e.currentTarget.value || null,
                      })}
                    />
                  </label>
                </div>

                <div class="toggles">
                  <Checkbox
                    checked={rule.enabled}
                    label="Enabled"
                    onchange={(v) => update(platform.id, { enabled: v })}
                  />
                  <Checkbox
                    checked={rule.only_matched}
                    label="Only ROMs RomM has identified"
                    onchange={(v) => update(platform.id, { only_matched: v })}
                  />
                  <Checkbox
                    checked={rule.only_unmatched}
                    label="Only ROMs RomM could not identify"
                    onchange={(v) => update(platform.id, { only_unmatched: v })}
                  />
                  {#if spec?.supportsDeleteOnVerify}
                    <Checkbox
                      checked={rule.delete_on_verify}
                      label="Delete the source after the new file verifies"
                      onchange={(v) => update(platform.id, { delete_on_verify: v })}
                    />
                  {/if}
                </div>

                {#if romm.losesDatMatch(spec)}
                  <p class="warn">
                    RomM cannot hash-match <strong>{spec?.outputExt}</strong>, so
                    these ROMs need their metadata re-applied after RomM rescans.
                    Compressatorium saves it first and restores it for you.
                  </p>
                {/if}

                <p class="last-run muted">{lastRunLabel(platform.id)}</p>
              {:else}
                <p class="muted">Pick a target format to configure this platform.</p>
              {/if}
            </div>
          {/if}
        </article>
      {/each}
    </div>

    <div class="save-bar" class:visible={dirty}>
      <span>Unsaved changes</span>
      <Button onclick={save} loading={romm.rulesSaving} icon={saveIcon}>Save rules</Button>
    </div>
  {/if}
</div>

{#snippet playIcon()}<Play size={14} />{/snippet}
{#snippet eyeIcon()}<Eye size={14} />{/snippet}
{#snippet saveIcon()}<Save size={14} />{/snippet}

<style>
  .automation { display: grid; gap: var(--space-4); }
  .master {
    display: flex;
    justify-content: space-between;
    gap: var(--space-4);
    flex-wrap: wrap;
    align-items: center;
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
  }
  .master-text h2 { margin: 0; font-size: var(--text-lg); }
  .master-text p {
    margin: var(--space-1) 0 0;
    color: var(--text-2);
    font-size: var(--text-sm);
    max-width: 60ch;
  }
  .master-actions { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; }

  .sweep {
    background: var(--surface-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    padding: var(--space-3);
  }
  .candidates { margin-top: var(--space-2); }
  .candidates ul { margin: var(--space-1) 0 0; padding-left: var(--space-4); color: var(--text-2); }

  .rules { display: grid; gap: var(--space-2); }
  .rule {
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    overflow: hidden;
  }
  .rule.configured { border-color: var(--accent); }
  .rule header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-2) var(--space-3);
    flex-wrap: wrap;
  }
  .row-toggle {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    background: none;
    border: 0;
    color: inherit;
    font: inherit;
    cursor: pointer;
    padding: var(--space-1);
    min-width: 0;
  }
  .row-toggle :global(.chev) { transition: transform 120ms ease; }
  .row-toggle :global(.chev.open) { transform: rotate(180deg); }
  .pname { font-weight: var(--weight-semibold); }
  .row-summary { display: flex; align-items: center; gap: var(--space-2); }

  .body { padding: 0 var(--space-3) var(--space-3); display: grid; gap: var(--space-3); }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: var(--space-3);
  }
  .field { display: grid; gap: 4px; min-width: 0; }
  .field.wide { grid-column: 1 / -1; }
  .field > span:first-child { font-size: var(--text-sm); font-weight: var(--weight-medium); }
  .field input {
    padding: var(--space-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--surface-2);
    color: var(--text-1);
    font: inherit;
    min-width: 0;
  }
  .pair { display: flex; align-items: center; gap: var(--space-2); }
  .hint, .muted { color: var(--text-2); font-size: var(--text-sm); }

  .days { display: flex; gap: 4px; flex-wrap: wrap; }
  .day {
    padding: 4px 10px;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--surface-2);
    color: var(--text-2);
    cursor: pointer;
    font: inherit;
    font-size: var(--text-sm);
  }
  .day.on { background: var(--accent); border-color: var(--accent); color: #fff; }

  .toggles { display: grid; gap: var(--space-1); }
  .warn {
    margin: 0;
    padding: var(--space-2);
    border-radius: var(--radius-md);
    background: var(--surface-2);
    border: 1px solid var(--warning-border, var(--border-subtle));
    font-size: var(--text-sm);
  }
  .last-run { margin: 0; }

  .save-bar {
    position: sticky;
    bottom: var(--space-3);
    display: none;
    justify-content: space-between;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-3);
    border-radius: var(--radius-lg);
    background: var(--surface-1);
    border: 1px solid var(--accent);
    box-shadow: var(--elev-2, var(--elev-1));
  }
  .save-bar.visible { display: flex; }
</style>
