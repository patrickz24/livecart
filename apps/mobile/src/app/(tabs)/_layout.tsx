import { Tabs } from "expo-router/js-tabs";

import { useBrandTheme } from "../../brand/brandTheme";

/** The two places of the app: the seller's live, and the buyer's orders. */
export default function TabsLayout() {
  const { color, fontSize } = useBrandTheme();
  return (
    <Tabs
      screenOptions={{
        headerStyle: { backgroundColor: color.primary },
        headerTintColor: color.onPrimary,
        tabBarActiveTintColor: color.primary,
        tabBarInactiveTintColor: color.textMuted,
        // Labels only: the tabs have no icons. (The Android bundle does hold
        // MaterialSymbols_400Regular.ttf, which expo-router brings through
        // expo-symbols, but nothing here draws with it.)
        tabBarIconStyle: { display: "none" },
        tabBarLabelStyle: { fontSize: fontSize.body },
      }}
    >
      <Tabs.Screen name="index" options={{ title: "Live", headerShown: false }} />
      <Tabs.Screen name="orders" options={{ title: "My orders" }} />
    </Tabs>
  );
}
