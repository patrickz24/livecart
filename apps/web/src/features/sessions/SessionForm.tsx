import { zodResolver } from "@hookform/resolvers/zod";
import type { LiveSessionResponse, ProductResponse } from "@livecart/api-client";
import { useEffect, useMemo, useRef } from "react";
import { useFieldArray, useForm, useWatch } from "react-hook-form";
import { Link } from "react-router";

import { useServices } from "../../app/services";
import { PLATFORM_LABELS } from "../../strings";
import { Button } from "../../ui/Button";
import { SelectField, TextField } from "../../ui/fields";
import { Alert } from "../../ui/feedback";
import { formatMoney } from "../../ui/format";
import { newIdempotencyKey, saveSession, SaveFailed, type SavedState } from "./saveSession";
import {
  emptyProductRow,
  formErrorsOf,
  formValuesOf,
  PLATFORMS,
  rowField,
  sessionFormSchema,
  type SessionFormOutput,
  type SessionFormValues,
} from "./sessionSchema";

// The deployment's one currency (DEFAULT_CURRENCY, EUR in the stack): every
// product of the API names it, and a session without products yet shows it.
const DEFAULT_CURRENCY = "EUR";
const MAX_PRODUCTS = 200;
const PLATFORM_OPTIONS = PLATFORMS.map((value) => ({ value, label: PLATFORM_LABELS[value] }));

/**
 * A session and its products in one form (`/sessions/new`, `/sessions/:id/edit`).
 *
 * What the session's status locks is shown read-only rather than disabled, so
 * that it stays focusable and is read out: past the draft, the platform, the
 * live id and the saved products' codes and variants; once ended, everything.
 * Stock moves by restock deltas once a product exists, never by rewriting its
 * total, so that units reserved or sold meanwhile are never lost.
 */
