"""``LookUpOrder``: a buyer's two tiers of access to an order.

A reference such as ``LC-7K3F9Q`` is read aloud on a live stream and has
about 2**30 values: it identifies an order, it cannot authorize anything
(OWASP API1). So:

- the reference alone gives the order's public projection: its status, its
  product, quantity and total, and until when it is reserved;
- whatever needs more (for now the detailed view, with the shipping
  address) needs the order's access token as well, the one the buyer
  received with the reference. It is checked against the stored hash in
  constant time (``livecart_core.order_tokens``); a missing or wrong token,
  or the token of another order, gets exactly the answer of an unknown
  reference.

References are read tolerantly (``parse_order_reference``: any case, the
hyphen optional, ``O`` for ``0`` and ``I`` or ``L`` for ``1``); a text that
cannot be one is an unknown reference, answered without a query.
"""

from collections.abc import Callable

from livecart_core.application.dto import BuyerOrderRecord
from livecart_core.application.ports import OrderLookup
from livecart_core.domain.errors import InvalidOrderReference
from livecart_core.domain.reference import parse_order_reference

# (token as sent, stored hash) -> whether they match: OrderTokens.verify in production.
type TokenCheck = Callable[[str, bytes], bool]


class LookUpOrder:
    """An order by its reference, with or without its access token; see the module docstring."""

    def __init__(self, orders: OrderLookup, check_token: TokenCheck) -> None:
        self._orders = orders
        self._check_token = check_token

    async def by_reference(self, reference: str) -> BuyerOrderRecord | None:
        """The order the reference names, for its public projection only."""
        try:
            parsed = parse_order_reference(reference)
        except InvalidOrderReference:
            return None
        return await self._orders.find_by_reference(parsed)

    async def with_token(self, reference: str, token: str | None) -> BuyerOrderRecord | None:
        """The order, only when ``token`` is its access token; ``None`` in every other case."""
        if token is None:
            return None
        order = await self.by_reference(reference)
        if order is None or not self._check_token(token, order.access_token_hash):
            return None
        return order
