"""Two backends behind the repository ports, for scenarios that must hold on both.

ADR 0002 asks every SQL adapter to pass the scenarios of its in-memory fake.
``tests/contract`` writes each scenario once, against ``ReservationBackend``
(the comment inbox, stock and reservations) or ``AuthBackend`` (sellers and
refresh tokens),
and runs it on ``InMemoryBackend`` (the fakes of ``fakes.py``) and on
``PostgresBackend`` (the SQL adapters on livecart_test). A backend gives the
repositories under test, and seeds or reads the rows the scenarios need
beyond them: those helpers write as the migrator on PostgreSQL, as the plan's
fixtures do, while the repositories run as livecart_app.
"""

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.application.dto import (
    BuyerNotification,
    LiveSessionRecord,
    PaymentProviderName,
    Platform,
    ProductRecord,
    StockLevel,
)
from livecart_core.application.ports import (
    BuyerNotifier,
    CommentInbox,
    LiveSessionActivity,
    LiveSessionRepository,
    OrderLookup,
    OrderReservations,
    ProductStockRepository,
    RefreshTokenRepository,
    ReservationExpiry,
    SellerRepository,
    SessionEventLog,
)
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.money import Money
from livecart_core.domain.outcomes import RejectionReason
from livecart_core.domain.reference import RandomSource
from livecart_core.domain.state_machines import (
    CommentStatus,
    LiveSessionStatus,
    OrderStatus,
    PaymentStatus,
)
from livecart_core.infrastructure.db import tables
from livecart_core.infrastructure.db.activity import SqlLiveSessionActivity
from livecart_core.infrastructure.db.comments import SqlCommentInbox
from livecart_core.infrastructure.db.ids import new_id
from livecart_core.infrastructure.db.live_sessions import SqlLiveSessionRepository
from livecart_core.infrastructure.db.orders import SqlOrderRepository
from livecart_core.infrastructure.db.products import SqlProductStockRepository
from livecart_core.infrastructure.db.refresh_tokens import SqlRefreshTokenRepository
from livecart_core.infrastructure.db.sellers import SqlSellerRepository
from livecart_core.testing.builders import queued_comment
from livecart_core.testing.fakes import (
    FAKE_CLOCK_START,
    LOCKED_ACCOUNT,
    TEST_ORDER_TOKENS,
    InMemoryDatabase,
)

CURRENCY = "EUR"
# Unit 1's lease, as the worker takes it before unit 3 runs.
LEASE = timedelta(seconds=30)
# The access tokens of the orders both backends open.
ORDER_TOKENS = TEST_ORDER_TOKENS

type CountedTable = Literal["purchase_intents", "orders"]


class BackendOrders(OrderReservations, OrderLookup, ReservationExpiry, Protocol):
    """The order ports both backends implement so far: unit 3, its reads, the buyer's, the sweep."""


class ReservationBackend(Protocol):
    """The repositories under test, and the seeding and reading helpers of the scenarios."""

    @property
    def comments(self) -> CommentInbox: ...

    @property
    def live_sessions(self) -> LiveSessionRepository: ...

    @property
    def products(self) -> ProductStockRepository: ...

    @property
    def orders(self) -> BackendOrders: ...

    @property
    def activity(self) -> LiveSessionActivity: ...

    async def add_seller(self) -> UUID:
        """A seller whose account no password opens."""
        ...

    async def add_live_session(
        self,
        *,
        external_live_id: str = "live-1",
        status: LiveSessionStatus = LiveSessionStatus.LIVE,
        seller_id: UUID | None = None,
    ) -> LiveSessionRecord:
        """A session on the simulator, of ``seller_id`` or of a new seller, created at the start."""
        ...

    async def add_product(
        self,
        session_id: UUID,
        code: str,
        *,
        variant: str = "",
        name: str = "Produit",
        price: Money | None = None,
        stock_total: int = 10,
    ) -> ProductRecord:
        """A product priced 10.00 EUR unless ``price`` says otherwise."""
        ...

    async def add_comment(self, external_comment_id: str, *, author: str = "buyer-1") -> UUID:
        """A received comment whose lease a worker holds, as unit 1 leaves it."""
        ...

    async def comment_state(self, comment_id: UUID) -> tuple[CommentStatus, UUID | None]:
        """The comment's status and session."""
        ...

    async def reject_comment(self, comment_id: UUID) -> None:
        """Settle the comment as a ``no_intent`` rejection."""
        ...

    async def set_session_status(self, session_id: UUID, status: LiveSessionStatus) -> None:
        """Set the status directly, whatever the transitions allow."""
        ...

    async def expire_order(self, order_id: UUID) -> None:
        """Expire a pending order and release its stock, as the sweep does."""
        ...

    async def add_pending_payment(self, order_id: UUID) -> None:
        """A payment attempt in flight for the order, as StartCheckout opens one (milestone 5)."""
        ...

    async def set_stock(self, product_id: UUID, stock: StockLevel) -> None:
        """Set the counters directly, where a scenario needs units already reserved or sold."""
        ...

    async def count(self, table: CountedTable) -> int: ...


