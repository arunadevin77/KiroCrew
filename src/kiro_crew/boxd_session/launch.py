"""Prepare a session's boxd machine: the same seam :class:`CoderSessionEnvironments` offers.

A boxd machine is persistent and hibernates when idle, so a session keeps one
machine for its life and its files survive between turns and gateway restarts.
An archived session's machine is hibernated (it then bills for disk only) and
deleted after ``archived_retention_days``; reopening it before then keeps it.
boxd's own ``delete-after`` timer is armed to the same deadline as a backstop,
so the machine goes even if this gateway never runs again.

Security properties:

* No credential is stored or read by Kiro Crew: the gateway machine's own
  ``boxd auth login`` authenticates every call, and nothing reaches the machine.
* The gateway only ever acts on a machine it recorded creating, under a name it
  minted (``kc-`` plus 16 random hex digits), recorded before the create call so
  a failure can still delete it. A machine deleted outside Kiro Crew is replaced
  by a fresh one, never looked up by any other name.
* The guest worker's per-session secret travels on exec stdin, never in argv.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from kiro_crew.atomic_write import atomic_write
from kiro_crew.boxd_session.cli import BoxdCLI, BoxdError
from kiro_crew.boxd_session.shell import BoxdShell
from kiro_crew.coder.remote_worker import RemoteWorkerLaunch
from kiro_crew.coder.session_launch import PreparedRemoteSession
from kiro_crew.platform_compat import file_lock
from kiro_crew.session_environment import SessionEnvironmentUnavailable, report_start_stage

PROVIDER_ID = "boxd"
NAME_PREFIX = "kc-"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class SessionMachine:
    session_key: str
    profile_id: str
    machine: str
    created_at: str = ""
    #: Set when the session is archived: when the machine is deleted.
    deletion_due_at: str = ""


class SessionMachineRegistry:
    """``session key -> SessionMachine``, written atomically under an exclusive lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(".lock")

    def _read(self) -> dict[str, SessionMachine]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return {k: SessionMachine(**v) for k, v in raw.get("machines", {}).items()}
        except (OSError, ValueError, TypeError) as exc:
            raise SessionEnvironmentUnavailable("the boxd session registry is unreadable") from exc

    def _write(self, rows: dict[str, SessionMachine]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "machines": {k: asdict(v) for k, v in sorted(rows.items())}}
        atomic_write(
            self.path, json.dumps(payload, indent=2) + "\n", fsync=True, restrict_to_owner=True
        )

    def _locked(self, mutate: Callable[[dict[str, SessionMachine]], Any]) -> Any:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+b") as handle:
            with file_lock(handle.fileno(), exclusive=True, required=True):
                rows = self._read()
                result = mutate(rows)
                self._write(rows)
                return result

    def get(self, session_key: str) -> Optional[SessionMachine]:
        return self._read().get(session_key)

    def all(self) -> list[SessionMachine]:
        return list(self._read().values())

    def put(self, row: SessionMachine) -> None:
        self._locked(lambda rows: rows.__setitem__(row.session_key, row))

    def remove(self, session_key: str, machine: str) -> bool:
        def mutate(rows: dict[str, SessionMachine]) -> bool:
            current = rows.get(session_key)
            if current is None or current.machine != machine:
                return False
            del rows[session_key]
            return True

        return bool(self._locked(mutate))

    def set_deletion(self, session_key: str, due: str) -> Optional[SessionMachine]:
        def mutate(rows: dict[str, SessionMachine]) -> Optional[SessionMachine]:
            current = rows.get(session_key)
            if current is None:
                return None
            rows[session_key] = replace(current, deletion_due_at=due)
            return rows[session_key]

        result: Optional[SessionMachine] = self._locked(mutate)
        return result

    def due(self, moment: datetime) -> list[SessionMachine]:
        stamp = _stamp(moment)
        return [
            r for r in self._read().values() if r.deletion_due_at and r.deletion_due_at <= stamp
        ]


