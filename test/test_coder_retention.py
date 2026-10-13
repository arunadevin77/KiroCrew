"""Archived sessions: stop, keep for the retention window, then delete."""

from __future__ import annotations

import asyncio
import functools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from kiro_crew.coder import hot_pool, retention, session_launch
from kiro_crew.coder.client import CoderClientError, CoderWorkspace
from kiro_crew.coder.registry import DELETING
from kiro_crew.config.session_environments import (
    ARCHIVED_RETENTION_DAYS_DEFAULT,
    SessionEnvironmentsError,
    load_archived_retention_days,
    validate_archived_retention_days,
    validate_session_environments,
)
from kiro_crew.session_environment import SessionEnvironmentUnavailable

URL = "https://coder.example.test"
KEY = "dashboard:chat-1"


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), 30))

    return wrapper


class FakeCoder:
    def __init__(self):
        self.workspaces: dict[str, CoderWorkspace] = {}
        self.calls: list[str] = []
        self.fail_delete = False

    async def current_user(self):
        return "devuser", "u-1"

    async def list_workspaces(self):
        return tuple(self.workspaces.values())

    async def get_workspace(self, name):
        self.calls.append(f"get {name}")
        return self.workspaces.get(name)

    async def create_workspace(self, *, name, template, preset, stop_after_minutes):
        self.calls.append(f"create {name}")
        ws = CoderWorkspace(f"uuid-{name}", name, "u-1", template, "running", "")
        self.workspaces[name] = ws
        return ws

    def _status(self, name, status):
        ws = self.workspaces[name]
        self.workspaces[name] = CoderWorkspace(ws.uuid, ws.name, ws.owner, ws.template, status, "")
        return self.workspaces[name]

    async def start_workspace(self, name):
        self.calls.append(f"start {name}")
        return self._status(name, "running")

    async def stop_workspace(self, name):
        self.calls.append(f"stop {name}")
        return self._status(name, "stopped")

    async def delete_workspace(self, name):
        self.calls.append(f"delete {name}")
        if self.fail_delete:
            raise CoderClientError("delete refused")
        self.workspaces.pop(name, None)

    def verbs(self):
        return [call.split()[0] for call in self.calls]


def _config(days=3):
    profile = SimpleNamespace(id="dev", name="Dev", template="micro", workdir="/w", hot_pool=0)
    return SimpleNamespace(coder_url=URL, profiles=[profile], archived_retention_days=days)


def _envs(tmp_path, fake, days=3):
    return session_launch.CoderSessionEnvironments(
        _config(days), tmp_path, coder_bin="coder", client_factory=lambda b, u: fake
    )


async def _started(tmp_path, fake, days=3):
    envs = _envs(tmp_path, fake, days)
    await envs.prepare(KEY, "dev")
    return envs, envs.registry.get_by_session(KEY)


# ── Config ──────────────────────────────────────────────────────────────


def test_retention_is_zero_to_seven_days():
    assert validate_archived_retention_days(0) == 0
    assert validate_archived_retention_days(7) == 7
    for bad in (-1, 8, True, "3", 1.5):
        with pytest.raises(SessionEnvironmentsError, match="archived_retention_days"):
            validate_archived_retention_days(bad)


def test_retention_is_optional_and_lenient_on_load():
    base = {"coder_url": URL, "profiles": []}
    assert "archived_retention_days" not in validate_session_environments(base)
    assert (
        validate_session_environments({**base, "archived_retention_days": 2})[
            "archived_retention_days"
        ]
        == 2
    )
    assert load_archived_retention_days(base) == ARCHIVED_RETENTION_DAYS_DEFAULT
    assert load_archived_retention_days({**base, "archived_retention_days": 99}) == (
        ARCHIVED_RETENTION_DAYS_DEFAULT
    )


def test_a_config_without_the_setting_never_means_delete_now(tmp_path):
    envs = _envs(tmp_path, FakeCoder())
    del envs._config.archived_retention_days
    stamped = []

    def schedule(key, due):
        stamped.append(due)
        return None

    envs.registry.schedule_deletion = schedule
    asyncio.run(retention.archive_session(envs, KEY))
    due = datetime.fromisoformat(stamped[0].replace("Z", "+00:00"))
    assert due - datetime.now(timezone.utc) > timedelta(days=ARCHIVED_RETENTION_DAYS_DEFAULT - 1)


