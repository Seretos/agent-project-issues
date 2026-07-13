"""Guard test for ticket #218 finding 1 — `search_projects`'s "Interpreting
`score`" docstring block used to describe the `>= 300` / `>= 200` / `< 100`
thresholds but never named the `~100`-`~300` gray zone in between (a path or
description substring hit, or an accumulated sub-token score) as
"not reliably a real match". Locks in the added sentence so a later refactor
can't silently drop the clarification.
"""
from __future__ import annotations

from typing import Callable

from project_issues_plugin.tools import projects as projects_tools


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _register(module) -> dict[str, Callable]:
    stub = _StubMCP()
    module.register(stub)
    return stub.tools


_tools = _register(projects_tools)


def test_search_projects_docstring_names_the_gray_zone_band():
    doc = _tools["search_projects"].__doc__ or ""
    assert "100" in doc and "300" in doc, repr(doc)
    assert "gray zone" in doc, repr(doc)
    assert "NOT reliably a real match" in doc, repr(doc)


def test_search_projects_docstring_still_has_original_thresholds():
    doc = _tools["search_projects"].__doc__ or ""
    assert ">= 300" in doc
    assert ">= 200" in doc
    assert "< 100" in doc
