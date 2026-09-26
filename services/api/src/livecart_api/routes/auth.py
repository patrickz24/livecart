"""Seller authentication: ``/api/v1/auth`` login, refresh, logout and me.

- ``POST /login`` checks an e-mail and a password (argon2id) and answers an
  access token (a JWT of 15 minutes) in the body, and the refresh token in the
  ``lc_refresh`` cookie: ``HttpOnly``, so no script can read it,
  ``SameSite=Strict``, ``Path=/api/v1/auth``, so it only travels to these
  routes, and ``Secure`` in production. A wrong password and an unknown e-mail
  get the same 401. Logins are rate limited per client address, and failures
  per (account, address), with 429 beyond; while the limits cannot be
  counted (Redis down), logins answer 503 (``livecart_api.rate_limits``).
- ``POST /refresh`` trades the cookie for a new access token and rotates the
  refresh token, with a new cookie. Inside the reuse grace (another tab
  refreshed with the same cookie less than 30 s ago) the answer holds an
  access token only, without ``Set-Cookie``: the browser already holds the
  successor. A refused refresh clears the cookie. It has no rate limit and
  needs no Redis: sellers stay signed in through a Redis outage.
- ``POST /logout`` revokes the cookie's whole token family and clears the
  cookie; it answers 204 whatever the cookie held.
- ``GET /me`` is the seller's own profile, behind ``CurrentSeller``.

Login, refresh and logout check the ``Origin`` header (``require_same_origin``),
the login too since it sets the cookie: a page of another site must never
sign a seller's browser into an account of its choosing (login CSRF). The
JSON body and ``SameSite=Strict`` already make that hard; the ``Origin``
check depends on neither. A client outside a browser sends ``Origin`` itself.
Token answers carry ``Cache-Control: no-store`` (RFC 6749, section 5.1).
"""

from dataclasses import dataclass
from typing import Annotated, Final, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints

from livecart_api.access_tokens import AccessTokens
from livecart_api.deps import CurrentSeller, require_same_origin
from livecart_api.errors import ErrorResponse
from livecart_api.rate_limits import (
    TOO_MANY_REQUESTS,
    RateLimiting,
    client_address,
    login_failure_subject,
)
from livecart_core.application.dto import SellerGrant
from livecart_core.application.ports import Clock, SellerRepository
from livecart_core.application.seller_auth import (
    AuthenticateSeller,
    InvalidCredentials,
    RefreshRefusal,
    RefreshRefused,
    RevokeRefreshFamily,
    RotateRefreshToken,
)
from livecart_core.domain.sellers import MAX_EMAIL_LENGTH, MAX_PASSWORD_LENGTH

logger = structlog.get_logger(__name__)

PREFIX: Final = "/api/v1/auth"
REFRESH_COOKIE: Final = "lc_refresh"
# The token_type of RFC 6750: send the access token as Authorization: Bearer.
BEARER: Final = "bearer"


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_EMAIL_LENGTH)
    ]
    # Only the upper bound of the password policy: a login checks a password,
    # it does not choose one, and a shorter one simply matches no account.
    password: SecretStr = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = BEARER
    expires_in: int = Field(description="Seconds until the access token expires.")


class SellerProfile(BaseModel):
    id: UUID
    email: str
    slug: str
    display_name: str


@dataclass(frozen=True, slots=True)
class SellerAuth:
    """What the auth routes need, built by ``create_app`` from the resources and the settings."""

    authenticate: AuthenticateSeller
    rotate: RotateRefreshToken
    revoke: RevokeRefreshFamily
    sellers: SellerRepository
    access_tokens: AccessTokens
    clock: Clock
    # Secure cookies are required in production; development runs on plain HTTP.
    secure_cookies: bool


def _responses(*codes: int) -> dict[int | str, dict[str, object]]:
    descriptions = {
        status.HTTP_401_UNAUTHORIZED: "Not authenticated, or the credentials were refused",
        status.HTTP_403_FORBIDDEN: "The Origin header is missing or names another origin",
        status.HTTP_429_TOO_MANY_REQUESTS: TOO_MANY_REQUESTS,
        status.HTTP_503_SERVICE_UNAVAILABLE: "The rate limits cannot be counted: try again later",
    }
    return {code: {"model": ErrorResponse, "description": descriptions[code]} for code in codes}


