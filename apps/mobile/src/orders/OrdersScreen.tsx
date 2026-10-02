import { fontWeight } from "@livecart/design-tokens";
import { useRouter } from "expo-router";
import { ActivityIndicator, FlatList, Text, View } from "react-native";

import { useBrandTheme } from "../brand/brandTheme";
import { Button } from "../ui/Button";
import { Notice } from "../ui/Notice";

import { useStoredReferences } from "./OrderAccessProvider";

/**
 * The orders this phone holds a token for, by reference, newest first. The
 * list reads SecureStore only: each order's status is fetched when it is
 * opened, so that opening the list costs the API nothing.
 */
export function OrdersScreen() {
  const router = useRouter();
  const { color, space } = useBrandTheme();
  const references = useStoredReferences();
  const addOrder = () => {
    router.push("/orders/add");
  };

  if (references.isPending) {
    return <ActivityIndicator style={{ margin: space.xl }} color={color.primary} />;
  }
  return (
    <FlatList
      style={{ backgroundColor: color.background }}
      contentContainerStyle={{ padding: space.lg, gap: space.md, flexGrow: 1 }}
      data={references.data ?? []}
      keyExtractor={(reference) => reference}
      renderItem={({ item }) => <OrderRow reference={item} />}
      ListEmptyComponent={
        <Notice
          title="No orders yet"
          message="Comment a product's code on the live: you then receive a reference and a link. Open the link, or add the order here with its reference and token."
          action={{ label: "Add an order", onPress: addOrder }}
        />
      }
      ListFooterComponent={
        references.data?.length === 0 ? null : <Button label="Add an order" onPress={addOrder} />
      }
    />
  );
}

function OrderRow({ reference }: { reference: string }) {
  const { color, space, radius, fontSize } = useBrandTheme();
  return (
    <View
      style={{
        padding: space.lg,
        borderRadius: radius.md,
        borderWidth: 1,
        borderColor: color.border,
        backgroundColor: color.surface,
      }}
    >
      <Text style={{ color: color.text, fontSize: fontSize.title, fontWeight: fontWeight.bold }}>
        {reference}
      </Text>
    </View>
  );
}
