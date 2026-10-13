"""Tests for the Coder hot pool: ready workspaces a new remote session claims."""

from __future__ import annotations

import asyncio
import functools
from types import SimpleNamespace

import pytest

from kiro_crew.coder import hot_pool, session_launch
from kiro_crew.coder.client import CoderClientError, CoderWorkspace
from kiro_crew.coder.registry import POOL_READY, is_pool_key
from kiro_crew.coder.remote_worker import RemoteWorkerError, worker_digest
from kiro_crew.config.session_environments import (
    SessionEnvironmentsError,
    load_session_environments,
    validate_profile,
)
from kiro_crew.session_environment import SessionEnvironmentUnavailable

URL = "https://coder.example"


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), 30))

    return wrapper


def _profile(hot_pool=1, template="micro", pid="dev"):
    return SimpleNamespace(
        id=pid, name="Dev", template=template, workdir="/home/coder/app", hot_pool=hot_pool
    )


def _config(*profiles):
    return SimpleNamespace(coder_url=URL, profiles=list(profiles) or [_profile()])


class FakeCoder:
    def __init__(self, *, owner="devuser", owner_id="u-1"):
        self.owner, self.owner_id = owner, owner_id
        self.workspaces: dict[str, CoderWorkspace] = {}
        self.calls: list[str] = []
        self.create_status = "running"

    async def current_user(self):
        return self.owner, self.owner_id

    async def list_workspaces(self):
        return tuple(self.workspaces.values())

    async def get_workspace(self, name):
        self.calls.append(f"get {name}")
        return self.workspaces.get(name)

    async def create_workspace(self, *, name, template, preset, stop_after_minutes):
        self.calls.append(f"create {name}")
        ws = CoderWorkspace(f"uuid-{name}", name, self.owner_id, template, self.create_status, "")
        self.workspaces[name] = ws
        return ws

    async def start_workspace(self, name):
        self.calls.append(f"start {name}")
        self.set(name, status="running")
        return self.workspaces[name]

    async def delete_workspace(self, name):
        self.calls.append(f"delete {name}")
        self.workspaces.pop(name, None)

    async def extend_workspace_deadline(self, name, minutes):
        self.calls.append(f"extend {name} {minutes}")

    def set(self, name, **changes):
        ws = self.workspaces[name]
        values = {
            "uuid": ws.uuid,
            "name": ws.name,
            "owner": ws.owner,
            "template": ws.template,
            "status": ws.status,
            "last_used_at": "",
        }
        extra = {k: changes.pop(k) for k in ("outdated",) if k in changes}
        values.update(changes)
        self.workspaces[name] = CoderWorkspace(**values, **extra)

    def verbs(self):
        return [call.split()[0] for call in self.calls]


def _envs(tmp_path, fake, config=None):
    return session_launch.CoderSessionEnvironments(
        config or _config(), tmp_path, coder_bin="coder", client_factory=lambda b, u: fake
    )


class FakeInstaller:
    """Stands in for ``coder ssh``: no test pass may reach a real workspace."""

    def __init__(self, fail=False):
        self.installed: list[str] = []
        self.fail = fail

    async def __call__(self, workspace_name):
        if self.fail:
            raise RemoteWorkerError("guest command failed (exit 4): python3 is not installed")
        self.installed.append(workspace_name)
        return worker_digest()


@pytest.fixture(autouse=True)
def _no_installed_record(monkeypatch):
    # The record is process memory; each test starts from an empty one.
    monkeypatch.setattr(hot_pool, "_installed", {})


def _pool(tmp_path, fake, config=None, clock=None, installer=None):
    return hot_pool.CoderHotPool(
        _envs(tmp_path, fake, config),
        clock=clock or (lambda: 0.0),
        install_worker=installer or FakeInstaller(),
    )


# ── Config ──────────────────────────────────────────────────────────────


def test_profile_hot_pool_is_optional_bounded_and_strict():
    base = {"id": "dev", "name": "Dev", "template": "micro", "workdir": "/w"}
    assert validate_profile(base)["hot_pool"] == 0
    assert validate_profile({**base, "hot_pool": 2})["hot_pool"] == 2
    for bad in (-1, 6, True, "1", 1.5):
        with pytest.raises(SessionEnvironmentsError, match="hot_pool"):
            validate_profile({**base, "hot_pool": bad})
    # A bad pool count drops only that profile at load time.
    _url, profiles = load_session_environments(
        {"profiles": [{**base, "hot_pool": 99}, {**base, "id": "ok", "hot_pool": 1}]}
    )
    assert [(p["id"], p["hot_pool"]) for p in profiles] == [("ok", 1)]


