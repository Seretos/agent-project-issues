"""Command-line surface of the bundled binary (`project-issues <subcommand>`).

Currently one subcommand, `wait-pipeline`, which blocks until the CI runs of
a commit reach a verdict (or a timeout elapses) and reports the outcome as an
exit code plus exactly one JSON object on stdout. Every diagnostic goes to
stderr. Starting the binary with no arguments is handled in `__main__` and
still starts the MCP stdio server.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import httpx

from lib_python_projects import ConfigError
from lib_python_projects.providers.azuredevops import AzureDevOpsError
from lib_python_projects.providers.base import ProviderError
from lib_python_projects.providers.github import GitHubError
from lib_python_projects.providers.gitlab import GitLabError

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_PENDING = 2
EXIT_NO_RUNS = 3
EXIT_ERROR = 4
EXIT_NO_VERDICT = 5

_EPILOG = """\
exit codes:
  0  success     every run of the commit completed green
  1  failure     at least one run failed
  2  pending     runs were still in progress when --timeout elapsed
  3  no runs     no CI run exists for the commit (or none appeared in time)
  4  error       config / provider / usage error (message on stderr, nothing on stdout)
  5  no verdict  runs finished but none is green or red: cancelled, timed_out or
                 skipped (the "state" field names which; see "runs" for detail)

stdout is one JSON object: {"state", "waited_s", "runs": [{"id", "event",
"status", "conclusion", "url"}]}. Diagnostics go to stderr.
"""

# Raw run conclusions (lower-cased) -> verdict. Anything not listed here and
# non-empty is treated as "no verdict": never report green for an unknown value.
_SUCCESS = {"success", "succeeded"}
_FAILURE = {
    "failure", "failed", "startup_failure", "action_required", "stale",
    "partiallysucceeded",
}
_NO_VERDICT_STATE = {
    "cancelled": "cancelled", "canceled": "cancelled",
    "timed_out": "timed_out", "timedout": "timed_out",
    "skipped": "skipped", "neutral": "no_verdict", "manual": "no_verdict",
}
_NO_VERDICT_ORDER = ("cancelled", "timed_out", "skipped", "no_verdict")


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse's default exit 2 would read as "timeout"
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        sys.exit(EXIT_ERROR)


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="project-issues wait-pipeline",
        description="Block until the CI runs of a commit finish or --timeout elapses.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--project", required=True, help="project id from projects.yml")
    parser.add_argument("--sha", required=True, help="commit sha to wait for")
    parser.add_argument(
        "--timeout", type=float, default=600.0,
        help="seconds to wait at most (default 600)",
    )
    parser.add_argument(
        "--interval", type=float, default=20.0,
        help="seconds between polls (default 20; the library floors it at 5)",
    )
    return parser


def _classify(runs) -> tuple[int, str]:
    if not runs:
        return EXIT_NO_RUNS, "no_runs"
    failed = pending = False
    no_verdict: set[str] = set()
    for run in runs:
        conclusion = run.conclusion
        if conclusion is None or conclusion == "":
            pending = True
            continue
        c = str(conclusion).lower()
        if c in _FAILURE:
            failed = True
        elif c in _SUCCESS:
            continue
        else:
            no_verdict.add(_NO_VERDICT_STATE.get(c, "no_verdict"))
    if failed:
        return EXIT_FAILURE, "failure"
    if pending:
        return EXIT_PENDING, "pending"
    if no_verdict:
        state = next(s for s in _NO_VERDICT_ORDER if s in no_verdict)
        return EXIT_NO_VERDICT, state
    return EXIT_SUCCESS, "success"


def _wait_pipeline(args) -> int:
    from project_issues_plugin.tools import _providers

    project = _providers._resolve(args.project)
    provider = _providers._provider_for(project)
    token = _providers._require_token(project)
    timeout = max(0.0, args.timeout)
    interval = max(1.0, min(args.interval, timeout))
    result = provider.wait_for_pipeline(
        project, token, args.sha, timeout_s=timeout, poll_interval_s=interval
    )
    code, state = _classify(result.runs)
    payload = {
        "state": state,
        "waited_s": round(result.waited_s, 1),
        "runs": [
            {
                "id": r.id,
                "event": r.event,
                "status": r.status,
                "conclusion": r.conclusion,
                "url": r.url,
            }
            for r in result.runs
        ],
    }
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()
    return code


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level=getattr(
            logging,
            os.environ.get("PROJECT_ISSUES_PLUGIN_LOG", "INFO").upper(),
            logging.INFO,
        ),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if argv and argv[0] == "wait-pipeline":
        args = _build_parser().parse_args(argv[1:])
    else:
        top = _Parser(prog="project-issues")
        top.error(
            f"unknown subcommand {argv[0]!r}; available: wait-pipeline"
            if argv
            else "a subcommand is required (wait-pipeline)"
        )
    try:
        return _wait_pipeline(args)
    except (
        LookupError, PermissionError, NotImplementedError, ValueError, TypeError,
        GitHubError, GitLabError, AzureDevOpsError, ProviderError,
        httpx.HTTPError, ConfigError,
    ) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_ERROR
