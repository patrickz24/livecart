// Public entry point. Everything under generated/ is rewritten by
// `pnpm run gen-api`; this file only chooses what the apps import: the SDK
// functions and schema types, plus the fetch client so that each app sets its
// own base URL (same origin on the web, EXPO_PUBLIC_API_URL on mobile).
export * from "./generated";
export { client } from "./generated/client.gen";
export { createClient, createConfig, type Client, type Config } from "./generated/client";
