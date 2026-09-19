#!/usr/bin/env bash
# Stage the installable plugin payload: stage-plugin-payload.sh <src> <dest>
#
# Single source of truth for the package layout shipped by both the install
# ZIP and the orphan `release` branch (release.yml). Portable Agent Plugins
# layout: .claude-plugin/ (Claude Code) and .codex-plugin/plugin.json -> .mcp.json
# (Codex, #332) next to hooks/, skills/ and bin/. No root plugin.json/mcp.json:
# no host reads them.
set -euo pipefail

SRC="${1:?usage: stage-plugin-payload.sh <src> <dest>}"
DEST="${2:?usage: stage-plugin-payload.sh <src> <dest>}"

for req in .claude-plugin .codex-plugin .mcp.json bin hooks; do
  [ -e "$SRC/$req" ] || { echo "::error::stage-plugin-payload: required '$req' missing in $SRC" >&2; exit 1; }
done

mkdir -p "$DEST"
cp -a "$SRC/.claude-plugin" "$SRC/.codex-plugin" "$SRC/.mcp.json" "$SRC/hooks" "$DEST/"
# bin/ holds both `project-issues` and `project-issues.exe`; MSYS cp treats
# them as one file on Windows ("File exists"), so copy it through tar.
(cd "$SRC" && tar -cf - bin) | (cd "$DEST" && tar -xf -)
# #239: ship the bundled skill.
[ -d "$SRC/skills" ] && cp -a "$SRC/skills" "$DEST/"
[ -f "$SRC/README.md" ] && cp "$SRC/README.md" "$DEST/"
[ -f "$SRC/LICENSE" ] && cp "$SRC/LICENSE" "$DEST/"
[ -f "$SRC/description.md" ] && cp "$SRC/description.md" "$DEST/"
[ -d "$SRC/assets" ] && cp -a "$SRC/assets" "$DEST/"
exit 0
