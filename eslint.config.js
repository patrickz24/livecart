// @ts-check
import js from "@eslint/js";
import { defineConfig, globalIgnores } from "eslint/config";
import globals from "globals";
import tseslint from "typescript-eslint";

export default defineConfig([
  // node_modules/ and .git/ are ignored by ESLint itself.
  globalIgnores([
    // Local agent worktrees: each one is a full copy of the repo.
    ".claude/",
    "**/.venv/",
    "**/dist/",
    "**/coverage/",
    "**/.expo/",
    "apps/mobile/android/",
    "apps/mobile/ios/",
    "packages/api-client/src/generated/",
  ]),
  {
    name: "livecart/javascript",
    files: ["**/*.{js,mjs,cjs}"],
    extends: [js.configs.recommended],
    languageOptions: { globals: globals.node },
  },
  {
    name: "livecart/typescript",
    files: ["**/*.{ts,tsx,mts,cts}"],
    extends: [
      js.configs.recommended,
      tseslint.configs.strictTypeChecked,
      tseslint.configs.stylisticTypeChecked,
    ],
    languageOptions: {
      parserOptions: {
        // Each package's tsconfig.json drives type-aware rules.
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
  },
]);
