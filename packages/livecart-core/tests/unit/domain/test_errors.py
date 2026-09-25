import pytest

from livecart_core.domain.errors import (
    CurrencyMismatch,
    DomainError,
    InvalidOrderReference,
    InvalidProductCode,
    InvalidValue,
    QuantityOutOfBounds,
)

ALL_ERRORS = [
    InvalidValue,
    InvalidProductCode,
    InvalidOrderReference,
    QuantityOutOfBounds,
    CurrencyMismatch,
]


@pytest.mark.parametrize("error", ALL_ERRORS)
def test_every_domain_error_can_be_caught_as_a_domain_error(error: type[DomainError]) -> None:
    with pytest.raises(DomainError):
        raise error("boom")


@pytest.mark.parametrize(
    "error", [InvalidValue, InvalidProductCode, InvalidOrderReference, QuantityOutOfBounds]
)
def test_bad_input_errors_are_value_errors_for_pydantic_validators(
    error: type[InvalidValue],
) -> None:
    assert issubclass(error, ValueError)


def test_a_currency_mismatch_is_a_programming_error_not_bad_input() -> None:
    assert not issubclass(CurrencyMismatch, ValueError)
