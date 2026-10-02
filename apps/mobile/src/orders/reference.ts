/**
 * Order references and access tokens as the buyer pastes or receives them.
 *
 * A reference is LC- then six symbols of Crockford's base 32, read as the API
 * reads it (livecart_core.domain.reference): any case, spaces around and the
 * hyphen optional, O for 0, I and L for 1. The app stores and shows the
 * canonical form only. A token is the order's 256-bit HMAC, base64url without
 * padding: 43 characters (livecart_core.order_tokens).
 */

const CROCKFORD = /^[0-9A-HJKMNP-TV-Z]{6}$/;
const MISREAD: Readonly<Record<string, string>> = { O: "0", I: "1", L: "1" };
const TOKEN = /^[A-Za-z0-9_-]{43}$/;

/** `LC-7K3F9Q` from what the buyer typed, or null when it is no order reference. */
export function parseOrderReference(text: string): string | null {
  const compact = text.normalize("NFKC").trim().toUpperCase();
  const match = /^LC-?([0-9A-Z]{6})$/.exec(compact);
  if (match?.[1] === undefined) {
    return null;
  }
  const symbols = match[1].replace(/[OIL]/g, (symbol) => MISREAD[symbol] ?? symbol);
  return CROCKFORD.test(symbols) ? `LC-${symbols}` : null;
}

/** Whether `text` has the shape of an order's access token; only the API can tell it is right. */
export function isOrderToken(text: string): boolean {
  return TOKEN.test(text);
}
