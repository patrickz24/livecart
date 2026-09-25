# syntax=docker/dockerfile:1
#
# Seller dashboard: Vite builds the static app, then an unprivileged nginx
# serves it and forwards the API paths to the api service (same origin, no
# CORS). The nginx configuration is static: infra/nginx/default.conf.
#
# Build from the repository root:
#   docker build -f infra/docker/web.Dockerfile -t livecart-web:local .
#
# Node and pnpm versions must match .nvmrc and packageManager
# (pnpm run check:pins).

# --- Build stage --------------------------------------------------------------
FROM node:24.21.0-alpine AS build

# pnpm from npm, as on developer machines and in CI (no Corepack).
RUN npm install -g pnpm@12.6.0

# CI mode: frozen lockfile, no prompts, and lefthook's postinstall does nothing.
ENV CI=true

WORKDIR /repo

# Manifests first: the dependency layer is rebuilt only when one changes. Only
# the web app and the workspace packages it depends on are installed.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/
COPY packages/api-client/package.json packages/api-client/
RUN --mount=type=cache,target=/pnpm/store \
    pnpm install --frozen-lockfile --store-dir /pnpm/store --filter "@livecart/web..."

COPY tsconfig.base.json ./
COPY packages/api-client packages/api-client
COPY apps/web apps/web
RUN pnpm --filter @livecart/web run build

# --- Runtime stage --------------------------------------------------------------
FROM nginxinc/nginx-unprivileged:1.30.5-alpine AS runtime

# Owned by root and read-only for nginx.
COPY infra/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=build /repo/apps/web/dist /usr/share/nginx/html

# The base image already runs as nginx (uid 101); stated here so that the
# Dockerfile alone shows the container never runs as root.
USER 101

EXPOSE 8080

# /healthz is answered by nginx itself (busybox wget; no curl in the image).
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/healthz"]
