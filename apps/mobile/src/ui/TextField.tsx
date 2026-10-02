import { fontWeight } from "@livecart/design-tokens";
import { Text, TextInput, View, type TextInputProps } from "react-native";

import { useBrandTheme } from "../brand/brandTheme";

export interface TextFieldProps extends Omit<TextInputProps, "style"> {
  label: string;
  /** Shown under the field, which is then marked invalid. */
  error?: string | null;
}

export function TextField({ label, error, ...input }: TextFieldProps) {
  const { color, space, radius, fontSize } = useBrandTheme();
  const invalid = error !== undefined && error !== null;
  return (
    <View style={{ gap: space.xs }}>
      <Text
        style={{ color: color.text, fontSize: fontSize.caption, fontWeight: fontWeight.semibold }}
      >
        {label}
      </Text>
      <TextInput
        accessibilityLabel={label}
        placeholderTextColor={color.textMuted}
        {...input}
        style={{
          color: color.text,
          fontSize: fontSize.body,
          padding: space.md,
          borderRadius: radius.sm,
          borderWidth: 1,
          borderColor: invalid ? color.danger : color.border,
          backgroundColor: color.surface,
        }}
      />
      {invalid ? (
        <Text accessibilityRole="alert" style={{ color: color.danger, fontSize: fontSize.caption }}>
          {error}
        </Text>
      ) : null}
    </View>
  );
}
