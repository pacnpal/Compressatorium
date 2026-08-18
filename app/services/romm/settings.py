"""Runtime-editable RomM settings.

Everything about the RomM integration is configurable from the app: connection,
credentials, library location, unattended-conversion policy, and the
per-platform rules. Environment variables remain the *first-run defaults* — an
operator can still bake a working configuration into a compose file — but the
saved settings win once anything has been set, and take effect without a
restart.

Layering, highest priority first:

1. what the operator saved in the app (SQLite ``preferences`` row)
2. the environment (``ROMM_URL`` / ``ROMM_TOKEN`` / ``ROMM_LIBRARY_ROOT`` / …)
3. the field's built-in default

A **sync** snapshot is what the client actually reads: ``RommClient``'s methods
are blocking and run through ``run_in_threadpool``, so they cannot await a
store. ``effective()`` serves a cached dict that ``load()`` primes at startup
and ``save()`` refreshes, which also keeps a settings read off the hot path of
every catalog request.

The token is held here too, and that is a deliberate trade: the alternative is
an env-only secret the user cannot change without redeploying, which is exactly
what "configure it in the app" rules out. It is never returned by the API —
callers only ever learn whether one is set — and the store is the same SQLite
file that already holds the rest of the app's state.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any

from logging_setup import get_logger
from services.preferences_store import preferences_store

logger = get_logger("romm_settings")

SETTINGS_KEY = "romm.settings"

# Persisted alongside the settings: the operator cleared the token *in the app*.
# Without it, clearing a token on a deployment that also sets ``ROMM_TOKEN``
# would silently fall straight back to the environment value, so the UI would
# report the token gone while every request still authenticated with it.
# (The constant is deliberately not named TOKEN_*: it is a preference key, not
# a credential, and the secret-scanners read the name.)
CLEARED_KEY = "token_cleared"

# Persisted alongside the settings: an identity change whose cleanup has not
# been confirmed. Written in the same row as the new URL/library root so the
# two commit together; see `cleanup_owed`.
CLEANUP_KEY = "identity_cleanup_pending"

# field -> (env var, default, kind). One table, so a new setting is one row and
# every layer (env fallback, coercion, serialization) picks it up for free.
_FIELDS: dict[str, tuple[str, Any, str]] = {
    "url": ("ROMM_URL", "", "str"),
    "library_root": ("ROMM_LIBRARY_ROOT", "", "str"),
    "auto_convert": ("ROMM_AUTO_CONVERT", False, "bool"),
    "auto_convert_interval_minutes": ("ROMM_AUTO_CONVERT_INTERVAL_MINUTES", 60, "int"),
    "auto_convert_max_per_run": ("ROMM_AUTO_CONVERT_MAX_PER_RUN", 25, "int"),
    # Whether a conversion to a format RomM cannot hash-match records the
    # source's metadata so it can be re-applied after RomM rescans.
    "repin_enabled": ("ROMM_REPIN", True, "bool"),
    # Re-apply metadata automatically whenever the RomM view loads, rather than
    # only when the operator presses the button.
    "repin_on_load": ("ROMM_REPIN_ON_LOAD", True, "bool"),
    # Days after which a pending re-pin whose output never appeared is retired.
    "repin_abandon_days": ("ROMM_REPIN_ABANDON_DAYS", 7, "int"),
    # Verify each conversion before its metadata is re-applied.
    "verify_after_convert": ("ROMM_VERIFY_AFTER_CONVERT", False, "bool"),
    # Delete the source once the new output verifies. Off by default: it is
    # destructive, and the RomM library is the user's collection.
    "delete_source_after_verify": ("ROMM_DELETE_SOURCE_AFTER_VERIFY", False, "bool"),
}

_INT_BOUNDS = {
    "auto_convert_interval_minutes": (5, 10080),
    "auto_convert_max_per_run": (1, 1000),
    "repin_abandon_days": (1, 365),
}

_cache: dict[str, Any] | None = None
_token: str | None = None
_token_cleared = False
_cleanup_pending = False
# Bumped by every save that moves the identity. Anything that reads the catalog
# and then writes rows against it -- the re-pin plan -- carries this across its
# awaits and refuses to write if it changed underneath. See `identity_generation`.
_identity_generation = 0
_lock = threading.Lock()
# Serialises the read-modify-write in save(). Two concurrent saves would
# otherwise each read the pre-patch row and the later put would drop the
# earlier one's fields.
_save_lock = asyncio.Lock()


def _coerce(kind: str, value: Any, default: Any, field: str | None = None) -> Any:
    try:
        if kind == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if kind == "int":
            out = int(value)
            if field in _INT_BOUNDS:
                low, high = _INT_BOUNDS[field]
                out = max(low, min(high, out))
            return out
        return str(value)
    except (TypeError, ValueError):
        return default


def _from_env() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field, (env, default, kind) in _FIELDS.items():
        raw = os.environ.get(env)
        out[field] = default if raw is None else _coerce(kind, raw, default, field)
    return out


def _merge(stored: Any) -> dict[str, Any]:
    merged = _from_env()
    if isinstance(stored, dict):
        for field, (_, default, kind) in _FIELDS.items():
            if field in stored and stored[field] is not None:
                merged[field] = _coerce(kind, stored[field], default, field)
    return merged


def effective() -> dict[str, Any]:
    """The settings in force right now. Safe to call from a worker thread."""
    with _lock:
        if _cache is not None:
            return dict(_cache)
    # Never primed (a unit test, or a call before startup): fall back to the
    # environment rather than reporting the feature unconfigured.
    return _from_env()


def token() -> str | None:
    """The RomM API token in force, or None. Never leaves the backend."""
    with _lock:
        if _token is not None:
            return _token or None
        if _token_cleared:
            # Cleared in the app: an env token must not resurrect it.
            return None
    return os.environ.get("ROMM_TOKEN") or None


async def load(*, force: bool = False) -> dict[str, Any]:
    """Prime the cache from the store. Called once at startup."""
    global _cache, _token, _token_cleared, _cleanup_pending
    with _lock:
        if _cache is not None and not force:
            return dict(_cache)
    stored = await preferences_store.get(SETTINGS_KEY)
    merged = _merge(stored)
    row = stored if isinstance(stored, dict) else {}
    stored_token = row.get("token")
    with _lock:
        _cache = merged
        _token = stored_token if stored_token else None
        _token_cleared = bool(row.get(CLEARED_KEY))
        _cleanup_pending = bool(row.get(CLEANUP_KEY))
    return dict(merged)


async def save(
    patch: dict[str, Any], *, cleanup_pending: bool = False,
) -> dict[str, Any]:
    """Apply *patch* and persist it. Unknown keys are ignored.

    Only the fields present in *patch* change, so a form that edits one card
    cannot blank the settings another card owns.

    *cleanup_pending* stamps the row with :data:`CLEANUP_KEY` in the *same*
    write that installs the new identity, so the two cannot commit apart. See
    :func:`cleanup_owed`.
    """
    global _cache, _token, _token_cleared, _cleanup_pending, _identity_generation
    # Read-modify-write, so it has to be one critical section: concurrent saves
    # of two different cards would otherwise each read the pre-patch row and the
    # later put would silently discard the earlier one.
    async with _save_lock:
        stored = await preferences_store.get(SETTINGS_KEY)
        stored = dict(stored) if isinstance(stored, dict) else {}

        for field, (_, default, kind) in _FIELDS.items():
            if field in patch and patch[field] is not None:
                stored[field] = _coerce(kind, patch[field], default, field)

        # Clearing is an explicit flag rather than a reserved token value: a
        # magic string in a credential field is both worse to use and
        # indistinguishable from someone's actual token.
        if patch.get("clear_token"):
            stored.pop("token", None)
            stored[CLEARED_KEY] = True
        elif isinstance(patch.get("token"), str) and patch["token"].strip():
            stored["token"] = patch["token"].strip()
            stored.pop(CLEARED_KEY, None)
        # An empty or omitted token means "leave the stored one alone", so a
        # form that cannot display the secret does not blank it just by being
        # submitted.

        if cleanup_pending:
            stored[CLEANUP_KEY] = True

        await preferences_store.put(SETTINGS_KEY, stored)
        merged = _merge(stored)
        with _lock:
            _cache = merged
            _token = stored.get("token") or None
            _token_cleared = bool(stored.get(CLEARED_KEY))
            _cleanup_pending = bool(stored.get(CLEANUP_KEY))
            if cleanup_pending:
                # Only an identity move asks for cleanup, so this is the one
                # signal that says "anything holding the old catalog is stale".
                _identity_generation += 1
        return dict(merged)


def identity_generation() -> int:
    """A counter that changes whenever the RomM instance or library root does.

    The settings route serialises against sweeps and the settle pass, but a
    manual re-pin plan holds neither lock -- and it reads the catalog, then
    writes rows carrying that catalog's provider ids, with awaits in between.
    A change landing in that window retires the existing rows and installs the
    new identity, and the plan then inserts old ids into a fresh row that the
    cleanup never saw. Reading this before and after is what lets the plan
    notice and refuse.
    """
    with _lock:
        return _identity_generation


def cleanup_owed() -> bool:
    """Is a post-identity-change cleanup still outstanding?

    The conversion history and the pending re-pin rows belong to whichever RomM
    and library they were recorded against, so a change of either has to clear
    them. Save and cleanup are two operations, and either can be the one that
    survives a crash:

    * cleanup first, then a failed save -> the old identity stays in force with
      its history gone and its snapshots retired, unrecoverably;
    * save first, then a failed cleanup -> the retry compares the new values
      with themselves, concludes nothing moved, and leaves the previous
      instance's ids live against the new one, forever.

    So the marker is written in the same row as the new identity, and only
    cleared once the cleanup has actually run. Whichever half is interrupted,
    the marker is what makes the other half replay -- and both halves are
    idempotent, so replaying costs nothing.
    """
    with _lock:
        if _cache is not None:
            return bool(_cleanup_pending)
    return False


async def clear_cleanup_owed() -> None:
    """Record that the cleanup this identity change owed has been done."""
    global _cleanup_pending
    async with _save_lock:
        stored = await preferences_store.get(SETTINGS_KEY)
        stored = dict(stored) if isinstance(stored, dict) else {}
        if stored.pop(CLEANUP_KEY, None) is None:
            with _lock:
                _cleanup_pending = False
            return
        await preferences_store.put(SETTINGS_KEY, stored)
        with _lock:
            _cleanup_pending = False


def public(values: dict[str, Any] | None = None) -> dict[str, Any]:
    """Settings as the API returns them: everything except the secret itself."""
    values = values if values is not None else effective()
    out = dict(values)
    out.pop("token", None)
    out["token_set"] = bool(token())
    # Tell the UI which fields the environment pins a default for, so it can
    # explain where a value came from on a fresh install.
    out["env_defaults"] = sorted(
        field for field, (env, _, _) in _FIELDS.items() if os.environ.get(env)
    )
    return out


def reset_for_tests() -> None:
    """Drop the cached snapshot (tests re-prime against their own store)."""
    global _cache, _token, _token_cleared
    with _lock:
        _cache = None
        _token = None
        _token_cleared = False
