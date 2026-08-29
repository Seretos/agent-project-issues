#!/usr/bin/env bash
# Builds the `repository_dispatch` JSON payload sent to
# Seretos/agent-marketplace. Extracted (ticket #298) from the inline `jq -n`
# block that used to live in both release.yml's and dispatch.yml's
# "Dispatch to agent-marketplace" step, so the logic is unit-testable and
# byte-identical between the two callers.
#
# Network-free: reads its inputs from the environment, writes the payload
# JSON to stdout.
#
# Required env: NAME DESC REPO VERSION TAG
# Optional env: CHANGELOG_RAW RELEASE_URL (either may be entirely unset)
#
# A missing required var fails loudly (non-zero exit, before jq ever runs)
# rather than silently emitting a malformed/partial payload.
set -euo pipefail

: "${NAME:?NAME is required}"
: "${DESC:?DESC is required}"
: "${REPO:?REPO is required}"
: "${VERSION:?VERSION is required}"
: "${TAG:?TAG is required}"

CHANGELOG_RAW="${CHANGELOG_RAW:-}"
RELEASE_URL="${RELEASE_URL:-}"

# Same reserved-room truncation filter as release.yml's pre-#298 inline
# block: reserves room for the "...truncated" suffix instead of slicing to
# a bare [:8000], and measures length in codepoints (jq's `length` on a
# string), not UTF-8 bytes.
#
# The `&& printf x` sentinel (mirrors the workflow-side BODY derivation,
# see release.yml's "Dispatch to agent-marketplace" step) is required
# because plain `$(...)` command substitution unconditionally strips ALL
# trailing newlines -- without it, a changelog body that legitimately ends
# in a newline would silently lose it here, indistinguishable from one that
# never had it. `x` is stripped back off, then exactly jq's own one
# auto-appended trailing newline is stripped, leaving the filter's real
# output (including any of its own trailing newline) intact. The `\r`
# strip is defensive: a `jq` build that writes stdout in CRT text mode
# (observed with the Windows jq.exe binary) turns every `\n` it writes into
# `\r\n`; stripping stray `\r` characters here restores the logical LF-only
# value on any such platform without touching legitimate content, and is a
# no-op everywhere else (including the Linux jq GitHub Actions runners
# ship). The body is handed to jq via `--rawfile body <tmpfile>` rather
# than `--arg body "$CHANGELOG_RAW"`: a large raw release body passed as a
# command-line argument can exceed the platform's argv-length limit
# (observed with a ~40000-byte multibyte body via a native Windows jq.exe
# binary); a file avoids that ceiling entirely and keeps the filter program
# itself byte-identical to release.yml's pre-#298 inline version.
BODY_FILE=$(mktemp)
printf '%s' "$CHANGELOG_RAW" > "$BODY_FILE"
CHANGELOG=$(jq -rn --rawfile body "$BODY_FILE" --arg url "$RELEASE_URL" --argjson max 8000 '("\n\n…truncated — full notes: " + $url) as $suffix | if ($body|length) <= $max then $body else ($body[0:($max - ($suffix|length))] + $suffix) end' && printf x) || CHANGELOG=""
rm -f "$BODY_FILE"
CHANGELOG="${CHANGELOG%x}"
CHANGELOG="${CHANGELOG//$'\r'/}"
CHANGELOG="${CHANGELOG%$'\n'}"
if [ -z "$CHANGELOG" ]; then
  echo "::warning::changelog is empty; omitting changelog from marketplace dispatch payload" >&2
fi

# Base payload: reproduces the pre-#298 9-field jq -n block exactly, with
# $REPO substituted for $GITHUB_REPOSITORY so this script has no dependency
# on running inside a specific GitHub Actions job.
PAYLOAD=$(jq -n \
  --arg name "$NAME" \
  --arg description "$DESC" \
  --arg repo "$REPO" \
  --arg version "$VERSION" \
  --arg ref "$TAG" \
  --arg icon "https://raw.githubusercontent.com/$REPO/$TAG/assets/icon.png" \
  --arg description_url "https://raw.githubusercontent.com/$REPO/$TAG/description.md" \
  '{
    event_type: "plugin-release",
    client_payload: {
      name: $name,
      description: $description,
      repo: $repo,
      category: "mcp",
      tags: ["git", "github", "gitlab", "organisation", "ticket"],
      version: $version,
      ref: $ref,
      icon: $icon,
      description_url: $description_url
    }
  }')

if [ -n "$CHANGELOG" ]; then
  PAYLOAD=$(printf '%s' "$PAYLOAD" | jq --arg changelog "$CHANGELOG" '.client_payload.changelog = $changelog')
fi

printf '%s' "$PAYLOAD"
