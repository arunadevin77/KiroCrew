"""Slack Socket Mode gateway orchestrator for KiroCrew.

Manages the lifecycle of all runtime services: session manager, cron
scheduler, context builder, heartbeat, subagents, task runner, dashboard,
and the Slack Socket Mode connection.

Event routing, interactive button handling, and allowlist management
live in sibling modules:

- ``events``        — Socket Mode event dispatch + dedup
- ``interactions``  — Block Kit button routing
- ``allowlist``     — tracking-channel join prompts + config persistence

The orchestrator's own responsibilities are composed from private owners in
``gateway_runtime``: tool-approval policy and the approval callbacks, cron
dispatch and run tallies, delivery routing and the result egress legs, the
channel lifecycle, the MCP broker, the memory lifecycle, service construction and
``run``, the heartbeat, admission, boot-time install repair, the update schedule
and apply, AutoNudge's wiring and fire paths, the exit path, and the process entry
point. This module stays their only import path and
patch surface, and their functions run on its globals, so an import no function
left here reads is still an owner's global or part of the module's name surface
(``test_gateway_runtime_bindings.py`` checks each is read). What stays here, and
why, is recorded in ``docs/system-specs/modules/slack-gateway.md`` (Composition).
"""

from __future__ import annotations

# Many imports here are the gateway_runtime owners' globals, so setup.cfg turns F401 off for
# this file: removing one fails test_every_global_an_owner_function_reads_is_bound_on_the_facade,
# not a call at runtime, and test_gateway_runtime_bindings.py checks that each import is read.
import asyncio
import contextlib
import functools
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import socket
import sys
import threading
import time
import uuid
import webbrowser
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web
from slack_sdk.socket_mode.websockets import SocketModeClient as WSSocketModeClient

