import { fontWeight } from "@livecart/design-tokens";
import { ActivityIndicator, Pressable, StyleSheet, Text } from "react-native";

import { useBrandTheme } from "../brand/brandTheme";

export interface ButtonProps {
  label: string;
  onPress: () => void;
  /** `primary` in the brand's color; `secondary` outlined, for the other actions. */
  variant?: "primary" | "secondary";
  disabled?: boolean;
  /** Shows a spinner, and ignores presses, while an action runs. */
  busy?: boolean;
}

export function Button({ label, onPress, variant = "primary", disabled, busy }: ButtonProps) {
  const { color, space, radius, fontSize } = useBrandTheme();
  const primary = variant === "primary";
  const inactive = disabled === true || busy === true;
  const foreground = primary ? color.onPrimary : color.primary;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ disabled: inactive, busy: busy === true }}
      disabled={inactive}
      onPress={onPress}
      style={({ pressed }) => [
        styles.button,
        {
          backgroundColor: primary ? color.primary : color.surface,
          borderColor: color.primary,
          borderRadius: radius.md,
          paddingVertical: space.md,
          paddingHorizontal: space.lg,
          opacity: inactive ? 0.5 : pressed ? 0.8 : 1,
        },
      ]}
    >
      {busy === true ? <ActivityIndicator color={foreground} /> : null}
      <Text style={{ color: foreground, fontSize: fontSize.body, fontWeight: fontWeight.semibold }}>
        {label}
      </Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  button: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
    borderWidth: 1,
  },
});
