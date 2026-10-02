// Not in src/app: expo-router would take any file there for a route.
import { afterAll, describe, expect, it, jest } from "@jest/globals";
import { render, screen } from "@testing-library/react-native";
import Constants from "expo-constants";

import { loadBrand } from "../app.config";

import type RootLayoutComponent from "./app/_layout";
import { BRANDS_DIRECTORY } from "./test/brands";

// The build's app config, as expo-constants reads it from the bundle.
jest.mock("expo-constants", () => ({ __esModule: true, default: { expoConfig: null } }));

const apiUrl = process.env.EXPO_PUBLIC_API_URL;

afterAll(() => {
  if (apiUrl === undefined) {
    delete process.env.EXPO_PUBLIC_API_URL;
  } else {
    process.env.EXPO_PUBLIC_API_URL = apiUrl;
  }
});

describe("the root layout", () => {
  it("says the bundle has no API address when EXPO_PUBLIC_API_URL is missing", async () => {
    delete process.env.EXPO_PUBLIC_API_URL;
    jest.replaceProperty(Constants, "expoConfig", {
      name: "LiveCart Demo",
      slug: "livecart-demo",
      extra: { brand: loadBrand("demo", BRANDS_DIRECTORY) },
    });
    // Loaded here rather than imported: it reads the brand and the API's address once, on load.
    const { default: RootLayout } = jest.requireActual<{ default: typeof RootLayoutComponent }>(
      "./app/_layout",
    );

    await render(<RootLayout />);

    expect(screen.getByText("No API address")).toBeTruthy();
    expect(
      screen.getByText(
        "This bundle has no EXPO_PUBLIC_API_URL: set it in apps/mobile/.env (see .env.example), then start Expo again.",
      ),
    ).toBeTruthy();
  });
});
