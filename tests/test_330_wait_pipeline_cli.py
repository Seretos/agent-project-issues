"""Tests for ticket #330: the `project-issues wait-pipeline` CLI subcommand.

The bundled binary gains a blocking `wait-pipeline --project <id> --sha <sha>
--timeout <s> [--interval <s>]` subcommand: exit 0 success / 1 failure /
2 pending / 3 no runs / 4 config-or-provider error / 5 no verdict
(cancelled / timed_out / skipped), one JSON object on stdout, diagnostics on
stderr. Starting with no arguments must still start the MCP stdio server.

The tests run the REAL process (`python -m project_issues_plugin ...`) against
a local threaded HTTP stub that speaks the three GitLab endpoints the lib
calls for commit runs, with a real `projects.yml` in a temp dir.

RED today: `__main__` ignores `sys.argv` and starts the MCP server, so no
subcommand output / exit code ever appears.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

PROJECT_ID = "stub-gl"
PROJECT_PATH = "acme/backend"
SHA = "a" * 40
SUBPROCESS_TIMEOUT = 90


# ---------------------------------------------------------------- HTTP stub


class _Stub:
    """Scripted GitLab: `pipelines` is a list of poll responses (each a list
    of pipeline dicts); the last one repeats. Records request paths."""

    def __init__(self, pipelines, *, ci_configured=True):
        self.pipelines = pipelines
        self.ci_configured = ci_configured
        self.requests: list[str] = []
        self.pipeline_polls = 0
        self._lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                parts = urlsplit(self.path)
                path = unquote(parts.path)
                base = f"/api/v4/projects/{PROJECT_PATH}"
                with stub._lock:
                    stub.requests.append(path)
                if path == f"{base}/repository/commits/{SHA}":
                    return self._send(200, {"id": SHA})
                if path == f"{base}/pipelines":
                    with stub._lock:
                        idx = min(stub.pipeline_polls, len(stub.pipelines) - 1)
                        stub.pipeline_polls += 1
                    return self._send(200, stub.pipelines[idx])
                if path == base:
                    return self._send(
                        200,
                        {
                            "builds_access_level": (
                                "enabled" if stub.ci_configured else "disabled"
                            ),
                            "default_branch": "main",
                        },
                    )
                if path.startswith(f"{base}/repository/files/"):
                    return self._send(200 if stub.ci_configured else 404, {})
                sys.stderr.write(f"STUB: unexpected path {self.path}\n")
                return self._send(404, {"message": "unexpected"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def _pipeline(pid, status, **extra):
    return {
        "id": pid,
        "status": status,
        "sha": SHA,
        "ref": "main",
        "source": "push",
        "web_url": f"http://example.invalid/pipelines/{pid}",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:01:00Z",
        **extra,
    }


# ----------------------------------------------------------------- helpers

_STRIP_PREFIXES = (
    "GITHUB_TOKEN",
    "GITLAB_TOKEN",
    "AZURE_DEVOPS",
    "PROJECT_ISSUES_",
    "CLAUDE_PROJECT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
)


def _env(tmp_path, port, *, token=True, extra=None):
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith(_STRIP_PREFIXES)
    }
    env.update(
        {
            "PROJECT_ISSUES_CONFIG": str(tmp_path / "projects.yml"),
            "PYTHONPATH": str(SRC),
            "NO_PROXY": "127.0.0.1",
        }
    )
    if token:
        env["PI_TEST_TOKEN"] = "x"
    else:
        env.pop("PI_TEST_TOKEN", None)
    env.update(extra or {})
    return env


def _write_config(tmp_path, port):
    (tmp_path / "projects.yml").write_text(
        "version: 1\n"
        "projects:\n"
        f"  - id: {PROJECT_ID}\n"
        "    provider: gitlab\n"
        f"    path: {PROJECT_PATH}\n"
        f"    base_url: http://127.0.0.1:{port}\n"
        "    token_env: PI_TEST_TOKEN\n",
        encoding="utf-8",
    )


def _run(tmp_path, port, args, *, token=True, extra_env=None, driver=None):
    _write_config(tmp_path, port)
    if driver is None:
        cmd = [sys.executable, "-m", "project_issues_plugin", *args]
    else:
        cmd = [sys.executable, "-c", driver, *args]
    started = time.monotonic()
    proc = subprocess.run(
        cmd,
        cwd=tmp_path,
        env=_env(tmp_path, port, token=token, extra=extra_env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    proc.elapsed = time.monotonic() - started
    return proc


def _wait_args(*, timeout=30, interval=5, project=PROJECT_ID, sha=SHA):
    return [
        "wait-pipeline",
        "--project", project,
        "--sha", sha,
        "--timeout", str(timeout),
        "--interval", str(interval),
    ]


# Driver for conclusions GitLab's own mapping cannot yield (timed_out, or an
# unknown string): patch the lib's run mapper in-process, then enter the real
# `__main__` via runpy so the argv dispatch is still what is under test.
_PATCHED_DRIVER = """
import dataclasses, runpy, sys
import lib_python_projects.providers.gitlab as g
_orig = g._map_pipeline_run
_RAW = {"timed_out", "weird_new_conclusion"}
def _patched(raw):
    run = _orig(raw)
    if raw.get("status") in _RAW:
        run = dataclasses.replace(run, status="completed", conclusion=raw["status"])
    return run
