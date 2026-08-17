"""Direct tests for ``SubprocessRunner``, the shared subprocess loop that
``chdman`` and ``dolphin_tool`` now delegate their ``convert()`` to.

These drive the highest-risk paths (cancel, stall timeout, non-zero exit) with
a real child process spawned via ``sys.executable`` so the spawn / line-buffer
/ cancel-watcher / finalize machinery is exercised end to end, since the real
CLIs are unavailable in the sandbox.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import threading
import time

import pytest

from app.services import subprocess_runner as runner_module
from app.services.subprocess_runner import ConversionCancelled, SubprocessRunner


def _parse_pct(line: str) -> int | None:
    match = re.search(r"(\d+)\s*%", line)
    return int(match.group(1)) if match else None


def _py_cmd(script: str) -> list[str]:
    return [sys.executable, "-u", "-c", script]


async def _drain(gen) -> list[dict]:
    return [update async for update in gen]


def test_happy_path_streams_progress_then_final_100(tmp_path):
    out = tmp_path / "out.bin"
    script = (
        "import sys\n"
        "for p in (10, 50, 90):\n"
        "    sys.stdout.write(f'Progress {p}%\\n')\n"
    )
    runner = SubprocessRunner(owner="test")

    updates = asyncio.run(
        _drain(
            runner.run(
                _py_cmd(script),
                input_path=str(tmp_path / "in.bin"),
                output_path=str(out),
                parse_progress=_parse_pct,
                fail_label="testproc",
            )
        )
    )

    progresses = [u["progress"] for u in updates]
    assert progresses[:3] == [10, 50, 90]
    assert updates[-1]["progress"] == 100
    assert updates[-1]["message"] == "Conversion complete"
    assert not runner.active_pids()


def test_nonzero_exit_raises_runtimeerror_with_tail(tmp_path):
    script = (
        "import sys\n"
        "sys.stdout.write('starting\\n')\n"
        "sys.stdout.write('boom error\\n')\n"
        "sys.exit(3)\n"
    )
    runner = SubprocessRunner(owner="test")

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            _drain(
                runner.run(
                    _py_cmd(script),
                    input_path=str(tmp_path / "in.bin"),
                    output_path=str(tmp_path / "out.bin"),
                    parse_progress=_parse_pct,
                    fail_label="testproc",
                )
            )
        )

    message = str(excinfo.value)
    assert "testproc failed with return code 3" in message
    assert "boom error" in message
    assert not runner.active_pids()


def test_require_output_missing_raises_with_tail(tmp_path):
    """A clean exit (code 0) that leaves no file at output_path is a failure
    when require_output=True, and the error carries the stdout tail so the
    reason the tool printed before exiting isn't lost (nsz's prior behavior)."""
    script = (
        "import sys\n"
        "sys.stdout.write('preparing\\n')\n"
        "sys.stdout.write('keys file missing, nothing written\\n')\n"
        "sys.exit(0)\n"
    )
    runner = SubprocessRunner(owner="test")
    out = tmp_path / "out.bin"  # the child never creates this

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            _drain(
                runner.run(
                    _py_cmd(script),
                    input_path=str(tmp_path / "in.bin"),
                    output_path=str(out),
                    parse_progress=_parse_pct,
                    fail_label="nsz",
                    require_output=True,
                )
            )
        )

    message = str(excinfo.value)
    assert "nsz produced no output file" in message
    assert "keys file missing, nothing written" in message
    assert not out.exists()
    assert not runner.active_pids()


def test_require_output_present_completes(tmp_path):
    """When the child does produce output_path, require_output is satisfied and
    the run finishes with the normal terminal 100%."""
    out = tmp_path / "out.bin"
    script = (
        "import sys\n"
        f"open({str(out)!r}, 'wb').write(b'data')\n"
        "sys.stdout.write('done\\n')\n"
    )
    runner = SubprocessRunner(owner="test")

    updates = asyncio.run(
        _drain(
            runner.run(
                _py_cmd(script),
                input_path=str(tmp_path / "in.bin"),
                output_path=str(out),
                parse_progress=_parse_pct,
                fail_label="nsz",
                require_output=True,
            )
        )
    )

    assert updates[-1]["progress"] == 100
    assert updates[-1]["message"] == "Conversion complete"
    assert out.exists()
    assert not runner.active_pids()


def test_cancel_event_raises_conversion_cancelled(tmp_path):
    script = (
        "import sys, time\n"
        "sys.stdout.write('working 10%\\n')\n"
        "time.sleep(30)\n"
    )
    runner = SubprocessRunner(owner="test")
    cancel_event = asyncio.Event()

    async def _run():
        gen = runner.run(
            _py_cmd(script),
            input_path=str(tmp_path / "in.bin"),
            output_path=str(tmp_path / "out.bin"),
            parse_progress=_parse_pct,
            cancel_event=cancel_event,
            fail_label="testproc",
        )
        async for update in gen:
            if "10%" in update["message"]:
                cancel_event.set()

    with pytest.raises(ConversionCancelled):
        asyncio.run(_run())
    assert not runner.active_pids()


def test_cancel_event_preset_terminates_and_raises(tmp_path):
    """A cancel_event already set when run() starts is honored by the watcher:
    the child is spawned, terminated, and ConversionCancelled is raised.

    (The runner intentionally does not short-circuit before spawning, so that a
    ConversionCancelled always implies the child ran — callers rely on that to
    avoid deleting an output their conversion never wrote.)
    """
    script = "import time; time.sleep(30)"
    runner = SubprocessRunner(owner="test")
    cancel_event = asyncio.Event()
    cancel_event.set()

    async def _run():
        return await _drain(
            runner.run(
                _py_cmd(script),
                input_path=str(tmp_path / "in.bin"),
                output_path=str(tmp_path / "out.bin"),
                parse_progress=_parse_pct,
                cancel_event=cancel_event,
                fail_label="testproc",
            )
        )

    with pytest.raises(ConversionCancelled):
        asyncio.run(_run())
    assert not runner.active_pids()


def test_spawn_failure_propagates_without_masking(tmp_path, monkeypatch):
    """A pre-spawn failure (create_subprocess_exec raising) surfaces as-is, with
    no PID tracked — callers distinguish it from a post-spawn error to avoid
    deleting an output the conversion never wrote.
    """

    async def fake_exec(*_args, **_kwargs):
        raise FileNotFoundError("no such binary")

    monkeypatch.setattr(runner_module.asyncio, "create_subprocess_exec", fake_exec)
    runner = SubprocessRunner(owner="test")

    async def _run():
        return await _drain(
            runner.run(
                _py_cmd("import sys"),
                input_path=str(tmp_path / "in.bin"),
                output_path=str(tmp_path / "out.bin"),
                parse_progress=_parse_pct,
                fail_label="testproc",
            )
        )

    with pytest.raises(FileNotFoundError):
        asyncio.run(_run())
    assert not runner.active_pids()


def test_cancel_watcher_survives_terminate_processlookuperror(tmp_path, monkeypatch):
    """If the child exits between the watcher's returncode check and terminate(),
    the resulting ProcessLookupError must not mask the cancellation.

    Without the guard the watcher task ends in ProcessLookupError, which the
    finally re-raises over the intended ConversionCancelled.
    """

    class _RaceProc:
        """Child whose terminate() raises ProcessLookupError (already exited)."""

        def __init__(self):
            self.pid = 999
            self.returncode = None
            self._stop = asyncio.Event()
            self.stdout = self
            self._first = True

        async def read(self, _n: int) -> bytes:
            if self._first:
                self._first = False
                return b"working 10%\n"
            await self._stop.wait()
            return b""

        async def wait(self) -> int:
            await self._stop.wait()
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

        def terminate(self) -> None:
            self._stop.set()
            raise ProcessLookupError

        def kill(self) -> None:
            self._stop.set()

    async def fake_exec(*_args, **_kwargs):
        return _RaceProc()

    monkeypatch.setattr(runner_module.asyncio, "create_subprocess_exec", fake_exec)
    runner = SubprocessRunner(owner="test")
    cancel_event = asyncio.Event()

    async def _run():
        gen = runner.run(
            _py_cmd("import sys"),
            input_path=str(tmp_path / "in.bin"),
            output_path=str(tmp_path / "out.bin"),
            parse_progress=_parse_pct,
            cancel_event=cancel_event,
            fail_label="testproc",
        )
        async for update in gen:
            if "10%" in update["message"]:
                cancel_event.set()

    with pytest.raises(ConversionCancelled):
        asyncio.run(_run())
    assert not runner.active_pids()


def test_cancel_racing_clean_exit_reports_success(tmp_path, monkeypatch):
    """When cancellation races a clean exit (the child already returned 0), the
    conversion finished in that instant and is reported complete — a cancel that
    races a successful completion delivers the result rather than discarding it
    (a deliberate product choice). The still-running case is covered by
    test_cancel_event_preset_terminates_and_raises.
    """

    class _TimeoutThenExitedProc:
        """read() times out (no more output) while returncode is already 0, so
        the watcher skips marking the cancel and the run completes normally."""

        def __init__(self):
            self.pid = 555
            self.returncode = 0
            self.stdout = self

        async def read(self, _n: int) -> bytes:
            raise asyncio.TimeoutError  # the runner wraps read in wait_for(timeout=2)

        async def wait(self) -> int:
            return 0

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            pass

    async def fake_exec(*_args, **_kwargs):
        return _TimeoutThenExitedProc()

    monkeypatch.setattr(runner_module.asyncio, "create_subprocess_exec", fake_exec)
    runner = SubprocessRunner(owner="test")
    cancel_event = asyncio.Event()
    cancel_event.set()

    updates = asyncio.run(
        _drain(
            runner.run(
                _py_cmd("import sys"),
                input_path=str(tmp_path / "in.bin"),
                output_path=str(tmp_path / "out.bin"),
                parse_progress=_parse_pct,
                cancel_event=cancel_event,
                fail_label="testproc",
            )
        )
    )

    assert updates[-1]["progress"] == 100
    assert updates[-1]["message"] == "Conversion complete"
    assert not runner.active_pids()


def test_stall_timeout_raises_runtimeerror(tmp_path, monkeypatch):
    # Force a short stall window; the child emits one line then goes silent and
    # never grows the output file, so the stall watchdog must fire.
    monkeypatch.setattr(
        runner_module, "compute_progress_stall_timeout", lambda **_: 1,
    )
    script = (
        "import sys, time\n"
        "sys.stdout.write('starting\\n')\n"
        "time.sleep(30)\n"
    )
    runner = SubprocessRunner(owner="test")

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            _drain(
                runner.run(
                    _py_cmd(script),
                    input_path=str(tmp_path / "in.bin"),
                    output_path=str(tmp_path / "out.bin"),
                    parse_progress=_parse_pct,
                    fail_label="testproc",
                )
            )
        )

    assert "Conversion stalled" in str(excinfo.value)
    assert not runner.active_pids()


def test_size_progress_emits_from_output_growth(tmp_path):
    """A tool that prints no percent still reports, from output-file growth.

    The child writes the output file then a stdout line each step, so the
    post-line size tick fires without waiting on the read timeout, and the
    stream ends at the runner's terminal 100%. ``mode`` supplies the size ratio,
    so the fallback carries a percentage as well as the bytes/rate message.
    """
    out = tmp_path / "out.bin"
    src = tmp_path / "in.bin"
    src.write_bytes(b"s" * 2000)  # cso_compress ratio 0.5 -> expected 1000
    # The growth probe runs in a worker thread and is read one tick later, so
    # the child pauses between steps to let each measurement land -- mirroring a
    # real conversion, where ticks are seconds apart.
    script = (
        "import sys, time\n"
        f"out = {str(out)!r}\n"
        "with open(out, 'wb') as f:\n"
        "    for i in range(3):\n"
        "        f.write(b'x' * 100); f.flush()\n"
        "        sys.stdout.write(f'step {i}\\n'); sys.stdout.flush()\n"
        "        time.sleep(0.2)\n"
    )
    runner = SubprocessRunner(owner="test")

    updates = asyncio.run(
        _drain(
            runner.run(
                _py_cmd(script),
                input_path=str(src),
                output_path=str(out),
                parse_progress=lambda _line: None,
                mode="cso_compress",
                fail_label="testproc",
            )
        )
    )

    size_updates = [u for u in updates if "MB written" in u["message"]]
    assert size_updates, "expected at least one size-based progress update"
    # The estimate matches the shared helper (100 B against expected 1000 -> 14%)
    # and the emitted bar never goes backwards.
    assert max(u["progress"] for u in size_updates) >= 14
    progresses = [u["progress"] for u in updates]
    assert progresses == sorted(progresses)
    assert updates[-1]["progress"] == 100
    assert updates[-1]["message"] == "Conversion complete"
    assert not runner.active_pids()


def test_env_is_forwarded_to_subprocess(tmp_path):
    """run(env=...) is passed through to the spawned subprocess's environment."""
    script = (
        "import os, sys\n"
        "sys.stdout.write('VAL=' + os.environ.get('CMP_RUNNER_TEST', 'unset') + '\\n')\n"
    )
    runner = SubprocessRunner(owner="test")

    updates = asyncio.run(
        _drain(
            runner.run(
                _py_cmd(script),
                input_path=str(tmp_path / "in.bin"),
                output_path=str(tmp_path / "out.bin"),
                parse_progress=lambda _line: None,
                env={**os.environ, "CMP_RUNNER_TEST": "from-env"},
                fail_label="testproc",
            )
        )
    )

    messages = " ".join(u["message"] for u in updates)
    assert "VAL=from-env" in messages
    assert not runner.active_pids()


