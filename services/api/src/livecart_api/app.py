"""Application factory: builds the FastAPI app around injected resources."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version

import structlog
from asgi_correlation_id import CorrelationIdMiddleware
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from livecart_api import errors
from livecart_api.access_tokens import AccessTokens
from livecart_api.middleware.request_context import RequestContextMiddleware
from livecart_api.resources import Resources
from livecart_api.routes.auth import SellerAuth, build_auth_router
from livecart_api.routes.health import build_health_router
from livecart_api.routes.live_sessions import build_live_session_router
from livecart_api.routes.orders import build_order_router
from livecart_api.routes.products import build_product_router
from livecart_api.routes.storefronts import build_storefront_router
from livecart_core.application.buyer_orders import LookUpOrder
from livecart_core.application.idempotency import IdempotentRequests
from livecart_core.application.live_sessions import SellerLiveSessions
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.application.products import SellerProducts
from livecart_core.application.seller_auth import (
    AuthenticateSeller,
    RevokeRefreshFamily,
    RotateRefreshToken,
)
from livecart_core.application.storefront import ViewStorefront
from livecart_core.order_tokens import OrderTokens
from livecart_core.settings import Settings

logger = structlog.get_logger(__name__)

# Per-dependency budget of /ready.
READINESS_TIMEOUT_S = 2.0
# What a client waits before trying again after a 503.
RETRY_AFTER_S = 1


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
) -> FastAPI:
    """Create the app. It owns ``resources`` and closes them when it shuts down."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await resources.aclose()

    # Interactive docs and the schema route are development conveniences only;
    # the committed OpenAPI file is generated from app.openapi() directly.
    docs_enabled = not settings.is_production
    app = FastAPI(
        title="LiveCart API",
        version=version("livecart-api"),
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
        generate_unique_id_function=_operation_id,
    )
    seller_auth = build_seller_auth(settings, resources)
    # Read by CurrentSeller (livecart_api.deps), which every seller route declares.
    app.state.access_tokens = seller_auth.access_tokens
    app.include_router(
        build_health_router(resources.readiness_checks, timeout_s=readiness_timeout_s)
    )
    app.include_router(build_auth_router(seller_auth))
    app.include_router(
        build_live_session_router(SellerLiveSessions(resources.live_sessions, resources.clock))
    )
    app.include_router(
        build_product_router(
            SellerProducts(resources.products, resources.clock),
            IdempotentRequests(resources.idempotency),
        )
    )
    app.include_router(
        build_storefront_router(
            ViewStorefront(resources.sellers, resources.live_sessions, resources.products)
        )
    )
    # Checking a token needs its stored hash only, never ORDER_TOKEN_KEY.
    app.include_router(build_order_router(LookUpOrder(resources.orders, OrderTokens.verify)))
    app.add_exception_handler(ServiceUnavailable, _service_unavailable)
    errors.install(app)

    # The last middleware added runs first: CorrelationIdMiddleware sets the
    # request id, then RequestContextMiddleware binds it to the logs.
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    return app
