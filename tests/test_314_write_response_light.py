"""Ticket #314 — write tools return a light response by default.

Eight write tools (`create_ticket`, `update_ticket`, `add_comment`,
`update_comment`, `create_pr`, `update_pr`, `merge_pr`, `add_relation`) gain a
`response: "light" | "full"` parameter defaulting to `"light"`.

* R1: with no `response` argument each tool returns exactly its declared light
  key set (a strict subset of the full vocabulary; `None` values are kept).
* R2: `response="full"` is byte-identical (key order included) to the response
  the tools produced before this ticket — pinned below as JSON literals.
* R3: each tool documents the light behaviour (parameter description + docstring).
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Callable

import pytest
from mcp.server.fastmcp.utilities.func_metadata import func_metadata

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers.base import (
    Comment,
    PullRequest,
    Relation,
    Ticket,
)
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import comments as comment_tools
from project_issues_plugin.tools import pulls as pull_tools
from project_issues_plugin.tools import relations as relation_tools
from project_issues_plugin.tools import tickets as ticket_tools


# ---------- fixtures ---------------------------------------------------------


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
        permissions={
            "issues": {"create": True, "modify": True},
            "pulls": {"create": True, "modify": True, "merge": True},
        },
    )


def _ticket() -> Ticket:
    return Ticket(
        id="5",
        title="a title",
        body="#ai-generated\n\nthe body",
        status="open",
        author="alice",
        assignees=["bob"],
        labels=["ai-generated", "ai-modified"],
        url="https://example.test/issues/5",
        created_at="2026-05-18T10:00:00Z",
        updated_at="2026-05-18T20:36:48Z",
    )


def _comment() -> Comment:
    return Comment(
        id="99",
        author="alice",
        body="#ai-generated\n\nhello",
        url="https://example.test/issues/5#issuecomment-99",
        created_at="2026-05-18T10:00:00Z",
    )


def _pr() -> PullRequest:
    return PullRequest(
        id="7",
        number=7,
        title="a pr",
        body="#ai-generated\n\npr body",
        status="merged",
        draft=False,
        author="alice",
        assignees=["bob"],
        reviewers=[],
        requested_reviewers=["carol"],
        labels=["ai-generated"],
        head={"ref": "feature/x", "sha": "deadbeef", "repo": "acme/backend"},
        base={"ref": "main", "sha": "cafebabe"},
        merged=True,
        mergeable=None,
        url="https://example.test/pull/7",
        created_at="2026-05-18T10:00:00Z",
        updated_at="2026-05-18T20:00:00Z",
        merge_commit_sha="abc123",
    )


def _relation() -> Relation:
    return Relation(
        kind="blocks",
        ticket_id="#7",
        title="target title",
        url="https://example.test/issues/7",
        state="open",
        is_pull_request=False,
        resolved=True,
    )


class _FakeProvider:
    """Returns the fixed dataclasses; deliberately has no
    `list_issue_templates`, so `create_ticket` yields a `template_warning`."""

    def create_ticket(self, *a, **k):
        return _ticket()

    def update_ticket(self, *a, **k):
        return _ticket()

    def add_comment(self, *a, **k):
        return _comment()

    def update_comment(self, *a, **k):
        return _comment()

    def create_pr(self, *a, **k):
        return _pr()

    def update_pr(self, *a, **k):
        return _pr()

    def merge_pr(self, *a, **k):
        return _pr()

    def add_relation(self, *a, **k):
        return _relation()


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> dict[str, Callable]:
    project = _project()

    def fake_load_projects(*_a, **_k):
        return ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    for mod in (pull_tools, ticket_tools, comment_tools, relation_tools):
        if hasattr(mod, "load_projects"):
            monkeypatch.setattr(mod, "load_projects", fake_load_projects)
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "ghp_token")
    monkeypatch.setitem(providers_mod._PROVIDERS, "github", _FakeProvider())

    stub = _StubMCP()
    for mod in (ticket_tools, comment_tools, pull_tools, relation_tools):
        mod.register(stub)
    return stub.tools


# tool name -> (call kwargs, result key of the object, light key set)
_TICKET_LIGHT = {"id", "url", "status", "labels", "custom_fields", "updated_at"}
_COMMENT_LIGHT = {"id", "url", "created_at"}
_PR_LIGHT = {"id", "url", "status", "merged", "mergeable_state", "head"}
_REL_LIGHT = {"kind", "ticket_id"}

_CASES: dict[str, tuple[dict[str, Any], str, set[str]]] = {
    "create_ticket": (
        {"project_id": "acme", "title": "a title", "body": "b"},
        "ticket", _TICKET_LIGHT,
    ),
    "update_ticket": (
        {"project_id": "acme", "ticket_id": "5", "title": "a title"},
        "ticket", _TICKET_LIGHT,
    ),
    "add_comment": (
        {"project_id": "acme", "ticket_id": "5", "body": "hello"},
        "comment", _COMMENT_LIGHT,
    ),
    "update_comment": (
        {"project_id": "acme", "comment_id": "99", "body": "hello", "ticket_id": "5"},
        "comment", _COMMENT_LIGHT,
    ),
    "create_pr": (
        {"project_id": "acme", "title": "a pr", "body": "b", "head": "feature/x",
         "base": "main"},
        "pull_request", _PR_LIGHT,
    ),
    "update_pr": (
        {"project_id": "acme", "pr_id": "7", "title": "a pr"},
        "pull_request", _PR_LIGHT,
    ),
    "merge_pr": (
        {"project_id": "acme", "pr_id": "7"},
        "pull_request", _PR_LIGHT,
    ),
    "add_relation": (
        {"project_id": "acme", "ticket_id": "5", "kind": "blocks", "target": "#7"},
        "relation", _REL_LIGHT,
    ),
}


# ---------- R1: default response is the light key set ------------------------


@pytest.mark.parametrize("tool_name", list(_CASES))
def test_default_response_is_exactly_the_light_key_set(tools, tool_name):
    kwargs, obj_key, light = _CASES[tool_name]
    out = tools[tool_name](**kwargs)
    assert "error" not in out, out
    assert set(out[obj_key]) == light
    assert out["project_id"] == "acme"
    # echo-heavy fields are gone
    for gone in ("body", "comments", "reviews", "title", "author"):
        assert gone not in out[obj_key]


@pytest.mark.parametrize("tool_name", list(_CASES))
def test_light_values_are_the_full_values_for_every_kept_key(
    tools, tool_name
):
    """`light` is a strict subset of `full`: same values for every kept key."""
    kwargs, obj_key, light = _CASES[tool_name]
    lite = tools[tool_name](**kwargs)[obj_key]
    full = tools[tool_name](**kwargs, response="full")[obj_key]
    assert set(lite) == light
    for key in lite:
        assert lite[key] == full[key]


def test_pr_light_keeps_head_dict_with_sha_and_none_mergeable_state(tools):
    out = tools["merge_pr"](project_id="acme", pr_id="7")["pull_request"]
    assert set(out) == _PR_LIGHT
    assert out["head"]["sha"] == "deadbeef"
    assert out["merged"] is True
    assert out["status"] == "merged"
    # None is kept (present-as-None), not dropped
    assert "mergeable_state" in out and out["mergeable_state"] is None


def test_ticket_light_keeps_none_custom_fields(tools):
    out = tools["update_ticket"](
        project_id="acme", ticket_id="5", title="t",
    )["ticket"]
    assert set(out) == _TICKET_LIGHT
    assert "custom_fields" in out and out["custom_fields"] is None


def test_relation_light_keeps_target_ticket_id_and_kind(tools):
    out = tools["add_relation"](
        project_id="acme", ticket_id="5", kind="blocks", target="#7",
    )
    assert out["relation"] == {"kind": "blocks", "ticket_id": "#7"}
    assert out["project_id"] == "acme"


def test_create_ticket_light_keeps_template_and_board_warning(
    tools, monkeypatch
):
    monkeypatch.setattr(
        ticket_tools, "_default_board_custom_fields",
        lambda project, provider, token, cf, off: (cf, "board went wrong"),
    )
    out = tools["create_ticket"](project_id="acme", title="t", body="b")
    assert set(out["ticket"]) == _TICKET_LIGHT
    assert out["board_warning"] == "board went wrong"
    assert out["template_warning"] == "project has no templates"


def test_update_ticket_light_keeps_template_warning(tools):
    out = tools["update_ticket"](
        project_id="acme", ticket_id="5", body="new body",
    )
    assert set(out["ticket"]) == _TICKET_LIGHT
    assert out["template_warning"] == "project has no templates"


def test_invalid_response_value_is_rejected_by_schema(tools):
    for name in _CASES:
        schema = func_metadata(tools[name]).arg_model.model_json_schema()
        prop = schema["properties"]["response"]
        assert prop["default"] == "light"
        assert set(prop["enum"]) == {"light", "full"}


# ---------- R2: response="full" is byte-identical to the old response --------

_FULL_TICKET = (
    '{"id": "5", "title": "a title", "body": "#ai-generated\\n\\nthe body", '
    '"status": "open", "author": "alice", "assignees": ["bob"], '
    '"labels": ["ai-generated", "ai-modified"], '
    '"url": "https://example.test/issues/5", '
    '"created_at": "2026-05-18T10:00:00Z", "updated_at": "2026-05-18T20:36:48Z", '
    '"acceptance_criteria": "", "custom_fields": null, "idempotent_replay": false, '
    '"parent_id": null, "milestone": null}'
)
_FULL_COMMENT = (
    '{"id": "99", "author": "alice", "body": "#ai-generated\\n\\nhello", '
    '"url": "https://example.test/issues/5#issuecomment-99", '
    '"created_at": "2026-05-18T10:00:00Z", "updated_at": ""}'
)
_FULL_PR = (
    '{"id": "7", "number": 7, "title": "a pr", "body": "#ai-generated\\n\\npr body", '
    '"status": "merged", "draft": false, "author": "alice", "assignees": ["bob"], '
    '"reviewers": [], "requested_reviewers": ["carol"], "labels": ["ai-generated"], '
    '"head": {"ref": "feature/x", "sha": "deadbeef", "repo": "acme/backend"}, '
    '"base": {"ref": "main", "sha": "cafebabe"}, "merged": true, "mergeable": null, '
    '"url": "https://example.test/pull/7", "created_at": "2026-05-18T10:00:00Z", '
    '"updated_at": "2026-05-18T20:00:00Z", "mergeable_state": null, '
    '"merge_commit_sha": "abc123", "review_decision": null, "auto_merge": null, '
    '"detailed_merge_status": null, "pipeline_status": null, '
    '"approvals_required": null, "approvals_received": null, "reviews": [], '
    '"warnings": [], "idempotent_replay": false}'
)
_FULL_RELATION = (
    '{"kind": "blocks", "ticket_id": "#7", "title": "target title", '
    '"url": "https://example.test/issues/7", "state": "open", '
    '"is_pull_request": false, "resolved": true}'
)

_FULL_EXPECTED = {
    "create_ticket": (
        '{"project_id": "acme", "ticket": ' + _FULL_TICKET
        + ', "template_warning": "project has no templates"}'
    ),
    "update_ticket": (
        '{"project_id": "acme", "ticket": ' + _FULL_TICKET
        + ', "template_warning": "project has no templates"}'
    ),
    "add_comment": '{"project_id": "acme", "comment": ' + _FULL_COMMENT + "}",
    "update_comment": '{"project_id": "acme", "comment": ' + _FULL_COMMENT + "}",
    "create_pr": '{"project_id": "acme", "pull_request": ' + _FULL_PR + "}",
    "update_pr": '{"project_id": "acme", "pull_request": ' + _FULL_PR + "}",
    "merge_pr": '{"project_id": "acme", "pull_request": ' + _FULL_PR + "}",
    "add_relation": '{"project_id": "acme", "relation": ' + _FULL_RELATION + "}",
}


@pytest.mark.parametrize("tool_name", list(_CASES))
def test_full_response_is_byte_identical_to_pre_change_snapshot(tools, tool_name):
    kwargs = dict(_CASES[tool_name][0])
    if tool_name == "update_ticket":
        kwargs["body"] = "new body"  # triggers the template gate like today
    out = tools[tool_name](**kwargs, response="full")
    assert json.dumps(out, ensure_ascii=False) == _FULL_EXPECTED[tool_name]


def test_snapshot_literals_match_the_dataclasses():
    """Guards the pinned literals against typos: they are the dataclasses'
    own asdict output."""
    assert json.dumps(asdict(_ticket()), ensure_ascii=False) == _FULL_TICKET
    assert json.dumps(asdict(_comment()), ensure_ascii=False) == _FULL_COMMENT
    assert json.dumps(asdict(_pr()), ensure_ascii=False) == _FULL_PR
    assert json.dumps(asdict(_relation()), ensure_ascii=False) == _FULL_RELATION


# ---------- R3: documentation ------------------------------------------------


def _desc(fn: Callable) -> str:
    schema = func_metadata(fn).arg_model.model_json_schema()
    return schema.get("properties", {}).get("response", {}).get("description", "")


def _text(fn: Callable) -> str:
    return _desc(fn) + " " + (fn.__doc__ or "")


_DOC_CASES = {
    name: sorted(light) for name, (_, _, light) in _CASES.items()
}


@pytest.mark.parametrize("tool_name", list(_CASES))
def test_response_parameter_has_a_description(tools, tool_name):
    schema = func_metadata(tools[tool_name]).arg_model.model_json_schema()
    assert schema["properties"]["response"].get("description")


@pytest.mark.parametrize("tool_name", list(_CASES))
def test_docs_state_light_set_default_full_pointer_and_none_fields(
    tools, tool_name
):
    text = _text(tools[tool_name])
    for key in _DOC_CASES[tool_name]:
        assert key in text, f"{tool_name}: light key {key!r} undocumented"
    assert 'response="full"' in text
    assert "light" in text
    assert "default" in text.lower()
    assert "None" in text
    assert "reload" in text.lower()
    assert "still applied" in text


@pytest.mark.parametrize("tool_name", ["create_ticket", "update_ticket"])
def test_ticket_docs_point_at_get_ticket_for_fresh_status(tools, tool_name):
    text = _desc(tools[tool_name])
    assert "get_ticket(..., include_custom_fields=True)" in text
    assert "pre-cascade" in text


@pytest.mark.parametrize("tool_name", ["create_pr", "update_pr", "merge_pr"])
def test_pr_docs_map_ac_aliases_to_light_keys(tools, tool_name):
    text = _desc(tools[tool_name])
    assert "head.sha" in text
    assert "head_sha" in text
    assert "number" in text
    assert "state" in text


def test_relation_docs_map_target_alias(tools):
    text = _desc(tools["add_relation"])
    assert "relation.ticket_id" in text
    assert "target" in text
