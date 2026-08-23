"""Unit tests for `project_issues_plugin.tools._log_slicing.slice_log`.

Direct, pure-function tests — no MCP tool wiring, no provider mocking.
"""
from __future__ import annotations

from project_issues_plugin.tools._log_slicing import slice_log


def _lines(n: int, *, prefix: str = "line") -> list[str]:
    return [f"{prefix}{i}" for i in range(n)]


def _fixed_width_lines(n: int, width: int) -> list[str]:
    """`n` lines, each exactly `width` characters, index-prefixed and
    padded with `x` — deterministic lengths for character-budget math.
    """
    return [f"L{i:04d}".ljust(width, "x") for i in range(n)]


# ---------- empty input -------------------------------------------------------


def test_slice_log_empty_string_all_modes() -> None:
    for mode in ("tail", "around_failure", "errors_only"):
        result = slice_log("", mode=mode, max_lines=50)
        assert result == {
            "lines": "",
            "truncated": False,
            "total_lines": 0,
            "returned_lines": 0,
            "mode": mode,
            "more_available": False,
        }


# ---------- tail ---------------------------------------------------------------


def test_slice_log_tail_returns_last_n_lines_when_truncated() -> None:
    text = "\n".join(_lines(10))
    result = slice_log(text, mode="tail", max_lines=3)
    assert result["lines"] == "line7\nline8\nline9"
    assert result["total_lines"] == 10
    assert result["returned_lines"] == 3
    assert result["truncated"] is True
    assert result["more_available"] is True
    assert result["mode"] == "tail"


def test_slice_log_tail_not_truncated_when_log_fits() -> None:
    text = "\n".join(_lines(3))
    result = slice_log(text, mode="tail", max_lines=5)
    assert result["lines"] == "line0\nline1\nline2"
    assert result["total_lines"] == 3
    assert result["returned_lines"] == 3
    assert result["truncated"] is False
    assert result["more_available"] is False


# ---------- around_failure -----------------------------------------------------


def test_slice_log_around_failure_centers_window_on_first_match() -> None:
    # 20 lines; put an error-like line at index 10.
    lines = _lines(20)
    lines[10] = "ERROR: boom at index 10"
    text = "\n".join(lines)
    result = slice_log(text, mode="around_failure", max_lines=6)
    # half = 3, so start=7, end=13 -> lines[7:13]
    assert result["lines"] == "\n".join(lines[7:13])
    assert "ERROR: boom at index 10" in result["lines"]
    assert result["mode"] == "around_failure"
    assert result["total_lines"] == 20
    assert result["returned_lines"] == 6
    assert result["truncated"] is True
    assert result["more_available"] is True


def test_slice_log_around_failure_clamps_at_start() -> None:
    lines = _lines(20)
    lines[1] = "Traceback (most recent call last):"
    text = "\n".join(lines)
    result = slice_log(text, mode="around_failure", max_lines=6)
    # Window would run off the left edge -> clamp to [0, 6).
    assert result["lines"] == "\n".join(lines[0:6])
    assert result["returned_lines"] == 6


def test_slice_log_around_failure_clamps_at_end() -> None:
    lines = _lines(20)
    lines[18] = "fatal: something broke"
    text = "\n".join(lines)
    result = slice_log(text, mode="around_failure", max_lines=6)
    # Window would run off the right edge -> clamp to [14, 20).
    assert result["lines"] == "\n".join(lines[14:20])
    assert result["returned_lines"] == 6
    assert "fatal: something broke" in result["lines"]


def test_slice_log_around_failure_no_match_falls_back_to_tail() -> None:
    text = "\n".join(_lines(10))
    result = slice_log(text, mode="around_failure", max_lines=3)
    assert result["mode"] == "around_failure->tail"
    assert result["lines"] == "line7\nline8\nline9"
    assert result["truncated"] is True
    assert result["more_available"] is True


def test_slice_log_around_failure_case_insensitive_match() -> None:
    lines = _lines(10)
    lines[5] = "Something FAILED here"
    text = "\n".join(lines)
    result = slice_log(text, mode="around_failure", max_lines=4)
    assert result["mode"] == "around_failure"
    assert "Something FAILED here" in result["lines"]


# ---------- errors_only ---------------------------------------------------------


def test_slice_log_errors_only_filters_and_preserves_order() -> None:
    lines = [
        "starting up",
        "step 1 ok",
        "ERROR: something broke",
        "step 2 ok",
        "Traceback (most recent call last):",
        "cleanup",
    ]
    text = "\n".join(lines)
    result = slice_log(text, mode="errors_only", max_lines=10)
    assert result["lines"] == "ERROR: something broke\nTraceback (most recent call last):"
    assert result["total_lines"] == 6
    assert result["returned_lines"] == 2
    assert result["truncated"] is False
    assert result["more_available"] is False
    assert result["mode"] == "errors_only"


