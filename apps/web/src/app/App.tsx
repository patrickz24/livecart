import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import type { DataRouter } from "react-router";
import { RouterProvider } from "react-router/dom";

import { isRetryable } from "../api/errors";
import { ServicesProvider, useServices, type Services } from "./services";

/** The server state's one cache: queries, and the live view's events written into it. */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: (failures, error) => isRetryable(error) && failures < 2,
        staleTime: 10_000,
      },
    },
  });
}

/** A session that ends, by a logout or a refused refresh, leaves no data of its seller behind. */
function ClearCacheOnSignOut() {
  const { session } = useServices();
  const queryClient = useQueryClient();
  useEffect(
    () =>
      session.subscribe(() => {
        if (session.status === "signed_out") {
          queryClient.clear();
        }
      }),
    [session, queryClient],
  );
  return null;
}

export function App({
  services,
  queryClient,
  router,
}: {
  services: Services;
  queryClient: QueryClient;
  router: DataRouter;
}) {
  return (
    <ServicesProvider services={services}>
      <QueryClientProvider client={queryClient}>
        <ClearCacheOnSignOut />
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ServicesProvider>
  );
}
