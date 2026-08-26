"""Behavioural driving tests for WP #291's new `list_pr_files` tool.

Covers R1 (targets exist + shape), R2 (patch knobs), R3 (side pass-through
matches the lib's own hunk parser), and R13 (the `line_ranges` tri-state:
`None` vs `[]` vs non-empty).

Two harness routes, per the plan:
  - HTTP-stub route (`_register_tools_with` + `_install_mock`), copied from
    `tests/test_pulls.py`'s helpers — drives a real `GitHubProvider` against
    a mocked transport so `line_ranges` come from the lib's actual
    `parse_diff_hunk_ranges`.
  - Fake-provider route (`_register_fake_provider_tools`), copied from the
    `monkeypatch.setitem(providers_mod._PROVIDERS, ...)` pattern in
    `tests/test_265_client_side_validation.py` — used for the Azure
    (`SUPPORTS_DIFF_LINE_RANGES=False`) semantics and the `[]` leg, since
    those need direct control over the `PRFileDiff` values returned rather
    than a plausible-looking HTTP payload.

Not yet implemented: `list_pr_files` does not exist on `tools/pulls.py`
today, so every test in this file is expected to fail with
`KeyError: 'list_pr_files'` when `tools["list_pr_files"]` is looked up —
that is the expected RED reason for this whole file until the
`phase=implement` dispatch adds the tool.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers.base import PRFileDiff, parse_diff_hunk_ranges
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import pulls as pull_tools


# ---------- shared helpers (copied from tests/test_pulls.py) -----------------


def _project(
    *, provider: str = "github", project_id: str = "acme",
) -> ProjectConfig:
    # azuredevops requires 'organization/project/repository' (test_257's
    # _project() established this same pattern) -- the github-shaped
    # "acme/backend" only satisfies github/gitlab's validator.
    path = (
        "myorg/myproject/myrepo" if provider == "azuredevops"
        else f"{project_id}/backend"
    )
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


def _file_payload(
    filename: str,
    *,
    status: str = "modified",
    patch: str | None = None,
    additions: int = 0,
    deletions: int = 0,
    previous_filename: str | None = None,
) -> dict:
    row: dict = {
        "filename": filename,
        "status": status,
        "additions": additions,
        "deletions": deletions,
    }
    if patch is not None:
        row["patch"] = patch
    if previous_filename is not None:
        row["previous_filename"] = previous_filename
    return row


def _json(payload, status_code: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def _install_mock(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
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


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _register_tools_with(monkeypatch: pytest.MonkeyPatch, project: ProjectConfig):
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=[project], state="ok", search_root="/tmp",
        )

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setattr(pull_tools, "load_projects", fake_load_projects)

    stub = _StubMCP()
    pull_tools.register(stub)
    return stub.tools


def _register_fake_provider_tools(
    monkeypatch: pytest.MonkeyPatch, project: ProjectConfig, provider_instance,
):
    """Fake-provider route (test_265's `_PROVIDERS` monkeypatch pattern)."""
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=[project], state="ok", search_root="/tmp",
        )

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setattr(pull_tools, "load_projects", fake_load_projects)
    monkeypatch.setitem(providers_mod._PROVIDERS, project.provider, provider_instance)

    stub = _StubMCP()
    pull_tools.register(stub)
    return stub.tools


class _FakeDiffProvider:
    """Records nothing — just serves canned `PRFileDiff` rows so R13's
    tri-state can be asserted precisely without a plausible HTTP payload."""

    def __init__(self, files: list[PRFileDiff], *, supports: bool = True) -> None:
        self._files = files
        self.SUPPORTS_DIFF_LINE_RANGES = supports

    def list_pr_files(self, project_, token, pr_id):
        return self._files


# ---------- R1 — tool exists, returns diff targets ---------------------------


