"""Write the API's OpenAPI schema to packages/api-client/openapi.json.

The TypeScript client is generated from that file (``pnpm run gen-api``), and CI
fails when the committed copy differs from what the code produces
(``pnpm run check:openapi``). No server starts and nothing connects: the app is
built around an export-only ``Settings`` instance and resources that probe
nothing. The output is deterministic (sorted keys, two-space indent, LF line
endings, trailing newline), so the same code always gives the same bytes.

Usage, from the repository root::

    uv run python services/api/scripts/export_openapi.py [--output PATH]
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import SecretStr

from livecart_api.app import create_app
from livecart_api.resources import Resources
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
        _env_file=None,
    )


async def _nothing_to_close() -> None:
    return None


def build_export_app() -> FastAPI:
    """The real app factory, with no dependency to probe or close."""
    return create_app(export_settings(), Resources(readiness_checks=(), aclose=_nothing_to_close))


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

    output.write_text(render_schema(build_export_app().openapi()), encoding="utf-8", newline="\n")
    sys.stderr.write(f"OpenAPI schema written to {output}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