def test_nice_via_wrapper_omits_preexec_fn(tmp_path, monkeypatch):
    """nice_via_wrapper=True must spawn with preexec_fn=None — the deadlock-
    avoidance contract maxcso/nsz rely on — while still completing normally.

    Asserting the spawn kwarg (not just completion) guards against a regression
    that reintroduced a harmless-looking preexec_fn, which a completion-only
    test would miss.
    """
    out = tmp_path / "out.bin"
    script = "import sys; sys.stdout.write('done 50%\\n')"
    runner = SubprocessRunner(owner="test")

    captured = {}
    real_exec = runner_module.asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        captured.update(kwargs)
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(runner_module.asyncio, "create_subprocess_exec", spy)

    updates = asyncio.run(
        _drain(
            runner.run(
                _py_cmd(script),
                input_path=str(tmp_path / "in.bin"),
                output_path=str(out),
                parse_progress=_parse_pct,
                nice_via_wrapper=True,
                fail_label="testproc",
            )
        )
    )

    assert captured.get("preexec_fn") is None
    assert 50 in [u["progress"] for u in updates]
    assert updates[-1]["progress"] == 100
    assert updates[-1]["message"] == "Conversion complete"
    assert not runner.active_pids()


def test_initial_progress_floor_keeps_bar_monotonic(tmp_path):
    """A non-parseable early line must not drop the bar below the seeded floor.

    The child emits a stdout line before the output file grows; with
    parse_progress -> None that line would otherwise emit progress 0. With
    initial_progress=5 it emits the floor instead, and size growth + the final
    100% stay monotonic.
    """
    out = tmp_path / "out.bin"
    script = (
        "import sys\n"
        f"out = {str(out)!r}\n"
        "sys.stdout.write('warming up\\n'); sys.stdout.flush()\n"
        "with open(out, 'wb') as f:\n"
        "    f.write(b'x' * 100); f.flush()\n"
        "    sys.stdout.write('step\\n'); sys.stdout.flush()\n"
    )
    runner = SubprocessRunner(owner="test")
    src = tmp_path / "in.bin"
    src.write_bytes(b"s" * 2000)

    updates = asyncio.run(
        _drain(
            runner.run(
                _py_cmd(script),
                input_path=str(src),
                output_path=str(out),
                parse_progress=lambda _line: None,
                initial_progress=5,
                mode="cso_compress",
                fail_label="testproc",
            )
        )
    )

    progresses = [u["progress"] for u in updates]
    assert min(progresses) >= 5            # never drops below the seeded floor
    assert progresses == sorted(progresses)  # monotonic non-decreasing
    assert updates[-1]["progress"] == 100
    assert not runner.active_pids()