# ── Archive and reopen ──────────────────────────────────────────────────


@sync
async def test_archive_stops_the_workspace_and_stamps_the_retention(tmp_path):
    fake = FakeCoder()
    envs, row = await _started(tmp_path, fake, days=3)
    fake.calls.clear()
    await retention.archive_session(envs, KEY)
    assert fake.verbs() == ["get", "stop"]
    stamped = envs.registry.get_by_session(KEY)
    due = datetime.fromisoformat(stamped.deletion_due_at.replace("Z", "+00:00"))
    assert timedelta(days=2, hours=23) < due - datetime.now(timezone.utc) <= timedelta(days=3)


@sync
async def test_a_session_with_no_workspace_is_a_no_op(tmp_path):
    fake = FakeCoder()
    assert await retention.archive_session(_envs(tmp_path, fake), "dashboard:local") is None
    assert fake.calls == []


@sync
async def test_reopening_before_the_deadline_keeps_the_same_workspace(tmp_path):
    fake = FakeCoder()
    envs, row = await _started(tmp_path, fake)
    await retention.archive_session(envs, KEY)
    fake.calls.clear()
    prepared = await envs.prepare(KEY, "dev")
    assert prepared.workspace == row.workspace_name
    assert "create" not in fake.verbs() and "start" in fake.verbs()
    assert envs.registry.get_by_session(KEY).deletion_due_at == ""
    # The cancelled stamp never deletes it later.
    later = datetime.now(timezone.utc) + timedelta(days=30)
    assert await retention.sweep_due(envs, now=later) == 0


@sync
async def test_reopening_while_it_is_being_deleted_is_refused(tmp_path):
    fake = FakeCoder()
    envs, row = await _started(tmp_path, fake, days=0)
    await retention.archive_session(envs, KEY)
    envs.registry.begin_deletion(row.binding_id, datetime.now(timezone.utc))
    with pytest.raises(SessionEnvironmentUnavailable, match="being deleted"):
        await envs.prepare(KEY, "dev")


# ── Sweep ───────────────────────────────────────────────────────────────


@sync
async def test_the_sweep_deletes_only_what_is_due_and_drops_the_row(tmp_path):
    fake = FakeCoder()
    envs, row = await _started(tmp_path, fake, days=3)
    await retention.archive_session(envs, KEY)
    assert await retention.sweep_due(envs) == 0  # not yet
    later = datetime.now(timezone.utc) + timedelta(days=3, minutes=1)
    fake.calls.clear()
    assert await retention.sweep_due(envs, now=later) == 1
    assert f"delete {row.workspace_name}" in fake.calls
    assert envs.registry.get_by_session(KEY) is None
    # Reopened afterwards, the session gets a fresh workspace.
    prepared = await envs.prepare(KEY, "dev")
    assert prepared.workspace != row.workspace_name


@sync
async def test_zero_days_deletes_on_the_next_pass_without_a_stop(tmp_path):
    fake = FakeCoder()
    envs, row = await _started(tmp_path, fake, days=0)
    fake.calls.clear()
    await retention.archive_session(envs, KEY)
    assert "stop" not in fake.verbs()
    assert await retention.sweep_due(envs) == 1
    assert row.workspace_name not in fake.workspaces


@sync
async def test_a_failed_delete_stays_deleting_and_retries(tmp_path):
    fake = FakeCoder()
    envs, row = await _started(tmp_path, fake, days=0)
    await retention.archive_session(envs, KEY)
    fake.fail_delete = True
    assert await retention.sweep_due(envs) == 0
    assert envs.registry.get_by_session(KEY).state == DELETING
    fake.fail_delete = False
    assert await retention.sweep_due(envs) == 1
    assert envs.registry.get_by_session(KEY) is None


