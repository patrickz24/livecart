# ADR 0015: Quality tooling and supply chain: ruff, mypy, Vitest, Jest, lefthook, commitlint, pip-audit, Trivy, gitleaks, k6, SHA pinning

## Status

Accepted, 2026-09-25, completed 2026-10-02. Milestone 1 put the tooling in place, milestone 8 added Jest and milestone 9 the k6 load test.

## Context

A public portfolio repository is judged on the discipline of its tooling as much as on its features: consistent formatting, strict typing on both sides, security-relevant lint rules, a secret-free history, dependencies whose advisories are caught before merge, and a CI whose third-party actions cannot be swapped under it. The project is solo and developed on Windows, so every hook and check must run identically in Git Bash, PowerShell and on the Ubuntu runners.

## Decision

### Python: ruff and mypy

- ruff 0.16.9 lints and formats (`target-version = "py312"`, line length 100, LF line endings, formatted code in docstrings). Selected rule families: pycodestyle (`E`, `W`), pyflakes (`F`), isort (`I`), bugbear (`B`), pyupgrade (`UP`), bandit (`S`, including `S608` for SQL built from strings), flake8-async (`ASYNC`, blocking calls in coroutines), datetimez (`DTZ`, naive datetimes), simplify (`SIM`), comprehensions (`C4`), pytest-style (`PT`) and ruff's own rules (`RUF`). Tests may use `assert` and obviously fake credentials: `S101` and `S105` to `S107` are ignored under `tests/`.
- mypy 2.3.1 runs in `strict` mode with `warn_unreachable`, the `ignore-without-code` error code and the `pydantic.mypy` plugin (`init_forbid_extra`, `init_typed`, `warn_required_dynamic_aliases`). It checks the `src` trees, `services/api/scripts`, the root `conftest.py` and every `tests` folder, with the src roots on `mypy_path` and explicit package bases so that two members can hold a file with the same name.
- pytest 9 with its native TOML configuration, `--import-mode=importlib`, `strict`, warnings turned into errors and `asyncio_mode = "auto"`; an `integration` marker names the tests that need the compose PostgreSQL and Redis. pytest-cov 7.1.0 gates coverage since milestone 2: `[tool.coverage]` measures the branch coverage of `livecart_core.domain` and `livecart_core.application`, every module of both, and `make test` and the python job of CI run `pytest --cov`, which fails below 80 %. The adapters and the entry points are proven by their integration tests instead.

### TypeScript: ESLint, Prettier and Vitest

- ESLint 10.11.0 with a flat config (`eslint.config.js`): `@eslint/js` recommended for JavaScript with Node globals, and for TypeScript the `strictTypeChecked` and `stylisticTypeChecked` presets of typescript-eslint 8.70.1, type-aware through `projectService`, so each member's `tsconfig.json` drives the rules. Generated code, build output and the native mobile folders are ignored.
- Prettier 3.9.9 (`printWidth: 100`) formats JS, TS, JSON, Markdown, YAML, CSS and HTML; the lockfile and the generated files are excluded. `tsc` runs per member through `pnpm -r typecheck`.
- Vitest 5.0.1 tests `apps/web` in jsdom, configured in the same `vite.config.ts` as the build. The root scripts are tested with `node:test` (`pnpm run test:scripts`).
- Jest 29.7.0 with jest-expo 57 tests `apps/mobile` (jest-expo 57 does not run on Jest 30), with React Native Testing Library 14 and expo-router's testing library, which renders the real routes. CI's `mobile` job adds expo-doctor 1.20.4, a devDependency of the app, and an `expo export` of each brand (ADR 0013). expo-doctor checks the dependencies against the versions of Expo SDK 57, which it asks of Expo's API, so a patch Expo publishes fails it until `npx expo install --fix`. The weekly scheduled run skips that one check and runs the others (`EXPO_DOCTOR_SKIP_DEPENDENCY_VERSION_CHECK` is `true` on the schedule only): that run is there for new advisories, and a patch Expo published since the last change would turn `main` red with no change in the repository. A push or a pull request still runs it.

### Git hooks: lefthook, commitlint and gitleaks

