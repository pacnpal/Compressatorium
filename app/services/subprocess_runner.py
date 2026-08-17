"""Shared subprocess orchestration for conversion tools.

The implementation lives in this top-level ``services`` module (rather than
inside the ``services.tools`` package) so the service singletons can import it
during their own module initialization without triggering ``services.tools``'s
eager registry build, which imports the tool wrappers, which import back into
the still-initializing service modules.  ``services.tools.runner`` re-exports
these names so the design's documented path keeps working.

``SubprocessRunner.run`` collapses the line-buffered subprocess loop that was
duplicated, almost verbatim, in ``chdman``'s and ``dolphin_tool``'s
``convert()`` (spawn, ``nice`` wrap, PID tracking, ``\\r``/``\\n`` buffering,
stall timeout, cancel watcher, non-zero-exit error tail, final 100% emit).
dolphin's only addition over chdman is a periodic heartbeat, which is an opt-in
flag here.

``SubprocessRunner.run_capture`` is the one-shot counterpart for tools that
need a buffered ``(returncode, stdout, stderr)`` rather than streamed lines
(info / header / embedded-hash extraction). It shares the same PID tracking
and adds cancel-event + timeout handling (terminate -> kill), so an expensive
capture such as ``dolphin-tool verify --algorithm sha1`` aborts promptly when
a background scan/match job is cancelled instead of running to completion.

``ConversionCancelled`` is defined here (rather than in ``services.chdman``) so
the runner can raise it without importing back into the service that uses the
runner.  ``services.chdman`` re-exports it from this module, so its identity and
every existing import path (``from services.chdman import ConversionCancelled``)
are preserved.
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
import shutil
import threading
import time
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence

from config import settings
from logging_setup import get_logger
from services.timeout_policy import compute_progress_stall_timeout


class ConversionCancelled(Exception):
    """Raised when a conversion is cancelled before completion."""


# Hard bound on a filesystem probe taken on the spawn path, where there is no
# stall loop yet to rescue a wait that never returns.
_STAT_TIMEOUT = 10.0


def _probe_in_daemon_thread(func: Callable[[], object]) -> asyncio.Future:
    """Run a blocking filesystem call on a throwaway daemon thread.

    Deliberately not a ``ThreadPoolExecutor``. Its workers are joined during
    interpreter shutdown, so a probe wedged on a dead mount would stop the
    container from restarting cleanly -- and a pooled worker that never returns
    starves every probe queued behind it. A daemon thread does neither: it holds
    up nothing at exit and occupies no shared capacity. A call that never
    returns simply costs one written-off thread that dies with the process,
    which is the unavoidable price of a syscall Python cannot cancel.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def _settle(setter: Callable, value: object) -> None:
        if not future.done():
            setter(value)

    def _worker() -> None:
        try:
            result = func()
        except BaseException as exc:  # noqa: BLE001 - relayed to the awaiter
            setter, value = future.set_exception, exc
        else:
            setter, value = future.set_result, result
        with contextlib.suppress(RuntimeError):
            # RuntimeError: the loop closed while this thread was blocked.
            loop.call_soon_threadsafe(_settle, setter, value)

    threading.Thread(target=_worker, daemon=True, name="fs-probe").start()
    return future


async def _bounded_probe(func: Callable, *args: object, **kwargs: object) -> object:
    """Run a one-shot blocking filesystem call with a hard bound.

    Raises :class:`asyncio.TimeoutError` if it does not finish within
    :data:`_STAT_TIMEOUT`; every other exception propagates untouched. The
    timeout is signalled rather than folded into a ``None`` return because some
    of these calls (``os.makedirs``) return ``None`` on success, and each caller
    wants a different fallback anyway.

    A ``stat`` on an unresponsive mount blocks in uninterruptible I/O and cannot
    be cancelled -- Python can abandon the future but never the OS thread. Each
    call therefore gets its **own** disposable executor, shut down without
    joining: a wedged probe writes off exactly one thread and cannot occupy a
    worker that anything else depends on. Sharing one would let a hung
    input-side stat starve the continuous output-growth probe, leaving a job
    whose output is writing perfectly well with no status and no watchdog
    activity until the stall timeout killed it.
    """
    return await asyncio.wait_for(
        _probe_in_daemon_thread(functools.partial(func, *args, **kwargs)),
        timeout=_STAT_TIMEOUT,
    )


# --- Bounded cleanup of partial output ------------------------------------
#
# Every tool that writes its output in place sweeps the partial away when a run
# ends abnormally. That sweep runs *after* the runner has already given up, on
# the same storage the run just failed on -- so on a dead mount the unlink or
# rmtree blocks in uninterruptible I/O and the job never finalises. With
# MAX_CONCURRENT_JOBS defaulting to 1 and jobs running inline, that freezes the
# queue exactly the way the bounded reap (#263/#265) was written to prevent.
# These helpers are the one bounded seam every tool's cleanup goes through.

_cleanup_logger = get_logger("cleanup")

# Hard bound on that sweep. Larger than _STAT_TIMEOUT because a sweep may unlink
# several files in a row (a split makeps3iso build) or walk a work dir, and each
# step is a separate round trip to storage that is, by assumption, already
# unhappy. Still a bound: a leftover partial file is a far smaller problem than
# a frozen queue, so cleanup gives up and the job finishes as failed.
_CLEANUP_TIMEOUT = 30.0


