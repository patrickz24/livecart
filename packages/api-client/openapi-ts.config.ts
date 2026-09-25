import { defineConfig } from "@hey-api/openapi-ts";

// Input: the schema exported from the FastAPI app, never edited by hand.
// Output: src/generated, committed and checked for drift in CI. Regenerate both
// from the repository root with `pnpm run gen-api`.
export default defineConfig({
  input: "./openapi.json",
  output: { path: "./src/generated" },
  plugins: ["@hey-api/typescript", "@hey-api/sdk", "@hey-api/client-fetch"],
});
