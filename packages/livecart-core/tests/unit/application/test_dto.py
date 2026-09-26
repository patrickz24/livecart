from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from livecart_core.application.dto import (
    MAX_BATCH_SIZE,
    MAX_COMMENT_LENGTH,
    MAX_IDENTIFIER_LENGTH,
    MAX_PAGE_SIZE,
    MAX_PRICE_MINOR,
    MAX_PRODUCT_NAME_LENGTH,
    MAX_STOCK_TOTAL,
    MAX_TITLE_LENGTH,
    AttemptOpening,
    AttemptOpeningKind,
    BuyerNotification,
    CarrierName,
    CheckoutRequest,
    CheckoutStatus,
    CommentBatch,
    FallbackReceipt,
    FallbackRequest,
    InboxComment,
    IncomingComment,
    IntentSource,
    IssuedRefreshToken,
    LiveSessionChanges,
    LiveSessionRecord,
    LlmProposal,
    NewLiveSession,
    NewProduct,
    OrderRecord,
    Page,
    PaymentEvent,
    PaymentProviderName,
    PaymentRecord,
    Platform,
    ProductChanges,
    ProductRecord,
    PurchaseIntentRecord,
    QueuedComment,
    QueueDelivery,
    RefreshTokenRecord,
    RegisteredEvent,
    ReservationRequest,
    ReservationResult,
    SellerRecord,
    SessionEvent,
    SessionEventType,
    ShippingAddress,
    StockLevel,
    WebhookOutcome,
    check_page_size,
    page_of,
)
from livecart_core.domain.codes import NO_VARIANT, ProductCode, Variant
from livecart_core.domain.money import Money
from livecart_core.domain.outcomes import (
    AmbiguityReason,
    IntentOutcome,
    LlmOutcome,
    RejectionReason,
)
from livecart_core.domain.parser import Ambiguous
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.reference import OrderReference
from livecart_core.domain.state_machines import (
    CommentStatus,
    LiveSessionStatus,
    OrderStatus,
    PaymentStatus,
)
from livecart_core.testing.builders import demo_catalog, incoming_comment, queued_comment
from livecart_core.testing.fakes import FAKE_CLOCK_START

NOW = FAKE_CLOCK_START
ID = UUID(int=1)


def _assign(record: object, attribute: str, value: object) -> None:
    """An assignment mypy cannot see, to prove at run time that a record refuses it."""
    setattr(record, attribute, value)


def _comment_fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "platform": "simulator",
        "external_comment_id": "comment-1",
        "external_live_id": "live-1",
        "author_platform_user_id": "buyer-1",
        "text": "je prends A12 M",
        "posted_at": "2026-09-26T12:00:00Z",
    }
    fields.update(overrides)
    return fields


# --- Closed lists ----------------------------------------------------------------------


def test_the_closed_lists_hold_the_database_values() -> None:
    assert [p.value for p in Platform] == ["simulator", "facebook", "instagram", "tiktok"]
    assert [p.value for p in PaymentProviderName] == ["mock", "stripe", "paypal", "viva"]
    assert [c.value for c in CarrierName] == ["mock", "dpd", "chronopost", "dhl"]
    assert [s.value for s in IntentSource] == ["parser", "llm"]
    assert [o.value for o in WebhookOutcome] == ["applied", "ignored", "failed"]
    assert [s.value for s in CheckoutStatus] == ["open", "complete", "expired"]


def test_session_event_types_are_the_published_ones_of_the_plan() -> None:
    assert [t.value for t in SessionEventType] == [
        "intent.recorded",
        "comment.rejected",
        "order.updated",
        "stock.updated",
        "shipment.updated",
        "session.updated",
    ]


# --- Comment webhook format ------------------------------------------------------------


def test_an_incoming_comment_reads_the_common_format() -> None:
    comment = IncomingComment.model_validate(_comment_fields())

    assert comment.platform is Platform.SIMULATOR
    assert comment.external_comment_id == "comment-1"
    assert comment.posted_at == NOW


