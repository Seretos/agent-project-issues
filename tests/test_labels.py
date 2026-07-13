"""Tests for the label-management MCP tools (ticket #79).

Follows the test_pipelines.py pattern: a ``_StubMCP``, a
``_register_tools_with`` helper that monkey-patches
``_providers.load_projects``, and ``httpx.MockTransport``-backed handlers.

Coverage:
  - Happy paths for all four tools on GitHub.
  - Permission gates: create/update/delete with ``issues.modify=False``
    return ``{"error": ...}``; no HTTP call is made.
  - ``list_labels`` works with no token (public repo).
  - Error translation: create already-exists (GitHub 422), update with no
    fields (ValueError), delete not-found (GitHub 404), unknown project —
    all return ``{"error": ...}``; none raise.
  - Azure DevOps capability gap: create/update/delete return
    ``{"error": ...}`` containing "not supported"; list_labels returns a list.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers import azuredevops as ado_provider
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import labels as label_tools


# ---------- helpers ----------------------------------------------------------


def _github_project(*, modify: bool = True) -> ProjectConfig:
    from lib_python_projects import IssuesPermissions, Permissions, PullsPermissions
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
        permissions=Permissions(
            issues=IssuesPermissions(create=True, modify=modify),
            pulls=PullsPermissions(create=True, modify=True, merge=True),
        ),
    )


def _ado_project() -> ProjectConfig:
    from lib_python_projects import IssuesPermissions, Permissions, PullsPermissions
    # AzureDevOps path must be 'organization/project/repository'.
    return ProjectConfig(
        id="acme-ado",
        provider="azuredevops",
        path="MyOrg/MyProject/MyRepo",
        token_env="ADO_TOKEN",
        permissions=Permissions(
            issues=IssuesPermissions(create=True, modify=True),
            pulls=PullsPermissions(create=True, modify=True, merge=True),
        ),
    )


def _json_resp(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def _install_github_mock(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    transport = httpx.MockTransport(wrapped)

    def fake_client(token: str | None) -> httpx.Client:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "test"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.Client(
            base_url=github_provider.API_BASE,
            headers=headers,
            transport=transport,
        )

    monkeypatch.setattr(github_provider, "_client", fake_client)
    return seen


def _install_ado_mock(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    transport = httpx.MockTransport(wrapped)

    def fake_client(project: ProjectConfig, token: str | None) -> httpx.Client:
        return httpx.Client(
            base_url="https://dev.azure.com",
            headers={"Accept": "application/json"},
            transport=transport,
        )

    monkeypatch.setattr(ado_provider, "_client", fake_client)
    return seen


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _register_tools_with(monkeypatch: pytest.MonkeyPatch, project: ProjectConfig):
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=[project],
            state="ok",
            search_root="/tmp",
        )

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    # Provide a dummy token via env so _require_token doesn't block write tests.
    monkeypatch.setenv(project.token_env, "test-token")

    stub = _StubMCP()
    label_tools.register(stub)
    return stub.tools


# ---------- list_labels: GitHub happy path -----------------------------------


def test_list_labels_github_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_labels returns project_id + labels list with two entries."""
    project = _github_project()
    tools = _register_tools_with(monkeypatch, project)

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/acme/backend/labels"
        return _json_resp([
            {"name": "bug", "color": "d73a4a", "description": "Something isn't working"},
            {"name": "enhancement", "color": "a2eeef", "description": "New feature"},
        ])

    _install_github_mock(monkeypatch, handler)

    result = tools["list_labels"](project_id="acme")
    assert "error" not in result, result
    assert result["project_id"] == "acme"
    labels = result["labels"]
    assert len(labels) == 2
    assert labels[0] == {"name": "bug", "color": "d73a4a", "description": "Something isn't working"}
    assert labels[1]["name"] == "enhancement"


# ---------- list_labels: no token (public repo) ------------------------------


def test_list_labels_no_token_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_labels is token-optional — public repos can be queried without a token."""
    project = _github_project()
    tools = _register_tools_with(monkeypatch, project)
    # Remove the token from the environment to simulate a public repo.
    monkeypatch.delenv(project.token_env, raising=False)

    def handler(req: httpx.Request) -> httpx.Response:
        # No Authorization header expected.
        assert "Authorization" not in req.headers or req.headers["Authorization"] == ""
        return _json_resp([{"name": "good first issue", "color": "7057ff", "description": ""}])

    _install_github_mock(monkeypatch, handler)

    result = tools["list_labels"](project_id="acme")
    assert "error" not in result, result
    assert result["labels"][0]["name"] == "good first issue"


# ---------- create_label: GitHub happy path ----------------------------------


def test_create_label_github_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_label posts to /labels and returns project_id + label envelope."""
    project = _github_project()
    tools = _register_tools_with(monkeypatch, project)
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "POST"
        assert req.url.path == "/repos/acme/backend/labels"
        captured["body"] = json.loads(req.content)
        return _json_resp({"name": "triage", "color": "e4e669", "description": "Needs triage"})

    _install_github_mock(monkeypatch, handler)

    result = tools["create_label"](
        project_id="acme", name="triage", color="e4e669", description="Needs triage"
    )
    assert "error" not in result, result
    assert result["project_id"] == "acme"
    assert result["label"]["name"] == "triage"
    assert result["label"]["color"] == "e4e669"
    assert result["label"]["description"] == "Needs triage"
    assert captured["body"]["name"] == "triage"
    assert captured["body"]["color"] == "e4e669"


