"""Gateway-selected controls for the experimental unprivileged MicroVM supervisor."""

from __future__ import annotations

import json
from typing import Any

EXECUTION_MODE = "microvm-process-supervisor-v1"
SCHEMA = "kirocrew.microvm.workload/1"
WORKLOAD_PATH = "/opt/kirocrew/workload.py"

# Runs as the session user over the already-authenticated Coder channel. Never
# accept a program path from a guest contract or execute guest-writable code.
CONTROL_SCRIPT = r"""
import json, pathlib, stat, subprocess, sys
action, *arguments = sys.argv[1:]
if action not in {"freeze", "verify", "thaw", "cancel", "status", "activity"}:
    raise SystemExit(4)
program = pathlib.Path("/opt/kirocrew/workload.py")
for target in (program, pathlib.Path("/etc/kirocrew-coder-contract.json")):
    for path in (target, *target.parents):
        info = path.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise SystemExit(4)
        if path == target and not stat.S_ISREG(info.st_mode):
            raise SystemExit(4)
        if path != target and not stat.S_ISDIR(info.st_mode):
            raise SystemExit(4)
contract = json.loads(pathlib.Path("/etc/kirocrew-coder-contract.json").read_text())
if (contract.get("version") != 2
    or contract.get("execution_mode") != "microvm-process-supervisor-v1"
    or "process-supervisor" not in contract.get("capabilities", [])):
    raise SystemExit(4)
result = subprocess.run(
    ["/usr/bin/python3", str(program), action, *arguments],
    capture_output=True, timeout=90, check=False,
)
# Keep failure bodies as evidence, without making an inactive runtime an SSH error.
if len(result.stdout) > 1024 * 1024:
    raise SystemExit(4)
report = json.loads(result.stdout)
print(json.dumps({"exit_code": result.returncode, "report": report}))
"""


class WorkloadControlError(RuntimeError):
    """The guest did not provide a verified supervisor result."""


def parse_control_result(raw: bytes | str, action: str) -> dict[str, Any]:
    try:
        result = json.loads(raw)
        report = result["report"]
        code = result["exit_code"]
        if not isinstance(report, dict) or report.get("schema") != SCHEMA:
            raise ValueError("unexpected schema")
        # cancel/status do not publish an execution_mode field. The trusted
        # control script already verifies the image contract before invoking them.
        if action in {"freeze", "verify", "thaw", "activity"}:
            if report.get("execution_mode") != EXECUTION_MODE:
                raise ValueError("unexpected execution mode")
        expected_codes = {0, 5} if action == "activity" else {0}
        if type(code) is not int or code not in expected_codes:
            raise ValueError("supervisor refused operation")
        if action in {"freeze", "verify"} and report.get("verified") is not True:
            raise ValueError("freeze unverified")
        if action == "thaw" and report.get("thawed") is not True:
            raise ValueError("thaw unverified")
        if action == "activity":
            active = report.get("has_active_runtime")
            if type(active) is not bool or active != (code == 0):
                raise ValueError("inconsistent activity result")
        if action == "cancel":
            status = report.get("status")
            if not isinstance(status, dict) or status.get("quiescent") is not True:
                raise ValueError("runtime cleanup unverified")
        return report
    except (ValueError, TypeError, KeyError) as exc:
        raise WorkloadControlError(f"MicroVM supervisor {action} was not verified") from exc
