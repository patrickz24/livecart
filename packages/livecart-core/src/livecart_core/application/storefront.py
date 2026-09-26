"""``ViewStorefront``: a seller's public page, found by the slug of its URL.

The buyer app opens it by the slug its brand names (``brand.json``): the
seller's display name, the live session that started last, if one is live,
and its products with their price and available stock. What the API answers
from it is public; the use case reads the records whole, and the route
projects them.
"""

from livecart_core.application.dto import Storefront
from livecart_core.application.ports import (
    LiveSessionRepository,
    ProductStockRepository,
    SellerRepository,
)
from livecart_core.domain.errors import InvalidValue
from livecart_core.domain.sellers import check_slug


class ViewStorefront:
    """The storefront of a slug, or ``None`` when no seller has it."""

    def __init__(
        self,
        sellers: SellerRepository,
        sessions: LiveSessionRepository,
        products: ProductStockRepository,
    ) -> None:
        self._sellers = sellers
        self._sessions = sessions
        self._products = products

    async def __call__(self, slug: str) -> Storefront | None:
        try:
            # A slug no seller could have is answered without a query.
            check_slug(slug)
        except InvalidValue:
            return None
        seller = await self._sellers.find_by_slug(slug)
        if seller is None:
            return None
        session = await self._sessions.find_current(seller.id)
        if session is None:
            return Storefront(seller)
        products = await self._products.list_for_session(session.id)
        return Storefront(seller, session, tuple(products))