import kiro_crew
import kiro_crew.crash_guard as crash_guard
from kiro_crew import (
    agent_scratch,
    autonudge_selfarm,
    beacon,
    dep_sync,
    name_grant,
    platform_compat,
    runtime_death,
    session_work_dir,
    shutdown_event,
    update_ownership,
    work_root,
)
from kiro_crew.acp.client import AcpError, AcpProcessDied
from kiro_crew.agent_sdk import AgentTurnUsage
from kiro_crew.agents_janitor import sweep_agents_dir
from kiro_crew.autonudge import (
    APPROVAL_STALL_REASON,
    CONSECUTIVE_FAILURE_REASON,
    FINISHED_LOOP_REASONS,
    MODEL_REFUSED_REASON,
    MONITOR_TERMINAL_REASON,
    STRUCTURAL_TERMINAL_REASON,
    AutoNudgeService,
    NudgeLoop,
)
from kiro_crew.autonudge import enabled as autonudge_enabled
from kiro_crew.autonudge import (
    is_channel_key,
    is_structured_monitor_loop,
    nudge_cycle_header,
    reason_in,
    runtime_budget_exceeded,
    terminal_notification_delivery_matches,
)
from kiro_crew.beacon import distribution
from kiro_crew.channel_history import ChannelHistory
from kiro_crew.channels import builtin_channel_descriptors
from kiro_crew.config import KiroCrewConfig, live
from kiro_crew.config.live import ConfigChange, ConfigDeferred, Subscription
from kiro_crew.config.loader import (
    CRED_DISCORD_BOT_TOKEN,
    CRED_FEISHU_APP_ID,
    CRED_FEISHU_APP_SECRET,
    CRED_MICROSOFT_APP_ID,
    CRED_MICROSOFT_APP_PASSWORD,
    CRED_MICROSOFT_APP_TENANT_ID,
    CRED_OWNER_ID,
    CRED_SLACK_APP_TOKEN,
    CRED_SLACK_BOT_TOKEN,
    CRED_TELEGRAM_BOT_TOKEN,
    CRED_WEBEX_BOT_TOKEN,
    CRED_WECOM_BOT_ID,
    CRED_WECOM_SECRET,
    CRED_WEIXIN_TOKEN,
    _session_work_dir,
    build_provider_factory,
    config_dir,
    data_home,
    workspace_root,
)
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.constants import (
    NO_TOOL_CALLS_NOTE,
    SUBAGENT_COMPLETION_META_KEY,
)
from kiro_crew.context import ContextBuilder, session_store_for_turn
from kiro_crew.context_management import summarize_result
from kiro_crew.cron import (
    _AUTO_PAUSE_THRESHOLD,
    _SUBPROC_CLEANUP_ALLOWANCE_SECS,
    CronJob,
    CronService,
    CronStoreBusy,
    CronStoreUnreadable,
    agent_sequence_dispatches,
    build_cron_session_context,
    effective_wake_budget,
)
from kiro_crew.cron_script import delivery_fingerprint, run_command_sandboxed, run_script_sandboxed
from kiro_crew.dashboard import cautious_boot, start_dashboard, token_secret
from kiro_crew.dashboard.chat_persistence import (
    rehydrate_slot_from_history_async,
    wait_for_unwinding_turns,
)
from kiro_crew.dashboard.chat_runner import (
    _arm_queued_delivery_settlement,
    _auto_approve_reason,
    _resolve_channel_target,
    _retire_sessions_on_identity_change,
    _run_chat,
    _slot_is_trusted,
    drain_idle_parent_queue_for_stall,
    turn_stats_meta,
)
from kiro_crew.dashboard.chat_utils import (
    CRON_NOTIFICATION_KIND,
    SUBAGENT_COMPLETION_KIND,
    _queued_depth_off_loop,
    dashboard_slot_key,
    mint_options_token,
    remember_slack_options,
    subagent_event_slot,
)
from kiro_crew.dashboard.cron_inject import (
    context_meter_reading,
    ensure_cron_slot,
    inject_cron_result_to_dashboard,
    prefetch_cron_history,
)
from kiro_crew.dashboard.handlers import MAX_PROMPT_BYTES
from kiro_crew.dashboard.handlers.autonudge import (
    _redact_monitor_value,
    compose_nudge_body,
    render_nudge_message,
)
from kiro_crew.dashboard.handlers.updates import _update_info, record_auto_update_effect
from kiro_crew.dashboard.handlers.updates import remediation_command as _remediation_command
from kiro_crew.dashboard.handlers.usage import (
    persist_token_record_async,
    read_context_tokens,
    read_effective_agent,
    read_turn_model,
)
from kiro_crew.dashboard.listener_guard import listener_guard_exit_code
from kiro_crew.dashboard.origin import (
    build_dashboard_url,
    format_dashboard_urls,
    is_local_only,
    parse_dashboard_url,
    resolve_dashboard_host,
)
from kiro_crew.dashboard.persistence_watchdog import (
    probe_data_home,
    run_persistence_watchdog,
)
from kiro_crew.dashboard.persistence_watchdog import (
    shutdown_exit_code as persistence_lost_exit_code,
)
from kiro_crew.dashboard.sign_in_refresher import drain_sign_in_refresher, run_sign_in_refresher
from kiro_crew.dashboard.stale_asset_watchdog import (
    run_stale_asset_watchdog,
    shutdown_exit_code,
)
from kiro_crew.dashboard.stale_bundle_guard import check_bundle_freshness
from kiro_crew.dashboard.state import (
    SUBAGENT_BATCH_COMPLETION_PREFIX,
    SUBAGENT_COMPLETION_PREFIX,
    DashboardState,
    load_window_off_loop,
)
from kiro_crew.dashboard.token_auth import MAX_SESSION_TTL_SECS, generate_token
from kiro_crew.dashboard.turn_dispatch import bounded_chat_turn, spawn_guarded_turn
from kiro_crew.embeddings import (
    embedding_model_is_custom,
    get_shared_embedder,
    make_sync_embed_fn,
    model_file_present,
    peek_ready_shared_embedder,
    reconcile_store_embedding_space,
    reembed_progress,
    start_background_model_download,
    store_embedding_space_is_stale,
)
from kiro_crew.executors import (
    CronQueueTimeout,
    configure_default_executor,
    cron_gate_budget,
    embed_executor,
    maintenance_executor,
    run_in_cron_gate_pool,
    run_in_cron_pool,
    run_in_embed_pool,
    subprocess_executor,
)
from kiro_crew.frontend import build_frontend_async
from kiro_crew.gateway_restart import resolve_restart_launcher, supervisor_reentry
from kiro_crew.gateway_shutdown_budget import GRACEFUL_SHUTDOWN_SECS, UPDATE_INSTALLER_STOP_SECS
from kiro_crew.heartbeat import (
    HEARTBEAT_TASK_TIMEOUT_SECS,
    HeartbeatService,
    is_keep_response,
    strip_keep_sentinel,
)
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.hooks import (
    HookManager,
    HooksConfig,
    _is_host_read_only_builtin,
    hooks_config_from_config_dict,
)
from kiro_crew.kiro_cli import PATH_ONLY_INSTALL_NOTE, is_bundled_kiro_cli, pin_kiro_cli
from kiro_crew.learn import LessonStore
from kiro_crew.llm_helpers import (
    SESSION_NOT_FOUND_GIVE_UP_TEXT,
    SESSION_NOT_FOUND_NOT_REPLAYED_TEXT,
    TOOL_ACTIVITY_ATTR,
    PromptBusyExhaustedError,
    ToolApprovalPolicy,
    acp_error_after_tool_activity,
    acp_error_is_session_not_found,
    acp_error_is_transient,
    annotate_model_fallback,
    append_fallback_story,
    configured_fallback_chain,
    provider_fallback_active,
    provider_last_turn_usage,
    provider_model_pin_partial,
    provider_model_pin_refused,
    save_conversation_turn_off_loop,
    stream_and_collect,
    transient_retry_delay,
)
from kiro_crew.mcp_cron import vet_job_at_fire_time
from kiro_crew.mcp_gateway import is_gateway_supported
from kiro_crew.mcp_gateway.launch_approval import (
    LaunchApprovals,
    filter_target_env,
    load_approvals,
    save_pass,
)
from kiro_crew.mcp_gateway.launch_resolve import rewrite_kwargs
from kiro_crew.mcp_gateway.manager import (
    GatewayManager,
    GatewaySpec,
)
from kiro_crew.mcp_gateway.resolve_once import prefetch as resolve_prefetch
from kiro_crew.mcp_gateway.rewriter import rewrite_agents
from kiro_crew.mcp_hot_reload import parse_kiro_cli_version
from kiro_crew.memory import MemoryStore
from kiro_crew.messaging import (
    APPROVAL_INTERACTIVE,
    TurnDriver,
    inbound_spool,
    registry,
    turn_ceiling,
)
from kiro_crew.messaging.dispatch import (
    build_directive_consumer,
    build_tool_gate,
    consume_reinjection,
    rearm_reinjection,
    rollback_skill_bodies,
    stop_reason_landed,
)
from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.egress import channel_egress_parts, send_channel_parts
from kiro_crew.messaging.identity import channel_inbound_permitted, publish_turn_identity
from kiro_crew.messaging.link import (
    CHANNEL_SESSION_NAMESPACES,
    CHAT_TYPE_DIRECT,
    DM_SCOPE_UNIFIED,
    SLACK_NAMESPACE,
    ChannelLink,
    channel_namespace_of,
    is_legacy_slack_key,
    parse_session_key,
)
from kiro_crew.messaging.renderer import (
    SilentRenderer,
    chunk_for_transport,
    display_safe,
)
from kiro_crew.messaging.spawn_approval_delivery import deliver_spawn_approval
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.monitoring.completion import (
    MonitorCompletionHook,
    disposition_for_stop_reason,
    is_monitor_completion_evidence,
)
from kiro_crew.monitoring.models import (
    DEFAULT_MONITOR_STALL_MIN_SECS,
    DEFAULT_MONITOR_STALL_TICKS,
    MONITOR_STOP_APPROVAL_STALL,
    MONITOR_STOP_COMPLETION_UNAVAILABLE,
    MONITOR_STOP_INVALID_RECORD,
    MONITOR_STOP_SESSION_UNAVAILABLE,
    MONITOR_STOP_UNSUPPORTED_VERSION,
    MONITOR_STOP_VERDICT_STALL,
    MonitorActionDisposition,
    MonitorDispatchResult,
    MonitorOutcome,
    monitor_state_public_dict,
)
from kiro_crew.notifications.bus import MONITOR_CHANNEL
from kiro_crew.platform import boot_platform
from kiro_crew.platform.bootstrap import apply_kas_cli_floor
from kiro_crew.platform.context import (
    PlatformCompositionError,
    current_context,
    redact_log_via_context,
    redact_via_context,
    safe_context_call,
)
from kiro_crew.platform.governance_profiles import (
    HOST_SESSION_KEY,
    audit_governance_degraded,
    governance_permits,
    vet_and_audit,
)
from kiro_crew.platform.update_capability import (
    AUTO_EFFECT_INSTALL,
    AUTO_EFFECT_MANDATORY,
    AUTO_EFFECT_NOTIFY,
    AUTO_ROUTE_GIT,
    AUTO_ROUTE_PROVIDER,
    AUTO_ROUTE_WHEEL,
    CHECK_SUCCEEDED,
    CHECK_UNCHECKED,
    EXTERNALLY_MANAGED_STAMPS,
    MANAGED_BY_COMMAND,
    AutoUpdateEffect,
    auto_update_effect,
)
from kiro_crew.platform.update_governance import (
    commits_ahead,
    git_command_env,
    hidden_worktree_edits,
    is_primary_branch,
    loggable_path,
    repo_exec_config_reason,
    resolve_remote_url,
    tracks_upstream,
    update_blocked_reason,
)
from kiro_crew.pre_exec_stops import run_pre_exec_stops
from kiro_crew.providers.base import LLMEvent
from kiro_crew.safety_override import flush_breadcrumb_writes, safety_override
from kiro_crew.sandbox import (
    SandboxUnavailableError,
    create_subprocess_limited,
    ensure_agents_slice_limits,
    sandboxed_spawn_argv,
    sandboxed_spawn_argv_async,
    warm_backend,
)
from kiro_crew.security import (
    redact,
    redact_and_truncate,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.security.readonly_bash import command_invokes_git, is_read_only_bash
from kiro_crew.sel import flush_audit_queue_before_hard_exit, sel
from kiro_crew.service.common import restart_command_hint
from kiro_crew.session import (
    HEARTBEAT_KEY,
    SessionBusyError,
    SessionClosingError,
    SessionManager,
)
from kiro_crew.session_start_sizing import resolve_session_start_sizing
from kiro_crew.skills import SkillsLoader
from kiro_crew.slack import gateway_runtime as _runtime
from kiro_crew.slack.client import RealSlackClient
from kiro_crew.slack.format import (
    build_cron_ack_block,
    build_options_blocks,
    escape_mrkdwn,
    extract_options,
    render_for_slack,
)
from kiro_crew.slack.gateway_runtime import admission as _admission
from kiro_crew.slack.gateway_runtime import channel_lifecycle as _channel_lifecycle
from kiro_crew.slack.gateway_runtime import cron_dispatch as _cron_dispatch
from kiro_crew.slack.gateway_runtime import cron_verdict as _cron_verdict
from kiro_crew.slack.gateway_runtime import delivery as _delivery
from kiro_crew.slack.gateway_runtime import exit_path as _exit
from kiro_crew.slack.gateway_runtime import heartbeat_turns as _heartbeat
from kiro_crew.slack.gateway_runtime import install_repair as _install
from kiro_crew.slack.gateway_runtime import mcp_broker as _mcp_broker
from kiro_crew.slack.gateway_runtime import memory_lifecycle as _memory_lifecycle
from kiro_crew.slack.gateway_runtime import nudge_channel_fire as _nudge_chan
from kiro_crew.slack.gateway_runtime import nudge_dashboard_fire as _nudge_dash
from kiro_crew.slack.gateway_runtime import nudge_wiring as _nudge_wiring
from kiro_crew.slack.gateway_runtime import process_entry as _entry
from kiro_crew.slack.gateway_runtime import result_egress as _egress
from kiro_crew.slack.gateway_runtime import service_boot as _boot
from kiro_crew.slack.gateway_runtime import tool_policy as _tool_policy
from kiro_crew.slack.gateway_runtime import turn_approvals as _approvals
from kiro_crew.slack.gateway_runtime import update_apply as _upd_apply
from kiro_crew.slack.gateway_runtime import update_schedule as _upd_sched
from kiro_crew.slack.gateway_runtime.channel_lifecycle import (
    _channel_transport_permitted,
    _push_observe_limits,
)
from kiro_crew.slack.gateway_runtime.cron_dispatch import (
    _CRON_POSTTOKEN_CONTINUE_MSG,
    _CRON_RESERVED_ENV_KEYS,
    _CRON_TRANSIENT_RETRIES,
    CronClaimAbandoned,
    CronClaimTimeDenied,
    CronVetOverran,
    _annotate_model_fallback,
    _await_cron_fire_time_gate,
    _claim_backstop,
    _ClaimHandoff,
    _cron_stream_with_posttoken_resume,
    _defer_cron_before_dispatch,
    _pre_create_cron_slot,
    _vet_at_claim_then,
    claim_vet_bound,
    cron_job_env_without_reserved,
)
from kiro_crew.slack.gateway_runtime.cron_verdict import (
    _CRON_FAILURE_DETAIL_CAP,
    _EPOCH_RE,
    _EPOCH_WINDOW_SECS,
    _FAILURE_REMINDER_SECS,
    _NO_RESPONSE,
    _SUCCESS_REMINDER_SECS,
    _VOLATILE_RE,
    _annotate_partial_block,
    _dedup_text,
    _GateTally,
    _result_hash,
)
from kiro_crew.slack.gateway_runtime.heartbeat_turns import (
    _persist_turn_row,
)
from kiro_crew.slack.gateway_runtime.install_repair import (
    _KIRO_CLI_RESOLVE_TIMEOUT_SECS,
    _pinned_kiro_cli,
)
from kiro_crew.slack.gateway_runtime.nudge_channel_fire import (
    _NUDGE_TURN_TIMEOUT,
    _delivery_result,
    _DmDispatchAdapter,
)
from kiro_crew.slack.gateway_runtime.process_entry import (
    _live_session_agents,
    _live_session_work_dirs,
    _run_signing_key_preflight_bounded,
    _start_kas_cli_floor,
    _sweep_predecessor_session_work_dirs,
    run_gateway,
    signing_key_preflight,
)
from kiro_crew.slack.gateway_runtime.result_egress import (
    _heartbeat_slack_parts,
)
from kiro_crew.slack.gateway_runtime.tool_policy import (
    _BACKGROUND_APPROVAL_SOURCES,
    _HEARTBEAT_CORE_SERVER,
    _HEARTBEAT_SAFE_CORE_MCP_TOOLS,
    _HEARTBEAT_STATUS_PREFIXES,
    _READ_ONLY_TOOL_PREFIXES,
    _WRITE_INDICATORS,
    _bare_tool_name,
    _build_heartbeat_hooks,
    _is_heartbeat_safe_tool,
    _is_read_only_tool,
)
from kiro_crew.slack.gateway_runtime.update_schedule import (
    _UPDATE_STOP_GRACE_SECS,
)
from kiro_crew.slack.handler import (
    _get_agent_for_session,
    build_timing_footer,
    is_thread_incognito,
    is_thread_temporary,
    is_tracked_channel,
)
from kiro_crew.slack.outbound import PostedOptions
from kiro_crew.slack.retry import open_dm_with_retry
from kiro_crew.slack.scope_probe import log_probe_failure, warn_unreadable_tracked_channels
from kiro_crew.slack.transport import SlackTransport
from kiro_crew.subagent import (
    _TRANSIENT_CONTINUE_MSG,
    DIGEST_HOLD_SECS,
    INJECTION_TIMEOUT,
    ORPHAN_NOTICE_OWED,
    SpawnApprovalUnreachable,
    SubagentDelivery,
    SubagentInfo,
    SubagentManager,
    ToolApprovalCallback,
    _injection_notice_outcome,
    format_subagent_usage,
    resolve_max_subagents,
)
from kiro_crew.subagent_completion_meta import (
    OUTCOME_FAILED,
    OUTCOME_OK,
    OUTCOME_STOPPED,
    single_completion_meta,
    wave_chunk_meta,
    wave_final_meta,
)
from kiro_crew.subagent_persistence import read_tombstone
from kiro_crew.subagent_wait_reasons import (  # noqa: F401
    NEVER_STARTED_MEMORY_PREFIX,
    QUEUED_WAIT_EXPIRED_TEXT,
)
from kiro_crew.taskrunner import TaskRunner
from kiro_crew.tunnel import set_publish_disabled
from kiro_crew.validation import CHANNEL_ID_RE
from kiro_crew.wecom.gateway import warn_if_channel_uncredentialed

if TYPE_CHECKING:
    from kiro_crew.adaptive.controller import AdaptiveController
    from kiro_crew.dashboard.state import _ChatSlot
    from kiro_crew.discord.client import DiscordClient
    from kiro_crew.imessage.client import IMessageClient
    from kiro_crew.memory_startup import MemoryStartup
    from kiro_crew.messaging.registry import ChannelDescriptor
    from kiro_crew.providers.base import LLMProvider
    from kiro_crew.subagent_scale import SubagentEventCoalescer
    from kiro_crew.task_models import Task
    from kiro_crew.teams.client import TeamsClient
    from kiro_crew.telegram.client import TelegramClient
    from kiro_crew.vector_memory import VectorMemoryStore
    from kiro_crew.webex.client import WebexClient
    from kiro_crew.wecom.client import WeComClient
    from kiro_crew.weixin.client import WeixinClient
    from kiro_crew.whatsapp.client import WhatsAppClient


# Chunked wave-digest size: every multi-task wave delivers its completed
# results to the parent in digest CHUNKS of this many members (queue-style —
# each chunk is one injection turn), with a final partial chunk when the wave
# closes. A 60-agent wave = 6 digest turns spread across the wave's runtime
# instead of 60 per-agent turns (the parent-context flood at scale) or one
# straggler-gated mega-digest at the very end. Single-task spawns have no
# batch identity and keep the plain per-agent injection.
# Tunable via KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE. Guarded parse: a malformed
# value must never crash gateway import — fall back to the default and clamp
# to a sane positive range.


def _digest_chunk_size() -> int:
    try:
        return max(1, min(int(os.environ.get("KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE", "10")), 1000))
    except (TypeError, ValueError):
        return 10


SUBAGENT_DIGEST_CHUNK_SIZE = _digest_chunk_size()


def _injection_slot_busy(slot: Any) -> bool:
    """True when *slot* already owns a turn a new injection must wait behind.

    ``slot.running`` alone is not enough. A just-dispatched injection parks in
    ``bounded_chat_turn``'s off-loop timeout resolution before ``_run_chat``
    starts, and only the live ``slot.task`` — assigned synchronously at
    dispatch — records that claim. A slot whose ``running`` is not derived
    from ``task`` (test doubles, duck-typed slots) reads such a window as
    idle, so a later digest chunk takes the idle branch: it appends in
    whichever order the dispatch hops resolve (not FIFO under CPU load) and
    assigns ``slot.task`` over the earlier chunk's still-pending task instead
    of awaiting it. Consulting the claim directly keeps chunk delivery FIFO
    regardless of how ``running`` is implemented or when the hop resolves.
    """
    task = slot.task
    return bool(slot.running) or (task is not None and not task.done())


async def _subagent_work_pending(manager: Any, parent_session_key: str) -> bool:
    """Whether *parent_session_key* still has sub-agents running or QUEUED.

    Asked through ``SubagentManager.has_pending_work_for_async``, whose store
    ``count_pending`` runs on the task store's writer thread: the synchronous
    entry takes the SQLite connection on this loop, and a wait there freezes
    every session's turn behind it. A manager double without the async sibling
    is asked synchronously -- the pre-queue behaviour those doubles model, and
    the same probe ``dashboard.handlers.messaging._spawn_on_loop`` makes for
    ``spawn_async``.
    """
    import inspect

    entry = getattr(manager, "has_pending_work_for_async", None)
    if inspect.iscoroutinefunction(entry):
        return bool(await entry(parent_session_key))
    return bool(manager.has_pending_work_for(parent_session_key))


async def _subagent_queued_count(manager: Any, parent_session_key: str) -> int:
    """This parent's QUEUED spawn count, read off-loop like
    :func:`_subagent_work_pending` (which also counts the running ones)."""
    return await _queued_depth_off_loop(manager, parent_session_key)


async def _subagent_batch_pending(manager: Any, batch_id: str) -> bool:
    """Whether any member of *batch_id* is still outstanding, read off-loop for
    :func:`_subagent_work_pending`'s reason: the store half of
    ``batch_members_pending`` is a ``fetch_pending_by_batch`` on the connection
    this loop would otherwise block on."""
    import inspect

    entry = getattr(manager, "batch_members_pending_async", None)
    if inspect.iscoroutinefunction(entry):
        return bool(await entry(batch_id))
    return bool(manager.batch_members_pending(batch_id))


logger = logging.getLogger(__name__)


# Full chat turn timeout — tool calls, multi-step reasoning, spawning.
# More generous than INJECTION_TIMEOUT (default 900s, tunable via
# KIROCREW_INJECTION_TIMEOUT) which only covers a single injected continuation turn.

# Max attempts, the first one included, to inject a subagent result into a
# parent session.
_MAX_INJECT_ATTEMPTS = 2


# Budget for awaiting the in-flight run-marker write during shutdown. Bounded
# so a stalled write can never eat into GRACEFUL_SHUTDOWN_SECS (which saves
# active slots) — the marker is best-effort, the slot save is not.
_MARKER_WRITE_WAIT_SECS = 5.0

# How often the gateway re-stats the governance profiles directory and re-derives
# the on-disk auto-approvals when a saved profile has moved the governance answer.
# Bounds how long a profile that withdraws an auto-approved tool can be bypassed by
# a newly started session; an unchanged tick costs a directory stat.
_PROFILE_PROJECTION_POLL_S = 15.0


# Slack Block Kit section.text hard limit is 3000 chars.
# We split cron output at this boundary so each chunk fits in a section block.
_CRON_MSG_LIMIT = 3000


#: Session-key prefixes of a workflow RUN's step sessions, each followed by
#: ``<run_id>:``: per-call (``wf:``), pooled worker, unpooled call, and the
#: memory-scoped worker key.
_WORKFLOW_STEP_PREFIXES = ("wf:", "wf-pool:", "wf-unpooled:", "wf-worker:")


def _spawn_parent_slot(parent_key: str, workflow_service: Any) -> str:
    """The dashboard slot whose Trust governs a spawn parented on *parent_key*.

    A workflow step runs in its own ``<prefix>:<run_id>:...`` session, which no
    tab displays. Its spawns answer to the chat that STARTED the run: the registry
    records that origin ``session_key`` only after the run route proved the
    caller is that session. An unknown run or a blank origin yields ``""``, so
    the spawn prompts exactly as before.
    """
    if not parent_key:
        return ""
    if not parent_key.startswith(_WORKFLOW_STEP_PREFIXES):
        return subagent_event_slot(parent_key)
    run_id = parent_key.split(":", 2)[1]
    registry = getattr(workflow_service, "registry", None)
    try:
        handle = registry.get(run_id) if run_id and registry is not None else None
    except Exception:
        logger.warning("spawn slot: workflow run %s lookup failed", run_id, exc_info=True)
        return ""
    origin = str(getattr(handle, "session_key", "") or "")
    return subagent_event_slot(origin) if origin else ""


# Prepended to every heartbeat task_text before ``ctx_builder.build_message``.
# Inline injection survives context compaction and webhook-restored sessions
# where skill / system-prompt copies of the same instruction can drift out of
# effective context.
_HEARTBEAT_KEEP_INJECTION = (
    "[HEARTBEAT TASK — you MUST include the keyword HEARTBEAT_KEEP "
    "in your response if this task is NOT complete. Omit the "
    "keyword only when the task is fully complete.]\n\n"
)


def _apply_gate_verdict(job: CronJob, tally: _GateTally) -> bool:
    """Record a finished cron run's success or failure from its gate outcomes.

    Shared by both cron agent paths so their verdicts cannot drift. Mutating
    ``last_status`` is how the non-raising cron paths signal failure:
    ``CronScheduler._execute`` keeps an explicit "error" rather than
    overwriting it with "ok".

    Returns whether this call counted a failure, because a run must increment
    ``consecutive_failures`` **at most once**. On the single-agent path the
    delivery work that follows (dashboard broadcast, Slack post) runs inside a
    ``try`` whose handler counts too, so a blocked turn whose delivery then
    failed would otherwise reach the auto-pause threshold in three runs rather
    than five — pausing on arithmetic instead of on evidence.

    Three outcomes, not two. A run with every call refused is a failure and
    is counted. A run with SOME call refused keeps ``last_status = "error"``
    with the refusal as its reason but is counted in neither direction: the
    approved calls prove the job can work, so it must not march toward
    auto-pause, and the lost call means it did not succeed, so it must not
    read as ``ok`` and must not reset a failure streak or the failure-alert
    dedup the way a success does. Only a run with no refusal at all records a
    success.
    """
    if tally.all_blocked:
        # Nothing the model attempted was permitted, so the run accomplished
        # nothing however plausible its reply reads. A success resets
        # consecutive_failures and clears auto_paused, so recording one here
        # would keep a structurally-failing job firing on its schedule forever.
        job.last_status = "error"
        job.last_error = tally.refusal_summary()
        job.record_failure()
        if job.auto_paused:
            logger.warning(
                "Cron '%s': auto-paused after %d consecutive failures",
                job.name,
                job.consecutive_failures,
            )
        return True
    if tally.partially_blocked:
        # The run did work AND lost work. "error" is the one non-ok status the
        # row, cron_list and the history record understand, and the reason
        # names the refused call. Neither counter moves: not record_failure(),
        # because the approved calls are evidence the job can work; not
        # record_success(), because that would reset a failure streak and the
        # failure-alert dedup on a run that did not fully succeed.
        job.last_status = "error"
        job.last_error = tally.refusal_summary()
        return False
    # Clear failure dedup on any success, regardless of whether the success
    # result itself is a dup. A successful run means the job recovered — next
    # failure should always alert fresh. record_success() owns the reset now, so
    # every kind's success path gets it rather than only this one.
    job.record_success()
    # Same reason record_success resets consecutive_failures: a run that
    # worked proves the job can work, so the shared-death streak that
    # substitutes for that counter is reset with it. Without this the
    # streak is a LIFETIME total, so after enough deaths the bound is
    # permanently tripped and the exemption silently stops applying.
    runtime_death.clear_shared_deaths(f"cron:{job.id}")
    return False


# Restart step named by the boot refusal. Deliberately static: probing for the
# installed service would stat HOME, which may be a hung network mount.
BOOT_RESTART_HINT = "kirocrew restart"

# Boot deadline for the signing-key preflight. Its lstat and key load normally
# take milliseconds (the loader's own retry budget is well under a second), so a
# preflight still running at this point is stuck on the filesystem.
_SIGNING_KEY_PREFLIGHT_TIMEOUT_SECS = 30.0


class GatewayOrchestrator:
    """Manages the lifecycle of all gateway services.

    Responsibilities are intentionally narrow — event routing and
    interactive handling are delegated to :mod:`events` and
    :mod:`interactions` respectively.
    """

    #: The stub set the broker's last start ATTEMPT was made with, which is not
    #: the configured one: a stub change is recorded for the next gateway start
    #: and deliberately not applied in place. Anything that restarts the broker
    #: for an unrelated reason re-emits THIS set, or it silently applies a change
    #: the operator was told is pending.
    #:
    #: Written on the attempt rather than on success, because a start that fails
    #: leaves the broker down and something still has to know which set to bring
    #: up when a later restart retries it. Recording only successes would turn a
    #: transient start failure into a permanently absent broker.
    #:
    #: Declared on the class so it is total for every construction path,
    #: including the ``__new__`` fixtures that never run ``__init__`` -- a
    #: partially built orchestrator reading it must get "nothing attempted", not
    #: AttributeError.
    _mcp_stub_servers_started: frozenset[str] = frozenset()

    # Declared for the type checker only (no value, so no class attribute): each
    # is first assigned in an owner function, which mypy does not read as an
    # attribute declaration the way it reads an assignment in a method.
    _slack_connect_error: str
    _adaptive_start_sub: Subscription | None
    vector_memory: VectorMemoryStore
    _dashboard_port: int
    _configured_host: str
    _local_only: bool
    _member_reconcile_task: asyncio.Task[None]
    _persistence_watchdog_task: asyncio.Task[bool]

    def __init__(
        self,
        cfg: KiroCrewConfig,
        *,
        no_dashboard: bool = False,
        no_crons: bool = False,
        no_open: bool = False,
        port_override: str | None = None,
        json_ready: bool = False,
        approval_mode: str | None = None,
        test_mode: bool = False,
        credentials: Mapping[str, str] | None = None,
    ) -> None:
        # NOTE: test_heartbeat_prompt_deliver.py creates instances via __new__
        # (bypassing __init__). Update that fixture if new attributes are added.
        self._cfg = cfg
        self._no_dashboard = no_dashboard
        self._no_crons = no_crons
        self._no_open = no_open
        self._port_override = port_override
        self._json_ready = json_ready
        self._approval_mode = approval_mode
        self._test_mode = test_mode
        # *credentials* is ``load_credentials()`` already run off the event loop
        # (``run_gateway``): resolving a secret:// reference reads the vault.
        creds = dict(credentials) if credentials is not None else cfg.load_credentials()
        self._app_token = creds.get(CRED_SLACK_APP_TOKEN, "")
        self._bot_token = creds.get(CRED_SLACK_BOT_TOKEN, "")
        self._owner_id = creds.get(CRED_OWNER_ID, "")
        # Multi-user access is disabled — only owner is authorized.
        # Prune stale allowed_users entries from config and warn.
        stale = {u["slack_id"] for u in cfg.slack.allowed_users} - (
            {self._owner_id} if self._owner_id else set()
        )
        if stale:
            logger.warning(
                "Pruning %d stale allowlist entries (multi-user disabled): %s",
                len(stale),
                stale,
            )
        self._allowed_users: set[str] = {self._owner_id} if self._owner_id else set()
        self._tracking_channels: set[str] = {
            c["channel_id"] for c in cfg.slack.tracking_channels if c.get("channel_id")
        }
        self._open_channels: set[str] = set(cfg.slack.open_channels)
        self._slack_enabled = bool(self._app_token and self._bot_token)
        # One hoist per channel, in roster order. Each reads the credential
        # store + that channel's config section into the ``_<channel>_*``
        # attributes its ``maybe_start_*`` factory consumes. Kept as methods so
        # ``restart_channel`` can re-run exactly one of them against a fresh
        # config when a connection parameter changes.
        self._hoist_wecom(cfg, creds)
        self._hoist_telegram(cfg, creds)
        self._hoist_weixin(cfg, creds)
        self._hoist_whatsapp(cfg, creds)
        self._hoist_feishu(cfg, creds)
        self._hoist_discord(cfg, creds)
        self._hoist_webex(cfg, creds)
        self._hoist_imessage(cfg, creds)
        self._hoist_teams(cfg, creds)
        self.slack_command = cfg.slack.command
        # Serializes restart_channel: two config writes landing within one
        # poll interval must close-then-start the same channel in order.
        self._channel_restart_lock = asyncio.Lock()
        # Set once _start_channel_transports has run; the restart applier defers
        # before that, because the boot loop is about to start every channel
        # from the hoist above and a concurrent start would double it.
        self._channel_transports_started = False
        self._channel_restart_tasks: set[asyncio.Task[None]] = set()
        self._channel_restart_gen: dict[str, int] = {}
        self._config_subs: list[Subscription] = []
        self._register_config_appliers()

        # Services (initialized in start())
        self.slack: RealSlackClient | None = None
        self.sessions: SessionManager | None = None
        self.ctx_builder: ContextBuilder | None = None
        self.conv_log: ConversationLog | None = None
        self.consolidator: HistoryConsolidator | None = None
        self.cron_svc: CronService | None = None
        self._cron_reconciled = False
        self._cron_armed = False
        self.heartbeat_svc: HeartbeatService | None = None
        # Declared here, not just assigned in `_init_autonudge`: that method
        # returns early when `KIROCREW_AUTONUDGE=0`, BEFORE its only assignment,
        # so with the flag off the attribute never existed at all -- and the
        # seven `if self.autonudge_svc:` sites in the loop-CRUD handlers below
        # would raise AttributeError rather than read a default.
        self.autonudge_svc: AutoNudgeService | None = None
        # Secretary runtime service removed (Amazon-internal). Attribute stays
        # as an inert None so other modules referencing it degrade gracefully.
        self.secretary_svc: object | None = None
        self.subagent_mgr: SubagentManager | None = None
        # Adaptive concurrency controller: started by _start_adaptive_controller
        # after dashboard and task-store readiness; stopped in shutdown before
        # the manager is cancelled so no actuator fires into a closing manager.
        self._adaptive_controller: AdaptiveController | None = None
        self._subagent_coalescer_inst: "SubagentEventCoalescer | None" = None
        # Wave accounting for the completion digest (batch_id -> progress).
        self._batch_progress: dict[str, dict] = {}
        self._cron_injecting: dict[str, int] = {}  # parent_key → pending injection count
        self._running_script_ids: set[str] = (
            set()
        )  # job IDs with in-flight script/command execution
        self.task_runner: TaskRunner | None = None
        # Runner admission over the task queue, attached by _wire_runner_admission
        self._runner_admission: Any = None
        # Whether the consumers' orphan-adoption sweep has already been handed
        # out over a store that existed. The first wiring pass can run while the
        # store is still opening off the loop, and adopting nothing is not the
        # same as having adopted: _runner_admission_store_ready re-attaches then,
        # and this keeps that from becoming a SECOND sweep over the same rows.
        self._runner_admission_adopted = False
        self.channel_history: ChannelHistory | None = None
        self.dashboard_state: DashboardState | None = None
        self._background_tasks: set[asyncio.Task] = set()  # prevent GC of fire-and-forget tasks
        # Approval-state persistence is scheduled while the MCP broker starts,
        # but its task waits for the dashboard/API readiness boundary.
        self._mcp_launch_approval_ready = asyncio.Event()
        self._memory_startup: MemoryStartup | None = None
        self._memory_startup_task: asyncio.Task | None = None
        self._memory_repair_task: asyncio.Task | None = None
        self._memory_repair_stop = threading.Event()
        self._memory_repair_cursor = ""
        # Dedicated ownership for the repair's dep_sync/pip process tree. The
        # general set only prevents task GC; shutdown must cancel and await this
        # task so _check_console_script can kill and reap its child group.
        self._console_script_repair_task: "asyncio.Task[None] | None" = None
        self._marker_write_task: "asyncio.Task[None] | None" = None
        # Shutdown waits on this task so an in-flight sign-in renewal can
        # persist its replacement credential before the hard exit.
        self._sign_in_refresher_task: "asyncio.Task[None] | None" = None
        # Set by the shutdown path when the marker write is still in flight:
        # tells the writer thread to self-clear after publishing, closing the
        # write-after-clear race without any event-loop dependency.
        self._marker_clear_pending = threading.Event()
        self._dashboard_runner: web.AppRunner | None = None
        self._handler_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
        self._session_tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        self._pending_queue: dict[str, list] = {}
        self._socket_client: WSSocketModeClient | None = None
        self._wecom_client: "WeComClient | None" = None  # set by maybe_start_wecom
        # Registry-owned live channel handles ({channel_type: client}). The
        # per-channel _<type>_client attributes are legacy mirrors kept in sync
        # by messaging.registry.start_channels until the config schema retires
        # them; shutdown closes through THIS dict.
        self._channel_handles: dict[str, object] = {}
        # Detached boot task that replays the durable inbound spool.
        # Held on the instance so the task is not garbage-collected mid-flight.
        self._inbound_replay_task: "asyncio.Task[None] | None" = None
        self._model_download_task: "asyncio.Task[bool] | None" = None
        self._feature_video_task: "asyncio.Task[bool] | None" = None
        self._auto_migrate_task: "asyncio.Task[None] | None" = None
        # Recurring update coordinator, started after signal handlers are
        # installed. Cancelled on shutdown so stalled network or installer work
        # cannot hold the process open.
        self._update_check_task: "asyncio.Task[None] | None" = None
        # Periodic governance-profile re-projection; cancelled at shutdown beside the
        # distribution refresher, so no rebuild starts into a closing gateway.
        self._profile_watch_task: "asyncio.Task[None] | None" = None
        self._update_apply_deferred = False
        self._pending_update_respawn: Callable[[], str] | None = None
        # Whether the pending restart serves a policy floor, and its target key:
        # kept beside the respawn so every retry runs under the same mandatory
        # grace instead of restarting the clock.
        self._pending_update_mandatory = False
        self._pending_update_mandatory_key = ""
        self._mandatory_update_deferred_at: float | None = None
        self._mandatory_update_deferred_key: str | None = None
        # Managed-venv notices already sent in this process, so each reaches the
        # operator once per release: the signed metadata excludes this host
        # (``incompatible:<version>``), or applying it would detach the
        # sandbox's AppArmor profile (``reattach:<version>``). Nothing here
        # suppresses an attempt: incompatibility is decided again, before any
        # download, on every cycle.
        self._wheel_update_noticed: set[str] = set()
        self._mcp_gateway_manager: GatewayManager | None = None
        # Detached pre-resolve pass for npm-launcher MCP targets. Held so the
        # loop keeps a strong reference (a bare create_task is only weakly held)
        # and so broker shutdown can cancel an install still in flight.
        self._mcp_resolve_prefetch: asyncio.Task[None] | None = None
        # The rewriter's ``KIROCREW_MCP_TARGET_*`` mapping from the last broker
        # start, kept so an explicit refresh resolves the same launches the
        # daemon is actually serving rather than a freshly re-derived guess.
        self._mcp_target_env: dict[str, str] = {}
        # Resolved here, in sync construction, because config_dir() does file IO
        # and must never be called from an async path. The store
        # lives beside the rest of the data home for the life of the process.

        self._mcp_resolve_home: str = str(config_dir())

    def _live_internal_secret(self) -> str:
        """This gateway's LIVE internal-API secret, for script-cron ``notify()``.

        The dashboard mints the secret at startup and the auth middleware
        compares against that same in-memory value (``app["local_secret"]``).
        A script cron's child authenticates ``/api/send-message`` with the
        secret it is handed at spawn, so the in-process scheduler hands it THIS
        value rather than one re-derived from the environment or a per-port
        file — a stale ``KIROCREW_INTERNAL_SECRET`` or ``.secret`` file
        otherwise wins the derivation and 403s every ``notify()``.

        Returns ``""`` when no dashboard has started (``--no-dashboard`` /
        API-only), so ``run_script_sandboxed`` falls back to env/file
        derivation. Read-only and in-process only: the value is never logged or
        returned over HTTP.
        """
        runner = self._dashboard_runner
        if runner is None:
            return ""
        try:
            return runner.app.get("local_secret", "") or ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Tool approval callback (shared by cron, heartbeat, subagent, task)
    # ------------------------------------------------------------------

    # gateway_runtime/turn_approvals.py
    _dashboard_client_attached = _approvals._dashboard_client_attached

    _interactive_approval = _approvals._interactive_approval

    _heartbeat_approval = _approvals._heartbeat_approval

    # Required packages that must be importable (import_name, pip_spec).
    # pip_spec may include version constraints matching setup.cfg.
    _REQUIRED_DEPS = [
        ("snowballstemmer", "snowballstemmer>=1.0"),
        # PyYAML (import name ``yaml``) is imported by cc_agent on every CLI
        # path. It installs cleanly from public PyPI, so list it here as a
        # backstop: if it is ever missing (e.g. a partial install), the startup
        # self-heal repairs it instead of every command crashing at import.
        ("yaml", "PyYAML>=6,<7"),
    ]

    @staticmethod
    def _is_brazil_install(proj: str) -> bool:
        """Return True if *proj* was installed via Brazil, False for venv/pip."""
        method_file = Path(proj) / ".install-method"
        if method_file.is_file():
            return method_file.read_text().strip() == "brazil"
        return bool(
            shutil.which("brazil-build") and (Path(proj).parent.parent / ".brazil").is_dir()
        )

    # ------------------------------------------------------------------
    # Service initialisation
    # ------------------------------------------------------------------

    # FTS rebuild is part of deferred memory initialization: it scans user
    # history and must complete before memory is released to consumers.

    # gateway_runtime/delivery.py
    _open_dm_with_retry = _delivery._open_dm_with_retry

    _record_cron_delivery = _delivery._record_cron_delivery

    _remember_options = _delivery._remember_options

    # gateway_runtime/delivery.py
    _channel_reply_link = _delivery._channel_reply_link

    # gateway_runtime/delivery.py
    _cron_origin_key = _delivery._cron_origin_key

    _deliver_cron_to_channel = _delivery._deliver_cron_to_channel

    # ── One spelling of the cron failure-alert mechanism ───────────────────
    #
    # Two call sites alert on a failed cron run: the script/command helper
    # (`_alert_cron_failure`) and the message path's own `except` block. They
    # legitimately differ in control flow (one re-raises), in who owns
    # `record_failure()`, and in wording. What they must NOT differ in is the
    # mechanism below -- the dedup window, the Slack-sink hardening, the
    # one-surface delivery rule, and when the dedup anchor advances.
    #
    # A docstring promising the two "cannot drift" is prose, not a mechanism:
    # nothing in it stops one surface's DM saying only "check logs" while the
    # other already carries the reason, and no test catches that. These four
    # helpers ARE the mechanism, so a change lands on both surfaces or on
    # neither.

    def _failure_alert_is_duplicate(self, job: CronJob, failure_hash: str) -> bool:
        """Whether this failure repeats the last one inside the reminder window.

        A job that fails identically every minute alerts once per
        ``_FAILURE_REMINDER_SECS`` rather than once per fire. Both surfaces read
        the SAME ``last_failure_hash`` / ``last_failure_at`` pair; a job is
        exactly one kind, so the two writers never interleave on one job.
        """
        return (
            failure_hash == job.last_failure_hash
            and time.time() - job.last_failure_at < _FAILURE_REMINDER_SECS
        )

    def _slack_safe_fenced(self, text: str) -> str:
        """Make *text* safe to interpolate into a Slack mrkdwn code fence.

        Two hazards, one of which escaping alone does not cover. Slack PARSES
        entity markup, and both halves of a failure alert are attacker-shaped --
        the job name is user-authored and the reason carries subprocess output, so
        a job named ``<!channel>`` would notify a whole channel the moment it
        failed. And three backticks inside the reason would CLOSE the fence early
        and hand the remainder to the parser as markup, which escaping does not
        prevent.

        Slack-facing sinks only. The dashboard bell is not a mrkdwn sink and
        escaping there would render a literal ``&lt;``.
        """
        return escape_mrkdwn(text).replace("```", "'''")

    async def _deliver_failure_alert(
        self,
        job: CronJob,
        *,
        mrkdwn: str,
        plain: str,
        actor_key: str,
        silent: bool = False,
    ) -> tuple[bool, bool, bool]:
        """Deliver a failure alert on exactly ONE surface.

        Returns ``(channel_delivered, slack_delivered, slack_failed)``.
        ``slack_failed`` names a real delivery exception, never an unresolved
        channel -- the caller's dedup decision turns on that split.

        The one-surface rule: when the conversation that scheduled the job will
        hear about the failure, the owner DM would be a second alert for one
        event. An explicit ``job.channel`` is a destination the user pinned and
        still wins, so the channel leg is skipped for it -- without that guard the
        channel leg reports a delivery, stands the Slack leg down, and the alert
        lands on the origin conversation instead of the destination the user
        named.

        *mrkdwn* and *plain* are composed by the caller and are deliberately two
        strings: Slack's markup is not another transport's dialect, so a shared
        string would show ``&lt;`` and stray backticks to a channel reader.

        Never raises. Both callers are inside an ``except`` whose exception is the
        real story, and one of them re-raises it.
        """
        channel_delivered = False
        slack_delivered = False
        slack_failed = False
        if not silent and not job.channel:
            try:
                channel_delivered = await self._deliver_cron_to_channel(
                    job.session_key, plain, actor_key=actor_key
                )
            except Exception:
                logger.error(
                    "Cron '%s': channel failure-alert delivery failed",
                    job.name,
                    exc_info=True,
                )
        if self.slack and not silent and not channel_delivered:
            try:
                channel = job.channel
                if not channel and (job.created_by or self._owner_id):
                    channel = await self._open_dm_with_retry(
                        job.created_by or self._owner_id, job.name
                    )
                if channel:
                    await self.slack.post_message(channel, mrkdwn)
                    slack_delivered = True
                else:
                    logger.warning("Cron '%s': no channel resolved for failure alert", job.name)
            except Exception:
                slack_failed = True
                logger.error(
                    "Cron '%s': Slack failure-alert delivery failed",
                    job.name,
                    exc_info=True,
                )
        return channel_delivered, slack_delivered, slack_failed

    def _advance_failure_dedup(
        self,
        job: CronJob,
        failure_hash: str,
        *,
        channel_delivered: bool,
        slack_failed: bool,
    ) -> None:
        """Advance the dedup anchor once the reason actually reached someone.

        "No channel available" counts as delivered -- the bell rang -- so a
        Slack-less install does not re-notify the dashboard on every fire. A
        confirmed channel delivery counts for the same reason: the reason reached
        the user even when the Slack leg threw. Only a REAL Slack exception holds
        the anchor back, so the next identical failure tries again.

        Only the dedup fields move here. ``record_failure()`` has its own owner
        per run and is deliberately not touched.
        """
        if channel_delivered or not slack_failed:
            job.last_failure_hash = failure_hash
            job.last_failure_at = time.time()

    # gateway_runtime/delivery.py
    _cron_job_is_silent = _delivery._cron_job_is_silent

    async def _init_cron(self, *, arm: bool = True) -> None:
        """Initialize the cron service, optionally arming it immediately."""

        async def _deliver_script_result(
            job: CronJob, message: str, *, remove: bool = False
        ) -> None:
            """Deliver a script cron result to the originating session. Optionally remove the job."""
            delivered = False
            try:
                if message and not job.silent and self.dashboard_state and job.session_key:
                    slot_key = job.session_key.removeprefix("dashboard:")
                    slot = self.dashboard_state.get_slot(slot_key)
                    if slot is None:
                        # Async form: the sync one parses the whole transcript on
                        # the loop, which stalls every other session's frames on
                        # a large store.
                        slot = await rehydrate_slot_from_history_async(
                            self.dashboard_state, slot_key
                        )
                    elif getattr(slot, "window_pending", False) is True:
                        # A tab restored as a sidebar row: its window loads
                        # before the delivery below appends to it, off the loop
                        # for the same reason, and the tab is read again after.
                        await load_window_off_loop(slot)
                        slot = self.dashboard_state.get_slot(slot_key)
                    label = redact(job.name)
                    if slot:
                        wrapped = f'[Cron notification: "{label}"]\n{message}\n[/Cron notification]'
                        inject_cls = json.dumps({"cronLabel": label})
                        if slot.running:
                            qid = slot.queue_append(wrapped, kind=CRON_NOTIFICATION_KIND)
                            _cls = json.loads(inject_cls)
                            _cls["queue_id"] = qid
                            slot.append("queued", wrapped, json.dumps(_cls))
                        else:
                            # `cls` is not persisted for role `inject`, so the label
                            # must also ride in `meta`, which is — otherwise the row
                            # loses its identity on the next rehydrate.
                            slot.append(
                                "inject",
                                wrapped,
                                inject_cls,
                                meta={"injectKind": "cron", "cronLabel": label},
                            )
                            task = spawn_guarded_turn(
                                self.dashboard_state,
                                slot,
                                _run_chat(
                                    self.dashboard_state,
                                    slot,
                                    wrapped,
                                    _directive_user_origin=False,
                                    # Structural provenance for the session
                                    # crew log: the queued twin above carries
                                    # CRON_NOTIFICATION_KIND, and this branch is
                                    # the same injector dispatching directly.
                                    _turn_actor="cron",
                                ),
                            )
                            slot.task = task
                        self.dashboard_state.push_slots_update()
                    else:
                        self.dashboard_state.notify(
                            "cron", f"⚡ {label}", message, meta={"job_id": job.id}
                        )
                elif message and not job.silent and self.dashboard_state:
                    label = redact(job.name)
                    self.dashboard_state.notify(
                        "cron", f"⚡ {label}", message, meta={"job_id": job.id}
                    )
                delivered = True
            except Exception as notify_exc:
                logger.warning("Cron '%s' delivery failed: %s", job.name, notify_exc)
            if remove and delivered and self.cron_svc:
                try:
                    await self.cron_svc.remove_job_async(
                        job.id,
                        actor="cron",
                        source="cron",
                        one_shot_path="cron_gateway",
                    )
                except (CronStoreBusy, CronStoreUnreadable):
                    # No caller to retry this fire-and-forget removal, so hand
                    # it to the service's deferred-removal queue: the job is
                    # disabled in memory immediately (can't re-fire) and the
                    # next timer tick drains it from disk under the store lock.
                    # No audit here — whichever path lands the removal on disk
                    # emits cron.remove: the deferred drain, or the run-merge
                    # consume when a delete_after_run job's merge gets there
                    # first.
                    self.cron_svc.defer_removal(job.id)
                    logger.warning(
                        "Cron '%s': store busy, queued one-shot removal for " "the next timer tick",
                        job.name,
                    )

        async def _alert_cron_failure(job: CronJob, detail: str, *, denied: bool = False) -> None:
            """Tell the user WHY a script/command cron run failed or was denied.

            The script and command paths signal failure by mutating the job
            (``last_status="error"`` + ``last_error``) and returning normally, so
            they never reached the message path's failure alert below — the reason
            existed only in the gateway log and in a dashboard field nobody is
            watching when the notification they expected simply never arrives. A
            job whose every run dies on a startup-time ``RuntimeError`` therefore
            looked idle rather than broken.

            Deliberately NOT a delivery of the run's *result*: this is a bell plus
            a DM carrying the reason, never an injected turn like
            :func:`_deliver_script_result`. A job failing on its own schedule must
            not spend a model turn per failure, and an injected turn is exactly how
            a failing cron would amplify itself.

            Contract, so the two failure surfaces cannot drift:

            * ``record_failure()`` is NOT called here. Every call site already
              counted the run (or deliberately did not, for a policy denial), and
              the counter has one owner per run — see :func:`_apply_gate_verdict`.
              Callers therefore alert AFTER counting, so ``consecutive_failures``
              reads true if a future body wants it.
            * Dedup reuses the SAME ``last_failure_hash`` / ``last_failure_at``
              fields as the message path. A job is exactly one kind, so the two
              writers never interleave on one job, and a run that fails
              identically every minute alerts once per
              ``_FAILURE_REMINDER_SECS`` instead of once per fire.
            * Never raises. Every call site is inside an ``except`` block whose
              exception is the real story; an alert that failed must not replace
              it. Cancellation still propagates (``CancelledError`` is a
              ``BaseException``).
            """
            try:
                if job.silent:
                    # Silent jobs still execute and still count toward auto-pause;
                    # only the user-facing surfaces are suppressed.
                    return
                text = redact(detail or "")
                text, _ = redact_exfiltration_urls(text)
                text, _ = redact_credentials(text)
                text = text.strip()[:_CRON_FAILURE_DETAIL_CAP] or "no reason reported"
                # job.name is user-controlled and the reason can carry subprocess
                # output, so both are scrubbed once, ahead of either surface and
                # of either branch below.
                label = redact(job.name)
                label, _ = redact_exfiltration_urls(label)
                label, _ = redact_credentials(label)
                mark = "⛔" if denied else "❌"
                headline = "Blocked by policy" if denied else "Run failed"
                # Denials and failures hash apart so a policy denial does not read
                # as a dup of a same-worded crash (and vice versa).
                fh = _result_hash(f"{'denied' if denied else 'failed'}:{text}")
                if self._failure_alert_is_duplicate(job, fh):
                    logger.info(
                        "Cron '%s': duplicate failure alert suppressed (%s)",
                        job.name,
                        "denied" if denied else "failed",
                    )
                    # Same split the message path draws: the LOCAL bell still
                    # rings (marked suppressed, so a user watching the feed sees
                    # the job is still down) and only the Slack DM is withheld.
                    try:
                        if self.dashboard_state:
                            self.dashboard_state.notify(
                                "cron",
                                f"🔇 Cron: {label} (repeat)",
                                f"{mark} Still failing (suppressed — same reason):\n{text}",
                                meta={"job_id": job.id, "failure_hash": fh},
                            )
                    except Exception:
                        logger.debug(
                            "Dashboard notify failed in cron run-failure suppress path",
                            exc_info=True,
                        )
                    return
                try:
                    if self.dashboard_state:
                        self.dashboard_state.notify(
                            "cron",
                            f"Cron: {label}",
                            f"{mark} {headline}:\n{text}",
                            meta={"job_id": job.id, "failure_hash": fh},
                        )
                except Exception:
                    logger.debug(
                        "Dashboard notify failed in cron run-failure alert path", exc_info=True
                    )
                # Name the machine for the same reason the message path does: a
                # laptop and a cloud desktop can both run Kiro Crew, and the
                # alert is the only place the user learns which one failed. Read
                # once, ahead of both delivery legs, so they cannot disagree.
                host = socket.gethostname().split(".")[0]
                # Slack PARSES entity markup and a fence can be closed early by
                # the reason's own backticks; `_slack_safe_fenced` is the one
                # spelling of that hardening. `label` and `text` are already
                # scrubbed above, and the transport leg redacts again at egress.
                safe_label = self._slack_safe_fenced(label)
                safe_text = self._slack_safe_fenced(text)
                msg = (
                    f"⏰ *Cron: {safe_label}* {mark} "
                    f"_{headline} on {escape_mrkdwn(host)}_\n```{safe_text}```"
                )
                msg, _ = redact_exfiltration_urls(msg)
                msg, _ = redact_credentials(msg)
                # Plain twin for a non-Slack transport: mrkdwn is not another
                # channel's dialect, so a shared string would show `&lt;` and
                # stray backticks there.
                plain = f"⏰ Cron: {label} {mark} {headline} on {host}\n{text}"
                # Silent jobs returned above, so this leg is never suppressed here.
                channel_delivered, slack_delivered, slack_failed = (
                    await self._deliver_failure_alert(
                        job,
                        mrkdwn=msg,
                        plain=plain,
                        actor_key=f"cron:{job.id}",
                    )
                )
                self._advance_failure_dedup(
                    job, fh, channel_delivered=channel_delivered, slack_failed=slack_failed
                )
                try:
                    # Name every surface the alert actually left on, so the trail
                    # does not read "none" for a run answered on Discord.
                    surfaces = ["slack"] if slack_delivered else []
                    if channel_delivered:
                        surfaces.append(channel_namespace_of(job.session_key))
                    sel().log_tool_invocation(
                        session_key=f"cron:{job.id}",
                        tool_name="cron_run_failure_alert",
                        outcome="denied" if denied else "alerted",
                        downstream_service=",".join(surfaces) or "none",
                    )
                except Exception:
                    logger.debug("SEL logging failed in cron run-failure alert path", exc_info=True)
            except Exception:
                logger.warning("Cron '%s': run-failure alert failed", job.name, exc_info=True)

        async def _cron_callback(job: CronJob) -> str | None:
            # A crewmate's Daily Goal Reminder is not an ordinary cron turn: it
            # runs as one hidden turn in the crewmate's own thread, built from
            # its current goal record (dashboard.goal_reminder).
            from kiro_crew.dashboard import goal_reminder

            if goal_reminder.is_reminder_job(job):
                # It still starts an agent turn, so it passes the same fire-time
                # governance gate as a message job first: a `capabilities.cron`
                # ceiling tightened after the switch was turned on must stop it.
                gate_reason, gate_starved = await _await_cron_fire_time_gate(
                    job, tool_name="cron_goal_checkin", tool_kind="cron_message"
                )
                if gate_starved:
                    return None
                if gate_reason:
                    # No record_failure(), as for the message-job deny below.
                    job.last_status = "error"
                    job.last_error = redact(gate_reason)
                    job.fire_time_denied = True
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_goal_checkin",
                            tool_kind="cron_message",
                            outcome="denied",
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in goal check-in fire-time deny path",
                            exc_info=True,
                        )
                    await _alert_cron_failure(job, gate_reason, denied=True)
                    return None
                return await goal_reminder.fire(self.dashboard_state, job)

            # True once ANY prompt has been handed to the provider this
            # invocation. The whole-callback transient retry below is only
            # safe BEFORE dispatch: after it, tools may have run, so a
            # resubmit risks duplicate side effects (in-stream transient
            # errors are stream_and_collect's own retry's job).
            _prompt_dispatched = False

            # helper picks stable vs ephemeral session key and
            # decides whether to prepend last_result, based on job.persistent_session.
            session_key, msg = build_cron_session_context(job)

            from kiro_crew.cron import resolve_cron_memory
            from kiro_crew.execution_context import (
                ExecutionContext,
                execution_for_store,
                execution_from_record,
            )

            # Snapshot the job before yielding; reloading a cron cannot rebind it.
            cron_agents = list(job.agent_sequence)
            # ── Default-agent fallback (LLM jobs) ──
            # A job with no explicit agent_id must run the configured default
            # agent (config.agent.default_agent), matching the chat transports'
            # fallback (transport_dispatch: `self.agent or cfg.agent.default_agent`).
            # Without it the empty selector reaches dispatch as AcpClient's
            # "kirocrew" floor -- an agent whose config carries none of the
            # default agent's MCP servers, so agent-less cron sessions silently
            # run without the expected toolset.
            #
            # Resolved ABOVE the branch and applied BELOW it, to the dispatched
            # agent only -- never to the captured execution identity -- and only
            # where the resolved agent is still the bare "kirocrew" floor. All
            # three halves are load-bearing:
            #   * Above the branch, because every record created since the
            #     execution-context migration carries `execution_context` and so
            #     takes the other arm. Resolving inside `else:` reaches only
            #     pre-migration jobs and leaves the reported symptom in place for
            #     everything created since.
            #   * Not into the capture, because `cron_execution` is what
            #     `bind_session_execution` publishes under the session key, and a
            #     persistent_session job (the default) reuses the stable key
            #     `cron:{job.id}` on every fire. That binder is called
            #     positionally, so `replace_existing` is False, and it refuses a
            #     candidate that differs from what is already recorded. A job that
            #     has already run holds `template_id="kirocrew"`; folding the
            #     default into the captured identity changes that field, so every
            #     later fire raises "session already belongs to another execution"
            #     before get_or_create -- not transient, caught nowhere in this
            #     callback, one recorded failure per fire until the job
            #     auto-pauses. Leaving the captured identity alone is also what
            #     keeps the resolved name out of the durable store.
            #   * Floor-gated, because a captured `template_id` that names a real
            #     template is a deliberate selection and must survive; see the
            #     gate below.
            # job.agent_id is never mutated: the snapshot is deliberately immune
            # to later mutation ("captured selectors, never the scheduler's
            # mutable job"), and a declared agent_sequence owns its own dispatch
            # and is left untouched. agent_sequence_dispatches is the ONE spelling
            # of "the sequence owns dispatch": a lone entry is dormant and
            # dispatch falls through to agent_id, which must still get the default.
            # Typed deliberately: a malformed or non-str configured default must
            # not reach the dispatched agent kwarg, so it degrades to the existing
            # "kirocrew" floor instead of failing the dispatch.
            _default_agent = ""
            # True once the dispatched agent came from ``agent.default_agent``.
            # That field is the TEMPLATE namespace (config/sections.py, free-form
            # str, no alias validation) -- unlike the alias-namespaced default,
            # which has its own `default_agent_not_alias` guard. The collapse in
            # _resolve_cron_agent treats ANY name that keys ``cfg.agents`` as a
            # crew alias, so without this flag a default that merely shares a
            # name with a crew member would hand an agent-less cron that crew's
            # workspace and capability MCP servers -- a silent capability and
            # identity widening, announced at DEBUG only.
            _default_substituted = False
            _agentless_job = not (job.agent_id or "").strip() and not agent_sequence_dispatches(
                job.agent_sequence
            )
            if _agentless_job:
                try:
                    # Prefer the last APPLIED reload over the boot snapshot.
                    # agent.default_agent carries no restart=True mark, so the
                    # config watcher adopts a dashboard/CLI change live, while
                    # self._cfg is rebuilt only by unrelated MCP handlers. A
                    # boot-only read would keep dispatching the "kirocrew" floor
                    # after an operator sets a default -- the exact symptom this
                    # hunk removes -- until a gateway restart or an incidental
                    # reload. Same one-liner the sibling resolver below uses.
                    _cfg_now = live.snapshot() or self._cfg
                    _configured = _cfg_now.agent.default_agent
                    if isinstance(_configured, str):
                        _default_agent = _configured.strip()
                except Exception:
                    _default_agent = ""
            if job.execution_context is not None:
                cron_execution = execution_from_record({"execution_context": job.execution_context})
            else:
                legacy_selection = CronJob(
                    id=job.id,
                    name=job.name,
                    message=job.message,
                    member_id=job.member_id,
                    memory_store=job.memory_store,
                    agent_id=job.agent_id,
                )

                def resolve_legacy_execution():
                    # Both calls can load configuration. The worker sees only
                    # captured selectors, never the scheduler's mutable job.
                    resolve_cron_memory(legacy_selection, validate_memory_files=False)
                    return execution_for_store(
                        legacy_selection.memory_store,
                        template_id=legacy_selection.agent_id or "kirocrew",
                    )

                cron_execution = await asyncio.to_thread(resolve_legacy_execution)
            cron_memory_store, cron_agent = (
                cron_execution.store.legacy_name,
                cron_execution.template_id,
            )
            # The configured default lands HERE and nowhere else: cron_agent is
            # read only by _resolve_cron_agent and the `agent=` kwargs below, none
            # of which any durable record compares.
            #
            # Gated on the resolved agent still being the bare "kirocrew" floor,
            # which is the whole of what this change replaces. A record whose
            # captured template_id names a REAL template keeps it: a schedule
            # created from a template chat with no `agent` argument names its
            # template ONLY there, `agent_id` staying empty: bind_cron_memory
            # (cron_service/identity.py:143-152) captures the CREATOR session's
            # execution, and overwrites template_id only `if job.agent_id`. So
            # an agent_id-only gate reads that job as agent-less. Overriding it
            # would re-point the job onto the default on every fire while its
            # captured execution and memory store stayed the original template's,
            # and dispatched_agents_from_disk -- the delete guard's ONE walk of
            # the dispatch-mirroring rule -- still reported the old agent: guard
            # and dispatch would drift, which is the same silent wrong-agent class
            # this change removes, reintroduced on the other arm. Compared after
            # .strip() so a whitespace-only capture counts as the floor, which is
            # where whitespace agent_id handling lives now that the capture itself
            # is left byte-for-byte alone.
            # Also gated on the capture carrying no member_id. A cron created
            # from a crew member's chat is captured by resolve_member_execution
            # as a MEMBER execution whose template_id is
            # `agent.kiro_agent or "kirocrew"` -- so a member whose agent names
            # no kiro_agent captures the floor spelling with agent_id empty, and
            # a floor-only gate reads it as "selected nothing". Substituting
            # there makes build_message inject the substituted template's system
            # prompt and documents into that member's envelope while
            # _resolve_cron_agent still dispatches the member's own kiro_agent,
            # and attributes the usage row to the substituted name. Requiring
            # member_id to be unset narrows the override to a capture that
            # really did select nothing.
            #
            # member_id ALONE does not narrow it far enough. A member predating
            # the identity migration persists no member_id at all, and
            # `with_template` states the rule that admits it: such a record "is
            # named by ``selection_kind == \"member\"`` and ``selection_name``
            # alone" (execution_context.py). ExecutionContext.__post_init__
            # constrains selection_kind to exactly "member" or "template", so
            # the namespace is the authoritative answer to "did this capture
            # select a member?" and member_id is only its persisted form.
            # Requiring BOTH keeps a legacy member on an unmigrated store out of
            # the override, which a member_id-only gate admitted.
            #
            # And BOTH capture gates together are still not enough, because on
            # the `job.execution_context is None` arm cron_execution is
            # RECONSTRUCTED, not loaded: resolve_legacy_execution calls
            # execution_for_store, which for a V1 store returns
            # `ExecutionContext(None, MemoryStoreRef(name), "template", ...)` --
            # hardcoding a null member_id and the "template" namespace whatever
            # the schedule actually selected. A member cron predating the
            # migration therefore passes both capture gates while job.member_id
            # still names the member, and resolve_cron_memory deliberately keeps
            # that record dispatching rather than raising
            # (cron_service/identity.py: "Older V1 schedules may still carry the
            # historical member selector beside their explicit legacy store"),
            # with nothing backfilling execution_context on load. So the JOB is
            # asked as well: on that arm it is the only surviving evidence of the
            # selection. A truthy non-str also declines, which is the safe
            # direction -- declining leaves the pre-change floor behaviour.
            if (
                _default_agent
                and (cron_agent or "").strip() in ("", "kirocrew")
                and not job.member_id
                and not cron_execution.member_id
                and cron_execution.selection_kind != "member"
            ):
                # ── Spawn-allowlist divergence gate ──
                # The substitution reaches the dispatched `agent=` only, while
                # bind_session_execution publishes the UNSUBSTITUTED capture
                # under `cron:{job.id}` -- which it must, for the auto-pause
                # reason above. Both spawn entry points derive the parent's
                # declaration from that record alone: read_session_execution(
                # parent) -> parent_spawn_allowlists(execution.template_id), in
                # dashboard/handlers/messaging.py and the off-loop twin in
                # subagent.py. The floor spelling resolves to `()`, which
                # _vet_parent_available_agents admits as "no declaration to
                # honour" -- so a toolsSettings.subagent.availableAgents list on
                # the substituted template is never consulted and a child it
                # forbids starts.
                #
                # Honouring it properly means a NON-DURABLE parent execution the
                # spawn gate reads, in two files this change does not touch and
                # whose reviewers are scoped to spawn admission, not cron. So
                # this change narrows itself instead: a default that declares an
                # allowlist is not substituted, and the job keeps the floor it
                # already had before this change. Nothing an operator has today
                # regresses; the capability simply waits for that CR.
                #
                # DENY BY DEFAULT. A non-empty tuple declines, and so does the
                # reader's `None`. Only `()` -- "no readable spec declares this
                # name, and every spec file parsed" -- permits the substitution.
                #
                # An earlier revision substituted on `None`, arguing the spawn
                # gate would refuse anyway because it asks about the CAPTURE's
                # floor spelling. That premise is false: `agent.py` generates and
                # installs `kirocrew.json` into the agents directory and
                # `config/defaults.json` declares `"name": "kirocrew"`, so a
                # readable spec DOES declare the floor name -- and
                # `parent_spawn_allowlists` answers `None` only when NO readable
                # spec declares the name while some file could not be read. For
                # the floor it therefore returns `()`, "no declaration to
                # honour", and the gate ADMITS. `None` is exactly the state where
                # a restrictive declaration may be sitting in the spec file the
                # hardened reader refused, in a shared user-writable directory,
                # so letting a falsy value skip the check is the deny-by-default
                # violation this clause exists to avoid.
                from kiro_crew.subagent import parent_spawn_allowlists as _spawn_allowlists

                _declared = await asyncio.to_thread(_spawn_allowlists, _default_agent)
                if _declared or _declared is None:
                    logger.warning(
                        "Cron '%s': default agent %r %s, and the captured "
                        "execution's floor template cannot carry that declaration to "
                        "the spawn gate; keeping the floor for this fire rather than "
                        "dispatching a restricted agent whose allowlist would not be "
                        "honoured",
                        job.name,
                        _default_agent,
                        (
                            "declares toolsSettings.subagent.availableAgents"
                            if _declared
                            else "has an unreadable agent spec, so its "
                            "toolsSettings.subagent.availableAgents is unknown"
                        ),
                    )
                else:
                    cron_agent = _default_agent
                    _default_substituted = True

            def _resolve_cron_agent(
                alias: str | None,
                *,
                template_namespace: bool = False,
                sequence_step: bool = False,
            ) -> "tuple[str | None, str | None, str | None]":
                """Resolve a cron agent alias to (kiro_agent, cwd, crew_alias).

                A cron bound to a Slack channel carries that channel's agent
                ALIAS (e.g. ``in-3d``) in ``job.agent_id`` / ``agent_sequence``.
                kiro-cli only accepts a materialized agent MODE, not a Kiro Crew
                alias, so dispatching the alias verbatim fails closed with
                "Agent mode 'in-3d' is not available … its ~/.kiro/agents/
                in-3d.json is likely missing". The dashboard chat path already
                collapses the alias the right way — see
                ``chat_runner._allocation_kwargs``, which passes
                ``agent=<kiro_agent>`` alongside ``crew_agent=<alias>`` so
                ``prepare_runtime`` still resolves the member identity from the
                alias. The cron path was the one turn-running surface that
                skipped it.

                Mirror that here: dispatch the alias's ``kiro_agent`` (usually
                ``kirocrew``) as ``agent``, run in the agent's workspace ``cwd``,
                AND return the ``crew_alias`` so the caller can pass it as
                ``crew_agent=`` — without which ``resolve_crew_identity`` sees
                only the bare kiro template name (not a ``config.agents`` key),
                returns ``""``, and every member-capability gate, the crew's
                pinned model / reasoning-effort, and its watchdog windows are
                silently skipped. Returns (None, None, None) on any miss so the
                caller falls back to the raw value unchanged (behavior-preserving
                for a job whose agent is already a real mode or is unset).

                ``sequence_step`` marks a declared ``agent_sequence`` dispatch,
                where ``alias`` is the step's OWN crew, not the schedule's
                captured member. The captured-member pin below is then skipped so
                each step resolves its own crew's kiro_agent, workspace, pinned
                model and capability gates; the captured memory store is shared
                by every step regardless (it is bound per session key by the
                caller), so the run keeps one memory identity while the steps run
                as the crews the sequence names. Without this, every step
                resolved to the captured member and the other crews named in the
                sequence never actually ran.
                """
                if not alias:
                    return None, None, None
                # A template-namespace name is NOT a crew alias, even when it
                # happens to key ``cfg.agents``. ``agent.default_agent`` is a
                # free-form str in the template namespace with no alias
                # validation (config/sections.py), so an operator naming a crew
                # member there is ordinary input -- and the collapse below keys
                # only on membership in ``cfg.agents``, then returns the alias as
                # ``crew_agent``, which ``resolve_crew_identity`` honours
                # verbatim. That would give an agent-less cron the named crew's
                # workspace, pinned model and capability MCP servers, silently
                # apart from a DEBUG line. Return the EXPLICIT no-crew spelling
                # rather than the miss tuple the other early returns use: `""`
                # is what `resolve_crew_identity` documents as opting out of the
                # fallback ("an explicit crew_agent wins verbatim -- including
                # \"\""), while `None` is the fallback-ENABLED spelling, so a
                # `None` here would still let `if agent and agent in
                # config.agents` reselect the colliding crew (loader.py) and the
                # bypass would be a no-op. `kiro_agent`/`cwd` stay None so the
                # name dispatches as the TEMPLATE it was configured as, in the
                # default workspace.
                # Consequence to expect, and the intended one: a default that is
                # really only a crew alias now fails closed and loudly with
                # "Agent mode '<name>' is not available", instead of quietly
                # inheriting that crew's identity.
                if template_namespace:
                    return None, None, ""
                try:
                    from kiro_crew.config.loader import (
                        resolve_agent_bindings,
                        workspace_dir_from_entry,
                    )

                    cfg = getattr(self, "_cfg", None)
                    # Prefer the last APPLIED reload over the boot snapshot: an
                    # agent created at runtime (dashboard/CLI) writes cfg.agents
                    # in live.snapshot() but not in the boot self._cfg, so a
                    # boot-only read would miss a hot-added alias and dispatch it
                    # raw (failing closed) on every fire until a gateway restart.
                    # Same one-liner the watchdog/mcp reads use elsewhere here.
                    cfg = live.snapshot() or cfg
                    if cfg is None:
                        return None, None, None
                    agents = getattr(cfg, "agents", None) or {}
                    # Resolve by IDENTITY, not by name, for a member execution.
                    # The memory store is captured at authoring (cron_execution),
                    # but the runtime/workspace/crew_agent are resolved live here;
                    # if a same-name alias was deleted and recreated, a by-name
                    # lookup would bind the NEW member's runtime while the store
                    # stays the retired member's silo — a cross-identity memory
                    # leak (the memory-store-seam execution_context.py guards).
                    # member_config_for_id pins resolution to the captured
                    # member_id, so the alias whose bindings we read is the same
                    # member the store belongs to; a mismatch (recreated/renamed)
                    # raises and we fall back to the raw value unchanged.
                    #
                    # Skipped for a declared agent_sequence step: there the
                    # captured member is the run's shared memory identity, not
                    # the agent each step runs as, and ``alias`` already names
                    # the step's OWN crew. Pinning here would collapse every step
                    # onto the captured member, so the sequence step falls
                    # through to the by-alias path below instead.
                    resolved_alias = alias
                    captured_member = (
                        None if sequence_step else getattr(cron_execution, "member_id", None)
                    )
                    if captured_member:
                        try:
                            from kiro_crew.execution_context import member_config_for_id

                            resolved_alias, _ = member_config_for_id(cfg, captured_member)
                        except Exception:
                            logger.debug(
                                "cron member identity %r not resolvable in live cfg; "
                                "leaving agent %r unchanged",
                                captured_member,
                                alias,
                                exc_info=True,
                            )
                            return None, None, None
                    # For a non-member (template/legacy) execution there is no
                    # identity to pin to: ONLY collapse a real Kiro Crew alias.
                    # resolve_agent_bindings falls back to the default agent for
                    # an unknown name, so resolving unconditionally would rewrite
                    # a legitimate kiro mode (e.g. 'kirocrew-lite') into the
                    # default. A name that is not an alias is either a real mode
                    # or unset — leave it untouched.
                    elif alias not in agents:
                        return None, None, None
                    # validate_memory_files=False: we only need the alias's
                    # kiro_agent + workspace mapping here, and this runs on the
                    # gateway event loop. The default (True) does a synchronous
                    # store dir stat + SQLite identity read, which would stall
                    # the loop on every cron fire; the session's own
                    # execution-context resolution validates the store later.
                    bindings = resolve_agent_bindings(
                        cfg, resolved_alias, validate_memory_files=False
                    )
                    kiro_agent = bindings.kiro_agent or None
                    # Anchor the workspace dir by the one placement rule. The
                    # resolved bindings.workspace_dir is the RAW configured value
                    # (e.g. the shipped relative default "workspace"); handing
                    # that to the provider as a cwd would resolve it against the
                    # gateway PROCESS directory, not the data home. Resolve the
                    # alias's workspace entry through workspace_dir_from_entry so
                    # a relative dir anchors under config_dir() and an absolute
                    # dir is honored as-is.
                    agent_cfg = agents.get(resolved_alias)
                    ws_name = getattr(agent_cfg, "workspace", None) if agent_cfg else None
                    ws_entry = None
                    if ws_name:
                        ws_entry = getattr(cfg, "workspaces", {}).get(ws_name)
                    # workspace_dir_from_entry(None) is the BASE workspace
                    # directory under config_dir() — the documented answer for an
                    # unmapped/empty workspace name (loader.py's workspace_dir_for
                    # rule). Deliberately NOT cfg.default_workspace's dir: that
                    # rule forbids an unmapped name hopping to whatever absolute
                    # dir the default declares, so a missing mapping anchors to
                    # the data home rather than escaping it (or defaulting to the
                    # gateway process cwd via a None).
                    ws_dir = workspace_dir_from_entry(ws_entry)
                    cwd = str(ws_dir) if ws_dir else None
                    # Carry the identity-resolved alias back as crew_alias:
                    # prepare_runtime needs it to resolve the member identity (the
                    # kiro_agent name alone is not a config.agents key), and it is
                    # the alias pinned to the captured member_id above.
                    return kiro_agent, cwd, resolved_alias
                except Exception:
                    logger.debug("cron agent resolve failed for %r", alias, exc_info=True)
                    return None, None, None

            # ── Concurrent execution guard ──
            if (job.script or job.command) and job.id in self._running_script_ids:
                logger.info("Cron '%s': previous execution still running, skipping", job.name)
                return None

            # ── Command mode: direct shell execution (sandboxed) ──
            if job.command:
                if getattr(self.sessions, "admission_closed", False) is True:
                    logger.info("Cron '%s': gateway admission is closed, deferring", job.name)
                    _defer_cron_before_dispatch(job, "gateway admission is closed")
                    return None
                self._running_script_ids.add(job.id)
                # Bound to the overlap guard's own lifetime, and created HERE rather
                # than at the submit below so the ``finally`` that releases the guard
                # can always reach it -- including on the fire-time deny paths that
                # return before anything is submitted.
                handoff = _ClaimHandoff()
                try:
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_command_exec",
                            tool_kind="cron_command",
                            outcome="invoked",
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron command invoked path", exc_info=True
                        )
                    # Re-run governance at fire time, not just at cron_add authoring
                    # time. A job vetted when it was scheduled can outlive a later
                    # policy tightening: the mcp_cron _vet_* gates only run once, at
                    # authoring, so a ceiling change has no effect on an
                    # already-scheduled job until someone notices and re-authors it.
                    # Denial here does not delete the job, so a later policy
                    # loosening lets it resume on its own. vet_job_at_fire_time is
                    # the shared gate for all three job kinds (command/script/message).
                    # Off-loop: the script variant of this gate reads the script
                    # file from disk, and governance profile resolution can touch
                    # the filesystem too — neither may block the event loop.
                    #
                    # On the GOVERNANCE pool, deliberately NOT the cron pool. This
                    # await sits inside the deadline _execute_with_timeout arms
                    # BEFORE the callback runs, so whatever this gate waits for is
                    # charged to the job's own execution budget. The cron pool is
                    # bounded at _MAX_CRON_WORKERS and its workers are held for a
                    # whole job's DURATION, so gating there puts a short policy
                    # check behind however many long-running command/script jobs
                    # currently occupy it -- and a job whose budget is spent that
                    # way is killed having run no code, reported as an overrun,
                    # and (if delete_after_run) deleted without ever dispatching.
                    # The governance pool holds only short, bounded policy work,
                    # which is what makes the residual wait here proportionate.
                    #
                    # The alternative -- widening every job's deadline by the pool
                    # allowance instead -- is the wrong lever twice over: it would
                    # delay the wedged-delivery backstop by that allowance for
                    # runs that never queue, and it would leave THIS wait
                    # unbounded and still misreported, merely later.
                    gate_reason, gate_starved = await _await_cron_fire_time_gate(
                        job, tool_name="cron_command_exec", tool_kind="cron_command"
                    )
                    if gate_starved:
                        return None
                    if gate_reason:
                        # Deliberately NOT record_failure(): a governance denial
                        # is a policy state, not a job defect. Counting it would
                        # auto-pause the job after _AUTO_PAUSE_THRESHOLD fires,
                        # and a paused job never fires again — breaking the
                        # documented resume-on-policy-loosening semantic.
                        # A denial is result-less: without this the run shows a
                        # PREVIOUS run's output beside this run's error status.
                        job.clear_carried_result()
                        job.last_status = "error"
                        job.last_error = redact(gate_reason)
                        job.fire_time_denied = True
                        try:
                            sel().log_tool_invocation(
                                session_key=f"cron:{job.id}",
                                tool_name="cron_command_exec",
                                tool_kind="cron_command",
                                outcome="denied",
                            )
                        except Exception:
                            logger.debug(
                                "SEL logging failed in cron command fire-time deny path",
                                exc_info=True,
                            )
                        await _alert_cron_failure(job, gate_reason, denied=True)
                        return None
                    cmd_timeout = job.timeout or 300
                    # Queue wait is NOT charged to cmd_timeout: see
                    # run_in_cron_pool.  The timeout here is a backstop only --
                    # run_command_sandboxed already enforces cmd_timeout on the
                    # subprocess itself -- but it must stay, or a wedged worker
                    # leaves this entry un-failed forever.
                    #
                    # Submitted through _vet_at_claim_then for the same reason as
                    # the script site: the command TEXT cannot be substituted
                    # (it is already captured in job.command), but the governance
                    # POLICY it was vetted against can tighten during the queue
                    # wait, and the gate above ran before that wait.
                    result = await run_in_cron_pool(
                        _vet_at_claim_then,
                        handoff,
                        job,
                        run_command_sandboxed,
                        job.command,
                        cmd_timeout,
                        job.id,
                        job.secret_env,
                        job.secret_env_pin,
                        timeout=_claim_backstop(job, cmd_timeout),
                    )
                    if result.get("status") == "cancelled":
                        # User-initiated cancel: CronService.cancel() owns the
                        # bookkeeping/history — no failure counting, no delivery.
                        return None
                    if result.get("status") == "skipped":
                        # Overlapping wake refused because this job is already
                        # spawning or running. Not a job defect, so no failure
                        # counting and no delivery -- counting it would strike a
                        # job for a transient scheduling overlap.
                        #
                        # But returning None alone is NOT neutral: _execute treats
                        # any non-"error" last_status as success, so it would set
                        # last_status="ok" AND call record_success(), fabricating a
                        # run that never happened and refilling the auto-pause
                        # budget. The cancelled branch above escapes that only
                        # because _execute additionally checks the run claim's
                        # cancel flag, and a refused overlap never sets it.
                        #
                        # So use the established deliberately-neutral shape of the
                        # starvation and fire-time-denial paths: last_status="error"
                        # to skip the success branch, run_never_started=True as the
                        # retention marker, and DELIBERATELY NOT record_failure() --
                        # a strike is only ever counted by that explicit call, never
                        # by last_status, so this spends no budget and refills none.
                        logger.info("Cron '%s': overlapping run refused, skipping", job.name)
                        job.clear_carried_result()
                        job.last_status = "error"
                        job.last_error = "Another run of this job was already starting or running"
                        job.run_never_started = True
                        return None
                    output = result.get("output", "")
                    if not output.strip():
                        if result.get("status") == "ok":
                            # Cleared, not marked: last_status already says the run
                            # succeeded, so last_result carries produced text only.
                            job.clear_carried_result()
                            job.last_status = "ok"
                            job.last_error = ""
                            job.record_success()
                            # Same reason record_success resets consecutive_failures: a run that
                            # worked proves the job can work, so the shared-death streak that
                            # substitutes for that counter is reset with it. Without this the
                            # streak is a LIFETIME total, so after enough deaths the bound is
                            # permanently tripped and the exemption silently stops applying.
                            runtime_death.clear_shared_deaths(f"cron:{job.id}")
                        else:
                            # Cleared so displays fall back to last_error below.
                            job.clear_carried_result()
                            job.last_status = "error"
                            job.last_error = (
                                f"non-ok status with no output (status={result.get('status')})"
                            )
                            job.record_failure()
                            await _alert_cron_failure(job, job.last_error)
                        return None  # no output = no delivery
                    job.set_run_result(redact(output))
                    job.last_error = ""
                    if result.get("status") == "ok":
                        job.last_status = "ok"
                        job.record_success()
                        # Same reason record_success resets consecutive_failures: a run that
                        # worked proves the job can work, so the shared-death streak that
                        # substitutes for that counter is reset with it. Without this the
                        # streak is a LIFETIME total, so after enough deaths the bound is
                        # permanently tripped and the exemption silently stops applying.
                        runtime_death.clear_shared_deaths(f"cron:{job.id}")
                    else:
                        job.last_status = "error"
                        job.last_error = f"command failed (exit_code={result.get('exit_code')})"
                        job.record_failure()
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_command_exec",
                            tool_kind="cron_command",
                            outcome=job.last_status,
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron command result path", exc_info=True
                        )
                    if job.last_status == "error":
                        # A non-zero exit DOES produce output, and that output is
                        # the reason — carry it, not just the exit code.
                        await _alert_cron_failure(job, f"{job.last_error}\n{output}")
                    return job.last_result
                except CronQueueTimeout as exc:
                    # Pool starvation, not a broken command: every worker was
                    # busy for the whole budget so this never started.  Say so,
                    # or the next saturation reads as N independent failures.
                    job.clear_carried_result()
                    job.last_error = str(exc)
                    job.last_status = "error"
                    # Retention-only marker: a one-shot must not be consumed by a
                    # run it never had.  NOT fire_time_denied -- that would also
                    # park an at-job disabled and call this a policy denial.
                    job.run_never_started = True
                    # Deliberately NOT record_failure(): starvation is a fleet
                    # state, not a job defect, exactly as a fire-time governance
                    # denial is a policy state.  Counting it would auto-pause the
                    # job after _AUTO_PAUSE_THRESHOLD starved wakes, and a paused
                    # job never fires again -- so a pool that recovers would leave
                    # a perfectly healthy job disabled and its work unscheduled.
                    # The distinct error text above is what makes the saturation
                    # legible; the counter is for runs that actually ran.
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_command_exec",
                            tool_kind="cron_command",
                            outcome="error",
                            error=str(exc),
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron command queue-timeout path",
                            exc_info=True,
                        )
                    return None
                except asyncio.TimeoutError:
                    # Retain the one-shot when the payload never started. This arm
                    # is the CLAIM BACKSTOP expiring, and it fires for two
                    # different runs: a slow claim-time vet that burned the bound
                    # before ``claim()`` was ever granted, and a payload that DID
                    # start and then overran. Only the first is a never-started
                    # run, and ``abandon()`` is the only thing that can tell them
                    # apart -- it returns whether the payload had started, and
                    # ``claim()`` refuses once it has been called, so a False here
                    # can never become True later. Without this the marker stayed
                    # unset and ``_merge_job_result`` consumed a
                    # ``delete_after_run`` job that dispatched nothing.
                    #
                    # Deliberately scoped to THIS arm rather than to the shared
                    # ``finally`` below, which also runs on the fire-time deny
                    # path: a deny reaches it with the payload equally unstarted,
                    # but its retention is owned by ``fire_time_denied``, whose
                    # readers park an at-job disabled. Setting this marker there
                    # would park a job for a policy decision never made -- the
                    # opposite silent failure. ``abandon()`` is documented
                    # idempotent, so calling it here and again in the ``finally``
                    # is safe.
                    started = handoff.abandon()
                    job.run_never_started = not started
                    job.clear_carried_result()
                    job.last_error = f"timeout ({cmd_timeout + 5}s)"
                    job.last_status = "error"
                    # Count only a run that DISPATCHED. The same reasoning the
                    # starvation, gate-deny and vet-overrun arms above already
                    # apply: a backstop that expired before ``claim()`` means no
                    # line of this job ran, so it is a fleet state rather than a
                    # job defect, and counting it auto-pauses at
                    # _AUTO_PAUSE_THRESHOLD -- a paused job never fires again, so
                    # repeated wedged wakes would permanently disable a healthy
                    # job and leave its work unscheduled. ``cron.py``'s
                    # _execute_with_timeout guard already refuses to count a
                    # never-started run, but this arm calls record_failure()
                    # DIRECTLY and so never reaches it. A genuine overrun (the
                    # payload started, then ran long) still counts, which is what
                    # keeps this a discriminator rather than a deletion.
                    if started:
                        job.record_failure()
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_command_exec",
                            tool_kind="cron_command",
                            outcome="timeout",
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron command timeout path", exc_info=True
                        )
                    await _alert_cron_failure(job, f"command {job.last_error}")
                    return None
                except CronClaimTimeDenied as exc:
                    # Governance refused this run when the worker claimed it --
                    # the policy tightened during the queue wait.  Same
                    # disposition as the fire-time deny above, deliberately:
                    # result-less, keeps the job, and NOT record_failure(),
                    # because a policy state must not feed the auto-pause
                    # counter.  This clause exists so the run cannot reach the
                    # generic arm below, which does count it.
                    job.clear_carried_result()
                    job.last_status = "error"
                    job.last_error = redact(exc.reason)
                    job.fire_time_denied = True
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_command_exec",
                            tool_kind="cron_command",
                            outcome="denied",
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron command claim-time deny path",
                            exc_info=True,
                        )
                    return None
                except CronVetOverran as exc:
                    # The vet outran the allowance the deadline carries for it, so
                    # the payload was REFUSED rather than started with a margin
                    # that no longer covers its own bound plus teardown.  Nothing
                    # ran, so this is retention-shaped like starvation: mark the
                    # run never-started and deliberately do NOT record_failure() --
                    # a slow governance read is a fleet state, not a job defect,
                    # and counting it would auto-pause a healthy job at
                    # _AUTO_PAUSE_THRESHOLD.  NOT fire_time_denied: no policy
                    # decision was made, and that flag also parks an at-job.
                    logger.warning("Cron '%s': %s; payload refused", job.name, exc)
                    job.clear_carried_result()
                    job.last_error = str(exc)
                    job.last_status = "error"
                    job.run_never_started = True
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_command_exec",
                            tool_kind="cron_command",
                            outcome="error",
                            error=str(exc),
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron command vet-overrun path", exc_info=True
                        )
                    return None
                except asyncio.CancelledError:
                    # The wake deadline cancelled this callback outright, so none of
                    # the arms above ran: CancelledError is a BaseException, which
                    # the ``except Exception`` below deliberately does not catch.
                    # Without this the run reaches cron.py's delete site with
                    # neither retention flag set, so a ``delete_after_run`` one-shot
                    # is consumed having never executed.
                    #
                    # ``abandon()`` is the discriminator, exactly as the claim
                    # backstop above uses it: it reports whether the payload had
                    # already started, and ``claim()`` refuses once abandoned, so a
                    # False can never later become True.  Assigning ``not started``
                    # rather than setting True unconditionally is what keeps the
                    # opposite failure closed -- a payload that DID run reports True,
                    # so the marker stays clear and the one-shot is still consumed
                    # instead of firing a second time.
                    #
                    # Deliberately NOT in the shared ``finally`` below, which the
                    # fire-time deny path also reaches with the payload equally
                    # unstarted: retention there is owned by ``fire_time_denied``,
                    # and setting this marker would park an at-job disabled for a
                    # policy decision that was never made.
                    job.run_never_started = not handoff.abandon()
                    raise
                except Exception as exc:
                    logger.exception("Command cron '%s' failed: %s", job.name, exc)
                    job.clear_carried_result()
                    err_str = redact(str(exc))
                    job.last_error = err_str[:200]
                    job.last_status = "error"
                    job.record_failure()
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_command_exec",
                            tool_kind="cron_command",
                            outcome="error",
                        )
                    except Exception:
                        logger.debug("SEL logging failed in cron command error path", exc_info=True)
                    await _alert_cron_failure(job, f"{type(exc).__name__}: {exc}")
                    return None
                finally:
                    # Abandon BEFORE releasing the overlap guard, and do it here
                    # rather than in the timeout arm because this ``finally`` is the
                    # single place the guard is released -- so it also covers any
                    # exit no ``except`` arm above handles.  A no-op on the success
                    # path: the payload already ran.  ``abandon()`` is idempotent,
                    # so the cancellation arm above having already called it changes
                    # nothing observed here.
                    handoff.abandon()
                    self._running_script_ids.discard(job.id)

            # ── Code-based script execution (deterministic, no LLM) ──
            if job.script:
                if getattr(self.sessions, "admission_closed", False) is True:
                    logger.info("Cron '%s': gateway admission is closed, deferring", job.name)
                    _defer_cron_before_dispatch(job, "gateway admission is closed")
                    return None
                self._running_script_ids.add(job.id)
                # Bound to the overlap guard's own lifetime, and created HERE rather
                # than at the submit below so the ``finally`` that releases the guard
                # can always reach it -- including on the fire-time deny paths that
                # return before anything is submitted.
                handoff = _ClaimHandoff()
                try:
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name=job.script,
                            tool_kind="cron_script",
                            outcome="invoked",
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron script invoked path", exc_info=True
                        )
                    # Fire-time governance gate — mirrors the command path above.
                    # vet_job_at_fire_time re-runs the capabilities.cron gate AND
                    # re-scans the script BODY on the freshly re-resolved path
                    # (which also validates the path, making a bare
                    # resolve_script_path call here redundant), so a policy
                    # tightened after scheduling — or a script file edited on disk
                    # after authoring — denies this run. The job is kept: a later
                    # policy loosening lets it resume on its own.
                    # Off-loop: reads the script body from disk (up to the scan
                    # cap) — must not block the event loop on a wedged FS.
                    # Governance pool, not the cron pool: see the command site.
                    gate_reason, gate_starved = await _await_cron_fire_time_gate(
                        job, tool_name="cron_script_exec", tool_kind="cron_script"
                    )
                    if gate_starved:
                        return None
                    if gate_reason:
                        # No record_failure() — see the command-path deny above:
                        # a policy denial must not feed the auto-pause counter.
                        # A denial is result-less: without this the run shows a
                        # PREVIOUS run's output beside this run's error status.
                        job.clear_carried_result()
                        job.last_status = "error"
                        job.last_error = redact(gate_reason)
                        job.fire_time_denied = True
                        try:
                            sel().log_tool_invocation(
                                session_key=f"cron:{job.id}",
                                tool_name=job.script,
                                tool_kind="cron_script",
                                outcome="denied",
                            )
                        except Exception:
                            logger.debug(
                                "SEL logging failed in cron script fire-time deny path",
                                exc_info=True,
                            )
                        await _alert_cron_failure(job, gate_reason, denied=True)
                        return None
                    # Run in sandboxed subprocess via wrap_argv()
                    script_timeout = job.timeout or 30
                    # Queue wait is NOT charged to script_timeout: see
                    # run_in_cron_pool.  The timeout here is a backstop only --
                    # run_script_sandboxed already enforces script_timeout on
                    # the subprocess itself -- but it must stay, or a wedged
                    # worker leaves this entry un-failed forever.
                    #
                    # Submitted through _vet_at_claim_then, not bare: the gate
                    # above ran before the queue wait, and the launcher re-reads
                    # the body from disk in the child, so the gate's scan alone
                    # authorises bytes that may no longer be there.  The re-vet
                    # runs inside the worker, after the wait, so the decision
                    # holds at the moment of use.  It shares the backstop below,
                    # which is why it must stay short.
                    result = await run_in_cron_pool(
                        _vet_at_claim_then,
                        handoff,
                        job,
                        run_script_sandboxed,
                        job.script,
                        job.id,
                        job.message,
                        script_timeout,
                        job.secret_env,
                        job.secret_env_pin,
                        delivery_fingerprint(
                            job.session_key,
                            job.silent,
                            job.channel or "",
                            job.thread_ts or "",
                        ),
                        self._live_internal_secret,
                        timeout=_claim_backstop(job, script_timeout),
                    )
                    status = result.get("status", "error")
                    if status == "cancelled":
                        # User-initiated cancel: CronService.cancel() owns the
                        # bookkeeping/history — no failure counting, no delivery.
                        return None
                    if status == "skipped":
                        # Overlapping wake refused because this job is already
                        # spawning or running -- no failure counting, no delivery.
                        # See the command path for why returning None alone would
                        # be recorded as a SUCCESS: last_status="error" skips
                        # _execute's success branch, run_never_started=True is the
                        # retention marker, and record_failure() is deliberately
                        # NOT called so the overlap costs no auto-pause strike.
                        logger.info("Cron '%s': overlapping run refused, skipping", job.name)
                        job.clear_carried_result()
                        job.last_status = "error"
                        job.last_error = "Another run of this job was already starting or running"
                        job.run_never_started = True
                        return None
                    if status == "ok":
                        job.clear_carried_result()
                        job.last_error = ""
                        job.last_status = "ok"
                        job.record_success()
                        # Same reason record_success resets consecutive_failures: a run that
                        # worked proves the job can work, so the shared-death streak that
                        # substitutes for that counter is reset with it. Without this the
                        # streak is a LIFETIME total, so after enough deaths the bound is
                        # permanently tripped and the exemption silently stops applying.
                        runtime_death.clear_shared_deaths(f"cron:{job.id}")
                        try:
                            sel().log_tool_invocation(
                                session_key=f"cron:{job.id}",
                                tool_name=job.script,
                                tool_kind="cron_script",
                                outcome="ok",
                            )
                        except Exception:
                            logger.debug("SEL logging failed in cron script ok path", exc_info=True)
                        return "ok"
                    elif status == "skip":
                        # A completed Skip is a successful run that chose no-op —
                        # the same "success" outcome as the ok/done/report
                        # siblings above. Unlike them it deliberately does NOT
                        # call job.record_success() here: CronScheduler._execute
                        # is the backstop that resets consecutive_failures (and
                        # lifts auto-pause) on every non-error return — Skip
                        # included, since this branch returns None without
                        # setting last_status="error" — and its reset is guarded
                        # by the claim.cancelled cancel-race check. Resetting in
                        # this branch would bypass that guard and could re-enable
                        # a job cancelled mid-tick.
                        # Result-less like the deny paths: a Skip that carried the
                        # previous run's output read as though it had produced it.
                        job.clear_carried_result()
                        try:
                            sel().log_tool_invocation(
                                session_key=f"cron:{job.id}",
                                tool_name=job.script,
                                tool_kind="cron_script",
                                outcome="skip",
                            )
                        except Exception:
                            logger.debug(
                                "SEL logging failed in cron script skip path", exc_info=True
                            )
                        return None
                    elif status == "done":
                        msg = result.get("message", "")
                        script_msg = redact(msg) if msg else ""
                        job.set_run_result(script_msg)
                        job.last_error = ""
                        job.last_status = "ok"
                        job.record_success()
                        # Same reason record_success resets consecutive_failures: a run that
                        # worked proves the job can work, so the shared-death streak that
                        # substitutes for that counter is reset with it. Without this the
                        # streak is a LIFETIME total, so after enough deaths the bound is
                        # permanently tripped and the exemption silently stops applying.
                        runtime_death.clear_shared_deaths(f"cron:{job.id}")
                        # Deliver Done message and remove job
                        await _deliver_script_result(job, script_msg, remove=True)
                        try:
                            sel().log_tool_invocation(
                                session_key=f"cron:{job.id}",
                                tool_name=job.script,
                                tool_kind="cron_script",
                                outcome="done",
                            )
                        except Exception:
                            logger.debug(
                                "SEL logging failed in cron script done path", exc_info=True
                            )
                        return script_msg or "done"
                    elif status == "report":
                        msg = result.get("message", "")
                        script_msg = redact(msg) if msg else ""
                        job.set_run_result(script_msg)
                        job.last_error = ""
                        job.last_status = "ok"
                        job.record_success()
                        # Same reason record_success resets consecutive_failures: a run that
                        # worked proves the job can work, so the shared-death streak that
                        # substitutes for that counter is reset with it. Without this the
                        # streak is a LIFETIME total, so after enough deaths the bound is
                        # permanently tripped and the exemption silently stops applying.
                        runtime_death.clear_shared_deaths(f"cron:{job.id}")
                        # Deliver Report message (keep job running)
                        await _deliver_script_result(job, script_msg)
                        try:
                            sel().log_tool_invocation(
                                session_key=f"cron:{job.id}",
                                tool_name=job.script,
                                tool_kind="cron_script",
                                outcome="report",
                            )
                        except Exception:
                            logger.debug(
                                "SEL logging failed in cron script report path", exc_info=True
                            )
                        return script_msg or "report"
                    else:
                        err = result.get("error", "unknown error")
                        raise RuntimeError(err)
                except CronQueueTimeout as exc:
                    # Pool starvation, not a broken script: every worker was
                    # busy for the whole budget so this never ran a line.  The
                    # distinct text is what makes the next saturation legible
                    # instead of looking like N scripts that each overran.
                    logger.warning("Script cron '%s' never got a worker slot: %s", job.name, exc)
                    job.clear_carried_result()
                    job.last_error = str(exc)
                    job.last_status = "error"
                    # Retention-only marker: see the command path.
                    job.run_never_started = True
                    # Deliberately NOT record_failure(): see the command path.
                    # Starvation means the script never ran a line, so counting it
                    # would auto-pause a healthy job after _AUTO_PAUSE_THRESHOLD
                    # starved wakes and leave it disabled once the pool recovered.
                    # No auto-pause log here either: this path cannot reach the
                    # threshold.
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name=job.script,
                            tool_kind="cron_script",
                            outcome="error",
                            error=str(exc),
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron script queue-timeout path",
                            exc_info=True,
                        )
                    return None
                except asyncio.TimeoutError:
                    # See the command path above: this is the claim backstop, and
                    # only ``abandon()`` distinguishes a vet that burned the bound
                    # before ``claim()`` from a payload that started and overran.
                    # Scoped to this arm, not the shared ``finally``, so the
                    # fire-time deny path keeps its retention in
                    # ``fire_time_denied`` instead of parking an at-job disabled.
                    started = handoff.abandon()
                    job.run_never_started = not started
                    logger.warning(
                        "Script cron '%s' timed out after %ds", job.name, script_timeout + 5
                    )
                    job.clear_carried_result()
                    job.last_error = f"timeout ({script_timeout + 5}s)"
                    job.last_status = "error"
                    # See the command path: count only a run that DISPATCHED, so a
                    # backstop that expired before ``claim()`` cannot auto-pause a
                    # job that never ran a line. A genuine overrun still counts.
                    if started:
                        job.record_failure()
                    if job.auto_paused:
                        logger.warning(
                            "Script cron '%s' auto-paused after %d consecutive errors",
                            job.name,
                            job.consecutive_failures,
                        )
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name=job.script,
                            tool_kind="cron_script",
                            outcome="error",
                            error=f"timeout ({script_timeout + 5}s)",
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron script timeout path", exc_info=True
                        )
                    await _alert_cron_failure(job, f"script {job.last_error}")
                    return None
                except CronClaimTimeDenied as exc:
                    # Governance refused this run when the worker claimed it --
                    # the body on disk, or the policy, changed during the queue
                    # wait.  Same disposition as the fire-time deny above,
                    # deliberately: result-less, keeps the job, and NOT
                    # record_failure(), because a policy state must not feed the
                    # auto-pause counter.  This clause exists so the run cannot
                    # reach the generic arm below, which does count it.
                    job.clear_carried_result()
                    job.last_status = "error"
                    job.last_error = redact(exc.reason)
                    job.fire_time_denied = True
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name=job.script,
                            tool_kind="cron_script",
                            outcome="denied",
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron script claim-time deny path",
                            exc_info=True,
                        )
                    return None
                except CronVetOverran as exc:
                    # The vet outran the allowance the deadline carries for it, so
                    # the payload was REFUSED rather than started with a margin
                    # that no longer covers its own bound plus teardown.  Nothing
                    # ran, so this is retention-shaped like starvation: mark the
                    # run never-started and deliberately do NOT record_failure() --
                    # a slow governance read is a fleet state, not a job defect,
                    # and counting it would auto-pause a healthy job at
                    # _AUTO_PAUSE_THRESHOLD.  NOT fire_time_denied: no policy
                    # decision was made, and that flag also parks an at-job.
                    logger.warning("Cron '%s': %s; payload refused", job.name, exc)
                    job.clear_carried_result()
                    job.last_error = str(exc)
                    job.last_status = "error"
                    job.run_never_started = True
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name=job.script,
                            tool_kind="cron_script",
                            outcome="error",
                            error=str(exc),
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron script vet-overrun path", exc_info=True
                        )
                    return None
                except asyncio.CancelledError:
                    # See the command path above: a wake deadline cancelling this
                    # callback runs no ``except`` arm, because CancelledError is a
                    # BaseException, so without this the delete site consumes a
                    # one-shot that never executed.  ``abandon()`` reports whether
                    # the payload had started, so ``not started`` retains only the
                    # run that dispatched nothing and leaves a completed run
                    # deletable.  Not in the shared ``finally``, whose deny path
                    # retention belongs to ``fire_time_denied``.
                    job.run_never_started = not handoff.abandon()
                    raise
                except Exception as exc:
                    logger.exception("Script cron '%s' failed: %s", job.name, exc)
                    job.clear_carried_result()
                    err_str = redact(str(exc))
                    job.last_error = err_str
                    job.last_status = "error"
                    job.record_failure()
                    if job.auto_paused:
                        logger.warning(
                            "Script cron '%s' auto-paused after %d consecutive errors",
                            job.name,
                            job.consecutive_failures,
                        )
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name=job.script,
                            tool_kind="cron_script",
                            outcome="error",
                            error=err_str,
                        )
                    except Exception:
                        logger.debug("SEL logging failed in cron script error path", exc_info=True)
                    # The reason a script cron dies is often environmental (a
                    # startup RuntimeError, a missing dependency) and identical on
                    # every fire, so it read as an idle job rather than a broken
                    # one until this alert carried the reason out.
                    await _alert_cron_failure(job, f"{type(exc).__name__}: {exc}")
                    return None
                finally:
                    # Abandon BEFORE releasing the overlap guard, and do it here
                    # rather than in the timeout arm because this ``finally`` is the
                    # single place the guard is released -- so it also covers any
                    # exit no ``except`` arm above handles.  A no-op on the success
                    # path: the payload already ran.  ``abandon()`` is idempotent,
                    # so the cancellation arm above having already called it changes
                    # nothing observed here.
                    handoff.abandon()
                    self._running_script_ids.discard(job.id)

            # ── Fire-time governance gate: message (LLM) jobs ──
            # Command and script jobs are gated inside their blocks above; a job
            # reaching this point dispatches an LLM turn, so it is gated here too:
            # without this check, disabling the cron capability after scheduling
            # would leave message jobs firing. Same deny semantics as the other
            # kinds: mark the run failed, keep the job.
            # Off-loop for the same reason as the command/script sites above, and
            # on the GOVERNANCE pool for the same reason: a message job gets no
            # pool allowance on either deadline, so gating it on the cron pool
            # would charge a saturated pool's queue directly to its execution
            # budget.
            gate_reason, gate_starved = await _await_cron_fire_time_gate(
                job, tool_name="cron_message_dispatch", tool_kind="cron_message"
            )
            if gate_starved:
                return None
            if gate_reason:
                # No record_failure() — see the command-path deny above: a
                # policy denial must not feed the auto-pause counter.
                job.last_status = "error"
                job.last_error = redact(gate_reason)
                job.fire_time_denied = True
                try:
                    sel().log_tool_invocation(
                        session_key=f"cron:{job.id}",
                        tool_name="cron_message_dispatch",
                        tool_kind="cron_message",
                        outcome="denied",
                    )
                except Exception:
                    logger.debug(
                        "SEL logging failed in cron message fire-time deny path", exc_info=True
                    )
                await _alert_cron_failure(job, gate_reason, denied=True)
                return None

            # ── First-run tab pre-create ──
            # The result injection at the end of this callback is the other
            # creator site for the job's dashboard tab, and on its own it leaves
            # a NEW job's first run without one: session-control caller identity
            # (caller_slot_key walks slot links for cron:{id}) refuses every verb
            # with caller_unidentified, and the dashboard-surface registry has no
            # row for sub-agent/widget/question/approval routing. Bind the tab up
            # front — eligibility (persistent_session and not hide_in_chat) lives
            # inside the helper, so script/command jobs never reach it (they
            # return above) and ineligible message jobs are untouched. Placed
            # AFTER the fire-time gate: a denied run dispatches nothing a tab
            # could serve. Visible consequence, accepted deliberately: a first
            # run that starts and then fails leaves an empty tab. Pre-creating
            # anyway is the choice, because gating the tab on the run reaching
            # injection would reopen the very hole this closes — identity must
            # exist DURING the run. Guarded wrapper, not a bare await: a tab we
            # FAIL to mint must not kill — or, via the run_never_started
            # lifecycle, CONSUME — the one-shot run it was meant to serve (see
            # _pre_create_cron_slot for the retention-marker contract).
            if self.dashboard_state:
                await _pre_create_cron_slot(self.dashboard_state, job)

            def _cron_extra_env() -> dict[str, str] | None:
                """job.env plus KIROCREW_APPROVAL_MODE when the job runs auto.

                SubagentManager.spawn's own auto-approve fallback for a cron's
                spawn_run subagents (parent_trusted, i.e.
                sessions.get_approval_policy(parent_session_key)=="auto")
                depends on parent_session resolving back to this cron's session
                key -- an identity-plumbing path that can fail silently and
                leave the spawn stuck on the interactive approval path a cron
                has no responder for. Injecting the mode directly as an env var
                the spawned kiro-cli process inherits (mirroring
                KIROCREW_SESSION_KEY/KIROCREW_CHANNEL_ID) lets the spawn_run MCP
                tool (mcp_core.py) read and forward its own approval_mode
                explicitly, independent of whether parent_session resolution
                succeeds.

                KIROCREW_APPROVAL_MODE is a RESERVED control var: it is stripped
                from the app/user-controlled ``job.env`` on every path and only
                re-injected here when the job's VALIDATED ``approval_mode`` is
                "auto". Otherwise an app manifest could set it directly in
                ``job.env`` and have an interactive cron's spawn_run subagents
                silently auto-approved -- an authorization bypass.

                The two governance policy-path vars are reserved for the same
                reason, and the strip set is ``_CRON_RESERVED_ENV_KEYS``.
                """
                env = cron_job_env_without_reserved(job.env)
                if job.approval_mode == "auto":
                    env["KIROCREW_APPROVAL_MODE"] = "auto"
                return env or None

            async def _acquire_with_model_fallback(
                key: str,
                agent_id: str | None,
                cwd: str | None = None,
                crew_agent: str | None = None,
                *,
                execution: ExecutionContext | None = None,
                replace_execution: bool = False,
            ) -> "tuple[LLMProvider, bool, bool, bool]":
                """get_or_create honoring job.model; if that model is
                unavailable, retry once with the registry default.
                Returns (client, is_new, resumed, downgraded).

                ``agent_id`` is the RESOLVED kiro agent mode (an alias must be
                collapsed via _resolve_cron_agent before this call), ``cwd``
                is that agent's workspace so the session runs in the right tree,
                and ``crew_agent`` is the original alias so prepare_runtime
                resolves the member identity (its capability gates, model /
                reasoning-effort pins, and watchdog windows). ``execution``
                overrides only the record published under the session key; its
                store and member identity must remain the captured run's.
                ``replace_execution`` admits a sequence step's template switch
                only while the durable record still matches the fresh read.
                """

                assert self.sessions is not None
                from kiro_crew.execution_context import (
                    bind_session_execution,
                    read_session_execution,
                )

                bound_execution = execution if execution is not None else cron_execution
                if replace_execution:
                    expected_execution = await asyncio.to_thread(read_session_execution, key)
                    await asyncio.to_thread(
                        bind_session_execution,
                        key,
                        bound_execution,
                        replace_existing=True,
                        expected=expected_execution,
                    )
                else:
                    await asyncio.to_thread(bind_session_execution, key, bound_execution)
                modes = getattr(self.ctx_builder, "_session_memory_modes", None)
                if isinstance(modes, dict):
                    # A separately scheduled run is durable work, not a child
                    # conversation. Only this trusted dispatch admits its key.
                    from kiro_crew.messaging.privacy_mode import strictest
                    from kiro_crew.subagent_persistence import bind_session_memory_mode
                    from kiro_crew.workflows.registry import _await_owned

                    publication = asyncio.create_task(
                        asyncio.to_thread(
                            bind_session_memory_mode, key, bound_execution.memory_mode
                        )
                    )
                    admitted_mode = await _await_owned(publication)
                    modes[key] = (
                        strictest((admitted_mode, modes.get(key, "persistent"))) or "persistent"
                    )

                async def _rebind_prepared_execution(client: LLMProvider) -> None:
                    if not replace_execution:
                        return
                    prepared_template = getattr(client, "loaded_capability_template", None)
                    if (
                        not isinstance(prepared_template, str)
                        or not prepared_template
                        or prepared_template == bound_execution.template_id
                    ):
                        return
                    rebound = bound_execution.with_template(prepared_template, crew_agent or "")
                    await asyncio.to_thread(
                        bind_session_execution,
                        key,
                        rebound,
                        replace_existing=True,
                        expected=bound_execution,
                    )

                # The dashboard's per-turn account gate, run before acquiring as
                # _run_chat runs it. A `cron:` key never claims a warm-pool
                # process, but a fire whose sub-agents are still running skips the
                # reset in its `finally`, and the next fire of a
                # `persistent_session` job, or of the same `agent_sequence` step,
                # reuses that live session. After an account switch or
                # `kiro-cli logout` outside the dashboard it still holds the
                # previous account, and a host where only crons run has no
                # dashboard turn to retire it. Passed as unattended: when the gate
                # cannot identify the signed-in account (a social login, an
                # unreadable store) the fire touches no session, since it could
                # not tell a stale one from a healthy one on any fire.
                await _retire_sessions_on_identity_change(self.dashboard_state, unattended=True)
                try:
                    client, is_new, resumed = await self.sessions.get_or_create(
                        key,
                        agent=agent_id,
                        crew_agent=crew_agent,
                        channel_id=job.channel,
                        approval_policy=job.approval_mode,
                        model=job.model or None,
                        extra_env=_cron_extra_env(),
                        cwd=cwd,
                    )
                    await _rebind_prepared_execution(client)
                    # A config-option backend refuses a pin without raising and
                    # stays on its default: the same downgrade as the except
                    # below, so report it the same way.
                    return (
                        client,
                        is_new,
                        resumed,
                        bool(job.model) and provider_model_pin_refused(client),
                    )
                except Exception as model_exc:
                    if not job.model:
                        raise
                    # Only fall back when the failure plausibly implicates the
                    # pinned model; unrelated session-creation errors (provider
                    # spawn, missing factory, transient I/O) must propagate so
                    # they are not misreported as a model downgrade.
                    _err = str(model_exc).lower()
                    if "model" not in _err and job.model.lower() not in _err:
                        raise
                    logger.warning(
                        "Cron '%s': model %r unavailable (%s); retrying with default",
                        job.name,
                        job.model,
                        model_exc,
                    )
                    client, is_new, resumed = await self.sessions.get_or_create(
                        key,
                        agent=agent_id,
                        crew_agent=crew_agent,
                        channel_id=job.channel,
                        approval_policy=job.approval_mode,
                        extra_env=_cron_extra_env(),
                        cwd=cwd,
                    )
                    await _rebind_prepared_execution(client)
                    return client, is_new, resumed, True

            def _retained_session_agent_mismatch(key: str, agent: str | None) -> str | None:
                """Return the retained agent only on positive mismatch evidence."""
                live_agent = None
                try:
                    reader = getattr(self.sessions, "_get_session_agent", None)
                    if callable(reader):
                        live_agent = reader(key)
                except Exception:
                    logger.debug(
                        "cron '%s': live session agent unreadable", job.name, exc_info=True
                    )
                    live_agent = None
                if isinstance(live_agent, str) and live_agent and live_agent != (agent or ""):
                    return live_agent
                return None

            def _annotate_model_downgrade(text: str) -> str:
                # job.model is LLM-controllable via MCP; redact before it
                # reaches Slack/dashboard through last_result.
                safe_model = redact_credentials(redact_exfiltration_urls(job.model)[0])[0]
                return f"⚠️ Model '{safe_model}' unavailable; ran with default.\n\n" + text

            # ── Sequential agent execution ──
            # When agent_sequence has multiple agents, run them sequentially
            # with per-agent session keys and per-job env vars.
            agents = cron_agents
            if agent_sequence_dispatches(agents):
                assert self.sessions is not None
                assert self.ctx_builder is not None

                @dataclass(frozen=True)
                class _SequenceStep:
                    alias: str
                    kiro_agent: str | None
                    cwd: str | None
                    crew: str | None
                    dispatch_agent: str
                    session_key: str

                # Resolve once per fire so the pre-loop comparison and dispatch
                # use one runtime identity. Keep a list parallel to ``agents``:
                # duplicate aliases are separate steps sharing one stable key.
                steps: list[_SequenceStep] = []
                for agent in agents:
                    _seq_kagent, _seq_cwd, _seq_crew = _resolve_cron_agent(
                        agent, sequence_step=True
                    )
                    steps.append(
                        _SequenceStep(
                            alias=agent,
                            kiro_agent=_seq_kagent,
                            cwd=_seq_cwd,
                            crew=_seq_crew,
                            dispatch_agent=_seq_kagent or agent,
                            session_key=f"cron:{job.id}:{agent}",
                        )
                    )

                # Sweep every stable per-step key before dispatching anything.
                # Deferring mid-loop would replay completed steps on the next
                # fire, so one mismatch defers the whole sequence instead.
                for step in steps:
                    if not step.alias:
                        logger.error(
                            "Cron '%s': empty agent_sequence step cannot resolve a crew; "
                            "refusing to dispatch",
                            job.name,
                        )
                        _defer_cron_before_dispatch(
                            job,
                            f"cron '{job.name}': empty agent_sequence step cannot resolve a crew; "
                            "refusing to dispatch",
                        )
                        # The job's own configuration causes this refusal, so a
                        # retry meets it again: a plain one-shot is parked
                        # (``run_parks_one_shot``) instead of retried every floor.
                        job.refusal_cannot_clear = True
                        return None
                    _live_agent = _retained_session_agent_mismatch(
                        step.session_key, step.dispatch_agent
                    )
                    if _live_agent is not None:
                        logger.info(
                            "Cron '%s': sequence step %r session retained under agent %r "
                            "but this fire resolves it to %r; deferring the whole sequence "
                            "rather than reusing its runtime",
                            job.name,
                            step.alias,
                            _live_agent,
                            step.dispatch_agent,
                        )
                        _defer_cron_before_dispatch(
                            job,
                            f"sequence step {step.alias!r} session retained under agent "
                            f"{_live_agent!r} with work pending; this fire resolves the "
                            f"step to {step.dispatch_agent!r} and will not run a partial "
                            "sequence",
                        )
                        return None

                result_text = "_No response._"
                _seq_downgraded = False
                # Run-scoped: a sequence where one agent got a tool through has
                # done work, even if a later agent was blocked outright.
                _gate = _GateTally()
                for step in steps:
                    # One re-load per step when a live backend lost this step's
                    # session: the step is re-run on a fresh runtime that
                    # session/loads the same id. A second loss ends the fire.
                    _seq_snf_retry_used = False
                    while True:
                        _seq_snf_retry = False
                        _seq_snf_err = ""
                        agent = step.alias
                        agent_session_key = step.session_key
                        if self.cron_svc is not None:
                            self.cron_svc.register_active_session_key(job.id, agent_session_key)
                        _acq = False
                        # Post-compaction re-injection bookkeeping for the finally:
                        # consumed the one-shot flag / turn landed. The stream
                        # returns text only, so the completion's stop reason is
                        # captured through on_complete (last completion wins: a
                        # post-token resume's continuation is the turn that ended).
                        _seq_reinjection = False
                        _seq_landed = False
                        # None until a completion is observed: a stream that ends
                        # without one is not landed.
                        _seq_stop: dict[str, str | None] = {"reason": None}
                        # Whether the backend reported a completed compaction during
                        # the step's stream, which drops the session-start context.
                        _seq_compaction: dict[str, bool] = {"completed": False}

                        def _seq_note_complete(
                            ev: Any, _box: dict[str, str | None] = _seq_stop
                        ) -> None:
                            _box["reason"] = str(getattr(ev, "stop_reason", "") or "")

                        def _seq_note_compaction(
                            ev: Any, _box: dict[str, bool] = _seq_compaction
                        ) -> None:
                            if getattr(ev, "text", "") == "completed":
                                _box["completed"] = True

                        try:
                            _seq_execution = cron_execution.with_template(
                                step.dispatch_agent, step.crew or step.alias
                            )
                            client, is_new, _resumed, _downgraded = (
                                await _acquire_with_model_fallback(
                                    step.session_key,
                                    step.dispatch_agent,
                                    step.cwd,
                                    step.crew,
                                    execution=_seq_execution,
                                    replace_execution=True,
                                )
                            )
                            _seq_downgraded = _seq_downgraded or _downgraded
                            _acq = True
                            # Publish this turn's session identity so managed MCP
                            # tools resolve their parent session. The cron path was
                            # the ONE turn-running surface that skipped this (every
                            # other surface publishes — see messaging.identity), and
                            # under session sharing the runtime env carries no
                            # KIROCREW_SESSION_KEY and macOS sets no
                            # KIROCREW_HOST_PID, so the ancestor PID-walk over the
                            # per-turn pidfile mapping is the only identity source
                            # left. Without the publish, spawn_run resolved an
                            # empty parent ("notification only (parent=)") unless an
                            # unrelated surface happened to be mid-turn.
                            await publish_turn_identity(self.sessions, agent_session_key)
                            # A compaction drops session-start context. Read-and-clear
                            # the one-shot flag so this turn re-injects it exactly
                            # once; the finally re-arms it if the turn never lands.
                            _seq_reinjection = consume_reinjection(self.sessions, agent_session_key)
                            # Off-loop: build_message embeds the episodic query.
                            full_message, _ = await run_in_embed_pool(
                                self.ctx_builder.build_message,
                                msg,
                                is_new,
                                agent_session_key,
                                interactive=False,
                                agent=agent,
                                memory_store=cron_memory_store or None,
                                execution_context=cron_execution,
                                context_provider=client,
                                resumed=_resumed,
                                needs_reinjection=_seq_reinjection,
                                minimal_context=job.minimal_context,
                            )
                            # Wall clock for the cron agent turn: acp never assigns
                            # TurnUsage.duration_ms, so the row falls back to this.
                            # Brackets only the model turn — session acquisition and
                            # the episodic-query embed above are setup, not the turn.
                            _turn_t0 = time.monotonic()
                            _prompt_dispatched = True
                            result_text, _carried_credits = (
                                await _cron_stream_with_posttoken_resume(
                                    client,
                                    full_message,
                                    job_name=job.name,
                                    approval_policy=(
                                        ToolApprovalPolicy.AUTO_APPROVE
                                        if job.approval_mode == "auto"
                                        else ToolApprovalPolicy.HOOK_BASED
                                    ),
                                    hooks=self.ctx_builder.hooks,
                                    on_tool_approval=(
                                        None
                                        if job.approval_mode == "auto"
                                        else self._interactive_approval("cron")
                                    ),
                                    on_tool_gate=_gate.note,
                                    on_complete=_seq_note_complete,
                                    on_compaction=_seq_note_compaction,
                                    fallback_models=configured_fallback_chain(),
                                )
                            )
                            # The prompt reached the model and the turn completed, so
                            # the finally must NOT restore the re-injection flag --
                            # only for a succeeded stop reason.
                            _seq_landed = stop_reason_landed(_seq_stop["reason"])
                            if not result_text:
                                result_text = _gate.empty_reply_placeholder()
                            result_text = _annotate_model_fallback(result_text, client)
                            logger.info("Cron '%s': agent '%s' completed", job.name, agent)

                            # ── Per-turn usage row: background spend. ──
                            try:

                                _used, _window = read_context_tokens(client)
                                _turn_usage = provider_last_turn_usage(client)
                                if _carried_credits:
                                    # A resumed turn's post-turn read sees only the
                                    # continuation prompt; bill the interrupted
                                    # prompt's snapshotted credits too.
                                    _turn_usage.credits += _carried_credits
                                await persist_token_record_async(
                                    agent_session_key,
                                    # Blank on a downgrade: the configured model was
                                    # unavailable and the default ran instead, so the
                                    # requested id would attribute spend to a model
                                    # that never executed. Blank defers to
                                    # model_source, which reports what actually ran.
                                    # A half-applied pair pin bills the bare
                                    # model that ran, not the suffixed pin.
                                    (
                                        ""
                                        if (_seq_downgraded or provider_fallback_active(client))
                                        else (
                                            (job.model and provider_model_pin_partial(client))
                                            or job.model
                                            or ""
                                        )
                                    ),
                                    _turn_usage,
                                    provider=(
                                        self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"
                                    ),
                                    surface="cron",
                                    agent=read_effective_agent(client) or agent or "",
                                    context_used=_used,
                                    context_window=_window,
                                    elapsed_ms=int((time.monotonic() - _turn_t0) * 1000),
                                    model_source=client,
                                )
                            except Exception:
                                logger.debug("usage row (cron seq) persist failed", exc_info=True)
                        except Exception as _seq_exc:
                            if not acp_error_is_session_not_found(_seq_exc):
                                raise
                            # Re-running the step re-sends its prompt, which
                            # could repeat a tool the lost turn already ran.
                            if acp_error_after_tool_activity(_seq_exc):
                                raise AcpError(
                                    f"{SESSION_NOT_FOUND_NOT_REPLAYED_TEXT} {_seq_exc}"
                                ) from _seq_exc
                            if _seq_snf_retry_used:
                                raise AcpError(
                                    f"{SESSION_NOT_FOUND_GIVE_UP_TEXT} {_seq_exc}"
                                ) from _seq_exc
                            _seq_snf_retry_used = True
                            _seq_snf_retry = True
                            _seq_snf_err = str(_seq_exc)
                            logger.warning(
                                "Cron '%s': backend lost the session of step %r, "
                                "re-loading it on a fresh runtime and retrying the step",
                                job.name,
                                agent,
                            )
                        finally:
                            # Before the reset below: a turn that consumed the
                            # post-compaction flag but never landed, or whose backend
                            # compacted the session, sets the flag so a session that
                            # survives (deferred reset) re-injects.
                            rearm_reinjection(
                                self.sessions,
                                agent_session_key,
                                consumed=_seq_reinjection,
                                landed=_seq_landed,
                                compacted=_seq_compaction["completed"],
                            )
                            rollback_skill_bodies(
                                self.ctx_builder, agent_session_key, landed=_seq_landed
                            )
                            if _acq:
                                self.sessions.release(agent_session_key)
                                # Mirror the single-agent finally below: defer the
                                # reset when this agent's sub-agents are still
                                # running, QUEUED behind the concurrency/stagger
                                # gate, or mid-injection — _subagent_done resets
                                # after the last one. Now that this path publishes
                                # turn identity, a non-final agent's spawn_run
                                # resolves a REAL parent key, so an unconditional
                                # reset here would tear down the session a pending
                                # completion is about to inject into (cold-starting
                                # a context-free replacement) and the completion's
                                # own cleanup would clear the reaper registration
                                # for the NEXT agent's still-in-flight turn.
                                _has_pending = bool(
                                    self.subagent_mgr
                                    and await _subagent_work_pending(
                                        self.subagent_mgr, agent_session_key
                                    )
                                )
                                _has_injecting = self._cron_injecting.get(agent_session_key, 0) > 0
                                if _has_pending or _has_injecting:
                                    logger.info(
                                        "Cron '%s': deferring reset of %s, subagents pending",
                                        job.name,
                                        agent_session_key,
                                    )
                                else:
                                    await self.sessions.reset(agent_session_key)
                                    if self.cron_svc is not None:
                                        self.cron_svc.clear_active_session_key(
                                            job.id, agent_session_key
                                        )
                        if not _seq_snf_retry:
                            break
                        # The finally above defers its reset while sub-agents
                        # are pending so a completion about to inject is not
                        # torn down; honour that same deferral here. Spawning a
                        # sub-agent is tool activity, so a loss with THIS turn's
                        # sub-agents in flight already took the no-replay arm and
                        # never reaches here; this guards a sibling step's
                        # completion still injecting. When we must defer, let
                        # _subagent_done do the reset and give up the retry (a
                        # re-run against the un-reset stale binding would only
                        # hit Session not found again), rather than reset under
                        # the pending completion.
                        _retry_has_pending = bool(
                            self.subagent_mgr
                            and await _subagent_work_pending(self.subagent_mgr, agent_session_key)
                        )
                        _retry_has_injecting = self._cron_injecting.get(agent_session_key, 0) > 0
                        if _retry_has_pending or _retry_has_injecting:
                            logger.info(
                                "Cron '%s': lost session of step %r but sub-agents "
                                "pending; deferring reset and not retrying this step",
                                job.name,
                                agent,
                            )
                            raise AcpError(f"{SESSION_NOT_FOUND_GIVE_UP_TEXT} {_seq_snf_err}")
                        await self.sessions.reset(agent_session_key)
                if _seq_downgraded:
                    result_text = _annotate_model_downgrade(result_text)
                result_text = _annotate_partial_block(result_text, _gate)
                job.set_run_result(result_text)
                # This path owns the same verdict as the single-agent one, so a
                # multi-agent job's failure counter moves in both directions —
                # which is what keeps auto-pause both reachable and clearable.
                _apply_gate_verdict(job, _gate)
                return result_text

            # ── Single-agent path (existing behavior) ──
            # Tell the reaper which key to target if this run hangs.
            if self.cron_svc is not None:
                self.cron_svc.register_active_session_key(job.id, session_key)

            _acquired = False
            _model_downgraded = False
            # Set when the gate verdict below already counted this run, so the
            # exception handler does not count it a second time.
            _gate_counted = False
            # The provider THIS run acquired, held for the failure handler's
            # attribution question. Captured here rather than looked up when the
            # failure is handled, because by then the runtime is gone: the
            # ACP-death arm resets the session before it retries, and the retry
            # frame's own finally resets again on unwind -- so a lookup by key
            # answers None, or worse the REPLACEMENT provider, and either one
            # reads as "this job's own fault".
            _run_provider: object | None = None
            # Post-compaction re-injection bookkeeping for the finally: consumed
            # the one-shot flag / turn landed. Stop reason captured through
            # on_complete, as on the sequential path above.
            _needs_reinjection = False
            _turn_landed = False
            _turn_stop: dict[str, str | None] = {"reason": None}
            # Whether the backend reported a completed compaction during the
            # turn's stream, which drops the session-start context.
            _turn_compaction: dict[str, bool] = {"completed": False}

            def _note_complete(ev: Any, _box: dict[str, str | None] = _turn_stop) -> None:
                _box["reason"] = str(getattr(ev, "stop_reason", "") or "")

            def _note_compaction(ev: Any, _box: dict[str, bool] = _turn_compaction) -> None:
                if getattr(ev, "text", "") == "completed":
                    _box["completed"] = True

            try:
                assert self.sessions is not None
                assert self.ctx_builder is not None
                # Collapse an alias (channel-bound agent) to its real kiro mode
                # + workspace before dispatch; falls back to the raw value when
                # it is already a real mode or unset.
                _single_kagent, _single_cwd, _single_crew = _resolve_cron_agent(
                    cron_agent or None, template_namespace=_default_substituted
                )
                _dispatch_agent = _single_kagent or cron_agent or None
                # ── Retained-session agent guard (every single-agent job) ──
                # The cron key is the stable f"cron:{job.id}", and the run's
                # `finally` DEFERS the reset while sub-agents are pending or an
                # injection is in flight, so a session can outlive its fire. The
                # claim path then returns that live `session.provider` by key
                # WITHOUT comparing the `agent` kwarg (session_allocation.py's
                # `existing is not None and not recycling` arm), so a default
                # narrowed between fires would build the NEW agent's context and
                # run it on the OLD agent's process -- keeping MCP servers, a
                # workspace and a pinned model the operator has just taken away.
                #
                # Two things let consecutive fires of one job dispatch different
                # names. An agent-less job re-reads `agent.default_agent` on
                # every fire, and the guard is NOT keyed on
                # `_default_substituted`, which fails open in exactly the case
                # this guard exists for: fire N
                # substitutes and defers its reset, the operator then CLEARS
                # `agent.default_agent`, and fire N+1 resolves an empty default,
                # so that flag is False, the guard is skipped, and the fire runs
                # the FLOOR's context on the process still holding the previous
                # default's runtime. Every other reason for declining the
                # substitution below reaches the same hole. A job pinning its
                # own agent_id reaches it too: `cron update` re-points the
                # captured template (`recapture_cron_template`), so two fires of
                # one pinned job can dispatch different names. The guard
                # therefore runs for every single-agent dispatch; a job whose
                # agent did not change compares equal and fires as before. A
                # dispatching sequence is handled separately by the
                # pre-loop sweep above, because each stable per-agent key must be
                # compared with that step's freshly resolved dispatch agent.
                #
                # DEFER, never reset: a retained session implies pending
                # sub-agent work -- the no-pending case already reset in the
                # prior fire's `finally` -- so resetting to get a correct agent
                # would destroy work in flight, which is strictly worse than
                # skipping one fire. The retention is temporary by construction
                # (`_subagent_done` resets after the last one lands) and the
                # reaper still targets this key if that reset hangs, so the
                # deferral cannot become permanent. `_defer_cron_before_dispatch`
                # is the established spelling: the run counts as neither success
                # nor failure, so a skipped fire cannot walk a job toward
                # auto-pause, and `run_never_started` keeps a delete_after_run
                # job from being consumed by a fire that ran no line.
                #
                # The active-session-key registration above is deliberately
                # LEFT in place: a session really is still live under this key,
                # which is the same reason the deferred-reset arm leaves it.
                #
                # POSITIVE EVIDENCE ONLY. A non-empty `str` from the reader that
                # differs from the name about to be dispatched is a mismatch; a
                # manager without the reader, an unreadable answer, a non-`str`,
                # or "" (no session under the key) all leave this dispatch
                # exactly as it was. Fail-closed here means refusing the unsafe
                # REUSE, not treating an unanswerable question as a mismatch --
                # deferring on a silent reader would stall every agent-less cron
                # on such a host forever, which is a much larger failure than
                # the widening being closed. `session.agent` is stored as the
                # `agent` kwarg verbatim (`agent=agent or ""`), so the two sides
                # of this comparison are the same spelling by construction.
                _live_agent = _retained_session_agent_mismatch(session_key, _dispatch_agent)
                if _live_agent is not None:
                    logger.info(
                        "Cron '%s': session retained under agent %r but this fire "
                        "dispatches %r; deferring rather than reusing its runtime",
                        job.name,
                        _live_agent,
                        _dispatch_agent or "",
                    )
                    _defer_cron_before_dispatch(
                        job,
                        f"session retained under agent {_live_agent!r} with work "
                        f"pending; this fire dispatches "
                        f"{_dispatch_agent or ''!r} and will not reuse it",
                    )
                    return None
                # After an agent change the session record a previous run left
                # under the stable key still names the template captured before,
                # and the plain bind in `_acquire_with_model_fallback` refuses
                # it. Bring the job's own record to the current capture first,
                # template only; any other difference is left for that bind to
                # refuse. Done here, at the fire, so a record published late (a
                # run still in flight during the update) or one written while the
                # job was stateless is caught on the next fire, and a failed
                # write is retried by it.
                if job.execution_context is not None:
                    from kiro_crew.cron_service.identity import rebind_cron_session_template

                    await asyncio.to_thread(
                        rebind_cron_session_template, session_key, cron_execution, job.name
                    )
                client, is_new, _resumed, _model_downgraded = await _acquire_with_model_fallback(
                    session_key, _dispatch_agent, _single_cwd, _single_crew
                )
                _acquired = True
                _run_provider = client
                # Same identity publish as the sequential site above — the
                # single-agent cron turn must publish its pidfile mapping or
                # spawn_run's parent resolution has no source to walk to.
                await publish_turn_identity(self.sessions, session_key)
                if job.acked_items:
                    msg += (
                        "\n\n[User has seen and acknowledged ALL of the following — "
                        "do NOT repeat the same content]\n"
                        + "\n".join(f"- {a}" for a in job.acked_items)
                    )
                _provider = self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"
                # A compaction drops session-start context. Read-and-clear the
                # one-shot flag so this turn re-injects it exactly once; the
                # finally re-arms it if the turn never lands.
                _needs_reinjection = consume_reinjection(self.sessions, session_key)
                # Off-loop: build_message embeds the episodic query.
                full_message, _ = await run_in_embed_pool(
                    self.ctx_builder.build_message,
                    msg,
                    is_new,
                    session_key,
                    interactive=False,
                    agent=cron_agent or None,
                    memory_store=cron_memory_store or None,
                    execution_context=cron_execution,
                    context_provider=client,
                    resumed=_resumed,
                    needs_reinjection=_needs_reinjection,
                    provider_type=_provider,
                    minimal_context=job.minimal_context,
                )

                # Wall clock for the cron agent turn — see the sequential site
                # above. acp reports no duration, so this is the row's fallback.
                _turn_t0 = time.monotonic()
                _gate = _GateTally()
                _prompt_dispatched = True
                result_text, _carried_credits = await _cron_stream_with_posttoken_resume(
                    client,
                    full_message,
                    job_name=job.name,
                    approval_policy=(
                        ToolApprovalPolicy.AUTO_APPROVE
                        if job.approval_mode == "auto"
                        else ToolApprovalPolicy.HOOK_BASED
                    ),
                    hooks=self.ctx_builder.hooks,
                    on_tool_approval=(
                        None if job.approval_mode == "auto" else self._interactive_approval("cron")
                    ),
                    on_tool_gate=_gate.note,
                    on_complete=_note_complete,
                    on_compaction=_note_compaction,
                    fallback_models=configured_fallback_chain(),
                )

                # The prompt reached the model and the turn completed, so the
                # finally must NOT restore the re-injection flag -- only for a
                # succeeded stop reason.
                _turn_landed = stop_reason_landed(_turn_stop["reason"])

                # Recorded, not inferred from the text: the dedup hash below
                # swaps the placeholder for its count-free twin only when the
                # placeholder was substituted here, never over model prose.
                _empty_reply = not result_text
                if _empty_reply:
                    result_text = _gate.empty_reply_placeholder()

                if _model_downgraded:
                    result_text = _annotate_model_downgrade(result_text)
                result_text = _annotate_model_fallback(result_text, client)
                # Before set_run_result and the dedup hash below, so the stored
                # result, the slot, the bell and every transport carry the
                # refusal, and a run that lost a call never hashes equal to
                # one that did not.
                result_text = _annotate_partial_block(result_text, _gate)

                job.set_run_result(result_text)

                # Context-meter reading for the dashboard slot, captured NOW:
                # the finally block below resets this session, so the open
                # path can never read the provider live. Routed through
                # broadcast_context_usage by inject_cron_result_to_dashboard.
                _ctx_reading = context_meter_reading(client)

                # ── Per-turn usage row: attribute background spend. ──
                # Best-effort; must never fail the cron turn.
                # The same usage feeds the result row's footer (meta.turn_stats).
                _turn_stats: dict[str, Any] | None = None
                try:

                    _used, _window = read_context_tokens(client)
                    _turn_usage = provider_last_turn_usage(client)
                    if _carried_credits:
                        # See the sequential site above: bill the interrupted
                        # prompt's snapshotted credits alongside the
                        # continuation's on a resumed turn.
                        _turn_usage.credits += _carried_credits
                    _turn_stats = turn_stats_meta(
                        int(_turn_usage.duration_ms or (time.monotonic() - _turn_t0) * 1000),
                        float(_turn_usage.credits or 0.0),
                        float(_turn_usage.cost_usd or 0.0),
                        read_turn_model(client),
                    )
                    await persist_token_record_async(
                        session_key,
                        # Blank on a downgrade or an active fallback — see the
                        # sequential site above / provider_fallback_active. A
                        # half-applied pair pin bills the bare model that ran.
                        (
                            ""
                            if (_model_downgraded or provider_fallback_active(client))
                            else (
                                (job.model and provider_model_pin_partial(client))
                                or job.model
                                or ""
                            )
                        ),
                        _turn_usage,
                        provider=_provider,
                        surface="cron",
                        agent=read_effective_agent(client) or cron_agent or "",
                        context_used=_used,
                        context_window=_window,
                        elapsed_ms=int((time.monotonic() - _turn_t0) * 1000),
                        model_source=client,
                    )
                except Exception:
                    logger.debug("usage row (cron) persist failed", exc_info=True)

                # ── Error deduplication ──
                # Suppress repeated identical results to avoid spam. This is
                # delivery-agnostic in both directions: the anchor it reads is
                # advanced by _record_cron_delivery on EVERY confirmed surface,
                # so the early return below suppresses the channel post too,
                # not only Slack's. Deliberate for a channel-delivered cron:
                # identical output is equally noisy in a Telegram chat, and the
                # 24h reminder plus the "same result N times in a row" caption
                # are what keep a persistently-identical job from going
                # unnoticed. An empty reply is hashed over its count-free
                # placeholder: the approved-call count the user sees is per-run,
                # and a key that moves with it never suppresses.
                rh = _result_hash(_dedup_text(result_text, _gate, empty_reply=_empty_reply))

                _gate_counted = _apply_gate_verdict(job, _gate)

                if rh == job.last_posted_hash:
                    job.consecutive_dupes += 1
                    # Time-based reminder: re-post after 24h so persistent identical
                    # results don't go unnoticed indefinitely.
                    if time.time() - job.last_posted_at >= _SUCCESS_REMINDER_SECS:
                        # NB: consecutive_dupes is captured here before the reset
                        # at the post-delivery state update further below.
                        result_text = (
                            f"⚠️ Cron '{job.name}' has produced the same result"
                            f" {job.consecutive_dupes} times in a row:\n\n{result_text}"
                        )
                    else:
                        logger.info(
                            "Cron '%s': duplicate result #%d — suppressing delivery",
                            job.name,
                            job.consecutive_dupes,
                        )
                        if self.dashboard_state:
                            redacted_for_dash, _ = redact_exfiltration_urls(result_text)
                            redacted_for_dash, _ = redact_credentials(redacted_for_dash)
                            title = f"🔇 Cron: {job.name} (dup #{job.consecutive_dupes})"
                            title, _ = redact_exfiltration_urls(title)
                            title, _ = redact_credentials(title)
                            self.dashboard_state.notify(
                                "cron",
                                title,
                                redacted_for_dash,
                                meta={"job_id": job.id},
                            )

                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_dedup_suppress",
                            outcome="suppressed",
                            downstream_service="none",
                        )
                        # Still inject into dashboard slot even when Slack is suppressed
                        if (
                            self.dashboard_state
                            and job.persistent_session
                            and not job.hide_in_chat
                            and self.dashboard_state.has_slot(f"cron-{job.id}")
                        ):
                            inject_cron_result_to_dashboard(
                                self.dashboard_state,
                                job,
                                result_text,
                                history=await prefetch_cron_history(self.dashboard_state, job.id),
                                context_reading=_ctx_reading,
                                turn_stats=_turn_stats,
                            )
                        return result_text

                if job.silent:
                    logger.info("Cron job '%s' silent — suppressing auto-delivery", job.name)

                    sel().log_tool_invocation(
                        session_key=f"cron:{job.id}",
                        tool_name="cron_silent_suppress",
                        outcome="suppressed",
                        downstream_service="none",
                    )
                    # Still inject into dashboard slot even when silent
                    if (
                        self.dashboard_state
                        and job.persistent_session
                        and not job.hide_in_chat
                        and self.dashboard_state.has_slot(f"cron-{job.id}")
                    ):
                        inject_cron_result_to_dashboard(
                            self.dashboard_state,
                            job,
                            result_text,
                            history=await prefetch_cron_history(self.dashboard_state, job.id),
                            context_reading=_ctx_reading,
                            turn_stats=_turn_stats,
                        )
                    return result_text

                if self.dashboard_state:
                    # Inject into slot BEFORE notification so has_slot() is true for notify_meta.
                    # hide_in_chat=True keeps the cron out of the active session list — the
                    # result still reaches Slack/bell below, and the run stays visible in the
                    # History tab via the cron execution-history store (CronHistoryStore, written
                    # unconditionally by the executor and surfaced at GET /api/crons/{id}/history).
                    # NOTE: the cron:{id} dashboard conversation_log is written ONLY by
                    # inject_cron_result_to_dashboard (gated off here for hidden crons), so it is
                    # intentionally empty for a hidden cron — it exists solely to feed a dashboard
                    # follow-up turn, which a no-slot cron never has. Do NOT rely on cron:{id} for
                    # hidden-cron result persistence; get_history() is the source of truth.
                    # This is the only slot *creator* site (get_or_create_slot); the dedup/silent
                    # paths above only re-inject into an already-existing slot via has_slot(), so
                    # they self-no-op when hide_in_chat is True.
                    if job.persistent_session and not job.hide_in_chat:
                        history = (
                            await asyncio.to_thread(
                                self.dashboard_state.conversation_log.read_messages,
                                f"cron:{job.id}",
                            )
                            if self.dashboard_state.conversation_log
                            else []
                        )
                        inject_cron_result_to_dashboard(
                            self.dashboard_state,
                            job,
                            result_text,
                            history=history,
                            context_reading=_ctx_reading,
                            turn_stats=_turn_stats,
                        )
                    redacted_for_dash, _ = redact_exfiltration_urls(result_text)
                    redacted_for_dash, _ = redact_credentials(redacted_for_dash)
                    safe_name, _ = redact_exfiltration_urls(job.name)
                    safe_name, _ = redact_credentials(safe_name)
                    notify_meta: dict[str, str] = {"job_id": job.id}
                    # Gate the slot linkage on not hide_in_chat for parity with the
                    # three inject sites above. Without this, a job flipped to
                    # hide_in_chat=True that still owns an older cron-{id} slot would
                    # keep emitting meta.slot (has_slot stays True) → the notification
                    # CTA shows "Continue session" pointing at a slot no longer
                    # receiving results. Gating here forces the no-slot "View last
                    # result" CTA, which lazily rebuilds from CronHistoryStore.
                    if (
                        job.persistent_session
                        and not job.hide_in_chat
                        and self.dashboard_state.has_slot(f"cron-{job.id}")
                    ):
                        notify_meta["slot"] = f"cron-{job.id}"
                    self.dashboard_state.notify(
                        "cron",
                        f"Cron: {safe_name}",
                        redacted_for_dash,
                        meta=notify_meta,
                    )
                # A job belongs to ONE surface: the conversation that scheduled
                # it. Delivering to both would notify an operator twice for one
                # run, which is how notifications become noise people stop
                # reading. So the channel leg is attempted FIRST and Slack stands
                # down only on a CONFIRMED delivery: a predicate saying a channel
                # *would* take it is not the same claim, and standing Slack down
                # on that loses the result outright when the channel send is
                # refused by governance or fails on the wire. An explicit
                # `job.channel` is a destination the user pinned, so it wins over
                # both. Every job that exists today has no channel origin, so
                # this is inert for current installs.
                channel_delivered = False
                if not job.channel:
                    try:
                        channel_delivered = await self._deliver_cron_to_channel(
                            job.session_key,
                            f"⏰ Cron: {job.name}\n\n{result_text}",
                            actor_key=session_key,
                        )
                    except Exception:
                        # The job SUCCEEDED. Letting a delivery error reach the
                        # outer handler would record a failure and march the job
                        # toward auto-pause on a messaging fault. Slack still
                        # runs below, because nothing was delivered here.
                        logger.error(
                            "Cron job '%s': channel delivery failed (job succeeded)",
                            job.name,
                            exc_info=True,
                        )
                if channel_delivered:
                    # Same dedup contract as the Slack leg: the hash advances
                    # once the result reached someone. Without this a Slack-less
                    # install never advances it, so the suppression branch can
                    # never fire and an unchanged result is re-delivered forever.
                    self._record_cron_delivery(job, rh)
                    # No reply-anchor write to mirror the Slack branch's
                    # set_thread/set_channel below, deliberately. Those two record
                    # where a Slack cron post LANDED so a later subagent completion
                    # under ``cron:{id}`` can be threaded onto it; the channel leg
                    # learns nothing equivalent at send time -- its conversation was
                    # already resolved FROM the creating session's own durable
                    # origin/mirror link, which every later delivery re-reads.
                    # Routing a cron's subagent completions back to the creating
                    # channel needs a ``cron:{id}`` -> creating-key edge instead,
                    # which is its own change; half of it here would look like
                    # parity without being it.
                if self.slack and not channel_delivered:
                    try:
                        # Retry only open_dm (transient Slack API errors).
                        # Delivery (post_blocks/post_message) is NOT retried to avoid duplicates.
                        channel = job.channel
                        if not channel and (job.created_by or self._owner_id):
                            channel = await self._open_dm_with_retry(
                                job.created_by or self._owner_id, job.name
                            )
                        if channel:
                            # ── Extract embedded local images and upload them
                            # natively (mirror the chat renderer's on_done seal).
                            # The cron delivery path posts text only; without
                            # this an inline ![alt](/abs/path.png) in the reply
                            # ships as literal Markdown and Slack shows nothing.
                            # within_root is the cron session's resolved cwd
                            # (client.cwd — the agent's workspace), which bounds
                            # extraction to files the session may read.
                            # extract_outbound_with_audit is the SAME admission
                            # seal the renderer uses: it writes the mandatory SEL
                            # record for both admitted and refused files and folds
                            # any refusal note into the text, so a cron's file
                            # egress leaves the same audit trail as a chat reply.
                            #
                            # ``_cron_body`` is a SEPARATE Slack-only local: the
                            # extraction strips the local path and may fold in a
                            # refusal note, neither of which belongs in the value
                            # the callback RETURNS. That return value is the job's
                            # stored result and feeds the next run's "[Previous
                            # run result]" prompt, so ``result_text`` is left
                            # untouched and only the delivered body is rewritten.
                            _cron_upload_files: list = []
                            _cron_root = getattr(client, "cwd", "") or ""
                            _cron_body = result_text
                            if _cron_root:
                                from kiro_crew.slack.files import (
                                    extract_outbound_with_audit,
                                )

                                _cron_body, _cron_files, _ = await extract_outbound_with_audit(
                                    result_text,
                                    within_root=_cron_root,
                                    audit_caller=session_key or channel or "cron",
                                )
                                _cron_upload_files = list(_cron_files)
                            # The caption is redacted-but-not-converted by
                            # render_for_slack's header= seam, which also charges
                            # it against the limit. Doing it there rather than
                            # here is the point: a cron name is LLM-authored (the
                            # agent can create crons via cron_add), and the
                            # hand-rolled version of this had already forgotten to
                            # redact it once.
                            parts = render_for_slack(
                                _cron_body,
                                limit=_CRON_MSG_LIMIT,
                                header=f"⏰ *Cron: {job.name}*\n\n",
                            )
                            # First part as Block Kit message with ack button
                            blocks: list[dict] = [
                                {
                                    "type": "section",
                                    "text": {"type": "mrkdwn", "text": parts[0]},
                                },
                            ] + build_cron_ack_block(job.id)
                            parent_ts = await self.slack.post_blocks(
                                channel, blocks, parts[0], job.thread_ts
                            )
                            thread_root = job.thread_ts or parent_ts
                            # Store the thread so subagents can route replies here
                            # -- but CLAIM the thread for inbound routing only when
                            # the cron POSTED it itself (no ``job.thread_ts``, so
                            # ``thread_root`` is this run's own ``parent_ts``).
                            #
                            # ``set_thread`` -> ``set_slack_link("cron:<id>", ts)``
                            # is a NON-self-derived claim (``cron:`` is not in
                            # ``CHANNEL_SESSION_NAMESPACES``), so it EVICTS whatever
                            # else owns ``ts`` in ``_thread_to_session``. For an
                            # inherited/explicit ``job.thread_ts`` -- a human's own
                            # Slack thread, keyed ``slack:<ts>`` -- that eviction is
                            # permanent: the self-link that would restore the owner
                            # (``transport_dispatch.py``) only fires on an UNCLAIMED
                            # thread, so the human's next message in their own thread
                            # would run in ``cron:<id>`` forever. A cron must never
                            # take over a thread it did not create. The channel bind
                            # is harmless (``set_channel`` writes an empty ``ts`` for
                            # an unclaimed cron, which never evicts) and overflow
                            # parts below still thread under ``thread_root``.
                            if thread_root and self.sessions:
                                if not job.thread_ts:
                                    await self.sessions.set_thread(session_key, thread_root)
                                else:
                                    # An inherited/explicit ``job.thread_ts`` must
                                    # not be stored as ``cron:<id>``'s anchor (the
                                    # claim above evicts the human owner), and any
                                    # anchor a prior top-level run left is cleared
                                    # to empty so ``set_channel`` keeps it empty
                                    # and the readers resolve the job's own
                                    # ``thread_ts`` through ``_cron_outbound_thread``
                                    # instead of a stale ts. ``set_thread(key, "")``
                                    # is an empty, unindexed anchor and evicts no
                                    # owner.
                                    await self.sessions.set_thread(session_key, "")
                                await self.sessions.set_channel(session_key, channel)
                            # Overflow parts as threaded follow-up messages
                            for part in parts[1:]:
                                await self.slack.post_message(channel, part, thread_root)
                            # Upload any extracted local images natively into the
                            # same thread (bytes travel, not the path — every gate
                            # in outbound_files was already applied). The reporting
                            # variant posts a redacted note into the thread for any
                            # upload Slack refuses, so a failed upload is never a
                            # silent loss (the image markup is already stripped).
                            if _cron_upload_files:
                                from kiro_crew.slack.files import (
                                    upload_outbound_files_reporting,
                                )

                                await upload_outbound_files_reporting(
                                    self.slack,
                                    channel,
                                    thread_root or "",
                                    _cron_upload_files,
                                )
                            # Dedup state: only advance after confirmed delivery.
                            self._record_cron_delivery(job, rh)
                        else:
                            logger.warning(
                                "Cron '%s': no channel resolved, skipping notification", job.name
                            )
                    except Exception as slack_exc:
                        logger.error(
                            "Cron job '%s': Slack delivery failed (job succeeded)",
                            job.name,
                            exc_info=True,
                        )
                        if self.dashboard_state:
                            exc_msg, _ = redact_exfiltration_urls(str(slack_exc))
                            exc_msg, _ = redact_credentials(exc_msg)
                            self.dashboard_state.notify(
                                "cron",
                                f"Cron: {job.name}",
                                f"⚠️ Job completed but Slack delivery failed: {exc_msg}",
                                meta={"job_id": job.id},
                            )
                # Session cleanup happens in finally block
                return result_text
            except Exception as exc:
                if isinstance(exc, SessionClosingError) and not _prompt_dispatched:
                    logger.info(
                        "Cron '%s': gateway admission closed before dispatch, deferring",
                        job.name,
                    )
                    _defer_cron_before_dispatch(job, "gateway admission is closed")
                    return None
                # Attempt one retry for ACP process death before any dedup / alert.
                exc_msg = str(exc).lower()
                # Set once the ACP retry below enters its attempt. If that
                # attempt fails it may have dispatched, so the transient ladder
                # must not run again. A failed reset leaves it unset.
                _acp_retry_attempted = False
                if (
                    # Match the death by type: the pipe-broken raise sites in
                    # acp/client.py word it "pipe broken", which no substring
                    # below covers. The typed arm is held to before dispatch,
                    # where no tool can have run yet. Once the retry attempt
                    # has started, this frame skips its transient ladder, so
                    # no prompt is sent again after that attempt.
                    # The substrings keep their old reach.
                    (
                        (isinstance(exc, AcpProcessDied) and not _prompt_dispatched)
                        or (
                            isinstance(exc, AcpError)
                            and ("not running" in exc_msg or "process exited" in exc_msg)
                        )
                        # A live backend that lost the session under the mapped
                        # id: the reset keeps the session-map entry, so the
                        # retry's claim re-loads the SAME id on a fresh runtime.
                        # Not after a tool ran: the retry re-sends the prompt.
                        or (
                            acp_error_is_session_not_found(exc)
                            and not acp_error_after_tool_activity(exc)
                        )
                    )
                    and not getattr(job, "_acp_retried", False)
                    and self.sessions is not None
                ):
                    logger.warning(
                        "Cron '%s': %s, resetting session and retrying",
                        job.name,
                        (
                            "backend lost the session"
                            if acp_error_is_session_not_found(exc)
                            else "ACP process died"
                        ),
                    )
                    job._acp_retried = True  # type: ignore[attr-defined]
                    try:
                        if _acquired:
                            self.sessions.release(session_key)
                            _acquired = False
                        await self.sessions.reset(session_key)
                        # The reset destroyed the live session that carried the
                        # consumed re-injection flag, and the retry starts the
                        # replacement cold (session-start context comes with a
                        # fresh session). Hand the bookkeeping to the retry, as
                        # the transient arm does, or the finally below would
                        # re-arm the REPLACEMENT after the retry lands and keeps
                        # it (deferred reset for pending sub-agents) -- one
                        # duplicated re-injection on its next turn. A compaction
                        # the destroyed session reported goes with it.
                        _needs_reinjection = False
                        _turn_compaction["completed"] = False
                        _acp_retry_attempted = True
                        return await _cron_callback(job)
                    except Exception:
                        pass  # retry failed — fall through to dedup + alert
                    finally:
                        job._acp_retried = False  # type: ignore[attr-defined]
                # ── Transient backend errors: retry the whole callback with ──
                # backoff instead of counting a failure. stream_and_collect's
                # in-stream retry only covers errors raised INSIDE the prompt
                # stream; a throttle/5xx during session acquire, client
                # creation, or context assembly propagates here, and without
                # this branch would go straight to record_failure(), marching
                # consecutive_failures toward auto-pause (threshold 5) on pure
                # infrastructure weather -- five throttled wakes silently
                # auto-pausing a healthy perpetual agent. The subagent path
                # retries these 3x with backoff; the cron path carries the same
                # semantics.
                #
                # Guarded by the same recursion marker pattern as the ACP
                # retry: the attempt counter lives on the job for the duration
                # of the outermost invocation only, and the recursive call
                # re-enters the full callback so a retry that succeeds runs
                # the complete delivery path.
                if (
                    acp_error_is_transient(exc)
                    and not _prompt_dispatched
                    and not _acp_retry_attempted
                ):
                    _t_attempt = getattr(job, "_transient_attempts", 0)
                    if _t_attempt < _CRON_TRANSIENT_RETRIES:
                        job._transient_attempts = _t_attempt + 1  # type: ignore[attr-defined]
                        _delay = transient_retry_delay(_t_attempt + 1)
                        logger.warning(
                            "Cron '%s': transient backend error (attempt %d/%d), "
                            "retrying in %.1fs: %s",
                            job.name,
                            _t_attempt + 1,
                            _CRON_TRANSIENT_RETRIES,
                            _delay,
                            exc,
                        )
                        # The callback only INCREMENTS `_transient_attempts`
                        # (above). Reading it into the persisted telemetry and
                        # clearing it for the next run both belong to
                        # `CronService._execute`, which owns every other
                        # per-run stamp (`last_run_ts` first among them): a
                        # write from inside this frame would land BEFORE that
                        # method stamps this run's `last_run_ts`, binding the
                        # count to the previous run. Its read-and-clear is in a
                        # `finally`, so a wake-budget cancellation landing in
                        # this sleep cannot strand the just-consumed attempt on
                        # the in-memory job either.
                        try:
                            if _acquired and self.sessions is not None:
                                self.sessions.release(session_key)
                                _acquired = False
                        except Exception:
                            logger.debug("release before transient retry failed", exc_info=True)
                        # The retry re-enters this callback on the SAME live
                        # session (no reset on this arm), so give it back the
                        # re-injection this attempt consumed and never delivered
                        # -- and hand the bookkeeping to the retry, or the
                        # finally below would re-arm a second time after it lands.
                        rearm_reinjection(
                            self.sessions,
                            session_key,
                            consumed=_needs_reinjection,
                            landed=_turn_landed,
                        )
                        # Same reason, for the build-time skill-body dedup: this
                        # attempt recorded its injected bodies as sent, but a
                        # transient error discarded the prompt before it landed
                        # (_turn_landed is False on this arm). Without the
                        # rollback the retry's build sees them already-sent and
                        # demotes to a pointer, so the body never reaches the
                        # window for a run that otherwise succeeds -- the skill
                        # is lost for that run. Roll back so the retry re-injects
                        # full bodies, and hand the settle to the retry (the
                        # finally below would otherwise settle a second time).
                        rollback_skill_bodies(self.ctx_builder, session_key, landed=_turn_landed)
                        _needs_reinjection = False
                        await asyncio.sleep(_delay)
                        return await _cron_callback(job)
                    # Retries exhausted — fall through to dedup + alert +
                    # record_failure: a persistent outage should still count.
                logger.exception("Cron job '%s' failed", job.name)
                # During an in-flight ACP retry (inner recursive _cron_callback
                # call), suppress all notify/slack/dedup work — the outer
                # invocation is authoritative and will handle notification
                # for the retry's final failure. Without this guard, the
                # inner call emits its own dashboard notify + Slack alert
                # and advances dedup state, duplicating the outer handler.
                if getattr(job, "_acp_retried", False):
                    raise
                # Was this run's failure this JOB's, or its runtime's? A cron
                # runtime hosts the job's own sub-agents, so a death there is a
                # process event several accounts witness -- and five of them
                # auto-pause a job that has done nothing wrong, which is the
                # threshold this guard protects. Read the record the death wrote
                # once, through the provider the manager still holds for this key
                # (never a pid, which a session must not be able to name).
                # Unattributable, or a runtime this job was alone on: counted
                # exactly as before.
                _job_owns_failure = runtime_death.caused_by_this_session(_run_provider)
                # Set only when the substitute bound reaches its limit below, and
                # consumed only beside record_failure(), so the three steps of the
                # hand-over stay contiguous.
                _hand_over_streak = False

                def _charge_failure() -> None:
                    """Charge this run's failure, performing any pending hand-over first.

                    The two are one indivisible move. The hand-over raises the
                    job's counter to one below the threshold so that the charge
                    below lands ON it, and forgets the streak whose value it just
                    transferred -- and none of those three steps may be separated
                    from the others by an await. ``cancel()`` is a plain
                    ``task.cancel()`` which, unlike the wake-budget timeout,
                    charges nothing on its way out, so a torn move would leave the
                    counter and the streak disagreeing with no writer left to
                    reconcile them.
                    """
                    if _hand_over_streak:
                        job.consecutive_failures = max(
                            job.consecutive_failures, _AUTO_PAUSE_THRESHOLD - 1
                        )
                        runtime_death.clear_shared_deaths(f"cron:{job.id}")
                    job.record_failure()

                if not _job_owns_failure:
                    # Not charging is not the same as never charging. A job whose
                    # shared runtime dies every run would otherwise re-fire on
                    # every tick forever, because record_failure() is the only
                    # actuator auto-pause has. So the exemption is bounded by the
                    # job's own threshold, counted against the shared runtime
                    # rather than against the job -- the same substitute bound the
                    # chat runner and the taskrunner use.
                    # Keyed to the JOB, never to this run's session key. The
                    # counter this substitutes for -- job.consecutive_failures --
                    # lives on the job, while a non-persistent job's session key
                    # is `cron:{id}:{uuid}`, fresh every run: a streak keyed there
                    # would never reach 2, so the bound would never fire and the
                    # exemption would be unbounded for exactly the jobs that most
                    # need it. A substitute bound is keyed to whatever owns the
                    # counter it replaces.
                    _shared_streak = runtime_death.note_shared_death(f"cron:{job.id}")
                    if _shared_streak >= _AUTO_PAUSE_THRESHOLD:
                        # At the limit the substitute bound HANDS OVER its
                        # accumulated value to the counter it stood in for,
                        # instead of adding a single charge to a counter still at
                        # zero. Adding one would deliver twice the bound this
                        # claims: the exemption spends the first
                        # _AUTO_PAUSE_THRESHOLD deaths, and the job's own counter
                        # would then need that many charges again, so a job whose
                        # shared runtime dies every run keeps firing for about
                        # twice as many runs as a job that was never exempted.
                        # The hand-over leaves the one charge below to land ON the
                        # threshold, so record_failure() stays the sole owner of
                        # both the counter and the pause, and the alert's
                        # displayed count is the number that actually paused it.
                        #
                        # The transfer itself is NOT performed here. It is a
                        # three-step move -- raise the counter, forget the streak,
                        # charge the failure -- and every await in this handler
                        # precedes the counter for the reason the comment beside
                        # record_failure() gives. Splitting the move across those
                        # awaits leaves a window: `cancel()` is a plain
                        # ``task.cancel()`` that charges nothing, so a cancellation
                        # during the failure alert would leave the counter at
                        # threshold-1 with the streak already erased, and the next
                        # failure would pause a job that had not reached the
                        # threshold. So the decision is recorded now and the move
                        # happens beside record_failure(), where the three steps
                        # are contiguous and cannot be torn apart.
                        _hand_over_streak = True
                        logger.warning(
                            "Cron '%s': the runtime it shares has died %d times running — "
                            "handing the streak to the job's own counter so it pauses now",
                            job.name,
                            _shared_streak,
                        )
                        _job_owns_failure = True
                    else:
                        logger.warning(
                            "Cron '%s': the runtime it shares died (%d running) — recording the "
                            "error but not counting it toward auto-pause",
                            job.name,
                            _shared_streak,
                        )
                # ── Failure dedup: suppress repeated identical crash notifications ──
                # A chain-exhaustion failure carries the fallback story on the
                # exception (llm_helpers.FALLBACK_STORY_ATTR); append it so the
                # alert names the whole walk, not just the last candidate's
                # error.
                # Redact over the FULL error text BEFORE any truncation — a cap
                # applied first can cut a credential at the boundary, leaving a
                # fragment the redaction regexes no longer match. The story is
                # redacted+capped centrally in fallback_story_of.
                _exc_text = f"{type(exc).__name__}: {exc}"
                _snf_text = str(exc)
                if (
                    acp_error_is_session_not_found(exc)
                    and SESSION_NOT_FOUND_GIVE_UP_TEXT not in _snf_text
                    and SESSION_NOT_FOUND_NOT_REPLAYED_TEXT not in _snf_text
                ):
                    # Name the outcome, not just the backend's words: the retry
                    # was skipped after a tool ran, or it ran and failed too.
                    _exc_text = (
                        f"{SESSION_NOT_FOUND_NOT_REPLAYED_TEXT} {_exc_text}"
                        if acp_error_after_tool_activity(exc)
                        else f"{SESSION_NOT_FOUND_GIVE_UP_TEXT} {_exc_text}"
                    )
                _exc_text, _ = redact_exfiltration_urls(_exc_text)
                _exc_text, _ = redact_credentials(_exc_text)
                # Delivery detail: the budget trims the ERROR part to leave the
                # story room (a verbose backend error must not evict the walk),
                # floored at half the cap — an oversized story trims its own
                # tail past that point instead of evicting the error.
                exc_detail = append_fallback_story(_exc_text, exc, budget=_CRON_FAILURE_DETAIL_CAP)
                # Dedup hashes the STORY-FREE error text: the walk can differ
                # between two occurrences of the same backend error (a
                # candidate momentarily unadvertised changes `walked`; a
                # skipped walk has no story at all), and a hash keyed on it
                # would miss the duplicate and re-page the user.
                fh = _result_hash(_exc_text)
                # Kept separately from the suppression gate below: `is_dup` still
                # selects the "still failing" wording on an alert that DOES go out
                # because the reminder window has expired.
                is_dup = fh == job.last_failure_hash
                if self._failure_alert_is_duplicate(job, fh):
                    # record_failure() is the counter's sole owner: a suppressed
                    # duplicate is still a failed run, so it must count toward
                    # the auto-pause threshold like every other failure path —
                    # unless the gate verdict already counted THIS run, in which
                    # case counting again would pause on arithmetic rather than
                    # on five distinct failures, or the failure belonged to a
                    # shared runtime rather than to this job.
                    if not _gate_counted and _job_owns_failure:
                        _charge_failure()
                    if job.auto_paused:
                        logger.warning(
                            "Cron '%s' auto-paused after %d consecutive failures",
                            job.name,
                            job.consecutive_failures,
                        )
                    logger.info(
                        "Cron '%s': duplicate failure #%d — suppressing Slack",
                        job.name,
                        job.consecutive_failures,
                    )
                    # Dashboard notify is best-effort — never mask the original
                    # exception if notification itself fails.
                    try:
                        if self.dashboard_state and not job.silent:
                            title = f"🔇 Cron: {job.name} (dup failure #{job.consecutive_failures})"
                            title, _ = redact_exfiltration_urls(title)
                            title, _ = redact_credentials(title)
                            self.dashboard_state.notify(
                                "cron",
                                title,
                                f"❌ Job failed (suppressed — same error):\n{exc_detail}",
                                meta={"job_id": job.id, "failure_hash": fh},
                            )
                    except Exception:
                        logger.debug(
                            "Dashboard notify failed in cron failure suppress path", exc_info=True
                        )
                    # SEL logging is best-effort — never mask the original
                    # exception if audit logging itself fails.
                    try:

                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_failure_dedup_suppress",
                            outcome="suppressed",
                            downstream_service="none",
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron failure suppress path",
                            exc_info=True,
                        )
                    raise
                # First failure (or fresh failure after reminder window) — alert.
                # Dashboard notify is best-effort — never mask the original
                # exception if notification itself fails.
                try:
                    if self.dashboard_state and not job.silent:
                        alert_title = f"Cron: {job.name}"
                        alert_title, _ = redact_exfiltration_urls(alert_title)
                        alert_title, _ = redact_credentials(alert_title)
                        # Carry the reason, matching the suppressed-duplicate body
                        # below: without it the FIRST alert — the one the user
                        # actually reads — was the least informative of the two.
                        self.dashboard_state.notify(
                            "cron",
                            alert_title,
                            f"❌ Job failed:\n{exc_detail}",
                            meta={"job_id": job.id, "failure_hash": fh},
                        )
                except Exception:
                    logger.debug(
                        "Dashboard notify failed in cron failure alert path", exc_info=True
                    )
                # Include the machine hostname so multi-gateway setups (e.g. a
                # laptop + a cloud desktop both running KiroCrew) can tell which
                # machine's session failed. This is framework-level: the ❌ DM
                # can fire before any prompt logic runs (e.g. a session-startup
                # credential failure), so the machine name must come from here,
                # not from inside the cron prompt.
                host = socket.gethostname().split(".")[0]
                # Slack PARSES entity markup here, and job.name is user-authored:
                # a job named `<!channel>` notifies the whole channel on failure.
                # Same sink and same source as the script/command alert, so it
                # goes through the one shared spelling of that hardening.
                safe_name = self._slack_safe_fenced(job.name)
                # Carry the reason here too. The dashboard body above and the
                # script/command DM both do, so leaving this one at "check logs"
                # made the DM the only failure surface that still withheld what
                # the caller already knows.
                safe_reason = self._slack_safe_fenced(exc_detail)
                # +1 only when the charge below will actually land. This run's
                # failure is recorded after the awaited Slack attempt, so the
                # display count has to add it explicitly -- but it is now gated on
                # _job_owns_failure, and an exempted run that still claimed
                # "N+1 consecutive failures" would report a number the counter
                # driving auto-pause never reached.
                _display_bump = 1 if (_job_owns_failure and not _gate_counted) else 0
                if is_dup:
                    fail_msg = (
                        f"⏰ *Cron: {safe_name}* ❌ _Job still failing on {escape_mrkdwn(host)}"
                        f" ({job.consecutive_failures + _display_bump} consecutive failures)"
                        f" — check logs._\n```{safe_reason}```"
                    )
                else:
                    fail_msg = (
                        f"⏰ *Cron: {safe_name}* ❌ "
                        f"_Job failed on {escape_mrkdwn(host)} — check logs._\n"
                        f"```{safe_reason}```"
                    )
                # Never trust interpolated content (job.name is user-controlled):
                # scrub exfiltration URLs + credentials before it reaches Slack,
                # mirroring the dashboard alert_title redaction above.
                fail_msg, _ = redact_exfiltration_urls(fail_msg)
                fail_msg, _ = redact_credentials(fail_msg)
                # Channel-neutral twin of ``fail_msg``: the same sentence with no
                # mrkdwn. Composed separately rather than reusing the escaped
                # form because Slack's markup is not another channel's dialect --
                # a fence and a ``&lt;`` would reach that reader literally -- and
                # separately from ``render_for_slack`` is how the success leg
                # composes its own channel string too.
                if is_dup:
                    channel_fail_msg = (
                        f"⏰ Cron: {job.name} ❌ Job still failing on {host}"
                        f" ({job.consecutive_failures + _display_bump} consecutive failures)"
                        f" — check logs.\n{exc_detail}"
                    )
                else:
                    channel_fail_msg = (
                        f"⏰ Cron: {job.name} ❌ Job failed on {host} — check logs.\n"
                        f"{exc_detail}"
                    )
                channel_fail_msg, _ = redact_exfiltration_urls(channel_fail_msg)
                channel_fail_msg, _ = redact_credentials(channel_fail_msg)
                # Silent jobs still execute but suppress notifications (UI bells
                # AND Slack DMs). The failure is still logged at warning level
                # and counted toward auto-pause above — we just skip
                # user-facing noise.
                # One spelling of the one-surface rule and both delivery legs,
                # shared with the script/command alert. `channel_fail_msg` rather
                # than `fail_msg` for the channel leg: that string is mrkdwn, no
                # other transport parses it, and the channel form also carries the
                # repeat-failure wording and both egress redaction passes.
                #
                # Placed before record_failure() deliberately: every await in this
                # handler must precede the counter, so a cancellation mid-alert
                # cannot leave the run counted twice.
                channel_delivered, slack_delivered, slack_failed = (
                    await self._deliver_failure_alert(
                        job,
                        mrkdwn=fail_msg,
                        plain=channel_fail_msg,
                        actor_key=session_key,
                        silent=job.silent,
                    )
                )
                # record_failure() is the counter's sole owner: it continues an
                # accumulation another writer (gate verdict, timeout) already
                # built up instead of restarting at 1, and it is deliberately
                # NOT gated on the alert's Slack delivery above — the run
                # failed either way, and a job whose failure alerts also fail
                # must still reach the auto-pause threshold. It runs AFTER the
                # awaited Slack attempt so a timeout cancelling this handler
                # mid-alert cannot leave the run counted here AND again by the
                # timeout handler. For the same reason it defers when the gate
                # verdict already counted THIS run, or when the failure belonged
                # to a runtime this job was sharing rather than to the job.
                if not _gate_counted and _job_owns_failure:
                    _charge_failure()
                if job.auto_paused:
                    logger.warning(
                        "Cron '%s' auto-paused after %d consecutive failures",
                        job.name,
                        job.consecutive_failures,
                    )
                # One spelling of the advance rule, shared with the
                # script/command alert: the anchor moves once the reason reached
                # someone, and only a REAL Slack exception holds it back.
                self._advance_failure_dedup(
                    job, fh, channel_delivered=channel_delivered, slack_failed=slack_failed
                )
                # The SEL record was nested INSIDE the advance condition before
                # this refactor, so a Slack exception suppressed the audit line as
                # well as the anchor. Preserved verbatim rather than quietly
                # widened -- whether the audit should be unconditional (the
                # script/command alert logs it either way) is a behaviour question,
                # not a consolidation one. Restating the condition is what makes
                # that gating visible instead of implied by indentation.
                if channel_delivered or not slack_failed:
                    # SEL logging is best-effort — never mask the original
                    # exception if audit logging itself fails.
                    try:
                        # Name every surface the alert actually left on, so the
                        # trail does not read "none" for a crash sent to Discord.
                        surfaces = ["slack"] if slack_delivered else []
                        if channel_delivered:
                            surfaces.append(channel_namespace_of(job.session_key))
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_failure_alert",
                            outcome="suppressed" if job.silent else "alerted",
                            downstream_service=",".join(surfaces) or "none",
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron failure alert path",
                            exc_info=True,
                        )
                raise
            finally:
                assert self.sessions is not None
                # Before the reset below: a turn that consumed the post-compaction
                # flag but never landed, or whose backend compacted the session,
                # sets the flag so a session that survives (deferred reset)
                # re-injects on its next turn.
                rearm_reinjection(
                    self.sessions,
                    session_key,
                    consumed=_needs_reinjection,
                    landed=_turn_landed,
                    compacted=_turn_compaction["completed"],
                )
                rollback_skill_bodies(self.ctx_builder, session_key, landed=_turn_landed)
                if _acquired:
                    self.sessions.release(session_key)
                    # Defer session reset if subagents are still running,
                    # queued behind the concurrency/stagger gate, or
                    # mid-injection — _subagent_done will reset after the last one.
                    has_pending = bool(
                        self.subagent_mgr
                        and await _subagent_work_pending(self.subagent_mgr, session_key)
                    )
                    has_injecting = self._cron_injecting.get(session_key, 0) > 0
                    if has_pending or has_injecting:
                        logger.info("Cron '%s': deferring reset, subagents pending", job.name)
                        # leave the active-session registration in place so
                        # the reaper can still target the ephemeral key if the deferred
                        # reset hangs. _subagent_done will clear it after the real reset.
                    else:
                        await self.sessions.reset(session_key)
                        # reset done → reaper no longer needs this key.
                        if self.cron_svc is not None:
                            self.cron_svc.clear_active_session_key(job.id, session_key)
                # Per-job env vars (single-agent path) travel via extra_env passthrough

        self._cron_reconciled = False
        self._cron_armed = False
        # An unparseable store refuses every write, and the app cron cleanup
        # below and in the dashboard's startup hooks writes to it, so the
        # gateway could not start. create() moves it aside instead.
        self.cron_svc = await CronService.create(
            base_dir=data_home(), on_job=_cron_callback, quarantine_unreadable=True
        )
        moved = getattr(self.cron_svc, "quarantined_store", None)
        self._cron_quarantine = moved if isinstance(moved, Path) else None
        if self.dashboard_state:
            self.cron_svc.set_refresh_callback(self.dashboard_state.push_refresh)
        if self._no_crons:
            logger.info("Cron scheduler disabled (--no-crons)")
        else:
            # CronService.create has loaded durable jobs but has not armed a
            # timer yet. Remove jobs owned by disabled or execution-denied apps
            # at this boundary; if cleanup cannot complete, leave the entire
            # scheduler stopped rather than risk firing a denied command.
            from kiro_crew.apps.bridges import reconcile_app_crons_for_execution

            try:
                await reconcile_app_crons_for_execution(self.cron_svc)
            except Exception:
                logger.exception(
                    "App cron execution reconciliation failed; refusing to arm "
                    "the cron scheduler"
                )
                return
            self._cron_reconciled = True
            if arm:
                await self._start_cron_after_memory_ready()

    def _announce_cron_quarantine(self) -> None:
        """Tell the dashboard that startup moved an unreadable cron store aside."""
        moved = getattr(self, "_cron_quarantine", None)
        if moved is None or self.dashboard_state is None:
            return
        self.dashboard_state.notify(
            "cron",
            "Scheduled tasks could not be read",
            "The schedule file was not a readable schedule (invalid JSON, or no jobs "
            f"list), so Kiro Crew moved it to {moved} and started with no scheduled "
            "tasks. Nothing was deleted. To restore: fix that copy, stop Kiro Crew, "
            f"move it back to {data_home() / 'crons.json'}, and start again. "
            "`kirocrew doctor` shows the same steps.",
        )

    async def _start_cron_after_memory_ready(self) -> None:
        """Arm overdue jobs only after the memory preparation fence completes."""
        if self._no_crons or self.cron_svc is None or not self._cron_reconciled:
            return
        if self._cron_armed:
            return
        startup = getattr(self, "_memory_startup", None)
        if startup is not None and (startup.stopped or not startup.ready):
            raise RuntimeError("Cron scheduler cannot start before memory preparation completes")
        await self.cron_svc.start()
        self._cron_armed = True
        if self.sessions:
            self.cron_svc.start_reaper(self.sessions)
        else:
            logger.warning("Cron reaper not started: sessions not available")

    # gateway_runtime/heartbeat_turns.py
    _init_heartbeat = _heartbeat._init_heartbeat

    # gateway_runtime/nudge_channel_fire.py
    _fire_slack_nudge = _nudge_chan._fire_slack_nudge

    _fire_dm_nudge = _nudge_chan._fire_dm_nudge

    _fire_discord_nudge = _nudge_chan._fire_discord_nudge

    _fire_webex_nudge = _nudge_chan._fire_webex_nudge

    _stop_message_loop_if_structural_terminal = (
        _nudge_chan._stop_message_loop_if_structural_terminal
    )

    _monitor_completion_hook = _nudge_chan._monitor_completion_hook

    _report_monitor_completion = _nudge_chan._report_monitor_completion

    # gateway_runtime/nudge_dashboard_fire.py
    _fire_dashboard_nudge = _nudge_dash._fire_dashboard_nudge

    _work_ledger_wake_loop = _nudge_dash._work_ledger_wake_loop

    _work_ledger_wake_text = _nudge_dash._work_ledger_wake_text

    _steer_work_ledger_wake = _nudge_dash._steer_work_ledger_wake

    _dashboard_mode_admits = staticmethod(_nudge_dash._dashboard_mode_admits)

    _audit_fire_refused = staticmethod(_nudge_dash._audit_fire_refused)

    # gateway_runtime/nudge_wiring.py
    _init_autonudge = _nudge_wiring._init_autonudge

    _notify_nudge_held = _nudge_wiring._notify_nudge_held

    _notify_nudge_expired = _nudge_wiring._notify_nudge_expired

    @staticmethod
    def _defer_queued_delivery(
        slot: Any, announce: str, info: SubagentInfo, *, flush_only: bool
    ) -> None:
        """Owe a queued completion's delivery tombstones to the queue drain.

        The retention window for ``result.txt`` (``agent.subagent_result_ttl_secs``)
        exists so the parent can read the full transcript AFTER the completion
        event reaches it. Writing the ``delivered`` tombstone when the announce is
        merely QUEUED starts that clock while the event is still waiting for a
        turn, so a long-running turn ahead of it lets the reaper prune every file
        the queued announce points at.

        So the ids are handed to the slot keyed on the announce ITSELF,
        ``_delivery_queued`` tells the run loop to skip its own ``mark_delivered``,
        and the drain (``chat_runner._start_next_queued_turn``) settles them once a
        turn has consumed that announce -- including a retry, because a failure
        before the model consumed the prompt re-queues the same text under a newly
        minted queue id, which a debt keyed on the original id could never match. A
        wave digest carries its held members' snapshots too:
        ``_digest_settle_deliveries`` is
        transferred (not copied), so the run loop's ``_settle_digest_holds`` becomes
        a no-op rather than a second writer.

        Best-effort in one direction only: if the slot cannot take the ids (a
        stubbed slot in tests), nothing is transferred and the previous
        immediate-tombstone behaviour stands — better a short window than a
        folder no one ever tombstones.
        """
        # A flush-only record is synthetic (no run, no folder of its own). Only a
        # COMPLETED member owes a delivered mark: ``info.outcome`` is the codebase's
        # canonical three-way classification precisely because the ``error``-
        # nullability idiom reports a user-stopped agent as completed, and a
        # stopped or failed run already carries its own tombstone whose 7-day
        # post-mortem window a "delivered" write would shorten to the result TTL.
        # A memory-wait expiry has no folder either, but the store owes its report
        # until the announce is consumed, so its debt is carried the same way.
        owed: list[SubagentDelivery] = []
        if not flush_only and info.outcome == "completed":
            owed.append(SubagentDelivery(info.id, info.elapsed, info.credits))
        elif not flush_only and getattr(info, "_report_owed", False) is True:
            owed.append(SubagentDelivery(info.id, info.elapsed, info.credits, report_owed=True))
        held = getattr(info, "_digest_settle_deliveries", None)
        if isinstance(held, list):
            owed.extend(held)
        try:
            slot.note_pending_subagent_delivery(announce, owed)
        except Exception:
            logger.debug(
                "Subagent %s: could not defer queued delivery marks", info.id, exc_info=True
            )
            return
        info._delivery_queued = True
        if isinstance(held, list):
            info._digest_settle_deliveries = []

    @staticmethod
    def _notif_meta(parent_key: str | None) -> dict[str, str] | None:
        """Build notification meta with slot or slack_link for jump-to-source."""
        if not parent_key:
            return None
        # A jump-to-source slot beats a channel deep link whenever a tab is
        # open, including for a channel-born conversation whose key is the
        # channel's own.
        slot = dashboard_slot_key(parent_key)
        if slot:
            return {"slot": slot}
        if ":" in parent_key and not parent_key.startswith(("cron:", "subagent:", "hook:")):
            chan, ts = parent_key.split(":", 1)
            return {
                "slack_link": f"https://amzn-aws.slack.com/archives/{chan}/p{ts.replace('.', '')}"
            }
        return None

    # gateway_runtime/result_egress.py
    _deliver_cron_response = _egress._deliver_cron_response

    _deliver_channel_reply = _egress._deliver_channel_reply

    _cron_outbound_thread = _egress._cron_outbound_thread

    _persist_slot_title = _egress._persist_slot_title

    _resolve_loaded_slot = _egress._resolve_loaded_slot

    _deliver_result = _egress._deliver_result

    # gateway_runtime/mcp_broker.py
    _init_mcp_discovery = _mcp_broker._init_mcp_discovery

    def _subagent_coalescer(self) -> "SubagentEventCoalescer":
        """Lazily construct the scale coalescer (needs dashboard_state +
        subagent_mgr, both wired after __init__)."""
        if self._subagent_coalescer_inst is None:
            from kiro_crew.subagent_scale import SubagentEventCoalescer

            _state = self.dashboard_state

            def _bcast_all(t: str, d: dict) -> None:
                if _state:
                    _state.broadcast_ws(t, d)

            def _bcast_subs(t: str, d: dict) -> None:
                if _state:
                    _state.broadcast_ws_subagent_subscribers(t, d)

            self._subagent_coalescer_inst = SubagentEventCoalescer(
                _bcast_all,
                _bcast_subs,
                lambda: self.subagent_mgr.running_count if self.subagent_mgr else 0,
            )
        return self._subagent_coalescer_inst

    def _init_subagents(self) -> None:
        """Initialize the subagent manager."""
        from kiro_crew.recovery.ladder import configure_default_ladder

        # The process recovery ladder is snapshotted from config HERE because
        # this step is unconditional and runs before the task runner, the
        # dashboard and any chat slot — i.e. before any layer can decide a
        # retry, and outside the event-loop failure branch that would otherwise
        # be the first to want a delay. `agent.recovery_backoff_*` are
        # restart=True, so one snapshot is the whole contract.
        configure_default_ladder(self._cfg)

        # Per-slot WS events route by EXACT slot-key match in the frontend —
        # `subagent_event_slot` maps a parent session key to the tab that
        # displays it (cron-born tabs are `cron-<id>`, channel-born tabs their
        # transcript stem), falling back to the legacy prefix-strip when no
        # tab is open. A raw `removeprefix("dashboard:")` here left the
        # Subagents panel permanently empty for cron/channel-born sessions.
        _event_slot = subagent_event_slot

        async def _broadcast_subagent_status(
            info: SubagentInfo,
            event: str,
            selected_slot_name: str = "",
        ) -> None:
            """Broadcast subagent status change via WS for per-slot tracking."""
            if not self.dashboard_state:
                return
            try:
                slot = selected_slot_name or _event_slot(info.parent_session_key)
                agents = (
                    self.subagent_mgr.running_agents_for(info.parent_session_key)
                    if self.subagent_mgr
                    else []
                )
                running = len(agents)
                payload = {
                    "running": running,
                    "id": info.id,
                    "event": event,
                    "slot": slot,
                    "agents": agents,
                }
                logger.info(
                    "📡 subagent_status WS: event=%s slot=%s running=%d agents=%d",
                    event,
                    slot,
                    running,
                    len(agents),
                )
                self.dashboard_state.broadcast_ws("subagent_status", payload)
            except Exception:
                logger.info("Failed to broadcast subagent %s status", info.id, exc_info=True)

        def _retrigger_recovery(slot: "_ChatSlot", parent_key: str) -> None:
            """Drain queued failures into a new recovery _run_chat turn.

            Called from _on_done callbacks after resetting the guard, so
            failures that arrived while the previous recovery was running
            get processed without waiting for user input.
            """
            if slot._recovery_chat_triggered or not slot._pending_subagent_failures:
                return
            if not self.dashboard_state:
                return
            _max_retrigger = 3
            if slot.recovery_retrigger_count >= _max_retrigger:
                logger.warning(
                    "Recovery retrigger cap (%d) reached for %s, dropping %d queued failures",
                    _max_retrigger,
                    parent_key,
                    len(slot._pending_subagent_failures),
                )
                slot._pending_subagent_failures.clear()
                return
            slot.recovery_retrigger_count += 1
            slot._recovery_chat_triggered = True
            # Bound here rather than at module scope: this reads ``_run_chat`` from
            # ``dashboard.chat``, a different module than the top-level
            # ``chat_runner`` import, and resolving it per call is what lets a test
            # patch ``dashboard.chat._run_chat`` and have this path observe it.
            from kiro_crew.dashboard.chat import _run_chat

            failures = slot._pending_subagent_failures[:]
            slot._pending_subagent_failures.clear()
            msg = "\n\n".join(failures)
            msg, _ = redact_exfiltration_urls(msg)
            msg, _ = redact_credentials(msg)
            slot.append("user", msg, "msg msg-u auto-go")
            logger.info(
                "Re-triggering recovery _run_chat for %s (%d queued failures)",
                parent_key,
                len(failures),
            )

            def _done(t: asyncio.Task) -> None:  # type: ignore[type-arg]
                if t.cancelled():
                    logger.warning("Re-triggered recovery cancelled for %s", parent_key)
                    slot._recovery_chat_triggered = False
                    return
                elif t.exception():
                    logger.error(
                        "Re-triggered recovery failed for %s",
                        parent_key,
                        exc_info=t.exception(),
                    )
                slot._recovery_chat_triggered = False
                if slot._pending_subagent_failures:
                    _retrigger_recovery(slot, parent_key)

            _task = asyncio.create_task(
                bounded_chat_turn(
                    _run_chat(
                        self.dashboard_state,
                        slot,
                        msg,
                        _directive_user_origin=False,
                        # The text is the re-injected sub-agent completion or
                        # failure this recovery drains, so the sub-agent is what
                        # caused the turn.
                        _turn_actor="subagent",
                    ),
                    state=self.dashboard_state,
                    slot=slot,
                ),
            )
            slot.task = _task
            self._background_tasks.add(_task)
            _task.add_done_callback(self._background_tasks.discard)
            _task.add_done_callback(_done)

        async def _subagent_done(info: SubagentInfo) -> None:
            async def _inject_with_retry(
                client,
                msg: str,
                parent_key: str,
                label: str,
            ) -> str | None:
                """Retry stream_and_collect up to 3 times on AcpError.

                Cancels any orphaned prompt between attempts so the next
                retry doesn't hit 'Prompt already in progress'. A live backend
                that lost the parent's session is answered once by a reset and a
                fresh claim, which re-loads the same backend id; the caller's
                single ``release(parent_key)`` balances against that claim.
                """
                _snf_reloaded = False
                attempt = 0
                while True:
                    try:
                        response = await stream_and_collect(client, msg, retry_transient=False)
                        # The parent's turn consumed the report: the positive
                        # acknowledgement an owed expiry report is cleared on.
                        info._report_delivered = True
                        return response
                    except PromptBusyExhaustedError:
                        # Provider is dead after exhausting prompt-busy retries.
                        # Reset session + notify, same as TimeoutError path.
                        logger.error(
                            "Subagent %s: provider dead after prompt-busy retries (%s)",
                            info.id,
                            label,
                        )
                        # Same ownership question as the AcpProcessDied arm below:
                        # the provider being dead is a PROCESS fact, and on a
                        # shared runtime the process that died was carrying the
                        # parent and its co-tenants too. Resetting the parent for
                        # a death it did not cause takes their sessions with it.
                        if runtime_death.caused_by_this_session(client):
                            try:
                                assert self.sessions is not None
                                await self.sessions.reset(parent_key)
                            except Exception:
                                logger.debug(
                                    "Failed to reset %s after busy exhaustion",
                                    parent_key,
                                    exc_info=True,
                                )
                        else:
                            logger.warning(
                                "Subagent %s: the SHARED runtime died — leaving parent %s "
                                "to re-acquire on its own next turn",
                                info.id,
                                parent_key,
                            )
                        if self.subagent_mgr:
                            self.subagent_mgr.notify_injection_failed(
                                info,
                                reason="provider dead after prompt-busy retries",
                            )
                        return None
                    except AcpProcessDied:
                        logger.warning(
                            "Subagent %s: ACP process died during %s injection",
                            info.id,
                            label,
                        )
                        # The parent is reset only when the death was the
                        # PARENT's runtime ending. A sub-agent runs on its
                        # parent's process, so this handler also catches the
                        # child's own death -- and resetting the parent for that
                        # tears down a healthy conversation, plus every other
                        # tenant's session with it, over a child that failed.
                        # A single-tenant runtime is reset exactly as before.
                        if runtime_death.caused_by_this_session(client):
                            try:
                                assert self.sessions is not None
                                await self.sessions.reset(parent_key)
                            except Exception:
                                logger.debug(
                                    "Failed to reset %s after process death",
                                    parent_key,
                                    exc_info=True,
                                )
                        else:
                            logger.warning(
                                "Subagent %s: the SHARED runtime died — leaving parent %s "
                                "to re-acquire on its own next turn",
                                info.id,
                                parent_key,
                            )
                        if self.subagent_mgr:
                            self.subagent_mgr.notify_injection_failed(
                                info,
                                reason="ACP process died",
                            )
                        return None
                    except AcpError as exc:
                        if acp_error_is_session_not_found(exc):
                            if acp_error_after_tool_activity(exc):
                                raise AcpError(
                                    f"{SESSION_NOT_FOUND_NOT_REPLAYED_TEXT} {exc}"
                                ) from exc
                            if _snf_reloaded:
                                raise AcpError(f"{SESSION_NOT_FOUND_GIVE_UP_TEXT} {exc}") from exc
                            _snf_reloaded = True
                            logger.warning(
                                "Subagent %s: backend lost parent session %s during %s "
                                "injection — re-loading it on a fresh runtime",
                                info.id,
                                parent_key,
                                label,
                            )
                            assert self.sessions is not None
                            await self.sessions.reset(parent_key)
                            client, _is_new, _resumed = await self.sessions.get_or_create(
                                parent_key
                            )
                            continue
                        if attempt == 2:
                            raise
                        logger.warning(
                            "Subagent %s %s injection attempt %d failed, retrying",
                            info.id,
                            label,
                            attempt + 1,
                        )
                        try:
                            assert self.sessions is not None
                            await self.sessions.cancel_current(parent_key)
                        except Exception:
                            logger.debug(
                                "Failed to cancel parent prompt for %s",
                                info.id,
                                exc_info=True,
                            )
                        await asyncio.sleep(2**attempt)
                        attempt += 1

            # A synthetic flush-only record is NOT a wave member (see
            # SubagentManager.force_digest_flush): it exists only to force the
            # wave's pending digest chunk out when its hold deadline expired.
            # Every per-member side effect below must be skipped for it — a
            # terminal WS event, orchestration accounting or a done/ok counter
            # bump would invent an agent that never ran.
            _flush_only = getattr(info, "_digest_flush_only", False) is True
            parent_key = info.parent_session_key
            _parent_slot_name = dashboard_slot_key(parent_key)

            _injection_slot = None
            if self.dashboard_state and _parent_slot_name:
                from kiro_crew.dashboard.handlers.messaging import _slot_for_parent

                _injection_slot = _slot_for_parent(self.dashboard_state, parent_key)
                if _injection_slot is None:
                    _injection_slot = self.dashboard_state.get_slot(_parent_slot_name)
                if getattr(_injection_slot, "window_pending", False) is True:
                    # A parent tab restored as a sidebar row loads its window
                    # before the completion is delivered into it, off the loop.
                    await load_window_off_loop(_injection_slot)
                    _injection_slot = _slot_for_parent(
                        self.dashboard_state, parent_key
                    ) or self.dashboard_state.get_slot(_parent_slot_name)
            _completion_key = getattr(_injection_slot, "key", "")
            _injection_slot_name = (
                _completion_key
                if isinstance(_completion_key, str) and _completion_key
                else _event_slot(parent_key)
            )

            if not _flush_only:
                await _broadcast_subagent_status(info, "done", _injection_slot_name)
            # Three-way outcome: a user stop is neutral — neither a success nor
            # a failure. The record contract keeps ``error`` unset for stops, so
            # every consumer below must branch on ``user_stopped`` explicitly
            # rather than inferring success from an empty error.
            if info.user_stopped:
                # The stop's own origin when the record carries one (a
                # parent-end verb), so the announce does not
                # credit the user with a stop they never pressed.
                status, emoji, single_outcome = (
                    getattr(info, "_stop_origin", "") or "stopped by user",
                    "⏹",
                    OUTCOME_STOPPED,
                )
            elif info.error:
                status, emoji, single_outcome = "failed", "❌", OUTCOME_FAILED
            else:
                status, emoji, single_outcome = "completed", "✅", OUTCOME_OK
            title = f"Subagent `{info.id}` {emoji}"

            # Chat mode: inline info.result (subagent.py already trimmed it to
            # agent.completion_keep + completion_keep_chars) when it fits. When the
            # completion copy dropped content (result_truncated), emit a summary +
            # result_path pointer so the parent reads the full
            # transcript on demand (read / grep / spawn_status) instead of re-running
            # the subagent.
            result_path = info.result_path or ""
            if info.user_stopped:
                _partial = info.result or ""
                # Same origin as the status line above: a parent end or a row
                # chat Autopilot queued must not read as the user's own Stop in
                # the digest text.
                _origin = getattr(info, "_stop_origin", "") or "stopped by user"
                _who = (
                    "Stopped by the user"
                    if _origin == "stopped by user"
                    else f"Stopped ({_origin})"
                )
                detail = (
                    f"{_who} before completing. Do NOT treat this as "
                    "a finished result or retry it unprompted."
                    + (f"\n\nPartial output:\n{_partial}" if _partial else "")
                )
            elif info.error:
                detail = f"Error: {info.error}"
                # A run that ended on a stall / cancel / transport death keeps
                # what it streamed as a flagged PARTIAL (``info.partial``, set by
                # the stop-reason classifier in subagent_manager/run.py); deliver
                # it so the parent continues from it instead of re-submitting the
                # whole task.
                if info.partial and info.result:
                    detail += (
                        "\n\nPartial output (the run did NOT finish — do not treat "
                        f"this as a completed result):\n{info.result}"
                    )
            elif result_path and info.result_truncated:
                detail = summarize_result(info.result, result_path)
            else:
                detail = info.result or "_No response._"
            detail, _ = redact_exfiltration_urls(detail)
            detail, _ = redact_credentials(detail)
            task_text, _ = redact_exfiltration_urls(info.task)
            task_text, _ = redact_credentials(task_text)
            task_text = task_text[:100]
            usage = format_subagent_usage(info.credits, info.elapsed)
            body = f"{task_text}\n\nUsage: {usage}\n\n{detail}"
            title, _ = redact_exfiltration_urls(title)
            title, _ = redact_credentials(title)

            # A completed run that made no tool call cannot have touched the disk,
            # so it carries the claim note in the event the parent reads its
            # narration from. A lone run or a single wave gets no synthesis turn,
            # so this event is the only place the parent can be told before it
            # reports the work to the user. Stops and failures say nothing: their
            # prose already tells the parent not to treat the output as finished.
            no_tools_note = ""
            if single_outcome == OUTCOME_OK and not _flush_only and info.made_no_tool_calls:
                no_tools_note = f"⚠ Agent `{info.id}` {NO_TOOL_CALLS_NOTE}"
            announce = (
                f"{SUBAGENT_COMPLETION_PREFIX}\n"
                f"Agent `{info.id}`"
                f"{f' ({info.agent})' if info.agent else ''}"
                f" {status} {emoji}\n"
                f"Task: {task_text}\n\n"
                f"Usage: {usage}\n\n"
                f"{detail}" + (f"\n\n{no_tools_note}" if no_tools_note else "")
            )
            # Structured header facts for the dashboard card, stamped on the row
            # so a reword of the prose above cannot silently break rendering.
            # The card reads this; the frontend regexes are a fallback.
            # Reassigned for the wave-digest shapes below when this member's
            # completion is folded into a batch chunk instead of injected alone.
            sub_meta = single_completion_meta(
                agent_id=info.id,
                outcome=single_outcome,
                agent_name=info.agent or "",
                task=task_text,
                requested_model=info.requested_model or info.model or "",
                resolved_model=info.resolved_model or "",
            )

            if _flush_only:
                # The synthetic record has no result of its own. Its title/body
                # are only used by the "parent slot gone → notification only"
                # fallback, so make them describe the WAVE, not a phantom agent.
                title = "Wave results (partial)"
                body = task_text

            # ── Batch accounting + wave digest (scale plumbing) ──
            # Every batch member is accounted here (the single completion
            # consumer for all terminal paths). Waves larger than the digest
            # threshold deliver ONE consolidated injection turn when the wave
            # finishes, instead of N per-agent turns — at 60-100 agents the
            # per-agent turns are the parent-context flood (N full LLM turns)
            # and bury the 2 failures among 58 successes.
            # (Type guards: test doubles pass MagicMock infos whose attrs are
            # truthy mocks — only real str/int batch identity participates.)
            _batch_id = getattr(info, "batch_id", "")
            _batch_total = getattr(info, "batch_total", 0)
            if not isinstance(_batch_id, str):
                _batch_id = ""
            if not isinstance(_batch_total, int):
                _batch_total = 0
            if _flush_only and not _batch_id:
                # A flush-only record without wave identity has nothing to
                # release and MUST NOT fall through to the per-agent routing
                # below — that would inject a completion turn for an agent that
                # never ran.
                return
            # The one await on the wave-accounting path runs BEFORE this
            # member touches the wave record. Everything from reading
            # ``_batch_progress`` to the hold-or-flush decision below is then a
            # single synchronous block, so a sibling completion can never close
            # the wave between this member writing its result line and deciding
            # how that line is delivered: the closer's settle set always carries
            # every member its digest names. While this member awaits, its own
            # report hold is still armed, so no sibling closes the wave on it.
            # The lookup is skipped when this member's count alone closes the
            # wave; nothing can interleave between the peek and the block.
            _no_batch_pending = False
            if _batch_id and not _flush_only and self.subagent_mgr:
                _peek = self._batch_progress.get(_batch_id)
                _peek_done = _peek["done"] if _peek is not None else 0
                if not (_batch_total > 0 and _peek_done + 1 >= _batch_total):
                    try:
                        _members = await _subagent_batch_pending(self.subagent_mgr, _batch_id)
                        _no_batch_pending = not _members
                    except Exception:
                        _no_batch_pending = False
            if _batch_id:
                if _flush_only:
                    # Nothing to release (wave already closed / already flushed
                    # by a completion that raced this sweep) → no-op.
                    _bp = self._batch_progress.get(_batch_id)
                    if _bp is None or _bp["done"] <= _bp["flushed"]:
                        return
                    bp = _bp
                    # A forced flush never closes the wave: the sweep only fires
                    # while members are still outstanding, so the wave-close
                    # digest (counts + release guidance) is still to come.
                    _last = False
                    _oc = ""
                else:
                    bp = self._batch_progress.setdefault(
                        _batch_id,
                        {
                            "total": _batch_total,
                            "done": 0,
                            "ok": 0,
                            "err": 0,
                            "stopped": 0,
                            # Failed members a memory wait ended before they
                            # started: dropped work the parent may resubmit. A
                            # refusal is not counted; resubmitting it would only
                            # be refused again.
                            "not_run": 0,
                            "fail_lines": [],
                            "ok_lines": [],
                            "held_ok_deliveries": [],
                            # Members whose delivery is currently held, so the
                            # hold-deadline sweep's timestamps can be cleared
                            # when their chunk finally fires.
                            "held_infos": [],
                            # Chunked delivery bookkeeping: "flushed" = members whose
                            # results have already been delivered in a prior chunk;
                            # "chunks" = digest chunks emitted so far.
                            "flushed": 0,
                            "chunks": 0,
                        },
                    )
            if _batch_id and not _flush_only:
                # This member's report has been consumed: its contribution is
                # about to land in the wave's done-count, so it stops holding
                # the wave-close fallback open (batch_reports_in_flight).
                # Release in the same synchronous block as the
                # increment so the hold and the count can never be observed
                # apart. The release goes to the manager-level registry -- NOT
                # an agent-record flag -- so an operator clear (DELETE
                # /api/spawn) popping this member from ``_agents`` mid-flight
                # cannot have dropped the hold before this line lands it.
                if self.subagent_mgr is not None:
                    self.subagent_mgr.consume_report_hold(_batch_id, info.id)
                bp["done"] += 1
                _oc = info.outcome
                if _oc == "stopped":
                    bp["stopped"] += 1
                elif _oc == "failed":
                    bp["err"] += 1
                    if (info.error or "").startswith(NEVER_STARTED_MEMORY_PREFIX):
                        bp["not_run"] += 1
                else:
                    bp["ok"] += 1
                # Per-member model provenance in the PARENT-READ digest text: the
                # announce body the parent LLM consumes is built from
                # ok_lines/fail_lines, so surface each member's served
                # model inline there — rather than in a structured meta field
                # with no consumer.
                #
                # Print the SERVED model id only (no "(requested …)" qualifier):
                # `_res_model != _req_model` is NOT how the card decides a
                # downgrade — `isModelDowngrade` folds auto/default to "no pin"
                # and treats alias-vs-canonical / routing-prefix pairs as the
                # same model, so a raw inequality would print a false downgrade
                # on every member of a normal wave (default agent.model is
                # "auto"). Until this uses the same fold (or the registry
                # fold), show only the served id, and show nothing when there is
                # no served model — matching what the card renders in that case.
                # The value is caller-influenceable (spawn_run.model), so redact
                # it through the display context before it enters the digest
                # text broadcast to the dashboard/channels: credential-shaped
                # input must not reach metadata.
                _res_model = info.resolved_model or ""
                if _res_model:
                    _res_model, _ = redact_for_display(_res_model, redact_via_context)
                _model_tag = f" · model {_res_model}" if _res_model else ""
                # Exception-first digest content: failures/stops carry detail,
                # successes are one pointer line (full output stays on disk).
                if _oc == "completed":
                    # A run that kept its output after a generate failure is
                    # completed but partial; the digest must say so.
                    _kept = " (partial: backend failed to generate the final response)"
                    bp["ok_lines"].append(
                        f"— `{info.id}` ✅{_kept if info.partial else ''} {task_text[:80]}"
                        f"{_model_tag} · {usage}"
                        + (f"\n  → {result_path}" if result_path else "")
                        + (f"\n  {no_tools_note}" if no_tools_note else "")
                    )
                else:
                    bp["fail_lines"].append(
                        f"— `{info.id}` {status} {emoji} · {task_text[:80]}{_model_tag}\n"
                        f"  Usage: {usage}\n"
                        f"  {detail[:400]}{'…' if len(detail) > 400 else ''}"
                    )
                _last = bp["total"] > 0 and bp["done"] >= bp["total"]
                if not _last:
                    # Robustness: a wave member that failed AT SPAWN never
                    # reaches this consumer, so done can never hit total.
                    # Completion is decided by THIS batch's outstanding
                    # members only (running OR still queued behind the
                    # stagger gate) — an unrelated agent under the same
                    # parent must neither hold the digest hostage nor
                    # release it early.
                    #
                    # A member whose ``done`` flag has flipped but whose
                    # terminal report has not reached this consumer yet is
                    # OUTSTANDING too: ``batch_members_pending`` does not
                    # count it while its contribution to bp["done"] is still
                    # in flight, so without the reports-in-flight check a
                    # sibling landing in that window finalizes the wave early
                    # and the in-flight report then finalizes it again.
                    #
                    # The membership lookup ran before this block (see
                    # ``_no_batch_pending``); the in-flight read is synchronous
                    # and runs after this member released its own hold above.
                    try:
                        _last = bool(
                            self.subagent_mgr
                            and _no_batch_pending
                            and not self.subagent_mgr.batch_reports_in_flight(_batch_id)
                        )
                    except Exception:
                        _last = False
                if _last:
                    self._batch_progress.pop(_batch_id, None)
                    # Prune per-wave bookkeeping for ALL wave sizes (bounds
                    # _seen_batches / _batch_submitted growth).
                    try:
                        if self.subagent_mgr:
                            self.subagent_mgr.finalize_batch(_batch_id)
                    except Exception:
                        logger.debug("finalize_batch failed", exc_info=True)
                    try:
                        if self.dashboard_state:
                            self.dashboard_state.broadcast_ws(
                                "batch_finished",
                                {
                                    "batch_id": _batch_id,
                                    "slot": _injection_slot_name,
                                    "total": bp["total"],
                                    "ok": bp["ok"],
                                    "err": bp["err"],
                                    "stopped": bp["stopped"],
                                },
                            )
                    except Exception:
                        logger.debug("batch_finished broadcast failed", exc_info=True)
            if _batch_id:
                if _flush_only and bp["total"] <= 1:
                    # Single-member wave: nothing is ever held, and falling
                    # through would route the phantom per-agent announce.
                    return
                if bp["total"] > 1:
                    # ── Chunked wave delivery ── completed results feed the
                    # parent queue-style: every SUBAGENT_DIGEST_CHUNK_SIZE
                    # completions flush ONE digest chunk (an injection turn);
                    # the final member flushes the remaining partial chunk.
                    # This bounds each digest's size AND gives the parent
                    # incremental signal — one straggler does not withhold
                    # every sibling's result for its entire runtime.
                    #
                    # The count trigger alone cannot deliver that incremental
                    # signal for a wave smaller than the chunk size: _pending can
                    # never reach it, so wave close is the only flush.
                    # _flush_only is the LATENCY trigger the count
                    # lacks — the reaper's hold-deadline sweep forces the
                    # pending chunk out once results have been held too long.
                    _pending = bp["done"] - bp["flushed"]
                    _flush = _last or _flush_only or _pending >= SUBAGENT_DIGEST_CHUNK_SIZE
                    if not _flush:
                        # Held for the next chunk — the terminal WS event,
                        # tracker accounting, and stats above already ran;
                        # only the per-agent injection turn is suppressed.
                        # Restart safety: flag the member so the run loop SKIPS
                        # mark_delivered — its result is not in the parent's
                        # context yet, and a "delivered" tombstone would hide it
                        # from orphan reconciliation. If the gateway restarts
                        # mid-chunk, the orphan path finds these undelivered
                        # results and delivers a recovery digest; in normal
                        # operation they are marked delivered when their chunk
                        # flushes.
                        info._digest_held = True
                        # Hold clock for the reaper's hold-deadline sweep. Kept
                        # separate from _digest_held (the restart-safety flag the
                        # run loop reads) so the sweep never mutates that
                        # contract; cleared when this member's chunk fires.
                        # Monotonic, as the sweep's ``now`` is: a wall-clock step
                        # must not force a partial digest out, or hold one past
                        # its deadline.
                        info._digest_held_at = time.monotonic()
                        bp.setdefault("held_infos", []).append(info)
                        if _oc == "completed":
                            bp["held_ok_deliveries"].append(
                                SubagentDelivery(info.id, info.elapsed, info.credits)
                            )
                        elif getattr(info, "_report_owed", False) is True:
                            # A memory-wait expiry the store owes a report until
                            # it reaches the parent: the debt rides with this
                            # chunk, and the settle that delivers the chunk
                            # clears it. Held only here, a restart before the
                            # flush leaves it owed for the next start.
                            bp["held_ok_deliveries"].append(
                                SubagentDelivery(
                                    info.id, info.elapsed, info.credits, report_owed=True
                                )
                            )
                        logger.info(
                            "Subagent %s: completion held for digest chunk (%d/%d done)",
                            info.id,
                            bp["done"],
                            bp["total"],
                        )
                        return
                    # Chunk fires now. Do NOT settle the held members'
                    # delivery tombstones here — composition precedes routing,
                    # and marking "delivered" before the chunk is handed off
                    # would re-open the restart-loss window.
                    # Stash the ids on the flushing member: the run loop
                    # settles them only after _on_done (which includes the
                    # routing below) returns without raising.
                    info._digest_settle_deliveries = list(bp.get("held_ok_deliveries", []))
                    # These members are no longer held: stop the hold clock so
                    # the reaper's deadline sweep does not force a second flush
                    # for results this chunk already carries.
                    for _held in bp.get("held_infos", []):
                        _held._digest_held_at = 0.0
                    bp["held_infos"] = []
                    _failures = bp["fail_lines"]
                    _oks = bp["ok_lines"]
                    _digest_body = "\n".join(_failures + _oks)
                    if len(_digest_body) > 60_000:
                        _digest_body = _digest_body[:60_000] + "\n…(digest truncated)"
                    bp["chunks"] += 1
                    bp["flushed"] = bp["done"]
                    _chunk_k = bp["chunks"]
                    # Total chunks: full chunks + one final partial. Completion
                    # order fills chunks to exactly CHUNK_SIZE, so this is
                    # ceil(total / chunk_size) — but a NON-final chunk always
                    # has at least the wave-close chunk still to come, so it can
                    # never honestly label itself k/k. Without the +1 a
                    # deadline-forced flush on a small wave would announce
                    # "1/1 — 1 still running", telling the parent the wave is
                    # fully delivered while a member is outstanding.
                    _chunk_j = max(
                        _chunk_k if _last else _chunk_k + 1,
                        -(-bp["total"] // SUBAGENT_DIGEST_CHUNK_SIZE),
                    )
                    _footer = (
                        "Failures are listed first. Full outputs are on disk — "
                        "read the result paths on demand; do NOT re-run "
                        "completed agents."
                    )
                    if _last:
                        # Final chunk: release the spawn-discipline gate.
                        #
                        # The tally counts this wave's DIRECT members only.
                        # ``wave_has_live_nested_spawns`` reports whether a
                        # member of this wave has itself spawned work that is
                        # still running (its own independent batch, so not in
                        # this total). When it is, the completion wording is
                        # scoped to the direct members and states that their
                        # nested work reports on its own; the unconditional
                        # "This run is complete / All results delivered" claim
                        # is reserved for a wave with no live nested work. The
                        # check is read-only and never withholds the digest.
                        try:
                            _nested_live = bool(
                                self.subagent_mgr
                                and self.subagent_mgr.wave_has_live_nested_spawns(_batch_id)
                            )
                        except Exception:
                            _nested_live = False
                        # Members a memory wait ended are dropped work, not
                        # done work. After the tally sentence, which the card
                        # regexes anchor on. "Once memory eases": a member the
                        # spent episode ended at once is counted too, and a
                        # resubmit while that episode lasts is ended at once.
                        _not_run_line = (
                            f"{bp['not_run']} of the ❌ never started, so their "
                            f"work was not done: resubmit them once memory "
                            f"eases if it is still needed. "
                            if bp.get("not_run")
                            else ""
                        )
                        if _nested_live:
                            _completion_line = (
                                f"These {bp['total']} sub-agents finished: "
                                f"{bp['ok']} ✅ · {bp['err']} ❌ · "
                                f"{bp['stopped']} ⏹. {_not_run_line}"
                                f"Their results are below. "
                                f"NOTE: a sub-agent in this wave spawned further "
                                f"work that is still running; that nested work "
                                f"is tracked as its own wave and reports "
                                f"separately when it finishes — this digest does "
                                f"NOT cover it.\n"
                                f"Finish processing these results before "
                                f"spawning any follow-up sub-agents.\n"
                            )
                        else:
                            _completion_line = (
                                f"wave finished: "
                                f"{bp['ok']} ✅ · {bp['err']} ❌ · "
                                f"{bp['stopped']} ⏹ of {bp['total']} agents. "
                                f"{_not_run_line}"
                                f"All results delivered.\n"
                                f"This run is complete. Finish processing all "
                                f"results before spawning any follow-up "
                                f"sub-agents.\n"
                            )
                        announce = (
                            f"{SUBAGENT_BATCH_COMPLETION_PREFIX}\n"
                            f"Batch results {_chunk_k}/{_chunk_j} — "
                            f"{_completion_line}"
                            f"{_footer}\n\n{_digest_body}"
                        )
                        # This member's completion is delivered as the wave-close
                        # digest, not a per-agent row — stamp the digest's facts
                        # (tallies + chunk index) in place of the single-agent
                        # meta built above.
                        sub_meta = wave_final_meta(
                            chunk=_chunk_k,
                            chunks=_chunk_j,
                            ok=bp["ok"],
                            failed=bp["err"],
                            stopped=bp["stopped"],
                            total=bp["total"],
                        )
                    else:
                        # Non-final chunk: spawn-discipline guidance — the
                        # parent wakes mid-wave, so it must not start new
                        # spawns that would interleave with the batches still
                        # arriving from this run.
                        _remaining = max(0, bp["total"] - bp["done"])
                        # A deadline-forced flush is a STRAGGLER release, not a
                        # full chunk: say so, or the parent reads "still
                        # running" as normal progress and keeps waiting rather
                        # than deciding whether the remainder is worth waiting
                        # for.
                        _why = (
                            f"The results below were finished and held for "
                            f"{int(DIGEST_HOLD_SECS)}s+ while the remaining "
                            f"agent(s) ran, so they are being delivered early "
                            f"as a PARTIAL result set. Synthesize what you can "
                            f"now; if a remaining agent never reports, work "
                            f"with what you have or tell the user.\n"
                            if _flush_only
                            else ""
                        )
                        announce = (
                            f"{SUBAGENT_BATCH_COMPLETION_PREFIX}\n"
                            f"Batch results {_chunk_k}/{_chunk_j} — "
                            f"{bp['done']} of {bp['total']} delivered, "
                            f"{_remaining} still running.\n"
                            f"{_why}"
                            f"Process these results now, but do NOT spawn new "
                            f"sub-agents yet — more result batches from this "
                            f"run are still arriving, and spawning now will "
                            f"interleave with them.\n"
                            f"{_footer}\n\n{_digest_body}"
                        )
                        # Mid-wave chunk: progress facts (delivered/running), no
                        # tallies — mirrors the CHUNK regex the frontend demotes.
                        sub_meta = wave_chunk_meta(
                            chunk=_chunk_k,
                            chunks=_chunk_j,
                            delivered=bp["done"],
                            total=bp["total"],
                            running=_remaining,
                        )
                        # Reset per-chunk buffers for the next chunk. (On the
                        # final chunk bp was already popped from
                        # _batch_progress above — nothing to reset.)
                        bp["fail_lines"] = []
                        bp["ok_lines"] = []
                        bp["held_ok_deliveries"] = []

            # ── Route completion back to the originating session ──
            # Tab open        → that tab (a channel-born tab mirrors on to its channel)
            # Channel, no tab → channel thread + dashboard notification
            # Cron/no parent  → dashboard notification only

            _slot_name = _injection_slot_name
            if _parent_slot_name and self.dashboard_state:
                # Route the result through _run_chat for full streaming, tool
                # call visibility, and proper lifecycle. A channel-born tab
                # runs on the channel's own session, so the turn's mirror
                # carries the reply back to the thread — the raw-injection path
                # below is for parents with no tab to stream into.
                # Redact LLM-generated output before any external surface
                announce, _ = redact_exfiltration_urls(announce)
                announce, _ = redact_credentials(announce)
                body, _ = redact_exfiltration_urls(body)
                body, _ = redact_credentials(body)

                if _injection_slot:

                    # ── Fix 2 (B1): arm a one-shot post-fan-out synthesis turn ──
                    # When this is the LAST outstanding sub-agent for the parent,
                    # flag the slot so that once every completion has been
                    # processed and the queue drains, _run_chat fires ONE
                    # dedicated synthesis turn (see chat_runner drain/idle branch).
                    # The fire gate drops it when one completion turn carried the
                    # whole batch (slot._synthesis_completion_turns == 1).
                    # Ordering guarantees running_agents_for == [] here on the last
                    # agent (info.done set + _running_count decremented first).
                    # IN MEMORY ONLY, cheapest first, and with no await: the arm
                    # sits on the delivery path, so a store read here would queue
                    # every completion behind the writer, and an await would let
                    # the tab close or a sibling register under it. The store
                    # half (a sibling the gate still holds) is the fire gate's
                    # (chat_utils.synthesis_fire_verdict), which refuses while
                    # anything is queued; the other completions re-arm.
                    try:
                        _mgr = self.subagent_mgr
                        _arm_synthesis = (
                            _mgr is not None
                            and not _injection_slot._pending_synthesis
                            and not _flush_only
                            and info.id not in _injection_slot._subagents_inline_collected
                            and _mgr.running_agents_for(parent_key) == []
                            and not _mgr.has_in_memory_pending_work_for(
                                parent_key, exclude_id=info.id
                            )
                        )
                    except Exception:
                        _arm_synthesis = False  # error → don't arm (fail safe)
                    if _arm_synthesis:
                        _injection_slot._pending_synthesis = True

                    # ── Skip injection for blocking-tool-collected results ──
                    # spawn_sub_agents (blocking MCP tool) already delivered
                    # this result inline as a tool-call return value. Injecting
                    # it again would trigger a redundant _run_chat turn whose
                    # assistant response shadows any [OPTIONS:] buttons from the
                    # synthesis message. Mark delivered and return.
                    # NOTE: This check is placed BEFORE the inflight counter and
                    # busy-wait because at this point the blocking tool's
                    # mark-collected POST has already landed (the tool returns
                    # before its turn ends, and _subagent_done fires only after
                    # the agent's terminal report, which is after the tool has
                    # finished). However, if the slot is busy (turn still
                    # running) we must wait first, then re-check — see the
                    # second check after the busy-wait below.
                    if info.id in _injection_slot._subagents_inline_collected:
                        _injection_slot._subagents_inline_collected.discard(info.id)
                        # Disarm synthesis — the blocking tool already delivered
                        # all results and the model synthesized inline.
                        if not _injection_slot._subagents_inline_collected:
                            _injection_slot._pending_synthesis = False
                        logger.info(
                            "Subagent %s: skipping injection (already collected inline by spawn_sub_agents)",
                            info.id,
                        )
                        # The blocking tool's return value carried it.
                        info._report_delivered = True
                        return

                    # Transfer an accepted completion beyond the report task.
                    # The manager count survives slot removal and is the sole
                    # stop-fence ledger until consumption or durable discard.
                    _ownership_manager = self.subagent_mgr
                    _retain_ownership = getattr(
                        _ownership_manager, "retain_completion_delivery", None
                    )
                    _release_ownership = getattr(
                        _ownership_manager, "release_completion_delivery", None
                    )
                    _manager_owns_completion = [False]

                    def _retain_manager_ownership() -> None:
                        if _manager_owns_completion[0] or not callable(_retain_ownership):
                            return
                        _retain_ownership(parent_key or "", info.id)
                        _manager_owns_completion[0] = True

                    def _release_manager_ownership() -> None:
                        if not _manager_owns_completion[0] or not callable(_release_ownership):
                            return
                        _release_ownership(parent_key or "", info.id)
                        _manager_owns_completion[0] = False

                    _retain_manager_ownership()

                    def _note_completion_consumed(consumed: bool = True) -> None:
                        if consumed:
                            _release_manager_ownership()
                        else:
                            _retain_manager_ownership()

                    _discard_started = False

                    def _note_completion_discarded() -> None:
                        """Release ownership only after discard settlement is durable."""
                        nonlocal _discard_started
                        if _discard_started:
                            return
                        _discard_started = True
                        try:
                            owed = _injection_slot.take_pending_subagent_deliveries([announce])
                        except Exception:
                            logger.debug(
                                "Subagent %s: could not retire discarded delivery debt",
                                info.id,
                                exc_info=True,
                            )
                            return
                        manager = self.subagent_mgr
                        if not owed:
                            _note_completion_consumed()
                            return

                        def _retain(reason: str) -> None:
                            try:
                                sel().log_tool_invocation(
                                    session_key=parent_key or "",
                                    source="subagent",
                                    tool_name="subagent_completion_discard",
                                    outcome="retained",
                                    metadata={"subagent_id": info.id, "reason": reason},
                                )
                            except Exception:
                                logger.debug(
                                    "Subagent %s: retained-completion audit failed",
                                    info.id,
                                    exc_info=True,
                                )

                        if manager is None:
                            _retain("no_manager")
                            return

                        async def _settle_discarded_delivery() -> None:
                            try:
                                await manager.settle_queued_delivery(owed)
                                tombstone_ids = [
                                    delivery.agent_id
                                    for delivery in owed
                                    if not delivery.report_owed
                                ]
                                durable = await asyncio.to_thread(
                                    lambda: all(
                                        (read_tombstone(agent_id) or {}).get("cause") == "delivered"
                                        for agent_id in tombstone_ids
                                    )
                                )
                            except asyncio.CancelledError:
                                _retain("settlement_cancelled")
                                raise
                            except Exception:
                                logger.warning(
                                    "Subagent %s: discarded delivery settlement failed",
                                    info.id,
                                    exc_info=True,
                                )
                                _retain("settlement_failed")
                                return
                            if not durable:
                                _retain("tombstone_not_durable")
                                return
                            _note_completion_consumed()

                        writer = asyncio.create_task(_settle_discarded_delivery())
                        background_tasks = getattr(self.dashboard_state, "_background_tasks", None)
                        if isinstance(background_tasks, set):
                            background_tasks.add(writer)
                            writer.add_done_callback(background_tasks.discard)

                    # Fix 2 (B1) race guard: count this completion as an
                    # in-flight delivery from entry until it is handed off (turn
                    # launched or queued). The synthesis fire-gate in chat_runner
                    # requires this count to be zero, so a concurrently-finishing
                    # sibling that is still awaiting the current turn (busy path)
                    # can't let an earlier turn fire synthesis before this result
                    # is delivered. try/finally so a CancelledError can't leak it.
                    _injection_slot._subagent_deliveries_inflight += 1
                    try:
                        if _injection_slot_busy(_injection_slot):
                            # Slot is busy (or an injection is dispatched but
                            # not yet started) — wait for that task to finish,
                            # then inject. No visible queue card.
                            _current = _injection_slot.task
                            if _current is not None:
                                try:
                                    await asyncio.wait_for(
                                        asyncio.shield(_current),
                                        timeout=INJECTION_TIMEOUT,
                                    )
                                except asyncio.TimeoutError:
                                    pass  # Timed out waiting — slot still busy, will be queued below
                                except asyncio.CancelledError:
                                    _note_completion_consumed()
                                    raise  # Don't swallow cancellation of this coroutine
                                except Exception:
                                    pass  # Task failed — slot is now idle

                            # The slot may have closed while this callback waited.
                            if self.dashboard_state.get_slot(_slot_name) is not _injection_slot:
                                self._defer_queued_delivery(
                                    _injection_slot,
                                    announce,
                                    info,
                                    flush_only=_flush_only,
                                )
                                _note_completion_discarded()
                                return

                            # Re-check: another injection may have claimed the slot
                            # during the await above.
                            if _injection_slot_busy(_injection_slot):
                                # Check inline-collected before queuing — if the
                                # blocking tool already handled this result, don't
                                # queue it for a later redundant turn.
                                if info.id in _injection_slot._subagents_inline_collected:
                                    _injection_slot._subagents_inline_collected.discard(info.id)
                                    if not _injection_slot._subagents_inline_collected:
                                        _injection_slot._pending_synthesis = False
                                    logger.info(
                                        "Subagent %s: skipping queue " "(already collected inline)",
                                        info.id,
                                    )
                                    _note_completion_consumed()
                                    info._report_delivered = True
                                    return
                                logger.info(
                                    "Subagent %s: slot %s claimed by another injection, queuing",
                                    info.id,
                                    _slot_name,
                                )
                                # Each queued completion drains as its own turn
                                # (a system injection never merges), so it is
                                # one turn for the synthesis fire gate's count.
                                _injection_slot._synthesis_completion_turns += 1
                                # Bounded by the configured turn ceiling
                                # (chat_turn_timeout_secs, 14400s default):
                                # _run_chat's finally block drains slot._queue
                                # on any exit path.
                                # Carry the structured completion facts so the
                                # drained row is a card without re-parsing the
                                # prose; _start_next_queued_turn reads them.
                                _injection_slot.queue_insert(
                                    len(_injection_slot._queue),
                                    announce,
                                    kind=SUBAGENT_COMPLETION_KIND,
                                    meta={SUBAGENT_COMPLETION_META_KEY: sub_meta},
                                    on_consumed=_note_completion_consumed,
                                    on_discarded=_note_completion_discarded,
                                )
                                # Queuing is not delivery. The announce promises
                                # result paths the parent can read on demand, but
                                # it will not be in the parent's context until a
                                # turn drains it — a wait bounded only by the turn
                                # ceiling, so longer than the retention TTL. Owe
                                # the delivery tombstones to that drain instead of
                                # writing them now, or the reaper prunes
                                # result.txt while the promise is still queued
                                # and the parent is handed dead paths.
                                self._defer_queued_delivery(
                                    _injection_slot, announce, info, flush_only=_flush_only
                                )
                                self.dashboard_state.push_slots_update()
                                logger.info("Subagent %s → queued in %s", info.id, _slot_name)
                                return

                        # Slot is idle — re-check inline-collected (the
                        # blocking tool's mark-collected POST has now landed,
                        # since the tool returns before its owning turn ends).
                        if info.id in _injection_slot._subagents_inline_collected:
                            _injection_slot._subagents_inline_collected.discard(info.id)
                            if not _injection_slot._subagents_inline_collected:
                                _injection_slot._pending_synthesis = False
                            logger.info(
                                "Subagent %s: skipping injection after wait "
                                "(already collected inline by spawn_sub_agents)",
                                info.id,
                            )
                            _note_completion_consumed()
                            info._report_delivered = True
                            return

                        # Slot is idle — start _run_chat.
                        #
                        # This branch hands the digest off ASYNCHRONOUSLY: the
                        # turn is a task, and `_on_done` returns to
                        # `_report_terminal` while it is still pending — so a
                        # bare return here is a local routing success, not
                        # evidence the parent received anything. Owe
                        # the delivery bookkeeping to the turn's CONSUMPTION
                        # instead, through the same `_defer_queued_delivery`
                        # the queue branch uses: it records the debt (the
                        # completed member's own tombstone AND any held wave
                        # siblings) in the slot's content-keyed ledger, keyed
                        # on this announce, and flags `_delivery_queued` — so
                        # the run loop's `mark_delivered` and its digest-hold
                        # settle both become no-ops for this route, and the
                        # two settle paths cannot both fire.
                        #
                        # The task's own OUTCOME is deliberately not the
                        # signal: `_run_chat` returns NORMALLY on a signed-out
                        # CLI, a dead provider, exhausted retries and a first
                        # empty response — several of them after re-queueing
                        # the announce itself — so "the task finished cleanly"
                        # says nothing about delivery. Consumption does, and a
                        # failure before it re-queues the announce, whose drain
                        # claims this same content-keyed debt on the replay. An
                        # unconfirmed hand-off leaves the debt parked on
                        # purpose: a duplicate announce after a restart is
                        # visible to the parent and recoverable, a lost result
                        # is neither.
                        #
                        # Computed BEFORE the transfer (which detaches the held
                        # ids); stays False when there is nothing to owe — a
                        # failed or stopped solo member settles through its own
                        # failure tombstone, not this ledger. A memory-wait
                        # expiry is the exception: it has no tombstone, and the
                        # store owes its report until this turn consumes it.
                        _owes_delivery = bool(info._digest_settle_deliveries) or (
                            not _flush_only
                            and (
                                info.outcome == "completed"
                                or getattr(info, "_report_owed", False) is True
                            )
                        )
                        self._defer_queued_delivery(
                            _injection_slot, announce, info, flush_only=_flush_only
                        )
                        _consumed: list[bool] = [False]
                        _lifecycle_recovery_retained = False
                        _runner_entered = False

                        def _note_consumed(consumed: bool = True) -> None:
                            # False is a retraction: the first empty response
                            # re-queues this exact announce verbatim, so the
                            # delivery that counts has not happened yet.
                            _consumed[0] = consumed
                            _note_completion_consumed(consumed)

                        def _note_lifecycle_recovery_retained() -> None:
                            nonlocal _lifecycle_recovery_retained
                            from kiro_crew.dashboard.chat_utils import (
                                LIFECYCLE_RECOVERY_KIND,
                            )

                            _lifecycle_recovery_retained = any(
                                isinstance(item, dict)
                                and item.get("kind")
                                in (LIFECYCLE_RECOVERY_KIND, SUBAGENT_COMPLETION_KIND)
                                and item.get("_on_discarded") is _note_completion_discarded
                                for item in _injection_slot._queue
                            )

                        _run_kwargs: dict[str, Any] = {
                            "_on_consumed": _note_consumed,
                            "_on_discarded": _note_completion_discarded,
                            "_on_lifecycle_recovery_retained": (_note_lifecycle_recovery_retained),
                        }
                        # One completion turn for the synthesis fire gate's
                        # count, as in the queued branch above.
                        _injection_slot._synthesis_completion_turns += 1
                        _run_chat_coro = _run_chat(
                            self.dashboard_state,
                            _injection_slot,
                            announce,
                            _directive_user_origin=False,
                            # Structural provenance for the session crew log: the
                            # queued twin above carries SUBAGENT_COMPLETION_KIND.
                            _turn_actor="subagent",
                            **_run_kwargs,
                        )

                        async def _run_injected_completion() -> None:
                            nonlocal _runner_entered
                            _runner_entered = True
                            await _run_chat_coro

                        _injected_completion_coro = _run_injected_completion()
                        _task = asyncio.create_task(
                            bounded_chat_turn(
                                _injected_completion_coro,
                                state=self.dashboard_state,
                                slot=_injection_slot,
                            )
                        )
                        _injection_slot.task = _task
                        self.dashboard_state._background_tasks.add(_task)
                        _task.add_done_callback(self.dashboard_state._background_tasks.discard)

                        def _on_inject_done(t: asyncio.Task) -> None:  # type: ignore[type-arg]
                            if _injection_slot.task is t:
                                _injection_slot.task = None
                            if not _runner_entered:
                                _injected_completion_coro.close()
                                _run_chat_coro.close()
                                if t.cancelled():
                                    _note_completion_discarded()
                                    return
                            if not t.cancelled() and t.exception():
                                logger.error(
                                    "Subagent injection _run_chat failed: %s", t.exception()
                                )
                                if _lifecycle_recovery_retained:
                                    return
                                if self.subagent_mgr:
                                    _reason = str(t.exception())
                                    _reason, _ = redact_exfiltration_urls(_reason)
                                    _reason, _ = redact_credentials(_reason)
                                    self.subagent_mgr.notify_injection_failed(
                                        info,
                                        reason=_reason,
                                    )

                        _task.add_done_callback(_on_inject_done)
                        if _owes_delivery:
                            # Settle the owed tombstones only once the model has
                            # consumed this turn's prompt — the drain's own
                            # settlement path, reused verbatim. If the transfer
                            # above fell back (stubbed slot), the ledger holds
                            # no debt and the claim inside is an empty no-op.
                            _arm_queued_delivery_settlement(
                                self.dashboard_state,
                                _injection_slot,
                                _task,
                                [announce],
                                _consumed,
                            )
                        self.dashboard_state.push_slots_update()
                        logger.info("Subagent %s → _run_chat in %s", info.id, _slot_name)
                    finally:
                        _injection_slot._subagent_deliveries_inflight -= 1
                else:
                    logger.info(
                        "Subagent %s: parent slot %s gone, notification only",
                        info.id,
                        _slot_name,
                    )
                    # Only notify when slot is gone — active slots already show
                    # results in the Activity panel and chat.
                    self.dashboard_state.notify(
                        "subagent",
                        title,
                        body,
                        meta=self._notif_meta(parent_key),
                    )
                    # The notification is this route's whole delivery.
                    info._report_delivered = True
                return

            if parent_key and not parent_key.startswith(("cron:", "subagent:")):
                # Parent with no tab to stream into — inject silently into the
                # parent's ACP session, then deliver only the synthesized reply
                # to the conversation. Make up to _MAX_INJECT_ATTEMPTS attempts;
                # only a timeout starts the next one (_inject_with_retry has its
                # own ACP-error retry inside each attempt). Slack keeps its
                # dedicated rich posting; every other channel namespace
                # (Telegram, Discord, …) delivers through the governed transport
                # ladder — its stored channel value is not a Slack channel id,
                # so posting it through the Slack client can never reach the
                # user. A parent with neither a channel namespace nor a Slack
                # thread (a taskrunner: session, say) is still injected, but its
                # reply goes to no channel.
                assert self.sessions is not None
                _namespace = channel_namespace_of(parent_key)
                _via_transport = bool(_namespace) and _namespace != SLACK_NAMESPACE
                _slack_thread = self.sessions.get_thread(parent_key)
                if _slack_thread is None and is_legacy_slack_key(parent_key):
                    _slack_thread = parent_key
                _via_slack = _namespace == SLACK_NAMESPACE or bool(_slack_thread)
                if _via_transport:
                    _inject_label = _namespace
                elif _via_slack:
                    _inject_label = "Slack"
                else:
                    _inject_label = "session"
                # Snapshot the transport reply link BEFORE the injection retry
                # loop: the timeout path's sessions.reset() evicts the session's
                # in-memory origin link, the first rung _channel_reply_link
                # tries, so resolving after a retry could miss the
                # conversation's own thread/forum target.
                _reply_link = self._channel_reply_link(parent_key) if _via_transport else None
                _injected = False
                _inject_failure_reasons: list[str] = []
                _sleep_before_retry = False
                for _attempt in range(1, _MAX_INJECT_ATTEMPTS + 1):
                    if _sleep_before_retry:
                        await asyncio.sleep(2)
                        _sleep_before_retry = False
                    _acquired = False
                    _footer_client = None
                    try:
                        logger.debug(
                            "Subagent %s: %s injection attempt %d/%d into %s",
                            info.id,
                            _inject_label,
                            _attempt,
                            _MAX_INJECT_ATTEMPTS,
                            parent_key,
                        )
                        _memory_store = await session_store_for_turn(self.ctx_builder, parent_key)
                        client, is_new, _resumed = await self.sessions.get_or_create(parent_key)
                        _acquired = True
                        _footer_client = client
                        _provider = self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"
                        if self.ctx_builder:
                            # The completion is injected into the PARENT's
                            # conversation, so it reads the parent session's silo,
                            # resolved from that session's recorded binding. The
                            # child's own store is not the answer here: this turn
                            # continues the parent, and the reply it produces is
                            # consolidated into the parent's store.
                            msg, _ = await run_in_embed_pool(
                                self.ctx_builder.build_message,
                                announce,
                                is_new,
                                parent_key,
                                memory_store=_memory_store,
                                provider_type=_provider,
                                context_provider=client,
                                resumed=_resumed,
                            )
                        else:
                            msg = announce
                        response = await asyncio.wait_for(
                            _inject_with_retry(client, msg, parent_key, _inject_label),
                            timeout=INJECTION_TIMEOUT,
                        )
                        _injected = True  # LLM processed result; channel posting is best-effort

                        # Persisted at the per-attempt level, NOT inside the
                        # Slack branch below: a channel parent (Discord,
                        # Telegram) delivers via the transport ladder and skips
                        # that branch entirely, so persisting there dropped
                        # every non-Slack subagent turn from replay. It still
                        # runs BEFORE the Slack control is posted, so the
                        # control's staleness token names this turn rather than
                        # the one before it.
                        # Persist BEFORE the control below invites
                        # an answer to this turn: the token names
                        # this session's last written row, so an
                        # unwritten turn stamps the control with the
                        # PREVIOUS turn's position and the first
                        # click reads as already superseded.
                        if self.conv_log and not (
                            is_thread_temporary(parent_key) or is_thread_incognito(parent_key)
                        ):
                            try:
                                # Defense-in-depth: `announce` is composed from
                                # already-redacted parts plus identifiers such as
                                # `info.agent`; we re-redact before persisting to the
                                # dashboard replay (an external surface), mirroring the
                                # dashboard branch. `response` is fresh LLM output from
                                # stream_and_collect and is NOT yet redacted, so its
                                # redaction here is strictly required.
                                safe_announce, _ = redact_exfiltration_urls(announce)
                                safe_announce, _ = redact_credentials(safe_announce)
                                safe_response, _ = redact_exfiltration_urls(response or "")
                                safe_response, _ = redact_credentials(safe_response)
                                await save_conversation_turn_off_loop(
                                    self.conv_log,
                                    parent_key,
                                    safe_announce,
                                    safe_response,
                                    source_thread=parent_key,
                                    source_user="subagent",
                                    agent=_get_agent_for_session(parent_key),
                                )
                            except Exception:
                                logger.warning(
                                    "Failed to persist subagent turn for %s",
                                    parent_key,
                                    exc_info=True,
                                )
                        # Deliver only the LLM's synthesized response to the
                        # parent's own conversation. Non-Slack channels go
                        # through the governed transport ladder; on failure
                        # the dashboard notification below still fires.
                        if _via_transport and response:
                            await self._deliver_channel_reply(
                                parent_key, response, resolved_link=_reply_link
                            )
                        # Post only the LLM's synthesized response to Slack
                        try:
                            if response and _via_slack and self.slack and self._owner_id:
                                channel = (
                                    self.sessions.get_channel(parent_key) if self.sessions else None
                                ) or await self.slack.open_dm(self._owner_id)
                                if channel:
                                    # OPTIONS off the RAW text, then render: the
                                    # tag is plain text, so extracting it after
                                    # conversion made the controls hostage to
                                    # to_slack_mrkdwn's 39,000-char truncation.
                                    reply_text, options = extract_options(response)
                                    for part in render_for_slack(reply_text):
                                        await self.slack.post_message(channel, part, _slack_thread)
                                    try:
                                        elapsed = (
                                            info.elapsed
                                            if info.elapsed > 0
                                            else (time.monotonic() - info.started)
                                        )
                                        footer_blocks, footer_text = build_timing_footer(
                                            elapsed,
                                            _footer_client,
                                        )
                                        if options:
                                            _sub_token = await asyncio.to_thread(
                                                mint_options_token,
                                                self.dashboard_state,
                                                parent_key,
                                            )
                                            footer_blocks.extend(
                                                build_options_blocks(
                                                    options, staleness_token=_sub_token
                                                )
                                            )
                                        _footer_ts = await self.slack.post_blocks(
                                            channel,
                                            footer_blocks,
                                            footer_text,
                                            _slack_thread,
                                        )
                                        self._remember_options(
                                            parent_key,
                                            channel,
                                            _footer_ts,
                                            options,
                                            footer_blocks,
                                            footer_text,
                                        )
                                    except Exception:
                                        logger.debug(
                                            "Failed to post timing footer for %s",
                                            parent_key,
                                            exc_info=True,
                                        )
                        except Exception:
                            logger.exception(
                                "Subagent %s: Slack posting failed (injection succeeded)",
                                info.id,
                            )

                        logger.info(
                            "Subagent %s → %s session %s", info.id, _inject_label, parent_key
                        )
                        break
                    except asyncio.TimeoutError:
                        _inject_failure_reasons.append(
                            f"attempt {_attempt} timed out after {int(INJECTION_TIMEOUT)}s"
                        )
                        logger.warning(
                            "Subagent %s: %s injection attempt %d/%d timed out after %.0fs",
                            info.id,
                            _inject_label,
                            _attempt,
                            _MAX_INJECT_ATTEMPTS,
                            INJECTION_TIMEOUT,
                        )
                        if _acquired:
                            try:
                                await self.sessions.reset(parent_key)
                            except Exception:
                                logger.debug(
                                    "Failed to reset %s after channel injection timeout",
                                    parent_key,
                                    exc_info=True,
                                )
                        if _attempt < _MAX_INJECT_ATTEMPTS:
                            _sleep_before_retry = True
                    except Exception as exc:
                        _inject_failure_reasons.append(f"attempt {_attempt} failed: {exc}")
                        logger.exception("Subagent %s %s injection failed", info.id, _inject_label)
                        break
                    finally:
                        if _acquired:
                            try:
                                await self.sessions.cancel_current(parent_key)
                            except Exception:
                                logger.debug(
                                    "Failed to cancel parent prompt for %s",
                                    info.id,
                                    exc_info=True,
                                )
                            try:
                                self.sessions.release(parent_key)
                            except Exception:
                                logger.exception("Failed to release session %s", parent_key)

                if not _injected:
                    _last_failure_reason = "; ".join(_inject_failure_reasons)
                    _last_failure_reason, _ = redact_exfiltration_urls(_last_failure_reason)
                    _last_failure_reason, _ = redact_credentials(_last_failure_reason)
                    logger.error(
                        "Subagent %s: all %d %s injection attempts failed: %s",
                        info.id,
                        _MAX_INJECT_ATTEMPTS,
                        _inject_label,
                        _last_failure_reason,
                    )
                    if self.subagent_mgr:
                        self.subagent_mgr.notify_injection_failed(
                            info,
                            reason=_last_failure_reason,
                        )
                # Dashboard notification
                if self.dashboard_state:
                    self.dashboard_state.notify(
                        "subagent",
                        title,
                        body,
                        meta=self._notif_meta(parent_key),
                    )
                return

            # Cron parent — inject result back into the cron session.
            # Track pending injections to avoid resetting the session while
            # other subagents are queued behind the per-session semaphore.
            if parent_key.startswith("cron:"):
                self._cron_injecting[parent_key] = self._cron_injecting.get(parent_key, 0) + 1
                assert self.sessions is not None
                acquired = False
                cron_response: str | None = None
                try:
                    _memory_store = await session_store_for_turn(self.ctx_builder, parent_key)
                    client, is_new, _resumed = await self.sessions.get_or_create(parent_key)
                    acquired = True
                    _provider = self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"
                    if self.ctx_builder:
                        # Same rule as the interactive injection above: the turn
                        # continues the PARENT conversation, so it reads the parent
                        # session's silo from that session's recorded binding.
                        msg, _ = await run_in_embed_pool(
                            self.ctx_builder.build_message,
                            announce,
                            is_new,
                            parent_key,
                            memory_store=_memory_store,
                            provider_type=_provider,
                            context_provider=client,
                            resumed=_resumed,
                        )
                    else:
                        msg = announce
                    cron_response = await asyncio.wait_for(
                        _inject_with_retry(client, msg, parent_key, "cron"),
                        timeout=INJECTION_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "Subagent %s: cron injection timed out after %.0fs",
                        info.id,
                        INJECTION_TIMEOUT,
                    )
                    try:
                        await self.sessions.reset(parent_key)
                    except Exception:
                        logger.debug(
                            "Failed to reset %s after cron injection timeout",
                            parent_key,
                            exc_info=True,
                        )
                    if self.subagent_mgr:
                        self.subagent_mgr.notify_injection_failed(
                            info,
                            reason=f"injection timed out after {int(INJECTION_TIMEOUT)}s",
                        )
                except Exception:
                    logger.exception("Subagent %s cron injection failed", info.id)
                    # Swallowed with no failure notice, so the record is the only
                    # place that says the parent was never told.
                    info._report_undelivered = True
                finally:
                    if acquired:
                        try:
                            await self.sessions.cancel_current(parent_key)
                        except Exception:
                            logger.debug(
                                "Failed to cancel parent prompt for cron %s", info.id, exc_info=True
                            )
                        try:
                            self.sessions.release(parent_key)
                        except Exception:
                            logger.exception("Failed to release session %s", parent_key)
                    self._cron_injecting[parent_key] = self._cron_injecting.get(parent_key, 1) - 1
                    if self._cron_injecting[parent_key] <= 0:
                        self._cron_injecting.pop(parent_key, None)
                if cron_response:
                    cron_response, _ = redact_exfiltration_urls(cron_response)
                    cron_response, _ = redact_credentials(cron_response)
                    body = f"{body}\n\n{cron_response}"
                    logger.info("Subagent %s → cron session %s", info.id, parent_key)
                    # also deliver the synthesized response to the job's own
                    # surfaces. honor the parent cron job's silent flag too:
                    # info.silent is never set from the cron's silent setting for
                    # spawn_run sub-agents, so a silent cron would otherwise still
                    # post every subagent-completion turn.
                    try:
                        await self._deliver_cron_response(
                            parent_key,
                            cron_response,
                            silent=info.silent or self._cron_job_is_silent(parent_key),
                        )
                    except Exception:
                        logger.exception(
                            "Subagent %s: failed to deliver cron response",
                            info.id,
                        )
                # Reset only when no subagents running or QUEUED AND no
                # injections pending. Queued spawns (behind the concurrency /
                # stagger gate) have no SubagentInfo in `running` yet — a
                # sibling completing while the rest of the wave is still
                # queued must not reset the parent out from under them.
                still_running = self.subagent_mgr and (
                    any(
                        a.parent_session_key == parent_key and a.id != info.id
                        for a in self.subagent_mgr.running
                    )
                    or await _subagent_queued_count(self.subagent_mgr, parent_key) > 0
                )
                still_injecting = self._cron_injecting.get(parent_key, 0) > 0
                if not still_running and not still_injecting:
                    try:
                        await self.sessions.reset(parent_key)
                        logger.info(
                            "Cron session %s: last subagent done, session reset", parent_key
                        )
                        # reset succeeded → reaper no longer needs the
                        # registered ephemeral key. Clear inside try so a failed
                        # reset leaves the key registered (ephemeral session may
                        # still be alive — reaper must be able to target it).
                        # parent_key is "cron:{job_id}" (persistent) or
                        # "cron:{job_id}:{run_id}" (ephemeral); job_id is the
                        # second colon-separated segment in both cases. Exact-key
                        # tracking means an older deferred reset removes only its
                        # own key even after a newer run registers another one,
                        # without stripping the reaper/ownership fence for either.
                        cron_svc = getattr(self, "cron_svc", None)
                        if cron_svc is not None:
                            parts = parent_key.split(":", 2)
                            if len(parts) >= 2:
                                cron_svc.clear_active_session_key(parts[1], parent_key)
                    except Exception:
                        logger.exception(
                            "Cron session %s: reset failed after last subagent", parent_key
                        )

            # Dashboard notification
            if self.dashboard_state and not info.silent:
                self.dashboard_state.notify(
                    "subagent",
                    title,
                    body,
                    meta=self._notif_meta(parent_key),
                )
            if not parent_key.startswith("cron:"):
                logger.info("Subagent %s → notification only (parent=%s)", info.id, parent_key)
                # No conversation to inject into: the notification is this
                # route's whole delivery. A cron parent acknowledges in
                # ``_inject_with_retry`` instead, and only when it lands.
                info._report_delivered = True

        assert self.sessions is not None
        assert self.ctx_builder is not None

        def _is_yolo() -> bool:
            return safety_override().is_active()

        def _spawn_slot_resolver(request_id: str) -> str:
            """Resolve slot from spawn request_id (spawn:{agent_id})."""
            agent_id = request_id.removeprefix("spawn:")
            info = self.subagent_mgr.get(agent_id) if self.subagent_mgr is not None else None
            # A workflow step's spawn resolves to the run's origin chat.
            slot = _spawn_parent_slot(
                info.parent_session_key if info else "",
                getattr(self.dashboard_state, "workflow_service", None),
            )
            logger.info(
                "_spawn_slot_resolver: rid=%s agent_id=%s info=%s slot=%s",
                request_id,
                agent_id,
                info is not None,
                slot,
            )
            return slot

        _approve_subagent = self._interactive_approval(
            "subagent", slot_resolver=_spawn_slot_resolver
        )
        # A SECOND instance of the same callback, differing only in the
        # unreachable behaviour. It is a separate closure because the one above is
        # also wired as ``on_tool_approval``: a mid-run tool prompt has no
        # terminal path that could report the refusal, so raising there would turn
        # a recoverable park into a lost turn.
        _approve_spawn_gate = self._interactive_approval(
            "subagent", slot_resolver=_spawn_slot_resolver, raise_when_unreachable=True
        )

        async def _spawn_approve(
            request_id: str, description: str, parent_session_key: str = ""
        ) -> bool:
            # Channel-side delivery FIRST. A spawn parented on
            # a live channel conversation (Telegram, …) is best answered where the
            # human already is, with that channel's own Approve/Deny/Trust
            # keyboard. The seam returns True/False when the channel surfaced the
            # prompt and got a press; None means no channel hook owns this session,
            # or the hook could not surface it here — either way, fall through to
            # the unchanged Slack-DM/dashboard gate below (which still raises
            # SpawnApprovalUnreachable when no surface is attached).
            channel_decision = await deliver_spawn_approval(
                request_id, description, parent_session_key
            )
            if channel_decision is not None:
                return channel_decision
            event = LLMEvent(kind="permission_request", request_id=request_id, title=description)
            return await _approve_spawn_gate(event, parent_session_key)

        # Debounced slots push: keep slots[].subagents_running and
        # slots[].subagents_queued live for every
        # SSE consumer (composer busy affordance, Board "working" lane, and
        # external readers of the slots stream). Without this, the field is
        # only fresh on a full GET — serialize_slots() computes it at call
        # time but nothing pushed on sub-agent lifecycle transitions. The
        # 0.2s coalesce window collapses batch spawns into one push. Covers
        # the reaper too: _force_reap fires subagent_done through the same
        # on_event path.
        _slots_push_pending = False

        def _flush_slots_push() -> None:
            nonlocal _slots_push_pending
            _slots_push_pending = False
            if self.dashboard_state:
                self.dashboard_state.push_slots_update()

        def _schedule_slots_push() -> None:
            nonlocal _slots_push_pending
            if _slots_push_pending:
                return
            _slots_push_pending = True
            asyncio.get_running_loop().call_later(0.2, _flush_slots_push)

        def _tab_queued_depth(parent_key: str, slot_name: str, extra: dict) -> object:
            """The queued depth a ``subagent_queued`` frame carries: its tab's sum.

            Several parents can route to one tab (a cron tab's ``cron:<job>:<run>``
            and ``cron:<job>:<agent>`` runs). The client reducer sets the tab's
            count from each frame, and the slot list sums the published depths by
            tab, so a frame carrying only its own parent's depth would disagree
            with the next slots push and the count would flip between the two.
            The frame therefore carries this parent's depth plus every other
            parent's published depth on the same tab. A non-int depth is passed
            through unchanged for the client to ignore.
            """
            own = extra.get("queued")
            if not isinstance(own, int) or isinstance(own, bool) or not self.subagent_mgr:
                return own
            total = max(own, 0)
            for other, depth in self.subagent_mgr.published_queued_depths().items():
                if other != parent_key and _event_slot(other) == slot_name:
                    total += depth
            return total

        async def _subagent_event(etype: str, info: SubagentInfo, extra: dict) -> None:
            if not self.dashboard_state:
                return
            slot_name = _event_slot(info.parent_session_key)
            base = {"id": info.id, "slot": slot_name}
            # Batch identity rides every frame when present so the UI can
            # group/aggregate a wave without a lookup table. (Type guard:
            # test doubles pass MagicMock infos.)
            _ebid = getattr(info, "batch_id", "")
            if isinstance(_ebid, str) and _ebid:
                base["batch_id"] = _ebid
            if etype == "subagent_queued":
                extra = {
                    **extra,
                    "queued": _tab_queued_depth(info.parent_session_key, slot_name, extra),
                }
            if etype == "subagent_injection_failed":
                # Show error in UI + queue for LLM context on next turn.
                slot = self.dashboard_state.get_slot(slot_name)
                if slot is not None and getattr(slot, "window_pending", False) is True:
                    # A parent tab restored as a sidebar row loads its window
                    # before the card is appended, with the read off the loop.
                    await load_window_off_loop(slot)
                    slot = self.dashboard_state.get_slot(slot_name)
                if slot:
                    task_preview = redact_and_truncate(info.task or "", 100)
                    error_text, _ = redact_exfiltration_urls(extra.get("error", "timed out"))
                    error_text, _ = redact_credentials(error_text)
                    # The visible transcript card must state the run's real
                    # outcome, same as the queued LLM copy: this event fires for
                    # every terminal state whose report could not be injected,
                    # not only successful completions.
                    outcome_line = _injection_notice_outcome(info)
                    usage = format_subagent_usage(info.credits, info.elapsed)
                    slot.append(
                        "assistant",
                        f"{SUBAGENT_COMPLETION_PREFIX}\n"
                        f"Agent `{info.id}` ❌\n"
                        f"Task: {task_preview}\n\n"
                        f"Usage: {usage}\n\n"
                        f"Error: {error_text}\n"
                        f"⚠️ Result delivery failed — {outcome_line}",
                        "msg msg-a",
                        meta={
                            SUBAGENT_COMPLETION_META_KEY: single_completion_meta(
                                agent_id=info.id,
                                outcome=OUTCOME_FAILED,
                                agent_name=info.agent or "",
                                task=task_preview,
                                requested_model=info.requested_model or info.model or "",
                                resolved_model=info.resolved_model or "",
                            )
                        },
                    )
                    # Queue failure for LLM context drain
                    failure_msg = extra.get("failure_msg", "")
                    if failure_msg:
                        failure_msg, _ = redact_exfiltration_urls(failure_msg)
                        failure_msg, _ = redact_credentials(failure_msg)
                        slot._pending_subagent_failures.append(failure_msg)
                        # A completed run keeps no delivered tombstone while its
                        # report is undelivered, and the drain that hands this
                        # notice to the parent writes it, keyed on the text.
                        if info.outcome == "completed":
                            try:
                                slot.note_pending_subagent_delivery(
                                    failure_msg,
                                    [SubagentDelivery(info.id, info.elapsed, info.credits)],
                                )
                            except Exception:
                                logger.debug(
                                    "Subagent %s: could not record the notice's delivery",
                                    info.id,
                                    exc_info=True,
                                )
                    self.dashboard_state.push_slots_update()
                    logger.warning(
                        "Injected timeout error for subagent %s into slot %s", info.id, slot_name
                    )
                self.dashboard_state.broadcast_ws(etype, {**base, **extra})
            elif etype == "subagent_chunk":
                # Heavy data — only to subscribed clients. At scale (>threshold
                # active agents) the coalescer absorbs it into the ~1s
                # subagent_batch_chunks frame instead of a per-event frame.
                if self._subagent_coalescer().handle(etype, {**base, **extra}):
                    return
                self.dashboard_state.broadcast_ws_subagent_subscribers(etype, {**base, **extra})
            else:
                # Lightweight status events — broadcast to all. High-frequency
                # deltas (tool/stalled/retrying) coalesce at scale into ONE
                # subagent_batch_update frame per tick; lifecycle events
                # (spawn/done/recovering/batch_*) always pass through.
                #
                # A child the reaper flagged stalled does not hold the parent's
                # user messages (``subagents_hold_user_messages``), yet nothing
                # else starts that drain: a message queued BEFORE the flag is set
                # stays queued until the next send or the wall-clock reap. Kick
                # the parent slot's drain here — before the coalescer can absorb
                # the UI frame — so the parked message starts now. The drain
                # re-reads the hold under the slot lock, so a live sibling keeps
                # the queue parked and a running slot is left alone.
                if etype == "subagent_stalled" and extra.get("stalled"):
                    _drain = asyncio.ensure_future(
                        drain_idle_parent_queue_for_stall(
                            self.dashboard_state, info.parent_session_key
                        )
                    )
                    self.dashboard_state._background_tasks.add(_drain)
                    _drain.add_done_callback(self.dashboard_state._background_tasks.discard)
                if self._subagent_coalescer().handle(etype, {**base, **extra}):
                    return
                self.dashboard_state.broadcast_ws(etype, {**base, **extra})
                # subagents_running flips truth value exactly at spawn/done, and
                # subagents_queued changes with every queued-depth frame — push
                # (debounced) so slots-stream consumers stay live, and a client
                # that missed a depth frame is corrected by the next push.
                if etype in ("subagent_spawn", "subagent_done", "subagent_queued"):
                    _schedule_slots_push()

        async def _orphan_notify(
            parent_session: str,
            msg: str,
            meta: dict | None = None,
            delivery: SubagentDelivery | None = None,
        ) -> bool | str:
            """Inject an orphan notification into the parent dashboard slot.

            Mirrors the subagent_injection_failed delivery: visible transcript
            card + queue into ``slot._pending_subagent_failures`` so the LLM
            drains it (as a digest with any other pending failures) on its next
            turn. Returns False when the slot no longer exists so the manager
            falls through to the owner-DM path. ``msg`` is redacted by the
            manager before delivery; re-redact defensively anyway.

            ``meta`` carries the structured completion facts so the
            orphan row renders as a card without re-parsing its prose header.
            *delivery* is the tombstone the notice owes. It is recorded on the
            slot's ledger under the queued text, for the drain of that text to
            write, and ``ORPHAN_NOTICE_OWED`` tells the manager it was taken.
            """
            # Deliver to whichever tab shows the parent conversation, including a
            # channel-born one; only a parent with no tab wants the owner DM.
            slot_name = dashboard_slot_key(parent_session)
            if not self.dashboard_state or not slot_name:
                return False
            slot = self.dashboard_state.get_slot(slot_name)
            if slot is not None and getattr(slot, "window_pending", False) is True:
                # A parent tab restored as a sidebar row loads its window before
                # the notice is appended, with the read off the loop.
                await load_window_off_loop(slot)
                slot = self.dashboard_state.get_slot(slot_name)
            if not slot:
                return False
            safe_msg, _ = redact_exfiltration_urls(msg)
            safe_msg, _ = redact_credentials(safe_msg)
            slot.append(
                "assistant",
                safe_msg,
                "msg msg-a",
                meta={SUBAGENT_COMPLETION_META_KEY: meta} if meta else None,
            )
            slot._pending_subagent_failures.append(safe_msg)
            owed = False
            if delivery is not None:
                try:
                    slot.note_pending_subagent_delivery(safe_msg, [delivery])
                    owed = True
                except Exception:
                    logger.debug(
                        "Could not record orphan %s's notice delivery",
                        delivery.agent_id,
                        exc_info=True,
                    )
            self.dashboard_state.push_slots_update()
            logger.info("Orphan notification injected into slot %s", slot_name)
            return ORPHAN_NOTICE_OWED if owed else True

        async def _orphan_dm(msg: str) -> bool:
            """Owner-DM fallback for orphan notifications (bell + Slack DM)."""
            safe_msg, _ = redact_exfiltration_urls(msg)
            safe_msg, _ = redact_credentials(safe_msg)
            delivered = False
            if self.dashboard_state:
                try:
                    self.dashboard_state.notify(
                        "subagent", "Sub-agent orphaned by restart", safe_msg
                    )
                    delivered = True
                except Exception:
                    logger.debug("Orphan bell notification failed", exc_info=True)
            try:
                if self.slack and self._owner_id:
                    ch = await self.slack.open_dm(self._owner_id)
                    if ch:
                        await self.slack.post_message(ch, safe_msg)
                        delivered = True
            except Exception as exc:
                logger.warning("Failed to send orphan notification to Slack DM: %s", exc)
            return delivered

        self.subagent_mgr = SubagentManager(
            sessions=self.sessions,
            ctx_builder=self.ctx_builder,
            on_done=_subagent_done,
            max_concurrent=resolve_max_subagents(self._cfg),
            default_turn_limit=self._cfg.agent.subagent_max_turns,
            default_timeout=self._cfg.agent.subagent_timeout_secs,
            stall_idle_secs=self._cfg.agent.subagent_stall_idle_secs,
            on_tool_approval=_approve_subagent,
            on_spawn_approval=_spawn_approve,
            is_yolo=_is_yolo,
            on_event=_subagent_event,
            on_orphan_notify=_orphan_notify,
            on_orphan_dm=_orphan_dm,
            completion_keep=self._cfg.agent.completion_keep,
            completion_keep_chars=self._cfg.agent.completion_keep_chars,
            # Rows that survived the restart are claimed by the manager's pump,
            # which the reaper start below kicks as soon as the loop yields --
            # that yield is ``run()``'s memory barrier. Hold the pump until
            # ``_start_subagent_dispatch_after_memory_ready`` opens it.
            defer_queue_dispatch=True,
        )
        # A parent that ends takes its children with it, on every backend. The
        # session lifecycle owns the boundary and drives both halves at each of its
        # parent-end paths, so no surface that closes, resets, discards or retires a
        # conversation needs a cancel call of its own. The manager is passed whole
        # rather than as two bound methods because the halves have to agree about
        # which runs they are talking about.
        self.sessions.set_child_teardown_handler(self.subagent_mgr)
        self.subagent_mgr.start_reaper()

    # gateway_runtime/admission.py
    _start_subagent_dispatch_after_memory_ready = (
        _admission._start_subagent_dispatch_after_memory_ready
    )

    _register_child_liveness = _admission._register_child_liveness

    _start_adaptive_controller = _admission._start_adaptive_controller

    _wire_overload_health = _admission._wire_overload_health

    _subagent_dependency_coordinator = _admission._subagent_dependency_coordinator

    _ensure_subagent_coordinator = _admission._ensure_subagent_coordinator

    _unwire_overload_health = _admission._unwire_overload_health

    _wire_runner_admission = _admission._wire_runner_admission

    _subscribe_runner_admission = staticmethod(_admission._subscribe_runner_admission)

    _attach_runner_admission_consumers = _admission._attach_runner_admission_consumers

    _runner_admission_store_ready = _admission._runner_admission_store_ready

    _unwire_runner_admission = _admission._unwire_runner_admission

    _start_dashboard_workers_after_memory_ready = (
        _admission._start_dashboard_workers_after_memory_ready
    )

    # gateway_runtime/memory_lifecycle.py
    _initialize_memory_worker = _memory_lifecycle._initialize_memory_worker

    _stop_memory_startup = _memory_lifecycle._stop_memory_startup

    _start_memory_after_ready = _memory_lifecycle._start_memory_after_ready

    _schedule_memory_preparation = _memory_lifecycle._schedule_memory_preparation

    _wait_for_memory_preparation = _memory_lifecycle._wait_for_memory_preparation

    _repair_member_memory_once = _memory_lifecycle._repair_member_memory_once

    _repair_member_memory = _memory_lifecycle._repair_member_memory

    _start_embeddings = _memory_lifecycle._start_embeddings

    _auto_migrate_memory = _memory_lifecycle._auto_migrate_memory

    _set_memory_migrated = _memory_lifecycle._set_memory_migrated

    # ------------------------------------------------------------------
    # MCP Gateway
    # ------------------------------------------------------------------

    # gateway_runtime/mcp_broker.py
    _schedule_mcp_launch_approval_persist = _mcp_broker._schedule_mcp_launch_approval_persist

    _init_mcp_gateway = _mcp_broker._init_mcp_gateway

    #: Floor on the gap between timed pre-resolve passes. ``refresh_hours = 0``
    #: legitimately means "always stale" for the freshness check, but it must not
    #: turn the timer into a spin loop that reinstalls continuously.
    _MCP_RESOLVE_MIN_SLEEP_SECS = 300.0

    # gateway_runtime/mcp_broker.py
    _mcp_resolve_refresh_secs = _mcp_broker._mcp_resolve_refresh_secs

    _mcp_resolve_prefetch_loop = _mcp_broker._mcp_resolve_prefetch_loop

    _prefetch_mcp_resolutions = _mcp_broker._prefetch_mcp_resolutions

    _refresh_mcp_resolutions = _mcp_broker._refresh_mcp_resolutions

    _stop_mcp_broker = _mcp_broker._stop_mcp_broker

    _apply_mcp_gateway_enabled = _mcp_broker._apply_mcp_gateway_enabled

    _apply_mcp_stub = _mcp_broker._apply_mcp_stub

    _wire_mcp_gateway_dashboard = _mcp_broker._wire_mcp_gateway_dashboard

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Auto-update
    # ------------------------------------------------------------------

    @update_ownership.owning(update_ownership.Step.RESTART)
    async def _restart_after_update(self, respawn: Callable[[], str]) -> None:
        """Save state and restart only after callback/refusal work is durable.

        ``respawn`` is loaded before apply can replace the environment. If the
        pre-fence drain cannot finish, retain it and retry the restart in five
        minutes with admission reopened; never force through accepted work.

        The interpreter is established BEFORE any of that. When it is missing, this
        returns without saving, fencing or draining, because an exec that cannot
        succeed must not be reached after every session has been closed.

        Owns a missing bundle while it runs (``update_ownership``), so the
        stale-asset watchdog does not race its teardown. A deferral while
        callback work drains is owned for a while too, from the first deferral
        on (a retry does not extend it), so the watchdog does not force the
        restart it just put off.
        """
        logger.info("Update applied, preparing a callback-safe gateway restart")
        self._pending_update_respawn = respawn
        launcher = await asyncio.to_thread(resolve_restart_launcher)
        exe = await asyncio.to_thread(respawn) if launcher is None else None
        # Off-loop for the same reason as the two resolvers above: both predicates
        # are metadata syscalls against a pathname this process does not control,
        # and an interpreter on a stalled network mount would freeze every gateway
        # task -- including the heartbeat -- rather than one restart.
        usable = launcher is not None
        if not usable and exe:
            usable = await asyncio.to_thread(platform_compat.execv_target_available, exe)
        if not usable:
            # No usable interpreter: the apply pruned the tree this process was
            # running from. RETURN BEFORE saving, fencing or draining. Reaching
            # the exec with no interpreter closes every session first and then
            # raises ENOENT. Admission itself does come back --
            # ``_finish_auto_update_apply`` resumes it -- but the sessions
            # ``close_all()`` tore down do not, and ``_pending_update_respawn``
            # is cleared just before the exec, so nothing retries: what survives
            # is a gateway with no sessions, running a different version from the
            # install on disk. Deferring here keeps the sessions instead, and
            # ``_pending_update_respawn`` stays set so
            # ``_retry_pending_update_restart`` finishes the update once an
            # operator repairs the install.
            # Not a deferral the watchdog waits out: with the tree pruned the
            # bundle is gone too, and its exit lets the supervisor relaunch
            # through its own command, which a retry here cannot do. So one an
            # earlier drain armed ends here too.
            update_ownership.clear_restart_deferral()
            self._update_apply_deferred = True
            logger.error(
                "Update applied but restart deferred: no usable interpreter. "
                "Restore the interpreter and this retries itself -- the retry "
                "re-resolves it, so no configuration change is needed."
            )
            if self.dashboard_state:
                self.dashboard_state.push_update_progress(
                    "restarting",
                    "Update applied — restart needs a usable interpreter",
                )
            return
        # ONE restart sequence per process: the dashboard's ``_restart_gateway``
        # claims the same flag, and two sequences must never both drain sessions
        # and race separate successors. A restart already under way owns the
        # exec; this one stays pending and retries if that one is refused.
        state = self.dashboard_state
        if state is not None and getattr(state, "_gateway_restart_in_progress", False) is True:
            self._update_apply_deferred = True
            logger.info("A gateway restart is already in progress; the update restart waits")
            return
        if state is not None:
            state._gateway_restart_in_progress = True
        try:
            # Through the class, not ``self``: the restart is also driven with a
            # stand-in ``self`` that carries only the state it reads.
            await GatewayOrchestrator._restart_after_update_claimed(self, launcher, exe)
        finally:
            if state is not None:
                state._gateway_restart_in_progress = False

    # gateway_runtime/install_repair.py
    _DEP_INSTALL_TIMEOUT_SECS = _install._DEP_INSTALL_TIMEOUT_SECS

    _KIRO_CLI_VERSION_TIMEOUT_SECS = _install._KIRO_CLI_VERSION_TIMEOUT_SECS

    _STARTUP_CHILD_REAP_SECS = _install._STARTUP_CHILD_REAP_SECS

    _kill_startup_child = staticmethod(_install._kill_startup_child)

    _reap_startup_child = classmethod(_install._reap_startup_child)

    _check_missing_deps = _install._check_missing_deps

    _check_console_script = _install._check_console_script

    _schedule_console_script_repair = _install._schedule_console_script_repair

    _warn_if_kiro_cli_outdated = _install._warn_if_kiro_cli_outdated

    # gateway_runtime/update_schedule.py
    _in_flight_work_counts = _upd_sched._in_flight_work_counts

    _count_in_flight_work = _upd_sched._count_in_flight_work

    _live_update_handler_tasks = _upd_sched._live_update_handler_tasks

    _drain_update_callback_work = _upd_sched._drain_update_callback_work

    _UPDATE_BUSY_RETRY_SECS = _upd_sched._UPDATE_BUSY_RETRY_SECS

    _MANDATORY_UPDATE_MAX_DEFER_SECS = _upd_sched._MANDATORY_UPDATE_MAX_DEFER_SECS

    _UPDATE_DRAIN_TIMEOUT_SECS = _upd_sched._UPDATE_DRAIN_TIMEOUT_SECS

    _prepare_auto_update_apply = _upd_sched._prepare_auto_update_apply

    _finish_auto_update_apply = _upd_sched._finish_auto_update_apply

    _wheel_apply_module = staticmethod(_upd_sched._wheel_apply_module)

    _dashboard_policy_applies = staticmethod(_upd_sched._dashboard_policy_applies)

    _settle_update_work = _upd_sched._settle_update_work

    _run_update_checks = _upd_sched._run_update_checks

    _retry_pending_update_restart = _upd_sched._retry_pending_update_restart

    _check_for_updates = _upd_sched._check_for_updates

    _auto_update_effect = _upd_sched._auto_update_effect

    _publish_provider_update_state = _upd_sched._publish_provider_update_state

    _check_for_updates_via_provider = _upd_sched._check_for_updates_via_provider

    _restart_after_update_claimed = _upd_sched._restart_after_update_claimed

    _check_for_updates_legacy = _upd_sched._check_for_updates_legacy

    # gateway_runtime/update_apply.py
    _auto_apply_update = _upd_apply._auto_apply_update

    _auto_apply_wheel_update = _upd_apply._auto_apply_wheel_update

    _notice_wheel_update_once = _upd_apply._notice_wheel_update_once

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    # gateway_runtime/channel_lifecycle.py
    _connect_slack = _channel_lifecycle._connect_slack

    # gateway_runtime/exit_path.py
    _shutdown = _exit._shutdown

    _write_marker_worker = _exit._write_marker_worker

    _install_shutdown_signal_handlers = _exit._install_shutdown_signal_handlers

    _shutdown_and_exit = _exit._shutdown_and_exit

    # gateway_runtime/service_boot.py
    _auto_open_dashboard = _boot._auto_open_dashboard

    _init_services = _boot._init_services

    _notify_consolidation_abandoned = _boot._notify_consolidation_abandoned

    _schedule_session_start_sizing = _boot._schedule_session_start_sizing

    _log_session_start_sizing = _boot._log_session_start_sizing

    _init_task_runner = _boot._init_task_runner

    _init_dashboard = _boot._init_dashboard

    _init_api_server = _boot._init_api_server

    run = _boot.run

    # ── per-channel hoists ───────────────────────────────────────────────
    # Each one is the ONLY place its channel's boot state is derived, so a
    # restart re-derives exactly what boot derived. A hoist resets the legacy
    # ``_<channel>_client`` mirror too: it runs only before a start (boot or
    # restart), where the mirror is None by construction.

    def _hoist_wecom(self, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
        self._wecom_bot_id = creds.get(CRED_WECOM_BOT_ID, "")
        self._wecom_secret = creds.get(CRED_WECOM_SECRET, "")
        self._wecom_enabled = bool(cfg.wecom.enabled and self._wecom_bot_id and self._wecom_secret)

    def _hoist_telegram(self, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
        # Telegram — the TELEGRAM_BOT_TOKEN credential (env/.env) overrides
        # cfg.telegram.bot_token; all other settings come from the typed
        # cfg.telegram dataclass (no ad-hoc config.json re-parse).
        self._telegram_bot_token = creds.get(CRED_TELEGRAM_BOT_TOKEN, "") or cfg.telegram.bot_token
        # telegram.accounts is deprecated and inert, and while it is set the channel
        # stays OFF rather than falling back to the top-level token. A config that
        # named accounts served ONLY those accounts — the top-level bot_token and
        # allowed_user_ids were shadowed — so serving them now would reopen a bot
        # the operator had stopped, under an allow-list they may have narrowed when
        # they migrated. Staying off preserves what the accounts block already did
        # and leaves re-enabling an explicit edit.
        self._telegram_enabled = bool(
            cfg.telegram.enabled and self._telegram_bot_token and not cfg.telegram.accounts
        )
        if cfg.telegram.accounts:
            logger.warning(
                "telegram.accounts is no longer served (%d account(s): %s) — multi-bot "
                "operation is withdrawn until a bot is a governable unit, and the "
                "Telegram channel stays OFF while telegram.accounts is set (these "
                "entries already shadowed the top-level token, so falling back to it "
                "would start a bot you had stopped). Remove the accounts block and put "
                "the one token you want served in telegram.bot_token; the entries are "
                "preserved in config until you do.",
                len(cfg.telegram.accounts),
                ", ".join(sorted(cfg.telegram.accounts)),
            )
        self._telegram_allowed_user_ids: list[int] = list(cfg.telegram.allowed_user_ids)
        # Forum-topic gate (fail closed): serve supergroup forum Topics only when
        # allow_forum is set AND the supergroup's chat_id is allow-listed.
        self._telegram_allow_forum: bool = bool(cfg.telegram.allow_forum)
        self._telegram_allowed_forum_chat_ids: list[int] = list(cfg.telegram.allowed_forum_chat_ids)
        self._telegram_client: "TelegramClient | None" = None

    def _hoist_weixin(self, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
        # Weixin (iLink personal WeChat) — the WEIXIN_TOKEN credential (env/.env)
        # overrides cfg.weixin.token. token + account_id come from the Settings
        # QR flow; deny-by-default DM policy from the typed cfg.weixin dataclass.
        self._weixin_token = creds.get(CRED_WEIXIN_TOKEN, "") or cfg.weixin.token
        self._weixin_account_id: str = cfg.weixin.account_id
        self._weixin_base_url: str = cfg.weixin.base_url
        self._weixin_dm_policy: str = cfg.weixin.dm_policy
        self._weixin_allowed_user_ids: list[str] = list(cfg.weixin.allowed_user_ids)
        self._weixin_enabled = bool(
            cfg.weixin.enabled and self._weixin_token and self._weixin_account_id
        )
        self._weixin_client: "WeixinClient | None" = None

    def _hoist_whatsapp(self, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
        # WhatsApp (QR-linked personal account) — no credential: pairing state
        # lives in the channel's session DB, created by the Settings QR flow.
        # Enablement is config-only; maybe_start_whatsapp reports the missing
        # optional dependency or an unpaired session via the status badge.
        self._whatsapp_enabled = bool(cfg.whatsapp.enabled)
        self._whatsapp_client: "WhatsAppClient | None" = None

    def _hoist_feishu(self, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
        # Feishu (Lark/飞书) — FEISHU_APP_ID / FEISHU_APP_SECRET (env/.env),
        # matching the Feishu developer console's own naming; everything else
        # from the typed cfg.feishu dataclass. Both are registered credentials,
        # so they are stripped from the agent subprocess environment by
        # sandbox._AGENT_DENIED_ENV_KEYS — the gateway is their only consumer.
        # Deny-by-default: an empty allowed_open_ids authorises nobody, and a
        # group chat needs BOTH allow_group and an allow-listed chat_id. The
        # client handle is owned by the channel registry (``kiro_crew.channels``),
        # which also closes it on shutdown.
        self._feishu_app_id = creds.get(CRED_FEISHU_APP_ID, "")
        self._feishu_app_secret = creds.get(CRED_FEISHU_APP_SECRET, "")
        self._feishu_enabled = bool(
            cfg.feishu.enabled and self._feishu_app_id and self._feishu_app_secret
        )
        self._feishu_allowed_open_ids: list[str] = list(cfg.feishu.allowed_open_ids)
        self._feishu_allow_group: bool = bool(cfg.feishu.allow_group)
        self._feishu_allowed_group_ids: list[str] = list(cfg.feishu.allowed_group_ids)

    def _hoist_discord(self, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
        # Discord — the DISCORD_BOT_TOKEN credential (env/.env) overrides
        # cfg.discord.bot_token; all other settings come from the typed
        # cfg.discord dataclass (mirrors the Telegram block above).
        self._discord_bot_token = creds.get(CRED_DISCORD_BOT_TOKEN, "") or cfg.discord.bot_token
        self._discord_enabled = bool(cfg.discord.enabled and self._discord_bot_token)
        self._discord_allowed_user_ids: list[str] = [str(u) for u in cfg.discord.allowed_user_ids]
        self._discord_allowed_thread_ids: list[str] = [
            str(t) for t in cfg.discord.allowed_thread_ids
        ]
        self._discord_allowed_channel_ids: list[str] = [
            str(c) for c in cfg.discord.allowed_channel_ids
        ]
        self._discord_auto_thread = bool(cfg.discord.auto_thread)
        self._discord_client: "DiscordClient | None" = None

    def _hoist_webex(self, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
        # Webex — the WEBEX_BOT_TOKEN credential (env/.env) overrides
        # cfg.webex.bot_token; all other settings come from the typed
        # cfg.webex dataclass (no ad-hoc config.json re-parse).
        self._webex_bot_token = creds.get(CRED_WEBEX_BOT_TOKEN, "") or cfg.webex.bot_token
        self._webex_enabled = bool(cfg.webex.enabled and self._webex_bot_token)
        self._webex_allowed_emails: list[str] = list(cfg.webex.allowed_emails)
        self._webex_client: "WebexClient | None" = None

    def _hoist_imessage(self, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
        # iMessage — no credential exists to hoist: the transport is the user's
        # own signed-in Messages.app, so enablement is the config flag alone.
        # Everything else is read from the typed cfg.imessage dataclass at start.
        self._imessage_enabled = bool(cfg.imessage.enabled)
        self._imessage_client: "IMessageClient | None" = None

    def _hoist_teams(self, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
        # Teams — the MICROSOFT_APP_ID / MICROSOFT_APP_PASSWORD / _TENANT_ID
        # credentials (env/.env) override the typed cfg.teams fields; all other
        # settings come from the typed cfg.teams dataclass.
        self._teams_app_id = creds.get(CRED_MICROSOFT_APP_ID, "") or cfg.teams.app_id
        self._teams_app_password = (
            creds.get(CRED_MICROSOFT_APP_PASSWORD, "") or cfg.teams.app_password
        )
        self._teams_tenant_id = creds.get(CRED_MICROSOFT_APP_TENANT_ID, "") or cfg.teams.tenant_id
        self._teams_enabled = bool(
            cfg.teams.enabled and self._teams_app_id and self._teams_app_password
        )
        self._teams_allowed_emails: list[str] = list(cfg.teams.allowed_emails)
        self._teams_client: "TeamsClient | None" = None

    # ── live config: per-channel restart + Slack hot fields ─────────────

    #: Slack-owned top-level and section prefixes whose changes the Slack applier
    #: reconciles. Everything Slack reads lives under ``slack.*`` in the
    #: serialized document (``slack.channels``, ``slack.dm_activation`` and the
    #: ``observe_*`` caps are emitted inside the ``slack`` section) plus the
    #: transport switch under ``messaging``.
    _SLACK_APPLIER_PREFIXES: tuple[str, ...] = ("slack", "messaging")

    def _register_config_appliers(self) -> None:
        """Subscribe this orchestrator's config appliers on the process watcher.

        Bound methods are held weakly by the watcher, so an orchestrator built
        and discarded by a test does not pin itself into the registry. The
        Subscription objects are kept on ``self`` so they live as long as the
        orchestrator does.
        """
        channel_prefixes = tuple(
            d.channel_type for d in registry.bootable(builtin_channel_descriptors())
        )
        self._config_subs = [
            live.subscribe(
                *channel_prefixes,
                callback=self._on_channel_config_change,
                name="GatewayOrchestrator.channel_restart",
            ),
            live.subscribe(
                *self._SLACK_APPLIER_PREFIXES,
                callback=self._on_slack_config_change,
                name="GatewayOrchestrator.slack",
            ),
            # Its own subscription: a deferral is merged per subscription, so a
            # discarded ``agent`` section is retried without keeping the Slack
            # paths stale beside it.
            live.subscribe(
                "agent.default_agent",
                callback=self._on_default_agent_config_change,
                name="GatewayOrchestrator.default_agent",
            ),
        ]

    # gateway_runtime/channel_lifecycle.py
    _on_channel_config_change = _channel_lifecycle._on_channel_config_change

    _restart_changed_channels = _channel_lifecycle._restart_changed_channels

    channel_restarts_settled = _channel_lifecycle.channel_restarts_settled

    restart_channel = _channel_lifecycle.restart_channel

    _forget_superseded_start = _channel_lifecycle._forget_superseded_start

    _close_channel_locked = _channel_lifecycle._close_channel_locked

    _on_slack_config_change = _channel_lifecycle._on_slack_config_change

    _on_default_agent_config_change = _channel_lifecycle._on_default_agent_config_change

    _adopt_channel_sections_from_watcher = _channel_lifecycle._adopt_channel_sections_from_watcher

    _adopt_channel_section_from_watcher = _channel_lifecycle._adopt_channel_section_from_watcher

    async def _start_channel_transports(
        self, descriptors: "tuple[ChannelDescriptor, ...] | None" = None
    ) -> None:
        """Start each non-Slack transport, gated on the ``channels`` scope.

        Registry-driven (PR ③ of the channel-plugin RFC): the roster comes from
        :func:`kiro_crew.channels.builtin_channel_descriptors` and the loop
        lives in :mod:`kiro_crew.messaging.registry` — adding a channel no
        longer edits this method. ``descriptors`` is injectable for tests.

        Every transport is a guarded no-op unless enabled + credentialed (its
        own ``maybe_start_*``), and is ADDITIONALLY gated on the ``channels``
        governance scope: a policy that denies the transport member keeps it from
        connecting at all, and its client stays ``None``. The member ids are
        IDENTICAL to the outbound chokepoints — outbound-send (``mcp_core``) and
        outbound cross-surface mirroring (``chat_runner``) — so one ``channels``
        allowlist governs a transport at connect time and on every outbound path
        (inbound receive is gated per-message by
        ``messaging.identity.channel_inbound_permitted``).

        Default-build invariant: with no policy governing ``channels`` (the
        standard OSS build) the gate permits, so every transport starts. Slack is
        a registry member too (``start=None``) but is gated in ``_connect_slack``
        rather than here,
        because it owns its own socket-client lifecycle (a deny must drop that
        client, not just skip a start call).

        Loop hygiene: ``_channel_transport_permitted`` reaches
        ``ProfileStore._ensure_fresh``, which stats/reads the profile files off
        disk — blocking I/O. This method runs on the gateway event loop (inside
        ``run()``), so the governance decisions are computed together in an
        executor BEFORE any transport is started; only the actual factory
        awaits stay on the loop.

        Enabled-only eval: the gate is queried ONLY for a transport whose
        ``_<member>_enabled`` is set (config-enabled + credentialed). A transport
        that is off never starts regardless of policy, so evaluating it would only
        emit a spurious deny-SEL for a channel that was never going to connect.
        A member not evaluated defaults to not-permitted (it is off anyway), so
        the no-policy default permits: every ENABLED transport resolves to permit
        and starts.
        """
        if descriptors is None:
            descriptors = builtin_channel_descriptors()
        boot = registry.bootable(descriptors)
        creds = await self._adopt_channel_sections_from_watcher(boot)
        enabled = {
            d.channel_type: bool(getattr(self, f"_{d.channel_type}_enabled", False)) for d in boot
        }
        # The enabled-only gate below never calls a factory whose flag is
        # False — for a disabled and an enabled-but-uncredentialed channel
        # alike — so a factory-level skip-reason log can never be reached.
        # Say WHY each channel is being skipped here, at the decision point.
        # Runs after KIROCREW_READY, outside the
        # boot-path window. Each row lists exactly the credential operands its
        # _<channel>_enabled predicate reads: telegram folds in the
        # deprecated-accounts stop (which already has its own warning at
        # config-load time, so pointing at the token would misname the actual
        # blocker), and teams deliberately omits the tenant id its predicate
        # never reads. whatsapp/imessage enablement is config-only (no
        # credential operand), so they have no row.
        uncredentialed_probe_rows: tuple[
            tuple[str, str, bool, tuple[tuple[str, bool], ...]], ...
        ] = (
            (
                "wecom",
                "WeCom",
                self._cfg.wecom.enabled,
                (
                    (CRED_WECOM_BOT_ID, bool(self._wecom_bot_id)),
                    (CRED_WECOM_SECRET, bool(self._wecom_secret)),
                ),
            ),
            (
                "telegram",
                "Telegram",
                bool(self._cfg.telegram.enabled and not self._cfg.telegram.accounts),
                ((CRED_TELEGRAM_BOT_TOKEN, bool(self._telegram_bot_token)),),
            ),
            (
                "weixin",
                "WeChat",
                self._cfg.weixin.enabled,
                (
                    (CRED_WEIXIN_TOKEN, bool(self._weixin_token)),
                    ("weixin.account_id", bool(self._weixin_account_id)),
                ),
            ),
            (
                "feishu",
                "Feishu",
                self._cfg.feishu.enabled,
                (
                    (CRED_FEISHU_APP_ID, bool(self._feishu_app_id)),
                    (CRED_FEISHU_APP_SECRET, bool(self._feishu_app_secret)),
                ),
            ),
            (
                "discord",
                "Discord",
                self._cfg.discord.enabled,
                ((CRED_DISCORD_BOT_TOKEN, bool(self._discord_bot_token)),),
            ),
            (
                "webex",
                "Webex",
                self._cfg.webex.enabled,
                ((CRED_WEBEX_BOT_TOKEN, bool(self._webex_bot_token)),),
            ),
            (
                "teams",
                "Teams",
                self._cfg.teams.enabled,
                (
                    (CRED_MICROSOFT_APP_ID, bool(self._teams_app_id)),
                    (CRED_MICROSOFT_APP_PASSWORD, bool(self._teams_app_password)),
                ),
            ),
        )
        for channel_type, settings_name, cfg_enabled, presence in uncredentialed_probe_rows:
            warn_if_channel_uncredentialed(channel_type, settings_name, cfg_enabled, presence)
        loop = asyncio.get_running_loop()
        permitted = await loop.run_in_executor(
            maintenance_executor(),
            lambda: {
                m: (_channel_transport_permitted(m) if enabled[m] else False) for m in enabled
            },
        )
        # BEFORE starting: a channel that starts sets its own badge, so seeding
        # first lets a success overwrite this and leaves it only where the factory
        # bailed out early.
        await loop.run_in_executor(maintenance_executor(), self._badge_unready_channels, boot)
        # Second adoption, per channel, right before its factory: the first pass
        # above fed the enabled census; this one feeds each transport's first
        # authorization state with whatever landed while the channels ahead of it
        # were connecting. Synchronous by contract (see the hook's docstring).
        self._channel_handles = await registry.start_channels(
            self,
            descriptors,
            permitted,
            before_start=lambda desc: self._adopt_channel_section_from_watcher(desc, creds),
        )
        # A connection-parameter edit that landed while the channels were still
        # starting was deferred by the applier and sits in the watcher's stale
        # table; its next tick re-runs the applier against the current snapshot,
        # with the same per-channel restart the edit would have triggered a
        # moment later.
        self._channel_transports_started = True
        # Tell the sender of whatever the SHUTDOWN GATE refused on the way down
        # that it was never processed. Ordered AFTER the transports
        # because the notice is sent through the channel that received the
        # message, and detached from boot so a slow platform send cannot hold the
        # gateway's start open. The spool's location is resolved HERE, on the
        # loop, as the task is scheduled: the pass reads the spool on a worker
        # thread, and a path resolved there would name whatever data home the
        # environment holds at that later moment -- not the one this gateway
        # booted under. Under the test suite that moment is after the starting
        # test's pins are gone; five full runs left a lock file in the operator's
        # REAL data home that way.
        self._schedule_inbound_replay()

    # gateway_runtime/channel_lifecycle.py
    _schedule_inbound_replay = _channel_lifecycle._schedule_inbound_replay

    _replay_spooled_inbound = _channel_lifecycle._replay_spooled_inbound

    _badge_unready_channels = _channel_lifecycle._badge_unready_channels


# Every function the owners define runs on this module's globals, so a patch of
# ``kiro_crew.slack.gateway.<name>`` reaches it wherever it now lives; see
# ``gateway_runtime``. Run once, after the class has bound the owner methods.
_runtime.compose(
    globals(),
    GatewayOrchestrator,
    (
        _admission,
        _channel_lifecycle,
        _cron_dispatch,
        _cron_verdict,
        _delivery,
        _mcp_broker,
        _memory_lifecycle,
        _tool_policy,
        _install,
        _upd_sched,
        _upd_apply,
        _nudge_wiring,
        _nudge_dash,
        _nudge_chan,
        _approvals,
        _boot,
        _heartbeat,
        _egress,
        _exit,
        _entry,
    ),
)


# Strong reference to the background slice-limit apply task: the event loop
# holds tasks weakly, so a fire-and-forget create_task with no reference can
# be garbage-collected mid-flight.
_SLICE_LIMITS_TASK: "asyncio.Task[None] | None" = None

# Strong reference to the background KAS kiro-cli floor check, for the same reason.
_KAS_CLI_FLOOR_TASK: "asyncio.Task[None] | None" = None


# Strong ref to the fire-and-forget agents-dir janitor sweep launched at boot
# (the loop holds tasks weakly, so without this it could be GC'd mid-flight).
_AGENTS_JANITOR_TASK: "asyncio.Task[None] | None" = None
#: Strong ref for the liveness-keyed agent-scratch sweep loop.
_AGENT_SCRATCH_SWEEP_TASK: "asyncio.Task[None] | None" = None
#: Strong ref for the kiro-cli log cap loop.
_KIRO_CLI_LOG_CAP_TASK: "asyncio.Task[None] | None" = None
