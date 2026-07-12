import asyncio
import logging
from logging_setup import get_logger
import os
import resource
import shutil
import sys
import tempfile
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from config import settings
from fastapi.concurrency import run_in_threadpool
from models import ConversionJob, ConversionMode, InputKind, JobStatus
from services.archive import archive_service
from services.chd_metadata_store import chd_metadata_store
from services.chdman import ConversionCancelled, chdman_service
from services.concurrency_manager import concurrency_manager
from services.lock_manager import lock_manager
from services.makeps3iso import makeps3iso_service
from services.tools import ModeKind, registry
from services.verification_store import verification_store
from utils.delete_plan import build_delete_plan
from utils.path_utils import (
    is_safe_directory_tree,
    is_within_configured_volumes,
    strip_archive_path,
)

logger = get_logger("job_manager")

# Modes for externally-managed jobs that bypass the conversion queue,
# they are driven by callers via create/update/finish_external_job and
# must NOT count against max_queue_depth backpressure, stuck-detection,
# or the cancel-all / queued-count surfaces that only apply to the
# chdman/dolphin/z3ds conversion pipeline.
_EXTERNAL_JOB_MODES = frozenset({
    ConversionMode.METADATA_SCAN,
    ConversionMode.DAT_MATCH,
})


def _is_conversion_job(job) -> bool:
    """A real conversion job, not external bookkeeping (metadata scan / DAT match).

    The single definition of "counts toward the conversion queue", used by every
    backpressure / queue-depth / stuck-detection surface so they can't drift.
    """
    return job.mode not in _EXTERNAL_JOB_MODES


def _is_active_conversion(job) -> bool:
    """A conversion job currently occupying the queue (queued or processing)."""
    return (
        job.status in (JobStatus.QUEUED, JobStatus.PROCESSING)
        and _is_conversion_job(job)
    )


def _paths_collide(path_a: str, path_b: str) -> bool:
    try:
        return os.path.realpath(path_a) == os.path.realpath(path_b)
    except OSError:
        return False


class QueueBackpressureError(RuntimeError):
    """Raised when queue backpressure limits would be exceeded."""

    def __init__(self, current_depth: int, max_depth: int, additional_jobs: int):
        self.current_depth = max(0, int(current_depth))
        self.max_depth = max(0, int(max_depth))
        self.additional_jobs = max(1, int(additional_jobs))
        remaining = max(0, self.max_depth - self.current_depth)
        self.detail = (
            f"Conversion queue is at capacity ({self.current_depth}/{self.max_depth}). "
            f"Retry later or submit <= {remaining} additional job(s)."
        )
        super().__init__(self.detail)


class ExternalJobCancelled(Exception):
    """Raised inside an external-job loop when cancel_job() has been requested."""


