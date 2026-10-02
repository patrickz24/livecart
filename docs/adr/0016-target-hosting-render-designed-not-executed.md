# ADR 0016: Target hosting: Render, designed, not executed

## Status

Accepted, 2026-10-02, as a design: decision 6 of the project plan (25 September 2026) keeps the deployment "designed, not executed", without `render.yaml`, deploy workflow or image push. [docs/DEPLOYMENT.md](../DEPLOYMENT.md) holds the design; executing it would add those three, and complete this record with what the platform turned out to be.

## Context

The requirements ask for a deployment guide on a container host of LiveCart's choice, with migrations, environment variables, rollback and backups. The stack is two images (`infra/docker/python.Dockerfile` for the API, the worker and `livecart-migrate`; `infra/docker/web.Dockerfile` for nginx and the dashboard), PostgreSQL 16 and a Redis-compatible store that must refuse writes rather than evict a queued comment (ADR 0004). One person builds and would run it, so every hour spent on servers is an hour not spent on the product. Two facts of the code bound what any host can do today: the worker refuses `APP_ENV=production` until a notifier delivers on the platforms (ADR 0007), and the payments run in Stripe's test mode only (ADR 0008).

## Decision

- **Render**, region Frankfurt: `livecart-web` as the public web service (nginx, health check `/healthz`), `livecart-api` as a private service (health check `/health`, two instances or more), `livecart-worker` as a background worker, a managed PostgreSQL 16 and a Key Value instance with `maxmemoryPolicy: noeviction`, all on the platform's private network, with no external connection to the data stores. The images are the compose ones, pulled from GHCR by their `sha-<7>` tag.
- **Migrations** run as the pre-deploy command of `livecart-api`, `livecart-migrate upgrade head`, as `livecart_migrator`: a failed migration fails the deploy and leaves the previous release serving, and only one service ever migrates. Migrations keep the previous release working against the new schema, since the worker deploys on its own.
- **Rollback** moves both images back to the previous tag together. Across a migration, the release's own image downgrades first: the previous image cannot locate the newer revision, and its pre-deploy command would fail.
- **Backups** are the platform's point-in-time recovery of PostgreSQL, plus a `pg_dump` kept outside the platform before each release with a migration. Key Value is not backed up; what its loss costs is listed in DEPLOYMENT.md.
- **One more trusted hop**: nginx trusts the platform proxy's range for `X-Forwarded-For` (`set_real_ip_from`, `real_ip_recursive on`) and keeps overwriting the header; uvicorn trusts the private network's range (`FORWARDED_ALLOW_IPS`), since nginx's addresses there are not fixed, and never `*`. Render gets a second static nginx file rather than a template, so that the reviewed file stays the file that runs (ADR 0011).
- **`/health`, not `/ready`**, as the API's health check: a Key Value outage must not take out of rotation the instances that still serve the PostgreSQL reads, while the routes that need Key Value already answer for themselves (logins 503, the other limits open, webhooks 503). `/ready` stays the readiness of compose and of the CI smoke test.

## Consequences

- The design cannot be executed as is: the worker refuses production, and no image is pushed. DEPLOYMENT.md says so first, and lists the platform facts it assumes (the proxy and private network ranges, the resolver, health checks on private services, the pre-deploy command's environment, the proxy's idle timeout, the database user's `CREATEROLE`, the Key Value plan's memory and persistence, the recovery window) to confirm before anything runs.
- The pre-deploy command shares the API's environment, so `MIGRATOR_URL` sits there, where compose gives it to the `migrate` container alone. The API never reads it; a compromised API process could. A one-off job run by hand before each deploy would keep the separation, at the cost of the automatic ordering.
- The managed database gives no superuser: the roles of `infra/postgres/roles.sql` and `grants.sql` are created once by hand, as the database's owner, before the first migration, which revokes `livecart_app`'s rights on `alembic_version` and needs the role to exist.
- Render's connection string starts with `postgresql://`, which `Settings` refuses: `DATABASE_URL` and `MIGRATOR_URL` are typed in with `postgresql+asyncpg://`, as secrets, rather than wired from the database.
- `REDIS_MAXMEMORY_BYTES` must be set to the Key Value plan's memory by hand: the startup check that a full queue fits in half of it only knows the setting.
- The image's `HEALTHCHECK` is not assumed to run on Render: a worker that hangs without exiting shows in its logs and its backlog, and is not restarted on its own.
- The images stay platform-neutral; the platform is one YAML file and one nginx file, which a move to another host replaces.

## Alternatives considered

- **A VPS with `docker compose`**: the cheapest, and the compose file already runs the whole stack, but backups, TLS, OS updates, restarts and recovery become the solo maintainer's job, and the database is one disk.
- **Fly.io**: Docker images, a private network and European regions as well; its Redis-compatible store is run by a partner, so the queue's guarantee would rest on a second vendor where Render's Blueprint declares all five services, the `noeviction` policy included.
- **AWS ECS on Fargate, RDS and ElastiCache**: the most control and the most standard answer at scale, at the price of a VPC, IAM, load balancers and infrastructure as code that a demo run by one person does not need.
- **Kubernetes (managed)**: horizontal scaling the stack does not need yet, and a cluster to keep up to date.
