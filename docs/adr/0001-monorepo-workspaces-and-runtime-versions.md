# ADR 0001: Monorepo: pnpm and uv workspaces, Node, Python and React versions

## Status

Accepted, 2026-09-25.

## Context

LiveCart is one repository with two ecosystems: Python for the shared core, the API and the worker (`packages/livecart-core`, `services/api`, `services/worker`), and TypeScript for the generated API client and the apps (`packages/api-client`, `apps/web`, later `apps/mobile` and `packages/design-tokens`). `packages/` holds members of both ecosystems. The project is a solo portfolio MVP: every tooling choice must be explainable in an interview, reproducible from a fresh clone on Windows (Git Bash and PowerShell 5.1), Linux and CI, and must not need a build orchestrator at this size.

## Decision

- **One workspace and one lockfile per ecosystem.** The root `pyproject.toml` is a virtual uv workspace (no `[project]` table of its own): it lists the members explicitly, holds the shared `dev` dependency group and the tool configuration, and `uv.lock` covers every member. The root `package.json` is a private pnpm workspace; `pnpm-workspace.yaml` lists the JS members explicitly, because a glob on `packages/*` would catch the uv member, and `pnpm-lock.yaml` covers them. Members that do not exist yet are not listed: the simulator joins the uv workspace in milestone 4a, `apps/mobile` and `packages/design-tokens` join the pnpm workspace in milestone 8.
- **Locked installs everywhere.** `uv sync --locked` and `uv run --locked` on developer machines, in CI and in the Python image; `pnpm install --frozen-lockfile` in CI and in the web image. Each Python member uses the `uv_build` backend and `requires-python = ">=3.12,<3.13"`; the API and the worker depend on `livecart-core` as a workspace source.
- **Node 24.21.0**, pinned in `.nvmrc` (the source of truth) and mirrored by `engines.node` (`>=24.21.0 <25`), the `node:24.21.0-alpine` build stage of the web image and `node-version-file: .nvmrc` in CI. Developers manage it per folder with fnm. `pnpm run check:node` (`scripts/check-node.mjs`) fails unless the Node that runs it is the one of `.nvmrc`; `make lint` and `make test` run it first, and `check-pins.mjs` and `smoke-compose.mjs` call the same guard, because on Windows the child processes of pnpm can pick another Node from the `PATH` and then fail with errors that never mention the version. The scripts detect their entry point with a real-path comparison rather than `import.meta.main`, so that an older Node fails loudly instead of exiting 0 without running anything.
- **pnpm 12.6.0**, pinned by `packageManager` in `package.json` (the source of truth) and mirrored by `engines.pnpm` (`>=12.6.0 <13`), the web image and `PNPM_VERSION` in CI. pnpm is installed with `npm install -g` on developer machines, in CI and in the web image; Corepack is not used.
- **CPython 3.12.14**, pinned in `.python-version` (installed by uv) and mirrored by the `python:3.12.14-slim-trixie` base image. **uv 0.12.19**, pinned by `[tool.uv] required-version = ">=0.12.19,<0.13"` and mirrored by the `uv_build` requirement of each member, the `ghcr.io/astral-sh/uv:0.12.19` image stage and `UV_VERSION` in CI.
- **TypeScript 6.0.3**, kept below 6.1.0, the upper bound that typescript-eslint 8.70.1 supports, with a strict shared `tsconfig.base.json` (`strict`, `noUncheckedIndexedAccess`, `verbatimModuleSyntax`, `isolatedModules`) that each JS member extends.
- **React 19.3.0 on the web** (`apps/web`). The mobile app of milestone 8 will use React 19.2.3, the version Expo SDK 57 imposes, while the dashboard needs at least 19.2.7 for react-router 8.4.0 (milestone 7). Two versions are acceptable because `packages/` does not depend on React and each app has its own dependency tree.
- **`pnpm run check:pins`** (`scripts/check-pins.mjs`, covered by `node:test`) fails when a copy of the Node, pnpm, CPython or uv version drifts from its source of truth, when a compose or Dockerfile image lacks an explicit version tag or is tagged differently in two files, and when a GitHub Action is not pinned by a full commit SHA, so a tool is upgraded by changing the source and every copy in one commit. The TypeScript and React versions are not read by the script: their copies (`typescript` in the root, `apps/web` and `packages/api-client`; `react`, `react-dom` and their `@types` in `apps/web`) are kept in step by hand.

## Consequences

- A fresh clone needs `uv sync --locked` and `pnpm install`; nothing is resolved at install time. Both lockfiles are committed and reviewed like code.
- Two package managers and two lockfiles to maintain, but no cross-ecosystem orchestrator to configure or explain: `make` targets and `pnpm -r` run every check.
- Every version lives in one place and its copies are checked; upgrading Node, pnpm, CPython or uv touches several files at once, and CONTRIBUTING.md lists them.
- The Node guard adds one step to `make lint` and `make test`, and a documented `PATH` fix on Windows.
- A contributor with another uv or CPython gets a clear error from `required-version` or `.python-version` instead of a different resolution.
- Trivy 0.74.0 cannot parse the `uv.lock` of a virtual workspace root; ADR 0015 covers Python dependencies with pip-audit and the image scan instead.

## Alternatives considered

- **npm workspaces**: pnpm gives stricter isolation between members and explicit control over dependency build scripts.
- **Poetry** for Python: uv is a single tool for the interpreter, the lockfile, the virtual environment and script execution, and `--locked` is a one-flag guarantee in CI.
- **Turborepo or Nx**: a task orchestrator is superfluous for two apps and two packages; `pnpm -r` runs the per-member scripts.
- **Corepack** to provision pnpm: installing pnpm with npm behaves the same on developer machines, in CI, in the web image and in the EAS builds of milestone 8.
- **`.node-version`** instead of `.nvmrc`: `.nvmrc` is read by fnm and by `actions/setup-node` alike.
- **A system-wide Node** installed with winget: fnm pins the version per folder, so the other projects on the machine keep theirs.
- **Node 20**: end of life.
- **One React version for both apps**: Expo SDK 57 and react-router 8.4.0 cannot agree on one, and nothing shared depends on React.
