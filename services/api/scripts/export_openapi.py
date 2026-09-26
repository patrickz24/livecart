"""Write the API's OpenAPI schema to packages/api-client/openapi.json.

The TypeScript client is generated from that file (``pnpm run gen-api``), and CI
fails when the committed copy differs from what the code produces
(``pnpm run check:openapi``). No server starts and nothing connects: the app is
built around an export-only ``Settings`` instance and the real resources, whose
engine and Redis client only connect on first use, which never comes. The
output is deterministic (sorted keys, two-space indent, LF line endings,
trailing newline), so the same code always gives the same bytes.

Usage, from the repository root::

    uv run python services/api/scripts/export_openapi.py [--output PATH]
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import SecretStr

from livecart_api.app import create_app
from livecart_api.resources import build_resources
from livecart_core.settings import AppEnv, LogLevel, Settings

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = REPO_ROOT / "packages" / "api-client" / "openapi.json"

# RFC 2606 reserves .invalid: these URLs can never resolve, let alone connect.
EXPORT_HOST = "openapi-export.invalid"


def export_settings() -> Settings:
    """Settings that pass every startup validator but are useless at runtime.

    They go through the validators instead of around them (no ``model_construct``),
    in production mode so the export describes the production surface. Every
    field that could vary is explicit, and ``.env`` is not read, so neither the
    environment nor a local file can change the exported schema.
    """
    return Settings(
        app_env=AppEnv.PRODUCTION,
        log_level=LogLevel.WARNING,
        database_url=SecretStr(f"postgresql+asyncpg://{EXPORT_HOST}/openapi_export"),
        redis_url=SecretStr(f"redis://{EXPORT_HOST}/0"),
        # Keys that sign nothing: the export never serves a request.
        jwt_secret=SecretStr("openapi-export-jwt-" * 2),
        order_token_key=SecretStr("openapi-export-order-token-" * 2),
        _env_file=None,
    )


async def _export_schema() -> dict[str, Any]:
    settings = export_settings()
    resources = build_resources(settings)
    try:
        app: FastAPI = create_app(settings, resources)
        return app.openapi()
    finally:
        await resources.aclose()


def export_schema() -> dict[str, Any]:
    """The schema of the real app factory and composition, which serves no request."""
    return asyncio.run(_export_schema())


def render_schema(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export the LiveCart OpenAPI schema.")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="destination file (default: packages/api-client/openapi.json)",
    )
    args = parser.parse_args(argv)
    output: Path = args.output

    output.write_text(render_schema(export_schema()), encoding="utf-8", newline="\n")
    sys.stderr.write(f"OpenAPI schema written to {output}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
