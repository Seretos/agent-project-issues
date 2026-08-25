"""Tests for ticket #265 — inconsistent client-side error validation.

Two client-side validation gaps, per the approved plan:

  1. `merge_pr`'s `merge_method` is currently `Literal["merge", "squash",
     "rebase"]`, so an invalid value leaks a raw Pydantic `literal_error`
     with a pydantic.dev URL instead of a friendly `{"error": ...}`. Fix
     mirrors `update_pr.status` / `submit_pr_review.state` in the same
     file: plain `str` + `Annotated[..., Field(description=...)]`, with a
     pre-flight guard that runs before `go()` (so before the permission
     gate — see `tests/test_schema_constraints_100.py` and
     `tools/pulls.py` L373-427 / L547-627 for the precedent).

  2. GitLab label `color` on `create_label`/`update_label` has no local
     validation (GitHub already has `_validate_github_color` in
     `tools/labels.py`). Fix adds a `_validate_gitlab_color` mirroring the
     GitHub one, wired into both call sites gated on
     `project.provider == "gitlab"`.

Schema assertions reuse the `_StubMCP` / `func_metadata(fn).arg_model
.model_json_schema()` / `_param_description` pattern from
`tests/test_schema_constraints_100.py`. Behavioural assertions reuse the
fake-provider monkeypatch pattern from `tests/test_257_error_surface_
consistency.py` / `tests/test_230_ensure_board_column.py` (patch
`providers_mod._PROVIDERS` + `providers_mod.load_projects`, plus
`pull_tools.load_projects` for pulls.py per `tests/test_pulls.py`), with
fake providers recording calls in a list so tests can assert zero calls
were made when validation should reject pre-flight.
"""
from __future__ import annotations

from typing import Callable

import pytest
from mcp.server.fastmcp.utilities.func_metadata import func_metadata

from lib_python_projects import (
    IssuesPermissions,
    Permissions,
    ProjectConfig,
    ProjectsLoadResult,
    PullsPermissions,
)
from lib_python_projects.providers.base import Label, PullRequest
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import labels as label_tools
from project_issues_plugin.tools import pulls as pull_tools


# ---------------------------------------------------------------------------
# Shared scaffolding
# ---------------------------------------------------------------------------


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _register_schema(module) -> dict[str, Callable]:
    stub = _StubMCP()
    module.register(stub)
    return stub.tools


def _param_description(fn: Callable, param: str) -> str:
    """Return the Field description for a parameter, or '' if absent."""
    schema = func_metadata(fn).arg_model.model_json_schema()
    prop = schema.get("properties", {}).get(param, {})
    return prop.get("description", "")


# ---------- pulls.py behavioural scaffolding ---------------------------------


def _pulls_project(
    *, provider: str = "github", pulls_merge: bool = True, project_id: str = "acme",
) -> ProjectConfig:
    return ProjectConfig(
        id=project_id,
        provider=provider,
        path=f"{project_id}/backend",
        token_env=f"TOKEN_{project_id.upper()}",
        permissions=Permissions(
            issues=IssuesPermissions(create=True, modify=True),
            pulls=PullsPermissions(create=True, modify=True, merge=pulls_merge),
        ),
    )


def _fake_pr(number: int = 1) -> PullRequest:
    return PullRequest(
        id=str(number),
        number=number,
        title=f"PR {number}",
        body="body",
        status="open",
        draft=False,
        author="alice",
        assignees=[],
        reviewers=[],
        requested_reviewers=[],
        labels=[],
        head={"ref": "feature/x", "sha": "deadbeef"},
        base={"ref": "main", "sha": "cafebabe"},
        merged=False,
        mergeable=True,
        url="https://example.com/pr/1",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
    )


