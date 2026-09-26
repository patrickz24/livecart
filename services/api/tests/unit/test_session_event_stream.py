"""``GET /api/v1/live-sessions/{id}/events`` over the fakes, read frame by frame as it streams.

The snapshot and its cursor, the events after it, ``Last-Event-ID``, the gap,
the close before the token expires, the keep-alive comment, the cap of
streams per instance, and the answers before any stream: 401, 404, 422.
"""

from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

import fastapi.routing
import pytest
import structlog
from fastapi import FastAPI
from prometheus_client import CollectorRegistry

from livecart_api.access_tokens import AccessTokens
from livecart_api.routes.session_events import (
    CLOSE_EVENT,
    GAP_EVENT,
    RECONNECT_AFTER_MS,
    SELLER_STREAMS_FULL,
    SNAPSHOT_EVENT,
    STREAMS_FULL,
    StreamRefusal,
    StreamSlots,
)
from livecart_api.sse_testing import EventStream, StreamEnded, open_event_stream, parse_frame
from livecart_api.testing import (
    current_time_clock,
    make_app,
    make_settings,
    open_client,
    open_order,
    seller_headers,
)
from livecart_core.application.dto import (
    LiveSessionRecord,
    SessionEvent,
    SessionEventType,
    StockLevel,
)
from livecart_core.application.ports import START_CURSOR, Clock
from livecart_core.application.session_events import stock_updated
from livecart_core.domain.state_machines import LiveSessionStatus
from livecart_core.infrastructure.clock import SystemClock
from livecart_core.testing.fakes import FakeClock, InMemoryDatabase, InMemorySessionEvents
from livecart_core.testing.keys import TEST_JWT_SECRET
from livecart_core.testing.polling import until


@dataclass
class Live:
    app: FastAPI
    database: InMemoryDatabase
    clock: Clock
    events: InMemorySessionEvents
    session: LiveSessionRecord

    @property
    def path(self) -> str:
        return f"/api/v1/live-sessions/{self.session.id}/events"

    def headers(self, **extra: str) -> dict[str, str]:
        return {**seller_headers(self.app, self.session.seller_id), **extra}

    async def publish(self, available: int) -> SessionEvent:
        product = self.database.tables.products[next(iter(self.database.tables.products))]
        event = stock_updated(
            self.session.id,
            product.id,
            StockLevel(total=10, reserved=10 - available),
            correlation_id=f"correlation-{available}",
            occurred_at=self.clock.now(),
        )
        await self.events.publish(event)
        return event


def _live(*, clock: Clock | None = None, max_streams: int = 200, per_seller: int = 5) -> Live:
    clock = clock if clock is not None else current_time_clock()
    database = InMemoryDatabase()
    session = database.add_live_session()
    database.add_product(session.id, "B07", stock_total=10)
    events = InMemorySessionEvents(clock if isinstance(clock, FakeClock) else FakeClock())
    app = make_app(
        database=database,
        clock=clock,
        session_events=events,
        settings=make_settings(sse_max_streams=max_streams, sse_max_streams_per_seller=per_seller),
    )
    return Live(app, database, clock, events, session)


async def _snapshot(stream: EventStream) -> dict[str, object]:
    frame = await stream.next_message()
    assert frame.event == SNAPSHOT_EVENT
    body = frame.json()
    assert isinstance(body, dict)
    return body


