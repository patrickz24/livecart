"""``PaymentProvider`` in development: a hosted checkout that needs no account and no key.

The mock stands for a provider such as Stripe, and keeps its sessions where a
provider would, outside every LiveCart process: in Redis, under
``{prefix}:mock-payments:``, so that the API, which creates the sessions and
serves their hosted page, and the worker, which expires them, see the same
ones. The processes themselves hold nothing, and every name is derived:

- a session's ref is ``mock_cs_`` and 40 hexadecimal digits of the HMAC of
  its request's idempotency key under ``MOCK_PAYMENT_WEBHOOK_SECRET``: the
  same attempt always names the same session, and nobody without the key can
  guess another buyer's. The session's data (the order, the attempt, the
  reference, the amount and the URLs to send the buyer back to) is written
  once, with ``SET NX``: a request sent again with the same key finds it and
  gets the same session, and a request with the same key and other
  parameters raises ``ValueError``, as Stripe refuses it;
- the hosted page is ``{PUBLIC_API_BASE_URL}/api/v1/mock-payments/<ref>``,
  served by the API in development only, with a Pay and a Cancel button;
- a session leaves ``open`` once, by ``SET NX`` of its outcome: ``complete``
  when the buyer pays on the page, ``expired`` when the sweep expires it.
  Whichever comes first wins, and the other finds the session closed: the
  race between a late payment and the release of the stock is settled here,
  as Stripe settles it (expire-first);
- paying produces one ``checkout.session.completed`` event, whose id and
  payment ref are derived from the session's ref, signed with
  ``livecart_core.signing`` under the same key: the API posts it to its
  payment webhook, like a provider would, and ``verify_webhook`` checks it
  there. The hosted page itself never holds the key.

A session lasts a week in Redis, far beyond any reservation. Any Redis error
raises ``ServiceUnavailable``. The mock is a development tool: the API
serves its hosted page only outside production, and refuses to start with
``PAYMENT_PROVIDER=mock`` in production (``livecart_core.infrastructure.payments``).
"""

import hashlib
import hmac
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, SecretStr
from redis.asyncio import Redis
from redis.exceptions import RedisError

from livecart_core.application.dto import (
    CheckoutEventType,
    CheckoutPaymentStatus,
    CheckoutRequest,
    CheckoutSession,
    CheckoutStatus,
    PaymentEvent,
    PaymentProviderName,
)
from livecart_core.application.ports import Clock, RecordNotFound, ServiceUnavailable
from livecart_core.domain.money import Money
from livecart_core.domain.reference import OrderReference
from livecart_core.signing import SIGNATURE_HEADER, WebhookSigner, header_value

# Where the API serves the hosted page of a session, under PUBLIC_API_BASE_URL.
MOCK_PAYMENTS_PATH: Final = "/api/v1/mock-payments"
SESSION_REF_PREFIX: Final = "mock_cs_"
PAYMENT_REF_PREFIX: Final = "mock_pi_"
EVENT_ID_PREFIX: Final = "evt_mock_"
# How long Redis keeps a session and its outcome: a week.
MOCK_SESSION_TTL_S: Final = 7 * 86_400
# A session whose data expires between the failed SET NX and the GET is written again, once.
CREATE_TRIES: Final = 2


@asynccontextmanager
async def _redis_errors() -> AsyncIterator[None]:
    try:
        yield
    except RedisError as error:
        raise ServiceUnavailable(
            f"mock payment store unavailable: {type(error).__name__}"
        ) from error


class _StoredSession(BaseModel):
    """What a session is created with, and never changes: the JSON of its Redis key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    order_id: UUID
    attempt_no: int
    reference: str
    amount_minor: int
    currency: str
    success_url: str
    cancel_url: str

    @classmethod
    def of(cls, request: CheckoutRequest) -> Self:
        return cls(
            order_id=request.order_id,
            attempt_no=request.attempt_no,
            reference=request.reference.value,
            amount_minor=request.amount.amount_minor,
            currency=request.amount.currency,
            success_url=request.success_url,
            cancel_url=request.cancel_url,
        )


class _Outcome(BaseModel):
    """How a session left ``open``, and when: the JSON of its outcome key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: CheckoutStatus
    at: AwareDatetime


