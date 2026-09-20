"""Ticket #340 - `required_sections` must list a markdown template's headings.

`list_ticket_templates` and both refusal payloads (`template_violation`,
`template_required`) reported `required_sections: []` for a `kind="markdown"`
template, while `create_ticket` enforced its headings anyway. Harness helpers
are reused from tests/test_307_ticket_templates.py (Route A: real
GitHubProvider + httpx.MockTransport).
"""

from __future__ import annotations

import httpx
import pytest

from lib_python_projects import templates as templates_lib
from lib_python_projects.providers import github as github_provider

from project_issues_plugin.tools.tickets import _template_view

from tests.test_307_ticket_templates import (
    _assert_template_view_shape,
    _b64,
    _install_mock,
    _json,
    _project,
    _register_tools_with,
)

_HEADINGS = ["Steps to Reproduce", "Expected Behavior", "Actual Behavior"]

_BUG_MD = """\
---
name: Bug Report
about: Report a bug
labels: bug
---

## Steps to Reproduce

1. ...

## Expected Behavior

## Actual Behavior
"""

_NO_HEADINGS_BODY = "no headings at all"


def _md_handler(md_text: str):
    def handler(req: httpx.Request) -> httpx.Response:
        base = "/repos/acme/backend"
        if req.method == "GET" and req.url.path == f"{base}/contents/.github/ISSUE_TEMPLATE":
            return _json([
                {"name": "bug_report.md", "path": ".github/ISSUE_TEMPLATE/bug_report.md",
                 "type": "file"},
            ])
        if req.method == "GET" and req.url.path == f"{base}/contents/.github/ISSUE_TEMPLATE/bug_report.md":
            return _json({"content": _b64(md_text), "encoding": "base64"})
        raise AssertionError(f"unexpected request: {req.method} {req.url}")
    return handler


def _setup(monkeypatch: pytest.MonkeyPatch, md_text: str = _BUG_MD):
    tools = _register_tools_with(monkeypatch, _project())
    seen = _install_mock(monkeypatch, _md_handler(md_text))
    return tools, seen


def _real_template():
    return github_provider.GitHubProvider().list_issue_templates(
        _project(), token=None,
    )[0]


def _headings(entry: dict) -> list[str]:
    return [rs["heading"] for rs in entry["required_sections"]]


def test_list_ticket_templates_reports_markdown_required_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1 driving test. RED today: `required_sections` is `[]` because a
    markdown template has no `fields`."""
    tools, _ = _setup(monkeypatch)

    result = tools["list_ticket_templates"](project_id="acme")
    assert "error" not in result, result
    assert len(result["templates"]) == 1
    entry = result["templates"][0]
    _assert_template_view_shape(entry)
    assert entry["kind"] == "markdown"

    real = _real_template()
    violations = templates_lib.validate_ticket_body(_NO_HEADINGS_BODY, real)
    assert _headings(entry) == [v.field_label for v in violations]
    # skeleton order
    assert _headings(entry) == _HEADINGS
    positions = [entry["skeleton"].index(h) for h in _headings(entry)]
    assert positions == sorted(positions)


def test_markdown_refusals_carry_the_same_required_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2 driving test. RED today: both refusal payloads carry
    `required_sections: []`."""
    tools, seen = _setup(monkeypatch)
    listed = tools["list_ticket_templates"](project_id="acme")["templates"][0]
    expected = _template_view(_real_template())
    expected.pop("skeleton")
    listed_summary = {k: v for k, v in listed.items() if k != "skeleton"}
    assert listed_summary == expected

    violation = tools["create_ticket"](
        project_id="acme", title="Bug", body=_NO_HEADINGS_BODY, template="Bug Report",
    )
    assert violation["state"] == "template_violation"
    assert violation["written"] is False
    assert [v["field_label"] for v in violation["violations"]] == _HEADINGS
    assert violation["template"]["required_sections"] == expected["required_sections"]
    assert _headings(violation["template"]) == _HEADINGS

    required = tools["create_ticket"](project_id="acme", title="Bug", body=_NO_HEADINGS_BODY)
    assert required["state"] == "template_required"
    assert required["written"] is False
    assert len(required["templates"]) == 1
    assert required["templates"][0]["required_sections"] == expected["required_sections"]
    assert _headings(required["templates"][0]) == _HEADINGS

    assert all(r.method == "GET" for r in seen), "refusal must not write"


def test_repeated_heading_is_listed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    md = "---\nname: Bug Report\nlabels: bug\n---\n\n## A\n\n## B\n\n## A\n"
    tools, _ = _setup(monkeypatch, md)
    entry = tools["list_ticket_templates"](project_id="acme")["templates"][0]
    assert _headings(entry) == ["A", "B"]


def test_fenced_heading_is_not_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    md = "---\nname: Bug Report\nlabels: bug\n---\n\n## Real\n\n```\n## Fake\n```\n"
    tools, _ = _setup(monkeypatch, md)
    entry = tools["list_ticket_templates"](project_id="acme")["templates"][0]
    assert _headings(entry) == ["Real"]


def test_markdown_template_without_headings_yields_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    md = "---\nname: Bug Report\nlabels: bug\n---\n\nJust prose, no headings.\n"
    tools, _ = _setup(monkeypatch, md)
    entry = tools["list_ticket_templates"](project_id="acme")["templates"][0]
    assert entry["required_sections"] == []
