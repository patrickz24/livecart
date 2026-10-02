import { QueryClient } from "@tanstack/react-query";
import { renderRouter, type MockContextConfig } from "expo-router/testing-library";
import type { ReactNode } from "react";

import { ApiProvider } from "../api/ApiProvider";
import { createApiClient } from "../api/client";
import { BrandProvider } from "../brand/brandTheme";

import { testBrandTheme } from "./brands";
import { FAKE_API_ORIGIN, FakeApi } from "./fakeApi";

export interface RenderRoutesOptions {
  /** The brand of brands/ the app is built for; the demo by default. */
  brandId?: string;
  /** The API the app talks to; one without any answer by default. */
  api?: FakeApi;
}

/**
 * Renders `routes` (route file to screen, as expo-router's testing library
 * takes them) at `initialUrl`, inside the providers the root layout gives
 * the app, on a fake API and a query cache that never retries.
 */
export function renderRoutes(
  routes: MockContextConfig,
  initialUrl: string,
  { brandId = "demo", api = new FakeApi() }: RenderRoutesOptions = {},
) {
  const theme = testBrandTheme(brandId);
  const client = createApiClient(FAKE_API_ORIGIN, api.fetch);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function Providers({ children }: { children: ReactNode }) {
    return (
      <BrandProvider theme={theme}>
        <ApiProvider client={client} queryClient={queryClient}>
          {children}
        </ApiProvider>
      </BrandProvider>
    );
  }
  return renderRouter(routes, { initialUrl, wrapper: Providers });
}
