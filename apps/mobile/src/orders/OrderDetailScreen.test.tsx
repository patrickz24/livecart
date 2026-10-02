import { describe, expect, it } from "@jest/globals";
import { fireEvent, screen, waitFor } from "expo-router/testing-library";

import { FakeApi, json } from "../test/fakeApi";
import { tokenAt } from "../test/memoryStorage";
import { ADDRESS, orderDetails, tracking } from "../test/orders";
import { memoryOrderStore, renderRoutes } from "../test/renderRoutes";
import { formatMoney } from "../ui/format";

const DETAILS = "/api/v1/orders/LC-7K3F9Q/details";
const TRACKING = "/api/v1/orders/LC-7K3F9Q/tracking";

async function storedOrder() {
  const { storage, store } = memoryOrderStore();
  await store.save("LC-7K3F9Q", tokenAt(1));
  return { storage, store };
}

describe("an order's detail", () => {
  it("shows the order, asked for with its token in the Authorization header", async () => {
    const { store } = await storedOrder();
    const api = new FakeApi().on(
      "GET",
      DETAILS,
      json(200, orderDetails({ quantity: 2, total_minor: 5000 })),
    );

    await renderRoutes("/orders/LC-7K3F9Q", { api, store });

    expect(await screen.findByText("Awaiting payment")).toBeTruthy();
    expect(screen.getByText("A12 M · Linen shirt")).toBeTruthy();
    expect(screen.getByText(`2 × ${formatMoney(2500, "EUR")}`)).toBeTruthy();
    expect(screen.getByText(formatMoney(5000, "EUR"))).toBeTruthy();
    expect(api.sent("GET", DETAILS)[0]?.headers.get("Authorization")).toBe(`Bearer ${tokenAt(1)}`);
    // Not paid: nothing to track yet.
    expect(api.sent("GET", TRACKING)).toEqual([]);
  });

  it("leads an order awaiting its payment to the payment", async () => {
    const { store } = await storedOrder();
    const api = new FakeApi().on("GET", DETAILS, json(200, orderDetails()));

    const app = await renderRoutes("/orders/LC-7K3F9Q", { api, store });
    await fireEvent.press(
      await screen.findByRole("button", { name: `Pay ${formatMoney(2500, "EUR")}` }),
    );

    await waitFor(() => {
      expect(app.getPathname()).toBe("/orders/LC-7K3F9Q/pay");
    });
  });

  it("follows the parcel of a paid order", async () => {
    const { store } = await storedOrder();
    const api = new FakeApi()
      .on(
        "GET",
        DETAILS,
        json(
          200,
          orderDetails({
            status: "paid",
            paid_at: "2026-10-02T18:05:00Z",
            shipping_address: ADDRESS,
          }),
        ),
      )
      .on("GET", TRACKING, json(200, tracking({ status: "out_for_delivery" })));

    await renderRoutes("/orders/LC-7K3F9Q", { api, store });

    expect(await screen.findByText("MOCK · MOCK-7K3F9Q2B")).toBeTruthy();
    expect(screen.getByText("Paid")).toBeTruthy();
    expect(screen.getByText("Camille Martin")).toBeTruthy();
    expect(screen.getByText("69003 Lyon, FR")).toBeTruthy();
    expect(screen.getAllByText("Out for delivery")).toHaveLength(1);
    expect(screen.getByText("● Out for delivery")).toBeTruthy();
    expect(screen.getByText("○ Delivered")).toBeTruthy();
    expect(api.sent("GET", TRACKING)[0]?.headers.get("Authorization")).toBe(`Bearer ${tokenAt(1)}`);
  });

  it("says a paid order has not shipped yet while the API has no shipment for it", async () => {
    const { store } = await storedOrder();
    const api = new FakeApi()
      .on("GET", DETAILS, json(200, orderDetails({ status: "paid" })))
      .on("GET", TRACKING, json(404, { detail: "Not found" }));

    await renderRoutes("/orders/LC-7K3F9Q", { api, store });

    expect(await screen.findByText("Not shipped yet: the parcel is being prepared.")).toBeTruthy();
  });

  it("says when the server is unavailable, and loads the order when asked again", async () => {
    const { store } = await storedOrder();
    const api = new FakeApi().on(
      "GET",
      DETAILS,
      json(503, { detail: "Database unavailable" }, { "Retry-After": "1" }),
    );

    await renderRoutes("/orders/LC-7K3F9Q", { api, store });

    expect(await screen.findByText("The order cannot be loaded")).toBeTruthy();
    expect(
      screen.getByText("The server is unavailable right now. Try again in a moment."),
    ).toBeTruthy();
    // Unavailable is not unknown: the order stays on this phone.
    expect(screen.queryByRole("button", { name: "Remove it from this phone" })).toBeNull();
    expect(await store.tokenFor("LC-7K3F9Q")).toBe(tokenAt(1));

    api.on("GET", DETAILS, json(200, orderDetails()));
    await fireEvent.press(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByText("Awaiting payment")).toBeTruthy();
  });

  it("asks nothing of the API for an order this phone has no token for", async () => {
    const api = new FakeApi();

    await renderRoutes("/orders/LC-7K3F9Q", { api });

    expect(await screen.findByText("This order is not on this phone")).toBeTruthy();
    expect(api.requests).toEqual([]);
  });

  it("offers to forget an order the API no longer knows with its token", async () => {
    const { storage, store } = await storedOrder();
    const api = new FakeApi().on("GET", DETAILS, json(404, { detail: "Not found" }));

    const app = await renderRoutes("/orders/LC-7K3F9Q", { api, store });
    expect(await screen.findByText("This order cannot be found")).toBeTruthy();
    await fireEvent.press(screen.getByRole("button", { name: "Remove it from this phone" }));

    await waitFor(() => {
      expect(app.getPathname()).toBe("/orders");
    });
    expect(storage.values.has("order_LC-7K3F9Q")).toBe(false);
    expect(await store.references()).toEqual([]);
  });
});
