import type {
  IntentPayload,
  LiveSessionEvent,
  LiveSessionSnapshot,
  OrderPayload,
  ShipmentPayload,
} from "@livecart/api-client";
import { describe, expect, it } from "vitest";

import { aProduct, aSession, SESSION_ID } from "../test/fixtures";
import { applyEvent, fromSnapshot, type LiveState } from "./applyEvent";

// UUID v7 ids: their text sorts by creation time.
const id = (n: number) => `0199a000-0000-7000-8000-${String(n).padStart(12, "0")}`;
const PRODUCT = aProduct({ id: id(900) });

function envelope<T extends LiveSessionEvent["type"]>(type: T) {
  return {
    type,
    session_id: SESSION_ID,
    occurred_at: "2026-10-02T10:00:00Z",
    correlation_id: null,
  };
}

function intent(n: number, overrides: Partial<IntentPayload> = {}): IntentPayload {
  return {
    intent_id: id(n),
    comment_id: id(n + 1000),
    product_id: PRODUCT.id,
    quantity: 1,
    source: "parser",
    confidence: "1",
    outcome: "reserved",
    order_id: id(n + 2000),
    ...overrides,
  };
}

function order(n: number, overrides: Partial<OrderPayload> = {}): OrderPayload {
  return {
    order_id: id(n),
    reference: `LC-${String(n).padStart(6, "0")}`,
    product_id: PRODUCT.id,
    quantity: 1,
    total_minor: 2990,
    currency: "EUR",
    status: "pending_payment",
    reserved_until: "2026-10-02T10:15:00Z",
    paid_at: null,
    expired_at: null,
    ...overrides,
  };
}

function shipment(orderNumber: number, status: ShipmentPayload["status"]): ShipmentPayload {
  return {
    shipment_id: id(orderNumber + 5000),
    order_id: id(orderNumber),
    carrier: "mock",
    tracking_number: "MOCK-ABC123",
    status,
    last_event_at: "2026-10-02T10:05:00Z",
  };
}

function snapshot(overrides: Partial<LiveSessionSnapshot> = {}): LiveSessionSnapshot {
  return {
    session: aSession({ status: "live", started_at: "2026-10-02T09:30:00Z" }),
    events_cursor: "1-0",
    products: [PRODUCT],
    intents: [intent(1)],
    rejections: [{ comment_id: id(3001), reason: "no_intent" }],
    orders: [{ ...order(2001), shipment: null }],
    limit: 3,
    taken_at: "2026-10-02T10:00:00Z",
    ...overrides,
  };
}

const intentRecorded = (data: IntentPayload): LiveSessionEvent => ({
  ...envelope("intent.recorded"),
  data,
});
const orderUpdated = (data: OrderPayload): LiveSessionEvent => ({
  ...envelope("order.updated"),
  data,
});
const shipmentUpdated = (data: ShipmentPayload): LiveSessionEvent => ({
  ...envelope("shipment.updated"),
  data,
});

/** Applying each event twice gives what applying it once gives. */
function applyTwice(state: LiveState, event: LiveSessionEvent): [LiveState, LiveState] {
  const once = applyEvent(state, event);
  return [once, applyEvent(once, event)];
}

describe("fromSnapshot", () => {
  it("holds the snapshot's lists newest first by id", () => {
    const state = fromSnapshot(snapshot({ intents: [intent(1), intent(3), intent(2)], limit: 10 }));

    expect(state.intents.map((item) => item.intent_id)).toEqual([id(3), id(2), id(1)]);
    expect(state.products).toEqual([PRODUCT]);
    expect(state.session.status).toBe("live");
  });
});

