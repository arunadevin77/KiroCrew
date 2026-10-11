"""Member harness choices use the current execution context and run record."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_spawn_backend import TestFollowUps as _FollowUps
from test_spawn_backend import _mgr

from kiro_crew import model_registry, subagent_persistence
from kiro_crew.config import loader
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    resolve_agent_bindings,
    resolve_agent_identity,
)
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.subagent import SubagentInfo, _capture_member_settings

pytestmark = pytest.mark.usefixtures("healthy_host_memory", "close_subagent_managers")


@pytest.fixture
def member_config(monkeypatch):
    cfg = KiroCrewConfig()
    cfg.agent.model = "global-choice"
    cfg.agent.role_models = {"subagent": "role-choice"}
    cfg.agent.reasoning_effort = ""
    cfg.agents["worker"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew",
        acp_backend="codex",
        model="member-choice",
        reasoning_effort="high",
    )
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(
        KiroCrewConfig, "_resolve_named_agent_model", lambda *args: "template-choice"
    )
    return cfg


@pytest.fixture
def member_execution():
    return ExecutionContext(
        member_id=None,
        store=MemoryStoreRef("default"),
        selection_kind="member",
        template_id="kirocrew",
        selection_name="worker",
    )


@pytest.mark.parametrize("pin,expected", [(None, "claude"), ("", ""), ("codex", "codex")])
def test_inherit_and_explicit_kiro_are_distinct(member_config, pin, expected):
    member_config.agent.acp_backend = "claude"
    member_config.agents["worker"].acp_backend = pin
    assert member_config.resolve_session_backend(crew_agent="worker") == expected
    assert member_config.resolve_session_backend(crew_agent="worker", backend_override="") == ""


def test_template_selection_does_not_take_a_same_named_member_pin(member_config):
    assert member_config.resolve_session_backend(agent="worker", crew_agent="") == ""


@pytest.fixture
def readout_config(monkeypatch):
    cfg = KiroCrewConfig.load()
    assert "default" in cfg.agents
    cfg.default_agent = "default"
    cfg.agent.acp_backend = "claude"
    cfg.agent.member_acp_backend = "codex"
    cfg.agent.model = "global-choice"
    cfg.agent.role_models = {}
    cfg.agent.reasoning_effort = ""
    cfg.agents["default"].kiro_agent = "kirocrew"
    cfg.agents["default"].acp_backend = None
    cfg.agents["default"].model = "member-choice"
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(KiroCrewConfig, "_resolve_agent_model", lambda *_: "global-choice")
    monkeypatch.setattr(KiroCrewConfig, "_resolve_named_agent_model", lambda *_: "template-choice")
    monkeypatch.setattr(loader, "_materialized_kiro_agent", lambda name, *_: name if name else "")
    monkeypatch.setattr(
        model_registry,
        "_ADVERTISED_MODELS",
        {"claude_code": ["global-choice", "template-choice"], "codex": ["member-choice"]},
    )
    return cfg


async def _readout(agent, **context):
    from kiro_crew.dashboard.handlers import api_kirocrew_agent_resolved_model

    app = web.Application()
    app.router.add_get("/api/agents/resolved-model", api_kirocrew_agent_resolved_model)
    async with TestClient(TestServer(app)) as client:
        response = await asyncio.wait_for(
            client.get("/api/agents/resolved-model", params={"agent": agent, **context}),
            timeout=10,
        )
        assert response.status == 200
        return await asyncio.wait_for(response.json(), timeout=10)


def _readout_factory(cfg, monkeypatch, tmp_path, agent, kind, session_key):
    """Run real binding and factory selection, replacing only the provider boundary."""
    from kiro_crew.providers import acp

    bindings = resolve_agent_bindings(cfg, agent, selection_kind=kind, validate_memory_files=False)
    crew = "" if kind == "template" else bindings.resolved_alias
    provider = MagicMock()
    monkeypatch.setattr(acp, "AcpProvider", provider)
    cfg.create_provider_factory()(
        session_key=session_key,
        agent=bindings.kiro_agent,
        crew_agent=crew,
        model_override=bindings.model or None,
        cwd=str(tmp_path),
    )
    return provider.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_key,kind,backend_pin,model_pin,expected_backend,expected_model",
    [
        pytest.param(None, "", None, "member-choice", "codex", "member-choice", id="preview"),
        pytest.param(
            "dashboard:member-default",
            "member",
            None,
            "member-choice",
            "codex",
            "member-choice",
            id="member-dm",
        ),
        pytest.param(
            "slot-a",
            "member",
            None,
            "member-choice",
            "claude",
            "global-choice",
            id="ordinary-member",
        ),
        pytest.param(
            "",
            "member",
            None,
            "member-choice",
            "claude",
            "global-choice",
            id="settings",
        ),
        pytest.param(
            "slot-a",
            "member",
            "codex",
            "member-choice",
            "codex",
            "member-choice",
            id="explicit-member-backend",
        ),
        pytest.param(
            "slot-a",
            "member",
            None,
            "",
            "claude",
            "global-choice",
            id="no-model-pin",
        ),
        pytest.param(
            "slot-a",
            "",
            None,
            "member-choice",
            "claude",
            "global-choice",
            id="existing-selection-resolution",
        ),
        pytest.param(
            "member-default",
            "template",
            "claude",
            "",
            "codex",
            "",
            id="template-on-dm-route",
        ),
    ],
)
async def test_model_readout_uses_the_session_route(
    readout_config,
    monkeypatch,
    tmp_path,
    session_key,
    kind,
    backend_pin,
    model_pin,
    expected_backend,
    expected_model,
):
    cfg = readout_config
    cfg.agents["default"].acp_backend = backend_pin
    cfg.agents["default"].model = model_pin
    context = {} if session_key is None else {"session_key": session_key, "selection_kind": kind}
    body = await _readout("default", **context)
    actual = _readout_factory(
        cfg,
        monkeypatch,
        tmp_path,
        "default",
        kind,
        session_key if session_key is not None else "member-default",
    )
    assert (body["resolved_acp_backend"], body["model"]) == (expected_backend, expected_model)
    assert (actual["acp_backend"], actual["model"]) == (expected_backend, expected_model)
    assert body["pinned"] is (bool(model_pin) and kind != "template")
    assert body["inherited_acp_backend"] == (
        "codex" if session_key is None or "member-default" in session_key else "claude"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("template", ["default", "worker", "shared-template"])
async def test_template_readout_excludes_member_model_and_effort_pins(
    readout_config,
    monkeypatch,
    tmp_path,
    template,
):
    cfg = readout_config
    cfg.agent.member_acp_backend = "claude"
    cfg.agents["default"].acp_backend = "claude"
    cfg.agents["default"].reasoning_effort = "max"
    cfg.agents["worker"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew",
        model="other-member-choice",
        acp_backend="claude",
        reasoning_effort="high",
    )
    # All pins are native to one namespace: scope filtering cannot hide a
    # default/same-named member pin mistakenly treated as the template's pin.
    monkeypatch.setattr(
        model_registry,
        "_ADVERTISED_MODELS",
        {
            "claude_code": [
                "member-choice",
                "other-member-choice",
                "template-choice",
                "global-choice",
            ],
        },
    )
    alias, _, model_pin = resolve_agent_identity(cfg, template, selection_kind="template")
    assert alias == "default" and model_pin == ""
    body = await _readout(template, session_key="slot-a", selection_kind="template")
    actual = _readout_factory(cfg, monkeypatch, tmp_path, template, "template", "slot-a")
    assert body["model"] == actual["model"] == "template-choice"
    assert body["resolved_acp_backend"] == actual["acp_backend"] == "claude"
    assert actual["crew_agent"] == ""
    assert body["pinned"] is False
    assert body["reasoning_effort"] == ""
    assert body["effort_pinned"] is False


def test_member_pin_wins_over_role_and_global(member_config, member_execution):
    assert _capture_member_settings(member_execution, None, None, "") == (
        "codex",
        "member-choice",
        "high",
        True,
    )


def test_explicit_auto_and_effort_win(member_config, member_execution):
    assert _capture_member_settings(member_execution, "", "auto", "low") == (
        "",
        "auto",
        "low",
        True,
    )


def test_foreign_member_does_not_inherit_template_global_or_role_models(
    member_config, member_execution
):
    member_config.agents["worker"].model = ""
    member_config.agents["worker"].reasoning_effort = ""
    assert _capture_member_settings(member_execution, None, None, "") == (
        "codex",
        "auto",
        "",
        True,
    )


def test_template_keeps_member_memory_without_member_execution_defaults(
    member_config, member_execution
):
    template = member_execution.with_template("worker", "worker")
    assert _capture_member_settings(template, None, None, "") == (None, None, "", False)


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_model", ["", "template-choice"])
@pytest.mark.parametrize("edit_source", ["member-profile", "member-default"])
@pytest.mark.parametrize("restart", ["failed-compaction", "queued-reset"])
async def test_member_live_reuse_and_captured_restarts_keep_backend_after_defaults_change(
    monkeypatch, member_config, member_execution, configured_model, edit_source, restart
):
    from test_session_pool import _make_manager, _make_provider

    from kiro_crew.members import member_thread_session_alias
    from kiro_crew.session_capabilities import CapabilityPreparation
    from kiro_crew.session_lifecycle import allocation_identity

    member_config.agent.model = "auto"
    member_config.agent.acp_backend = ""
    member_config.agent.member_acp_backend = ""
    member_config.agents["worker"].acp_backend = None
    member_config.agents["worker"].model = ""
    manager, factory = _make_manager(pool_size=0)
    manager._cfg = member_config
    manager._ensure_cleanup_task = MagicMock()
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution", lambda *a: member_execution
    )
    monkeypatch.setattr(
        "kiro_crew.session_capabilities.prepare_runtime",
        lambda *a: CapabilityPreparation(member="worker", template="kirocrew"),
    )
    started = []

    def create(key, **kwargs):
        provider = _make_provider()
        backend = member_config.resolve_session_backend(
            key, kwargs.get("agent"), kwargs.get("crew_agent"), kwargs.get("backend_override")
        )
        provider.client.backend = backend
        provider._configured_model = kwargs.get("model_override") or configured_model
        started.append((backend, kwargs.get("model_override")))
        return provider

    factory.side_effect = create
    key = member_thread_session_alias("worker")
    try:
        await asyncio.wait_for(
            manager.get_or_create(key, agent="kirocrew", crew_agent="worker"), timeout=10
        )
        manager.release(key)
        original = manager._sessions[key]
        if edit_source == "member-profile":
            member_config.agents["worker"].acp_backend = "codex"
            member_config.agents["worker"].model = "new-member-choice"
        else:
            member_config.agent.member_acp_backend = "codex"
            assert member_config.agents["worker"].acp_backend is None
        fresh_key = member_thread_session_alias("worker-new")
        assert (
            member_config.resolve_session_backend(fresh_key, agent="kirocrew", crew_agent="worker")
            == "codex"
        )

        live, _, _ = await asyncio.wait_for(
            manager.get_or_create(key, agent="kirocrew", crew_agent="worker"), timeout=10
        )
        manager.release(key)
        assert live is original.provider
        assert live.client.backend == ""
        assert started == [("", None)]

        identity = allocation_identity(manager, key, original)
        assert identity["backend_override"] == ""
        expected_model = configured_model or "auto"
        assert identity["model"] == expected_model
        if restart == "queued-reset":
            assert manager.enqueue(key, "follow-up", "continue the work", force=True)
            assert await asyncio.wait_for(manager.reset(key), timeout=10)
            # Reset schedules the queued-work successor on the manager's task set.
            await asyncio.wait_for(asyncio.gather(*tuple(manager._background_tasks)), timeout=10)
            assert [entry[0] for entry in manager._sessions[key].queue] == ["follow-up"]
            assert not manager._sessions[key].semaphore.locked()
        else:
            # A failed-compaction restart re-enters allocation with the captured identity.
            manager._recycling[key] = original
            manager._session_map.clear_sid(key)
            await asyncio.wait_for(
                manager.get_or_create(key, **identity, speculative=True), timeout=10
            )
            manager.release(key)
        successor = manager._sessions[key]
        assert successor is not original
        assert successor.provider.client.backend == ""
        assert successor.requested_model == expected_model
        assert started == [("", None), ("", expected_model)]

        fresh, _, _ = await asyncio.wait_for(
            manager.get_or_create(fresh_key, agent="kirocrew", crew_agent="worker"),
            timeout=10,
        )
        manager.release(fresh_key)
        assert fresh.client.backend == "codex"
        assert [backend for backend, _ in started] == ["", "", "codex"]
    finally:
        await asyncio.wait_for(manager.close_all(), timeout=10)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["", "other-template"])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_spawn_captures_even_when_execution_context_is_already_supplied(
    monkeypatch, member_config, member_execution, agent, asynchronous
):
    mgr = _mgr()
    mgr._spawn_stagger_secs = 0
    monkeypatch.setattr(mgr, "_run", AsyncMock())
    params = dict(
        agent=agent,
        _execution_context=member_execution.to_record(),
        _parent_spawn_policy=("", ()),
        _agent_prevalidated=True,
    )
    info = (
        await asyncio.wait_for(mgr.spawn_async("work", **params), timeout=10)
        if asynchronous
        else mgr.spawn("work", **params)
    )
    assert info is not None and not info.error
    expected = (None, "", "", False) if agent else ("codex", "member-choice", "high", True)
    assert (
        info.backend,
        info.model,
        info.reasoning_effort,
        info.execution_settings_captured,
    ) == expected
    assert info.execution_context == (
        member_execution.with_template(agent, agent) if agent else member_execution
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["removed", "io"])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_capture_failure_is_one_counted_refusal(
    monkeypatch, member_config, member_execution, failure, asynchronous
):
    from kiro_crew import subagent

    def fail(*args):
        if failure == "io":
            raise OSError("capture read failed")
        member_config.agents.clear()
        return _capture_member_settings(*args)

    monkeypatch.setattr(subagent, "_capture_member_settings", fail)
    mgr = _mgr()
    run = AsyncMock()
    announce = MagicMock(wraps=mgr._announce_rejection)
    monkeypatch.setattr(mgr, "_run", run)
    monkeypatch.setattr(mgr, "_announce_rejection", announce)
    params = dict(
        _execution_context=member_execution.to_record(),
        _parent_spawn_policy=("", ()),
        _agent_prevalidated=True,
        batch_id="capture-batch",
        batch_total=1,
    )
    info = (
        await asyncio.wait_for(mgr.spawn_async("work", **params), timeout=10)
        if asynchronous
        else mgr.spawn("work", **params)
    )
    assert info.done and info.error.startswith("memory_unavailable:")
    assert mgr._batch_submitted["capture-batch"] == [1, 1]
    announce.assert_called_once()
    run.assert_not_called()
    assert not mgr._queue


def test_queue_retains_choices_after_member_settings_change(
    monkeypatch, member_config, member_execution
):
    mgr = _mgr()
    monkeypatch.setattr(mgr, "_should_stagger_queue", lambda *args: (True, False))
    info = mgr.spawn(
        "queued",
        _execution_context=member_execution.to_record(),
        _parent_spawn_policy=("", ()),
        _agent_prevalidated=True,
    )
    assert info is not None and info.queued
    params = mgr._queue[0]
    member_config.agents["worker"].acp_backend = ""
    member_config.agents["worker"].model = "changed"
    assert (params["backend"], params["model"], params["reasoning_effort"]) == (
        "codex",
        "member-choice",
        "high",
    )
    assert params["_execution_settings_captured"] is True
    assert (info.backend, info.model, info.reasoning_effort) == ("codex", "member-choice", "high")


@pytest.mark.parametrize("cold", [False, True])
def test_continuation_keeps_captured_auto_and_empty_effort(
    monkeypatch, tmp_path, member_config, cold
):
    monkeypatch.setattr(subagent_persistence, "_subagents_dir", lambda: tmp_path)
    mgr = _mgr()
    info = SubagentInfo(
        id="original",
        task="work",
        backend="codex",
        model="auto",
        reasoning_effort="",
        execution_settings_captured=True,
        done=True,
    )
    if cold:
        subagent_persistence.create_agent_folder(
            info.id,
            task=info.task,
            backend=info.backend,
            model=info.model,
            reasoning_effort=info.reasoning_effort,
            execution_settings_captured=info.execution_settings_captured,
        )
    else:
        mgr._agents[info.id] = info
    member_config.agent.reasoning_effort = "max"
    captured = _FollowUps()._continue(mgr, monkeypatch, info.id)
    assert (captured["backend"], captured["model"], captured["reasoning_effort"]) == (
        "codex",
        "auto",
        "",
    )
    assert captured["_execution_settings_captured"] is True


@pytest.mark.asyncio
async def test_backend_change_requires_explicit_choices_before_mutation(monkeypatch):
    from kiro_crew.dashboard.handlers import agents

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )
    cfg = KiroCrewConfig.load()
    cfg.agents["worker"] = KiroCrewAgentConfig(kiro_agent="kirocrew", description="original")
    cfg.save()
    app = web.Application()
    app.router.add_put("/api/agents/{name}", agents.api_kirocrew_agent_update)
    async with TestClient(TestServer(app)) as client:
        response = await asyncio.wait_for(
            client.put(
                "/api/agents/worker", json={"acp_backend": "codex", "description": "changed"}
            ),
            timeout=10,
        )
        assert response.status == 400
        body = await asyncio.wait_for(response.json(), timeout=10)
        assert body["code"] == "backend_choices_required"
        saved = KiroCrewConfig.load().agents["worker"]
        assert saved.acp_backend is None and saved.description == "original"
        response = await asyncio.wait_for(
            client.put(
                "/api/agents/worker",
                json={"acp_backend": "", "model": "auto", "reasoning_effort": ""},
            ),
            timeout=10,
        )
        assert response.status == 200
    assert KiroCrewConfig.load().agents["worker"].acp_backend == ""


@pytest.mark.parametrize("bare", [False, True])
def test_catalog_alias_remains_valid_after_live_wire_ids_arrive(monkeypatch, bare):
    from kiro_crew import model_registry
    from kiro_crew.dashboard.handlers import agents, core

    choice = next(
        row["model_name"]
        for row in model_registry.display_list("claude_code")
        if ".anthropic." in model_registry.to_provider_id(row["model_name"], "claude_code")
    )
    wire = model_registry.to_provider_id(choice, "claude_code")
    advertised = None
    monkeypatch.setattr(core, "_active_advertised_ids", lambda *args, **kwargs: advertised)
    assert agents._model_pin_rejected(choice, None, "acp", backend="claude") is None
    advertised = [wire.split(".anthropic.", 1)[-1] if bare else wire]
    assert agents._model_pin_rejected(choice, None, "acp", backend="claude") is None
    assert agents._model_pin_rejected("unavailable-choice", None, "acp", backend="claude")