@pytest.mark.parametrize(
    "overrides",
    [
        {"external_comment_id": ""},
        {"external_live_id": ""},
        {"author_platform_user_id": ""},
        {"external_comment_id": "x" * (MAX_IDENTIFIER_LENGTH + 1)},
        {"external_live_id": "x" * (MAX_IDENTIFIER_LENGTH + 1)},
        {"author_platform_user_id": "x" * (MAX_IDENTIFIER_LENGTH + 1)},
        {"text": "x" * (MAX_COMMENT_LENGTH + 1)},
        {"platform": "myspace"},
        {"posted_at": "2026-09-26T12:00:00"},
        {"unexpected": "field"},
    ],
    ids=[
        "empty-comment-id",
        "empty-live-id",
        "empty-author",
        "long-comment-id",
        "long-live-id",
        "long-author",
        "long-text",
        "unknown-platform",
        "naive-time",
        "extra-field",
    ],
)
def test_an_incoming_comment_refuses_what_breaks_its_contract(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        IncomingComment.model_validate(_comment_fields(**overrides))


@pytest.mark.parametrize("text", ["", "x" * MAX_COMMENT_LENGTH])
def test_a_comment_text_may_be_empty_or_as_long_as_the_column(text: str) -> None:
    assert IncomingComment.model_validate(_comment_fields(text=text)).text == text


def test_an_identifier_may_fill_its_column() -> None:
    long_id = "x" * MAX_IDENTIFIER_LENGTH

    comment = IncomingComment.model_validate(_comment_fields(external_comment_id=long_id))

    assert comment.external_comment_id == long_id


def test_an_incoming_comment_is_frozen() -> None:
    comment = incoming_comment()

    with pytest.raises(ValidationError):
        _assign(comment, "text", "autre chose")


@pytest.mark.parametrize("size", [1, MAX_BATCH_SIZE])
def test_a_batch_holds_from_one_to_two_hundred_comments(size: int) -> None:
    batch = CommentBatch.model_validate({"comments": [_comment_fields()] * size})

    assert len(batch.comments) == size


@pytest.mark.parametrize(
    "body",
    [
        {"comments": []},
        {"comments": [_comment_fields()] * (MAX_BATCH_SIZE + 1)},
        {"comments": [_comment_fields()], "unexpected": 1},
        {},
    ],
    ids=["empty", "too-many", "extra-field", "missing"],
)
def test_a_batch_refuses_what_breaks_its_contract(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CommentBatch.model_validate(body)


def test_the_queue_envelope_survives_a_json_round_trip() -> None:
    envelope = queued_comment()

    assert QueuedComment.model_validate_json(envelope.model_dump_json()) == envelope


@pytest.mark.parametrize(
    "overrides",
    [
        {"correlation_id": ""},
        {"request_id": ""},
        {"correlation_id": "x" * (MAX_IDENTIFIER_LENGTH + 1)},
        {"enqueued_at": "2026-09-26T12:00:00"},
        {"unexpected": "field"},
    ],
    ids=["empty-correlation-id", "empty-request-id", "long-correlation-id", "naive", "extra"],
)
def test_the_queue_envelope_refuses_what_breaks_its_contract(
    overrides: dict[str, object],
) -> None:
    fields = queued_comment().model_dump(mode="json")
    fields.update(overrides)

    with pytest.raises(ValidationError):
        QueuedComment.model_validate(fields)


def test_a_delivery_counts_from_one() -> None:
    assert QueueDelivery("1-0", queued_comment(), 1).delivery_count == 1
    with pytest.raises(ValueError, match="starts at 1"):
        QueueDelivery("1-0", queued_comment(), 0)


# --- Session events --------------------------------------------------------------------


def test_a_session_event_defaults_to_empty_data_and_round_trips() -> None:
    event = SessionEvent(type=SessionEventType.STOCK_UPDATED, session_id=ID, occurred_at=NOW)

    assert event.data == {}
    assert event.correlation_id is None
    assert SessionEvent.model_validate_json(event.model_dump_json()) == event


def test_a_session_event_carries_json_data() -> None:
    event = SessionEvent(
        type=SessionEventType.ORDER_UPDATED,
        session_id=ID,
        occurred_at=NOW,
        correlation_id="correlation-1",
        data={"reference": "LC-7K3F9Q", "quantity": 2, "tags": ["a", None]},
    )

    assert event.model_dump(mode="json")["data"] == {
        "reference": "LC-7K3F9Q",
        "quantity": 2,
        "tags": ["a", None],
    }


def test_a_session_event_refuses_the_sse_only_types_and_unknown_fields() -> None:
    fields = {"type": "stream.gap", "session_id": str(ID), "occurred_at": "2026-09-26T12:00:00Z"}

    with pytest.raises(ValidationError):
        SessionEvent.model_validate(fields)
    with pytest.raises(ValidationError):
        SessionEvent.model_validate({**fields, "type": "stock.updated", "extra": 1})


# --- Stock -----------------------------------------------------------------------------


def test_available_stock_is_what_is_neither_reserved_nor_sold() -> None:
    assert StockLevel(10, reserved=3, sold=2).available == 5
    assert StockLevel(10).available == 10
    assert StockLevel(5, reserved=2, sold=3).available == 0


@pytest.mark.parametrize(
    ("total", "reserved", "sold"),
    [(-1, 0, 0), (10, -1, 0), (10, 0, -1), (10, 6, 5)],
    ids=["negative-total", "negative-reserved", "negative-sold", "oversold"],
)
def test_stock_keeps_the_invariant_of_the_database_check(
    total: int, reserved: int, sold: int
) -> None:
    with pytest.raises(ValueError, match=r"reserved \+ sold <= total"):
        StockLevel(total, reserved=reserved, sold=sold)


def test_records_are_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        _assign(StockLevel(10), "total", 11)


# --- LLM fallback ----------------------------------------------------------------------


def _ambiguous() -> Ambiguous:
    return Ambiguous(AmbiguityReason.PRODUCT_BY_NAME, (), ("robe",))


@pytest.mark.parametrize("timeout", [0.0, -1.0])
def test_a_fallback_request_needs_time_left(timeout: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        FallbackRequest(ID, "je prends la robe", _ambiguous(), demo_catalog(), timeout)


def test_a_fallback_request_holds_the_text_ambiguity_and_catalog() -> None:
    request = FallbackRequest(ID, "je prends la robe", _ambiguous(), demo_catalog(), 7.5)

    assert request.ambiguity.reason is AmbiguityReason.PRODUCT_BY_NAME
    assert request.catalog == demo_catalog()
    assert request.timeout_seconds == 7.5


def test_only_an_ok_receipt_carries_a_proposal() -> None:
    proposal = LlmProposal("A12", "M", 1, Decimal("0.9"))

    assert FallbackReceipt(LlmOutcome.OK, proposal).proposal == proposal
    assert FallbackReceipt(LlmOutcome.DISABLED).proposal is None
    with pytest.raises(ValueError, match="ok receipt"):
        FallbackReceipt(LlmOutcome.OK)
    with pytest.raises(ValueError, match="ok receipt"):
        FallbackReceipt(LlmOutcome.TRUNCATED, proposal)


# --- Inbox -----------------------------------------------------------------------------

RECEIVED_COMMENT = InboxComment(
    id=ID,
    platform=Platform.SIMULATOR,
    external_comment_id="comment-1",
    author_platform_user_id="buyer-1",
    text="je prends A12 M",
    correlation_id="correlation-1",
    status=CommentStatus.RECEIVED,
    created_at=NOW,
)


def test_only_a_rejected_comment_has_a_rejection_reason() -> None:
    rejected = replace(
        RECEIVED_COMMENT, status=CommentStatus.REJECTED, rejection_reason=RejectionReason.NO_INTENT
    )

    assert rejected.rejection_reason is RejectionReason.NO_INTENT
    with pytest.raises(ValueError, match="rejection reason"):
        replace(RECEIVED_COMMENT, status=CommentStatus.REJECTED)
    with pytest.raises(ValueError, match="rejection reason"):
        replace(RECEIVED_COMMENT, rejection_reason=RejectionReason.NO_INTENT)


# --- Reservations and orders -----------------------------------------------------------

RESERVATION = ReservationRequest(
    comment_id=ID,
    session_id=ID,
    product_code=ProductCode("B07"),
    variant=NO_VARIANT,
    quantity=Quantity(1),
    source=IntentSource.PARSER,
    confidence=Decimal("1.00"),
    evidence=("B07",),
    now=NOW,
    reserved_until=NOW + timedelta(seconds=900),
)

ORDER = OrderRecord(
    id=ID,
    reference=OrderReference("LC-7K3F9Q"),
    seller_id=ID,
    session_id=ID,
    intent_id=ID,
    product_id=ID,
    quantity=Quantity(2),
    unit_price=Money(1_250, "EUR"),
    total=Money(2_500, "EUR"),
    status=OrderStatus.PENDING_PAYMENT,
    reserved_until=NOW,
    buyer_platform_user_id="buyer-1",
    created_at=NOW,
)


@pytest.mark.parametrize("confidence", ["0", "0.5", "1"])
def test_a_reservation_takes_a_confidence_from_zero_to_one(confidence: str) -> None:
    request = replace(RESERVATION, confidence=Decimal(confidence))

    assert request.confidence == Decimal(confidence)


@pytest.mark.parametrize("confidence", ["-0.01", "1.01"])
def test_a_reservation_refuses_a_confidence_out_of_range(confidence: str) -> None:
    with pytest.raises(ValueError, match="confidence"):
        replace(RESERVATION, confidence=Decimal(confidence))


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1)])
def test_a_reservation_must_end_after_now(ttl: timedelta) -> None:
    with pytest.raises(ValueError, match="end after"):
        replace(RESERVATION, reserved_until=NOW + ttl)


