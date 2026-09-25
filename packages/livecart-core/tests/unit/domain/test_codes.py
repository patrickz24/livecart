import pytest

from livecart_core.domain.codes import (
    MAX_VARIANT_LENGTH,
    NO_VARIANT,
    ProductCode,
    Variant,
    parse_product_code,
    parse_variant,
)
from livecart_core.domain.errors import DomainError, InvalidProductCode, InvalidValue

# Non-ASCII input is built from code points so that the source stays unambiguous.
# FULLWIDTH LATIN CAPITAL LETTER C, FULLWIDTH DIGIT THREE: "C3" on a CJK keyboard.
FULLWIDTH_C3 = chr(0xFF23) + chr(0xFF13)
# FULLWIDTH LATIN SMALL LETTER C, FULLWIDTH DIGIT THREE.
FULLWIDTH_LOWER_C3 = chr(0xFF43) + chr(0xFF13)
NO_BREAK_SPACE = chr(0x00A0)
IDEOGRAPHIC_SPACE = chr(0x3000)
SUPERSCRIPT_TWO = chr(0x00B2)
CIRCLED_ONE = chr(0x2460)
SMALL_E_ACUTE = chr(0x00E9)


@pytest.mark.parametrize("value", ["A1", "A12", "C3", "AB12", "ABC1234", "Z9999", "XYZ1"])
def test_a_canonical_product_code_is_letters_then_digits(value: str) -> None:
    code = ProductCode(value)

    assert code.value == value
    assert str(code) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "A",
        "12",
        "12A",
        "ABCD1",
        "A12345",
        "A1B2",
        "a12",
        " A12",
        "A12 ",
        "A-12",
        "A 12",
        FULLWIDTH_C3,
    ],
)
def test_the_constructor_takes_the_canonical_form_only(value: str) -> None:
    with pytest.raises(InvalidProductCode):
        ProductCode(value)


def test_product_codes_are_values() -> None:
    assert ProductCode("A12") == ProductCode("A12")
    assert ProductCode("A12") != ProductCode("A13")
    assert len({ProductCode("A12"), ProductCode("A12")}) == 1


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("A12", "A12"),
        ("a12", "A12"),
        (" c3 ", "C3"),
        ("\tC3\n", "C3"),
        (FULLWIDTH_C3, "C3"),
        (FULLWIDTH_LOWER_C3, "C3"),
        (f"C3{NO_BREAK_SPACE}", "C3"),
        (f"{IDEOGRAPHIC_SPACE}C3{IDEOGRAPHIC_SPACE}", "C3"),
        (f"A{SUPERSCRIPT_TWO}", "A2"),
        (f"A{CIRCLED_ONE}", "A1"),
    ],
)
def test_parsing_normalises_case_width_and_spaces(typed: str, expected: str) -> None:
    assert parse_product_code(typed) == ProductCode(expected)


@pytest.mark.parametrize("typed", ["", "   ", "hello", "C3 x2", "je prends A12", "A12-M", "1A"])
def test_parsing_rejects_text_that_is_not_a_code_after_normalisation(typed: str) -> None:
    with pytest.raises(InvalidProductCode) as excinfo:
        parse_product_code(typed)

    assert isinstance(excinfo.value, DomainError)


def test_the_empty_variant_means_no_variant() -> None:
    assert Variant() == Variant("")
    assert Variant("") == NO_VARIANT
    assert NO_VARIANT.is_empty
    assert str(NO_VARIANT) == ""
    assert not Variant("M").is_empty


def test_a_variant_keeps_the_seller_label() -> None:
    assert Variant("M").value == "M"
    assert str(Variant("Bleu nuit")) == "Bleu nuit"
    assert Variant("M") != Variant("m")


def test_a_variant_may_use_the_whole_column_width_in_code_points() -> None:
    assert Variant("x" * MAX_VARIANT_LENGTH).value == "x" * MAX_VARIANT_LENGTH
    assert Variant(SMALL_E_ACUTE * MAX_VARIANT_LENGTH).value == SMALL_E_ACUTE * MAX_VARIANT_LENGTH


def test_a_variant_longer_than_the_column_is_rejected() -> None:
    with pytest.raises(InvalidValue, match="longer than 32"):
        Variant("x" * (MAX_VARIANT_LENGTH + 1))


@pytest.mark.parametrize("value", [" M", "M ", " M ", "\tM", "M\n", " "])
def test_surrounding_whitespace_is_rejected_rather_than_hidden(value: str) -> None:
    with pytest.raises(InvalidValue, match="whitespace"):
        Variant(value)


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("M", "M"),
        (" M ", "M"),
        ("Bleu nuit", "Bleu nuit"),
        ("", ""),
        ("   ", ""),
        (FULLWIDTH_LOWER_C3, "c3"),
        (f"M{NO_BREAK_SPACE}", "M"),
    ],
)
def test_parsing_a_variant_normalises_width_and_spaces_but_keeps_case(
    typed: str, expected: str
) -> None:
    assert parse_variant(typed) == Variant(expected)


def test_parsing_a_variant_still_enforces_the_width() -> None:
    with pytest.raises(InvalidValue, match="longer than 32"):
        parse_variant("x" * (MAX_VARIANT_LENGTH + 1))
