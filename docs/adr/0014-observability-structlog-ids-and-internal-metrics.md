# ADR 0014: Observability: structlog, request and correlation ids, metrics on an internal port

## Status

Accepted, 2026-09-26. The JSON logs, the request id and the health endpoints date from milestone 1, the correlation id from milestone 4a; milestone 4b added the Prometheus metrics of the API and the worker. The LLM's outcomes and spend (milestone 4c), the payments (5) and the shipments (6) add their metrics with their milestones. A scraper, Prometheus and Grafana, is on the roadmap.

## Context

A comment crosses four processes (the platform's webhook call, the API, the queue, a worker, then the dashboard's stream), several replicas of each. When a buyer says "I commented and got nothing", the answer must be findable from one id; when a live slows down, whether the webhook, the queue or the database is behind must show in numbers, not in guesses. The requirements ask for structured JSON logs with request and correlation ids, `/health` and `/ready`, and basic Prometheus metrics: comments received, intents detected, orders created, latency. Metrics must not become a public surface (OWASP API9): nothing about the traffic of the platform should be readable from the internet.

## Decision

### Logs and ids

- structlog renders one JSON object per line on stdout, uvicorn's and SQLAlchemy's lines included (`livecart_core.logging`); CR and LF are escaped by the encoding, and keys that name a credential are redacted. Tokens, passwords and buyer identities never become fields.
- `request_id` comes from asgi-correlation-id (`X-Request-ID`, a validated UUID, echoed in the answer). `correlation_id` is created once per comment at ingestion, carried by its queue envelope, bound to every line the worker writes about it, and repeated in the session events it causes, so the dashboard's message and the worker's log line share it.

### Health

`/health` probes nothing; `/ready` runs `SELECT 1` and `PING` in parallel, 2 s each. The worker touches a heartbeat file every 10 s while Redis answers and its consumer loop goes round. They serve the orchestrator; the metrics serve people.

### Metrics on an internal port

- Each process keeps its metrics in a `CollectorRegistry` of its own, never the client's global one, so tests build as many apps as they like and scrape exactly one. `MetricsServer` serves it with prometheus-client's WSGI server, in a daemon thread, on `METRICS_PORT` (9100). Compose exposes that port to its network only: it is never published on the host, and nginx never routes it; the smoke test probes it inside each container and checks it is published nowhere. The API's own port serves no `/metrics`, so no proxy rule has to hide one.
- The API: `http_request_duration_seconds` by route template, method and status class, from prometheus-fastapi-instrumentator (its `expose()` is never called; unknown paths share the label `none`; the SSE stream of a session is left out, since it lasts as long as its client listens); then, read at scrape time from the plain counters the use cases keep, `livecart_comments_received_total`, `livecart_queue_backpressure_total`, `livecart_queue_backlog` as the webhook last saw it, `livecart_rate_limiter_errors_total`, `livecart_buyer_lookup_not_found_total`, `livecart_sse_streams` (open) with its opened and refused counters, and the `session.updated` events that failed to reach their stream.
- The worker: `livecart_intents_detected_total{source}`, `livecart_orders_created_total`, `livecart_comments_rejected_total{reason}`, `livecart_queue_wait_seconds` (from the envelope's `enqueued_at` to the handler's start), `livecart_comment_processing_seconds` (retries included), `livecart_comment_failures_total{cause}` (deadline, transient, error), `livecart_comments_reclaimed_total`, `livecart_comments_dead_lettered_total`, `livecart_queue_trimmed_entries_total`, `livecart_queue_backlog` sampled at each reclaim, `livecart_reservations_expired_total` and `livecart_session_events_unpublished_total`.
- Labels only take values of closed lists (`RejectionReason`, `IntentSource`, the failure causes), each series present from the start at zero, so a rate of zero is a zero rather than a missing series, and the number of series is fixed: never a comment's, a session's or a buyer's id. The client's `_created` series are turned off: they would double every counter for a timestamp no query reads.
- The application layer imports no metrics library: its use cases count in plain integers or return what they did (`HandledComment`, `SweepResult`), and the entry points turn that into metrics.

## Consequences

- One id follows a comment from the platform's call to the dashboard, and one scrape per process says where a live is behind: the webhook's 503s, the queue's backlog and wait, the worker's processing time and failures.
- The metrics cannot be read from outside the container network, which also means they cannot be read at all until a scraper runs beside the containers, on the roadmap with Grafana and OpenTelemetry traces.
- A process that cannot bind its metrics port fails to start, as it would for its main port; `METRICS_PORT=` turns the endpoint off (tests, several processes on one host).

## Alternatives considered

- **`/metrics` on the API's port, hidden by nginx or by a middleware**: one forgotten rule publishes it; a port that is never published has no rule to forget.
- **The global default registry**: shared by every app of a test run, so counts leak between tests, and duplicate names fail at the second app.
- **OpenTelemetry metrics and traces now**: more to run (a collector) than an MVP needs; the ids already give a trace by hand, and Prometheus names carry over.
- **Labels by session or seller**: the number of series would grow with the business; the logs carry those ids instead.