async def test_the_stream_sends_the_snapshot_then_every_event_after_its_cursor() -> None:
    live = _live()
    order = await open_order(live.database, live.session.id)
    before = await live.publish(9)

    async with open_event_stream(live.app, live.path, live.headers()) as stream:
        assert stream.status == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert stream.headers["cache-control"] == "no-cache"
        assert stream.headers["x-accel-buffering"] == "no"
        snapshot_frame = await stream.next_message()
        after = await live.publish(8)
        streamed = await stream.next_message()

    snapshot = snapshot_frame.json()
    cursor = await live.events.cursor(live.session.id)
    # The cursor was read before the snapshot: the event before it is not sent again.
    assert snapshot_frame.id == snapshot["events_cursor"] != cursor
    assert snapshot["session"]["id"] == str(live.session.id)
    assert [product["code"] for product in snapshot["products"]] == ["B07"]
    assert [item["order_id"] for item in snapshot["orders"]] == [str(order.id)]
    assert snapshot["orders"][0]["reference"] == order.reference.value
    assert snapshot["intents"][0]["outcome"] == "reserved"
    assert snapshot["limit"] == 200
    assert streamed.event == "stock.updated"
    assert streamed.id == cursor
    assert streamed.json() == after.model_dump(mode="json")
    assert streamed.json() != before.model_dump(mode="json")


async def test_last_event_id_resumes_after_it_without_a_snapshot() -> None:
    live = _live()
    await live.publish(9)
    cursor = await live.events.cursor(live.session.id)
    missed = await live.publish(8)

    async with open_event_stream(
        live.app, live.path, live.headers(**{"Last-Event-ID": cursor})
    ) as stream:
        resumed = await stream.next_message()

    assert resumed.event == "stock.updated"
    assert resumed.json() == missed.model_dump(mode="json")


async def test_a_cursor_whose_events_are_gone_gets_a_gap_then_a_new_snapshot() -> None:
    live = _live()
    await live.publish(9)
    cursor = await live.events.cursor(live.session.id)
    live.events.expire(live.session.id)

    async with open_event_stream(
        live.app, live.path, live.headers(**{"Last-Event-ID": cursor})
    ) as stream:
        gap = await stream.next_message()
        snapshot = await _snapshot(stream)

    assert gap.event == GAP_EVENT
    assert gap.id is None
    assert gap.json()["type"] == GAP_EVENT
    assert gap.json()["data"] == {"after": cursor}
    assert gap.json()["session_id"] == str(live.session.id)
    assert snapshot["events_cursor"] == START_CURSOR


async def test_the_stream_closes_before_the_access_token_expires() -> None:
    live = _live(clock=SystemClock())
    # A token of 8 s: the stream closes 5 s before it expires, 2 to 3 s from now (whole
    # seconds), time enough for the snapshot even on a loaded machine.
    token = AccessTokens(TEST_JWT_SECRET, live.clock, ttl=timedelta(seconds=8))
    headers = {"Authorization": f"Bearer {token.issue(live.session.seller_id).value}"}

    async with open_event_stream(live.app, live.path, headers) as stream:
        await _snapshot(stream)
        close = await stream.next_message(timeout_s=6)
        with pytest.raises(StreamEnded):
            await stream.next_frame()

    assert close.event == CLOSE_EVENT
    assert close.json()["data"] == {"reason": "token_expiring"}
    assert close.retry == RECONNECT_AFTER_MS


async def test_an_idle_stream_sends_a_keep_alive_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    # FastAPI's own keep-alive, every 15 s in production.
    monkeypatch.setattr(fastapi.routing, "_PING_INTERVAL", 0.05)
    live = _live()

    async with open_event_stream(live.app, live.path, live.headers()) as stream:
        await _snapshot(stream)
        ping = await stream.next_frame()

    assert ping.comments == ("ping",)
    assert not ping.is_message


async def test_another_sellers_session_is_not_found() -> None:
    live = _live()
    stranger = live.database.add_seller()

    async with open_event_stream(
        live.app, live.path, seller_headers(live.app, stranger.id)
    ) as response:
        body = await response.body()

    assert response.status == 404
    assert body == '{"detail":"Not found"}'
    assert _slots(live.app).open == 0


async def test_a_stream_needs_an_access_token() -> None:
    live = _live()

    async with open_event_stream(live.app, live.path, {}) as response:
        await response.body()

    assert response.status == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("cursor", ["latest", "1-2-3", "12", "1" * 21 + "-0"])
