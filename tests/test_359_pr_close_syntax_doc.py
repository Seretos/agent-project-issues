"""Driving tests for ticket #359: `create_pr` / `update_pr`'s served
descriptions say nothing about what a PR `body` must contain to link or
close a ticket on merge, so an agent writes GitHub's `Closes #<n>` on an
Azure DevOps project — which closes nothing there. This adds one
identical paragraph to both docstrings, stating the per-provider rule.

Every test reads the text FastMCP actually serves — `FastMCP("t")` +
`pulls.register(mcp)` + `asyncio.run(mcp.list_tools())` + `.description` —
mirroring `tests/test_358_merge_state_semantics_doc.py`'s style.

- R1: both `create_pr` and `update_pr` carry the block, with one bullet
  per provider (GitHub/GitLab: `Closes #<n>` + `default branch`; Azure
  DevOps: `#<n>`, `does not complete`, `update_ticket`,
  `list_ticket_statuses`, and the "no keyword closes" claim only ever
  appears in its negated form), `AB#` appears nowhere, and the block is
  byte-identical between the two tools.
- R2: the Azure claim ("merge_pr does not complete linked work items")
  matches the pinned lib's real `create_pr`/`merge_pr` requests (no
  `workItemRefs`, no `transitionWorkItems`), and `#42` in `body` survives
  Azure's markdown->HTML conversion into `description`. GitHub/GitLab's
  `create_pr` forwards `Closes #42` through unchanged (edge-case
  coverage; platform auto-close itself is not code in this repo and is
  not re-verified here, exactly as `get_pr`'s merge-state table treats
  GitHub/GitLab auto-close as a platform fact).

Both are expected to fail RED for the same reason before `pulls.py`
gains the paragraph: "close-syntax block missing: no 'Linking / closing
a ticket on merge' paragraph in <tool> description".

Plan-critic notes verified against the pinned `lib_python_projects`
0.3.22 while writing these tests (all minor/non-blocking; recorded here
so the implement phase can see what was actually checked):
  - Azure "commit mention" resolution (a project-level Azure Repos
    setting that can close a work item from a keyword in a *commit*
    message, independent of any REST payload field) is a real,
    distinct mechanism from `workItemRefs`/`transitionWorkItems`. It is
    not exercised by any assertion below: `merge_pr`'s `commit_message`
    parameter does feed `completionOptions.mergeCommitMessage` (see the
    real PATCH body captured in
    `test_azure_merge_and_create_send_no_work_item_completion`), but
    whether Azure's backend later scans that merge commit's message for
    an `AB#<n>` mention is a platform behaviour with no HTTP call this
    test suite can observe. The docstring's Azure bullet is written and
    tested as a claim about `body`, not `commit_message` — the "no
    keyword closes one" sentence should be read in that scope.
  - The Azure `#<n>` "links work item" half is, like GitHub/GitLab's
    auto-close, a platform fact this repo's tests cannot verify end to
    end; `test_azure_merge_and_create_send_no_work_item_completion`
    only proves the literal text survives the HTML round-trip into
    `description`, not that ADO resolves it into a work-item link.
  - Mixed setups (ticket tracker on Azure Boards, PRs on a GitHub-hosted
    repo) are not reachable through this plugin: `ProjectConfig` carries
    exactly one `provider` field and one `path`, used for both tickets
    and PRs on that single project entry (see
    `lib_python_projects.models.ProjectConfig`). A project configured
    `provider="azuredevops"` always means Azure Repos for PRs, so the
    unconditional "no AB#" framing is safe.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import azuredevops
from lib_python_projects.providers.azuredevops import AzureDevOpsProvider
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers.github import GitHubProvider
from lib_python_projects.providers import gitlab as gitlab_provider
from lib_python_projects.providers.gitlab import GitLabProvider
from mcp.server.fastmcp import FastMCP

from project_issues_plugin.tools import pulls as pulls_tools

TOOL_NAMES = ("create_pr", "update_pr")


# ---------- served-description helpers ---------------------------------------


def _served_descriptions() -> dict[str, str]:
    mcp = FastMCP("t")
    pulls_tools.register(mcp)
    tools = asyncio.run(mcp.list_tools())
    return {t.name: (t.description or "") for t in tools}


_BLOCK_START = "Linking / closing a ticket on merge"
_BLOCK_END_MARKER = "rewrites these keywords in `body`."


def _close_syntax_block(description: str, tool_name: str) -> str:
    """Return the close-syntax paragraph — from "Linking / closing a
    ticket on merge" up to and including "...rewrites these keywords in
    `body`." — raising with the expected RED reason when it's absent."""
    start = description.find(_BLOCK_START)
    if start == -1:
        raise AssertionError(
            "close-syntax block missing: no 'Linking / closing a ticket "
            f"on merge' paragraph in {tool_name} description:\n{description}"
        )
    end_marker_idx = description.find(_BLOCK_END_MARKER, start)
    if end_marker_idx == -1:
        raise AssertionError(
            "close-syntax block missing: no terminating "
            "'...rewrites these keywords in `body`.' sentence found after "
            f"the opening paragraph in {tool_name} description:\n{description}"
        )
    return description[start : end_marker_idx + len(_BLOCK_END_MARKER)]


