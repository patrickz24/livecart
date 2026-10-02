/** Formatting for the seller's locale: money in minor units, dates. */

const moneyFormats = new Map<string, Intl.NumberFormat>();

function moneyFormat(currency: string): Intl.NumberFormat {
  let format = moneyFormats.get(currency);
  if (format === undefined) {
    format = new Intl.NumberFormat(undefined, { style: "currency", currency });
    moneyFormats.set(currency, format);
  }
  return format;
}

/** The number of minor units in a major one is 10 to this (2 for EUR, 0 for JPY). */
export function fractionDigits(currency: string): number {
  return moneyFormat(currency).resolvedOptions().maximumFractionDigits ?? 2;
}

/** An amount of the API, in integer minor units, as the locale writes it. */
export function formatMoney(minor: number, currency: string): string {
  return moneyFormat(currency).format(minor / 10 ** fractionDigits(currency));
}

/** Minor units as the price field shows them: "19.90" for 1990 in EUR. */
export function minorToInput(minor: number, currency: string): string {
  const digits = fractionDigits(currency);
  return (minor / 10 ** digits).toFixed(digits);
}

/**
 * A price as typed ("19.9", "19,90", "20") in minor units, or `null` when it is
 * no amount of this currency: exact integer arithmetic, no floating point.
 */
export function inputToMinor(text: string, currency: string): number | null {
  const digits = fractionDigits(currency);
  const match = /^(\d{1,9})(?:[.,](\d+))?$/.exec(text.trim());
  if (match === null) {
    return null;
  }
  const [, units = "", fraction = ""] = match;
  if (fraction.length > digits) {
    return null;
  }
  return Number(units) * 10 ** digits + Number(fraction.padEnd(digits, "0") || "0");
}

const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

export function formatDateTime(iso: string): string {
  return dateTime.format(new Date(iso));
}
