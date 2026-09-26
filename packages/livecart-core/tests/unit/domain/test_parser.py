from decimal import Decimal

import pytest

from livecart_core.domain.catalog import Catalog
from livecart_core.domain.codes import NO_VARIANT, ProductCode, Variant
from livecart_core.domain.outcomes import AmbiguityReason, RejectionReason
from livecart_core.domain.parser import (
    Ambiguous,
    Confidence,
    Match,
    ParseResult,
    Rejected,
    parse_comment,
)
from livecart_core.domain.quantity import Quantity
from livecart_core.testing.builders import (
    DEMO_MAX_QUANTITY,
    catalog,
    catalog_product,
    demo_catalog,
)

# Non-ASCII input is built from code points so that the source stays unambiguous.
VS16 = chr(0xFE0F)
KEYCAP_TWO = "2" + VS16 + chr(0x20E3)
KEYCAP_ONE = "1" + VS16 + chr(0x20E3)
KEYCAP_ZERO = "0" + VS16 + chr(0x20E3)
KEYCAP_TEN = chr(0x1F51F)
HEAVY_MULTIPLICATION_X = chr(0x2716) + VS16
MULTIPLICATION_SIGN = chr(0x00D7)
SHOPPING_CART = chr(0x1F6D2)
FULLWIDTH_C3 = chr(0xFF23) + chr(0xFF13)
SMALL_E_GRAVE = chr(0x00E8)
SMALL_E_ACUTE = chr(0x00E9)
SMALL_A_GRAVE = chr(0x00E0)
SMALL_O_CIRCUMFLEX = chr(0x00F4)
SMALL_I_CIRCUMFLEX = chr(0x00EE)
RIGHT_SINGLE_QUOTE = chr(0x2019)
EURO = chr(0x20AC)
HEART = chr(0x2764) + VS16
QUESTION_EMOJI = chr(0x2753)

A12, B07, C3, D4, E15, Z99 = (
    ProductCode(code) for code in ("A12", "B07", "C3", "D4", "E15", "Z99")
)

# The demo catalog, and one where C3 has no variant, to see the plan's C3
# cases go all the way to a match.
DEMO = demo_catalog()
PLAIN_C3 = catalog(catalog_product("C3", "T-shirt"), catalog_product("A12", "Robe", "S", "M", "L"))


def parse(
    text: str, products: Catalog = DEMO, max_quantity: int = DEMO_MAX_QUANTITY
) -> ParseResult:
    return parse_comment(text, products, max_quantity=max_quantity)


def match(
    code: ProductCode,
    variant: str = "",
    quantity: int = 1,
    confidence: Confidence = Confidence.EXACT,
    *evidence: str,
) -> Match:
    return Match(code, Variant(variant), Quantity(quantity), confidence, evidence)


def rejected(text: str, products: Catalog = DEMO) -> RejectionReason:
    result = parse(text, products)
    assert isinstance(result, Rejected), result
    return result.reason


def ambiguous(text: str, products: Catalog = DEMO) -> Ambiguous:
    result = parse(text, products)
    assert isinstance(result, Ambiguous), result
    return result


# The cases the plan names.


def test_sold_code_size_and_quantity() -> None:
    assert parse("SOLD A12 M x2") == match(A12, "M", 2, Confidence.EXACT, "SOLD", "A12", "M", "x2")


def test_a_typo_on_the_verb_is_tolerated() -> None:
    assert parse("slod C3", PLAIN_C3) == match(C3, "", 1, Confidence.CORRECTED, "slod", "C3")
    # In the demo catalog C3 comes in four colours, and none is named.
    assert parse("slod C3") == Ambiguous(AmbiguityReason.UNRESOLVED_VARIANT, (C3,), ("slod", "C3"))


def test_a_keycap_digit_after_x_is_the_quantity() -> None:
    text = f"C3 x {KEYCAP_TWO}"

    assert parse(text, PLAIN_C3) == match(C3, "", 2, Confidence.CODE_ALONE, "C3", f"x {KEYCAP_TWO}")
    assert parse(text) == Ambiguous(
        AmbiguityReason.UNRESOLVED_VARIANT, (C3,), ("C3", f"x {KEYCAP_TWO}")
    )


