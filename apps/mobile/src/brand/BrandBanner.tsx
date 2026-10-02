import { fontWeight } from "@livecart/design-tokens";
import { Image, StyleSheet, Text, View } from "react-native";

import { useBrandTheme } from "./brandTheme";

/** The brand's logo and name, on its primary color: the top of the storefront. */
export function BrandBanner({ subtitle }: { subtitle?: string }) {
  const { brand, logo, color, space, fontSize } = useBrandTheme();
  return (
    <View
      accessibilityRole="header"
      style={[styles.banner, { backgroundColor: color.primary, padding: space.lg, gap: space.md }]}
    >
      <Image source={logo} style={styles.logo} accessibilityLabel={`${brand.displayName} logo`} />
      <View style={styles.text}>
        <Text
          style={{ color: color.onPrimary, fontSize: fontSize.title, fontWeight: fontWeight.bold }}
        >
          {brand.displayName}
        </Text>
        {subtitle === undefined ? null : (
          <Text style={{ color: color.onPrimary, fontSize: fontSize.caption }}>{subtitle}</Text>
        )}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  banner: { flexDirection: "row", alignItems: "center" },
  logo: { width: 48, height: 48 },
  text: { flex: 1 },
});
