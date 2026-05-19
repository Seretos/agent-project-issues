"""Tests for the GitLab provider's issue + comment surface.

Covers:
- `list_tickets` filter param translation (state, labels, not_labels,
  assignee, author, search, dates, sort)
- `get_ticket` issue fetch + system-note filtering
- `create_ticket` ai-generated marker (body prefix + label) and
  assignee resolution
- `update_ticket` state_event mapping, label add/remove, ai-modified
  heuristic, assignee delta
- `add_comment`, `list_comments`, `get_comment`, `update_comment`
  composite-key handling
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from project_issues_plugin.config import ProjectConfig
from project_issues_plugin.providers import gitlab as gitlab_mod
from project_issues_plugin.providers.base import TicketFilters
from project_issues_plugin.providers.gitlab import GitLabError, GitLabProvider


def _project(path: str = "acme/backend") -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="gitlab",
        path=path,
        token_env="GITLAB_TOKEN_ACME",
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
        headers = {
            "Accept": "application/json",
            "User-Agent": "test-agent",
        }
        if token:
            headers["PRIVATE-TOKEN"] = token
        return httpx.Client(
            base_url=gitlab_mod._base_url(project),
            headers=headers,
            transport=transport,
        )

    monkeypatch.setattr(gitlab_mod, "_client", fake_client)
    return seen


def _json(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def _issue_payload(iid: int, **overrides) -> dict:
    base = {
        "iid": iid,
        "title": f"Issue {iid}",
        "description": "body",
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


# ---------- list_tickets -----------------------------------------------------


def test_list_tickets_default_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET"
        # Path uses URL-encoded project path.
        assert "acme%2Fbackend" in str(req.url)
        # Defaults: state=opened, per_page=30, sort=desc, order_by=created_at.
        assert req.url.params.get("state") == "opened"
        assert req.url.params.get("per_page") == "30"
        assert req.url.params.get("order_by") == "created_at"
        assert req.url.params.get("sort") == "desc"
        # No filter params set in the default case.
        assert "labels" not in req.url.params
        assert "assignee_username" not in req.url.params
        return _json([_issue_payload(1), _issue_payload(2)])

    _install_mock(monkeypatch, handler)
    tickets = GitLabProvider().list_tickets(
        _project(), token="tok", filters=TicketFilters(),
    )
    assert len(tickets) == 2
    assert tickets[0].id == "1"


def test_list_tickets_state_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_states: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_states.append(req.url.params.get("state"))
        return _json([])

    _install_mock(monkeypatch, handler)
    p = _project()
    GitLabProvider().list_tickets(p, "t", TicketFilters(status="open"))
    GitLabProvider().list_tickets(p, "t", TicketFilters(status="closed"))
    GitLabProvider().list_tickets(p, "t", TicketFilters(status="any"))
    assert seen_states == ["opened", "closed", "all"]


def test_list_tickets_label_and_assignee_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params.get("labels") == "bug,p1"
        assert req.url.params.get("not[labels]") == "wontfix"
        assert req.url.params.get("assignee_username") == "alice"
        assert req.url.params.get("author_username") == "bob"
        assert req.url.params.get("search") == "memory leak"
        return _json([])

    _install_mock(monkeypatch, handler)
    GitLabProvider().list_tickets(
        _project(), "t",
        TicketFilters(
            labels=["bug", "p1"],
            not_labels=["wontfix"],
            assignee="alice",
            author="bob",
            search="memory leak",
        ),
    )


def test_list_tickets_date_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params.get("created_after") == "2026-01-01"
        assert req.url.params.get("created_before") == "2026-12-31"
        assert req.url.params.get("updated_after") == "2026-05-01"
        assert req.url.params.get("updated_before") == "2026-05-31"
        return _json([])

    _install_mock(monkeypatch, handler)
    GitLabProvider().list_tickets(
        _project(), "t",
        TicketFilters(
            created_after="2026-01-01",
            created_before="2026-12-31",
            updated_after="2026-05-01",
            updated_before="2026-05-31",
        ),
    )


def test_list_tickets_sort_by_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.params.get("order_by"))
        return _json([])

    _install_mock(monkeypatch, handler)
    p = _project()
    GitLabProvider().list_tickets(p, "t", TicketFilters(sort_by="created"))
    GitLabProvider().list_tickets(p, "t", TicketFilters(sort_by="updated"))
    GitLabProvider().list_tickets(p, "t", TicketFilters(sort_by="comments"))
    assert seen == ["created_at", "updated_at", "user_notes_count"]


def test_list_tickets_limit_capped_at_100(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params.get("per_page") == "100"
        return _json([])

    _install_mock(monkeypatch, handler)
    GitLabProvider().list_tickets(
        _project(), "t", TicketFilters(limit=500),
    )


def test_list_tickets_propagates_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "404 Not Found"}, status_code=404)

    _install_mock(monkeypatch, handler)
    with pytest.raises(GitLabError) as exc:
        GitLabProvider().list_tickets(_project(), "t", TicketFilters())
    assert exc.value.status == 404


# ---------- get_ticket -------------------------------------------------------


def test_get_ticket_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("acme%2Fbackend/issues/5"):
            return _json(_issue_payload(5))
        if "acme%2Fbackend/issues/5/notes" in url:
            return _json([
                {
                    "id": 100, "body": "comment 1", "system": False,
                    "author": {"username": "alice"},
                    "created_at": "2024-01-01T00:00:00Z",
                },
                {
                    "id": 101, "body": "added label", "system": True,
                    "author": {"username": "alice"},
                    "created_at": "2024-01-01T00:01:00Z",
                },
                {
                    "id": 102, "body": "comment 2", "system": False,
                    "author": {"username": "bob"},
                    "created_at": "2024-01-01T00:02:00Z",
                },
            ])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    ticket, comments, relations, truncated = GitLabProvider().get_ticket(
        _project(), token="t", ticket_id="5",
    )
    assert ticket.id == "5"
    # System notes are filtered out — only the two user comments remain.
    assert [c.id for c in comments] == ["100", "102"]
    assert relations == []
    assert truncated is False


def test_get_ticket_skips_relations_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`include_relations=False` doesn't change the issue/notes calls,
    but it bypasses the (currently-stubbed) relations resolver. We
    document the call shape here so task #7 can extend it without
    breaking existing callers."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/issues/5"):
            return _json(_issue_payload(5))
        return _json([])

    _install_mock(monkeypatch, handler)
    _, _, relations, _ = GitLabProvider().get_ticket(
        _project(), "t", "5", include_relations=False,
    )
    assert relations == []


# ---------- create_ticket ----------------------------------------------------


def test_create_ticket_applies_marker_label_and_body_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "/issues" in req.url.path:
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(
                42,
                description=captured["body"]["description"],
                labels=captured["body"].get("labels", "").split(",") if captured["body"].get("labels") else [],
            ))
        if req.method == "GET" and req.url.path == "/api/v4/users":
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    ticket = GitLabProvider().create_ticket(
        _project(), "t", title="New issue", body="content",
        labels=["bug"], assignees=[],
    )
    assert ticket.id == "42"
    assert captured["body"]["title"] == "New issue"
    # Body prefix applied.
    assert captured["body"]["description"].startswith("#ai-generated")
    # ai-generated label included alongside caller-supplied "bug".
    assert "ai-generated" in captured["body"]["labels"]
    assert "bug" in captured["body"]["labels"]


def test_create_ticket_resolves_assignee_usernames_to_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/api/v4/users":
            username = req.url.params.get("username")
            users = {"alice": 1, "bob": 2}
            if username in users:
                return _json([{"id": users[username], "username": username}])
            return _json([])
        if req.method == "POST" and "/issues" in req.url.path:
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(1))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().create_ticket(
        _project(), "t", title="t", body="b", labels=[],
        assignees=["alice", "bob"],
    )
    assert captured["body"]["assignee_ids"] == [1, 2]


def test_create_ticket_drops_unknown_assignees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/api/v4/users":
            # No users match.
            return _json([])
        if req.method == "POST":
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(1))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().create_ticket(
        _project(), "t", title="t", body="b", labels=[],
        assignees=["ghost"],
    )
    # No assignee_ids key when nothing resolved — matches the GitHub
    # provider's "no assignees" call shape.
    assert "assignee_ids" not in captured["body"]


# ---------- update_ticket ----------------------------------------------------


def test_update_ticket_status_close(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return _json(_issue_payload(5, labels=["ai-generated"]))
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(5, state="closed"))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    ticket = GitLabProvider().update_ticket(
        _project(), "t", "5", status="closed:completed",
    )
    assert ticket.status == "closed"
    assert captured["body"]["state_event"] == "close"


def test_update_ticket_status_reopen(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return _json(_issue_payload(5, state="closed", labels=["ai-generated"]))
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(5, state="opened"))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_ticket(_project(), "t", "5", status="open")
    assert captured["body"]["state_event"] == "reopen"


def test_update_ticket_rejects_unknown_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json(_issue_payload(5, labels=["ai-generated"]))

    _install_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="unsupported status"):
        GitLabProvider().update_ticket(
            _project(), "t", "5", status="In Progress",
        )


def test_update_ticket_adds_ai_modified_for_non_ai_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            # No ai-generated label → ai-modified should be added.
            return _json(_issue_payload(5, labels=["bug"]))
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(5))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_ticket(_project(), "t", "5", title="renamed")
    assert "ai-modified" in captured["body"]["add_labels"]


def test_update_ticket_skips_ai_modified_when_already_ai_generated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return _json(_issue_payload(5, labels=["ai-generated"]))
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(5))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_ticket(_project(), "t", "5", title="renamed")
    # Body shouldn't have add_labels at all — nothing to add.
    assert "add_labels" not in captured["body"]


def test_update_ticket_label_add_and_remove(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return _json(_issue_payload(5, labels=["ai-generated", "p2"]))
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(5))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_ticket(
        _project(), "t", "5",
        labels_add=["bug"], labels_remove=["p2"],
    )
    assert captured["body"]["add_labels"] == "bug"
    assert captured["body"]["remove_labels"] == "p2"


def test_update_ticket_assignee_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and "issues/5" in path:
            return _json(_issue_payload(
                5, labels=["ai-generated"],
                assignees=[{"username": "alice"}],
            ))
        if req.method == "GET" and path == "/api/v4/users":
            username = req.url.params.get("username")
            users = {"alice": 1, "bob": 2}
            if username in users:
                return _json([{"id": users[username], "username": username}])
            return _json([])
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_issue_payload(5))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_ticket(
        _project(), "t", "5",
        assignees_add=["bob"], assignees_remove=["alice"],
    )
    # Final list: alice removed, bob added → just [bob] → id=[2].
    assert captured["body"]["assignee_ids"] == [2]


def test_update_ticket_no_changes_returns_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No fields changed AND ticket is ai-generated (so no ai-modified
    marker to inject) → no PUT, return the current snapshot."""
    puts: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return _json(_issue_payload(5, labels=["ai-generated"]))
        if req.method == "PUT":
            puts.append(req)
            return _json({}, status_code=500)
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    ticket = GitLabProvider().update_ticket(_project(), "t", "5")
    assert ticket.id == "5"
    assert puts == []


