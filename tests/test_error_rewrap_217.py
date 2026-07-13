"""Tests for ticket #217: `update_ticket(labels_add=["nonexistent-label"])`
against GitHub used to surface a misleading "ticket 'acme#5' not found"
error, because `_rewrap_404` clobbered ANY 404 into a ticket-not-found
message — including the distinguishable label-specific 404
(`GitHubError(404, "label '...' does not exist in <project.id>")`) already
raised by the lib's `_assert_labels_exist`.

Mirrors the fake-provider-raises pattern used in `test_error_rewrap_195.py`
— a mock provider is registered directly into `providers_mod._PROVIDERS` so
no HTTP mocking is needed.
"""
from __future__ import annotations

from typing import Callable

import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers.github import GitHubError
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import tickets as ticket_tools
from project_issues_plugin.tools._providers import _rewrap_404, _rewrap_label_404


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _ticket_project() -> ProjectConfig:
    from lib_python_projects import IssuesPermissions, Permissions
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
        permissions=Permissions(
            issues=IssuesPermissions(create=True, modify=True),
        ),
    )


def _register_ticket_tools_with_provider(
    monkeypatch: pytest.MonkeyPatch, provider_instance,
) -> dict[str, Callable]:
    project = _ticket_project()

    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=[project], state="ok", search_root="/tmp"
        )

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "ghp_token")
    monkeypatch.setitem(providers_mod._PROVIDERS, "github", provider_instance)

    stub = _StubMCP()
    ticket_tools.register(stub)
    return stub.tools


class _MockGitHubProviderLabel404:
    """Fake GitHub provider whose update_ticket raises the raw label-404,
    mirroring the lib's real `_assert_labels_exist` response body."""

    def __init__(self, message: str = "label 'nonexistent-label' does not exist in acme"):
        self._message = message

    def update_ticket(self, project, token, ticket_id, **kwargs):
        raise GitHubError(404, self._message)


class _MockGitHubProviderBadTicketId404:
    """Fake GitHub provider whose update_ticket raises a genuine
    bad-ticket-id 404, unrelated to labels."""

    def update_ticket(self, project, token, ticket_id, **kwargs):
        raise GitHubError(404, "Not Found")


# ---------------------------------------------------------------------------
# Regression test: label-404 must not be clobbered into "ticket not found"
# ---------------------------------------------------------------------------


def test_update_ticket_nonexistent_label_is_not_reported_as_ticket_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`update_ticket(labels_add=["nonexistent-label"])` must not surface
    the misleading "ticket 'acme#5' not found" message; it must name the
    offending label instead."""
    tools = _register_ticket_tools_with_provider(
        monkeypatch, _MockGitHubProviderLabel404(),
    )

    out = tools["update_ticket"](
        project_id="acme", ticket_id="5", labels_add=["nonexistent-label"],
    )

    assert "error" in out, f"expected error dict; got: {out}"
    message = out["error"]
    assert "nonexistent-label" in message
    assert "does not exist" in message
    assert "not found" not in message
    assert "acme#5" not in message


# ---------------------------------------------------------------------------
# Discrimination test: a genuine bad-ticket-id 404 keeps its old message
# ---------------------------------------------------------------------------


def test_update_ticket_bad_ticket_id_404_still_reports_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine bad-ticket-id 404 (unrelated to labels) is still
    rewrapped by `_rewrap_404` with the usual project/id "not found"
    message."""
    tools = _register_ticket_tools_with_provider(
        monkeypatch, _MockGitHubProviderBadTicketId404(),
    )

    out = tools["update_ticket"](
        project_id="acme", ticket_id="5", labels_add=["nonexistent-label"],
    )

    assert "error" in out, f"expected error dict; got: {out}"
    message = out["error"]
    assert "acme#5" in message
    assert "not found" in message


# ---------------------------------------------------------------------------
# Co-existence: labels_add + assignees_add, label-404 wins the message
# ---------------------------------------------------------------------------


def test_update_ticket_label_404_wins_over_assignees_add_combo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call combining `labels_add` and `assignees_add` where the
    provider raises the label-404 still yields the label message (the
    422-assignee rewrap is inert on a 404)."""
    tools = _register_ticket_tools_with_provider(
        monkeypatch, _MockGitHubProviderLabel404(),
    )

    out = tools["update_ticket"](
        project_id="acme", ticket_id="5",
        labels_add=["nonexistent-label"], assignees_add=["someuser"],
    )

    assert "error" in out, f"expected error dict; got: {out}"
    message = out["error"]
    assert "nonexistent-label" in message
    assert "does not exist" in message


# ---------------------------------------------------------------------------
# Direct unit tests of the new helper
# ---------------------------------------------------------------------------


def test_rewrap_label_404_names_the_label() -> None:
    """Direct unit test: a matching label-404 returns a new exception
    naming the offending label."""
    exc = GitHubError(404, "label 'nonexistent-label' does not exist in acme")
    out = _rewrap_label_404(exc, labels_add=["nonexistent-label"])
    assert out is not exc
    assert "nonexistent-label" in out.message
    assert "does not exist" in out.message


def test_rewrap_label_404_passes_through_non_matching_404() -> None:
    """Direct unit test: a 404 that doesn't mention a nonexistent label
    (e.g. a genuine "Not Found") passes through unchanged."""
    exc = GitHubError(404, "Not Found")
    out = _rewrap_label_404(exc, labels_add=["nonexistent-label"])
    assert out is exc


def test_rewrap_label_404_passes_through_non_404() -> None:
    """Direct unit test: a non-404 error is untouched, even if its
    message happens to mention 'label' (status gate holds)."""
    exc = GitHubError(500, "label 'x' does not exist")
    out = _rewrap_label_404(exc, labels_add=["x"])
    assert out is exc


def test_rewrap_404_passes_through_label_404_message() -> None:
    """Direct unit test: `_rewrap_404` must NOT clobber a label-404
    message — locks the anti-clobber gate that lets `_rewrap_label_404`
    run instead."""
    exc = GitHubError(404, "label 'nonexistent-label' does not exist in acme")
    out = _rewrap_404(exc, project_id="acme", kind="ticket", ident="5")
    assert out is exc


def test_rewrap_label_404_unparseable_falls_back_to_input_list() -> None:
    """Edge case: a label-404 whose message can't be parsed into a single
    quoted name still yields a message naming the caller's `labels_add`
    input list."""
    exc = GitHubError(404, "label something does not exist somewhere")
    out = _rewrap_label_404(exc, labels_add=["nonexistent-label", "other-label"])
    assert out is not exc
    assert "nonexistent-label" in out.message
    assert "other-label" in out.message
    assert "do not exist" in out.message
