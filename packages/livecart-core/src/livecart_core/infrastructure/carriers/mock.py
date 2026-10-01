"""``CarrierProvider`` in development: a carrier that keeps no state at all.

Everything the mock says of a shipment is derived, so that any number of
workers, and a worker restarted, agree on it without a store:

- a shipment's carrier ref is ``mock_shp_`` and 32 hexadecimal digits of the
  HMAC-SHA256 of its order's id under ``MOCK_CARRIER_SECRET``: creating the
  shipment of an order again names the same one, so a crash between creating
  it and recording it is harmless;
- its tracking number is ``MOCK-`` and 16 symbols of Crockford's base 32 (80
  bits) of the HMAC of its carrier ref under the same key: what the buyer
  follows, which nobody can guess from the order's id or reference without
  the key. It is also the mock's proof that it issued a ref: a ref whose
  number does not match is ``RecordNotFound``;
- its status is computed from the ref's ``created_at`` and the ``Clock``:
  ``created``, then one step of the route every ``MOCK_CARRIER_STEP_SECONDS``
  (120 s by default), ``in_transit``, ``out_for_delivery``, and
  ``delivered`` for good after three steps. The time of its latest event is
  the start of the step it reached;
- a cancellation is accepted while the shipment is ``created`` and refused
  with ``InvalidTransition`` once it is in transit. The mock records nothing,
  so its tracking goes on as if nothing happened: LiveCart's row holds the
  cancellation, and a cancelled shipment is never tracked again.

``created_at`` is the clock's time of the call that created the shipment: a
later call names the same shipment with a later time, and LiveCart keeps the
first one it recorded (``shipments.carrier_created_at``), which it hands back
with the ref, so that the route starts once. The mock calls nothing, so it
never fails with ``ServiceUnavailable``.
"""

import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Final

from pydantic import SecretStr

from livecart_core.application.dto import (
    CarrierName,
    ShipmentRef,
    ShipmentRequest,
    TrackingUpdate,
)
from livecart_core.application.ports import Clock, RecordNotFound
from livecart_core.domain.reference import CROCKFORD_ALPHABET
from livecart_core.domain.shipments import SHIPMENT_ROUTE
from livecart_core.domain.state_machines import SHIPMENT_TRANSITIONS, ShipmentStatus, transition

CARRIER_REF_PREFIX: Final = "mock_shp_"
TRACKING_NUMBER_PREFIX: Final = "MOCK-"
# 128 bits of the shipment's HMAC, and 80 of the tracking number's.
CARRIER_REF_DIGITS: Final = 32
TRACKING_NUMBER_SYMBOLS: Final = 16
# How long a shipment stays at each step of its route, by default.
MOCK_CARRIER_STEP: Final = timedelta(seconds=120)
# What each HMAC signs before its data, so that the key's two uses never meet.
_SHIPMENT_LABEL: Final = b"livecart-mock-carrier:shipment:"
_TRACKING_LABEL: Final = b"livecart-mock-carrier:tracking:"
_BITS_PER_SYMBOL: Final = 5


def _crockford(digest: bytes, symbols: int) -> str:
    """The first ``symbols`` five-bit groups of ``digest``, in Crockford's base 32."""
    value = int.from_bytes(digest, "big") >> (len(digest) * 8 - symbols * _BITS_PER_SYMBOL)
    mask = len(CROCKFORD_ALPHABET) - 1
    return "".join(
        CROCKFORD_ALPHABET[(value >> (_BITS_PER_SYMBOL * shift)) & mask]
        for shift in reversed(range(symbols))
    )


class MockCarrier:
    """A carrier whose every answer is derived: see the module docstring."""

    def __init__(
        self, secret: SecretStr, clock: Clock, *, step: timedelta = MOCK_CARRIER_STEP
    ) -> None:
        if step <= timedelta(0):
            raise ValueError(f"a step of the route must be positive, not {step}")
        self._key = secret.get_secret_value().encode()
        self._clock = clock
        self._step = step

    @property
    def name(self) -> CarrierName:
        return CarrierName.MOCK

    async def create_shipment(self, request: ShipmentRequest) -> ShipmentRef:
        digest = self._mac(_SHIPMENT_LABEL + request.order_id.bytes)
        carrier_ref = f"{CARRIER_REF_PREFIX}{digest.hex()[:CARRIER_REF_DIGITS]}"
        return ShipmentRef(
            CarrierName.MOCK, carrier_ref, self._tracking_number(carrier_ref), self._clock.now()
        )

    async def get_tracking(self, ref: ShipmentRef) -> TrackingUpdate:
        self._check(ref)
        steps = self._steps_since(ref.created_at)
        return TrackingUpdate(SHIPMENT_ROUTE[steps], ref.created_at + steps * self._step)

    async def cancel_shipment(self, ref: ShipmentRef) -> None:
        current = await self.get_tracking(ref)
        # Created: the cancellation is accepted; in transit or beyond: InvalidTransition.
        transition(SHIPMENT_TRANSITIONS, current.status, ShipmentStatus.CANCELLED)

    def _steps_since(self, created_at: datetime) -> int:
        """The steps of the route the shipment took: none before its creation, three at most."""
        elapsed = self._clock.now() - created_at
        if elapsed <= timedelta(0):
            return 0
        return min(elapsed // self._step, len(SHIPMENT_ROUTE) - 1)

    def _check(self, ref: ShipmentRef) -> None:
        """Raise ``RecordNotFound`` for a ref the mock did not issue under its key."""
        issued = (
            ref.carrier is CarrierName.MOCK
            and ref.carrier_ref.startswith(CARRIER_REF_PREFIX)
            and hmac.compare_digest(ref.tracking_number, self._tracking_number(ref.carrier_ref))
        )
        if not issued:
            raise RecordNotFound("the mock carrier issued no such shipment")

    def _tracking_number(self, carrier_ref: str) -> str:
        digest = self._mac(_TRACKING_LABEL + carrier_ref.encode())
        return f"{TRACKING_NUMBER_PREFIX}{_crockford(digest, TRACKING_NUMBER_SYMBOLS)}"

    def _mac(self, message: bytes) -> bytes:
        return hmac.new(self._key, message, hashlib.sha256).digest()
