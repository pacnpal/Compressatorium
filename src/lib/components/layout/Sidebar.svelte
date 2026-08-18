<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import SidebarItem from './SidebarItem.svelte';
  import LayoutDashboard from '@lucide/svelte/icons/layout-dashboard';
  import Boxes from '@lucide/svelte/icons/boxes';
  import Database from '@lucide/svelte/icons/database';
  import Library from '@lucide/svelte/icons/library';
  import HelpCircle from '@lucide/svelte/icons/circle-help';
  import ChevronLeft from '@lucide/svelte/icons/chevron-left';
  import ChevronRight from '@lucide/svelte/icons/chevron-right';

  const collapsed = $derived(ui.sidebarCollapsed);
  const view = $derived(ui.activeView);
  const tool = $derived(ui.workspaceTool);
  const tools = $derived(registry.all().filter((t) => !ui.hiddenTools.has(t.id)));

  function go(target, t) {
    ui.closeDrawer();
    ui.navigate(target, t);
  }
</script>

<aside class="sidebar" class:collapsed aria-label="Primary navigation">
  <div class="brand">
    <img class="brand-logo" src="/static/images/logo.png" alt="" aria-hidden="true" width="32" height="32" />
    {#if !collapsed}<span class="brand-text">Compressatorium</span>{/if}
  </div>

  <nav class="section">
    {#if !collapsed}<h2 class="section-title">Navigate</h2>{/if}
    <SidebarItem label="Workspace" {collapsed} active={view === 'workspace'} onclick={() => go('workspace', tool)}>
      {#snippet icon()}<Boxes size={16} />{/snippet}
    </SidebarItem>
    <SidebarItem label="Dashboard" {collapsed} active={view === 'dashboard'} onclick={() => go('dashboard')}>
      {#snippet icon()}<LayoutDashboard size={16} />{/snippet}
    </SidebarItem>
    <SidebarItem label="RomM" {collapsed} active={view === 'romm'} onclick={() => go('romm')}>
      {#snippet icon()}<Library size={16} />{/snippet}
    </SidebarItem>
    <SidebarItem label="DAT Library" {collapsed} active={view === 'dat'} onclick={() => go('dat')}>
      {#snippet icon()}<Database size={16} />{/snippet}
    </SidebarItem>
    <SidebarItem label="Help" {collapsed} active={view === 'help'} onclick={() => go('help')}>
      {#snippet icon()}<HelpCircle size={16} />{/snippet}
    </SidebarItem>
  </nav>

  <nav class="section tools">
    {#if !collapsed}<h2 class="section-title">Tool</h2>{/if}
    {#each tools as t (t.id)}
      <SidebarItem
        label={t.label}
        title={t.hint}
        {collapsed}
        active={view === 'workspace' && tool === t.id}
        onclick={() => go('workspace', t.id)}
      >
        {#snippet icon()}
          <span class="tool-glyph" style:color={t.accent ?? 'var(--accent)'} aria-hidden="true">
            {t.glyph ?? t.label[0]}
          </span>
        {/snippet}
      </SidebarItem>
    {/each}
  </nav>

  <nav class="section view">
    <SidebarItem
      label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
      {collapsed}
      onclick={() => ui.toggleSidebar()}
    >
      {#snippet icon()}
        {#if collapsed}<ChevronRight size={16} />{:else}<ChevronLeft size={16} />{/if}
      {/snippet}
    </SidebarItem>
  </nav>

  <div class="spacer"></div>
</aside>

<style>
  .sidebar {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    width: var(--sidebar-width);
    padding: var(--space-3);
    background: var(--surface-1);
    border-right: 1px solid var(--border-subtle);
    overflow-y: auto;
    transition: width var(--dur-base) var(--ease-out);
  }
  .collapsed {
    width: var(--sidebar-collapsed-width);
    padding: var(--space-3) var(--space-2);
  }
  .brand {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2);
    color: var(--text-1);
  }
  .brand-logo {
    width: 32px;
    height: 32px;
    border-radius: var(--radius-sm);
    flex-shrink: 0;
    object-fit: contain;
  }
  .brand-text {
    font-weight: var(--weight-semibold);
    font-size: var(--text-lg);
    flex-shrink: 0;
    white-space: nowrap;
  }
  .collapsed .brand { justify-content: center; padding: var(--space-1); }
  .section { display: flex; flex-direction: column; gap: 2px; }
  .section.view {
    border-top: 1px solid var(--border-subtle);
    padding-top: var(--space-2);
  }
  .section-title {
    margin: var(--space-2) var(--space-3) var(--space-1);
    font-size: var(--text-xs);
    font-weight: var(--weight-semibold);
    color: var(--text-3);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .tool-glyph {
    font-weight: var(--weight-bold);
    font-size: 11px;
    letter-spacing: 0.02em;
    line-height: 1;
  }
  .spacer { flex: 1; }
</style>
