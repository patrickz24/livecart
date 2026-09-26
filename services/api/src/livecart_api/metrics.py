"""The API's Prometheus metrics, in a registry of the app's own, served on the internal port.

Two sources, both scraped from ``create_app``'s registry:

- the HTTP latency, ``http_request_duration_seconds`` by route template,
  method and status class, recorded by prometheus-fastapi-instrumentator's
  middleware. Its ``expose()`` is never called: the metrics are served on
  ``METRICS_PORT`` (``livecart_core.metrics``), so no route of the API's own
  port serves them. The streaming routes ``build_registry`` names, the SSE
  stream of a session's events, are left out: a stream lasts as long as its
  client listens, up to an hour, which says nothing of the API's latency;
- the counters the use cases keep as plain integers, read at scrape time by
  ``UseCaseCollector``, so that the application layer never imports a
  metrics library: the comments queued (``livecart_comments_received_total``),
  the batches refused by backpressure, the backlog the webhook last saw
  (``livecart_queue_backlog``), the rate limiter's failures, the buyers'
  not-found lookups, the streams of session events open, opened and
  refused (``livecart_sse_streams``), and the ``session.updated`` events of
  a start or an end that failed to reach their stream.
"""

import re
from collections.abc import Iterable, Sequence
from typing import Final

from fastapi import FastAPI
from prometheus_client import CollectorRegistry
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.registry import Collector
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_fastapi_instrumentator.metrics import latency

from livecart_api.rate_limits import RateLimiting
from livecart_api.routes.session_events import StreamSlots
from livecart_core.application.ingest_comments import IngestComments
from livecart_core.application.live_sessions import SellerLiveSessions

# From 5 ms to 10 s: an API answer beyond that has failed already.
HTTP_LATENCY_BUCKETS: Final = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class UseCaseCollector(Collector):
    """The plain counters of the ingestion, the rate limits and the streams, read at scrape time."""

    def __init__(
        self,
        ingest: IngestComments,
        limits: RateLimiting,
        slots: StreamSlots,
        live_sessions: SellerLiveSessions,
    ) -> None:
        self._ingest = ingest
        self._limits = limits
        self._slots = slots
        self._live_sessions = live_sessions

    def collect(self) -> Iterable[Metric]:
        yield CounterMetricFamily(
            "livecart_comments_received_total",
            "Comments the webhook queued for the workers.",
            value=self._ingest.comments_received_total,
        )
        yield CounterMetricFamily(
            "livecart_queue_backpressure_total",
            "Webhook batches refused with 503 because the queue's backlog was at its depth.",
            value=self._ingest.queue_backpressure_total,
        )
        yield GaugeMetricFamily(
            "livecart_queue_backlog",
            "The comment queue's backlog (lag + pending) as the webhook last knew it.",
            value=self._ingest.queue_backlog,
        )
        yield CounterMetricFamily(
            "livecart_rate_limiter_errors_total",
            "Rate limit counts that failed (Redis unavailable) and let the request through.",
            value=self._limits.limiter_errors_total,
        )
        yield CounterMetricFamily(
            "livecart_buyer_lookup_not_found_total",
            "Order lookups by reference or token that answered 404.",
            value=self._limits.buyer_lookup_not_found_total,
        )
        yield GaugeMetricFamily(
            "livecart_sse_streams",
            "Streams of session events this instance serves now.",
            value=self._slots.open,
        )
        yield CounterMetricFamily(
            "livecart_sse_streams_opened_total",
            "Streams of session events opened.",
            value=self._slots.opened_total,
        )
        yield CounterMetricFamily(
            "livecart_sse_streams_refused_total",
            "Streams of session events refused with 503 at SSE_MAX_STREAMS.",
            value=self._slots.refused_total,
        )
        yield CounterMetricFamily(
            "livecart_session_events_unpublished_total",
            "session.updated events of a start or an end that failed to reach the stream.",
            value=self._live_sessions.unpublished_events,
        )


def build_registry(
    app: FastAPI,
    ingest: IngestComments,
    limits: RateLimiting,
    slots: StreamSlots,
    live_sessions: SellerLiveSessions,
    *,
    streaming_routes: Sequence[str] = (),
) -> CollectorRegistry:
    """The app's registry, with the latency middleware added to ``app``.

    Call it after every other middleware, so that the latency covers them
    all. ``streaming_routes`` are route templates whose duration is not
    recorded.
    """
    registry = CollectorRegistry()
    registry.register(UseCaseCollector(ingest, limits, slots, live_sessions))
    instrumentator = Instrumentator(
        should_group_status_codes=True,
        should_group_untemplated=True,
        excluded_handlers=[f"^{re.escape(route)}$" for route in streaming_routes],
        registry=registry,
    )
    instrumentator.add(latency(registry=registry, buckets=HTTP_LATENCY_BUCKETS))
    instrumentator.instrument(app)
    return registry