def test_targets_are_clamped_in_total():
    profiles = [_profile(5, pid=f"p{i}") for i in range(3)]
    assert hot_pool.pool_targets(profiles) == {"p0": 5, "p1": 5}
    assert hot_pool.pool_targets([_profile(0)]) == {}


# ── Keeper ──────────────────────────────────────────────────────────────


@sync
async def test_off_means_no_coder_calls(tmp_path):
    fake = FakeCoder()
    summary = await _pool(tmp_path, fake, _config(_profile(0))).reconcile()
    assert summary.created == 0 and fake.calls == []


@sync
async def test_fills_the_pool_and_renews_the_deadline_while_ready(tmp_path):
    fake = FakeCoder()
    now = [0.0]
    pool = _pool(tmp_path, fake, _config(_profile(2)), clock=lambda: now[0])
    summary = await pool.reconcile()
    assert summary.created == 2 and summary.ready == {"dev": 2}
    rows = pool.registry.pool_rows("dev")
    assert len(rows) == 2 and all(r.state == POOL_READY and r.workspace_uuid for r in rows)
    assert all(r.workspace_name.startswith("crewenv-devuser-") for r in rows)

    fake.calls.clear()
    await pool.reconcile()
    assert "create" not in fake.verbs() and "extend" not in fake.verbs()
    now[0] = hot_pool.EXTEND_EVERY_SECS + 1
    await pool.reconcile()
    assert fake.verbs().count("extend") == 2


@sync
async def test_a_session_claims_a_ready_workspace_instead_of_creating(tmp_path):
    fake = FakeCoder()
    pool = _pool(tmp_path, fake)
    await pool.reconcile()
    ready = pool.registry.pool_rows("dev")[0]
    fake.calls.clear()

    prepared = await _envs(tmp_path, fake).prepare("sess-a", "dev")
    assert prepared.workspace == ready.workspace_name
    assert "create" not in fake.verbs()
    claimed = pool.registry.get_by_session("sess-a")
    assert claimed.binding_id == ready.binding_id and claimed.generation == ready.generation + 1
    assert pool.registry.pool_rows("dev") == ()

    # The keeper refills, and the claimed workspace is never touched again.
    fake.calls.clear()
    summary = await pool.reconcile()
    assert summary.created == 1
    assert not any(ready.workspace_name in call for call in fake.calls)


@sync
async def test_a_waiting_workspace_gets_the_worker_files_once(tmp_path):
    fake, installer = FakeCoder(), FakeInstaller()
    pool = _pool(tmp_path, fake, _config(_profile(2)), installer=installer)
    await pool.reconcile()
    names = sorted(row.workspace_name for row in pool.registry.pool_rows("dev"))
    assert sorted(installer.installed) == names
    await pool.reconcile()
    assert sorted(installer.installed) == names  # recorded, so not repeated


@sync
async def test_a_claim_skips_the_install_the_pool_already_did(tmp_path):
    fake = FakeCoder()
    pool = _pool(tmp_path, fake)
    await pool.reconcile()
    prepared = await _envs(tmp_path, fake).prepare("sess-a", "dev")
    assert prepared.launch.preinstalled_digest == worker_digest()
    # Handed over once: the workspace is the session's now.
    assert hot_pool._installed == {}


@sync
async def test_a_failed_preinstall_still_readies_the_workspace(tmp_path):
    fake = FakeCoder()
    pool = _pool(tmp_path, fake, installer=FakeInstaller(fail=True))
    summary = await pool.reconcile()
    assert summary.ready == {"dev": 1}
    prepared = await _envs(tmp_path, fake).prepare("sess-a", "dev")
    assert prepared.launch.preinstalled_digest == ""  # the claimant installs


