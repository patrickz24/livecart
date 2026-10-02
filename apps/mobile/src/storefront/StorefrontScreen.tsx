import { fontWeight } from "@livecart/design-tokens";
import {
  getStorefront,
  type StorefrontProduct,
  type StorefrontResponse,
} from "@livecart/api-client";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { ActivityIndicator, FlatList, RefreshControl, Text, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";

import { useApiClient } from "../api/ApiProvider";
import { describeError, isNotFound, unwrap } from "../api/errors";
import { BrandBanner } from "../brand/BrandBanner";
import { useBrandTheme } from "../brand/brandTheme";
import { Notice } from "../ui/Notice";
import { formatMoney } from "../ui/format";

export const storefrontKey = (sellerSlug: string) => ["storefront", sellerSlug] as const;

/**
 * The live session of the brand's seller, and its products: what is on sale
 * now, at what price, and how many are left. The buyer orders by commenting
 * on the live; this screen does not reserve anything.
 */
export function StorefrontScreen() {
  const client = useApiClient();
  const { brand, color, space } = useBrandTheme();
  const storefront = useQuery({
    queryKey: storefrontKey(brand.sellerSlug),
    queryFn: ({ signal }) =>
      unwrap(getStorefront({ client, path: { seller_slug: brand.sellerSlug }, signal })),
  });

  const session = storefront.data?.live_session ?? null;
  const subtitle = storefront.isSuccess
    ? (session?.title ?? "No live right now")
    : storefront.isPending
      ? "Loading the live…"
      : undefined;

  return (
    <SafeAreaView edges={["top"]} style={{ flex: 1, backgroundColor: color.primary }}>
      <BrandBanner subtitle={subtitle} />
      <FlatList
        style={{ backgroundColor: color.background }}
        contentContainerStyle={{ padding: space.lg, gap: space.md, flexGrow: 1 }}
        data={session?.products ?? []}
        keyExtractor={(product) => `${product.code} ${product.variant}`}
        renderItem={({ item }) => <ProductRow product={item} />}
        refreshControl={
          <RefreshControl
            refreshing={storefront.isRefetching}
            onRefresh={() => void storefront.refetch()}
            colors={[color.primary]}
          />
        }
        ListHeaderComponent={session === null ? null : <HowToBuy />}
        ListEmptyComponent={<EmptyStorefront storefront={storefront} />}
      />
    </SafeAreaView>
  );
}

/** What the list shows without products: loading, a failure, no live, or a live with nothing. */
function EmptyStorefront({ storefront }: { storefront: UseQueryResult<StorefrontResponse> }) {
  const { brand, color } = useBrandTheme();
  const retry = { label: "Try again", onPress: () => void storefront.refetch() };
  if (storefront.isPending) {
    return <ActivityIndicator color={color.primary} accessibilityLabel="Loading" />;
  }
  if (storefront.isError) {
    // A 404 is the brand's own configuration: its sellerSlug names no seller of this API.
    return isNotFound(storefront.error) ? (
      <Notice
        tone="error"
        title="Store not found"
        message={`This app's store, ${brand.sellerSlug}, does not exist on this server.`}
        action={retry}
      />
    ) : (
      <Notice
        tone="error"
        title="The live cannot be loaded"
        message={describeError(storefront.error)}
        action={retry}
      />
    );
  }
  if (storefront.data.live_session === null) {
    return (
      <Notice
        title="No live right now"
        message={`${brand.displayName} is not live at the moment. Pull down to check again.`}
      />
    );
  }
  return <Notice title="Nothing on sale yet" message="The seller has not added products." />;
}

function HowToBuy() {
  const { color, fontSize } = useBrandTheme();
  return (
    <Text style={{ color: color.textMuted, fontSize: fontSize.body }}>
      To buy, comment a product's code on the live, such as “A12 M”. You then get a reference and a
      link: add the order under My orders to pay for it.
    </Text>
  );
}

function ProductRow({ product }: { product: StorefrontProduct }) {
  const { color, space, radius, fontSize } = useBrandTheme();
  const soldOut = product.available === 0;
  const code = product.variant === "" ? product.code : `${product.code} ${product.variant}`;
  return (
    <View
      accessibilityLabel={`${code}, ${product.name}`}
      style={{
        flexDirection: "row",
        alignItems: "center",
        gap: space.md,
        padding: space.md,
        borderRadius: radius.md,
        backgroundColor: color.surface,
        borderWidth: 1,
        borderColor: color.border,
      }}
    >
      <View
        style={{
          paddingHorizontal: space.sm,
          paddingVertical: space.xs,
          borderRadius: radius.sm,
          backgroundColor: color.accent,
        }}
      >
        <Text style={{ color: color.text, fontWeight: fontWeight.bold }}>{code}</Text>
      </View>
      <View style={{ flex: 1 }}>
        <Text style={{ color: color.text, fontSize: fontSize.body }}>{product.name}</Text>
        <Text
          style={{ color: soldOut ? color.danger : color.textMuted, fontSize: fontSize.caption }}
        >
          {soldOut ? "Sold out" : `${String(product.available)} left`}
        </Text>
      </View>
      <Text style={{ color: color.text, fontSize: fontSize.body, fontWeight: fontWeight.semibold }}>
        {formatMoney(product.price_minor, product.currency)}
      </Text>
    </View>
  );
}
