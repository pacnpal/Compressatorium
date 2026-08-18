"""Remote hash lookup against a Hasheous server (https://hasheous.org).

Hasheous indexes 14 signature sources -- Redump, No-Intro, TOSEC, MAMEArcade,
MAMEMess, **MAMERedump**, WHDLoad, RetroAchievements, FBNeo and friends -- so it
is a strict superset of the MAMERedump DATs this app syncs locally, and it also
carries platform / publisher / year / region plus links out to IGDB,
TheGamesDB and RetroAchievements.

It is consulted **only** when the locally imported DATs don't know a hash (see
``routes.dat._lookup_match``), and only when the operator opts in, so
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

import contextlib
import http.client
import json
import re
import ssl
import threading
import time
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

# How long to stop calling out after a failure. Without this, an outage during
# a 1,000-file scan costs 1,000 x hasheous_timeout -- over four hours at the
# default -- to learn the same fact once per file. Files still come back
# non-cacheable during the cooldown, just immediately.
_COOLDOWN_SECONDS = 60

_cooldown_lock = threading.Lock()
_unavailable_until = 0.0  # monotonic deadline; 0 == service presumed up


class HasheousUnavailable(Exception):
    """Transient failure talking to Hasheous.

    Raised for timeouts, 5xx, and unparseable responses -- everything *except*
    a genuine 404 miss. Callers must surface a non-cacheable error rather than
    record ``matched: False``, otherwise one network blip permanently caches
    every in-flight file as unmatched.
    """


# Runtime override set from the Web UI toggle, persisted in the preferences
# table and reloaded at startup. ``None`` means "no override, follow the env
# var". Toggling has to take effect without a container restart, which is why
# the flag is not read straight from Settings on every call.
#
# A plain module-level bool needs no lock: assignment is atomic, the container
# pins uvicorn to --workers 1 (see the note in routes/dat.py), and a lookup
# racing a toggle is harmless either way.
_enabled_override: bool | None = None


def enabled() -> bool:
    """True when remote lookups are switched on.

    The UI toggle wins when set; otherwise the ``COMPRESSATORIUM_HASHEOUS_ENABLED``
    environment default applies.
    """
    if _enabled_override is not None:
        return _enabled_override
    return bool(getattr(settings, "hasheous_enabled", False))


def env_default() -> bool:
    """What the environment alone would say, ignoring any UI override."""
    return bool(getattr(settings, "hasheous_enabled", False))


def set_enabled_override(value: bool | None) -> None:
    """Apply (or clear, with ``None``) the UI override."""
    global _enabled_override  # noqa: PLW0603, intentional module-level state
    _enabled_override = None if value is None else bool(value)
    # A toggle is the operator saying "try again": drop any active cooldown so
    # the next lookup actually goes out instead of reporting a stale outage.
    _clear_cooldown()


def override() -> bool | None:
    """The current override, or ``None`` when following the environment."""
    return _enabled_override


def base_url() -> str:
    return str(getattr(settings, "hasheous_base_url", "") or "").rstrip("/")


async def health() -> dict:
    """Probe the configured server so the UI can offer a 'Test connection'.

    Never raises: a failure is the answer the caller wants to display.
    """
    url = f"{base_url()}/api/v1/Healthcheck"
    started = time.monotonic()
    try:
        await run_in_threadpool(_probe, url)
    except (HasheousUnavailable, ValueError) as exc:
        # A failed probe IS an outage observation, so record it: the next match
        # then fails instantly instead of paying another full timeout to
        # rediscover what the operator just watched the button discover.
        _begin_cooldown()
        return {"ok": False, "url": url, "error": str(exc)}

    # ...and a successful probe clears it. Otherwise the button could report
    # "Reachable" while browsing kept answering "Hasheous unavailable" from a
    # stale cooldown for up to another minute -- the exact question the button
    # exists to settle.
    _clear_cooldown()
    return {
        "ok": True,
        "url": url,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }


def _probe(url: str) -> None:
    """GET *url*, raising HasheousUnavailable unless it answers 2xx."""
    _require_https(url)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with _deadline_of(_timeout()), _opener.open(  # nosec B310
            req, timeout=_timeout()
        ) as resp:
            resp.read(1024)
    except urllib.error.HTTPError as exc:
        raise HasheousUnavailable(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        # http.client.HTTPException covers IncompleteRead (a chunked response
        # cut short) and malformed status lines. It is neither an OSError nor a
        # URLError, so without it a truncated response escaped as a 500 and
        # skipped the cooldown -- a bulk job then re-contacted the failing
        # server once per file.
        raise HasheousUnavailable(f"{type(exc).__name__}: {exc}") from exc


def _require_https(url: str) -> None:
    """Raise ValueError if *url* does not use the https scheme.

    Mirrors ``dat_sync._require_https``. A plain-http base URL would put file
    hashes on the wire in the clear.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"Only https URLs are permitted; got scheme '{parsed.scheme}'")