class _FakeMergeProvider:
    """Records every `merge_pr` call so tests can assert zero calls were
    made when the client-side guard should reject pre-flight."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def merge_pr(
        self, project_, token, normalized_pr, *,
        merge_method=None, commit_title=None, commit_message=None,
    ):
        self.calls.append({
            "project": project_,
            "token": token,
            "pr_id": normalized_pr,
            "merge_method": merge_method,
            "commit_title": commit_title,
            "commit_message": commit_message,
        })
        return _fake_pr(1)


def _register_pull_tools(
    monkeypatch: pytest.MonkeyPatch, provider_instance, project: ProjectConfig,
) -> dict[str, Callable]:
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setattr(pull_tools, "load_projects", fake_load_projects)
    monkeypatch.setitem(providers_mod._PROVIDERS, project.provider, provider_instance)
    monkeypatch.setenv(project.token_env, "tok")

    stub = _StubMCP()
    pull_tools.register(stub)
    return stub.tools


# ---------- labels.py behavioural scaffolding --------------------------------


def _labels_project(
    *, provider: str = "gitlab", project_id: str = "acme",
) -> ProjectConfig:
    path = "myorg/myproject/myrepo" if provider == "azuredevops" else f"{project_id}/backend"
    return ProjectConfig(
        id=project_id,
        provider=provider,
        path=path,
        token_env=f"TOKEN_{project_id.upper()}",
        permissions=Permissions(
            issues=IssuesPermissions(create=True, modify=True),
            pulls=PullsPermissions(create=True, modify=True, merge=True),
        ),
    )


class _FakeLabelProvider:
    """Records every `create_label`/`update_label` call so tests can
    assert zero calls were made when the client-side guard should reject
    pre-flight."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create_label(self, project_, token, name, *, color=None, description=None):
        self.calls.append({
            "op": "create", "name": name, "color": color, "description": description,
        })
        return Label(name=name, color=color or "", description=description or "")

    def update_label(
        self, project_, token, name, *,
        new_name=None, color=None, description=None,
    ):
        self.calls.append({
            "op": "update", "name": name, "new_name": new_name,
            "color": color, "description": description,
        })
        return Label(
            name=new_name or name, color=color or "", description=description or "",
        )


def _register_label_tools(
    monkeypatch: pytest.MonkeyPatch, provider_instance, project: ProjectConfig,
) -> dict[str, Callable]:
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setitem(providers_mod._PROVIDERS, project.provider, provider_instance)
    monkeypatch.setenv(project.token_env, "tok")

    stub = _StubMCP()
    label_tools.register(stub)
    return stub.tools


# ---------------------------------------------------------------------------
# Requirement 1a — merge_pr.merge_method schema is no longer a Literal enum
# ---------------------------------------------------------------------------


def test_merge_pr_merge_method_schema_type_is_not_literal_enum():
    """Driving test. Today `merge_method: Literal["merge", "squash",
    "rebase"]` renders as an `"enum"` key in the JSON schema property, which
    is what produces the raw Pydantic `literal_error` at the MCP boundary
    for an invalid value. Expected RED: `"enum"` is present."""
    tools = _register_schema(pull_tools)
    schema = func_metadata(tools["merge_pr"]).arg_model.model_json_schema()
    prop = schema.get("properties", {}).get("merge_method", {})
    assert "enum" not in prop, (
        f"Expected no 'enum' key in merge_method schema property, got: {prop!r}"
    )
    for sub in prop.get("anyOf", []):
        assert "enum" not in sub, (
            f"Expected no 'enum' key in merge_method anyOf sub-schema, got: {sub!r}"
        )


def test_merge_pr_merge_method_description_mentions_merge():
    """Driving test. Today `merge_method` has no `Annotated`/`Field`
    description at all — the schema property carries no `description` key,
    so `_param_description` returns `''`. Expected RED: 'merge' not in ''."""
    tools = _register_schema(pull_tools)
    desc = _param_description(tools["merge_pr"], "merge_method")
    assert "merge" in desc, f"Expected 'merge' in description, got: {desc!r}"


def test_merge_pr_merge_method_description_mentions_squash():
    tools = _register_schema(pull_tools)
    desc = _param_description(tools["merge_pr"], "merge_method")
    assert "squash" in desc, f"Expected 'squash' in description, got: {desc!r}"


def test_merge_pr_merge_method_description_mentions_rebase():
    tools = _register_schema(pull_tools)
    desc = _param_description(tools["merge_pr"], "merge_method")
    assert "rebase" in desc, f"Expected 'rebase' in description, got: {desc!r}"


# ---------------------------------------------------------------------------
# Requirement 1b — invalid merge_method returns a friendly error, no
# provider call.
# ---------------------------------------------------------------------------


