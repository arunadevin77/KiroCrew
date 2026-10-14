"""Review-workflow tests for how the review lanes write their marker comments: a notice
comment is created only from a slot lookup that answered.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from ai_review_workflows_helpers import (
    _bash,
    _shell_function,
    _step_script,
    _workflow,
)

#: Every review lane's notice step, with the slot-lookup shape it is allowed to
#: carry. ``defines_lookup`` says the step declares ``find_existing``; ``creates``
#: says at least one branch in it CREATES a marker comment rather than only
#: patching one that already exists. A create is the case that cannot be undone,
#: so it is the case the gate exists for -- but the lookup is budgeted in both,
#: because a swallowed read on a patch-only branch silently leaves an earlier
#: revision's outcome standing in the slot.
_NOTICE_LANES = (
    ("claude-review.yml", "Post Opus 5.5 review summary", False, False),
    ("codex-review.yml", "Post/update review comment", False, False),
    ("design-review.yml", "Post design review summary", True, True),
    ("first-principles-review.yml", "Post first-principles review summary", True, True),
    ("fork-design-review.yml", "Post/update design review comment", False, False),
    (
        "fork-first-principles-review.yml",
        "Post/update first-principles review comment",
        True,
        True,
    ),
    ("fork-gpt-review.yml", "Post/update summary comment", False, False),
    ("fork-opus-review.yml", "Post/update summary comment", False, False),
    ("fork-security-scope-review.yml", "Post/update the scope review comment", False, False),
    ("fork-ux-review.yml", "Post UX review summary", True, True),
    ("security-scope-review.yml", "Post the scope verdict", False, False),
    ("ux-review.yml", "Post UX review summary", True, True),
)

_NOTICE_LANE_PARAMS = [
    pytest.param(workflow, step, defines, creates, id=f"{workflow}-{int(defines)}{int(creates)}")
    for workflow, step, defines, creates in _NOTICE_LANES
]


class TestNoticeSlotLookupLicensesEveryCreate:
    """A notice comment is created only from a slot read that actually answered.

    The verdict writes in these steps route through a guarded upsert that
    pre-reads the slot and refuses an occupant. The override, no-contract and
    skip notices in the same steps do not: they decide between PATCH and CREATE
    from one marker lookup of their own. When that lookup is a single attempt
    whose error is swallowed, an ordinary API flake is indistinguishable from an
    empty slot, and the branch takes the CREATE arm against a slot that already
    holds a comment. Two comments then share one marker: a later lookup patches
    whichever id it picks first and the other keeps whatever line it carries,
    with no run that reconciles them.
    """

    #: The arms a notice write may sit behind. A write runs only when the slot
    #: read answered AND this PR's head is still the head the notice is about.
    #: The third arm only REPORTS, so it carries no head licence: an unreadable
    #: slot is a fact worth printing whatever the head now says, and requiring
    #: the licence there would swallow it whenever both reads fail at once.
    GATE = 'elif [ "$head_unchanged" -eq 1 ] && [ "$lookup_ok" -eq 1 ]; then'
    UNREADABLE = 'elif [ "$lookup_ok" -eq 0 ]; then'
    PATCH_GATE = (
        'if [ "$head_unchanged" -eq 1 ] && [ -n "$existing" ] && [ "$existing" != "null" ]; then'
    )

    def _notice_script(self, workflow: str, step: str) -> str:
        return _step_script(_workflow(workflow), step)

    def test_slot_lookup_is_one_budgeted_body_wherever_it_is_defined(self) -> None:
        # Same invariant retry_comment_write already carries, applied to the
        # lookup that gates the notice writes: one body, edited in every lane at
        # once, so the budget cannot drift lane by lane.
        bodies = set()
        for workflow, step, defines, _creates in _NOTICE_LANES:
            script = self._notice_script(workflow, step)
            if not defines:
                assert "find_existing() {" not in script, workflow
                continue
            bodies.add(_shell_function(script, "find_existing"))
        assert len(bodies) == 1, (
            "find_existing must stay byte-identical across every lane that "
            "defines one; edit all copies together"
        )
        canonical = bodies.pop()
        code = [line for line in canonical.splitlines() if not line.lstrip().startswith("#")]
        # Bounded and budgeted on the same terms as the reads that gate a
        # verdict: six attempts, 5s linear backoff, ~75s. The window this
        # exists for is an API exhaustion lasting minutes, not one bad request.
        assert any("for attempt in 1 2 3 4 5 6; do" in line for line in code)
        assert any('sleep "$(( attempt * 5 ))"' in line for line in code)
        assert any('if [ "$attempt" -lt 6 ]; then' in line for line in code)
        # The failure is REPORTED, not swallowed. `|| true` on the read is the
        # whole defect: it turns "the API refused" into "the slot is empty".
        assert not any("|| true" in line for line in code), code
        # Two separate facts, because only one of them licenses a create.
        assert any(line.strip() == "lookup_ok=1" for line in code)
        assert any(line.strip() == 'existing=""' for line in code)
        # `awk 'NR == 1'`, not `head -n1`: a head in the pipeline SIGPIPEs the
        # api call under pipefail, which is itself a swallowed read.
        assert not any("head -n1" in line for line in code), code
        assert any("| awk 'NR == 1'" in line for line in code)

    @pytest.mark.parametrize(("workflow", "step", "defines", "creates"), _NOTICE_LANE_PARAMS)
    def test_every_notice_create_sits_behind_the_gate(
        self, workflow: str, step: str, defines: bool, creates: bool
    ) -> None:
        # Enumerated per lane rather than spot-checked: the lanes that create a
        # notice must gate every one of those creates, and the lanes that do not
        # create must still not gain an ungated one later. A lane whose notices
        # only patch reports an unreadable slot instead, because the comment
        # left standing there describes an earlier revision.
        script = self._notice_script(workflow, step)
        lines = script.replace("\\\n", " ").splitlines()
        # The guarded upsert's own writes are not notices: they are already
        # pre-read and refused on an occupant, and they carry `write_rc` rather
        # than `|| true`.
        notice_creates = [
            (n, line)
            for n, line in enumerate(lines)
            if not line.lstrip().startswith("#")
            if "gh pr comment " in line
            if "write_rc" not in line
        ]
        if not creates:
            assert notice_creates == [], (
                f"{workflow} gained a notice create; flip its _NOTICE_LANES "
                f"'creates' flag to True and keep the gate below: {notice_creates}"
            )
        else:
            assert notice_creates, workflow
        for n, line in notice_creates:
            assert lines[n - 1].strip() == self.GATE, (workflow, line, lines[n - 1])
        if defines:
            # Every branch that reads the slot accounts for the unreadable case:
            # either it gates a create, or it says the notice did not land.
            assert self.GATE in script or self.UNREADABLE in script, workflow
            # This read names ITSELF in the log. The guarded upsert's own slot
            # pre-read already prints "to find this lane's slot failed on
            # attempt N"; reusing that sentence here makes a failed publish a
            # column of identical lines and leaves the reader unable to tell
            # which read gave up. Asserting on a phrase both reads share would
            # pass without measuring anything, because both live in this step.
            assert (
                "Reading this PR's comments before writing this lane's notice" in script
            ), workflow

    @pytest.mark.parametrize(("workflow", "step", "defines", "creates"), _NOTICE_LANE_PARAMS)
    def test_no_notice_read_keeps_its_own_unbudgeted_copy(
        self, workflow: str, step: str, defines: bool, creates: bool
    ) -> None:
        # A second, private marker lookup inside one branch is what lets the
        # override note read the slot on different terms from the rest of its own
        # step. Every marker-filtered read in these steps belongs to the one
        # budgeted body, so a lane may hold no other.
        script = self._notice_script(workflow, step)
        lines = script.splitlines()
        inline_reads = [
            line
            for line in lines
            if not line.lstrip().startswith("#")
            if "issues/$PR/comments" in line
            if "head -n1" in line
        ]
        assert inline_reads == [], (workflow, inline_reads)

    def test_head_confirmation_is_one_body_wherever_a_notice_is_written(self) -> None:
        # Same one-body rule the slot lookup carries, for the check that decides
        # whether writing is still safe. A per-lane copy is how one lane keeps
        # treating an unreadable head as a clear one.
        bodies = set()
        for workflow, step, defines, _creates in _NOTICE_LANES:
            script = self._notice_script(workflow, step)
            if not defines:
                assert "confirm_head() {" not in script, workflow
                continue
            bodies.add(_shell_function(script, "confirm_head"))
        assert len(bodies) == 1, (
            "confirm_head must stay byte-identical across every lane that "
            "writes a notice; edit all copies together"
        )
        code = [line for line in bodies.pop().splitlines() if not line.lstrip().startswith("#")]
        # The head is read from the PR itself, not from the event payload, which
        # names the head the run started on and need not still be current.
        assert any("repos/$REPO/pulls/$PR" in line for line in code), code
        assert any("'.head.sha'" in line for line in code), code
        # Three outcomes, and only one of them licenses a write. An unreadable
        # answer is treated as a moved head because writing is the direction
        # that destroys something: the notice carries no verdict, so declining
        # costs a stale line while writing costs a newer revision's verdict.
        assert any(line.strip() == "head_unchanged=1" for line in code), code
        assert sum(1 for line in code if line.strip() == "return 0") == 3, code
        assert any('[ -n "$head_now" ]' in line for line in code), code
        assert any('[ "$head_now" != "$HEAD" ]' in line for line in code), code
        # Both refusals are visible in the run log, and each names the revision
        # whose notice was withheld.
        assert sum(1 for line in code if "::warning::" in line) == 2, code
        assert sum(1 for line in code if "$HEAD" in line) >= 2, code
        # The read carries the same budget as the slot read above it, because the
        # two run inside one exhaustion window: a lane that has just spent up to
        # 75s retrying the slot is the least likely to get this one answered
        # first time, and an unbudgeted refusal here reads as a moved head.
        assert any("for attempt in 1 2 3 4 5 6; do" in line for line in code), code
        assert any('sleep "$(( attempt * 5 ))"' in line for line in code), code
        assert any('[ "$attempt" -lt 6 ]' in line for line in code), code
        # A moved head is a definite answer, so it is not retried: its `return`
        # sits INSIDE the loop, ahead of the line that reports a failed attempt.
        moved = next(n for n, line in enumerate(code) if '[ "$head_now" != "$HEAD" ]' in line)
        retry = next(n for n, line in enumerate(code) if "failed on attempt $attempt" in line)
        licensed = next(n for n, line in enumerate(code) if line.strip() == "head_unchanged=1")
        assert moved < retry, code
        assert licensed < retry, code

    @pytest.mark.parametrize(("workflow", "step", "defines", "creates"), _NOTICE_LANE_PARAMS)
    def test_every_notice_write_is_licensed_by_a_confirmed_head(
        self, workflow: str, step: str, defines: bool, creates: bool
    ) -> None:
        # Enumerated per lane, per arm. A notice PATCH is the arm that can bury
        # a verdict: the slot read answered, so the branch holds a real comment
        # id, and by the time it writes that comment can be the newer
        # revision's. A notice CREATE is the weaker half -- it presents a
        # superseded revision's line as the current one.
        script = self._notice_script(workflow, step)
        if not defines:
            assert "confirm_head" not in script, workflow
            return
        lines = script.replace("\\\n", " ").splitlines()
        bare = [line for line in lines if not line.lstrip().startswith("#")]
        # Both questions are asked before every write, and the ORDER is the content
        # of the rule, because the head check can spend over a minute on backoff:
        #
        #   creates  -- head first, slot read LAST. An empty slot means POST, so
        #               deciding that from a minute-old read lets another run on
        #               this same head fill the slot inside the gap. A write is
        #               pending as soon as the head holds, so asking early is free.
        #               The CREATE is safe from the slot read's own backoff: an
        #               empty slot stays empty-or-newer and the POST only adds.
        #               The PATCH is NOT -- it overwrites, and the head confirmed
        #               before a read that can sleep ~75s is stale by the time it
        #               writes, so a newer-head run that published in the gap
        #               would be buried. The PATCH therefore RE-CONFIRMS the head
        #               after the read, immediately before the write, and the
        #               destructive direction fails closed on a moved/unreadable
        #               head. So a creating arm asks the head TWICE (once to open
        #               the arm, once to license the PATCH) and reads the slot
        #               once.
        #   replaces -- slot read first, head inside the occupant test. There is no
        #               CREATE to misfire, and an empty slot means do nothing, so
        #               asking the head first reports a notice the arm never had.
        heads = [n for n, line in enumerate(bare) if line.strip() == "confirm_head"]
        reads = [n for n, line in enumerate(bare) if line.strip() == "find_existing"]
        assert heads and reads, workflow
        creating = replacing = 0
        consumed: set[int] = set()
        for n in heads:
            if n in consumed:
                continue
            following = bare[n + 1].strip()
            if following == "find_existing":
                # Head first, then slot: only legitimate where the arm can create.
                assert bare[n + 2].strip() == self.PATCH_GATE, (workflow, bare[n + 2])
                tail = "\n".join(bare[n : n + 16])
                assert self.GATE in tail, (workflow, "reads last but never creates", tail)
                # The PATCH overwrites, so a skip-notice arm RE-CONFIRMS the head
                # after the slot read, immediately before the write: the arm's
                # body opens with a second `confirm_head` and the destructive
                # write sits behind an `if head_unchanged` of its own. The
                # human-override arm predates this rule and writes a different
                # body (`ai-override-note.md`); it is the one creating arm not
                # held to the re-confirm, so a false negative there is a known
                # pre-existing gap, not one this enumeration introduces.
                is_override = "ai-override-note.md" in tail
                if not is_override:
                    recheck = bare[n + 3].strip()
                    assert recheck == "confirm_head", (
                        workflow,
                        "skip-notice patch does not re-confirm",
                        recheck,
                    )
                    assert bare[n + 4].strip() == 'if [ "$head_unchanged" -eq 1 ]; then', (
                        workflow,
                        bare[n + 4],
                    )
                    consumed.add(n + 3)
                creating += 1
                continue
            # Otherwise the head check sits inside the occupant test, which the
            # slot read must therefore precede.
            assert following == 'if [ "$head_unchanged" -eq 1 ]; then', (workflow, following)
            opener = bare[n - 1].strip()
            assert opener == 'if [ -n "$existing" ] && [ "$existing" != "null" ]; then', (
                workflow,
                opener,
            )
            assert bare[n - 2].strip() == "find_existing", (workflow, bare[n - 2])
            # And it must NOT create, or deferring the question would skip it.
            tail = "\n".join(bare[n - 2 : n + 14])
            assert self.GATE not in tail, (workflow, "defers the question yet creates", tail)
            replacing += 1
        # Every `find_existing` is matched by exactly one arm; the re-confirm
        # `confirm_head` the PATCH adds is paired to its opener above, not a new
        # read, so a creating arm shows two heads and one read.
        assert creating + replacing == len(reads), (workflow, heads, reads)
        # And every notice write arm states its own licence rather than
        # inheriting one from an enclosing branch. The verdict writes in the
        # same step are not notices: they route through retry_comment_write and
        # carry its `write_rc`, and the test below is what holds them out.
        notice_writes = [
            (n, line)
            for n, line in enumerate(bare)
            if "retry_comment_write" not in line
            if "write_rc" not in line
            if "issues/comments/$existing" in line or "gh pr comment " in line
        ]
        assert notice_writes, workflow
        for n, line in notice_writes:
            arm = next(
                bare[m].strip()
                for m in range(n, -1, -1)
                if bare[m].strip().startswith(("if ", "elif "))
            )
            assert '"$head_unchanged" -eq 1' in arm, (workflow, line, arm)
        # The complement, and the third case in the enumeration: the arm that
        # only REPORTS an unreadable slot writes nothing, so it needs no licence
        # and must not borrow one. Requiring it there loses the slot fact in the
        # one run where both reads fail, which is the run most in need of it.
        assert self.UNREADABLE in script, workflow
        # The destructive PATCH's own guard is the ONLY `elif`-free bare
        # `if head_unchanged` allowed: it sits inside the occupant arm, never as
        # a top-level `elif`, so a stale-head CREATE can never be reached through
        # it.
        assert 'elif [ "$head_unchanged" -eq 1 ]; then' not in script, workflow

    def test_the_verdict_path_does_not_take_the_notice_head_gate(self) -> None:
        # The qualifier on the rule above, and the reason it says NOTICE rather
        # than every write. A verdict withheld is the expensive direction: the
        # slot is the only thing a freshness verifier reads, so a run that
        # declines to publish leaves the revision indistinguishable from one no
        # lane ever reviewed. The guarded upsert therefore writes on its first
        # attempt whatever the head now says, and confirms the head only before
        # a REPEAT, whose first write may already have landed.
        for workflow, step, defines, _creates in _NOTICE_LANES:
            if not defines:
                continue
            script = self._notice_script(workflow, step)
            upsert_step = _step_script(_workflow(workflow), step)
            assert "retry_comment_write" in upsert_step, workflow
            body = _shell_function(upsert_step, "retry_comment_write")
            code = [line for line in body.splitlines() if not line.lstrip().startswith("#")]
            assert not any("head_unchanged" in line for line in code), workflow
            assert any('[ "$attempt" -gt 1 ]' in line for line in code), workflow
            assert any('[ "$head_now" != "$HEAD" ]' in line for line in code), workflow
            # The notice gate lives in the same step, so the two must not be
            # confused for one another by a later edit.
            assert "confirm_head() {" in script, workflow

    def test_a_moved_head_leaves_the_newer_revision_verdict_in_the_slot(
        self, tmp_path: Path
    ) -> None:
        # The behavioural half of the head gate, and the exact sequence the
        # backoff above widened: this run's slot read fails, it sleeps, a newer
        # revision publishes its verdict into the slot during that window, and
        # this run's retry then succeeds and holds a real comment id. Writing
        # that id replaces a verdict for a revision this run never reviewed,
        # and no later run puts it back.
        bash = _bash()
        if bash is None or shutil.which("jq") is None:
            pytest.skip("notice slot-lookup test requires Bash and jq")
        if os.name == "nt":
            pytest.skip("stubbed-PATH gh interception is exercised on POSIX runners")

        head = "a" * 40
        newer_head = "b" * 40
        stub_dir = tmp_path / "stub"
        stub_dir.mkdir()
        calls_dir = tmp_path / "calls"
        calls_dir.mkdir()
        runner_temp = tmp_path / "runner-temp"
        runner_temp.mkdir()

        gh_stub = stub_dir / "gh"
        gh_stub.write_text(
            "#!/usr/bin/env bash\n"
            "# The slot read answers and reports an occupant, so this run holds a\n"
            "# real comment id. The PR's head has moved on while it was reading.\n"
            'if [ "$1" = "api" ] && [ "$2" = "--method" ] && [ "$3" = "PATCH" ]; then\n'
            '  printf \'%s\\n\' "$4" >> "$STUB_CALLS/patch-calls.txt"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "api" ] && [ "$2" = "repos/o/r/pulls/1" ]; then\n'
            "  printf 'head\\n' >> \"$STUB_CALLS/head-calls.txt\"\n"
            f'  echo "{newer_head}"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "api" ]; then\n'
            "  printf 'read\\n' >> \"$STUB_CALLS/read-calls.txt\"\n"
            "  echo '4242'\n"
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "pr" ] && [ "$2" = "comment" ]; then\n'
            "  printf 'create\\n' >> \"$STUB_CALLS/create-calls.txt\"\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        gh_stub.chmod(0o755)
        sleep_stub = stub_dir / "sleep"
        sleep_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        sleep_stub.chmod(0o755)

        script = self._notice_script(
            "first-principles-review.yml", "Post first-principles review summary"
        )
        script_file = tmp_path / "step.sh"
        script_file.write_text(script, encoding="utf-8")
        env = {
            "PATH": f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "STUB_CALLS": str(calls_dir),
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_OUTPUT": str(tmp_path / "gh-output.txt"),
            "GH_TOKEN": "stub",
            "REPO": "o/r",
            "PR": "1",
            "HEAD": head,
            "HUMAN_OVERRIDE": "false",
            "OVERRIDE_ACTOR": "",
            "ACTOR": "someone",
            "EXEC_FILE": "",
            "SURFACE": "true",
            "CONTRACT": "false",
            "REVIEW_OUTCOME": "success",
        }
        result = subprocess.run(
            [bash, str(script_file)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr.decode()
        # The assertion that names the defect: the slot read answered and gave a
        # comment id, so without the head check this branch PATCHes its own
        # notice over the newer revision's verdict.
        assert not (calls_dir / "patch-calls.txt").exists()
        assert not (calls_dir / "create-calls.txt").exists()
        # The head was in fact consulted, once, and only after the slot read.
        assert (calls_dir / "head-calls.txt").read_text(encoding="utf-8").splitlines() == ["head"]
        # The log names both revisions, so a reader can tell which run gave way
        # to which.
        stdout = result.stdout.decode()
        assert "::warning::" in stdout
        assert newer_head in stdout
        assert head in stdout

    def test_an_unreadable_slot_creates_nothing_and_says_so(self, tmp_path: Path) -> None:
        # The behavioural half. The lane has a no-contract notice to publish and
        # the comments API refuses every read. The slot in fact already holds
        # this lane's comment, so the create arm would produce the second comment
        # that no run can take back out.
        bash = _bash()
        if bash is None or shutil.which("jq") is None:
            pytest.skip("notice slot-lookup test requires Bash and jq")
        if os.name == "nt":
            pytest.skip("stubbed-PATH gh interception is exercised on POSIX runners")

        head = "f" * 40
        stub_dir = tmp_path / "stub"
        stub_dir.mkdir()
        calls_dir = tmp_path / "calls"
        calls_dir.mkdir()
        runner_temp = tmp_path / "runner-temp"
        runner_temp.mkdir()

        gh_stub = stub_dir / "gh"
        gh_stub.write_text(
            "#!/usr/bin/env bash\n"
            "# Records every mutation; refuses every comments read, which is the\n"
            "# condition the gate exists for.\n"
            'if [ "$1" = "api" ] && [ "$2" = "--method" ] && [ "$3" = "PATCH" ]; then\n'
            '  printf \'%s\\n\' "$4" >> "$STUB_CALLS/patch-calls.txt"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "api" ] && [ "$2" = "repos/o/r/pulls/1" ]; then\n'
            "  printf 'head\\n' >> \"$STUB_CALLS/head-calls.txt\"\n"
            "  echo 'gh: api rate limit exceeded' >&2\n"
            "  exit 1\n"
            "fi\n"
            'if [ "$1" = "api" ]; then\n'
            "  printf 'read\\n' >> \"$STUB_CALLS/read-calls.txt\"\n"
            "  echo 'gh: api rate limit exceeded' >&2\n"
            "  exit 1\n"
            "fi\n"
            'if [ "$1" = "pr" ] && [ "$2" = "comment" ]; then\n'
            "  printf 'create\\n' >> \"$STUB_CALLS/create-calls.txt\"\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        gh_stub.chmod(0o755)
        # `sleep` is stubbed away so the six-attempt budget costs no wall clock.
        sleep_stub = stub_dir / "sleep"
        sleep_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        sleep_stub.chmod(0o755)

        script = self._notice_script(
            "first-principles-review.yml", "Post first-principles review summary"
        )
        script_file = tmp_path / "step.sh"
        script_file.write_text(script, encoding="utf-8")
        env = {
            "PATH": f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "STUB_CALLS": str(calls_dir),
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_OUTPUT": str(tmp_path / "gh-output.txt"),
            "GH_TOKEN": "stub",
            "REPO": "o/r",
            "PR": "1",
            "HEAD": head,
            "HUMAN_OVERRIDE": "false",
            "OVERRIDE_ACTOR": "",
            "ACTOR": "someone",
            "EXEC_FILE": "",
            # The no-contract branch: the lane has reviewable surface and the
            # contract step reported the rubric absent from the base commit.
            "SURFACE": "true",
            "CONTRACT": "false",
            "REVIEW_OUTCOME": "success",
        }
        result = subprocess.run(
            [bash, str(script_file)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr.decode()
        # Nothing was written on either arm. The create is the assertion that
        # names the defect: with an unbudgeted, error-swallowing read this is the
        # second comment under this lane's marker, and no run takes it back out.
        # No PATCH either, against an id the failed read never produced.
        assert not (calls_dir / "create-calls.txt").exists()
        assert not (calls_dir / "patch-calls.txt").exists()
        # And the read was budgeted, not attempted once.
        reads = (calls_dir / "read-calls.txt").read_text(encoding="utf-8").splitlines()
        assert len(reads) == 6, reads
        # So was the head check that follows it, and this run is why: one
        # exhausted window refuses BOTH reads, so an unbudgeted head read would
        # spend the slot budget and then treat its own first refusal as a moved
        # head. It refuses here only after the same 6 attempts.
        heads = (calls_dir / "head-calls.txt").read_text(encoding="utf-8").splitlines()
        assert len(heads) == 6, heads
        # The run says which notice did not land, naming the revision.
        stdout = result.stdout.decode()
        assert "::warning::" in stdout
        assert "unreadable after 6 attempts" in stdout
        assert head in stdout

    def test_a_transient_head_read_is_retried_not_read_as_a_moved_head(
        self, tmp_path: Path
    ) -> None:
        # The sequence the unbudgeted read turned into a withheld write: a human
        # override is accepted, the slot holds this lane's prior blocking comment,
        # and the head read blips twice before answering with the SAME head. One
        # attempt makes that blip indistinguishable from a moved head, so nothing
        # is written while `verdict=OVERRIDE` is still emitted -- the slot keeps a
        # block over an override a human already accepted, and no later run in
        # this lane clears it.
        bash = _bash()
        if bash is None or shutil.which("jq") is None:
            pytest.skip("notice slot-lookup test requires Bash and jq")
        if os.name == "nt":
            pytest.skip("stubbed-PATH gh interception is exercised on POSIX runners")

        head = "c" * 40
        stub_dir = tmp_path / "stub"
        stub_dir.mkdir()
        calls_dir = tmp_path / "calls"
        calls_dir.mkdir()
        runner_temp = tmp_path / "runner-temp"
        runner_temp.mkdir()

        gh_stub = stub_dir / "gh"
        gh_stub.write_text(
            "#!/usr/bin/env bash\n"
            "# The slot read answers at once and reports an occupant. The head\n"
            "# read refuses twice, then answers with the head this run is about.\n"
            'if [ "$1" = "api" ] && [ "$2" = "--method" ] && [ "$3" = "PATCH" ]; then\n'
            '  printf \'%s\\n\' "$4" >> "$STUB_CALLS/patch-calls.txt"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "api" ] && [ "$2" = "repos/o/r/pulls/1" ]; then\n'
            "  printf 'head\\n' >> \"$STUB_CALLS/head-calls.txt\"\n"
            '  attempts=$(wc -l < "$STUB_CALLS/head-calls.txt")\n'
            '  if [ "$attempts" -lt 3 ]; then\n'
            "    echo 'api blip' >&2\n"
            "    exit 1\n"
            "  fi\n"
            f'  echo "{head}"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "api" ]; then\n'
            "  printf 'read\\n' >> \"$STUB_CALLS/read-calls.txt\"\n"
            "  echo '4242'\n"
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "pr" ] && [ "$2" = "comment" ]; then\n'
            "  printf 'create\\n' >> \"$STUB_CALLS/create-calls.txt\"\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        gh_stub.chmod(0o755)
        sleep_stub = stub_dir / "sleep"
        sleep_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        sleep_stub.chmod(0o755)

        script = self._notice_script(
            "first-principles-review.yml", "Post first-principles review summary"
        )
        script_file = tmp_path / "step.sh"
        script_file.write_text(script, encoding="utf-8")
        env = {
            "PATH": f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "STUB_CALLS": str(calls_dir),
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_OUTPUT": str(tmp_path / "gh-output.txt"),
            "GH_TOKEN": "stub",
            "REPO": "o/r",
            "PR": "1",
            "HEAD": head,
            "HUMAN_OVERRIDE": "true",
            "OVERRIDE_ACTOR": "someone",
            "ACTOR": "someone",
            "EXEC_FILE": "",
            "SURFACE": "true",
            "CONTRACT": "true",
            "REVIEW_OUTCOME": "success",
        }
        result = subprocess.run(
            [bash, str(script_file)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr.decode()
        # The assertion that names the defect: the override note replaced the
        # blocking comment, rather than the blip leaving it standing.
        assert (calls_dir / "patch-calls.txt").exists()
        assert (calls_dir / "patch-calls.txt").read_text(encoding="utf-8").splitlines() == [
            "repos/o/r/issues/comments/4242"
        ]
        # It took three attempts to get there, and no second comment was made.
        assert len((calls_dir / "head-calls.txt").read_text(encoding="utf-8").splitlines()) == 3
        assert not (calls_dir / "create-calls.txt").exists()
        # A retried blip is not reported as a withheld notice.
        stdout = result.stdout.decode()
        assert "::warning::" not in stdout, stdout
        assert "failed on attempt 1" in stdout
        assert (tmp_path / "gh-output.txt").read_text(encoding="utf-8").count(
            "verdict=OVERRIDE"
        ) == 1

    def test_a_skip_arm_with_an_empty_slot_creates_a_head_stamped_notice(
        self, tmp_path: Path
    ) -> None:
        # A whole-design lane that declines to review must leave a slot that
        # NAMES the head it declined, so readiness can tell "did not review this
        # head" (slot present, head named) from "could not publish" (slot absent
        # on a `success` conclusion). The skip arm therefore confirms the head
        # and CREATES the keyed comment when the slot is empty: without the slot
        # readiness cannot distinguish the two, so a backend-only PR either
        # wedges on a permanent pending or scores as reviewed.
        bash = _bash()
        if bash is None or shutil.which("jq") is None:
            pytest.skip("notice slot-lookup test requires Bash and jq")
        if os.name == "nt":
            pytest.skip("stubbed-PATH gh interception is exercised on POSIX runners")

        head = "d" * 40
        stub_dir = tmp_path / "stub"
        stub_dir.mkdir()
        calls_dir = tmp_path / "calls"
        calls_dir.mkdir()
        runner_temp = tmp_path / "runner-temp"
        runner_temp.mkdir()

        gh_stub = stub_dir / "gh"
        gh_stub.write_text(
            "#!/usr/bin/env bash\n"
            "# The slot read answers and reports the slot empty. The head read\n"
            "# answers and confirms the head is unchanged, so the create arm\n"
            "# fires and the body it posts is recorded.\n"
            'if [ "$1" = "api" ] && [ "$2" = "--method" ] && [ "$3" = "PATCH" ]; then\n'
            '  printf \'%s\\n\' "$4" >> "$STUB_CALLS/patch-calls.txt"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "api" ] && [ "$2" = "repos/o/r/pulls/1" ]; then\n'
            "  printf 'head\\n' >> \"$STUB_CALLS/head-calls.txt\"\n"
            "  printf '%s\\n' \"$HEAD\"\n"
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "api" ]; then\n'
            "  printf 'read\\n' >> \"$STUB_CALLS/read-calls.txt\"\n"
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "pr" ] && [ "$2" = "comment" ]; then\n'
            "  printf 'create\\n' >> \"$STUB_CALLS/create-calls.txt\"\n"
            '  while [ "$#" -gt 0 ]; do\n'
            '    if [ "$1" = "--body-file" ]; then cat "$2" >> "$STUB_CALLS/create-body.txt"; fi\n'
            "    shift\n"
            "  done\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        gh_stub.chmod(0o755)
        sleep_stub = stub_dir / "sleep"
        sleep_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        sleep_stub.chmod(0o755)

        script = self._notice_script(
            "first-principles-review.yml", "Post first-principles review summary"
        )
        script_file = tmp_path / "step.sh"
        script_file.write_text(script, encoding="utf-8")
        env = {
            "PATH": f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "STUB_CALLS": str(calls_dir),
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_OUTPUT": str(tmp_path / "gh-output.txt"),
            "GH_TOKEN": "stub",
            "REPO": "o/r",
            "PR": "1",
            "HEAD": head,
            "HUMAN_OVERRIDE": "false",
            "OVERRIDE_ACTOR": "",
            "ACTOR": "someone",
            "EXEC_FILE": "",
            # The skip arm: this revision ships no reviewable capability.
            "SURFACE": "false",
            "CONTRACT": "true",
            "REVIEW_OUTCOME": "success",
        }
        result = subprocess.run(
            [bash, str(script_file)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr.decode()
        # The head WAS read -- a create is pending, so the arm licenses it on a
        # confirmed head rather than writing blind.
        assert (calls_dir / "head-calls.txt").exists()
        # The slot was consulted and reported empty, so the arm CREATED the
        # notice rather than patching or doing nothing.
        assert (calls_dir / "create-calls.txt").exists()
        assert not (calls_dir / "patch-calls.txt").exists()
        # And the created body names THIS head in prose: that is the per-head
        # marker readiness scopes the skip exemption on (pr_status.py
        # `slots_naming_head`).
        created = (calls_dir / "create-body.txt").read_text(encoding="utf-8")
        assert head in created, created
        # The lane still reported its skip.
        assert "verdict=SKIPPED" in (tmp_path / "gh-output.txt").read_text(encoding="utf-8")

    def test_a_create_arm_reads_the_slot_after_the_head_backoff(self, tmp_path: Path) -> None:
        # The window the head budget opens, and the one arm it can hurt. The head
        # check may spend over a minute retrying; a create decided from a slot read
        # taken BEFORE that wait acts on a minute-old answer. Two runs of this lane
        # on the SAME head both pass the head check -- a re-run, or a run cancelled
        # by cancel-in-progress whose post step keeps executing -- so the second
        # comment lands under one marker and no run takes it back out.
        #
        # The stub reports the slot empty on the first read and occupied on any
        # later one, so an arm that reads before the wait creates and an arm that
        # reads after it patches. The head answers only on its third attempt, which
        # is what puts real backoff between the two reads.
        bash = _bash()
        if bash is None or shutil.which("jq") is None:
            pytest.skip("notice slot-lookup test requires Bash and jq")
        if os.name == "nt":
            pytest.skip("stubbed-PATH gh interception is exercised on POSIX runners")

        head = "e" * 40
        stub_dir = tmp_path / "stub"
        stub_dir.mkdir()
        calls_dir = tmp_path / "calls"
        calls_dir.mkdir()
        runner_temp = tmp_path / "runner-temp"
        runner_temp.mkdir()

        gh_stub = stub_dir / "gh"
        gh_stub.write_text(
            "#!/usr/bin/env bash\n"
            'if [ "$1" = "api" ] && [ "$2" = "--method" ] && [ "$3" = "PATCH" ]; then\n'
            '  printf \'%s\\n\' "$4" >> "$STUB_CALLS/patch-calls.txt"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "api" ] && [ "$2" = "repos/o/r/pulls/1" ]; then\n'
            "  printf 'head\\n' >> \"$STUB_CALLS/head-calls.txt\"\n"
            '  attempts=$(wc -l < "$STUB_CALLS/head-calls.txt")\n'
            '  if [ "$attempts" -lt 3 ]; then\n'
            "    exit 1\n"
            "  fi\n"
            f'  echo "{head}"\n'
            "  exit 0\n"
            "fi\n"
            "# The slot is empty until the head backoff has run, and taken after it.\n"
            "# That is the race: another run on this same head publishes during the\n"
            "# wait. An arm reading the slot BEFORE the wait sees empty and creates;\n"
            "# one reading after sees the occupant and patches.\n"
            'if [ "$1" = "api" ]; then\n'
            "  printf 'read\\n' >> \"$STUB_CALLS/read-calls.txt\"\n"
            "  tries=0\n"
            '  if [ -f "$STUB_CALLS/head-calls.txt" ]; then\n'
            '    tries=$(wc -l < "$STUB_CALLS/head-calls.txt")\n'
            "  fi\n"
            '  if [ "$tries" -ge 3 ]; then\n'
            "    echo '4242'\n"
            "  fi\n"
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "pr" ] && [ "$2" = "comment" ]; then\n'
            "  printf 'create\\n' >> \"$STUB_CALLS/create-calls.txt\"\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        gh_stub.chmod(0o755)
        # A real sleep would make this test as slow as the backoff it proves; the
        # ORDER of the two reads is what decides the outcome, not the wall clock.
        sleep_stub = stub_dir / "sleep"
        sleep_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        sleep_stub.chmod(0o755)

        script = self._notice_script(
            "first-principles-review.yml", "Post first-principles review summary"
        )
        script_file = tmp_path / "step.sh"
        script_file.write_text(script, encoding="utf-8")
        env = {
            "PATH": f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "STUB_CALLS": str(calls_dir),
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_OUTPUT": str(tmp_path / "gh-output.txt"),
            "GH_TOKEN": "stub",
            "REPO": "o/r",
            "PR": "1",
            "HEAD": head,
            # The override-note arm, which is a creating arm.
            "HUMAN_OVERRIDE": "true",
            "OVERRIDE_ACTOR": "someone",
            "ACTOR": "someone",
            "EXEC_FILE": "",
            "SURFACE": "true",
            "CONTRACT": "true",
            "REVIEW_OUTCOME": "success",
        }
        result = subprocess.run(
            [bash, str(script_file)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr.decode()
        # The assertion that names the defect: the arm saw the occupant the head
        # backoff let arrive, so it patched instead of adding a second comment.
        assert not (calls_dir / "create-calls.txt").exists()
        assert (calls_dir / "patch-calls.txt").read_text(encoding="utf-8").splitlines() == [
            "repos/o/r/issues/comments/4242"
        ]
        # And the order is the reason: the head was asked before the slot, and the
        # slot read that decided the write came after the retries.
        heads = (calls_dir / "head-calls.txt").read_text(encoding="utf-8").splitlines()
        reads = (calls_dir / "read-calls.txt").read_text(encoding="utf-8").splitlines()
        assert len(heads) == 3, heads
        assert len(reads) == 1, reads

    def test_a_replace_arm_still_writes_from_a_head_read_taken_before_the_write(self) -> None:
        # The residual this enumeration leaves, pinned at its current answer so a
        # later reading of the rule does not mistake it for solved.
        #
        # On a replace-only arm the order has to be slot-then-head, so the head
        # answer is up to a minute old when the PATCH runs. The head check narrows
        # that window and cannot close it: the slot holds one comment id whichever
        # revision wrote it, so re-reading the id proves nothing about whose body is
        # there now. Closing it needs a conditional write the API does not offer.
        #
        # The exposure is bounded where the create arms' is not: a stale PATCH
        # overwrites one comment, while a stale CREATE adds a second under one
        # marker that no run removes.
        for workflow, step, defines, creates in _NOTICE_LANES:
            if not defines:
                continue
            script = self._notice_script(workflow, step)
            bare = [
                line
                for line in script.replace("\\\n", " ").splitlines()
                if not line.lstrip().startswith("#")
            ]
            for n, line in enumerate(bare):
                if line.strip() != "confirm_head":
                    continue
                if bare[n + 1].strip() == "find_existing":
                    continue  # a creating arm, covered by the test above
                # The replacing arm: no read sits between the head answer and the
                # write, so the head answer is as old as its own backoff.
                window = bare[n + 1 : n + 10]
                assert not any(entry.strip() == "find_existing" for entry in window), (
                    workflow,
                    window,
                )
                assert any("issues/comments/$existing" in entry for entry in window), (
                    workflow,
                    window,
                )
            # `creates` stays part of the table this residual is scoped by.
            assert creates in (True, False)

    def test_one_marker_serves_every_write_in_the_step(self) -> None:
        # The invariant the added comments state, pinned rather than asserted in
        # prose: a single marker is what makes the slot one slot. Two marker VALUES
        # in one step would split it, so the notice writes and the verdict writes
        # would read different comments and neither could see the other's.
        #
        # The value is the invariant, not the assignment count: a name re-assigned
        # to the same string still names one slot, while a second name or a second
        # value splits it however few times either is written.
        for workflow, step, defines, _creates in _NOTICE_LANES:
            if not defines:
                continue
            script = self._notice_script(workflow, step)
            assigns = [
                line.strip()
                for line in script.splitlines()
                if not line.lstrip().startswith("#")
                if re.match(r"^[A-Z_]*MARKER=", line.strip())
            ]
            assert assigns, workflow
            names = {line.split("=", 1)[0] for line in assigns}
            values = {line.split("=", 1)[1] for line in assigns}
            assert names == {"MARKER"}, (workflow, names)
            assert len(values) == 1, (workflow, values)
            # No lane retains a second marker name it could read the slot with.
            assert "OV_MARKER" not in script, workflow
            # And no second marker STRING anywhere in the step. This is what a
            # split slot looks like: the reads themselves spell the marker several
            # ways -- `$MARKER` directly, or a positional the caller fills from it
            # -- so the literal is the thing worth counting.
            literals = set(re.findall(r"<!--[^>]*-->", script))
            assert len(literals) == 1, (workflow, sorted(literals))
            assert literals == {values.pop().strip('"')}, (workflow, literals)

    @pytest.mark.parametrize(("workflow", "step", "defines", "creates"), _NOTICE_LANE_PARAMS)
    def test_no_notice_body_is_staged_at_a_hardcoded_host_path(
        self, workflow: str, step: str, defines: bool, creates: bool
    ) -> None:
        # Every body a lane this change touches stages goes under the runner's own
        # temp dir. A hardcoded `/tmp/...` path is shared state three ways: two
        # lanes on one runner write the same file, a self-hosted runner keeps it
        # between jobs, and a test that executes the arm writes it on the host.
        #
        # Both spellings are accepted. `$RUNNER_TEMP/x` is the stricter one and
        # several fork lanes use it; `${RUNNER_TEMP:-/tmp}/x` adds a fallback for a
        # context where the variable is unset. What is refused is the literal path.
        del creates  # the rule is about where a body is staged, not who writes it
        script = self._notice_script(workflow, step)
        flat = script.replace("\\\n", " ")
        offenders = [
            line.strip()
            for line in flat.splitlines()
            if not line.lstrip().startswith("#")
            if re.search(r"(?:>|--body-file|\bcat)\s+/tmp/", line)
        ]
        if defines:
            # The five lanes this change owns: the rule holds outright.
            assert not offenders, (workflow, offenders)
            return
        # Outside them, the current answer is recorded rather than assumed clean, so
        # the one remaining instance is visible instead of quietly excluded by the
        # lane filter. Fixing it should turn this red and update the record.
        known_unfixed = {"claude-review.yml": ["/tmp/claude-summary.md"]}
        expected = known_unfixed.get(workflow, [])
        staged = sorted({m for m in re.findall(r"/tmp/[A-Za-z0-9_.-]+", " ".join(offenders))})
        assert staged == sorted(expected), (workflow, staged, expected)
