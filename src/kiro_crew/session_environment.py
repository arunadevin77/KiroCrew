"""Provider-neutral contracts for developer session execution environments."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_CONFIGURATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_RESOURCE_NAME_MAX_CHARS = 255
_ENVIRONMENT_STATES = frozenset({"starting", "running", "stopped", "unavailable"})
_MEMORY_PRESSURES = frozenset({"normal", "elevated", "critical"})


class SessionEnvironmentUnavailable(RuntimeError):
    """A persisted or selected environment provider cannot serve a session."""


# -- start progress ------------------------------------------------------------------
#
# A remote session's first start can take minutes (create + build a workspace), and
# without a signal the chat cannot tell a slow start from a hang. Providers report the
# stage they are in; the dashboard reads it while the turn waits. Provider-neutral
# names so Coder and Fargate report the same stages. In-memory and per-gateway: it
# describes a start in flight in THIS process and means nothing after a restart.

#: Ordered start stages. ``checking``: sign-in and finding the session's resource.
#: ``provisioning``: creating or starting compute. ``connecting``: installing and
#: reaching the guest worker. ``starting_agent``: the local engine's own startup.
START_STAGES: tuple[str, ...] = ("checking", "provisioning", "connecting", "starting_agent")
_START_PROGRESS_MAX = 256
#: session key -> (current stage, entered at, start began at, stages entered so
#: far, whether this start reconnects a session already attached here)
_start_progress: dict[str, tuple[str, float, float, tuple[str, ...], bool]] = {}


def report_start_stage(session_key: str | None, stage: str) -> None:
    """Record that ``session_key``'s environment start entered ``stage``."""
    if not session_key or stage not in START_STAGES:
        return
    now = time.monotonic()
    previous = _start_progress.get(session_key)
    began = previous[2] if previous is not None else now
    entered = previous[3] if previous is not None else ()
    if stage not in entered:
        entered = (*entered, stage)
    # Decided once, when the start begins: a session this gateway already
    # attached to a remote workspace is reconnecting (an engine restart), and
    # the dashboard shows that as an ordinary turn, not as setup.
    reconnect = previous[4] if previous is not None else session_key in _remote_workspaces
    if previous is None and len(_start_progress) >= _START_PROGRESS_MAX:
        # A leaked entry must never grow without bound; the oldest goes first.
        _start_progress.pop(next(iter(_start_progress)))
    _start_progress[session_key] = (stage, now, began, entered, reconnect)


def clear_start_stage(session_key: str | None) -> None:
    """The start finished (ready, failed or cancelled): stop reporting a stage."""
    if session_key:
        _start_progress.pop(session_key, None)


# -- live remote workspaces -----------------------------------------------------------
#
# Which sessions' runtimes are CURRENTLY attached to a remote workspace, so the
# per-message context can tell the model where its files live (the [PROJECT]
# line otherwise names the gateway's directory). Set and cleared by the provider
# at every runtime start, so a session switched back to local stops reporting.
_REMOTE_WORKSPACES_MAX = 512
_remote_workspaces: dict[str, tuple[str, str]] = {}


def remember_remote_workspace(session_key: str | None, name: str, workdir: str) -> None:
    """Record that ``session_key``'s runtime works in ``workdir`` on ``name``."""
    if not session_key:
        return
    if session_key not in _remote_workspaces and len(_remote_workspaces) >= _REMOTE_WORKSPACES_MAX:
        _remote_workspaces.pop(next(iter(_remote_workspaces)))
    _remote_workspaces[session_key] = (name, workdir)


def forget_remote_workspace(session_key: str | None) -> None:
    if session_key:
        _remote_workspaces.pop(session_key, None)
        _remote_memory.pop(session_key, None)


# -- the remote workspace's own memory ------------------------------------------------
#
# The [RESOURCES] line measures the gateway. For a remote session the machine
# that runs its builds is the workspace, so the line also reports that machine's
# memory, from the last health reading (``session_browse.session_health``).
# In-memory, bounded, and refreshed in the background at most once a minute per
# session, so building a turn's context never waits on a remote round trip.
REMOTE_MEMORY_MAX_AGE_SECS = 60.0
_REMOTE_MEMORY_MAX = 512
#: session key -> (available bytes, total bytes, monotonic time read)
_remote_memory: dict[str, tuple[int, int, float]] = {}
_remote_memory_refreshing: set[str] = set()


def remember_remote_memory(session_key: str | None, available: int, total: int) -> None:
    """Cache one reading of ``session_key``'s remote workspace memory, in bytes."""
    if not session_key or available < 0 or total <= 0:
        return
    if session_key not in _remote_memory and len(_remote_memory) >= _REMOTE_MEMORY_MAX:
        _remote_memory.pop(next(iter(_remote_memory)))
    _remote_memory[session_key] = (int(available), int(total), time.monotonic())


