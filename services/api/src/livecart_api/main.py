"""Composition root of the API process, served by ``uvicorn livecart_api.main:app``.

Importing this module validates the settings (a misconfiguration stops the
process before it listens), switches logging to JSON (uvicorn's own loggers
included) and builds the resources. Nothing connects until the first request;
the metrics endpoint listens on ``METRICS_PORT`` once the app starts.
"""

from livecart_api.app import create_app
from livecart_api.resources import build_resources
from livecart_core.logging import configure_logging
from livecart_core.settings import Settings

settings = Settings()
configure_logging(service="api", level=settings.log_level)
app = create_app(settings, build_resources(settings), metrics_port=settings.metrics_port)
