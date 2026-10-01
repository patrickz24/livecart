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

The routes that act on the order are of the second tier too, with the same
404 for a token that is not the order's:

- ``PUT /{reference}/shipping-address`` records the delivery address, which
  pydantic validates (``ShippingAddress``), while the order waits for its
  payment (409 once it is paid or expired);
- ``POST /{reference}/checkout`` opens the provider's hosted page
  (``application.checkout.StartCheckout``) and answers its URL, for the app
  to open. ``return_url`` is the link of the app the bridge page sends the
  buyer back to, one of ``ALLOWED_APP_SCHEMES`` (422 otherwise). The order
  needs an address first, then a reservation still running and an attempt
  left (409 otherwise). A second click while the attempt is pending answers
  the same URL;
- ``GET /{reference}/tracking`` answers where the paid order's parcel is:
  its carrier, its tracking number, its status and the time of the
  carrier's latest event, as the worker's tracking last recorded them. An
  order without a shipment yet answers 404 as well, the same body: the
  token holder knows from the order's status whether it is paid.

All are rate limited (``livecart_api.rate_limits``): the lookup per client
address, its not-found answers too, so that guessing references is slow, and
the token routes per access token, the checkout within them to 5 a minute.
The not-found answers of the token routes, those of a wrong or missing token
included, count against the client address with the lookup's: a client that
sends a new token value with every request gets no new window from it.
"""

from datetime import datetime
from typing import Annotated, Final, Self

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from livecart_api.errors import NOT_FOUND, documented
from livecart_api.rate_limits import (
    TOO_MANY_REQUESTS,
    RateLimiting,
    client_address,
    token_subject,
)
from livecart_core.application.buyer_orders import (
    LookUpOrder,
    OrderTracking,
    SetShippingAddress,
    TrackOrder,
)
from livecart_core.application.checkout import StartCheckout
from livecart_core.application.dto import (
    MAX_URL_LENGTH,
    BuyerOrderRecord,
    CarrierName,
    ShipmentRecord,
    ShippingAddress,
)
from livecart_core.domain.state_machines import OrderStatus, ShipmentStatus

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


class TrackingResponse(BaseModel):
    """Where the order's parcel is, as its carrier last reported it."""

    reference: str
    carrier: CarrierName
    tracking_number: str
    status: ShipmentStatus
    last_event_at: datetime = Field(
        description="The time of the carrier's latest event: the shipment's creation at first."
    )

    @classmethod
    def of(cls, tracking: OrderTracking, shipment: ShipmentRecord) -> Self:
        return cls(
            reference=tracking.order.order.reference.value,
            carrier=shipment.ref.carrier,
            tracking_number=shipment.ref.tracking_number,
            status=shipment.status,
            last_event_at=shipment.last_event_at,
        )


class CheckoutBody(BaseModel):
    """Where the bridge page sends the buyer back once the provider's page is done."""

    model_config = ConfigDict(extra="forbid")

    return_url: str = Field(
        min_length=1,
        max_length=MAX_URL_LENGTH,
        description="A link of the app, such as livecart://payment-return: no query, no fragment.",
        examples=["livecart://payment-return"],
    )


class CheckoutResponse(BaseModel):
    """The provider's hosted page for the order's payment attempt."""

    redirect_url: str
    attempt_no: int


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)


def _token(credentials: HTTPAuthorizationCredentials | None) -> str | None:
    return None if credentials is None else credentials.credentials