def test_a_fullwidth_code_before_the_verb() -> None:
    text = f"{FULLWIDTH_C3} je prends"

    assert parse(text, PLAIN_C3) == match(C3, "", 1, Confidence.EXACT, FULLWIDTH_C3, "prends")
    assert parse(text) == Ambiguous(
        AmbiguityReason.UNRESOLVED_VARIANT, (C3,), (FULLWIDTH_C3, "prends")
    )


def test_a_question_about_availability_is_not_an_order() -> None:
    assert parse("C3 dispo ?") == Rejected(RejectionReason.NO_INTENT)
    assert parse("C3 dispo ?", PLAIN_C3) == Rejected(RejectionReason.NO_INTENT)


def test_two_products_in_one_comment_are_rejected() -> None:
    assert parse("je prends A12 M et C3") == Rejected(RejectionReason.MULTIPLE_PRODUCTS)


# Matches.


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("je prends B07", match(B07, "", 1, Confidence.EXACT, "prends", "B07")),
        (
            f"j{RIGHT_SINGLE_QUOTE}ach{SMALL_E_GRAVE}te B07",
            match(B07, "", 1, Confidence.EXACT, f"ach{SMALL_E_GRAVE}te", "B07"),
        ),
        ("je veux Z99", match(Z99, "", 1, Confidence.EXACT, "veux", "Z99")),
        ("je voudrais Z99", match(Z99, "", 1, Confidence.EXACT, "voudrais", "Z99")),
        (
            f"je r{SMALL_E_ACUTE}serve Z99",
            match(Z99, "", 1, Confidence.EXACT, f"r{SMALL_E_ACUTE}serve", "Z99"),
        ),
        ("i'll take B07", match(B07, "", 1, Confidence.EXACT, "take", "B07")),
        ("I take B07 x2", match(B07, "", 2, Confidence.EXACT, "take", "B07", "x2")),
        ("buy Z99", match(Z99, "", 1, Confidence.EXACT, "buy", "Z99")),
        ("i'd like Z99", match(Z99, "", 1, Confidence.EXACT, "i'd like", "Z99")),
        (f"{SHOPPING_CART} B07", match(B07, "", 1, Confidence.EXACT, SHOPPING_CART, "B07")),
    ],
)
def test_french_and_english_buy_signals(text: str, expected: Match) -> None:
    assert parse(text) == expected


@pytest.mark.parametrize(
    ("text", "signals"),
    [
        ("jprends B07", ("jprends",)),
        ("jveux le B07", ("jveux",)),
        (f"jach{SMALL_E_GRAVE}te B07", (f"jach{SMALL_E_GRAVE}te",)),
        (f"mets-moi le B07 de c{SMALL_O_CIRCUMFLEX}t{SMALL_E_ACUTE}", ("mets-moi",)),
        ("mettez-moi B07 svp", ("mettez-moi",)),
        ("je reprends un B07", ("reprends",)),
        ("order B07", ("order",)),
        ("I'll have B07", ("I'll have",)),
        ("purchase B07 please", ("purchase",)),
        ("gimme B07", ("gimme",)),
        ("B07 mine!", ("mine",)),
        ("dibs on B07", ("dibs",)),
    ],
)
def test_phone_spellings_colloquial_and_english_buy_signals(
    text: str, signals: tuple[str, ...]
) -> None:
    result = parse(text)

    assert isinstance(result, Match), result
    assert result.product_code == B07
    assert result.confidence is Confidence.EXACT
    assert set(signals) <= set(result.evidence)


def test_set_aside_quotes_both_phrases() -> None:
    text = f"mets-moi le B07 de c{SMALL_O_CIRCUMFLEX}t{SMALL_E_ACUTE}"

    assert parse(text) == match(
        B07, "", 1, Confidence.EXACT, "mets-moi", "B07", f"de c{SMALL_O_CIRCUMFLEX}t{SMALL_E_ACUTE}"
    )


@pytest.mark.parametrize(
    "text", ["cancel my order B07", "I ordered B07", "I purchased B07", "do you have B07"]
)
def test_an_order_already_placed_or_a_question_is_not_a_new_order(text: str) -> None:
    assert rejected(text) is RejectionReason.NO_INTENT


