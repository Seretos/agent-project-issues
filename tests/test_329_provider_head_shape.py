"""Ticket #329 — premise pin for the documented `head` sub-keys.

The write-tool descriptions name exactly `head.ref`, `head.sha` and
`head.repo_full_name`. That is only true while every provider's PR mapper
builds a `head` with exactly those three keys, so a lib bump that reshapes
`head` fails here instead of silently falsifying the descriptions.

Retrospective premise pin: these assertions already hold on the pinned lib
today (the RED driver for the documentation is
`tests/test_314_write_response_light.py`).
"""
from __future__ import annotations

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import azuredevops, github, gitlab

_HEAD_KEYS = {"ref", "sha", "repo_full_name"}


def _project(provider: str) -> ProjectConfig:
    return ProjectConfig(
        id="acme", provider=provider, path="acme/backend", token_env="T",
    )


def test_github_map_pr_head_keys():
    raw = {
        "number": 7, "title": "t", "body": "", "state": "open",
        "head": {"ref": "f", "sha": "s", "repo": {"full_name": "acme/backend"}},
        "base": {"ref": "main", "sha": "b"},
        "user": {"login": "a"},
    }
    assert set(github._map_pr(raw).head) == _HEAD_KEYS


def test_gitlab_map_mr_head_keys():
    raw = {
        "iid": 7, "title": "t", "description": "", "state": "opened",
        "source_branch": "f", "target_branch": "main", "sha": "s",
        "source_project_id": 1, "target_project_id": 1,
        "author": {"username": "a"},
    }
    pr = gitlab._map_mr(raw, _project("gitlab"))
    assert set(pr.head) == _HEAD_KEYS


def test_gitlab_repo_full_name_is_none_without_project():
    raw = {
        "iid": 7, "title": "t", "description": "", "state": "opened",
        "source_branch": "f", "target_branch": "main", "sha": "s",
        "author": {"username": "a"},
    }
    pr = gitlab._map_mr(raw, None)
    assert set(pr.head) == _HEAD_KEYS
    assert pr.head["repo_full_name"] is None


def test_azuredevops_map_pr_head_keys():
    raw = {
        "pullRequestId": 7, "title": "t", "description": "", "status": "active",
        "sourceRefName": "refs/heads/f", "targetRefName": "refs/heads/main",
        "lastMergeSourceCommit": {"commitId": "s"},
        "createdBy": {"displayName": "a"},
    }
    project = ProjectConfig(
        id="acme", provider="azuredevops", path="org/proj/repo", token_env="T",
    )
    pr = azuredevops._map_pr(raw, project)
    assert set(pr.head) == _HEAD_KEYS