# ---------------------------------------------------------------------------
# run_capture  (shared one-shot capture with cancel + timeout)
# ---------------------------------------------------------------------------


def test_run_capture_returns_output_and_returncode():
    runner = SubprocessRunner(owner="test")
    rc, stdout, stderr = asyncio.run(
        runner.run_capture(_py_cmd("import sys; sys.stdout.write('hello')")),
    )
    assert rc == 0
    assert stdout == b"hello"
    assert stderr == b""
    assert not runner.active_pids()


def test_run_capture_nonzero_returncode():
    runner = SubprocessRunner(owner="test")
    rc, _stdout, _stderr = asyncio.run(
        runner.run_capture(_py_cmd("import sys; sys.exit(3)")),
    )
    assert rc == 3
    assert not runner.active_pids()


def test_run_capture_cancel_returns_none_and_terminates():
    runner = SubprocessRunner(owner="test")

    async def go():
        cancel = asyncio.Event()
        cancel.set()  # already cancelled: must abort before the sleep elapses
        return await runner.run_capture(
            _py_cmd("import time; time.sleep(30)"), cancel_event=cancel,
        )

    rc, _stdout, _stderr = asyncio.run(go())
    # None signals the abort; the child was terminated, not waited out.
    assert rc is None
    assert not runner.active_pids()