# ---------- update_label: GitHub happy path ----------------------------------


def test_update_label_github_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """update_label patches /labels/{name} and returns the updated label."""
    project = _github_project()
    tools = _register_tools_with(monkeypatch, project)
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "PATCH"
        assert req.url.path == "/repos/acme/backend/labels/bug"
        captured["body"] = json.loads(req.content)
        return _json_resp({"name": "bug-fixed", "color": "00ff00", "description": "A bug"})

    _install_github_mock(monkeypatch, handler)

    result = tools["update_label"](
        project_id="acme", name="bug", new_name="bug-fixed", color="00ff00"
    )
    assert "error" not in result, result
    assert result["project_id"] == "acme"
    assert result["label"]["name"] == "bug-fixed"
    assert captured["body"]["new_name"] == "bug-fixed"
    assert captured["body"]["color"] == "00ff00"


# ---------- delete_label: GitHub happy path ----------------------------------


def test_delete_label_github_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """delete_label deletes /labels/{name} and returns deleted=True."""
    project = _github_project()
    tools = _register_tools_with(monkeypatch, project)

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "DELETE"
        assert req.url.path == "/repos/acme/backend/labels/wontfix"
        return httpx.Response(status_code=204, content=b"")

    _install_github_mock(monkeypatch, handler)

    result = tools["delete_label"](project_id="acme", name="wontfix")
    assert "error" not in result, result
    assert result["project_id"] == "acme"
    assert result["deleted"] is True
    assert result["name"] == "wontfix"


# ---------- permission gates -------------------------------------------------


def test_create_label_no_modify_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_label with issues.modify=False returns error, no HTTP call."""
    project = _github_project(modify=False)
    tools = _register_tools_with(monkeypatch, project)

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected; got {req.url}")

    _install_github_mock(monkeypatch, handler)

    result = tools["create_label"](project_id="acme", name="bug")
    assert "error" in result
    assert "modify" in result["error"].lower() or "permission" in result["error"].lower()


def test_update_label_no_modify_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    """update_label with issues.modify=False returns error, no HTTP call."""
    project = _github_project(modify=False)
    tools = _register_tools_with(monkeypatch, project)

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected; got {req.url}")

    _install_github_mock(monkeypatch, handler)

    result = tools["update_label"](project_id="acme", name="bug", new_name="defect")
    assert "error" in result
    assert "modify" in result["error"].lower() or "permission" in result["error"].lower()


def test_delete_label_no_modify_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    """delete_label with issues.modify=False returns error, no HTTP call."""
    project = _github_project(modify=False)
    tools = _register_tools_with(monkeypatch, project)

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected; got {req.url}")

    _install_github_mock(monkeypatch, handler)

    result = tools["delete_label"](project_id="acme", name="wontfix")
    assert "error" in result
    assert "modify" in result["error"].lower() or "permission" in result["error"].lower()


# ---------- error translation ------------------------------------------------


def test_create_label_already_exists_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub 422 already_exists -> structured error dict, not exception."""
    project = _github_project()
    tools = _register_tools_with(monkeypatch, project)

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_resp(
            {"errors": [{"code": "already_exists"}], "message": "Validation Failed"},
            status_code=422,
        )

    _install_github_mock(monkeypatch, handler)

    result = tools["create_label"](project_id="acme", name="bug")
    assert "error" in result
    assert "already exists" in result["error"]


def test_update_label_no_fields_supplied_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: update_label with no fields raises ValueError -> {"error": ...}."""
    project = _github_project()
    tools = _register_tools_with(monkeypatch, project)

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected for no-field update; got {req.url}")

    _install_github_mock(monkeypatch, handler)

    result = tools["update_label"](project_id="acme", name="bug")
    assert "error" in result
    # The ValueError message mentions the missing fields.
    assert "new_name" in result["error"] or "at least one" in result["error"]


def test_delete_label_not_found_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub 404 on delete -> structured error dict, not exception."""
    project = _github_project()
    tools = _register_tools_with(monkeypatch, project)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=404, content=b'{"message":"Not Found"}',
                              headers={"Content-Type": "application/json"})

    _install_github_mock(monkeypatch, handler)

    result = tools["delete_label"](project_id="acme", name="nonexistent")
    assert "error" in result
    assert "not found" in result["error"].lower()


