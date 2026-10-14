"""Shared helpers for the ``test_ai_review_workflows*`` tests.

Workflow and prompt readers, step extraction, the bash runner and its stubs, and the
lane tables the parts read.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

# Every privileged Stage-2 fork reviewer. They share one trigger contract, so the
# trigger assertions run against the whole set rather than one sampled lane.
FORK_REVIEW_LANES = (
    "fork-opus-review.yml",
    "fork-gpt-review.yml",
    "fork-design-review.yml",
    "fork-ux-review.yml",
    "fork-first-principles-review.yml",
    "fork-security-scope-review.yml",
)

REVIEW_PROMPTS = ROOT / ".github" / "review-prompts"

#: Variables a Windows child needs even when the test controls the rest of its
#: environment. A from-scratch `env=` dict is harmless on POSIX, where these tests
#: were written, but on Windows dropping `SystemRoot` / `COMSPEC` leaves the child
#: unable to resolve system DLLs or a shell -- which is how a step that runs fine
#: locally comes back from the Windows shard with no usable output at all. Only
#: names the OS itself needs; nothing about the test's own inputs leaks in.
_WINDOWS_CHILD_KEEP = ("SystemRoot", "SYSTEMROOT", "COMSPEC", "SystemDrive", "TEMP", "TMP")


def _child_env(env: "dict[str, str]") -> "dict[str, str]":
    """`env` plus the variables a Windows child cannot start without."""
    if os.name != "nt":
        return env
    merged = dict(env)
    for name in _WINDOWS_CHILD_KEEP:
        value = os.environ.get(name)
        if value is not None:
            merged.setdefault(name, value)
    return merged


def _proc_log(result: "subprocess.CompletedProcess[str]") -> str:
    """A failure message from a completed process that cannot itself raise.

    `result.stdout + result.stderr` raises when either stream is None -- observed on
    the Windows shard even with `capture_output=True`, cause not established. Since
    these streams exist only to explain a failed assertion, build the message by
    repr: a None then READS as `stdout=None` in the report rather than replacing the
    diagnostic with a TypeError or, worse, with an empty string.
    """
    return f"rc={result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"


def _bash() -> str | None:
    """Return a Bash that can consume native paths from this Python process.

    On Windows, ``shutil.which("bash")`` commonly resolves to the WSL launcher
    in System32.  That executable starts a Linux process but does not translate
    the Windows argv paths or inherit arbitrary environment variables, so these
    host-side workflow tests produce false failures.  Git for Windows ships a
    native-path-aware Bash; prefer it when available.
    """
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            candidate = Path(git).resolve().parent.parent / "bin" / "bash.exe"
            if candidate.is_file():
                return str(candidate)
        for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(env_name)
            if root:
                candidate = Path(root) / "Git" / "bin" / "bash.exe"
                if candidate.is_file():
                    return str(candidate)
        return None
    return shutil.which("bash")


def _prompt(name: str) -> str:
    """Read a review-prompt file.

    The contract the reviewer obeys lives here, not in the workflow, so a
    contract assertion must read the prompt or it proves nothing.
    """
    return (REVIEW_PROMPTS / name).read_text(encoding="utf-8")


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _stub_path(tmp_path: Path) -> str:
    """PATH for executing a workflow read block with stubbed commands.

    ``tmp_path`` comes first so the ``gh``/``sleep`` stubs win. The read
    blocks pipe through the host's standalone ``jq``, which Git Bash may install
    outside the Unix defaults. Put its discovered directory before those
    defaults so an older system copy cannot shadow it; skip where jq is absent.
    """
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("the read block pipes through jq; skip where jq is absent")
    return os.pathsep.join(
        [
            str(tmp_path),
            str(Path(jq).parent),
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
        ]
    )


def _flat(text: str) -> str:
    """Collapse whitespace runs so prose assertions survive re-wrapping.

    The review prompts are hand-wrapped markdown; asserting on a phrase that
    happens to straddle a line break would make these tests fail on a reflow that
    changes nothing about the contract.
    """
    return re.sub(r"\s+", " ", text)


def _step_script(workflow: str, step_name: str) -> str:
    step_start = workflow.index(f"      - name: {step_name}")
    run_start = workflow.index("        run: |\n", step_start) + len("        run: |\n")
    # The next step may begin with `- uses:` rather than `- name:`; stopping only
    # at `- name:` would splice that step's YAML into the returned script.
    nxt = re.search(r"\n      - (?:name|uses):", workflow[run_start:])
    step_end = len(workflow) if nxt is None else run_start + nxt.start()
    return "\n".join(
        line[10:] if line.startswith("          ") else line
        for line in workflow[run_start:step_end].splitlines()
    )


def _shell_function(script: str, function_name: str) -> str:
    lines = script.splitlines()
    start = lines.index(f"{function_name}() {{")
    end = lines.index("}", start)
    return "\n".join(lines[start : end + 1])


UX_LANES = ("ux-review.yml", "fork-ux-review.yml")

# The one fetch loop both steps source. The fork lane runs it from its
# trusted base checkout, so a fork cannot alter what fetches its evidence.
ATTACHMENT_SCRIPT = ".github/scripts/pr-attachment-evidence.sh"

# The ONE read of the PR's mutable title and description, shared by both
# consumers in a job. Sourced by sibling path rather than through an env var,
# so a lane cannot silently opt out of it.
SNAPSHOT_SCRIPT = ".github/scripts/pr-body-snapshot.sh"
SNAPSHOT_SOURCE_FRAGMENT = "/pr-body-snapshot.sh"


def _attachment_script() -> str:
    return (ROOT / ATTACHMENT_SCRIPT).read_text(encoding="utf-8")


def _snapshot_script() -> str:
    return (ROOT / SNAPSHOT_SCRIPT).read_text(encoding="utf-8")


_SCOPE_LANES = ("security-scope-review.yml", "fork-security-scope-review.yml")


def _step_by_name(workflow_name: str, job_id: str, name_fragment: str) -> dict:
    """One step of one job, addressed by a fragment of its `name:`."""
    job = (yaml.safe_load(_workflow(workflow_name)).get("jobs") or {})[job_id]
    for step in job.get("steps") or []:
        if name_fragment in str(step.get("name", "")):
            return step
    raise AssertionError(f"{workflow_name}:{job_id} has no step named like {name_fragment!r}")


#: The one capture every description-reading lane sources.
CAPTURE_SCRIPT = ROOT / ".github" / "scripts" / "pr-description-capture.sh"
CAPTURE_SOURCE_LINE = '. "$GITHUB_WORKSPACE/.github/scripts/pr-description-capture.sh"'


def _fork_child_env(tmp_path: Path, stub_dir: Path, **extra: str) -> dict[str, str]:
    """A from-scratch child env: stubs first on PATH, HOME and TMPDIR in tmp_path.

    No live credential can reach a stub-resolution miss: GH_TOKEN is a dummy and
    GH_CONFIG_DIR keeps a real gh, if one were reached, from loading the user's
    persisted authentication.
    """
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("the step pipes through jq; skip where jq is absent")
    path = os.pathsep.join(
        [str(stub_dir), str(Path(jq).parent), "/usr/local/bin", "/usr/bin", "/bin"]
    )
    env = {
        "PATH": path,
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "GH_TOKEN": "stub-token",
        "GH_CONFIG_DIR": str(tmp_path / "gh-config"),
        "LC_ALL": "C",
    }
    env.update(extra)
    return _child_env(env)


def _github_outputs(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            out[key] = value
    return out


def _write_stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8", newline="\n")
    path.chmod(0o755)


# Both first-principles lanes carry byte-identical reasoning, so every
# contract assertion runs against the pair.
FP_LANES = ("first-principles-review.yml", "fork-first-principles-review.yml")


def _reads_header_anchored(workflow: str, header: str) -> bool:
    """Whether *workflow*'s verdict CAPTURE reads *header* anchored to line start.

    Tool-agnostic on purpose. A lane may read its verdict header with ``grep
    -iE '^Header:'`` or with an ``awk`` record match ``!found && tolower($0) ~
    /^header:/``; what the lanes must not lose is the ANCHOR, because an
    unanchored read turns a model that merely mentions the header in prose into
    a verdict. A pin naming one program stops finding the capture the day the
    capture changes tool, and then passes while measuring nothing.

    The match is the verdict CAPTURE expression in either spelling, not a bare
    anchored header anywhere in the file: these steps also TRIM the summary to
    the header one or more lines above the capture (``grep -qiE '^Header:'`` and
    ``awk 'f || tolower($0) ~ /^header:/'``), and a file-wide search for ``^Header:``
    is satisfied by the trim alone. That would pass even with the capture deleted
    outright, measuring the trim instead of the verdict read. Both capture
    spellings carry a mark the trim does not: the grep capture has no ``-q`` and
    is followed by ``head -n1``; the awk capture opens ``!found &&`` where the
    trim opens ``f ||``.
    """
    escaped = re.escape(header)
    lowered = re.escape(header.lower())
    return (
        re.search(rf"grep -iE '\^{escaped}' \| head", workflow, re.IGNORECASE) is not None
        or re.search(rf"!found && tolower\(\$0\) ~ /\^{lowered}/", workflow) is not None
    )


def _review_prompt(stage: str) -> str:
    """Read a shared Opus review prompt (`opus-discovery` / `opus-validate`)."""
    return (REVIEW_PROMPTS / f"{stage}.md").read_text(encoding="utf-8")


def _line_containing(text: str, *substrings: str) -> str:
    """First line in `text` that contains every one of `substrings`."""
    for line in text.splitlines():
        if all(s in line for s in substrings):
            return line
    raise AssertionError(f"no line contains all of {substrings!r}")


def _fp_contract() -> str:
    """The first-principles review contract -- one file, loaded by both lanes."""
    return (REVIEW_PROMPTS / "first-principles.md").read_text(encoding="utf-8")


def _step(workflow_name: str, step_name: str) -> dict:
    doc = yaml.safe_load((WORKFLOWS / workflow_name).read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        for step in job["steps"]:
            if step.get("name") == step_name:
                return step
    raise AssertionError(f"{workflow_name}: no step named {step_name!r}")


def _step_env(workflow_name: str, step_name: str) -> dict[str, str]:
    return {k: str(v) for k, v in (_step(workflow_name, step_name).get("env") or {}).items()}


def _exec_transcript(runner_temp: Path, review_text: str) -> dict[str, str]:
    """Write a claude-code execution_file fixture and return its env binding.

    The design-family lanes read the model's review text from the action's
    transcript (a single result object is one of the three shapes the step's
    slurp+flatten parse accepts).
    """
    exec_file = runner_temp / "execution.json"
    exec_file.write_text(json.dumps({"result": review_text}), encoding="utf-8")
    return {"EXEC_FILE": str(exec_file), "REVIEW_OUTCOME": "success"}


def _fork_scope_comment(
    cwd: Path, runner_temp: Path, head: str, *, marker_head: str | None, rows: bool = True
) -> dict[str, str]:
    """Write the fork scope lane's comment body the way the lane itself writes it.

    That lane's upsert step consumes ``$RUNNER_TEMP/scope-comment.md``, which a
    SEPARATE step composes, so the fixture runs the real "Assemble the comment
    body" bash rather than hand-writing a body. Hand-writing it would pin a shape
    the lane never emits, and the withheld-notice wording is the exact thing the
    guard reads -- a fixture that drifted there would report a guarantee about
    text no run produces.

    ``marker_head`` is the sha the model's own text claims: this run's head for a
    completed verdict, a different sha (or ``None`` for no text at all) for
    output that cannot be attributed to this revision.

    ``rows`` says whether the CLASSIFIER folded a verdict. It is a separate axis
    from the marker: the classifier measured both refs itself, so its rows are
    attributable to this head even when the model half produced nothing. With
    ``rows=False`` nothing was measured at all, which is the state that leaves the
    body with no stamp of any kind.
    """
    bash = _bash()
    if bash is None:
        pytest.skip("the assemble step is Bash; skip where Bash is absent")
    review = cwd / "review" / "scope-review-output.md"
    review.parent.mkdir(parents=True, exist_ok=True)
    if marker_head is None:
        review.write_text("", encoding="utf-8")
    else:
        review.write_text(
            "Scope-Verdict: PASS\n\nno legitimate operation newly refused\n\n"
            f"[SCOPE-REVIEWED] {marker_head}\n",
            encoding="utf-8",
        )
    folded = ""
    if rows:
        folded = (
            "### Adjudicated candidates\n\n| operation | base | head |\n"
            "| --- | --- | --- |\n| `git status` | allowed | allowed |\n"
        )
    body = runner_temp / "scope-verdict.md"
    body.write_text(folded, encoding="utf-8")
    present = marker_head == head
    script = _step_script(_workflow("fork-security-scope-review.yml"), "Assemble the comment body")
    result = subprocess.run(
        [bash, "-e", "-c", script],
        check=False,
        capture_output=True,
        cwd=cwd,
        env={
            **os.environ,
            "HEAD": head,
            "CONCLUSION": "success" if present else "failure",
            "TITLE": (
                "PASS - no legitimate operation newly refused"
                if present
                else "no [SCOPE-REVIEWED] marker for this head"
            ),
            "MARKER_STATE": "present" if present else "absent",
            "REVIEW": "review/scope-review-output.md",
            "BODY": str(body),
            "OUT": str(runner_temp / "scope-comment.md"),
        },
    )
    assert result.returncode == 0, result.stderr.decode()
    return {}


# One entry per review lane that carries the guarded comment upsert. Each
# describes how to drive that lane's REAL posting step into a completed run
# (body carries "<stamp> <head>") and an incomplete one (no verdict for the
# current head), plus the phrase its incomplete body is known by.
_GUARDED_LANES = [
    {
        # The same-repo GPT lane. It is FAIL-CLOSED: its gate reads the captured
        # review text rather than the PR, so a completed verdict that did not
        # reach the slot exits non-zero (`missed_slot_rc`), where every other
        # guarded lane stays green and lets its annotation carry the loss. The
        # upsert itself is the same byte-identical function; the exit is read
        # off its `publication` output after it returns.
        "id": "codex",
        "workflow": "codex-review.yml",
        "step": "Post/update review comment",
        "marker": "<!-- codex-ai-review -->",
        "stamp": "[GPT-REVIEWED]",
        "incomplete_text": "review incomplete",
        "needs_perl": False,
        "missed_slot_rc": 1,
        "env": {
            "HUMAN_OVERRIDE": "false",
            "OVERRIDE_ACTOR": "",
            "OVERRIDE_SOURCE": "",
            "REFUSED": "",
            "ADJ_DECISION": "",
            "ADJ_NOTE": "",
        },
        "completed": lambda cwd, rt, head: (
            (cwd / "codex-review-output.md").write_text(
                f"FINDINGS\n[GPT-REVIEWED] {head}\n", encoding="utf-8"
            ),
            {},
        )[1],
        "incomplete": lambda cwd, rt, head: {},
    },
    {
        "id": "fork-gpt",
        "workflow": "fork-gpt-review.yml",
        "step": "Post/update summary comment",
        "marker": "<!-- codex-ai-review -->",
        "stamp": "[GPT-REVIEWED]",
        "incomplete_text": "review incomplete",
        "needs_perl": False,
        "env": {"ADJ_DECISION": "", "ADJ_NOTE": ""},
        "completed": lambda cwd, rt, head: (
            (cwd / "codex-review-output.md").write_text(
                f"FINDINGS\n[GPT-REVIEWED] {head}\n", encoding="utf-8"
            ),
            {},
        )[1],
        "incomplete": lambda cwd, rt, head: {},
    },
    {
        "id": "fork-opus",
        "workflow": "fork-opus-review.yml",
        "step": "Post/update summary comment",
        "marker": "<!-- claude-ai-review -->",
        "stamp": "[OPUS-REVIEWED]",
        "incomplete_text": "review incomplete",
        "needs_perl": False,
        "env": {},
        "completed": lambda cwd, rt, head: (
            (cwd / "claude-review-output.md").write_text(
                f"FINDINGS\n[OPUS-REVIEWED] {head}\n", encoding="utf-8"
            ),
            {},
        )[1],
        "incomplete": lambda cwd, rt, head: {},
    },
    {
        "id": "design",
        "workflow": "design-review.yml",
        "step": "Post design review summary",
        "marker": "<!-- design-review -->",
        "stamp": "[DESIGN-REVIEWED]",
        "incomplete_text": "could not complete",
        "needs_perl": True,
        "env": {"HUMAN_OVERRIDE": "false", "OVERRIDE_ACTOR": "", "ACTOR": "someone"},
        "completed": lambda cwd, rt, head: _exec_transcript(
            rt, f"Design-Verdict: PASS\n\nsolid reasoning\n\n[DESIGN-REVIEWED] {head}\n"
        ),
        "incomplete": lambda cwd, rt, head: _exec_transcript(
            rt, "Design-Verdict: PASS\n\nstale reasoning\n\n[DESIGN-REVIEWED] feedbead\n"
        ),
    },
    {
        "id": "ux",
        "workflow": "ux-review.yml",
        "step": "Post UX review summary",
        "marker": "<!-- ux-review -->",
        "stamp": "[UX-REVIEWED]",
        "incomplete_text": "could not complete",
        "needs_perl": True,
        "env": {
            "HUMAN_OVERRIDE": "false",
            "OVERRIDE_ACTOR": "",
            "ACTOR": "someone",
            "UI_SCOPE": "true",
        },
        "completed": lambda cwd, rt, head: _exec_transcript(
            rt, f"UX-Verdict: PASS\n\nsolid reasoning\n\n[UX-REVIEWED] {head}\n"
        ),
        "incomplete": lambda cwd, rt, head: _exec_transcript(
            rt, "UX-Verdict: PASS\n\nstale reasoning\n\n[UX-REVIEWED] feedbead\n"
        ),
    },
    {
        "id": "first-principles",
        "workflow": "first-principles-review.yml",
        "step": "Post first-principles review summary",
        "marker": "<!-- first-principles-review -->",
        "stamp": "[FIRST-PRINCIPLES-REVIEWED]",
        "incomplete_text": "could not complete",
        "needs_perl": True,
        "env": {
            "HUMAN_OVERRIDE": "false",
            "OVERRIDE_ACTOR": "",
            "ACTOR": "someone",
            "SURFACE": "true",
            "CONTRACT": "true",
        },
        "completed": lambda cwd, rt, head: _exec_transcript(
            rt,
            "First-Principles-Verdict: PASS\n\nsolid reasoning\n\n"
            f"[FIRST-PRINCIPLES-REVIEWED] {head}\n",
        ),
        "incomplete": lambda cwd, rt, head: _exec_transcript(
            rt,
            "First-Principles-Verdict: PASS\n\nstale reasoning\n\n"
            "[FIRST-PRINCIPLES-REVIEWED] feedbead\n",
        ),
    },
    {
        "id": "fork-design",
        "workflow": "fork-design-review.yml",
        "step": "Post/update design review comment",
        "marker": "<!-- design-review -->",
        "stamp": "[DESIGN-REVIEWED]",
        "incomplete_text": "could not complete",
        "needs_perl": False,
        "env": {},
        "completed": lambda cwd, rt, head: (
            (rt / "design-review-output.md").write_text(
                f"solid reasoning\n\n[DESIGN-REVIEWED] {head}\n", encoding="utf-8"
            ),
            {"VERDICT": "PASS", "REVIEW_OUTCOME": "success"},
        )[1],
        "incomplete": lambda cwd, rt, head: {"VERDICT": "UNKNOWN", "REVIEW_OUTCOME": "failure"},
    },
    {
        "id": "fork-ux",
        "workflow": "fork-ux-review.yml",
        "step": "Post UX review summary",
        "marker": "<!-- ux-review -->",
        "stamp": "[UX-REVIEWED]",
        "incomplete_text": "could not complete",
        "needs_perl": True,
        "env": {"UI_SCOPE": "true"},
        "completed": lambda cwd, rt, head: _exec_transcript(
            rt, f"UX-Verdict: PASS\n\nsolid reasoning\n\n[UX-REVIEWED] {head}\n"
        ),
        "incomplete": lambda cwd, rt, head: _exec_transcript(
            rt, "UX-Verdict: PASS\n\nstale reasoning\n\n[UX-REVIEWED] feedbead\n"
        ),
    },
    {
        "id": "fork-first-principles",
        "workflow": "fork-first-principles-review.yml",
        "step": "Post/update first-principles review comment",
        "marker": "<!-- first-principles-review -->",
        "stamp": "[FIRST-PRINCIPLES-REVIEWED]",
        "incomplete_text": "could not complete",
        "needs_perl": False,
        "env": {"WITHHELD": ""},
        "completed": lambda cwd, rt, head: (
            (rt / "first-principles-output.md").write_text(
                f"solid reasoning\n\n[FIRST-PRINCIPLES-REVIEWED] {head}\n", encoding="utf-8"
            ),
            {"VERDICT": "PASS", "REVIEW_OUTCOME": "success"},
        )[1],
        "incomplete": lambda cwd, rt, head: {"VERDICT": "UNKNOWN", "REVIEW_OUTCOME": "failure"},
    },
    {
        # The incomplete case is the ordinary one: the candidate stage succeeded
        # and left no current-head marker, so the review cannot be attributed to
        # this revision. `FOLDED=clean` keeps the classifier half silent, which
        # is what makes this an incomplete run rather than a script-confirmed
        # one -- the confirmed-rows path is a different contract and has its own
        # cases below.
        "id": "security-scope",
        "workflow": "security-scope-review.yml",
        "step": "Post the scope verdict",
        "marker": "<!-- security-scope-review -->",
        "stamp": "[SCOPE-REVIEWED]",
        "incomplete_text": "could not complete",
        "needs_perl": False,
        # This lane's posting step scrubs with the base-owned redactor rather than
        # an embedded program, so the harness supplies the same two things the
        # workflow does: the staged script's path, and a `python` that runs it.
        "needs_redactor": True,
        "env": {
            "HUMAN_OVERRIDE": "false",
            "OVERRIDE_ACTOR": "",
            "ACTOR": "someone",
            # A folded verdict of `clean` keeps the script half out of the way,
            # so each case turns on the model half exactly as the lane scores it.
            "FOLDED": "clean",
            "ADJUDICATED": "true",
            "GENERATE_RESULT": "success",
            "VALIDATE_RESULT": "success",
            "ADJUDICATE_RESULT": "success",
            # The post step runs the shared conclusion table from the staged
            # committed-blob harness ($HARNESS/scope_candidates.py), through the
            # `python` shim `needs_redactor` installs. Point it at the real script.
            "HARNESS": str(ROOT / "scripts"),
        },
        "completed": lambda cwd, rt, head: (
            (cwd / "scope-review.md").write_text(
                f"Scope-Verdict: PASS\n\nnothing newly refused\n\n[SCOPE-REVIEWED] {head}\n",
                encoding="utf-8",
            ),
            {},
        )[1],
        "incomplete": lambda cwd, rt, head: (
            (cwd / "scope-review.md").write_text(
                "Scope-Verdict: PASS\n\nstale reasoning\n\n[SCOPE-REVIEWED] feedbead\n",
                encoding="utf-8",
            ),
            {},
        )[1],
    },
    {
        # The body this lane upserts is composed by a different step, so both
        # cases run that step's real bash through `_fork_scope_comment` instead
        # of describing its output.
        "id": "fork-security-scope",
        "workflow": "fork-security-scope-review.yml",
        "step": "Post/update the scope review comment",
        "marker": "<!-- security-scope-review -->",
        "stamp": "[SCOPE-REVIEWED]",
        "incomplete_text": "the model's text is withheld",
        "needs_perl": False,
        "env": {},
        "completed": lambda cwd, rt, head: _fork_scope_comment(cwd, rt, head, marker_head=head),
        # `rows=False`: the shared incomplete contract is a run that measured
        # NOTHING, which is what leaves a body with no stamp at all. A run whose
        # classifier folded rows while the model half died is a different case and
        # has its own test, matching how the same-repo entry above is driven.
        "incomplete": lambda cwd, rt, head: _fork_scope_comment(
            cwd, rt, head, marker_head="feedbead", rows=False
        ),
    },
]

# Both scope lanes are registered above, and they behave the same way in the state
# that separates a review from a measurement: when the model half leaves no marker
# for this head but the classifier folded rows, each lane publishes those rows and
# stamps them on the CLASSIFIER's authority, with the model's own prose withheld.
# `deny_diff.py` read both refs itself, so the rows describe this revision whatever
# the model produced -- and without the stamp the guarded upsert withholds the whole
# comment, leaving the broken operation and its tier readable only in the job logs.

#: The lanes whose accepted human override note goes through the guarded
#: upsert under the `[<LANE>-OVERRIDE]` needle. The fork UX lane writes it the
#: way ux-review.yml does instead, because its scope skip exits before the
#: upsert is defined, and the scope lane's override note carries the lane's
#: own stamp.
_GUARDED_OVERRIDE_LANES = ("codex", "fork-gpt", "fork-opus", "fork-design", "fork-first-principles")

DESIGN_LANES = ("design-review.yml", "fork-design-review.yml")


def _review_contract_module():
    """Import the REAL marker parser so a drift in its regexes fails these tests.

    Re-declaring the patterns here would let the workflow's neutralization and
    the parser drift apart silently, which is the whole failure mode under test.
    Loaded via ``load_skill_script`` so the import writes no ``__pycache__``
    into the checked-in scripts directory.
    """
    from skill_script_helpers import load_skill_script

    return load_skill_script(
        "_review_contract_under_test",
        ROOT
        / "src"
        / "kiro_crew"
        / "builtin_skills"
        / "kirocrew-dev"
        / "kirocrew-prepare-pr"
        / "scripts"
        / "_review_contract.py",
    )
