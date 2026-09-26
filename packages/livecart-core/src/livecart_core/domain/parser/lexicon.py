"""The closed French and English vocabulary of the grammar, in folded form.

Every list is lower case and without accents, as ``normalize_comment`` leaves
the words: "achète" is listed as "achete". A word outside every list is a
content word: it does not stop a buy verb from matching, but it does stop the
"code alone" form (see ``livecart_core.domain.parser``).

Only the buy verbs tolerate a typo, and only one: a single insertion,
deletion, substitution or swap of two neighbouring letters (the optimal
string alignment distance), as in "slod" for "sold" or "prend" for
"prends". Three guards keep that tolerance from reading ordinary words as
verbs: the verb has at least four letters (a typo of "buy" would accept
"by" and "guy"), the first letter is right (so "gold", "deux" and "make" are
not typos of "sold", "veux" and "take"), and the word is not itself a word of
this vocabulary or of ``NOT_A_TYPO``, the real words one edit away from a verb.
"""

import re
from functools import lru_cache
from typing import Final

# One word is enough; the pronoun before it ("je", "i'll") is a filler.
BUY_VERBS: Final = frozenset(
    {
        "prends",
        "prendre",
        "veux",
        "voudrais",
        "achete",
        "acheter",
        "reserve",
        "commande",
        "sold",
        "take",
        "want",
        "wanna",
        "buy",
    }
)
TYPO_MIN_LENGTH: Final = 4
# "i'd like" and "would like": "like" alone is a compliment, not an order.
BUY_PHRASES: Final = frozenset({("id", "like"), ("would", "like")})

NOT_A_TYPO: Final = frozenset(
    {
        "pendre",
        "vieux",
        "vaux",
        "acheve",
        "sol",
        "sole",
        "solo",
        "sols",
        "solde",
        "scold",
        "tale",
        "tame",
        "tape",
        "taken",
        "wait",
        "went",
        "wand",
        "wane",
        "wart",
        "wan",
    }
)

# A question, a negation or a doubt about the stock. With a buy signal it is a
# conflicting signal; without one, the comment is not an order.
DOUBT_WORDS: Final = frozenset(
    {
        # Questions.
        "dispo",
        "dispos",
        "disponible",
        "disponibles",
        "available",
        "availability",
        "combien",
        "prix",
        "price",
        "how",
        "what",
        "quel",
        "quelle",
        "quels",
        "quelles",
        "quoi",
        "reste",
        "restent",
        "restant",
        # Negations and cancellations.
        "pas",
        "non",
        "ne",
        "jamais",
        "annule",
        "annuler",
        "annulez",
        "not",
        "no",
        "dont",
        "cant",
        "wont",
        "didnt",
        "doesnt",
        "isnt",
        "never",
        "nope",
        "cancel",
        # Stock.
        "epuise",
        "epuisee",
        "epuises",
        "epuisees",
        "rupture",
    }
)
# "sold out" is read before "sold", the verb.
DOUBT_PHRASES: Final = frozenset({("sold", "out"), ("out", "of", "stock")})

# Written after "x" or before a number: "x2", "qte 2", "quantite: 2".
QUANTITY_PREFIXES: Final = frozenset({"x", "qte", "qty", "qt", "nb", "quantite", "quantity"})
# The prefixes a buyer glues to the number: "x2", "qte3", "nb5".
GLUED_QUANTITY_PREFIXES: Final = ("x", "qte", "qty", "qt", "nb")
# Written after a number: "2x", "2 pcs", "2 fois".
QUANTITY_SUFFIXES: Final = frozenset(
    {
        "x",
        "pcs",
        "pc",
        "piece",
        "pieces",
        "ex",
        "exemplaire",
        "exemplaires",
        "unite",
        "unites",
        "unit",
        "units",
        "item",
        "items",
        "fois",
        "paire",
        "paires",
        "pair",
        "pairs",
    }
)
# The parser reads quantities in digits only. A number written as a word may
# change the quantity, so it is a conflicting signal rather than silently 1.
# "un", "une" and "one" mean the default quantity and are fillers; "neuf" is
# left out, as it also means "new".
NUMBER_WORDS: Final = frozenset(
    {
        "deux",
        "trois",
        "quatre",
        "cinq",
        "six",
        "sept",
        "huit",
        "dix",
        "two",
        "three",
        "four",
        "five",
        "seven",
        "eight",
        "nine",
        "ten",
    }
)
# The price of the product, not a quantity: "15 euros", "15 balles".
CURRENCY_WORDS: Final = frozenset(
    {"eur", "euro", "euros", "balle", "balles", "usd", "dollar", "dollars"}
)
# A time, not a quantity: "14 h", "20 heures", "10 min".
TIME_WORDS: Final = frozenset({"h", "heure", "heures", "min", "mn", "minute", "minutes"})
# Glued to a number, a time ("14h", "10h30", "20min") or an ordinal ("3e",
# "2eme", "1er", "2nd"; "15e" is also 15 euros): never a quantity either.
_TIME_OR_ORDINAL: Final = re.compile(
    r"[0-9]+(?:h(?:[0-9]{2})?|min|mn|e|er|ere|eme|nd|nde|st|rd|th)"
)