def test_unknown_project_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requesting a project not in the config returns error, does not raise."""
    project = _github_project()
    tools = _register_tools_with(monkeypatch, project)

    result = tools["list_labels"](project_id="does-not-exist")
    assert "error" in result
    assert "does-not-exist" in result["error"]


def test_list_labels_provider_http_error_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider HTTP 500 surfaces as {"error": ...}, never raises."""
    project = _github_project()
    tools = _register_tools_with(monkeypatch, project)

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_resp({"message": "Internal Server Error"}, status_code=500)

    _install_github_mock(monkeypatch, handler)

    result = tools["list_labels"](project_id="acme")
    assert "error" in result


# ---------- Azure DevOps: capability gap -------------------------------------


def test_ado_create_label_unsupported_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_label on Azure DevOps raises LabelOperationUnsupported -> error dict."""
    project = _ado_project()
    tools = _register_tools_with(monkeypatch, project)

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected; got {req.url}")

    _install_ado_mock(monkeypatch, handler)

    result = tools["create_label"](project_id="acme-ado", name="mytag")
    assert "error" in result
    assert "not supported" in result["error"] or "unsupported" in result["error"].lower()


def test_ado_update_label_unsupported_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """update_label on Azure DevOps raises LabelOperationUnsupported -> error dict."""
    project = _ado_project()
    tools = _register_tools_with(monkeypatch, project)

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected; got {req.url}")

    _install_ado_mock(monkeypatch, handler)

    result = tools["update_label"](project_id="acme-ado", name="mytag", new_name="newtag")
    assert "error" in result
    assert "not supported" in result["error"] or "unsupported" in result["error"].lower()


def test_update_label_accepts_name_param(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: update_label must accept `name` (not `current_name`) without error."""
    project = _github_project()
    tools = _register_tools_with(monkeypatch, project)
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "PATCH"
        assert req.url.path == "/repos/acme/backend/labels/bug"
        captured["body"] = json.loads(req.content)
        return _json_resp({"name": "bug-fixed", "color": "00ff00", "description": ""})

    _install_github_mock(monkeypatch, handler)

    result = tools["update_label"](
        project_id="acme", name="bug", new_name="bug-fixed", color="00ff00"
    )
    assert "error" not in result, result
    assert result["project_id"] == "acme"
    assert result["label"]["name"] == "bug-fixed"
    assert captured["body"]["new_name"] == "bug-fixed"
    assert captured["body"]["color"] == "00ff00"


def test_update_label_missing_name_returns_soft_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """update_label with no `name` argument returns a soft error dict, not a ValidationError."""
    project = _github_project()
    tools = _register_tools_with(monkeypatch, project)

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected; got {req.url}")

    _install_github_mock(monkeypatch, handler)

    result = tools["update_label"](project_id="acme")
    assert "error" in result, f"expected error key, got: {result}"
    assert "name" in result["error"].lower()


def test_ado_delete_label_unsupported_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """delete_label on Azure DevOps raises LabelOperationUnsupported -> error dict."""
    project = _ado_project()
    tools = _register_tools_with(monkeypatch, project)

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected; got {req.url}")

    _install_ado_mock(monkeypatch, handler)

    result = tools["delete_label"](project_id="acme-ado", name="mytag")
    assert "error" in result
    assert "not supported" in result["error"] or "unsupported" in result["error"].lower()


def test_ado_list_labels_returns_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_labels on Azure DevOps returns a label list (best-effort tags).

    lib-python-projects v0.3.3 (#172) rescoped list_labels away from the
    org/project tag catalog (`_apis/wit/tags`, which also lists catalog-only
    tags not applied to any work item) to the union of `System.Tags`
    actually present on the project's work items: a WIQL query for every
    work-item id, then a batched `workitemsbatch` fetch of `System.Tags`.
    """
    project = _ado_project()
    tools = _register_tools_with(monkeypatch, project)
    # Remove token env so no token is required for list (token-optional).
    monkeypatch.delenv(project.token_env, raising=False)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/_apis/wit/wiql"):
            return _json_resp({
                "workItems": [{"id": 101}, {"id": 102}],
            })
        if req.method == "POST" and req.url.path.endswith(
            "/_apis/wit/workitemsbatch"
        ):
            return _json_resp({
                "value": [
                    {"id": 101, "fields": {"System.Tags": "sprint-1"}},
                    {"id": 102, "fields": {"System.Tags": "sprint-2"}},
                ],
            })
        raise AssertionError(f"unexpected request: {req.url}")

    _install_ado_mock(monkeypatch, handler)

    result = tools["list_labels"](project_id="acme-ado")
    assert "error" not in result, result
    assert result["project_id"] == "acme-ado"
    labels = result["labels"]
    assert len(labels) == 2
    assert labels[0]["name"] == "sprint-1"
    # ADO tags always have empty color and description.
    assert labels[0]["color"] == ""
    assert labels[0]["description"] == ""
