import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import * as WebBrowser from "expo-web-browser";
import { fireEvent, screen, waitFor } from "expo-router/testing-library";

import { FakeApi, json } from "../test/fakeApi";
import { tokenAt } from "../test/memoryStorage";
import { ADDRESS, orderDetails } from "../test/orders";
import { memoryOrderStore, renderRoutes } from "../test/renderRoutes";

import { paymentReturnUrl } from "./paymentReturn";

// The auth session is the one native part of the flow: its result is scripted.
jest.mock("expo-web-browser", () => ({
  ...jest.requireActual<typeof WebBrowser>("expo-web-browser"),
  openAuthSessionAsync: jest.fn(),
}));
const openAuthSession = jest.mocked(WebBrowser.openAuthSessionAsync);

const ORDER = "/api/v1/orders/LC-7K3F9Q";
const HOSTED_PAGE = "http://api.test/api/v1/mock-payments/mock_cs_1";
const TOKEN = tokenAt(1);

function checkoutApi() {
  return new FakeApi()
    .on("GET", `${ORDER}/details`, json(200, orderDetails()))
    .on("PUT", `${ORDER}/shipping-address`, (request) =>
      json(200, orderDetails({ shipping_address: request.body as typeof ADDRESS })),
    )
    .on("POST", `${ORDER}/checkout`, json(200, { redirect_url: HOSTED_PAGE, attempt_no: 1 }));
}

async function renderPay(api: FakeApi) {
  const { store } = memoryOrderStore();
  await store.save("LC-7K3F9Q", TOKEN);
  return renderRoutes("/orders/LC-7K3F9Q/pay", { api, store });
}

async function fillAddress() {
  await fireEvent.changeText(await screen.findByLabelText("Full name"), ADDRESS.full_name);
  await fireEvent.changeText(screen.getByLabelText("Address"), ADDRESS.line1);
  await fireEvent.changeText(screen.getByLabelText("Postal code"), ADDRESS.postal_code);
  await fireEvent.changeText(screen.getByLabelText("City"), ADDRESS.city);
}

describe("paying for an order", () => {
  beforeEach(() => {
    openAuthSession.mockReset();
  });

  it("records the address, opens the checkout in an auth session, then follows the return", async () => {
    const api = checkoutApi();
    openAuthSession.mockResolvedValue({
      type: "success",
      url: `${paymentReturnUrl()}?status=success&reference=LC-7K3F9Q`,
    });

    const app = await renderPay(api);
    await fillAddress();
    await fireEvent.press(screen.getByRole("button", { name: /^Pay / }));

    await waitFor(() => {
      expect(app.getPathname()).toBe("/payment-return");
    });
    const [address] = api.sent("PUT", `${ORDER}/shipping-address`);
    expect(address?.body).toEqual({ ...ADDRESS, line2: "" });
    const [checkout] = api.sent("POST", `${ORDER}/checkout`);
    expect(checkout?.body).toEqual({ return_url: paymentReturnUrl() });
    expect(openAuthSession).toHaveBeenCalledWith(HOSTED_PAGE, paymentReturnUrl(), {
      preferEphemeralSession: true,
    });
    // The token rode in the Authorization header of each request, and nowhere else.
    for (const request of api.requests) {
      expect(request.headers.get("Authorization")).toBe(`Bearer ${TOKEN}`);
      expect(request.path).not.toContain(TOKEN);
      expect(JSON.stringify(request.body ?? null)).not.toContain(TOKEN);
    }
    expect(JSON.stringify(openAuthSession.mock.calls)).not.toContain(TOKEN);
    expect(app.getPathnameWithParams()).not.toContain(TOKEN);
  });

  it("says so when the buyer closes the payment page without returning", async () => {
    const api = checkoutApi();
    openAuthSession.mockResolvedValue({ type: WebBrowser.WebBrowserResultType.DISMISS });

    const app = await renderPay(api);
    await fillAddress();
    await fireEvent.press(screen.getByRole("button", { name: /^Pay / }));

    expect(await screen.findByText("The payment page was closed")).toBeTruthy();
    expect(app.getPathname()).toBe("/orders/LC-7K3F9Q/pay");
  });

  it("shows the API's refusal of the checkout", async () => {
    const api = checkoutApi().on(
      "POST",
      `${ORDER}/checkout`,
      json(409, { detail: "The order has used all its payment attempts" }),
    );

    await renderPay(api);
    await fillAddress();
    await fireEvent.press(screen.getByRole("button", { name: /^Pay / }));

    expect(await screen.findByText("The order has used all its payment attempts")).toBeTruthy();
    expect(openAuthSession).not.toHaveBeenCalled();
  });

  it("asks for the missing fields before any request", async () => {
    const api = checkoutApi();

    await renderPay(api);
    await screen.findByLabelText("Full name");
    await fireEvent.press(screen.getByRole("button", { name: /^Pay / }));

    expect(await screen.findAllByText("Required.")).toHaveLength(4);
    expect(api.sent("PUT", `${ORDER}/shipping-address`)).toEqual([]);
  });

  it("has nothing to pay for an order already paid", async () => {
    const api = new FakeApi().on(
      "GET",
      `${ORDER}/details`,
      json(200, orderDetails({ status: "paid" })),
    );

    await renderPay(api);

    expect(await screen.findByText("This order is paid")).toBeTruthy();
  });
});
