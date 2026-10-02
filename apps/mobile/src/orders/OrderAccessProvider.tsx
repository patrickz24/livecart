import { useQuery } from "@tanstack/react-query";
import { createContext, use, useState, type ReactNode } from "react";

import { createOrderAccessStore, type OrderAccessStore } from "./orderAccessStore";

const OrderAccessContext = createContext<OrderAccessStore | null>(null);

/** The query key of the stored references, which every change to the store invalidates. */
export const storedOrdersKey = ["stored-orders"] as const;

/** The query key of one order's token, under `storedOrdersKey`. */
export const orderTokenKey = (reference: string | null) =>
  [...storedOrdersKey, reference, "token"] as const;

/** The store of the buyer's order tokens: SecureStore's, unless a test passes another. */
export function OrderAccessProvider({
  store,
  children,
}: {
  store?: OrderAccessStore;
  children: ReactNode;
}) {
  const [orders] = useState(() => store ?? createOrderAccessStore());
  return <OrderAccessContext value={orders}>{children}</OrderAccessContext>;
}

export function useOrderAccess(): OrderAccessStore {
  const store = use(OrderAccessContext);
  if (store === null) {
    throw new Error("useOrderAccess needs an OrderAccessProvider above it");
  }
  return store;
}

/** The references stored on this phone, most recently added first. */
export function useStoredReferences() {
  const store = useOrderAccess();
  return useQuery({ queryKey: storedOrdersKey, queryFn: () => store.references() });
}

/**
 * The token of `reference` on this phone, read from SecureStore, or null. It
 * stays in memory (the query cache, which nothing persists) while a screen
 * uses it, and goes to the Authorization header of that order's requests.
 */
export function useOrderToken(reference: string | null) {
  const store = useOrderAccess();
  return useQuery({
    queryKey: orderTokenKey(reference),
    queryFn: () => (reference === null ? null : store.tokenFor(reference)),
  });
}
