/**
 * The dashboard's labels for the contract's closed lists. Each record is typed
 * by the generated union it labels, so a value added to the API's schema does
 * not compile here until it has its label.
 */
import type {
  IntentOutcome,
  LiveSessionStatus,
  OrderStatus,
  Platform,
  RejectionReason,
  ShipmentStatus,
} from "@livecart/api-client";

import type { Tone } from "./ui/feedback";

export const PLATFORM_LABELS: Record<Platform, string> = {
  simulator: "Simulator",
  facebook: "Facebook",
  instagram: "Instagram",
  tiktok: "TikTok",
};

export const SESSION_STATUS: Record<LiveSessionStatus, { label: string; tone: Tone }> = {
  draft: { label: "Draft", tone: "neutral" },
  live: { label: "Live", tone: "danger" },
  ended: { label: "Ended", tone: "info" },
};

export const ORDER_STATUS: Record<OrderStatus, { label: string; tone: Tone }> = {
  pending_payment: { label: "Awaiting payment", tone: "warning" },
  paid: { label: "Paid", tone: "success" },
  expired: { label: "Expired", tone: "neutral" },
  paid_after_expiry: { label: "Paid after expiry", tone: "danger" },
};

export const SHIPMENT_STATUS: Record<ShipmentStatus, { label: string; tone: Tone }> = {
  created: { label: "Label created", tone: "neutral" },
  in_transit: { label: "In transit", tone: "info" },
  out_for_delivery: { label: "Out for delivery", tone: "info" },
  delivered: { label: "Delivered", tone: "success" },
  cancelled: { label: "Cancelled", tone: "danger" },
};

export const INTENT_OUTCOME: Record<IntentOutcome, { label: string; tone: Tone }> = {
  reserved: { label: "Reserved", tone: "success" },
  out_of_stock: { label: "Out of stock", tone: "danger" },
  already_reserved: { label: "Already reserved", tone: "warning" },
};

export const REJECTION_REASON: Record<RejectionReason, string> = {
  no_intent: "No purchase intent",
  multiple_products: "Several products in one comment",
  quantity_out_of_bounds: "Quantity out of bounds",
  variant_not_found: "Unknown variant",
  ambiguous_unresolved: "Ambiguous, not resolved",
  session_not_found: "No matching session",
  session_not_live: "Session not live",
};
