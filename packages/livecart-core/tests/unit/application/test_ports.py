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
    IdempotencyStore,
    IntentFallback,
    InvalidSignature,
    LiveSessionConflict,
    LiveSessionRepository,
    NullIntentFallback,
    OrderRepository,
    PasswordHasher,
    PaymentProvider,
    PaymentRepository,
    PlatformAdapter,
    PortError,
    ProductConflict,
    ProductStockRepository,
    RateLimiter,
    RecordNotFound,
    RefreshTokenRepository,
    SellerConflict,
    SellerRepository,
    ServiceUnavailable,
    ShipmentRepository,
    StockConflict,
    WebhookEventRegistry,
)
from livecart_core.domain.live_sessions import CatalogConflict
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
    FakePasswordHasher,
    FakePaymentProvider,
    FakePlatformAdapter,
    InMemoryDatabase,
    InMemoryIdempotencyStore,
    InMemoryRateLimiter,
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
    password_hashers: list[PasswordHasher] = [FakePasswordHasher()]
    sellers: list[SellerRepository] = [database.sellers]
    refresh_tokens: list[RefreshTokenRepository] = [database.refresh_tokens]
    inboxes: list[CommentInbox] = [database.comments]
    sessions: list[LiveSessionRepository] = [database.live_sessions]
    products: list[ProductStockRepository] = [database.products]
    orders: list[OrderRepository] = [database.orders]
    payments: list[PaymentRepository] = [database.payments]
    shipments: list[ShipmentRepository] = [database.shipments]
    webhook_events: list[WebhookEventRegistry] = [database.webhook_events]
    idempotency: list[IdempotencyStore] = [InMemoryIdempotencyStore()]
    rate_limiters: list[RateLimiter] = [InMemoryRateLimiter(clock)]

    ports: list[Sequence[object]] = [
        clocks,
        adapters,
        queues,
        fallbacks,
        publishers,
        notifiers,
        payment_providers,
        carriers,
        password_hashers,
        sellers,
        refresh_tokens,
        inboxes,
        sessions,
        products,
        orders,
        payments,
        shipments,
        webhook_events,
        idempotency,
        rate_limiters,
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
    "error",
    [
        RecordNotFound,
        ServiceUnavailable,
        InvalidSignature,
        StockConflict,
        SellerConflict,
        LiveSessionConflict,
        ProductConflict,
    ],
)
def test_every_port_error_belongs_to_one_family(error: type[PortError]) -> None:
    assert issubclass(error, PortError)


def test_a_seller_conflict_names_the_taken_field() -> None:
    conflict = SellerConflict("slug")

    assert conflict.field == "slug"
    assert str(conflict) == "another seller has this slug"


def test_a_product_conflict_names_what_the_catalog_refused() -> None:
    conflict = ProductConflict(CatalogConflict.VARIANT_MIX)

    assert conflict.conflict is CatalogConflict.VARIANT_MIX
    assert str(conflict) == "the catalog cannot hold this product: variant_mix"


def test_a_missing_record_is_also_a_lookup_error() -> None:
    assert issubclass(RecordNotFound, LookupError)
