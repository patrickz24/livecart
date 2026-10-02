import { createClient, createConfig, type Client } from "@livecart/api-client";

import { createAuthFetch } from "../auth/authFetch";
import type { AuthSession } from "../auth/session";

/** The OpenAPI security scheme of the seller routes; the order token's is another. */
export const SELLER_SCHEME = "SellerAccessToken";

/**
 * The dashboard's API client. Requests go to the page's own origin: the Vite
 * dev server, and nginx in compose, forward API paths to FastAPI, so the
 * browser never makes a cross-origin call, sends the `lc_refresh` cookie
 * (SameSite=Strict) to the auth routes, and adds the `Origin` header their
 * check reads. The seller's access token goes on the routes of the seller
 * scheme only, never on a buyer route's order-token scheme.
 */
export function createApiClient(options: {
  session: AuthSession;
  send: (request: Request) => Promise<Response>;
}): Client {
  const { session, send } = options;
  return createClient(
    createConfig({
      baseUrl: window.location.origin,
      fetch: createAuthFetch(session, send),
      auth: async (auth) =>
        auth.key === SELLER_SCHEME ? ((await session.validToken()) ?? undefined) : undefined,
    }),
  );
}