def test_merge_pr_invalid_merge_method_returns_friendly_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test. Today there is no guard at all: `merge_method="bogus_
    method"` is forwarded verbatim to the provider (the plain-function test
    call bypasses Pydantic's Literal validation, which only fires at the
    real MCP JSON-RPC boundary), and the fake provider happily returns a
    success envelope. Expected RED: `calls` is non-empty and/or `'error'`
    is absent from the result."""
    provider = _FakeMergeProvider()
    project = _pulls_project(pulls_merge=True)
    tools = _register_pull_tools(monkeypatch, provider, project)

    out = tools["merge_pr"](project_id="acme", pr_id="42", merge_method="bogus_method")

    assert "error" in out, f"expected error dict; got: {out}"
    err = out["error"]
    assert "merge" in err and "squash" in err and "rebase" in err, (
        f"expected all three accepted values named in the error; got: {err!r}"
    )
    assert "bogus_method" in err, f"expected the bad value echoed; got: {err!r}"
    assert provider.calls == [], f"expected no provider call; got: {provider.calls}"


def test_merge_pr_invalid_merge_method_empty_string_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case for requirement 1b. Same missing-guard reason as the
    driving test — expected RED."""
    provider = _FakeMergeProvider()
    project = _pulls_project(pulls_merge=True)
    tools = _register_pull_tools(monkeypatch, provider, project)

    out = tools["merge_pr"](project_id="acme", pr_id="42", merge_method="")

    assert "error" in out, f"expected error dict; got: {out}"
    assert provider.calls == [], f"expected no provider call; got: {provider.calls}"


def test_merge_pr_invalid_merge_method_case_sensitive_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case for requirement 1b: the guard is case-sensitive, so
    'Squash' must be rejected same as any other bogus value. Same
    missing-guard reason as the driving test — expected RED."""
    provider = _FakeMergeProvider()
    project = _pulls_project(pulls_merge=True)
    tools = _register_pull_tools(monkeypatch, provider, project)

    out = tools["merge_pr"](project_id="acme", pr_id="42", merge_method="Squash")

    assert "error" in out, f"expected error dict; got: {out}"
    assert provider.calls == [], f"expected no provider call; got: {provider.calls}"


@pytest.mark.parametrize("method", ["merge", "squash", "rebase"])
def test_merge_pr_valid_merge_method_forwarded_no_regression(
    monkeypatch: pytest.MonkeyPatch, method: str,
) -> None:
    """Regression guard — already passes today; must keep passing once the
    guard is added."""
    provider = _FakeMergeProvider()
    project = _pulls_project(pulls_merge=True)
    tools = _register_pull_tools(monkeypatch, provider, project)

    out = tools["merge_pr"](project_id="acme", pr_id="42", merge_method=method)

    assert "error" not in out, f"unexpected error: {out}"
    assert len(provider.calls) == 1
    assert provider.calls[0]["merge_method"] == method


def test_merge_pr_merge_method_default_is_merge_no_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard — already passes today; omitting merge_method
    defaults to 'merge'."""
    provider = _FakeMergeProvider()
    project = _pulls_project(pulls_merge=True)
    tools = _register_pull_tools(monkeypatch, provider, project)

    out = tools["merge_pr"](project_id="acme", pr_id="42")

    assert "error" not in out, f"unexpected error: {out}"
    assert provider.calls[0]["merge_method"] == "merge"


def test_merge_pr_invalid_merge_method_error_wins_over_permission_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for guard ordering. Today there is no merge_method
    guard at all, so `_require_pulls_merge` runs first inside `go()`: a
    project with `pulls_merge=False` raises `PermissionError('... does not
    permit merging pull requests: permissions.pulls.merge is false ...')`
    before merge_method is ever inspected, and the provider is never
    called. Expected RED: the returned error names 'pulls.merge' instead
    of 'merge_method', because the future pre-flight guard (which must run
    *before* `_require_pulls_merge`, mirroring `update_pr`/`submit_pr_
    review`) does not exist yet."""
    provider = _FakeMergeProvider()
    project = _pulls_project(pulls_merge=False)
    tools = _register_pull_tools(monkeypatch, provider, project)

    out = tools["merge_pr"](project_id="acme", pr_id="42", merge_method="bogus_method")

    assert "error" in out, f"expected error dict; got: {out}"
    assert "merge_method" in out["error"], (
        "expected the merge_method guard to win over the permission gate; "
        f"got: {out['error']!r}"
    )
    assert "pulls.merge" not in out["error"], (
        f"permission error leaked through instead: {out['error']!r}"
    )
    assert provider.calls == [], f"expected no provider call; got: {provider.calls}"


# ---------------------------------------------------------------------------
# Requirement 2 — GitLab label color validated locally (both call sites)
# ---------------------------------------------------------------------------


def test_create_label_gitlab_invalid_color_returns_friendly_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test. Today `create_label`'s color guard only checks
    `project.provider == "github"` — GitLab has no branch at all, so
    `color="notacolor"` is forwarded straight to the fake provider, which
    succeeds. Expected RED: `calls` is non-empty and/or `'error'` is
    absent."""
    provider = _FakeLabelProvider()
    project = _labels_project(provider="gitlab")
    tools = _register_label_tools(monkeypatch, provider, project)

    out = tools["create_label"](project_id="acme", name="bug", color="notacolor")

    assert "error" in out, f"expected error dict; got: {out}"
    assert "#ff0000" in out["error"], (
        f"expected the example hex-format hint; got: {out['error']!r}"
    )
    assert "notacolor" in out["error"], (
        f"expected the bad value echoed; got: {out['error']!r}"
    )
    assert provider.calls == [], f"expected no provider call; got: {provider.calls}"


