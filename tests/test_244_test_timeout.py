"""Regression test for ticket #244: the suite must have a per-test timeout.

728 tests across 57 files, all provider HTTP mocked, ran with no bound on
any single test -- a hung socket or a mock that never returns could block
`python -m pytest` indefinitely, which in agent-driven runs pushes the
Bash call into the background and wedges the calling agent.

Mirrors the sibling plugin `agent-worktree`, which already runs with
`timeout = 60` in `[tool.pytest.ini_options]` (see its pyproject.toml).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    pyproject = _repo_root() / "pyproject.toml"
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))


def test_hanging_test_is_killed_by_timeout(pytester: pytest.Pytester) -> None:
    """A test that hangs forever must be killed within the configured
    per-test timeout, not block the run indefinitely. Runs in a subprocess
    (runpytest_subprocess) -- the in-process kill path can take down the
    host interpreter, which would kill this test runner along with it.
    """
    pytester.makepyfile(
        test_hang="""
        import time

        def test_hangs_forever():
            time.sleep(600)

        def test_is_fast():
            assert True
        """
    )

    result = pytester.runpytest_subprocess("--timeout=2", "-p", "timeout")

    # No wall-clock assertion here: runpytest_subprocess's own overhead
    # (interpreter startup, plugin loading, collection, subprocess teardown)
    # is unbounded on a slow/contended runner and would make this regression
    # test itself flaky. The exit code plus the "Timeout" message in the
    # output already prove pytest-timeout killed the hang well short of the
    # 600s sleep, without needing a timing assertion.
    assert result.ret != 0
    result.stdout.re_match_lines([r".*Timeout.*"])


def test_fast_test_is_unaffected_by_timeout_flag(pytester: pytest.Pytester) -> None:
    """Companion to the hanging-test case: a normal, fast test must still
    exit 0 under the same --timeout flag -- proving the timeout produces
    no false failures for well-behaved tests."""
    pytester.makepyfile(
        test_fast="""
        def test_is_fast():
            assert True
        """
    )

    result = pytester.runpytest_subprocess("--timeout=2", "-p", "timeout")

    assert result.ret == 0


def test_ini_timeout_is_active_in_this_session(pytestconfig: pytest.Config) -> None:
    """The live session config -- not just the on-disk file -- must expose
    a `timeout` ini value of 60. Fails today with
    `ValueError: could not convert string to float: ''` because no such
    key is declared in [tool.pytest.ini_options]."""
    assert float(pytestconfig.getini("timeout")) == 60


def test_pyproject_declares_timeout_ini_and_dependency() -> None:
    data = _pyproject()

    ini_options = data["tool"]["pytest"]["ini_options"]
    assert ini_options["timeout"] == 60

    test_extra = data["project"]["optional-dependencies"]["test"]
    requirements = [Requirement(entry) for entry in test_extra]

    pytest_req = next((r for r in requirements if r.name == "pytest"), None)
    assert pytest_req is not None, "no 'pytest' entry found in the test extra"
    assert "8" in pytest_req.specifier

    timeout_req = next(
        (r for r in requirements if r.name == "pytest-timeout"), None
    )
    assert timeout_req is not None, (
        "no 'pytest-timeout' entry found in the test extra"
    )
    # Lower bound must be at least 2.3 -- exercise the specifier against a
    # version just below and just at/above the required floor rather than
    # string-matching, so a looser or reordered specifier still fails
    # honestly.
    assert "2.2.9" not in timeout_req.specifier
    assert "2.3" in timeout_req.specifier
