import pytest

from livecart_core.domain.parser.lexicon import (
    BUY_VERBS,
    COLOUR_WORDS,
    GRAMMAR_WORDS,
    NOT_A_TYPO,
    SIZE_WORDS,
    in_vocabulary,
    one_edit_apart,
    same_variant_word,
    same_word,
    time_or_ordinal,
    typo_of_buy_verb,
    word_forms,
)


@pytest.mark.parametrize(
    ("typed", "reference"),
    [
        ("prend", "prends"),  # deletion
        ("prendss", "prends"),  # insertion
        ("prenda", "prends"),  # substitution
        ("slod", "sold"),  # swap of neighbours
        ("sodl", "sold"),
        ("tkae", "take"),
    ],
)
def test_one_edit_apart(typed: str, reference: str) -> None:
    assert one_edit_apart(typed, reference)
    assert one_edit_apart(reference, typed)


@pytest.mark.parametrize(
    ("typed", "reference"),
    [
        ("sold", "sold"),  # no edit at all
        ("sdlo", "sold"),  # two edits
        ("prendre", "prends"),
        ("pren", "prends"),
        ("osdl", "sold"),  # two swaps
        ("sloe", "sold"),  # a swap and a substitution
    ],
)
def test_not_one_edit_apart(typed: str, reference: str) -> None:
    assert not one_edit_apart(typed, reference)


@pytest.mark.parametrize(
    "typed",
    ["slod", "prend", "prnds", "tkae", "wnat", "achte", "veu", "sodl", "odrer", "reprend"],
)
def test_a_buy_verb_with_one_typo_is_recognised(typed: str) -> None:
    assert typo_of_buy_verb(typed)


@pytest.mark.parametrize(
    ("typed", "why"),
    [
        ("sold", "the verb itself is not a typo"),
        ("by", "buy is too short to tolerate a typo"),
        ("guy", "buy is too short to tolerate a typo"),
        ("gold", "the first letter differs, and it is a colour"),
        ("deux", "the first letter differs from veux"),
        ("make", "the first letter differs from take"),
        ("solde", "a real word one edit away"),
        ("vieux", "a real word one edit away"),
        ("wait", "a real word one edit away"),
        ("taken", "a real word one edit away"),
        ("reserved", "someone else's reservation"),
        ("achetez", "a call to the other viewers"),
        ("older", "a real word one edit away"),
        ("purchased", "a purchase already made"),
        ("jprendss", "slang tolerates no typo"),
        ("mind", "slang tolerates no typo"),
        ("me", "a word of the grammar"),
        ("ta2e", "not a word"),
        ("slodd", "two edits away"),
    ],
)
def test_ordinary_words_are_not_typos_of_buy_verbs(typed: str, why: str) -> None:
    assert not typo_of_buy_verb(typed), why


def test_every_word_that_is_not_a_typo_really_is_one_edit_from_a_verb() -> None:
    # Keeps the list honest: an entry that no verb is close to has no purpose.
    for word in NOT_A_TYPO:
        assert any(one_edit_apart(word, verb) and word[0] == verb[0] for verb in BUY_VERBS), word


@pytest.mark.parametrize(
    ("typed", "reference"),
    [
        ("bleue", "bleu"),
        ("bleus", "bleu"),
        ("bleues", "bleu"),
        ("robes", "robe"),
        ("gris", "grise"),
    ],
)
def test_same_word_allows_a_plural_or_feminine_ending(typed: str, reference: str) -> None:
    assert same_word(typed, reference)
    assert same_word(reference, typed)


@pytest.mark.parametrize(
    ("typed", "reference"),
    [
        ("me", "m"),
        ("ms", "m"),
        ("les", "l"),
        ("38s", "38"),
        ("blanche", "blanc"),
        ("rougex", "rouge"),
    ],
)
def test_same_word_needs_a_base_of_three_letters_and_a_known_ending(
    typed: str, reference: str
) -> None:
    assert not same_word(typed, reference)


def test_word_forms_of_a_word_that_is_not_alphabetic_is_the_word_alone() -> None:
    assert word_forms("38") == {"38"}
    assert word_forms("2xl") == {"2xl"}


@pytest.mark.parametrize(
    ("typed", "label"),
    [("blue", "bleu"), ("bleu", "bleu"), ("red", "rouge"), ("medium", "m"), ("grey", "gris")],
)
def test_a_variant_word_matches_its_french_or_english_synonym(typed: str, label: str) -> None:
    assert same_variant_word(typed, label)


@pytest.mark.parametrize(("typed", "label"), [("red", "bleu"), ("small", "m"), ("blue", "38")])
def test_a_variant_word_does_not_match_another_label(typed: str, label: str) -> None:
    assert not same_variant_word(typed, label)


def test_vocabulary_membership_allows_inflections() -> None:
    assert in_vocabulary("rouges", COLOUR_WORDS)
    assert in_vocabulary("noire", COLOUR_WORDS)
    assert in_vocabulary("xl", SIZE_WORDS)
    assert not in_vocabulary("me", SIZE_WORDS)
    assert not in_vocabulary("robe", COLOUR_WORDS)


def test_the_lexicon_is_folded() -> None:
    # normalize_comment leaves lower-case words without accents; so must the lists.
    for word in GRAMMAR_WORDS | NOT_A_TYPO:
        assert word == word.lower()
        assert word.isascii(), word


@pytest.mark.parametrize(
    "word", ["14h", "10h30", "9h00", "20min", "5mn", "3e", "2eme", "1er", "1ere", "2nd", "4th"]
)
def test_a_number_glued_to_a_time_or_ordinal_ending(word: str) -> None:
    assert time_or_ordinal(word)


@pytest.mark.parametrize("word", ["14", "h", "2x", "2pcs", "10h3", "x2", "3eme3", "2ex"])
def test_other_glued_numbers_are_not_times_or_ordinals(word: str) -> None:
    assert not time_or_ordinal(word)