@pytest.mark.parametrize(
    ("text", "verb"), [("je prend B07", "prend"), ("SODL B07", "SODL"), ("tkae B07", "tkae")]
)
def test_a_verb_typo_lowers_the_confidence(text: str, verb: str) -> None:
    assert parse(text) == match(B07, "", 1, Confidence.CORRECTED, verb, "B07")


@pytest.mark.parametrize(
    ("typed_code", "code", "variant"),
    [("BO7", B07, ""), ("bo7", B07, ""), ("El5", E15, "Unique"), ("EI5", E15, "Unique")],
)
def test_a_swapped_code_lowers_the_confidence(
    typed_code: str, code: ProductCode, variant: str
) -> None:
    assert parse(f"je prends {typed_code}") == match(
        code, variant, 1, Confidence.CORRECTED, "prends", typed_code
    )


def test_a_swapped_code_and_a_verb_typo_stay_at_the_lowest_level() -> None:
    assert parse("slod Al2 M") == match(A12, "M", 1, Confidence.CORRECTED, "slod", "Al2", "M")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("B07", match(B07, "", 1, Confidence.CODE_ALONE, "B07")),
        ("z99 svp merci", match(Z99, "", 1, Confidence.CODE_ALONE, "z99")),
        ("B07 pour moi", match(B07, "", 1, Confidence.CODE_ALONE, "B07")),
        (f"{HEART} B07 {HEART}", match(B07, "", 1, Confidence.CODE_ALONE, "B07")),
        ("@marie B07", match(B07, "", 1, Confidence.CODE_ALONE, "B07")),
        ("le sac B07", match(B07, "", 1, Confidence.CODE_ALONE, "B07")),
        ("C3 rouge x2", match(C3, "Rouge", 2, Confidence.CODE_ALONE, "C3", "rouge", "x2")),
        ("BO7", match(B07, "", 1, Confidence.CORRECTED, "BO7")),
    ],
)
def test_a_code_alone_is_an_order(text: str, expected: Match) -> None:
    assert parse(text) == expected


@pytest.mark.parametrize(
    ("text", "quantity", "quoted"),
    [
        ("je prends B07 x2", 2, "x2"),
        ("je prends B07 2x", 2, "2x"),
        (f"je prends B07 {MULTIPLICATION_SIGN}3", 3, f"{MULTIPLICATION_SIGN}3"),
        (f"je prends B07 {HEAVY_MULTIPLICATION_X}4", 4, f"{HEAVY_MULTIPLICATION_X}4"),
        ("je prends B07 5 pcs", 5, "5 pcs"),
        (f"je prends B07 quantit{SMALL_E_ACUTE} 6", 6, f"quantit{SMALL_E_ACUTE} 6"),
        ("je prends B07 qte: 7", 7, "qte: 7"),
        ("je prends B07 3 fois", 3, "3 fois"),
        (f"je prends B07 {KEYCAP_TEN}", 10, KEYCAP_TEN),
        (f"je prends B07 {KEYCAP_ONE}{KEYCAP_ZERO}", 10, f"{KEYCAP_ONE}{KEYCAP_ZERO}"),
        ("je prends B07 x02", 2, "x02"),
    ],
)
def test_quantity_forms(text: str, quantity: int, quoted: str) -> None:
    assert parse(text) == match(B07, "", quantity, Confidence.EXACT, "prends", "B07", quoted)


def test_a_quantity_padded_with_thousands_of_zeros_is_still_read() -> None:
    result = parse("je prends B07 x" + "0" * 5000 + "2")

    assert isinstance(result, Match)
    assert result.quantity == Quantity(2)


def test_a_bare_number_is_a_quantity() -> None:
    assert parse("je prends 2 B07") == match(B07, "", 2, Confidence.EXACT, "prends", "2", "B07")


