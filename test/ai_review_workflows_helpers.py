"""Shared helpers for the ``test_ai_review_workflows*`` tests.

Workflow and prompt readers, step extraction, the bash runner and its stubs, and the
lane tables the parts read.
"""

from __future__ import annotations

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
