"""The products of a seller's live session: ``/api/v1/live-sessions/{session_id}/products``.

As for the sessions, every route declares ``CurrentSeller`` and every query
filters by the seller: a product of another seller's session, or of another
session than the path's, answers 404.

- ``POST /`` adds a product: its code is read by the domain's
  ``parse_product_code`` (case and fullwidth characters folded, then one to
  three letters and one to four digits), its variant by ``parse_variant``;
  a taken code and variant, a code that would hold a row without a variant
  beside rows with one, a full catalog or an ended session answer 409;
- ``GET /`` lists them in creation order, a bounded page at a time;
- ``GET /{id}`` and ``PATCH /{id}``: the name and the price change until the
  session ends, the code and the variant only in a draft. The stock is not
  among the fields: it moves by the restock route's deltas only.

Prices are integer minor units of the deployment's currency, which every
answer names.
"""

from typing import Annotated, Final, Self
from uuid import UUID

from fastapi import APIRouter, Response, status
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from livecart_api.deps import CurrentSeller
from livecart_api.errors import documented
from livecart_api.routes.live_sessions import Cursor, PageSize
from livecart_core.application.dto import (
    MAX_PRICE_MINOR,
    MAX_PRODUCT_NAME_LENGTH,
    MAX_STOCK_TOTAL,
    NewProduct,
    ProductChanges,
    ProductRecord,
)
from livecart_core.application.products import SellerProducts
from livecart_core.domain.codes import (
    MAX_VARIANT_LENGTH,
    ProductCode,
    Variant,
    parse_product_code,
    parse_variant,
)

PREFIX: Final = "/api/v1/live-sessions/{session_id}/products"
DEFAULT_PAGE_SIZE: Final = 50
# What a client may send before normalisation: a code is 7 characters once
# canonical, a variant 32, and folding may drop spaces around them.
MAX_TYPED_CODE_LENGTH: Final = 16
MAX_TYPED_VARIANT_LENGTH: Final = 2 * MAX_VARIANT_LENGTH


def _canonical_code(text: str) -> str:
    """The code as the catalog stores it; the domain's error is the 422's message."""
    return parse_product_code(text).value


def _canonical_variant(text: str) -> str:
    return parse_variant(text).value


Code = Annotated[
    str,
    StringConstraints(max_length=MAX_TYPED_CODE_LENGTH),
    AfterValidator(_canonical_code),
    Field(
        description="1 to 3 letters then 1 to 4 digits, such as A12; case and fullwidth "
        "characters are folded.",
        examples=["A12"],
    ),
]
VariantLabel = Annotated[
    str,
    StringConstraints(max_length=MAX_TYPED_VARIANT_LENGTH),
    AfterValidator(_canonical_variant),
    Field(
        description=f"A size or a colour, {MAX_VARIANT_LENGTH} characters at most; empty for none.",
        examples=["M"],
    ),
]
Name = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_PRODUCT_NAME_LENGTH),
]
PriceMinor = Annotated[
    int, Field(ge=0, le=MAX_PRICE_MINOR, description="Integer minor units (cents for EUR).")
]


class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Code
    variant: VariantLabel = ""
    name: Name
    price_minor: PriceMinor
    stock_total: int = Field(ge=0, le=MAX_STOCK_TOTAL)


class ProductUpdate(BaseModel):
    """The fields to change; a field left out, or null, keeps its value."""

    model_config = ConfigDict(extra="forbid")

    code: Code | None = None
    variant: VariantLabel | None = None
    name: Name | None = None
    price_minor: PriceMinor | None = None


class ProductResponse(BaseModel):
    id: UUID
    session_id: UUID
    code: str
    variant: str
    name: str
    price_minor: int
    currency: str
    stock_total: int
    reserved: int
    sold: int
    available: int

    @classmethod
    def of(cls, product: ProductRecord) -> Self:
        return cls(
            id=product.id,
            session_id=product.session_id,
            code=product.code.value,
            variant=product.variant.value,
            name=product.name,
            price_minor=product.price.amount_minor,
            currency=product.price.currency,
            stock_total=product.stock.total,
            reserved=product.stock.reserved,
            sold=product.stock.sold,
            available=product.stock.available,
        )


class ProductPage(BaseModel):
    items: list[ProductResponse]
    next_cursor: UUID | None


UNAUTHORIZED: Final = "Not authenticated, or the access token was refused"
NOT_FOUND: Final = "No session of this seller has this id, or no product of it has this one"
CONFLICT: Final = "The catalog cannot hold the product, or the session's status forbids it"


def build_product_router(products: SellerProducts) -> APIRouter:
    router = APIRouter(prefix=PREFIX, tags=["products"])

    @router.post(
        "",
        summary="Add a product to a live session",
        status_code=status.HTTP_201_CREATED,
        responses=documented({401: UNAUTHORIZED, 404: NOT_FOUND, 409: CONFLICT}),
    )
    async def create_product(
        seller: CurrentSeller, session_id: UUID, body: ProductCreate, response: Response
    ) -> ProductResponse:
        product = await products.add(
            seller.id,
            session_id,
            NewProduct(
                code=ProductCode(body.code),
                variant=Variant(body.variant),
                name=body.name,
                price_minor=body.price_minor,
                stock_total=body.stock_total,
            ),
        )
        response.headers["Location"] = f"{PREFIX.format(session_id=session_id)}/{product.id}"
        return ProductResponse.of(product)

    @router.get(
        "",
        summary="List the products of a live session, in creation order",
        responses=documented({401: UNAUTHORIZED, 404: NOT_FOUND}),
    )
    async def list_products(
        seller: CurrentSeller,
        session_id: UUID,
        limit: PageSize = DEFAULT_PAGE_SIZE,
        cursor: Cursor = None,
    ) -> ProductPage:
        page = await products.list(seller.id, session_id, limit=limit, cursor=cursor)
        return ProductPage(
            items=[ProductResponse.of(product) for product in page.items],
            next_cursor=page.next_cursor,
        )

    @router.get(
        "/{product_id}",
        summary="Read a product of a live session",
        responses=documented({401: UNAUTHORIZED, 404: NOT_FOUND}),
    )
    async def get_product(
        seller: CurrentSeller, session_id: UUID, product_id: UUID
    ) -> ProductResponse:
        return ProductResponse.of(await products.get(seller.id, session_id, product_id))

    @router.patch(
        "/{product_id}",
        summary="Change a product's name and price, and in a draft its code and variant",
        responses=documented({401: UNAUTHORIZED, 404: NOT_FOUND, 409: CONFLICT}),
    )
    async def update_product(
        seller: CurrentSeller, session_id: UUID, product_id: UUID, body: ProductUpdate
    ) -> ProductResponse:
        changes = ProductChanges(
            code=None if body.code is None else ProductCode(body.code),
            variant=None if body.variant is None else Variant(body.variant),
            name=body.name,
            price_minor=body.price_minor,
        )
        return ProductResponse.of(await products.update(seller.id, session_id, product_id, changes))

    return router
