import pytest

from livecart_core.domain.errors import InvalidValue
from livecart_core.domain.sellers import (
    MAX_DISPLAY_NAME_LENGTH,
    MAX_EMAIL_LENGTH,
    MAX_PASSWORD_LENGTH,
    MAX_SLUG_LENGTH,
    MIN_PASSWORD_LENGTH,
    check_display_name,
    check_email,
    check_password,
    check_slug,
)


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("seller@example.test", "seller@example.test"),
        # The case is kept as typed: uniqueness ignores it, the stored value does not.
        ("  Demo.Seller+live@Example.TEST \n", "Demo.Seller+live@Example.TEST"),
        ("a@b", "a@b"),
    ],
)
def test_an_email_is_stored_stripped_with_its_case(typed: str, stored: str) -> None:
    assert check_email(typed) == stored


def test_an_email_may_use_the_whole_smtp_length() -> None:
    longest = "a" * (MAX_EMAIL_LENGTH - len("@example.test")) + "@example.test"

    assert check_email(longest) == longest
    with pytest.raises(InvalidValue, match="1 to 254"):
        check_email("a" + longest)


@pytest.mark.parametrize(
    "email",
    [
        "",
        "   ",
        "seller.example.test",
        "@example.test",
        "seller@",
        "seller@@example.test",
        "sel ler@example.test",
        "seller@exa\tmple.test",
        # Outside ASCII, lower() in PostgreSQL and in Python may disagree.
        "vendeuse@exemple.fré",
        "STRASSE@example.testß",
        "seller@example.test\x00",
    ],
)
def test_anything_but_an_ascii_address_is_refused(email: str) -> None:
    with pytest.raises(InvalidValue):
        check_email(email)


@pytest.mark.parametrize("slug", ["demo-seller", "sample-shop", "a", "shop42", "x-1-y"])
def test_a_slug_is_lower_case_words_joined_by_hyphens(slug: str) -> None:
    assert check_slug(slug) == slug


@pytest.mark.parametrize(
    "slug",
    ["", "Demo-Seller", "demo_seller", "demo--seller", "-demo", "demo-", "démo", " demo", "a b"],
)
def test_any_other_slug_is_refused(slug: str) -> None:
    with pytest.raises(InvalidValue, match="not a slug"):
        check_slug(slug)


def test_a_slug_fits_its_column() -> None:
    assert check_slug("a" * MAX_SLUG_LENGTH) == "a" * MAX_SLUG_LENGTH
    with pytest.raises(InvalidValue):
        check_slug("a" * (MAX_SLUG_LENGTH + 1))


def test_a_display_name_is_stripped_and_bounded() -> None:
    assert check_display_name("  LiveCart Demo  ") == "LiveCart Demo"
    assert check_display_name("é" * MAX_DISPLAY_NAME_LENGTH) == "é" * MAX_DISPLAY_NAME_LENGTH
    for refused in ("", "   ", "x" * (MAX_DISPLAY_NAME_LENGTH + 1), "Live\nCart"):
        with pytest.raises(InvalidValue, match="display name"):
            check_display_name(refused)


@pytest.mark.parametrize(
    "password",
    [
        "x" * MIN_PASSWORD_LENGTH,
        "x" * MAX_PASSWORD_LENGTH,
        # Any character counts, spaces and letters beyond ASCII included, and
        # nothing is stripped.
        " correct horse battery ",
        "mot de passe très long",
    ],
)
def test_a_password_of_12_to_128_characters_is_kept_as_typed(password: str) -> None:
    assert check_password(password) == password


@pytest.mark.parametrize("length", [0, 1, MIN_PASSWORD_LENGTH - 1, MAX_PASSWORD_LENGTH + 1])
def test_a_password_outside_the_bounds_is_refused_without_being_repeated(length: int) -> None:
    # A letter the message does not hold, so that finding it would mean an echo.
    password = "Z" * length

    with pytest.raises(InvalidValue, match="12 to 128 characters") as excinfo:
        check_password(password)

    assert "Z" not in str(excinfo.value)
