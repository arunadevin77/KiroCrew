"""Prepare a session's sprite: the same seam :class:`CoderSessionEnvironments` offers.

A sprite is a persistent VM that goes cold when idle, so a session keeps one
sprite for its life and its files survive between turns and gateway restarts.
An archived session's sprite is kept for ``archived_retention_days`` (a cold
sprite bills storage only) and then deleted; reopening it before then keeps it.

Security properties:

* The Sprites API token lives in the gateway's encrypted vault, named by the
  profile's ``token_secret``. It is read per call, sent only to the Sprites API,
  and never reaches the sprite, a log, a config file or the agent.
* The gateway only ever acts on a sprite it recorded creating, under a name it
  minted (``kirocrew-`` plus 16 random hex digits), recorded before the create
  call so a failure can still delete it. A sprite deleted outside Kiro Crew is
  replaced by a fresh one, never looked up by any other name.
* The guest worker's per-session secret travels on exec stdin, never in a URL.
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
from kiro_crew.coder.session_launch import PreparedRemoteSession
from kiro_crew.platform_compat import file_lock
from kiro_crew.session_environment import SessionEnvironmentUnavailable, report_start_stage
from kiro_crew.sprites_session.client import SpritesClient, SpritesError
from kiro_crew.sprites_session.shell import SpritesShell, SpritesWorkerLaunch

PROVIDER_ID = "sprites"
NAME_PREFIX = "kirocrew-"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class SessionSprite:
    session_key: str
    profile_id: str
    sprite: str
    created_at: str = ""
    #: Set when the session is archived: when the sprite is deleted.
    deletion_due_at: str = ""


class SessionSpriteRegistry:
    """``session key -> SessionSprite``, written atomically under an exclusive lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(".lock")

    def _read(self) -> dict[str, SessionSprite]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return {k: SessionSprite(**v) for k, v in raw.get("sprites", {}).items()}
        except (OSError, ValueError, TypeError) as exc:
            raise SessionEnvironmentUnavailable(
                "the Sprites session registry is unreadable"
            ) from exc

    def _write(self, rows: dict[str, SessionSprite]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "sprites": {k: asdict(v) for k, v in sorted(rows.items())}}
        atomic_write(
            self.path, json.dumps(payload, indent=2) + "\n", fsync=True, restrict_to_owner=True
        )

    def _locked(self, mutate: Callable[[dict[str, SessionSprite]], Any]) -> Any:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+b") as handle:
            with file_lock(handle.fileno(), exclusive=True, required=True):
                rows = self._read()
                result = mutate(rows)
                self._write(rows)
                return result

    def get(self, session_key: str) -> Optional[SessionSprite]:
        return self._read().get(session_key)

    def all(self) -> list[SessionSprite]:
        return list(self._read().values())

    def put(self, row: SessionSprite) -> None:
        self._locked(lambda rows: rows.__setitem__(row.session_key, row))

    def remove(self, session_key: str, sprite: str) -> bool:
        """Drop the row only if it still names ``sprite`` (a newer one is left alone)."""

        def mutate(rows: dict[str, SessionSprite]) -> bool:
            current = rows.get(session_key)
            if current is None or current.sprite != sprite:
                return False
            del rows[session_key]
            return True

        return bool(self._locked(mutate))

    def set_deletion(self, session_key: str, due: str) -> Optional[SessionSprite]:
        def mutate(rows: dict[str, SessionSprite]) -> Optional[SessionSprite]:
            current = rows.get(session_key)
            if current is None:
                return None
            rows[session_key] = replace(current, deletion_due_at=due)
            return rows[session_key]

        result: Optional[SessionSprite] = self._locked(mutate)
        return result

    def due(self, moment: datetime) -> list[SessionSprite]:
        stamp = _stamp(moment)
        return [
            r for r in self._read().values() if r.deletion_due_at and r.deletion_due_at <= stamp
        ]


def _vault_token(secret_name: str) -> str:
    from kiro_crew.config.paths import config_dir
    from kiro_crew.secrets.vault import SecretVault

    value = SecretVault(config_dir()).get(secret_name)
    return value.reveal() if value is not None else ""


