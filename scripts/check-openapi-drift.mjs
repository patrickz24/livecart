// Fails when the committed OpenAPI schema or generated client differs from what
// the code produces. Run through `pnpm run check:openapi`, which regenerates
// both first (`pnpm run gen-api`); this script only asks git what changed, so
// it also catches generated files that were added or deleted.
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const repoRoot = fileURLToPath(new URL("..", import.meta.url));
const generatedPaths = ["packages/api-client/openapi.json", "packages/api-client/src/generated"];

function git(...args) {
  return execFileSync("git", args, { cwd: repoRoot, encoding: "utf8" });
}

const changes = git("status", "--porcelain", "--untracked-files=all", "--", ...generatedPaths);

if (changes.trim() !== "") {
  console.error("The generated API contract is out of date:\n");
  console.error(changes);
  console.error(git("--no-pager", "diff", "--stat", "--", ...generatedPaths));
  console.error(
    "Run `pnpm run gen-api` from the repository root and commit the result " +
      "(openapi.json and src/generated are never edited by hand).",
  );
  process.exit(1);
}

console.log("The generated API contract matches the code.");
