import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import {
  checkPins,
  dockerfileImages,
  loadRepositoryFiles,
  lowerBound,
  splitImage,
  yamlImages,
} from "./check-pins.mjs";

const CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1";

/** A minimal repository whose pins all agree. */
function consistentRepository() {
  return {
    ".nvmrc": "24.21.0\n",
    ".python-version": "3.12.14\n",
    "package.json": JSON.stringify({
      packageManager: "pnpm@12.6.0",
      engines: { node: ">=24.21.0 <25", pnpm: ">=12.6.0 <13" },
    }),
    "pyproject.toml": [
      "[tool.uv]",
      'required-version = ">=0.12.19,<0.13"',
      "[tool.uv.workspace]",
      'members = ["packages/core"]',
    ].join("\n"),
    "packages/core/pyproject.toml": 'requires = ["uv_build>=0.12.19,<0.13.0"]\n',
    "infra/docker/python.Dockerfile": [
      "FROM python:3.12.14-slim-trixie AS builder",
      "COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /bin/uv",
      "FROM python:3.12.14-slim-trixie AS runtime",
      "COPY --from=builder /app/.venv /app/.venv",
    ].join("\n"),
    "infra/docker/web.Dockerfile": [
      "FROM node:24.21.0-alpine AS build",
      "RUN npm install -g pnpm@12.6.0",
      "FROM nginxinc/nginx-unprivileged:1.30.5-alpine",
      "COPY --from=build /repo/dist /usr/share/nginx/html",
    ].join("\n"),
    "compose.yaml": [
      "services:",
      "  postgres:",
      "    image: postgres:16.15-alpine",
      "  redis:",
      "    image: ${REDIS_IMAGE:-redis:8.10.2-alpine}",
      "  api:",
      "    image: livecart-python:local",
    ].join("\n"),
    ".github/workflows/ci.yml": [
      "env:",
      '  UV_VERSION: "0.12.19"',
      '  PNPM_VERSION: "12.6.0"',
      '  TRIVY_VERSION: "v0.74.0"',
      "jobs:",
      "  test:",
      "    services:",
      "      postgres:",
      "        image: postgres:16.15-alpine",
      "    steps:",
      `      - uses: ${CHECKOUT}`,
      "      - uses: ./.github/actions/setup",
    ].join("\n"),
    ".github/actions/setup/action.yml": [
      "inputs:",
      "  pnpm-version: {}",
      "runs:",
      "  steps:",
      "    - uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0",
      "      with:",
      "        node-version-file: .nvmrc",
      "    - env:",
      "        PNPM_VERSION: ${{ inputs.pnpm-version }}",
    ].join("\n"),
  };
}

/** Problems found after replacing `search` with `replacement` in one file. */
function problemsAfter(file, search, replacement) {
  const files = consistentRepository();
  assert.ok(files[file].includes(search), `fixture ${file} contains ${search}`);
  files[file] = files[file].replace(search, replacement);
  return checkPins(files).problems;
}

describe("helpers", () => {
  it("reads the lower bound of a version specifier", () => {
    assert.equal(lowerBound(">=0.12.19,<0.13"), "0.12.19");
    assert.equal(lowerBound(">=24.21.0 <25"), "24.21.0");
    assert.equal(lowerBound("<13"), undefined);
  });

  it("splits image references, registry ports and digests included", () => {
    assert.deepEqual(splitImage("ghcr.io/astral-sh/uv:0.12.19"), {
      name: "ghcr.io/astral-sh/uv",
      tag: "0.12.19",
      digest: undefined,
    });
    assert.equal(splitImage("localhost:5000/app").tag, undefined);
    assert.equal(splitImage("redis@sha256:abc").digest, "sha256:abc");
  });

  it("skips build stages in Dockerfiles", () => {
    const refs = dockerfileImages(consistentRepository()["infra/docker/python.Dockerfile"]);
    assert.deepEqual(
      refs.map((image) => image.ref),
      ["python:3.12.14-slim-trixie", "ghcr.io/astral-sh/uv:0.12.19", "python:3.12.14-slim-trixie"],
    );
  });

  it("resolves the default of an image variable", () => {
    const [image] = yamlImages("    image: ${REDIS_IMAGE:-valkey/valkey:9.1.2-alpine}");
    assert.equal(image?.name, "valkey/valkey");
    assert.equal(image?.tag, "9.1.2-alpine");
  });
});

