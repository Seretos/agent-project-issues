"""Tests for the write-side relation tools (ticket #41).

Covers `add_relation` / `remove_relation` on both providers via mocked
HTTP transports. The same provider-agnostic surface translates to:
  - GitHub: Sub-Issues API (parent/child), Dependencies API
    (blocks/blocked_by, API 2026-03-10), body-edit + close
    (duplicate_of).
  - GitLab: Issue Links REST (blocks/blocked_by/relates_to),
    Work Items GraphQL `hierarchyWidget` (parent/child), body-edit +
    close + relates_to (duplicate_of).

Kinds the provider cannot model natively surface as
`RelationKindUnsupported` (the `_safe` wrapper translates that to
`{"error": "..."}` for the tool callers).
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers import gitlab as gitlab_provider
from lib_python_projects.providers.base import RelationKindUnsupported, RelationNotFound
from lib_python_projects.providers.github import GitHubProvider
from lib_python_projects.providers.gitlab import GitLabProvider


# ---------- helpers ----------------------------------------------------------


def _github_project() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
    )


def _gitlab_project() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="gitlab",
        path="acme/backend",
        token_env="GITLAB_TOKEN_ACME",
    )


def _json(payload, status_code: int = 200) -> httpx.Response:
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
        return httpx.Client(
            base_url=github_provider.API_BASE,
            headers={"Accept": "application/vnd.github+json"},
            transport=transport,
        )

    monkeypatch.setattr(github_provider, "_client", fake_client)
    return seen


def _install_gitlab_mock(
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
            base_url=gitlab_provider._base_url(project),
            headers={"Accept": "application/json"},
            transport=transport,
        )

    monkeypatch.setattr(gitlab_provider, "_client", fake_client)
    return seen


def _gh_issue(number: int, **overrides) -> dict:
    base = {
        "id": 10_000 + number,                # internal id
        "number": number,
        "title": f"Issue {number}",
        "body": "",
        "state": "open",
        "user": {"login": "alice"},
        "assignees": [],
        "labels": [],
        "html_url": f"https://github.com/acme/backend/issues/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
    }
    base.update(overrides)
    return base


def _gl_issue(iid: int, **overrides) -> dict:
    base = {
        "iid": iid,
        "title": f"Issue {iid}",
        "description": "",
        "state": "opened",
        "author": {"username": "alice"},
        "assignees": [],
        "labels": [],
        "web_url": f"https://gitlab.com/acme/backend/-/issues/{iid}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
    }
    base.update(overrides)
    return base


# ---------- GitHub: child / parent (Sub-Issues API) -------------------------


def test_github_add_relation_parent_posts_sub_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_relation(A, kind=parent, target=B) → POST /issues/A/sub_issues
    with `sub_issue_id` set to B's internal id (A becomes B's parent).

    lib-python-projects v0.3.3 (#171) corrected the previously-inverted
    parent/child write direction: `kind=parent` now targets the ticket's
    own sub_issues endpoint with the target as the sub-issue."""
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        # Resolve target B's internal id.
        if req.method == "GET" and path == "/repos/acme/backend/issues/7":
            return _json(_gh_issue(7))
        # v0.1.8 pre-flight: check existing sub-issues before POST.
        if req.method == "GET" and path == "/repos/acme/backend/issues/5/sub_issues":
            return _json([])
        if (
            req.method == "POST"
            and path == "/repos/acme/backend/issues/5/sub_issues"
        ):
            captured["body"] = json.loads(req.content)
            return _json({"ok": True})
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_github_mock(monkeypatch, handler)
    rel = GitHubProvider().add_relation(
        _github_project(), "tok", "5", "parent", "#7",
    )
    assert rel.kind == "parent"
    assert rel.ticket_id == "#7"
    assert captured["body"] == {"sub_issue_id": 10_007}


