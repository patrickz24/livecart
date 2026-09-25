// Conventional Commits, with scopes limited to the monorepo members and
// cross-cutting areas. Commits touching the whole repo use no scope.
export default {
  extends: ["@commitlint/config-conventional"],
  rules: {
    "scope-enum": [
      2,
      "always",
      [
        "core",
        "api",
        "worker",
        "simulator",
        "web",
        "mobile",
        "api-client",
        "design-tokens",
        "infra",
        "loadtest",
        "deps",
      ],
    ],
  },
};
