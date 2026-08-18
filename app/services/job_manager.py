import asyncio
import inspect
import logging
from logging_setup import get_logger
import os
import resource
import shutil
import sys
import tempfile
import time
import uuid
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Deque, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from config import settings
from fastapi.concurrency import run_in_threadpool
from models import ConversionJob, ConversionMode, InputKind, JobStatus
from services.archive import archive_service
from services.chd_metadata_store import chd_metadata_store
from services.chdman import ConversionCancelled, chdman_service
from services.disc_id import DiscIdStorageAbandoned
from services.concurrency_manager import concurrency_manager
from services.lock_manager import lock_manager
from services.subprocess_runner import run_detached
from services.tools import ModeKind, registry
from services.verification_store import verification_store
from utils.delete_plan import build_delete_plan, build_delete_snapshot
from utils.path_utils import (
    is_safe_directory_tree,
    is_within_configured_volumes,
    source_companions_are_safe,
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


def _lexical_path(path: str) -> str:
    """*path* as a comparable key without touching the filesystem.

    The answer when the volume will not say: absolute and normalised, so two
    spellings of the same path still collide, but symlink aliases do not.
    """
    return os.path.normpath(os.path.abspath(path))


def _canonical_path(path: str, resolved: Optional[Mapping[str, str]] = None) -> str:
    """*path* as one comparable key: symlinks resolved, or the path as given.

    Split out from :func:`_paths_collide` so a caller comparing one path
    against many resolves each of them once instead of per comparison --
    `realpath` is a blocking stat chain.

    Pass *resolved* (from :func:`_resolve_paths_bounded`) and this never touches
    the filesystem at all: it reads the pre-computed key, falling back to the
    lexical one for a path the pre-flight did not see. That is the form the
    reservation uses, because its work happens on the event loop under
    ``_create_lock`` -- one `realpath` into a dead NFS/SMB mount there is
    uninterruptible, and it freezes every unrelated request in the process
    along with all job creation.
    """
    if resolved is not None:
        return resolved.get(path) or _lexical_path(path)
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _paths_collide(
    path_a: str, path_b: str, resolved: Optional[Mapping[str, str]] = None,
) -> bool:
    return _canonical_path(path_a, resolved) == _canonical_path(path_b, resolved)


# How long the whole pre-flight canonicalisation may take before the batch
# proceeds on lexical keys. Generous, because it covers every path in one
# submit and a healthy mount answers in microseconds; the point is only that a
# mount which never answers cannot hold up job creation forever.
_CANONICAL_PROBE_SECONDS = 20.0


async def _resolve_paths_bounded(paths: Iterable[str]) -> Dict[str, str]:
    """``{path: canonical key}`` for *paths*, resolved off the event loop.

    Every key is present before the resolution starts, seeded lexically, and
    upgraded in place as each `realpath` returns -- so a bound that expires
    part-way keeps the paths that did answer instead of discarding the lot.

    Detached rather than pooled for the usual reason: a `realpath` on an
    unresponsive mount cannot be cancelled, only abandoned, and abandoning a
    shared pool worker per submit would eventually starve every unrelated
    offload. The thread here is disposable.
    """
    pending = list(dict.fromkeys(paths))
    resolved: Dict[str, str] = {path: _lexical_path(path) for path in pending}
    if not pending:
        return resolved

    def _resolve_all() -> None:
        for path in pending:
            try:
                resolved[path] = os.path.realpath(path)
            except OSError:
                pass  # keep the lexical seed

    try:
        await asyncio.wait_for(
            run_detached(_resolve_all), _CANONICAL_PROBE_SECONDS,
        )
    except (asyncio.TimeoutError, OSError):
        logger.warning(
            "Canonicalising %d destination(s) did not finish in %.1fs; "
            "reserving on lexical paths (a volume is not answering)",
            len(pending),
            _CANONICAL_PROBE_SECONDS,
        )
    return resolved


# The statuses a job never leaves. Anything else is still in flight.
_TERMINAL_STATUSES = (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)


class OutputClaimedError(ValueError):
    """Raised when a destination another live job holds is requested again.

    A ``ValueError`` subclass so the callers that already treat a rejected
    spec as a bad request keep working, but its own type because this is a
    *concurrency* outcome rather than a malformed request: two submissions
    resolved the same destination, and the loser should be told to retry or
    skip (409) rather than shown a 500.
    """

    def __init__(self, detail: str, *, claimed_by: str | None = None):
        super().__init__(detail)
        self.detail = detail
        self.claimed_by = claimed_by


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
    MAX_TRACKED_EVICTED_IDS = 2000

    def __init__(self, max_concurrent: int = 1, max_job_history: int = 500):
        self.jobs: OrderedDict[str, ConversionJob] = OrderedDict()
        self._archived_jobs: OrderedDict[str, Tuple[ConversionJob, float]] = OrderedDict()
        # Called once per job that reaches a terminal status; see
        # `add_terminal_listener` for why anything caring about *how* a job
        # ended has to be told rather than ask later.
        self._terminal_listeners: List[Callable[[ConversionJob], object]] = []
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
        # job id -> the canonical key its output was reserved under. Recorded
        # at queue time because the reservation's pre-flight map is built
        # *before* `_create_lock` is taken (see `_reservation_keys`): two
        # submissions can both pre-resolve, and the second's map cannot contain
        # a job the first queues in between. Without the key kept here the
        # second would fall back to the first job's lexical path, and two
        # spellings of one file -- a symlinked directory and its target --
        # would each be accepted. Dropped when the job leaves the queue; only
        # live jobs are ever consulted.
        self._output_keys: Dict[str, str] = {}
        # Jobs currently inside the verify phase, mapped to the monotonic clock
        # reading when that phase began. Verification emits no progress and can
        # legitimately run for many minutes, so the stalled-job warning reports
        # it against its own elapsed time and in its own words rather than
        # calling healthy work stalled (or, as before issue #266, saying nothing
        # at all about a job genuinely wedged in verify).
        self._verifying: dict[str, float] = {}
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
        # Running tally of terminal jobs the history cap has evicted, keyed
        # status → mode → count. `self.jobs` can only ever answer "how much
        # history is still retained", so once a run passes max_job_history
        # every UI count derived from it freezes at the cap while work keeps
        # finishing. This is the missing half: retained + evicted is the real
        # number. Keyed by mode as well as status so a client can apply the
        # same external-scan filter it applies to live jobs (the Jobs panel
        # hides metadata_scan / dat_match unless asked for them).
        self._evicted_history: Dict[str, Dict[str, int]] = {}
        # Ids of recently evicted jobs, each stamped with a monotonic sequence
        # number. A client learns of a completion (SSE) before the prune that
        # the completion triggers, so for a moment it holds a row the backend
        # has since deleted; adding the eviction tally on top of that row would
        # double-count it until the next snapshot poll. Replaying the ids lets
        # the client drop the stale rows in the same beat it takes the new
        # totals. The window scales with the history cap — a bigger retained
        # history means bigger client snapshots and more ids in flight — and a
        # client that still falls off the end is told its cursor expired so it
        # can re-sync rather than silently keep a row the cap deleted.
        self._evicted_ids: Deque[Tuple[int, str]] = deque(
            maxlen=max(self.MAX_TRACKED_EVICTED_IDS, self.max_job_history * 4)
        )
        self._eviction_seq = 0
        # Where the last Clear landed. A reset drops the id log, so a cursor
        # from before it has no tombstones to replay — and since Clear deletes
        # every finished job, a client whose cursor predates one is holding
        # rows that no longer exist. Both make such a cursor expired.
        self._last_reset_seq = 0
        # Identifies this process's eviction log. The sequence lives in memory
        # and restarts at 0 when the backend does, so a client that compares
        # sequences to reject stale payloads would reject every payload from
        # the new process until it out-counted the old one — badges frozen for
        # the rest of the browser session. A changed generation is the signal
        # to drop the cursor instead of trusting it.
        self.history_generation = uuid.uuid4().hex[:12]

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
        verify_after: bool = False,
        split: bool = False,
        delete_snapshot: Optional[Dict[str, object]] = None,
        resolved: Optional[Mapping[str, str]] = None,
    ) -> ConversionJob:
        """Queue a job while holding _create_lock (no backpressure check here)."""
        job_id = str(uuid.uuid4())[:8]
        filename = filename_override or os.path.basename(file_path)

        output_path = self._resolve_output_locked(
            file_path, mode, output_dir=output_dir, output_path=output_path,
            resolved=resolved,
        )


        # Carry the mode's input kind end-to-end so the pipeline skips the
        # archive-extract / file-only assumptions for a directory job and the
        # lock manager can protect the whole source subtree. Derived from the
        # registry spec (every conversion mode is registered; external jobs
        # bypass this path), so FILE/DIRECTORY can't drift via a typo.
        try:
            input_kind = registry.mode_input_kind(mode.value)
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
            verify_after=verify_after,
            split=split,
            input_kind=input_kind,
        )

        self.jobs[job_id] = job
        # The key this destination is claimed under, for the reservation that
        # runs after this one. See `_output_keys`.
        self._output_keys[job_id] = _canonical_path(output_path, resolved)
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
        verify_after: bool = False,
        split: bool = False,
        delete_snapshot: Optional[Dict[str, object]] = None,
    ) -> ConversionJob:
        """Create a new conversion job."""
        specs: List[Dict[str, object]] = [{
            "file_path": file_path,
            "output_dir": output_dir,
            "output_path": output_path,
        }]
        # Outside the lock, and off the event loop: see `_reservation_keys`.
        resolved = await self._reservation_keys(specs, mode)
        async with self._create_lock:
            self._enforce_queue_backpressure_locked(1)
            # Same destination reservation the batch path gets: a single
            # submit racing a sweep is the same collision with one fewer file.
            self._reject_claimed_destinations_locked(specs, mode, resolved)
            job = self._queue_job_locked(
                file_path=file_path,
                mode=mode,
                output_dir=output_dir,
                output_path=output_path,
                allow_overwrite=allow_overwrite,
                filename_override=filename_override,
                compression=compression,
                delete_on_verify=delete_on_verify,
                verify_after=verify_after,
                split=split,
                delete_snapshot=delete_snapshot,
                resolved=resolved,
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
        verify_after: bool = False,
    ) -> List[ConversionJob]:
        """Create multiple jobs atomically under a single backpressure check."""
        if not job_specs:
            return []

        jobs: List[ConversionJob] = []
        # Outside the lock, and off the event loop: see `_reservation_keys`.
        resolved = await self._reservation_keys(job_specs, mode)
        async with self._create_lock:
            self._enforce_queue_backpressure_locked(len(job_specs))
            self._reject_claimed_destinations_locked(job_specs, mode, resolved)
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
                        verify_after=verify_after,
                        split=split,
                        delete_snapshot=spec.get("delete_snapshot"),
                        resolved=resolved,
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
        split: bool = False,
        verify_after: bool = False,
        output_paths: Optional[Dict[str, str]] = None,
        allow_overwrite: bool = False,
    ) -> List[ConversionJob]:
        """Create multiple conversion jobs.

        ``output_paths`` overrides the derived destination per input, and
        ``allow_overwrite`` authorises writing over an existing one. Both exist
        so a caller that has already resolved duplicates (the RomM sweep's
        rename/overwrite policy) hands the decision down instead of the queue
        re-deriving a different answer.
        """
        job_specs: List[Dict[str, object]] = []
        for fp in file_paths:
            snapshot = delete_snapshots.get(fp) if delete_snapshots else None
            spec: Dict[str, object] = {
                "file_path": fp,
                "output_dir": output_dir,
                "delete_snapshot": snapshot,
                "allow_overwrite": allow_overwrite,
            }
            override = output_paths.get(fp) if output_paths else None
            if override:
                spec["output_path"] = override
            job_specs.append(spec)
        return await self.create_jobs_atomic(
            job_specs,
            mode,
            compression=compression,
            delete_on_verify=delete_on_verify,
            split=split,
            verify_after=verify_after,
        )

    @staticmethod
    def _derive_output(
        file_path: str, mode: ConversionMode, output_dir: Optional[str],
    ) -> str:
        """Where *mode* would write *file_path*, as pure derivation.

        Every tool's ``output_path()`` is string work over the stem and the
        mode's suffix -- no stat, no listing. That is what lets the reservation
        canonicalise its destinations *before* taking ``_create_lock``: the
        paths are known without touching the filesystem, so only the
        (bounded, off-loop) `realpath` needs the volume to answer.
        """
        return registry.for_mode(mode.value).output_path(
            mode.value, file_path, output_dir,
        )

    async def _reservation_keys(
        self, job_specs: List[Dict[str, object]], mode: ConversionMode,
    ) -> Dict[str, str]:
        """Pre-resolve every path the destination reservation will compare.

        Runs before ``_create_lock`` is taken, so the critical section itself
        does no filesystem work: see :func:`_canonical_path`. Covers each
        spec's source and derived destination plus every live job's output,
        because the reservation compares the first set against the second.

        A spec whose destination cannot be derived (unknown mode, unsupported
        extension) is skipped rather than raised on -- the locked pass runs the
        same derivation and produces the caller-facing error there, once.
        """
        paths: List[str] = []
        for spec in job_specs:
            file_path = str(spec["file_path"])
            paths.append(file_path)
            explicit = spec.get("output_path")
            if explicit is not None:
                paths.append(str(explicit))
                continue
            output_dir = spec.get("output_dir")
            try:
                paths.append(self._derive_output(
                    file_path,
                    mode,
                    str(output_dir) if output_dir is not None else None,
                ))
            except (KeyError, ValueError):
                continue
        paths.extend(
            job.output_path
            for job in self.jobs.values()
            if job.output_path
            and job.status in (JobStatus.QUEUED, JobStatus.PROCESSING)
        )
        return await _resolve_paths_bounded(paths)

    def _resolve_output_locked(
        self,
        file_path: str,
        mode: ConversionMode,
        *,
        output_dir: Optional[str] = None,
        output_path: Optional[str] = None,
        resolved: Optional[Mapping[str, str]] = None,
    ) -> str:
        """The destination this job will write, with the pre-creation guards.

        Split out so a batch can be validated in full before a single job is
        created: raising partway through the creation loop left the earlier
        jobs registered and running while the caller was told the whole batch
        had failed -- and the RomM submit path then retired the re-pin rows for
        conversions that were, in fact, under way.
        """
        if output_path is not None:
            return output_path
        output_path = self._derive_output(file_path, mode, output_dir)
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
        if spec.kind != ModeKind.COPY and _paths_collide(
            output_path, file_path, resolved,
        ):
            raise ValueError(
                "Output path matches input; refusing to overwrite source"
            )
        return output_path

    def _reject_claimed_destinations_locked(
        self,
        job_specs: List[Dict[str, object]],
        mode: ConversionMode,
        resolved: Optional[Mapping[str, str]] = None,
    ) -> None:
        """Refuse the batch if any destination is already spoken for.

        No two live jobs may write the same file. Callers resolve duplicates
        before submitting, but that resolution is a prediction made outside
        this lock: a manual submit and an automation sweep can each settle on
        the same destination before either job starts and takes it, and the
        second then overwrites the first's result -- with delete-on-verify,
        removing both sources for one surviving output.

        Every destination is checked before any job is created, so the batch
        stays all-or-nothing. Intra-batch collisions count too: two specs of
        one submit resolving to the same path is the same bug arriving twice
        at once.
        """
        # Canonicalise once per path and compare keys. The pairwise version of
        # this re-resolved every earlier destination for every new one, and the
        # live-job lookup re-resolved every live job's output per spec --
        # quadratic in the batch size, in blocking `realpath` calls, on the
        # event loop while `_create_lock` is held. A Select-All submit of a few
        # thousand files made that millions of stat chains against exactly the
        # remote mounts this integration exists for.
        #
        # The keys themselves come pre-resolved from `_reservation_keys`, run
        # off the loop before the lock: even one `realpath` here is a stat
        # chain into the same remote mount, and one that never returns takes
        # the whole process with it.
        active = self._active_output_map(resolved)
        planned: Dict[str, str] = {}   # canonical destination -> source
        for spec in job_specs:
            file_path = str(spec["file_path"])
            output_dir = spec.get("output_dir")
            explicit = spec.get("output_path")
            destination = self._resolve_output_locked(
                file_path,
                mode,
                output_dir=str(output_dir) if output_dir is not None else None,
                output_path=str(explicit) if explicit is not None else None,
                resolved=resolved,
            )
            key = _canonical_path(destination, resolved)
            claimed_by = active.get(key)
            if claimed_by is not None:
                raise OutputClaimedError(
                    f"Another queued job ({claimed_by}) is already writing "
                    f"{os.path.basename(destination)}",
                    claimed_by=claimed_by,
                )
            other = planned.get(key)
            if other is not None:
                raise OutputClaimedError(
                    "Two files in this batch would write the same output: "
                    f"{os.path.basename(other)} and "
                    f"{os.path.basename(file_path)}",
                )
            planned[key] = file_path

    def _active_output_map(
        self, resolved: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, str]:
        """``{canonical output path: job id}`` for every live job.

        Built once per batch rather than re-derived per spec: each entry costs
        a `realpath`, which is a blocking stat chain, and this runs under
        ``_create_lock`` on the event loop -- so the keys come from the
        pre-flight in *resolved*. Primary outputs only -- companions are
        covered by the conflict probe callers already run.
        """
        active: Dict[str, str] = {}
        for job in self.jobs.values():
            if job.status not in (JobStatus.QUEUED, JobStatus.PROCESSING):
                continue
            if job.output_path:
                # The key the job was queued under, when it has one: the
                # pre-flight map in *resolved* is older than any job queued
                # since it was built, and falling back to lexical for those
                # would miss an alias of a path already claimed.
                key = self._output_keys.get(job.id)
                if key is None:
                    key = _canonical_path(job.output_path, resolved)
                active.setdefault(key, job.id)
        return active

    def get_job(self, job_id: str) -> Optional[ConversionJob]:
        """Get a job by ID."""
        return self.jobs.get(job_id)

    def add_terminal_listener(self, callback: Callable[[ConversionJob], object]) -> None:
        """Call *callback* once each job reaches a terminal status.

        For consumers that must remember how a job *ended* after the queue has
        forgotten it. History is capped and lives in memory, so asking
        ``get_job()`` later answers "unknown" for anything pruned or predating
        a restart -- and a consumer that then guesses from the filesystem
        cannot tell a finished conversion from a failed one that unlinked the
        old artifact. A listener is told at the moment the answer is still
        known, and can persist it wherever it needs it.

        The callback may be sync or async (a coroutine is scheduled on the
        loop). It runs after the status is final; exceptions are logged and
        swallowed, because a listener must never fail a conversion.
        """
        self._terminal_listeners.append(callback)

    def _notify_terminal(self, job: ConversionJob) -> None:
        """Tell every listener that *job* is finished. Never raises."""
        if not self._terminal_listeners:
            return
        if job.status not in _TERMINAL_STATUSES:
            return
        for callback in list(self._terminal_listeners):
            try:
                result = callback(job)
            except Exception:  # a listener must never fail a conversion
                logger.warning(
                    "Terminal-job listener failed for %s", job.id, exc_info=True,
                )
                continue
            if inspect.isawaitable(result):
                try:
                    self._spawn_background(result)
                except RuntimeError:
                    # No running loop (a synchronous test harness): the
                    # coroutine is simply not awaited, and the consumer's own
                    # fallback still applies.
                    result.close()

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
        self._notify_terminal(job)
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
        self._notify_terminal(job)
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
        """Sibling *input* files an active job also reads, so rename/delete
        can't pull one out from under a running conversion.

        Two sources, because the two kinds of multi-file input are described
        differently: ``.cue``/``.gdi`` track files are parsed out of the file's
        contents (``build_delete_plan``), while a tool whose source is a *set*
        declares its members through the registry (``source_companions`` — a
        split Wii U dump's ``game_part2.wud`` … ``game_part12.wud``). Without
        the second, deleting or renaming part 5 mid-run would fail a conversion
        that may already have cleared an existing output for overwrite.

        Stays cheap: this helper is called synchronously from async route code,
        and ``source_companions`` is pure name math for every tool that has none.
        """
        if "::" in file_path:
            return []
        if not os.path.isfile(file_path):
            return []
        ext = Path(file_path).suffix.lower()
        if ext in {".cue", ".gdi"}:
            try:
                # Already includes source_companions; one call covers both.
                candidates = build_delete_plan(file_path).get("delete_paths", [])
            except Exception:
                return []
        else:
            candidates = registry.source_companions(file_path)
        source_real = os.path.realpath(file_path)
        tracks = []
        for path in candidates:
            if os.path.realpath(path) == source_real:
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
        # Tool-neutral: the mode's own plugin enumerates everything to sweep
        # (primary + companions, or for makeps3iso the base plus any numbered
        # split parts). Directory-input jobs take this same path — there is no
        # per-tool branch here, so a second folder-input tool gets correct
        # cleanup instead of inheriting makeps3iso's part logic.
        #
        # Companions are cleared even when the primary is already gone: a lone
        # companion (e.g. a stray extractcd .bin whose .cue was deleted) is what
        # made check_output_conflicts authorize the overwrite, so it must not be
        # left to collide with the new output. Validate the whole set first — a
        # non-file occupant (a directory squatting on the primary or a companion
        # name) can't be unlinked, so reject before removing anything rather than
        # deleting the primary and then failing against the stray occupant.
        tool = registry.for_mode(job.mode.value)
        mode, output_path = job.mode.value, job.output_path

        # Enumeration is inside the hop, not before it: a tool's
        # ``overwrite_targets`` may probe the disk (makeps3iso walks .0/.1/…),
        # which would otherwise block the event loop on a slow/network volume.
        await run_in_threadpool(
            lambda: self._sweep_overwrite_targets(
                tool.overwrite_targets(output_path, mode),
            ),
        )
        # Unconditional: an authorized overwrite replaces whatever lives at this
        # path, so a verification record for it is stale even when the sweep
        # removed nothing — the prior output may have been deleted by something
        # else while the job sat in the queue. Letting the record survive would
        # report the freshly built, unverified artifact as verified.
        await verification_store.clear(output_path)

    @staticmethod
    def _sweep_overwrite_targets(targets: list[str]) -> None:
        """Validate then unlink ``targets``.

        Blocking stat/unlink work, kept in one sync helper so the caller runs
        the whole enumerate-check-remove sequence off the event loop in a single
        hop (the check must stay atomic with respect to its own removals).

        Uses ``lexists``/``islink`` rather than ``exists``/``isfile``: both of
        the latter *follow* symlinks and so report False for a **dangling** one,
        which would leave it in place for the converter to write through —
        potentially landing the output outside the validated volume. Unlinking a
        symlink removes the link itself and never its target, so a link sitting
        on an authorized-overwrite path is safe to clear; anything else that
        isn't a regular file (a directory, a device node) can't be unlinked at
        all and is rejected before anything is removed.
        """
        def _removable(path: str) -> bool:
            return os.path.islink(path) or os.path.isfile(path)

        for target in targets:
            if os.path.lexists(target) and not _removable(target):
                raise RuntimeError("Output path exists and is not a file")
        for target in targets:
            if os.path.lexists(target) and _removable(target):
                os.remove(target)

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
        """Trim finished jobs down to ``max_job_history``.

        The cap counts **terminal** jobs only, never the queue. It used to gate on
        the total job count, which conflated pending work with history: submitting
        more than ``max_job_history`` files at once (easy now that Select All spans
        every page — a Redump platform set is thousands) put the total permanently
        over the cap, so every sweep deleted every terminal job it could find and
        still couldn't get under it. The Completed and Failed tabs emptied
        continuously mid-batch, and a run finished with no record of which files
        failed. Queued jobs were never at risk — they simply aren't removable — but
        they were being *counted*, which is what evicted the history.

        Pending and processing jobs are therefore ignored here: a deep queue is a
        queue, not a backlog of history to trim. ``exclude_id`` (the job that just
        finished) still counts toward the cap but is never itself deleted.
        """
        if self.max_job_history <= 0:
            return

        # Insertion order is creation order, so this is oldest-first and the
        # slice below evicts the oldest history first.
        terminal_ids = [
            job_id
            for job_id, job in self.jobs.items()
            if job.status
            in (
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            )
        ]
        excess = len(terminal_ids) - self.max_job_history
        if excess <= 0:
            return

        removable = [job_id for job_id in terminal_ids if job_id != exclude_id]
        for job_id in removable[:excess]:
            evicted = self.jobs.get(job_id)
            if await self.delete_job(job_id) and evicted is not None:
                self._record_history_eviction(evicted)

    # ------------------------------------------------------------------
    # History overflow: what the cap has already thrown away
    # ------------------------------------------------------------------

    def _record_history_eviction(self, job: ConversionJob) -> None:
        """Tally a terminal job the history cap just evicted.

        Only automatic eviction lands here. A job the user deletes (single
        row, or Clear) is history they chose to drop, not history we dropped
        behind their back, so it must not keep inflating the counts.
        """
        status = getattr(job.status, "value", str(job.status))
        mode = getattr(job.mode, "value", str(job.mode))
        by_mode = self._evicted_history.setdefault(status, {})
        by_mode[mode] = by_mode.get(mode, 0) + 1
        self._eviction_seq += 1
        self._evicted_ids.append((self._eviction_seq, job.id))

    def get_history_overflow(
        self,
        since: Optional[int] = None,
        generation: Optional[str] = None,
    ) -> Dict[str, object]:
        """Counts of terminal jobs evicted by the ``max_job_history`` cap.

        Clients add these to the jobs they can still see to report a true
        total: the retained list stops growing at the cap, the work doesn't.

        ``since`` is a previously returned ``seq``; pass it to also get the ids
        evicted after that point, so a client can drop rows it still holds for
        jobs the cap has already deleted (otherwise it would count them twice —
        once as a retained row, once in the tally). Omit it to get counts only.

        ``generation`` identifies this process's log. The sequence is in-memory
        and restarts at 0 with the backend, so a client comparing sequences
        across a restart would reject every newer payload as stale; a changed
        generation tells it to drop its cursor instead. Pass the generation the
        cursor came from: a cursor minted by an earlier process means nothing
        here, and reading it literally would silently return no ids — so it is
        rewound to the start of this log instead.

        ``cursor_expired`` says the cursor fell off the end of the bounded log,
        so the ids cannot be complete. The client must then re-sync against the
        job list rather than trust the rows it holds.
        """
        cursor = since
        cursor_expired = False
        if cursor is not None and generation is not None and generation != self.history_generation:
            cursor = 0
        evicted_ids: List[str] = []
        if cursor is not None:
            evicted_ids = [job_id for seq, job_id in self._evicted_ids if seq > cursor]
            # The oldest entry we still hold is the furthest back we can speak
            # for. A cursor older than that may be missing ids we dropped. An
            # empty log holds nothing back: it means nothing has been evicted
            # since startup or since Clear, so there is nothing to replay.
            oldest_held = self._evicted_ids[0][0] if self._evicted_ids else None
            cursor_expired = oldest_held is not None and cursor < oldest_held - 1
            # A Clear throws the log away, tombstones included, so a cursor
            # from before it can't be answered either — and a Clear deletes
            # *every* finished job, so a client reading the list either side
            # of one is holding rows that no longer exist anywhere.
            if cursor < self._last_reset_seq:
                cursor_expired = True
        return {
            "generation": self.history_generation,
            "cursor_expired": cursor_expired,
            "max_job_history": self.max_job_history,
            "evicted": {
                status: dict(by_mode) for status, by_mode in self._evicted_history.items()
            },
            "total_evicted": sum(
                count for by_mode in self._evicted_history.values() for count in by_mode.values()
            ),
            "seq": self._eviction_seq,
            "evicted_ids": evicted_ids,
        }

    def history_eviction_seq(self) -> int:
        """Current position in the eviction log, for opening a client cursor."""
        return self._eviction_seq

    def history_overflow_total(self) -> int:
        """How many finished jobs the cap has evicted and is still counting."""
        return sum(
            count for by_mode in self._evicted_history.values() for count in by_mode.values()
        )

    def reset_history_overflow(self) -> int:
        """Forget the evicted-history tally (Clear wipes history wholesale).

        The sequence *advances* rather than rewinding. It is a cursor clients
        hold, so rewinding it would make a stale cursor look current; advancing
        it makes every read taken before the reset recognizably older, which is
        how a client rejects an in-flight hydration response that would
        otherwise restore the tally it just cleared. Returns the new sequence.
        """
        self._evicted_history.clear()
        self._evicted_ids.clear()
        self._eviction_seq += 1
        self._last_reset_seq = self._eviction_seq
        return self._eviction_seq

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
            # A queued job cancelled before it starts never enters
            # `_process_job`, so its listeners have to be told here.
            self._notify_terminal(job)
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
            self._output_keys.pop(job_id, None)
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

    def _log_stalled_jobs(self) -> None:
        """Warn about a PROCESSING job whose progress has gone quiet.

        Runs at the default log level and *outside* the DEBUG-only heartbeat
        block: a job wedged mid-conversion is precisely the state `is_stuck()`
        cannot see -- it bails early on any PROCESSING conversion -- so without
        this the sole trace of a frozen queue was a debug line nobody would find
        (issue #263).

        Jobs in the verify phase are reported *as verifying*, not as stalled.
        They used to be skipped outright, because verification emits no progress
        and legitimately runs for many minutes on a large image -- but that also
        meant a job genuinely wedged **in** verify logged nothing at all, which
        is exactly the state issue #266 is about. Verify is now bounded and
        cancellable, so the honest report is what phase the job is in and how
        long it has been there; an operator reading the log can tell a long
        checksum from a hang by whether the line keeps repeating past what the
        file's size can justify.

        Deliberately touches no filesystem. Running at the default log level
        means running for every processing job on every heartbeat, and the mount
        this is meant to report on is precisely the one where ``stat`` blocks in
        uninterruptible I/O -- which would freeze the event loop it is diagnosing.
        ``idle_for`` comes from the in-memory progress clock, and the job message
        already carries bytes written and rate for tools using the size-growth
        fallback.
        """
        if settings.debug_progress_timeout <= 0:
            return
        now = time.monotonic()
        for job in list(self.jobs.values()):
            if job.status != JobStatus.PROCESSING:
                continue
            verifying_since = self._verifying.get(job.id)
            # In verify, the phase's own clock is the meaningful one: the
            # progress clock stopped at the end of the conversion, so it would
            # report the verify as having been idle since before it started.
            idle_for = now - (
                verifying_since if verifying_since is not None
                else self._last_progress_at.get(job.id, now)
            )
            if idle_for < settings.debug_progress_timeout:
                continue
            # None, not 0, for "never logged": monotonic() counts from boot, so
            # a 0 sentinel reads as "logged at boot" and suppressed the first
            # warning for the first debug_progress_timeout seconds of uptime.
            last_stall = self._last_stall_log_at.get(job.id)
            if last_stall is not None and now - last_stall < settings.debug_progress_timeout:
                continue
            self._last_stall_log_at[job.id] = now
            if verifying_since is not None:
                logger.warning(
                    "Verifying job %s has been in the verify phase for %.1fs "
                    "(bounded; cancellable) input=%s output=%s started_at=%s",
                    job.id,
                    idle_for,
                    job.file_path,
                    job.output_path,
                    job.started_at,
                )
                continue
            logger.warning(
                "Stalled job %s idle=%.1fs progress=%s message=%s input=%s output=%s "
                "started_at=%s",
                job.id,
                idle_for,
                job.progress,
                job.message,
                job.file_path,
                job.output_path,
                job.started_at,
            )

    async def _debug_loop(self):
        cleanup_counter = 0
        while self._running:
            try:
                await asyncio.sleep(settings.debug_heartbeat_interval)

                # Handle background maintenance tasks
                cleanup_counter = await self._handle_background_maintenance(cleanup_counter)

                # Before the DEBUG gate: this warning must reach a default-level
                # log, not just a debug one.
                self._log_stalled_jobs()

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

    @staticmethod
    def _norm_fps(
        fingerprints: Optional[Dict[str, Dict[str, object]]],
    ) -> Dict[str, Dict[str, int]]:
        """Normalize a ``{realpath: {size, mtime_ns, inode, device}}`` map to
        plain ints, so a stored (JSON-round-tripped) fingerprint and a freshly
        computed one compare by equality. ``inode``/``device`` are carried
        alongside size+mtime_ns — the same four fields the delete-safety
        snapshot trusts to authorize an irreversible delete, so a reuse is held
        to no weaker a bar (a copy-restore lands on a new inode, a moved mount on
        a new device)."""
        out: Dict[str, Dict[str, int]] = {}
        for path, fp in (fingerprints or {}).items():
            out[path] = {
                "size": int(fp.get("size", -1)),
                "mtime_ns": int(fp.get("mtime_ns", -1)),
                "inode": int(fp.get("inode", 0) or 0),
                "device": int(fp.get("device", 0) or 0),
            }
        return out

    def _source_fingerprint(
        self, source_path: str,
    ) -> Optional[Dict[str, Dict[str, int]]]:
        """Stat fingerprint of the *complete* source set for ``source_path``.

        Uses ``build_delete_snapshot`` so a ``.cue`` / ``.gdi`` descriptor's
        referenced track files are fingerprinted too — a replaced ``.bin`` track
        (with the descriptor untouched) changes the fingerprint, which a
        descriptor-only check would miss. Returns ``{realpath: {size, mtime_ns,
        inode, device}}`` or ``None`` when the set can't be safely enumerated
        (unsafe/missing tracks, a non-file), so those never qualify for reuse.
        """
        try:
            snapshot = build_delete_snapshot(source_path)
        except Exception:
            return None
        return self._norm_fps(snapshot.get("fingerprints")) or None

    @staticmethod
    def _output_fingerprint(output_path: str) -> Optional[Dict[str, int]]:
        """Stat fingerprint (size + mtime_ns + inode + device) of an output
        file, or ``None``. inode/device catch a replaced file that happens to
        preserve size+mtime (e.g. a copy-restore onto a fresh inode)."""
        try:
            st = os.stat(output_path, follow_symlinks=False)
        except OSError:
            return None
        if not os.path.isfile(output_path):
            return None
        return {
            "size": int(st.st_size),
            "mtime_ns": int(
                getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
            ),
            "inode": int(getattr(st, "st_ino", 0) or 0),
            "device": int(getattr(st, "st_dev", 0) or 0),
        }

    def _build_produced_meta(self, job: ConversionJob) -> Optional[Dict[str, object]]:
        """Snapshot of what produced this verified artifact, for the re-run fast
        path: the mode + output-shaping settings and stat fingerprints of the
        complete source set and the output.

        The source fingerprint is the **pre-conversion** delete snapshot
        (captured when the job was planned, before the converter read the
        source), re-validated to still describe the on-disk source here. If the
        source changed between planning and this point — e.g. mutated while the
        converter was running — the pre-conversion snapshot no longer matches and
        no reusable metadata is recorded, so a later re-queue can't take the
        no-op path against an output built from now-stale bytes.

        Returns ``None`` (record no meta → no fast path) for an archive-member
        source, when there is no pre-conversion snapshot, when the source moved
        since planning, or when the output can't be fingerprinted.
        """
        if "::" in job.file_path:
            # Archive members: the on-disk "source" is the whole archive and the
            # extracted member is transient, so a reuse can't be proven cheaply.
            return None
        pre = self._delete_plans.get(job.id)
        if not pre or not pre.get("fingerprints"):
            return None
        source_fp = self._norm_fps(pre.get("fingerprints"))
        if not source_fp or self._source_fingerprint(job.file_path) != source_fp:
            return None
        output_fp = self._output_fingerprint(job.output_path)
        if not output_fp:
            return None
        return {
            "mode": job.mode.value,
            "compression": job.compression,
            "split": bool(job.split),
            "source": source_fp,
            "output": output_fp,
        }

    def _produced_meta_matches(self, job: ConversionJob, meta: object) -> bool:
        """Whether the on-disk output is the *exact* result of THIS request.

        Requires the recorded producing settings (mode + compression + split) to
        equal the current request's, the complete source set to be byte-for-byte
        unchanged since it was verified (so a different track can't be silently
        reused), and the output to be unchanged (so a replaced/corrupted file
        with a fresh mtime can't pass). Any mismatch → re-convert.
        """
        if not isinstance(meta, dict):
            return False
        if (
            meta.get("mode") != job.mode.value
            or meta.get("compression") != job.compression
            or bool(meta.get("split")) != bool(job.split)
        ):
            return False
        if self._output_fingerprint(job.output_path) != meta.get("output"):
            return False
        if self._source_fingerprint(job.file_path) != meta.get("source"):
            return False
        return True

    async def _output_already_verified(self, job: ConversionJob) -> bool:
        """Whether a prior run already produced and verified this exact output.

        Backs the job-start "recognize prior success" fast path (issue #184,
        site 1): a re-queued job whose verified artifact is already on disk
        completes as a no-op instead of re-spawning the converter. Only a
        verification record carrying a full ``produced_meta`` snapshot (written
        by a prior delete-on-verify conversion) can qualify, and only when that
        snapshot proves the on-disk output is the exact result of the current
        request — see :meth:`_produced_meta_matches`. Directory-input and
        delete-on-verify *requests* always take the normal path (the former has
        split-set outputs, the latter must run its guarded source deletion).
        """
        if (
            job.delete_on_verify
            or job.input_kind == InputKind.DIRECTORY
            or not job.output_path
        ):
            return False
        try:
            record = await verification_store.get_record(job.output_path)
        except Exception as exc:
            # Never let a verification-store hiccup skip a conversion: on any
            # lookup failure fall through to the normal path (re-convert).
            logger.debug(
                "Prior-success check skipped for %s: %s", job.output_path, exc,
            )
            return False
        if not record:
            return False
        meta = record.get("produced_meta")
        if not meta:
            return False
        return await run_in_threadpool(self._produced_meta_matches, job, meta)

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
            # Both early exits below return before the try/finally that
            # notifies for every other outcome, so they announce themselves.
            # Listeners may hear about one job twice (this path runs after
            # `cancel_job` already fired) and must be idempotent.
            self._notify_terminal(job)
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
            self._notify_terminal(job)
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

            # Terminal, and it returns below without reaching the try/finally
            # that announces every other outcome -- so a listener recording how
            # this job ended would never hear about the one case where the job
            # failed *before* touching the output.
            self._notify_terminal(job)
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
                # Same as the lock failure above: an early return, so it has to
                # announce itself.
                self._notify_terminal(job)
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
        # Restart the progress clock at the moment work actually begins. It was
        # last set when the job was created, so a job that waited in the queue
        # longer than debug_progress_timeout -- routine on a deep queue -- would
        # otherwise be reported stalled the instant it started running.
        self._last_progress_at[job_id] = time.monotonic()
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
            # Fast path: a re-queued job whose verified artifact already exists
            # completes as a no-op rather than re-spawning the converter (issue
            # #184, site 1). Checked before extraction / _clear_existing_output
            # so the existing verified output is neither re-extracted-from nor
            # deleted. Honors a cancel requested before we got here.
            if not cancel_event.is_set() and await self._output_already_verified(job):
                # Re-check after the awaited store/stat lookups: a cancel that
                # landed while they were in flight must win over the no-op.
                if cancel_event.is_set():
                    raise ConversionCancelled("Conversion cancelled")
                self._cancelled.discard(job_id)
                job.progress = 100
                job.output_size = await run_in_threadpool(
                    self._compute_output_size, job,
                )
                job.status = JobStatus.COMPLETED
                job.completed_at = datetime.now(timezone.utc)
                job.message = "Output already verified; skipped re-conversion."
                await self._notify_subscribers(
                    job_id,
                    {
                        "type": "complete",
                        "job_id": job_id,
                        "output_path": job.output_path,
                        "output_size": job.output_size,
                        "verified": True,
                        "source_deleted": False,
                    },
                )
                return

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

            # Same point-in-time problem as the directory check below, for a
            # multi-file *file* source: a queued split Wii U set's
            # `game_partN.wud` can be swapped for a symlink out of the volumes
            # after `plan_job` cleared it, and the converter enumerates and
            # opens the parts itself. Re-check under the job's locks, and
            # before clearing any existing output so a rejection on an
            # overwrite job leaves the user's prior file intact.
            if not await run_in_threadpool(
                source_companions_are_safe, input_path, job.mode.value,
            ):
                job.status = JobStatus.FAILED
                job.error_message = (
                    "A companion file this source consumes is a symlink or "
                    "resolves outside configured volumes"
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
                now = time.monotonic()
                # The runner tells us which updates were real forward movement
                # (a percentage that advanced, or the output file growing) and
                # which were keep-alives. Don't re-derive it: counting every
                # arrival makes a heartbeating-but-hung job look alive, and
                # counting only percentage changes makes a healthy job look
                # stalled once the size fallback pins at 95% or when its mode
                # has no size ratio at all (issue #263).
                if update.get("activity"):
                    self._last_progress_at[job_id] = now
                job.progress = update["progress"]
                job.message = update["message"]
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
                except DiscIdStorageAbandoned:
                    # Deliberately not swallowed with the rest. Everything below
                    # touches job.output_path on the same storage -- the output
                    # size probe is an unbounded run_in_threadpool, and
                    # delete-on-verify spawns a verifier -- so let the outer
                    # handler fail this job now rather than hang it (#263/#268).
                    raise
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
                # Two ways to ask for the same verify: delete-on-verify needs it
                # as a precondition, verify_after wants the check on its own and
                # keeps the source. One block runs it either way, so a verified
                # output means the same thing however it was requested.
                if job.delete_on_verify or job.verify_after:
                    # Both halves of the contract, re-asked here because this is
                    # where the source actually gets unlinked. The plan sites
                    # (`routes/convert.py`, `romm_auto.normalize_rule`) check the
                    # same pair, but a job can reach this point without passing
                    # either -- restored queue state, a hand-edited rule blob, a
                    # future caller. `supports_delete_on_verify` says the mode
                    # can offer it at all; `delete_on_verify_is_safe` says THIS
                    # job can, given its compression, and that is the one that
                    # stops a Wii U `noverify` conversion from deleting a 25 GB
                    # source on a structural check.
                    if job.delete_on_verify and not (
                        registry.spec(job.mode.value).supports_delete_on_verify
                        and registry.for_mode(job.mode.value).delete_on_verify_is_safe(
                            job.mode.value, job.compression,
                        )
                    ):
                        raise RuntimeError(
                            "Delete-on-verify is not safe for this conversion: "
                            "the mode or its compression settings cannot prove "
                            "the output before the source is removed"
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

                    tool = registry.for_mode(job.mode.value)
                    # Bound the whole verify, not just whatever subprocess it
                    # happens to spawn: a tool whose verify is pure Python (the
                    # Wii U container walk, the PS3 PARAM.SFO readback) has no
                    # subprocess timeout to hide behind, and with
                    # MAX_CONCURRENT_JOBS=1 a verify that never returns freezes
                    # every job queued behind it (issue #266). The tool resolves
                    # the number so its own per-tool override applies, and takes
                    # the cancel event because sizing the output is a stat of
                    # its own: on a mount that has stopped answering, a Cancel
                    # pressed here must not wait out the probe bound before the
                    # verify that would report it even starts.
                    # Ask the tool what to verify rather than assuming the
                    # planned path holds it: a split makeps3iso build writes
                    # `<iso>.0`/`.1`/… and no bare `.iso`, so verifying
                    # `output_path` read a file that was never created and
                    # failed a conversion that had in fact succeeded.
                    verify_path = await run_in_threadpool(
                        tool.verify_target, job.output_path, job.mode.value,
                    )
                    if not verify_path:
                        raise RuntimeError(
                            "Verification could not find the converted output"
                        )
                    verify_bound = await tool.verify_timeout(
                        verify_path, cancel_event=cancel_event,
                    )
                    self._verifying[job_id] = time.monotonic()
                    try:
                        verify_result = await asyncio.wait_for(
                            tool.verify(verify_path, cancel_event=cancel_event),
                            timeout=verify_bound or None,
                        )
                    except asyncio.TimeoutError:
                        raise RuntimeError(
                            f"Verification timed out after {verify_bound}s"
                        ) from None
                    finally:
                        self._verifying.pop(job_id, None)
                    if verify_result.get("cancelled"):
                        # The verifier stopped because Cancel was pressed, so it
                        # reached no verdict. Reporting that as a failed
                        # verification would be wrong twice over: it is not a bad
                        # file, and the source must not be deleted on it.
                        raise ConversionCancelled("Conversion cancelled")
                    if verify_result.get("abandoned"):
                        # The verifier outlived SIGKILL and is still holding the
                        # storage. Distinguished from an ordinary failure so the
                        # log and the job's error name the real problem -- the
                        # file was never judged, the mount stopped answering --
                        # rather than reading as "this output is bad".
                        logger.error(
                            "Job %s: verification could not be stopped and is "
                            "still holding %s; the volume is likely not "
                            "responding",
                            job_id, job.output_path,
                        )
                        raise RuntimeError(
                            f"Verification could not be stopped: "
                            f"{verify_result.get('message')}"
                        )
                    if not verify_result.get("valid"):
                        raise RuntimeError(
                            f"Verification failed: {verify_result.get('message')}"
                        )

                    verified = True
                    # Snapshot what produced this verified artifact (mode +
                    # output shaping + source/output fingerprints) so a later
                    # re-queue can recognize prior success and complete as a
                    # no-op without re-spawning the converter (#184, site 1).
                    # The source still exists here (delete runs below), so its
                    # complete set can be fingerprinted.
                    produced_meta = await run_in_threadpool(
                        self._build_produced_meta, job,
                    )
                    await verification_store.mark_verified(
                        job.output_path,
                        source_path=job.file_path,
                        produced_meta=produced_meta,
                    )

                    if not job.delete_on_verify:
                        # verify_after only: the source is kept by design, so
                        # the job is finished the moment the output checks out.
                        job.message = "Verification complete."
                        await self._notify_subscribers(
                            job_id,
                            {
                                "type": "progress",
                                "job_id": job_id,
                                "progress": job.progress,
                                "message": job.message,
                            },
                        )
                    elif cancel_event.is_set():
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
            # Before any cleanup: this is the one place every runner outcome
            # (completed, failed, cancelled mid-run) passes through, and a
            # listener that must record how the job ended has to hear about it
            # while the job object still says so.
            self._notify_terminal(job)
            # Only release lock if we acquired it
            if lock_acquired:
                lock_manager.release_lock(job.output_path)
            # Release the directory subtree lock held by a folder->iso job.
            if dir_lock_acquired:
                lock_manager.release_dir_lock(job.file_path)

            concurrency_manager.release(job_id)

            self._delete_plans.pop(job_id, None)
            self._output_keys.pop(job_id, None)
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
