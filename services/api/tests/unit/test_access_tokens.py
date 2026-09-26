"""Seller access tokens: what is issued, and every token that must be refused."""

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
import pytest
from pydantic import SecretStr

from livecart_api.access_tokens import (
    ACCESS_TOKEN_TTL,
    AUDIENCE,
    ISSUER,
    REQUIRED_CLAIMS,
    AccessTokens,
    InvalidAccessToken,
)
from livecart_api.testing import current_time_clock
from livecart_core.testing.keys import TEST_JWT_SECRET, TEST_ORDER_TOKEN_KEY

SELLER_ID = UUID("0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b")
KEY = TEST_JWT_SECRET.get_secret_value()


def _tokens() -> AccessTokens:
    return AccessTokens(TEST_JWT_SECRET, current_time_clock())


def _claims(**overrides: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(SELLER_ID),
        "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        "jti": "token-id",
    }
    claims.update(overrides)
    return {name: value for name, value in claims.items() if value is not None}


def _b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def test_an_issued_token_verifies_and_names_its_seller() -> None:
    clock = current_time_clock()
    tokens = AccessTokens(TEST_JWT_SECRET, clock)

    issued = tokens.issue(SELLER_ID)
    claims = tokens.verify(issued.value)

    assert claims.seller_id == SELLER_ID
    assert claims.expires_at == issued.expires_at
    assert clock.now() + ACCESS_TOKEN_TTL - timedelta(seconds=1) < issued.expires_at
    assert issued.expires_at <= clock.now() + ACCESS_TOKEN_TTL
    assert tokens.ttl == ACCESS_TOKEN_TTL == timedelta(seconds=900)


def test_the_token_holds_the_required_claims_under_hs256() -> None:
    issued = _tokens().issue(SELLER_ID)

    header = jwt.get_unverified_header(issued.value)
    payload = jwt.decode(issued.value, options={"verify_signature": False})

    assert header == {"alg": "HS256", "typ": "JWT"}
    assert set(payload) == set(REQUIRED_CLAIMS)
    assert (payload["iss"], payload["aud"], payload["sub"]) == (ISSUER, AUDIENCE, str(SELLER_ID))


def test_every_token_has_its_own_id() -> None:
    tokens = _tokens()

    ids = {tokens.verify(tokens.issue(SELLER_ID).value).token_id for _ in range(20)}

    assert len(ids) == 20


def test_the_alg_none_token_is_refused() -> None:
    unsigned = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(_claims())}."

    with pytest.raises(InvalidAccessToken, match="InvalidAlgorithmError"):
        _tokens().verify(unsigned)


def test_a_token_signed_with_another_algorithm_is_refused() -> None:
    # HS512 wants a 64-byte key; the algorithm is refused before any signature check.
    other = jwt.encode(_claims(), KEY * 2, algorithm="HS512")

    with pytest.raises(InvalidAccessToken, match="InvalidAlgorithmError"):
        _tokens().verify(other)


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"aud": "livecart-buyer"}, "InvalidAudienceError"),
        ({"iss": "someone-else"}, "InvalidIssuerError"),
        ({"exp": int((datetime.now(UTC) - timedelta(seconds=1)).timestamp())}, "Expired"),
        ({"sub": "not-a-uuid"}, "ValueError"),
        ({"sub": 42}, "InvalidSubjectError"),
        ({"jti": 7}, "InvalidJTIError"),
    ],
)
def test_a_token_with_a_wrong_claim_is_refused(overrides: dict[str, Any], error: str) -> None:
    token = jwt.encode(_claims(**overrides), KEY, algorithm="HS256")

    with pytest.raises(InvalidAccessToken, match=error):
        _tokens().verify(token)


@pytest.mark.parametrize("missing", REQUIRED_CLAIMS)
def test_a_token_missing_a_required_claim_is_refused(missing: str) -> None:
    token = jwt.encode(_claims(**{missing: None}), KEY, algorithm="HS256")

    with pytest.raises(InvalidAccessToken):
        _tokens().verify(token)


def test_an_expired_token_issued_by_the_codec_is_refused() -> None:
    clock = current_time_clock()
    clock.set(clock.now() - ACCESS_TOKEN_TTL - timedelta(seconds=1))
    stale = AccessTokens(TEST_JWT_SECRET, clock).issue(SELLER_ID)

    with pytest.raises(InvalidAccessToken, match="ExpiredSignatureError"):
        _tokens().verify(stale.value)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not.a.jwt",
        # Another key, of the right length.
        jwt.encode(_claims(), TEST_ORDER_TOKEN_KEY.get_secret_value(), algorithm="HS256"),
    ],
)
def test_a_malformed_or_foreign_token_is_refused(token: str) -> None:
    with pytest.raises(InvalidAccessToken):
        _tokens().verify(token)


def test_a_tampered_payload_breaks_the_signature() -> None:
    header, _, signature = _tokens().issue(SELLER_ID).value.split(".")
    forged = f"{header}.{_b64(_claims(sub=str(UUID(int=1))))}.{signature}"

    with pytest.raises(InvalidAccessToken, match="InvalidSignatureError"):
        _tokens().verify(forged)


def test_the_secret_and_the_tokens_stay_out_of_reprs() -> None:
    tokens = AccessTokens(SecretStr("a-secret-of-thirty-two-bytes-ok!"), current_time_clock())
    issued = tokens.issue(SELLER_ID)

    assert "a-secret" not in repr(tokens)
    assert issued.value not in repr(issued)
