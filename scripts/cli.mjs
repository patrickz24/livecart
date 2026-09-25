// Helpers shared by the command-line scripts of this folder. They only use
// APIs that every maintained Node release has: a script started by the wrong
// Node must fail loudly, never exit 0 without doing anything.
import { readFileSync, realpathSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = fileURLToPath(new URL("..", import.meta.url));

/** Why running Node `actual` is wrong when .nvmrc holds `pinned`, or null. */
export function nodeVersionProblem(pinned, actual) {
  const wanted = pinned.trim().replace(/^v/, "");
  if (actual === wanted) return null;
  return [
    `Node ${actual} runs this command, but .nvmrc pins ${wanted}.`,
    "pnpm, make and the git hooks start the first `node` of the PATH that their child",
    "processes see, which is not always the one `node -v` reports in your shell.",
    `Put Node ${wanted} first on PATH (CONTRIBUTING.md, "Node version seen by pnpm"),`,
    `then check that \`pnpm exec node -v\` prints v${wanted}.`,
  ].join("\n");
}

/**
 * Exit with a readable error unless this process runs the Node of .nvmrc.
 * Without it, a wrong Node shows up later as unrelated errors (Vitest crashing
 * in jsdom, `node --test` not expanding its glob). Returns the version.
 */
export function requirePinnedNode() {
  const pinned = readFileSync(path.join(repoRoot, ".nvmrc"), "utf8");
  const problem = nodeVersionProblem(pinned, process.versions.node);
  if (problem !== null) {
    console.error(`FAIL  ${problem.split("\n").join("\n      ")}`);
    process.exit(1);
  }
  return process.versions.node;
}

/** Absolute real path, compared case-insensitively on Windows. */
function canonicalPath(file) {
  const real = realpathSync(path.resolve(file));
  return process.platform === "win32" ? real.toLowerCase() : real;
}

/**
 * Whether the module whose `import.meta.url` is `moduleUrl` is the script that
 * `node` was started with, rather than a module imported by another one (a
 * test, for example). `import.meta.main` answers the same question, but only
 * from Node 24.2: on an older Node it is undefined, so a script guarded by it
 * would exit 0 without running anything.
 */
export function isMainModule(moduleUrl, entry = process.argv[1]) {
  if (!entry) return false;
  try {
    return canonicalPath(fileURLToPath(moduleUrl)) === canonicalPath(entry);
  } catch {
    // The entry is not a file (node --eval, a REPL): not this module.
    return false;
  }
}
