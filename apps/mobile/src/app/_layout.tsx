import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { View } from "react-native";

import { ApiProvider } from "../api/ApiProvider";
import { API_BASE_URL, createApiClient } from "../api/client";
import { BrandProvider, isLightColor } from "../brand/brandTheme";
import { currentBrandTheme } from "../brand/currentBrand";
import { Notice } from "../ui/Notice";

// Once, at startup: the brand and the API's address are fixed for the life of the bundle.
const theme = currentBrandTheme();
const client = API_BASE_URL === null ? null : createApiClient(API_BASE_URL);

/** The root of the router: the brand's theme, the API, then one stack in the brand's colors. */
export default function RootLayout() {
  return (
    <BrandProvider theme={theme}>
      <StatusBar style={isLightColor(theme.color.onPrimary) ? "light" : "dark"} />
      {client === null ? (
        <View style={{ flex: 1, justifyContent: "center", padding: theme.space.lg }}>
          <Notice
            tone="error"
            title="No API address"
            message="This bundle has no EXPO_PUBLIC_API_URL: set it in apps/mobile/.env (see .env.example), then start Expo again."
          />
        </View>
      ) : (
        <ApiProvider client={client}>
          <Stack
            screenOptions={{
              headerStyle: { backgroundColor: theme.color.primary },
              headerTintColor: theme.color.onPrimary,
              contentStyle: { backgroundColor: theme.color.background },
            }}
          >
            <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
          </Stack>
        </ApiProvider>
      )}
    </BrandProvider>
  );
}
