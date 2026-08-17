// Verification store, tracks verified output paths, per-file verify
// progress, and the active batch-verify run. Single-file verify uses
// EventSource (sse.js); batch verify uses POST + ReadableStream (sseFetch.js)
// via the registry's verifyBatch binding.

import { SvelteMap, SvelteSet } from 'svelte/reactivity';
import { api } from '$lib/api/endpoints.js';
import { registry } from '$lib/tools/registry.js';

class VerificationStore {
  statuses = new SvelteSet();
  progress = new SvelteMap();
  batchRun = $state(null);
  _batchAbort = null;
  // Snapshot of statuses at the moment loadVerified() started, used
  // to detect paths added mid-flight (a `verifyOne` resolved, a
  // `complete` SSE arrived, etc.) so the response replay doesn't
  // erase them. Each loadVerified call overwrites the slot.
  _loadBaseline = null;

  async loadVerified() {
    // Snapshot the set we're about to overwrite. Anything in `statuses`
    // that's NOT in this baseline at completion time was added during
    // the in-flight request and must survive the replay.
    const baseline = new Set(this.statuses);
    this._loadBaseline = baseline;
    try {
      const data = await api.getVerifiedCHDs();
      // Preserve mid-flight additions: paths added to `statuses` after
      // we snapshotted `baseline` but before the response landed.
      // Plain object map for the membership check, same reason as
      // jobs.refresh's remoteIds (svelte/prefer-svelte-reactivity
      // flags raw Set, and SvelteSet here would be pointless
      // overhead for a transient local).
      const addedSince = Object.create(null);
      for (const p of this.statuses) {
        if (!baseline.has(p)) addedSince[p] = true;
      }
      this.statuses.clear();
      for (const path of data?.verified ?? []) this.statuses.add(path);
      for (const path of Object.keys(addedSince)) this.statuses.add(path);
    } catch (_e) {
      // non-fatal, leave whatever we had
    } finally {
      if (this._loadBaseline === baseline) this._loadBaseline = null;
    }
  }

  isVerified(path) {
    return path ? this.statuses.has(path) : false;
  }

  progressFor(path) {
    return path ? this.progress.get(path) ?? null : null;
  }

  // ─── Single-file verify ───────────────────────────────────────────────
  // `onProgress` (optional) is forwarded the same { percent, message }
  // shape stored in the progress map, so a caller can drive a live toast
  // without subscribing to the store. Progress reporting is best-effort:
  // a callback that throws must not fail the verify or strand the
  // progress entry, so every forward is swallowed.
  async verifyOne(toolId, path, { onProgress } = {}) {
    const tool = registry.forTool(toolId);
    if (!tool) throw new Error(`Unknown tool: ${toolId}`);
    const starting = { percent: 0, message: 'Starting…' };
    this.progress.set(path, starting);
    try {
      onProgress?.(starting);
    } catch (_e) {
      // a broken progress callback shouldn't break verification
    }
    try {
      const result = await tool.verify(path, {
        onProgress: (data) => {
          const next = {
            percent: typeof data?.progress === 'number' ? data.progress : null,
            message: data?.message ?? '',
          };
          this.progress.set(path, next);
          try {
            onProgress?.(next);
          } catch (_e) {
            // a broken progress callback shouldn't break verification
          }
        },
      });
      this.progress.delete(path);
      if (result?.valid) this.statuses.add(path);
      return result;
    } catch (e) {
      this.progress.delete(path);
      throw e;
    }
  }

  // ─── Batch verify ────────────────────────────────────────────────────
  async verifyBatch(toolId, paths) {
    const tool = registry.forTool(toolId);
    if (!tool) throw new Error(`Unknown tool: ${toolId}`);
    if (this._batchAbort) {
      // Refuse to start a second batch concurrently.
      throw new Error('Another batch verification is already running');
    }
    this._batchAbort = new AbortController();
    this.batchRun = {
      toolId,
      total: paths.length,
      done: 0,
      verified: 0,
      failed: 0,
      currentPath: null,
      currentFilename: null,
      currentPercent: null,
      message: '',
    };

    try {
      const result = await tool.verifyBatch(paths, {
        signal: this._batchAbort.signal,
        onProgress: (evt) => this._handleBatchProgress(evt),
        onFileComplete: (data) => {
          if (data?.path && data?.valid) this.statuses.add(data.path);
        },
      });
      if (this.batchRun) {
        // `aborted` means the backend stopped early because a verifier
        // survived SIGKILL and is still running: every remaining file lives on
        // the same storage and would strand another process. Reporting the run
        // as complete would hide that, so keep the real counts and say why.
        const aborted = Boolean(result?.aborted);
        const done = (result?.verified ?? 0) + (result?.failed ?? 0);
        this.batchRun = {
          ...this.batchRun,
          done: aborted ? done : this.batchRun.total,
          verified: result?.verified ?? this.batchRun.verified,
          failed: result?.failed ?? this.batchRun.failed,
          currentPath: null,
          currentFilename: null,
          currentPercent: aborted ? this.batchRun.currentPercent : 100,
          message: aborted
            ? (result?.message ?? 'Batch stopped early')
            : 'Batch complete',
        };
      }
      return result;
    } finally {
      this._batchAbort = null;
    }
  }

  cancelBatch() {
    if (this._batchAbort) {
      this._batchAbort.abort();
      this._batchAbort = null;
    }
    this.batchRun = null;
  }

  /**
   * Drop the batchRun summary left behind after a run completes
   * normally. cancelBatch() also aborts the in-flight fetch, which is
   * wrong for an already-finished run; this is the no-abort variant
   * the UI calls when dismissing a completed batch. Anything observing
   * `batchRun` as a "batch is active" signal (e.g. the auto-refresh
   * gate in App.svelte) needs this cleared or it stays blocked forever.
   */
  clearBatch() {
    this.batchRun = null;
  }

  _handleBatchProgress(evt) {
    if (!this.batchRun) return;
    switch (evt.type) {
      case 'start':
        this.batchRun = {
          ...this.batchRun,
          total: evt.total ?? this.batchRun.total,
        };
        break;
      case 'progress':
        this.batchRun = {
          ...this.batchRun,
          done: typeof evt.index === 'number' ? evt.index : this.batchRun.done,
          verified: evt.verified ?? this.batchRun.verified,
          failed: evt.failed ?? this.batchRun.failed,
          currentPath: evt.path ?? null,
          currentFilename: evt.filename ?? null,
        };
        break;
      case 'file_progress':
        this.batchRun = {
          ...this.batchRun,
          currentPercent: typeof evt.progress === 'number' ? evt.progress : null,
          message: evt.message ?? '',
        };
        break;
      case 'file_complete':
        this.batchRun = {
          ...this.batchRun,
          done: (typeof evt.index === 'number' ? evt.index : this.batchRun.done) + 1,
          verified: evt.verified ?? this.batchRun.verified,
          failed: evt.failed ?? this.batchRun.failed,
          currentPercent: 100,
        };
        break;
      default:
        break;
    }
  }
}

export const verification = new VerificationStore();
