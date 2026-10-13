"""Prepare a session's Fargate task: the same seam :class:`CoderSessionEnvironments` offers.

A Fargate workspace is ephemeral: the task IS the workspace, and stopping it
deletes it. So a session keeps its task while it lives, an archived session's
task is stopped at once (there is nothing to keep it for), and a session reopened
after its task is gone gets a fresh one. The chat history, memory and approvals
stay on the gateway as always; only files in the task are lost.

Security properties:

* The gateway only ever acts on a task it recorded launching, and before reusing
  one it proves the task still carries this session's tag (the AWS-side record
  of who started it), so a recycled ARN can never be adopted.
* The session secret is generated here, delivered over the SSM forward by the
  one-shot enrolment, and kept in a 0600 file in a 0700 directory so a gateway
  restart can reach the same task. It is never in a task definition, an
  override, an environment variable, a log, or the agent's reach.
* A task whose enrolment this gateway did not win is stopped, never used.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from kiro_crew.atomic_write import atomic_write
from kiro_crew.coder.session_launch import PreparedRemoteSession
from kiro_crew.fargate_session import bootstrap, tasks
from kiro_crew.fargate_session.forward import PortForward
from kiro_crew.fargate_session.shell import FargateShell, FargateWorkerLaunch
from kiro_crew.platform_compat import file_lock
from kiro_crew.session_environment import SessionEnvironmentUnavailable, report_start_stage

PROVIDER_ID = "fargate"
_ENROLL_ATTEMPTS = 20
_ENROLL_GAP_SECS = 3.0
#: One forward per task, shared by every runtime and relay of that session.
_FORWARDS: dict[str, PortForward] = {}


@dataclass(frozen=True)
class SessionTask:
    session_key: str
    profile_id: str
    arn: str
    task_id: str
    cluster: str
    runtime_id: str = ""

    @property
    def task(self) -> tasks.FargateTask:
        return tasks.FargateTask(self.arn, self.task_id, self.cluster, self.runtime_id)


class SessionTaskRegistry:
    """``session key -> SessionTask``, written atomically under an exclusive lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(".lock")

    def _read(self) -> dict[str, SessionTask]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return {key: SessionTask(**value) for key, value in raw.get("tasks", {}).items()}
        except (OSError, ValueError, TypeError) as exc:
            raise SessionEnvironmentUnavailable(
                "the Fargate session registry is unreadable"
            ) from exc

    def _write(self, rows: dict[str, SessionTask]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "tasks": {k: asdict(v) for k, v in sorted(rows.items())}}
        atomic_write(
            self.path, json.dumps(payload, indent=2) + "\n", fsync=True, restrict_to_owner=True
        )

    def _locked(self, mutate):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+b") as handle:
            with file_lock(handle.fileno(), exclusive=True, required=True):
                rows = self._read()
                result = mutate(rows)
                self._write(rows)
                return result

    def get(self, session_key: str) -> Optional[SessionTask]:
        return self._read().get(session_key)

    def all(self) -> list[SessionTask]:
        return list(self._read().values())

    def put(self, row: SessionTask) -> None:
        self._locked(lambda rows: rows.__setitem__(row.session_key, row))

    def remove(self, session_key: str, arn: str) -> bool:
        """Drop the row only if it still names ``arn`` (a newer task is left alone)."""

        def mutate(rows: dict[str, SessionTask]) -> bool:
            current = rows.get(session_key)
            if current is None or current.arn != arn:
                return False
            del rows[session_key]
            return True

        return bool(self._locked(mutate))


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if hasattr(os, "getuid") and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise SessionEnvironmentUnavailable("the Fargate secret directory is not private")
    return path


