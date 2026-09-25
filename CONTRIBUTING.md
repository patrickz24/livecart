# Contributing to LiveCart

## Prerequisites

| Tool           | Version | Why this version                                                     |
| -------------- | ------- | -------------------------------------------------------------------- |
| Node.js        | 24.21.0 | `.nvmrc`, managed per folder by fnm                                  |
| pnpm           | 12.6.0  | `packageManager` in `package.json`, installed with npm (no Corepack) |
| uv             | 0.12.19 | `[tool.uv] required-version` in `pyproject.toml`                     |
| CPython        | 3.12.14 | `.python-version`, installed by uv                                   |
| GNU Make       | 4.4.1   | the `Makefile` targets (Git Bash on Windows)                         |
| gitleaks       | 8.30.x  | called by the pre-commit hook                                        |
| Docker Desktop | recent  | WSL 2 backend, Linux containers                                      |

On Windows, in PowerShell:

```powershell
winget install -e --id Schniz.fnm
fnm env --use-on-cd --shell powershell | Out-String | Invoke-Expression   # add to $PROFILE to keep it
corepack disable                                                         # only if Corepack was enabled
fnm install 24.21.0; fnm default 24.21.0
npm install -g pnpm@12.6.0                                               # again after each Node change
winget install -e --id astral-sh.uv --version 0.12.19                    # or: uv self update 0.12.19
uv python install 3.12.14
winget install -e --id ezwinports.make
winget install -e --id Gitleaks.Gitleaks
```

In Git Bash, add `eval "$(fnm env --use-on-cd --shell bash)"` to `~/.bashrc`. On Linux or macOS, install fnm, uv, make and gitleaks with the system package manager, then run the same `fnm`, `npm` and `uv` commands. fnm's Node is only on the `PATH` of shells where `fnm env` runs, and `--use-on-cd` switches to the `.nvmrc` version when a shell enters the repository, so a system-wide Node used by other projects is left untouched.

### Node version seen by pnpm

Check once, from the repository root, that the programs pnpm starts get the same Node as your shell:

```bash
node -v             # v24.21.0
pnpm exec node -v   # must print v24.21.0 too
```

pnpm scripts, `pnpm exec`, `make` and the git hooks run the first `node` on the `PATH` of the processes they start. On Windows, fnm puts its Node first through a per-shell link (`fnm_multishells`); where a Windows program does not follow that link, it falls back to any other Node on the `PATH`, such as a system-wide Node 20. Vitest and `node --test` then fail with errors that do not mention the version, so `make lint` and `make test` start with `pnpm run check:node`, which fails with a clear message instead; `make` may even fail to start `pnpm` (`CreateProcess(NULL, pnpm ...) failed`), because `npm install -g` put pnpm in the same fnm folder. The fix is to put the fnm installation of Node 24.21.0 first on the `PATH`, after the `fnm env` line:

```bash
# Git Bash (~/.bashrc)
export PATH="$(cygpath "$FNM_DIR")/node-versions/v24.21.0/installation:$PATH"
```

```powershell
# PowerShell ($PROFILE)
$env:PATH = "$env:FNM_DIR\node-versions\v24.21.0\installation;$env:PATH"
```

Update the version in that line when `.nvmrc` changes.

### Version pins

`pnpm run check:pins` fails when a copy of these versions (Dockerfiles, CI, `package.json` engines) drifts from its source of truth. To upgrade a tool, change the source of truth and every copy the check lists, in one commit.

## First setup

From the repository root:

```bash
cp .env.example .env                        # PowerShell: Copy-Item .env.example .env
uv sync --locked                            # Python workspace in .venv
pnpm install                                # JS workspace; its prepare script installs the git hooks
docker compose up -d --wait postgres redis  # servers of the integration tests
```

`pnpm install` ends with `lefthook install`, run by the root `prepare` script (`scripts/install-git-hooks.mjs`), unless `CI` is set. `.git/hooks` then holds `pre-commit` and `commit-msg`; if it does not, run `pnpm exec lefthook install`.

Every command runs from the repository root: the Python processes read `.env` from the working directory.

## Make targets and their raw commands

`make` targets are thin wrappers, run from Git Bash. In PowerShell, run the raw commands one by one, in the same order (Windows PowerShell 5.1 has no `&&`; use one line per command, or `;`).