@dataclass(frozen=True, slots=True)
class MockHostedSession:
    """A session as its hosted page shows it: what the buyer pays, and where the page goes next."""

    session: CheckoutSession
    reference: OrderReference
    success_url: str
    cancel_url: str
    # When the session was paid, for the event of the payment.
    completed_at: datetime | None = None


class MockPaymentProvider:
    """The mock provider; see the module docstring."""

    def __init__(
        self,
        client: Redis,
        *,
        prefix: str,
        secret: SecretStr,
        clock: Clock,
        public_base_url: str,
        ttl_seconds: int = MOCK_SESSION_TTL_S,
    ) -> None:
        self._client = client
        self._prefix = prefix
        self._signer = WebhookSigner(secret)
        self._key = secret.get_secret_value().encode()
        self._clock = clock
        self._pages = f"{public_base_url.rstrip('/')}{MOCK_PAYMENTS_PATH}"
        self._ttl_seconds = ttl_seconds

    def __repr__(self) -> str:
        return f"MockPaymentProvider(prefix={self._prefix!r})"

    @property
    def name(self) -> PaymentProviderName:
        return PaymentProviderName.MOCK

    # --- The names, all derived --------------------------------------------------------

    def _derive(self, purpose: str, value: str, digits: int) -> str:
        message = f"{purpose}:{value}".encode()
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()[:digits]

    def session_ref(self, idempotency_key: str) -> str:
        """The ref of the session an idempotency key names."""
        return SESSION_REF_PREFIX + self._derive("checkout-session", idempotency_key, 40)

    def payment_ref(self, session_ref: str) -> str:
        return PAYMENT_REF_PREFIX + self._derive("payment", session_ref, 24)

    def event_id(self, session_ref: str) -> str:
        return EVENT_ID_PREFIX + self._derive("event-completed", session_ref, 24)

    def hosted_page_url(self, session_ref: str) -> str:
        return f"{self._pages}/{session_ref}"

    def session_key(self, session_ref: str) -> str:
        return f"{self._prefix}:mock-payments:session:{session_ref}"

    def outcome_key(self, session_ref: str) -> str:
        return f"{self._prefix}:mock-payments:outcome:{session_ref}"

    # --- PaymentProvider ---------------------------------------------------------------

    async def create_checkout(self, request: CheckoutRequest) -> CheckoutSession:
        ref = self.session_ref(request.idempotency_key)
        wanted = _StoredSession.of(request)
        async with _redis_errors():
            stored = await self._create(ref, wanted)
            outcome = await self._client.get(self.outcome_key(ref))
        if stored != wanted:
            raise ValueError(f"key {request.idempotency_key} reused with other parameters")
        return self._hosted(ref, stored, outcome).session

    async def expire_checkout(self, provider_ref: str) -> bool:
        async with _redis_errors():
            if not await self._client.exists(self.session_key(provider_ref)):
                raise RecordNotFound(f"no mock checkout session {provider_ref}")
            return bool(await self._settle(provider_ref, CheckoutStatus.EXPIRED))

    async def get_checkout(self, provider_ref: str) -> CheckoutSession:
        return (await self.hosted_session(provider_ref)).session

    async def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> PaymentEvent:
        self._signer.verify(
            header_value(headers, SIGNATURE_HEADER), raw_body, now=self._clock.now().timestamp()
        )
        # pydantic's ValidationError is a ValueError, as the port promises.
        event = PaymentEvent.model_validate_json(raw_body)
        if event.provider is not PaymentProviderName.MOCK:
            raise ValueError("a verified mock webhook names another provider")
        return event

    # --- The hosted page's side --------------------------------------------------------

    async def hosted_session(self, provider_ref: str) -> MockHostedSession:
        """The session, what it charges and where its buyer goes next; ``RecordNotFound``."""
        async with _redis_errors():
            raw, outcome = await self._client.mget(
                self.session_key(provider_ref), self.outcome_key(provider_ref)
            )
        if raw is None:
            raise RecordNotFound(f"no mock checkout session {provider_ref}")
        return self._hosted(provider_ref, _StoredSession.model_validate_json(raw), outcome)

    async def pay(self, provider_ref: str) -> MockHostedSession:
        """The buyer pays on the hosted page: the session as it stands after the attempt.

        ``complete`` when this call or an earlier one paid it, ``expired`` when
        the sweep expired it first: a session never leaves ``open`` twice.
        """
        await self.hosted_session(provider_ref)
        async with _redis_errors():
            await self._settle(provider_ref, CheckoutStatus.COMPLETE)
        return await self.hosted_session(provider_ref)

    def completed_event(self, hosted: MockHostedSession) -> PaymentEvent:
        """The ``checkout.session.completed`` event of a paid session, the same every time."""
        session = hosted.session
        if session.status is not CheckoutStatus.COMPLETE or hosted.completed_at is None:
            raise ValueError(f"session {session.provider_ref} is {session.status}, not complete")
        return PaymentEvent(
            provider=PaymentProviderName.MOCK,
            event_id=self.event_id(session.provider_ref),
            event_type=CheckoutEventType.COMPLETED.value,
            checkout_ref=session.provider_ref,
            payment_ref=session.payment_ref,
            payment_status=CheckoutPaymentStatus.PAID,
            amount_minor=session.amount.amount_minor,
            currency=session.amount.currency,
            occurred_at=hosted.completed_at,
        )

    def signed_webhook(self, event: PaymentEvent) -> tuple[dict[str, str], bytes]:
        """The headers and the body of the webhook of ``event``, signed now."""
        body = event.model_dump_json().encode()
        timestamp = int(self._clock.now().timestamp())
        return {SIGNATURE_HEADER: self._signer.header(body, timestamp=timestamp)}, body

    # --- Redis -------------------------------------------------------------------------

    async def _create(self, ref: str, wanted: _StoredSession) -> _StoredSession:
        """The session's data: ``wanted``, written now, or what an earlier call wrote."""
        key = self.session_key(ref)
        for _ in range(CREATE_TRIES):
            if await self._client.set(key, wanted.model_dump_json(), nx=True, ex=self._ttl_seconds):
                return wanted
            raw = await self._client.get(key)
            if raw is not None:
                return _StoredSession.model_validate_json(raw)
        raise ServiceUnavailable("the mock checkout session kept expiring while it was created")

    async def _settle(self, ref: str, status: CheckoutStatus) -> bool:
        """Take the session out of ``open`` with ``status``, unless it left it already."""
        outcome = _Outcome(status=status, at=self._clock.now())
        return bool(
            await self._client.set(
                self.outcome_key(ref), outcome.model_dump_json(), nx=True, ex=self._ttl_seconds
            )
        )

    def _hosted(
        self, ref: str, stored: _StoredSession, raw_outcome: bytes | str | None
    ) -> MockHostedSession:
        outcome = None if raw_outcome is None else _Outcome.model_validate_json(raw_outcome)
        status = CheckoutStatus.OPEN if outcome is None else outcome.status
        complete = status is CheckoutStatus.COMPLETE
        session = CheckoutSession(
            provider_ref=ref,
            redirect_url=self.hosted_page_url(ref),
            status=status,
            amount=Money(stored.amount_minor, stored.currency),
            payment_ref=self.payment_ref(ref) if complete else None,
            # The mock completes a session only once the buyer paid it.
            payment_status=CheckoutPaymentStatus.PAID if complete else None,
        )
        return MockHostedSession(
            session=session,
            reference=OrderReference(stored.reference),
            success_url=stored.success_url,
            cancel_url=stored.cancel_url,
            completed_at=outcome.at if outcome is not None and complete else None,
        )
