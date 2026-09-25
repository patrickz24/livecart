# ADR 0015: Quality tooling and supply chain: ruff, mypy, Vitest, Jest, lefthook, commitlint, pip-audit, Trivy, gitleaks, k6, SHA pinning

## Status

Accepted, 2026-09-25. Milestone 1 put the tooling in place; Jest (milestone 8) and k6 (milestone 9) are not in the repository yet.

## Context

A public portfolio repository is judged on the discipline of its tooling as much as on its features: consistent formatting, strict typing on both sides, security-relevant lint rules, a secret-free history, dependencies whose advisories are caught before merge, and a CI whose third-party actions cannot be swapped under it. The project is solo and developed on Windows, so every hook and check must run identically in Git Bash, PowerShell and on the Ubuntu runners.

## Decision

### Python: ruff and mypy

- ruff 0.16.9 lints and formats (`target-version = "py312"`, line length 100, LF line endings, formatted code in docstrings). Selected rule families: pycodestyle (`E`, `W`), pyflakes (`F`), isort (`I`), bugbear (`B`), pyupgrade (`UP`), bandit (`S`, including `S608` for SQL built from strings), flake8-async (`ASYNC`, blocking calls in coroutines), datetimez (`DTZ`, naive datetimes), simplify (`SIM`), comprehensions (`C4`), pytest-style (`PT`) and ruff's own rules (`RUF`). Tests may use `assert` and obviously fake credentials: `S101` and `S105` to `S107` are ignored under `tests/`.
- mypy 2.3.1 runs in `strict` mode with `warn_unreachable`, the `ignore-without-code` error code and the `pydantic.mypy` plugin (`init_forbid_extra`, `init_typed`, `warn_required_dynamic_aliases`). It checks the `src` trees, `services/api/scripts`, the root `conftest.py` and every `tests` folder, with the src roots on `mypy_path` and explicit package bases so that two members can hold a file with the same name.
- pytest 9 with its native TOML configuration, `--import-mode=importlib`, `strict`, warnings turned into errors and `asyncio_mode = "auto"`; an `integration` marker names the tests that need the compose PostgreSQL and Redis. pytest-cov 7.1.0 is installed and `[tool.coverage.run]` sets `branch = true`, but no command runs coverage yet: the plan's branch-coverage threshold (80 %) is an end criterion of milestone 2.

### TypeScript: ESLint, Prettier and Vitest

- ESLint 10.11.0 with a flat config (`eslint.config.js`): `@eslint/js` recommended for JavaScript with Node globals, and for TypeScript the `strictTypeChecked` and `stylisticTypeChecked` presets of typescript-eslint 8.70.1, type-aware through `projectService`, so each member's `tsconfig.json` drives the rules. Generated code, build output and the native mobile folders are ignored.
- Prettier 3.9.9 (`printWidth: 100`) formats JS, TS, JSON, Markdown, YAML, CSS and HTML; the lockfile and the generated files are excluded. `tsc` runs per member through `pnpm -r typecheck`.
- Vitest 5.0.1 tests `apps/web` in jsdom, configured in the same `vite.config.ts` as the build. The root scripts are tested with `node:test` (`pnpm run test:scripts`).
- Jest 29 with jest-expo arrives with the mobile app in milestone 8 (jest-expo 57 does not run on Jest 30); it is not in the repository yet.

### Git hooks: lefthook, commitlint and gitleaks

- lefthook 2.1.14 runs the hooks. Pre-commit: `ruff check --fix` and `ruff format` on the staged Python files, Prettier on the other staged files, each re-staging what it fixed, then `gitleaks git --pre-commit --staged --redact` on the final staged content. Commit-msg: commitlint 21.2.3 with `config-conventional` and a `scope-enum` limited to the monorepo members and cross-cutting areas; repository-wide changes use no scope.
- The hooks are installed by the root `prepare` script (`scripts/install-git-hooks.mjs`, run by `pnpm install`), which skips when `CI` is set or outside a git clone, and fails loudly when `lefthook install` fails. lefthook's own postinstall is denied in `allowBuilds`: pnpm does not re-run a dependency's build script once its store has cached the result, so a second clone on the same machine silently got no hook. pnpm 12 blocks every dependency build script unless it is reviewed in `allowBuilds`. The web image installs with `--ignore-scripts`.
- gitleaks is a native binary, installed on the developer machine and downloaded in CI from its GitHub release with its SHA-256 checked before use.

