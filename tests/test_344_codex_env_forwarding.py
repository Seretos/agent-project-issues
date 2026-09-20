"""Ticket #344: the bundled Codex server must be able to read provider tokens.

Codex builds the STDIO server's environment from a declared allow-list
(`env_vars` in the server object of `.mcp.json`), so a variable that is not
named there never reaches the process and the server reports
`token_error: env_var_unset`.

The driving test spawns a real child interpreter whose environment is built
ONLY from the names `.mcp.json` declares in `env_vars`, plus a minimal
OS/interpreter bootstrap set (not part of the forwarding claim), and runs the
real `list_projects` tool through the real token-resolution path.

Known limit: this models Codex's launch environment as evidenced by installed
bundled plugin configs; it does not prove Codex implements `env_vars`.
Live-Codex behaviour is UNVERIFIED by owner decision.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SECRET = "ghp_test_value_344"

# OS minimum Python needs to start; deliberately excludes every token variable.
_BOOTSTRAP = (
    "SystemRoot", "SystemDrive", "PATH", "PATHEXT", "TEMP", "TMP",
    "COMSPEC", "WINDIR", "HOME",
)

_SNIPPET = r"""
import json
from lib_python_projects import ProjectConfig, ProjectsLoadResult
from project_issues_plugin.tools import projects as projects_mod

class _StubMCP:
    def __init__(self):
        self.tools = {}
    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco

project = ProjectConfig(
    id="acme", provider="github", path="acme/backend", token_env="GITHUB_TOKEN"
)
projects_mod.load_projects = lambda *a, **k: ProjectsLoadResult(
    projects=[project], state="ok", search_root="/tmp"
)
mcp = _StubMCP()
projects_mod.register(mcp)
print(json.dumps(mcp.tools["list_projects"]()))
"""


def _server() -> dict:
    mcp = json.loads((REPO / ".mcp.json").read_text(encoding="utf-8"))
    return mcp["mcpServers"]["project-issues"]


def _declared() -> list[str]:
    return list(_server().get("env_vars", []))


def _run_child(ambient_token: bool = True) -> subprocess.CompletedProcess:
    env: dict[str, str] = {}
    for name in _BOOTSTRAP:
        if name in os.environ:
            env[name] = os.environ[name]
    # What Codex would forward: declared names present in the parent shell.
    parent = dict(os.environ)
    if ambient_token:
        parent["GITHUB_TOKEN"] = SECRET
    for name in _declared():
        if name in parent:
            env[name] = parent[name]
    env["PYTHONPATH"] = str(REPO / "src")
    return subprocess.run(
        [sys.executable, "-c", _SNIPPET],
        env=env, capture_output=True, text=True, timeout=120, cwd=str(REPO),
    )


def _project(proc: subprocess.CompletedProcess) -> dict:
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["projects"][0]


def test_declared_env_vars_carry_the_github_token():
    proj = _project(_run_child())
    assert proj["token_error"] is None
    assert proj["token_available"] is True


def test_token_value_is_not_emitted_in_output():
    proc = _run_child()
    assert proc.returncode == 0, proc.stderr
    assert SECRET not in proc.stdout
    assert SECRET not in proc.stderr


def test_without_the_token_the_child_reports_env_var_unset():
    """Nothing in the parent shell -> nothing forwarded (passes pre-fix too)."""
    parent_had = os.environ.pop("GITHUB_TOKEN", None)
    try:
        proj = _project(_run_child(ambient_token=False))
    finally:
        if parent_had is not None:
            os.environ["GITHUB_TOKEN"] = parent_had
    assert proj["token_error"] == "env_var_unset"


def test_all_default_provider_tokens_are_declared():
    assert {"GITHUB_TOKEN", "GITLAB_TOKEN", "AZURE_DEVOPS_TOKEN"} <= set(_declared())


def test_declaration_carries_names_only():
    server = _server()
    assert "env" not in server
    for name in server.get("env_vars", []):
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
