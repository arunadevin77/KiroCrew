"""What happens to a session's Coder workspace when the session is archived.

Archiving stops the workspace and stamps its registry row with
``deletion_due_at`` = now + ``session_environments.archived_retention_days``
(0-7). Reopening the session before then clears the stamp and starts the same
workspace, files intact. Once the stamp is due, :func:`sweep_due` (run by the
hot-pool keeper loop every minute) deletes the workspace and drops the row, so a
session reopened after that gets a fresh workspace.

Safety properties:

* Only a SESSION row is ever scheduled or swept. Pool rows are the keeper's.
* A deletion is decided under the registry lock (``begin_deletion``), the same
  critical section a resume's ``cancel_deletion`` takes, so a reopen and the
  sweep cannot both win. Once ``deleting``, a reopen is refused until it ends.
* The sweep proves owner, template and uuid before deleting, exactly like the
  keeper does, and never deletes a workspace its row did not mint.
* Everything here is best effort and never raises into the close path: a stop
  that fails leaves Coder's own auto-stop in charge, and the stamp still ages.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

from kiro_crew.coder.client import CoderClientError
from kiro_crew.coder.registry import DELETING, WorkspaceBinding

logger = logging.getLogger(__name__)

_RUNNING = "running"
#: Strong references to in-flight archive tasks: asyncio holds only weak ones.
_TASKS: "set[asyncio.Task[None]]" = set()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


async def _environments() -> Optional[Any]:
    """The live Coder environments, or None when Coder is not configured."""
    from kiro_crew.coder.session_launch import CoderSessionEnvironments
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.paths import data_home

    config = live.snapshot()
    if config is None:
        config = await asyncio.to_thread(KiroCrewConfig.load)
    section = config.session_environments
    if not getattr(section, "coder_url", ""):
        return None
    return CoderSessionEnvironments(section, await asyncio.to_thread(data_home))


#: Bound on the unsaved-work check that runs before an archive stops anything:
#: it must never hold up the stop for long, and a slow machine just skips it.
UNSAVED_CHECK_SECS = 20.0
#: ``notify(title, body)``: how the archive path tells the person, when it can.
Notify = Callable[[str, str], None]


def schedule_archived(session_keys: Iterable[str], notify: Optional[Notify] = None) -> None:
    """Fire-and-forget from the archive path: the close never waits on Coder."""
    keys = tuple(dict.fromkeys(k for k in session_keys if k))
    if not keys:
        return
    try:
        task = asyncio.ensure_future(_archive(keys, notify))
    except RuntimeError:
        return  # no running loop (a synchronous caller in a test)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def feed_notifier(state: Any) -> Optional[Notify]:
    """A :data:`Notify` that publishes to the dashboard's notification feed."""
    bus = getattr(state, "notification_bus", None)
    if bus is None:
        return None

    def notify(title: str, body: str) -> None:
        from kiro_crew.notifications.bus import NotificationPayload

        bus.push(
            NotificationPayload(
                source="system",
                channel="system.agent",
                kind="agent",
                title=title,
                body=body,
                group_key="session-environment-unsaved",
            )
        )

    return notify


async def warn_unsaved_work(session_keys: tuple[str, ...], notify: Optional[Notify]) -> None:
    """Tell the person when an archived session's workspace holds unsaved work.

    Checked before anything is stopped, on running workspaces only (the check
    never wakes stopped compute), and bounded: files, uncommitted changes and
    unpushed commits on the remote machine exist nowhere else, and they go when
    its retention runs out (a Fargate task's with the stop itself). Never raises.
    """
    if notify is None:
        return
    from kiro_crew.coder.session_browse import unsaved_work
    from kiro_crew.session_environment import session_runs_remotely

    for key in session_keys:
        try:
            if not await asyncio.to_thread(session_runs_remotely, key):
                continue
            summary = await asyncio.wait_for(unsaved_work(key), UNSAVED_CHECK_SECS)
        except Exception:  # noqa: BLE001 -- the archive proceeds regardless
            logger.debug("Unsaved-work check failed for %s", key, exc_info=True)
            continue
        if summary:
            try:
                notify(
                    "Archived session has unsaved remote work",
                    f"Its workspace holds {summary}, which exists only on that machine. "
                    "Reopen the session and commit or push before its retention runs out. "
                    "An AWS Fargate workspace is deleted when the session is archived.",
                )
            except Exception:  # noqa: BLE001 -- a failed notice never fails the archive
                logger.warning("Could not publish the unsaved-work notice", exc_info=True)


