"""Tests for the AI-attribution marker behaviour on the GitHub provider.

Covers the three failure modes described in ticket
Seretos/agent-marketplace#15 and verifies the decisions taken on the plan:

  * D1 Option C — body-prefix is always applied, label is best-effort,
    silent label-drop after a successful POST emits a warning.
  * D2 Option B — `_ensure_label` hard-fails on 403 (the historical
    silent-log behaviour is replaced by a `GitHubError`), but
    `_ensure_label_best_effort` lets callers tolerate that failure.

The tests use `httpx.MockTransport` to intercept HTTP calls and assert
on the JSON payload of each request the provider issues.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from project_issues_plugin.config import ProjectConfig
from project_issues_plugin.providers import github as github_provider
from project_issues_plugin.providers.github import (
    GitHubError,
    GitHubProvider,
    _ensure_label,
    _ensure_label_best_effort,
    _label_present,
)


# ---------- helpers ----------------------------------------------------------


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        owner="acme",
        repo="backend",
        token_env="GITHUB_TOKEN_ACME",
    )


def _issue_payload(number: int, **overrides) -> dict:
    base = {
        "number": number,
        "title": f"Issue {number}",
        "body": "body",
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


def _json(payload, status_code: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
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


# ---------- _ensure_label hard-fail (D2 Option B) ---------------------------


def test_ensure_label_hard_fails_on_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 403 from `POST /labels` must raise `GitHubError` — the historical
    log-and-continue behaviour caused Mode B (the follow-up POST /issues
    then 403'd with the label still in the payload)."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/repos/acme/backend/labels":
            return _json(
                {
                    "message": "You do not have permission to create labels on "
                               "this repository.",
                },
                status_code=403,
            )
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    with github_provider._client("tok") as client:
        with pytest.raises(GitHubError) as exc_info:
            _ensure_label(client, _project(), "ai-generated")
    assert exc_info.value.status == 403


def test_ensure_label_returns_for_422_already_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """422 ("validation_failed: name already_exists") is idempotent success."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json(
            {"message": "Validation Failed", "errors": [{"code": "already_exists"}]},
            status_code=422,
        )

    _install_mock(monkeypatch, handler)
    with github_provider._client("tok") as client:
        # Must not raise.
        _ensure_label(client, _project(), "ai-generated")


def test_ensure_label_best_effort_returns_false_on_permission_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The best-effort wrapper swallows GitHubError and returns False so
    callers know to drop the label from subsequent payloads."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "no permission"}, status_code=403)

    _install_mock(monkeypatch, handler)
    with github_provider._client("tok") as client:
        result = _ensure_label_best_effort(client, _project(), "ai-generated")
    assert result is False


def test_ensure_label_best_effort_returns_true_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"name": "ai-generated"}, status_code=201)

    _install_mock(monkeypatch, handler)
    with github_provider._client("tok") as client:
        result = _ensure_label_best_effort(client, _project(), "ai-generated")
    assert result is True


# ---------- _label_present detection (D1 Option C — Mode A) -----------------


def test_label_present_finds_name_in_string_list():
    payload = {"labels": ["bug", "ai-generated"]}
    assert _label_present(payload, "ai-generated") is True


def test_label_present_finds_name_in_object_list():
    payload = {"labels": [{"name": "bug"}, {"name": "ai-generated"}]}
    assert _label_present(payload, "ai-generated") is True


def test_label_present_returns_false_when_dropped():
    """Mode A from ticket #15: GitHub returned 201 but stripped the label
    from the response payload (caller lacked `triage`)."""
    payload = {"labels": []}
    assert _label_present(payload, "ai-generated") is False


# ---------- create_ticket integration (Mode A + Mode B) ---------------------


def test_create_ticket_body_prefix_applied_when_label_create_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mode B reproduction: token can't create the label, but the issue
    must still be created and the body-prefix marker must land. The POST
    payload must NOT carry the `ai-generated` label (avoids the 403)."""

    captured_post: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path == "/repos/acme/backend/labels":
            return _json({"message": "no permission"}, status_code=403)
        if req.method == "POST" and path == "/repos/acme/backend/issues":
            captured_post["body"] = json.loads(req.content)
            return _json(_issue_payload(99, body=captured_post["body"]["body"]))
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    ticket = provider.create_ticket(
        _project(), "tok", title="t", body="hello world", labels=[], assignees=[],
    )
    assert ticket.id == "99"
    sent = captured_post["body"]
    assert sent["body"].startswith("#ai-generated\n\n")
    assert sent["body"].endswith("hello world")
    # Label MUST NOT be in the POST payload when _ensure_label_best_effort
    # returned False — otherwise we'd hit Mode B (403 on POST /issues).
    assert "labels" not in sent


def test_create_ticket_caller_labels_preserved_when_marker_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even when the AI marker can't land, an explicit caller-supplied
    label like `bug` should still be sent (the repo may have it already
    or accept it for the issue author)."""

    captured_post: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path == "/repos/acme/backend/labels":
            return _json({"message": "no permission"}, status_code=403)
        if req.method == "POST" and path == "/repos/acme/backend/issues":
            captured_post["body"] = json.loads(req.content)
            return _json(_issue_payload(100, body=captured_post["body"]["body"]))
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    provider.create_ticket(
        _project(), "tok", title="t", body="b", labels=["bug"], assignees=[],
    )
    sent = captured_post["body"]
    assert sent["labels"] == ["bug"]  # ai-generated dropped, bug retained


