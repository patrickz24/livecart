/**
 * LiveCart's design tokens: tokens.json holds the values, this module their
 * types. A brand of the buyer app overrides the three colors of `BrandColors`
 * (its brand.json); every other token is shared by all brands.
 *
 * The JSON is checked against `DesignTokens` at compile time (`satisfies`):
 * a token removed, renamed or of the wrong type fails `pnpm -r typecheck`.
 */
import rawTokens from "../tokens.json";

/** A color as #RRGGBB. */
export type HexColor = string;

export interface ColorTokens {
  /** Buttons, the header, links: the brand's main color. */
  primary: HexColor;
  /** Text and icons drawn on `primary`. */
  onPrimary: HexColor;
  /** Highlights: prices, badges. */
  accent: HexColor;
  background: HexColor;
  surface: HexColor;
  text: HexColor;
  textMuted: HexColor;
  border: HexColor;
  success: HexColor;
  warning: HexColor;
  danger: HexColor;
}

/** The colors a brand chooses; the others stay the shared ones. */
export type BrandColors = Pick<ColorTokens, "primary" | "onPrimary" | "accent">;

export interface DesignTokens {
  color: ColorTokens;
  /** Spacing, in density-independent pixels. */
  space: Record<"xs" | "sm" | "md" | "lg" | "xl" | "xxl", number>;
  radius: Record<"sm" | "md" | "lg" | "pill", number>;
  fontSize: Record<"caption" | "body" | "title" | "headline", number>;
}

/**
 * Font weights, as React Native types them: a closed union of strings, which
 * a JSON import would widen to `string`, hence their place here.
 */
export const fontWeight = { regular: "400", semibold: "600", bold: "700" } as const;

export const tokens: DesignTokens = rawTokens satisfies DesignTokens;
