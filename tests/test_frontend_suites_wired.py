"""Every jsdom suite in tests/frontend runs under pytest, so CI runs it.

CI runs pytest, never ``node --test`` on its own: a ``*.test.cjs`` file that no
pytest wrapper names would pass review and never run. A wrapper names a suite
in its code (a docstring or a comment does not count) by its path,
``"tests/frontend/<stem>.test.cjs"``, or by its quoted stem when it builds the
path from an ``f"tests/frontend/{suite}.test.cjs"`` template.
"""

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent
PREFIX, SUFFIX = "tests/frontend/", ".test.cjs"


def _code_strings(tree: ast.Module) -> tuple[set[str], bool]:
    """The module's string literals, docstrings left out, and whether it has a
    ``tests/frontend/{…}.test.cjs`` f-string."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    strings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    }
    template = False
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr) and len(node.values) == 3:
            head, middle, tail = node.values
            template = template or (
                isinstance(head, ast.Constant)
                and head.value == PREFIX
                and isinstance(middle, ast.FormattedValue)
                and isinstance(tail, ast.Constant)
                and tail.value == SUFFIX
            )
    return strings, template


def test_every_jsdom_suite_runs_under_pytest() -> None:
    wrappers = {
        source.name: _code_strings(ast.parse(source.read_text(encoding="utf-8")))
        for source in sorted(TESTS.glob("*.py"))
        if source.name != Path(__file__).name
    }
    suites = sorted(
        suite.name.removesuffix(SUFFIX) for suite in (TESTS / "frontend").glob(f"*{SUFFIX}")
    )
    assert "html-sinks" in suites, suites
    unwired = [
        stem
        for stem in suites
        if not any(
            f"{PREFIX}{stem}{SUFFIX}" in strings or (template and stem in strings)
            for strings, template in wrappers.values()
        )
    ]
    assert unwired == [], (
        f"No pytest wrapper runs these jsdom suites, so CI never does: {unwired}. "
        "Name each in a tests/*.py that runs `node --test` on it."
    )
