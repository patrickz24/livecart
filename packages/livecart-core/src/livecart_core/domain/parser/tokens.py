"""Tokens of a folded comment: words, numbers, glued quantities, product codes and marks.

The folded text splits into words (runs of letters and digits) and one-character
marks. A word of ASCII letters and digits is then read against the catalog,
because a buyer glues what belongs together ("A12M", "C3x2", "2xC3"):

1. the whole word, if it is a catalog code as typed (``a12``);
2. the whole word, if it is a glued quantity (``x2``, ``qte3``): a catalog
   code wins over a quantity, so a catalog with an ``X2`` product reads
   ``x2`` as that product;
3. the whole word, if exactly one catalog code matches it once O and 0, or
   I, L and 1, are swapped: ``Al2`` reads ``A12``, ``BO7`` reads ``B07``;
4. otherwise the word splits where a digit is followed by a letter (``a12m``
   gives ``a12`` and ``m``, ``c3x2`` gives ``c3`` and ``x2``), and each piece is
   read the same way; a piece ``x`` + code (from ``2xC3``) splits once more,
   and so does a piece of letters + glued quantity (``lx2`` from ``A12Lx2``);
5. if no piece is a catalog code but the whole word has the shape of a code,
   even through a swap (``A13``, ``A1O``), the word is one unknown code,
   carrying the catalog codes it could be read as when there are several.

A numbered size (``2xl``), a time (``14h``, ``10h30``) or an ordinal (``3e``,
``2eme``) stays one word, so that its number never passes for a quantity.

A word with a non-ASCII letter (another script) never holds a code: it splits
into its ASCII and non-ASCII runs, and the non-ASCII runs are plain words.
"""

import re
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from itertools import product
from typing import Final

from livecart_core.domain.catalog import Catalog
from livecart_core.domain.codes import PRODUCT_CODE_PATTERN, ProductCode
from livecart_core.domain.parser import lexicon
from livecart_core.domain.parser.normalization import (
    AMOUNT_UNIT,
    CART,
    DECIMAL,
    NEGATION,
    QUESTION,
    NormalizedText,
)


class MarkKind(Enum):
    QUESTION = QUESTION
    CART = CART
    NEGATION = NEGATION
    AMOUNT_UNIT = AMOUNT_UNIT
    DECIMAL = DECIMAL


@dataclass(frozen=True, slots=True)
class TokenBase:
    # The folded text, and the span of the original comment it comes from.
    text: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Word(TokenBase):
    pass


@dataclass(frozen=True, slots=True)
class Number(TokenBase):
    value: int


@dataclass(frozen=True, slots=True)
class GluedQuantity(TokenBase):
    value: int


@dataclass(frozen=True, slots=True)
class Code(TokenBase):
    code: ProductCode
    corrected: bool


@dataclass(frozen=True, slots=True)
class UnknownCode(TokenBase):
    candidates: tuple[ProductCode, ...]


@dataclass(frozen=True, slots=True)
class Mark(TokenBase):
    kind: MarkKind


type Token = Word | Number | GluedQuantity | Code | UnknownCode | Mark

_TOKEN: Final = re.compile(rf"[^\W_]+|[{re.escape(''.join(kind.value for kind in MarkKind))}]")
_SCRIPT_RUN: Final = re.compile(r"[a-z0-9]+|[^a-z0-9]+")
_PIECE: Final = re.compile(r"[0-9]+|[a-z]+[0-9]*")
_GLUED_QUANTITY: Final = re.compile(r"(?:x|qte|qty|qt|nb)([0-9]+)")
# "lx2" in "A12Lx2", "rougex2" in "C3rougex2": a size or colour, then "x2".
_WORD_THEN_QUANTITY: Final = re.compile(r"([a-z]+?)(x[0-9]+)")
# "2xl", "3xs": a size, not the quantity 2 of "xl".
_NUMBERED_SIZE: Final = re.compile(r"[2-6]x[sl]")

# Swaps between look-alike letters and digits. In the letter part of a code a
# digit may stand for a letter, and in the digit part a letter for a digit.
_LETTERS_FOR_DIGIT: Final[dict[str, tuple[str, ...]]] = {"0": ("o",), "1": ("i", "l")}
_DIGIT_FOR_LETTER: Final[dict[str, str]] = {"o": "0", "i": "1", "l": "1"}
_LETTER_PART_CHARS: Final = frozenset("abcdefghijklmnopqrstuvwxyz01")
_DIGIT_PART_CHARS: Final = frozenset("0123456789oil")
_MAX_LETTERS: Final = 3
_MAX_DIGITS: Final = 4
# A longer run of digits is out of every bound; reading it as this value
# avoids converting a string of any length to an int.
_MAX_NUMBER_DIGITS: Final = 6
TOO_LARGE: Final = 10**_MAX_NUMBER_DIGITS


def number_value(digits: str) -> int:
    # Leading zeros count towards int()'s limit on digits too, so they go first.
    significant = digits.lstrip("0")
    return int(significant or "0") if len(significant) <= _MAX_NUMBER_DIGITS else TOO_LARGE