def remote_memory_for(session_key: str | None) -> tuple[int, int, float] | None:
    """``(available bytes, total bytes, age seconds)`` of the last reading, or None."""
    entry = _remote_memory.get(session_key or "")
    if entry is None:
        return None
    return entry[0], entry[1], time.monotonic() - entry[2]


def refresh_remote_memory_soon(session_key: str | None) -> None:
    """Start a background health read when the cached reading is missing or stale.

    No-op without a running event loop, or while one is already in flight for
    this session. Never wakes stopped compute: ``session_health`` reports a
    stopped workspace without measuring it, and then nothing is cached.
    """
    import asyncio

    if not session_key or session_key in _remote_memory_refreshing:
        return
    cached = remote_memory_for(session_key)
    if cached is not None and cached[2] < REMOTE_MEMORY_MAX_AGE_SECS:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _remote_memory_refreshing.add(session_key)

    async def refresh() -> None:
        try:
            from kiro_crew.coder.session_browse import session_health

            await asyncio.wait_for(session_health(session_key), 30)
        except Exception:  # noqa: BLE001 -- a missing reading just omits the clause
            pass
        finally:
            _remote_memory_refreshing.discard(session_key)

    loop.create_task(refresh())


#: Sessions whose conversation outlived its workspace: the runtime that just
#: started got a NEW, empty resource (the old one was deleted, stopped with its
#: files, or retired). The next context build says so once, so the model does
#: not reason about files from earlier in the conversation that are gone.
_recreated: set[str] = set()


def mark_workspace_recreated(session_key: str | None) -> None:
    if session_key:
        if len(_recreated) >= _REMOTE_WORKSPACES_MAX:
            _recreated.clear()  # a leak must stay bounded; losing a note is harmless
        _recreated.add(session_key)


def take_workspace_recreated(session_key: str | None) -> bool:
    """True once after :func:`mark_workspace_recreated`, then False."""
    if not session_key or session_key not in _recreated:
        return False
    _recreated.discard(session_key)
    return True


def remote_workspace_for(session_key: str | None) -> tuple[str, str] | None:
    """``(environment name, guest workdir)`` while the session runs remotely."""
    return _remote_workspaces.get(session_key or "")


# -- local-only tools -----------------------------------------------------------------
#
# A remote session's engine runs on the gateway, so every Crew tool runs here too.
# Most act on gateway state that IS the session's (memory, crons, subagents,
# notifications). These act on THIS machine's files, browser, desktop or worktrees
# instead, which for a remote session is the wrong machine. They are unmounted from
# a remote session's agent (``AcpRuntime._remote_workspace_agents``) and refused in
# band by their servers as a second fence (``refuse_local_only_tool``).

#: ``kirocrew-core`` tools that act on the gateway machine itself.
LOCAL_ONLY_CORE_TOOLS: frozenset[str] = frozenset(
    {
        "set_project",  # retargets the slot at a directory on this machine
        "file_send",  # reads a file off this machine's disk
        "browser",  # drives this machine's browser panel
        "pod_up",  # boots a gateway from a worktree on this machine
        "pod_down",  # and deletes that pod's data directory here
    }
)
#: Whole servers that act on the gateway machine: computer use drives its desktop.
LOCAL_ONLY_SERVERS: frozenset[str] = frozenset({"kirocrew-computer"})


def session_runs_remotely(session_key: str | None) -> bool:
    """True when ``session_key`` is bound to a remote environment.

    Read from the durable choice record, so it holds in any process (the MCP
    servers run outside the gateway) and before a runtime attaches. An
    unreadable record answers True: refusing a local-only tool in a session that
    turns out to be local is recoverable, acting on the wrong machine is not.
    """
    if not session_key:
        return False
    from kiro_crew.coder.session_launch import SessionEnvironmentChoices
    from kiro_crew.config.paths import data_home

    choices = SessionEnvironmentChoices(data_home() / "coder" / "session-choices.json")
    try:
        return choices.resolve(session_key) is not None
    except SessionEnvironmentUnavailable:
        return True


def refuse_local_only_tool(session_key: str | None, server: str, tool: str) -> str | None:
    """The refusal text when ``server``/``tool`` would act on the wrong machine."""
    local_only = server in LOCAL_ONLY_SERVERS or (
        server == "kirocrew-core" and tool in LOCAL_ONLY_CORE_TOOLS
    )
    if not local_only or not session_runs_remotely(session_key):
        return None
    return (
        f"{tool} is not available in this session: it runs in a remote workspace, and "
        f"{tool} would act on the gateway machine instead. Use the workspace tools for "
        "files and commands in the remote workspace."
    )