def test_a_number_word_that_repeats_the_digits_agrees_with_them() -> None:
    assert parse("je prends B07 x2 (deux)") == match(
        B07, "", 2, Confidence.EXACT, "prends", "B07", "x2", "deux"
    )
    assert parse("I take 3 B07, three") == match(
        B07, "", 3, Confidence.EXACT, "take", "3", "B07", "three"
    )
    assert ambiguous("je prends B07 x3 (deux)").reason is AmbiguityReason.CONFLICTING_SIGNALS


@pytest.mark.parametrize(
    "text",
    [
        "je prends B07 pas de souci",
        "je prends B07 pas de probleme",
        "B07 pas mal je prends",
        "I'll take B07 no worries",
    ],
)
def test_an_idiom_with_a_negation_is_no_doubt(text: str) -> None:
    result = parse(text)

    assert isinstance(result, Match), result
    assert result.product_code == B07


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("je prends B07 x1O", "x1O"),
        ("je prends B07 xl0", "xl0"),
        ("je prends B07 1o", "1o"),
        ("B07 x 1O", "1O"),
        ("je prends B07 svp 1O", "1O"),
    ],
)
def test_a_number_typed_with_look_alike_letters_is_left_to_the_fallback(
    text: str, number: str
) -> None:
    # Not a second product: the fallback can read "1O" as 10.
    assert ambiguous(text) == Ambiguous(
        AmbiguityReason.CONFLICTING_SIGNALS,
        (B07,),
        tuple(quote for quote in ("prends", "B07", number) if quote in text),
    )


def test_a_look_alike_number_that_repeats_the_digits_agrees_with_them() -> None:
    assert parse("je prends B07 x10 (1O)") == match(
        B07, "", 10, Confidence.EXACT, "prends", "B07", "x10", "1O"
    )


def test_the_same_quantity_written_twice_is_one_quantity() -> None:
    result = parse("je prends 2 B07 x2")

    assert isinstance(result, Match)
    assert result.quantity == Quantity(2)


@pytest.mark.parametrize(
    "text",
    [
        f"je prends B07 15{EURO}",
        "je prends B07 15 euros",
        "je prends B07 a 15 balles",
        "je prends B07 -50%",
        f"je prends B07 {SMALL_A_GRAVE} 9,90{EURO}",
        f"je prends B07 {SMALL_A_GRAVE} 15.90 {EURO}",
        "je prends B07 a 29.99",
    ],
)
def test_a_price_or_a_discount_is_not_a_quantity(text: str) -> None:
    assert parse(text) == match(B07, "", 1, Confidence.EXACT, "prends", "B07")


@pytest.mark.parametrize(
    "text",
    [
        "je prends B07 10h",
        f"je prends B07 demain {SMALL_A_GRAVE} 9h",
        "je prends B07 avant 20h",
        "je prends B07 je paie a 10h30",
        "je prends B07 vers 9 h",
        "je prends B07 dans 5 min",
        "je prends B07 pour la 3e fois",
        f"je prends B07 pour la 2{SMALL_E_GRAVE}me fois",
        "je prends B07 le 1er",
    ],
)
def test_a_time_or_an_ordinal_is_not_a_quantity(text: str) -> None:
    assert parse(text) == match(B07, "", 1, Confidence.EXACT, "prends", "B07")


def test_a_price_beside_a_quantity_leaves_the_quantity() -> None:
    assert parse(f"je prends B07 x2 {SMALL_A_GRAVE} 12,50{EURO}") == match(
        B07, "", 2, Confidence.EXACT, "prends", "B07", "x2"
    )
    assert parse("je prends 2 B07 pour 30 euros") == match(
        B07, "", 2, Confidence.EXACT, "prends", "2", "B07"
    )


@pytest.mark.parametrize(
    ("text", "variant", "quoted"),
    [
        ("je prends A12 M", "M", "M"),
        ("je prends A12 taille m", "M", "m"),
        ("je prends A12M", "M", "M"),
        ("je prends l'A12 en L", "L", "L"),
        ("je prends A12 medium", "M", "medium"),
        ("je prends C3 rouge", "Rouge", "rouge"),
        ("je prends C3 ROUGE", "Rouge", "ROUGE"),
        ("je prends C3 noire", "Noir", "noire"),
        ("je prends C3 blue", "Bleu", "blue"),
        ("je prends C3 bleu nuit", "Bleu nuit", "bleu nuit"),
        ("je prends C3 bleu-nuit", "Bleu nuit", "bleu-nuit"),
        ("je prends C3 bleu", "Bleu", "bleu"),
        ("je prends D4 38", "38", "38"),
        ("je prends D4 T40", "40", "T40"),
    ],
)
def test_variants_are_read_against_the_product_labels(text: str, variant: str, quoted: str) -> None:
    result = parse(text)

    assert isinstance(result, Match), result
    assert result.variant == Variant(variant)
    assert quoted in result.evidence