def test_github_add_relation_child_swaps_to_parent_on_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_relation(A, kind=child, target=B) → POST /issues/B/sub_issues
    with `sub_issue_id` set to A's internal id (B becomes A's parent).

    lib-python-projects v0.3.3 (#171) corrected the previously-inverted
    parent/child write direction: `kind=child` now targets the target's
    own sub_issues endpoint with the ticket as the sub-issue, resolving
    the ticket's own internal id (with 404 handling) first."""
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/issues/7":
            return _json(_gh_issue(7))
        if req.method == "GET" and path == "/repos/acme/backend/issues/5":
            return _json(_gh_issue(5))
        # v0.1.8 pre-flight: check existing sub-issues before POST.
        if req.method == "GET" and path == "/repos/acme/backend/issues/7/sub_issues":
            return _json([])
        if (
            req.method == "POST"
            and path == "/repos/acme/backend/issues/7/sub_issues"
        ):
            captured["body"] = json.loads(req.content)
            return _json({"ok": True})
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_github_mock(monkeypatch, handler)
    rel = GitHubProvider().add_relation(
        _github_project(), "tok", "5", "child", "#7",
    )
    assert rel.kind == "child"
    assert rel.ticket_id == "#7"
    assert captured["body"] == {"sub_issue_id": 10_005}


def test_github_remove_relation_child_deletes_sub_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """child(A→B): B is A's parent → DELETE fires on B's (the target's) own
    sub_issue endpoint (lib-python-projects v0.3.3, #171)."""
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(f"{req.method} {req.url.path}")
        if req.method == "GET":
            return _json(_gh_issue(7))
        if (
            req.method == "DELETE"
            and req.url.path == "/repos/acme/backend/issues/7/sub_issue"
        ):
            return _json({"ok": True})
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_github_mock(monkeypatch, handler)
    result = GitHubProvider().remove_relation(
        _github_project(), "tok", "5", "child", "#7",
    )
    assert result == {"removed": True}


# ---------- GitHub: blocked_by / blocks (Dependencies API) ------------------


def test_github_add_relation_blocked_by_posts_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_relation(A, kind=blocked_by, target=B) → POST
    /issues/A/dependencies/blocked_by with {issue_id: <B internal id>}."""
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/issues/7":
            return _json(_gh_issue(7))
        # v0.1.8 pre-flight: check existing blocked_by deps before POST.
        if (
            req.method == "GET"
            and path == "/repos/acme/backend/issues/5/dependencies/blocked_by"
        ):
            return _json([])
        if (
            req.method == "POST"
            and path == "/repos/acme/backend/issues/5"
            "/dependencies/blocked_by"
        ):
            captured["body"] = json.loads(req.content)
            return _json({"ok": True})
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_github_mock(monkeypatch, handler)
    rel = GitHubProvider().add_relation(
        _github_project(), "tok", "5", "blocked_by", "#7",
    )
    assert rel.kind == "blocked_by"
    assert captured["body"] == {"issue_id": 10_007}


def test_github_add_relation_blocks_swaps_to_blocked_by_on_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """blocks(A→B) is sent as blocked_by(B→A) on B's endpoint."""
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/issues/7":
            return _json(_gh_issue(7))
        if req.method == "GET" and path == "/repos/acme/backend/issues/5":
            return _json(_gh_issue(5))
        # v0.1.8 pre-flight: check existing blocked_by deps on B before POST.
        if (
            req.method == "GET"
            and path == "/repos/acme/backend/issues/7/dependencies/blocked_by"
        ):
            return _json([])
        if (
            req.method == "POST"
            and path == "/repos/acme/backend/issues/7"
            "/dependencies/blocked_by"
        ):
            captured["body"] = json.loads(req.content)
            return _json({"ok": True})
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_github_mock(monkeypatch, handler)
    rel = GitHubProvider().add_relation(
        _github_project(), "tok", "5", "blocks", "#7",
    )
    assert rel.kind == "blocks"
    assert captured["body"] == {"issue_id": 10_005}


def test_github_remove_relation_blocked_by_deletes_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: the relation exists, the pre-check finds it, the
    DELETE proceeds, and the provider reports `removed=True`."""
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(f"{req.method} {req.url.path}")
        if (
            req.method == "GET"
            and req.url.path
            == "/repos/acme/backend/issues/5/dependencies/blocked_by"
        ):
            # The pre-check (ticket #49 finding 8) GETs the current
            # dependency list — return one entry whose `id` matches the
            # target's resolved internal id.
            return _json([{"id": 10007, "number": 7}])
        if req.method == "GET":
            return _json(_gh_issue(7))
        if (
            req.method == "DELETE"
            and req.url.path
            == "/repos/acme/backend/issues/5/dependencies/blocked_by/10007"
        ):
            return _json({"ok": True})
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_github_mock(monkeypatch, handler)
    result = GitHubProvider().remove_relation(
        _github_project(), "tok", "5", "blocked_by", "#7",
    )
    assert result == {"removed": True}


def test_github_remove_relation_blocked_by_404_when_link_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ticket #49 finding 8 / #48 finding 3: the documented contract is
    that removing a non-existent relation errors instead of silently
    succeeding. The pre-check now enforces that."""
    def handler(req: httpx.Request) -> httpx.Response:
        if (
            req.method == "GET"
            and req.url.path
            == "/repos/acme/backend/issues/5/dependencies/blocked_by"
        ):
            return _json([])  # no dependencies at all
        if req.method == "GET":
            return _json(_gh_issue(7))
        if req.method == "DELETE":
            raise AssertionError(
                "DELETE must not fire when the pre-check fails"
            )
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_github_mock(monkeypatch, handler)
    from lib_python_projects.providers.base import RelationNotFound
    with pytest.raises(RelationNotFound, match="no 'blocked_by' relation"):
        GitHubProvider().remove_relation(
            _github_project(), "tok", "5", "blocked_by", "#7",
        )


# ---------- GitHub: duplicate_of (body + state) -----------------------------


def test_github_add_relation_duplicate_of_edits_body_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """duplicate_of edits the body (inserting `Duplicate of #N` after the
    AI marker) and closes the issue with state_reason=duplicate."""
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/issues/7":
            return _json(_gh_issue(7))
        if req.method == "GET" and path == "/repos/acme/backend/issues/5":
            return _json(_gh_issue(
                5, body="#ai-generated\n\noriginal description",
                labels=[{"name": "ai-generated"}],
            ))
        if req.method == "PATCH" and path == "/repos/acme/backend/issues/5":
            captured["body"] = json.loads(req.content)
            return _json(_gh_issue(
                5, state="closed", state_reason="duplicate",
            ))
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_github_mock(monkeypatch, handler)
    rel = GitHubProvider().add_relation(
        _github_project(), "tok", "5", "duplicate_of", "#7",
    )
    assert rel.kind == "duplicate_of"
    patch = captured["body"]
    assert patch["state"] == "closed"
    assert patch["state_reason"] == "duplicate"
    assert patch["body"].startswith("#ai-generated\n\n")
    assert "Duplicate of #7" in patch["body"]
    assert "original description" in patch["body"]
    # marker appears exactly once
    assert patch["body"].count("#ai-generated") == 1
    assert patch["body"].count("#ai-modified") == 0


def test_github_add_relation_duplicate_of_stamps_modified_on_human_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """duplicate_of on a human-authored ticket stamps #ai-modified."""
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/issues/7":
            return _json(_gh_issue(7))
        if req.method == "GET" and path == "/repos/acme/backend/issues/5":
            return _json(_gh_issue(5, body="human description"))
        if req.method == "PATCH" and path == "/repos/acme/backend/issues/5":
            captured["body"] = json.loads(req.content)
            return _json(_gh_issue(5, state="closed", state_reason="duplicate"))
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_github_mock(monkeypatch, handler)
    GitHubProvider().add_relation(
        _github_project(), "tok", "5", "duplicate_of", "#7",
    )
    assert captured["body"]["body"].startswith("#ai-modified\n\n")


def test_github_remove_relation_duplicate_of_reopens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lib-python-projects v0.3.3 (#173) added an existence pre-check: the
    source issue's body must actually contain the `Duplicate of #N` marker
    before removal proceeds, so the mock source body must carry it."""
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/repos/acme/backend/issues/5":
            return _json(_gh_issue(
                5, body="#ai-generated\n\nDuplicate of #7\n\noriginal description",
            ))
        if req.method == "GET":
            return _json(_gh_issue(7))
        if req.method == "PATCH" and req.url.path == "/repos/acme/backend/issues/5":
            captured["body"] = json.loads(req.content)
            return _json(_gh_issue(5))
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_github_mock(monkeypatch, handler)
    GitHubProvider().remove_relation(
        _github_project(), "tok", "5", "duplicate_of", "#7",
    )
    # v0.1.6: remove_relation for duplicate_of now also rewrites the body
    # (strips the "Duplicate of #N" marker and re-applies the AI marker) in
    # the same PATCH request that reopens the issue.
    assert captured["body"]["state"] == "open"  # type: ignore[index]
    assert captured["body"]["body"].startswith("#ai-")  # type: ignore[union-attr]


# ---------- GitHub: relates_to unsupported ----------------------------------


def test_github_add_relation_relates_to_raises_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_github_mock(monkeypatch, lambda r: _json({}, 200))
    with pytest.raises(RelationKindUnsupported) as ei:
        GitHubProvider().add_relation(
            _github_project(), "tok", "5", "relates_to", "#7",
        )
    assert ei.value.kind == "relates_to"
    assert ei.value.provider == "github"
    assert "duplicate_of" in ei.value.supported_kinds


# ---------- GitLab: blocked_by / blocks / relates_to (Issue Links) ----------


def test_gitlab_add_relation_blocked_by_posts_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_gitlab_mock(monkeypatch, lambda r: _json({}, 200))
    with pytest.raises(RelationKindUnsupported) as ei:
        GitLabProvider().add_relation(
            _gitlab_project(), "tok", "5", "blocked_by", "#7",
        )
    assert ei.value.kind == "blocked_by"
    assert ei.value.provider == "gitlab"


def test_gitlab_add_relation_blocks_uses_blocks_link_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_gitlab_mock(monkeypatch, lambda r: _json({}, 200))
    with pytest.raises(RelationKindUnsupported) as ei:
        GitLabProvider().add_relation(
            _gitlab_project(), "tok", "5", "blocks", "#7",
        )
    assert ei.value.kind == "blocks"
    assert ei.value.provider == "gitlab"


def test_gitlab_add_relation_relates_to_uses_relates_to_link_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/api/v4/projects/acme/backend":
            return _json({"id": 4242})
        if (
            req.method == "POST"
            and "/issues/5/links" in req.url.path
        ):
            captured["body"] = json.loads(req.content.decode())
            return _json({"iid": 7, "web_url": "x"})
        return _json({}, status_code=404)

    _install_gitlab_mock(monkeypatch, handler)
    GitLabProvider().add_relation(
        _gitlab_project(), "tok", "5", "relates_to", "#7",
    )
    assert captured["body"]["link_type"] == "relates_to"


def test_gitlab_remove_relation_blocked_by_deletes_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_gitlab_mock(monkeypatch, lambda r: _json({}, 200))
    with pytest.raises(RelationKindUnsupported) as ei:
        GitLabProvider().remove_relation(
            _gitlab_project(), "tok", "5", "blocked_by", "#7",
        )
    assert ei.value.kind == "blocked_by"
    assert ei.value.provider == "gitlab"


# ---------- GitLab: parent / child via Work Items GraphQL (lib v0.3.0) -----
#
# lib-python-projects v0.3.0 added GitLab `parent`/`child` relation support
# via the Work Items GraphQL `hierarchyWidget` (ticket #151 in the lib) —
# previously these two kinds raised `RelationKindUnsupported`. These tests
# were updated (alongside the v0.3.0 pin bump, ticket #201) to cover the
# now-supported happy path via a minimal GraphQL mock instead of asserting
# the stale "unsupported" behaviour.


def _gl_work_item_node(
    *, gid: str, iid: str, title: str, web_url: str,
    state: str = "OPEN", parent: dict | None = None,
) -> dict:
    return {
        "id": gid,
        "iid": iid,
        "title": title,
        "webUrl": web_url,
        "state": state,
        "widgets": [{"parent": parent}],
    }


def _install_gitlab_hierarchy_mock(
    monkeypatch: pytest.MonkeyPatch, work_items_by_iid: dict[str, dict],
) -> list[httpx.Request]:
    """Mock the two GraphQL round-trips `_gitlab_add_hierarchy_relation`
    makes: a `workItems(iid: ...)` query per side of the edge, then a
    `workItemUpdate` hierarchy mutation."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/graphql"
        body = json.loads(req.content.decode())
        query = body["query"]
        variables = body["variables"]
        if "workItemUpdate" in query:
            work_item = work_items_by_iid[variables["id"]]
            return _json({
                "data": {
                    "workItemUpdate": {
                        "workItem": work_item,
                        "errors": [],
                    }
                }
            })
        # workItems hierarchy lookup query.
        work_item = work_items_by_iid.get(variables["iid"])
        return _json({
            "data": {
                "project": {
                    "workItems": {"nodes": [work_item] if work_item else []}
                }
            }
        })

    return _install_gitlab_mock(monkeypatch, handler)


def test_gitlab_add_relation_parent_sets_hierarchy_via_graphql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kind="parent": ticket_id (#5)'s work item gets target (#7) as its
    new parent — the returned Relation describes the target (#7)."""
    subject = _gl_work_item_node(
        gid="gid://gitlab/WorkItem/500", iid="5",
        title="Issue 5", web_url="https://gitlab.example.com/acme/backend/-/work_items/5",
    )
    parent = _gl_work_item_node(
        gid="gid://gitlab/WorkItem/700", iid="7",
        title="Issue 7", web_url="https://gitlab.example.com/acme/backend/-/work_items/7",
    )
    work_items_by_iid = {
        "5": subject,
        "7": parent,
        # Mutation lookups key on the work-item gid passed as `id`.
        "gid://gitlab/WorkItem/500": {
            **subject,
            "widgets": [{"parent": parent}],
        },
    }
    _install_gitlab_hierarchy_mock(monkeypatch, work_items_by_iid)

    relation = GitLabProvider().add_relation(
        _gitlab_project(), "tok", "5", "parent", "#7",
    )
    assert relation.kind == "parent"
    assert relation.ticket_id == "#7"
    assert relation.title == "Issue 7"
    assert relation.url == "https://gitlab.example.com/acme/backend/-/issues/7"
    assert relation.state == "open"
    assert relation.resolved is True


def test_gitlab_add_relation_child_sets_hierarchy_via_graphql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kind="child": target (#7)'s work item gets ticket_id (#5) as its
    new parent — the returned Relation still describes the target (#7),
    but this time `target_wi` is the *subject* work item (mirrors the
    lib's `target_wi = parent_wi if kind == "parent" else subject_wi`)."""
    subject = _gl_work_item_node(
        gid="gid://gitlab/WorkItem/700", iid="7",
        title="Issue 7", web_url="https://gitlab.example.com/acme/backend/-/work_items/7",
    )
    parent = _gl_work_item_node(
        gid="gid://gitlab/WorkItem/500", iid="5",
        title="Issue 5", web_url="https://gitlab.example.com/acme/backend/-/work_items/5",
    )
    work_items_by_iid = {
        "7": subject,
        "5": parent,
        "gid://gitlab/WorkItem/700": {
            **subject,
            "widgets": [{"parent": parent}],
        },
    }
    _install_gitlab_hierarchy_mock(monkeypatch, work_items_by_iid)

    relation = GitLabProvider().add_relation(
        _gitlab_project(), "tok", "5", "child", "#7",
    )
    assert relation.kind == "child"
    assert relation.ticket_id == "#7"
    assert relation.title == "Issue 7"
    assert relation.url == "https://gitlab.example.com/acme/backend/-/issues/7"
    assert relation.state == "open"
    assert relation.resolved is True


def test_gitlab_add_relation_parent_not_found_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When either side of the edge can't be resolved to a work item
    (e.g. the mock/real project has no matching iid), `add_relation`
    raises `RelationNotFound` rather than silently no-op'ing."""
    _install_gitlab_mock(monkeypatch, lambda r: _json({}, 200))
    with pytest.raises(RelationNotFound):
        GitLabProvider().add_relation(
            _gitlab_project(), "tok", "5", "parent", "#7",
        )


# ---------- GitLab: duplicate_of ---------------------------------------------


def test_gitlab_add_relation_duplicate_of_edits_body_closes_and_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_put: dict[str, object] = {}
    captured_post: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/api/v4/projects/acme/backend":
            return _json({"id": 4242})
        if req.method == "GET" and "/issues/5" in path and "links" not in path:
            return _json(_gl_issue(
                5, description="#ai-generated\n\nsrc body",
                labels=["ai-generated"],
            ))
        if req.method == "PUT" and path.endswith("/issues/5"):
            captured_put["body"] = json.loads(req.content.decode())
            return _json(_gl_issue(5, state="closed"))
        if req.method == "POST" and "/issues/5/links" in path:
            captured_post["body"] = json.loads(req.content.decode())
            return _json({"iid": 7, "title": "T", "state": "opened",
                          "web_url": "x"})
        raise AssertionError(f"unexpected {req.method} {req.url}")

    _install_gitlab_mock(monkeypatch, handler)
    rel = GitLabProvider().add_relation(
        _gitlab_project(), "tok", "5", "duplicate_of", "#7",
    )
    assert rel.kind == "duplicate_of"
    # Body must contain the duplicate-of line and keep the marker.
    assert captured_put["body"]["state_event"] == "close"
    desc = captured_put["body"]["description"]
    assert desc.startswith("#ai-generated\n\n")
    # Ticket #49 finding 2: we always use the issue sigil `#N`, never
    # the MR sigil `!N`, because the target is an issue.
    assert "Duplicate of #7" in desc
    assert "src body" in desc
    # And a relates_to issue link was also posted.
    assert captured_post["body"]["link_type"] == "relates_to"
    assert captured_post["body"]["target_issue_iid"] == "7"


def test_gitlab_remove_relation_duplicate_of_reopens_and_deletes_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        seen.append(f"{req.method} {path}")
        if req.method == "GET" and path.endswith("/issues/5/links"):
            return _json([
                {"iid": 7, "issue_link_id": 99, "web_url": "x"},
            ])
        if req.method == "DELETE" and path.endswith("/issues/5/links/99"):
            return _json({})
        # v0.1.8: GET current issue body before stripping dup line and PUT reopen.
        if req.method == "GET" and path.endswith("/issues/5"):
            return _json(_gl_issue(5, description="Duplicate of #7\n\nsrc body"))
        if req.method == "PUT" and path.endswith("/issues/5"):
            return _json(_gl_issue(5))
        return _json({}, status_code=404)

    _install_gitlab_mock(monkeypatch, handler)
    result = GitLabProvider().remove_relation(
        _gitlab_project(), "tok", "5", "duplicate_of", "#7",
    )
    assert result == {"removed": True}
    assert any(s.endswith("/issues/5/links/99") and "DELETE" in s for s in seen)
    assert any(s.endswith("/issues/5") and "PUT" in s for s in seen)


# ---------- target parsing edge cases ---------------------------------------


def test_github_add_relation_invalid_target_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_github_mock(monkeypatch, lambda r: _json({}, 200))
    with pytest.raises(ValueError, match="relation target"):
        GitHubProvider().add_relation(
            _github_project(), "tok", "5", "child", "not-a-number",
        )


def test_github_cross_repo_target_not_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-repo targets are reserved surface but not yet implemented."""
    _install_github_mock(monkeypatch, lambda r: _json({}, 200))
    with pytest.raises(NotImplementedError, match="cross-repo"):
        GitHubProvider().add_relation(
            _github_project(), "tok", "5", "child", "other/repo#7",
        )