def test_run_capture_timeout_returns_none_and_terminates():
    runner = SubprocessRunner(owner="test")
    rc, _stdout, _stderr = asyncio.run(
        runner.run_capture(
            _py_cmd("import time; time.sleep(30)"), timeout=0.2,
        ),
    )
    assert rc is None
    assert not runner.active_pids()


# ---------------------------------------------------------------------------
# reap()  (bounded teardown -- issue #263)
# ---------------------------------------------------------------------------


class _UnkillableProcess:
    """A child that ignores every signal, like one wedged in D state.

    A real process in uninterruptible I/O cannot be simulated (SIGKILL always
    works on a healthy one), so the ladder is exercised against a stand-in that
    records the signals and never exits.
    """

    def __init__(self):
        self.pid = -1
        self.returncode = None
        self.signals: list[str] = []

    def terminate(self):
        self.signals.append("TERM")

    def kill(self):
        self.signals.append("KILL")

    async def wait(self):
        await asyncio.sleep(3600)  # never returns; every caller must bound it


def test_reap_abandons_a_child_that_survives_sigkill(monkeypatch):
    """reap() escalates TERM -> KILL and then gives up instead of hanging.

    The regression guard for #263: an unbounded wait here blocked the job
    forever and, at MAX_CONCURRENT_JOBS=1, every job queued behind it.
    """
    monkeypatch.setattr(runner_module, "_TERM_GRACE", 0.01)
    monkeypatch.setattr(runner_module, "_KILL_GRACE", 0.01)
    runner = SubprocessRunner(owner="test")
    process = _UnkillableProcess()

    reaped = asyncio.run(runner.reap(process, exit_timeout=0.01))

    assert reaped is False, "an unkillable child must be abandoned, not waited on"
    assert process.signals == ["TERM", "KILL"], "expected the full escalation ladder"


