"""A scripted random source for tests, in place of the ``secrets`` module."""

from collections.abc import Iterable


class SequenceRandomSource:
    """Plays back ``values`` one per ``randbelow`` call, and fails once they run out.

    Running out is a failure rather than a wrap-around, so that a test which
    scripts two draws notices a third. Each value must be below the bound asked
    for, as a real source's would be. ``requested_bounds`` records the bound of
    every call, for a test to check the size of the space that was drawn from.
    """

    def __init__(self, values: Iterable[int]) -> None:
        self._values = iter(values)
        self.requested_bounds: list[int] = []

    def randbelow(self, exclusive_upper_bound: int, /) -> int:
        self.requested_bounds.append(exclusive_upper_bound)
        try:
            value = next(self._values)
        except StopIteration:
            raise AssertionError("SequenceRandomSource has no scripted value left") from None
        if not 0 <= value < exclusive_upper_bound:
            raise AssertionError(f"scripted value {value} is not below {exclusive_upper_bound}")
        return value
