"""Tests for the pull-request tools (list/get/create/update/merge + add_pr_comment).

Uses `httpx.MockTransport` to intercept HTTP calls; the provider is
monkey-patched so `_client(token)` returns a client backed by the mock
transport. Tool-level permission gates and the `_safe` wrapper are
exercised via the `tools/pulls.py` register entrypoint.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from project_issues_plugin.config import ProjectConfig
from project_issues_plugin.providers import github as github_provider
from project_issues_plugin.tools import pulls as pull_tools


# ---------- helpers ----------------------------------------------------------


def _project(
    *,
    pulls_create: bool = False,
    pulls_modify: bool = False,
    pulls_merge: bool = False,
) -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
        permissions={
            "issues": {"create": True, "modify": True},
            "pulls": {
                "create": pulls_create,
                "modify": pulls_modify,
                "merge": pulls_merge,
            },
        },
    )


def _pr_payload(number: int, **overrides) -> dict:
    base = {
        "number": number,
        "title": f"PR {number}",
        "body": "body",
        "state": "open",
        "draft": False,
        "merged": False,
        "merged_at": None,
        "mergeable": True,
        "user": {"login": "alice"},
        "assignees": [],
        "requested_reviewers": [],
        "labels": [],
        "head": {
            "ref": "feature/x",
            "sha": "deadbeef",
            "repo": {"full_name": "acme/backend"},
        },
        "base": {"ref": "main", "sha": "cafebabe"},
        "html_url": f"https://github.com/acme/backend/pull/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
    }
    base.update(overrides)
    return base


def _comment_payload(comment_id: int, body: str = "hello") -> dict:
    return {
        "id": comment_id,
        "user": {"login": "alice"},
        "body": body,
        "html_url": f"https://github.com/acme/backend/issues/1#issuecomment-{comment_id}",
        "created_at": "2024-01-01T00:00:00Z",
    }


def _json(payload, status_code: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def _install_mock(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)

    def fake_client(token: str | None) -> httpx.Client:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "test-agent",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.Client(
            base_url=github_provider.API_BASE,
            headers=headers,
            transport=transport,
        )

    monkeypatch.setattr(github_provider, "_client", fake_client)
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
    from project_issues_plugin import config as cfg_mod

    def fake_load_projects(cwd=None):
        return cfg_mod.LoadResult(
            projects=[project],
            state="ok",
            search_root="/tmp",
        )

    monkeypatch.setattr(cfg_mod, "load_projects", fake_load_projects)
    monkeypatch.setattr(pull_tools, "load_projects", fake_load_projects)

    stub = _StubMCP()
    pull_tools.register(stub)
    return stub.tools


# ---------- list_prs ---------------------------------------------------------


def test_list_prs_default_uses_pulls_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """No filter beyond `status` -> hits the cheap `/pulls` endpoint."""
    tools = _register_tools_with(monkeypatch, _project())
    seen_paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_paths.append(req.url.path)
        if req.url.path == "/repos/acme/backend/pulls":
            return _json([_pr_payload(1), _pr_payload(2)])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_prs"](project_id="acme")
    assert "error" not in result, result
    assert [pr["id"] for pr in result["pull_requests"]] == ["1", "2"]
    assert seen_paths == ["/repos/acme/backend/pulls"]


def test_list_prs_with_labels_switches_to_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`labels` set -> switch to `/search/issues` with `is:pr` qualifier."""
    tools = _register_tools_with(monkeypatch, _project())
    captured_q: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/search/issues":
            captured_q["q"] = req.url.params.get("q", "")
            return _json({"items": [_pr_payload(5, title="match")]})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_prs"](project_id="acme", labels=["needs-review"])
    assert "error" not in result, result
    q = captured_q["q"]
    assert "is:pr" in q
    assert "repo:acme/backend" in q
    assert "label:needs-review" in q
    assert [pr["id"] for pr in result["pull_requests"]] == ["5"]


# ---------- create_pr -------------------------------------------------------


