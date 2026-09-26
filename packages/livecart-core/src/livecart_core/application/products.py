"""The products of a seller's live session: add, list, read and edit them.

As for the sessions (``application.live_sessions``), every method takes the
seller's id and every repository call filters by it, so a product of another
seller's session is ``RecordNotFound``. The session's status decides what a
product still takes (``domain.live_sessions``): in a draft everything, while
live everything but its code and variant, once ended nothing.
"""

from uuid import UUID

from livecart_core.application.dto import (
    NewProduct,
    Page,
    ProductChanges,
    ProductRecord,
    check_page_size,
)
from livecart_core.application.ports import Clock, ProductStockRepository, RecordNotFound


class SellerProducts:
    """The product operations of one seller at a time; see the module docstring."""

    def __init__(self, products: ProductStockRepository, clock: Clock) -> None:
        self._products = products
        self._clock = clock

    async def add(self, seller_id: UUID, session_id: UUID, product: NewProduct) -> ProductRecord:
        """A new product; ``ProductConflict`` when the catalog cannot hold it."""
        return await self._products.add(seller_id, session_id, product, now=self._clock.now())

    async def list(
        self, seller_id: UUID, session_id: UUID, *, limit: int, cursor: UUID | None = None
    ) -> Page[ProductRecord]:
        """The session's products in creation order, a page of ``limit`` at a time."""
        return await self._products.list_for_seller(
            seller_id, session_id, limit=check_page_size(limit), cursor=cursor
        )

    async def get(self, seller_id: UUID, session_id: UUID, product_id: UUID) -> ProductRecord:
        product = await self._products.get_for_seller(seller_id, session_id, product_id)
        if product is None:
            raise RecordNotFound(f"no product {product_id} in this seller's session {session_id}")
        return product

    async def update(
        self, seller_id: UUID, session_id: UUID, product_id: UUID, changes: ProductChanges
    ) -> ProductRecord:
        return await self._products.update(seller_id, session_id, product_id, changes)