describe("applyEvent", () => {
  const base = fromSnapshot(snapshot());

  it("inserts a new intent in id order, and keeps the limit's newest", () => {
    const state = [intent(4), intent(2), intent(3)].reduce(
      (current, item) => applyEvent(current, intentRecorded(item)),
      base,
    );

    expect(state.intents.map((item) => item.intent_id)).toEqual([id(4), id(3), id(2)]);
  });

  it("upserts an intent: twice is once, and the same state comes back", () => {
    const [once, twice] = applyTwice(base, intentRecorded(intent(5, { source: "llm" })));

    expect(once.intents).toHaveLength(2);
    expect(once.intents[0]?.source).toBe("llm");
    expect(twice).toBe(once);
  });

  it("upserts a rejection by its comment", () => {
    const event: LiveSessionEvent = {
      ...envelope("comment.rejected"),
      data: { comment_id: id(3002), reason: "ambiguous_unresolved" },
    };
    const [once, twice] = applyTwice(base, event);

    expect(once.rejections.map((item) => item.reason)).toEqual([
      "ambiguous_unresolved",
      "no_intent",
    ]);
    expect(twice).toBe(once);
  });

  it("upserts an order, keeping the shipment it already has", () => {
    const shipped = applyEvent(base, shipmentUpdated(shipment(2001, "created")));
    const paid = order(2001, { status: "paid", paid_at: "2026-10-02T10:03:00Z" });

    const [once, twice] = applyTwice(shipped, orderUpdated(paid));

    expect(once.orders).toHaveLength(1);
    expect(once.orders[0]?.status).toBe("paid");
    expect(once.orders[0]?.shipment?.status).toBe("created");
    expect(twice).toBe(once);
  });

  it("adds a new order newest first", () => {
    const state = applyEvent(base, orderUpdated(order(2005)));

    expect(state.orders.map((item) => item.order_id)).toEqual([id(2005), id(2001)]);
    expect(state.orders[0]?.shipment).toBeNull();
  });

  it("never moves an order back along its machine", () => {
    const paid = applyEvent(base, orderUpdated(order(2001, { status: "paid" })));

    expect(applyEvent(paid, orderUpdated(order(2001)))).toBe(paid);

    const expired = applyEvent(base, orderUpdated(order(2001, { status: "expired" })));
    const late = applyEvent(expired, orderUpdated(order(2001, { status: "paid_after_expiry" })));
    expect(late.orders[0]?.status).toBe("paid_after_expiry");
    expect(applyEvent(late, orderUpdated(order(2001, { status: "expired" })))).toBe(late);
  });

  it("moves a shipment forward only, and ignores one of an order it does not hold", () => {
    const created = applyEvent(base, shipmentUpdated(shipment(2001, "created")));
    const [transit, again] = applyTwice(created, shipmentUpdated(shipment(2001, "in_transit")));

    expect(transit.orders[0]?.shipment?.status).toBe("in_transit");
    expect(again).toBe(transit);
    expect(applyEvent(transit, shipmentUpdated(shipment(2001, "created")))).toBe(transit);
    expect(applyEvent(base, shipmentUpdated(shipment(9999, "created")))).toBe(base);
  });

  it("sets a product's counters from stock.updated, idempotently", () => {
    const event: LiveSessionEvent = {
      ...envelope("stock.updated"),
      data: { product_id: PRODUCT.id, stock_total: 10, reserved: 4, sold: 1, available: 5 },
    };
    const [once, twice] = applyTwice(base, event);

    expect(once.products[0]).toEqual({ ...PRODUCT, reserved: 4, sold: 1, available: 5 });
    expect(twice).toBe(once);
    expect(applyEvent(base, { ...event, data: { ...event.data, product_id: id(901) } })).toBe(base);
  });

  it("follows the session forward, and not back", () => {
    const event: LiveSessionEvent = {
      ...envelope("session.updated"),
      data: {
        status: "ended",
        title: "Friday drop",
        started_at: "2026-10-02T09:30:00Z",
        ended_at: "2026-10-02T11:00:00Z",
      },
    };
    const [ended, twice] = applyTwice(base, event);

    expect(ended.session.status).toBe("ended");
    expect(ended.session.ended_at).toBe("2026-10-02T11:00:00Z");
    expect(twice).toBe(ended);
    expect(
      applyEvent(ended, { ...event, data: { ...event.data, status: "live", ended_at: null } }),
    ).toBe(ended);
  });

  it("leaves the state alone for the stream's own messages", () => {
    expect(applyEvent(base, { ...envelope("stream.gap"), data: { after: "1-0" } })).toBe(base);
    expect(
      applyEvent(base, { ...envelope("stream.close"), data: { reason: "token_expiring" } }),
    ).toBe(base);
  });

  it("gives the same state whatever the order of distinct events", () => {
    const events = [
      intentRecorded(intent(7)),
      orderUpdated(order(2007)),
      intentRecorded(intent(6)),
      orderUpdated(order(2006)),
    ];
    const forward = events.reduce(applyEvent, base);
    const backward = events.toReversed().reduce(applyEvent, base);

    expect(backward).toEqual(forward);
  });
});