async def _archive(session_keys: tuple[str, ...], notify: Optional[Notify] = None) -> None:
    await warn_unsaved_work(session_keys, notify)
    try:
        await _archive_fargate(session_keys)
    except Exception:  # noqa: BLE001 -- never surfaces into a close that already succeeded
        logger.warning("Archived session task: could not stop it", exc_info=True)
    try:
        await _archive_sprites(session_keys)
    except Exception:  # noqa: BLE001 -- never surfaces into a close that already succeeded
        logger.warning("Archived session sprite: could not schedule its deletion", exc_info=True)
    try:
        await _archive_boxd(session_keys)
    except Exception:  # noqa: BLE001 -- never surfaces into a close that already succeeded
        logger.warning("Archived session boxd machine: could not schedule it", exc_info=True)
    try:
        environments = await _environments()
        if environments is None:
            return
        for key in session_keys:
            await archive_session(environments, key)
    except Exception:  # noqa: BLE001 -- never surfaces into a close that already succeeded
        logger.warning("Archived session workspace: could not schedule its release", exc_info=True)


async def _fargate_environments() -> Any:
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.paths import data_home
    from kiro_crew.fargate_session.launch import FargateSessionEnvironments

    config = live.snapshot()
    if config is None:
        config = await asyncio.to_thread(KiroCrewConfig.load)
    return FargateSessionEnvironments(
        config.session_environments, await asyncio.to_thread(data_home)
    )


async def _archive_fargate(session_keys: tuple[str, ...]) -> None:
    """Stop an archived session's Fargate task now: its workspace is ephemeral.

    A stopped Fargate task is deleted with its files, so there is no retention
    window to honour; keeping it running would only bill for nothing.
    """
    environments = await _fargate_environments()
    for key in session_keys:
        await environments.archive_session(key)


def _retention_days(config: Any) -> int:
    from kiro_crew.config.session_environments import (
        ARCHIVED_RETENTION_DAYS_DEFAULT,
        ARCHIVED_RETENTION_DAYS_MAX,
    )

    days = getattr(config, "archived_retention_days", ARCHIVED_RETENTION_DAYS_DEFAULT)
    if (
        isinstance(days, bool)
        or not isinstance(days, int)
        or not 0 <= days <= ARCHIVED_RETENTION_DAYS_MAX
    ):
        return ARCHIVED_RETENTION_DAYS_DEFAULT
    return days


async def _sprites_environments() -> Any:
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.paths import data_home
    from kiro_crew.sprites_session.launch import SpritesSessionEnvironments

    config = live.snapshot()
    if config is None:
        config = await asyncio.to_thread(KiroCrewConfig.load)
    return SpritesSessionEnvironments(
        config.session_environments, await asyncio.to_thread(data_home)
    )


async def _archive_sprites(session_keys: tuple[str, ...]) -> None:
    """Schedule an archived session's sprite for deletion, on the same retention.

    A cold sprite bills storage only, so it is kept like a stopped Coder
    workspace; at 0 days it is deleted now.
    """
    environments = await _sprites_environments()
    days = _retention_days(environments._config)
    for key in session_keys:
        await environments.archive_session(key, days)


async def _boxd_environments() -> Any:
    from kiro_crew.boxd_session.launch import BoxdSessionEnvironments
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.paths import data_home

    config = live.snapshot()
    if config is None:
        config = await asyncio.to_thread(KiroCrewConfig.load)
    return BoxdSessionEnvironments(config.session_environments, await asyncio.to_thread(data_home))


