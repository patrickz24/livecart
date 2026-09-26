"""The import rule of the inner layers, and the ban on hand-written SQL, checked with ``ast``.

``livecart_core.domain`` may import the standard library and itself only;
``livecart_core.application`` may add pydantic. Reading the source rather
than importing it catches an import inside a function or under
``TYPE_CHECKING`` too, and does not depend on what happens to be installed.
No runtime module of the core package or of the entry points (the API and
the worker, where the routes and the loops live) may use SQLAlchemy's raw
SQL text either (ruff's S608 catches SQL built from strings; this catches
``text()``).
"""

import ast
import inspect
import sys
from pathlib import Path
from types import ModuleType

import pytest

import livecart_api
import livecart_core
import livecart_core.application
import livecart_core.domain
import livecart_worker

DOMAIN_ALLOWED = frozenset({"livecart_core.domain"})
APPLICATION_ALLOWED = frozenset({"livecart_core.domain", "livecart_core.application", "pydantic"})


def _modules(package: ModuleType) -> list[tuple[str, Path, str]]:
    """(module name, file, owning package) of every module under ``package``."""
    root = Path(inspect.getfile(package)).parent
    found = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        owner = ".".join((package.__name__, *relative.parent.parts))
        module = owner if relative.stem == "__init__" else f"{owner}.{relative.stem}"
        found.append((module, path, owner))
    return found


def _resolve_relative(package: str, level: int, module: str | None) -> str:
    """``from ..x import y`` inside ``package`` names the module ``x`` of its parent."""
    base = package.rsplit(".", level - 1)[0] if level > 1 else package
    return f"{base}.{module}" if module else base


def _imported_names(source: str, *, package: str) -> list[str]:
    """Every module name the source imports, anywhere in the file, made absolute."""
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names.append(_resolve_relative(package, node.level, node.module))
            elif node.module:
                names.append(node.module)
    return names


def _is_allowed(name: str, allowed: frozenset[str]) -> bool:
    if name.partition(".")[0] in sys.stdlib_module_names:
        return True
    # A prefix match on whole dotted names: "pydantic" allows "pydantic.fields",
    # never "pydantic_settings".
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in allowed)


def _violations(source: str, *, package: str, allowed: frozenset[str]) -> list[str]:
    return [
        name for name in _imported_names(source, package=package) if not _is_allowed(name, allowed)
    ]


def _cases(package: ModuleType, allowed: frozenset[str]) -> list[object]:
    return [
        pytest.param(path, owner, allowed, id=module) for module, path, owner in _modules(package)
    ]


@pytest.mark.parametrize(
    ("path", "package", "allowed"),
    [
        *_cases(livecart_core.domain, DOMAIN_ALLOWED),
        *_cases(livecart_core.application, APPLICATION_ALLOWED),
    ],
)
def test_the_inner_layers_import_inward_only(
    path: Path, package: str, allowed: frozenset[str]
) -> None:
    source = path.read_text(encoding="utf-8")

    assert _violations(source, package=package, allowed=allowed) == []


def test_the_walk_finds_the_packages_and_their_modules() -> None:
    domain = [module for module, _, _ in _modules(livecart_core.domain)]
    application = [module for module, _, _ in _modules(livecart_core.application)]

    assert "livecart_core.domain" in domain
    assert "livecart_core.domain.money" in domain
    assert "livecart_core.domain.state_machines" in domain
    # Subpackages are walked too.
    assert "livecart_core.domain.parser" in domain
    assert "livecart_core.domain.parser.grammar" in domain
    assert "livecart_core.application" in application
    assert "livecart_core.application.ports" in application
    assert "livecart_core.application.dto" in application
    assert "livecart_core.application.seller_auth" in application
    assert "livecart_core.application.live_sessions" in application
    assert "livecart_core.application.products" in application
    assert "livecart_core.application.idempotency" in application
    assert "livecart_core.application.storefront" in application
    assert "livecart_core.application.buyer_orders" in application
    assert "livecart_core.application.demo_seed" in application
    assert "livecart_core.application.ingest_comments" in application
    assert "livecart_core.application.process_comment" in application
    assert "livecart_core.application.session_events" in application
    assert "livecart_core.application.reclaim_comments" in application
    assert "livecart_core.application.expire_reservations" in application
    assert "livecart_core.domain.sellers" in domain
    assert "livecart_core.domain.live_sessions" in domain


# A module that mixes allowed and forbidden imports, at the top level, relative,
# under TYPE_CHECKING and inside a function.
SAMPLE = """
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pydantic
from pydantic.fields import Field
import pydantic_settings
from sqlalchemy import select
from livecart_core.settings import Settings
from livecart_core.domain.money import Money
import livecart_core.domain.errors
from . import errors
from .codes import ProductCode
from ..settings import Settings as RelativeSettings

if TYPE_CHECKING:
    from redis.asyncio import Redis


def later() -> None:
    import structlog
"""


def test_the_check_reports_every_import_outside_the_domain() -> None:
    violations = _violations(SAMPLE, package="livecart_core.domain", allowed=DOMAIN_ALLOWED)

    assert sorted(violations) == [
        "livecart_core.settings",
        "livecart_core.settings",
        "pydantic",
        "pydantic.fields",
        "pydantic_settings",
        "redis.asyncio",
        "sqlalchemy",
        "structlog",
    ]


