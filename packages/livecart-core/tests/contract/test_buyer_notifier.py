"""``BuyerNotifier`` on every backend: the in-memory fake, then the development stream on Redis.

A notice carries its order's reference and access token to the author of the
comment, at least once: a failure raises ``ServiceUnavailable`` and delivers
nothing, and the comment's redelivery notifies again with the same token,
recomputed from the order id. The Redis stream's own fields beyond the notice
(the buyer's link, the trim, the expiry) are that adapter's, and its
integration test checks them.
"""

from uuid import UUID

import pytest

from livecart_core.application.dto import BuyerNotification, Platform
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.domain.reference import OrderReference
from livecart_core.testing.backends import NotifierBackend
from livecart_core.testing.fakes import TEST_ORDER_TOKENS

REFERENCES = ("LC-7K3F9Q", "LC-2M8XWD", "LC-Q4ZT6H")


def _notice(n: int) -> BuyerNotification:
    """The notice of the n-th order: its buyer, its reference and the token derived from its id."""
    order_id = UUID(int=n + 1)
    return BuyerNotification(
        order_id=order_id,
        platform=Platform.SIMULATOR,
        recipient=f"buyer-{n}",
        reference=OrderReference(REFERENCES[n]),
        access_token=TEST_ORDER_TOKENS.token_for(order_id),
    )


async def test_each_notice_is_delivered_once_in_order_to_its_buyer(
    buyer_notifier: NotifierBackend,
) -> None:
    notices = [_notice(n) for n in range(3)]

    for notice in notices:
        await buyer_notifier.notifier.notify(notice)

    delivered = await buyer_notifier.delivered()
    assert delivered == notices
    assert [notice.recipient for notice in delivered] == ["buyer-0", "buyer-1", "buyer-2"]
    assert [notice.access_token for notice in delivered] == [
        TEST_ORDER_TOKENS.token_for(UUID(int=n + 1)) for n in range(3)
    ]


async def test_a_notice_sent_again_is_delivered_again_with_the_same_token(
    buyer_notifier: NotifierBackend,
) -> None:
    notice = _notice(0)

    await buyer_notifier.notifier.notify(notice)
    await buyer_notifier.notifier.notify(notice)

    assert await buyer_notifier.delivered() == [notice, notice]


async def test_a_failed_notice_delivers_nothing_and_its_error_holds_no_token(
    buyer_notifier: NotifierBackend,
) -> None:
    notice = _notice(1)

    with pytest.raises(ServiceUnavailable) as excinfo:
        await buyer_notifier.failing().notify(notice)

    assert notice.access_token not in str(excinfo.value)
    assert await buyer_notifier.delivered() == []


async def test_the_redelivery_after_a_failure_delivers_the_notice_once(
    buyer_notifier: NotifierBackend,
) -> None:
    notice = _notice(2)
    with pytest.raises(ServiceUnavailable):
        await buyer_notifier.failing().notify(notice)

    await buyer_notifier.notifier.notify(notice)

    assert await buyer_notifier.delivered() == [notice]
