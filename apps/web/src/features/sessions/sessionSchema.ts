/**
 * The session form's values, their zod schema, and the way back from an API
 * error to the field it is about.
 *
 * The form holds strings, as inputs do; the schema checks them with the API's
 * own bounds and turns them into the request bodies, whose types are the
 * generated ones (`satisfies` below): a field renamed or added in the
 * contract stops this file from compiling. The API stays the judge (a code
 * already sold in the session is its 409), the schema only spares a round
 * trip for what a form can tell.
 */
import type {
  LiveSessionCreate,
  LiveSessionResponse,
  Platform,
  ProductCreate,
  ProductResponse,
} from "@livecart/api-client";
import { z } from "zod";

import { ApiError, describeError } from "../../api/errors";
import { PLATFORM_LABELS } from "../../strings";
import { inputToMinor, minorToInput } from "../../ui/format";

// The API's bounds (livecart_core.application.dto and domain.codes).
const MAX_TITLE_LENGTH = 200;
const MAX_LIVE_ID_LENGTH = 128;
const MAX_NAME_LENGTH = 200;
const MAX_VARIANT_LENGTH = 32;
const MAX_PRICE_MINOR = 100_000_000;
const MAX_STOCK_TOTAL = 1_000_000;
const PRODUCT_CODE = /^[A-Z]{1,3}[0-9]{1,4}$/;

export const PLATFORMS = Object.keys(PLATFORM_LABELS) as [Platform, ...Platform[]];

export interface ProductRowValues {
  /** The product's id once it exists; `null` for a row not saved yet. */
  productId: string | null;
  code: string;
  variant: string;
  name: string;
  /** In major units, as typed: "19.90". */
  price: string;
  /** A new row's stock total. */
  stock: string;
  /** A saved row's restock: units to add, or to remove when negative. */
  restock: string;
}

export interface SessionFormValues {
  title: string;
  platform: Platform;
  external_live_id: string;
  products: ProductRowValues[];
}

/** What a valid form sends: the generated request bodies, and each row's restock. */
export interface SessionFormOutput {
  session: LiveSessionCreate;
  products: { productId: string | null; fields: ProductCreate; delta: number }[];
}

/** Text as the API reads it: NFKC, then trimmed (domain.text.normalize_text). */
const normalized = () => z.string().transform((text) => text.normalize("NFKC").trim());

const units = (label: string, min: number, max: number) =>
  z
    .string()
    .trim()
    .regex(min < 0 ? /^([+-]?\d{1,7})?$/ : /^\d{1,7}$/, `Enter a whole number of ${label}.`)
    .transform((text) => (text === "" ? 0 : Number(text)))
    .pipe(
      z
        .number()
        .int()
        .min(min, `At least ${String(min)}.`)
        .max(max, `At most ${String(max)}.`),
    );

export const liveSessionFields = z.object({
  title: normalized().pipe(
    z
      .string()
      .min(1, "Enter a title.")
      .max(MAX_TITLE_LENGTH, `${String(MAX_TITLE_LENGTH)} characters at most.`),
  ),
  platform: z.enum(PLATFORMS),
  external_live_id: normalized().pipe(
    z
      .string()
      .min(1, "Enter the live's id on its platform.")
      .max(MAX_LIVE_ID_LENGTH, `${String(MAX_LIVE_ID_LENGTH)} characters at most.`),
  ),
}) satisfies z.ZodType<LiveSessionCreate>;

/** A product row; `currency` gives the price's minor units. */
export function productRow(currency: string) {
  return z
    .object({
      productId: z.string().nullable(),
      code: normalized()
        .transform((text) => text.toUpperCase())
        .pipe(z.string().regex(PRODUCT_CODE, "1 to 3 letters, then 1 to 4 digits: A12.")),
      variant: normalized().pipe(
        z.string().max(MAX_VARIANT_LENGTH, `${String(MAX_VARIANT_LENGTH)} characters at most.`),
      ),
      name: normalized().pipe(
        z
          .string()
          .min(1, "Enter a name.")
          .max(MAX_NAME_LENGTH, `${String(MAX_NAME_LENGTH)} characters at most.`),
      ),
      price: z
        .string()
        .transform((text) => inputToMinor(text, currency))
        .pipe(
          z
            .number("Enter a price such as 19.90.")
            .int()
            .max(MAX_PRICE_MINOR, "This price is too high."),
        ),
      stock: units("units", 0, MAX_STOCK_TOTAL),
      restock: units("units", -MAX_STOCK_TOTAL, MAX_STOCK_TOTAL),
    })
    .transform((row) => ({
      productId: row.productId,
      fields: {
        code: row.code,
        variant: row.variant,
        name: row.name,
        price_minor: row.price,
        stock_total: row.stock,
      } satisfies ProductCreate,
      delta: row.restock,
    }));
}

