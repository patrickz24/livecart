import re
import secrets

import pytest

from livecart_core.domain.errors import DomainError, InvalidOrderReference
from livecart_core.domain.reference import (
    CROCKFORD_ALPHABET,
    REFERENCE_SPACE,
    OrderReference,
    RandomSource,
    encode_reference,
    generate_order_reference,
    parse_order_reference,
)
from livecart_core.testing.randomness import SequenceRandomSource

CANONICAL = re.compile(r"LC-[0-9A-HJKMNP-TV-Z]{6}")

# Non-ASCII input is built from code points so that the source stays unambiguous:
# "LC-7K3F9Q" as typed on a CJK keyboard (fullwidth forms sit 0xFEE0 above ASCII).
FULLWIDTH_REFERENCE = "".join(chr(0xFEE0 + ord(char)) for char in "LC-7K3F9Q")
SMALL_E_ACUTE = chr(0x00E9)


def test_the_alphabet_is_crockford_base32() -> None:
    assert len(CROCKFORD_ALPHABET) == 32
    assert len(set(CROCKFORD_ALPHABET)) == 32
    assert "".join(sorted(CROCKFORD_ALPHABET)) == CROCKFORD_ALPHABET
    assert not set("ILOU") & set(CROCKFORD_ALPHABET)
    assert set(CROCKFORD_ALPHABET) < set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def test_six_symbols_give_two_to_the_thirty_references() -> None:
    assert REFERENCE_SPACE == 32**6 == 2**30 == 1_073_741_824


@pytest.mark.parametrize("value", ["LC-7K3F9Q", "LC-000000", "LC-ZZZZZZ", "LC-0A1B2C"])
def test_a_canonical_reference_is_lc_then_six_symbols(value: str) -> None:
    reference = OrderReference(value)

    assert reference.value == value
    assert str(reference) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "LC-",
        "LC-7K3F9",
        "LC-7K3F9QQ",
        "lc-7K3F9Q",
        "LC7K3F9Q",
        "LC-7K3F9I",
        "LC-7K3F9L",
        "LC-7K3F9O",
        "LC-7K3F9U",
        "XX-7K3F9Q",
        "LC-7K3F9*",
        " LC-7K3F9Q",
    ],
)
def test_the_constructor_takes_the_canonical_form_only(value: str) -> None:
    with pytest.raises(InvalidOrderReference):
        OrderReference(value)


def test_references_are_values() -> None:
    assert OrderReference("LC-7K3F9Q") == OrderReference("LC-7K3F9Q")
    assert len({OrderReference("LC-7K3F9Q"), OrderReference("LC-7K3F9Q")}) == 1


@pytest.mark.parametrize(
    ("index", "expected"),
    [
        (0, "LC-000000"),
        (1, "LC-000001"),
        (9, "LC-000009"),
        (10, "LC-00000A"),
        (31, "LC-00000Z"),
        (32, "LC-000010"),
        (32**5, "LC-100000"),
        (REFERENCE_SPACE - 1, "LC-ZZZZZZ"),
    ],
)
def test_encoding_writes_the_index_in_big_endian_base32(index: int, expected: str) -> None:
    assert encode_reference(index) == OrderReference(expected)


@pytest.mark.parametrize("index", [-1, REFERENCE_SPACE, REFERENCE_SPACE + 1])
def test_encoding_refuses_an_index_outside_the_space(index: int) -> None:
    with pytest.raises(ValueError, match="index must be from 0 to 1073741823"):
        encode_reference(index)


def test_generation_draws_one_uniform_index_over_the_whole_space() -> None:
    source = SequenceRandomSource([0, REFERENCE_SPACE - 1])

    first, second = generate_order_reference(source), generate_order_reference(source)

    assert (first, second) == (OrderReference("LC-000000"), OrderReference("LC-ZZZZZZ"))
    assert source.requested_bounds == [REFERENCE_SPACE, REFERENCE_SPACE]


def test_a_scripted_source_can_force_a_collision() -> None:
    source = SequenceRandomSource([123_456, 123_456])

    assert generate_order_reference(source) == generate_order_reference(source)


def test_the_scripted_source_fails_on_an_unscripted_draw() -> None:
    source = SequenceRandomSource([1])
    generate_order_reference(source)

    with pytest.raises(AssertionError, match="no scripted value left"):
        generate_order_reference(source)


def test_the_scripted_source_refuses_a_value_outside_the_bound() -> None:
    with pytest.raises(AssertionError, match="not below"):
        generate_order_reference(SequenceRandomSource([REFERENCE_SPACE]))


def test_generation_defaults_to_the_secrets_module() -> None:
    reference = generate_order_reference()

    assert CANONICAL.fullmatch(reference.value)


def test_the_secrets_module_is_a_random_source() -> None:
    source: RandomSource = secrets

    assert 0 <= source.randbelow(REFERENCE_SPACE) < REFERENCE_SPACE


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("LC-7K3F9Q", "LC-7K3F9Q"),
        ("lc-7k3f9q", "LC-7K3F9Q"),
        ("Lc-7k3F9q", "LC-7K3F9Q"),
        ("LC7K3F9Q", "LC-7K3F9Q"),
        ("lc7k3f9q", "LC-7K3F9Q"),
        ("  LC-7K3F9Q\n", "LC-7K3F9Q"),
        ("LC-7K3F9O", "LC-7K3F90"),
        ("LC-OOOOOO", "LC-000000"),
        ("LC-IL3F9Q", "LC-113F9Q"),
        ("lc-l1i1l1", "LC-111111"),
        ("lco0i1l1", "LC-001111"),
        (FULLWIDTH_REFERENCE, "LC-7K3F9Q"),
    ],
)
def test_parsing_tolerates_case_a_missing_hyphen_and_misread_symbols(
    typed: str, expected: str
) -> None:
    assert parse_order_reference(typed) == OrderReference(expected)


@pytest.mark.parametrize("symbol", list(CROCKFORD_ALPHABET))
def test_every_symbol_of_the_alphabet_reads_back_as_itself(symbol: str) -> None:
    value = f"LC-{symbol * 6}"

    assert parse_order_reference(value) == OrderReference(value)
    assert parse_order_reference(value.lower()) == OrderReference(value)


@pytest.mark.parametrize(
    "typed",
    [
        "",
        "LC-",
        "LC-7K3F9",
        "LC-7K3F9QQ",
        "LC-7K3F9U",
        "LC-UUUUUU",
        "LC--7K3F9Q",
        "LC 7K3F9Q",
        "LC-7K3-F9Q",
        "7K3F9Q",
        "XX-7K3F9Q",
        "LC-7K3F9*",
        "LC-7K3F9" + SMALL_E_ACUTE,
        "order LC-7K3F9Q",
    ],
)
def test_parsing_rejects_anything_else(typed: str) -> None:
    with pytest.raises(InvalidOrderReference) as excinfo:
        parse_order_reference(typed)

    assert isinstance(excinfo.value, DomainError)


@pytest.mark.parametrize("index", [0, 1, 31, 32, 12_345_678, REFERENCE_SPACE - 1])
def test_a_generated_reference_reads_back_as_itself(index: int) -> None:
    reference = encode_reference(index)

    assert parse_order_reference(str(reference)) == reference
    assert parse_order_reference(str(reference).lower().replace("-", "")) == reference
