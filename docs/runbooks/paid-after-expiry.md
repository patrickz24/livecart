# Runbook: an order paid after its reservation expired

## What happened

The worker applied a payment event to an order whose reservation the expiry sweep had already released: the order is now `paid_after_expiry`. Its units went back to the stock when the sweep expired it, and may since have been reserved or sold to another buyer, so the payment is not turned into a sale: the stock is left as it is, and the buyer has paid for an order LiveCart will not ship.

With a hosted checkout, the sweep expires the provider's session before it releases the stock (expire-first), and a session that expired can no longer be paid, so this state is a defence in depth: it means that a provider confirmed a payment LiveCart did not expect.

## How it shows

- The worker logs `payment_after_expiry` at ERROR, with the provider, the event id and type, the order's id and reference, and `order_status=paid_after_expiry`.
- `livecart_orders_paid_after_expiry_total` on the worker's metrics port counts it.
- The dashboard receives the order's `order.updated` event with the status `paid_after_expiry`, and shows its badge.
- In PostgreSQL, the order has `status = 'paid_after_expiry'` and a `paid_at`; its attempt keeps the status the sweep left it in, `expired`, and records the provider's payment in `provider_payment_ref`; the event is in `processed_webhook_events` with `outcome = 'applied'`.

## What to do

1. Find the order by its reference, and its payment attempts (`payments.order_id`): the attempt whose `provider_ref` is the event's checkout session is the one the provider charged.
2. Check at the provider that the session was paid and for how much: the Stripe dashboard (test mode) shows the Checkout Session and its PaymentIntent, whose id is that attempt's `payments.provider_payment_ref`, recorded from the provider's event although the attempt stays `expired`. The mock provider charges nothing: there is nothing to refund.
3. If the product is still in stock and the seller agrees, the seller may sell it to the buyer again outside LiveCart; otherwise refund the payment in full at the provider (for Stripe: refund the PaymentIntent from the dashboard).
4. Tell the buyer, through the platform, that the order was refunded, with its reference.

Refunds are out of scope in the MVP: LiveCart records no `refunded` status, and a "Mark refunded" action on the dashboard is on the roadmap. Nothing in LiveCart needs to change after the refund.
