// Safe wrappers over window.localStorage. SSR-friendly (no-op if unavailable),
// JSON-encoded by default, never throws on quota or access errors.

const STORAGE_KEYS = Object.freeze({
  PRIMARY_TOOL: 'primary_tool_preference',
  SHOW_METADATA_JOBS: 'compressatorium_show_metadata_jobs',
  THEME: 'theme-preference',
  SIDEBAR_COLLAPSED: 'sidebar-collapsed',
  LAYOUT: 'workspace-layout',
  PAGE_SIZE: 'compressatorium_file_list_page_size',
});

function safe() {
  try {
    return typeof window !== 'undefined' && window.localStorage ? window.localStorage : null;
  } catch (_e) {
    return null;
  }
}

export function readString(key, fallback = null) {
  const ls = safe();
  if (!ls) return fallback;
  try {
    const v = ls.getItem(key);
    return v == null ? fallback : v;
  } catch (_e) {
    return fallback;
  }
}

export function writeString(key, value) {
  const ls = safe();
  if (!ls) return;
  try {
    if (value == null) ls.removeItem(key);
    else ls.setItem(key, String(value));
  } catch (_e) {
    // quota or access denied; ignore
  }
}

export function readBool(key, fallback = false) {
  const v = readString(key);
  if (v == null) return fallback;
  return v === 'true' || v === '1';
}

export function writeBool(key, value) {
  writeString(key, value ? 'true' : 'false');
}

export { STORAGE_KEYS };