def test_list_pr_files_returns_diff_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Driving test for R1. RED today: `KeyError: 'list_pr_files'` — the
    tool is not registered on `tools/pulls.py` yet."""
    tools = _register_tools_with(monkeypatch, _project())
    patch = "@@ -10,3 +10,5 @@\n context\n-old\n+new1\n+new2\n context"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/pulls/7/files":
            return _json([
                _file_payload("src/foo.py", status="modified", patch=patch, additions=2, deletions=1),
            ])
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_pr_files"](project_id="acme", pr_id="7")
    assert "error" not in result, result
    assert result["project_id"] == "acme"
    assert result["supports_line_ranges"] is True
    files = result["files"]
    assert len(files) == 1
    row = files[0]
    assert row["path"] == "src/foo.py"
    assert row["change_type"] == "modified"
    assert {"side": "RIGHT", "start": 10, "end": 14} in row["line_ranges"]


def test_list_pr_files_404_names_project_and_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Edge case for R1: a 404 is rewrapped naming `acme#7`."""
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/pulls/7/files":
            return _json({"message": "Not Found"}, status_code=404)
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_pr_files"](project_id="acme", pr_id="7")
    assert "error" in result
    assert "acme#7" in result["error"]


def test_list_pr_files_empty_payload_returns_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Edge case for R1: no changed files -> `files: []`."""
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/pulls/7/files":
            return _json([])
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_pr_files"](project_id="acme", pr_id="7")
    assert "error" not in result, result
    assert result["files"] == []


def test_list_pr_files_previous_path_none_for_non_rename(monkeypatch: pytest.MonkeyPatch) -> None:
    """Edge case for R1: `previous_path` is present and `None` for a non-rename."""
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/pulls/7/files":
            return _json([_file_payload("src/foo.py", status="modified")])
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_pr_files"](project_id="acme", pr_id="7")
    assert "error" not in result, result
    row = result["files"][0]
    assert "previous_path" in row and row["previous_path"] is None


def test_list_pr_files_previous_path_set_for_rename(monkeypatch: pytest.MonkeyPatch) -> None:
    """Edge case for R1: a rename row carries the old path + change_type."""
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/pulls/7/files":
            return _json([
                _file_payload("new/name.py", status="renamed", previous_filename="old/name.py"),
            ])
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_pr_files"](project_id="acme", pr_id="7")
    assert "error" not in result, result
    row = result["files"][0]
    assert row["previous_path"] == "old/name.py"
    assert row["change_type"] == "renamed"


# ---------- R2 — patch omitted by default, slice-able on request -------------


def test_list_pr_files_default_omits_patch_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Driving test for R2. RED today: `KeyError: 'list_pr_files'`."""
    tools = _register_tools_with(monkeypatch, _project())
    patch = "@@ -1,1 +1,2 @@\n-a\n+a\n+b"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/pulls/7/files":
            return _json([_file_payload("src/foo.py", patch=patch)])
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_pr_files"](project_id="acme", pr_id="7")
    assert "error" not in result, result
    assert "patch" not in result["files"][0]


def test_list_pr_files_include_patch_true_returns_full_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _register_tools_with(monkeypatch, _project())
    patch = "@@ -1,1 +1,2 @@\n-a\n+a\n+b"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/pulls/7/files":
            return _json([_file_payload("src/foo.py", patch=patch)])
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_pr_files"](project_id="acme", pr_id="7", include_patch=True)
    assert "error" not in result, result
    assert result["files"][0]["patch"] == patch


def test_list_pr_files_include_patch_and_max_chars_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _register_tools_with(monkeypatch, _project())
    patch = "0123456789ABCDE"  # 15 chars, no hunk header needed for this check

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/pulls/7/files":
            return _json([_file_payload("src/foo.py", patch=patch)])
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_pr_files"](
        project_id="acme", pr_id="7", include_patch=True, patch_max_chars=10,
    )
    assert "error" not in result, result
    row = result["files"][0]
    assert row["patch"] == patch[:10]
    assert row["patch_truncated"] is True


def test_list_pr_files_patch_max_chars_alone_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Edge case for R2: `patch_max_chars` without `include_patch=True` changes nothing."""
    tools = _register_tools_with(monkeypatch, _project())
    patch = "0123456789ABCDE"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/pulls/7/files":
            return _json([_file_payload("src/foo.py", patch=patch)])
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result_default = tools["list_pr_files"](project_id="acme", pr_id="7")
    result_with_max = tools["list_pr_files"](project_id="acme", pr_id="7", patch_max_chars=5)
    assert "error" not in result_default, result_default
    assert "error" not in result_with_max, result_with_max
    assert result_default == result_with_max
    assert "patch" not in result_with_max["files"][0]
    assert "patch_truncated" not in result_with_max["files"][0]


def test_list_pr_files_binary_file_patch_null_with_include_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Edge case for R2: a binary file (no `patch` key from GitHub) stays
    `patch: null` even under `include_patch=True`, with no truncation flag."""
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/pulls/7/files":
            return _json([_file_payload("assets/logo.png", status="modified")])
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_pr_files"](
        project_id="acme", pr_id="7", include_patch=True, patch_max_chars=10,
    )
    assert "error" not in result, result
    row = result["files"][0]
    assert "patch" in row and row["patch"] is None
    assert "patch_truncated" not in row
    assert row["line_ranges"] == []