class SpritesSessionEnvironments:
    """Create, reuse and delete sprites for the saved Sprites profiles."""

    def __init__(
        self,
        config: Any,
        data_home: Path,
        *,
        token_reader: Optional[Callable[[str], str]] = None,
        base_url: Optional[str] = None,
    ) -> None:
        self._config = config
        self.data_home = Path(data_home)
        self.registry = SessionSpriteRegistry(self.data_home / "sprites" / "session-sprites.json")
        self.worker_secrets_dir = self.data_home / "remote-session-secrets"
        self._token_reader = token_reader
        self._base_url = base_url

    def profiles(self) -> list[dict[str, Any]]:
        return [
            p
            for p in (getattr(self._config, "sprites_profiles", None) or [])
            if isinstance(p, dict)
        ]

    def profile(self, profile_id: str) -> dict[str, Any]:
        for p in self.profiles():
            if p.get("id") == profile_id:
                return p
        raise SessionEnvironmentUnavailable(
            "this session's Sprites profile is no longer configured"
        )

    async def client(self, profile: dict[str, Any]) -> SpritesClient:
        name = str(profile.get("token_secret") or "")
        reader = self._token_reader or _vault_token
        token = await asyncio.to_thread(reader, name) if name else ""
        if not token:
            raise SessionEnvironmentUnavailable(
                f"no Sprites API token: save one as the secret {name or 'SPRITES_TOKEN'} "
                "in Settings > Secrets"
            )
        kwargs = {"base_url": self._base_url} if self._base_url else {}
        return SpritesClient(token, **kwargs)

    async def shell_for(self, session_key: str, profile_id: str) -> Optional[SpritesShell]:
        """The session's existing sprite, or None. No sprite is created or woken."""
        row = await asyncio.to_thread(self.registry.get, session_key)
        if row is None:
            return None
        return SpritesShell(await self.client(self.profile(profile_id)), row.sprite)

    async def prepare(self, session_key: str, profile_id: str) -> PreparedRemoteSession:
        profile = self.profile(profile_id)
        client = await self.client(profile)
        workdir = str(profile.get("workdir") or "")
        row = await asyncio.to_thread(self.registry.get, session_key)
        if row is not None:
            if row.deletion_due_at:
                # Reopened before its retention ran out: keep it, files intact.
                await asyncio.to_thread(self.registry.set_deletion, session_key, "")
            if await client.get_sprite(row.sprite) is None:
                # Deleted outside Kiro Crew: never adopt another name, start fresh.
                await asyncio.to_thread(self.registry.remove, session_key, row.sprite)
                row = None
        created = row is None
        if row is None:
            row = await self._create(session_key, profile, client)
        shell = SpritesShell(client, row.sprite)
        if workdir:
            try:
                await shell.run(f"mkdir -p {shlex.quote(workdir)}")
            except Exception as exc:  # noqa: BLE001 -- surfaced as an unavailable session
                raise SessionEnvironmentUnavailable(f"the sprite did not answer: {exc}") from exc
        launch = SpritesWorkerLaunch(
            f"sp-{row.sprite[len(NAME_PREFIX):]}", shell, self.worker_secrets_dir
        )
        return PreparedRemoteSession(
            launch,
            workdir,
            row.sprite,
            str(profile["id"]),
            str(profile.get("name") or profile["id"]),
            created=created,
        )

    async def _create(
        self, session_key: str, profile: dict[str, Any], client: SpritesClient
    ) -> SessionSprite:
        report_start_stage(session_key, "provisioning")
        row = SessionSprite(
            session_key, str(profile["id"]), NAME_PREFIX + os.urandom(8).hex(), _stamp(_now())
        )
        # Recorded before the create, so an archive (or a failure below) can delete it.
        await asyncio.to_thread(self.registry.put, row)
        try:
            await client.create_sprite(row.sprite)
        except BaseException:
            try:
                await client.delete_sprite(row.sprite)
            except SpritesError:
                pass  # the row stays; the next prepare or sweep retries the cleanup
            else:
                await asyncio.to_thread(self.registry.remove, session_key, row.sprite)
            raise
        return row

    async def archive_session(self, session_key: str, retention_days: int) -> bool:
        """Schedule an archived session's sprite for deletion; 0 days deletes it now."""
        row = await asyncio.to_thread(self.registry.get, session_key)
        if row is None:
            return False
        if retention_days <= 0:
            await self._delete(row)
            return True
        due = _stamp(_now() + timedelta(days=retention_days))
        await asyncio.to_thread(self.registry.set_deletion, session_key, due)
        return True

    async def _delete(self, row: SessionSprite) -> None:
        try:
            profile = self.profile(row.profile_id)
        except SessionEnvironmentUnavailable:
            # A removed profile cannot name its token; keep the row for later.
            return
        await (await self.client(profile)).delete_sprite(row.sprite)
        await asyncio.to_thread(self.registry.remove, row.session_key, row.sprite)

    async def sweep_due(self, *, now: Optional[datetime] = None) -> int:
        """Delete every archived session's sprite whose retention ran out."""
        deleted = 0
        for row in await asyncio.to_thread(self.registry.due, now or _now()):
            try:
                await self._delete(row)
            except SessionEnvironmentUnavailable:
                continue  # retried on the next pass
            if await asyncio.to_thread(self.registry.get, row.session_key) is None:
                deleted += 1
        return deleted
