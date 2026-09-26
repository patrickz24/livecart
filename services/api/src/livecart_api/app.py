"""Application factory: builds the FastAPI app around injected resources.

Around the routes, every request crosses, from the outside in:

1. ``CorrelationIdMiddleware`` and ``RequestContextMiddleware``: the request
   id, in the response and in every log line;
2. ``SecurityHeadersMiddleware``: the security headers of every response,
   the refusals below included;
3. ``TrustedHostMiddleware``: 400 unless the ``Host`` header is one of
   ``ALLOWED_HOSTS``;
4. ``RequestBodyLimitMiddleware``: 413 beyond 1 MiB, whether the client
   announced the length or streams the body, as nginx caps it in front.

Outside all of them, the latency middleware of the metrics
(``livecart_api.metrics``), whose registry the app serves on
``METRICS_PORT`` while it runs, never on its own port.

No CORS middleware: the dashboard and the mobile app reach the API through
nginx on their own origin, so no response ever allows another origin to read
it. The client address the rate limits count is uvicorn's, which trusts
``X-Forwarded-For`` from nginx alone (``--forwarded-allow-ips`` in compose).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import Any

import structlog
from asgi_correlation_id import CorrelationIdMiddleware
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from livecart_api import errors
from livecart_api.access_tokens import AccessTokens
from livecart_api.metrics import build_registry
from livecart_api.middleware.request_context import RequestContextMiddleware
from livecart_api.middleware.security_headers import SecurityHeadersMiddleware
from livecart_api.rate_limits import (
    RateLimiting,
    RateLimitRules,
    TooManyRequests,
    too_many_requests,
)
from livecart_api.resources import Resources
from livecart_api.routes.auth import SellerAuth, build_auth_router
from livecart_api.routes.health import build_health_router
from livecart_api.routes.live_sessions import build_live_session_router
from livecart_api.routes.orders import build_order_router
from livecart_api.routes.products import build_product_router
from livecart_api.routes.session_events import (
    EVENTS_ROUTE,
    STREAM_MODELS,
    StreamSlots,
    add_components,
    build_session_event_router,
)
from livecart_api.routes.storefronts import build_storefront_router
from livecart_api.routes.webhooks import build_webhook_router
from livecart_core.application.buyer_orders import LookUpOrder
from livecart_core.application.idempotency import IdempotentRequests
from livecart_core.application.ingest_comments import IngestComments
from livecart_core.application.live_sessions import SellerLiveSessions
from livecart_core.application.ports import SNAPSHOT_LIMIT, ServiceUnavailable
from livecart_core.application.products import SellerProducts
from livecart_core.application.seller_auth import (
    AuthenticateSeller,
    RevokeRefreshFamily,
    RotateRefreshToken,
)
from livecart_core.application.storefront import ViewStorefront
from livecart_core.application.watch_live_session import WatchLiveSession
from livecart_core.metrics import MetricsServer
from livecart_core.order_tokens import OrderTokens
from livecart_core.settings import Settings

logger = structlog.get_logger(__name__)

# Per-dependency budget of /ready.
READINESS_TIMEOUT_S = 2.0
# What a client waits before trying again after a 503.
RETRY_AFTER_S = 1
# The largest request body, as nginx's client_max_body_size: comment batches
# of 200 are far below it.
MAX_REQUEST_BODY_BYTES = 1024 * 1024


class LiveCartApi(FastAPI):
    """The app, whose OpenAPI document also describes the messages of its event streams.

    FastAPI documents a stream of ``ServerSentEvent`` as generic SSE items,
    since each item names its own event and data: the models of those data,
    ``LiveSessionSnapshot`` and the ``LiveSessionEvent`` union, reach no
    component on their own. They are added to ``components.schemas`` from
    their pydantic JSON schemas (``STREAM_MODELS``), in serialization mode as
    every response, and the stream route's item points at them; a model of
    the same name that FastAPI generated must be identical, or the export fails.
    """

    def openapi(self) -> dict[str, Any]:
        if self.openapi_schema is None:
            schema = super().openapi()
            add_components(schema, STREAM_MODELS)
        return super().openapi()


def _operation_id(route: APIRoute) -> str:
    # The handler name ("get_health") becomes the generated client's function name.
    return route.name


def build_seller_auth(settings: Settings, resources: Resources) -> SellerAuth:
    """The auth use cases over the injected adapters, and the access tokens under JWT_SECRET."""
    return SellerAuth(
        authenticate=AuthenticateSeller(
            resources.sellers, resources.refresh_tokens, resources.passwords, resources.clock
        ),
        rotate=RotateRefreshToken(resources.refresh_tokens, resources.clock),
        revoke=RevokeRefreshFamily(resources.refresh_tokens, resources.clock),
        sellers=resources.sellers,
        access_tokens=AccessTokens(settings.jwt_secret, resources.clock),
        clock=resources.clock,
        secure_cookies=settings.is_production,
    )


async def _service_unavailable(request: Request, error: Exception) -> JSONResponse:
    # A transient failure of a dependency (ServiceUnavailable from a port):
    # only its type reaches the logs, since messages can carry hosts or SQL.
    logger.warning("dependency_unavailable", error=type(error).__name__, path=request.url.path)
    return JSONResponse(
        {"detail": "Service temporarily unavailable"},
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        headers={"Retry-After": str(RETRY_AFTER_S)},
    )


def create_app(
    settings: Settings,
    resources: Resources,
    *,
    readiness_timeout_s: float = READINESS_TIMEOUT_S,
    metrics_port: int | None = None,
) -> FastAPI:
    """Create the app. It owns ``resources`` and closes them when it shuts down.

    ``metrics_port``, given by the composition root, serves the metrics while
    the app runs; the tests scrape ``app.state.metrics_registry`` instead.
    """

    @asynccontextmanager
    async def lifespan(running: FastAPI) -> AsyncIterator[None]:
        server = None
        if metrics_port is not None:
            server = MetricsServer.start(registry, port=metrics_port)
        running.state.metrics_server = server
        try:
            yield
        finally:
            if server is not None:
                server.close()
            await resources.aclose()

    # Interactive docs and the schema route are development conveniences only;
    # the committed OpenAPI file is generated from app.openapi() directly.
    docs_enabled = not settings.is_production
    app = LiveCartApi(
        title="LiveCart API",
        version=version("livecart-api"),
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
        generate_unique_id_function=_operation_id,
    )
    seller_auth = build_seller_auth(settings, resources)
    limits = RateLimiting(resources.rate_limiter, RateLimitRules.from_settings(settings))
    # Read by CurrentSeller (livecart_api.deps), which every seller route declares.
    app.state.access_tokens = seller_auth.access_tokens
    app.state.rate_limiting = limits
    app.include_router(
        build_health_router(resources.readiness_checks, timeout_s=readiness_timeout_s)
    )
    app.include_router(build_auth_router(seller_auth, limits))
    live_sessions = SellerLiveSessions(
        resources.live_sessions, resources.clock, resources.session_events
    )
    app.state.live_sessions = live_sessions
    app.include_router(build_live_session_router(live_sessions))
    slots = StreamSlots(settings.sse_max_streams)
    app.state.stream_slots = slots
    watch = WatchLiveSession(
        resources.live_sessions,
        resources.activity,
        resources.event_streams,
        resources.clock,
        snapshot_limit=SNAPSHOT_LIMIT,
    )
    app.include_router(
        build_session_event_router(watch, slots, resources.clock, snapshot_limit=SNAPSHOT_LIMIT)
    )
    app.include_router(
        build_product_router(
            SellerProducts(resources.products, resources.clock),
            IdempotentRequests(resources.idempotency),
        )
    )
    app.include_router(
        build_storefront_router(
            ViewStorefront(resources.sellers, resources.live_sessions, resources.products),
            limits,
        )
    )
    # Checking a token needs its stored hash only, never ORDER_TOKEN_KEY.
    app.include_router(
        build_order_router(LookUpOrder(resources.orders, OrderTokens.verify), limits)
    )
    ingest = IngestComments(
        resources.comment_queue,
        resources.clock,
        max_depth=settings.queue_max_depth,
        max_envelope_bytes=settings.queue_max_envelope_bytes,
    )
    app.state.ingest_comments = ingest
    app.include_router(build_webhook_router(resources.platforms, ingest, limits))
    app.add_exception_handler(ServiceUnavailable, _service_unavailable)
    app.add_exception_handler(TooManyRequests, too_many_requests)
    errors.install(app)

    # The last middleware added runs first: see the module docstring.
    app.add_middleware(RequestBodyLimitMiddleware, max_body_size=MAX_REQUEST_BODY_BYTES)
    # Refuse, never redirect: a www. redirect would build its URL from the request.
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts), www_redirect=False
    )
    docs_pages = (app.docs_url, app.swagger_ui_oauth2_redirect_url) if docs_enabled else ()
    app.add_middleware(
        SecurityHeadersMiddleware,
        hsts=settings.hsts_enabled,
        csp_exempt_paths=[path for path in docs_pages if path is not None],
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    registry = build_registry(
        app, ingest, limits, slots, live_sessions, streaming_routes=(EVENTS_ROUTE,)
    )
    app.state.metrics_registry = registry
    return app
