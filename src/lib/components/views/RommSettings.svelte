<script>
  /**
   * RomM connection settings, editable in-app.
   *
   * Environment variables remain first-run defaults, but nothing here requires
   * a redeploy to change. The connection test is deliberately granular —
   * reachable / authorised / mounted are three independent things to get right,
   * and "it doesn't work" is useless when it could be any of them.
   */
  import { toast } from 'svelte-sonner';
  import { romm } from '$lib/stores/romm.svelte.js';
  import Button from '$lib/components/ui/Button.svelte';
  import Checkbox from '$lib/components/ui/Checkbox.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Spinner from '$lib/components/ui/Spinner.svelte';
  import Check from '@lucide/svelte/icons/check';
  import XIcon from '@lucide/svelte/icons/x';
  import Plug from '@lucide/svelte/icons/plug';
  import Save from '@lucide/svelte/icons/save';

  let { onsaved } = $props();

  const s = $derived(romm.settings);

  // Local edit buffer so a half-typed URL never becomes live config.
  let url = $state('');
  let libraryRoot = $state('');
  let token = $state('');
  let clearToken = $state(false);
  let repinEnabled = $state(true);
  let repinOnLoad = $state(true);
  let repinAbandonDays = $state(7);
  let loaded = $state(false);

  $effect(() => {
    if (s && !loaded) {
      url = s.url ?? '';
      libraryRoot = s.library_root ?? '';
      repinEnabled = s.repin_enabled !== false;
      repinOnLoad = s.repin_on_load !== false;
      repinAbandonDays = s.repin_abandon_days ?? 7;
      loaded = true;
    }
  });

  const test = $derived(romm.testResult);
  const envPinned = $derived(new Set(s?.env_defaults ?? []));

  function patch() {
    const out = {
      url: url.trim(),
      library_root: libraryRoot.trim(),
      ...metadataPatch(),
    };
    if (clearToken) out.clear_token = true;
    else if (token.trim()) out.token = token.trim();
    return out;
  }

  /**
   * The Metadata card's fields, and only those.
   *
   * Saving here must never submit the connection buffers. They hold whatever
   * is half-typed in the other card, and a URL or library root that differs
   * from the saved one is an *identity change* on the backend: it clears the
   * conversion history and retires every pending metadata snapshot. Toggling
   * "re-apply on load" is not a reason to lose those.
   */
  function metadataPatch() {
    return {
      repin_enabled: repinEnabled,
      repin_on_load: repinOnLoad,
      repin_abandon_days: Number(repinAbandonDays) || undefined,
    };
  }

  async function save(body, message) {
    try {
      await romm.saveSettings(body);
      // Reset the credential buffers only when the request actually carried
      // them. They belong to the Connection card, and the Metadata card's Save
      // deliberately submits neither -- so clearing them here discarded a
      // replacement token the operator had typed and not yet saved. RomM shows
      // an API token once, at creation, so that is frequently a token nobody
      // can get back: the operator has to generate another one and update
      // anything else using it. The body is the authority on what was sent,
      // rather than a flag each call site has to remember to pass.
      if ('token' in body || 'clear_token' in body) {
        token = '';
        clearToken = false;
      }
      toast.success(message);
      onsaved?.();
    } catch (e) {
      toast.error(e?.message ?? 'Failed to save settings');
    }
  }

  const handleSave = () => save(patch(), 'RomM settings saved');
  const handleSaveMetadata = () =>
    save(metadataPatch(), 'Metadata settings saved');

  async function handleTest() {
    try {
      const result = await romm.testConnection(patch());
      if (result?.authorized && result?.library_root_mounted) {
        toast.success(`Connected to RomM ${result.version ?? ''}`.trim());
      } else if (result?.error) {
        toast.error(result.error);
      }
    } catch (e) {
      toast.error(e?.message ?? 'Connection test failed');
    }
  }
</script>