def start_progress(session_key: str | None) -> dict[str, object] | None:
    """``{"stage", "stage_elapsed_s", "elapsed_s", "entered"}`` while a start is in flight.

    ``entered`` lists every stage this start has been in, in order. A stage can
    finish between two dashboard polls (connecting to a pre-installed pool
    workspace takes well under one), so the poll alone cannot tell a stage that
    ran from one that was skipped.
    """
    entry = _start_progress.get(session_key or "")
    if entry is None:
        return None
    stage, stage_since, began, entered, reconnect = entry
    now = time.monotonic()
    return {
        "stage": stage,
        "stage_elapsed_s": round(now - stage_since, 1),
        "elapsed_s": round(now - began, 1),
        "entered": list(entered),
        "reconnect": reconnect,
    }


class SessionEnvironmentPrefetchUnavailable(SessionEnvironmentUnavailable):
    """A speculative reconnect cannot safely reuse an existing environment."""


@dataclass(frozen=True)
class SessionEnvironmentConfiguration:
    """One public, selectable configuration owned by an environment provider."""

    id: str
    name: str

    def __post_init__(self) -> None:
        _validate_configuration(self.id)
        if (
            not isinstance(self.name, str)
            or not self.name
            or len(self.name) > _RESOURCE_NAME_MAX_CHARS
        ):
            raise ValueError("configuration name must be a bounded non-empty string")

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name}


@dataclass(frozen=True)
class SessionEnvironmentSelection:
    """Allocation intent selected before a managed resource exists."""

    provider: str
    configuration: str = ""

    def __post_init__(self) -> None:
        _validate_provider(self.provider)
        _validate_configuration(self.configuration)


@dataclass(frozen=True)
class SessionEnvironmentBinding:
    """Dashboard-safe durable identity, not authority to mutate a resource."""

    provider: str
    configuration: str = ""
    resource_name: str = ""

    def __post_init__(self) -> None:
        _validate_provider(self.provider)
        _validate_configuration(self.configuration)
        _validate_resource_name(self.resource_name)

    @property
    def selection(self) -> SessionEnvironmentSelection:
        return SessionEnvironmentSelection(self.provider, self.configuration)

    @classmethod
    def from_dict(cls, value: object) -> SessionEnvironmentBinding | None:
        if not isinstance(value, dict):
            return None
        provider = value.get("provider")
        configuration = value.get("configuration")
        resource_name = value.get("resource_name")
        if not isinstance(provider, str):
            return None
        if not isinstance(configuration, str):
            return None
        if not isinstance(resource_name, str):
            return None
        try:
            return cls(provider, configuration, resource_name)
        except ValueError:
            return None

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "configuration": self.configuration,
            "resource_name": self.resource_name,
        }


@dataclass(frozen=True)
class SessionEnvironmentMemoryHealth:
    """Provider-neutral memory pressure projected for dashboard display only."""

    available_gb: float
    total_gb: float
    used_percent: float
    pressure: str

    def __post_init__(self) -> None:
        values = (self.available_gb, self.total_gb, self.used_percent)
        if (
            any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in values
            )
            or self.available_gb < 0
            or self.total_gb <= 0
            or self.available_gb > self.total_gb
            or self.used_percent < 0
            or self.used_percent > 100
            or not isinstance(self.pressure, str)
            or self.pressure not in _MEMORY_PRESSURES
        ):
            raise ValueError("session environment memory health is invalid")

    def to_dict(self) -> dict[str, float | str]:
        return {
            "available_gb": self.available_gb,
            "total_gb": self.total_gb,
            "used_percent": self.used_percent,
            "pressure": self.pressure,
        }


@dataclass(frozen=True)
class SessionEnvironmentHealth:
    """Ephemeral provider-neutral health for one session-bound environment."""

    provider: str
    resource_name: str
    state: str
    memory: SessionEnvironmentMemoryHealth | None = None

    def __post_init__(self) -> None:
        _validate_provider(self.provider)
        if not isinstance(self.state, str) or self.state not in _ENVIRONMENT_STATES:
            raise ValueError("session environment state is invalid")
        _validate_resource_name(self.resource_name)

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "provider": self.provider,
            "resource_name": self.resource_name,
            "state": self.state,
        }
        if self.memory is not None:
            value["memory"] = self.memory.to_dict()
        return value


class SessionEnvironmentProvider(Protocol):
    """Lifecycle adapter for one managed session-environment system."""

    provider_id: str
    display_name: str
    icon: str

    def configurations(self) -> Sequence[SessionEnvironmentConfiguration]: ...

    def validate_configuration(self, configuration: str) -> str: ...

    def binding_for_session(self, session_key: str) -> SessionEnvironmentBinding | None: ...

    def create_session_host(self, session_key: str, configuration: str) -> Any: ...

    async def request_stop_for_session(self, session_key: str) -> str | None: ...

    async def stop_for_session(self, session_key: str) -> str | None: ...


