"""Behavioural driving tests for WP #307's issue-template enforcement gate:
a new read-only `list_ticket_templates(project_id)` tool, plus automatic
(non-configurable) template enforcement in `create_ticket` and in
`update_ticket` whenever a `body` is supplied.

Covers R1 (list_ticket_templates shape/skeleton/cache), R2 (create_ticket's
3 refusal states), R3 (conforming create_ticket write), R4 (update_ticket's
template resolution/gate), R5 (no-templates pass-through / listing-failure
fail-closed), R6 (create_ticket docstring swap), R7a-c (SKILL.md/README.md/
AGENTS.md documentation).

Two harness routes, per the plan (mirrors `tests/test_291_list_pr_files.py`):
  - Route A (contract fidelity) — a real `GitHubProvider` driven against a
    mocked `httpx` transport (`monkeypatch.setattr(github_provider,
    "_client", fake_client)`) serving a realistic GitHub issue-form YAML
    under `.github/ISSUE_TEMPLATE/`, so the lib's own
    `list_issue_templates` / `validate_ticket_body` / `render_skeleton` run
    for real.
  - Route B (control) — `_StubMCP` + `monkeypatch.setattr(providers_mod,
    "load_projects", ...)` + `monkeypatch.setitem(providers_mod._PROVIDERS,
    ...)` with a recording fake provider (mirrors `_FakeMergeProvider` /
    `_FakeLabelProvider` in `tests/test_265_client_side_validation.py`), for
    "no write happened" / cache-hit-count assertions and the
    capability-absent leg. Fake-provider `IssueTemplate` /
    `TemplateField` / `TemplateViolation` instances are never hand-defined:
    each is built from the dataclass imported from
    `lib_python_projects.templates` (same objects as
    `lib_python_projects.providers.base`, confirmed via introspection
    before writing this file), the convention of
    `tests/test_230_ensure_board_column.py:20` /
    `tests/test_291_list_pr_files.py:36` — a renamed/reshaped lib field
    then fails loudly at construction. Assertions target the tool's own
    fixed contract, never `dataclasses.fields()` of a lib type.

Implementation note for the phase=implement dispatch: the "pre-marker
validation" edge test below
(`test_create_ticket_validates_body_before_marker_prepended`) spy-wraps
`lib_python_projects.templates.validate_ticket_body` by patching that
module's attribute directly. For the spy to observe the call, `tools/
tickets.py` must call it as `templates.validate_ticket_body(...)` via a
qualified module-level reference (`from lib_python_projects import
templates`), not a rebound `from lib_python_projects.templates import
validate_ticket_body` — the latter binds a local name at import time that
a later `monkeypatch.setattr(templates_lib, "validate_ticket_body", ...)`
would not affect. This mirrors how `tests/test_statuses.py`'s
`test_list_ticket_statuses_caches_within_ttl` patches
`GitHubProvider.list_statuses` on the class (observable regardless of
import style, since it's a bound method call) rather than a free
function import.

Implementation note on `update_ticket`'s label-based template resolution:
"the ticket's current labels matching exactly one template's labels" is
read here as a SUBSET match (`set(template.labels) <= set(ticket.labels)`),
not exact set equality — a real ticket almost always also carries the
`ai-generated`/`ai-modified` marker label (and often others) alongside a
template's own labels, so exact equality would make label-based inference
practically unusable. `test_update_ticket_ambiguous_label_match_refuses_
with_full_list` below exercises the ambiguous case this produces (two
templates whose label sets are both subsets of the ticket's labels).

No e2e/live-provider harness exists anywhere in this repo (confirmed by
grep before writing this file) — this is not merely "no live HTTP was
exercised for this file" (true of every test file here), but specifically
that even if an e2e harness existed, it would need a templated fixture
repo (a real GitHub repo with `.github/ISSUE_TEMPLATE/*.yml` committed) to
exercise template discovery/validation end-to-end, and no such harness —
templated or otherwise — exists at all. Route A above is the closest
approximation available: a real `GitHubProvider` exercising the lib's
actual template-parsing/validation/rendering code, just against a mocked
transport instead of live HTTP.

Phase = tests: only RED driving tests + compile-level scaffolding here. No
production code (`tools/tickets.py`, `skills/project-issues/SKILL.md`,
`README.md`, `AGENTS.md`) is touched in this file.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects import templates as templates_lib
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers.base import Ticket
from lib_python_projects.providers.github import GitHubError
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import pulls as pulls_tools
from project_issues_plugin.tools import tickets as ticket_tools


_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Shared scaffolding
# ---------------------------------------------------------------------------


def _project(*, project_id: str = "acme", provider: str = "github") -> ProjectConfig:
    path = "myorg/myproject/myrepo" if provider == "azuredevops" else f"{project_id}/backend"
    return ProjectConfig(
        id=project_id,
        provider=provider,
        path=path,
        token_env=f"TOKEN_{project_id.upper()}",
        permissions={
            "issues": {"create": True, "modify": True},
            "pulls": {"create": False, "modify": False, "merge": False},
        },
    )


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _register_plain(module) -> dict[str, Callable]:
    """Register a tool module's tools with no provider/project mocking —
    for pure docstring/schema inspection (mirrors
    tests/test_284_docstring_front_loading.py's `_register`)."""
    stub = _StubMCP()
    module.register(stub)
    return stub.tools


def _register_tools_with(monkeypatch: pytest.MonkeyPatch, project: ProjectConfig):
    """Route A registration: real provider, HTTP-mocked via
    `github_provider._client` (patched separately by `_install_mock`)."""
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setenv(project.token_env, "tok")
    ticket_tools._status_cache_clear()

    stub = _StubMCP()
    ticket_tools.register(stub)
    return stub.tools


def _register_fake_provider_tools(
    monkeypatch: pytest.MonkeyPatch, project: ProjectConfig, provider_instance,
) -> dict[str, Callable]:
    """Route B registration: fake provider swapped into `_PROVIDERS`
    (mirrors `tests/test_230_ensure_board_column.py`'s `_register`)."""
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setitem(providers_mod._PROVIDERS, project.provider, provider_instance)
    monkeypatch.setenv(project.token_env, "tok")
    ticket_tools._status_cache_clear()

    stub = _StubMCP()
    ticket_tools.register(stub)
    return stub.tools


def _json(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def _install_mock(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Copied from tests/test_291_list_pr_files.py."""
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
            base_url=github_provider.API_BASE, headers=headers, transport=transport,
        )

    monkeypatch.setattr(github_provider, "_client", fake_client)
    return seen


# ---------- a realistic GitHub issue-form YAML fixture ("Bug Report") --------
#
# Two required fields (Description: textarea, Severity: dropdown) and one
# optional field (Steps to Reproduce: textarea) — enough to exercise
# missing/empty/invalid-option violations and the "optional field may be
# absent" pass-through, all via the lib's REAL validate_ticket_body.

_BUG_FORM_YAML = """\
name: Bug Report
description: File a bug report
title: "[Bug]: "
labels: ["bug", "triage"]
body:
  - type: textarea
    id: description
    attributes:
      label: Description
      description: What happened?
    validations:
      required: true
  - type: dropdown
    id: severity
    attributes:
      label: Severity
      options:
        - Low
        - High
    validations:
      required: true
  - type: textarea
    id: repro
    attributes:
      label: Steps to Reproduce
    validations:
      required: false
"""


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _template_content_response(owner_repo: str, req: httpx.Request) -> httpx.Response | None:
    """Serves the two GitHub contents-API calls `list_issue_templates`
    makes for the single `bug_report.yml` fixture above. Returns `None`
    for any other request so per-test handlers can chain their own
    routes after this one."""
    base = f"/repos/{owner_repo}"
    if req.method == "GET" and req.url.path == f"{base}/contents/.github/ISSUE_TEMPLATE":
        return _json([
            {"name": "bug_report.yml", "path": ".github/ISSUE_TEMPLATE/bug_report.yml", "type": "file"},
        ])
    if req.method == "GET" and req.url.path == f"{base}/contents/.github/ISSUE_TEMPLATE/bug_report.yml":
        return _json({"content": _b64(_BUG_FORM_YAML), "encoding": "base64"})
    return None


def _bug_template() -> templates_lib.IssueTemplate:
    """The same template the YAML fixture parses to, hand-built for
    Route B tests via the lib's own dataclasses (fidelity rule)."""
    return templates_lib.IssueTemplate(
        name="Bug Report",
        filename="bug_report.yml",
        title_prefix="[Bug]: ",
        labels=["bug", "triage"],
        kind="form",
        fields=[
            templates_lib.TemplateField(
                label="Description", field_id="description", type="textarea",
                required=True, options=None, description="What happened?",
                placeholder=None,
            ),
            templates_lib.TemplateField(
                label="Severity", field_id="severity", type="dropdown",
                required=True, options=["Low", "High"], description=None,
                placeholder=None,
            ),
            templates_lib.TemplateField(
                label="Steps to Reproduce", field_id="repro", type="textarea",
                required=False, options=None, description=None, placeholder=None,
            ),
        ],
    )


_TEMPLATE_VIEW_KEYS = {"name", "kind", "labels", "title_prefix", "required_sections", "skeleton"}


_TEMPLATE_SUMMARY_KEYS = _TEMPLATE_VIEW_KEYS - {"skeleton"}


def _assert_template_view_shape(entry: dict, *, with_skeleton: bool = True) -> None:
    """`with_skeleton=True` is the `list_ticket_templates` shape; refusal
    payloads carry the skeleton-free summary (#325)."""
    expected = _TEMPLATE_VIEW_KEYS if with_skeleton else _TEMPLATE_SUMMARY_KEYS
    assert set(entry.keys()) == expected, entry
    assert isinstance(entry["required_sections"], list)
    for rs in entry["required_sections"]:
        assert set(rs.keys()) == {"heading", "expected"}, rs
        assert isinstance(rs["expected"], str) and rs["expected"]


_REFUSAL_KEYS = {
    "state", "hint", "templates", "template", "violations",
    "skeleton", "received_sections", "written",
}


def _assert_refusal_shape(result: dict, *, expected_state: str) -> None:
    assert set(result.keys()) == _REFUSAL_KEYS, result
    assert result["written"] is False
    assert result["state"] == expected_state
    assert isinstance(result["templates"], list)
    assert isinstance(result["violations"], list)
    assert isinstance(result["received_sections"], list)
    assert isinstance(result["hint"], str) and result["hint"]
    for entry in result["templates"]:
        _assert_template_view_shape(entry, with_skeleton=False)


def _fake_ticket(*, id: str, title: str = "t", body: str = "", labels=None) -> Ticket:
    return Ticket(
        id=id, title=title, body=body, status="open", author="alice",
        assignees=[], labels=list(labels or []), url=f"https://example.com/{id}",
        created_at="2024-01-01T00:00:00Z", updated_at="2024-01-01T00:00:00Z",
    )


class _FakeTemplateProvider:
    """Records every `create_ticket`/`update_ticket`/`get_ticket` call so
    tests can assert zero writes happened when the gate should refuse
    pre-flight — mirrors `_FakeMergeProvider`/`_FakeLabelProvider` in
    tests/test_265_client_side_validation.py."""

    def __init__(self, templates, *, tickets=None, raise_on_list: Exception | None = None):
        self._templates = templates
        self._tickets = tickets or {}
        self._raise_on_list = raise_on_list
        self.list_calls = 0
        self.create_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self.get_ticket_calls: list[str] = []

    def list_issue_templates(self, project_, token):
        self.list_calls += 1
        if self._raise_on_list is not None:
            raise self._raise_on_list
        return self._templates

    def create_ticket(
        self, project_, token, title, body, labels, assignees, *,
        status=None, custom_fields=None,
    ):
        self.create_calls.append({
            "title": title, "body": body, "labels": list(labels or []),
            "assignees": list(assignees or []),
        })
        return _fake_ticket(id="1", title=title, body=body, labels=labels)

    def get_ticket(
        self, project_, token, ticket_id, *,
        include_relations: bool = True, include_custom_fields: bool = False,
    ):
        self.get_ticket_calls.append(ticket_id)
        ticket = self._tickets[ticket_id]
        return ticket, [], [], False

    def update_ticket(self, project_, token, ticket_id, **kwargs):
        self.update_calls.append({"ticket_id": ticket_id, **kwargs})
        title = kwargs.get("title") or "t"
        body = kwargs.get("body") or ""
        return _fake_ticket(id=ticket_id, title=title, body=body)


class _NoTemplateCapabilityProvider:
    """No `list_issue_templates` attribute at all — mirrors a provider
    with no template capability (e.g. today's GitLab/Azure DevOps
    surface, or the existing fakes in
    tests/test_default_board_column_232.py / tests/test_265_client_side_
    validation.py, none of which expose this method — regression
    safety)."""

    def __init__(self) -> None:
        self.create_calls: list[dict] = []

    def create_ticket(
        self, project_, token, title, body, labels, assignees, *,
        status=None, custom_fields=None,
    ):
        self.create_calls.append({"title": title, "body": body, "labels": list(labels or [])})
        return _fake_ticket(id="1", title=title, body=body, labels=labels)


# ===========================================================================
# R1 — list_ticket_templates: fixed shape, real skeleton, TTL cache
# ===========================================================================


def test_list_ticket_templates_returns_fixed_shape_and_real_skeleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for R1 (Route A). RED today:
    `KeyError: 'list_ticket_templates'` — the tool is not registered on
    `tools/tickets.py` yet."""
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        resp = _template_content_response("acme/backend", req)
        if resp is not None:
            return resp
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)

    result = tools["list_ticket_templates"](project_id="acme")
    assert "error" not in result, result
    assert result["project_id"] == "acme"
    templates = result["templates"]
    assert len(templates) == 1
    entry = templates[0]
    _assert_template_view_shape(entry)
    assert entry["name"] == "Bug Report"
    assert entry["kind"] == "form"
    assert set(entry["labels"]) == {"bug", "triage"}
    assert entry["title_prefix"] == "[Bug]: "
    required_headings = {rs["heading"] for rs in entry["required_sections"]}
    assert required_headings == {"Description", "Severity"}

    # skeleton must equal the lib's own render_skeleton output for the
    # REAL IssueTemplate the same mocked HTTP call discovers — not a
    # hand-computed string (fidelity rule).
    real_templates = github_provider.GitHubProvider().list_issue_templates(
        _project(), token=None,
    )
    assert len(real_templates) == 1
    assert entry["skeleton"] == templates_lib.render_skeleton(real_templates[0])


def test_list_ticket_templates_caches_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Driving test for R1 (Route B — cache-hit count). RED today:
    `KeyError: 'list_ticket_templates'`."""
    provider = _FakeTemplateProvider(templates=[_bug_template()])
    project = _project()
    tools = _register_fake_provider_tools(monkeypatch, project, provider)

    tools["list_ticket_templates"](project_id="acme")
    tools["list_ticket_templates"](project_id="acme")
    tools["list_ticket_templates"](project_id="acme")

    assert provider.list_calls == 1, "expected the shared discovery cache to dedupe within TTL"


# ===========================================================================
# R2 — create_ticket refuses in all 3 states, writing nothing
# ===========================================================================


def test_create_ticket_refuses_when_template_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """Driving test for R2 (Route A, `template_required`). RED today: no
    gate exists, so the ungated write proceeds straight to
    `POST /repos/acme/backend/labels` (best-effort ai-generated label
    ensure) — a route this handler does not define, so it fails with
    `AssertionError: unexpected request: POST .../labels`, proving
    nothing currently stops the write."""
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        resp = _template_content_response("acme/backend", req)
        if resp is not None:
            return resp
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)

    body = "### Description\nSomething broke\n"
    result = tools["create_ticket"](project_id="acme", title="Bug", body=body)

    _assert_refusal_shape(result, expected_state="template_required")
    assert result["template"] is None
    assert result["violations"] == []
    assert result["skeleton"] is None
    assert result["received_sections"] == ["Description"]
    assert len(result["templates"]) == 1
    assert "create_ticket" in result["hint"]
    assert "Bug Report" not in result["hint"]


def test_create_ticket_refuses_when_template_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Driving test for R2 (Route A, `template_unknown`). RED today:
    `create_ticket()` has no `template` parameter yet, so passing
    `template="Nonexistent"` raises `TypeError: create_ticket() got an
    unexpected keyword argument 'template'` — the compile-level gap this
    ticket must close."""
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        resp = _template_content_response("acme/backend", req)
        if resp is not None:
            return resp
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)

    body = "### Description\nSomething broke\n"
    result = tools["create_ticket"](
        project_id="acme", title="Bug", body=body, template="Nonexistent",
    )

    _assert_refusal_shape(result, expected_state="template_unknown")
    assert result["template"] is None
    assert result["violations"] == []
    assert result["skeleton"] is None
    assert result["received_sections"] == ["Description"]
    assert len(result["templates"]) == 1
    assert "create_ticket" in result["hint"]
    assert "Bug Report" not in result["hint"]


def test_create_ticket_refuses_with_violations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Driving test for R2 (Route A, `template_violation`). RED today:
    same `TypeError` as the `template_unknown` case above — `template`
    isn't a parameter yet. Violations, skeleton, and received_sections
    are all cross-checked against the lib's REAL `validate_ticket_body`
    / `render_skeleton` run on the same discovered template (fidelity
    rule) — never hand-computed."""
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        resp = _template_content_response("acme/backend", req)
        if resp is not None:
            return resp
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)

    # Description (required) is missing entirely; Severity is filled with
    # a value outside its dropdown options.
    body = "### Severity\nMedium\n"
    result = tools["create_ticket"](
        project_id="acme", title="Bug", body=body, template="Bug Report",
    )

    _assert_refusal_shape(result, expected_state="template_violation")
    assert result["template"] is not None
    _assert_template_view_shape(result["template"], with_skeleton=False)
    assert result["template"]["name"] == "Bug Report"
    assert result["received_sections"] == ["Severity"]
    assert "Bug Report" in result["hint"]
    assert "create_ticket" in result["hint"]

    real_template = github_provider.GitHubProvider().list_issue_templates(
        _project(), token=None,
    )[0]
    expected_violations = [asdict(v) for v in templates_lib.validate_ticket_body(body, real_template)]
    assert expected_violations, "fixture body must actually violate the template"
    assert result["violations"] == expected_violations
    assert result["skeleton"] == templates_lib.render_skeleton(real_template)


def test_create_ticket_refusal_never_calls_provider_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Driving test for R2 (Route B — zero writes across all 3 refusal
    states). RED today: the fake provider's `create_ticket` is called
    (and succeeds) for the `template_required` leg since no gate exists;
    the `template_unknown`/`template_violation` legs additionally raise
    `TypeError` today since `template=` isn't a parameter yet — either
    way this fails today."""
    provider = _FakeTemplateProvider(templates=[_bug_template()])
    project = _project()
    tools = _register_fake_provider_tools(monkeypatch, project, provider)

    tools["create_ticket"](project_id="acme", title="Bug", body="### Description\nx\n")
    tools["create_ticket"](
        project_id="acme", title="Bug", body="### Description\nx\n", template="Nope",
    )
    tools["create_ticket"](
        project_id="acme", title="Bug", body="### Severity\nMedium\n", template="Bug Report",
    )

    assert provider.create_calls == [], f"expected no writes; got: {provider.create_calls}"


def test_create_ticket_validates_body_before_marker_prepended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for R2's pre-marker validation edge (Route A,
    conforming create so the provider call — and its marker-prepending —
    actually happens). RED today: `template` isn't a parameter yet
    (`TypeError`), and even ignoring that, nothing calls
    `validate_ticket_body` at all today, so the spy's `calls` list stays
    empty — `assert len(calls) == 1` fails.

    See the module docstring for why the spy patches
    `lib_python_projects.templates.validate_ticket_body` directly rather
    than a tool-module-rebound name."""
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        resp = _template_content_response("acme/backend", req)
        if resp is not None:
            return resp
        if req.method == "GET" and req.url.path in (
            "/repos/acme/backend/labels/bug", "/repos/acme/backend/labels/triage",
        ):
            return _json({"name": "x"})
        if req.method == "POST" and req.url.path == "/repos/acme/backend/labels":
            return _json({"name": "ai-generated"}, 201)
        if req.method == "POST" and req.url.path == "/repos/acme/backend/issues":
            payload = json.loads(req.content.decode("utf-8"))
            return _json({
                "number": 1, "title": payload["title"], "body": payload["body"],
                "state": "open", "user": {"login": "alice"}, "assignees": [],
                "labels": [{"name": n} for n in payload.get("labels", [])],
                "html_url": "", "created_at": "", "updated_at": "",
            }, 201)
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    seen = _install_mock(monkeypatch, handler)

    calls: list[str] = []
    real_validate = templates_lib.validate_ticket_body

    def spy(body, template):
        calls.append(body)
        return real_validate(body, template)

    monkeypatch.setattr(templates_lib, "validate_ticket_body", spy)

    body = "### Description\nSomething happened\n\n### Severity\nHigh\n"
    result = tools["create_ticket"](
        project_id="acme", title="Bug", body=body, template="Bug Report",
    )

    assert "error" not in result, result
    assert len(calls) == 1, f"expected validate_ticket_body called exactly once; got: {calls}"
    assert "#ai-generated" not in calls[0], (
        "validate_ticket_body must see the caller's RAW body, before the "
        "provider prepends the #ai-generated marker"
    )

    post = next(
        r for r in seen if r.method == "POST" and r.url.path == "/repos/acme/backend/issues"
    )
    sent_body = json.loads(post.content.decode("utf-8"))["body"]
    assert "#ai-generated" in sent_body, (
        "the OUTGOING create request body must carry the marker — only "
        "validation runs pre-marker, not the actual write"
    )


# ===========================================================================
# R3 — conforming create_ticket succeeds: labels unioned, title_prefix
#      applied only when absent
# ===========================================================================


def test_create_ticket_conforming_writes_with_labels_and_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for R3 (Route A — real validation success path). RED
    today: `TypeError` — `template` isn't a parameter yet."""
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        resp = _template_content_response("acme/backend", req)
        if resp is not None:
            return resp
        if req.method == "GET" and req.url.path in (
            "/repos/acme/backend/labels/bug", "/repos/acme/backend/labels/triage",
        ):
            return _json({"name": "x"})
        if req.method == "POST" and req.url.path == "/repos/acme/backend/labels":
            return _json({"name": "ai-generated"}, 201)
        if req.method == "POST" and req.url.path == "/repos/acme/backend/issues":
            payload = json.loads(req.content.decode("utf-8"))
            return _json({
                "number": 9, "title": payload["title"], "body": payload["body"],
                "state": "open", "user": {"login": "alice"}, "assignees": [],
                "labels": [{"name": n} for n in payload.get("labels", [])],
                "html_url": "", "created_at": "", "updated_at": "",
            }, 201)
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    seen = _install_mock(monkeypatch, handler)

    body = "### Description\nSomething happened\n\n### Severity\nHigh\n"
    result = tools["create_ticket"](
        project_id="acme", title="Something broke", body=body, template="Bug Report",
    )
    assert "error" not in result, result

    post = next(
        r for r in seen if r.method == "POST" and r.url.path == "/repos/acme/backend/issues"
    )
    sent = json.loads(post.content.decode("utf-8"))
    assert sent["title"] == "[Bug]: Something broke"
    assert set(sent["labels"]) >= {"bug", "triage"}


def test_create_ticket_title_prefix_not_doubled_when_already_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for R3 (Route B — "prefix already present" leg + no
    label-creation call + label-union dedup). RED today: `TypeError` —
    `template` isn't a parameter yet."""
    provider = _FakeTemplateProvider(templates=[_bug_template()])
    project = _project()
    tools = _register_fake_provider_tools(monkeypatch, project, provider)

    body = "### Description\nfilled in\n\n### Severity\nLow\n"
    result = tools["create_ticket"](
        project_id="acme", title="[Bug]: Already prefixed", body=body,
        template="Bug Report", labels=["urgent", "bug"],
    )

    assert "error" not in result, result
    assert len(provider.create_calls) == 1
    call = provider.create_calls[0]
    assert call["title"] == "[Bug]: Already prefixed", (
        "title_prefix must not be applied a second time when already present"
    )
    assert set(call["labels"]) == {"urgent", "bug", "triage"}
    assert len(call["labels"]) == 3, (
        f"expected no duplicate 'bug' after union with template labels; got: {call['labels']}"
    )


# ===========================================================================
# R4 — update_ticket gated only when body is supplied; template resolved
#      from template=, else current labels, else refuses
# ===========================================================================


def test_update_ticket_resolves_template_from_labels_then_refuses_on_violation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for R4 (Route A — label-resolution leg). The ticket's
    current labels (`bug`, `triage`, `ai-generated`) are a superset of
    the "Bug Report" template's own labels (`bug`, `triage`), so exactly
    one template is inferred with no explicit `template=`; the submitted
    body still violates it, so the write is refused.

    RED today: no gate exists at all, so the PATCH goes through — the
    handler's PATCH branch raises `AssertionError` naming that as
    unexpected, since nothing should have resolved/validated a template
    yet."""
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        resp = _template_content_response("acme/backend", req)
        if resp is not None:
            return resp
        if req.method == "GET" and req.url.path == "/repos/acme/backend/issues/42":
            return _json({
                "number": 42, "title": "Bug", "body": "old",
                "state": "open", "user": {"login": "alice"}, "assignees": [],
                "labels": [{"name": "bug"}, {"name": "triage"}, {"name": "ai-generated"}],
                "html_url": "", "created_at": "", "updated_at": "",
            })
        if req.method == "GET" and req.url.path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        if req.method == "PATCH":
            raise AssertionError(
                "PATCH must not happen — the violating body should have been refused"
            )
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)

    body = "### Severity\nMedium\n"  # Description missing, Severity invalid
    result = tools["update_ticket"](project_id="acme", ticket_id="42", body=body)

    _assert_refusal_shape(result, expected_state="template_violation")
    assert result["template"] is not None
    assert result["template"]["name"] == "Bug Report"
    assert "update_ticket" in result["hint"]
    assert "create_ticket" not in result["hint"]


def test_update_ticket_refuses_when_no_template_inferred_from_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for R4 (Route A — no-match refusal leg, hint names
    `update_ticket` not `create_ticket`). RED today: no gate exists, so
    the write proceeds unguarded — in this fixture it actually trips on
    the ai-modified label best-effort ensure (`POST .../labels`, an
    unhandled route) before ever reaching PATCH, since the ticket
    carries no `ai-generated` label yet; either way the handler raises
    `AssertionError`, proving nothing currently blocks the write."""
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        resp = _template_content_response("acme/backend", req)
        if resp is not None:
            return resp
        if req.method == "GET" and req.url.path == "/repos/acme/backend/issues/42":
            return _json({
                "number": 42, "title": "Bug", "body": "old",
                "state": "open", "user": {"login": "alice"}, "assignees": [],
                "labels": [{"name": "enhancement"}],
                "html_url": "", "created_at": "", "updated_at": "",
            })
        if req.method == "GET" and req.url.path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        if req.method == "PATCH":
            raise AssertionError("PATCH must not happen — no template could be resolved")
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)

    body = "### Description\nfixed now\n"
    result = tools["update_ticket"](project_id="acme", ticket_id="42", body=body)

    _assert_refusal_shape(result, expected_state="template_required")
    assert result["template"] is None
    assert "update_ticket" in result["hint"]
    assert "create_ticket" not in result["hint"]
    assert "Bug Report" not in result["hint"]


