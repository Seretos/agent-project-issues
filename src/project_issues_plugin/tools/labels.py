"""Label-catalog management tools exposed to the agent.

Wires four label-management MCP tools — `list_labels`, `create_label`,
`update_label`, `delete_label` — against the label CRUD surface already
present in lib-python-projects v0.1.6.  All provider work is delegated to
the lib; this module is wiring only.

Provider color format notes:
  - **GitHub**: `color` is a 6-hex string *without* `#` (e.g. ``"ededed"``).
    Defaults to ``"ededed"`` when omitted on create.
  - **GitLab**: `color` is `#RRGGBB` (e.g. ``"#ff0000"``).
  - **Azure DevOps**: `color` is always empty — the provider uses implicit
    tag-style labels with no color or description support.  `create_label`,
    `update_label`, and `delete_label` on Azure DevOps will always return
    ``{"error": "..."}`` containing "not supported"; `list_labels` works.
"""
from __future__ import annotations

from dataclasses import asdict

from mcp.server.fastmcp import FastMCP

from lib_python_projects import resolve_token
from project_issues_plugin.tools._providers import (
    _provider_for,
    _require_issues_modify,
    _require_token,
    _resolve,
    _safe,
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
          - GitLab: ``#RRGGBB``, e.g. ``"#ff0000"``.
          - Azure DevOps: always ``""`` (tags have no color concept).
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
        color: str | None = None,
        description: str | None = None,
    ) -> dict:
        """Create a new label in the project's repository.

        Requires the project's `issues.modify` permission.

        `name` is the label name (required).

        `color` format is provider-specific:
          - GitHub: 6-hex string *without* ``#`` (e.g. ``"ededed"``).
            Omit to use the GitHub default (``"ededed"``).
          - GitLab: ``#RRGGBB`` (e.g. ``"#ff0000"``).
          - Azure DevOps: label creation is not supported — returns
            ``{"error": "..."}`` containing "not supported".

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
            label = provider.create_label(
                project, token, name, color=color, description=description,
            )
            return {
                "project_id": project.id,
                "label": asdict(label),
            }
        return _safe(go)

    @mcp.tool()
    def update_label(
        project_id: str,
        name: str,
        new_name: str | None = None,
        color: str | None = None,
        description: str | None = None,
    ) -> dict:
        """Rename or recolour an existing label.

        Requires the project's `issues.modify` permission.

        `name` identifies the label to update (required).

        At least one of `new_name`, `color`, or `description` must be
        supplied; passing none returns ``{"error": "..."}`` without making
        any HTTP call.

        `color` format is provider-specific (see `create_label` docs).

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
            label = provider.update_label(
                project, token, name,
                new_name=new_name, color=color, description=description,
            )
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