async def _archive_boxd(session_keys: tuple[str, ...]) -> None:
    """Hibernate an archived session's boxd machine and schedule its deletion."""
    environments = await _boxd_environments()
    days = _retention_days(environments._config)
    for key in session_keys:
        await environments.archive_session(key, days)


async def sweep_due_boxd() -> int:
    """Delete archived sessions' boxd machines whose retention ran out (registry-only when none)."""
    environments = await _boxd_environments()
    if not await asyncio.to_thread(environments.registry.due, _now()):
        return 0  # no CLI call, so a gateway without boxd pays nothing
    return int(await environments.sweep_due())


async def sweep_due_sprites() -> int:
    """Delete archived sessions' sprites whose retention ran out (registry-only when none)."""
    return int(await (await _sprites_environments()).sweep_due())


#: Ceiling on the whole shutdown stop: it runs beside the gateway's own graceful
#: shutdown and must never hold the exit. Whatever it does not reach in time is
#: stopped by the guest itself (the worker's orphan timeout, the Fargate
#: bootstrap's idle exit, Coder's deadline, a sprite or boxd machine going idle).
SHUTDOWN_STOP_SECS = 20.0


async def stop_on_shutdown_enabled() -> bool:
    """``session_environments.stop_on_shutdown`` (default True); True when unreadable."""
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        config = live.snapshot()
        if config is None:
            config = await asyncio.to_thread(KiroCrewConfig.load)
        return bool(getattr(config.session_environments, "stop_on_shutdown", True))
    except Exception:  # noqa: BLE001 -- an unreadable config keeps the safe default
        return True


async def stop_for_shutdown() -> dict[str, int]:
    """Stop every remote environment this gateway owns, because it is going away.

    Nothing is scheduled for deletion and no registry row is dropped except a
    Fargate task's (a stopped task is gone with its files, exactly as on archive),
    so a session reopened after the next start resumes the same Coder workspace or
    boxd machine. Pool workspaces stop too; the keeper starts them again on boot.
    Sprites need nothing: one goes cold by itself once its connections close.
    Best effort per resource, and never raises.
    """
    counts = {"coder": 0, "fargate": 0, "boxd": 0}

    async def coder() -> None:
        environments = await _environments()
        if environments is None:
            return
        rows = await asyncio.to_thread(environments.registry.list_bindings)
        if not rows:
            return
        client = environments._client()
        for row in rows:
            if row.state == DELETING:
                continue
            try:
                ws = await client.get_workspace(row.workspace_name)
                if (
                    ws is not None
                    and ws.status == _RUNNING
                    and (not row.workspace_uuid or ws.uuid == row.workspace_uuid)
                ):
                    await client.stop_workspace(row.workspace_name)
                    counts["coder"] += 1
            except CoderClientError as exc:
                logger.info("Shutdown: Coder workspace stop failed (%s); auto-stop applies", exc)

    async def fargate() -> None:
        environments = await _fargate_environments()
        for row in await asyncio.to_thread(environments.registry.all):
            if await environments.archive_session(row.session_key, "the gateway shut down"):
                counts["fargate"] += 1

    async def boxd() -> None:
        environments = await _boxd_environments()
        rows = await asyncio.to_thread(environments.registry.all)
        if not rows:
            return  # no CLI call, so a gateway without boxd pays nothing
        cli = environments.cli()
        for row in rows:
            try:
                await cli.hibernate(row.machine)
                counts["boxd"] += 1
            except Exception as exc:  # noqa: BLE001 -- boxd idles it on its own
                logger.info("Shutdown: boxd hibernate failed (%s); boxd idles it", exc)

    async def guarded(name: str, step: Any) -> None:
        try:
            await step()
        except Exception:  # noqa: BLE001 -- shutdown must not fail on one provider
            logger.warning("Shutdown: could not stop %s session environments", name, exc_info=True)

    await asyncio.gather(
        guarded("Coder", coder), guarded("Fargate", fargate), guarded("boxd", boxd)
    )
    if any(counts.values()):
        logger.info("Shutdown: stopped session environments %s", counts)
    return counts