g._map_pipeline_run = _patched
sys.argv = ["project-issues"] + sys.argv[1:]
runpy.run_module("project_issues_plugin", run_name="__main__", alter_sys=True)
"""


# ------------------------------------------------- R1: exit codes / stdout


@pytest.mark.parametrize(
    "pipelines, ci_configured, extra_args, exit_code, state, use_driver",
    [
        ([[_pipeline(1, "success")]], True, {}, 0, "success", False),
        ([[_pipeline(1, "failed")]], True, {}, 1, "failure", False),
        ([[_pipeline(1, "running")]], True, {"timeout": 2}, 2, "pending", False),
        ([[]], False, {}, 3, "no_runs", False),
        ([[_pipeline(1, "canceled")]], True, {}, 5, "cancelled", False),
        ([[_pipeline(1, "skipped")]], True, {}, 5, "skipped", False),
        ([[_pipeline(1, "timed_out")]], True, {}, 5, "timed_out", True),
    ],
    ids=["success", "failure", "pending", "no_runs", "cancelled", "skipped", "timed_out"],
)
def test_exit_codes_for_each_outcome(
    tmp_path, pipelines, ci_configured, extra_args, exit_code, state, use_driver
):
    with _Stub(pipelines, ci_configured=ci_configured) as stub:
        proc = _run(
            tmp_path,
            stub.port,
            _wait_args(**extra_args),
            driver=_PATCHED_DRIVER if use_driver else None,
        )
    assert proc.returncode == exit_code, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    assert payload["state"] == state
    assert isinstance(payload["waited_s"], (int, float))
    assert isinstance(payload["runs"], list)
    # waited_s is real elapsed time, rounded to 0.1 s and bounded by the run.
    assert 0 <= payload["waited_s"] <= proc.elapsed + 0.5
    assert payload["waited_s"] == round(payload["waited_s"], 1)
    if state == "pending":
        assert payload["waited_s"] >= 1.5  # blocked until --timeout 2 elapsed


def test_unknown_project_exits_4_with_empty_stdout(tmp_path):
    with _Stub([[_pipeline(1, "success")]]) as stub:
        proc = _run(tmp_path, stub.port, _wait_args(project="nope"))
    assert proc.returncode == 4, (proc.stdout, proc.stderr)
    assert proc.stdout == ""
    assert "nope" in proc.stderr


def test_missing_token_exits_4_with_empty_stdout(tmp_path):
    with _Stub([[_pipeline(1, "success")]]) as stub:
        proc = _run(tmp_path, stub.port, _wait_args(), token=False)
    assert proc.returncode == 4, (proc.stdout, proc.stderr)
    assert proc.stdout == ""
    assert proc.stderr.strip() != ""


def test_unrecognised_conclusion_is_no_verdict_not_success(tmp_path):
    with _Stub([[_pipeline(1, "weird_new_conclusion")]]) as stub:
        proc = _run(tmp_path, stub.port, _wait_args(), driver=_PATCHED_DRIVER)
    assert proc.returncode == 5, (proc.stdout, proc.stderr)
    assert json.loads(proc.stdout)["state"] == "no_verdict"


def test_failed_plus_canceled_run_exits_1(tmp_path):
    with _Stub([[_pipeline(2, "failed"), _pipeline(1, "canceled")]]) as stub:
        proc = _run(tmp_path, stub.port, _wait_args())
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert json.loads(proc.stdout)["state"] == "failure"


def test_run_entries_carry_exactly_the_documented_keys(tmp_path):
    with _Stub([[_pipeline(7, "success")]]) as stub:
        proc = _run(tmp_path, stub.port, _wait_args())
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    runs = json.loads(proc.stdout)["runs"]
    assert len(runs) == 1
    assert set(runs[0]) == {"id", "event", "status", "conclusion", "url"}
    assert runs[0]["id"] == "7"
    assert runs[0]["conclusion"] == "success"
    assert runs[0]["url"] == "http://example.invalid/pipelines/7"


# ------------------------------------------------- R2: early return on red


def test_returns_immediately_on_first_failure(tmp_path):
    with _Stub([[_pipeline(1, "failed")]]) as stub:
        proc = _run(tmp_path, stub.port, _wait_args(timeout=60))
        polls = stub.pipeline_polls
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert json.loads(proc.stdout)["state"] == "failure"
    assert proc.elapsed < 20
    assert polls == 1


# ------------------------------------------------- R3: stdout is one object


def test_stdout_is_exactly_one_json_object(tmp_path):
    with _Stub([[_pipeline(1, "success")]]) as stub:
        proc = _run(
            tmp_path,
            stub.port,
            _wait_args(),
            extra_env={"PROJECT_ISSUES_PLUGIN_LOG": "DEBUG"},
        )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    out = proc.stdout.strip()
    assert out.count("\n") == 0
    assert isinstance(json.loads(out), dict)


# ------------------------------------------------- R4: no argv -> MCP server


def test_no_argv_still_speaks_mcp(tmp_path):
    init = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        }
    )
    proc = subprocess.run(
        [sys.executable, "-m", "project_issues_plugin"],
        cwd=tmp_path,
        env=_env(tmp_path, 0),
        input=init + "\n",
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert '"result"' in proc.stdout, (proc.stdout, proc.stderr)
    assert "protocolVersion" in proc.stdout


# ------------------------------------------------- R5: --help / usage errors


def test_help_lists_arguments_and_exit_codes(tmp_path):
    proc = _run(tmp_path, 0, ["wait-pipeline", "--help"])
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    for flag in ("--project", "--sha", "--timeout", "--interval"):
        assert flag in proc.stdout
    meanings = {0: "success", 1: "fail", 2: "pending", 3: "no run", 4: "error", 5: "verdict"}
    for code, word in meanings.items():
        assert re.search(
            rf"(?mi)^\s*{code}\s+.*{word}", proc.stdout
        ), f"exit code {code} not paired with {word!r}"


def test_unknown_flag_exits_4_not_argparse_default_2(tmp_path):
    proc = _run(tmp_path, 0, ["wait-pipeline", "--bogus-flag"])
    assert proc.returncode == 4, (proc.stdout, proc.stderr)
    assert proc.stdout == ""
    assert "usage" in proc.stderr.lower()


# ------------------------------------------------- R6: built binary (manual)


def _built_binary():
    for name in ("project-issues.exe", "project-issues"):
        p = ROOT / "bin" / name
        if p.exists():
            return p
    return None


@pytest.mark.skipif(
    _built_binary() is None,
    reason="bin/project-issues is gitignored; run scripts/build.ps1 first",
)
def test_built_binary_accepts_wait_pipeline():
    proc = subprocess.run(
        [str(_built_binary()), "wait-pipeline", "--help"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "--sha" in proc.stdout
