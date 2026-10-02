import type { LiveSessionSnapshot, SnapshotOrder } from "@livecart/api-client";
import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { eventStream, signedInApi } from "../../test/fakeApi";
import { aProduct, aSession, SESSION_ID } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";

const EVENTS = `/api/v1/live-sessions/${SESSION_ID}/events`;
const id = (n: number) => `0199a000-0000-7000-8000-${String(n).padStart(12, "0")}`;
const PRODUCT = aProduct({ id: id(900), code: "A12", available: 7 });

function anOrder(n: number, overrides: Partial<SnapshotOrder> = {}): SnapshotOrder {
  return {
    order_id: id(n),
    reference: `LC-00000${String(n % 10)}`,
    product_id: PRODUCT.id,
    quantity: 1,
    total_minor: 2990,
    currency: "EUR",
    status: "pending_payment",
    reserved_until: new Date(Date.now() + 10 * 60_000).toISOString(),
    paid_at: null,
    expired_at: null,
    shipment: null,
    ...overrides,
  };
}

function snapshot(): LiveSessionSnapshot {
  return {
    session: aSession({ status: "live", started_at: "2026-10-02T09:30:00Z" }),
    events_cursor: "1-0",
    products: [PRODUCT],
    intents: [
      {
        intent_id: id(12),
        comment_id: id(112),
        product_id: PRODUCT.id,
        quantity: 2,
        source: "llm",
        confidence: "0.92",
        outcome: "reserved",
        order_id: id(1),
      },
      {
        intent_id: id(11),
        comment_id: id(111),
        product_id: PRODUCT.id,
        quantity: 1,
        source: "parser",
        confidence: "1",
        outcome: "out_of_stock",
        order_id: null,
      },
    ],
    rejections: [
      { comment_id: id(203), reason: "no_intent" },
      { comment_id: id(202), reason: "ambiguous_unresolved" },
      { comment_id: id(201), reason: "no_intent" },
    ],
    orders: [
      anOrder(3, {
        status: "paid",
        paid_at: "2026-10-02T09:50:00Z",
        shipment: {
          shipment_id: id(503),
          order_id: id(3),
          carrier: "mock",
          tracking_number: "MOCK-7K3F9Q",
          status: "in_transit",
          last_event_at: "2026-10-02T09:55:00Z",
        },
      }),
      anOrder(2, { status: "paid_after_expiry" }),
      anOrder(1),
    ],
    limit: 200,
    taken_at: "2026-10-02T10:00:00Z",
  };
}

function liveApi() {
  const stream = eventStream();
  const api = signedInApi("access-1").on("GET", EVENTS, stream.handler);
  return { api, stream };
}

function row(name: RegExp | string): HTMLElement {
  const header = screen.getByRole("rowheader", { name });
  const tr = header.closest("tr");
  if (tr === null) {
    throw new Error("no row");
  }
  return tr;
}

