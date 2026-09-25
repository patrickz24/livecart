import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import path from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { isMainModule, nodeVersionProblem } from "./cli.mjs";

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

describe("nodeVersionProblem", () => {
  it("accepts the version of .nvmrc, with or without a v and a newline", () => {
    assert.equal(nodeVersionProblem("24.21.0\n", "24.21.0"), null);
    assert.equal(nodeVersionProblem("v24.21.0", "24.21.0"), null);
  });

  it("explains how to check and fix another version", () => {
    const problem = nodeVersionProblem("24.21.0\n", "20.19.0");

    assert.match(problem ?? "", /Node 20\.19\.0 runs this command, but \.nvmrc pins 24\.21\.0/);
    assert.match(problem ?? "", /`pnpm exec node -v` prints v24\.21\.0/);
  });
});

describe("check-node.mjs", () => {
  it("reports the pinned Node when node starts it", () => {
    const script = fileURLToPath(new URL("./check-node.mjs", import.meta.url));
    const output = execFileSync(process.execPath, [script], { encoding: "utf8" });

    assert.equal(output.trim(), `ok    Node ${process.versions.node} (.nvmrc)`);
  });
});