async def test_a_malformed_last_event_id_is_refused(cursor: str) -> None:
    live = _live()

    async with open_event_stream(
        live.app, live.path, live.headers(**{"Last-Event-ID": cursor})
    ) as response:
        await response.body()

    assert response.status == 422


def _slots(app: FastAPI) -> StreamSlots:
    slots = app.state.stream_slots
    assert isinstance(slots, StreamSlots)
    return slots


async def test_beyond_its_streams_an_instance_answers_503_until_one_closes() -> None:
    live = _live(max_streams=1)

    async with open_event_stream(live.app, live.path, live.headers()) as first:
        await _snapshot(first)
        async with open_event_stream(live.app, live.path, live.headers()) as refused:
            body = await refused.body()
        assert _slots(live.app).open == 1

    assert refused.status == 503
    assert refused.headers["retry-after"] == "5"
    assert STREAMS_FULL in body
    await _until_released(live.app)
    async with open_event_stream(live.app, live.path, live.headers()) as again:
        await _snapshot(again)
    assert _slots(live.app).refused_total[StreamRefusal.INSTANCE_FULL] == 1


async def _until_released(app: FastAPI) -> None:
    await until(lambda: _slots(app).open == 0)


async def test_beyond_its_own_streams_a_seller_gets_429_and_the_others_still_stream() -> None:
    live = _live(max_streams=10, per_seller=2)
    other_session = live.database.add_live_session(external_live_id="live-2")
    other_path = f"/api/v1/live-sessions/{other_session.id}/events"
    other_headers = seller_headers(live.app, other_session.seller_id)

    async with open_event_stream(live.app, live.path, live.headers()) as first:
        await _snapshot(first)
        async with open_event_stream(live.app, live.path, live.headers()) as second:
            await _snapshot(second)
            async with open_event_stream(live.app, live.path, live.headers()) as refused:
                body = await refused.body()
            async with open_event_stream(live.app, other_path, other_headers) as other:
                await _snapshot(other)
                assert _slots(live.app).open == 3

    assert refused.status == 429
    assert refused.headers["retry-after"] == "5"
    assert SELLER_STREAMS_FULL in body
    await _until_released(live.app)
    async with open_event_stream(live.app, live.path, live.headers()) as again:
        await _snapshot(again)
    registry = live.app.state.metrics_registry
    assert isinstance(registry, CollectorRegistry)
    refusals = {
        reason: registry.get_sample_value(
            "livecart_sse_streams_refused_total", {"reason": reason.value}
        )
        for reason in StreamRefusal
    }
    assert refusals == {StreamRefusal.SELLER_LIMIT: 1, StreamRefusal.INSTANCE_FULL: 0}


async def test_a_closed_stream_releases_its_slot_and_is_left_out_of_the_latency() -> None:
    live = _live()

    async with open_event_stream(live.app, live.path, live.headers()) as stream:
        await _snapshot(stream)
        assert _slots(live.app).open == 1
    await _until_released(live.app)

    registry = live.app.state.metrics_registry
    assert isinstance(registry, CollectorRegistry)
    assert registry.get_sample_value("livecart_sse_streams") == 0
    assert registry.get_sample_value("livecart_sse_streams_opened_total") == 1
    handlers = {
        sample.labels.get("handler")
        for metric in registry.collect()
        for sample in metric.samples
        if metric.name == "http_request_duration_seconds"
    }
    assert "/api/v1/live-sessions/{session_id}/events" not in handlers


def test_frames_are_parsed_with_their_fields_and_comments() -> None:
    frame = parse_frame(': hello\nevent: snapshot\nid: 3-0\nretry: 1000\ndata: {"a":\ndata: 1}')

    assert frame.comments == ("hello",)
    assert (frame.event, frame.id, frame.retry) == ("snapshot", "3-0", 1000)
    assert frame.json() == {"a": 1}
    with pytest.raises(ValueError, match="no data"):
        parse_frame(": ping").json()


