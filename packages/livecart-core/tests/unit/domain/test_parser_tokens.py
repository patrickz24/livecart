import pytest

from livecart_core.domain.catalog import Catalog
from livecart_core.domain.codes import ProductCode
from livecart_core.domain.parser.normalization import normalize_comment
from livecart_core.domain.parser.tokens import (
    TOO_LARGE,
    Code,
    GluedQuantity,
    Mark,
    MarkKind,
    Number,
    Token,
    UnknownCode,
    Word,
    code_readings,
    lookalike_number,
    number_value,
    tokenize,
)
from livecart_core.testing.builders import catalog, catalog_product, demo_catalog

FULLWIDTH_C3 = chr(0xFF23) + chr(0xFF13)
SHOPPING_CART = chr(0x1F6D2)
CJK_PRETTY = chr(0x6F02) + chr(0x4EAE)  # "pretty" in Chinese


def tokens(text: str, products: Catalog | None = None) -> list[Token]:
    return tokenize(normalize_comment(text), products if products is not None else demo_catalog())


def shapes(text: str, products: Catalog | None = None) -> list[str]:
    """A compact view of the tokens: kind and folded text."""
    return [f"{type(token).__name__}:{token.text}" for token in tokens(text, products)]


def test_a_catalog_code_as_typed_is_an_exact_code() -> None:
    [token] = tokens("A12")

    assert token == Code("a12", 0, 3, code=ProductCode("A12"), corrected=False)


@pytest.mark.parametrize(
    ("typed", "code"),
    [("Al2", "A12"), ("AI2", "A12"), ("BO7", "B07"), ("bo7", "B07"), ("El5", "E15")],
)
def test_a_look_alike_swap_resolves_when_one_catalog_code_matches(typed: str, code: str) -> None:
    [token] = tokens(typed)

    assert isinstance(token, Code)
    assert token.code == ProductCode(code)
    assert token.corrected


def test_an_exact_code_wins_over_a_swap_reading() -> None:
    both = catalog(catalog_product("AL2", "Autre"), catalog_product("A12", "Robe"))
    [token] = tokens("Al2", both)

    assert token == Code("al2", 0, 3, code=ProductCode("AL2"), corrected=False)


def test_a_swap_can_join_what_the_digit_letter_split_would_cut() -> None:
    # "a1o" would split into "a1" and "o"; read whole, it is A10.
    ten = catalog(catalog_product("A10", "Dix"))
    [token] = tokens("A1O", ten)

    assert token == Code("a1o", 0, 3, code=ProductCode("A10"), corrected=True)


def test_several_swap_readings_make_an_unknown_code_with_candidates() -> None:
    lookalikes = catalog(catalog_product("A10", "Un"), catalog_product("AI0", "Deux"))
    [token] = tokens("A1O", lookalikes)

    assert token == UnknownCode("a1o", 0, 3, candidates=(ProductCode("A10"), ProductCode("AI0")))


@pytest.mark.parametrize("typed", ["A13", "A1O", "Q7", "B7", "mp3"])
def test_a_code_shaped_word_off_the_catalog_is_an_unknown_code(typed: str) -> None:
    [token] = tokens(typed)

    assert isinstance(token, UnknownCode)
    assert token.candidates == ()


@pytest.mark.parametrize("typed", ["lol", "sold", "12", "abcd1", "2x2", "x"])
def test_words_without_the_shape_of_a_code_are_not_codes(typed: str) -> None:
    assert not any(isinstance(token, Code | UnknownCode) for token in tokens(typed))


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("A12M", ["Code:a12", "Word:m"]),
        ("a12l", ["Code:a12", "Word:l"]),
        ("C3x2", ["Code:c3", "GluedQuantity:x2"]),
        ("2xC3", ["Number:2", "Word:x", "Code:c3"]),
        ("2C3", ["Number:2", "Code:c3"]),
        ("B07x2", ["Code:b07", "GluedQuantity:x2"]),
        ("xB07", ["Word:x", "Code:b07"]),
        ("2pcs", ["Number:2", "Word:pcs"]),
        ("2x", ["Number:2", "Word:x"]),
        ("A12Lx2", ["Code:a12", "Word:l", "GluedQuantity:x2"]),
        ("C3rougex2", ["Code:c3", "Word:rouge", "GluedQuantity:x2"]),
        ("soldB07", ["Word:sold", "Code:b07"]),
        ("prendsA12M", ["Word:prends", "Code:a12", "Word:m"]),
        ("takeBO7x2", ["Word:take", "Code:bo7", "GluedQuantity:x2"]),
        # A verb and a code off the catalog: no split.
        ("soldQ7", ["Word:soldq7"]),
    ],
)
def test_glued_words_split_where_a_digit_meets_a_letter(typed: str, expected: list[str]) -> None:
    assert shapes(typed) == expected


def test_a_split_that_finds_no_catalog_code_keeps_the_code_shaped_word_whole() -> None:
    assert shapes("A1O") == ["UnknownCode:a1o"]
    # Neither half is a code: the split pieces stay.
    assert shapes("Q7x2") == ["UnknownCode:q7", "GluedQuantity:x2"]


