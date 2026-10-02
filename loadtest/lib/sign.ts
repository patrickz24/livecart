// The comment webhook's signature, the scheme of livecart_core.signing, which
// the simulator signs with and the API checks before it reads the body:
//
//   X-LiveCart-Signature: t=<unix time>,v1=<hex HMAC-SHA256 of "<t>.<body>">
//
// under COMMENT_WEBHOOK_SECRET_<PLATFORM>. The API refuses a timestamp more
// than 300 s away from its clock, so each call is signed as it is sent.
import { hmac } from "k6/crypto";

export const SIGNATURE_HEADER = "X-LiveCart-Signature";

/** The header value that signs `body` at `timestamp`, in whole Unix seconds. */
export function signature(secret: string, body: string, timestamp: number): string {
  const t = String(timestamp);
  return `t=${t},v1=${hmac("sha256", secret, `${t}.${body}`, "hex")}`;
}
