"""``CommentQueue`` on Redis Streams: one stream, one consumer group, a dead-letter stream.

Keys, under the deployment's prefix: the stream ``{prefix}:queue:comments``,
read by the consumer group ``workers``, and its dead-letter stream
``{prefix}:queue:comments:dlq``. Each entry holds one envelope
(``QueuedComment`` as JSON, with its ``correlation_id`` and ``request_id``) in
its ``envelope`` field; the entry id is the delivery id.

- ``enqueue``: one ``XADD`` per comment, all in one pipeline, so a batch costs
  one round trip. No ``MAXLEN``: a length cap would drop entries nobody has
  read, the very comments backpressure exists to keep. A pipeline is not a
  transaction, so a failure may leave part of a batch queued; it raises
  ``ServiceUnavailable``, the API answers 503, and the platform's retry is
  deduplicated by the inbox;
- ``receive``: ``XREADGROUP GROUP workers <consumer> COUNT n BLOCK ms`` on
  ``>``, new entries only, each delivered for the first time. An entry stays
  in the group's pending entries list (PEL) until it is acknowledged;
- ``ack``: ``XACK``, which does nothing for an id no longer pending;
- ``reclaim``: ``XAUTOCLAIM`` from ``0-0``, oldest first, of the entries
  pending for at least the minimum idle time; Redis counts one more
  delivery for each, which ``XPENDING`` then reads;
- ``dead_letter``: one Lua script that ``XACK``s the entry and, only if it was
  still pending, copies it to the dead-letter stream (capped, since the
  inbox's ``dead_lettered`` row is the durable record);
- ``backlog``: ``lag + pending`` of the group from ``XINFO GROUPS`` (Redis 7.0),
  what is left to settle, however long the acknowledged history; ``XLEN``,
  an upper bound, when Redis cannot tell the lag or the group does not exist;
- ``trim``: ``XTRIM MINID ~`` below the oldest pending entry and the group's
  last-delivered id, whichever is lower. Every entry below both was
  delivered and settled, and none can become unread or pending again (new
  deliveries take ids above the last-delivered one, reclaims move entries
  already pending), so reading the group's state and trimming need not be
  atomic.

The group is created on first need at ``0`` with ``MKSTREAM``, so comments
queued before any worker started are delivered too. An entry whose envelope
no longer validates cannot be processed by any worker: it goes to the
dead-letter stream at once. Any Redis error raises ``ServiceUnavailable``.
"""

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final

import structlog
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

from livecart_core.application.dto import QueuedComment, QueueDelivery
from livecart_core.application.ports import ServiceUnavailable

logger = structlog.get_logger(__name__)

GROUP: Final = "workers"
ENVELOPE_FIELD: Final = "envelope"
# Dead letters are rare, and each one's inbox row says what happened to it:
# the stream keeps about the last thousand for inspection, at most some 6 MiB
# (6.2 KB each at the envelope's bound), outside the queue's half of maxmemory.
DEAD_LETTER_MAX_LENGTH: Final = 1_000
# The oldest id of a stream: nothing sorts below it.
START_ID: Final = "0-0"

# KEYS: the stream, the dead-letter stream. ARGV: the group, the entry id, the
# envelope, the delivery count, the cap of the dead-letter stream.
DEAD_LETTER_SCRIPT: Final = """
if redis.call("XACK", KEYS[1], ARGV[1], ARGV[2]) == 0 then
  return 0
end
redis.call("XADD", KEYS[2], "MAXLEN", "~", ARGV[5], "*",
  "envelope", ARGV[3], "delivery_id", ARGV[2], "delivery_count", ARGV[4])
return 1
"""

type StreamId = tuple[int, int]
# An entry as redis-py reads it: its id, and its fields by name.
type Entry = tuple[bytes, Mapping[bytes, bytes]]


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


def parse_stream_id(value: bytes | str) -> StreamId:
    """``1695000000000-3`` as (1695000000000, 3), which sorts as Redis sorts ids."""
    milliseconds, _, sequence = _text(value).partition("-")
    return int(milliseconds), int(sequence or 0)


def _require_positive(name: str, value: float) -> None:
    if not value > 0:
        raise ValueError(f"{name} must be positive, not {value}")


