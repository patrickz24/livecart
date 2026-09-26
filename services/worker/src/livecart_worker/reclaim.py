"""The reclaim loop: every 30 s, what other consumers left pending, redelivered or dead-lettered.

One tick is ``ReclaimComments``: ``XAUTOCLAIM`` of the deliveries idle past
``QUEUE_RECLAIM_MIN_IDLE_MS`` (120 s, derived from the consumer's window), the
dead-letter queue for those past ``QUEUE_MAX_DELIVERIES`` (3), then the safe
``XTRIM MINID``. The deliveries it hands back go to this worker's own
consumer, in room reserved in its window before the reclaim asked for them,
so that the window keeps its bound and the idle time stays sound.

The first tick runs at startup, so that a worker restarted after a crash
takes its own leftovers back as soon as they are idle. The loop is
sequential and holds at most one connection at a time (the inbox's part of a
dead letter); a failure is logged and the next tick tries again.
"""

import asyncio
import contextlib
from typing import Final

import structlog

from livecart_core.application.ports import ServiceUnavailable
from livecart_core.application.reclaim_comments import ReclaimComments
from livecart_worker.consumer import CommentConsumer

logger = structlog.get_logger(__name__)

RECLAIM_INTERVAL_S: Final = 30.0


async def reclaim_once(reclaim: ReclaimComments, consumer: CommentConsumer) -> None:
    """One tick; see the module docstring."""
    room = consumer.reserve_room()
    try:
        reclaimed = await reclaim(consumer.name, room=room)
    except ServiceUnavailable as error:
        consumer.release_room(room)
        logger.warning("queue_reclaim_unavailable", error=str(error))
        return
    except Exception:
        consumer.release_room(room)
        logger.exception("queue_reclaim_failed")
        return
    except BaseException:
        consumer.release_room(room)
        raise
    consumer.release_room(room - len(reclaimed.redelivered))
    for delivery in reclaimed.redelivered:
        consumer.submit(delivery, reserved=True)
    for dead in reclaimed.dead_letters:
        envelope = dead.delivery.comment
        logger.error(
            "comment_dead_lettered",
            delivery_id=dead.delivery.delivery_id,
            delivery_count=dead.delivery.delivery_count,
            correlation_id=envelope.correlation_id,
            request_id=envelope.request_id,
            inbox=None if dead.inbox is None else dead.inbox.value,
        )
    if reclaimed.redelivered or reclaimed.dead_letters or reclaimed.trimmed:
        logger.info(
            "queue_reclaimed",
            redelivered=len(reclaimed.redelivered),
            dead_lettered=len(reclaimed.dead_letters),
            trimmed=reclaimed.trimmed,
        )


async def run_reclaim(
    reclaim: ReclaimComments,
    consumer: CommentConsumer,
    stop: asyncio.Event,
    *,
    interval_s: float = RECLAIM_INTERVAL_S,
) -> None:
    """Tick at startup, then every ``interval_s``, until ``stop`` is set."""
    logger.info("queue_reclaim_started", interval_s=interval_s)
    while not stop.is_set():
        await reclaim_once(reclaim, consumer)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(interval_s):
                await stop.wait()
    logger.info("queue_reclaim_stopped")
