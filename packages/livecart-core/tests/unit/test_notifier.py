from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis

from livecart_core.application.dto import BuyerNotification, Platform
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.domain.reference import OrderReference
from livecart_core.infrastructure.notifier import SimulatedBuyerNotifier, buyer_link
from livecart_core.infrastructure.redis_client import create_redis_client
from livecart_core.testing.fakes import TEST_ORDER_TOKENS

ORDER_ID = UUID(int=7)
TOKEN = TEST_ORDER_TOKENS.token_for(ORDER_ID)
NOTICE = BuyerNotification(
    order_id=ORDER_ID,
    platform=Platform.SIMULATOR,
    recipient="buyer-1",
    reference=OrderReference("LC-7K3F9Q"),
    access_token=TOKEN,
)


@pytest.fixture
async def unreachable() -> AsyncIterator[Redis]:
    """A client of a closed port: no command ever reaches a server."""
    client = create_redis_client(SecretStr("redis://127.0.0.1:1/15"))
    try:
        yield client
    finally:
        await client.aclose()


def test_the_buyer_link_carries_the_reference_in_its_path_and_the_token_in_its_fragment() -> None:
    assert buyer_link("livecart", NOTICE) == f"livecart://link/LC-7K3F9Q#t={TOKEN}"


def test_the_simulated_notifier_is_refused_in_production(unreachable: Redis) -> None:
    with pytest.raises(ValueError, match="refused when APP_ENV=production"):
        SimulatedBuyerNotifier(unreachable, prefix="p", link_scheme="livecart", production=True)


@pytest.mark.parametrize("scheme", ["", "Livecart", "1app", "live cart", "live/cart"])
def test_the_link_scheme_is_a_uri_scheme(unreachable: Redis, scheme: str) -> None:
    with pytest.raises(ValueError, match="not a URI scheme"):
        SimulatedBuyerNotifier(unreachable, prefix="p", link_scheme=scheme, production=False)


async def test_an_unreachable_redis_is_unavailable_and_the_error_holds_no_token(
    unreachable: Redis,
) -> None:
    notifier = SimulatedBuyerNotifier(
        unreachable, prefix="p", link_scheme="livecart", production=False
    )

    with pytest.raises(ServiceUnavailable) as excinfo:
        await notifier.notify(NOTICE)

    assert TOKEN not in str(excinfo.value)
    # Not chained either: a Redis error may echo the command that failed.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__
    assert TOKEN not in repr(NOTICE)
