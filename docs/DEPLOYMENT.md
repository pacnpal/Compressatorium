# Deployment guide

How to run Compressatorium in production, what the image already handles, and what you should add yourself.

This file started as a one-time readiness audit (2026-01-20). Several of its old recommendations are now part of the image (`.dockerignore`, a multi-stage build, resource limits in the compose files), so what follows is the current picture, not the original audit.

## Security

**Web UI/API authentication.** Authentication is off by default and gated behind `COMPRESSATORIUM_ENABLE_AUTH`. Set `COMPRESSATORIUM_ENABLE_AUTH=true` to require a token for the Web UI and `/api` routes, then either set `COMPRESSATORIUM_AUTH_TOKEN` (or `CHD_AUTH_TOKEN`) to a strong value or let startup generate `/config/auth_token` and use that as the HTTP Basic password for user `admin`. `/health` stays unauthenticated for Docker health checks. The token is accepted only in request headers (HTTP Basic, `Authorization: Bearer`, or `X-Compressatorium-Token`) — never in the URL query string — and state-changing requests are restricted to same-origin callers so cached browser Basic credentials cannot be replayed cross-site (CSRF). Leave auth disabled only when another access-control layer protects the service.

**Path traversal.** Every file operation goes through `is_within_configured_volumes()` and `ensure_path_within_volumes()` in `app/utils/path_utils.py`. Paths are resolved with `Path.resolve()` and checked against the configured volumes, and symlink loops (ELOOP) are rejected outright instead of slipping through. Used in `files.py` and `convert.py`.

**No hardcoded secrets.** No passwords, API keys, or tokens in the code. Sensitive config comes from environment variables.

**Command injection.** The chdman and subprocess services call `asyncio.create_subprocess_exec()` with an argument list, never `shell=True`. See `app/services/chdman.py` and `app/services/subprocess_runner.py`.

**Privilege drop.** `entrypoint.sh` starts as root, optionally remaps `PUID`/`PGID`, then drops to the `converter` user with `gosu` before the app runs.

**Input validation.** File paths, custom output directories, and archive extraction paths are all validated against the configured volumes before use.

## Health check

The image ships a `HEALTHCHECK` (30s interval, 10s timeout, 10s start period, 3 retries) that hits `/health`. In CLI mode it exits 0 so a batch run isn't marked unhealthy. `/health` returns `{"status": "healthy", "version": "..."}`.

## Environment and volumes

