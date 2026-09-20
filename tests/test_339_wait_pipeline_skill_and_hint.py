"""Ticket #339: the bundled skill documents `project-issues wait-pipeline`
and the empty-result hint of `list_pipeline_runs` points at it."""
from __future__ import annotations

import re
import shlex

import httpx
import pytest

from project_issues_plugin import cli
from project_issues_plugin.tools import pipelines as pipeline_tools

from tests.test_239_bundled_skill import _skill_text
from tests.test_pipelines import (
    _install_mock,
    _json,
    _project,
    _register_tools_with,
)


def _wait_section() -> str:
    text = _skill_text()
    assert "wait-pipeline" in text, "SKILL.md never mentions wait-pipeline"
    start = text.index("wait-pipeline")
    # Section = from the first mention's heading/paragraph to the next
    # top-level ("## ") heading.
    head = text.rfind("\n#", 0, start)
    nxt = text.find("\n## ", start)
    return text[head if head != -1 else 0: nxt if nxt != -1 else len(text)]


# ---------- R1: the skill teaches how to wait for CI -------------------------


def test_skill_documents_wait_pipeline_command_and_flags() -> None:
    section = _wait_section()
    m = re.search(r"project-issues wait-pipeline[^\n`]*", section)
    assert m, "no full `project-issues wait-pipeline ...` command line"
    parser = cli._build_parser()
    argv = shlex.split(m.group(0))[2:]
    argv = [a.replace("<id>", "acme").replace("<commit>", "abc123") for a in argv]
    ns = parser.parse_args(argv)  # every shown flag must be accepted
    assert ns.timeout == 540
    assert ns.interval == 20
    assert "--project" in m.group(0) and "--sha" in m.group(0)


def test_skill_states_one_blocking_call_with_bash_timeout() -> None:
    section = _wait_section()
    assert "600000" in section
    assert re.search(r"(one|single|ONE)[^\n]{0,60}(foreground|blocking|call)", section)
    assert re.search(r"(loop|poll)", section, re.I)


def test_skill_explains_why_no_mcp_wait_tool() -> None:
    section = _wait_section()
    assert re.search(r"no MCP (wait )?tool|MCP[^\n]{0,80}no[^\n]{0,20}wait", section, re.I)
    assert re.search(r"script", section, re.I), "missing 'not callable from scripts' reason"
    assert re.search(r"block", section, re.I)


def test_skill_exit_codes_match_cli_constants() -> None:
    section = _wait_section()
    expected = {
        cli.EXIT_SUCCESS: r"green|success|pass",
        cli.EXIT_FAILURE: r"fail",
        cli.EXIT_PENDING: r"timeout|timed out|pending",
        cli.EXIT_NO_RUNS: r"no run",
        cli.EXIT_ERROR: r"error",
        cli.EXIT_NO_VERDICT: r"verdict",
    }
    assert sorted(expected) == [0, 1, 2, 3, 4, 5]
    for code, meaning in expected.items():
        lines = [
            ln for ln in section.splitlines()
            if re.search(rf"(?<![\w.-]){code}(?![\w.-])", ln)
            and re.search(meaning, ln, re.I)
        ]
        assert lines, f"exit code {code} ({meaning}) not documented"
    assert re.search(r"cancelled", section) and re.search(r"skipped", section)


def test_skill_names_binary_location_and_not_on_path() -> None:
    section = _wait_section()
    assert "bin/project-issues" in section
    assert re.search(r"not on `?PATH`?", section, re.I)
    assert "CLAUDE_PLUGIN_ROOT" in section
    assert "--help" in section


def test_skill_light_response_writes_pinned_ticket_item_d() -> None:
    text = _skill_text()
    for tool in ("add_pr_comment", "add_pr_review_comment", "submit_pr_review"):
        assert tool in text
    assert 'response="full"' in text


# ---------- R2: empty-result hint names wait-pipeline ------------------------


def _empty_hint(monkeypatch: pytest.MonkeyPatch, **kwargs) -> dict:
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/commits/abc123":
            return _json({"sha": "abc123"})
        if req.url.path == "/repos/acme/backend/branches/main":
            return _json({"name": "main", "commit": {"sha": "abc123"}})
        if req.url.path == "/repos/acme/backend/actions/runs":
            return _json({"workflow_runs": []})
        if req.url.path == "/repos/acme/backend/actions/workflows":
            return _json({"workflows": [{"id": 1, "name": "CI"}]})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    return tools["list_pipeline_runs"](project_id="acme", **kwargs)


def test_empty_commit_hint_names_wait_pipeline(monkeypatch) -> None:
    result = _empty_hint(monkeypatch, commit_sha="abc123")
    assert "error" not in result, result
    assert result["addressed_by"] == "commit"
    assert result["runs"] == []
    assert "wait-pipeline" in result["hint"]


def test_empty_branch_hint_names_wait_pipeline(monkeypatch) -> None:
    result = _empty_hint(monkeypatch, branch="main")
    assert "error" not in result, result
    assert result["runs"] == []
    assert "wait-pipeline" in result["hint"]


def test_tag_resolution_failure_hint_omits_wait_pipeline(monkeypatch) -> None:
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        if "/git/refs/tags/" in req.url.path or "/tags" in req.url.path:
            return _json({"message": "Not Found"}, status_code=404)
        if req.url.path == "/repos/acme/backend/actions/runs":
            return _json({"workflow_runs": []})
        if req.url.path == "/repos/acme/backend/actions/workflows":
            return _json({"workflows": []})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_pipeline_runs"](project_id="acme", tag="nope")
    assert "could not resolve tag" in (result.get("hint") or ""), result
    assert "wait-pipeline" not in result["hint"]


def test_list_pipeline_runs_docstring_mentions_wait_pipeline(monkeypatch) -> None:
    tools = _register_tools_with(monkeypatch, _project())
    assert "wait-pipeline" in (tools["list_pipeline_runs"].__doc__ or "")
