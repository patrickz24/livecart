import { createClient, createConfig, type Client } from "@livecart/api-client";

/**
 * The API's address in this bundle, from EXPO_PUBLIC_API_URL, which Expo
 * inlines when it bundles (apps/mobile/.env, see .env.example): nginx on the
 * development PC, as the phone reaches it on the LAN, such as
 * http://192.168.1.20:8080. Null when it is unset or no http(s) URL.
 */
export function readApiBaseUrl(value: string | undefined): string | null {
  const text = value?.trim() ?? "";
  if (!/^https?:\/\/[^\s/?#]+(?::\d+)?\/?$/.test(text)) {
    return null;
  }
  return text.replace(/\/$/, "");
}

// Read as `process.env.EXPO_PUBLIC_API_URL`, literally: Expo inlines that form only.
export const API_BASE_URL = readApiBaseUrl(process.env.EXPO_PUBLIC_API_URL);

/**
 * A client of the generated SDK for `baseUrl`. The order token of a request
 * is passed to that request alone (`auth`), never to the client, and only
 * ever travels in its Authorization header.
 */
export function createApiClient(baseUrl: string, fetchImplementation?: typeof fetch): Client {
  return createClient(
    createConfig({
      baseUrl,
      ...(fetchImplementation === undefined ? {} : { fetch: fetchImplementation }),
    }),
  );
}