The full environment variable reference lives in [README.md](../README.md#environment-variables). The ones that matter most for a deployment:

| Variable | Default | Purpose |
|----------|---------|---------|
| `CHD_MODE` | `webui` | Web UI or CLI batch mode |
| `COMPRESSATORIUM_MOUNT_ROOT` | `/data` | Startup scan root; `/data/*` is auto-registered |
| `COMPRESSATORIUM_VOLUMES` | (unset) | Explicit comma-separated volume paths; skips the scan |
| `PUID` / `PGID` | `999` | Remap the `converter` user/group to match host ownership |
| `CHD_DATA_DIR` | `/config` | Persistent data; the SQLite DB and generated auth token live here |
| `COMPRESSATORIUM_ENABLE_AUTH` | `false` | Enable token auth for the Web UI and `/api`. Off by default; set `true` to require a token. |
| `COMPRESSATORIUM_AUTH_TOKEN` / `CHD_AUTH_TOKEN` | generated in `/config/auth_token` | Web UI/API password (when auth is enabled). Use it via HTTP Basic, bearer token, or `X-Compressatorium-Token`. |
| `COMPRESSATORIUM_AUTH_USERNAME` | `admin` | HTTP Basic username for the Web UI (when auth is enabled). |
| `MAX_CONCURRENT_JOBS` | `1` | Parallel conversion jobs |
| `COMPRESSATORIUM_TOOL_NICE` / `COMPRESSATORIUM_TOOL_IOPRIO_CLASS` / `COMPRESSATORIUM_TOOL_IOPRIO_LEVEL` | `10` / `2` / `6` | CPU and I/O priority for **all** tools (chdman, Dolphin, 3DS, Switch, PSP/PS2 CSO, handheld ROM, PS3 ISO). Legacy aliases `CHD_CHDMAN_NICE` / `CHD_CHDMAN_IOPRIO_CLASS` / `CHD_CHDMAN_IOPRIO_LEVEL` still work. Also `COMPRESSATORIUM_TOOL_INFO_TIMEOUT` (chdman/Dolphin info subprocess) and `COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT` (verify across all tools). Per-tool overrides: `COMPRESSATORIUM_<TOOL>_NICE` / `_IOPRIO_CLASS` / `_IOPRIO_LEVEL` / `_VERIFY_TIMEOUT` for `<TOOL>` = `CHDMAN`, `DOLPHIN_TOOL`, `NSZ`, `Z3DS`, `MAXCSO`, `ROMZ`, `MAKEPS3ISO` (`MAKEPS3ISO` has no `_VERIFY_TIMEOUT`, since makeps3iso has no verify step); `_INFO_TIMEOUT` only for `CHDMAN` and `DOLPHIN_TOOL`. |
| `SWITCH_KEYS` | (unset) | Directory holding your own Switch `prod.keys`. Source of truth for the Switch (nsz) tool; mount it read-only. When unset, the app best-effort checks `~/.switch` and your mounted volumes. No keys ship with the image. |
| `STATIC_DIR` | `/static` | Static web assets |

**Switch keys.** The Switch (nsz) tool needs your own `prod.keys`, dumped from a
console you own. Mount the directory holding it read-only and set `SWITCH_KEYS`
to that directory. When unset, the app does a cheap, non-blocking best-effort
search of `~/.switch` and your mounted volumes at runtime. The app ships no keys,
never logs them, and git-ignores key file names so they can't be committed or
baked into an image. The file only needs to be readable by uid 999.

Volume precedence:
- If `COMPRESSATORIUM_VOLUMES` is set, that explicit list wins.
- Otherwise the app scans `COMPRESSATORIUM_MOUNT_ROOT/*` once at startup.
- Restart the container after adding or removing mounts so the discovered volumes refresh.

## Tuning

**Where to change it.** Set environment variables in your runtime (Compose, Unraid, or `docker run`) and apply CPU/memory limits at the container level.

**Starting points**
- **Low to medium hosts (≤16 GB RAM, HDD or parity-backed arrays):** keep `MAX_CONCURRENT_JOBS=1`, `COMPRESSATORIUM_TOOL_NICE=10`, `COMPRESSATORIUM_TOOL_IOPRIO_CLASS=2`, `COMPRESSATORIUM_TOOL_IOPRIO_LEVEL=6`. Set a memory limit of 8 to 12 GB.
- **Faster hosts (32+ GB RAM, SSD cache):** try `MAX_CONCURRENT_JOBS=2` and 16 to 24 GB. Raise I/O priority only if the host stays responsive.
- **If the host gets sluggish:** lower `MAX_CONCURRENT_JOBS`, raise `COMPRESSATORIUM_TOOL_NICE`, or set `COMPRESSATORIUM_TOOL_IOPRIO_CLASS=3` (idle) with `COMPRESSATORIUM_TOOL_IOPRIO_LEVEL=7`.

**Host tips**
- Put `CHD_TEMP_DIR` and CHD output on SSD or cache to cut array contention.
- Don't run other heavy services during a conversion.
- Always set container CPU/memory limits on a shared host.

## What's already in the image

- `.dockerignore` at the repo root.
- Multi-stage `Dockerfile`: a Debian `builder`, a `node:lts-slim` `frontend-builder` for the Svelte UI, and a slim runtime with no Node.
- Conservative CPU/memory limits in the default compose files.
- Async job queue with configurable concurrency, SSE progress, job cancellation, and per-job file locks.
- Archive support (ZIP, 7z, RAR) through `py7zr`, `rarfile`, and the `p7zip-full` / `unrar-free` system packages, with temp extraction and cleanup.
- Auto-generated API docs at `/docs` (OpenAPI/Swagger).
- CI/CD in `.github/workflows/docker-image.yml`: builds `linux/amd64` and `linux/arm64` and pushes to Docker Hub and GHCR. It triggers on a published GitHub Release only (no push or tag trigger); the image tags come from the release tag.

## What you should add yourself

Compressatorium ships built-in token authentication (opt-in) but doesn't terminate TLS. For anything reachable beyond your LAN:

- Put it behind a reverse proxy (nginx or Traefik) with HTTPS.
- Enable built-in auth (`COMPRESSATORIUM_ENABLE_AUTH=true`) and/or add an authentication layer at the proxy.
- Add security headers (`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`) and rate limiting at the proxy.
- There are no CORS restrictions by default. Add them at the proxy if the API needs protecting.
- Set up monitoring and log aggregation. Logs go to stdout, or to `LOG_PATH` if you set it.

## Directory structure

```
.
├── .github/workflows/      # CI/CD
├── app/
│   ├── routes/             # API endpoints
│   ├── services/           # conversion tools, stores, job manager
│   ├── utils/              # path validation, delete plans
│   ├── config.py
│   ├── main.py             # FastAPI app
│   └── models.py
├── src/                    # Svelte 5 + Vite frontend source
├── static/                 # built UI: index.html + assets/ + images/
├── migrations/             # Alembic migrations
├── tests/
├── Dockerfile
├── docker-compose*.yml
├── entrypoint.sh
└── requirements.txt
```

## Deployment checklist

**Before**
- [ ] Set the environment variables you need.
- [ ] Create the game library directories on the host.
- [ ] Make sure there's enough disk space for conversions.
- [ ] Mount your `/data/*` directories, or set an explicit `COMPRESSATORIUM_VOLUMES`.
- [ ] Pick a `MAX_CONCURRENT_JOBS` that fits the host.

**Deploy**
- [ ] Pull or build the image.
- [ ] `docker-compose up -d`.
- [ ] `docker-compose ps` and confirm the container is healthy.
- [ ] Open http://localhost:8080.
- [ ] `curl http://localhost:8080/health`.

**After**
- [ ] Browse files in the Web UI.
- [ ] Convert a small test file and confirm the output lands.
- [ ] Check logs: `docker-compose logs -f`.
- [ ] Watch resource use: `docker stats`.
- [ ] Set up backups of converted files if you need them.
- [ ] Put a reverse proxy with HTTPS in front if it's internet-facing.

## Bottom line

The app is safe to run on development and staging right away. For production, enable built-in auth (`COMPRESSATORIUM_ENABLE_AUTH=true`), add a reverse proxy with HTTPS, set resource limits, and decide on monitoring and backups. Nothing in the codebase blocks a production deploy. The gaps are the usual operational ones it leaves to you on purpose.
