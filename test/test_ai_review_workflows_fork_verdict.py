"""Review-workflow tests for how a fork lane captures, publishes and passes its
verdict, and how it honours an accepted human override.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from ai_review_workflows_helpers import (
    _bash,
    _child_env,
    _fork_child_env,
    _github_outputs,
    _proc_log,
    _step_script,
    _workflow,
    _write_stub,
)

#: The two FORK advisory lanes whose verdict capture runs under ``set -uo pipefail``,
#: with the header each one reads. The same-repo ``design-review.yml`` and
#: ``first-principles-review.yml`` are deliberately absent: their post-summary steps
#: carry no ``set -`` line, so an unmatched ``grep``'s status is masked by the
#: pipeline's last command and the step cannot die this way. Adding them here would
#: pin a property they do not have, and would invite a ``pipefail`` to be added to
#: them later without the capture being converted first.
_FORK_VERDICT_LANES = (
    ("fork-design-review.yml", "Design-Verdict:"),
    ("fork-first-principles-review.yml", "First-Principles-Verdict:"),
)


class TestForkVerdictCaptureNeverAbortsAboveItsOwnFallback:
    """A fork advisory lane must live to read the fallback written one line below it.

    Both steps end their capture with ``[ -n "$v" ] && verdict="$v"`` and start it
    with ``verdict="UNKNOWN"``, which is a complete, fail-closed answer for a review
    that names no verdict. Neither line runs if the capture itself takes the step
    down: the lane then writes no verdict output at all, and the comment that would
    have named the cause is never posted.

    Two ordinary model outcomes do exactly that, under the ``bash -e`` a ``run:``
    block with no ``shell:`` key gets plus the ``set -uo pipefail`` these steps add:

    * a review with NO header -- ``grep`` matches nothing and exits 1, and
      ``pipefail`` promotes that status to the pipeline's, so the assignment fails;
    * a review with MANY headers -- ``head -n1`` closes the pipe and its producer
      dies of SIGPIPE (141). This is the worse arm: a value is already in hand, so
      an expression that merely suppressed the status would hand the lane a verdict
      read out of a document the step never finished reading.

    Each step's OWN expression is run, extracted by shape rather than by the program
    it names, so a pin keyed to one tool cannot keep passing after the capture is
    rewritten. Both the status and the value are asserted: asserting the value alone
    passes an expression that returns the right answer and kills the step anyway.

    The two Security Scope lanes read their header with the same single-pass awk,
    for the same reason. The three sites in these files that ALREADY use a
    here-string (each with a comment saying a long summary must not manufacture a
    SIGPIPE status under ``pipefail``) show the rule applies here too: the surviving
    pipeline is the one place in these steps where it is not met.
    """

    #: Headers in the many-headers review. Large enough to fill the pipe buffer and
    #: make ``head -n1``'s exit close it on its producer -- measured to need more
    #: than a few thousand on Git-for-Windows Bash, where a 64 KiB buffer swallows
    #: the smaller body whole -- and still a fixture of a couple of megabytes.
    _MANY_HEADERS = 60_000

    #: Review body kinds. Every one is an ordinary model outcome, not malformed
    #: input. The last three CARRY a verdict, and they are what pins the conversion
    #: as behaviour-preserving: an expression that never aborted and also stopped
    #: reading verdicts would satisfy the status assertion on its own.
    _REVIEW_KINDS = (
        "no-header",
        "header-shaped-prose",
        "many-headers",
        "plain",
        "lowercase",
        "crlf",
    )

    #: The fallback line, which is also how the capture is IDENTIFIED: the capture
    #: is by definition the assignment this line reads, so the variable name comes
    #: from the fallback rather than from a guess. Both steps ALSO trim their
    #: summary to the header with a second assignment mentioning the same header,
    #: and a first-match-wins selector silently measures that one instead.
    _FALLBACK = '[ -n "$v" ] && verdict="$v"'

    def _capture_line(self, workflow: str, header: str) -> str:
        """The verdict ASSIGNMENT in *workflow*, named by what the fallback reads.

        Found by SHAPE, never by the program it runs: a selector keyed to ``grep``
        or to ``awk`` stops finding the capture the day the capture changes tool,
        and then measures nothing while still passing.
        """
        text = _workflow(workflow)
        assert self._FALLBACK in text, f"{workflow}: {self._FALLBACK!r} is gone"
        name = self._FALLBACK.split("-n ", 1)[1].split("]", 1)[0].strip().strip('"$')
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if header.lower() not in stripped.lower():
                continue
            if stripped.startswith(f'{name}="$('):
                return stripped
        raise AssertionError(f"{workflow}: no {header} capture assigned to {name!r}")

    def _body(self, kind: str, header: str) -> tuple[str, str]:
        """The review text for *kind*, and the value the capture must yield for it."""
        if kind == "no-header":
            return "The change refuses nothing new.\n\nNothing to check.\n", ""
        if kind == "header-shaped-prose":
            # The whole header, colon and all, but NOT at the start of a record:
            # the anchor is the only thing that tells it from a real verdict line,
            # so this case fails the moment a rewrite drops the `^`. (Stripping the
            # colon instead would be rejected by the colon, not the anchor, and the
            # case could not catch an unanchored read.)
            return f"I would write {header} PASS as a header if the contract asked.\n", ""
        if kind == "plain":
            return f"{header} CONCERNS\n\nOne thing to watch.\n", "CONCERNS"
        if kind == "lowercase":
            # The capture upper-cases, so every later comparison sees one spelling.
            return f"{header.lower()} block\n", "BLOCK"
        if kind == "crlf":
            # A model emitting CRLF must not leave a carriage return inside the
            # value: that would make each `[ "$verdict" = "BLOCK" ]` test false.
            return f"{header} BLOCK\r\nrest\r\n", "BLOCK"
        body = f"{header} PASS\n" + f"{header} BLOCK\n" * self._MANY_HEADERS
        return body, "PASS"

    @pytest.mark.parametrize(("workflow", "header"), _FORK_VERDICT_LANES)
    def test_the_step_still_carries_the_fallback_this_pin_is_about(
        self, workflow: str, header: str
    ) -> None:
        """If the fallback goes, the test below is measuring something else."""
        text = _workflow(workflow)
        assert 'verdict="UNKNOWN"' in text, (
            f"{workflow}: no UNKNOWN default, so an aborted capture is no longer "
            "distinguishable from a lane that simply read no verdict"
        )
        assert '[ -n "$v" ] && verdict="$v"' in text, (
            f"{workflow}: the capture's own fallback is gone; this pin asserts that "
            "the capture lives long enough to reach it"
        )

    @pytest.mark.parametrize(("workflow", "header"), _FORK_VERDICT_LANES)
    @pytest.mark.parametrize("review_kind", _REVIEW_KINDS)
    def test_every_ordinary_review_reaches_the_fallback_with_the_right_value(
        self, workflow: str, header: str, review_kind: str, tmp_path: Path
    ) -> None:
        bash = _bash()
        if bash is None:
            pytest.skip("the capture is Bash; skip where Bash is absent")
        body, expected = self._body(review_kind, header)
        review = tmp_path / "review.md"
        review.write_text(body, encoding="utf-8")
        line = self._capture_line(workflow, header)
        name = line.split("=", 1)[0]
        # The step's own preamble: `pipefail` is what turns an unmatched grep into
        # the pipeline's status, and `bash -e` below is what the runner supplies for
        # a `run:` block with no `shell:` key. Running without either is what would
        # let this pin pass while the lane died.
        script = "\n".join(
            [
                "set -uo pipefail",
                'summary="$(cat "$IN")"',
                line,
                f'printf %s "${name}"',
            ]
        )
        out = subprocess.run(
            [bash, "-e", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "IN": str(review)},
            cwd=tmp_path,
        )
        assert out.returncode == 0, (
            f"{workflow}: a {review_kind} review aborted the step "
            f"(rc={out.returncode}) under the runner's own `bash -e`, above the "
            f"fallback written for it: {out.stderr.strip()}"
        )
        # Clipped: the many-headers body is megabytes, and a failure message that
        # prints it whole buries the one line saying what went wrong.
        got = out.stdout if len(out.stdout) <= 120 else out.stdout[:120] + "..."
        assert out.stdout == expected, (
            f"{workflow}: a {review_kind} review captured {got!r} "
            f"({len(out.stdout)} chars), expected {expected!r}"
        )


# ---- A fork lane consumes the accepted override record ---------------------
# The Stage-2 fork lanes read the bot-written `/ai-review override` record for
# their own lane at their exact head before any credential or model call. An
# accepted record skips the review and publishes the human decision -- a
# `success` check-run and the override note -- as the same-repo lanes do. Each
# condition fails closed, and a feed the step could not read leaves the lane
# reviewing normally.

_FORK_OVERRIDE_TARGETS = (
    ("fork-opus-review.yml", "fable"),
    ("fork-gpt-review.yml", "gpt"),
    ("fork-design-review.yml", "design"),
    ("fork-ux-review.yml", "ux"),
    ("fork-first-principles-review.yml", "first-principles"),
    ("fork-security-scope-review.yml", "scope"),
)

_OVERRIDE_GATE = "steps.human_override.outputs.active != 'true'"

#: Steps an accepted override skips that are not a credential or claude-code-action
#: step: the GPT lane's own model calls and CLI setup, and every step that fetches
#: the untrusted description or its attachments.
_OVERRIDE_SKIPPED_STEPS = {
    "fork-gpt-review.yml": (
        "Install review CLI",
        "Configure the review CLI for Amazon Bedrock",
        "GPT 6.1 review (discovery pass)",
        "GPT 6.1 review (falsification pass)",
        "Fetch PR intent (stated purpose — data only, for the scope check)",
    ),
    "fork-design-review.yml": (
        "Collect rendered evidence",
        "Capture the PR intent (untrusted data file)",
    ),
    "fork-ux-review.yml": (
        "Collect review evidence (description attachments and committed media)",
        "Capture the PR intent (untrusted data file)",
    ),
    "fork-first-principles-review.yml": ("Fetch PR intent (untrusted data file)",),
}


def _gated_on_the_override(step: dict) -> bool:
    """The override gate is a TOP-LEVEL conjunct of the step's `if:`.

    A substring match would accept `x || <gate>`, which runs the step whenever
    `x` holds, override or not.
    """
    cond = " ".join(str(step.get("if") or "").split())
    if cond == _OVERRIDE_GATE:
        return True
    lead = _OVERRIDE_GATE + " && ("
    if not cond.startswith(lead):
        return False
    # The parenthesis opened after the gate must close at the very end, or a
    # trailing `|| x` would run the step whenever `x` holds.
    depth = 1
    for i, ch in enumerate(cond[len(lead) :], start=len(lead)):
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if depth == 0:
            return i == len(cond) - 1
    return False


class TestForkLaneConsumesTheOverrideRecord:
    HEAD = "1234567890abcdef1234567890abcdef12345678"
    OTHER = "aaaa567890abcdef1234567890abcdef1234aaaa"

    def _record(self, target: str, head: str, login: str = "github-actions[bot]", lead: str = ""):
        return {
            "user": {"login": login},
            "body": (
                f"{lead}<!-- ai-review-human-override target={target} head={head} "
                "actor=alice source=42 -->\n## Human judgment recorded\n\n> a reason"
            ),
        }

    def _run(self, tmp_path: Path, workflow: str, comments: list | None):
        bash = _bash()
        if bash is None or os.name == "nt":
            pytest.skip("the resolve step is Bash with a stubbed gh; POSIX only")
        stub_dir = tmp_path / "stub"
        stub_dir.mkdir()
        attempts = tmp_path / "attempts"
        if comments is None:
            # What gh does on an HTTP error: the API's JSON error body on STDOUT,
            # the message on stderr, a non-zero exit.
            _write_stub(
                stub_dir / "gh",
                f'printf x >> "{attempts}"\n'
                'printf \'{"message":"Resource not accessible by integration","status":"403"}\'\n'
                'echo "gh: Resource not accessible by integration (HTTP 403)" >&2\n'
                "exit 1\n",
            )
        else:
            reply = tmp_path / "comments.json"
            reply.write_text(json.dumps(comments), encoding="utf-8")
            _write_stub(stub_dir / "gh", f'printf x >> "{attempts}"\ncat "{reply}"\n')
        _write_stub(stub_dir / "sleep", "exit 0\n")
        script = tmp_path / "step.sh"
        script.write_text(
            _step_script(_workflow(workflow), "Resolve human override"),
            encoding="utf-8",
            newline="\n",
        )
        out = tmp_path / "github-output"
        result = subprocess.run(
            [bash, "-e", str(script)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=tmp_path,
            env=_fork_child_env(
                tmp_path,
                stub_dir,
                REPO="example/repo",
                PR="7",
                HEAD=self.HEAD,
                GITHUB_OUTPUT=str(out),
            ),
        )
        return result, _github_outputs(out), attempts

    @pytest.mark.parametrize(("workflow", "target"), _FORK_OVERRIDE_TARGETS)
    def test_an_accepted_record_for_this_lane_and_head_is_active(
        self, tmp_path: Path, workflow: str, target: str
    ) -> None:
        result, outputs, _ = self._run(tmp_path, workflow, [self._record(target, self.HEAD)])
        assert result.returncode == 0, _proc_log(result)
        assert outputs == {"active": "true", "actor": "alice", "source": "42"}, _proc_log(result)

    @pytest.mark.parametrize(("workflow", "target"), _FORK_OVERRIDE_TARGETS)
    def test_a_record_for_all_lanes_is_active(self, tmp_path: Path, workflow: str, target: str):
        result, outputs, _ = self._run(tmp_path, workflow, [self._record("all", self.HEAD)])
        assert result.returncode == 0, _proc_log(result)
        assert outputs["active"] == "true", _proc_log(result)

    @pytest.mark.parametrize(
        "case", ["wrong-head", "wrong-lane", "not-the-bot", "not-leading", "short-head"]
    )
    @pytest.mark.parametrize(("workflow", "target"), _FORK_OVERRIDE_TARGETS)
    def test_a_record_that_fails_any_condition_is_ignored(
        self, tmp_path: Path, workflow: str, target: str, case: str
    ) -> None:
        other_lane = "gpt" if target != "gpt" else "design"
        record = {
            "wrong-head": self._record(target, self.OTHER),
            "wrong-lane": self._record(other_lane, self.HEAD),
            "not-the-bot": self._record(target, self.HEAD, login="mallory"),
            "not-leading": self._record(target, self.HEAD, lead="quoting: "),
            # The handler writes the FULL head; a prefix names no exact head.
            "short-head": self._record(target, self.HEAD[:12]),
        }[case]
        result, outputs, _ = self._run(tmp_path, workflow, [record])
        assert result.returncode == 0, _proc_log(result)
        assert outputs == {"active": "false", "actor": "", "source": ""}, _proc_log(result)

    @pytest.mark.parametrize(("workflow", "target"), _FORK_OVERRIDE_TARGETS)
    def test_a_failed_read_falls_back_to_the_model_review(
        self, tmp_path: Path, workflow: str, target: str
    ) -> None:
        # Never an accepted record, and never a failed step: the lane reviews
        # normally, and the JSON error body gh printed on stdout is not read as
        # a comment feed.
        result, outputs, attempts = self._run(tmp_path, workflow, None)
        assert result.returncode == 0, _proc_log(result)
        assert outputs == {"active": "false", "actor": "", "source": ""}, _proc_log(result)
        assert "::warning::" in result.stdout
        assert "Reviewing normally" in result.stdout
        assert attempts.read_text(encoding="utf-8") == "xxx"

    def test_the_resolve_step_is_one_body_across_the_fork_lanes(self) -> None:
        bodies = set()
        for workflow, target in _FORK_OVERRIDE_TARGETS:
            script = _step_script(_workflow(workflow), "Resolve human override")
            # The slice runs up to the next step's `- name:`, so it carries that
            # step's leading comment block; only the step itself is compared.
            script = script[: script.index('\n} >> "$GITHUB_OUTPUT"')]
            needle = f'exact="<!-- ai-review-human-override target={target} head=$HEAD "'
            assert script.count(needle) == 1, workflow
            bodies.add(
                script.replace(needle, 'exact="<!-- ai-review-human-override target=@ head=$HEAD "')
            )
        assert len(bodies) == 1, "the fork lanes' Resolve human override steps drifted apart"

    @pytest.mark.parametrize(("workflow", "target"), _FORK_OVERRIDE_TARGETS)
    def test_no_credential_or_model_step_runs_under_an_accepted_record(
        self, workflow: str, target: str
    ) -> None:
        doc = yaml.safe_load(_workflow(workflow))
        seen = 0
        for job in doc["jobs"].values():
            steps = job["steps"]
            names = [s.get("name") for s in steps]
            named = _OVERRIDE_SKIPPED_STEPS.get(workflow, ())
            for i, step in enumerate(steps):
                uses = str(step.get("uses") or "")
                if (
                    "configure-aws-credentials" not in uses
                    and "claude-code-action" not in uses
                    and step.get("name") not in named
                ):
                    continue
                seen += 1
                # Read in the SAME job, before the step it gates.
                assert "Resolve human override" in names[:i], (workflow, step.get("name"))
                assert _gated_on_the_override(step), (workflow, step.get("name") or uses)
        assert seen >= 2 + len(_OVERRIDE_SKIPPED_STEPS.get(workflow, ())), workflow

    @pytest.mark.parametrize(("workflow", "target"), _FORK_OVERRIDE_TARGETS[:5])
    def test_the_record_is_read_after_the_lanes_row_is_open(
        self, workflow: str, target: str
    ) -> None:
        # The handler posts the record and then reads the lane's row; the lane
        # opens its row and then reads the record. Either order alone leaves a
        # window in which neither sees the other.
        names = [
            s.get("name")
            for s in yaml.safe_load(_workflow(workflow))["jobs"][
                next(iter(yaml.safe_load(_workflow(workflow))["jobs"]))
            ]["steps"]
        ]
        assert names.index("Open check-run (in progress)") < names.index("Resolve human override")

    def test_the_scope_lane_reads_the_record_again_before_it_decides(self) -> None:
        # Its only check-run is the one `publish` posts, so a record posted while
        # `generate`, `validate` or `adjudicate` ran would have no row for the
        # handler to find. `publish` repeats the read just before deciding.
        workflow = _workflow("fork-security-scope-review.yml")
        doc = yaml.safe_load(workflow)
        publish = doc["jobs"]["publish"]["steps"]
        names = [s.get("name") for s in publish]
        i = names.index("Resolve human override")
        assert i < names.index("Decide the lane's conclusion")
        assert publish[i]["if"] == "always()"
        assert publish[i]["env"]["HEAD"] == "${{ needs.generate.outputs.head_sha }}"
        # One read, byte for byte the same in both jobs.
        scripts = {
            s["run"]
            for job in ("generate", "publish")
            for s in doc["jobs"][job]["steps"]
            if s.get("name") == "Resolve human override"
        }
        assert len(scripts) == 1
        either = (
            "${{ needs.generate.outputs.override == 'true' "
            "|| steps.human_override.outputs.active == 'true' }}"
        )
        for step in (
            "Decide the lane's conclusion",
            "Assemble the comment body",
            "Publish check-run",
        ):
            assert (
                next(s for s in publish if s.get("name") == step)["env"]["HUMAN_OVERRIDE"] == either
            )


class TestForkLaneFinalizeHonoursTheOverride:
    """The check-run a fork lane completes is the row readiness reads."""

    HEAD = "1234567890abcdef1234567890abcdef12345678"
    LANES = (
        # workflow, finalize step, check name, how a BLOCK verdict reaches the step
        ("fork-design-review.yml", "Finalize check-run (advisory)", "Design Review", "env"),
        ("fork-ux-review.yml", "Finalize check-run (advisory)", "UX Review", "env"),
        (
            "fork-first-principles-review.yml",
            "Finalize check-run (advisory)",
            "First Principles Review",
            "env",
        ),
        ("fork-opus-review.yml", "Finalize check-run (fail closed)", "Opus 5.5 Review", "OPUS"),
        ("fork-gpt-review.yml", "Finalize check-run (fail closed)", "GPT 6.1 Review", "GPT"),
    )

    def _run(
        self, tmp_path: Path, workflow: str, step: str, how: str, *, override: bool, check_id: str
    ):
        bash = _bash()
        if bash is None or os.name == "nt":
            pytest.skip("the finalize step is Bash with a stubbed gh; POSIX only")
        stub_dir = tmp_path / "stub"
        stub_dir.mkdir()
        calls = tmp_path / "calls"
        calls.mkdir()
        _write_stub(
            stub_dir / "gh",
            'if [ "$1" = "api" ] && [ "$2" = "--method" ] && [ "$3" = "PATCH" ]; then\n'
            f'  printf \'%s\\n\' "$@" >> "{calls}/patch-argv.txt"; exit 0\n'
            "fi\n"
            'if [ "$1" = "api" ] && [ "$2" = "--method" ] && [ "$3" = "POST" ]; then\n'
            f'  printf \'%s\\n\' "$@" >> "{calls}/post-argv.txt"; echo 1; exit 0\n'
            "fi\n"
            "exit 0\n",
        )
        _write_stub(stub_dir / "sleep", "exit 0\n")
        cwd = tmp_path / "workspace"
        cwd.mkdir()
        runner_temp = tmp_path / "runner-temp"
        runner_temp.mkdir()
        env = {
            "REPO": "example/repo",
            "PR": "7",
            "HEAD": self.HEAD,
            "CHECK_ID": check_id,
            "WR_RUN_ID": "900",
            "WR_RUN_ATTEMPT": "2",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_RUN_ID": "5551",
            "RUNNER_TEMP": str(runner_temp),
            "HUMAN_OVERRIDE": "true" if override else "false",
            "OVERRIDE_ACTOR": "alice" if override else "",
            "ADJ_DECISION": "",
            "ADJ_NOTE": "",
            "WITHHELD": "",
            "UNFETCHED": "",
        }
        if how == "env":
            env["VERDICT"] = "BLOCK"
        else:
            out = "claude-review-output.md" if how == "OPUS" else "codex-review-output.md"
            (cwd / out).write_text(
                f"BLOCKING -- src/a.py:1 -- x\n[BLOCK-MERGE] {self.HEAD}\n[{how}-REVIEWED] {self.HEAD}\n",
                encoding="utf-8",
            )
        script = tmp_path / "step.sh"
        script.write_text(_step_script(_workflow(workflow), step), encoding="utf-8", newline="\n")
        result = subprocess.run(
            [bash, "-e", str(script)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=cwd,
            env=_fork_child_env(tmp_path, stub_dir, **env),
        )
        return result, calls

    @pytest.mark.parametrize(("workflow", "step", "name", "how"), LANES)
    def test_an_accepted_override_completes_the_row_success(
        self, tmp_path: Path, workflow: str, step: str, name: str, how: str
    ) -> None:
        # A BLOCK left behind by the skipped review steps does not outvote it.
        result, calls = self._run(tmp_path, workflow, step, how, override=True, check_id="4242")
        assert result.returncode == 0, _proc_log(result)
        argv = (calls / "patch-argv.txt").read_text(encoding="utf-8")
        assert "conclusion=success" in argv and "conclusion=failure" not in argv, argv
        assert f"output[title]={name} — human override accepted" in argv, argv
        assert "@alice" in argv
        assert "<!-- ai-review-fork-lane run=5551 -->" in argv, argv

    @pytest.mark.parametrize(("workflow", "step", "name", "how"), LANES)
    def test_the_fallback_post_carries_the_conclusion_the_verdict_earned(
        self, tmp_path: Path, workflow: str, step: str, name: str, how: str
    ) -> None:
        # No opening row to complete: the fallback POST is the row readiness
        # reads, so a BLOCK must not publish as a neutral pass.
        result, calls = self._run(tmp_path, workflow, step, how, override=False, check_id="")
        assert result.returncode == 0, _proc_log(result)
        argv = (calls / "post-argv.txt").read_text(encoding="utf-8")
        assert "conclusion=failure" in argv, argv
        assert "conclusion=neutral" not in argv and "conclusion=success" not in argv, argv
        assert "external_id=" in argv and "-pr-7-900-2" in argv, argv
        assert "<!-- ai-review-fork-lane run=5551 -->" in argv, argv

    @pytest.mark.parametrize(("workflow", "step", "name", "how"), LANES)
    def test_the_fallback_post_records_an_accepted_override(
        self, tmp_path: Path, workflow: str, step: str, name: str, how: str
    ) -> None:
        result, calls = self._run(tmp_path, workflow, step, how, override=True, check_id="")
        assert result.returncode == 0, _proc_log(result)
        argv = (calls / "post-argv.txt").read_text(encoding="utf-8")
        assert "conclusion=success" in argv, argv
        assert f"output[title]={name} — human override accepted" in argv, argv


class TestForkRequiredLanePassesOnlyAPublishedVerdict:
    """A fork lane's clean verdict is a pass only once it is in the PR's slot.

    The output file is written before publication, so its stamp proves a verdict
    was computed and nothing about whether the PR shows it. The finalize step is
    the row PR Readiness reads, so a pass decided from the file alone presents a
    green required check over a slot that still holds another head's verdict.
    """

    HEAD = "1234567890abcdef1234567890abcdef12345678"
    LANES = (
        ("fork-opus-review.yml", "Opus 5.5 Review", "claude-review-output.md", "OPUS"),
        ("fork-gpt-review.yml", "GPT 6.1 Review", "codex-review-output.md", "GPT"),
    )
    STEP = "Finalize check-run (fail closed)"

    def _run(
        self,
        tmp_path: Path,
        workflow: str,
        out: str,
        stamp: str,
        *,
        publication: str | None,
        check_id: str = "4242",
        block: bool = False,
        adj_decision: str = "",
        override: bool = False,
    ):
        bash = _bash()
        assert bash is not None, "these cases need bash (Git Bash on Windows)"
        calls = tmp_path / "calls"
        calls.mkdir()
        # The stubs are shell FUNCTIONS sourced through BASH_ENV rather than
        # scripts on PATH: Git for Windows' bash prepends its own bin directories
        # to PATH, which would shadow a PATH stub. `jq` is the sweep's check-name
        # encoder only, and its output feeds nothing but the `gh` stub.
        stubs = tmp_path / "stubs.sh"
        stubs.write_text(
            "gh() {\n"
            '  if [ "$1" = "api" ] && [ "$2" = "--method" ] && [ "$3" = "PATCH" ]; then\n'
            '    printf \'%s\\n\' "$@" >> "$STUB_CALLS/patch-argv.txt"; return 0\n'
            "  fi\n"
            '  if [ "$1" = "api" ] && [ "$2" = "--method" ] && [ "$3" = "POST" ]; then\n'
            '    printf \'%s\\n\' "$@" >> "$STUB_CALLS/post-argv.txt"; echo 1; return 0\n'
            "  fi\n"
            "  return 0\n"
            "}\n"
            "sleep() { :; }\n"
            "jq() { :; }\n",
            encoding="utf-8",
            newline="\n",
        )
        cwd = tmp_path / "workspace"
        cwd.mkdir()
        body = f"[{stamp}-REVIEWED] {self.HEAD}\n"
        if block:
            body = f"BLOCKING -- src/a.py:1 -- x\n[BLOCK-MERGE] {self.HEAD}\n" + body
        (cwd / out).write_text(body, encoding="utf-8", newline="\n")
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": tmp_path.as_posix(),
            "TMPDIR": tmp_path.as_posix(),
            "GH_TOKEN": "stub-token",
            "GH_CONFIG_DIR": (tmp_path / "gh-config").as_posix(),
            "BASH_ENV": stubs.as_posix(),
            "STUB_CALLS": calls.as_posix(),
            "REPO": "example/repo",
            "PR": "7",
            "HEAD": self.HEAD,
            "CHECK_ID": check_id,
            "WR_RUN_ID": "900",
            "WR_RUN_ATTEMPT": "2",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_RUN_ID": "5551",
            "RUNNER_TEMP": tmp_path.as_posix(),
            "HUMAN_OVERRIDE": "true" if override else "false",
            "OVERRIDE_ACTOR": "alice" if override else "",
            "ADJ_DECISION": adj_decision,
            "ADJ_NOTE": "",
        }
        if publication is not None:
            env["PUBLICATION"] = publication
        script = tmp_path / "step.sh"
        script.write_text(
            _step_script(_workflow(workflow), self.STEP), encoding="utf-8", newline="\n"
        )
        result = subprocess.run(
            [bash, "-e", str(script)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=cwd,
            env=_child_env(env),
        )
        assert result.returncode == 0, _proc_log(result)
        row = "patch-argv.txt" if check_id else "post-argv.txt"
        return (calls / row).read_text(encoding="utf-8")

    @pytest.mark.parametrize(("workflow", "name", "out", "stamp"), LANES)
    @pytest.mark.parametrize("publication", [None, "", "not_published", "withheld", "override"])
    def test_a_clean_verdict_that_missed_the_slot_fails(
        self, tmp_path: Path, workflow: str, name: str, out: str, stamp: str, publication
    ) -> None:
        # No record at all is the post step failing or never running, and it
        # fails closed exactly like a record that says the write did not land.
        # `override` is not a record the post step writes: an override for
        # this head reaches the finalize step through its own input instead.
        argv = self._run(tmp_path, workflow, out, stamp, publication=publication)
        assert "conclusion=failure" in argv and "conclusion=success" not in argv, argv
        assert f"output[title]={name} — verdict not published" in argv, argv
        assert "Re-run this lane to publish it." in argv, argv

    @pytest.mark.parametrize(("workflow", "name", "out", "stamp"), LANES)
    @pytest.mark.parametrize("publication", ["published", "superseded"])
    def test_a_published_verdict_keeps_its_own_conclusion(
        self, tmp_path: Path, workflow: str, name: str, out: str, stamp: str, publication: str
    ) -> None:
        argv = self._run(tmp_path, workflow, out, stamp, publication=publication)
        assert "conclusion=success" in argv and "conclusion=failure" not in argv, argv
        assert f"output[title]={name} — no blocking findings" in argv, argv

    @pytest.mark.parametrize(("workflow", "name", "out", "stamp"), LANES)
    def test_the_fallback_row_carries_the_same_failure(
        self, tmp_path: Path, workflow: str, name: str, out: str, stamp: str
    ) -> None:
        # With no opening row to complete, the fallback POST is what readiness
        # reads, so it must not re-derive a pass from the file either.
        argv = self._run(tmp_path, workflow, out, stamp, publication="not_published", check_id="")
        assert "conclusion=failure" in argv and "conclusion=success" not in argv, argv
        assert f"output[title]={name} — verdict not published" in argv, argv

    @pytest.mark.parametrize(("workflow", "name", "out", "stamp"), LANES)
    def test_an_unpublished_blocking_verdict_still_reads_as_blocking(
        self, tmp_path: Path, workflow: str, name: str, out: str, stamp: str
    ) -> None:
        argv = self._run(tmp_path, workflow, out, stamp, publication="not_published", block=True)
        assert "conclusion=failure" in argv, argv
        assert f"output[title]={name} — changes requested (blocking)" in argv, argv

    @pytest.mark.parametrize(("workflow", "name", "out", "stamp"), LANES)
    @pytest.mark.parametrize("publication", [None, "not_published"])
    def test_an_accepted_override_for_this_head_still_passes(
        self, tmp_path: Path, workflow: str, name: str, out: str, stamp: str, publication
    ) -> None:
        # The override is decided AFTER the publication check, from its own
        # input, so a skipped review with no publication record and a BLOCK
        # left in the output file still completes the row success.
        argv = self._run(
            tmp_path, workflow, out, stamp, publication=publication, block=True, override=True
        )
        assert "conclusion=success" in argv and "conclusion=failure" not in argv, argv
        assert f"output[title]={name} — human override accepted" in argv, argv

    def test_an_adjudicated_clearance_that_missed_the_slot_fails(self, tmp_path: Path) -> None:
        # The GPT lane's second way to a pass, a fully reconciled clearance,
        # is a pass on the same terms as a clean verdict.
        argv = self._run(
            tmp_path,
            "fork-gpt-review.yml",
            "codex-review-output.md",
            "GPT",
            publication="not_published",
            block=True,
            adj_decision="cleared",
        )
        assert "conclusion=failure" in argv and "conclusion=success" not in argv, argv
        assert "output[title]=GPT 6.1 Review — verdict not published" in argv, argv

    @pytest.mark.parametrize(("workflow", "name", "out", "stamp"), LANES)
    def test_the_finalize_step_reads_the_post_steps_record(
        self, workflow: str, name: str, out: str, stamp: str
    ) -> None:
        doc = yaml.safe_load(_workflow(workflow))
        steps = [s for job in doc["jobs"].values() for s in job.get("steps", [])]
        post = [s for s in steps if s.get("id") == "post"]
        assert len(post) == 1 and "guarded_comment_upsert" in post[0]["run"]
        final = next(s for s in steps if s.get("name") == self.STEP)
        assert final["env"]["PUBLICATION"] == "${{ steps.post.outputs.publication }}"


class TestForkGptRefusalTerminalState:
    """A reviewer that CRASHED and one that REFUSED both arrive as ``rc != 0``
    or an empty pass file, but they mean opposite things: a crash is fixed by
    re-running, while a provider refusal is caused by what the diff IS and is
    empirically sticky across re-runs.  The fork GPT lane now classifies a
    failed pass against the provider's own refusal line -- anchored at line
    start and only in the tail of the captured stream -- and publishes a
    distinct terminal state that still fails the gate closed (a declined review
    is not an approval) but names human adjudication instead of prescribing a
    re-run.  The classification travels as a step output; nothing downstream
    greps it out of model prose.

    The fork lane differs from the same-repo lane in one surface detail:
    the refused body says "fork PR's diff" rather than "this diff", to name
    the provenance clearly for a maintainer looking at an external contributor's
    work. The remedy is identical: ``/ai-review override gpt $HEAD``.
    """

    HEAD = "0123456789abcdef0123456789abcdef01234567"
    WORKFLOW = "fork-gpt-review.yml"

    @staticmethod
    def _pass_step(name: str) -> dict:
        doc = yaml.safe_load(_workflow("fork-gpt-review.yml"))
        for step in [s for job in doc["jobs"].values() for s in job.get("steps", [])]:
            if step.get("name") == name:
                return step
        raise AssertionError(f"step not found: {name}")

    def test_refusal_is_classified_where_rc_is_captured(self) -> None:
        workflow = _workflow("fork-gpt-review.yml")
        discovery_step = workflow[
            workflow.index("- name: GPT 6.1 review (discovery pass)") : workflow.index(
                "- name: Refresh the signer's credentials (keep them behind the UID boundary)"
            )
        ]
        review_step = workflow[
            workflow.index("- name: GPT 6.1 review (falsification pass)") : workflow.index(
                "- name: Stop the loopback signing proxy"
            )
        ]
        for step, n in ((discovery_step, 1), (review_step, 2)):
            # The signature can only be matched against output that was
            # captured; the CLI's combined stream is tee'd into RUNNER_TEMP
            # (never the checkout, so a PR cannot plant a symlink at the
            # log's name). The match is line-anchored and tail-scoped because
            # the fork lane hands the model more attacker-controlled text
            # than the same-repo lane -- the prompt embeds the fork author's
            # title/body, and the reviewer echoes the diff, which for a PR
            # touching the workflow itself contains the signature verbatim.
            assert f'tee "$RUNNER_TEMP/codex-pass-{n}-log.txt"' in step
            assert "REFUSAL_SIGNATURE:" in step
            assert (
                f'tail -c 4000 "$RUNNER_TEMP/codex-pass-{n}-log.txt" | grep -q "^$REFUSAL_SIGNATURE"'
                in step
            )
            assert f"printf ' {n}' >> \"$RUNNER_TEMP/codex-refused-passes\"" in step
        # A stale refusal record from an earlier run of the same job must not
        # classify a fresh failure, so the record is re-initialized with the
        # refused-passes file.
        assert 'rm -f "$RUNNER_TEMP/codex-refused-passes"' in discovery_step
        # The signature is interpolated into an anchored grep pattern, so it
        # must carry the provider's line-leading prefix and stay free of
        # basic-regex metacharacters.

        signature = self._pass_step("GPT 6.1 review (discovery pass)")["env"]["REFUSAL_SIGNATURE"]
        assert signature.startswith("ERROR: ")
        assert re.search(r"[.*\[\]^$\\]", signature) is None

    def test_signature_stays_in_sync_with_the_same_repo_lane(self) -> None:
        # Both lanes classify against the same provider error line.  Drift
        # would silently split refusal semantics: a diff refused on one lane
        # but classified as crashed on the other would get contradictory
        # guidance depending on which lane the maintainer is reading.
        fork_sig = self._pass_step("GPT 6.1 review (discovery pass)")["env"]["REFUSAL_SIGNATURE"]
        same_repo_doc = yaml.safe_load(_workflow("codex-review.yml"))
        same_repo_steps = list(same_repo_doc["jobs"].values())[0]["steps"]
        same_repo_step = next(
            s for s in same_repo_steps if s.get("name") == "GPT 6.1 review (discovery pass)"
        )
        assert fork_sig == same_repo_step["env"]["REFUSAL_SIGNATURE"]

    def _classify(self, tmp_path: Path, log: str) -> str:
        """Run the discovery pass's failure-classification block against a
        fabricated captured stream and return the refused-passes record."""
        bash = _bash()
        assert bash is not None, "classification requires Bash"
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
        # The stream carries the diff the reviewer echoed. A fork PR touching
        # this workflow contains the signature verbatim -- as an indented diff
        # line, never line-leading -- and a crash on such a PR must stay a
        # crash, not reclassify as refused.
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
        assert bash is not None, "verdict assembly requires Bash"
        script = _step_script(
            _workflow("fork-gpt-review.yml"), "GPT 6.1 review (falsification pass)"
        )
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
        # carries NO machine marker.
        assert "[GPT-REVIEWED]" not in out
        assert "[GPT-REFUSED]" not in out
        assert "Re-run the workflow" not in out
        # The fork lane does resolve /ai-review override gpt $HEAD records, so
        # the refused copy names the same remedy as the same-repo lane.
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

    def test_refused_run_never_modifies_an_existing_comment(self) -> None:
        # A refused run produces a body with no [GPT-REVIEWED] stamp. The
        # shared guarded_comment_upsert path -- called with [GPT-REVIEWED] as
        # the proof marker -- never PATCHes a comment whose new body lacks the
        # stamp, so an existing completed verdict is left entirely alone.
        workflow = _workflow("fork-gpt-review.yml")
        comment_step = workflow[
            workflow.index("- name: Post/update summary comment") : workflow.index(
                "- name: Retain this run's unpublished verdict"
            )
        ]
        # The refused body rendering has no [GPT-REVIEWED] marker in it,
        # so the upsert guard treats it like an incomplete run: only creates
        # when the slot is empty, never patches.
        assert 'guarded_comment_upsert "$MARKER" "[GPT-REVIEWED]"' in comment_step
        # The refused branch is only triggered by the step output, never a
        # body grep, so a verdict that merely quotes the refusal wording is
        # not reclassified.
        assert "REFUSED: ${{ steps.gpt_pass2.outputs.refused }}" in comment_step
        assert 'kind="refused"' in comment_step
        assert "GPT-REFUSED" not in comment_step
        # The comment step shows the refused body by cat'ing codex-review-output.md
        # (assembled by the falsification pass), so the static script does not
        # contain the remedy text directly.
        # test_refused_pass_publishes_its_own_terminal_state verifies the remedy
        # text (/ai-review override gpt $HEAD) lands in codex-review-output.md.

    def test_refused_classification_rides_the_step_output_into_the_comment(self) -> None:
        # The comment step reads the assembly's step output, never a grep of
        # codex-review-output.md (which on the clean path is model prose), so a
        # verdict that merely quotes the refusal wording is not reclassified.
        workflow = _workflow("fork-gpt-review.yml")
        comment_step = workflow[
            workflow.index("- name: Post/update summary comment") : workflow.index(
                "- name: Retain this run's unpublished verdict"
            )
        ]
        assert "REFUSED: ${{ steps.gpt_pass2.outputs.refused }}" in comment_step
        assert (
            'elif [ "${REFUSED:-}" = "true" ]' in comment_step
            or 'if [ "${REFUSED:-}" = "true" ]' in comment_step
        )
