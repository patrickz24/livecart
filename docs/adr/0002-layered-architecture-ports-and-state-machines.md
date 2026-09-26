# ADR 0002: Layered architecture, Protocol ports and hand-written state machines

## Status

Accepted, 2026-09-26. Milestone 2 put the domain, the ports and their fakes in place; the use cases and the SQL, Redis and provider adapters that satisfy the ports arrive with milestones 3 to 6.

## Context

The requirements ask for a layered Python backend: a domain of pure business logic without a framework, an application layer of use cases, an infrastructure layer (database, Redis, Stripe, LLM, carriers) and the API routes, with a domain that is tested without a database. Every later milestone adds a rule that must be tested hard (atomic reservations, idempotent webhooks, expiry, the LLM fallback), and milestone 2 ends when the unit tests pass without Docker with at least 80 % branch coverage of the domain and the application. The statuses of orders, payments, shipments, live sessions and comments are changed by several workers and webhooks at once, and a delivery can be replayed, so every status change must be explicit and safe to repeat.

## Decision

### Four layers, imports inward only

- **Domain** (`livecart_core.domain`): the value objects (`Money`, `ProductCode`, `Variant`, `Quantity`, `OrderReference`), the session `Catalog`, the closed outcome lists, the state machines and the purchase-intent parser. Standard library only: no pydantic, no settings, no clock, no I/O. What varies arrives as an argument: the parser takes `max_quantity`, `OrderReference` takes its random source.
- **Application** (`livecart_core.application`): the ports (`ports.py`) and the data that crosses them (`dto.py`); it may import the domain and pydantic. The use cases join it in the milestones that implement them.
- **Infrastructure** (`livecart_core.infrastructure`, today the SQLAlchemy engine and the Redis client factories) and the **entry points** (`livecart_api`, `livecart_worker`, the CLIs) may import anything; they build the adapters and inject them. The worker does not depend on FastAPI.
- `tests/architecture/test_layering.py` reads the source of every module of the two inner layers with `ast` and fails on any import outside the allowed set, including an import inside a function or under `TYPE_CHECKING`.

### Ports are `typing.Protocol`, and each one has a fake

- A port is a `Protocol`: an adapter satisfies it by its shape, with no base class to inherit, and `mypy --strict` checks the fit wherever an adapter is passed as a port. The ports are `Clock`, `PlatformAdapter`, `CommentQueue`, `IntentFallback`, `EventPublisher`, `BuyerNotifier`, `PaymentProvider`, `CarrierProvider` and the repositories `CommentInbox`, `LiveSessionRepository`, `ProductStockRepository`, `OrderRepository`, `PaymentRepository`, `ShipmentRepository` and `WebhookEventRegistry`.
- The docstring of `ports.py` states the rules every adapter keeps: a repository method is one atomic unit (one transaction in SQL), so a use case never holds a transaction across another port and a network call always falls between two units; a status change is a compare-and-set that returns `APPLIED` or `NOOP` or raises `InvalidTransition`; a missing row raises `RecordNotFound`, a failure worth retrying `ServiceUnavailable`; every `now` comes from the `Clock` port.
- The data that crosses the ports comes in two kinds: pydantic models for the payloads validated at a process or network boundary (`IncomingComment`, `CommentBatch`, the queue envelope `QueuedComment`, `SessionEvent`, `ShippingAddress`, `PaymentEvent`), frozen, with unknown fields refused and every string bounded; and frozen dataclasses for the records the adapters return and the requests a use case sends, holding domain objects.
- `NullIntentFallback` lives in the application layer rather than among the fakes, because the worker runs it in production whenever no LLM is configured.
- `livecart_core.testing.fakes` holds one fake per port. They are deterministic (sequential UUIDs, `FakeClock`, an injectable random source for references), failures are scripted with `fail_next(n)`, and the repository fakes share one `InMemoryDatabase`. None of their methods awaits, so each one is atomic under asyncio like the transaction it stands for; `tests/unit/fakes/test_atomicity.py` checks that on the source. The tests under `tests/unit/fakes` pin what each fake promises.
- Every port of the plan's journeys arrives now, the comment ingestion included: `IncomingComment`, `CommentBatch`, the queue envelope (`QueuedComment`, `QueueDelivery`) and `PlatformAdapter`. The first commit the plan lists for milestone 4a ("add comment envelope and platform adapter port") is therefore absorbed into milestone 2, and milestone 4a starts with the signed webhook. The milestone that implements a port may refine it, together with its fake, in the same commit.
- The fakes already enforce the business rules their adapters will: a reservation checks the stock, turns a second pending order of the same buyer and product into an `already_reserved` intent, and draws an order reference at most three times before `ServiceUnavailable`; opening a payment attempt extends the reservation to `max(reserved_until, now + grace)`; the expiry sweep releases the orders past their deadline that have no pending payment; a payment after expiry becomes `paid_after_expiry`.

