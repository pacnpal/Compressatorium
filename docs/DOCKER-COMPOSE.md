# Docker Compose Quick Reference

This is a quick reference for the available Docker Compose configurations.

## Available Configurations

### 1. Single Volume Setup (Basic)
**File:** `docker-compose.yml`

Start with a single game directory:
```bash
docker-compose up -d
```

**Default configuration:**
- Port: 127.0.0.1:8080:8080 (loopback-only by default)
- Volume: `./games` → `/data/games`
- Temp: `/config/temp` (inside `./config`)
- UID/GID remap: optional via `PUID`/`PGID` (defaults `999:999`)
- Mode: Web UI
- Concurrent jobs: 1

**Use case:** A top-level directory with games in subfolders. The Web UI browses every subdirectory, so you can convert files anywhere in the tree.

---

### 2. Multiple Volumes (Advanced)
**File:** `docker-compose.multi-volume.yml`

For organizing different game libraries as separate mount points:
```bash
docker-compose -f docker-compose.multi-volume.yml up -d
```

**Configured volumes:**
- `./games/dreamcast` → `/data/dreamcast`
- `./games/psp` → `/data/psp`
- `./games/ps1` → `/data/ps1`
- `./games/ps2` → `/data/ps2`
- Temp: `/config/temp` (inside `./config`)

**Use case:** Games kept in separate directories, like different drives or network shares. Each mount shows up as its own volume in the Web UI.

**Customization:**
Edit the file to add/remove volume mounts under `/data/*`.
Use `COMPRESSATORIUM_VOLUMES` only when you want an explicit comma-separated list and to skip startup scanning.

---

### 3. CLI Mode (Batch Processing)
**File:** `docker-compose.cli.yml`

For automated/headless conversion:
```bash
docker-compose -f docker-compose.cli.yml up
```

**Behavior:**
- Converts top-level `.gdi`, `.iso`, `.cue` files in mounted volumes
- Exits after completion (no restart)
- No web interface
- CHDMAN-only (no Dolphin modes in CLI)

**Note:** CLI mode only processes files in the top level of each mounted volume. For files in subdirectories, use the Web UI mode which supports recursive directory browsing.

**To change conversion mode:**
Edit `CHDMAN_MODE` in the file:
- `createcd` for CD-ROM (Dreamcast, PS1, etc.)
- `createdvd` for DVD-ROM (PSP, PS2, etc.)

### 4. Alongside RomM (library integration)