@sync
async def test_a_restarted_pool_workspace_is_installed_again(tmp_path):
    fake, installer = FakeCoder(), FakeInstaller()
    pool = _pool(tmp_path, fake, installer=installer)
    await pool.reconcile()
    row = pool.registry.pool_rows("dev")[0]
    fake.set(row.workspace_name, status="stopped")
    await pool.reconcile()
    assert installer.installed == [row.workspace_name, row.workspace_name]


@sync
async def test_a_claimed_workspace_this_start_restarts_trusts_no_preinstall(tmp_path):
    fake = FakeCoder()
    pool = _pool(tmp_path, fake)
    await pool.reconcile()
    row = pool.registry.pool_rows("dev")[0]
    fake.set(row.workspace_name, status="stopped")
    prepared = await _envs(tmp_path, fake).prepare("sess-a", "dev")
    assert prepared.launch.preinstalled_digest == ""


@sync
async def test_a_claim_overlaps_whoami_with_the_workspace_lookup(tmp_path):
    """Each is a CLI round trip on the start path; a claimed row needs no owner first."""
    fake = FakeCoder()
    pool = _pool(tmp_path, fake)
    await pool.reconcile()
    in_flight: list[str] = []
    overlapped: list[bool] = []
    real_user, real_get = fake.current_user, fake.get_workspace

    async def slow_user():
        in_flight.append("whoami")
        await asyncio.sleep(0.05)
        in_flight.remove("whoami")
        return await real_user()

    async def get(name):
        overlapped.append("whoami" in in_flight)
        return await real_get(name)

    fake.current_user, fake.get_workspace = slow_user, get
    await _envs(tmp_path, fake).prepare("sess-a", "dev")
    assert overlapped == [True]


@sync
async def test_a_failed_sign_in_after_a_claim_keeps_the_row_for_the_retry(tmp_path):
    fake = FakeCoder()
    pool = _pool(tmp_path, fake)
    await pool.reconcile()
    ready = pool.registry.pool_rows("dev")[0]
    real_user = fake.current_user

    async def signed_out():
        raise CoderClientError("not logged in")

    fake.current_user = signed_out
    with pytest.raises(SessionEnvironmentUnavailable, match="coder login"):
        await _envs(tmp_path, fake).prepare("sess-a", "dev")
    fake.current_user = real_user
    fake.calls.clear()
    prepared = await _envs(tmp_path, fake).prepare("sess-a", "dev")
    assert prepared.workspace == ready.workspace_name
    assert "create" not in fake.verbs()


@sync
async def test_an_empty_pool_falls_back_to_provisioning(tmp_path):
    fake = FakeCoder()
    prepared = await _envs(tmp_path, fake).prepare("sess-a", "dev")
    assert fake.verbs().count("create") == 1
    assert not is_pool_key(_envs(tmp_path, fake).registry.get_by_session("sess-a").session_key)
    assert prepared.workspace


@sync
async def test_a_session_with_a_workspace_never_claims_a_second(tmp_path):
    fake = FakeCoder()
    first = await _envs(tmp_path, fake).prepare("sess-a", "dev")
    pool = _pool(tmp_path, fake)
    await pool.reconcile()
    again = await _envs(tmp_path, fake).prepare("sess-a", "dev")
    assert again.workspace == first.workspace
    assert len(pool.registry.pool_rows("dev")) == 1


@sync
async def test_a_starting_workspace_is_not_claimable(tmp_path):
    fake = FakeCoder()
    fake.create_status = "starting"
    pool = _pool(tmp_path, fake)
    summary = await pool.reconcile()
    assert summary.ready == {"dev": 0}
    assert (
        pool.registry.claim_pool("sess-a", profile_id="dev", template="micro", deployment_id=URL)
        is None
    )
    name = pool.registry.pool_rows("dev")[0].workspace_name
    fake.set(name, status="running")
    assert (await pool.reconcile()).ready == {"dev": 1}


@sync
async def test_shrinking_the_pool_retires_extras(tmp_path):
    fake = FakeCoder()
    await _pool(tmp_path, fake, _config(_profile(3))).reconcile()
    summary = await _pool(tmp_path, fake, _config(_profile(1))).reconcile()
    assert summary.retired == 2 and fake.verbs().count("delete") == 2
    assert len(_envs(tmp_path, fake).registry.pool_rows("dev")) == 1


