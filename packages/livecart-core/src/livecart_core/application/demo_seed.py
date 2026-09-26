"""``SeedDemo``: the demo data of the stack, created on the first run and found again on the next.

What ``DEMO_SELLERS`` holds:

- the seller ``demo-seller``, and its two live sessions on the simulator,
  started: ``loadtest`` (live id ``loadtest``), Z99 alone with 200 in stock,
  the target of the load test; then ``friday-drop`` (live id
  ``friday-drop``), with the catalog the parser corpus is written against
  (``testing.builders.demo_catalog``: A12 in S, M and L, B07, C3 in four
  colours, D4 in four sizes, E15, Z99) and one product more, F8, which no
  corpus comment names. In that order, so that ``friday-drop`` starts last
  and is the session the seller's storefront shows;
- the seller ``sample-shop``, without any session: the storefront of the
  second brand of the buyer app.

Every run looks each record up by its natural key (a seller's slug, a
session's platform and live id, a product's code and variant) and adds only
what is missing, through the same ports and rules as the API: the domain
checks every field, and a session is started only from a draft. What exists
is never changed, neither a seller's password nor a product's stock, so a
second run changes nothing. A demo session that a seller has ended stays
ended, since ended is final: the run reports it and adds nothing to it, and
only an empty database (``make reset``) brings it back. Both sellers sign in
with the one demo password.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from livecart_core.application.create_seller import CreateSeller
from livecart_core.application.dto import (
    LiveSessionRecord,
    NewLiveSession,
    NewProduct,
    Platform,
    SellerRecord,
)
from livecart_core.application.ports import (
    Clock,
    LiveSessionRepository,
    PasswordHasher,
    ProductStockRepository,
    SellerRepository,
)
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.state_machines import LiveSessionStatus

LOADTEST_LIVE_ID: Final = "loadtest"


class SeedConflict(Exception):
    """A record the seed needs is in a state it cannot use: the message says which."""


@dataclass(frozen=True, slots=True)
class DemoProduct:
    code: str
    name: str
    price_minor: int
    stock_total: int
    variant: str = ""


@dataclass(frozen=True, slots=True)
class DemoSession:
    title: str
    external_live_id: str
    products: tuple[DemoProduct, ...]
    platform: Platform = Platform.SIMULATOR


@dataclass(frozen=True, slots=True)
class DemoSeller:
    slug: str
    email: str
    display_name: str
    sessions: tuple[DemoSession, ...] = ()


def _variants(
    code: str, name: str, price_minor: int, stock_total: int, *labels: str
) -> tuple[DemoProduct, ...]:
    """One product row per variant of a code, each with its own stock."""
    return tuple(DemoProduct(code, name, price_minor, stock_total, label) for label in labels)


FRIDAY_DROP: Final = DemoSession(
    title="Friday drop",
    external_live_id="friday-drop",
    products=(
        *_variants("A12", "Robe fleurie", 3_990, 5, "S", "M", "L"),
        DemoProduct("B07", "Sac en cuir", 8_900, 3),
        *_variants("C3", "T-shirt coton", 1_990, 10, "Rouge", "Bleu", "Bleu nuit", "Noir"),
        *_variants("D4", "Jean droit", 5_990, 4, "36", "38", "40", "42"),
        DemoProduct("E15", "Casquette brodée", 2_490, 8, "Unique"),
        DemoProduct("Z99", "Carte cadeau", 2_500, 50),
        DemoProduct("F8", "Foulard en soie", 2_990, 6),
    ),
)
LOADTEST: Final = DemoSession(
    title="Load test",
    external_live_id=LOADTEST_LIVE_ID,
    products=(DemoProduct("Z99", "Carte cadeau", 2_500, 200),),
)
DEMO_SELLERS: Final = (
    DemoSeller(
        slug="demo-seller",
        email="demo-seller@livecart.test",
        display_name="LiveCart Demo",
        # The storefront shows the session that started last: friday-drop.
        sessions=(LOADTEST, FRIDAY_DROP),
    ),
    DemoSeller(slug="sample-shop", email="sample-shop@livecart.test", display_name="Sample Shop"),
)


class Seeded(StrEnum):
    """What a run did with a record."""

    CREATED = "created"
    FOUND = "found"


@dataclass(frozen=True, slots=True)
class SeededSession:
    session: LiveSessionRecord
    seeded: Seeded
    products_added: int


@dataclass(frozen=True, slots=True)
class SeededSeller:
    seller: SellerRecord
    seeded: Seeded
    sessions: tuple[SeededSession, ...] = ()


@dataclass(frozen=True, slots=True)
class SeedReport:
    """What a run found or created, seller by seller."""

    sellers: tuple[SeededSeller, ...]

    def session(self, external_live_id: str) -> LiveSessionRecord:
        """The seeded session with this live id."""
        for seller in self.sellers:
            for seeded in seller.sessions:
                if seeded.session.external_live_id == external_live_id:
                    return seeded.session
        raise KeyError(external_live_id)


class SeedDemo:
    """Create what is missing of the demo data; see the module docstring."""

    def __init__(
        self,
        sellers: SellerRepository,
        sessions: LiveSessionRepository,
        products: ProductStockRepository,
        passwords: PasswordHasher,
        clock: Clock,
    ) -> None:
        self._sellers = sellers
        self._sessions = sessions
        self._products = products
        self._create_seller = CreateSeller(sellers, passwords, clock)
        self._clock = clock

    async def __call__(
        self, *, password: str, demo: Sequence[DemoSeller] = DEMO_SELLERS
    ) -> SeedReport:
        return SeedReport(tuple([await self._seller(seller, password) for seller in demo]))

    async def _seller(self, demo: DemoSeller, password: str) -> SeededSeller:
        seller = await self._sellers.find_by_slug(demo.slug)
        seeded = Seeded.FOUND
        if seller is None:
            seller = await self._create_seller(
                email=demo.email, slug=demo.slug, display_name=demo.display_name, password=password
            )
            seeded = Seeded.CREATED
        sessions = tuple([await self._session(seller, session) for session in demo.sessions])
        return SeededSeller(seller, seeded, sessions)

    async def _session(self, seller: SellerRecord, demo: DemoSession) -> SeededSession:
        session = await self._sessions.find_by_live_id(demo.platform, demo.external_live_id)
        seeded = Seeded.FOUND
        if session is None:
            session = await self._sessions.add(
                seller.id,
                NewLiveSession(demo.title, demo.platform, demo.external_live_id),
                now=self._clock.now(),
            )
            seeded = Seeded.CREATED
        elif session.seller_id != seller.id:
            raise SeedConflict(
                f"the live id {demo.platform}/{demo.external_live_id} belongs to another seller"
            )
        if session.status is LiveSessionStatus.ENDED:
            # Ended is final, and takes no product: a normal end of a demo, not an error.
            return SeededSession(session, seeded, 0)
        added = await self._add_missing_products(seller, session, demo)
        if session.status is LiveSessionStatus.DRAFT:
            started = await self._sessions.set_status(
                seller.id, session.id, LiveSessionStatus.LIVE, now=self._clock.now()
            )
            session = started.session
        return SeededSession(session, seeded, added)

    async def _add_missing_products(
        self, seller: SellerRecord, session: LiveSessionRecord, demo: DemoSession
    ) -> int:
        existing = {
            (product.code, product.variant)
            for product in await self._products.list_for_session(session.id)
        }
        added = 0
        for product in demo.products:
            code, variant = ProductCode(product.code), Variant(product.variant)
            if (code, variant) in existing:
                continue
            await self._products.add(
                seller.id,
                session.id,
                NewProduct(
                    code=code,
                    variant=variant,
                    name=product.name,
                    price_minor=product.price_minor,
                    stock_total=product.stock_total,
                ),
                now=self._clock.now(),
            )
            added += 1
        return added
