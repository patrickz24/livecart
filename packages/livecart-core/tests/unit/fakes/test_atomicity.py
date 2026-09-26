"""The rule every repository fake rests on: none of its methods awaits.

Under asyncio a coroutine only yields at an await, so a method without one
runs whole, like the transaction of the SQL adapter it stands for. The rule
is checked on the source, since a test of behaviour would only catch an
await that happens to suspend.
"""

import ast
import inspect

import pytest

from livecart_core.testing import fakes

_TREE = ast.parse(inspect.getsource(fakes))
_REPOSITORIES = [
    node
    for node in _TREE.body
    if isinstance(node, ast.ClassDef)
    and any(isinstance(base, ast.Name) and base.id == "_Repository" for base in node.bases)
]
_SUSPENDING = (ast.Await, ast.AsyncFor, ast.AsyncWith)


def test_every_repository_fake_is_found() -> None:
    assert {node.name for node in _REPOSITORIES} == {
        "InMemoryCommentInbox",
        "InMemoryLiveSessionRepository",
        "InMemoryProductStockRepository",
        "InMemoryOrderRepository",
        "InMemoryPaymentRepository",
        "InMemoryShipmentRepository",
        "InMemoryWebhookEventRegistry",
    }


@pytest.mark.parametrize("repository", _REPOSITORIES, ids=lambda node: node.name)
def test_no_method_of_a_repository_fake_awaits(repository: ast.ClassDef) -> None:
    awaiting = sorted(
        f"{method.name} (line {node.lineno})"
        for method in repository.body
        if isinstance(method, ast.AsyncFunctionDef | ast.FunctionDef)
        for node in ast.walk(method)
        if isinstance(node, _SUSPENDING)
    )

    assert awaiting == []
