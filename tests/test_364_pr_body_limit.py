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
- R3 (generation 2 replan): `create_pr` and `update_pr`'s docstrings are
  RENDERED from the same `_PR_BODY_MAX_CHARS` table the enforcement
  helper reads — via `_render_pr_body_limit_doc()` / a
  `_with_pr_body_limit_doc` decorator applied at `register()` time —
  never hand-written prose that could drift from the runtime check.
  `test_limit_doc_and_enforcement_follow_same_table` proves this by
  swapping the table for a synthetic one (`{"gitlab": 100}`) via
  `monkeypatch.setattr` and re-registering the tools on a fresh stub:
  both docstrings must then show gitlab's swapped limit and say
  github/azuredevops are "not validated" now that they dropped out of
  the table, AND the enforcement itself must follow the SAME swapped
  table (a gitlab body at 100 chars passes, at 101 is refused with
  nothing sent, and a large github body — now outside the table —
  passes unenforced). A hard-coded paragraph, or a renderer/helper
  reading a different table than the one actually swapped in, fails
  this test either way — there is no trusted intermediary between
  documented prose, enforcement, and observed behaviour.

Harness mirrors `tests/test_314_write_response_light.py`: a `_StubMCP` +
monkeypatched `_providers.load_projects` + `_PROVIDERS[...]` substitution,
with a recording fake provider standing in for the real one.

Expected RED reason:
- R1/R2: there is no pre-check yet on the current code, so the fake
  provider is called (and no `{"error": ...}` is returned) where the test
  expects a refusal.
- R3: `_PR_BODY_MAX_CHARS` does not exist anywhere in `_providers.py`
  yet (no production code for this ticket has landed at all), so
  `monkeypatch.setattr(providers_mod, "_PR_BODY_MAX_CHARS", {"gitlab":
  100})` fails immediately with a clear `AttributeError` naming the
  missing attribute — not a confusing crash further into the test body.
