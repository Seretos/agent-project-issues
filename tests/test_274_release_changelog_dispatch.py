"""Tests for ticket #274: carry the *published* GitHub Release body as a new
`changelog` field in the `repository_dispatch` payload sent to
`Seretos/agent-marketplace`, from both `.github/workflows/release.yml` and
`.github/workflows/dispatch.yml`, and move payload construction off the
unquoted `<<EOF` heredoc onto `jq -n --arg` (closing the JSON-injection bug
class that already bit the `tags` field, `agent-marketplace@89aa850`).

Plain text/regex assertions against the workflow YAML read as text -- no
PyYAML/ruamel as a hard dependency (see tests/test_271_ci_trigger_pull_request_only.py,
tests/test_245_security_hint_optout.py), except in the final, explicitly
skippable YAML-still-parses test. The jq-execution test is
`shutil.which("jq")`-guarded, mirroring the `node`-guarded subprocess style
in tests/test_245_security_hint_optout.py -- jq is not on PATH in this dev
sandbox, so that one test is expected to SKIP locally and only exercises for
real where jq is available (e.g. GitHub Actions runners, which ship it).

Retargeted for ticket #298: the jq-built, changelog-carrying, truncating
payload-construction logic that used to be inlined in both workflows'
"Dispatch to agent-marketplace" step moved to the standalone, directly
unit-tested `.github/scripts/marketplace-payload.sh` (see
tests/test_release_scripts.py). Tests that asserted on that logic now assert
against the script's own text (`_script_text`) instead of the workflow run
body (`_dispatch_run_body`); tests that assert on what stayed inline in each
workflow (the `gh release view` body/url read, permissions, env vars, the
`--generate-notes` -> `--notes-file` swap) are unchanged in spirit and still
assert against the run body.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WORKFLOWS = ("release.yml", "dispatch.yml")
DISPATCH_STEP_NAME = "Dispatch to agent-marketplace"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _workflow_path(name: str) -> Path:
    return _repo_root() / ".github" / "workflows" / name


def _workflow_text(name: str) -> str:
    return _workflow_path(name).read_text(encoding="utf-8")


def _script_path(name: str) -> Path:
    return _repo_root() / ".github" / "scripts" / name


def _script_text(name: str) -> str:
    return _script_path(name).read_text(encoding="utf-8")


def _step_block(text: str, step_name: str = DISPATCH_STEP_NAME) -> str:
    """Return the step block: from its `- name:` line up to, but not
    including, the next step at the same indentation (or EOF)."""
    lines = text.splitlines()
    start_pattern = re.compile(r"^(\s*)-\s+name:\s*" + re.escape(step_name) + r"\s*$")
    start_idx = None
    indent = None
    for i, line in enumerate(lines):
        m = start_pattern.match(line)
        if m:
            start_idx, indent = i, len(m.group(1))
            break
    if start_idx is None:
        raise AssertionError(f"step {step_name!r} not found")
    next_step = re.compile(r"^" + " " * indent + r"-\s+\S")
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        if next_step.match(lines[i]):
            end_idx = i
            break
    return "\n".join(lines[start_idx:end_idx])


def _run_body(step_block: str) -> str:
    """Isolate the shell body of the step's `run: |` scalar."""
    lines = step_block.splitlines()
    run_idx = next(
        i for i, l in enumerate(lines) if re.match(r"^\s*run\s*:\s*\|?\s*$", l)
    )
    run_indent = len(lines[run_idx]) - len(lines[run_idx].lstrip())
    end_idx = len(lines)
    for i in range(run_idx + 1, len(lines)):
        line = lines[i]
        if line.strip() == "":
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= run_indent:
            end_idx = i
            break
    return "\n".join(lines[run_idx + 1 : end_idx])


def _dispatch_run_body(name: str) -> str:
    return _run_body(_step_block(_workflow_text(name)))


