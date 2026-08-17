"""Unattended conversion of a RomM library, driven by per-platform rules.

Each RomM platform gets its own rule: what to convert it to, how to compress
it, where to put the result, when to run, how much to queue at once, and which
ROMs to consider at all. Platforms are independent — GameCube can convert to
RVZ hourly at max 10 jobs a run while PS2 converts to CHD nightly between 01:00
and 06:00 — because a library is not homogeneous and one global setting cannot
describe it.

Three properties this is built for, in order:

**Idempotent.** A ROM is queued only when its target output is genuinely
absent, and that answer comes from ``ToolPlugin.detect_output()`` — the same
registry-driven detector that badges rows in the file browser. The filesystem
is the state, so a sweep that runs twice, runs after a restart, or races a
manual conversion converges instead of duplicating. Nothing is remembered
between sweeps because nothing needs to be.

**Deterministic.** Platforms run in priority order and ROMs in the rule's
chosen order, so a capped sweep resumes predictably rather than sampling the
library at random.

**Bounded.** Every sweep is capped per platform and overall, respects the
queue's own backpressure, and stops rather than spins — because the failure
mode of the alternative is discovering a 2,000-ROM platform and committing the
operator's disk and CPU for a week on the strength of one checkbox.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any

from fastapi.concurrency import run_in_threadpool

from logging_setup import get_logger
from models import ConversionMode
from services import romm_settings
from services.job_manager import QueueBackpressureError, job_manager
from services.preferences_store import preferences_store
from services.romm import RommError, romm_client
from services.tools import registry
from utils.path_utils import is_within_configured_volumes

logger = get_logger("romm_auto")

# Per-platform rules (user configuration) and per-platform run state, kept in
# separate keys on purpose: mixing "when did this last run" into the rule blob
# means every sweep rewrites the operator's configuration.
RULES_KEY = "romm.rules"
STATE_KEY = "romm.auto_state"

ORDERS = ("name", "size_desc", "size_asc", "id", "newest")
DUPLICATE_ACTIONS = ("skip", "overwrite", "rename")
# 0 = Monday, matching datetime.weekday().
ALL_DAYS = (0, 1, 2, 3, 4, 5, 6)

# Bounds applied to every numeric rule field, so a hand-edited preference blob
# cannot produce a sweep that queues the whole library or runs every second.
_BOUNDS = {
    "interval_minutes": (5, 10080),
    "max_per_run": (1, 1000),
    "priority": (-100, 100),
    "min_size_mb": (0, 1024 * 1024),
    "max_size_mb": (0, 1024 * 1024),
    "compression_level": (0, 22),
}

# Cap on a user-supplied filter pattern. A pathological regex is the operator's
# own doing, but length is a cheap first guard against the obvious ones.
_MAX_PATTERN = 500


def _clamp(field: str, value: Any, default: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    low, high = _BOUNDS[field]
    return max(low, min(high, out))


def _parse_hhmm(value: Any) -> dt_time | None:
    """Parse ``"HH:MM"``. None (no window) on anything unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        hour, _, minute = value.strip().partition(":")
        return dt_time(hour=int(hour), minute=int(minute or 0))
    except (TypeError, ValueError):
        return None


