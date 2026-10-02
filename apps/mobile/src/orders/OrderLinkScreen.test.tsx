import { describe, expect, it } from "@jest/globals";
import { fireEvent, screen, waitFor } from "expo-router/testing-library";

import { FakeApi, json } from "../test/fakeApi";
import { orderDetails } from "../test/orders";
import { referenceAt, tokenAt } from "../test/memoryStorage";
import { memoryOrderStore, renderRoutes } from "../test/renderRoutes";

import { INDEX_KEY, MAX_STORED_ORDERS } from "./orderAccessStore";

const DETAILS = "/api/v1/orders/LC-7K3F9Q/details";
const UNKNOWN = "The server knows no order with this link's reference and token.";

describe("a buyer link", () => {
  it("checks the token of its #t= fragment with the API, keeps it in SecureStore, then leaves the link behind", async () => {
    const { storage, store } = memoryOrderStore();
    const token = tokenAt(7);
    const api = new FakeApi().on("GET", DETAILS, json(200, orderDetails()));

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
    expect(api.requests.length).toBeGreaterThan(0);
    for (const request of api.requests) {
      expect(request.path).toBe(DETAILS);
      expect(request.headers.get("Authorization")).toBe(`Bearer ${token}`);
    }
  });

  it("reads its reference as the API does", async () => {
    const { store } = memoryOrderStore();
    const api = new FakeApi().on("GET", DETAILS, json(200, orderDetails()));

    const app = await renderRoutes(`/link/lc7k3f9q#t=${tokenAt(1)}`, { api, store });

    await waitFor(() => {
      expect(app.getPathname()).toBe("/orders/LC-7K3F9Q");
    });
    expect(await store.references()).toEqual(["LC-7K3F9Q"]);
  });

  it("keeps the token already stored when the API does not know the link's", async () => {
    const { store } = memoryOrderStore();
    await store.save("LC-7K3F9Q", tokenAt(1));
    const api = new FakeApi().on("GET", DETAILS, json(404, { detail: "Not found" }));

    await renderRoutes(`/link/LC-7K3F9Q#t=${tokenAt(9)}`, { api, store });

    expect(await screen.findByText(UNKNOWN)).toBeTruthy();
    expect(screen.getByText("This link cannot be opened")).toBeTruthy();
    expect(api.sent("GET", DETAILS)[0]?.headers.get("Authorization")).toBe(`Bearer ${tokenAt(9)}`);
    expect(await store.tokenFor("LC-7K3F9Q")).toBe(tokenAt(1));
    expect(await store.references()).toEqual(["LC-7K3F9Q"]);
  });

  it("pushes no order out of a full phone for a token the API does not know", async () => {
    const { storage, store } = memoryOrderStore();
    for (let index = 0; index < MAX_STORED_ORDERS; index += 1) {
      await store.save(referenceAt(index), tokenAt(index));
    }
    const before = await store.references();
    const api = new FakeApi().on("GET", DETAILS, json(404, { detail: "Not found" }));

    await renderRoutes(`/link/LC-7K3F9Q#t=${tokenAt(99)}`, { api, store });

    expect(await screen.findByText(UNKNOWN)).toBeTruthy();
    expect(await store.references()).toEqual(before);
    expect(await store.tokenFor(referenceAt(0))).toBe(tokenAt(0));
    expect(await store.tokenFor("LC-7K3F9Q")).toBeNull();
    expect(storage.values.size).toBe(MAX_STORED_ORDERS + 1);
  });

  it("stores nothing while the server is unavailable, then tries again", async () => {
    const { store } = memoryOrderStore();
    const api = new FakeApi().on(
      "GET",
      DETAILS,
      json(503, { detail: "Database unavailable" }, { "Retry-After": "1" }),
    );

    const app = await renderRoutes(`/link/LC-7K3F9Q#t=${tokenAt(3)}`, { api, store });

    expect(
      await screen.findByText("The server is unavailable right now. Try again in a moment."),
    ).toBeTruthy();
    expect(await store.references()).toEqual([]);

    api.on("GET", DETAILS, json(200, orderDetails()));
    await fireEvent.press(screen.getByRole("button", { name: "Try again" }));

    await waitFor(() => {
      expect(app.getPathname()).toBe("/orders/LC-7K3F9Q");
    });
    expect(await store.tokenFor("LC-7K3F9Q")).toBe(tokenAt(3));
  });

  it("is refused without a token, and stores nothing", async () => {
    const { storage, store } = memoryOrderStore();
    const api = new FakeApi();

    await renderRoutes("/link/LC-7K3F9Q", { api, store });

    expect(await screen.findByText("This link cannot be opened")).toBeTruthy();
    expect(storage.values.size).toBe(0);
    expect(api.requests).toEqual([]);
  });

  it("is refused with a token in its query rather than its fragment", async () => {
    const { storage, store } = memoryOrderStore();

    await renderRoutes(`/link/LC-7K3F9Q?t=${tokenAt(1)}`, { store });

    expect(await screen.findByText("This link cannot be opened")).toBeTruthy();
    expect(storage.values.size).toBe(0);
  });
});
