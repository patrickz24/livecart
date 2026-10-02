import type { SecureStorage } from "../orders/orderAccessStore";

/** The largest value expo-secure-store documents as safe on iOS. */
export const SECURE_STORE_VALUE_LIMIT = 2048;

/**
 * SecureStore in memory, with its rules: a key of letters, digits, `.`, `-`
 * and `_` only, and a value of 2048 bytes at most, refused otherwise.
 */
export class MemorySecureStorage implements SecureStorage {
  readonly values = new Map<string, string>();

  getItemAsync(key: string): Promise<string | null> {
    return Promise.resolve(this.values.get(checkedKey(key)) ?? null);
  }

  setItemAsync(key: string, value: string): Promise<void> {
    if (new TextEncoder().encode(value).length > SECURE_STORE_VALUE_LIMIT) {
      return Promise.reject(
        new Error(`the value of ${key} is over ${String(SECURE_STORE_VALUE_LIMIT)} bytes`),
      );
    }
    this.values.set(checkedKey(key), value);
    return Promise.resolve();
  }

  deleteItemAsync(key: string): Promise<void> {
    this.values.delete(checkedKey(key));
    return Promise.resolve();
  }
}

function checkedKey(key: string): string {
  if (!/^[\w.-]+$/.test(key)) {
    throw new Error(`${key} is no SecureStore key`);
  }
  return key;
}

const CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

/** The reference at `index`, as the API encodes them: 0 gives LC-000000. */
export function referenceAt(index: number): string {
  let rest = index;
  let symbols = "";
  for (let position = 0; position < 6; position += 1) {
    symbols = (CROCKFORD[rest % 32] ?? "0") + symbols;
    rest = Math.floor(rest / 32);
  }
  return `LC-${symbols}`;
}

/** A token of the right shape (43 base64url characters), different for each `index`. */
export function tokenAt(index: number): string {
  return `tok_${String(index)}`.padEnd(43, "x");
}
