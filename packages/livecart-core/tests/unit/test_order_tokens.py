import base64
import hashlib
import hmac
from collections.abc import Callable
from uuid import UUID

import pytest
from pydantic import SecretStr

from livecart_core.order_tokens import MIN_KEY_BYTES, TOKEN_LENGTH, OrderTokens, hash_of

KEY = SecretStr("order-token-" * 3)
# Its token holds "-" and lower-case letters, so that every mangling below changes it.
ORDER = UUID("0192f6d4-5a1b-7c3d-8e4f-a0b1c2d3e0f7")
OTHER_ORDER = UUID("0192f6d4-5a1b-7c3d-8e4f-a0b1c2d3e0f8")
TOKENS = OrderTokens(KEY)


def test_a_token_is_the_base64url_hmac_sha256_of_the_order_id() -> None:
    expected = hmac.new(KEY.get_secret_value().encode(), ORDER.bytes, hashlib.sha256).digest()

    token = TOKENS.token_for(ORDER)

    assert token == base64.urlsafe_b64encode(expected).rstrip(b"=").decode()
    assert len(token) == TOKEN_LENGTH
    assert "=" not in token
    # 256 bits, in the URL-safe alphabet.
    assert len(base64.urlsafe_b64decode(token + "=")) == 32
    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def test_a_token_is_recomputed_identically_and_is_one_per_order_and_key() -> None:
    assert TOKENS.token_for(ORDER) == OrderTokens(KEY).token_for(ORDER)
    assert TOKENS.token_for(ORDER) != TOKENS.token_for(OTHER_ORDER)
    assert TOKENS.token_for(ORDER) != OrderTokens(SecretStr("another-key-" * 3)).token_for(ORDER)


def test_the_stored_hash_is_the_sha256_of_the_hmac() -> None:
    digest = hmac.new(KEY.get_secret_value().encode(), ORDER.bytes, hashlib.sha256).digest()

    stored = TOKENS.stored_hash(ORDER)

    assert stored == hashlib.sha256(digest).digest() == hash_of(digest)
    assert len(stored) == 32
    # The hash is not the token, nor the HMAC it encodes.
    assert stored not in (digest, TOKENS.token_for(ORDER).encode())


def test_the_token_of_the_order_verifies_against_its_stored_hash() -> None:
    stored = TOKENS.stored_hash(ORDER)

    assert OrderTokens.verify(TOKENS.token_for(ORDER), stored)
    assert not OrderTokens.verify(TOKENS.token_for(OTHER_ORDER), stored)
    assert not OrderTokens.verify(
        OrderTokens(SecretStr("another-key-" * 3)).token_for(ORDER), stored
    )


def _flip_last(token: str) -> str:
    return token[:-1] + ("A" if token[-1] != "A" else "B")


@pytest.mark.parametrize(
    "mangle",
    [
        pytest.param(_flip_last, id="one symbol changed"),
        pytest.param(lambda token: token[:-1], id="one symbol short"),
        pytest.param(lambda token: token + "A", id="one symbol long"),
        pytest.param(lambda token: token + "=", id="with padding"),
        pytest.param(lambda token: token.replace("-", "+").replace("_", "/"), id="standard base64"),
        pytest.param(lambda token: "!" + token[1:], id="outside the alphabet"),
        pytest.param(lambda token: "é" + token[1:], id="not ascii"),
        pytest.param(lambda token: "", id="empty"),
        pytest.param(lambda token: token.upper(), id="upper case"),
    ],
)
def test_a_mangled_token_never_verifies(mangle: Callable[[str], str]) -> None:
    token = TOKENS.token_for(ORDER)
    mangled = mangle(token)

    assert mangled != token
    assert not OrderTokens.verify(mangled, TOKENS.stored_hash(ORDER))


def test_a_token_whose_unused_bits_are_set_is_not_its_canonical_spelling() -> None:
    token = TOKENS.token_for(ORDER)
    # The last symbol of 32 bytes carries 4 bits: its 2 low bits are always zero.
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    sibling = token[:-1] + alphabet[alphabet.index(token[-1]) | 1]

    assert base64.urlsafe_b64decode(sibling + "=") == base64.urlsafe_b64decode(token + "=")
    assert not OrderTokens.verify(sibling, TOKENS.stored_hash(ORDER))


def test_verification_compares_the_hashes_in_constant_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compared: list[tuple[bytes, bytes]] = []
    real_compare_digest = hmac.compare_digest

    def compare_digest(left: bytes, right: bytes) -> bool:
        compared.append((left, right))
        return real_compare_digest(left, right)

    # The module looks compare_digest up on hmac at each call.
    monkeypatch.setattr(hmac, "compare_digest", compare_digest)

    assert OrderTokens.verify(TOKENS.token_for(ORDER), TOKENS.stored_hash(ORDER))
    assert compared == [(TOKENS.stored_hash(ORDER), TOKENS.stored_hash(ORDER))]


def test_a_key_shorter_than_32_bytes_is_refused() -> None:
    with pytest.raises(ValueError, match=f"at least {MIN_KEY_BYTES} bytes"):
        OrderTokens(SecretStr("k" * (MIN_KEY_BYTES - 1)))

    # Bytes, not characters: 16 two-byte characters are long enough.
    OrderTokens(SecretStr("é" * 16))


def test_the_key_never_shows_in_a_repr() -> None:
    assert KEY.get_secret_value() not in repr(TOKENS)
    assert KEY.get_secret_value() not in str(TOKENS)
