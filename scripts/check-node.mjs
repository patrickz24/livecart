// Fails unless the Node that runs it is the version of .nvmrc.
//
//   pnpm run check:node
//
// `make lint` and `make test` run it first, through pnpm, so it checks the
// Node that pnpm's scripts, Vitest and the node:test suites really get, which
// can differ from the shell's `node -v` (see CONTRIBUTING.md).
import { isMainModule, requirePinnedNode } from "./cli.mjs";

if (isMainModule(import.meta.url)) {
  console.log(`ok    Node ${requirePinnedNode()} (.nvmrc)`);
}
