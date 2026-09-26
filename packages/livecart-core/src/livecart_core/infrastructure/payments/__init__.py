"""The payment providers behind ``PaymentProvider``, one of which ``PAYMENT_PROVIDER`` selects.

The mock is the default: it needs no account and no key. Stripe Checkout
runs in test mode, with ``STRIPE_SECRET_KEY`` and ``STRIPE_WEBHOOK_SECRET``.
PayPal and Viva are documented stubs. ``build_payment_provider`` builds the
selected one when a process starts, so that a misconfiguration stops it
before it takes an order: a stub raises ``NotImplementedError``, the mock
without ``MOCK_PAYMENT_WEBHOOK_SECRET`` or Stripe without its two values a
``ValueError``.

The mock charges nothing, so a production process refuses it:
``refuse_mock_payments_in_production`` runs in the composition roots, apart
from the build, so that tools that only build the app, such as the export of
the OpenAPI document, still can.
"""

from typing import Final

from redis.asyncio import Redis

from livecart_core.application.dto import PaymentProviderName
from livecart_core.application.ports import Clock, PaymentProvider
from livecart_core.infrastructure.payments.mock import MockPaymentProvider
from livecart_core.infrastructure.payments.paypal import PayPalPaymentProvider
from livecart_core.infrastructure.payments.stripe import StripePaymentProvider
from livecart_core.infrastructure.payments.stub import PaymentProviderStub
from livecart_core.infrastructure.payments.viva import VivaPaymentProvider
from livecart_core.settings import Settings

STUBS: Final[dict[PaymentProviderName, type[PaymentProviderStub]]] = {
    PaymentProviderName.PAYPAL: PayPalPaymentProvider,
    PaymentProviderName.VIVA: VivaPaymentProvider,
}


def build_payment_provider(settings: Settings, redis: Redis, clock: Clock) -> PaymentProvider:
    """The provider of ``PAYMENT_PROVIDER``; see the module docstring."""
    provider = settings.payment_provider
    if provider is PaymentProviderName.MOCK:
        if settings.mock_payment_webhook_secret is None:
            raise ValueError("MOCK_PAYMENT_WEBHOOK_SECRET must be set while PAYMENT_PROVIDER=mock")
        return MockPaymentProvider(
            redis,
            prefix=settings.redis_key_prefix,
            secret=settings.mock_payment_webhook_secret,
            clock=clock,
            public_base_url=settings.public_api_base_url,
        )
    if provider is PaymentProviderName.STRIPE:
        if settings.stripe_secret_key is None or settings.stripe_webhook_secret is None:
            raise ValueError(
                "STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET must be set while "
                "PAYMENT_PROVIDER=stripe"
            )
        return StripePaymentProvider(
            secret_key=settings.stripe_secret_key, webhook_secret=settings.stripe_webhook_secret
        )
    return STUBS[provider]()


def refuse_mock_payments_in_production(settings: Settings) -> None:
    """Raise ``ValueError`` when a production process would take payments with the mock."""
    if settings.is_production and settings.payment_provider is PaymentProviderName.MOCK:
        raise ValueError(
            "PAYMENT_PROVIDER=mock charges nothing: it is refused when APP_ENV=production"
        )
