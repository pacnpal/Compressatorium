// Global UI state: active view, sidebar/drawer, modal targets, app version.
//
// Theme management lives in mode-watcher (./App.svelte mounts the
// <ModeWatcher /> component); ThemeToggle reads userPrefersMode / mode
// directly from mode-watcher rather than going through this store.
//
// Toast notifications live in svelte-sonner; callers `import { toast } from
// 'svelte-sonner'` directly. The one piece this store still owns is the
// SSE connection-state toast (reportConnection) because it needs
// debouncing + sticky-toast-with-id tracking that's awkward to inline.

import { toast } from 'svelte-sonner';
import { STORAGE_KEYS, readBool, writeBool, readString, writeString } from '$lib/util/localStorage.js';
import { api } from '$lib/api/endpoints.js';
import { registry } from '$lib/tools/registry.js';

const VIEWS = Object.freeze(['dashboard', 'workspace', 'romm', 'dat', 'help']);
// Tool ids come from the registry, single source of truth, no
// hardcoded set to keep in sync when a 4th tool is added.
const VALID_TOOLS = registry.ids();
const DEFAULT_VIEW = 'workspace';

function loadPrimaryTool() {
  const raw = readString(STORAGE_KEYS.PRIMARY_TOOL, 'chdman');
  return VALID_TOOLS.has(raw) ? raw : 'chdman';
}

class UIStore {
  // Routing
  activeView = $state(DEFAULT_VIEW);
  workspaceTool = $state(loadPrimaryTool());
  // Tool ids the backend reports unavailable (e.g. Switch with no prod.keys).
  // Empty until the first /api/tools fetch, so nothing flickers hidden->shown.
  hiddenTools = $state(new Set());

  // Layout
  sidebarCollapsed = $state(readBool(STORAGE_KEYS.SIDEBAR_COLLAPSED, false));
  mobileDrawerOpen = $state(false);

  // Metadata from /api/version
  appVersion = $state(null);
  searchAutoReturnToFileList = $state(true);

  // Modal targets, one at a time
  infoTarget = $state(null);
  renameTarget = $state(null);
  deleteTarget = $state(null);
  bulkDeleteEntries = $state(null);
  bulkVerifyItems = $state(null);
  duplicateCheck = $state(null);
  deletePlan = $state(null);
  showCancelAll = $state(false);
  showClearDone = $state(false);
  showHelp = $state(false);
  // ConvertPanel awaits a per-submit user choice on duplicate outputs.
  // Flag is observed by App.svelte so background refresh-on-terminal
  // events can't swap entries out from under the open dialog.
  duplicatePromptOpen = $state(false);
  // Same idea for the delete-on-verify plan confirmation.
  deletePlanPromptOpen = $state(false);

  // Focus signal, bumped on view change so App.svelte can move focus to
  // the main landmark without screen readers losing context.
  focusBump = $state(0);

  /**
   * True when any modal that depends on file-listing state is open.
   * Used by App.svelte to suppress background refreshes that would
   * swap entries / selection / paging underneath an open dialog. The
   * pure-confirmation modals (cancel-all, clear-completed, help) are
   * intentionally excluded since they don't reference fileBrowser
   * entries.
   */
  get anyEntryModalOpen() {
    return (
      !!this.infoTarget ||
      !!this.renameTarget ||
      !!this.deleteTarget ||
      !!this.bulkDeleteEntries ||
      !!this.bulkVerifyItems ||
      !!this.duplicateCheck ||
      !!this.deletePlan ||
      this.duplicatePromptOpen ||
      this.deletePlanPromptOpen
    );
  }

  // SSE connection-state tracking. Internal, only reportConnection() touches.
  _connectionToastId = null;
  _disconnectTimer = null;

  toggleSidebar() {
    this.sidebarCollapsed = !this.sidebarCollapsed;
    writeBool(STORAGE_KEYS.SIDEBAR_COLLAPSED, this.sidebarCollapsed);
  }

  openDrawer() {
    this.mobileDrawerOpen = true;
  }

  closeDrawer() {
    this.mobileDrawerOpen = false;
  }

  /** Navigate by updating the URL hash; the router reads it back into state. */
  navigate(view, tool) {
    if (!VIEWS.includes(view)) return;
    let target = `#/${view}`;
    if (view === 'workspace' && tool && VALID_TOOLS.has(tool)) {
      target += `/${tool}`;
    }
    if (typeof window !== 'undefined') {
      if (window.location.hash === target) {
        // Force-apply if already on this hash (e.g. switching tools).
        this.applyHash(target);
      } else {
        window.location.hash = target;
      }
    }
  }

  applyHash(hash) {
    const cleaned = (hash ?? '').replace(/^#/, '').replace(/^\/+/, '');
    if (!cleaned) {
      this.activeView = DEFAULT_VIEW;
      this.focusBump++;
      return;
    }
    const [view, sub] = cleaned.split('/');
    if (VIEWS.includes(view)) {
      this.activeView = view;
      if (view === 'workspace' && sub && VALID_TOOLS.has(sub)) {
        this.workspaceTool = sub;
        writeString(STORAGE_KEYS.PRIMARY_TOOL, sub);
      }
    } else {
      this.activeView = DEFAULT_VIEW;
    }
    this.focusBump++;
  }

  setWorkspaceTool(toolId) {
    if (!VALID_TOOLS.has(toolId)) return;
    this.workspaceTool = toolId;
    writeString(STORAGE_KEYS.PRIMARY_TOOL, toolId);
    if (this.activeView === 'workspace') {
      this.navigate('workspace', toolId);
    }
  }

  /**
   * Apply the backend's tool-availability report (from GET /api/tools).
   * Hides tools the backend can't run (e.g. Switch without prod.keys), and if
   * the currently selected tool just got hidden, falls back to a visible one.
   */
  applyToolAvailability(available) {
    const allow = new Set(available ?? [...VALID_TOOLS]);
    this.hiddenTools = new Set([...VALID_TOOLS].filter((id) => !allow.has(id)));
    if (this.hiddenTools.has(this.workspaceTool)) {
      const fallback = registry.all().find((t) => !this.hiddenTools.has(t.id));
      if (fallback) this.setWorkspaceTool(fallback.id);
    }
  }

  /**
   * Called by the jobs SSE wrapper on connection state changes.
   * Brief drops (<1.5s) are NOT announced so we don't spam the toast on
   * momentary blips. On longer drops we open a sticky warning toast,
   * remember its id, and dismiss + surface a brief success toast on
   * recovery.
   */
  reportConnection(state) {
    if (state === 'open') {
      if (this._disconnectTimer) {
        clearTimeout(this._disconnectTimer);
        this._disconnectTimer = null;
      }
      if (this._connectionToastId != null) {
        toast.dismiss(this._connectionToastId);
        this._connectionToastId = null;
        toast.success('Reconnected to backend', { duration: 2500 });
      }
    } else if (state === 'reconnecting') {
      if (this._connectionToastId != null || this._disconnectTimer) return;
      this._disconnectTimer = setTimeout(() => {
        this._disconnectTimer = null;
        this._connectionToastId = toast.warning('Lost connection, retrying…', {
          duration: Number.POSITIVE_INFINITY,
        });
      }, 1500);
    }
  }

  async loadVersion() {
    try {
      const data = await api.getVersion();
      this.appVersion = data?.version ?? null;
      if (typeof data?.search_auto_return_to_file_list === 'boolean') {
        this.searchAutoReturnToFileList = data.search_auto_return_to_file_list;
      }
    } catch (_e) {
      this.appVersion = null;
    }
  }
}

export const ui = new UIStore();
