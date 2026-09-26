"""``StartCheckout``, ``SetShippingAddress`` and the return URLs, over the fakes.

The attempts' own rules, on the fake and on PostgreSQL alike, are in
``tests/contract/test_payment_attempts.py``.
"""

from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from livecart_core.application.buyer_orders import LookUpOrder, SetShippingAddress
from livecart_core.application.checkout import (
    BRIDGE_PATH,
    BridgeStatus,
    CheckoutPages,
    CheckoutRefusal,
    CheckoutRefused,
    InvalidReturnUrl,
    ReturnUrlPolicy,
    StartCheckout,
    StartedCheckout,
)
from livecart_core.application.dto import (
    MAX_URL_LENGTH,
    CheckoutRequest,
    CheckoutSession,
    IntentSource,
    OrderRecord,
    PaymentProviderName,
    PaymentRecord,
    ReservationRequest,
    ShippingAddress,
)
from livecart_core.application.ports import OrderClosed, ServiceUnavailable
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.money import Money
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.reference import OrderReference
from livecart_core.domain.state_machines import OrderStatus, PaymentStatus
from livecart_core.testing.builders import queued_comment
from livecart_core.testing.fakes import (
    FAKE_CLOCK_START,
    TEST_ORDER_TOKENS,
    FakeClock,
    FakePaymentProvider,
    InMemoryDatabase,
)

NOW = FAKE_CLOCK_START
TTL = timedelta(seconds=900)
GRACE = timedelta(seconds=600)
BASE_URL = "http://localhost:8080"
RETURN_URL = "livecart://payment-return"
ADDRESS = ShippingAddress(
    full_name="Camille Martin",
    line1="12 rue des Lilas",
    postal_code="69003",
    city="Lyon",
    country="FR",
)
APP_SCHEMES = frozenset({"livecart", "samplelive"})


class InspectingProvider(FakePaymentProvider):
    """A fake provider that notes what the database held when a session was created."""

    def __init__(self, database: InMemoryDatabase) -> None:
        super().__init__()
        self.database = database
        self.seen: list[tuple[OrderRecord, list[PaymentStatus]]] = []

    async def create_checkout(self, request: CheckoutRequest) -> CheckoutSession:
        order = self.database.tables.orders[request.order_id]
        attempts = [
            payment.status
            for payment in self.database.tables.payments.values()
            if payment.order_id == request.order_id
        ]
        self.seen.append((order, attempts))
        return await super().create_checkout(request)


@dataclass
class Shop:
    database: InMemoryDatabase
    clock: FakeClock
    provider: InspectingProvider
    checkout: StartCheckout
    addresses: SetShippingAddress
    order: OrderRecord

    @property
    def token(self) -> str:
        return TEST_ORDER_TOKENS.token_for(self.order.id)

    async def start(self, return_url: str = RETURN_URL) -> StartedCheckout | None:
        return await self.checkout.start(self.order.reference.value, self.token, return_url)

    def stored(self) -> OrderRecord:
        return self.database.tables.orders[self.order.id]


async def _order(database: InMemoryDatabase, buyer: str) -> OrderRecord:
    """A pending order of 2 x B07 at 12.50 EUR, reserved until NOW + TTL."""
    session = next(iter(database.tables.sessions.values()))
    claim = await database.comments.claim(
        queued_comment(external_comment_id=f"comment-{buyer}", author=buyer),
        holder="worker-1",
        now=NOW,
        lease=timedelta(seconds=30),
    )
    result = await database.orders.reserve(
        ReservationRequest(
            comment_id=claim.comment.id,
            session_id=session.id,
            product_code=ProductCode("B07"),
            variant=Variant(),
            quantity=Quantity(2),
            source=IntentSource.PARSER,
            confidence=Decimal("1.00"),
            evidence=("B07",),
            now=NOW,
            reserved_until=NOW + TTL,
        )
    )
    assert result is not None
    assert result.order is not None
    return result.order


@pytest.fixture
async def shop() -> Shop:
    database = InMemoryDatabase()
    session = database.add_live_session()
    database.add_product(session.id, "B07", price=Money(1_250, "EUR"))
    order = await _order(database, "buyer-1")
    clock = FakeClock()
    provider = InspectingProvider(database)
    lookup = LookUpOrder(database.orders, TEST_ORDER_TOKENS.verify)
    checkout = StartCheckout(
        lookup,
        database.payments,
        provider,
        clock,
        pages=CheckoutPages(BASE_URL),
        return_urls=ReturnUrlPolicy(APP_SCHEMES, allow_expo_go=True),
        grace=GRACE,
        max_attempts=3,
    )
    return Shop(
        database, clock, provider, checkout, SetShippingAddress(lookup, database.orders), order
    )


