"""Tests for the nsz service (compress/decompress/verify/keys guard).

These mock ``asyncio.create_subprocess_exec`` in the service module, so they
need neither the real ``nsz`` binary nor real prod.keys. A dummy key file on
disk is enough for the ``_keys_home`` symlink path to work.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services import nsz as nsz_module
from app.services.chdman import ConversionCancelled

NSZ_OUTPUT_FORMATS = nsz_module.NSZ_OUTPUT_FORMATS


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


def _write_produced(argv: list[str]) -> None:
    """Mimic nsz: write the stem-named output into the -o directory."""
    out_dir = argv[argv.index("-o") + 1]
    src = argv[-1]
    p = Path(src)
    produced = Path(out_dir) / f"{p.stem}{NSZ_OUTPUT_FORMATS[p.suffix.lower()]}"
    produced.write_bytes(b"converted")


@pytest.fixture
def keys_present(tmp_path, monkeypatch):
    """A real (dummy) keys dir so resolved_keys_file()/_keys_home() work."""
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()
    (keys_dir / "prod.keys").write_text("dummy = 00")
    monkeypatch.setattr(nsz_module.settings, "switch_keys_dir", str(keys_dir))
    return keys_dir


async def _drain(agen) -> list[dict]:
    return [u async for u in agen]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "src", "out"),
    [
        ("nsz_compress", "game.nsp", "game.nsz"),
        ("nsz_compress", "game.xci", "game.xcz"),
        ("nsz_decompress", "game.nsz", "game.nsp"),
        ("nsz_decompress", "game.xcz", "game.xci"),
    ],
)
async def test_convert_happy_path(tmp_path, monkeypatch, keys_present, mode, src, out):
    src_path = tmp_path / src
    src_path.write_bytes(b"input-bytes")
    out_path = tmp_path / out

    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["argv"] = list(args)
        captured["env"] = kwargs.get("env")
        _write_produced(list(args))
        return _FakeProcess(pid=4242, chunks=[b"Compressing\n"])

    monkeypatch.setattr(nsz_module.asyncio, "create_subprocess_exec", fake_exec)

    updates = await _drain(
        nsz_module.nsz_service.convert(str(src_path), str(out_path), mode),
    )

    assert updates[-1]["progress"] == 100
    assert out_path.read_bytes() == b"converted"
    # The work dir nsz wrote into is cleaned up; only the final file remains.
    assert not any(p.name.startswith(".nsz-") for p in tmp_path.iterdir())

    argv = captured["argv"]
    assert "--keys" not in argv          # nsz 4.6.x has no --keys flag
    assert "--minimal-output" not in argv  # nor this one
    assert ("-D" in argv) == (mode == "nsz_decompress")
    assert ("-C" in argv) == (mode == "nsz_compress")
    # Keys are supplied via HOME, not a flag.
    assert captured["env"]["HOME"] != ""


@pytest.mark.asyncio
async def test_convert_without_keys_fails_before_spawn(tmp_path, monkeypatch):
    src_path = tmp_path / "game.nsp"
    src_path.write_bytes(b"input")
    out_path = tmp_path / "game.nsz"

    monkeypatch.setattr(nsz_module.nsz_service, "resolved_keys_file", lambda: None)

    spawned = False

    async def fake_exec(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        return _FakeProcess(pid=1, chunks=[])

    monkeypatch.setattr(nsz_module.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError, match=r"prod\.keys"):
        await _drain(
            nsz_module.nsz_service.convert(str(src_path), str(out_path), "nsz_compress"),
        )
    assert spawned is False


@pytest.mark.asyncio
async def test_convert_nonzero_exit_raises(tmp_path, monkeypatch, keys_present):
    src_path = tmp_path / "game.nsp"
    src_path.write_bytes(b"input")
    out_path = tmp_path / "game.nsz"

    async def fake_exec(*_args, **_kwargs):
        return _FakeProcess(pid=5, chunks=[b"bad keys\n"], returncode=1)

    monkeypatch.setattr(nsz_module.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError, match="return code 1"):
        await _drain(
            nsz_module.nsz_service.convert(str(src_path), str(out_path), "nsz_compress"),
        )
    assert not out_path.exists()


@pytest.mark.asyncio
async def test_convert_missing_output_raises_with_tail(tmp_path, monkeypatch, keys_present):
    """nsz exiting 0 without producing a file (e.g. a silent key problem) raises
    via the runner's require_output guard, surfacing the printed reason in the
    error rather than a bare "no output" message."""
    src_path = tmp_path / "game.nsp"
    src_path.write_bytes(b"input")
    out_path = tmp_path / "game.nsz"

    async def fake_exec(*_args, **_kwargs):
        # Exits 0 but never writes the produced file into the work dir.
        return _FakeProcess(pid=9, chunks=[b"title key missing\n"], returncode=0)

    monkeypatch.setattr(nsz_module.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError, match="produced no output file") as excinfo:
        await _drain(
            nsz_module.nsz_service.convert(str(src_path), str(out_path), "nsz_compress"),
        )
    assert "title key missing" in str(excinfo.value)
    assert not out_path.exists()


class _CancelStdout:
    def __init__(self, stop: asyncio.Event):
        self._stop = stop
        self._first = True

    async def read(self, _n: int) -> bytes:
        if self._first:
            self._first = False
            return b"Compressing\n"
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
async def test_convert_cancel_leaves_no_output(tmp_path, monkeypatch, keys_present):
    src_path = tmp_path / "game.nsp"
    src_path.write_bytes(b"input")
    out_path = tmp_path / "game.nsz"

    cancel_event = asyncio.Event()
    cancel_event.set()

    async def fake_exec(*args, **_kwargs):
        _write_produced(list(args))  # partial output in the work dir
        return _CancelProcess(pid=7, stop=asyncio.Event())

    monkeypatch.setattr(nsz_module.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(ConversionCancelled):
        await _drain(
            nsz_module.nsz_service.convert(
                str(src_path), str(out_path), "nsz_compress", cancel_event=cancel_event,
            ),
        )
    assert not out_path.exists()
    assert not any(p.name.startswith(".nsz-") for p in tmp_path.iterdir())


@pytest.mark.asyncio
async def test_verify_passes_on_zero_exit(tmp_path, monkeypatch, keys_present):
    nsz_path = tmp_path / "game.nsz"
    nsz_path.write_bytes(b"data")

    async def fake_exec(*_args, **_kwargs):
        return _FakeProcess(pid=11, chunks=[], returncode=0)

    monkeypatch.setattr(nsz_module.asyncio, "create_subprocess_exec", fake_exec)

    result = await nsz_module.nsz_service.verify(str(nsz_path))
    assert result["valid"] is True


@pytest.mark.asyncio
async def test_verify_fails_on_nonzero_exit(tmp_path, monkeypatch, keys_present):
    nsz_path = tmp_path / "game.nsz"
    nsz_path.write_bytes(b"data")

    async def fake_exec(*_args, **_kwargs):
        return _FakeProcess(pid=12, chunks=[b"hash mismatch\n"], returncode=2)

    monkeypatch.setattr(nsz_module.asyncio, "create_subprocess_exec", fake_exec)

    result = await nsz_module.nsz_service.verify(str(nsz_path))
    assert result["valid"] is False
    assert "failed" in result["message"].lower()


@pytest.mark.asyncio
async def test_verify_without_keys_fails(tmp_path, monkeypatch):
    nsz_path = tmp_path / "game.nsz"
    nsz_path.write_bytes(b"data")
    monkeypatch.setattr(nsz_module.nsz_service, "keys_available", lambda: False)

    result = await nsz_module.nsz_service.verify(str(nsz_path))
    assert result["valid"] is False
    assert "prod.keys" in result["message"]


@pytest.mark.asyncio
async def test_verify_rejects_uncompressed_extension(tmp_path, keys_present):
    nsp = tmp_path / "game.nsp"
    nsp.write_bytes(b"data")
    result = await nsz_module.nsz_service.verify(str(nsp))
    assert result["valid"] is False
    assert "extension" in result["message"].lower()


class _HangingProcess:
    """``communicate()`` never returns, so the verify timeout must fire."""

    def __init__(self, pid: int = 99):
        self.pid = pid
        self.returncode = None
        self.terminated = False
        self.killed = False

    async def communicate(self):
        await asyncio.sleep(10)
        return b"", b""

    def terminate(self) -> None:
        # Teardown runs through the shared reap() ladder, which tries SIGTERM
        # before SIGKILL. A healthy child dies here, so kill() is never reached.
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = -9 if self.killed else 0
        return self.returncode


@pytest.mark.asyncio
async def test_verify_times_out_when_process_hangs(tmp_path, monkeypatch, keys_present):
    nsz_path = tmp_path / "game.nsz"
    nsz_path.write_bytes(b"data")

    proc = _HangingProcess(pid=77)

    async def fake_exec(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(nsz_module.asyncio, "create_subprocess_exec", fake_exec)
    # Bound the verify subprocess at a tiny timeout so the hang trips it fast.
    monkeypatch.setattr(nsz_module, "verify_timeout", lambda _owner=None: 0.05)

    result = await nsz_module.nsz_service.verify(str(nsz_path))
    assert result["valid"] is False
    assert "timed out" in result["message"].lower()
    # The runner must reap the hung child rather than leak it. The shared
    # ladder signals TERM first, so a child that dies there never reaches KILL.
    assert proc.terminated is True
    assert proc.pid not in set(nsz_module.nsz_service.active_pids())


def test_output_path_rejects_unknown_extension():
    with pytest.raises(ValueError, match="Unsupported file extension"):
        nsz_module.nsz_service.get_output_path_for_mode("nsz_compress", "/data/file.iso")


@pytest.mark.parametrize(
    ("compression", "expect_flag", "expect_level"),
    [
        ("solid:5", "-S", "5"),
        ("block:20", "-B", "20"),
        ("block:99", "-B", "22"),       # clamped to nsz max
        ("solid:0", "-S", "1"),         # clamped to nsz min
        ("solid", "-S", "18"),          # no level -> configured default
        (None, None, "18"),             # nothing specified -> default, no mode flag
        ("none", None, "18"),
    ],
)
def test_build_command_threads_compression(compression, expect_flag, expect_level):
    svc = nsz_module.nsz_service
    cmd = svc._build_command("/data/game.nsp", "/work", "nsz_compress", compression)
    # level is always passed
    assert cmd[cmd.index("-l") + 1] == expect_level
    for flag in ("-S", "-B"):
        assert (flag in cmd) == (flag == expect_flag)


def test_build_command_decompress_ignores_compression():
    svc = nsz_module.nsz_service
    cmd = svc._build_command("/data/game.nsz", "/work", "nsz_decompress", "block:20")
    assert "-D" in cmd
    assert "-C" not in cmd and "-l" not in cmd and "-B" not in cmd


def test_keys_available_reads_configured_dir(tmp_path, monkeypatch):
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()
    (keys_dir / "prod.keys").write_text("dummy = 00")
    monkeypatch.setattr(nsz_module.settings, "switch_keys_dir", str(keys_dir))
    assert nsz_module.nsz_service.keys_available() is True

    # SWITCH_KEYS set but empty -> unavailable.
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(nsz_module.settings, "switch_keys_dir", str(empty))
    assert nsz_module.nsz_service.keys_available() is False


def test_resolved_keys_searches_recursively(tmp_path, monkeypatch):
    # Keys live a couple levels below the configured SWITCH_KEYS directory.
    deep = tmp_path / "switch" / "firmware" / "keys"
    deep.mkdir(parents=True)
    (deep / "prod.keys").write_text("dummy = 00")
    monkeypatch.setattr(nsz_module.settings, "switch_keys_dir", str(tmp_path / "switch"))
    assert nsz_module.nsz_service.resolved_keys_file() == str(deep / "prod.keys")


def test_recursive_search_skips_junk_dirs(tmp_path, monkeypatch):
    # A key hidden only inside a junk dir (@eaDir) must NOT be discovered.
    junk = tmp_path / "switch" / "@eaDir"
    junk.mkdir(parents=True)
    (junk / "prod.keys").write_text("dummy = 00")
    monkeypatch.setattr(nsz_module.settings, "switch_keys_dir", str(tmp_path / "switch"))
    assert nsz_module.nsz_service.resolved_keys_file() is None


def test_info_reports_compression_state(tmp_path):
    nsz_path = tmp_path / "game.nsz"
    nsz_path.write_bytes(b"x" * 2048)
    info = nsz_module.nsz_service.info(str(nsz_path))
    assert info["compressed"] is True
    assert info["extension"] == ".nsz"

    nsp = tmp_path / "game.nsp"
    nsp.write_bytes(b"x" * 2048)
    assert nsz_module.nsz_service.info(str(nsp))["compressed"] is False