def test_a_numeric_size_and_a_quantity_side_by_side() -> None:
    assert parse("je prends 2 D4 38") == match(
        D4, "38", 2, Confidence.EXACT, "prends", "2", "D4", "38"
    )
    assert parse("je prends D4 38 x2") == match(
        D4, "38", 2, Confidence.EXACT, "prends", "D4", "38", "x2"
    )


def test_the_number_after_en_is_a_size_unless_a_quantity_word_follows() -> None:
    assert parse("je prends D4 en 38") == match(D4, "38", 1, Confidence.EXACT, "prends", "D4", "38")
    assert parse("je prends D4 en 44") == Rejected(RejectionReason.VARIANT_NOT_FOUND)
    # Named, not coded: the size still does not pass for a quantity of 38.
    assert ambiguous("je prends le jean en 38") == Ambiguous(
        AmbiguityReason.PRODUCT_BY_NAME, (D4,), ("prends", "jean", "38")
    )
    result = parse("je prends le C3 noir en 2 exemplaires")
    assert isinstance(result, Match)
    assert result.quantity == Quantity(2)


def test_after_en_a_product_without_sizes_can_only_take_a_quantity() -> None:
    assert parse("je prends Z99 en 2") == match(Z99, "", 2, Confidence.EXACT, "prends", "Z99", "2")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The size is already named: the number after "en" can only be a quantity.
        ("je prends A12 M en 2", match(A12, "M", 2, Confidence.EXACT, "prends", "A12", "M", "2")),
        (
            "je prends A12 M en 2 svp",
            match(A12, "M", 2, Confidence.EXACT, "prends", "A12", "M", "2"),
        ),
        (
            "je prends A12 taille M en 2",
            match(A12, "M", 2, Confidence.EXACT, "prends", "A12", "M", "2"),
        ),
        (
            "je prends C3 rouge en 2",
            match(C3, "Rouge", 2, Confidence.EXACT, "prends", "C3", "rouge", "2"),
        ),
        ("je prends D4 38 en 2", match(D4, "38", 2, Confidence.EXACT, "prends", "D4", "38", "2")),
        # Colours, or a single variant: no size can be a number.
        (
            "je prends E15 en 2",
            match(E15, "Unique", 2, Confidence.EXACT, "prends", "E15", "2"),
        ),
        (
            "je prends C3 en 2",
            Ambiguous(AmbiguityReason.UNRESOLVED_VARIANT, (C3,), ("prends", "C3", "2")),
        ),
        (
            "je prends A12 en 3 svp",
            Ambiguous(AmbiguityReason.UNRESOLVED_VARIANT, (A12,), ("prends", "A12", "3")),
        ),
        # Numbered sizes and none named yet: the number is a size.
        ("je prends D4 en 2", Rejected(RejectionReason.VARIANT_NOT_FOUND)),
    ],
)
def test_after_en_a_number_is_a_size_only_while_a_numbered_size_is_open(
    text: str, expected: ParseResult
) -> None:
    assert parse(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "je prends A12 s il vous plait",
            Ambiguous(AmbiguityReason.UNRESOLVED_VARIANT, (A12,), ("prends", "A12")),
        ),
        (
            f"A12 s il vous pla{SMALL_I_CIRCUMFLEX}t",
            Ambiguous(AmbiguityReason.UNRESOLVED_VARIANT, (A12,), ("A12",)),
        ),
        (
            "je prends A12 M s il vous plait",
            match(A12, "M", 1, Confidence.EXACT, "prends", "A12", "M"),
        ),
        (
            "je prends A12 M pour l anniversaire de ma fille",
            match(A12, "M", 1, Confidence.EXACT, "prends", "A12", "M"),
        ),
        (
            "je prends A12 M, je l offre a ma soeur",
            match(A12, "M", 1, Confidence.EXACT, "prends", "A12", "M"),
        ),
        # The apostrophe typed: the size next to "il" stays a size.
        (
            "je prends A12 en S s'il vous plait",
            match(A12, "S", 1, Confidence.EXACT, "prends", "A12", "S"),
        ),
        # A size, then a word that happens to start with a vowel.
        (
            "je prends A12 L avec la ceinture",
            match(A12, "L", 1, Confidence.EXACT, "prends", "A12", "L"),
        ),
    ],
)
def test_an_elided_letter_typed_without_its_apostrophe_is_not_a_size(
    text: str, expected: ParseResult
) -> None:
    assert parse(text) == expected