def test_slice_log_errors_only_caps_at_max_lines_based_on_matches() -> None:
    # 5 matching lines, only 2 non-matching -> total_lines reflects the
    # whole log, but truncated/more_available are based on the 5
    # matching lines exceeding max_lines=2, not on total_lines.
    lines = [
        "ok line",
        "error one",
        "error two",
        "error three",
        "ok line 2",
        "error four",
        "error five",
    ]
    text = "\n".join(lines)
    result = slice_log(text, mode="errors_only", max_lines=2)
    assert result["lines"] == "error one\nerror two"
    assert result["total_lines"] == 7
    assert result["returned_lines"] == 2
    assert result["truncated"] is True
    assert result["more_available"] is True


def test_slice_log_errors_only_no_matches_returns_empty() -> None:
    text = "\n".join(_lines(5))
    result = slice_log(text, mode="errors_only", max_lines=10)
    assert result["lines"] == ""
    assert result["returned_lines"] == 0
    assert result["truncated"] is False
    assert result["more_available"] is False


# ---------- hard cap -------------------------------------------------------------


def test_slice_log_max_lines_above_hard_cap_is_clamped_to_1000() -> None:
    text = "\n".join(_lines(2000))
    result = slice_log(text, mode="tail", max_lines=5000)
    assert result["returned_lines"] == 1000
    assert result["total_lines"] == 2000
    assert result["truncated"] is True
    assert result["more_available"] is True


def test_slice_log_max_lines_above_hard_cap_clamped_for_errors_only() -> None:
    lines = [f"error {i}" for i in range(1500)]
    text = "\n".join(lines)
    result = slice_log(text, mode="errors_only", max_lines=10_000)
    assert result["returned_lines"] == 1000
    assert result["truncated"] is True
    assert result["more_available"] is True


# ---------- ticket #262: character-based hard cap --------------------------------


def test_slice_log_default_char_cap_bounds_output() -> None:
    """R1: a log that fits well within `max_lines` can still blow past
    a sane character budget when lines are long (the ticket's actual
    scenario — long timestamped pytest lines). The *default* 40k
    character hard cap must bound output even when max_lines alone
    would not have truncated anything."""
    lines = _fixed_width_lines(300, width=200)
    text = "\n".join(lines)
    assert len(text) > 40_000  # sanity check on the fixture itself

    result = slice_log(text, mode="tail", max_lines=1000)
    # 300 lines < max_lines=1000, so no line-level truncation — but the
    # char cap must still have bounded `lines`.
    assert len(result["lines"]) <= 40_000
    assert result["truncated"] is True
    assert result["more_available"] is True


def test_slice_log_max_chars_tightens_and_never_loosens() -> None:
    """R2: `max_chars` can only ever tighten the effective budget below
    the 40k hard cap, never loosen past it — mirrors how `max_lines`
    already behaves against `_MAX_LINES_HARD_CAP`."""
    lines = _fixed_width_lines(50, width=200)
    text = "\n".join(lines)

    tight = slice_log(text, mode="tail", max_lines=50, max_chars=100)
    assert len(tight["lines"]) <= 100

    # "never loosens": the fixture above (~10,049 chars) is already
    # under the 40k hard cap, so it can't actually exercise a missing
    # `min()` clamp against `_MAX_CHARS_HARD_CAP` — a broken
    # implementation that never clamps `max_chars` at all would still
    # pass trivially. Use a fixture well over 40k instead, with
    # `max_lines` raised so line-level slicing alone doesn't already
    # bound the output below 40k: a missing clamp would then let
    # ~60k chars through.
    big_lines = _fixed_width_lines(300, width=200)
    big_text = "\n".join(big_lines)
    assert len(big_text) > 40_000  # sanity check on the fixture itself
    loosen_attempt = slice_log(big_text, mode="tail", max_lines=1000, max_chars=10_000_000)
    assert len(loosen_attempt["lines"]) <= 40_000

    default_none = slice_log(text, mode="tail", max_lines=50, max_chars=None)
    assert len(default_none["lines"]) <= 40_000


def test_slice_log_char_budget_boundaries() -> None:
    """R3: exact-equality and edge-of-range behavior for the char
    budget — no trimming exactly at the boundary, a 1-char budget still
    returns exactly one character, a non-positive budget defensively
    returns nothing, and empty input is entirely unaffected by
    `max_chars` for every mode (proof that no new response key was
    introduced — same shape as `test_slice_log_empty_string_all_modes`
    above)."""
    lines = _fixed_width_lines(5, width=10)
    text = "\n".join(lines)

    # max_chars == len(joined) -> exact fit, no trimming at all.
    exact = slice_log(text, mode="tail", max_lines=5, max_chars=len(text))
    assert exact["lines"] == text
    assert exact["returned_lines"] == 5
    assert exact["truncated"] is False

    # max_chars=1 on a multi-line log -> single-char survivor.
    one_char = slice_log(text, mode="tail", max_lines=5, max_chars=1)
    assert one_char["lines"] == text[-1:]
    assert len(one_char["lines"]) == 1
    assert one_char["returned_lines"] == 1

    # Defensive: budget <= 0 -> empty lines, 0 returned_lines.
    zero_budget = slice_log(text, mode="tail", max_lines=5, max_chars=0)
    assert zero_budget["lines"] == ""
    assert zero_budget["returned_lines"] == 0

    for mode in ("tail", "around_failure", "errors_only"):
        result = slice_log("", mode=mode, max_lines=50, max_chars=10)
        assert result == {
            "lines": "",
            "truncated": False,
            "total_lines": 0,
            "returned_lines": 0,
            "mode": mode,
            "more_available": False,
        }