def test_update_ticket_ambiguous_label_match_refuses_with_full_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for R4's ambiguous-match edge (Route B): two
    templates whose label sets are both subsets of the ticket's current
    labels — no unique inference is possible, so the tool must refuse
    with the full template list rather than guessing. RED today: no gate
    exists, so the fake provider's `update_ticket` is called and
    succeeds — `provider.update_calls == []` fails."""
    template_a = templates_lib.IssueTemplate(
        name="A", filename="a.yml", title_prefix="", labels=["bug"],
        kind="form", fields=[],
    )
    template_b = templates_lib.IssueTemplate(
        name="B", filename="b.yml", title_prefix="", labels=["bug", "urgent"],
        kind="form", fields=[],
    )
    ticket = _fake_ticket(id="42", labels=["bug", "urgent", "ai-generated"])
    provider = _FakeTemplateProvider(templates=[template_a, template_b], tickets={"42": ticket})
    project = _project()
    tools = _register_fake_provider_tools(monkeypatch, project, provider)

    result = tools["update_ticket"](project_id="acme", ticket_id="42", body="### x\ny\n")

    _assert_refusal_shape(result, expected_state="template_required")
    assert len(result["templates"]) == 2
    assert provider.update_calls == [], f"expected no write; got: {provider.update_calls}"


def test_update_ticket_infers_template_with_empty_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for review round 1's blocking finding: a template
    that declares no `labels:` at all (a valid, common GitHub issue-form
    shape) must still be inferable, because an empty set is mathematically
    a subset of every ticket's label set — exactly what the tool's own
    docstring promises ("the template is inferred from the ticket's
    current labels — exactly one template whose labels are a subset of
    the ticket's current labels").

    RED today: `tickets.py`'s inference guards with `t.labels and
    set(t.labels) <= current_labels`, so a `labels=[]` template is
    excluded from `matches` unconditionally — `matches` is always empty
    for a single-template project like this one, and the call wrongly
    refuses with `template_required` instead of inferring the sole
    template and validating the body against it."""
    template_no_labels = templates_lib.IssueTemplate(
        name="Freeform", filename="freeform.yml", title_prefix="", labels=[],
        kind="form",
        fields=[
            templates_lib.TemplateField(
                label="Description", field_id="description", type="textarea",
                required=True, options=None, description=None, placeholder=None,
            ),
        ],
    )
    ticket = _fake_ticket(id="42", labels=["enhancement"])
    provider = _FakeTemplateProvider(
        templates=[template_no_labels], tickets={"42": ticket},
    )
    project = _project()
    tools = _register_fake_provider_tools(monkeypatch, project, provider)

    body = "### Description\nfixed now\n"
    result = tools["update_ticket"](project_id="acme", ticket_id="42", body=body)

    assert "error" not in result, result
    assert "state" not in result, (
        f"expected the sole empty-label template to be inferred and the "
        f"conforming body to be accepted, not refused; got: {result}"
    )
    assert len(provider.update_calls) == 1, (
        f"expected exactly one write via the inferred empty-label "
        f"template; got: {provider.update_calls}"
    )


