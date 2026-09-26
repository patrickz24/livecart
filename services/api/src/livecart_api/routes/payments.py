"""The payment bridge page: where the provider sends the buyer's browser back, to reopen the app.

``GET /api/v1/payments/return?status=success|cancel&ref=LC-XXXXXX`` is the
success and the cancel URL of every hosted session (``CheckoutPages``), on
``PUBLIC_API_BASE_URL``. It answers an HTML page, 200, with one button, a
plain link to the return URL the app gave its checkout, with the outcome
and the reference added: ``livecart://payment-return?status=success&reference=LC-7K3F9Q``.

- A page rather than a 302 to the app: Chrome, and the Custom Tabs the app
  opens the hosted page in, open another app only on a user gesture, so a
  redirect to ``livecart://`` would stop on a blank page. A tap on the
  button is that gesture;
- the URL carries the reference only, never the order's token: it goes
  through the provider, the browser's history and logs. The return URL is
  the one stored on the order's last payment attempt, which the checkout
  checked against ``ALLOWED_APP_SCHEMES``: nothing in this URL chooses
  where the button leads;
- ``success`` only says that the buyer left the provider's page after
  paying: the payment is the provider's webhook's to confirm, and the app
  reads the order's status to know it;
- the page escapes what it shows and runs no script (``livecart_api.pages``);
  an unknown or malformed reference, an unknown status, or an order that
  never opened a checkout all get the same 404 page;
- it is rate limited as the lookup by reference is, since it tells whether
  a reference exists: per client address, its not-found answers too.
"""

from typing import Annotated, Final
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import HTMLResponse

from livecart_api.pages import html_page, text
from livecart_api.rate_limits import RateLimiting, client_address
from livecart_core.application.buyer_orders import LookUpOrder
from livecart_core.application.checkout import BRIDGE_PATH, BridgeStatus
from livecart_core.application.ports import PaymentAttempts

# A reference is nine characters once canonical; what a link carries may be typed.
MAX_TYPED_REFERENCE_LENGTH: Final = 32

TITLES: Final = {
    BridgeStatus.SUCCESS: "Payment submitted",
    BridgeStatus.CANCEL: "Payment cancelled",
}
MESSAGES: Final = {
    BridgeStatus.SUCCESS: (
        "Thank you: the payment of order {reference} is being confirmed. The app shows its status."
    ),
    BridgeStatus.CANCEL: (
        "You left the payment page: order {reference} is not paid. "
        "You can pay it from the app while it is reserved."
    ),
}


def app_link(return_url: str, outcome: BridgeStatus, reference: str) -> str:
    """The return URL of the app, with the outcome and the reference added to it."""
    return f"{return_url}?{urlencode({'status': outcome.value, 'reference': reference})}"


def _outcome(value: str | None) -> BridgeStatus | None:
    try:
        return None if value is None else BridgeStatus(value)
    except ValueError:
        return None


def _not_found() -> HTMLResponse:
    return html_page(
        "Unknown payment",
        "<h1>Unknown payment</h1><p>No order goes with this link. Return to the app.</p>",
        status_code=status.HTTP_404_NOT_FOUND,
    )


def build_payment_bridge_router(
    lookup: LookUpOrder, payments: PaymentAttempts, limits: RateLimiting
) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    async def limit_lookups(request: Request) -> None:
        address = client_address(request)
        await limits.enforce(limits.rules.buyer_lookup, address)
        await limits.check(limits.rules.buyer_lookup_not_found, address)

    @router.get(BRIDGE_PATH, dependencies=[Depends(limit_lookups)])
    async def payment_return(
        request: Request,
        outcome: Annotated[str | None, Query(alias="status")] = None,
        ref: str | None = None,
    ) -> HTMLResponse:
        bridge = _outcome(outcome)
        found = None
        if bridge is not None and ref is not None and len(ref) <= MAX_TYPED_REFERENCE_LENGTH:
            found = await lookup.by_reference(ref)
        latest = None if found is None else await payments.latest(found.order.id)
        if bridge is None or found is None or latest is None:
            await limits.record_lookup_not_found(client_address(request))
            return _not_found()
        reference = found.order.reference.value
        link = app_link(latest.return_url, bridge, reference)
        body = (
            f"<h1>{text(TITLES[bridge])}</h1>"
            f"<p>{text(MESSAGES[bridge].format(reference=reference))}</p>"
            f'<a class="button" href="{text(link)}">Return to the app</a>'
        )
        return html_page(TITLES[bridge], body)

    return router
