"""Driving tests for ticket #297 — accept `#`-prefixed GitHub label colors.

`create_label` / `update_label` currently reject `color="#3399ff"` on GitHub
in the wrapper's own pre-flight check (`_validate_github_color` /
`_GITHUB_HEX_COLOR` in `tools/labels.py`), so the call never reaches the
provider. The fix loosens that check to accept an optional leading `#`,
strips it before the provider call, and brings the docs (which claim
"without '#'") back in line — see the approved plan for #297.

Reuses the `_StubMCP` / `_FakeLabelProvider` / `_register_label_tools`
pattern from `tests/test_265_client_side_validation.py`, re-declared here
per this repo's per-ticket-file convention (see `tests/test_282_gitlab_
default_label_color_docs.py`).

Covers:
  - R1: a `#`-prefixed 6-hex GitHub color is accepted at both call sites
    and reaches the provider stripped of its leading `#`.
  - R2: genuinely malformed GitHub colors are still rejected pre-flight,
    now with the new message (no more "without '#'"), and never reach the
    provider.
  - R3: the agent-visible schema/docs no longer claim `#` is rejected, and
    now pin one exact phrase confirming it is accepted and stripped.

Note (discovered while grounding this file, informational only — does not
change the plan's chosen fix shape): once the lib deps are locally synced
(`pwsh scripts/sync-libs.ps1`), `lib_python_projects.providers.github`
turns out to already strip a leading `#` server-side via its own
`_normalize_github_color` helper (used by both `GitHubProvider.create_label`
and `.update_label`). The plan anticipated this could not be verified and
chose to strip in the wrapper regardless, since that is correct whether or
not the lib also normalizes (idempotent no-op if it does) — this file's
`_FakeLabelProvider`-based tests exercise the wrapper's own stripping
directly (the fake does no normalization of its own), independent of that
lib behaviour.
"""
from __future__ import annotations

from typing import Callable

import pytest
from mcp.server.fastmcp.utilities.func_metadata import func_metadata

from lib_python_projects import IssuesPermissions, Permissions, ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers.base import Label
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import labels as label_tools


# ---------------------------------------------------------------------------
# Shared scaffolding (re-declared per this repo's per-ticket-file convention)
# ---------------------------------------------------------------------------


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _register_schema(module) -> dict[str, Callable]:
    stub = _StubMCP()
    module.register(stub)
    return stub.tools


def _param_description(fn: Callable, param: str) -> str:
    """Return the Field description for a parameter, or '' if absent."""
    schema = func_metadata(fn).arg_model.model_json_schema()
    prop = schema.get("properties", {}).get(param, {})
    return prop.get("description", "")


def _labels_project(*, provider: str = "github", project_id: str = "acme") -> ProjectConfig:
    return ProjectConfig(
        id=project_id,
        provider=provider,
        path=f"{project_id}/backend",
        token_env=f"TOKEN_{project_id.upper()}",
        permissions=Permissions(issues=IssuesPermissions(create=True, modify=True)),
    )


