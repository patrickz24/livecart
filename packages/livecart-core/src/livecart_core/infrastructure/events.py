"""``EventPublisher`` adapters: where the session events go after their commit.

Milestone 4b appends them to the Redis stream of each session, which the SSE
endpoint relays. Until then the worker publishes through ``NullEventPublisher``:
the use cases already build and hand over every event, so that the real
publisher only replaces this one in the composition root.
"""

from livecart_core.application.dto import SessionEvent


class NullEventPublisher:
    """Publishes nothing, and never fails: no session has a stream yet."""

    async def publish(self, event: SessionEvent) -> None:
        return None