def test_update_label_gitlab_invalid_color_returns_friendly_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test. Same missing-branch reason as the create_label driving
    test above, for the update_label call site. Expected RED."""
    provider = _FakeLabelProvider()
    project = _labels_project(provider="gitlab")
    tools = _register_label_tools(monkeypatch, provider, project)

    out = tools["update_label"](project_id="acme", name="bug", color="notacolor")

    assert "error" in out, f"expected error dict; got: {out}"
    assert "#ff0000" in out["error"], (
        f"expected the example hex-format hint; got: {out['error']!r}"
    )
    assert "notacolor" in out["error"], (
        f"expected the bad value echoed; got: {out['error']!r}"
    )
    assert provider.calls == [], f"expected no provider call; got: {provider.calls}"


_INVALID_GITLAB_COLORS = [
    "red",
    "",
    "#ff00",       # 4 hex digits
    "#ff00000",    # 7 hex digits
    "#gggggg",     # non-hex chars
    "  #ff0000  ", # whitespace padding — no implicit trim
    "#fff",        # 3-digit shorthand — GitLab's real API is 6-digit only;
    "fff",         # see the round-2 investigation note on _GITLAB_HEX_COLOR.
]


@pytest.mark.parametrize("color", _INVALID_GITLAB_COLORS)
def test_create_label_gitlab_rejects_invalid_colors(
    monkeypatch: pytest.MonkeyPatch, color: str,
) -> None:
    """Edge-case coverage for requirement 2's create_label call site.
    Expected RED today for the same missing-branch reason as the driving
    test above."""
    provider = _FakeLabelProvider()
    project = _labels_project(provider="gitlab")
    tools = _register_label_tools(monkeypatch, provider, project)

    out = tools["create_label"](project_id="acme", name="bug", color=color)

    assert "error" in out, f"expected rejection for color={color!r}; got: {out}"
    assert provider.calls == [], f"expected no provider call; got: {provider.calls}"


@pytest.mark.parametrize("color", _INVALID_GITLAB_COLORS)
def test_update_label_gitlab_rejects_invalid_colors(
    monkeypatch: pytest.MonkeyPatch, color: str,
) -> None:
    """Edge-case coverage for requirement 2's update_label call site.
    Expected RED today for the same missing-branch reason as the driving
    test above."""
    provider = _FakeLabelProvider()
    project = _labels_project(provider="gitlab")
    tools = _register_label_tools(monkeypatch, provider, project)

    out = tools["update_label"](project_id="acme", name="bug", color=color)

    assert "error" in out, f"expected rejection for color={color!r}; got: {out}"
    assert provider.calls == [], f"expected no provider call; got: {provider.calls}"


_VALID_GITLAB_COLORS = ["#ff0000", "ff0000", "#FF00AA"]


@pytest.mark.parametrize("color", _VALID_GITLAB_COLORS)
def test_create_label_gitlab_accepts_valid_colors_no_regression(
    monkeypatch: pytest.MonkeyPatch, color: str,
) -> None:
    """Regression guard — already passes today; must keep passing once the
    GitLab branch is added."""
    provider = _FakeLabelProvider()
    project = _labels_project(provider="gitlab")
    tools = _register_label_tools(monkeypatch, provider, project)

    out = tools["create_label"](project_id="acme", name="bug", color=color)

    assert "error" not in out, f"unexpected error for color={color!r}: {out}"
    assert len(provider.calls) == 1
    assert provider.calls[0]["color"] == color


@pytest.mark.parametrize("color", _VALID_GITLAB_COLORS)
def test_update_label_gitlab_accepts_valid_colors_no_regression(
    monkeypatch: pytest.MonkeyPatch, color: str,
) -> None:
    """Regression guard — already passes today; must keep passing once the
    GitLab branch is added."""
    provider = _FakeLabelProvider()
    project = _labels_project(provider="gitlab")
    tools = _register_label_tools(monkeypatch, provider, project)

    out = tools["update_label"](project_id="acme", name="bug", color=color)

    assert "error" not in out, f"unexpected error for color={color!r}: {out}"
    assert len(provider.calls) == 1
    assert provider.calls[0]["color"] == color


def test_create_label_gitlab_color_none_skips_validation_no_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard — already passes today; `color=None` must keep
    skipping validation and reaching the provider unchanged."""
    provider = _FakeLabelProvider()
    project = _labels_project(provider="gitlab")
    tools = _register_label_tools(monkeypatch, provider, project)

    out = tools["create_label"](project_id="acme", name="bug", color=None)

    assert "error" not in out, f"unexpected error: {out}"
    assert provider.calls[0]["color"] is None