To use the **RomM** view, run Compressatorium beside your
[RomM](https://romm.app) container and give it the **same library bind mount**.
Compressatorium reads RomM's catalog over the API but the ROM files through the
filesystem, so no disc image is ever streamed over HTTP.

```yaml
services:
  romm:
    image: rommapp/romm:latest
    volumes:
      - /path/to/library:/romm/library     # RomM's library
    # ...

  compressatorium:
    image: pacnpal/compressatorium:latest
    volumes:
      - /path/to/library:/data/library     # the SAME host path
    environment:
      - ROMM_URL=http://romm:8080
      - ROMM_TOKEN=rmm_...                 # see below
      - ROMM_LIBRARY_ROOT=/data/library    # where RomM's library is mounted HERE
    ports:
      - "8080:8080"
```

> **You can skip all of this and configure RomM in the app instead.** These
> variables only seed the first run; **RomM → Settings** in the web UI sets the
> same values, saves them, and applies them without a restart. They are here for
> operators who prefer a declarative compose file.

**Getting a token:** in RomM, go to *Administration → Client API Tokens* and
create one. Client API tokens (`rmm_` + 64 hex characters) are per-user, do not
expire unless you set an expiry, and carry only the scopes you grant. Give it
`platforms.read` and `roms.read`; add `roms.write` if you want Compressatorium to
re-apply metadata after converting to a format RomM cannot hash-match (RVZ, CSO,
NSZ, WUX, Z3DS).

**Two things to get right:**

- **`ROMM_LIBRARY_ROOT` is the path *inside the Compressatorium container*,** not
  the path RomM sees. RomM reports each ROM's location relative to its own
  library root, and Compressatorium joins that onto `ROMM_LIBRARY_ROOT`. It must
  also be inside a configured Compressatorium volume, or the ROMs are skipped.
- **Match the UID/GID** the two containers run as. If they differ, RomM cannot
  read the files Compressatorium writes (and its cleanup tasks cannot manage
  them).

**Remote RomM:** there is no separate "remote" mode and none is needed — mount
the remote library over NFS/SMB/rclone and point `ROMM_LIBRARY_ROOT` at the
mount. Note RomM advises against enabling its own filesystem watcher
(`ENABLE_RESCAN_ON_FILESYSTEM_CHANGE`) on SMB/rclone mounts; scan from RomM's UI
instead after converting.

**After converting:** RomM has to rescan before it sees the new files. Either
enable its filesystem watcher or run a scan from RomM. Then, if you converted to
a format RomM cannot hash-match, press **Re-match in RomM** in the RomM view to
re-apply the saved metadata (or leave it to run automatically on page load).

**Unattended conversion:** set `ROMM_AUTO_CONVERT=true` to enable the scheduler,
then configure per-platform rules under **RomM → Automation** — target format,
schedule, queueing limits and selection filters, all per platform. The rules
themselves live in the app, not in environment variables: a per-platform map is
not something an env var can express.

---

## Common Commands

### Start service (detached)
```bash
docker-compose up -d
```

### Stop service
```bash
docker-compose down
```

### View logs
```bash
docker-compose logs -f
```

### Check status
```bash
docker-compose ps
```

### Restart service
```bash
docker-compose restart
```

### Remove containers and volumes
```bash
docker-compose down -v
```

---

## Access Web UI

After starting with `docker-compose up -d`:
- **URL:** http://localhost:8080
- **Health Check:** http://localhost:8080/health
- **API Docs:** http://localhost:8080/docs

The Web UI examples bind the published port to `127.0.0.1` because the API is
intended for trusted local access. If you expose it beyond the host, put it
behind a trusted reverse proxy, VPN, or authentication layer.

---

## Environment Variables

All configurations support these environment variables (edit in the compose file):

Volume behavior:
- If `COMPRESSATORIUM_VOLUMES` is set, that explicit list is used.
- If `COMPRESSATORIUM_VOLUMES` is unset, the app scans `COMPRESSATORIUM_MOUNT_ROOT/*` at startup (restart after mount changes).

| Variable | Default | Description |
|----------|---------|-------------|
| `CHD_MODE` | `webui` | Mode: `webui` or `cli` |
| `COMPRESSATORIUM_MOUNT_ROOT` | `/data` | Startup scan root for auto-discovered volumes (`/data/*`) |
| `COMPRESSATORIUM_VOLUMES` | (unset) | Explicit comma-separated volume paths (skips startup scan) |
| `CHD_MOUNT_ROOT` | `/data` | Legacy alias for `COMPRESSATORIUM_MOUNT_ROOT` |
| `CHD_VOLUMES` | (unset) | Legacy alias for `COMPRESSATORIUM_VOLUMES` |
| `PUID` | `999` | Optional runtime UID remap for `converter` (commonly set on Unraid) |
| `PGID` | `999` | Optional runtime GID remap for `converter`; reuses an existing group when that GID is already present |
| `CHD_DATA_DIR` | `/config` | Persistent data directory |
| `COMPRESSATORIUM_ENABLE_AUTH` | `false` | Enable token auth for the Web UI and `/api`. Off by default; set `true` to require a token. |
| `COMPRESSATORIUM_AUTH_TOKEN` / `CHD_AUTH_TOKEN` | generated in `/config/auth_token` | Web UI/API password (when auth is enabled). Use it via HTTP Basic, bearer token, or `X-Compressatorium-Token`. |
| `COMPRESSATORIUM_AUTH_USERNAME` | `admin` | HTTP Basic username for the Web UI (when auth is enabled). |
| `COMPRESSATORIUM_SEARCH_AUTO_RETURN_TO_FILE_LIST` | `true` | Web UI: when true, `Search All` conversions return to the previous file-list view after queueing |
| `CHD_SEARCH_AUTO_RETURN_TO_FILE_LIST` | `true` | Legacy alias for `COMPRESSATORIUM_SEARCH_AUTO_RETURN_TO_FILE_LIST` |
| `CHD_TEMP_DIR` | `/config/temp` | Temporary working directory for archive extraction (auto-created) |
| `CHD_CONCURRENCY_LOCK_DIR` | `/tmp/chd-locks` | Directory for job lock files (ephemeral, auto-cleaned on container restart) |
| `COMPRESSATORIUM_DB_PATH` | `/config/compressatorium.db` | Unified SQLite database (DAT index, DAT-sync state, match cache, CHD metadata, verification state). On first startup, legacy JSON files are auto-migrated to this DB and renamed to `*.migrated.bak` (never deleted). Custom legacy paths set via `CHD_METADATA_STORE` / `CHD_VERIFICATION_STORE` are honored during migration. |
| `CHD_METADATA_STORE` | *(deprecated)* | Legacy JSON path; auto-migrated to SQLite on first startup (custom path honored if set) |
| `CHD_VERIFICATION_STORE` | *(deprecated)* | Legacy JSON path; auto-migrated to SQLite on first startup (custom path honored if set) |
| `ROMM_URL` | *(unset)* | Base URL of your RomM instance, e.g. `http://romm:8080`. Unset disables the RomM view entirely. |
| `ROMM_TOKEN` | *(unset)* | RomM **client API token** (`rmm_…`), created under *Administration → Client API Tokens*. Needs `platforms.read` + `roms.read`, plus `roms.write` to re-apply metadata after conversion. Seeds the first run only. A token entered in **RomM → Settings** is stored in the `preferences` table of `compressatorium.db` (never returned to the browser) and takes precedence over this variable — so back up and permission that file accordingly. |
| `ROMM_LIBRARY_ROOT` | *(unset)* | Where RomM's library folder is mounted **in this container**. Must be inside a configured volume. RomM's ROM paths are resolved relative to it. |
| `ROMM_AUTO_CONVERT` | `false` | Enable scheduled per-platform conversion sweeps. Rules are configured in the app (**RomM → Automation**). |
| `ROMM_AUTO_CONVERT_MAX_PER_RUN` | `25` | Ceiling on jobs queued by one sweep, across all platforms. |
| `ROMM_REPIN` | `true` | Save a ROM's metadata before converting to a format RomM cannot hash-match, so it can be restored afterwards. |
| `ROMM_REPIN_ON_LOAD` | `true` | Re-apply saved metadata automatically when the RomM view loads. |

All of these are editable in **RomM → Settings**; the environment only supplies
the first-run default.
| `CHDMAN_MODE` | `createcd` | Conversion mode: `createcd` or `createdvd` (CLI mode) |
| `CHDMAN_PATH` | `/usr/bin/chdman` | Path to chdman binary |
| `DOLPHIN_TOOL_PATH` | `/usr/local/bin/dolphin-tool` | Path to dolphin-tool binary |
| `MAXCSO_PATH` | `/usr/local/bin/maxcso` | Path to maxcso binary (PSP/PS2 CSO/CSO v2/ZSO/DAX) |
| `SEVENZIP_PATH` | `7z` | Path to the 7z binary (handheld ROM `.gb`/`.gbc`/`.gba`/`.nds` ↔ `.7z`/`.zip`). Ships via `p7zip-full`; set to `7zz` on distros that provide the newer `7zip` package |
| `MAKEPS3ISO_PATH` | `/usr/local/bin/makeps3iso` | Path to the makeps3iso binary (PS3 decrypted folder → ISO) |
| `JWUDTOOL_PATH` | `/usr/local/bin/jwudtool` | Path to the JWUDTool launcher (Wii U `.wud` ↔ `.wux`). The image ships a launcher that execs the bundled jar with a headless JRE |
| `NKIT2ISO_PATH` | `/usr/local/bin/nkit2iso` | Path to the nkit2iso binary (NKit-shrunk GameCube/Wii image → ISO) |
| `NKIT2ISO_RECOVERY` | `none` | What nkit2iso does with a Wii image whose update partition was removed: `none` zero-fills it (fully offline; playable but not redump-verifiable), `download` fetches the publicly archived recovery partition for a bit-exact restore. `download` is the only path on which the tool uses the network. |
| `SWITCH_KEYS` | *(unset)* | Directory holding your own Switch `prod.keys`. Source of truth for Switch (nsz); mount it read-only. When unset, the app best-effort checks `~/.switch` and your mounted volumes. No keys ship with the image. |
| `NSZ_COMPRESSION_LEVEL` | `18` | zstandard level for Switch compression (1-22) |
| `MAX_CONCURRENT_JOBS` | `1` | Parallel conversion jobs |
| `MAX_JOB_HISTORY` | `500` | Finished jobs to retain in history (the counts stay true past the cap; only the listed rows are trimmed) |
| `COMPRESSATORIUM_TOOL_NICE` | `10` | Nice level for all tools (0-19). Legacy alias: `CHD_CHDMAN_NICE`. |
| `COMPRESSATORIUM_TOOL_IOPRIO_CLASS` | `2` | I/O priority class for all tools (`1` realtime, `2` best-effort, `3` idle). Legacy alias: `CHD_CHDMAN_IOPRIO_CLASS`. |
| `COMPRESSATORIUM_TOOL_IOPRIO_LEVEL` | `6` | I/O priority level for all tools (`0` highest, `7` lowest). Legacy alias: `CHD_CHDMAN_IOPRIO_LEVEL`. |
| `COMPRESSATORIUM_TOOL_INFO_TIMEOUT` | `60` | Timeout in seconds for `info`/`header` subprocesses (chdman and Dolphin; nsz/3DS read info from the filesystem). 0 disables. Legacy alias: `CHD_INFO_TIMEOUT`. |
| `COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT` | `1800` | Baseline timeout in seconds for verify runs across all tools. The effective bound is this plus `COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT_PER_GIB` per GiB of the file being verified, capped by `COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT_CAP` — verify reads the whole file, so its runtime scales with size. Set to `0` to disable this bound (the no-output stall bound below is a separate knob and stays on; zero both for no bound at all). Legacy alias: `CHD_VERIFY_TIMEOUT`. |
| `COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT_PER_GIB` | `600` | Extra verify allowance in seconds per GiB of the verified file (0 makes the bound flat). |
| `COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT_CAP` | `86400` | Upper bound in seconds on the size-scaled verify timeout (0 uncapped). |
| `COMPRESSATORIUM_TOOL_VERIFY_PROGRESS_TIMEOUT` | `600` | Timeout in seconds without any verify output, for the verifiers that stream progress (chdman, dolphin-tool); catches a wedged verify long before the overall bound. Independent of `COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT` — it still applies when that is `0`. 0 disables. Legacy alias: `CHD_VERIFY_PROGRESS_TIMEOUT`. |
| `COMPRESSATORIUM_<TOOL>_NICE` / `_IOPRIO_CLASS` / `_IOPRIO_LEVEL` / `_VERIFY_TIMEOUT` | *(shared default)* | Optional per-tool overrides (`<TOOL>` = `CHDMAN`, `DOLPHIN_TOOL`, `NSZ`, `Z3DS`, `MAXCSO`, `ROMZ`, `MAKEPS3ISO`, `NKIT2ISO`, `JWUD`) that fall back to the shared `COMPRESSATORIUM_TOOL_*` values. `NKIT2ISO` and `MAKEPS3ISO` have no `_VERIFY_TIMEOUT` (no verify step, and no reachable verify entry point, respectively); `JWUD` verifies without a subprocess, so its bound applies to the verify call itself. |
| `COMPRESSATORIUM_<TOOL>_INFO_TIMEOUT` | *(shared default)* | Optional per-tool info-timeout override, only for `<TOOL>` = `CHDMAN` or `DOLPHIN_TOOL` (the only tools whose `info` runs a subprocess). |
| `CHD_ARCHIVE_MAX_ENTRIES` | `5000` | Max archive members to list (0 disables limit) |
| `CHD_ARCHIVE_MAX_MEMBER_SIZE` | `0` | Max size in bytes per archive member (0 disables limit) |
| `CHD_ARCHIVE_MAX_TOTAL_SIZE` | `0` | Max total size in bytes for archive listings/extractions (0 disables) |
| `LOGLEVEL` | `INFO` | Log verbosity level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) |
| `LOG_PATH` | (none) | Path to log file (stdout only if unset) |
| `CHD_DEBUG_HEARTBEAT` | `30` | Maintenance loop interval (seconds) |
| `CHD_DEBUG_PROGRESS_INTERVAL` | `30` | Debug progress log interval |
| `CHD_DEBUG_PROGRESS_TIMEOUT` | `300` | Debug progress timeout |
| `CHD_PROGRESS_TIMEOUT` | `600` | Fail a conversion if progress and output size do not advance for this many seconds (0 disables) |
| `CHD_PROGRESS_TIMEOUT_PER_GIB` | `120` | Additional stall-timeout seconds per GiB of input size |
| `CHD_PROGRESS_TIMEOUT_CAP` | `7200` | Upper bound for adaptive conversion stall timeout (0 disables cap) |
| `STATIC_DIR` | `/static` | Path to static web assets |

