"""HTTP authentication helpers for the web UI and API."""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from config import settings
from fastapi import Request, status
from fastapi.responses import JSONResponse, Response

_AUTH_FILENAME = "auth_token"

# Methods that never change server state; exempt from the same-origin check.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def ensure_auth_token() -> str | None:
    """Return the configured auth token, creating a persistent one if needed.

    Only relevant when ``COMPRESSATORIUM_ENABLE_AUTH`` is set. When no explicit
    token is configured, a random one is persisted in the data directory. If it
    cannot be persisted, startup fails with a clear error rather than installing
    an in-memory token the operator can never discover.
    """
    if not settings.enable_auth:
        return None
    if settings.auth_token:
        return settings.auth_token

    data_dir = Path(settings.data_dir)
    token_path = data_dir / _AUTH_FILENAME
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        if token_path.exists():
            token = token_path.read_text(encoding="utf-8").strip()
            if token:
                settings.auth_token = token
                return token
        token = secrets.token_urlsafe(32)
        # Create the file owner-only (0o600) atomically so the credential is
        # never briefly world-readable. Filesystems that ignore Unix modes
        # (CIFS/NTFS bind mounts) still accept the write, so this does not lock
        # out those deployments.
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{token}\n")
        settings.auth_token = token
        return token
    except OSError as exc:
        raise RuntimeError(
            f"Authentication is enabled but the auth token could not be persisted to "
            f"{token_path} ({exc}). Set COMPRESSATORIUM_AUTH_TOKEN to an explicit value "
            f"or make the data directory writable."
        ) from exc


def _basic_token(value: str) -> str | None:
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    username, sep, password = decoded.partition(":")
    if sep != ":" or username != settings.auth_username:
        return None
    return password


def _request_tokens(request: Request) -> list[str]:
    """Collect every candidate token the request offers, in priority order.

    All sources are gathered (not just the first one present) so a valid
    ``X-Compressatorium-Token`` still authenticates even when a proxy or browser
    also forwards an unrelated ``Authorization`` header. Tokens are never read
    from the query string, to keep the credential out of access logs, proxy
    logs, and browser history.
    """
    tokens: list[str] = []
    auth = request.headers.get("Authorization", "")
    parts = auth.split(maxsplit=1)
    if len(parts) == 2:
        scheme, value = parts[0].lower(), parts[1]
        if scheme == "bearer" and value:
            tokens.append(value)
        elif scheme == "basic" and value:
            password = _basic_token(value)
            if password:
                tokens.append(password)
    header_token = request.headers.get("X-Compressatorium-Token")
    if header_token:
        tokens.append(header_token)
    return tokens


def _is_forbidden_cross_site(request: Request) -> bool:
    """Return True for a state-changing request from another origin.

    Because a browser attaches cached HTTP Basic credentials to same-origin
    requests even when a malicious page triggers them, unsafe methods are
    restricted to same-origin callers. Modern browsers are judged by the
    ``Sec-Fetch-Site`` metadata header; older ones fall back to an ``Origin``
    host comparison. Non-browser clients (curl, scripts) send neither and are
    allowed through — they authenticate with an explicit token header and are
    not exposed to CSRF.
    """
    if request.method in _SAFE_METHODS:
        return False
    sec_fetch_site = request.headers.get("Sec-Fetch-Site")
    if sec_fetch_site is not None:
        return sec_fetch_site not in ("same-origin", "none")
    origin = request.headers.get("Origin")
    if origin and origin != "null":
        return urlsplit(origin).netloc != (request.headers.get("Host") or "")
    return False


def _forbidden(request: Request) -> Response:
    detail = "Cross-origin request forbidden"
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": detail}, status_code=status.HTTP_403_FORBIDDEN)
    return Response(detail, status_code=status.HTTP_403_FORBIDDEN)


def _unauthorized(request: Request) -> Response:
    headers = {"WWW-Authenticate": 'Basic realm="Compressatorium", charset="UTF-8"'}
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"detail": "Authentication required"},
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers=headers,
        )
    return Response(
        "Authentication required",
        status_code=status.HTTP_401_UNAUTHORIZED,
        headers=headers,
    )


async def require_auth_middleware(request: Request, call_next):
    """Require authentication for the web UI and API unless explicitly disabled."""
    if not settings.enable_auth or request.url.path == "/health":
        return await call_next(request)

    # The token is resolved once at startup (see main.py lifespan), so read it
    # directly here instead of re-running the persistence path on every request.
    expected = settings.auth_token
    if not expected:
        return _unauthorized(request)
    if not any(_tokens_match(token, expected) for token in _request_tokens(request)):
        return _unauthorized(request)
    if _is_forbidden_cross_site(request):
        return _forbidden(request)
    return await call_next(request)


def _tokens_match(provided: str, expected: str) -> bool:
    """Constant-time token comparison that tolerates non-ASCII input.

    ``secrets.compare_digest`` raises ``TypeError`` for ``str`` arguments
    containing non-ASCII characters, so compare the UTF-8 encoded bytes to
    avoid an unhandled 500 when a client sends a non-ASCII token.
    """
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
