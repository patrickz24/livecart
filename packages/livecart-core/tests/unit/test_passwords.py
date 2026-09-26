"""The argon2id adapter of ``PasswordHasher``: real hashes, then a stub that records its calls."""

import asyncio
import threading
import time

import pytest
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from livecart_core.infrastructure.passwords import MAX_CONCURRENT_HASHES, Argon2PasswordHasher

PASSWORD = "correct horse battery staple"


async def test_a_password_is_hashed_with_argon2id_default_parameters() -> None:
    hasher = Argon2PasswordHasher()

    stored = await hasher.hash(PASSWORD)

    defaults = PasswordHasher()
    assert stored.startswith(
        f"$argon2id$v=19$m={defaults.memory_cost},t={defaults.time_cost},p={defaults.parallelism}$"
    )
    assert (defaults.time_cost, defaults.memory_cost, defaults.parallelism) == (3, 65_536, 4)
    assert await hasher.hash(PASSWORD) != stored  # a new salt each time
    assert await hasher.verify(stored, PASSWORD) is True
    assert await hasher.verify(stored, PASSWORD + "!") is False


@pytest.mark.parametrize("stored", [None, "!", "", "$argon2id$not-a-hash"])
async def test_no_account_or_a_locked_one_never_verifies(stored: str | None) -> None:
    assert await Argon2PasswordHasher().verify(stored, PASSWORD) is False


class RecordingArgon2:
    """Stands for ``argon2.PasswordHasher``: slow on purpose, and it records every call."""

    def __init__(self, *, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.verified: list[str] = []
        self.threads: set[int] = set()
        self.running = 0
        self.most_running = 0
        self._lock = threading.Lock()

    def hash(self, password: str | bytes, *, salt: bytes | None = None) -> str:
        return f"stub${password!s}"

    def verify(self, hash: str | bytes, password: str | bytes) -> bool:
        with self._lock:
            self.verified.append(str(hash))
            self.threads.add(threading.get_ident())
            self.running += 1
            self.most_running = max(self.most_running, self.running)
        try:
            time.sleep(self.delay_s)
            if not str(hash).startswith("stub$"):
                raise InvalidHashError("not a stub hash")
            if str(hash) != f"stub${password!s}":
                raise VerifyMismatchError("mismatch")
            return True
        finally:
            with self._lock:
                self.running -= 1


async def test_an_unknown_or_locked_account_costs_one_check_against_the_dummy() -> None:
    stub = RecordingArgon2()
    hasher = Argon2PasswordHasher(stub)

    assert await hasher.verify(None, PASSWORD) is False
    assert await hasher.verify("!", PASSWORD) is False
    assert await hasher.verify("stub$other password", PASSWORD) is False

    # One check per call, but a locked hash fails at parsing, so the dummy follows it.
    dummy, locked, dummy_again, wrong = stub.verified
    # The dummy is built once, when the hasher is, from a random password nobody knows.
    assert dummy.startswith("stub$")
    assert dummy == dummy_again
    assert dummy not in (f"stub${PASSWORD}", "stub$other password")
    assert (locked, wrong) == ("!", "stub$other password")


async def test_hashes_run_off_the_event_loop_at_most_four_at_a_time() -> None:
    stub = RecordingArgon2(delay_s=0.05)
    hasher = Argon2PasswordHasher(stub)

    results = await asyncio.gather(*(hasher.verify(f"stub${n}", str(n)) for n in range(12)))

    assert results == [True] * 12
    assert stub.most_running == MAX_CONCURRENT_HASHES == 4
    assert threading.get_ident() not in stub.threads


def test_the_concurrency_bound_is_at_least_one() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        Argon2PasswordHasher(RecordingArgon2(), max_concurrency=0)
