"""Async file-level SHA1 computation for DAT hash matching."""

from __future__ import annotations

import hashlib

from fastapi.concurrency import run_in_threadpool

_CHUNK_SIZE = 65536  # 64KB


async def compute_file_sha1(file_path: str) -> str:
    """Compute SHA1 hash of an entire file.

    Runs in a thread pool to avoid blocking the event loop.
    Returns lowercase hex digest.
    """
    return await run_in_threadpool(_sha1_sync, file_path)


def compute_file_sha1_sync(file_path: str) -> str:
    """Blocking SHA-1 for callers that bring their own offload strategy.

    ``compute_file_sha1`` hands the work to the shared threadpool, which is the
    wrong home for a whole-file read of a path on storage that may stop
    answering (AGENTS.md: such a read must never occupy a pooled worker). A
    caller that runs this through ``run_detached`` gets the same digest on a
    throwaway thread instead.
    """
    return _sha1_sync(file_path)


def _sha1_sync(file_path: str) -> str:
    """Synchronous SHA1 computation."""
    h = hashlib.sha1(usedforsecurity=False)  # nosec # nosemgrep: insecure-hash-algorithm-sha1  # pylint: disable=unexpected-keyword-arg
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


async def compute_file_sha1_with_size(file_path: str) -> tuple[str, int]:
    """Compute SHA1 and return file size."""
    return await run_in_threadpool(_sha1_with_size_sync, file_path)


def _sha1_with_size_sync(file_path: str) -> tuple[str, int]:
    """Synchronous SHA1 + size computation."""
    h = hashlib.sha1(usedforsecurity=False)  # nosec # nosemgrep: insecure-hash-algorithm-sha1  # pylint: disable=unexpected-keyword-arg
    size = 0
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size