def build_auth_router(auth: SellerAuth, limits: RateLimiting) -> APIRouter:
    router = APIRouter(prefix=PREFIX, tags=["auth"])

    async def limit_logins(request: Request) -> None:
        # A dependency, so that every attempt counts, one whose fields fail validation included.
        await limits.enforce(limits.rules.login, client_address(request))

    def set_refresh_cookie(response: Response, grant: SellerGrant) -> None:
        if grant.refresh_token is None:
            return
        lifetime = grant.refresh_token.expires_at - auth.clock.now()
        response.set_cookie(
            REFRESH_COOKIE,
            grant.refresh_token.value,
            max_age=max(0, int(lifetime.total_seconds())),
            path=PREFIX,
            secure=auth.secure_cookies,
            httponly=True,
            samesite="strict",
        )

    def clear_refresh_cookie(response: Response) -> None:
        response.delete_cookie(
            REFRESH_COOKIE,
            path=PREFIX,
            secure=auth.secure_cookies,
            httponly=True,
            samesite="strict",
        )

    def refused() -> JSONResponse:
        # The cookie grants nothing any more: clearing it stops the client from retrying.
        response = JSONResponse(
            {"detail": "Invalid refresh token"}, status_code=status.HTTP_401_UNAUTHORIZED
        )
        clear_refresh_cookie(response)
        return response

    def granted(grant: SellerGrant, response: Response) -> AccessTokenResponse:
        access = auth.access_tokens.issue(grant.seller_id)
        set_refresh_cookie(response, grant)
        response.headers["Cache-Control"] = "no-store"
        return AccessTokenResponse(
            access_token=access.value, expires_in=int(auth.access_tokens.ttl.total_seconds())
        )

    @router.post(
        "/login",
        summary="Log in with an e-mail and a password",
        response_model=AccessTokenResponse,
        # The limit first, so that every attempt counts, a cross-origin one included.
        dependencies=[Depends(limit_logins), Depends(require_same_origin)],
        responses=_responses(
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
            status.HTTP_429_TOO_MANY_REQUESTS,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ),
    )
    async def login(
        body: LoginRequest, request: Request, response: Response
    ) -> AccessTokenResponse:
        failures = login_failure_subject(body.email, client_address(request))
        # Checked before the password: past the limit, even the right one is refused.
        await limits.check(limits.rules.login_failures, failures)
        try:
            grant = await auth.authenticate(body.email, body.password.get_secret_value())
        except InvalidCredentials:
            await limits.record(limits.rules.login_failures, failures)
            # No e-mail in the logs: it is personal data, and the access log has the client IP.
            logger.info("seller_login_refused")
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, detail="Invalid e-mail or password"
            ) from None
        logger.info("seller_logged_in", seller_id=str(grant.seller_id))
        return granted(grant, response)

    @router.post(
        "/refresh",
        summary="Trade the refresh cookie for a new access token",
        response_model=AccessTokenResponse,
        dependencies=[Depends(require_same_origin)],
        responses=_responses(status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
    )
    async def refresh_access_token(
        request: Request, response: Response
    ) -> AccessTokenResponse | JSONResponse:
        token = request.cookies.get(REFRESH_COOKIE)
        if token is None:
            logger.info("refresh_refused", reason="no_cookie")
            return refused()
        try:
            grant = await auth.rotate(token)
        except RefreshRefused as refusal:
            if refusal.reason is RefreshRefusal.REUSED:
                logger.warning(
                    "refresh_token_reuse_detected",
                    seller_id=str(refusal.seller_id),
                    family_id=str(refusal.family_id),
                )
            else:
                logger.info("refresh_refused", reason=refusal.reason.value)
            return refused()
        return granted(grant, response)

    @router.post(
        "/logout",
        summary="Revoke the refresh cookie's session and clear the cookie",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_same_origin)],
        responses=_responses(status.HTTP_403_FORBIDDEN),
    )
    async def logout(request: Request) -> Response:
        token = request.cookies.get(REFRESH_COOKIE)
        if token is not None and await auth.revoke(token):
            logger.info("seller_logged_out")
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        clear_refresh_cookie(response)
        return response

    @router.get(
        "/me",
        summary="The profile of the seller the access token names",
        responses=_responses(status.HTTP_401_UNAUTHORIZED, status.HTTP_429_TOO_MANY_REQUESTS),
    )
    async def get_current_seller(seller: CurrentSeller) -> SellerProfile:
        record = await auth.sellers.get(seller.id)
        if record is None:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired access token",
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
        return SellerProfile(
            id=record.id, email=record.email, slug=record.slug, display_name=record.display_name
        )

    return router