- lefthook 2.1.14 runs the hooks. Pre-commit: `ruff check --fix` and `ruff format` on the staged Python files, Prettier on the other staged files, each re-staging what it fixed, then `gitleaks git --pre-commit --staged --redact` on the final staged content. Commit-msg: commitlint 21.2.3 with `config-conventional` and a `scope-enum` limited to the monorepo members and cross-cutting areas; repository-wide changes use no scope.
- The hooks are installed by the root `prepare` script (`scripts/install-git-hooks.mjs`, run by `pnpm install`), which skips when `CI` is set or outside a git clone, and fails loudly when `lefthook install` fails. lefthook's own postinstall is denied in `allowBuilds`: pnpm does not re-run a dependency's build script once its store has cached the result, so a second clone on the same machine silently got no hook. pnpm 12 blocks every dependency build script unless it is reviewed in `allowBuilds`. The web image installs with `--ignore-scripts`.
- gitleaks is a native binary, installed on the developer machine and downloaded in CI from its GitHub release with its SHA-256 checked before use.

### CI: audits, scans and pinned actions

- The `security` job runs on every pull request, every push to `main` and weekly, so that advisories published since the last change still fail CI: gitleaks over the full history; a Trivy v0.74.0 filesystem scan (vulnerabilities, secrets, misconfigurations; fixable HIGH and CRITICAL fail) that covers `pnpm-lock.yaml` and the Dockerfiles; pip-audit 2.10.1 with `--strict --require-hashes` on a `uv export --locked --all-packages` of the Python lockfile; `pnpm audit --prod --audit-level=high`.
- The `docker` job builds `livecart-python:local` and `livecart-web:local`, runs the compose smoke test and scans both images with Trivy (fixable HIGH and CRITICAL fail). Images are never pushed.
- Trivy 0.74.0 cannot read the `uv.lock` of a virtual workspace root, so Python dependencies are covered by pip-audit and by the image scan, which reads the installed virtual environment.
- A fixable HIGH advisory in a transitive dependency is lifted with a pnpm `override` (`js-yaml` under the Hey API parser), not ignored. One without any fixed release, in a dependency that never reaches what the project ships, is ignored by its GHSA id in `auditConfig.ignoreGhsas` of `pnpm-workspace.yaml`, with the reason beside it: node-forge under Expo's CLI, which signs iOS builds and expo-updates manifests on the developer's machine. Trivy skips it as unfixed. pnpm applies the entry to the whole workspace, not to Expo's CLI alone, so it is revisited at each Expo upgrade (SECURITY.md, known limits).
- Every third-party GitHub Action is pinned by its full commit SHA with the release in a trailing comment (`actions/checkout`, `actions/setup-node`, `actions/cache`, `astral-sh/setup-uv`, `aquasecurity/trivy-action`), because a tag can be moved (GHSA-69fq-xp46-6x23, the trivy-action compromise). The tool versions of the workflow (`UV_VERSION`, `PNPM_VERSION`, `PIP_AUDIT_VERSION`, `GITLEAKS_VERSION`, `TRIVY_VERSION`) are exact. `pnpm run check:pins` enforces both rules, and that one action has one SHA across files. The workflow runs with `contents: read` and checks out without persisted credentials.

### Load test: k6

