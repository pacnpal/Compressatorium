# Agent runbook

A quick execution guide for agents working in this repo. Treat it as an operational checklist, not product docs. Run the commands from the repo root.

## Canonical references

- `README.md`
- `docs/RELEASE_NOTES.md`
- `docs/ADDING_PLATFORMS_AND_TOOLS.md` — **start here to add a conversion tool
  or a new platform/mode**: the step-by-step walkthrough, the exhaustive
  file inventory, and a copy/paste checklist
- `docs/DESIGN_tool_plugin_architecture.md` — *why* the plugin contract and
  shared infrastructure look the way they do
- `docs/DOCKER-COMPOSE.md`
- `docs/DEPLOYMENT.md`
- `.github/workflows/docker-image.yml`

## Design principles

- **Modularity is key.** If logic can be shared between tools, it must be — put
  it on the `ToolPlugin`/`BaseTool` contract or in shared infrastructure
  (`services/subprocess_runner.py`, `services/tools/registry.py`), never
  copy-pasted per tool. Prefer registry-driven behavior over branching on tool
  identity or hard-coded extensions. Examples of the shared seams to extend
  before reaching for per-tool code: `SubprocessRunner.run` (streaming) and
  `SubprocessRunner.run_capture` (one-shot capture with cancel/timeout),
  `ToolPlugin.embedded_hashes` (DAT-match content hashes), and the
  `ToolRegistry` extension helpers (`output_extensions` / `verify_extensions` /
  `scannable_extensions`).
- **Document shared machinery in the proper doc.** When you add or change a
  shared seam, update `docs/DESIGN_tool_plugin_architecture.md` (the plugin
  contract + shared infrastructure) and note user-facing effects in
  `docs/RELEASE_NOTES.md`.

## Current workflows

### 1. Local dev (Web UI)

The frontend is a Svelte 5 + Vite single-page app under `src/` (see README, "Frontend Development"). The backend is FastAPI under `app/`. The build lands in `static/` and FastAPI serves it through the `/static` mount.

**Runtime libraries already in use (don't reinvent these):**

- Icons: `@lucide/svelte`. `import Sun from '@lucide/svelte/icons/sun'`, use as `<Sun size={16} />`.
- Toasts: `svelte-sonner`. `import { toast } from 'svelte-sonner'`, then `toast.success(msg)` / `toast.error(msg)` / `toast.promise(p, {...})` from any store or component.
- Theme (light/dark/system): `mode-watcher`. `import { setMode, userPrefersMode, mode } from 'mode-watcher'`. Don't roll your own theme state in `ui.svelte.js`.
- Headless accessibility primitives: `bits-ui`. Pull Dialog, DropdownMenu, ContextMenu, and Tooltip from `bits-ui` instead of building modal/menu/tooltip components from scratch. Per-row file actions use a `bits-ui` DropdownMenu in `src/lib/components/panels/RowActionsMenu.svelte`. Reuse it as the pattern for new menus (style with `:global(...)` selectors keyed off `[data-highlighted]` / `[data-disabled]`).

**Where conversion config lives:**

- Mode dropdown: `panels/ModeSelect.svelte`. Options come from `registry.modesByGroup(toolId)`, so adding a mode means editing `registry.js` and nothing else.
- Compression UI: `panels/CompressionPicker.svelte`. The style comes from `tool.compressionStyle` (`'multi'` / `'single-with-level'` / `'none'`), codecs from `tool.compressionCodecs`, and the level range from `tool.compressionLevelRange`.
- Output dir, delete-on-verify, submit: `panels/ConvertPanel.svelte`. Delete-on-verify blocks submit when any selected source is unverified, the same invariant the backend enforces.

Production-style run (FastAPI serves the prebuilt SPA):

```bash
npm install && npm run build      # one-time, or when src/ changes
./run_dev.sh                       # loads .env.local, bootstraps .venv, starts uvicorn
```

Hot-reload dev loop (two terminals):

```bash
# Terminal 1: backend
./run_dev.sh                       # uvicorn on :8080

# Terminal 2: Vite dev server with HMR
npm run dev                        # http://localhost:5173 (proxies /api and /health to :8080)
```

App URL: `http://localhost:8080` (prod-style) or `http://localhost:5173` (Vite HMR). If `COMPRESSATORIUM_VOLUMES` is unset, volumes are auto-discovered under `COMPRESSATORIUM_MOUNT_ROOT/*`. All UI work goes in `src/`.

### 2. Tests

```bash
pytest -q tests
```

Targeted suites:

```bash
pytest -q tests/test_mode_parity_fixes.py
pytest -q tests/test_volume_discovery.py
```

### 3. Docker runtime

```bash
docker-compose up -d                                        # single-volume Web UI
docker-compose -f docker-compose.multi-volume.yml up -d     # multi-volume Web UI
docker-compose -f docker-compose.cli.yml up                 # CLI batch mode
```

Common operations:

```bash
docker-compose ps
docker-compose logs -f
docker-compose restart
docker-compose down
```

### 4. Queue / admin API

Health and version:

```bash
curl -s http://localhost:8080/health
curl -s http://localhost:8080/api/version
```

Cancel all queued and active jobs (needs the confirmation header):

```bash
curl -s -X POST \
  -H 'X-CHD-Action-Confirm: cancel-all-jobs' \
  http://localhost:8080/api/jobs/cancel-all
```

Clear completed, failed, and cancelled jobs (needs the confirmation header):

```bash
curl -s -X DELETE \
  -H 'X-CHD-Action-Confirm: clear-completed-jobs' \
  http://localhost:8080/api/jobs/completed
```

Start a metadata scan and poll status:

```bash
curl -s -X POST http://localhost:8080/api/chd-metadata/scan
curl -s http://localhost:8080/api/chd-metadata/scan/status
```

### 5. Version and release

The version lives in `package.json` (`version`). There is no `.version` file. A release means bumping `package.json`, updating `docs/RELEASE_NOTES.md` (Keep a Changelog format), and publishing a GitHub Release tagged `vX.Y.Z`. Publishing the release is what triggers the image build.

Schema changes use `scripts/new_migration.sh` (see README, "Schema changes"). That's the only script in `scripts/`.

### 6. CI (`docker-image.yml`)

- Name: `Build and Push Docker Image`.
- Trigger: a published GitHub Release only. There is no push trigger, PR trigger, or manual dispatch.
- It lints the Dockerfile with Hadolint, builds `linux/amd64` and `linux/arm64`, and pushes to Docker Hub and GHCR. The image tags, including the semver tags, come from the release tag.

Watch the latest run:

```bash
gh run list --workflow docker-image.yml --limit 5
gh run watch "$(gh run list --workflow docker-image.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
```

## Command notes

- Preferred env names: `COMPRESSATORIUM_MOUNT_ROOT` and `COMPRESSATORIUM_VOLUMES`.
- Legacy aliases `CHD_MOUNT_ROOT` and `CHD_VOLUMES` still work.
- Keep `MAX_CONCURRENT_JOBS=1` unless you've checked the host can handle more.