def test_slice_log_char_trim_preserves_mode_anchor() -> None:
    """R4 (driving test): character trimming must preserve each mode's
    anchor rather than blindly chopping from one end.

    - `tail` keeps the TAIL chars of the sole surviving line.
    - `errors_only` keeps the HEAD chars of the sole surviving line.
    - `around_failure` drops whole lines from whichever end is farthest
      from the matched line first (tie -> drop the leading line first),
      until the joined text fits the budget.
    - `around_failure->tail` (no match found) degrades to the same
      tail hard-slice rule as plain `tail`.
    """
    # tail: single-line survivor keeps the tail chars of that line.
    tail_last_line = "y" * 40 + "0123456789"
    tail_lines = ["short0", "short1", tail_last_line]
    tail_result = slice_log("\n".join(tail_lines), mode="tail", max_lines=3, max_chars=10)
    assert tail_result["mode"] == "tail"
    assert tail_result["returned_lines"] == 1
    assert tail_result["lines"] == tail_last_line[-10:]

    # errors_only: single-line survivor keeps the head chars of that line.
    errors_first_match = "error" + "9876543210"
    errors_lines = [errors_first_match, "error other match, irrelevant here"]
    errors_result = slice_log(
        "\n".join(errors_lines), mode="errors_only", max_lines=1, max_chars=10,
    )
    assert errors_result["mode"] == "errors_only"
    assert errors_result["returned_lines"] == 1
    assert errors_result["lines"] == errors_first_match[:10]

    # around_failure (matched): drop the farthest line from the match
    # first; idx0 and idx4 are tied (both distance 2 from idx2) -> the
    # leading line (idx0) is dropped before idx4.
    af_lines = [
        "L0000xxxxx",  # idx0, distance 2 from match -> dropped first (tie-break)
        "L0001xxxxx",  # idx1, distance 1
        "errorxxxxx",  # idx2 <- match
        "L0003xxxxx",  # idx3, distance 1
        "L0004xxxxx",  # idx4, distance 2 -> dropped second
    ]
    af_text = "\n".join(af_lines)
    af_result = slice_log(af_text, mode="around_failure", max_lines=5, max_chars=32)
    assert af_result["mode"] == "around_failure"
    assert af_result["returned_lines"] == 3
    assert af_result["lines"] == "\n".join(af_lines[1:4])
    assert af_result["truncated"] is True
    assert af_result["more_available"] is True

    # around_failure->tail (no match at all): degrades to the tail
    # hard-slice rule.
    fb_last_line = "w" * 40 + "9988776655"
    fb_lines = ["alpha0", "alpha1", fb_last_line]
    fb_result = slice_log("\n".join(fb_lines), mode="around_failure", max_lines=3, max_chars=10)
    assert fb_result["mode"] == "around_failure->tail"
    assert fb_result["returned_lines"] == 1
    assert fb_result["lines"] == fb_last_line[-10:]


def test_slice_log_char_trim_around_failure_clamps_near_line_end() -> None:
    """R4 (additional coverage): when the matched offset is near the
    end of the sole surviving line, the fixed-size window clamps so it
    never runs past the line's end — mirrors `_window_bounds` clamping
    for line-level windows."""
    line = "x" * 40 + "error"  # match ("error") starts at offset 40, len=45
    text = "\n".join(["prefix line", line, "suffix line"])
    result = slice_log(text, mode="around_failure", max_lines=1, max_chars=10)
    assert result["returned_lines"] == 1
    assert result["lines"] == line[-10:]
    assert result["lines"] == "xxxxxerror"


def test_slice_log_char_trim_around_failure_leftmost_pattern_wins() -> None:
    """R4 (additional coverage): when a line matches more than one
    error pattern, the hard-slice window anchors on the leftmost match
    (here "exception" at offset 5), not just the first pattern checked
    in `_ERROR_PATTERNS` order (here "failed", which occurs later in
    the line at offset 24)."""
    line = ("z" * 5) + "exception" + ("z" * 10) + "failed" + ("z" * 10)
    text = "\n".join(["header line", line, "footer line"])
    result = slice_log(text, mode="around_failure", max_lines=1, max_chars=15)
    assert result["returned_lines"] == 1
    assert result["lines"] == line[5:20]
    assert result["lines"] == "exceptionzzzzzz"
