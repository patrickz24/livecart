import { describe, expect, it } from "@jest/globals";
import type { OrderDetailsResponse } from "@livecart/api-client";
import { fireEvent, screen, waitFor } from "expo-router/testing-library";

import { FakeApi, json } from "../test/fakeApi";
import { tokenAt } from "../test/memoryStorage";
import { memoryOrderStore, renderRoutes } from "../test/renderRoutes";

const DETAILS: OrderDetailsResponse = {
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
};

async function fill(reference: string, token?: string) {
  await fireEvent.changeText(await screen.findByLabelText("Reference"), reference);
  if (token !== undefined) {
    await fireEvent.changeText(screen.getByLabelText("Token"), token);
  }
  await fireEvent.press(screen.getByRole("button", { name: "Add the order" }));
}

describe("adding an order", () => {
  it("checks the pair with the API, the token in the header only, then stores it", async () => {
    const token = tokenAt(5);
    const api = new FakeApi().on("GET", "/api/v1/orders/LC-7K3F9Q/details", json(200, DETAILS));
    const { store } = memoryOrderStore();

    const app = await renderRoutes("/orders/add", { api, store });
    await fill("lc-7k3f9q", token);

    await waitFor(() => {
      expect(app.getPathname()).toBe("/orders");
    });
    expect(await store.tokenFor("LC-7K3F9Q")).toBe(token);
    const [request] = api.sent("GET", "/api/v1/orders/LC-7K3F9Q/details");
    expect(request?.headers.get("Authorization")).toBe(`Bearer ${token}`);
    expect(api.requests.every((sent) => !sent.path.includes(token))).toBe(true);
  });

  it("fills both fields from a whole pasted link", async () => {
    const token = tokenAt(6);
    const api = new FakeApi().on("GET", "/api/v1/orders/LC-7K3F9Q/details", json(200, DETAILS));
    const { store } = memoryOrderStore();

    const app = await renderRoutes("/orders/add", { api, store });
    await fill(`livecart://link/LC-7K3F9Q#t=${token}`);

    await waitFor(() => {
      expect(app.getPathname()).toBe("/orders");
    });
    expect(await store.tokenFor("LC-7K3F9Q")).toBe(token);
  });

  it("stores nothing when the API knows no such order with this token", async () => {
    const api = new FakeApi().on(
      "GET",
      "/api/v1/orders/LC-7K3F9Q/details",
      json(404, { detail: "Not found" }),
    );
    const { storage, store } = memoryOrderStore();

    await renderRoutes("/orders/add", { api, store });
    await fill("LC-7K3F9Q", tokenAt(5));

    expect(
      await screen.findByText(
        "No order has this reference and token. Check both, as you received them.",
      ),
    ).toBeTruthy();
    expect(storage.values.size).toBe(0);
  });

  it("asks for a reference and a token of the right shape before any request", async () => {
    const api = new FakeApi();

    await renderRoutes("/orders/add", { api });
    await fill("LC-123", "abc");

    expect(
      await screen.findByText("A reference is LC- then six letters or digits, such as LC-7K3F9Q."),
    ).toBeTruthy();
    expect(
      screen.getByText("The token is the 43-character code you received with the reference."),
    ).toBeTruthy();
    expect(api.requests).toEqual([]);
  });
});
