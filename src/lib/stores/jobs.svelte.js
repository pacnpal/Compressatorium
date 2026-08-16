// Job queue store, owns the single /api/jobs/events SSE connection (auto-
// reconnecting). All job mutations come through here so components see a
// single source of truth.

import { SvelteMap, SvelteSet } from 'svelte/reactivity';
import { api } from '$lib/api/endpoints.js';
import { STORAGE_KEYS, readBool, writeBool } from '$lib/util/localStorage.js';
import { verification } from './verification.svelte.js';
import { ui } from './ui.svelte.js';
import {
  ACTIVE_STATUSES,
  EXTERNAL_SCAN_MODES,
  TERMINAL_STATUSES,
} from './jobConstants.js';

const MAX_OPTIMISTIC_PLACEHOLDERS = 100;

class JobsStore {
  jobs = $state([]);
  creatingJobs = $state([]);
  hiddenIds = new SvelteSet();
  tab = $state('queue');
  pageSize = $state(50);
  page = $state(1);
  stuckState = $state(null);
  showExternalScanJobs = $state(readBool(STORAGE_KEYS.SHOW_METADATA_JOBS, false));

  // Finished jobs the backend's MAX_JOB_HISTORY cap has already evicted,
  // keyed status → mode → count, plus the cap itself. The jobs list can
  // never hold more than the cap, so every count derived from it alone
  // freezes there ("Completed 500" while the queue is still draining
  // thousands). Retained + evicted is the real number. Fed by the `history`
  // SSE event and re-read on refresh(); see convert.py:event_generator.
  historyOverflow = $state({});
  historyLimit = $state(0);
  // Sequence of the newest overflow state applied. The REST read in refresh()
  // races the `history` stream, so without it a hydration response captured
  // before a newer event could land after it and roll the totals backwards —
  // and since the backend only re-emits on change, they'd stay wrong until the
  // next poll. Every change to the tally (evictions and Clear alike) advances
  // the sequence, so an older payload is always recognizable.
  historySeq = -1;
  // Which backend process the sequence belongs to. The sequence is in-memory
  // and restarts at 0 with the backend, so without this a restart would make
  // every payload from the new process look older than what we hold and the
  // guard above would reject all of them — badges frozen for the rest of the
  // session. A changed generation means: drop the cursor, trust the payload.
  historyGeneration = null;

  cancellingAll = $state(false);
  clearingCompleted = $state(false);
  recoveringStuck = $state(false);
  loading = $state(false);

  // index for O(1) lookup; not a $state because it's derived from this.jobs
  _byId = new SvelteMap();
  _unsubscribe = null;
  _pollTimer = null;

  get activeCount() {
    return this.jobs.reduce((n, j) => (ACTIVE_STATUSES.has(j.status) ? n + 1 : n), 0);
  }

  get hasActive() {
    return this.activeCount > 0;
  }

  get queuedCount() {
    return this.jobs.reduce((n, j) => (j.status === 'queued' ? n + 1 : n), 0);
  }

  get processingCount() {
    return this.jobs.reduce((n, j) => (j.status === 'processing' ? n + 1 : n), 0);
  }

  /**
   * Sum the evicted-history tally for `statuses`.
   *
   * `visibleOnly` applies the same external-scan filter the job list uses,
   * so turning `Show metadata jobs` off doesn't leave pruned metadata scans
   * silently inflating a tab badge. Locally hidden ids can't be honoured
   * here — an evicted job no longer has a row to hide — so a job the user
   * dismissed locally rejoins the total once the cap evicts it. That's a
   * cosmetic edge on a count that was previously wrong by thousands.
   */
  _evictedCount(statuses, visibleOnly = false) {
    let total = 0;
    for (const status of statuses) {
      const byMode = this.historyOverflow?.[status];
      if (!byMode) continue;
      for (const [mode, count] of Object.entries(byMode)) {
        if (visibleOnly && !this.showExternalScanJobs && EXTERNAL_SCAN_MODES.has(mode)) continue;
        total += count || 0;
      }
    }
    return total;
  }

