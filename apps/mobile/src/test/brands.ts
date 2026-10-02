import path from "node:path";

import { knownBrands, loadBrand } from "../../app.config";
import { brandTheme, type BrandTheme } from "../brand/brandTheme";

export const BRANDS_DIRECTORY = path.join(__dirname, "..", "..", "brands");

/** Every brand of brands/, as app.config.ts finds them. */
export const BRAND_IDS = knownBrands(BRANDS_DIRECTORY);

/**
 * The theme of one brand of brands/, validated as a build validates it. Its
 * logo is a URI naming the brand's file, so that a snapshot shows whose logo
 * a screen draws (in a build, metro.config.js bundles the file itself).
 */
export function testBrandTheme(brandId = "demo"): BrandTheme {
  const brand = loadBrand(brandId, BRANDS_DIRECTORY);
  return brandTheme(brand, { uri: `brands/${brand.id}/${brand.assets.logo}` });
}
