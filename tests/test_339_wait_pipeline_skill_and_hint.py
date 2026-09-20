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


def _pipelines_section() -> str:
    """Body of the `## Pipelines...` heading, up to the next `## ` heading.

    Anchored to the heading (not to a first mention of any token); `###`
    subsections stay inside the window, `## ` siblings end it. Fenced code
    blocks are honoured so a `## ` inside a fence does not end the section.
    """
    lines = _skill_text().splitlines()
    out: list[str] = []
    inside = False
    fenced = False
    for ln in lines:
        if ln.lstrip().startswith("```"):
            fenced = not fenced
        is_h2 = not fenced and re.match(r"##\s", ln) is not None
        if is_h2 and inside:
            break
        if is_h2 and ln.startswith("## Pipelines"):
            inside = True
        if inside:
            out.append(ln)
    assert out, "SKILL.md has no '## Pipelines' section"
    return "\n".join(out)


# ---------- R1: structural checks on the skill's wait-pipeline docs ----------
# (the prose itself is reviewed in the diff; only what can bite is asserted)


def test_skill_wait_pipeline_command_parses_with_cli_parser() -> None:
    section = _pipelines_section()
    cmds = [
        ln for ln in section.splitlines()
        if re.search(r"project-issues\s+wait-pipeline\s+--", ln)
    ]
    assert cmds, "no full `project-issues wait-pipeline --...` command in Pipelines section"
    parser = cli._build_parser()
    m = re.search(r"project-issues\s+wait-pipeline[^\n`]*", cmds[0])
    argv = shlex.split(m.group(0))[1:]
    argv = [a.replace("<id>", "acme").replace("<commit>", "abc123") for a in argv]
    ns = parser.parse_args(argv)  # every shown flag must be accepted
    assert ns.timeout == 540
    assert ns.interval == 20


_CODE = r"(?<![\w.-])([0-5])(?![\w.-])"


def test_skill_exit_codes_pair_number_with_meaning_per_line() -> None:
    section = _pipelines_section()
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
        # One code per line: a line listing several codes cannot mis-pair them.
        lines = [
            ln for ln in section.splitlines()
            if set(re.findall(_CODE, ln)) == {str(code)}
            and re.search(meaning, ln, re.I)
        ]
        assert lines, f"exit code {code} ({meaning}) not documented on its own line"
        if code == cli.EXIT_NO_VERDICT:
            assert any(
                all(re.search(w, ln, re.I) for w in ("cancelled", "timed.out", "skipped"))
                for ln in lines
            ), "exit 5 line must name cancelled / timed_out / skipped"


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