def test_update_ticket_empty_label_template_ambiguous_alongside_another_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additional coverage for the empty-label fix's interaction with
    ambiguity (review round 1, point 2): an empty-label template is now
    ALWAYS a subset-match candidate, so when the ticket's labels also
    satisfy a second, more specific template, inference must refuse as
    ambiguous rather than silently preferring either one. This already
    fails today too (for the wrong reason: the buggy truthiness guard
    excludes the empty-label template entirely, so the non-empty "Bug"
    template alone looks like a unique match and the write wrongly
    succeeds) — after the fix, both templates are legitimate subset
    matches and the tool must refuse with the full list instead of
    guessing."""
    template_empty = templates_lib.IssueTemplate(
        name="Freeform", filename="freeform.yml", title_prefix="", labels=[],
        kind="form", fields=[],
    )
    template_bug = templates_lib.IssueTemplate(
        name="Bug", filename="bug.yml", title_prefix="", labels=["bug"],
        kind="form", fields=[],
    )
    ticket = _fake_ticket(id="42", labels=["bug", "ai-generated"])
    provider = _FakeTemplateProvider(
        templates=[template_empty, template_bug], tickets={"42": ticket},
    )
    project = _project()
    tools = _register_fake_provider_tools(monkeypatch, project, provider)

    result = tools["update_ticket"](project_id="acme", ticket_id="42", body="### x\ny\n")

    _assert_refusal_shape(result, expected_state="template_required")
    assert len(result["templates"]) == 2
    assert provider.update_calls == [], f"expected no write; got: {provider.update_calls}"


def test_update_ticket_body_none_skips_template_gate_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additional coverage for R4 (Route B): a `body=None` update must
    never trigger template discovery or a pre-update fetch at all. This
    already passes today (no gate exists yet to trigger spuriously) —
    included as a regression guard the gate must keep satisfying once
    implemented, not as a RED driving assertion."""
    provider = _FakeTemplateProvider(
        templates=[_bug_template()],
        tickets={"42": _fake_ticket(id="42", labels=["bug", "triage"])},
    )
    project = _project()
    tools = _register_fake_provider_tools(monkeypatch, project, provider)

    result = tools["update_ticket"](project_id="acme", ticket_id="42", title="New title")

    assert "error" not in result, result
    assert provider.list_calls == 0
    assert provider.get_ticket_calls == []
    assert len(provider.update_calls) == 1
    assert provider.update_calls[0]["title"] == "New title"
    assert provider.update_calls[0].get("body") is None


