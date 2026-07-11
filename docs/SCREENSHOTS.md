# Screenshot automation

The Web UI screenshots in `docs/screenshots/` — the ones embedded in
[`README.md`](../README.md) — are generated automatically with
[shot-scraper](https://shot-scraper.datasette.io/) rather than captured by hand.
This keeps them in sync with the current UI and its light/dark themes.

## Pieces

| File | Role |
|------|------|
| [`shots.yml`](../shots.yml) | shot-scraper definition of every screenshot: URL (via the app's hash router), viewport size, theme, and any interaction (selecting a volume, ticking files). |
| [`scripts/make_screenshot_fixture.sh`](../scripts/make_screenshot_fixture.sh) | Builds a throwaway "volume" of tiny placeholder game files so the file browser and convert panels have realistic content to show. No real game images are committed or converted. |
| [`scripts/take_screenshots.py`](../scripts/take_screenshots.py) | Driver that captures each `shots.yml` entry in its own browser (see [Why not `shot-scraper multi`](#why-not-shot-scraper-multi)). |
| [`.github/workflows/screenshots.yml`](../.github/workflows/screenshots.yml) | CI workflow that wires it all together and commits the results back. |

## What the workflow does

1. Builds the Svelte frontend (`npm run build` → `static/`).
2. Creates the fixture volume and boots the FastAPI backend against it, serving
   the built UI on `http://127.0.0.1:8099`.
3. Runs `python scripts/take_screenshots.py shots.yml`, which drives
   shot-scraper over every surface in light and dark.
4. Losslessly optimises the PNGs with [Oxipng](https://github.com/shssoichiro/oxipng)
   (`oxipng -o 4 -i 0 --strip safe`).
5. Commits any changes under `docs/screenshots/`.

It runs on manual dispatch and on pushes that touch the UI (see the workflow's
`paths:` list). Changes under `docs/screenshots/**` are deliberately not a
trigger, so the commit the workflow makes cannot trigger itself.

## Running it locally

```bash
# 1. Build the frontend
npm ci
npm run build

# 2. Install the screenshot tooling
pip install -r requirements.txt shot-scraper
shot-scraper install chromium

# 3. Start the app against a fixture library
scripts/make_screenshot_fixture.sh /tmp/shot-fixture
COMPRESSATORIUM_MOUNT_ROOT=/tmp/shot-fixture \
  STATIC_DIR="$PWD/static" \
  CHD_MODE=webui \
  PYTHONPATH="$PWD/app" \
  python -m uvicorn main:app --host 127.0.0.1 --port 8099 &

# 4. Capture and optimise
python scripts/take_screenshots.py shots.yml
oxipng -o 4 -i 0 --strip safe docs/screenshots/*.png
```

## How theming works

The UI stores its light/dark preference in the `theme-preference` localStorage
key (via [mode-watcher](https://github.com/svecosystem/mode-watcher)). Each shot
sets that key and dispatches a synthetic `storage` event in its `javascript`
block, which flips the theme reactively before the screenshot is taken. That is
why every surface can be captured as a light / dark pair from the same running
server.

## Why not `shot-scraper multi`

The idiomatic command is `shot-scraper multi shots.yml`, and `shots.yml` is kept
in that exact format so it stays usable that way for any UI without a streaming
connection. Compressatorium's UI, though, keeps a long-lived Server-Sent Events
connection open (`/api/jobs/events`) for the live job queue. `multi` reuses a
single browser page for every shot, so after a handful of navigations the
lingering SSE sockets exhaust Chromium's per-host connection limit and later
navigations hang waiting for a `load` event that never fires. Capturing each
shot in a fresh browser — what `scripts/take_screenshots.py` does — avoids that
entirely.
