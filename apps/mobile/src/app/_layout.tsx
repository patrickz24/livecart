import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";

import { BrandProvider, isLightColor } from "../brand/brandTheme";
import { currentBrandTheme } from "../brand/currentBrand";

// Once, at startup: the brand is fixed for the life of the build.
const theme = currentBrandTheme();

/** The root of the router: the brand's theme, then one stack in its colors. */
export default function RootLayout() {
  return (
    <BrandProvider theme={theme}>
      <StatusBar style={isLightColor(theme.color.onPrimary) ? "light" : "dark"} />
      <Stack
        screenOptions={{
          headerStyle: { backgroundColor: theme.color.primary },
          headerTintColor: theme.color.onPrimary,
          contentStyle: { backgroundColor: theme.color.background },
        }}
      />
    </BrandProvider>
  );
}
