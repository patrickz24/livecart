import {
  putShippingAddress,
  startCheckout,
  type Client,
  type ShippingAddress,
} from "@livecart/api-client";
import * as WebBrowser from "expo-web-browser";

import { unwrap } from "../api/errors";

import { parsePaymentReturn, type PaymentOutcome } from "./paymentReturn";

/**
 * Records the address, then opens the order's checkout: both requests carry
 * the order's token in their Authorization header, and the body of the
 * second only the app's return link. Answers the provider's hosted page.
 */
export async function openCheckoutSession({
  client,
  reference,
  token,
  address,
  returnUrl,
}: {
  client: Client;
  reference: string;
  token: string;
  address: ShippingAddress;
  returnUrl: string;
}): Promise<string> {
  await unwrap(putShippingAddress({ client, path: { reference }, auth: token, body: address }));
  const checkout = await unwrap(
    startCheckout({ client, path: { reference }, auth: token, body: { return_url: returnUrl } }),
  );
  return checkout.redirect_url;
}

/** How the buyer left the provider's page: through the bridge page's link, or not. */
export type PaymentSessionResult =
  { type: "returned"; status: PaymentOutcome; reference: string } | { type: "closed" };

/**
 * The provider's page in an auth session: an ephemeral one on iOS
 * (ASWebAuthenticationSession, no cookie shared with Safari), a Custom Tab on
 * Android. The session ends when the bridge page's button opens `returnUrl`,
 * or when the buyer closes it.
 */
export async function payInBrowser(
  redirectUrl: string,
  returnUrl: string,
): Promise<PaymentSessionResult> {
  const result = await WebBrowser.openAuthSessionAsync(redirectUrl, returnUrl, {
    preferEphemeralSession: true,
  });
  if (result.type === "success") {
    const returned = parsePaymentReturn(result.url);
    if (returned !== null) {
      return { type: "returned", ...returned };
    }
  }
  return { type: "closed" };
}
