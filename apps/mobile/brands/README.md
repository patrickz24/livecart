# Brands

Each folder here is one white-label build of the buyer app: `brand.json` and the four images it names. `app.config.ts` builds the brand that `APP_BRAND` names (`demo` when unset), after checking it against `brand.schema.ts`; any problem fails the start, the export or the build, with every reason. [ADR 0013](../../../docs/adr/0013-white-label-brand-config-eas-profiles-design-tokens.md) explains the decisions.

| Brand      | Name          | Scheme       | Seller (`sellerSlug`) | Identifiers (placeholders)      |
| ---------- | ------------- | ------------ | --------------------- | ------------------------------- |
| `demo`     | LiveCart Demo | `livecart`   | `demo-seller`         | `com.example.livecart.demo`     |
| `pomelune` | Pomelune      | `samplelive` | `sample-shop`         | `com.example.livecart.pomelune` |

Pomelune is an invented name: a quick web search on 2026-10-02 found no company, shop or trademark using it.

## `brand.json`

| Field                  | What it is                                                                                                               |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `id`                   | The folder's name: 2 to 30 lower-case letters or digits                                                                  |
| `displayName`          | The app's name under its icon and in its header                                                                          |
| `slug`                 | The project's slug at Expo (EAS)                                                                                         |
| `scheme`               | The URI scheme of the app's links (`<scheme>://link/LC-XXXXXX#t=...`) and of its payment return URL                      |
| `ios.bundleIdentifier` | The iOS bundle identifier                                                                                                |
| `android.package`      | The Android application id                                                                                               |
| `sellerSlug`           | The seller whose storefront the app shows: `GET /api/v1/storefronts/{sellerSlug}`                                        |
| `colors`               | `primary`, `onPrimary` (text on primary) and `accent`, as `#RRGGBB`, over the shared tokens of `@livecart/design-tokens` |
| `assets`               | The file names of `logo` (shown in the app), `icon`, `adaptiveIcon` (Android) and `splash`, PNG, in this folder          |
| `easProjectId`         | Optional: the brand's EAS project, which `eas init` creates                                                              |

A field missing, unknown or malformed is refused, as are a folder whose name is not the brand's `id` and an image the brand names but its folder lacks.

## Identifiers are placeholders

`com.example.livecart.<brand>` uses `example`, a domain reserved for documentation: these identifiers are placeholders. A bundle identifier and an Android application id are final once a store has published the app, so replace them with identifiers of a domain you own (for instance `dev.<github-handle>.livecart.<brand>`) before any store build. Development builds and Expo Go do not need that.

## Schemes and the API

The payment bridge page sends the buyer back to the app through `<scheme>://payment-return`, and the API takes only the return URLs of `ALLOWED_APP_SCHEMES` (`livecart,samplelive` by default, in `.env.example`). A new brand's scheme must be added there, or its checkout is refused with a 422. Pomelune keeps `samplelive`, the second scheme the API already allows. The buyer links of the comment simulator use `BUYER_LINK_SCHEME` (`livecart`), the demo's.

## Adding a brand

1. Create `brands/<id>/brand.json`, with the seller's slug, a scheme of `ALLOWED_APP_SCHEMES` and identifiers of your own.
2. Draw its images, or generate placeholders from its colors: `node scripts/generate-brand-assets.mjs <id>` (from `apps/mobile`).
3. Add a build profile to `eas.json`: `{ "extends": "base", "env": { "APP_BRAND": "<id>" } }`.
4. Check it: `APP_BRAND=<id> npx expo config --type public` (in PowerShell, `$env:APP_BRAND = "<id>"` first).
