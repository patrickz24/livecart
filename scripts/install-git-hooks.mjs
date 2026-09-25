// Installs the git hooks of lefthook.yml into this clone (`lefthook install`).
//
//   pnpm install                         # runs it as the root "prepare" script
//   node scripts/install-git-hooks.mjs   # by hand; or: pnpm exec lefthook install
//
// pnpm runs the root project's lifecycle scripts on every full install. It
// does not always run a dependency's build script: once its store holds the
// result of lefthook's postinstall (the side-effects cache), a second clone on
// the same machine got no hook at all, silently. So lefthook's own
// postinstall is denied in pnpm-workspace.yaml and this script replaces it.
//
// Skipped, like lefthook's postinstall, when CI is set (unless LEFTHOOK is
// set too), and outside a git clone (an archive, a Docker build context).
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { isMainModule } from "./cli.mjs";

const repoRoot = fileURLToPath(new URL("..", import.meta.url));

const isEnabled = (value) => Boolean(value) && value !== "0" && value !== "false";

/** Why the hooks must not be installed here, or null when they must be. */
export function skipReason(env, root) {
  if (isEnabled(env.CI) && !isEnabled(env.LEFTHOOK)) return "CI is set";
  // A directory in a clone, a file in a worktree.
  if (!existsSync(path.join(root, ".git"))) return "not a git clone";
  return null;
}

function installHooks() {
  const reason = skipReason(process.env, repoRoot);
  if (reason !== null) {
    console.log(`install-git-hooks: skipped (${reason}).`);
    return;
  }
  // lefthook's launcher finds the native binary of this platform.
  const launcher = createRequire(import.meta.url).resolve("lefthook");
  const result = spawnSync(process.execPath, [launcher, "install"], {
    cwd: repoRoot,
    stdio: "inherit",
  });
  if (result.status !== 0) {
    console.error(
      "install-git-hooks: `lefthook install` failed, so this clone has no git hook. " +
        "Fix the error above, then run `pnpm exec lefthook install`.",
    );
    process.exit(1);
  }
}

if (isMainModule(import.meta.url)) {
  installHooks();
}
