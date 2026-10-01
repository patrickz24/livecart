"""The payment providers ``PAYMENT_PROVIDER`` selects: the mock, Stripe, the stubs, at startup."""

import importlib
from collections.abc import AsyncIterator

import httpx2
import pytest
from pydantic import SecretStr
from redis.asyncio import Redis

from livecart_core.application.dto import CheckoutRequest, PaymentProviderName
from livecart_core.application.ports import PaymentProvider
from livecart_core.infrastructure.payments import (
    STUBS,
    build_payment_provider,
    close_payment_provider,
    refuse_mock_payments_in_production,
)
from livecart_core.infrastructure.payments.mock import MockPaymentProvider
from livecart_core.infrastructure.payments.paypal import PayPalPaymentProvider
from livecart_core.infrastructure.payments.stripe import Httpx2Client, StripePaymentProvider
from livecart_core.infrastructure.payments.stub import PaymentProviderStub
from livecart_core.infrastructure.payments.viva import VivaPaymentProvider
from livecart_core.infrastructure.redis_client import create_redis_client
from livecart_core.settings import AppEnv, Settings
from livecart_core.testing.fakes import FakeClock
from livecart_core.testing.keys import (
    TEST_JWT_SECRET,
    TEST_MOCK_PAYMENT_WEBHOOK_SECRET,
    TEST_ORDER_TOKEN_KEY,
    TEST_STRIPE_SECRET_KEY,
    TEST_STRIPE_WEBHOOK_SECRET,
)


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    """A client of a host that does not exist: building a provider connects nothing."""
    client = create_redis_client(SecretStr("redis://redis.invalid:6379/0"))
    try:
        yield client
    finally:
        await client.aclose()


def _settings(**overrides: object) -> Settings:
    settings = Settings(
        database_url=SecretStr("postgresql+asyncpg://livecart_app:unit@db.invalid/livecart"),
        redis_url=SecretStr("redis://:unit@redis.invalid:6379/0"),
        jwt_secret=TEST_JWT_SECRET,
        order_token_key=TEST_ORDER_TOKEN_KEY,
        mock_payment_webhook_secret=TEST_MOCK_PAYMENT_WEBHOOK_SECRET,
        _env_file=None,
    )
    return settings.model_copy(update=overrides)


def test_the_mock_is_the_default_provider_and_names_itself_mock(redis: Redis) -> None:
    provider = build_payment_provider(_settings(), redis, FakeClock())

    assert isinstance(provider, MockPaymentProvider)
    assert provider.name is PaymentProviderName.MOCK
    assert TEST_MOCK_PAYMENT_WEBHOOK_SECRET.get_secret_value() not in repr(provider)


def test_the_mock_needs_its_webhook_key(redis: Redis) -> None:
    with pytest.raises(ValueError, match="MOCK_PAYMENT_WEBHOOK_SECRET must be set"):
        build_payment_provider(_settings(mock_payment_webhook_secret=None), redis, FakeClock())


def test_the_mock_hosts_its_pages_on_the_public_origin(redis: Redis) -> None:
    settings = _settings(public_api_base_url="http://192.168.1.20:8080")
    provider = build_payment_provider(settings, redis, FakeClock())

    assert isinstance(provider, MockPaymentProvider)
    assert provider.hosted_page_url("mock_cs_1") == (
        "http://192.168.1.20:8080/api/v1/mock-payments/mock_cs_1"
    )


@pytest.mark.parametrize("provider", [PaymentProviderName.PAYPAL, PaymentProviderName.VIVA])
def test_a_stub_stops_the_process_at_startup(provider: PaymentProviderName, redis: Redis) -> None:
    with pytest.raises(NotImplementedError, match=f"the {provider} payment provider is a"):
        build_payment_provider(_settings(payment_provider=provider), redis, FakeClock())


