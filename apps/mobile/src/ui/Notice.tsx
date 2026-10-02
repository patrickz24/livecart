import { fontWeight } from "@livecart/design-tokens";
import { Text, View } from "react-native";

import { useBrandTheme } from "../brand/brandTheme";

import { Button } from "./Button";

export interface NoticeProps {
  title: string;
  message?: string;
  /** `error` for a failure, `info` for an empty or waiting state. */
  tone?: "info" | "error";
  action?: { label: string; onPress: () => void };
}

/** A state of a screen that has nothing else to show: empty, refused, unreachable. */
export function Notice({ title, message, tone = "info", action }: NoticeProps) {
  const { color, space, radius, fontSize } = useBrandTheme();
  return (
    <View
      accessibilityRole={tone === "error" ? "alert" : "summary"}
      style={{
        gap: space.sm,
        padding: space.lg,
        borderRadius: radius.md,
        borderWidth: 1,
        borderColor: tone === "error" ? color.danger : color.border,
        backgroundColor: color.surface,
      }}
    >
      <Text
        style={{
          color: tone === "error" ? color.danger : color.text,
          fontSize: fontSize.title,
          fontWeight: fontWeight.bold,
        }}
      >
        {title}
      </Text>
      {message === undefined ? null : (
        <Text style={{ color: color.textMuted, fontSize: fontSize.body }}>{message}</Text>
      )}
      {action === undefined ? null : (
        <Button label={action.label} onPress={action.onPress} variant="secondary" />
      )}
    </View>
  );
}
