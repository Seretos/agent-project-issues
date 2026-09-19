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
import re
from typing import Any, Callable

import pytest
from mcp.server.fastmcp.utilities.func_metadata import func_metadata

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers.base import (
    Comment,
    PullRequest,
    Relation,
    Review,
    ReviewComment,
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
        head={"ref": "feature/x", "sha": "deadbeef", "repo_full_name": "acme/backend"},
        base={"ref": "main", "sha": "cafebabe"},
        merged=True,
        mergeable=None,
        url="https://example.test/pull/7",
        created_at="2026-05-18T10:00:00Z",
        updated_at="2026-05-18T20:00:00Z",
        merge_commit_sha="abc123",
    )


def _review() -> Review:
    return Review(
        id="31",
        state="approve",
        author="alice",
        body="#ai-generated\n\nlgtm",
        url="https://example.test/pull/7#pullrequestreview-31",
        submitted_at="2026-05-18T11:00:00Z",
        commit_sha="deadbeef",
    )


def _review_comment() -> ReviewComment:
    return ReviewComment(
        id="55",
        author="alice",
        body="#ai-generated\n\nnit",
        path="src/foo.py",
        line=3,
        side="RIGHT",
        commit_sha="deadbeef",
        created_at="2026-05-18T10:30:00Z",
        url="https://example.test/pull/7#discussion_r55",
        discussion_id="55",
    )


def _relation() -> Relation:
    return Relation(
        kind="blocks",
        ticket_id="#70",
        title="target title",
        url="https://example.test/issues/70",
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

    def add_pr_comment(self, *a, **k):
        return _comment()

    def add_pr_review_comment(self, *a, **k):
        return _review_comment()

    def submit_pr_review(self, *a, **k):
        return _review()


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


# tool name -> (call kwargs, result key of the object, light key set,
#               fields light does NOT echo that the description must name)
_TICKET_LIGHT = {"id", "url", "status", "labels", "custom_fields", "updated_at"}
_COMMENT_LIGHT = {"id", "url", "created_at"}
_PR_LIGHT = {"id", "url", "status", "merged", "mergeable_state", "head"}
_REL_LIGHT = {"kind", "ticket_id"}
_REVIEW_LIGHT = {"id", "url", "submitted_at"}
_REVIEW_COMMENT_LIGHT = {"id", "url", "created_at", "discussion_id"}

_CASES: dict[str, tuple[dict[str, Any], str, set[str], tuple[str, ...]]] = {
    "create_ticket": (
        {"project_id": "acme", "title": "a title", "body": "b"},
        "ticket", _TICKET_LIGHT, ("title",),
    ),
    "update_ticket": (
        {"project_id": "acme", "ticket_id": "5", "title": "a title"},
        "ticket", _TICKET_LIGHT, ("title",),
    ),
    "add_comment": (
        {"project_id": "acme", "ticket_id": "5", "body": "hello"},
        "comment", _COMMENT_LIGHT, ("body", "updated_at"),
    ),
    "update_comment": (
        {"project_id": "acme", "comment_id": "99", "body": "hello", "ticket_id": "5"},
        "comment", _COMMENT_LIGHT, ("body", "updated_at"),
    ),
    "create_pr": (
        {"project_id": "acme", "title": "a pr", "body": "b", "head": "feature/x",
         "base": "main"},
        "pull_request", _PR_LIGHT, ("title", "body", "draft"),
    ),
    "update_pr": (
        {"project_id": "acme", "pr_id": "7", "title": "a pr"},
        "pull_request", _PR_LIGHT, ("title", "body", "draft"),
    ),
    "merge_pr": (
        {"project_id": "acme", "pr_id": "7"},
        "pull_request", _PR_LIGHT, ("title", "body", "draft"),
    ),
    "add_relation": (
        {"project_id": "acme", "ticket_id": "5", "kind": "parent", "target": "#7"},
        "relation", _REL_LIGHT, ("title", "state"),
    ),
    "add_pr_comment": (
        {"project_id": "acme", "pr_id": "7", "body": "hello"},
        "comment", _COMMENT_LIGHT, ("body", "updated_at"),
    ),
    "submit_pr_review": (
        {"project_id": "acme", "pr_id": "7", "state": "approve"},
        "review", _REVIEW_LIGHT, ("state", "body"),
    ),
    "add_pr_review_comment": (
        {"project_id": "acme", "pr_id": "7", "body": "nit",
         "in_reply_to": "55"},
        "review_comment", _REVIEW_COMMENT_LIGHT,
        ("path", "line", "side", "commit_sha", "body"),
    ),
}


# ---------- R1: default response is the light key set ------------------------


@pytest.mark.parametrize("tool_name", list(_CASES))
def test_default_response_is_exactly_the_light_key_set(tools, tool_name):
    kwargs, obj_key, light, _omitted = _CASES[tool_name]
    out = tools[tool_name](**kwargs)
    assert "error" not in out, out
    assert set(out[obj_key]) == light
    assert out["project_id"] == "acme"


@pytest.mark.parametrize("tool_name", list(_CASES))
def test_light_values_are_the_full_values_for_every_kept_key(
    tools, tool_name
):
    """`light` is a strict subset of `full`: same values for every kept key."""
    kwargs, obj_key, light, _omitted = _CASES[tool_name]
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
        project_id="acme", ticket_id="5", kind="parent", target="#7",
    )
    # The fake provider returns kind="blocks"/ticket_id="#70": values must come
    # from the provider's response, not be echoed from the call's arguments.
    assert out["relation"] == {"kind": "blocks", "ticket_id": "#70"}
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
    '"head": {"ref": "feature/x", "sha": "deadbeef", "repo_full_name": "acme/backend"}, '
    '"base": {"ref": "main", "sha": "cafebabe"}, "merged": true, "mergeable": null, '
    '"url": "https://example.test/pull/7", "created_at": "2026-05-18T10:00:00Z", '
    '"updated_at": "2026-05-18T20:00:00Z", "mergeable_state": null, '
    '"merge_commit_sha": "abc123", "review_decision": null, "auto_merge": null, '
    '"detailed_merge_status": null, "pipeline_status": null, '
    '"approvals_required": null, "approvals_received": null, "reviews": [], '
    '"warnings": [], "idempotent_replay": false}'
)
_FULL_RELATION = (
    '{"kind": "blocks", "ticket_id": "#70", "title": "target title", '
    '"url": "https://example.test/issues/70", "state": "open", '
    '"is_pull_request": false, "resolved": true}'
)

