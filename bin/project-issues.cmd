@echo off
REM Windows-batch entry-point wrapper for the project-issues MCP server.
REM
REM Counterpart to release/wrappers/project-issues (POSIX shell). The
REM MCP host resolves `command: bin/project-issues` to this file via
REM PATHEXT, then this dispatches to the actual PyInstaller binary
REM in windows-x86_64\.
"%~dp0windows-x86_64\project-issues.exe" %*