# ===========================================================================
# R5 — no-templates projects pass through with template_warning; a
#      listing failure fails closed and is never cached
# ===========================================================================


def test_create_ticket_passes_through_when_provider_has_no_template_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for R5 (Route B — capability-absent leg). RED today:
    `result["template_warning"]` raises `KeyError` — the key doesn't
    exist yet."""
    provider = _NoTemplateCapabilityProvider()
    project = _project()
    tools = _register_fake_provider_tools(monkeypatch, project, provider)

    result = tools["create_ticket"](project_id="acme", title="Bug", body="anything at all")

    assert "error" not in result, result
    assert result["template_warning"] == "project has no templates"
    assert len(provider.create_calls) == 1


def test_create_ticket_passes_through_when_project_has_zero_templates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for R5 (Route A — empty-list leg: a real
    `GitHubProvider` whose `.github/ISSUE_TEMPLATE` 404s, which the lib
    folds to `[]`). RED today: same `KeyError` reasoning as above."""
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/repos/acme/backend/contents/.github/ISSUE_TEMPLATE":
            return httpx.Response(404, json={"message": "Not Found"})
        if req.method == "POST" and req.url.path == "/repos/acme/backend/labels":
            return _json({"name": "ai-generated"}, 201)
        if req.method == "POST" and req.url.path == "/repos/acme/backend/issues":
            payload = json.loads(req.content.decode("utf-8"))
            return _json({
                "number": 1, "title": payload["title"], "body": payload["body"],
                "state": "open", "user": {"login": "alice"}, "assignees": [],
                "labels": [{"name": n} for n in payload.get("labels", [])],
                "html_url": "", "created_at": "", "updated_at": "",
            }, 201)
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)

    result = tools["create_ticket"](
        project_id="acme", title="Bug", body="anything, no template exists",
    )
    assert "error" not in result, result
    assert result["template_warning"] == "project has no templates"


