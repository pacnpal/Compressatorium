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
  import { registry, DEFAULT_COMPRESSION_LEVEL_RANGE } from '$lib/tools/registry.js';
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

  // The convertible targets one platform can actually use. Built from the
  // registry so a new tool or mode appears here with no change, then narrowed
  // by the tool ids the backend derived for this platform's slug — the same
  // narrowing the conversion path applies, so the editor cannot offer a
  // GameCube library a PS2-only mode.
  function modeOptionsFor(platform) {
    const allowedTools = platform?.tool_ids;
    // Narrowed per mode as well as per tool. A composite tool belongs to no
    // single system — the chain tool has a GameCube mode and a PS2 mode — so
    // the tool list keeps it on both and only the mode list can tell them
    // apart. Both come from the backend registry rather than a second copy of
    // the platform table here.
    const allowedModes = platform?.mode_ids;
    const out = [{ value: '', label: 'Off — do not convert this platform' }];
    for (const tool of registry.all()) {
      // No opinion from the backend (unknown slug, older server) keeps every
      // tool, matching narrow_to_platform's conservative contract.
      if (Array.isArray(allowedTools) && !allowedTools.includes(tool.id)) continue;
      for (const m of tool.modes ?? []) {
        if (m.kind === 'extract') continue; // automation converts, not unpacks
        if (Array.isArray(allowedModes) && !allowedModes.includes(m.mode)) continue;
        out.push({ value: m.mode, label: `${tool.label} → ${m.label}` });
      }
    }
    return out;
  }

  function toggle(platformId) {
    if (expanded.has(platformId)) expanded.delete(platformId);
    else expanded.add(platformId);
  }

  // The zone the schedule window is evaluated in. Sent with every rule so a
  // window entered as "22:00" means 22:00 where the operator is, not UTC.
  const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';

  function update(platformId, patch) {
    const current = romm.ruleFor(platformId);
    // A platform with no stored rule is being created right now, so it adopts
    // the browser's zone. `ruleFor` falls back to the backend default, whose
    // timezone is the truthy string "UTC" — testing `current.timezone ||`
    // therefore always kept UTC and a 22:00–04:00 window written in Berlin ran
    // at 22:00 UTC. A rule the operator has already saved keeps its own zone.
    const isNew = !romm.rules[String(platformId)];
    const rule = {
      ...current,
      timezone: isNew ? browserZone : (current.timezone || browserZone),
      // Changing the target invalidates everything that described the old one.
      // Carrying `compression: "max"` from a CSO rule into a CHDMAN one passes
      // normalization and then fails every queued job on `chdman -c max`.
      ...(patch.mode !== undefined && patch.mode !== current.mode
        ? { compression: null, compression_level: null, split: false }
        : {}),
      ...patch,
    };
    if (!rule.mode) romm.removeRule(platformId);
    else romm.setRule(platformId, rule);
    dirty = true;
  }

  /**
   * The two identification filters are mutually exclusive.
   *
   * The backend collapses "both ticked" to "neither" (it would otherwise mean
   * "nothing matches"), so leaving both tickable in the editor would show a
   * state that silently becomes a different one on save. Ticking one clears
   * the other here instead.
   */
  function setIdFilter(platformId, field, value) {
    const other = field === 'only_matched' ? 'only_unmatched' : 'only_matched';
    update(platformId, { [field]: value, ...(value ? { [other]: false } : {}) });
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

  function toolFor(mode) {
    return mode ? registry.toolForMode(mode) : null;
  }

  // --- conversion options, straight off the registry descriptor ----------
  //
  // Same source of truth CompressionPicker reads for the manual panel, so a
  // new tool declaring `compressionCodecs` / `compressionStyle` in
  // registry.js appears here with no edit. The rule stores one string, which
  // is what the job pipeline takes: a comma list for chdman's multi style,
  // a single codec elsewhere (the level rides separately and the backend
  // joins them as `codec:level`).

  function codecsFor(mode) {
    return toolFor(mode)?.compressionCodecs ?? [];
  }

  function codecStyleFor(mode) {
    return toolFor(mode)?.compressionStyle ?? 'none';
  }

  function levelRangeFor(mode) {
    return toolFor(mode)?.compressionLevelRange ?? DEFAULT_COMPRESSION_LEVEL_RANGE;
  }

  /** The codecs a rule currently has selected, as a list. */
  function selectedCodecs(rule) {
    return (rule.compression ?? '').split(',').map((c) => c.trim()).filter(Boolean);
  }

  /** Toggle one codec in a multi-codec (chdman) rule.
   *
   * The cap comes from the registry, the same place the manual picker reads
   * it: chdman takes at most four codecs in `-c`, and a fifth here would save
   * happily and then fail every job the rule ever queued.
   */
  function toggleCodec(platformId, rule, value) {
    const current = selectedCodecs(rule);
    if (!current.includes(value) && current.length >= registry.maxCodecsFor(rule.mode)) {
      toast.error(`Up to ${registry.maxCodecsFor(rule.mode)} codecs at once.`);
      return;
    }
    const next = current.includes(value)
      ? current.filter((c) => c !== value)
      : [...current, value];
    update(platformId, { compression: next.join(',') || null });
  }

  const duplicateLabels = {
    skip: 'Skip it — leave the existing file alone',
    overwrite: 'Overwrite it',
    rename: 'Write alongside it (Game_1.rvz)',
  };

  const duplicateOptions = $derived(
    (romm.ruleOptions?.duplicate_actions ?? ['skip', 'overwrite', 'rename']).map(
      (value) => ({ value, label: duplicateLabels[value] ?? value }),
    ),
  );

  async function save() {
    try {
      await romm.saveRules();
      dirty = false;
      toast.success('Automation rules saved');
    } catch (e) {
      toast.error(e?.message ?? 'Failed to save rules');
    }
  }

  /**
   * Persist unsaved edits before a sweep.
   *
   * The sweep runs server-side against the *stored* rules, so previewing or
   * running with a dirty editor would report on rules the operator can no
   * longer see. Saving first keeps the backend the single source of truth
   * instead of teaching the sweep endpoints to accept a rule set inline.
   */
  async function commitPendingEdits() {
    if (!dirty) return true;
    try {
      await romm.saveRules();
      dirty = false;
      return true;
    } catch (e) {
      toast.error(e?.message ?? 'Failed to save rules');
      return false;
    }
  }

  async function preview() {
    if (!(await commitPendingEdits())) return;
    try {
      const r = await romm.previewSweep();
      const n = r?.queued ?? 0;
      toast.info(n ? `Would queue ${n} conversion${n === 1 ? '' : 's'}` : 'Nothing to convert');
    } catch (e) {
      toast.error(e?.message ?? 'Preview failed');
    }
  }

  async function runNow() {
    if (!(await commitPendingEdits())) return;
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

  /** Clear one platform's converted-id history (see the store for why). */
  async function forgetConverted(platformId) {
    try {
      await romm.forgetConverted([String(platformId)]);
      toast.success('Conversion history cleared for this platform');
    } catch (e) {
      toast.error(e?.message ?? 'Failed to clear the conversion history');
    }
  }

  /** Why a platform produced nothing, in the operator's terms.
   *
   * These reasons exist precisely because the sweep refuses to queue work it
   * knows will fail, so saying nothing would leave a rule that converts
   * nothing and never explains itself.
   */
  const SWEEP_ERROR_LABELS = {
    tool_not_ready: 'the tool for this format is not installed here — skipped',
    tool_wrong_for_platform: 'the saved format is not for this system — skipped',
    output_dir_outside_volumes:
      'its output folder is outside the configured volumes — skipped',
    unreadable: 'RomM could not list this platform',
    queue_failed: 'the conversion queue rejected this batch',
  };

  function sweepErrorLabel(code) {
    return SWEEP_ERROR_LABELS[code] ?? code;
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
      {r.queued ?? 0} queued
      <span class="muted">
        · {r.skipped_existing ?? 0} already converted
        · {r.skipped_filtered ?? 0} filtered out
        {#if r.skipped_active}· {r.skipped_active} already in the queue{/if}
        {#if r.skipped_missing}· {r.skipped_missing} listed by RomM but gone from disk{/if}
        {#if r.skipped_unconvertible}
          · {r.skipped_unconvertible} the target format cannot take
        {/if}
        {#if r.stopped_reason === 'limit'}· stopped at the per-run limit{/if}
        {#if r.stopped_reason === 'queue_full'}· stopped, queue full{/if}
        {#if r.stopped_reason === 'no_rules'}· no rules configured{/if}
      </span>
      {#if r.errors?.length}
        <ul class="sweep-errors">
          {#each r.errors as e (e.platform_id)}
            <li>{romm.platformName(e.platform_id)}: {sweepErrorLabel(e.error)}</li>
          {/each}
        </ul>
      {/if}
      {#each r.platforms ?? [] as p (p.platform_id)}
        {#if p.candidates?.length}
          <details class="candidates">
            <summary>{romm.platformName(p.platform_id)}: {p.queued}</summary>
            <!-- Keyed by index: the backend reduces candidates to basenames, so
                 two ROMs in different folders can both be "Game.iso" and a
                 value key would throw mid-render. -->
            <ul>{#each p.candidates as c, i (i)}<li>{c}</li>{/each}</ul>
          </details>
        {/if}
      {/each}
    </section>
  {/if}

  {#if romm.rulesError}
    <div class="rules-error" role="alert">
      Could not load the automation rules: {romm.rulesError}
    </div>
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
                    options={modeOptionsFor(platform)}
                    ariaLabel="Target format"
                    onchange={(v) => update(platform.id, { mode: v })}
                  />
                </label>
              </div>

              {#if rule.mode}
                {@const codecs = codecsFor(rule.mode)}
                {@const style = codecStyleFor(rule.mode)}
                {@const range = levelRangeFor(rule.mode)}
                {@const chosen = selectedCodecs(rule)}

                <div class="grid">
                  {#if (spec?.supportsCompression || spec?.supportsCompressionLevel) && codecs.length > 0}
                    {#if style === 'multi'}
                      <div class="field wide">
                        <span>Compression</span>
                        <div class="chips" role="group" aria-label="Codec selection">
                          {#each codecs as codec (codec.value)}
                            {#if codec.value !== 'none'}
                              <button
                                type="button"
                                class="chip"
                                class:active={chosen.includes(codec.value)}
                                aria-pressed={chosen.includes(codec.value)}
                                title={codec.hint ?? ''}
                                onclick={() => toggleCodec(platform.id, rule, codec.value)}
                              >{codec.label}</button>
                            {/if}
                          {/each}
                        </div>
                        <span class="hint">
                          Leave all off to use {spec?.label ?? 'the tool'}'s default.
                          {#if Number.isFinite(registry.maxCodecsFor(rule.mode))}
                            Up to {registry.maxCodecsFor(rule.mode)} at once
                            ({chosen.length} selected).
                          {/if}
                        </span>
                      </div>
                    {:else}
                      <label class="field">
                        <span>Compression</span>
                        <Select
                          value={rule.compression ?? ''}
                          options={[
                            { value: '', label: 'Tool default' },
                            ...codecs.map((c) => ({ value: c.value, label: c.label })),
                          ]}
                          ariaLabel="Compression codec"
                          onchange={(v) => update(platform.id, { compression: v || null })}
                        />
                      </label>
                    {/if}
                  {/if}

                  {#if spec?.supportsCompressionLevel}
                    <label class="field">
                      <span>Compression level</span>
                      <input
                        type="number" min={range.min} max={range.max}
                        placeholder={String(range.default)}
                        value={rule.compression_level ?? ''}
                        onchange={(e) => update(platform.id, {
                          compression_level: e.currentTarget.value === ''
                            ? null
                            : Number(e.currentTarget.value),
                        })}
                      />
                      <span class="hint">{range.min}–{range.max}. Blank uses {range.default}.</span>
                    </label>
                  {/if}

                  <label class="field wide">
                    <span>If the output already exists</span>
                    <Select
                      value={rule.duplicate_action ?? 'skip'}
                      options={duplicateOptions}
                      ariaLabel="Existing output policy"
                      onchange={(v) => update(platform.id, { duplicate_action: v })}
                    />
                  </label>
                </div>

                {#if spec?.supportsSplit}
                  <div class="toggles">
                    <Checkbox
                      checked={rule.split}
                      label="Split into 4 GB parts (FAT32)"
                      description="Writes Game.iso.0, Game.iso.1, … so the image fits on FAT32."
                      onchange={(v) => update(platform.id, { split: v })}
                    />
                  </div>
                {/if}

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
                    <span class="hint">
                      Leave blank to run at any hour. Times are
                      {rule.timezone || browserZone}.
                    </span>
                  </label>

                  <div class="field wide">
                    <span>On these days</span>
                    <div class="days">
                      {#each DAY_LABELS as label, day (label)}
                        <button
                          type="button"
                          class="day"
                          class:on={(rule.days ?? []).includes(day)}
                          aria-pressed={(rule.days ?? []).includes(day)}
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
                    onchange={(v) => setIdFilter(platform.id, 'only_matched', v)}
                  />
                  <Checkbox
                    checked={rule.only_unmatched}
                    label="Only ROMs RomM could not identify"
                    onchange={(v) => setIdFilter(platform.id, 'only_unmatched', v)}
                  />
                  {#if spec?.supportsDeleteOnVerify}
                    <Checkbox
                      checked={rule.verify_after || rule.delete_on_verify}
                      disabled={rule.delete_on_verify}
                      label="Verify each converted file"
                      description="Deleting the source already verifies first."
                      onchange={(v) => update(platform.id, { verify_after: v })}
                    />
                    <Checkbox
                      checked={rule.delete_on_verify}
                      label="Delete the source after the new file verifies"
                      onchange={(v) => update(platform.id, { delete_on_verify: v })}
                    />
                  {/if}
                </div>

                {#if rule.invalid_output_dir}
                  <p class="warn" role="alert">
                    <strong>{rule.invalid_output_dir}</strong> is outside the configured
                    volumes, so it was not saved and this rule is paused. Pick a folder
                    inside a mounted volume, or clear the field to write beside each source.
                  </p>
                {/if}

                {#if rule.invalid_pattern}
                  <p class="warn" role="alert">
                    A name filter could not be read as a regular expression, so it was
                    not saved and this rule is paused — running it unfiltered would
                    convert the whole platform.
                  </p>
                {/if}

                {#if rule.unsafe_delete_on_verify}
                  <p class="warn" role="alert">
                    Deleting the source was switched off: with this compression setting
                    the verification is only structural, which is not enough to justify
                    removing the original.
                  </p>
                {/if}

                {#if romm.losesDatMatch(spec)}
                  <p class="warn">
                    RomM cannot hash-match <strong>{spec?.outputExt}</strong>, so
                    these ROMs need their metadata re-applied after RomM rescans.
                    Compressatorium saves it first and restores it for you.
                  </p>
                {/if}

                <p class="last-run muted">{lastRunLabel(platform.id)}</p>

                {#if rule.duplicate_action !== 'skip'}
                  {@const remembered = romm.convertedCountFor(platform.id)}
                  {#if remembered > 0}
                    <p class="last-run muted forget">
                      <span>
                        Remembers {remembered}
                        {remembered === 1 ? 'ROM' : 'ROMs'} it has already converted, so
                        {duplicateLabels[rule.duplicate_action] ?? rule.duplicate_action}
                        happens once per ROM instead of every run.
                      </span>
                      <Button
                        variant="ghost"
                        onclick={() => forgetConverted(platform.id)}
                      >Forget history</Button>
                    </p>
                  {/if}
                {/if}
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
  .sweep-errors {
    margin: 0.5rem 0 0;
    padding-left: 1.1rem;
    color: var(--warn, #b45309);
    font-size: 0.85rem;
  }

  .forget {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    flex-wrap: wrap;
  }

  .rules-error {
    margin: 0 0 0.75rem;
    padding: 0.6rem 0.8rem;
    border: 1px solid var(--danger, #b3261e);
    border-radius: 6px;
    color: var(--danger, #b3261e);
    font-size: 0.9rem;
  }

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

  /* Codec chips for the multi-codec style (chdman). Mirrors CompressionPicker's
     affordance so the rule editor and the convert panel read the same way. */
  .chips { display: flex; flex-wrap: wrap; gap: 0.35rem; }
  .chip {
    padding: 0.25rem 0.6rem;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--surface-2);
    color: var(--text-1);
    font-size: var(--text-sm);
    cursor: pointer;
  }
  .chip:hover { border-color: var(--accent); }
  .chip.active {
    background: var(--accent);
    border-color: var(--accent);
    color: var(--accent-contrast, #fff);
  }

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
