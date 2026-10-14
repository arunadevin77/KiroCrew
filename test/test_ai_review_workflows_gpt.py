"""Review-workflow tests for the GPT lanes: how a verdict, or a refusal, reaches the
PR, the downgrade fence table, and the fork lane's repo context.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml
from ai_review_workflows_helpers import (
    REVIEW_PROMPTS,
    ROOT,
    WORKFLOWS,
    _bash,
    _step_script,
    _workflow,
)


class TestGptVerdictVisibility:
    """The same-repo GPT lane publishes through the shared guard.

    An inline lookup, verdict guard or notice strip in this step is a second
    spelling of the visibility invariant: a position-blind ``sed`` range strip
    deletes lines of a standing verdict whenever a bare quoted begin marker
    sits inside preserved review prose. The behavioural contract -- an
    incomplete run never buries a posted verdict, a completed one replaces it,
    a superseded one leaves it alone -- is the shared ``guarded_comment_upsert``
    and is exercised for this lane by ``TestReviewLaneVerdictVisibility``. What
    stays lane-specific is pinned here: the stamp each kind publishes under, and
    the gate that stays outside the convergence.
    """

    def test_the_post_step_routes_through_the_shared_guard(self) -> None:
        workflow = _workflow("codex-review.yml")
        script = _step_script(workflow, "Post/update review comment")
        # The function is defined in this step and pinned byte-identical to
        # every other lane's copy by the identity test. Both publication kinds
        # name their own head-scoped stamp: a review carries [GPT-REVIEWED] and
        # an accepted override [GPT-OVERRIDE], so a standing block for this
        # same head can never read as the override's own write.
        assert "guarded_comment_upsert() {" in script
        assert (
            'guarded_comment_upsert "$MARKER" "[GPT-REVIEWED]" "GPT 6.1 Review"'
            ' "${RUNNER_TEMP:-/tmp}/codex-comment.md"'
        ) in script
        assert (
            'guarded_comment_upsert "$MARKER" "[GPT-OVERRIDE]" "GPT 6.1 Review"'
            ' "${RUNNER_TEMP:-/tmp}/codex-comment.md"'
        ) in script
        assert 'echo "[GPT-OVERRIDE] $HEAD"' in script
        # The override arm is selected by `kind`, a shell variable this step
        # assigns from the review's outcome, and the selection comes AFTER that
        # assignment: a call placed above it would test an empty kind and route
        # the one body that must replace a standing block through the review
        # stamp no override body carries.
        assert script.index('kind="override"') < script.index(
            'guarded_comment_upsert "$MARKER" "[GPT-OVERRIDE]"'
        )
        # The inline shape is gone with the defect it carried: no body read, no
        # merged body, no notice strip.
        assert "stale-notice" not in script
        assert "codex-existing-comment.md" not in script
        assert "codex-merged-comment.md" not in script
        assert "head_needle" not in script
        assert "| {id, body} | @json" not in script

    def test_the_gate_is_untouched(self) -> None:
        # The convergence changes the posting tail only. The gate's fail-closed
        # contract is byte-identical to before: it reads the captured review
        # text for this head's stamp and fails without it.
        gate = _step_script(_workflow("codex-review.yml"), "Gate on findings")
        assert 'if ! grep -Fq "$reviewed" codex-review-output.md; then' in gate
        assert "Failing closed" in gate
        assert "stale-notice" not in gate
        assert "publication" not in gate


class TestGptRefusalTerminalState:
    """A reviewer that CRASHED and one that REFUSED both arrive as ``rc != 0``
    or an empty pass file, but they mean opposite things: a crash is fixed by
    re-running, while a provider refusal is caused by what the diff IS and is
    empirically sticky across re-runs. The lane classifies a failed pass
    against the provider's own refusal line — anchored at line start and only
    in the tail of the captured stream, because the stream also carries
    PR-controlled text — and publishes a distinct terminal state that still
    fails the gate closed (a declined review is not an approval) but names
    human adjudication instead of prescribing a re-run. The classification
    travels as a step output; nothing downstream greps it out of model prose.
    """

    HEAD = "0123456789abcdef0123456789abcdef01234567"

    @staticmethod
    def _pass_step(name: str) -> dict:
        doc = yaml.safe_load(_workflow("codex-review.yml"))
        for step in list(doc["jobs"].values())[0]["steps"]:
            if step.get("name") == name:
                return step
        raise AssertionError(f"step not found: {name}")

    def test_refusal_is_classified_where_rc_is_captured(self) -> None:
        workflow = _workflow("codex-review.yml")
        discovery_step = workflow[
            workflow.index("- name: GPT 6.1 review (discovery pass)") : workflow.index(
                "- name: GPT 6.1 review (falsification pass)"
            )
        ]
        review_step = workflow[
            workflow.index("- name: GPT 6.1 review (falsification pass)") : workflow.index(
                "- name: Redact credential shapes from review output"
            )
        ]
        for step, n in ((discovery_step, 1), (review_step, 2)):
            # The signature can only be matched against output that was
            # captured; the CLI's combined stream is tee'd where rc is
            # captured, into RUNNER_TEMP so a PR cannot plant a symlink at
            # the log's name. The match is line-anchored and tail-scoped
            # because the stream also carries PR-controlled text (the prompt
            # embeds the PR title/body, and the reviewer echoes the diff —
            # which, for a PR touching the workflow itself, contains the
            # signature verbatim): an unanchored whole-stream match would let
            # an echoed copy reclassify an ordinary crash as a refusal.
            assert f'tee "$RUNNER_TEMP/codex-pass-{n}-log.txt"' in step
            assert "REFUSAL_SIGNATURE:" in step
            assert (
                f'tail -c 4000 "$RUNNER_TEMP/codex-pass-{n}-log.txt" | grep -q "^$REFUSAL_SIGNATURE"'
                in step
            )
            assert f"printf ' {n}' >> \"$RUNNER_TEMP/codex-refused-passes\"" in step
        # A stale refusal record from an earlier run of the same job must not
        # classify a fresh failure, so the record is re-initialized with the
        # crash record.
        assert 'rm -f "$RUNNER_TEMP/codex-refused-passes"' in discovery_step
        # The signature is interpolated into an anchored grep pattern, so it
        # must carry the provider's line-leading prefix and stay free of
        # basic-regex metacharacters.
        signature = self._pass_step("GPT 6.1 review (discovery pass)")["env"]["REFUSAL_SIGNATURE"]
        assert signature.startswith("ERROR: ")
        assert re.search(r"[.*\[\]^$\\]", signature) is None

    def _classify(self, tmp_path: Path, log: str) -> str:
        """Run the discovery pass's failure-classification block against a
        fabricated captured stream and return the refused-passes record."""
        bash = _bash()
        if bash is None:
            pytest.skip("classification requires Bash")
        step = self._pass_step("GPT 6.1 review (discovery pass)")
        script = step["run"]
        snippet = script[script.index('if [ "$rc" -ne 0 ]') :]
        runner_temp = tmp_path / "rt"
        runner_temp.mkdir()
        (runner_temp / "codex-pass-1-log.txt").write_text(log, encoding="utf-8")
        result = subprocess.run(
            [bash, "-c", f"set -uo pipefail\nrc=124\n{snippet}"],
            check=False,
            capture_output=True,
            encoding="utf-8",
            cwd=tmp_path,
            env={
                **os.environ,
                "RUNNER_TEMP": str(runner_temp),
                "REFUSAL_SIGNATURE": step["env"]["REFUSAL_SIGNATURE"],
            },
        )
        assert result.returncode == 0, result.stderr
        record = runner_temp / "codex-refused-passes"
        return record.read_text(encoding="utf-8") if record.exists() else ""

    def test_provider_emitted_refusal_line_classifies_as_refused(self, tmp_path: Path) -> None:
        signature = self._pass_step("GPT 6.1 review (discovery pass)")["env"]["REFUSAL_SIGNATURE"]
        log = f"some progress output\n{signature}.\nLearn more here: https://example.invalid\n"
        assert self._classify(tmp_path, log) == " 1"

    def test_echoed_signature_in_pr_controlled_text_stays_a_crash(self, tmp_path: Path) -> None:
        # The stream carries the diff the reviewer echoed. A PR touching this
        # workflow contains the signature verbatim — as an indented diff line,
        # never line-leading — and a crash on such a PR must stay a crash:
        # mislabeling it as refused points the operator at /ai-review override
        # when the re-run it forecloses would have worked.
        signature = self._pass_step("GPT 6.1 review (discovery pass)")["env"]["REFUSAL_SIGNATURE"]
        log = f'+          REFUSAL_SIGNATURE: "{signature}"\n> quoted: {signature}\n'
        assert self._classify(tmp_path, log) == ""

    def test_signature_outside_the_stream_tail_stays_a_crash(self, tmp_path: Path) -> None:
        # The provider emits the refusal as the stream's final act. A copy of
        # the line early in a long stream (echoed content scrolled past) must
        # not classify a later, unrelated crash.
        signature = self._pass_step("GPT 6.1 review (discovery pass)")["env"]["REFUSAL_SIGNATURE"]
        log = f"{signature}.\n" + ("x" * 80 + "\n") * 100
        assert self._classify(tmp_path, log) == ""

    def _assemble_verdict(
        self,
        tmp_path: Path,
        *,
        failed_passes: str,
        refused_passes: str | None,
        pass2: str | None,
    ) -> tuple[str, str]:
        bash = _bash()
        if bash is None:
            pytest.skip("verdict assembly requires Bash")
        script = _step_script(_workflow("codex-review.yml"), "GPT 6.1 review (falsification pass)")
        snippet = script[
            script.index("refused_passes=") : script.index("# Gate the adjudication pass below")
        ]
        runner_temp = tmp_path / "rt"
        runner_temp.mkdir()
        gh_output = tmp_path / "gh-output"
        gh_output.write_text("", encoding="utf-8")
        if refused_passes is not None:
            (runner_temp / "codex-refused-passes").write_text(refused_passes, encoding="utf-8")
        if pass2 is not None:
            (tmp_path / "codex-pass-2.md").write_text(pass2, encoding="utf-8")
        harness = f"set -uo pipefail\nfailed_passes='{failed_passes}'\n{snippet}\ncat codex-review-output.md\n"
        result = subprocess.run(
            [bash, "-c", harness],
            check=False,
            capture_output=True,
            encoding="utf-8",
            cwd=tmp_path,
            env={
                **os.environ,
                "HEAD": self.HEAD,
                "RUNNER_TEMP": str(runner_temp),
                "GITHUB_OUTPUT": str(gh_output),
            },
        )
        assert result.returncode == 0, result.stderr
        return result.stdout, gh_output.read_text(encoding="utf-8")

    def test_refused_pass_publishes_its_own_terminal_state(self, tmp_path: Path) -> None:
        out, gh_output = self._assemble_verdict(
            tmp_path, failed_passes=" 2", refused_passes=" 2", pass2=None
        )
        assert "Reviewer refused" in out
        # The refusal must not read as a completed review, must not prescribe
        # the re-run that has never been observed to work, and deliberately
        # carries NO machine marker: classification rides the step output, and
        # a marker in the body would invite the prose-grepping that the
        # output exists to prevent.
        assert "[GPT-REVIEWED]" not in out
        assert "[GPT-REFUSED]" not in out
        assert "Re-run the workflow" not in out
        assert "/ai-review override gpt" in out
        # Downstream classification rides this output, never a body grep.
        assert "refused=true" in gh_output

    def test_crashed_pass_keeps_the_rerunnable_incomplete_state(self, tmp_path: Path) -> None:
        out, gh_output = self._assemble_verdict(
            tmp_path, failed_passes=" 1", refused_passes=None, pass2=None
        )
        assert "Incomplete review" in out
        assert "Re-run the workflow" in out
        assert "[GPT-REFUSED]" not in out
        assert "refused=false" in gh_output

    def test_refusal_dominates_a_mixed_failure(self, tmp_path: Path) -> None:
        # Pass 1 crashed AND pass 2 was refused: the adjudication route is the
        # reliable exit either way, while a re-run only helps if the refusal
        # does not recur, so the refusal is what the operator is told about.
        out, gh_output = self._assemble_verdict(
            tmp_path, failed_passes=" 1 2", refused_passes=" 2", pass2=None
        )
        assert "Reviewer refused" in out
        assert "Re-run the workflow" not in out
        assert "refused=true" in gh_output

    def test_clean_run_still_publishes_pass_2_verbatim(self, tmp_path: Path) -> None:
        verdict = f"all good\n[GPT-REVIEWED] {self.HEAD}\n"
        out, gh_output = self._assemble_verdict(
            tmp_path, failed_passes="", refused_passes=None, pass2=verdict
        )
        assert f"[GPT-REVIEWED] {self.HEAD}" in out
        assert "[GPT-REFUSED]" not in out
        assert "refused=false" in gh_output

    def _run_gate(
        self, tmp_path: Path, review_output: str, *, refused: str = ""
    ) -> subprocess.CompletedProcess[str]:
        bash = _bash()
        if bash is None:
            pytest.skip("gate script requires Bash")
        script = _step_script(_workflow("codex-review.yml"), "Gate on findings")
        (tmp_path / "codex-review-output.md").write_text(review_output, encoding="utf-8")
        return subprocess.run(
            [bash, "-c", f"set -e\n{script}"],
            check=False,
            capture_output=True,
            encoding="utf-8",
            cwd=tmp_path,
            env={
                **os.environ,
                "HEAD": self.HEAD,
                "ACTOR": "someone",
                "HUMAN_OVERRIDE": "false",
                "REFUSED": refused,
                "ADJ_DECISION": "",
                "ADJ_NOTE": "",
            },
        )

    def test_gate_fails_closed_on_refusal_and_names_adjudication_not_rerun(
        self, tmp_path: Path
    ) -> None:
        proc = self._run_gate(
            tmp_path,
            "> **Reviewer refused:** the provider declined.\n",
            refused="true",
        )
        assert proc.returncode == 1
        assert "REFUSED" in proc.stdout
        assert "/ai-review override gpt" in proc.stdout
        # The crash branch's advice must not fire for a refusal: prescribing a
        # re-run for a content-caused refusal is the defect this state exists
        # to remove.
        assert "re-run the workflow or inspect" not in proc.stdout

    def test_completed_verdict_quoting_the_marker_is_not_reclassified(self, tmp_path: Path) -> None:
        # On the clean path codex-review-output.md is verbatim model prose. A
        # review that QUOTES the refusal marker — say, while reviewing this
        # workflow — must not flip a completed verdict into a refusal: the
        # gate reads the assembly's step output, never the body.
        quoted = (
            "quoting the refusal state: '> **Reviewer refused:** the provider "
            "declined to review this diff' and the phrase ERROR: This request "
            "has been flagged for potentially high-risk cyber activity\n"
            f"[GPT-REVIEWED] {self.HEAD}\n"
        )
        proc = self._run_gate(tmp_path, quoted, refused="")
        assert proc.returncode == 0

    def test_gate_without_refusal_keeps_the_rerunnable_crash_advice(self, tmp_path: Path) -> None:
        proc = self._run_gate(tmp_path, "> **Incomplete review:** pass(es) 1 did not complete.\n")
        assert proc.returncode == 1
        assert "did not complete" in proc.stdout

    def test_gate_still_passes_and_blocks_exactly_as_before(self, tmp_path: Path) -> None:
        clean = self._run_gate(tmp_path, f"fine\n[GPT-REVIEWED] {self.HEAD}\n")
        assert clean.returncode == 0
        blocked = self._run_gate(
            tmp_path, f"bad\n[GPT-REVIEWED] {self.HEAD}\n[BLOCK-MERGE] {self.HEAD}\n"
        )
        assert blocked.returncode == 1

    def test_comment_classifies_refusal_and_preserves_prior_verdicts(self) -> None:
        workflow = _workflow("codex-review.yml")
        comment_step = workflow[
            workflow.index("- name: Post/update review comment") : workflow.index(
                "- name: Gate on findings"
            )
        ]
        # Its own kind, read from the assembly's step output — never a grep of
        # the model-authored body — so a maintainer sees "refused" from the
        # lane itself rather than a generic "incomplete, re-run it", and a
        # verdict that merely quotes the marker is not reclassified.
        assert "REFUSED: ${{ steps.gpt_pass2.outputs.refused }}" in comment_step
        assert 'elif [ "$REFUSED" = "true" ]; then' in comment_step
        assert 'kind="refused"' in comment_step
        assert "GPT-REFUSED" not in comment_step
        # Visibility invariant: a refusal has no verdict, so like an
        # incomplete run it must never bury an existing [GPT-REVIEWED] body.
        # Both kinds reach the shared guard with a body carrying no
        # `[GPT-REVIEWED] $HEAD` stamp, which is exactly the arm that leaves
        # an existing comment untouched (TestReviewLaneVerdictVisibility runs
        # it for this lane); the refusal's own wording is what the comment
        # says when the slot is empty, and it must not advise the re-run the
        # incomplete notice advises.
        assert 'guarded_comment_upsert "$MARKER" "[GPT-REVIEWED]"' in comment_step
        assert "a re-run is very unlikely to produce a verdict" in comment_step
        assert "re-runs have not been observed to clear this class of refusal" in comment_step


class TestGptDowngradeFenceTable:
    """The codex-review.yml comment calls its fence table complete. Hold it to
    that: every AUTOSDE rule id is either matched by the Anchor fence and named
    as fenced, or not matched and named as NOT fenced."""

    def test_every_rule_id_is_classified_as_the_fence_classifies_it(self) -> None:
        workflow = _workflow("codex-review.yml")
        match = re.search(r"SECURITY_RE: '(.*)'\n", workflow)
        assert match, "SECURITY_RE moved"
        anchor_re = re.compile(
            match.group(1) + r"|\bsecurity\b|residual/|harness-parity|no-test-side-effects",
            re.IGNORECASE,
        )
        start = workflow.index("#   - NOT fenced (downgrade adjudication is the intended path):")
        end = workflow.index("#   - a NEW AUTOSDE rule id defaults to NOT fenced", start)
        table = workflow[start:end]
        split = table.index("#   - fenced by the vocabulary above, not by name:")

        # Re-join ids the comment hyphen-wrapped across lines.
        def ids_in(text: str) -> str:
            text = re.sub(r"-\n\s*#\s*", "-", text)
            return " ".join(text.replace("#", " ").split())

        not_fenced, by_vocab = ids_in(table[:split]), ids_in(table[split:])
        root = yaml.safe_load((ROOT / "AUTOSDE.yaml").read_text(encoding="utf-8"))
        for rule in root["custom-rules"]:
            rid = rule["id"]
            fenced = anchor_re.search(f"Anchor: {rid}") is not None
            if rid in ("harness-parity", "no-test-side-effects"):
                assert fenced, rid  # fenced by name, documented above the table
            elif fenced:
                assert rid in by_vocab, f"{rid} is fenced but not listed as fenced"
            else:
                assert re.search(
                    rf"(?<![\w-]){re.escape(rid)}(?![\w-])", not_fenced
                ), f"{rid} is not fenced but missing from the NOT fenced list"
        website = yaml.safe_load((ROOT / "website" / "AUTOSDE.yaml").read_text(encoding="utf-8"))
        for rule in website["custom-rules"]:
            if anchor_re.search(f"Anchor: {rule['id']}"):
                assert rule["id"] in by_vocab, rule["id"]


def _fork_parity_flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _fork_parity_steps(workflow: str) -> list[dict]:
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    return [step for job in doc["jobs"].values() for step in job.get("steps", [])]


def _fork_parity_paragraphs(text: str) -> list[str]:
    return [_fork_parity_flat(p) for p in re.split(r"\n\s*\n", text) if p.strip()]


class TestForkGptRepoContextParity:
    """fork-gpt-review.yml inlines the REPO CONTEXT that codex-review.yml
    splices from gpt-repo-context.md. Every shared paragraph must match; the
    paragraphs below are the declared lane-specific deviations."""

    # gpt-repo-context.md paragraphs the fork lane deliberately words differently
    # (keyed by their opening words).
    SAME_REPO_ONLY = (
        "Follow the conventions in CLAUDE.md",  # fork adds the router sentence
        "Start from the changes this branch introduces",  # fork reads a patch file
        "══════",  # DIVISION OF LABOUR: no CodeQL on forks
        "NEVER report anything those tools own",  # fork: "the tools that DO run"
        "PRECEDENCE",  # fork lane has no adjudication ledger (no step 0)
    )
    FORK_ONLY = (
        "Follow the conventions in CLAUDE.md",
        "The changes under review are the GitHub-authentic",
        "══════",
        "NOTE — FORK LANE: CodeQL does NOT run",
        "NEVER report anything the tools that DO run own",
        "PRECEDENCE",
    )

    def _fork_block(self) -> str:
        run = next(
            step["run"]
            for step in _fork_parity_steps("fork-gpt-review.yml")
            if "REPO CONTEXT:" in (step.get("run") or "")
        )
        start = run.index("REPO CONTEXT:")
        return textwrap.dedent(run[start : run.index("\nEOF", start)])

    def _shared_block(self) -> str:
        text = (REVIEW_PROMPTS / "gpt-repo-context.md").read_text(encoding="utf-8")
        return text[text.index("REPO CONTEXT:") :]

    def test_every_shared_paragraph_is_carried_verbatim(self) -> None:
        fork = _fork_parity_paragraphs(self._fork_block())
        shared = _fork_parity_paragraphs(self._shared_block())
        missing = [
            p[:80] for p in shared if p not in fork and not p.startswith(self.SAME_REPO_ONLY)
        ]
        assert missing == [], f"fork-gpt-review.yml REPO CONTEXT drifted: {missing}"

    def test_fork_adds_only_declared_fork_parity_paragraphs(self) -> None:
        fork = _fork_parity_paragraphs(self._fork_block())
        shared = _fork_parity_paragraphs(self._shared_block())
        extra = [p[:80] for p in fork if p not in shared and not p.startswith(self.FORK_ONLY)]
        assert extra == [], f"undeclared fork-only REPO CONTEXT paragraphs: {extra}"

    def test_sandbox_scope_rule_reaches_the_fork_lane(self) -> None:
        block = _fork_parity_flat(self._fork_block())
        assert "THE SHELL COMMAND GATE IS NOT THE ONLY CONTROL" in block
        assert "the sandbox's SCOPE is not yours to widen either" in block

    def test_test_matrix_is_named_not_counted(self) -> None:
        block = _fork_parity_flat(self._fork_block())
        assert "12 pytest shards" not in block
        assert "3.10" not in block
        assert "(`backend-test` and `backend-test-windows` in ci.yml)" in block
        ci = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
        assert {"backend-test", "backend-test-windows"} <= set(ci["jobs"])