class _HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect that would downgrade the transport.

    ``urlopen`` follows redirects on its own, and ``_require_https`` only sees
    the URL we start with -- so a misconfigured or hostile server could bounce
    a lookup to ``http://`` and put the file's SHA1 on the wire in the clear.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _require_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# The deadline for the request running on this thread, as a monotonic
# timestamp. Thread-local because lookups run in a threadpool and the socket
# below has no other way to learn which request it belongs to.
_deadline = threading.local()


class _DeadlineSSLSocket(ssl.SSLSocket):  # pylint: disable=abstract-method
    """A TLS socket that enforces one deadline across the whole request.

    ``urlopen(timeout=...)`` bounds each *socket operation*, not the request:
    every byte that arrives resets it. A server dripping slower than the
    timeout but never stopping therefore pins a lookup -- and the scan job
    around it -- indefinitely, without ever raising, so the cooldown never
    opens either. Measured against a real dripping server: an 18.5s
    ``open()`` under a 2s timeout, unbounded for a longer header.

    Enforcing it here rather than around the body read is what makes the bound
    real: every byte of the response, status line and headers included, arrives
    through ``recv_into``, so connect, headers and body share one deadline.

    The TLS handshake is the exception: measured, it makes **zero**
    ``recv_into`` calls, reading through the C layer instead, so
    ``do_handshake`` has to apply the deadline itself. It sets the remaining
    budget as the socket timeout, which bounds a peer that stalls or dies
    mid-handshake. A peer that drips a handshake record at a time, each within
    the remaining budget, is not fully bounded by this -- doing that properly
    means leaving urllib. It is called out in the design doc rather than
    silently implied.

    Two other things sit outside it. **A proxy CONNECT tunnel**: when
    ``HTTPS_PROXY`` is set, ``build_opener`` installs a ``ProxyHandler`` and
    ``http.client._tunnel`` reads the proxy's status line and headers off the
    *raw* socket, before ``wrap_socket`` exists, so a proxy dripping that
    response pins the request exactly the way a dripping origin server used to.
    This one is the same severity as the bugs that justified this class -- it
    never raises -- and it is only unfixed here because bounding it means a
    deadline-aware raw socket and a custom connection/handler pair, i.e. a
    fifth deadline mechanism. It belongs in a consolidation that covers
    CONNECT, handshake, headers and body from one place. Only reachable with a
    proxy configured.

    What the deadline also does not cover is DNS: ``urllib`` resolves the hostname
    before this socket exists, and ``getaddrinfo`` ignores socket timeouts. A
    stalled resolver is still bounded -- by ``/etc/resolv.conf`` (glibc default
    5s x 2 attempts per nameserver), not by ``hasheous_timeout`` -- and it
    *raises*, so it opens the cooldown and the rest of a scan short-circuits.
    That is the difference from a dripping server, which never raises at all.
    Bounding it properly would mean resolving on an abandonable thread; the
    trade is documented rather than taken.

    ``dup()`` is abstract on ``ssl.SSLSocket`` upstream (CPython raises
    ``NotImplementedError``), hence the pylint waiver: nothing here duplicates
    the socket, and overriding it would only re-raise the same error.
    """

    def do_handshake(self, *args, **kwargs):
        self._apply_deadline()
        return super().do_handshake(*args, **kwargs)

    def _apply_deadline(self) -> None:
        end = getattr(_deadline, "at", None)
        if end is None:
            return
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("lookup exceeded the overall timeout")
        self.settimeout(remaining)

    def recv_into(self, *args, **kwargs):
        self._apply_deadline()
        return super().recv_into(*args, **kwargs)


def _ssl_context() -> ssl.SSLContext:
    """The stock verifying context, with our socket class installed."""
    context = ssl.create_default_context()
    context.sslsocket_class = _DeadlineSSLSocket
    return context


_opener = urllib.request.build_opener(
    _HTTPSOnlyRedirectHandler,
    urllib.request.HTTPSHandler(context=_ssl_context()),
)


@contextlib.contextmanager
def _deadline_of(seconds: float):
    """Bound everything done inside to *seconds* total."""
    _deadline.at = time.monotonic() + seconds
    try:
        yield
    finally:
        _deadline.at = None


def _cooldown_remaining() -> float:
    """Seconds left before remote lookups are attempted again (0 when up)."""
    with _cooldown_lock:
        return max(0.0, _unavailable_until - time.monotonic())


def _begin_cooldown() -> None:
    global _unavailable_until  # noqa: PLW0603, intentional module-level state
    with _cooldown_lock:
        _unavailable_until = time.monotonic() + _COOLDOWN_SECONDS