def _unlink_all(
    paths: tuple[str, ...],
    discover: Callable[[], Sequence[str]] | None,
    abandoned: threading.Event,
) -> None:
    """Unlink each path, reporting what could not be removed.

    Runs on the cleanup thread, which is also where ``discover`` is called:
    enumerating a set (makeps3iso's numbered split parts) *stats the same
    storage the sweep is about to unlink from*, so doing it on the event loop
    would leave the exact unbounded block this seam exists to remove.

    An absent path is the goal, not a failure, so ``FileNotFoundError`` is
    skipped; every other ``OSError`` (a directory shadowing the output, a
    permission problem) is collected and raised at the end so one bad path is
    logged without stopping the sweep of the rest.

    ``abandoned`` is checked before each unlink and stops the sweep once the
    waiter has given up (timed out, or was cancelled). A sweep that outlives its
    bound must not keep deleting: the job has finalised and released those
    paths, the mount can recover minutes later, and by then a retry may own the
    names -- this thread would be deleting the *new* job's good output. The one
    unlink already issued when the bound expired cannot be recalled (an
    in-flight syscall is not cancellable), so that single path stays at risk;
    every path after it does not. On a responsive filesystem the whole sweep
    finishes long before either giving-up path is reached, so this costs the
    normal case nothing.
    """
    targets = list(paths)
    if discover is not None:
        targets.extend(discover())
    failures: list[str] = []
    for path in targets:
        if abandoned.is_set():
            _cleanup_logger.warning(
                "Abandoned cleanup stopping before %s; it is no longer this "
                "job's to delete.", path,
            )
            return
        try:
            os.remove(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            failures.append(f"{path} ({exc})")
    if failures:
        raise OSError("; ".join(failures))


def _rmtree_quietly(path: str, _abandoned: threading.Event) -> None:
    """``rmtree`` ignoring errors. Takes the flag for the ``work`` signature.

    Nothing to check it against: ``rmtree`` is one call, so there is no next
    path to stop before.
    """
    shutil.rmtree(path, ignore_errors=True)


async def _bounded_cleanup(
    work: Callable[[threading.Event], None],
    what: str,
    timeout: float,
) -> bool:
    """Run a blocking cleanup call with a hard bound, raising only to cancel.

    Returns ``True`` when the removal finished, ``False`` when it did not --
    timed out, or failed. A cleanup problem is logged rather than raised: on an
    error path it must not replace the exception that actually explains the
    job's outcome, and in a ``finally`` after a *successful* conversion it must
    not fail a job whose output is already published. It must also never hold up
    finalising the job.

    ``CancelledError`` is the one exception that does propagate, like from any
    other ``await``. Swallowing it would defeat cancellation outright for the
    callers that have no pending exception to re-raise -- nsz, JWUDTool, romz's
    extract and chains all clean up in a ``finally`` that runs on the success
    path too, where consuming the cancel lets the job go on to be marked
    complete.

    The blocking call is *dispatched* before the wait, so a cancellation
    arriving afterwards cannot take back work the sweep already did -- the
    useful half of what the per-tool blocks bought by unlinking synchronously on
    the event loop (awaiting during a cancellation could be re-cancelled and
    skip the cleanup), without the unbounded block. What giving up *does* stop
    is the rest of the sweep: once this returns, the job finalises and stops
    owning those paths, so ``abandoned`` tells the thread not to unlink anything
    it has not already started. See :func:`_unlink_all`.
    """
    abandoned = threading.Event()

    def _run_work() -> None:
        try:
            work(abandoned)
        finally:
            if abandoned.is_set():
                _cleanup_logger.warning(
                    "Cleanup of %s finished after it was abandoned", what,
                )

    try:
        future = _probe_in_daemon_thread(_run_work)
    except RuntimeError as exc:
        # Out of threads -- plausible precisely here, since a run of dead-mount
        # sweeps deliberately writes threads off. Nothing was dispatched, so
        # report it like any other cleanup failure rather than letting it
        # escape and stand in for the conversion's own outcome.
        _cleanup_logger.warning(
            "Could not start a cleanup thread for %s: %s", what, exc,
        )
        return False
    try:
        await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        abandoned.set()
        _cleanup_logger.warning(
            "Giving up after %.0fs on removing %s; the job will finish and the "
            "file is left on disk. The storage it lives on is most likely "
            "unresponsive.",
            timeout, what,
        )
        return False
    except asyncio.CancelledError:
        # Stop the sweep before re-raising: the job is going away, so anything
        # it has not already started to unlink is no longer its to delete.
        abandoned.set()
        _cleanup_logger.warning("Interrupted while removing %s", what)
        raise
    except Exception as exc:  # noqa: BLE001 - cleanup never raises at its caller
        _cleanup_logger.warning("Could not remove %s: %s", what, exc)
        return False
    return True


async def remove_partial_output(
    *paths: str,
    discover: Callable[[], Sequence[str]] | None = None,
    label: str = "partial output",
    timeout: float | None = None,
) -> bool:
    """Bounded unlink of the file(s) a failed run may have left behind.

    Pass every path the run could have written. When the set is only knowable
    by probing the disk (a split build leaves numbered parts alongside the
    base), pass ``discover`` instead of enumerating first: it is called *inside*
    the bounded worker, so the probing is bounded along with the unlinking.
    ``label`` names the sweep in the log, and should carry the path when
    ``discover`` means there is nothing static to print.

    Returns ``True`` if the sweep finished, ``False`` if it did not; only
    ``CancelledError`` propagates. ``timeout`` defaults to
    :data:`_CLEANUP_TIMEOUT`, read at call time so the bound stays one knob
    rather than a value frozen into each signature.
    """
    if discover is None and not paths:
        return True
    what = f"{label} {', '.join(paths)}" if paths else label
    return await _bounded_cleanup(
        functools.partial(_unlink_all, paths, discover),
        what,
        _CLEANUP_TIMEOUT if timeout is None else timeout,
    )


async def remove_partial_tree(
    path: str,
    *,
    label: str = "work directory",
    timeout: float | None = None,
) -> bool:
    """Bounded ``rmtree`` of a private work/scratch dir, errors ignored.

    The file-level counterpart of :func:`remove_partial_output` for the tools
    that name their own output inside a temp dir (nsz, JWUDTool, romz extract,
    chains). ``ignore_errors`` matches what these call sites always wanted: the
    dir is private to the run and a stray file in it must not fail the job.
    """
    what = f"{label} {path}"
    return await _bounded_cleanup(
        functools.partial(_rmtree_quietly, path),
        what,
        _CLEANUP_TIMEOUT if timeout is None else timeout,
    )


# Bounds on reaping a subprocess: how long to let it exit on its own once its
# output stream closes, then the SIGTERM and SIGKILL grace periods.
# How long a stall check will wait for the first growth measurement to land
# before judging without one. Spans a few read-loop ticks: long enough that
# probe lag cannot fake a stall, short enough that a probe which never returns
# cannot disable the watchdog.
_FIRST_SAMPLE_GRACE = 6.0

# Minimum spacing between output-size samples. The read loop turns over once per
# stdout chunk, so without this a chatty converter would set the probe rate --
# hundreds of threads and metadata round-trips per second against the very mount
# whose responsiveness is in question.
_PROBE_INTERVAL = 2.0

# Floor for the stall timeout. Growth is sampled every _PROBE_INTERVAL and read
# a tick later, so the detector cannot resolve a window shorter than a few
# sample periods: below that, "no growth observed" only means "not looked at
# recently", and a converter writing steadily gets killed for it. Clamped rather
# than trusted, so no configuration can ask the watchdog for a precision it does
# not have.
_MIN_STALL_TIMEOUT = 3 * _PROBE_INTERVAL

_EXIT_GRACE = 60.0
_TERM_GRACE = 5.0
_KILL_GRACE = 10.0


# --- Shared process-priority / timeout policy -----------------------------
#
# These knobs (nice level, I/O priority, info/verify timeouts) govern *every*
# conversion tool's subprocess, not just chdman. They live here -- read once in
# one place -- rather than being re-read with a chdman-flavoured name in each
# service. The shared default comes from the tool-neutral ``tool_*`` settings;
# an optional per-tool override (``<owner>_*``, e.g. ``dolphin_tool_nice``)
# takes precedence when set. ``owner`` matches the ``SubprocessRunner`` owner
# string each service constructs (``chdman``, ``dolphin_tool``, ``nsz``,
# ``z3ds``, ``maxcso``).


def _resolve_policy(key: str, owner: str | None):
    """Return the per-owner override for ``key`` if set, else the shared default.

    ``key`` is the bare setting suffix (``nice``, ``ioprio_class``,
    ``ioprio_level``, ``info_timeout``, ``verify_timeout``); the shared value is
    ``settings.tool_<key>`` and the optional override ``settings.<owner>_<key>``.
    """
    if owner is not None:
        override = getattr(settings, f"{owner}_{key}", None)
        if override is not None:
            return override
    return getattr(settings, f"tool_{key}")


def nice_value(owner: str | None = None) -> int | None:
    """Effective ``nice`` increment for ``owner`` (None disables renicing)."""
    return _resolve_policy("nice", owner)


def ioprio_prefix(owner: str | None = None) -> list[str]:
    """Return the ``ionice`` command prefix per the shared priority policy.

    Empty when I/O priority is unset for ``owner`` or ``ionice`` is unavailable.
    """
    ioprio_class = _resolve_policy("ioprio_class", owner)
    ioprio_level = _resolve_policy("ioprio_level", owner)
    if ioprio_class is None or ioprio_level is None:
        return []
    ionice = shutil.which("ionice")
    if not ionice:
        return []
    return [ionice, "-c", str(ioprio_class), "-n", str(ioprio_level)]


def nice_prefix(owner: str | None = None) -> list[str]:
    """Return the ``nice`` command prefix for ``owner``.

    Used by services that apply nice via a command wrapper instead of a
    ``preexec_fn`` (e.g. nsz, where forking a Python callable in a
    multithreaded process can deadlock the child before exec).
    """
    value = nice_value(owner)
    if value is None:
        return []
    nice = shutil.which("nice")
    if not nice:
        return []
    return [nice, "-n", str(value)]


def apply_nice(owner: str | None = None) -> None:
    """Renice the current process per the shared policy (for ``preexec_fn``)."""
    value = nice_value(owner)
    if value is None:
        return
    try:
        os.nice(value)
    except OSError:
        pass


def info_timeout(owner: str | None = None) -> int:
    """Effective ``info`` subprocess timeout in seconds (0 disables)."""
    return max(0, int(_resolve_policy("info_timeout", owner) or 0))


def verify_timeout(owner: str | None = None) -> int:
    """Effective ``verify`` subprocess timeout in seconds (0 disables)."""
    return max(0, int(_resolve_policy("verify_timeout", owner) or 0))


def _split_stream_lines(buffer: str) -> tuple[list[str], str]:
    """Segment accumulated subprocess output into complete lines + a remainder.

    Segmentation is a pure function of the byte stream, independent of where
    ``read()`` chunk boundaries fall: CRLF and bare CR (progress redraws) are
    normalized to LF, then the buffer is split on LF. The text after the final
    LF is returned as the remainder to prepend to the next chunk. Lines are
    stripped and blanks dropped, matching the prior per-separator loop's output
    minus its chunk-boundary sensitivity (issue #183, site 5).
    """
    normalized = buffer.replace("\r\n", "\n").replace("\r", "\n")
    segments = normalized.split("\n")
    remainder = segments[-1]
    return [s.strip() for s in segments[:-1] if s.strip()], remainder


# Expected output size as a multiple of the input, per conversion mode. The
# single source of truth for the size-growth progress estimate: one table here
# rather than a `_COMPRESS_RATIO`/`_DECOMPRESS_RATIO` pair and an
# `expected_size` closure re-derived inside every tool service. A mode absent
# from the table still reports status -- it falls back to the bytes-written
# message, which needs no ratio -- so a new tool is never *required* to add a
# row, and adding one only sharpens its percentage estimate. The ratio only
# smooths the bar (see `output_size_progress`), so an approximate value is fine.
SIZE_RATIOS: dict[str, float] = {
    # dolphin-tool: RVZ/WIA/GCZ compression, and decompression back to ISO.
    "dolphin_rvz": 0.5, "dolphin_wia": 0.5, "dolphin_gcz": 0.7,
    "dolphin_iso": 2.0,
    # maxcso: PSP/PS2 CSO family.
    "cso_compress": 0.5, "cso2_compress": 0.5, "zso_compress": 0.5,
    "dax_compress": 0.5, "cso_decompress": 2.0,
    # nsz: Switch NSP/XCI <-> NSZ/XCZ.
    "nsz_compress": 0.6, "nsz_decompress": 1.7,
    # z3ds: 3DS CIA/3DS <-> Z3DS.
    "z3ds_compress": 0.5, "z3ds_decompress": 2.0,
}


def size_ratio_for(mode: str | None) -> float | None:
    """Expected output/input size ratio for ``mode`` (None when unknown)."""
    return SIZE_RATIOS.get(mode) if mode else None


def output_size_message(size: int, delta_bytes: int, delta_seconds: float) -> str:
    """Status line for a size-growth tick: bytes written and the current rate.

    The shared human-readable half of the size-growth progress signal (the
    numeric half is :func:`output_size_progress`). A tool that is merely *slow*
    -- a conversion crawling against a saturated array or a stalled share --
    must read as slow rather than as indistinguishable from a hang, so the
    message carries an absolute (MB written) and a derivative (MB/min): the
    first keeps climbing, the second collapses toward zero.

    The rate is measured **between consecutive samples**, not as total bytes
    over total elapsed time. A cumulative average is dominated by whatever the
    conversion did first: a job that writes gigabytes quickly and then crawls
    would keep advertising hundreds of MB/min long after it slowed to nothing,
    destroying the one distinction this line exists to make. Rate is per minute
    because the case worth diagnosing is the pathological one, where a
    per-second figure rounds to ``0.0`` and says nothing.
    """
    written_mb = size / (1024 * 1024)
    per_min = (delta_bytes / (1024 * 1024)) / (max(delta_seconds, 1e-9) / 60.0)
    return f"Working... ({written_mb:,.0f} MB written, {per_min:,.1f} MB/min)"


def output_size_progress(current: int, expected_size: int) -> int:
    """Estimate a 5-95% progress value from output-file growth.

    The shared progress estimate for tools whose subprocess draws a TTY
    progress bar that falls silent on a pipe (``maxcso``, ``nsz``, ``z3ds``):
    with no parseable percent in stdout, the growing output file *is* the
    progress signal, scored against an expected size (the input size times a
    per-direction ratio — roughly 0.5 for compress, 2.0 for decompress). The
    ratio only smooths the bar, so an inexact guess affects perceived
    smoothness, not correctness. Clamped to ``[5, 95]``; :meth:`SubprocessRunner.run`
    emits the terminal 100% on a clean exit. ``expected_size`` is floored at 1
    to avoid a divide-by-zero on a zero-byte input.
    """
    expected = expected_size if expected_size > 0 else 1
    return min(95, max(1, int(current / expected * 90) + 5))


class SubprocessRunner:
    """Spawns a conversion subprocess and streams progress updates.

    One instance per tool owns that tool's in-flight PID set, so both
    ``run()`` and the tool's separate ``verify_stream`` loop can register
    their subprocesses through the same store (``track_pid``/``untrack_pid``).
    """

    def __init__(self, owner: str) -> None:
        self._owner = owner
        self._active_pids: set[int] = set()
        self._pid_lock = threading.Lock()
        self._logger = logging.getLogger(f"chd.{owner}")

    @property
    def owner(self) -> str:
        """Tool identifier used to resolve per-tool priority/timeout overrides."""
        return self._owner

    def track_pid(self, pid: int) -> None:
        with self._pid_lock:
            self._active_pids.add(pid)

    def untrack_pid(self, pid: int) -> None:
        with self._pid_lock:
            self._active_pids.discard(pid)

    def active_pids(self) -> list[int]:
        with self._pid_lock:
            return list(self._active_pids)

    async def reap(self, process, *, exit_timeout: float = _EXIT_GRACE) -> bool:
        """Wait for ``process`` to exit, bounded at every step. True if reaped.

        The shared teardown for every subprocess this module spawns. A child
        blocked in uninterruptible I/O (``D`` state -- a stalled mount, a drive
        that stopped answering) does not die on ``SIGTERM`` *or* ``SIGKILL``:
        the signal is only delivered once the kernel I/O returns, which may be
        never. A bare ``await process.wait()`` on such a child therefore blocks
        its job forever, and because ``MAX_CONCURRENT_JOBS=1`` runs jobs inline
        in the dispatcher, every job queued behind it too (issue #263).

        So each step is bounded and the ladder terminates: wait ``exit_timeout``
        for a voluntary exit (skipped when 0 -- teardown paths have already
        stopped reading), then ``SIGTERM`` + grace, then ``SIGKILL`` + grace.
        Returning ``False`` means the child outlived ``SIGKILL`` and has been
        abandoned -- it keeps its pipes and one OS process until the kernel
        unblocks it, which is the unavoidable cost of not hanging the queue.
        Callers turn that into a failed job.
        """
        if process.returncode is not None:
            return True

        async def _wait(timeout: float) -> bool:
            if timeout <= 0:
                return process.returncode is not None
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                return False
            return True

        if await _wait(exit_timeout):
            return True

        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        if await _wait(_TERM_GRACE):
            return True

        with contextlib.suppress(ProcessLookupError):
            process.kill()
        if await _wait(_KILL_GRACE):
            return True

        self._logger.error(
            "%s pid=%s survived SIGKILL (likely blocked in uninterruptible I/O); "
            "abandoning it so the job fails instead of stalling the queue",
            self._owner, process.pid,
        )
        return False

    async def run_capture(
        self,
        cmd: list[str],
        *,
        timeout: float | None = None,
        cancel_event: asyncio.Event | None = None,
        stderr_to_stdout: bool = False,
    ) -> tuple[int | None, bytes, bytes]:
        """Run ``cmd`` to completion and capture ``(returncode, stdout, stderr)``.

        Shared one-shot counterpart to :meth:`run` for tools that need a
        buffered result rather than the streaming line loop (info / header /
        hash extraction). PID-tracked like :meth:`run`, so ``active_pids``
        and cancellation see these subprocesses too.

        The call races ``communicate()`` against an optional ``cancel_event``
        and ``timeout`` (seconds). If either fires before the process exits,
        the subprocess is terminated (TERM, then KILL after 5s) and the
        returncode is reported as ``None`` to signal the abort. ``stderr`` is
        folded into ``stdout`` when ``stderr_to_stdout`` is set (and the
        returned ``stderr`` is then empty).
        """
        # Honour the shared process-priority policy, same as the streaming
        # run(): renice via preexec and wrap with ionice. A captured command
        # (e.g. dolphin-tool verify reconstructing a full disc for DAT hashing)
        # is just as heavy as a conversion, so it must respect TOOL_NICE /
        # TOOL_IOPRIO_* instead of running at normal priority.
        cmd = ioprio_prefix(self._owner) + cmd

        def _preexec():
            apply_nice(self._owner)

        process = await asyncio.create_subprocess_exec(  # nosemgrep
            cmd[0], *cmd[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=(
                asyncio.subprocess.STDOUT
                if stderr_to_stdout
                else asyncio.subprocess.PIPE
            ),
            preexec_fn=_preexec if os.name == "posix" else None,
        )
        self.track_pid(process.pid)
        comm = asyncio.ensure_future(process.communicate())
        cancel_wait = (
            asyncio.ensure_future(cancel_event.wait())
            if cancel_event is not None
            else None
        )
        try:
            waiters = [comm] + ([cancel_wait] if cancel_wait is not None else [])
            done, _pending = await asyncio.wait(
                waiters,
                timeout=timeout or None,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if comm in done:
                stdout, stderr = comm.result()
                return process.returncode, stdout or b"", stderr or b""
            # Cancelled or timed out before the process exited; the finally
            # block below terminates it and drains ``comm``.
            return None, b"", b""
        finally:
            if cancel_wait is not None and not cancel_wait.done():
                cancel_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_wait
            # exit_timeout=0: nothing is reading the child's output any more, so
            # go straight to signalling instead of waiting out a voluntary exit.
            await self.reap(process, exit_timeout=0)
            if not comm.done():
                comm.cancel()
            # CancelledError is a BaseException, so it is NOT covered by
            # suppress(Exception); list it explicitly so a cancelled/timed-out
            # capture still returns (None, b"", b"") instead of propagating the
            # cancellation out of run_capture.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await comm
            self.untrack_pid(process.pid)

    async def run(
        self,
        cmd: list[str],
        *,
        input_path: str,
        output_path: str,
        parse_progress: Callable[[str], int | None],
        initial_progress: int = 0,
        cancel_event: asyncio.Event | None = None,
        heartbeat: bool = False,
        fail_label: str = "process",
        complete_message: str = "Conversion complete",
        cwd: str | None = None,
        output_growth_paths: Callable[[], list[str]] | None = None,
        mode: str | None = None,
        nice_via_wrapper: bool = False,
        env: Mapping[str, str] | None = None,
        require_output: bool = False,
    ) -> AsyncGenerator[dict, None]:
        """Spawn ``cmd``, stream stdout, and yield ``{"progress", "message"}``.

        ``parse_progress(line) -> int | None`` is the only per-tool knob in the
        common path; ``heartbeat`` enables dolphin's 2-second "Converting..."
        keep-alive.  Handles nice wrap, PID tracking, ``\\r``/``\\n`` line
        buffering, stall timeout via ``compute_progress_stall_timeout``, a
        cancel watcher (terminate -> kill), ``ConversionCancelled`` on request,
        a non-zero-exit ``RuntimeError`` carrying the output tail, and the final
        100% emit.

        ``output_growth_paths`` is an optional callable returning the set of
        files whose **summed** size is the growth signal for stall detection,
        replacing the single ``output_path`` probe. A tool whose output filename
        changes mid-run uses it so the probe keeps following the write — e.g.
        makeps3iso ``-s`` renames the base ``.iso`` to ``.iso.0`` and then writes
        ``.iso.1``/…, which the bare ``output_path`` probe would stop seeing.

        **Every run reports status, with no per-tool wiring.** ``parse_progress``
        is the preferred signal and always wins: as soon as it returns a real
        percent, that tool is reporting for itself. When it never does — the
        common case, since most of these CLIs draw a TTY bar that falls silent
        on a pipe — the runner falls back to the growing output file, emitting
        bytes-written and a MB/min rate (:func:`output_size_message`) so a slow
        job is visibly slow rather than indistinguishable from a hung one. Pass
        ``mode`` to additionally get a percentage from :data:`SIZE_RATIOS`; a
        mode absent from that table still gets the message, so a new tool needs
        no wiring to be observable and a ratio only sharpens the bar.
        Updates carry ``"activity": True`` when this tick represented real
        forward movement -- a parsed percentage that advanced, or the output
        file growing -- and omit it for keep-alives. It is the same signal the
        stall watchdog runs on, published so callers judging liveness do not
        have to re-derive it from a proxy: percentage alone stops moving at the
        fallback's 95% cap and never moves for a mode with no size ratio, and
        update arrivals alone count heartbeats as progress (issue #263).

        ``initial_progress`` seeds the floor with the caller's preamble (e.g. a
        service's "Starting..." yield at 1/5%) so an early non-parseable line
        cannot drop the bar below it.

        ``nice_via_wrapper`` skips the ``preexec_fn`` renice when the caller has
        already prefixed ``cmd`` with ``nice``/``ionice`` command wrappers
        (maxcso/nsz avoid ``preexec_fn``: forking a Python callable in this
        multithreaded process can deadlock the child before ``exec``).  ``env``
        is forwarded to the subprocess (nsz runs with a private keys-home env).

        ``require_output`` makes a clean exit (return code 0) that left no file
        at ``output_path`` a failure, raised as a ``RuntimeError`` carrying the
        same stdout tail as the non-zero-exit path — so a tool that exits 0
        without producing output still reports the reason it printed first,
        rather than a bare "no output" message. Used by nsz, whose ``output_path``
        is the temp file the runner already watches.
        """
        output_dir = os.path.dirname(output_path)
        if output_dir:
            # Bounded like every other filesystem call here: an unresponsive
            # output mount must fail this job, not hang it -- and hanging here,
            # before the child exists, would freeze the whole queue behind it
            # (issue #263).
            try:
                await _bounded_probe(os.makedirs, output_dir, exist_ok=True)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"{fail_label}: output directory {output_dir} stopped responding"
                ) from None

        def _preexec():
            apply_nice(self._owner)

        # nice/ionice: by default renice the forked child via ``preexec_fn``
        # (ionice, when used, is folded into ``cmd`` by the caller's
        # ``_build_command``). ``nice_via_wrapper`` callers have instead prefixed
        # ``cmd`` with ``nice``/``ionice`` command wrappers and must NOT also be
        # reniced via preexec — forking a Python callable in this multithreaded
        # process can deadlock the child before ``exec``.
        use_preexec = os.name == "posix" and not nice_via_wrapper

        # cmd is built from validated settings paths (no shell interpretation);
        # create_subprocess_exec passes the arg list directly (shell=False), so
        # there is no shell expansion / injection surface. Same call shape that
        # already lives unsuppressed in chdman/dolphin/z3ds; flagged here only
        # because the line is new in this extracted module.
        process = await asyncio.create_subprocess_exec(  # nosemgrep
            cmd[0], *cmd[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            preexec_fn=_preexec if use_preexec else None,
            cwd=cwd,
            env=env,
        )
        self.track_pid(process.pid)
        # Everything below runs under try/finally so that if the caller stops
        # iterating (generator aclose / task cancellation) or an unexpected
        # error fires, the subprocess, the cancel-watcher task and the PID entry
        # are always cleaned up rather than leaked.
        cancel_task = None
        # Bound before the try: the finally tears these down, so an early failure
        # must not hit an unbound name and mask the real error.
        size_probe: asyncio.Task | None = None
        # Set once the ladder has already run and given up, so teardown does not
        # repeat TERM/KILL on a child known to be unkillable -- that second pass
        # is another 15s holding the queue's only slot for no possible gain.
        reap_failed = False
        try:
            if self._logger.isEnabledFor(logging.DEBUG):
                self._logger.debug(
                    "Starting %s pid=%s cmd=%s",
                    self._owner, process.pid, " ".join(cmd),
                )

            # Sizing the adaptive stall timeout stats the input. On a dead
            # mount that must not hang the spawn path, so fall back to the
            # non-adaptive baseline rather than waiting -- a watchdog with a
            # rough bound beats no watchdog.
            try:
                stall_timeout = await _bounded_probe(
                    compute_progress_stall_timeout,
                    input_path=input_path,
                    base_timeout=getattr(settings, "progress_timeout", 0),
                    timeout_per_gib=getattr(settings, "progress_timeout_per_gib", 0),
                    timeout_cap=getattr(settings, "progress_timeout_cap", 0),
                )
            except asyncio.TimeoutError:
                stall_timeout = max(0, int(getattr(settings, "progress_timeout", 0) or 0))
            if 0 < stall_timeout < _MIN_STALL_TIMEOUT:
                self._logger.warning(
                    "Stall timeout of %ss is below the %ss sampling floor; using %ss. "
                    "A shorter window cannot tell a stalled converter from one that "
                    "simply has not been sampled yet.",
                    stall_timeout, _MIN_STALL_TIMEOUT, _MIN_STALL_TIMEOUT,
                )
                stall_timeout = _MIN_STALL_TIMEOUT
            # Seed the progress floor with the caller's preamble (e.g. the
            # service's "Starting..." yield at 1/5%) so an early non-parseable
            # stdout line — which emits last_progress_value — can't drop the bar
            # below it before the first size-growth tick.
            last_progress_value = initial_progress
            last_output_size: int | None = None
            last_activity_at = time.monotonic()
            # Start of the current rate window (last observed growth).
            last_growth_at = last_activity_at
            start = last_activity_at
            last_heartbeat_at = start

            cancelled_by_request = False
            if cancel_event:

                async def _cancel_watcher():
                    nonlocal cancelled_by_request
                    await cancel_event.wait()
                    if process.returncode is not None:
                        return
                    cancelled_by_request = True
                    if self._logger.isEnabledFor(logging.DEBUG):
                        self._logger.debug(
                            "Cancelling %s pid=%s", self._owner, process.pid,
                        )
                    try:
                        process.terminate()
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                    except ProcessLookupError:
                        # The child exited between the returncode check and
                        # terminate(); the cancel is already recorded, so swallow
                        # it. Otherwise the watcher task ends in an exception that
                        # the finally re-raises, masking the run's real result.
                        pass

                cancel_task = asyncio.create_task(_cancel_watcher())

            buffer = ""
            output_lines: list[str] = []
            stall_error: str | None = None
            last_message = ""
            abandoned_error: str | None = None
            # Native stdout parsing is the preferred signal; the size-growth
            # fallback below only speaks while this stays False.
            saw_native_progress = False
            ratio = size_ratio_for(mode)
            expected_size = 0
            if ratio:
                # Unbounded here would hang the job after the child is already
                # running, with no stall loop yet to end it. No sample just means
                # no percentage; the bytes/rate message needs no ratio.
                try:
                    input_size = await _bounded_probe(os.path.getsize, input_path)
                except (asyncio.TimeoutError, OSError):
                    input_size = None
                if input_size is not None:
                    expected_size = max(1, int(input_size * ratio))

            def _record_line(line: str) -> None:
                if not output_lines or output_lines[-1] != line:
                    output_lines.append(line)
                    if len(output_lines) > 30:
                        output_lines.pop(0)

            probed_size: int | None = None
            last_probe_at = 0.0
            # Whether any probe has ever finished. Distinguishes "no measurement
            # yet" from "measured, and there is no output file".
            probe_completed = False

            def _measure_output_sync() -> int | None:
                # Summed size of the growth-probe target(s). Default is the
                # single output_path; output_growth_paths widens it to a set
                # whose total grows monotonically even as filenames change
                # mid-run (makeps3iso split). Returns None when nothing exists.
                paths = (
                    output_growth_paths() if output_growth_paths
                    else ([output_path] if output_path else [])
                )
                total = 0
                found = False
                for probe in paths:
                    try:
                        total += os.path.getsize(probe)
                        found = True
                    except OSError:
                        continue
                return total if found else None

            def _measure_output() -> int | None:
                """Most recent output size, measured off the event loop.

                ``getsize`` on an unresponsive mount blocks in uninterruptible
                I/O, and inline that would freeze the entire event loop --
                including the stall watchdog and the ``reap()`` ladder that exist
                to rescue exactly this situation (issue #263). So the probe runs
                in a worker thread and is *never awaited*: each tick reads the
                last completed measurement and kicks off the next. Single-flight,
                so a wedged mount costs one blocked thread for the life of the
                job rather than one per tick. The cost is that the size is one
                tick (~2s) stale, which no consumer here cares about.
                """
                nonlocal size_probe, probed_size, probe_completed, last_probe_at
                if size_probe is not None and size_probe.done():
                    if not size_probe.cancelled():
                        probe_completed = True
                        with contextlib.suppress(Exception):
                            probed_size = size_probe.result()
                    size_probe = None
                now = time.monotonic()
                if size_probe is None and now - last_probe_at >= _PROBE_INTERVAL:
                    last_probe_at = now
                    size_probe = _probe_in_daemon_thread(_measure_output_sync)
                return probed_size

            def _update_output_activity(now: float):
                nonlocal last_output_size, last_activity_at
                size = _measure_output()
                if size is None:
                    return
                if last_output_size is None or size > last_output_size:
                    last_output_size = size
                    last_activity_at = now

            def _size_update(now: float) -> dict | None:
                # Status from output-file growth, the universal fallback for a
                # tool whose stdout tells us nothing: every conversion writes a
                # file, so a growing file is a progress signal even when the CLI
                # prints no parseable percent (its TTY bar goes silent on a pipe)
                # -- which is most of them. This is what makes a merely *slow*
                # job legible instead of indistinguishable from a hung one
                # (issue #263).
                #
                # Native parsing wins whenever it works: once a real percent has
                # been parsed, that tool is reporting for itself and the fallback
                # stands down rather than fighting it for the message line. A
                # percentage is emitted only for a mode with a known size ratio;
                # otherwise the bar holds at its floor and the message alone
                # carries the news, which needs no ratio and so costs a new tool
                # no wiring at all.
                nonlocal last_output_size, last_activity_at, last_progress_value
                nonlocal last_growth_at
                size = _measure_output()
                if size is None:
                    return None
                if last_output_size is not None and size <= last_output_size:
                    return None
                delta_bytes = size - (last_output_size or 0)
                delta_seconds = now - last_growth_at
                last_output_size = size
                last_growth_at = now
                last_activity_at = now
                if saw_native_progress:
                    # The tool speaks for itself, so don't replace its status
                    # line -- but a file that is still growing is still
                    # liveness, and a native tool can sit on one integer
                    # percentage for many minutes on a large image. Republish
                    # the current status carrying the flag so the job manager's
                    # clock sees it (issue #263).
                    return {
                        "progress": last_progress_value,
                        "message": last_message,
                        "activity": True,
                    }
                progress = last_progress_value
                if expected_size:
                    # Clamped to the floor: an estimate must never walk the bar
                    # backward from a higher seeded value.
                    progress = max(progress, output_size_progress(size, expected_size))
                    last_progress_value = progress
                return {
                    "progress": progress,
                    "message": output_size_message(size, delta_bytes, delta_seconds),
                    "activity": True,
                }

            async def _check_stall(now: float) -> bool:
                nonlocal stall_error
                if stall_timeout <= 0:
                    return False
                _update_output_activity(now)
                if not probe_completed and now - start < _FIRST_SAMPLE_GRACE:
                    # No growth measurement has landed yet: the probe is off the
                    # event loop and read one tick later, so the very first
                    # checks have nothing to judge by. Don't call a stall on the
                    # absence of a measurement -- with a stall timeout shorter
                    # than the ~2s probe cadence that would kill a converter
                    # whose output is growing steadily.
                    #
                    # Bounded by the grace, though: a probe against a dead mount
                    # never completes, and waiting on it forever would disable
                    # the watchdog entirely and hang the queue on exactly the
                    # storage failure this exists to catch. Past the grace the
                    # check proceeds without a sample, so an unresponsive output
                    # still stalls out on schedule.
                    return False
                if now - last_activity_at < stall_timeout:
                    return False
                stall_error = (
                    "Conversion stalled: no progress increase or output growth "
                    f"for {stall_timeout}s (progress={last_progress_value}%,"
                    f" output_size={last_output_size})"
                )
                if process.returncode is None:
                    try:
                        process.terminate()
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                    except ProcessLookupError:
                        pass
                return True

            while True:
                try:
                    chunk = await asyncio.wait_for(
                        process.stdout.read(100), timeout=2,
                    )
                except asyncio.TimeoutError:
                    if cancel_event and cancel_event.is_set():
                        # Stop reading once cancellation is requested. A child
                        # that is still running is terminated by the watcher
                        # (-> cancelled_by_request -> ConversionCancelled); a
                        # child that has already exited 0 finished in that instant
                        # and is reported complete. A cancel that races a clean
                        # exit delivers the result rather than discarding it (a
                        # deliberate product choice for this inherently ambiguous
                        # race).
                        break
                    now = time.monotonic()
                    update = _size_update(now)
                    if update is not None:
                        yield update
                        # A size update carries strictly more than the generic
                        # heartbeat (bytes and a rate, not just elapsed seconds),
                        # so it stands in for one rather than being overwritten
                        # by one on the same tick.
                        last_heartbeat_at = now
                    if await _check_stall(now):
                        break
                    if heartbeat and now - last_heartbeat_at >= 2:
                        elapsed = int(now - start)
                        yield {
                            "progress": last_progress_value,
                            "message": f"Converting... ({elapsed}s)",
                        }
                        last_heartbeat_at = now
                    continue
                if not chunk:
                    break

                buffer += chunk.decode("utf-8", errors="replace")

                lines, buffer = _split_stream_lines(buffer)
                for line in lines:
                    _record_line(line)
                    now = time.monotonic()
                    progress = parse_progress(line)
                    advanced = False
                    if progress is not None:
                        saw_native_progress = True
                        if progress > last_progress_value:
                            last_progress_value = progress
                            last_activity_at = now
                            advanced = True
                    # Clamp to the running floor (incl. initial_progress) so a
                    # parsed value below it can't move the bar backward.
                    last_message = line
                    update = {"progress": last_progress_value, "message": line}
                    if advanced:
                        update["activity"] = True
                    yield update
                now = time.monotonic()
                update = _size_update(now)
                if update is not None:
                    yield update
                if await _check_stall(now):
                    break

            if buffer.strip():
                line = buffer.strip()
                _record_line(line)
                now = time.monotonic()
                progress = parse_progress(line)
                advanced = False
                if progress is not None:
                    saw_native_progress = True
                    if progress > last_progress_value:
                        last_progress_value = progress
                        last_activity_at = now
                        advanced = True
                last_message = line
                update = {"progress": last_progress_value, "message": line}
                if advanced:
                    update["activity"] = True
                yield update
                update = _size_update(time.monotonic())
                if update is not None:
                    yield update
                await _check_stall(time.monotonic())

            # A cancel or a stall already sent TERM (and KILL). Waiting out the
            # voluntary-exit grace again would hold the queue's only slot for
            # another minute for no reason, so go straight to the ladder.
            already_signalled = stall_error is not None or (
                cancel_event is not None and cancel_event.is_set()
            )
            if not await self.reap(
                process, exit_timeout=0 if already_signalled else _EXIT_GRACE,
            ):
                reap_failed = True
                abandoned_error = (
                    f"{fail_label} did not exit and could not be killed "
                    f"(pid {process.pid}); it is likely blocked on unresponsive "
                    "storage. Abandoning it so the queue can continue."
                )
            if self._logger.isEnabledFor(logging.DEBUG):
                self._logger.debug(
                    "%s pid=%s exit=%s", self._owner, process.pid, process.returncode,
                )

            if abandoned_error:
                # Abandonment outranks the stall that triggered it: the child is
                # still alive and still holding its output, which is the part an
                # operator has to act on.
                raise RuntimeError(
                    f"{stall_error} {abandoned_error}" if stall_error else abandoned_error
                )

            if stall_error:
                raise RuntimeError(stall_error)

            # Only raise when this run actually observed cancellation while the
            # process was still running (the watcher sets the flag only if
            # returncode was None when the event fired). A child that finished
            # before cancellation took effect is reported complete — its output
            # is valid and must not be deleted as a false cancellation.
            if cancelled_by_request:
                if abandoned_error:
                    # Reporting a clean CANCELLED would be a lie: the child is
                    # still running and still holding its output.
                    raise RuntimeError(
                        f"Cancellation did not stop {fail_label}. {abandoned_error}"
                    )
                raise ConversionCancelled("Conversion cancelled")

            # Nothing below can be trusted for an abandoned child: it has no
            # return code and never will.
            if abandoned_error:
                raise RuntimeError(abandoned_error)

            if process.returncode != 0:
                tail = "\n".join(output_lines[-6:])
                if tail:
                    raise RuntimeError(
                        f"{fail_label} failed with return code {process.returncode}."
                        f"\nLast output:\n{tail}",
                    )
                raise RuntimeError(
                    f"{fail_label} failed with return code {process.returncode}",
                )

            # A clean exit that left no file at output_path is an anomaly the
            # caller asked us to enforce (require_output): surface it with the
            # recorded stdout tail, since a tool that exits 0 without producing
            # output usually printed the reason first (e.g. nsz, whose output is
            # the temp file the runner already watches).
            if require_output and not os.path.exists(output_path):
                tail = "\n".join(output_lines[-6:])
                if tail:
                    raise RuntimeError(
                        f"{fail_label} produced no output file.\nLast output:\n{tail}",
                    )
                raise RuntimeError(f"{fail_label} produced no output file")

            yield {"progress": 100, "message": complete_message, "activity": True}
        finally:
            self.untrack_pid(process.pid)
            if size_probe is not None and not size_probe.done():
                size_probe.cancel()
            if cancel_task:
                cancel_task.cancel()
                try:
                    await cancel_task
                except asyncio.CancelledError:
                    pass
            # exit_timeout=0: nothing is reading the child's output any more, so
            # go straight to signalling instead of waiting out a voluntary exit.
            if not reap_failed:
                await self.reap(process, exit_timeout=0)
