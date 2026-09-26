"""A seller's public storefront: ``GET /api/v1/storefronts/{seller_slug}``.

No token: the buyer app opens it by the slug its brand names. It answers the
seller's display name and the live session that started last, if one is
live, with each product's code, variant, name, price and available stock.
Nothing private or internal: no e-mail, no id, no reserved or sold count,
no live id. An unknown slug is 404, which the app shows as "shop not found".

Open to anyone and three queries a request, it is rate limited per client
address (``livecart_api.rate_limits``), 429 beyond.
"""

from datetime import datetime
from typing import Annotated, Final, Self

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel

from livecart_api.errors import NOT_FOUND, documented
from livecart_api.rate_limits import TOO_MANY_REQUESTS, RateLimiting, client_address
from livecart_core.application.dto import Platform, ProductRecord, Storefront
from livecart_core.application.storefront import ViewStorefront
from livecart_core.domain.sellers import MAX_SLUG_LENGTH

PREFIX: Final = "/api/v1/storefronts"


class StorefrontProduct(BaseModel):
    code: str
    variant: str
    name: str
    price_minor: int
    currency: str
    available: int

    @classmethod
    def of(cls, product: ProductRecord) -> Self:
        return cls(
            code=product.code.value,
            variant=product.variant.value,
            name=product.name,
            price_minor=product.price.amount_minor,
            currency=product.price.currency,
            available=product.stock.available,
        )


class StorefrontSession(BaseModel):
    title: str
    platform: Platform
    started_at: datetime | None
    products: list[StorefrontProduct]


class StorefrontResponse(BaseModel):
    slug: str
    display_name: str
    live_session: StorefrontSession | None

    @classmethod
    def of(cls, storefront: Storefront) -> Self:
        session = storefront.session
        return cls(
            slug=storefront.seller.slug,
            display_name=storefront.seller.display_name,
            live_session=None
            if session is None
            else StorefrontSession(
                title=session.title,
                platform=session.platform,
                started_at=session.started_at,
                products=[StorefrontProduct.of(product) for product in storefront.products],
            ),
        )


def build_storefront_router(view: ViewStorefront, limits: RateLimiting) -> APIRouter:
    router = APIRouter(prefix=PREFIX, tags=["storefronts"])

    async def limit_storefronts(request: Request) -> None:
        await limits.enforce(limits.rules.storefront, client_address(request))

    @router.get(
        "/{seller_slug}",
        summary="A seller's storefront: the current live session and its products",
        dependencies=[Depends(limit_storefronts)],
        responses=documented({404: "No seller has this slug", 429: TOO_MANY_REQUESTS}),
    )
    async def get_storefront(
        seller_slug: Annotated[str, Path(max_length=MAX_SLUG_LENGTH)],
    ) -> StorefrontResponse:
        storefront = await view(seller_slug)
        if storefront is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
        return StorefrontResponse.of(storefront)

    return router