describe("checkPins", () => {
  it("accepts a repository whose pins agree", () => {
    const { passed, problems } = checkPins(consistentRepository());
    assert.deepEqual(problems, []);
    assert.equal(passed.length, 6);
  });

  it("detects a Node image that differs from .nvmrc", () => {
    const problems = problemsAfter("infra/docker/web.Dockerfile", "node:24.21.0", "node:24.20.0");
    assert.match(problems.join("\n"), /Node: infra\/docker\/web\.Dockerfile:1 has 24\.20\.0/);
  });

  it("detects a setup-node step that does not read .nvmrc", () => {
    const problems = problemsAfter(
      ".github/actions/setup/action.yml",
      "node-version-file: .nvmrc",
      "node-version: 24.x",
    );
    assert.match(problems.join("\n"), /Node: .*node-version has 24\.x/);
  });

  it("detects a pnpm version that differs from packageManager", () => {
    const problems = problemsAfter(
      ".github/workflows/ci.yml",
      'PNPM_VERSION: "12.6.0"',
      'PNPM_VERSION: "12.5.0"',
    );
    assert.match(problems.join("\n"), /pnpm: .*PNPM_VERSION has 12\.5\.0, expected 12\.6\.0/);
  });

  it("detects a Python image that differs from .python-version", () => {
    const problems = problemsAfter(
      "infra/docker/python.Dockerfile",
      "FROM python:3.12.14-slim-trixie AS runtime",
      "FROM python:3.12.13-slim-trixie AS runtime",
    );
    assert.match(problems.join("\n"), /CPython: infra\/docker\/python\.Dockerfile:3 has 3\.12\.13/);
  });

  it("detects a uv copy that differs from required-version", () => {
    const problems = problemsAfter(
      "packages/core/pyproject.toml",
      "uv_build>=0.12.19",
      "uv_build>=0.12.18",
    );
    assert.match(problems.join("\n"), /uv: packages\/core\/pyproject\.toml uv_build has 0\.12\.18/);
  });

  it("reports a missing copy or file instead of passing silently", () => {
    const files = consistentRepository();
    files["infra/docker/python.Dockerfile"] = "FROM debian:13.1-slim\n";
    assert.match(
      checkPins(files).problems.join("\n"),
      /CPython: no copy found to compare with \.python-version/,
    );
    delete files["infra/docker/python.Dockerfile"];
    assert.match(checkPins(files).problems.join("\n"), /python\.Dockerfile: file not found/);
  });

  it("requires explicit version tags and one tag per image", () => {
    assert.match(
      problemsAfter("compose.yaml", "postgres:16.15-alpine", "postgres:latest").join("\n"),
      /postgres:latest needs an explicit version tag/,
    );
    assert.match(
      problemsAfter(
        ".github/workflows/ci.yml",
        "postgres:16.15-alpine",
        "postgres:16.14-alpine",
      ).join("\n"),
      /uses postgres:16\.14-alpine, but compose\.yaml:3 uses 16\.15-alpine/,
    );
  });

  it("requires actions pinned by full SHA with a release comment", () => {
    assert.match(
      problemsAfter(".github/workflows/ci.yml", CHECKOUT, "actions/checkout@v7").join("\n"),
      /actions\/checkout@v7 must be pinned by a full commit SHA/,
    );
    assert.match(
      problemsAfter(".github/workflows/ci.yml", " # v7.0.1", "").join("\n"),
      /needs a "# vX\.Y\.Z" comment/,
    );
  });

  it("requires exact versions for the workflow tools", () => {
    const problems = problemsAfter(".github/workflows/ci.yml", '"v0.74.0"', "latest");
    assert.match(problems.join("\n"), /TRIVY_VERSION=latest is not an exact version/);
  });
});

describe("this repository", () => {
  it("has every pin in agreement", () => {
    const root = fileURLToPath(new URL("..", import.meta.url));
    assert.deepEqual(checkPins(loadRepositoryFiles(root)).problems, []);
  });
});