### CI: audits, scans and pinned actions

- The `security` job runs on every pull request, every push to `main` and weekly, so that advisories published since the last change still fail CI: gitleaks over the full history; a Trivy v0.74.0 filesystem scan (vulnerabilities, secrets, misconfigurations; fixable HIGH and CRITICAL fail) that covers `pnpm-lock.yaml` and the Dockerfiles; pip-audit 2.10.1 with `--strict --require-hashes` on a `uv export --locked --all-packages` of the Python lockfile; `pnpm audit --prod --audit-level=high`.
- The `docker` job builds `livecart-python:local` and `livecart-web:local`, runs the compose smoke test and scans both images with Trivy (fixable HIGH and CRITICAL fail). Images are never pushed.
- Trivy 0.74.0 cannot read the `uv.lock` of a virtual workspace root, so Python dependencies are covered by pip-audit and by the image scan, which reads the installed virtual environment.
- A fixable HIGH advisory in a transitive dependency is lifted with a pnpm `override` (`js-yaml` under the Hey API parser), not ignored.
- Every third-party GitHub Action is pinned by its full commit SHA with the release in a trailing comment (`actions/checkout`, `actions/setup-node`, `actions/cache`, `astral-sh/setup-uv`, `aquasecurity/trivy-action`), because a tag can be moved (GHSA-69fq-xp46-6x23, the trivy-action compromise). The tool versions of the workflow (`UV_VERSION`, `PNPM_VERSION`, `PIP_AUDIT_VERSION`, `GITLEAKS_VERSION`, `TRIVY_VERSION`) are exact. `pnpm run check:pins` enforces both rules, and that one action has one SHA across files. The workflow runs with `contents: read` and checks out without persisted credentials.

### Load test: k6

- k6 runs in Docker from the `grafana/k6` image, in a compose `loadtest` profile, in milestone 9; `make load-test` is a placeholder until then.

## Consequences

- `make lint` and `make test` run the checks of the `python` and `node` CI jobs with the same pinned versions; `uv run --locked` and `pnpm exec` never pick another tool. The OpenAPI drift check, the security scans and the docker job (image builds, compose smoke test, image scans) are CI-only; the drift check and the smoke test can be run by hand with `pnpm run check:openapi` and `pnpm run smoke:compose`.
- Commits are formatted and secret-scanned before they exist, so CI failures are about logic, not style. The hooks are never bypassed with `--no-verify`.
- Strict typing on both sides, tests included, costs some annotations up front; the pydantic plugin makes model constructors type-checked.
- A newly published advisory can turn `main` red on a Monday without any change in the repository. That is intended: the fix is an upgrade or an override, not an ignore list. Dependabot is planned but not configured yet.
- Upgrading an action is a deliberate commit that changes the SHA and its version comment together; the pins check refuses a tag or a bare SHA.
- gitleaks must be installed on the developer machine; the pre-commit hook says so when it is missing.
- The Trivy filesystem scan does not cover `uv.lock`; the gap is closed by pip-audit and the image scan, and documented in the workflow.

## Alternatives considered

- **Black, isort, flake8 and bandit** as separate tools: ruff replaces the four with one configuration and one binary.
- **The pre-commit framework with `repo: local` hooks**, which would run the same project commands (`uv run`, `pnpm exec`): lefthook was chosen for its native Windows binary, which comes with `pnpm install` as a dev dependency and installs the hooks from that same command, so the hooks need one `lefthook.yml` and no Python-side install step.
- **Jest for the web app**: Vitest reuses the Vite configuration; Jest stays for the mobile app, where jest-expo requires it.
- **ESLint 9**: end of life, and ESLint 10 only reads the flat config.
- **Tag-pinned actions** (`@v7`): a moved tag can run code the review never saw.
- **Trivy alone** for dependencies: it cannot parse a virtual uv workspace lockfile, so pip-audit is the source of truth for Python.