@sync
async def test_removed_profile_and_template_drift_retire_the_pool(tmp_path):
    fake = FakeCoder()
    await _pool(tmp_path, fake, _config(_profile(1))).reconcile()
    name = _envs(tmp_path, fake).registry.pool_rows("dev")[0].workspace_name
    fake.set(name, outdated=True)
    summary = await _pool(tmp_path, fake, _config(_profile(1))).reconcile()
    assert summary.retired == 1 and summary.created == 1
    assert name not in fake.workspaces

    summary = await _pool(tmp_path, fake, _config(_profile(1, pid="other"))).reconcile()
    assert _envs(tmp_path, fake).registry.pool_rows("dev") == ()


@sync
async def test_a_claimed_row_is_never_retired(tmp_path):
    fake = FakeCoder()
    pool = _pool(tmp_path, fake)
    await pool.reconcile()
    row = pool.registry.pool_rows("dev")[0]
    claimed = pool.registry.claim_pool(
        "sess-a", profile_id="dev", template="micro", deployment_id=URL
    )
    assert claimed is not None
    # A retire decided on the stale pre-claim row must not delete it.
    await pool._retire(fake, row, hot_pool.PoolPass())
    assert row.workspace_name in fake.workspaces
    assert pool.registry.update_pool(row.binding_id, state="retiring") is None
    assert pool.registry.remove_pool(row.binding_id) is False


@sync
async def test_stopped_ready_workspace_is_started_before_it_is_claimable(tmp_path):
    fake = FakeCoder()
    pool = _pool(tmp_path, fake)
    await pool.reconcile()
    name = pool.registry.pool_rows("dev")[0].workspace_name
    fake.set(name, status="stopped")
    fake.calls.clear()
    summary = await pool.reconcile()
    assert summary.started == 1 and summary.ready == {"dev": 1}
    assert pool.registry.pool_rows("dev")[0].state == POOL_READY


@sync
async def test_a_workspace_that_changed_hands_is_forgotten_not_touched(tmp_path):
    fake = FakeCoder()
    pool = _pool(tmp_path, fake)
    await pool.reconcile()
    name = pool.registry.pool_rows("dev")[0].workspace_name
    fake.set(name, owner="mallory")
    fake.calls.clear()
    summary = await pool.reconcile()
    assert summary.forgotten == 1
    assert not any(name in call for call in fake.calls)
    assert name in fake.workspaces


@sync
async def test_a_deleted_pool_workspace_is_replaced(tmp_path):
    fake = FakeCoder()
    pool = _pool(tmp_path, fake)
    await pool.reconcile()
    name = pool.registry.pool_rows("dev")[0].workspace_name
    del fake.workspaces[name]
    summary = await pool.reconcile()
    assert summary.forgotten == 1 and summary.created == 1


def test_pool_keys_are_reserved(tmp_path):
    registry = _envs(tmp_path, FakeCoder()).registry
    with pytest.raises(ValueError, match="reserved"):
        registry.allocate(
            "coder-hot-pool:dev:x",
            template="micro",
            preset="",
            prefix="crewenv",
            owner_name="devuser",
            deployment_id=URL,
        )
    with pytest.raises(ValueError):
        registry.claim_pool(
            "coder-hot-pool:dev:x", profile_id="dev", template="micro", deployment_id=URL
        )
    with pytest.raises(ValueError):
        registry.update_pool("b", session_key="x")


@sync
async def test_keeper_loop_survives_a_failed_pass(monkeypatch):
    passes = []

    async def flaky():
        passes.append(1)
        if len(passes) == 1:
            raise CoderClientError("down")
        if len(passes) >= 3:
            raise asyncio.CancelledError
        return None

    monkeypatch.setattr(hot_pool, "reconcile_once", flaky)
    with pytest.raises(asyncio.CancelledError):
        await hot_pool.run_keeper(interval=0.01, initial_delay=0)
    assert len(passes) == 3


