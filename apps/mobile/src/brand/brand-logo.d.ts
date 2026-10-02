// `@brand/logo` is the logo of the brand being built: metro.config.js resolves
// it to brands/<APP_BRAND>/<assets.logo>, and jest.config.js to the demo's.
declare module "@brand/logo" {
  import type { ImageSourcePropType } from "react-native";

  const logo: ImageSourcePropType;
  export default logo;
}
