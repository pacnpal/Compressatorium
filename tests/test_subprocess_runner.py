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
    assert updates[-1] == {"progress": 100, "message": "Conversion complete"}
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

    assert updates[-1] == {"progress": 100, "message": "Conversion complete"}
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

    assert updates[-1] == {"progress": 100, "message": "Conversion complete"}
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
    assert updates[-1] == {"progress": 100, "message": "Conversion complete"}
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
    assert updates[-1] == {"progress": 100, "message": "Conversion complete"}
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