def test_list_pr_files_patch_max_chars_not_truncated_when_over_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case for R2: `patch_max_chars` >= len(patch) => full patch, `patch_truncated=False`."""
    tools = _register_tools_with(monkeypatch, _project())
    patch = "short"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/pulls/7/files":
            return _json([_file_payload("src/foo.py", patch=patch)])
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_pr_files"](
        project_id="acme", pr_id="7", include_patch=True, patch_max_chars=100,
    )
    assert "error" not in result, result
    row = result["files"][0]
    assert row["patch"] == patch
    assert row["patch_truncated"] is False


# ---------- R3 — side passed through verbatim, matches the lib parser --------


def test_list_pr_files_line_ranges_match_lib_parser_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Driving test for R3: exact structural equality against the real
    `parse_diff_hunk_ranges` output — not hand-computed expected ranges."""
    tools = _register_tools_with(monkeypatch, _project())
    patch = "@@ -10,3 +20,4 @@\n-old\n+new\n context\n+more"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/pulls/7/files":
            return _json([_file_payload("src/foo.py", patch=patch, additions=2, deletions=1)])
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_pr_files"](project_id="acme", pr_id="7")
    assert "error" not in result, result
    row = result["files"][0]
    expected = [asdict(r) for r in parse_diff_hunk_ranges(patch)]
    assert row["line_ranges"] == expected
    sides = {r["side"] for r in row["line_ranges"]}
    assert sides == {"LEFT", "RIGHT"}


def test_list_pr_files_pure_addition_hunk_is_right_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Edge case for R3: a pure-addition hunk (`b == 0`) emits RIGHT only."""
    tools = _register_tools_with(monkeypatch, _project())
    patch = "@@ -5,0 +6,3 @@\n+a\n+b\n+c"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/pulls/7/files":
            return _json([_file_payload("src/new.py", status="added", patch=patch, additions=3)])
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_pr_files"](project_id="acme", pr_id="7")
    assert "error" not in result, result
    row = result["files"][0]
    assert len(row["line_ranges"]) == 1
    assert all(r["side"] == "RIGHT" for r in row["line_ranges"])


# ---------- R13 — the line_ranges tri-state is observable end-to-end ---------


def test_list_pr_files_line_ranges_empty_list_when_supported_but_no_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for R13's `[]` leg: a provider that supports positions
    in general but has nothing to report for this (binary) file."""
    project = _project()
    fake = _FakeDiffProvider(
        [
            PRFileDiff(
                path="assets/logo.png", change_type="modified", previous_path=None,
                patch=None, line_ranges=[], additions=0, deletions=0,
            ),
        ],
        supports=True,
    )
    tools = _register_fake_provider_tools(monkeypatch, project, fake)
    result = tools["list_pr_files"](project_id="acme", pr_id="7")
    assert "error" not in result, result
    assert result["supports_line_ranges"] is True
    row = result["files"][0]
    assert "line_ranges" in row
    assert row["line_ranges"] == []
    assert row["line_ranges"] is not None


def test_list_pr_files_line_ranges_null_when_provider_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for R13's `null` leg: an Azure-style provider that
    cannot supply positions at all."""
    project = _project(provider="azuredevops")
    fake = _FakeDiffProvider(
        [
            PRFileDiff(
                path="src/foo.cs", change_type="modified", previous_path=None,
                patch=None, line_ranges=None, additions=1, deletions=1,
            ),
        ],
        supports=False,
    )
    tools = _register_fake_provider_tools(monkeypatch, project, fake)
    result = tools["list_pr_files"](project_id="acme", pr_id="7")
    assert "error" not in result, result
    assert result["supports_line_ranges"] is False
    row = result["files"][0]
    assert "line_ranges" in row
    assert row["line_ranges"] is None
    # Distinguishability, asserted directly per the plan.
    assert row["line_ranges"] != []
