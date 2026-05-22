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

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers import github as github_provider
from project_issues_plugin.tools import _providers as providers_mod
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
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=[project],
            state="ok",
            search_root="/tmp",
        )

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
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
    assert [pr["id"] for pr in result["prs"]] == ["1", "2"]
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
    assert [pr["id"] for pr in result["prs"]] == ["5"]


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


# ---------- inline review comments (ticket #43 D) ---------------------------


def _review_comment_payload(rcid: int, **overrides) -> dict:
    base = {
        "id": rcid,
        "user": {"login": "alice"},
        "body": "nit: rename this",
        "path": "src/foo.py",
        "line": 42,
        "original_line": 40,
        "side": "RIGHT",
        "commit_id": "deadbeef",
        "html_url": (
            f"https://github.com/acme/backend/pull/7#discussion_r{rcid}"
        ),
        "created_at": "2024-01-04T00:00:00Z",
        "updated_at": "2024-01-04T00:00:00Z",
    }
    base.update(overrides)
    return base


def test_get_pr_surfaces_review_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`get_pr` returns an extra `review_comments` array of inline notes."""
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7":
            return _json(_pr_payload(7))
        if req.method == "GET" and path == "/repos/acme/backend/issues/7/comments":
            return _json([])
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7/comments":
            return _json([
                _review_comment_payload(11),
                _review_comment_payload(12, in_reply_to_id=11, body="agreed"),
            ])
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["get_pr"](project_id="acme", pr_id="7")
    assert "error" not in result, result
    rcs = result["review_comments"]
    assert [c["id"] for c in rcs] == ["11", "12"]
    assert rcs[0]["in_reply_to"] is None
    assert rcs[1]["in_reply_to"] == "11"
    assert rcs[0]["path"] == "src/foo.py"
    assert rcs[0]["line"] == 42
    # Cross-provider discussion_id semantics: top-of-thread's anchor is
    # its own id; a reply's anchor is the parent (same on both providers,
    # different internal meaning).
    assert rcs[0]["discussion_id"] == "11"
    assert rcs[1]["discussion_id"] == "11"


def test_add_pr_review_comment_new_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New-thread mode POSTs path/line/commit_id with the body."""
    tools = _register_tools_with(monkeypatch, _project(pulls_modify=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if (
            req.method == "POST"
            and req.url.path == "/repos/acme/backend/pulls/7/comments"
        ):
            captured["body"] = json.loads(req.content)
            return _json(_review_comment_payload(99))
        raise AssertionError(f"unexpected: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["add_pr_review_comment"](
        project_id="acme",
        pr_id="7",
        body="rename this",
        path="src/foo.py",
        line=42,
        commit_sha="deadbeef",
    )
    assert "error" not in result, result
    posted = captured["body"]
    assert posted["path"] == "src/foo.py"
    assert posted["line"] == 42
    assert posted["commit_id"] == "deadbeef"
    assert posted["side"] == "RIGHT"
    assert posted["body"].startswith("#ai-generated\n\n")
    assert result["review_comment"]["id"] == "99"
    # Top-of-thread → discussion_id == own id (no `in_reply_to_id` set).
    assert result["review_comment"]["discussion_id"] == "99"


def test_add_pr_review_comment_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reply mode POSTs body+in_reply_to with no positional metadata."""
    tools = _register_tools_with(monkeypatch, _project(pulls_modify=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if (
            req.method == "POST"
            and req.url.path == "/repos/acme/backend/pulls/7/comments"
        ):
            captured["body"] = json.loads(req.content)
            return _json(_review_comment_payload(100, in_reply_to_id=99))
        raise AssertionError(f"unexpected: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["add_pr_review_comment"](
        project_id="acme",
        pr_id="7",
        body="agreed",
        in_reply_to="99",
    )
    assert "error" not in result, result
    posted = captured["body"]
    assert posted["in_reply_to"] == 99
    assert "path" not in posted and "line" not in posted
    assert result["review_comment"]["in_reply_to"] == "99"
    # Reply → discussion_id == parent anchor (= `in_reply_to_id`).
    assert result["review_comment"]["discussion_id"] == "99"


def test_add_pr_review_comment_rejects_mixed_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing both positional args and in_reply_to is rejected by the tool."""
    tools = _register_tools_with(monkeypatch, _project(pulls_modify=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP expected; got {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["add_pr_review_comment"](
        project_id="acme",
        pr_id="7",
        body="confused",
        path="src/foo.py",
        line=1,
        commit_sha="x",
        in_reply_to="99",
    )
    assert "error" in result
    assert "either" in result["error"].lower()


def test_add_pr_review_comment_rejects_incomplete_new_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new thread missing line is rejected."""
    tools = _register_tools_with(monkeypatch, _project(pulls_modify=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP expected; got {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["add_pr_review_comment"](
        project_id="acme", pr_id="7", body="x",
        path="src/foo.py", commit_sha="x",
    )
    assert "error" in result
    assert "line" in result["error"]


# ---------- submit_pr_review (ticket #43 C) ---------------------------------


def _review_payload(rid: int, state: str = "APPROVED", body: str = "") -> dict:
    return {
        "id": rid,
        "user": {"login": "alice"},
        "state": state,
        "body": body,
        "html_url": f"https://github.com/acme/backend/pull/7#pullrequestreview-{rid}",
        "submitted_at": "2024-01-03T00:00:00Z",
        "commit_id": "deadbeef",
    }


def test_submit_pr_review_approve(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _register_tools_with(monkeypatch, _project(pulls_modify=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if (
            req.method == "POST"
            and req.url.path == "/repos/acme/backend/pulls/7/reviews"
        ):
            captured["body"] = json.loads(req.content)
            return _json(_review_payload(101, state="APPROVED", body="lgtm!"))
        raise AssertionError(f"unexpected: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["submit_pr_review"](
        project_id="acme", pr_id="7", state="approve", body="lgtm!",
    )
    assert "error" not in result, result
    assert captured["body"]["event"] == "APPROVE"
    assert captured["body"]["body"].startswith("#ai-generated\n\n")
    assert result["review"]["state"] == "approve"


def test_submit_pr_review_request_changes_requires_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools_with(monkeypatch, _project(pulls_modify=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP expected; got {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["submit_pr_review"](
        project_id="acme", pr_id="7", state="request_changes",
    )
    assert "error" in result
    assert "body" in result["error"].lower()


def test_submit_pr_review_passes_commit_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools_with(monkeypatch, _project(pulls_modify=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if (
            req.method == "POST"
            and req.url.path == "/repos/acme/backend/pulls/7/reviews"
        ):
            captured["body"] = json.loads(req.content)
            return _json(
                _review_payload(102, state="COMMENTED", body="comment")
            )
        raise AssertionError(f"unexpected: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["submit_pr_review"](
        project_id="acme", pr_id="7", state="comment",
        body="comment", commit_sha="abc123",
    )
    assert "error" not in result, result
    assert captured["body"]["commit_id"] == "abc123"
    assert captured["body"]["event"] == "COMMENT"


# ---------- reviewers on write surface (ticket #43 B) -----------------------


def test_create_pr_requests_reviewers(monkeypatch: pytest.MonkeyPatch) -> None:
    """`create_pr(requested_reviewers=[...])` POSTs to /requested_reviewers."""
    tools = _register_tools_with(monkeypatch, _project(pulls_create=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path == "/repos/acme/backend/labels":
            return _json({"name": "ai-generated"}, status_code=201)
        if req.method == "POST" and path == "/repos/acme/backend/pulls":
            return _json(_pr_payload(11, title="t"))
        if (
            req.method == "POST"
            and path == "/repos/acme/backend/issues/11/labels"
        ):
            body = json.loads(req.content)
            return _json([{"name": n} for n in body["labels"]])
        if (
            req.method == "POST"
            and path == "/repos/acme/backend/pulls/11/requested_reviewers"
        ):
            captured["reviewers"] = json.loads(req.content)["reviewers"]
            return _json(
                _pr_payload(
                    11,
                    requested_reviewers=[{"login": n} for n in captured["reviewers"]],
                )
            )
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["create_pr"](
        project_id="acme",
        title="t", body="b", head="feat/x", base="main",
        requested_reviewers=["bob", "carol"],
    )
    assert "error" not in result, result
    assert captured["reviewers"] == ["bob", "carol"]
    assert result["pull_request"]["requested_reviewers"] == ["bob", "carol"]


def test_update_pr_reviewers_add_and_remove(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed add/remove issues both POST and DELETE on /requested_reviewers."""
    tools = _register_tools_with(monkeypatch, _project(pulls_modify=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    posts: list[list[str]] = []
    deletes: list[list[str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7":
            return _json(
                _pr_payload(
                    7,
                    requested_reviewers=[{"login": "alice"}, {"login": "bob"}],
                )
            )
        if (
            req.method == "POST"
            and path == "/repos/acme/backend/pulls/7/requested_reviewers"
        ):
            posts.append(json.loads(req.content)["reviewers"])
            return _json(_pr_payload(7))
        if (
            req.method == "DELETE"
            and path == "/repos/acme/backend/pulls/7/requested_reviewers"
        ):
            deletes.append(json.loads(req.content)["reviewers"])
            return _json(_pr_payload(7))
        if (
            req.method == "PUT"
            and path == "/repos/acme/backend/issues/7/labels"
        ):
            return _json([{"name": "ai-modified"}])
        if req.method == "POST" and path == "/repos/acme/backend/labels":
            return _json({"name": "ai-modified"}, status_code=201)
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["update_pr"](
        project_id="acme",
        pr_id="7",
        reviewers_add=["carol"],
        reviewers_remove=["alice"],
    )
    assert "error" not in result, result
    assert posts == [["carol"]]
    assert deletes == [["alice"]]


# ---------- draft toggle on update_pr (ticket #43 A) ------------------------


def test_update_pr_draft_true_calls_graphql_convert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flipping a ready PR to draft issues `convertPullRequestToDraft`."""
    tools = _register_tools_with(monkeypatch, _project(pulls_modify=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    graphql_payload: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7":
            return _json(
                _pr_payload(7, draft=False, node_id="PR_kwDO_node")
            )
        if req.method == "POST" and path == "/graphql":
            graphql_payload["body"] = json.loads(req.content)
            return _json({"data": {"convertPullRequestToDraft": {
                "pullRequest": {"id": "PR_kwDO_node", "isDraft": True}
            }}})
        if (
            req.method == "PUT"
            and path == "/repos/acme/backend/issues/7/labels"
        ):
            body = json.loads(req.content)
            return _json([{"name": n} for n in body["labels"]])
        if req.method == "POST" and path == "/repos/acme/backend/labels":
            return _json({"name": "ai-modified"}, status_code=201)
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["update_pr"](project_id="acme", pr_id="7", draft=True)
    assert "error" not in result, result
    query = graphql_payload["body"]["query"]
    assert "convertPullRequestToDraft" in query
    assert graphql_payload["body"]["variables"]["id"] == "PR_kwDO_node"


def test_update_pr_draft_false_calls_graphql_mark_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flipping a draft PR to ready issues `markPullRequestReadyForReview`."""
    tools = _register_tools_with(monkeypatch, _project(pulls_modify=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    graphql_payload: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7":
            return _json(
                _pr_payload(7, draft=True, node_id="PR_kwDO_x")
            )
        if req.method == "POST" and path == "/graphql":
            graphql_payload["body"] = json.loads(req.content)
            return _json({"data": {"markPullRequestReadyForReview": {
                "pullRequest": {"id": "PR_kwDO_x", "isDraft": False}
            }}})
        if (
            req.method == "PUT"
            and path == "/repos/acme/backend/issues/7/labels"
        ):
            return _json([{"name": "ai-modified"}])
        if req.method == "POST" and path == "/repos/acme/backend/labels":
            return _json({"name": "ai-modified"}, status_code=201)
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["update_pr"](project_id="acme", pr_id="7", draft=False)
    assert "error" not in result, result
    assert "markPullRequestReadyForReview" in graphql_payload["body"]["query"]


def test_update_pr_draft_noop_when_state_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No GraphQL call is issued when draft value already matches current."""
    tools = _register_tools_with(monkeypatch, _project(pulls_modify=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(f"{req.method} {req.url.path}")
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/pulls/7":
            return _json(_pr_payload(7, draft=True, node_id="x"))
        if (
            req.method == "PUT"
            and path == "/repos/acme/backend/issues/7/labels"
        ):
            return _json([{"name": "ai-modified"}])
        if req.method == "POST" and path == "/repos/acme/backend/labels":
            return _json({"name": "ai-modified"}, status_code=201)
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["update_pr"](project_id="acme", pr_id="7", draft=True)
    assert "error" not in result, result
    assert "POST /graphql" not in seen


# ---------- response-shape inventory (ticket #43 G) -------------------------


def test_get_pr_surfaces_github_specific_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`get_pr` propagates mergeable_state / merge_commit_sha / auto_merge.

    Ticket #43 (item G) extended the `PullRequest` dataclass with
    GitHub-specific qualitative state. GitLab-only fields stay `None` on
    a GitHub payload.
    """
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/repos/acme/backend/pulls/7":
            return _json(
                _pr_payload(
                    7,
                    mergeable_state="clean",
                    merge_commit_sha="abc123def",
                    auto_merge={"enabled_by": {"login": "alice"}},
                )
            )
        if (
            req.method == "GET"
            and req.url.path == "/repos/acme/backend/issues/7/comments"
        ):
            return _json([])
        if (
            req.method == "GET"
            and req.url.path == "/repos/acme/backend/pulls/7/comments"
        ):
            return _json([])
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["get_pr"](project_id="acme", pr_id="7")
    assert "error" not in result, result
    pr = result["pull_request"]
    assert pr["mergeable_state"] == "clean"
    assert pr["merge_commit_sha"] == "abc123def"
    assert pr["auto_merge"] == {"enabled_by": {"login": "alice"}}
    # GitLab-only fields stay None on a GitHub payload.
    assert pr["detailed_merge_status"] is None
    assert pr["pipeline_status"] is None
    assert pr["approvals_required"] is None
    assert pr["approvals_received"] is None
    # review_decision is sourced from GraphQL — REST mapping leaves None.
    assert pr["review_decision"] is None