def _require_not_negative(name: str, value: float) -> None:
    if value < 0:
        raise ValueError(f"{name} must not be negative, not {value}")


@asynccontextmanager
async def _redis_errors(operation: str) -> AsyncIterator[None]:
    try:
        yield
    except RedisError as error:
        raise ServiceUnavailable(
            f"comment queue unavailable ({operation}): {type(error).__name__}"
        ) from error


def _is_missing_group(error: ResponseError) -> bool:
    return str(error).startswith("NOGROUP")


class RedisCommentQueue:
    """The comment queue on Redis Streams; see the module docstring."""

    def __init__(
        self,
        client: Redis,
        *,
        prefix: str,
        approximate_trim: bool = True,
        dead_letter_max_length: int = DEAD_LETTER_MAX_LENGTH,
    ) -> None:
        self._client = client
        self.stream_key = f"{prefix}:queue:comments"
        self.dead_letter_key = f"{self.stream_key}:dlq"
        # Exact trimming only serves the tests, which check its boundary: `~`
        # lets Redis drop whole nodes only, which costs far less.
        self._approximate_trim = approximate_trim
        self._dead_letter_max_length = dead_letter_max_length
        self._dead_letter_script = client.register_script(DEAD_LETTER_SCRIPT)

    async def ensure_group(self) -> None:
        """Create the group ``workers`` at the start of the stream, unless it exists."""
        async with _redis_errors("create group"):
            try:
                await self._client.xgroup_create(self.stream_key, GROUP, id=START_ID, mkstream=True)
            except ResponseError as error:
                if not str(error).startswith("BUSYGROUP"):
                    raise

    async def backlog(self) -> int:
        async with _redis_errors("backlog"):
            pipeline = self._client.pipeline(transaction=False)
            pipeline.xlen(self.stream_key)
            pipeline.xinfo_groups(self.stream_key)
            length, groups = await pipeline.execute(raise_on_error=False)
        if isinstance(length, Exception):
            name = type(length).__name__
            raise ServiceUnavailable(f"comment queue unavailable (backlog): {name}")
        # An error here is a stream that does not exist (yet): XLEN said 0.
        group = None if isinstance(groups, Exception) else self._group(groups)
        if group is None or group.get("lag") is None:
            return int(length)
        return int(group["lag"]) + int(group["pending"])

    async def enqueue(self, comments: Sequence[QueuedComment]) -> None:
        if not comments:
            return
        async with _redis_errors("enqueue"):
            pipeline = self._client.pipeline(transaction=False)
            for comment in comments:
                pipeline.xadd(self.stream_key, {ENVELOPE_FIELD: comment.model_dump_json()})
            results = await pipeline.execute(raise_on_error=False)
        failed = [result for result in results if isinstance(result, Exception)]
        if failed:
            raise ServiceUnavailable(
                f"comment queue unavailable (enqueue): {len(failed)} of {len(results)} "
                f"comments not queued ({type(failed[0]).__name__})"
            )

    async def receive(
        self, consumer: str, *, max_messages: int, block_seconds: float
    ) -> list[QueueDelivery]:
        _require_positive("max_messages", max_messages)
        _require_not_negative("block_seconds", block_seconds)
        # BLOCK 0 waits forever: no wait is no BLOCK at all, and any wait at least 1 ms.
        block = max(1, round(block_seconds * 1000)) if block_seconds > 0 else None
        async with _redis_errors("receive"):
            try:
                response = await self._read(consumer, max_messages, block)
            except ResponseError as error:
                if not _is_missing_group(error):
                    raise
                await self.ensure_group()
                response = await self._read(consumer, max_messages, block)
        return await self._deliveries(response, counts=None)

    async def ack(self, delivery: QueueDelivery) -> None:
        async with _redis_errors("ack"):
            await self._client.xack(self.stream_key, GROUP, delivery.delivery_id)

    async def reclaim(
        self, consumer: str, *, min_idle_seconds: float, max_messages: int
    ) -> list[QueueDelivery]:
        _require_positive("max_messages", max_messages)
        _require_not_negative("min_idle_seconds", min_idle_seconds)
        async with _redis_errors("reclaim"):
            try:
                _cursor, entries, deleted = await self._client.xautoclaim(
                    self.stream_key,
                    GROUP,
                    consumer,
                    round(min_idle_seconds * 1000),
                    START_ID,
                    count=max_messages,
                )
            except ResponseError as error:
                if not _is_missing_group(error):
                    raise
                return []
            if deleted:
                # Pending entries whose data went (by XDEL, never by trim): XAUTOCLAIM
                # has dropped them from the PEL, and no inbox row waits on them.
                logger.warning("queue_pending_entries_deleted", count=len(deleted))
            counts = await self._delivery_counts([entry_id for entry_id, _ in entries])
        return await self._deliveries(entries, counts=counts)

    async def dead_letter(self, delivery: QueueDelivery) -> None:
        await self._move_to_dead_letters(
            delivery.delivery_id, delivery.comment.model_dump_json(), delivery.delivery_count
        )

    async def trim(self) -> int:
        async with _redis_errors("trim"):
            try:
                groups = await self._client.xinfo_groups(self.stream_key)
            except ResponseError:
                return 0  # no stream yet
            group = self._group(groups)
            if group is None:
                return 0
            threshold = parse_stream_id(group["last-delivered-id"])
            pending = await self._client.xpending(self.stream_key, GROUP)
            if pending["pending"]:
                threshold = min(threshold, parse_stream_id(pending["min"]))
            if threshold == (0, 0):
                return 0
            minid = f"{threshold[0]}-{threshold[1]}"
            removed = await self._client.xtrim(
                self.stream_key, minid=minid, approximate=self._approximate_trim
            )
        return int(removed)

    # --- Helpers -----------------------------------------------------------------------

    @staticmethod
    def _group(groups: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
        """The group ``workers`` in the answer of XINFO GROUPS, or ``None``."""
        return next((group for group in groups if _name(group.get("name")) == GROUP), None)

    async def _read(self, consumer: str, max_messages: int, block: int | None) -> list[Entry]:
        response: Any = await self._client.xreadgroup(
            GROUP, consumer, {self.stream_key: ">"}, count=max_messages, block=block
        )
        # RESP2, the client's protocol: [[stream key, [entry, ...]]], or nothing.
        return list(response[0][1]) if response else []

    async def _delivery_counts(self, entry_ids: Sequence[bytes]) -> dict[bytes, int]:
        """What XPENDING says of each id: how many times it was delivered, this one included."""
        if not entry_ids:
            return {}
        pipeline = self._client.pipeline(transaction=False)
        for entry_id in entry_ids:
            pipeline.xpending_range(self.stream_key, GROUP, entry_id, entry_id, 1)
        rows = await pipeline.execute()
        return {_bytes(row[0]["message_id"]): int(row[0]["times_delivered"]) for row in rows if row}

    async def _deliveries(
        self,
        entries: Sequence[Entry],
        *,
        counts: Mapping[bytes, int] | None,
    ) -> list[QueueDelivery]:
        deliveries = []
        for entry_id, fields in entries:
            delivery_count = 1 if counts is None else counts.get(entry_id, 1)
            raw = fields.get(ENVELOPE_FIELD.encode(), b"")
            try:
                comment = QueuedComment.model_validate_json(raw)
            except ValidationError:
                # No worker can ever process it: dead-letter it now rather than
                # have it reclaimed until its deliveries run out.
                logger.error("queue_entry_malformed", delivery_id=_text(entry_id))
                await self._move_to_dead_letters(_text(entry_id), raw, delivery_count)
                continue
            deliveries.append(QueueDelivery(_text(entry_id), comment, delivery_count))
        return deliveries

    async def _move_to_dead_letters(
        self, delivery_id: str, envelope: str | bytes, delivery_count: int
    ) -> None:
        async with _redis_errors("dead letter"):
            await self._dead_letter_script(
                keys=[self.stream_key, self.dead_letter_key],
                args=[GROUP, delivery_id, envelope, delivery_count, self._dead_letter_max_length],
            )


def _name(value: object) -> str | None:
    return _text(value) if isinstance(value, bytes | str) else None


def _bytes(value: object) -> bytes:
    return value if isinstance(value, bytes) else str(value).encode()