def test_reap_returns_true_for_an_already_exited_child():
    """The common case costs nothing: no signals, no waiting."""
    runner = SubprocessRunner(owner="test")
    process = _UnkillableProcess()
    process.returncode = 0

    assert asyncio.run(runner.reap(process)) is True
    assert process.signals == []


def test_native_progress_suppresses_the_size_fallback(tmp_path):
    """A tool that reports its own percent is left alone.

    Native parsing and the growth fallback must not both drive the message
    line; once a real percent is parsed the fallback stands down.
    """
    out = tmp_path / "out.bin"
    src = tmp_path / "in.bin"
    src.write_bytes(b"s" * 2000)
    script = (
        "import sys\n"
        f"out = {str(out)!r}\n"
        "with open(out, 'wb') as f:\n"
        "    for pct in (10, 50):\n"
        "        f.write(b'x' * 100); f.flush()\n"
        "        sys.stdout.write(f'{pct}%\\n'); sys.stdout.flush()\n"
    )
    runner = SubprocessRunner(owner="test")

    updates = asyncio.run(
        _drain(
            runner.run(
                _py_cmd(script),
                input_path=str(src),
                output_path=str(out),
                parse_progress=_parse_pct,
                mode="cso_compress",
                fail_label="testproc",
            )
        )
    )

    assert not [u for u in updates if "MB written" in u["message"]]
    assert 50 in [u["progress"] for u in updates]


def test_size_message_rate_reflects_the_current_window_not_the_average():
    """A job that was fast and is now crawling must report the crawl.

    Guards the distinction the status line exists to make: with a cumulative
    average, an early fast phase keeps advertising a high rate long after the
    conversion has slowed to nothing.
    """
    mb = 1024 * 1024
    # Same 2 GB written in both cases; only the most recent window differs.
    fast = runner_module.output_size_message(2000 * mb, 500 * mb, 60.0)
    crawling = runner_module.output_size_message(2000 * mb, 1 * mb, 60.0)

    assert "2,000 MB written" in fast and "2,000 MB written" in crawling
    assert "500.0 MB/min" in fast
    assert "1.0 MB/min" in crawling


def test_bounded_probe_gives_up_instead_of_waiting(monkeypatch):
    """A filesystem probe that never returns must not hold the caller.

    Stands in for a `stat` wedged on an unresponsive mount, which cannot be
    cancelled -- the thread is written off and the caller decides its own
    fallback rather than the job hanging (issue #263).
    """
    monkeypatch.setattr(runner_module, "_STAT_TIMEOUT", 0.05)
    # An Event, not a sleep: shutdown(wait=False) cannot stop the worker, and
    # the interpreter joins it at exit, so an uninterruptible sleep would stall
    # the whole test process on the way out.
    release = threading.Event()

    async def _go():
        return await runner_module._bounded_probe(release.wait, 30)

    try:
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(_go())
    finally:
        release.set()


