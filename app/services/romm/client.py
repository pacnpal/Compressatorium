"""RomM catalog client.

RomM (https://romm.app) is a self-hosted ROM library manager.  It owns the
*metadata* — platform, game name, hashes, DAT match — for files Compressatorium
can already see on disk.  This module reads that catalog; it never moves ROM
bytes.  RomM's library is mounted as an ordinary Compressatorium volume (the
same bind mount locally, NFS/SMB/rclone remotely), so a multi-GB disc image is
never streamed over HTTP in either topology.

Transport is stdlib ``urllib`` rather than a new dependency, following
``services.dat_sync`` — the project's other outbound-HTTP client — including its
https guard, request timeout and response size cap.  The calls are synchronous
and callers run them through ``run_in_threadpool``: ``MAX_CONCURRENT_JOBS``
defaults to 1 with jobs running inline in the dispatcher, so a blocking network
call on the event loop would stall the whole conversion queue.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from logging_setup import get_logger

logger = get_logger("romm")

# Timeout for individual HTTP requests (seconds). Matches dat_sync.
_HTTP_TIMEOUT = 30

# Cap on a single catalog response. RomM's own limit ceiling is 10000 roms, and
# a page that large with `with_files` is a few MB of JSON; 64 MB is generous
# headroom that still refuses to buffer a runaway/hostile response into memory.
_MAX_RESPONSE_SIZE = 64 * 1024 * 1024

_USER_AGENT = "compressatorium-romm/1.0"

# Page size for catalog reads. RomM allows up to 10000, but a smaller page keeps
# each request's latency and memory bounded and costs only a few extra requests.
PAGE_SIZE = 500

# Hard ceiling on pages walked for one platform, so a paging bug (or a RomM that
# ignores `offset`) cannot spin forever holding a threadpool worker.
_MAX_PAGES = 200

# Output extensions whose DAT identity survives conversion, because RomM matches
# them on something other than the container's own bytes: a CHD by the raw+meta
# SHA-1 in its v5 header (which is the digest Redump/No-Intro publish), and an
# archive by the hashes of its largest member. See RomM's `Rom.lookup_hashes`.
#
# Everything else we emit (.rvz/.cso/.zso/.nsz/.wux/.z3ds/...) is matched on the
# container hash, which conversion necessarily changes — those need the re-pin
# pass in `routes.romm`. Single source of truth: the API hands this to the
# frontend so the UI keeps no second copy.
DAT_SAFE_OUTPUT_EXTS = frozenset({".chd", ".zip", ".7z"})

# Provider id fields carried across a conversion. Captured from the source ROM
# before it is converted and re-applied to the record RomM creates for the
# output, so a format RomM cannot hash-match keeps the metadata the user already
# curated. RomM exposes these on the ROM schema and accepts them back on PUT.
METADATA_ID_FIELDS = (
    "igdb_id",
    "moby_id",
    "ss_id",
    "ra_id",
    "launchbox_id",
    "hasheous_id",
    "tgdb_id",
    "flashpoint_id",
    "hltb_id",
)


class RommError(RuntimeError):
    """A RomM request failed, or RomM is not configured.

    ``status`` carries RomM's HTTP status when there was one, so callers can
    describe the failure ("token rejected", "not reachable") from a value we
    own rather than by echoing the exception text. The message itself may quote
    RomM's response body and is for the log, not for an API response.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RommNotConfigured(RommError):
    """ROMM_URL is unset — the integration is off."""


def _token() -> str | None:
    """The RomM client API token in force, or None.

    Resolved through :mod:`services.romm.settings`, so a token saved in the app
    wins over the ``ROMM_TOKEN`` environment default and takes effect without a
    restart. Deliberately not a field on the global ``Settings`` object: a
    secret there leaks into every ``repr()`` and config dump.
    """
    from services.romm import settings as romm_settings

    return romm_settings.token()