class JobManager:
    """Manages conversion job queue and execution."""

    # Constants
    STUCK_RECOVERY_COOLDOWN_SECONDS = 60
    ARCHIVED_JOB_TTL_SECONDS = 60 * 15
    MAX_ARCHIVED_JOBS = 2000

    def __init__(self, max_concurrent: int = 1, max_job_history: int = 500):
        self.jobs: OrderedDict[str, ConversionJob] = OrderedDict()
        self._archived_jobs: OrderedDict[str, Tuple[ConversionJob, float]] = OrderedDict()
        self.max_concurrent = max(1, max_concurrent)
        self.max_job_history = max(0, max_job_history)
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._cancelled: Set[str] = set()
        self._cancel_events: Dict[str, asyncio.Event] = {}
        # Strong refs to in-flight requeue tasks so the event loop can't
        # garbage-collect them mid-`asyncio.sleep` (see _schedule_dir_lock_requeue).
        self._requeue_tasks: Set[asyncio.Task] = set()
        # Same strong-ref guard for fire-and-forget notify/prune tasks: asyncio
        # only keeps a weak ref to a bare create_task(), so it can be GC'd before
        # it runs (a cancel notification or history prune silently dropped).
        self._background_tasks: Set[asyncio.Task] = set()
        self._delete_plans: Dict[str, Dict[str, object]] = {}
        self._last_progress_at: Dict[str, float] = {}
        self._last_progress_log_at: Dict[str, float] = {}
        self._last_stall_log_at: Dict[str, float] = {}
        self._pid_stats: Dict[int, Dict[str, int]] = {}
        self._last_output_size: Dict[str, int] = {}
        self._last_output_size_at: Dict[str, float] = {}
        self._running = False
        self._dispatcher_task: Optional[asyncio.Task] = None
        self._debug_task: Optional[asyncio.Task] = None
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._stuck_detected_at: Optional[float] = None
        self._last_stuck_recovery_at: float = 0
        self._create_lock = asyncio.Lock()

    def _enforce_queue_backpressure_locked(self, additional_jobs: int = 1) -> None:
        """Raise QueueBackpressureError when queue depth limits are exceeded.

        This method is expected to be called only while holding ``self._create_lock``.
        """
        assert self._create_lock.locked(), (
            "_enforce_queue_backpressure_locked must be called with self._create_lock held"
        )
        max_depth = max(0, int(getattr(settings, "max_queue_depth", 0) or 0))
        if max_depth <= 0:
            return

        needed = max(1, int(additional_jobs))
        current_depth = sum(
            1 for job in self.jobs.values() if _is_active_conversion(job)
        )
        if current_depth + needed > max_depth:
            raise QueueBackpressureError(
                current_depth=current_depth,
                max_depth=max_depth,
                additional_jobs=needed,
            )

    def _queue_job_locked(
        self,
        file_path: str,
        mode: ConversionMode,
        *,
        output_dir: Optional[str] = None,
        output_path: Optional[str] = None,
        allow_overwrite: bool = False,
        filename_override: Optional[str] = None,
        compression: Optional[str] = None,
        delete_on_verify: bool = False,
        split: bool = False,
        delete_snapshot: Optional[Dict[str, object]] = None,
    ) -> ConversionJob:
        """Queue a job while holding _create_lock (no backpressure check here)."""
        job_id = str(uuid.uuid4())[:8]
        filename = filename_override or os.path.basename(file_path)

        # Determine output path - use explicit path if provided, otherwise calculate
        if output_path is None:
            output_path = registry.for_mode(mode.value).output_path(
                mode.value, file_path, output_dir,
            )
            # The HTTP routes validate inputs before passing an explicit
            # output_path; direct service callers reach this fallback. Most
            # tools' output_path() raises for an unsupported extension, but
            # z3ds's does not, so keep its input-extension gate -- now read from
            # the mode's declared input_extensions instead of the per-direction
            # Z3DS_*_FORMATS constants.
            spec = registry.spec(mode.value)
            if spec.tool_id == "z3ds":
                ext = Path(file_path).suffix.lower()
                if ext not in spec.input_extensions:
                    raise ValueError(f"Unsupported file extension: {ext}")
            # Generic same-path guard: a non-copy mode must never write over its
            # own source (chdman copy is an intentional in-place .chd recompress;
            # every other mode changes the extension so output != input).
            if spec.kind != ModeKind.COPY and _paths_collide(output_path, file_path):
                raise ValueError(
                    "Output path matches input; refusing to overwrite source"
                )

        # Carry the mode's input kind end-to-end so the pipeline skips the
        # archive-extract / file-only assumptions for a directory job and the
        # lock manager can protect the whole source subtree. Derived from the
        # registry spec (every conversion mode is registered; external jobs
        # bypass this path), so FILE/DIRECTORY can't drift via a typo.
        try:
            mode_spec = registry.spec(mode.value)
            input_kind = (
                InputKind.DIRECTORY
                if InputKind.DIRECTORY in mode_spec.input_kinds
                else InputKind.FILE
            )
        except KeyError:
            input_kind = InputKind.FILE

        job = ConversionJob(
            id=job_id,
            file_path=file_path,
            filename=filename,
            mode=mode,
            status=JobStatus.QUEUED,
            progress=0,
            created_at=datetime.now(timezone.utc),
            output_path=output_path,
            allow_overwrite=allow_overwrite,
            compression=compression,
            delete_on_verify=delete_on_verify,
            split=split,
            input_kind=input_kind,
        )

        self.jobs[job_id] = job
        if delete_on_verify and delete_snapshot:
            self._delete_plans[job_id] = delete_snapshot
        ticket = concurrency_manager.reserve_ticket(job_id)
        self._queue.put_nowait((ticket, job_id))
        now = time.monotonic()
        self._last_progress_at[job_id] = now
        self._last_progress_log_at[job_id] = now
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Queued job %s mode=%s input=%s output=%s overwrite=%s compression=%s",
                job_id,
                mode.value,
                file_path,
                output_path,
                allow_overwrite,
                compression,
            )
        return job

    async def create_job(
        self,
        file_path: str,
        mode: ConversionMode,
        *,
        output_dir: Optional[str] = None,
        output_path: Optional[str] = None,
        allow_overwrite: bool = False,
        filename_override: Optional[str] = None,
        compression: Optional[str] = None,
        delete_on_verify: bool = False,
        split: bool = False,
        delete_snapshot: Optional[Dict[str, object]] = None,
    ) -> ConversionJob:
        """Create a new conversion job."""
        async with self._create_lock:
            self._enforce_queue_backpressure_locked(1)
            job = self._queue_job_locked(
                file_path=file_path,
                mode=mode,
                output_dir=output_dir,
                output_path=output_path,
                allow_overwrite=allow_overwrite,
                filename_override=filename_override,
                compression=compression,
                delete_on_verify=delete_on_verify,
                split=split,
                delete_snapshot=delete_snapshot,
            )
        await self._prune_jobs()
        return job

    async def create_jobs_atomic(
        self,
        job_specs: List[Dict[str, object]],
        mode: ConversionMode,
        compression: Optional[str] = None,
        delete_on_verify: bool = False,
        split: bool = False,
    ) -> List[ConversionJob]:
        """Create multiple jobs atomically under a single backpressure check."""
        if not job_specs:
            return []

        jobs: List[ConversionJob] = []
        async with self._create_lock:
            self._enforce_queue_backpressure_locked(len(job_specs))
            for spec in job_specs:
                file_path = str(spec["file_path"])
                output_dir = spec.get("output_dir")
                output_path = spec.get("output_path")
                filename_override = spec.get("filename_override")
                jobs.append(
                    self._queue_job_locked(
                        file_path=file_path,
                        mode=mode,
                        output_dir=str(output_dir) if output_dir is not None else None,
                        output_path=str(output_path) if output_path is not None else None,
                        allow_overwrite=bool(spec.get("allow_overwrite", False)),
                        filename_override=(
                            str(filename_override)
                            if filename_override is not None
                            else None
                        ),
                        compression=compression,
                        delete_on_verify=delete_on_verify,
                        split=split,
                        delete_snapshot=spec.get("delete_snapshot"),
                    )
                )
        await self._prune_jobs()
        return jobs

    async def create_batch_jobs(
        self,
        file_paths: List[str],
        mode: ConversionMode,
        *,
        output_dir: Optional[str] = None,
        compression: Optional[str] = None,
        delete_on_verify: bool = False,
        delete_snapshots: Optional[Dict[str, Dict[str, object]]] = None,
    ) -> List[ConversionJob]:
        """Create multiple conversion jobs."""
        job_specs: List[Dict[str, object]] = []
        for fp in file_paths:
            snapshot = delete_snapshots.get(fp) if delete_snapshots else None
            job_specs.append(
                {
                    "file_path": fp,
                    "output_dir": output_dir,
                    "delete_snapshot": snapshot,
                }
            )
        return await self.create_jobs_atomic(
            job_specs,
            mode,
            compression=compression,
            delete_on_verify=delete_on_verify,
        )

    def get_job(self, job_id: str) -> Optional[ConversionJob]:
        """Get a job by ID."""
        return self.jobs.get(job_id)

    def _prune_archived_jobs(self) -> None:
        if not self._archived_jobs:
            return

        now = time.monotonic()
        max_keep = max(self.MAX_ARCHIVED_JOBS, self.max_job_history * 2)
        while self._archived_jobs:
            if len(self._archived_jobs) > max_keep:
                self._archived_jobs.popitem(last=False)
                continue
            oldest_job_id, (_, archived_at) = next(iter(self._archived_jobs.items()))
            if now - archived_at <= self.ARCHIVED_JOB_TTL_SECONDS:
                break
            self._archived_jobs.pop(oldest_job_id, None)

    def _archive_job_for_lookup(self, job: ConversionJob) -> None:
        archived = job.model_copy(deep=True)
        self._archived_jobs[archived.id] = (archived, time.monotonic())
        self._prune_archived_jobs()

    # ------------------------------------------------------------------
    # External-job API (for tasks that manage their own execution, e.g.
    # metadata scans).  These jobs bypass the conversion queue and must
    # be driven entirely by the caller.
    # ------------------------------------------------------------------

    def create_external_job(
        self,
        filename: str,
        mode: ConversionMode,
        message: str = "",
    ) -> ConversionJob:
        """Create and register an externally-managed job that bypasses the
        conversion queue.  The caller drives state changes via
        :meth:`update_external_job` and :meth:`finish_external_job`."""
        job_id = str(uuid.uuid4())[:8]
        job = ConversionJob(
            id=job_id,
            # Use a sentinel path that will never resolve to a real volume path
            # so that path-in-use checks cannot accidentally match this job.
            file_path=f"/__external_jobs__/{job_id}",
            filename=filename,
            mode=mode,
            status=JobStatus.PROCESSING,
            progress=0,
            message=message,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
        )
        self.jobs[job_id] = job
        # Register a cancel event so external-job loops can check for
        # cancellation (symmetric with dispatcher jobs at _process_job).
        # Event must be created on a running loop; fall back silently in
        # sync test contexts where no loop is active yet.
        try:
            asyncio.get_running_loop()
            self._cancel_events[job_id] = asyncio.Event()
        except RuntimeError:
            pass
        # Enforce max_job_history for external jobs too (best-effort; only runs
        # when there is a running event loop, i.e. production, not sync tests).
        try:
            asyncio.get_running_loop()
            self._spawn_background(self._prune_jobs())
        except RuntimeError:
            pass
        return job

    def get_cancel_event(self, job_id: str) -> Optional[asyncio.Event]:
        """Return the cancel event for *job_id*, if one is registered."""
        return self._cancel_events.get(job_id)

    def is_cancelled(self, job_id: str) -> bool:
        """True once cancel_job() has been requested for *job_id*."""
        return job_id in self._cancelled

    async def update_external_job(
        self,
        job_id: str,
        *,
        progress: Optional[int] = None,
        message: Optional[str] = None,
    ) -> None:
        """Update the progress/message of an externally-managed job and
        notify SSE subscribers."""
        job = self.jobs.get(job_id)
        if job is None:
            return
        if progress is not None:
            job.progress = max(0, min(100, progress))
        if message is not None:
            job.message = message
        await self._notify_subscribers(
            job_id,
            {
                "type": "progress",
                "job_id": job_id,
                "status": job.status.value,
                "progress": job.progress,
                "message": job.message,
            },
        )

    async def finish_external_job(
        self,
        job_id: str,
        *,
        success: bool,
        error_message: Optional[str] = None,
    ) -> None:
        """Mark an externally-managed job as complete or failed, notify
        subscribers, and archive it."""
        job = self.jobs.get(job_id)
        if job is None:
            return
        job.status = JobStatus.COMPLETED if success else JobStatus.FAILED
        if success:
            job.progress = 100
        job.completed_at = datetime.now(timezone.utc)
        if error_message is not None:
            job.error_message = error_message
        # Clean up any spurious cancel state that may have been set by
        # cancel_all while the external task was still running.
        self._cancel_events.pop(job_id, None)
        self._cancelled.discard(job_id)
        event_type = "complete" if success else "error"
        await self._notify_subscribers(
            job_id,
            {
                "type": event_type,
                "job_id": job_id,
                "status": job.status.value,
                "progress": job.progress,
                "message": job.message,
                "error_message": job.error_message,
            },
        )
        self._archive_job_for_lookup(job)
        # Keep the job in self.jobs with its terminal status so that:
        # - /api/jobs continues to list it until the user clears it
        # - the normal "Clear Done" flow can remove it alongside conversion jobs
        # Enforce max_job_history on completion (exclude_id preserves this job).
        await self._prune_jobs(exclude_id=job_id)

    async def finish_external_job_cancelled(
        self,
        job_id: str,
        *,
        message: Optional[str] = None,
    ) -> None:
        """Finalize an externally-managed job that was cancelled mid-run.

        Parallels :meth:`finish_external_job` but sets status to CANCELLED
        and emits a ``cancelled`` SSE event so the UI transitions cleanly.
        """
        job = self.jobs.get(job_id)
        if job is None:
            return
        job.status = JobStatus.CANCELLED
        job.completed_at = datetime.now(timezone.utc)
        if message is not None:
            job.message = message
        self._cancel_events.pop(job_id, None)
        self._cancelled.discard(job_id)
        await self._notify_subscribers(
            job_id,
            {
                "type": "cancelled",
                "job_id": job_id,
                "status": job.status.value,
                "progress": job.progress,
                "message": job.message,
            },
        )
        self._archive_job_for_lookup(job)
        await self._prune_jobs(exclude_id=job_id)

    def get_job_for_lookup(self, job_id: str) -> Optional[ConversionJob]:
        """Get a live job, or a recently archived one that was deleted from history."""
        job = self.jobs.get(job_id)
        if job is not None:
            return job
        self._prune_archived_jobs()
        archived = self._archived_jobs.pop(job_id, None)
        if archived is None:
            return None
        # Mark as recently accessed so active clients can briefly recover.
        archived_job, _ = archived
        self._archived_jobs[job_id] = (archived_job, time.monotonic())
        return archived_job

    def get_all_jobs(self) -> List[ConversionJob]:
        """Get all jobs."""
        return list(self.jobs.values())

    def get_queue_depth(self) -> int:
        """Return queued + processing job count for backpressure checks.

        External jobs (e.g. METADATA_SCAN) are excluded so they cannot
        consume queue capacity or trigger false backpressure errors.
        """
        return sum(
            1 for job in self.jobs.values() if _is_active_conversion(job)
        )

    def get_active_job_candidates(self) -> List[Tuple[str, List[str]]]:
        """Return active job ids with their candidate paths (input/output)."""
        candidates: List[Tuple[str, List[str]]] = []
        for job in self.jobs.values():
            if job.status not in (JobStatus.QUEUED, JobStatus.PROCESSING):
                continue
            candidates.append((job.id, self._candidate_paths(job)))
        return candidates

    @staticmethod
    def _normalize_path(path: str) -> Optional[Path]:
        try:
            return Path(path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return None

    @staticmethod
    def _is_descendant(target: Path, ancestor: Path) -> bool:
        """Whether ``target`` is at or below ``ancestor`` (pure path prefix)."""
        try:
            target.relative_to(ancestor)
            return True
        except ValueError:
            return False

    def _directory_job_blocks(self, job: ConversionJob, target: Path) -> bool:
        """True when ``target`` lives inside an active directory job's source.

        A directory job (makeps3iso folder->iso) is packaging its whole source
        subtree, so a per-file job / rename / delete on any path *under* that
        folder — e.g. ``<folder>/PS3_GAME/PARAM.SFO`` while ``<folder>`` is being
        packed — would corrupt the in-flight ISO. The bare path-hash lock can't
        see that containment (a child hashes to a different key), so guard it
        here. Cheap: a normalized ``Path`` prefix check, no extra disk I/O.
        """
        if job.input_kind != InputKind.DIRECTORY:
            return False
        job_dir = self._normalize_path(job.file_path)
        if job_dir is None:
            return False
        return self._is_descendant(target, job_dir)

    def _blocked_by_dir_lock(self, job: ConversionJob) -> bool:
        """Whether this job's input/output is inside a directory subtree another
        job has locked (a makeps3iso folder->iso job packing that tree).

        Such a conflict is **transient** — it clears when the folder job
        finishes — so the dispatcher waits and re-queues the job rather than
        failing it, exactly like a job waiting its turn in the queue.
        """
        if job.input_kind == InputKind.DIRECTORY:
            return lock_manager.dir_lock_would_conflict(job.file_path)
        paths = [job.output_path]
        if "::" not in job.file_path:
            paths.append(job.file_path)
        return any(lock_manager.is_within_locked_dir(p) for p in paths if p)

    async def _defer_blocked_job(self, job_id: str, *, output_lock_held: bool) -> None:
        """Release a job blocked by a directory subtree lock and re-queue it.

        The job waits its turn in the queue and is re-dispatched once the folder
        job releases the lock — the same outcome as any queued job, never a
        failure. Releases the held slot (and the output lock when one was taken)
        so the folder job and others can proceed meanwhile.
        """
        job = self.jobs.get(job_id)
        if output_lock_held and job is not None and job.output_path:
            lock_manager.release_lock(job.output_path)
        if job_id in self._cancel_events:
            del self._cancel_events[job_id]
        concurrency_manager.release(job_id)
        if job is not None:
            job.message = "Waiting for an in-progress folder conversion to finish..."
            await self._notify_subscribers(
                job_id,
                {
                    "type": "status",
                    "job_id": job_id,
                    "status": job.status.value,
                    "progress": job.progress,
                    "message": job.message,
                },
            )
        self._schedule_dir_lock_requeue(job_id)

    def _schedule_dir_lock_requeue(self, job_id: str, delay: float = 2.0) -> None:
        """Re-dispatch a deferred job after a short delay so it retries once the
        blocking folder job has had a chance to finish."""

        async def _requeue() -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            job = self.jobs.get(job_id)
            if job is None or job_id in self._cancelled:
                return
            if job.status in (
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            ):
                return
            job.status = JobStatus.QUEUED
            ticket = concurrency_manager.reserve_ticket(job_id)
            self._queue.put_nowait((ticket, job_id))
            self._last_progress_at[job_id] = time.monotonic()

        # Keep a strong reference until the task finishes; a bare create_task()
        # can be collected while still awaiting the sleep.
        task = asyncio.create_task(_requeue())
        self._requeue_tasks.add(task)
        task.add_done_callback(self._requeue_tasks.discard)

    def _spawn_background(self, coro) -> None:
        """Fire-and-forget *coro*, retaining a strong ref until it completes.

        asyncio holds only a weak reference to a task, so a bare
        ``create_task(coro)`` can be garbage-collected before it runs — a cancel
        notification or history prune may then silently never happen. Mirror
        ``_schedule_dir_lock_requeue``: keep the task in ``_background_tasks`` and
        drop it on completion.
        """
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _track_candidate_paths(self, file_path: str) -> List[str]:
        if "::" in file_path:
            return []
        ext = Path(file_path).suffix.lower()
        if ext not in {".cue", ".gdi"}:
            return []
        if not os.path.isfile(file_path):
            return []
        try:
            plan = build_delete_plan(file_path)
        except Exception:
            return []
        source_real = os.path.realpath(file_path)
        tracks = []
        for path in plan.get("delete_paths", []):
            if path == source_real:
                continue
            if os.path.exists(path):
                tracks.append(path)
        return tracks

    def _candidate_paths(self, job: ConversionJob) -> List[str]:
        paths = []
        file_path = job.file_path
        if "::" in file_path:
            paths.append(file_path.split("::", 1)[0])
        else:
            paths.append(file_path)
            paths.extend(self._track_candidate_paths(file_path))
        if job.output_path:
            paths.append(job.output_path)
            # Companion outputs a mode writes beside its primary (extractcd's
            # .bin). Directory modes are skipped: makeps3iso's split parts are
            # queue-time-unknown, disk-probed (companion_outputs scans the dir),
            # and already covered by _split_output_blocks' I/O-free prefix match —
            # so this stays a pure, event-loop-safe lookup (this helper is called
            # synchronously from async route code).
            if job.input_kind != InputKind.DIRECTORY:
                paths.extend(
                    registry.for_mode(job.mode.value).companion_outputs(
                        job.output_path, job.mode.value,
                    )
                )
        return paths

    def find_active_job_for_path(
        self, path: str, *, is_dir: bool = False
    ) -> Optional[ConversionJob]:
        """Return the active job using a path (input/output) or None."""
        target = self._normalize_path(path)
        if target is None:
            return None

        for job in self.jobs.values():
            if job.status not in (JobStatus.QUEUED, JobStatus.PROCESSING):
                continue
            # A directory job protects its whole subtree against concurrent
            # mutation: a target *inside* the in-flight folder is in use.
            if self._directory_job_blocks(job, target):
                return job
            # An in-flight split build's numbered parts (Game.iso.0/.1/…) count
            # as the job's output even though they aren't in _candidate_paths.
            if self._split_output_blocks(job, target):
                return job
            for candidate in self._candidate_paths(job):
                cand_path = self._normalize_path(candidate)
                if cand_path is None:
                    continue
                if cand_path == target:
                    return job
                if is_dir:
                    try:
                        cand_path.relative_to(target)
                        return job
                    except ValueError:
                        continue
        return None

    def _is_path_in_use_by_other_job(self, job_id: str, path: str) -> bool:
        target = self._normalize_path(path)
        if target is None:
            return False

        for job in self.jobs.values():
            if job.id == job_id:
                continue
            if not _is_active_conversion(job):
                continue
            # Reject a delete that targets a path inside another active
            # directory job's source folder (its whole subtree is in use).
            if self._directory_job_blocks(job, target):
                return True
            if self._split_output_blocks(job, target):
                return True
            for candidate in self._candidate_paths(job):
                cand_path = self._normalize_path(candidate)
                if cand_path is None:
                    continue
                if cand_path == target:
                    return True
        return False

    def _split_output_blocks(self, job: ConversionJob, target: Path) -> bool:
        """Whether ``target`` is a numbered split part of an active split job's
        output (``Game.iso.0``/``.1``/…).

        makeps3iso ``-s`` renames the locked base ``.iso`` to ``.iso.0`` and
        writes ``.iso.1``/… mid-run, so those names don't exist when the job is
        queued and can't be listed in the static ``_candidate_paths`` (which
        carries the base ``output_path``). A prefix check marks the whole set
        in-use so a rename/delete can't mutate a part while it's being written.
        """
        if not job.split or not job.output_path:
            return False
        out = self._normalize_path(job.output_path)
        if out is None or target.parent != out.parent:
            return False
        prefix = out.name + "."
        suffix = target.name[len(prefix):]
        return target.name.startswith(prefix) and suffix.isdigit()

    async def _clear_existing_output(self, job: ConversionJob) -> None:
        """Remove a prior output ahead of an **authorized** overwrite.

        Gated on ``allow_overwrite`` so it never deletes an output the user chose
        to skip/rename — including a split set (``Game.iso.0``/…) that appeared
        after planning but before the worker started (the per-path
        ``acquire_lock`` only sees the bare base name).
        """
        if not job.allow_overwrite or not job.output_path:
            return
        if job.input_kind == InputKind.DIRECTORY:
            # makeps3iso output is a single .iso OR a split set (.0/.1/…); clear
            # all of it via the tool's own part-aware cleanup. But a destination
            # that already exists as a *directory* named like the output can't be
            # cleared by remove_outputs (its os.remove() fails on a dir and is
            # suppressed); makeps3iso would then write *inside* it while the job
            # still reports the bare path as its output. Reject rather than
            # corrupt — mirroring the non-file guard on the file-job path below.
            if await run_in_threadpool(os.path.isdir, job.output_path):
                raise RuntimeError("Output path exists and is not a file")
            await run_in_threadpool(
                makeps3iso_service.remove_outputs, job.output_path,
            )
            await verification_store.clear(job.output_path)
            return
        # Clear the primary output and every companion the mode wrote beside it
        # (enumerated from the tool, not re-derived). Companions are cleared even
        # when the primary is already gone: a lone companion (e.g. a stray
        # extractcd .bin whose .cue was deleted) is what made
        # check_output_conflicts authorize the overwrite, so it must not be left
        # to collide with the new output. Validate the whole set first — a
        # non-file occupant (a directory squatting on the primary or a companion
        # name) can't be unlinked, so reject before removing anything rather than
        # deleting the primary and then failing against the stray occupant.
        targets = [
            job.output_path,
            *registry.for_mode(job.mode.value).companion_outputs(
                job.output_path, job.mode.value,
            ),
        ]
        for target in targets:
            if os.path.exists(target) and not os.path.isfile(target):
                raise RuntimeError("Output path exists and is not a file")
        primary_removed = False
        for target in targets:
            if os.path.isfile(target):
                os.remove(target)
                primary_removed = primary_removed or target == job.output_path
        if primary_removed:
            await verification_store.clear(job.output_path)

    def _get_queued_and_processing_jobs(self) -> tuple[list[str], list[str]]:
        """Get lists of queued and processing job IDs.

        External jobs (e.g. METADATA_SCAN) are excluded so they cannot
        mask a stuck conversion queue or skew health metrics.

        Returns:
            Tuple of (queued_job_ids, processing_job_ids)
        """
        queued_job_ids: list[str] = []
        processing_job_ids: list[str] = []
        for job in self.jobs.values():
            if not _is_conversion_job(job):
                continue
            if job.status == JobStatus.QUEUED:
                queued_job_ids.append(job.id)
            elif job.status == JobStatus.PROCESSING:
                processing_job_ids.append(job.id)
        return queued_job_ids, processing_job_ids

    def is_stuck(self) -> bool:
        """Check if the job queue is stuck (queued jobs but none processing).

        External jobs (e.g. METADATA_SCAN) are excluded so a running scan
        cannot mask a stuck conversion queue.

        Returns:
            True if conversion jobs are queued but none are processing, False otherwise
        """
        has_queued = False
        for job in self.jobs.values():
            if not _is_conversion_job(job):
                continue
            if job.status == JobStatus.PROCESSING:
                # Any processing conversion means the queue isn't stuck; bail early.
                return False
            if job.status == JobStatus.QUEUED:
                has_queued = True
        return has_queued

    def get_stuck_state_info(self) -> Dict[str, object]:
        """Get information about the stuck state.

        Returns:
            Dictionary with stuck state information
        """
        is_stuck = self.is_stuck()
        queued_job_ids, processing_job_ids = self._get_queued_and_processing_jobs()
        now_monotonic = time.monotonic()

        result = {
            "is_stuck": is_stuck,
            "queued_count": len(queued_job_ids),
            "processing_count": len(processing_job_ids),
        }

        # Expose durations derived from monotonic times rather than the raw values,
        # which are not meaningful as wall-clock timestamps.
        if self._stuck_detected_at is not None:
            result["stuck_for_seconds"] = int(now_monotonic - self._stuck_detected_at)

        if self._last_stuck_recovery_at > 0:
            result["last_recovery_seconds_ago"] = int(now_monotonic - self._last_stuck_recovery_at)

        return result

    async def recover_from_stuck_state(self) -> Dict[str, object]:
        """Attempt to recover from a stuck state by cleaning up stale locks.

        Returns:
            Dictionary with recovery results and actions taken
        """
        now = time.monotonic()

        # Prevent recovery spam (minimum cooldown between attempts)
        if now - self._last_stuck_recovery_at < self.STUCK_RECOVERY_COOLDOWN_SECONDS:
            return {
                "success": False,
                "message": "Recovery attempted too recently, please wait",
                "cooldown_remaining": int(
                    self.STUCK_RECOVERY_COOLDOWN_SECONDS - (now - self._last_stuck_recovery_at)
                )
            }

        self._last_stuck_recovery_at = now

        logger.warning("Attempting recovery from stuck state")

        # Cleanup stale locks
        removed_locks = await run_in_threadpool(lock_manager.cleanup_stale_locks_periodic)

        # Get current state
        queued_job_ids, processing_job_ids = self._get_queued_and_processing_jobs()

        result = {
            "success": True,
            "message": "Recovery attempt completed",
            "removed_locks": removed_locks,
            "queued_jobs": len(queued_job_ids),
            "processing_jobs": len(processing_job_ids),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        logger.info(
            "Stuck state recovery completed: removed_locks=%d queued=%d processing=%d",
            removed_locks, len(queued_job_ids), len(processing_job_ids)
        )

        # Clear stuck detection timestamp if state looks healthy now
        if not self.is_stuck():
            self._stuck_detected_at = None

        return result

    async def _prune_jobs(self, *, exclude_id: Optional[str] = None):
        if self.max_job_history <= 0:
            return
        if len(self.jobs) <= self.max_job_history:
            return

        removable = []
        for job_id, job in self.jobs.items():
            if job_id == exclude_id:
                continue
            if job.status in (
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            ):
                removable.append(job_id)
            if len(self.jobs) - len(removable) <= self.max_job_history:
                break

        for job_id in removable:
            await self.delete_job(job_id)

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel a job."""
        job = self.jobs.get(job_id)
        if not job:
            return False

        # Externally-managed jobs (metadata scan, DAT match) are always
        # created in PROCESSING state, they never sit in the dispatcher
        # queue. Signal via the cancel event; the owning task checks
        # job_manager.is_cancelled() inside its loop and finalizes via
        # finish_external_job_cancelled().
        if job.mode in _EXTERNAL_JOB_MODES:
            if job.status != JobStatus.PROCESSING:
                return False
            self._cancelled.add(job_id)
            cancel_event = self._cancel_events.get(job_id)
            if cancel_event:
                cancel_event.set()
            # ASCII ellipsis (matches the conversion-job branch below and
            # the frontend's optimistic "Cancelling..." string, avoids a
            # render flicker when the SSE status event lands).
            job.message = "Cancelling..."
            self._spawn_background(
                self._notify_subscribers(
                    job_id,
                    {
                        "type": "status",
                        "job_id": job_id,
                        "status": job.status.value,
                        "progress": job.progress,
                        "message": job.message,
                    },
                )
            )
            return True

        if job.status == JobStatus.QUEUED:
            self._cancelled.add(job_id)
            job.status = JobStatus.CANCELLED
            job.completed_at = datetime.now(timezone.utc)
            cancel_event = self._cancel_events.get(job_id)
            if cancel_event:
                cancel_event.set()
            concurrency_manager.release(job_id)
            self._spawn_background(
                self._notify_subscribers(
                    job_id,
                    {"type": "cancelled", "job_id": job_id, "status": job.status.value},
                )
            )
            await self._cleanup_temp_dir(job)
            return True

        if job.status == JobStatus.PROCESSING:
            self._cancelled.add(job_id)
            cancel_event = self._cancel_events.get(job_id)
            if cancel_event:
                cancel_event.set()
            job.message = "Cancelling..."
            self._spawn_background(
                self._notify_subscribers(
                    job_id,
                    {
                        "type": "status",
                        "job_id": job_id,
                        "status": job.status.value,
                        "progress": job.progress,
                        "message": job.message,
                    },
                )
            )
            return True
        return False

    async def cancel_all_jobs(self) -> Dict[str, object]:
        """Cancel all queued and processing jobs.

        Returns:
            Summary payload with queued/processing counts and job IDs that received
            a cancellation request.
        """
        queued_ids = [
            job.id for job in self.jobs.values()
            if job.status == JobStatus.QUEUED
        ]
        processing_ids = [
            job.id for job in self.jobs.values()
            if job.status == JobStatus.PROCESSING
        ]
        requested_ids: list[str] = []

        # Snapshot IDs first to avoid mutating while iterating jobs.
        for job_id in queued_ids + processing_ids:
            if await self.cancel_job(job_id):
                requested_ids.append(job_id)

        return {
            "requested": len(requested_ids),
            "queued": len(queued_ids),
            "processing": len(processing_ids),
            "job_ids": requested_ids,
        }

    async def delete_job(self, job_id: str) -> bool:
        """Delete a job from the list."""
        if job_id in self.jobs:
            job = self.jobs[job_id]
            if job.status == JobStatus.PROCESSING:
                await self.cancel_job(job_id)
            self._archive_job_for_lookup(job)
            self._cancelled.discard(job_id)
            self._delete_plans.pop(job_id, None)
            if job_id not in self._cancel_events:
                concurrency_manager.release(job_id)
            if job_id not in self._cancel_events:
                await self._cleanup_temp_dir(job)
            del self.jobs[job_id]
            return True
        return False

    def subscribe(self, job_id: str) -> asyncio.Queue:
        """Subscribe to progress updates for a job."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        if job_id not in self._subscribers:
            self._subscribers[job_id] = []
        self._subscribers[job_id].append(queue)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue):
        """Unsubscribe from progress updates."""
        if job_id in self._subscribers:
            try:
                self._subscribers[job_id].remove(queue)
            except ValueError:
                pass

    async def _cleanup_temp_dir(self, job: ConversionJob):
        if job.temp_dir:
            temp_dir = job.temp_dir
            try:
                if not self._is_safe_temp_dir(temp_dir):
                    logger.warning(
                        "Skipping cleanup for unsafe temp dir %s (job %s)",
                        temp_dir,
                        job.id,
                    )
                    return
                if await run_in_threadpool(os.path.isdir, temp_dir):
                    await run_in_threadpool(shutil.rmtree, temp_dir, ignore_errors=True)
            except Exception as cleanup_error:
                print(f"Failed to cleanup temp dir {temp_dir}: {cleanup_error}")
            finally:
                job.temp_dir = None

    def _is_safe_temp_dir(self, temp_dir: str) -> bool:
        if not temp_dir:
            return False
        try:
            resolved = Path(temp_dir).resolve(strict=False)
        except (OSError, RuntimeError):
            return False

        bases = []
        if settings.temp_dir:
            try:
                bases.append(Path(settings.temp_dir).resolve(strict=False))
            except (OSError, RuntimeError) as exc:
                logger.debug(
                    "Skipping configured temp_dir %s due to resolution error: %s",
                    settings.temp_dir,
                    exc,
                )
        try:
            bases.append(Path(settings.data_dir).resolve(strict=False) / "temp")
        except (OSError, RuntimeError) as exc:
            logger.debug(
                "Skipping data_dir temp base %s due to resolution error: %s",
                settings.data_dir,
                exc,
            )
        try:
            bases.append(Path(tempfile.gettempdir()).resolve(strict=False))
        except (OSError, RuntimeError) as exc:
            logger.debug(
                "Skipping system temp base due to resolution error: %s",
                exc,
            )

        for base in bases:
            try:
                resolved.relative_to(base)
                return True
            except ValueError:
                continue
        return False

    async def _notify_subscribers(self, job_id: str, data: dict):
        """Notify all subscribers of a job update."""
        if job_id in self._subscribers:
            for queue in self._subscribers[job_id]:
                try:
                    queue.put_nowait(data)
                except asyncio.QueueFull:
                    pass

    async def _set_job_message(self, job_id: str, message: str):
        job = self.jobs.get(job_id)
        if not job:
            return
        job.message = message
        now = time.monotonic()
        self._last_progress_at[job_id] = now
        await self._notify_subscribers(
            job_id,
            {
                "type": "status",
                "job_id": job_id,
                "status": job.status.value,
                "progress": job.progress,
                "message": job.message,
            },
        )

    async def _emit_extract_updates(
        self, job_id: str, archive_path: str, internal_path: str, task: asyncio.Task
    ):
        start = time.monotonic()
        while not task.done():
            job = self.jobs.get(job_id)
            if not job or job.status != JobStatus.PROCESSING:
                return
            elapsed = int(time.monotonic() - start)
            name = os.path.basename(internal_path)
            message = f"Extracting {name}... ({elapsed}s)"
            await self._set_job_message(job_id, message)
            await asyncio.sleep(2)

    async def process_queue(self):
        """Background task to process conversion queue."""
        if self._running:
            return
        self._running = True
        self._dispatcher_task = asyncio.create_task(self._dispatcher_loop())

        # Only start the maintenance loop if the configured heartbeat interval is positive.
        debug_interval = getattr(settings, "debug_heartbeat_interval", None)
        if isinstance(debug_interval, (int, float)) and debug_interval > 0:
            self._debug_task = asyncio.create_task(self._debug_loop())
        else:
            self._debug_task = None
            if debug_interval is not None:
                logger.warning(
                    "Maintenance loop disabled: non-positive CHD_DEBUG_HEARTBEAT value %r. "
                    "Stuck-job detection and stale lock cleanup will not run.",
                    debug_interval,
                )
        await self._dispatcher_task

    async def _handle_background_maintenance(self, cleanup_counter: int) -> int:
        """Handle stuck state detection and periodic lock cleanup.

        Args:
            cleanup_counter: Current cleanup counter value

        Returns:
            Updated cleanup counter value
        """
        # Check for stuck state (queued jobs but none processing)
        now = time.monotonic()
        if self.is_stuck():
            if self._stuck_detected_at is None:
                self._stuck_detected_at = now
                logger.warning(
                    "Stuck state detected: jobs queued but none processing. "
                    f"Will attempt automatic recovery in {self.STUCK_RECOVERY_COOLDOWN_SECONDS}"
                    " seconds if state persists."
                )
            else:
                stuck_duration = now - self._stuck_detected_at
                if stuck_duration >= self.STUCK_RECOVERY_COOLDOWN_SECONDS:
                    # Stuck for 60+ seconds, attempt automatic recovery
                    logger.error(
                        "Jobs have been stuck for %.1f seconds. Attempting automatic recovery...",
                        stuck_duration
                    )
                    result = await self.recover_from_stuck_state()
                    if result.get("success"):
                        logger.info(
                            "Automatic recovery completed: removed %d stale locks",
                            result.get("removed_locks", 0)
                        )
                    else:
                        logger.warning("Automatic recovery failed: %s", result.get("message"))
        else:
            # Not stuck, clear detection timestamp
            if self._stuck_detected_at is not None:
                logger.info("Stuck state cleared")
                self._stuck_detected_at = None

        # Periodic stale lock cleanup (every 10 heartbeats = 5 minutes by default)
        cleanup_counter += 1
        if cleanup_counter >= 10:
            cleanup_counter = 0
            try:
                removed = await run_in_threadpool(lock_manager.cleanup_stale_locks_periodic)
                if removed > 0:
                    logger.info("Periodic cleanup removed %d stale lock file(s)", removed)
            except Exception as e:
                logger.warning("Periodic lock cleanup failed: %s", e)

        return cleanup_counter

    async def _debug_loop(self):
        cleanup_counter = 0
        while self._running:
            try:
                await asyncio.sleep(settings.debug_heartbeat_interval)

                # Handle background maintenance tasks
                cleanup_counter = await self._handle_background_maintenance(cleanup_counter)

                if not logger.isEnabledFor(logging.DEBUG):
                    continue

                jobs = list(self.jobs.values())
                status_counts = {
                    JobStatus.QUEUED: 0,
                    JobStatus.PROCESSING: 0,
                    JobStatus.COMPLETED: 0,
                    JobStatus.FAILED: 0,
                    JobStatus.CANCELLED: 0,
                }
                for job in jobs:
                    status_counts[job.status] = status_counts.get(job.status, 0) + 1

                subscriber_queues = sum(len(qs) for qs in self._subscribers.values())
                temp_dirs = sum(1 for job in jobs if job.temp_dir)

                usage = resource.getrusage(resource.RUSAGE_SELF)
                rss_raw = usage.ru_maxrss
                if sys.platform == "darwin":
                    rss_mb = rss_raw / (1024 * 1024)
                else:
                    rss_mb = rss_raw / 1024
                open_fds = None
                if os.path.exists("/proc/self/fd"):
                    try:
                        open_fds = len(os.listdir("/proc/self/fd"))
                    except OSError:
                        open_fds = None
                loadavg = None
                if hasattr(os, "getloadavg"):
                    try:
                        loadavg = os.getloadavg()
                    except OSError:
                        loadavg = None

                logger.debug(
                    "Heartbeat jobs=%d queued=%d processing=%d completed=%d failed=%d cancelled=%d "
                    "queue_size=%d semaphore=%s cancelled_set=%d subscribers=%d temp_dirs=%d "
                    "locks=%d tickets=%d active_chdman=%d rss_raw=%d rss_mb=%.1f"
                    " open_fds=%s loadavg=%s",
                    len(jobs),
                    status_counts[JobStatus.QUEUED],
                    status_counts[JobStatus.PROCESSING],
                    status_counts[JobStatus.COMPLETED],
                    status_counts[JobStatus.FAILED],
                    status_counts[JobStatus.CANCELLED],
                    self._queue.qsize(),
                    getattr(self._semaphore, "_value", None),
                    len(self._cancelled),
                    subscriber_queues,
                    temp_dirs,
                    lock_manager.stats().get("locks"),
                    concurrency_manager.stats().get("tickets"),
                    len(chdman_service.active_pids()),
                    rss_raw,
                    rss_mb,
                    open_fds,
                    loadavg,
                )

                for job in jobs:
                    if job.status != JobStatus.PROCESSING:
                        continue
                    now = time.monotonic()
                    last_progress = self._last_progress_at.get(job.id, now)
                    idle_for = now - last_progress
                    output_size = None
                    output_idle = None
                    if job.output_path and os.path.exists(job.output_path):
                        try:
                            output_size = os.path.getsize(job.output_path)
                        except OSError:
                            output_size = None
                        if output_size is not None:
                            last_size = self._last_output_size.get(job.id)
                            last_size_at = self._last_output_size_at.get(job.id, now)
                            if last_size is None or output_size != last_size:
                                self._last_output_size[job.id] = output_size
                                self._last_output_size_at[job.id] = now
                            else:
                                output_idle = now - last_size_at
                    logger.debug(
                        "Processing job %s progress=%s idle=%.1fs output_size=%s output_idle=%s",
                        job.id,
                        job.progress,
                        idle_for,
                        output_size,
                        output_idle,
                    )

                if settings.debug_progress_timeout > 0:
                    now = time.monotonic()
                    for job in jobs:
                        if job.status != JobStatus.PROCESSING:
                            continue
                        output_size = None
                        output_idle = None
                        if job.output_path and os.path.exists(job.output_path):
                            try:
                                output_size = os.path.getsize(job.output_path)
                            except OSError:
                                output_size = None
                            if output_size is not None:
                                last_size = self._last_output_size.get(job.id)
                                last_size_at = self._last_output_size_at.get(
                                    job.id, now
                                )
                                if last_size is None or output_size != last_size:
                                    self._last_output_size[job.id] = output_size
                                    self._last_output_size_at[job.id] = now
                                else:
                                    output_idle = now - last_size_at
                        last_progress = self._last_progress_at.get(job.id, now)
                        idle_for = now - last_progress
                        if idle_for < settings.debug_progress_timeout:
                            continue
                        last_stall = self._last_stall_log_at.get(job.id, 0)
                        if now - last_stall < settings.debug_progress_timeout:
                            continue
                        self._last_stall_log_at[job.id] = now
                        logger.debug(
                            "Stalled job %s idle=%.1fs progress=%s message=%s input=%s output=%s "
                            "output_size=%s output_idle=%s started_at=%s",
                            job.id,
                            idle_for,
                            job.progress,
                            job.message,
                            job.file_path,
                            job.output_path,
                            output_size,
                            output_idle,
                            job.started_at,
                        )

                for pid in chdman_service.active_pids():
                    proc_io_path = f"/proc/{pid}/io"
                    proc_status_path = f"/proc/{pid}/status"
                    if not os.path.exists(proc_status_path):
                        self._pid_stats.pop(pid, None)
                        continue

                    rss_kb = None
                    threads = None
                    read_bytes = None
                    write_bytes = None

                    try:
                        with open(proc_status_path, "r", encoding="utf-8") as fh:
                            for line in fh:
                                if line.startswith("VmRSS:"):
                                    rss_kb = int(line.split()[1])
                                elif line.startswith("Threads:"):
                                    threads = int(line.split()[1])
                    except OSError:
                        pass

                    if os.path.exists(proc_io_path):
                        try:
                            with open(proc_io_path, "r", encoding="utf-8") as fh:
                                for line in fh:
                                    if line.startswith("read_bytes:"):
                                        read_bytes = int(line.split()[1])
                                    elif line.startswith("write_bytes:"):
                                        write_bytes = int(line.split()[1])
                        except OSError:
                            pass

                    prev = self._pid_stats.get(pid, {})
                    delta_read = None
                    delta_write = None
                    if read_bytes is not None and "read_bytes" in prev:
                        delta_read = read_bytes - prev["read_bytes"]
                    if write_bytes is not None and "write_bytes" in prev:
                        delta_write = write_bytes - prev["write_bytes"]

                    logger.debug(
                        "chdman pid=%s rss_kb=%s threads=%s read_bytes=%s(+%s) write_bytes=%s(+%s)",
                        pid,
                        rss_kb,
                        threads,
                        read_bytes,
                        delta_read,
                        write_bytes,
                        delta_write,
                    )

                    updated = {}
                    if read_bytes is not None:
                        updated["read_bytes"] = read_bytes
                    if write_bytes is not None:
                        updated["write_bytes"] = write_bytes
                    if updated:
                        self._pid_stats[pid] = updated
            except Exception as exc:
                logger.exception("Debug heartbeat error: %s", exc)

    async def _dispatcher_loop(self):
        """Dispatcher loop that starts jobs in FIFO order with concurrency control."""
        while self._running:
            try:
                _, job_id = await self._queue.get()
            except asyncio.CancelledError:
                break

            try:
                job = self.jobs.get(job_id)
                if not job:
                    continue
                if job_id in self._cancelled or job.status == JobStatus.CANCELLED:
                    self._cancelled.discard(job_id)
                    await self._cleanup_temp_dir(job)
                    continue
                await self._semaphore.acquire()
                try:
                    if self.max_concurrent == 1:
                        await self._run_job(job_id)
                    else:
                        asyncio.create_task(self._run_job(job_id))
                except Exception:
                    # _run_job() always releases the semaphore in its finally block.
                    # Only release here if task creation failed before _run_job started.
                    if self.max_concurrent != 1:
                        self._semaphore.release()
                    raise
            except Exception as e:
                logger.exception("Dispatcher error: %s", e)
            finally:
                self._queue.task_done()

    def _compute_output_size(self, job: ConversionJob) -> Optional[int]:
        """Total bytes of a completed job's output (None if absent).

        Single source of truth for every output shape: the primary output plus
        each companion the mode wrote (extractcd's .bin sidecar, a split
        folder_to_iso's numbered .iso.0/.1/… parts), enumerated from the owning
        tool's ``companion_outputs`` hook rather than re-encoded per mode. A
        split build leaves no bare .iso, so the primary getsize simply misses
        and the numbered parts carry the whole total. Synchronous (a directory
        mode's companion lookup scans the output dir) — call via
        ``run_in_threadpool`` off the event loop.
        """
        companions = registry.for_mode(job.mode.value).companion_outputs(
            job.output_path, job.mode.value,
        )
        total_size = 0
        for path in [job.output_path, *companions]:
            try:
                total_size += os.path.getsize(path)
            except OSError:
                pass
        return total_size if total_size > 0 else None

    async def _run_job(self, job_id: str):
        try:
            await self._process_job(job_id)
        finally:
            self._semaphore.release()

    async def _process_job(self, job_id: str):
        """Process a single conversion job."""
        job = self.jobs.get(job_id)
        if not job:
            return

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Starting job %s status=%s input=%s output=%s",
                job_id,
                job.status.value,
                job.file_path,
                job.output_path,
            )

        if job_id in self._cancelled or job.status == JobStatus.CANCELLED:
            self._cancelled.discard(job_id)
            await self._cleanup_temp_dir(job)
            await self._prune_jobs(exclude_id=job_id)
            return

        cancel_event = asyncio.Event()
        self._cancel_events[job_id] = cancel_event
        if job_id in self._cancelled or job.status == JobStatus.CANCELLED:
            cancel_event.set()
            self._cancelled.discard(job_id)
            if not job.completed_at:
                job.completed_at = datetime.now(timezone.utc)
            await self._notify_subscribers(
                job_id,
                {"type": "cancelled", "job_id": job_id, "status": job.status.value},
            )
            await self._cleanup_temp_dir(job)
            del self._cancel_events[job_id]
            await self._prune_jobs(exclude_id=job_id)
            return

        slot_acquired = await concurrency_manager.acquire(
            job_id, cancel_event=cancel_event
        )
        if not slot_acquired:
            if job.status != JobStatus.CANCELLED:
                job.status = JobStatus.CANCELLED
                job.completed_at = datetime.now(timezone.utc)
                await self._notify_subscribers(
                    job_id,
                    {"type": "cancelled", "job_id": job_id, "status": job.status.value},
                )
            await self._cleanup_temp_dir(job)
            concurrency_manager.release(job_id)
            if job_id in self._cancel_events:
                del self._cancel_events[job_id]
            await self._prune_jobs(exclude_id=job_id)
            return

        # If this job's path is inside a folder another job is currently packing
        # (makeps3iso folder->iso), don't fail — wait in the queue and retry once
        # that folder job releases its subtree lock, like any queued job.
        if await run_in_threadpool(self._blocked_by_dir_lock, job):
            await self._defer_blocked_job(job_id, output_lock_held=False)
            return

        # Try to acquire lock for the output file (prevents race conditions)
        lock_acquired = lock_manager.acquire_lock(
            job.output_path, allow_existing=job.allow_overwrite
        )
        if not lock_acquired:
            # A transient block by a folder->iso subtree lock (racing the
            # precheck) waits & re-queues rather than failing.
            if await run_in_threadpool(self._blocked_by_dir_lock, job):
                await self._defer_blocked_job(job_id, output_lock_held=False)
                return
            # Could not acquire lock - either file exists or is being converted
            # Check current status to provide better error message
            file_exists, is_locked = lock_manager.check_file_status(job.output_path)
            job.status = JobStatus.FAILED
            if is_locked:
                job.error_message = (
                    "Another job is already converting to this output file"
                )
            elif file_exists:
                job.error_message = "Output CHD file already exists"
            else:
                job.error_message = "Could not acquire lock for output file"
            job.completed_at = datetime.now(timezone.utc)

            await self._notify_subscribers(
                job_id, {"type": "error", "job_id": job_id, "error": job.error_message}
            )
            if job_id in self._cancel_events:
                del self._cancel_events[job_id]
            concurrency_manager.release(job_id)
            await self._cleanup_temp_dir(job)
            await self._prune_jobs(exclude_id=job_id)
            return

        # acquire_lock above already rejects a destination that exists but isn't a
        # plain file — incl. a *directory* shadowing the output path — via its
        # ``not os.path.isfile`` clause (for both overwrite states). What it can't
        # see is a prior split set (``Game.iso.0``/``.1`` with no bare
        # ``Game.iso``): os.path.exists(bare) is False, so the lock is granted. A
        # non-overwrite job would then clobber that existing deliverable and, on a
        # later failure, unlink its parts via remove_outputs. Mirror the bare-file
        # "already exists" rejection for the split set. (An authorized overwrite
        # clears it in _clear_existing_output.)
        if job.input_kind == InputKind.DIRECTORY and not job.allow_overwrite:
            existing_parts = await run_in_threadpool(
                registry.for_mode(job.mode.value).companion_outputs,
                job.output_path, job.mode.value,
            )
            # companion_outputs returns the numbered split parts only when a real
            # -s split set exists (a bare .iso is the primary, not a companion,
            # and is already rejected by acquire_lock above), so any companions
            # mean a prior split build already occupies the target.
            if existing_parts:
                job.status = JobStatus.FAILED
                job.error_message = "Output file already exists"
                job.completed_at = datetime.now(timezone.utc)
                await self._notify_subscribers(
                    job_id,
                    {"type": "error", "job_id": job_id, "error": job.error_message},
                )
                lock_manager.release_lock(job.output_path)
                if job_id in self._cancel_events:
                    del self._cancel_events[job_id]
                concurrency_manager.release(job_id)
                await self._cleanup_temp_dir(job)
                await self._prune_jobs(exclude_id=job_id)
                return

        # A directory-input job (makeps3iso folder->iso) additionally locks its
        # whole source subtree, so any concurrent per-file job / rename / delete
        # whose path falls inside the folder contends on the lock instead of
        # corrupting the in-flight ISO. A conflict here means another job is
        # already operating inside the folder; fail like an output collision.
        dir_lock_acquired = False
        if job.input_kind == InputKind.DIRECTORY:
            dir_lock_acquired = await run_in_threadpool(
                lock_manager.acquire_dir_lock, job.file_path,
            )
            if not dir_lock_acquired:
                # Another job is operating inside this folder; wait in the queue
                # and retry rather than failing (releases the output lock taken
                # just above).
                await self._defer_blocked_job(job_id, output_lock_held=True)
                return

        job.status = JobStatus.PROCESSING
        job.started_at = datetime.now(timezone.utc)
        processing_now = sum(
            1 for candidate in self.jobs.values() if candidate.status == JobStatus.PROCESSING
        )
        if processing_now > self.max_concurrent:
            logger.error(
                "Processing concurrency invariant violated: processing=%d max_concurrent=%d",
                processing_now,
                self.max_concurrent,
            )

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Job %s acquired locks and started processing", job_id)

        await self._notify_subscribers(
            job_id,
            {
                "type": "status",
                "job_id": job_id,
                "status": job.status.value,
                "progress": 0,
            },
        )

        try:
            input_path = job.file_path
            if "::" in job.file_path:
                extract_start = time.monotonic()
                archive_path, internal_path = job.file_path.split("::", 1)
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Job %s extracting archive %s member %s",
                        job_id,
                        archive_path,
                        internal_path,
                    )
                extract_task = asyncio.create_task(
                    run_in_threadpool(
                        archive_service.extract_file, archive_path, internal_path
                    )
                )
                extract_status_task = asyncio.create_task(
                    self._emit_extract_updates(
                        job_id, archive_path, internal_path, extract_task
                    )
                )
                input_path, temp_dir = await extract_task
                extract_status_task.cancel()
                try:
                    await extract_status_task
                except asyncio.CancelledError:
                    pass
                job.temp_dir = temp_dir
                await run_in_threadpool(
                    archive_service.extract_related_files,
                    archive_path,
                    internal_path,
                    temp_dir,
                )
                if logger.isEnabledFor(logging.DEBUG):
                    extracted_size = None
                    try:
                        extracted_size = os.path.getsize(input_path)
                    except OSError:
                        pass
                    logger.debug(
                        "Job %s extracted to %s size=%s in %.2fs",
                        job_id,
                        input_path,
                        extracted_size,
                        time.monotonic() - extract_start,
                    )
                if cancel_event.is_set():
                    raise ConversionCancelled("Conversion cancelled")

            if job.input_kind == InputKind.DIRECTORY:
                # Re-check directory-input safety after acquiring the source
                # subtree lock and immediately before invoking the native
                # recursive packer. A queued PS3 folder job may have been
                # valid when planned but mutated before it starts; this guards
                # the exact tree makeps3iso is about to read. Run it *before*
                # clearing any existing output so a safety rejection on an
                # overwrite job is non-destructive: the user's prior ISO/split
                # set is only removed once the source is confirmed still safe.
                if not await run_in_threadpool(is_safe_directory_tree, input_path):
                    job.status = JobStatus.FAILED
                    job.error_message = (
                        "PS3 folder contains symlinks, special files, "
                        "or paths outside configured volumes"
                    )
                    job.completed_at = datetime.now(timezone.utc)
                    await self._notify_subscribers(
                        job_id,
                        {
                            "type": "error",
                            "job_id": job_id,
                            "error": job.error_message,
                        },
                    )
                    return

            # The recursive PS3 safety walk above can take a while on a large
            # source tree; honor a cancellation requested during it before
            # deleting the user's existing output, mirroring the post-extract
            # cancel check. Otherwise an overwrite job cancelled mid-walk would
            # still clear the prior ISO/split set only to abort immediately.
            if cancel_event.is_set():
                raise ConversionCancelled("Conversion cancelled")

            await self._clear_existing_output(job)

            _convert_service = registry.for_mode(job.mode.value)
            async for update in _convert_service.convert(
                input_path,
                job.output_path,
                job.mode.value,
                compression=job.compression,
                split=job.split,
                cancel_event=cancel_event,
            ):
                if cancel_event.is_set():
                    continue
                job.progress = update["progress"]
                job.message = update["message"]
                now = time.monotonic()
                self._last_progress_at[job_id] = now
                if logger.isEnabledFor(logging.DEBUG):
                    last_log = self._last_progress_log_at.get(job_id, 0)
                    if now - last_log >= settings.debug_progress_interval:
                        self._last_progress_log_at[job_id] = now
                        logger.debug(
                            "Job %s progress=%s message=%s",
                            job_id,
                            job.progress,
                            job.message,
                        )

                await self._notify_subscribers(
                    job_id,
                    {
                        "type": "progress",
                        "job_id": job_id,
                        "progress": job.progress,
                        "message": job.message,
                    },
                )

            if job.status != JobStatus.CANCELLED:
                self._cancelled.discard(job_id)
                job.progress = 100

                # Run the tool's post-convert hook before sizing the output.
                # chdman uses it to embed disc-ID GAME/NAME tags into a freshly
                # created CD/DVD CHD; the default is a no-op, so this is safe to
                # call for every mode. Running it first means the output_size
                # computed below reflects the tagged file. The source
                # (input_path) is still present here, even when delete-on-verify
                # is requested (deletion happens after verify).
                try:
                    await registry.for_mode(job.mode.value).post_convert(
                        input_path, job.output_path, job.mode.value,
                    )
                except Exception as e:
                    logger.debug(
                        "Job %s post-convert hook skipped: %s", job_id, e
                    )

                # Output size = primary + companions (extractcd's .bin, a split
                # folder_to_iso's numbered parts), via the shared companion-aware
                # helper. Off the event loop: a split folder_to_iso's companion
                # lookup scans the output dir.
                job.output_size = await run_in_threadpool(
                    self._compute_output_size, job,
                )

                verified = False
                source_deleted = False
                if job.delete_on_verify:
                    if not registry.spec(job.mode.value).supports_delete_on_verify:
                        raise RuntimeError(
                            "Delete-on-verify is only supported for "
                            "create/copy/Dolphin/3DS/Switch-compress modes"
                        )
                    if cancel_event.is_set():
                        raise ConversionCancelled("Conversion cancelled")

                    verified = False
                    job.message = "Verifying output..."
                    await self._notify_subscribers(
                        job_id,
                        {
                            "type": "progress",
                            "job_id": job_id,
                            "progress": job.progress,
                            "message": job.message,
                        },
                    )

                    verify_result = await registry.for_mode(job.mode.value).verify(
                        job.output_path
                    )
                    if not verify_result.get("valid"):
                        raise RuntimeError(
                            f"Verification failed: {verify_result.get('message')}"
                        )

                    verified = True
                    await verification_store.mark_verified(
                        job.output_path, source_path=job.file_path
                    )

                    if cancel_event.is_set():
                        job.message = "Verification complete. Delete skipped (cancelled)."
                        await self._notify_subscribers(
                            job_id,
                            {
                                "type": "progress",
                                "job_id": job_id,
                                "progress": job.progress,
                                "message": job.message,
                            },
                        )
                    else:
                        delete_label = (
                            "source archive"
                            if "::" in job.file_path
                            else "source"
                        )
                        job.message = (
                            f"Verification complete. Deleting {delete_label}..."
                        )
                        await self._notify_subscribers(
                            job_id,
                            {
                                "type": "progress",
                                "job_id": job_id,
                                "progress": job.progress,
                                "message": job.message,
                            },
                        )

                        snapshot = self._delete_plans.get(job_id)
                        if not snapshot or not snapshot.get("paths"):
                            raise RuntimeError(
                                "Delete plan snapshot missing; refusing to delete"
                            )

                        expected_paths = list(snapshot.get("paths", []))
                        expected_set = set(expected_paths)
                        fingerprints = snapshot.get("fingerprints") or {}

                        current_plan = await run_in_threadpool(
                            build_delete_plan, job.file_path
                        )
                        if (
                            current_plan.get("errors")
                            or current_plan.get("unsafe_paths")
                            or current_plan.get("missing_paths")
                        ):
                            raise RuntimeError(
                                "Delete plan no longer safe; refusing to delete"
                            )
                        current_set = set(current_plan.get("delete_paths", []))
                        if current_set != expected_set:
                            raise RuntimeError(
                                "Delete plan changed; refusing to delete"
                            )

                        output_real = os.path.realpath(job.output_path)
                        source_for_delete = (
                            strip_archive_path(job.file_path)
                            if "::" in job.file_path
                            else job.file_path
                        )
                        source_real = os.path.realpath(source_for_delete)
                        delete_order = [
                            p
                            for p in expected_paths
                            if os.path.realpath(p) != source_real
                        ]
                        if source_real in expected_set:
                            delete_order.append(source_real)
                        else:
                            delete_order = expected_paths

                        for path in expected_paths:
                            _, is_locked = lock_manager.check_file_status(path)
                            if is_locked:
                                raise RuntimeError(
                                    "Delete path is locked by an active conversion"
                                )
                            within_volumes = await run_in_threadpool(
                                is_within_configured_volumes,
                                path,
                                treat_archives=False,
                            )
                            if not within_volumes:
                                raise RuntimeError(
                                    "Delete path outside configured volumes; refusing to delete"
                                )
                            if self._is_path_in_use_by_other_job(job_id, path):
                                raise RuntimeError(
                                    "Delete path is still in use by another job"
                                )
                            if os.path.islink(path):
                                raise RuntimeError(
                                    "Delete path is a symlink; refusing to delete"
                                )
                            try:
                                st = os.stat(path, follow_symlinks=False)
                            except FileNotFoundError as exc:
                                raise RuntimeError(
                                    "Delete path no longer exists; refusing to delete"
                                ) from exc
                            if not os.path.isfile(path):
                                raise RuntimeError(
                                    "Delete path is not a file; refusing to delete"
                                )
                            if os.path.realpath(path) == output_real:
                                raise RuntimeError(
                                    "Delete path matches output path; refusing to delete"
                                )
                            fp = fingerprints.get(path)
                            if not fp:
                                raise RuntimeError(
                                    "Delete fingerprint missing; refusing to delete"
                                )
                            mtime_ns = int(
                                getattr(
                                    st,
                                    "st_mtime_ns",
                                    int(st.st_mtime * 1_000_000_000),
                                )
                            )
                            if (
                                int(fp.get("size", -1)) != int(st.st_size)
                                or int(fp.get("mtime_ns", -1)) != mtime_ns
                            ):
                                raise RuntimeError(
                                    "Delete path fingerprint mismatch; refusing to delete"
                                )
                            snapshot_inode = int(fp.get("inode", 0) or 0)
                            snapshot_device = int(fp.get("device", 0) or 0)
                            current_inode = int(getattr(st, "st_ino", 0) or 0)
                            current_device = int(getattr(st, "st_dev", 0) or 0)
                            if (
                                (
                                    snapshot_inode > 0
                                    and current_inode > 0
                                    and snapshot_inode != current_inode
                                )
                                or (
                                    snapshot_device > 0
                                    and current_device > 0
                                    and snapshot_device != current_device
                                )
                            ):
                                raise RuntimeError(
                                    "Delete path fingerprint mismatch; refusing to delete"
                                )

                        for path in delete_order:
                            await run_in_threadpool(os.remove, path)

                        source_deleted = True

                        ext = os.path.splitext(job.file_path)[1].lower()
                        if ext in registry.verify_extensions():
                            await verification_store.clear(job.file_path)
                        if ext == ".chd":
                            await chd_metadata_store.clear(job.file_path)

                job.status = JobStatus.COMPLETED
                job.completed_at = datetime.now(timezone.utc)
                await self._notify_subscribers(
                    job_id,
                    {
                        "type": "complete",
                        "job_id": job_id,
                        "output_path": job.output_path,
                        "output_size": job.output_size,
                        "verified": verified,
                        "source_deleted": source_deleted,
                    },
                )

        except ConversionCancelled:
            self._cancelled.discard(job_id)
            job.status = JobStatus.CANCELLED
            job.completed_at = datetime.now(timezone.utc)
            await self._notify_subscribers(
                job_id,
                {"type": "cancelled", "job_id": job_id, "status": job.status.value},
            )
        except Exception as e:
            self._cancelled.discard(job_id)
            job.status = JobStatus.FAILED
            job.error_message = str(e)
            job.completed_at = datetime.now(timezone.utc)

            await self._notify_subscribers(
                job_id, {"type": "error", "job_id": job_id, "error": str(e)}
            )

        finally:
            # Only release lock if we acquired it
            if lock_acquired:
                lock_manager.release_lock(job.output_path)
            # Release the directory subtree lock held by a folder->iso job.
            if dir_lock_acquired:
                lock_manager.release_dir_lock(job.file_path)

            concurrency_manager.release(job_id)

            self._delete_plans.pop(job_id, None)
            if job_id in self._cancel_events:
                del self._cancel_events[job_id]
            self._last_progress_at.pop(job_id, None)
            self._last_progress_log_at.pop(job_id, None)
            self._last_stall_log_at.pop(job_id, None)
            self._last_output_size.pop(job_id, None)
            self._last_output_size_at.pop(job_id, None)

            # Clean up temp directory if this was an archive extraction
            await self._cleanup_temp_dir(job)
            await self._prune_jobs(exclude_id=job_id)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("Job %s finished status=%s", job_id, job.status.value)


job_manager = JobManager(
    max_concurrent=settings.max_concurrent_jobs,
    max_job_history=settings.max_job_history,
)
