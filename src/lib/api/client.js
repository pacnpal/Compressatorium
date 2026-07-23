// Shared HTTP client: URL building, error normalization, action-confirm headers.

export const API_BASE = '/api';

/** Same-origin URL builder for /api endpoints, validated via URL constructor. */
export function buildApiUrl(endpoint, query) {
  const url = new URL(API_BASE + endpoint, window.location.origin);
  if (query instanceof URLSearchParams) {
    url.search = query.toString();
  } else if (typeof query === 'string' && query) {
    url.search = query;
  }
  return url.pathname + url.search;
}

/** Confirmation header values expected by the backend for destructive actions. */
export const CONFIRM = Object.freeze({
  CANCEL_ALL_JOBS: 'cancel-all-jobs',
  CLEAR_COMPLETED_JOBS: 'clear-completed-jobs',
  DELETE_ON_VERIFY: 'delete-on-verify',
  RENAME_FILE: 'rename-file',
  DELETE_FILE: 'delete-file',
  RECURSIVE_DELETE: 'recursive-delete',
});

/**
 * fetch wrapper that resolves to JSON and throws a normalized Error on non-2xx,
 * preferring the backend `{detail}` envelope as the message and attaching the
 * HTTP status as `err.status`. Tolerates empty bodies (204 / 205 / empty 200),
 * endpoints like cancelJob can legitimately return no payload.
 */
export async function fetchJson(url, opts = {}, fallbackMessage = 'Request failed') {
  const res = await fetch(url, opts);
  if (!res.ok) {
    let detail = fallbackMessage;
    try {
      const body = await res.json();
      if (body && typeof body.detail === 'string') detail = body.detail;
    } catch (_e) {
      // body wasn't JSON; keep fallbackMessage
    }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  if (res.status === 204 || res.status === 205) return {};
  const text = await res.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch (_e) {
    return {};
  }
}

/** Convenience for POST application/json. */
export function jsonPost(url, body, opts = {}, fallbackMessage) {
  // Destructure first so a caller-supplied `headers` doesn't replace the
  // Content-Type merge below, and so a caller-supplied `method`/`body` can't
  // overwrite the POST + JSON envelope by accident.
  const { headers = {}, method: _ignoredMethod, body: _ignoredBody, ...rest } = opts;
  return fetchJson(
    url,
    {
      ...rest,
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...headers },
      body: JSON.stringify(body),
    },
    fallbackMessage,
  );
}
