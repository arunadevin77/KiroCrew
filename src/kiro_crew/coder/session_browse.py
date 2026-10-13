"""Dashboard access to a remote session's workspace: files, git, terminal.

A remote session's files live in its guest workspace, so the dashboard's file,
git and terminal views cannot use the gateway's disk or shell. These ask the
guest instead, through the gateway's ``coder ssh`` (never autostarting a stopped
workspace):

* :func:`browse_session` runs ``workspace_tools.py --browse``: ``list``, ``read``
  and read-only git views, with the agent tools' root confinement and caps. It
  has no write or run operation at all.
* :func:`write_session_file` runs the separate ``--write`` mode, the one mutation,
  which refuses a save whose expected text no longer matches the file.
* :func:`remote_terminal_argv` is the argv for an interactive shell there.

Requests travel on stdin as JSON, so a path or file body never reaches a shell.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from dataclasses import dataclass
from typing import Any

from kiro_crew.coder.remote_worker import (
    WORKER_DIR,
    CoderShell,
    RemoteWorkerError,
    RemoteWorkerLaunch,
    _scrubbed_env,
    run_collect,
)
from kiro_crew.session_environment import SessionEnvironmentUnavailable

#: The guest caps a read at 256 KB of text; JSON escaping can roughly double it.
_BROWSE_OUTPUT_MAX = 2 * 1024 * 1024
_BROWSE_TIMEOUT = 45.0
_MAX_PATH = 4096
#: Largest file body the dashboard editor may save (the guest allows more).
MAX_WRITE_BYTES = 1024 * 1024
READ_OPS = ("list", "read", "git_status", "git_log", "git_diff", "stats")


class NotRemote(LookupError):
    """The session runs on this machine: there is no remote workspace."""


class WorkspaceNotRunning(SessionEnvironmentUnavailable):
    """The session's workspace is stopped (or not created yet)."""


@dataclass(frozen=True)
class _Target:
    shell: Any  # CoderShell, or a FargateShell for a Fargate task
    workdir: str
    binding_id: str
    home: Any


async def _resolve(session_key: str) -> _Target:
    from kiro_crew.coder.registry import WorkspaceBindingRegistry
    from kiro_crew.coder.session_launch import (
        CoderSessionEnvironments,
        SessionEnvironmentChoices,
        resolve_coder_bin,
    )
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.paths import data_home

    home = await asyncio.to_thread(data_home)
    choices = SessionEnvironmentChoices(home / "coder" / "session-choices.json")
    resolved = await asyncio.to_thread(choices.resolve, session_key)
    if resolved is None:
        raise NotRemote(session_key)
    profile_id, owner = resolved
    config = live.snapshot()
    if config is None:
        config = await asyncio.to_thread(KiroCrewConfig.load)
    section = config.session_environments
    from kiro_crew.coder.session_launch import kiro_cloud_profile

    if kiro_cloud_profile(section, profile_id) is not None:
        # The sandbox belongs to Kiro's cloud and is reached only by its agent.
        raise WorkspaceNotRunning("a Kiro Cloud session's files live in Kiro's cloud sandbox")
    fargate_ids = {
        p.get("id")
        for p in (getattr(section, "fargate_profiles", None) or [])
        if isinstance(p, dict)
    }
    if profile_id in fargate_ids:
        return await _resolve_fargate(section, home, owner, profile_id)
    from kiro_crew.coder.session_launch import sprites_profile_ids

    if profile_id in sprites_profile_ids(section):
        return await _resolve_sprites(section, home, owner, profile_id)
    from kiro_crew.coder.session_launch import boxd_profile_ids

    if profile_id in boxd_profile_ids(section):
        return await _resolve_boxd(section, home, owner, profile_id)
    profile = CoderSessionEnvironments(section, home).profile(profile_id)
    registry = WorkspaceBindingRegistry(home / "coder" / "session-envs.json")
    binding = await asyncio.to_thread(registry.get_by_session, owner)
    if binding is None or not binding.workspace_uuid:
        raise WorkspaceNotRunning("this session's workspace has not been created yet")
    return _Target(
        CoderShell(resolve_coder_bin(), binding.workspace_name),
        profile.workdir,
        binding.binding_id,
        home,
    )


async def _resolve_fargate(section: Any, home: Any, owner: str, profile_id: str) -> _Target:
    """The session's Fargate task, reached through its bootstrap. No AWS call."""
    from kiro_crew.fargate_session.launch import FargateSessionEnvironments
    from kiro_crew.fargate_session.shell import FargateShell

    envs = FargateSessionEnvironments(section, home)
    profile = envs.profile(profile_id)
    row = await asyncio.to_thread(envs.registry.get, owner)
    secret = envs._load_secret(row.task_id) if row is not None and row.runtime_id else None
    if row is None or secret is None:
        raise WorkspaceNotRunning("this session's task is not running")
    shell = FargateShell(envs._forward(profile, row.task).connect, secret)
    shell.workspace = f"task {row.task_id[:12]}"
    return _Target(shell, str(profile.get("workdir") or ""), f"fg-{row.task_id[:24]}", home)