@pytest.mark.parametrize(
    ("typed", "value"), [("x2", 2), ("X10", 10), ("qte3", 3), ("qty4", 4), ("nb5", 5)]
)
def test_glued_quantities(typed: str, value: int) -> None:
    [token] = tokens(typed)

    assert isinstance(token, GluedQuantity)
    assert token.value == value


def test_a_catalog_code_wins_over_a_glued_quantity() -> None:
    with_x2 = catalog(catalog_product("X2", "Produit X2"))
    [token] = tokens("x2", with_x2)

    assert isinstance(token, Code)
    assert token.code == ProductCode("X2")


def test_a_code_typed_like_a_quantity_is_one_next_to_another_product() -> None:
    with_x2 = catalog(catalog_product("X2", "Gadget"), catalog_product("B07", "Sac"))

    assert shapes("B07 x2", with_x2) == ["Code:b07", "GluedQuantity:x2"]
    assert shapes("x2B07", with_x2) == ["GluedQuantity:x2", "Code:b07"]
    assert shapes("Q7 x2", with_x2) == ["UnknownCode:q7", "GluedQuantity:x2"]
    # No other product: the code.
    assert shapes("x2 x2", with_x2) == ["Code:x2", "Code:x2"]


@pytest.mark.parametrize("typed", ["2xl", "3XS", "4xl"])
def test_a_numbered_size_is_one_word(typed: str) -> None:
    assert shapes(typed) == [f"Word:{typed.lower()}"]


@pytest.mark.parametrize("typed", ["14h", "10h30", "20min", "3e", "2eme", "1er", "2nd", "15e"])
def test_a_time_or_an_ordinal_is_one_word(typed: str) -> None:
    # Split, "10h30" would leave a code-shaped "h30" and a quantity of 10.
    assert shapes(typed) == [f"Word:{typed}"]


def test_marks_are_tokens_of_their_own() -> None:
    marks = [
        token
        for token in tokens(f"je n'en veux ? {SHOPPING_CART} 15,90$")
        if isinstance(token, Mark)
    ]

    assert [mark.kind for mark in marks] == [
        MarkKind.NEGATION,
        MarkKind.QUESTION,
        MarkKind.CART,
        MarkKind.DECIMAL,
        MarkKind.AMOUNT_UNIT,
    ]


def test_a_word_in_another_script_never_holds_a_code() -> None:
    assert shapes(f"C3{CJK_PRETTY}") == ["Code:c3", f"Word:{CJK_PRETTY}"]


def test_tokens_quote_the_original_text() -> None:
    text = f"je prends {FULLWIDTH_C3} x2"
    by_kind = {type(token): text[token.start : token.end] for token in tokens(text)}

    assert by_kind[Code] == FULLWIDTH_C3
    assert by_kind[GluedQuantity] == "x2"


def test_numbers_keep_their_value_up_to_six_significant_digits() -> None:
    assert number_value("7") == 7
    assert number_value("007") == 7
    assert number_value("999999") == 999999
    assert number_value("1000000") == TOO_LARGE
    assert number_value("0") == 0
    assert number_value("000") == 0
    # Far beyond what int() would convert by default: never converted at all.
    assert number_value("9" * 5000) == TOO_LARGE
    # Leading zeros count towards that limit too.
    assert number_value("0" * 5000 + "2") == 2
    [token] = tokens("0000000000002")
    assert token == Number("0000000000002", 0, 13, value=2)


@pytest.mark.parametrize("typed", ["1O", "1o", "x1O", "xl0", "XI5", "qte1o"])
def test_a_number_typed_with_look_alike_letters_is_a_word_not_a_code(typed: str) -> None:
    assert shapes(typed) == [f"Word:{typed.lower()}"]


def test_the_value_of_a_number_typed_with_look_alike_letters() -> None:
    assert lookalike_number("1o") == 10
    assert lookalike_number("x1o") == 10
    assert lookalike_number("xl0") == 10
    assert lookalike_number("qte1o") == 10
    # Nothing to swap back, no digit at all, or the shape of a code.
    assert lookalike_number("10") is None
    assert lookalike_number("lol") is None
    assert lookalike_number("lo1") is None
    assert lookalike_number("a1o") is None
    assert lookalike_number(chr(0x0663) + "o") is None


def test_a_word_that_may_be_a_code_stays_an_unknown_code() -> None:
    assert shapes("lo1") == ["UnknownCode:lo1"]


def test_code_readings_cover_both_directions_of_the_swaps() -> None:
    assert code_readings("a1o") == {"A10", "AI0", "AL0"}
    assert code_readings("al2") == {"A12", "AL2"}
    assert code_readings("a12") == {"A12", "AI2", "AL2"}
    # Five digits after one letter are too many; after "a1" read as letters, four remain.
    assert code_readings("a12345") == {"AI2345", "AL2345"}
    assert code_readings("lol") == frozenset()
    assert code_readings("12") == frozenset()
    assert code_readings("abcd1") == frozenset()


def test_an_empty_catalog_reads_every_code_as_unknown() -> None:
    assert shapes("A12 x2", Catalog(())) == ["UnknownCode:a12", "GluedQuantity:x2"]
    assert shapes("Word", Catalog(())) == ["Word:word"]
    assert tokens("", Catalog(())) == []
    assert isinstance(tokens("A", Catalog(()))[0], Word)