def test_a_stream_slot_limit_is_positive() -> None:
    with pytest.raises(ValueError, match="one stream"):
        StreamSlots(0, per_seller=1)
    with pytest.raises(ValueError, match="one of each seller"):
        StreamSlots(1, per_seller=0)


def test_a_seller_releases_only_the_slots_it_holds() -> None:
    slots = StreamSlots(2, per_seller=2)
    seller, other = uuid4(), uuid4()

    assert slots.acquire(seller) is None
    assert slots.acquire(seller) is None
    assert slots.acquire(other) is StreamRefusal.INSTANCE_FULL
    slots.release(seller)

    assert (slots.open, slots.held_by(seller), slots.held_by(other)) == (1, 1, 0)
    with pytest.raises(RuntimeError, match="holds no stream slot"):
        slots.release(other)
    slots.release(seller)
    assert (slots.open, slots.held_by(seller)) == (0, 0)


def test_the_stream_is_documented_as_events_and_its_errors_as_json() -> None:
    operation = make_app().openapi()["paths"]["/api/v1/live-sessions/{session_id}/events"]["get"]

    assert set(operation["responses"]["200"]["content"]) == {"text/event-stream"}
    for code in ("401", "404", "429", "503"):
        content = operation["responses"][code]["content"]
        assert content == {
            "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
        }
    assert [parameter["name"] for parameter in operation["parameters"]] == [
        "session_id",
        "Last-Event-ID",
    ]


async def test_starting_a_session_streams_its_update_and_a_lost_one_is_counted() -> None:
    clock = current_time_clock()
    database = InMemoryDatabase()
    session = database.add_live_session(status=LiveSessionStatus.DRAFT)
    events = InMemorySessionEvents(clock)
    app = make_app(database=database, clock=clock, session_events=events)
    headers = seller_headers(app, session.seller_id)
    path = f"/api/v1/live-sessions/{session.id}"

    async with (
        open_client(app) as client,
        open_event_stream(app, f"{path}/events", headers) as stream,
    ):
        await _snapshot(stream)
        started = await client.post(f"{path}/start", headers=headers)
        update = await stream.next_message()
        events.fail_next()
        ended = await client.post(f"{path}/end", headers=headers)

    assert (started.status_code, ended.status_code) == (200, 200)
    assert update.event == "session.updated"
    body = update.json()
    assert body["type"] == "session.updated"
    assert body["correlation_id"] is None
    assert body["data"]["status"] == "live"
    assert body["data"]["started_at"] is not None
    registry = app.state.metrics_registry
    assert isinstance(registry, CollectorRegistry)
    assert registry.get_sample_value("livecart_session_events_unpublished_total") == 1


async def test_an_event_that_fits_no_member_of_the_union_is_skipped_and_logged() -> None:
    live = _live()
    malformed = SessionEvent(
        type=SessionEventType.STOCK_UPDATED,
        session_id=live.session.id,
        occurred_at=live.clock.now(),
        data={"available": "plenty"},
    )

    with structlog.testing.capture_logs() as logs:
        async with open_event_stream(live.app, live.path, live.headers()) as stream:
            await _snapshot(stream)
            await live.events.publish(malformed)
            after = await live.publish(7)
            relayed = await stream.next_message()

    assert relayed.json() == after.model_dump(mode="json")
    assert [log["event"] for log in logs if log["log_level"] == "error"] == [
        "session_event_unrelayable"
    ]


async def test_a_stream_the_events_fail_is_closed_and_logged() -> None:
    live = _live()

    with structlog.testing.capture_logs() as logs:
        async with open_event_stream(live.app, live.path, live.headers()) as stream:
            await _snapshot(stream)
            live.events.fail_next()
            close = await stream.next_message()

    assert close.event == CLOSE_EVENT
    assert close.json()["data"] == {"reason": "unavailable"}
    [closed] = [log for log in logs if log["event"] == "session_stream_closed"]
    assert closed["log_level"] == "warning"
    assert closed["error"] == "InMemorySessionEvents.read: scripted failure"
