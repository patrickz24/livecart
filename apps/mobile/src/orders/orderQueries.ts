import {
  getOrderDetails,
  getOrderTracking,
  type Client,
  type OrderStatus,
  type ShipmentStatus,
} from "@livecart/api-client";
import { queryOptions } from "@tanstack/react-query";

import { unwrap } from "../api/errors";

/** The query keys of one order, all under ["order", reference] so that one call refreshes them. */
export const orderKeys = {
  all: (reference: string) => ["order", reference] as const,
  details: (reference: string) => ["order", reference, "details"] as const,
  tracking: (reference: string) => ["order", reference, "tracking"] as const,
};

/** The order in full, with its token in the Authorization header (a 404 for a wrong token). */
export function orderDetailsQuery(client: Client, reference: string, token: string) {
  return queryOptions({
    queryKey: orderKeys.details(reference),
    queryFn: ({ signal }) =>
      unwrap(getOrderDetails({ client, path: { reference }, auth: token, signal })),
  });
}

/** Where the paid order's parcel is; a 404 until the worker has created its shipment. */
export function orderTrackingQuery(client: Client, reference: string, token: string) {
  return queryOptions({
    queryKey: orderKeys.tracking(reference),
    queryFn: ({ signal }) =>
      unwrap(getOrderTracking({ client, path: { reference }, auth: token, signal })),
  });
}

export const ORDER_STATUS_TEXT: Record<OrderStatus, string> = {
  pending_payment: "Awaiting payment",
  paid: "Paid",
  expired: "Expired: the reservation ended before the payment",
  paid_after_expiry: "Paid after the reservation ended: the seller will contact you",
};

/** The steps of a parcel, in order; `cancelled` ends a shipment that never left. */
export const SHIPMENT_STEPS: readonly ShipmentStatus[] = [
  "created",
  "in_transit",
  "out_for_delivery",
  "delivered",
];

export const SHIPMENT_STATUS_TEXT: Record<ShipmentStatus, string> = {
  created: "Label created",
  in_transit: "In transit",
  out_for_delivery: "Out for delivery",
  delivered: "Delivered",
  cancelled: "Cancelled",
};

/** Whether the order has been paid, so that it ships. */
export function isPaid(status: OrderStatus): boolean {
  return status === "paid" || status === "paid_after_expiry";
}
