# ADR 0009: Real time: Server-Sent Events over a Redis stream per session, published after commit

## Status

Accepted, 2026-09-26. Milestone 4b implemented the whole path: the worker and the API publish each session's events to its Redis stream after their commit, and the API streams them to the session's seller over SSE, after a snapshot. The dashboard that reads the stream arrives in milestone 7.

## Context

During a live, the seller watches intents, orders and stock change within a second of each comment. The changes happen in the worker (a comment reserved or rejected, a reservation expired, later a payment) and in the API (a session started or ended), several processes of each; the dashboard is one browser tab per seller, which may lose its connection, sleep and come back. Nothing may be lost silently on a reconnection, yet a lost event must never undo a reservation: the database is the truth, the stream only tells the dashboard what changed. The dashboard authenticates with a short access token (15 minutes, ADR 0010) sent as `Authorization: Bearer`, never in a URL.

## Decision

### Publication after commit, one stream per session

- Each change builds its events after its commit (`livecart_core.application.session_events`): `intent.recorded`, `comment.rejected`, `order.updated`, `stock.updated`, `session.updated`, and `shipment.updated` from milestone 6. Each has a payload model (`IntentPayload`, `OrderPayload`...) holding identifiers, statuses, amounts and counters, never a buyer's platform identity, an address or a token.
- `RedisSessionEvents` appends them to `{p}:events:session:{id}`, in one Lua script: `XADD ... MINID ~ <TIME - 15 min>`, then `EXPIRE 86400`, or `3600` once the session has ended (a `session.updated` with the status `ended`, and every event after it, such as an order expiring after the end). Redis's own `TIME` dates the trim, the clock that dates the entry ids.
- Best effort: a Redis failure is logged (`session_event_unpublished`), counted (`livecart_session_events_unpublished_total`) and never retried; the commit stands, and the dashboard catches up from its next snapshot. The dashboard's reducer upserts by the ids of each payload, so an event seen twice (a redelivered comment republishes its stored outcome) is harmless.

### The stream: snapshot, cursor, events