@sync
async def test_the_sweep_never_deletes_a_workspace_its_row_did_not_mint(tmp_path):
    fake = FakeCoder()
    envs, row = await _started(tmp_path, fake, days=0)
    await retention.archive_session(envs, KEY)
    fake.workspaces[row.workspace_name] = CoderWorkspace(
        "someone-else", row.workspace_name, "u-2", "micro", "running", ""
    )
    fake.calls.clear()
    assert await retention.sweep_due(envs) == 1  # the row is forgotten...
    assert "delete" not in fake.verbs()  # ...and the workspace left alone
    assert row.workspace_name in fake.workspaces


@sync
async def test_pool_rows_are_never_scheduled_or_swept(tmp_path):
    fake = FakeCoder()
    envs = _envs(tmp_path, fake, days=0)
    pool_row = envs.registry.allocate_pool(
        "dev", template="micro", prefix="crewenv", owner_name="devuser", deployment_id=URL
    )
    assert envs.registry.schedule_deletion(pool_row.session_key, "2000-01-01T00:00:00Z") is None
    assert envs.registry.due_for_deletion(datetime.now(timezone.utc)) == ()


@sync
async def test_the_keeper_pass_runs_the_sweep(tmp_path, monkeypatch):
    fake = FakeCoder()
    envs, row = await _started(tmp_path, fake, days=0)
    await retention.archive_session(envs, KEY)

    from kiro_crew.config import live

    # Every Coder call in the pass goes to the fake: no real binary is reachable.
    monkeypatch.setattr(session_launch, "CoderSessionEnvironments", lambda section, home: envs)
    monkeypatch.setattr(
        live, "snapshot", lambda: SimpleNamespace(session_environments=envs._config)
    )
    monkeypatch.setattr(hot_pool, "_keepers", {})
    summary = await hot_pool.reconcile_once()
    assert summary is not None and summary.archived_deleted == 1


# ── Gateway shutdown ────────────────────────────────────────────────────


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


@sync
async def test_shutdown_stops_every_provider_without_scheduling_deletion(tmp_path, monkeypatch):
    fake = FakeCoder()
    envs, binding = await _started(tmp_path, fake)
    fake.calls.clear()
    stopped_tasks: list[tuple[str, str]] = []
    hibernated: list[str] = []

    async def archive(key, reason="the session was archived"):
        stopped_tasks.append((key, reason))
        return True

    fargate = SimpleNamespace(
        registry=_Rows([SimpleNamespace(session_key="dashboard:chat-f")]), archive_session=archive
    )

    class Cli:
        async def hibernate(self, name):
            hibernated.append(name)

    boxd = SimpleNamespace(
        registry=_Rows(
            [SimpleNamespace(session_key="dashboard:chat-b", machine="kc-0123456789abcdef")]
        ),
        cli=lambda: Cli(),
    )

    async def coder_envs():
        return envs

    async def fargate_envs():
        return fargate

    async def boxd_envs():
        return boxd

    monkeypatch.setattr(retention, "_environments", coder_envs)
    monkeypatch.setattr(retention, "_fargate_environments", fargate_envs)
    monkeypatch.setattr(retention, "_boxd_environments", boxd_envs)

    counts = await retention.stop_for_shutdown()

    assert counts == {"coder": 1, "fargate": 1, "boxd": 1}
    assert f"stop {binding.workspace_name}" in fake.calls
    assert stopped_tasks == [("dashboard:chat-f", "the gateway shut down")]
    assert hibernated == ["kc-0123456789abcdef"]
    # A restart resumes the same workspace: nothing was stamped for deletion.
    assert envs.registry.get_by_session(KEY).deletion_due_at == ""


@sync
async def test_shutdown_never_raises_when_a_provider_fails(tmp_path, monkeypatch):
    async def boom():
        raise RuntimeError("provider unreachable")

    async def nothing():
        return None

    boxd = SimpleNamespace(registry=_Rows([]), cli=lambda: None)

    async def boxd_envs():
        return boxd

    monkeypatch.setattr(retention, "_environments", nothing)
    monkeypatch.setattr(retention, "_fargate_environments", boom)
    monkeypatch.setattr(retention, "_boxd_environments", boxd_envs)
    assert await retention.stop_for_shutdown() == {"coder": 0, "fargate": 0, "boxd": 0}


