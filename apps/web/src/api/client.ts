import { createClient, createConfig, type Client } from "@livecart/api-client";

/**
 * The dashboard's API client. Requests go to the page's own origin: the Vite
 * dev server, and nginx in compose, forward API paths to FastAPI, so the
 * browser never makes a cross-origin call. `fetch` is injectable for tests.
 */
export function createApiClient(options: { fetch?: typeof fetch } = {}): Client {
  return createClient(createConfig({ baseUrl: window.location.origin, ...options }));
}
