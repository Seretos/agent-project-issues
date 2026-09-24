"""Driving tests for ticket #364: `create_pr` / `update_pr` refuse a `body`
longer than a verified provider limit (measured after the `#ai-*` marker
is applied) before any provider write, instead of letting the caller hit
an opaque provider error.

- R1: `create_pr` on a `github` project refuses a body whose
  marker-applied length exceeds 65536 characters, with `{"error": ...}`
  naming `github` and `65536`, and never calls the provider's
  `create_pr`. A body at or under the limit is forwarded unchanged.
  `gitlab`/`azuredevops` projects are not enforced (no verified limit) —
  the provider is always called, no error.
- R2: `update_pr` measures the marker flavour that will actually be
  applied — decided by whether the PR currently carries the
  `ai-generated` label (a `get_pr` read, which is itself never a write).
  Both flavours fitting or both being over decide the outcome without a
  read; only when the flavours straddle the boundary does the helper
  call `get_pr` to learn which one applies.
- R3: `create_pr` and `update_pr`'s docstrings state each verified
  provider's limit and say every unverified provider is not validated.
  R3's test cross-checks the docstrings against the actual runtime
  constant `_PR_BODY_MAX_CHARS` (not a hard-coded `"65536"` literal), so
  it fails if the documented number ever drifts from what the code
  enforces (test-critic round 2 finding: a bare string-presence check on
  docs proves nothing about what's actually enforced).

Harness mirrors `tests/test_314_write_response_light.py`: a `_StubMCP` +
monkeypatched `_providers.load_projects` + `_PROVIDERS[...]` substitution,
with a recording fake provider standing in for the real one.

Expected RED reason:
- R1/R2: there is no pre-check yet on the current code, so the fake
  provider is called (and no `{"error": ...}` is returned) where the test
  expects a refusal.
- R3: `_PR_BODY_MAX_CHARS` does not exist yet in `_providers.py` —
  importing it raises `ImportError`, a valid RED for a not-yet-written
  constant (mirrors the `_with_auth_hint` ImportError-RED pattern in
  `tests/test_266_error_tone_actionability.py`).
"""
from __future__ import annotations

import re
from typing import Any, Callable

import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects import markers as markers_mod
from lib_python_projects.providers.base import PullRequest
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import pulls as pull_tools


# ---------- marker-length helpers ---------------------------------------------


def _marker_prefix_len(will_be_ai_generated: bool, marker_set=None) -> int:
    """Length of the marker prefix `apply_body_marker` adds, for a
    non-empty body (the empty-body case omits the trailing blank line,
    which would throw off a simple `len(prefix)` calculation)."""
    kwargs = {} if marker_set is None else {"markers": marker_set}
    return len(
        markers_mod.apply_body_marker("x", will_be_ai_generated=will_be_ai_generated, **kwargs)
    ) - 1


def _body_of_flavour_length(
    target_len: int, *, will_be_ai_generated: bool, marker_set=None
) -> str:
    """A raw body whose marker-applied length (for the given flavour) is
    exactly `target_len`."""
    prefix_len = _marker_prefix_len(will_be_ai_generated, marker_set)
    n = target_len - prefix_len
    assert n >= 1, f"target_len {target_len} too small for prefix {prefix_len}"
    return "x" * n


# ---------- fixtures ------------------------------------------------------------


def _project(
    provider: str = "github",
    *,
    pulls_create: bool = True,
    pulls_modify: bool = True,
    auto_labels: dict | None = None,
) -> ProjectConfig:
    path = "acme/org/backend" if provider == "azuredevops" else "acme/backend"
    kwargs: dict[str, Any] = dict(
        id="acme",
        provider=provider,
        path=path,
        token_env="TEST_TOKEN_364",
        permissions={
            "issues": {"create": True, "modify": True},
            "pulls": {"create": pulls_create, "modify": pulls_modify, "merge": True},
        },
    )
    if auto_labels is not None:
        kwargs["auto_labels"] = auto_labels
    return ProjectConfig(**kwargs)


