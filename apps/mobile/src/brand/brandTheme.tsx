import { tokens, type ColorTokens, type DesignTokens } from "@livecart/design-tokens";
import { createContext, use, type ReactNode } from "react";
import type { ImageSourcePropType } from "react-native";

import type { Brand } from "../../brand.schema";

/** What the screens draw with: the brand, its logo, and the design tokens in its colors. */
export interface BrandTheme extends DesignTokens {
  brand: Brand;
  logo: ImageSourcePropType;
  color: ColorTokens;
}

/** The shared tokens, with the brand's three colors over them. */
export function brandTheme(brand: Brand, logo: ImageSourcePropType): BrandTheme {
  return { ...tokens, brand, logo, color: { ...tokens.color, ...brand.colors } };
}

/** Whether a #RRGGBB color is light (relative luminance above one half), for the status bar. */
export function isLightColor(hex: string): boolean {
  const channels = [1, 3, 5].map((start) => {
    const value = Number.parseInt(hex.slice(start, start + 2), 16) / 255;
    return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  });
  const [red = 0, green = 0, blue = 0] = channels;
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue > 0.5;
}

const BrandContext = createContext<BrandTheme | null>(null);

export function BrandProvider({ theme, children }: { theme: BrandTheme; children: ReactNode }) {
  return <BrandContext value={theme}>{children}</BrandContext>;
}

/** The theme of the brand this build is for; only under `BrandProvider`. */
export function useBrandTheme(): BrandTheme {
  const theme = use(BrandContext);
  if (theme === null) {
    throw new Error("useBrandTheme needs a BrandProvider above it");
  }
  return theme;
}
