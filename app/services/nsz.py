"""Wrapper for the ``nsz`` tool (https://github.com/nicoboss/nsz).

nsz compresses Nintendo Switch ``.nsp``/``.xci`` dumps into ``.nsz``/``.xcz``
and back. Unlike the other tools here, it needs the console's ``prod.keys`` to
decrypt the NCA content before compressing (and to re-encrypt on the way back).

Two things about real nsz (4.6.x) drive this wrapper:

- **Keys are loaded at import time** from ``keys.txt`` next to the binary or
  ``~/.switch/prod.keys``. There is no ``--keys`` flag. So we point the child's
  ``$HOME`` at a throwaway dir whose ``.switch/prod.keys`` is a symlink to the
  operator's configured key file. We ship no keys; the operator mounts their own.
- **Progress is drawn with enlighten**, which goes silent on a non-TTY pipe, so
  there is no parseable percentage text. We estimate progress from the growth of
  the output file, like the z3ds service does.

nsz writes its output into a directory (``-o``) and names the file itself. We
hand it a private temp dir, then move the single result onto the exact
``output_path`` the job expects. That avoids nsz's same-name skip behaviour and
keeps duplicate handling in the job layer where it belongs.
"""
import asyncio
import contextlib
from logging_setup import get_logger
import os
import shutil
import tempfile
from collections.abc import AsyncGenerator
from pathlib import Path

from config import settings
from services.subprocess_runner import (
    SubprocessRunner,
    ioprio_prefix,
    nice_prefix,
    verify_timeout,
)
from utils.junk import is_junk_entry

# Input -> output extension, both directions. The four extensions are distinct,
# so this one map covers compress and decompress without ambiguity.
NSZ_COMPRESS_EXTENSIONS = {".nsp", ".xci"}
NSZ_DECOMPRESS_EXTENSIONS = {".nsz", ".xcz"}
NSZ_OUTPUT_FORMATS = {
    ".nsp": ".nsz",
    ".xci": ".xcz",
    ".nsz": ".nsp",
    ".xcz": ".xci",
}

# Rough output:input size ratios, used only to smooth the progress bar.

# Key filenames nsz/homebrew tools use.
_KEY_FILENAMES = ("prod.keys", "keys.txt")
# Cap the recursive volume walk so a huge library can't stall startup or a
# request. If keys live deeper than this, set SWITCH_KEYS directly.
_MAX_KEY_SEARCH_DIRS = 5000

logger = get_logger("nsz")