export function SessionForm({
  session,
  products,
  onSaved,
}: {
  session: LiveSessionResponse | null;
  products: readonly ProductResponse[];
  onSaved: (session: LiveSessionResponse) => Promise<void> | void;
}) {
  const { client } = useServices();
  const currency = products[0]?.currency ?? DEFAULT_CURRENCY;
  const schema = useMemo(() => sessionFormSchema(currency), [currency]);
  const form = useForm<SessionFormValues, unknown, SessionFormOutput>({
    resolver: zodResolver(schema),
    defaultValues: formValuesOf(session, products),
  });
  const { fields, append, remove } = useFieldArray({ control: form.control, name: "products" });
  const rows = useWatch({ control: form.control, name: "products" });
  const saved = useRef<SavedState>({
    session,
    products: new Map(products.map((product) => [product.id, product])),
  });
  // The session as the API last answered it: a start or an end of the live
  // (LifecycleControls) changes which of its fields a save may send.
  useEffect(() => {
    if (session !== null) {
      saved.current.session = session;
    }
  }, [session]);
  // One key per (product, delta): a retry of the same restock sends the same key.
  const restockKeys = useRef(new Map<string, { delta: number; key: string }>());

  const status = session?.status ?? "draft";
  const ended = status === "ended";
  const keysLocked = status !== "draft";
  const { errors, isSubmitting } = form.formState;

  function restockKey(productId: string, delta: number): string {
    const known = restockKeys.current.get(productId);
    if (known?.delta === delta) {
      return known.key;
    }
    const key = newIdempotencyKey();
    restockKeys.current.set(productId, { delta, key });
    return key;
  }

  async function submit(output: SessionFormOutput) {
    try {
      const result = await saveSession(client, saved.current, output, {
        onSessionSaved: (savedSession) => {
          saved.current.session = savedSession;
        },
        onProductSaved: (index, product) => {
          saved.current.products.set(product.id, product);
          form.setValue(rowField(index, "productId"), product.id);
        },
        onRestocked: (index, product) => {
          restockKeys.current.delete(product.id);
          saved.current.products.set(product.id, product);
          form.setValue(rowField(index, "restock"), "");
          form.setValue(rowField(index, "stock"), String(product.stock_total));
        },
        restockKey,
      });
      await onSaved(result);
    } catch (error) {
      if (!(error instanceof SaveFailed)) {
        throw error;
      }
      formErrorsOf(error.step, error.cause).forEach((failure, index) => {
        form.setError(
          failure.target,
          { type: "server", message: failure.message },
          { shouldFocus: index === 0 },
        );
      });
    }
  }

  return (
    <form
      noValidate
      onSubmit={(event) => void form.handleSubmit(submit)(event)}
      className="mt-6 space-y-6"
      aria-describedby={errors.root === undefined ? undefined : "form-error"}
    >
      {ended ? <Alert tone="info">This session has ended: it can no longer change.</Alert> : null}
      {errors.root?.message === undefined ? null : (
        <div id="form-error">
          <Alert>{errors.root.message}</Alert>
        </div>
      )}

      <fieldset className="rounded-control border border-line bg-surface p-4">
        <legend className="px-1 text-base font-semibold">Session</legend>
        <div className="grid gap-4 md:grid-cols-3">
          <TextField
            label="Title"
            readOnly={ended}
            maxLength={200}
            error={errors.title?.message}
            {...form.register("title")}
          />
          {keysLocked ? (
            <TextField
              label="Platform"
              readOnly
              value={PLATFORM_LABELS[form.getValues("platform")]}
              hint="Set while the session is a draft."
            />
          ) : (
            <SelectField
              label="Platform"
              options={PLATFORM_OPTIONS}
              error={errors.platform?.message}
              {...form.register("platform")}
            />
          )}
          <TextField
            label="Live id"
            readOnly={keysLocked}
            maxLength={128}
            hint={
              keysLocked
                ? "Set while the session is a draft."
                : "The live's id on its platform: comments find the session by it."
            }
            error={errors.external_live_id?.message}
            {...form.register("external_live_id")}
          />
        </div>
      </fieldset>

      <fieldset className="rounded-control border border-line bg-surface p-4">
        <legend className="px-1 text-base font-semibold">Products</legend>
        {fields.length === 0 ? <p className="text-sm text-ink-muted">No products yet.</p> : null}
        <ol className="space-y-4">
          {fields.map((field, index) => {
            const productId = rows[index]?.productId ?? null;
            const savedProduct =
              productId === null ? undefined : saved.current.products.get(productId);
            const rowErrors = errors.products?.[index];
            const number = index + 1;
            return (
              <li key={field.id}>
                <fieldset className="rounded-control border border-line p-3">
                  <legend className="px-1 text-sm font-medium">Product {number}</legend>
                  <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-6">
                    <TextField
                      label="Code"
                      readOnly={ended || (keysLocked && productId !== null)}
                      maxLength={16}
                      autoCapitalize="characters"
                      hint="Such as A12"
                      error={rowErrors?.code?.message}
                      {...form.register(rowField(index, "code"))}
                    />
                    <TextField
                      label="Variant"
                      readOnly={ended || (keysLocked && productId !== null)}
                      maxLength={64}
                      hint="Size or colour, optional"
                      error={rowErrors?.variant?.message}
                      {...form.register(rowField(index, "variant"))}
                    />
                    <TextField
                      label="Name"
                      className="lg:col-span-2"
                      readOnly={ended}
                      maxLength={200}
                      error={rowErrors?.name?.message}
                      {...form.register(rowField(index, "name"))}
                    />
                    <TextField
                      label={`Price (${currency})`}
                      readOnly={ended}
                      inputMode="decimal"
                      hint="Such as 19.90"
                      error={rowErrors?.price?.message}
                      {...form.register(rowField(index, "price"))}
                    />
                    {productId === null ? (
                      <TextField
                        label="Stock"
                        inputMode="numeric"
                        hint="Units for sale"
                        error={rowErrors?.stock?.message}
                        {...form.register(rowField(index, "stock"))}
                      />
                    ) : (
                      <TextField
                        label="Restock"
                        readOnly={ended}
                        inputMode="numeric"
                        placeholder="+0"
                        hint={
                          savedProduct === undefined
                            ? undefined
                            : `${String(savedProduct.stock_total)} in stock: ${String(savedProduct.reserved)} reserved, ${String(savedProduct.sold)} sold. Add units, or remove them with a minus sign.`
                        }
                        error={rowErrors?.restock?.message}
                        {...form.register(rowField(index, "restock"))}
                      />
                    )}
                  </div>
                  {savedProduct === undefined ? null : (
                    <p className="mt-2 text-xs text-ink-muted">
                      Saved at {formatMoney(savedProduct.price_minor, savedProduct.currency)}.
                    </p>
                  )}
                  {productId === null ? (
                    <Button
                      variant="ghost"
                      className="mt-2"
                      onClick={() => {
                        remove(index);
                      }}
                    >
                      Remove product {number}
                    </Button>
                  ) : null}
                </fieldset>
              </li>
            );
          })}
        </ol>
        {ended ? null : (
          <Button
            className="mt-4"
            disabled={fields.length >= MAX_PRODUCTS}
            onClick={() => {
              append(emptyProductRow(), {
                shouldFocus: true,
                focusName: rowField(fields.length, "code"),
              });
            }}
          >
            Add a product
          </Button>
        )}
      </fieldset>

      <div className="flex items-center gap-3">
        {ended ? null : (
          <Button type="submit" variant="primary" disabled={isSubmitting}>
            {isSubmitting ? "Saving…" : session === null ? "Create session" : "Save changes"}
          </Button>
        )}
        <Link to="/sessions" className="text-sm font-medium text-brand underline">
          Back to sessions
        </Link>
      </div>
    </form>
  );
}