# ---------- comments / notes -------------------------------------------------


def test_add_comment_applies_marker_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            captured["body"] = json.loads(req.content.decode())
            return _json({
                "id": 99, "body": captured["body"]["body"],
                "author": {"username": "alice"},
                "created_at": "2024-01-01T00:00:00Z",
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    c = GitLabProvider().add_comment(_project(), "t", "5", "comment text")
    assert c.id == "99"
    assert captured["body"]["body"].startswith("#ai-generated")


def test_list_comments_filters_system_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json([
            {
                "id": 1, "body": "user comment", "system": False,
                "author": {"username": "a"}, "created_at": "2024-01-01T00:00:00Z",
            },
            {
                "id": 2, "body": "label added", "system": True,
                "author": {"username": "a"}, "created_at": "2024-01-01T00:01:00Z",
            },
        ])

    _install_mock(monkeypatch, handler)
    comments = GitLabProvider().list_comments(_project(), "t", "5")
    assert len(comments) == 1
    assert comments[0].body == "user comment"


def test_get_comment_composite_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert "acme%2Fbackend/issues/5/notes/99" in str(req.url)
        return _json({
            "id": 99, "body": "x",
            "author": {"username": "a"}, "created_at": "2024-01-01T00:00:00Z",
        })

    _install_mock(monkeypatch, handler)
    c = GitLabProvider().get_comment(_project(), "t", comment_id="5/99")
    assert c.id == "99"


def test_get_comment_plain_id_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain note ids aren't addressable without context — must surface
    a clear error so the caller migrates to the composite form."""
    _install_mock(monkeypatch, lambda r: _json({}, 200))
    with pytest.raises(GitLabError, match="issue_iid"):
        GitLabProvider().get_comment(_project(), "t", comment_id="99")


def test_update_comment_reapplies_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT":
            assert "acme%2Fbackend/issues/5/notes/99" in str(req.url)
            captured["body"] = json.loads(req.content.decode())
            return _json({
                "id": 99, "body": captured["body"]["body"],
                "author": {"username": "a"}, "created_at": "2024-01-01T00:00:00Z",
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_comment(_project(), "t", "5/99", "new content")
    assert captured["body"]["body"].startswith("#ai-generated")
    assert "new content" in captured["body"]["body"]
