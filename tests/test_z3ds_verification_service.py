"""Regression tests for z3ds verification safety checks."""

import asyncio
import struct
from pathlib import Path

import pytest

from app.services import z3ds_compress as z3ds_module
from app.services.chdman import ConversionCancelled


class _FakeStdin:
    def __init__(self, *, fail_write: bool = False):
        self.buffer = bytearray()
        self.closed = False
        self._fail_write = fail_write

    def write(self, chunk: bytes) -> None:
        if self._fail_write:
            raise OSError("simulated stream write failure")
        self.buffer.extend(chunk)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _FakeProcess:
    def __init__(self, pid: int, *, fail_write: bool = False):
        self.pid = pid
        self.stdin = _FakeStdin(fail_write=fail_write)
        self.stdout = None
        self.stderr = None
        self.returncode = None
        self.killed = False
        self.wait_called = False

    async def communicate(self):
        if self.returncode is None:
            self.returncode = -9 if self.killed else 0
        return b"", b""

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.wait_called = True
        if self.returncode is None:
            self.returncode = -9 if self.killed else 0
        return self.returncode


def _build_z3ds_container(
    *,
    metadata_size: int,
    payload: bytes,
    underlying_magic: bytes = b"CART",
) -> bytes:
    header = struct.pack(
        "<4s4sBBHIQQ",
        b"Z3DS",
        underlying_magic,
        1,
        0,
        0x20,
        metadata_size,
        len(payload),
        1024,
    )
    metadata = b"M" * metadata_size
    return header + metadata + payload


@pytest.mark.asyncio
async def test_verify_fails_when_zstd_missing(tmp_path: Path, monkeypatch):
    """Verification must fail closed when deep integrity tooling is unavailable."""
    rom_path = tmp_path / "game.z3ds"
    rom_path.write_bytes(b"Z3DS" + (b"\x00" * 256))

    monkeypatch.setattr(z3ds_module.shutil, "which", lambda name: None)

    result = await z3ds_module.z3ds_compress_service.verify(str(rom_path))

    assert result["valid"] is False
    assert "zstd" in result["message"].lower()


@pytest.mark.asyncio
async def test_verify_reads_payload_from_header_offset(tmp_path: Path, monkeypatch):
    """Verification should stream from computed header+metadata offset, not a fixed skip."""
    payload = b"zstd-stream-bytes"
    rom_path = tmp_path / "game.z3ds"
    rom_path.write_bytes(_build_z3ds_container(metadata_size=0x10, payload=payload))

    process = _FakeProcess(pid=10101)

    async def _fake_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(z3ds_module.shutil, "which", lambda _name: "/usr/bin/zstd")
    monkeypatch.setattr(
        z3ds_module.asyncio,
        "create_subprocess_exec",
        _fake_exec,
    )

    result = await z3ds_module.z3ds_compress_service.verify(str(rom_path))

    assert result["valid"] is True
    assert bytes(process.stdin.buffer) == payload


@pytest.mark.asyncio
async def test_verify_cleans_tracked_pid_on_stream_failure(tmp_path: Path, monkeypatch):
    """Verification must always untrack/reap child process on streaming errors."""
    payload = b"zstd-stream-bytes"
    rom_path = tmp_path / "game.z3ds"
    rom_path.write_bytes(_build_z3ds_container(metadata_size=0x10, payload=payload))

    process = _FakeProcess(pid=20202, fail_write=True)

    async def _fake_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(z3ds_module.shutil, "which", lambda _name: "/usr/bin/zstd")
    monkeypatch.setattr(
        z3ds_module.asyncio,
        "create_subprocess_exec",
        _fake_exec,
    )

    service = z3ds_module.z3ds_compress_service
    before = set(service.active_pids())
    result = await service.verify(str(rom_path))
    after = set(service.active_pids())

    assert result["valid"] is False
    assert process.wait_called is True
    assert process.pid not in after
    assert after == before


class _HangingProcess(_FakeProcess):
    """Streams normally, but ``communicate()`` never returns."""

    async def communicate(self):
        await asyncio.sleep(10)
        return b"", b""


@pytest.mark.asyncio
async def test_verify_times_out_when_process_hangs(tmp_path: Path, monkeypatch):
    """A hung zstd verify must trip the verify timeout and reap the child."""
    payload = b"zstd-stream-bytes"
    rom_path = tmp_path / "game.z3ds"
    rom_path.write_bytes(_build_z3ds_container(metadata_size=0x10, payload=payload))

    process = _HangingProcess(pid=30303)

    async def _fake_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(z3ds_module.shutil, "which", lambda _name: "/usr/bin/zstd")
    monkeypatch.setattr(z3ds_module.asyncio, "create_subprocess_exec", _fake_exec)
    # Bound the verify subprocess at a tiny timeout so the hang trips it fast.
    async def _tiny_bound(_path, _owner=None):
        return 0.05

    monkeypatch.setattr(z3ds_module, "resolve_verify_timeout", _tiny_bound)

    service = z3ds_module.z3ds_compress_service
    result = await service.verify(str(rom_path))

    assert result["valid"] is False
    assert "timed out" in result["message"].lower()
    # Reaped via the shared TERM -> KILL ladder: a child that answers SIGTERM
    # never needs SIGKILL, so assert it was signalled and is gone, not that it
    # was killed outright.
    assert process.returncode is not None
    assert process.pid not in set(service.active_pids())


