import { QueryClient } from "@tanstack/react-query";
import { Stack } from "expo-router";
import { renderRouter, type MockContextConfig } from "expo-router/testing-library";
import type { ReactNode } from "react";

import { ApiProvider } from "../api/ApiProvider";
import { createApiClient } from "../api/client";
import TabsLayout from "../app/(tabs)/_layout";
import StorefrontRoute from "../app/(tabs)/index";
import OrdersRoute from "../app/(tabs)/orders";
import OrderLinkRoute from "../app/link/[reference]";
import OrderDetailRoute from "../app/orders/[reference]/index";
import PayRoute from "../app/orders/[reference]/pay";
import AddOrderRoute from "../app/orders/add";
import PaymentReturnRoute from "../app/payment-return";
import { BrandProvider } from "../brand/brandTheme";
import { OrderAccessProvider } from "../orders/OrderAccessProvider";
import { createOrderAccessStore, type OrderAccessStore } from "../orders/orderAccessStore";

import { testBrandTheme } from "./brands";
import { FAKE_API_ORIGIN, FakeApi } from "./fakeApi";
import { MemorySecureStorage } from "./memoryStorage";

/** The app's routes, under a plain stack (the real root layout reads the build's brand). */
export const APP_ROUTES = {
  _layout: () => <Stack />,
  "(tabs)/_layout": TabsLayout,
  "(tabs)/index": StorefrontRoute,
  "(tabs)/orders": OrdersRoute,
  "orders/add": AddOrderRoute,
  "orders/[reference]/index": OrderDetailRoute,
  "orders/[reference]/pay": PayRoute,
  "link/[reference]": OrderLinkRoute,
  "payment-return": PaymentReturnRoute,
} satisfies MockContextConfig;

export interface RenderRoutesOptions {
  /** The brand of brands/ the app is built for; the demo by default. */
  brandId?: string;
  /** The API the app talks to; one without any answer by default. */
  api?: FakeApi;
  /** The order tokens of the phone; an empty store on an in-memory SecureStore by default. */
  store?: OrderAccessStore;
}

/** A store of order tokens on an in-memory SecureStore, with that storage to look into. */
export function memoryOrderStore() {
  const storage = new MemorySecureStorage();
  return { storage, store: createOrderAccessStore(storage) };
}

/**
 * Renders the app's routes (or `routes`) at `initialUrl`, inside the
 * providers the root layout gives the app, on a fake API, an in-memory
 * SecureStore and a query cache that never retries.
 */
export async function renderRoutes(
  initialUrl: string,
  {
    brandId = "demo",
    api = new FakeApi(),
    store = memoryOrderStore().store,
  }: RenderRoutesOptions = {},
  routes: MockContextConfig = APP_ROUTES,
) {
  const theme = testBrandTheme(brandId);
  const client = createApiClient(FAKE_API_ORIGIN, api.fetch);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function Providers({ children }: { children: ReactNode }) {
    return (
      <BrandProvider theme={theme}>
        <ApiProvider client={client} queryClient={queryClient}>
          <OrderAccessProvider store={store}>{children}</OrderAccessProvider>
        </ApiProvider>
      </BrandProvider>
    );
  }
  // renderRouter adds its helpers to the promise of RNTL 14's async render, not to its result.
  const rendering = renderRouter(routes, { initialUrl, wrapper: Providers });
  return Object.assign(await rendering, {
    getPathname: () => rendering.getPathname(),
    getPathnameWithParams: () => rendering.getPathnameWithParams(),
    getRouterState: () => rendering.getRouterState(),
  });
}