  // Terminal-status counts include jobs the history cap has evicted; the
  // work happened, and the dashboard's Done / Failed tiles are totals for
  // the run, not a measure of how much history we happen to be holding.
  // Queued / processing counts need no such treatment: pruning only ever
  // touches terminal jobs, so the live list is already complete.
  get completedCount() {
    return (
      this.jobs.reduce((n, j) => (j.status === 'completed' ? n + 1 : n), 0)
      + this._evictedCount(['completed'])
    );
  }

  get failedCount() {
    return (
      this.jobs.reduce((n, j) => (j.status === 'failed' ? n + 1 : n), 0)
      + this._evictedCount(['failed'])
    );
  }

  get cancelledCount() {
    return (
      this.jobs.reduce((n, j) => (j.status === 'cancelled' ? n + 1 : n), 0)
      + this._evictedCount(['cancelled'])
    );
  }

  /**
   * Counts scoped to the same predicate as `visibleJobs`, i.e. they
   * exclude jobs the user has locally hidden and (when the metadata
   * toggle is off) external-scan modes. JobsPanel tab badges + the
   * Cancel-all / Clear-all gating use these so a hidden DAT-match or
   * metadata-scan can't drive a count > 0 while the row list is
   * empty, and so destructive actions never operate on jobs the
   * user explicitly chose to hide.
   */
  _matchesTabFilter(job, tab) {
    if (this.hiddenIds.has(job.id)) return false;
    if (!this.showExternalScanJobs && EXTERNAL_SCAN_MODES.has(job.mode)) return false;
    switch (tab) {
      case 'queue':
        return ACTIVE_STATUSES.has(job.status);
      case 'completed':
        return job.status === 'completed';
      case 'failed':
        return job.status === 'failed' || job.status === 'cancelled';
      default:
        return true;
    }
  }

  get visibleQueuedCount() {
    return this.jobs.reduce(
      (n, j) => (this._matchesTabFilter(j, 'queue') ? n + 1 : n),
      0,
    );
  }

  get visibleCompletedCount() {
    return (
      this.jobs.reduce((n, j) => (this._matchesTabFilter(j, 'completed') ? n + 1 : n), 0)
      + this._evictedCount(['completed'], true)
    );
  }

  get visibleFailedCount() {
    return (
      this.jobs.reduce((n, j) => (this._matchesTabFilter(j, 'failed') ? n + 1 : n), 0)
      + this._evictedCount(['failed', 'cancelled'], true)
    );
  }

  /** Filter by current `tab` + external-scan toggle + locally hidden ids. */
  get visibleJobs() {
    const filtered = this.jobs.filter((job) => {
      if (this.hiddenIds.has(job.id)) return false;
      if (!this.showExternalScanJobs && EXTERNAL_SCAN_MODES.has(job.mode)) return false;
      switch (this.tab) {
        case 'queue':
          return ACTIVE_STATUSES.has(job.status);
        case 'completed':
          return job.status === 'completed';
        case 'failed':
          return job.status === 'failed' || job.status === 'cancelled';
        default:
          return true;
      }
    });
    return filtered;
  }

  /** Rows the current tab can actually list — pagination spans these only. */
  get retainedCount() {
    return this.visibleJobs.length;
  }

  /**
   * Jobs matching the current tab that the history cap has already evicted,
   * i.e. counted in the tab badge but no longer listable. Non-zero only past
   * the cap; the panel uses it to explain the gap rather than let the badge
   * and the pager silently disagree.
   */
  get trimmedCount() {
    if (this.tab === 'completed') return this._evictedCount(['completed'], true);
    if (this.tab === 'failed') return this._evictedCount(['failed', 'cancelled'], true);
    return 0;
  }

  get pageCount() {
    return Math.max(1, Math.ceil(this.visibleJobs.length / this.pageSize));
  }

  /** Slice of `visibleJobs` for the current page. */
  get pageJobs() {
    const start = (this.page - 1) * this.pageSize;
    return this.visibleJobs.slice(start, start + this.pageSize);
  }

  setTab(tab) {
    this.tab = tab;
    this.page = 1;
  }

  setShowExternalScanJobs(value) {
    this.showExternalScanJobs = !!value;
    writeBool(STORAGE_KEYS.SHOW_METADATA_JOBS, this.showExternalScanJobs);
  }

