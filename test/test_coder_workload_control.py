"""The guest supervisor result is verified, never taken on its word."""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew.coder import workload_control as controls


def _envelope(report: dict[str, Any], code: int) -> str:
    report.update(schema=controls.SCHEMA, execution_mode=controls.EXECUTION_MODE)
    return json.dumps({"exit_code": code, "report": report})


@pytest.mark.parametrize(
    ("action", "code", "report"),
    [
        ("freeze", 0, {"verified": False}),
        ("verify", 4, {"verified": True}),
        ("thaw", 0, {"thawed": False}),
        ("activity", 0, {"has_active_runtime": False}),
        ("cancel", 0, {"status": {"quiescent": False}}),
    ],
)
def test_control_does_not_accept_an_unverified_success(
    action: str,
    code: int,
    report: dict[str, Any],
) -> None:
    with pytest.raises(controls.WorkloadControlError):
        controls.parse_control_result(_envelope(report, code), action)


@pytest.mark.parametrize(
    ("action", "code", "report"),
    [
        ("freeze", 0, {"verified": True}),
        ("verify", 0, {"verified": True}),
        ("thaw", 0, {"thawed": True}),
        ("activity", 0, {"has_active_runtime": True}),
        ("activity", 5, {"has_active_runtime": False}),
        ("cancel", 0, {"status": {"quiescent": True}}),
    ],
)
def test_control_returns_the_report_for_a_verified_result(
    action: str,
    code: int,
    report: dict[str, Any],
) -> None:
    parsed = controls.parse_control_result(_envelope(report, code), action)

    assert parsed["schema"] == controls.SCHEMA


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json",
        json.dumps({"report": {"schema": controls.SCHEMA}}),
        json.dumps({"exit_code": 0, "report": "not-a-mapping"}),
        json.dumps({"exit_code": True, "report": {"schema": controls.SCHEMA, "verified": True}}),
        json.dumps({"exit_code": 0, "report": {"schema": "other/1", "verified": True}}),
    ],
)
def test_control_refuses_a_malformed_envelope(raw: bytes | str) -> None:
    with pytest.raises(controls.WorkloadControlError, match="was not verified"):
        controls.parse_control_result(raw, "freeze")


def test_an_activity_answer_inconsistent_with_its_exit_code_is_refused() -> None:
    with pytest.raises(controls.WorkloadControlError):
        controls.parse_control_result(_envelope({"has_active_runtime": True}, 5), "activity")


def test_a_freeze_report_without_the_gateway_selected_execution_mode_is_refused() -> None:
    raw = json.dumps(
        {
            "exit_code": 0,
            "report": {
                "schema": controls.SCHEMA,
                "execution_mode": "some-other-mode",
                "verified": True,
            },
        }
    )

    with pytest.raises(controls.WorkloadControlError):
        controls.parse_control_result(raw, "freeze")


def test_the_control_script_never_takes_its_program_path_from_the_guest() -> None:
    assert controls.WORKLOAD_PATH in controls.CONTROL_SCRIPT
    assert "/etc/kirocrew-coder-contract.json" in controls.CONTROL_SCRIPT
    assert "sys.argv" in controls.CONTROL_SCRIPT
    assert 'program = pathlib.Path("/opt/kirocrew/workload.py")' in controls.CONTROL_SCRIPT
    assert "shell=True" not in controls.CONTROL_SCRIPT