class BoxdSessionEnvironments:
    """Create, reuse and delete boxd machines for the saved boxd profiles."""

    def __init__(self, config: Any, data_home: Path, *, cli: Optional[BoxdCLI] = None) -> None:
        self._config = config
        self.data_home = Path(data_home)
        self.registry = SessionMachineRegistry(self.data_home / "boxd" / "session-machines.json")
        self.worker_secrets_dir = self.data_home / "remote-session-secrets"
        self._cli = cli

    def cli(self) -> BoxdCLI:
        if self._cli is None:
            self._cli = BoxdCLI()
        return self._cli

    def profiles(self) -> list[dict[str, Any]]:
        return [
            p for p in (getattr(self._config, "boxd_profiles", None) or []) if isinstance(p, dict)
        ]

    def profile(self, profile_id: str) -> dict[str, Any]:
        for p in self.profiles():
            if p.get("id") == profile_id:
                return p
        raise SessionEnvironmentUnavailable("this session's boxd profile is no longer configured")

    def shell(self, machine: str) -> BoxdShell:
        return BoxdShell(self.cli().bin, machine)

    async def shell_for(self, session_key: str) -> Optional[BoxdShell]:
        """The session's existing machine, or None. Nothing is created."""
        row = await asyncio.to_thread(self.registry.get, session_key)
        return self.shell(row.machine) if row is not None else None

    async def prepare(self, session_key: str, profile_id: str) -> PreparedRemoteSession:
        profile = self.profile(profile_id)
        cli = self.cli()
        workdir = str(profile.get("workdir") or "")
        row = await asyncio.to_thread(self.registry.get, session_key)
        if row is not None:
            if await cli.get(row.machine) is None:
                # Deleted outside Kiro Crew: never adopt another name, start fresh.
                await asyncio.to_thread(self.registry.remove, session_key, row.machine)
                row = None
            elif row.deletion_due_at:
                # Reopened before its retention ran out: keep it, files intact.
                await cli.set_delete_after(row.machine, "off")
                await asyncio.to_thread(self.registry.set_deletion, session_key, "")
        created = row is None
        if row is None:
            row = await self._create(session_key, profile, cli)
        shell = self.shell(row.machine)
        if workdir:
            try:
                await shell.run(f"mkdir -p {shlex.quote(workdir)}")
            except Exception as exc:  # noqa: BLE001 -- surfaced as an unavailable session
                raise SessionEnvironmentUnavailable(
                    f"the boxd machine did not answer: {exc}"
                ) from exc
        launch = RemoteWorkerLaunch(
            f"bx-{row.machine[len(NAME_PREFIX):]}", shell, self.worker_secrets_dir
        )
        return PreparedRemoteSession(
            launch,
            workdir,
            row.machine,
            str(profile["id"]),
            str(profile.get("name") or profile["id"]),
            created=created,
        )

    async def _create(
        self, session_key: str, profile: dict[str, Any], cli: BoxdCLI
    ) -> SessionMachine:
        report_start_stage(session_key, "provisioning")
        row = SessionMachine(
            session_key, str(profile["id"]), NAME_PREFIX + os.urandom(8).hex(), _stamp(_now())
        )
        # Recorded before the create, so an archive (or a failure below) can delete it.
        await asyncio.to_thread(self.registry.put, row)
        try:
            await cli.create(
                row.machine,
                snapshot=str(profile.get("snapshot") or ""),
                vcpu=int(profile.get("vcpu") or 0),
            )
        except BaseException:
            try:
                await cli.remove(row.machine)
            except BoxdError:
                pass  # the row stays; the next prepare or sweep retries the cleanup
            else:
                await asyncio.to_thread(self.registry.remove, session_key, row.machine)
            raise
        return row

    async def archive_session(self, session_key: str, retention_days: int) -> bool:
        """Hibernate an archived session's machine and schedule it; 0 days deletes it now."""
        row = await asyncio.to_thread(self.registry.get, session_key)
        if row is None:
            return False
        cli = self.cli()
        if retention_days <= 0:
            await self._delete(row)
            return True
        due = _now() + timedelta(days=retention_days)
        await asyncio.to_thread(self.registry.set_deletion, session_key, _stamp(due))
        try:
            # boxd's own timer is the backstop if this gateway never runs again.
            await cli.set_delete_after(row.machine, f"{retention_days}d")
            await cli.hibernate(row.machine)
        except BoxdError:
            pass  # the stamp stands; the keeper's sweep still deletes it on time
        return True

    async def _delete(self, row: SessionMachine) -> None:
        await self.cli().remove(row.machine)
        await asyncio.to_thread(self.registry.remove, row.session_key, row.machine)

    async def sweep_due(self, *, now: Optional[datetime] = None) -> int:
        """Delete every archived session's machine whose retention ran out."""
        deleted = 0
        for row in await asyncio.to_thread(self.registry.due, now or _now()):
            try:
                await self._delete(row)
            except SessionEnvironmentUnavailable:
                continue  # retried on the next pass
            deleted += 1
        return deleted