def _pr(body: str = "", labels: list[str] | None = None) -> PullRequest:
    return PullRequest(
        id="7",
        number=7,
        title="a pr",
        body=body,
        status="open",
        draft=False,
        author="alice",
        assignees=[],
        reviewers=[],
        requested_reviewers=[],
        labels=labels or [],
        head={"ref": "feature/x", "sha": "deadbeef", "repo_full_name": "acme/backend"},
        base={"ref": "main", "sha": "cafebabe"},
        merged=False,
        mergeable=None,
        url="https://example.test/pull/7",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


class _RecordingProvider:
    """Logs the bodies passed to `create_pr`/`update_pr` and counts
    `get_pr` calls (a read, never a write). `get_pr` returns a
    `PullRequest` carrying `labels` this test controls."""

    def __init__(self, labels: list[str] | None = None) -> None:
        self.create_calls: list[str] = []
        self.update_calls: list[str | None] = []
        self.get_pr_calls = 0
        self.labels = labels if labels is not None else []

    def create_pr(self, project, token, title, body, head, base, **kwargs):
        self.create_calls.append(body)
        return _pr(body=body)

    def update_pr(self, project, token, pr_id, **kwargs):
        self.update_calls.append(kwargs.get("body"))
        return _pr(body=kwargs.get("body") or "")

    def get_pr(self, project, token, pr_id):
        self.get_pr_calls += 1
        return _pr(body="", labels=list(self.labels)), []


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _make_tools(
    monkeypatch: pytest.MonkeyPatch, project: ProjectConfig, provider: _RecordingProvider,
) -> dict[str, Callable]:
    def fake_load_projects(*_a, **_k):
        return ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    if hasattr(pull_tools, "load_projects"):
        monkeypatch.setattr(pull_tools, "load_projects", fake_load_projects)
    monkeypatch.setenv("TEST_TOKEN_364", "tok")
    monkeypatch.setitem(providers_mod._PROVIDERS, project.provider, provider)

    stub = _StubMCP()
    pull_tools.register(stub)
    return stub.tools


def _pulls_tools() -> dict[str, Callable]:
    """Registered tools with no project/provider wiring — enough for the
    pure-docstring R3 check, which never calls `go()`."""
    stub = _StubMCP()
    pull_tools.register(stub)
    return stub.tools


# ---------- R1: create_pr -------------------------------------------------------


def test_create_pr_over_github_limit_refused_without_write(monkeypatch):
    """Driving test for R1."""
    project = _project("github")
    provider = _RecordingProvider()
    tools = _make_tools(monkeypatch, project, provider)

    body = _body_of_flavour_length(65537, will_be_ai_generated=True)
    out = tools["create_pr"](
        project_id="acme", title="t", body=body, head="feature/x", base="main",
    )

    assert "error" in out, out
    assert "github" in out["error"]
    assert "65536" in out["error"]
    assert provider.create_calls == []


def test_create_pr_at_github_limit_passes_with_raw_body_unchanged(monkeypatch):
    """Additional coverage: exactly at the limit passes, unmodified."""
    project = _project("github")
    provider = _RecordingProvider()
    tools = _make_tools(monkeypatch, project, provider)

    body = _body_of_flavour_length(65536, will_be_ai_generated=True)
    out = tools["create_pr"](
        project_id="acme", title="t", body=body, head="feature/x", base="main",
    )

    assert "error" not in out, out
    assert provider.create_calls == [body]


def test_create_pr_raw_65536_body_refused_proves_marker_is_counted(monkeypatch):
    """Additional coverage: a *raw* 65536-char body is over the limit once
    the marker is added — proves the check counts the marker, not just
    the raw input."""
    project = _project("github")
    provider = _RecordingProvider()
    tools = _make_tools(monkeypatch, project, provider)

    body = "x" * 65536
    out = tools["create_pr"](
        project_id="acme", title="t", body=body, head="feature/x", base="main",
    )

    assert "error" in out, out
    assert "github" in out["error"]
    assert "65536" in out["error"]
    assert provider.create_calls == []


def test_create_pr_longer_custom_ai_generated_label_moves_boundary(monkeypatch):
    """Additional coverage: a longer custom `auto_labels.ai_generated`
    lengthens the marker prefix, so a body that fits under the default
    label name is refused under the longer custom one."""
    project = _project(
        "github", auto_labels={"ai_generated": "x" * 100, "ai_modified": "ai-modified"},
    )
    provider = _RecordingProvider()
    tools = _make_tools(monkeypatch, project, provider)

    # Fits at exactly the limit under the *default* generated marker.
    body = _body_of_flavour_length(65536, will_be_ai_generated=True)
    out = tools["create_pr"](
        project_id="acme", title="t", body=body, head="feature/x", base="main",
    )

    assert "error" in out, out
    assert "github" in out["error"]
    assert "65536" in out["error"]
    assert provider.create_calls == []


@pytest.mark.parametrize("provider_name", ["gitlab", "azuredevops"])
def test_create_pr_unverified_provider_limit_is_a_no_op(monkeypatch, provider_name):
    """Additional coverage: GitLab / Azure DevOps have no verified limit
    in the table, so even a very long body is forwarded, unrefused."""
    project = _project(provider_name)
    provider = _RecordingProvider()
    tools = _make_tools(monkeypatch, project, provider)

    body = "x" * 200_000
    out = tools["create_pr"](
        project_id="acme", title="t", body=body, head="feature/x", base="main",
    )

    assert "error" not in out, out
    assert provider.create_calls == [body]


# ---------- R2: update_pr -------------------------------------------------------


def test_update_pr_over_limit_for_applied_flavour_refused(monkeypatch):
    """Driving test for R2: the PR carries the `ai-generated` label, so
    the generated flavour (65537, over) is what will be applied, even
    though the modified flavour (65536, at the limit) would have fit."""
    project = _project("github")
    provider = _RecordingProvider(labels=["ai-generated"])
    tools = _make_tools(monkeypatch, project, provider)

    body = _body_of_flavour_length(65537, will_be_ai_generated=True)
    assert len(
        markers_mod.apply_body_marker(body, will_be_ai_generated=False)
    ) == 65536, "fixture must straddle the boundary: modified flavour at the limit"

    out = tools["update_pr"](project_id="acme", pr_id="7", body=body)

    assert "error" in out, out
    assert "github" in out["error"]
    assert "65536" in out["error"]
    assert provider.get_pr_calls == 1
    assert provider.update_calls == []


def test_update_pr_same_body_without_ai_generated_label_uses_modified_flavour(
    monkeypatch,
):
    """Additional coverage: the very same body, but the PR does NOT carry
    the `ai-generated` label — the modified flavour (65536, at the limit)
    is what applies, and it fits, so the write proceeds with the raw
    body unchanged."""
    project = _project("github")
    provider = _RecordingProvider(labels=[])
    tools = _make_tools(monkeypatch, project, provider)

    body = _body_of_flavour_length(65537, will_be_ai_generated=True)
    out = tools["update_pr"](project_id="acme", pr_id="7", body=body)

    assert "error" not in out, out
    assert provider.get_pr_calls == 1
    assert provider.update_calls == [body]


def test_update_pr_custom_ai_generated_label_present_uses_generated_flavour(monkeypatch):
    """Additional coverage for R2, closing a tautology gap: the project
    uses a *custom* `auto_labels.ai_generated` value (not the default
    'ai-generated' string), and the fake PR's labels are built from that
    same custom value — never the literal default. The generated flavour
    (measured under the custom marker set) is over the limit; the
    modified flavour is at the limit. An implementation that hard-codes
    the literal 'ai-generated' string instead of reading
    `project.auto_labels.ai_generated` would look for 'ai-generated' in
    these labels, not find it, wrongly conclude the modified flavour
    applies, and let the over-limit write through unrefused — failing the
    assertions below. Only an implementation that actually reads
    `project.auto_labels.ai_generated` and finds it present passes."""
    # Same length as the default "ai-generated" (12 chars), so the
    # generated/modified prefix-length delta matches the driving test's
    # (1 char) and the same 65537/65536 straddle applies. Only the text
    # differs from the default, which is the point of this case.
    custom_generated_label = "custom-label"
    project = _project(
        "github",
        auto_labels={"ai_generated": custom_generated_label, "ai_modified": "ai-modified"},
    )
    marker_set = markers_mod.MarkerSet(custom_generated_label, "ai-modified")
    provider = _RecordingProvider(labels=[custom_generated_label])
    tools = _make_tools(monkeypatch, project, provider)

    body = _body_of_flavour_length(65537, will_be_ai_generated=True, marker_set=marker_set)
    assert len(
        markers_mod.apply_body_marker(body, will_be_ai_generated=False, markers=marker_set)
    ) == 65536, "fixture must straddle the boundary under the custom marker set"

    out = tools["update_pr"](project_id="acme", pr_id="7", body=body)

    assert "error" in out, out
    assert "github" in out["error"]
    assert "65536" in out["error"]
    assert provider.get_pr_calls == 1
    assert provider.update_calls == []


def test_update_pr_custom_ai_generated_label_absent_uses_modified_flavour_despite_default_literal(
    monkeypatch,
):
    """Additional coverage for R2, closing the other half of the same
    tautology gap: the project again uses a *custom*
    `auto_labels.ai_generated` value, but the fake PR's labels instead
    carry the literal default string 'ai-generated' — which is NOT the
    project's configured value, so it must NOT be read as "generated".
    The modified flavour (at the limit) is what actually applies here;
    the generated flavour is over. Both a hard-coded-'ai-generated'
    implementation (finds the literal present -> wrongly picks the
    generated/over flavour) and an "any label present" implementation
    (labels is non-empty -> wrongly picks the generated/over flavour)
    would refuse this write and fail the assertions below. Only an
    implementation that checks membership of the actual
    `project.auto_labels.ai_generated` value (absent here) concludes the
    modified flavour applies and lets the at-limit write through."""
    # Same length as the default "ai-generated" (12 chars) — see the
    # sibling test above for why the length is held constant.
    custom_generated_label = "custom-label"
    project = _project(
        "github",
        auto_labels={"ai_generated": custom_generated_label, "ai_modified": "ai-modified"},
    )
    marker_set = markers_mod.MarkerSet(custom_generated_label, "ai-modified")
    provider = _RecordingProvider(labels=["ai-generated"])
    tools = _make_tools(monkeypatch, project, provider)

    body = _body_of_flavour_length(65537, will_be_ai_generated=True, marker_set=marker_set)
    assert len(
        markers_mod.apply_body_marker(body, will_be_ai_generated=False, markers=marker_set)
    ) == 65536, "fixture must straddle the boundary under the custom marker set"

    out = tools["update_pr"](project_id="acme", pr_id="7", body=body)

    assert "error" not in out, out
    assert provider.get_pr_calls == 1
    assert provider.update_calls == [body]


def test_update_pr_over_limit_in_both_flavours_refused_without_read(monkeypatch):
    """Additional coverage: both flavours are over the limit, so the
    helper can refuse without needing to read the PR's current labels."""
    project = _project("github")
    provider = _RecordingProvider(labels=["ai-generated"])
    tools = _make_tools(monkeypatch, project, provider)

    body = _body_of_flavour_length(65538, will_be_ai_generated=True)
    assert len(
        markers_mod.apply_body_marker(body, will_be_ai_generated=False)
    ) == 65537, "fixture must keep both flavours over the limit"

    out = tools["update_pr"](project_id="acme", pr_id="7", body=body)

    assert "error" in out, out
    assert "github" in out["error"]
    assert "65536" in out["error"]
    assert provider.get_pr_calls == 0
    assert provider.update_calls == []


def test_update_pr_under_limit_in_both_flavours_passes_without_read(monkeypatch):
    """Additional coverage: both flavours fit, so the helper can pass
    without needing to read the PR's current labels."""
    project = _project("github")
    provider = _RecordingProvider(labels=["ai-generated"])
    tools = _make_tools(monkeypatch, project, provider)

    body = _body_of_flavour_length(65535, will_be_ai_generated=True)
    assert len(
        markers_mod.apply_body_marker(body, will_be_ai_generated=False)
    ) < 65536, "fixture must keep both flavours under the limit"

    out = tools["update_pr"](project_id="acme", pr_id="7", body=body)

    assert "error" not in out, out
    assert provider.get_pr_calls == 0
    assert provider.update_calls == [body]


def test_update_pr_body_none_with_title_change_never_refused(monkeypatch):
    """Additional coverage: `body=None` (title-only update) is a no-op for
    the length check — never refused, never reads the PR."""
    project = _project("github")
    provider = _RecordingProvider(labels=["ai-generated"])
    tools = _make_tools(monkeypatch, project, provider)

    out = tools["update_pr"](project_id="acme", pr_id="7", title="new title")

    assert "error" not in out, out
    assert provider.get_pr_calls == 0
    assert provider.update_calls == [None]


# ---------- R3: docstrings state the limits -------------------------------------


# Search term each provider key is expected to appear under in prose —
# `_PROVIDERS`' key is "azuredevops", but the docstrings (like the rest of
# this file's style, see test_359) write it out as "Azure DevOps".
_PROVIDER_DOC_TERMS = {"github": "github", "gitlab": "gitlab", "azuredevops": "azure"}


def test_pr_docstrings_state_body_limits():
    """Driving test for R3: both `create_pr` and `update_pr`'s docstrings
    state each verified provider's limit and say every unverified
    provider is not validated — checked against the actual runtime
    constant `_PR_BODY_MAX_CHARS`, not a hard-coded literal.

    A regex over prose can never prove the documented number is the
    number actually enforced: a docstring could contain the right tokens
    near each other while the code enforces something else (or nothing).
    Importing `_PR_BODY_MAX_CHARS` and asserting against its real value
    closes that gap — this test fails if the docstring's stated number
    ever drifts from what the code enforces, not just if the prose is
    missing.

    RED today: `_PR_BODY_MAX_CHARS` does not exist yet in `_providers.py`
    (production code isn't written in this tests-only phase), so the
    import below raises `ImportError`.
    """
    from project_issues_plugin.tools._providers import _PR_BODY_MAX_CHARS

    verified = dict(_PR_BODY_MAX_CHARS)
    unverified = [p for p in providers_mod._PROVIDERS if p not in verified]
    assert verified, (
        "expected _PR_BODY_MAX_CHARS to hold at least one verified "
        f"provider limit; got {_PR_BODY_MAX_CHARS!r}"
    )
    assert unverified, (
        "expected at least one provider with no verified limit; "
        f"_PR_BODY_MAX_CHARS covers all of {list(providers_mod._PROVIDERS)}"
    )

    tools = _pulls_tools()
    for name in ("create_pr", "update_pr"):
        doc = tools[name].__doc__ or ""
        for provider, limit in verified.items():
            term = _PROVIDER_DOC_TERMS[provider]
            assert re.search(
                rf"{term}[^.]{{0,200}}{limit}|{limit}[^.]{{0,200}}{term}",
                doc, re.I | re.S,
            ), (
                f"{name}: docstring's {limit}-char limit not attributed to "
                f"{provider} (runtime value from _PR_BODY_MAX_CHARS):\n{doc}"
            )
        for provider in unverified:
            term = _PROVIDER_DOC_TERMS[provider]
            assert re.search(
                rf"{term}[^.]{{0,200}}not validated|not validated[^.]{{0,200}}{term}",
                doc, re.I | re.S,
            ), f"{name}: docstring doesn't say {provider} isn't validated:\n{doc}"
