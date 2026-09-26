# ADR 0005: Anti-oversell: one conditional UPDATE, one pending order per buyer and product

## Status

Accepted, 2026-09-26. Milestone 3 implemented the reservation and its proof under concurrency, milestone 4a the expiry of unpaid reservations for the orders without a payment in flight. The orders with one wait for milestone 5, which expires the provider's session first.

## Context

During a live, hundreds of buyers comment on the same product within seconds, and several workers reserve for them at once. Selling a unit twice is the one failure a seller cannot accept, and a comment may also be delivered more than once. Each reservation must decide on the stock that is really left, and a burst on one product must neither fail nor retry its way through. A buyer who repeats "je prends A12" must not hold the stock several times over.

## Decision

### Counters the database guards

- A product row holds `stock_total`, `reserved` and `sold`, and `available`, a column PostgreSQL generates and stores as `stock_total - reserved - sold`. `ck_products_stock` requires `reserved >= 0`, `sold >= 0` and `reserved + sold <= stock_total`: a code path that got the arithmetic wrong fails its statement rather than oversell.
- Reserved and sold stay apart, so that the release of an unpaid reservation and a sale each move one counter, and the reservation compares one column.

### Unit 3: one transaction, one conditional update

`SqlOrderRepository.reserve`, called by the `ReserveStock` use case, runs in one READ COMMITTED transaction, with SQLAlchemy Core constructs only:

1. `UPDATE comments SET status = 'processed' ... WHERE id = :id AND status = 'received'`: no row means another delivery settled the comment, and the unit writes nothing;
2. a savepoint, then the reservation itself:

   ```sql
   UPDATE products SET reserved = reserved + :q
   FROM live_sessions
   WHERE products.id = :id AND products.session_id = live_sessions.id
     AND live_sessions.status = 'live' AND products.available >= :q
   RETURNING ...
   ```

   When another transaction holds the product's row, this statement waits for it, then PostgreSQL evaluates its `WHERE` again on the row version that transaction committed. The last unit goes to exactly one of them; the other gets no row, and its intent is recorded `out_of_stock`, as it is when the session is no longer live. No read races the write, so nothing is oversold and nothing is retried;

3. the intent is recorded `reserved`, then the pending order is inserted. The partial unique index `uq_orders_pending_buyer_product`, on `(session_id, product_id, buyer_platform_user_id) WHERE status = 'pending_payment'`, refuses a second pending order of the same buyer for the product: the unit rolls back to the savepoint, which undoes the reservation and the intent, and records the intent as `already_reserved`. Short stock is checked first: when too little is left, the intent is `out_of_stock`, even for a buyer who already holds a pending order;
4. the order's reference is drawn under a savepoint nested in the reservation's, so that a collision undoes the insert only (ADR 0007).

The comment's row is locked before the product's in every unit, so two units cannot deadlock. The reservation lasts until `now + RESERVATION_TTL_SECONDS` (900 by default, from 60 to 86400, validated at startup), `now` coming from the `Clock` port. A lock that waits more than `lock_timeout` (2 s), a deadlock or a lost connection rolls the whole unit back as `ServiceUnavailable`: the comment stays `received`, for its redelivery.

### Expiry of unpaid reservations

Every worker sweeps every 5 s (`ExpireReservations`, `SqlOrderRepository.expire_due`). A batch is one transaction:

