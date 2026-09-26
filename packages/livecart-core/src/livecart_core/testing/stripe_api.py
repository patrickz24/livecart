"""The part of Stripe's API the Stripe adapter calls, in memory, behind an httpx2 transport.

``FakeStripeApi.transport`` answers what stripe-python sends to
``https://api.stripe.com/v1/checkout/sessions``, the way Stripe documents
it, so that ``StripePaymentProvider`` runs its real code, the SDK's request
encoding and error mapping included, without the network:

- ``POST /v1/checkout/sessions`` creates an open session from the form the
  SDK encodes, under its ``Idempotency-Key``: the same key and parameters
  replay the first answer, with ``Idempotent-Replayed: true``, as Stripe
  does for 24 hours, and other parameters under the key answer 400
  ``idempotency_error``;
- ``GET /v1/checkout/sessions/{id}`` answers the session as it stands, 404
  ``resource_missing`` for an id it never made;
- ``POST /v1/checkout/sessions/{id}/expire`` expires an open session, and
  answers 400 for one no longer open.

``pay`` is the buyer paying on the hosted page (a no-op on a session that
left ``open``), ``event`` builds the webhook body Stripe would post for a
``PaymentEvent``, customer details included, and ``signed`` signs it as
Stripe does. ``down`` makes every call fail on the network.
"""

import json
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import parse_qsl

import httpx2
import stripe
from pydantic import SecretStr

from livecart_core.application.dto import PaymentEvent

API_BASE: Final = "https://api.stripe.com"
SESSIONS_PATH: Final = "/v1/checkout/sessions"
HOSTED_PAGE: Final = "https://checkout.stripe.com/c/pay"
# What Stripe answers a payment's customer: what the allow-list must drop.
CUSTOMER_DETAILS: Final = {"email": "buyer@example.invalid", "name": "Ada Buyer"}


def _error(status: int, error_type: str, message: str, code: str | None = None) -> httpx2.Response:
    body: dict[str, Any] = {"type": error_type, "message": message}
    if code is not None:
        body["code"] = code
    return httpx2.Response(status, json={"error": body}, headers={"Request-Id": "req_fake"})


class FakeStripeApi:
    """Stripe's Checkout Sessions in memory; see the module docstring."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        # Idempotency-Key -> (the form it came with, the first answer's body).
        self._keys: dict[str, tuple[list[tuple[str, str]], bytes]] = {}
        self.requests: list[httpx2.Request] = []
        self.down = False

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        if self.down:
            raise httpx2.ConnectError("connection refused", request=request)
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == SESSIONS_PATH:
            return self._create(request)
        if not path.startswith(f"{SESSIONS_PATH}/"):
            return _error(404, "invalid_request_error", f"Unrecognized request URL ({path})")
        ref, _, action = path.removeprefix(f"{SESSIONS_PATH}/").partition("/")
        session = self.sessions.get(ref)
        if session is None:
            message = f"No such checkout.session: '{ref}'"
            return _error(404, "invalid_request_error", message, "resource_missing")
        if request.method == "GET" and not action:
            return httpx2.Response(200, json=session)
        if request.method == "POST" and action == "expire":
            if session["status"] != "open":
                message = 'Only Checkout Sessions with a status in ["open"] can be expired.'
                return _error(400, "invalid_request_error", message)
            session |= {"status": "expired", "url": None}
            return httpx2.Response(200, json=session)
        return _error(404, "invalid_request_error", f"Unrecognized request URL ({path})")

    def _create(self, request: httpx2.Request) -> httpx2.Response:
        form = sorted(parse_qsl(request.content.decode(), keep_blank_values=True))
        key = request.headers.get("Idempotency-Key")
        if key is not None and key in self._keys:
            first_form, first_answer = self._keys[key]
            if first_form != form:
                return _error(
                    400,
                    "idempotency_error",
                    "Keys for idempotent requests can only be used with the same parameters "
                    "they were first used with.",
                )
            return httpx2.Response(
                200, content=first_answer, headers={"Idempotent-Replayed": "true"}
            )
        fields = dict(form)
        ref = f"cs_test_fake{len(self.sessions) + 1:04d}"
        session: dict[str, Any] = {
            "id": ref,
            "object": "checkout.session",
            "mode": fields["mode"],
            "status": "open",
            "payment_status": "unpaid",
            "url": f"{HOSTED_PAGE}/{ref}",
            "amount_total": int(fields["line_items[0][price_data][unit_amount]"]),
            "currency": fields["line_items[0][price_data][currency]"],
            "payment_intent": None,
            "client_reference_id": fields["client_reference_id"],
            "metadata": {
                name.removeprefix("metadata[").removesuffix("]"): value
                for name, value in form
                if name.startswith("metadata[")
            },
            "success_url": fields["success_url"],
            "cancel_url": fields["cancel_url"],
            "customer_details": None,
        }
        self.sessions[ref] = session
        answer = json.dumps(session).encode()
        if key is not None:
            self._keys[key] = (form, answer)
        return httpx2.Response(200, content=answer, headers={"Request-Id": "req_fake"})

    def pay(self, ref: str) -> None:
        """The buyer pays an open session on Stripe's page; a closed one stays as it is."""
        session = self.sessions[ref]
        if session["status"] == "open":
            session |= {
                "status": "complete",
                "payment_status": "paid",
                "payment_intent": f"pi_test_fake{ref.removeprefix('cs_test_fake')}",
                "url": None,
                "customer_details": CUSTOMER_DETAILS,
            }

    @staticmethod
    def event(event: PaymentEvent) -> dict[str, Any]:
        """The body Stripe posts for ``event``, a checkout session's, customer details included."""
        session: dict[str, Any] = {
            "id": event.checkout_ref,
            "object": "checkout.session",
            "payment_intent": event.payment_ref,
            "payment_status": None if event.payment_status is None else event.payment_status.value,
            "amount_total": event.amount_minor,
            "currency": None if event.currency is None else event.currency.lower(),
            "customer_details": CUSTOMER_DETAILS,
        }
        return {
            "id": event.event_id,
            "object": "event",
            "api_version": stripe.api_version,
            "created": int(event.occurred_at.timestamp()),
            "livemode": False,
            "type": event.event_type,
            "data": {"object": session},
        }

    @staticmethod
    def sign(body: bytes, secret: SecretStr, *, at: datetime | None = None) -> dict[str, str]:
        """The ``Stripe-Signature`` header of ``body``, as Stripe signs it, now or ``at``."""
        timestamp = int((at if at is not None else datetime.now(UTC)).timestamp())
        header = stripe.WebhookSignature.generate_signature_header(
            body.decode(), secret.get_secret_value(), timestamp=timestamp
        )
        return {"Stripe-Signature": str(header)}

    def signed(self, event: PaymentEvent, secret: SecretStr) -> tuple[dict[str, str], bytes]:
        body = json.dumps(self.event(event)).encode()
        return self.sign(body, secret), body