def _valid_pattern(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    pattern = value.strip()[:_MAX_PATTERN]
    try:
        re.compile(pattern)
    except re.error:
        logger.warning("romm_auto: ignoring invalid filter pattern %r", pattern)
        return None
    return pattern


def default_rule(mode: str = "") -> dict[str, Any]:
    """A rule with every field at its default, for the UI to render a new row."""
    return {
        "enabled": False,
        "mode": mode,
        "compression": None,
        "compression_level": None,
        "output_dir": None,
        "duplicate_action": "skip",
        "delete_on_verify": False,
        "verify_after": False,
        "split": False,
        # scheduling
        "interval_minutes": 60,
        "window_start": None,
        "window_end": None,
        "days": list(ALL_DAYS),
        # queueing
        "max_per_run": 25,
        "priority": 0,
        "order": "name",
        # selection filters
        "min_size_mb": 0,
        "max_size_mb": 0,
        "include_pattern": None,
        "exclude_pattern": None,
        "only_unmatched": False,
        "only_matched": False,
    }


def normalize_rule(raw: Any, *, mode_required: bool = True) -> dict[str, Any] | None:
    """Coerce one submitted/stored rule into the full schema.

    Returns None when the rule cannot be honoured at all (no mode, or a mode no
    registered tool provides). Tolerant elsewhere: a rule naming a stale option
    falls back to that field's default rather than failing the whole sweep,
    because these blobs outlive the tools that were installed when they were
    written.
    """
    if not isinstance(raw, dict):
        return None
    out = default_rule()
    mode = raw.get("mode")
    if isinstance(mode, str) and mode:
        try:
            registry.spec(mode)
            out["mode"] = mode
        except KeyError:
            logger.warning("romm_auto: rule names unknown mode %r", mode)
            return None
    elif mode_required:
        return None

    spec = registry.spec(out["mode"]) if out["mode"] else None

    out["enabled"] = bool(raw.get("enabled", False))
    # Compression is only meaningful where the mode supports it; storing it
    # otherwise would be silently dropped at submit time anyway.
    if spec is not None and spec.supports_compression and raw.get("compression"):
        out["compression"] = str(raw["compression"])
    if spec is not None and spec.supports_compression_level:
        if raw.get("compression_level") is not None:
            out["compression_level"] = _clamp(
                "compression_level", raw["compression_level"], 0,
            )
    if raw.get("output_dir"):
        out["output_dir"] = str(raw["output_dir"])
    if raw.get("duplicate_action") in DUPLICATE_ACTIONS:
        out["duplicate_action"] = raw["duplicate_action"]
    # Both gated on what the mode actually allows, mirroring the manual path.
    if spec is not None and spec.supports_delete_on_verify:
        out["delete_on_verify"] = bool(raw.get("delete_on_verify", False))
    out["verify_after"] = bool(raw.get("verify_after", False))
    out["split"] = bool(raw.get("split", False))

    out["interval_minutes"] = _clamp("interval_minutes", raw.get("interval_minutes"), 60)
    out["window_start"] = raw.get("window_start") or None
    out["window_end"] = raw.get("window_end") or None
    days = raw.get("days")
    if isinstance(days, list):
        parsed = sorted({int(d) for d in days if isinstance(d, (int, float)) and 0 <= int(d) <= 6})
        out["days"] = parsed or list(ALL_DAYS)

    out["max_per_run"] = _clamp("max_per_run", raw.get("max_per_run"), 25)
    out["priority"] = _clamp("priority", raw.get("priority"), 0)
    if raw.get("order") in ORDERS:
        out["order"] = raw["order"]

    out["min_size_mb"] = _clamp("min_size_mb", raw.get("min_size_mb"), 0)
    out["max_size_mb"] = _clamp("max_size_mb", raw.get("max_size_mb"), 0)
    out["include_pattern"] = _valid_pattern(raw.get("include_pattern"))
    out["exclude_pattern"] = _valid_pattern(raw.get("exclude_pattern"))
    out["only_unmatched"] = bool(raw.get("only_unmatched", False))
    out["only_matched"] = bool(raw.get("only_matched", False))
    # Mutually exclusive; "both" is the same as neither, and silently meaning
    # "nothing matches" would be a confusing way to spend a sweep.
    if out["only_matched"] and out["only_unmatched"]:
        out["only_matched"] = out["only_unmatched"] = False
    return out


def normalize_rules(raw: Any) -> dict[str, dict]:
    if not isinstance(raw, dict):
        return {}
    rules: dict[str, dict] = {}
    for key, value in raw.items():
        try:
            platform_id = int(key)
        except (TypeError, ValueError):
            continue
        rule = normalize_rule(value)
        if rule is not None:
            rules[str(platform_id)] = rule
    return rules


async def get_rules() -> dict[str, dict]:
    return normalize_rules(await preferences_store.get(RULES_KEY))


async def set_rules(raw: Any) -> dict[str, dict]:
    rules = normalize_rules(raw)
    await preferences_store.put(RULES_KEY, rules)
    return rules


async def get_state() -> dict[str, dict]:
    stored = await preferences_store.get(STATE_KEY)
    return stored if isinstance(stored, dict) else {}


async def _record_run(platform_id: str, summary: dict) -> None:
    state = await get_state()
    state[str(platform_id)] = {
        "last_run_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "last_queued": summary.get("queued", 0),
        "last_considered": summary.get("considered", 0),
    }
    await preferences_store.put(STATE_KEY, state)


# ----------------------------------------------------------------------
# scheduling
# ----------------------------------------------------------------------


def _in_window(rule: dict, now: datetime) -> bool:
    """Is *now* inside the rule's allowed days and time-of-day window?

    A window whose end is before its start wraps midnight (22:00–04:00), which
    is the shape an overnight conversion window actually takes.
    """
    if now.weekday() not in rule["days"]:
        return False
    start = _parse_hhmm(rule["window_start"])
    end = _parse_hhmm(rule["window_end"])
    if start is None or end is None:
        return True
    current = now.time()
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def _is_due(rule: dict, state: dict, now: datetime) -> bool:
    if not rule["enabled"]:
        return False
    if not _in_window(rule, now):
        return False
    last = state.get("last_run_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except ValueError:
        return True
    elapsed_minutes = (now - last_dt).total_seconds() / 60
    return elapsed_minutes >= rule["interval_minutes"]


# ----------------------------------------------------------------------
# selection
# ----------------------------------------------------------------------


def _rom_sort_key(rule: dict):
    order = rule["order"]
    if order == "size_desc":
        return lambda r: (-(r.get("fs_size_bytes") or 0), r.get("id") or 0)
    if order == "size_asc":
        return lambda r: ((r.get("fs_size_bytes") or 0), r.get("id") or 0)
    if order == "id":
        return lambda r: (r.get("id") or 0,)
    if order == "newest":
        return lambda r: (-(r.get("id") or 0),)
    return lambda r: ((r.get("name") or r.get("fs_name") or "").lower(), r.get("id") or 0)


def _passes_filters(rom: dict, rule: dict) -> bool:
    name = rom.get("fs_name") or rom.get("name") or ""
    size = rom.get("fs_size_bytes") or 0
    if rule["min_size_mb"] and size < rule["min_size_mb"] * 1024 * 1024:
        return False
    if rule["max_size_mb"] and size > rule["max_size_mb"] * 1024 * 1024:
        return False
    if rule["include_pattern"] and not re.search(rule["include_pattern"], name, re.I):
        return False
    if rule["exclude_pattern"] and re.search(rule["exclude_pattern"], name, re.I):
        return False
    # RomM reports identification two ways depending on version; treat either
    # as authoritative and fall back to "unknown" rather than filtering blind.
    identified = rom.get("is_identified")
    if identified is None:
        identified = bool(rom.get("igdb_id") or rom.get("moby_id") or rom.get("ss_id"))
    if rule["only_matched"] and not identified:
        return False
    if rule["only_unmatched"] and identified:
        return False
    return True


def _output_exists(path: str, tool_id: str) -> bool:
    """True when this tool's output already sits beside *path*.

    ``exists`` covers both a finished output and one a job is writing right
    now: either way, queueing another conversion of the same source is wrong.
    """
    tool = registry.get(tool_id)
    if tool is None:
        return False
    status = tool.detect_output(path)
    return status is not None and status.exists


def _active_source_paths() -> set[str]:
    """Sources already queued or converting, resolved once per sweep."""
    out: set[str] = set()
    for _job_id, paths in job_manager.get_active_job_candidates():
        for candidate in paths:
            try:
                out.add(str(Path(candidate).expanduser().resolve(strict=False)))
            except (OSError, RuntimeError):
                continue
    return out


# ----------------------------------------------------------------------
# the sweep
# ----------------------------------------------------------------------


async def sweep(
    *,
    platform_ids: list[int] | None = None,
    ignore_schedule: bool = False,
    dry_run: bool = False,
    overall_limit: int | None = None,
) -> dict:
    """Run one pass over the ruled platforms.

    Returns a summary rather than raising on a partial failure: one unreachable
    platform must not abort the sweep for the others.
    """
    cfg = romm_settings.effective()
    rules = await get_rules()
    state = await get_state()
    now = datetime.now(timezone.utc)

    result: dict = {
        "queued": 0,
        "considered": 0,
        "skipped_existing": 0,
        "skipped_active": 0,
        "skipped_filtered": 0,
        "skipped_unconvertible": 0,
        "platforms": [],
        "errors": [],
        "stopped_reason": None,
        "dry_run": dry_run,
    }
    if not rules:
        result["stopped_reason"] = "no_rules"
        return result
    if not romm_client.configured or not romm_client.library_root:
        result["stopped_reason"] = "not_configured"
        return result

    cap = overall_limit if overall_limit is not None else int(
        cfg.get("auto_convert_max_per_run", 25),
    )
    wanted = {str(p) for p in platform_ids} if platform_ids else None

    # Priority first (lower runs earlier), then id, so the order is total and
    # a capped sweep resumes where the last one stopped.
    ordered = sorted(rules, key=lambda pid: (rules[pid]["priority"], int(pid)))
    active_paths = await run_in_threadpool(_active_source_paths)

    for platform_id in ordered:
        if result["queued"] >= cap:
            result["stopped_reason"] = "limit"
            break
        rule = rules[platform_id]
        if wanted is not None and platform_id not in wanted:
            continue
        if not rule["enabled"] and not ignore_schedule:
            continue
        if not ignore_schedule and not _is_due(rule, state.get(platform_id, {}), now):
            continue

        try:
            spec = registry.spec(rule["mode"])
            tool = registry.for_mode(rule["mode"])
        except KeyError:
            continue

        try:
            roms = await run_in_threadpool(romm_client.roms, int(platform_id))
        except RommError as exc:
            logger.warning("romm_auto: platform %s unreadable: %s", platform_id, exc)
            result["errors"].append(
                {"platform_id": int(platform_id), "error": "unreadable"},
            )
            continue

        per_platform_cap = min(rule["max_per_run"], cap - result["queued"])
        batch: list[str] = []
        considered = 0

        for rom in sorted(roms, key=_rom_sort_key(rule)):
            if len(batch) >= per_platform_cap:
                break
            considered += 1
            if not _passes_filters(rom, rule):
                result["skipped_filtered"] += 1
                continue
            path = romm_client.local_path(rom)
            if not path or not is_within_configured_volumes(path):
                continue
            if not await run_in_threadpool(tool.converts_path, path):
                result["skipped_unconvertible"] += 1
                continue
            try:
                resolved = str(Path(path).expanduser().resolve(strict=False))
            except (OSError, RuntimeError):
                continue
            if resolved in active_paths:
                result["skipped_active"] += 1
                continue
            if rule["duplicate_action"] == "skip" and await run_in_threadpool(
                _output_exists, path, spec.tool_id,
            ):
                result["skipped_existing"] += 1
                continue
            batch.append(path)

        result["considered"] += considered
        summary = {
            "platform_id": int(platform_id),
            "mode": rule["mode"],
            "queued": 0,
            "considered": considered,
        }

        if batch and not dry_run:
            try:
                jobs = await job_manager.create_batch_jobs(
                    batch,
                    ConversionMode(rule["mode"]),
                    output_dir=rule["output_dir"],
                    compression=rule["compression"],
                    delete_on_verify=rule["delete_on_verify"],
                )
                summary["queued"] = len(jobs)
                result["queued"] += len(jobs)
                # Claim them immediately so a later platform in the same sweep
                # cannot queue the same source twice.
                active_paths.update(
                    str(Path(p).expanduser().resolve(strict=False)) for p in batch
                )
            except QueueBackpressureError:
                # The queue is full. Stop rather than spin: the next sweep
                # resumes exactly here, because the filesystem still reports
                # these ROMs unconverted.
                logger.info("romm_auto: queue full, stopping sweep")
                result["stopped_reason"] = "queue_full"
                result["platforms"].append(summary)
                break
            except Exception:
                logger.warning(
                    "romm_auto: failed to queue platform %s", platform_id, exc_info=True,
                )
                result["errors"].append(
                    {"platform_id": int(platform_id), "error": "queue_failed"},
                )
        elif batch:
            summary["queued"] = len(batch)
            result["queued"] += len(batch)

        summary["candidates"] = [Path(p).name for p in batch[:25]]
        result["platforms"].append(summary)
        # A real run updates the schedule clock; a preview must not, or looking
        # at what *would* happen would postpone the run that should.
        if not dry_run:
            await _record_run(platform_id, summary)

    return result


async def run_forever() -> None:
    """Background scheduler. Ticks once a minute; each platform runs on its own
    cadence, so per-platform intervals and windows are honoured without a
    timer per rule."""
    logger.info("romm_auto: scheduler started")
    while True:
        try:
            await asyncio.sleep(60)
            cfg = romm_settings.effective()
            # Re-read every tick: the master switch is editable in the app, so
            # turning it off must take effect without a restart.
            if not cfg.get("auto_convert"):
                continue
            summary = await sweep()
            if summary["queued"]:
                logger.info(
                    "romm_auto: queued %s job(s) across %s platform(s)",
                    summary["queued"], len(summary["platforms"]),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A sweep failure must never kill the scheduler; the next tick retries.
            logger.warning("romm_auto: sweep failed", exc_info=True)
