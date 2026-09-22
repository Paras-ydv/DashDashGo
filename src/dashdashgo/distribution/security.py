"""HTTP-level protection for the UI and API.

* **Login** - optional HTTP Basic auth (``AUTH_USERNAME`` / ``AUTH_PASSWORD``).
  Everything except the health check and static assets needs it once enabled.
  Basic auth keeps the UI dependency-free (no sessions, no login page) and works
  unchanged for ``curl -u`` and the CLI's HTTP clients.
* **Cross-site request protection** - browsers resend Basic credentials to any
  page that targets the app, so a malicious site could start runs or rewrite
  configs. Mutating requests are refused when the browser says they come from
  another site (``Sec-Fetch-Site``) or their ``Origin`` is not this host.
  Non-browser clients send neither header and are unaffected.
"""

from __future__ import annotations

import base64
import binascii
import secrets
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from dashdashgo.settings import Settings

PUBLIC_PATHS = ("/api/health", "/static/", "/favicon.ico")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_TRUSTED_FETCH_SITES = frozenset({"same-origin", "none"})

Handler = Callable[[Request], Awaitable[Response]]


def _credentials(request: Request) -> tuple[str, str] | None:
    scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        username, sep, password = base64.b64decode(encoded, validate=True).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return None
    return (username, password) if sep else None


def _is_cross_site(request: Request) -> bool:
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site not in _TRUSTED_FETCH_SITES
    origin = request.headers.get("origin")
    if origin is None:
        return False
    return origin == "null" or urlsplit(origin).netloc != request.headers.get("host", "")


def install_security(app: FastAPI, settings: Settings) -> None:
    auth_enabled = settings.auth_enabled
    expected = (settings.auth_username.encode(), settings.auth_password.get_secret_value().encode())

    @app.middleware("http")
    async def protect(request: Request, call_next: Handler) -> Response:
        path = request.url.path
        if request.method not in SAFE_METHODS and _is_cross_site(request):
            return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
        if auth_enabled and not path.startswith(PUBLIC_PATHS):
            given = _credentials(request)
            ok = given is not None and (
                secrets.compare_digest(given[0].encode(), expected[0])
                & secrets.compare_digest(given[1].encode(), expected[1])
            )
            if not ok:
                return JSONResponse(
                    {"detail": "authentication required"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="DashDashGo", charset="UTF-8"'},
                )
        return await call_next(request)
