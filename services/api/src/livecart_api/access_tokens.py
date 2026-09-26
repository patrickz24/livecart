"""Seller access tokens: JWTs of 15 minutes, HS256 with PyJWT, verified with the algorithm pinned.

- ``jwt.decode`` gets ``algorithms=["HS256"]``: the token's own ``alg``
  header never chooses how it is checked, so ``none``, another HMAC or an
  asymmetric algorithm (the RS256/HS256 confusion) are refused.
- ``iss``, ``aud``, ``sub``, ``exp`` and ``jti`` are required and checked:
  a token minted for another audience or by another issuer that happens to
  share the key is refused. ``sub`` is the seller's id; ``jti`` makes every
  token unique, for the logs.
- ``JWT_SECRET`` is at least 32 bytes (checked by the settings, and again by
  PyJWT with ``enforce_minimum_key_length``). It is one key without rotation:
  a known limit, on the roadmap.

The expiry is issued from the ``Clock`` port and checked by PyJWT against
the system clock, which accepts no injected time: in production both are the
system clock, and the tests that verify tokens start their clock at the
current time. No ``iat`` is sent, since nothing reads it, so a test clock
ahead of the system clock never makes a token "not yet valid".
"""

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

import jwt
from pydantic import SecretStr

from livecart_core.application.ports import Clock

ALGORITHM: Final = "HS256"
ISSUER: Final = "livecart-api"
AUDIENCE: Final = "livecart-seller"
ACCESS_TOKEN_TTL: Final = timedelta(seconds=900)
REQUIRED_CLAIMS: Final = ("iss", "aud", "sub", "exp", "jti")


class InvalidAccessToken(Exception):
    """A token that grants nothing; the message names the check that failed, never the token."""


@dataclass(frozen=True, slots=True)
class IssuedAccessToken:
    value: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AccessClaims:
    """What a verified token says: whose it is, its id and when it expires."""

    seller_id: UUID
    token_id: str
    expires_at: datetime


class AccessTokens:
    """Issues and verifies seller access tokens under ``JWT_SECRET``; see the module docstring."""

    def __init__(
        self, secret: SecretStr, clock: Clock, *, ttl: timedelta = ACCESS_TOKEN_TTL
    ) -> None:
        self._key = secret.get_secret_value()
        self._clock = clock
        self._ttl = ttl

    def __repr__(self) -> str:
        return "AccessTokens(secret=SecretStr('**********'))"

    @property
    def ttl(self) -> timedelta:
        return self._ttl

    def issue(self, seller_id: UUID) -> IssuedAccessToken:
        # A NumericDate is whole seconds; truncating keeps the token within its TTL.
        expires = int((self._clock.now() + self._ttl).timestamp())
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": str(seller_id),
            "exp": expires,
            "jti": secrets.token_urlsafe(16),
        }
        return IssuedAccessToken(
            jwt.encode(claims, self._key, algorithm=ALGORITHM),
            datetime.fromtimestamp(expires, UTC),
        )

    def verify(self, token: str) -> AccessClaims:
        """The claims of a valid token; ``InvalidAccessToken`` for anything else."""
        try:
            claims = jwt.decode(
                token,
                self._key,
                algorithms=[ALGORITHM],
                audience=AUDIENCE,
                issuer=ISSUER,
                options={"require": list(REQUIRED_CLAIMS), "enforce_minimum_key_length": True},
            )
            # PyJWT checked that sub and jti are strings; sub must also be an id.
            seller_id = UUID(claims["sub"])
        except (jwt.InvalidTokenError, ValueError) as error:
            raise InvalidAccessToken(type(error).__name__) from None
        return AccessClaims(
            seller_id=seller_id,
            token_id=claims["jti"],
            expires_at=datetime.fromtimestamp(claims["exp"], UTC),
        )