def test_create_ticket_listing_failure_fails_closed_and_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for R5 (Route B — a listing failure fails closed and
    is never cached, so an immediate retry re-attempts enforcement). RED
    today: the fake provider's `create_ticket` succeeds unconditionally
    (no gate calls `list_issue_templates` at all, so `provider.list_
    calls` stays 0, and `"error" in result1` is false since nothing
    ever raises)."""
    provider = _FakeTemplateProvider(templates=[], raise_on_list=GitHubError(500, "boom"))
    project = _project()
    tools = _register_fake_provider_tools(monkeypatch, project, provider)

    result1 = tools["create_ticket"](project_id="acme", title="Bug", body="x")
    assert "error" in result1, f"expected fail-closed error; got: {result1}"
    assert provider.create_calls == []

    result2 = tools["create_ticket"](project_id="acme", title="Bug", body="x")
    assert "error" in result2, f"expected fail-closed error; got: {result2}"
    assert provider.create_calls == []
    assert provider.list_calls == 2, (
        "a listing failure must not be cached — each retry re-attempts enforcement"
    )


# ===========================================================================
# R6 — create_ticket's docstring drops "DO NOT pre-inspect", gains the
#      ticket's exact replacement sentence, stays under the char ceiling
# ===========================================================================


_NEW_TEMPLATE_SENTENCE = (
    "On a project with templates, call `list_ticket_templates` first "
    "(or read the templates from the refusal) and fill the skeleton; "
    "do not inspect the codebase for context."
)


def test_create_ticket_docstring_drops_do_not_pre_inspect_and_gains_template_sentence() -> None:
    """Driving test for R6. RED today: `create_ticket.__doc__` still
    contains "DO NOT pre-inspect" verbatim (measured directly against
    the installed source before writing this test) and does not contain
    the ticket's replacement sentence at all."""
    tools = _register_plain(ticket_tools)
    doc = tools["create_ticket"].__doc__ or ""
    assert "DO NOT pre-inspect" not in doc
    assert _NEW_TEMPLATE_SENTENCE in doc, (
        "expected the ticket's EXACT replacement sentence, verbatim (not "
        f"paraphrased); got docstring: {doc!r}"
    )
    assert len(doc) <= 4796, f"create_ticket docstring grew to {len(doc)} chars"