  hideLocally(jobId) {
    this.hiddenIds.add(jobId);
  }

  // ─── Mutations ────────────────────────────────────────────────────────
  _applyJob(job) {
    if (!job?.id) return;
    const existing = this._byId.get(job.id);
    if (existing) {
      // Replace the slot via index assignment rather than Object.assign on
      // the existing reference. Svelte 5's deep-proxy tracks $state arrays
      // at index granularity, and consumers iterating `jobs` (via {#each})
      // re-render on slot replacement, but Object.assign on an outside
      // reference may not propagate when the reference is also stored in
      // _byId, leading to stale progress/status until an unrelated change.
      const idx = this.jobs.findIndex((j) => j.id === job.id);
      if (idx !== -1) {
        this.jobs[idx] = job;
      } else {
        this.jobs.push(job);
      }
      this._byId.set(job.id, job);
    } else {
      this.jobs.push(job);
      this._byId.set(job.id, job);
      if (job.file_path) {
        // First arrival only: clear exactly one matching optimistic
        // placeholder (the oldest). _applyJob also runs on every later SSE
        // progress/status update for an already-known job; re-clearing then
        // would drop a placeholder belonging to a *second* in-flight
        // same-file/same-mode submission before its own job arrives — the
        // under-count this guards against. See _removeOptimistic.
        this._removeOptimistic(job.file_path, job.mode);
      }
    }
  }

  _replaceAll(jobs) {
    this.jobs = jobs.slice();
    this._byId.clear();
    for (const job of jobs) this._byId.set(job.id, job);
  }

  /**
   * Absolute evicted-history totals from the backend
   * ({evicted, max_job_history, evicted_ids}).
   *
   * `evicted_ids` (SSE only) names jobs the cap deleted since our last event.
   * We must drop those rows before taking the new tally: a completion reaches
   * us over SSE *before* the prune it triggers, so for a moment we hold a row
   * the backend has already deleted, and counting it as retained *and* as
   * evicted would inflate every badge — by one per completion for as long as
   * a batch runs past the cap, until refresh()'s deletion reconcile caught up.
   */
  _applyHistoryOverflow(payload) {
    if (!payload || typeof payload !== 'object') return;
    // A new backend process restarts the sequence at 0, so the cursor from
    // the old one is meaningless — adopt the new generation and take its
    // payload as the baseline rather than comparing across processes.
    const generation = payload.generation ?? null;
    if (generation !== this.historyGeneration) {
      this.historyGeneration = generation;
      this.historySeq = -1;
    }
    // Drop payloads older than what we've already applied. Equal sequences
    // carry equal counts (nothing changes the tally without advancing it), so
    // re-applying those is a no-op rather than a regression.
    const seq = payload.seq;
    if (typeof seq === 'number') {
      if (seq < this.historySeq) return;
      this.historySeq = seq;
    }
    const evictedIds = payload.evicted_ids;
    if (Array.isArray(evictedIds) && evictedIds.length > 0) {
      // Plain object as an id→true map, matching refresh(): the svelte-eslint
      // `prefer-svelte-reactivity` rule flags raw Set usage even for locals.
      const gone = Object.create(null);
      for (const id of evictedIds) gone[id] = true;
      this.jobs = this.jobs.filter((j) => {
        if (gone[j.id]) {
          this._byId.delete(j.id);
          return false;
        }
        return true;
      });
    }
    this.historyOverflow = payload.evicted ?? {};
    this.historyLimit = payload.max_job_history ?? 0;
  }

