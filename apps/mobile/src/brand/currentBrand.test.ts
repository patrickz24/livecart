import { afterEach, describe, expect, it, jest } from "@jest/globals";
import Constants from "expo-constants";
import { ZodError } from "zod";

import { loadBrand } from "../../app.config";
import { BRANDS_DIRECTORY } from "../test/brands";

import { currentBrandTheme } from "./currentBrand";

// The build's app config, as expo-constants reads it from the bundle: each test sets its own.
jest.mock("expo-constants", () => ({ __esModule: true, default: { expoConfig: null } }));

function buildWith(extra: Record<string, unknown>) {
  jest.replaceProperty(Constants, "expoConfig", { name: "LiveCart", slug: "livecart", extra });
}

/** A brand as the manifest carries it: app.config.ts's object, through JSON. */
function manifestBrand(brandId: string): unknown {
  return JSON.parse(JSON.stringify(loadBrand(brandId, BRANDS_DIRECTORY)));
}

afterEach(() => {
  jest.restoreAllMocks();
});

describe("the brand of the build", () => {
  it("is the one app.config.ts put in extra.brand, in its own colors", () => {
    buildWith({ brand: manifestBrand("pomelune") });

    const theme = currentBrandTheme();

    expect(theme.brand).toEqual(loadBrand("pomelune", BRANDS_DIRECTORY));
    expect(theme.color).toMatchObject({
      primary: "#6B2E8F",
      onPrimary: "#FFFFFF",
      accent: "#F2C14E",
    });
  });

  it("stops the app at startup when the bundle has no app config", () => {
    expect(() => currentBrandTheme()).toThrow(ZodError);
  });

  it("stops the app at startup when extra has no brand", () => {
    buildWith({ eas: { projectId: "00000000-0000-0000-0000-000000000000" } });

    expect(() => currentBrandTheme()).toThrow(ZodError);
  });

  it("stops the app at startup on a brand the schema refuses", () => {
    const brand = manifestBrand("demo") as { colors: Record<string, string> };
    buildWith({ brand: { ...brand, colors: { ...brand.colors, primary: "red" } } });

    expect(() => currentBrandTheme()).toThrow(ZodError);
  });
});
