import hashlib
import hmac

import pytest
from pydantic import SecretStr

from livecart_core.application.ports import InvalidSignature
from livecart_core.signing import (
    MIN_SECRET_BYTES,
    SIGNATURE_HEADER,
    SIGNATURE_TOLERANCE_SECONDS,
    WebhookSigner,
    header_value,
)

SECRET = SecretStr("simulator-webhook-" * 2)
SIGNER = WebhookSigner(SECRET)
BODY = b'{"comments":[{"text":"je prends A12"}]}'
# 2026-09-21T14:13:20Z, as a Unix time.
NOW = 1_790_000_000


def _hmac(timestamp: int, body: bytes, secret: SecretStr = SECRET) -> str:
    payload = f"{timestamp}.".encode() + body
    return hmac.new(secret.get_secret_value().encode(), payload, hashlib.sha256).hexdigest()


def test_the_header_is_the_timestamp_and_the_hmac_sha256_of_t_dot_body() -> None:
    assert SIGNER.header(BODY, timestamp=NOW) == f"t={NOW},v1={_hmac(NOW, BODY)}"
    assert SIGNATURE_HEADER == "X-LiveCart-Signature"


@pytest.mark.parametrize("delta", [-SIGNATURE_TOLERANCE_SECONDS, -1, 0, 0.5, 300, 300.99])
def test_a_signature_verifies_within_300_s_of_the_clock_either_way(delta: float) -> None:
    # Whole seconds, like the timestamp: 300.99 s after it is still second 300.
    SIGNER.verify(SIGNER.header(BODY, timestamp=NOW), BODY, now=NOW + delta)


@pytest.mark.parametrize("now", [NOW - 301, NOW - 300.01, NOW + 301, NOW + 86_400])
def test_a_signature_outside_the_window_is_refused_whichever_way(now: float) -> None:
    # A stale signature replays a captured request; a future one would stretch the window.
    header = SIGNER.header(BODY, timestamp=NOW)

    with pytest.raises(InvalidSignature):
        SIGNER.verify(header, BODY, now=now)


def test_a_changed_body_another_secret_or_another_timestamp_does_not_verify() -> None:
    header = SIGNER.header(BODY, timestamp=NOW)
    other = WebhookSigner(SecretStr("another-webhook-key-" * 2))

    with pytest.raises(InvalidSignature):
        SIGNER.verify(header, BODY + b" ", now=NOW)
    with pytest.raises(InvalidSignature):
        other.verify(header, BODY, now=NOW)
    # The timestamp is signed: moving it into the window breaks the signature.
    with pytest.raises(InvalidSignature):
        SIGNER.verify(header.replace(f"t={NOW}", f"t={NOW + 1}"), BODY, now=NOW)


def test_an_empty_body_is_signed_like_any_other() -> None:
    SIGNER.verify(SIGNER.header(b"", timestamp=NOW), b"", now=NOW)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        f"v1={_hmac(NOW, BODY)},t={NOW}",
        f"t={NOW},v1={_hmac(NOW, BODY).upper()}",
        f"t={NOW}, v1={_hmac(NOW, BODY)}",
        f" t={NOW},v1={_hmac(NOW, BODY)}",
        f"t={NOW},v1={_hmac(NOW, BODY)},v0=00",
        f"t={NOW}.0,v1={_hmac(NOW, BODY)}",
        f"t=-{NOW},v1={_hmac(NOW, BODY)}",
        f"t={NOW},v1={_hmac(NOW, BODY)[:-1]}",
        f"t={NOW}",
        f"v1={_hmac(NOW, BODY)}",
        f"t=1{NOW:012d},v1={_hmac(NOW, BODY)}",
    ],
)
def test_a_missing_or_malformed_header_is_refused(header: str | None) -> None:
    with pytest.raises(InvalidSignature):
        SIGNER.verify(header, BODY, now=NOW)


def test_the_header_is_found_whatever_its_case_and_only_when_alone() -> None:
    assert header_value({"x-livecart-signature": "a"}, SIGNATURE_HEADER) == "a"
    assert header_value({"X-LIVECART-SIGNATURE": "a"}, SIGNATURE_HEADER) == "a"
    repeated = {"x-livecart-signature": "a", "X-LiveCart-Signature": "b"}
    assert header_value(repeated, SIGNATURE_HEADER) is None
    assert header_value({"content-type": "application/json"}, SIGNATURE_HEADER) is None


def test_a_short_secret_is_refused_and_a_secret_never_shows() -> None:
    with pytest.raises(ValueError, match=f"at least {MIN_SECRET_BYTES} bytes"):
        WebhookSigner(SecretStr("x" * (MIN_SECRET_BYTES - 1)))

    assert SECRET.get_secret_value() not in repr(SIGNER)
    assert SECRET.get_secret_value() not in SIGNER.header(BODY, timestamp=NOW)


def test_a_timestamp_before_1970_is_refused_when_signing() -> None:
    with pytest.raises(ValueError, match="Unix time"):
        SIGNER.header(BODY, timestamp=-1)