  /**
   * Hydrate / re-sync from the REST snapshot.
   *
   * Designed to be called AFTER connect() so that any terminal events
   * (`complete` / `error` / `cancelled`) delivered during snapshot fetch
   * are preserved. The `/api/jobs/events` backend stream only subscribes
   * to jobs that are QUEUED or PROCESSING at the time the loop sees
   * them, a job that goes from PROCESSING to COMPLETED *before* the
   * SSE subscribes would never emit its terminal event. Opening SSE
   * first reduces that race window to the snapshot's generation time
   * on the server.
   *
   * Reconciliation rule: SSE wins. Snapshot only fills in jobs we
   * don't already know about (the SSE never re-emits state for
   * completed/failed/cancelled jobs, so historical entries reach us
   * through the snapshot). Errors are absorbed so a transient
   * /api/jobs failure doesn't surface as an unhandled rejection on
   * page load, the SSE is already connected and the user can recover
   * by retrying any action.
   */
  async refresh() {
    this.loading = true;
    // Cursor captured before the job list is read, so the history read below
    // can name anything evicted from here on. See the tail of this method.
    const cursor = this.historySeq;
    let listSynced = false;
    try {
      const data = await api.getJobs();
      if (!Array.isArray(data)) return;
      listSynced = true;
      // Plain object as a transient id→true map. The svelte-eslint
      // `prefer-svelte-reactivity` rule (when present) flags raw Set
      // usage even for non-reactive locals; an object lookup avoids
      // both the rule and any reactivity overhead.
      const remoteIds = Object.create(null);
      for (const job of data) {
        if (!job?.id) continue;
        remoteIds[job.id] = true;
        const existing = this._byId.get(job.id);
        if (!existing) {
          // Unknown to us, definitely add. Most common case for
          // historical jobs (completed before SSE opened).
          this.jobs.push(job);
          this._byId.set(job.id, job);
          continue;
        }
        const existingTerminal = TERMINAL_STATUSES.has(existing.status);
        const incomingTerminal = TERMINAL_STATUSES.has(job.status);
        if (existingTerminal && !incomingTerminal) {
          // Terminal is final on the backend; an active snapshot for
          // an already-terminal local job is stale. Keep ours.
          continue;
        }
        if (!existingTerminal && incomingTerminal) {
          // Heal: SSE must have missed the terminal event (network
          // blip, tab backgrounded, etc.). The snapshot is the
          // source of truth for terminal jobs.
          this._applyJob(job);
          continue;
        }
        // Both active. Apply queued → processing transitions (and any
        // progress bump on a processing job) when the SSE missed
        // them. We only landed here for the OTHER-client polling
        // case: a job we picked up as `queued` from a poll never
        // gets a PROCESSING SSE frame from the backend if our SSE
        // stream wasn't open when it transitioned, so the row would
        // sit stuck at queued forever. The terminal+heal branch
        // already covers terminal-while-active; this covers the
        // queued-while-running progression.
        const queuedToProcessing = existing.status === 'queued' && job.status === 'processing';
        const progressBump = existing.status === job.status
          && (job.progress ?? 0) > (existing.progress ?? 0);
        if (queuedToProcessing || progressBump) {
          this._applyJob(job);
        }
        // Both terminal or both active with no useful progression:
        // leave SSE-derived state alone; live updates are at least
        // as fresh as the snapshot.
      }
      // Reconcile deletions. When another tab clears completed history
      // (or the backend prunes old terminal jobs on its own), /api/jobs
      // stops returning those ids, and there is no SSE event to drop
      // them client-side, so the local store would keep showing stale
      // rows indefinitely. Drop locally-known TERMINAL jobs that are
      // absent from the snapshot. Active jobs are left alone: SSE owns
      // them, and they may simply not be in the snapshot yet if they
      // were just queued between the snapshot generation and arrival.
      this.jobs = this.jobs.filter((j) => {
        if (TERMINAL_STATUSES.has(j.status) && !remoteIds[j.id]) {
          this._byId.delete(j.id);
          return false;
        }
        return true;
      });
    } catch (e) {
      console.error('Job snapshot hydration failed:', e);
    } finally {
      this.loading = false;
    }
    // Re-read the evicted-history totals. The `history` SSE event is the live
    // path; this is the safety net for a client whose stream dropped a frame,
    // reconnected across an outage, or was never open (poll-only).
    //
    // Deliberately AFTER the job list, and with the cursor captured BEFORE it:
    // the two reads describe different moments, and a job evicted in between
    // is both still in the list we just applied and inside these totals. The
    // cursor makes the backend name it so we drop the row instead of counting
    // it twice. Anything evicted before the list read is already absent from
    // it, and counted here — the two reconcile exactly.
    //
    // Skipped when the list read failed: the halves are only coherent
    // together, and advancing the cursor against a list we couldn't refresh
    // would retire rows without taking their replacements. Leave both stale
    // and let the next poll re-establish truth.
    if (!listSynced) return;
    try {
      this._applyHistoryOverflow(await api.getJobHistoryOverflow(cursor));
    } catch (_e) {
      // Non-fatal: the next poll or `history` event re-establishes truth.
    }
  }

