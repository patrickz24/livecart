// Compile-time checks of the generated contract, run by `pnpm -r typecheck` (make lint):
// this file stops compiling when the generated client no longer exports what the apps
// rely on. It is not re-exported by index.ts, and holds no runtime logic.
import type { LiveSessionEvent, LiveSessionSnapshot, ShipmentPayload } from "./generated";

type Same<A, B> = [A] extends [B] ? ([B] extends [A] ? true : false) : false;

/** The messages of a session's stream after its snapshot, by SSE event name. */
type StreamedEventType =
  | "intent.recorded"
  | "comment.rejected"
  | "order.updated"
  | "stock.updated"
  | "shipment.updated"
  | "session.updated"
  | "stream.gap"
  | "stream.close";

/** Every type of the stream is in the generated union, and nothing else. */
export const everyEventTypeIsGenerated: Same<LiveSessionEvent["type"], StreamedEventType> = true;

/** Narrowing on `type` gives each member its own payload. */
export function stockOf(event: LiveSessionEvent): number | null {
  return event.type === "stock.updated" ? event.data.available : null;
}

/** The snapshot carries the cursor that the events after it follow. */
export const snapshotHasItsCursor: Same<LiveSessionSnapshot["events_cursor"], string> = true;

/** Each order of the snapshot carries its shipment as shipment.updated does, or null. */
export const snapshotOrdersCarryTheirShipment: Same<
  LiveSessionSnapshot["orders"][number]["shipment"],
  ShipmentPayload | null
> = true;
