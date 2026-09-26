import pytest

from livecart_core.domain.parser.normalization import (
    AMOUNT_UNIT,
    CART,
    DECIMAL,
    MINUS,
    NEGATION,
    normalize_comment,
)

# Non-ASCII input is built from code points so that the source stays unambiguous.
VS16 = chr(0xFE0F)  # VARIATION SELECTOR-16, the "emoji presentation" suffix
KEYCAP = chr(0x20E3)  # COMBINING ENCLOSING KEYCAP
KEYCAP_TWO = "2" + VS16 + KEYCAP
KEYCAP_TEN = chr(0x1F51F)
MULTIPLICATION_SIGN = chr(0x00D7)
HEAVY_MULTIPLICATION_X = chr(0x2716) + VS16
SHOPPING_CART = chr(0x1F6D2)
FULLWIDTH_C3 = chr(0xFF23) + chr(0xFF13)
SMALL_E_GRAVE = chr(0x00E8)
COMBINING_ACUTE = chr(0x0301)
SHARP_S = chr(0x00DF)
RIGHT_SINGLE_QUOTE = chr(0x2019)
QUESTION_EMOJI = chr(0x2753)
INVERTED_QUESTION = chr(0x00BF)
ZERO_WIDTH_JOINER = chr(0x200D)
ZERO_WIDTH_SPACE = chr(0x200B)
EURO = chr(0x20AC)
EN_DASH = chr(0x2013)
HEART = chr(0x2764) + VS16
CIRCLED_M = chr(0x24C2)
FULLWIDTH_ONE = chr(0xFF11)
FULLWIDTH_COMMA = chr(0xFF0C)


def folded(text: str) -> str:
    return " ".join(normalize_comment(text).text.split())


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("SOLD A12 M x2", "sold a12 m x2"),
        (FULLWIDTH_C3 + " je prends", "c3 je prends"),
        (f"j{RIGHT_SINGLE_QUOTE}ach{SMALL_E_GRAVE}te", "achete"),
        (f"ache{COMBINING_ACUTE}te", "achete"),
        (f"gro{SHARP_S}", "gross"),
        (f"A12 {CIRCLED_M}", "a12 m"),
        ("  je\tprends\n\nC3  ", "je prends c3"),
    ],
    ids=["case", "fullwidth", "accent", "combining-accent", "casefold", "circled", "spaces"],
)
def test_text_is_folded_for_matching(typed: str, expected: str) -> None:
    assert folded(typed) == expected


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        (f"C3 x {KEYCAP_TWO}", "c3 x 2"),
        (f"C3 x{KEYCAP_TWO}", "c3 x2"),
        (f"C3 {KEYCAP_TEN}", "c3 10"),
        (f"C3 {MULTIPLICATION_SIGN}2", "c3 x2"),
        (f"C3{HEAVY_MULTIPLICATION_X}2", "c3x2"),
        (f"{SHOPPING_CART}C3", f"{CART} c3"),
    ],
    ids=["keycap", "keycap-glued", "keycap-ten", "times", "heavy-x", "cart"],
)
def test_emojis_that_carry_meaning_are_read(typed: str, expected: str) -> None:
    assert folded(typed) == expected


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        (f"C3 {HEART} !!!", "c3"),
        (f"C3{ZERO_WIDTH_JOINER}", "c3"),
        ("C3, A12. B07; Z99", "c3 a12 b07 z99"),
        ("#C3 (rouge)", "c3 rouge"),
        ("C3_rouge", "c3 rouge"),
    ],
    ids=["emoji", "zero-width", "punctuation", "hashtag", "underscore"],
)
def test_meaningless_characters_become_spaces(typed: str, expected: str) -> None:
    assert folded(typed) == expected


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("C3 dispo ?", "c3 dispo ?"),
        (f"C3 dispo {QUESTION_EMOJI}", "c3 dispo ?"),
        (f"{INVERTED_QUESTION}C3?", "?c3?"),
    ],
    ids=["ascii", "emoji", "inverted"],
)
def test_question_marks_are_kept(typed: str, expected: str) -> None:
    assert folded(typed) == expected


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("je prends l'A12", "je prends a12"),
        ("s'il vous plait", "il vous plait"),
        ("s il vous plait", "il vous plait"),
        ("qu ils", "ils"),
        ("M s'il", "m il"),
        ("S il", "il"),
        ("s ile", "s ile"),
        ("as il", "as il"),
        ("qu'il", "il"),
        ("je n'en veux pas", f"je {NEGATION} en veux pas"),
        (f"je n{RIGHT_SINGLE_QUOTE}en veux", f"je {NEGATION} en veux"),
        ("i'll take", "ill take"),
        ("don't", "dont"),
        ("C3 'rouge'", "c3 rouge"),
    ],
)
def test_apostrophes_elide_join_or_separate(typed: str, expected: str) -> None:
    assert folded(typed) == expected


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("A-12", "a12"),
        (f"A{EN_DASH}12", "a12"),
        ("ABC-1234", "abc1234"),
        ("T-shirt", "t shirt"),
        ("bleu-nuit", "bleu nuit"),
        ("ABCD-12", "abcd 12"),
        ("C3 - 2", "c3 2"),
    ],
)
def test_a_hyphen_only_joins_the_two_halves_of_a_code(typed: str, expected: str) -> None:
    assert folded(typed) == expected


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("x-1", f"x{MINUS}1"),
        ("qte-3", f"qte{MINUS}3"),
        ("x -1", f"x {MINUS}1"),
        ("B07 -2", f"b07 {MINUS}2"),
        (f"B07 {EN_DASH}2", f"b07 {MINUS}2"),
        ("-50%", f"{MINUS}50{AMOUNT_UNIT}"),
        ("12-10", "12 10"),
        # "x" ends another word here: still the two halves of a code.
        ("AX-2", "ax2"),
    ],
    ids=["glued-x", "glued-qte", "spaced", "bare", "en-dash", "discount", "range", "code"],
)
def test_a_dash_before_a_digit_is_a_minus_sign(typed: str, expected: str) -> None:
    assert folded(typed) == expected


