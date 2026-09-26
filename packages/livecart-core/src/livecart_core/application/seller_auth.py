"""Seller sign-in: the login, the rotation of refresh tokens with its reuse grace, the logout.

A login checks the password and starts a family of refresh tokens that lives
``REFRESH_FAMILY_LIFETIME`` (30 days): every successor inherits the family's
expiry, so a seller signs in again at least once a month. The API turns each
grant into a short-lived access token and puts the refresh token in an
HttpOnly cookie; only the SHA-256 of a refresh token is ever stored, and no
refresh token is sent twice.

A refresh token is used once. ``RotateRefreshToken`` marks it used and issues
its successor in one unit (``RefreshTokenRepository.rotate``), which exactly
one of several concurrent refreshes wins. A token presented again is either:

- a race between tabs: the browser's cookie jar is shared, so two tabs may
  send the same token at once. A token used less than ``REUSE_GRACE`` (30 s)
  ago whose successor is still active gets a new access token only, without a
  refresh token: the jar already holds the successor the winner set;
- a replay: after the grace, or once the successor is revoked, the whole
  family is revoked, the thief's copy and the seller's session with it.

Every time comes from the ``Clock`` port.
"""

import hashlib
import secrets
import string
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID

from livecart_core.application.dto import (
    IssuedRefreshToken,
    RefreshTokenRecord,
    SellerGrant,
)
from livecart_core.application.ports import (
    Clock,
    PasswordHasher,
    RefreshTokenRepository,
    SellerRepository,
)

REFRESH_FAMILY_LIFETIME: Final = timedelta(days=30)
REUSE_GRACE: Final = timedelta(seconds=30)
# 256 bits of randomness, in 43 base64url characters without padding.
REFRESH_TOKEN_BYTES: Final = 32
REFRESH_TOKEN_LENGTH: Final = 43
_BASE64URL: Final = frozenset(string.ascii_letters + string.digits + "-_")


class AuthenticationFailed(Exception):
    """Base of the refusals of this module: the API answers each with a generic 401."""


class InvalidCredentials(AuthenticationFailed):
    """No seller has this e-mail, or the password is wrong: deliberately not told apart."""

    def __init__(self) -> None:
        super().__init__("invalid e-mail or password")


class RefreshRefusal(StrEnum):
    """Why a refresh token was refused, for the logs; the client only ever sees a 401."""

    MALFORMED = "malformed"
    UNKNOWN = "unknown"
    EXPIRED = "expired"
    REVOKED = "revoked"
    # Used before, outside the grace or with its successor revoked: the family is revoked.
    REUSED = "reused"


class RefreshRefused(AuthenticationFailed):
    """A refresh token that grants nothing; ``reason`` says why, the ids name a revoked family."""

    def __init__(
        self,
        reason: RefreshRefusal,
        *,
        seller_id: UUID | None = None,
        family_id: UUID | None = None,
    ) -> None:
        super().__init__(f"refresh token refused: {reason}")
        self.reason = reason
        self.seller_id = seller_id
        self.family_id = family_id


def new_refresh_token() -> str:
    """A fresh opaque refresh token: 256 bits from the operating system's CSPRNG."""
    return secrets.token_urlsafe(REFRESH_TOKEN_BYTES)


def hash_refresh_token(token: str) -> bytes:
    """What ``refresh_tokens.token_hash`` stores for a token: SHA-256 of its text.

    A plain hash is enough, with no salt nor slow function: the token holds
    256 random bits, so its hash cannot be reversed by guessing.
    """
    return hashlib.sha256(token.encode("ascii")).digest()


def read_refresh_token(text: str) -> bytes | None:
    """The hash of a token as a client sent it, or ``None`` when it cannot be one of ours.

    Refused on its shape alone, before any query: 43 base64url characters.
    """
    if len(text) != REFRESH_TOKEN_LENGTH or not _BASE64URL.issuperset(text):
        return None
    return hash_refresh_token(text)


class AuthenticateSeller:
    """Check an e-mail and a password, then start a refresh token family.

    An unknown e-mail and a wrong password take the same path: one lookup and
    one password check (against a dummy hash when no seller has the e-mail),
    then the same ``InvalidCredentials``.
    """

    def __init__(
        self,
        sellers: SellerRepository,
        tokens: RefreshTokenRepository,
        passwords: PasswordHasher,
        clock: Clock,
    ) -> None:
        self._sellers = sellers
        self._tokens = tokens
        self._passwords = passwords
        self._clock = clock

    async def __call__(self, email: str, password: str) -> SellerGrant:
        seller = await self._sellers.find_by_email(email)
        verified = await self._passwords.verify(
            None if seller is None else seller.password_hash, password
        )
        if seller is None or not verified:
            raise InvalidCredentials()
        now = self._clock.now()
        token = new_refresh_token()
        record = await self._tokens.issue(
            seller.id,
            hash_refresh_token(token),
            now=now,
            expires_at=now + REFRESH_FAMILY_LIFETIME,
        )
        return SellerGrant(seller.id, IssuedRefreshToken(token, record.expires_at))


class RotateRefreshToken:
    """Trade a refresh token for a grant; see the module docstring for the reuse grace."""

    def __init__(self, tokens: RefreshTokenRepository, clock: Clock) -> None:
        self._tokens = tokens
        self._clock = clock

    async def __call__(self, token: str) -> SellerGrant:
        token_hash = read_refresh_token(token)
        if token_hash is None:
            raise RefreshRefused(RefreshRefusal.MALFORMED)
        now = self._clock.now()
        successor_token = new_refresh_token()
        successor = await self._tokens.rotate(
            token_hash, hash_refresh_token(successor_token), now=now
        )
        if successor is not None:
            return SellerGrant(
                successor.seller_id, IssuedRefreshToken(successor_token, successor.expires_at)
            )
        # Nothing rotated: the token is unknown, or it is no longer active. A
        # concurrent winner has committed by now, since the losing UPDATE waited
        # for its row lock, so what this reads is final.
        current = await self._tokens.find(token_hash)
        if current is None:
            raise RefreshRefused(RefreshRefusal.UNKNOWN)
        if current.revoked_at is not None:
            raise RefreshRefused(RefreshRefusal.REVOKED)
        if current.expires_at <= now:
            raise RefreshRefused(RefreshRefusal.EXPIRED)
        if await self._within_grace(current, now):
            return SellerGrant(current.seller_id, None)
        await self._tokens.revoke_family(current.family_id, now=now)
        raise RefreshRefused(
            RefreshRefusal.REUSED, seller_id=current.seller_id, family_id=current.family_id
        )

    async def _within_grace(self, used: RefreshTokenRecord, now: datetime) -> bool:
        """Used less than 30 s before ``now``, and its successor neither revoked nor expired."""
        if used.used_at is None or used.replaced_by_id is None or now - used.used_at >= REUSE_GRACE:
            return False
        successor = await self._tokens.get(used.replaced_by_id)
        return successor is not None and successor.revoked_at is None and successor.expires_at > now


class RevokeRefreshFamily:
    """The logout: revoke the whole family of the token, whatever the token's own state."""

    def __init__(self, tokens: RefreshTokenRepository, clock: Clock) -> None:
        self._tokens = tokens
        self._clock = clock

    async def __call__(self, token: str) -> bool:
        """``True`` when the token named a family, now revoked; ``False`` for anything else."""
        token_hash = read_refresh_token(token)
        current = None if token_hash is None else await self._tokens.find(token_hash)
        if current is None:
            return False
        await self._tokens.revoke_family(current.family_id, now=self._clock.now())
        return True
