"""A buyer's order, by its reference: ``/api/v1/orders/{reference}``.

Two tiers (``application.buyer_orders``):

- ``GET /{reference}`` needs nothing but the reference, read tolerantly
  (``lc7k3f9q`` finds ``LC-7K3F9Q``), and answers the public projection only:
  the status, the product, the quantity, the total and until when the stock
  is reserved. No buyer id, no address, no seller;
- ``GET /{reference}/details`` also needs ``Authorization: Bearer`` with the
  order's access token, which the buyer received with the reference. It adds
  the unit price, the shipping address and the payment and expiry times.
  A missing or wrong token, or the token of another order, answers the very
  404 of an unknown reference, so the route tells a guesser nothing.

The routes that pay, give the address and follow the delivery join the
second tier with the milestones that implement them.
"""

from datetime import datetime
from typing import Annotated, Final, Self

from fastapi import APIRouter, Depends, HTTPException, Path, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from livecart_api.errors import NOT_FOUND, documented
from livecart_core.application.buyer_orders import LookUpOrder
from livecart_core.application.dto import BuyerOrderRecord, ShippingAddress
from livecart_core.domain.state_machines import OrderStatus

PREFIX: Final = "/api/v1/orders"
# A reference is 9 characters once canonical; what a buyer types may carry spaces.
MAX_TYPED_REFERENCE_LENGTH: Final = 32

Reference = Annotated[
    str,
    Path(
        max_length=MAX_TYPED_REFERENCE_LENGTH,
        description="LC- then six symbols, in any case, the hyphen optional.",
        examples=["LC-7K3F9Q"],
    ),
]

_order_token = HTTPBearer(
    auto_error=False,
    scheme_name="OrderAccessToken",
    description="The order's access token, delivered to the buyer with its reference.",
)


class OrderProduct(BaseModel):
    code: str
    variant: str
    name: str


class OrderStatusResponse(BaseModel):
    """What the reference alone shows of an order."""

    reference: str
    status: OrderStatus
    product: OrderProduct
    quantity: int
    total_minor: int
    currency: str
    reserved_until: datetime

    @classmethod
    def of(cls, found: BuyerOrderRecord) -> Self:
        order = found.order
        return cls(
            reference=order.reference.value,
            status=order.status,
            product=OrderProduct(
                code=found.product_code.value,
                variant=found.product_variant.value,
                name=found.product_name,
            ),
            quantity=order.quantity.value,
            total_minor=order.total.amount_minor,
            currency=order.total.currency,
            reserved_until=order.reserved_until,
        )


class OrderDetailsResponse(OrderStatusResponse):
    """What the order's access token adds to the public projection."""

    unit_price_minor: int
    shipping_address: ShippingAddress | None
    paid_at: datetime | None
    expired_at: datetime | None

    @classmethod
    def of(cls, found: BuyerOrderRecord) -> Self:
        order = found.order
        return cls(
            **OrderStatusResponse.of(found).model_dump(),
            unit_price_minor=order.unit_price.amount_minor,
            shipping_address=order.shipping_address,
            paid_at=order.paid_at,
            expired_at=order.expired_at,
        )


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)


def build_order_router(lookup: LookUpOrder) -> APIRouter:
    router = APIRouter(prefix=PREFIX, tags=["orders"])

    @router.get(
        "/{reference}",
        summary="An order's status, product and total, from its reference alone",
        responses=documented({404: "No order has this reference"}),
    )
    async def get_order(reference: Reference) -> OrderStatusResponse:
        found = await lookup.by_reference(reference)
        if found is None:
            raise _not_found()
        return OrderStatusResponse.of(found)

    @router.get(
        "/{reference}/details",
        summary="An order in full, shipping address included, with its access token",
        responses=documented(
            {404: "No order has this reference, or the token is missing or not its own"}
        ),
    )
    async def get_order_details(
        reference: Reference,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_order_token)],
    ) -> OrderDetailsResponse:
        token = None if credentials is None else credentials.credentials
        found = await lookup.with_token(reference, token)
        if found is None:
            raise _not_found()
        return OrderDetailsResponse.of(found)

    return router