def test_build_command_passes_direction_flag():
    """compress -> -c, decompress -> -d, both before the positional paths."""
    service = z3ds_module.z3ds_compress_service
    compress = service._build_command("in.cci", "out.zcci", "z3ds_compress")
    decompress = service._build_command("in.zcci", "out.cci", "z3ds_decompress")
    # ioprio_prefix may prepend a wrapper; assert on the tail (the real argv).
    assert compress[-3:] == ["-c", "in.cci", "out.zcci"]
    assert decompress[-3:] == ["-d", "in.zcci", "out.cci"]


@pytest.mark.parametrize(
    ("mode", "inp", "expected"),
    [
        ("z3ds_compress", "rom.cci", "rom.zcci"),
        ("z3ds_compress", "rom.cia", "rom.zcia"),
        ("z3ds_compress", "rom.3ds", "rom.z3ds"),
        ("z3ds_compress", "rom.cxi", "rom.zcxi"),
        ("z3ds_compress", "rom.3dsx", "rom.z3dsx"),
        ("z3ds_decompress", "rom.zcci", "rom.cci"),
        ("z3ds_decompress", "rom.zcia", "rom.cia"),
        ("z3ds_decompress", "rom.z3ds", "rom.3ds"),
        ("z3ds_decompress", "rom.zcxi", "rom.cxi"),
        ("z3ds_decompress", "rom.z3dsx", "rom.3dsx"),
    ],
)
def test_output_path_extension_maps(mode, inp, expected):
    service = z3ds_module.z3ds_compress_service
    assert service.get_output_path_for_mode(mode, inp).endswith(expected)


@pytest.mark.parametrize(
    ("name", "compressed"),
    [("a.zcxi", True), ("b.z3dsx", True), ("c.cxi", False), ("d.3dsx", False)],
)
def test_info_flags_new_compressed_extensions(tmp_path, name, compressed):
    """The .zcxi/.z3dsx containers report as compressed; raw .cxi/.3dsx don't."""
    service = z3ds_module.z3ds_compress_service
    rom = tmp_path / name
    rom.write_bytes(b"\0" * 4096)
    assert service.info(str(rom))["compressed"] is compressed


# ---------------------------------------------------------------------------
# convert()  (now delegated to SubprocessRunner — spawn is intercepted via the
# shared global asyncio.create_subprocess_exec the runner also calls)
# ---------------------------------------------------------------------------


class _ConvertStdout:
    """Yields preset stdout chunks, then EOF."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)

    async def read(self, _n: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _ConvertProcess:
    """Fake convert subprocess: streams preset stdout chunks, then exits."""

    def __init__(self, pid: int, chunks: list[bytes], returncode: int = 0):
        self.pid = pid
        self.stdout = _ConvertStdout(chunks)
        self.returncode = None
        self._final_rc = returncode
        self.killed = False

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = -9 if self.killed else self._final_rc
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _CancelStdout:
    """Stdout that yields one line, then blocks until the process is stopped."""

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
    """Fake subprocess that only exits once terminate()/kill() is called."""

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
@pytest.mark.parametrize(
    ("mode", "src", "out"),
    [
        ("z3ds_compress", "game.cci", "game.zcci"),
        ("z3ds_decompress", "game.zcci", "game.cci"),
    ],
)
async def test_convert_happy_path(tmp_path, monkeypatch, mode, src, out):
    """convert() streams the runner's updates and ends at 100% with output written."""
    src_path = tmp_path / src
    src_path.write_bytes(b"rom-bytes")
    out_path = tmp_path / out

    async def fake_exec(*args, **_kwargs):
        # z3ds_compressor writes the container to the last positional arg.
        Path(list(args)[-1]).write_bytes(b"converted")
        return _ConvertProcess(pid=4242, chunks=[b"working\n"])

    monkeypatch.setattr(z3ds_module.asyncio, "create_subprocess_exec", fake_exec)

    service = z3ds_module.z3ds_compress_service
    updates = [u async for u in service.convert(str(src_path), str(out_path), mode)]

    assert updates[0]["progress"] == 5  # the "Starting 3DS ..." preamble
    assert updates[-1]["progress"] == 100
    assert out_path.read_bytes() == b"converted"
    assert not service.active_pids()


@pytest.mark.asyncio
async def test_convert_cancel_cleans_partial_output(tmp_path, monkeypatch):
    """A cancel mid-convert raises ConversionCancelled and removes the partial."""
    src_path = tmp_path / "game.cci"
    src_path.write_bytes(b"rom")
    out_path = tmp_path / "game.zcci"

    cancel_event = asyncio.Event()
    cancel_event.set()

    async def fake_exec(*args, **_kwargs):
        Path(list(args)[-1]).write_bytes(b"partial")  # partial written in place
        return _CancelProcess(pid=7, stop=asyncio.Event())

    monkeypatch.setattr(z3ds_module.asyncio, "create_subprocess_exec", fake_exec)

    service = z3ds_module.z3ds_compress_service
    with pytest.raises(ConversionCancelled):
        async for _ in service.convert(
            str(src_path), str(out_path), "z3ds_compress", cancel_event=cancel_event,
        ):
            pass

    assert not out_path.exists()
    assert not service.active_pids()