<div class="settings">
  <section class="card">
    <header>
      <h2>Connection</h2>
      <p>
        Compressatorium reads RomM's catalog over the API, but the ROM files
        themselves through the filesystem — no disc image is ever copied over
        HTTP.
      </p>
    </header>

    <label class="field">
      <span class="label">
        RomM URL
        {#if envPinned.has('url')}<Badge tone="neutral" size="sm">from env</Badge>{/if}
      </span>
      <input
        type="url"
        bind:value={url}
        placeholder="http://romm:8080"
        autocomplete="off"
        spellcheck="false"
      />
      <span class="hint">The base URL of your RomM instance.</span>
    </label>

    <label class="field">
      <span class="label">
        API token
        {#if s?.token_set}<Badge tone="success" size="sm">set</Badge>{/if}
      </span>
      <input
        type="password"
        bind:value={token}
        placeholder={s?.token_set ? '•••••••• (leave blank to keep)' : 'rmm_…'}
        autocomplete="new-password"
        spellcheck="false"
        disabled={clearToken}
      />
      <span class="hint">
        In RomM: <strong>Administration → Client API Tokens</strong>. Needs
        <code>platforms.read</code> and <code>roms.read</code>; add
        <code>roms.write</code> to let Compressatorium restore metadata after
        converting. The token is never shown again once saved.
      </span>
    </label>
    <!-- Outside the <label>: a label may own exactly one control, and with the
         checkbox inside it a click on the checkbox row could focus the password
         input instead, while assistive tech read one ambiguous name for both. -->
    {#if s?.token_set}
      <Checkbox bind:checked={clearToken} label="Remove the saved token" />
    {/if}

    <label class="field">
      <span class="label">
        Library path (in this container)
        {#if envPinned.has('library_root')}<Badge tone="neutral" size="sm">from env</Badge>{/if}
      </span>
      <input
        type="text"
        bind:value={libraryRoot}
        placeholder="/data/library"
        autocomplete="off"
        spellcheck="false"
      />
      <span class="hint">
        Where RomM's library folder is mounted <em>here</em> — not the path RomM
        sees. It must also be inside a configured Compressatorium volume.
      </span>
    </label>

    <div class="actions">
      <Button variant="secondary" onclick={handleTest} loading={romm.testing} icon={plugIcon}>
        Test connection
      </Button>
      <!-- Until the saved settings have loaded the buffers are empty, and
           submitting them would blank the URL and library root -- which the
           backend reads as a move to a different instance. -->
      <Button
        onclick={handleSave}
        loading={romm.settingsSaving}
        disabled={!loaded}
        icon={saveIcon}
      >
        Save
      </Button>
    </div>

    {#if romm.testing}
      <div class="test"><Spinner /> Testing…</div>
    {:else if test}
      <ul class="test-results">
        <li class:ok={test.reachable}>
          {#if test.reachable}<Check size={14} />{:else}<XIcon size={14} />{/if}
          RomM reachable
          {#if test.version}<span class="muted">(v{test.version})</span>{/if}
        </li>
        <li class:ok={test.authorized}>
          {#if test.authorized}<Check size={14} />{:else}<XIcon size={14} />{/if}
          Token accepted
          {#if test.platform_count !== null && test.platform_count !== undefined}
            <span class="muted">({test.platform_count} platforms)</span>
          {/if}
        </li>
        <li class:ok={test.library_root_mounted}>
          {#if test.library_root_mounted}<Check size={14} />{:else}<XIcon size={14} />{/if}
          Library folder mounted here
        </li>
      </ul>
      {#if test.error}<p class="error">{test.error}</p>{/if}
    {/if}
  </section>

  <section class="card">
    <header>
      <h2>Metadata</h2>
      <p>
        RomM identifies a CHD by the hash inside its header and an archive by its
        largest member, so those keep their Redump/No-Intro match automatically.
        RVZ, CSO, NSZ, WUX and Z3DS are matched on the file's own hash, which
        conversion changes — so their metadata is saved first and re-applied
        after RomM rescans.
      </p>
    </header>
    <Checkbox
      bind:checked={repinEnabled}
      label="Save metadata before converting to a format RomM can't hash"
    />
    <Checkbox
      bind:checked={repinOnLoad}
      label="Re-apply metadata automatically when this page loads"
    />
    <label class="field">
      <span>Give up on a saved snapshot after</span>
      <div class="days">
        <input type="number" min="1" max="365" bind:value={repinAbandonDays} />
        <span class="unit">days</span>
      </div>
      <span class="hint">
        A snapshot waits for RomM to rescan the converted file. Raise this if
        your conversion queue or your RomM scan schedule runs longer than that
        — retiring a snapshot early means that ROM's metadata is gone for good.
      </span>
    </label>
    <div class="actions">
      <Button
        onclick={handleSaveMetadata}
        loading={romm.settingsSaving}
        disabled={!loaded}
        icon={saveIcon}
      >
        Save
      </Button>
    </div>
  </section>
</div>

{#snippet plugIcon()}<Plug size={14} />{/snippet}
{#snippet saveIcon()}<Save size={14} />{/snippet}

<style>
  .days {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .days input {
    width: 6rem;
  }

  .unit {
    color: var(--text-muted, #6b7280);
    font-size: 0.9rem;
  }

  .settings {
    display: grid;
    gap: var(--space-4);
    max-width: 720px;
  }
  .card {
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
    display: grid;
    gap: var(--space-3);
  }
  header h2 {
    margin: 0;
    font-size: var(--text-lg);
    font-weight: var(--weight-semibold);
  }
  header p {
    margin: var(--space-1) 0 0;
    color: var(--text-2);
    font-size: var(--text-sm);
  }
  .field { display: grid; gap: var(--space-1); }
  .label {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-weight: var(--weight-medium);
  }
  input {
    width: 100%;
    padding: var(--space-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--surface-2);
    color: var(--text-1);
    font: inherit;
  }
  input:disabled { opacity: 0.5; }
  .hint { color: var(--text-2); font-size: var(--text-sm); }
  .hint code {
    background: var(--surface-2);
    padding: 0 4px;
    border-radius: var(--radius-sm);
  }
  .actions { display: flex; gap: var(--space-2); flex-wrap: wrap; }
  .test { display: flex; align-items: center; gap: var(--space-2); }
  .test-results {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: var(--space-1);
  }
  .test-results li {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    color: var(--text-2);
  }
  .test-results li.ok { color: var(--text-1); }
  .muted { color: var(--text-2); }
  .error { color: var(--danger-text, var(--text-1)); margin: 0; }
</style>