@sync
async def test_saving_session_environments_wakes_the_keeper_now(monkeypatch):
    """Settings says ready workspaces start on save, so a save cannot wait an interval."""
    from kiro_crew.config import live

    subscribed: list[tuple[tuple[str, ...], object]] = []
    cancelled: list[bool] = []

    class _Sub:
        def cancel(self):
            cancelled.append(True)

    def fake_subscribe(*prefixes, callback, name=None):
        subscribed.append((prefixes, callback))
        return _Sub()

    passes = []

    async def one_pass():
        passes.append(1)
        if len(passes) == 1:
            # The save lands while the keeper sleeps on an hour-long interval.
            asyncio.get_running_loop().call_soon(subscribed[0][1], object())
        else:
            raise asyncio.CancelledError
        return None

    monkeypatch.setattr(live, "subscribe", fake_subscribe)
    monkeypatch.setattr(hot_pool, "reconcile_once", one_pass)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(hot_pool.run_keeper(interval=3600, initial_delay=0), timeout=5)
    assert subscribed[0][0] == ("session_environments",)
    assert len(passes) == 2
    assert cancelled == [True]


# ── Status read (dashboard) ─────────────────────────────────────────────


@sync
async def test_pool_status_counts_ready_and_warming_from_the_registry_alone(tmp_path):
    fake = FakeCoder()
    pool = _pool(tmp_path, fake, _config(_profile(3)))
    await pool.reconcile()
    rows = pool.registry.pool_rows("dev")
    pool.registry.update_pool(rows[0].binding_id, state="starting")
    pool.registry.update_pool(rows[1].binding_id, state="retiring")
    fake.calls.clear()
    status = hot_pool.pool_status(pool.registry, [_profile(3), _profile(0, pid="off")])
    assert status == {
        "dev": {"target": 3, "ready": 1, "warming": 1},
        "off": {"target": 0, "ready": 0, "warming": 0},
    }
    assert fake.calls == []


def _handler_config(monkeypatch, tmp_path, profiles, coder_url=URL):
    from kiro_crew.config import loader, paths

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    section = SimpleNamespace(coder_url=coder_url, profiles=profiles)
    monkeypatch.setattr(
        loader.KiroCrewConfig,
        "load",
        classmethod(lambda cls: SimpleNamespace(session_environments=section)),
    )


@sync
async def test_slot_profiles_carry_pool_counts_only_for_pooled_profiles(tmp_path, monkeypatch):
    from kiro_crew.dashboard.handlers import session_environments as handlers

    await _pool(tmp_path, FakeCoder(), _config(_profile(1))).reconcile()
    _handler_config(monkeypatch, tmp_path, [_profile(1), _profile(0, pid="plain")])
    profiles = handlers._profiles()
    assert profiles[0]["pool"] == {"target": 1, "ready": 1, "warming": 0}
    assert "pool" not in profiles[1]
    # An unreadable registry drops the counts, never the profile list.
    (tmp_path / "coder" / "session-envs.json").write_text("{not json", encoding="utf-8")
    assert [p["id"] for p in handlers._profiles()] == ["dev", "plain"]
    assert "pool" not in handlers._profiles()[0]


