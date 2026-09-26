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

When `.env.example` gains a variable without a default (`JWT_SECRET`, `ORDER_TOKEN_KEY` and `SEED_DEMO_PASSWORD` in milestone 3), an older `.env` lacks it and `docker compose` refuses to start, naming it: copy its lines from `.env.example` into `.env`.

## Make targets and their raw commands

`make` targets are thin wrappers, run from Git Bash. In PowerShell, run the raw commands one by one, in the same order (Windows PowerShell 5.1 has no `&&`; use one line per command, or `;`).

| Target           | What it does                                                                 | Raw commands                                                                                                                                                                                                                      |
| ---------------- | ---------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `make dev`       | builds and starts the whole stack in the foreground                          | `docker compose up --build`                                                                                                                                                                                                       |
| `make lint`      | Node version, lint, format and type checks, pins                             | `pnpm run check:node`<br>`uv run --locked ruff check .`<br>`uv run --locked ruff format --check .`<br>`uv run --locked mypy`<br>`pnpm run lint`<br>`pnpm exec prettier --check .`<br>`pnpm -r typecheck`<br>`pnpm run check:pins` |
| `make test`      | every test suite, on the compose PostgreSQL and Redis, and the coverage gate | `pnpm run check:node`<br>`docker compose up --detach --wait postgres redis`<br>`uv run --locked pytest --cov`<br>`pnpm -r test`<br>`pnpm run test:scripts`                                                                        |
| `make seed`      | loads the demo data again, creating only what is missing                     | `docker compose run --rm seed`                                                                                                                                                                                                    |
| `make load-test` | k6 comment spike (arrives in milestone 9)                                    | none yet                                                                                                                                                                                                                          |
| `make gen-api`   | regenerates `openapi.json` and the TypeScript client                         | `pnpm run gen-api`                                                                                                                                                                                                                |
| `make reset`     | stops the stack and deletes its data volumes                                 | `docker compose down --volumes`                                                                                                                                                                                                   |

Other useful commands:

- Unit tests only, without Docker: `uv run --locked pytest -m "not integration"`, then `pnpm -r test`.
- Coverage gate: `--cov` measures the branch coverage of `livecart_core.domain` and `livecart_core.application`, every module of both (`[tool.coverage]` in `pyproject.toml`), and fails below 80 %. The unit tests reach it on their own, without Docker: `uv run --locked pytest -m "not integration" --cov`. A subset of the tests covers a subset of the code and fails the gate; and since `--cov` takes an optional value, put it after any test path.
- Formatting fixes: `uv run --locked ruff format .` and `pnpm run format`.
- The compose smoke test that CI runs (starts the stack with `docker compose up --wait`, which must succeed on its own, then checks every service and endpoint): `pnpm run smoke:compose`. It checks the compose project the environment names and asks `docker compose port` where the API and nginx are published, so a throwaway second stack (`COMPOSE_PROJECT_NAME` set, other `PG_HOST_PORT`, `REDIS_HOST_PORT`, `COMPOSE_SUBNET`, `COMPOSE_IP_RANGE` and `NGINX_IP` in its `.env`, and a `compose.override.yaml` that republishes 8000 and 8080 with `ports: !override`) is checked the same way; remove it afterwards with `docker compose -p <name> down --volumes`.
- The OpenAPI drift check of CI: `pnpm run check:openapi`. `packages/api-client/openapi.json` and `packages/api-client/src/generated/` are generated: never edit them by hand, run `make gen-api` and commit the result with the API change.
- The PostgreSQL roles and grants of `infra/postgres` are applied once, when the postgres volume is created. After a change to them, re-apply them to the existing volume with `docker compose exec postgres bash /docker-entrypoint-initdb.d/10-init-roles.sh` (idempotent; in Git Bash, prefix it with `MSYS_NO_PATHCONV=1`), or start over with `make reset`, which deletes the data.

## Demo data

`docker compose up` runs the one-shot `seed` service after `migrate`, and the worker starts once it has completed (the API does not wait for it): `livecart-seed` creates, as `livecart_migrator`, only what is missing of the demo data, so running it again changes nothing (`make seed`):