@pytest.mark.parametrize(
    "missing",
    [
        {"stripe_secret_key": None},
        {"stripe_webhook_secret": None},
    ],
    ids=["key", "webhook-secret"],
)
def test_stripe_needs_its_key_and_its_webhook_secret(
    redis: Redis, missing: dict[str, object]
) -> None:
    settings = _settings(
        payment_provider=PaymentProviderName.STRIPE,
        stripe_secret_key=TEST_STRIPE_SECRET_KEY,
        stripe_webhook_secret=TEST_STRIPE_WEBHOOK_SECRET,
    ).model_copy(update=missing)

    with pytest.raises(ValueError, match="STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET"):
        build_payment_provider(settings, redis, FakeClock())


async def test_stripe_is_built_from_its_key_and_its_webhook_secret(redis: Redis) -> None:
    settings = _settings(
        payment_provider=PaymentProviderName.STRIPE,
        stripe_secret_key=TEST_STRIPE_SECRET_KEY,
        stripe_webhook_secret=TEST_STRIPE_WEBHOOK_SECRET,
    )

    provider = build_payment_provider(settings, redis, FakeClock())

    assert isinstance(provider, StripePaymentProvider)
    assert provider.name is PaymentProviderName.STRIPE
    assert TEST_STRIPE_SECRET_KEY.get_secret_value() not in repr(provider)
    await provider.aclose()


def test_every_stub_names_its_provider_and_documents_its_flow() -> None:
    assert {
        PaymentProviderName.PAYPAL: PayPalPaymentProvider,
        PaymentProviderName.VIVA: VivaPaymentProvider,
    } == STUBS
    for provider, stub in STUBS.items():
        module = importlib.import_module(stub.__module__)
        assert module.__doc__ is not None
        assert "create_checkout" in module.__doc__
        assert "expire_checkout" in module.__doc__
        assert "verify_webhook" in module.__doc__
        # The class attribute, readable without building the stub.
        assert stub._provider is provider
    assert "PAYPAL-TRANSMISSION-SIG" in PayPalPaymentProvider.signature_headers


async def test_a_stub_answers_nothing_even_when_built_around_its_constructor() -> None:
    stub = object.__new__(PayPalPaymentProvider)
    provider: PaymentProvider = stub
    request = object.__new__(CheckoutRequest)

    assert provider.name is PaymentProviderName.PAYPAL
    with pytest.raises(NotImplementedError):
        await provider.create_checkout(request)
    with pytest.raises(NotImplementedError):
        await provider.expire_checkout("ref")
    with pytest.raises(NotImplementedError):
        await provider.get_checkout("ref")
    with pytest.raises(NotImplementedError):
        await provider.verify_webhook({}, b"")
    assert isinstance(stub, PaymentProviderStub)


def test_production_refuses_the_mock_and_takes_a_real_provider() -> None:
    production = _settings(app_env=AppEnv.PRODUCTION)

    with pytest.raises(ValueError, match="PAYMENT_PROVIDER=mock charges nothing"):
        refuse_mock_payments_in_production(production)
    refuse_mock_payments_in_production(_settings())
    refuse_mock_payments_in_production(
        _settings(app_env=AppEnv.PRODUCTION, payment_provider=PaymentProviderName.STRIPE)
    )


class _ClosingClient(Httpx2Client):
    """Stripe's HTTP client over a transport that answers nothing, noting its closing."""

    def __init__(self) -> None:
        super().__init__(transport=httpx2.MockTransport(lambda request: httpx2.Response(500)))
        self.closed = 0

    async def close_async(self) -> None:
        self.closed += 1
        await super().close_async()


async def test_closing_the_provider_releases_stripes_connections_only(redis: Redis) -> None:
    client = _ClosingClient()
    provider = StripePaymentProvider(
        secret_key=TEST_STRIPE_SECRET_KEY,
        webhook_secret=TEST_STRIPE_WEBHOOK_SECRET,
        http_client=client,
    )

    await close_payment_provider(provider)
    # The mock shares the process's Redis client, which the process closes itself.
    await close_payment_provider(build_payment_provider(_settings(), redis, FakeClock()))

    assert client.closed == 1