def test_create_pr_docstring_do_not_pre_inspect_sentence_unaffected() -> None:
    """No-regression guard (already passing today): `pulls.py`'s
    identical sentence in `create_pr` must be untouched by this ticket."""
    tools = _register_plain(pulls_tools)
    doc = tools["create_pr"].__doc__ or ""
    assert "DO NOT pre-inspect" in doc


# ===========================================================================
# R7a — SKILL.md gets a "## Ticket templates" section
# ===========================================================================


def _skill_section(heading: str) -> str:
    text = (_REPO_ROOT / "skills" / "project-issues" / "SKILL.md").read_text(encoding="utf-8")
    start = text.find(heading)
    assert start != -1, f"heading {heading!r} not found in SKILL.md"
    rest = text[start + len(heading):]
    next_heading = re.search(r"^## ", rest, re.MULTILINE)
    end = start + len(heading) + (next_heading.start() if next_heading else len(rest))
    return text[start:end]


def test_skill_md_has_ticket_templates_section_with_required_contents() -> None:
    """Driving test for R7a. RED today: no `## Ticket templates` heading
    exists in SKILL.md at all — `_skill_section` asserts and fails
    immediately."""
    section = _skill_section("## Ticket templates")
    assert "list_ticket_templates" in section
    assert '"written": false' in section
    assert "template=" in section, "expected the one-call correction path (template=...)"
    assert "leg ein Ticket an" in section
    assert "erstelle ein Issue" in section


