"""Credential-safe Coder CLI adapter returning bounded structured records."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.coder.startup import startup_timing
from kiro_crew.coder.user_bus import user_bus_command
from kiro_crew.sandbox import RLIMIT_PROFILE_SESSION_HOST, create_subprocess_limited

_COMMAND_TIMEOUT_SECS = 600.0
_OUTPUT_MAX_BYTES = 4 * 1024 * 1024
_PIPE_READ_BYTES = 64 * 1024
_HEALTH_OUTPUT_MAX_BYTES = 4 * 1024
_HEALTH_TIMEOUT_SECS = 30.0
_MEMORY_ELEVATED_PERCENT = 80.0
_MEMORY_CRITICAL_PERCENT = 90.0
_BYTES_PER_GIB = 1024**3
_MEMORY_BYTES_MAX = 1 << 60
_BOOTSTRAP_STATUS_RE = re.compile(r"^(?:complete|(?:running|failed):[a-z0-9-]{1,64})$")
_WORKSPACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_CODER_DEADLINE_EXTENSION_MINUTES_MIN = 30
logger = logging.getLogger(__name__)
_WORKSPACE_MEMORY_SCRIPT = r"""
import json
from pathlib import Path


def read_int(path):
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
        return None if value == "max" else int(value)
    except (OSError, ValueError):
        return None


meminfo = {}
for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
    key, value = line.split(":", 1)
    meminfo[key] = int(value.strip().split()[0]) * 1024

total = meminfo["MemTotal"]
available = meminfo["MemAvailable"]
for limit_path, current_path in (
    ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current"),
    (
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
        "/sys/fs/cgroup/memory/memory.usage_in_bytes",
    ),
):
    limit = read_int(limit_path)
    current = read_int(current_path)
    if limit is None or current is None or limit <= 0 or limit >= total:
        continue
    total = limit
    available = min(available, max(0, limit - current))
    break

try:
    bootstrap_status = Path("/var/lib/kirocrew/bootstrap-status").read_text(
        encoding="utf-8"
    ).strip()
except OSError:
    bootstrap_status = ""

