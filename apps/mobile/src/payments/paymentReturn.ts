import * as Linking from "expo-linking";

import { parseOrderReference } from "../orders/reference";

/**
 * The app's link the API's bridge page sends the buyer back to once the
 * provider's page is done: `<scheme>://payment-return` in a build of the
 * brand, `exp://<the PC>:8081/--/payment-return` in Expo Go, which the API
 * takes in development only, from a private address. No query, no fragment:
 * the bridge page adds `?status=...&reference=...` itself, never the token.
 */
export function paymentReturnUrl(): string {
  return Linking.createURL("payment-return");
}

export type PaymentOutcome = "success" | "cancel";

/** The outcome and the reference of a return link, or null when it is none. */
export function parsePaymentReturn(
  url: string,
): { status: PaymentOutcome; reference: string } | null {
  const query = url.split("#")[0]?.split("?")[1];
  if (query === undefined) {
    return null;
  }
  const params = new URLSearchParams(query);
  const status = params.get("status");
  const reference = parseOrderReference(params.get("reference") ?? "");
  if ((status !== "success" && status !== "cancel") || reference === null) {
    return null;
  }
  return { status, reference };
}

/**
 * The waits between two reads of the order's status after a payment: 3 s,
 * then 1.5 times longer each time, 10 s at most, until 60 s have passed. The
 * provider's webhook reaches the API, then the worker within about 2 s, so
 * the first reads are the likely ones; the later ones cover a slow webhook
 * without asking the API every 3 s for a minute.
 */
export function confirmationDelays({
  first = 3_000,
  factor = 1.5,
  longest = 10_000,
  budget = 60_000,
} = {}): number[] {
  const delays: number[] = [];
  let elapsed = 0;
  let next = first;
  while (elapsed < budget) {
    const delay = Math.min(next, longest, budget - elapsed);
    delays.push(delay);
    elapsed += delay;
    next = Math.round(next * factor);
  }
  return delays;
}
