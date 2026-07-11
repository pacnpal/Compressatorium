#!/usr/bin/env bash
#
# Build a throwaway fixture volume tree for the screenshot workflow.
#
# The Web UI only has anything interesting to show when at least one volume is
# mounted with files a tool can convert. Rather than ship real (copyrighted)
# game images, this creates a handful of tiny placeholder files with the right
# extensions so the file browser, tool pickers and convert panels render with
# realistic content. The files are never actually converted — the screenshot
# run captures the UI, it does not start jobs.
#
# Usage:
#   scripts/make_screenshot_fixture.sh <target-dir>
#
# The directory named by <target-dir> is used as COMPRESSATORIUM_MOUNT_ROOT; each
# immediate subdirectory becomes a volume in the UI.

set -euo pipefail

root="${1:?usage: make_screenshot_fixture.sh <target-dir>}"

mkdir -p \
  "$root/Arcade CHD" \
  "$root/GameCube" \
  "$root/Nintendo 3DS"

# Placeholder file sizes are in KiB. `dd` is used rather than `head -c` because
# the latter's byte count is a GNU extension the BSD `head` on macOS lacks.
placeholder() { dd if=/dev/zero of="$1" bs=1024 count="$2" 2>/dev/null; }

# CHDMAN: a .cue sheet referencing a .bin data track.
printf 'FILE "game.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n' \
  > "$root/Arcade CHD/street-fighter.cue"
placeholder "$root/Arcade CHD/game.bin" 2304

# Dolphin: a GameCube/Wii disc image plus an already-compressed RVZ.
placeholder "$root/GameCube/mario-kart.iso" 4096
placeholder "$root/GameCube/zelda-wind-waker.rvz" 1024

# 3DS: a couple of ROMs.
placeholder "$root/Nintendo 3DS/pokemon-x.3ds" 2048
placeholder "$root/Nintendo 3DS/mario-3d-land.cci" 2048

echo "Fixture volume created under: $root"
