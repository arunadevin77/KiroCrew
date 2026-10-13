"""Kiro Cloud Sessions as a session environment.

A Kiro Cloud profile asks KAS to place the session in a Kiro cloud sandbox, using
the ``_meta.kiro`` keys kiro-cli's own ``--cloud`` flag sends on ``session/new``.
KAS forwards only the mode, the target and the repositories to that sandbox, so
Crew sends neither its agent spec nor its MCP servers, and it refuses to start
when KAS does not offer cloud sandboxes rather than quietly running locally.

The KAS stub records every request and advertises ``cloud-sandbox`` only when
the test asks it to, the way KAS gates it on its cloud link being configured.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from kiro_crew.acp import session_handle as sh
from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.config.session_environments import (
    SessionEnvironmentsError,
    load_kiro_cloud_profiles,
    validate_session_environments,
)

_STUB = """
import json, os, sys

RECORD = os.environ["KAS_STUB_RECORD"]
CLOUD = os.environ.get("KAS_STUB_CLOUD") == "1"
seen = []

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method != "initialize":
        seen.append({"method": method, "params": params})
        with open(RECORD, "w", encoding="utf-8") as fh:
            json.dump(seen, fh)
    if method == "initialize":
        kiro = {"executionTargets": ["local", "cloud-sandbox"] if CLOUD else ["local"],
                "sessionSources": ["local", "remote"] if CLOUD else ["local"]}
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion"),
            "agentCapabilities": {"loadSession": True, "_meta": {"kiro": kiro}},
        }})
    elif method in ("session/new", "session/load"):
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "sessionId": params.get("sessionId") or "cloud-session",
            "modes": {"currentModeId": "vibe",
                      "availableModes": [{"id": "vibe", "name": "vibe"}]},
        }})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