_FULL_REVIEW = (
    '{"id": "31", "state": "approve", "author": "alice", '
    '"body": "#ai-generated\\n\\nlgtm", '
    '"url": "https://example.test/pull/7#pullrequestreview-31", '
    '"submitted_at": "2026-05-18T11:00:00Z", "commit_sha": "deadbeef"}'
)
_FULL_REVIEW_COMMENT = (
    '{"id": "55", "author": "alice", "body": "#ai-generated\\n\\nnit", '
    '"path": "src/foo.py", "line": 3, "original_line": null, "side": "RIGHT", '
    '"commit_sha": "deadbeef", "in_reply_to": null, '
    '"created_at": "2026-05-18T10:30:00Z", "updated_at": "", '
    '"url": "https://example.test/pull/7#discussion_r55", '
    '"discussion_id": "55"}'
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
    "add_pr_comment": '{"project_id": "acme", "comment": ' + _FULL_COMMENT + "}",
    "submit_pr_review": '{"project_id": "acme", "review": ' + _FULL_REVIEW + "}",
    "add_pr_review_comment": (
        '{"project_id": "acme", "review_comment": ' + _FULL_REVIEW_COMMENT + "}"
    ),
}


@pytest.mark.parametrize("tool_name", list(_CASES))
def test_full_response_is_byte_identical_to_pre_change_snapshot(tools, tool_name):
    kwargs = dict(_CASES[tool_name][0])
    if tool_name == "update_ticket":
        kwargs["body"] = "new body"  # triggers the template gate like today
    out = tools[tool_name](**kwargs, response="full")
    assert json.dumps(out, ensure_ascii=False) == _FULL_EXPECTED[tool_name]


