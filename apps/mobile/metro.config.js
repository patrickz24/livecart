// Metro, as Expo configures it, plus one module of its own: `@brand/logo`,
// the logo of the brand being built. Expo evaluates app.config.ts again here
// (APP_BRAND, validated), so the bundle holds the logo of its own brand and
// no other brand's image.
const path = require("node:path");

const { getConfig } = require("expo/config");
const { getDefaultConfig } = require("expo/metro-config");

const BRAND_LOGO = "@brand/logo";

const config = getDefaultConfig(__dirname);
const { exp } = getConfig(__dirname, { skipSDKVersionRequirement: true });
const brand = exp.extra?.brand;
const logo = path.join(__dirname, "brands", brand.id, brand.assets.logo);

const resolveRequest = config.resolver.resolveRequest;
config.resolver.resolveRequest = (context, moduleName, platform) => {
  if (moduleName === BRAND_LOGO) {
    return context.resolveRequest(context, logo, platform);
  }
  return (resolveRequest ?? context.resolveRequest)(context, moduleName, platform);
};

module.exports = config;