_BULLET_MARKERS = ("- GitHub:", "- GitLab:", "- Azure DevOps:")
# Ticket #359 test-critic round 1 (tautology::F1): the Azure bullet has no
# following bullet marker, so without this boundary its slice would run to
# the end of the block and sweep in the closing fallback sentence ("If a
# ticket must be closed ... rewrites these keywords in `body`."), which also
# mentions `update_ticket` — letting a surviving implementation satisfy the
# Azure bullet's `update_ticket` requirement via the fallback sentence
# instead of the bullet's own text. Bounding the slice here means every
# required phrase is checked against the Azure bullet itself.
_BULLET_TRAILING_MARKER = "If a ticket must be closed"


def _bullet(block: str, marker: str) -> str:
    """Return one provider's bullet from the close-syntax block, bounded
    by the next bullet marker or (for the last bullet) the block's
    trailing fallback sentence — never sweeping either in."""
    idx = block.find(marker)
    if idx == -1:
        raise AssertionError(f"no {marker!r} bullet found in close-syntax block:\n{block}")
    rest = block[idx:]
    end = len(rest)
    for other in _BULLET_MARKERS:
        if other == marker:
            continue
        other_idx = rest.find(other, len(marker))
        if other_idx != -1 and other_idx < end:
            end = other_idx
    trailing_idx = rest.find(_BULLET_TRAILING_MARKER, len(marker))
    if trailing_idx != -1 and trailing_idx < end:
        end = trailing_idx
    return rest[:end]


# ---------- R1: both tools state the per-provider rule ------------------------


@pytest.mark.parametrize("tool_name", TOOL_NAMES)
def test_close_syntax_block_per_provider(tool_name: str) -> None:
    """Driving test for R1: `create_pr`/`update_pr`'s served description
    carries the close-syntax block, with one bullet per provider whose
    content matches what this plugin's create_pr/merge_pr path actually
    does — not merely a block that exists."""
    description = _served_descriptions()[tool_name]
    block = _close_syntax_block(description, tool_name)

    github_bullet = _bullet(block, "- GitHub:")
    assert "Closes #<n>" in github_bullet, (
        f"{tool_name}: GitHub bullet missing 'Closes #<n>':\n{github_bullet}"
    )
    assert "default branch" in github_bullet, (
        f"{tool_name}: GitHub bullet missing 'default branch':\n{github_bullet}"
    )

    gitlab_bullet = _bullet(block, "- GitLab:")
    assert "Closes #<n>" in gitlab_bullet, (
        f"{tool_name}: GitLab bullet missing 'Closes #<n>':\n{gitlab_bullet}"
    )
    assert "default branch" in gitlab_bullet, (
        f"{tool_name}: GitLab bullet missing 'default branch':\n{gitlab_bullet}"
    )

    azure_bullet = _bullet(block, "- Azure DevOps:")
    for required in ("#<n>", "does not complete", "update_ticket", "list_ticket_statuses"):
        assert required in azure_bullet, (
            f"{tool_name}: Azure DevOps bullet missing {required!r}:\n{azure_bullet}"
        )
    # The "closes" claim may only appear in its negated form — a bare
    # "Closes #<n>" in the Azure bullet would (wrongly) read as the same
    # working keyword GitHub/GitLab document.
    assert azure_bullet.count("Closes #<n>") == 1, (
        f"{tool_name}: Azure DevOps bullet must mention the literal "
        f"'Closes #<n>' exactly once (inside a negation), got "
        f"{azure_bullet.count('Closes #<n>')}:\n{azure_bullet}"
    )
    assert "neither `Closes #<n>`" in azure_bullet, (
        f"{tool_name}: Azure DevOps bullet's 'Closes #<n>' mention must be "
        f"the negated form ('neither `Closes #<n>` ...'):\n{azure_bullet}"
    )

    assert "AB#" not in description, (
        f"{tool_name}: description must never document 'AB#' as a working "
        f"keyword (this plugin's Azure DevOps path is always Azure Repos, "
        f"never a GitHub-hosted repo with Azure Boards):\n{description}"
    )