def test_create_label_github_bare_hex_accepted_no_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-provider non-regression — already passes today; GitHub's
    existing validation must be unaffected by adding a GitLab branch."""
    provider = _FakeLabelProvider()
    project = _labels_project(provider="github")
    tools = _register_label_tools(monkeypatch, provider, project)

    out = tools["create_label"](project_id="acme", name="bug", color="ededed")

    assert "error" not in out, f"unexpected error: {out}"
    assert provider.calls[0]["color"] == "ededed"


def test_create_label_github_hashed_hex_rejected_with_github_message_no_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-provider non-regression — already passes today; GitHub still
    rejects a leading '#' with its own message (distinct from the GitLab
    hint)."""
    provider = _FakeLabelProvider()
    project = _labels_project(provider="github")
    tools = _register_label_tools(monkeypatch, provider, project)

    out = tools["create_label"](project_id="acme", name="bug", color="#ededed")

    assert "error" in out, f"expected rejection; got: {out}"
    assert "without '#'" in out["error"], (
        f"expected the GitHub-specific message; got: {out['error']!r}"
    )
    assert provider.calls == [], f"expected no provider call; got: {provider.calls}"


def test_create_label_azuredevops_color_not_validated_by_either_branch_no_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-provider non-regression — already passes today; Azure DevOps
    hits neither the GitHub nor the (new) GitLab color-validation branch,
    so any string is forwarded straight to the provider untouched. Uses a
    fake provider (not the real Azure provider, which rejects label
    creation outright as unsupported) purely to observe that no client-side
    ValueError fires for a malformed color on this provider path."""
    provider = _FakeLabelProvider()
    project = _labels_project(provider="azuredevops")
    tools = _register_label_tools(monkeypatch, provider, project)

    out = tools["create_label"](project_id="acme", name="bug", color="notacolor")

    assert "error" not in out, f"unexpected error: {out}"
    assert provider.calls[0]["color"] == "notacolor"


# ---------------------------------------------------------------------------
# Requirement 2c — schema description mentions GitLab local validation
# ---------------------------------------------------------------------------


def test_create_label_color_description_mentions_gitlab_local_validation():
    """Driving test. Today the `color` description says GitHub's format is
    'validated locally before the API call' but says nothing of the sort
    for GitLab's format — the phrase appears exactly once. Expected RED:
    count == 1, not 2."""
    tools = _register_schema(label_tools)
    desc = _param_description(tools["create_label"], "color")
    assert desc.count("validated locally") == 2, (
        "expected both the GitHub and GitLab sections to say 'validated "
        f"locally'; got: {desc!r}"
    )


def test_update_label_color_description_mentions_gitlab_local_validation():
    """Driving test. Same reason as the create_label description test
    above, for update_label's color description. Expected RED."""
    tools = _register_schema(label_tools)
    desc = _param_description(tools["update_label"], "color")
    assert desc.count("validated locally") == 2, (
        "expected both the GitHub and GitLab sections to say 'validated "
        f"locally'; got: {desc!r}"
    )