class AuthBackend(Protocol):
    """The seller and refresh token repositories under test; they seed their own rows."""

    @property
    def sellers(self) -> SellerRepository: ...

    @property
    def refresh_tokens(self) -> RefreshTokenRepository: ...


class Backend(ReservationBackend, AuthBackend, Protocol):
    """Every repository under test, on one database."""


class InMemoryBackend:
    """The fakes of ``fakes.py``, all over one ``InMemoryDatabase``."""

    def __init__(self, *, random_source: RandomSource = secrets) -> None:
        self.database = InMemoryDatabase(random_source=random_source)

    @property
    def comments(self) -> CommentInbox:
        return self.database.comments

    @property
    def live_sessions(self) -> LiveSessionRepository:
        return self.database.live_sessions

    @property
    def products(self) -> ProductStockRepository:
        return self.database.products

    @property
    def orders(self) -> BackendOrders:
        return self.database.orders

    @property
    def activity(self) -> LiveSessionActivity:
        return self.database.activity

    @property
    def sellers(self) -> SellerRepository:
        return self.database.sellers

    @property
    def refresh_tokens(self) -> RefreshTokenRepository:
        return self.database.refresh_tokens

    async def add_seller(self) -> UUID:
        return self.database.add_seller().id

    async def add_live_session(
        self,
        *,
        external_live_id: str = "live-1",
        status: LiveSessionStatus = LiveSessionStatus.LIVE,
        seller_id: UUID | None = None,
    ) -> LiveSessionRecord:
        return self.database.add_live_session(
            external_live_id=external_live_id,
            status=status,
            seller_id=seller_id if seller_id is not None else await self.add_seller(),
        )

    async def add_product(
        self,
        session_id: UUID,
        code: str,
        *,
        variant: str = "",
        name: str = "Produit",
        price: Money | None = None,
        stock_total: int = 10,
    ) -> ProductRecord:
        return self.database.add_product(
            session_id, code, variant=variant, name=name, price=price, stock_total=stock_total
        )

    async def add_comment(self, external_comment_id: str, *, author: str = "buyer-1") -> UUID:
        claim = await self.database.comments.claim(
            queued_comment(external_comment_id=external_comment_id, author=author),
            holder="worker-1",
            now=FAKE_CLOCK_START,
            lease=LEASE,
        )
        return claim.comment.id

    async def comment_state(self, comment_id: UUID) -> tuple[CommentStatus, UUID | None]:
        comment = self.database.tables.comments[comment_id]
        return comment.status, comment.session_id

    async def reject_comment(self, comment_id: UUID) -> None:
        await self.database.comments.reject(comment_id, RejectionReason.NO_INTENT, session_id=None)

    async def set_session_status(self, session_id: UUID, status: LiveSessionStatus) -> None:
        session = self.database.tables.sessions[session_id]
        self.database.tables.sessions[session_id] = replace(session, status=status)

    async def expire_order(self, order_id: UUID) -> None:
        order = self.database.tables.orders[order_id]
        expired = await self.database.orders.expire_due(
            now=order.reserved_until + timedelta(seconds=1), limit=len(self.database.tables.orders)
        )
        if order_id not in {row.id for row in expired.orders}:
            raise AssertionError(f"order {order_id} was not pending")

    async def add_pending_payment(self, order_id: UUID) -> None:
        opening = await self.database.payments.open_attempt(
            order_id,
            provider=PaymentProviderName.MOCK,
            return_url="livecart://payment-return",
            now=FAKE_CLOCK_START,
            grace=timedelta(seconds=60),
            max_attempts=3,
        )
        if opening.payment is None:
            raise AssertionError(f"order {order_id} takes no payment: {opening.kind}")

    async def set_stock(self, product_id: UUID, stock: StockLevel) -> None:
        product = self.database.tables.products[product_id]
        self.database.tables.products[product_id] = ProductRecord(
            id=product.id,
            session_id=product.session_id,
            code=product.code,
            variant=product.variant,
            name=product.name,
            price=product.price,
            stock=stock,
        )

    async def count(self, table: CountedTable) -> int:
        rows = (
            self.database.tables.intents
            if table == "purchase_intents"
            else self.database.tables.orders
        )
        return len(rows)


