"""Regression test for ticket #335: bump the `lib-python-projects` exact-tag pin
in `pyproject.toml` from v0.3.20 to v0.3.21 (adds `wait_for_pipeline`).

Floor-based, modelled on tests/test_328_lib_projects_pin.py. The generic
installed-vs-declared and requirement-shape checks live dynamically in
tests/test_319_lib_pins.py and are not re-copied here.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

_MIN_VERSION = Version("0.3.21")
_COMMENT_TAG_RE = re.compile(r"v\d+\.\d+\.\d+")
_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _declared_tag() -> str:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    for entry in data["project"]["dependencies"]:
        if Requirement(entry).name == "lib-python-projects":
            return entry.rsplit("@", 1)[-1].strip()
    raise AssertionError("no 'lib-python-projects' entry found in project.dependencies")


def test_lib_python_projects_pin_meets_v0_3_21_floor() -> None:
    """Driving test (R1): declared pin must be >= v0.3.21. RED while
    pyproject.toml still declares v0.3.20."""
    assert Version(_declared_tag().lstrip("v")) >= _MIN_VERSION


def test_pin_comment_names_a_tag_meeting_the_floor() -> None:
    """Driving test (R1): the comment block above the dependency names the
    declared tag, and that tag meets the floor. RED while it says v0.3.20."""
    declared = _declared_tag()
    lines = _PYPROJECT.read_text(encoding="utf-8").splitlines()
    dep_idx = next(i for i, ln in enumerate(lines) if "lib-python-projects @ git+" in ln)
    start = dep_idx - 1
    while start >= 0 and lines[start].strip().startswith("#"):
        start -= 1
    start += 1
    block = "\n".join(lines[start:dep_idx])
    found = _COMMENT_TAG_RE.findall(block)
    assert found, "expected a vX.Y.Z tag mention in the lib-python-projects comment block"
    assert all(t == declared for t in found), (found, declared)
    assert all(Version(t.lstrip("v")) >= _MIN_VERSION for t in found), found