def _clear_cooldown() -> None:
    global _unavailable_until  # noqa: PLW0603, intentional module-level state
    with _cooldown_lock:
        _unavailable_until = 0.0


def _lookup_url(sha1: str) -> str:
    # base_url(), not a second copy of the normalization: routes.dat stamps
    # cached misses with base_url() and cached_result_usable() compares against
    # that stamp, so the two must not be able to drift.
    return f"{base_url()}/api/v1/Lookup/ByHash/sha1/{sha1}"


def _timeout() -> int:
    return max(1, int(getattr(settings, "hasheous_timeout", 15) or 15))


def _fetch_json(url: str) -> dict | None:
    """GET *url* and decode the JSON body. ``None`` means a clean 404 miss.

    The single seam tests patch (the same pattern as
    ``tests/test_dat_sync.py`` patching ``sync_service._fetch_json``).
    """
    try:
        _require_https(url)
        # Built inside the guard: Request() itself raises ValueError on an
        # empty or schemeless URL (an unset COMPRESSATORIUM_HASHEOUS_URL), and
        # from outside the try that escaped as a 500 without opening the
        # cooldown -- so a bulk job repeated it once per file.
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
        )
        with _deadline_of(_timeout()), _opener.open(  # nosec B310
            req, timeout=_timeout()
        ) as resp:
            raw = resp.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # The documented "no such hash" answer, not a failure.
            return None
        raise HasheousUnavailable(f"HTTP {exc.code}") from exc
    except ValueError as exc:
        # A non-https base URL, or a redirect trying to downgrade to http (the
        # redirect handler raises from inside opener.open). Both are
        # configuration/transport failures, so they have to arrive as
        # HasheousUnavailable: a bare ValueError would escape the match path
        # as a 500 and skip the cooldown, so a bulk match would re-raise it
        # once per file.
        raise HasheousUnavailable(str(exc)) from exc
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        # http.client.HTTPException covers IncompleteRead (a chunked response
        # cut short) and malformed status lines. It is neither an OSError nor a
        # URLError, so without it a truncated response escaped as a 500 and
        # skipped the cooldown -- a bulk job then re-contacted the failing
        # server once per file.
        raise HasheousUnavailable(f"{type(exc).__name__}: {exc}") from exc

    if len(raw) > _MAX_RESPONSE_BYTES:
        raise HasheousUnavailable("response exceeded size limit")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HasheousUnavailable("unparseable response") from exc
    if not isinstance(data, dict):
        raise HasheousUnavailable("unexpected response shape")
    return data


def _obj(value) -> dict:
    """A nested field, defensively.

    Hasheous' documented shape nests objects under ``signature``, ``platform``
    and ``publisher``, but a malformed or hostile 200 can put a string there.
    Reaching ``.get`` on that raised ``AttributeError``, which is not
    ``HasheousUnavailable`` -- so it escaped the match path as a 500 and never
    opened the cooldown, once per file.
    """
    return value if isinstance(value, dict) else {}


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
    signature = _obj(data.get("signature"))
    rom = _obj(signature.get("rom"))
    game = _obj(signature.get("game"))

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
        "platform": _text(_obj(data.get("platform")).get("name")),
        "publisher": (
            _text(_obj(data.get("publisher")).get("name"))
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

    remaining = _cooldown_remaining()
    if remaining > 0:
        # Still unavailable, and still non-cacheable -- just without paying
        # another full timeout to rediscover it.
        raise HasheousUnavailable(
            f"skipped: unavailable, retrying in {remaining:.0f}s"
        )

    # Everything that can decide "this server is not answering properly" lives
    # inside the one try, so it all opens the cooldown. Validation used to sit
    # after it: a proxy returning `{}` for every hash was then re-requested
    # once per file, recreating exactly the hours-long outage the breaker
    # exists to prevent.
    try:
        data = await run_in_threadpool(_fetch_json, _lookup_url(normalized))
        record = None
        if data is not None:
            try:
                record = _normalize(data)
            except Exception as exc:  # defensive: a shape _obj didn't foresee
                raise HasheousUnavailable(f"unreadable response: {exc}") from exc
            if not _is_identified(record):
                # A 200 with no game identity is not a hit. Hasheous answers an
                # unknown hash with 404, so this is something else answering
                # for it -- a proxy error envelope, a self-host returning `{}`.
                # Caching it would record an authoritative-looking match with
                # no game attached.
                raise HasheousUnavailable("response carried no game identity")
    except HasheousUnavailable:
        _begin_cooldown()
        raise

    # A clean 404 counts as healthy: the server answered, it just doesn't know
    # this hash.
    _clear_cooldown()
    return record


def _is_identified(record: dict) -> bool:
    """True when a normalized record actually names a game."""
    return bool(record.get("game_name") or record.get("rom_name"))
