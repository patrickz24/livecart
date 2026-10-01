# LiveCart

LiveCart turns live-stream comments such as "SOLD A12 x2" into purchase intents, reserves the stock atomically and issues a short order reference.
Buyers pay with Stripe Checkout (test mode) in a white-label Expo app, while sellers follow intents, orders and stock in real time on a React dashboard.
A FastAPI API, an asyncio worker and a Redis Streams queue absorb comment spikes without ever overselling.

> **Personal demo project, fictional brand, not affiliated with any company.**

## Status

Milestone 5 of 9: the payment. It stands on the comment ingestion, the worker, the real-time events and the optional LLM fallback of milestone 4, the HTTP API, the atomic stock reservation and the order references of milestone 3, the domain and parser of milestone 2, and the monorepo, CI and Docker stack of milestone 1.

- **Payment provider** (5): one `PaymentProvider` port of four calls (create a hosted session, expire it, read it back, verify a webhook). The mock is the default and needs no account and no key: its hosted page is served by the API in development, and its Pay button posts a signed webhook as a provider would. Stripe Checkout runs in test mode with your own test keys; PayPal and Viva are documented stubs that refuse to start ([ADR 0008](docs/adr/0008-payments-provider-port-hosted-checkout-expire-first.md)).
- **Checkout** (5): with the order's token, the buyer records an address, then `POST /api/v1/orders/{reference}/checkout` extends the reservation to at least now + `PAYMENT_GRACE_SECONDS` (600 s) and only then creates the provider's session, under an idempotency key per attempt, 3 attempts at most. The provider sends the buyer back to a bridge page that reopens the app with the reference, never the token.
- **Webhooks** (5): the signature is checked before anything is read, the event reduced to an allow-list without the customer's details and recorded once in PostgreSQL, then 200, with Redis up or down. Every worker applies that registry every 2 s, each event once whatever its deliveries, the amount checked against the order's.
- **Expire-first** (5): the sweep never releases the stock of an order whose payment is in flight before the provider has expired the session. One worker at a time asks about each order (a 30 s lease), a session the buyer paid first is settled as paid, and an attempt left without its session gets the same one again under its key ([ADR 0005](docs/adr/0005-anti-oversell-conditional-update.md)). [CONTRIBUTING.md](CONTRIBUTING.md#payments) runs one mock payment end to end, and one expiry.

The Stripe adapter is tested offline only, against an in-memory stand-in of its API: no test calls Stripe, and the `stripe` compose profile (the Stripe CLI forwarding a test account's events through nginx) has not been run against a real account yet.

From milestone 4, the comment path and the events the payment builds on:

- **Comment ingestion** (4a): one signed webhook per platform, `POST /api/v1/webhooks/comments/{platform}`, in a common format (`IncomingComment`), its HMAC-SHA256 signature checked before anything is read. The simulator's adapter is real; Facebook, Instagram and TikTok are documented stubs that refuse to start. Each batch goes to a Redis stream in one pipeline and the webhook answers 202, or 503 with `Retry-After` once the workers' backlog reaches `QUEUE_MAX_DEPTH` ([ADR 0004](docs/adr/0004-redis-streams-queue-and-leased-inbox.md)).
- **Worker** (4a): a consumer group reads the stream; a PostgreSQL inbox, with a lease per delivery, makes every comment count once whatever its deliveries; the parser reads it and one transaction reserves the stock and opens the order. Idle deliveries are reclaimed after 120 s, dead-lettered after 3 deliveries, and the settled history trimmed safely. Every 5 s, the reservations nobody paid expire and their stock is for sale again ([ADR 0005](docs/adr/0005-anti-oversell-conditional-update.md)). The buyer receives the order's reference and its access token, derived again at each delivery, on a development stream.
- **Comment simulator** (4a): a live audience on the demo session, comments arriving as a Poisson process, mostly orders, some noise and some ambiguous ones ([CONTRIBUTING.md](CONTRIBUTING.md#comment-simulator)).
- **Real time** (4b): each change is published to its session's Redis stream after its commit, and `GET /api/v1/live-sessions/{id}/events` streams it to the seller over SSE, after a snapshot and its cursor, resumable with `Last-Event-ID` ([ADR 0009](docs/adr/0009-realtime-sse-over-a-redis-stream-per-session.md)). The `LiveSessionEvent` union is in the OpenAPI contract and the generated client.
- **Observability** (4a to 4c): JSON logs with request and correlation ids, and Prometheus metrics on each process's internal port 9100, published nowhere ([ADR 0014](docs/adr/0014-observability-structlog-ids-and-internal-metrics.md)).
- **LLM fallback** (4c): optional. With `ANTHROPIC_API_KEY` set, the comments the parser leaves ambiguous go to Claude (`claude-opus-5` at low effort) in structured outputs, once per comment, and the domain checks the answer against the catalog before anything is reserved. A budget shared in Redis (200 calls per session, $1.00 a day) and 4 slots per worker, waited for 7 s at most, bound the cost and the wait; `scripts/llm_eval.py` measures a model on the corpus before any change ([ADR 0006](docs/adr/0006-deterministic-parser-then-llm-fallback.md)). It has not been run against the real API yet. Without a key, everything runs on the parser alone.

From milestone 3, the API, the reservation and the references that the worker builds on:

- **Schema** (`livecart_core.infrastructure.db`): every table of the data model, from sellers to shipments, as SQLAlchemy Core metadata and an Alembic revision that a test compares with it, with UUID v7 keys, integer minor units and the plan's named constraints ([ADR 0003](docs/adr/0003-backend-stack-fastapi-sqlalchemy-core-alembic.md)).
- **Reservation**: one conditional `UPDATE` in a READ COMMITTED transaction reserves the stock, and a partial unique index allows one pending order per buyer and product. A thousand concurrent reservations of 200 units, three times over, never oversell ([ADR 0005](docs/adr/0005-anti-oversell-conditional-update.md)).
- **Order references and tokens**: `LC-` and six Crockford symbols, drawn again after a collision, and an access token derived by HMAC and stored as a hash only. The reference alone shows an order's status; its details need the token ([ADR 0007](docs/adr/0007-order-reference-and-hmac-access-token.md)).
- **Seller API**: login with argon2id, access tokens of 15 minutes, rotating refresh tokens in an HttpOnly cookie with a 30 s grace between tabs ([ADR 0010](docs/adr/0010-seller-auth-short-jwt-rotating-refresh-argon2id.md)); live sessions from draft to live to ended; products; restock by delta under an `Idempotency-Key`. Every route filters by the seller and answers 404 for another seller's objects.
- **Buyer API**: the storefront of a seller by its slug, and an order by its reference, in two tiers.
- **Perimeter**: rate limits in Redis (per address, per seller, per account and per token), security headers, trusted hosts, a 1 MiB body limit and uvicorn trusting nginx alone for the client address ([ADR 0011](docs/adr/0011-perimeter-and-hardening.md)).
- **Demo data and accounts**: the one-shot `seed` service creates the demo sellers and sessions; `livecart-create-seller` opens other accounts ([CONTRIBUTING.md](CONTRIBUTING.md#demo-data)).

The dashboard is still a shell page that checks the API: the carrier, the dashboard itself and the buyer app arrive with milestones 6 to 8. The features described above arrive milestone by milestone; this README grows with them.

## Quickstart

With Docker (Docker Desktop with the WSL 2 backend on Windows):

```bash
git clone <repository-url> livecart && cd livecart
cp .env.example .env
docker compose up --build
```

Then open the dashboard on <http://localhost:8080>, and the interactive API documentation, in development only, on <http://127.0.0.1:8000/docs>, where `POST /api/v1/auth/login` signs in the demo seller: `demo-seller@livecart.test`, with the `SEED_DEMO_PASSWORD` of `.env`. The demo storefront is <http://localhost:8080/api/v1/storefronts/demo-seller>. `docker compose down --volumes` (or `make reset`) removes the stack and its data.

The stack needs no third-party key: payments go through the mock provider, and to try the LLM fallback, set `ANTHROPIC_API_KEY` in `.env` (the always-on simulator then spends the daily budget; `docker compose stop simulator` pauses it). Stripe in test mode needs your own test keys and the `stripe` compose profile ([CONTRIBUTING.md](CONTRIBUTING.md#payments)).

The copy of `.env.example` is the one step before `docker compose up`: `compose.yaml` gives no default to any secret and refuses to start without them, so that no password ever has a default value in a versioned file. The `change-me-*` values are development placeholders, refused when `APP_ENV=production`.

| Service      | Role                                                            | Published on                              |
| ------------ | --------------------------------------------------------------- | ----------------------------------------- |
| `web`        | nginx: the dashboard, and the API proxied on `/api/`            | `8080` (every interface, for a LAN phone) |
| `api`        | FastAPI behind uvicorn                                          | `127.0.0.1:8000`                          |
| `worker`     | asyncio worker: comments, reclaim, expiry sweep, payment events | not published                             |
| `simulator`  | comment simulator on the demo session                           | not published                             |
| `migrate`    | one-shot Alembic upgrade, as the schema owner                   | not published                             |
| `seed`       | one-shot demo data, created only where missing                  | not published                             |
| `postgres`   | PostgreSQL 16, databases `livecart` and `livecart_test`         | `127.0.0.1:55432`                         |
| `redis`      | Redis 8 with a password, append-only file                       | `127.0.0.1:56379`                         |
| `stripe-cli` | `stripe` profile only: Stripe's events forwarded to the API     | not published                             |

## Demo

_To be completed: `docs/assets/demo.gif`, recorded once the dashboard and the buyer app exist._

## Architecture

_To be completed: the Mermaid diagram of the comment path, from the signed webhook to the SSE event._

The Python code is layered, and imports only go inwards ([ADR 0002](docs/adr/0002-layered-architecture-ports-and-state-machines.md)):

- **domain** (`livecart_core.domain`): value objects, state machines and the parser, standard library only;
- **application** (`livecart_core.application`): the ports, the data that crosses them and the use cases, with pydantic;
- **infrastructure** (`livecart_core.infrastructure`): the SQL repositories, the Redis comment queue, session streams, idempotency store, rate limiter and LLM budget, the platform adapters, argon2id and the clock today, then the payment providers and the carriers;
- **entry points** (`livecart_api`, `livecart_worker`, the CLIs): routes, loops and the wiring of the adapters; the worker also holds the Claude adapter, the one module that needs the Anthropic SDK.

An architecture test fails on any import that breaks the rule of the two inner layers.

## Features

_To be completed as each milestone lands: comment ingestion and intent parsing, atomic reservations and order references, Stripe Checkout, mock carrier, real-time dashboard, white-label buyer app._

## Key decisions and trade-offs

_To be completed as the milestones land; every record is in [`docs/adr/`](docs/adr/README.md)._

| Decision                                                                                                                                                                                                                                  | Trade-off                                                                                                                                                                                              |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| One `cp .env.example .env` before `docker compose up`                                                                                                                                                                                     | One extra command, so that secrets never have a default in a versioned file                                                                                                                            |
| One Python image for the API, the worker, migrate and seed                                                                                                                                                                                | A slightly larger image per process, but migrations always ship with the code that uses them                                                                                                           |
| A deterministic parser first, an LLM only for its ambiguities ([ADR 0006](docs/adr/0006-deterministic-parser-then-llm-fallback.md))                                                                                                       | A phrasing the lexicon misses is rejected or costs a model call until it is added; clear orders never wait for, pay for or trust a model                                                               |
| Protocol ports with in-memory fakes, hand-written state machines ([ADR 0002](docs/adr/0002-layered-architecture-ports-and-state-machines.md))                                                                                             | The fakes hold rules the SQL adapters must match, so both are tested on the same scenarios; every rule is tested without Docker                                                                        |
| One conditional `UPDATE` reserves the stock, in READ COMMITTED ([ADR 0005](docs/adr/0005-anti-oversell-conditional-update.md))                                                                                                            | Reservations of one product wait on its row lock, one short transaction each; none oversells, and a long burst on one product can pass the 2 s lock timeout, which costs a retry                       |
| A public reference, and an access token derived by HMAC for the rest ([ADR 0007](docs/adr/0007-order-reference-and-hmac-access-token.md))                                                                                                 | The buyer keeps two things; a leak of `ORDER_TOKEN_KEY` would expose every token, and its rotation is not implemented                                                                                  |
| Access tokens of 15 minutes, rotating refresh tokens with a 30 s grace ([ADR 0010](docs/adr/0010-seller-auth-short-jwt-rotating-refresh-argon2id.md))                                                                                     | An access token lives until it expires; tabs racing on one refresh token keep the seller signed in                                                                                                     |
| Rate limits in Redis, logins closed while it is down ([ADR 0011](docs/adr/0011-perimeter-and-hardening.md))                                                                                                                               | A Redis outage refuses new logins; fixed windows let a burst of up to twice a limit through at their edge                                                                                              |
| Redis Streams with backpressure on the backlog, and a leased PostgreSQL inbox ([ADR 0004](docs/adr/0004-redis-streams-queue-and-leased-inbox.md))                                                                                         | Delivery is at least once and the inbox makes it harmless; after a worker crashes, its comments wait about two minutes for a reclaim                                                                   |
| SSE over a Redis stream per session, published after commit ([ADR 0009](docs/adr/0009-realtime-sse-over-a-redis-stream-per-session.md))                                                                                                   | Best effort: an event lost to a Redis failure is caught up from the next snapshot, never retried                                                                                                       |
| Claude only for the parser's ambiguities, in structured outputs, behind a budget and 4 slots per worker ([ADR 0006](docs/adr/0006-deterministic-parser-then-llm-fallback.md))                                                             | A comment past the budget or the slot wait is a missed sale rather than a late one; the model's answer is checked before it reserves anything                                                          |
| A hosted checkout whose session the sweep expires at the provider before it releases any stock, and a PostgreSQL registry of webhooks the workers poll ([ADR 0008](docs/adr/0008-payments-provider-port-hosted-checkout-expire-first.md)) | Up to 2 s before an order shows paid, and the stock of a payment in flight stays reserved while the provider cannot be reached; a payment is never applied twice, nor lost to a Redis or worker outage |
| Metrics on an internal port only ([ADR 0014](docs/adr/0014-observability-structlog-ids-and-internal-metrics.md))                                                                                                                          | Nothing outside the container network can read them, nor anyone until a scraper runs beside the containers                                                                                             |

## Out of scope

_To be completed: real Facebook, Instagram and TikTok integrations, PayPal, Viva Wallet and real carriers, buyer accounts, refunds, iOS builds, real deployment._

## Roadmap

_To be completed._

## Development

Prerequisites, `make` targets with their raw commands for PowerShell, commit conventions and port overrides: see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
