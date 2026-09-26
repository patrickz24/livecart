"""The grammar pass: what each token of a comment means.

Roles are handed out in a fixed order, and a token keeps the first role it
gets, so that the catalog always wins over the generic vocabulary:

1. product references: the tokens read as catalog codes;
2. variants: when the comment names exactly one catalog code, the labels of
   that product, longest first ("bleu nuit" before "bleu"), matched on words,
   numbers and code-shaped tokens alike ("38", "T2"), allowing a plural or
   feminine ending ("bleue") and the English or French synonym of a size or
   colour ("blue" for "Bleu"); then a ``t38``-style size, read as "taille 38";
3. the word after a variant keyword ("taille", "size", "couleur"...), a size
   or colour even when it looks like a quantity ("taille 2"); likewise the
   number after "en" ("en 38"), while no variant is named yet and the product
   comes in numbered sizes, unless a quantity word follows it ("en 2
   exemplaires"): "A12 M en 2" and "C3 rouge en 2" are quantities;
4. phrases: "sold out" and "out of stock" (doubts), "i'd like" and "would like"
   (buy signals);
5. the unknown codes left: product references too;
6. quantities: a number next to a currency, ``%``, a time unit or a decimal
   separator is a price, a discount, a time or a decimal and is ignored
   ("15,90 €", "29.99", "14 h"); then a glued quantity ("x2"), a prefix and a
   number ("x 2", "qte 2"), a number and a suffix ("2x", "2 pcs"), and
   finally a bare number;
7. marks: the cart emoji is a buy signal, a question mark or an elided "n'" a
   doubt;
8. words of the lexicon: buy verbs, doubts, numbers in words, the name of the
   referenced product, sizes and colours, fillers, times and ordinals
   ("14h", "3e"); then the typos of buy verbs; every other word is content.

When the comment references no product at all, the content, size and colour
words are also compared with the words of every product name.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum, auto
from functools import lru_cache
from typing import Final

from livecart_core.domain.catalog import Catalog, CatalogProduct
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.parser import lexicon
from livecart_core.domain.parser.normalization import normalize_comment
from livecart_core.domain.parser.tokens import (
    Code,
    GluedQuantity,
    Mark,
    MarkKind,
    Number,
    Token,
    UnknownCode,
    Word,
    tokenize,
)


class Role(Enum):
    REFERENCE = auto()
    VARIANT = auto()
    VARIANT_WORD = auto()
    SIGNAL = auto()
    TYPO_SIGNAL = auto()
    DOUBT = auto()
    QUANTITY = auto()
    NUMBER_WORD = auto()
    NEUTRAL = auto()
    CONTENT = auto()


@dataclass(frozen=True, slots=True)
class Mention:
    """A stretch of the original comment: one token, or a few read together."""

    start: int
    end: int
    text: str


@dataclass(frozen=True, slots=True)
class Reference:
    """One product the comment points at, by a catalog code or a code-shaped token."""

    # The catalog code, or None for a code-shaped token that matches none.
    code: ProductCode | None
    # True when every mention needed a look-alike swap to match the code.
    corrected: bool
    # For an unknown code, the catalog codes it could be read as, when several.
    candidates: tuple[ProductCode, ...]
    mentions: tuple[Mention, ...]


@dataclass(frozen=True, slots=True)
class QuantityMention:
    value: int
    mention: Mention


@dataclass(frozen=True, slots=True)
class VariantMention:
    variant: Variant
    mention: Mention


@dataclass(frozen=True, slots=True)
class NamedProduct:
    code: ProductCode
    mentions: tuple[Mention, ...]


@dataclass(frozen=True, slots=True)
class Reading:
    """Everything the decision needs, with the mentions it quotes as evidence."""

    references: tuple[Reference, ...]
    signals: tuple[Mention, ...]
    typo_signals: tuple[Mention, ...]
    doubts: tuple[Mention, ...]
    quantities: tuple[QuantityMention, ...]
    variants: tuple[VariantMention, ...]
    variant_words: tuple[Mention, ...]
    number_words: tuple[Mention, ...]
    content: tuple[Mention, ...]
    named_products: tuple[NamedProduct, ...]


_NO_CATALOG: Final = Catalog(())
_NUMBERED_SIZE_CODE: Final = re.compile(r"t([0-9]{1,3})")
_DASHES: Final = re.compile(r"[-\u2010-\u2015]")
_MIN_NAME_WORD: Final = 3
# Colours and sizes may be part of a name ("Pull rouge"); the rest of the
# vocabulary never identifies a product.
_NAME_STOPWORDS: Final = lexicon.GRAMMAR_WORDS - lexicon.COLOUR_WORDS - lexicon.SIZE_WORDS
_UNIT_WORDS: Final = lexicon.CURRENCY_WORDS | lexicon.TIME_WORDS


def _phrases_by_first_word() -> dict[str, list[tuple[tuple[str, ...], Role]]]:
    phrases: dict[str, list[tuple[tuple[str, ...], Role]]] = {}
    for words, role in [
        *((words, Role.DOUBT) for words in sorted(lexicon.DOUBT_PHRASES)),
        *((words, Role.SIGNAL) for words in sorted(lexicon.BUY_PHRASES)),
    ]:
        phrases.setdefault(words[0], []).append((words, role))
    return phrases


_PHRASES_BY_FIRST_WORD: Final = _phrases_by_first_word()
# Labels and names come back with every comment of a session: fold them once.
_CACHE_SIZE: Final = 4096


@lru_cache(maxsize=_CACHE_SIZE)
def label_words(label: str) -> tuple[str, ...]:
    """The words of a variant label, folded and split exactly as a comment is."""
    return tuple(
        token.text
        for token in tokenize(normalize_comment(label), _NO_CATALOG)
        if not isinstance(token, Mark)
    )


@lru_cache(maxsize=_CACHE_SIZE)
def name_words(name: str) -> frozenset[str]:
    """The words of a product name that may identify it: "T-shirt" gives "shirt" and "tshirt"."""
    words = {
        word
        for spelling in (name, _DASHES.sub("", name))
        for word in label_words(spelling)
        if word.isalpha() and len(word) >= _MIN_NAME_WORD and word not in _NAME_STOPWORDS
    }
    return frozenset(words)


class _Grammar:
    """Assigns one role per token, in the order of the module docstring."""

    def __init__(self, original: str, tokens: list[Token], catalog: Catalog) -> None:
        self._original = original
        self._tokens = tokens
        self._catalog = catalog
        self._roles: list[Role | None] = [None] * len(tokens)
        self._quantities: list[QuantityMention] = []
        self._variants: list[VariantMention] = []
        # The last token of a phrase, keyed by its first token, which holds its role.
        self._phrase_ends: dict[int, int] = {}

    def read(self) -> Reading:
        codes = [token for token in self._tokens if isinstance(token, Code)]
        self._assign_all(self._indexes(Code), Role.REFERENCE)
        distinct = list(dict.fromkeys(token.code for token in codes))
        product = self._catalog.get(distinct[0]) if len(distinct) == 1 else None
        if product is not None:
            self._read_variants(product)
        self._read_variant_keywords(product)
        self._read_phrases()
        unknown_indexes = self._indexes(UnknownCode)
        unknown = [
            token for i in unknown_indexes if isinstance(token := self._tokens[i], UnknownCode)
        ]
        self._assign_all(unknown_indexes, Role.REFERENCE)
        self._read_quantities()
        self._read_marks()
        self._read_words(product)
        references = self._references(codes, unknown)
        return Reading(
            references=references,
            signals=self._mentions(Role.SIGNAL),
            typo_signals=self._mentions(Role.TYPO_SIGNAL),
            doubts=self._mentions(Role.DOUBT),
            quantities=tuple(self._quantities),
            variants=tuple(self._variants),
            variant_words=self._mentions(Role.VARIANT_WORD),
            number_words=self._mentions(Role.NUMBER_WORD),
            content=self._mentions(Role.CONTENT),
            named_products=() if references else self._named_products(),
        )

    # Helpers.

    def _indexes(self, kind: type[Code] | type[UnknownCode]) -> list[int]:
        """The unassigned tokens of one kind."""
        return [
            i
            for i, token in enumerate(self._tokens)
            if isinstance(token, kind) and self._roles[i] is None
        ]

    def _mention(self, first: int, last: int) -> Mention:
        start, end = self._tokens[first].start, self._tokens[last].end
        return Mention(start, end, self._original[start:end])

    def _mentions(self, role: Role) -> tuple[Mention, ...]:
        return tuple(
            self._mention(i, self._phrase_ends.get(i, i))
            for i, assigned in enumerate(self._roles)
            if assigned is role
        )

    def _assign_all(self, indexes: Iterable[int], role: Role) -> None:
        for i in indexes:
            self._roles[i] = role

    def _free_word(self, i: int) -> str | None:
        """The text of token ``i`` if it is an unassigned word, else None."""
        if 0 <= i < len(self._tokens) and self._roles[i] is None:
            token = self._tokens[i]
            if isinstance(token, Word):
                return token.text
        return None

    def _free_number(self, i: int) -> Number | None:
        if 0 <= i < len(self._tokens) and self._roles[i] is None:
            token = self._tokens[i]
            if isinstance(token, Number):
                return token
        return None

    def _free_label_part(self, i: int) -> str | None:
        """The text of token ``i`` if it is unassigned and could be part of a variant label."""
        if 0 <= i < len(self._tokens) and self._roles[i] is None:
            token = self._tokens[i]
            if isinstance(token, Word | Number | UnknownCode):
                return token.text
        return None

    # Passes.

    def _read_variants(self, product: CatalogProduct) -> None:
        labels = sorted(
            ((label_words(variant.value), variant) for variant in product.variants),
            key=lambda entry: -len(entry[0]),
        )
        i = 0
        while i < len(self._tokens):
            matched = next(
                (
                    (words, variant)
                    for words, variant in labels
                    if words and self._label_at(i, words)
                ),
                None,
            )
            if matched is None:
                i += 1
                continue
            words, variant = matched
            last = i + len(words) - 1
            self._assign_all(range(i, last + 1), Role.VARIANT)
            self._variants.append(VariantMention(variant, self._mention(i, last)))
            i = last + 1
        # "T38" is "taille 38": a size of this product, or a size it does not have.
        for i, token in enumerate(self._tokens):
            if not (isinstance(token, UnknownCode) and self._roles[i] is None):
                continue
            if (size := _NUMBERED_SIZE_CODE.fullmatch(token.text)) is None:
                continue
            sized = next((v for words, v in labels if words == (size.group(1),)), None)
            if sized is None:
                self._roles[i] = Role.VARIANT_WORD
            else:
                self._roles[i] = Role.VARIANT
                self._variants.append(VariantMention(sized, self._mention(i, i)))

    def _label_at(self, i: int, words: tuple[str, ...]) -> bool:
        for offset, word in enumerate(words):
            part = self._free_label_part(i + offset)
            if part is None or not lexicon.same_variant_word(part, word):
                return False
        return True

    def _read_variant_keywords(self, product: CatalogProduct | None) -> None:
        # "en 38" names a size only while the size is still open and the
        # product comes in numbered sizes: after "A12 M", or for a T-shirt in
        # colours, "en 2" can only be a quantity. Without a single product the
        # sizes are unknown, so the number stays a size rather than a quantity
        # of 38. "en 2 exemplaires" stays a quantity either way.
        sized = product is None or (not self._variants and _has_numbered_sizes(product))
        for i in range(len(self._tokens)):
            if (word := self._free_word(i)) is None:
                continue
            if word in lexicon.VARIANT_KEYWORDS:
                self._roles[i] = Role.NEUTRAL
                if self._free_label_part(i + 1) is not None:
                    self._roles[i + 1] = Role.VARIANT_WORD
            elif (
                word == lexicon.SIZE_PREPOSITION
                and sized
                and self._free_number(i + 1) is not None
                and self._free_word(i + 2) not in lexicon.QUANTITY_SUFFIXES
            ):
                self._roles[i + 1] = Role.VARIANT_WORD

    def _read_phrases(self) -> None:
        for i in range(len(self._tokens)):
            if (first := self._free_word(i)) is None:
                continue
            for words, role in _PHRASES_BY_FIRST_WORD.get(first, ()):
                span = range(i, i + len(words))
                if all(self._free_word(j) == word for j, word in zip(span, words, strict=True)):
                    # One mention for the whole phrase, on its first token.
                    self._assign_all(span, Role.NEUTRAL)
                    self._roles[i] = role
                    self._phrase_ends[i] = span[-1]

    def _read_quantities(self) -> None:
        for i in range(len(self._tokens)):
            number = self._free_number(i)
            if number is None:
                continue
            for neighbour in (i - 1, i + 1):
                if self._is_unit(neighbour):
                    self._roles[i] = Role.NEUTRAL
                    self._roles[neighbour] = Role.NEUTRAL
        i = 0
        while i < len(self._tokens):
            token = self._tokens[i]
            if self._roles[i] is not None:
                i += 1
            elif isinstance(token, GluedQuantity):
                self._quantity(token.value, i, i)
                i += 1
            elif (
                self._free_word(i) in lexicon.QUANTITY_PREFIXES
                and (number := self._free_number(i + 1)) is not None
            ):
                self._quantity(number.value, i, i + 1)
                i += 2
            elif (number := self._free_number(i)) is not None:
                if self._free_word(i + 1) in lexicon.QUANTITY_SUFFIXES:
                    self._quantity(number.value, i, i + 1)
                    i += 2
                else:
                    self._quantity(number.value, i, i)
                    i += 1
            else:
                i += 1

    def _is_unit(self, i: int) -> bool:
        """Token ``i`` makes the number next to it an amount, a decimal or a time."""
        if not 0 <= i < len(self._tokens):
            return False
        token = self._tokens[i]
        if isinstance(token, Mark):
            return token.kind in {MarkKind.AMOUNT_UNIT, MarkKind.DECIMAL}
        return self._free_word(i) in _UNIT_WORDS

    def _quantity(self, value: int, first: int, last: int) -> None:
        self._assign_all(range(first, last + 1), Role.QUANTITY)
        self._quantities.append(QuantityMention(value, self._mention(first, last)))

    def _read_marks(self) -> None:
        roles = {
            MarkKind.CART: Role.SIGNAL,
            MarkKind.QUESTION: Role.DOUBT,
            MarkKind.NEGATION: Role.DOUBT,
            MarkKind.AMOUNT_UNIT: Role.NEUTRAL,
            MarkKind.DECIMAL: Role.NEUTRAL,
        }
        for i, token in enumerate(self._tokens):
            if isinstance(token, Mark) and self._roles[i] is None:
                self._roles[i] = roles[token.kind]

    def _read_words(self, product: CatalogProduct | None) -> None:
        own_name = name_words(product.name) if product is not None else frozenset()
        # Every token but the words has a role by now.
        for i in range(len(self._tokens)):
            if (word := self._free_word(i)) is not None:
                self._roles[i] = _word_role(word, own_name)

    def _references(self, codes: list[Code], unknown: list[UnknownCode]) -> tuple[Reference, ...]:
        by_key: dict[str, list[Code | UnknownCode]] = {}
        in_order: list[Code | UnknownCode] = sorted([*codes, *unknown], key=lambda t: t.start)
        for token in in_order:
            key = str(token.code) if isinstance(token, Code) else token.text.upper()
            by_key.setdefault(key, []).append(token)
        references = []
        for tokens in by_key.values():
            first = tokens[0]
            mentions = tuple(
                Mention(t.start, t.end, self._original[t.start : t.end]) for t in tokens
            )
            if isinstance(first, Code):
                corrected = all(isinstance(t, Code) and t.corrected for t in tokens)
                references.append(Reference(first.code, corrected, (), mentions))
            else:
                references.append(Reference(None, False, first.candidates, mentions))
        return tuple(references)

    def _named_products(self) -> tuple[NamedProduct, ...]:
        words = [
            (i, token.text)
            for i, token in enumerate(self._tokens)
            if isinstance(token, Word) and self._roles[i] in {Role.CONTENT, Role.VARIANT_WORD}
        ]
        named = []
        for product in sorted(self._catalog, key=lambda p: str(p.code)):
            own = name_words(product.name)
            hits = tuple(
                self._mention(i, i)
                for i, word in words
                if not lexicon.word_forms(word).isdisjoint(own)
            )
            if hits:
                named.append(NamedProduct(product.code, hits))
        return tuple(named)


def _has_numbered_sizes(product: CatalogProduct) -> bool:
    """A label of the product starts with a number: "38", "2 ans"."""
    return any(
        (words := label_words(variant.value)) and words[0].isdigit() for variant in product.variants
    )


def _word_role(word: str, own_name: frozenset[str]) -> Role:
    if word in lexicon.BUY_VERBS:
        return Role.SIGNAL
    if word in lexicon.DOUBT_WORDS:
        return Role.DOUBT
    if word in lexicon.NUMBER_WORDS:
        return Role.NUMBER_WORD
    if not lexicon.word_forms(word).isdisjoint(own_name):
        return Role.NEUTRAL
    if lexicon.in_vocabulary(word, lexicon.SIZE_WORDS | lexicon.COLOUR_WORDS):
        return Role.VARIANT_WORD
    if word in lexicon.GRAMMAR_WORDS or lexicon.time_or_ordinal(word):
        return Role.NEUTRAL
    if lexicon.typo_of_buy_verb(word):
        return Role.TYPO_SIGNAL
    return Role.CONTENT


def read_comment(original: str, tokens: list[Token], catalog: Catalog) -> Reading:
    """Give every token its role and gather what the decision needs."""
    return _Grammar(original, tokens, catalog).read()
