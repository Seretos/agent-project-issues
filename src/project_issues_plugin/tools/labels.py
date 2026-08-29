"""Label-catalog management tools exposed to the agent.

Wires four label-management MCP tools — `list_labels`, `create_label`,
`update_label`, `delete_label` — against the label CRUD surface already
present in lib-python-projects v0.1.6.  All provider work is delegated to
the lib; this module is wiring only.

Provider color format notes:
  - **GitHub**: `color` is a 6-digit hex string (e.g. ``"ededed"``);
    a leading '#' is accepted and stripped.
    Defaults to ``"ededed"`` when omitted on create.
  - **GitLab**: `color` is `#RRGGBB` (e.g. ``"#ff0000"``); bare 6-hex
    like ``"ff00ff"`` is also accepted and normalized to ``#RRGGBB``.
    Omit it and this tool's pinned lib-python-projects dependency
    fills in ``#ededed`` (matching GitHub's default) before the
    GitLab API call is made — this is this plugin's own default
    behavior, not a default documented by GitLab's native API.
    Validated locally before the API call.
  - **Azure DevOps**: `color` is always empty — the provider uses implicit
    tag-style labels with no color or description support.  `create_label`,
    `update_label`, and `delete_label` on Azure DevOps will always return
    ``{"error": "..."}`` containing "not supported"; `list_labels` works.
"""
from __future__ import annotations

import re
from dataclasses import asdict
from typing import Annotated

from pydantic import Field

from mcp.server.fastmcp import FastMCP

from lib_python_projects import resolve_token
from lib_python_projects.providers.azuredevops import AzureDevOpsError
from lib_python_projects.providers.github import GitHubError
from lib_python_projects.providers.gitlab import GitLabError
from project_issues_plugin.tools._providers import (
    _provider_for,
    _require_issues_modify,
    _require_token,
    _resolve,
    _rewrap_label_already_exists,
    _safe,
)

# GitHub label colors are a 6-digit hex string; an optional leading '#' is
# accepted here and stripped before the value reaches the provider (#297).
_GITHUB_HEX_COLOR = re.compile(r"^#?[0-9a-fA-F]{6}$")

# GitLab label colors are '#RRGGBB' (optional leading '#'), 6 hex digits
# only. Hex-only by explicit design decision — GitLab's real API was
# checked for CSS colour-name support and none was found in the installed
# provider; do not add colour-name handling here.
#
# 3-digit shorthand (e.g. "#fff") is deliberately NOT accepted, even though
# the ticket's original gatekeeper clarification asked for a "3-or-6 hex
# digit" rule to "avoid a regression on '#fff'-style calls that work
# today". Investigated post-review (round 2): the installed
# lib-python-projects GitLab provider's `_normalize_gitlab_color` helper
# (lib_python_projects/providers/gitlab.py) only prepends a missing '#' —
# it does not expand 3-digit shorthand to 6-digit, and its own docstring
# states the *required* wire form is `#RRGGBB` (6 digit). No test or code
# in this repo predating #265 exercises a 3-digit GitLab color either, so
# there is no evidence any "#fff"-style call "works today" against the
# real API. Accepting 3-digit shorthand here would therefore let a caller
# pass local validation and still hit a raw, unhinted GitLab API error —
# exactly the failure mode this ticket exists to close (reviewer finding,
# round 2). 6-digit-only satisfies the gatekeeper's underlying goal even
# though it deviates from the literal "3-or-6" wording.
_GITLAB_HEX_COLOR = re.compile(r"^#?[0-9a-fA-F]{6}$")


def _validate_label_name(name: str) -> None:
    """Reject an empty/whitespace label name before the provider does.

    Without this guard a blank name reaches GitHub and comes back as a
    raw 422 leaking internal field names (e.g. ``Label.name
    (missing_field)``); validating here returns a plain rule instead.
    """
    if not name or not name.strip():
        raise ValueError("label name must be non-empty")


