# ADR 0013: White label: `brand.json`, `app.config.ts`, EAS profiles, design tokens

## Status

Accepted, 2026-10-02. Milestone 8 implemented it in `apps/mobile` (the buyer app) and `packages/design-tokens`. Android development builds and the app on a phone are not run yet: they need Patrick's Expo account and Android phone ([the milestone log](../../CONTRIBUTING.md#milestone-log)). iOS is configured, not built.

## Context

LiveCart's buyer app is sold to each seller under the seller's own brand: its name under the icon, its colors, its logo, its icons and splash, its URI scheme and its store identifiers, and the storefront it opens on. Each brand is a build of its own, since the name, the identifiers, the icons and the scheme are compiled into the binary; everything else is one code base. Adding a brand must be data, not code, and a mistake in a brand's data must stop the build, not ship an app with another brand's logo or a scheme the API refuses. The app is built with Expo SDK 57 and EAS, from a pnpm workspace, on a free Expo account.

## Decision

### One folder per brand

- `apps/mobile/brands/<id>/brand.json` holds the brand: `id` (the folder's name), `displayName`, the Expo `slug`, the `scheme`, `ios.bundleIdentifier`, `android.package`, the `sellerSlug` of its storefront, three `colors` (`primary`, `onPrimary`, `accent`) and the file names of its `logo`, `icon`, `adaptiveIcon` and `splash`, which sit in the same folder; `easProjectId` is optional until `eas init` creates the project.
- Two brands ship: `demo` (LiveCart Demo, scheme `livecart`, seller `demo-seller`) and `pomelune` (Pomelune, an invented name a web search found unused, scheme `samplelive`, seller `sample-shop` of the seed). Their identifiers, `com.example.livecart.<id>`, are placeholders on a domain reserved for documentation, to replace before any store build, since a store makes them final.
- A brand's scheme must be one of the API's `ALLOWED_APP_SCHEMES`, or its checkout's return URL is refused (ADR 0008). Pomelune keeps `samplelive`, which the API already allows by default, so the allow-list does not change.
- `scripts/generate-brand-assets.mjs` draws placeholder images from a brand's colors (the initial of its name in a disc), with Node's zlib alone and deterministically, until a designer's files replace them under the same names.

### `app.config.ts` validates, then builds

- `APP_BRAND` names the brand (`demo` when unset). `app.config.ts` reads its `brand.json`, validates it against `brand.schema.ts`, a strict zod schema, then checks that the folder's name is the brand's `id` and that every image it names exists. Any problem throws, with every reason at once, so that `expo start`, `expo export`, `expo-doctor` and EAS all stop before using the brand: an unknown brand (the known ones are listed), a field missing, unknown or malformed, a scheme the API never sends a buyer back to (`http`, `exp`...), an image named by a path rather than a file of the folder, or an image missing.
- It then returns the brand's Expo configuration (name, slug, scheme, identifiers, icon, adaptive icon on the primary color, the splash through `expo-splash-screen`'s plugin, typed routes) and the brand itself as `extra.brand`.
- The app reads `extra.brand` back through `expo-constants` and parses it with the same schema at startup. The storefront calls `GET /api/v1/storefronts/{extra.brand.sellerSlug}`; a 404 shows "Store not found", since it means the brand names no seller of that API.
- The logo is the one image the app's JavaScript shows. `@brand/logo` is a module of `metro.config.js`'s own, which evaluates the app configuration again and resolves to the logo of `APP_BRAND`: a build bundles its own brand's logo and no other brand's (an export of `pomelune` holds `brands/pomelune/logo.png` only). Jest maps it to the demo's.
- `app.config.ts` imports `brand.schema.ts` by its full file name: Expo transpiles the configuration file alone, and Node 24 loads what it imports, types stripped, only from a path that names the file (`allowImportingTsExtensions`).

### One EAS profile per brand

`eas.json` has a `base` profile (Node 24.21.0, pnpm 12.6.0, `corepack: false`, the development client, internal distribution, an Android APK, an iOS simulator build) and one profile per brand that extends it and sets `APP_BRAND`. `pnpm run check:pins` compares the profiles' Node and pnpm with `.nvmrc` and `packageManager`. A build of a brand is `npx eas-cli@24.8.0 build --profile <id> --platform android`, from `apps/mobile`, once `eas init` has given the brand its project.

### Design tokens

`packages/design-tokens` holds `tokens.json` (colors, spacing, radii, font sizes) and `src/index.ts`, its types, checked against the JSON with `satisfies`. A brand overrides three colors (`BrandColors`), the others are shared; `BrandProvider` gives the screens the tokens with the brand's colors over them. Font weights are TypeScript constants, because React Native types them as a closed union of strings that a JSON import widens to `string`. The web dashboard keeps its own Tailwind theme, in a few lines of its `app.css`; sharing the tokens with it is on the roadmap.

## Consequences

- Adding a brand is a folder (`brand.json` and four images) and a profile; its scheme in `ALLOWED_APP_SCHEMES` if it is a new one; and an EAS project. No code changes.
- A broken brand never builds: the tests prove the refusals (unknown brand, missing field, missing logo, and the others) on copies of a brand's folder.
- Every brand is one Expo project and one store listing of its own, with its own EAS project id; a free Expo account has a monthly quota of builds shared by all of them.
- In Expo Go, a brand's JavaScript (`APP_BRAND=pomelune npx expo start`) runs under Expo Go's own name and icon, and none of the brands' schemes is registered: buyer links open in the development build only, and Expo Go's payment return goes through `exp://`, which the API takes in development, from a private address, only. The "Add order" screen is the path meant to work in both.
- The placeholder identifiers and images are not fit for a store, by design.

## Alternatives considered

- **One `app.json` per brand**: every brand copies every setting, and nothing checks a copy against the others or against the API's schemes.
- **A brand chosen at run time, from one binary**: the name, the icon, the identifiers and the scheme are compiled in; one binary cannot carry several.
- **Every brand's logo bundled, chosen by `extra.brand`**: simpler than the Metro module, but each brand's app would carry the other brands' images, which a white-label customer would not accept, and the bundle would grow with every brand.
- **Expo's `variant` pattern (one `APP_VARIANT` switch inside `app.config.ts`)**: the same mechanism with the brands' values in code; a folder of data per brand keeps them out of it and validated.
- **A token pipeline (Style Dictionary) generating files for both apps**: more tooling than three overridden colors need while the web has its own theme.
