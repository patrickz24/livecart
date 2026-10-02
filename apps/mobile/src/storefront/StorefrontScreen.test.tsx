import { describe, expect, it } from "@jest/globals";
import type { StorefrontResponse } from "@livecart/api-client";
import { screen } from "expo-router/testing-library";

import { FakeApi, json } from "../test/fakeApi";
import { renderRoutes } from "../test/renderRoutes";
import { formatMoney } from "../ui/format";

const LIVE: StorefrontResponse = {
  slug: "sample-shop",
  display_name: "Sample Shop",
  live_session: {
    title: "Friday drop",
    platform: "simulator",
    started_at: "2026-10-02T18:00:00Z",
    products: [
      {
        code: "A12",
        variant: "M",
        name: "Linen shirt",
        price_minor: 2500,
        currency: "EUR",
        available: 2,
      },
      {
        code: "B07",
        variant: "",
        name: "Silk scarf",
        price_minor: 1990,
        currency: "EUR",
        available: 0,
      },
    ],
  },
};

describe("the live storefront", () => {
  it("shows the products of the brand's own seller", async () => {
    const api = new FakeApi().on("GET", "/api/v1/storefronts/sample-shop", json(200, LIVE));

    await renderRoutes("/", { brandId: "pomelune", api });

    expect(await screen.findByText("Linen shirt")).toBeTruthy();
    expect(screen.getByText("Pomelune")).toBeTruthy();
    expect(screen.getByText("Friday drop")).toBeTruthy();
    expect(screen.getByText("A12 M")).toBeTruthy();
    expect(screen.getByText("2 left")).toBeTruthy();
    expect(screen.getByText(formatMoney(2500, "EUR"))).toBeTruthy();
    expect(screen.getByText("B07")).toBeTruthy();
    expect(screen.getByText("Sold out")).toBeTruthy();
    expect(api.requests.map((request) => request.path)).toEqual([
      "/api/v1/storefronts/sample-shop",
    ]);
  });

  it("says the store is not found when the API does not know the brand's seller", async () => {
    const api = new FakeApi().on(
      "GET",
      "/api/v1/storefronts/demo-seller",
      json(404, { detail: "Not found" }),
    );

    await renderRoutes("/", { api });

    expect(await screen.findByText("Store not found")).toBeTruthy();
    expect(
      screen.getByText("This app's store, demo-seller, does not exist on this server."),
    ).toBeTruthy();
    expect(screen.queryByText("No live right now")).toBeNull();
  });

  it("says when the seller is not live", async () => {
    const api = new FakeApi().on(
      "GET",
      "/api/v1/storefronts/demo-seller",
      json(200, { ...LIVE, slug: "demo-seller", live_session: null }),
    );

    await renderRoutes("/", { api });

    expect(
      await screen.findByText("LiveCart Demo is not live at the moment. Pull down to check again."),
    ).toBeTruthy();
  });

  it("tells a server out of reach from a missing store", async () => {
    const api = new FakeApi().on("GET", "/api/v1/storefronts/demo-seller", () => {
      throw new TypeError("Network request failed");
    });

    await renderRoutes("/", { api });

    expect(await screen.findByText("The live cannot be loaded")).toBeTruthy();
    expect(
      screen.getByText("The server cannot be reached. Check your connection and try again."),
    ).toBeTruthy();
  });
});