- k6 2.3.0 runs in Docker from the `grafana/k6:2.3.0` image, in the compose profile `loadtest`: nothing to install on the host, and the generator reaches the API and the worker's metrics on the compose network. `check:pins` checks its tag like every compose image. `K6_AUTO_EXTENSION_RESOLUTION=false` keeps k6 from downloading a custom build of itself when a script imports an extension, and the scenario needs none; `K6_NO_USAGE_REPORT` turns off its usage report.
- The scenario, `loadtest/comment-spike.ts`, is TypeScript that k6 runs natively, without a bundler. `loadtest/types/k6.d.ts` types the k6 APIs it calls, so that ESLint and `tsc -p loadtest` (in `make lint` and CI) check it like the rest of the code; a dozen signatures do not justify an `@types/k6` dependency whose versions follow k6's own.
- An open model, `ramping-arrival-rate`: calls arrive on schedule whatever the latency, as a live audience does. The default burst is 1,000 webhook calls of one signed comment each, from a buyer of its own, on Z99 of the seeded `loadtest` session (200 in stock), peaking at 80 calls a second, below the comment webhook's 6,000 a minute (ADR 0011), so that the run loads the queue and the workers, not the rate limiter. One call in ten replays the comment of five calls before it, which the inbox must count once.
- `make load-test` runs `seed --reset-loadtest` first: the scenario sends the same comment ids at every run, which the inbox would take for duplicates, and its setup refuses a session that was not reset.
- Correctness alone decides pass or fail. `teardown()` waits until the worker's `livecart_queue_backlog` reads 0 on a sample taken after the burst, then reads the session's stock and orders in one consistent read, the snapshot that opens its event stream: no other route lists a session's orders. The `no_oversell` check fails the run unless the orders hold at most the stock, exactly the units the counters say are reserved and sold. 429 (abuse) and 503 (backpressure) are counted and thresholded apart; there is no latency threshold.
- `handleSummary` writes one JSON summary per run to `loadtest/results/`. The summaries are versioned and marked `linguist-generated`, as the only source of the measured figures; the raw outputs (`loadtest/results/raw/`) stay ignored.

## Consequences

- `make lint` and `make test` run the checks of the `python` and `node` CI jobs with the same pinned versions; `uv run --locked` and `pnpm exec` never pick another tool. The OpenAPI drift check, the security scans and the docker job (image builds, compose smoke test, image scans) are CI-only; the drift check and the smoke test can be run by hand with `pnpm run check:openapi` and `pnpm run smoke:compose`.
- Commits are formatted and secret-scanned before they exist, so CI failures are about logic, not style. The hooks are never bypassed with `--no-verify`.
- Strict typing on both sides, tests included, costs some annotations up front; the pydantic plugin makes model constructors type-checked.
- A newly published advisory can turn `main` red on a Monday without any change in the repository. That is intended: the fix is an upgrade or an override, and an ignore only for an advisory without any fixed release. A new patch of Expo does not, since the weekly run skips expo-doctor's version check; the next push meets it. Dependabot is planned but not configured yet.
- Upgrading an action is a deliberate commit that changes the SHA and its version comment together; the pins check refuses a tag or a bare SHA.
- gitleaks must be installed on the developer machine; the pre-commit hook says so when it is missing.
- The Trivy filesystem scan does not cover `uv.lock`; the gap is closed by pip-audit and the image scan, and documented in the workflow.
- The load test runs on the developer machine only, never in CI, and its generator shares that machine with the stack: its figures describe one laptop, not a deployment.
- The snapshot read relies on k6 keeping the bytes that arrived before a request's timeout, since k6 has no streaming client; a k6 that kept none would fail `no_oversell`, never pass it. The drain wait assumes the worker's reclaim interval (30 s), which samples the backlog.

## Alternatives considered

- **Black, isort, flake8 and bandit** as separate tools: ruff replaces the four with one configuration and one binary.
- **The pre-commit framework with `repo: local` hooks**, which would run the same project commands (`uv run`, `pnpm exec`): lefthook was chosen for its native Windows binary, which comes with `pnpm install` as a dev dependency and installs the hooks from that same command, so the hooks need one `lefthook.yml` and no Python-side install step.
- **Jest for the web app**: Vitest reuses the Vite configuration; Jest stays for the mobile app, where jest-expo requires it.
- **ESLint 9**: end of life, and ESLint 10 only reads the flat config.
- **Tag-pinned actions** (`@v7`): a moved tag can run code the review never saw.
- **Trivy alone** for dependencies: it cannot parse a virtual uv workspace lockfile, so pip-audit is the source of truth for Python.
- **A closed model** (a fixed number of VUs looping): its arrivals slow down whenever the API does, which hides the queueing a comment spike is about.
- **k6 extensions** (xk6-sql to count the orders in PostgreSQL, xk6-sse to read the stream): a custom k6 build to pin and scan, for two reads the API already serves.
