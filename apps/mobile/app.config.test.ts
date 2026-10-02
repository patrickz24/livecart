import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "@jest/globals";
import type { ConfigContext } from "expo/config";

import appConfig, { BrandConfigError, loadBrand } from "./app.config";

const PROJECT_ROOT = __dirname;
const BRANDS = path.join(PROJECT_ROOT, "brands");
const DEMO_FILES = ["brand.json", "logo.png", "icon.png", "adaptive-icon.png", "splash-icon.png"];

function context(projectRoot = PROJECT_ROOT): ConfigContext {
  return { projectRoot, staticConfigPath: null, packageJsonPath: null, config: {} };
}

function withBrand(brandId: string | undefined, run: () => void): void {
  const previous = process.env.APP_BRAND;
  if (brandId === undefined) {
    delete process.env.APP_BRAND;
  } else {
    process.env.APP_BRAND = brandId;
  }
  try {
    run();
  } finally {
    if (previous === undefined) {
      delete process.env.APP_BRAND;
    } else {
      process.env.APP_BRAND = previous;
    }
  }
}

describe("the app config", () => {
  it("builds the demo brand when APP_BRAND is unset", () => {
    withBrand(undefined, () => {
      const config = appConfig(context());

      expect(config).toMatchObject({
        name: "LiveCart Demo",
        slug: "livecart-demo",
        scheme: "livecart",
        icon: "./brands/demo/icon.png",
        ios: { bundleIdentifier: "com.example.livecart.demo" },
        android: {
          package: "com.example.livecart.demo",
          adaptiveIcon: {
            foregroundImage: "./brands/demo/adaptive-icon.png",
            backgroundColor: "#1F6FEB",
          },
        },
        experiments: { typedRoutes: true },
        extra: { brand: { id: "demo", sellerSlug: "demo-seller" } },
      });
    });
  });

  it("builds the brand APP_BRAND names, with its own scheme, ids and seller", () => {
    withBrand("pomelune", () => {
      const config = appConfig(context());

      expect(config).toMatchObject({
        name: "Pomelune",
        scheme: "samplelive",
        ios: { bundleIdentifier: "com.example.livecart.pomelune" },
        android: { package: "com.example.livecart.pomelune" },
        extra: { brand: { id: "pomelune", sellerSlug: "sample-shop" } },
      });
      expect(config.plugins).toContainEqual([
        "expo-splash-screen",
        { image: "./brands/pomelune/splash-icon.png", imageWidth: 200, backgroundColor: "#6B2E8F" },
      ]);
    });
  });

  it("refuses an unknown brand and names the known ones", () => {
    withBrand("acme", () => {
      expect(() => appConfig(context())).toThrow(BrandConfigError);
      expect(() => appConfig(context())).toThrow(
        /unknown brand: no brands\/acme\/brand.json \(APP_BRAND\); known brands: demo, pomelune/,
      );
    });
  });

  it("refuses a brand name that would leave the brands' folder", () => {
    expect(() => loadBrand("../brands/demo", BRANDS)).toThrow(/unknown brand/);
  });
});

describe("a brand folder", () => {
  let brands: string;
  let folder: string;

  beforeEach(() => {
    brands = mkdtempSync(path.join(tmpdir(), "livecart-brands-"));
    folder = path.join(brands, "demo");
    mkdirSync(folder);
    for (const file of DEMO_FILES) {
      copyFileSync(path.join(BRANDS, "demo", file), path.join(folder, file));
    }
  });

  afterEach(() => {
    rmSync(brands, { recursive: true, force: true });
  });

  function editBrandJson(edit: (brand: Record<string, unknown>) => void): void {
    const file = path.join(folder, "brand.json");
    const brand = JSON.parse(readFileSync(file, "utf8")) as Record<string, unknown>;
    edit(brand);
    writeFileSync(file, JSON.stringify(brand));
  }

  it("loads when complete", () => {
    expect(loadBrand("demo", brands).displayName).toBe("LiveCart Demo");
  });

  it("is refused when a field is missing, with the field's name", () => {
    editBrandJson((brand) => {
      delete brand.scheme;
    });

    expect(() => loadBrand("demo", brands)).toThrow(/- scheme: /);
  });

  it("is refused when its logo is missing", () => {
    rmSync(path.join(folder, "logo.png"));

    expect(() => loadBrand("demo", brands)).toThrow(
      /- assets\.logo: brands\/demo\/logo\.png is missing/,
    );
  });

  it("is refused with every problem of brand.json at once", () => {
    editBrandJson((brand) => {
      brand.colors = { primary: "blue", onPrimary: "#FFFFFF", accent: "#F2994A" };
      brand.android = { package: "com.example.live-cart" };
    });

    const problems = (() => {
      try {
        loadBrand("demo", brands);
        return "";
      } catch (error) {
        return String(error);
      }
    })();
    expect(problems).toMatch(/- colors\.primary: must be a color as #RRGGBB/);
    expect(problems).toMatch(/- android\.package: must be an Android package/);
  });

  it("is refused with a field it does not know", () => {
    editBrandJson((brand) => {
      brand.colour = "#000000";
    });

    expect(() => loadBrand("demo", brands)).toThrow(/colour/);
  });

  it("is refused when its id is not its folder's name", () => {
    editBrandJson((brand) => {
      brand.id = "other";
    });

    expect(() => loadBrand("demo", brands)).toThrow(
      /- id: is other, but the folder is brands\/demo/,
    );
  });

  it("is refused when an asset names a path rather than a file of the folder", () => {
    editBrandJson((brand) => {
      brand.assets = {
        logo: "../pomelune/logo.png",
        icon: "icon.png",
        adaptiveIcon: "adaptive-icon.png",
        splash: "splash-icon.png",
      };
    });

    expect(() => loadBrand("demo", brands)).toThrow(/- assets\.logo: must be the name of a PNG/);
  });

  it("is refused when the API could never send the buyer back to its scheme", () => {
    editBrandJson((brand) => {
      brand.scheme = "https";
    });

    expect(() => loadBrand("demo", brands)).toThrow(/- scheme: is no app's scheme/);
  });

  it("is refused when brand.json is not JSON", () => {
    writeFileSync(path.join(folder, "brand.json"), "{ id: demo }");

    expect(() => loadBrand("demo", brands)).toThrow(/brand\.json is not valid JSON/);
  });
});
