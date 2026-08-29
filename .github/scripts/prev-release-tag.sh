#!/usr/bin/env bash
# Picks the highest *other* `<plugin>--v<strict-semver>` tag from a list of
# candidate tag lines on stdin. Pure filter: no git, no network, so it is
# unit-testable with no fixture. Used by release.yml (ticket #298) to find
# the previous release's `src/<TAG>` marker tag so `--generate-notes` can be
# pointed at real, shared history instead of the ancestor-less orphan tag.
#
# Usage: printf '%s\n' "${TAGS[@]}" | prev-release-tag.sh <plugin> [exclude-tag]
#
# - Candidate lines are matched against ^<plugin>--v<semver>$ exactly (a
#   `src/*` marker tag or a foreign-plugin tag can never match, even if the
#   caller's glob is sloppy).
# - <semver> follows semver.org's strict grammar (no build metadata, no
#   leading zeros).
# - The tag named as <exclude-tag> (the tag this run is about to create) is
#   never reported back as "the previous release", even if it would
#   otherwise win.
# - Prints the winning line, or nothing if there is no valid candidate.
# - Always exits 0: a malformed/garbage candidate list must never block the
#   release.
set -euo pipefail

PLUGIN="$1"
EXCLUDE="${2:-}"

# semver.org grammar, no build metadata:
#   1 = major, 2 = minor, 3 = patch
#   5 = full prerelease identifier chain (without the leading '-'), or empty
SEMVER_RE='(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-((0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(\.(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?'
PATTERN="^${PLUGIN}--v${SEMVER_RE}\$"

# semver.org precedence rule 11: returns 0 (true) if version A > version B.
# Every comparison happens inside an `if`/`[ ]`/`[[ ]]` test, never as a
# standalone arithmetic statement, so this stays safe to call under `set -e`
# regardless of calling context.
semver_gt() {
  local a_major="$1" a_minor="$2" a_patch="$3" a_pre="$4"
  local b_major="$5" b_minor="$6" b_patch="$7" b_pre="$8"

  if [ "$((10#$a_major))" -ne "$((10#$b_major))" ]; then
    if [ "$((10#$a_major))" -gt "$((10#$b_major))" ]; then return 0; else return 1; fi
  fi
  if [ "$((10#$a_minor))" -ne "$((10#$b_minor))" ]; then
    if [ "$((10#$a_minor))" -gt "$((10#$b_minor))" ]; then return 0; else return 1; fi
  fi
  if [ "$((10#$a_patch))" -ne "$((10#$b_patch))" ]; then
    if [ "$((10#$a_patch))" -gt "$((10#$b_patch))" ]; then return 0; else return 1; fi
  fi

  # Same major.minor.patch -- rule 11.3: no prerelease outranks any prerelease.
  if [ -z "$a_pre" ] && [ -z "$b_pre" ]; then
    return 1
  fi
  if [ -z "$a_pre" ]; then
    return 0
  fi
  if [ -z "$b_pre" ]; then
    return 1
  fi

  # Rule 11.4: dot-separated identifier walk.
  IFS='.' read -ra a_ids <<<"$a_pre"
  IFS='.' read -ra b_ids <<<"$b_pre"

  local i=0
  local a_len=${#a_ids[@]}
  local b_len=${#b_ids[@]}
  while [ "$i" -lt "$a_len" ] && [ "$i" -lt "$b_len" ]; do
    local ai="${a_ids[$i]}"
    local bi="${b_ids[$i]}"
    if [ "$ai" != "$bi" ]; then
      local a_is_num=0
      local b_is_num=0
      if [[ "$ai" =~ ^[0-9]+$ ]]; then a_is_num=1; fi
      if [[ "$bi" =~ ^[0-9]+$ ]]; then b_is_num=1; fi
      if [ "$a_is_num" -eq 1 ] && [ "$b_is_num" -eq 1 ]; then
        # 11.4.1: both numeric -- compare numerically.
        if [ "$((10#$ai))" -gt "$((10#$bi))" ]; then return 0; else return 1; fi
      elif [ "$a_is_num" -eq 1 ] && [ "$b_is_num" -eq 0 ]; then
        # 11.4.3: numeric identifiers always have lower precedence.
        return 1
      elif [ "$a_is_num" -eq 0 ] && [ "$b_is_num" -eq 1 ]; then
        return 0
      else
        # 11.4.4: both alphanumeric -- ASCII compare.
        if [[ "$ai" > "$bi" ]]; then return 0; else return 1; fi
      fi
    fi
    i=$((i + 1))
  done

  # 11.4.4: a shorter set of identifiers that is a prefix of the longer
  # one has lower precedence.
  if [ "$a_len" -gt "$b_len" ]; then return 0; else return 1; fi
}

best_line=""
have_best=0
best_major=""
best_minor=""
best_patch=""
best_prerelease=""

while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"
  [ -z "$line" ] && continue

  if [[ "$line" =~ $PATTERN ]]; then
    if [ -n "$EXCLUDE" ] && [ "$line" = "$EXCLUDE" ]; then
      continue
    fi

    major="${BASH_REMATCH[1]}"
    minor="${BASH_REMATCH[2]}"
    patch="${BASH_REMATCH[3]}"
    prerelease="${BASH_REMATCH[5]}"

    if [ "$have_best" -eq 0 ]; then
      best_line="$line"
      best_major="$major"
      best_minor="$minor"
      best_patch="$patch"
      best_prerelease="$prerelease"
      have_best=1
      continue
    fi

    if semver_gt "$major" "$minor" "$patch" "$prerelease" \
                 "$best_major" "$best_minor" "$best_patch" "$best_prerelease"; then
      best_line="$line"
      best_major="$major"
      best_minor="$minor"
      best_patch="$patch"
      best_prerelease="$prerelease"
    fi
  fi
done

if [ -n "$best_line" ]; then
  printf '%s\n' "$best_line"
fi

exit 0
