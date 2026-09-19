"""Driving tests for #325: the template-enforcement refusal payload of
`create_ticket` / `update_ticket` must not ship skeletons inside template
views. `template_required` / `template_unknown` return skeleton-free
`templates` entries; `template_violation` carries the skeleton exactly once
(top-level `skeleton`) and an empty `templates` list.

Harness helpers are reused from tests/test_307_ticket_templates.py (real
`GitHubProvider` against a mocked httpx transport, so the lib's own
`list_issue_templates` / `render_skeleton` / `validate_ticket_body` run for
real; Route B fake provider for the ambiguous-label leg).
"""
from __future__ import annotations

import json
from dataclasses import asdict

import httpx
import pytest

from lib_python_projects import templates as templates_lib
from lib_python_projects.providers import github as github_provider

from project_issues_plugin.tools.tickets import _template_view

from tests.test_307_ticket_templates import (
    _FakeTemplateProvider,
    _fake_ticket,
    _install_mock,
    _json,
    _project,
    _register_fake_provider_tools,
    _register_tools_with,
    _template_content_response,
)

_SUMMARY_KEYS = {"name", "kind", "labels", "title_prefix", "required_sections"}


def _real_template():
    return github_provider.GitHubProvider().list_issue_templates(
        _project(), token=None,
    )[0]


def _expected_summary(template) -> dict:
    """What a refusal's template entry must equal: the real template's view
    (fixture-independent, computed by the tool's own contract) minus skeleton."""
    view = _template_view(template)
    view.pop("skeleton")
    return view


def _escaped(text: str) -> str:
    """`text` as it appears inside a json.dumps string literal."""
    return json.dumps(text)[1:-1]


def _template_handler(req: httpx.Request) -> httpx.Response:
    resp = _template_content_response("acme/backend", req)
    if resp is not None:
        return resp
    raise AssertionError(f"unexpected request: {req.method} {req.url}")


def test_create_ticket_template_required_refusal_carries_no_skeletons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1 driving test. RED today: every `templates` entry carries a
    `skeleton` key and the rendered skeleton text is in the payload."""
    tools = _register_tools_with(monkeypatch, _project())
    _install_mock(monkeypatch, _template_handler)

    result = tools["create_ticket"](
        project_id="acme", title="Bug", body="### Description\nSomething broke\n",
    )

    assert result["state"] == "template_required"
    assert result["skeleton"] is None
    assert result["written"] is False
    assert len(result["templates"]) == 1
    for entry in result["templates"]:
        assert set(entry.keys()) == _SUMMARY_KEYS, entry
    entry = result["templates"][0]
    assert entry["name"] == "Bug Report"
    assert entry["required_sections"], "required_sections must be kept"
    real = _real_template()
    assert entry == _expected_summary(real)
    assert entry["kind"] == real.kind
    assert entry["labels"] == list(real.labels)
    assert entry["title_prefix"] == real.title_prefix

    skeleton = templates_lib.render_skeleton(_real_template())
    assert skeleton
    assert _escaped(skeleton) not in json.dumps(result)


def test_template_violation_refusal_contains_skeleton_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2 driving test. RED today: skeleton appears in `template.skeleton`
    and top-level `skeleton` (twice) and `templates` is non-empty."""
    tools = _register_tools_with(monkeypatch, _project())
    _install_mock(monkeypatch, _template_handler)

    body = "### Severity\nMedium\n"
    result = tools["create_ticket"](
        project_id="acme", title="Bug", body=body, template="Bug Report",
    )

    assert result["state"] == "template_violation"
    real_template = _real_template()
    skeleton = templates_lib.render_skeleton(real_template)

    assert json.dumps(result).count(_escaped(skeleton)) == 1
    assert result["templates"] == []
    assert "skeleton" not in result["template"]
    assert result["template"]["name"] == "Bug Report"
    assert result["template"] == _expected_summary(real_template)
    assert result["template"]["required_sections"]
    assert result["skeleton"] == skeleton
    assert result["violations"] == [
        asdict(v) for v in templates_lib.validate_ticket_body(body, real_template)
    ]
    assert result["received_sections"] == ["Severity"]


def test_template_unknown_refusal_carries_no_skeletons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 driving test. RED today: entries carry `skeleton`."""
    tools = _register_tools_with(monkeypatch, _project())
    _install_mock(monkeypatch, _template_handler)

    result = tools["create_ticket"](
        project_id="acme", title="Bug", body="### Description\nx\n",
        template="Nonexistent",
    )

    assert result["state"] == "template_unknown"
    assert result["skeleton"] is None
    assert result["template"] is None
    assert len(result["templates"]) == 1
    for entry in result["templates"]:
        assert set(entry.keys()) == _SUMMARY_KEYS, entry
    assert result["templates"] == [_expected_summary(_real_template())]
    skeleton = templates_lib.render_skeleton(_real_template())
    assert _escaped(skeleton) not in json.dumps(result)
    assert "Bug Report" not in result["hint"]
    assert "list_ticket_templates" in result["hint"]


def test_update_ticket_refusals_are_trimmed_like_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4 driving test, both `update_ticket` legs. RED today: extra
    `skeleton` keys / duplicated skeleton via the shared refusal builder."""
    # Leg 1: label-inferred template_violation (Route A).
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/repos/acme/backend/issues/42":
            return _json({
                "number": 42, "title": "Bug", "body": "old",
                "state": "open", "user": {"login": "alice"}, "assignees": [],
                "labels": [{"name": "bug"}, {"name": "triage"}, {"name": "ai-generated"}],
                "html_url": "", "created_at": "", "updated_at": "",
            })
        if req.method == "GET" and req.url.path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        if req.method in ("PATCH", "POST"):
            raise AssertionError(f"no write may happen: {req.method} {req.url}")
        return _template_handler(req)

    _install_mock(monkeypatch, handler)

    result = tools["update_ticket"](
        project_id="acme", ticket_id="42", body="### Severity\nMedium\n",
    )
    assert result["state"] == "template_violation"
    skeleton = templates_lib.render_skeleton(_real_template())
    assert json.dumps(result).count(_escaped(skeleton)) == 1
    assert result["templates"] == []
    assert "skeleton" not in result["template"]
    assert result["template"] == _expected_summary(_real_template())
    assert result["skeleton"] == skeleton

    # Leg 2: ambiguous labels -> template_required listing both (Route B).
    template_a = templates_lib.IssueTemplate(
        name="A", filename="a.yml", title_prefix="", labels=["bug"],
        kind="form", fields=[],
    )
    template_b = templates_lib.IssueTemplate(
        name="B", filename="b.yml", title_prefix="", labels=["bug", "urgent"],
        kind="form", fields=[],
    )
    ticket = _fake_ticket(id="42", labels=["bug", "urgent", "ai-generated"])
    provider = _FakeTemplateProvider(
        templates=[template_a, template_b], tickets={"42": ticket},
    )
    tools_b = _register_fake_provider_tools(monkeypatch, _project(), provider)

    result_b = tools_b["update_ticket"](
        project_id="acme", ticket_id="42", body="### x\ny\n",
    )
    assert result_b["state"] == "template_required"
    assert result_b["skeleton"] is None
    assert [e["name"] for e in result_b["templates"]] == ["A", "B"]
    for entry in result_b["templates"]:
        assert set(entry.keys()) == _SUMMARY_KEYS, entry
    assert result_b["templates"] == [
        _expected_summary(template_a), _expected_summary(template_b),
    ]
    assert provider.update_calls == []
