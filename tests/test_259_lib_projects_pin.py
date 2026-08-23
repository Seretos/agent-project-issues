"""Regression test for ticket #259: bump the `lib-python-projects` exact-tag
pin in `pyproject.toml` from v0.3.11 to v0.3.12 (tag confirmed real via
`git ls-remote --tags`).

Modelled on tests/test_254_lib_projects_pin.py's tomllib + packaging.requirements
helpers -- reuses the same helper functions verbatim.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

_MIN_VERSION = Version("0.3.12")
_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
_COMMENT_TAG_RE = re.compile(r"v\d+\.\d+\.\d+")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _dependencies() -> list[str]:
    pyproject = _repo_root() / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return data["project"]["dependencies"]


def _lib_python_projects_entry() -> str:
    for entry in _dependencies():
        requirement = Requirement(entry)
        if requirement.name == "lib-python-projects":
            return entry
    raise AssertionError("no 'lib-python-projects' entry found in project.dependencies")


def _tag_from_url(entry: str) -> str:
    # entry looks like: "lib-python-projects @ git+https://.../lib-python-projects@v0.3.10"
    return entry.rsplit("@", 1)[-1].strip()


def _declared_tag() -> str:
    """The exact vX.Y.Z tag currently declared for lib-python-projects in
    pyproject.toml (e.g. "v0.3.12")."""
    entry = _lib_python_projects_entry()
    return _tag_from_url(entry)


def test_lib_python_projects_pin_meets_v0_3_12_floor() -> None:
    """Floor guard, not an exact-pin check: this test intentionally stays
    passing across future chore-ticket bumps (e.g. v0.3.13+) with zero edits.
    `test_installed_lib_matches_declared_pin` is what enforces exactness
    against the live environment."""
    tag = _declared_tag()
    assert Version(tag.lstrip("v")) >= _MIN_VERSION


def test_pin_is_an_exact_tag_not_a_branch() -> None:
    """Regression guard: the pin must stay an exact vX.Y.Z tag, never a
    floating branch like `release/0.x` (that's lib-python-config's scheme,
    not lib-python-projects')."""
    entry = _lib_python_projects_entry()
    tag = _tag_from_url(entry)
    assert _TAG_RE.match(tag), f"expected an exact 'vX.Y.Z' tag, got {tag!r}"


def test_pin_url_shape_is_sync_libs_parseable() -> None:
    """scripts/sync-libs.ps1 regex-parses dependency lines with the pattern
    '"(lib-python-[^"]+@[^"]+)"'. Confirm the raw string still matches that
    shape, and that the host/repo path is unchanged."""
    pyproject = _repo_root() / "pyproject.toml"
    raw = pyproject.read_text(encoding="utf-8")

    # sync-libs.ps1's actual pattern: '"(lib-python-[^"]+@[^"]+)"'
    sync_pattern = re.compile(r'"(lib-python-[^"]+@[^"]+)"')
    matches = sync_pattern.findall(raw)
    projects_matches = [m for m in matches if m.startswith("lib-python-projects")]
    assert projects_matches, "sync-libs.ps1's pattern found no lib-python-projects match"

    entry = projects_matches[0]
    assert entry.startswith(
        "lib-python-projects @ git+https://github.com/Seretos/lib-python-projects@"
    )


def test_dependency_names_still_parse() -> None:
    """Every entry in project.dependencies must parse via Requirement(...),
    and the lib-python-projects lookup must actually find a match -- fail
    loudly on a miss, don't skip."""
    names = set()
    for entry in _dependencies():
        requirement = Requirement(entry)
        names.add(requirement.name)

    assert "lib-python-projects" in names
    assert "lib-python-config" in names
    assert "mcp" in names


def test_lib_python_config_pin_unchanged() -> None:
    """This ticket only bumps lib-python-projects; lib-python-config must
    stay on its floating `release/0.x` branch, unaffected by the bump."""
    for entry in _dependencies():
        requirement = Requirement(entry)
        if requirement.name == "lib-python-config":
            assert entry.endswith("@release/0.x"), (
                f"expected lib-python-config to still float on release/0.x, got {entry!r}"
            )
            return
    raise AssertionError("no 'lib-python-config' entry found in project.dependencies")


def test_installed_lib_matches_declared_pin() -> None:
    """Coverage: the environment this suite runs in must have EXACTLY the
    tag declared in pyproject.toml installed -- not a stale/local shadow,
    and not a newer unreleased dev checkout either. RED in a pre-sync
    environment (e.g. bare `python -m pytest` after only the pyproject.toml
    edit, before `pwsh -File scripts/test.ps1` / `scripts/sync-libs.ps1` has
    re-pulled the new tag), and RED for a local editable shadow ahead of the
    declared pin."""
    import importlib.metadata

    declared = _declared_tag().lstrip("v")

    try:
        installed = importlib.metadata.version("lib-python-projects")
    except importlib.metadata.PackageNotFoundError:
        raise AssertionError(
            "lib-python-projects is not installed in this environment -- "
            "run 'pwsh -File scripts/test.ps1' (or scripts/sync-libs.ps1) "
            "instead of a bare 'python -m pytest'"
        )

    assert installed == declared, (
        f"installed lib-python-projects=={installed} does not match the "
        f"declared pin ({declared}); run 'pwsh -File scripts/test.ps1' "
        "to sync the environment to pyproject.toml's pin (this catches a "
        "stale/local shadow in either direction -- older OR newer than the "
        "declared tag)"
    )


def test_pin_comment_matches_declared_tag() -> None:
    """The explanatory comment above the lib-python-projects dependency line
    names the pinned version (e.g. "Pinned to an exact immutable tag
    (v0.3.12)."). Every vX.Y.Z-shaped token anywhere in the comment/dependency
    block for lib-python-projects must equal the declared tag, so a future
    bump that edits the dependency line but forgets the comment fails loudly.
    Keyed off the declared tag (not hardcoded) so it survives future bumps."""
    declared = _declared_tag()

    pyproject = _repo_root() / "pyproject.toml"
    lines = pyproject.read_text(encoding="utf-8").splitlines()

    # Isolate the lib-python-projects comment + dependency lines: the
    # comment immediately precedes the dependency line in pyproject.toml.
    dep_line_idx = next(
        i for i, line in enumerate(lines) if "lib-python-projects @ git+" in line
    )
    comment_idx = dep_line_idx - 1
    while comment_idx >= 0 and lines[comment_idx].strip().startswith("#"):
        comment_idx -= 1
    comment_idx += 1

    block = "\n".join(lines[comment_idx : dep_line_idx + 1])
    found_tags = _COMMENT_TAG_RE.findall(block)

    assert found_tags, "expected at least one vX.Y.Z tag mention in the lib-python-projects block"
    assert all(tag == declared for tag in found_tags), (
        f"found tag mention(s) {found_tags!r} that don't match the declared "
        f"pin {declared!r} in the lib-python-projects comment/dependency block"
    )
