"""Tests for ticket #298: two new standalone release scripts extracted so
their pure logic is unit-testable outside the `release.yml` workflow.

`.github/scripts/prev-release-tag.sh` picks the highest *other*
`<plugin>--v<strict-semver>` tag from a list of candidate tags on stdin, so
`gh release create --generate-notes` can be pointed at a `src/<TAG>` marker
tag that actually shares history with the previous release (fixing the
orphan-branch empty-notes bug).

`.github/scripts/marketplace-payload.sh` extracts the inline `jq -n`
marketplace dispatch payload construction (release.yml:382-410) into a
standalone script, reusing the existing truncation filter verbatim.

Runs the real scripts as subprocesses (mirrors how the workflow invokes
them), matching the subprocess-guard convention from
tests/test_245_security_hint_optout.py: skipped entirely when a real bash
is not on PATH at the expected location. jq-dependent tests (all of R2,
since marketplace-payload.sh reuses the jq truncation filter) are
additionally skipped when `jq` isn't on PATH, mirroring
tests/test_274_release_changelog_dispatch.py.

RED today (both scripts do not exist yet): `bash <missing-script>` exits
127 with "No such file or directory" on stderr -- a real, non-zero exit
distinct from a Python-level FileNotFoundError (bash itself is what's
invoked; the missing file is bash's own argument), and distinct from any
setup/environment failure.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _bash() -> str | None:
    if sys.platform == "win32":
        candidate = Path(r"C:\Program Files\Git\bin\bash.exe")
        return str(candidate) if candidate.exists() else None
    return shutil.which("bash")


pytestmark = pytest.mark.skipif(_bash() is None, reason="a real bash is not on PATH")

needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not on PATH")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _scripts_dir() -> Path:
    return _repo_root() / ".github" / "scripts"


def _prev_release_tag_script() -> Path:
    return _scripts_dir() / "prev-release-tag.sh"


def _marketplace_payload_script() -> Path:
    return _scripts_dir() / "marketplace-payload.sh"


def _release_workflow_path() -> Path:
    return _repo_root() / ".github" / "workflows" / "release.yml"


# ---------- R1: prev-release-tag.sh ----------------------------------------


def _run_prev_release_tag(
    stdin_text: str, plugin: str, exclude: str | None = None, timeout: int = 15
) -> SimpleNamespace:
    """Run the script with raw bytes on stdin/stdout, sidestepping the
    Windows `subprocess` text-mode universal-newline translation gotcha
    (writing '\\n' in text mode gets translated to os.linesep on write,
    which would corrupt the deliberate CRLF test below if we used
    text=True for stdin)."""
    bash = _bash()
    argv = [plugin] if exclude is None else [plugin, exclude]
    result = subprocess.run(
        [bash, str(_prev_release_tag_script()), *argv],
        input=stdin_text.encode("utf-8"),
        capture_output=True,
        timeout=timeout,
    )
    return SimpleNamespace(
        returncode=result.returncode,
        stdout=result.stdout.decode("utf-8"),
        stderr=result.stderr.decode("utf-8"),
    )


def test_prev_release_tag_numeric_patch_ordering() -> None:
    """Driving test (R1). RED today: script does not exist (bash exit 127).
    `0.0.9 < 0.0.10` -- a naive string/lexicographic or `sort -V`-free
    comparator must not pick 0.0.9 as the max."""
    stdin = "myplugin--v0.0.9\nmyplugin--v0.0.10\n"
    result = _run_prev_release_tag(stdin, "myplugin")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "myplugin--v0.0.10"


def test_prev_release_tag_prerelease_chain_ordering() -> None:
    """Driving test (R1). `0.1.0-rc.1 < 0.1.0-rc.2 < 0.1.0-rc.10 < 0.1.0` --
    the full chain from the plan, in one stdin batch; the release (no
    prerelease) must outrank every prerelease per semver.org precedence
    rule 11.3 (a version without a prerelease has higher precedence)."""
    stdin = "\n".join(
        [
            "myplugin--v0.1.0-rc.1",
            "myplugin--v0.1.0-rc.2",
            "myplugin--v0.1.0-rc.10",
            "myplugin--v0.1.0",
        ]
    ) + "\n"
    result = _run_prev_release_tag(stdin, "myplugin")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "myplugin--v0.1.0"


def test_prev_release_tag_numeric_prerelease_identifier_compares_numerically() -> None:
    """Driving test (R1). Without the final release present, `rc.10` must
    still beat `rc.2` -- this is exactly the case `sort -V`/lexicographic
    string comparison gets wrong (it would put 'rc.10' before 'rc.2')."""
    stdin = "myplugin--v0.1.0-rc.2\nmyplugin--v0.1.0-rc.10\nmyplugin--v0.1.0-rc.1\n"
    result = _run_prev_release_tag(stdin, "myplugin")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "myplugin--v0.1.0-rc.10"


def test_prev_release_tag_numeric_identifier_lower_than_alphanumeric() -> None:
    """Driving test (R1). semver.org precedence rule 11.4.3: a numeric
    identifier always has lower precedence than an alphanumeric one at the
    same dot-separated position -- `1.0.0-alpha.1 < 1.0.0-alpha.beta`."""
    stdin = "myplugin--v1.0.0-alpha.1\nmyplugin--v1.0.0-alpha.beta\n"
    result = _run_prev_release_tag(stdin, "myplugin")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "myplugin--v1.0.0-alpha.beta"


def test_prev_release_tag_excludes_the_tag_being_created() -> None:
    """Driving test (R1). The tag `release.yml` is about to create must
    never be reported back as "the previous release", even though it is
    itself a valid, numerically-highest match in the candidate list."""
    stdin = "myplugin--v1.0.0\nmyplugin--v1.1.0\n"
    result = _run_prev_release_tag(stdin, "myplugin", exclude="myplugin--v1.1.0")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "myplugin--v1.0.0"


def test_prev_release_tag_ignores_foreign_plugin_tags() -> None:
    """Driving test (R1). A numerically-higher tag belonging to a different
    plugin in the same monorepo/tag namespace must not win."""
    stdin = "otherplugin--v9.9.9\nmyplugin--v0.2.0\n"
    result = _run_prev_release_tag(stdin, "myplugin")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "myplugin--v0.2.0"


def test_prev_release_tag_ignores_src_marker_tags() -> None:
    """Driving test (R1). `src/<plugin>--v<semver>` marker tags (this
    ticket's own new naming scheme, used to give `--generate-notes` real
    history) must not be mistaken for a plugin release tag, even when
    numerically highest -- the `^${PLUGIN}--v${SEMVER}$` anchor must not
    match a 'src/' prefixed line."""
    stdin = "src/myplugin--v9.9.9\nmyplugin--v0.2.0\n"
    result = _run_prev_release_tag(stdin, "myplugin")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "myplugin--v0.2.0"


@pytest.mark.parametrize(
    "bad_tag",
    [
        "myplugin--v01.2.3",  # leading zero
        "myplugin--v1.2",  # missing patch component
        "myplugin--v1.2.3+build",  # build metadata not allowed
    ],
)
def test_prev_release_tag_rejects_non_strict_semver(bad_tag: str) -> None:
    """Driving test (R1/R3). Non-strict-semver tags must be silently
    ignored, not crash the script and not be treated as a match."""
    stdin = f"{bad_tag}\nmyplugin--v0.1.0\n"
    result = _run_prev_release_tag(stdin, "myplugin")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "myplugin--v0.1.0"


def test_prev_release_tag_all_invalid_yields_empty_output() -> None:
    """Driving test (R1). When every candidate line is rejected, the script
    must still exit 0 with empty stdout ('never blocks release'), not error
    out."""
    stdin = "myplugin--v01.2.3\nmyplugin--v1.2\nmyplugin--v1.2.3+build\n"
    result = _run_prev_release_tag(stdin, "myplugin")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_prev_release_tag_empty_stdin_yields_empty_output() -> None:
    """Driving test (R1). Empty stdin (first-ever release of a plugin) must
    print nothing and exit 0."""
    result = _run_prev_release_tag("", "myplugin")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_prev_release_tag_tolerates_blank_lines_and_crlf() -> None:
    """Additional edge-case coverage (R1): blank lines and CRLF line endings
    (as `git tag -l` may emit on a Windows runner) must yield the same
    result as clean LF-only input."""
    stdin = "\r\nmyplugin--v0.1.0\r\n\r\nmyplugin--v0.2.0\r\n\r\n"
    result = _run_prev_release_tag(stdin, "myplugin")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "myplugin--v0.2.0"


def test_prev_release_tag_garbage_only_input_exits_zero() -> None:
    """Additional edge-case coverage (R1): garbage-only input must never
    block the release -- exit 0, empty stdout."""
    stdin = "not-a-tag-at-all\n???\n\t\n"
    result = _run_prev_release_tag(stdin, "myplugin")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_prev_release_tag_exact_anchor_not_confused_by_prefix_tag() -> None:
    """Driving test (R1/R3, plan-critic note): 'myplugin--v0.3.0' is a
    string-prefix of 'myplugin--v0.3.0-rc.1'. The workflow-level
    `gh api repos/$REPO/git/refs/tags/{ref}` call does prefix matching (a
    separate, already-flagged concern for the workflow implementer), but
    this script's own `^${PLUGIN}--v${SEMVER}$` anchor must match each
    candidate line against its FULL contents, so the two tags are never
    conflated. Given both, 0.3.0 (no prerelease) correctly outranks
    0.3.0-rc.1 per semver precedence."""
    stdin = "myplugin--v0.3.0\nmyplugin--v0.3.0-rc.1\n"
    result = _run_prev_release_tag(stdin, "myplugin")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "myplugin--v0.3.0"


# ---------- R3: semver grammar drift guard ----------------------------------


def _release_yml_semver_grammar() -> str:
    """Extract the strict-semver regex literal from release.yml's "Validate
    version is semver" step (the `[[ ! "$V" =~ ^...$  ]]` line), stripped of
    its `^`/`$` anchors so it compares as the bare grammar -- the same shape
    prev-release-tag.sh's SEMVER_RE is defined in before it gets embedded in
    a larger anchored pattern."""
    text = _release_workflow_path().read_text(encoding="utf-8")
    match = re.search(r'\[\[ ! "\$V" =~ (.+?) \]\]', text)
    assert match, (
        "release.yml: could not locate the \"Validate version is semver\" "
        "step's regex -- update this extraction if that step was rewritten"
    )
    literal = match.group(1)
    assert literal.startswith("^") and literal.endswith("$"), (
        f"release.yml: expected the regex to be fully anchored, got {literal!r}"
    )
    return literal[1:-1]


def _prev_release_tag_semver_re() -> str:
    """Extract the SEMVER_RE literal from prev-release-tag.sh."""
    text = _prev_release_tag_script().read_text(encoding="utf-8")
    match = re.search(r"^SEMVER_RE='(.+)'$", text, re.M)
    assert match, "prev-release-tag.sh: could not locate the SEMVER_RE assignment"
    return match.group(1)


def test_semver_grammar_identical_between_release_yml_and_prev_release_tag_sh() -> None:
    """Drift guard (R3, review finding #2). #298's plan calls for one semver
    grammar duplicated as a literal, with a test pinning the copies
    identical. dispatch.yml carries no independent copy of the grammar --
    its `steps.tag.outputs.version` is raw human input from the
    `workflow_dispatch` `tag` field, but it is only ever used downstream
    after "Verify plugin.json version matches tag" confirms it matches
    plugin.json's version on the `release` branch, which release.yml's
    `stamp` job never stamps with a non-semver value (see the comments on
    dispatch.yml's "Resolve tag" and "Checkout scripts from main" steps) --
    so there are only two literal copies left to keep in sync: release.yml's
    "Validate version is semver" step and prev-release-tag.sh's SEMVER_RE.
    This is a two-way drift guard, not the evidence that the grammar itself
    is correct -- that is test_prev_release_tag_rejects_non_strict_semver
    and friends above, which exercise prev-release-tag.sh's copy directly."""
    release_grammar = _release_yml_semver_grammar()
    script_grammar = _prev_release_tag_semver_re()
    assert release_grammar == script_grammar, (
        "semver grammar drifted between release.yml's \"Validate version is "
        "semver\" step and prev-release-tag.sh's SEMVER_RE:\n"
        f"release.yml:          {release_grammar!r}\n"
        f"prev-release-tag.sh:  {script_grammar!r}"
    )


# ---------- R2: marketplace-payload.sh --------------------------------------


REQUIRED_ENV_KEYS = ("NAME", "DESC", "REPO", "VERSION", "TAG")
OPTIONAL_ENV_KEYS = ("CHANGELOG_RAW", "RELEASE_URL")

BASE_ENV = {
    "NAME": "agent-project-issues",
    "DESC": "MCP server for issue tracking",
    "REPO": "Seretos/agent-project-issues",
    "VERSION": "1.2.3",
    "TAG": "agent-project-issues--v1.2.3",
}

_EXPECTED_ICON = (
    f"https://raw.githubusercontent.com/{BASE_ENV['REPO']}/{BASE_ENV['TAG']}"
    "/assets/icon.png"
)
_EXPECTED_DESCRIPTION_URL = (
    f"https://raw.githubusercontent.com/{BASE_ENV['REPO']}/{BASE_ENV['TAG']}"
    "/description.md"
)
_TAGS_LITERAL = ["git", "github", "gitlab", "organisation", "ticket"]

# Test-critic F1: every other R2 case below shares BASE_ENV's exact
# NAME/DESC/REPO/VERSION/TAG literals -- only CHANGELOG_RAW/RELEASE_URL ever
# vary. A hardcoded implementation that ignored the env entirely and always
# emitted BASE_ENV's literal values would pass every "exact value" assertion
# in this file. ALT_ENV differs from BASE_ENV in all five required keys so
# test_marketplace_payload_reflects_distinct_env_values below can rule that
# out.
ALT_ENV = {
    "NAME": "other-plugin",
    "DESC": "A completely different plugin, on purpose",
    "REPO": "SomeOtherOrg/other-repo",
    "VERSION": "9.9.9",
    "TAG": "other-plugin--v9.9.9",
}


def _run_marketplace_payload(
    overrides: dict[str, str], timeout: int = 15
) -> subprocess.CompletedProcess:
    bash = _bash()
    env = dict(os.environ)
    for key in REQUIRED_ENV_KEYS + OPTIONAL_ENV_KEYS:
        env.pop(key, None)
    env.update(overrides)
    return subprocess.run(
        [bash, str(_marketplace_payload_script())],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=timeout,
    )


@needs_jq
def test_marketplace_payload_emits_all_nine_fields_and_omits_changelog() -> None:
    """Driving test (R2). RED today: script does not exist (bash exit 127).
    All 9 `client_payload` fields round-trip through `json.loads` with
    exact values; `changelog` is absent when CHANGELOG_RAW is empty."""
    result = _run_marketplace_payload(dict(BASE_ENV))
    assert result.returncode == 0, result.stderr

    payload = json.loads(result.stdout)
    assert payload["event_type"] == "plugin-release"
    cp = payload["client_payload"]
    assert cp["name"] == BASE_ENV["NAME"]
    assert cp["description"] == BASE_ENV["DESC"]
    assert cp["repo"] == BASE_ENV["REPO"]
    assert cp["category"] == "mcp"
    assert cp["tags"] == _TAGS_LITERAL
    assert cp["version"] == BASE_ENV["VERSION"]
    assert cp["ref"] == BASE_ENV["TAG"]
    assert cp["icon"] == _EXPECTED_ICON
    assert cp["description_url"] == _EXPECTED_DESCRIPTION_URL
    assert "changelog" not in cp


@needs_jq
def test_marketplace_payload_matches_pre_extraction_shape() -> None:
    """Driving test (R2). Pins the exact key set produced by today's inline
    `jq -n` (release.yml:386-407) -- guards against the extraction silently
    adding/renaming/dropping a field."""
    result = _run_marketplace_payload(dict(BASE_ENV))
    assert result.returncode == 0, result.stderr

    payload = json.loads(result.stdout)
    assert set(payload.keys()) == {"event_type", "client_payload"}
    assert set(payload["client_payload"].keys()) == {
        "name",
        "description",
        "repo",
        "category",
        "tags",
        "version",
        "ref",
        "icon",
        "description_url",
    }


@needs_jq
def test_marketplace_payload_reflects_distinct_env_values_not_hardcoded_constants() -> None:
    """Driving test (R2, test-critic F1). Uses ALT_ENV, whose
    NAME/DESC/REPO/VERSION/TAG are all different from BASE_ENV's, and
    asserts the output payload reflects ALT_ENV's specific values -- ruling
    out an implementation that ignores the env and hardcodes BASE_ENV's
    literal values (which would otherwise pass every other test in this
    file, since they all share BASE_ENV)."""
    result = _run_marketplace_payload(dict(ALT_ENV))
    assert result.returncode == 0, result.stderr

    payload = json.loads(result.stdout)
    cp = payload["client_payload"]
    assert cp["name"] == ALT_ENV["NAME"]
    assert cp["description"] == ALT_ENV["DESC"]
    assert cp["repo"] == ALT_ENV["REPO"]
    assert cp["version"] == ALT_ENV["VERSION"]
    assert cp["ref"] == ALT_ENV["TAG"]
    assert cp["icon"] == (
        f"https://raw.githubusercontent.com/{ALT_ENV['REPO']}/{ALT_ENV['TAG']}"
        "/assets/icon.png"
    )
    assert cp["description_url"] == (
        f"https://raw.githubusercontent.com/{ALT_ENV['REPO']}/{ALT_ENV['TAG']}"
        "/description.md"
    )
    # Sanity check on the test itself: if any of these accidentally matched
    # BASE_ENV, the case above would no longer rule out a hardcoded
    # implementation.
    assert cp["name"] != BASE_ENV["NAME"]
    assert cp["description"] != BASE_ENV["DESC"]
    assert cp["repo"] != BASE_ENV["REPO"]
    assert cp["version"] != BASE_ENV["VERSION"]
    assert cp["ref"] != BASE_ENV["TAG"]


@needs_jq
def test_marketplace_payload_includes_changelog_when_present() -> None:
    """Driving test (R2). A non-empty CHANGELOG_RAW under the truncation
    threshold survives unchanged as `client_payload.changelog`."""
    env = dict(BASE_ENV, CHANGELOG_RAW="- did a thing\n- did another")
    env["RELEASE_URL"] = "https://github.com/Seretos/agent-project-issues/releases/tag/v1.2.3"
    result = _run_marketplace_payload(env)
    assert result.returncode == 0, result.stderr

    payload = json.loads(result.stdout)
    assert payload["client_payload"]["changelog"] == env["CHANGELOG_RAW"]


@needs_jq
def test_marketplace_payload_hostile_changelog_survives_byte_for_byte() -> None:
    """Driving test (R2). A hostile changelog body (quotes, backtick, a
    command substitution, a backslash, a literal '\\n' escape sequence, and
    an embedded real newline) must reach the JSON payload byte-for-byte --
    this is the JSON-injection bug class ticket #274 already closed for the
    inline heredoc payload; reuse via `jq --arg` must preserve that."""
    hostile = 'quote:" backtick:` cmd:$(whoami) backslash:\\ literal-newline:\\n embedded\nnewline'
    env = dict(BASE_ENV, CHANGELOG_RAW=hostile, RELEASE_URL="https://example.com/rel")
    result = _run_marketplace_payload(env)
    assert result.returncode == 0, result.stderr

    payload = json.loads(result.stdout)
    assert payload["client_payload"]["changelog"] == hostile


@needs_jq
def test_marketplace_payload_omits_changelog_when_explicitly_empty() -> None:
    """Driving test (R2). Empty CHANGELOG_RAW omits the key entirely (rather
    than emitting `changelog: ""`) and warns on stderr."""
    env = dict(BASE_ENV, CHANGELOG_RAW="", RELEASE_URL="https://example.com/rel")
    result = _run_marketplace_payload(env)
    assert result.returncode == 0, result.stderr

    payload = json.loads(result.stdout)
    assert "changelog" not in payload["client_payload"]
    assert "::warning::" in result.stderr


@needs_jq
def test_marketplace_payload_truncates_long_changelog_body() -> None:
    """Driving test (R2/R3 reuse). A 20000-codepoint body is truncated to
    <=8000 codepoints, ending with the release URL, via the same reserved-
    room truncation filter as release.yml:382. Test-critic F2: also asserts
    the literal truncation marker text itself appears in the output -- a
    filter that truncated and appended the bare URL with no marker text
    would otherwise pass the length/suffix checks alone."""
    url = "https://github.com/Seretos/agent-project-issues/releases/tag/v1.2.3"
    env = dict(BASE_ENV, CHANGELOG_RAW="x" * 20000, RELEASE_URL=url)
    result = _run_marketplace_payload(env)
    assert result.returncode == 0, result.stderr

    changelog = json.loads(result.stdout)["client_payload"]["changelog"]
    assert len(changelog) <= 8000
    assert changelog.endswith(url)
    assert "…truncated — full notes:" in changelog, changelog


@needs_jq
def test_marketplace_payload_leaves_short_changelog_unchanged() -> None:
    """Additional edge-case coverage (R2/R3): a body already under the
    threshold (7900 codepoints) is not touched by the truncation filter."""
    env = dict(BASE_ENV, CHANGELOG_RAW="y" * 7900, RELEASE_URL="https://example.com/rel")
    result = _run_marketplace_payload(env)
    assert result.returncode == 0, result.stderr

    changelog = json.loads(result.stdout)["client_payload"]["changelog"]
    assert changelog == "y" * 7900


@needs_jq
def test_marketplace_payload_truncation_is_codepoint_correct_for_multibyte() -> None:
    """Additional edge-case coverage (R2/R3): a filter that mismeasures
    length by UTF-8 byte length instead of codepoint length would truncate
    a multibyte body far short of the 8000-codepoint budget -- this tight
    lower bound catches that class of bug."""
    env = dict(BASE_ENV, CHANGELOG_RAW="\u00e9" * 20000, RELEASE_URL="https://example.com/rel")
    result = _run_marketplace_payload(env)
    assert result.returncode == 0, result.stderr

    changelog = json.loads(result.stdout)["client_payload"]["changelog"]
    assert 7900 <= len(changelog) <= 8000


@needs_jq
def test_marketplace_payload_keeps_trailing_newline_in_changelog() -> None:
    """Additional edge-case coverage (R2): a trailing newline in
    CHANGELOG_RAW must be preserved -- proves the workflow-side `printf`
    strip (not this script) is the only place a trailing newline is ever
    removed."""
    raw = "line one\nline two\n"
    env = dict(BASE_ENV, CHANGELOG_RAW=raw, RELEASE_URL="https://example.com/rel")
    result = _run_marketplace_payload(env)
    assert result.returncode == 0, result.stderr

    changelog = json.loads(result.stdout)["client_payload"]["changelog"]
    assert changelog == raw


@pytest.mark.parametrize("missing_key", REQUIRED_ENV_KEYS)
def test_marketplace_payload_missing_required_env_var_fails(missing_key: str) -> None:
    """Additional edge-case coverage (R2, per plan). Each of the 5 required
    env vars is mandatory -- omitting any one must fail the script with the
    specific `"<VAR> is required"` message its own `: "${VAR:?...}"` guard
    produces on stderr (matching the script's actual `NAME is required` /
    `DESC is required` / etc. text), not merely a non-zero exit. Not
    jq-guarded: this must fail before or independently of jq being
    available, since the missing var itself is the failure.

    Test-critic F4: `returncode != 0` alone is satisfied by any failure mode
    -- an unrelated crash, a jq error, the script's outright absence (bash
    exit 127) -- and proves nothing about *why* it failed. Asserting the
    exact message ties the failure to the missing-var guard specifically."""
    env = dict(BASE_ENV)
    env.pop(missing_key)
    result = _run_marketplace_payload(env)
    assert result.returncode != 0
    assert f"{missing_key} is required" in result.stderr, result.stderr
