import type { ConfigContext, ExpoConfig } from "expo/config";

/**
 * The app's Expo configuration, evaluated by the Expo CLI and EAS before each
 * start, export and build.
 */
export default function appConfig({ config }: ConfigContext): ExpoConfig {
  return {
    ...config,
    name: "LiveCart",
    slug: "livecart-demo",
    scheme: "livecart",
    version: "0.1.0",
    orientation: "portrait",
    userInterfaceStyle: "light",
    plugins: ["expo-router", "expo-secure-store", "expo-web-browser"],
    experiments: { typedRoutes: true },
  };
}
