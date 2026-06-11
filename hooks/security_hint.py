"""
UserPromptSubmit hook: periodically reminds the agent to surface the
.seretos/ managed-settings deny-rule guidelines when a .seretos/ directory
is present in the project tree.

Manual verification commands
-----------------------------
# From a directory WITH a .seretos ancestor (replace <dir> with the path):
#   echo {"session_id": "t1", "cwd": "<dir>"} | python hooks/security_hint.py
#   -> first call: prints hint JSON
#   -> immediate second call (same session_id): prints nothing (rate-limited)

# Malformed stdin:
#   echo not-json | python hooks/security_hint.py
#   -> exit 0, no output, no traceback

# No .seretos ancestor:
#   echo {"session_id": "t1", "cwd": "/tmp"} | python hooks/security_hint.py
#   -> exit 0, no output

# Validate JSON shape of the hint output:
#   echo {"session_id": "t1", "cwd": "<dir>"} | python hooks/security_hint.py | python -m json.tool
"""

import hashlib
import json
import os
import sys
import tempfile


_HINT_TEXT = (
    "Security notice: This project uses .seretos/projects.yml to configure"
    " agent permissions. Inform the user that a Claude Code managed-settings"
    " deny rule is recommended to protect this file from being read or modified"
    " by agents. See"
    " https://github.com/Seretos/agent-project-issues/blob/main/SECURITY.md"
    " for setup instructions."
)

_FIRE_ON_FIRST = 1   # fire on the 1st qualifying prompt of the session
_FIRE_INTERVAL = 10  # then every 10th prompt thereafter


def _has_seretos_ancestor(start_dir: str) -> bool:
    """Walk up from start_dir; return True if any ancestor has a .seretos/ subdir."""
    current = os.path.abspath(start_dir)
    while True:
        if os.path.isdir(os.path.join(current, ".seretos")):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            # Reached filesystem root.
            return False
        current = parent


def _state_file(session_id: str) -> str:
    return os.path.join(
        tempfile.gettempdir(),
        f"project-issues-hook-state-{session_id}.json",
    )


def _load_counter(session_id: str) -> int:
    path = _state_file(session_id)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return int(data.get("counter", 0))
    except Exception:
        return 0


def _save_counter(session_id: str, counter: int) -> None:
    path = _state_file(session_id)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"counter": counter}, fh)
    except Exception:
        pass


def _should_fire(counter: int) -> bool:
    """Return True if the hint should fire at this counter value (1-based prompt count)."""
    if counter == _FIRE_ON_FIRST:
        return True
    if counter > _FIRE_ON_FIRST and (counter - _FIRE_ON_FIRST) % _FIRE_INTERVAL == 0:
        return True
    return False


def main() -> None:
    try:
        raw = sys.stdin.read()
        try:
            event = json.loads(raw)
        except Exception:
            sys.exit(0)

        # Resolve CWD from event or fall back to process CWD.
        cwd = event.get("cwd") or os.getcwd()

        if not _has_seretos_ancestor(str(cwd)):
            sys.exit(0)

        # Determine session_id: use event field or hash of CWD.
        session_id = event.get("session_id")
        if not session_id:
            session_id = hashlib.sha256(str(cwd).encode()).hexdigest()[:16]
        session_id = str(session_id)

        counter = _load_counter(session_id) + 1
        _save_counter(session_id, counter)

        if not _should_fire(counter):
            sys.exit(0)

        output = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": _HINT_TEXT,
            }
        }
        sys.stdout.write(json.dumps(output))
        sys.stdout.flush()

    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