def test_close_syntax_block_identical() -> None:
    """Driving test for R1: the close-syntax block is byte-identical
    between `create_pr` and `update_pr` — a shared constant isn't used
    (FastMCP reads `__doc__` at decoration time), so this is the guard
    against the two copies drifting apart."""
    descriptions = _served_descriptions()
    create_block = _close_syntax_block(descriptions["create_pr"], "create_pr")
    update_block = _close_syntax_block(descriptions["update_pr"], "update_pr")
    assert create_block == update_block, (
        "close-syntax block differs between create_pr and update_pr:\n"
        f"create_pr:\n{create_block}\n\nupdate_pr:\n{update_block}"
    )


# ---------- R2: the Azure claim matches the real create_pr/merge_pr path ------


def _azure_project() -> ProjectConfig:
    return ProjectConfig(id="acme", provider="azuredevops", path="org/project/repo")


def test_azure_merge_and_create_send_no_work_item_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for R2: drives the real `AzureDevOpsProvider.create_pr`
    and `merge_pr` against a capturing `httpx.MockTransport`, mirroring the
    `_client`/`_resolve_repository_id`/`_MERGE_SETTLE_DELAYS_MS`
    monkeypatching in `tests/test_358_merge_state_semantics_doc.py` (l.
    432-453). The code-side assertions (no `workItemRefs`, no
    `transitionWorkItems`, `#42` surviving the HTML round-trip) already
    pass today — they're a guard: a future lib bump that adds either
    field must fail this test and force a doc update. Only the final
    assertion (the served Azure bullet says `does not complete`) is
    expected to be RED today, for the same "close-syntax block missing"
    reason as R1.
    """
    captured: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if method == "POST" and path.endswith("/pullrequests"):
            body = json.loads(request.content)
            captured["create"] = body
            return httpx.Response(
                200,
                json={
                    "pullRequestId": 42,
                    "status": "active",
                    "sourceRefName": body["sourceRefName"],
                    "targetRefName": body["targetRefName"],
                    "title": body["title"],
                    "description": body["description"],
                },
            )
        if method == "POST" and path.endswith("/labels"):
            # Best-effort label application — a 404 is swallowed by
            # `_add_pr_label_best_effort`, so the PR create still succeeds.
            return httpx.Response(404, json={"message": "not found"})
        if method == "GET" and path.endswith("/pullrequests/42"):
            return httpx.Response(
                200,
                json={
                    "status": "active",
                    "mergeStatus": "notSet",
                    "lastMergeSourceCommit": {"commitId": "c"},
                },
            )
        if method == "PATCH" and path.endswith("/pullrequests/42"):
            body = json.loads(request.content)
            captured["merge"] = body
            return httpx.Response(
                200,
                json={
                    "pullRequestId": 42,
                    "status": "completed",
                    "mergeStatus": "succeeded",
                    "lastMergeSourceCommit": {"commitId": "c"},
                },
            )
        raise AssertionError(f"unexpected request: {method} {request.url}")

    def fake_client(project, token, *, base_url=None):
        return httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url=base_url or "https://dev.azure.com",
        )

    monkeypatch.setattr(azuredevops, "_client", fake_client)
    provider = AzureDevOpsProvider()
    monkeypatch.setattr(provider, "_resolve_repository_id", lambda project, token: "repo")
    monkeypatch.setattr(provider, "_MERGE_SETTLE_DELAYS_MS", (0,))
    project = _azure_project()

    provider.create_pr(
        project, "tok", "t", "See #42 for details.", "feat/x", "main", light=True,
    )
    provider.merge_pr(project, "tok", "42", light=True)

    create_body = captured["create"]
    assert "workItemRefs" not in create_body, (
        f"create_pr must not send workItemRefs (no work-item linking API "
        f"field exists in this plugin's payload): {create_body}"
    )
    assert "#42" in create_body["description"], (
        f"create_pr's description must still carry '#42' after markdown->"
        f"HTML conversion: {create_body['description']!r}"
    )

    merge_body = captured["merge"]
    completion_options = merge_body.get("completionOptions") or {}
    assert "transitionWorkItems" not in completion_options, (
        f"merge_pr must not send transitionWorkItems (this plugin's "
        f"merge_pr never completes linked work items): {completion_options}"
    )

    description = _served_descriptions()["create_pr"]
    block = _close_syntax_block(description, "create_pr")
    azure_bullet = _bullet(block, "- Azure DevOps:")
    assert "does not complete" in azure_bullet, (
        f"Azure DevOps bullet must say 'does not complete' (matching the "
        f"real create_pr/merge_pr requests captured above, which carry no "
        f"work-item-completion fields):\n{azure_bullet}"
    )


# ---------- Additional coverage: GitHub/GitLab forward the keyword unchanged --


def _github_project() -> ProjectConfig:
    return ProjectConfig(id="acme", provider="github", path="acme/backend")


def _gitlab_project() -> ProjectConfig:
    return ProjectConfig(id="acme", provider="gitlab", path="group/proj")


def test_github_gitlab_create_pr_forwards_closes_keyword(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additional coverage for R2: drives the real `GitHubProvider.create_pr`
    and `GitLabProvider.create_pr` against a capturing `httpx.MockTransport`
    and asserts the sent body/description still contains `Closes #42`
    unchanged after the `#ai-generated` marker prefix — grounding the
    GitHub/GitLab bullets' claim that this plugin never rewrites the
    keyword. Platform auto-close itself (GitHub/GitLab actually closing
    the issue on merge) is not exercised here — it's platform behaviour,
    not code in this repo, exactly as the plan and `get_pr`'s
    merge-state-table tests treat it."""
    body_text = "Closes #42\n\nDetails."

    # ---- GitHub ----
    github_captured: dict[str, dict] = {}

    def gh_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if method == "POST" and path == "/repos/acme/backend/labels":
            return httpx.Response(201, json={"name": "ai-generated"})
        if method == "POST" and path == "/repos/acme/backend/pulls":
            payload = json.loads(request.content)
            github_captured["body"] = payload
            return httpx.Response(
                201,
                json={
                    "number": 42,
                    "title": "t",
                    "body": payload["body"],
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
                        "ref": "feat/x", "sha": "deadbeef",
                        "repo": {"full_name": "acme/backend"},
                    },
                    "base": {"ref": "main", "sha": "cafebabe"},
                    "html_url": "https://github.com/acme/backend/pull/42",
                    "created_at": "2024-01-01T00:00:00Z",
                    "updated_at": "2024-01-02T00:00:00Z",
                },
            )
        if method == "POST" and path == "/repos/acme/backend/issues/42/labels":
            payload = json.loads(request.content)
            return httpx.Response(200, json=[{"name": n} for n in payload["labels"]])
        raise AssertionError(f"unexpected GitHub request: {method} {request.url}")

    def gh_fake_client(token: str | None) -> httpx.Client:
        return httpx.Client(
            base_url=github_provider.API_BASE,
            transport=httpx.MockTransport(gh_handler),
        )

    monkeypatch.setattr(github_provider, "_client", gh_fake_client)
    GitHubProvider().create_pr(
        _github_project(), "tok", "t", body_text, "feat/x", "main",
    )
    assert "Closes #42" in github_captured["body"]["body"], (
        f"GitHub create_pr must forward 'Closes #42' unchanged: "
        f"{github_captured['body']['body']!r}"
    )

    # ---- GitLab ----
    gitlab_captured: dict[str, dict] = {}

    def gl_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/merge_requests"):
            payload = json.loads(request.content)
            gitlab_captured["body"] = payload
            return httpx.Response(
                201,
                json={
                    "iid": 42,
                    "state": "opened",
                    "source_branch": "feat/x",
                    "target_branch": "main",
                    "description": payload["description"],
                    "web_url": "https://gitlab.example.com/group/proj/-/merge_requests/42",
                },
            )
        raise AssertionError(f"unexpected GitLab request: {request.method} {request.url}")

    def gl_fake_client(project: ProjectConfig, token: str | None) -> httpx.Client:
        return httpx.Client(
            base_url="https://gitlab.example.com/api/v4",
            transport=httpx.MockTransport(gl_handler),
        )

    monkeypatch.setattr(gitlab_provider, "_client", gl_fake_client)
    GitLabProvider().create_pr(
        _gitlab_project(), "tok", "t", body_text, "feat/x", "main",
    )
    assert "Closes #42" in gitlab_captured["body"]["description"], (
        f"GitLab create_pr must forward 'Closes #42' unchanged: "
        f"{gitlab_captured['body']['description']!r}"
    )