---

## Resource Limits

Each configuration includes conservative resource limits. Adjust CPU and memory values based on your system.

### Tuning and Host Recommendations

**How to change settings**
- Edit the compose file and update `MAX_CONCURRENT_JOBS`, `COMPRESSATORIUM_TOOL_*`, and the `deploy.resources` limits.

**Recommended starting points**
- **Low/medium hosts (≤16 GB RAM, HDD or parity-backed arrays):** keep `MAX_CONCURRENT_JOBS=1`, `COMPRESSATORIUM_TOOL_NICE=10`, `COMPRESSATORIUM_TOOL_IOPRIO_CLASS=2`, `COMPRESSATORIUM_TOOL_IOPRIO_LEVEL=6`. Set a container memory limit (8–12 GB).
- **Faster hosts (32+ GB RAM, SSD cache):** try `MAX_CONCURRENT_JOBS=2` and a higher memory limit (16–24 GB). Raise I/O priority only if the host remains responsive.
- **If the host becomes sluggish:** lower `MAX_CONCURRENT_JOBS`, increase `COMPRESSATORIUM_TOOL_NICE`, or set `COMPRESSATORIUM_TOOL_IOPRIO_CLASS=3` (idle) with `COMPRESSATORIUM_TOOL_IOPRIO_LEVEL=7`.

