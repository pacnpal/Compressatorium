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
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from datetime import time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from models import ConversionMode, JobStatus
from services import romm_repin, romm_settings
from services.job_manager import QueueBackpressureError, job_manager
from services.output_conflicts import (
    QUEUE,
    SKIP_EXISTING,
    SKIP_LOCKED,
    resolve_destination,
)
from services.preferences_store import preferences_store
from services.romm import RommError, romm_client
from services.tools import InputKind, registry
from utils.delete_plan import build_delete_snapshot
from utils.path_utils import is_within_configured_volumes, match_extension

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

# One sweep at a time, process-wide. The active-source snapshot is taken before
# anything is queued, so the minute scheduler and a manual "Run now" could each
# read the same "nothing in flight" and queue the same ROM twice -- the job
# manager's creation lock serialises the inserts but does not deduplicate
# across two independently planned batches.
_sweep_lock = asyncio.Lock()


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


def _valid_timezone(value: Any) -> str:
    """An IANA zone name we can actually load, or ``"UTC"``.

    Validated on the way in so a sweep can never fail on a zone the host has no
    data for -- a rule written on a machine with a fuller tzdata than the
    container's would otherwise break the schedule silently.
    """
    if not isinstance(value, str) or not value.strip():
        return "UTC"
    name = value.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        logger.warning("romm_auto: unknown timezone %r, falling back to UTC", name)
        return "UTC"
    return name


def _valid_pattern(value: Any) -> tuple[str | None, bool]:
    """``(pattern, invalid)``. A mistyped regex is refused, never dropped.

    Dropping it silently was the dangerous option: the filter is what keeps a
    rule to a subset, so a rule whose `(USA` never compiled would come back as
    *unfiltered* and the next unattended sweep could queue the whole platform —
    with delete-on-verify attached. The caller disables the rule instead.
    """
    if not isinstance(value, str) or not value.strip():
        return None, False
    pattern = value.strip()[:_MAX_PATTERN]
    try:
        re.compile(pattern)
    except re.error:
        logger.warning("romm_auto: refusing invalid filter pattern %r", pattern)
        return None, True
    if _has_nested_quantifier(pattern):
        logger.warning(
            "romm_auto: refusing filter pattern %r, it backtracks too long",
            pattern,
        )
        return None, True
    return pattern, False


# Quantifiers that can repeat unboundedly. `?` is excluded: `(a?)?` cannot
# blow up, because neither level can consume more than once.
_UNBOUNDED_QUANTIFIERS = "*+{"


def _has_nested_quantifier(pattern: str) -> bool:
    """Whether *pattern* has the shape that backtracks exponentially.

    A quantified group whose body itself repeats -- ``(a+)+``, ``(\\w+\\s?)*``
    -- or whose body is an alternation, ``(a|a)+``. Those are the shapes where
    the engine has exponentially many ways to split the same input, and where
    a filename of a few dozen characters takes longer than the heat death of
    the sweep.

    Read, never run. The obvious way to measure this is to time the pattern
    against an adversarial string, but executing an operator-supplied regex is
    precisely what CodeQL's ``py/regex-injection`` flags -- and rightly: the
    check would then have to survive the very thing it is looking for, and
    timing is load-dependent, so a busy machine could reject a fine pattern.
    Inspecting the source costs nothing and is deterministic.

    Conservative, and deliberately so. It refuses a little more than it must
    (``(USA|Europe)+`` is harmless but rejected), and it is a shape test, not a
    proof. Being wrong here costs one clear message at save time; being wrong
    the other way wedges the scheduler, because ``re`` cannot be interrupted
    and the sweep holds ``_sweep_lock`` while it matches.
    """
    group_starts: list[int] = []
    i = 0
    length = len(pattern)
    while i < length:
        char = pattern[i]
        if char == "\\":
            i += 2
            continue
        if char == "[":
            i = _skip_class(pattern, i)
            continue
        if char == "(":
            group_starts.append(i + 1)
            i += 1
            continue
        if char == ")":
            body = pattern[group_starts.pop() if group_starts else 0:i]
            following = pattern[i + 1] if i + 1 < length else ""
            if following and following in _UNBOUNDED_QUANTIFIERS and (
                _contains_quantifier(body) or "|" in _strip_atoms(body)
            ):
                return True
            i += 1
            continue
        i += 1
    return False


def _skip_class(pattern: str, i: int) -> int:
    """Index just past the character class starting at *i*."""
    i += 1
    while i < len(pattern) and pattern[i] != "]":
        i += 2 if pattern[i] == "\\" else 1
    return i + 1


def _contains_quantifier(body: str) -> bool:
    """Whether *body* repeats anything, ignoring escapes and classes."""
    return any(c in _UNBOUNDED_QUANTIFIERS for c in _strip_atoms(body))


