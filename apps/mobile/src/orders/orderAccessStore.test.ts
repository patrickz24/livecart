import { describe, expect, it } from "@jest/globals";

import {
  MemorySecureStorage,
  SECURE_STORE_VALUE_LIMIT,
  referenceAt,
  tokenAt,
} from "../test/memoryStorage";

import {
  INDEX_KEY,
  InvalidOrderAccess,
  MAX_STORED_ORDERS,
  createOrderAccessStore,
  tokenKey,
} from "./orderAccessStore";

function storeOnMemory() {
  const storage = new MemorySecureStorage();
  return { storage, store: createOrderAccessStore(storage) };
}

describe("the order access store", () => {
  it("keeps each token under its own key, and the references in an index", async () => {
    const { storage, store } = storeOnMemory();

    await store.save("LC-7K3F9Q", tokenAt(1));
    await store.save("LC-8M2P4R", tokenAt(2));

    expect(await store.references()).toEqual(["LC-8M2P4R", "LC-7K3F9Q"]);
    expect(await store.tokenFor("LC-7K3F9Q")).toBe(tokenAt(1));
    expect(storage.values.get("order_LC-7K3F9Q")).toBe(tokenAt(1));
    expect(storage.values.get(INDEX_KEY)).toBe('["LC-8M2P4R","LC-7K3F9Q"]');
  });

  it("stores a reference as typed in its canonical form", async () => {
    const { store } = storeOnMemory();

    expect(await store.save(" lc7k3f9q ", tokenAt(1))).toBe("LC-7K3F9Q");
    // O reads as 0, I and L as 1, as the API reads them.
    expect(await store.save("LC-O1IL2Z", tokenAt(2))).toBe("LC-01112Z");
    expect(await store.tokenFor("lc-7k3f9q")).toBe(tokenAt(1));
  });

  it("moves an order saved again to the front, with its new token, once", async () => {
    const { store } = storeOnMemory();
    await store.save("LC-7K3F9Q", tokenAt(1));
    await store.save("LC-8M2P4R", tokenAt(2));

    await store.save("LC-7K3F9Q", tokenAt(3));

    expect(await store.references()).toEqual(["LC-7K3F9Q", "LC-8M2P4R"]);
    expect(await store.tokenFor("LC-7K3F9Q")).toBe(tokenAt(3));
  });

  it("holds 50 orders, under SecureStore's value limit, and forgets the oldest for the 51st", async () => {
    const { storage, store } = storeOnMemory();
    for (let index = 0; index < MAX_STORED_ORDERS; index += 1) {
      await store.save(referenceAt(index), tokenAt(index));
    }

    const fifty = await store.references();
    expect(fifty).toHaveLength(50);
    expect(fifty[0]).toBe(referenceAt(49));
    expect(fifty.at(-1)).toBe(referenceAt(0));
    const indexBytes = storage.values.get(INDEX_KEY)?.length ?? 0;
    expect(indexBytes).toBeLessThan(SECURE_STORE_VALUE_LIMIT);
    expect(indexBytes).toBe(2 + 50 * 12 - 1);

    await store.save(referenceAt(50), tokenAt(50));

    const references = await store.references();
    expect(references).toHaveLength(50);
    expect(references[0]).toBe(referenceAt(50));
    expect(references).not.toContain(referenceAt(0));
    expect(await store.tokenFor(referenceAt(0))).toBeNull();
    expect(storage.values.has(tokenKey(referenceAt(0)))).toBe(false);
    expect(await store.tokenFor(referenceAt(1))).toBe(tokenAt(1));
    // 50 tokens and the index: nothing else is left behind.
    expect(storage.values.size).toBe(51);
  });

  it("keeps every order of saves made at once", async () => {
    const { store } = storeOnMemory();

    await Promise.all(
      Array.from({ length: 10 }, (_, index) => store.save(referenceAt(index), tokenAt(index))),
    );

    expect([...(await store.references())].sort()).toEqual(
      Array.from({ length: 10 }, (_, index) => referenceAt(index)).sort(),
    );
  });

  it("refuses what is no reference or no token, and stores nothing", async () => {
    const { storage, store } = storeOnMemory();

    await expect(store.save("LC-7K3F9", tokenAt(1))).rejects.toThrow(InvalidOrderAccess);
    // U is no symbol of the alphabet.
    await expect(store.save("LC-7K3F9U", tokenAt(1))).rejects.toThrow(InvalidOrderAccess);
    await expect(store.save("LC-7K3F9Q", "short")).rejects.toThrow(
      "This is not an order's access token.",
    );
    await expect(store.save("LC-7K3F9Q", `${tokenAt(1).slice(1)}=`)).rejects.toThrow(
      InvalidOrderAccess,
    );
    expect(storage.values.size).toBe(0);
  });

  it("forgets one order, its token and its place", async () => {
    const { storage, store } = storeOnMemory();
    await store.save("LC-7K3F9Q", tokenAt(1));
    await store.save("LC-8M2P4R", tokenAt(2));

    await store.remove("LC-7K3F9Q");

    expect(await store.references()).toEqual(["LC-8M2P4R"]);
    expect(storage.values.has("order_LC-7K3F9Q")).toBe(false);
  });

  it("reads a damaged index as what it still holds of references", async () => {
    const { storage, store } = storeOnMemory();
    storage.values.set(INDEX_KEY, '["LC-7K3F9Q", 42, "../x", "LC-7K3F9Q"]');

    expect(await store.references()).toEqual(["LC-7K3F9Q"]);

    storage.values.set(INDEX_KEY, "not json");
    expect(await store.references()).toEqual([]);
  });
});
