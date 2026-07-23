"""HTTP authentication helpers for the web UI and API."""

from __future__ import annotations

import base64
import secrets
from pathlib import Path

from config import settings
from fastapi import Request, status
from fastapi.responses import JSONResponse, Response

_TOKEN_FILE = "auth_token"


def ensure_auth_token() -> str | None:
    """Return the configured auth token, creating a persistent one if needed."""
    if not settings.enable_auth:
        return None
    if settings.auth_token:
        return settings.auth_token

    data_dir = Path(settings.data_dir)
    token_path = data_dir / _TOKEN_FILE
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        if token_path.exists():
            token = token_path.read_text(encoding="utf-8").strip()
            if token:
                settings.auth_token = token
                return token
        token = secrets.token_urlsafe(32)
        token_path.write_text(f"{token}\n", encoding="utf-8")
        # Best-effort: some bind mounts (CIFS/NTFS-style) reject chmod. The
        # written token is still the active one, so don't discard it on failure.
        try:
            token_path.chmod(0o600)
        except OSError:
            pass
        settings.auth_token = token
        return token
    except OSError:
        token = secrets.token_urlsafe(32)
        settings.auth_token = token
        return token


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
    ``X-Compressatorium-Token`` or ``access_token`` still authenticates even when
    a proxy or browser also forwards an unrelated ``Authorization`` header.
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
    query_token = request.query_params.get("access_token")
    if query_token:
        tokens.append(query_token)
    return tokens


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

    expected = ensure_auth_token()
    if not expected:
        return _unauthorized(request)
    if any(_tokens_match(token, expected) for token in _request_tokens(request)):
        return await call_next(request)
    return _unauthorized(request)


def _tokens_match(provided: str, expected: str) -> bool:
    """Constant-time token comparison that tolerates non-ASCII input.

    ``secrets.compare_digest`` raises ``TypeError`` for ``str`` arguments
    containing non-ASCII characters, so compare the UTF-8 encoded bytes to
    avoid an unhandled 500 when a client sends a non-ASCII token.
    """
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
