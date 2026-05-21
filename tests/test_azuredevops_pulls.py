"""Tests for the Azure DevOps provider's pull-request surface.

Covers:
- `list_prs` status / branch filter translation + repo-id resolution cache
- `get_pr` + top-level thread comments (with thread-without-context shape)
- `create_pr` body marker + draft toggle + reviewers
- `update_pr` status/title/body/reviewers
- `merge_pr` merge-method mapping
- `add_pr_comment` thread-without-context creation
- `add_pr_review_comment` diff-anchored thread + reply path
- `submit_pr_review` reviewer-vote mapping
- `list_pr_review_comments` thread-with-context filtering
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from project_issues_plugin.config import ProjectConfig
from project_issues_plugin.providers import azuredevops as azure_mod
from project_issues_plugin.providers.azuredevops import (
    AzureDevOpsProvider,
    _basic_auth_header,
    _cache_clear_all,
)
from project_issues_plugin.providers.base import PRFilters


REPO_ID = "da0d7da0-6a8c-4958-aad3-be17cbf806eb"


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="azure-tests",
        provider="azuredevops",
        path="seredos/azure-tests/azure-tests",
        token_env="AZURE_TOKEN",
    )


def _json(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def _install_mock(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)

    def fake_client(project: ProjectConfig, token: str | None) -> httpx.Client:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = _basic_auth_header(token)
        base = (project.base_url or "https://dev.azure.com").rstrip("/")
        return httpx.Client(base_url=base, headers=headers, transport=transport)

    monkeypatch.setattr(azure_mod, "_client", fake_client)
    return seen


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    _cache_clear_all()


def _pr_payload(pr_id: int, **overrides) -> dict:
    base = {
        "pullRequestId": pr_id,
        "title": f"PR {pr_id}",
        "description": "<p>impl</p>",
        "status": "active",
        "isDraft": False,
        "createdBy": {"displayName": "Alice"},
        "reviewers": [],
        "labels": [],
        "sourceRefName": "refs/heads/feat/x",
        "targetRefName": "refs/heads/main",
        "lastMergeSourceCommit": {"commitId": "abc"},
        "lastMergeTargetCommit": {"commitId": "def"},
        "creationDate": "2026-05-18T10:00:00Z",
        "repository": {"name": "azure-tests"},
    }
    base.update(overrides)
    return base


def _repos_response() -> httpx.Response:
    return _json({
        "value": [
            {
                "id": REPO_ID,
                "name": "azure-tests",
                "defaultBranch": "refs/heads/main",
            },
            {
                "id": "00000000-0000-0000-0000-000000000002",
                "name": "azure-tests2",
                "defaultBranch": "refs/heads/main",
            },
        ]
    })


def _repos_handler(req: httpx.Request) -> httpx.Response | None:
    """Shared handler shard for repository listing — the call PRs depend on."""
    if req.url.path.endswith("/_apis/git/repositories"):
        return _repos_response()
    return None


# ---------- list_prs ---------------------------------------------------------


def test_list_prs_open_translates_status(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        if "/pullrequests" in req.url.path:
            captured.append(req.url.params.get("searchCriteria.status", ""))
            return _json({"value": [_pr_payload(1), _pr_payload(2)]})
        raise AssertionError(f"unexpected {req.url.path}")

    _install_mock(monkeypatch, handler)
    prs = AzureDevOpsProvider().list_prs(
        _project(), token="t", filters=PRFilters(status="open", limit=30)
    )
    assert [p.id for p in prs] == ["1", "2"]
    assert captured == ["active"]


def test_list_prs_translates_head_and_base_to_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        if "/pullrequests" in req.url.path:
            captured["source"] = req.url.params.get("searchCriteria.sourceRefName")
            captured["target"] = req.url.params.get("searchCriteria.targetRefName")
            return _json({"value": []})
        raise AssertionError

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().list_prs(
        _project(),
        token="t",
        filters=PRFilters(head="feat/x", base="develop"),
    )
    assert captured["source"] == "refs/heads/feat/x"
    assert captured["target"] == "refs/heads/develop"


def test_list_prs_filters_by_label_client_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        if "/pullrequests" in req.url.path:
            return _json({
                "value": [
                    _pr_payload(1, labels=[{"name": "bug"}]),
                    _pr_payload(2, labels=[{"name": "other"}]),
                ]
            })
        raise AssertionError

    _install_mock(monkeypatch, handler)
    prs = AzureDevOpsProvider().list_prs(
        _project(),
        token="t",
        filters=PRFilters(labels=["bug"]),
    )
    assert [p.id for p in prs] == ["1"]


def test_repo_id_cache_hits_after_first_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_list_calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal repo_list_calls
        if req.url.path.endswith("/_apis/git/repositories"):
            repo_list_calls += 1
            return _repos_response()
        if "/pullrequests" in req.url.path:
            return _json({"value": []})
        raise AssertionError

    _install_mock(monkeypatch, handler)
    p = AzureDevOpsProvider()
    for _ in range(3):
        p.list_prs(_project(), token="t", filters=PRFilters(limit=1))
    assert repo_list_calls == 1


# ---------- get_pr -----------------------------------------------------------


def test_get_pr_lists_top_level_thread_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        path = req.url.path
        if path.endswith("/pullrequests/7"):
            return _json(_pr_payload(7))
        if path.endswith("/pullrequests/7/threads"):
            return _json({
                "value": [
                    # Top-level discussion thread (no threadContext).
                    {
                        "id": 1,
                        "threadContext": None,
                        "comments": [
                            {
                                "id": 1,
                                "parentCommentId": 0,
                                "author": {"displayName": "Alice"},
                                "content": "<p>looks good</p>",
                                "commentType": "text",
                                "publishedDate": "2026-05-18T10:00:00Z",
                            }
                        ],
                    },
                    # Diff-anchored thread — excluded from top-level list.
                    {
                        "id": 2,
                        "threadContext": {
                            "filePath": "/a.py",
                            "rightFileStart": {"line": 5},
                        },
                        "comments": [
                            {
                                "id": 1,
                                "parentCommentId": 0,
                                "content": "<p>inline</p>",
                                "commentType": "text",
                            }
                        ],
                    },
                ]
            })
        raise AssertionError(f"unexpected {path}")

    _install_mock(monkeypatch, handler)
    pr, comments = AzureDevOpsProvider().get_pr(_project(), token="t", pr_id="7")
    assert pr.id == "7"
    assert [c.body for c in comments] == ["looks good"]


def test_list_pr_review_comments_only_anchored_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        if req.url.path.endswith("/pullrequests/7/threads"):
            return _json({
                "value": [
                    {
                        "id": 1, "threadContext": None,
                        "comments": [
                            {"id": 1, "content": "<p>ignored</p>", "commentType": "text"}
                        ],
                    },
                    {
                        "id": 2,
                        "threadContext": {
                            "filePath": "/a.py",
                            "rightFileStart": {"line": 5},
                        },
                        "comments": [
                            {
                                "id": 1,
                                "parentCommentId": 0,
                                "author": {"displayName": "Reviewer"},
                                "content": "<p>fix here</p>",
                                "commentType": "text",
                            }
                        ],
                    },
                ]
            })
        raise AssertionError

    _install_mock(monkeypatch, handler)
    rcs = AzureDevOpsProvider().list_pr_review_comments(
        _project(), token="t", pr_id="7"
    )
    assert len(rcs) == 1
    rc = rcs[0]
    assert rc.path == "/a.py"
    assert rc.line == 5
    assert rc.side == "RIGHT"


# ---------- create_pr -------------------------------------------------------


def test_create_pr_emits_refs_and_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        if req.method == "POST" and req.url.path.endswith("/pullrequests"):
            captured["body"] = json.loads(req.content.decode("utf-8"))
            return _json(_pr_payload(11))
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    pr = AzureDevOpsProvider().create_pr(
        _project(),
        token="t",
        title="hello",
        body="Body",
        head="feat/x",
        base="main",
        draft=True,
    )
    assert pr.id == "11"
    body = captured["body"]
    assert body["sourceRefName"] == "refs/heads/feat/x"
    assert body["targetRefName"] == "refs/heads/main"
    assert body["isDraft"] is True
    assert "#ai-generated" in body["description"]


# ---------- update_pr -------------------------------------------------------


def test_update_pr_status_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Closing a PR via our generic `status='closed'` maps to ADO 'abandoned'."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        path = req.url.path
        if req.method == "PATCH" and path.endswith("/pullrequests/7"):
            captured["body"] = json.loads(req.content.decode("utf-8"))
            return _json(_pr_payload(7, status="abandoned"))
        if req.method == "GET" and path.endswith("/pullrequests/7"):
            return _json(_pr_payload(7, status="abandoned"))
        if path.endswith("/threads"):
            return _json({"value": []})
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().update_pr(_project(), token="t", pr_id="7", status="closed")
    assert captured["body"]["status"] == "abandoned"


# ---------- merge_pr --------------------------------------------------------


@pytest.mark.parametrize("ours,theirs", [
    ("merge", "noFastForward"),
    ("squash", "squash"),
    ("rebase", "rebase"),
])
def test_merge_pr_method_mapping(
    monkeypatch: pytest.MonkeyPatch, ours: str, theirs: str
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        path = req.url.path
        if req.method == "GET" and path.endswith("/pullrequests/7"):
            return _json(_pr_payload(7))
        if req.method == "PATCH" and path.endswith("/pullrequests/7"):
            captured["body"] = json.loads(req.content.decode("utf-8"))
            return _json(
                _pr_payload(7, status="completed", mergeStatus="succeeded")
            )
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().merge_pr(
        _project(), token="t", pr_id="7", merge_method=ours
    )
    assert captured["body"]["status"] == "completed"
    assert captured["body"]["completionOptions"]["mergeStrategy"] == theirs


# ---------- add_pr_comment + review comments -------------------------------


def test_add_pr_comment_creates_thread_without_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        if req.method == "POST" and req.url.path.endswith("/pullrequests/7/threads"):
            captured["body"] = json.loads(req.content.decode("utf-8"))
            return _json({
                "id": 42,
                "threadContext": None,
                "comments": [
                    {
                        "id": 1,
                        "author": {"displayName": "AI"},
                        "content": captured["body"]["comments"][0]["content"],
                        "commentType": "text",
                        "publishedDate": "2026-05-18T10:00:00Z",
                    }
                ],
            })
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    comment = AzureDevOpsProvider().add_pr_comment(
        _project(), token="t", pr_id="7", body="LGTM"
    )
    body = captured["body"]
    assert "threadContext" not in body
    assert body["comments"][0]["parentCommentId"] == 0
    assert "#ai-generated" in body["comments"][0]["content"]
    assert comment.id.startswith("42.")


def test_add_pr_review_comment_anchored_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        if req.method == "POST" and req.url.path.endswith("/pullrequests/7/threads"):
            captured["body"] = json.loads(req.content.decode("utf-8"))
            return _json({
                "id": 99,
                "threadContext": captured["body"]["threadContext"],
                "comments": [
                    {
                        "id": 1,
                        "parentCommentId": 0,
                        "author": {"displayName": "AI"},
                        "content": captured["body"]["comments"][0]["content"],
                        "commentType": "text",
                        "publishedDate": "2026-05-18T10:00:00Z",
                    }
                ],
            })
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    _install_mock(monkeypatch, handler)
    rc = AzureDevOpsProvider().add_pr_review_comment(
        _project(), token="t", pr_id="7",
        body="please fix", path="/file.py", line=12, side="RIGHT",
    )
    ctx = captured["body"]["threadContext"]
    assert ctx["filePath"] == "/file.py"
    assert ctx["rightFileStart"]["line"] == 12
    assert "leftFileStart" not in ctx
    assert rc.path == "/file.py"
    assert rc.line == 12
    assert rc.side == "RIGHT"


def test_add_pr_review_comment_left_side(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        if req.method == "POST" and req.url.path.endswith("/pullrequests/7/threads"):
            captured["body"] = json.loads(req.content.decode("utf-8"))
            return _json({
                "id": 99,
                "threadContext": captured["body"]["threadContext"],
                "comments": [
                    {"id": 1, "parentCommentId": 0, "content": "<p>x</p>", "commentType": "text"}
                ],
            })
        raise AssertionError

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().add_pr_review_comment(
        _project(), token="t", pr_id="7",
        body="x", path="/file.py", line=12, side="LEFT",
    )
    ctx = captured["body"]["threadContext"]
    assert "leftFileStart" in ctx
    assert "rightFileStart" not in ctx


def test_add_pr_review_comment_rejects_without_anchor_or_reply() -> None:
    from project_issues_plugin.providers.azuredevops import AzureDevOpsError

    with pytest.raises(AzureDevOpsError) as exc:
        AzureDevOpsProvider().add_pr_review_comment(
            _project(), token="t", pr_id="7", body="x"
        )
    assert "in_reply_to" in str(exc.value) or "path" in str(exc.value)


def test_submit_pr_review_vote_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        cached = _repos_handler(req)
        if cached is not None:
            return cached
        path = req.url.path
        if path.endswith("/_apis/connectionData"):
            return _json({
                "authenticatedUser": {
                    "id": "user-guid",
                    "displayName": "Me",
                }
            })
        if req.method == "PUT" and "/reviewers/user-guid" in path:
            captured["body"] = json.loads(req.content.decode("utf-8"))
            return _json({"id": "user-guid", "vote": captured["body"]["vote"]})
        if req.method == "POST" and path.endswith("/threads"):
            return _json({
                "id": 1,
                "comments": [
                    {"id": 1, "content": "<p>x</p>", "commentType": "text"}
                ],
            })
        raise AssertionError(f"unexpected {req.method} {path}")

    _install_mock(monkeypatch, handler)
    AzureDevOpsProvider().submit_pr_review(
        _project(), token="t", pr_id="7", state="approve", body="lgtm"
    )
    assert captured["body"]["vote"] == 10
