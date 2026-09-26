"""The text mode, ``livecart-simulate --text``: what the buyers received, as they would read it.

The live platforms would send each buyer a direct message with the order's
reference, its access token and the buyer app's link; their integrations
are out of scope, so the worker's ``SimulatedBuyerNotifier`` appends each
message to the development stream ``{prefix}:dev:buyer-dm`` instead. This
mode prints that stream, oldest first, then follows it (``XREAD BLOCK``)
until Ctrl+C; ``--no-follow`` stops at its end. For each message: the
reference and its buyer, the token for the app's "Add order" screen, and the
link that opens the order in the app.

It prints tokens, on purpose: they are what a buyer receives. So it writes
to its standard output only, never to a log line, and the stream itself
exists in development only: the worker refuses to write it in production.
"""

import asyncio
from collections.abc import Mapping
from typing import Any, Final, TextIO

from redis.asyncio import Redis

from livecart_core.infrastructure.notifier import buyer_dm_stream_key

# Entries read per XREAD while catching up with the stream.
PAGE_SIZE: Final = 100
FOLLOW_BLOCK_MS: Final = 2_000
START_ID: Final = "0-0"


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def render_message(fields: Mapping[str, str]) -> str:
    """Three lines: the reference and its buyer, the token, the link."""
    return (
        f"{fields.get('reference', '?')} for {fields.get('recipient', '?')} "
        f"on {fields.get('platform', '?')}\n"
        f"  token: {fields.get('token', '?')}\n"
        f"  link:  {fields.get('link', '?')}\n"
    )


class BuyerMessages:
    """Reads the development stream of the buyers' messages; see the module docstring."""

    def __init__(self, client: Redis, *, prefix: str) -> None:
        self._client = client
        self.stream_key = buyer_dm_stream_key(prefix)

    async def print_all(self, out: TextIO, *, stop: asyncio.Event, follow: bool = True) -> int:
        """Print every message, then follow the stream when ``follow``; return how many."""
        printed = 0
        last_id = START_ID
        while not stop.is_set():
            block = FOLLOW_BLOCK_MS if follow else None
            entries = await self._read(last_id, block)
            if not entries:
                if not follow:
                    break
                continue
            for entry_id, fields in entries:
                out.write(render_message(fields))
                last_id = entry_id
                printed += 1
            out.flush()
        return printed

    async def _read(self, after: str, block: int | None) -> list[tuple[str, dict[str, str]]]:
        response: Any = await self._client.xread(
            {self.stream_key: after}, count=PAGE_SIZE, block=block
        )
        if not response:
            return []
        # RESP2: [[stream key, [(entry id, {field: value}), ...]]].
        return [
            (_text(entry_id), {_text(key): _text(value) for key, value in fields.items()})
            for entry_id, fields in response[0][1]
        ]
