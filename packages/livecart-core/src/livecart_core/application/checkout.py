"""``StartCheckout``: the buyer opens a hosted payment page for an order, with its token.

In the plan's order, each step its own unit, so that no transaction ever
waits on the provider:

1. the order, by its reference and its access token (``LookUpOrder``): any
   other token, or none, is the answer of an unknown reference. The return
   URL is checked against the app's schemes first, and the order must have
   a shipping address;
2. one transaction (``PaymentAttempts.open_attempt``): the reservation is
   extended to at least ``now + PAYMENT_GRACE_SECONDS``, guarded by
   ``status = 'pending_payment' AND reserved_until > now``, and attempt
   n + 1 opens, pending. The extension is committed before the provider is
   called: a sweep running meanwhile finds an attempt in flight and leaves
   the order to the later steps, which expire the provider's session first;
3. the provider's hosted session, created under the attempt's idempotency
   key ``checkout-session:{order_id}:{attempt_no}``, then its ref and
   redirect URL recorded on the attempt.

A second click while an attempt is pending gets the redirect URL recorded
on it, and extends nothing. An attempt left pending without a session (a
crash, or a provider down between steps 2 and 3) gets its session on the
next click, created again under the same key: the provider answers the same
session. A new attempt opens only once the previous one was settled (its
session expired, or its payment failed), ``PAYMENT_MAX_ATTEMPTS`` times at
most. The provider sends the buyer back to the bridge page of
``PUBLIC_API_BASE_URL`` (``CheckoutPages``), with the order's reference in
the URL, never its token.

The return URL is where the bridge page sends the buyer's browser back to
the app: a link of one of ``ALLOWED_APP_SCHEMES`` (``livecart://...``), or,
in development only, Expo Go's ``exp://`` on a private address. Nothing
else, http and https above all: the API never sends a browser to a place
a client named.
"""

import ipaddress
import re
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Final
from urllib.parse import urlsplit

from livecart_core.application.buyer_orders import LookUpOrder
from livecart_core.application.dto import (
    MAX_URL_LENGTH,
    AttemptOpeningKind,
    CheckoutRequest,
    PaymentRecord,
)
from livecart_core.application.ports import Clock, PaymentAttempts, PaymentProvider
from livecart_core.domain.reference import OrderReference

# Where the provider sends the buyer back: <PUBLIC_API_BASE_URL>/api/v1/payments/return.
BRIDGE_PATH: Final = "/api/v1/payments/return"
# Expo Go's scheme, which any phone with Expo Go opens: development only.
EXPO_GO_SCHEME: Final = "exp"
# <scheme>://<the rest>, the rest of RFC 3986's path and authority characters
# only: no query, no fragment, no space, no markup.
RETURN_URL_PATTERN: Final = re.compile(r"[a-z][a-z0-9+.-]*://[A-Za-z0-9._~:/@!$&'()*+,;=%\[\]-]*")
# Where a phone running Expo Go finds the developer's machine.
PRIVATE_NETWORKS: Final = tuple(
    ipaddress.ip_network(network)
    for network in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


class BridgeStatus(StrEnum):
    """How the buyer left the provider's page: the ``status`` of the bridge page's URL."""

    SUCCESS = "success"
    CANCEL = "cancel"


class InvalidReturnUrl(ValueError):
    """A return URL that is no link of the app."""


class CheckoutRefusal(StrEnum):
    ADDRESS_REQUIRED = "address_required"
    # The order is paid or expired, or its reservation has ended.
    NOT_PAYABLE = "not_payable"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"


class CheckoutRefused(Exception):
    """An order that cannot open a payment attempt now; ``reason`` says why."""

    def __init__(self, reason: CheckoutRefusal) -> None:
        super().__init__(f"checkout refused: {reason}")
        self.reason = reason


def _is_private_host(host: str) -> bool:
    """A host of the developer's own network: loopback, an RFC 1918 or a link-local address.

    Named explicitly rather than by ``is_private``, which also counts the
    documentation ranges and other reserved blocks.
    """
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in PRIVATE_NETWORKS)


