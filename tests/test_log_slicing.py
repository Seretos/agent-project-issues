"""Unit tests for `project_issues_plugin.tools._log_slicing.slice_log`.

Direct, pure-function tests — no MCP tool wiring, no provider mocking.
"""
from __future__ import annotations

from project_issues_plugin.tools._log_slicing import slice_log


def _lines(n: int, *, prefix: str = "line") -> list[str]:
    return [f"{prefix}{i}" for i in range(n)]


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
