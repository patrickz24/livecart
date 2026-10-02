/**
 * The schema of brands/<id>/brand.json: everything that makes one white-label
 * build of the buyer app differ from another. app.config.ts validates the
 * brand it builds against it, and fails the build on any problem; the app
 * reads the same brand back from `extra.brand` and parses it again.
 *
 * No Node API here: the app's bundle imports this module too.
 */
import { z } from "zod";

/**
 * Schemes a brand never takes: the web's, those a browser runs or reads, and
 * Expo Go's. The API refuses them as return URLs whatever ALLOWED_APP_SCHEMES
 * says (FORBIDDEN_APP_SCHEMES in livecart_core.settings).
 */
const FORBIDDEN_SCHEMES: ReadonlySet<string> = new Set([
  "http",
  "https",
  "ws",
  "wss",
  "ftp",
  "javascript",
  "vbscript",
  "data",
  "file",
  "blob",
  "about",
  "exp",
]);

/** The image files of a brand, each in its own folder. */
export const BRAND_ASSETS = ["logo", "icon", "adaptiveIcon", "splash"] as const;
export type BrandAsset = (typeof BRAND_ASSETS)[number];

const hexColor = z.string().regex(/^#[0-9A-Fa-f]{6}$/, "must be a color as #RRGGBB");

// A file name, never a path: nothing outside the brand's folder.
const pngFile = z
  .string()
  .regex(/^[a-z0-9][a-z0-9-]*\.png$/, "must be the name of a PNG file of the brand's folder");

// The seller's slug, as the API's sellers have it (livecart_core.domain.sellers).
const sellerSlug = z
  .string()
  .max(64)
  .regex(/^[a-z0-9]+(?:-[a-z0-9]+)*$/, "must be lower-case words joined by hyphens");

export const brandSchema = z.strictObject({
  /** The folder's name: lower-case letters and digits, as in the app identifiers. */
  id: z.string().regex(/^[a-z][a-z0-9]{1,29}$/, "must be 2 to 30 lower-case letters or digits"),
  /** The app's name under its icon, and in its header. */
  displayName: z.string().trim().min(1).max(30),
  /** The project's slug at Expo (EAS). */
  slug: z
    .string()
    .max(64)
    .regex(/^[a-z0-9]+(?:-[a-z0-9]+)*$/, "must be lower-case words joined by hyphens"),
  /** The app's URI scheme: its deep links and its payment return URL, <scheme>://... */
  scheme: z
    .string()
    .regex(/^[a-z][a-z0-9+.-]*$/, "must be a lower-case URI scheme, without ://")
    .refine((scheme) => !FORBIDDEN_SCHEMES.has(scheme), "is no app's scheme"),
  ios: z.strictObject({
    bundleIdentifier: z
      .string()
      .regex(
        /^[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z][A-Za-z0-9-]*)+$/,
        "must be a reverse-DNS identifier, such as com.example.livecart.demo",
      ),
  }),
  android: z.strictObject({
    package: z
      .string()
      .regex(
        /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/,
        "must be an Android package, such as com.example.livecart.demo",
      ),
  }),
  /** The seller whose storefront the app shows (GET /api/v1/storefronts/{sellerSlug}). */
  sellerSlug,
  /** The brand's colors, over the shared design tokens (@livecart/design-tokens). */
  colors: z.strictObject({ primary: hexColor, onPrimary: hexColor, accent: hexColor }),
  assets: z.strictObject({
    logo: pngFile,
    icon: pngFile,
    adaptiveIcon: pngFile,
    splash: pngFile,
  }),
  /** The EAS project of this brand, which `eas init` creates; none until then. */
  easProjectId: z.uuid().optional(),
});

export type Brand = z.infer<typeof brandSchema>;

/** Each problem of an invalid brand, as `field.path: message`. */
export function describeIssues(error: z.ZodError): string[] {
  return error.issues.map((issue) => {
    const field = issue.path.map(String).join(".");
    return `${field === "" ? "(the file)" : field}: ${issue.message}`;
  });
}