class FargateSessionEnvironments:
    """Run, reuse and stop Fargate session tasks for the saved Fargate profiles."""

    def __init__(self, config: Any, data_home: Path) -> None:
        self._config = config
        self.data_home = Path(data_home)
        self.registry = SessionTaskRegistry(self.data_home / "fargate" / "session-tasks.json")
        self.secrets_dir = self.data_home / "fargate" / "secrets"
        self.worker_secrets_dir = self.data_home / "remote-session-secrets"

    def profiles(self) -> list[dict[str, Any]]:
        return [
            p
            for p in (getattr(self._config, "fargate_profiles", None) or [])
            if isinstance(p, dict)
        ]

    def profile(self, profile_id: str) -> dict[str, Any]:
        for p in self.profiles():
            if p.get("id") == profile_id:
                return p
        raise SessionEnvironmentUnavailable(
            "this session's Fargate profile is no longer configured"
        )

    # -- secrets --------------------------------------------------------------------
    def _secret_path(self, task_id: str) -> Path:
        if not task_id.isalnum():
            raise SessionEnvironmentUnavailable("unexpected ECS task id")
        return _private_dir(self.secrets_dir) / f"{task_id}.key"

    def _save_secret(self, task_id: str, secret: bytes) -> None:
        path = self._secret_path(task_id)
        fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        try:
            os.write(fd, secret.hex().encode())
        finally:
            os.close(fd)

    def _load_secret(self, task_id: str) -> Optional[bytes]:
        try:
            return bytes.fromhex(self._secret_path(task_id).read_text().strip())
        except (OSError, ValueError):
            return None

    def _forget_secret(self, task_id: str) -> None:
        with contextlib.suppress(OSError, SessionEnvironmentUnavailable):
            self._secret_path(task_id).unlink()

    # -- forwards -------------------------------------------------------------------
    def _forward(self, profile: dict[str, Any], task: tasks.FargateTask) -> PortForward:
        forward = _FORWARDS.get(task.task_id)
        if forward is None or forward.target != task.ssm_target:
            forward = PortForward(
                task.ssm_target,
                bootstrap.BOOTSTRAP_PORT,
                profile=str(profile.get("aws_profile") or ""),
                region=str(profile.get("region") or ""),
            )
            _FORWARDS[task.task_id] = forward
        return forward

    async def _drop_forward(self, task_id: str) -> None:
        forward = _FORWARDS.pop(task_id, None)
        if forward is not None:
            await forward.stop()

    # -- lifecycle ------------------------------------------------------------------
    async def _reusable(self, profile: dict[str, Any], row: SessionTask) -> Optional[bytes]:
        """The secret for ``row``'s task if it is still ours and running, else None."""
        secret = self._load_secret(row.task_id)
        if secret is None or not row.runtime_id:
            return None
        info = await tasks.describe(profile, row.task)
        if info is None or info.get("lastStatus") != "RUNNING":
            return None
        tags = {t.get("key"): t.get("value") for t in info.get("tags") or []}
        if tags.get(tasks.SESSION_TAG) != tasks.session_tag(row.session_key):
            return None
        return secret

    async def _launch(self, session_key: str, profile: dict[str, Any]) -> tuple[SessionTask, bytes]:
        report_start_stage(session_key, "provisioning")
        container = await tasks.task_container(profile)
        task = await tasks.run_task(
            profile,
            session_key,
            container=container,
            command=bootstrap.container_command(),
            environment={
                "KIROCREW_WORKDIR": str(profile.get("workdir") or ""),
                "KIROCREW_BOOTSTRAP_PORT": str(bootstrap.BOOTSTRAP_PORT),
            },
        )
        row = SessionTask(session_key, str(profile["id"]), task.arn, task.task_id, task.cluster)
        # Recorded before waiting, so an archive (or a failure below) can stop it.
        await asyncio.to_thread(self.registry.put, row)
        try:
            running = await tasks.wait_running(profile, task)
            secret = os.urandom(32)
            await self._enroll(self._forward(profile, running), secret)
            await asyncio.to_thread(self._save_secret, running.task_id, secret)
            row = SessionTask(
                session_key,
                row.profile_id,
                running.arn,
                running.task_id,
                running.cluster,
                running.runtime_id,
            )
            await asyncio.to_thread(self.registry.put, row)
            return row, secret
        except BaseException:
            await self._stop(profile, row, "the session task did not start cleanly")
            raise

    async def _enroll(self, forward: PortForward, secret: bytes) -> None:
        # The task reports RUNNING a few seconds before the bootstrap listens.
        last: Optional[BaseException] = None
        for _ in range(_ENROLL_ATTEMPTS):
            try:
                await bootstrap.enroll(forward.connect, secret)
                return
            except bootstrap.BootstrapError as exc:
                if "already enrolled" in str(exc):
                    raise SessionEnvironmentUnavailable(
                        "someone else reached the session task first; it was stopped"
                    ) from exc
                last = exc
            except SessionEnvironmentUnavailable as exc:
                last = exc
            await asyncio.sleep(_ENROLL_GAP_SECS)
        raise SessionEnvironmentUnavailable(f"the session task's bootstrap never answered: {last}")

    async def _stop(self, profile: dict[str, Any], row: SessionTask, reason: str) -> None:
        with contextlib.suppress(SessionEnvironmentUnavailable):
            await tasks.stop_task(profile, row.task, reason)
        await self._drop_forward(row.task_id)
        self._forget_secret(row.task_id)
        await asyncio.to_thread(self.registry.remove, row.session_key, row.arn)

    async def prepare(self, session_key: str, profile_id: str) -> PreparedRemoteSession:
        profile = self.profile(profile_id)
        row = await asyncio.to_thread(self.registry.get, session_key)
        secret: Optional[bytes] = None
        if row is not None:
            secret = await self._reusable(profile, row)
            if secret is None:
                # Gone, stopped or no longer provably ours: forget it, start fresh.
                await self._stop(profile, row, "replaced: no longer reachable")
                row = None
        created = False
        if row is None or secret is None:
            created = True
            row, secret = await self._launch(session_key, profile)
        forward = self._forward(profile, row.task)
        shell = FargateShell(forward.connect, secret)
        launch = FargateWorkerLaunch(f"fg-{row.task_id[:24]}", shell, self.worker_secrets_dir)
        return PreparedRemoteSession(
            launch,
            str(profile.get("workdir") or ""),
            f"task {row.task_id[:12]}",
            str(profile["id"]),
            str(profile.get("name") or profile["id"]),
            created=created,
        )

    async def archive_session(
        self, session_key: str, reason: str = "the session was archived"
    ) -> bool:
        """Stop a session's task now: an ephemeral workspace has nothing to keep."""
        row = await asyncio.to_thread(self.registry.get, session_key)
        if row is None:
            return False
        try:
            profile = self.profile(row.profile_id)
        except SessionEnvironmentUnavailable:
            # The profile was removed; the row still names the region-less task.
            profile = {"aws_profile": "", "region": ""}
        await self._stop(profile, row, reason)
        return True
