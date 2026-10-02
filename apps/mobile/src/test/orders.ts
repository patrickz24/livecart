import type { OrderDetailsResponse, TrackingResponse } from "@livecart/api-client";

/** An order as GET /orders/{reference}/details answers it: pending, without an address. */
export function orderDetails(changes: Partial<OrderDetailsResponse> = {}): OrderDetailsResponse {
  return {
    reference: "LC-7K3F9Q",
    status: "pending_payment",
    product: { code: "A12", variant: "M", name: "Linen shirt" },
    quantity: 1,
    unit_price_minor: 2500,
    total_minor: 2500,
    currency: "EUR",
    reserved_until: "2026-10-02T18:15:00Z",
    shipping_address: null,
    paid_at: null,
    expired_at: null,
    ...changes,
  };
}

export const ADDRESS = {
  full_name: "Camille Martin",
  line1: "12 rue des Lilas",
  postal_code: "69003",
  city: "Lyon",
  country: "FR",
};

/** A parcel as GET /orders/{reference}/tracking answers it. */
export function tracking(changes: Partial<TrackingResponse> = {}): TrackingResponse {
  return {
    reference: "LC-7K3F9Q",
    carrier: "mock",
    tracking_number: "MOCK-7K3F9Q2B",
    status: "in_transit",
    last_event_at: "2026-10-02T18:20:00Z",
    ...changes,
  };
}