@sync
async def test_shutdown_leaves_an_already_stopped_workspace_alone(tmp_path, monkeypatch):
    fake = FakeCoder()
    envs, binding = await _started(tmp_path, fake)
    fake._status(binding.workspace_name, "stopped")
    fake.calls.clear()

    async def coder_envs():
        return envs

    async def empty():
        return SimpleNamespace(registry=_Rows([]), cli=lambda: None)

    monkeypatch.setattr(retention, "_environments", coder_envs)
    monkeypatch.setattr(retention, "_fargate_environments", empty)
    monkeypatch.setattr(retention, "_boxd_environments", empty)
    assert (await retention.stop_for_shutdown())["coder"] == 0
    assert "stop" not in fake.verbs()


@sync
async def test_gateway_shutdown_stop_is_bounded_and_cancels_the_pool_keeper(monkeypatch):
    from kiro_crew.slack import gateway

    async def hang():
        await asyncio.sleep(3600)

    keeper = asyncio.ensure_future(asyncio.sleep(3600))
    monkeypatch.setattr(gateway, "_CODER_HOT_POOL_TASK", keeper)
    monkeypatch.setattr(retention, "stop_for_shutdown", hang)
    monkeypatch.setattr(retention, "SHUTDOWN_STOP_SECS", 0.05)
    await asyncio.wait_for(gateway._stop_session_environments(), 5)
    await asyncio.sleep(0)
    assert keeper.cancelled()


@sync
async def test_archive_warns_about_unsaved_work_before_stopping(monkeypatch):
    from kiro_crew import session_environment as se
    from kiro_crew.coder import session_browse

    order: list[str] = []
    notes: list[tuple[str, str]] = []

    async def unsaved(key):
        order.append(f"check {key}")
        return "3 uncommitted files" if key == "dashboard:dirty" else None

    monkeypatch.setattr(session_browse, "unsaved_work", unsaved)
    monkeypatch.setattr(se, "session_runs_remotely", lambda key: key != "dashboard:local")
    await retention.warn_unsaved_work(
        ("dashboard:dirty", "dashboard:clean", "dashboard:local"),
        lambda title, body: notes.append((title, body)),
    )
    assert order == ["check dashboard:dirty", "check dashboard:clean"]
    assert len(notes) == 1 and "3 uncommitted files" in notes[0][1]


@sync
async def test_an_unsaved_work_check_failure_never_blocks_the_archive(monkeypatch):
    from kiro_crew import session_environment as se
    from kiro_crew.coder import session_browse

    async def boom(key):
        raise RuntimeError("machine unreachable")

    monkeypatch.setattr(session_browse, "unsaved_work", boom)
    monkeypatch.setattr(se, "session_runs_remotely", lambda key: True)
    await retention.warn_unsaved_work(("dashboard:x",), lambda *a: pytest.fail("no notice"))


@sync
async def test_stop_on_shutdown_off_leaves_environments_running(monkeypatch):
    from kiro_crew.slack import gateway

    called = []

    async def stop():
        called.append(1)
        return {}

    async def off():
        return False

    monkeypatch.setattr(gateway, "_CODER_HOT_POOL_TASK", None)
    monkeypatch.setattr(retention, "stop_for_shutdown", stop)
    monkeypatch.setattr(retention, "stop_on_shutdown_enabled", off)
    await gateway._stop_session_environments()
    assert called == []


def test_stop_on_shutdown_is_a_boolean_that_defaults_on():
    from kiro_crew.config.session_environments import (
        load_stop_on_shutdown,
        validate_stop_on_shutdown,
    )

    base = {"coder_url": URL, "profiles": []}
    assert load_stop_on_shutdown(base) is True
    assert load_stop_on_shutdown({**base, "stop_on_shutdown": False}) is False
    assert load_stop_on_shutdown({**base, "stop_on_shutdown": "no"}) is True
    assert (
        validate_session_environments({**base, "stop_on_shutdown": False})["stop_on_shutdown"]
        is False
    )
    for bad in ("true", 1, None):
        with pytest.raises(SessionEnvironmentsError, match="stop_on_shutdown"):
            validate_stop_on_shutdown(bad)
