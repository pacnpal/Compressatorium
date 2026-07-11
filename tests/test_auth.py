import asyncio
import base64

from starlette.requests import Request
from starlette.responses import JSONResponse

from auth import ensure_auth_token, require_auth_middleware
from config import settings


def _reset_auth(monkeypatch, *, token="test-token", disabled=False, username="admin"):
    monkeypatch.setattr(settings, "auth_token", token)
    monkeypatch.setattr(settings, "disable_auth", disabled)
    monkeypatch.setattr(settings, "auth_username", username)


def _request(path="/api/version", headers=None, query_string=b""):
    raw_headers = []
    for key, value in (headers or {}).items():
        raw_headers.append((key.lower().encode("latin-1"), value.encode("latin-1")))
    return Request({
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": query_string,
        "headers": raw_headers,
        "server": ("testserver", 80),
        "scheme": "http",
        "client": ("testclient", 50000),
    })


async def _ok_response(_request):
    return JSONResponse({"ok": True})


def _run(request):
    return asyncio.run(require_auth_middleware(request, _ok_response))


def test_api_rejects_unauthenticated_requests(monkeypatch):
    _reset_auth(monkeypatch)

    response = _run(_request())

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic ")


def test_api_accepts_bearer_token(monkeypatch):
    _reset_auth(monkeypatch)

    response = _run(_request(headers={"Authorization": "Bearer test-token"}))

    assert response.status_code == 200


def test_web_ui_accepts_basic_auth(monkeypatch):
    _reset_auth(monkeypatch, token="secret", username="operator")
    credentials = base64.b64encode(b"operator:secret").decode("ascii")

    response = _run(_request("/", headers={"Authorization": f"Basic {credentials}"}))

    assert response.status_code == 200


def test_eventsource_can_use_query_token(monkeypatch):
    _reset_auth(monkeypatch)

    response = _run(_request("/api/jobs/events", query_string=b"access_token=test-token"))

    assert response.status_code == 200


def test_health_remains_unauthenticated(monkeypatch):
    _reset_auth(monkeypatch)

    response = _run(_request("/health"))

    assert response.status_code == 200


def test_auth_can_be_explicitly_disabled(monkeypatch):
    _reset_auth(monkeypatch, disabled=True)

    response = _run(_request())

    assert response.status_code == 200


def test_auth_token_is_persisted(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "auth_token", None)
    monkeypatch.setattr(settings, "disable_auth", False)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))

    token = ensure_auth_token()

    assert token
    assert (tmp_path / "auth_token").read_text(encoding="utf-8").strip() == token
    assert ensure_auth_token() == token