def test_bounded_probe_returns_the_value_when_it_lands():
    async def _go():
        return await runner_module._bounded_probe(len, "abcd")

    assert asyncio.run(_go()) == 4


def test_growing_output_is_not_killed_by_a_short_stall_timeout(tmp_path, monkeypatch):
    """A converter that is writing must never be stalled out by probe lag.

    The growth probe runs off the event loop and is read a tick later, so the
    first checks have no measurement to judge by. With a stall timeout shorter
    than the probe cadence, treating "no sample yet" as "no growth" would kill a
    child whose output is growing steadily.
    """
    monkeypatch.setattr(
        runner_module, "compute_progress_stall_timeout", lambda **_: 1,
    )
    out = tmp_path / "out.bin"
    src = tmp_path / "in.bin"
    src.write_bytes(b"s" * 2000)
    script = (
        "import sys, time\n"
        f"out = {str(out)!r}\n"
        "with open(out, 'wb') as f:\n"
        "    for _ in range(40):\n"          # ~4s of steady writing, silent stdout
        "        f.write(b'x' * 4096); f.flush()\n"
        "        time.sleep(0.1)\n"
    )
    runner = SubprocessRunner(owner="test")

    updates = asyncio.run(
        _drain(
            runner.run(
                _py_cmd(script),
                input_path=str(src),
                output_path=str(out),
                parse_progress=lambda _line: None,
                mode="cso_compress",
                fail_label="testproc",
            )
        )
    )

    assert updates[-1]["progress"] == 100
    assert updates[-1]["message"] == "Conversion complete"
    assert not runner.active_pids()


def test_activity_flag_marks_real_movement_only(tmp_path):
    """Keep-alives are not activity; output growth is.

    The job manager judges liveness off this flag, so a heartbeat must not
    refresh its clock (that made a hung job look alive) and growth must, even
    once the size estimate pins at its 95% cap (that made a healthy job look
    stalled).
    """
    out = tmp_path / "out.bin"
    src = tmp_path / "in.bin"
    src.write_bytes(b"s" * 2000)
    # Writes once immediately, then goes silent. The first tick has no sample
    # yet so a heartbeat fires; the growth sample lands a tick later; the rest
    # of the silence produces heartbeats against an unchanging file.
    script = (
        "import time\n"
        f"out = {str(out)!r}\n"
        "with open(out, 'wb') as f:\n"
        "    f.write(b'x' * 4096); f.flush()\n"
        "time.sleep(4.5)\n"
    )
    runner = SubprocessRunner(owner="test")

    updates = asyncio.run(
        _drain(
            runner.run(
                _py_cmd(script),
                input_path=str(src),
                output_path=str(out),
                parse_progress=lambda _line: None,
                mode="cso_compress",
                heartbeat=True,
                fail_label="testproc",
            )
        )
    )

    heartbeats = [u for u in updates if u["message"].startswith("Converting...")]
    growth = [u for u in updates if "MB written" in u["message"]]

    assert heartbeats, "expected the keep-alive to fire during the silent stretch"
    assert not any(u.get("activity") for u in heartbeats)
    assert growth and all(u.get("activity") for u in growth)


def test_stall_timeout_is_clamped_to_the_sampling_floor(tmp_path, monkeypatch):
    """A sub-cadence stall timeout must not kill a converter that is writing.

    Growth is sampled every couple of seconds, so a shorter window cannot tell a
    stalled child from an unsampled one. The timeout is raised to the floor
    rather than believed.
    """
    monkeypatch.setattr(
        runner_module, "compute_progress_stall_timeout", lambda **_: 1,
    )
    out = tmp_path / "out.bin"
    src = tmp_path / "in.bin"
    src.write_bytes(b"s" * 2000)
    # Writes and prints every 100ms: continuously alive, but between samples the
    # last-known size is stale, which a 1s window would misread as a stall.
    script = (
        "import sys, time\n"
        f"out = {str(out)!r}\n"
        "with open(out, 'wb') as f:\n"
        "    for i in range(80):\n"
        "        f.write(b'x' * 4096); f.flush()\n"
        "        sys.stdout.write(f'chunk {i}\\n'); sys.stdout.flush()\n"
        "        time.sleep(0.1)\n"
    )
    runner = SubprocessRunner(owner="test")

    updates = asyncio.run(
        _drain(
            runner.run(
                _py_cmd(script),
                input_path=str(src),
                output_path=str(out),
                parse_progress=lambda _line: None,
                mode="cso_compress",
                fail_label="testproc",
            )
        )
    )

    assert updates[-1]["progress"] == 100
    assert not runner.active_pids()


