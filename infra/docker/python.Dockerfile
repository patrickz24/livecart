# syntax=docker/dockerfile:1
#
# One image for every Python process of LiveCart: the API (default command),
# the worker, the comment simulator and the one-shot migrate and seed jobs.
# Building a single artifact means the migrations always come from the same
# code as the processes that use the schema, and the simulator from the same
# signing code as the API that checks it. compose.yaml chooses each service's
# command and healthcheck.
#
# Build from the repository root:
#   docker build -f infra/docker/python.Dockerfile -t livecart-python:local .
#
# Base image and uv versions must match .python-version and the uv
# required-version of pyproject.toml (pnpm run check:pins).

# --- Build stage: resolve the locked dependencies into /app/.venv ------------
FROM python:3.12.14-slim-trixie AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /bin/uv

# Compile bytecode at build time: the runtime root filesystem is read-only, so
# Python could not write __pycache__ at import time. Copy files out of the cache
# mount instead of hard-linking them, use the image's interpreter only (never a
# download) and leave the dev dependency group out.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_DEV=1

WORKDIR /app

# Third-party dependencies first, in their own layer: it is rebuilt only when
# the lockfile or a pyproject.toml changes. --locked needs every workspace
# member's pyproject.toml to prove that uv.lock is up to date.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=.python-version,target=.python-version \
    --mount=type=bind,source=packages/livecart-core/pyproject.toml,target=packages/livecart-core/pyproject.toml \
    --mount=type=bind,source=services/api/pyproject.toml,target=services/api/pyproject.toml \
    --mount=type=bind,source=services/worker/pyproject.toml,target=services/worker/pyproject.toml \
    --mount=type=bind,source=services/simulator/pyproject.toml,target=services/simulator/pyproject.toml \
    uv sync --locked --all-packages --no-install-workspace --no-editable

# Then the workspace members, installed as regular (non-editable) packages, so
# the runtime stage needs the virtual environment only, not the sources.
COPY pyproject.toml uv.lock .python-version ./
COPY packages/livecart-core packages/livecart-core
COPY services/api services/api
COPY services/worker services/worker
COPY services/simulator services/simulator
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --all-packages --no-editable

# --- Runtime stage: the virtual environment and an unprivileged user ---------
FROM python:3.12.14-slim-trixie AS runtime

# Fixed ids, so that volumes and orchestrator security contexts can refer to
# them. No home directory and no login shell.
RUN groupadd --system --gid 10001 livecart \
    && useradd --system --uid 10001 --gid livecart --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin livecart

# Owned by root and read-only for the livecart user: a compromised process
# cannot rewrite its own code.
COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
USER livecart:livecart

EXPOSE 8000

# Liveness of the default command (the API). compose.yaml replaces it for the
# worker (heartbeat file) and disables it for the migrate job. The image ships
# neither curl nor wget: livecart-healthcheck uses the standard library only.
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=3 \
    CMD ["livecart-healthcheck", "http", "http://127.0.0.1:8000/health"]

# One uvicorn process per container; scale with replicas. Without
# --forwarded-allow-ips, uvicorn trusts proxy headers from 127.0.0.1 only;
# compose.yaml adds the nginx address.
CMD ["uvicorn", "livecart_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
