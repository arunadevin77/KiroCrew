"""Gateway and job builders shared by the ``test_cron_gateway_integration*.py`` modules.

``test_cron_gateway_integration`` re-exports ``_make_gw``, ``_make_command_job``,
``_run_command_callback``, ``_make_gw_for_llm`` and ``_run_llm_callback``, which other
test modules import from it.
"""

from __future__ import annotations

import contextlib
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

from kiro_crew.cron import CronJob, CronSchedule


def _make_gw():
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.ctx_builder = MagicMock()
    gw.slack = MagicMock()
    gw.conv_log = None
    gw.dashboard_state = MagicMock()
    gw.dashboard_state.get_slot = MagicMock(return_value=None)
    gw._owner_id = "U000"
    gw.subagent_mgr = None
    gw._cron_injecting = {}
    gw._cron_session_binding_overflow_count = 0
    gw._running_script_ids = set()
    gw._no_crons = False
    gw.cron_svc = MagicMock()
    gw.cron_svc.remove_job_async = AsyncMock(return_value=True)
    gw.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    gw.sessions.release = MagicMock()
    gw.sessions.reset = AsyncMock()
    gw.sessions.set_thread = AsyncMock()
    gw.sessions.set_channel = AsyncMock()
    gw.ctx_builder.build_message = MagicMock(return_value=("msg", None))
    gw.ctx_builder.hooks = MagicMock()
    gw._interactive_approval = MagicMock(return_value="cb")
    return gw


def _make_script_job(**overrides):
    defaults = dict(
        id="sj1",
        name="script-job",
        message="CR-123",
        schedule=CronSchedule(kind="every", every_secs=60),
        script="~/.kirocrew/crons/monitor.py:run",
    )
    defaults.update(overrides)
    return CronJob(**defaults)


def _make_command_job(**overrides):
    defaults = dict(
        id="cj1",
        name="cmd-job",
        message="",
        schedule=CronSchedule(kind="every", every_secs=60),
        command="echo hello",
    )
    defaults.update(overrides)
    return CronJob(**defaults)


