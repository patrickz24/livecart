"""The carriers behind ``CarrierProvider``, one of which ``CARRIER_PROVIDER`` selects.

The mock is the default: it needs no account, keeps no state and derives
every answer from ``MOCK_CARRIER_SECRET`` and the clock. DPD, Chronopost and
DHL are documented stubs. ``build_carrier`` builds the selected one when the
worker starts, the one process that calls the carrier, so that a
misconfiguration stops it before it takes a payment it cannot ship: a stub
raises ``NotImplementedError``, the mock without its key a ``ValueError``.

Unlike the payment mock, the carrier's is not refused in production:
LiveCart has no real carrier yet, and its payments run in Stripe's test mode
only, so nothing a deployment sells ships for real either. Its tracking
numbers mean nothing outside LiveCart.
"""

from typing import Final

from livecart_core.application.dto import CarrierName
from livecart_core.application.ports import CarrierProvider, Clock
from livecart_core.infrastructure.carriers.chronopost import ChronopostCarrier
from livecart_core.infrastructure.carriers.dhl import DhlCarrier
from livecart_core.infrastructure.carriers.dpd import DpdCarrier
from livecart_core.infrastructure.carriers.mock import MockCarrier
from livecart_core.infrastructure.carriers.stub import CarrierProviderStub
from livecart_core.settings import Settings

STUBS: Final[dict[CarrierName, type[CarrierProviderStub]]] = {
    CarrierName.DPD: DpdCarrier,
    CarrierName.CHRONOPOST: ChronopostCarrier,
    CarrierName.DHL: DhlCarrier,
}


def build_carrier(settings: Settings, clock: Clock) -> CarrierProvider:
    """The carrier of ``CARRIER_PROVIDER``; see the module docstring."""
    carrier = settings.carrier_provider
    if carrier is CarrierName.MOCK:
        if settings.mock_carrier_secret is None:
            raise ValueError("MOCK_CARRIER_SECRET must be set while CARRIER_PROVIDER=mock")
        return MockCarrier(settings.mock_carrier_secret, clock, step=settings.mock_carrier_step)
    return STUBS[carrier]()
