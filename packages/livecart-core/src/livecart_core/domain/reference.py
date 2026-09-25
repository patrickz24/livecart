"""Order references: ``LC-`` then six symbols of Crockford's base 32.

The alphabet is ``0-9`` and ``A-Z`` without ``I``, ``L``, ``O`` and ``U``
(32 symbols): those four are what people misread, and a reference is read
aloud on a live stream, typed on a phone and matched against the seller's
records. Six symbols give 32**6 = 1,073,741,824 (2**30) references, drawn
uniformly by one ``randbelow`` call on a cryptographic source, so that a
reference cannot be guessed from the previous one. With a million orders in
the table, a fresh draw collides about once in a thousand times: the
persistence layer (milestone 3) retries a colliding insert, and no collision
handling lives here. The random source is injected so that tests can force a
collision or fix the outcome.

Reading is tolerant, in the spirit of the encoding: case does not matter, the
hyphen is optional, ``O`` reads as ``0`` and ``I`` and ``L`` read as ``1``.
Anything else (a ``U``, a seventh symbol, another prefix) is an
``InvalidOrderReference``.
"""

import re
import secrets
from dataclasses import dataclass
from typing import Final, Protocol

from livecart_core.domain.errors import InvalidOrderReference
from livecart_core.domain.text import normalize_text

CROCKFORD_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
REFERENCE_PREFIX: Final = "LC-"
REFERENCE_SYMBOLS: Final = 6
# The number of distinct references: 32**6.
REFERENCE_SPACE: Final = len(CROCKFORD_ALPHABET) ** REFERENCE_SYMBOLS

_CANONICAL: Final = re.compile(rf"LC-[{CROCKFORD_ALPHABET}]{{{REFERENCE_SYMBOLS}}}")
_TOLERANT: Final = re.compile(rf"LC-?([0-9A-Z]{{{REFERENCE_SYMBOLS}}})")
_MISREAD: Final = str.maketrans({"O": "0", "I": "1", "L": "1"})


class RandomSource(Protocol):
    """A uniform random integer in ``range(exclusive_upper_bound)``.

    The ``secrets`` module satisfies it as is; tests pass a scripted source.
    """

    def randbelow(self, exclusive_upper_bound: int, /) -> int: ...


@dataclass(frozen=True, slots=True)
class OrderReference:
    value: str

    def __post_init__(self) -> None:
        if not _CANONICAL.fullmatch(self.value):
            raise InvalidOrderReference(
                f"{self.value!r} is not a canonical order reference (LC- then six symbols)"
            )

    def __str__(self) -> str:
        return self.value


def encode_reference(index: int) -> OrderReference:
    """The reference at one point of the space: 0 gives ``LC-000000``, 32**6 - 1 ``LC-ZZZZZZ``."""
    if not 0 <= index < REFERENCE_SPACE:
        raise ValueError(f"index must be from 0 to {REFERENCE_SPACE - 1}, not {index}")
    symbols = []
    for _ in range(REFERENCE_SYMBOLS):
        index, digit = divmod(index, len(CROCKFORD_ALPHABET))
        symbols.append(CROCKFORD_ALPHABET[digit])
    return OrderReference(REFERENCE_PREFIX + "".join(reversed(symbols)))


def generate_order_reference(random_source: RandomSource = secrets) -> OrderReference:
    """Draw one reference uniformly from the whole space."""
    return encode_reference(random_source.randbelow(REFERENCE_SPACE))


def parse_order_reference(text: str) -> OrderReference:
    """Read a reference as a buyer typed it, and return its canonical form."""
    match = _TOLERANT.fullmatch(normalize_text(text).upper())
    if match is None:
        raise InvalidOrderReference(f"{text!r} is not an order reference (LC- then six symbols)")
    body = match.group(1).translate(_MISREAD)
    if any(symbol not in CROCKFORD_ALPHABET for symbol in body):
        raise InvalidOrderReference(f"{text!r} holds a symbol outside the reference alphabet")
    return OrderReference(REFERENCE_PREFIX + body)
