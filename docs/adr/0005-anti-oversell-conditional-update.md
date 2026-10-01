# ADR 0005: Anti-oversell: one conditional UPDATE, one pending order per buyer and product

## Status

Accepted, 2026-09-26, completed 2026-10-01. Milestone 3 implemented the reservation and its proof under concurrency, milestone 4a the expiry of unpaid reservations for the orders without a payment in flight, milestone 5 the expiry of those with one, which asks the payment provider to expire its session first ([ADR 0008](0008-payments-provider-port-hosted-checkout-expire-first.md)).

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

### Expiry of the reservations with a payment in flight

An order whose buyer opened the provider's hosted page has a pending payment attempt, and its reservation was extended to at least `now + PAYMENT_GRACE_SECONDS` (600 by default) when the attempt opened. The first step above never releases it: the buyer may be paying. Once that reservation has ended too, the same tick of every worker runs the plan's second and third steps (`ExpireCheckouts`, `SqlOrderRepository.lease_due_checkouts` and `release_checkout`):

1. a lease, one statement:

   ```sql
   UPDATE orders SET expiry_lease_until = :now + 30 s
   WHERE id IN (SELECT candidate.id FROM orders AS candidate
                WHERE candidate.status = 'pending_payment' AND candidate.reserved_until < :now
                  AND EXISTS (SELECT 1 FROM payments
                              WHERE order_id = candidate.id AND status = 'pending')
                  AND (candidate.expiry_lease_until IS NULL
                       OR candidate.expiry_lease_until < :now)
                ORDER BY candidate.id LIMIT 20 FOR UPDATE SKIP LOCKED)
   RETURNING ...
   ```

   then the pending attempt of each, read under the rows' locks. It commits before any call: until the lease ends, no other sweep takes the order, so one worker alone asks the provider about it;

2. for each leased order, at most 4 calls to the provider at once per worker, no connection held during a call and one at most otherwise, all of a claim's work ending before its lease does: an attempt left without a session (a crash between the two steps of the checkout) gets it again under its idempotency key, then the session is expired;
3. the provider expired it, or reads it back expired: one unit locks the order's row, expires the attempt by compare-and-set, then, if the order still waits for its payment, is due and has no pending attempt left, releases its units (`UPDATE products SET reserved = reserved - :q`) and expires the order last, as the first step does. The provider refused because the buyer paid first: the session's payment is applied through the registry of payment events, as the provider's own webhook would be (`reconciled:<session>`), and the order is paid, its units sold, never released. Anything else waits for the lease to end.

Every release, whichever step makes it, is guarded by `status = 'pending_payment'` under the order's row lock, so an order's units go back to the stock exactly once. `tests/contract/test_checkout_expiry.py` runs the lease and the release on the fake and on PostgreSQL (two sweeps leasing disjoint orders, a release done once, after the provider's own event, never of a paid order); `tests/unit/application/test_expire_checkouts.py` the use case, with each interleaving of a checkout; `services/worker/tests/integration/test_checkout_expiry_postgres.py` the whole of it on PostgreSQL against the mock on Redis, two sweeps making exactly one expiry call for each of 20 orders, and no connection checked out during any call.

The seller's restock is the same kind of statement: one `UPDATE ... SET stock_total = stock_total + :delta` whose `WHERE` requires the new total to stay between `reserved + sold` and the maximum, so a restock waiting on a reservation decides on the counters it committed, and answers 409 otherwise.

### The proof

- `tests/integration/test_reservation_concurrency.py`: a thousand `ReserveStock` calls on a product stocked at 200, released together by an `asyncio.Barrier`, through the worker's pool of 20 connections, three rounds. Each round asserts invariants only: the counters within `ck_products_stock`, exactly 200 reservations, one pending order with a distinct reference per reservation, every other intent `out_of_stock`, every comment processed. A probe of the pool fails the round unless all twenty connections were checked out at once, so the contention is measured, not assumed.
- Twenty concurrent comments of one buyer on one product give one order and nineteen `already_reserved` intents, with the rolled-back reservations leaving no trace on the stock.
- The scenarios of `tests/contract/test_order_reservations.py` run on the in-memory fake and on PostgreSQL alike (ADR 0002).

## Consequences

- Reservations of one product serialize on its row lock, each for the length of one short transaction; different products reserve in parallel. The pool bounds the concurrency, and `lock_timeout` bounds each wait.
- The race for the last unit never needs another attempt: its loser gets a final answer, `out_of_stock`, the first time.
- A sustained burst on one product, with several workers, can queue more reservations on its row than `lock_timeout` lets wait: under the load of the whole test suite, the milestone 5 review counted up to 15 sessions waiting on the row of the concurrency test, and waits of 2.1 to 2.5 s, past the 2 s of `LOCK_TIMEOUT_MS`. The unit that waited too long rolls back whole as `ServiceUnavailable`, and its comment stays `received`: the consumer tries the delivery again twice in the process, after a jittered delay (ADR 0004), and at worst leaves it pending for a reclaim after `QUEUE_RECLAIM_MIN_IDLE_MS` (120 s), and that buyer gets an answer two minutes later or more. The cost is retries and latency, never an oversell: the counters only ever move by the conditional update that holds the row's lock. `tests/integration/test_process_comment_concurrency.py` therefore retries each delivery as the consumer does, and fails, with the error's type, only on a delivery that still fails after those retries.
- A buyer holds at most one pending order per product, of at most `MAX_QUANTITY_PER_COMMENT` (10) units: a troll can still hold that much stock per account and product until the reservation expires.
- An unpaid reservation holds its stock for `RESERVATION_TTL_SECONDS` plus at most one sweep interval (5 s). One whose buyer opened a payment holds it at least `PAYMENT_GRACE_SECONDS` from that moment, then until the provider has expired the session: while the provider cannot be reached, the stock stays reserved, and the sweep asks again after each lease of 30 s. A reservation is never released under a payment the buyer made.
- Ending a session never waits for the reservations, nor they for it: the reservation reads the session's row without locking it, and the `FOR KEY SHARE` of its foreign keys does not conflict with the `FOR NO KEY UPDATE` of the transition. A reservation whose statement started before an `end` committed still reads the session as live, and may open a pending order in a session that is ended by the time it commits (checked with two connections on PostgreSQL 16). The stock stays exact, since the product's row is what serializes the reservations; that order expires like any other, with the sweep. A hard cut would need `end` to lock the session's product rows (`FOR NO KEY UPDATE`) before it changes the status.

## Alternatives considered

- **`SELECT ... FOR UPDATE`, then an `UPDATE`**: the same lock on the same row, held across a second round trip, with the rule checked in Python between the two statements instead of by the statement that writes.
- **SERIALIZABLE (or REPEATABLE READ)**: the losers of a race get a serialization failure and must retry; under a burst on one product the retries multiply, where READ COMMITTED's second evaluation gives each one its final answer at once.
- **A Lua script decrementing the stock in Redis**: the stock would live in two places, and the order is written in PostgreSQL anyway; a crash between the two would lose or leak units, which a reconciliation would have to find.
- **An optimistic version column**: every conflict is a retry.
- **One counter per product without `reserved` and `sold`**: releasing an unpaid reservation and recording a sale could no longer be told apart.
