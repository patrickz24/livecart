"""Timestamped HMAC signatures of webhook bodies: ``X-LiveCart-Signature: t=<unix>,v1=<hex>``.

The simulator signs the comment webhooks it posts with this scheme, the one
Stripe uses for its own webhooks, and the API checks it before it reads the
body (``PlatformAdapter.verify``):

- the signed payload is ``<t>.<body>``: the Unix time of signing in whole
  seconds, a dot, then the exact bytes of the body, so that a signature
  covers when it was made as well as what it signs;
- ``v1`` is the HMAC-SHA256 of that payload under the platform's secret
  (``COMMENT_WEBHOOK_SECRET_<PLATFORM>``, 32 bytes at least), in lower-case
  hexadecimal;
- the receiver recomputes it and compares the two in constant time
  (``hmac.compare_digest``), then refuses a timestamp more than 300 s away
  from its own clock, in either direction: a captured request replays for
  five minutes at most, and a signature dated in the future cannot stretch
  that. The inbox deduplicates what replays within the window.

Anything else, a missing, repeated or malformed header included, raises
``InvalidSignature``, whose message the API never repeats: a sender learns
nothing about which check failed.
"""

import hashlib
import hmac
import re
from collections.abc import Mapping
from typing import Final

from pydantic import SecretStr

from livecart_core.application.ports import InvalidSignature
from livecart_core.order_tokens import MIN_KEY_BYTES

SIGNATURE_HEADER: Final = "X-LiveCart-Signature"
SIGNATURE_TOLERANCE_SECONDS: Final = 300
# The shortest secret accepted: as long as the HMAC-SHA256 output.
MIN_SECRET_BYTES: Final = MIN_KEY_BYTES
# One timestamp of at most 12 digits (until the year 33658), then one signature.
_HEADER_PATTERN: Final = re.compile(r"t=(\d{1,12}),v1=([0-9a-f]{64})")


def header_value(headers: Mapping[str, str], name: str) -> str | None:
    """The value of the one header called ``name``, whatever its case.

    ``None`` when it is missing or repeated: two signatures on one request
    leave it unclear which one was checked, so neither is.
    """
    wanted = name.lower()
    values = [value for key, value in headers.items() if key.lower() == wanted]
    return values[0] if len(values) == 1 else None


class WebhookSigner:
    """Signs and verifies webhook bodies under one secret, which never shows in a repr."""

    def __init__(self, secret: SecretStr) -> None:
        key = secret.get_secret_value().encode()
        if len(key) < MIN_SECRET_BYTES:
            raise ValueError(f"a webhook secret needs at least {MIN_SECRET_BYTES} bytes")
        self._key = key

    def __repr__(self) -> str:
        return "WebhookSigner(secret=SecretStr('**********'))"

    def _digest(self, body: bytes, timestamp: int) -> str:
        payload = str(timestamp).encode("ascii") + b"." + body
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()

    def header(self, body: bytes, *, timestamp: int) -> str:
        """The ``X-LiveCart-Signature`` value that signs ``body`` at ``timestamp``."""
        if timestamp < 0:
            raise ValueError(f"a signature timestamp is a Unix time, not {timestamp}")
        return f"t={timestamp},v1={self._digest(body, timestamp)}"

    def verify(
        self,
        header: str | None,
        body: bytes,
        *,
        now: float,
        tolerance_seconds: int = SIGNATURE_TOLERANCE_SECONDS,
    ) -> None:
        """Raise ``InvalidSignature`` unless ``header`` signs ``body`` close enough to ``now``.

        ``now`` is a Unix time from the receiver's ``Clock``; close enough is
        within ``tolerance_seconds``, before or after.
        """
        match = _HEADER_PATTERN.fullmatch(header) if header is not None else None
        if match is None:
            raise InvalidSignature("the signature header is missing or malformed")
        timestamp, signature = int(match[1]), match[2]
        if not hmac.compare_digest(signature, self._digest(body, timestamp)):
            raise InvalidSignature("the signature does not match the body")
        if abs(now - timestamp) > tolerance_seconds:
            raise InvalidSignature("the signature is outside the tolerance window")
