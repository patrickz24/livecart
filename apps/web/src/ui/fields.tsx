import { useId, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes } from "react";

const CONTROL =
  "block w-full rounded-control border border-line-strong bg-surface px-3 py-2 text-sm text-ink " +
  "disabled:bg-canvas disabled:text-ink-muted aria-invalid:border-danger";

interface FieldProps {
  label: string;
  /** Help shown under the control, read with it. */
  hint?: ReactNode;
  /** The validation message, read with the control and marking it invalid. */
  error?: string | undefined;
  /** Hide the label visually, for a table cell whose column header names it. */
  labelHidden?: boolean;
  className?: string;
}

function Field({
  id,
  label,
  hint,
  error,
  labelHidden,
  className,
  children,
}: FieldProps & { id: string; children: ReactNode }) {
  return (
    <div className={className}>
      <label
        htmlFor={id}
        className={labelHidden === true ? "sr-only" : "mb-1 block text-sm font-medium"}
      >
        {label}
      </label>
      {children}
      {hint === undefined ? null : (
        <p id={`${id}-hint`} className="mt-1 text-xs text-ink-muted">
          {hint}
        </p>
      )}
      {error === undefined ? null : (
        <p id={`${id}-error`} className="mt-1 text-xs font-medium text-danger">
          {error}
        </p>
      )}
    </div>
  );
}

function describedBy(id: string, hint: unknown, error: unknown): string | undefined {
  const ids = [
    hint === undefined ? null : `${id}-hint`,
    error === undefined ? null : `${id}-error`,
  ];
  const joined = ids.filter((value) => value !== null).join(" ");
  return joined === "" ? undefined : joined;
}

/** A labelled input; its hint and error are announced with it. */
export function TextField({
  label,
  hint,
  error,
  labelHidden,
  className,
  ...input
}: FieldProps & InputHTMLAttributes<HTMLInputElement>) {
  const generated = useId();
  const id = input.id ?? generated;
  return (
    <Field
      id={id}
      label={label}
      hint={hint}
      error={error}
      labelHidden={labelHidden ?? false}
      className={className ?? ""}
    >
      <input
        {...input}
        id={id}
        className={CONTROL}
        aria-invalid={error === undefined ? undefined : true}
        aria-describedby={describedBy(id, hint, error)}
      />
    </Field>
  );
}

/** A labelled select of fixed options. */
export function SelectField({
  label,
  hint,
  error,
  labelHidden,
  className,
  options,
  ...select
}: FieldProps &
  SelectHTMLAttributes<HTMLSelectElement> & {
    options: readonly { value: string; label: string }[];
  }) {
  const generated = useId();
  const id = select.id ?? generated;
  return (
    <Field
      id={id}
      label={label}
      hint={hint}
      error={error}
      labelHidden={labelHidden ?? false}
      className={className ?? ""}
    >
      <select
        {...select}
        id={id}
        className={CONTROL}
        aria-invalid={error === undefined ? undefined : true}
        aria-describedby={describedBy(id, hint, error)}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </Field>
  );
}
