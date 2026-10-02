import { Stack } from "expo-router";
import { SafeAreaView } from "react-native-safe-area-context";

import { BrandBanner } from "../brand/BrandBanner";
import { useBrandTheme } from "../brand/brandTheme";

export default function HomeScreen() {
  const { color } = useBrandTheme();
  return (
    <SafeAreaView edges={["top"]} style={{ flex: 1, backgroundColor: color.primary }}>
      <Stack.Screen options={{ headerShown: false }} />
      <BrandBanner />
    </SafeAreaView>
  );
}
