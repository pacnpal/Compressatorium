"""Remote hash lookup against a Hasheous server (https://hasheous.org).

Hasheous indexes 14 signature sources -- Redump, No-Intro, TOSEC, MAMEArcade,
MAMEMess, **MAMERedump**, WHDLoad, RetroAchievements, FBNeo and friends -- so it
is a strict superset of the MAMERedump DATs this app syncs locally, and it also
carries platform / publisher / year / region plus links out to IGDB,
TheGamesDB and RetroAchievements.

It is consulted **only** when the locally imported DATs don't know a hash (see
``routes.dat._lookup_sha1_match``), and only when the operator opts in, so
local matching stays instant and fully offline.

Deliberately stdlib-only, mirroring ``services.dat_sync``: the project has no
``httpx``/``requests`` dependency and doesn't need one for a single GET.

Upstream shape (verified against the live API):

* ``GET /api/v1/Lookup/ByHash/sha1/{sha1}`` -- no authentication required.
* A hit returns 200 with the game/signature JSON.
* A **miss returns 404**, not 200-with-null.
* There is no bulk endpoint: a JSON array body means "several hashes for one
  object", not a batch of files, so this is one request per file.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger

from config import settings

logger = get_logger("hasheous")

_USER_AGENT = "compressatorium-hasheous/1.0"

# A hit is ~19 KB. Anything far beyond that means base_url points at something
# that isn't Hasheous, so refuse it rather than buffering an unbounded body.
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024

_SHA1_RE = re.compile(r"[0-9a-f]{40}")


class HasheousUnavailable(Exception):
    """Transient failure talking to Hasheous.

    Raised for timeouts, 5xx, and unparseable responses -- everything *except*
    a genuine 404 miss. Callers must surface a non-cacheable error rather than
    record ``matched: False``, otherwise one network blip permanently caches
    every in-flight file as unmatched.
    """


def enabled() -> bool:
    """True when the operator has opted in to remote lookups."""
    return bool(getattr(settings, "hasheous_enabled", False))


def _require_https(url: str) -> None:
    """Raise ValueError if *url* does not use the https scheme.

    Mirrors ``dat_sync._require_https``. A plain-http base URL would put file
    hashes on the wire in the clear.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"Only https URLs are permitted; got scheme '{parsed.scheme}'")


def _lookup_url(sha1: str) -> str:
    base = str(getattr(settings, "hasheous_base_url", "") or "").rstrip("/")
    return f"{base}/api/v1/Lookup/ByHash/sha1/{sha1}"


def _timeout() -> int:
    return max(1, int(getattr(settings, "hasheous_timeout", 15) or 15))


def _fetch_json(url: str) -> dict | None:
    """GET *url* and decode the JSON body. ``None`` means a clean 404 miss.

    The single seam tests patch (the same pattern as
    ``tests/test_dat_sync.py`` patching ``sync_service._fetch_json``).
    """
    _require_https(url)
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:  # nosec B310
            raw = resp.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # The documented "no such hash" answer, not a failure.
            return None
        raise HasheousUnavailable(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise HasheousUnavailable(str(exc)) from exc

    if len(raw) > _MAX_RESPONSE_BYTES:
        raise HasheousUnavailable("response exceeded size limit")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HasheousUnavailable("unparseable response") from exc
    if not isinstance(data, dict):
        raise HasheousUnavailable("unexpected response shape")
    return data


def _text(value) -> str | None:
    """Coerce a Hasheous field that may be a string OR a ``{code: name}`` map.

    ``rom.country`` and ``game.year`` come back as a bare string on some
    signature sources and as a (frequently empty) dict on others.
    """
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict) and value:
        parts = [str(v).strip() for v in value.values() if str(v).strip()]
        return ", ".join(parts) or None
    return None


def _normalize(data: dict) -> dict:
    """Map a Hasheous response onto the record shape ``dat_store`` returns.

    Same keys as ``dat_store.lookup_sha1`` (so the caller builds one result
    dict for both sources), plus the extra identity fields Hasheous carries.

    ``dat_id`` is always ``None``: it is a foreign key into the *local* ``dats``
    table, and a remote hit has no row there. ``dat_store`` already nulls out
    unknown ``dat_id`` values before writing, so this keeps remote rows stable
    across re-runs instead of relying on that guard.
    """
    signature = data.get("signature") or {}
    rom = signature.get("rom") or {}
    game = signature.get("game") or {}

    links = [
        {"source": entry.get("source"), "link": entry.get("link")}
        for entry in (data.get("metadata") or [])
        if isinstance(entry, dict)
        and entry.get("status") == "Mapped"
        and entry.get("link")
    ]

    return {
        "dat_id": None,
        # Which preservation DAT the hash actually came from (Redump,
        # No-Intro, TOSEC, MAMERedump, ...). Shown where a local match shows
        # its DAT name.
        "dat_name": _text(rom.get("signatureSource")) or "Hasheous",
        "game_name": _text(data.get("name")) or _text(game.get("name")),
        "rom_name": _text(rom.get("name")),
        "source": "hasheous",
        "platform": _text((data.get("platform") or {}).get("name")),
        "publisher": (
            _text((data.get("publisher") or {}).get("name"))
            or _text(game.get("publisher"))
        ),
        "year": _text(game.get("year")),
        "region": _text(rom.get("country")) or _text(game.get("country")),
        "hasheous_id": data.get("id"),
        "metadata_links": links,
    }


async def lookup(sha1: str) -> dict | None:
    """Look ``sha1`` up remotely; ``None`` when Hasheous has no such hash.

    Raises :class:`HasheousUnavailable` on any transient failure so the caller
    can return a non-cacheable error instead of a false negative.
    """
    normalized = (sha1 or "").strip().lower()
    if not _SHA1_RE.fullmatch(normalized):
        # Not a SHA1 we can put in a URL path; treat as "nothing to ask".
        return None

    data = await run_in_threadpool(_fetch_json, _lookup_url(normalized))
    if data is None:
        return None
    return _normalize(data)
