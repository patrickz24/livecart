# LiveCart

LiveCart turns live-stream comments such as "SOLD A12 x2" into purchase intents, reserves the stock atomically and issues a short order reference.
Buyers pay with Stripe Checkout (test mode) in a white-label Expo app, while sellers follow intents, orders and stock in real time on a React dashboard.
A FastAPI API, an asyncio worker and a Redis Streams queue absorb comment spikes without ever overselling.

> **Personal demo project, fictional brand, not affiliated with any company.**

## Status

Milestone 2 of 9: the domain and the comment parser, on top of the monorepo, tooling, CI and Docker stack of milestone 1.

- **Domain** (`livecart_core.domain`, standard library only): the value objects (`Money`, `ProductCode`, `OrderReference`, drawn from an injected random source and read back tolerantly in Crockford base 32), the state machines of orders, payment attempts, shipments, live sessions and comments, and a deterministic parser that reads a French or English comment against the session catalog into a match, an ambiguity (the only kind a later LLM fallback will see) or a rejection.
- **Corpus**: 200 French and English comments with their exact expected results (`packages/livecart-core/src/livecart_core/testing/corpus/`), against which the parser is tested field by field, alongside unit and seeded property tests.
- **Ports and fakes** (`livecart_core.application`): every port the later milestones need, as a `typing.Protocol`, with a deterministic in-memory fake of each in `livecart_core.testing.fakes`.
- **Tests**: the unit tests need no Docker (`uv run --locked pytest -m "not integration" --cov`), and the coverage gate fails below 80 % branch coverage of the domain and the application.

Nothing calls the parser or the ports yet: the API only answers `/health` and `/ready`, the worker only runs its heartbeat, and the dashboard is a shell page that checks the API. The features described above arrive milestone by milestone; this README grows with them.

## Quickstart

With Docker (Docker Desktop with the WSL 2 backend on Windows):

```bash
git clone <repository-url> livecart && cd livecart
cp .env.example .env
docker compose up --build
```

Then open the dashboard on <http://localhost:8080>, and the interactive API documentation, in development only, on <http://127.0.0.1:8000/docs>. `docker compose down --volumes` (or `make reset`) removes the stack and its data.

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
- **application** (`livecart_core.application`): the ports and the data that crosses them, with pydantic; the use cases join it from milestone 3;
- **infrastructure** (`livecart_core.infrastructure`): the PostgreSQL engine and the Redis client today, then the SQL repositories, the queue, the payment providers, the carriers and the LLM;
- **entry points** (`livecart_api`, `livecart_worker`, the CLIs): routes, loops and the wiring of the adapters.

An architecture test fails on any import that breaks the rule of the two inner layers.

## Features

_To be completed as each milestone lands: comment ingestion and intent parsing, atomic reservations and order references, Stripe Checkout, mock carrier, real-time dashboard, white-label buyer app._

## Key decisions and trade-offs

_To be completed as the milestones land; every record is in [`docs/adr/`](docs/adr/README.md)._

| Decision                                                                                                                                      | Trade-off                                                                                                                                |
| --------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| One `cp .env.example .env` before `docker compose up`                                                                                         | One extra command, so that secrets never have a default in a versioned file                                                              |
| One Python image for the API, the worker and migrate                                                                                          | A slightly larger image per process, but migrations always ship with the code that uses them                                             |
| A deterministic parser first, an LLM only for its ambiguities ([ADR 0006](docs/adr/0006-deterministic-parser-then-llm-fallback.md))           | A phrasing the lexicon misses is rejected or costs a model call until it is added; clear orders never wait for, pay for or trust a model |
| Protocol ports with in-memory fakes, hand-written state machines ([ADR 0002](docs/adr/0002-layered-architecture-ports-and-state-machines.md)) | The fakes hold rules the SQL adapters must match, so both are tested on the same scenarios; every rule is tested without Docker          |

## Out of scope

_To be completed: real Facebook, Instagram and TikTok integrations, PayPal, Viva Wallet and real carriers, buyer accounts, refunds, iOS builds, real deployment._

## Roadmap

_To be completed._

## Development

Prerequisites, `make` targets with their raw commands for PowerShell, commit conventions and port overrides: see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
