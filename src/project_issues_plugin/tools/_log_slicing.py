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

# Ticket #262: a *character*-based hard cap alongside the line cap
# above. 1000 lines of a real CI log (e.g. a verbose pytest run with
# long timestamped lines) can still be ~97,500 characters — nominally
# respecting `max_lines` while blowing well past any sane bound on the
# text actually handed back to an agent. This is always enforced
# (default budget when `max_chars` is None) and can only be *tightened*
# by a caller-supplied `max_chars`, never loosened past it.
_MAX_CHARS_HARD_CAP = 40_000

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


def _error_match_offset(line: str) -> int | None:
    """Leftmost character offset (case-insensitive) of any pattern in
    `_ERROR_PATTERNS` within `line`, or `None` if none match. The
    leftmost match wins even if a later-in-order pattern (e.g.
    `"failed"`) occurs earlier in `_ERROR_PATTERNS` than the pattern
    that actually matches first in the text (e.g. `"exception"`)."""
    lower = line.lower()
    best: int | None = None
    for pattern in _ERROR_PATTERNS:
        idx = lower.find(pattern)
        if idx != -1 and (best is None or idx < best):
            best = idx
    return best


def _is_error_line(line: str) -> bool:
    return _error_match_offset(line) is not None


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


def _hard_slice_line(line: str, budget: int, anchor: str) -> str:
    """Slice a single surviving line down to `budget` characters,
    preserving the mode's anchor:

      - `"tail"`: keep the tail — `line[len(line) - budget:]`.
      - `"errors_only"`: keep the head — `line[:budget]`.
      - `"around_failure"`: a fixed-size window of `budget` characters
        starting at the line's leftmost error-pattern match, clamped so
        it never runs past the line's end.
    """
    if anchor == "errors_only":
        return line[:budget]
    if anchor == "around_failure":
        offset = _error_match_offset(line)
        if offset is None:
            # Defensive — shouldn't happen, the surviving line in
            # around_failure mode is always the matched line.
            return line[len(line) - budget:]
        start = offset
        if start + budget > len(line):
            start = len(line) - budget
        if start < 0:
            start = 0
        return line[start:start + budget]
    # anchor == "tail" (also used by the around_failure->tail fallback)
    return line[len(line) - budget:]


def _char_trim(
    window: list[str], budget: int, *, anchor: str, match_idx: int | None = None,
) -> tuple[list[str], bool]:
    """Trim `window` (a list of already line-sliced log lines) down to
    at most `budget` characters when joined with `"\\n"`, preserving
    the mode's anchor. Returns `(new_window, char_truncated)`.

    Whole lines are dropped first, from whichever end is farthest from
    the anchor, until the joined text fits or a single line remains.
    If a single surviving line still exceeds `budget`, it is
    hard-sliced via `_hard_slice_line`.
    """
    if not window:
        return [], False
    if budget <= 0:
        return [], True

    joined = "\n".join(window)
    if len(joined) <= budget:
        return window, False

    n = len(window)
    if anchor == "errors_only":
        order = list(range(n - 1, -1, -1))
    elif anchor == "around_failure" and match_idx is not None:
        indices = [i for i in range(n) if i != match_idx]
        indices.sort(key=lambda i: (-abs(i - match_idx), i))
        order = indices
    else:
        # "tail" (and the around_failure->tail fallback, which reuses
        # the tail anchor entirely).
        order = list(range(n))

    remaining = set(range(n))
    for idx in order:
        if len(remaining) <= 1:
            break
        remaining.discard(idx)
        kept = [window[i] for i in sorted(remaining)]
        if len("\n".join(kept)) <= budget:
            return kept, True

    kept = [window[i] for i in sorted(remaining)]
    if len(kept) == 1 and len(kept[0]) > budget:
        return [_hard_slice_line(kept[0], budget, anchor)], True
    return kept, True


def slice_log(
    text: str, *, mode: str, max_lines: int, max_chars: int | None = None,
) -> dict:
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

    `max_chars` (ticket #262) is a second, character-based bound,
    applied after the line-level slicing above to the retained line
    list. The effective character budget is `_MAX_CHARS_HARD_CAP`
    (40,000) when `max_chars` is `None`, else
    `min(max_chars, _MAX_CHARS_HARD_CAP)` — a caller can only *tighten*
    the cap, never loosen it. When the retained lines exceed the
    budget, whole lines are dropped first from whichever end is
    farthest from the mode's anchor (`tail` drops from the front,
    `errors_only` drops from the back, `around_failure` drops
    whichever remaining line is farthest from the matched line, ties
    broken by dropping the leading line first); if a single surviving
    line still exceeds the budget it is hard-sliced in place,
    preserving the anchor (tail keeps the line's tail, errors_only
    keeps the line's head, around_failure keeps a `budget`-sized window
    starting at the matched pattern, clamped to the line's end).
    `budget <= 0` degenerates to an empty result.

    For `"tail"` and `"around_failure"`, `truncated`/`more_available`
    reflect whether the *whole raw log* has more lines than were
    returned, OR whether the character budget trimmed anything. For
    `"errors_only"`, they instead reflect whether there were more
    *matching* lines than `max_lines` could hold, OR a character trim
    — the non-matching lines dropped by the mode's own filtering are
    not "truncation", so total log length is not the relevant measure
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
    budget = _MAX_CHARS_HARD_CAP if max_chars is None else min(max_chars, _MAX_CHARS_HARD_CAP)

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
        line_truncated = len(matching) > len(capped)
        window, char_truncated = _char_trim(capped, budget, anchor="errors_only")
        truncated = line_truncated or char_truncated
        return {
            "lines": "\n".join(window),
            "truncated": truncated,
            "total_lines": total_lines,
            "returned_lines": len(window),
            "mode": mode,
            "more_available": truncated,
        }

    if mode == "around_failure":
        match_idx = next(
            (i for i, line in enumerate(all_lines) if _is_error_line(line)),
            None,
        )
        if match_idx is None:
            # No error-like line found — degrade to tail and say so.
            return _tail_result(
                all_lines, total_lines, max_lines, budget, mode="around_failure->tail",
            )
        start, end = _window_bounds(total_lines, match_idx, max_lines)
        window_lines = all_lines[start:end]
        line_truncated = total_lines > len(window_lines)
        match_idx_in_window = match_idx - start
        trimmed, char_truncated = _char_trim(
            window_lines, budget, anchor="around_failure", match_idx=match_idx_in_window,
        )
        truncated = line_truncated or char_truncated
        return {
            "lines": "\n".join(trimmed),
            "truncated": truncated,
            "total_lines": total_lines,
            "returned_lines": len(trimmed),
            "mode": mode,
            "more_available": truncated,
        }

    # mode == "tail" (default/fallback target)
    return _tail_result(all_lines, total_lines, max_lines, budget, mode="tail")


def _tail_result(
    all_lines: list[str], total_lines: int, max_lines: int, budget: int, *, mode: str,
) -> dict:
    tail = all_lines[-max_lines:] if max_lines > 0 else []
    line_truncated = total_lines > len(tail)
    trimmed, char_truncated = _char_trim(tail, budget, anchor="tail")
    truncated = line_truncated or char_truncated
    return {
        "lines": "\n".join(trimmed),
        "truncated": truncated,
        "total_lines": total_lines,
        "returned_lines": len(trimmed),
        "mode": mode,
        "more_available": truncated,
    }


__all__ = ["slice_log"]