class NszService:
    """Wrapper for the nsz binary."""

    def __init__(self):
        self.nsz_path = settings.nsz_path
        # _run_convert() delegates its streaming loop to the runner; verify_stream()
        # still spawns nsz -V directly but tracks its PID in the same set, so
        # active_pids() reflects both.
        self._runner = SubprocessRunner(owner="nsz")

    # ----- keys -------------------------------------------------------------

    def resolved_keys_file(self) -> str | None:
        """Path to a readable key file, or None.

        ``SWITCH_KEYS`` is the source of truth when set: its directory is checked
        directly and then searched recursively. When unset, check the standard
        nsz/homebrew locations, then recursively search the game volumes and the
        data dir. The recursive walk skips junk dirs and is bounded by
        ``_MAX_KEY_SEARCH_DIRS`` so a huge library can't stall things.
        """
        configured = settings.switch_keys_dir
        if configured:
            return self._first_key_in_dir(configured) or self._recursive_find_keys(
                [configured],
            )
        for candidate in self._standard_key_files():
            if self._readable(candidate):
                return candidate
        return self._recursive_find_keys(self._search_roots())

    def keys_available(self) -> bool:
        return self.resolved_keys_file() is not None

    def key_search_dirs(self) -> list[str]:
        """Locations checked when SWITCH_KEYS is unset (for startup logging)."""
        seen: list[str] = []
        candidates = [os.path.dirname(p) for p in self._standard_key_files()]
        candidates += [f"{root} (recursive)" for root in self._search_roots()]
        for entry in candidates:
            if entry not in seen:
                seen.append(entry)
        return seen

    def log_startup_status(self) -> None:
        """Log once, at startup, whether Switch (nsz) is enabled and where the
        keys were found (or which locations were searched and came up empty)."""
        keys_file = self.resolved_keys_file()
        if keys_file:
            logger.info("Switch (nsz) enabled: prod.keys found at %s", keys_file)
        elif settings.switch_keys_dir:
            logger.warning(
                "Switch (nsz) disabled: SWITCH_KEYS=%s has no prod.keys/keys.txt "
                "(searched recursively)",
                settings.switch_keys_dir,
            )
        else:
            logger.info(
                "Switch (nsz) disabled: no prod.keys found (searched %s). Set "
                "SWITCH_KEYS to the directory holding your prod.keys to enable it.",
                ", ".join(self.key_search_dirs()),
            )

    @staticmethod
    def _readable(path: str) -> bool:
        return os.path.isfile(path) and os.access(path, os.R_OK)

    @staticmethod
    def _standard_key_files() -> list[str]:
        """Standard nsz/homebrew key locations (explicit files, cheap to stat)."""
        home = os.path.expanduser("~")
        xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        return [
            os.path.join(home, ".switch", "prod.keys"),
            os.path.join(xdg, "nsz", "prod.keys"),
            os.path.join(home, ".config", "nsz", "prod.keys"),
        ]

    @staticmethod
    def _search_roots() -> list[str]:
        """Roots walked recursively for keys: the game volumes + the data dir."""
        roots: list[str] = []
        with contextlib.suppress(Exception):  # never let discovery break a job
            roots.extend(settings.volumes)
        with contextlib.suppress(Exception):
            roots.append(str(settings.data_dir))
        return roots

    @classmethod
    def _first_key_in_dir(cls, directory: str) -> str | None:
        for name in _KEY_FILENAMES:
            candidate = os.path.join(directory, name)
            if cls._readable(candidate):
                return candidate
        return None

    def _recursive_find_keys(self, roots: list[str]) -> str | None:
        """Walk ``roots`` for a key file, skipping junk dirs, bounded by the
        directory cap. Returns the first readable prod.keys/keys.txt found."""
        visited = 0
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                # Prune junk dirs in place so os.walk won't descend into them.
                dirnames[:] = [d for d in dirnames if not is_junk_entry(d)]
                visited += 1
                if visited > _MAX_KEY_SEARCH_DIRS:
                    logger.warning(
                        "Switch key search hit the %d-directory cap under %s; set "
                        "SWITCH_KEYS to point directly at your prod.keys.",
                        _MAX_KEY_SEARCH_DIRS, root,
                    )
                    break
                for name in _KEY_FILENAMES:
                    if name in filenames:
                        candidate = os.path.join(dirpath, name)
                        if self._readable(candidate):
                            return candidate
        return None

    @contextlib.contextmanager
    def _keys_home(self):
        """Yield an env dict whose HOME exposes the keys at ~/.switch/prod.keys.

        nsz reads keys at import from ``$HOME/.switch/prod.keys``; we symlink the
        configured key file there in a throwaway HOME so the child finds it no
        matter where the operator mounted it.
        """
        keys_file = self.resolved_keys_file()
        if not keys_file:
            raise RuntimeError(
                "nsz needs prod.keys to (de)compress Switch content. Mount your "
                "own prod.keys and set SWITCH_KEYS to the directory holding it "
                "(or place it at ~/.switch/prod.keys). No keys ship with this app.",
            )
        home_dir = tempfile.mkdtemp(prefix=".nsz-home-")
        try:
            switch_dir = os.path.join(home_dir, ".switch")
            os.makedirs(switch_dir, exist_ok=True)
            dest = os.path.join(switch_dir, "prod.keys")
            src = os.path.abspath(keys_file)
            try:
                os.symlink(src, dest)
            except (OSError, NotImplementedError):
                # Symlinks need a privilege on Windows; the key file is tiny, so
                # just copy it into the throwaway HOME instead.
                shutil.copy2(src, dest)
            yield {**os.environ, "HOME": home_dir}
        finally:
            shutil.rmtree(home_dir, ignore_errors=True)

    # ----- command ----------------------------------------------------------

    def _parse_compression(self, compression: str | None) -> tuple[bool | None, int]:
        """Resolve a per-job ``compression`` string into (block, level).

        Format is ``"<mode>:<level>"`` where mode is ``solid`` or ``block``,
        the same ``codec:level`` shape the UI sends for Dolphin. ``block`` is
        None when unspecified (let nsz pick its per-container default). Level
        falls back to the configured default and is clamped to nsz's 1-22 range.
        """
        level = settings.nsz_compression_level
        block: bool | None = None
        if compression and compression.lower() != "none":
            mode_part, _, level_part = compression.partition(":")
            mode_part = mode_part.strip().lower()
            if mode_part == "block":
                block = True
            elif mode_part == "solid":
                block = False
            if level_part.strip():
                try:
                    level = int(level_part)
                except ValueError:
                    pass
        return block, max(1, min(22, level))

    def _build_command(
        self, input_path: str, work_dir: str, mode: str, compression: str | None = None,
    ) -> list[str]:
        cmd = [self.nsz_path]
        if mode == "nsz_decompress":
            cmd.append("-D")
        else:
            block, level = self._parse_compression(compression)
            cmd += ["-C", "-l", str(level)]
            if block is True:
                cmd.append("-B")
            elif block is False:
                cmd.append("-S")
        cmd += ["-o", work_dir, input_path]

        # Apply nice/ionice via command wrappers, NOT preexec_fn: forking a
        # Python callable in a multithreaded app (this one uses threadpools) can
        # deadlock the child before exec. `nice`/`ionice` are exec-only.
        prefix = nice_prefix("nsz") + ioprio_prefix("nsz")
        return prefix + cmd

    def active_pids(self) -> list[int]:
        return self._runner.active_pids()

    # ----- output paths -----------------------------------------------------

    def get_output_path(self, input_path: str, output_dir: str | None = None) -> str:
        input_file = Path(input_path)
        ext = input_file.suffix.lower()
        if ext not in NSZ_OUTPUT_FORMATS:
            raise ValueError(f"Unsupported file extension: {ext}")
        output_name = input_file.stem + NSZ_OUTPUT_FORMATS[ext]
        if output_dir:
            return str(Path(output_dir) / output_name)
        return str(input_file.parent / output_name)

    @staticmethod
    def get_output_path_for_mode(
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        """Output path for an nsz mode. Both modes map purely on the input
        extension via ``NSZ_OUTPUT_FORMATS``.

        ``treat_as_stem`` is accepted for interface parity with z3ds and needs
        no separate branch: archive members arrive as flattened filenames that
        preserve their original extension (see
        ``ArchiveService._output_name_for_member``), so the suffix lookup maps
        them the same way as an on-disk file (.nsp -> .nsz, .xci -> .xcz, and
        back). Every accepted input extension is a known key, so an unknown
        extension is a genuine error rather than a fallback case."""
        input_p = Path(input_path)
        ext = input_p.suffix.lower()
        output_ext = NSZ_OUTPUT_FORMATS.get(ext)
        if output_ext is None:
            raise ValueError(f"Unsupported file extension: {ext}")
        filename = f"{input_p.stem}{output_ext}"
        if output_dir:
            return str(Path(output_dir) / filename)
        return str(input_p.parent / filename)

    def _produced_name(self, input_path: str) -> str:
        p = Path(input_path)
        return f"{p.stem}{NSZ_OUTPUT_FORMATS[p.suffix.lower()]}"

    # ----- convert ----------------------------------------------------------

    async def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str = "nsz_compress",
        *,
        compression: str | None = None,  # per-job "<solid|block>:<level>"
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        verb = "decompression" if mode == "nsz_decompress" else "compression"
        out_dir = os.path.dirname(output_path) or "."
        # Resolve the produced filename before creating the temp dir, so an
        # unexpected input extension (KeyError) surfaces here rather than
        # orphaning an empty .nsz-* work dir.
        produced_name = self._produced_name(input_path)
        await asyncio.to_thread(os.makedirs, out_dir, exist_ok=True)

        # Private temp dir on the same filesystem as the destination, so moving
        # the finished file onto output_path is a cheap rename.
        work_dir = await asyncio.to_thread(
            tempfile.mkdtemp, prefix=".nsz-", dir=out_dir,
        )
        produced_path = os.path.join(work_dir, produced_name)

        try:
            with self._keys_home() as env:
                async for update in self._run_convert(
                    input_path, produced_path, work_dir, mode, verb, env,
                    cancel_event, compression,
                ):
                    yield update

            await asyncio.to_thread(os.replace, produced_path, output_path)
            yield {"progress": 100, "message": f"Switch {verb} complete"}
        finally:
            await asyncio.to_thread(shutil.rmtree, work_dir, True)

    async def _run_convert(self, input_path, produced_path, work_dir, mode, verb,
                           env, cancel_event, compression=None) -> AsyncGenerator[dict, None]:
        cmd = self._build_command(input_path, work_dir, mode, compression)

        yield {"progress": 1, "message": f"Starting Switch {verb}..."}

        # nsz names the file itself inside work_dir, so the runner watches
        # produced_path for growth/stall. nice/ionice are command wrappers in
        # _build_command (nice_via_wrapper) and the private keys-home env is
        # forwarded; nsz prints no parseable percent, so the runner's fallback drives the
        # bar. The terminal 100% is held back — convert() emits it after the
        # os.replace onto the real output_path. require_output makes the runner
        # raise (with the stdout tail) when nsz exits 0 but leaves no produced
        # file, so the reason it printed isn't lost.
        async for update in self._runner.run(
            cmd,
            input_path=input_path,
            output_path=produced_path,
            parse_progress=lambda _line: None,
            initial_progress=1,
            cancel_event=cancel_event,
            fail_label="nsz",
            complete_message=f"Switch {verb} complete",
            mode=mode,
            nice_via_wrapper=True,
            env=env,
            require_output=True,
        ):
            if update.get("progress", 0) >= 100:
                continue
            yield update

    # ----- info -------------------------------------------------------------

    def info(self, file_path: str) -> dict:
        """Filesystem-level info (size, format label, compression state). nsz
        exposes no offline metadata dump. Synchronous; wrap callers in a
        threadpool."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        file_size = os.path.getsize(file_path)
        ext = Path(file_path).suffix.lower()

        is_compressed = ext in NSZ_DECOMPRESS_EXTENSIONS
        base_format = {
            ".nsp": "NSP (Nintendo Submission Package)",
            ".nsz": "NSZ (compressed NSP)",
            ".xci": "XCI (Cartridge Image)",
            ".xcz": "XCZ (compressed XCI)",
        }.get(ext)

        size_mb = file_size / (1024 * 1024)
        size_display = f"{size_mb:.2f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB"

        return {
            "file": file_path,
            "size": file_size,
            "size_display": size_display,
            "format": base_format,
            "extension": ext,
            "compressed": is_compressed,
            "compression_type": "NSZ (zstandard)" if is_compressed else None,
        }

    @staticmethod
    def is_convertible(filename: str) -> bool:
        ext = Path(filename).suffix.lower()
        return ext in NSZ_COMPRESS_EXTENSIONS or ext in NSZ_DECOMPRESS_EXTENSIONS

    # ----- verify -----------------------------------------------------------

    async def verify(self, file_path: str) -> dict:
        final = {"valid": False, "message": "Verification failed"}
        async for update in self.verify_stream(file_path):
            if update.get("type") in ("complete", "error"):
                final = update
        return {
            "valid": bool(final.get("valid", False)),
            "message": final.get("message") or "Verification failed",
        }

    async def verify_stream(self, file_path: str) -> AsyncGenerator[dict, None]:
        """Verify a compressed Switch file by running ``nsz -V`` on it.

        NSZ is an NCZ block container, not a raw zstd stream, so a generic zstd
        test can't validate it. nsz's own verify re-derives and checks the
        content hashes, which is why this also needs keys.
        """
        if not os.path.exists(file_path):
            yield {"type": "error", "valid": False, "message": "File not found"}
            return
        try:
            is_empty = os.path.getsize(file_path) == 0
        except OSError as e:
            yield {"type": "error", "valid": False, "message": f"Error reading file: {e}"}
            return
        if is_empty:
            yield {"type": "error", "valid": False, "message": "File is empty"}
            return
        ext = Path(file_path).suffix.lower()
        if ext not in NSZ_DECOMPRESS_EXTENSIONS:
            yield {"type": "error", "valid": False, "message": f"Invalid extension: {ext}"}
            return
        if not self.keys_available():
            yield {
                "type": "error",
                "valid": False,
                "message": (
                    "nsz needs prod.keys to verify Switch content. Set "
                    "SWITCH_KEYS to a directory with prod.keys, or place it at "
                    "~/.switch/prod.keys."
                ),
            }
            return

        try:
            with self._keys_home() as env:
                process = await asyncio.create_subprocess_exec(  # nosemgrep
                    self.nsz_path, "-V", file_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                )
                self._runner.track_pid(process.pid)
                overall_timeout = verify_timeout("nsz")
                try:
                    yield {"type": "progress", "progress": 0, "message": "Verifying integrity..."}
                    try:
                        if overall_timeout > 0:
                            stdout, _ = await asyncio.wait_for(
                                process.communicate(), timeout=overall_timeout,
                            )
                        else:
                            stdout, _ = await process.communicate()
                    except asyncio.TimeoutError:
                        # Process is killed in the finally block below.
                        yield {
                            "type": "error",
                            "valid": False,
                            "message": f"Verification timed out after {overall_timeout}s",
                        }
                        return
                    output = (stdout or b"").decode("utf-8", errors="replace").strip()
                    if process.returncode == 0:
                        yield {
                            "type": "progress",
                            "progress": 100,
                            "message": "Integrity check passed",
                        }
                        yield {
                            "type": "complete",
                            "valid": True,
                            "message": "File verified successfully",
                        }
                    else:
                        tail = (
                            "\n".join(output.splitlines()[-5:])
                            if output else "verification failed"
                        )
                        yield {
                            "type": "error",
                            "valid": False,
                            "message": f"Integrity check failed: {tail}",
                        }
                finally:
                    if process.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                        with contextlib.suppress(Exception):
                            await process.wait()
                    self._runner.untrack_pid(process.pid)
        except Exception as e:
            logger.exception("Error during Switch verification: %s", e)
            yield {"type": "error", "valid": False, "message": f"Verification error: {e}"}


# Global service instance
nsz_service = NszService()
