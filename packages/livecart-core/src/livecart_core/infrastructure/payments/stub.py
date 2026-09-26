"""The shape shared by the payment providers that are documented but not implemented.

PayPal and Viva Wallet are out of the MVP's scope. Each still has its class,
which names the provider and documents how its hosted checkout, its
expiry and its webhooks map onto the four calls of ``PaymentProvider``, so
that the real adapter knows what it must do. Building one raises
``NotImplementedError``: selected by ``PAYMENT_PROVIDER``, a stub stops the
process at startup rather than take orders it cannot charge.
"""

from collections.abc import Mapping
from typing import ClassVar

from livecart_core.application.dto import (
    CheckoutRequest,
    CheckoutSession,
    PaymentEvent,
    PaymentProviderName,
)


class PaymentProviderStub:
    """A payment provider that is designed but not implemented; see the module docstring."""

    # The provider's own webhook signature headers, which the real adapter will verify.
    signature_headers: ClassVar[tuple[str, ...]]
    _provider: ClassVar[PaymentProviderName]

    def __init__(self) -> None:
        raise NotImplementedError(
            f"the {self._provider} payment provider is a documented stub, not implemented: "
            "choose another PAYMENT_PROVIDER"
        )

    @property
    def name(self) -> PaymentProviderName:
        return self._provider

    async def create_checkout(self, request: CheckoutRequest) -> CheckoutSession:
        raise NotImplementedError(f"the {self._provider} provider is a stub")

    async def expire_checkout(self, provider_ref: str) -> bool:
        raise NotImplementedError(f"the {self._provider} provider is a stub")

    async def get_checkout(self, provider_ref: str) -> CheckoutSession:
        raise NotImplementedError(f"the {self._provider} provider is a stub")

    async def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> PaymentEvent:
        raise NotImplementedError(f"the {self._provider} provider is a stub")
