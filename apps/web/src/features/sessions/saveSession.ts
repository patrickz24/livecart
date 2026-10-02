import {
  createLiveSession,
  createProduct,
  restockProduct,
  updateLiveSession,
  updateProduct,
  type Client,
  type LiveSessionCreate,
  type LiveSessionResponse,
  type LiveSessionStatus,
  type LiveSessionUpdate,
  type ProductCreate,
  type ProductResponse,
  type ProductUpdate,
} from "@livecart/api-client";

import { unwrap } from "../../api/errors";
import type { SaveStep, SessionFormOutput } from "./sessionSchema";

/** What the API holds already: the baseline each save compares the form with. */
export interface SavedState {
  session: LiveSessionResponse | null;
  products: Map<string, ProductResponse>;
}

export interface SaveCallbacks {
  /** A request succeeded: the caller records it, so a retry after a later failure skips it. */
  onSessionSaved(session: LiveSessionResponse): void;
  onProductSaved(index: number, product: ProductResponse): void;
  onRestocked(index: number, product: ProductResponse): void;
  /** The `Idempotency-Key` of a restock: the same for every retry of the same change. */
  restockKey(productId: string, delta: number): string;
}

/** A request of the save failed; the ones before it succeeded and were recorded. */
export class SaveFailed extends Error {
  readonly step: SaveStep;
  constructor(step: SaveStep, cause: unknown) {
    super("A request of the save failed", { cause });
    this.name = "SaveFailed";
    this.step = step;
  }
}

/** The session's changed fields, or `null`; the platform and live id only change in a draft. */
export function sessionChanges(
  saved: LiveSessionResponse,
  wanted: LiveSessionCreate,
): LiveSessionUpdate | null {
  const changes: LiveSessionUpdate = {};
  if (wanted.title !== saved.title) {
    changes.title = wanted.title;
  }
  if (saved.status === "draft") {
    if (wanted.platform !== saved.platform) {
      changes.platform = wanted.platform;
    }
    if (wanted.external_live_id !== saved.external_live_id) {
      changes.external_live_id = wanted.external_live_id;
    }
  }
  return Object.keys(changes).length === 0 ? null : changes;
}

/** A product's changed fields, or `null`; its code and variant only change in a draft. */
export function productChanges(
  saved: ProductResponse,
  wanted: ProductCreate,
  status: LiveSessionStatus,
): ProductUpdate | null {
  const changes: ProductUpdate = {};
  if (wanted.name !== saved.name) {
    changes.name = wanted.name;
  }
  if (wanted.price_minor !== saved.price_minor) {
    changes.price_minor = wanted.price_minor;
  }
  if (status === "draft") {
    if (wanted.code !== saved.code) {
      changes.code = wanted.code;
    }
    if ((wanted.variant ?? "") !== saved.variant) {
      changes.variant = wanted.variant ?? "";
    }
  }
  return Object.keys(changes).length === 0 ? null : changes;
}

/**
 * Save the form, one request at a time, in the order the API needs: the
 * session (created, or its changed fields), then each product row (created,
 * or its changed fields), then that row's restock through the idempotent
 * endpoint. Nothing unchanged is sent. The first failure stops the save and
 * says which step it was; everything before it is reported through the
 * callbacks, so that the next attempt neither creates a row twice nor adds a
 * restock twice (its key is the same, the API replays its first answer).
 */
export async function saveSession(
  client: Client,
  saved: SavedState,
  form: SessionFormOutput,
  callbacks: SaveCallbacks,
): Promise<LiveSessionResponse> {
  let session = saved.session;
  try {
    if (session === null) {
      session = await unwrap(createLiveSession({ client, body: form.session }));
    } else {
      const changes = sessionChanges(session, form.session);
      if (changes !== null) {
        session = await unwrap(
          updateLiveSession({ client, path: { session_id: session.id }, body: changes }),
        );
      }
    }
  } catch (error) {
    throw new SaveFailed({ kind: "session" }, error);
  }
  callbacks.onSessionSaved(session);
  const sessionId = session.id;

  for (const [index, row] of form.products.entries()) {
    let product = row.productId === null ? undefined : saved.products.get(row.productId);
    try {
      if (row.productId === null) {
        product = await unwrap(
          createProduct({ client, path: { session_id: sessionId }, body: row.fields }),
        );
      } else if (product !== undefined) {
        const changes = productChanges(product, row.fields, session.status);
        if (changes !== null) {
          product = await unwrap(
            updateProduct({
              client,
              path: { session_id: sessionId, product_id: product.id },
              body: changes,
            }),
          );
        }
      }
    } catch (error) {
      throw new SaveFailed({ kind: "product", index }, error);
    }
    if (product === undefined) {
      continue;
    }
    callbacks.onProductSaved(index, product);

    if (row.productId !== null && row.delta !== 0) {
      try {
        product = await unwrap(
          restockProduct({
            client,
            path: { session_id: sessionId, product_id: product.id },
            headers: { "Idempotency-Key": callbacks.restockKey(product.id, row.delta) },
            body: { delta: row.delta },
          }),
        );
      } catch (error) {
        throw new SaveFailed({ kind: "restock", index }, error);
      }
      callbacks.onRestocked(index, product);
    }
  }
  return session;
}

/**
 * A restock's `Idempotency-Key`: 128 random bits in hex, from
 * `crypto.getRandomValues`, which exists in insecure contexts too (unlike
 * `crypto.randomUUID`), such as plain HTTP on a LAN address.
 */
export function newIdempotencyKey(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}