print(json.dumps({
    "available_bytes": min(available, total),
    "total_bytes": total,
    "bootstrap_status": bootstrap_status,
}, separators=(",", ":")))
""".strip()


class CoderClientError(RuntimeError):
    """A bounded Coder lifecycle operation failed."""


@dataclass(frozen=True)
class CoderWorkspace:
    uuid: str
    name: str
    owner: str
    template: str
    status: str
    last_used_at: str
    # Build drift, as the control plane reports it. Defaulted because an older
    # deployment may omit them, and a caller must be able to tell "this build is
    # current" from "this deployment did not say", never conflate the two.
    # ``outdated`` is None when unknown.
    template_version_id: str = ""
    template_active_version_id: str = ""
    outdated: bool | None = None

    @property
    def build_is_current(self) -> bool | None:
        """True/False when drift is knowable, None when the deployment did not say.

        Unknown is not a pass: a caller that refuses to hand a session compute built
        from a superseded template version must treat None as "cannot prove current"
        rather than as current.
        """
        if self.outdated is not None:
            return not self.outdated
        if self.template_version_id and self.template_active_version_id:
            return self.template_version_id == self.template_active_version_id
        return None


@dataclass(frozen=True)
class CoderWorkspaceBuild:
    """One workspace build, which is what a compute GENERATION actually is.

    A MicroVM cannot be restarted in place -- every argument on the underlying
    resource forces replacement -- so "new compute for the same workspace" means a
    new Coder build of the same workspace. That makes ``build_id`` the compute
    identity and ``build_number`` the monotonic generation, both stated by the Coder
    control plane rather than by anything running inside the guest.

    ``created_at`` is the build's own start time. A runtime deadline is NOT here
    because Coder does not report it: a transport that needs one derives it itself.

    ``owner`` and ``template`` are carried alongside the build rather than fetched
    separately, and they are read with exactly the same envelope rules
    :class:`CoderWorkspace` uses (see :meth:`CoderClient._owner_of` and
    :meth:`CoderClient._template_of`). Anything deciding whether to STOP a workspace
    has to compare all four of uuid, owner, template and build against its own
    durable binding, and comparing fields that came from two different control-plane
    answers is how a caller ends up stopping something that changed between them.
    """

    workspace_uuid: str
    workspace_name: str
    build_id: str
    build_number: int
    created_at: str
    status: str
    transition: str
    owner: str = ""
    template: str = ""
    completed_at: str = ""


@dataclass(frozen=True)
class CoderWorkspaceMemory:
    available_gb: float
    total_gb: float
    used_percent: float
    pressure: str
    bootstrap_status: str = ""


Runner = Callable[[list[str], dict[str, str], Path], Awaitable[bytes]]


class CoderClient:
    def __init__(
        self,
        coder_bin: str,
        url: str,
        token: str,
        cwd: Path,
        runner: Runner | None = None,
    ) -> None:
        self.coder_bin = coder_bin
        self.url = url
        self._token = token
        self.cwd = cwd
        self._runner = runner or self._run

    def _env(self) -> dict[str, str]:
        env = {
            key: value
            for key in (
                "PATH",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "NO_PROXY",
            )
            if (value := os.environ.get(key))
        }
        env["CODER_URL"] = self.url
        env["CODER_SESSION_TOKEN"] = self._token
        return env

    async def _run(self, argv: list[str], env: dict[str, str], cwd: Path) -> bytes:
        process = await create_subprocess_limited(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=(
                platform_compat.CREATE_NEW_PROCESS_GROUP | platform_compat._SUBPROCESS_NO_WINDOW
            ),
            profile=RLIMIT_PROFILE_SESSION_HOST,
        )
        if process.stdout is None or process.stderr is None:
            raise CoderClientError("Coder lifecycle command pipes are unavailable")
        stdout_task = asyncio.create_task(self._drain_bounded(process.stdout, _OUTPUT_MAX_BYTES))
        # Drain stderr without retaining it: remote commands may print secrets
        # without labels, which a pattern-based redactor cannot safely identify.
        stderr_task = asyncio.create_task(self._drain_bounded(process.stderr, 0))
        wait_task = asyncio.create_task(process.wait())
        tasks = (stdout_task, stderr_task, wait_task)
        try:
            (stdout, stdout_exceeded), _stderr_result, returncode = await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=_COMMAND_TIMEOUT_SECS,
            )
        except asyncio.TimeoutError as exc:
            # The shared tree kill + bounded reap: a CLI child owns no session
            # runtime, so there is no lease for an ownership gate to consult.
            await platform_compat.kill_and_reap(process)
            raise CoderClientError("Coder lifecycle command timed out") from exc
        except asyncio.CancelledError:
            await platform_compat.kill_and_reap(process)
            raise
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if returncode:
            logger.warning("Coder lifecycle command failed (exit %d)", returncode)
            raise CoderClientError(f"Coder lifecycle command failed (exit {returncode})")
        if stdout_exceeded:
            raise CoderClientError("Coder lifecycle command returned too much data")
        return stdout

    @staticmethod
    async def _drain_bounded(
        stream: asyncio.StreamReader,
        limit: int,
    ) -> tuple[bytes, bool]:
        retained = bytearray()
        exceeded = False
        while chunk := await stream.read(_PIPE_READ_BYTES):
            remaining = limit - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])
            if len(chunk) > remaining:
                exceeded = True
        return bytes(retained), exceeded

    async def _call(self, *args: str) -> bytes:
        # Only the fixed verb is logged. Later arguments can carry user data.
        with startup_timing(logger, "cli_" + args[0]):
            return await self._runner([self.coder_bin, *args], self._env(), self.cwd)

    @staticmethod
    def _owner_of(raw: Mapping[str, Any]) -> Any:
        """Owner identity out of one ``coder list`` record.

        Lifecycle authorization binds to the immutable Coder owner ID. Older
        servers may omit it, so the display-name fallback is retained without
        preferring that mutable label when both are present. Shared with
        :meth:`_build` so a workspace record and the build record read off the SAME
        envelope can never disagree about who owns it.
        """
        return raw.get("owner_id", raw.get("owner_name", raw.get("owner", "")))

    @staticmethod
    def _template_of(raw: Mapping[str, Any]) -> Any:
        """Template name out of one ``coder list`` record. Shared with :meth:`_build`."""
        return raw.get("template_name", raw.get("template", ""))

    @staticmethod
    def _workspace(raw: object) -> CoderWorkspace | None:
        if not isinstance(raw, dict):
            return None
        latest = raw.get("latest_build")
        status = latest.get("status", "") if isinstance(latest, dict) else raw.get("status", "")
        values = {
            "uuid": raw.get("id", ""),
            "name": raw.get("name", ""),
            "owner": CoderClient._owner_of(raw),
            "template": CoderClient._template_of(raw),
            "status": status,
            "last_used_at": raw.get("last_used_at", ""),
        }
        if not all(isinstance(value, str) for value in values.values()):
            raise CoderClientError("Coder workspace record has an invalid shape")
        if not values["uuid"] or not values["name"]:
            return None
        # Drift metadata is optional and read defensively: a deployment that omits
        # it, or reports it in an unexpected type, must leave the existing session
        # path exactly as it was rather than fail a workspace query.
        version_id = latest.get("template_version_id") if isinstance(latest, dict) else None
        active_version_id = raw.get("template_active_version_id")
        outdated = raw.get("outdated")
        return CoderWorkspace(
            **values,
            template_version_id=version_id if isinstance(version_id, str) else "",
            template_active_version_id=(
                active_version_id if isinstance(active_version_id, str) else ""
            ),
            outdated=outdated if isinstance(outdated, bool) else None,
        )

    @staticmethod
    def _json(output: bytes, operation: str) -> Any:
        if not output.strip():
            return []
        try:
            return json.loads(output)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CoderClientError(f"Coder {operation} returned invalid data") from exc

    async def list_workspaces(self) -> tuple[CoderWorkspace, ...]:
        raw = self._json(await self._call("list", "--output", "json"), "workspace query")
        if not isinstance(raw, list):
            raise CoderClientError("Coder workspace query returned an invalid shape")
        records: list[CoderWorkspace] = []
        for value in raw:
            workspace = self._workspace(value)
            if workspace is not None:
                records.append(workspace)
        return tuple(records)

    async def get_workspace(self, name: str) -> CoderWorkspace | None:
        return next(
            (workspace for workspace in await self.list_workspaces() if workspace.name == name),
            None,
        )

    @staticmethod
    def _build(raw: object) -> CoderWorkspaceBuild | None:
        if not isinstance(raw, dict):
            return None
        latest = raw.get("latest_build")
        if not isinstance(latest, dict):
            return None
        number = latest.get("build_number")
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise CoderClientError("Coder workspace build number is invalid")
        job = latest.get("job")
        completed_at = job.get("completed_at", "") if isinstance(job, dict) else ""
        values = {
            "completed_at": completed_at or "",
            "workspace_uuid": raw.get("id", ""),
            "workspace_name": raw.get("name", ""),
            "build_id": latest.get("id", ""),
            "created_at": latest.get("created_at", ""),
            "status": latest.get("status", ""),
            "transition": latest.get("transition", ""),
            # Same envelope rules as _workspace, so a caller comparing identity
            # before a destructive stop/start is comparing one answer, not two.
            "owner": CoderClient._owner_of(raw),
            "template": CoderClient._template_of(raw),
        }
        if not all(isinstance(value, str) for value in values.values()):
            raise CoderClientError("Coder workspace build record has an invalid shape")
        if not values["workspace_uuid"] or not values["build_id"] or not values["created_at"]:
            return None
        return CoderWorkspaceBuild(build_number=number, **values)

    async def get_workspace_build(self, name: str) -> CoderWorkspaceBuild | None:
        """Return the latest build of *name*, read-only, without starting anything.

        Read from the same authenticated ``coder list`` payload as
        :meth:`get_workspace`, so the build and the workspace status a caller
        compares came from one control-plane answer rather than two.
        """
        validated = self._validated_workspace_name(name)
        raw = self._json(await self._call("list", "--output", "json"), "workspace query")
        if not isinstance(raw, list):
            raise CoderClientError("Coder workspace query returned an invalid shape")
        for value in raw:
            if isinstance(value, dict) and value.get("name") == validated:
                return self._build(value)
        return None

    async def current_user(self) -> tuple[str, str]:
        """Return the authenticated Coder username and immutable user id."""
        owner_bytes = await self._call("whoami", "--output", "json")
        if len(owner_bytes) > 1024:
            raise CoderClientError("Coder identity response is too large")
        identity = self._json(owner_bytes, "identity query")
        if not isinstance(identity, list) or len(identity) != 1:
            raise CoderClientError("Coder identity response is invalid")
        record = identity[0]
        if not isinstance(record, dict):
            raise CoderClientError("Coder identity response is invalid")
        owner = record.get("username")
        owner_id = record.get("user_id")
        if not isinstance(owner, str) or not owner or not isinstance(owner_id, str) or not owner_id:
            raise CoderClientError("Coder identity response is invalid")
        return owner, owner_id

    async def probe(self, *, template: str, preset: str) -> dict[str, str]:
        owner, _owner_id = await self.current_user()
        raw = self._json(
            await self._call("templates", "list", "--output", "json"),
            "template query",
        )
        if not isinstance(raw, list):
            raise CoderClientError("Coder template query returned an invalid shape")
        names: set[str] = set()
        for value in raw:
            if not isinstance(value, dict):
                continue
            # Coder's table renderer wraps current JSON records under the
            # display-column label; older releases returned the record itself.
            record = value.get("Template", value)
            if isinstance(record, dict) and isinstance(record.get("name"), str):
                names.add(record["name"])
        if template not in names:
            raise CoderClientError("Coder template is unavailable")
        # Presets are applied by the create command. Coder deployments that do
        # not advertise preset metadata still validate the template without
        # provisioning compute; create remains the authoritative preset check.
        _ = preset
        return {"owner": owner, "template": template}

    async def create_workspace(
        self,
        *,
        name: str,
        template: str,
        preset: str,
        stop_after_minutes: int,
    ) -> CoderWorkspace:
        workspace_name = self._validated_workspace_name(name)
        args = ["create", workspace_name, "--template", template]
        if preset:
            args.extend(("--preset", preset))
        args.extend(
            (
                "--stop-after",
                f"{stop_after_minutes}m",
                "--yes",
                "--use-parameter-defaults",
            )
        )
        await self._call(*args)
        workspace = await self.get_workspace(workspace_name)
        if workspace is None:
            raise CoderClientError("Coder did not return the created workspace")
        return workspace

    async def start_workspace(self, name: str) -> CoderWorkspace:
        workspace_name = self._validated_workspace_name(name)
        await self._call("start", workspace_name, "--yes")
        workspace = await self.get_workspace(workspace_name)
        if workspace is None:
            raise CoderClientError("Coder did not return the started workspace")
        return workspace

    async def stop_workspace(self, name: str) -> CoderWorkspace:
        await self._call("stop", self._validated_workspace_name(name), "--yes")
        workspace = await self.get_workspace(name)
        if workspace is None:
            raise CoderClientError("Coder did not return the stopped workspace")
        return workspace

    async def delete_workspace(self, name: str) -> None:
        await self._call("delete", self._validated_workspace_name(name), "--yes")

    @staticmethod
    def _validated_workspace_name(name: str) -> str:
        if not _WORKSPACE_NAME_RE.fullmatch(name):
            raise CoderClientError("Coder workspace name is invalid")
        return name

    async def has_active_workload_scope(self, name: str) -> bool:
        """Return whether this workspace has a live Kiro Crew systemd scope."""
        workspace = self._validated_workspace_name(name)
        pattern = f"kirocrew-{workspace}-*.scope"
        command = user_bus_command(
            [
                "systemctl",
                "--user",
                "list-units",
                "--type=scope",
                "--state=active",
                "--no-legend",
                "--plain",
                "--no-pager",
                pattern,
            ]
        )
        output = await self._call("ssh", "--disable-autostart", workspace, "--", command)
        return bool(output.strip())

    async def has_active_microvm_workload(self, name: str) -> bool:
        """Read the v2 supervisor activity predicate without starting a workspace.

        The caller must first match the control-plane workspace to the protected
        MicroVM configuration. Guest capabilities alone never select this path.
        """
        from kiro_crew.coder.workload_control import CONTROL_SCRIPT, parse_control_result

        workspace = self._validated_workspace_name(name)
        command = shlex.join(["/usr/bin/python3", "-c", CONTROL_SCRIPT, "activity"])
        output = await self._call("ssh", "--disable-autostart", workspace, "--", command)
        report = parse_control_result(output, "activity")
        return bool(report["has_active_runtime"])

    async def workspace_memory(self, name: str) -> CoderWorkspaceMemory:
        """Read bounded memory telemetry without starting a stopped workspace."""
        workspace = self._validated_workspace_name(name)
        remote_command = shlex.join(
            [
                "env",
                "-u",
                "CODER_AGENT_TOKEN",
                "-u",
                "CODER_AGENT_TOKEN_FILE",
                "python3",
                "-c",
                _WORKSPACE_MEMORY_SCRIPT,
            ]
        )
        try:
            output = await asyncio.wait_for(
                self._call(
                    "ssh",
                    "--disable-autostart",
                    workspace,
                    "--",
                    remote_command,
                ),
                timeout=_HEALTH_TIMEOUT_SECS,
            )
        except asyncio.TimeoutError as exc:
            raise CoderClientError("Coder workspace health probe timed out") from exc
        if len(output) > _HEALTH_OUTPUT_MAX_BYTES:
            raise CoderClientError("Coder workspace health probe returned too much data")
        raw = self._json(output, "workspace health probe")
        if not isinstance(raw, dict):
            raise CoderClientError("Coder workspace health probe returned an invalid shape")
        available = raw.get("available_bytes")
        total = raw.get("total_bytes")
        bootstrap_status = raw.get("bootstrap_status", "")
        if (
            not isinstance(available, int)
            or isinstance(available, bool)
            or not isinstance(total, int)
            or isinstance(total, bool)
            or total <= 0
            or total > _MEMORY_BYTES_MAX
            or available < 0
            or available > total
            or not isinstance(bootstrap_status, str)
            or bool(bootstrap_status and not _BOOTSTRAP_STATUS_RE.fullmatch(bootstrap_status))
        ):
            raise CoderClientError("Coder workspace health probe returned invalid memory data")
        used_percent = (total - available) / total * 100
        if used_percent >= _MEMORY_CRITICAL_PERCENT:
            pressure = "critical"
        elif used_percent >= _MEMORY_ELEVATED_PERCENT:
            pressure = "elevated"
        else:
            pressure = "normal"
        return CoderWorkspaceMemory(
            available_gb=round(available / _BYTES_PER_GIB, 2),
            total_gb=round(total / _BYTES_PER_GIB, 2),
            used_percent=round(used_percent, 1),
            pressure=pressure,
            bootstrap_status=bootstrap_status,
        )

    async def extend_workspace_deadline(self, name: str, minutes: int) -> None:
        """Renew a running workspace deadline without reprovisioning it."""
        workspace = self._validated_workspace_name(name)
        duration = max(_CODER_DEADLINE_EXTENSION_MINUTES_MIN, minutes)
        await self._call("schedule", "extend", workspace, f"{duration}m")