# --------------------------------------------------------------------------- #
# Bounded partial-output cleanup (issue #267)
# --------------------------------------------------------------------------- #


def test_remove_partial_output_deletes_every_path(tmp_path):
    """The sweep clears the whole set a failed run can leave behind."""
    base = tmp_path / "Game.iso"
    parts = [tmp_path / f"Game.iso.{n}" for n in range(3)]
    for path in (base, *parts):
        path.write_bytes(b"partial")

    async def _go():
        return await runner_module.remove_partial_output(
            *[str(p) for p in (base, *parts)],
        )

    assert asyncio.run(_go()) is True
    assert not any(p.exists() for p in (base, *parts))


def test_remove_partial_output_treats_an_absent_path_as_done(tmp_path):
    """Nothing to remove is the goal, not a failure -- and needs no extra stat."""
    async def _go():
        return await runner_module.remove_partial_output(str(tmp_path / "gone.iso"))

    assert asyncio.run(_go()) is True


def test_remove_partial_output_reports_what_it_could_not_remove(tmp_path):
    """A path it cannot unlink is reported, and does not stop the rest.

    A directory shadowing the output is the real case (makeps3iso would write
    inside it): os.remove fails, the sweep says so, and the sibling partial is
    still cleared rather than left behind by an early return.
    """
    blocked = tmp_path / "Game.iso"
    blocked.mkdir()
    partial = tmp_path / "Game.iso.1"
    partial.write_bytes(b"partial")

    async def _go():
        return await runner_module.remove_partial_output(
            str(blocked), str(partial),
        )

    assert asyncio.run(_go()) is False
    assert blocked.is_dir()
    assert not partial.exists()


def test_remove_partial_output_gives_up_instead_of_freezing_the_queue(monkeypatch):
    """An unlink wedged on a dead mount must not hold the job open.

    The scenario #265 left open: the runner abandons the child (bounded), the
    tool wrapper then tries to delete the partial *on the same dead mount*, and
    with MAX_CONCURRENT_JOBS=1 running jobs inline, a cleanup that never
    returns freezes the whole queue. Cleanup reports failure and the job
    finalises instead.
    """
    release = threading.Event()

    def _wedged(_paths):
        release.wait(30)  # stands in for an unlink in uninterruptible I/O

    monkeypatch.setattr(runner_module, "_unlink_all", _wedged)

    async def _go():
        return await runner_module.remove_partial_output(
            "/mnt/dead/game.cso", timeout=0.05,
        )

    try:
        assert asyncio.run(_go()) is False
    finally:
        release.set()


def test_a_cancelled_wait_does_not_undo_a_removal_already_underway(tmp_path):
    """Cancelling abandons the wait, not work the sweep already did.

    The old synchronous unlinks couldn't be skipped by a second cancellation.
    The bounded await keeps the useful half of that: the removal is dispatched
    before the wait, so a cancel arriving afterwards can't take it back. (What
    a cancel *does* stop is paths the sweep hasn't started — see
    ``test_abandoned_sweep_stops_before_touching_later_paths``.)
    """
    partial = tmp_path / "game.cso"
    partial.write_bytes(b"partial")
    start = threading.Event()
    release = threading.Event()
    real_unlink_all = runner_module._unlink_all

    def _slow(paths, discover, abandoned):
        real_unlink_all(paths, discover, abandoned)  # the removal lands here
        start.set()
        release.wait(30)  # keep the thread alive so the cancel races it

    async def _go():
        task = asyncio.ensure_future(
            runner_module.remove_partial_output(str(partial)),
        )
        await asyncio.get_running_loop().run_in_executor(None, start.wait, 5)
        task.cancel()
        return await task

    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(runner_module, "_unlink_all", _slow)
            # The cancellation is swallowed: the caller re-raises the failure it
            # was already unwinding, so the job's reported outcome is unchanged.
            assert asyncio.run(_go()) is False
    finally:
        release.set()
    assert not partial.exists()