class SessionEnvironmentLifecycleProvider:
    """Positive opt-in for gateway-owned provider maintenance.

    Catalog-shaped objects cannot acquire periodic mutation authority. Concrete
    adapters opt in explicitly and retain their own resource-ownership checks.
    """

    @property
    def lifecycle_interval_seconds(self) -> int | None:
        return None

    @property
    def runtime_warm_seconds(self) -> int | None:
        return None

    async def reconcile_lifecycle(self) -> None:
        return None

    async def shutdown_lifecycle(self) -> None:
        return None


class SessionEnvironmentPrefetchProvider:
    """Positive opt-in for reconnecting without allocating or starting compute.

    Adapters must prove the binding is running, then return a host that still
    refuses create/start if the resource stops between inspection and connection.
    """

    def can_prefetch_session(self, session_key: str) -> bool:
        return False

    def create_prefetch_session_host(self, session_key: str, configuration: str) -> Any:
        raise SessionEnvironmentPrefetchUnavailable(
            "session environment cannot be safely prefetched"
        )


class SessionEnvironmentHealthProvider:
    """Positive opt-in for bounded, on-demand environment health inspection."""

    async def health_for_session(self, session_key: str) -> SessionEnvironmentHealth:
        raise SessionEnvironmentUnavailable("session environment health is unavailable")


class SessionEnvironmentRegistry:
    """Lookup and public catalog for explicitly registered environment providers."""

    def __init__(
        self,
        providers: Sequence[SessionEnvironmentProvider],
        *,
        default_provider_id: str = "",
    ) -> None:
        self._providers: dict[str, SessionEnvironmentProvider] = {}
        for provider in providers:
            _validate_provider(provider.provider_id)
            if provider.provider_id in self._providers:
                raise ValueError(f"duplicate environment provider: {provider.provider_id}")
            self._providers[provider.provider_id] = provider
        if default_provider_id:
            _validate_provider(default_provider_id)
            if default_provider_id not in self._providers:
                raise ValueError("default environment provider is unavailable")
        self._default_provider_id = default_provider_id

    def get(self, provider_id: str) -> SessionEnvironmentProvider | None:
        return self._providers.get(provider_id)

    def require(self, provider_id: str) -> SessionEnvironmentProvider:
        provider = self.get(provider_id)
        if provider is None:
            raise SessionEnvironmentUnavailable(
                f"session environment provider {provider_id!r} is unavailable"
            )
        return provider

    def catalog(self) -> list[dict[str, object]]:
        return [
            {
                "id": provider.provider_id,
                "name": provider.display_name,
                "icon": provider.icon,
                "configurations": [item.to_dict() for item in provider.configurations()],
            }
            for provider in self._providers.values()
        ]

    def providers(self) -> tuple[SessionEnvironmentProvider, ...]:
        return tuple(self._providers.values())

    def supports_prefetch(
        self,
        session_key: str,
        selection: SessionEnvironmentSelection | None,
    ) -> bool:
        """Return whether a bound session can reconnect without new compute."""
        provider_id = selection.provider if selection is not None else self._default_provider_id
        provider = self.get(provider_id) if provider_id else None
        return isinstance(
            provider, SessionEnvironmentPrefetchProvider
        ) and provider.can_prefetch_session(session_key)

    def prefetch_host(
        self,
        session_key: str,
        selection: SessionEnvironmentSelection | None,
    ) -> Any:
        """Create a reconnect host only through an explicitly opted-in provider."""
        provider_id = selection.provider if selection is not None else self._default_provider_id
        provider = self.require(provider_id) if provider_id else None
        if not isinstance(provider, SessionEnvironmentPrefetchProvider):
            raise SessionEnvironmentPrefetchUnavailable(
                "session environment cannot be safely prefetched"
            )
        configuration = provider.validate_configuration(
            selection.configuration if selection is not None else ""
        )
        return provider.create_prefetch_session_host(session_key, configuration)


def _validate_provider(value: str) -> None:
    if not isinstance(value, str) or not _PROVIDER_ID_RE.fullmatch(value):
        raise ValueError("provider must be one lowercase safe identifier")


def _validate_configuration(value: str) -> None:
    if not isinstance(value, str) or (value and not _CONFIGURATION_ID_RE.fullmatch(value)):
        raise ValueError("configuration must be one safe identifier")


def _validate_resource_name(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > _RESOURCE_NAME_MAX_CHARS
        or any(char in value for char in "\r\n\x00")
    ):
        raise ValueError("resource_name must be one bounded display value")