async def _resolve_sprites(section: Any, home: Any, owner: str, profile_id: str) -> _Target:
    """The session's sprite, through the Sprites API. Never creates one."""
    from kiro_crew.sprites_session.launch import SpritesSessionEnvironments

    envs = SpritesSessionEnvironments(section, home)
    profile = envs.profile(profile_id)
    shell = await envs.shell_for(owner, profile_id)
    if shell is None:
        raise WorkspaceNotRunning("this session's sprite has not been created yet")
    # The terminal bridge reads the token itself, by this name, from the vault.
    shell.token_secret = str(profile.get("token_secret") or "")
    return _Target(shell, str(profile.get("workdir") or ""), f"sp-{shell.sprite[-16:]}", home)


def _has_command_argv(shell: Any) -> bool:
    """True for a shell reached by a local command (``coder ssh``, ``boxd machine exec``)."""
    from kiro_crew.boxd_session.shell import BoxdShell

    return isinstance(shell, (CoderShell, BoxdShell))


async def _resolve_boxd(section: Any, home: Any, owner: str, profile_id: str) -> _Target:
    """The session's boxd machine. Never creates one."""
    from kiro_crew.boxd_session.launch import BoxdSessionEnvironments

    envs = BoxdSessionEnvironments(section, home)
    profile = envs.profile(profile_id)
    shell = await envs.shell_for(owner)
    if shell is None:
        raise WorkspaceNotRunning("this session's boxd machine has not been created yet")
    return _Target(shell, str(profile.get("workdir") or ""), f"bx-{shell.machine[3:]}", home)


async def _ask(target: _Target, mode: str, request: dict[str, Any]) -> dict[str, Any]:
    # The workdir is operator config, quoted; the request travels on stdin.
    command = (
        f'cd "{WORKER_DIR}" && exec python3 workspace_tools.py '
        f"--root {shlex.quote(target.workdir)} --{mode}"
    )
    payload = json.dumps(request).encode("utf-8")

    async def once() -> bytes:
        if not _has_command_argv(target.shell):
            # A shell with no dial command (a Fargate task's bootstrap) runs it itself.
            return await asyncio.wait_for(target.shell.run(command, payload), _BROWSE_TIMEOUT)
        return await run_collect(
            target.shell.argv(command),
            payload,
            _scrubbed_env(),
            max_output=_BROWSE_OUTPUT_MAX,
            timeout=_BROWSE_TIMEOUT,
        )

    try:
        try:
            out = await once()
        except RemoteWorkerError as exc:
            # A guest whose files predate this mode (or that no runtime has
            # prepared since): install the current files once and retry.
            if f"--{mode}" not in str(exc) and "No such file" not in str(exc):
                raise
            await RemoteWorkerLaunch(target.binding_id, target.shell, target.home).install()
            out = await once()
    except asyncio.TimeoutError as exc:
        raise SessionEnvironmentUnavailable("the remote workspace did not answer") from exc
    except RemoteWorkerError as exc:
        if "must be started" in str(exc) or "not running" in str(exc):
            raise WorkspaceNotRunning("this session's workspace is stopped") from exc
        raise SessionEnvironmentUnavailable(f"could not reach the remote workspace: {exc}") from exc
    try:
        result = json.loads(out)
    except ValueError as exc:
        raise SessionEnvironmentUnavailable("the remote workspace sent an invalid reply") from exc
    if not isinstance(result, dict):
        raise SessionEnvironmentUnavailable("the remote workspace sent an invalid reply")
    return result


def _check_path(path: str) -> str:
    if not isinstance(path, str) or len(path) > _MAX_PATH or "\x00" in path:
        raise ValueError("path is invalid")
    return path or "."


async def browse_session(session_key: str, op: str, path: str) -> dict[str, Any]:
    """A read-only request: ``list``/``read`` a path, or a git view of the root."""
    if op not in READ_OPS:
        raise ValueError("op must be one of " + ", ".join(READ_OPS))
    return await _ask(await _resolve(session_key), "browse", {"op": op, "path": _check_path(path)})


async def write_session_file(
    session_key: str, path: str, content: str, expected: str | None
) -> dict[str, Any]:
    """Save ``content`` to ``path``; refused when the file no longer holds ``expected``."""
    path = _check_path(path)
    if path == ".":
        raise ValueError("a file path is required")
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        raise ValueError(f"content must be text of at most {MAX_WRITE_BYTES} bytes")
    request: dict[str, Any] = {"path": path, "content": content}
    if isinstance(expected, str):
        request["expected"] = expected
    return await _ask(await _resolve(session_key), "write", request)


