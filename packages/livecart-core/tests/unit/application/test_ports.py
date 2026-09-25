from collections.abc import Sequence
from uuid import UUID

import pytest

from livecart_core.application.dto import FallbackRequest
from livecart_core.application.ports import (
    BuyerNotifier,
    CarrierProvider,
    Clock,
    CommentInbox,
    CommentQueue,
    EventPublisher,
    IntentFallback,
    InvalidSignature,
    LiveSessionRepository,
    NullIntentFallback,
    OrderRepository,
    PaymentProvider,
    PaymentRepository,
    PlatformAdapter,
    PortError,
    ProductStockRepository,
    RecordNotFound,
    ServiceUnavailable,
    ShipmentRepository,
    StockConflict,
    WebhookEventRegistry,
)
from livecart_core.domain.outcomes import AmbiguityReason, LlmOutcome
from livecart_core.domain.parser import Ambiguous
from livecart_core.testing.builders import demo_catalog
from livecart_core.testing.fakes import (
    FakeBuyerNotifier,
    FakeCarrierProvider,
    FakeClock,
    FakeCommentQueue,
    FakeEventPublisher,
    FakeIntentFallback,
    FakePaymentProvider,
    FakePlatformAdapter,
    InMemoryDatabase,
)


def test_every_fake_satisfies_its_port() -> None:
    clock = FakeClock()
    database = InMemoryDatabase()

    # The annotations are the check: mypy fails this file when a fake drifts
    # from its port. The run itself only proves that the fakes build.
    clocks: list[Clock] = [clock]
    adapters: list[PlatformAdapter] = [FakePlatformAdapter()]
    queues: list[CommentQueue] = [FakeCommentQueue(clock)]
    fallbacks: list[IntentFallback] = [FakeIntentFallback(), NullIntentFallback()]
    publishers: list[EventPublisher] = [FakeEventPublisher()]
    notifiers: list[BuyerNotifier] = [FakeBuyerNotifier()]
    payment_providers: list[PaymentProvider] = [FakePaymentProvider()]
    carriers: list[CarrierProvider] = [FakeCarrierProvider(clock)]
    inboxes: list[CommentInbox] = [database.comments]
    sessions: list[LiveSessionRepository] = [database.live_sessions]
    products: list[ProductStockRepository] = [database.products]
    orders: list[OrderRepository] = [database.orders]
    payments: list[PaymentRepository] = [database.payments]
    shipments: list[ShipmentRepository] = [database.shipments]
    webhook_events: list[WebhookEventRegistry] = [database.webhook_events]

    ports: list[Sequence[object]] = [
        clocks,
        adapters,
        queues,
        fallbacks,
        publishers,
        notifiers,
        payment_providers,
        carriers,
        inboxes,
        sessions,
        products,
        orders,
        payments,
        shipments,
        webhook_events,
    ]
    assert all(ports)


async def test_the_null_fallback_leaves_every_ambiguity_unresolved() -> None:
    request = FallbackRequest(
        UUID(int=1),
        "je prends la robe",
        Ambiguous(AmbiguityReason.PRODUCT_BY_NAME, (), ("robe",)),
        demo_catalog(),
        7.0,
    )

    receipt = await NullIntentFallback().resolve(request)

    assert receipt.outcome is LlmOutcome.DISABLED
    assert receipt.proposal is None
    assert receipt.model is None


@pytest.mark.parametrize(
    "error", [RecordNotFound, ServiceUnavailable, InvalidSignature, StockConflict]
)
def test_every_port_error_belongs_to_one_family(error: type[PortError]) -> None:
    assert issubclass(error, PortError)


def test_a_missing_record_is_also_a_lookup_error() -> None:
    assert issubclass(RecordNotFound, LookupError)
