import type { Client } from "@livecart/api-client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createContext, use, useState, type ReactNode } from "react";

import { isRetryable } from "./errors";

const ApiClientContext = createContext<Client | null>(null);

/** A query cache for the app: a failure is retried twice, unless the API refused the request. */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: (failures, error) => failures < 2 && isRetryable(error),
        staleTime: 10_000,
      },
    },
  });
}

/** The API client and the query cache the screens use. */
export function ApiProvider({
  client,
  queryClient,
  children,
}: {
  client: Client;
  queryClient?: QueryClient;
  children: ReactNode;
}) {
  const [cache] = useState(() => queryClient ?? createQueryClient());
  return (
    <ApiClientContext value={client}>
      <QueryClientProvider client={cache}>{children}</QueryClientProvider>
    </ApiClientContext>
  );
}

export function useApiClient(): Client {
  const client = use(ApiClientContext);
  if (client === null) {
    throw new Error("useApiClient needs an ApiProvider above it");
  }
  return client;
}
