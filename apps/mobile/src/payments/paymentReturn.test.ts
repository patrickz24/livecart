import { describe, expect, it, jest } from "@jest/globals";
import * as Linking from "expo-linking";

import { confirmationDelays, parsePaymentReturn, paymentReturnUrl } from "./paymentReturn";

// expo-linking builds the app's links from the bundle's manifest, which a unit test has not.
jest.mock("expo-linking", () => ({ createURL: jest.fn((path: string) => `livecart://${path}`) }));

describe("the payment return link", () => {
  it("is the app's own link to payment-return, without query or fragment", () => {
    expect(paymentReturnUrl()).toBe("livecart://payment-return");
    expect(jest.mocked(Linking.createURL)).toHaveBeenCalledWith("payment-return");
  });

  it("reads the outcome and the reference the bridge page adds", () => {
    expect(
      parsePaymentReturn("livecart://payment-return?status=success&reference=LC-7K3F9Q"),
    ).toEqual({ status: "success", reference: "LC-7K3F9Q" });
    expect(
      parsePaymentReturn(
        "exp://192.168.1.20:8081/--/payment-return?status=cancel&reference=lc7k3f9q",
      ),
    ).toEqual({ status: "cancel", reference: "LC-7K3F9Q" });
  });

  it("refuses a link without a known outcome or a reference", () => {
    expect(parsePaymentReturn("livecart://payment-return")).toBeNull();
    expect(
      parsePaymentReturn("livecart://payment-return?status=paid&reference=LC-7K3F9Q"),
    ).toBeNull();
    expect(parsePaymentReturn("livecart://payment-return?status=success")).toBeNull();
  });
});

describe("the confirmation schedule", () => {
  it("waits 3 s, then 1.5 times longer, 10 s at most, for 60 s in all", () => {
    const delays = confirmationDelays();

    expect(delays).toEqual([3000, 4500, 6750, 10000, 10000, 10000, 10000, 5750]);
    expect(delays.reduce((sum, delay) => sum + delay, 0)).toBe(60_000);
  });
});