def test_an_order_total_is_its_unit_price_times_its_quantity() -> None:
    assert ORDER.total == Money(2_500, "EUR")
    with pytest.raises(ValueError, match="unit price times"):
        replace(ORDER, total=Money(1_250, "EUR"))


def _intent(outcome: IntentOutcome) -> PurchaseIntentRecord:
    return PurchaseIntentRecord(
        ID, ID, ID, ID, Quantity(2), IntentSource.PARSER, Decimal("1.00"), ("B07",), outcome
    )


def test_an_order_exists_exactly_when_the_intent_was_reserved() -> None:
    stock = StockLevel(10, reserved=2)

    assert ReservationResult(_intent(IntentOutcome.RESERVED), ORDER, stock).order == ORDER
    assert ReservationResult(_intent(IntentOutcome.OUT_OF_STOCK), None, stock).order is None
    with pytest.raises(ValueError, match="exactly when"):
        ReservationResult(_intent(IntentOutcome.RESERVED), None, stock)
    with pytest.raises(ValueError, match="exactly when"):
        ReservationResult(_intent(IntentOutcome.ALREADY_RESERVED), ORDER, stock)


# --- Shipping address ------------------------------------------------------------------


def _address(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "full_name": "Camille Martin",
        "line1": "12 rue des Lilas",
        "postal_code": "69003",
        "city": "Lyon",
        "country": "FR",
    }
    fields.update(overrides)
    return fields


