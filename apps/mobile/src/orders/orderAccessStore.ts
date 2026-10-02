import * as SecureStore from "expo-secure-store";

import { isOrderToken, parseOrderReference } from "./reference";

/**
 * The buyer's orders on this phone: the access token of each, in SecureStore
 * (the Keychain on iOS, Keystore-encrypted storage on Android), never in
 * plain storage and never in a URL. A token leaves the store for one place
 * only, the Authorization header of a request about its own order.
 *
 * - One entry per order, named `order_` then its reference, whose value is
 *   its token.
 * - One index key, `order_index`: the stored references as a JSON array,
 *   most recently added first. 50 references take about 600 bytes, under the
 *   2 KB a SecureStore value may hold on iOS; adding a 51st forgets the
 *   oldest order, token included.
 * - Writes run one after another, so that two orders added at once both
 *   stay in the index.
 */

export const MAX_STORED_ORDERS = 50;
export const INDEX_KEY = "order_index";

export function tokenKey(reference: string): string {
  return `order_${reference}`;
}

/** The part of expo-secure-store the store uses; tests pass an in-memory one. */
export interface SecureStorage {
  getItemAsync(key: string, options?: SecureStore.SecureStoreOptions): Promise<string | null>;
  setItemAsync(key: string, value: string, options?: SecureStore.SecureStoreOptions): Promise<void>;
  deleteItemAsync(key: string, options?: SecureStore.SecureStoreOptions): Promise<void>;
}

/** A reference or a token that is not one: nothing was stored. */
export class InvalidOrderAccess extends Error {
  constructor(message: string) {
    super(message);
    this.name = "InvalidOrderAccess";
  }
}

export interface OrderAccessStore {
  /** The stored references, most recently added first. */
  references(): Promise<string[]>;
  /** The token of `reference`, or null when this phone has none. */
  tokenFor(reference: string): Promise<string | null>;
  /**
   * Stores the token of `reference` (read tolerantly, stored canonical) and
   * puts the reference first; answers the canonical reference.
   */
  save(reference: string, token: string): Promise<string>;
  /** Forgets an order: its token and its place in the index. */
  remove(reference: string): Promise<void>;
}

// The tokens never leave this device: no iCloud Keychain sync, no backup restored
// elsewhere (on Android, expo-secure-store's plugin keeps them out of backups).
const OPTIONS: SecureStore.SecureStoreOptions = {
  keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
};

function parseIndex(value: string | null): string[] {
  if (value === null) {
    return [];
  }
  try {
    const parsed: unknown = JSON.parse(value);
    if (!Array.isArray(parsed)) {
      return [];
    }
    // Whatever is not a canonical reference is dropped: a damaged index loses nothing else.
    const references = parsed.filter(
      (item): item is string => typeof item === "string" && parseOrderReference(item) === item,
    );
    return [...new Set(references)];
  } catch {
    return [];
  }
}

export function createOrderAccessStore(storage: SecureStorage = SecureStore): OrderAccessStore {
  let writes: Promise<unknown> = Promise.resolve();
  function serialized<T>(task: () => Promise<T>): Promise<T> {
    const result = writes.then(task, task);
    writes = result.catch(() => undefined);
    return result;
  }

  const readIndex = async () => parseIndex(await storage.getItemAsync(INDEX_KEY, OPTIONS));
  const writeIndex = (references: readonly string[]) =>
    storage.setItemAsync(INDEX_KEY, JSON.stringify(references), OPTIONS);

  return {
    references: readIndex,

    async tokenFor(reference) {
      const canonical = parseOrderReference(reference);
      if (canonical === null) {
        return null;
      }
      const token = await storage.getItemAsync(tokenKey(canonical), OPTIONS);
      return token !== null && isOrderToken(token) ? token : null;
    },

    save(reference, token) {
      const canonical = parseOrderReference(reference);
      if (canonical === null) {
        return Promise.reject(new InvalidOrderAccess("This is not an order reference."));
      }
      const trimmed = token.trim();
      if (!isOrderToken(trimmed)) {
        return Promise.reject(new InvalidOrderAccess("This is not an order's access token."));
      }
      return serialized(async () => {
        // The token first: the index never names an order whose token is not stored.
        await storage.setItemAsync(tokenKey(canonical), trimmed, OPTIONS);
        const others = (await readIndex()).filter((stored) => stored !== canonical);
        const kept = [canonical, ...others].slice(0, MAX_STORED_ORDERS);
        await writeIndex(kept);
        for (const forgotten of others.slice(MAX_STORED_ORDERS - 1)) {
          await storage.deleteItemAsync(tokenKey(forgotten), OPTIONS);
        }
        return canonical;
      });
    },

    remove(reference) {
      const canonical = parseOrderReference(reference);
      if (canonical === null) {
        return Promise.resolve();
      }
      return serialized(async () => {
        await writeIndex((await readIndex()).filter((stored) => stored !== canonical));
        await storage.deleteItemAsync(tokenKey(canonical), OPTIONS);
      });
    },
  };
}
