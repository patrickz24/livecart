import { describe, expect, it, jest } from "@jest/globals";
import type { OrderStatus } from "@livecart/api-client";
import { act, screen } from "expo-router/testing-library";

import { FakeApi, json } from "../test/fakeApi";
import { tokenAt } from "../test/memoryStorage";
import { orderDetails } from "../test/orders";
import { memoryOrderStore, renderRoutes } from "../test/renderRoutes";

const DETAILS = "/api/v1/orders/LC-7K3F9Q/details";
const SUCCESS = "/payment-return?status=success&reference=LC-7K3F9Q";

/** An API whose order is pending for the first `pendingReads` reads, then has `then`. */
function apiPaidAfter(pendingReads: number, then: OrderStatus = "paid") {
  let reads = 0;
  return new FakeApi().on("GET", DETAILS, () => {
    reads += 1;
    return json(200, orderDetails({ status: reads > pendingReads ? then : "pending_payment" }));
  });
}

async function storeWithOrder() {
  const { store } = memoryOrderStore();
  await store.save("LC-7K3F9Q", tokenAt(1));
  return store;
}

/** Lets the fake clock run `ms`, with the reads it triggers answered on the way. */
async function wait(ms: number) {
  await act(async () => {
    await jest.advanceTimersByTimeAsync(ms);
  });
}

describe("the payment return", () => {
  it("reads the order's status with its token until the payment is confirmed", async () => {
    const api = apiPaidAfter(2);

    await renderRoutes(SUCCESS, { api, store: await storeWithOrder() });

    expect(await screen.findByText("Confirming the payment of LC-7K3F9Q…")).toBeTruthy();
    expect(api.sent("GET", DETAILS)).toHaveLength(1);
    await wait(3_000);
    expect(api.sent("GET", DETAILS)).toHaveLength(2);
    expect(screen.queryByText("Payment confirmed")).toBeNull();
    await wait(4_500);

    expect(await screen.findByText("Payment confirmed")).toBeTruthy();
    expect(screen.getByText("Order LC-7K3F9Q is paid: it ships soon.")).toBeTruthy();
    const reads = api.sent("GET", DETAILS);
    expect(reads).toHaveLength(3);
    expect(
      reads.every((read) => read.headers.get("Authorization") === `Bearer ${tokenAt(1)}`),
    ).toBe(true);
    // Confirmed: no more reads.
    await wait(30_000);
    expect(api.sent("GET", DETAILS)).toHaveLength(3);
  });

  it("stops after 60 s, on a payment still pending, without asking again", async () => {
    const api = apiPaidAfter(Number.POSITIVE_INFINITY);

    await renderRoutes(SUCCESS, { api, store: await storeWithOrder() });
    await screen.findByText("Confirming the payment of LC-7K3F9Q…");
    await wait(59_000);
    expect(screen.queryByText("Still being confirmed")).toBeNull();
    await wait(1_000);

    expect(await screen.findByText("Still being confirmed")).toBeTruthy();
    // The first read, then one after each of the 8 waits of the schedule.
    expect(api.sent("GET", DETAILS)).toHaveLength(9);
    await wait(120_000);
    expect(api.sent("GET", DETAILS)).toHaveLength(9);
  });

  it("says when the reservation ended before the payment", async () => {
    const api = apiPaidAfter(0, "expired");

    await renderRoutes(SUCCESS, { api, store: await storeWithOrder() });

    expect(await screen.findByText("The reservation has ended")).toBeTruthy();
  });

  it("says a cancelled payment left the order unpaid, without asking the API", async () => {
    const api = new FakeApi();

    await renderRoutes("/payment-return?status=cancel&reference=LC-7K3F9Q", {
      api,
      store: await storeWithOrder(),
    });

    expect(await screen.findByText("Payment cancelled")).toBeTruthy();
    expect(api.requests).toEqual([]);
  });

  it("asks nothing for an order this phone has no token for", async () => {
    const api = new FakeApi();

    await renderRoutes(SUCCESS, { api });

    expect(await screen.findByText("This order is not on this phone")).toBeTruthy();
    expect(api.requests).toEqual([]);
  });
});