class PostgresBackend:
    """The SQL adapters as livecart_app, the seeding and reading helpers as livecart_migrator.

    It expects empty tables at the head revision: the ``clean_database`` fixture.
    """

    def __init__(
        self,
        app_engine: AsyncEngine,
        migrator_engine: AsyncEngine,
        *,
        random_source: RandomSource = secrets,
    ) -> None:
        self._migrator = migrator_engine
        self._comments = SqlCommentInbox(app_engine)
        self._live_sessions = SqlLiveSessionRepository(app_engine)
        self._products = SqlProductStockRepository(app_engine, currency=CURRENCY)
        self._orders = SqlOrderRepository(
            app_engine,
            currency=CURRENCY,
            access_token_hash=ORDER_TOKENS.stored_hash,
            random_source=random_source,
        )
        self._activity = SqlLiveSessionActivity(app_engine, currency=CURRENCY)
        self._sellers = SqlSellerRepository(app_engine)
        self._refresh_tokens = SqlRefreshTokenRepository(app_engine)

    @property
    def comments(self) -> CommentInbox:
        return self._comments

    @property
    def live_sessions(self) -> LiveSessionRepository:
        return self._live_sessions

    @property
    def products(self) -> ProductStockRepository:
        return self._products

    @property
    def orders(self) -> BackendOrders:
        return self._orders

    @property
    def activity(self) -> LiveSessionActivity:
        return self._activity

    @property
    def sellers(self) -> SellerRepository:
        return self._sellers

    @property
    def refresh_tokens(self) -> RefreshTokenRepository:
        return self._refresh_tokens

    async def add_seller(self) -> UUID:
        seller_id = new_id()
        async with self._migrator.begin() as connection:
            await connection.execute(
                insert(tables.sellers).values(
                    id=seller_id,
                    email=f"seller-{seller_id.hex}@example.test",
                    slug=f"seller-{seller_id.hex}",
                    display_name="Seller",
                    password_hash=LOCKED_ACCOUNT,
                )
            )
        return seller_id

    async def add_live_session(
        self,
        *,
        external_live_id: str = "live-1",
        status: LiveSessionStatus = LiveSessionStatus.LIVE,
        seller_id: UUID | None = None,
    ) -> LiveSessionRecord:
        session = LiveSessionRecord(
            id=new_id(),
            seller_id=seller_id if seller_id is not None else await self.add_seller(),
            title="Friday drop",
            platform=Platform.SIMULATOR,
            external_live_id=external_live_id,
            status=status,
            created_at=FAKE_CLOCK_START,
        )
        async with self._migrator.begin() as connection:
            await connection.execute(
                insert(tables.live_sessions).values(
                    id=session.id,
                    seller_id=session.seller_id,
                    title=session.title,
                    platform=session.platform.value,
                    external_live_id=session.external_live_id,
                    status=session.status.value,
                    created_at=session.created_at,
                )
            )
        return session

    async def add_product(
        self,
        session_id: UUID,
        code: str,
        *,
        variant: str = "",
        name: str = "Produit",
        price: Money | None = None,
        stock_total: int = 10,
    ) -> ProductRecord:
        product = ProductRecord(
            id=new_id(),
            session_id=session_id,
            code=ProductCode(code),
            variant=Variant(variant),
            name=name,
            price=price if price is not None else Money(1_000, CURRENCY),
            stock=StockLevel(stock_total),
        )
        async with self._migrator.begin() as connection:
            await connection.execute(
                insert(tables.products).values(
                    id=product.id,
                    session_id=session_id,
                    code=code,
                    variant=variant,
                    name=name,
                    price_minor=product.price.amount_minor,
                    stock_total=stock_total,
                )
            )
        return product

    async def add_comment(self, external_comment_id: str, *, author: str = "buyer-1") -> UUID:
        comment_id = new_id()
        await self.add_comments([(comment_id, external_comment_id, author)])
        return comment_id

    async def add_comments(self, comments: list[tuple[UUID, str, str]]) -> None:
        """Many received comments in one statement: (id, external comment id, author) each."""
        async with self._migrator.begin() as connection:
            await connection.execute(
                insert(tables.comments),
                [
                    {
                        "id": comment_id,
                        "platform": Platform.SIMULATOR.value,
                        "external_comment_id": external_comment_id,
                        "author_platform_user_id": author,
                        "text": "je prends B07",
                        "correlation_id": f"correlation-{external_comment_id}",
                        "status": CommentStatus.RECEIVED.value,
                        "claimed_by": "worker-1",
                        "claimed_until": FAKE_CLOCK_START + LEASE,
                        "created_at": FAKE_CLOCK_START,
                    }
                    for comment_id, external_comment_id, author in comments
                ],
            )

    async def comment_state(self, comment_id: UUID) -> tuple[CommentStatus, UUID | None]:
        async with self._migrator.connect() as connection:
            row = (
                await connection.execute(
                    select(tables.comments.c.status, tables.comments.c.session_id).where(
                        tables.comments.c.id == comment_id
                    )
                )
            ).one()
        return CommentStatus(row.status), row.session_id

    async def reject_comment(self, comment_id: UUID) -> None:
        async with self._migrator.begin() as connection:
            await connection.execute(
                update(tables.comments)
                .where(tables.comments.c.id == comment_id)
                .values(
                    status=CommentStatus.REJECTED.value,
                    rejection_reason=RejectionReason.NO_INTENT.value,
                )
            )

    async def set_session_status(self, session_id: UUID, status: LiveSessionStatus) -> None:
        async with self._migrator.begin() as connection:
            await connection.execute(
                update(tables.live_sessions)
                .where(tables.live_sessions.c.id == session_id)
                .values(status=status.value)
            )

    async def expire_order(self, order_id: UUID) -> None:
        async with self._migrator.begin() as connection:
            order = (
                await connection.execute(
                    update(tables.orders)
                    .where(
                        tables.orders.c.id == order_id,
                        tables.orders.c.status == OrderStatus.PENDING_PAYMENT.value,
                    )
                    .values(status=OrderStatus.EXPIRED.value, expired_at=func.now())
                    .returning(tables.orders.c.product_id, tables.orders.c.quantity)
                )
            ).one()
            await connection.execute(
                update(tables.products)
                .where(tables.products.c.id == order.product_id)
                .values(reserved=tables.products.c.reserved - order.quantity)
            )

    async def add_pending_payment(self, order_id: UUID) -> None:
        async with self._migrator.begin() as connection:
            order = (
                await connection.execute(
                    select(tables.orders.c.total_minor, tables.orders.c.currency).where(
                        tables.orders.c.id == order_id
                    )
                )
            ).one()
            await connection.execute(
                insert(tables.payments).values(
                    id=new_id(),
                    order_id=order_id,
                    attempt_no=1,
                    provider=PaymentProviderName.MOCK.value,
                    return_url="livecart://payment-return",
                    amount_minor=order.total_minor,
                    currency=order.currency,
                    status=PaymentStatus.PENDING.value,
                )
            )

    async def set_stock(self, product_id: UUID, stock: StockLevel) -> None:
        async with self._migrator.begin() as connection:
            await connection.execute(
                update(tables.products)
                .where(tables.products.c.id == product_id)
                .values(stock_total=stock.total, reserved=stock.reserved, sold=stock.sold)
            )

    async def count(self, table: CountedTable) -> int:
        async with self._migrator.connect() as connection:
            counted = await connection.scalar(
                select(func.count()).select_from(tables.metadata.tables[table])
            )
        return int(counted or 0)


@dataclass(frozen=True, slots=True)
class EventsBackend:
    """The session streams under test, and how time and expiry reach them.

    ``let_retention_pass`` waits until the events published so far are past
    the retention; ``expire`` drops a session's stream, as its time to live
    would.
    """

    events: SessionEventLog
    let_retention_pass: Callable[[], Awaitable[None]]
    expire: Callable[[UUID], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class NotifierBackend:
    """The buyer notifier under test, what it delivered, and a notifier that fails.

    ``delivered`` reads the notices back from where the backend keeps them,
    oldest first. ``failing`` gives a notifier whose next notice fails, as an
    outage of its store would, over the store ``delivered`` reads.
    """

    notifier: BuyerNotifier
    delivered: Callable[[], Awaitable[list[BuyerNotification]]]
    failing: Callable[[], BuyerNotifier]


class BackendFactory(Protocol):
    """Builds a backend whose SQL adapter draws order references from ``random_source``."""

    def __call__(self, *, random_source: RandomSource = secrets) -> Backend: ...
