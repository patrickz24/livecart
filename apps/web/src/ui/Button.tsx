import type { ComponentPropsWithRef } from "react";
import { Link, type LinkProps } from "react-router";

type Variant = "primary" | "secondary" | "danger" | "ghost";

const BASE =
  "inline-flex items-center justify-center gap-2 rounded-control px-3 py-2 text-sm font-medium " +
  "disabled:cursor-not-allowed disabled:opacity-60";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-brand text-white hover:bg-brand-strong",
  secondary: "border border-line-strong bg-surface text-ink hover:bg-canvas",
  danger: "bg-danger text-white hover:opacity-90",
  ghost: "text-brand hover:bg-brand-soft",
};

export function buttonClass(variant: Variant = "secondary", extra = ""): string {
  return `${BASE} ${VARIANTS[variant]} ${extra}`.trim();
}

/** A button that never submits a form unless it says so. */
export function Button({
  variant = "secondary",
  className = "",
  type = "button",
  ...props
}: ComponentPropsWithRef<"button"> & { variant?: Variant }) {
  return <button type={type} className={buttonClass(variant, className)} {...props} />;
}

/** A link that looks like a button: navigation stays a link for assistive technologies. */
export function ButtonLink({
  variant = "secondary",
  className = "",
  ...props
}: LinkProps & { variant?: Variant }) {
  return <Link className={buttonClass(variant, className)} {...props} />;
}