# ---------- R3: documentation ------------------------------------------------
# Every check reads the `response` PARAMETER description only: it does not exist
# before this ticket, so pre-existing docstring prose cannot pre-satisfy it.


def _desc(fn: Callable) -> str:
    schema = func_metadata(fn).arg_model.model_json_schema()
    return schema.get("properties", {}).get("response", {}).get("description", "")


def _ticks(text: str) -> set[str]:
    return set(re.findall(r"`([^`]+)`", text))


_PR_TOOLS = ["create_pr", "update_pr", "merge_pr"]
# dropped-from-light keys that MAY be named in the "use full for ..." pointer
_POINTER_ALLOWED = {"body", "comments", "reviews"}


@pytest.mark.parametrize("tool_name", list(_CASES))
def test_response_parameter_has_a_description(tools, tool_name):
    schema = func_metadata(tools[tool_name]).arg_model.model_json_schema()
    assert schema["properties"]["response"].get("description")


@pytest.mark.parametrize("tool_name", list(_CASES))
def test_docs_list_exactly_the_light_keys(tools, tool_name):
    kwargs, obj_key, light, _omitted = _CASES[tool_name]
    desc = _desc(tools[tool_name])
    ticks = _ticks(desc)
    for key in light:
        assert key in ticks, f"{tool_name}: light key `{key}` not backticked"
    # no key that light DROPS may be documented as returned
    full_keys = set(tools[tool_name](**kwargs, response="full")[obj_key])
    # `number` is also a dropped full-PR key, but the PR docs must backtick it
    # as the AC alias of light key `id` (see test_pr_docs_map_ac_aliases_...),
    # so it cannot also be forbidden here.
    for key in (full_keys - light) - _POINTER_ALLOWED - {"number"}:
        assert key not in ticks, (
            f"{tool_name}: `{key}` is not in the light set but is backticked"
        )
    if tool_name in _PR_TOOLS:
        # set EQUALITY on the head sub-keys: the description names exactly the
        # sub-keys the real light `head` carries, no more and no fewer.
        head_keys = set(tools[tool_name](**kwargs)[obj_key]["head"])
        assert head_keys == {"ref", "sha", "repo_full_name"}
        documented = {t for t in ticks if t.startswith("head.")}
        assert documented == {f"head.{k}" for k in head_keys}


@pytest.mark.parametrize("tool_name", _PR_TOOLS)
def test_pr_light_head_keeps_repo_full_name_key_when_none(
    tools, monkeypatch, tool_name
):
    """GitLab yields `repo_full_name=None` for an unresolved cross-fork source:
    the key must survive light (present-as-None) and be documented as nullable."""
    def pr_none(*a, **k):
        pr = _pr()
        pr.head = {"ref": "feature/x", "sha": "deadbeef", "repo_full_name": None}
        return pr

    monkeypatch.setattr(_FakeProvider, tool_name, pr_none)
    kwargs, obj_key, _light, _omitted = _CASES[tool_name]
    head = tools[tool_name](**kwargs)[obj_key]["head"]
    assert set(head) == {"ref", "sha", "repo_full_name"}
    assert head["repo_full_name"] is None
    desc = _desc(tools[tool_name])
    assert re.search(
        r"head\.repo_full_name`?[^.]{0,120}\bNone\b|\bNone\b[^.]{0,120}head\.repo_full_name",
        desc,
    )


@pytest.mark.parametrize("tool_name", list(_CASES))
def test_docs_name_the_fields_light_does_not_echo(tools, tool_name):
    """Each description says (in plain words, not as a returned key) which
    edited fields light does not confirm."""
    _kwargs, _obj_key, _light, omitted = _CASES[tool_name]
    desc = _desc(tools[tool_name])
    ticks = _ticks(desc)
    for field in omitted:
        # named in the *absent* sense: inside a sentence saying light does not
        # echo it / it is absent from the response
        assert any(
            re.search(rf"\b{re.escape(field)}\b", sent)
            and re.search(r"does not echo|\babsent\b|not returned", sent, re.I)
            for sent in re.split(r"(?<=\.)\s+", desc)
        ), f"{tool_name}: light-omitted `{field}` is not named as absent"
        assert field not in ticks, (
            f"{tool_name}: `{field}` is omitted by light but backticked as returned"
        )


