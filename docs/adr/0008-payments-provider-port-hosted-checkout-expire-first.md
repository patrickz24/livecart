# ADR 0008: Payment: a `PaymentProvider` port, Stripe Checkout, expire-first, the webhook registry as the outbox

## Status

Accepted, 2026-10-01. Milestone 5 implemented it whole: the port and its four adapters, the hosted checkout and its bridge page, the registry of payment webhooks and the worker's loop that applies it, and the later phases of the expiry sweep, which expire the provider's session before they release any stock.

The plan's end criterion for the milestone is half met. Verified: the mock runs the whole flow without any key on the compose stack, where on 2026-10-01 a payment reached `paid` and showed on the seller's SSE stream as an `order.updated`; and the Stripe adapter, with the real SDK, against `FakeStripeApi` only (see "What is tested"). Not verified: a payment with Stripe's test card 4242 reaching `paid`, visible in SSE, which needs a Stripe account's test keys, the maintainer's own, in `.env`; none was available. The steps are those of CONTRIBUTING's Payments section: `PAYMENT_PROVIDER=stripe` and `STRIPE_SECRET_KEY=sk_test_...` in `.env`; the secret printed by `docker compose --profile stripe run --rm stripe-cli listen --print-secret` as `STRIPE_WEBHOOK_SECRET`; `docker compose --profile stripe up -d --wait`; then, with the session's SSE stream open, the checkout paid on Stripe's page with `4242 4242 4242 4242`. Until that run, Stripe's own behaviour (its answers, its event bodies, its CLI's forwarding) is known from its documentation and its SDK, not from a call.

## Context

The requirements ask for a `PaymentProvider` interface with Stripe in test mode (a Checkout Session or a PaymentIntent), a webhook whose signature is checked and whose processing is idempotent, and PayPal and Viva Wallet as documented interfaces only. A buyer pays a reservation that holds the stock until `reserved_until`, often in its last seconds, from a phone, while several workers sweep the reservations nobody paid. Three things must never happen: a payment for stock already released to someone else, a payment applied twice, a payment lost because a process or Redis was down when the provider called. The provider delivers each webhook at least once, in any order, and retries for days until it gets a 2xx. No card data may reach LiveCart.

## Decision

### One port, four calls, provider-neutral records

`PaymentProvider` (`livecart_core.application.ports`) has four async calls: `create_checkout` (a hosted session for one payment attempt, idempotent on the attempt's key), `expire_checkout` (`True` when this call expired an open session, `False` when it is no longer open), `get_checkout` (the session as the provider sees it, with its `payment_status`) and `verify_webhook` (the signature checked before the body is read, then the event reduced to `PaymentEvent`, an allow-list without the customer's details). Transport failures are `ServiceUnavailable`, an unknown session `RecordNotFound`, a key reused with other parameters `ValueError`. The records are `CheckoutRequest`, `CheckoutSession` and `PaymentEvent`, the same for every provider.

`PAYMENT_PROVIDER` selects the adapter, which `build_payment_provider` builds in both composition roots, so that the API and the worker see the same sessions:

- `mock`, the default: sessions kept in the shared Redis, named by an HMAC of their idempotency key under `MOCK_PAYMENT_WEBHOOK_SECRET`; a hosted page served by the API outside production, whose Pay button signs a `checkout.session.completed` event with the same key and posts it to the API's own webhook. A session leaves `open` once, by `SET NX`, paid or expired, whichever comes first. Refused when `APP_ENV=production` (`refuse_mock_payments_in_production`), since it charges nothing;
- `stripe`: Stripe Checkout through stripe-python's async `StripeClient` over httpx2, with test keys only (`sk_test_`/`rk_test_`, and the `whsec_` signing secret checked by `construct_event`, 300 s of tolerance);
- `paypal` and `viva`: documented stubs that stop the process at startup.

`close_payment_provider` releases Stripe's HTTP connections when either process stops.

### A hosted Checkout Session per attempt

