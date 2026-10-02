import type { OrderStatus } from "@livecart/api-client";
import { useEffect, useState } from "react";

import { isNotFound } from "../api/errors";

export type Confirmation =
  | { kind: "confirming" }
  /** The order left pending_payment: paid, expired or paid after its expiry. */
  | { kind: "settled"; status: Exclude<OrderStatus, "pending_payment"> }
  /** Still pending once every read of `delays` was made. */
  | { kind: "timed_out" }
  /** The API knows no order with this token. */
  | { kind: "not_found" };

/**
 * Reads the order's status now, then after each of `delays`, until it leaves
 * pending_payment or the delays run out. A failed read (no network, a 5xx)
 * waits for the next one, except a 404, which no wait will change.
 * `readStatus` must keep its identity between renders.
 */
export function usePaymentConfirmation(
  readStatus: () => Promise<OrderStatus>,
  delays: readonly number[],
): Confirmation {
  const [confirmation, setConfirmation] = useState<Confirmation>({ kind: "confirming" });

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let reads = 0;

    const read = async () => {
      try {
        const status = await readStatus();
        if (stopped) {
          return;
        }
        if (status !== "pending_payment") {
          setConfirmation({ kind: "settled", status });
          return;
        }
      } catch (error) {
        if (stopped) {
          return;
        }
        if (isNotFound(error)) {
          setConfirmation({ kind: "not_found" });
          return;
        }
      }
      const delay = delays[reads];
      reads += 1;
      if (delay === undefined) {
        setConfirmation({ kind: "timed_out" });
        return;
      }
      timer = setTimeout(() => void read(), delay);
    };

    setConfirmation({ kind: "confirming" });
    void read();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [readStatus, delays]);

  return confirmation;
}