export function sessionFormSchema(currency: string) {
  return z
    .object({
      ...liveSessionFields.shape,
      products: z.array(productRow(currency)),
    })
    .superRefine((form, context) => {
      // The API refuses a second (code, variant) in a session: say so on the row.
      const seen = new Set<string>();
      form.products.forEach((row, index) => {
        const key = `${row.fields.code}\u0000${row.fields.variant}`;
        if (seen.has(key)) {
          context.addIssue({
            code: "custom",
            path: ["products", index, "code"],
            message: "Another row has this code and variant.",
          });
        }
        seen.add(key);
      });
    })
    .transform((form): SessionFormOutput => ({
      session: {
        title: form.title,
        platform: form.platform,
        external_live_id: form.external_live_id,
      },
      products: form.products,
    }));
}

export function emptyProductRow(): ProductRowValues {
  return {
    productId: null,
    code: "",
    variant: "",
    name: "",
    price: "",
    stock: "0",
    restock: "",
  };
}

/** The form as a session and its products stand; a new session starts with one empty row. */
export function formValuesOf(
  session: LiveSessionResponse | null,
  products: readonly ProductResponse[],
): SessionFormValues {
  if (session === null) {
    return {
      title: "",
      platform: "simulator",
      external_live_id: "",
      products: [emptyProductRow()],
    };
  }
  return {
    title: session.title,
    platform: session.platform,
    external_live_id: session.external_live_id,
    products: products.map((product) => ({
      productId: product.id,
      code: product.code,
      variant: product.variant,
      name: product.name,
      price: minorToInput(product.price_minor, product.currency),
      stock: String(product.stock_total),
      restock: "",
    })),
  };
}

// --- From an API error back to its field --------------------------------------------

/** Where the form shows an error: one of its fields, or the form as a whole. */
export type FormErrorTarget =
  | "title"
  | "platform"
  | "external_live_id"
  | `products.${number}.${keyof ProductRowValues}`
  | "root";

/** The form path of a product row's field, as react-hook-form names it. */
export function rowField<F extends keyof ProductRowValues>(
  index: number,
  field: F,
): `products.${number}.${F}` {
  return ["products", index, field].join(".") as `products.${number}.${F}`;
}

export interface FormError {
  target: FormErrorTarget;
  message: string;
}

/** Which request of the save failed: the session's, or a product row's. */
export type SaveStep = { kind: "session" } | { kind: "product" | "restock"; index: number };

const SESSION_FIELDS = new Set(["title", "platform", "external_live_id"]);
const PRODUCT_FIELDS: Record<string, "code" | "variant" | "name" | "price" | "stock"> = {
  code: "code",
  variant: "variant",
  name: "name",
  price_minor: "price",
  stock_total: "stock",
};

/** FastAPI prefixes the message of a validator's own error. */
function cleanMessage(message: string): string {
  const text = message.replace(/^Value error, /, "");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * The errors to show for a failed request: each 422 location under `body`
 * on the field it names, every other refusal on the field it is about when
 * one is obvious (a taken live id, a code the catalog cannot hold, a restock
 * below what is reserved and sold), the rest on the form.
 */
export function formErrorsOf(step: SaveStep, error: unknown): FormError[] {
  if (!(error instanceof ApiError)) {
    return [{ target: "root", message: describeError(error) }];
  }
  if (step.kind === "restock" && (error.status === 409 || error.status === 422)) {
    return [{ target: rowField(step.index, "restock"), message: describeError(error) }];
  }
  if (error.status === 422 && error.validationErrors.length > 0) {
    return error.validationErrors.map((issue): FormError => {
      const [where, field] = issue.loc;
      const message = cleanMessage(issue.msg);
      if (where === "body" && typeof field === "string") {
        if (step.kind === "session" && SESSION_FIELDS.has(field)) {
          return { target: field as FormErrorTarget, message };
        }
        const productField = PRODUCT_FIELDS[field];
        if (step.kind === "product" && productField !== undefined) {
          return { target: rowField(step.index, productField), message };
        }
      }
      return { target: "root", message };
    });
  }
  // A 409 says why in its detail only: the session's status ("The session is
  // live: ..."), which is the form's; a taken live id, or a code and variant
  // the catalog cannot hold, which are a field's.
  const aboutStatus = error.detail?.startsWith("The session is") ?? false;
  if (error.status === 409 && !aboutStatus && step.kind === "session") {
    return [{ target: "external_live_id", message: describeError(error) }];
  }
  if (error.status === 409 && !aboutStatus && step.kind === "product") {
    return [{ target: rowField(step.index, "code"), message: describeError(error) }];
  }
  return [{ target: "root", message: describeError(error) }];
}