async def _with_address(shop: Shop) -> None:
    found = await shop.addresses.set(shop.order.reference.value, shop.token, ADDRESS)
    assert found is not None


# --- StartCheckout ---------------------------------------------------------------------


async def test_a_checkout_opens_the_providers_page_for_the_first_attempt(shop: Shop) -> None:
    await _with_address(shop)
    reference = shop.order.reference.value

    started = await shop.start()

    [request] = shop.provider.requests
    assert started == StartedCheckout("https://payments.invalid/checkout/fake_cs_1", 1)
    assert request == CheckoutRequest(
        order_id=shop.order.id,
        attempt_no=1,
        reference=shop.order.reference,
        amount=Money(2_500, "EUR"),
        success_url=f"{BASE_URL}{BRIDGE_PATH}?status=success&ref={reference}",
        cancel_url=f"{BASE_URL}{BRIDGE_PATH}?status=cancel&ref={reference}",
    )
    assert request.idempotency_key == f"checkout-session:{shop.order.id}:1"
    # The reference only: never the order's token, in any URL.
    assert shop.token not in request.success_url + request.cancel_url
    [payment] = shop.database.tables.payments.values()
    assert (payment.provider_ref, payment.redirect_url) == ("fake_cs_1", started.redirect_url)
    assert payment.return_url == RETURN_URL


async def test_the_grace_extension_is_committed_before_the_session_is_created(
    shop: Shop,
) -> None:
    await _with_address(shop)
    shop.clock.advance(TTL - timedelta(seconds=30))

    await shop.start()

    [(order_then, attempts_then)] = shop.provider.seen
    # Extended, with its attempt pending, when the provider was called.
    assert order_then.reserved_until == shop.clock.now() + GRACE
    assert attempts_then == [PaymentStatus.PENDING]
    assert shop.stored().reserved_until == shop.clock.now() + GRACE


async def test_a_second_click_gets_the_same_url_without_a_new_session(shop: Shop) -> None:
    await _with_address(shop)
    first = await shop.start()
    extended = shop.stored().reserved_until
    shop.clock.advance(TTL + timedelta(seconds=60))

    again = await shop.start()

    assert again == first
    assert len(shop.provider.requests) == 1
    assert shop.stored().reserved_until == extended
    assert len(shop.database.tables.payments) == 1


async def test_an_attempt_left_without_a_session_gets_it_on_the_next_click(shop: Shop) -> None:
    await _with_address(shop)
    shop.provider.fail_next()

    with pytest.raises(ServiceUnavailable):
        await shop.start()
    [pending] = shop.database.tables.payments.values()
    assert (pending.status, pending.provider_ref) == (PaymentStatus.PENDING, None)
    retried = await shop.start()

    assert retried == StartedCheckout("https://payments.invalid/checkout/fake_cs_1", 1)
    assert [request.idempotency_key for request in shop.provider.requests] == [
        f"checkout-session:{shop.order.id}:1"
    ]
    assert len(shop.database.tables.payments) == 1


async def test_a_new_attempt_follows_an_expired_session_and_a_fourth_is_refused(
    shop: Shop,
) -> None:
    await _with_address(shop)
    urls = []
    for _ in range(3):
        started = await shop.start()
        assert started is not None
        urls.append(started.redirect_url)
        pending = await shop.database.payments.get_pending(shop.order.id)
        assert pending is not None
        await shop.database.payments.set_status(pending.id, PaymentStatus.EXPIRED)

    with pytest.raises(CheckoutRefused) as refused:
        await shop.start()

    assert refused.value.reason is CheckoutRefusal.ATTEMPTS_EXHAUSTED
    assert len(set(urls)) == 3
    assert [request.attempt_no for request in shop.provider.requests] == [1, 2, 3]


async def test_a_checkout_needs_a_shipping_address_first(shop: Shop) -> None:
    with pytest.raises(CheckoutRefused) as refused:
        await shop.start()

    assert refused.value.reason is CheckoutRefusal.ADDRESS_REQUIRED
    assert shop.database.tables.payments == {}
    assert shop.provider.requests == []


async def test_an_order_whose_reservation_ended_is_no_longer_payable(shop: Shop) -> None:
    await _with_address(shop)
    shop.clock.advance(TTL)

    with pytest.raises(CheckoutRefused) as refused:
        await shop.start()

    assert refused.value.reason is CheckoutRefusal.NOT_PAYABLE
    assert shop.provider.requests == []
    assert shop.stored().reserved_until == NOW + TTL


