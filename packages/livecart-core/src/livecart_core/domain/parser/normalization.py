"""Comment text folded for matching, with a map back to the original characters.

Matching works on a folded copy of the comment, while the evidence of a
result quotes the comment as the buyer typed it. Folding therefore runs one
character at a time and records, for every folded character, the span of the
original characters it comes from. Folding one character alone gives the same
result as folding the whole string here, because the accents that NFKC would
compose with their base letter are dropped anyway.

Per character, in this order:

1. a few explicit readings: the multiplication signs (U+00D7, U+2715, U+2716,
   U+2A2F) read as the letter ``x`` of ``x2``; the shopping cart emoji
   (U+1F6D2) becomes the ``CART`` mark, a buy signal; the keycap ten
   (U+1F51F) reads ``10``; the question emojis and the inverted question mark
   read ``?``; the typographic apostrophes read ``'``;
2. NFKC (fullwidth ``C3``, superscript and circled digits, ligatures), case
   folding, then canonical decomposition;
3. zero-width characters disappear: combining marks (accents, and the
   U+20E3 enclosing keycap of ``2`` U+FE0F U+20E3, which leaves the digit),
   variation selectors, joiners and other format characters;
4. letters and digits stay; ``?``, ``'``, ``@`` and dashes stay for the rules
   below; a currency symbol or ``%`` becomes the ``AMOUNT_UNIT`` mark, since
   the number next to it is a price or a discount, never a quantity; a comma
   or a full stop between two digits becomes the ``DECIMAL`` mark, as in
   ``15,90`` (a quantity is never a decimal); every other character
   (punctuation, emojis, spaces, controls) becomes a space.

Then, on the folded string: an ``@mention`` is dropped, as it names another
viewer; a French elision (``l'``, ``j'``, ``qu'``...) is dropped, except
``n'``, which becomes the ``NEGATION`` mark; an apostrophe inside a word is
dropped (``i'll`` reads ``ill``, ``don't`` reads ``dont``); a dash right
before a digit, at the start of a word or after a glued quantity prefix
(``-2``, ``x-1``), becomes the ``MINUS`` mark, so that a negative quantity is
out of bounds rather than read as its opposite; a hyphen between one to three
other letters and a digit is dropped (``A-12`` reads ``a12``); any other
apostrophe or dash becomes a space.
"""

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

from livecart_core.domain.parser.lexicon import GLUED_QUANTITY_PREFIXES

# Marks that the folding writes into the text, one character each. None of
# them can come from the comment itself: a "?" typed by the buyer is the same
# question mark, the cart emoji is only written by the explicit reading, and
# "$" stands for every currency symbol typed.
QUESTION: Final = "?"
CART: Final = "\U0001f6d2"
NEGATION: Final = "\u00ac"
AMOUNT_UNIT: Final = "$"
# A typed "." folds to a space: only a decimal separator is written as this mark.
DECIMAL: Final = "."
# A typed minus sign folds to "-" (see _EXPLICIT): only a sign is written as this mark.
MINUS: Final = "\u2212"

_EXPLICIT: Final[dict[str, str]] = {
    "\u00d7": "x",  # MULTIPLICATION SIGN
    "\u2715": "x",  # MULTIPLICATION X
    "\u2716": "x",  # HEAVY MULTIPLICATION X, the emoji
    "\u2a2f": "x",  # VECTOR OR CROSS PRODUCT
    CART: f" {CART} ",
    "\U0001f51f": "10",  # KEYCAP TEN
    "\u00bf": "?",  # INVERTED QUESTION MARK
    "\u2753": "?",  # BLACK QUESTION MARK ORNAMENT, the emoji
    "\u2754": "?",  # WHITE QUESTION MARK ORNAMENT, the emoji
    "\u2018": "'",  # LEFT SINGLE QUOTATION MARK
    "\u2019": "'",  # RIGHT SINGLE QUOTATION MARK, what phones type for '
    "\u02bc": "'",  # MODIFIER LETTER APOSTROPHE
    "\u00b4": "'",  # ACUTE ACCENT, typed as an apostrophe on some keyboards
    "`": "'",
    "\u2032": "'",  # PRIME
    "\u2212": "-",  # MINUS SIGN
}
_KEPT_PUNCTUATION: Final = frozenset({"?", "'", "@"})
# ASCII and fullwidth, since fullwidth digits fold to digits too.
_DECIMAL_SEPARATORS: Final = frozenset({",", ".", "\uff0c", "\uff0e"})


@dataclass(frozen=True, slots=True)
class NormalizedText:
    """The folded text, and for each folded character its span in ``original``."""

    original: str
    text: str
    starts: tuple[int, ...]
    ends: tuple[int, ...]

    def source_span(self, start: int, end: int) -> tuple[int, int]:
        """The original span that the folded characters ``[start, end)`` come from."""
        return self.starts[start], self.ends[end - 1]