def _strip_atoms(body: str) -> str:
    """*body* with escape pairs and character classes removed.

    So a literal ``\\+`` or a ``[+|]`` class cannot be mistaken for a
    quantifier or an alternation.
    """
    out: list[str] = []
    i = 0
    while i < len(body):
        char = body[i]
        if char == "\\":
            i += 2
            continue
        if char == "[":
            i = _skip_class(body, i)
            continue
        out.append(char)
        i += 1
    return "".join(out)


def default_rule(mode: str = "") -> dict[str, Any]:
    """A rule with every field at its default, for the UI to render a new row.

    The scheduling and verification defaults come from the effective settings,
    not from constants here, so a deployment that sets
    ``ROMM_AUTO_CONVERT_INTERVAL_MINUTES``, ``ROMM_VERIFY_AFTER_CONVERT`` or
    ``ROMM_DELETE_SOURCE_AFTER_VERIFY`` actually gets them. Documented as
    first-run defaults, they were previously loaded into ``romm_settings`` and
    then read by nobody.
    """
    cfg = romm_settings.effective()
    return {
        "enabled": False,
        "mode": mode,
        "compression": None,
        "compression_level": None,
        "output_dir": None,
        "duplicate_action": "skip",
        "delete_on_verify": bool(cfg.get("delete_source_after_verify", False)),
        "verify_after": bool(cfg.get("verify_after_convert", False)),
        "split": False,
        # scheduling
        "interval_minutes": _clamp(
            "interval_minutes", cfg.get("auto_convert_interval_minutes"), 60,
        ),
        "window_start": None,
        "window_end": None,
        "days": list(ALL_DAYS),
        # IANA zone the window and weekday mask are evaluated in. The editor
        # sends the browser's zone; UTC is the fallback for a rule written
        # before this existed. Stored as a name rather than a fixed offset so
        # the window stays correct across DST.
        "timezone": "UTC",
        # queueing
        "max_per_run": _clamp(
            "max_per_run", cfg.get("auto_convert_max_per_run"), 25,
        ),
        "priority": 0,
        "order": "name",
        # selection filters
        "min_size_mb": 0,
        "max_size_mb": 0,
        "include_pattern": None,
        "exclude_pattern": None,
        "only_unmatched": False,
        "only_matched": False,
        # Set only when a submitted output_dir was refused, so the editor can
        # say why the rule is not writing where the operator asked.
        "invalid_output_dir": None,
        # Set when delete-on-verify was refused because this mode + compression
        # cannot verify strongly enough to justify removing the source.
        "unsafe_delete_on_verify": False,
        # Set when a submitted include/exclude regex would not compile, so the
        # editor can say why the rule is paused instead of silently widening it.
        "invalid_pattern": False,
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
    # `supports_compression or supports_compression_level`, matching the shared
    # CompressionPicker gate. nsz declares only the latter but still offers a
    # solid/block layout through `compressionCodecs`, so the stricter test
    # dropped the layout AND, because the level rides on the same string, the
    # level with it.
    if spec is not None and (
        spec.supports_compression or spec.supports_compression_level
    ) and raw.get("compression"):
        out["compression"] = str(raw["compression"])
    if (
        spec is not None
        and spec.supports_compression_level
        and raw.get("compression_level") is not None
    ):
        out["compression_level"] = _clamp("compression_level", raw["compression_level"], 0)
    if raw.get("output_dir"):
        candidate = str(raw["output_dir"])
        # The sweep queues through the job manager directly, so it does not get
        # `/jobs/batch`'s containment check for free. An unvalidated rule could
        # otherwise point the converter at any writable path in the container.
        # Refused here, at the edge where the value is accepted, and again in
        # the sweep before anything is queued.
        if is_within_configured_volumes(candidate):
            out["output_dir"] = candidate
        else:
            logger.warning(
                "romm_auto: refusing output_dir outside the configured volumes: %r",
                candidate,
            )
            out["output_dir"] = None
            out["invalid_output_dir"] = candidate
            # Pausing matters: with output_dir cleared the sweep would happily
            # write beside each source instead, filling a filesystem the
            # operator did not choose. The editor surfaces the rejected path.
            out["enabled"] = False
    if raw.get("duplicate_action") in DUPLICATE_ACTIONS:
        out["duplicate_action"] = raw["duplicate_action"]
    # Both gated on what the mode actually allows, mirroring the manual path.
    # `supports_delete_on_verify` is the registry's answer to "can this mode's
    # output be verified at all", which is the precondition for either switch:
    # deleting needs the check to pass first, verify_after wants only the check.
    if spec is not None and spec.supports_delete_on_verify:
        # `out[...]` as the fallback, not False: an omitted field must inherit
        # the configured default rather than silently overriding it.
        out["delete_on_verify"] = bool(
            raw.get("delete_on_verify", out["delete_on_verify"]),
        )
        out["verify_after"] = bool(raw.get("verify_after", out["verify_after"]))
        # A mode that *can* be verified is not always safely deletable: jwud's
        # verify is a structural WUX walk backed only by JWUDTool's own
        # byte-for-byte pass, which `-noVerify` turns off. The manual route
        # refuses that combination; automation must too, or an unattended rule
        # deletes a 25 GB source on the strength of a geometry check.
        tool = registry.for_mode(out["mode"])
        if out["delete_on_verify"] and not tool.delete_on_verify_is_safe(
            out["mode"], out["compression"],
        ):
            logger.warning(
                "romm_auto: refusing delete-on-verify for %s with compression %r",
                out["mode"], out["compression"],
            )
            out["delete_on_verify"] = False
            out["unsafe_delete_on_verify"] = True
    else:
        out["delete_on_verify"] = False
        out["verify_after"] = False
    out["split"] = bool(raw.get("split", False))

    # `out[...]` as the fallback, not a literal: `default_rule()` already seeded
    # these from the effective settings, and re-clamping against 60/25 threw that
    # away for any stored or API-submitted rule that simply omits the field.
    out["interval_minutes"] = _clamp(
        "interval_minutes", raw.get("interval_minutes"), out["interval_minutes"],
    )
    out["window_start"] = raw.get("window_start") or None
    out["window_end"] = raw.get("window_end") or None
    out["timezone"] = _valid_timezone(raw.get("timezone"))
    days = raw.get("days")
    if isinstance(days, list):
        parsed = sorted({int(d) for d in days if isinstance(d, (int, float)) and 0 <= int(d) <= 6})
        out["days"] = parsed or list(ALL_DAYS)

    out["max_per_run"] = _clamp(
        "max_per_run", raw.get("max_per_run"), out["max_per_run"],
    )
    out["priority"] = _clamp("priority", raw.get("priority"), 0)
    if raw.get("order") in ORDERS:
        out["order"] = raw["order"]

    out["min_size_mb"] = _clamp("min_size_mb", raw.get("min_size_mb"), 0)
    out["max_size_mb"] = _clamp("max_size_mb", raw.get("max_size_mb"), 0)
    out["include_pattern"], bad_include = _valid_pattern(raw.get("include_pattern"))
    out["exclude_pattern"], bad_exclude = _valid_pattern(raw.get("exclude_pattern"))
    if bad_include or bad_exclude:
        # Pause rather than run wider than asked. The editor shows why.
        out["invalid_pattern"] = True
        out["enabled"] = False
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


# The rule fields that decide *what* a conversion produces. Change any of
# them and the outputs recorded against this platform no longer answer the
# question "has this rule converted that source" -- so the provenance has to
# go with them.
OUTPUT_IDENTITY_FIELDS = (
    "mode",
    "output_dir",
    "compression",
    "compression_level",
    "split",
    "duplicate_action",
)


def _output_identity(rule: dict | None) -> tuple:
    if not rule:
        return ()
    return tuple(rule.get(field) for field in OUTPUT_IDENTITY_FIELDS)


@asynccontextmanager
async def paused():
    """Hold off the sweep for the duration of the block.

    Anything that changes what a sweep *means* -- which RomM it talks to,
    where the library is mounted, what each rule targets -- has to take this,
    not just the bookkeeping that follows it. A sweep holds the ROM records it
    fetched but resolves each one's local path lazily, so swapping the library
    root underneath a running sweep makes it queue conversions for whatever
    unrelated files happen to sit at the same relative paths in the new
    library. With delete-on-verify on, it then deletes them.
    """
    async with _sweep_lock:
        yield


async def set_rules(raw: Any) -> dict[str, dict]:
    """Replace the rule set, forgetting provenance the new rules invalidate.

    Switching a platform from RVZ to GCZ, or pointing it at a new output
    directory, asks for a different file than the one already produced. Left
    alone, ``converted_ids`` would report every source as done and the
    retargeted rule would convert nothing, with no way to say otherwise --
    so a rule that changes what it produces starts its history over.
    """
    # Under the sweep lock, and for the whole read-compare-write. A sweep
    # holds the rules it started with, and finishes by writing back the ROM ids
    # it queued and its schedule stamp under the same platform key. Clearing
    # the history alongside it would let the old sweep's write land after the
    # clear, so the retargeted rule would skip ROMs it never produced its new
    # format for -- and inherit the old run's clock as well.
    async with _sweep_lock:
        previous = await get_rules()
        rules = normalize_rules(raw)
        stale = [
            pid for pid in previous
            if _output_identity(previous[pid]) != _output_identity(rules.get(pid))
        ]
        await preferences_store.put(RULES_KEY, rules)
        if stale:
            await forget_converted_locked(stale)
        return rules


async def forget_converted(platform_ids: list[str] | None = None) -> int:
    """Drop the converted-id history for *platform_ids* (or all of them).

    The operator-facing escape hatch: restoring ROMs from a backup, or moving
    outputs out of the way by hand, leaves the library in a state only they
    can see. Returns how many platforms were cleared.
    """
    async with _sweep_lock:
        return await forget_converted_locked(platform_ids)


async def forget_converted_locked(platform_ids: list[str] | None = None) -> int:
    """:func:`forget_converted` for a caller already inside :func:`paused`."""
    state = await get_state()
    targets = (
        [str(p) for p in platform_ids] if platform_ids is not None else list(state)
    )
    cleared = 0
    for pid in targets:
        entry = state.get(pid)
        if isinstance(entry, dict) and (
            entry.get("converted") or entry.get("converted_ids")
        ):
            entry = dict(entry)
            entry.pop("converted", None)
            entry.pop("converted_ids", None)
            state[pid] = entry
            cleared += 1
    if cleared:
        await preferences_store.put(STATE_KEY, state)
    return cleared


async def get_state() -> dict[str, dict]:
    stored = await preferences_store.get(STATE_KEY)
    return stored if isinstance(stored, dict) else {}


async def _record_run(platform_id: str, summary: dict) -> None:
    """Stamp the schedule clock, merging into whatever else the entry holds.

    The per-platform entry is shared with ``converted_ids``: replacing it
    wholesale would forget, on every single run, which sources the rule had
    already converted -- and that set is the only thing stopping an
    ``overwrite`` rule from reconverting the library forever.
    """
    state = await get_state()
    entry = dict(state.get(str(platform_id)) or {})
    entry.update({
        "last_run_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "last_queued": summary.get("queued", 0),
        "last_considered": summary.get("considered", 0),
    })
    state[str(platform_id)] = entry
    await preferences_store.put(STATE_KEY, state)


# ----------------------------------------------------------------------
# scheduling
# ----------------------------------------------------------------------


def _in_window(rule: dict, now: datetime) -> bool:
    """Is *now* inside the rule's allowed days and time-of-day window?

    Evaluated in the rule's own timezone. The editor collects plain wall-clock
    values, so comparing them against UTC would run a 22:00-04:00 window at
    22:00 UTC -- the middle of the working day for most of the world.

    A window whose end is before its start wraps midnight (22:00-04:00), which
    is the shape an overnight conversion window actually takes. The weekday is
    read in the same zone, so "Saturday" means the operator's Saturday.
    """
    try:
        now = now.astimezone(ZoneInfo(rule.get("timezone") or "UTC"))
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        pass
    start = _parse_hhmm(rule["window_start"])
    end = _parse_hhmm(rule["window_end"])
    current = now.time()

    if start is None or end is None:
        return now.weekday() in rule["days"]

    if start <= end:
        return now.weekday() in rule["days"] and start <= current <= end

    # An overnight window (22:00-04:00) belongs to the day it *started*, so the
    # 02:00 tail of a Monday window runs on Tuesday morning and a Tuesday-only
    # rule does not fire at 02:00 Tuesday (that tail began on Monday).
    if current >= start:
        return now.weekday() in rule["days"]
    if current <= end:
        return (now.weekday() - 1) % 7 in rule["days"]
    return False


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
    if last_dt.tzinfo is None:
        # A hand-edited or pre-tz-aware state row. Subtracting a naive datetime
        # from an aware one raises TypeError, which would abort the whole sweep
        # over one stale preference blob -- read it as UTC, which is what this
        # module has always written.
        last_dt = last_dt.replace(tzinfo=timezone.utc)
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
    if rule["include_pattern"] and not re.search(rule["include_pattern"], name, re.IGNORECASE):
        return False
    if rule["exclude_pattern"] and re.search(rule["exclude_pattern"], name, re.IGNORECASE):
        return False
    # RomM reports identification two ways depending on version; treat either
    # as authoritative and fall back to "unknown" rather than filtering blind.
    identified = rom.get("is_identified")
    if identified is None:
        # Every provider, not a hand-picked subset: a ROM matched only through
        # RetroAchievements or Hasheous is still identified, and checking three
        # of the nine fields made `only_matched` skip it.
        identified = bool(romm_repin.metadata_ids(rom))
    if rule["only_matched"] and not identified:
        return False
    return not (rule["only_unmatched"] and identified)


def _resolve_source(path: str) -> str | None:
    """Absolute, symlink-free source path, or None when it cannot be resolved.

    Its own function so the sweep can push it into a worker thread: resolving a
    path stats every component, and doing that per ROM on the event loop stalls
    the whole app for the length of a catalog.
    """
    try:
        return str(Path(path).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return None


def _compression_arg(rule: dict) -> str | None:
    """The rule's compression as the job pipeline expects it: ``"codec:level"``.

    The level is not a separate parameter anywhere downstream -- ``/api/jobs``
    encodes it into the compression string and the tools parse it back out --
    so sending the codec alone silently dropped the level the operator set.
    """
    codec = rule["compression"]
    level = rule["compression_level"]
    if level is None:
        return codec or None
    # A level with no codec is meaningful: nsz reads the empty layout part as
    # "tool default" and still honours the level, so ":18" must not collapse to
    # None or the operator's level would be silently discarded.
    return f"{codec or ''}:{level}"


def _accepts_source(tool, spec, path: str) -> bool:
    """Whether this *mode* takes *path* as an input unit.

    The mode, not the tool. `converts_path` answers a tool-wide,
    direction-agnostic question, and for a tool whose directions have
    different inputs it answers backwards: chdman deliberately drops `.chd`
    from its tool-level extensions so a finished CHD is not badged as a
    convertible source, yet `.chd` is exactly what its extract and copy modes
    take. Gating the sweep on it rejected every real `copy` source and
    accepted `.iso` files that mode cannot consume.

    Directory modes (makeps3iso's decrypted PS3 folder) declare no input
    extensions at all, so the extension match rejects every one of them; the
    registry says which predicate applies.

    Where the tool *does* claim the extension, its own predicate still gets
    the final word: that is where the per-file refinements live (jwud leaves
    the secondary members of a split Wii U dump visible but non-convertible,
    since only `game_part1.wud` drives the set).
    """
    if InputKind.DIRECTORY in spec.input_kinds:
        return tool.accepts_directory(path)
    if spec.input_extensions and match_extension(path, spec.input_extensions) is None:
        return False
    if match_extension(path, tool.input_extensions) is not None:
        return tool.converts_path(path)
    return bool(spec.input_extensions)


async def _converted_map(platform_id: str) -> dict:
    """What this rule has produced, as ``{rom_id: {"path", "pre"}}``.

    Provenance the filesystem cannot supply on its own. ``skip`` is idempotent
    from the destination alone, but the other two policies are not:

    * ``overwrite`` resolves an existing destination as queueable by
      definition, so a standing rule reconverts and rewrites the same
      multi-gigabyte image every interval, forever;
    * ``rename`` moves to the next free suffix each time, so the same source
      accumulates ``Game_1``, ``Game_2``, ... until the volume fills.

    The record is *not* "this was queued" -- queueing is not producing, and a
    job that is cancelled, or interrupted by a restart, or fails in the
    converter would otherwise mark its ROM done forever. What is stored is the
    destination and a fingerprint of whatever occupied it at planning time, so
    "did this actually happen" is answered by the destination having changed
    since. A conversion that never ran leaves it untouched and the next sweep
    picks the ROM up again, with no bookkeeping to keep in sync and nothing to
    reconcile after a crash.

    Bounded by the platform's ROM count, which is the same order as the catalog
    each sweep already holds in memory.
    """
    state = await get_state()
    entry = state.get(str(platform_id), {})
    stored = entry.get("converted")
    out = {str(k): v for k, v in stored.items() if isinstance(v, dict)} if isinstance(
        stored, dict,
    ) else {}
    # An earlier shape recorded ids alone, with no evidence of production.
    # Honour them as done rather than reconverting a library on upgrade.
    for rom_id in entry.get("converted_ids") or []:
        out.setdefault(str(rom_id), {"path": "", "pre": None})
    return out


def _was_produced(remembered: dict | None) -> bool:
    """Whether the conversion recorded in *remembered* actually happened.

    The job's own outcome first, and the filesystem only as a fallback. A
    changed destination is not proof of success: a failed or cancelled
    ``overwrite`` job can unlink the previous artifact or leave a partial one
    behind, which looks exactly like a fresh output from the outside -- and
    the ROM would then be skipped by every later sweep until the operator
    cleared the history by hand.

    The job id is the precise answer while the queue still remembers it, which
    covers the window that matters: the next sweep is minutes away. Job history
    is pruned and does not survive a restart, so after that the fingerprint is
    the best evidence left -- weaker, but it only ever mis-reads a *destroyed*
    destination, and `Forget history` is the way back from that.

    ``pre`` is None for a legacy record that carries no evidence either way;
    those are trusted, since the alternative is reconverting a whole library.
    """
    if not remembered:
        return False
    job_id = remembered.get("job_id")
    if job_id:
        job = job_manager.get_job(job_id)
        if job is not None:
            if job.status in (JobStatus.QUEUED, JobStatus.PROCESSING):
                return True  # in flight; not a candidate either way
            return job.status == JobStatus.COMPLETED
    if remembered.get("pre") is None:
        return True
    return romm_repin.path_fingerprint(remembered["path"]) != remembered["pre"]


async def _mark_converted(platform_id: str, produced: list[tuple]) -> None:
    """Remember ``(rom_id, destination, pre_fingerprint, job_id)`` per queued ROM."""
    entries = {
        str(rid): {"path": dest, "pre": pre, "job_id": job_id}
        for rid, dest, pre, job_id in produced if rid is not None
    }
    if not entries:
        return
    state = await get_state()
    entry = dict(state.get(str(platform_id)) or {})
    merged = dict(entry.get("converted") or {})
    merged.update(entries)
    entry["converted"] = merged
    state[str(platform_id)] = entry
    await preferences_store.put(STATE_KEY, state)


def _inspect_candidate(rom: dict, rule: dict, tool, spec, remembered: dict | None) -> dict:
    """Every disk-touching question about one candidate, answered in one hop.

    Mapping the RomM record onto a local path, the volume check, the input
    predicate, path resolution and the duplicate-policy destination all stat
    the filesystem — and on the NFS/SMB/rclone deployments this integration
    explicitly supports, a mount that stops answering would otherwise block the
    event loop for the length of the catalog.

    The name filters run here too, and first. They touch no disk, but they
    evaluate an operator-supplied regex against operator-supplied names, and
    a pattern like ``(a+)+$`` backtracks for minutes on one long name -- on
    the event loop that is the whole app, not one sweep. Answering ahead of
    the path mapping also means a filtered-out ROM still costs no disk work.

    ``skip`` is None when the ROM should be queued, otherwise the reason.
    ``resolved`` is still filled in whenever it could be, because the caller
    checks it against the in-flight set before acting on ``skip``.
    """
    miss = {
        "skip": "unresolvable", "resolved": None, "destination": None,
        "path": None, "pre": None,
    }
    if not _passes_filters(rom, rule):
        return {**miss, "skip": "filtered"}
    # Already produced by this rule. Asked here rather than on the event loop
    # because the answer is a stat of the destination this rule last chose --
    # queueing is not producing, so the record alone does not settle it.
    if _was_produced(remembered):
        return {**miss, "skip": SKIP_EXISTING}
    path = romm_client.local_path(rom)
    if not path or not is_within_configured_volumes(path):
        return miss
    if not _accepts_source(tool, spec, path):
        return {**miss, "skip": "unconvertible", "path": path}
    resolved = _resolve_source(path)
    if resolved is None:
        return miss
    # RomM's catalog outlives the files it describes: an entry whose ROM was
    # moved or deleted outside RomM still lists a local_path, and queueing it
    # spends a worker slot to fail in the tool. Same stat call either way, so
    # asking here costs nothing the resolve did not already pay for.
    if not os.path.exists(resolved):
        return {**miss, "skip": "missing", "path": path}
    destination, decision = resolve_destination(
        tool, path, rule["mode"], rule["output_dir"], rule["duplicate_action"],
    )
    # The rule's output_dir is validated when the rule is saved, but that is
    # not the only way a destination is chosen: with no output_dir the tool
    # derives one from the source, and a mode can place it beside the source
    # rather than inside it (makeps3iso writes the ISO next to the decrypted
    # folder). Check what was actually resolved -- but only when the candidate
    # is going to be queued, or this would mask the policy's own answer (a
    # skipped destination is reported as None).
    if decision == QUEUE and (
        not destination or not is_within_configured_volumes(destination)
    ):
        return {**miss, "skip": "outside_volumes", "path": path}
    return {
        "path": path,
        "skip": None if decision == QUEUE else decision,
        "resolved": resolved,
        "destination": destination,
        # What occupies the destination right now, so a later sweep can tell
        # "the conversion ran" from "the job never happened".
        "pre": romm_repin.path_fingerprint(destination) if destination else "",
    }


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

    Serialised process-wide, so a scheduled tick and a manual run cannot both
    plan against the same "nothing is in flight" snapshot. A preview is a real
    sweep in every respect but the queueing, so it takes the lock too rather
    than reporting candidates a concurrent run is already claiming.
    """
    async with _sweep_lock:
        return await _sweep_locked(
            platform_ids=platform_ids,
            ignore_schedule=ignore_schedule,
            dry_run=dry_run,
            overall_limit=overall_limit,
        )


async def _sweep_locked(
    *,
    platform_ids: list[int] | None,
    ignore_schedule: bool,
    dry_run: bool,
    overall_limit: int | None,
) -> dict:
    """The sweep body. Always called with ``_sweep_lock`` held."""
    cfg = romm_settings.effective()
    rules = await get_rules()
    state = await get_state()
    now = datetime.now(timezone.utc)

    result: dict = {
        "queued": 0,
        "considered": 0,
        "repins_recorded": 0,
        "skipped_existing": 0,
        "skipped_active": 0,
        "skipped_filtered": 0,
        "skipped_unconvertible": 0,
        "skipped_missing": 0,
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
    # Destinations already claimed this sweep. Two sources can resolve to the
    # same output -- a PS3 folder and its sibling ISO, or two entries RomM
    # lists for one file -- and under `overwrite` both resolve as queueable,
    # so without this the second job overwrites the first's output and, with
    # delete_on_verify, both sources are deleted for one surviving file.
    claimed_destinations: set[str] = set()

    for platform_id in ordered:
        if result["queued"] >= cap:
            result["stopped_reason"] = "limit"
            break
        rule = rules[platform_id]
        if wanted is not None and platform_id not in wanted:
            continue
        # `enabled` gates unconditionally: a paused platform must stay paused
        # even when the operator presses Run now, which passes
        # ignore_schedule=True. The one exception is naming platforms
        # explicitly -- that is a deliberate per-platform action, and it keeps
        # the "configure a rule, leave the scheduler off, run it by hand"
        # workflow working.
        if not rule["enabled"] and wanted is None:
            continue
        # ignore_schedule bypasses only the clock (interval, window, weekday).
        if not ignore_schedule and not _is_due(rule, state.get(platform_id, {}), now):
            continue

        try:
            spec = registry.spec(rule["mode"])
            tool = registry.for_mode(rule["mode"])
        except KeyError:
            continue

        # A rule outlives the install it was written against: the editor only
        # offers ready tools, but a saved rule keeps firing after its binary
        # is removed or a container is rebuilt without it, and every job it
        # queues fails at launch. Ask before queueing, and say so.
        if not await tool.is_ready():
            logger.warning(
                "romm_auto: %s is not installed, skipping platform %s",
                spec.tool_id, platform_id,
            )
            result["errors"].append(
                {"platform_id": int(platform_id), "error": "tool_not_ready"},
            )
            continue

        # Belt and braces on the destination: `normalize_rule` refuses an
        # out-of-volume output_dir at save time, but a rules blob can also be
        # edited straight in the database.
        if rule["output_dir"] and not is_within_configured_volumes(rule["output_dir"]):
            logger.warning(
                "romm_auto: skipping platform %s, output_dir outside volumes",
                platform_id,
            )
            result["errors"].append(
                {"platform_id": int(platform_id), "error": "output_dir_outside_volumes"},
            )
            continue

        try:
            roms = await run_in_threadpool(romm_client.roms, int(platform_id))
        except RommError as exc:
            logger.warning("romm_auto: platform %s unreadable: %s", platform_id, exc)
            result["errors"].append(
                {"platform_id": int(platform_id), "error": "unreadable"},
            )
            continue

        # The editor narrows the target list, but a rule can outlive the tool
        # set it was written against (or be hand-edited), and `converts_path` is
        # extension-based -- it cannot tell a GameCube .iso from a PS2 one. Ask
        # the registry, using the slug RomM stamps on every record.
        slug = next(
            (r.get("platform_slug") for r in roms if r.get("platform_slug")), None,
        )
        # Per mode, not per tool. A composite tool is a shell that belongs to
        # no system -- the chain tool has a GameCube mode and a PS2 mode, so it
        # passes a tool-level check on both, and a rule saved against the wrong
        # one would convert a disc to a format for the other console.
        if not registry.mode_allows_platform(rule["mode"], slug):
            logger.info(
                "romm_auto: %s is not a %s mode, skipping platform %s",
                rule["mode"], slug, platform_id,
            )
            result["errors"].append(
                {"platform_id": int(platform_id), "error": "tool_wrong_for_platform"},
            )
            continue

        per_platform_cap = min(rule["max_per_run"], cap - result["queued"])
        # Asked once per platform, not per ROM: whether this target format keeps
        # RomM's DAT match is a property of the mode. Honours the same
        # `repin_enabled` switch the manual path reads.
        repin_needed = (
            bool(cfg.get("repin_enabled", True))
            and romm_repin.mode_needs_repin(spec.output_ext)
        )
        # Only the destructive policies need provenance: `skip` is already
        # idempotent from the destination alone, and consulting the record
        # there would refuse to reconvert an output the operator deliberately
        # deleted.
        converted = (
            await _converted_map(platform_id)
            if rule["duplicate_action"] != "skip"
            else {}
        )
        batch: list[str] = []
        rom_by_path: dict[str, dict] = {}
        destinations: dict[str, str] = {}
        resolved_by_path: dict[str, str] = {}
        pre_by_path: dict[str, str] = {}
        considered = 0

        for rom in sorted(roms, key=_rom_sort_key(rule)):
            if len(batch) >= per_platform_cap:
                break
            considered += 1
            # One hop to a worker thread for every disk-touching check on this
            # candidate — the local-path mapping and volume check included,
            # since both resolve paths against a mount that may be remote.
            decision = await run_in_threadpool(
                _inspect_candidate, rom, rule, tool, spec,
                converted.get(str(rom.get("id"))),
            )
            path = decision["path"]
            if decision["skip"] == "filtered":
                result["skipped_filtered"] += 1
                continue
            if decision["skip"] == "unresolvable":
                continue
            if decision["skip"] == "unconvertible":
                result["skipped_unconvertible"] += 1
                continue
            if decision["skip"] == "missing":
                result["skipped_missing"] += 1
                continue
            if decision["skip"] == "outside_volumes":
                result["skipped_unconvertible"] += 1
                continue
            if decision["resolved"] in active_paths:
                result["skipped_active"] += 1
                continue
            if decision["skip"] == SKIP_EXISTING:
                result["skipped_existing"] += 1
                continue
            if decision["skip"] == SKIP_LOCKED:
                result["skipped_active"] += 1
                continue
            if decision["destination"] in claimed_destinations:
                result["skipped_active"] += 1
                continue
            claimed_destinations.add(decision["destination"])
            batch.append(path)
            rom_by_path[path] = rom
            destinations[path] = decision["destination"]
            resolved_by_path[path] = decision["resolved"]
            pre_by_path[path] = decision["pre"]

        result["considered"] += considered
        summary = {
            "platform_id": int(platform_id),
            "mode": rule["mode"],
            "queued": 0,
            # Always present, so a caller reading a preview does not have to
            # tell "no re-pins" apart from "this key only exists on real runs".
            "repins_recorded": 0,
            "considered": considered,
        }

        if batch and not dry_run:
            try:
                # delete-on-verify needs its snapshot up front, or
                # `_process_job` refuses to delete ("Delete plan snapshot
                # missing") and fails every job after doing the full
                # conversion. Same helper the manual path uses.
                snapshots = None
                if rule["delete_on_verify"]:
                    snapshots = {}
                    for path in batch:
                        snapshots[path] = await run_in_threadpool(
                            build_delete_snapshot, path,
                        )

                jobs = await job_manager.create_batch_jobs(
                    batch,
                    ConversionMode(rule["mode"]),
                    output_dir=rule["output_dir"],
                    compression=_compression_arg(rule),
                    delete_on_verify=rule["delete_on_verify"],
                    delete_snapshots=snapshots,
                    split=rule["split"],
                    # delete_on_verify already verifies as a precondition, so
                    # asking for both would not verify twice -- but a rule that
                    # only wants the check must still get it.
                    verify_after=rule["verify_after"],
                    # The duplicate policy was resolved per candidate above;
                    # hand the answer down rather than let the queue derive a
                    # second, possibly different one.
                    output_paths=destinations,
                    allow_overwrite=rule["duplicate_action"] == "overwrite",
                )
                summary["queued"] = len(jobs)
                result["queued"] += len(jobs)

                # Only now, with the jobs actually queued: snapshot the RomM
                # metadata for formats RomM cannot hash-match, or an automatic
                # RVZ sweep destroys exactly the metadata the re-pin feature
                # exists to protect. Recording *before* the queue call would
                # leave pending rows behind for conversions that never ran, and
                # those rows re-pin whatever later lands on that path. The
                # records the sweep already holds carry the provider ids, so
                # this costs no extra catalog fetch.
                #
                # Every path in `batch` is queued or none is: create_jobs_atomic
                # takes the queue lock once and checks backpressure for the
                # whole set, so reaching here means the batch went in.
                repin_count = 0
                if repin_needed:
                    for path in batch:
                        rom = rom_by_path.get(path)
                        ids = romm_repin.metadata_ids(rom) if rom else {}
                        if not ids:
                            continue
                        # The fingerprint comes from candidate inspection, not
                        # from now: on an idle queue a fast conversion can
                        # finish between `create_batch_jobs` returning and this
                        # call, and stating the destination here would record
                        # the *finished* output as the pre-conversion state --
                        # after which the settler sees nothing change and
                        # abandons the row, losing exactly the metadata this
                        # exists to protect.
                        if await run_in_threadpool(
                            romm_repin.record, rom, destinations[path], ids,
                            rule["mode"], pre_by_path[path],
                        ):
                            repin_count += 1
                summary["repins_recorded"] = repin_count
                result["repins_recorded"] += repin_count

                # Record what this rule has now converted, so `overwrite` and
                # `rename` stop here instead of reconverting the same sources
                # every interval. Only after the queue accepted them.
                if rule["duplicate_action"] != "skip":
                    # Paired by source path, not by position: the record is
                    # only meaningful if it names the job whose outcome decides
                    # whether this ROM was really converted.
                    job_by_path = {j.file_path: j.id for j in jobs}
                    await _mark_converted(platform_id, [
                        (rom_by_path[path].get("id"), destinations[path],
                         pre_by_path[path], job_by_path.get(path))
                        for path in batch
                    ])

                # Claim them immediately so a later platform in the same sweep
                # cannot queue the same source twice. Already resolved during
                # selection, so this costs no further disk work.
                active_paths.update(resolved_by_path.values())
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
                summary["queue_failed"] = True
        elif batch:
            summary["queued"] = len(batch)
            result["queued"] += len(batch)

        summary["candidates"] = [Path(p).name for p in batch[:25]]
        result["platforms"].append(summary)
        # A real run updates the schedule clock; a preview must not, or looking
        # at what *would* happen would postpone the run that should. Nor does a
        # run that failed to queue anything: advancing the clock there would
        # make the platform sit out a full interval over a transient error.
        if not dry_run and not summary.get("queue_failed"):
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
        except Exception:
            # A sweep failure must never kill the scheduler; the next tick
            # retries. Shutdown still stops it: CancelledError derives from
            # BaseException, so it passes straight through this handler.
            logger.warning("romm_auto: sweep failed", exc_info=True)
