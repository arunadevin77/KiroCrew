"""Keep ready Coder workspaces waiting so a new remote session skips provisioning.

A profile's ``hot_pool`` is how many workspaces the gateway keeps created,
running and owned by NO session. A session that starts on that profile claims
one (:meth:`WorkspaceBindingRegistry.claim_pool`) instead of creating its own,
and the keeper below creates a replacement in the background. So with
``hot_pool: 1`` there is always one MicroVM waiting beside the ones in use.

Safety properties:

* Every pool workspace is minted in the ownership registry BEFORE it exists,
  exactly like a session's, so a crash mid-create leaves a countable row and
  never an orphan nothing can name. The keeper only ever acts on rows in the
  reserved pool namespace, and only while they are still there: a row a session
  claimed is that session's and the keeper never touches it again.
* A claimed workspace never returns to the pool. A workspace one session used is
  never handed to another.
* Before acting on a workspace the keeper proves it is the one its row minted
  (owner, template, uuid). A name that now points at someone else's workspace is
  forgotten, never started, extended or deleted.
* Retiring a pool workspace (pool shrunk, profile removed, template drifted) is
  a two-step move: the row is flipped to ``retiring`` under the registry lock
  FIRST, which a claim can never pick, and only then is the workspace deleted.
  A session that claimed it in between wins and the delete never runs.
* Ready workspaces carry the normal auto-stop. The keeper renews the deadline
  while the gateway is up, so if the gateway goes away the pool stops billing on
  its own within ``stop_after_minutes``.
* Nothing runs, and no Coder command is issued, while every ``hot_pool`` is 0
  and the registry holds no pool rows.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from kiro_crew.coder.client import CoderClient, CoderClientError, CoderWorkspace
from kiro_crew.coder.registry import (
    POOL_READY,
    POOL_SESSION_PREFIX,
    WorkspaceBinding,
    WorkspaceRegistryCorrupt,
)
from kiro_crew.config.session_environments import HOT_POOL_MAX

logger = logging.getLogger(__name__)

#: Most pool workspaces across every profile, whatever the profiles ask for.
POOL_TOTAL_MAX = 10
#: How often the keeper looks at the pool when nothing wakes it sooner.
KEEPER_INTERVAL_SECS = 60.0
#: Boot work stays off the gateway's startup path.
KEEPER_INITIAL_DELAY_SECS = 30.0
#: Renew a ready workspace's auto-stop deadline at most this often.
EXTEND_EVERY_SECS = 10 * 60.0

_RUNNING = "running"
_STARTABLE = frozenset({"stopped", "failed", "canceled"})

#: Workspace uuid -> digest of the worker files the keeper installed in it.
#: In memory on purpose: the keeper and every claimant run in the gateway
#: process, a gateway restart only costs one install per workspace, and a
#: claimant that trusts a stale entry reinstalls when its worker fails to start.
_installed: dict[str, str] = {}
_INSTALLED_MAX = 64


def take_installed_digest(workspace_uuid: str) -> str:
    """The pre-installed worker digest for a claimed workspace, handed over once."""
    return _installed.pop(workspace_uuid, "") if workspace_uuid else ""


_RETIRING = "retiring"
_STARTING = "starting"


def _profile_of(binding: WorkspaceBinding) -> str:
    rest = binding.session_key[len(POOL_SESSION_PREFIX) :]
    return rest.split(":", 1)[0]


def pool_targets(profiles: list[Any]) -> dict[str, int]:
    """Profile id -> ready workspaces wanted, clamped per profile and in total."""
    targets: dict[str, int] = {}
    budget = POOL_TOTAL_MAX
    for profile in profiles:
        wanted = getattr(profile, "hot_pool", 0)
        if isinstance(wanted, bool) or not isinstance(wanted, int) or wanted <= 0:
            continue
        granted = min(wanted, HOT_POOL_MAX, budget)
        if granted <= 0:
            logger.warning(
                "Coder hot pool: %d-workspace total reached; profile %r gets none",
                POOL_TOTAL_MAX,
                profile.id,
            )
            continue
        targets[profile.id] = granted
        budget -= granted
    return targets


@dataclass
class PoolPass:
    """What one reconcile pass did. Counts only, no names or identifiers."""

    ready: dict[str, int] = field(default_factory=dict)
    created: int = 0
    started: int = 0
    retired: int = 0
    forgotten: int = 0
    #: Archived sessions' workspaces deleted because their retention ran out.
    archived_deleted: int = 0


class CoderHotPool:
    """Reconcile the registry's pool rows against each profile's ``hot_pool``.

    ``environments`` is a :class:`~kiro_crew.coder.session_launch.CoderSessionEnvironments`
    (typed loosely to avoid an import cycle): its registry, Coder client,
    deployment URL, profiles and auto-stop are reused, so a pool workspace is
    created exactly the way a session's is.
    """

    def __init__(
        self,
        environments: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
        install_worker: Optional[Callable[[str], Awaitable[str]]] = None,
    ) -> None:
        self.env = environments
        self._clock = clock
        self._extended_at: dict[str, float] = {}
        self._install_worker = install_worker or self._install_over_coder_ssh

    async def _install_over_coder_ssh(self, workspace_name: str) -> str:
        from kiro_crew.coder.remote_worker import CoderShell, install_worker

        return await install_worker(CoderShell(self.env._bin(), workspace_name))

    async def _preinstall(self, row: WorkspaceBinding, ws: CoderWorkspace) -> None:
        """Put this build's worker files in a waiting workspace, off the start path.

        A session that claims it then skips that ``coder ssh``. Best effort: a
        failure only means the claimant installs, as it would without a pool.
        """
        from kiro_crew.coder.remote_worker import worker_digest

        if _installed.get(ws.uuid) == worker_digest():
            return
        try:
            digest = await self._install_worker(row.workspace_name)
            if len(_installed) >= _INSTALLED_MAX:
                _installed.pop(next(iter(_installed)))
            _installed[ws.uuid] = digest
        except Exception as exc:  # noqa: BLE001 -- any failure falls back to the claimant
            _installed.pop(ws.uuid, None)
            logger.debug("Coder hot pool: could not pre-install the worker: %s", exc)

    @property
    def registry(self) -> Any:
        return self.env.registry

    def _profiles(self) -> list[Any]:
        return list(getattr(self.env._config, "profiles", []) or [])

    async def reconcile(self) -> PoolPass:
        result = PoolPass()
        profiles = {p.id: p for p in self._profiles()}
        targets = pool_targets(list(profiles.values()))
        rows = self.registry.pool_rows()
        if not targets and not rows:
            return result
        client: CoderClient = self.env._client()
        owner, owner_id = await client.current_user()
        workspaces = {w.name: w for w in await client.list_workspaces()}

        by_profile: dict[str, list[WorkspaceBinding]] = {}
        for row in rows:
            by_profile.setdefault(_profile_of(row), []).append(row)
        for profile_id in sorted(set(by_profile) | set(targets)):
            await self._reconcile_profile(
                client,
                profile_id,
                profiles.get(profile_id),
                targets.get(profile_id, 0),
                by_profile.get(profile_id, []),
                workspaces,
                (owner, owner_id),
                result,
            )
        return result

    @staticmethod
    def _owned(row: WorkspaceBinding, ws: CoderWorkspace, identity: tuple[str, str]) -> bool:
        if ws.owner not in identity:
            return False
        if row.template != ws.template:
            return False
        return not row.workspace_uuid or row.workspace_uuid == ws.uuid

    async def _reconcile_profile(
        self,
        client: CoderClient,
        profile_id: str,
        profile: Optional[Any],
        target: int,
        rows: list[WorkspaceBinding],
        workspaces: dict[str, CoderWorkspace],
        identity: tuple[str, str],
        result: PoolPass,
    ) -> None:
        live: list[tuple[WorkspaceBinding, CoderWorkspace]] = []
        for row in rows:
            ws = workspaces.get(row.workspace_name)
            if ws is None:
                # Deleted outside Kiro Crew, or a create that never landed. Either
                # way there is no compute behind the row; the deficit refills it.
                if self.registry.remove_pool(row.binding_id):
                    result.forgotten += 1
                self._extended_at.pop(row.binding_id, None)
                _installed.pop(row.workspace_uuid, None)
                continue
            if not self._owned(row, ws, identity):
                # The name now names a workspace this row did not mint. Forget
                # the row and never act on that workspace.
                logger.warning("Coder hot pool: a pool workspace changed identity; forgetting it")
                if self.registry.remove_pool(row.binding_id):
                    result.forgotten += 1
                continue
            if (
                row.state == _RETIRING
                or profile is None
                or ws.template != profile.template
                or ws.build_is_current is False
            ):
                await self._retire(client, row, result)
                continue
            live.append((row, ws))

        # Keep the ready ones first, oldest first; anything past the target goes.
        live.sort(key=lambda item: (item[1].status != _RUNNING, item[0].created_at))
        keep, excess = live[:target], live[target:]
        for row, _ws in excess:
            await self._retire(client, row, result)
        ready = 0
        for row, ws in keep:
            if await self._keep_ready(client, row, ws, identity, result):
                ready += 1
        for _ in range(target - len(keep)):
            if profile is not None and await self._create(client, profile, identity, result):
                ready += 1
        if target:
            result.ready[profile_id] = ready

    async def _keep_ready(
        self,
        client: CoderClient,
        row: WorkspaceBinding,
        ws: CoderWorkspace,
        identity: tuple[str, str],
        result: PoolPass,
    ) -> bool:
        if ws.status in _STARTABLE:
            # Flip out of "ready" before starting, so no session claims a
            # workspace that is mid-start; the next pass marks it ready.
            starting = self.registry.update_pool(row.binding_id, state=_STARTING)
            if starting is None:
                return False
            row = starting
            # A restarted workspace may not keep its files; install afresh.
            _installed.pop(ws.uuid, None)
            ws = await client.start_workspace(row.workspace_name)
            result.started += 1
            if not self._owned(row, ws, identity):
                return False
        if ws.status != _RUNNING:
            return False
        # Before a starting row is marked claimable, so its claimant normally
        # finds the files in place. A row already ready (say, after a gateway
        # restart emptied the record) gets them on this pass; a session that
        # claims it meanwhile just installs them itself.
        await self._preinstall(row, ws)
        if row.state != POOL_READY or row.workspace_uuid != ws.uuid:
            marked = self.registry.update_pool(
                row.binding_id,
                state=POOL_READY,
                workspace_uuid=ws.uuid,
                owner_id=identity[1],
            )
            if marked is None:
                return False
        await self._renew(client, row)
        return True

    async def _renew(self, client: CoderClient, row: WorkspaceBinding) -> None:
        now = self._clock()
        last = self._extended_at.get(row.binding_id)
        if last is not None and now - last < EXTEND_EVERY_SECS:
            return
        try:
            await client.extend_workspace_deadline(row.workspace_name, self.env.stop_after_minutes)
        except CoderClientError:
            # The workspace keeps its existing deadline; the next pass retries.
            logger.debug("Coder hot pool: could not renew a ready workspace's deadline")
            return
        self._extended_at[row.binding_id] = now

    async def _create(
        self,
        client: CoderClient,
        profile: Any,
        identity: tuple[str, str],
        result: PoolPass,
    ) -> bool:
        from kiro_crew.coder.session_launch import WORKSPACE_PREFIX

        row = self.registry.allocate_pool(
            profile.id,
            template=profile.template,
            prefix=WORKSPACE_PREFIX,
            owner_name=identity[0],
            deployment_id=self.env.url,
        )
        ws = await client.create_workspace(
            name=row.workspace_name,
            template=profile.template,
            preset="",
            stop_after_minutes=self.env.stop_after_minutes,
        )
        result.created += 1
        if not self._owned(row, ws, identity):
            logger.warning("Coder hot pool: a created workspace did not verify; leaving it")
            return False
        state = POOL_READY if ws.status == _RUNNING else _STARTING
        if state == POOL_READY:
            await self._preinstall(row, ws)
        marked = self.registry.update_pool(
            row.binding_id, state=state, workspace_uuid=ws.uuid, owner_id=identity[1]
        )
        if marked is not None and state == POOL_READY:
            self._extended_at[row.binding_id] = self._clock()
            return True
        return False

    async def _retire(
        self,
        client: CoderClient,
        row: WorkspaceBinding,
        result: PoolPass,
    ) -> None:
        # Step one under the lock: a "retiring" row can never be claimed. If a
        # session took it first, update_pool says so and the workspace is theirs.
        if row.state != _RETIRING:
            if self.registry.update_pool(row.binding_id, state=_RETIRING) is None:
                return
        await client.delete_workspace(row.workspace_name)
        self.registry.remove_pool(row.binding_id)
        self._extended_at.pop(row.binding_id, None)
        _installed.pop(row.workspace_uuid, None)
        result.retired += 1


def pool_status(registry: Any, profiles: list[Any]) -> dict[str, dict[str, int]]:
    """Per profile: ``{target, ready, warming}`` from the registry alone.

    No Coder command runs, so the dashboard can poll this cheaply. ``ready`` is
    what a new session can claim right now; ``warming`` is pool workspaces being
    created or started. Rows being retired count as neither.
    """
    targets = pool_targets(profiles)
    status = {p.id: {"target": targets.get(p.id, 0), "ready": 0, "warming": 0} for p in profiles}
    for row in registry.pool_rows():
        entry = status.get(_profile_of(row))
        if entry is None or row.state == _RETIRING:
            continue
        entry["ready" if row.state == POOL_READY else "warming"] += 1
    return status


# ── The gateway's keeper loop ─────────────────────────────────────────────

_wake: Optional[asyncio.Event] = None


def request_refill() -> None:
    """Wake the keeper now, e.g. right after a session claimed a ready workspace."""
    if _wake is not None:
        _wake.set()


async def reconcile_once() -> Optional[PoolPass]:
    """One pass against the live config; None when Coder is not configured."""
    from kiro_crew.coder.session_launch import CoderSessionEnvironments
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.paths import data_home

    config = live.snapshot()
    if config is None:
        config = await asyncio.to_thread(KiroCrewConfig.load)
    section = config.session_environments
    # Archived sessions' sprites run out on this clock too, Coder or not.
    # Registry-only unless a row is due, so it issues no Sprites call otherwise.
    from kiro_crew.coder.retention import sweep_due_boxd, sweep_due_sprites

    try:
        sprites_deleted = await sweep_due_sprites()
    except Exception:  # noqa: BLE001 -- a Sprites failure never stops the Coder pass
        logger.warning("Archived session sprites: sweep failed", exc_info=True)
        sprites_deleted = 0
    try:
        sprites_deleted += await sweep_due_boxd()
    except Exception:  # noqa: BLE001 -- a boxd failure never stops the Coder pass
        logger.warning("Archived session boxd machines: sweep failed", exc_info=True)
    if not getattr(section, "coder_url", ""):
        return None
    home = await asyncio.to_thread(data_home)
    environments = CoderSessionEnvironments(section, home)
    summary = await _keeper_for(environments).reconcile()
    # Archived sessions' workspaces past their retention go on the same clock.
    # Registry-only unless a row is due, so an idle pool still issues nothing.
    from kiro_crew.coder.retention import sweep_due

    summary.archived_deleted = await sweep_due(environments) + sprites_deleted
    return summary


_keepers: dict[str, CoderHotPool] = {}


def _keeper_for(environments: Any) -> CoderHotPool:
    # One keeper per data home keeps the renew-throttle memory across passes.
    key = str(environments.data_home)
    keeper = _keepers.get(key)
    if keeper is None:
        keeper = _keepers[key] = CoderHotPool(environments)
    else:
        keeper.env = environments
    return keeper


async def run_keeper(
    *,
    interval: float = KEEPER_INTERVAL_SECS,
    initial_delay: float = KEEPER_INITIAL_DELAY_SECS,
) -> None:
    """Run reconcile passes forever. Failures are logged; the loop never dies."""
    global _wake
    _wake = asyncio.Event()
    # A saved pool size (or URL, or profile) is acted on now, not at the next
    # interval: Settings tells the user ready workspaces are starting. Held on
    # this frame so the subscription lives exactly as long as the keeper.
    from kiro_crew.config import live

    subscription = live.subscribe(
        "session_environments", callback=_on_config_change, name="coder_hot_pool.refill"
    )
    try:
        await _keeper_loop(interval, initial_delay)
    finally:
        subscription.cancel()


def _on_config_change(_change: Any) -> None:
    request_refill()


async def _keeper_loop(interval: float, initial_delay: float) -> None:
    assert _wake is not None
    # A save during the startup delay ends it early, like any other wake.
    try:
        await asyncio.wait_for(_wake.wait(), timeout=initial_delay)
    except asyncio.TimeoutError:
        pass
    _wake.clear()
    last_error = ""
    while True:
        try:
            summary = await reconcile_once()
            if summary is not None and (
                summary.created or summary.started or summary.retired or summary.archived_deleted
            ):
                logger.info(
                    "Coder hot pool: ready=%s created=%d started=%d retired=%d "
                    "archived_deleted=%d",
                    summary.ready,
                    summary.created,
                    summary.started,
                    summary.retired,
                    summary.archived_deleted,
                )
            last_error = ""
        except asyncio.CancelledError:
            raise
        except (CoderClientError, WorkspaceRegistryCorrupt) as exc:
            # Same failure every minute is one warning, not sixty.
            message = str(exc)
            if message != last_error:
                logger.warning("Coder hot pool pass failed: %s", message)
            last_error = message
        except Exception:
            logger.warning("Coder hot pool pass failed", exc_info=True)
        try:
            await asyncio.wait_for(_wake.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        _wake.clear()