1. `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 100` of the orders `pending_payment` whose `reserved_until` is before `now` (the `Clock` port's) and that have no pending payment, lowest id first, by the partial index `ix_orders_pending_reserved_until`. Another sweep's rows are skipped, not waited for: sweeps running at once in several workers take disjoint batches;
2. one aggregated `UPDATE products SET reserved = reserved - :q` per product, in ascending product id order, so that two sweeps lock products in the same order;
3. `UPDATE orders SET status = 'expired', expired_at = :now` with the guard `status = 'pending_payment' AND reserved_until < :now`.

The order rows are only locked (`FOR UPDATE`) until the products are released, and updated last. A reservation of the same buyer and product that runs meanwhile holds the product's row and inserts its order: against a row that is only locked, `uq_orders_pending_buyer_product` fails at once and the intent is `already_reserved`, whereas a row this transaction had already updated would make that insert wait for the sweep, while the sweep waits for the product: a deadlock. A full batch means more may be due: the sweep goes on until a batch comes back short or the tick's 5 s have passed. After each batch commits, `order.updated` and `stock.updated` are published, best effort.

`tests/contract/test_reservation_expiry.py` runs the rules on the fake and on PostgreSQL (the payment in flight left alone, a reservation ending exactly at `now` not due yet, two sweeps at once expiring each order exactly once), and `tests/integration/test_reservation_expiry_postgres.py` shortens `RESERVATION_TTL_SECONDS` to 2 s with an injected clock: the order expires, the unit is for sale again, and two concurrent sweeps through the worker's pool release 250 orders exactly once (a second release would break `ck_products_stock`).

The seller's restock is the same kind of statement: one `UPDATE ... SET stock_total = stock_total + :delta` whose `WHERE` requires the new total to stay between `reserved + sold` and the maximum, so a restock waiting on a reservation decides on the counters it committed, and answers 409 otherwise.

### The proof

- `tests/integration/test_reservation_concurrency.py`: a thousand `ReserveStock` calls on a product stocked at 200, released together by an `asyncio.Barrier`, through the worker's pool of 20 connections, three rounds. Each round asserts invariants only: the counters within `ck_products_stock`, exactly 200 reservations, one pending order with a distinct reference per reservation, every other intent `out_of_stock`, every comment processed. A probe of the pool fails the round unless all twenty connections were checked out at once, so the contention is measured, not assumed.
- Twenty concurrent comments of one buyer on one product give one order and nineteen `already_reserved` intents, with the rolled-back reservations leaving no trace on the stock.
- The scenarios of `tests/contract/test_order_reservations.py` run on the in-memory fake and on PostgreSQL alike (ADR 0002).

## Consequences

- Reservations of one product serialize on its row lock, each for the length of one short transaction; different products reserve in parallel. The pool bounds the concurrency, and `lock_timeout` bounds each wait.
- A burst never fails for a reason another attempt would fix: the loser of the last unit gets a final answer, `out_of_stock`, the first time.
- A buyer holds at most one pending order per product, of at most `MAX_QUANTITY_PER_COMMENT` (10) units: a troll can still hold that much stock per account and product until the reservation expires.
- An unpaid reservation holds its stock for `RESERVATION_TTL_SECONDS` plus at most one sweep interval (5 s). The orders with a payment in flight are not released before milestone 5, which asks the provider to expire its session first.
- Ending a session never waits for the reservations, nor they for it: the reservation reads the session's row without locking it, and the `FOR KEY SHARE` of its foreign keys does not conflict with the `FOR NO KEY UPDATE` of the transition. A reservation whose statement started before an `end` committed still reads the session as live, and may open a pending order in a session that is ended by the time it commits (checked with two connections on PostgreSQL 16). The stock stays exact, since the product's row is what serializes the reservations; that order expires like any other, with the sweep. A hard cut would need `end` to lock the session's product rows (`FOR NO KEY UPDATE`) before it changes the status.

## Alternatives considered

- **`SELECT ... FOR UPDATE`, then an `UPDATE`**: the same lock on the same row, held across a second round trip, with the rule checked in Python between the two statements instead of by the statement that writes.
- **SERIALIZABLE (or REPEATABLE READ)**: the losers of a race get a serialization failure and must retry; under a burst on one product the retries multiply, where READ COMMITTED's second evaluation gives each one its final answer at once.
- **A Lua script decrementing the stock in Redis**: the stock would live in two places, and the order is written in PostgreSQL anyway; a crash between the two would lose or leak units, which a reconciliation would have to find.
- **An optimistic version column**: every conflict is a retry.
- **One counter per product without `reserved` and `sold`**: releasing an unpaid reservation and recording a sale could no longer be told apart.
