# LiveCart

LiveCart turns live-stream comments such as "SOLD A12 x2" into purchase intents, reserves the stock atomically and issues a short order reference.
Buyers pay with Stripe Checkout (test mode) in a white-label Expo app, while sellers follow intents, orders and stock in real time on a React dashboard.
A FastAPI API, an asyncio worker and a Redis Streams queue absorb comment spikes without ever overselling.

> **Personal demo project, fictional brand, not affiliated with any company.**

## Status

Milestone 1 of 9: the monorepo, its tooling, CI and the Docker stack. The API only answers `/health` and `/ready`, the worker only runs its heartbeat, and the dashboard is a shell page that checks the API. The features described above arrive milestone by milestone; this README grows with them.

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
| `postgres` | PostgreSQL 16, databases `livecart` and `livecart_test` | `127.0.0.1:55432`                         |
| `redis`    | Redis 8 with a password, append-only file               | `127.0.0.1:56379`                         |

## Demo

_To be completed: `docs/assets/demo.gif`, recorded once the dashboard and the buyer app exist._

## Architecture

_To be completed: the Mermaid diagram of the comment path, from the signed webhook to the SSE event, and the Python layers (domain, application, infrastructure, entry points)._

## Features

_To be completed as each milestone lands: comment ingestion and intent parsing, atomic reservations and order references, Stripe Checkout, mock carrier, real-time dashboard, white-label buyer app._

## Key decisions and trade-offs

_To be completed, with a link to each ADR in `docs/adr/`._

| Decision                                              | Trade-off                                                                                    |
| ----------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| One `cp .env.example .env` before `docker compose up` | One extra command, so that secrets never have a default in a versioned file                  |
| One Python image for the API, the worker and migrate  | A slightly larger image per process, but migrations always ship with the code that uses them |

## Out of scope

_To be completed: real Facebook, Instagram and TikTok integrations, PayPal, Viva Wallet and real carriers, buyer accounts, refunds, iOS builds, real deployment._

## Roadmap

_To be completed._

## Development

Prerequisites, `make` targets with their raw commands for PowerShell, commit conventions and port overrides: see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