- the seller `demo-seller` (`demo-seller@livecart.test`), with two live sessions on the simulator: `friday-drop`, whose seven products are the catalog the parser corpus is written against (A12 in S, M and L, B07, C3 in four colours, D4 in four sizes, E15, Z99) and F8; and `loadtest`, Z99 alone with 200 in stock;
- the seller `sample-shop` (`sample-shop@livecart.test`), without a session, for the second brand of the buyer app.

Both sign in with `SEED_DEMO_PASSWORD` from `.env`, set when the seed first creates them: after changing it, `make reset` starts over, as it does for a demo session a seller has ended, which the seed reports and leaves ended. `docker compose run --rm seed --reset-loadtest` also deletes the comments, intents and orders of the `loadtest` session and puts its stock back to 200, before a load test. From the host, against the database of `.env`: `uv run --locked livecart-seed`.

## Comment simulator

`docker compose up` also starts `simulator`, a live audience on the seeded `friday-drop` session: `livecart-simulate` posts signed comments to the comment webhook as a Poisson process, one every 5 s on average (`SIMULATOR_RATE=0.2`), from twelve fake buyers. About three comments in four are purchases in French or English ("je prends A12 M", "SOLD C3 rouge x2", "B07 🛒"), which the parser reads as exactly the product, variant and quantity meant; the others are noise ("prix ?") and ambiguous comments ("je prends C3", "je prends la robe fleurie"), which the parser leaves to the LLM fallback, and which are rejected as `ambiguous_unresolved` without it. The stock of the demo session runs out within minutes, and comes back as the unpaid reservations expire (`RESERVATION_TTL_SECONDS`).

```bash
docker compose stop simulator                       # pause the audience; `docker compose start simulator` resumes it
docker compose run --rm simulator --text            # the buyers' messages: reference, token and link of each order (Ctrl+C stops)
docker compose run --rm simulator --text --no-follow
uv run --locked livecart-simulate --rate 2 --count 50 --exclude-codes B07,E15   # from the host, against 127.0.0.1:8000
```

`--exclude-codes` keeps products out of the comments, to buy them by hand meanwhile; `--seed` repeats a run; `--help` lists the rest, each option with the `SIMULATOR_*` variable it overrides (`.env.example`). From the host, the simulator reads `COMMENT_WEBHOOK_SECRET_SIMULATOR` and, for `--text`, `REDIS_URL` from `.env`. It refuses to run with `APP_ENV=production`, where it would open fake orders.

## Metrics

The API and the worker each serve their Prometheus metrics on port 9100 of their container (`METRICS_PORT`), for a scraper on the compose network: the port is never published on the host and nginx never routes it. To read them by hand:

```bash
docker compose exec worker python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9100/metrics').read().decode())"
```

The API counts the comments it queued (`livecart_comments_received_total`), the backlog it last saw and the HTTP latency by route (`http_request_duration_seconds`); the worker, the intents by source, the orders, the rejections by reason, the queue wait and the processing time of each comment, the reclaims, the dead letters and the expired reservations.

## Seller accounts

There is no public sign-up: a seller account is created with `livecart-create-seller`, installed with the API. It checks the fields (an ASCII e-mail, unique whatever its case; a slug of lower-case words joined by hyphens; a display name), hashes the password with argon2id and connects as `livecart_app` through `DATABASE_URL`. From the repository root, against the database of `.env`:

```bash
uv run --locked livecart-create-seller --email seller@example.test --slug my-shop --display-name "My shop"
```

The command asks for the password twice, without echo: 12 to 128 characters, of any kind. It never takes the password as an argument, which would stay in the shell history and show in the process list. For a script, set `LIVECART_SELLER_PASSWORD` in the environment instead; it is read from the environment only, never from `.env`. In the compose stack, the API image ships the command, and `docker compose run` gives it a terminal to ask on; `-e LIVECART_SELLER_PASSWORD`, without a value, passes the variable of your shell instead:

```bash
docker compose run --rm api livecart-create-seller --email seller@example.test --slug my-shop --display-name "My shop"
```

It prints the new seller's id and slug. It exits with 1, and a message on standard error, when a field is refused, when the e-mail or the slug is taken, or when the database cannot be reached; with 2 on a usage error.

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
