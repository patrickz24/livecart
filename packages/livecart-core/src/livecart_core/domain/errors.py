"""Errors raised by the domain layer.

Every domain error derives from ``DomainError``, so that an outer layer can
catch the whole family at once. The errors about bad input also derive from
``ValueError``: a pydantic validator that builds a value object then reports
the failure as an ordinary validation error, without a wrapper of its own.
"""


class DomainError(Exception):
    """Base of every error raised by ``livecart_core.domain``."""


class InvalidValue(DomainError, ValueError):
    """Input that breaks the invariant of a value object."""


class InvalidProductCode(InvalidValue):
    """Text that is not a product code, even after normalisation."""


class InvalidOrderReference(InvalidValue):
    """Text that is not an order reference, even after tolerant reading."""


class QuantityOutOfBounds(InvalidValue):
    """A purchase quantity below 1 or above the bound in force."""


class CurrencyMismatch(DomainError):
    """Arithmetic or ordering between two currencies: a programming error, not bad input."""