**Docker host tips**
- Prefer SSD/cache for `CHD_TEMP_DIR` and CHD output to reduce array contention.
- Avoid running other heavy services during conversion.
- Always set container CPU/memory limits on shared hosts.

Example:
```yaml
deploy:
  resources:
    limits:
      cpus: '2.0'      # Maximum 2 CPU cores
      memory: 8G       # Maximum 8GB RAM
    reservations:
      cpus: '0.5'      # Reserved 0.5 CPU cores
      memory: 512M     # Reserved 512MB RAM
```

### Synology DSM (and other CFS-less kernels)

The error covers two distinct causes — Synology DSM is the common one, but
the same message also fires when a host's cgroup controllers simply aren't
mounted (a Docker/host misconfiguration unrelated to Synology):

```
Error response from daemon: NanoCPUs can not be set, as your kernel does not
support CPU CFS scheduler or the cgroup is not mounted
```

On DSM specifically, the Docker/Container Manager kernel doesn't support CPU
CFS bandwidth control, so the `cpus:` limit above (Docker's `NanoCPUs`
setting) fails outright — that's the documented, known-affected case below.
If you're not on Synology, check which controllers are actually mounted
before assuming the same fix applies: `cat /sys/fs/cgroup/cgroup.controllers`
(cgroup v2) or `ls /sys/fs/cgroup/cpuset` (cgroup v1) — if `cpuset` isn't
listed/present either, the `cpuset` workaround below will fail the same way
and dropping `cpus:` entirely is the only option.

