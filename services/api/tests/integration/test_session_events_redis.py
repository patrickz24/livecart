"""The stream of a session's events on the SQL snapshot and the Redis stream of compose.

What the worker publishes (through the same ``RedisSessionEvents``) reaches
an open stream after the snapshot; ``Last-Event-ID`` resumes from Redis's own
ids; a stream deleted meanwhile is a gap; another seller gets a 404.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import httpx2
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_api.app import create_app
from livecart_api.sse_testing import EventStream, open_event_stream
from livecart_api.testing import (
    current_time_clock,
    make_settings,
    open_client,
    postgres_resources,
    seller_headers,
)
from livecart_core.application.dto import (
    IntentSource,
    LiveSessionRecord,
    ReservationRequest,
    SessionEvent,
)
from livecart_core.application.session_events import reservation_events
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.quantity import Quantity
from livecart_core.infrastructure.events import RedisSessionEvents
from livecart_core.testing.backends import PostgresBackend
from livecart_core.testing.fakes import FAKE_CLOCK_START

pytestmark = pytest.mark.integration


@dataclass
class Stack:
    app: FastAPI
    client: httpx2.AsyncClient
    backend: PostgresBackend
    events: RedisSessionEvents
    session: LiveSessionRecord

    @property
    def path(self) -> str:
        return f"/api/v1/live-sessions/{self.session.id}/events"

    def owner(self, **extra: str) -> dict[str, str]:
        return {**seller_headers(self.app, self.session.seller_id), **extra}

    async def reserve(self, comment: str, buyer: str) -> list[SessionEvent]:
        """Unit 3 for a comment, then its events, published as the worker does after the commit."""
        comment_id = await self.backend.add_comment(comment, author=buyer)
        result = await self.backend.orders.reserve(
            ReservationRequest(
                comment_id=comment_id,
                session_id=self.session.id,
                product_code=ProductCode("B07"),
                variant=Variant(""),
                quantity=Quantity(1),
                source=IntentSource.PARSER,
                confidence=Decimal("1.00"),
                evidence=("B07",),
                now=FAKE_CLOCK_START,
                reserved_until=FAKE_CLOCK_START + timedelta(seconds=900),
            )
        )
        assert result is not None
        events = reservation_events(
            result, correlation_id=f"correlation-{comment}", occurred_at=FAKE_CLOCK_START
        )
        for event in events:
            await self.events.publish(event)
        return events


@pytest.fixture
async def stack(
    clean_database: None,
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    redis_client: Redis,
    redis_key_prefix: str,
) -> AsyncIterator[Stack]:
    clock = current_time_clock()
    events = RedisSessionEvents(redis_client, prefix=redis_key_prefix)
    app = create_app(
        make_settings(), postgres_resources(app_engine, clock=clock, session_events=events)
    )
    backend = PostgresBackend(app_engine, migrator_engine)
    session = await backend.add_live_session()
    await backend.add_product(session.id, "B07", stock_total=5)
    async with open_client(app) as client:
        yield Stack(app, client, backend, events, session)


async def _messages(stream: EventStream, count: int) -> list[tuple[str | None, object]]:
    frames = [await stream.next_message() for _ in range(count)]
    return [(frame.event, frame.json()) for frame in frames]


async def test_the_snapshot_then_the_events_the_worker_publishes(stack: Stack) -> None:
    await stack.reserve("c-1", "buyer-1")

    async with open_event_stream(stack.app, stack.path, stack.owner()) as stream:
        snapshot = await stream.next_message()
        published = await stack.reserve("c-2", "buyer-2")
        streamed = [await stream.next_message() for _ in published]

    body = snapshot.json()
    assert body["events_cursor"] == snapshot.id
    assert [product["reserved"] for product in body["products"]] == [1]
    assert len(body["orders"]) == len(body["intents"]) == 1
    assert [frame.event for frame in streamed] == [
        "intent.recorded",
        "order.updated",
        "stock.updated",
    ]
    assert [frame.json() for frame in streamed] == [
        event.model_dump(mode="json") for event in published
    ]
    assert streamed[-1].id == await stack.events.cursor(stack.session.id)
    assert streamed[-1].json()["data"]["reserved"] == 2


async def test_last_event_id_resumes_from_the_redis_id(stack: Stack) -> None:
    first = await stack.reserve("c-1", "buyer-1")
    cursor = await stack.events.cursor(stack.session.id)
    second = await stack.reserve("c-2", "buyer-2")

    async with open_event_stream(
        stack.app, stack.path, stack.owner(**{"Last-Event-ID": cursor})
    ) as stream:
        resumed = await _messages(stream, len(second))

    assert [data for _, data in resumed] == [event.model_dump(mode="json") for event in second]
    assert first != second


async def test_a_stream_gone_from_redis_is_a_gap_then_a_snapshot(
    stack: Stack, redis_client: Redis
) -> None:
    await stack.reserve("c-1", "buyer-1")
    cursor = await stack.events.cursor(stack.session.id)
    await redis_client.delete(stack.events.stream_key(stack.session.id))

    async with open_event_stream(
        stack.app, stack.path, stack.owner(**{"Last-Event-ID": cursor})
    ) as stream:
        (gap, gap_data), (snapshot, snapshot_data) = await _messages(stream, 2)

    assert gap == "stream.gap"
    assert isinstance(gap_data, dict)
    assert (gap_data["session_id"], gap_data["data"]) == (str(stack.session.id), {"after": cursor})
    assert snapshot == "snapshot"
    assert isinstance(snapshot_data, dict)
    assert len(snapshot_data["orders"]) == 1


async def test_another_seller_gets_the_404_of_an_unknown_session(stack: Stack) -> None:
    stranger: UUID = await stack.backend.add_seller()

    foreign = await stack.client.get(stack.path, headers=seller_headers(stack.app, stranger))
    unknown = await stack.client.get(
        f"/api/v1/live-sessions/{UUID(int=404)}/events", headers=stack.owner()
    )

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json() == {"detail": "Not found"}