describe("live session view", () => {
  it("opens the stream with the token, then shows the snapshot", async () => {
    const { api, stream } = liveApi();
    renderApp(`/sessions/${SESSION_ID}/live`, api);
    await expect.poll(() => stream.opened.length).toBe(1);

    stream.send("snapshot", snapshot(), "1-0");

    expect(await screen.findByRole("heading", { name: "Friday drop", level: 1 })).toBeTruthy();
    const request = stream.opened[0]?.request;
    expect(request?.headers.get("Authorization")).toBe("Bearer access-1");
    expect(request?.headers.get("Last-Event-ID")).toBeNull();
    expect(screen.getByText("Live updates on")).toBeTruthy();

    // Stock per product.
    expect(within(row(/^A12/)).getAllByRole("cell")[0]?.textContent).toContain("7");

    // Orders: a countdown for the pending one, the paid-after-expiry badge, the shipment.
    expect(within(row("LC-000001")).getByText(/^\d+:\d\d left$/)).toBeTruthy();
    expect(within(row("LC-000002")).getByText("Paid after expiry")).toBeTruthy();
    expect(within(row("LC-000003")).getByText("In transit")).toBeTruthy();
    expect(within(row("LC-000003")).getByText("MOCK-7K3F9Q")).toBeTruthy();

    // Intents: the LLM's is marked AI, for screen readers too.
    const intents = within(screen.getByRole("region", { name: "Purchase intents" }));
    expect(intents.getAllByText("AI")).toHaveLength(1);
    expect(intents.getByText(": read by the LLM fallback")).toBeTruthy();
    expect(intents.getByText("Out of stock")).toBeTruthy();

    // Rejections, grouped by reason, most frequent first.
    const rejections = within(screen.getByRole("region", { name: "Rejected comments" }));
    const items = rejections.getAllByRole("listitem").map((item) => item.textContent);
    expect(items).toEqual(["No purchase intent2", "Ambiguous, not resolved1"]);
  });

  it("applies each event as it arrives", async () => {
    const { api, stream } = liveApi();
    renderApp(`/sessions/${SESSION_ID}/live`, api);
    await expect.poll(() => stream.opened.length).toBe(1);
    stream.send("snapshot", snapshot(), "1-0");
    await screen.findByRole("heading", { name: "Friday drop", level: 1 });

    stream.send(
      "stock.updated",
      {
        type: "stock.updated",
        session_id: SESSION_ID,
        occurred_at: "2026-10-02T10:00:01Z",
        correlation_id: "c-1",
        data: { product_id: PRODUCT.id, stock_total: 10, reserved: 4, sold: 2, available: 4 },
      },
      "2-0",
    );
    stream.send(
      "order.updated",
      {
        type: "order.updated",
        session_id: SESSION_ID,
        occurred_at: "2026-10-02T10:00:01Z",
        correlation_id: "c-1",
        data: { ...anOrder(4), shipment: undefined },
      },
      "3-0",
    );

    expect(await screen.findByRole("rowheader", { name: "LC-000004" })).toBeTruthy();
    expect(within(row(/^A12/)).getAllByRole("cell")[0]?.textContent).toContain("4");
  });

  it("says when the session is not the seller's", async () => {
    const api = signedInApi().on(
      "GET",
      EVENTS,
      Response.json({ detail: "Not found" }, { status: 404 }),
    );

    renderApp(`/sessions/${SESSION_ID}/live`, api);

    expect((await screen.findByRole("alert")).textContent).toBe(
      "This session does not exist, or it is not one of yours.",
    );
  });

  it("opens an order from the view, its shipment read from the stream", async () => {
    const { api, stream } = liveApi();
    api.on("GET", "/api/v1/orders/LC-000003", () =>
      Response.json({
        reference: "LC-000003",
        status: "paid",
        product: { code: "A12", name: "Linen shirt", variant: "" },
        quantity: 1,
        total_minor: 2990,
        currency: "EUR",
        reserved_until: "2026-10-02T10:15:00Z",
      }),
    );
    const { user, router } = renderApp(`/sessions/${SESSION_ID}/live`, api);
    await expect.poll(() => stream.opened.length).toBe(1);
    stream.send("snapshot", snapshot(), "1-0");

    await user.click(await screen.findByRole("link", { name: "LC-000003" }));

    expect(await screen.findByRole("heading", { name: "Order LC-000003" })).toBeTruthy();
    expect(router.state.location.pathname).toBe("/orders/LC-000003");
    expect(screen.getByText("In transit")).toBeTruthy();
    expect(screen.getByText("MOCK-7K3F9Q")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Friday drop" }).getAttribute("href")).toBe(
      `/sessions/${SESSION_ID}/live`,
    );
    // Leaving the view closed its stream.
    expect(stream.opened[0]?.request.signal.aborted).toBe(true);
  });

  it("answers an unknown reference on the order page", async () => {
    const api = signedInApi();

    renderApp("/orders/LC-ZZZZZZ", api);

    expect((await screen.findByRole("alert")).textContent).toBe("No order has this reference.");
  });
});
