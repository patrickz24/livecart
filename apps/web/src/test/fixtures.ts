import type { LiveSessionResponse, ProductResponse } from "@livecart/api-client";

export const SESSION_ID = "0199a000-0000-7000-8000-0000000000a1";

export function aSession(overrides: Partial<LiveSessionResponse> = {}): LiveSessionResponse {
  return {
    id: SESSION_ID,
    title: "Friday drop",
    platform: "simulator",
    external_live_id: "friday-drop",
    status: "draft",
    created_at: "2026-10-02T09:00:00Z",
    started_at: null,
    ended_at: null,
    ...overrides,
  };
}

export function aProduct(overrides: Partial<ProductResponse> = {}): ProductResponse {
  return {
    id: "0199a000-0000-7000-8000-0000000000b1",
    session_id: SESSION_ID,
    code: "A12",
    variant: "",
    name: "Linen shirt",
    price_minor: 2990,
    currency: "EUR",
    stock_total: 10,
    reserved: 2,
    sold: 1,
    available: 7,
    ...overrides,
  };
}

/** FastAPI's 422 for one field of the body. */
export function validationError(field: string, msg: string): Response {
  return Response.json(
    { detail: [{ loc: ["body", field], msg, type: "value_error" }] },
    { status: 422 },
  );
}
