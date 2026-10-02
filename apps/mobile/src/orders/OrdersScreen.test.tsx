import { describe, expect, it } from "@jest/globals";
import { fireEvent, screen, waitFor } from "expo-router/testing-library";

import { FakeApi } from "../test/fakeApi";
import { tokenAt } from "../test/memoryStorage";
import { memoryOrderStore, renderRoutes } from "../test/renderRoutes";

describe("my orders", () => {
  it("lists the orders of this phone, newest first, without asking the API", async () => {
    const { store } = memoryOrderStore();
    await store.save("LC-7K3F9Q", tokenAt(1));
    await store.save("LC-8M2P4R", tokenAt(2));
    const api = new FakeApi();

    await renderRoutes("/orders", { api, store });

    expect(await screen.findByText("LC-8M2P4R")).toBeTruthy();
    const shown = screen.getAllByText(/^LC-/).map((text) => text.props.children as string);
    expect(shown).toEqual(["LC-8M2P4R", "LC-7K3F9Q"]);
    expect(api.requests).toEqual([]);
  });

  it("explains how an order arrives, and leads to the form", async () => {
    const app = await renderRoutes("/orders");

    expect(await screen.findByText("No orders yet")).toBeTruthy();
    await fireEvent.press(screen.getByRole("button", { name: "Add an order" }));

    await waitFor(() => {
      expect(app.getPathname()).toBe("/orders/add");
    });
  });
});