def test_the_application_may_add_pydantic_but_nothing_else() -> None:
    violations = _violations(
        SAMPLE, package="livecart_core.application", allowed=APPLICATION_ALLOWED
    )

    assert sorted(violations) == [
        "livecart_core.settings",
        "livecart_core.settings",
        "pydantic_settings",
        "redis.asyncio",
        "sqlalchemy",
        "structlog",
    ]


# --- No SQL written by hand ------------------------------------------------------------

# The SQLAlchemy names that take raw SQL text. The adapters build every
# statement with Core constructs instead (insert, update().where().returning(),
# select().with_for_update()): hand-written SQL lives in infra/postgres only,
# and in the migrations, which are DDL frozen in time.
RAW_SQL_NAMES = frozenset({"text", "literal_column", "TextClause"})
# The migrations, and the test support that only tests import.
RAW_SQL_EXEMPT = ("livecart_core.migrations", "livecart_core.testing", "livecart_api.testing")
RUNTIME_PACKAGES = (livecart_core, livecart_api, livecart_worker)


def _raw_sql_uses(source: str) -> list[str]:
    """``from sqlalchemy... import text`` and ``sa.text`` where ``sa`` is a sqlalchemy import."""
    tree = ast.parse(source)
    aliases: set[str] = set()
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update(
                (alias.asname or alias.name).partition(".")[0]
                for alias in node.names
                if alias.name.partition(".")[0] == "sqlalchemy"
            )
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sqlalchemy"):
            found.extend(
                f"{node.module}.{alias.name}" for alias in node.names if alias.name in RAW_SQL_NAMES
            )
            # `from sqlalchemy import sql` then `sql.text(...)`.
            aliases.update(
                alias.asname or alias.name for alias in node.names if alias.name == "sql"
            )
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in RAW_SQL_NAMES
            and isinstance(node.value, ast.Name | ast.Attribute)
            and _root_name(node.value) in aliases
        ):
            found.append(f"{_root_name(node.value)}...{node.attr}")
    return found


def _root_name(node: ast.expr) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _runtime_modules() -> dict[str, Path]:
    """Every module of the core, the API and the worker but the migrations and the test support."""
    return {
        module: path
        for package in RUNTIME_PACKAGES
        for module, path, _ in _modules(package)
        if not module.startswith(RAW_SQL_EXEMPT)
    }


@pytest.mark.parametrize(
    "path", [pytest.param(path, id=module) for module, path in _runtime_modules().items()]
)
def test_no_runtime_module_writes_sql_by_hand(path: Path) -> None:
    assert _raw_sql_uses(path.read_text(encoding="utf-8")) == []


def test_the_raw_sql_check_finds_every_spelling() -> None:
    sample = """
import sqlalchemy as sa
from sqlalchemy import text, select
from sqlalchemy.sql import literal_column
from sqlalchemy import sql

def query():
    sa.text("SELECT 1")
    sql.expression.text("SELECT 1")
    select(sa.column("id"))
"""

    assert sorted(_raw_sql_uses(sample)) == [
        "sa...text",
        "sql...text",
        "sqlalchemy.sql.literal_column",
        "sqlalchemy.text",
    ]


def test_the_adapters_are_among_the_modules_checked_for_raw_sql() -> None:
    checked = set(_runtime_modules())

    assert "livecart_core.infrastructure.db.orders" in checked
    assert "livecart_core.infrastructure.db.comments" in checked
    assert "livecart_core.infrastructure.db.live_sessions" in checked
    assert "livecart_core.infrastructure.idempotency" in checked
    assert "livecart_core.infrastructure.ratelimit" in checked
    assert "livecart_core.infrastructure.comment_queue" in checked
    assert "livecart_core.infrastructure.platforms.simulator" in checked
    assert "livecart_core.signing" in checked
    assert "livecart_core.infrastructure.db.session_reset" in checked
    assert "livecart_core.seed" in checked
    assert "livecart_core.infrastructure.db.products" in checked
    assert "livecart_core.infrastructure.db.sellers" in checked
    assert "livecart_core.infrastructure.db.refresh_tokens" in checked
    assert "livecart_core.infrastructure.db.tables" in checked
    assert not any(name.startswith(RAW_SQL_EXEMPT) for name in checked)


def test_the_entry_points_are_among_the_modules_checked_for_raw_sql() -> None:
    checked = set(_runtime_modules())

    assert "livecart_api.app" in checked
    assert "livecart_api.routes.orders" in checked
    assert "livecart_api.routes.storefronts" in checked
    assert "livecart_api.routes.webhooks" in checked
    assert "livecart_api.cli.create_seller" in checked
    assert "livecart_api.middleware.security_headers" in checked
    assert "livecart_worker.main" in checked
    assert "livecart_worker.heartbeat" in checked
    assert "livecart_worker.consumer" in checked
    assert "livecart_worker.reclaim" in checked
    assert "livecart_worker.sweeps" in checked
    assert "livecart_api.testing" not in checked


def test_relative_imports_resolve_against_the_owning_package() -> None:
    assert _resolve_relative("livecart_core.domain", 1, "errors") == "livecart_core.domain.errors"
    assert _resolve_relative("livecart_core.domain", 1, None) == "livecart_core.domain"
    assert _resolve_relative("livecart_core.domain", 2, "settings") == "livecart_core.settings"
    assert _resolve_relative("livecart_core.domain", 2, None) == "livecart_core"
