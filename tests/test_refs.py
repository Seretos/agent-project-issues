"""Tests for the central id-normalisation layer (ticket #46)."""
from __future__ import annotations

import pytest

from lib_python_projects import ProjectConfig
from project_issues_plugin.refs import (
    GitHubRefParser,
    GitLabRefParser,
    normalize_id,
    normalize_target,
)


def _github_project(path: str = "Seretos/agent-project-issues") -> ProjectConfig:
    return ProjectConfig(id="github-tests", provider="github", path=path)


def _gitlab_project(path: str = "Seredos/gitlab-tests") -> ProjectConfig:
    return ProjectConfig(id="gitlab-tests", provider="gitlab", path=path)


# ---------- normalize_id: bare / hash / whitespace --------------------------


def test_normalize_id_bare_number():
    assert normalize_id("12", _github_project()) == "12"


def test_normalize_id_accepts_int():
    assert normalize_id(12, _github_project()) == "12"


def test_normalize_id_strips_hash_prefix():
    assert normalize_id("#12", _github_project()) == "12"


def test_normalize_id_trims_whitespace():
    assert normalize_id("  #12  ", _github_project()) == "12"


def test_normalize_id_trims_after_hash():
    assert normalize_id("# 12", _github_project()) == "12"


def test_normalize_id_returns_none_on_none():
    assert normalize_id(None, _github_project()) is None


def test_normalize_id_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        normalize_id("", _github_project())


def test_normalize_id_rejects_whitespace_only():
    with pytest.raises(ValueError, match="empty"):
        normalize_id("   ", _github_project())


def test_normalize_id_rejects_non_numeric():
    with pytest.raises(ValueError, match="bare number"):
        normalize_id("abc", _github_project())


def test_normalize_id_rejects_wrong_type():
    with pytest.raises(ValueError, match="must be"):
        normalize_id([1, 2], _github_project())  # type: ignore[arg-type]


# ---------- normalize_id: GitLab composite comment id -----------------------


def test_normalize_id_passes_through_gitlab_composite():
    # `<iid>/<note_id>` is the self-contained comment-id form get/update/
    # delete_comment document; it must survive normalisation unchanged.
    assert normalize_id("5/123", _gitlab_project()) == "5/123"


def test_normalize_id_composite_trims_inner_whitespace():
    assert normalize_id("  5 / 123  ", _gitlab_project()) == "5/123"


def test_normalize_id_rejects_three_segment_path():
    with pytest.raises(ValueError, match="bare number"):
        normalize_id("1/2/3", _gitlab_project())


def test_normalize_id_rejects_non_numeric_composite():
    with pytest.raises(ValueError, match="bare number"):
        normalize_id("a/b", _gitlab_project())


# ---------- normalize_id: GitHub URLs ---------------------------------------


def test_normalize_id_github_issue_url():
    project = _github_project()
    url = "https://github.com/Seretos/agent-project-issues/issues/46"
    assert normalize_id(url, project) == "46"


def test_normalize_id_github_pull_url():
    project = _github_project()
    url = "https://github.com/Seretos/agent-project-issues/pull/9"
    assert normalize_id(url, project) == "9"


def test_normalize_id_github_url_case_insensitive_owner_repo():
    project = _github_project()
    url = "https://github.com/SERETOS/Agent-Project-Issues/issues/46"
    assert normalize_id(url, project) == "46"


def test_normalize_id_github_url_with_anchor_and_query_strips_path_only():
    """Anchors / query strings shouldn't break parsing (urlsplit handles them)."""
    project = _github_project()
    url = "https://github.com/Seretos/agent-project-issues/issues/46#issuecomment-9999"
    assert normalize_id(url, project) == "46"


def test_normalize_id_github_url_rejects_mismatched_repo():
    project = _github_project(path="Seretos/agent-project-issues")
    url = "https://github.com/other-owner/other-repo/issues/46"
    with pytest.raises(ValueError, match="URL points to 'other-owner/other-repo'"):
        normalize_id(url, project)


