"""/ready with the real resources, against the compose PostgreSQL and Redis."""

import httpx2
import pytest

from livecart_api.app import create_app
from livecart_api.resources import build_resources
from livecart_core.settings import Settings
from livecart_core.testing.database import IntegrationSettings
from livecart_core.testing.keys import (
    TEST_COMMENT_WEBHOOK_SECRET,
    TEST_JWT_SECRET,
    TEST_ORDER_TOKEN_KEY,
)

pytestmark = pytest.mark.integration


async def test_ready_is_200_when_postgres_and_redis_answer(
    integration_settings: IntegrationSettings,
) -> None:
    settings = Settings(
        database_url=integration_settings.database_url,
        redis_url=integration_settings.redis_url,
        jwt_secret=TEST_JWT_SECRET,
        order_token_key=TEST_ORDER_TOKEN_KEY,
        comment_webhook_secret_simulator=TEST_COMMENT_WEBHOOK_SECRET,
        _env_file=None,
    )
    resources = build_resources(settings)
    app = create_app(settings, resources)

    try:
        transport = httpx2.ASGITransport(app=app)
        # The URL of the container healthcheck, whose host the default ALLOWED_HOSTS keep.
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8000"
        ) as client:
            response = await client.get("/ready")
    finally:
        await resources.aclose()

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"postgres": "ok", "redis": "ok"}}
