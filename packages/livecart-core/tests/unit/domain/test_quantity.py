from typing import cast

import pytest

from livecart_core.domain.errors import DomainError, QuantityOutOfBounds
from livecart_core.domain.quantity import QUANTITY_CEILING, Quantity, parse_quantity


def test_the_ceiling_is_the_database_check_bound() -> None:
    assert QUANTITY_CEILING == 99


@pytest.mark.parametrize("value", [1, 2, 10, 99])
def test_a_quantity_is_an_int_from_one_to_the_ceiling(value: int) -> None:
    assert Quantity(value).value == value


@pytest.mark.parametrize("value", [0, -1, 100, 1000])
def test_a_quantity_outside_the_ceiling_is_rejected(value: int) -> None:
    with pytest.raises(QuantityOutOfBounds, match="from 1 to 99"):
        Quantity(value)


@pytest.mark.parametrize("value", [2.0, True, "2"], ids=["float", "bool", "str"])
def test_a_quantity_must_be_a_real_int(value: object) -> None:
    # The cast lets the test reach the runtime guard mypy would otherwise stop it at.
    with pytest.raises(QuantityOutOfBounds):
        Quantity(cast(int, value))


def test_quantities_are_values() -> None:
    assert Quantity(2) == Quantity(2)
    assert Quantity(2) != Quantity(3)
    assert len({Quantity(2), Quantity(2)}) == 1


@pytest.mark.parametrize("value", [1, 5, 10])
def test_parsing_accepts_one_to_the_business_bound(value: int) -> None:
    assert parse_quantity(value, max_quantity=10) == Quantity(value)


@pytest.mark.parametrize("value", [0, -3, 11, 99, 100])
def test_parsing_rejects_outside_the_business_bound(value: int) -> None:
    with pytest.raises(QuantityOutOfBounds, match="from 1 to 10") as excinfo:
        parse_quantity(value, max_quantity=10)

    assert isinstance(excinfo.value, DomainError)


def test_parsing_rejects_a_bool_or_a_float_as_a_quantity() -> None:
    with pytest.raises(QuantityOutOfBounds):
        parse_quantity(cast(int, True), max_quantity=10)
    with pytest.raises(QuantityOutOfBounds):
        parse_quantity(cast(int, 2.0), max_quantity=10)


def test_the_business_bound_may_reach_the_ceiling() -> None:
    assert parse_quantity(99, max_quantity=QUANTITY_CEILING) == Quantity(99)
    assert parse_quantity(1, max_quantity=1) == Quantity(1)


@pytest.mark.parametrize("max_quantity", [0, -1, 100])
def test_a_business_bound_outside_the_ceiling_is_a_caller_error(max_quantity: int) -> None:
    with pytest.raises(ValueError, match="max_quantity must be from 1 to 99"):
        parse_quantity(1, max_quantity=max_quantity)
