"""The import rule of the inner layers, checked from the source text with ``ast``.

``livecart_core.domain`` may import the standard library and itself only;
``livecart_core.application`` may add pydantic. Reading the source rather
than importing it catches an import inside a function or under
``TYPE_CHECKING`` too, and does not depend on what happens to be installed.
"""

import ast
import inspect
import sys
from pathlib import Path
from types import ModuleType

import pytest

import livecart_core.application
import livecart_core.domain

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


def test_relative_imports_resolve_against_the_owning_package() -> None:
    assert _resolve_relative("livecart_core.domain", 1, "errors") == "livecart_core.domain.errors"
    assert _resolve_relative("livecart_core.domain", 1, None) == "livecart_core.domain"
    assert _resolve_relative("livecart_core.domain", 2, "settings") == "livecart_core.settings"
    assert _resolve_relative("livecart_core.domain", 2, None) == "livecart_core"
