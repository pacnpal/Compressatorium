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

# CHDMAN: a .cue sheet referencing a .bin data track.
printf 'FILE "game.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n' \
  > "$root/Arcade CHD/street-fighter.cue"
head -c 2359296 /dev/zero > "$root/Arcade CHD/game.bin"

# Dolphin: a GameCube/Wii disc image plus an already-compressed RVZ.
head -c 4194304 /dev/zero > "$root/GameCube/mario-kart.iso"
head -c 1048576 /dev/zero > "$root/GameCube/zelda-wind-waker.rvz"

# 3DS: a couple of ROMs.
head -c 2097152 /dev/zero > "$root/Nintendo 3DS/pokemon-x.3ds"
head -c 2097152 /dev/zero > "$root/Nintendo 3DS/mario-3d-land.cci"

echo "Fixture volume created under: $root"