def _step_env_block(step_block: str) -> str:
    lines = step_block.splitlines()
    env_idx = next(
        (i for i, l in enumerate(lines) if re.match(r"^\s*env\s*:\s*$", l)), None
    )
    if env_idx is None:
        return ""
    env_indent = len(lines[env_idx]) - len(lines[env_idx].lstrip())
    end_idx = len(lines)
    for i in range(env_idx + 1, len(lines)):
        line = lines[i]
        if line.strip() == "":
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= env_indent:
            end_idx = i
            break
    return "\n".join(lines[env_idx + 1 : end_idx])


def _top_level_block(text: str, key: str) -> str:
    """Same slicing idea as test_271's `_on_block`, generalised to any
    column-0 key."""
    lines = text.splitlines()
    key_idx = next(
        (i for i, l in enumerate(lines) if re.match(rf"^{re.escape(key)}\s*:", l)),
        None,
    )
    if key_idx is None:
        return ""
    end_idx = len(lines)
    for i in range(key_idx + 1, len(lines)):
        stripped = lines[i].strip()
        if lines[i].startswith((" ", "\t")):
            continue
        if stripped == "" or stripped.startswith("#"):
            continue
        end_idx = i
        break
    return "\n".join(lines[key_idx:end_idx])


_ARGJSON_MAX_RE = re.compile(r"--argjson\s+max\s+8000\b")


def _extract_truncation_filter(run_body: str) -> str:
    """Find the jq invocation line carrying `--argjson max 8000` and return
    its single-quoted filter program text. Raises AssertionError with a
    clear, legible message (never a bare KeyError/None-indexing crash) when
    no such invocation exists yet -- which is the case on the unfixed code,
    so this is how tests 7/8/10 demonstrate valid RED today."""
    for line in run_body.splitlines():
        if "jq" in line and _ARGJSON_MAX_RE.search(line):
            matches = re.findall(r"'([^']*)'", line)
            if not matches:
                raise AssertionError(
                    "found a jq line with --argjson max 8000 but no "
                    f"single-quoted filter program on it: {line!r}"
                )
            return matches[-1]
    raise AssertionError(
        "no jq invocation with `--argjson max 8000` found in the run body "
        "-- the truncation filter does not exist yet"
    )


def _changelog_region(run_body: str) -> str:
    """The changelog-computation region: from the `gh release view` line
    through the last `jq` line before the `curl` dispatch call, inclusive,
    normalised to strip leading/trailing whitespace per line (comparison is
    modulo indentation, per plan R5)."""
    lines = run_body.splitlines()
    start = next((i for i, l in enumerate(lines) if "gh release view" in l), None)
    if start is None:
        raise AssertionError(
            "no `gh release view` line found to anchor the changelog region"
        )
    curl_idx = next((i for i, l in enumerate(lines) if re.search(r"\bcurl\b", l)), None)
    if curl_idx is None:
        raise AssertionError("no curl dispatch call found")
    jq_indices = [i for i in range(start, curl_idx) if re.search(r"\bjq\b", lines[i])]
    if not jq_indices:
        raise AssertionError(
            "no jq payload-build line found between `gh release view` and `curl`"
        )
    end = jq_indices[-1]
    return "\n".join(l.strip() for l in lines[start : end + 1] if l.strip())


# ---------- R1: read the published release body back ----------------------


def test_both_workflows_read_published_release_body() -> None:
    """Driving test (R1). RED today: neither file contains the string
    `gh release view` at all."""
    for name in WORKFLOWS:
        body = _dispatch_run_body(name)
        assert re.search(r"\bgh release view\b", body), (
            f"{name}: expected a `gh release view` call in the dispatch step, "
            f"got:\n{body!r}"
        )
        json_match = re.search(r"--json\s+(\S+)", body)
        assert json_match, (
            f"{name}: expected a --json flag on `gh release view`, got:\n{body!r}"
        )
        fields = json_match.group(1).split(",")
        assert "body" in fields, f"{name}: --json fields {fields!r} missing 'body'"
        assert "url" in fields, f"{name}: --json fields {fields!r} missing 'url'"


