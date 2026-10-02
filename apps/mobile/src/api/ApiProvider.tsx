import type { Client } from "@livecart/api-client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createContext, use, useState, type ReactNode } from "react";

import { ApiError, isRetryable } from "./errors";

const ApiClientContext = createContext<Client | null>(null);

// The longest a screen waits behind its spinner before trying a query again.
const MAX_RETRY_DELAY_MS = 10_000;

/**
 * The wait before the next try of a failed query: the Retry-After of a 503
 * when the API sent one, else React Query's own backoff (1 s, then 2 s);
 * 10 s at most either way.
 */
function retryDelay(failures: number, error: unknown): number {
  const delay =
    error instanceof ApiError && error.retryAfterSeconds !== null
      ? error.retryAfterSeconds * 1000
      : 1000 * 2 ** failures;
  return Math.min(delay, MAX_RETRY_DELAY_MS);
}

/**
 * A query cache for the app: a failure is retried twice, after the wait the
 * API asked for, unless the API refused the request.
 */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: (failures, error) => failures < 2 && isRetryable(error),
        retryDelay,
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