def test_a_product_with_a_single_variant_takes_it() -> None:
    assert parse("je prends E15 x3") == match(
        E15, "Unique", 3, Confidence.EXACT, "prends", "E15", "x3"
    )


def test_a_product_without_variants_ignores_size_and_colour_words() -> None:
    assert parse("je prends B07 rouge taille M") == match(
        B07, "", 1, Confidence.EXACT, "prends", "B07"
    )


def test_the_same_variant_named_twice_is_one_variant() -> None:
    result = parse("je prends A12 M taille M")

    assert isinstance(result, Match)
    assert result.variant == Variant("M")


def test_a_named_variant_wins_over_an_unknown_one() -> None:
    result = parse("je prends A12 M rouge")

    assert isinstance(result, Match)
    assert result.variant == Variant("M")


def test_the_match_carries_no_variant_for_a_product_without_any() -> None:
    result = parse("je prends Z99")

    assert isinstance(result, Match)
    assert result.variant == NO_VARIANT


def test_the_confidence_scale_is_closed_and_ordered() -> None:
    assert [level.value for level in Confidence] == [
        Decimal("1.00"),
        Decimal("0.90"),
        Decimal("0.80"),
    ]


# Rejections.


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "bonjour",
        "trop beau",
        "prix ?",
        f"{HEART}{HEART}{HEART}",
        "trop beau le C3",
        "B07 pour ma fille",
        "annule B07",
        "B07 sold out",
        "Z99 dispo en rouge ?",
        f"C3 dispo {QUESTION_EMOJI}",
        "je prends",
        f"je prends {SHOPPING_CART}",
        "sold",
        "la robe fleurie est trop belle",
        "wasnt B07",
        "it wasn't B07 i wanted",
        "B07 hasn't arrived",
    ],
)
def test_no_intent(text: str) -> None:
    assert rejected(text) is RejectionReason.NO_INTENT


@pytest.mark.parametrize(
    "text",
    ["je prends A12 M et C3", "A12 C3", "je prends B07 ou Z99 ?", "je prends Z99 et Q7", "Q7 Q8"],
)
def test_multiple_products(text: str) -> None:
    assert rejected(text) is RejectionReason.MULTIPLE_PRODUCTS


def test_the_same_code_twice_is_one_product() -> None:
    assert parse("B07 B07") == match(B07, "", 1, Confidence.CODE_ALONE, "B07", "B07")
    assert isinstance(parse("je prends Al2 M, oui A12"), Match)


@pytest.mark.parametrize(
    "text",
    [
        "je prends B07 x11",
        "je prends B07 x0",
        "je prends 50 pcs B07",
        "je prends B07 qte 12",
        "je prends 0 B07",
        "je prends B07 x 1000000000000000000000000",
        "je prends B07 x11 x12",
        "je prends B07 x11 12",
        # A negative quantity is below 1, not its opposite.
        "je prends A12 M x-1",
        "je prends B07 x -2",
        "je prends B07 qte-3",
        "je prends B07 -2",
    ],
)
def test_quantity_out_of_bounds(text: str) -> None:
    assert rejected(text) is RejectionReason.QUANTITY_OUT_OF_BOUNDS


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("je prends 50 B07", "50"),
        ("je prends B07 le 12", "12"),
        ("B07 29", "29"),
        # No size for a bag: a size typed by mistake, or a quantity of 38?
        ("je prends B07 en 38", "38"),
        # A size that A12 does not have, or a quantity of 38?
        ("je prends A12 en 38", "38"),
        ("je prends D4 44", "44"),
    ],
)
def test_a_bare_number_above_the_bound_is_left_to_the_fallback(text: str, number: str) -> None:
    result = ambiguous(text)

    assert result.reason is AmbiguityReason.CONFLICTING_SIGNALS
    assert number in result.evidence