@sync
async def test_pool_endpoint_is_owner_only_and_reports_counts(tmp_path, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import session_environments as handlers

    await _pool(tmp_path, FakeCoder(), _config(_profile(2))).reconcile()
    _handler_config(monkeypatch, tmp_path, [_profile(2)])
    owner = {"ok": True}

    async def gate(request, operation):
        assert operation == "session_environments.pool"
        return None if owner["ok"] else web.json_response({"code": "owner_only"}, status=403)

    monkeypatch.setattr(handlers, "require_owner_dashboard_request", gate)
    app = web.Application()
    app.router.add_get("/pool", handlers.api_session_environments_pool_get)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        body = await (await client.get("/pool")).json()
        assert body == {"profiles": {"dev": {"target": 2, "ready": 2, "warming": 0}}}
        owner["ok"] = False
        assert (await client.get("/pool")).status == 403
    finally:
        await client.close()


async def _endpoint(monkeypatch, handler, method, path, operation):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import session_environments as handlers

    owner = {"ok": True}

    async def gate(request, op):
        assert op == operation
        return None if owner["ok"] else web.json_response({"code": "owner_only"}, status=403)

    monkeypatch.setattr(handlers, "require_owner_dashboard_request", gate)
    app = web.Application()
    app.router.add_route(method, path, getattr(handlers, handler))
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, owner


@sync
async def test_readiness_check_is_read_only_and_owner_only(tmp_path, monkeypatch):
    from kiro_crew.coder import session_launch
    from kiro_crew.remote_session import git_gateway

    fake = FakeCoder()
    probed: list[str] = []

    async def probe(*, template, preset):
        probed.append(template)
        if template != "micro":
            raise CoderClientError("Coder template is unavailable")
        return {"owner": "devuser", "template": template}

    fake.probe = probe
    _handler_config(monkeypatch, tmp_path, [_profile(0), _profile(0, template="gone", pid="old")])
    monkeypatch.setattr(
        session_launch.CoderSessionEnvironments, "_client", lambda self: fake, raising=True
    )

    async def status(host):
        return "signed_in" if host == "github.com" else "unchecked"

    monkeypatch.setattr(git_gateway, "login_status", status)
    client, owner = await _endpoint(
        monkeypatch,
        "api_session_environments_check",
        "POST",
        "/check",
        "session_environments.check",
    )
    try:
        body = await (await client.post("/check")).json()
        assert body["coder"] == {"ok": True, "owner": "devuser"}
        assert body["profiles"] == [
            {"id": "dev", "ok": True},
            {"id": "old", "ok": False, "error": "Coder template is unavailable"},
        ]
        assert body["git"]["github.com"] == "signed_in"
        assert set(body["git"]) == set(git_gateway.HOSTS)
        # Read-only against Coder: no create, start, stop, delete, extend.
        assert fake.calls == [] and probed == ["micro", "gone"]
        owner["ok"] = False
        assert (await client.post("/check")).status == 403
    finally:
        await client.close()


@sync
async def test_readiness_check_reports_a_signed_out_gateway(tmp_path, monkeypatch):
    from kiro_crew.coder import session_launch
    from kiro_crew.remote_session import git_gateway

    class SignedOut(FakeCoder):
        async def current_user(self):
            raise CoderClientError("not logged in")

    _handler_config(monkeypatch, tmp_path, [_profile(0)])
    monkeypatch.setattr(
        session_launch.CoderSessionEnvironments, "_client", lambda self: SignedOut()
    )

    async def status(host):
        return "not_signed_in"

    monkeypatch.setattr(git_gateway, "login_status", status)
    client, _owner = await _endpoint(
        monkeypatch,
        "api_session_environments_check",
        "POST",
        "/check",
        "session_environments.check",
    )
    try:
        body = await (await client.post("/check")).json()
        assert body["coder"]["ok"] is False and "coder login" in body["coder"]["error"]
        assert body["profiles"] == []
    finally:
        await client.close()


@sync
async def test_workspace_list_comes_from_the_registry_alone(tmp_path, monkeypatch):
    fake = FakeCoder()
    await _pool(tmp_path, fake, _config(_profile(1))).reconcile()
    envs = _envs(tmp_path, fake, _config(_profile(0)))
    await envs.prepare("dashboard:chat-1", "dev")
    envs.registry.schedule_deletion("dashboard:chat-1", "2026-10-16T11:00:00Z")
    from kiro_crew.fargate_session.launch import SessionTask, SessionTaskRegistry

    task_id = "0123456789abcdef0123"
    SessionTaskRegistry(tmp_path / "fargate" / "session-tasks.json").put(
        SessionTask("dashboard:chat-9", "aws-dev", f"arn:task/{task_id}", task_id, "dev", "rt")
    )
    _handler_config(monkeypatch, tmp_path, [_profile(1)])
    fake.calls.clear()
    client, owner = await _endpoint(
        monkeypatch,
        "api_session_environments_workspaces_get",
        "GET",
        "/workspaces",
        "session_environments.workspaces",
    )
    try:
        rows = (await (await client.get("/workspaces")).json())["workspaces"]
        assert [(r["provider"], r["kind"]) for r in rows] == [
            ("fargate", "session"),
            ("coder", "session"),
            ("coder", "pool"),
        ]
        # The card labels a Fargate row, so the gateway sends the bare id.
        assert rows[0]["workspace"] == task_id[:12]
        assert rows[0]["slot"] == "chat-9" and rows[0]["state"] == "running"
        rows = rows[1:]
        assert rows[0]["slot"] == "chat-1"
        assert rows[0]["deletion_due_at"] == "2026-10-16T11:00:00Z"
        assert rows[1]["state"] == "ready"
        assert fake.calls == []  # no Coder command
        owner["ok"] = False
        assert (await client.get("/workspaces")).status == 403
    finally:
        await client.close()
