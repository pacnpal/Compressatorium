// All REST endpoints to the FastAPI backend, grouped by feature.
// Direct port of static/js/api.js; every URL, header, and payload preserved.

import { API_BASE, CONFIRM, buildApiUrl, fetchJson, jsonPost } from './client.js';
import { sseConnectNamed, verifyEventSource } from './sse.js';
import { sseFetchPost } from './sseFetch.js';

const dispatchBatchEvent = (event, { onProgress, onFileComplete }) => {
  switch (event.type) {
    case 'verify_batch_start':
      onProgress?.({ type: 'start', ...event.data });
      return false;
    case 'verify_batch_progress':
      onProgress?.({ type: 'progress', ...event.data });
      return false;
    case 'verify_batch_file_progress':
      onProgress?.({ type: 'file_progress', ...event.data });
      return false;
    case 'verify_batch_file_complete':
      onFileComplete?.(event.data);
      onProgress?.({ type: 'file_complete', ...event.data });
      return false;
    case 'verify_batch_complete':
      return event.data; // signals completion with final result
    default:
      return false;
  }
};

const runBatchVerify = async (url, paths, { onProgress, onFileComplete, signal } = {}) => {
  let finalResult = { total: 0, verified: 0, failed: 0 };
  let completed = false;
  await sseFetchPost(
    url,
    { paths },
    {
      signal,
      fallbackMessage: 'Failed to start batch verification',
      onEvent: (event) => {
        const result = dispatchBatchEvent(event, { onProgress, onFileComplete });
        if (result && typeof result === 'object') {
          finalResult = result;
          completed = true;
        }
      },
    },
  );
  if (!completed) {
    if (signal?.aborted) {
      throw new DOMException('Aborted', 'AbortError');
    }
    // A stream that drains without verify_batch_complete is a failure mode
    // (proxy timeout, backend crash). Surface it rather than silently
    // returning the zero-counts default, callers update UI based on the
    // returned result and would otherwise show "0 verified" as success.
    const err = new Error('Batch verification stream ended before completion');
    err.partial = finalResult;
    throw err;
  }
  return finalResult;
};