def test_an_address_is_stripped_and_has_an_empty_second_line_by_default() -> None:
    address = ShippingAddress.model_validate(_address(city="  Lyon  "))

    assert address.city == "Lyon"
    assert address.line2 == ""


@pytest.mark.parametrize(
    "overrides",
    [
        {"full_name": "   "},
        {"line1": ""},
        {"postal_code": "1" * 17},
        {"country": "fr"},
        {"country": "FRA"},
        {"unexpected": "field"},
    ],
    ids=["blank-name", "empty-line1", "long-postal-code", "lower-country", "alpha3", "extra"],
)
def test_an_address_refuses_what_breaks_its_contract(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ShippingAddress.model_validate(_address(**overrides))


# --- Payments --------------------------------------------------------------------------

PAYMENT = PaymentRecord(
    id=ID,
    order_id=ID,
    attempt_no=1,
    provider=PaymentProviderName.MOCK,
    amount=Money(2_500, "EUR"),
    return_url="livecart://payment-return",
    status=PaymentStatus.PENDING,
    created_at=NOW,
)


@pytest.mark.parametrize("kind", [AttemptOpeningKind.OPENED, AttemptOpeningKind.PENDING])
def test_an_opened_or_pending_attempt_carries_its_payment(kind: AttemptOpeningKind) -> None:
    assert AttemptOpening(kind, PAYMENT).payment == PAYMENT
    with pytest.raises(ValueError, match="carries its payment"):
        AttemptOpening(kind)


@pytest.mark.parametrize("kind", [AttemptOpeningKind.NOT_PAYABLE, AttemptOpeningKind.EXHAUSTED])
def test_a_refused_attempt_carries_no_payment(kind: AttemptOpeningKind) -> None:
    assert AttemptOpening(kind).payment is None
    with pytest.raises(ValueError, match="carries its payment"):
        AttemptOpening(kind, PAYMENT)


def _checkout(attempt_no: int) -> CheckoutRequest:
    return CheckoutRequest(
        UUID(int=42),
        attempt_no,
        OrderReference("LC-7K3F9Q"),
        Money(2_500, "EUR"),
        "https://example.invalid/success",
        "https://example.invalid/cancel",
    )


def test_a_checkout_has_one_idempotency_key_per_attempt() -> None:
    assert _checkout(2).idempotency_key == (
        "checkout-session:00000000-0000-0000-0000-00000000002a:2"
    )
    assert _checkout(1).idempotency_key != _checkout(2).idempotency_key


def test_a_checkout_attempt_counts_from_one() -> None:
    with pytest.raises(ValueError, match="starts at 1"):
        _checkout(0)


def _payment_event(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "provider": "stripe",
        "event_id": "evt_1",
        "event_type": "checkout.session.completed",
        "checkout_ref": "cs_1",
        "payment_ref": "pi_1",
        "amount_minor": 2_500,
        "currency": "EUR",
        "occurred_at": "2026-09-26T12:00:00Z",
    }
    fields.update(overrides)
    return fields


def test_a_payment_event_gives_its_amount_as_money() -> None:
    event = PaymentEvent.model_validate(_payment_event())

    assert event.amount == Money(2_500, "EUR")
    assert event.provider is PaymentProviderName.STRIPE


def test_a_payment_event_may_have_no_amount() -> None:
    event = PaymentEvent.model_validate(_payment_event(amount_minor=None, currency=None))

    assert event.amount is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount_minor": None},
        {"currency": None},
        {"amount_minor": -1},
        {"currency": "eur"},
        {"event_id": ""},
        {"customer_details": {"email": "buyer@example.invalid"}},
    ],
    ids=["currency-alone", "amount-alone", "negative", "lower-currency", "empty-id", "extra"],
)
def test_a_payment_event_refuses_what_breaks_its_allow_list(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PaymentEvent.model_validate(_payment_event(**overrides))


def test_only_a_processed_webhook_event_has_an_outcome() -> None:
    event = PaymentEvent.model_validate(_payment_event())
    processed = RegisteredEvent(event, NOW, processed_at=NOW, outcome=WebhookOutcome.APPLIED)

    assert RegisteredEvent(event, NOW).outcome is None
    assert processed.outcome is WebhookOutcome.APPLIED
    with pytest.raises(ValueError, match="processed event"):
        RegisteredEvent(event, NOW, processed_at=NOW)
    with pytest.raises(ValueError, match="processed event"):
        RegisteredEvent(event, NOW, outcome=WebhookOutcome.IGNORED)


# --- Buyer notification ----------------------------------------------------------------


def test_the_access_token_stays_out_of_the_repr() -> None:
    notification = BuyerNotification(
        ID, Platform.SIMULATOR, "buyer-1", OrderReference("LC-7K3F9Q"), "token-value-of-order-1"
    )

    assert "token-value-of-order-1" not in repr(notification)
    assert "LC-7K3F9Q" in repr(notification)
    assert notification.access_token == "token-value-of-order-1"


# --- Sellers and refresh tokens --------------------------------------------------------


def _refresh_token(
    *, used_at: datetime | None = None, replaced_by_id: UUID | None = None
) -> RefreshTokenRecord:
    return RefreshTokenRecord(
        id=ID,
        seller_id=UUID(int=2),
        family_id=UUID(int=3),
        expires_at=FAKE_CLOCK_START + timedelta(days=30),
        created_at=FAKE_CLOCK_START,
        used_at=used_at,
        replaced_by_id=replaced_by_id,
    )


def test_a_refresh_token_is_active_until_used_revoked_or_expired() -> None:
    token = _refresh_token()
    later = FAKE_CLOCK_START + timedelta(seconds=1)

    assert token.is_active(FAKE_CLOCK_START)
    assert not token.is_active(token.expires_at)
    assert not replace(token, revoked_at=later).is_active(later)
    assert not replace(token, used_at=later, replaced_by_id=UUID(int=4)).is_active(later)


def test_a_used_refresh_token_and_only_a_used_one_names_its_successor() -> None:
    with pytest.raises(ValueError, match="successor"):
        _refresh_token(used_at=FAKE_CLOCK_START)
    with pytest.raises(ValueError, match="successor"):
        _refresh_token(replaced_by_id=UUID(int=4))


def test_secrets_of_sellers_stay_out_of_the_repr() -> None:
    seller = SellerRecord(
        ID, "seller@example.test", "demo-seller", "Demo", "argon2-hash-value", FAKE_CLOCK_START
    )
    issued = IssuedRefreshToken("refresh-token-value", FAKE_CLOCK_START)

    assert "argon2-hash-value" not in repr(seller)
    assert "demo-seller" in repr(seller)
    assert "refresh-token-value" not in repr(issued)
    assert issued.value == "refresh-token-value"


# --- Pages, sessions and products of a seller ------------------------------------------


@dataclass(frozen=True)
class _Row:
    id: UUID


def test_a_page_of_limit_plus_one_rows_names_its_next_cursor() -> None:
    rows = [_Row(UUID(int=n)) for n in range(1, 5)]

    assert page_of(rows, 3) == Page(tuple(rows[:3]), UUID(int=3))
    assert page_of(rows[:3], 3) == Page(tuple(rows[:3]), None)
    assert page_of([], 3) == Page((), None)


@pytest.mark.parametrize("limit", [1, MAX_PAGE_SIZE])
def test_a_page_holds_one_to_the_maximum_items(limit: int) -> None:
    assert check_page_size(limit) == limit


@pytest.mark.parametrize("limit", [0, -1, MAX_PAGE_SIZE + 1])
def test_a_page_size_outside_its_bounds_is_a_caller_bug(limit: int) -> None:
    with pytest.raises(ValueError, match="page holds"):
        check_page_size(limit)


TITLE, LIVE_ID = "Friday drop", "fb-1"


@pytest.mark.parametrize(
    ("title", "external_live_id"),
    [
        ("", LIVE_ID),
        (" Friday", LIVE_ID),
        ("x" * (MAX_TITLE_LENGTH + 1), LIVE_ID),
        (TITLE, ""),
        (TITLE, "x" * (MAX_IDENTIFIER_LENGTH + 1)),
    ],
)
def test_a_session_refuses_empty_padded_or_long_labels(title: str, external_live_id: str) -> None:
    with pytest.raises(ValueError, match="characters"):
        NewLiveSession(title, Platform.FACEBOOK, external_live_id)
    with pytest.raises(ValueError, match="characters"):
        LiveSessionChanges(title=title, external_live_id=external_live_id)


@pytest.mark.parametrize(
    ("changes", "touches"),
    [
        (LiveSessionChanges(), False),
        (LiveSessionChanges(title="Renamed"), False),
        (LiveSessionChanges(platform=Platform.FACEBOOK, external_live_id=LIVE_ID), False),
        (LiveSessionChanges(platform=Platform.TIKTOK), True),
        (LiveSessionChanges(external_live_id="fb-2"), True),
    ],
)
def test_a_session_change_touches_its_keys_only_when_it_moves_them(
    changes: LiveSessionChanges, touches: bool
) -> None:
    session = LiveSessionRecord(
        id=UUID(int=1),
        seller_id=UUID(int=2),
        title=TITLE,
        platform=Platform.FACEBOOK,
        external_live_id=LIVE_ID,
        status=LiveSessionStatus.LIVE,
        created_at=FAKE_CLOCK_START,
    )

    assert changes.touches_keys(session) is touches


@pytest.mark.parametrize(
    ("name", "price_minor", "stock_total"),
    [
        ("", 1_000, 5),
        ("x" * (MAX_PRODUCT_NAME_LENGTH + 1), 1_000, 5),
        ("Robe", -1, 5),
        ("Robe", MAX_PRICE_MINOR + 1, 5),
        ("Robe", 1_000, -1),
        ("Robe", 1_000, MAX_STOCK_TOTAL + 1),
    ],
)
def test_a_new_product_keeps_to_its_bounds(name: str, price_minor: int, stock_total: int) -> None:
    with pytest.raises(ValueError, match=r"from 0 to|characters"):
        NewProduct(ProductCode("A12"), NO_VARIANT, name, price_minor, stock_total)


def test_a_product_takes_its_bounds_inclusive() -> None:
    longest = "x" * MAX_PRODUCT_NAME_LENGTH
    dearest = NewProduct(ProductCode("A12"), NO_VARIANT, longest, MAX_PRICE_MINOR, MAX_STOCK_TOTAL)
    free = NewProduct(ProductCode("A12"), NO_VARIANT, "Robe", 0, 0)

    assert (dearest.price_minor, dearest.stock_total) == (MAX_PRICE_MINOR, MAX_STOCK_TOTAL)
    assert (free.price_minor, free.stock_total) == (0, 0)


@pytest.mark.parametrize(("name", "price_minor"), [(" Robe", 1), ("Robe", MAX_PRICE_MINOR + 1)])
def test_product_changes_keep_to_the_same_bounds(name: str, price_minor: int) -> None:
    with pytest.raises(ValueError, match=r"characters|from 0 to"):
        ProductChanges(name=name, price_minor=price_minor)


def test_a_product_change_touches_its_keys_only_when_it_moves_them() -> None:
    product = ProductRecord(
        id=UUID(int=1),
        session_id=UUID(int=2),
        code=ProductCode("A12"),
        variant=Variant("M"),
        name="Robe",
        price=Money(1_000, "EUR"),
        stock=StockLevel(5),
    )

    assert not ProductChanges(name="Robe longue", price_minor=1).touches_keys(product)
    assert not ProductChanges(code=ProductCode("A12"), variant=Variant("M")).touches_keys(product)
    assert ProductChanges(code=ProductCode("A13")).touches_keys(product)
    assert ProductChanges(variant=Variant("L")).key_of(product) == (
        ProductCode("A12"),
        Variant("L"),
    )