def _fold_char(char: str) -> str:
    """The folded form of one character: empty, one or several characters."""
    if char.isascii():
        return _ASCII_FOLDS[ord(char)]
    return _fold_non_ascii(char)


# A comment repeats a few characters many times; each is folded once.
@lru_cache(maxsize=4096)
def _fold_non_ascii(char: str) -> str:
    return _fold_uncached(char)


def _fold_uncached(char: str) -> str:
    if (explicit := _EXPLICIT.get(char)) is not None:
        return explicit
    decomposed = unicodedata.normalize("NFD", unicodedata.normalize("NFKC", char).casefold())
    folded: list[str] = []
    for part in decomposed:
        category = unicodedata.category(part)
        if category[0] == "M" or category == "Cf":
            continue
        if category[0] in "LN" or part in _KEPT_PUNCTUATION:
            folded.append(part)
        elif category == "Pd":
            folded.append("-")
        elif category == "Sc" or part == "%":
            folded.append(AMOUNT_UNIT)
        else:
            folded.append(" ")
    return "".join(folded)


_ASCII_FOLDS: Final = tuple(_fold_uncached(chr(code)) for code in range(128))


def _rewrite(
    source: NormalizedText, pattern: re.Pattern[str], replace: Callable[[re.Match[str]], str]
) -> NormalizedText:
    """Replace every match of ``pattern``, keeping the map to the original text.

    A replacement takes the span of the whole match; a deletion leaves the
    neighbours' spans as they are, so a joined word still quotes the dropped
    character in its evidence.
    """
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    position = 0
    for match in pattern.finditer(source.text):
        chars.append(source.text[position : match.start()])
        starts.extend(source.starts[position : match.start()])
        ends.extend(source.ends[position : match.start()])
        replacement = replace(match)
        if replacement:
            start, end = source.source_span(match.start(), match.end())
            chars.append(replacement)
            starts.extend([start] * len(replacement))
            ends.extend([end] * len(replacement))
        position = match.end()
    chars.append(source.text[position:])
    starts.extend(source.starts[position:])
    ends.extend(source.ends[position:])
    return NormalizedText(source.original, "".join(chars), tuple(starts), tuple(ends))


_WORD_CHAR: Final = r"[^\W_]"
_MENTION: Final = re.compile(rf"@{_WORD_CHAR}*")
_ELISION: Final = re.compile(rf"(?<!{_WORD_CHAR})(qu|[cdjlmnst])'(?={_WORD_CHAR})")
_INNER_APOSTROPHE: Final = re.compile(rf"(?<={_WORD_CHAR})'(?={_WORD_CHAR})")
# A lookbehind must have a fixed width: one per prefix.
_SIGN: Final = re.compile(
    "(?:"
    + "|".join(
        [
            rf"(?<!{_WORD_CHAR})",
            *(rf"(?<=(?<!{_WORD_CHAR}){prefix})" for prefix in GLUED_QUANTITY_PREFIXES),
        ]
    )
    + ")-(?=[0-9])"
)
_CODE_HYPHEN: Final = re.compile(rf"(?<!{_WORD_CHAR})([a-z]{{1,3}})-(?=[0-9])")
_LEFTOVER: Final = re.compile(r"['-]")


def _between_digits(text: str, index: int) -> bool:
    return 0 < index < len(text) - 1 and text[index - 1].isdecimal() and text[index + 1].isdecimal()


def normalize_comment(text: str) -> NormalizedText:
    """Fold a comment for matching; see the module docstring for the rules."""
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for index, char in enumerate(text):
        folded = _fold_char(char)
        if char in _DECIMAL_SEPARATORS and _between_digits(text, index):
            folded = DECIMAL
        if folded:
            chars.append(folded)
            starts.extend([index] * len(folded))
            ends.extend([index + 1] * len(folded))
        elif ends:
            # A dropped character (an accent, the keycap enclosure) belongs to
            # the character before it, so that the evidence quotes a keycap whole.
            ends[-1] = index + 1
    folded_text = NormalizedText(text, "".join(chars), tuple(starts), tuple(ends))
    folded_text = _rewrite(folded_text, _MENTION, lambda _: " ")
    folded_text = _rewrite(
        folded_text,
        _ELISION,
        lambda match: f" {NEGATION} " if match.group(1) == "n" else " ",
    )
    folded_text = _rewrite(folded_text, _INNER_APOSTROPHE, lambda _: "")
    folded_text = _rewrite(folded_text, _SIGN, lambda _: MINUS)
    folded_text = _rewrite(folded_text, _CODE_HYPHEN, lambda match: match.group(1))
    return _rewrite(folded_text, _LEFTOVER, lambda _: " ")