async def session_health(session_key: str) -> dict[str, Any]:
    """Workspace identity, state and machine metrics for the environment card.

    Never starts compute: a stopped workspace reports ``state: stopped`` and no
    metrics, rather than being woken to be measured.
    """
    target = await _resolve(session_key)
    base: dict[str, Any] = {"workspace": target.shell.workspace, "workdir": target.workdir}
    sprites_client = getattr(target.shell, "client", None)
    if sprites_client is not None and hasattr(sprites_client, "get_sprite"):
        # Any exec wakes a cold sprite (and bills it), so ask its status first.
        try:
            info = await sprites_client.get_sprite(target.shell.sprite)
        except SessionEnvironmentUnavailable as exc:
            return {**base, "state": "unavailable", "error": str(exc)[:300]}
        if info is None or info.get("status") != "running":
            return {**base, "state": "stopped"}
    from kiro_crew.boxd_session.shell import BoxdShell

    if isinstance(target.shell, BoxdShell):
        # An exec wakes a hibernated machine (and bills it), so ask its status first.
        from kiro_crew.boxd_session.cli import BoxdCLI

        try:
            machine = await BoxdCLI(target.shell.boxd_bin).get(target.shell.machine)
        except SessionEnvironmentUnavailable as exc:
            return {**base, "state": "unavailable", "error": str(exc)[:300]}
        if machine is None or machine.get("status") != "running":
            return {**base, "state": "stopped"}
    try:
        stats = await _ask(target, "browse", {"op": "stats", "path": "."})
    except WorkspaceNotRunning:
        return {**base, "state": "stopped"}
    except SessionEnvironmentUnavailable as exc:
        return {**base, "state": "unavailable", "error": str(exc)[:300]}
    if "error" in stats:
        return {**base, "state": "unavailable", "error": str(stats["error"])[:300]}
    memory = stats.get("memory") if isinstance(stats, dict) else None
    if isinstance(memory, dict):
        # Feeds the [RESOURCES] line's workspace clause for this session.
        from kiro_crew.session_environment import remember_remote_memory

        try:
            remember_remote_memory(
                session_key, int(memory["available_bytes"]), int(memory["total_bytes"])
            )
        except (KeyError, TypeError, ValueError):
            pass
    return {**base, "state": "running", "stats": stats}


_AHEAD_RE = re.compile(r"\[(?:[^\]]*\b)?ahead (\d+)")


def summarize_unsaved(status_output: str) -> str | None:
    """What ``git status --porcelain=v1 --branch`` says would be lost, or None.

    Counts changed and untracked paths, commits ahead of the upstream, and a
    branch with no upstream at all (every commit on it exists only here).
    """
    lines = [line for line in status_output.splitlines() if line.strip()]
    if not lines:
        return None
    head, changes = (lines[0], lines[1:]) if lines[0].startswith("## ") else ("", lines)
    parts: list[str] = []
    if changes:
        parts.append(f"{len(changes)} uncommitted file{'s' if len(changes) != 1 else ''}")
    ahead = _AHEAD_RE.search(head)
    if ahead and int(ahead.group(1)) > 0:
        count = int(ahead.group(1))
        parts.append(f"{count} unpushed commit{'s' if count != 1 else ''}")
    elif head and "..." not in head and not head.startswith(("## No commits yet", "## HEAD")):
        parts.append(f"branch {head[3:].strip()} was never pushed")
    return ", ".join(parts) or None


async def unsaved_work(session_key: str) -> str | None:
    """Uncommitted or unpushed work in a RUNNING session workspace, or None.

    Reads health first, which never wakes stopped compute: a stopped workspace
    is not measured, and None (nothing known) is the answer.
    """
    health = await session_health(session_key)
    if health.get("state") != "running":
        return None
    result = await _ask(await _resolve(session_key), "browse", {"op": "git_status", "path": "."})
    if not isinstance(result, dict) or not result.get("git"):
        return None
    return summarize_unsaved(str(result.get("output") or ""))


async def remote_terminal_argv(session_key: str) -> list[str]:
    """argv for an interactive login shell in the session's remote workspace.

    ``coder ssh`` with no command opens a login shell in the agent's directory,
    which the session template sets to the profile's workdir.
    """
    target = await _resolve(session_key)
    if _has_command_argv(target.shell):
        # Coder never autostarts here; boxd's connect wakes a hibernated machine,
        # which opening a terminal is an explicit request for.
        return list(target.shell.interactive_argv())
    from kiro_crew.sprites_session.shell import SpritesShell

    if isinstance(target.shell, SpritesShell):
        from kiro_crew import platform_compat
        from kiro_crew.sprites_session.terminal import bridge_argv

        if not platform_compat.IS_POSIX:
            raise SessionEnvironmentUnavailable("a Sprites terminal needs a macOS or Linux gateway")
        # Opening a terminal is an explicit action, so it may wake a cold sprite.
        return bridge_argv(
            target.shell.sprite, getattr(target.shell, "token_secret", ""), target.workdir
        )
    raise SessionEnvironmentUnavailable(
        "a terminal is not available for a Fargate session yet; its commands run "
        "through the agent's workspace tools"
    )
