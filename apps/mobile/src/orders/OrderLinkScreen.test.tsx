import { describe, expect, it } from "@jest/globals";
import { screen, waitFor } from "expo-router/testing-library";

import { FakeApi, json } from "../test/fakeApi";
import { orderDetails } from "../test/orders";
import { tokenAt } from "../test/memoryStorage";
import { memoryOrderStore, renderRoutes } from "../test/renderRoutes";

import { INDEX_KEY } from "./orderAccessStore";

describe("a buyer link", () => {
  it("keeps the token of its #t= fragment in SecureStore, then leaves the link behind", async () => {
    const { storage, store } = memoryOrderStore();
    const token = tokenAt(7);
    const api = new FakeApi().on(
      "GET",
      "/api/v1/orders/LC-7K3F9Q/details",
      json(200, orderDetails()),
    );

    const app = await renderRoutes(`/link/LC-7K3F9Q#t=${token}`, { api, store });

    await waitFor(() => {
      expect(app.getPathname()).toBe("/orders/LC-7K3F9Q");
    });
    expect(await screen.findByText("Awaiting payment")).toBeTruthy();
    expect(await store.tokenFor("LC-7K3F9Q")).toBe(token);
    expect(storage.values.get(INDEX_KEY)).toBe('["LC-7K3F9Q"]');
    // The token is nowhere in the navigation any more, and only ever in a header.
    expect(JSON.stringify(app.getRouterState())).not.toContain(token);
    expect(app.getPathnameWithParams()).not.toContain(token);
    expect(api.requests.map((request) => request.path)).toEqual([
      "/api/v1/orders/LC-7K3F9Q/details",
    ]);
    expect(api.requests[0]?.headers.get("Authorization")).toBe(`Bearer ${token}`);
  });

  it("reads its reference as the API does", async () => {
    const { store } = memoryOrderStore();

    const app = await renderRoutes(`/link/lc7k3f9q#t=${tokenAt(1)}`, { store });

    await waitFor(() => {
      expect(app.getPathname()).toBe("/orders/LC-7K3F9Q");
    });
    expect(await store.references()).toEqual(["LC-7K3F9Q"]);
  });

  it("is refused without a token, and stores nothing", async () => {
    const { storage, store } = memoryOrderStore();

    await renderRoutes("/link/LC-7K3F9Q", { store });

    expect(await screen.findByText("This link cannot be opened")).toBeTruthy();
    expect(storage.values.size).toBe(0);
  });

  it("is refused with a token in its query rather than its fragment", async () => {
    const { storage, store } = memoryOrderStore();

    await renderRoutes(`/link/LC-7K3F9Q?t=${tokenAt(1)}`, { store });

    expect(await screen.findByText("This link cannot be opened")).toBeTruthy();
    expect(storage.values.size).toBe(0);
  });
});
