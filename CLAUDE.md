# Compressatorium — game-image converter (web UI + headless CLI)

A FastAPI + Svelte app that wraps eight conversion tools — CHDMAN, dolphin-tool,
z3ds, nsz, maxcso, 7z, makeps3iso, and nkit2iso — behind one tool-plugin
architecture.
Operational runbook for agents: `AGENTS.md`. Tool plugins and shared
infrastructure: `docs/DESIGN_tool_plugin_architecture.md`. **Adding a tool or a
platform/mode: `docs/ADDING_PLATFORMS_AND_TOOLS.md`** — the step-by-step
walkthrough, file inventory, and checklist.

## Commands

- Install (frontend): `npm install`
- Build SPA: `npm run build` (Vite → `static/`, served by FastAPI at `/static`)
- Run (prod-style): `./run_dev.sh` (bootstraps `.venv`, uvicorn on `:8080`)
- Dev HMR: `npm run dev` (Vite on `:5173`, proxies `/api` + `/health` to `:8080`)
- Tests: `PYTHONPATH=app python -m pytest -q tests`
- Lint (Python): `ruff check .` (max line length 100, matching `.pylintrc` / Codacy)
- Lint (JS/Svelte): `npm run lint` (eslint)
- Version lives in `package.json`; releases also update `docs/RELEASE_NOTES.md`.

## Architecture

- Backend: FastAPI under `app/`. Intra-project imports are written
  `from services.x import y`, so `app/` must be on `PYTHONPATH` (why tests and
  `run_dev.sh` set it).
- Frontend: Svelte 5 + Vite SPA under `src/`; the build lands in `static/`, which
  FastAPI serves via the `/static` mount. All UI work goes in `src/`.
- Each conversion tool is a `ToolPlugin`/`BaseTool` plugin over shared infra in
  `app/services/`; see the design doc for the contract.

## Standing rules

- **Modularity is key.** If something can be shared between tools, it should be.
  Put shared logic on the `ToolPlugin`/`BaseTool` contract or in shared
  infrastructure (`services/subprocess_runner.py`, `services/tools/registry.py`)
  rather than copy-pasting per tool, and prefer registry-driven behavior over
  branching on tool identity or hard-coded extensions.
- **Document it.** When you add or change shared machinery, document it in the
  proper doc under `docs/` — the plugin contract / shared infrastructure in
  `docs/DESIGN_tool_plugin_architecture.md`, and user-facing changes in
  `docs/RELEASE_NOTES.md`.
- **Keep the screenshots current.** The Web UI screenshots in `docs/screenshots/`
  (embedded in `README.md`) are generated from `shots.yml` by shot-scraper, not
  captured by hand. When a UI change alters an existing surface, refresh them —
  push (the **Take screenshots** workflow, `.github/workflows/screenshots.yml`,
  regenerates and commits them) or run it locally per `docs/SCREENSHOTS.md`. When
  you add or meaningfully change a surface, update the list itself: add or edit
  the matching entry in `shots.yml` (and reference the new image from the README /
  docs) so the automation captures it. See `docs/SCREENSHOTS.md` for the setup.
