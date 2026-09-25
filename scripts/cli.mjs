// Helpers shared by the command-line scripts of this folder. They only use
// APIs that every maintained Node release has: a script started by the wrong
// Node must fail loudly, never exit 0 without doing anything.
import { realpathSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

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