| Target           | What it does                                          | Raw commands                                                                                                                                                                                                                      |
| ---------------- | ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `make dev`       | builds and starts the whole stack in the foreground   | `docker compose up --build`                                                                                                                                                                                                       |
| `make lint`      | Node version, lint, format and type checks, pins      | `pnpm run check:node`<br>`uv run --locked ruff check .`<br>`uv run --locked ruff format --check .`<br>`uv run --locked mypy`<br>`pnpm run lint`<br>`pnpm exec prettier --check .`<br>`pnpm -r typecheck`<br>`pnpm run check:pins` |
| `make test`      | every test suite, on the compose PostgreSQL and Redis | `pnpm run check:node`<br>`docker compose up --detach --wait postgres redis`<br>`uv run --locked pytest`<br>`pnpm -r test`<br>`pnpm run test:scripts`                                                                              |
| `make seed`      | demo data (arrives in milestone 3)                    | none yet                                                                                                                                                                                                                          |
| `make load-test` | k6 comment spike (arrives in milestone 9)             | none yet                                                                                                                                                                                                                          |
| `make gen-api`   | regenerates `openapi.json` and the TypeScript client  | `pnpm run gen-api`                                                                                                                                                                                                                |
| `make reset`     | stops the stack and deletes its data volumes          | `docker compose down --volumes`                                                                                                                                                                                                   |

Other useful commands:

- Unit tests only, without Docker: `uv run --locked pytest -m "not integration"`, then `pnpm -r test`.
- Formatting fixes: `uv run --locked ruff format .` and `pnpm run format`.
- The compose smoke test that CI runs (starts the stack, checks every service and endpoint): `pnpm run smoke:compose`.
- The OpenAPI drift check of CI: `pnpm run check:openapi`. `packages/api-client/openapi.json` and `packages/api-client/src/generated/` are generated: never edit them by hand, run `make gen-api` and commit the result with the API change.
- The PostgreSQL roles and grants of `infra/postgres` are applied once, when the postgres volume is created. After a change to them, re-apply them to the existing volume with `docker compose exec postgres bash /docker-entrypoint-initdb.d/10-init-roles.sh` (idempotent; in Git Bash, prefix it with `MSYS_NO_PATHCONV=1`), or start over with `make reset`, which deletes the data.

## Ports

| Port    | Service  | Bound to         | Override                    |
| ------- | -------- | ---------------- | --------------------------- |
| `8080`  | web      | every interface  | not configurable            |
| `8000`  | api      | `127.0.0.1` only | not configurable            |
| `55432` | postgres | `127.0.0.1` only | `PG_HOST_PORT` in `.env`    |
| `56379` | redis    | `127.0.0.1` only | `REDIS_HOST_PORT` in `.env` |

PostgreSQL and Redis use non-standard host ports so that another project's servers can keep 5432 and 6379. If a port is still taken, set `PG_HOST_PORT` or `REDIS_HOST_PORT` in `.env`, change the same port in the host-run URLs of `.env` (`DATABASE_URL`, `MIGRATOR_URL`, `REDIS_URL` and the three `TEST_*` URLs), then run `docker compose up -d --wait postgres redis` again. Containers talk to each other on the compose network and are not affected.

Host-run URLs use `127.0.0.1` rather than `localhost`: on Windows, `localhost` resolves to `::1` first, and every connection would wait about 2 s before falling back to IPv4.

## Commit conventions

- [Conventional Commits](https://www.conventionalcommits.org/) in English, checked by commitlint: `type(scope): subject`, for example `feat(api): add health and readiness endpoints`. Types: `feat`, `fix`, `test`, `docs`, `refactor`, `perf`, `build`, `ci`, `chore`, `style`, `revert`. Scopes: `core`, `api`, `worker`, `simulator`, `web`, `mobile`, `api-client`, `design-tokens`, `infra`, `loadtest`, `deps`; no scope for repository-wide changes.
- Small, atomic commits: one logical step each, with its tests, and the tests pass at every commit.
- Git hooks (lefthook, installed by `pnpm install` or `pnpm exec lefthook install`): pre-commit fixes and re-stages Python files with ruff and the other files with Prettier, then scans the staged changes with gitleaks; commit-msg runs commitlint. Never bypass them with `--no-verify`: fix what they report.
- No secret in the repository: `.env` is ignored, and `.env.example` holds `change-me-*` placeholders only.
- Pull requests have an English description and are merged with "Rebase and merge", once CI is green.
