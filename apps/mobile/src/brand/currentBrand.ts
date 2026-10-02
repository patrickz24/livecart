import logo from "@brand/logo";
import Constants from "expo-constants";

import { brandSchema } from "../../brand.schema";

import { brandTheme, type BrandTheme } from "./brandTheme";

/**
 * The brand this build is for: app.config.ts validated it and put it in
 * `extra.brand`; it is parsed again here, so that a bundle that somehow
 * lacks it stops at startup instead of showing another brand. Its logo is
 * `@brand/logo`, which metro.config.js resolves to that brand's file.
 */
export function currentBrandTheme(): BrandTheme {
  const extra: Record<string, unknown> | undefined = Constants.expoConfig?.extra;
  return brandTheme(brandSchema.parse(extra?.brand), logo);
}
