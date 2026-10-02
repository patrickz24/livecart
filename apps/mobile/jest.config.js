// Jest 29 with the jest-expo preset (jest-expo 57 does not run on Jest 30).
/** @type {import("jest").Config} */
module.exports = {
  preset: "jest-expo",
  // pnpm keeps each package under node_modules/.pnpm/<name>@<version>/node_modules,
  // where the preset's pattern does not see React Native's packages, which ship
  // untranspiled code: let Babel transform them there too.
  transformIgnorePatterns: [
    "node_modules/(?!(?:\\.pnpm/[^/]+/node_modules/)?(?:(?:jest-)?react-native|@react-native(?:-community)?|expo(?:nent)?|@expo(?:nent)?/.*|@expo-google-fonts/.*|react-navigation|@react-navigation/.*|@unimodules/.*|unimodules|react-native-svg))",
  ],
  testPathIgnorePatterns: ["/node_modules/", "/.expo/"],
};
