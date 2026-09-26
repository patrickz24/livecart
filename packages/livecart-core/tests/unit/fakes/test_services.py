import asyncio
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from livecart_core.application.dto import (
    BuyerNotification,
    CommentBatch,
    FallbackReceipt,
    FallbackRequest,
    Platform,
    SessionEvent,
    SessionEventType,
)
from livecart_core.application.ports import InvalidSignature, ServiceUnavailable
from livecart_core.domain.llm_decision import LlmProposal
from livecart_core.domain.outcomes import AmbiguityReason, LlmOutcome, LlmReason
from livecart_core.domain.parser import Ambiguous
from livecart_core.domain.reference import OrderReference
from livecart_core.testing.builders import demo_catalog, incoming_comment
from livecart_core.testing.fakes import (
    FAKE_CLOCK_START,
    FAKE_SIGNATURE_HEADER,
    FakeBuyerNotifier,
    FakeEventPublisher,
    FakeIntentFallback,
    FakePlatformAdapter,
    ok_receipt,
)

SESSION_ID = UUID(int=7)
BATCH = CommentBatch(
    comments=[
        incoming_comment("je prends A12 M"),
        incoming_comment("C3 x2", external_comment_id="comment-2", author="buyer-2"),
    ]
)


# --- Platform adapter ------------------------------------------------------------------


def test_a_signed_batch_verifies_and_parses_back() -> None:
    adapter = FakePlatformAdapter(Platform.TIKTOK)
    headers, body = adapter.signed(BATCH)

    adapter.verify(headers, body)

    assert adapter.platform is Platform.TIKTOK
    assert adapter.parse(body) == BATCH.comments


def test_the_signature_header_is_read_whatever_its_case() -> None:
    adapter = FakePlatformAdapter()
    headers, body = adapter.signed(BATCH)

    adapter.verify({FAKE_SIGNATURE_HEADER.upper(): headers[FAKE_SIGNATURE_HEADER]}, body)


def test_a_tampered_unsigned_or_foreign_body_is_refused() -> None:
    adapter = FakePlatformAdapter()
    headers, body = adapter.signed(BATCH)
    foreign_headers, _ = FakePlatformAdapter(key=b"another-key").signed(BATCH)

    with pytest.raises(InvalidSignature):
        adapter.verify(headers, body.replace(b"A12", b"A13"))
    with pytest.raises(InvalidSignature):
        adapter.verify({}, body)
    with pytest.raises(InvalidSignature):
        adapter.verify(foreign_headers, body)


def test_a_body_that_is_no_batch_is_a_value_error() -> None:
    with pytest.raises(ValidationError):
        FakePlatformAdapter().parse(b'{"comments": []}')
    assert issubclass(ValidationError, ValueError)


# --- Intent fallback -------------------------------------------------------------------


def _request(timeout_seconds: float = 5.0) -> FallbackRequest:
    return FallbackRequest(
        SESSION_ID,
        "je prends la robe",
        Ambiguous(AmbiguityReason.PRODUCT_BY_NAME, (), ("robe",)),
        demo_catalog(),
        timeout_seconds,
    )


def test_an_ok_receipt_proposes_one_product() -> None:
    receipt = ok_receipt("A12", "M", 2)

    assert receipt.outcome is LlmOutcome.OK
    assert receipt.proposal == LlmProposal(LlmReason.ORDER, "A12", "M", 2, Decimal("0.90"))
    assert receipt.model == "fake-model"


async def test_the_fake_fallback_plays_its_receipts_in_order_and_records_requests() -> None:
    busy = FallbackReceipt(LlmOutcome.LLM_BUSY)
    fallback = FakeIntentFallback([ok_receipt("A12", "M")])
    fallback.script(busy)

    first = await fallback.resolve(_request())
    second = await fallback.resolve(_request())

    assert first == ok_receipt("A12", "M")
    assert second is busy
    assert fallback.requests == [_request(), _request()]


async def test_running_out_of_receipts_fails_the_test() -> None:
    with pytest.raises(AssertionError, match="no scripted receipt left"):
        await FakeIntentFallback().resolve(_request())


async def test_a_slow_fallback_answers_within_the_timeout() -> None:
    fallback = FakeIntentFallback([ok_receipt("A12", "M")], delay_seconds=0.01)

    assert (await fallback.resolve(_request(timeout_seconds=1.0))).outcome is LlmOutcome.OK


async def test_a_fallback_slower_than_the_timeout_returns_a_timeout_receipt() -> None:
    fallback = FakeIntentFallback([ok_receipt("A12", "M")], delay_seconds=1.0)
    loop = asyncio.get_running_loop()
    started = loop.time()

    receipt = await fallback.resolve(_request(timeout_seconds=0.01))

    assert receipt == FallbackReceipt(LlmOutcome.TIMEOUT)
    assert loop.time() - started < 0.5
    # The scripted receipt was never read: it answers the next, faster call.
    fallback.delay_seconds = 0.0
    assert (await fallback.resolve(_request())).outcome is LlmOutcome.OK


# --- Event publisher and buyer notifier ------------------------------------------------


def _event(event_type: SessionEventType) -> SessionEvent:
    return SessionEvent(type=event_type, session_id=SESSION_ID, occurred_at=FAKE_CLOCK_START)


async def test_the_publisher_records_events_in_order() -> None:
    publisher = FakeEventPublisher()
    events = [
        _event(SessionEventType.INTENT_RECORDED),
        _event(SessionEventType.STOCK_UPDATED),
        _event(SessionEventType.ORDER_UPDATED),
        _event(SessionEventType.STOCK_UPDATED),
    ]

    for event in events:
        await publisher.publish(event)

    assert publisher.published == events
    assert publisher.of_type(SessionEventType.STOCK_UPDATED) == [events[1], events[3]]


async def test_a_failed_publication_records_nothing() -> None:
    publisher = FakeEventPublisher()
    publisher.fail_next(2)

    for _ in range(2):
        with pytest.raises(ServiceUnavailable):
            await publisher.publish(_event(SessionEventType.STOCK_UPDATED))
    await publisher.publish(_event(SessionEventType.ORDER_UPDATED))

    assert [event.type for event in publisher.published] == [SessionEventType.ORDER_UPDATED]


async def test_the_notifier_records_what_it_sends_and_fails_on_demand() -> None:
    notifier = FakeBuyerNotifier()
    notification = BuyerNotification(
        UUID(int=3), Platform.SIMULATOR, "buyer-1", OrderReference("LC-7K3F9Q"), "token-of-3"
    )
    notifier.fail_next()

    with pytest.raises(ServiceUnavailable):
        await notifier.notify(notification)
    await notifier.notify(notification)
    await notifier.notify(notification)

    # At least once: the redelivery sends the same message again.
    assert notifier.sent == [notification, notification]
