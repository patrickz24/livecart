"""The ``PasswordHasher`` port with argon2-cffi: argon2id, off the event loop, four at a time.

- argon2id with argon2-cffi's default parameters (RFC 9106's low-memory
  profile: t=3, 64 MiB, p=4). The parameters live in each PHC string, so a
  later change of defaults keeps verifying the hashes already stored.
- Each hash costs tens of milliseconds of CPU and 64 MiB of memory: it runs in
  a worker thread, so the event loop keeps serving other requests, and at most
  ``MAX_CONCURRENT_HASHES`` (4) run at once, so a burst of logins queues rather
  than taking 64 MiB each out of the API container's 512 MiB.
- ``verify(None, ...)``, an e-mail no seller has, checks the password against
  a dummy hash made with the same parameters when the hasher is built, and so
  costs what a wrong password costs. A stored value that is no argon2 hash (a
  locked account) is answered after the same dummy check.
"""

import asyncio
import contextlib
import secrets
from typing import Final, Protocol

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MAX_CONCURRENT_HASHES: Final = 4


class Argon2Api(Protocol):
    """The two calls of ``argon2.PasswordHasher`` this adapter makes, so a test can slow them."""

    def hash(self, password: str | bytes, *, salt: bytes | None = None) -> str: ...

    def verify(self, hash: str | bytes, password: str | bytes) -> bool: ...


class Argon2PasswordHasher:
    """argon2id password hashes; see the module docstring."""

    def __init__(
        self,
        hasher: Argon2Api | None = None,
        *,
        max_concurrency: int = MAX_CONCURRENT_HASHES,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError(f"max_concurrency must be at least 1, not {max_concurrency}")
        self._hasher: Argon2Api = hasher if hasher is not None else PasswordHasher()
        self._slots = asyncio.Semaphore(max_concurrency)
        # Made once, from a password nobody knows, with the parameters of every new hash.
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(32))

    async def hash(self, password: str) -> str:
        async with self._slots:
            return await asyncio.to_thread(self._hasher.hash, password)

    async def verify(self, password_hash: str | None, password: str) -> bool:
        async with self._slots:
            return await asyncio.to_thread(self._verify, password_hash, password)

    def _verify(self, password_hash: str | None, password: str) -> bool:
        if password_hash is not None:
            with contextlib.suppress(InvalidHashError):
                return self._matches(password_hash, password)
        # No account, or a locked one: the same work as a wrong password, then no.
        self._matches(self._dummy_hash, password)
        return False

    def _matches(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except VerificationError:
            # VerifyMismatchError, a wrong password, is the common case.
            return False
