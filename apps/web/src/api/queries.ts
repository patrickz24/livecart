import {
  getLiveSession,
  listProducts,
  type Client,
  type ProductPage,
  type ProductResponse,
} from "@livecart/api-client";
import { queryOptions } from "@tanstack/react-query";

import { unwrap } from "./errors";

/** The keys of the query cache, in one place so that writers and readers agree. */
export const keys = {
  me: ["me"] as const,
  sessions: ["sessions"] as const,
  session: (sessionId: string) => ["session", sessionId] as const,
  products: (sessionId: string) => ["session", sessionId, "products"] as const,
  live: (sessionId: string) => ["live", sessionId] as const,
  order: (reference: string) => ["order", reference] as const,
};

/** The API's largest page: a session holds up to 200 products, two pages at most. */
const PAGE_SIZE = 100;

export function sessionQuery(client: Client, sessionId: string) {
  return queryOptions({
    queryKey: keys.session(sessionId),
    queryFn: ({ signal }) =>
      unwrap(getLiveSession({ client, path: { session_id: sessionId }, signal })),
  });
}

export function productsQuery(client: Client, sessionId: string) {
  return queryOptions({
    queryKey: keys.products(sessionId),
    queryFn: async ({ signal }) => {
      const products: ProductResponse[] = [];
      let cursor: string | null = null;
      do {
        const page: ProductPage = await unwrap(
          listProducts({
            client,
            path: { session_id: sessionId },
            query: { limit: PAGE_SIZE, cursor },
            signal,
          }),
        );
        products.push(...page.items);
        cursor = page.next_cursor;
      } while (cursor !== null);
      return products;
    },
  });
}