export const api = {
  // ─── Version ──────────────────────────────────────────────────────────
  getVersion: () => fetchJson(`${API_BASE}/version`, undefined, 'Failed to fetch version'),

  // ─── Volumes ──────────────────────────────────────────────────────────
  getVolumes: () => fetchJson(`${API_BASE}/volumes`, undefined, 'Failed to fetch volumes'),

  // ─── Preferences (server-stored UI layout) ─────────────────────────────
  getPreferences: () =>
    fetchJson(`${API_BASE}/preferences`, undefined, 'Failed to fetch preferences'),
  getConversionPrefs: () =>
    fetchJson(
      `${API_BASE}/preferences/conversion`,
      undefined,
      'Failed to fetch compression preferences',
    ),
  putConversionPrefs(prefs) {
    return fetchJson(
      `${API_BASE}/preferences/conversion`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(prefs),
      },
      'Failed to save compression preferences',
    );
  },
  putPreferences(layout) {
    return fetchJson(
      `${API_BASE}/preferences`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(layout),
      },
      'Failed to save preferences',
    );
  },

  // ─── Files ────────────────────────────────────────────────────────────
  // summarizeArchives=false keeps the directory listing fast in folders with
  // thousands of archives: archive rows come back without member counts /
  // verifiable_by, which the browser then hydrates via getArchiveSummaryBatch
  // (mirroring CHD media_type hydration). Pass true for a one-shot inline
  // summary when not hydrating separately.
  listFiles(path, showArchives = true, summarizeArchives = false) {
    const params = new URLSearchParams({
      path,
      show_archives: String(showArchives),
      summarize_archives: String(summarizeArchives),
    });
    return fetchJson(buildApiUrl('/files', params), undefined, 'Failed to list files');
  },

  // Per-archive summaries (member counts, verifiable_by) for the lazy listing.
  getArchiveSummaryBatch(paths) {
    if (!paths?.length) return Promise.resolve({});
    return jsonPost(
      buildApiUrl('/archive-summary'), { paths }, {}, 'Failed to fetch archive summaries',
    );
  },

  searchFiles(path, recursive = true, includeArchives = true) {
    const params = new URLSearchParams({
      path,
      recursive: String(recursive),
      include_archives: String(includeArchives),
    });
    return fetchJson(buildApiUrl('/files/search', params), undefined, 'Failed to search files');
  },

  listArchive(path) {
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/files/archive', params), undefined, 'Failed to list archive');
  },

  renameFile(path, newName) {
    const params = new URLSearchParams({ path, new_name: newName });
    return fetchJson(
      buildApiUrl('/files/rename', params),
      { method: 'POST', headers: { 'X-CHD-Action-Confirm': CONFIRM.RENAME_FILE } },
      'Failed to rename',
    );
  },

  deleteFile(path, { recursive = false } = {}) {
    const params = new URLSearchParams({ path });
    if (recursive) params.set('recursive', 'true');
    const headers = {
      'X-CHD-Action-Confirm': recursive ? CONFIRM.RECURSIVE_DELETE : CONFIRM.DELETE_FILE,
    };
    return fetchJson(
      buildApiUrl('/files/delete', params),
      { method: 'DELETE', headers },
      'Failed to delete',
    );
  },

  deleteBatch(paths) {
    return jsonPost(
      `${API_BASE}/files/delete-batch`,
      { paths },
      { headers: { 'X-CHD-Action-Confirm': CONFIRM.DELETE_FILE } },
      'Failed to delete files',
    );
  },

  // ─── Jobs ─────────────────────────────────────────────────────────────
  createJob(
    filePath,
    mode = 'createcd',
    outputDir = null,
    compression = null,
    deleteOnVerify = false,
    split = false,
  ) {
    return jsonPost(
      `${API_BASE}/jobs`,
      {
        file_path: filePath,
        mode,
        output_dir: outputDir,
        compression,
        delete_on_verify: deleteOnVerify,
        split,
      },
      {},
      'Failed to create job',
    );
  },

  createBatchJobs(
    filePaths,
    mode = 'createcd',
    outputDir = null,
    duplicateAction = 'skip',
    compression = null,
    deleteOnVerify = false,
    split = false,
  ) {
    return jsonPost(
      `${API_BASE}/jobs/batch`,
      {
        file_paths: filePaths,
        mode,
        output_dir: outputDir,
        duplicate_action: duplicateAction,
        compression,
        delete_on_verify: deleteOnVerify,
        split,
      },
      {},
      'Failed to create jobs',
    );
  },

  checkDuplicates(filePaths, outputDir = null, mode = 'createcd') {
    return jsonPost(
      `${API_BASE}/jobs/check-duplicates`,
      { file_paths: filePaths, output_dir: outputDir, mode },
      {},
      'Failed to check duplicates',
    );
  },

  getDeletePlan(filePaths, mode = 'createcd') {
    return jsonPost(
      `${API_BASE}/jobs/delete-plan`,
      { file_paths: filePaths, mode },
      {},
      'Failed to build delete plan',
    );
  },

  getJobs: () => fetchJson(`${API_BASE}/jobs`, undefined, 'Failed to fetch jobs'),

  getJob: (jobId) => fetchJson(`${API_BASE}/jobs/${jobId}`, undefined, 'Failed to fetch job'),

  cancelJob: (jobId) =>
    fetchJson(`${API_BASE}/jobs/${jobId}`, { method: 'DELETE' }, 'Failed to cancel job'),

  cancelAllJobs: () =>
    fetchJson(
      `${API_BASE}/jobs/cancel-all`,
      { method: 'POST', headers: { 'X-CHD-Action-Confirm': CONFIRM.CANCEL_ALL_JOBS } },
      'Failed to cancel all jobs',
    ),

  deleteCompletedJobs: () =>
    fetchJson(
      `${API_BASE}/jobs/completed`,
      { method: 'DELETE', headers: { 'X-CHD-Action-Confirm': CONFIRM.CLEAR_COMPLETED_JOBS } },
      'Failed to delete completed jobs',
    ),

  checkStuckStatus: () =>
    fetchJson(`${API_BASE}/jobs/stuck-status`, undefined, 'Failed to check stuck status'),

  recoverStuckJobs: () =>
    fetchJson(`${API_BASE}/jobs/recover`, { method: 'POST' }, 'Failed to recover stuck jobs'),

  /**
   * Subscribe to the global job event stream. Auto-reconnects on error.
   * Returns an unsubscribe function.
   *
   * Includes `snapshot` so the one-time hydration emission the backend
   * sends on connect (and re-sends after each reconnect) flows through
   * the same handler as live updates. See convert.py:event_generator.
   *
   * @param {(evt: { type: string, data: any }) => void} onEvent
   * @param {{onOpen?: () => void, onReconnecting?: () => void}} [status]
   */
  subscribeToJobs(onEvent, status) {
    const conn = sseConnectNamed(
      `${API_BASE}/jobs/events`,
      ['snapshot', 'progress', 'complete', 'error', 'status', 'cancelled'],
      onEvent,
      status,
    );
    return () => conn.close();
  },

  // ─── Tool info ────────────────────────────────────────────────────────
  getCHDInfo(path) {
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/info', params), undefined, 'Failed to get CHD info');
  },

  getDolphinInfo(path) {
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/dolphin-info', params), undefined, 'Failed to get disc info');
  },

  getZ3DSInfo(path) {
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/z3ds-info', params), undefined, 'Failed to get 3DS ROM info');
  },

  getNszInfo(path) {
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/nsz-info', params), undefined, 'Failed to get Switch file info');
  },

  getCsoInfo(path) {
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/cso-info', params), undefined, 'Failed to get CSO info');
  },

  getRomzInfo(path) {
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/romz-info', params), undefined, 'Failed to get ROM info');
  },

  getJwudInfo(path) {
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/jwud-info', params), undefined, 'Failed to get Wii U image info');
  },

  // Which tools the UI should show. Switch is reported unavailable when no
  // prod.keys are configured, so the sidebar can hide it entirely.
  getTools() {
    return fetchJson(`${API_BASE}/tools`, undefined, 'Failed to load tool availability');
  },

  // ─── Single-file verify (SSE + sync fallback) ─────────────────────────
  verifyCHD(path, { onProgress } = {}) {
    if (onProgress) {
      const params = new URLSearchParams({ path });
      return verifyEventSource(buildApiUrl('/verify/events', params), onProgress, {
        failureFallback: 'CHD verification failed',
      });
    }
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/verify', params), undefined, 'Failed to verify CHD');
  },

  verifyDolphin(path, { onProgress } = {}) {
    if (onProgress) {
      const params = new URLSearchParams({ path });
      return verifyEventSource(buildApiUrl('/dolphin-verify/events', params), onProgress, {
        failureFallback: 'Disc verification failed',
      });
    }
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/dolphin-verify', params), undefined, 'Failed to verify disc');
  },

  verify3DS(path, { onProgress } = {}) {
    if (onProgress) {
      const params = new URLSearchParams({ path });
      return verifyEventSource(buildApiUrl('/z3ds-verify/events', params), onProgress, {
        failureFallback: '3DS verification failed',
      });
    }
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/z3ds-verify', params), undefined, 'Failed to verify 3DS ROM');
  },

  verifyNsz(path, { onProgress } = {}) {
    if (onProgress) {
      const params = new URLSearchParams({ path });
      return verifyEventSource(buildApiUrl('/nsz-verify/events', params), onProgress, {
        failureFallback: 'Switch verification failed',
      });
    }
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/nsz-verify', params), undefined, 'Failed to verify Switch file');
  },

  verifyCso(path, { onProgress } = {}) {
    if (onProgress) {
      const params = new URLSearchParams({ path });
      return verifyEventSource(buildApiUrl('/cso-verify/events', params), onProgress, {
        failureFallback: 'CSO verification failed',
      });
    }
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/cso-verify', params), undefined, 'Failed to verify CSO file');
  },

  verifyRomz(path, { onProgress } = {}) {
    if (onProgress) {
      const params = new URLSearchParams({ path });
      return verifyEventSource(buildApiUrl('/romz-verify/events', params), onProgress, {
        failureFallback: 'ROM archive verification failed',
      });
    }
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/romz-verify', params), undefined, 'Failed to verify ROM archive');
  },

  verifyJwud(path, { onProgress } = {}) {
    if (onProgress) {
      const params = new URLSearchParams({ path });
      return verifyEventSource(buildApiUrl('/jwud-verify/events', params), onProgress, {
        failureFallback: 'Wii U verification failed',
      });
    }
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/jwud-verify', params), undefined, 'Failed to verify Wii U image');
  },

  getVerifiedCHDs: () =>
    fetchJson(`${API_BASE}/verified`, undefined, 'Failed to fetch verified CHDs'),

  // ─── Batch verify (POST + ReadableStream SSE) ─────────────────────────
  verifyBatchCHDs(paths, opts) {
    return runBatchVerify(`${API_BASE}/verify-batch/events`, paths, opts);
  },

  verifyBatchDolphin(paths, opts) {
    return runBatchVerify(`${API_BASE}/dolphin-verify-batch/events`, paths, opts);
  },

  verifyBatchZ3DS(paths, opts) {
    return runBatchVerify(`${API_BASE}/z3ds-verify-batch/events`, paths, opts);
  },

  verifyBatchNsz(paths, opts) {
    return runBatchVerify(`${API_BASE}/nsz-verify-batch/events`, paths, opts);
  },

  verifyBatchCso(paths, opts) {
    return runBatchVerify(`${API_BASE}/cso-verify-batch/events`, paths, opts);
  },

  verifyBatchRomz(paths, opts) {
    return runBatchVerify(`${API_BASE}/romz-verify-batch/events`, paths, opts);
  },

  verifyBatchJwud(paths, opts) {
    return runBatchVerify(`${API_BASE}/jwud-verify-batch/events`, paths, opts);
  },

  // ─── CHD metadata cache ───────────────────────────────────────────────
  getCHDMetadataBatch(paths) {
    if (!paths || paths.length === 0) return Promise.resolve({});
    return jsonPost(`${API_BASE}/chd-metadata`, { paths }, {}, 'Failed to fetch CHD metadata');
  },

  scanMetadata(force = false) {
    const suffix = force ? '?force=true' : '';
    return fetchJson(
      `${API_BASE}/chd-metadata/scan${suffix}`,
      { method: 'POST' },
      'Failed to start metadata scan',
    );
  },

  getScanStatus: () =>
    fetchJson(`${API_BASE}/chd-metadata/scan/status`, undefined, 'Failed to get scan status'),

  // ─── DAT library ──────────────────────────────────────────────────────
  importDAT(file) {
    const formData = new FormData();
    formData.append('file', file);
    return fetchJson(
      `${API_BASE}/dat/import`,
      { method: 'POST', body: formData },
      'Failed to import DAT',
    );
  },

  listDATs: () => fetchJson(`${API_BASE}/dat/list`, undefined, 'Failed to list DATs'),

  deleteDAT: (datId) =>
    fetchJson(`${API_BASE}/dat/${datId}`, { method: 'DELETE' }, 'Failed to delete DAT'),

  getDATStats: () => fetchJson(`${API_BASE}/dat/stats`, undefined, 'Failed to get DAT stats'),

  matchBatch(paths) {
    return jsonPost(`${API_BASE}/dat/match-batch`, { paths }, {}, 'Failed to match files');
  },

  startMatchJob(paths) {
    return jsonPost(
      `${API_BASE}/dat/match-batch/job`,
      { paths },
      {},
      'Failed to start DAT match job',
    );
  },

  getMatchCache(paths) {
    if (!paths || paths.length === 0) return Promise.resolve({ results: {} });
    return jsonPost(
      `${API_BASE}/dat/matches/lookup`,
      { paths },
      {},
      'Failed to look up DAT match cache',
    );
  },

  syncMAMERedump(tag = null) {
    return jsonPost(
      `${API_BASE}/dat/sync`,
      tag ? { tag } : {},
      {},
      'Failed to start sync',
    );
  },

  getSyncStatus: () =>
    fetchJson(`${API_BASE}/dat/sync/status`, undefined, 'Failed to get sync status'),

  cancelSync: () =>
    fetchJson(`${API_BASE}/dat/sync/cancel`, { method: 'POST' }, 'Failed to cancel sync'),
};