def test_changelog_is_not_regenerated() -> None:
    """R1 edge, retargeted for #298 -- coverage guard, not a driving test.
    Ticket #298 replaces `gh release create --generate-notes` (which
    resolved against the ancestor-less orphan release tag and so always
    produced empty notes) with `--notes-file` against notes generated from
    the real, previous-release-scoped `src/<TAG>` marker tags (computed in
    the "Push source marker + generate release notes" step, which calls the
    `releases/generate-notes` API directly -- that's expected and does not
    violate the invariant this test protects, which is that the
    *dispatch*-to-marketplace step never recomputes/regenerates the
    changelog itself)."""
    release_lines = _workflow_text("release.yml").splitlines()
    dispatch_text = _workflow_text("dispatch.yml")

    generate_notes_idxs = [
        i for i, l in enumerate(release_lines) if "--generate-notes" in l
    ]
    assert not generate_notes_idxs, (
        "release.yml must not call `--generate-notes` anywhere -- #298 "
        f"replaces it with `--notes-file`; found at lines {generate_notes_idxs}"
    )
    notes_file_idxs = [i for i, l in enumerate(release_lines) if "--notes-file" in l]
    assert len(notes_file_idxs) == 1, (
        "expected exactly one `--notes-file` occurrence in release.yml, "
        f"found at lines {notes_file_idxs}"
    )
    release_create_idxs = [
        i for i, l in enumerate(release_lines) if "gh release create" in l
    ]
    assert release_create_idxs, "no `gh release create` call found in release.yml"
    assert notes_file_idxs[0] > release_create_idxs[0], (
        "--notes-file must belong to the `gh release create` invocation"
    )

    assert "--generate-notes" not in dispatch_text, (
        "dispatch.yml must not regenerate notes via --generate-notes"
    )

    for name in WORKFLOWS:
        body = _dispatch_run_body(name)
        assert "releases/generate-notes" not in body, (
            f"{name}: dispatch step must not call the generate-notes API directly"
        )
        assert "git log" not in body, (
            f"{name}: dispatch step must not regenerate changelog via `git log`"
        )


# ---------- R2: JSON-safe, jq-built payload --------------------------------


_HEREDOC_MARKER_RE = re.compile(r"<<-?\s*['\"]?\w+['\"]?")


def test_payload_is_built_by_the_shared_marketplace_payload_script() -> None:
    """Driving test (R2, retargeted for #298). Originally RED because
    both files built the payload inline via `-d @- <<EOF`; #298 moves the
    entire jq-built, changelog-carrying construction into the standalone
    `.github/scripts/marketplace-payload.sh` (unit-tested directly in
    tests/test_release_scripts.py), so each workflow's dispatch step now
    only needs to (a) never re-embed the heredoc pattern the original bug
    lived in, and (b) actually feed curl's stdin from that script's output,
    not from some other inline reconstruction.

    The heredoc check matches ANY heredoc delimiter (`<<EOF`, `<<'JSON'`,
    `<<-PAYLOAD`, ...) appearing on the curl line or immediately around it,
    not just the literal `EOF`, so renaming the delimiter can't sneak the
    heredoc past this test."""
    for name in WORKFLOWS:
        body = _dispatch_run_body(name)
        lines = body.splitlines()

        curl_idx = next(
            (i for i, l in enumerate(lines) if re.search(r"-d\s+@-", l)), None
        )
        assert curl_idx is not None, (
            f"{name}: expected a `curl ... -d @-` dispatch call, got:\n{body!r}"
        )
        curl_neighbourhood = lines[max(0, curl_idx - 3) : curl_idx + 1]
        assert not any(_HEREDOC_MARKER_RE.search(l) for l in curl_neighbourhood), (
            f"{name}: a heredoc marker still feeds `curl ... -d @-` "
            f"(renaming the delimiter away from `EOF` does not close the "
            f"JSON-injection bug), got:\n{curl_neighbourhood!r}"
        )

        script_idxs = [i for i, l in enumerate(lines) if "marketplace-payload.sh" in l]
        assert script_idxs, (
            f"{name}: expected a call to the shared marketplace-payload.sh "
            f"script, got:\n{body!r}"
        )
        var_match = re.match(r"\s*(\w+)=\$\(", lines[script_idxs[0]])
        assert var_match, (
            f"{name}: expected the marketplace-payload.sh call to be a "
            f"shell-variable capture (VAR=$(...)), got: {lines[script_idxs[0]]!r}"
        )
        var = var_match.group(1)
        assert any(
            re.search(rf"\$\{{?{re.escape(var)}\b", l) for l in curl_neighbourhood
        ), (
            f"{name}: could not establish that {var!r} (marketplace-payload.sh's "
            f"captured output) is what feeds curl's stdin; curl context: "
            f"{curl_neighbourhood!r}"
        )


