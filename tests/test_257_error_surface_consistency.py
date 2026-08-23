"""Tests for ticket #257 — schema / error-message consistency sweep.

The ticket bundles four E2E findings. Findings 1 (`update_label.name`
schema mismatch, #249) and 4 (GitLab 404 doubled provider/status suffix,
#251) are already fixed by prior tickets; this file adds verification-only
regression coverage for them (expected to pass immediately, no RED
required). Finding 3 (`add_relation` circular-error leak) is covered by
the `lib-python-projects` v0.3.12 pin bump and already has dedicated
coverage in `tests/test_259_lib_projects_pin.py` — not duplicated here.

Finding 2 is the one real bug: `ensure_board_column`'s gate order in
`tools/tickets.py::go()` currently checks `_require_board_manage` and
`_require_token` BEFORE the `hasattr(provider, "ensure_board_column")`
capability check, so GitLab (which has no board concept) surfaces a
misleading `board.manage` permission error or a "no API token" error
instead of a clear "provider does not support board management" error.
The fix reorders the gates so the capability check runs first.

Mirrors the `_StubMCP` + `_project(...)` + `_register(...)` scaffolding
from `tests/test_230_ensure_board_column.py` (the exact template for this
tool), extended with a `set_token` switch so the no-token gate-ordering
variant is expressible.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Callable

import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers.base import BoardColumnSpec
from lib_python_projects.providers.gitlab import GitLabError
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import comments as comment_tools
from project_issues_plugin.tools import labels as label_tools
from project_issues_plugin.tools import tickets as ticket_tools


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _project(
    project_id: str = "acme",
    provider: str = "github",
    *,
    board_manage: bool | None = False,
    no_board_block: bool = False,
) -> ProjectConfig:
    path = "myorg/myproject/myrepo" if provider == "azuredevops" else f"{project_id}/backend"
    permissions: dict = {
        "issues": {"create": True, "modify": True},
        "pulls": {"create": True, "modify": True, "merge": True},
    }
    if not no_board_block:
        permissions["board"] = {"manage": bool(board_manage)}
    return ProjectConfig(
        id=project_id,
        provider=provider,
        path=path,
        token_env=f"TOKEN_{project_id.upper()}",
        permissions=permissions,
    )


def _register(
    monkeypatch: pytest.MonkeyPatch,
    provider_instance,
    project: ProjectConfig,
    *,
    set_token: bool = True,
) -> dict[str, Callable]:
    """Registers `tickets.py`'s tools with a fake project/provider.

    `set_token=False` deletes the token env var (rather than simply not
    setting it) so the test is robust to a stray value leaking in from the
    real environment.
    """
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setitem(providers_mod._PROVIDERS, project.provider, provider_instance)
    token_env = f"TOKEN_{project.id.upper()}"
    if set_token:
        monkeypatch.setenv(token_env, "tok")
    else:
        monkeypatch.delenv(token_env, raising=False)

    stub = _StubMCP()
    ticket_tools.register(stub)
    return stub.tools


class _MockEnsureBoardProvider:
    """Mirrors `_MockEnsureBoardProvider` in test_230_ensure_board_column.py."""

    def __init__(self, result=None) -> None:
        self._result = result
        self.called_with: tuple | None = None

    def ensure_board_column(self, project_, token, column_name):
        self.called_with = (project_, token, column_name)
        return self._result


class _NoBoardSupportProvider:
    """A provider stub with no `ensure_board_column` method at all —
    mirrors GitLab's lack of a board concept."""


# ---------------------------------------------------------------------------
# Behavioural requirement 1 — capability check precedes the board.manage gate.
# ---------------------------------------------------------------------------


def test_gitlab_unsupported_error_wins_over_board_manage_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test. On current code `_require_board_manage` runs before the
    capability check, so a GitLab project with `board_manage=False` raises
    `PermissionError("project 'acme' does not permit managing board
    columns...")` instead of the NotImplementedError — 'not support' is
    absent and 'board.manage' is present. Expected RED for that reason."""
    project = _project(provider="gitlab", board_manage=False)
    tools = _register(monkeypatch, _NoBoardSupportProvider(), project)

    out = tools["ensure_board_column"](project_id="acme", column_name="Approved")

    assert "error" in out, f"expected error dict; got: {out}"
    assert "not support" in out["error"].lower(), (
        f"expected the unsupported-provider error to win; got: {out['error']!r}"
    )
    assert "board.manage" not in out["error"], (
        f"board.manage permission error leaked through instead: {out['error']!r}"
    )


def test_gitlab_unsupported_error_wins_when_no_board_permissions_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additional edge-case coverage for requirement 1: no `permissions.board`
    block at all resolves to the same manage=False default. Also RED today,
    same reason as the driving test above."""
    project = _project(provider="gitlab", no_board_block=True)
    tools = _register(monkeypatch, _NoBoardSupportProvider(), project)

    out = tools["ensure_board_column"](project_id="acme", column_name="Approved")

    assert "error" in out, f"expected error dict; got: {out}"
    assert "not support" in out["error"].lower()
    assert "board.manage" not in out["error"]