@pytest.mark.parametrize("tool_name", list(_CASES))
def test_docs_state_default_light_full_pointer_no_reload_labels_none(
    tools, tool_name
):
    fn = tools[tool_name]
    desc = _desc(fn)
    assert 'response="full"' in desc
    assert re.search(r"default.{0,40}light|light.{0,40}default", desc, re.I | re.S)
    assert re.search(r"\b(no|without an?)\s+(extra\s+)?reload", desc, re.I)
    assert re.search(r"labels?\b.{0,80}still applied", desc, re.I | re.S)
    assert re.search(r"\bNone\b", desc)
    # one-line default note on the tool docstring or the description
    both = desc + " " + (fn.__doc__ or "")
    assert 'response="light"' in both and 'response="full"' in both


@pytest.mark.parametrize("tool_name", _PR_TOOLS)
def test_pr_docs_name_provider_specific_none_fields(tools, tool_name):
    desc = _desc(tools[tool_name])
    assert re.search(
        r"\bNone\b.{0,120}(GitHub|GitLab|Azure)|(GitHub|GitLab|Azure).{0,120}\bNone\b",
        desc, re.S,
    )


@pytest.mark.parametrize("tool_name", ["create_ticket", "update_ticket"])
def test_ticket_docs_point_at_get_ticket_for_fresh_status(tools, tool_name):
    desc = _desc(tools[tool_name])
    assert re.search(r"pre-cascade", desc)
    assert re.search(
        r"get_ticket\(\.\.\.,\s*include_custom_fields=True\)", desc
    )


def _paired(text: str, a: str, b: str) -> bool:
    a, b = re.escape(a), re.escape(b)
    return bool(re.search(
        rf"`{a}`[^.]{{0,40}}`{b}`|`{b}`[^.]{{0,40}}`{a}`", text
    ))


@pytest.mark.parametrize("tool_name", _PR_TOOLS)
def test_pr_docs_map_ac_aliases_to_light_keys(tools, tool_name):
    desc = _desc(tools[tool_name])
    for alias, key in (("number", "id"), ("state", "status"),
                       ("head_sha", "head.sha")):
        assert _paired(desc, alias, key), f"{alias} not paired with {key}"


def test_relation_docs_map_target_alias(tools):
    desc = _desc(tools["add_relation"])
    assert _paired(desc, "target", "relation.ticket_id")


# Alias semantics (#323): the alias pairs are reading aids. A description must
# say what they are NOT: not keys of the response, and (PR tools) not input
# parameter names either. Concept-level regexes, not an exact phrase.
_NOT_A_RESPONSE_KEY = re.compile(
    r"\bnot\b[^.]{0,60}\b(keys?|fields?)\b[^.]{0,30}\bresponse\b"
    r"|\bnot\b[^.]{0,60}\bresponse\b[^.]{0,30}\b(keys?|fields?)\b",
    re.I,
)
_NOT_AN_INPUT_NAME = re.compile(
    r"\b(neither|nor|not)\b[^.]{0,80}\b(input|parameters?|arguments?)\b", re.I
)


def _alias_sentence(desc: str, alias: str) -> str:
    """The sentence of `desc` that introduces the backticked alias."""
    hits = [s for s in re.split(r"(?<=\.)\s+", desc) if f"`{alias}`" in s]
    assert hits, f"alias `{alias}` not mentioned"
    return hits[0]


@pytest.mark.parametrize("tool_name", _PR_TOOLS)
def test_pr_docs_say_aliases_are_neither_response_keys_nor_input_names(
    tools, tool_name
):
    desc = _desc(tools[tool_name])
    for alias in ("number", "state", "head_sha"):
        sent = _alias_sentence(desc, alias)
        assert _NOT_A_RESPONSE_KEY.search(sent), "aliases not disowned as response keys"
        assert _NOT_AN_INPUT_NAME.search(sent), "aliases not disowned as input names"


def test_relation_docs_say_target_is_not_a_response_key(tools):
    desc = _desc(tools["add_relation"])
    assert _NOT_A_RESPONSE_KEY.search(_alias_sentence(desc, "target"))
