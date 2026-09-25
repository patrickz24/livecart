"""/ready with the real resources, against the compose PostgreSQL and Redis."""

import httpx2
import pytest

from livecart_api.app import create_app
from livecart_api.resources import build_resources
from livecart_core.settings import Settings
from livecart_core.testing.database import IntegrationSettings

pytestmark = pytest.mark.integration


async def test_ready_is_200_when_postgres_and_redis_answer(
    integration_settings: IntegrationSettings,
) -> None:
    settings = Settings(
        database_url=integration_settings.database_url,
        redis_url=integration_settings.redis_url,
        _env_file=None,
    )
    resources = build_resources(settings)
    app = create_app(settings, resources)

    try:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/ready")
    finally:
        await resources.aclose()

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"postgres": "ok", "redis": "ok"}}
