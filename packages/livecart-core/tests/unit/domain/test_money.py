import operator
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from livecart_core.domain.errors import CurrencyMismatch, InvalidValue
from livecart_core.domain.money import Money


def test_money_is_a_value_equal_by_amount_and_currency() -> None:
    assert Money(1999, "EUR") == Money(1999, "EUR")
    assert hash(Money(1999, "EUR")) == hash(Money(1999, "EUR"))
    assert Money(1999, "EUR") != Money(2000, "EUR")


def test_equality_across_currencies_is_false_without_an_error() -> None:
    assert Money(1999, "EUR") != Money(1999, "USD")


def test_money_is_immutable() -> None:
    price = Money(1999, "EUR")

    with pytest.raises(FrozenInstanceError):
        price.amount_minor = 1  # type: ignore[misc]


def test_zero_is_a_valid_amount() -> None:
    assert Money(0, "EUR").amount_minor == 0


def test_a_negative_amount_is_rejected() -> None:
    with pytest.raises(InvalidValue, match="negative"):
        Money(-1, "EUR")


@pytest.mark.parametrize("amount", [19.99, True, "1999"], ids=["float", "bool", "str"])
def test_only_an_int_of_minor_units_is_accepted(amount: object) -> None:
    # The cast lets the test reach the runtime guard mypy would otherwise stop it at.
    with pytest.raises(InvalidValue, match="int of minor units"):
        Money(cast(int, amount), "EUR")


@pytest.mark.parametrize("currency", ["eur", "EURO", "EU", "E1R", "", "€"])
def test_the_currency_must_be_three_upper_case_letters(currency: str) -> None:
    with pytest.raises(InvalidValue, match="ISO 4217"):
        Money(1999, currency)


def test_addition_keeps_the_currency() -> None:
    assert Money(1000, "EUR") + Money(999, "EUR") == Money(1999, "EUR")


def test_addition_refuses_to_mix_currencies() -> None:
    with pytest.raises(CurrencyMismatch, match="add USD and EUR"):
        Money(1000, "EUR") + Money(1000, "USD")


def test_multiplication_by_a_quantity_is_exact() -> None:
    assert Money(1999, "EUR") * 3 == Money(5997, "EUR")
    assert 3 * Money(1999, "EUR") == Money(5997, "EUR")
    assert Money(1999, "EUR") * 0 == Money(0, "EUR")


def test_multiplication_by_a_negative_quantity_cannot_produce_a_negative_amount() -> None:
    with pytest.raises(InvalidValue, match="negative"):
        Money(1999, "EUR") * -1


@pytest.mark.parametrize("factor", [1.5, True], ids=["float", "bool"])
def test_multiplication_accepts_a_count_only(factor: object) -> None:
    with pytest.raises(TypeError):
        Money(1999, "EUR") * cast(int, factor)
    with pytest.raises(TypeError):
        cast(int, factor) * Money(1999, "EUR")


def test_ordering_within_a_currency() -> None:
    cheaper, dearer = Money(1000, "EUR"), Money(1999, "EUR")

    assert cheaper < dearer
    assert cheaper <= dearer
    assert dearer > cheaper
    assert dearer >= cheaper
    assert cheaper <= Money(1000, "EUR")
    assert cheaper >= Money(1000, "EUR")
    assert not cheaper > dearer
    assert not dearer < cheaper


@pytest.mark.parametrize("compare", [operator.lt, operator.le, operator.gt, operator.ge])
def test_ordering_refuses_to_mix_currencies(compare: Callable[[Money, Money], bool]) -> None:
    with pytest.raises(CurrencyMismatch, match="compare USD and EUR"):
        compare(Money(1000, "EUR"), Money(1000, "USD"))


def test_sorting_orders_by_amount() -> None:
    amounts = [Money(300, "EUR"), Money(100, "EUR"), Money(200, "EUR")]

    assert [money.amount_minor for money in sorted(amounts)] == [100, 200, 300]