# French for "in", as in "en 38": the number after it is a size, not a quantity.
SIZE_PREPOSITION: Final = "en"
# The next word is a size or a colour, even when it looks like a quantity: "taille 2".
VARIANT_KEYWORDS: Final = frozenset(
    {"taille", "size", "pointure", "couleur", "color", "colour", "coloris"}
)
# Words that name a size or a colour. For a product with variants, one that
# matches no label of the product is a variant that does not exist.
SIZE_WORDS: Final = frozenset(
    {
        "xxs",
        "xs",
        "s",
        "m",
        "l",
        "xl",
        "xxl",
        "xxxl",
        "2xl",
        "3xl",
        "4xl",
        "5xl",
        "6xl",
        "small",
        "medium",
        "large",
    }
)
COLOUR_WORDS: Final = frozenset(
    {
        "rouge",
        "bleu",
        "vert",
        "noir",
        "blanc",
        "blanche",
        "jaune",
        "rose",
        "gris",
        "violet",
        "violette",
        "orange",
        "marron",
        "beige",
        "dore",
        "argent",
        "argente",
        "kaki",
        "bordeaux",
        "turquoise",
        "fuchsia",
        "marine",
        "camel",
        "ecru",
        "creme",
        "multicolore",
        "red",
        "blue",
        "green",
        "black",
        "white",
        "yellow",
        "pink",
        "grey",
        "gray",
        "purple",
        "brown",
        "gold",
        "silver",
        "navy",
        "khaki",
        "cream",
        "ivory",
        "burgundy",
        "teal",
        "nude",
    }
)

# Words with no bearing on the order: pronouns, articles, politeness, greetings.
FILLERS: Final = frozenset(
    {
        # French.
        "je",
        "moi",
        "me",
        "pour",
        "le",
        "la",
        "les",
        "un",
        "une",
        "des",
        "du",
        "de",
        "en",
        "et",
        "ou",
        "aussi",
        "ca",
        "cela",
        "ce",
        "celui",
        "celle",
        "ci",
        "mon",
        "ma",
        "mes",
        "est",
        "svp",
        "stp",
        "il",
        "vous",
        "te",
        "plait",
        "merci",
        "beaucoup",
        "bonjour",
        "bonsoir",
        "salut",
        "coucou",
        "oui",
        "accord",
        "voila",
        # English.
        "i",
        "ill",
        "im",
        "id",
        "will",
        "my",
        "the",
        "a",
        "an",
        "for",
        "of",
        "in",
        "and",
        "or",
        "too",
        "also",
        "one",
        "please",
        "pls",
        "plz",
        "thanks",
        "thank",
        "thx",
        "you",
        "hello",
        "hi",
        "hey",
        "yes",
        "yeah",
        "yep",
        "ok",
        "okay",
    }
)

# The same size or colour in French and English, so that "C3 blue" finds the
# seller's label "Bleu" and "A12 medium" the label "M".
VARIANT_SYNONYMS: Final = (
    frozenset({"s", "small"}),
    frozenset({"m", "medium"}),
    frozenset({"l", "large"}),
    frozenset({"bleu", "blue"}),
    frozenset({"rouge", "red"}),
    frozenset({"noir", "black"}),
    frozenset({"blanc", "blanche", "white"}),
    frozenset({"vert", "green"}),
    frozenset({"jaune", "yellow"}),
    frozenset({"rose", "pink"}),
    frozenset({"gris", "grey", "gray"}),
    frozenset({"violet", "violette", "purple"}),
    frozenset({"marron", "brown"}),
    frozenset({"dore", "gold"}),
    frozenset({"argent", "argente", "silver"}),
    frozenset({"kaki", "khaki"}),
    frozenset({"creme", "cream"}),
    frozenset({"marine", "navy"}),
    frozenset({"bordeaux", "burgundy"}),
)

