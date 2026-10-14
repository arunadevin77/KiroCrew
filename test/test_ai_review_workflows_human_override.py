"""Review-workflow tests for human overrides: the handler's re-run of the fork lane
run an override binds.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from ai_review_workflows_helpers import (
    _bash,
    _fork_child_env,
    _proc_log,
    _step_script,
    _workflow,
    _write_stub,
)


class TestOverrideHandlerReRunsTheBoundForkLaneRun:
    """Execute the handler's re-run step against a stubbed GitHub API."""

    HEAD = "1234567890abcdef1234567890abcdef12345678"
    TRIGGER = {"id": 900, "run_attempt": 2}

    def _check_run(
        self, cid: int, lane: str, attempt: int, conclusion: str, text: str, pr: int = 7
    ):
        return {
            "id": cid,
            "external_id": f"{lane}-pr-{pr}-900-{attempt}",
            "status": "completed",
            "conclusion": conclusion,
            "details_url": f"https://github.com/example/repo/runs/{cid}",
            "output": {"text": text},
        }

    @staticmethod
    def _marker(run_id: str) -> str:
        return f"Lane run: https://github.com/example/repo/actions/runs/{run_id}\n\n<!-- ai-review-fork-lane run={run_id} -->"

    def _run(
        self,
        tmp_path: Path,
        target: str,
        check_runs: list,
        runs: dict,
        head_repo: str = "fork/repo",
    ):
        bash = _bash()
        if bash is None or os.name == "nt":
            pytest.skip("the handler step is Bash with a stubbed gh; POSIX only")
        stub_dir = tmp_path / "stub"
        stub_dir.mkdir()
        data = tmp_path / "data"
        data.mkdir()
        calls = tmp_path / "calls"
        calls.mkdir()
        (data / "runs.json").write_text(
            json.dumps(
                {
                    "workflow_runs": [
                        {
                            "id": 900,
                            "run_attempt": 2,
                            "path": ".github/workflows/fast-gate.yml",
                            "head_repository": {"full_name": "fork/repo"},
                            "head_branch": "feature",
                        },
                        # Sibling pull requests' newer Fast Gates on the same head:
                        # another fork, and the same fork's other branch.
                        {
                            "id": 950,
                            "run_attempt": 1,
                            "path": ".github/workflows/fast-gate.yml",
                            "head_repository": {"full_name": "other/repo"},
                            "head_branch": "feature",
                        },
                        {
                            "id": 960,
                            "run_attempt": 1,
                            "path": ".github/workflows/fast-gate.yml",
                            "head_repository": {"full_name": "fork/repo"},
                            "head_branch": "other-branch",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        (data / "checkruns.json").write_text(
            json.dumps({"check_runs": check_runs}), encoding="utf-8"
        )
        for run_id, payload in runs.items():
            (data / f"run-{run_id}.json").write_text(json.dumps(payload), encoding="utf-8")
        _write_stub(
            stub_dir / "gh",
            'args="$*"\n'
            f'printf \'%s\\n\' "$args" >> "{calls}/all.txt"\n'
            'case "$args" in\n'
            f'  *"/dispatches"*) printf \'%s\\n\' "$args" >> "{calls}/dispatch.txt"; exit 0 ;;\n'
            f'  *"/issues/"*"/comments"*) cat >> "{calls}/notice.txt"; exit 0 ;;\n'
            f'  *"/rerun"*) printf \'%s\\n\' "$args" >> "{calls}/rerun.txt"; exit 0 ;;\n'
            '  *"/cancel"*)\n'
            '    id="${args##*actions/runs/}"; id="${id%%/*}"\n'
            f'    touch "{calls}/cancelled-$id"; exit 0 ;;\n'
            f'  *"actions/runs?event=pull_request"*) cat "{data}/runs.json"; exit 0 ;;\n'
            f'  *"/check-runs?"*) cat "{data}/checkruns.json"; exit 0 ;;\n'
            '  *"actions/runs/"*)\n'
            '    id="${args##*actions/runs/}"; id="${id%% *}"\n'
            "    filter=.\n"
            '    case "$args" in *"--jq "*) filter="${args##*--jq }" ;; esac\n'
            f'    if [ -f "{data}/run-$id.json" ]; then\n'
            f'      if [ -f "{calls}/cancelled-$id" ]; then\n'
            f'        jq -c \'.status = "completed"\' "{data}/run-$id.json" | jq -r "$filter"\n'
            "      else\n"
            f'        jq -r "$filter" "{data}/run-$id.json"\n'
            "      fi\n"
            "      exit 0\n"
            "    fi\n"
            '    printf \'{"message":"Not Found","status":"404"}\'\n'
            '    echo "gh: Not Found (HTTP 404)" >&2\n'
            "    exit 1 ;;\n"
            "esac\n"
            'echo "unexpected gh call: $args" >&2\n'
            "exit 9\n",
        )
        _write_stub(stub_dir / "sleep", "exit 0\n")
        script = tmp_path / "step.sh"
        script.write_text(
            _step_script(
                _workflow("ai-review-human-override.yml"),
                "Re-run line reviewers with the human decision",
            ),
            encoding="utf-8",
            newline="\n",
        )
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
                HEAD_REPO=head_repo,
                HEAD_REF="feature",
                TARGET=target,
                IS_FORK="true",
                DEFAULT_BRANCH="main",
            ),
        )

        def read(name: str) -> str:
            f = calls / name
            return f.read_text(encoding="utf-8") if f.exists() else ""

        return result, read

    @staticmethod
    def _lane_run(run_id: int, workflow: str) -> dict:
        return {"id": run_id, "path": f".github/workflows/{workflow}", "status": "completed"}

    def test_it_re_runs_the_run_readiness_binds(self, tmp_path: Path) -> None:
        rows = [
            # An earlier attempt of the same trigger, and a sibling PR's row.
            self._check_run(10, "design", 1, "failure", self._marker("111")),
            self._check_run(11, "design", 2, "failure", self._marker("222")),
            self._check_run(12, "design", 2, "failure", self._marker("333"), pr=8),
        ]
        result, read = self._run(
            tmp_path, "design", rows, {"222": self._lane_run(222, "fork-design-review.yml")}
        )
        assert result.returncode == 0, _proc_log(result)
        assert "actions/runs/222/rerun" in read("rerun.txt"), _proc_log(result)
        assert "111" not in read("rerun.txt") and "333" not in read("rerun.txt")
        assert read("notice.txt") == ""
        assert read("dispatch.txt") == "", "a lane re-run reaches readiness through its sweep"

    def test_target_all_re_runs_only_the_red_lanes(self, tmp_path: Path) -> None:
        rows = [
            self._check_run(20, "design", 2, "failure", self._marker("401")),
            self._check_run(21, "ux", 2, "success", self._marker("402")),
            self._check_run(22, "first-principles", 2, "neutral", self._marker("403")),
            self._check_run(23, "scope", 2, "failure", self._marker("404")),
        ]
        runs = {
            "401": self._lane_run(401, "fork-design-review.yml"),
            "402": self._lane_run(402, "fork-ux-review.yml"),
            "403": self._lane_run(403, "fork-first-principles-review.yml"),
            "404": self._lane_run(404, "fork-security-scope-review.yml"),
        }
        result, read = self._run(tmp_path, "all", rows, runs)
        assert result.returncode == 0, _proc_log(result)
        reruns = read("rerun.txt")
        assert "runs/401/rerun" in reruns and "runs/404/rerun" in reruns, _proc_log(result)
        assert "402" not in reruns and "403" not in reruns, reruns
        # Opus and GPT have posted no row at this attempt: nothing to re-run,
        # and not a failure either.
        assert read("notice.txt") == "", read("notice.txt")

    @pytest.mark.parametrize(
        "text",
        [
            "no marker at all",
            "<!-- ai-review-fork-lane run=55a -->",
            "<!-- ai-review-fork-lane run= -->",
        ],
    )
    def test_a_row_without_a_digits_only_run_id_is_not_re_run(self, tmp_path: Path, text: str):
        rows = [self._check_run(30, "design", 2, "failure", text)]
        result, read = self._run(tmp_path, "design", rows, {})
        assert result.returncode == 0, _proc_log(result)
        assert read("rerun.txt") == ""
        assert "carries no lane-run id" in result.stdout, _proc_log(result)
        assert "Design Review" in read("notice.txt")
        # Nothing was re-run, so readiness is dispatched to read the record now.
        dispatch = read("dispatch.txt")
        assert "pr-readiness.yml/dispatches" in dispatch, _proc_log(result)
        assert "inputs[pr]=7" in dispatch and f"inputs[sha]={self.HEAD}" in dispatch
        assert "ref=main" in dispatch

    def test_a_failed_run_read_is_reported_as_a_read_failure(self, tmp_path: Path) -> None:
        # gh prints the 404 JSON body on stdout. Swallowing the exit status read
        # that body's `.path` as empty and reported "points at ''".
        rows = [self._check_run(40, "design", 2, "failure", self._marker("777"))]
        result, read = self._run(tmp_path, "design", rows, {})
        assert result.returncode == 0, _proc_log(result)
        assert read("rerun.txt") == ""
        assert "reading fork-design-review.yml run 777 failed" in result.stdout, _proc_log(result)
        assert "HTTP 404" in result.stdout
        assert "points at" not in result.stdout

    def test_a_run_of_another_workflow_is_refused(self, tmp_path: Path) -> None:
        rows = [self._check_run(50, "design", 2, "failure", self._marker("888"))]
        result, read = self._run(
            tmp_path, "design", rows, {"888": self._lane_run(888, "some-other.yml")}
        )
        assert result.returncode == 0, _proc_log(result)
        assert read("rerun.txt") == ""
        assert "refusing to re-run it" in result.stdout, _proc_log(result)

    def test_a_passing_lane_is_left_alone_and_readiness_recomputes(self, tmp_path: Path) -> None:
        rows = [self._check_run(60, "design", 2, "success", self._marker("999"))]
        result, read = self._run(
            tmp_path, "design", rows, {"999": self._lane_run(999, "fork-design-review.yml")}
        )
        assert result.returncode == 0, _proc_log(result)
        assert read("rerun.txt") == ""
        assert "already passes" in result.stdout
        assert "pr-readiness.yml/dispatches" in read("dispatch.txt")

    def test_the_newest_row_for_the_bound_id_wins(self, tmp_path: Path) -> None:
        # A re-run on an unchanged head keeps the trigger-bound id, so the stale
        # attempt's row and the fresh one share it; readiness reads the newest by
        # check-run id, and so does the handler.
        rows = [
            self._check_run(71, "design", 2, "failure", self._marker("111")),
            self._check_run(75, "design", 2, "failure", self._marker("222")),
            self._check_run(73, "design", 2, "failure", self._marker("333")),
        ]
        runs = {r: self._lane_run(int(r), "fork-design-review.yml") for r in ("111", "222", "333")}
        result, read = self._run(tmp_path, "design", rows, runs)
        assert result.returncode == 0, _proc_log(result)
        assert read("rerun.txt").strip().endswith("actions/runs/222/rerun"), read("rerun.txt")
        assert "111" not in read("rerun.txt") and "333" not in read("rerun.txt")

    def test_a_running_lane_is_cancelled_then_re_run(self, tmp_path: Path) -> None:
        row = self._check_run(80, "design", 2, "failure", self._marker("444"))
        row.update(status="in_progress", conclusion=None)
        run = self._lane_run(444, "fork-design-review.yml")
        run["status"] = "in_progress"
        result, read = self._run(tmp_path, "design", [row], {"444": run})
        assert result.returncode == 0, _proc_log(result)
        assert "actions/runs/444/cancel" in read("all.txt"), _proc_log(result)
        assert "actions/runs/444/rerun" in read("rerun.txt"), _proc_log(result)

    def test_a_deleted_fork_binds_no_run(self, tmp_path: Path) -> None:
        # Readiness binds no run to a head repository that is gone; neither may
        # the handler, by matching an empty name against a null one.
        rows = [self._check_run(90, "design", 2, "failure", self._marker("555"))]
        result, read = self._run(
            tmp_path,
            "design",
            rows,
            {"555": self._lane_run(555, "fork-design-review.yml")},
            head_repo="",
        )
        assert result.returncode == 0, _proc_log(result)
        assert read("rerun.txt") == ""
        assert "head repository or branch is gone" in result.stdout, _proc_log(result)
        assert "pr-readiness.yml/dispatches" in read("dispatch.txt")
