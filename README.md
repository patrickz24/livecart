# LiveCart

LiveCart turns live-stream comments such as "SOLD A12 x2" into purchase intents, reserves the stock atomically and issues a short order reference.
Buyers pay with Stripe Checkout (test mode) in a white-label Expo app, while sellers follow intents, orders and stock in real time on a React dashboard.
A FastAPI API, an asyncio worker and a Redis Streams queue absorb comment spikes without ever overselling.

> **Personal demo project, fictional brand, not affiliated with any company.**

## Status

Milestone 8 of 9: the white-label buyer app. It stands on the seller dashboard of milestone 7, the mock carrier of milestone 6, the payment of milestone 5, the comment ingestion, the worker, the real-time events and the optional LLM fallback of milestone 4, the HTTP API, the atomic stock reservation and the order references of milestone 3, the domain and parser of milestone 2, and the monorepo, CI and Docker stack of milestone 1.

- **Brands** (8): one white-label build per folder of `apps/mobile/brands/`: `brand.json` (name, slug, scheme, bundle identifier, Android package, the seller of its storefront, three colors, four images) and its images. `app.config.ts` builds the brand `APP_BRAND` names, after checking it against a strict zod schema: an unknown brand, a field missing, unknown or malformed, or an image missing fails `expo start`, `expo export` and EAS alike, with every reason. Two brands: LiveCart Demo (scheme `livecart`, seller `demo-seller`) and Pomelune, an invented name (scheme `samplelive`, seller `sample-shop`); their identifiers, `com.example.livecart.<brand>`, are placeholders to replace before any store build. `eas.json` has one profile per brand (Node 24.21.0, pnpm 12.6.0, Corepack off), `packages/design-tokens` the shared tokens a brand recolors, and a build bundles its own brand's logo and no other's ([ADR 0013](docs/adr/0013-white-label-brand-config-eas-profiles-design-tokens.md)).
- **Live** (8): the Live tab shows the storefront of the brand's seller, through the generated client: the live session, each product's price and the stock left; a 404 shows "Store not found", since the brand then names no seller of that API.
- **My orders** (8): each order's token lives in `expo-secure-store` only, one entry per order and an index of 50 references at most, the oldest forgotten for a 51st, and leaves it only for the `Authorization` header of its own order's requests. Orders come in through "Add order" (the reference and the token that `livecart-simulate --text` prints, or the whole link), checked with the API before anything is stored, or through a link, `<scheme>://link/LC-XXXXXX#t=<token>`, whose fragment no server ever receives and which leaves the navigation once the token is stored.
- **Order and tracking** (8): an order's status, product, total and address, then, once paid, its parcel's carrier, tracking number and step, all through the token routes, limited per token rather than per address.
- **Payment** (8): the address typed in the app, then the checkout opened in an auth session (`openAuthSessionAsync`, ephemeral on iOS, a Custom Tab on Android). The bridge page's button returns to `<scheme>://payment-return` (Expo Go: `exp://...`) with the outcome and the reference only; the app then reads the order's status every 3 s, then less often, for 60 s at most, until it is paid or expired.
- **Quality** (8): 75 Jest tests (jest-expo on Jest 29, React Native Testing Library 14, expo-router's testing library over the real routes, on a fake API and an in-memory SecureStore), among them a snapshot per brand, the refusals of the brand config, "Store not found", the store with 50 orders, the `#t=` fragment and the payment return. CI's new `mobile` job runs expo-doctor and an Android `expo export` of each brand; the JS packages' suites now run one after another.

**The end criterion of milestone 8 is not met yet: it needs a phone and an Expo account.** The plan asks that the app runs in Expo Go on a physical Android phone; that the phone, on the LAN, reaches `/api/v1` and the bridge page through nginx; that the buyer adds an order from the simulator's output and pays it; and that the payment return is checked, and dated, on the development build, iOS being "configured, not built".

- Run, on 2026-10-02, on the development PC, by the coding agent: TypeScript, ESLint, Prettier and the 75 Jest tests of the app; `make lint`; expo-doctor (21 checks out of 21) and `expo export --platform android` for both brands, the Pomelune bundle holding its own logo only; `expo config` refusing an unknown brand; the API's return-URL policy taking `livecart://payment-return`, `samplelive://payment-return` and Expo Go's `exp://192.168.1.20:8081/--/payment-return` in development; the running stack's storefronts read with curl (`demo-seller` live, `sample-shop` without a live, an unknown slug 404).
- Not run: the app on any phone or emulator, Expo Go, and EAS (`eas init`, the Android development build), hence the deep links and the payment return on a device; the iOS build, configured only.
- To run it, as [CONTRIBUTING.md](CONTRIBUTING.md#buyer-app) says: the PC's LAN address in `ALLOWED_HOSTS`, `PUBLIC_API_BASE_URL` and `apps/mobile/.env`'s `EXPO_PUBLIC_API_URL`, `npx expo start` in `apps/mobile` and Expo Go on the phone, then the demo path through "Add order"; then, with an Expo account, `eas init` and `npx eas-cli@24.8.0 build --profile demo --platform android` for the development build, a buyer link and a payment return on it.

From milestone 7, the seller dashboard the orders come from:

- **Sign-in** (7): `/login`, then every page behind `RequireAuth`. The access token stays in the tab's memory, never in a storage a script could read; a reload trades the HttpOnly `lc_refresh` cookie for a new one. One refresh at a time: the callers of a tab share it, and tabs take turns under a Web Lock (`navigator.locks`), so no two tabs ever send the same refresh token. A request answered 401 is retried once after that refresh; an unreachable server never signs the seller out ([ADR 0017](docs/adr/0017-dashboard-auth-and-event-stream-client.md)).
- **Sessions and products** (7): `/sessions`, and `/sessions/new` and `/sessions/:id/edit`, one react-hook-form form for a session and its products, checked by a zod schema typed against the generated request bodies, with the API's 422 shown on the field it names. Stock moves by restock deltas through the idempotent endpoint, under an `Idempotency-Key` kept for every retry of the same change. "Start live" and "End live" follow the session's machine, ending only after a confirmation, and a 409 shows the status the server holds.
- **Live view** (7): `/sessions/:id/live`, from the session's SSE stream read with `fetch` and eventsource-parser: stock per product, orders with their reservation countdown, status, `paid_after_expiry` badge and shipment, intents with an "AI" badge for the LLM's, and rejected comments by reason. A pure reducer, `applyEvent`, applies each event by upsert, so a duplicate changes nothing; the stream resumes with `Last-Event-ID`, backs off from 1 to 30 s, refreshes its token on a 401, reloads the snapshot after a gap, drops a connection silent for 45 s, and shows a banner once it has been down for 5 s. `/orders/:reference` shows one order. nginx relays the stream unbuffered.
- **Look and feel** (7): English UI, plain Tailwind CSS 4 with a theme of a few tokens in `app.css`, no component library; labelled fields with announced errors, a visible focus ring, the focus on each new page's heading, a skip link. The built SPA keeps its CSP (`default-src 'self'`): one stylesheet and one script of its own origin, nothing inline, no `eval`.

**The end criterion of milestone 7 is met, with what a browser in the agent's session could show.** The plan asks that events arrive live on <http://localhost:8080>, and that the stream resumes after `docker compose restart api`. On 2026-10-02, after `docker compose up -d --build --wait`, in a browser pane driven by the coding agent: the demo seller signed in, the friday-drop live view showed the simulator's orders and stock changing, and `docker compose restart api` cut its stream for about 4 s, after which new orders kept arriving; a 30 s stop of the API showed the outage banner 5 s after the cut and live updates again once the API was back. The same stream client, run under Node through nginx, logged its reconnection with `Last-Event-ID` and no new snapshot. Not run: a session created through the form against the stack (the form's requests and the API's 422 and 409 bodies were checked separately, with tests and curl), two real tabs refreshing at the same moment (the lock is tested with a fake one), and any browser other than the agent's Chromium pane.

From milestone 6, the mock carrier whose parcels the dashboard follows:

- **Carrier port** (6): one `CarrierProvider` port of three calls (create a shipment, idempotent per order; read its tracking; cancel it). The mock is the default and needs no account: it keeps no state, and derives each shipment's carrier ref and `MOCK-` tracking number from the order's id under `MOCK_CARRIER_SECRET`, and its status from the clock, one step every `MOCK_CARRIER_STEP_SECONDS` (120 s), so a parcel is delivered six minutes after its payment. DPD, Chronopost and DHL are documented stubs that refuse to start ([ADR 0002](docs/adr/0002-layered-architecture-ports-and-state-machines.md)).
- **Shipments** (6): once a worker's tick has paid an order, the order's shipment is created at the carrier out of any transaction, within 2 s per tick so that a slow carrier never stalls the payments, and recorded once per order whatever the workers (`uq_shipments_order_id`). Every `SHIPMENT_SYNC_INTERVAL_S` (60 s), the shipments loop ships the paid orders a crash, an outage or a slow carrier left without one, then asks the carrier about the shipments in flight, each leased to one worker, and persists each move by compare-and-set.
- **Tracking** (6): `GET /api/v1/orders/{reference}/tracking`, with the order's token, answers the carrier, the tracking number, the status and the time of the carrier's latest event, read from PostgreSQL; the reference alone never shows the tracking number. The seller's SSE stream gets one `shipment.updated` at the creation and one per move, and its snapshot carries each order's shipment as it stands. [CONTRIBUTING.md](CONTRIBUTING.md#shipments) follows one parcel.

**The end criterion of milestone 6 is met.** The plan asks that a paid order's shipment progress, visible through the API and in SSE. On 2026-10-02, on the compose stack, an order of the simulator paid on the mock's page had its shipment `created` on its tracking route within seconds and `in_transit` two minutes later, and the seller's SSE stream showed its `shipment.updated` events to `out_for_delivery`, then `delivered`, six minutes after the payment.

From milestone 5, the payment that the shipments follow:

- **Payment provider** (5): one `PaymentProvider` port of four calls (create a hosted session, expire it, read it back, verify a webhook). The mock is the default and needs no account and no key: its hosted page is served by the API in development, and its Pay button posts a signed webhook as a provider would. Stripe Checkout runs in test mode with your own test keys; PayPal and Viva are documented stubs that refuse to start ([ADR 0008](docs/adr/0008-payments-provider-port-hosted-checkout-expire-first.md)).
- **Checkout** (5): with the order's token, the buyer records an address, then `POST /api/v1/orders/{reference}/checkout` extends the reservation to at least now + `PAYMENT_GRACE_SECONDS` (600 s) and only then creates the provider's session, under an idempotency key per attempt, 3 attempts at most. The provider sends the buyer back to a bridge page that reopens the app with the reference, never the token.
- **Webhooks** (5): the signature is checked before anything is read, the event reduced to an allow-list without the customer's details and recorded once in PostgreSQL, then 200, with Redis up or down. Every worker applies that registry every 2 s, each event once whatever its deliveries, the amount checked against the order's.
- **Expire-first** (5): the sweep never releases the stock of an order whose payment is in flight before the provider has expired the session. One worker at a time asks about each order (a 30 s lease), a session the buyer paid first is settled as paid, and an attempt left without its session gets the same one again under its key ([ADR 0005](docs/adr/0005-anti-oversell-conditional-update.md)). [CONTRIBUTING.md](CONTRIBUTING.md#payments) runs one mock payment end to end, and one expiry.

**The end criterion of milestone 5 is half met.** The plan asks for the mock's flow without any key, and for a payment with Stripe's test card 4242 that reaches `paid`, visible in SSE.

- Run: the mock's flow on the compose stack. On 2026-10-01, an order of the simulator, its address, its checkout and Pay on the mock's page gave `paid`, and the seller's SSE stream showed its `order.updated` with the status `paid`; a checkout left unpaid was expired at the mock, then its stock released.
- Not run: the 4242 payment. There is no Stripe test key yet, so the Stripe adapter is verified only against `FakeStripeApi`, an in-memory stand-in of Stripe's API (no test calls Stripe), and the `stripe` compose profile, the Stripe CLI forwarding a test account's events through nginx, has never met a real account.
- To run it, with the maintainer's own Stripe test keys in `.env`, never in a versioned file, as the [Payments section of CONTRIBUTING.md](CONTRIBUTING.md#payments) says: set `PAYMENT_PROVIDER=stripe` and `STRIPE_SECRET_KEY=sk_test_...`; set the secret that `docker compose --profile stripe run --rm stripe-cli listen --print-secret` prints as `STRIPE_WEBHOOK_SECRET`; `docker compose --profile stripe up -d --wait`; open the session's SSE stream, run the checkout and pay on Stripe's page with `4242 4242 4242 4242`, any future date and any CVC.

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
- **Buyer API**: the storefront of a seller by its slug, and an order by its reference, in two tiers: the reference alone shows the status, and the order's token its details, then its address and checkout (milestone 5) and its tracking (milestone 6).
- **Perimeter**: rate limits in Redis (per address, per seller, per account and per token), security headers, trusted hosts, a 1 MiB body limit and uvicorn trusting nginx alone for the client address ([ADR 0011](docs/adr/0011-perimeter-and-hardening.md)).
- **Demo data and accounts**: the one-shot `seed` service creates the demo sellers and sessions; `livecart-create-seller` opens other accounts ([CONTRIBUTING.md](CONTRIBUTING.md#demo-data)).

The features described above arrive milestone by milestone; this README grows with them.

## Quickstart

With Docker (Docker Desktop with the WSL 2 backend on Windows):

```bash
git clone <repository-url> livecart && cd livecart
cp .env.example .env
docker compose up --build
```

Then open the dashboard on <http://localhost:8080> and sign in as the demo seller, `demo-seller@livecart.test`, with the `SEED_DEMO_PASSWORD` of `.env`: the friday-drop session is live, and the simulator's comments show up in its live view. The interactive API documentation, in development only, is on <http://127.0.0.1:8000/docs>. The demo storefront is <http://localhost:8080/api/v1/storefronts/demo-seller>. `docker compose down --volumes` (or `make reset`) removes the stack and its data.

The stack needs no third-party key: payments go through the mock provider and shipments through the mock carrier, and to try the LLM fallback, set `ANTHROPIC_API_KEY` in `.env` (the always-on simulator then spends the daily budget; `docker compose stop simulator` pauses it). Stripe in test mode needs your own test keys and the `stripe` compose profile ([CONTRIBUTING.md](CONTRIBUTING.md#payments)).

The copy of `.env.example` is the one step before `docker compose up`: `compose.yaml` gives no default to any secret and refuses to start without them, so that no password ever has a default value in a versioned file. The `change-me-*` values are development placeholders, refused when `APP_ENV=production`.

| Service      | Role                                                                       | Published on                              |
| ------------ | -------------------------------------------------------------------------- | ----------------------------------------- |
| `web`        | nginx: the dashboard, and the API proxied on `/api/`                       | `8080` (every interface, for a LAN phone) |
| `api`        | FastAPI behind uvicorn                                                     | `127.0.0.1:8000`                          |
| `worker`     | asyncio worker: comments, reclaim, expiry sweep, payment events, shipments | not published                             |
| `simulator`  | comment simulator on the demo session                                      | not published                             |
| `migrate`    | one-shot Alembic upgrade, as the schema owner                              | not published                             |
| `seed`       | one-shot demo data, created only where missing                             | not published                             |
| `postgres`   | PostgreSQL 16, databases `livecart` and `livecart_test`                    | `127.0.0.1:55432`                         |
| `redis`      | Redis 8 with a password, append-only file                                  | `127.0.0.1:56379`                         |
| `stripe-cli` | `stripe` profile only: Stripe's events forwarded to the API                | not published                             |

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
| A stateless mock carrier, and tracking by polling with a lease per shipment ([ADR 0002](docs/adr/0002-layered-architecture-ports-and-state-machines.md))                                                                                  | A mock parcel moves on a fixed schedule and the mock forgets a cancellation; a move reaches the seller up to a minute late, and each shipment is asked about once a minute whatever the workers        |
| The dashboard's access token in memory only, one refresh at a time across tabs under a Web Lock, the event stream read with `fetch` ([ADR 0017](docs/adr/0017-dashboard-auth-and-event-stream-client.md))                                 | Each reload and new tab costs a refresh; on plain HTTP to a LAN address there are no Web Locks, and tabs fall back on the API's 30 s grace                                                             |
| One folder per brand (`brand.json` and its images) checked by `app.config.ts`, one EAS profile per brand, the built brand's logo alone in its bundle ([ADR 0013](docs/adr/0013-white-label-brand-config-eas-profiles-design-tokens.md))   | A broken brand never builds; every brand is an Expo project and a store listing of its own, and the free plan's builds are shared by all                                                               |
| Metrics on an internal port only ([ADR 0014](docs/adr/0014-observability-structlog-ids-and-internal-metrics.md))                                                                                                                          | Nothing outside the container network can read them, nor anyone until a scraper runs beside the containers                                                                                             |

## Out of scope

_To be completed: real Facebook, Instagram and TikTok integrations, PayPal, Viva Wallet and real carriers, buyer accounts, refunds, iOS builds, real deployment._

## Roadmap

_To be completed._

## Development

Prerequisites, `make` targets with their raw commands for PowerShell, commit conventions and port overrides: see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
