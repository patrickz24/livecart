"""The webhook poster on ``httpx2.MockTransport``: signed as the API checks, retried as a platform.

The signature is verified by the API's own adapter, so a change of scheme on
either side fails here, not in the demo.
"""

import asyncio
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import httpx2
import pytest
from pydantic import SecretStr

from livecart_core.application.dto import CommentBatch
from livecart_core.application.ports import InvalidSignature
from livecart_core.infrastructure.platforms.simulator import SimulatorPlatformAdapter
from livecart_core.testing.builders import incoming_comment
from livecart_core.testing.fakes import FAKE_CLOCK_START, FakeClock
from livecart_core.testing.keys import TEST_COMMENT_WEBHOOK_SECRET
from livecart_simulator.poster import (
    FIRST_RETRY_DELAY_S,
    MAX_RETRY_DELAY_S,
    Delivery,
    WebhookPoster,
    retry_after,
)

COMMENTS = [
    incoming_comment("je prends A12 M", external_comment_id="sim-1"),
    incoming_comment("SOLD B07 x2", external_comment_id="sim-2", author="buyer-2"),
]
WEBHOOK_URL = "http://api.test/api/v1/webhooks/comments/simulator"

type Answer = Callable[[httpx2.Request], httpx2.Response]


class Api:
    """Answers each request with the next of ``answers``, recording what it received."""

    def __init__(self, answers: Sequence[Answer]) -> None:
        self._answers = deque(answers)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self._answers.popleft()(request)


def status(code: int, **headers: str) -> Answer:
    return lambda _request: httpx2.Response(code, headers=headers, json={"detail": "Refused"})


def unreachable(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("connection refused", request=request)


@dataclass
class Sleeps:
    waits: list[float] = field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


async def _post(api: Api, *, stop: asyncio.Event | None = None) -> tuple[Delivery, Sleeps]:
    sleeps = Sleeps()
    transport = httpx2.MockTransport(api)
    async with httpx2.AsyncClient(transport=transport, base_url="http://api.test") as client:
        poster = WebhookPoster(
            client,
            TEST_COMMENT_WEBHOOK_SECRET,
            wall_clock=lambda: FAKE_CLOCK_START.timestamp(),
            sleep=sleeps,
        )
        delivery = await poster.post(COMMENTS, stop=stop or asyncio.Event())
    return delivery, sleeps


def _accepted(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(202, json={"accepted": 2})


async def test_a_batch_is_signed_as_the_api_adapter_checks_it() -> None:
    api = Api([_accepted])

    delivery, _ = await _post(api)

    assert delivery is Delivery.ACCEPTED
    [request] = api.requests
    assert str(request.url) == WEBHOOK_URL
    assert request.headers["content-type"] == "application/json"
    adapter = SimulatorPlatformAdapter(TEST_COMMENT_WEBHOOK_SECRET, FakeClock())
    adapter.verify(dict(request.headers), request.content)
    assert adapter.parse(request.content) == COMMENTS


async def test_another_secret_does_not_verify() -> None:
    api = Api([_accepted])
    await _post(api)

    adapter = SimulatorPlatformAdapter(SecretStr("another-webhook-key-" * 2), FakeClock())
    with pytest.raises(InvalidSignature):
        adapter.verify(dict(api.requests[0].headers), api.requests[0].content)


@pytest.mark.parametrize("busy", [503, 429])
async def test_a_busy_api_gets_the_same_batch_after_retry_after(busy: int) -> None:
    api = Api([status(busy, **{"Retry-After": "5"}), status(busy), _accepted])

    delivery, sleeps = await _post(api)

    assert delivery is Delivery.ACCEPTED
    assert sleeps.waits == [5.0, FIRST_RETRY_DELAY_S]
    bodies = [CommentBatch.model_validate_json(request.content) for request in api.requests]
    assert {tuple(body.comments) for body in bodies} == {tuple(COMMENTS)}


async def test_an_unreachable_api_is_retried_after_doubling_delays() -> None:
    api = Api([unreachable] * 7 + [_accepted])

    delivery, sleeps = await _post(api)

    assert delivery is Delivery.ACCEPTED
    assert sleeps.waits == [1.0, 2.0, 4.0, 8.0, 16.0, MAX_RETRY_DELAY_S, MAX_RETRY_DELAY_S]


@pytest.mark.parametrize("refusal", [401, 404, 413, 422])
async def test_a_refusal_drops_the_batch_at_once(refusal: int) -> None:
    api = Api([status(refusal)])

    delivery, sleeps = await _post(api)

    assert delivery is Delivery.REFUSED
    assert len(api.requests) == 1
    assert sleeps.waits == []


async def test_a_refusal_without_a_json_body_is_dropped_too() -> None:
    api = Api([lambda _request: httpx2.Response(400, text="Bad Request")])

    delivery, _ = await _post(api)

    assert delivery is Delivery.REFUSED


async def test_a_stopped_poster_abandons_its_batch() -> None:
    stop = asyncio.Event()
    stop.set()
    api = Api([])

    delivery, _ = await _post(api, stop=stop)

    assert delivery is Delivery.ABANDONED
    assert api.requests == []


async def test_without_a_sleep_the_wait_ends_when_stopped() -> None:
    stop = asyncio.Event()

    def busy_then_stop(request: httpx2.Request) -> httpx2.Response:
        # The stop comes while the poster waits for the 30 s it was asked to.
        asyncio.get_running_loop().call_later(0.05, stop.set)
        return httpx2.Response(503, headers={"Retry-After": "30"})

    transport = httpx2.MockTransport(Api([busy_then_stop]))
    async with httpx2.AsyncClient(transport=transport, base_url="http://api.test") as client:
        poster = WebhookPoster(client, TEST_COMMENT_WEBHOOK_SECRET)
        delivery = await asyncio.wait_for(poster.post(COMMENTS, stop=stop), timeout=2)

    assert delivery is Delivery.ABANDONED


@pytest.mark.parametrize(
    ("header", "seconds"),
    [(None, 1.0), ("5", 5.0), ("0.5", 0.5), ("later", 1.0), ("-3", 0.0), ("3600", 30.0)],
)
def test_retry_after_is_read_within_bounds(header: str | None, seconds: float) -> None:
    headers = {} if header is None else {"Retry-After": header}

    assert retry_after(httpx2.Response(503, headers=headers)) == seconds