async def _run_script_callback(gw, job, script_result=None, vet_reason=None, side_effect=None):
    """Run the cron callback with a mocked run_script_sandboxed result.

    ``vet_reason`` feeds the fire-time governance gate (None = job may run);
    patching vet_job_at_fire_time also stands in for the script-path
    resolution it performs, which the removed gateway-level
    resolve_script_path call also covered.

    Pass ``side_effect`` to make the mocked call raise instead of returning.
    """
    captured_cb = None
    mock_kw = (
        {"side_effect": side_effect} if side_effect is not None else {"return_value": script_result}
    )

    with (
        patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
        patch("kiro_crew.slack.gateway.run_script_sandboxed", **mock_kw) as mock_run,
        patch("kiro_crew.slack.gateway.vet_job_at_fire_time", return_value=vet_reason),
        patch("kiro_crew.slack.gateway.sel"),
    ):

        def capture_cron(on_job=None, **kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            svc.remove_job_async = AsyncMock(return_value=True)
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

        async def _init_and_run():
            await gw._init_cron()
            assert captured_cb is not None
            return await captured_cb(job)

        return await _init_and_run(), mock_run


async def _run_command_callback(gw, job, cmd_result=None, side_effect=None, vet_reason=None):
    """Run the cron callback with a mocked run_command_sandboxed result.

    Pass ``side_effect`` to make the mocked call raise instead of returning.
    ``vet_reason`` feeds the fire-time governance gate (None = job may run),
    mirroring _run_script_callback.
    """
    captured_cb = None
    mock_kw = (
        {"side_effect": side_effect} if side_effect is not None else {"return_value": cmd_result}
    )
    # Only stand in for the gate when simulating a denial: other tests here drive
    # the REAL gate, and patching it unconditionally would silence them.
    gate = (
        patch("kiro_crew.slack.gateway.vet_job_at_fire_time", return_value=vet_reason)
        if vet_reason is not None
        else nullcontext()
    )

    with (
        patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
        patch("kiro_crew.slack.gateway.run_command_sandboxed", **mock_kw) as mock_run,
        gate,
        patch("kiro_crew.slack.gateway.sel"),
    ):

        def capture_cron(on_job=None, **kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            svc.remove_job_async = AsyncMock(return_value=True)
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

        async def _init_and_run():
            await gw._init_cron()
            assert captured_cb is not None
            return await captured_cb(job)

        return await _init_and_run(), mock_run


def _make_llm_job(**overrides):
    """LLM-based cron job (no script, no command) with optional model override."""
    defaults = dict(
        id="lj1",
        name="llm-job",
        message="Run daily check",
        schedule=CronSchedule(kind="every", every_secs=3600),
    )
    defaults.update(overrides)
    return CronJob(**defaults)


def _make_gw_for_llm():
    """Extended _make_gw with attributes the LLM single-agent path needs."""
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.ctx_builder = MagicMock()
    gw.slack = None  # suppress Slack delivery
    gw.conv_log = None
    gw.dashboard_state = MagicMock()
    gw.dashboard_state.get_slot = MagicMock(return_value=None)
    gw.dashboard_state.has_slot = MagicMock(return_value=False)
    gw.dashboard_state.notify = MagicMock()
    gw._owner_id = "U000"
    gw.subagent_mgr = None
    gw._cron_injecting = {}
    gw._cron_session_binding_overflow_count = 0
    gw._running_script_ids = set()
    gw._no_crons = False
    gw.cron_svc = MagicMock()
    gw.cron_svc.remove_job_async = AsyncMock(return_value=True)
    gw._cfg = MagicMock()
    gw._cfg.agent.provider = "acp"
    gw._cfg.hooks = {}
    gw._approval_mode = None
    gw.sessions.release = MagicMock()
    gw.sessions.reset = AsyncMock()
    gw.sessions.set_thread = AsyncMock()
    gw.sessions.set_channel = AsyncMock()
    gw.sessions.get_channel = MagicMock(return_value=None)
    gw.ctx_builder.build_message = MagicMock(return_value=("full prompt", None))
    gw.ctx_builder.hooks = MagicMock()
    gw._interactive_approval = MagicMock(return_value="cb")
    return gw


async def _run_llm_callback(
    gw,
    job,
    *,
    get_or_create_side_effect=None,
    fires=1,
    sel_factory=None,
    real_session_context=False,
    embed_calls=None,
):
    """Run the cron callback for an LLM-based job through _init_cron.

    get_or_create_side_effect: if provided, set as the side_effect on
    sessions.get_or_create (for simulating model errors / fallback).
    fires: how many times to run the ONE callback the gateway built, as one
    gateway process fires a job on successive schedule slots; the last result
    is returned.
    real_session_context: build the prompt with the real
    ``build_cron_session_context`` instead of the bare-message stub.
    embed_calls: a list that receives the ``run_in_embed_pool`` mock, so a caller
    can read the prompt and ``compressed_history`` the fire built.
    """
    captured_cb = None

    if get_or_create_side_effect is not None:
        gw.sessions.get_or_create = AsyncMock(side_effect=get_or_create_side_effect)
    else:
        provider_mock = MagicMock()
        gw.sessions.get_or_create = AsyncMock(return_value=(provider_mock, True, False))

    _embed_mock = AsyncMock(return_value=("full prompt", None))
    _stream_mock = AsyncMock(return_value="Agent response here")
    sel_patch = (
        patch("kiro_crew.slack.gateway.sel", new=sel_factory)
        if sel_factory is not None
        else patch("kiro_crew.slack.gateway.sel")
    )

    with (
        patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
        patch("kiro_crew.slack.gateway.run_in_embed_pool", _embed_mock),
        patch("kiro_crew.slack.gateway.stream_and_collect", _stream_mock),
        sel_patch,
        patch("kiro_crew.slack.gateway.build_cron_session_context") as mock_ctx,
    ):

        if real_session_context:
            from kiro_crew.cron_service.identity import build_cron_session_context

            mock_ctx.side_effect = build_cron_session_context
        else:
            mock_ctx.return_value = (f"cron:{job.id}", job.message)
        if embed_calls is not None:
            embed_calls.append(_embed_mock)

        def capture_cron(on_job=None, **kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            svc.update_job_async = AsyncMock()
            svc.remove_job_async = AsyncMock(return_value=True)
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)
        await gw._init_cron()
        assert captured_cb is not None
        for _ in range(fires):
            result = await captured_cb(job)
        return result, _stream_mock


@contextlib.contextmanager
def _resolved_binding():
    """Patch the two calls every fire-time agent-resolution test stubs: same
    two patches, same nesting order, same lifetime as the inlined ``with``
    block, just given a name. Yields the ``resolve_agent_bindings`` mock for
    the caller to configure; ``warm_project_agent_names`` has no reader."""
    with (
        patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve,
        patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
    ):
        yield mock_resolve


def _set_alias_pinned_model_binding(mock_resolve):
    """Shared resolved-alias binding for the two alias-model-precedence
    tests (``..._is_not_dropped`` / ``..._outranks_the_alias_model``)."""
    mock_resolve.return_value.requested_resolved = True
    mock_resolve.return_value.kiro_agent = "raw-kiro-agent-name"
    mock_resolve.return_value.model = "alias-pinned-model"
    mock_resolve.return_value.memory_store_name = ""
    mock_resolve.return_value.execution_context = None
    mock_resolve.return_value.resolved_alias = "ea-dev"
    mock_resolve.return_value.resolved_source = "alias"


def _set_project_resolved_ea_dev_binding(mock_resolve):
    """Shared resolved-project ``ea-dev`` binding for the two control tests."""
    mock_resolve.return_value.requested_resolved = True
    mock_resolve.return_value.kiro_agent = "ea-dev"
    mock_resolve.return_value.model = ""
    mock_resolve.return_value.memory_store_name = ""
    mock_resolve.return_value.execution_context = None
    mock_resolve.return_value.resolved_alias = None
    mock_resolve.return_value.resolved_source = "project"


def _set_member_template_alias_binding(mock_resolve):
    """Shared resolved-member-template-alias binding; callers keep their own
    ``with`` patch tuple (count/order varies) and just configure it here."""
    mock_resolve.return_value.requested_resolved = True
    mock_resolve.return_value.kiro_agent = "member-template"
    mock_resolve.return_value.model = ""
    mock_resolve.return_value.resolved_alias = "someone-else"
    mock_resolve.return_value.resolved_source = "alias"
    mock_resolve.return_value.memory_store_name = ""
    mock_resolve.return_value.execution_context = None


async def _run_llm_callback_unresolved_default(gw, job):
    """Fire through ``_run_llm_callback`` with ``resolve_agent_bindings``
    stubbed as unresolved-default -- the four call sites below repeat this
    one-patch setup verbatim."""
    with patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve:
        mock_resolve.return_value.requested_resolved = False
        mock_resolve.return_value.resolved_source = "default"
        return await _run_llm_callback(gw, job)


def _make_gw_for_llm_with_update_job():
    """``_make_gw_for_llm()`` plus the ``update_job_async`` override seven
    skip-path tests below each repeat verbatim."""
    gw = _make_gw_for_llm()
    gw.cron_svc.update_job_async = AsyncMock()
    return gw


def _recording_cron_provider_factory(created: list[tuple[object, str | None, str | None]]):
    """Build live providers while recording the binding used for each allocation."""

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = MagicMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.memory_mode = kwargs.get("memory_mode", "persistent")
        provider.is_process_alive = lambda: True
        provider.context_usage_pct = lambda: 0.0
        provider.context_window_tokens = lambda: 0
        provider.has_active_turn = lambda: False
        provider.runtime_info = lambda: (None, None)
        created.append((provider, agent, kwargs.get("cwd")))
        return provider

    return factory
