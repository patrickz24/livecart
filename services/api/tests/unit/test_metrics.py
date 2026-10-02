"""The API's metrics: scraped from the app's own registry, and served on the internal port only."""

import urllib.request
from contextlib import AsyncExitStack

import httpx2
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from prometheus_client import REGISTRY, CollectorRegistry

from livecart_api.app import create_app
from livecart_api.rate_limits import RateLimiting
from livecart_api.testing import (
    SIMULATOR_WEBHOOK,
    current_time_clock,
    make_app,
    make_settings,
    memory_resources,
    open_client,
    signed_webhook,
)
from livecart_core.application.dto import CommentBatch
from livecart_core.metrics import MetricsServer
from livecart_core.testing.builders import incoming_comment
from livecart_core.testing.fakes import FakeCommentQueue, InMemoryDatabase

BATCH = CommentBatch(
    comments=[
        incoming_comment("je prends A12 M", external_comment_id="c-1"),
        incoming_comment("SOLD B07 x2", external_comment_id="c-2"),
    ]
)
WEBHOOK_ROUTE = {"handler": "/api/v1/webhooks/comments/{platform}", "method": "POST"}


def _registry(app: FastAPI) -> CollectorRegistry:
    registry = app.state.metrics_registry
    assert isinstance(registry, CollectorRegistry)
    return registry


def _sample(app: FastAPI, name: str, **labels: str) -> float | None:
    return _registry(app).get_sample_value(name, labels)


def _route_paths(app: FastAPI) -> set[str]:
    """The paths of ``app``'s routes, those of its included routers among them.

    ``app.routes`` lists an included router, not its routes. A router's mount or
    Starlette route has no path here: FastAPI keeps one for its API routes only.
    """
    return {route.path for route in iter_route_contexts(app.routes) if route.path}


async def test_queued_comments_and_the_backlog_are_counted() -> None:
    clock = current_time_clock()
    queue = FakeCommentQueue(clock)
    app = make_app(clock=clock, comment_queue=queue)
    headers, body = signed_webhook(BATCH, clock)

    async with open_client(app) as client:
        response = await client.post(SIMULATOR_WEBHOOK, headers=headers, content=body)

    assert response.status_code == 202
    assert _sample(app, "livecart_comments_received_total") == 2
    assert _sample(app, "livecart_queue_backlog") == 2
    assert _sample(app, "livecart_queue_backpressure_total") == 0


async def test_the_latency_of_each_route_is_recorded_by_template() -> None:
    clock = current_time_clock()
    app = make_app(clock=clock, comment_queue=FakeCommentQueue(clock))
    headers, body = signed_webhook(BATCH, clock)

    async with open_client(app) as client:
        await client.post(SIMULATOR_WEBHOOK, headers=headers, content=body)
        await client.post(SIMULATOR_WEBHOOK, content=body)
        await client.get("/health")
        await client.get("/no/such/route")

    count = "http_request_duration_seconds_count"
    assert _sample(app, count, **WEBHOOK_ROUTE, status="2xx") == 1
    assert _sample(app, count, **WEBHOOK_ROUTE, status="4xx") == 1
    assert _sample(app, count, handler="/health", method="GET", status="2xx") == 1
    # Unknown paths share one label value: a scan cannot grow the series without bound.
    assert _sample(app, count, handler="none", method="GET", status="4xx") == 1


async def test_rate_limiter_failures_and_buyer_not_found_lookups_are_exposed() -> None:
    app = make_app()
    limits = app.state.rate_limiting
    assert isinstance(limits, RateLimiting)

    await limits.record_lookup_not_found("192.0.2.1")
    limits.limiter_errors_total += 3

    assert _sample(app, "livecart_buyer_lookup_not_found_total") == 1
    assert _sample(app, "livecart_rate_limiter_errors_total") == 3


def test_each_app_has_a_registry_of_its_own() -> None:
    first, second = make_app(), make_app()

    assert _registry(first) is not _registry(second)
    assert REGISTRY.get_sample_value("livecart_comments_received_total") is None


async def test_the_api_port_serves_no_metrics_route() -> None:
    app = make_app()

    async with open_client(app) as client:
        response = await client.get("/metrics")

    # Any route there answers otherwise: 405 for another method, a redirect for a mount.
    assert response.status_code == 404
    paths = _route_paths(app)
    assert "/health" in paths
    assert "/metrics" not in paths


def test_the_metrics_server_serves_a_registry_until_closed() -> None:
    registry = _registry(make_app())
    server = MetricsServer.start(registry, port=0, host="127.0.0.1")
    try:
        url = f"http://127.0.0.1:{server.port}/metrics"
        with urllib.request.urlopen(url, timeout=5) as response:
            text = response.read().decode()
    finally:
        server.close()

    assert "livecart_comments_received_total 0.0" in text
    assert "# TYPE http_request_duration_seconds histogram" in text


async def test_the_app_serves_its_metrics_while_it_runs() -> None:
    clock = current_time_clock()
    app = create_app(
        make_settings(), memory_resources(InMemoryDatabase(), clock=clock), metrics_port=0
    )

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))
        server = app.state.metrics_server
        assert isinstance(server, MetricsServer)
        async with httpx2.AsyncClient() as client:
            response = await client.get(f"http://127.0.0.1:{server.port}/metrics")

    assert response.status_code == 200
    assert "livecart_queue_backlog" in response.text


async def test_without_a_port_the_app_serves_no_metrics() -> None:
    app = make_app()

    async with app.router.lifespan_context(app):
        assert getattr(app.state, "metrics_server", None) is None