# Every word the grammar gives a meaning to; none of them is ever a typo.
GRAMMAR_WORDS: Final = (
    BUY_VERBS
    | DOUBT_WORDS
    | QUANTITY_PREFIXES
    | QUANTITY_SUFFIXES
    | NUMBER_WORDS
    | CURRENCY_WORDS
    | TIME_WORDS
    | VARIANT_KEYWORDS
    | SIZE_WORDS
    | COLOUR_WORDS
    | FILLERS
)

# A French adjective agrees with the noun and a noun takes a plural: "bleue",
# "bleus" and "bleues" are the colour "bleu", "robes" the name "robe".
_INFLECTIONS: Final = ("s", "e", "es")
_MIN_INFLECTED_BASE: Final = 3
# Bounds of the memo caches below: a live session repeats the same few words.
_CACHE_SIZE: Final = 4096
_SYNONYMS_OF: Final = {word: group for group in VARIANT_SYNONYMS for word in group}


@lru_cache(maxsize=_CACHE_SIZE)
def word_forms(word: str) -> frozenset[str]:
    """``word``, and the words it is an inflection of or inflects to.

    One of two words is the other plus an ``s``, ``e`` or ``es`` ending; the
    base must have three letters or more, so that "me" never reads as the
    size "m".
    """
    forms = {word}
    if word.isalpha():
        for ending in _INFLECTIONS:
            base = word.removesuffix(ending)
            if base != word and len(base) >= _MIN_INFLECTED_BASE:
                forms.add(base)
            if len(word) >= _MIN_INFLECTED_BASE:
                forms.add(word + ending)
    return frozenset(forms)


def same_word(typed: str, reference: str) -> bool:
    """Equal, or one is the other with an ``s``, ``e`` or ``es`` ending (see ``word_forms``)."""
    return reference in word_forms(typed)


def same_variant_word(typed: str, label_word: str) -> bool:
    """``same_word``, or a French or English synonym of the label word."""
    group = _SYNONYMS_OF.get(label_word, frozenset({label_word}))
    return not word_forms(typed).isdisjoint(group)


def in_vocabulary(word: str, vocabulary: frozenset[str]) -> bool:
    """``word`` is in ``vocabulary``, possibly inflected."""
    return not word_forms(word).isdisjoint(vocabulary)


def one_edit_apart(typed: str, reference: str) -> bool:
    """Exactly one insertion, deletion, substitution or swap of neighbours apart."""
    if typed == reference or abs(len(typed) - len(reference)) > 1:
        return False
    if len(typed) == len(reference):
        mismatches = [i for i, (a, b) in enumerate(zip(typed, reference, strict=True)) if a != b]
        if len(mismatches) == 1:
            return True
        first, second = mismatches[0], mismatches[-1]
        return (
            len(mismatches) == 2
            and second == first + 1
            and typed[first] == reference[second]
            and typed[second] == reference[first]
        )
    shorter, longer = sorted((typed, reference), key=len)
    # Skip the one extra character of the longer word wherever it is.
    return any(longer[:i] + longer[i + 1 :] == shorter for i in range(len(longer)))


def time_or_ordinal(word: str) -> bool:
    """A number glued to a time or ordinal ending: "14h", "10h30", "3e", "2eme"."""
    return _TIME_OR_ORDINAL.fullmatch(word) is not None


@lru_cache(maxsize=_CACHE_SIZE)
def typo_of_buy_verb(word: str) -> bool:
    """A buy verb with one typo, under the guards of the module docstring."""
    if not word.isalpha() or word in GRAMMAR_WORDS or word in NOT_A_TYPO:
        return False
    return any(
        len(verb) >= TYPO_MIN_LENGTH and word[0] == verb[0] and one_edit_apart(word, verb)
        for verb in BUY_VERBS
    )
