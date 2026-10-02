import { getOrderDetails } from "@livecart/api-client";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "expo-router";
import { useState } from "react";
import { ScrollView, Text } from "react-native";

import { useApiClient } from "../api/ApiProvider";
import { describeError, isNotFound, unwrap } from "../api/errors";
import { useBrandTheme } from "../brand/brandTheme";
import { Button } from "../ui/Button";
import { TextField } from "../ui/TextField";

import { storedOrdersKey, useOrderAccess } from "./OrderAccessProvider";
import { isOrderToken, parseOrderLink, parseOrderReference } from "./reference";

interface Problems {
  reference?: string;
  token?: string;
  form?: string;
}

/**
 * "Add order": the buyer pastes the reference and the token they received
 * (or the whole link, which fills both). The app checks the pair with the
 * API, the token in the Authorization header, then keeps the token in
 * SecureStore: it never goes through a URL. This is the path that works in
 * Expo Go too, which cannot open the brand's links.
 */
export function AddOrderScreen() {
  const client = useApiClient();
  const store = useOrderAccess();
  const queryClient = useQueryClient();
  const router = useRouter();
  const { space, color, fontSize } = useBrandTheme();
  const [reference, setReference] = useState("");
  const [token, setToken] = useState("");
  const [problems, setProblems] = useState<Problems>({});

  const add = useMutation({
    mutationFn: async (order: { reference: string; token: string }) => {
      await unwrap(
        getOrderDetails({ client, path: { reference: order.reference }, auth: order.token }),
      );
      return store.save(order.reference, order.token);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: storedOrdersKey });
      router.dismissTo("/orders");
    },
    onError: (error) => {
      setProblems({
        form: isNotFound(error)
          ? "No order has this reference and token. Check both, as you received them."
          : describeError(error),
      });
    },
  });

  const changeReference = (text: string) => {
    const link = parseOrderLink(text);
    if (link === null) {
      setReference(text);
    } else {
      setReference(link.reference);
      setToken(link.token);
    }
    setProblems({});
  };

  const submit = () => {
    const canonical = parseOrderReference(reference);
    const trimmed = token.trim();
    const found: Problems = {};
    if (canonical === null) {
      found.reference = "A reference is LC- then six letters or digits, such as LC-7K3F9Q.";
    }
    if (!isOrderToken(trimmed)) {
      found.token = "The token is the 43-character code you received with the reference.";
    }
    setProblems(found);
    if (canonical !== null && found.token === undefined) {
      add.mutate({ reference: canonical, token: trimmed });
    }
  };

  return (
    <ScrollView
      style={{ backgroundColor: color.background }}
      contentContainerStyle={{ padding: space.lg, gap: space.lg }}
      keyboardShouldPersistTaps="handled"
    >
      <Text style={{ color: color.textMuted, fontSize: fontSize.body }}>
        Paste the reference and the token of your order, as you received them, or the whole link.
      </Text>
      <TextField
        label="Reference"
        value={reference}
        onChangeText={changeReference}
        placeholder="LC-7K3F9Q"
        autoCapitalize="characters"
        autoCorrect={false}
        autoComplete="off"
        error={problems.reference}
      />
      <TextField
        label="Token"
        value={token}
        onChangeText={(text) => {
          setToken(text);
          setProblems({});
        }}
        secureTextEntry
        autoCapitalize="none"
        autoCorrect={false}
        autoComplete="off"
        importantForAutofill="no"
        error={problems.token}
      />
      {problems.form === undefined ? null : (
        <Text accessibilityRole="alert" style={{ color: color.danger, fontSize: fontSize.body }}>
          {problems.form}
        </Text>
      )}
      <Button label="Add the order" onPress={submit} busy={add.isPending} />
    </ScrollView>
  );
}