def _urlopen(req: urllib.request.Request, origin_url: str):
    """Open *req*, following redirects only within *origin_url*'s origin.

    The one seam every RomM HTTP call goes through, so the redirect policy
    cannot be bypassed by a caller reaching for ``urlopen`` directly.
    """
    opener = urllib.request.build_opener(_SameOriginRedirectHandler(origin_url))
    return opener.open(req, timeout=_HTTP_TIMEOUT)  # nosec B310


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse any redirect that leaves the origin the request started on.

    The default handler re-sends ``Authorization`` to whatever ``Location``
    names, so a redirect off-origin leaks the RomM API token. Rather than strip
    the header and follow (which turns an authenticated read into a confusing
    401 from a stranger), this refuses outright: a RomM that redirects its API
    to another host is not a RomM we should be talking to.
    """

    # Normalised so the *same* origin written two ways still matches. A
    # redirect from `https://romm` to `https://romm:443/...` is same-origin by
    # every definition that matters, but the raw netloc differs -- refusing it
    # would break a perfectly ordinary RomM behind a proxy that spells the port
    # out. Scheme still has to match exactly, so an HTTPS-to-HTTP downgrade is
    # still a cross-origin refusal.
    _DEFAULT_PORTS = {"http": 80, "https": 443}

    @classmethod
    def _origin_of(cls, url: str) -> tuple[str, str, int | None]:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        try:
            port = parsed.port
        except ValueError:  # malformed port -- never matches a real origin
            return (scheme, (parsed.hostname or "").lower(), -1)
        return (
            scheme,
            (parsed.hostname or "").lower(),
            port or cls._DEFAULT_PORTS.get(scheme),
        )

    def __init__(self, origin_url: str) -> None:
        super().__init__()
        self._origin = self._origin_of(origin_url)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        target = self._origin_of(newurl)
        if parsed.scheme.lower() not in ("http", "https") or target != self._origin:
            logger.warning(
                "romm: refusing cross-origin redirect to %r", parsed.netloc or newurl,
            )
            raise urllib.error.HTTPError(
                newurl, code,
                "Refusing to follow a redirect off the configured RomM origin",
                headers, fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class RommClient:
    """Thin synchronous client over RomM's REST API."""

    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self._explicit_base = base_url
        self._explicit_token = token

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        if self._explicit_base is not None:
            return self._explicit_base.rstrip("/")
        from services.romm import settings as romm_settings

        return str(romm_settings.effective().get("url") or "").rstrip("/")

    @property
    def library_root(self) -> str:
        from services.romm import settings as romm_settings

        return str(romm_settings.effective().get("library_root") or "")

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _require_configured(self) -> str:
        base = self.base_url
        if not base:
            raise RommNotConfigured("ROMM_URL is not set")
        return base

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    @staticmethod
    def _require_http(url: str) -> None:
        """Reject any scheme that isn't http/https.

        ``dat_sync`` can demand https outright because it only ever talks to
        GitHub.  RomM is the operator's own service and is very commonly reached
        over plain http on a container network (``http://romm:8080``), so http
        is permitted here — but ``file://``/``ftp://`` and friends are not, since
        a crafted ROMM_URL would otherwise turn this into a local-file reader.
        """
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise RommError(
                f"Only http/https URLs are permitted; got scheme '{parsed.scheme}'",
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: bytes | None = None,
        content_type: str | None = None,
        authenticated: bool = True,
    ) -> Any:
        url = f"{self._require_configured()}{path}"
        if params:
            # Drop None so callers can pass optional filters unconditionally.
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += f"?{urllib.parse.urlencode(clean, doseq=True)}"
        self._require_http(url)

        headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
        if content_type:
            headers["Content-Type"] = content_type
        token = self._explicit_token if self._explicit_token is not None else _token()
        if authenticated and token:
            # RomM client API tokens ("rmm_" + 64 hex) are sent as bearer
            # credentials. RomM's CSRF middleware short-circuits on a bearer or
            # basic scheme, so no CSRF token is needed for writes.
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(url, headers=headers, data=data, method=method)
        try:
            with _urlopen(req, url) as resp:
                raw = resp.read(_MAX_RESPONSE_SIZE + 1)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(4096).decode("utf-8", "replace").strip()
            except (OSError, ValueError):
                # Best-effort context only; the status code is the real signal.
                detail = ""
            raise RommError(
                f"RomM {method} {path} failed: HTTP {exc.code}"
                + (f" — {detail}" if detail else ""),
                status=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise RommError(f"RomM {method} {path} failed: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            # The socket can time out or drop *while reading the body*, after
            # urlopen has already returned. urllib raises the bare OS error
            # there rather than URLError, so without this the failure escapes
            # as a 500 instead of the documented 502 / status payload.
            raise RommError(f"RomM {method} {path} failed: {exc}") from exc

        if len(raw) > _MAX_RESPONSE_SIZE:
            raise RommError(
                f"RomM {method} {path} response exceeds {_MAX_RESPONSE_SIZE} bytes",
            )
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise RommError(f"RomM {method} {path} returned invalid JSON") from exc

    # ------------------------------------------------------------------
    # catalog reads
    # ------------------------------------------------------------------

    def heartbeat(self) -> dict:
        """Return RomM's public heartbeat (version, config, enabled providers).

        Unauthenticated on RomM's side, which makes it the right probe for a
        "test connection" button: it separates "cannot reach RomM" from "reached
        RomM, token rejected".
        """
        result = self._request("GET", "/api/heartbeat", authenticated=False)
        return result if isinstance(result, dict) else {}

    def platforms(self) -> list[dict]:
        """Return RomM's platforms. Flat array, not paginated."""
        result = self._request("GET", "/api/platforms")
        return result if isinstance(result, list) else []

    def roms(self, platform_id: int) -> list[dict]:
        """Return every ROM on *platform_id*, paging until exhausted.

        ``with_char_index`` / ``with_rom_id_index`` / ``with_filter_values``
        default to true on RomM's side and exist only to drive its own web UI's
        virtual scroll and filter dropdowns; switching them off keeps the
        payload to the records we actually read.
        """
        out: list[dict] = []
        offset = 0
        for _ in range(_MAX_PAGES):
            page = self._request(
                "GET",
                "/api/roms",
                params={
                    "platform_ids": platform_id,
                    "with_files": "true",
                    "with_char_index": "false",
                    "with_rom_id_index": "false",
                    "with_filter_values": "false",
                    "limit": PAGE_SIZE,
                    "offset": offset,
                },
            )
            items = (page or {}).get("items") or []
            if not items:
                break
            out.extend(items)
            if len(items) < PAGE_SIZE:
                break
            offset += len(items)
        else:
            logger.warning(
                "romm: stopped paging platform %s at the %d-page ceiling",
                platform_id, _MAX_PAGES,
            )
        return out

    def rom(self, rom_id: int) -> dict:
        result = self._request("GET", f"/api/roms/{int(rom_id)}")
        return result if isinstance(result, dict) else {}

    def rom_by_sha1(self, sha1: str) -> dict | None:
        """Return the ROM whose file hashes to *sha1*, or None.

        This is the join used to settle a re-pin: RomM computes the converted
        file's own SHA-1 when it scans, and ``file_hasher.compute_file_sha1``
        computes the same one, so the match is exact and survives the user
        renaming the file in between.
        """
        try:
            result = self._request(
                "GET", "/api/roms/by-hash", params={"sha1_hash": sha1},
            )
        except RommError as exc:
            # RomM answers 404 when nothing matches, which for us just means
            # "not scanned yet" — a normal, expected state, not a failure.
            # Read the status we recorded, not the message text: a body that
            # happens to mention "HTTP 404" must not swallow a 500.
            if exc.status == 404:
                return None
            raise
        return result if isinstance(result, dict) and result.get("id") else None

    def update_rom_metadata(self, rom_id: int, metadata_ids: dict) -> None:
        """Re-attach provider ids to *rom_id*.

        ``PUT /api/roms/{id}`` is multipart/form-data. Only the provider id
        fields are sent, so nothing else on the record is touched.
        """
        fields = {
            k: v for k, v in (metadata_ids or {}).items()
            if k in METADATA_ID_FIELDS and v not in (None, "")
        }
        if not fields:
            return
        body, content_type = _encode_multipart(fields)
        self._request(
            "PUT", f"/api/roms/{int(rom_id)}", data=body, content_type=content_type,
        )

    # ------------------------------------------------------------------
    # path mapping
    # ------------------------------------------------------------------

    def local_path(self, rom: dict) -> str | None:
        """Map a RomM ROM record onto its path on our filesystem.

        RomM's ``full_path`` is ``fs_path/fs_name`` and is *relative* to RomM's
        library root, already carrying the platform folder (``roms/snes/Foo.iso``).
        Joining it to the local mount point avoids reconstructing the platform
        folder ourselves, and so sidesteps ``platform_slug`` vs
        ``platform_fs_slug`` diverging when the operator overrides folder names.

        Returns None when the record has no usable path, or when the path tries
        to escape the library root.  The escape check is a trust boundary: the
        value is supplied by a remote service, and the caller turns the result
        into a filesystem operation.
        """
        root = self.library_root
        if not root:
            return None
        rel = (rom.get("full_path") or "").strip()
        if not rel:
            fs_path = (rom.get("fs_path") or "").strip()
            fs_name = (rom.get("fs_name") or "").strip()
            if not fs_name:
                return None
            rel = f"{fs_path}/{fs_name}" if fs_path else fs_name
        # realpath, not abspath: the library is usually reached through a
        # container bind mount, and a symlinked component would otherwise make
        # a contained path look like an escape (or the reverse).
        root_abs = os.path.realpath(root)
        # No lstrip("/"): an absolute value is malformed coming from RomM (its
        # own validate_path rejects one), and stripping the slash would silently
        # rehome "/etc/passwd" inside the library instead of refusing it.
        # os.path.join lets an absolute component win, so the containment check
        # below catches it.
        candidate = os.path.realpath(os.path.join(root_abs, rel))
        # realpath collapses any ".." *and* resolves symlinks before this
        # compares, so neither a traversing full_path nor a symlink planted
        # inside the library can point the result outside it.
        if candidate != root_abs and not candidate.startswith(root_abs + os.sep):
            logger.warning("romm: rejecting out-of-library path %r", rel)
            return None
        return candidate


def _encode_multipart(fields: dict) -> tuple[bytes, str]:
    """Encode *fields* as multipart/form-data. Text fields only.

    Field names and values are refused if they carry CR, LF or a quote: this
    encoder writes the header line by string interpolation, so such a character
    would let a value forge a part boundary or a header of its own. The values
    originate in a remote RomM record, so the check is a trust boundary, and
    refusing is right — silently stripping would send altered metadata.
    """
    # Fixed boundary would risk colliding with field content; derive a random one.
    boundary = "----compressatorium" + os.urandom(16).hex()
    parts: list[bytes] = []
    for key, value in fields.items():
        name = str(key)
        text = str(value)
        if any(c in name for c in '\r\n"') or any(c in text for c in "\r\n"):
            raise RommError(f"Refusing to send multipart field with control characters: {name!r}")
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{text}\r\n".encode(),
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# Module-level singleton, mirroring the other services.
romm_client = RommClient()
