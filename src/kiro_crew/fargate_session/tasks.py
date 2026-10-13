"""ECS calls for a Fargate session task, through the ``cloud.aws`` chokepoint.

Every call goes through :func:`kiro_crew.cloud.aws.checked_json`, which runs the
``aws`` CLI with the profile's own named credentials (never a raw key) under the
OS sandbox, and refuses mutations from an agent subprocess. These run in the
gateway process on a session's behalf, never as agent tools.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from kiro_crew.session_environment import SessionEnvironmentUnavailable

#: Tag on every session task, so the gateway only ever stops what it started.
SESSION_TAG = "kirocrew:session"
STARTED_BY = "kirocrew-session"
_WAIT_SECS = 300.0
_POLL_SECS = 3.0


@dataclass(frozen=True)
class FargateTask:
    arn: str
    task_id: str
    cluster: str
    runtime_id: str = ""

    @property
    def ssm_target(self) -> str:
        """The SSM target for a port forward into this task's container."""
        return f"ecs:{self.cluster}_{self.task_id}_{self.runtime_id}"


def session_tag(session_key: str) -> str:
    """A tag value that names the session without putting its key in AWS."""
    return hashlib.sha256(session_key.encode()).hexdigest()[:32]


async def _aws(args: list[str], profile: Mapping[str, Any], action: str) -> Any:
    from kiro_crew.cloud import aws

    def call() -> Any:
        return aws.checked_json(
            args,
            str(profile.get("aws_profile") or ""),
            str(profile.get("region") or ""),
            action=action,
        )

    try:
        return await asyncio.to_thread(call)
    except aws.AWSError as exc:
        raise SessionEnvironmentUnavailable(f"AWS refused {action}: {exc}"[:500]) from exc


async def task_container(profile: Mapping[str, Any]) -> str:
    """The task definition's essential container, which the bootstrap replaces.

    Refused when it sets ``entryPoint``: an override can replace only the
    command, so an entry point would receive the bootstrap as its arguments.
    """
    data = await _aws(
        ["ecs", "describe-task-definition", "--task-definition", str(profile["task_definition"])],
        profile,
        "ecs:DescribeTaskDefinition",
    )
    containers = (data.get("taskDefinition") or {}).get("containerDefinitions") or []
    essential = [c for c in containers if c.get("essential", True)]
    if not essential:
        raise SessionEnvironmentUnavailable("the task definition has no essential container")
    container = essential[0]
    if container.get("entryPoint"):
        raise SessionEnvironmentUnavailable(
            "the task definition's container sets an entryPoint; session tasks need "
            "an image whose command can be replaced (no entryPoint)"
        )
    return str(container["name"])


async def run_task(
    profile: Mapping[str, Any],
    session_key: str,
    *,
    container: str,
    command: list[str],
    environment: Mapping[str, str],
) -> FargateTask:
    network = {
        "awsvpcConfiguration": {
            "subnets": list(profile["subnets"]),
            "securityGroups": list(profile["security_groups"]),
            "assignPublicIp": "ENABLED" if profile.get("assign_public_ip") else "DISABLED",
        }
    }
    overrides = {
        "containerOverrides": [
            {
                "name": container,
                "command": command,
                "environment": [{"name": k, "value": v} for k, v in environment.items()],
            }
        ]
    }
    data = await _aws(
        [
            "ecs",
            "run-task",
            "--cluster",
            str(profile["cluster"]),
            "--task-definition",
            str(profile["task_definition"]),
            "--launch-type",
            "FARGATE",
            # The SSM agent ECS injects is what the port forward reaches.
            "--enable-execute-command",
            "--started-by",
            STARTED_BY,
            "--network-configuration",
            json.dumps(network),
            "--overrides",
            json.dumps(overrides),
            "--tags",
            f"key={SESSION_TAG},value={session_tag(session_key)}",
        ],
        profile,
        "ecs:RunTask",
    )
    tasks = data.get("tasks") or []
    if not tasks:
        failures = data.get("failures") or []
        raise SessionEnvironmentUnavailable(f"ECS started no task: {failures}"[:500])
    arn = str(tasks[0]["taskArn"])
    return FargateTask(arn, arn.rsplit("/", 1)[-1], str(profile["cluster"]))


async def describe(profile: Mapping[str, Any], task: FargateTask) -> Optional[dict]:
    data = await _aws(
        [
            "ecs",
            "describe-tasks",
            "--cluster",
            task.cluster,
            "--tasks",
            task.arn,
            "--include",
            "TAGS",
        ],
        profile,
        "ecs:DescribeTasks",
    )
    tasks = data.get("tasks") or []
    return tasks[0] if tasks else None


async def wait_running(
    profile: Mapping[str, Any], task: FargateTask, *, sleep=asyncio.sleep
) -> FargateTask:
    """Poll until the task runs and its exec agent is up; the runtime id SSM needs."""
    waited = 0.0
    while waited < _WAIT_SECS:
        info = await describe(profile, task)
        if info is None:
            raise SessionEnvironmentUnavailable("the session task disappeared while starting")
        status = info.get("lastStatus")
        if status == "STOPPED":
            raise SessionEnvironmentUnavailable(
                f"the session task stopped while starting: {info.get('stoppedReason') or 'unknown'}"[
                    :400
                ]
            )
        if status == "RUNNING":
            for container in info.get("containers") or []:
                agents = container.get("managedAgents") or []
                running = any(
                    a.get("name") == "ExecuteCommandAgent" and a.get("lastStatus") == "RUNNING"
                    for a in agents
                )
                if running and container.get("runtimeId"):
                    return FargateTask(task.arn, task.task_id, task.cluster, container["runtimeId"])
        await sleep(_POLL_SECS)
        waited += _POLL_SECS
    raise SessionEnvironmentUnavailable("the session task did not become reachable in time")


async def stop_task(profile: Mapping[str, Any], task: FargateTask, reason: str) -> None:
    await _aws(
        [
            "ecs",
            "stop-task",
            "--cluster",
            task.cluster,
            "--task",
            task.arn,
            "--reason",
            reason[:255],
        ],
        profile,
        "ecs:StopTask",
    )