def test_the_quantity_bound_is_an_argument() -> None:
    assert parse("je prends B07 x2", max_quantity=1) == Rejected(
        RejectionReason.QUANTITY_OUT_OF_BOUNDS
    )
    result = parse("je prends B07 x99", max_quantity=99)
    assert isinstance(result, Match)
    assert result.quantity == Quantity(99)


@pytest.mark.parametrize("max_quantity", [0, -1, 100])
def test_a_bound_outside_the_ceiling_is_a_caller_error(max_quantity: int) -> None:
    with pytest.raises(ValueError, match="max_quantity must be from 1 to 99"):
        parse_comment("je prends B07", DEMO, max_quantity=max_quantity)


@pytest.mark.parametrize(
    "text",
    [
        "je prends A12 XL",
        "A12 taille XL",
        "je prends A12 taille 2",
        "D4 T44",
        "je prends E15 rouge",
    ],
)
def test_variant_not_found(text: str) -> None:
    assert rejected(text) is RejectionReason.VARIANT_NOT_FOUND


# Ambiguities.


def test_product_by_name() -> None:
    assert ambiguous("je prends la robe") == Ambiguous(
        AmbiguityReason.PRODUCT_BY_NAME, (A12,), ("prends", "robe")
    )
    assert ambiguous("je prends le sac en cuir").candidates == (B07,)
    assert ambiguous("je prends le T-shirt").candidates == (C3,)
    assert ambiguous("je prends des robes").candidates == (A12,)


def test_product_by_name_lists_every_product_it_could_be() -> None:
    two_bags = catalog(catalog_product("B1", "Sac rouge"), catalog_product("B2", "Sac noir"))

    assert ambiguous("je prends le sac", two_bags).candidates == (
        ProductCode("B1"),
        ProductCode("B2"),
    )
    assert ambiguous("je prends le sac rouge", two_bags).candidates == (
        ProductCode("B1"),
        ProductCode("B2"),
    )


@pytest.mark.parametrize("text", ["je prends A13", "Q7", "je prends B7", "A1O"])
def test_unknown_code(text: str) -> None:
    result = ambiguous(text)

    assert result.reason is AmbiguityReason.UNKNOWN_CODE
    assert result.candidates == ()


def test_an_unknown_code_lists_the_catalog_codes_it_could_be_read_as() -> None:
    lookalikes = catalog(catalog_product("A10", "Un"), catalog_product("AI0", "Deux"))

    assert ambiguous("je prends A1O", lookalikes) == Ambiguous(
        AmbiguityReason.UNKNOWN_CODE, (ProductCode("A10"), ProductCode("AI0")), ("prends", "A1O")
    )


@pytest.mark.parametrize(
    "text",
    [
        "je prends B07 ?",
        "je veux pas B07",
        "je n'en veux pas B07",
        "don't take B07",
        "je prends B07 si dispo",
        "je prends B07 x2 x3",
        "je prends 2 B07 x3",
        "je prends deux B07",
        "je prends C3 rouge ou noir",
        "je prends A12 M et L",
        "je prends D4 38 40",
    ],
)
def test_conflicting_signals(text: str) -> None:
    result = ambiguous(text)

    assert result.reason is AmbiguityReason.CONFLICTING_SIGNALS
    assert len(result.candidates) == 1