# ===========================================================================
# R7b — README.md documents list_ticket_templates + the epic note; no
#       configurable mode is ever documented
# ===========================================================================


def _readme_text() -> str:
    return (_REPO_ROOT / "README.md").read_text(encoding="utf-8")


def test_readme_documents_list_ticket_templates_and_epic_note() -> None:
    """Driving test for R7b (positive). RED today: none of these strings
    appear in README.md (confirmed via grep before writing this test)."""
    text = _readme_text()
    assert "list_ticket_templates" in text
    assert "epic.yml" in text
    assert "no special case" in text
    assert "label-based exemption" in text


def test_readme_does_not_document_a_configurable_template_mode() -> None:
    """Driving/guard test for R7b (negative). Already passes today (none
    of these strings exist yet) and must keep passing — enforcement is
    mechanical and non-configurable, never a `tickets.templates` mode
    knob. Uses distinctive words ("enforce"/"advise") plus a
    `mode: <value>`-shaped regex for "off" rather than a bare "off"
    substring check, since "off" appears today in ordinary prose
    ("hanging off the PR") unrelated to any config mode."""
    text = _readme_text()
    assert "tickets.templates" not in text
    assert "enforce" not in text
    assert "advise" not in text
    assert not re.search(r"\bmode\s*[:=]\s*(enforce|advise|off)\b", text, re.IGNORECASE)


# ===========================================================================
# R7c — AGENTS.md's Invariants section states the two ticket-fixed phrases
# ===========================================================================


def _agents_section(heading: str) -> str:
    text = (_REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    start = text.find(heading)
    assert start != -1, f"heading {heading!r} not found in AGENTS.md"
    rest = text[start + len(heading):]
    next_heading = re.search(r"^## ", rest, re.MULTILINE)
    end = start + len(heading) + (next_heading.start() if next_heading else len(rest))
    return text[start:end]


def test_agents_md_invariants_state_template_rationales() -> None:
    """Driving test for R7c. RED today: neither phrase appears anywhere
    in AGENTS.md's Invariants section (confirmed via grep before writing
    this test)."""
    section = _agents_section("## Invariants")
    assert "the library reports, the plugin decides" in section
    assert "validation runs before the #ai-generated marker" in section
