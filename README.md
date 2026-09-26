# LiveCart

LiveCart turns live-stream comments such as "SOLD A12 x2" into purchase intents, reserves the stock atomically and issues a short order reference.
Buyers pay with Stripe Checkout (test mode) in a white-label Expo app, while sellers follow intents, orders and stock in real time on a React dashboard.
A FastAPI API, an asyncio worker and a Redis Streams queue absorb comment spikes without ever overselling.

> **Personal demo project, fictional brand, not affiliated with any company.**

## Status

Milestone 3 of 9: the HTTP API, the atomic stock reservation and the order references, on top of the domain and parser of milestone 2 and the monorepo, CI and Docker stack of milestone 1.

- **Schema** (`livecart_core.infrastructure.db`): every table of the data model, from sellers to shipments, as SQLAlchemy Core metadata and an Alembic revision that a test compares with it, with UUID v7 keys, integer minor units and the plan's named constraints ([ADR 0003](docs/adr/0003-backend-stack-fastapi-sqlalchemy-core-alembic.md)).
- **Reservation**: one conditional `UPDATE` in a READ COMMITTED transaction reserves the stock, and a partial unique index allows one pending order per buyer and product. A thousand concurrent reservations of 200 units, three times over, never oversell ([ADR 0005](docs/adr/0005-anti-oversell-conditional-update.md)).
- **Order references and tokens**: `LC-` and six Crockford symbols, drawn again after a collision, and an access token derived by HMAC and stored as a hash only. The reference alone shows an order's status; its details need the token ([ADR 0007](docs/adr/0007-order-reference-and-hmac-access-token.md)).
- **Seller API**: login with argon2id, access tokens of 15 minutes, rotating refresh tokens in an HttpOnly cookie with a 30 s grace between tabs ([ADR 0010](docs/adr/0010-seller-auth-short-jwt-rotating-refresh-argon2id.md)); live sessions from draft to live to ended; products; restock by delta under an `Idempotency-Key`. Every route filters by the seller and answers 404 for another seller's objects.
- **Buyer API**: the storefront of a seller by its slug, and an order by its reference, in two tiers.
- **Perimeter**: rate limits in Redis (per address, per seller, per account and per token), security headers, trusted hosts, a 1 MiB body limit and uvicorn trusting nginx alone for the client address ([ADR 0011](docs/adr/0011-perimeter-and-hardening.md)).
- **Demo data and accounts**: the one-shot `seed` service creates the demo sellers and sessions; `livecart-create-seller` opens other accounts ([CONTRIBUTING.md](CONTRIBUTING.md#demo-data)).

Nothing ingests comments yet: the signed webhook, the queue and the worker that reserves for each comment arrive in milestone 4a, and the worker still only runs its heartbeat. The dashboard is a shell page that checks the API. The features described above arrive milestone by milestone; this README grows with them.

## Quickstart

With Docker (Docker Desktop with the WSL 2 backend on Windows):

```bash
git clone <repository-url> livecart && cd livecart
cp .env.example .env
docker compose up --build
```

Then open the dashboard on <http://localhost:8080>, and the interactive API documentation, in development only, on <http://127.0.0.1:8000/docs>, where `POST /api/v1/auth/login` signs in the demo seller: `demo-seller@livecart.test`, with the `SEED_DEMO_PASSWORD` of `.env`. The demo storefront is <http://localhost:8080/api/v1/storefronts/demo-seller>. `docker compose down --volumes` (or `make reset`) removes the stack and its data.

The copy of `.env.example` is the one step before `docker compose up`: `compose.yaml` gives no default to any secret and refuses to start without them, so that no password ever has a default value in a versioned file. The `change-me-*` values are development placeholders, refused when `APP_ENV=production`.

| Service    | Role                                                    | Published on                              |
| ---------- | ------------------------------------------------------- | ----------------------------------------- |
| `web`      | nginx: the dashboard, and the API proxied on `/api/`    | `8080` (every interface, for a LAN phone) |
| `api`      | FastAPI behind uvicorn                                  | `127.0.0.1:8000`                          |
| `worker`   | asyncio worker (heartbeat only for now)                 | not published                             |
| `migrate`  | one-shot Alembic upgrade, as the schema owner           | not published                             |
| `seed`     | one-shot demo data, created only where missing          | not published                             |
| `postgres` | PostgreSQL 16, databases `livecart` and `livecart_test` | `127.0.0.1:55432`                         |
| `redis`    | Redis 8 with a password, append-only file               | `127.0.0.1:56379`                         |

## Demo

_To be completed: `docs/assets/demo.gif`, recorded once the dashboard and the buyer app exist._

## Architecture

_To be completed: the Mermaid diagram of the comment path, from the signed webhook to the SSE event._

The Python code is layered, and imports only go inwards ([ADR 0002](docs/adr/0002-layered-architecture-ports-and-state-machines.md)):

- **domain** (`livecart_core.domain`): value objects, state machines and the parser, standard library only;
- **application** (`livecart_core.application`): the ports, the data that crosses them and the use cases, with pydantic;
- **infrastructure** (`livecart_core.infrastructure`): the SQL repositories, the Redis idempotency store and rate limiter, argon2id and the clock today, then the queue, the payment providers, the carriers and the LLM;
- **entry points** (`livecart_api`, `livecart_worker`, the CLIs): routes, loops and the wiring of the adapters.

An architecture test fails on any import that breaks the rule of the two inner layers.

## Features

_To be completed as each milestone lands: comment ingestion and intent parsing, atomic reservations and order references, Stripe Checkout, mock carrier, real-time dashboard, white-label buyer app._

## Key decisions and trade-offs

_To be completed as the milestones land; every record is in [`docs/adr/`](docs/adr/README.md)._

| Decision                                                                                                                                              | Trade-off                                                                                                                                |
| ----------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| One `cp .env.example .env` before `docker compose up`                                                                                                 | One extra command, so that secrets never have a default in a versioned file                                                              |
| One Python image for the API, the worker, migrate and seed                                                                                            | A slightly larger image per process, but migrations always ship with the code that uses them                                             |
| A deterministic parser first, an LLM only for its ambiguities ([ADR 0006](docs/adr/0006-deterministic-parser-then-llm-fallback.md))                   | A phrasing the lexicon misses is rejected or costs a model call until it is added; clear orders never wait for, pay for or trust a model |
| Protocol ports with in-memory fakes, hand-written state machines ([ADR 0002](docs/adr/0002-layered-architecture-ports-and-state-machines.md))         | The fakes hold rules the SQL adapters must match, so both are tested on the same scenarios; every rule is tested without Docker          |
| One conditional `UPDATE` reserves the stock, in READ COMMITTED ([ADR 0005](docs/adr/0005-anti-oversell-conditional-update.md))                        | Reservations of one product wait on its row lock, one short transaction each; none oversells and none retries                            |
| A public reference, and an access token derived by HMAC for the rest ([ADR 0007](docs/adr/0007-order-reference-and-hmac-access-token.md))             | The buyer keeps two things; a leak of `ORDER_TOKEN_KEY` would expose every token, and its rotation is not implemented                    |
| Access tokens of 15 minutes, rotating refresh tokens with a 30 s grace ([ADR 0010](docs/adr/0010-seller-auth-short-jwt-rotating-refresh-argon2id.md)) | An access token lives until it expires; tabs racing on one refresh token keep the seller signed in                                       |
| Rate limits in Redis, logins closed while it is down ([ADR 0011](docs/adr/0011-perimeter-and-hardening.md))                                           | A Redis outage refuses new logins; fixed windows let a burst of up to twice a limit through at their edge                                |

## Out of scope

_To be completed: real Facebook, Instagram and TikTok integrations, PayPal, Viva Wallet and real carriers, buyer accounts, refunds, iOS builds, real deployment._

## Roadmap

_To be completed._

## Development

Prerequisites, `make` targets with their raw commands for PowerShell, commit conventions and port overrides: see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