def test_create_pr_denied_when_pulls_create_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools_with(monkeypatch, _project(pulls_create=False))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected; got {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["create_pr"](
        project_id="acme", title="t", body="b", head="feat/x", base="main",
    )
    assert "error" in result
    assert "pulls.create" in result["error"]


def test_create_pr_succeeds_when_pulls_create_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permitted create_pr applies the `ai-generated` marker label."""
    tools = _register_tools_with(monkeypatch, _project(pulls_create=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    captured_labels: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path == "/repos/acme/backend/labels":
            return _json({"name": "ai-generated"}, status_code=201)
        if req.method == "POST" and path == "/repos/acme/backend/pulls":
            return _json(_pr_payload(42, title="t"))
        if (
            req.method == "POST"
            and path == "/repos/acme/backend/issues/42/labels"
        ):
            body = json.loads(req.content)
            captured_labels["labels"] = body["labels"]
            return _json([{"name": n} for n in body["labels"]])
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["create_pr"](
        project_id="acme",
        title="t",
        body="b",
        head="feat/x",
        base="main",
    )
    assert "error" not in result, result
    assert result["pull_request"]["id"] == "42"
    assert "ai-generated" in (captured_labels.get("labels") or [])
    assert "ai-generated" in result["pull_request"]["labels"]


# ---------- update_pr -------------------------------------------------------


def test_update_pr_status_closed_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Closing a PR with `pulls.modify=True` issues a PATCH /pulls/{n}."""
    tools = _register_tools_with(
        monkeypatch, _project(pulls_modify=True)
    )
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    captured_patch: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7":
            return _json(_pr_payload(7))
        if req.method == "PATCH" and path == "/repos/acme/backend/pulls/7":
            captured_patch["body"] = json.loads(req.content)
            return _json(_pr_payload(7, state="closed"))
        # update_pr may also issue a labels PUT when `ai-modified` is added.
        if (
            req.method == "PUT"
            and path == "/repos/acme/backend/issues/7/labels"
        ):
            body = json.loads(req.content)
            return _json([{"name": n} for n in body["labels"]])
        if (
            req.method == "POST"
            and path == "/repos/acme/backend/labels"
        ):
            return _json({"name": "ai-modified"}, status_code=201)
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["update_pr"](
        project_id="acme", pr_id="7", status="closed",
    )
    assert "error" not in result, result
    assert captured_patch["body"] == {"state": "closed"}
    assert result["pull_request"]["status"] == "closed"


def test_update_pr_rejects_merged_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """`status="merged"` is rejected at the tool layer with a hint to merge_pr."""
    tools = _register_tools_with(monkeypatch, _project(pulls_modify=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected; got {req.url}")

    _install_mock(monkeypatch, handler)
    # We bypass the MCP Literal validation by calling with a stringly-typed
    # status value — the tool's defence-in-depth guard catches it.
    result = tools["update_pr"](project_id="acme", pr_id="7", status="merged")
    assert "error" in result
    assert "merge_pr" in result["error"]


# ---------- merge_pr -------------------------------------------------------


def test_merge_pr_denied_when_pulls_merge_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools_with(
        monkeypatch, _project(pulls_modify=True, pulls_merge=False),
    )
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected; got {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["merge_pr"](project_id="acme", pr_id="7", merge_method="squash")
    assert "error" in result
    assert "pulls.merge" in result["error"]


def test_merge_pr_succeeds_when_pulls_merge_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools_with(monkeypatch, _project(pulls_merge=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    captured_merge: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "PUT" and path == "/repos/acme/backend/pulls/7/merge":
            captured_merge["body"] = json.loads(req.content)
            return _json({"merged": True, "message": "Pull Request successfully merged"})
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7":
            return _json(_pr_payload(7, state="closed", merged=True, merged_at="2024-02-01T00:00:00Z"))
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["merge_pr"](
        project_id="acme", pr_id="7", merge_method="squash",
    )
    assert "error" not in result, result
    assert captured_merge["body"]["merge_method"] == "squash"
    assert result["pull_request"]["status"] == "merged"
    assert result["pull_request"]["merged"] is True


# ---------- add_pr_comment --------------------------------------------------


def test_add_pr_comment_denied_when_pulls_modify_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools_with(monkeypatch, _project(pulls_modify=False))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected; got {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["add_pr_comment"](
        project_id="acme", pr_id="7", body="LGTM",
    )
    assert "error" in result
    assert "pulls.modify" in result["error"]


def test_add_pr_comment_succeeds_with_modify(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _register_tools_with(monkeypatch, _project(pulls_modify=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if (
            req.method == "POST"
            and req.url.path == "/repos/acme/backend/issues/7/comments"
        ):
            captured["body"] = json.loads(req.content)
            return _json(_comment_payload(999, body=captured["body"]["body"]))
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["add_pr_comment"](
        project_id="acme", pr_id="7", body="LGTM",
    )
    assert "error" not in result, result
    assert captured["body"]["body"] == "#ai-generated\n\nLGTM"
    assert result["comment"]["body"].startswith("#ai-generated\n\n")
