/**
 * zod's global configuration, imported first by main.tsx: zod decides when it
 * builds each object schema, at module evaluation, whether to compile its
 * parser with `new Function`, after probing that it may. The SPA's CSP has no
 * 'unsafe-eval', so the probe fails as intended, yet the browser reports a CSP
 * violation for it. Interpreted parsing is all the forms need.
 */
import { z } from "zod";

z.config({ jitless: true });