### State machines written by hand

- Each status is a `StrEnum` whose values are the exact strings of the database `CHECK` constraints, the metric labels and the dashboard (`livecart_core.domain.state_machines`). Each machine is a frozen set of the allowed (current, target) pairs, and `transition(table, current, target)` is the one rule that reads them: `APPLIED`, `NOOP` when the target is already reached (a replayed webhook), otherwise `InvalidTransition`. A state that never appears as a source is terminal.
- Five machines: the order (`pending_payment` to `paid` or `expired`, `expired` to `paid_after_expiry`), the payment attempt (`pending` to `succeeded`, `failed` or `expired`), the shipment (`created`, `in_transit`, `out_for_delivery`, `delivered`, or `created` to `cancelled`), the live session (`draft`, `live`, `ended`) and the comment inbox (`received` to `processed`, `rejected` or `dead_lettered`). The reservation has no machine of its own: the order carries it.
- The persistence layer (milestone 3) applies a transition as a compare-and-set `UPDATE ... WHERE status = :current` and maps its row count onto the same outcomes, so that two workers cannot both apply one change.
- The tests enumerate every ordered pair of states of every machine: each allowed pair applies, each other pair raises, each state reached again is a `NOOP`.

## Consequences

- The domain and the application are tested without Docker: `uv run --locked pytest -m "not integration" --cov` runs the unit tests and the branch-coverage gate (80 %, `[tool.coverage]` in `pyproject.toml`) over both layers.
- A framework or a setting imported into the domain fails the architecture test, not only a review.
- A fake and its adapter can drift, since the fakes hold rules the SQL adapters must also enforce. Milestones 3, 5 and 6 test their SQL adapters against the same scenarios as `tests/unit/fakes`, so that a fake and its adapter cannot drift silently.
- A `Protocol` is checked by mypy, not at run time: an adapter of the wrong shape is a `make lint` failure, which CI runs on every pull request.
- Two kinds of transfer objects to know: pydantic where data is validated on arrival, dataclasses inside the process.
- A new transition is one pair in a frozen set, and the exhaustive tests cover it at once; there is no library to learn, and the tables read like the plan's.

## Alternatives considered

- **Abstract base classes for the ports**: an adapter would have to import and inherit from the application layer; a `Protocol` keeps the adapters free of that coupling and mypy checks the fit structurally.
- **`unittest.mock` instead of fakes**: a mock checks the calls a test scripted, not behaviour, so a use case tested against it holds only against that script; a fake behaves like its adapter and serves every milestone.
- **A state machine library** (transitions, python-statemachine): more API than five small tables need, and callbacks would hide effects that must stay in the SQL compare-and-set; plain tables are data that the tests enumerate.
- **Pydantic in the domain**: it would tie the business rules to a validation library and its versions; the value objects are dataclasses that check their invariants in `__post_init__`.
- **import-linter** for the layering rule: one more tool and configuration for two rules; the test is short, reads the source, and runs with the unit tests.
