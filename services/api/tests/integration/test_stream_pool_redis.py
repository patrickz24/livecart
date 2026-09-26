"""The event streams on a Redis pool of their own, over the real resources of ``build_resources``.

More streams than the shared pool's 100 connections stay open in ``XREAD
BLOCK`` on the compose Redis, and meanwhile a signed webhook is queued, a
seller logs in and ``/ready`` answers 200: the streams never take the
connections the rest of the API needs. On the shared pool, the 101st stream
and every other use of Redis failed with ``MaxConnectionsError``.
"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Final

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_api.app import create_app
from livecart_api.resources import Resources, build_resources
from livecart_api.routes.session_events import StreamSlots
from livecart_api.sse_testing import open_event_stream
from livecart_api.testing import (
    SIMULATOR_WEBHOOK,
    TEST_HOST,
    TEST_ORIGIN,
    open_client,
    seller_headers,
    signed_webhook,
)
from livecart_core.application.create_seller import CreateSeller
from livecart_core.application.dto import CommentBatch, LiveSessionRecord, SellerRecord
from livecart_core.infrastructure.clock import SystemClock
from livecart_core.infrastructure.redis_client import SHARED_POOL_CONNECTIONS
from livecart_core.settings import Settings
from livecart_core.testing.backends import PostgresBackend
from livecart_core.testing.builders import incoming_comment
from livecart_core.testing.database import IntegrationSettings
from livecart_core.testing.keys import (
    TEST_COMMENT_WEBHOOK_SECRET,
    TEST_JWT_SECRET,
    TEST_ORDER_TOKEN_KEY,
)

pytestmark = pytest.mark.integration

# More streams than the shared pool holds connections.
STREAMS: Final = SHARED_POOL_CONNECTIONS + 10
EMAIL: Final = "streams@example.test"
PASSWORD: Final = "correct horse battery staple"


@dataclass
class RealApi:
    app: FastAPI
    client: httpx2.AsyncClient
    seller: SellerRecord
    session: LiveSessionRecord

    @property
    def events_path(self) -> str:
        return f"/api/v1/live-sessions/{self.session.id}/events"


@pytest.fixture
async def api(
    clean_database: None,
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    integration_settings: IntegrationSettings,
    redis_key_prefix: str,
) -> AsyncIterator[RealApi]:
    settings = Settings(
        database_url=integration_settings.database_url,
        redis_url=integration_settings.redis_url,
        redis_key_prefix=redis_key_prefix,
        jwt_secret=TEST_JWT_SECRET,
        order_token_key=TEST_ORDER_TOKEN_KEY,
        comment_webhook_secret_simulator=TEST_COMMENT_WEBHOOK_SECRET,
        allowed_hosts=(TEST_HOST,),
        sse_max_streams=STREAMS,
        metrics_port=None,
        _env_file=None,
    )
    resources: Resources = build_resources(settings)
    try:
        seller = await CreateSeller(resources.sellers, resources.passwords, resources.clock)(
            email=EMAIL, slug="stream-seller", display_name="Streams", password=PASSWORD
        )
        backend = PostgresBackend(app_engine, migrator_engine)
        session = await backend.add_live_session(seller_id=seller.id)
        app = create_app(settings, resources)
        async with open_client(app) as client:
            yield RealApi(app, client, seller, session)
    finally:
        await resources.aclose()


async def test_the_streams_at_their_cap_leave_the_shared_redis_to_the_rest(
    api: RealApi,
) -> None:
    headers = seller_headers(api.app, api.seller.id)
    async with AsyncExitStack() as streams:
        for _ in range(STREAMS):
            stream = await streams.enter_async_context(
                open_event_stream(api.app, api.events_path, headers)
            )
            assert stream.status == 200
            assert (await stream.next_message()).event == "snapshot"
        slots = api.app.state.stream_slots
        assert isinstance(slots, StreamSlots)
        assert slots.open == STREAMS

        webhook_headers, body = signed_webhook(
            CommentBatch(comments=[incoming_comment("je prends A12")]), SystemClock()
        )
        webhook = await api.client.post(SIMULATOR_WEBHOOK, content=body, headers=webhook_headers)
        login = await api.client.post(
            "/api/v1/auth/login",
            json={"email": EMAIL, "password": PASSWORD},
            headers={"Origin": TEST_ORIGIN},
        )
        ready = await api.client.get("/ready")

    assert (webhook.status_code, webhook.json()) == (202, {"accepted": 1})
    assert login.status_code == 200
    assert (ready.status_code, ready.json()["checks"]) == (200, {"postgres": "ok", "redis": "ok"})
