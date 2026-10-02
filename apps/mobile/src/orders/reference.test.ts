import { describe, expect, it } from "@jest/globals";

import { tokenAt } from "../test/memoryStorage";

import { parseOrderLink, parseOrderReference, readLinkFragment } from "./reference";

describe("order references", () => {
  it.each([
    ["LC-7K3F9Q", "LC-7K3F9Q"],
    ["lc-7k3f9q", "LC-7K3F9Q"],
    ["LC7K3F9Q", "LC-7K3F9Q"],
    ["  LC-7K3F9Q \n", "LC-7K3F9Q"],
    ["LC-7K3F9O", "LC-7K3F90"],
    ["LC-IL0000", "LC-110000"],
    ["ＬＣ－７Ｋ３Ｆ９Ｑ", "LC-7K3F9Q"],
  ])("reads %j as %s", (typed, canonical) => {
    expect(parseOrderReference(typed)).toBe(canonical);
  });

  it.each(["LC-7K3F9", "LC-7K3F9QQ", "LC-7K3F9U", "XX-7K3F9Q", "LC 7K3F9Q", ""])(
    "refuses %j",
    (typed) => {
      expect(parseOrderReference(typed)).toBeNull();
    },
  );
});

describe("buyer links", () => {
  const token = tokenAt(3);

  it("reads the token of a link's fragment", () => {
    expect(readLinkFragment(`t=${token}`)).toBe(token);
    expect(readLinkFragment(`#t=${token}`)).toBe(token);
  });

  it("finds no token in a fragment without one, or with a malformed one", () => {
    expect(readLinkFragment(undefined)).toBeNull();
    expect(readLinkFragment("")).toBeNull();
    expect(readLinkFragment(`token=${token}`)).toBeNull();
    expect(readLinkFragment("t=short")).toBeNull();
  });

  it("splits a whole link pasted as text", () => {
    expect(parseOrderLink(` livecart://link/LC-7K3F9Q#t=${token} `)).toEqual({
      reference: "LC-7K3F9Q",
      token,
    });
    expect(parseOrderLink(`samplelive://link/lc7k3f9q#t=${token}`)).toEqual({
      reference: "LC-7K3F9Q",
      token,
    });
  });

  it("does not take a reference, or a link without its token, for a link", () => {
    expect(parseOrderLink("LC-7K3F9Q")).toBeNull();
    expect(parseOrderLink("livecart://link/LC-7K3F9Q")).toBeNull();
    expect(parseOrderLink(`https://example.test/link/LC-7K3F9Q?t=${token}`)).toBeNull();
  });
});