def test_normalize_id_github_url_unknown_path_segment_returns_none_then_errors():
    """A github URL that isn't an issue/pull URL is treated as unparseable."""
    project = _github_project()
    url = "https://github.com/Seretos/agent-project-issues/settings/secrets"
    with pytest.raises(ValueError, match="does not look like"):
        normalize_id(url, project)


# ---------- normalize_id: GitLab URLs ---------------------------------------


def test_normalize_id_gitlab_issue_url():
    project = _gitlab_project()
    url = "https://gitlab.com/Seredos/gitlab-tests/-/issues/12"
    assert normalize_id(url, project) == "12"


def test_normalize_id_gitlab_work_item_url():
    project = _gitlab_project()
    url = "https://gitlab.com/Seredos/gitlab-tests/-/work_items/12"
    assert normalize_id(url, project) == "12"


def test_normalize_id_gitlab_merge_request_url():
    project = _gitlab_project()
    url = "https://gitlab.com/Seredos/gitlab-tests/-/merge_requests/3"
    assert normalize_id(url, project) == "3"


def test_normalize_id_gitlab_subgroup_path():
    project = _gitlab_project(path="group/sub/project")
    url = "https://gitlab.com/group/sub/project/-/issues/7"
    assert normalize_id(url, project) == "7"


def test_normalize_id_gitlab_url_rejects_mismatched_path():
    project = _gitlab_project(path="Seredos/gitlab-tests")
    url = "https://gitlab.com/other-group/other-repo/-/issues/12"
    with pytest.raises(ValueError, match="other-group/other-repo"):
        normalize_id(url, project)


def test_normalize_id_gitlab_url_case_insensitive_path():
    project = _gitlab_project(path="Seredos/gitlab-tests")
    url = "https://gitlab.com/seredos/gitlab-tests/-/issues/12"
    assert normalize_id(url, project) == "12"


# ---------- normalize_target ------------------------------------------------


def test_normalize_target_bare_number():
    assert normalize_target("12", _github_project()) == "12"


def test_normalize_target_hash_prefix():
    assert normalize_target("#12", _github_project()) == "12"


def test_normalize_target_url():
    project = _github_project()
    url = "https://github.com/Seretos/agent-project-issues/issues/46"
    assert normalize_target(url, project) == "46"


def test_normalize_target_passes_through_cross_repo_github():
    """`owner/repo#N` is preserved verbatim — upstream rejects with
    NotImplementedError; rewriting it here would erase that context."""
    project = _github_project()
    assert normalize_target("other/repo#9", project) == "other/repo#9"


def test_normalize_target_passes_through_cross_repo_gitlab():
    project = _gitlab_project()
    assert normalize_target("group/project#7", project) == "group/project#7"
    assert normalize_target("group/project!3", project) == "group/project!3"


def test_normalize_target_passes_through_gitlab_composite():
    """`<iid>/<note_id>` has a `/` but no `#`/`!`, so it falls through the
    cross-repo guard to `normalize_id`, which now passes it through."""
    assert normalize_target("5/123", _gitlab_project()) == "5/123"


def test_normalize_target_returns_none_on_none():
    assert normalize_target(None, _github_project()) is None


# ---------- parser classes directly -----------------------------------------


def test_github_parser_returns_none_for_non_github_url():
    parser = GitHubRefParser()
    assert parser.parse_url("https://example.com/foo/issues/1", _github_project()) is None


def test_github_parser_returns_none_for_non_http_scheme():
    parser = GitHubRefParser()
    assert parser.parse_url("ftp://github.com/x/y/issues/1", _github_project()) is None


def test_gitlab_parser_returns_none_when_no_minus_segment():
    parser = GitLabRefParser()
    # Older GitLab URL form without the /-/ separator — treat as unparseable.
    assert parser.parse_url("https://gitlab.com/group/project/issues/1", _gitlab_project()) is None


def test_gitlab_parser_returns_none_for_non_issue_kind():
    parser = GitLabRefParser()
    url = "https://gitlab.com/group/project/-/settings/general"
    assert parser.parse_url(url, _gitlab_project(path="group/project")) is None
