# ADR 0007: Order reference and HMAC-derived access token, two tiers of access

## Status

Accepted, 2026-09-26. Milestone 3 implemented the reference, the token and the two tiers of `GET /api/v1/orders/{reference}`; milestone 4a the notice that delivers them to the buyer, through the simulated notifier of development. Milestone 5 added the routes that take the address and start the payment to the token tier; the one that follows the delivery joins it with milestone 6.

## Context

An order is born from a comment, for a buyer who has no account: the platform user id of the comment is all LiveCart knows of them. The buyer gets a short reference such as `LC-7K3F9Q`, which the seller reads aloud on the live and the buyer types on a phone. Such a reference is public by nature, and short: it cannot also be what lets someone pay for the order, read its shipping address or follow its delivery, since anyone watching could use it (OWASP API1, broken object level authorization). A comment may also be delivered twice, and the second delivery must give the buyer the same credentials as the first, without storing any of them in clear.

## Decision

### The reference identifies

- `LC-` then six symbols of Crockford's base 32 (`0-9` and `A-Z` without `I`, `L`, `O` and `U`, the letters people misread): 32^6 = 2^30 references, drawn uniformly by one `randbelow` call on the `secrets` module, so that a reference says nothing of the previous one. The random source is injected, so that tests can force a collision.
- It is read tolerantly (`parse_order_reference`): any case, the hyphen optional, `O` for `0`, `I` and `L` for `1`; anything else is no reference, answered like an unknown one, without a query.
- `orders.reference` is `CHAR(9)` with `ck_orders_reference` and `uq_orders_reference`. Unit 3 inserts each order under a savepoint nested in the reservation's: when the unique index refuses a reference, only that savepoint rolls back and a new one is drawn, the reserved stock kept. After three collisions (`REFERENCE_DRAWS`) the unit raises `ServiceUnavailable` and rolls back whole, so the comment stays received for its redelivery; no `IntegrityError` reaches the use case. A test draws the same reference twice through an injected source, on the fake and on PostgreSQL.

### The token authorizes

- The access token of an order is `base64url(HMAC-SHA256(ORDER_TOKEN_KEY, order_id))`, without padding: 256 bits in 43 characters (`livecart_core.order_tokens`). Nothing random goes in, so it can be computed again at any time: it is never stored, and a second delivery can hand out exactly the first one.
- `orders.access_token_hash` holds the SHA-256 of the HMAC's 32 bytes (`ck_orders_access_token_hash` checks the length), written by unit 3 with the order. A copy of the database alone yields no usable token.
- Verifying needs the stored hash only, never the key: `OrderTokens.verify` refuses a malformed or non-canonical token before any comparison, then compares the hashes with `hmac.compare_digest`.
- `ORDER_TOKEN_KEY` is at least 32 bytes and must differ from `JWT_SECRET`; the settings refuse otherwise, and refuse a `change-me` value in production.

### The notice

- Once unit 3 has committed an order, the worker (`ProcessComment`) sends its buyer, the author of the comment, the reference and the token through the `BuyerNotifier` port, the token computed then from the order id. It stamps `orders.buyer_notified_at` after the notice, and acknowledges the comment's delivery last.
- A crash in between leaves the delivery pending, and its redelivery finds the comment processed: it sends the notice again, with the very same token, when `buyer_notified_at` is still empty and the order still pending, and nothing otherwise. A buyer may get the same message twice, never two tokens for one order.
- The platforms' direct messages are out of the MVP's scope. `SimulatedBuyerNotifier` appends each notice (order id, platform, recipient, reference, token, and the link `<BUYER_LINK_SCHEME>://link/<reference>#t=<token>`, whose fragment the app reads and never sends to a server) to the development stream `{prefix}:dev:buyer-dm`, with `XADD ... MAXLEN ~ 1000` and a sliding `EXPIRE` of an hour, for the comment simulator's text mode to show (`livecart-simulate --text`). That stream holds working tokens in clear: the notifier refuses to be built when `APP_ENV=production`.
- The token never reaches a log line: `BuyerNotification` keeps it out of its repr, a Redis failure of the notice is reported by its type alone, and a test runs the worker with every logger at `DEBUG`, SQLAlchemy's statements included, and finds the token in the stream and nowhere in the logs.

### Two tiers

- `GET /api/v1/orders/{reference}` needs the reference alone and answers the public projection: status, product code, variant and name, quantity, total, currency and `reserved_until`. No buyer id, no address, no seller.
- `GET /api/v1/orders/{reference}/details` also needs `Authorization: Bearer <token>` and adds the unit price, the shipping address and the payment and expiry times. A missing, malformed or wrong token, and the token of another order, get the very 404 of an unknown reference: the route tells a guesser nothing.
- Both are rate limited (ADR 0011): the lookup to 30 requests and 20 not-found answers a minute per client address, with a warning logged when the not-found answers of all addresses together pass a threshold; the details to 60 requests a minute per token, counted by its SHA-256.
- Seller routes follow the same rule for their own objects: every query filters by the seller of the access token, and another seller's session or product answers 404, never 403.

## Consequences

- A reference heard on a live shows the order's status and total, nothing personal, and guessing references is slow: after 20 not-found answers, an address finds nothing for the rest of its window, up to a minute, existing orders included.
- The buyer keeps two things, the reference to read and the token to act. The API reads the token from the `Authorization` header only, never from a URL, where it would reach logs and referrers.
- Whoever holds `ORDER_TOKEN_KEY` can compute every token: its rotation is not implemented. A single order's token cannot be revoked either, since it is derived rather than stored.
- 2^30 references are enough while the orders stay far fewer; running out of draws would show as `ServiceUnavailable`, not as a duplicate.
- A production worker needs a notifier that delivers on the platforms themselves. None exists yet, and the simulated one is refused there: the worker does not start with `APP_ENV=production` until a platform integration brings one.

## Alternatives considered

- **The reference alone as the authorization**: simpler for the buyer, but anyone who heard it on the live could pay for the order or read its address.
- **A random token, stored hashed**: as strong, but it cannot be sent again after a crash between the commit and the notification without being stored in clear somewhere.
- **A signed JWT per order**: longer to carry, with an expiry to manage, where the HMAC token is as unforgeable and shorter.
- **Sequential order numbers**: guessable, and they reveal the number of orders.
- **Longer references**: harder to read aloud; six symbols keep collisions rare at this scale.
