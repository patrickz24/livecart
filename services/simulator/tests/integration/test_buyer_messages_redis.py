"""The text mode on the compose Redis: what the worker's notifier wrote, printed for the buyer.

The messages are written by the worker's own ``SimulatedBuyerNotifier``, so
the reader and the writer agree on the stream's key and fields.
"""

import asyncio
import io
from uuid import UUID

import pytest
from redis.asyncio import Redis

from livecart_core.application.dto import BuyerNotification, Platform
from livecart_core.domain.reference import OrderReference
from livecart_core.infrastructure.notifier import SimulatedBuyerNotifier
from livecart_core.testing.polling import until
from livecart_simulator.buyer_messages import BuyerMessages

pytestmark = pytest.mark.integration


def _notice(number: int) -> BuyerNotification:
    return BuyerNotification(
        order_id=UUID(int=number),
        platform=Platform.SIMULATOR,
        recipient=f"sim-buyer-0{number}",
        reference=OrderReference(f"LC-7K3F9{number}"),
        access_token=f"token-{number}",
    )


async def _notify(client: Redis, prefix: str, *numbers: int) -> None:
    notifier = SimulatedBuyerNotifier(
        client, prefix=prefix, link_scheme="livecart", production=False
    )
    for number in numbers:
        await notifier.notify(_notice(number))


async def test_every_message_is_printed_with_its_token_and_link(
    redis_client: Redis, redis_key_prefix: str
) -> None:
    await _notify(redis_client, redis_key_prefix, 1, 2, 3)
    out = io.StringIO()

    printed = await BuyerMessages(redis_client, prefix=redis_key_prefix).print_all(
        out, stop=asyncio.Event(), follow=False
    )

    assert printed == 3
    lines = out.getvalue().splitlines()
    assert lines[:3] == [
        "LC-7K3F91 for sim-buyer-01 on simulator",
        "  token: token-1",
        "  link:  livecart://link/LC-7K3F91#t=token-1",
    ]
    assert lines[6] == "LC-7K3F93 for sim-buyer-03 on simulator"


async def test_an_empty_stream_prints_nothing(redis_client: Redis, redis_key_prefix: str) -> None:
    out = io.StringIO()

    printed = await BuyerMessages(redis_client, prefix=redis_key_prefix).print_all(
        out, stop=asyncio.Event(), follow=False
    )

    assert printed == 0
    assert out.getvalue() == ""


async def test_the_stream_is_followed_until_stopped(
    redis_client: Redis, redis_key_prefix: str
) -> None:
    await _notify(redis_client, redis_key_prefix, 1)
    out = io.StringIO()
    stop = asyncio.Event()
    reader = BuyerMessages(redis_client, prefix=redis_key_prefix)

    following = asyncio.create_task(reader.print_all(out, stop=stop, follow=True))
    await until(lambda: "LC-7K3F91" in out.getvalue())
    await _notify(redis_client, redis_key_prefix, 2)
    await until(lambda: "LC-7K3F92" in out.getvalue())
    stop.set()

    assert await asyncio.wait_for(following, timeout=5) == 2
