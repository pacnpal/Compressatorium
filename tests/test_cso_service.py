"""Tests for the maxcso service (compress/decompress/verify/output paths).

These mock ``asyncio.create_subprocess_exec`` in the service module, so they
need neither the real ``maxcso`` binary nor real disc images.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from app.services import maxcso as maxcso_module
from app.services.chdman import ConversionCancelled

# ``services.subprocess_runner``, not ``app.services.subprocess_runner``: the
# two are separate module objects (the app is imported with ``app`` on the path
# and uses the former), so patching the latter would leave the code under test
# untouched and quietly prove nothing.
from services import subprocess_runner as runner_module

service = maxcso_module.maxcso_service


class _FakeProcess:
    """Reads preset stdout chunks then EOF; wait() sets the return code."""

    def __init__(self, pid: int, chunks: list[bytes], returncode: int = 0):
        self.pid = pid
        self._chunks = list(chunks)
        self.returncode = None
        self._final_rc = returncode
        self.killed = False
        self.stdout = self

    async def read(self, _n: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = -9 if self.killed else self._final_rc
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def communicate(self):
        if self.returncode is None:
            self.returncode = self._final_rc
        return b"", b""


def _write_output(argv: list[str]) -> None:
    """Mimic maxcso: write the file named after the ``-o`` argument."""
    out_path = argv[argv.index("-o") + 1]
    Path(out_path).write_bytes(b"converted")


async def _drain(agen) -> list[dict]:
    return [u async for u in agen]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "src", "out"),
    [
        ("cso_compress", "game.iso", "game.cso"),
        ("cso2_compress", "game.iso", "game.cso"),
        ("zso_compress", "game.iso", "game.zso"),
        ("dax_compress", "game.iso", "game.dax"),
        ("cso_decompress", "game.cso", "game.iso"),
        ("cso_decompress", "game.zso", "game.iso"),
        ("cso_decompress", "game.dax", "game.iso"),
    ],
)
async def test_convert_happy_path(tmp_path, monkeypatch, mode, src, out):
    src_path = tmp_path / src
    src_path.write_bytes(b"input-bytes")
    out_path = tmp_path / out

    captured = {}

    async def fake_exec(*args, **_kwargs):
        captured["argv"] = list(args)
        _write_output(list(args))
        return _FakeProcess(pid=4242, chunks=[b"42%\n"])

    monkeypatch.setattr(maxcso_module.asyncio, "create_subprocess_exec", fake_exec)

    updates = await _drain(service.convert(str(src_path), str(out_path), mode))

    assert updates[-1]["progress"] == 100
    assert out_path.read_bytes() == b"converted"
    argv = captured["argv"]
    assert ("--decompress" in argv) == (mode == "cso_decompress")
    assert ("--format=zso" in argv) == (mode == "zso_compress")


@pytest.mark.asyncio
async def test_convert_nonzero_exit_raises(tmp_path, monkeypatch):
    src_path = tmp_path / "game.iso"
    src_path.write_bytes(b"input")
    out_path = tmp_path / "game.cso"

    async def fake_exec(*_args, **_kwargs):
        return _FakeProcess(pid=5, chunks=[b"bad input\n"], returncode=1)

    monkeypatch.setattr(maxcso_module.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError, match="return code 1"):
        await _drain(service.convert(str(src_path), str(out_path), "cso_compress"))


@pytest.mark.asyncio
async def test_convert_failure_finishes_even_if_cleanup_wedges(tmp_path, monkeypatch):
    """A dead output mount must not turn a failed job into a frozen queue.

    Issue #267: the runner's waits are bounded, but the partial-output sweep
    that runs *after* it raises used to be an unbounded ``os.remove`` on the
    very mount the conversion just died on. With MAX_CONCURRENT_JOBS defaulting
    to 1 and jobs running inline, blocking there froze every queued job. The
    conversion must still surface its own error, promptly.
    """
    src_path = tmp_path / "game.iso"
    src_path.write_bytes(b"input")
    out_path = tmp_path / "game.cso"

    async def fake_exec(*_args, **_kwargs):
        return _FakeProcess(pid=5, chunks=[b"bad input\n"], returncode=1)

    release = threading.Event()
    entered = threading.Event()

    # Signature must match `_unlink_all`: a stub that raises TypeError would be
    # caught as an ordinary cleanup failure, and this would pass without the
    # wedge or the timeout ever being exercised.
    def _wedged(_paths, _discover, _abandoned):
        entered.set()
        release.wait(30)  # an unlink stuck in uninterruptible I/O

    monkeypatch.setattr(maxcso_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(runner_module, "_unlink_all", _wedged)
    monkeypatch.setattr(runner_module, "_CLEANUP_TIMEOUT", 0.05)

    try:
        with pytest.raises(RuntimeError, match="return code 1"):
            await asyncio.wait_for(
                _drain(service.convert(str(src_path), str(out_path), "cso_compress")),
                timeout=10,
            )
        assert entered.is_set(), "the wedged sweep must actually have been run"
    finally:
        release.set()


class _CancelStdout:
    def __init__(self, stop: asyncio.Event):
        self._stop = stop
        self._first = True

    async def read(self, _n: int) -> bytes:
        if self._first:
            self._first = False
            return b"working\n"
        await self._stop.wait()
        return b""


class _CancelProcess:
    def __init__(self, pid: int, stop: asyncio.Event):
        self.pid = pid
        self._stop = stop
        self.stdout = _CancelStdout(stop)
        self.returncode = None

    def terminate(self) -> None:
        self.returncode = -15
        self._stop.set()

    def kill(self) -> None:
        self.returncode = -9
        self._stop.set()

    async def wait(self) -> int:
        await self._stop.wait()
        if self.returncode is None:
            self.returncode = -15
        return self.returncode


@pytest.mark.asyncio
async def test_convert_cancel_removes_partial_output(tmp_path, monkeypatch):
    src_path = tmp_path / "game.iso"
    src_path.write_bytes(b"input")
    out_path = tmp_path / "game.cso"

    cancel_event = asyncio.Event()
    cancel_event.set()

    async def fake_exec(*args, **_kwargs):
        _write_output(list(args))  # partial output written directly to -o path
        return _CancelProcess(pid=7, stop=asyncio.Event())

    monkeypatch.setattr(maxcso_module.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(ConversionCancelled):
        await _drain(
            service.convert(
                str(src_path), str(out_path), "cso_compress", cancel_event=cancel_event,
            ),
        )
    assert not out_path.exists()


@pytest.mark.asyncio
async def test_convert_spawn_failure_preserves_existing_output(tmp_path, monkeypatch):
    """A pre-spawn failure (e.g. missing binary) must NOT delete a pre-existing
    output: maxcso never ran, so it wrote nothing to remove."""
    src_path = tmp_path / "game.iso"
    src_path.write_bytes(b"input")
    out_path = tmp_path / "game.cso"
    out_path.write_bytes(b"pre-existing valid output")

    async def fake_exec(*_args, **_kwargs):
        raise FileNotFoundError("maxcso binary missing")

    monkeypatch.setattr(maxcso_module.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(FileNotFoundError):
        await _drain(service.convert(str(src_path), str(out_path), "cso_compress"))

    assert out_path.read_bytes() == b"pre-existing valid output"  # untouched


@pytest.mark.asyncio
async def test_convert_cleans_partial_on_generator_close(tmp_path, monkeypatch):
    """Closing the generator mid-conversion (task cancellation / GeneratorExit)
    removes the in-place partial — the writer cleans up on BaseException, not
    only on ConversionCancelled."""
    src_path = tmp_path / "game.iso"
    src_path.write_bytes(b"input")
    out_path = tmp_path / "game.cso"

    async def fake_exec(*args, **_kwargs):
        _write_output(list(args))  # partial written straight to the -o path
        return _CancelProcess(pid=21, stop=asyncio.Event())

    monkeypatch.setattr(maxcso_module.asyncio, "create_subprocess_exec", fake_exec)

    gen = service.convert(str(src_path), str(out_path), "cso_compress")
    assert (await gen.__anext__())["progress"] == 1  # "Starting..." preamble
    await gen.__anext__()  # enters the runner: spawns (writes partial), reads a line
    assert out_path.exists()

    await gen.aclose()  # GeneratorExit -> in-place cleanup
    assert not out_path.exists()


@pytest.mark.asyncio
async def test_verify_passes_on_zero_exit(tmp_path, monkeypatch):
    cso_path = tmp_path / "game.cso"
    cso_path.write_bytes(b"data")

    async def fake_exec(*_args, **_kwargs):
        return _FakeProcess(pid=11, chunks=[], returncode=0)

    monkeypatch.setattr(maxcso_module.asyncio, "create_subprocess_exec", fake_exec)

    result = await service.verify(str(cso_path))
    assert result["valid"] is True


@pytest.mark.asyncio
async def test_verify_applies_priority_wrappers(tmp_path, monkeypatch):
    """`maxcso --crc` is as heavy as a convert, so it must honor nice/ionice."""
    cso_path = tmp_path / "game.cso"
    cso_path.write_bytes(b"data")

    captured = {}

    async def fake_exec(*args, **_kwargs):
        captured["argv"] = list(args)
        return _FakeProcess(pid=13, chunks=[], returncode=0)

    monkeypatch.setattr(maxcso_module.asyncio, "create_subprocess_exec", fake_exec)

    await service.verify(str(cso_path))

    argv = captured["argv"]
    # The maxcso invocation is the tail; the head is the shared priority policy.
    assert argv[-3:] == [service.maxcso_path, "--crc", str(cso_path)]
    expected_prefix = (
        maxcso_module.nice_prefix(maxcso_module._OWNER)
        + maxcso_module.ioprio_prefix(maxcso_module._OWNER)
    )
    assert argv[: len(expected_prefix)] == expected_prefix


@pytest.mark.asyncio
async def test_verify_fails_on_nonzero_exit(tmp_path, monkeypatch):
    cso_path = tmp_path / "game.cso"
    cso_path.write_bytes(b"data")

    async def fake_exec(*_args, **_kwargs):
        return _FakeProcess(pid=12, chunks=[b"crc mismatch\n"], returncode=2)

    monkeypatch.setattr(maxcso_module.asyncio, "create_subprocess_exec", fake_exec)

    result = await service.verify(str(cso_path))
    assert result["valid"] is False
    assert "failed" in result["message"].lower()


@pytest.mark.asyncio
async def test_verify_rejects_uncompressed_extension(tmp_path):
    iso = tmp_path / "game.iso"
    iso.write_bytes(b"data")
    result = await service.verify(str(iso))
    assert result["valid"] is False
    assert "extension" in result["message"].lower()


@pytest.mark.parametrize(
    ("mode", "inp", "expected_suffix"),
    [
        ("cso_compress", "/data/game.iso", ".cso"),
        ("cso2_compress", "/data/game.iso", ".cso"),
        ("zso_compress", "/data/game.iso", ".zso"),
        ("dax_compress", "/data/game.iso", ".dax"),
        ("cso_decompress", "/data/game.cso", ".iso"),
        ("cso_decompress", "/data/game.zso", ".iso"),
        ("cso_decompress", "/data/game.dax", ".iso"),
    ],
)
def test_output_path_for_mode(mode, inp, expected_suffix):
    out = service.get_output_path_for_mode(mode, inp)
    assert out.endswith(f"game{expected_suffix}")


def test_output_path_rejects_unknown_mode():
    with pytest.raises(ValueError, match="Unsupported maxcso mode"):
        service.get_output_path_for_mode("bogus_mode", "/data/game.iso")


def test_build_command_flags():
    compress = service._build_command("/data/game.iso", "/data/game.cso", "cso_compress")
    assert "--decompress" not in compress
    assert "--format=zso" not in compress
    # CSO v1 is the default format -> no --format flag at all.
    assert not any(a.startswith("--format=") for a in compress)

    cso2 = service._build_command("/data/game.iso", "/data/game.cso", "cso2_compress")
    assert "--format=cso2" in cso2

    zso = service._build_command("/data/game.iso", "/data/game.zso", "zso_compress")
    assert "--format=zso" in zso

    dax = service._build_command("/data/game.iso", "/data/game.dax", "dax_compress")
    assert "--format=dax" in dax

    decompress = service._build_command("/data/game.cso", "/data/game.iso", "cso_decompress")
    assert "--decompress" in decompress
    assert "--format=zso" not in decompress


_ALL_EFFORT_FLAGS = ("--fast", "--use-zopfli", "--use-libdeflate", "--use-lz4brute")


@pytest.mark.parametrize(
    ("mode", "effort", "expected"),
    [
        ("cso_compress", "fast", ["--fast"]),
        ("cso_compress", "max", ["--use-zopfli", "--use-libdeflate"]),
        ("cso_compress", "default", []),
        ("cso_compress", None, []),
        # CSO2/DAX are deflate-based -> same Zopfli+libdeflate "max" trials as CSO.
        ("cso2_compress", "max", ["--use-zopfli", "--use-libdeflate"]),
        ("dax_compress", "max", ["--use-zopfli", "--use-libdeflate"]),
        ("dax_compress", "fast", ["--fast"]),
        ("zso_compress", "fast", ["--fast"]),
        ("zso_compress", "max", ["--use-lz4brute"]),       # lz4 format -> lz4 trials
        ("cso_decompress", "max", []),                      # effort ignored on decompress
    ],
)
def test_build_command_effort_flags(mode, effort, expected):
    cmd = service._build_command("/data/in", "/data/out", mode, effort)
    for flag in _ALL_EFFORT_FLAGS:
        assert (flag in cmd) == (flag in expected)


def test_info_reports_compression_state(tmp_path):
    cso_path = tmp_path / "game.cso"
    cso_path.write_bytes(b"x" * 2048)
    info = service.info(str(cso_path))
    assert info["compressed"] is True
    assert info["extension"] == ".cso"

    iso = tmp_path / "game.iso"
    iso.write_bytes(b"x" * 2048)
    assert service.info(str(iso))["compressed"] is False