Compose has no way to detect either cause at parse time, so there's no single
compose file that works everywhere. Pick one of these instead:

- **Remove the CPU limit (simplest, recommended):** delete or comment out the
  `cpus:` lines under both `limits:` and `reservations:`; keep `memory:`,
  which is unaffected. **This leaves CPU usage unbounded** — a conversion can
  use up to 100% of every core the container can see. `MAX_CONCURRENT_JOBS=1`
  only limits how many jobs run at once, not how much CPU each one takes, and
  the `COMPRESSATORIUM_TOOL_NICE`/`COMPRESSATORIUM_TOOL_IOPRIO_*` settings
  only change scheduling *priority* relative to other processes on the host
  (see `nice(1)`/`ionice(1)`) — they make a conversion yield under
  contention, they don't cap it. If you need an actual CPU ceiling on a host
  that can't use `cpus:`, use `cpuset` below instead.
- **Pin to specific cores with `cpuset` instead:** add a top-level
  `cpuset: "0-1"` on the service (a sibling of `deploy:`, not nested under
  `resources:`) to restrict the container to specific CPU cores via the
  `cpuset` cgroup controller, which Synology does support (see the cgroup
  check above for other hosts). **This is not a drop-in replacement for
  `cpus:`** — `cpuset` pins to a set of cores rather than capping the
  proportion of CPU time used, so the container can still use up to 100% of
  each pinned core. Size the core count to the compute budget you actually
  want.

---

## Troubleshooting

### Container won't start
```bash
docker-compose logs
```

If the error is `NanoCPUs can not be set, as your kernel does not support CPU
CFS scheduler or the cgroup is not mounted` (common on Synology DSM), see
[Synology DSM (and other CFS-less kernels)](#synology-dsm-and-other-cfs-less-kernels)
above.

### Check health status
```bash
docker-compose ps
# Look for "healthy" status
```

### Reset everything
```bash
docker-compose down -v
docker-compose up -d
```

### Access container shell
```bash
docker-compose exec compressatorium bash
```

---

## Production Deployment

For production guidance, security notes, and checklists, see **[DEPLOYMENT.md](DEPLOYMENT.md)**.

Key recommendations:
- Enable resource limits
- Set up HTTPS if exposing externally
- Enable built-in auth (`COMPRESSATORIUM_ENABLE_AUTH=true`) and/or add proxy authentication
- Monitor disk space and resource usage
- Regular backups of converted files
