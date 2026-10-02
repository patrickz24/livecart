import { renderRouter, type MockContextConfig } from "expo-router/testing-library";
import type { ReactNode } from "react";

import { BrandProvider } from "../brand/brandTheme";

import { testBrandTheme } from "./brands";

export interface RenderRoutesOptions {
  /** The brand of brands/ the app is built for; the demo by default. */
  brandId?: string;
}

/**
 * Renders `routes` (route path to screen, as expo-router's testing library
 * takes them) at `initialUrl`, inside the providers the root layout gives
 * the app.
 */
export function renderRoutes(
  routes: MockContextConfig,
  initialUrl: string,
  { brandId = "demo" }: RenderRoutesOptions = {},
) {
  const theme = testBrandTheme(brandId);
  function Providers({ children }: { children: ReactNode }) {
    return <BrandProvider theme={theme}>{children}</BrandProvider>;
  }
  return renderRouter(routes, { initialUrl, wrapper: Providers });
}
