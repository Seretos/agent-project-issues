"""FastMCP application entry point — wires up the tool modules."""
from __future__ import annotations

import logging
import os
import sys

from mcp.server.fastmcp import FastMCP

log = logging.getLogger("project-issues")

mcp = FastMCP("project-issues")


def main() -> None:
    level_name = os.environ.get("PROJECT_ISSUES_PLUGIN_LOG", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    log.info(
        "project-issues MCP server starting (plugin_root=%s, cwd=%s, python=%s)",
        os.environ.get("PROJECT_ISSUES_PLUGIN_ROOT", "?"),
        os.getcwd(),
        sys.version.split()[0],
    )

    from .tools import (
        projects as project_tools,
        tickets as ticket_tools,
        comments as comment_tools,
        bulk as bulk_tools,
        pulls as pull_tools,
        pipelines as pipeline_tools,
    )
    project_tools.register(mcp)
    ticket_tools.register(mcp)
    comment_tools.register(mcp)
    bulk_tools.register(mcp)
    pull_tools.register(mcp)
    pipeline_tools.register(mcp)

    mcp.run()
