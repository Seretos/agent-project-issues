"""Tests for ticket #245: `PROJECT_ISSUES_SECURITY_HINT_DISABLED` opt-out
for the `hooks/security_hint.mjs` UserPromptSubmit hook.

Runs the real hook as a subprocess (mirrors how Claude Code invokes it) so
these tests exercise the actual Node script, not a reimplementation.
Skipped entirely when `node` isn't on PATH.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _hook_path() -> Path:
    return _repo_root() / "hooks" / "security_hint.mjs"


def _state_file(session_id: str) -> Path:
    return Path(tempfile.gettempdir()) / f"project-issues-hook-state-{session_id}.json"


def _run_hook(*, cwd: str, session_id: str, env_overrides: dict | None = None) -> str:
    import subprocess

    env = dict(os.environ)
    env.pop("PROJECT_ISSUES_SECURITY_HINT_DISABLED", None)
    if env_overrides:
        env.update(env_overrides)
    payload = json.dumps({"session_id": session_id, "cwd": cwd})
    result = subprocess.run(
        ["node", str(_hook_path())],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    return result.stdout


@pytest.fixture
def seretos_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".seretos"
    d.mkdir()
    return tmp_path


@pytest.fixture
def session_id() -> str:
    """Fresh per-test session id so tests never share hook rate-limit state."""
    return f"t245-{uuid.uuid4().hex[:12]}"


def _cleanup(session_id: str) -> None:
    state = _state_file(session_id)
    if state.exists():
        state.unlink()


# ---------- B6: opt-out env var ------------------------------------------------


def test_hook_suppressed_when_opt_out_env_set(seretos_dir: Path, session_id: str) -> None:
    try:
        out = _run_hook(
            cwd=str(seretos_dir), session_id=session_id,
            env_overrides={"PROJECT_ISSUES_SECURITY_HINT_DISABLED": "1"},
        )
        assert out == ""
    finally:
        _cleanup(session_id)


def test_hook_control_run_without_env_var_still_emits_hint(
    seretos_dir: Path, session_id: str,
) -> None:
    """Control: same directory/session, no opt-out env var -> hint fires on
    the first qualifying prompt, proving the opt-out (not something else)
    is what suppresses it above."""
    try:
        out = _run_hook(cwd=str(seretos_dir), session_id=session_id)
        assert out != ""
        payload = json.loads(out)
        assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    finally:
        _cleanup(session_id)


def test_hook_falsy_values_do_not_suppress(seretos_dir: Path, session_id: str) -> None:
    for falsy in ("0", "false", "no", "off", ""):
        sid = f"{session_id}-{falsy or 'empty'}"
        try:
            out = _run_hook(
                cwd=str(seretos_dir), session_id=sid,
                env_overrides={"PROJECT_ISSUES_SECURITY_HINT_DISABLED": falsy},
            )
            assert out != "", f"falsy value {falsy!r} unexpectedly suppressed the hook"
        finally:
            _cleanup(sid)


def test_hook_opt_out_is_case_and_whitespace_tolerant(
    seretos_dir: Path, session_id: str,
) -> None:
    for truthy in ("TRUE", " true ", "Yes", "ON", "1"):
        sid = f"{session_id}-{abs(hash(truthy))}"
        try:
            out = _run_hook(
                cwd=str(seretos_dir), session_id=sid,
                env_overrides={"PROJECT_ISSUES_SECURITY_HINT_DISABLED": truthy},
            )
            assert out == "", f"truthy value {truthy!r} did not suppress the hook"
        finally:
            _cleanup(sid)


def test_hook_opt_out_writes_no_state_file(seretos_dir: Path, session_id: str) -> None:
    state = _state_file(session_id)
    assert not state.exists()
    try:
        _run_hook(
            cwd=str(seretos_dir), session_id=session_id,
            env_overrides={"PROJECT_ISSUES_SECURITY_HINT_DISABLED": "1"},
        )
        assert not state.exists(), (
            "an opted-out run must not write a rate-limit state file"
        )
    finally:
        _cleanup(session_id)


def test_hook_source_contains_env_var_name() -> None:
    """Static, node-free assertion — guards against the env var name
    silently drifting between the hook source and its documentation."""
    text = _hook_path().read_text(encoding="utf-8")
    assert "PROJECT_ISSUES_SECURITY_HINT_DISABLED" in text
