import { useEffect, useRef, type ReactNode } from "react";

export type Tone = "neutral" | "brand" | "success" | "warning" | "danger" | "info";

const TONES: Record<Tone, string> = {
  neutral: "bg-canvas text-ink-muted border-line",
  brand: "bg-brand-soft text-brand-strong border-brand-soft",
  success: "bg-success-soft text-success border-success-soft",
  warning: "bg-warning-soft text-warning border-warning-soft",
  danger: "bg-danger-soft text-danger border-danger-soft",
  info: "bg-info-soft text-info border-info-soft",
};

/** A short label of a status; its text says it all, the colour only repeats it. */
export function Badge({
  tone = "neutral",
  children,
  title,
}: {
  tone?: Tone;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap ${TONES[tone]}`}
    >
      {children}
    </span>
  );
}

/**
 * A message box. An error is an `alert`, announced as soon as it appears;
 * anything else is a polite `status`.
 */
export function Alert({
  tone = "danger",
  children,
  className = "",
}: {
  tone?: Exclude<Tone, "neutral" | "brand">;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      role={tone === "danger" ? "alert" : "status"}
      className={`rounded-control border px-3 py-2 text-sm ${TONES[tone]} ${className}`}
    >
      {children}
    </div>
  );
}

/** A polite loading message. */
export function Loading({ children = "Loading…" }: { children?: ReactNode }) {
  return (
    <p role="status" className="py-6 text-sm text-ink-muted">
      {children}
    </p>
  );
}

/** Every page has one `PageTitle`, under this id. */
const PAGE_TITLE_ID = "page-title";

/**
 * The page's heading. It takes the focus when the page opens, so that a
 * screen reader announces the new page after a client-side navigation, and
 * names the document.
 */
export function PageTitle({ children, title }: { children: ReactNode; title: string }) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    document.title = `${title} · LiveCart`;
    heading.current?.focus();
  }, [title]);
  return (
    <h1
      ref={heading}
      id={PAGE_TITLE_ID}
      tabIndex={-1}
      className="text-2xl font-semibold focus:outline-none"
    >
      {children}
    </h1>
  );
}

/**
 * Move the focus to the page's heading, for a control that is about to
 * unmount while it has it: the focus would otherwise fall to the document's
 * body, and a keyboard or screen reader user would start again from the top.
 */
export function focusPageTitle(): void {
  document.getElementById(PAGE_TITLE_ID)?.focus();
}
