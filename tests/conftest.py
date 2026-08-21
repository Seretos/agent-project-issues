"""Shared pytest fixtures for the project-issues plugin tests."""

# Enables the `pytester` fixture (ticket #244's timeout regression test runs
# a throwaway suite via pytester.runpytest_subprocess).
pytest_plugins = ["pytester"]