"""
from __future__ import annotations

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


# The single source of truth for "which providers have no verified limit
# in the REAL (unmodified) `_PR_BODY_MAX_CHARS` table, so the helper is a
# no-op for them". Parametrizes the behavioural no-op tests below (R1's
# test_create_pr_unverified_provider_limit_is_a_no_op and R2's
# test_update_pr_unverified_provider_limit_is_a_no_op). Not used by R3
# (generation 2) — R3 swaps the table itself, so which providers count
# as "unverified" changes with it; see
# test_limit_doc_and_enforcement_follow_same_table.
_UNVERIFIED_PROVIDERS = ("gitlab", "azuredevops")


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


@pytest.mark.parametrize("provider_name", _UNVERIFIED_PROVIDERS)
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


@pytest.mark.parametrize("provider_name", _UNVERIFIED_PROVIDERS)
def test_update_pr_unverified_provider_limit_is_a_no_op(monkeypatch, provider_name):
    """Additional coverage, mirroring R1's
    `test_create_pr_unverified_provider_limit_is_a_no_op` but through
    `update_pr`: GitLab / Azure DevOps have no verified limit in the
    table, so even a very long body is forwarded, unrefused. This is the
    behavioural counterpart the test-critic flagged as missing — R3's
    docstring assertions alone only check *prose*, and would pass even if
    a future implementation mistakenly enforced the github limit against
    gitlab/azuredevops on this call path. This test proves the no-op
    behaviourally for `update_pr` specifically, not just for `create_pr`.

    The unverified-provider branch is a no-op at the table-lookup stage,
    before any flavour selection, so it never needs to read the PR's
    labels either — `get_pr_calls` stays at 0."""
    project = _project(provider_name)
    provider = _RecordingProvider(labels=["ai-generated"])
    tools = _make_tools(monkeypatch, project, provider)

    body = "x" * 200_000
    out = tools["update_pr"](project_id="acme", pr_id="7", body=body)

    assert "error" not in out, out
    assert provider.get_pr_calls == 0
    assert provider.update_calls == [body]


# ---------- R3: docs and enforcement share one table (generation 2) -------------


def test_limit_doc_and_enforcement_follow_same_table(monkeypatch):
    """Driving test for R3 (generation 2 replan): `create_pr` /
    `update_pr`'s "Body size limit" docstring paragraph is RENDERED from
    the same `_PR_BODY_MAX_CHARS` table the enforcement helper reads
    (`_render_pr_body_limit_doc()`, applied at `register()` time via a
    `_with_pr_body_limit_doc` decorator mirroring `pipelines.py`'s
    `_with_run_vocabulary`) — never hand-written prose that could drift
    from the runtime check.

    Proof: swap `_PR_BODY_MAX_CHARS` for a synthetic table
    (`{"gitlab": 100}`, deliberately NOT the real table) and re-register
    the tools on a fresh stub so the decorator re-renders against the
    swapped table. Both the docstrings AND the enforcement must follow
    that swap together:
      - both docstrings state gitlab's new 100-char limit and say
        github/azuredevops are "not validated" now that they dropped out
        of the table (they were the *enforced* one before the swap);
      - a gitlab body whose marker-applied length is 101 is refused with
        nothing sent to the provider;
      - the same body at exactly 100 chars passes, forwarded unchanged;
      - a large (200_000-char) github body — no longer in the swapped
        table — passes completely unenforced.
    A hard-coded paragraph fails the docstring assertions (it can't know
    about "gitlab: 100"); a renderer/helper that reads a different table
    than the one actually swapped in fails the behavioural assertions
    either way. There is no trusted intermediary between documented
    prose, enforcement, and observed behaviour.

    RED today: `_PR_BODY_MAX_CHARS` does not exist anywhere in
    `_providers.py` yet — no production code for ticket #364 has landed
    in this generation at all — so `monkeypatch.setattr(providers_mod,
    "_PR_BODY_MAX_CHARS", {"gitlab": 100})` fails immediately with a
    clear `AttributeError` naming the missing attribute, before any
    docstring or boundary-body assertion even runs.
    """
    monkeypatch.setattr(providers_mod, "_PR_BODY_MAX_CHARS", {"gitlab": 100})

    # Re-register on a fresh stub so `_with_pr_body_limit_doc` re-renders
    # each tool's docstring against the swapped table.
    tools = _pulls_tools()

    for name in ("create_pr", "update_pr"):
        doc = tools[name].__doc__ or ""
        assert "gitlab" in doc.lower() and "100" in doc, (
            f"{name}: expected the docstring to state gitlab's swapped "
            f"limit (100 characters) once _PR_BODY_MAX_CHARS is "
            f"{{'gitlab': 100}}:\n{doc}"
        )
        assert (
            "github" in doc.lower() and "not validated" in doc.lower()
        ), (
            f"{name}: expected the docstring to say github is not "
            f"validated now that it dropped out of the swapped table:\n{doc}"
        )
        assert (
            "azure" in doc.lower()
            and doc.lower().count("not validated") >= 2
        ), (
            f"{name}: expected the docstring to say azuredevops is not "
            f"validated too (two 'not validated' providers: github and "
            f"azuredevops):\n{doc}"
        )
        # Proves `_with_pr_body_limit_doc` is applied to BOTH tools, and
        # that it appends the renderer's own live output verbatim rather
        # than a separately hand-typed paragraph that merely happens to
        # look similar.
        assert providers_mod._render_pr_body_limit_doc() in doc, (
            f"{name}: docstring does not contain "
            "_render_pr_body_limit_doc()'s current output verbatim — a "
            f"hand-written paragraph would silently drift from the "
            f"table:\n{doc}"
        )

    # ---- enforcement follows the SAME swapped table --------------------

    gitlab_project = _project("gitlab")
    gitlab_provider = _RecordingProvider()
    gitlab_tools = _make_tools(monkeypatch, gitlab_project, gitlab_provider)

    at_limit = _body_of_flavour_length(100, will_be_ai_generated=True)
    out = gitlab_tools["create_pr"](
        project_id="acme", title="t", body=at_limit, head="feature/x", base="main",
    )
    assert "error" not in out, (
        f"swapped table says gitlab's limit is 100, but a body of "
        f"exactly that length was refused: {out}"
    )
    assert gitlab_provider.create_calls == [at_limit]

    over_limit = _body_of_flavour_length(101, will_be_ai_generated=True)
    out = gitlab_tools["create_pr"](
        project_id="acme", title="t", body=over_limit, head="feature/x", base="main",
    )
    assert "error" in out, (
        f"swapped table says gitlab's limit is 100, but a 101-char body "
        f"was accepted: {out}"
    )
    assert "gitlab" in out["error"]
    assert "100" in out["error"]
    assert gitlab_provider.create_calls == [at_limit]  # no second entry appended

    # A provider that dropped OUT of the swapped table (github) is a
    # complete no-op, even for a body far larger than the old real limit.
    github_project = _project("github")
    github_provider = _RecordingProvider()
    github_tools = _make_tools(monkeypatch, github_project, github_provider)

    huge_body = "x" * 200_000
    out = github_tools["create_pr"](
        project_id="acme", title="t", body=huge_body, head="feature/x", base="main",
    )
    assert "error" not in out, (
        f"github dropped out of the swapped table ({{'gitlab': 100}}), "
        f"so it must be unenforced, but the call was refused: {out}"
    )
    assert github_provider.create_calls == [huge_body]


def test_pr_docstrings_contain_rendered_limit_doc_with_real_table():
    """Additional coverage for R3: with the REAL (unmodified)
    `_PR_BODY_MAX_CHARS` table — not the synthetic swap the driving test
    above uses — both `create_pr` and `update_pr`'s docstrings still
    contain `_render_pr_body_limit_doc()`'s live output verbatim. This is
    the plan's edge case proving the decorator is wired to both tools
    under normal, non-test conditions; the swapped-table driving test
    above already proves the NUMBERS themselves track the table (R1
    proves the real 65536 number is correct).

    RED today: `_render_pr_body_limit_doc` does not exist in
    `_providers.py` yet, so this fails with a clear `AttributeError`
    before any docstring is even inspected.
    """
    tools = _pulls_tools()
    rendered = providers_mod._render_pr_body_limit_doc()
    for name in ("create_pr", "update_pr"):
        doc = tools[name].__doc__ or ""
        assert rendered in doc, (
            f"{name}: docstring does not contain the real table's "
            f"rendered 'Body size limit' paragraph verbatim:\n{doc}"
        )
