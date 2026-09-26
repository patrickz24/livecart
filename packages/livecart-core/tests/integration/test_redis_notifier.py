"""``SimulatedBuyerNotifier`` on a real Redis: the development stream of the buyers' messages."""

from typing import Any
from uuid import UUID

import pytest
from redis.asyncio import Redis

from livecart_core.application.dto import BuyerNotification, Platform
from livecart_core.domain.reference import OrderReference
from livecart_core.infrastructure.notifier import (
    BUYER_DM_MAX_LENGTH,
    BUYER_DM_TTL_S,
    SimulatedBuyerNotifier,
)
from livecart_core.testing.fakes import TEST_ORDER_TOKENS

pytestmark = pytest.mark.integration


def _notice(n: int) -> BuyerNotification:
    order_id = UUID(int=n + 1)
    return BuyerNotification(
        order_id=order_id,
        platform=Platform.SIMULATOR,
        recipient=f"buyer-{n}",
        reference=OrderReference("LC-7K3F9Q"),
        access_token=TEST_ORDER_TOKENS.token_for(order_id),
    )


@pytest.fixture
def notifier(redis_client: Redis, redis_key_prefix: str) -> SimulatedBuyerNotifier:
    return SimulatedBuyerNotifier(
        redis_client, prefix=redis_key_prefix, link_scheme="livecart", production=False
    )


async def test_a_notice_is_one_entry_of_the_development_stream_for_an_hour(
    notifier: SimulatedBuyerNotifier, redis_client: Redis, redis_key_prefix: str
) -> None:
    notice = _notice(0)

    await notifier.notify(notice)

    assert notifier.stream_key == f"{redis_key_prefix}:dev:buyer-dm"
    entries: Any = await redis_client.xrange(notifier.stream_key)
    [(_, fields)] = entries
    token = notice.access_token
    assert {key.decode(): value.decode() for key, value in fields.items()} == {
        "order_id": str(notice.order_id),
        "platform": "simulator",
        "recipient": "buyer-0",
        "reference": "LC-7K3F9Q",
        "token": token,
        "link": f"livecart://link/LC-7K3F9Q#t={token}",
    }
    assert BUYER_DM_TTL_S - 5 <= await redis_client.ttl(notifier.stream_key) <= BUYER_DM_TTL_S


async def test_the_stream_keeps_about_the_last_thousand_notices(
    notifier: SimulatedBuyerNotifier, redis_client: Redis
) -> None:
    for n in range(BUYER_DM_MAX_LENGTH + 150):
        await notifier.notify(_notice(n))

    # Trimmed by whole nodes of 100 entries: never below the cap, never far above.
    length = await redis_client.xlen(notifier.stream_key)
    assert BUYER_DM_MAX_LENGTH <= length < BUYER_DM_MAX_LENGTH + 100
    last: Any = await redis_client.xrevrange(notifier.stream_key, count=1)
    assert last[0][1][b"recipient"] == f"buyer-{BUYER_DM_MAX_LENGTH + 149}".encode()
