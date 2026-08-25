"""Regression test for ticket #271: restrict `.github/workflows/test.yml`'s
`on:` trigger block to `pull_request` only (drop `push`), and update the
header comment to stop claiming a push trigger.

Plain text/regex assertions -- no PyYAML, it is not a declared test
dependency (see tests/test_239_bundled_skill.py).
"""

from __future__ import annotations

import re
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _workflow_path(name: str) -> Path:
    return _repo_root() / ".github" / "workflows" / name


def _test_workflow_text() -> str:
    return _workflow_path("test.yml").read_text(encoding="utf-8")


def _on_block(text: str) -> str:
    """Slice from the column-0 `on:` line to the next column-0 non-comment,
    non-blank line (exclusive)."""
    lines = text.splitlines()
    on_idx = next(i for i, line in enumerate(lines) if re.match(r"^on\s*:", line))
    end_idx = len(lines)
    for i in range(on_idx + 1, len(lines)):
        stripped = lines[i].strip()
        if lines[i].startswith((" ", "\t")):
            continue
        if stripped == "" or stripped.startswith("#"):
            continue
        end_idx = i
        break
    return "\n".join(lines[on_idx:end_idx])


def _header_comment_block(text: str) -> str:
    """The contiguous leading '#' comment block above the column-0 `on:`
    line."""
    lines = text.splitlines()
    on_idx = next(i for i, line in enumerate(lines) if re.match(r"^on\s*:", line))
    comment_lines = [
        line for line in lines[:on_idx] if line.strip().startswith("#")
    ]
    return "\n".join(comment_lines)


def test_test_workflow_triggers_on_pull_request_only() -> None:
    """Driving test (R4): the `on:` block must contain `pull_request:` and
    must NOT contain `push:`. RED because the current block has both."""
    block = _on_block(_test_workflow_text())
    assert re.search(r"^\s*pull_request\s*:", block, re.M), (
        f"expected a pull_request trigger in the on: block, got:\n{block!r}"
    )
    assert not re.search(r"^\s*push\s*:", block, re.M), (
        f"expected no push trigger in the on: block, got:\n{block!r}"
    )


def test_branches_glob_gone_from_whole_file() -> None:
    """Edge-case coverage: the `branches: ["**"]` glob that accompanied the
    push trigger must be gone from the whole file, not just renamed."""
    text = _test_workflow_text()
    assert 'branches: ["**"]' not in text


def test_exactly_one_column_zero_on_key_exists() -> None:
    """Edge-case coverage: exactly one column-0 `on:` key exists (no
    duplication/malformed YAML introduced by the edit)."""
    lines = _test_workflow_text().splitlines()
    on_lines = [line for line in lines if re.match(r"^on\s*:", line)]
    assert len(on_lines) == 1, f"expected exactly one column-0 'on:' key, found {on_lines!r}"


def test_job_id_and_name_unchanged() -> None:
    """Edge-case coverage: job id stays `pytest`, job name stays
    `pytest (${{ matrix.os }})` -- minimal-diff regression guard."""
    text = _test_workflow_text()
    assert re.search(r"^\s{2}pytest\s*:\s*$", text, re.M), "job id 'pytest' not found unchanged"
    assert 'name: pytest (${{ matrix.os }})' in text


def test_matrix_unchanged() -> None:
    """Edge-case coverage: matrix os list stays [windows-latest,
    ubuntu-22.04] -- minimal-diff regression guard."""
    text = _test_workflow_text()
    assert "os: [windows-latest, ubuntu-22.04]" in text


def test_other_workflows_untouched_by_trigger_change() -> None:
    """Edge-case coverage: release.yml and dispatch.yml each keep an `on:`
    block containing workflow_dispatch and neither push nor pull_request; no
    pytest/lint step was added to either. This ticket must not spread scope
    beyond test.yml."""
    for name in ("release.yml", "dispatch.yml"):
        text = _workflow_path(name).read_text(encoding="utf-8")
        block = _on_block(text)
        assert re.search(r"^\s*workflow_dispatch\s*:", block, re.M), (
            f"{name}: expected workflow_dispatch trigger, got:\n{block!r}"
        )
        assert not re.search(r"^\s*push\s*:", block, re.M), f"{name}: unexpected push trigger"
        assert not re.search(r"^\s*pull_request\s*:", block, re.M), (
            f"{name}: unexpected pull_request trigger"
        )
        assert "pytest" not in text, f"{name}: unexpected pytest step added"


def test_header_comment_no_longer_claims_push_trigger() -> None:
    """Driving test (R5): the header comment must stop claiming a push
    trigger. RED because line 3 currently reads "Runs pytest on every push
    to any branch and on every PR."."""
    header = _header_comment_block(_test_workflow_text())

    assert "on every push to any branch" not in header.lower()
    assert re.search(r"\bon\s+(?:every\s+)?push\b", header, re.I) is None, (
        f"header still phrases a push trigger:\n{header!r}"
    )
    assert re.search(r"pull request|\bPR\b", header, re.I), (
        f"header no longer mentions the pull_request trigger:\n{header!r}"
    )


def test_header_matrix_rationale_preserved() -> None:
    """Minimal-diff regression guard: the untouched sentences (the
    release.yml contrast sentence, the ubuntu-22.04-pinned rationale) must
    survive the header edit. May already pass -- it targets sentences the
    plan doesn't ask to change."""
    header = _header_comment_block(_test_workflow_text())

    assert "release.yml" in header
    assert "tag-driven" in header
    assert "Matrix: windows-latest + ubuntu-22.04" in header
    assert "glibc" in header