async def archive_session(environments: Any, session_key: str) -> Optional[WorkspaceBinding]:
    """Stamp ``session_key``'s workspace for deletion and stop it. None if it has none."""
    from kiro_crew.config.session_environments import (
        ARCHIVED_RETENTION_DAYS_DEFAULT,
        ARCHIVED_RETENTION_DAYS_MAX,
    )

    # A config object without the field (an older shape) keeps the default,
    # never 0: a missing setting must not mean "delete immediately".
    days = getattr(environments._config, "archived_retention_days", ARCHIVED_RETENTION_DAYS_DEFAULT)
    if (
        isinstance(days, bool)
        or not isinstance(days, int)
        or not 0 <= days <= ARCHIVED_RETENTION_DAYS_MAX
    ):
        days = ARCHIVED_RETENTION_DAYS_DEFAULT
    due = _now() + timedelta(days=days)
    binding = await asyncio.to_thread(
        environments.registry.schedule_deletion, session_key, _stamp(due)
    )
    if binding is None:
        return None
    if days == 0:
        # Deleted on the keeper's next pass, which this wakes; no stop first.
        from kiro_crew.coder.hot_pool import request_refill

        request_refill()
        return binding
    try:
        workspace = await environments._client().get_workspace(binding.workspace_name)
        if (
            workspace is not None
            and workspace.status == _RUNNING
            and (not binding.workspace_uuid or workspace.uuid == binding.workspace_uuid)
        ):
            await environments._client().stop_workspace(binding.workspace_name)
    except CoderClientError as exc:
        # Coder's own auto-stop still applies; the deletion stamp stands.
        logger.info("Archived session workspace: stop failed (%s); auto-stop applies", exc)
    return binding


async def sweep_due(environments: Any, *, now: Optional[datetime] = None) -> int:
    """Delete every workspace whose retention ran out. Returns how many went."""
    moment = now or _now()
    rows = await asyncio.to_thread(environments.registry.due_for_deletion, moment)
    if not rows:
        return 0
    client = environments._client()
    owner, owner_id = await client.current_user()
    deleted = 0
    for row in rows:
        claimed = await asyncio.to_thread(
            environments.registry.begin_deletion, row.binding_id, moment
        )
        if claimed is None:
            continue  # reopened in the meantime
        try:
            workspace = await client.get_workspace(claimed.workspace_name)
        except CoderClientError as exc:
            logger.warning("Archived session workspace: lookup failed (%s); retrying later", exc)
            continue
        if workspace is not None:
            if (
                workspace.owner not in (owner, owner_id)
                or workspace.template != claimed.template
                or (claimed.workspace_uuid and workspace.uuid != claimed.workspace_uuid)
            ):
                # The name now names a workspace this row did not mint: forget the
                # row, never act on that workspace.
                logger.warning("Archived session workspace changed identity; forgetting it")
            else:
                try:
                    await client.delete_workspace(claimed.workspace_name)
                except CoderClientError as exc:
                    # Stays ``deleting``: the next pass retries, and a reopen is
                    # refused until it is gone.
                    logger.warning("Archived session workspace: delete failed (%s)", exc)
                    continue
        await asyncio.to_thread(environments.registry.remove_deleted, claimed.binding_id)
        deleted += 1
    return deleted


def resume_check(registry: Any, session_key: str) -> None:
    """Cancel a pending deletion for a reopened session, or refuse mid-delete."""
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    binding = registry.get_by_session(session_key)
    if binding is None:
        return
    if binding.state == DELETING or (
        binding.deletion_due_at and registry.cancel_deletion(binding.binding_id) is None
    ):
        raise SessionEnvironmentUnavailable(
            "this archived session's workspace is being deleted; try again in a minute "
            "and it will start on a fresh one"
        )
