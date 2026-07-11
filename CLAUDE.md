# Compressatorium — game-image converter (web UI + headless CLI)

A FastAPI + Svelte app that wraps seven conversion tools — CHDMAN, dolphin-tool,
z3ds, nsz, maxcso, 7z, and makeps3iso — behind one tool-plugin architecture.
Operational runbook for agents: `AGENTS.md`. Tool plugins and shared
infrastructure: `docs/DESIGN_tool_plugin_architecture.md`.

## Commands

- Install (frontend): `npm install`
- Build SPA: `npm run build` (Vite → `static/`, served by FastAPI at `/static`)
- Run (prod-style): `./run_dev.sh` (bootstraps `.venv`, uvicorn on `:8080`)
- Dev HMR: `npm run dev` (Vite on `:5173`, proxies `/api` + `/health` to `:8080`)
- Tests: `PYTHONPATH=app python -m pytest -q`
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
