/**
 * The live view's state, and the pure reducer that applies the stream to it.
 *
 * A snapshot replaces the state; every event after it is applied by upsert,
 * keyed by the id its payload names, so an event received twice (a
 * redelivered comment publishes its stored outcome again, an event published
 * between the snapshot's cursor and its read is in both) changes nothing the
 * second time. The lists are kept newest first by id: the API's ids are UUID
 * v7, whose text sorts by creation time, so an event that arrives late still
 * lands in its place, and each list keeps the snapshot's `limit` newest.
 *
 * The statuses only move forward along the API's machines: an order event
 * older than what the state holds (pending after paid) is ignored rather
 * than shown, and so is a shipment's or a session's. The stock counters have
 * no such order: the last event wins, and the next snapshot is the truth.
 */
import type {
  IntentPayload,
  LiveSessionEvent,
  LiveSessionResponse,
  LiveSessionSnapshot,
  LiveSessionStatus,
  OrderStatus,
  ProductResponse,
  RejectionPayload,
  ShipmentStatus,
  SnapshotOrder,
} from "@livecart/api-client";

/** An order as the live view holds it: its last payload, and its shipment. */
export type LiveOrder = SnapshotOrder;

export interface LiveState {
  session: LiveSessionResponse;
  /** In the catalog's order. */
  products: readonly ProductResponse[];
  /** Newest first, at most `limit` of each. */
  intents: readonly IntentPayload[];
  rejections: readonly RejectionPayload[];
  orders: readonly LiveOrder[];
  limit: number;
}

/** Every status each status may still become, along the API's machines. */
const ORDER_NEXT: Record<OrderStatus, readonly OrderStatus[]> = {
  pending_payment: ["paid", "expired", "paid_after_expiry"],
  expired: ["paid_after_expiry"],
  paid: [],
  paid_after_expiry: [],
};
const SHIPMENT_NEXT: Record<ShipmentStatus, readonly ShipmentStatus[]> = {
  created: ["in_transit", "out_for_delivery", "delivered", "cancelled"],
  in_transit: ["out_for_delivery", "delivered"],
  out_for_delivery: ["delivered"],
  delivered: [],
  cancelled: [],
};
const SESSION_NEXT: Record<LiveSessionStatus, readonly LiveSessionStatus[]> = {
  draft: ["live", "ended"],
  live: ["ended"],
  ended: [],
};

function mayFollow<S extends string>(next: Record<S, readonly S[]>, from: S, to: S): boolean {
  return from === to || next[from].includes(to);
}

/** Two flat payloads hold the same values (a nested shipment is compared the same way). */
function same(a: object, b: object): boolean {
  const left: [string, unknown][] = Object.entries(a);
  if (left.length !== Object.keys(b).length) {
    return false;
  }
  const right = b as Record<string, unknown>;
  return left.every(([key, value]) => {
    const other = right[key];
    if (
      typeof value === "object" &&
      value !== null &&
      typeof other === "object" &&
      other !== null
    ) {
      return same(value, other);
    }
    return Object.is(value, other);
  });
}

function byIdDescending<T>(id: (item: T) => string) {
  return (a: T, b: T) => (id(a) < id(b) ? 1 : id(a) > id(b) ? -1 : 0);
}

/**
 * `items` with `item` in place of the one of the same id, or inserted in id
 * order; the same array when nothing changes. Past `limit`, the oldest goes.
 */
function upsert<T extends object>(
  items: readonly T[],
  item: T,
  id: (item: T) => string,
  limit: number,
): readonly T[] {
  const key = id(item);
  const index = items.findIndex((existing) => id(existing) === key);
  if (index >= 0) {
    const existing = items[index];
    if (existing !== undefined && same(existing, item)) {
      return items;
    }
    return items.with(index, item);
  }
  const at = items.findIndex((existing) => id(existing) < key);
  const inserted = at < 0 ? [...items, item] : items.toSpliced(at, 0, item);
  return inserted.length > limit ? inserted.slice(0, limit) : inserted;
}

const intentId = (intent: IntentPayload) => intent.intent_id;
const rejectionId = (rejection: RejectionPayload) => rejection.comment_id;
const orderId = (order: LiveOrder) => order.order_id;

export function fromSnapshot(snapshot: LiveSessionSnapshot): LiveState {
  return {
    session: snapshot.session,
    products: snapshot.products,
    intents: snapshot.intents.toSorted(byIdDescending(intentId)),
    rejections: snapshot.rejections.toSorted(byIdDescending(rejectionId)),
    orders: snapshot.orders.toSorted(byIdDescending(orderId)),
    limit: snapshot.limit,
  };
}

/** The state after `event`: the same object when the event changes nothing. */
export function applyEvent(state: LiveState, event: LiveSessionEvent): LiveState {
  switch (event.type) {
    case "intent.recorded": {
      const intents = upsert(state.intents, event.data, intentId, state.limit);
      return intents === state.intents ? state : { ...state, intents };
    }
    case "comment.rejected": {
      const rejections = upsert(state.rejections, event.data, rejectionId, state.limit);
      return rejections === state.rejections ? state : { ...state, rejections };
    }
    case "order.updated": {
      const current = state.orders.find((order) => order.order_id === event.data.order_id);
      if (current !== undefined && !mayFollow(ORDER_NEXT, current.status, event.data.status)) {
        return state;
      }
      const order: LiveOrder = { ...event.data, shipment: current?.shipment ?? null };
      const orders = upsert(state.orders, order, orderId, state.limit);
      return orders === state.orders ? state : { ...state, orders };
    }
    case "shipment.updated": {
      const index = state.orders.findIndex((order) => order.order_id === event.data.order_id);
      const order = state.orders[index];
      // A parcel of an order older than the view's window: the view does not show it.
      if (order === undefined) {
        return state;
      }
      if (order.shipment !== null) {
        if (
          !mayFollow(SHIPMENT_NEXT, order.shipment.status, event.data.status) ||
          same(order.shipment, event.data)
        ) {
          return state;
        }
      }
      return { ...state, orders: state.orders.with(index, { ...order, shipment: event.data }) };
    }
    case "stock.updated": {
      const index = state.products.findIndex((product) => product.id === event.data.product_id);
      const product = state.products[index];
      if (product === undefined) {
        return state;
      }
      const { stock_total, reserved, sold, available } = event.data;
      const updated = { ...product, stock_total, reserved, sold, available };
      return same(product, updated)
        ? state
        : { ...state, products: state.products.with(index, updated) };
    }
    case "session.updated": {
      const { session } = state;
      if (!mayFollow(SESSION_NEXT, session.status, event.data.status)) {
        return state;
      }
      const updated: LiveSessionResponse = {
        ...session,
        status: event.data.status,
        title: event.data.title,
        started_at:
          event.data.started_at === undefined ? session.started_at : event.data.started_at,
        ended_at: event.data.ended_at === undefined ? session.ended_at : event.data.ended_at,
      };
      return same(session, updated) ? state : { ...state, session: updated };
    }
    case "stream.gap":
    case "stream.close":
      // The stream's own messages: the client acts on them, the state does not.
      return state;
  }
}