  // ─── Creation ─────────────────────────────────────────────────────────
  _addOptimistic(filePath, mode) {
    if (this.creatingJobs.length >= MAX_OPTIMISTIC_PLACEHOLDERS) return;
    this.creatingJobs.push({ file_path: filePath, mode, _placeholder: true, _ts: Date.now() });
  }

  _removeOptimistic(filePath, mode) {
    // Drop a single matching placeholder (the oldest), not every one. Submitting
    // the same file+mode twice creates two indistinguishable placeholders;
    // clearing both when the first real job (or skip) resolves would under-count
    // the optimistic total and discard the second placeholder before its own
    // job arrives. Removing one per resolution keeps the count accurate.
    let removed = false;
    this.creatingJobs = this.creatingJobs.filter((p) => {
      if (!removed && p.file_path === filePath && p.mode === mode) {
        removed = true;
        return false;
      }
      return true;
    });
  }

  async create(filePath, mode, opts = {}) {
    this._addOptimistic(filePath, mode);
    try {
      const job = await api.createJob(
        filePath,
        mode,
        opts.outputDir ?? null,
        opts.compression ?? null,
        opts.deleteOnVerify ?? false,
        opts.split ?? false,
      );
      this._applyJob(job);
      return job;
    } catch (e) {
      this._removeOptimistic(filePath, mode);
      throw e;
    }
  }

  async createBatch(filePaths, mode, opts = {}) {
    for (const fp of filePaths) this._addOptimistic(fp, mode);
    try {
      const created = await api.createBatchJobs(
        filePaths,
        mode,
        opts.outputDir ?? null,
        opts.duplicateAction ?? 'skip',
        opts.compression ?? null,
        opts.deleteOnVerify ?? false,
        opts.split ?? false,
      );
      if (Array.isArray(created)) {
        for (const job of created) this._applyJob(job);
      }
      return created;
    } finally {
      // Always clear placeholders for the requested paths. _applyJob already
      // removes the placeholder for any path that came back as a real job;
      // this also drops placeholders for paths the backend skipped (e.g.
      // duplicates with duplicate_action: 'skip') so they don't linger.
      for (const fp of filePaths) this._removeOptimistic(fp, mode);
    }
  }

  // ─── Cancellation / cleanup ──────────────────────────────────────────
  async cancel(jobId) {
    const result = await api.cancelJob(jobId);
    const existing = this._byId.get(jobId);
    if (existing) {
      // Optimistically flip QUEUED jobs to cancelled, the backend
      // dequeues them immediately and there is no worker to wait for.
      //
      // For PROCESSING jobs, the backend only sets the cancel event
      // and leaves status=processing with message="Cancelling..." until
      // the worker exits and emits the terminal `cancelled` SSE event.
      // Forcing status=cancelled here can race the in-flight SSE update
      // and leave counts/history wrong if the SSE arrives reordered or
      // the stream is briefly reconnecting. Update the message field
      // only and trust the terminal SSE to flip status.
      if (existing.status === 'queued') {
        this._applyJob({ ...existing, status: 'cancelled' });
      } else if (existing.status === 'processing') {
        this._applyJob({ ...existing, message: 'Cancelling…' });
      }
    }
    return result;
  }

  async cancelAll() {
    this.cancellingAll = true;
    try {
      const res = await api.cancelAllJobs();
      // SSE will deliver the actual cancelled events; refresh just in case.
      await this.refresh();
      return res;
    } finally {
      this.cancellingAll = false;
    }
  }

  async clearCompleted() {
    this.clearingCompleted = true;
    try {
      const res = await api.deleteCompletedJobs();
      // Drop terminal jobs locally for instant feedback. The backend forgets
      // its evicted-history tally on the same call, so drop ours too or the
      // badges would keep counting jobs no list can show. Adopting the
      // post-reset sequence rejects any history read still in flight from
      // before the clear, which would otherwise restore the tally.
      this.historyOverflow = {};
      if (typeof res?.history_seq === 'number') this.historySeq = res.history_seq;
      this.jobs = this.jobs.filter((j) => !TERMINAL_STATUSES.has(j.status));
      this._byId.clear();
      for (const job of this.jobs) this._byId.set(job.id, job);
      return res;
    } finally {
      this.clearingCompleted = false;
    }
  }

