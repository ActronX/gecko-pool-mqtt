"""Requirements R-OPS: what the deployment and the entry point declare.

TC-OPS-01-01, TC-OPS-04-01.

These properties are asserted against the source rather than at runtime. The
restart policy is a declarative line in the Compose file, and the signal
handlers live inside an asyncio entry point that the project has no harness for.
``TC-OPS-02-01`` uses the same approach for the shutdown order.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_function(source: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run":
            return node
    raise AssertionError("app/main.py no longer has an async run()")


def test_tc_ops_01_01_signals_trigger_orderly_shutdown() -> None:
    """R-OPS-01: SIGINT and SIGTERM set the stop event through the event loop.

    Setting ``stopped`` is what ends ``await stopped.wait()`` and lets the
    ``finally`` block run the orderly shutdown. ``TC-OPS-02-01`` asserts that
    block.
    """
    run = _run_function((REPO_ROOT / "app" / "main.py").read_text(encoding="utf-8"))

    signals: set[str] = set()
    handler_arguments: list[tuple[str, ...]] = []
    for node in ast.walk(run):
        # for sig in (signal.SIGINT, signal.SIGTERM):
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple):
            for element in node.iter.elts:
                if (
                    isinstance(element, ast.Attribute)
                    and isinstance(element.value, ast.Name)
                    and element.value.id == "signal"
                ):
                    signals.add(element.attr)
        # loop.add_signal_handler(sig, request_stop)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_signal_handler"
        ):
            handler_arguments.append(tuple(ast.unparse(argument) for argument in node.args))

    assert signals == {"SIGINT", "SIGTERM"}
    assert ("sig", "request_stop") in handler_arguments

    handler = next(
        node
        for node in ast.walk(run)
        if isinstance(node, ast.FunctionDef) and node.name == "request_stop"
    )
    assert any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "set"
        for node in ast.walk(handler)
    ), "request_stop() no longer sets the stop event"


def test_tc_ops_04_01_service_restarts_after_a_crash() -> None:
    """R-OPS-04: The service declares unless-stopped, so a crash restarts it.

    An orderly ``docker compose stop`` is not restarted, because the policy
    treats an explicit stop as intentional.
    """
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "restart: unless-stopped" in compose