class _FakeLabelProvider:
    """Records every `create_label`/`update_label` call so tests can assert
    exactly what color reached the provider, and that no call was made when
    the client-side guard should reject pre-flight."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create_label(self, project_, token, name, *, color=None, description=None):
        self.calls.append({
            "op": "create", "name": name, "color": color, "description": description,
        })
        return Label(name=name, color=color or "", description=description or "")

    def update_label(
        self, project_, token, name, *,
        new_name=None, color=None, description=None,
    ):
        self.calls.append({
            "op": "update", "name": name, "new_name": new_name,
            "color": color, "description": description,
        })
        return Label(
            name=new_name or name, color=color or "", description=description or "",
        )


def _register_label_tools(
    monkeypatch: pytest.MonkeyPatch, provider_instance, project: ProjectConfig,
) -> dict[str, Callable]:
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setitem(providers_mod._PROVIDERS, project.provider, provider_instance)
    monkeypatch.setenv(project.token_env, "tok")

    stub = _StubMCP()
    label_tools.register(stub)
    return stub.tools


# ---------------------------------------------------------------------------
# R1 — driving tests: `#`-prefixed GitHub color accepted, stripped before
# the provider call.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", ["create_label", "update_label"])
def test_github_hash_prefixed_color_accepted_and_stripped(
    monkeypatch: pytest.MonkeyPatch, tool_name: str,
) -> None:
    """Driving test. Today `_GITHUB_HEX_COLOR` is `^[0-9a-fA-F]{6}$` — no
    leading '#' allowed — so `color="#3399ff"` raises `ValueError` in
    `_validate_github_color` before the provider is ever called. Expected
    RED: `'error'` is present in the result and `provider.calls` is
    empty."""
    provider = _FakeLabelProvider()
    project = _labels_project(provider="github")
    tools = _register_label_tools(monkeypatch, provider, project)

    out = tools[tool_name](project_id="acme", name="bug", color="#3399ff")

    assert "error" not in out, f"expected success; got: {out}"
    assert provider.calls, "expected a provider call"
    assert provider.calls[-1]["color"] == "3399ff", (
        f"expected the leading '#' stripped before the provider call; "
        f"got: {provider.calls[-1]!r}"
    )


@pytest.mark.parametrize("tool_name", ["create_label", "update_label"])
def test_github_hash_prefixed_uppercase_color_accepted_and_stripped(
    monkeypatch: pytest.MonkeyPatch, tool_name: str,
) -> None:
    """Driving test. Same missing-behaviour reason as above, for an
    uppercase hex value — also confirms case is preserved, only the
    leading '#' is stripped. Expected RED: rejected today."""
    provider = _FakeLabelProvider()
    project = _labels_project(provider="github")
    tools = _register_label_tools(monkeypatch, provider, project)

    out = tools[tool_name](project_id="acme", name="bug", color="#3399FF")

    assert "error" not in out, f"expected success; got: {out}"
    assert provider.calls[-1]["color"] == "3399FF", (
        f"expected case preserved and '#' stripped; got: {provider.calls[-1]!r}"
    )


# ---------- Additional coverage (already passing today, no RED needed) -----


@pytest.mark.parametrize("tool_name", ["create_label", "update_label"])
def test_github_bare_hex_color_unchanged_no_regression(
    monkeypatch: pytest.MonkeyPatch, tool_name: str,
) -> None:
    """Regression guard — already passes today; a bare 6-hex value with no
    leading '#' must keep reaching the provider unchanged."""
    provider = _FakeLabelProvider()
    project = _labels_project(provider="github")
    tools = _register_label_tools(monkeypatch, provider, project)

    out = tools[tool_name](project_id="acme", name="bug", color="3399ff")

    assert "error" not in out, f"unexpected error: {out}"
    assert provider.calls[-1]["color"] == "3399ff"


@pytest.mark.parametrize("tool_name", ["create_label", "update_label"])
def test_github_color_none_skips_validation_no_regression(
    monkeypatch: pytest.MonkeyPatch, tool_name: str,
) -> None:
    """Regression guard — already passes today; `color=None` must keep
    skipping validation and reaching the provider unchanged."""
    provider = _FakeLabelProvider()
    project = _labels_project(provider="github")
    tools = _register_label_tools(monkeypatch, provider, project)

    out = tools[tool_name](project_id="acme", name="bug", color=None)

    assert "error" not in out, f"unexpected error: {out}"
    assert provider.calls[-1]["color"] is None


# ---------------------------------------------------------------------------
# R2 — driving tests: genuinely malformed GitHub colors still rejected
# pre-flight, now with the new message; no provider call.
# ---------------------------------------------------------------------------


_MALFORMED_GITHUB_COLORS = [
    "notacolor",
    "",
    "#fff",         # 3-digit shorthand, not accepted
    "fff",          # bare 3-digit shorthand, not accepted
    "#ededed ",     # trailing space — no implicit trim
    "##ededed",     # double leading '#'
    "#ff00000",     # 7 hex digits
]


@pytest.mark.parametrize("tool_name", ["create_label", "update_label"])
@pytest.mark.parametrize("color", _MALFORMED_GITHUB_COLORS)
def test_github_malformed_colors_rejected_with_new_message(
    monkeypatch: pytest.MonkeyPatch, tool_name: str, color: str,
) -> None:
    """Driving test. These values are already rejected today, but with the
    *old* message (`"...without '#' (e.g. 'ededed')..."`). Expected RED:
    the old wording is still present / the new wording is absent — this
    file pins the message change, not just the rejection, which already
    passes."""
    provider = _FakeLabelProvider()
    project = _labels_project(provider="github")
    tools = _register_label_tools(monkeypatch, provider, project)

    out = tools[tool_name](project_id="acme", name="bug", color=color)

    assert "error" in out, f"expected rejection for color={color!r}; got: {out}"
    err = out["error"]
    assert "6-digit hex" in err, f"expected the rule named; got: {err!r}"
    assert "ededed" in err, f"expected the example; got: {err!r}"
    assert repr(color) in err, f"expected the bad value echoed; got: {err!r}"
    assert "optional leading '#'" in err, (
        f"expected the new message wording; got: {err!r}"
    )
    assert "without '#'" not in err, (
        f"expected the old rejection wording gone; got: {err!r}"
    )
    assert "Label.color" not in err, f"leaked provider field name: {err!r}"
    assert provider.calls == [], f"expected no provider call; got: {provider.calls}"


# ---------------------------------------------------------------------------
# R3 — driving tests: schema/docs no longer claim '#' is rejected, and now
# pin one exact phrase confirming it is accepted and stripped.
# ---------------------------------------------------------------------------


_HASH_ACCEPTED_PHRASE = "a leading '#' is accepted and stripped"


def test_create_label_color_description_no_longer_rejects_hash() -> None:
    """Driving test. Today's Field description literally contains
    "without '#'". Expected RED: substring present."""
    tools = _register_schema(label_tools)
    desc = _param_description(tools["create_label"], "color")
    assert "without '#'" not in desc, (
        f"expected the old GitHub rejection wording gone; got: {desc!r}"
    )


def test_update_label_color_description_no_longer_rejects_hash() -> None:
    """Driving test. Same reason as the create_label description test
    above, for update_label's color description. Expected RED."""
    tools = _register_schema(label_tools)
    desc = _param_description(tools["update_label"], "color")
    assert "without '#'" not in desc, (
        f"expected the old GitHub rejection wording gone; got: {desc!r}"
    )


