"""``BuyerNotifier`` in development: the buyer's message, on a Redis stream instead of a platform.

The real platforms would deliver an order's reference and token by direct
message; their integrations are out of the MVP's scope. The simulated
notifier appends the message to the development stream
``{prefix}:dev:buyer-dm``, for the comment simulator's text mode
(``livecart-simulate --text``, the last step of milestone 4a) to show the
reference, the token and the buyer's link, for the buyer app's "Add order"
screen or its deep link:

- one entry per notice: the order id, the platform, the recipient (the
  author of the comment), the reference, the token and the link
  ``<scheme>://link/<reference>#t=<token>``, whose fragment the app reads and
  never sends to a server;
- ``XADD ... MAXLEN ~ 1000`` then ``EXPIRE 3600`` in one ``MULTI``: the
  stream keeps about the last thousand notices, and goes an hour after the
  last one, so that tokens do not pile up in Redis;
- the token goes to this stream and nowhere else: never a log line, never an
  error message.

The stream holds working tokens in clear, readable by anyone with access to
Redis: the notifier refuses to be built when ``APP_ENV=production``.
"""

import re
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError
from redis.typing import EncodableT, FieldT

from livecart_core.application.dto import BuyerNotification
from livecart_core.application.ports import ServiceUnavailable

# About the last thousand notices; `~` lets Redis trim whole nodes only.
BUYER_DM_MAX_LENGTH: Final = 1_000
# The stream goes an hour after its last notice.
BUYER_DM_TTL_S: Final = 3_600
# A URI scheme (RFC 3986): a letter, then letters, digits, "+", "-" or ".".
LINK_SCHEME_PATTERN: Final = re.compile(r"[a-z][a-z0-9+.-]*")


def buyer_link(scheme: str, notification: BuyerNotification) -> str:
    """The link the buyer app opens: the reference in its path, the token in its fragment."""
    return f"{scheme}://link/{notification.reference.value}#t={notification.access_token}"


class SimulatedBuyerNotifier:
    """The development notifier; see the module docstring."""

    def __init__(self, client: Redis, *, prefix: str, link_scheme: str, production: bool) -> None:
        if production:
            raise ValueError(
                "the simulated buyer notifier writes order tokens to a development stream: "
                "it is refused when APP_ENV=production"
            )
        if not LINK_SCHEME_PATTERN.fullmatch(link_scheme):
            raise ValueError(f"{link_scheme!r} is not a URI scheme")
        self._client = client
        self._link_scheme = link_scheme
        self.stream_key = f"{prefix}:dev:buyer-dm"

    async def notify(self, notification: BuyerNotification) -> None:
        fields: dict[FieldT, EncodableT] = {
            "order_id": str(notification.order_id),
            "platform": notification.platform.value,
            "recipient": notification.recipient,
            "reference": notification.reference.value,
            "token": notification.access_token,
            "link": buyer_link(self._link_scheme, notification),
        }
        try:
            async with self._client.pipeline(transaction=True) as pipeline:
                pipeline.xadd(self.stream_key, fields, maxlen=BUYER_DM_MAX_LENGTH, approximate=True)
                pipeline.expire(self.stream_key, BUYER_DM_TTL_S)
                await pipeline.execute()
        except RedisError as error:
            # The type only: a Redis error may echo the command, and the token in it.
            raise ServiceUnavailable(
                f"buyer notifier unavailable: {type(error).__name__}"
            ) from None
