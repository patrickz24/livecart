/** Formatting in the phone's locale: money in minor units, dates. */

const moneyFormats = new Map<string, Intl.NumberFormat>();

function moneyFormat(currency: string): Intl.NumberFormat {
  let format = moneyFormats.get(currency);
  if (format === undefined) {
    format = new Intl.NumberFormat(undefined, { style: "currency", currency });
    moneyFormats.set(currency, format);
  }
  return format;
}

/** An amount of the API, in integer minor units, as the locale writes it (2 decimals for EUR). */
export function formatMoney(minor: number, currency: string): string {
  const format = moneyFormat(currency);
  const digits = format.resolvedOptions().maximumFractionDigits ?? 2;
  return format.format(minor / 10 ** digits);
}

const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

export function formatDateTime(iso: string): string {
  return dateTime.format(new Date(iso));
}
