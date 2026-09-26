"""Posts batches of comments to the signed webhook, as a live platform would.

``POST {api}/api/v1/webhooks/comments/simulator`` with a ``CommentBatch`` body,
signed with ``X-LiveCart-Signature: t=<unix time>,v1=<HMAC-SHA256 of
"<t>.<body>">`` under ``COMMENT_WEBHOOK_SECRET_SIMULATOR``
(``livecart_core.signing``), the very scheme the API's adapter checks. What
each answer means for the batch:

- **202**: queued; the workers take it from there;
- **503** (backpressure, a Redis failure) and **429** (the platform's rate
  limit): wait ``Retry-After`` and post the same batch again. Some of it may
  be queued already: its comment ids are the same, so the inbox counts each
  comment once, which is how a platform's retries behave;
- a transport error (the API not up yet, a restart): the same, after a
  delay that doubles up to 30 s;
- any other answer (401 for a wrong secret, 422, 404 for a platform the API
  does not serve) would come back identical: the batch is dropped and the
  error logged.

Each attempt is signed again, so that a batch retried for minutes never
carries a signature past the API's 300 s tolerance.
"""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from typing import Final

import httpx2
import structlog
from pydantic import SecretStr

from livecart_core.application.dto import CommentBatch, IncomingComment, Platform
from livecart_core.signing import SIGNATURE_HEADER, WebhookSigner

logger = structlog.get_logger(__name__)

WEBHOOK_PATH: Final = "/api/v1/webhooks/comments/{platform}"
# The longest wait between two attempts, whatever Retry-After asks.
MAX_RETRY_DELAY_S: Final = 30.0
# The first delay after a transport error, doubled at each one after it.
FIRST_RETRY_DELAY_S: Final = 1.0
REQUEST_TIMEOUT_S: Final = 10.0
RETRIED_STATUSES: Final = frozenset({429, 502, 503, 504})


class Delivery(StrEnum):
    """What became of a batch."""

    ACCEPTED = "accepted"
    # Refused for good: posting it again would get the same answer.
    REFUSED = "refused"
    # The simulator was stopped while the batch waited for another attempt.
    ABANDONED = "abandoned"


def retry_after(response: httpx2.Response) -> float:
    """The wait a 429 or a 503 asks for, in seconds: its ``Retry-After``, 1 s without one."""
    value = response.headers.get("retry-after", "")
    try:
        seconds = float(value)
    except ValueError:
        seconds = FIRST_RETRY_DELAY_S
    return min(max(seconds, 0.0), MAX_RETRY_DELAY_S)


class WebhookPoster:
    """Posts signed batches through ``client``, based at the API's URL; see the module docstring.

    ``wall_clock`` dates the signatures (Unix time); ``sleep`` waits between
    attempts. The tests replace both.
    """

    def __init__(
        self,
        client: httpx2.AsyncClient,
        secret: SecretStr,
        *,
        platform: Platform = Platform.SIMULATOR,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._signer = WebhookSigner(secret)
        self._path = WEBHOOK_PATH.format(platform=platform.value)
        self._wall_clock = wall_clock
        self._sleep = sleep

    async def post(self, comments: Sequence[IncomingComment], *, stop: asyncio.Event) -> Delivery:
        """Post one batch until it is accepted or refused, or until ``stop`` is set."""
        body = CommentBatch(comments=list(comments)).model_dump_json().encode()
        delay_s = FIRST_RETRY_DELAY_S
        while not stop.is_set():
            headers = {
                SIGNATURE_HEADER: self._signer.header(body, timestamp=int(self._wall_clock())),
                "Content-Type": "application/json",
            }
            try:
                response = await self._client.post(self._path, content=body, headers=headers)
            except httpx2.TransportError as error:
                logger.warning(
                    "webhook_unreachable", error=type(error).__name__, retry_in_s=delay_s
                )
                await self._wait(delay_s, stop)
                delay_s = min(delay_s * 2, MAX_RETRY_DELAY_S)
                continue
            if response.status_code == 202:
                return Delivery.ACCEPTED
            if response.status_code in RETRIED_STATUSES:
                wait_s = retry_after(response)
                logger.warning(
                    "webhook_busy",
                    status=response.status_code,
                    retry_in_s=wait_s,
                    size=len(comments),
                )
                await self._wait(wait_s, stop)
                continue
            # The body of a refusal is a fixed message, never an echo of the batch.
            logger.error(
                "webhook_refused_batch",
                status=response.status_code,
                detail=_detail(response),
                size=len(comments),
            )
            return Delivery.REFUSED
        return Delivery.ABANDONED

    async def _wait(self, seconds: float, stop: asyncio.Event) -> None:
        if self._sleep is not None:
            await self._sleep(seconds)
            return
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await stop.wait()


def _detail(response: httpx2.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return detail if isinstance(detail, str) else None