async def test_another_orders_token_or_none_finds_nothing(shop: Shop) -> None:
    await _with_address(shop)
    other = await _order(shop.database, "buyer-2")
    reference = shop.order.reference.value

    answers = [
        await shop.checkout.start(reference, TEST_ORDER_TOKENS.token_for(other.id), RETURN_URL),
        await shop.checkout.start(reference, None, RETURN_URL),
        await shop.checkout.start("LC-000000", shop.token, RETURN_URL),
    ]

    assert answers == [None, None, None]
    assert shop.database.tables.payments == {}


async def test_the_return_url_is_checked_before_anything_else(shop: Shop) -> None:
    with pytest.raises(InvalidReturnUrl):
        await shop.start("https://evil.example/phish")

    assert shop.database.tables.payments == {}


# --- SetShippingAddress ----------------------------------------------------------------


async def test_the_address_needs_the_orders_token_and_a_pending_order(shop: Shop) -> None:
    reference = shop.order.reference.value
    other = await _order(shop.database, "buyer-2")

    found = await shop.addresses.set(reference, shop.token, ADDRESS)
    wrong = await shop.addresses.set(reference, TEST_ORDER_TOKENS.token_for(other.id), ADDRESS)
    missing = await shop.addresses.set(reference, None, ADDRESS)

    assert found is not None
    assert found.order.shipping_address == ADDRESS
    assert found.product_code == ProductCode("B07")
    assert (wrong, missing) == (None, None)
    shop.database.tables.orders[shop.order.id] = replace(shop.stored(), status=OrderStatus.EXPIRED)
    with pytest.raises(OrderClosed):
        await shop.addresses.set(reference, shop.token, ADDRESS)


# --- Return URLs and the bridge's URLs ------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "livecart://payment-return",
        "samplelive://payment-return",
        "livecart:///payment-return",
        "livecart://orders/LC-7K3F9Q/paid",
        "exp://192.168.1.20:8081/--/payment-return",
        "exp://10.0.0.5:8081/--/payment-return",
        "exp://127.0.0.1:8081/--/payment-return",
        "exp://localhost:8081/--/payment-return",
        "exp://172.20.3.4:8081/--/payment-return",
        "exp://[fd00::5]:8081/--/payment-return",
    ],
)
def test_links_of_the_app_are_return_urls_in_development(url: str) -> None:
    assert ReturnUrlPolicy(APP_SCHEMES, allow_expo_go=True).check(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/phish",
        "http://localhost:8080/api",
        "javascript://alert(1)",
        "otherapp://payment-return",
        "livecart://payment-return?token=abc",
        "livecart://payment-return#t=abc",
        "livecart://payment return",
        'livecart://payment-return"><script>',
        "livecart:payment-return",
        "LiveCart://payment-return",
        "exp://203.0.113.7:8081/--/payment-return",
        "exp://expo.example/--/payment-return",
        "exp:///--/payment-return",
        "exp://93.184.216.34:8081/--/payment-return",
        "exp://172.32.0.1:8081/--/payment-return",
        "exp://[fe80::1:8081/--/x",
        "livecart://" + "a" * MAX_URL_LENGTH,
    ],
)
def test_anything_else_is_refused(url: str) -> None:
    with pytest.raises(InvalidReturnUrl):
        ReturnUrlPolicy(APP_SCHEMES, allow_expo_go=True).check(url)


def test_expo_go_is_refused_outside_development() -> None:
    policy = ReturnUrlPolicy(APP_SCHEMES, allow_expo_go=False)

    with pytest.raises(InvalidReturnUrl, match="exp"):
        policy.check("exp://192.168.1.20:8081/--/payment-return")
    assert policy.check(RETURN_URL) == RETURN_URL


def test_the_bridge_urls_carry_the_status_and_the_reference_only() -> None:
    payment = PaymentRecord(
        id=UUID(int=2),
        order_id=UUID(int=1),
        attempt_no=2,
        provider=PaymentProviderName.MOCK,
        amount=Money(2_500, "EUR"),
        return_url=RETURN_URL,
        status=PaymentStatus.PENDING,
        created_at=NOW,
    )

    request = CheckoutPages("https://shop.example.com/").request_for(
        OrderReference("LC-7K3F9Q"), payment
    )

    assert (request.order_id, request.attempt_no, request.amount) == (
        UUID(int=1),
        2,
        Money(2_500, "EUR"),
    )
    assert request.success_url == (
        "https://shop.example.com/api/v1/payments/return?status=success&ref=LC-7K3F9Q"
    )
    assert request.cancel_url == (
        "https://shop.example.com/api/v1/payments/return?status=cancel&ref=LC-7K3F9Q"
    )
    assert [status.value for status in BridgeStatus] == ["success", "cancel"]
