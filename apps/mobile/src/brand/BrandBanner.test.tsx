import { describe, expect, it } from "@jest/globals";
import { render, screen } from "@testing-library/react-native";

import { BRAND_IDS, testBrandTheme } from "../test/brands";

import { BrandBanner } from "./BrandBanner";
import { BrandProvider } from "./brandTheme";

describe("BrandBanner", () => {
  it("knows both brands", () => {
    expect(BRAND_IDS).toEqual(["demo", "pomelune"]);
  });

  it.each(BRAND_IDS)("draws the %s brand in its own name, logo and colors", async (brandId) => {
    const theme = testBrandTheme(brandId);

    await render(
      <BrandProvider theme={theme}>
        <BrandBanner subtitle="Live now" />
      </BrandProvider>,
    );

    expect(screen.getByText(theme.brand.displayName)).toBeTruthy();
    expect(screen.getByLabelText(`${theme.brand.displayName} logo`)).toBeTruthy();
    expect(screen.toJSON()).toMatchSnapshot();
  });
});
