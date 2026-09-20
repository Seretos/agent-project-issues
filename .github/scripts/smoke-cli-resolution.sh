#!/usr/bin/env bash
# Smoke check: the documented CLI name resolution really runs (#347, #349).
#
# Shared by release.yml (before publishing) and test.yml (every PR). Runs from
# the repo root against bin/ holding BOTH built binaries side by side, as in
# the shipped plugin: project-issues (Linux ELF) and project-issues.exe.
# No arguments, no flags.
#
# `set -e` note (#349): the bare `--help` probe exits non-zero (4: subcommand
# required), so every probe is captured as `rc=0; cmd || rc=$?` -- a plain
# `cmd; rc=$?` would abort the script before the 126/127 retry and the
# `--sha` assertion were ever reached.
set -euo pipefail

# Match real directory entries: on MSYS `[ -f bin/project-issues ]` also
# succeeds when only project-issues.exe exists.
for f in project-issues project-issues.exe; do
  ls -1 bin 2>/dev/null | grep -qxF "$f"     || { echo "::error::smoke-cli-resolution: expected bin/$f is missing" >&2; exit 1; }
done
chmod +x bin/project-issues bin/project-issues.exe || true

PATH="$(cd bin && pwd):$PATH"
export PATH

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) PI=project-issues.exe ;;
  *) PI=project-issues ;;
esac

rc=0
"$PI" --help >/dev/null 2>&1 || rc=$?
if [ "$rc" -eq 126 ] || [ "$rc" -eq 127 ]; then
  if [ "$PI" = project-issues.exe ]; then PI=project-issues; else PI=project-issues.exe; fi
fi
echo "resolved PI=$PI"

rc=0
out="$("$PI" wait-pipeline --help 2>&1)" || rc=$?
echo "$out"
if [ "$rc" -ne 0 ]; then
  echo "::error::$PI wait-pipeline --help exited $rc"
  exit 1
fi
case "$out" in
  *--sha*) ;;
  *) echo "::error::--sha missing from wait-pipeline --help"; exit 1 ;;
esac