def test_marketplace_payload_script_is_jq_built_and_carries_changelog() -> None:
    """R2, retargeted for #298: the jq-built, changelog-carrying payload
    construction lives in .github/scripts/marketplace-payload.sh now (see
    tests/test_release_scripts.py for the execution-level driving tests);
    this guards against a regression that reintroduces a separate inline
    copy in either workflow instead of calling the shared script."""
    script_text = _script_text("marketplace-payload.sh")
    assert re.search(r"\bjq\s+-r?n\b", script_text), (
        f"expected marketplace-payload.sh to build the payload via `jq -n`/"
        f"`jq -rn`, got:\n{script_text!r}"
    )
    assert "--arg changelog" in script_text, (
        f"expected marketplace-payload.sh to carry the changelog via a jq "
        f"`--arg changelog` pass, got:\n{script_text!r}"
    )


_TAGS_LITERAL = '["git", "github", "gitlab", "organisation", "ticket"]'


def test_all_nine_existing_payload_fields_survive() -> None:
    """R2 edge, regression guard, retargeted for #298: the 9 payload fields
    now live in .github/scripts/marketplace-payload.sh (moved out of both
    workflows' inline heredoc/jq block); this proves the extraction didn't
    drop or rename any of them. Also counts distinct occurrences of these 9
    known keys (not just presence) so a duplicate/aliased key can't slip
    past a presence-only check."""
    known_keys = (
        "name",
        "description",
        "repo",
        "category",
        "tags",
        "version",
        "ref",
        "icon",
        "description_url",
    )
    script_text = _script_text("marketplace-payload.sh")
    match_counts = {
        key: len(re.findall(rf'"?{key}"?\s*[:=]', script_text)) for key in known_keys
    }
    for key, count in match_counts.items():
        assert count >= 1, (
            f"expected payload field {key!r} to survive, got:\n{script_text!r}"
        )
    assert sum(match_counts.values()) == len(known_keys), (
        f"expected exactly {len(known_keys)} occurrences across "
        f"the 9 known payload keys (one each), got counts "
        f"{match_counts!r} (total {sum(match_counts.values())})"
    )
    assert re.search(r'"?category"?\s*[:=]\s*"mcp"', script_text), (
        "category value must remain the literal \"mcp\""
    )
    assert _TAGS_LITERAL in script_text, (
        "expected the exact 5-element tags array to survive unchanged"
    )


def test_event_type_unchanged() -> None:
    """R2 edge, retargeted for #298 -- already GREEN; kept as a regression
    guard against the shared script's event_type value drifting."""
    script_text = _script_text("marketplace-payload.sh")
    assert re.search(r'"?event_type"?\s*[:=]\s*"plugin-release"', script_text), (
        f"expected event_type to remain \"plugin-release\", got:\n{script_text!r}"
    )


def test_no_actions_expression_splicing_into_the_payload_shell() -> None:
    """Driving test (R2 edge). RED today: `${{ github.repository }}` is
    spliced directly into both dispatch steps' `run:` bodies (release.yml's
    `repo`, `icon`, `description_url` fields; dispatch.yml's `repo`, `icon`,
    `description_url` fields too)."""
    for name in WORKFLOWS:
        body = _dispatch_run_body(name)
        assert "${{ github.repository }}" not in body, (
            f"{name}: `${{{{ github.repository }}}}` must not be spliced into "
            f"the run: body, got:\n{body!r}"
        )
        assert "$GITHUB_REPOSITORY" in body, (
            f"{name}: expected `$GITHUB_REPOSITORY` (runner env var) in the "
            f"run: body, got:\n{body!r}"
        )