def test_create_label_color_description_documents_hash_accepted() -> None:
    """Driving test. Expected RED: the pinned phrase does not exist yet
    anywhere in the docs."""
    tools = _register_schema(label_tools)
    desc = _param_description(tools["create_label"], "color")
    assert _HASH_ACCEPTED_PHRASE in desc, (
        f"expected the pinned hash-accepted phrase; got: {desc!r}"
    )


def test_update_label_color_description_documents_hash_accepted() -> None:
    """Driving test. Same reason as above, for update_label. Expected
    RED."""
    tools = _register_schema(label_tools)
    desc = _param_description(tools["update_label"], "color")
    assert _HASH_ACCEPTED_PHRASE in desc, (
        f"expected the pinned hash-accepted phrase; got: {desc!r}"
    )


def test_module_docstring_no_longer_rejects_hash() -> None:
    """Driving test. Today's module docstring GitHub bullet reads
    "*without* `#`" (line 9). Expected RED: phrase present, pinned
    replacement phrase absent."""
    doc = label_tools.__doc__ or ""
    assert "*without* `#`" not in doc, (
        f"expected the old GitHub-bullet rejection phrasing gone; got: {doc!r}"
    )
    assert _HASH_ACCEPTED_PHRASE in doc, (
        f"expected the pinned hash-accepted phrase; got: {doc!r}"
    )


def test_create_label_docstring_no_longer_rejects_hash() -> None:
    """Driving test. Today's `create_label` docstring GitHub bullet reads
    "*without* ``#``" (line 175). Expected RED: phrase present, pinned
    replacement phrase absent."""
    tools = _register_schema(label_tools)
    doc = tools["create_label"].__doc__ or ""
    assert "*without* ``#``" not in doc, (
        f"expected the old GitHub-bullet rejection phrasing gone; got: {doc!r}"
    )
    assert _HASH_ACCEPTED_PHRASE in doc, (
        f"expected the pinned hash-accepted phrase; got: {doc!r}"
    )