- `GET /api/v1/live-sessions/{id}/events`, for the session's seller only (404 for another seller, as for an unknown id), with FastAPI's native SSE (`EventSourceResponse`, `ServerSentEvent`). `WatchLiveSession` (application layer) decides what the stream says, before the first byte where it can, so that a refusal or a failure there is a plain HTTP answer:
  - without `Last-Event-ID`: the stream's cursor, read by `XREVRANGE + - COUNT 1`, **then** the snapshot, `event: snapshot` with the session, its products and stock, its latest 200 intents (with their order), rejections and orders, and `events_cursor`. An event published between the two reads is in the snapshot and read again after it, never missed. The snapshot is one REPEATABLE READ, read-only transaction, so that an order and its product's stock agree;
  - with `Last-Event-ID` (the id of the last message received, which is the stream's entry id): the events after it, without a snapshot, unless some are gone: then `event: stream.gap`, and a new snapshot.
  - then `XREAD BLOCK` from the cursor, 100 events and 2 s at most per read, and a gap check once a minute, in case a slow reader fell behind the retention.
- `event: stream.close` ends a stream at `min(access token expiry - 5 s, 1 h)`, with an SSE `retry` of 1 s: the dashboard refreshes its token if needed and reconnects with its last id. A stream never outlives the token that opened it. A failure of Redis or of the database mid-stream closes it the same way (`unavailable`), logged with its cause.
- The heartbeat is FastAPI's: `: ping` after 15 s without a message, with `Cache-Control: no-cache` and `X-Accel-Buffering: no`, which nginx honours; 15 s stays well within nginx's `proxy_read_timeout` of 60 s.
- Each instance serves at most `SSE_MAX_STREAMS` (200) streams, each holding one Redis connection in `XREAD BLOCK`: the next one answers 503 with `Retry-After`. The streams read through a Redis client of their own, whose pool holds `SSE_MAX_STREAMS` connections (`Resources.event_streams`): a stream holds one connection at most at a time, so that pool never runs out before the slots do, and the streams never take a connection from the shared client, whose pool keeps redis-py's default of 100 for the webhook, the rate limits, the logins, the idempotency keys, the events published and `/ready`. One seller holds at most `SSE_MAX_STREAMS_PER_SELLER` (5) streams on an instance, counted by its id while they are open: its next one answers 429 with `Retry-After`, its own excess rather than the instance's capacity, so that one seller can never take every slot from the others; a dashboard needs one or two, and a stream whose client vanished keeps its slot until the server notices. The refusals are counted by reason, `livecart_sse_streams_refused_total{reason="seller_limit"|"instance_full"}`. A stream counts once against the seller's rate limit, when it opens, and its duration stays out of `http_request_duration_seconds`.

### The gap, on the safe side

Trimming does not record the ids it removes (`max-deleted-entry-id` only follows `XDEL`), so `has_gap` cannot tell a reader whose cursor was exactly the last trimmed event from one that missed some. It says "gap" whenever the cursor is older than the stream's first event and the stream has lost an entry (`entries-added` above `length`), when the cursor predates a stream started again after it expired, when the stream no longer exists, and when the cursor is past the last id. A false gap costs one snapshot; a missed one would lose events silently.

### The contract

`LiveSessionEvent` is a discriminated union on `type` of the eight messages after the snapshot (the six events, `stream.gap`, `stream.close`), each with the envelope of a `SessionEvent` and its payload. The API checks every relayed event against its member (one that fits none is logged and skipped rather than ending the stream). FastAPI documents a stream of `ServerSentEvent` as generic items, so `LiveCartApi.openapi` adds `LiveSessionSnapshot` and `LiveSessionEvent` to `components.schemas` from their pydantic schemas, refuses a component generated twice differently, and points the route's `itemSchema` at them. The generated TypeScript client exports the union, which `contract-checks.ts` checks at compile time.

## Consequences

- A dashboard that reconnects within 15 minutes resumes where it stopped, without re-reading anything; later, or after a restart of Redis without its data, it gets a gap and a snapshot, never a silent hole.
- An event may reach the stream once, twice (a redelivered comment) or not at all (a Redis failure after the commit): the snapshot is what makes the dashboard right, the stream only makes it quick. An outbox table would make publication durable; it is on the roadmap, since the snapshot already bounds what a lost event costs.
- One blocked Redis connection per open stream: 200 per API instance by default, from the streams' own pool, beside the 100 of the shared pool, far below Redis's 10 000 clients; a fan-out reader per instance would share one, and is not needed at this size. The review of milestone 4 found the streams on the shared pool: 142 streams of one seller took its 100 connections, and every other use of Redis failed with `MaxConnectionsError` (webhooks and logins answered 503, `/ready` failed, the fail-open limits stopped counting) until they closed; `test_stream_pool_redis.py` now holds 110 streams open while a webhook, a login and `/ready` succeed.
- The read wait stays under the 5 s socket timeout of redis-py 8's client, which the live check of this milestone found: a `BLOCK 5000` that found nothing raised `TimeoutError` instead of returning empty.
- A stream's memory is about 15 minutes of events of one session, gone a day after its last event, an hour after the session ended.

## Alternatives considered

- **Redis Pub/Sub**: no history, so no `Last-Event-ID` resume and no cursor to pair with a snapshot; a reconnection would always need a snapshot, and an event published during the snapshot's read would be lost.
- **WebSocket**: bidirectional, which the dashboard does not need, with its own framing, reconnection and authentication to write; SSE is plain HTTP through nginx, and the browser's reconnection protocol (`Last-Event-ID`) is the resume.
- **The access token in the URL** (for `EventSource`, which sends no header): it would reach nginx's and uvicorn's access logs; the dashboard (milestone 7) reads the stream with `fetch` and `eventsource-parser` instead, and the generated client's `sse.get` does the same.
- **sse-starlette 3.4.11**: the plan's fallback if FastAPI's native SSE lacked the 15 s keep-alive; it has it, so there is one dependency less.
- **Polling the REST API**: a request per second per dashboard for changes that come in bursts, and no ordering between them.
