"""Errors raised by the domain layer.

Every domain error derives from ``DomainError``, so that an outer layer can
catch the whole family at once. The errors about bad input also derive from
``ValueError``: a pydantic validator that builds a value object then reports
the failure as an ordinary validation error, without a wrapper of its own.
"""

from enum import StrEnum


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


class InvalidTransition(DomainError):
    """A status change that the transition table of its aggregate does not allow."""

    def __init__(self, current: StrEnum, target: StrEnum) -> None:
        super().__init__(f"{type(current).__name__}: {current} -> {target} is not allowed")
        self.current = current
        self.target = target


class EditRefused(DomainError):
    """A change to a live session or to its catalog that the session's status no longer allows.

    ``keys`` says whether the change touched the keys comments are matched by
    (the session's platform and live id, a product's code and variant).
    """

    def __init__(self, status: StrEnum, *, keys: bool) -> None:
        what = "its platform, live id and product codes" if keys else "anything"
        super().__init__(f"the session is {status}: it can no longer change {what}")
        self.status = status
        self.keys = keys
