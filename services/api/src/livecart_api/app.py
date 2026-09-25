"""Application factory: builds the FastAPI app around injected resources."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version

from asgi_correlation_id import CorrelationIdMiddleware
from fastapi import FastAPI
from fastapi.routing import APIRoute

from livecart_api.middleware.request_context import RequestContextMiddleware
from livecart_api.resources import Resources
from livecart_api.routes.health import build_health_router
from livecart_core.settings import Settings

# Per-dependency budget of /ready.
READINESS_TIMEOUT_S = 2.0


def _operation_id(route: APIRoute) -> str:
    # The handler name ("get_health") becomes the generated client's function name.
    return route.name


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
    app.include_router(
        build_health_router(resources.readiness_checks, timeout_s=readiness_timeout_s)
    )

    # The last middleware added runs first: CorrelationIdMiddleware sets the
    # request id, then RequestContextMiddleware binds it to the logs.
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    return app