@pytest.mark.parametrize(
    "text",
    [
        f"j'ai d{SMALL_E_ACUTE}j{SMALL_A_GRAVE} command{SMALL_E_ACUTE} le B07",
        f"j'ai achet{SMALL_E_ACUTE} B07 la semaine derni{SMALL_E_GRAVE}re",
        f"j{RIGHT_SINGLE_QUOTE}ai command{SMALL_E_ACUTE} B07",
        f"B07 d{SMALL_E_ACUTE}j{SMALL_A_GRAVE} r{SMALL_E_ACUTE}serv{SMALL_E_ACUTE}",
        "I already take B07",
        "je prends B07 comme hier",
    ],
)
def test_a_purchase_already_made_is_not_a_new_order(text: str) -> None:
    # Perhaps a confirmation, perhaps a statement: the fallback reads it.
    assert ambiguous(text).reason is AmbiguityReason.CONFLICTING_SIGNALS


@pytest.mark.parametrize(
    "text",
    [
        "B07 reserved for @marie",
        "achetez B07 vite !",
        "B07 is sold",
        "B07 was sold",
        "annulez ma commande B07",
        f"ma commande B07 est arriv{SMALL_E_ACUTE}e",
    ],
)
def test_statements_about_a_product_or_an_order_are_not_orders(text: str) -> None:
    assert rejected(text) is RejectionReason.NO_INTENT


def test_commande_after_a_possessive_is_the_noun_but_the_verb_elsewhere() -> None:
    assert parse("je commande B07") == match(B07, "", 1, Confidence.EXACT, "commande", "B07")
    assert parse("ma commande: je commande B07") == match(
        B07, "", 1, Confidence.EXACT, "commande", "B07"
    )


def test_conflicting_signals_quote_both_sides() -> None:
    assert ambiguous("je veux pas B07") == Ambiguous(
        AmbiguityReason.CONFLICTING_SIGNALS, (B07,), ("veux", "pas", "B07")
    )
    assert ambiguous("je prends B07 x2 x3").evidence == ("prends", "B07", "x2", "x3")


@pytest.mark.parametrize("text", ["je prends A12", "A12", "C3 x2", "je prends D4 2"])
def test_unresolved_variant(text: str) -> None:
    result = ambiguous(text)

    assert result.reason is AmbiguityReason.UNRESOLVED_VARIANT
    assert len(result.candidates) == 1


# The decision order, where two rules could apply.


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A question is not an order, even about two products.
        ("A12 ou C3 ?", Rejected(RejectionReason.NO_INTENT)),
        # Two products, even with a doubt.
        ("je prends A12 et C3 ?", Rejected(RejectionReason.MULTIPLE_PRODUCTS)),
        # Out of bounds whatever the unknown code, the name or the doubt resolves to.
        ("je prends Q7 x50", Rejected(RejectionReason.QUANTITY_OUT_OF_BOUNDS)),
        ("je prends la robe x50", Rejected(RejectionReason.QUANTITY_OUT_OF_BOUNDS)),
        ("je prends pas B07 x50", Rejected(RejectionReason.QUANTITY_OUT_OF_BOUNDS)),
        # A size the product does not have, whatever the doubt.
        ("je prends pas A12 XXL", Rejected(RejectionReason.VARIANT_NOT_FOUND)),
    ],
)
def test_final_rejections_come_before_ambiguities(text: str, expected: Rejected) -> None:
    assert parse(text) == expected


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("je prends Q7 ?", AmbiguityReason.UNKNOWN_CODE),
        ("je prends la robe ?", AmbiguityReason.PRODUCT_BY_NAME),
        ("je prends A12 ?", AmbiguityReason.CONFLICTING_SIGNALS),
        ("je prends deux A12", AmbiguityReason.CONFLICTING_SIGNALS),
    ],
)
def test_which_product_comes_before_whether_and_which_variant(
    text: str, reason: AmbiguityReason
) -> None:
    assert ambiguous(text).reason is reason


def test_names_are_only_read_when_no_code_is_given() -> None:
    assert parse("je prends la robe B07") == match(B07, "", 1, Confidence.EXACT, "prends", "B07")


def test_the_evidence_quotes_the_comment_as_typed() -> None:
    text = f"Je PRENDS {FULLWIDTH_C3} Rouge x {KEYCAP_TWO} !!"
    result = parse(text)

    assert result == match(
        C3, "Rouge", 2, Confidence.EXACT, "PRENDS", FULLWIDTH_C3, "Rouge", f"x {KEYCAP_TWO}"
    )
