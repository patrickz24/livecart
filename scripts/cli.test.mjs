import assert from "node:assert/strict";
import path from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { isMainModule } from "./cli.mjs";

const thisFile = fileURLToPath(import.meta.url);

describe("isMainModule", () => {
  it("recognises the entry script, whatever the path spelling", () => {
    assert.equal(isMainModule(import.meta.url, thisFile), true);
    const relative = path.relative(process.cwd(), thisFile);
    assert.equal(isMainModule(import.meta.url, relative), true);
  });

  it("ignores letter case on Windows only", { skip: process.platform !== "win32" }, () => {
    assert.equal(isMainModule(import.meta.url, thisFile.toUpperCase()), true);
  });

  it("rejects another script, a missing file and an empty entry", () => {
    const other = fileURLToPath(new URL("./cli.mjs", import.meta.url));
    assert.equal(isMainModule(import.meta.url, other), false);
    assert.equal(
      isMainModule(import.meta.url, path.join(path.dirname(thisFile), "absent.mjs")),
      false,
    );
    // No entry script at all, as under node --eval.
    assert.equal(isMainModule(import.meta.url, ""), false);
  });
});
