#!/usr/bin/env python3
"""Capture the Web UI screenshots described in a shot-scraper ``shots.yml``.

This is a thin, dependency-light driver around ``shot-scraper shot`` that takes
each entry in the YAML file in its **own** browser process. The Compressatorium
UI holds a long-lived Server-Sent Events connection open (``/api/jobs/events``)
for the live job queue. ``shot-scraper multi`` reuses a single browser page for
every shot, so after a handful of navigations the lingering SSE sockets exhaust
Chromium's per-host connection limit and later navigations never fire ``load``.
Giving every shot a fresh browser sidesteps that entirely.

The ``shots.yml`` format is the standard shot-scraper one, so the same file also
works with ``shot-scraper multi shots.yml`` against any UI that does not keep a
streaming connection open. Only a subset of keys is honoured here — the ones the
project's shots use: ``url``, ``output``, ``width``, ``height``, ``wait`` and
``javascript``.

Usage:
    python scripts/take_screenshots.py shots.yml [--timeout MS]
"""

from __future__ import annotations

import argparse
import subprocess  # nosec B404 -- fixed argv lists, shell=False; see build_command
import sys
from pathlib import Path

import yaml


def build_command(shot: dict, timeout: int) -> list[str]:
    """Turn one shots.yml entry into a ``shot-scraper shot`` argv list."""
    url = shot.get("url")
    output = shot.get("output")
    if not url or not output:
        raise ValueError(f"shot is missing url/output: {shot!r}")

    cmd = ["shot-scraper", "shot", url, "-o", output, "--timeout", str(timeout)]
    if "width" in shot:
        cmd += ["-w", str(shot["width"])]
    if "height" in shot:
        cmd += ["-h", str(shot["height"])]
    if "wait" in shot:
        cmd += ["--wait", str(shot["wait"])]
    if shot.get("javascript"):
        cmd += ["--javascript", shot["javascript"]]
    return cmd


def main() -> int:
    """Parse arguments, capture every shot in ``config``, and return an exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="Path to shots.yml")
    parser.add_argument(
        "--timeout",
        type=int,
        default=15000,
        help="Per-shot navigation timeout in milliseconds (default: 15000)",
    )
    args = parser.parse_args()

    shots = yaml.safe_load(args.config.read_text())
    if not isinstance(shots, list):
        print(f"error: {args.config} must contain a YAML list of shots", file=sys.stderr)
        return 2

    failures = []
    for index, shot in enumerate(shots, start=1):
        output = shot.get("output", "?")
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        print(f"[{index}/{len(shots)}] {output}", flush=True)
        try:
            # cmd is a fixed argv list built from the trusted shots.yml and run with
            # shell=False, so the subprocess SAST warnings are false positives.
            cmd = build_command(shot, args.timeout)
            result = subprocess.run(cmd, check=False)  # nosec B603  # nosemgrep
        except FileNotFoundError:
            print(
                "error: 'shot-scraper' not found on PATH. Install it with "
                "'pip install shot-scraper' and run 'shot-scraper install'.",
                file=sys.stderr,
            )
            return 127
        except ValueError as exc:
            failures.append(output)
            print(f"  !! invalid shot: {exc}", file=sys.stderr, flush=True)
            continue
        if result.returncode != 0:
            failures.append(output)
            print(f"  !! failed ({result.returncode}): {output}", file=sys.stderr, flush=True)

    if failures:
        print(f"\n{len(failures)} shot(s) failed:", file=sys.stderr)
        for output in failures:
            print(f"  - {output}", file=sys.stderr)
        return 1

    print(f"\nAll {len(shots)} screenshots written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