@lru_cache(maxsize=4096)
def code_readings(piece: str) -> frozenset[str]:
    """Every product code ``piece`` may be read as, through the look-alike swaps.

    A piece without an ASCII letter or without a digit has no reading: "lol"
    and "12" are not codes, whatever the swaps.
    """
    if not (any(c.isdigit() for c in piece) and any(c.isalpha() for c in piece)):
        return frozenset()
    readings: set[str] = set()
    for split in range(1, min(_MAX_LETTERS, len(piece) - 1) + 1):
        letters, digits = piece[:split], piece[split:]
        if len(digits) > _MAX_DIGITS:
            continue
        if not (set(letters) <= _LETTER_PART_CHARS and set(digits) <= _DIGIT_PART_CHARS):
            continue
        digit_part = "".join(_DIGIT_FOR_LETTER.get(c, c) for c in digits)
        choices = [_LETTERS_FOR_DIGIT.get(c, (c,)) for c in letters]
        for letter_part in product(*choices):
            readings.add(("".join(letter_part) + digit_part).upper())
    return frozenset(readings)


def _exact_code(piece: str, catalog: Catalog) -> ProductCode | None:
    upper = piece.upper()
    if PRODUCT_CODE_PATTERN.fullmatch(upper) and ProductCode(upper) in catalog:
        return ProductCode(upper)
    return None


def _catalog_readings(piece: str, catalog: Catalog) -> tuple[ProductCode, ...]:
    return tuple(
        sorted(
            (code for reading in code_readings(piece) if (code := ProductCode(reading)) in catalog),
            key=str,
        )
    )


_MARKS: Final = {kind.value: kind for kind in MarkKind}


class _Reader:
    """Turns the words of one folded comment into tokens."""

    def __init__(self, folded: NormalizedText, catalog: Catalog) -> None:
        self._folded = folded
        self._catalog = catalog

    def tokens(self) -> list[Token]:
        tokens: list[Token] = []
        for match in _TOKEN.finditer(self._folded.text):
            text = match.group()
            if len(text) == 1 and text in _MARKS:
                start, end = self._folded.source_span(match.start(), match.end())
                tokens.append(Mark(text, start, end, _MARKS[text]))
                continue
            for run in _SCRIPT_RUN.finditer(text):
                offset = match.start() + run.start()
                if run.group().isascii():
                    tokens.extend(self._ascii_word(run.group(), offset))
                else:
                    tokens.append(self._word(run.group(), offset))
        return tokens

    def _span(self, offset: int, length: int) -> tuple[int, int]:
        return self._folded.source_span(offset, offset + length)

    def _word(self, text: str, offset: int) -> Word:
        return Word(text, *self._span(offset, len(text)))

    def _code(self, text: str, offset: int, code: ProductCode, *, corrected: bool) -> Code:
        return Code(text, *self._span(offset, len(text)), code=code, corrected=corrected)

    def _resolved(self, piece: str, offset: int) -> Code | None:
        """The catalog code of ``piece``, as typed or through exactly one swap reading."""
        if (code := _exact_code(piece, self._catalog)) is not None:
            return self._code(piece, offset, code, corrected=False)
        readings = _catalog_readings(piece, self._catalog)
        if len(readings) == 1:
            return self._code(piece, offset, readings[0], corrected=True)
        return None

    def _glued_quantity(self, piece: str, offset: int) -> GluedQuantity | None:
        if (match := _GLUED_QUANTITY.fullmatch(piece)) is None:
            return None
        return GluedQuantity(piece, *self._span(offset, len(piece)), number_value(match.group(1)))

    def _unknown_code(self, piece: str, offset: int) -> UnknownCode | None:
        if not code_readings(piece):
            return None
        readings = _catalog_readings(piece, self._catalog)
        # A single reading would have resolved the piece; several are candidates.
        return UnknownCode(piece, *self._span(offset, len(piece)), readings)

    def _piece(self, piece: str, offset: int) -> list[Token]:
        if (code := _exact_code(piece, self._catalog)) is not None:
            return [self._code(piece, offset, code, corrected=False)]
        if (quantity := self._glued_quantity(piece, offset)) is not None:
            return [quantity]
        if (resolved := self._resolved(piece, offset)) is not None:
            return [resolved]
        if (
            len(piece) > 1
            and piece.startswith("x")
            and (rest := self._resolved(piece[1:], offset + 1)) is not None
        ):
            return [self._word("x", offset), rest]
        if piece.isdigit():
            return [Number(piece, *self._span(offset, len(piece)), number_value(piece))]
        if (glued := _WORD_THEN_QUANTITY.fullmatch(piece)) is not None:
            head = glued.group(1)
            return [self._word(head, offset), *self._piece(glued.group(2), offset + len(head))]
        if (unknown := self._unknown_code(piece, offset)) is not None:
            return [unknown]
        return [self._word(piece, offset)]

    def _ascii_word(self, word: str, offset: int) -> list[Token]:
        if _NUMBERED_SIZE.fullmatch(word) or lexicon.time_or_ordinal(word):
            return [self._word(word, offset)]
        pieces = list(_PIECE.finditer(word))
        if len(pieces) == 1:
            return self._piece(word, offset)
        # A glued quantity is a single piece, so only a code can take the whole word here.
        if (resolved := self._resolved(word, offset)) is not None:
            return [resolved]
        split = [
            token
            for piece in pieces
            for token in self._piece(piece.group(), offset + piece.start())
        ]
        if not any(isinstance(token, Code) for token in split) and (
            unknown := self._unknown_code(word, offset)
        ):
            return [unknown]
        return split


def tokenize(folded: NormalizedText, catalog: Catalog) -> list[Token]:
    """The tokens of a folded comment, in text order; see the module docstring."""
    return _Reader(folded, catalog).tokens()
