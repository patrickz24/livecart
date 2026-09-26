"""Dependencies shared by the routers: ``CurrentSeller`` and the same-origin check.

``CurrentSeller`` guards every seller route: a route that declares a
parameter of that type answers 401, with ``WWW-Authenticate: Bearer``, unless
the request carries a valid access token in ``Authorization: Bearer``. It
reads nothing from the database: a verified JWT is the proof, which is what
keeps access tokens short-lived (15 minutes). It then counts the request
against the seller's rate limit (``livecart_api.rate_limits``), 429 beyond.

``require_same_origin`` guards the routes that set or act on the refresh
cookie. The cookie is ``SameSite=Strict`` already; the ``Origin`` header is a
second, independent check (OWASP's CSRF defence in depth). Browsers send
``Origin`` on every POST, so one that is missing or that differs from the
origin the request was addressed to is refused with 403. Everything is served from one
origin (nginx, or the Vite proxy in development), so no list of origins is
configured: the expected one is rebuilt from the request, its scheme from
``X-Forwarded-Proto`` as uvicorn trusts it from nginx only, its host and port
from ``Host``.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Final
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from livecart_api.access_tokens import AccessTokens, InvalidAccessToken
from livecart_api.rate_limits import rate_limiting

DEFAULT_PORTS: Final = {"http": 80, "https": 443}

type Origin = tuple[str, str, int]


@dataclass(frozen=True, slots=True)
class AuthenticatedSeller:
    """The seller an access token names, and when that token expires.

    The expiry bounds what the token opens: a stream of session events
    closes before it (``livecart_core.application.watch_live_session``).
    """

    id: UUID
    access_expires_at: datetime


def access_tokens(request: Request) -> AccessTokens:
    """The verifier ``create_app`` put on the app's state."""
    tokens = getattr(request.app.state, "access_tokens", None)
    if not isinstance(tokens, AccessTokens):
        raise RuntimeError("the app was built without an access token verifier")
    return tokens


_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="SellerAccessToken",
    description="The access_token of POST /api/v1/auth/login or /api/v1/auth/refresh.",
)


def _unauthorized(detail: str, *, challenge: str = "Bearer") -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail=detail, headers={"WWW-Authenticate": challenge}
    )


async def _current_seller(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthenticatedSeller:
    if credentials is None:
        raise _unauthorized("Not authenticated")
    try:
        claims = access_tokens(request).verify(credentials.credentials)
    except InvalidAccessToken:
        raise _unauthorized(
            "Invalid or expired access token", challenge='Bearer error="invalid_token"'
        ) from None
    # After the token, so that only a seller's own requests fill the seller's counter.
    limits = rate_limiting(request)
    await limits.enforce(limits.rules.seller, str(claims.seller_id))
    return AuthenticatedSeller(claims.seller_id, claims.expires_at)


CurrentSeller = Annotated[AuthenticatedSeller, Depends(_current_seller)]


def parse_origin(value: str) -> Origin | None:
    """(scheme, host, port) of a serialized origin such as ``http://localhost:8080``.

    ``None`` for anything that is not one: ``null``, a path, credentials, a
    scheme other than http(s).
    """
    parts = urlsplit(value)
    if (
        parts.scheme not in DEFAULT_PORTS
        or not parts.hostname
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or parts.username is not None
    ):
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    return parts.scheme, parts.hostname, port if port is not None else DEFAULT_PORTS[parts.scheme]


async def require_same_origin(request: Request) -> None:
    """403 unless ``Origin`` names the origin this request was addressed to."""
    origin = request.headers.get("origin")
    expected = parse_origin(str(request.base_url))
    if origin is None or expected is None or parse_origin(origin) != expected:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Cross-origin request refused")