`StartCheckout` (`POST /api/v1/orders/{reference}/checkout`, with the order's token) runs three units in order: the return URL checked against `ALLOWED_APP_SCHEMES`; one transaction that extends the reservation to `GREATEST(reserved_until, now + PAYMENT_GRACE_SECONDS)`, guarded by `status = 'pending_payment' AND reserved_until > now`, and opens attempt n + 1, `pending` (one pending attempt per order, `uq_payments_order_id_pending`); then, once that has committed, the provider's session, card only, one line of the order's total, no `expires_at`, and its ref and URL recorded. The extension commits before the provider is called: a sweep running meanwhile finds an attempt in flight and leaves the order to the later phases, which ask the provider first. A second click returns the stored URL; a new attempt opens only once the previous one settled, `PAYMENT_MAX_ATTEMPTS` (3) times at most.

The idempotency key is per attempt, `checkout-session:{order_id}:{attempt_no}`: one attempt always gets the same session, so a crash between creating it and recording it is recovered by creating it again, by the next click or by the sweep, with the same parameters. Per attempt rather than per order, because Stripe refuses a key sent again with other parameters, and each attempt checks the amount and the return URL again. The success and cancel URLs are the bridge page of `PUBLIC_API_BASE_URL`, with the reference only, never the token; the worker builds the same `CheckoutRequest` from the same setting (`CheckoutPages`), or Stripe would refuse its replay. The bridge page is an HTML page with a button rather than a redirect, because Chrome opens another app only on a user gesture.

### The registry is the outbox: record, acknowledge, apply later

`POST /api/v1/webhooks/payments/{provider}` verifies the signature, then writes the reduced event and nothing else: `INSERT INTO processed_webhook_events ... ON CONFLICT (provider, event_id) DO UPDATE SET delivery_count = delivery_count + 1`, and answers 200. PostgreSQL alone is on that path: a webhook is recorded with Redis down, and a PostgreSQL outage answers 503, which the provider retries. Every worker polls the registry every 2 s (decision 9 of the plan): a claim takes at most 20 unprocessed events whose lease has ended, oldest first, `FOR UPDATE SKIP LOCKED`, counts one attempt on each before it is made and leases them 30 s (`claimed_until`, migration 0005), so `attempts` in the registry is the one count, a worker that died midway included; the tenth failure settles the event as `failed`. `WebhookEventRegistry.settle` applies one event in one unit: the event's row, the order, then the attempt locked, the domain's `decide_payment` (the amount and the currency charged must be the order's, or nothing moves), the compare-and-sets it decides (attempt `pending -> succeeded|failed|expired`, the order's reserved units sold, order `pending_payment -> paid` or `expired -> paid_after_expiry`), and the event marked processed with its outcome. A redelivery is one row; a replay or an event out of order finds its rows where they are.

### Expire-first

An order whose attempt is pending is never released by the first phase of the sweep. The later phases (`ExpireCheckouts`, every 5 s, after the first) lease at most 20 due orders with a pending attempt, `UPDATE orders SET expiry_lease_until = :now + 30 s WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 20) RETURNING`, so that one sweep alone asks the provider about each, 4 calls at a time per worker (`Semaphore(4)`), holding no connection during a call and one at most otherwise; the work of a claim ends before its lease does. For each order:

- an attempt without a session gets it again first, under its key;
- `expire_checkout` expired it: one unit expires the attempt and the order and releases the stock (`release_checkout`);
- it refused: the session is read back. `expired` releases the same way. `complete` and paid is reconciled through the registry's own path: the `checkout.session.completed` event the provider sends, under the id `reconciled:<session>`, recorded in the registry and applied by `ApplyPaymentEvent`, whose compare-and-sets make it count once whatever the provider's own webhook does. Anything else (the provider down, a session still open, complete but not paid yet) waits for the lease to end.

The provider decides the race between the buyer's payment and the release, once: a session it expired can no longer be paid, and one paid first refuses to expire. `paid_after_expiry` is therefore a defence in depth, for a provider that confirms a payment LiveCart did not expect ([runbook](../runbooks/paid-after-expiry.md)).

### What is tested, and how

- The provider's contract (`tests/contract/test_payment_provider.py`) runs on the fake, on the mock over Redis and on the real Stripe adapter and SDK over `FakeStripeApi`, an in-memory stand-in of the three Checkout Session calls behind an httpx2 `MockTransport`. No test reaches api.stripe.com, and none ran against a real Stripe account: the `stripe` compose profile and the 4242 card are documented for a developer's own test keys, not exercised here.
- The registry, the attempts, the lease and the release run their scenarios on the fake and on PostgreSQL; the use cases on the fakes, with every interleaving the plan names: an event received 5 times, then out of order, a wrong amount, `paid_after_expiry`, a refused expiry whose session reads back complete (paid, nothing released) or expired (released), two sweeps at once making one expiry call per order, a click while the sweep creates the missing session again, the grace that keeps a checkout from the sweep, the fourth attempt refused, an event recorded while the worker is stopped then applied at its restart, `failed` after 10 attempts, a webhook recorded with Redis down.
- The mock's whole flow runs on the compose stack, without a key: a simulated comment, its order, the address, the checkout, Pay on the hosted page, `paid`; and a checkout left unpaid, expired at the mock, then released.

## Consequences

- An order is `paid` about 2 s after the provider's webhook at most, more if every worker is down; the event waits in the registry meanwhile, durable.
- A buyer who pays in the reservation's last seconds either paid (the order is paid, from the webhook or from the sweep's reconciliation) or finds the session expired: the stock is never released under a payment.
- While a provider cannot be reached, the stock of the orders with a payment in flight stays reserved: the sweep asks again after each lease. That is the cost of expire-first, bounded by the provider's outage.
- An attempt the sweep cannot settle for a reason that needs a person (a session the provider does not know, an attempt of another provider after `PAYMENT_PROVIDER` changed, a key refused) is logged at ERROR and counted as `failed`, and asked about again after each lease.
- Refunds stay manual (the runbook), and the buyer's card never reaches LiveCart.

## Alternatives considered

- **PaymentIntent with the Payment Sheet now**: a native module, so a development build of the Expo app rather than Expo Go, and in every white-label build; the payment form and its 3-D Secure steps inside the app; and a second flow beside the mock's hosted page. A hosted Checkout Session keeps one flow for the mock and Stripe, through the browser and the bridge page. The Payment Sheet is on the roadmap.
- **Release the stock, then hope no payment comes**: a payment after the release would sell stock another buyer may hold. Stripe's own `expires_at` cannot stand in for the sweep either: it takes 30 minutes at least, more than the reservation and its grace, and a release would still need the session's state.
- **A Redis stream of payment events** (question 9 of the plan): it would double the recovery, the dead letters and a catch-up of the registry, and make the provider's webhook depend on Redis; the registry row is durable already.
- **Applying the event in the webhook's request**: the provider's call would wait on the order's locks, and a failure would leave the retry to the provider's backoff, for days.
- **One idempotency key per order**: refused by Stripe for a new attempt whose parameters differ, and it would hide a changed amount.