def test_a_mention_names_another_viewer_and_is_dropped() -> None:
    assert folded("@marie je prends C3") == "je prends c3"
    assert folded("C3 @") == "c3"


def test_currency_symbols_and_percent_become_the_amount_unit() -> None:
    assert folded(f"15{EURO} -50% $3") == f"15{AMOUNT_UNIT} {MINUS}50{AMOUNT_UNIT} {AMOUNT_UNIT}3"


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("15,90", f"15{DECIMAL}90"),
        ("29.99", f"29{DECIMAL}99"),
        (f"{FULLWIDTH_ONE}{FULLWIDTH_COMMA}{FULLWIDTH_ONE}", f"1{DECIMAL}1"),
        ("B07, C3", "b07 c3"),
        ("x2. merci", "x2 merci"),
        ("2 ,3", "2 3"),
        (",5", "5"),
    ],
    ids=["comma", "full-stop", "fullwidth", "after-code", "after-number", "spaced", "leading"],
)
def test_a_separator_between_two_digits_becomes_the_decimal_mark(typed: str, expected: str) -> None:
    assert folded(typed) == expected


def test_every_folded_character_maps_back_to_the_original_text() -> None:
    text = f"Je prends {FULLWIDTH_C3} x {KEYCAP_TWO} ach{SMALL_E_GRAVE}te"
    result = normalize_comment(text)

    assert result.original == text
    assert len(result.starts) == len(result.ends) == len(result.text)
    code = result.text.index("c3")
    assert text[slice(*result.source_span(code, code + 2))] == FULLWIDTH_C3
    two = result.text.index("2")
    # The variation selector and the keycap enclosure belong to the digit.
    assert text[slice(*result.source_span(two, two + 1))] == KEYCAP_TWO
    verb = result.text.index("achete")
    assert text[slice(*result.source_span(verb, verb + 6))] == f"ach{SMALL_E_GRAVE}te"


def test_a_joined_word_still_quotes_the_dropped_apostrophe() -> None:
    text = "i'll"
    result = normalize_comment(text)

    assert result.text == "ill"
    assert text[slice(*result.source_span(0, 3))] == "i'll"


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        (f"je{ZERO_WIDTH_JOINER}prends", "je prends"),
        (f"je{ZERO_WIDTH_SPACE}prends", "je prends"),
        (f"B{ZERO_WIDTH_JOINER}07", "b07"),
        (f"A1{ZERO_WIDTH_SPACE}2", "a12"),
        (f"{ZERO_WIDTH_JOINER}je", "je"),
        (f"je{ZERO_WIDTH_JOINER}", "je"),
    ],
    ids=["joiner", "space", "letter-digit", "digit-digit", "leading", "trailing"],
)
def test_a_zero_width_break_between_two_letters_is_a_space(typed: str, expected: str) -> None:
    assert folded(typed) == expected


def test_leading_zero_width_characters_have_nothing_to_attach_to() -> None:
    result = normalize_comment(ZERO_WIDTH_JOINER + VS16 + "C3")

    assert result.text == "c3"
    assert result.source_span(0, 2) == (2, 4)


def test_empty_text_folds_to_empty_text() -> None:
    result = normalize_comment("")

    assert result.text == ""
    assert result.starts == result.ends == ()
