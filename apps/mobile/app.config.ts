/**
 * The app's Expo configuration, evaluated by the Expo CLI and EAS before each
 * start, export and build: one white-label build per brand.
 *
 * APP_BRAND names the brand (each EAS profile sets it; `demo` when unset),
 * whose folder brands/<id>/ holds brand.json and its images. Everything is
 * checked before Expo uses any of it, and any problem fails the build with
 * every reason: an unknown brand, a brand.json that is not JSON or does not
 * match brand.schema.ts (a field missing, unknown or malformed), a folder
 * whose name is not the brand's id, or an image the brand names but its
 * folder lacks. The brand then reaches the app as `extra.brand`.
 */
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";

import type { ConfigContext, ExpoConfig } from "expo/config";

import { BRAND_ASSETS, brandSchema, describeIssues, type Brand } from "./brand.schema.ts";

export const DEFAULT_BRAND = "demo";

/** A brand the build cannot use; the message lists every reason. */
export class BrandConfigError extends Error {
  constructor(brandId: string, reasons: readonly string[]) {
    super(`Brand "${brandId}" cannot be built:\n${reasons.map((r) => `  - ${r}`).join("\n")}`);
    this.name = "BrandConfigError";
  }
}

/** The brands of `brandsDirectory`: its folders that hold a brand.json. */
export function knownBrands(brandsDirectory: string): string[] {
  if (!existsSync(brandsDirectory)) {
    return [];
  }
  return readdirSync(brandsDirectory, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .filter((name) => existsSync(path.join(brandsDirectory, name, "brand.json")))
    .sort();
}

/** The brand `brandId` of `brandsDirectory`, validated with its images; `BrandConfigError` if not. */
export function loadBrand(brandId: string, brandsDirectory: string): Brand {
  const folder = path.join(brandsDirectory, brandId);
  const file = path.join(folder, "brand.json");
  // A name such as ../x would leave the brands' folder: only a plain folder name is a brand.
  if (!/^[a-z0-9]+$/.test(brandId) || !existsSync(file)) {
    const known = knownBrands(brandsDirectory);
    throw new BrandConfigError(brandId, [
      `unknown brand: no brands/${brandId}/brand.json (APP_BRAND); known brands: ${known.join(", ") || "none"}`,
    ]);
  }
  let json: unknown;
  try {
    json = JSON.parse(readFileSync(file, "utf8"));
  } catch (error) {
    throw new BrandConfigError(brandId, [`brand.json is not valid JSON: ${String(error)}`]);
  }
  const parsed = brandSchema.safeParse(json);
  if (!parsed.success) {
    throw new BrandConfigError(brandId, describeIssues(parsed.error));
  }
  const brand = parsed.data;
  const reasons: string[] = [];
  if (brand.id !== brandId) {
    reasons.push(`id: is ${brand.id}, but the folder is brands/${brandId}`);
  }
  for (const asset of BRAND_ASSETS) {
    const image = path.join(folder, brand.assets[asset]);
    if (!existsSync(image) || !statSync(image).isFile()) {
      reasons.push(`assets.${asset}: brands/${brandId}/${brand.assets[asset]} is missing`);
    }
  }
  if (reasons.length > 0) {
    throw new BrandConfigError(brandId, reasons);
  }
  return brand;
}

/** The Expo configuration of one brand, over the base `config` Expo passes in. */
export function brandExpoConfig(brand: Brand, config: Partial<ExpoConfig> = {}): ExpoConfig {
  const asset = (name: (typeof BRAND_ASSETS)[number]) =>
    `./brands/${brand.id}/${brand.assets[name]}`;
  return {
    ...config,
    name: brand.displayName,
    slug: brand.slug,
    scheme: brand.scheme,
    version: "0.1.0",
    platforms: ["android", "ios"],
    orientation: "portrait",
    userInterfaceStyle: "light",
    icon: asset("icon"),
    ios: { bundleIdentifier: brand.ios.bundleIdentifier, supportsTablet: false },
    android: {
      package: brand.android.package,
      adaptiveIcon: {
        foregroundImage: asset("adaptiveIcon"),
        backgroundColor: brand.colors.primary,
      },
    },
    plugins: [
      "expo-router",
      "expo-secure-store",
      "expo-web-browser",
      [
        "expo-splash-screen",
        { image: asset("splash"), imageWidth: 200, backgroundColor: brand.colors.primary },
      ],
    ],
    experiments: { typedRoutes: true },
    extra: {
      brand,
      ...(brand.easProjectId === undefined ? {} : { eas: { projectId: brand.easProjectId } }),
    },
  };
}

export default function appConfig({ config, projectRoot }: ConfigContext): ExpoConfig {
  const brandId = process.env.APP_BRAND ?? DEFAULT_BRAND;
  return brandExpoConfig(loadBrand(brandId, path.join(projectRoot, "brands")), config);
}
