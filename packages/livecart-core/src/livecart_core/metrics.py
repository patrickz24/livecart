"""The Prometheus endpoint of a process: its own registry, on an internal port.

The API and the worker each keep their metrics in a ``CollectorRegistry`` of
their own, never the client's global one, so that a test builds as many apps
as it likes and scrapes exactly one. ``MetricsServer`` serves a registry on
``METRICS_PORT`` (9100 in the containers) from a daemon thread, with the
client's own WSGI server: ``GET /metrics``, in the text format.

That port is internal on purpose: compose never publishes it on the host and
nginx never routes it, so nothing outside the container network reads the
metrics, and the API's own port serves no ``/metrics`` route that would need
a rule to hide it. A scraper runs next to the containers (Prometheus on the
roadmap).
"""

import threading
from collections.abc import Callable
from typing import Final, Self, cast
from wsgiref.simple_server import WSGIServer

from prometheus_client import CollectorRegistry, disable_created_metrics, start_http_server

# Every interface of the container: its network is the boundary, not its host.
METRICS_HOST: Final = "0.0.0.0"  # noqa: S104
DEFAULT_METRICS_PORT: Final = 9100

# No "<name>_created" series: they would double every counter and histogram
# for a creation timestamp that no query of ours reads. The switch is the
# client's, process-wide, so it is thrown once, when the metrics are imported.
# The client ships it without annotations: the cast gives it its real type.
cast(Callable[[], None], disable_created_metrics)()


class MetricsServer:
    """``GET /metrics`` of one registry, served from a daemon thread until ``close``."""

    def __init__(self, server: WSGIServer, thread: threading.Thread) -> None:
        self._server = server
        self._thread = thread

    @classmethod
    def start(cls, registry: CollectorRegistry, *, port: int, host: str = METRICS_HOST) -> Self:
        """Listen on ``host:port``; port 0 takes a free one, which ``port`` then reports."""
        server, thread = start_http_server(port, addr=host, registry=registry)
        return cls(server, thread)

    @property
    def port(self) -> int:
        return int(self._server.server_port)

    def close(self) -> None:
        """Stop serving and release the port."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