"""


@pytest.fixture(autouse=True)
def _fast_no_report_ceiling(monkeypatch):
    monkeypatch.setattr(sh, "_MCP_DRAIN_NO_REPORT_CEILING", 0.05)


@pytest.fixture
def stub(tmp_path, monkeypatch):
    script = tmp_path / "kas_cloud_stub.py"
    script.write_text(_STUB)
    record = tmp_path / "record.json"
    launcher = tmp_path / "kiro-cli-stub"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n')
    launcher.chmod(0o755)

    async def fake_bin(*, environ=None, home=None) -> str:
        return str(launcher)

    monkeypatch.setattr("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", fake_bin)
    monkeypatch.setenv("KAS_STUB_RECORD", str(record))
    monkeypatch.setenv("KAS_STUB_CLOUD", "1")
    return record


@pytest.fixture
def crew_agent(tmp_path, monkeypatch):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "kirocrew.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "description": "test crew agent",
                "prompt": "You are Kiro.",
                "tools": ["fs_read", "@kirocrew-core"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.config.paths.kiro_agents_dir", lambda: agents_dir)
    monkeypatch.setattr("kiro_crew.agent.ensure_agent_materialized", lambda _a: True)
    return agents_dir


def _runtime(tmp_path, repos=("acme/web",)) -> AcpRuntime:
    runtime = AcpRuntime(
        work_dir=tmp_path / "ws",
        agent="kirocrew",
        sandbox_mode="off",
        acp_backend=ACP_BACKEND_KAS,
    )
    runtime.set_cloud_sandbox(repos)
    return runtime


def _requests(record, method):
    return [r["params"] for r in json.loads(record.read_text()) if r["method"] == method]


@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
class TestCloudSessionWire:
    @pytest.mark.asyncio
    async def test_session_new_asks_for_a_cloud_sandbox_with_none_of_crews_surface(
        self, stub, crew_agent, tmp_path
    ):
        runtime = _runtime(tmp_path)
        try:
            await runtime.spawn()
            await runtime.create_session(cwd=tmp_path / "ws", agent="kirocrew")
        finally:
            await runtime.kill()
        [params] = _requests(stub, "session/new")
        kiro = params["_meta"]["kiro"]
        assert kiro["executionTarget"] == {"kind": "cloud-sandbox"}
        assert kiro["sessionSource"] == "remote"
        assert kiro["repositories"] == ["acme/web"]
        assert "isEmptyWorkspace" not in kiro
        # The sandbox would drop these, so they are never sent.
        assert "customAgents" not in kiro
        assert params["mcpServers"] == []
        # Crew's agent is not registered there, so it is never activated.
        assert _requests(stub, "session/set_mode") == []

    @pytest.mark.asyncio
    async def test_no_repositories_opens_an_empty_workspace(self, stub, crew_agent, tmp_path):
        runtime = _runtime(tmp_path, repos=())
        try:
            await runtime.spawn()
            await runtime.create_session(cwd=tmp_path / "ws")
        finally:
            await runtime.kill()
        [params] = _requests(stub, "session/new")
        assert params["_meta"]["kiro"]["isEmptyWorkspace"] is True
        assert "repositories" not in params["_meta"]["kiro"]

    @pytest.mark.asyncio
    async def test_refused_when_kas_offers_no_cloud_sandbox(
        self, stub, crew_agent, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("KAS_STUB_CLOUD", "0")
        runtime = _runtime(tmp_path)
        try:
            await runtime.spawn()
            with pytest.raises(AcpRuntimeError, match="not available"):
                await runtime.create_session(cwd=tmp_path / "ws")
        finally:
            await runtime.kill()
        # Refused before anything went out: never a local session in its place.
        assert not stub.exists() or _requests(stub, "session/new") == []

    @pytest.mark.asyncio
    async def test_session_load_reopens_from_the_cloud_store(self, stub, crew_agent, tmp_path):
        runtime = _runtime(tmp_path)
        try:
            await runtime.spawn()
            await runtime.load_session("", "cloud-1", cwd=tmp_path / "ws", agent="kirocrew")
        finally:
            await runtime.kill()
        [params] = _requests(stub, "session/load")
        assert params["sessionId"] == "cloud-1"
        assert params["_meta"] == {"kiro": {"sessionSource": "remote"}}
        assert params["mcpServers"] == []
        assert _requests(stub, "session/set_mode") == []


def test_cloud_sandbox_needs_kas_and_an_unspawned_runtime(tmp_path):
    kiro = AcpRuntime(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_KIRO)
    with pytest.raises(AcpRuntimeError, match="KAS"):
        kiro.set_cloud_sandbox(())
    assert kiro._cloud_sandbox is None


def _section(**extra):
    return {"coder_url": "", "profiles": [], **extra}


def test_config_accepts_and_normalizes_a_cloud_profile():
    out = validate_session_environments(
        _section(
            kiro_cloud_profiles=[
                {"id": "cloud", "name": " Kiro Cloud ", "repositories": ["acme/web", "acme/web"]}
            ],
            default_profile="cloud",
        )
    )
    assert out["kiro_cloud_profiles"] == [
        {"id": "cloud", "name": "Kiro Cloud", "repositories": ["acme/web"]}
    ]
    assert out["default_profile"] == "cloud"


@pytest.mark.parametrize(
    "profile",
    [
        {"id": "c", "name": "C", "repositories": ["a b"]},
        {"id": "c", "name": "C", "repositories": ["--flag"]},
        {"id": "c", "name": "C", "repositories": ["a/b/c"]},
        {"id": "c", "name": "C", "repositories": "acme/web"},
        {"id": "c", "name": "", "repositories": []},
        {"id": "c", "name": "C", "repositories": [], "token": "x"},
    ],
)
def test_config_refuses_a_malformed_cloud_profile(profile):
    with pytest.raises(SessionEnvironmentsError, match=r"kiro_cloud_profiles\[0\]"):
        validate_session_environments(_section(kiro_cloud_profiles=[profile]))


def test_config_refuses_an_id_shared_with_another_provider():
    coder = {"id": "x", "name": "X", "template": "t", "workdir": "/w", "hot_pool": 0}
    with pytest.raises(SessionEnvironmentsError, match="Kiro Cloud"):
        validate_session_environments(
            {
                "coder_url": "https://coder.example.com",
                "profiles": [coder],
                "kiro_cloud_profiles": [{"id": "x", "name": "X", "repositories": []}],
            }
        )


def test_load_keeps_valid_cloud_profiles_and_drops_bad_ones():
    raw = {
        "kiro_cloud_profiles": [
            {"id": "ok", "name": "OK", "repositories": []},
            {"id": "bad", "name": "Bad", "repositories": ["no spaces allowed"]},
        ]
    }
    assert [p["id"] for p in load_kiro_cloud_profiles(raw)] == ["ok"]


@pytest.mark.asyncio
async def test_prepare_returns_a_cloud_session_without_touching_any_compute(tmp_path, monkeypatch):
    from kiro_crew.coder import session_launch as sl

    section = SimpleNamespace(
        kiro_cloud_profiles=[{"id": "cloud", "name": "Kiro Cloud", "repositories": ["acme/web"]}],
        fargate_profiles=[],
        sprites_profiles=[],
        boxd_profiles=[],
    )
    monkeypatch.setattr("kiro_crew.config.paths.data_home", lambda: tmp_path)
    monkeypatch.setattr(
        "kiro_crew.config.live.snapshot", lambda: SimpleNamespace(session_environments=section)
    )
    sl.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json").set("s1", "cloud")

    def no_compute(*_a, **_k):
        raise AssertionError("a Kiro Cloud session provisions nothing")

    monkeypatch.setattr(sl, "CoderSessionEnvironments", no_compute)
    checked = []
    prepared = await sl.prepare_for_session("s1", before_compute=lambda: checked.append(1))
    assert prepared == sl.PreparedCloudSession("cloud", "Kiro Cloud", ("acme/web",))
    assert checked == [1]