class ReturnUrlPolicy:
    """Which return URLs a checkout takes: see the module docstring."""

    def __init__(self, schemes: frozenset[str], *, allow_expo_go: bool) -> None:
        self._schemes = schemes
        self._allow_expo_go = allow_expo_go

    def check(self, url: str) -> str:
        """``url`` when it is a link of the app; ``InvalidReturnUrl`` otherwise."""
        if len(url) > MAX_URL_LENGTH or not RETURN_URL_PATTERN.fullmatch(url):
            raise InvalidReturnUrl("not <scheme>://..., or with a query, a fragment or a space")
        try:
            parts = urlsplit(url)
            host = parts.hostname
        except ValueError as error:
            raise InvalidReturnUrl("an address in brackets that is no IPv6 address") from error
        if parts.scheme in self._schemes:
            return url
        expo_go = parts.scheme == EXPO_GO_SCHEME and self._allow_expo_go
        if expo_go and host is not None and _is_private_host(host):
            return url
        raise InvalidReturnUrl(f"the scheme {parts.scheme} is not one of the app's")


@dataclass(frozen=True, slots=True)
class CheckoutPages:
    """The URLs the provider sends the buyer back to: the bridge page, with the reference only.

    Built from ``PUBLIC_API_BASE_URL``, the same in the API, which creates a
    session, and in the worker, which creates it again for an attempt left
    without one: a provider refuses an idempotency key sent again with other
    URLs.
    """

    public_base_url: str

    def bridge_url(self, reference: OrderReference, status: BridgeStatus) -> str:
        base = self.public_base_url.rstrip("/")
        return f"{base}{BRIDGE_PATH}?status={status.value}&ref={reference.value}"

    def request_for(self, reference: OrderReference, payment: PaymentRecord) -> CheckoutRequest:
        """The session to create for an attempt: its amount, and the bridge page."""
        return CheckoutRequest(
            order_id=payment.order_id,
            attempt_no=payment.attempt_no,
            reference=reference,
            amount=payment.amount,
            success_url=self.bridge_url(reference, BridgeStatus.SUCCESS),
            cancel_url=self.bridge_url(reference, BridgeStatus.CANCEL),
        )


@dataclass(frozen=True, slots=True)
class StartedCheckout:
    """Where the buyer's browser goes to pay, and the attempt it pays."""

    redirect_url: str
    attempt_no: int


class StartCheckout:
    """Opens, or finds again, the payment attempt of an order; see the module docstring."""

    def __init__(
        self,
        lookup: LookUpOrder,
        payments: PaymentAttempts,
        provider: PaymentProvider,
        clock: Clock,
        *,
        pages: CheckoutPages,
        return_urls: ReturnUrlPolicy,
        grace: timedelta,
        max_attempts: int,
    ) -> None:
        self._lookup = lookup
        self._payments = payments
        self._provider = provider
        self._clock = clock
        self._pages = pages
        self._return_urls = return_urls
        self._grace = grace
        self._max_attempts = max_attempts

    async def start(
        self, reference: str, token: str | None, return_url: str
    ) -> StartedCheckout | None:
        """The hosted page of the order's attempt; ``None`` for an unknown order or a wrong token.

        Raises ``InvalidReturnUrl``, then ``CheckoutRefused``; a provider or a
        database that fails raises ``ServiceUnavailable``, the attempt left
        for the next click.
        """
        checked = self._return_urls.check(return_url)
        found = await self._lookup.with_token(reference, token)
        if found is None:
            return None
        order = found.order
        if order.shipping_address is None:
            raise CheckoutRefused(CheckoutRefusal.ADDRESS_REQUIRED)
        opening = await self._payments.open_attempt(
            order.id,
            provider=self._provider.name,
            return_url=checked,
            now=self._clock.now(),
            grace=self._grace,
            max_attempts=self._max_attempts,
        )
        payment = opening.payment
        if payment is None:
            exhausted = opening.kind is AttemptOpeningKind.EXHAUSTED
            raise CheckoutRefused(
                CheckoutRefusal.ATTEMPTS_EXHAUSTED if exhausted else CheckoutRefusal.NOT_PAYABLE
            )
        if payment.redirect_url is not None:
            return StartedCheckout(payment.redirect_url, payment.attempt_no)
        session = await self._provider.create_checkout(
            self._pages.request_for(order.reference, payment)
        )
        recorded = await self._payments.record_checkout(
            payment.id, provider_ref=session.provider_ref, redirect_url=session.redirect_url
        )
        # The first session recorded stays: a concurrent click's, the same one.
        return StartedCheckout(recorded.redirect_url or session.redirect_url, recorded.attempt_no)
