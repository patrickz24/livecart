"""Requests made safe to repeat by an idempotency key: the first answer is replayed for a day.

A client that did not get an answer (a timeout, a dropped connection)
repeats its request with the same ``Idempotency-Key``; the effect must happen
once. ``IdempotentRequests.run``:

1. claims the key in the store (``SET NX EX 86400`` in Redis), with the
   fingerprint of the request, the SHA-256 of its canonical JSON;
2. if another request claimed it: a different fingerprint is
   ``IdempotencyKeyReused`` (the key was reused for another request, 422); no
   answer yet is ``IdempotentRequestInFlight`` (409, try again shortly);
   otherwise its answer is replayed;
3. otherwise runs the action and stores its answer in place of the claim,
   whatever it is: a refusal such as 404 or 409 is replayed as faithfully as
   a success, so a repeated request never runs against a newer state.

An action that raises (the database unavailable, a bug) answered nothing:
the claim is released, and the same key may run again. The one case this
cannot tell apart is a connection lost during the ``COMMIT`` itself, which
may have committed: the retry then runs the effect again. A process that
dies between the effect and the answer leaves its claim until it expires,
and the key answers ``IdempotentRequestInFlight`` meanwhile: safe, since
nothing runs twice. Storing the record in the transaction of the effect
would close both gaps; it is on the roadmap.
"""

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from pydantic import JsonValue

from livecart_core.application.dto import IdempotencyKey, IdempotencyRecord, StoredResponse
from livecart_core.application.ports import IdempotencyStore


class IdempotencyKeyReused(Exception):
    """The key names another request of the same seller: a client bug, answered with 422."""


class IdempotentRequestInFlight(Exception):
    """The request that claimed the key has not answered yet: answered with 409."""


def fingerprint(request: Mapping[str, JsonValue]) -> str:
    """The SHA-256 of the request's canonical JSON: sorted keys, no spaces, UTF-8."""
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class IdempotentAnswer:
    """The answer to give, and whether it replays the first request's."""

    response: StoredResponse
    replayed: bool


class IdempotentRequests:
    """Runs a request once per idempotency key; see the module docstring."""

    def __init__(self, store: IdempotencyStore) -> None:
        self._store = store

    async def run(
        self,
        key: IdempotencyKey,
        request: Mapping[str, JsonValue],
        action: Callable[[], Awaitable[StoredResponse]],
    ) -> IdempotentAnswer:
        digest = fingerprint(request)
        first = await self._store.claim(key, digest)
        if first is not None:
            if first.fingerprint != digest:
                raise IdempotencyKeyReused("this key was used for another request")
            if first.response is None:
                raise IdempotentRequestInFlight("the request of this key has not answered yet")
            return IdempotentAnswer(first.response, replayed=True)
        try:
            response = await action()
        except Exception:
            # Nothing answered: the key may run again. A cancellation (BaseException)
            # keeps the claim instead, since the effect may have committed.
            await self._store.release(key)
            raise
        await self._store.save(key, IdempotencyRecord(fingerprint=digest, response=response))
        return IdempotentAnswer(response, replayed=False)