  async checkStuck() {
    try {
      this.stuckState = await api.checkStuckStatus();
    } catch (_e) {
      this.stuckState = null;
    }
    return this.stuckState;
  }

  async recoverStuck() {
    this.recoveringStuck = true;
    try {
      const res = await api.recoverStuckJobs();
      await this.refresh();
      return res;
    } finally {
      this.recoveringStuck = false;
    }
  }

  // ─── SSE lifecycle ────────────────────────────────────────────────────
  connect() {
    if (this._unsubscribe) return;
    this._unsubscribe = api.subscribeToJobs(
      ({ type, data }) => {
        switch (type) {
          // Hydration emission from the backend on (re)connection. Same
          // shape as progress; _applyJob is idempotent so re-running on
          // reconnect just refreshes the slot.
          case 'snapshot':
          // falls through
          case 'progress':
          case 'cancelled':
          case 'error':
            if (data?.job) this._applyJob(data.job);
            break;
          case 'complete':
            if (data?.job) this._applyJob(data.job);
            // Job completion carries verified / source_deleted alongside
            // the job payload (job_manager._notify_subscribers). When the
            // new output passed verification, add it to the verified set;
            // when delete-on-verify removed the source, drop it.
            if (data?.verified && data?.job?.output_path) {
              verification.statuses.add(data.job.output_path);
            }
            if (data?.source_deleted && data?.job?.file_path) {
              verification.statuses.delete(data.job.file_path);
            }
            break;
          case 'status':
            // Status pulses don't carry a job payload in some emit paths.
            if (data?.job) this._applyJob(data.job);
            break;
          case 'history':
            // Absolute totals, not deltas, so a missed frame or a reconnect
            // self-heals on the next emission.
            this._applyHistoryOverflow(data?.history);
            break;
          default:
            break;
        }
      },
      {
        onOpen: () => {
          ui.reportConnection('open');
          // Reconcile rows against the backend on every (re)connect. A stream
          // opens its history cursor at the current sequence, so its first
          // frame cannot name jobs evicted while we were disconnected — we'd
          // hold rows for jobs already deleted and count them again inside
          // the totals. The snapshot read drops exactly those rows, and it
          // re-reads the totals with a matching cursor.
          this.refresh().catch(() => {});
          // Re-sync verified state on every SSE (re)connect. Terminal
          // job snapshots emitted at reconnect only carry the `job`
          // payload, they drop the `verified` and `source_deleted`
          // side-effect flags that the live `complete` event uses to
          // mutate verification.statuses, so the OK badge cache could
          // drift after a brief backend outage. Reloading from
          // /api/verified is the cheapest way to re-establish truth.
          verification.loadVerified();
        },
        onReconnecting: () => ui.reportConnection('reconnecting'),
      },
    );
    // Lightweight polling fallback for jobs queued by OTHER clients.
    // The SSE feed does not emit anything for the QUEUED state, its
    // first frame for a normal job is the PROCESSING transition, so
    // a second tab/user's job sitting in the queue behind a long-
    // running local conversion stays invisible without this poll.
    // We DON'T skip while hasActive is true: the queued job from the
    // other client is exactly the case this exists to surface, and
    // it's invisible precisely when we have an active conversion of
    // our own. refresh() is idempotent, SSE-derived state always
    // wins for jobs that overlap. Only skip while a modal is open so
    // background entry swaps don't race user-driven actions.
    if (!this._pollTimer) {
      this._pollTimer = setInterval(() => {
        if (ui.anyEntryModalOpen) return;
        this.refresh().catch(() => {});
      }, 30000);
    }
  }

  dispose() {
    if (this._unsubscribe) {
      this._unsubscribe();
      this._unsubscribe = null;
    }
    if (this._pollTimer) {
      clearInterval(this._pollTimer);
      this._pollTimer = null;
    }
  }
}

export const jobs = new JobsStore();