def build_order_router(
    lookup: LookUpOrder,
    addresses: SetShippingAddress,
    checkout: StartCheckout,
    tracking: TrackOrder,
    limits: RateLimiting,
) -> APIRouter:
    router = APIRouter(
        prefix=PREFIX, tags=["orders"], responses=documented({429: TOO_MANY_REQUESTS})
    )

    async def limit_lookups(request: Request) -> None:
        address = client_address(request)
        await limits.enforce(limits.rules.buyer_lookup, address)
        # Past its not-found answers, an address finds nothing more, existing orders included.
        await limits.check(limits.rules.buyer_lookup_not_found, address)

    async def limit_token_requests(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_order_token)],
    ) -> None:
        address = client_address(request)
        # First, so that a refused request opens no counter for the token it sent.
        await limits.check(limits.rules.buyer_lookup_not_found, address)
        subject = address if credentials is None else token_subject(credentials.credentials)
        await limits.enforce(limits.rules.buyer_token, subject)

    async def limit_checkouts(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_order_token)],
    ) -> None:
        subject = (
            client_address(request)
            if credentials is None
            else token_subject(credentials.credentials)
        )
        await limits.enforce(limits.rules.checkout, subject)

    @router.get(
        "/{reference}",
        summary="An order's status, product and total, from its reference alone",
        dependencies=[Depends(limit_lookups)],
        responses=documented({404: "No order has this reference"}),
    )
    async def get_order(reference: Reference, request: Request) -> OrderStatusResponse:
        found = await lookup.by_reference(reference)
        if found is None:
            await limits.record_lookup_not_found(client_address(request))
            raise _not_found()
        return OrderStatusResponse.of(found)

    @router.get(
        "/{reference}/details",
        summary="An order in full, shipping address included, with its access token",
        dependencies=[Depends(limit_token_requests)],
        responses=documented(
            {404: "No order has this reference, or the token is missing or not its own"}
        ),
    )
    async def get_order_details(
        reference: Reference,
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_order_token)],
    ) -> OrderDetailsResponse:
        found = await lookup.with_token(reference, _token(credentials))
        if found is None:
            # Against the address, never the token: any value sent opens a counter of its
            # own, while the verified tokens of one shared address (CGNAT) never fill this one.
            await limits.record_lookup_not_found(client_address(request))
            raise _not_found()
        return OrderDetailsResponse.of(found)

    @router.put(
        "/{reference}/shipping-address",
        summary="Record where the order goes, with its access token",
        dependencies=[Depends(limit_token_requests)],
        responses=documented(
            {
                404: "No order has this reference, or the token is missing or not its own",
                409: "The order is paid or expired: its address no longer changes",
            }
        ),
    )
    async def put_shipping_address(
        reference: Reference,
        address: ShippingAddress,
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_order_token)],
    ) -> OrderDetailsResponse:
        found = await addresses.set(reference, _token(credentials), address)
        if found is None:
            await limits.record_lookup_not_found(client_address(request))
            raise _not_found()
        return OrderDetailsResponse.of(found)

    @router.get(
        "/{reference}/tracking",
        summary="Where the order's parcel is, with the order's access token",
        dependencies=[Depends(limit_token_requests)],
        responses=documented(
            {
                404: (
                    "No order has this reference, the token is missing or not its own, "
                    "or the order has no shipment yet"
                )
            }
        ),
    )
    async def get_order_tracking(
        reference: Reference,
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_order_token)],
    ) -> TrackingResponse:
        tracked = await tracking.track(reference, _token(credentials))
        if tracked is None:
            await limits.record_lookup_not_found(client_address(request))
            raise _not_found()
        if tracked.shipment is None:
            # The token holds: nothing shipped yet, which costs the address nothing.
            raise _not_found()
        return TrackingResponse.of(tracked, tracked.shipment)

    @router.post(
        "/{reference}/checkout",
        summary="Open the payment provider's hosted page, with the order's access token",
        dependencies=[Depends(limit_token_requests), Depends(limit_checkouts)],
        responses=documented(
            {
                404: "No order has this reference, or the token is missing or not its own",
                409: (
                    "No address yet, the order is paid or expired, its reservation has "
                    "ended, or its payment attempts are used up"
                ),
                422: "return_url is no link of the app",
                503: "The payment provider or the database is unavailable: try again",
            }
        ),
    )
    async def start_checkout(
        reference: Reference,
        body: CheckoutBody,
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_order_token)],
    ) -> CheckoutResponse:
        started = await checkout.start(reference, _token(credentials), body.return_url)
        if started is None:
            await limits.record_lookup_not_found(client_address(request))
            raise _not_found()
        return CheckoutResponse(redirect_url=started.redirect_url, attempt_no=started.attempt_no)

    return router