def test_gitlab_unsupported_error_when_board_manage_true_no_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Already passes today (this is test_230's existing coverage) — pinned
    here as an explicit no-regression baseline for requirement 1's fix."""
    project = _project(provider="gitlab", board_manage=True)
    tools = _register(monkeypatch, _NoBoardSupportProvider(), project)

    out = tools["ensure_board_column"](project_id="acme", column_name="Approved")

    assert "error" in out, f"expected error dict; got: {out}"
    assert "not support" in out["error"].lower()


# ---------------------------------------------------------------------------
# Behavioural requirement 2 — capability check precedes the token gate.
# ---------------------------------------------------------------------------


def test_gitlab_unsupported_error_wins_over_token_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test. On current code `_require_token` runs before the
    capability check, so a GitLab project with no token env var raises
    `PermissionError("project 'acme' has no API token available (env var
    'TOKEN_ACME' is unset)...")` instead of the NotImplementedError.
    Expected RED for that reason."""
    project = _project(provider="gitlab", board_manage=True)
    tools = _register(
        monkeypatch, _NoBoardSupportProvider(), project, set_token=False,
    )

    out = tools["ensure_board_column"](project_id="acme", column_name="Approved")

    assert "error" in out, f"expected error dict; got: {out}"
    assert "not support" in out["error"].lower(), (
        f"expected the unsupported-provider error to win; got: {out['error']!r}"
    )
    assert "no API token" not in out["error"], (
        f"missing-token permission error leaked through instead: {out['error']!r}"
    )


def test_gitlab_unsupported_error_wins_when_neither_permission_nor_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additional edge-case coverage for requirement 2: GitLab with neither
    board.manage nor a token still surfaces the capability error first."""
    project = _project(provider="gitlab", board_manage=False)
    tools = _register(
        monkeypatch, _NoBoardSupportProvider(), project, set_token=False,
    )

    out = tools["ensure_board_column"](project_id="acme", column_name="Approved")

    assert "error" in out, f"expected error dict; got: {out}"
    assert "not support" in out["error"].lower()


# ---------------------------------------------------------------------------
# Behavioural requirement 3 — board-capable providers unaffected
# (no-regression pins; all already pass today and must keep passing).
# ---------------------------------------------------------------------------


def test_github_denied_when_manage_false_no_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _MockEnsureBoardProvider(result=True)
    project = _project(board_manage=False)
    tools = _register(monkeypatch, provider, project)

    out = tools["ensure_board_column"](project_id="acme", column_name="Approved")

    assert "error" in out, f"expected error dict; got: {out}"
    assert "board.manage" in out["error"]
    assert provider.called_with is None


def test_github_denied_when_token_missing_no_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _MockEnsureBoardProvider(result=True)
    project = _project(board_manage=True)
    tools = _register(monkeypatch, provider, project, set_token=False)

    out = tools["ensure_board_column"](project_id="acme", column_name="Approved")

    assert "error" in out, f"expected error dict; got: {out}"
    assert "no API token" in out["error"]
    assert provider.called_with is None


def test_github_dispatches_when_manage_true_and_token_present_no_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _MockEnsureBoardProvider(result=True)
    project = _project(board_manage=True)
    tools = _register(monkeypatch, provider, project)

    out = tools["ensure_board_column"](project_id="acme", column_name="Approved")

    assert "error" not in out, f"unexpected error: {out}"
    assert provider.called_with == (project, "tok", "Approved")
    assert out["created"] is True


def test_azure_column_payload_shape_intact_no_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = BoardColumnSpec(
        logical="Approved", native="Approved", option_id="col-2",
        states=("Approved",), is_split=False,
    )
    provider = _MockEnsureBoardProvider(result=spec)
    project = _project(provider="azuredevops", board_manage=True)
    tools = _register(monkeypatch, provider, project)

    out = tools["ensure_board_column"](project_id="acme", column_name="Approved")

    assert "error" not in out, f"unexpected error: {out}"
    assert out["column"] == {
        "logical": "Approved", "native": "Approved", "option_id": "col-2",
        "states": ["Approved"], "is_split": False,
    }


def test_fidelity_pin_gitlab_lacks_capability_github_has_it() -> None:
    """Pins the real (non-monkeypatched) pinned-lib providers' shape: GitLab
    genuinely has no `ensure_board_column` method, GitHub does. Guards
    against the fake `_NoBoardSupportProvider` stub above drifting from
    the real lib's actual capability surface."""
    assert not hasattr(providers_mod._PROVIDERS["gitlab"], "ensure_board_column")
    assert hasattr(providers_mod._PROVIDERS["github"], "ensure_board_column")


# ---------------------------------------------------------------------------
# Behavioural requirement 4 — documentation matches the new precedence.
# ---------------------------------------------------------------------------

# Exact substring the fix's docstring edit must introduce. Chosen to be
# concrete and unambiguous per the plan-critic's note that "e.g./near"
# wording was under-specified.
_DOCSTRING_PRECEDENCE_CLAIM = (
    "the unsupported-provider error is returned regardless of the "
    "project's `board.manage` permission or whether a token is configured"
)

# Exact substring the fix's SKILL.md edit must introduce, near the
# existing `ensure_board_column` paragraph.
_SKILL_PRECEDENCE_CLAIM = (
    "on GitLab the call reports the provider as unsupported before any "
    "permission check"
)


def test_ensure_board_column_docstring_states_gitlab_precedence() -> None:
    """Driving test. Expected RED today: the current docstring documents
    GitLab's unsupported-provider error but says nothing about it taking
    precedence over the board.manage / token gates."""
    stub = _StubMCP()
    ticket_tools.register(stub)
    doc = stub.tools["ensure_board_column"].__doc__ or ""

    assert _DOCSTRING_PRECEDENCE_CLAIM in doc, (
        "expected the docstring to state the unsupported-provider "
        f"precedence claim verbatim; got:\n{doc}"
    )
    # Regression pin: test_194_board_docs.py::
    # test_ensure_board_column_docstring_mentions_board_manage still
    # requires this substring — the precedence sentence must be additive.
    assert "board.manage" in doc


def _skill_text() -> str:
    path = _repo_root() / "skills" / "project-issues" / "SKILL.md"
    assert path.is_file(), f"missing {path}"
    return path.read_text(encoding="utf-8")


def test_skill_md_states_gitlab_precedence_near_ensure_board_column() -> None:
    """Driving test. Expected RED today: SKILL.md's `ensure_board_column`
    paragraph documents the board.manage gate but says nothing about
    GitLab's unsupported-provider error taking precedence over it.

    Anchored on the detailed paragraph's own unique sentence (not the
    tool-listing bullet near the top of the file, which also mentions
    `ensure_board_column` in passing) so the window search finds the
    right prose to extend."""
    text = _skill_text()
    idx = text.index(
        "`ensure_board_column` can idempotently create a missing column"
    )
    window = text[idx: idx + 800]

    assert _SKILL_PRECEDENCE_CLAIM in window, (
        "expected the SKILL.md ensure_board_column paragraph to state the "
        f"GitLab precedence claim verbatim; got window:\n{window}"
    )
    # Regression pins for the existing SKILL.md hygiene guards in
    # test_239_bundled_skill.py — no internal ticket refs, no new
    # non-whitelisted backtick token (that file's
    # test_skill_mentions_only_real_tool_names already enforces the
    # latter globally; re-checking the no-ticket-ref guard here too since
    # it's cheap and this edit is the one introducing the new prose).
    assert not re.search(r"#\d+", text)


# ---------------------------------------------------------------------------
# Verification-only coverage for pre-fixed findings 1 and 4 — expected to
# pass immediately against current code. No RED needed; not driving tests.
# Finding 3 (add_relation circular-error leak / lib pin bump) already has
# dedicated exact-pin coverage in tests/test_259_lib_projects_pin.py and is
# not duplicated here.
# ---------------------------------------------------------------------------


class _GitLabGetComment404Provider:
    """Fake GitLab provider whose `get_comment` raises a raw 404, mirroring
    `_MockGitLabProvider404` in test_error_rewrap_251.py."""

    def get_comment(self, project, token, comment_id, *, ticket_id=None):
        raise GitLabError(404, "404 Not found")


def _register_comment_tools(
    monkeypatch: pytest.MonkeyPatch, provider_instance, project: ProjectConfig,
) -> dict[str, Callable]:
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setitem(providers_mod._PROVIDERS, project.provider, provider_instance)
    monkeypatch.setenv(f"TOKEN_{project.id.upper()}", "tok")

    stub = _StubMCP()
    comment_tools.register(stub)
    return stub.tools


def test_get_comment_gitlab_404_shape_already_fixed_by_251(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verification-only regression coverage for #257 finding 4 (already
    fixed by #251's `_rewrap_404`, see tests/test_error_rewrap_251.py).
    Expected to pass immediately — not a driving test, no RED run."""
    project = _project(provider="gitlab")
    tools = _register_comment_tools(
        monkeypatch, _GitLabGetComment404Provider(), project,
    )

    out = tools["get_comment"](project_id="acme", comment_id="12", ticket_id="7")

    assert "error" in out, f"expected error dict; got: {out}"
    assert out["error"] == "GitLab 404: comment 'acme#12' not found"
    assert out["error"].count("GitLab 404") == 1
    assert not out["error"].endswith(")")


def test_update_label_name_required_no_default_already_fixed_by_249() -> None:
    """Verification-only regression coverage for #257 finding 1 (already
    fixed by #249, full schema coverage in
    tests/test_249_update_label_name_required.py). Expected to pass
    immediately — not a driving test, no RED run."""
    stub = _StubMCP()
    label_tools.register(stub)
    sig = inspect.signature(stub.tools["update_label"])
    name_param = sig.parameters["name"]

    assert name_param.default is inspect.Parameter.empty, (
        f"expected 'name' to have no default; got {name_param.default!r}"
    )
    assert "None" not in str(name_param.annotation), (
        f"expected 'name' annotation to exclude None; got {name_param.annotation!r}"
    )
