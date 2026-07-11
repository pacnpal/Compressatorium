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
    if settings.disable_auth:
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
        token_path.chmod(0o600)
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


def _request_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    scheme, _, value = auth.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value
    if scheme.lower() == "basic" and value:
        return _basic_token(value)
    header_token = request.headers.get("X-Compressatorium-Token")
    if header_token:
        return header_token
    return request.query_params.get("access_token")


def _unauthorized(request: Request) -> Response:
    headers = {"WWW-Authenticate": f'Basic realm="Compressatorium", charset="UTF-8"'}
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
    if request.url.path == "/health" or settings.disable_auth:
        return await call_next(request)

    expected = ensure_auth_token()
    provided = _request_token(request)
    if not expected or not provided or not secrets.compare_digest(provided, expected):
        return _unauthorized(request)
    return await call_next(request)
