"""Property-style tests of the parser, on seeded pseudo-random input.

No property-testing library: a seeded ``random.Random`` gives the same inputs
on every run, so a failure is reproducible from the seed and the index.
"""

import random
import string

import pytest

from livecart_core.application.dto import MAX_COMMENT_LENGTH
from livecart_core.domain.parser import Ambiguous, Match, ParseResult, parse_comment
from livecart_core.testing.builders import DEMO_MAX_QUANTITY, demo_catalog

SEED = 20260926
DEMO = demo_catalog()
# Every code point but the surrogates, which a str may hold but UTF-8 may not.
UNICODE_RANGES = ((0x0000, 0xD7FF), (0xE000, 0x10FFFF))
UNICODE_SIZE = sum(end - start + 1 for start, end in UNICODE_RANGES)

# The pieces of real comments, glued at random: verbs and typos, codes and
# swaps, variants, quantities, doubts, noise and the characters that
# normalisation rewrites (built from code points, as elsewhere).
VS16 = chr(0xFE0F)
PIECES = (
    "je prends",
    "sold",
    "slod",
    "i'll take",
    "ach" + chr(0x00E8) + "te",
    chr(0x1F6D2),
    "A12",
    "Al2",
    "a12m",
    "B07",
    "BO7",
    "C3",
    chr(0xFF23) + chr(0xFF13),
    "D4",
    "E15",
    "Z99",
    "Q7",
    "A1O",
    "2xB07",
    "M",
    "L",
    "XL",
    "rouge",
    "bleu nuit",
    "blue",
    "38",
    "T40",
    "taille",
    "x2",
    "2" + VS16 + chr(0x20E3),
    chr(0x2716) + VS16 + "3",
    "2 pcs",
    "x11",
    "0",
    "?",
    chr(0x2753),
    "pas",
    "n'",
    "dispo",
    "deux",
    "svp",
    "merci",
    "trop",
    "beau",
    "la robe",
    "15" + chr(0x20AC),
    "-",
    "'",
    "@marie",
    "et",
    "ou",
    chr(0x2764) + VS16,
    "",
)


def random_unicode(rng: random.Random, length: int) -> str:
    chars = []
    for _ in range(length):
        index = rng.randrange(UNICODE_SIZE)
        for start, end in UNICODE_RANGES:
            if index <= end - start:
                chars.append(chr(start + index))
                break
            index -= end - start + 1
    return "".join(chars)


def parse(text: str) -> ParseResult:
    return parse_comment(text, DEMO, max_quantity=DEMO_MAX_QUANTITY)


def test_random_unicode_never_raises_and_never_matches() -> None:
    rng = random.Random(SEED)  # noqa: S311 - reproducible test data, not a secret

    for index in range(1000):
        text = random_unicode(rng, rng.randrange(0, 200))
        result = parse(text)
        assert not isinstance(result, Match), (index, text, result)


@pytest.mark.parametrize("round_number", range(20))
def test_1000_random_characters_never_raise_and_never_match(round_number: int) -> None:
    # The plan's "1 000 caracteres aleatoires": a comment of the largest size
    # the webhook accepts, drawn from every code point.
    rng = random.Random(SEED + 1000 + round_number)  # noqa: S311 - reproducible test data
    text = random_unicode(rng, MAX_COMMENT_LENGTH)

    assert len(text) == MAX_COMMENT_LENGTH
    assert not isinstance(parse(text), Match), text


@pytest.mark.parametrize("round_number", range(20))
def test_1000_random_ascii_characters_keep_every_invariant(round_number: int) -> None:
    # Letters and digits come far more often than in the whole of Unicode, so
    # codes, verbs and numbers do appear: whatever the result, it must hold.
    rng = random.Random(SEED + 2000 + round_number)  # noqa: S311 - reproducible test data
    text = "".join(rng.choice(string.printable) for _ in range(MAX_COMMENT_LENGTH))

    check_invariants(text, parse(text))


def test_lone_surrogates_and_control_characters_never_raise() -> None:
    text = "".join(chr(code) for code in (0xD800, 0xDFFF, 0x0000, 0x001B, 0x007F, 0xFFFF))

    assert not isinstance(parse(f"je prends {text}"), Match)
    assert isinstance(parse(f"je prends B07 {text}"), Match)


def check_invariants(text: str, result: ParseResult) -> None:
    if isinstance(result, Match):
        product = DEMO.get(result.product_code)
        assert product is not None, text
        assert 1 <= result.quantity.value <= DEMO_MAX_QUANTITY, text
        if len(product.variants) == 0:
            assert result.variant.is_empty, text
        else:
            assert result.variant in product.variants, text
        assert result.evidence, text
    if isinstance(result, Match | Ambiguous):
        # Evidence quotes the comment: every piece is a substring, in order.
        position = 0
        for quote in result.evidence:
            found = text.find(quote, position)
            assert found >= 0, (text, quote)
            position = found + len(quote)
    if isinstance(result, Ambiguous):
        assert all(code in DEMO for code in result.candidates), text


@pytest.mark.parametrize("round_number", range(4))
def test_comments_built_from_real_pieces_keep_every_invariant(round_number: int) -> None:
    rng = random.Random(SEED + round_number)  # noqa: S311 - reproducible test data, not a secret
    kinds: set[str] = set()

    for _ in range(500):
        text = " ".join(rng.choice(PIECES) for _ in range(rng.randrange(0, 8)))
        result = parse(text)
        check_invariants(text, result)
        # Pure: the same comment gives the same result.
        assert parse(text) == result
        kinds.add(type(result).__name__)

    # The pieces are rich enough to reach every kind of result.
    assert kinds == {"Match", "Ambiguous", "Rejected"}


def test_a_comment_of_the_largest_accepted_size_is_parsed() -> None:
    text = ("je prends A12 M x2 " * 52).ljust(1000)
    assert len(text) == 1000

    # The same product and quantity, again and again: one product, one quantity.
    assert isinstance(parse(text), Match)