# ---------- R3: bounded truncation -----------------------------------------


def test_truncation_filter_reserves_room_for_its_suffix() -> None:
    """Driving test (R3, retargeted for #298). Originally RED because no
    `--argjson max 8000` line existed in either workflow; the truncation
    filter now lives in the shared `.github/scripts/marketplace-payload.sh`
    (both workflows call it, see test_both_workflows_use_the_same_truncation_filter),
    so there is exactly one filter to extract and check, not one per
    workflow."""
    try:
        filter_text = _extract_truncation_filter(_script_text("marketplace-payload.sh"))
    except AssertionError as exc:
        pytest.fail(str(exc))

    normalized = re.sub(r"\s+", "", filter_text)
    assert re.search(r"\$max-\(\$\w+\|length\)", normalized), (
        f"expected a subtractive slice end `$max - ($suffix|length)`, "
        f"got filter:\n{filter_text!r}"
    )
    assert not re.search(r"\[:\s*\$max\s*\]", filter_text), (
        "found a bare `[:$max]` slice with no room reserved for the suffix"
    )
    assert not re.search(r"\[:\s*8000\s*\]", filter_text), (
        "found a bare `[:8000]` slice with no room reserved for the suffix"
    )


def _run_truncation_filter(filter_text: str, body: str, url: str) -> str:
    """Run the extracted `jq -rn` truncation filter and return its output
    with exactly one trailing newline stripped -- `jq -r` always appends a
    trailing `\\n` to raw string output, which is not part of the payload
    value itself, so callers comparing against expected content must not see
    it."""
    result = subprocess.run(
        [
            "jq",
            "-rn",
            "--arg",
            "body",
            body,
            "--arg",
            "url",
            url,
            "--argjson",
            "max",
            "8000",
            filter_text,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == 0, (
        f"jq filter failed (rc={result.returncode}):\n"
        f"stderr={result.stderr}\nfilter={filter_text!r}"
    )
    stdout = result.stdout
    if stdout.endswith("\n"):
        stdout = stdout[:-1]
    return stdout


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not on PATH")
def test_extracted_jq_filter_truncates_correctly_when_executed() -> None:
    """R3, execution-level correctness, retargeted for #298. Originally RED
    because no `--argjson max 8000` filter existed yet to extract; the
    filter now lives once, in the shared `marketplace-payload.sh` (see
    test_both_workflows_use_the_same_truncation_filter for the
    both-workflows-call-the-same-script guarantee), so it is extracted and
    executed once here rather than per workflow. Skipped entirely when `jq`
    isn't on PATH; GitHub Actions runners ship jq, so this exercises for
    real in CI.

    All comparisons below run against `_run_truncation_filter`'s return
    value, which has the single trailing `\\n` that `jq -r` always appends
    to raw string output already stripped -- without that strip, exact
    string/length comparisons would be off by one and could never pass even
    for a correct implementation.

    The `reembed` round-trip only proves the tricky-body case survives
    `jq --arg` embedding without crashing the shell -- that JSON-safety
    guarantee is inherent to `jq --arg` for any string, so it can't by
    itself distinguish a correct truncation filter from an incorrect one;
    the assertion tying `reembed`'s parsed value back to `out` is what ties
    the round-trip to this filter's actual output."""
    url = "https://github.com/Seretos/agent-project-issues/releases/tag/v1.2.3"

    try:
        filter_text = _extract_truncation_filter(_script_text("marketplace-payload.sh"))
    except AssertionError as exc:
        pytest.fail(str(exc))

    long_body = "x" * 20000
    out = _run_truncation_filter(filter_text, long_body, url)
    assert len(out) <= 8000
    assert out.endswith(url)
    footer_start = out.index("\n\n")
    assert long_body.startswith(out[:footer_start])

    short_body = "y" * 7900
    out = _run_truncation_filter(filter_text, short_body, url)
    assert out == short_body

    exact_body = "z" * 8000
    out = _run_truncation_filter(filter_text, exact_body, url)
    assert out == exact_body

    over_body = "w" * 8001
    out = _run_truncation_filter(filter_text, over_body, url)
    assert len(out) <= 8000

    tricky_body = 'quote:" backtick:` cmd:$(whoami) backslash:\\ literal-newline:\\n'
    out = _run_truncation_filter(filter_text, tricky_body, url)
    reembed = subprocess.run(
        ["jq", "-n", "--arg", "changelog", out, "{changelog:$changelog}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert reembed.returncode == 0, reembed.stderr
    parsed = json.loads(reembed.stdout)
    assert parsed["changelog"] == out, (
        "round-tripped changelog value diverged from the truncation "
        f"filter's own output: {parsed['changelog']!r} != {out!r}"
    )

    # Codepoint-correct measurement check: a filter that mismeasures
    # length by UTF-8 *byte* length (e.g. `length` applied to raw bytes,
    # or a `[:8000]` slice on byte-decoded input) would truncate a
    # multibyte body far short of the 8000-codepoint budget, not past
    # it -- so an upper bound alone can't catch that class of bug. A
    # tight lower bound forces codepoint-correct measurement.
    multibyte_body = "é" * 20000
    out = _run_truncation_filter(filter_text, multibyte_body, url)
    assert 7900 <= len(out) <= 8000, (
        "expected codepoint-correct truncation to land close to the "
        f"8000-codepoint budget, got len={len(out)} -- a filter that "
        "measures UTF-8 byte length instead of codepoint length would "
        "truncate a multibyte body much shorter than this"
    )


def test_truncation_uses_jq_not_bash_slicing() -> None:
    """R3 edge -- already GREEN today (no truncation logic of any kind
    exists yet); kept as a regression guard against a bash `${VAR:0:N}` or
    byte-based `head -c` slice sneaking in during implementation."""
    for name in WORKFLOWS:
        body = _dispatch_run_body(name)
        assert not re.search(r"\$\{\w+:0:", body), (
            f"{name}: bash slice `${{VAR:0:N}}` found where jq truncation is required"
        )
        assert "head -c" not in body, (
            f"{name}: `head -c` (byte-based truncation) found where jq "
            f"truncation is required"
        )


def test_both_workflows_use_the_same_truncation_filter() -> None:
    """Driving test (R3 edge, retargeted for #298). Originally RED via the
    same missing-filter extraction failure as
    test_truncation_filter_reserves_room_for_its_suffix; #298 guarantees
    "same filter" architecturally by having both workflows call the one
    shared marketplace-payload.sh script (which owns the only copy of the
    filter) instead of each embedding its own -- so this now asserts that
    both dispatch steps actually invoke that shared script, rather than
    extracting and diffing two separate inline copies that no longer
    exist."""
    for name in WORKFLOWS:
        body = _dispatch_run_body(name)
        assert "marketplace-payload.sh" in body, (
            f"{name}: expected the dispatch step to call the shared "
            f"marketplace-payload.sh script (which owns the truncation "
            f"filter), got:\n{body!r}"
        )


# ---------- R4: best-effort degradation ------------------------------------


def test_changelog_failure_warns_and_omits_the_key() -> None:
    """Driving test (R4, retargeted for #298). Originally RED because
    neither file contained any `::warning::` output, `|| VAR=` fallback, or
    conditional guard around the changelog key. #298 splits this behaviour
    across two locations: the `gh release view` read (and its
    `::warning::`) stayed inline in each workflow's dispatch step; the
    truncation call's own fallback and the `.client_payload.changelog`
    conditional guard moved into the shared marketplace-payload.sh (whose
    behaviour is additionally driving-tested end to end -- including the
    `::warning::` and the omitted key -- by
    tests/test_release_scripts.py::test_marketplace_payload_omits_changelog_when_explicitly_empty).

    The `::warning::` check requires the line to live on the failure path
    (near a `|| VAR=` fallback, and not the unconditional first line of the
    run body) so an always-fires top-of-step warning can't satisfy it. The
    conditional-guard check for the changelog-key jq pass scans strictly
    backward from that pass only until the nearest *other* `jq`-bearing
    line, so a guard token belonging to the unrelated truncation filter
    (whose own `if ... then ... else ... end` / `[a:b]` slice would
    otherwise bleed into a fixed-size preceding window) can never satisfy
    it."""
    fallback_re = re.compile(r"\|\|\s*\S+=")
    guard_re = re.compile(r'if\s*\[\[?\s*-n\s*"?\$\w+"?\s*\]\]?')

    for name in WORKFLOWS:
        body = _dispatch_run_body(name)
        lines = body.splitlines()

        warning_idxs = [
            i for i, l in enumerate(lines) if re.search(r"::warning::", l, re.I)
        ]
        assert any(
            re.search(r"changelog|release body", lines[i], re.I) for i in warning_idxs
        ), (
            f"{name}: expected a `::warning::` about the release body/changelog "
            f"read failure, got:\n{body!r}"
        )
        warning_idx = next(
            i
            for i in warning_idxs
            if re.search(r"changelog|release body", lines[i], re.I)
        )

        non_blank_idxs = [i for i, l in enumerate(lines) if l.strip()]
        assert non_blank_idxs and warning_idx != non_blank_idxs[0], (
            f"{name}: `::warning::` must not be the unconditional first "
            f"line of the run body -- that would fire on every run, not "
            f"just on the release-body read failure: {lines[warning_idx]!r}"
        )

        release_view_lines = [
            i for i, l in enumerate(lines) if "gh release view" in l
        ]
        assert release_view_lines, f"{name}: no `gh release view` call found"
        assert any(fallback_re.search(lines[i]) for i in release_view_lines), (
            f"{name}: `gh release view` call has no `|| VAR=` fallback: "
            f"{[lines[i] for i in release_view_lines]!r}"
        )

        fallback_idxs = [i for i, l in enumerate(lines) if fallback_re.search(l)]
        assert any(abs(warning_idx - i) <= 3 for i in fallback_idxs), (
            f"{name}: expected the `::warning::` line to sit near (within "
            f"a few lines of) a `|| VAR=` fallback, indicating it fires on "
            f"the failure path rather than unconditionally; warning at "
            f"line {warning_idx} ({lines[warning_idx]!r}), fallback lines "
            f"at {fallback_idxs}"
        )

    # The truncation-call fallback and the changelog-key conditional guard
    # now live in the shared script.
    script_text = _script_text("marketplace-payload.sh")
    script_lines = script_text.splitlines()

    truncation_lines = [
        i for i, l in enumerate(script_lines) if "--argjson max 8000" in l
    ]
    assert truncation_lines, "no truncation jq call found in marketplace-payload.sh"
    assert any(fallback_re.search(script_lines[i]) for i in truncation_lines), (
        f"marketplace-payload.sh: truncation jq call has no `|| VAR=` "
        f"fallback: {[script_lines[i] for i in truncation_lines]!r}"
    )

    script_warning_idxs = [
        i for i, l in enumerate(script_lines) if re.search(r"::warning::", l, re.I)
    ]
    assert any(
        re.search(r"changelog", script_lines[i], re.I) for i in script_warning_idxs
    ), (
        f"marketplace-payload.sh: expected a `::warning::` mentioning "
        f"changelog, got:\n{script_text!r}"
    )

    key_add_idx = next(
        (i for i, l in enumerate(script_lines) if "client_payload.changelog" in l),
        None,
    )
    assert key_add_idx is not None, (
        "marketplace-payload.sh: no conditional jq pass adding "
        "`.client_payload.changelog` found"
    )
    key_line = script_lines[key_add_idx]
    # Scan strictly backward, stopping at the nearest other jq-bearing
    # line, so an unrelated jq call's own conditional/slice syntax cannot
    # satisfy this guard check.
    guard_lines = []
    for i in range(key_add_idx - 1, -1, -1):
        if re.search(r"\bjq\b", script_lines[i]):
            break
        guard_lines.append(script_lines[i])
    guard_window = "\n".join(reversed(guard_lines))
    assert guard_re.search(key_line) or guard_re.search(guard_window), (
        f"marketplace-payload.sh: expected a shell conditional guard "
        f"(`if [ -n \"$VAR\" ]` / `if [[ -n $VAR ]]`) directly wrapping the "
        f"changelog-key jq pass, with no unrelated jq call in between; key "
        f"line: {key_line!r}, candidate guard window: {guard_window!r}"
    )


def test_dispatch_step_still_fails_hard_on_dispatch_error() -> None:
    """R4 edge -- already GREEN today; not a driving test. Kept so a later
    change can't silently weaken curl's hard failure with `|| true`."""
    for name in WORKFLOWS:
        body = _dispatch_run_body(name)
        lines = body.splitlines()
        curl_idx = next((i for i, l in enumerate(lines) if re.search(r"\bcurl\b", l)), None)
        assert curl_idx is not None, f"{name}: no curl dispatch call found"
        curl_line = lines[curl_idx]
        assert re.search(r"-\w*f\w*", curl_line), (
            f"{name}: curl dispatch call lost its -f (fail-on-error) flag: {curl_line!r}"
        )
        assert "|| true" not in body, (
            f"{name}: dispatch call must not be softened with `|| true`"
        )


# ---------- R5: workflows stay mirrored; dispatch.yml gains credentials ----


def test_dispatch_workflow_mirrors_release_workflow_changelog_logic() -> None:
    """Driving test (R5). RED today: dispatch.yml has no top-level
    `permissions:` block at all and no `GH_TOKEN` in either dispatch step's
    env:, and the changelog-computation region doesn't exist yet in either
    file (so `_changelog_region` raises)."""
    dispatch_text = _workflow_text("dispatch.yml")
    assert re.search(r"^permissions\s*:", dispatch_text, re.M), (
        "dispatch.yml has no top-level `permissions:` block"
    )
    perm_block = _top_level_block(dispatch_text, "permissions")
    assert re.search(r"contents\s*:\s*read", perm_block), (
        f"dispatch.yml permissions block does not grant contents: read, "
        f"got:\n{perm_block!r}"
    )

    for name in WORKFLOWS:
        step_block = _step_block(_workflow_text(name))
        env_block = _step_env_block(step_block)
        assert "GH_TOKEN" in env_block and "secrets.GITHUB_TOKEN" in env_block, (
            f"{name}: dispatch step env: block missing "
            f"`GH_TOKEN: ${{{{ secrets.GITHUB_TOKEN }}}}`, got:\n{env_block!r}"
        )
        assert "GH_PAT" in env_block and "secrets.MARKETPLACE_DISPATCH_TOKEN" in env_block, (
            f"{name}: dispatch step env: block missing GH_PAT, got:\n{env_block!r}"
        )

    try:
        release_region = _changelog_region(_dispatch_run_body("release.yml"))
        dispatch_region = _changelog_region(_dispatch_run_body("dispatch.yml"))
    except AssertionError as exc:
        pytest.fail(f"changelog region not found: {exc}")

    assert release_region == dispatch_region, (
        "changelog-computation region differs between release.yml and "
        f"dispatch.yml:\nrelease.yml:\n{release_region!r}\n"
        f"dispatch.yml:\n{dispatch_region!r}"
    )


def test_workflow_yaml_still_parses() -> None:
    """R5 edge, non-driving: both files already parse today; this guards
    against the rewrite introducing invalid YAML. PyYAML is available in
    this environment (checked: `import yaml` succeeds) but is not a
    declared test dependency, so this is importorskip-guarded to stay
    optional."""
    yaml = pytest.importorskip("yaml")
    for name in WORKFLOWS:
        with _workflow_path(name).open(encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        jobs = doc["jobs"]
        found = any(
            step.get("name") == DISPATCH_STEP_NAME
            for job in jobs.values()
            for step in job.get("steps", [])
        )
        assert found, (
            f"{name}: dispatch step {DISPATCH_STEP_NAME!r} not found after YAML parse"
        )
