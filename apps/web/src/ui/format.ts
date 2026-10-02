/** Formatting for the seller's locale: money in minor units, dates, durations. */

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

const time = new Intl.DateTimeFormat(undefined, { timeStyle: "medium" });

export function formatTime(iso: string): string {
  return time.format(new Date(iso));
}

/** A countdown as "14:05", or "1:02:03" past an hour; never negative. */
export function formatCountdown(milliseconds: number): string {
  const total = Math.max(0, Math.ceil(milliseconds / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const mmss = `${String(minutes).padStart(hours > 0 ? 2 : 1, "0")}:${String(seconds).padStart(2, "0")}`;
  return hours > 0 ? `${String(hours)}:${mmss}` : mmss;
}