def test_remove_partial_tree_clears_the_work_dir(tmp_path):
    work_dir = tmp_path / ".nsz-abc"
    (work_dir / "nested").mkdir(parents=True)
    (work_dir / "nested" / "Game.nsz").write_bytes(b"partial")

    async def _go():
        return await runner_module.remove_partial_tree(str(work_dir))

    assert asyncio.run(_go()) is True
    assert not work_dir.exists()


def test_remove_partial_tree_gives_up_on_an_unresponsive_volume(monkeypatch):
    release = threading.Event()

    def _wedged(_path, _ignore_errors):
        release.wait(30)

    monkeypatch.setattr(runner_module.shutil, "rmtree", _wedged)

    async def _go():
        return await runner_module.remove_partial_tree(
            "/mnt/dead/.nsz-abc", timeout=0.05,
        )

    try:
        assert asyncio.run(_go()) is False
    finally:
        release.set()


def test_remove_partial_output_discovers_inside_the_bound(tmp_path):
    """Enumerating the set to sweep must be bounded along with the unlinking.

    A split build's parts are only knowable by probing the disk -- the same
    disk the sweep is about to unlink from. Enumerating on the event loop first
    would leave that probe unbounded, so `discover` runs on the cleanup thread.
    """
    base = tmp_path / "Game.iso"
    parts = [tmp_path / f"Game.iso.{n}" for n in range(2)]
    for path in (base, *parts):
        path.write_bytes(b"partial")
    probed_on: list[str] = []

    def _discover() -> list[str]:
        probed_on.append(threading.current_thread().name)
        return [str(p) for p in (base, *parts)]

    async def _go():
        return await runner_module.remove_partial_output(discover=_discover)

    assert asyncio.run(_go()) is True
    assert not any(p.exists() for p in (base, *parts))
    assert probed_on == ["fs-probe"]  # not the event loop's thread


def test_remove_partial_output_bounds_a_wedged_discovery(monkeypatch):
    """A discovery that never returns is given up on like a wedged unlink."""
    release = threading.Event()

    def _wedged_discover():
        release.wait(30)
        return []

    async def _go():
        return await runner_module.remove_partial_output(
            discover=_wedged_discover, label="split set", timeout=0.05,
        )

    try:
        assert asyncio.run(_go()) is False
    finally:
        release.set()


def test_abandoned_sweep_stops_before_touching_later_paths(tmp_path):
    """A sweep that outlived its bound must not keep deleting.

    The mount can recover minutes after the job gave up, by which point a retry
    may own those names — an abandoned sweep resuming through the rest of its
    list would delete the *new* job's good output.
    """
    wedge = tmp_path / "Game.iso"
    wedge.write_bytes(b"partial")
    later = tmp_path / "Game.iso.1"
    later.write_bytes(b"a retry's valid output")

    release = threading.Event()
    real_remove = os.remove

    def _slow_remove(path):
        if str(path) == str(wedge):
            release.wait(30)  # still blocked when the caller gives up
        real_remove(path)

    async def _go():
        return await runner_module.remove_partial_output(
            str(wedge), str(later), timeout=0.05,
        )

    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(runner_module.os, "remove", _slow_remove)
            assert asyncio.run(_go()) is False
    finally:
        release.set()

    # The unlink already in flight can't be recalled, so `wedge` still goes...
    for _ in range(100):
        if not wedge.exists():
            break
        time.sleep(0.05)
    assert not wedge.exists()
    # ...but everything after it is left alone.
    assert later.read_bytes() == b"a retry's valid output"


def test_remove_partial_output_can_preserve_cancellation(tmp_path):
    """A caller that isn't unwinding a failure needs the cancel to survive.

    romz's pre-run sweep runs before anything has failed, so swallowing the
    CancelledError there would turn a cancelled job into a failed one.
    """
    partial = tmp_path / "game.7z"
    partial.write_bytes(b"stale")
    start = threading.Event()
    release = threading.Event()
    real_unlink_all = runner_module._unlink_all

    def _slow(paths, discover, abandoned):
        start.set()
        release.wait(30)
        real_unlink_all(paths, discover, abandoned)

    async def _go():
        task = asyncio.ensure_future(
            runner_module.remove_partial_output(
                str(partial), propagate_cancel=True,
            ),
        )
        await asyncio.get_running_loop().run_in_executor(None, start.wait, 5)
        task.cancel()
        return await task

    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(runner_module, "_unlink_all", _slow)
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(_go())
    finally:
        release.set()
