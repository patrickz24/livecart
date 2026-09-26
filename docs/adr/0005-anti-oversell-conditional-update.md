# ADR 0005: Anti-oversell: one conditional UPDATE, one pending order per buyer and product

## Status

Accepted, 2026-09-26. Milestone 3 implemented the reservation and its proof under concurrency. The expiry of unpaid reservations arrives in milestone 4a and gets its own section in this record then.

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

The seller's restock is the same kind of statement: one `UPDATE ... SET stock_total = stock_total + :delta` whose `WHERE` requires the new total to stay between `reserved + sold` and the maximum, so a restock waiting on a reservation decides on the counters it committed, and answers 409 otherwise.

### The proof

- `tests/integration/test_reservation_concurrency.py`: a thousand `ReserveStock` calls on a product stocked at 200, released together by an `asyncio.Barrier`, through the worker's pool of 20 connections, three rounds. Each round asserts invariants only: the counters within `ck_products_stock`, exactly 200 reservations, one pending order with a distinct reference per reservation, every other intent `out_of_stock`, every comment processed. A probe of the pool fails the round unless all twenty connections were checked out at once, so the contention is measured, not assumed.
- Twenty concurrent comments of one buyer on one product give one order and nineteen `already_reserved` intents, with the rolled-back reservations leaving no trace on the stock.
- The scenarios of `tests/contract/test_order_reservations.py` run on the in-memory fake and on PostgreSQL alike (ADR 0002).

## Consequences

- Reservations of one product serialize on its row lock, each for the length of one short transaction; different products reserve in parallel. The pool bounds the concurrency, and `lock_timeout` bounds each wait.
- A burst never fails for a reason another attempt would fix: the loser of the last unit gets a final answer, `out_of_stock`, the first time.
- A buyer holds at most one pending order per product, of at most `MAX_QUANTITY_PER_COMMENT` (10) units: a troll can still hold that much stock per account and product until the reservation expires.
- Until the expiry sweep of milestone 4a runs, `reserved_until` is recorded but nothing releases an unpaid reservation.

## Alternatives considered

- **`SELECT ... FOR UPDATE`, then an `UPDATE`**: the same lock on the same row, held across a second round trip, with the rule checked in Python between the two statements instead of by the statement that writes.
- **SERIALIZABLE (or REPEATABLE READ)**: the losers of a race get a serialization failure and must retry; under a burst on one product the retries multiply, where READ COMMITTED's second evaluation gives each one its final answer at once.
- **A Lua script decrementing the stock in Redis**: the stock would live in two places, and the order is written in PostgreSQL anyway; a crash between the two would lose or leak units, which a reconciliation would have to find.
- **An optimistic version column**: every conflict is a retry.
- **One counter per product without `reserved` and `sold`**: releasing an unpaid reservation and recording a sale could no longer be told apart.
