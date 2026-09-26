"""Order access tokens: derived from the order id by HMAC, stored only as a hash.

The reference of an order (``LC-7K3F9Q``) is read aloud on a live stream and
has about 2**30 values, so it only identifies an order: its status is public.
Paying, seeing the address and following the delivery need the order's
access token as well, sent as ``Authorization: Bearer <token>`` (OWASP API1).

- The token is ``base64url(HMAC-SHA256(ORDER_TOKEN_KEY, order_id))`` without
  padding: 256 bits in 43 characters, over the 16 bytes of the order's UUID.
  Nothing random goes in, so a redelivered comment recomputes exactly the
  token the buyer already received, and nothing about it needs storing.
- ``orders.access_token_hash`` holds ``SHA-256`` of the 32 bytes of the HMAC:
  a database leak alone gives no usable token, and verifying a token needs
  its hash only, compared in constant time by ``hmac.compare_digest``.
- ``ORDER_TOKEN_KEY`` is at least 32 bytes and distinct from ``JWT_SECRET``
  (checked by the settings). Whoever holds it can compute every token, hence
  its rotation on the roadmap.
"""

import base64
import binascii
import hashlib
import hmac
from typing import Final
from uuid import UUID

from pydantic import SecretStr

# The shortest key accepted: as long as the HMAC-SHA256 output.
MIN_KEY_BYTES: Final = 32
TOKEN_BYTES: Final = 32
# base64url of 32 bytes, without its one "=" of padding.
TOKEN_LENGTH: Final = 43


def _encode(digest: bytes) -> str:
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _decode(token: str) -> bytes | None:
    """The 32 bytes of a well-formed token, or ``None`` for anything else."""
    if len(token) != TOKEN_LENGTH or not token.isascii():
        return None
    try:
        # validate=True refuses characters outside the alphabet instead of skipping them.
        raw = base64.b64decode(token + "=", altchars=b"-_", validate=True)
    except binascii.Error:
        return None
    # A token has one canonical spelling: the last symbol carries 2 unused bits.
    return raw if _encode(raw) == token else None


def hash_of(digest: bytes) -> bytes:
    """What ``orders.access_token_hash`` stores for a token's 32 bytes."""
    return hashlib.sha256(digest).digest()


class OrderTokens:
    """The access tokens of the orders, under one key; the key never shows in a repr."""

    def __init__(self, key: SecretStr) -> None:
        secret = key.get_secret_value().encode()
        if len(secret) < MIN_KEY_BYTES:
            raise ValueError(f"an order token key needs at least {MIN_KEY_BYTES} bytes")
        self._key = secret

    def __repr__(self) -> str:
        return "OrderTokens(key=SecretStr('**********'))"

    def _digest(self, order_id: UUID) -> bytes:
        return hmac.new(self._key, order_id.bytes, hashlib.sha256).digest()

    def token_for(self, order_id: UUID) -> str:
        """The order's token, the same on every call: for the buyer, never for a log line."""
        return _encode(self._digest(order_id))

    def stored_hash(self, order_id: UUID) -> bytes:
        """The ``orders.access_token_hash`` of a new order."""
        return hash_of(self._digest(order_id))

    @staticmethod
    def verify(token: str, stored_hash: bytes) -> bool:
        """Whether ``token`` is the one whose hash is stored, in constant time.

        A malformed token is refused before any comparison: its length and
        alphabet are public, so rejecting it early reveals nothing.
        """
        raw = _decode(token)
        if raw is None:
            return False
        return hmac.compare_digest(hash_of(raw), stored_hash)