def _normalize_github_color(color: str) -> str:
    """Reject a malformed GitHub label color before the provider does, and
    return the bare 6-hex form the provider expects.

    GitHub wants a 6-digit hex string (e.g. ``"ededed"``); an optional
    leading ``#`` is accepted here and stripped before the value is passed
    on. A wrong value otherwise surfaces as a raw GitHub 422 that leaks
    internal field names (``Label.color (invalid)``); this restates the
    documented rule the caller can act on. Only applied to the GitHub
    provider — GitLab (``#RRGGBB``) and Azure DevOps have their own
    handling.
    """
    if not _GITHUB_HEX_COLOR.match(color):
        raise ValueError(
            f"color must be a 6-digit hex string (e.g. 'ededed', optional "
            f"leading '#') on GitHub; got {color!r}"
        )
    return color[1:] if color.startswith("#") else color


def _validate_gitlab_color(color: str) -> None:
    """Reject a malformed GitLab label color before the provider does.

    GitLab wants ``#RRGGBB`` (optional leading ``#``, 6 hex digits).
    A wrong value otherwise surfaces as a raw GitLab API error with no
    format hint; this restates the documented rule the caller can act
    on. Only applied to the GitLab provider — GitHub (bare 6-hex) and
    Azure DevOps have their own handling. Hex-only; no implicit
    trimming of whitespace.
    """
    if not _GITLAB_HEX_COLOR.match(color):
        raise ValueError(
            f"color must be a hex string like '#ff0000' on GitLab; "
            f"got {color!r}"
        )


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def list_labels(project_id: str) -> dict:
        """List all labels defined in the project's repository.

        Read-only — no permission flag required (token-optional, mirrors
        `list_tickets`).  Public repositories may be queried without a
        token.

        Return shape:

        ```
        {
          "project_id": str,
          "labels": [
            {"name": str, "color": str, "description": str},
            ...
          ]
        }
        ```

        `color` semantics differ by provider:
          - GitHub: bare 6-hex string, e.g. ``"ededed"``.
          - GitLab: ``#RRGGBB``, e.g. ``"#ff0000"``; bare 6-hex like
            ``"ff00ff"`` is also accepted and normalized to ``#RRGGBB``.
          - Azure DevOps: always ``""`` (tags have no color concept).

        `description` is always a string. An empty string ``""`` is a
        sentinel that conflates two cases the payload does not
        distinguish: "no description set" (GitHub / GitLab) and
        "descriptions are unsupported" (Azure DevOps, which has no
        description concept). Treat ``""`` as "absent".
        """
        def go() -> dict:
            project = _resolve(project_id)
            token = resolve_token(project)
            provider = _provider_for(project)
            labels = provider.list_labels(project, token)
            return {
                "project_id": project.id,
                "labels": [asdict(label) for label in labels],
            }
        return _safe(go)

    @mcp.tool()
    def create_label(
        project_id: str,
        name: str,
        color: Annotated[str | None, Field(description="Label color. GitHub: 6-digit hex (e.g. 'ededed'); a leading '#' is accepted and stripped — validated locally before the API call. GitLab: '#RRGGBB' (e.g. '#ff0000'); bare 6-hex like 'ff00ff' is also accepted and normalized to '#RRGGBB' — validated locally before the API call. Azure DevOps: ignored (tags have no color concept).")] = None,
        description: str | None = None,
    ) -> dict:
        """Create a new label in the project's repository.

        Requires the project's `issues.modify` permission.

        `name` is the label name (required).

        `color` format is provider-specific:
          - GitHub: 6-digit hex string (e.g. ``"ededed"``);
            a leading '#' is accepted and stripped.
            Omit to use the GitHub default (``"ededed"``). Validated
            locally before the API call.
          - GitLab: ``#RRGGBB`` (e.g. ``"#ff0000"``); bare 6-hex like
            ``"ff00ff"`` is also accepted and normalized to ``#RRGGBB``.
            Omit it and this tool's pinned lib-python-projects
            dependency fills in ``#ededed`` (matching GitHub's
            default) before the GitLab API call is made — this is
            this plugin's own default behavior, not a default
            documented by GitLab's native API.
            Validated locally before the API call.
          - Azure DevOps: label creation is not supported — returns
            ``{"error": "..."}`` containing "not supported".

        Azure DevOps workaround: Azure has no label catalog — tags are
        freeform. There is no separate "create the label first" step.
        Attach a never-before-seen tag directly via
        ``create_ticket(labels=[...])`` or
        ``update_ticket(labels_add=[...])`` and it is created on the fly
        as part of that call.

        `description` is optional on GitHub and GitLab; ignored on Azure
        DevOps.

        Returns ``{"error": "..."}`` when:
          - The label already exists (GitHub 422 / GitLab conflict).
          - The provider does not support label creation (Azure DevOps).
          - The project lacks `issues.modify` permission.
          - No API token is configured.

        Return shape on success:

        ```
        {"project_id": str, "label": {"name": str, "color": str, "description": str}}
        ```
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_issues_modify(project)
            token = _require_token(project)
            provider = _provider_for(project)
            _validate_label_name(name)
            normalized_color = color
            if color is not None and project.provider == "github":
                normalized_color = _normalize_github_color(color)
            elif color is not None and project.provider == "gitlab":
                _validate_gitlab_color(color)
            label = provider.create_label(
                project, token, name, color=normalized_color, description=description,
            )
            return {
                "project_id": project.id,
                "label": asdict(label),
            }
        return _safe(go)

    @mcp.tool()
    def update_label(
        project_id: str,
        name: Annotated[str, Field(description="Name of the label to look up (lookup key only — never mutated by this call). To rename the label, supply `new_name`.")],
        new_name: Annotated[str | None, Field(description="New name for the label (renames it). Leave unset to keep the current name.")] = None,
        color: Annotated[str | None, Field(description="Label color. GitHub: 6-digit hex (e.g. 'ededed'); a leading '#' is accepted and stripped — validated locally before the API call. GitLab: '#RRGGBB' (e.g. '#ff0000'); bare 6-hex like 'ff00ff' is also accepted and normalized to '#RRGGBB' — validated locally before the API call. Azure DevOps: ignored (tags have no color concept).")] = None,
        description: str | None = None,
    ) -> dict:
        """Rename or recolour an existing label.

        Requires the project's `issues.modify` permission.

        `name` is the label's **current** name (required), used only to
        look it up — it is never changed by this call. To rename the
        label, pass the desired name as `new_name`; leave `new_name`
        unset to keep the current name and only change `color` /
        `description`.

        At least one of `new_name`, `color`, or `description` must be
        supplied; passing none returns ``{"error": "..."}`` without making
        any HTTP call.

        `color` format is provider-specific (see `create_label` docs).

        Azure DevOps: not supported (tags are freeform, not a mutable
        catalog entry) — see the freeform-tag workaround documented on
        `create_label`.

        Returns ``{"error": "..."}`` when:
          - None of `new_name` / `color` / `description` is supplied.
          - The label does not exist (404).
          - The provider does not support label mutation (Azure DevOps).
          - The project lacks `issues.modify` permission.
          - No API token is configured.

        Return shape on success:

        ```
        {"project_id": str, "label": {"name": str, "color": str, "description": str}}
        ```
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_issues_modify(project)
            token = _require_token(project)
            provider = _provider_for(project)
            _validate_label_name(name)
            if new_name is not None:
                _validate_label_name(new_name)
            normalized_color = color
            if color is not None and project.provider == "github":
                normalized_color = _normalize_github_color(color)
            elif color is not None and project.provider == "gitlab":
                _validate_gitlab_color(color)
            try:
                label = provider.update_label(
                    project, token, name,
                    new_name=new_name, color=normalized_color, description=description,
                )
            except (GitHubError, GitLabError, AzureDevOpsError) as exc:
                raise _rewrap_label_already_exists(exc, new_name=new_name)
            return {
                "project_id": project.id,
                "label": asdict(label),
            }
        return _safe(go)

    @mcp.tool()
    def delete_label(project_id: str, name: str) -> dict:
        """Delete a label from the project's repository.

        Requires the project's `issues.modify` permission.

        `name` identifies the label to delete (required).

        Azure DevOps: not supported (tags are freeform, not a mutable
        catalog entry) — see the freeform-tag workaround documented on
        `create_label`.

        Returns ``{"error": "..."}`` when:
          - The label does not exist (404).
          - The provider does not support label deletion (Azure DevOps).
          - The project lacks `issues.modify` permission.
          - No API token is configured.

        Return shape on success:

        ```
        {"project_id": str, "deleted": true, "name": str}
        ```
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_issues_modify(project)
            token = _require_token(project)
            provider = _provider_for(project)
            provider.delete_label(project, token, name)
            return {
                "project_id": project.id,
                "deleted": True,
                "name": name,
            }
        return _safe(go)
