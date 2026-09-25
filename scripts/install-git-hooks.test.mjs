import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { after, describe, it } from "node:test";

import { skipReason } from "./install-git-hooks.mjs";

describe("skipReason", () => {
  const scratch = mkdtempSync(path.join(tmpdir(), "livecart-hooks-"));
  after(() => rmSync(scratch, { recursive: true, force: true }));

  const clone = path.join(scratch, "clone");
  mkdirSync(path.join(clone, ".git"), { recursive: true });
  const worktree = path.join(scratch, "worktree");
  mkdirSync(worktree);
  writeFileSync(path.join(worktree, ".git"), "gitdir: ../clone/.git/worktrees/w\n");
  const archive = path.join(scratch, "archive");
  mkdirSync(archive);

  it("installs the hooks in a clone or a worktree on a developer machine", () => {
    assert.equal(skipReason({}, clone), null);
    assert.equal(skipReason({ CI: "false" }, worktree), null);
    assert.equal(skipReason({ CI: "0" }, clone), null);
  });

  it("skips CI, unless LEFTHOOK asks for the hooks", () => {
    assert.equal(skipReason({ CI: "true" }, clone), "CI is set");
    assert.equal(skipReason({ CI: "1", LEFTHOOK: "0" }, clone), "CI is set");
    assert.equal(skipReason({ CI: "true", LEFTHOOK: "1" }, clone), null);
  });

  it("skips a directory that is not a git clone", () => {
    assert.equal(skipReason({}, archive), "not a git clone");
  });
});
