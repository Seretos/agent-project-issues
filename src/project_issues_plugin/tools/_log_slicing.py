"""Bounded log-slicing helpers for `get_pipeline_step_log` (ticket #199).

Sibling to `tools/_slicing.py` (which slices *rows*, e.g. comments) —
this module slices raw CI job-log *text* down to a small, bounded
window instead of ever handing an agent the full unbounded log that
`Provider.get_step_log` returns.

`slice_log` is pure and provider-agnostic: it knows nothing about
GitHub/GitLab/Azure DevOps, it only operates on the raw log string
`get_pipeline_step_log` already fetched.
"""
from __future__ import annotations

# Hard cap enforced regardless of what a caller asks for — defense in
# depth alongside whatever validation the tool layer applies to
# `max_lines` before calling in.
_MAX_LINES_HARD_CAP = 1000

# Small fixed error-signal pattern set, matched as a case-insensitive
# substring against each line. Deliberately broad/simple rather than a
# structured per-provider parser (mirrors how `log_excerpt`'s fallback
# substring scan already works in the lib) — good enough to find "the
# interesting part" of a huge raw log without provider-specific logic.
_ERROR_PATTERNS = (
    "##[error]",
    "error",
    "failed",
    "failure",
    "traceback",
    "exception",
    "fatal",
    "panic",
)


def _is_error_line(line: str) -> bool:
    lower = line.lower()
    return any(pattern in lower for pattern in _ERROR_PATTERNS)


def _window_bounds(total: int, idx: int, max_lines: int) -> tuple[int, int]:
    """Compute a `[start, end)` window of size <= `max_lines` centered
    on `idx`, clamped so it never runs off either end of `[0, total)`.
    """
    half = max_lines // 2
    start = idx - half
    end = start + max_lines
    if start < 0:
        start = 0
        end = min(total, max_lines)
    if end > total:
        end = total
        start = max(0, end - max_lines)
    return start, end


def slice_log(text: str, *, mode: str, max_lines: int) -> dict:
    """Slice raw log `text` down to a bounded window.

    `mode`:
      - `"tail"`: the last `max_lines` lines.
      - `"around_failure"`: a window of `max_lines` centered on the
        first line matching `_ERROR_PATTERNS` (case-insensitive
        substring). Falls back to `"tail"` behavior when no line
        matches, in which case the returned `mode` is
        `"around_failure->tail"` so callers can tell it degraded.
      - `"errors_only"`: only the lines matching `_ERROR_PATTERNS`, in
        original order, capped at `max_lines` matching lines.

    `max_lines` is clamped to a hard cap of `_MAX_LINES_HARD_CAP`
    (1000) here too, regardless of what the caller passed in.

    For `"tail"` and `"around_failure"`, `truncated`/`more_available`
    reflect whether the *whole raw log* has more lines than were
    returned. For `"errors_only"`, they instead reflect whether there
    were more *matching* lines than `max_lines` could hold — the
    non-matching lines dropped by the mode's own filtering are not
    "truncation", so total log length is not the relevant measure
    there.

    Returns:
        {
          "lines": str,
          "truncated": bool,
          "total_lines": int,      # lines in the whole raw log
          "returned_lines": int,   # lines actually in `lines`
          "mode": str,             # echoes mode, or "around_failure->tail"
          "more_available": bool,
        }
    """
    max_lines = min(max_lines, _MAX_LINES_HARD_CAP)

    if text == "":
        return {
            "lines": "",
            "truncated": False,
            "total_lines": 0,
            "returned_lines": 0,
            "mode": mode,
            "more_available": False,
        }

    all_lines = text.splitlines()
    total_lines = len(all_lines)

    if mode == "errors_only":
        matching = [line for line in all_lines if _is_error_line(line)]
        capped = matching[:max_lines]
        more = len(matching) > len(capped)
        return {
            "lines": "\n".join(capped),
            "truncated": more,
            "total_lines": total_lines,
            "returned_lines": len(capped),
            "mode": mode,
            "more_available": more,
        }

    if mode == "around_failure":
        match_idx = next(
            (i for i, line in enumerate(all_lines) if _is_error_line(line)),
            None,
        )
        if match_idx is None:
            # No error-like line found — degrade to tail and say so.
            return _tail_result(all_lines, total_lines, max_lines, mode="around_failure->tail")
        start, end = _window_bounds(total_lines, match_idx, max_lines)
        window = all_lines[start:end]
        truncated = total_lines > len(window)
        return {
            "lines": "\n".join(window),
            "truncated": truncated,
            "total_lines": total_lines,
            "returned_lines": len(window),
            "mode": mode,
            "more_available": truncated,
        }

    # mode == "tail" (default/fallback target)
    return _tail_result(all_lines, total_lines, max_lines, mode="tail")


def _tail_result(all_lines: list[str], total_lines: int, max_lines: int, *, mode: str) -> dict:
    tail = all_lines[-max_lines:] if max_lines > 0 else []
    truncated = total_lines > len(tail)
    return {
        "lines": "\n".join(tail),
        "truncated": truncated,
        "total_lines": total_lines,
        "returned_lines": len(tail),
        "mode": mode,
        "more_available": truncated,
    }


__all__ = ["slice_log"]