def test_create_ticket_warns_on_silent_label_drop(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mode A reproduction: label-create succeeds (or label already
    exists), but the response from POST /issues omits the label. We
    must log a warning so downstream tooling can spot the gap."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path == "/repos/acme/backend/labels":
            return _json({"name": "ai-generated"}, status_code=201)
        if req.method == "POST" and path == "/repos/acme/backend/issues":
            # GitHub stripped the label on the way out (Mode A).
            return _json(_issue_payload(101, labels=[]))
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    with caplog.at_level("WARNING", logger="project-issues.github"):
        provider.create_ticket(
            _project(), "tok", title="t", body="b", labels=[], assignees=[],
        )
    warned = " ".join(rec.getMessage() for rec in caplog.records)
    assert "ai-generated" in warned
    assert "silently dropped" in warned


def test_create_ticket_body_prefix_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling create_ticket with a body that already carries the marker
    must not produce a double prefix."""

    captured_post: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path == "/repos/acme/backend/labels":
            return _json({"name": "ai-generated"}, status_code=201)
        if req.method == "POST" and path == "/repos/acme/backend/issues":
            captured_post["body"] = json.loads(req.content)
            return _json(_issue_payload(102, body=captured_post["body"]["body"]))
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    provider.create_ticket(
        _project(),
        "tok",
        title="t",
        body="#ai-generated\n\nalready prefixed",
        labels=[],
        assignees=[],
    )
    assert (
        captured_post["body"]["body"]
        == "#ai-generated\n\nalready prefixed"
    )


# ---------- create_pr integration -------------------------------------------


def test_create_pr_body_prefix_applied_and_labels_skipped_on_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR body carries the marker; the follow-up labels POST is skipped
    entirely when there's nothing else to apply and the AI label was
    refused — avoids a needless API call that would also 403."""

    captured_pr_body: dict[str, object] = {}
    labels_post_seen = {"count": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path == "/repos/acme/backend/labels":
            return _json({"message": "no permission"}, status_code=403)
        if req.method == "POST" and path == "/repos/acme/backend/pulls":
            captured_pr_body["payload"] = json.loads(req.content)
            return _json(_pr_payload(200, body=captured_pr_body["payload"]["body"]))
        if (
            req.method == "POST"
            and path == "/repos/acme/backend/issues/200/labels"
        ):
            labels_post_seen["count"] += 1
            return _json([])
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    pr = provider.create_pr(
        _project(), "tok",
        title="t", body="describe", head="feat/x", base="main",
    )
    sent = captured_pr_body["payload"]
    assert sent["body"].startswith("#ai-generated\n\n")
    assert sent["body"].endswith("describe")
    assert pr.id == "200"
    assert labels_post_seen["count"] == 0  # labels POST skipped


def test_create_pr_warns_on_silent_label_drop(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path == "/repos/acme/backend/labels":
            return _json({"name": "ai-generated"}, status_code=201)
        if req.method == "POST" and path == "/repos/acme/backend/pulls":
            return _json(_pr_payload(201))
        if (
            req.method == "POST"
            and path == "/repos/acme/backend/issues/201/labels"
        ):
            # Mode A: response omits the label we asked for.
            return _json([])
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    with caplog.at_level("WARNING", logger="project-issues.github"):
        provider.create_pr(
            _project(), "tok",
            title="t", body="b", head="feat/x", base="main",
        )
    warned = " ".join(rec.getMessage() for rec in caplog.records)
    assert "ai-generated" in warned
    assert "silently dropped" in warned


# ---------- update_ticket: ai-modified is best-effort -----------------------


def test_update_ticket_proceeds_without_ai_modified_when_label_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the caller can't create the `ai-modified` label, the PATCH
    must still go through. The label simply isn't in the payload."""

    captured_patch: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/repos/acme/backend/issues/5":
            # Existing ticket — NOT AI-generated, so update_ticket would
            # normally try to add `ai-modified`.
            return _json(_issue_payload(5, labels=[]))
        if req.method == "POST" and path == "/repos/acme/backend/labels":
            return _json({"message": "no permission"}, status_code=403)
        if req.method == "PATCH" and path == "/repos/acme/backend/issues/5":
            captured_patch["body"] = json.loads(req.content)
            return _json(_issue_payload(5, title="new"))
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    ticket = provider.update_ticket(
        _project(), "tok", "5", title="new",
    )
    assert ticket.id == "5"
    body = captured_patch["body"]
    # title is patched; `labels` must NOT contain ai-modified (best-effort
    # dropped it) — and since current labels were [] and we couldn't add
    # the marker, no labels diff exists at all.
    assert body.get("title") == "new"
    assert "ai-modified" not in (body.get("labels") or [])
