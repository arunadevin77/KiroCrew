"""ACP client — JSON-RPC 2.0 over stdio with `kiro-cli acp` or `claude-agent-acp`.

Protocol (ACP JSON-RPC 2.0):
  initialize → session/new → session/set_mode → session/set_model → session/prompt
  (claude backend: no agent activation via set_mode -- its modes are permission
  modes, not agents -- and session/set_config_option for model)

Agent selection: ``session/set_mode`` with ``modeId`` activates the agent
config (prompt, tools, resources).  MCP servers are passed explicitly
in ``session/new`` via the ``mcpServers`` parameter.

Permission flow:
  ← session/request_permission (server→client REQUEST with uuid id)
  → {result: {outcome: {outcome: "selected", optionId: "allow_once"}}}
"""

from __future__ import annotations

import asyncio
import functools
import glob
import hashlib  # noqa: F401
import importlib
import json
import logging
import os
import re  # noqa: F401
import shutil
import signal  # noqa: F401
import stat
import subprocess as subprocess_mod
import sys
import tempfile
import time
import uuid
from collections import deque
from contextlib import aclosing, suppress
from pathlib import Path, PureWindowsPath  # noqa: F401
from types import ModuleType
from typing import (  # noqa: F401
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Mapping,
    TypeVar,
)

from kiro_crew import (  # noqa: F401
    __version__,
    _facade_mirror,
    acp_tool_gate,
    agent_scratch,
    model_registry,
    model_scope,
    permission_floor,
    pinned_fs,
    platform_compat,
)
from kiro_crew import sel as sel_module
from kiro_crew.acp import client_claude_seed as _client_claude_seed
from kiro_crew.acp import client_claude_session as _client_claude_session
from kiro_crew.acp import client_model_selection as _client_model_selection
from kiro_crew.acp import client_permissions as _client_permissions
from kiro_crew.acp import client_seed_ownership as _client_seed_ownership
from kiro_crew.acp import client_spawn_env as _client_spawn_env
from kiro_crew.acp import launch as launch_mod  # noqa: F401
from kiro_crew.acp import managed_agent_shadow as managed_shadow_mod
from kiro_crew.acp import (  # noqa: F401
    runtime_models,
    runtime_process_tree,
    seed_provenance,
    transport_framing,
)
from kiro_crew.acp._dispatch import (  # noqa: F401
    ACP_BACKENDS_META_IDENTITY,
    DRAIN_YIELD_AFTER_S,
    MAX_ACTIVE_TOOL_CALLS,
    BackgroundLaunchRecord,
    InFlightIdSet,
    _dumps_degraded,
    _loggable_request_id,
    _measure_tool_output,
    agent_version_from_init,
    build_permission_event,
    build_session_new_params,
    classify_tool_call,
    derive_edit_diff,
    error_is_refusal_terminal,
    extract_tool_purpose,
    gate_envelope,
    harness_tool_name,
)
from kiro_crew.acp._dispatch import identified_mcp_call as _identified_mcp_call  # noqa: F401
from kiro_crew.acp._dispatch import is_mcp_tool_approval as _is_mcp_tool_approval  # noqa: F401
from kiro_crew.acp._dispatch import (  # noqa: F401
    log_unrenderable_content,
    make_unified_diff,
    meta_builtin_server_names,
    parse_claude_compaction_notice,
    parse_codex_compaction_update,
    parse_message_id,
    parse_prompt_token_usage,
    parse_refusal,
    parse_session_modes,
    parse_usage_cost,
    parse_usage_update,
    redact_text,
)
from kiro_crew.acp._dispatch import select_tool_title as _select_tool_title
from kiro_crew.acp._dispatch import (  # noqa: F401
    tool_call_content_text,
)
from kiro_crew.acp._frame_record import record_frame
from kiro_crew.acp.agent_env import agent_env_overlay  # noqa: F401
from kiro_crew.acp.child_env_defaults import apply_child_env_defaults
from kiro_crew.acp.client_claude_seed import _claude_settings_usable  # noqa: F401
from kiro_crew.acp.client_claude_session import _CLAUDE_CARRIED_PERMISSION_KEYS  # noqa: F401
from kiro_crew.acp.client_claude_session import _CLAUDE_CREW_GOVERNED_SETTING_SOURCES  # noqa: F401
from kiro_crew.acp.client_claude_session import _CLAUDE_GATE_ESCAPING_MODES  # noqa: F401
from kiro_crew.acp.client_claude_session import _CLAUDE_PINNED_MODE  # noqa: F401
from kiro_crew.acp.client_claude_session import _CLAUDE_PROJECT_SETTINGS_FILES  # noqa: F401
from kiro_crew.acp.client_claude_session import _CLAUDE_PROJECT_SETTINGS_MAX_BYTES  # noqa: F401
from kiro_crew.acp.client_claude_session import _CLAUDE_PROJECT_UNCARRIED_GUARD_KEYS  # noqa: F401
from kiro_crew.acp.client_claude_session import _CLAUDE_SIDE_READ_ONLY_ASK_RULES  # noqa: F401
from kiro_crew.acp.client_claude_session import _GITIGNORE_PATTERN_CHARS  # noqa: F401
from kiro_crew.acp.client_claude_session import CLAUDE_ACP_FORK_POINT_MIN_VERSION  # noqa: F401
from kiro_crew.acp.client_claude_session import CLAUDE_ACP_SETTING_SOURCES_MIN_VERSION  # noqa: F401
from kiro_crew.acp.client_claude_session import CLAUDE_ACP_STEERING_MIN_VERSION  # noqa: F401
from kiro_crew.acp.client_claude_session import _adapter_version_at_least  # noqa: F401
from kiro_crew.acp.client_claude_session import _claude_root_pattern  # noqa: F401
from kiro_crew.acp.client_claude_session import _claude_side_read_deny_rules  # noqa: F401
from kiro_crew.acp.client_claude_session import _project_settings_restrictions  # noqa: F401
from kiro_crew.acp.client_claude_session import (  # noqa: F401
    _claude_adapter_honours_setting_sources,
)
from kiro_crew.acp.client_model_selection import _JSONRPC_INVALID_PARAMS  # noqa: F401
from kiro_crew.acp.client_model_selection import _is_config_value_rejection  # noqa: F401
from kiro_crew.acp.client_model_selection import _push_model_via_effort_split  # noqa: F401
from kiro_crew.acp.client_permissions import GOOSE_VERIFIED_VERSION_PREFIX  # noqa: F401
from kiro_crew.acp.client_permissions import PI_ACP_VERIFIED_VERSION  # noqa: F401
from kiro_crew.acp.client_permissions import _goose_versions_noted  # noqa: F401
from kiro_crew.acp.client_permissions import _pi_adapter_versions_noted  # noqa: F401
from kiro_crew.acp.client_permissions import _scrub_observed  # noqa: F401
from kiro_crew.acp.client_spawn_env import _CODEX_ACP_DEP_MARKER  # noqa: F401
from kiro_crew.acp.client_spawn_env import _CODEX_ACP_PKG_ENTRY  # noqa: F401
from kiro_crew.acp.client_spawn_env import _ENV_CODEX_ACP_BIN  # noqa: F401
from kiro_crew.acp.client_spawn_env import _KIRO_CLI_BUNDLE_EXECUTABLE  # noqa: F401
from kiro_crew.acp.client_spawn_env import _MACOS_APP_BUNDLE_SUFFIX  # noqa: F401
from kiro_crew.acp.client_spawn_env import _MACOS_APP_BUNDLE_TAIL  # noqa: F401
from kiro_crew.acp.client_spawn_env import CODEX_ACP_BIN  # noqa: F401
from kiro_crew.acp.client_spawn_env import CODEX_ACP_NPM_PKG  # noqa: F401
from kiro_crew.acp.client_spawn_env import CREDENTIAL_POINTER_ENV_VARS  # noqa: F401
from kiro_crew.acp.client_spawn_env import IDENTITY_STORE_ROOT_ENV_VARS  # noqa: F401
from kiro_crew.acp.client_spawn_env import KIRO_CLI_BIN  # noqa: F401
from kiro_crew.acp.client_spawn_env import KIRO_CLI_SUBCMD  # noqa: F401
from kiro_crew.acp.client_spawn_env import _apply_pod_home_remap  # noqa: F401
from kiro_crew.acp.client_spawn_env import _codex_acp_argv_cache  # noqa: F401
from kiro_crew.acp.client_spawn_env import _exit_origin  # noqa: F401
from kiro_crew.acp.client_spawn_env import _kiro_cli_app_bundle_target  # noqa: F401
from kiro_crew.acp.client_spawn_env import _kiro_cli_bundle_binary  # noqa: F401
from kiro_crew.acp.client_spawn_env import _KiroExecutableTrustError  # noqa: F401
from kiro_crew.acp.client_spawn_env import _launch_tools  # noqa: F401
from kiro_crew.acp.client_spawn_env import _resolve_codex_acp_bin  # noqa: F401
from kiro_crew.acp.client_spawn_env import _resolve_kiro_bin  # noqa: F401
from kiro_crew.acp.client_spawn_env import _resolve_kiro_bin_for_spawn  # noqa: F401
from kiro_crew.acp.client_spawn_env import apply_pod_bundle_spawn  # noqa: F401
from kiro_crew.acp.client_spawn_env import codex_acp_not_found_message  # noqa: F401
from kiro_crew.acp.client_spawn_env import finish_suspended_spawn  # noqa: F401
from kiro_crew.acp.client_spawn_env import kiro_cli_not_found_message  # noqa: F401
from kiro_crew.acp.client_timeouts import _ACP_RESPAWN_BACKOFF_S  # noqa: F401
from kiro_crew.acp.client_timeouts import _CANCEL_GRACE_SECS  # noqa: F401
from kiro_crew.acp.client_timeouts import _COMPACTION_FAILED_TURN_BUDGET  # noqa: F401
from kiro_crew.acp.client_timeouts import _DEFAULT_PROMPT_TIMEOUT  # noqa: F401
from kiro_crew.acp.client_timeouts import _DRAIN_DURATION  # noqa: F401
from kiro_crew.acp.client_timeouts import _DRAIN_IDLE_EXIT  # noqa: F401
from kiro_crew.acp.client_timeouts import _INIT_TIMEOUT  # noqa: F401
from kiro_crew.acp.client_timeouts import _MAX_CONSECUTIVE_EMPTY  # noqa: F401
from kiro_crew.acp.client_timeouts import _OPENCODE_PERMISSION_REFINEMENT_TIMEOUT  # noqa: F401
from kiro_crew.acp.client_timeouts import _POST_COMPACTION_METADATA_GRACE_SECS  # noqa: F401
from kiro_crew.acp.client_timeouts import _PROMPT_TIMEOUT_MARGIN_SECS  # noqa: F401
from kiro_crew.acp.client_timeouts import _READ_TIMEOUT  # noqa: F401
from kiro_crew.acp.client_timeouts import _SEL_AUDIT_TIMEOUT_SECONDS  # noqa: F401
from kiro_crew.acp.client_timeouts import _STALE_TURN_TIMEOUT  # noqa: F401
from kiro_crew.acp.client_timeouts import _STEERING_WAIT_SLICE_SECS  # noqa: F401
from kiro_crew.acp.client_timeouts import _SUPPRESSED_STDERR_SUMMARY_INTERVAL_SECS  # noqa: F401
from kiro_crew.acp.client_timeouts import _TOOL_STALL_TIMEOUT  # noqa: F401
from kiro_crew.acp.client_timeouts import _WAIT_RESPONSE_MAX_TIMEOUT  # noqa: F401
from kiro_crew.acp.client_timeouts import _effective_prompt_timeout  # noqa: F401
from kiro_crew.acp.client_timeouts import _effective_prompt_timeout_async  # noqa: F401
from kiro_crew.acp.client_timeouts import prompt_timeout_for_ceiling  # noqa: F401
from kiro_crew.acp.client_timeouts import resolve_prompt_timeout  # noqa: F401
from kiro_crew.acp.client_timeouts import resolve_prompt_timeout_for_deadline  # noqa: F401
from kiro_crew.acp.harness import claude as claude_mod  # noqa: F401
from kiro_crew.acp.harness import pi as pi_mod  # noqa: F401
from kiro_crew.acp.harness import process_adapter_for
from kiro_crew.acp.harness.base import SpawnContext, SpawnPlan
from kiro_crew.acp.harness_tool_names import CLAUDE_READ_ONLY_BUILTINS  # noqa: F401
from kiro_crew.acp.launch import LaunchRequest, LaunchTools, launch  # noqa: F401
from kiro_crew.acp.liveness import (
    EVIDENCE_SAMPLING,
    VERDICT_WORKING,
    LivenessOracle,
    _consume_future_exception,
    consult_offloaded,
)
from kiro_crew.acp.mcp_ref_guard import (
    warn_remote_header_secret_refs,
    warn_unresolved_server_refs,
)
from kiro_crew.acp.mcp_session_report import BUCKET_CAP as MCP_REPORT_BUCKET_CAP
from kiro_crew.acp.mcp_session_report import ERROR_CAP as MCP_REPORT_ERROR_CAP
from kiro_crew.acp.mcp_session_report import NAME_CAP as MCP_REPORT_NAME_CAP
from kiro_crew.acp.mcp_session_report import McpSessionReport, sanitize_sink_text
from kiro_crew.acp.mcp_startable import PrunedServer, prune_unstartable
from kiro_crew.acp.prompt_blocks import build_prompt_blocks
from kiro_crew.acp.runtime_models import (  # noqa: F401
    DEFAULT_MODEL,
    _extract_advisory_detail,
    _is_model_substitution_advisory,
    _substitute_model_from_advisory,
    model_is_unusable,
    pick_served_default,
)
from kiro_crew.acp.runtime_process_tree import ChildRecord
from kiro_crew.acp.session_mcp import agent_spec_snapshot, session_mcp_deny_rules  # noqa: F401
from kiro_crew.acp.skill_reads import SkillReadTracker
from kiro_crew.acp.transport_errors import (  # noqa: F401
    AcpAuthRequired,
    AcpError,
    AcpModelUnavailable,
    AcpProcessDied,
    AcpSandboxInitFailed,
    AcpTimeoutError,
    AcpToolGateUnroutable,
    _raise_acp_error,
    compaction_failure_detail,
    compaction_failure_is_transient,
    is_sandbox_init_failure_output,
    registration_rate_limited_error,
    registration_throttle_line,
    sandbox_init_failure,
)
from kiro_crew.acp.transport_framing import (
    RequestWriteResult,
    _stall_window_phrase,
    response_write_window_secs,
    settle_drain,
    write_notification_best_effort,
    write_request_frame_bounded,
    write_response_frame_bounded,
)
from kiro_crew.acp.types import (  # noqa: F401
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION,
    ACP_BACKENDS_FORK_AT_MESSAGE,
    ACP_BACKENDS_HARNESS_OWNED_SESSIONS,
    ACP_BACKENDS_INLINE_COMPACTION,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_LOAD_WITHOUT_MODES,
    ACP_BACKENDS_MEMBER_DISPATCH,
    ACP_BACKENDS_MEMBER_PANEL,
    ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS,
    ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION,
    ACP_BACKENDS_POD_HOME_REMAP,
    ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP,
    ACP_BACKENDS_RESUME_WITHOUT_LOAD,
    ACP_BACKENDS_SEED_LOCAL_SETTINGS,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
    ACP_BACKENDS_STEER,
    ACP_BACKENDS_STEERING_REQUEST,
    ACP_BACKENDS_STRUCTURED_REFUSAL,
    ACP_CLIENT_CAPABILITIES,
    EVENT_AGENT_SWITCHED,
    EVENT_CLEAR_STATUS,
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_MCP_OAUTH_REQUEST,
    EVENT_MCP_SERVER_INIT_FAILURE,
    EVENT_MCP_SERVER_INITIALIZED,
    EVENT_STEER_CONSUMED,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
    JSONRPC_METHOD_NOT_FOUND,
    KIRO_CLI_CLIENT_APPLICATION,
    KNOWN_SESSION_UPDATES,
    MAX_STEERING_ANSWERS,
    MAX_STEERING_TEXT_CHARS,
    MCP_ROSTER_COMPLETE_NOTE,
    METHOD_AGENT_SWITCHED,
    METHOD_CANCEL,
    METHOD_CLEAR_STATUS,
    METHOD_COMMANDS_EXECUTE,
    METHOD_COMPACTION_STATUS,
    METHOD_INITIALIZE,
    METHOD_KIRO_SESSION_UPDATE,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_MCP_SERVER_INIT_FAILURE,
    METHOD_MCP_SERVER_INITIALIZED,
    METHOD_METADATA,
    METHOD_PROMPT,
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_FORK,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SESSION_RESUME,
    METHOD_SESSION_STEERING,
    METHOD_SESSION_UPDATE,
    METHOD_SET_MODE,
    METHOD_SET_MODEL,
    METHOD_SUBAGENT_LIST_UPDATE,
    MODEL_CONFIG_ID,
    OPTION_ALLOW_ALWAYS,
    OPTION_ALLOW_ONCE,
    OUTCOME_CANCELLED,
    OUTCOME_SELECTED,
    STEERING_ANSWER_WAIT_SECS,
    STEERING_INJECTED,
    STOP_REASON_COMPACTION_FAILED,
    STOP_REASON_END_TURN,
    TERMINAL_TOOL_STATUSES,
    UPDATE_AGENT_MESSAGE_CHUNK,
    UPDATE_AGENT_THOUGHT_CHUNK,
    UPDATE_CONFIG_OPTION,
    UPDATE_CURRENT_MODE,
    UPDATE_SESSION_INFO,
    UPDATE_TOOL_CALL,
    UPDATE_USAGE,
    UPDATE_USER_MESSAGE_CHUNK,
    AcpEvent,
    AcpPromptStats,
    JsonRpcMessage,
    JsonRpcRequest,
    effort_config_option_id,
    effort_config_option_value,
    model_registry_namespace,
    overlay_project_scope,
    steering_outcome,
)
from kiro_crew.agent import (
    DerivedSpecSnapshot,
    DerivedSpecStale,
    ForkGovernanceUnresolved,
    ensure_agent_materialized,
    require_fork_governance,
    require_fresh_derived_spec,
    require_unchanged_derived_spec,
)
from kiro_crew.agent_sdk import host_auth
from kiro_crew.agent_sdk.backends import (  # noqa: F401
    ACP_BACKEND_LAUNCH,
    ACP_BACKEND_NODE_ADAPTER_PACKAGES,
    ACP_BACKEND_PROCESS_NAMES,
    ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL,
    NODE_ADAPTER_ENTRY_SEGMENTS,
    launch_for,
    model_refusal_phrase,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.atomic_write import atomic_write  # noqa: F401
from kiro_crew.browser_cli.launch import browser_session_env, browser_socket_env  # noqa: F401
from kiro_crew.config.paths import config_dir, kiro_sessions_dir
from kiro_crew.constants import COMPACT_WAIT_TIMEOUT_SECS
from kiro_crew.dashboard.side_readonly_spec import unavailable_mode_explanation
from kiro_crew.env import (  # noqa: F401
    augmented_path,
    describe_search_path,
    resolve_krb5_ccname,
)
from kiro_crew.executors import subprocess_executor
from kiro_crew.hooks import (
    HOOK_EVENT_POST_TOOL_USE,
    fire_tool_hooks,
    get_global_hook_store,
)
from kiro_crew.identity_stores import IDENTITY_STORE_ROOTS  # noqa: F401
from kiro_crew.json_line import parse_json_object_line
from kiro_crew.jsonl_util import bounded_raw_records_with_offsets
from kiro_crew.kiro_cli import known_kiro_cli_dirs, resolve_kiro_cli  # noqa: F401
from kiro_crew.mcp_gateway.claim import (  # noqa: F401
    STUB_SESSION_TOKEN_ENV,
    mint_stub_session_token,
    schedule_claim,
)
from kiro_crew.mcp_gateway.session_servers import (
    attach_stub_session_token,
    injection_server_names,
    pooled_session_servers,
)
from kiro_crew.metrics.tool_calls import note_tool_call_started, record_tool_call_finished
from kiro_crew.owner_only_files import ensure_directory
from kiro_crew.platform.context import redact_log_via_context  # noqa: F401
from kiro_crew.providers.mirrors import MIRRORS, mirror_for
from kiro_crew.recovery.ladder import L3_ACP_RUNTIME as _L3_ACP_RUNTIME  # noqa: F401
from kiro_crew.recovery.ladder import LADDER as _LADDER  # noqa: F401
from kiro_crew.resource_status import inject_xdist_auto_cap  # noqa: F401
from kiro_crew.sandbox import (  # noqa: F401
    BoundWorkspaceMismatch,
    _forward_ssh_auth_sock,
    apply_windows_resource_ceiling,
    assert_voice_runtime_outside_agent_workspace,
    bind_voice_safe_agent_workspace_async,
    cgroup_scope_argv,
    create_subprocess_limited,
    delegated_workspace_exposes_sealed_target,
    release_bound_agent_workspace,
    resolve_bound_session_workspace,
    scrub_agent_subprocess_env,
    wrap_argv,
    wrap_argv_async,
    wrapped_by_crew_sandbox,
)
from kiro_crew.security import (  # noqa: F401
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
    sandbox_credential_targets,
)
from kiro_crew.security.credential_sources import tool_output_fingerprints
from kiro_crew.sel import sel
from kiro_crew.session_pid import _replace_child_pids
from kiro_crew.session_token_sig import schedule_session_token_publish
from kiro_crew.skill_usage import get_global_skill_read_observer  # noqa: F401  (facade surface)
from kiro_crew.trust_patterns import GrantDeadline, GrantEnded, grant_deadline_guard  # noqa: F401
from kiro_crew.turn_limit import current_turn_limit_secs  # noqa: F401
from kiro_crew.user_json import loads_user_json  # noqa: F401

logger = logging.getLogger(__name__)

#: The name sent as ``clientInfo.name``. It IS the kiro-cli client-application
#: tag, so one filter string finds Crew on both channels and the two cannot drift.
CLIENT_NAME = KIRO_CLI_CLIENT_APPLICATION
#: The version reported to the agent host in ``initialize``'s ``clientInfo``, and
#: from there into the host's own telemetry (``acp_client_version``). It is the
#: PACKAGE version, never a literal of its own: a hand-maintained literal is a
#: second version number nobody bumps, and this one was not -- it sat at
#: ``"0.1.2"`` from the first commit that named it while the product shipped
#: 0.2.0 through 0.8.0, so every Crew-driven session of every release reported
#: one indistinguishable version and no version split of Crew traffic was
#: possible. Reading ``__version__`` also inherits what that attribute already
#: resolves at import: a release lane's rewritten literal AND a repackager's
#: ``BUILD_VERSION`` stamp.
CLIENT_VERSION = __version__
_T = TypeVar("_T")
# kiro-cli uses a date-stamped protocol; claude-agent-acp follows the
# upstream ACP SDK (numeric integer, currently 1).  See acp.types.
PROTOCOL_VERSION = "2025-08-22"
PROTOCOL_VERSION_CLAUDE = 1
# codex-acp has no literal here, and its absence is the point: this core drives
# only the harnesses in the table below, and codex is not one of them. Its
# handshake dialect is declared by ``acp.harness.codex``, on the core that does
# drive it. One harness, one declaration -- a second copy on a core that never
# performs the handshake is a copy nothing can keep honest.
# OpenCode answers ``initialize`` with an integer ``protocolVersion`` of 1, so it
# speaks the SPEC dialect rather than kiro-cli's date-stamped one. Verified off its
# own wire, and its own literal for the same reason codex has one (harness-parity
# H10): a divergence should be a one-line edit here, not a silent downgrade of
# whichever harness moved first.
PROTOCOL_VERSION_OPENCODE = launch_for(ACP_BACKEND_OPENCODE).protocol_version
# pi-acp answers ``initialize`` with an integer ``protocolVersion`` of 1 as well,
# verified off its own wire; its own literal for the same H10 reason.
PROTOCOL_VERSION_PI = 1
# goose answers ``initialize`` with an integer ``protocolVersion`` of 1, captured off
# goose 1.50.1's own wire (``test/fixtures/acp_frames/goose/handshake-live.jsonl``);
# its own literal for the same H10 reason.
PROTOCOL_VERSION_GOOSE = launch_for(ACP_BACKEND_GOOSE).protocol_version
# DeepSeek Harness answers ``initialize`` with an integer ``protocolVersion`` of 1,
# so it speaks the SPEC dialect too. Verified off its own wire, and its own literal
# for the same reason the two above have one (harness-parity H10).
PROTOCOL_VERSION_DEEPSEEK = launch_for(ACP_BACKEND_DEEPSEEK).protocol_version
#: Handshake dialect per harness. A TABLE, not an if-chain: the handshake runs on
#: the construction path kiro-cli shares with every adapter, and harness-parity H13
#: keeps that path free of conditionals added in service of one. A harness added
#: later is one row here; an id with no row speaks kiro-cli's date-stamped dialect.
_PROTOCOL_VERSION_BY_BACKEND: dict[str, int | str] = {
    ACP_BACKEND_CLAUDE: PROTOCOL_VERSION_CLAUDE,
    ACP_BACKEND_PI: PROTOCOL_VERSION_PI,
    # Every harness whose own binary serves ACP declares its dialect in its
    # ``ACP_BACKEND_LAUNCH`` row, so those rows are read rather than restated here.
    # The two adapters above keep explicit rows: each is a separate package with
    # its own release cadence, and none has a launch record to read.
    **{backend: record.protocol_version for backend, record in sorted(ACP_BACKEND_LAUNCH.items())},
}


# High-frequency, content-free adapter stderr diagnostics that _drain_stderr()
# drops instead of forwarding as per-line WARNINGs.  The driving case is the
# claude-agent-acp "Unexpected case: {...thinking_tokens...}" line.  Mechanism
# (confirmed by reading the vendored adapter, dist/acp-agent.js): the backend
# emits a `system` message with subtype `thinking_tokens`, but the adapter's
# `switch (message.subtype)` enumerates ~18 known subtypes (init, status,
# compact_boundary, memory_recall, api_retry, ...) and routes anything else to
# `default: unreachable(message)`, which does `logger.error("Unexpected case:
# " + JSON.stringify(message))` to stderr — one line per token delta.  Measured
# at ~10 lines/sec during active thinking (one line per 2-4 thinking tokens; the
# payload is only estimated_tokens/_delta/uuid/session_id — no response content,
# so dropping them loses nothing).
#
# This is a forward-compat gap in the adapter, NOT new behavior in a specific
# backend build: the `thinking_tokens` event is present in both 2.1.165.357
# and 2.1.168.358 (verified by string-matching both bundled `claude` binaries —
# identical occurrences), so it predates the .168 update that drew attention to
# it.  Exactly when it began appearing in our logs is unconfirmed.  The cleaner
# long-term fix is upstream (add a `thinking_tokens` case to the adapter / bump
# the vendored version); this filter is the version-agnostic stopgap that also
# absorbs the next unenumerated subtype's flood.
#
# Why drop rather than just downgrade the level:
#   1. Log hygiene — gateway.log uses a RotatingFileHandler(maxBytes=2MB,
#      backupCount=3) (see cli.py), so a sustained burst rolls genuine
#      diagnostics out of the retained 8MB window.
#   2. Event-loop load — the file handler is a plain *synchronous* handler and
#      _drain_stderr runs as a task on the gateway event loop, so each forwarded
#      line costs a synchronous file write + two regex redaction passes on the
#      same loop that streams responses.  Per-session the cost is small; it
#      compounds across concurrent thinking sessions.
# (This is a log-volume / event-loop-load reduction, NOT a fix for any
# turn-stall or "agent not responding" symptom — no such causal link was
# established.)
#
# Match on a stable substring (not the full JSON) so the filter survives field
# changes, and keep the tuple NARROW so a genuine error line is never silently
# swallowed.
_SUPPRESSED_STDERR_MARKERS = ("thinking_tokens",)


def _is_safe_oauth_url(url: str) -> bool:
    """Reject anything that isn't http(s) — `<a href>` will execute javascript:/data:."""
    if not url:
        return False
    lower = url.lower()
    return lower.startswith("https://") or lower.startswith("http://")


def _mise_which(tool: str) -> str | None:
    """Ask mise for the resolved path of *tool*.

    Respects MISE_DATA_DIR, global config, and .mise.toml — works
    regardless of how the user configured their mise installation.
    Returns None if mise isn't installed or the tool isn't registered.
    """
    mise_bin = shutil.which("mise")
    if not mise_bin:
        return None
    try:
        result = subprocess_mod.run(
            [mise_bin, "which", tool],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            path = result.stdout.strip()
            if Path(path).is_file():
                return path
    except (subprocess_mod.TimeoutExpired, OSError):
        pass
    return None


def steering_advertised(init_resp: object) -> bool:
    """Whether an ``initialize`` result advertises the ``_session/steering`` request.

    The extension is advertised at the TOP level of the result, as
    ``_meta.steering.supported``, a sibling of ``agentCapabilities``. Only a
    literal ``true`` counts.
    """
    meta = init_resp.get("_meta") if isinstance(init_resp, dict) else None
    steering = meta.get("steering") if isinstance(meta, dict) else None
    return isinstance(steering, dict) and steering.get("supported") is True


def _resolve_ssh_auth_sock(env: dict[str, str]) -> None:
    """Ensure SSH_AUTH_SOCK points to a live agent socket.

    The gateway's inherited value may be stale after an ssh-agent restart.
    Re-discovers the current agent socket without spawning a login shell.

    - macOS: launchd listener path changes on reboot
    - Linux: ssh-agent sockets live under /tmp/ssh-*/agent.*
    - Windows: no-op — there is no ``SSH_AUTH_SOCK`` (Win32 OpenSSH agent uses a
      named pipe, which needs no repair). Bare ``os.getuid()`` below would also
      ``AttributeError`` on win32, so return early; this function runs in the
      spawn prelude for BOTH ACP backends.
    """
    if platform_compat.IS_WINDOWS:
        return

    current = env.get("SSH_AUTH_SOCK", "")
    if current and os.path.exists(current):
        return  # already valid

    if sys.platform == "darwin":
        patterns = [
            "/tmp/com.apple.launchd.*/Listeners",
            "/var/run/com.apple.launchd.*/Listeners",
        ]
    else:
        uid = os.getuid()
        patterns = [
            "/tmp/ssh-*/agent.*",
            f"/run/user/{uid}/ssh-agent.socket",
            f"/run/user/{uid}/keyring/ssh",
        ]

    for pattern in patterns:
        candidates = [p for p in glob.glob(pattern) if stat.S_ISSOCK(os.stat(p).st_mode)]
        if candidates:
            best = max(candidates, key=lambda p: os.path.getmtime(p))
            env["SSH_AUTH_SOCK"] = best
            logger.debug("Resolved SSH_AUTH_SOCK → %s", best)
            return


def _resolve_spawn_env(env: dict[str, str], *, kiro_api_key: bool = False) -> dict[str, str]:
    """Repair stale credential pointers in *env* before an agent spawn.

    Bundles :func:`_resolve_ssh_auth_sock` (glob + stat over ``/tmp``) and
    :func:`resolve_krb5_ccname` (lstat/stat of ``/tmp/krb5cc_<uid>``) so the
    spawn path pays ONE thread hop for both. Both resolvers issue synchronous
    filesystem syscalls whose latency scales with the ``/tmp`` entry count, so
    they must never run on the event loop — call this via
    ``asyncio.to_thread``. Mutates *env* in place and returns it for
    convenience.

    With ``kiro_api_key=True`` (the kiro-cli backend), also re-injects the
    CLI's own model credential from the data home's ``.env`` when the Docker
    entrypoint scrubbed it out of the parent environ — the child authenticates
    from its environment, so without this an API-key container loses model
    auth. With ``kiro_api_key=False`` (a foreign backend) the credential is
    actively STRIPPED instead: it is kiro-cli's alone, and the deny scrub
    deliberately exempts it, so an inherited copy would otherwise ride into a
    foreign agent process. The file read is IO, which is why both branches
    ride this same off-loop hop.

    A kiro-cli child (``kiro_api_key=True``) also gets
    ``agent.child_env_defaults`` for keys *env* does not already carry -- a
    config read, so it rides this hop too. The ``AcpRuntime`` spawn applies the
    same helper through the kiro-family harness hook instead.
    """
    _resolve_ssh_auth_sock(env)
    resolve_krb5_ccname(env)
    # Deferred import: this module keeps config.loader off its import graph
    # (in-file convention; see the _prompt_timeout lazy-import note).
    from kiro_crew.config.loader import inject_kiro_cli_api_key, strip_kiro_cli_api_key

    if kiro_api_key:
        inject_kiro_cli_api_key(env)
        apply_child_env_defaults(env)
    else:
        strip_kiro_cli_api_key(env)
    return env


# Cap the structured-tool-params cache so a stream of ToolCall notifications with
# no matching request_permission can't grow it without bound (the entries are
# popped on the permission event and wholesale-cleared per prompt; this is just a
# backstop for the pathological no-permission case).
_MAX_CACHED_TOOL_PARAMS = 256


# Emitted by kiro-cli as a plain agent_message_chunk when its built-in, non-overridable
# security filter cancels every tool use in an assistant turn (e.g. shell commands
# containing "credentials").  After this text kiro-cli returns to an idle state waiting
# for the next user prompt and NEVER sends a ``complete`` response for the in-flight
# ``session/prompt`` — so without special handling Kiro Crew waits the full prompt timeout.
# Treating this chunk as end-of-turn unblocks the caller; the text itself is still
# yielded so the user/agent sees what happened.  We use an exact (stripped) match so
# the detection does not fire if the model merely quotes the marker string in prose.
_TOOL_INTERRUPTED_MARKER = "Tool uses were interrupted, waiting for the next user prompt"


def _is_tool_interrupted_marker(chunk: str) -> bool:
    """Exact match against the kiro-cli security-filter interrupt marker."""
    return chunk.strip() == _TOOL_INTERRUPTED_MARKER


def format_command_result(result: dict) -> str:
    """Extract displayable text from a commands/execute response.

    Module-level (not a method) because both native slash-command paths need
    it: AcpClient.stream_command (direct-spawn sessions) and
    AcpSessionHandle.stream_command (shared-runtime sessions).

    The output is two-pass redacted (URLs + credentials) HERE, in the shared
    helper, so every present and future caller inherits the security control
    (command output is backend-echoed text that reaches the dashboard) instead
    of each call site re-discovering it. Call-site re-redaction stays
    harmless — both passes are idempotent.
    """
    data = result.get("data")
    message = result.get("message", "")
    text = ""
    # Structured data — format as readable JSON block
    if isinstance(data, dict) and data:
        # Filter out agent/model metadata (handled separately)
        display = {k: v for k, v in data.items() if k not in ("agent", "model")}
        if display:
            block = json.dumps(display, indent=2)
            text = f"{message}\n```json\n{block}\n```" if message else f"```json\n{block}\n```"
    if not text:
        text = message or ""
    if text:
        text, _ = redact_exfiltration_urls(text)
        text, _ = redact_credentials(text)
    return text


def parse_slash_command(command: str) -> tuple[str, dict]:
    """Parse ``/foo bar baz`` into TuiCommand ``(name, args)``.

    Shared by AcpClient.stream_command and AcpSessionHandle.stream_command —
    both send the OBJECT form (``{command, args}``) because kiro-cli 2.14.0
    returns no response on the string form of ``_kiro.dev/commands/execute``.
    """
    parts = command.strip().split(None, 1)
    name = parts[0].lstrip("/") if parts else command.lstrip("/")
    value = parts[1] if len(parts) > 1 else None
    args: dict = {"value": value} if value else {}
    return name, args


# A member's own mid-turn steer request on THIS client, spoken in place of the
# ``_session/steering`` request claude takes
# (``AcpClient._steer_via_steering_request``). goose's
# ``_goose/unstable/session/steer`` takes ``{sessionId, expectedRunId, prompt}``,
# where the run id is the one goose announced in
# ``session_info_update._meta.goose.activeRunId`` when the prompt started; it
# answers ``{runId, messageId}`` once the text is queued for the run, and
# ``-32602`` when no run is active or the id names another run. A member with
# neither is not offered a steer on this client. The wait for the answer and the
# bounds on steers held for one turn are the shared ``STEERING_ANSWER_WAIT_SECS``,
# ``MAX_STEERING_TEXT_CHARS`` and ``MAX_STEERING_ANSWERS``.
_GOOSE_SESSION_STEER = "_goose/unstable/session/steer"
_CLIENT_STEERING_REQUEST_METHODS: dict[str, str] = {ACP_BACKEND_GOOSE: _GOOSE_SESSION_STEER}


# Canonical ACP tool-kind value for shell/exec tools. kiro-cli and
# claude-agent-acp both report shell commands with kind="execute", and so does
# codex-acp -- for its MCP tool calls too. _is_shell_kind() is therefore only
# the kind-half of the rule; the tool_call paths classify the WHOLE frame with
# _dispatch.classify_tool_call, which consults the adapter-authored MCP markers
# before the kind.
_ACP_SHELL_KIND = "execute"


def _is_shell_kind(kind: str | None) -> bool:
    """True when an ACP tool_kind denotes a shell/exec command (kind-only view)."""
    return kind == _ACP_SHELL_KIND


def _jsonrpc_error_code(error: object) -> int | None:
    """The integer ``code`` of a JSON-RPC error frame, or ``None``.

    Tolerant of every shape the wire has produced: a missing ``code``, a
    non-dict frame, or a code spelled as a numeric string all answer ``None``
    (a bool is refused too — ``True == 1`` would otherwise read as a code).
    """
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    if isinstance(code, bool):
        return None
    if isinstance(code, int):
        return code
    return None


def _make_unified_diff(old: str, new: str, path: str, max_len: int = 65536) -> str:
    """Generate a unified diff string from old/new text, handling empty inputs.

    Thin delegate to :func:`kiro_crew.acp._dispatch.make_unified_diff`, kept as
    a module-level name for this file's call sites and tests; the truncation
    semantics (line-boundary cut + ``DIFF_TRUNCATION_MARK``) live in one place.
    """
    return make_unified_diff(old, new, path, max_len=max_len)


class AcpClient:
    """JSON-RPC 2.0 client over stdio with kiro-cli acp."""

    # Written by the claude adapter at launch, read back by the settings writer
    # through ``getattr`` with an empty default: never set on any other host.
    _claude_adapter_disk_version: str
    # Fields first set by a member an owner module defines, declared so the class names them.
    _claude_shipped_restrictions: frozenset[tuple[str, str]]
    _entitlement_probe_inflight: asyncio.Future[tuple[list[dict[str, str]], float]] | None
    # The scrub this session applies to what a harness reports back, handed to the
    # host adapters that quote such a value (``ProcessSession._scrub_observed``).
    _scrub_observed = staticmethod(_scrub_observed)

    def __init__(
        self,
        work_dir: str | Path | None = None,
        model: str | None = None,
        agent: str = CLIENT_NAME,
        sandbox_mode: str = "auto",
        session_key: str | None = None,
        channel_id: str | None = None,
        extra_env: dict[str, str] | None = None,
        acp_backend: str = "",
        audit_source: str | None = None,
        mcp_gateway_overlay: str | Path | None = None,
        mcp_gateway_socket: str | Path | None = None,
        permission_mode: str | None = None,
        shared_scratch: Path | None = None,
        side_read_only: bool = False,
    ):
        if work_dir:
            self._work_dir = Path(work_dir)
        else:
            self._work_dir = config_dir() / "workspace"
        # Once-per-instance guard for the ensure_ready work-dir check: True
        # after the first (off-loop) mkdir, so the per-prompt warm path pays
        # no filesystem syscall at all.
        self._work_dir_ready = False
        self._model = model or DEFAULT_MODEL
        self._agent = agent
        self._sandbox_mode = sandbox_mode
        self.memory_mode = "persistent"
        self._acp_backend = acp_backend
        # Claude backend permission mode (Auto-mode / permission-UI parity).
        # Inert on the kiro-cli path. None = the backend's own default
        # ("default", i.e. every tool decision is forwarded to the host), which is
        # what _write_claude_local_settings leaves in place when nothing asked
        # for a mode.
        self._permission_mode = permission_mode
        # A Side Chat session that may run read-only tools. On claude it starts
        # with only the read built-ins and no MCP server, and its mode is pinned
        # to ``default`` (_claude_session_meta, _pin_claude_starting_mode). Inert
        # on the kiro-cli path, where the derived ``<agent>--readonly`` spec
        # confines the session instead.
        self._side_read_only = side_read_only
        # That session's sensitive-path deny rules, built off the loop before its
        # launch (_prepare_claude_side_read_denies). Empty until then, and a side
        # session whose envelope would carry none is not started.
        self._claude_side_read_denies: tuple[str, ...] = ()
        # The session tree's work directory when this client is a DEDICATED
        # subagent process spawned on a parent's behalf: re-validated at spawn
        # (``agent_scratch.shared_scratch_window``) and mounted as a second
        # private window beside this process's own scratch, which keeps the
        # temp triple and the kiro-cli log. ``None`` for a session that starts
        # its own tree. Once live, the client joins the tree's owner marker
        # beside its parent (``agent_scratch.adopt_owner``), so a parent that
        # dies first leaves no dead-owner marker over its running children.
        self._shared_scratch: Path | None = Path(shared_scratch) if shared_scratch else None
        self._scratch_dir: Path | None = None
        # True once this session has CREATED <work_dir>/.claude/settings.local.json
        # itself. Only then does reset remove it, and only then does a re-seed
        # overwrite it. The writer refuses a path that already holds a file it did
        # not author, so Crew never owns the undo for someone else's project
        # settings -- see _write_claude_local_settings.
        self._claude_settings_authored = False
        # True while the project owns settings.local.json and this session leaves
        # that file out of its setting sources, carrying Crew's settings inline in
        # ``_claude_inline_settings`` instead -- see _exclude_foreign_local_settings.
        self._claude_local_settings_excluded = False
        self._claude_inline_settings: dict[str, Any] | None = None
        # The exact bytes this session last wrote to that path, held as the str
        # whose utf-8 encoding IS those bytes -- the writer emits binary so no
        # newline translation can come between the two on any platform. Creating
        # the file is not sufficient ownership on its own: a user can replace it
        # atomically (write-temp + rename) after the create, and the replacement is
        # theirs. So the flag says "Crew created it" and this says "and it is still
        # Crew's content" -- the re-seed and the reset unlink both require BOTH.
        self._claude_settings_written: str | None = None
        # True while a durable OWNER holder of this client's is still on disk for a
        # file whose bytes are not Crew's: a user replaced the seed and the hand-back
        # (``forget``, then ``release``) could not reach the sidecar. Authorship
        # drops the moment the bytes are observed foreign -- governance is about the
        # file, not the record -- so this carries the one thing teardown still owes:
        # another attempt at the hand-back. It feeds NO governance decision.
        self._claude_settings_claim_unrevoked = False
        # True while this session holds a durable reader lease on a sibling
        # Crew client's seed. The lease pins that file's lifecycle even after a
        # later byte re-validation fails, so it is deliberately distinct from
        # whether the current bytes still govern this session's permission
        # surface. A sharer never removes the owner's file or live claim.
        self._claude_settings_shared = False
        # True only while the sibling seed's CURRENT on-disk bytes have passed
        # byte-for-byte validation against this session's rendered permission
        # surface. Unlike the retained reader lease above, every re-validation
        # clears this flag on entry and only full success re-earns it; the MCP
        # array is withheld after every non-success exit.
        self._permission_surface_share_validated = False
        # Identity this client claims its seed under, in the durable record. Two
        # keyless clients share the default work_dir, so a token is what keeps
        # "Crew wrote it" from collapsing into "any Crew client may take it": the
        # durable record is for adopting an ORPHAN, and a sibling still running in
        # this process does not have one. Per instance and never reset -- a client
        # that re-spawns is the same owner across spawns.
        self._seed_owner = uuid.uuid4().hex
        # This session's translated ``mcpServers`` array, resolved once per spawn.
        # Held here so the shared session-params call site is a pure in-memory
        # read: the translation touches disk, and doing that AT the call site
        # would put an executor hop on every backend's construction path
        # (harness-parity H13). None = not resolved yet; cleared on reset so the
        # next spawn re-reads the spec. See _session_mcp_servers.
        self._session_mcp_cache: list[dict[str, Any]] | None = None
        # The ``DerivedSpecSnapshot`` that array was built from, or None when this
        # session's agent mirrors nothing. For an array-backed host the array IS the
        # spec the host consumes -- it reads no spec of its own -- so the bracket the
        # kiro-cli path closes at ``initialize`` closes HERE at the ``session/new`` /
        # ``session/load`` response instead, against this snapshot. Resolved and
        # cleared together with the array, for the same per-spawn freshness reason.
        self._session_mcp_snapshot: DerivedSpecSnapshot | None = None
        # This session's agent spec, snapshotted once per spawn for the
        # unresolved-ref guard alone (see _guard_unresolved_mcp_refs). Held for
        # the same reason as the array above and read at the same kind of site:
        # the guard runs where the wire array is composed, which is shared with
        # kiro-cli, so the read cannot happen there. None = not snapshotted (or
        # unreadable), which makes the guard a no-op rather than a disk read on
        # the loop. Cleared on reset so the next spawn re-reads the spec.
        self._mcp_ref_spec: dict[str, Any] | None = None
        # What the pre-spawn freshness gate verified for THIS child, captured in
        # _spawn and re-verified in _initialize_session once the child has read its
        # spec. _spawn is the only writer and it runs before any handshake, so the
        # value the post-load half reads always belongs to the live process.
        # Deliberately NOT cleared on reset: a retained snapshot can only ever be
        # over-strict, while a None substituted there would short-circuit the
        # re-check on any path that reached it. None = no spawn yet.
        self._derived_spec_snapshot: DerivedSpecSnapshot | None = None
        # The mirror's CLIENT OBLIGATION from the same spec parse as the array
        # (``SessionProjection.denied_tools``): ``(server, tool)`` pairs the spec
        # switched off that the backend cannot refuse on the wire, so this client
        # refuses them when the backend asks permission (``_deny_spec_disabled_tool``).
        # A backend that honours the restriction natively (kiro-cli) or through a
        # file Crew writes (claude's ``permissions.deny``) leaves it empty, and an
        # empty set makes the refusal a no-op. Cleared on reset with the array.
        self._spec_denied_tools: frozenset[tuple[str, str]] = frozenset()
        # The mirror's other CLIENT OBLIGATION from the same parse
        # (``SessionProjection.zero_tools``): the spec declares ``"tools": []``, a
        # total ban the array and ``denied_tools`` above have no way to express --
        # they answer "which MCP servers mount", not "may this agent's own native
        # tools (the harness's built-in Bash/Read/Write) run at all". kiro-cli/KAS
        # honour the ban natively; every mirrored backend does not, so this client
        # refuses EVERY permission request when it is set (``_handle_permission``).
        # Cleared on reset with the array.
        self._spec_zero_tools: bool = False
        # Narrowed servers the projection withheld because THIS session cannot put
        # their per-tool rule in force (``SessionProjection.unhonoured_servers``). No
        # member mount may re-add one, whatever the backend's PerToolDeny says.
        self._session_mcp_unhonoured: frozenset[str] = frozenset()
        # Tool ids the projection asks the harness itself to deny
        # (``SessionProjection.harness_deny_rules``), seeded by the opencode routing.
        self._session_harness_deny_rules: tuple[str, ...] = ()
        # The subset of those rules the opencode read-back found IN FORCE, handed back
        # to every later projection so it withholds a server whose rule is not. None
        # until this spawn has seeded any.
        self._opencode_denies_in_force: frozenset[str] | None = None
        # The seeded rules the last read-back found outranked by another rule.
        self._opencode_denies_unenforced: frozenset[str] = frozenset()
        # Servers opencode mounts from its own config, from this spawn's read-back.
        self._opencode_native_mounts: frozenset[str] = frozenset()
        # The MCP server names opencode's routing read-back found in the harness's
        # own resolved config, so a fused tool title from one of them splits back
        # exactly. Recorded by its launch (``acp.harness.opencode``).
        self._opencode_config_mcp_servers: tuple[str, ...] = ()
        # The per-session nonce a gate extension echoes in every dialog it raises;
        # minted by the pi or DeepSeek Harness launch, placed in the child's
        # environment there, and the only key under which a permission frame is read
        # as an envelope.
        self._pi_gate_nonce = ""
        # The pi MCP handoff file the launch published and its bytes, kept so it can
        # be republished before each request that starts a pi process and removed
        # once the session is established (``acp.harness.pi``).
        self._pi_mcp_handoff: tuple[str, bytes] = ("", b"")
        # Every server name that handoff carried (the member mounts AND the pooled
        # stubs), which is what a pi MCP tool title is spelled after.
        self._pi_mcp_server_names: tuple[str, ...] = ()
        self._deepseek_gate_nonce = ""
        # toolCallIds the gate extension asked about in this session, read off the
        # envelopes; a completed tool call not in it is a call the gate never saw.
        self._pi_gate_asked_ids: set[str] = set()
        # request id -> toolCallId for every envelope the gate asked about, so a
        # reject answered by request id can name the call it denied; and the set
        # of denied calls, which must never reach ``completed``.
        self._pi_gate_request_tool: dict[str, str] = {}
        self._pi_gate_denied_ids: set[str] = set()
        self._session_key = session_key
        # When set, this client emits a per-tool-call SEL audit from the ACP
        # dispatch loop. Used by app/worker-pool clients (e.g. code-review-sage,
        # knowledge llm_pool) that have no external audit loop. Left None for
        # chat / subagent clients, which already audit via chat_runner /
        # SubagentManager, so they never double-log.
        self._audit_source = audit_source
        self._channel_id = channel_id
        self._extra_env = extra_env or {}
        # ``agent.env`` for the spawn in progress; see _agent_env_for_spawn.
        self._agent_env_snapshot: dict[str, str] | None = None
        # MCP gateway overlay: when set, the broker stubs in its rewritten specs
        # are injected into this session at ACP session/new, where they outrank
        # the same-named entries in the agent spec. Nothing is written to the
        # user's project or to ~/.kiro/agents. None = pooling off.
        # Broker requests carry the owning session through ordinary transport
        # authentication; member memory uses that session's execution record.
        self._mcp_gateway_overlay = str(mcp_gateway_overlay) if mcp_gateway_overlay else None
        self._mcp_gateway_socket = str(mcp_gateway_socket) if mcp_gateway_socket else None
        # Token this client's injected broker-stub entries carry, so gatewayd can
        # tell this session's stub connections from those of another session on
        # the same runtime PID (``mcp_gateway.claim.mint_stub_session_token``).
        # Minted once per client: one client drives one child process serving one
        # session at a time, and a warm-pool ``rekey()`` re-binds the SAME token
        # to the claiming session rather than re-minting — the stub processes
        # outlive a single chat, so a fresh token would leave the live ones
        # carrying a name no claim will ever mention again. Never logged.
        self._stub_session_token = mint_stub_session_token()
        self._sandbox_cleanup: str | None = None
        self._bound_workspace_fd: int | None = None
        self._spawn_work_dir = str(self._work_dir)
        self._process: asyncio.subprocess.Process | None = None
        self._pid: int | None = None
        self._kill_sent: tuple[int, str] | None = None  # (signal, reason) we last sent
        # The root's process-start identity, read once at spawn and handed to
        # both the session-file tracker and the identity-bound retirement in
        # _reset_state, so the line written and the line later compared are one
        # read of the identity rather than two (see session_pid._pid_start_token).
        self._spawn_start_token: str | None = None
        # False until shutdown confirms both the root's exit and every tracked
        # descendant's exit. A work-directory reclaim reads this fail-closed
        # verdict after shutdown.
        self._process_tree_confirmed_dead = False
        self._start_time: str | None = None  # start identity for PID-recycle detection
        # Names THIS spawn of the child, not the session it serves: a resume
        # re-uses the session id on a brand-new process (see ensure_ready's
        # session/load path), so the session id cannot distinguish the process
        # that minted a resource from a successor that cannot honor it. Fresh
        # per spawn, cleared with the process. See ``process_instance``.
        self._process_instance: str = ""
        self._session_id: str | None = None
        self._next_id = 1
        self._buffer: deque[JsonRpcMessage] = deque(maxlen=100)
        self._mcp_notifications: list[JsonRpcMessage] = []
        # What THIS session's MCP servers reported at init. The frames arrive
        # during _drain_notifications; reducing them to one log line and dropping
        # them would leave a session that started without a server unable to say
        # so. Read via mcp_session_report().
        self._mcp_report = McpSessionReport()
        #: Index into ``_mcp_notifications`` below which frames belong to a PRIOR
        #: session attempt and must not reach the report. See
        #: ``_begin_session_report``.
        self._mcp_report_frame_floor = 0
        #: Elements the last array build left out as unstartable, recorded on the
        #: report by ``_begin_session_report``. See ``_prune_unstartable_mcp``.
        self._mcp_pruned: tuple[PrunedServer, ...] = ()
        #: Names of the servers this attempt pruned, set by ``_begin_session_report``.
        #: The ref guard counts them as configured: each is already on the report as
        #: failed, and calling it unconfigured too would point at the projection.
        self._mcp_pruned_names: tuple[str, ...] = ()
        #: How many pruned servers the last array build had past
        #: ``MCP_REPORT_BUCKET_CAP``; counted on the report, never listed.
        self._mcp_pruned_omitted = 0
        # MCP OAuth requests collected during session init from
        # `_kiro.dev/mcp/oauth_request` notifications. Drained by callers via
        # `pop_pending_oauth_requests()` after `ensure_ready()` so the UI can
        # surface an Authorize button to the user. Each entry: {"serverName", "oauthUrl"}.
        self._pending_oauth_requests: list[dict[str, str]] = []
        # Server names already surfaced to the UI in this ACP session — kiro-cli
        # may emit `_kiro.dev/mcp/oauth_request` multiple times per server (e.g.
        # once per probe attempt). Dedupe so the user sees one banner per server.
        self._oauth_emitted_servers: set[str] = set()
        self._cancelled = False
        self._cancel_ts: float = 0.0
        # Cooperative-cancel read-grace for the CURRENT cancel. Defaults to the
        # module floor but is raised to the caller's ack budget by
        # cancel_session() so a configured soft_stop_budget_secs > 10 actually
        # extends the window instead of being silently capped (the read loop
        # would otherwise abort the turn at 10s while the soft waiter blocked
        # the full budget, then hard-kill — losing the session).
        self._cancel_grace_secs: float = _CANCEL_GRACE_SECS
        self._resume_session_id: str | None = None
        # (source session id, fork-point message id) for the next ensure_ready().
        self._fork_request: tuple[str, str] | None = None
        self._resumed = False
        self._can_load_session = False
        # agentInfo.version from the initialize response — the version the
        # spawned process runs, not the file on disk. "" until the handshake.
        self._agent_version = ""
        # Whether ``_agent_version`` has been read off a handshake at all, so an
        # adapter that reported NO version is told apart from one not yet asked.
        self._agent_version_read = False
        # Models advertised by the backend in the session/new (or session/load)
        # response. claude-agent-acp returns the real versioned Claude list
        # (Opus 4.8/4.7, Sonnet 4.6, …); kiro-cli returns its own. Captured so
        # the dashboard model dropdown reflects what the backend actually
        # offers rather than a hardcoded guess. Each entry: {modelId, name,
        # description}.
        self._available_models: list[dict[str, str]] = []
        # When that snapshot was captured (monotonic) and whether a live re-probe
        # confirmed it. A ``session/new`` answer is one unconfirmed reading taken
        # inside the startup race where an entitlement lookup can answer the
        # free-tier default; only :meth:`refresh_available_models` confirms it.
        self._available_models_captured_at: float = 0.0
        self._available_models_probe_confirmed: bool = False
        # Set by _capture_available_models (claude only) when the discovered ids
        # changed the cross-session provider-model cache, signalling the async
        # init path to offload a disk persist. Reset to False after each persist.
        self._advertised_models_changed: bool = False
        # Mode ids the backend advertised at session init (session/new|load
        # `modes.availableModes`). Empty when the backend omits `modes` (older
        # kiro-cli / offline fake) — the set_mode guard treats empty as "attempt"
        # for backward compatibility. Populated by _store_session_config.
        self._available_mode_ids: list[str] = []
        # Whether the backend advertised a `modes` list at all (even an empty
        # one). Distinguishes "unknown, attempt for backward compat" (False)
        # from "advertised zero/some modes, honor the list" (True) so an
        # explicitly-empty availableModes fails closed rather than attempting.
        self._modes_advertised: bool = False
        # Model kiro-cli/claude-agent-acp actually resolved to (may differ
        # from self._model when that's the "auto" sentinel). Used to look up
        # the context window when usage_update isn't sent (see _track_metadata).
        self._resolved_model_id: str | None = None
        # The model a ``_kiro.dev/agent/switched`` frame says the session now
        # runs, waiting to be judged by ``_judge_activated_model`` before the next
        # prompt. ``""`` = nothing to judge. See ``_note_agent_switched``.
        self._activated_model: str = ""
        # True from the start path's ``session/set_mode`` until the first
        # agent/switched frame after it, and whether a ``session/set_model``
        # went out in between (that later write is what the session runs).
        self._mode_frame_awaited: bool = False
        self._mode_frame_superseded: bool = False
        # Model the backend last substituted to via the -32603 admin-tier policy
        # advisory ("Using X instead"). Set by _wait_for_response when it sees the
        # advisory; consumed by the session/new path to re-issue creation on the
        # model the gateway will actually serve (the advisory carries no
        # sessionId, so the first attempt creates nothing). None = no substitution.
        self._last_substitution_model: str | None = None
        # The pinned model a startup config-option push was refused on, or a
        # startup pin kiro withheld because the account does not serve it, or
        # ``""``. Neither raises: the session stays on the backend default.
        # Callers that bill or label a turn by the pin read this to learn the
        # pin never ran.
        self.model_pin_refused: str = ""
        # The bare model a ``<model>[<effort>]`` pin landed as when its effort
        # half was refused, or ``""``. Set by ``_push_model_via_effort_split``.
        self.model_pin_partial: str = ""
        self._child_pids: dict[int, ChildRecord] = {}  # pid → (start_time, basename)
        self.last_prompt_stats = AcpPromptStats()
        self._tool_call_inputs: dict[str, str] = {}
        # Same-key provenance for the redacted display cache.  No removed bytes
        # are retained here; approval surfaces only need to know that the value
        # they received was not the complete command the provider requested.
        self._tool_call_input_redacted: dict[str, bool] = {}
        # Map toolCallId → is_shell, cached from the tool_call notification so
        # the later permission_request event (which carries no kind) can inherit
        # the canonical shell signal. Mirrors _tool_call_inputs lifecycle.
        self._tool_call_is_shell: dict[str, bool] = {}
        # Map toolCallId → "no channel classified this call". A tool_call frame
        # answers the question "is this a command?" through an ACP ``kind`` or
        # through a harness ``_meta`` channel; a frame carrying NEITHER leaves the
        # command gates with nothing to check, and the permission request that
        # follows carries no kind of its own. Remembered here so that request can
        # be refused rather than answered blind. Mirrors _tool_call_is_shell's
        # lifecycle.
        self._tool_call_unclassified: dict[str, bool] = {}
        # Skill bodies this session's tool calls read directly, credited to the
        # usage ledger once each read completes. Shared with AcpSessionHandle so
        # the credit does not depend on the transport. Mirrors
        # _tool_call_is_shell's lifecycle.
        self._skill_reads = SkillReadTracker()
        # Map toolCallId → trusted MCP server name (_meta.kiro.mcpServerName),
        # cached from the tool_call notification so the later permission_request
        # event (which carries no _meta) can inherit it — the signal the
        # app-own-server auto-approve keys on. Mirrors _tool_call_is_shell.
        self._tool_call_mcp_server: dict[str, str] = {}
        # Map toolCallId → trusted tool name (_meta.kiro.toolName), cached like
        # _tool_call_mcp_server so the permission_request event can rebuild the
        # canonical mcp__<server>__<tool> for per-tool governance in the
        # app-own-server auto-approve.
        self._tool_call_tool_name: dict[str, str] = {}
        # toolCallId -> the tool's own name its tool_call frame stated, for the
        # permission event's harness_tool_id (see _dispatch.harness_tool_name).
        self._tool_call_harness_tool_name: dict[str, str] = {}
        # toolCallId -> True when the tool_call frame PRESENTED an MCP identity half
        # the name bound refused (``mcp_identity_unreadable``); the permission
        # request is then refused in ``_handle_permission`` rather than judged
        # under the empty name. Mirrors AcpSessionHandle._tool_call_identity_unreadable.
        self._tool_call_identity_unreadable: dict[str, bool] = {}
        # Structured raw tool params (rawInput dict) keyed by toolCallId, cached
        # from the ToolCall notification so the later request_permission event —
        # which carries only a truncated title — can recover the real path/url
        # the governance gate needs (filesystem.write / network.egress scopes).
        self._tool_call_params: dict[str, dict] = {}
        # toolCallId -> path named by the tool_call's diff content block, so the
        # permission event can carry diff_path for the edit gate when the params
        # themselves carry no path key. Mirrors AcpSessionHandle's cache.
        self._tool_call_diff_path: dict[str, str] = {}
        # Map JSON-RPC request id → {"once": optionId, "always": optionId} so
        # the host can echo back the exact optionIds the agent advertised.
        # kiro-cli uses "allow_once"/"allow_always"; claude-agent-acp uses
        # "allow"/"allow_always". Falling back to OPTION_ALLOW_ONCE causes
        # claude-agent-acp to reject the response.
        self._permission_options: dict[str | int, dict[str, str]] = {}
        # Request id -> the permission event built for it, so approve_tool can
        # put the request through the security floor (``permission_floor``)
        # whichever consumer answers it.
        self._permission_gate_events: dict[str | int, AcpEvent] = {}
        self._stderr_lines: deque[str] = deque(maxlen=20)
        # Latched on this process's FIRST non-thinking text chunk, tool call or
        # tool result, cleared with the rest of the process state on respawn:
        # the registration-throttle death classification is refused once work
        # has been observed, so the transient verdict it hands the retry
        # ladders can only ever license replaying a turn that provably did
        # nothing. Per PROCESS (this client owns exactly one), matching the
        # ring the evidence is read from.
        self._prompt_or_tool_seen = False
        # Set by ``_spawn`` from the wrapped argv; only meaningful once a child
        # has been spawned. False before that, which is also the safe default for
        # the classifier: a spawn that never reached the wrap cannot have been
        # refused by it.
        self._sandbox_wrapped_by_crew: bool = False
        # The mask set this spawn asked for, so a trusted corroboration run can
        # exercise the same mounts rather than a weaker profile.
        self._sandbox_hidden_dirs: tuple[str, ...] = ()
        self._jsonl_pos: int = 0  # track read position in session JSONL for tool results
        self._stderr_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._last_activity: float = time.monotonic()
        # Set when a request write stalls (``_write_request_bounded``): the child
        # stopped reading stdin and may still read a buffered frame. The client
        # then reads as neither responsive nor mid-turn, and the next
        # ``ensure_ready`` kills it before spawning a replacement, so no new
        # prompt is ever appended behind the stalled one.
        self._stdin_stalled = False
        # Idle == done. An idle client (spawned, no prompt sent yet) must read
        # as NOT in a turn: has_active_turn() is the 409 turn_in_flight gate
        # on set-model / set-agent, so an unset Event on a live warm process
        # blocks the user until a real turn's finally runs. Every prompt
        # entry clear()s this before sending, so a real turn still reads active.
        self._turn_done: asyncio.Event = asyncio.Event()
        self._turn_done.set()
        # goose steer bookkeeping (``_steer_via_request``): the running prompt's
        # announced run id, steer requests awaiting their answer by request id,
        # and accepted steers awaiting their echo by goose ``messageId``.
        self._active_run_id: str | None = None
        self._steer_answers: dict[int, tuple[asyncio.Future[str], str]] = {}
        self._steers_awaiting_echo: dict[str, str] = {}
        # Serializes whole read turns on this client's single stdout StreamReader.
        # An asyncio StreamReader permits exactly ONE waiting reader; the shared
        # `_bg` session is streamed by ~8 callers and the per-session Semaphore(1)
        # does not cover abnormal-exit overlap (a turn dying mid-readline leaves a
        # parked read the next caller collides with -> "readuntil() called while
        # another coroutine is already waiting"). Acquired at the top of
        # _prompt_loop, released in its finally.
        #
        # Finalization caveat: a consumer that `return`s on "complete" without
        # exhausting _prompt_loop leaves the async-gen SUSPENDED; CPython runs
        # its finally via a *deferred* scheduled athrow (next loop tick), not at
        # the consumer's return. So the lock releases promptly on a live loop but
        # NOT synchronously. send_message_stream wraps the loop in `aclosing(...)`
        # to force deterministic release on its hot path; the other consumers
        # rely on the next-tick finalization.
        #
        # Coverage caveat: this lock only covers reads inside _prompt_loop.
        # _read_message also has callers OUTSIDE the loop (_wait_for_response
        # during init, wait_for_compaction). Those run in distinct lifecycle
        # phases that do not overlap a streaming _bg turn, so they are not
        # serialized here; if that ever changes, the readuntil race could recur.
        self._turn_lock: asyncio.Lock = asyncio.Lock()
        self._stale_eligible: bool = False  # set by _dispatch_events after text chunks
        # Set when a tool_call is yielded, cleared when the tool resolves
        # (tool_call_update result) or the turn starts/completes.  NOT cleared
        # on arbitrary inbound frames — that would disarm the watchdog after a
        # single progress frame.  Gates the _TOOL_STALL_TIMEOUT watchdog so a
        # dispatched-but-never-resolved tool can't hang the whole turn.
        self._tool_dispatched: bool = False
        # The open calls' ids, bounded like the session handle's attribution
        # map (``_dispatch.InFlightIdSet``: the shared key and count bound, the
        # oldest evicted first, evictions counted). After an eviction the set
        # answers ``possibly_open`` until the turn resets, so the tool-stall
        # clock (``_TOOL_STALL_TIMEOUT``) stays armed and the stale clock
        # disarmed for a call whose row was dropped but may still be running.
        self._active_tool_calls = InFlightIdSet()
        # Armed by _handle_compaction_status on a `failed` status and cleared at
        # turn start: gates the _COMPACTION_FAILED_TURN_BUDGET check in
        # _prompt_loop. _compaction_failed_turn records that the check fired, so
        # _dispatch_events ends the turn with the compaction stop reason instead
        # of the generic timeout error.
        self._compaction_failed_at: float | None = None
        # Retryability of the LAST failed compaction, read by the dashboard's
        # STOP_REASON_COMPACTION_FAILED branch to decide between re-queuing the
        # abandoned message and giving up. Public (no leading underscore)
        # because that consumer reaches it through getattr on whichever of the
        # two client classes is serving the slot. Only the VERDICT is carried:
        # the reason text reaches the chat row through the compaction-status
        # event title and the server log through the WARNING each arming site
        # already emits, so forwarding it as well would widen the provider
        # contract for no reader.
        self.last_compaction_transient: bool = False
        self._compaction_failed_turn: bool = False
        # Set when the claude backend's "Compacting..." notice is seen inside a
        # turn and cleared by its terminal notice. Only a MANUAL /compact gets a
        # terminal from the adapter (see parse_claude_compaction_notice), so an
        # automatic mid-turn compaction leaves this armed and the dispatch loop
        # settles it with a synthetic `completed` at turn end. Without that, a
        # consumer showing a compacting state would never leave it.
        self._claude_compaction_pending: bool = False
        # The codex twin of the flag above. It guards TWO directions, and the
        # second is why it is read on the way in as well as at turn end. A loaded
        # session replays a past compaction as a ``tool_call`` that is already
        # ``completed``, so only a terminal following a ``started`` seen in THIS
        # turn describes work this turn did. And a compaction that ERRORS reports
        # nothing at all -- codex-acp's ``runCompact`` never resolves, so the
        # ``session/prompt`` request goes unanswered -- which leaves this armed
        # for ``_settle_codex_compaction`` to close out at the turn's terminal.
        self._codex_compaction_pending: bool = False
        # When this session's harness last launched work that outlives the
        # prompt (a backgrounded command, a Workflow). Never reset per turn: a
        # turn ending says nothing about whether that work has finished, and
        # this client reads its pipe only while a call is waiting, so the
        # harness's eventual report of it is not seen until the next prompt.
        self._background_launches = BackgroundLaunchRecord()
        # Liveness oracle for the stale-turn gate: before ending a silent turn
        # at _STALE_TURN_TIMEOUT, consult /proc evidence so a backend that is
        # provably working (CPU/IO movement in the subprocess subtree) is not
        # reaped. Mirrors the kiro shared-runtime path (AcpSessionHandle), which
        # already defers on a WORKING verdict instead of a blunt wall-clock.
        self._liveness_oracle = LivenessOracle()
        # Keep the executor future, not an await-scoped flag: wait_for can time
        # out while the underlying thread continues its /proc walk. A pending
        # future prevents silent-read polling from stacking blocked workers.
        self._consult_future: asyncio.Future[tuple[str, str]] | None = None
        # Record every observed tool_call (id -> (title, kind)) so the
        # PostToolUse hook fire can recover the tool_name from the result's
        # tool_call_id — the RESULT event carries no title. See
        # _maybe_fire_post_tool_hooks.
        self._observed_tool_calls: dict[str, tuple[str, str]] = {}
        self._last_stop_reason: str = ""
        # Dynamic config from ACP session/new response and config_option_update notifications.
        # Only the effort configOptions are consumed (model lists come from
        # _capture_available_models, which parses the real dict-shaped `models`).
        self._acp_config_options: list[dict] = []

    @property
    def backend(self) -> str:
        """ACP backend identifier (e.g. ACP_BACKEND_CLAUDE for claude-agent-acp)."""
        return getattr(self, "_acp_backend", "")

    @property
    def agent_version(self) -> str:
        """``agentInfo.version`` reported at ``initialize`` (``""`` until then).

        The version this process RUNS — see :attr:`AcpRuntime.agent_version`.
        """
        return getattr(self, "_agent_version", "")

    @property
    def spec_zero_tools(self) -> bool:
        """True once this session's CONSUMED mirror projection confirms the spec's ``"tools": []``.

        Set from :attr:`_spec_zero_tools`, which only becomes True after the session's
        projection has been parsed and reports the agent spec actually in force declares
        an empty tool list, so :meth:`_deny_zero_tools` refuses every permission request
        the session raises. Stays False for the lifetime of a session whose spec is
        missing, malformed, or shadowed by another one, and always False on a backend
        with no mirror at all, where the harness itself reads the spec and enforces the
        ban natively rather than through this client.
        """
        return self._spec_zero_tools

    def authored_spec_declares_zero_tools(self) -> bool:
        """True when the authored spec this agent name resolves to declares ``"tools": []``.

        Reads the spec through :func:`agent_spec_snapshot`, project checkout first and
        user level second, which is the order the harness resolves ``--agent`` in, so
        a project-level spec that shadows the installed one is the one judged.

        Blocking (reads the spec); async callers run it with ``asyncio.to_thread``.

        Fails closed: an unreadable spec, a non-dict result, and a ``tools`` that is
        missing, not a list, or non-empty all answer False.
        """
        try:
            spec = agent_spec_snapshot(self._agent, work_dir=self._work_dir)
        except Exception:
            logger.warning(
                "agent spec for %r unreadable; not confirming zero tools",
                self._agent,
                exc_info=True,
            )
            return False
        if not isinstance(spec, dict):
            return False
        tools = spec.get("tools")
        return isinstance(tools, list) and not tools

    def effective_spec_declares_zero_tools(self, *, authored_before_spawn: bool = False) -> bool:
        """True when the spec the harness runs under declares an empty ``tools`` list.

        With a prepared projection the answer comes from the view handed to the
        harness through ``--agent``. The session prepares that view before the spawn
        and never re-reads it, so the view is immutable and authoritative: a change
        to the authored spec after the harness consumed it cannot change the answer,
        and *authored_before_spawn* is ignored. The view covers a project-level spec
        that shadows the installed one and any tool the projection adds, such as
        skill search for a spec carrying ``skill://`` resources. Unlike
        :attr:`spec_zero_tools` it needs no mirror, so it can confirm the ban on a
        backend that enforces it natively.

        With no prepared projection (the projection switched off, or its alias
        could not be taken) the harness reads the named spec itself. The caller
        reads that spec before the spawn and passes the result as
        *authored_before_spawn*; this method reads it again after the harness
        started, and both reads must declare an empty list. The bracket narrows a
        change to the authored file but cannot close one made and restored inside
        the spawn window, which is why a prepared view is preferred.

        Fails closed: a projection with no view for this agent or a view whose
        ``tools`` is missing or non-empty is unconfirmed, and so is a missing
        projection whose pre-spawn read was not confirmed or whose post-start read
        is not an empty list.

        The no-projection branch reads a file, so async callers run this method with
        ``asyncio.to_thread``.
        """
        projection = getattr(self, "_native_skill_projection", None)
        if projection is None:
            return authored_before_spawn and self.authored_spec_declares_zero_tools()
        view = projection.specs.get(self._agent)
        if not isinstance(view, dict):
            return False
        tools = view.get("tools")
        return isinstance(tools, list) and not tools

    @property
    def _enforces_zero_tool_ban(self) -> bool:
        """True when the spec's ``"tools": []`` is refused at the permission request.

        Needs both facts: the session's spec declares the empty list, and the backend
        honours it as a ban on native tools too. A backend that does not honour it
        leaves the refusal unreached on some paths, so it is not applied at the sites
        that do run.
        """
        return self._spec_zero_tools and capabilities_for(self.backend).honors_zero_tool_ban

    @property
    def _judges_permission_requests(self) -> bool:
        """True when a permission request on this session is JUDGED before it is answered.

        Two facts make an answering site build the full event and consult the provenance
        caches instead of approving on the frame alone: a spec deny set (a switched-off
        tool must be refused wherever it asks), and membership in
        ``ACP_BACKENDS_META_IDENTITY`` (the harness publishes a tool identity on every
        frame, and a frame that carries none, or one naming a server Crew never mounted,
        is refused). The second is a fact about the channel, not about the deny set: the
        goose projection carries an empty deny set by ruling, so a refusal that waited
        for one would never run on a goose session at all. Everywhere else the sites keep
        their prior behaviour byte for byte.

        A third fact judges too: :attr:`_enforces_zero_tool_ban`, the agent's own
        ``"tools": []`` on a backend that honours it. It is not folded into the
        deny-set check above because it answers a broader question -- every request,
        MCP or native -- and a spec with no MCP servers to deny at all (the ordinary
        case for a zero-tool agent) would otherwise leave ``_spec_denied_tools`` empty
        and this property False, which is exactly the auto-approve path the zero-tool
        ban exists to close.
        """
        return (
            bool(self._spec_denied_tools)
            or self._enforces_zero_tool_ban
            or self.backend in ACP_BACKENDS_META_IDENTITY
        )

    @property
    def _is_claude(self) -> bool:
        return self.backend == ACP_BACKEND_CLAUDE

    @property
    def _is_opencode(self) -> bool:
        return self.backend == ACP_BACKEND_OPENCODE

    @property
    def _is_pi(self) -> bool:
        return self.backend == ACP_BACKEND_PI

    @property
    def _is_goose(self) -> bool:
        return self.backend == ACP_BACKEND_GOOSE

    @property
    def _is_deepseek(self) -> bool:
        return self.backend == ACP_BACKEND_DEEPSEEK

    @property
    def _model_registry_namespace(self) -> str:
        """The model_registry namespace key for this backend (``claude_code`` /
        ``acp``). A registry index selector, NOT a provider-identity check — see
        agent_sdk.provider_identity note 3. Used to fold the wire model id and
        seed the allowlist against the right index for whichever backend is a
        member of ``ACP_BACKENDS_ADVERTISED_MODEL_SELECTION``."""
        return model_registry_namespace(self.backend)

    @property
    def _uses_advertised_model_selection(self) -> bool:
        """True when this backend sources its wire model id / seed from the
        provider's advertised list (see ``ACP_BACKENDS_ADVERTISED_MODEL_SELECTION``)."""
        return self.backend in ACP_BACKENDS_ADVERTISED_MODEL_SELECTION

    @property
    def _seeds_local_settings(self) -> bool:
        """True when this backend seeds (and must re-seed) a per-session settings
        file (see ``ACP_BACKENDS_SEED_LOCAL_SETTINGS``)."""
        return self.backend in ACP_BACKENDS_SEED_LOCAL_SETTINGS

    @property
    def _is_kiro(self) -> bool:
        """True when this client drives kiro-cli (the AcpClient default).

        AcpClient serves kiro-cli, claude-agent-acp and the dormant codex seam, so
        this is the positive spelling of the sites that used to read
        ``not self._is_claude`` (harness-parity H5). KAS runs on AcpRuntime, not
        AcpClient, so it never reaches this property.
        """
        return self.backend == ACP_BACKEND_KIRO

    def _pooled_mcp_servers(self) -> list[dict[str, Any]]:
        """Broker-stub ``mcpServers`` entries for this session's ``session/new``.

        A session-injected server outranks the same-named entry in the resolved
        agent spec, so injecting the stubs here is what actually pools the
        servers — nothing is written to the user's project or to
        ``~/.kiro/agents/``. Empty when the shared gateway is disabled.

        Empty for EVERY mirrored backend, with no per-backend branch: a mirror's
        projection places the stubs itself (``_resolve_session_mcp_servers`` hands
        them down as ``stub_elements``), so one withhold rule -- the spec's
        ``tools`` allowlist, codex's restriction set, claude's permission-surface
        precondition -- covers both halves of the array. A second unnarrowed
        append here would re-add, as an UNRESTRICTED broker stub, exactly the
        servers that projection withheld: a stub carries the same name as the
        entry it rewrites, so the withhold and the re-add are the same server.

        The shared append remains for the mirror-less backends: kiro-cli reads
        the spec itself via ``--agent``, so this array is the ONLY channel its
        stubs can arrive on, and the injection outranking the spec entry is what
        pools them there.

        Membership in ``MIRRORS`` rather than ``mirror_for``, because this sits
        on the shared session/new / session/load composition: ``mirror_for``
        RAISES for a backend registered in neither map, and kiro's construction
        path must not gain a failure mode in service of an adapter (H13).
        """
        if self.backend in MIRRORS:
            return []
        # A mirror-less member backend's own array (``_mirrorless_member_mounts``)
        # sits beside these stubs on the wire. Its entry carries the member's
        # identity, so a same-named stub is dropped rather than sent twice.
        mounted = {
            element.get("name")
            for element in (getattr(self, "_session_mcp_cache", None) or [])
            if isinstance(element, dict)
        }
        return [s for s in self._pooled_broker_stubs() if s.get("name") not in mounted]

    def _resolve_session_mcp_servers(self) -> list[dict[str, Any]]:
        """Translate the agent spec into this session's ``mcpServers`` array.

        Blocking (reads the agent spec and the gateway overlay), so it runs off
        the loop from the spawn path and its result is cached for the call sites
        — see :meth:`_session_mcp_servers` for why the call sites must not do
        this work themselves.

        The names of the pooled broker stubs the caller appends are resolved here
        and passed down, because this layer is the one that holds the overlay: a
        stub wraps — and is keyed by — the same name as the agent-spec entry it
        rewrites, so translating both halves would put two elements with one
        ``name`` into a single array (either the raw entry shadows the stub and the
        session bypasses the broker, or both register and every pooled backend runs
        twice — #927). Empty on error is the safe direction, the same one the KAS
        projection takes: it re-declares a stubbed server, where the injection
        still outranks it, rather than withholding a server nothing else supplies.

        ``permission_surface_owned`` carries whether Crew GOVERNS this session's
        native permission file, because a mirror cannot know that on its own:
        either this client authored it, or it verified the file is a sibling
        session's byte-identical live seed (``_permission_surface_governed``). It
        is a precondition on delivering tools at all: Crew's gate fires on
        ``session/request_permission``, and a tool pre-approved in a file Crew does
        not govern never sends one. The claude mirror fails closed on it; a backend
        that gates natively ignores it. Read here, AFTER the writer has run on the
        spawn path, so the value describes this session's real state.
        """
        try:
            stubbed: Collection[str] = injection_server_names(
                # The same checkout the projection below resolves the agent SPEC
                # against: a project agent's stubs must be read from the file the
                # session is running, not from the user-level agent of that name.
                self._mcp_gateway_overlay,
                self._agent,
                **overlay_project_scope(self.backend, self._work_dir),
            )
        except Exception:
            logger.warning(
                "could not resolve pooled stub names; the session MCP array may re-declare one",
                exc_info=True,
            )
            stubbed = frozenset()
        mirror = mirror_for(self.backend)
        if mirror is None:
            return self._mirrorless_member_mounts(stubbed)
        # Recorded BEFORE the projection reads the governed flag, and beside the
        # array it authorizes: the session/new envelope reads this record, so an
        # array delivered under the exclusion always ships with it.
        excludes_local = bool(getattr(self, "_claude_local_settings_excluded", False))
        self._session_mcp_excludes_local = excludes_local
        self._session_mcp_inline_settings = (
            getattr(self, "_claude_inline_settings", None) if excludes_local else None
        )
        # The STRUCTURED face rather than the wire one, for every mirror alike: two
        # things can come out of the one spec parse and only one of them is wire
        # data -- the array, and a per-tool deny set this client enforces at the
        # approval request. The wire dict must not carry the second, and a second
        # parse for it would be the consistency window the projection exists to
        # close. A mirror with nothing off-wire to say answers with an empty set.
        projection = mirror.session_projection(
            self._agent,
            stub_server_names=stubbed,
            # The broker stubs as ELEMENTS, for a mirror that must place them itself
            # so one withhold rule covers both halves of the array (codex). A mirror
            # that leaves them to the shared append ignores them.
            stub_elements=self._pooled_broker_stubs(),
            permission_surface_owned=self._permission_surface_governed,
            work_dir=self._work_dir,
            # A codex stdio child starts from env_clear() plus an allowlist, so
            # Crew's own servers reach it with an identity only if the ELEMENT
            # carries one. A mirror cannot discover either value; the client can.
            session_key=self._session_key or "",
            channel_id=self._channel_id or "",
            # This session's own name, so its control-plane elements resolve
            # identity through the signed mapping rather than through an env key
            # a warm-pool rekey can leave stale.
            session_token=self._stub_session_token,
            # The deny rules the opencode read-back found in force (None before any
            # were seeded), so a later re-parse withholds a server whose rule is not.
            harness_denies_in_force=getattr(self, "_opencode_denies_in_force", None),
        )
        self._spec_denied_tools = projection.denied_tools
        self._session_mcp_unhonoured = projection.unhonoured_servers
        self._session_harness_deny_rules = projection.harness_deny_rules
        self._spec_zero_tools = projection.zero_tools
        # Kept beside the array it describes, so the post-consume check judges the
        # generation these elements were built from and not a later read of the file.
        self._session_mcp_snapshot = projection.derived_spec_snapshot
        servers = projection.params.get("mcpServers") or []
        out = list(servers) if isinstance(servers, list) else []
        # The restriction half of the projection's withhold set, from the SAME parse the
        # array came out of: the member append must not re-add a name this projection
        # refused on a transport where the withhold is the whole of the enforcement.
        return self._append_member_panel_server(
            self._append_member_dispatch_server(
                out, projection.restricted_servers, projection.disabled_servers
            ),
            projection.restricted_servers,
            projection.disabled_servers,
        )

    def _mirrorless_member_mounts(self, stubbed: Collection[str]) -> list[dict[str, Any]]:
        """The member mounts of a backend with no mirror, which are its whole array.

        A mirror-less backend translates no spec, so the array Crew composes for it
        carries only what Crew adds on its own: the member session-control and panel
        entries. Empty for every backend outside both member sets, and for every
        non-member session, so an ordinary session's array is unchanged.

        The two switch-offs still bind, read from one parse of the spec and the
        global settings file. ``disabled`` reaches the appends as it does on a
        mirrored backend. A per-tool narrowing is recorded as UNHONOURED, so the
        whole server is withheld: no mirror translates the spec's deny set here, so
        nothing could refuse the narrowed tool once the server was mounted.

        Fail-closed: a spec that cannot be read mounts nothing, and the thread runs
        as plain chat.
        """
        if (
            self.backend not in ACP_BACKENDS_MEMBER_DISPATCH
            and self.backend not in ACP_BACKENDS_MEMBER_PANEL
        ):
            return []
        try:
            from kiro_crew.acp.session_mcp import session_mcp_projection

            projection = session_mcp_projection(
                self._agent, stub_server_names=stubbed, work_dir=self._work_dir
            )
        except Exception:
            logger.warning(
                "member session %s: agent spec unreadable, so no member tools are "
                "mounted -- the DM thread runs as plain chat this session",
                self._session_key,
                exc_info=True,
            )
            return []
        narrowed = frozenset(server for server, _tool in projection.disabled_tools)
        self._session_mcp_unhonoured = narrowed
        disabled = projection.disabled_servers
        return self._append_member_panel_server(
            self._append_member_dispatch_server([], narrowed, disabled),
            narrowed,
            disabled,
        )

    def _pooled_broker_stubs(self) -> list[dict[str, Any]]:
        """The raw broker stubs for this session, with no per-backend narrowing.

        Split from :meth:`_pooled_mcp_servers` so codex can take them through its
        own withholding rules while that method keeps returning ``[]`` for codex at
        the shared call site. Blocking; both callers are already off the loop.

        Each entry carries this client's stub session token, which is what a
        claim names so gatewayd re-targets the stubs of THIS session. One client
        drives one kiro-cli process serving one session at a time, so the token
        is per client and is re-bound — not re-minted — by every ``rekey()``.
        """
        return attach_stub_session_token(
            pooled_session_servers(
                self._mcp_gateway_overlay,
                self._agent,
                self._channel_id,
                **overlay_project_scope(self.backend, self._work_dir),
            ),
            self._stub_session_token,
        )

    def _append_member_dispatch_server(
        self,
        servers: list[dict[str, Any]],
        restricted: Collection[str] = (),
        disabled: Collection[str] = (),
    ) -> list[dict[str, Any]]:
        """Mount the dashboard session-control server into a member DM session.

        Session-level and additive: the on-disk agent spec is untouched, so every
        other session on the same agent keeps its ordinary tool set. The entry
        carries ``KIROCREW_SESSION_KEY`` for strict identity — the same value this
        client already exports to the child process env.

        The permission-surface precondition is asked of the BACKEND's ROUTING --
        ``acp_tool_gate.is_enforced`` -- rather than read off
        ``_claude_settings_authored`` alone. That flag answers one harness's question:
        claude declares a routing this core does not enforce, so owning
        ``settings.local.json`` is what stands in for the read-back it lacks, and
        appending session control onto a surface Crew does not own would hand a
        pre-approvable file exactly the tools the mirror's withhold keeps off it. A
        harness whose routing IS enforced cannot satisfy that flag and does not need
        to -- its session is refused before its first prompt unless the gate arms --
        and its mirror documents the flag as accepted-and-ignored (see
        ``providers/mirrors/opencode.py``), so reading the flag there would withhold
        every member's tools on the strength of a condition that cannot describe the
        backend.

        A whole-server ``disabled`` on the dashboard server stops the mount outright,
        for every backend and with no second channel to weigh: ``disabled`` has no
        per-tool or per-call form, so a harness handed the server cannot refuse a call
        to it, and the ``tools`` allowlist that keeps it out of the spec-described half
        of the array does not reach an element this method appends itself. Mounting it
        anyway would make the operator's switch-off of session control a no-op for the
        one session type that holds the strongest tools Crew hands out.

        The other composer answers alike: ``AcpRuntime`` asks
        ``session_mcp.session_mcp_server_is_disabled`` on its create and resume paths,
        so a member session on an ``ACP_BACKENDS_ACP_RUNTIME`` host (codex, KAS) gets
        the same answer this method gives. It asks through that reader rather than
        through a field on the projection because half of those hosts have no mirror to
        carry one -- KAS projects through ``acp.kas_agents``, not an array.

        A per-tool restriction on the dashboard server is the second precondition,
        and it is the one this method can UNDO rather than merely fail: the projection
        withholds a narrowed server (*restricted*), and on a backend whose
        ``registry.PerToolDeny`` is ``WHOLE_SERVER`` that withhold is the ONLY
        enforcement there is -- no rule in a file the harness reads, and no per-call
        identity Crew can refuse by. Appending the entry back would make a tool the
        operator switched off callable again. So the mount is withheld instead and the
        thread runs as plain chat. A backend with a second channel keeps its mount:
        codex refuses the call at permission time from ``denied_tools``, and claude's
        deny rules refuse it inside the adapter.

        Both halves of a session's array must agree about this. ``AcpRuntime`` mounts
        the same entry on the resume and create paths, and :meth:`_foreign_mcp_identity`
        judges a trusted tool identity against the array THIS method returns -- scoped to
        ``ACP_BACKENDS_SESSION_MCP_ARRAY``, and reached only from
        :meth:`_refuse_identity_drift`, whose own gate is ``ACP_BACKENDS_META_IDENTITY``
        (goose alone today, so no backend mounted here runs it yet). A backend that later
        joins that set and mounts the server on one path while this one withholds it would
        refuse every dispatch call as a drifted server rather than run a plain chat.
        """
        # Every exit below answers whether THIS composition mounted session
        # control, so the context builder can teach the session_* tools only to a
        # session that holds them (see :attr:`member_dispatch_mounted`).
        self._member_dispatch_mounted = False
        if self.backend not in ACP_BACKENDS_MEMBER_DISPATCH:
            return servers
        # circular import: members' module graph is heavy; resolved at call time.
        from kiro_crew.members import (
            MEMBER_DISPATCH_SERVER,
            is_member_session_key,
            member_dispatch_session_server,
        )

        if not is_member_session_key(self._session_key):
            return servers
        session_key = self._session_key or ""
        if self._member_mount_withheld(
            MEMBER_DISPATCH_SERVER, "session control", restricted, disabled
        ):
            return servers
        entry = member_dispatch_session_server(session_key, self._stub_session_token)
        if entry is None:
            logger.warning(
                "member session %s: dashboard server unresolved -- the DM thread "
                "runs as plain chat this session",
                self._session_key,
            )
            return servers
        self._member_dispatch_mounted = True
        return [e for e in servers if e.get("name") != entry["name"]] + [entry]

    @property
    def member_dispatch_mounted(self) -> bool | None:
        """Whether this session's array carries the member session-control entry.

        ``None`` until :meth:`_append_member_dispatch_server` has composed an
        array for this client (no evidence yet); after that, the answer that
        composition gave -- False on every withhold, on a non-member session and
        on a backend outside ``ACP_BACKENDS_MEMBER_DISPATCH``.
        """
        return getattr(self, "_member_dispatch_mounted", None)

    def _member_mount_withheld(
        self,
        server_name: str,
        capability: str,
        restricted: Collection[str],
        disabled: Collection[str],
    ) -> bool:
        """Whether a member session-array append must be withheld for *server_name*.

        The three preconditions :meth:`_append_member_dispatch_server` documents at
        length, asked once so both member mounts answer them the same way. Each is
        about the array, not about which capability rides it: a whole-server
        ``disabled``, a per-tool restriction on a backend where withholding is the
        only deny channel there is, and a permission surface Crew does not own.

        *capability* is the phrase the log uses for what the session loses, because
        that is the only part that differs between the two mounts.
        """
        if server_name in disabled:
            logger.warning(
                "member session %s: %s is switched off for this session (disabled), so "
                "%s is not mounted -- no backend can refuse a call to a server it was "
                "handed, and mounting it would undo that switch; re-enable that server "
                "to restore it",
                self._session_key,
                server_name,
                capability,
            )
            return True
        if server_name in getattr(self, "_session_mcp_unhonoured", frozenset()):
            logger.warning(
                "member session %s: one of %s's tools is switched off and this session "
                "could not put that restriction in force, so the projection withheld the "
                "server and mounting it here would make that tool reachable again; %s is "
                "not mounted",
                self._session_key,
                server_name,
                capability,
            )
            return True
        if server_name in restricted and self._withhold_is_the_only_deny_channel():
            logger.warning(
                "member session %s: one of %s's tools is switched off and this backend has "
                "no channel to refuse a call to it, so the projection withheld the server "
                "and mounting it here would make that tool reachable again; %s is not "
                "mounted, so stop narrowing that server to restore it",
                self._session_key,
                server_name,
                capability,
            )
            return True
        # An unenforced routing is the ONLY case the governed-surface fallback answers
        # for; see the precondition paragraph above for why an enforced one must not
        # read it.
        if not acp_tool_gate.is_enforced(self.backend) and not self._permission_surface_governed:
            logger.warning(
                "member session %s: permission surface not Crew-owned -- %s is not mounted",
                self._session_key,
                capability,
            )
            return True
        return False

    def _append_member_panel_server(
        self,
        servers: list[dict[str, Any]],
        restricted: Collection[str] = (),
        disabled: Collection[str] = (),
    ) -> list[dict[str, Any]]:
        """Mount the crew-panel server into a member DM session.

        The sibling of :meth:`_append_member_dispatch_server` and subject to the
        same three array-level preconditions, through the one reader
        :meth:`_member_mount_withheld`. One addition: ``agent.crew_panel``, the
        operator's single withdrawal of the capability, read fail-closed.

        Its own append rather than a widening of the dispatch one, because the two
        capabilities are assigned per server and withdrawn by separate switches: a
        member may hold session control without a panel, or a panel without session
        control, and each mount must answer for itself. For the same reason the
        backend question is read from :data:`ACP_BACKENDS_MEMBER_PANEL`, whose
        membership is argued for THIS capability: harness support for session
        control establishes nothing about the panel (harness-parity H6), so
        reusing the dispatch set would grant one capability on another's evidence.
        """
        if self.backend not in ACP_BACKENDS_MEMBER_PANEL:
            return servers
        # circular import: members' module graph is heavy; resolved at call time.
        from kiro_crew.members import (
            MEMBER_PANEL_SERVER,
            crew_panel_enabled,
            is_member_session_key,
            member_panel_session_server,
        )

        if not is_member_session_key(self._session_key):
            return servers
        if not crew_panel_enabled():
            logger.info(
                "member session %s: agent.crew_panel is off, so the crew panel is not "
                "mounted; the member keeps its other tools",
                self._session_key,
            )
            return servers
        if self._member_mount_withheld(MEMBER_PANEL_SERVER, "the crew panel", restricted, disabled):
            return servers
        entry = member_panel_session_server(self._session_key or "", self._stub_session_token)
        if entry is None:
            logger.warning(
                "member session %s: panel server unresolved -- the member runs without "
                "a panel this session",
                self._session_key,
            )
            return servers
        return [e for e in servers if e.get("name") != entry["name"]] + [entry]

    def _withhold_is_the_only_deny_channel(self) -> bool:
        """Whether withholding a server is this backend's ONLY per-tool deny channel.

        Read from the declaration each mirror already publishes
        (``registry.PerToolDeny``) rather than from a second membership set, so a
        backend cannot answer one way here and another way in the projection that
        performs the withhold.

        Fail-CLOSED on a backend with no declaration at all: ``projection_for`` raises
        for one, and "no declared deny channel" is exactly the case where re-adding a
        withheld server cannot be shown to be safe. The cost of being wrong in this
        direction is a member thread that runs as plain chat.
        """
        from kiro_crew.providers.mirrors import PerToolDeny, projection_for

        try:
            return projection_for(self.backend).per_tool_deny is PerToolDeny.WHOLE_SERVER
        except Exception:
            logger.warning(
                "member session %s: backend %r declares no MCP projection, so its per-tool "
                "deny channel is unknown; treating a withheld server as un-re-addable",
                self._session_key,
                self.backend,
                exc_info=True,
            )
            return True

    def _read_mcp_ref_spec(self) -> dict[str, Any] | None:
        """Snapshot this session's agent spec for the unresolved-ref guard.

        Blocking (reads the spec), so the spawn path runs it off the loop; the
        guard itself then only reads what this left behind.

        Best-effort by construction: every failure resolves to ``None``, which
        makes the detector silent rather than making the spawn fail. That is not
        politeness, it is the H13 constraint spelled out -- this runs on EVERY
        backend's construction path including kiro-cli's, and a diagnostic that
        can fail a session is a worse defect than the one it detects.
        """
        try:
            return agent_spec_snapshot(self._agent, work_dir=self._work_dir)
        except Exception:
            logger.debug("unresolved-ref guard: agent spec unreadable", exc_info=True)
            return None

    def _guard_unresolved_mcp_refs(self, wire_servers: Any) -> None:
        """Warn when the spec's ``@server`` refs name nothing in Crew's projection.

        The one place the answer can be known: *wire_servers* is the FINAL array
        -- spec projection plus the broker stubs -- so this is the last point
        before ``session/new`` at which "the spec asked for it" and "Crew's
        projection carries it" can be compared at all.

        Called for every backend, not just the ones with a mirror. The defect it
        detects has landed on three harnesses already
        (``providers/mirrors/README.md``), so a check that only ran on the harness
        someone had already thought about would be the same omission a fourth
        time. A host that mounts the spec's own ``mcpServers`` off the wire
        (``ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE``: kiro-cli loads them via
        ``--agent``) is judged against the spec's definition instead of the array,
        which the detector resolves from the backend id.

        Synchronous, in-memory and non-raising, in that order of importance: the
        composition site is shared with kiro-cli, so this adds no scheduling point
        and no failure mode to that backend's path (harness-parity H13). It also
        changes NOTHING -- not the array, not the session's fate. A ref naming
        nothing is a configuration fact, and the complaint about this defect class
        was that it was invisible, not that it was tolerated.
        """
        spec = self._mcp_ref_spec
        if spec is None:
            # No snapshot: either the spawn path did not warm one (a client driven
            # straight into session/new by a test) or the spec was unreadable.
            # Reading it here would be the disk touch this site must not have.
            return
        # A server this attempt pruned as unstartable IS configured: the report
        # already names it as failed with its reason, and a second row calling it
        # unconfigured would send the user after the projection instead of the
        # command.
        pruned = getattr(self, "_mcp_pruned_names", ())
        if pruned and isinstance(wire_servers, (list, tuple)):
            wire_servers = [*wire_servers, *({"name": name} for name in pruned)]
        try:
            unresolved = warn_unresolved_server_refs(
                spec,
                wire_servers,
                backend=self.backend,
                agent=self._agent,
                gateway_enabled=self._mcp_gateway_overlay is not None,
            )
            if unresolved:
                self._mcp_report.record_unresolved_refs(unresolved)
        except Exception:
            logger.debug("unresolved-ref guard: evaluation failed", exc_info=True)
        try:
            header_refs, omitted = warn_remote_header_secret_refs(
                spec, wire_servers, backend=self.backend, agent=self._agent
            )
            if header_refs:
                self._mcp_report.record_header_secret_refs(header_refs, omitted)
        except Exception:
            logger.debug("remote-header guard: evaluation failed", exc_info=True)

    def _session_mcp_servers(self) -> list[dict[str, Any]]:
        """MCP server array passed to this session's ``session/new`` / ``session/load``.

        Empty for kiro-cli, which receives the same servers through ``--agent``.
        For a harness in ``ACP_BACKENDS_SESSION_MCP_ARRAY`` the array is the ONLY
        channel — the adapter reads no agent spec of CREW'S — so it is built by
        translating this session's agent spec (see
        :mod:`kiro_crew.acp.session_mcp`). "Of Crew's" is the load-bearing half:
        codex-acp does load a config file of its own, and being a member says only
        that this array is the sole channel from HERE to there.

        **In-memory, and deliberately so.** The translation reads disk, but it
        runs once per spawn in :meth:`_resolve_session_mcp_servers` and lands in
        ``_session_mcp_cache``; this accessor only hands the cached list out. That
        is what keeps the shared session-params call site synchronous: awaiting an
        executor hop there would put a new scheduling and failure point on EVERY
        backend's construction path — kiro-cli included, which returns ``[]``
        here and needs no I/O at all — and the kiro path is not allowed to change
        in service of an adapter (harness-parity H13).

        Asks the capability set rather than ``self._is_claude`` on purpose: where a
        harness gets its MCP servers is a property of its transport, not of its
        vendor, so the next such adapter joins the set instead of adding a second
        branch here (harness-parity H6). The set check comes FIRST, so a harness
        outside it returns before the cache is ever consulted and can never be
        made to read a spec.

        A cold cache resolves inline rather than returning nothing: a set member
        whose spawn path does not pre-warm would otherwise come up with zero
        tools, which is the exact defect this module exists to fix. That costs a
        brief blocking read on the loop, which is why the spawn path warms it.

        Per-spawn freshness is preserved: ``_reset_state`` clears the cache, so an
        MCP install or toggle takes effect on the next session with no gateway
        restart.
        """
        if self.backend not in ACP_BACKENDS_SESSION_MCP_ARRAY:
            return []
        if self._session_mcp_cache is None:
            self._session_mcp_cache = self._resolve_session_mcp_servers()
        return list(self._session_mcp_cache)

    def _claude_session_mcp_servers(self) -> list:
        """MCP server array passed to a claude ``session/new`` / ``session/load``.

        Overridable seam, now FILLED for the public build. It used to return ``[]``,
        which is byte-identical for kiro-cli (it gets its servers via ``--agent``)
        but was a REAL GAP for claude: the claude-agent-acp adapter does not read
        ``kirocrew.mcp.json`` on its own, so a claude session had zero MCP tools.
        The harness itself worked — prompts, streaming, permissions — but Crew's own
        tools were absent, with no error anywhere.

        The translation lives in the mirror
        (:mod:`kiro_crew.providers.mirrors.claude_code`), not here: projecting the
        agent spec onto a backend's native shape is one named contract with one
        implementation per backend, rather than a per-harness override each backend
        author rediscovers. See ``docs/request-for-change/rfc-agent-config-mirror.md``.

        The seam is deliberately KEPT rather than replaced by a capability-set call:
        an edition already overrides this method, and swapping the call site for a
        set membership test would silently stop calling that override.

        In-memory only. The spawn path warms ``_session_mcp_cache`` off the loop, so
        this accessor adds no scheduling or failure point to a call site shared with
        kiro-cli (harness-parity H13).
        """
        return self._session_mcp_servers()

    def _opencode_session_mcp_servers(self) -> list:
        """MCP server array passed to an opencode ``session/new`` / ``session/load``.

        The opencode twin of
        :meth:`kiro_crew.acp.harness.codex.CodexHarness.session_mcp_servers`, and it
        must stay non-empty for the same reason: ``opencode acp`` reads no
        ``~/.kiro/agents/<name>.json``, so nothing Crew declares reaches the session
        through any other door. Until this hook existed an opencode session held
        none of Crew's own tools at all -- no ``spawn_run``, no ``cron_add``, no
        ``send_message`` -- while working in every visible respect.

        What it does NOT do is the interesting half. There is no transport filter
        here, unlike codex: an ``http`` element and an ``sse`` element are both
        ACCEPTED by ``opencode acp``, so ``drop_unadvertised_transports`` would only
        remove servers the harness would have mounted. Measured, not assumed --
        ``test/test_opencode_session_mcp.py::test_real_opencode_acp_accepts_the_crew_stdio_element``
        drives a real ``opencode acp`` the way its codex sibling drives codex-acp,
        and pins the ``initialize`` ``mcpCapabilities`` shape so a release that
        starts refusing the stdio element goes red here rather than silently
        emptying every session's tool set.

        The failure mode a bad element causes is the OPPOSITE of codex's, which is
        why the shared translator's skip discipline matters more here, not less: a
        malformed element (no ``command``, or an ``env`` that is not an array) fails
        the WHOLE ``session/new`` with ``-32602`` on this harness, where codex drops
        the element and succeeds. ``acp.session_mcp.acp_server_element`` returns
        ``None`` for an entry with neither ``command`` nor ``url`` and stringifies
        what it cannot type, so one hand-edited spec line costs that server rather
        than the session.

        The translation lives in the mirror
        (:mod:`kiro_crew.providers.mirrors.opencode`), not here, for the same reason
        claude's and codex's do: projecting the agent spec onto a backend's native
        shape is one named contract with one implementation per backend.

        The seam is deliberately KEPT rather than replaced by a capability-set
        call: an edition may override this method, and swapping the call site for a
        set membership test would silently stop calling that override.

        In-memory only. The spawn path warms ``_session_mcp_cache`` off the loop, so
        this accessor adds no scheduling or failure point to a call site shared with
        kiro-cli (harness-parity H13).
        """
        return self._session_mcp_servers()

    def _deepseek_session_mcp_servers(self) -> list:
        """MCP server array passed to a deepseek ``session/new`` / ``session/resume``.

        Only the member mounts: deepseek has no mirror, so its spec is not
        translated and Crew's pooled tools reach it through the shared broker
        append. Empty on every non-member session. In-memory only: the launch
        warms the cache off the loop.
        """
        return self._session_mcp_servers()

    def _pi_session_mcp_servers(self) -> list:
        """MCP server array passed to a pi ``session/new`` / ``session/load``.

        Only the member mounts, as on deepseek: pi has no mirror, so its spec is not
        translated and Crew's pooled tools arrive through the shared broker append.
        pi-acp stores the wire array and never forwards it, so what MOUNTS these
        servers is the handoff file the spawn wrote from the same composition
        (:func:`kiro_crew.acp.harness.pi.pi_mcp_handoff_payload`). The wire copy is
        what the session report reads its roster from. In-memory only.
        """
        return self._session_mcp_servers()

    async def _republish_pi_mcp_handoff(self) -> None:
        """Rewrite the MCP handoff file before a request that starts a pi process.

        pi-acp starts pi on ``session/load`` and on ``session/new``, and a load that
        fails falls through to a new session on the same adapter, so the file has to
        be present for each. The extension removes it after reading where its
        sandbox lets it. Off-loop: a file write.
        """
        from kiro_crew.acp.harness.pi import publish_pi_mcp_handoff

        path, payload = getattr(self, "_pi_mcp_handoff", ("", b""))
        if path and self._pi_gate_nonce:
            await asyncio.to_thread(publish_pi_mcp_handoff, self._pi_gate_nonce, payload)

    async def _discard_pi_mcp_handoff(self) -> None:
        """Remove the MCP handoff file and forget it. Off-loop: an unlink."""
        from kiro_crew.acp.harness.pi import discard_pi_mcp_handoff

        path, _payload = getattr(self, "_pi_mcp_handoff", ("", b""))
        self._pi_mcp_handoff = ("", b"")
        if path:
            await asyncio.to_thread(discard_pi_mcp_handoff, path)

    def _goose_session_mcp_servers(self) -> list:
        """MCP server array passed to a goose ``session/new`` / ``session/load``.

        The goose twin of :meth:`_opencode_session_mcp_servers`, and it must stay
        non-empty for the same reason: ``goose acp`` reads no
        ``~/.kiro/agents/<name>.json``, so nothing Crew declares reaches the session
        through any other door. Without this hook a goose session holds none of Crew's
        own tools at all -- no ``spawn_run``, no ``cron_add``, no ``send_message`` --
        while working in every visible respect. It also holds no pooled broker stubs,
        because :meth:`_pooled_mcp_servers` answers ``[]`` for any backend in
        ``MIRRORS`` and hands that half to the mirror instead.

        No transport filter, for opencode's reason rather than by copying it: this
        harness ACCEPTS the stdio element and mounts it, measured as a round trip
        against 1.50.1 -- the named child is asked ``initialize``,
        ``notifications/initialized``, ``tools/list`` and ``tools/call``, and the
        tool's own result comes back. So a filter would only remove servers the
        harness would have mounted.

        The failure mode a bad element causes is the LOOSEST of the three, and it is
        the one thing about this accessor a reader has to know: an element whose command
        cannot start does not fail ``session/new`` at all. The session is created with
        that element dropped, so a malformed spec line costs Crew's tools rather than the
        session.

        **The drop is unobservable on this harness, and the corpus pin is the accepted
        control.** goose emits no per-server MCP notification of any kind, so
        ``_mcp_timeout_progress`` -- which reads ``mcp_server_initialized`` -- has
        nothing to read here even as the timeout diagnostic it is; and the one
        enumeration ``session/new`` does carry, ``available_commands_update``, is
        byte-identical between a good mount and a dropped one. A session missing every
        Crew tool is therefore indistinguishable on the wire from a healthy one, and
        what stands in for a runtime check is
        ``test/fixtures/acp_frames/goose/mcp-stdio-mount-live.jsonl``: it pins the round
        trip -- ``initialize``, ``notifications/initialized``, ``tools/list`` and
        ``tools/call`` reaching the child, and its result coming back -- so a release
        that stops mounting the element goes red in the corpus rather than silently
        emptying every session's tool set.

        Accepted rather than closed, on two grounds. A dropped mount is a FUNCTIONALITY
        loss and not a gate bypass: permission routing on this harness rides on the
        seeded mode and its read-back, not on MCP, so every tool call a degraded session
        does make still reaches ``HookManager.on_tool_call``. And observing the connect
        is not goose-shaped work -- Crew hosts the broker stubs, so it can watch for the
        child's own ``initialize`` for every member of
        ``ACP_BACKENDS_SESSION_MCP_ARRAY``. That makes it a shared-path feature rather
        than one harness's onboarding cost, and it is named as a follow-up: the broker
        observes an ``initialize`` from the child within N seconds of ``session/new``,
        and otherwise logs a warning and marks the session degraded.

        The translation lives in the mirror
        (:mod:`kiro_crew.providers.mirrors.goose`), not here, for the same reason the
        three siblings' do: projecting the agent spec onto a backend's native shape is
        one named contract with one implementation per backend.

        The seam is deliberately KEPT rather than replaced by a capability-set call: an
        edition may override this method, and swapping the call site for a set
        membership test would silently stop calling that override.

        In-memory only. The spawn path warms ``_session_mcp_cache`` off the loop, so
        this accessor adds no scheduling or failure point to a call site shared with
        kiro-cli (harness-parity H13).

        The count is logged HERE, on the goose arm, because it is the one thing about a
        goose session's tool set that is observable at all: the harness reports nothing
        per server and nothing on a drop, so the number Crew handed over is the only
        greppable fact a degraded session leaves behind until the broker-side mount
        check exists. A count is not a mount -- it says what was offered, not what took.
        """
        if getattr(self, "_side_read_only", False):
            # A Side Chat session mounts no MCP server, as on claude: READ_ONLY
            # refuses every MCP call, and a server named after goose's builtin
            # extension would replace its read tools.
            return []
        servers = self._session_mcp_servers()
        logger.info(
            "goose session MCP: %d server(s) placed on the session array; this harness "
            "reports neither a mount nor a drop per server [session=%s]",
            len(servers),
            self._session_id,
        )
        return servers

    # Prove and hand back Crew's ownership of a claude settings seed on disk: ``kiro_crew.acp.client_seed_ownership``.
    _claude_local_settings_path = _client_seed_ownership._claude_local_settings_path
    _claude_settings_is_still_ours = _client_seed_ownership._claude_settings_is_still_ours
    _settings_path_bytes = staticmethod(_client_seed_ownership._settings_path_bytes)
    _expected_settings_fingerprint = _client_seed_ownership._expected_settings_fingerprint
    _withdraw_shared_reader_lease = _client_seed_ownership._withdraw_shared_reader_lease
    _rename_aside_noreplace = staticmethod(_client_seed_ownership._rename_aside_noreplace)
    _copy_aside_back_without_clobber = staticmethod(
        _client_seed_ownership._copy_aside_back_without_clobber
    )
    _discard_claude_settings_seed = _client_seed_ownership._discard_claude_settings_seed
    _hand_back_unrevoked_claim = _client_seed_ownership._hand_back_unrevoked_claim
    _settle_claude_settings_seed = _client_seed_ownership._settle_claude_settings_seed
    release_settings_seed_claim = _client_seed_ownership.release_settings_seed_claim

    @staticmethod
    def _settings_path_holds(path: Path, expectation: tuple[int, str] | None) -> bool:
        """Whether *path* still holds exactly the ``(size, sha256)`` in *expectation*.

        Split out from :meth:`_claude_settings_is_still_ours` so the teardown
        transaction can be a pure function of arguments captured before it starts.
        It runs in a thread while the client clears its instance flags, so a version
        that re-read ``self`` could decide ownership against state that changed
        underneath it. ``None`` means Crew has no claim to check, which reads as
        "not ours".
        """
        return AcpClient._settings_path_bytes(path, expectation) is not None

    @staticmethod
    def _claim_pathname_if_ours(
        path: Path, expectation: tuple[int, str] | None
    ) -> tuple[Path, tuple[int, int]] | None:
        """Atomically move *path* aside into a fresh name; return it IFF it is Crew's.

        Closes the window between an ownership check and the delete or overwrite that
        acts on it. ``_settings_path_holds`` verifies bytes by PATHNAME, but the delete
        or re-seed then mutates that same pathname a moment later -- and a user who
        atomically replaced the file in between would have their settings deleted or
        clobbered. ``os.replace`` captures whatever is at the pathname in ONE atomic
        step, so verifying the MOVED inode cannot then race: its bytes are fixed. A
        match is Crew's own seed (the caller deletes or replaces the moved file); a
        mismatch is a replacement that raced in, and it is put back untouched.

        The capture destination is a FRESH name created by ``mkstemp`` (``O_EXCL``) in
        the same directory, so it is a pathname this process just created and provably
        did NOT pre-exist. A fixed sibling name (e.g. ``<name>.crew-gc``) could name a
        file the project already owns, and ``os.replace`` onto it would clobber that
        file atomically -- relocating the very data loss this helper prevents one
        pathname over. ``os.replace`` onto our own fresh temp destroys only the empty
        temp we just made.

        ``(aside, identity)`` -- the moved file and ``(st_dev, st_ino)`` captured at
        the move, which every later restore and cleanup pins against. ``None``
        (nothing for the caller to mutate) when the path is gone, cannot be moved, or
        holds a file that is not Crew's. A crash between the move and the caller's
        follow-up leaves at most one such ``.crew-gc`` temp beside the target, which
        the next session's fresh seed ignores.
        """
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent), prefix=path.name + ".", suffix=".crew-gc"
            )
        except OSError:
            return None
        tmp_st = os.fstat(fd)
        os.close(fd)
        aside = Path(tmp_name)
        try:
            os.replace(path, aside)
        except OSError:
            # Nothing to capture (path gone, or it cannot be moved): drop the empty
            # temp so a refusal never litters a stray file beside the target.
            with suppress(OSError):
                pinned_fs.unlink_verified_by_name(
                    path.parent, aside.name, (tmp_st.st_dev, tmp_st.st_ino)
                )
            return None
        try:
            moved = os.lstat(aside)
            moved_ident = (moved.st_dev, moved.st_ino)
        except OSError:
            # The moved entry vanished under us (a racing unlink): nothing left
            # to validate or restore. The empty temp name is gone with it.
            return None
        if AcpClient._settings_path_holds(aside, expectation):
            return aside, moved_ident
        # Not Crew's after all: a replacement raced in, or the user's own file --
        # a different regular file, a symlink the verifying open refuses to
        # follow, a file of any size. Put the moved entry back AS IT IS, without
        # clobbering whatever else may have taken the vacated name in the
        # meantime. No-clobber preserves the NEWER occupant and keeps the moved
        # entry recoverable beside it, where a replace-semantics restore would
        # silently destroy the newer one.
        AcpClient._restore_aside_without_clobber(aside, path, moved_ident)
        return None

    @property
    def _permission_surface_governed(self) -> bool:
        """Whether Crew controls this session's native permission surface.

        The precondition for delivering the ``mcpServers`` array, in either of
        its two shapes: this client authored ``settings.local.json`` itself
        (``_claude_settings_authored``), or the file's CURRENT on-disk bytes
        passed the sibling-seed validation
        (``_permission_surface_share_validated``). The durable reader lease is
        tracked separately in ``_claude_settings_shared`` because it survives a
        failed re-validation to keep the file pinned for a later re-earn, and the
        owner's un-revoked durable claim (``_claude_settings_claim_unrevoked``) is
        deliberately not read here either: it says a hand-back is still owed for
        a file already observed to be someone else's, never that Crew governs it.
        ``getattr`` on both governed flags because tests build clients without
        ``__init__``.

        A third shape governs without the file: the project owns it, and this
        session leaves it out of its setting sources
        (``_claude_local_settings_excluded``, see
        :meth:`_exclude_foreign_local_settings`). Its ``permissions.allow`` then
        never loads, so it cannot pre-approve a tool either.
        """
        return (
            getattr(self, "_claude_settings_authored", False)
            or getattr(self, "_permission_surface_share_validated", False)
            or getattr(self, "_claude_local_settings_excluded", False)
        )

    def _invalidate_session_mcp_projection(self) -> None:
        """Drop the MCP array and the spec snapshot that authorized it."""
        self._session_mcp_cache = None
        self._session_mcp_snapshot = None

    # Author an ``AcpClient`` session's claude ``settings.local.json`` seed: ``kiro_crew.acp.client_claude_seed``.
    _share_settings_seed_if_identical = _client_claude_seed._share_settings_seed_if_identical
    _share_sibling_settings_seed = _client_claude_seed._share_sibling_settings_seed
    _retract_reseed = _client_claude_seed._retract_reseed
    _log_declined_share = _client_claude_seed._log_declined_share
    _render_claude_settings_payload = _client_claude_seed._render_claude_settings_payload
    _write_claude_local_settings = _client_claude_seed._write_claude_local_settings
    _reseed_after_capture = _client_claude_seed._reseed_after_capture
    _seed_session_settings = _client_claude_seed._seed_session_settings

    @staticmethod
    def _restore_aside_without_clobber(aside: Path, path: Path, expect: tuple[int, int]) -> bool:
        """Put a moved-aside entry back under *path* without replacing a new occupant.

        A same-directory no-clobber rename (:meth:`_rename_aside_noreplace`), so a
        symlink, a directory or a file of any size the user placed at the settings
        path is restored exactly as it was captured -- no copy, no size cap, no read
        of its bytes -- and the aside name is consumed by the move. An occupant that
        arrived at the pathname while the entry was out is newer, so the rename
        refuses it and the moved entry stays recoverable beside it as ``.crew-gc``
        litter. Where no no-clobber rename primitive exists the restore falls back
        to the validated byte copy :func:`pinned_fs.put_back_no_clobber`, pinned to
        the moved inode *expect*; that path restores only a regular file.
        """
        try:
            if AcpClient._rename_aside_noreplace(aside, path):
                return True
        except FileExistsError:
            back: str | None = pinned_fs.PUT_BACK_NAME_TAKEN
        except OSError:
            logger.warning("could not restore %s; it is at %s", path, aside.name)
            return False
        else:
            back = AcpClient._copy_aside_back_without_clobber(aside, path, expect)
            if back is None:
                return True
        if back == pinned_fs.PUT_BACK_NAME_TAKEN:
            logger.info(
                "%s was recreated while Crew's seed was moved aside; leaving the new "
                "file in place (the moved seed remains at %s).",
                path,
                aside.name,
            )
            return False
        logger.warning("could not restore %s; it is at %s", path, aside.name)
        return False

    # The claude-agent-acp session contract an ``AcpClient`` keeps: ``kiro_crew.acp.client_claude_session``.
    _exclude_foreign_local_settings = _client_claude_session._exclude_foreign_local_settings
    _claude_adapter_below_setting_sources_floor = (
        _client_claude_session._claude_adapter_below_setting_sources_floor
    )
    _log_claude_adapter_below_floor = _client_claude_session._log_claude_adapter_below_floor
    _prepare_claude_side_read_denies = _client_claude_session._prepare_claude_side_read_denies
    _claude_session_meta = _client_claude_session._claude_session_meta
    _claude_session_excludes_local = _client_claude_session._claude_session_excludes_local
    _verify_claude_project_denies_unchanged = (
        _client_claude_session._verify_claude_project_denies_unchanged
    )
    _pin_claude_starting_mode = _client_claude_session._pin_claude_starting_mode

    def _foreign_settings_owner(self, local_settings: Path) -> str:
        """Who a ``settings.local.json`` Crew does not govern belongs to, as a log phrase.

        The bytes Crew's durable record names are a Crew seed another open
        session holds, not the project's own file: removing or renaming it only
        lasts until that session re-seeds it. Same bounded, no-follow read as
        the share check.
        """
        recorded = seed_provenance.recorded_durable(local_settings)
        if self._settings_path_bytes(local_settings, recorded) is not None:
            return "is another open Crew session's settings seed for this work dir"
        return "belongs to the project"

    @property
    def is_ready(self) -> bool:
        return self._process is not None and self._session_id is not None

    def _is_process_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def work_scratch_dir(self) -> Path | None:
        """The session tree's work directory this process exposes as ``$KIROCREW_SCRATCH``.

        The inherited directory when this client was spawned into an existing
        tree, else its own allocation; ``None`` before spawn or when allocation
        failed. Handed as ``shared_scratch`` to every spawn made on behalf of
        this session (see ``session_allocation._collect_parent_runtime_kwargs``).
        """
        return self._shared_scratch or self._scratch_dir

    def is_process_alive(self) -> bool:
        """True if the underlying process exists and has not exited."""
        return self._is_process_alive()

    @property
    def process_tree_confirmed_dead(self) -> bool:
        """Whether shutdown confirmed the root and every tracked child exited."""
        return self._process_tree_confirmed_dead is True

    @property
    def process_instance(self) -> str:
        """Identity of the CURRENT child process instance (``""`` when none).

        A fresh random id per spawn. This — not the ACP session id — is what a
        resource minted by the child must be compared against later: a resume
        carries the SAME session id onto a NEW process (``ensure_ready``'s
        session/load path re-sets ``_session_id`` from ``resume_sid``), so a
        session-id comparison fails open exactly when the minting process is
        gone. Liveness is a separate question: this names which spawn, while
        :meth:`is_process_alive` answers whether it still runs.
        """
        return self._process_instance if self._process is not None else ""

    @property
    def exit_code(self) -> int | None:
        """Return the process exit code, or None if still running / never started."""
        return self._process.returncode if self._process else None

    def is_responsive(self, stale_threshold: float = 600.0) -> bool:
        """True if process is alive AND has had I/O activity within threshold seconds."""
        if not self._is_process_alive() or getattr(self, "_stdin_stalled", False):
            return False
        return (time.monotonic() - self._last_activity) < stale_threshold

    def touch_activity(self) -> None:
        """Refresh _last_activity without I/O. Used by long-running MCP tools
        (e.g. the `wait` tool) to prevent is_responsive() from flagging a
        deliberately-idle session as stale and triggering SIGTERM."""
        self._last_activity = time.monotonic()

    @property
    def resumed(self) -> bool:
        """True if the last session was restored via session/load."""
        return self._resumed

    def set_resume_session_id(self, sid: str) -> None:
        """Set a kiro-cli session ID to restore via session/load on next ensure_ready()."""
        self._resume_session_id = sid

    def set_fork_request(self, source_sid: str, message_id: str) -> None:
        """Start the next ensure_ready() from a fork of *source_sid* cut at *message_id*.

        Consumed by that handshake whether or not the fork succeeds; when it does
        not, the handshake falls back to session/new.
        """
        self._fork_request = (source_sid, message_id)

    def rekey(
        self,
        session_key: str,
        channel_id: str | None = None,
        crew_agent: str = "",
        watchdog: object | None = None,
    ) -> None:
        """Re-key this client for a different session (used by warm pool).

        ``crew_agent`` and ``watchdog`` exist only for signature parity with
        AcpSessionProvider.rekey (session_allocation.py calls
        provider.client.rekey uniformly): this client's dispatch loop carries no
        per-agent watchdog snapshot, so both are accepted and deliberately not
        stored."""
        self._session_key = session_key
        self._channel_id = channel_id
        self._last_activity = time.monotonic()
        # The prompt stats' context fields describe whatever this runtime did
        # BEFORE the handoff — carry_over() deliberately preserves them across
        # turn boundaries, so without this reset a recycled runtime hands its
        # previous session's context_pct to the new chat and the first
        # check_context_usage() compacts an empty conversation.
        self.last_prompt_stats.reset_context_state()
        # Claim-push: tell gatewayd this runtime PID now belongs to
        # ``session_key`` so every MCP stub connection under it carries the
        # right ``_meta.caller`` immediately — event-driven replacement for
        # the stub-side recaller poll (whose bounded budget stranded pool
        # runtimes claimed late). Fire-and-forget; no-ops without a gateway
        # socket or a live process.
        schedule_claim(
            self._mcp_gateway_socket,
            self._process.pid if self._process else None,
            session_key,
            channel_id,
            self._stub_session_token,
        )
        # The token deliberately SURVIVES a rekey (see __init__: a fresh one would
        # leave the live MCP children carrying a name no claim will ever mention
        # again), so the signed mapping is what has to move to the claiming
        # session. Without this, a child spawned for the previous session resolves
        # its token to that session's key until the first turn republishes — and
        # the claiming session's first tool call is exactly what happens in
        # between. Fire-and-forget; offloads its own file I/O.
        schedule_session_token_publish(self._stub_session_token, session_key)

    @property
    def session_identity_token(self) -> str:
        """This session's per-session identity token, or ``""``.

        The uniform name the shared per-turn publisher reads
        (``messaging.identity._publish_session_token``), for the same reason
        :meth:`reclaim` is uniform: the publisher must stay backend-agnostic, and
        the two ACP providers keep the token in different places — this one on the
        client, the shared-runtime one on the session handle. A provider without
        this attribute is simply not asked.
        """
        return self._stub_session_token

    def reclaim(self) -> None:
        """Re-push this session's claim, naming it by its stub token.

        Idempotent: gatewayd skips a connection whose caller is already this
        session, so the steady-state effect is refreshing the token binding.
        That refresh is the point — the binding lives only in the daemon's
        memory, so a gatewayd respawn under a live session leaves its stubs
        holding a token nothing names, and a token nothing names is refused
        rather than resolved from the process tree. Called at the start of every
        turn by the shared identity publisher, so a restart costs at most the
        turn it happened in.

        No-ops without a token (no stubs injected), without a socket, or without
        a live process — the same preconditions ``schedule_claim`` enforces.
        """
        if not self._stub_session_token:
            return
        schedule_claim(
            self._mcp_gateway_socket,
            self._process.pid if self._process else None,
            self._session_key or "",
            self._channel_id,
            self._stub_session_token,
        )

    # Select, verify and apply an ``AcpClient`` session's model: ``kiro_crew.acp.client_model_selection``.
    set_model = _client_model_selection.set_model
    _capture_available_models = _client_model_selection._capture_available_models
    _persist_advertised_models_if_changed = (
        _client_model_selection._persist_advertised_models_if_changed
    )
    available_models = _client_model_selection.available_models
    _advertised_model_ids = _client_model_selection._advertised_model_ids
    _model_is_unusable = _client_model_selection._model_is_unusable
    refresh_available_models = _client_model_selection.refresh_available_models
    _entitlement_probe_client = _client_model_selection._entitlement_probe_client
    _probe_advertised_models = _client_model_selection._probe_advertised_models
    _entitlement_probe_answer = _client_model_selection._entitlement_probe_answer
    _model_config_candidates = staticmethod(_client_model_selection._model_config_candidates)
    _push_model_config_option = _client_model_selection._push_model_config_option
    _ensure_served_default = _client_model_selection._ensure_served_default
    _note_agent_switched = _client_model_selection._note_agent_switched
    _judge_activated_model = _client_model_selection._judge_activated_model
    _reassert_adapter_resolved_model = _client_model_selection._reassert_adapter_resolved_model
    _apply_startup_model = _client_model_selection._apply_startup_model
    set_config_option = _client_model_selection.set_config_option
    _store_session_config = _client_model_selection._store_session_config
    _handle_config_option_update = _client_model_selection._handle_config_option_update
    _sync_effort_levels = _client_model_selection._sync_effort_levels
    supports_config_option = _client_model_selection.supports_config_option
    get_valid_effort_levels = _client_model_selection.get_valid_effort_levels

    # Answer an ``AcpClient`` session's permission requests and enforce its tool restrictions: ``kiro_crew.acp.client_permissions``.
    _apply_session_permission_routing = _client_permissions._apply_session_permission_routing
    _verify_goose_routing = _client_permissions._verify_goose_routing
    approve_tool = _client_permissions.approve_tool
    _note_pi_gate_denied = _client_permissions._note_pi_gate_denied
    reject_tool = _client_permissions.reject_tool
    _handle_permission = _client_permissions._handle_permission
    _refuse_identity_drift = _client_permissions._refuse_identity_drift
    _foreign_mcp_identity = _client_permissions._foreign_mcp_identity
    _unclassified_tool_call = _client_permissions._unclassified_tool_call
    _tripwire_spec_disabled_tool = _client_permissions._tripwire_spec_disabled_tool
    _note_pi_adapter_version = _client_permissions._note_pi_adapter_version
    _note_goose_version = _client_permissions._note_goose_version
    _tripwire_goose_mode = _client_permissions._tripwire_goose_mode
    _tripwire_pi_gate = _client_permissions._tripwire_pi_gate
    _audit_spec_restriction = _client_permissions._audit_spec_restriction
    _deny_zero_tools = _client_permissions._deny_zero_tools
    _deny_spec_disabled_tool = _client_permissions._deny_spec_disabled_tool
    _reject_unknown_server_request = _client_permissions._reject_unknown_server_request
    _note_pi_gate_asked = _client_permissions._note_pi_gate_asked
    _build_permission_event = _client_permissions._build_permission_event

    @property
    def acp_config_options(self) -> list[dict]:
        """Config options reported by ACP (effort, model, mode selectors)."""
        return self._acp_config_options

    def _next_req_id(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid

    # Prepare an ``AcpClient`` spawn before the process exists: ``kiro_crew.acp.client_spawn_env``.
    _prepare_spawn_workspace = _client_spawn_env._prepare_spawn_workspace
    _agent_env_for_spawn = _client_spawn_env._agent_env_for_spawn
    _apply_session_identity_env = _client_spawn_env._apply_session_identity_env
    _resolve_spawn_agent_argv = _client_spawn_env._resolve_spawn_agent_argv

    # ── Process Management ──

    def _discard_sandbox_cleanup(self) -> None:
        """Unlink and forget the sandbox temp file allocated by ``wrap_argv``.

        wrap_argv writes a launcher/profile file that the spawned child
        consumes at exec. Once no child will exec it — the spawn failed, was
        cancelled, or the process is being reset — it must be removed here, or
        each attempt leaks one file into the temp dir for the gateway's
        lifetime (nothing else references the path after ``_spawn`` reassigns
        ``self._sandbox_cleanup``).
        """
        if self._sandbox_cleanup:
            try:
                os.remove(self._sandbox_cleanup)
            except OSError:
                pass
            self._sandbox_cleanup = None

    async def _discard_bound_workspace(self) -> None:
        """Close the parent copy of a macOS workspace identity off-loop."""
        descriptor = getattr(self, "_bound_workspace_fd", None)
        self._bound_workspace_fd = None
        work_dir = getattr(self, "_work_dir", None)
        if work_dir is not None:
            self._spawn_work_dir = str(work_dir)
        if descriptor is not None:
            await release_bound_agent_workspace(descriptor)

    async def _session_work_dir(self) -> str:
        """Return the ACP cwd backed by the process's bound directory identity.

        Unbound -- every platform but macOS, where nothing binds -- this is the
        spawn's own pathname and there is nothing to re-check.

        Bound, the descriptor is re-verified and the peer receives the DESCRIPTOR's
        own name. The rule itself is ``sandbox.resolve_bound_session_workspace``,
        shared with ``AcpRuntime._session_work_dir`` so the two halves of this
        boundary cannot drift; only the error type is this front end's. Fails the
        session rather than falling back to the bind-time spelling; see the runtime's
        method for the residual limit a string cannot close.
        """
        if self._bound_workspace_fd is None:
            return self._spawn_work_dir
        try:
            return await resolve_bound_session_workspace(
                self._bound_workspace_fd, self._spawn_work_dir
            )
        except BoundWorkspaceMismatch as exc:
            raise AcpError(
                "A delegated macOS Kiro process is bound to one exact workspace; "
                "respawn it bound to the requested workspace"
            ) from exc
        except OSError as exc:
            raise AcpError("Cannot verify the macOS session workspace binding") from exc

    async def _cleanup_failed_live_spawn(self) -> None:
        """Kill a failed live child and always release its workspace binding."""
        try:
            await self._kill_process(force=True)
        finally:
            await self._discard_bound_workspace()
            # A failed startup that already wrote the seed owns one too, and the
            # session it belonged to is over -- so it is discarded on exactly the
            # terms a graceful shutdown uses. In the `finally` for the same reason
            # the caller puts `_reset_state` in one: this is the last chance.
            await self._discard_claude_settings_seed()

    async def _to_thread_guarding_sandbox(
        self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any
    ) -> _T:
        """``asyncio.to_thread`` that discards the sandbox file on failure.

        After ``wrap_argv`` has allocated the sandbox temp file, every
        suspension point before the exec is a leak window: a cancellation
        (turn cancel, session close, shutdown) unwinds ``_spawn`` without
        reaching ``_reset_state``, orphaning the file. Route any offload in
        that window through here so the file is removed before re-raising.
        """
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except BaseException:
            self._discard_sandbox_cleanup()
            raise

    async def _prepare_session_mcp(self) -> None:
        """Warm this session's ``mcpServers`` array off the loop, for its host's launch.

        A host whose array is the session's tool surface asks for this at its own
        point in the launch (:mod:`kiro_crew.acp.harness`). Correctness does not depend
        on it -- :meth:`_session_mcp_servers` resolves a cold cache itself -- the warm
        is what keeps the disk read off the loop.
        """
        self._session_mcp_cache = await asyncio.to_thread(self._resolve_session_mcp_servers)

    async def _spawn(self) -> None:
        """Start the ACP backend subprocess with stdio pipes, suspended, then track it.

        A host this client starts one process per session for -- claude-agent-acp,
        OpenCode, goose, pi, the DeepSeek Harness -- resolves its plan in its own
        adapter (:func:`kiro_crew.acp.harness.process_adapter_for`); kiro-cli keeps its
        own arm below. Either plan goes through the launch tail both drivers share
        (:func:`kiro_crew.acp.launch.launch`). What happens once the process exists --
        the resume, its identity, its pids, its stderr -- stays here.
        """
        # Off-loop: mkdir is a blocking syscall and the parent dirs may live on
        # slow storage; the loop must never wait on the kernel here. The
        # unresolved-ref snapshot rides IN this hop rather than in one of its own:
        # the detector runs for every harness (the defect it catches has shipped on
        # three), and a second await would be a new suspension point on kiro-cli's
        # construction path in service of a diagnostic -- so the count of awaits
        # here is deliberately unchanged (harness-parity H13).
        await asyncio.to_thread(self._prepare_spawn_workspace)
        # A fresh spawn reads ``agent.env`` afresh, once (_agent_env_for_spawn).
        self._agent_env_snapshot = None

        # Kiro's internal macOS sandbox replaces (rather than nests inside)
        # Kiro Crew's Seatbelt profile. Refuse a delegated agent workspace that
        # can reach the protected named decoder snapshots; otherwise a same-UID
        # agent could replace verified bytes before the decoder spawn opens them.
        if self.backend in ACP_BACKENDS_INTERNAL_SANDBOX:
            await asyncio.to_thread(assert_voice_runtime_outside_agent_workspace, self._work_dir)

        adapter = process_adapter_for(self.backend)
        if adapter is not None:
            # The host's own launch knowledge -- its binary, argv, labels, credential
            # mask and gate -- in its own file. The context names this session, which
            # the host may need to prepare before it starts (its MCP array, a seed)
            # and which keeps the facts its turn-time code reads back. The
            # environment and home fields are the shared context's; no per-process
            # host reads them.
            plan = await adapter.resolve_spawn(
                SpawnContext(
                    agent=self._agent,
                    work_dir=self._work_dir,
                    model=self._model,
                    environ=dict(os.environ),
                    home=Path(os.path.expanduser("~")),
                    sandbox_mode=self._sandbox_mode,
                    session=self,
                )
            )
        else:
            # Pin ONE reading of the environment for both the search and the
            # message that reports it. The previous code resolved against the live
            # ``os.environ`` and then, on failure, recomputed the directory set
            # from a FRESH read -- so a PATH change landing in that window (a
            # concurrent installer, a self-update, anything editing the gateway's
            # environment) would produce a "not found" message naming directories
            # that were never searched, while omitting ones that were. Caching the
            # search path WITH the resolution result is what avoids that, and is
            # the same guarantee the Claude adapter carries.
            spawn_environ = dict(os.environ)
            spawn_home = Path.home()
            try:
                kiro_bin = await _resolve_kiro_bin_for_spawn(environ=spawn_environ, home=spawn_home)
            except _KiroExecutableTrustError as exc:
                raise AcpError(str(exc)) from exc
            if not kiro_bin:
                # Pure function of the arguments, so this reproduces exactly the
                # set the resolution above walked. Still off-loop: it expands the
                # inherited PATH.
                raise AcpError(
                    await asyncio.to_thread(
                        kiro_cli_not_found_message,
                        environ=spawn_environ,
                        home=spawn_home,
                    )
                )
            # Self-heal (B): ensure the managed default agent file exists before
            # this --agent spawn, so kiro-cli registers the mode and step 4's
            # set_mode succeeds instead of faulting "Mode not found". Best-effort,
            # off the loop; non-managed agents fall through to the step-4 guard.
            try:
                await asyncio.to_thread(ensure_agent_materialized, self._agent)
            except Exception:
                logger.warning("pre-spawn agent materialization failed", exc_info=True)
            # ALSO not best-effort, and for the same reason as the fork gate below: a
            # DERIVED spec (kirocrew-worker, kirocrew-heartbeat) that predates the default
            # agent's still mounts and auto-approves a server the default does not have, so
            # proceeding would run ungoverned grants. Repairs first and refuses only
            # when it cannot -- a refused dispatch is recoverable and reportable,
            # which a worker running on a revoked server is not.
            try:
                derived_snapshot = await asyncio.to_thread(
                    require_fresh_derived_spec, self._agent, self._work_dir
                )
            except DerivedSpecStale as exc:
                raise AcpError(str(exc)) from exc
            # Captured for the post-load half of the bracket, which is NOT here: this
            # method creates the child and returns after bookkeeping, so at this point
            # the subprocess has not provably read its spec and closing the bracket
            # would prove nothing. ``_initialize_session`` closes it against this
            # object, at the ``initialize`` response -- the earliest signal that the
            # read happened. Same pairing as the AcpRuntime path.
            self._derived_spec_snapshot = derived_snapshot
            # NOT best-effort: a fork-backed agent may not spawn until fork
            # governance is re-projected, and a failed or timed-out refresh
            # ABORTS the spawn — its on-disk allowedTools/autoApprove bypass
            # the PreToolUse gate, so proceeding would run ungoverned grants.
            # The work dir is the cwd kiro-cli resolves --agent against first,
            # so the gate also refuses a fork shadowed by a project-local spec.
            try:
                await asyncio.to_thread(require_fork_governance, self._agent, self._work_dir)
            except ForkGovernanceUnresolved as exc:
                raise AcpError(str(exc)) from exc
            # The same cwd-first resolution makes a checkout that declares a MANAGED
            # agent name the spec that runs, whatever Kiro Crew installed under it.
            shadowed = await asyncio.to_thread(
                managed_shadow_mod.managed_agent_shadow_refusal, self._agent, self._work_dir
            )
            if shadowed:
                raise AcpError(shadowed)
            # The agents-tree seal is a launcher rule, and a spawn delegated to
            # kiro-cli's internal sandbox never sees the launcher — so on those
            # paths a workspace overlapping the agents directory is the one way
            # left to rewrite a spec. Refused before the spawn; off-loop because
            # the delegation predicate reads the kiro settings file.
            overlap = await asyncio.to_thread(
                delegated_workspace_exposes_sealed_target, self._work_dir
            )
            if overlap:
                raise AcpError(overlap)
            from kiro_crew.acp.skill_projection import prepare_native_skill_projection

            # One process, one session: the identity rides the process environment
            # (``_apply_session_identity_env``) and kiro-cli mounts ``kirocrew-core``
            # natively from the view, restrictions included, so no per-session
            # element replaces the declaration here -- the shared runtime's
            # element-withholding question does not arise, and a ``disabledTools``
            # naming other tools must not refuse this agent's view.
            self._native_skill_projection = await asyncio.to_thread(
                prepare_native_skill_projection, self._work_dir, per_session_element=False
            )
            if self._native_skill_projection is not None:
                # The agent this process is launched as: its own FIRST ``set_mode``
                # activation is tolerated even with no prepared view (see
                # NativeSkillProjection.request), which consumes the exemption --
                # so a later switch, including back to this same agent once its
                # view has vanished, takes the strict resolver and fails closed.
                self._native_skill_projection.spawn_agent_name = self._agent
                # Advertised independently of the request() exemption above: the
                # start must find the launch agent's mode in availableModes even
                # with no prepared view (see NativeSkillProjection.frame).
                self._native_skill_projection.advertised_launch_name = self._agent
            argv = [
                kiro_bin,
                KIRO_CLI_SUBCMD,
                "--agent",
                (
                    self._resolve_spawn_agent_argv()
                    if self._native_skill_projection is not None
                    else self._agent
                ),
            ]
            spawn_label = f"{KIRO_CLI_BIN} {KIRO_CLI_SUBCMD}"
            stderr_label = KIRO_CLI_BIN
            plan = SpawnPlan(argv=argv, spawn_label=spawn_label, stderr_label=stderr_label)

        async def _env_before_scrub(
            env: dict[str, str], spawned_binary: str | None
        ) -> dict[str, str]:
            # The host's own variables first, then this session's identity, so a
            # host value can never stand in for the identity the session carries.
            if adapter is not None:
                await adapter.prepare_spawn_env(env, to_thread=self._to_thread_guarding_sandbox)
            self._apply_session_identity_env(env)
            if self._channel_id:
                env["KIROCREW_CHANNEL_ID"] = self._channel_id
            else:
                env.pop("KIROCREW_CHANNEL_ID", None)
            # Resolve SSH_AUTH_SOCK dynamically — the gateway's env may be stale
            # after an ssh-agent restart — and KRB5CCNAME to a FILE: ccache (the
            # kernel keyring, the default on some Linux distros, is invisible to
            # this child, so Kerberos-gated MCP servers fail without it). Covers
            # the session agent and all ACP-provider subagents, which spawn through
            # this same path. The same hop settles the CLI's own KIRO_API_KEY:
            # re-injected from .env for the kiro-cli backend (post-scrub Docker),
            # actively stripped for a foreign backend, which must never receive it
            # (see config.loader.inject/strip_kiro_cli_api_key). All of this
            # glob/stat/reads under /tmp and the data home, so it runs off-loop in
            # ONE thread hop. Guarded: the sandbox temp file is live, so a
            # cancellation here must not orphan it. The launch tail scrubs after
            # it, and KIRO_API_KEY stays available only to the positively
            # identified Kiro backend.
            return await self._to_thread_guarding_sandbox(
                functools.partial(_resolve_spawn_env, kiro_api_key=self._is_kiro), env
            )

        def _env_after_scrub(env: dict[str, str]) -> None:
            # The auxiliary kiro-cli children never reach a harness's apply_spawn_env,
            # so they are named here.
            if self._is_kiro:
                from kiro_crew.acp.harness._common import apply_client_application_env

                apply_client_application_env(env)

        launched = await launch(
            self,
            LaunchRequest(
                argv=plan.argv,
                backend=self.backend,
                sandbox_mode=self._sandbox_mode,
                extra_env=self._extra_env,
                scratch_label=self._session_key or "session",
                env_before_scrub=_env_before_scrub,
                env_after_scrub=_env_after_scrub,
                agent_env=self._agent_env_snapshot,
                extra_hidden_dirs=plan.extra_hidden_dirs,
                extra_expose_files=plan.extra_expose_files,
                internal_sandbox=self.backend in ACP_BACKENDS_INTERNAL_SANDBOX,
                pod_home_remap=self.backend in ACP_BACKENDS_POD_HOME_REMAP,
            ),
            _launch_tools(),
        )
        self._process = launched.process
        self._pid = self._process.pid
        self._kill_sent = None
        self._process_tree_confirmed_dead = False
        # Minted with the process it names, random rather than pid-derived: a
        # pid can be reused by the OS, and the start-time disambiguator is not
        # readable on every platform, so equality on a fresh random id is the
        # comparison that cannot false-match across spawns.
        self._process_instance = uuid.uuid4().hex[:16]
        _spawn_label = plan.spawn_label
        # Everything from here to the end of _spawn runs with a LIVE subprocess
        # that nothing has recorded yet, so every step must be guarded. Without
        # this, any exception in the window — finish_suspended_spawn, the
        # start-time read, the two PID-file appends, the descendant scan — unwinds
        # out of _spawn leaving that process running and absent from both PID
        # files. It is then unreachable by every agent-runtime reaper (they all
        # read those files, and the /proc orphan scan declines managed agent
        # runtimes on purpose), so it leaks until the host reboots.
        #
        # ensure_ready()'s retry loop cannot substitute for this: it only catches
        # AcpTimeoutError / AcpError, and nothing raised in this window is either
        # of those — an OSError from the executor or a wedged file lock sails
        # straight past it and its `finally` records metrics only.
        #
        # BaseException so a CancelledError mid-window cleans up too. Mirrors the
        # twin guard in acp/runtime.py around reader startup + handshake.
        try:
            # Windows resource ceiling, applied while the child is still SUSPENDED,
            # then resumed. No-op on POSIX (CREATE_SUSPENDED is 0 there). OFFLOADED
            # because the Windows path reads the config file and walks the process
            # and thread tables (see the note on finish_suspended_spawn); the child
            # is frozen until it returns, so this is the one await the spawn cannot
            # skip.
            await platform_compat.finish_windows_cleanup_owned_spawn(
                lambda: asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(),
                    functools.partial(
                        finish_suspended_spawn, self._process, self._pid, label=_spawn_label
                    ),
                )
            )
            # Identity for the recycle guards that later decide whether this pid
            # may be signalled. ``get_process_start_id`` is in-process on every
            # platform (its own docstring pins that), so unlike the ``ps``-forking
            # reader it replaces there is nothing here to offload.
            self._start_time = platform_compat.get_process_start_id(self._pid)
            if self._scratch_dir is not None:
                # Liveness anchor for the scratch sweeps -- see acp/runtime.py's
                # twin block. Off-loop, and fail-open on a write that could not
                # happen: an unowned dir is covered by the grace-window rule.
                owner_outcome = await asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(),
                    functools.partial(agent_scratch.record_owner, self._scratch_dir, self._pid),
                )
                if owner_outcome == "refused":
                    # A LINK where this child's own marker belongs: the child
                    # owns that directory and has pointed it somewhere else, so
                    # it is steering an UNSANDBOXED gateway write at a path of
                    # its choosing. Raising hands it to the guard below, which
                    # reaps the process -- the spawn must not carry on as if the
                    # owner had been recorded.
                    raise agent_scratch.ScratchBoundaryError(
                        "the spawned agent replaced its scratch owner marker with a link"
                    )
                if owner_outcome == "stale":
                    # Not an attack: the update failed and the marker it left
                    # could not be cleared, so this dir still names the GATEWAY.
                    # That pid dies with the gateway while this child lives on,
                    # which is the state a later sweep reads as a dead owner
                    # before deleting a live agent's temp dir. Reaping now is
                    # recoverable; that deletion is not.
                    raise agent_scratch.ScratchBoundaryError(
                        "the scratch owner marker still names the gateway after a failed update"
                    )
            if self._shared_scratch is not None:
                # Twin of acp/runtime.py: join the tree's owner marker beside
                # the parent, so the sweep keeps the tree while either lives.
                adopt_outcome = await asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(),
                    functools.partial(agent_scratch.adopt_owner, self._shared_scratch, self._pid),
                )
                if adopt_outcome in ("refused", "stale"):
                    raise agent_scratch.SharedScratchJoinError(
                        "the inherited scratch owner marker could not be joined"
                        if adopt_outcome == "stale"
                        else "the inherited scratch owner marker was replaced with a link"
                    )
                if adopt_outcome != "recorded":
                    logger.warning(
                        "agent-scratch: could not join the owner marker of %r (%s); the tree's "
                        "work directory is left unowned and will not be swept",
                        self._shared_scratch.name,
                        adopt_outcome,
                    )
            logger.info("Spawned %s (PID %d)", _spawn_label, self._pid)
            # Track root PID and do an early descendant scan.  kiro-cli forks
            # child processes quickly after launch.  Recording them here means
            # _kill_process() can clean up even if _initialize_session() fails.
            from kiro_crew.session import (  # circular: session -> config.loader -> providers.acp -> acp.client
                _track_child_pids,
                _track_pid,
                _track_session_pid,
            )
            from kiro_crew.session_pid import _pid_start_token

            # Read BEFORE the appends: the identity of the process that holds the
            # number NOW, kept for the identity-bound retirement in _reset_state
            # and handed to the tracker so it records this same token.
            self._spawn_start_token = _pid_start_token(self._pid)

            # The PID-file trackers each take an exclusive file lock and do a
            # read-modify-append under it — blocking syscalls that must not run
            # on the event loop: ensure_ready() awaits _spawn() from the loop on
            # every cold start, so a contended or wedged lock holder here would
            # stall every task including the liveness heartbeat. Ride the same
            # executor as the descendant scans below.
            _loop = asyncio.get_running_loop()
            await _loop.run_in_executor(subprocess_executor(), _track_pid, self._pid)
            # Separate file for startup cleanup.
            await _loop.run_in_executor(
                subprocess_executor(), _track_session_pid, self._pid, self._spawn_start_token
            )
            await asyncio.sleep(0.3)
            early_descendants = await _loop.run_in_executor(
                subprocess_executor(), runtime_process_tree._get_child_pids, self._pid
            )
            if early_descendants:
                self._child_pids = await _loop.run_in_executor(
                    subprocess_executor(),
                    runtime_process_tree._capture_child_records,
                    early_descendants,
                )
                await _loop.run_in_executor(
                    subprocess_executor(), _track_child_pids, self._child_pids, self._pid or 0
                )
                logger.info(
                    "Early tracking %d descendants of PID %d", len(self._child_pids), self._pid
                )

            if self._process.stderr:
                self._stderr_task = asyncio.ensure_future(
                    self._drain_stderr(self._process.stderr, label=plan.stderr_label)
                )
        except BaseException:
            logger.error(
                "Spawn of %s (PID %s) failed after the process was live; killing it so it "
                "cannot leak untracked",
                _spawn_label,
                self._pid,
                exc_info=True,
            )
            try:
                await self._cleanup_failed_live_spawn()
            except Exception:
                logger.warning(
                    "Cleanup kill after a failed spawn did not complete for PID %s",
                    self._pid,
                    exc_info=True,
                )
            raise

    async def _drain_stderr(
        self, stderr: asyncio.StreamReader, *, label: str = KIRO_CLI_BIN
    ) -> None:
        # Count of suppressed high-frequency marker lines (see
        # _SUPPRESSED_STDERR_MARKERS) and the monotonic timestamp of the last
        # throttled summary, so a thinking burst is observable in the log
        # without re-introducing the per-delta flood it replaced.
        suppressed = 0
        last_summary = time.monotonic()
        while True:
            line = await stderr.readline()
            if not line:
                break
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            # Liveness must advance for EVERY line, including suppressed ones:
            # the adapter is provably alive while emitting them, and the idle
            # watchdog (is_responsive) must not kill an actively-thinking turn.
            # One monotonic read, reused for the throttle check below.
            now = time.monotonic()
            self._last_activity = now
            if any(marker in text for marker in _SUPPRESSED_STDERR_MARKERS):
                # Drop the line: no per-occurrence WARNING, and crucially do not
                # append to the bounded _stderr_lines ring buffer — otherwise a
                # thinking burst evicts the last real errors from diagnostics.
                suppressed += 1
                if now - last_summary >= _SUPPRESSED_STDERR_SUMMARY_INTERVAL_SECS:
                    logger.debug("suppressed %d adapter stderr marker line(s)", suppressed)
                    suppressed = 0
                    last_summary = now
                continue
            if self.memory_mode == "persistent":
                self._stderr_lines.append(text)
                redacted, _ = redact_exfiltration_urls(text)
                redacted, _ = redact_credentials(redacted)
                logger.warning("%s stderr: %s", label, redacted)
        if suppressed:
            # Flush the residual count once the stream closes so the final burst
            # is still accounted for.
            logger.debug("suppressed %d adapter stderr marker line(s)", suppressed)

    async def _snapshot_process_tree(self) -> None:
        """Discover and track the full process tree after MCP servers are loaded.

        Merges with any early snapshot taken in _spawn().  MCP servers
        (the internal MCP server, node) may not exist until after _initialize_session().
        """
        _loop = asyncio.get_running_loop()
        descendants = await _loop.run_in_executor(
            subprocess_executor(), runtime_process_tree._get_child_pids, self._pid
        )
        if not descendants:
            # Retry once — children may not have forked yet
            await asyncio.sleep(0.5)
            descendants = await _loop.run_in_executor(
                subprocess_executor(), runtime_process_tree._get_child_pids, self._pid
            )

        new_pids = [p for p in descendants if p not in self._child_pids]
        if new_pids:
            self._child_pids.update(
                await _loop.run_in_executor(
                    subprocess_executor(), runtime_process_tree._capture_child_records, new_pids
                )
            )

        if self._child_pids:
            from kiro_crew.session import _track_child_pids

            # Exclusive file lock + read-modify-append + per-child start-id
            # reads -- blocking syscalls that must not run on the event loop.
            # Ride the same executor as the twin call site in _spawn().
            await _loop.run_in_executor(
                subprocess_executor(),
                functools.partial(_track_child_pids, self._child_pids, parent_pid=self._pid or 0),
            )
            logger.info("Tracked %d descendant PIDs for PID %d", len(self._child_pids), self._pid)

    async def refresh_process_tree(self) -> None:
        """Record the descendants the live leader has started since the last snapshot.

        The startup snapshot runs once, after session initialise; a turn that
        runs a command which forks its own children adds processes it never
        saw. A teardown takes its kill handle from ``_child_pids``, and a
        leaderless group may only be signalled while a RECORDED member vouches
        for it (``process_identity._group_member_vouches``), so a group only
        late children keep alive was reported and never reaped. Teardown calls
        this just before it takes the handle.

        Walks only a leader that is ours and alive: the asyncio process has no
        return code yet AND the pid still reads back the recorded start id, both
        before the walk and after it. A pid recycled during the walk would name
        another tree, so the new records are dropped then. A dead leader gets no
        walk: its children are reparented and out of its reach. Each new child
        is kept only when a second walk still finds it under the leader and its
        start id still reads back as the one captured, and it is persisted with
        that captured id (``_replace_child_pids``), never re-read at write time.
        Never removes a record. A recorded pid whose start id fails to read
        back is captured again (the leader's tree reused the number), and the
        in-memory record is updated only if the client still holds the same
        process after the write.

        The walk is :func:`runtime_process_tree.guarded_tree_pass` in add-only
        mode, the same pass ``AcpRuntime._snapshot_descendants`` runs in replace
        mode. It stays add-only: the teardown checks each recorded child's
        identity before it signals one (``_kill_escaped_children``), so a pid
        that left is skipped there rather than dropped here.
        """
        # The client's own process object, not a shared runtime: one client
        # owns exactly one process, and that object is what is compared after
        # the walk to tell a respawn apart.
        process = self._process
        start = self._start_time
        if process is None or process.returncode is not None or start is None:
            return
        pid = process.pid
        if not pid:
            return

        def _leader_holds() -> bool:
            return self._process is process and platform_compat.get_process_start_id(pid) == start

        result = await runtime_process_tree.guarded_tree_pass(
            pid,
            self._child_pids,
            probes=runtime_process_tree.TreeProbes(
                walk=runtime_process_tree._get_child_pids,
                capture=runtime_process_tree._capture_child_records,
                start_id=platform_compat.get_process_start_id,
                write=_replace_child_pids,
            ),
            executor=subprocess_executor(),
            root_holds=_leader_holds,
            replace=False,
            # The write awaited: a reset or respawn may have replaced the process
            # meanwhile, and its record must not gain the previous one's children.
            still_owner=lambda: self._process is process,
        )
        if result.outcome == "write_failed":
            logger.warning(
                "Process tree refresh for PID %d: tracking write failed; %d child(ren) left "
                "unrecorded",
                pid,
                len(result.confirmed),
            )
            return
        if result.outcome != "written":
            return
        self._child_pids.update(result.confirmed)
        logger.info(
            "Refreshed process tree for PID %d: %d new descendant(s)", pid, len(result.confirmed)
        )

    async def _kill_process(self, *, force: bool = False) -> None:
        """Kill the subprocess and wait for it to exit.

        Uses process groups (killpg) for clean tree kill, then sweeps
        child PIDs that escaped to a different PGID.

        Args:
            force: If True, kill immediately (used during shutdown).
        """
        if self._process and platform_compat.IS_WINDOWS:
            self._windows_tree_cleanup_failed = True
            await platform_compat.terminate_windows_asyncio_tree(self._process)
            self._windows_tree_cleanup_failed = False
            return
        if not self._process or self._process.returncode is not None:
            return
        pid = self._pid
        if pid is None:  # narrow for mypy — set at _spawn time under the process guard
            return
        # Close pipes first to unblock any pending reads/writes
        for pipe in (self._process.stdin, self._process.stdout, self._process.stderr):
            if pipe:
                try:
                    pipe.close()  # type: ignore[union-attr]
                except Exception:
                    pass

        # Snapshot child PIDs before killing — children in different
        # process groups survive killpg (kiro-cli-chat acp leak).
        # Merge stored snapshot (from init, catches reparented-to-init PIDs)
        # with fresh scan (catches children spawned after init).
        _loop = asyncio.get_running_loop()
        fresh = await _loop.run_in_executor(
            subprocess_executor(), runtime_process_tree._get_child_pids, pid
        )
        stored = self._child_pids
        # Build merged dict: pid → (start_time, basename) (stored has both, fresh needs capture)
        merged: dict[int, ChildRecord] = dict(stored)
        new_pids = [p for p in fresh if p not in merged]
        if new_pids:
            # capture (start_time, basename) off-loop — on macOS these spawn `ps`
            merged.update(
                await _loop.run_in_executor(
                    subprocess_executor(), runtime_process_tree._capture_child_records, new_pids
                )
            )

        if not force:
            try:
                # POSIX: killpg(getpgid) tears down the whole group (setsid at
                # spawn). Windows: taskkill /T /F walks the child tree instead
                # (no process groups) — platform_compat dispatches both. Async
                # variant offloads the Windows taskkill spawn to
                # subprocess_executor so the event loop keeps ticking while
                # taskkill.exe runs.
                self._kill_sent = (int(platform_compat.SIGTERM), "stop requested")
                logger.info("Sending SIGTERM to ACP PID %s (reason: stop requested)", pid)
                await platform_compat.kill_process_tree_async(pid, platform_compat.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
                # _kill_escaped_children -> _is_our_child -> _get_start_time/
                # _read_basename spawn `ps` on macOS; run it off the loop.
                await _loop.run_in_executor(
                    subprocess_executor(), runtime_process_tree._kill_escaped_children, merged
                )
                return
            except asyncio.TimeoutError:
                pass
        # Force kill (async variant offloads Windows taskkill).
        reason = "forced stop" if force else "SIGTERM timed out"
        self._kill_sent = (int(platform_compat.SIGKILL), reason)
        logger.info("Sending SIGKILL to ACP PID %s (reason: %s)", pid, reason)
        try:
            await platform_compat.kill_process_tree_async(pid, platform_compat.SIGKILL)
        except (ProcessLookupError, OSError):
            try:
                self._process.kill()
            except (ProcessLookupError, OSError):
                pass
        await _loop.run_in_executor(
            subprocess_executor(), runtime_process_tree._kill_escaped_children, merged
        )
        try:
            await asyncio.wait_for(self._process.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            logger.warning("PID %s did not exit after force kill", pid)

    def _retire_liveness_state(self) -> None:
        """Release the tracked consult and swap in a fresh, configured oracle.

        Both boundaries that drop a movement baseline — turn start in
        ``_prompt_loop`` under ``_turn_lock``, and process reset — retire rather
        than ``reset()``,
        and both must retire the future TOGETHER with the oracle. Their lifetimes
        cannot diverge: if only the oracle were replaced, a walk wedged during the
        previous turn would answer every later poll with "prior consult still in
        flight", so the new turn never samples its own process and the 90s cutoff
        completes it early — the truncation this gate exists to prevent.

        Releasing the future costs at most one abandoned worker per boundary
        instead of per silent read, which is the bound this gate is actually for.
        A walk submitted through ``_consult_liveness_model_wait`` already carries a
        retrieval callback from submission time, so its eventual exception is
        consumed even though nobody awaits it any more; the consume/attach here
        additionally covers a future that did not come from that path.

        ``fresh()`` carries the configuration over: a default-constructed
        replacement would silently repoint a caller-supplied /proc root, clock or
        sampling interval. ``getattr`` because the low-level PID lifecycle tests
        build clients with ``__new__``.
        """
        prior_consult = getattr(self, "_consult_future", None)
        self._consult_future = None
        if prior_consult is not None:
            if prior_consult.done():
                _consume_future_exception(prior_consult)
            else:
                prior_consult.add_done_callback(_consume_future_exception)
        retiring = getattr(self, "_liveness_oracle", None)
        self._liveness_oracle = retiring.fresh() if retiring is not None else LivenessOracle()

    def _settle_claude_settings_seed_locked(
        self,
        path: Path,
        owner: str,
        payload: str | None,
        expectation: tuple[int, str] | None,
    ) -> None:
        try:
            if seed_provenance.has_sharers(path):
                # A live SHARER session in this process delivered its MCP array
                # against exactly the bytes at this path, and it can neither see
                # nor stop whatever would occupy the pathname next. Unlinking here
                # would free the name for a different permission file -- another
                # session's mode, up to bypassPermissions -- under tools already
                # delivered. So the file and its durable record both stay: that is
                # precisely the recorded-orphan shape a kill -9 leaves, which the
                # next session adopts and repairs once the sharers are gone (claim
                # refuses adoption while any remain). Only this owner's live slot
                # is handed back.
                logger.info(
                    "%s is still shared by a live sibling session; leaving the seed in "
                    "place for it rather than deleting a permission file its tools were "
                    "delivered against. The next session adopts and cleans it up once "
                    "the sharers are gone.",
                    path,
                )
                if not seed_provenance.release(path, owner):
                    logger.warning(
                        "could not durably release Crew's claim on %s; retaining the claim "
                        "with the seed until a later retry or process exit",
                        path,
                    )
                return
            claimed = self._claim_pathname_if_ours(path, expectation)
            if claimed is None:
                logger.info(
                    "%s no longer holds the bytes Crew wrote; leaving the replacement in "
                    "place instead of deleting a file Crew does not own.",
                    path,
                )
                # Nothing at the pathname is Crew's to move, revoke or delete, but
                # the durable owner holder ``record`` published still carries this
                # live process's identity. Handed back here, as the sibling arms do:
                # left standing, no prune reclaims it while this process lives, and
                # every later session on this work_dir reads the pathname as held
                # by a live sibling. ``release`` drops only this owner's holder --
                # never the record, the file, or a sharer's lease.
                if not seed_provenance.release(path, owner):
                    logger.warning(
                        "could not durably release Crew's claim on %s; retaining the claim "
                        "until a later retry or process exit",
                        path,
                    )
                return
            aside, aside_ident = claimed
            # Re-checked AFTER the move-aside, not only at the top: the probe above
            # and the move are not one atomic step, and a sharer registers BEFORE it
            # validates -- so a sharer whose disk check passed read the file before
            # the move, and its registration is necessarily visible here. Without
            # this barrier the interleaving "probe sees none -> sharer registers and
            # validates -> move/forget/unlink proceed" frees the pathname under a
            # governed reader. Restore the moved inode and stand down instead.
            if seed_provenance.has_sharers(path):
                logger.info(
                    "%s gained a live sharer while its teardown was starting; restoring "
                    "the seed and leaving it in place for that session.",
                    path,
                )
                # No-clobber, because the pathname has been free since the
                # move-aside: a settings file the user recreated in that window is
                # theirs, and a replace-semantics restore would silently destroy it.
                self._restore_aside_without_clobber(aside, path, aside_ident)
                if not seed_provenance.release(path, owner):
                    logger.warning(
                        "could not durably release Crew's claim on %s; retaining the claim "
                        "with the restored seed until a later retry or process exit",
                        path,
                    )
                return
            # The file is now the moved inode ``aside`` and the pathname is free, so a
            # user replacement racing in lands at a fresh ``path`` this never touches.
            if not seed_provenance.forget(path, owner):
                # Not revoked on disk, so the file must stay: a restart would still
                # read Crew as its owner, and a later session repairs it. Restore it
                # under the pathname and hand back the live claim.
                logger.warning(
                    "could not durably revoke Crew's claim on %s; restoring the file so a "
                    "later session can re-seed or remove it rather than deleting it "
                    "behind a revocation that never reached the disk",
                    path,
                )
                self._restore_aside_without_clobber(aside, path, aside_ident)
                if not seed_provenance.release(path, owner):
                    logger.warning(
                        "could not durably release Crew's claim on %s; retaining the claim "
                        "with the restored seed until a later retry or process exit",
                        path,
                    )
                return
            try:
                unlink_errors: list[OSError] = []
                pinned_fs.unlink_verified_by_name(
                    path.parent,
                    aside.name,
                    aside_ident,
                    on_error=unlink_errors.append,
                )
                if unlink_errors:
                    raise unlink_errors[0]
            except OSError:
                # Revoke landed but the moved inode will not delete. Put it back under
                # the pathname and re-record, so the path is a repairable orphan rather
                # than a frozen ``.crew-gc`` no session names.
                logger.debug(
                    "could not remove %s after session reset; re-recording Crew's claim so "
                    "a later session can still re-seed or remove it",
                    path,
                    exc_info=True,
                )
                if payload is not None:
                    if self._restore_aside_without_clobber(aside, path, aside_ident):
                        if seed_provenance.record(path, payload, owner):
                            if not seed_provenance.release(path, owner):
                                logger.warning(
                                    "could not durably release Crew's re-recorded claim on "
                                    "%s; retaining it until a later retry or process exit",
                                    path,
                                )
                        else:
                            logger.warning(
                                "could not durably re-record Crew's restored seed at %s; "
                                "leaving the recoverable bytes in place",
                                path,
                            )
        except Exception:  # pragma: no cover - defensive; teardown must not raise
            logger.debug("could not settle Crew's settings seed at %s", path, exc_info=True)

    def _reset_state(self) -> None:
        """Reset all session state (call after process is dead)."""
        cleanup = getattr(self._process, "_windows_cleanup_state", None)
        if platform_compat.IS_WINDOWS and isinstance(
            cleanup, platform_compat._PendingWindowsTreeCleanup
        ):
            if not cleanup.retired:
                self._windows_tree_cleanup_failed = True
        if getattr(self, "_windows_tree_cleanup_failed", False) is True:
            logger.warning(
                "Retaining Windows client PID %s after incomplete tree cleanup", self._pid
            )
            return
        if self._process:
            for pipe in (self._process.stdin, self._process.stdout, self._process.stderr):
                if pipe:
                    try:
                        pipe.close()  # type: ignore[union-attr]
                    except Exception:
                        pass
        # Clean up sandbox temp files (macOS seatbelt profile)
        self._discard_sandbox_cleanup()
        # The process this projection's views were prepared for is dead, so its
        # lease goes now and this home's unleased views are pruned at once,
        # rather than whenever the object is collected or the next spawn prunes
        # (the Kiro IDE lists every view left in the agents directory). The
        # object stays: a frame still being drained may name one of its aliases.
        # A respawn prepares a fresh projection. getattr: clients built without
        # __init__ in tests.
        projection = getattr(self, "_native_skill_projection", None)
        if projection is not None:
            try:
                projection.release()
            except Exception:
                logger.debug("skill projection release failed", exc_info=True)
        # The pi MCP handoff file of a spawn that never reached an established
        # session: one unlink, the same shape as the sandbox file above.
        handoff_path, _payload = getattr(self, "_pi_mcp_handoff", ("", b""))
        self._pi_mcp_handoff = ("", b"")
        self._pi_mcp_server_names = ()
        if handoff_path:
            try:
                os.remove(handoff_path)
            except OSError:
                pass
        # The settings.local.json THIS session seeded is removed by
        # _discard_claude_settings_seed, which every caller awaits in the `try` of
        # the `finally` that reaches here -- it is async because the ownership hash,
        # the durable revoke and the unlink are all blocking, and this method is
        # synchronous and runs on the event loop. That pairing also means this may
        # run because the discard's await was CANCELLED: the flags below are already
        # cleared by then, and the shielded transaction still finishes the disk
        # half. getattr: _reset_state runs on clients built without __init__ in
        # tests, and such a client has no work_dir to derive the seed's path from.
        owner = getattr(self, "_seed_owner", "")
        if getattr(self, "_claude_settings_authored", False):
            self._claude_settings_authored = False
            self._claude_settings_written = None
        self._claude_settings_claim_unrevoked = False
        if getattr(self, "_work_dir", None) is not None:
            path = self._claude_local_settings_path()
            # The seed itself and its durable holder are removed by
            # ``_discard_claude_settings_seed``, which every production caller
            # awaits off the loop before reaching here. This synchronous fallback
            # hands back only the in-memory slot, taking no lock and doing no I/O,
            # so an interrupted discard cannot leave the event loop blocked.
            #
            # Unconditional, not gated on the instance flags: ``release_local`` is
            # a no-op unless ``_LIVE[key] == owner``, so it never touches a sibling
            # client object's lease held under a different token. It covers both
            # refused durable withdrawals -- the authored discard whose ``forget``
            # and ``release`` were refused with the seed restored, and the
            # un-revoked hand-back whose retry was refused -- and the authored one
            # is the case a flag-gated drop leaks: the refused ``release`` puts the
            # in-memory slot back while the discard's ``finally`` has already
            # cleared authorship, so nothing flag-gated runs and every later
            # session on this work_dir in this process is wedged
            # (``held_by_another`` and ``claim`` consult that slot first). The
            # persisted holder is the half this cannot reach: it carries this
            # process's live identity, so it stands until this process exits.
            seed_provenance.release_local(path, owner)
            # A sharer never wrote the file and never held the live claim; its
            # whole teardown is withdrawing the shared-reader registration, so
            # the owner's teardown (or a later adoption) stops holding the file
            # for a reader that is gone.
            if getattr(self, "_claude_settings_shared", False):
                seed_provenance.unshare_local(path, owner)
        # The trailing plain assignments are unconditional so a client built
        # without __init__ in tests resets clean too.
        self._claude_settings_shared = False
        self._permission_surface_share_validated = False
        # Drop the translated MCP array: the spec is read PER SPAWN, which is what
        # lets installing or toggling a server take effect on the next session, so
        # a replacement process must not inherit this one's snapshot.
        self._session_mcp_cache = None
        self._session_mcp_snapshot = None
        # Same per-spawn freshness rule as the array above: an edited spec must be
        # what the next session's guard judges, not this one's.
        self._mcp_ref_spec = None
        self._spec_denied_tools = frozenset()
        self._spec_zero_tools = False
        self._session_mcp_unhonoured = frozenset()
        self._session_harness_deny_rules = ()
        self._opencode_denies_in_force = None
        self._opencode_denies_unenforced = frozenset()
        self._opencode_native_mounts = frozenset()
        # Save PID state before clearing it. A root is confirmed exited only
        # when its own Process reports a reaped return code; a missing or
        # unreadable PID is not enough to reclaim its working directory.
        root_confirmed_dead = bool(self._process and self._process.returncode is not None)
        saved_pid = None if platform_compat.IS_WINDOWS else self._pid
        saved_start_token = self._spawn_start_token
        saved_child_pids = self._child_pids
        self._process = None
        self._pid = None
        self._stdin_stalled = False
        self._spawn_start_token = None
        # The instance id names the process that just ended; a replacement spawn
        # mints its own, so nothing may keep answering with this one in between.
        self._process_instance = ""
        # A walk wedged on the dead PID's /proc entry can never speak for the
        # replacement process, so release it with the oracle it sampled into.
        self._retire_liveness_state()
        self._session_id = None
        # The launches this record holds died with the process whose harness
        # started them (a backgrounded command or Workflow runs in that
        # process's own tree). Carrying it across a respawn would grant the
        # FRESH tree the watchdog's background-work hold — and its hard-ceiling
        # grace — on behalf of work that is already dead. "Never reset per
        # turn" (see __init__) is a statement about turns, not processes.
        self._background_launches = BackgroundLaunchRecord()
        # The adapter's cumulative cost counter is in-process: a replacement
        # process restarts it at zero, so the delta baseline must restart with
        # it or spend up to the old total is silently dropped — the monotonic
        # guard only catches a counter that has NOT yet caught back up to the
        # stale baseline. The current turn's already-billed delta is kept;
        # carry_over() zeroes it at the next turn boundary.
        self.last_prompt_stats.cost_session_usd = 0.0
        self._buffer.clear()
        self._stderr_lines.clear()
        # A fresh process opens a fresh registration window: cleared WITH the
        # ring, so the latch and the evidence it gates always describe the same
        # child.
        self._prompt_or_tool_seen = False
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
        self._stderr_task = None
        self._cancelled = False
        self._cancel_ts = 0.0
        self._cancel_grace_secs = _CANCEL_GRACE_SECS
        self._resumed = False
        # _turn_done is deliberately NOT rebuilt here. Its state is the truth
        # about the TURN, not the process: the prompt entries clear() it before
        # ensure_ready(), and ensure_ready() reaches this reset on the
        # dead-process respawn branch -- rebuilding the Event there (set OR
        # unset) would misreport the whole respawned turn. Cleared stays
        # cleared (turn in flight, the shutdown drain must wait for it); set
        # stays set (idle, see __init__). Waiters on the old object also keep
        # their Event instead of being orphaned.
        self._last_stop_reason = ""
        self._pending_oauth_requests.clear()
        self._oauth_emitted_servers.clear()
        # A retired session's report must not be read as the next one's. This
        # view exists to be evidence, and stale evidence is worse than none:
        # the replacement process re-initializes its servers from scratch and
        # will report again.
        self._mcp_report = McpSessionReport()
        #: Index into ``_mcp_notifications`` below which frames belong to a PRIOR
        #: session attempt and must not reach the report. See
        #: ``_begin_session_report``.
        self._mcp_report_frame_floor = 0
        # Untrack PIDs from the orphan tracking files — but ONLY those confirmed
        # dead. A child or root still alive after teardown survived the kill
        # (killpg only reaches the kiro-cli process group, so children in other
        # groups can outlive it, and a mid-init crash can race the descendant
        # scan in _kill_process()). Retaining a survivor's entry keeps it visible
        # to the periodic orphan sweep and next-startup cleanup_orphaned_sessions(),
        # which reap it; untracking one would orphan it permanently (all sweep
        # mechanisms key off these files) — the memory-leak this guards against.
        # These untrack helpers live in kiro_crew.session, which imports this
        # module transitively, so they must be imported inline.
        from kiro_crew.session import _untrack_child_pids, _untrack_pid, _untrack_session_pid
        from kiro_crew.session_pid import (
            _pid_gone_or_unmanaged,
            _untrack_pid_if_dead,
            _untrack_root_by_identity,
        )

        survivors: list[int] = []
        if saved_child_pids:
            dead_children = {
                pid: rec for pid, rec in saved_child_pids.items() if _pid_gone_or_unmanaged(pid)
            }
            survivors = [p for p in saved_child_pids if p not in dead_children]
            if dead_children:
                try:
                    _untrack_child_pids(dead_children)
                except Exception:
                    logger.debug(
                        "untracking child PIDs %s failed", list(dead_children), exc_info=True
                    )
            if survivors:
                logger.warning(
                    "Retained tracking for %d live child PID(s) that survived "
                    "teardown; orphan sweep will reap them: %s",
                    len(survivors),
                    survivors,
                )
        # Untrack parent kiro-cli PID (only if confirmed dead) -- by IDENTITY,
        # the way AcpRuntime retires on both its paths. "Confirmed dead" is a
        # fact about the process, not about its number: the kernel can hand the
        # number to a root spawned since, and a prefix-matched untrack would take
        # the successor's lines with it. The token read at spawn names the line
        # that is ours; the bare line goes only while the number is dead at that
        # moment. A root whose identity could not be read at spawn keeps the
        # prefix-matched untrack it always had.
        if saved_pid is not None:
            if _pid_gone_or_unmanaged(saved_pid):
                try:
                    if saved_start_token:
                        if not _untrack_root_by_identity(saved_pid, saved_start_token):
                            _untrack_pid_if_dead(saved_pid)
                    else:
                        _untrack_pid(saved_pid)
                        _untrack_session_pid(saved_pid)
                except Exception:
                    logger.debug("untracking PID %s failed", saved_pid, exc_info=True)
            else:
                logger.warning(
                    "Retained tracking for live root PID %s that survived "
                    "teardown; orphan sweep will reap it",
                    saved_pid,
                )
        self._child_pids = {}
        self._process_tree_confirmed_dead = root_confirmed_dead and not survivors

    async def _new_session_following_substitution(self) -> dict:
        """Issue ``session/new``; if the gateway substitutes the model, adopt it
        and re-issue once so a real session is actually created.

        The admin-tier policy advisory ("Model X is restricted ... Using Y
        instead.") comes back as a ``-32603`` *error frame with no sessionId* --
        the first attempt creates nothing. ``_wait_for_response`` records the
        substitute model in ``self._last_substitution_model``; here we pin
        ``self._model`` to it, re-seed ``settings.local.json`` (the claude
        backend builds a fresh SettingsManager per session/new, so the new model
        takes effect), and re-issue. Idempotent and bounded to ONE retry -- if the
        gateway substitutes again to the same/another restricted id we stop
        rather than loop. Returns the session/new response dict (possibly still
        without a sessionId, which the caller treats as a hard failure).

        The substitution retry is a claude-only path (kiro-cli never emits this
        advisory), and so is the re-seed: the kiro-cli branch writes no
        ``settings.local.json`` at all.
        """
        new_params: dict = {
            "cwd": await self._session_work_dir(),
            # kiro-cli loads servers from --agent; a harness in
            # ACP_BACKENDS_SESSION_MCP_ARRAY must be told here -- it reads no
            # agent spec of its own, so this array is the only channel Crew has
            # onto the session's MCP surface (translated from that same spec,
            # see acp/session_mcp.py). Empty on the kiro-cli path.
            # Pooled broker stubs are appended for kiro-cli: a session-injected
            # server outranks the same-named entry in the agent spec, which is
            # how pooling takes effect without writing a spec anywhere.
            # Each per-harness hook is spliced only for ITS OWN backend. An
            # edition overriding both would otherwise hand a claude session
            # codex's entries and vice versa -- and one entry whose transport the
            # adapter does not advertise fails the whole session/new, not just
            # that server. Both hooks are in-memory reads of a cache the spawn
            # path warmed, NOT executor hops: this call site is shared with
            # kiro-cli, and adapter work must not add a scheduling or failure
            # point to that backend's construction path (harness-parity H13).
            # The pooled read stays off the loop, as it already was.
            # A claude Side Chat session mounts no server: READ_ONLY refuses every
            # MCP call, and a tool that is not mounted cannot be pre-approved by
            # a settings allow rule. Checked at the splice, not in the seam, so
            # an edition's override of the seam cannot mount servers there.
            "mcpServers": [
                *(
                    self._claude_session_mcp_servers()
                    if self._is_claude and not getattr(self, "_side_read_only", False)
                    else []
                ),
                *(self._opencode_session_mcp_servers() if self._is_opencode else []),
                *(self._goose_session_mcp_servers() if self._is_goose else []),
                *(self._deepseek_session_mcp_servers() if self._is_deepseek else []),
                *(self._pi_session_mcp_servers() if self._is_pi else []),
                *(await asyncio.to_thread(self._pooled_mcp_servers)),
            ],
        }
        if self.backend in ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP:
            new_params["mcpServers"] = await self._prune_unstartable_mcp(new_params["mcpServers"])
        if self._is_claude:
            new_params["_meta"] = self._claude_session_meta()

        # The roster this session put ON THE WIRE. Distinct from the agent spec
        # on disk: the backend starts the spec's own servers too, so the frames
        # that come back are a superset of this list.
        self._begin_session_report(new_params.get("mcpServers"))
        # AFTER begin_session_report, which clears the report: the guard writes a
        # row on it, and writing before the clear would erase the finding.
        self._guard_unresolved_mcp_refs(new_params.get("mcpServers"))

        self._last_substitution_model = None
        if self._is_pi:
            await self._republish_pi_mcp_handoff()
        session_id = await self._send_request(METHOD_SESSION_NEW, new_params)
        session_resp = await self._wait_for_response(
            session_id,
            timeout=_INIT_TIMEOUT,
            method=METHOD_SESSION_NEW,
            expected_mcp=new_params.get("mcpServers"),
        )

        # Happy path: a real session came back.
        if session_resp.get("sessionId"):
            return session_resp

        # Substitution path: the gateway named a model it WILL serve. Adopt it,
        # re-seed settings so the backend resolves to it, and re-issue once.
        substitute = self._last_substitution_model
        if self._is_claude and substitute and substitute != self._model:
            # Redact the substitute name before logging. It is parsed straight
            # from the untrusted ACP backend advisory (data.details "Using X
            # instead") and the gateway log fans out to the dashboard activity
            # feed and Slack -- mirror the dual-redaction discipline applied
            # to the source advisory in _wait_for_response.
            _sub_log, _ = redact_exfiltration_urls(str(substitute))
            _sub_log, _ = redact_credentials(_sub_log)
            logger.warning(
                "ACP session/new returned a substitution advisory with no session; "
                "adopting gateway-served model %r and retrying session creation.",
                _sub_log,
            )
            self._model = substitute
            # Re-seed settings.local.json so the fresh SettingsManager the adapter
            # builds for the retry resolves the substitute model (it merges
            # settings sources each session/new).
            try:
                await asyncio.to_thread(self._write_claude_local_settings)
            except (OSError, ValueError, TypeError):
                # Narrow to realistic re-seed failure modes: OSError covers
                # disk / permission errors on the atomic write; ValueError
                # and TypeError cover registry / json shape surprises.
                # Never let re-seed failure mask the retry -- worst case, the
                # adapter resolves to whatever it had cached and we still
                # retry session/new on the substitute path.
                logger.warning("re-seed of settings.local.json failed", exc_info=True)
            # The writer owns every transition in the native permission surface
            # and invalidates the frozen MCP projection when that state changes.
            # Re-resolve off the event loop, then rebuild the exact array the retry
            # will send so a failed byte re-validation withholds rather than
            # re-delivering the first attempt's stale roster.
            resolved_mcp = await asyncio.to_thread(self._resolve_session_mcp_servers)
            self._session_mcp_cache = resolved_mcp
            new_params["mcpServers"] = [
                *(
                    self._claude_session_mcp_servers()
                    if self._is_claude and not getattr(self, "_side_read_only", False)
                    else []
                ),
                *(self._opencode_session_mcp_servers() if self._is_opencode else []),
                *(self._goose_session_mcp_servers() if self._is_goose else []),
                *(self._deepseek_session_mcp_servers() if self._is_deepseek else []),
                *(self._pi_session_mcp_servers() if self._is_pi else []),
                *(await asyncio.to_thread(self._pooled_mcp_servers)),
            ]
            if self.backend in ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP:
                new_params["mcpServers"] = await self._prune_unstartable_mcp(
                    new_params["mcpServers"]
                )
            # Rebuilt AFTER the array, from the same re-seed: the envelope carries
            # the setting sources the array was authorized under and the inline
            # settings naming the substitute model.
            new_params["_meta"] = self._claude_session_meta()
            self._begin_session_report(new_params.get("mcpServers"))
            self._guard_unresolved_mcp_refs(new_params.get("mcpServers"))
            self._last_substitution_model = None
            retry_id = await self._send_request(METHOD_SESSION_NEW, new_params)
            session_resp = await self._wait_for_response(
                retry_id,
                timeout=_INIT_TIMEOUT,
                method=METHOD_SESSION_NEW,
                expected_mcp=new_params.get("mcpServers"),
            )

        return session_resp

    def _initialize_params(self) -> dict[str, Any]:
        """The ``initialize`` request params, one spelling for every handshake.

        Read by :meth:`_initialize_session` and by the entitlement probe's own
        handshake (:meth:`_entitlement_probe_answer`), so the probe process is
        introduced to kiro-cli exactly as the session it probes for was.
        """
        protocol_version: int | str = _PROTOCOL_VERSION_BY_BACKEND.get(
            self.backend, PROTOCOL_VERSION
        )
        return {
            "protocolVersion": protocol_version,
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            "clientCapabilities": ACP_CLIENT_CAPABILITIES,
        }

    async def _fork_session(
        self, source_sid: str, message_id: str, capabilities: dict
    ) -> str | None:
        """Fork *source_sid* up to and including *message_id*: the new sid, or None.

        Only a harness in ``ACP_BACKENDS_FORK_AT_MESSAGE``, at a version that reads
        the ``_meta`` fork point sent here: a fork that ignored it would copy the
        WHOLE session, including everything after *message_id* that the caller is
        discarding.
        """
        session_capabilities = capabilities.get("sessionCapabilities")
        if not (
            self.backend in ACP_BACKENDS_FORK_AT_MESSAGE
            and _adapter_version_at_least(self._agent_version, CLAUDE_ACP_FORK_POINT_MIN_VERSION)
            and isinstance(session_capabilities, dict)
            and isinstance(session_capabilities.get("fork"), dict)
        ):
            return None
        params = {
            "sessionId": source_sid,
            "cwd": await self._session_work_dir(),
            "mcpServers": [],
            "_meta": {"jetbrains": {"air": {"fork": {"version": 1, "messageId": message_id}}}},
        }
        try:
            fork_id = await self._send_request(METHOD_SESSION_FORK, params)
            fork_resp = await self._wait_for_response(fork_id, timeout=_INIT_TIMEOUT)
        except (AcpError, AcpTimeoutError):
            logger.info(
                "session/fork of %s at %s failed, falling back to session/new",
                source_sid,
                message_id,
            )
            return None
        forked_sid = fork_resp.get("sessionId")
        if not isinstance(forked_sid, str) or not forked_sid:
            return None
        logger.info("ACP session forked: %s -> %s at %s", source_sid, forked_sid, message_id)
        return forked_sid

    async def _initialize_session(self) -> None:
        """Handshake: initialize → session/load or session/new → set_mode → set_model."""
        # 1. Initialize
        init_id = await self._send_request(METHOD_INITIALIZE, self._initialize_params())
        init_resp = await self._wait_for_response(init_id, timeout=_INIT_TIMEOUT)
        logger.info("ACP initialized (protocol=%s)", init_resp.get("protocolVersion"))

        # Whether this harness can restore a session at all, asked of whichever
        # capability the harness advertises it under. Both spellings are standard ACP
        # v1: ``loadSession`` is the flag for ``session/load``, and
        # ``sessionCapabilities.resume`` is the object -- an empty one means supported
        # -- for ``session/resume``. Keyed on the membership set rather than probing
        # both, so a harness cannot be read as restorable through a capability it
        # never advertised and then sent a verb it does not serve.
        capabilities = init_resp.get("agentCapabilities") or {}
        if self.backend in ACP_BACKENDS_RESUME_WITHOUT_LOAD:
            session_capabilities = capabilities.get("sessionCapabilities")
            self._can_load_session = isinstance(session_capabilities, dict) and isinstance(
                session_capabilities.get("resume"), dict
            )
        else:
            self._can_load_session = bool(capabilities.get("loadSession", False))
        # No harness this core drives narrows the session MCP array against the
        # advertised ``mcpCapabilities``: every one of them either reads no array
        # or accepts the shapes Crew already sends, opencode being the measured
        # case of the latter (stdio, http and sse alike -- see
        # ``_opencode_session_mcp_servers``). The harness that DOES narrow reads
        # the handshake on the core that drives it, in
        # ``acp.harness.codex.session_mcp_servers``, so nothing is held here.
        self._agent_version = agent_version_from_init(init_resp)
        self._agent_version_read = True
        self._steering_advertised = steering_advertised(init_resp)
        self._note_pi_adapter_version()
        self._note_goose_version()

        # The subprocess has now read its agent spec, which closes the window the
        # pre-spawn snapshot opened: a write landing before this point is caught here,
        # and one landing after cannot change what kiro-cli already loaded. BEFORE
        # session/load or session/new, so a session is never created on a spec nobody
        # verified. Off-loop: one stat, and a hash only when the stat differs.
        #
        # Converted to ``AcpError`` for the SAME reason the pre-spawn gate above is:
        # that is the type ``ensure_ready`` handles, and its handler is what ends the
        # child and drops the half-registered session state --
        # ``_cleanup_failed_live_spawn`` kills the process, releases the workspace
        # binding and discards the settings seed, and ``_reset_state`` closes the
        # pipes and clears the PIDs, the session id and the per-spawn spec caches.
        # A raw ``DerivedSpecStale`` (a ``RuntimeError``) matches none of those
        # handlers and would leave a live child running on an unverified spec. The one
        # retry that handler allows is not a way past this: it respawns, so it goes
        # back through the pre-spawn gate and either re-derives from the default as it
        # now stands or refuses again. Nothing continues on this child.
        try:
            await asyncio.to_thread(require_unchanged_derived_spec, self._derived_spec_snapshot)
        except DerivedSpecStale as exc:
            raise AcpError(str(exc)) from exc

        # 2. Try session/load if we have a resume ID and kiro-cli supports it
        self._resumed = False
        resume_sid = self._resume_session_id
        self._resume_session_id = None  # consume — no retry loop
        fork_request = self._fork_request
        self._fork_request = None
        if fork_request and not resume_sid and self._can_load_session:
            # A successful fork is then restored like any resume.
            resume_sid = await self._fork_session(*fork_request, capabilities)
        sent_snapshot: DerivedSpecSnapshot | None = None

        if resume_sid and self._can_load_session:
            # Only attempt session/load when the prior session transcript
            # actually exists on disk. Without this guard a stale persisted SID
            # (e.g. a slot reopened for a brand-new conversation) triggers a
            # session/load that REPLAYS the old transcript on top of the fresh
            # system prompt + memory injection — inflating base context to
            # ~38% on turn 1. kiro-cli stores transcripts at ~/.kiro/sessions/
            # cli/<sid>.json; a missing transcript falls back to session/new
            # (a genuinely fresh start).
            if self.backend in ACP_BACKENDS_HARNESS_OWNED_SESSIONS:
                # The harness keeps its own session records and resolves them from
                # the sessionId, so there is no Crew-side file to name. Gating on
                # the kiro transcript here would make file_ok always False -- such
                # a session could never resume, it would silently start fresh every
                # time -- which is why the _meta block below gives it no
                # session_file either. For claude the SDK transcript-path resolver
                # is the internal companion's; the public core simply attempts the
                # load.
                session_file = ""
                file_ok = True
            else:
                session_file = str(kiro_sessions_dir() / f"{resume_sid}.json")
                file_ok = Path(session_file).exists()
            if file_ok:
                # WHICH restore verb, from the same membership set that chose the
                # capability above. The two calls share a contract --
                # ``ResumeSessionRequest`` carries the same fields as
                # ``LoadSessionRequest``, and their responses carry the same fields --
                # so the params built below and every check after them are unchanged,
                # and only the method name differs. That is why this is a set and a
                # constant rather than a restore path per harness (harness-parity
                # H13).
                #
                # Resolved BEFORE the try, not inside it: the failure log in the
                # handler names this method, and a params-building failure would
                # otherwise reach that log with the name unbound.
                restore_method = (
                    METHOD_SESSION_RESUME
                    if self.backend in ACP_BACKENDS_RESUME_WITHOUT_LOAD
                    else METHOD_SESSION_LOAD
                )
                try:
                    load_params: dict = {
                        "sessionId": resume_sid,
                        "cwd": await self._session_work_dir(),
                        # kiro-cli gets its servers via --agent; a session-array
                        # backend must receive them here as well -- a resumed
                        # session re-declares everything Crew mounts on it or comes
                        # back with no tools (see session/new above). Pooled stubs are
                        # re-declared so a resumed session keeps talking to the
                        # broker. Gated per backend, and in-memory here vs
                        # off-loop there, for the same reasons as session/new.
                        "mcpServers": [
                            *(
                                self._claude_session_mcp_servers()
                                if self._is_claude and not getattr(self, "_side_read_only", False)
                                else []
                            ),
                            *(self._opencode_session_mcp_servers() if self._is_opencode else []),
                            *(self._goose_session_mcp_servers() if self._is_goose else []),
                            *(self._deepseek_session_mcp_servers() if self._is_deepseek else []),
                            *(self._pi_session_mcp_servers() if self._is_pi else []),
                            *(await asyncio.to_thread(self._pooled_mcp_servers)),
                        ],
                    }
                    if self.backend in ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP:
                        load_params["mcpServers"] = await self._prune_unstartable_mcp(
                            load_params["mcpServers"]
                        )
                    if self._is_claude:
                        load_params["_meta"] = self._claude_session_meta()
                    elif self.backend not in ACP_BACKENDS_HARNESS_OWNED_SESSIONS:
                        # The kiro family reads its transcript path from _meta. A
                        # harness that owns its sessions reads no _meta of ours,
                        # so it gets neither key rather than a kiro session_file
                        # it would not know what to do with.
                        load_params["_meta"] = {"_kiro.dev/session_file": session_file}
                    self._begin_session_report(load_params.get("mcpServers"))
                    # A resumed session re-declares everything Crew mounts on it, so it
                    # can be short of a referenced server exactly as a fresh one can.
                    self._guard_unresolved_mcp_refs(load_params.get("mcpServers"))
                    if self._is_pi:
                        await self._republish_pi_mcp_handoff()
                    load_id = await self._send_request(restore_method, load_params)
                    load_resp = await self._wait_for_response(
                        load_id,
                        timeout=_INIT_TIMEOUT,
                        method=restore_method,
                        expected_mcp=load_params.get("mcpServers"),
                    )
                    # A ``modes`` block is the shape kiro-cli and claude return on a
                    # successful load, and its absence on THOSE harnesses means the
                    # load did not really take. A member of
                    # ACP_BACKENDS_LOAD_WITHOUT_MODES never returns one, so for it a
                    # response that is not an error IS the successful load -- gating
                    # it on ``modes`` would fall through to session/new and discard
                    # the conversation the harness had just restored.
                    if "modes" in load_resp or self.backend in ACP_BACKENDS_LOAD_WITHOUT_MODES:
                        self._session_id = resume_sid
                        self._resumed = True
                        self._capture_available_models(load_resp)
                        sent_snapshot = self._session_mcp_snapshot
                        if self._uses_advertised_model_selection:
                            await self._persist_advertised_models_if_changed()
                            await self._reseed_after_capture()
                        self._store_session_config(load_resp)
                        logger.info("ACP session resumed: %s", resume_sid)
                except (AcpError, AcpTimeoutError):
                    logger.info(
                        "%s failed for %s, falling back to session/new",
                        restore_method,
                        resume_sid,
                    )
                else:
                    # The ``else`` of the try, so this runs ONLY on a load that did not
                    # raise -- and OUTSIDE it, so the refusal below cannot be caught as
                    # a failed load. Gated here rather than inside the method so the
                    # first-class kiro path gains no call and no failure point in
                    # service of an adapter (harness-parity H13).
                    #
                    # A RESTORED session carries the mode it was left in, which the
                    # environment seed does not re-apply, so this is what keeps a
                    # resumed session from coming back permissive. Conditioned on
                    # ``_resumed`` because a load that returned no ``modes`` block did
                    # not take: that path falls through to session/new, which does its
                    # own read-back on the response that replaces this one.
                    if self._is_goose and self._resumed:
                        self._verify_goose_routing(load_resp)
                    if self._is_claude and self._resumed:
                        await self._pin_claude_starting_mode(load_resp)
            else:
                logger.info("Session file missing for %s, skipping load", resume_sid)

        # 3. Create new session if load didn't succeed. On the claude backend a
        # model-substitution advisory comes back as an error frame with NO
        # sessionId; _new_session_following_substitution adopts the substitute
        # model and re-issues once so a real session is actually created.
        if not self._session_id:
            # Capture the requested model before the helper runs. The helper resets
            # self._last_substitution_model = None on every return path, so we can't
            # use it to detect whether substitution happened. Comparing self._model
            # before vs after is the reliable signal.
            model_before = self._model
            session_resp = await self._new_session_following_substitution()
            sent_snapshot = self._session_mcp_snapshot
            self._session_id = session_resp.get("sessionId")
            self._capture_available_models(session_resp)
            if self._uses_advertised_model_selection:
                await self._persist_advertised_models_if_changed()
                await self._reseed_after_capture()
            self._store_session_config(session_resp)
            # Before the first prompt, and before the session id is even checked: a
            # session that cannot establish the asking posture is one where none of
            # Crew's tool controls execute. Gated HERE rather than inside the method so
            # the first-class kiro path gains no call and no failure point in service of
            # an adapter (harness-parity H13).
            if self._is_goose:
                self._verify_goose_routing(session_resp)
            if not self._session_id:
                # Both the initial attempt and the substitution retry failed to
                # yield a session. Raise a clear, actionable error instead of
                # letting the next step die on the opaque "Cannot set config
                # option before session is initialized" guard.
                # self._model can be backend-derived (the substitute adopted
                # by _new_session_following_substitution from the ACP advisory),
                # and AcpError chains through to the dashboard activity feed
                # and Slack. Dual-redact before interpolating, same discipline
                # as the logger paths.
                # Standardize redacted-local naming across this file: the
                # convention is _<source>_log so the reader sees what was redacted.
                _model_for_error_log, _ = redact_exfiltration_urls(str(self._model))
                _model_for_error_log, _ = redact_credentials(_model_for_error_log)
                raise AcpError(
                    "session/new returned no sessionId"
                    + (
                        f" even after adopting substitute model {_model_for_error_log!r}"
                        if self._model != model_before
                        else ""
                    )
                    + "; the backend did not create a session."
                )
            # self._model can be backend-derived if _new_session_following_substitution
            # adopted the gateway substitute. Redact it consistently with the
            # warning-log path so any URL / credential-shaped substitute id never
            # reaches the dashboard activity feed or Slack unredacted.
            _model_log, _ = redact_exfiltration_urls(str(self._model))
            _model_log, _ = redact_credentials(_model_log)
            logger.info("ACP session created: %s (model=%s)", self._session_id, _model_log)
            if self._is_claude:
                await self._pin_claude_starting_mode(session_resp)
        self._last_activity = time.monotonic()
        # The session exists, so the pi process that read the MCP handoff file has
        # started; the file has no further reader and goes now.
        if self._is_pi:
            await self._discard_pi_mcp_handoff()

        # Seek to end of JSONL so we only read new tool results.
        # claude-agent-acp stores sessions via its own SDK, not ~/.kiro/ — skip.
        if self._session_id and self._is_kiro:
            _jpath = kiro_sessions_dir() / f"{self._session_id}.jsonl"
            try:
                self._jsonl_pos = _jpath.stat().st_size if _jpath.exists() else 0
            except OSError:
                self._jsonl_pos = 0

        # The host has now consumed the ``mcpServers`` array, by whichever route created
        # the session -- ``session/load``, ``session/new``, or the substitution retry --
        # and every route sends the same cached array. For an array-backed host that
        # array IS the derived spec: the child reads no spec of its own, so the
        # ``initialize`` check above (which judges the kiro-cli ``--agent`` read) does
        # not cover it. This is the other end of THAT bracket: a write landing between
        # the array's build and this point is caught, and one landing after cannot
        # change what the host already registered. ``None`` for every agent that
        # mirrors nothing, and for the kiro-cli path, which sends no array. Converted
        # to ``AcpError`` for the reason the two checks above are: that is the type
        # ``ensure_ready`` handles, and its handler is what ends the child and drops
        # the half-registered session state.
        try:
            await asyncio.to_thread(require_unchanged_derived_spec, sent_snapshot)
        except DerivedSpecStale as exc:
            raise AcpError(str(exc)) from exc

        # 4. Activate agent via set_mode -- kiro only. claude-agent-acp does handle
        #    session/set_mode, but its modeIds are permission modes (default,
        #    acceptEdits, plan, ...), never an agent name, so there is no agent to
        #    activate there. _pin_claude_starting_mode uses it for the permission mode.
        #    Guard (A): fire only when the backend advertised this agent, or
        #    advertised no modes at all (older kiro-cli / fake → attempt,
        #    backward-compatible). If modes ARE advertised but this agent is
        #    absent, its ~/.kiro/agents/<agent>.json didn't load — FAIL CLOSED
        #    with an actionable error rather than silently running kiro-cli's
        #    default (broader) mode, which for a restricted agent is a privilege
        #    escalation. Self-heal (B, in _spawn) regenerates the managed default
        #    so the common case never reaches this branch.
        if self._is_kiro:
            # The advertised ids were rewritten to each agent's DECLARED name by
            # the projection's reverse map (frame()). The launch identity is
            # ``self._agent`` -- kiro-cli resolves ``--agent`` by the declared
            # name, so that is the id the start compares against -- while the
            # modeId sent is the same launch identity, which request() tolerates
            # for the agent's own first activation.
            if not self._modes_advertised or self._agent in self._available_mode_ids:
                # The answer is not awaited, so the model this activation puts the
                # session on is judged from its agent/switched frame wherever that
                # frame is read (_note_agent_switched).
                self._activated_model = ""
                self._mode_frame_awaited = True
                self._mode_frame_superseded = False
                await self._send_request(
                    METHOD_SET_MODE,
                    {"sessionId": self._session_id, "modeId": self._agent},
                )
                logger.info("ACP agent activated: %s", self._agent)
            else:
                cause, remedy = await asyncio.to_thread(unavailable_mode_explanation, self._agent)
                raise AcpError(
                    f"Agent mode {self._agent!r} is not available on this session "
                    f"(advertised modes: {self._available_mode_ids or 'none'}); {cause} "
                    f"Refusing to run the backend default mode in its place. {remedy}"
                )

        # 5. Set model — override if KiroCrew config specifies non-default.
        await self._apply_startup_model()

        # 6. Arm permission routing for harnesses whose asking is a session
        #    config option. AFTER the model apply (both write config options, and
        #    the permission one must land last) and before any prompt can run:
        #    _initialize_session is entirely pre-prompt, which is what makes this
        #    placement the guarantee rather than a best effort.
        #    Gated HERE rather than inside the method, so the first-class Kiro path
        #    gains no call, no await and no failure point in service of an adapter
        #    (harness-parity H13). A positive membership test, never "not claude".
        if acp_tool_gate.routing_for(self.backend) is acp_tool_gate.Routing.SESSION_CONFIG:
            await self._apply_session_permission_routing()

        # (settings.local.json is re-seeded up in step 2/3, beside the model-cache
        #  persist, rather than as a step of its own down here: see
        #  _reseed_after_capture on why it rides an EXISTING adapter-only branch.)

        # Drain MCP server init notifications
        await self._drain_notifications()

        # The drain usually reads step 4's agent/switched frame; judge the model
        # it named before the first prompt can run on it.
        await self._judge_activated_model()

    async def ensure_ready(self) -> None:
        """Ensure process is spawned and session is initialized.

        Runs before EVERY prompt, so the warm path must stay syscall-free:
        the work dir is created once per instance (off-loop) and remembered —
        a per-prompt mkdir would tax every prompt with a blocking syscall
        whose latency scales with the parent directory's entry count.
        Re-creating the directory later could not repair a live child anyway:
        a process's cwd is bound to the inode, not the path.
        """
        if (
            platform_compat.IS_WINDOWS
            and self._process
            and (
                self._process.returncode is not None
                or getattr(self, "_windows_tree_cleanup_failed", False) is True
            )
        ) or getattr(self, "_stdin_stalled", False):
            # A stalled child stopped reading stdin with a frame possibly
            # buffered: it is killed BEFORE a replacement starts, so it cannot
            # act on that frame while the new turn runs.
            await self._kill_process(force=True)
            if (
                getattr(self, "_stdin_stalled", False)
                and self._process is not None
                and self._process.returncode is None
            ):
                # Not confirmed dead: stay stalled, so the next ensure_ready
                # retries the kill rather than spawning beside the old child.
                raise AcpProcessDied(
                    "the stalled ACP child could not be confirmed dead; "
                    "not starting a replacement beside it"
                )
            try:
                await self._discard_claude_settings_seed()
            finally:
                self._reset_state()
        if not self._work_dir_ready:
            await asyncio.to_thread(ensure_directory, self._work_dir)
            self._work_dir_ready = True
        if self._process and self._process.returncode is None and self._session_id:
            if getattr(self, "_activated_model", ""):
                # An agent/switched frame read after the start drain (inside a
                # prompt) is judged here, before the next prompt goes out.
                await self._judge_activated_model()
            return

        # Telemetry (kirocrew.session.startup.duration): time the cold-start work
        # below (spawn + session init) and emit in the finally so every exit path
        # — success, auth-required, error — is measured. The warm fast-path above
        # is intentionally NOT measured (no startup work). Best-effort: a
        # telemetry failure must never affect session startup.
        _startup_t0 = time.monotonic()
        _startup_spawned = False
        # Default "error": any exit that is NOT the explicit success path below
        # — including an unexpected non-Acp exception propagating through the
        # finally — is recorded as a failure, never a false "ready".
        _startup_outcome = "error"
        try:
            # Retry once — kiro-cli first launch can be slow (MCP server init),
            # and transient failures (MCP crash, bad config read) are recoverable.
            for attempt in range(2):
                try:
                    if self._process and self._process.returncode is not None:
                        await self._discard_bound_workspace()
                        # `finally`, because the discard is an await and this runs on
                        # cancellable paths: the in-memory reset must land even when
                        # the cancellation arrives during the seed's settle. The
                        # settle itself is shielded, so it completes regardless.
                        try:
                            await self._discard_claude_settings_seed()
                        finally:
                            self._reset_state()

                    if not self._process:
                        await self._spawn()
                        _startup_spawned = True

                    await self._initialize_session()
                    try:
                        await self._snapshot_process_tree()
                    except Exception:
                        logger.warning("Failed to snapshot process tree", exc_info=True)

                    _startup_outcome = "ready"
                    return
                except AcpToolGateUnroutable:
                    # Non-retryable BY CONSTRUCTION (see the class docstring): the
                    # refusal is a configuration fact, so a respawn re-reads the same
                    # answer and refuses again -- a wasted spawn plus teardown that
                    # also spends the reconnect budget this DISTINCT type exists to
                    # protect. Must sit BEFORE the generic transport handler, because
                    # it subclasses AcpError and would otherwise be retried by it,
                    # which is the shape that made the distinct type decorative.
                    _startup_outcome = "tool_gate_unroutable"
                    await self._cleanup_failed_live_spawn()
                    self._reset_state()
                    raise
                except (AcpTimeoutError, AcpError, OSError) as exc:
                    # A sandbox that refused to initialize is DETERMINISTIC, so
                    # the second attempt below would rebuild the same profile on
                    # the same host and be refused identically -- paying a spawn
                    # and a teardown to report the same thing later, with the one
                    # line that names the fix buried in a generic exit card. Fail
                    # fast with the layer named instead, exactly as the
                    # ``AcpToolGateUnroutable`` arm above does for a
                    # configuration refusal. Placed inside the generic handler
                    # rather than as its own ``except`` clause because the
                    # signature is on the CHILD's stderr, not on the exception:
                    # the same refusal surfaces as an EOF ``AcpError``, as an
                    # ``AcpTimeoutError`` when the child dies before answering
                    # ``initialize``, and as an ``OSError`` on the write that
                    # follows it.
                    sandbox_failure = await self._classify_failed_start(self._sandbox_init_failure)
                    if sandbox_failure is not None:
                        _startup_outcome = "sandbox_init_failed"
                        await self._cleanup_failed_live_spawn()
                        self._reset_state()
                        raise sandbox_failure from exc
                    # The harness answered with its OWN "no provider / not signed
                    # in" words (declared in ``host_auth``). Deterministic like the
                    # sandbox refusal above: a fresh process reads the same missing
                    # configuration, so fail fast with the message that names the
                    # fix instead of a retry and then a raw JSON-RPC frame. A plain
                    # non-transient ``AcpError`` rather than ``AcpAuthRequired``: the
                    # dashboard reads that type as "Kiro is signed out" and would
                    # mark a valid kiro-cli login as not ready.
                    if isinstance(exc, AcpError) and host_auth.reports_signed_out(
                        self.backend, str(exc)
                    ):
                        _startup_outcome = "auth_required"
                        await self._cleanup_failed_live_spawn()
                        self._reset_state()
                        raise AcpError(
                            host_auth.signed_out_message(self.backend),
                            transient=False,
                            user_worded=True,
                        ) from exc
                    if attempt == 0:
                        logger.warning("ACP init failed (%s), retrying with fresh process...", exc)
                        await self._cleanup_failed_live_spawn()
                        self._reset_state()
                        if isinstance(exc, OSError):
                            await asyncio.sleep(_ACP_RESPAWN_BACKOFF_S)
                    else:
                        # Read the ring BEFORE the cleanup below clears it: a
                        # startup that died with a throttled registration on its
                        # stderr is pre-prompt by construction, so the typed
                        # transient subclass is the accurate verdict here too.
                        _throttled = await self._classify_failed_start(
                            self._registration_throttle_line
                        )
                        # AcpAuthRequired subclasses AcpError; label it distinctly
                        # so a not-logged-in exit is never counted as a generic
                        # startup error. (Only a harness's declared signed-out
                        # phrase fails fast, above; other auth answers keep the retry.)
                        if isinstance(exc, AcpAuthRequired):
                            _startup_outcome = "auth_required"
                        elif _throttled is not None:
                            _startup_outcome = "registration_rate_limited"
                        else:
                            _startup_outcome = "error"
                        await self._cleanup_failed_live_spawn()
                        self._reset_state()
                        if _throttled is not None and not isinstance(exc, AcpAuthRequired):
                            raise registration_rate_limited_error(
                                "ACP session startup failed", _throttled
                            ) from exc
                        raise
        finally:
            try:
                # circular import: importing get_recorder at module top would
                # form config.loader -> acp.types -> acp.client -> metrics.provider
                # -> config.loader (provider reads KiroCrewConfig). Keep it lazy so
                # provider is never loaded during config.loader's import chain.
                from kiro_crew.metrics.provider import get_recorder

                get_recorder().histogram(
                    "kirocrew.session.startup.duration",
                    (time.monotonic() - _startup_t0) * 1000.0,
                    unit="ms",
                    attrs={"outcome": _startup_outcome, "spawned": _startup_spawned},
                )
            except Exception:  # never let telemetry break session startup
                logger.debug("session startup metric emit failed", exc_info=True)

    async def _classify_failed_start(self, classify: Callable[[], Awaitable[_T]]) -> _T:
        """Run one of ``ensure_ready``'s failure-arm classifiers, cleaning up on a cancel.

        Each classifier settles the stderr drain first, which waits, and a cancel
        landing in that wait propagates (see :func:`settle_drain`). The failure arm
        has already decided to discard this child by then, and leaving it would
        keep a live process with a half-built session that the next
        ``ensure_ready`` takes the warm path straight back onto. So the cleanup the
        arm would have run runs before the cancel goes on, with the reset in a
        ``finally`` so a second cancel during the kill still clears the state. A
        cleanup failure is logged rather than raised: it must not replace the
        cancel, which is what the caller is owed.
        """
        try:
            return await classify()
        except asyncio.CancelledError:
            try:
                await self._cleanup_failed_live_spawn()
            except Exception:
                logger.warning("ACP failed-start cleanup after a cancel failed", exc_info=True)
            finally:
                self._reset_state()
            raise

    async def _settle_stderr(self, timeout: float = 0.5) -> None:
        """Bounded wait for the stderr drain, so its ring can be read consistently.

        Same budget as the EOF branch of :meth:`_read_message`, lifted here because
        the other failure shapes that reach a classifier -- an ``initialize``
        timeout, an ``OSError`` on the write after the child died -- never pass
        through it. A cancel of the caller propagates; see :func:`settle_drain`.
        """
        await settle_drain(self._stderr_task, timeout)

    async def _sandbox_init_failure(self) -> AcpSandboxInitFailed | None:
        """The classified sandbox-init error for this child's stderr, or ``None``.

        Reads the ring buffer rather than the exception: the refusal is printed by
        the child (or by the wrapper that could not start it), and the exception
        that reaches the caller is whatever transport symptom followed -- an EOF,
        an ``initialize`` timeout, a broken pipe.

        Returns ``None`` when nothing was retained, which is the case for a
        restricted-memory session: stderr is not kept there by design, so this
        cannot classify and must not guess. Those sessions keep today's retry
        behaviour rather than getting a made-up verdict.

        Settles the drain first, for the reason the EOF branch of
        ``_read_message`` does: the ring is filled by the drain task while the
        failure that brings us here can arrive from the stdout side or from a
        timeout that never touched it, so a straight read can miss a line already
        in the pipe. Bounded, and swallowing everything except a cancel of the
        caller -- this is already a failure path (see :func:`settle_drain`).
        """
        await self._settle_stderr()
        if not self._stderr_lines:
            return None
        haystack = "\n".join(self._stderr_lines)
        if not is_sandbox_init_failure_output(haystack):
            return None
        # The WHOLE tail, not its last line: the launcher's own refusal line is
        # what corroboration keys on, and a burst can end on the child's generic
        # "failed to spawn" instead.
        detail, _ = redact_exfiltration_urls(haystack)
        detail, _ = redact_credentials(detail)
        return await sandbox_init_failure(
            detail,
            crew_wrap=self._sandbox_wrapped_by_crew,
            mode=self._sandbox_mode,
            extra_hidden_dirs=self._sandbox_hidden_dirs,
        )

    async def _registration_throttle_line(self) -> str | None:
        """One redacted stderr line showing a throttled registration, or ``None``.

        Refuses to classify once this process has produced a non-thinking text
        chunk or dispatched a tool (``_prompt_or_tool_seen``): the transient
        verdict this evidence buys licenses the retry ladders to act, and a
        stale throttle line surviving in the ring past real work must never
        hand that verdict to a death whose replay could repeat side effects.
        The latch clears with the ring on respawn, so a recovered throttle
        followed by a fresh child opens a fresh window.

        Returns ``None`` when nothing was retained, which is the case for a
        restricted-memory session: stderr is not kept there by design, so this
        cannot classify and must not guess. Those sessions keep the generic
        death surface rather than getting a made-up verdict.

        Settles the drain first, for the reason :meth:`_sandbox_init_failure`
        does: the ring is filled by the drain task while the death that brings
        us here is discovered on the stdout side, so a straight read can miss a
        line already in the pipe. Redacted before it leaves: the line rides an
        exception message that reaches session cards and persisted errors, and
        child stderr is untrusted subprocess output that can echo a credential.
        """
        if getattr(self, "_prompt_or_tool_seen", True):
            return None
        await self._settle_stderr()
        if not self._stderr_lines:
            return None
        line = registration_throttle_line("\n".join(self._stderr_lines))
        if line is None:
            return None
        line, _ = redact_exfiltration_urls(line)
        line, _ = redact_credentials(line)
        return line

    async def shutdown(self) -> None:
        """Gracefully stop the ACP process."""
        self._process_tree_confirmed_dead = False
        # Tear down an in-flight entitlement probe first: its throwaway client
        # spawned its OWN kiro-cli (and whatever MCP servers that started), and
        # nothing else reaps it -- `_kill_process` below kills only THIS session's
        # tree. Cancelling the future runs `_probe_advertised_models`'s `finally`,
        # which shields the probe client's own `shutdown()`, so the probe process
        # dies with the client that spawned it instead of outliving it ~270s (or,
        # on loop teardown, forever, until the next-startup PID sweep). Bounded so
        # a wedged probe cannot hold this shutdown.
        inflight = getattr(self, "_entitlement_probe_inflight", None)
        if inflight is not None and not inflight.done():
            from kiro_crew.acp.runtime import _TERMINATE_TIMEOUT

            inflight.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(inflight), timeout=_TERMINATE_TIMEOUT)
            except (Exception, asyncio.CancelledError):
                logger.debug("entitlement probe teardown on shutdown failed", exc_info=True)
        # `_reset_state` in a `finally`, because `_kill_process` can leave
        # through several doors: it awaits four `run_in_executor` calls (child
        # scan, record capture, escaped-child sweep) that are not individually
        # guarded, `subprocess_executor()` refuses new work once the loop is
        # tearing down, and `asyncio.CancelledError` is a `BaseException` --
        # shutdown being exactly when cancellation arrives.
        #
        # Nothing retries. Every caller treats this as terminal and drops the
        # client immediately afterwards (`AcpWorker` and `_shutdown_quietly`
        # both `except Exception: log` and then set their reference to None), so
        # a skipped reset is permanent: the pipes stay open, the sandbox temp
        # files stay on disk, and for the claude backend
        # `.claude/settings.local.json` -- written to hold `bypassPermissions`
        # for the session -- survives the process it belonged to.
        #
        # Running it after a failed kill is safe by construction: `_reset_state`
        # untracks only PIDs it confirms dead and deliberately RETAINS tracking
        # for survivors so the orphan sweep still reaps them.
        try:
            await self._kill_process(force=True)
        finally:
            await self._discard_bound_workspace()
            # `finally` for the same reason the kill above has one: `_reset_state`
            # untracks the PIDs and must run even if the seed's await is cancelled.
            # The settle is shielded, so the disk half completes either way.
            try:
                await self._discard_claude_settings_seed()
            finally:
                self._reset_state()  # untracks all PIDs (root + children)

    # ── JSON-RPC Transport ──

    async def _send_request(
        self,
        method: str,
        params: dict,
        *,
        on_id: Callable[[int], None] | None = None,
    ) -> int:
        """Write one request frame and return its id.

        ``on_id`` is called with the id before the frame is written, so a caller
        that waits for the answer can register for it before the write's drain
        can suspend and the turn's read loop can read that answer.
        """
        if not self._process or not self._process.stdin:
            raise AcpError("ACP process not running")

        projection = getattr(self, "_native_skill_projection", None)
        if projection is not None:
            params = projection.request(method, params)
        req_id = self._next_req_id()
        if on_id is not None:
            on_id(req_id)
        req = JsonRpcRequest(method=method, params=params, id=req_id)
        data = json.dumps(req.to_dict()) + "\n"
        if method == METHOD_SET_MODEL:
            # The session runs this model from here on, whatever an earlier
            # agent activation put it on (see _note_agent_switched). Marked
            # before the write, whose drain can suspend while a read runs.
            self._activated_model = ""
            if getattr(self, "_mode_frame_awaited", False):
                self._mode_frame_superseded = True
        try:
            # Bounded on the reader's PROGRESS, not held across a raw drain: a
            # request write that parks while the backend is flow-control-paused
            # must not hold the write lock (acute on the shared runtime, where a
            # co-tenant waits behind the lock, but a single session can self-wedge
            # too). A stall raises AcpProcessDied, the same recovery a closed pipe
            # already gets.
            await self._write_request_bounded(data.encode(), req_id, method)
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AcpProcessDied(f"ACP process pipe broken: {exc}") from exc
        self._last_activity = time.monotonic()
        return req_id

    async def _write_request_bounded(self, data: bytes, request_id: int, method: str) -> None:
        """Write a REQUEST frame under the write lock and the same no-progress bound.

        The request twin of :meth:`_write_response_bounded`. A request frame is
        caller-sized (a prompt may carry any number of image blocks), which is
        why the bound is on the reader's PROGRESS rather than on elapsed time: a
        reader still consuming keeps the wait alive, and only a writer whose
        buffer has not shrunk for ``_RESPONSE_WRITE_BOUND_SECS`` is the
        reader-gone stall. Holding the write lock across an unbounded ``drain()``
        here would let a flow-control-paused backend park it forever WITH the
        lock held; on the shared runtime that wedges every co-tenant session
        behind the lock at 0 CPU. A stall is mapped to
        ``AcpProcessDied`` so the caller takes the existing session-reset +
        bounded-requeue recovery.

        A DRAIN_STALL can leave the frame buffered in a backend that resumes
        reading, but the kill-and-reap that would try to make delivery impossible
        is NOT done here: it cannot be guaranteed (a close/EOF keeps flushing the
        buffered bytes, and a wedged child is exactly the one that fails to exit
        within the grace, so death is not confirmed before the raise), and on the
        shared runtime it would bypass the ownership authorization and terminate
        sibling sessions. Instead a DRAIN_STALL raises ``AcpProcessDied`` with
        ``ambiguous_delivery`` set, which ``build_recovery_requeue`` reads to
        resume from restored state rather than replay a prompt the backend may
        have consumed; a LOCK_STALL wrote no byte, so it is not ambiguous and its
        replay is safe. The phase appears only in the log. Either phase marks
        the client stalled, so nothing reuses it: the next
        ``ensure_ready`` kills the child before spawning its replacement.
        """
        assert self._process is not None and self._process.stdin is not None
        result = await write_request_frame_bounded(
            self._process.stdin,
            self._stdin_write_lock(),
            data,
            bound_secs=transport_framing._RESPONSE_WRITE_BOUND_SECS,
        )
        if result is RequestWriteResult.OK:
            return
        safe_id = _loggable_request_id(request_id)
        window = response_write_window_secs(
            self._process.stdin, transport_framing._RESPONSE_WRITE_BOUND_SECS
        )
        logger.warning(
            "ACP stdin stalled: %s while sending request method=%s req=%s; "
            "treating the backend as dead (%s)",
            _stall_window_phrase(self._process.stdin, window),
            _loggable_request_id(method),
            safe_id,
            (
                "frame never written"
                if result is RequestWriteResult.LOCK_STALL
                else "frame already buffered"
            ),
        )
        self._stdin_stalled = True
        raise AcpProcessDied(
            f"ACP stdin stalled: no write progress for {window:g}s while "
            f"sending request req={safe_id}",
            ambiguous_delivery=result is RequestWriteResult.DRAIN_STALL,
        )

    def _stdin_write_lock(self) -> asyncio.Lock:
        """The one lock every stdin write on this client takes (see
        ``await_under_no_progress_bound`` for why the bound needs it). Created on
        first use so a client built without ``__init__`` (test doubles) has one."""
        lock = getattr(self, "_stdin_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._stdin_lock = lock
        return lock

    async def _write_response_bounded(
        self,
        data: bytes,
        request_id: str | int,
        *,
        before_write: Callable[[], None] | None = None,
    ) -> None:
        """Write a response/error frame under the write lock and a no-progress bound.

        Left alone, a deny path answering a permission request would sit on a
        flow-control-paused writer until the turn deadline. A writer whose
        buffer stops shrinking for ``_RESPONSE_WRITE_BOUND_SECS`` -- while
        waiting for the lock or while draining -- is the same undeliverable-
        response condition a closed pipe reports as an error, so it is mapped to
        the same exception: ``AcpProcessDied`` sends the caller down the existing
        session-reset + bounded-requeue recovery instead of hanging. The request
        id appears only through ``_loggable_request_id``. ``before_write`` runs
        under the lock right before the frame is written and may raise to abort
        it with nothing written.
        """
        assert self._process is not None and self._process.stdin is not None
        if await write_response_frame_bounded(
            self._process.stdin,
            self._stdin_write_lock(),
            data,
            bound_secs=transport_framing._RESPONSE_WRITE_BOUND_SECS,
            before_write=before_write,
        ):
            return
        safe_id = _loggable_request_id(request_id)
        window = response_write_window_secs(
            self._process.stdin, transport_framing._RESPONSE_WRITE_BOUND_SECS
        )
        logger.warning(
            "ACP stdin stalled: %s while "
            "delivering response to req=%s; treating the backend as dead",
            _stall_window_phrase(self._process.stdin, window),
            safe_id,
        )
        raise AcpProcessDied(
            f"ACP stdin stalled: no write progress for {window:g}s while "
            f"delivering response to req={safe_id}"
        )

    async def _send_response(
        self,
        request_id: str | int,
        result: dict,
        *,
        before_write: Callable[[], None] | None = None,
    ) -> None:
        if not self._process or not self._process.stdin:
            raise AcpError("ACP process not running")

        msg = {"jsonrpc": "2.0", "id": request_id, "result": result}
        data = json.dumps(msg) + "\n"
        try:
            if before_write is None:
                await self._write_response_bounded(data.encode(), request_id)
            else:
                await self._write_response_bounded(
                    data.encode(), request_id, before_write=before_write
                )
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AcpProcessDied(f"ACP process pipe broken: {exc}") from exc
        self._last_activity = time.monotonic()

    async def _send_error(self, request_id: str | int, code: int, message: str) -> None:
        """Send a JSON-RPC 2.0 error response for a server→client request.

        Used to answer an unrecognized inbound request (e.g. ``fs/read_text_file``,
        ``terminal/create``) with ``-32601 Method not found`` so the agent fails
        fast instead of blocking forever waiting for a response we'd never send.
        """
        if not self._process or not self._process.stdin:
            raise AcpError("ACP process not running")

        msg = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        data = json.dumps(msg) + "\n"
        try:
            await self._write_response_bounded(data.encode(), request_id)
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AcpProcessDied(f"ACP process pipe broken: {exc}") from exc
        self._last_activity = time.monotonic()

    async def _read_message(self, timeout: float = _READ_TIMEOUT) -> JsonRpcMessage | None:
        if self._cancelled:
            if time.monotonic() - self._cancel_ts > self._cancel_grace_secs:
                raise AcpError("Cancel grace window exceeded; agent unresponsive")

        if self._buffer:
            return self._buffer.popleft()

        if not self._process or not self._process.stdout:
            raise AcpError("ACP process not running")

        try:
            line = await asyncio.wait_for(self._process.stdout.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        except (ValueError, asyncio.LimitOverrunError) as exc:
            # A single JSON-RPC line exceeded the stdout StreamReader buffer
            # (_STDOUT_BUFFER_LIMIT). This does NOT corrupt the stream: before
            # raising ValueError, readline() deletes the oversize line through
            # its terminating newline when one is already buffered, else clears
            # the buffer, then resumes the transport (CPython
            # asyncio.streams.StreamReader.readline — its docstring states this).
            # So drop the frame and let the caller read the next one, exactly
            # like the blank-line and non-JSON paths below; raising
            # AcpProcessDied here would end a healthy live turn over one
            # unreadably large frame.
            #
            # NOTE the deliberate asymmetry with AcpRuntime._reader_loop, which
            # additionally enforces a drain budget: that reader is a standalone
            # task with no deadline, so an endlessly unterminated stream needs an
            # explicit terminal state there. HERE every call is bounded by the
            # caller's `timeout` and the callers run their own deadlines, so the
            # worst case is one turn ending on its deadline instead of a frame —
            # no unbounded state, and still strictly better than killing the turn
            # on the first oversize frame. Computing a byte budget would require
            # readuntil (readline reports neither the branch taken nor the bytes
            # dropped), i.e. hand-rolling readline's buffer repair on the path
            # that is NOT the reported failure.
            logger.warning("Dropped an oversize ACP stdout frame: %s", exc)
            return None
        if not line:
            # EOF — process likely died or closing. Check and avoid busy-loop.
            if self._process and self._process.returncode is not None:
                # Unshielded: the child has exited, so a drain still running past
                # the budget is held open only by a descendant that inherited the
                # pipe, and every later settle on this failure would wait on it
                # again.
                await settle_drain(self._stderr_task, 0.5, shield=False)
                stderr_tail = (
                    "; ".join(self._stderr_lines) if self.memory_mode == "persistent" else ""
                )
                if stderr_tail:
                    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

                    stderr_tail, _ = redact_exfiltration_urls(stderr_tail)
                    stderr_tail, _ = redact_credentials(stderr_tail)
                detail = f" — {stderr_tail}" if stderr_tail else ""
                code = self._process.returncode
                origin = _exit_origin(code, self._kill_sent)
                raise AcpError(f"ACP process exited (code={code}{origin}){detail}")
            await asyncio.sleep(0.1)
            return None

        text = line.decode(errors="replace").strip()
        if not text:
            return None

        self._last_activity = time.monotonic()

        # Only a JSON object is a frame. Every other line costs that line, not
        # the caller's handshake or turn: text that is not JSON, a bare scalar
        # or array (`123`, `[...]`, `null`), nesting past the decoder's ceiling
        # (RecursionError) and an integer past the int-string digit limit.
        parsed = parse_json_object_line(text)
        if parsed is None:
            if self.memory_mode == "persistent":
                logger.debug("Skipping a line from ACP that is not a JSON object: %.100s", text)
            return None
        data: dict[str, Any] = parsed

        # Opt-in raw-frame recording for the replay corpus. A no-op unless
        # KIROCREW_ACP_RECORD_FRAMES names a directory, and the write is
        # offloaded off this loop when it is set. It never raises -- see
        # kiro_crew.acp._frame_record. Placed after the buffer early-return
        # above so a frame is recorded once, when it comes off the wire, not
        # again when a turn loop replays it out of _buffer.
        if self.memory_mode == "persistent":
            await record_frame(self.backend, data, len(line))

        projection = getattr(self, "_native_skill_projection", None)
        if projection is not None:
            data = projection.frame(data)
        return JsonRpcMessage(
            id=data.get("id"),
            method=data.get("method"),
            result=data.get("result"),
            error=data.get("error"),
            params=data.get("params"),
        )

    async def _wait_for_response(
        self,
        req_id: int,
        timeout: float = 50.0,
        *,
        method: str = "",
        expected_mcp: object = None,
    ) -> dict:
        """Block until a JSON-RPC response matching *req_id* arrives.

        Explicitly classifies JSON-RPC 2.0 messages with the same method-aware
        discipline as ``JsonRpcMessage.is_response_for`` (a *response* has an
        id + no method; a *request* has both id AND method):

        - Notifications (method + no id): buffered in ``_mcp_notifications`` for
          ``_drain_notifications`` to process.
        - Matching response (id == req_id + no method): returned.
        - Server→client request (method + id) and foreign-id responses
          (id != req_id + no method): collected into a LOCAL ``deferred`` list
          and re-injected into ``self._buffer`` IN ORDER once the matching
          response arrives or on timeout.

        Server requests and foreign responses must NOT be re-appended to
        ``self._buffer`` mid-loop and ``continue``-d: ``_read_message`` pops
        ``self._buffer`` first, so it would immediately re-read the same frame,
        re-defer it, and spin until the deadline (the original bug). Holding
        them in a local list until exit guarantees forward progress while
        preserving the frame so a later ``_prompt_loop`` / ``_process_message``
        can answer the deferred ``session/request_permission`` request.

        The deadline is **activity-based**: any received message (notification,
        deferred frame, or the matching response) resets it to ``now + timeout``,
        bounded by an absolute ``_WAIT_RESPONSE_MAX_TIMEOUT`` safety cap. This
        keeps a long ``session/load`` replay (which streams the entire prior
        transcript as ``session/update`` notifications before resolving) alive
        instead of being killed by a fixed wall-clock and silently falling back
        to ``session/new``.
        """
        from kiro_crew import shutdown_event

        start = time.monotonic()
        deadline = start + timeout
        hard_deadline = start + max(timeout, _WAIT_RESPONSE_MAX_TIMEOUT)
        # Frames that are not the awaited response but must survive this call.
        deferred: list[JsonRpcMessage] = []

        def _reinject() -> None:
            # Re-inject in order at the FRONT of the buffer so a later
            # _prompt_loop / _process_message sees them before newer frames.
            for d in reversed(deferred):
                self._buffer.appendleft(d)
            deferred.clear()

        while time.monotonic() < deadline and time.monotonic() < hard_deadline:
            if shutdown_event.is_set():
                _reinject()
                raise AcpError("Shutdown in progress")
            remaining = min(deadline, hard_deadline) - time.monotonic()
            if remaining <= 0:
                break
            msg = await self._read_message(timeout=min(remaining, _READ_TIMEOUT))
            if msg is None:
                continue
            # Activity-based deadline: extend on any received frame, capped by
            # the hard safety deadline. Safe for init/handshake callers — only
            # extends while the agent is actively sending us data.
            deadline = min(time.monotonic() + timeout, hard_deadline)
            if msg.is_response_for(req_id):
                _reinject()
                if msg.error:
                    if _is_model_substitution_advisory(msg.error):
                        # Admin-tier / headless-tier policy substituted the
                        # requested model. The session is already live on the
                        # substitute -- keep going; log loudly so operators see it.
                        detail = _extract_advisory_detail(msg.error)
                        self._last_substitution_model = _substitute_model_from_advisory(msg.error)
                        # Redact before logging. The advisory payload originates
                        # from the ACP backend and flows to the dashboard activity
                        # feed and Slack via the gateway log. Match the existing
                        # _format_acp_error pattern (acp/transport_errors.py).
                        _payload_log = detail if detail else str(msg.error)
                        _payload_log, _ = redact_exfiltration_urls(_payload_log)
                        _payload_log, _ = redact_credentials(_payload_log)
                        logger.warning(
                            "ACP backend substituted model (policy): %s",
                            _payload_log,
                        )
                        return msg.result or {}
                    # Dual-redact msg.error before interpolating into the AcpError.
                    # msg.error is the wire-derived JSON-RPC error frame from the ACP
                    # backend; AcpError propagates to the dashboard activity feed and
                    # Slack via the same path as the logger sinks. Same redaction
                    # discipline as the substitution-advisory log site above.
                    _err_log, _ = redact_exfiltration_urls(str(msg.error))
                    _err_log, _ = redact_credentials(_err_log)
                    raise AcpError(
                        f"JSON-RPC error: {_err_log}", code=_jsonrpc_error_code(msg.error)
                    )
                return msg.result or {}
            # Notification (has method, no id) — buffer for drain.
            if msg.method and msg.id is None:
                self._mcp_notifications.append(msg)
                logger.debug("Buffered notification: %s (req=%d)", msg.method, req_id)
                continue
            # Server→client request (method AND id) or a foreign-id response
            # (id != req_id, no method). Defer locally — do NOT re-append to
            # self._buffer here, that would spin (see docstring). Re-injected
            # in order on return/raise so the permission request survives.
            if msg.method:
                logger.debug(
                    "Deferring inbound server request: method=%s id=%s (waiting for %d)",
                    _loggable_request_id(msg.method),
                    _loggable_request_id(msg.id),
                    req_id,
                )
            else:
                logger.debug(
                    "Deferring non-matching response: id=%s (waiting for %d)",
                    _loggable_request_id(msg.id),
                    req_id,
                )
            deferred.append(msg)

        _reinject()
        label = method or f"request {req_id}"
        message = f"ACP {label} timed out after {timeout:.0f}s"
        # Both restore verbs, not just one: a harness in
        # ``ACP_BACKENDS_RESUME_WITHOUT_LOAD`` reaches this with
        # ``method=session/resume`` and the same ``expected_mcp`` detail, and an
        # omission here silently drops that detail from the only message an operator
        # sees when a restore stalls.
        if method in {METHOD_SESSION_NEW, METHOD_SESSION_LOAD, METHOD_SESSION_RESUME}:
            progress = self._mcp_timeout_progress(expected_mcp)
            if progress:
                message += f" ({progress})"
            err = AcpTimeoutError(message=message)
            # A start that never answered, tagged for the self-driving callers
            # that count consecutive start failures (see
            # ``AcpError.session_start_failed``). Set only in this branch: the
            # other awaited requests are not session starts.
            err.session_start_failed = True
            raise err
        raise AcpTimeoutError(message=message)

    def _mcp_timeout_progress(self, expected: object) -> str:
        """Summarize the MCP notifications buffered during a stalled session start."""

        def clean(value: object, cap: int = 64) -> str:
            text, _ = redact_exfiltration_urls(str(value or ""))
            text, _ = redact_credentials(text)
            return "".join(ch for ch in " ".join(text.split()) if ch.isprintable())[:cap]

        roster = [
            clean(item.get("name"))
            for item in (expected if isinstance(expected, list) else [])
            if isinstance(item, dict) and item.get("name")
        ]
        ready: set[str] = set()
        failed: dict[str, str] = {}
        auth: set[str] = set()
        # Ready / failed count only THIS attempt's frames: a timed-out
        # ``session/load`` leaves its ``server_initialized`` frames in the
        # buffer (``_drain_notifications`` clears it only at the end of
        # ``_initialize_session``), and the ``session/new`` fallback would
        # otherwise report them as its own progress -- and, with a complete
        # roster, assert the stall is not in those servers on stale evidence.
        # ``_begin_session_report`` moves the floor at the start of every
        # attempt. An OAuth request is NOT floored, the same rule
        # ``_drain_notifications`` applies to its capture: an authorization the
        # failed attempt asked for is still one the user has to answer.
        floor = self._mcp_report_frame_floor
        for idx, msg in enumerate(self._mcp_notifications):
            params = msg.params if isinstance(msg.params, dict) else {}
            name = clean(params.get("serverName") or params.get("name"))
            if not name:
                continue
            if msg.is_method(METHOD_MCP_OAUTH_REQUEST):
                auth.add(name)
            elif idx < floor:
                continue
            elif msg.is_method(METHOD_MCP_SERVER_INITIALIZED):
                ready.add(name)
            elif msg.is_method(METHOD_MCP_SERVER_INIT_FAILURE):
                failed[name] = clean(params.get("error"), 120)

        def names(values: list[str]) -> str:
            head = values[:8]
            suffix = f" (+{len(values) - 8} more)" if len(values) > 8 else ""
            return ", ".join(head) + suffix

        reported = ready | set(failed)
        # "session-injected", and a note once the roster is complete: the same
        # wording as AcpRuntime._mcp_init_progress, for the same reason -- a bare
        # ``N/N MCP server(s) reported`` read as an MCP verdict it never was.
        parts: list[str] = []
        if roster:
            parts.append(
                f"{len(reported & set(roster))}/{len(roster)} session-injected "
                "MCP server(s) reported"
            )
            missing = [name for name in roster if name not in reported]
            if missing:
                parts.append(f"no report from {names(missing)}")
            elif not set(failed) & set(roster):
                # Same rule as the runtime: the verdict is withheld when a roster
                # member reported an init failure, which the ``failed:`` bucket
                # names.
                parts.append(MCP_ROSTER_COMPLETE_NOTE)
        else:
            # No roster to attribute against (an empty or absent ``mcpServers``):
            # the reports belong to the agent spec's own servers, so the count is
            # not labelled "session-injected" -- but it is still reported, the
            # same way as the runtime, instead of the timeout reading as a bare
            # budget.
            parts.append(f"{len(reported)} MCP server report(s), roster unknown")
        if failed:
            parts.append(
                "failed: "
                + names([f"{name} ({error})" if error else name for name, error in failed.items()])
            )
        if auth:
            parts.append(f"awaiting authorization: {names(sorted(auth))}")
        return "; ".join(parts)

    async def _drain_notifications(
        self,
        duration: float = _DRAIN_DURATION,
        idle_exit: float = _DRAIN_IDLE_EXIT,
    ) -> None:
        """Drain init notifications (buffered + live) and log MCP servers.

        Captures `_kiro.dev/mcp/oauth_request` into `_pending_oauth_requests` so
        callers can surface an Authorize prompt after `ensure_ready()` returns.

        Exits early once no notification has arrived for ``idle_exit`` seconds
        (MCP servers have gone quiet), bounded by the ``duration`` hard cap. This
        avoids always waiting the full cap on the common fast path while still
        giving genuinely slow servers up to ``duration`` to report in.
        """
        deadline = time.monotonic() + duration
        last_activity = time.monotonic()
        drained = 0
        mcp_servers: list[str] = []

        def _capture_oauth(msg: JsonRpcMessage) -> None:
            if not msg.is_method(METHOD_MCP_OAUTH_REQUEST):
                return
            params = msg.params if isinstance(msg.params, dict) else {}
            server_name = str(params.get("serverName") or params.get("name") or "")
            oauth_url = str(params.get("oauthUrl") or params.get("url") or "")
            # Drop unsafe-scheme URLs *before* recording dedupe so a later safe
            # retry for the same server still gets through.
            if not _is_safe_oauth_url(oauth_url):
                if oauth_url:
                    logger.warning(
                        "ACP: refusing unsafe MCP OAuth URL for %s", server_name or "(unknown)"
                    )
                return
            # Without a server_name we can't reliably correlate this banner with
            # the matching server_initialized/server_init_failure notification —
            # the discard path keys on server_name only.  Drop rather than risk
            # a permanently-stuck dedupe entry.
            if not server_name:
                logger.warning("ACP: dropping MCP OAuth request with empty serverName")
                return
            if server_name in self._oauth_emitted_servers:
                logger.debug("ACP: dropping duplicate MCP OAuth request for %s", server_name)
                return
            self._oauth_emitted_servers.add(server_name)
            self._pending_oauth_requests.append({"serverName": server_name, "oauthUrl": oauth_url})
            logger.info("ACP: MCP OAuth request for %s", server_name)

        def _capture_config_update(msg: JsonRpcMessage) -> None:
            if not msg.is_method(METHOD_SESSION_UPDATE):
                return
            params = msg.params or {}
            update = params.get("update", {})
            if isinstance(update, dict) and update.get("sessionUpdate") == UPDATE_CONFIG_OPTION:
                self._handle_config_option_update(msg)

        def _capture_agent_switched(msg: JsonRpcMessage) -> None:
            if msg.is_method(METHOD_AGENT_SWITCHED):
                self._note_agent_switched(msg.params if isinstance(msg.params, dict) else None)

        # Process notifications buffered during _wait_for_response
        for idx, msg in enumerate(self._mcp_notifications):
            drained += 1
            _capture_oauth(msg)
            _capture_config_update(msg)
            _capture_agent_switched(msg)
            # Frames below the floor were buffered by an EARLIER session attempt
            # (a session/load that failed before the fallback session/new). The
            # OAuth and config captures above still want them — the user must
            # answer that authorization either way — but the report must not
            # credit this session with a server that reported to another attempt.
            if idx >= self._mcp_report_frame_floor:
                # Owned by construction: this transport runs one session per
                # process, so there is no co-tenant whose frame could arrive
                # here. Stated rather than defaulted so a transport that gains
                # tenants has to answer the question instead of inheriting a
                # yes.
                self._mcp_report.record_frame(msg, owned=True)
            name = ""
            if isinstance(msg.params, dict):
                name = msg.params.get("name") or msg.params.get("serverName") or ""
            if name or "mcp" in (msg.method or ""):
                mcp_servers.append(name or msg.method or "unknown")
        self._mcp_notifications.clear()
        # The floor indexes INTO that buffer, so it has to fall with it —
        # otherwise the next attempt's frames land below a stale floor and are
        # dropped from the report instead of the previous attempt's.
        self._mcp_report_frame_floor = 0

        while True:
            # Single time snapshot per iteration so the deadline and idle checks
            # can't diverge on a loaded host (CR feedback).
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                break
            # Early-exit once servers have been quiet for the idle window. Poll in
            # idle-sized slices (capped by remaining) so we notice quiet promptly.
            idle_remaining = idle_exit - (now - last_activity)
            if idle_remaining <= 0:
                break
            try:
                read_msg = await self._read_message(timeout=min(remaining, idle_remaining, 2.0))
                if not read_msg:
                    continue
                # Any received message counts as activity (servers still talking),
                # resetting the idle window even if it carries no method.
                last_activity = time.monotonic()
                if not read_msg.method:
                    continue
                drained += 1
                _capture_oauth(read_msg)
                _capture_config_update(read_msg)
                _capture_agent_switched(read_msg)
                self._mcp_report.record_frame(read_msg, owned=True)
                if "mcp" in (read_msg.method or ""):
                    name = ""
                    if isinstance(read_msg.params, dict):
                        name = (
                            read_msg.params.get("name") or read_msg.params.get("serverName") or ""
                        )
                    mcp_servers.append(name or read_msg.method)
            except AcpError:
                break
        if mcp_servers:
            logger.info("ACP: MCP servers loaded: %s", ", ".join(mcp_servers))

    def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
        """Drain and return MCP OAuth requests captured during session init.

        Each entry has keys ``serverName`` and ``oauthUrl``. Callers (typically
        the dashboard chat runner) surface these to the UI as an Authorize
        prompt — kiro-cli's local callback handles the rest of the OAuth flow.
        """
        out = list(self._pending_oauth_requests)
        self._pending_oauth_requests.clear()
        return out

    def _begin_session_report(self, servers: Any) -> None:
        """Start the MCP report for a new session attempt.

        A failed ``session/load`` still buffers the notifications it received
        before it raised, and the client then falls back to ``session/new``.
        Replaying those frames into the replacement session's report would
        attribute a server to a session that never came up — the same
        cross-attempt leak ``begin_session`` closes for the derived buckets, one
        layer earlier.

        The buffer itself is NOT cleared: it is shared with the OAuth and
        config-option captures, and an authorization request from the failed
        attempt is still one the user has to answer. Only the report's view is
        floored.
        """
        self._mcp_report_frame_floor = len(self._mcp_notifications)
        self._mcp_report.begin_session(servers)
        # AFTER the clear: a server the array build left out is this attempt's own
        # failure, and recording it first would erase it.
        pruned, self._mcp_pruned = getattr(self, "_mcp_pruned", ()), ()
        omitted, self._mcp_pruned_omitted = getattr(self, "_mcp_pruned_omitted", 0), 0
        # Only a server the report admitted counts as configured for the ref guard:
        # a name the failed bucket refused must get no row anywhere.
        admitted: list[str] = []
        for server in pruned:
            if self._mcp_report.record_pruned(server.name, server.reason):
                if server.name not in admitted:
                    admitted.append(server.name)
        self._mcp_report.record_pruned_omitted(omitted)
        self._mcp_pruned_names = tuple(admitted)

    async def _prune_unstartable_mcp(self, servers: list[Any]) -> list[Any]:
        """*servers* without the stdio elements whose command certainly cannot start.

        Only for ``ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP``, where such an element is
        otherwise lost without a word (goose) or takes the whole session with it
        (deepseek). Each call site checks that set BEFORE awaiting this, so the kiro
        path gains no awaited step (harness-parity H13). The stats run off the loop.
        Each pruned server is warned about here and recorded on the MCP report as
        failed by :meth:`_begin_session_report`, which every array build reaches
        before its request is sent.
        """
        kept, pruned = await asyncio.to_thread(prune_unstartable, servers)
        # Bounded where it is kept, by the report's own caps: the count by the
        # failed bucket's size, and each name and reason by its string cap. The
        # reason quotes the configured command, which can hold a whole CLI line
        # with a key in it, so it is cleaned before it is kept or logged.
        retained = tuple(
            PrunedServer(
                sanitize_sink_text(str(server.name or ""), MCP_REPORT_NAME_CAP),
                sanitize_sink_text(str(server.reason or ""), MCP_REPORT_ERROR_CAP),
            )
            for server in pruned[:MCP_REPORT_BUCKET_CAP]
        )
        omitted = len(pruned) - len(retained)
        for server in retained:
            logger.warning(
                "%s session MCP: leaving server %r out of this session -- %s. The "
                "session starts without it; fix its command to get its tools back",
                self.backend,
                server.name,
                server.reason,
            )
        if omitted:
            logger.warning(
                "%s session MCP: leaving %d more unstartable servers out of this "
                "session, not listed",
                self.backend,
                omitted,
            )
        self._mcp_pruned = retained
        self._mcp_pruned_omitted = omitted
        return kept

    def mcp_session_report(self) -> McpSessionReport:
        """This session's MCP registration report (see ``mcp_session_report``).

        Unlike :meth:`pop_pending_oauth_requests` this does NOT drain: the report
        is the session's standing answer to "which servers actually started
        here", read repeatedly and still true. Callers must render an unreported
        server as *not reported*, never as *not mounted* — the drain is time
        bounded and a late frame still arrives mid-turn.
        """
        return self._mcp_report

    # ── Prompt Loop Helpers ──

    def _process_message(self, msg: JsonRpcMessage, req_id: int) -> str:
        """Classify a message into an action string.

        Actions: "complete", "error", "permission", "update", "metadata",
        "server_request_unknown", "skip".
        """
        if msg.is_response_for(req_id):
            return "error" if msg.error else "complete"

        if msg.is_method(METHOD_REQUEST_PERMISSION):
            return "permission"

        if msg.is_method(METHOD_SESSION_UPDATE):
            self._note_active_run(msg)
            return "update"

        if msg.method is None and msg.id is not None and self._settle_steer_answer(msg):
            return "skip"

        if msg.is_method(METHOD_METADATA):
            return "metadata"

        if msg.is_method(METHOD_COMPACTION_STATUS):
            return "compaction"

        if msg.is_method(METHOD_CLEAR_STATUS):
            return "clear"

        if msg.is_method(METHOD_AGENT_SWITCHED):
            return "agent_switched"

        if msg.is_method(METHOD_MCP_OAUTH_REQUEST):
            return "mcp_oauth_request"

        if msg.is_method(METHOD_MCP_SERVER_INITIALIZED):
            return "mcp_server_initialized"

        if msg.is_method(METHOD_MCP_SERVER_INIT_FAILURE):
            return "mcp_server_init_failure"

        if msg.is_method(METHOD_SUBAGENT_LIST_UPDATE):
            return "subagent_list"

        if msg.is_method(METHOD_KIRO_SESSION_UPDATE):
            return "subagent_activity"

        # Unknown server→client REQUEST (has both method AND id). Per JSON-RPC
        # the agent blocks until it gets a response, so it must be answered
        # (with -32601 by the dispatch sites) rather than silently skipped —
        # otherwise the agent hangs forever. Known requests (request_permission)
        # are handled above; only genuinely unrecognized requests reach here.
        if msg.method is not None and msg.id is not None:
            return "server_request_unknown"

        return "skip"

    async def _defer_opencode_shell_permission(self, msg: JsonRpcMessage, remaining: float) -> bool:
        """Queue a matching command update before its OpenCode shell permission."""
        params = msg.params if isinstance(msg.params, dict) else {}
        tool_call = params.get("toolCall")
        tool_call = tool_call if isinstance(tool_call, dict) else {}
        tool_call_id = tool_call.get("toolCallId")
        if (
            not isinstance(tool_call_id, str)
            or self._tool_call_is_shell.get(tool_call_id) is not True
        ):
            return False
        cached = self._tool_call_params.get(tool_call_id, {})
        if isinstance(cached, dict) and cached.get("command"):
            return False

        # OpenCode can ask permission before its command-bearing update. Read
        # only the next frame to preserve unrelated event order. The request's
        # own rawInput must never fill the trusted security cache.
        following = await self._read_message(
            timeout=min(_OPENCODE_PERMISSION_REFINEMENT_TIMEOUT, remaining)
        )
        if following is None:
            return False

        follow_params = following.params if isinstance(following.params, dict) else {}
        update = follow_params.get("update")
        update = update if isinstance(update, dict) else {}
        raw_input = update.get("rawInput")
        buffer_capacity = self._buffer.maxlen
        if not (
            following.is_method(METHOD_SESSION_UPDATE)
            and isinstance(params.get("sessionId"), str)
            and follow_params.get("sessionId") == params.get("sessionId")
            and update.get("sessionUpdate") == "tool_call_update"
            and update.get("toolCallId") == tool_call_id
            and update.get("status") == "in_progress"
            and isinstance(raw_input, dict)
            and isinstance(raw_input.get("command"), str)
            and raw_input["command"]
            and not update.get("content")
            and not update.get("rawOutput")
            and (buffer_capacity is None or len(self._buffer) <= buffer_capacity - 2)
        ):
            self._buffer.appendleft(following)
            return False

        # The normal dispatcher caches the command before building permission.
        self._buffer.appendleft(msg)
        self._buffer.appendleft(following)
        return True

    async def _prompt_loop(
        self,
        req_id: int,
        timeout: float,
    ) -> AsyncGenerator[tuple[str, JsonRpcMessage], None]:
        """Core prompt read loop. Yields (action, msg) pairs.

        Always releases ``_turn_done`` on exit — including abnormal exits
        (process death, cancel-grace exceeded, or a caller that raises on an
        ``error`` action and closes this generator). Without the ``finally``,
        those paths bypass the callers' trailing ``_turn_done.set()`` and a
        ``wait_turn_done`` waiter (the cooperative-stop ack) blocks for its
        full budget before escalating to a session-losing hard kill.
        """
        # L1 turn-lock: serialize the whole read turn on this client's single
        # stdout StreamReader so two _bg streaming turns can't both park on
        # readline() and trip "readuntil() called while another coroutine is
        # already waiting". Acquired here (every streaming consumer funnels
        # through _prompt_loop), released in the finally — see the __init__
        # comment for the finalization + coverage caveats.
        await self._turn_lock.acquire()
        try:
            # Retire the liveness state HERE, under the lock, because this is the
            # one point every prompt path funnels through: send_message (via
            # _read_prompt_response), send_message_stream, and _dispatch_events.
            # Retiring in a caller's prologue instead would (a) miss the direct
            # prompt APIs, leaving their next turn gated by the previous turn's
            # wedged walk, and (b) run BEFORE this acquire, letting a queued turn
            # clear the active turn's tracked consult and so allow a second walk
            # while the first is still pending.
            self._retire_liveness_state()
            # A goose run id and the steers awaiting their echo belong to one
            # turn: the new prompt's run id is announced by a frame this loop
            # has not read yet, and a steer the last turn never echoed was
            # discarded with that run and already queued by its caller.
            self._active_run_id = None
            self._steers_awaiting_echo = {}
            self._compaction_failed_at = None
            self._compaction_failed_turn = False
            self._claude_compaction_pending = False
            self._codex_compaction_pending = False
            deadline = time.monotonic() + timeout
            consecutive_empty = 0
            last_data_ts = time.monotonic()
            # Consumer park accounting (mirrors AcpSessionHandle._dispatch_events):
            # the interval between this generator's yield and its resume is
            # CONSUMER time — a human approval prompt parks the whole generator
            # chain at that yield — so the post-compaction-failure idle clock
            # below must subtract it or a long approval wait reads as backend
            # silence and the budget reaps a live turn. `parked_at_data`
            # snapshots the accumulator when `last_data_ts` is taken, so only
            # park time accrued SINCE the last frame is excluded.
            parked_total = 0.0
            parked_at_data = 0.0
            _last_yield = time.monotonic()

            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break

                # Post-compaction-failure budget: a `failed` status arrived and the
                # backend has since gone silent past the budget, so no prompt
                # response or end_turn is coming. Stop reading so _dispatch_events
                # ends the turn explicitly and the runner releases the slot,
                # instead of draining to `deadline` (hours).
                #
                # SUSPENDED while a tool is in flight: kiro-cli can recover from a
                # failed compaction and dispatch a tool, and a legitimately silent
                # long tool (a build, a spawned subagent) would then be reaped at
                # 60s — killing valid work that the tool-stall watchdog already
                # governs on its own, much longer, liveness-gated budget. A tool
                # dispatch is positive evidence the turn is alive, so this budget
                # hands off to _TOOL_STALL_TIMEOUT and re-arms when the tool
                # resolves (_tool_dispatched cleared in _dispatch_events).
                #
                # Clock asymmetry with AcpSessionHandle's twin is INTENTIONAL: this
                # path owns a dedicated process, so every frame is its own and the
                # park accounting below is what it needs; the shared-runtime twin
                # instead needs session-attributable frames (a co-tenant's fanout
                # must not defer the reap). Keep both when touching either.
                if self._compaction_failed_at is not None and not self._tool_dispatched:
                    _compact_idle = max(
                        0.0,
                        (time.monotonic() - max(self._compaction_failed_at, last_data_ts))
                        - max(0.0, parked_total - parked_at_data),
                    )
                    if _compact_idle > _COMPACTION_FAILED_TURN_BUDGET:
                        logger.warning(
                            "Compaction failed for req %d and no prompt response "
                            "arrived for %.0fs — ending the turn.",
                            req_id,
                            _compact_idle,
                        )
                        self._compaction_failed_turn = True
                        return

                # Cooperative yield, placed where the previous frame is fully
                # handled and the next one is not yet read: no frame is held in a
                # local here, so a cancellation landing on this yield drops
                # nothing. `_read_message` is NOT a suspension point when input is
                # already buffered -- it returns `self._buffer.popleft()` outright,
                # and `StreamReader.readline` returns without awaiting when the
                # line is already in its buffer -- so without this a burst drains
                # inside one task step. Twin of
                # AcpSessionHandle._dispatch_events; both read the same budget.
                _now = time.monotonic()
                if _now - _last_yield >= DRAIN_YIELD_AFTER_S:
                    await asyncio.sleep(0)
                    _last_yield = time.monotonic()

                msg = await self._read_message(timeout=min(remaining, _READ_TIMEOUT))
                if msg is None:
                    consecutive_empty += 1
                    if consecutive_empty >= _MAX_CONSECUTIVE_EMPTY and not self._is_process_alive():
                        rc = self._process.returncode if self._process else "?"
                        # A death whose retained stderr shows a throttled
                        # registration is endpoint throttling, not a crash:
                        # raise the typed transient subclass so the retry
                        # ladders recover it (a respawn through ensure_ready
                        # genuinely retries registration on this transport).
                        _throttled = await self._registration_throttle_line()
                        if _throttled is not None:
                            raise registration_rate_limited_error(
                                f"Process exited during prompt (exit code {rc})", _throttled
                            )
                        raise AcpProcessDied(f"Process exited during prompt (exit code {rc})")
                    # Staleness check: if caller set _stale_eligible (text was
                    # streamed) and kiro-cli has gone silent, exit early.
                    # Fold in _last_activity (refreshed by the stderr drain) so a
                    # turn that is still streaming thinking_tokens on stderr —
                    # between its final text chunk and its next tool call — is not
                    # mistaken for silence. Using only the stdout clock
                    # (last_data_ts) trips a false stale-turn on every multi-turn
                    # reasoning step, burning ~_STALE_TURN_TIMEOUT s per turn and
                    # reaping long subagent runs at the timeout.
                    last_seen = max(last_data_ts, self._last_activity)
                    if self._stale_eligible:
                        # Consult the liveness oracle on EVERY silent read once
                        # text has streamed — not only at the timeout. The oracle
                        # needs a prior sample to compute a movement delta (its
                        # first call after a boundary retirement always reads
                        # UNKNOWN/"sampling"),
                        # so a single consult at the 90s mark would always reap.
                        # Sampling each silent read primes the baseline and keeps
                        # the movement window recent, mirroring the kiro path's
                        # per-tick consult. The consult is /proc-based and
                        # offloaded so it cannot block the read loop, and degrades
                        # to a non-WORKING verdict on any error (fail toward
                        # reaping). In production, silent reads recur at the
                        # ~_READ_TIMEOUT cadence, giving well-spaced samples.
                        #
                        # Snapshot the idle window BEFORE awaiting the consult.
                        # The walk is OUR OWN probe and a cold one costs tens of
                        # milliseconds (executor warm-up, then the subtree walk);
                        # reading the clock AFTER it charges that latency to the
                        # BACKEND's silence budget, so on a loaded host the first
                        # silent read could cross the cutoff carrying the only
                        # verdict the oracle can give before it has a baseline,
                        # and reap a turn that was streaming a moment earlier.
                        _idle_secs = time.monotonic() - last_seen
                        verdict, evidence = await self._consult_liveness_model_wait()
                        if _idle_secs > _STALE_TURN_TIMEOUT:
                            # Past the cutoff: a backend still doing work (CPU/IO
                            # movement in its subtree — a long model generation or
                            # a spawned build) reads WORKING and we keep waiting.
                            # Only WORKING extends; any other verdict
                            # (DEAD/UNKNOWN/STUCK_INPUT) preserves the conservative
                            # end-the-turn behavior, so hang recovery is never
                            # weakened (still bounded by _DEFAULT_PROMPT_TIMEOUT).
                            if verdict == VERDICT_WORKING:
                                logger.debug(
                                    "Stale-turn deferral for req %d — idle %.0fs but "
                                    "backend WORKING (%s)",
                                    req_id,
                                    _idle_secs,
                                    evidence,
                                )
                                continue
                            if evidence == EVIDENCE_SAMPLING:
                                # The oracle stored a BASELINE and has nothing to
                                # compare it against yet, so this verdict is
                                # structurally incapable of reading WORKING: it
                                # means "ask me again", not "idle". Reaping on it
                                # ends a live turn on no evidence at all — the
                                # per-silent-read consulting above exists to make
                                # that unlikely, and deferring here makes it
                                # impossible rather than merely improbable. The
                                # next silent read has a baseline and answers for
                                # real, so recovery is delayed by one read, not
                                # weakened.
                                logger.debug(
                                    "Stale-turn deferral for req %d — idle %.0fs but the "
                                    "liveness baseline is still priming (%s)",
                                    req_id,
                                    _idle_secs,
                                    evidence,
                                )
                                continue
                            if self._tool_dispatched:
                                # An OPEN TOOL CALL is positive evidence the turn
                                # is alive, so it defers this cutoff whatever the
                                # verdict says. Only kiro-cli streams
                                # tool_call_update progress frames while a tool
                                # runs; a backend that runs the tool to completion
                                # and only then reports the result is silent for
                                # the whole tool, and with text streamed before
                                # the dispatch this cutoff would tear the turn down
                                # MID-TOOL — reporting truncated work as complete.
                                # Deliberately NOT a `continue`:
                                # falling through hands the silence to the
                                # tool-stall watchdog below, which governs exactly
                                # this case on its own much longer budget and
                                # RECOVERS the slot instead of completing the
                                # turn. Mirrors the compaction-failed budget's
                                # suspension above; _tool_dispatched is cleared
                                # when the tool resolves, so the cutoff re-arms
                                # for the model-wait silence it is actually for.
                                logger.debug(
                                    "Stale-turn deferral for req %d — idle %.0fs with a tool "
                                    "call still open (liveness=%s: %s); tool-stall watchdog "
                                    "governs",
                                    req_id,
                                    _idle_secs,
                                    verdict,
                                    evidence,
                                )
                            else:
                                logger.warning(
                                    "Stale turn detected for req %d — no data for %.0fs after text was streamed "
                                    "(liveness=%s: %s). Treating as complete.",
                                    req_id,
                                    _idle_secs,
                                    verdict,
                                    evidence,
                                )
                                return
                    # Tool-stall watchdog: a tool was dispatched but NOTHING has
                    # come back (no result, no progress, no permission) for the
                    # stall window.  This is the silent-hang case where
                    # _stale_eligible is False (cleared on tool_call) so the
                    # check above never fires.
                    #
                    # Recovery (not just detection): the original bare ``return``
                    # abandoned the turn but left the kiro-cli child ALIVE
                    # mid-prompt, so the slot wedged and every later prompt hit
                    # "Prompt already in progress" until the whole backend was
                    # killed by hand.  Instead, kill the wedged child so the next
                    # prompt cold-starts, and raise AcpProcessDied so the existing
                    # recovery path takes over: the dashboard resets the session
                    # and re-queues the message (bounded by _acp_pipe_death_retries
                    # → "Session stuck" after 3), and cron/other callers surface a
                    # clean error instead of a hung turn.  _kill_process only
                    # touches the subprocess/pipes (never _turn_lock), so it is
                    # safe to call here — the finally still releases the lock.
                    #
                    # Use max(last_data_ts, _last_activity) so that MCP tools
                    # which ping /api/session-keepalive (wait, spawn_sub_agents)
                    # keep the watchdog satisfied even though no JSON-RPC frames
                    # arrive on stdout during their execution.
                    _tool_last_seen = max(last_data_ts, self._last_activity)
                    if (
                        self._tool_dispatched
                        and (time.monotonic() - _tool_last_seen) > _TOOL_STALL_TIMEOUT
                    ):
                        _stall_idle = time.monotonic() - _tool_last_seen
                        logger.warning(
                            "Tool stall detected for req %d — tool dispatched but no data for %.0fs. "
                            "Killing agent to recover the slot.",
                            req_id,
                            _stall_idle,
                        )
                        await self._kill_process(force=True)
                        raise AcpProcessDied(
                            f"tool stalled — no data for {_stall_idle:.0f}s; agent killed to recover"
                        )
                    continue

                consecutive_empty = 0
                last_data_ts = time.monotonic()
                parked_at_data = parked_total
                # NB: do NOT clear _tool_dispatched here.  The last_data_ts reset
                # above already prevents false positives for tools that stream
                # progress frames (each frame restarts the _TOOL_STALL_TIMEOUT
                # countdown).  Clearing the flag on every inbound frame would
                # disarm the watchdog after a single progress frame, so a tool
                # that emits one frame then silently stalls would hang anyway —
                # exactly the bug this watchdog targets.  The flag is cleared
                # only when a tool actually resolves or the turn completes
                # (see _dispatch_events).
                self.last_prompt_stats.event_count += 1

                # The answer to a steer sent into this turn is read here, by
                # the one consumer of stdout, and handed to the waiting steer.
                if self._settle_steering_answer(msg):
                    continue

                action = self._process_message(msg, req_id)
                if (
                    action == "permission"
                    and self.backend == ACP_BACKEND_OPENCODE
                    and await self._defer_opencode_shell_permission(msg, remaining)
                ):
                    self.last_prompt_stats.event_count -= 1
                    continue
                # Single yield chokepoint: everything downstream (dispatch,
                # chat runner, a human answering an approval card) runs while
                # this generator is suspended here, so the whole gap is
                # consumer time. `finally` so an abandoned generator's
                # GeneratorExit still closes the park.
                _parked_since = time.monotonic()
                try:
                    yield action, msg
                finally:
                    parked_total += max(0.0, time.monotonic() - _parked_since)
        finally:
            self._turn_lock.release()
            # Nothing reads a steer's answer once this loop is gone, so the turn
            # stops taking steers on every exit.
            if self._steering_turn_req == req_id:
                self._steering_turn_req = None
            # Release any cooperative-stop waiter regardless of how the loop
            # ends. The callers set the precise stop reason on the clean
            # "complete" path before this runs (idempotent); on abnormal exit
            # the reason stays "" → provider.cancel reports "timeout" →
            # escalates to hard kill, the correct outcome for a dead turn.
            if not self._turn_done.is_set():
                self._turn_done.set()

    async def _consult_liveness_model_wait(self) -> tuple[str, str]:
        """Liveness verdict for the stale-turn gate, offloaded off the loop.

        The oracle walks ``/proc`` for the backend subprocess subtree — a
        synchronous filesystem walk that can block on a wedged fd — so it runs
        on ``subprocess_executor()`` under a bounded timeout, the same treatment
        the runtime's other /proc probes get. Any failure or timeout degrades to
        a non-WORKING verdict so the caller falls through to ending the turn
        (fail toward reaping, never toward hanging).

        Scope, mirroring the shared-runtime oracle this converges onto:
        - Linux-only evidence. On a host without ``/proc`` the verdict is
          UNKNOWN → the turn is reaped at the cutoff exactly as before this
          gate existed, so the change is behavior-preserving off Linux and a
          strict improvement on the gateway's Linux deploy target.
        - Subtree-aggregate movement. A busy *unrelated* descendant (e.g. an
          MCP child polling) can read WORKING even if the model turn itself is
          wedged with a lost completion frame, extending that turn to the
          ``_DEFAULT_PROMPT_TIMEOUT`` backstop rather than reaping at 90s. This
          is an inherent property of the shared ``LivenessOracle`` (the kiro
          path has it too); tighter per-branch attribution belongs in
          ``liveness.py``, shared by both callers, not here.

        A timed-out await does not stop its executor thread. The one-outstanding-
        walk bound, the refused-submission-reads-UNKNOWN contract, and exception
        retrieval all live in the shared :func:`consult_offloaded` guard.
        ``_prompt_loop`` retires the tracked future at turn start under
        ``_turn_lock``, so a walk abandoned by one turn never gates the next (at
        the cost of one abandoned worker per turn, versus one per silent read
        before this guard existed).
        """
        return await consult_offloaded(
            self,
            self._liveness_oracle.check_model_wait,
            (getattr(self, "_pid", None),),
            executor_factory=subprocess_executor,
            log_label="liveness consult",
        )

    # ── Public API ──

    async def send_message(self, message: str, timeout: float | None = None) -> str:
        """Send a prompt and return the full response text."""
        timeout = await _effective_prompt_timeout_async(timeout)
        self._cancelled = False
        self._turn_done.clear()
        await self.ensure_ready()

        req_id = await self._send_prompt(message)
        return await self._read_prompt_response(req_id, timeout)

    async def send_message_stream(
        self, message: str, timeout: float | None = None
    ) -> AsyncIterator[str]:
        """Send a prompt and yield text chunks as they arrive."""
        timeout = await _effective_prompt_timeout_async(timeout)
        # NOTE: PreToolUse/PostToolUse hooks are intentionally NOT fired on this
        # streaming path today. No audit_source (worker-pool) consumer uses
        # send_message_stream — hook instrumentation lives on the _read_prompt_response
        # path (_maybe_fire_pre_tool_hooks / _maybe_fire_post_tool_hooks). If a future
        # streaming subagent adopts this method, mirror that Pre/Post instrumentation here.
        self._cancelled = False
        self._turn_done.clear()
        await self.ensure_ready()

        req_id = await self._send_prompt(message)
        self.last_prompt_stats = self.last_prompt_stats.carry_over()

        # aclosing(): _prompt_loop holds _turn_lock and releases it in its
        # finally. Consumers below `return` on "complete" without exhausting the
        # loop, which leaves the async-generator SUSPENDED — and CPython
        # finalizes async-gens via a *deferred* scheduled athrow, not at the
        # return point, so the lock would stay held past the turn (next _bg
        # caller blocks = the freeze). aclosing() runs aclose() deterministically
        # on block exit, firing the finally and releasing the lock immediately.
        async with aclosing(self._prompt_loop(req_id, timeout)) as _loop:
            async for action, msg in _loop:
                if action == "complete":
                    reason = ""
                    result = msg.result or {}
                    if isinstance(result, dict):
                        reason = result.get("stopReason", "") or ""
                    self._track_prompt_usage(result)
                    # Close out an automatic claude compaction. The returned
                    # event is discarded — this API yields str — but the context
                    # counts it drops are what the meter reads next turn.
                    self._settle_claude_compaction(reason)
                    self._settle_codex_compaction(reason)
                    reason, _ = self.last_prompt_stats.terminal_refusal(reason)
                    self._last_stop_reason = reason
                    self._turn_done.set()
                    return
                if action == "error":
                    if error_is_refusal_terminal(msg.error, self.last_prompt_stats.refusal):
                        # The refusal's own -32603 terminal (see
                        # _dispatch_events). This API yields str, so the fold
                        # lands on _last_stop_reason and the turn ends cleanly
                        # rather than raising a deterministic decline.
                        reason, _ = self.last_prompt_stats.terminal_refusal("")
                        self._last_stop_reason = reason
                        self._turn_done.set()
                        return
                    _raise_acp_error(msg.error, self._advertised_model_ids(), backend=self.backend)
                if action == "permission":
                    await self._handle_permission(msg)
                elif action == "server_request_unknown":
                    await self._reject_unknown_server_request(msg)
                elif action == "update":
                    self._track_usage_update(msg)
                    self._note_background_launch(msg)
                    # Apply the codex compaction state change; this API yields
                    # str so the event has nowhere to go, but the context counts
                    # it drops are what the meter reads next turn.
                    self._codex_compaction_event(msg)
                    # The gate tripwire holds on every reader that answers
                    # permission frames, this text-only one included.
                    await self._tripwire_pi_gate(msg)
                    await self._tripwire_goose_mode(msg)
                    chunk, is_thinking = self._extract_text_chunk(msg)
                    if chunk and not is_thinking:
                        # A claude compaction notice is a control frame wearing
                        # assistant text. This API yields str, so the event has
                        # nowhere to go — but the state change still applies and
                        # the chunk is still forwarded. Recognizing a notice is a
                        # guess about bare prose, so dropping it here would let
                        # one wrong guess erase a real answer with nothing left
                        # to recover it from.
                        self._claude_compaction_event(chunk)
                        self.last_prompt_stats.text_chunks += 1
                        yield chunk
                        if _is_tool_interrupted_marker(chunk):
                            self._emit_tool_interrupted_sel("send_message_stream")
                            # send_message_stream yields only text chunks (str),
                            # not AcpEvent objects. Tool-result events are a
                            # different shape and cannot be yielded here; callers
                            # of this API do not consume them. Unlike
                            # _dispatch_events (which yields AcpEvent and must
                            # drain tool results before EVENT_COMPLETE), we just
                            # return — no further text will arrive from kiro-cli.
                            return
                    # On a session with a spec deny set, the full extractor rather
                    # than the stats-only tracker: this loop answers permission
                    # requests through _handle_permission, and the refusal there
                    # identifies a call ONLY from the provenance the extractor caches
                    # (raw params by toolCallId) -- stats alone would leave every
                    # request unidentified, and the auto-approve site then refuses
                    # them all rather than run a switched-off tool. The same extractor
                    # writes the classification cache the identity-drift refusals read,
                    # so a harness in ACP_BACKENDS_META_IDENTITY runs it too. On a
                    # session that judges nothing, the tracker's byte-identical
                    # behaviour on every other backend is kept.
                    if self._judges_permission_requests:
                        self._extract_tool_event(msg)
                    else:
                        self._track_tool_call(msg)
                elif action == "metadata":
                    self._track_metadata(msg)
                elif action == "compaction":
                    self._handle_compaction_status(msg)

        # Loop ended without "complete" — timeout or process death.
        self._last_stop_reason = ""
        self._turn_done.set()

    async def stream_events(
        self,
        message: str,
        timeout: float | None = None,
        *,
        allow_image: bool = True,
    ) -> AsyncIterator[AcpEvent]:
        """Send a prompt and yield AcpEvent objects (text, tool_call, permission, complete).

        ``allow_image=False`` sends *message* as text only: no path in it is
        read or inlined.
        """
        timeout = await _effective_prompt_timeout_async(timeout)
        self._cancelled = False
        self._turn_done.clear()
        await self.ensure_ready()
        req_id = await self._send_prompt(message, allow_image=allow_image)
        async for event in self._dispatch_events(req_id, timeout):
            yield event

    async def _dispatch_events(
        self,
        req_id: int,
        timeout: float,
        *,
        extract_agent_from_result: bool = False,
    ) -> AsyncIterator[AcpEvent]:
        """Shared event dispatch loop for prompts and commands."""
        self.last_prompt_stats = self.last_prompt_stats.carry_over()
        self._tool_call_inputs.clear()
        self._tool_call_input_redacted.clear()
        self._tool_call_is_shell.clear()
        self._tool_call_unclassified.clear()
        self._skill_reads.clear()
        self._tool_call_mcp_server.clear()
        self._tool_call_tool_name.clear()
        self._tool_call_harness_tool_name.clear()
        self._tool_call_identity_unreadable.clear()
        self._tool_call_params.clear()
        self._tool_call_diff_path.clear()
        # Reset the per-turn observed-tool-call bookkeeping (see __init__).
        self._observed_tool_calls.clear()
        # Clear stale permission options so an aborted/cancelled request from
        # a prior turn cannot leak into this one (memory + correctness).
        self._permission_options.clear()
        getattr(self, "_permission_gate_events", {}).clear()
        self._stale_eligible = False
        self._tool_dispatched = False
        if self._active_tool_calls.evicted_count:
            # The previous turn's overflow, counted and said once (the same
            # sentence the session handle says for its map).
            logger.warning(
                "tool-call open set evicted %d id(s) at its bound of %d last turn; the "
                "tool-stall clock stayed armed for that turn",
                self._active_tool_calls.evicted_count,
                MAX_ACTIVE_TOOL_CALLS,
            )
        self._active_tool_calls.clear()
        got_complete = False
        saw_agent_switch = False
        # A prompt turn takes ``_session/steering`` steers; a command turn does
        # not. Steers accepted into an earlier turn were settled or dropped there.
        self._steers_accepted = []
        self._steering_turn_req = None if extract_agent_from_result else req_id

        async for action, msg in self._prompt_loop(req_id, timeout):
            if action != "update":
                logger.debug(
                    "ACP event: method=%s id=%s action=%s",
                    _loggable_request_id(msg.method),
                    _loggable_request_id(msg.id),
                    action,
                )

            # Reset staleness only on events that indicate active work.
            # Passive updates (usage_update, tool_call_update after completion,
            # available_commands) must NOT reset it — they can arrive after the
            # final text chunk when kiro-cli has finished but hasn't sent the
            # complete response yet.
            if action != "update":
                self._stale_eligible = False

            if action == "complete":
                got_complete = True
                result = msg.result or {}
                reason = ""
                if isinstance(result, dict):
                    reason = result.get("stopReason", "") or ""
                self._track_prompt_usage(result)
                if extract_agent_from_result and isinstance(result, dict):
                    # commands/execute returns output in result fields,
                    # not via session/update chunks — yield as text.
                    text = format_command_result(result)
                    if text:
                        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text=text)
                    if not saw_agent_switch:
                        data = result.get("data", {})
                        if isinstance(data, dict) and data.get("agent"):
                            agent_info = data["agent"]
                            name = (
                                agent_info.get("name", "") if isinstance(agent_info, dict) else ""
                            )
                            if name:
                                yield AcpEvent(kind=EVENT_AGENT_SWITCHED, text=name)
                # Flush any remaining tool results before completing
                for tr_event in await self._read_new_tool_results():
                    yield tr_event
                # An automatic claude compaction never sends its own terminal —
                # close it out here, BEFORE EVENT_COMPLETE, so a consumer that
                # reads the terminal to leave its compacting state sees it
                # inside the turn rather than after the turn it belongs to.
                _compaction_settle = self._settle_claude_compaction(reason)
                if _compaction_settle is not None:
                    yield _compaction_settle
                # The codex reading of the same hole: a compaction that errored
                # sent no terminal, so the turn ending is where it is closed out.
                _codex_settle = self._settle_codex_compaction(reason)
                if _codex_settle is not None:
                    yield _codex_settle
                reason, _refusal = self.last_prompt_stats.terminal_refusal(reason)
                # Turn is over — disarm the stall watchdog.
                self._tool_dispatched = False
                self._last_stop_reason = reason
                _consumed = self._release_accepted_steers(req_id, reason, _refusal)
                self._turn_done.set()
                for _injected in _consumed:
                    yield AcpEvent(kind=EVENT_STEER_CONSUMED, text=_injected)
                yield AcpEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason=reason,
                    refusal=_refusal,
                    usage=self.last_prompt_stats.to_turn_usage(),
                )
                return
            if action == "error":
                if error_is_refusal_terminal(msg.error, self.last_prompt_stats.refusal):
                    # See AcpSessionHandle: a content-filter refusal can
                    # terminate as a bare -32603. The reason is already on the
                    # stats; surface it as the refusal terminal, never raise.
                    #
                    # Flush pending tool results first, exactly as the
                    # `complete` branch does: a tool that finished just before
                    # the filtered inference would otherwise have its result
                    # dropped, or emitted into the NEXT turn.
                    reason, _refusal = self.last_prompt_stats.terminal_refusal("")
                    for tr_event in await self._read_new_tool_results():
                        yield tr_event
                    self._tool_dispatched = False
                    self._last_stop_reason = reason
                    self._turn_done.set()
                    yield AcpEvent(
                        kind=EVENT_COMPLETE,
                        stop_reason=reason,
                        refusal=_refusal,
                        usage=self.last_prompt_stats.to_turn_usage(),
                    )
                    return
                _raise_acp_error(msg.error, self._advertised_model_ids(), backend=self.backend)
            if action == "permission":
                permission_event = self._build_permission_event(msg)
                if permission_event is None:
                    if msg.id is not None:
                        await self._send_error(msg.id, -32600, "invalid request id")
                    continue
                # Three refusals before the consumer's gate sees the request: a
                # zero-tool spec, a switched-off tool, and a harness identity that is
                # absent or names an unmounted server. The third matters HERE as much
                # as on the auto-approve site -- a frame nothing classified reaches
                # the consumer as a non-shell tool, so the command-deny tier never
                # sees the command it carries.
                if not (
                    await self._deny_zero_tools(permission_event)
                    or await self._deny_spec_disabled_tool(permission_event)
                    or await self._refuse_identity_drift(permission_event)
                ):
                    # The loop is parked here until the approval is answered, and
                    # nothing reads stdout meanwhile, so no steer is sent then.
                    self._parked_on_permission = True
                    try:
                        yield permission_event
                    finally:
                        self._parked_on_permission = False
            elif action == "server_request_unknown":
                await self._reject_unknown_server_request(msg)
            elif action == "update":
                self._track_usage_update(msg)
                self._note_background_launch(msg)
                _steer_echo = self._steer_echo_event(msg)
                if _steer_echo is not None:
                    yield _steer_echo
                # codex reports compaction as a marked tool_call pair rather than
                # as text, so it is read off the FRAME here instead of off a
                # chunk below. Yielded and then fallen through: the frame is
                # still a tool call this turn made.
                _codex_compaction = self._codex_compaction_event(msg)
                if _codex_compaction is not None:
                    yield _codex_compaction
                chunk, is_thinking = self._extract_text_chunk(msg)
                _notice_chunk = False
                if chunk and not is_thinking:
                    # The claude backend reports compaction as plain assistant
                    # text. Emit the compaction status event every consumer
                    # already handles, then fall through and yield the chunk too:
                    # the event is a side effect, not a replacement for the text.
                    # The chunk carries ``control_notice`` so a consumer can show
                    # it without counting it as the turn's answer — the ACP layer
                    # owns the classification, and nothing downstream re-parses.
                    _compaction_event = self._claude_compaction_event(chunk)
                    if _compaction_event is not None:
                        yield _compaction_event
                        _notice_chunk = True
                if chunk:
                    # Before yielding text, check for tool results from JSONL
                    for tr_event in await self._read_new_tool_results():
                        yield tr_event
                    kind = EVENT_THINKING_CHUNK if is_thinking else EVENT_TEXT_CHUNK
                    if not is_thinking:
                        self.last_prompt_stats.text_chunks += 1
                        self._stale_eligible = not self._active_tool_calls.possibly_open
                        self._prompt_or_tool_seen = True
                    yield AcpEvent(
                        kind=kind,
                        text=chunk,
                        control_notice=_notice_chunk,
                        message_id=parse_message_id((msg.params or {}).get("update")),
                    )
                    if not is_thinking and _is_tool_interrupted_marker(chunk):
                        # kiro-cli's built-in security filter cancelled the turn's tools.
                        # It will not send a ``complete`` response — synthesize one so the
                        # caller exits instead of waiting out the prompt timeout.
                        # (_emit_tool_interrupted_sel logs + audits the cancellation.)
                        self._emit_tool_interrupted_sel("_dispatch_events")
                        got_complete = True
                        for tr_event in await self._read_new_tool_results():
                            yield tr_event
                        yield AcpEvent(
                            kind=EVENT_COMPLETE,
                            usage=self.last_prompt_stats.to_turn_usage(),
                        )
                        return
                tool_event = self._extract_tool_event(msg)
                if tool_event:
                    self._stale_eligible = False
                    self._prompt_or_tool_seen = True
                    # Arm the tool-stall watchdog: if no further data arrives
                    # within _TOOL_STALL_TIMEOUT, _prompt_loop treats the turn
                    # as dead instead of hanging to the full prompt timeout.
                    self._active_tool_calls.add(tool_event.tool_call_id or "")
                    self._tool_dispatched = True
                    # Record every observed tool_call so PostToolUse can recover
                    # tool_name from _observed_tool_calls (see
                    # _maybe_fire_post_tool_hooks).
                    if tool_event.tool_call_id:
                        self._observed_tool_calls[tool_event.tool_call_id] = (
                            tool_event.title or "unknown",
                            tool_event.tool_kind or "",
                        )
                    # Check for results from previous tool before yielding new tool_call
                    for tr_event in await self._read_new_tool_results():
                        yield tr_event
                    # ACP-layer tool audit for clients with no external audit
                    # loop (e.g. app worker pools). No-op unless audit_source is set.
                    await self._maybe_audit_tool_call(tool_event)
                    # Co-located with the SEL audit Pre-side: fire the PreToolUse
                    # HOOK ENGINE so app/worker-pool subagents reach hook parity
                    # with the main agent / SubagentManager. PostToolUse fires
                    # separately on the tool_result branch below (fire_tool_hooks
                    # is Pre-only). No-op unless audit_source is set.
                    await self._skill_reads.note(tool_event)
                    await self._maybe_fire_pre_tool_hooks(tool_event)
                    yield tool_event
                # Real-time tool result from `tool_call_update` session updates.
                # kiro-cli emits these the moment a tool completes — fires before
                # the JSONL flush, so the inline pill gets its output the instant
                # the tool finishes instead of waiting for the next tool call or
                # message end.  See `_extract_tool_call_update` for the dual-path
                # (content blocks vs rawOutput) details.
                await self._tripwire_pi_gate(msg)
                await self._tripwire_goose_mode(msg)
                tool_result_event = self._extract_tool_call_update(msg)
                if tool_result_event:
                    if tool_result_event.tool_status in TERMINAL_TOOL_STATUSES:
                        self._active_tool_calls.discard(tool_result_event.tool_call_id or "")
                        self._tool_dispatched = self._active_tool_calls.possibly_open
                        self._stale_eligible = not self._active_tool_calls.possibly_open
                    # Fire the PostToolUse HOOK ENGINE now that the tool RESULT
                    # (and its output) exists — the Pre-vs-Post split is required
                    # because fire_tool_hooks above is PreToolUse-only. No-op
                    # unless audit_source is set.
                    self._skill_reads.credit(tool_result_event)
                    await self._maybe_fire_post_tool_hooks(tool_result_event)
                    self._tripwire_spec_disabled_tool(tool_result_event)
                    yield tool_result_event
                # claude-agent-acp emits a separate `tool_call_update` carrying
                # the refined title / kind / rawInput once `chunk.input` finishes
                # streaming (the initial `tool_call` arrives with empty input and
                # generic title like "Terminal"/"grep").  Yield as a refinement
                # event so the dashboard pill + persisted message can be
                # patched in place — see `EVENT_TOOL_CALL_UPDATE` in chat_runner.
                tool_refine_event = self._extract_tool_call_refinement(msg)
                if tool_refine_event:
                    await self._skill_reads.note(tool_refine_event)
                    yield tool_refine_event
            elif action == "metadata":
                self._track_metadata(msg)
            elif action == "compaction":
                self._handle_compaction_status(msg)
                params = msg.params or {}
                status = params.get("status", {})
                status_type = status.get("type", "") if isinstance(status, dict) else str(status)
                summary = params.get("summary", "")
                if status_type == "failed":
                    # The notice reads AcpEvent.title, and `summary` is empty on
                    # failure — carry the notification's own reason so the row
                    # stops collapsing to "unknown error".
                    summary = compaction_failure_detail(params)
                yield AcpEvent(kind=EVENT_COMPACTION_STATUS, text=status_type, title=summary)
            elif action == "clear":
                yield AcpEvent(kind=EVENT_CLEAR_STATUS)
            elif action == "subagent_list":
                params = msg.params or {}
                _subs = params.get("subagents")
                logger.debug(
                    "ACP subagent_list received: %s entries",
                    len(_subs) if isinstance(_subs, list) else "n/a",
                )
                if isinstance(_subs, list):
                    # A roster means children exist: a spawned child can mutate
                    # state before its first activity frame is observed, so the
                    # roster itself closes the registration-throttle window.
                    if _subs:
                        self._prompt_or_tool_seen = True
                    # No runtime_global marking here: AcpClient owns a dedicated
                    # process with a single session, so an ownerless frame from
                    # it is this session's own roster, never a co-tenant's.
                    yield AcpEvent(kind=EVENT_SUBAGENT_LIST, subagents=_subs)
            elif action == "subagent_activity":
                # _kiro.dev/session/update: a sub-agent session's own update,
                # tagged with its sessionId. Carries either:
                # - tool_call_chunk (inner tool starting) with toolCallId/title
                # - agent_message_chunk with text (sub-agent's streamed output)
                params = msg.params or {}
                _ssid = str(params.get("sessionId") or "")
                _upd_raw = params.get("update")
                _upd = _upd_raw if isinstance(_upd_raw, dict) else {}
                _su_kind = str(_upd.get("sessionUpdate") or "")
                _tcid = str(_upd.get("toolCallId") or "")
                # Prefer the nested ``content.text`` shape kiro-cli 2.10.0 emits
                # (via the shared chunk extractor); fall back to the flat
                # top-level ``text`` for older payloads. Fall back on a falsy
                # chunk (None OR empty string) so an empty nested content.text
                # does not shadow a populated flat ``text``.
                _su_chunk, _su_thinking = self._extract_text_chunk(msg)
                _su_text = str(_su_chunk or (_upd.get("text") or ""))
                # A frame naming THIS session is this turn's own stream, not a
                # child's. kiro-cli sends the extension spelling for the parent's
                # own tool-call chunk as well as for a child's update, and the
                # sessionId is what separates them; treating the parent's as a
                # child would put a sub-agent card on the session the user is
                # already looking at.
                if _ssid and _ssid == (self._session_id or ""):
                    continue
                if _ssid and _tcid:
                    # A child's tool call is this process's side effect for
                    # replay purposes — the parent prompt spawned it, so a
                    # replay would re-run it. Close the registration-throttle
                    # window, exactly as the shared-runtime handle does for its
                    # fanned-out child tool calls.
                    self._prompt_or_tool_seen = True
                    # Sub-agent output is LLM-influenced — redact the title before
                    # it reaches the dashboard/persisted message.
                    _su_title, _ = redact_exfiltration_urls(str(_upd.get("title") or ""))
                    _su_title, _ = redact_credentials(_su_title)
                    yield AcpEvent(
                        kind=EVENT_SUBAGENT_ACTIVITY,
                        sub_session_id=_ssid,
                        tool_call_id=_tcid,
                        title=_su_title,
                    )
                elif _ssid and _su_text and _su_kind == "agent_message_chunk" and not _su_thinking:
                    # Sub-agent's streamed text output — the real content; redact
                    # exfil URLs + credentials the sub-agent may have emitted.
                    # Skip reasoning/thinking blocks (is_thinking) — those are the
                    # sub-agent's internal reasoning, not user-visible output, and
                    # the flat pre-port read never surfaced them.
                    # Observed child output also closes the registration-throttle
                    # window: a child whose tool frame was lost or differently
                    # spelled must not read as "did nothing".
                    self._prompt_or_tool_seen = True
                    _su_text, _ = redact_exfiltration_urls(_su_text)
                    _su_text, _ = redact_credentials(_su_text)
                    yield AcpEvent(
                        kind=EVENT_SUBAGENT_ACTIVITY,
                        sub_session_id=_ssid,
                        text=_su_text,
                    )
            elif action == "agent_switched":
                saw_agent_switch = True
                params = msg.params or {}
                agent_name = params.get("agentName", "")
                self._note_agent_switched(params if isinstance(params, dict) else None)
                yield AcpEvent(kind=EVENT_AGENT_SWITCHED, text=agent_name)
            elif action == "mcp_oauth_request":
                params = msg.params or {}
                server_name = str(params.get("serverName") or params.get("name") or "")
                oauth_url = str(params.get("oauthUrl") or params.get("url") or "")
                # Reject unsafe-scheme URLs *before* recording dedupe so a later
                # safe retry for the same server still gets through.
                if not _is_safe_oauth_url(oauth_url):
                    if oauth_url:
                        logger.warning(
                            "ACP: refusing unsafe mid-session MCP OAuth URL for %s",
                            server_name or "(unknown)",
                        )
                    continue
                # Without a server_name we can't correlate this banner with the
                # later server_initialized/server_init_failure notification (the
                # discard path keys on server_name only).
                if not server_name:
                    logger.warning(
                        "ACP: dropping mid-session MCP OAuth request with empty serverName"
                    )
                    continue
                if server_name in self._oauth_emitted_servers:
                    logger.debug(
                        "ACP: dropping duplicate mid-session MCP OAuth request for %s",
                        server_name,
                    )
                    continue
                self._oauth_emitted_servers.add(server_name)
                logger.info("ACP: MCP OAuth request mid-session for %s", server_name)
                yield AcpEvent(
                    kind=EVENT_MCP_OAUTH_REQUEST,
                    server_name=server_name,
                    oauth_url=oauth_url,
                )
            elif action == "mcp_server_initialized":
                params = msg.params or {}
                server_name = str(params.get("serverName") or params.get("name") or "")
                if server_name:
                    logger.info("ACP: MCP server initialized: %s", server_name)
                    # Allow re-emission of oauth_request if this server's token expires later.
                    self._oauth_emitted_servers.discard(server_name)
                    yield AcpEvent(
                        kind=EVENT_MCP_SERVER_INITIALIZED,
                        server_name=server_name,
                    )
            elif action == "mcp_server_init_failure":
                params = msg.params or {}
                server_name = str(params.get("serverName") or params.get("name") or "")
                err = str(params.get("error") or "")
                if server_name:
                    logger.warning("ACP: MCP server init failure: %s — %s", server_name, err)
                    # The current banner is in a closed (failed) state — clear
                    # the dedupe entry so kiro-cli's next oauth_request retry
                    # for this server surfaces a new banner instead of being
                    # silently dropped.
                    self._oauth_emitted_servers.discard(server_name)
                    yield AcpEvent(
                        kind=EVENT_MCP_SERVER_INIT_FAILURE,
                        server_name=server_name,
                        text=err,
                    )

        if not got_complete:
            self._last_stop_reason = ""
            self._turn_done.set()
            if self._compaction_failed_turn:
                # Compaction failed and the turn was abandoned by the backend.
                # Terminate explicitly — checked BEFORE the stale-turn branch so
                # a turn that had streamed text does not report a normal
                # end_turn, and before AcpTimeoutError so callers get the real
                # cause. The user-facing notice is already appended by the
                # compaction-status path; this only ends the turn.
                self._compaction_failed_turn = False
                self._last_stop_reason = STOP_REASON_COMPACTION_FAILED
                yield AcpEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason=STOP_REASON_COMPACTION_FAILED,
                    usage=self.last_prompt_stats.to_turn_usage(),
                )
                return
            # If text was streamed, this is a stale turn (kiro-cli finished
            # but never sent `result`).  Yield a synthetic complete so callers
            # finalize normally instead of showing a timeout error.
            if self._stale_eligible:
                logger.info(
                    "Synthesizing EVENT_COMPLETE after stale turn (chunks=%d)",
                    self.last_prompt_stats.text_chunks,
                )
                yield AcpEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason=STOP_REASON_END_TURN,
                    synthetic_completion=True,
                    usage=self.last_prompt_stats.to_turn_usage(),
                )
                return
            raise AcpTimeoutError()

    async def command_result(
        self, command: str, args: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Execute a native kiro command and return its structured result.

        Internal callers use this when the command's ``data`` object is the
        contract (for example ``/mcp`` and ``/tools`` inventories). The result
        is backend output and must be reduced to a bounded, typed payload before
        it reaches an external surface. The TuiCommand object form is required:
        current kiro-cli can exit without a response for the legacy string form.
        """
        await self.ensure_ready()
        cmd_name, cmd_args = parse_slash_command(command)
        payload: dict[str, Any] = {
            "sessionId": self._session_id,
            "command": {"command": cmd_name, "args": args if args is not None else cmd_args},
        }
        req_id = await self._send_request(METHOD_COMMANDS_EXECUTE, payload)
        result = await self._wait_for_response(req_id, timeout=60.0)
        return result if isinstance(result, dict) else {}

    async def send_command(self, command: str, args: dict | None = None) -> str:
        """Execute a kiro slash command (e.g. '/compact', '/usage', '/effort').

        Returns the response text (if any).  For streaming output use
        :meth:`stream_command` instead.

        When *args* is provided (e.g. ``{"level": "high"}`` for ``/effort``),
        the TuiCommand object form ``{command, args}`` is used so kiro-cli
        receives the arguments — the plain-string form silently drops them.
        Otherwise the plain-string form is kept for backward compat with
        older kiro-cli.
        """
        await self.ensure_ready()
        if args:
            cmd_name = command.strip().split(None, 1)[0].lstrip("/")
            payload: dict = {
                "sessionId": self._session_id,
                "command": {"command": cmd_name, "args": args},
            }
        else:
            payload = {"sessionId": self._session_id, "command": command}
        req_id = await self._send_request(METHOD_COMMANDS_EXECUTE, payload)
        try:
            result = await self._wait_for_response(req_id, timeout=60.0)
            raw = result.get("text", "") or result.get("message", "")
            if raw:
                # Two-pass redaction (URLs + credentials) to match the shared
                # AcpSessionHandle.send_command path: a URL-only pass leaves
                # tokens/keys in slash-command output.
                raw, _ = redact_exfiltration_urls(raw)
                raw, _ = redact_credentials(raw)
            return raw
        except AcpTimeoutError:
            logger.debug("Command '%s' response timed out (may still be running)", command)
            return ""

    async def stream_command(
        self, command: str, timeout: float | None = None
    ) -> AsyncIterator[AcpEvent]:
        """Execute a slash command and yield streaming AcpEvents.

        Uses ``_kiro.dev/commands/execute`` with the TuiCommand object
        format (``{command, args}``) so kiro-cli executes the command
        natively and streams full output via ``session/update``.
        """
        timeout = await _effective_prompt_timeout_async(timeout)
        self._cancelled = False
        await self.ensure_ready()

        cmd_name, cmd_args = parse_slash_command(command)
        req_id = await self._send_request(
            METHOD_COMMANDS_EXECUTE,
            {
                "sessionId": self._session_id,
                "command": {"command": cmd_name, "args": cmd_args},
            },
        )
        async for event in self._dispatch_events(req_id, timeout, extract_agent_from_result=True):
            yield event

    async def cancel_session(self, grace_secs: float = 0.0) -> None:
        """Cancel the current in-flight operation via ACP session/cancel.

        Per ACP spec, session/cancel is a JSON-RPC notification (no id).
        The ack arrives as stopReason:"cancelled" on the session/prompt
        response, not as a response to this message.

        ``grace_secs`` is the caller's cooperative-cancel ack budget. The read
        loop aborts the turn as "unresponsive" once this elapses, so it must
        not be shorter than the budget the caller will wait on; we raise the
        per-cancel grace to ``max(floor, grace_secs)`` so a configured budget
        above the 10s floor genuinely extends the window instead of the loop
        bailing early and forcing a session-losing hard kill.
        """
        if not self._session_id:
            logger.debug("cancel_session: no session_id, skip")
            return
        self._cancelled = True
        self._cancel_ts = time.monotonic()
        self._cancel_grace_secs = max(_CANCEL_GRACE_SECS, grace_secs)
        logger.debug(
            "cancel_session: sending session/cancel notification (sid=%s, turn_done=%s, proc_alive=%s)",
            self._session_id,
            self._turn_done.is_set(),
            self._is_process_alive(),
        )
        if not self._process or not self._process.stdin:
            logger.debug("cancel_session: process not running")
            return
        try:
            notification = {
                "jsonrpc": "2.0",
                "method": METHOD_CANCEL,
                "params": {"sessionId": self._session_id},
            }
            data = json.dumps(notification) + "\n"
            # Best effort, never swallowed by the write lock: a cancel is the
            # one signal that can end a wedged turn, so it is appended unlocked
            # if the lock does not come within the no-progress bound.
            outcome = await write_notification_best_effort(
                self._process.stdin,
                self._stdin_write_lock(),
                data.encode(),
                bound_secs=transport_framing._RESPONSE_WRITE_BOUND_SECS,
            )
            # Only a drained frame is evidence the backend moved: an unlocked
            # append or a stall must not refresh the activity clock the
            # wedged-turn probes read.
            if outcome == "drained":
                self._last_activity = time.monotonic()
            logger.debug("cancel_session: wrote session/cancel notification (%s)", outcome)
        except Exception:
            logger.debug("Cancel notification failed", exc_info=True)
        # ACP: after session/cancel the client MUST answer every permission request
        # still open with the ``cancelled`` outcome. A harness that waits for that
        # answer before it acks the cancel (goose, pi) otherwise holds the turn open
        # until the caller's ack budget runs out and the process is hard-killed --
        # the path every Stop takes on a surface that did not reject the open
        # approval first. ``_permission_options`` holds the requests not yet
        # answered: ``reject_tool`` and ``approve_tool``'s auto-resolve path pop
        # their entry, and a new turn clears the map. An ``approve_tool`` call with
        # an explicit ``option_id`` leaves its entry behind; no caller passes one to
        # this client.
        open_requests = list(self._permission_options)
        self._permission_options.clear()
        if not open_requests:
            return
        for request_id in open_requests:
            # A cancelled gate dialog is not an approval, so pi's tripwire treats
            # the call exactly as it treats a rejected one.
            self._note_pi_gate_denied(request_id)

        # Each cancelled approval is a denial Crew made, so it gets its own SEL
        # record before the answers go out. ONE off-loop hop for all of them, with
        # the accessor inside it (an unwarmed ``sel()`` initialises on the calling
        # thread), bounded the way ``_maybe_audit_tool_call`` bounds its write, so
        # a stuck SEL backend costs this Stop at most one audit timeout.
        def _audit_cancelled() -> None:
            log = sel_module.sel()
            for request_id in open_requests:
                log.log_tool_invocation(
                    session_key=self._session_key or "",
                    agent=self._agent,
                    source="acp",
                    tool_name="approval_cancel",
                    tool_kind="permission",
                    outcome="rejected_on_cancel",
                    request_id=request_id,
                    metadata={"backend": self.backend},
                )

        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(subprocess_executor(), _audit_cancelled),
                timeout=_SEL_AUDIT_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.warning("cancel_session: SEL audit of cancelled approvals failed", exc_info=True)
        for request_id in open_requests:
            try:
                await self._send_response(request_id, {"outcome": {"outcome": OUTCOME_CANCELLED}})
            except Exception:
                logger.debug(
                    "cancel_session: answering an open permission request failed",
                    exc_info=True,
                )
                break

    async def steer(self, message: str) -> bool:
        """Inject a mid-turn steer into the running turn via kiro-cli's
        ``_session/steer`` ext-method. Fire-and-forget: the request is written
        but the response is NOT awaited, because the in-flight turn's read loop
        is the single consumer of this client's stdout and a concurrent wait
        would steal the turn's messages. kiro-cli answers ``{queued: true}`` and
        the authoritative signal is the ``steering_consumed`` notification; the
        steered reply streams back inside the SAME in-flight prompt. Returns
        False for an empty message or when there is no active session.

        A backend in ``ACP_BACKENDS_STEERING_REQUEST`` is delivered by a request
        whose answer the read loop hands over instead: goose by
        :meth:`_steer_via_request`, claude by :meth:`_steer_via_steering_request`.
        """
        text = (message or "").strip()
        if not text or not self._session_id:
            return False
        if self._speaks_steering_request:
            return await self._steer_via_request(
                _CLIENT_STEERING_REQUEST_METHODS[self.backend], text
            )
        if self.backend in ACP_BACKENDS_STEERING_REQUEST:
            return await self._steer_via_steering_request(text)
        wrapped = f"<user_message>\n{text}\n</user_message>"
        await self._send_request(
            "_session/steer", {"sessionId": self._session_id, "message": wrapped}
        )
        # See AcpSessionHandle.steer for why the stamp is taken at the write.
        self._last_steer_monotonic = time.monotonic()
        return True

    # The prompt request whose read loop takes ``_session/steering`` steers right
    # now, or None: set when a prompt turn's dispatch starts, cleared when its
    # loop ends or its terminal is handled.
    _steering_turn_req: int | None = None
    # Whether the adapter advertised ``_meta.steering.supported`` at ``initialize``.
    _steering_advertised: bool = False
    # True while the turn's dispatch is parked on a permission event its consumer
    # has not answered: the answer to a steer sent then would not be read until
    # the approval is, past the steer's own wait.
    _parked_on_permission: bool = False
    # (turn request id, ``<user_message>``-wrapped text) for each steer accepted
    # into the running turn and not yet released by its terminal.
    _steers_accepted: list[tuple[int, str]]

    def _steering_request_ready(self) -> bool:
        """Whether this session's adapter takes ``_session/steering`` as this client
        sends it: advertised at the handshake, and claude-agent-acp at or above
        :data:`CLAUDE_ACP_STEERING_MIN_VERSION`. The floor is that adapter's own
        release history, and claude is the only member this client sends
        ``_session/steering`` to, so any other member fails closed here."""
        if self._is_claude and getattr(self, "_steering_advertised", False) is True:
            return _adapter_version_at_least(
                getattr(self, "_agent_version", ""), CLAUDE_ACP_STEERING_MIN_VERSION
            )
        return False

    def _steering_waiters(self) -> dict[int, "asyncio.Future[Any]"]:
        """Unanswered steering requests by id. Created on first use so a client
        built without ``__init__`` (test doubles) has one."""
        waiters = getattr(self, "_steering_waiter_map", None)
        if waiters is None:
            waiters = {}
            self._steering_waiter_map = waiters
        return waiters

    def _settle_steering_answer(self, msg: JsonRpcMessage) -> bool:
        """Hand *msg* to the steer waiting for it; True when it was that answer.

        Only a response whose id a steer registered counts. A steer that stopped
        waiting has unregistered its id, so its answer is skipped like any other
        stray response.
        """
        waiters = getattr(self, "_steering_waiter_map", None)
        if not waiters or msg.method is not None or msg.id is None:
            return False
        answer = waiters.pop(msg.id, None)
        if answer is None:
            return False
        if not answer.done():
            if msg.error is not None:
                answer.set_exception(AcpError("steering request refused"))
            else:
                answer.set_result(msg.result)
        return True

    async def _steer_via_steering_request(self, text: str) -> bool:
        """Deliver a user steer over the adapter's ``_session/steering`` request.

        See ``ACP_BACKENDS_STEERING_REQUEST`` for what claude-agent-acp does with
        it. The request carries ``idleBehavior: "promptRequired"``, so a steer that
        reaches the adapter after its turn settled is answered ``promptRequired``
        and runs nothing. The answer is read by the running turn's read loop and
        awaited here until it arrives, the turn ends, or
        ``STEERING_ANSWER_WAIT_SECS`` pass.

        * ``injected`` while the same turn is still running -- True. That means
          accepted, not consumed: the turn reports it as ``EVENT_STEER_CONSUMED``
          only when it ends cleanly (see :meth:`_release_accepted_steers`).
        * anything else, an error answer, no answer in time, or a turn that ended
          first -- False, and the caller queues the text as the next turn.

        At-least-once: a steer the adapter injected just as the turn ended is
        queued as well and runs a second time, visibly, as its own turn.
        """
        aimed_at = self._steering_turn_req
        if (
            aimed_at is None
            or self._turn_done.is_set()
            or self._cancelled
            or self._parked_on_permission
            or not self._steering_request_ready()
        ):
            return False
        waiters = self._steering_waiters()
        accepted = getattr(self, "_steers_accepted", None)
        if accepted is None:
            accepted = []
            self._steers_accepted = accepted
        if len(text) > MAX_STEERING_TEXT_CHARS or len(waiters) + len(accepted) >= (
            MAX_STEERING_ANSWERS
        ):
            return False
        answer: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        sent: list[int] = []

        def _register(request_id: int) -> None:
            sent.append(request_id)
            waiters[request_id] = answer

        turn_done = self._turn_done
        try:
            await self._send_request(
                METHOD_SESSION_STEERING,
                {
                    "sessionId": self._session_id,
                    "prompt": [{"type": "text", "text": text}],
                    "_meta": {"steering": {"idleBehavior": "promptRequired"}},
                },
                on_id=_register,
            )
            await self._await_steering_answer(answer, turn_done)
        except Exception as exc:
            if getattr(exc, "ambiguous_delivery", False) is True:
                # The frame WAS written and sits in a pipe the live child may
                # still read: raised so the caller requeues it as possibly
                # delivered instead of as fresh text.
                raise
            logger.debug("steering request not written: %s", type(exc).__name__)
            return False
        finally:
            # Every exit stops waiting, so a late answer is skipped as stray.
            if sent:
                waiters.pop(sent[0], None)
        if not answer.done() or answer.cancelled() or answer.exception() is not None:
            return False
        outcome = steering_outcome(answer.result())
        if outcome != STEERING_INJECTED:
            logger.info("steering request answered %r; queueing", outcome[:32])
            return False
        if turn_done.is_set() or self._steering_turn_req != aimed_at:
            # Injected, but its turn is already over: nothing will report it
            # consumed, so the caller queues the text.
            return False
        self._steers_accepted.append((aimed_at, f"<user_message>\n{text}\n</user_message>"))
        self._last_steer_monotonic = time.monotonic()
        return True

    async def _await_steering_answer(
        self, answer: "asyncio.Future[Any]", turn_done: asyncio.Event
    ) -> None:
        """Wait for *answer* until it arrives, the turn ends, or the budget is spent.

        The budget is ``STEERING_ANSWER_WAIT_SECS`` of time the turn's read loop
        is free to read. While the loop is parked on an approval nothing reads
        stdout, so an answer queued behind the permission frame cannot arrive;
        that time is not charged, and the answer is read once the approval is.
        """
        ended = asyncio.ensure_future(turn_done.wait())
        budget = STEERING_ANSWER_WAIT_SECS
        try:
            while budget > 0 and not answer.done() and not ended.done():
                parked = self._parked_on_permission
                started = time.monotonic()
                await asyncio.wait(
                    {answer, ended},
                    timeout=min(budget, _STEERING_WAIT_SLICE_SECS),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # A slice that saw the loop parked at either end is not charged.
                if not parked and not self._parked_on_permission:
                    budget -= time.monotonic() - started
        finally:
            ended.cancel()

    def _release_accepted_steers(self, req_id: int, reason: str, refusal: Any) -> list[str]:
        """Echo text for the steers accepted into turn *req_id*, at a clean end only.

        claude-agent-acp settles a steered turn only once a result answers the
        steer, so a turn that ends with ``end_turn``, uncancelled and unrefused,
        has run every steer it accepted. On any other ending nothing is reported
        and the caller's pending entry for each is queued instead. The turn
        stops taking steers here, before its terminal is yielded, so a steer
        answered after this point is not accepted into a turn that has ended.
        """
        accepted = getattr(self, "_steers_accepted", None) or []
        self._steers_accepted = []
        if self._steering_turn_req == req_id:
            self._steering_turn_req = None
        if self._cancelled or refusal or reason != STOP_REASON_END_TURN:
            return []
        return [wrapped for aimed_at, wrapped in accepted if aimed_at == req_id]

    async def _steer_via_request(self, method: str, text: str) -> bool:
        """Deliver a user steer over goose's ``_goose/unstable/session/steer``.

        The request names the run it is for, so it is sent only while a turn of
        ours is running, its run id has been announced, and no approval is open:
        goose's agent loop is parked on the approval and cannot take the text, so
        the answer would not be read before the turn moves on. The read loop owns
        stdout, so this waits for the loop to hand the answer over, until it
        arrives, the turn ends, or ``STEERING_ANSWER_WAIT_SECS`` pass.

        True means goose queued the text for the running turn, not that the turn
        read it. goose drains queued steers between model calls and echoes each one
        as a ``user_message_chunk`` carrying ``_meta.goose.steer`` and the answer's
        ``messageId``; that echo is reported as ``EVENT_STEER_CONSUMED``. A steer
        still queued when its run ends is discarded by goose, so no echo arrives
        and the caller's pending entry is queued by the turn's teardown
        (``steer_needs_loss_recovery``). False -- an error answer, no answer in
        time, or no run to aim at -- sends the caller down its queue path.

        At-least-once: an answer slower than the wait leaves goose holding text
        the caller has also queued, and if the turn then reads it, it runs again
        as its own turn.
        """
        run_id = getattr(self, "_active_run_id", None)
        if (
            self._turn_done.is_set()
            or not run_id
            or getattr(self, "_permission_gate_events", None)
            or len(text) > MAX_STEERING_TEXT_CHARS
        ):
            return False
        held = getattr(self, "_steers_awaiting_echo", None)
        if held is None:
            held = self._steers_awaiting_echo = {}
        answers = getattr(self, "_steer_answers", None)
        if answers is None:
            answers = self._steer_answers = {}
        if len(held) + len(answers) >= MAX_STEERING_ANSWERS:
            return False
        answer: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        wrapped = f"<user_message>\n{text}\n</user_message>"
        req_ids: list[int] = []

        def _register(req_id: int) -> None:
            req_ids.append(req_id)
            answers[req_id] = (answer, wrapped)

        def _forget() -> None:
            # Every exit that does not return True drops the registration and any
            # echo mapping its answer already made, so a late echo settles nothing
            # the caller has moved on from.
            for req_id in req_ids:
                answers.pop(req_id, None)
            if answer.done() and not answer.cancelled() and answer.exception() is None:
                held.pop(answer.result(), None)

        params = {
            "sessionId": self._session_id,
            "expectedRunId": run_id,
            "prompt": [{"type": "text", "text": text}],
        }
        turn_done = self._turn_done
        ended = asyncio.ensure_future(turn_done.wait())
        try:
            await self._send_request(method, params, on_id=_register)
            waiters: set[asyncio.Future[Any]] = {answer, ended}
            await asyncio.wait(
                waiters, timeout=STEERING_ANSWER_WAIT_SECS, return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            _forget()
            raise
        finally:
            ended.cancel()
        for req_id in req_ids:
            answers.pop(req_id, None)
        if not answer.done() or answer.cancelled() or answer.exception() is not None:
            # No answer before the turn ended or the bound passed, or an error
            # answer (an older goose without the method answers -32601). The
            # class only: the error text is the harness's.
            logger.info(
                "steer request on %s not accepted: %s",
                self._session_id,
                "no answer" if not answer.done() else type(answer.exception()).__name__,
            )
            _forget()
            return False
        self._last_steer_monotonic = time.monotonic()
        return True

    def _note_active_run(self, msg: JsonRpcMessage) -> None:
        """Track the run id goose announces for the running prompt.

        goose sends ``session_info_update`` with ``_meta.goose.activeRunId`` set
        when a prompt starts and null when it ends; a steer must name it.
        """
        if self.backend not in _CLIENT_STEERING_REQUEST_METHODS:
            return
        update = (msg.params or {}).get("update")
        if not isinstance(update, dict) or update.get("sessionUpdate") != UPDATE_SESSION_INFO:
            return
        goose = (update.get("_meta") or {}).get("goose")
        if isinstance(goose, dict) and "activeRunId" in goose:
            run_id = goose.get("activeRunId")
            self._active_run_id = run_id if isinstance(run_id, str) and run_id else None

    def _settle_steer_answer(self, msg: JsonRpcMessage) -> bool:
        """Hand a steer request's answer to the waiting ``steer`` call.

        Runs in the read loop, so an accepted answer maps its ``messageId`` to the
        steer's text HERE, before any later frame is read: goose can echo the
        steer in the same burst, before the waiting call resumes. Returns True
        when the answer was one of ours.
        """
        answers = getattr(self, "_steer_answers", None)
        if not answers or not isinstance(msg.id, int):
            return False
        entry = answers.pop(msg.id, None)
        if entry is None:
            return False
        answer, wrapped = entry
        if answer.done():
            return True
        result = msg.result if isinstance(msg.result, dict) else {}
        message_id = result.get("messageId")
        if msg.error or not isinstance(message_id, str) or not message_id:
            answer.set_exception(AcpError("steer request refused"))
            # Retrieved here so an unawaited refusal never logs as unhandled.
            answer.exception()
            return True
        held = getattr(self, "_steers_awaiting_echo", None)
        if held is None:
            held = self._steers_awaiting_echo = {}
        held[message_id] = wrapped
        answer.set_result(message_id)
        return True

    def _steer_echo_event(self, msg: JsonRpcMessage) -> AcpEvent | None:
        """``EVENT_STEER_CONSUMED`` for goose's echo of a steer this turn sent.

        Matched by the ``messageId`` the steer's answer returned, never by text,
        and only on a chunk goose marks ``_meta.goose.steer``. A steer spanning
        several content blocks echoes once per block; the first settles it.
        """
        held = getattr(self, "_steers_awaiting_echo", None)
        if not held:
            return None
        update = (msg.params or {}).get("update")
        if not isinstance(update, dict) or update.get("sessionUpdate") != UPDATE_USER_MESSAGE_CHUNK:
            return None
        goose = (update.get("_meta") or {}).get("goose")
        if not isinstance(goose, dict) or goose.get("steer") is not True:
            return None
        wrapped = held.pop(str(goose.get("messageId") or ""), None)
        if wrapped is None:
            return None
        return AcpEvent(kind=EVENT_STEER_CONSUMED, text=wrapped)

    # Monotonic stamp of the last steer handed to the backend, 0.0 when never
    # steered. Mirrors AcpSessionHandle.last_steer_monotonic — the dashboard's
    # keepalive route reads whichever of the two backs the live session.
    _last_steer_monotonic: float = 0.0

    @property
    def last_steer_monotonic(self) -> float:
        """Monotonic time of the last steer written to the backend (0.0 if none)."""
        return self._last_steer_monotonic

    @property
    def supports_steer(self) -> bool:
        """True when the backend takes a user's mid-turn message.

        Membership in ``ACP_BACKENDS_STEER`` (kiro-cli's ``_session/steer``) or in
        ``ACP_BACKENDS_STEERING_REQUEST`` (harness-parity H6), so a harness added
        later does not inherit either from ``not _is_claude``. A member of the
        second takes its own request where this client speaks one
        (``_CLIENT_STEERING_REQUEST_METHODS``: goose); otherwise it also needs its
        adapter to have advertised ``_session/steering`` at the handshake and to be
        at or above its floor, and before the handshake it reads False.
        """
        if self.backend in ACP_BACKENDS_STEER or self._speaks_steering_request:
            return True
        return self.backend in ACP_BACKENDS_STEERING_REQUEST and self._steering_request_ready()

    @property
    def supports_refusal_steer(self) -> bool:
        """True when a deny notice steered into the refused turn reaches the model.

        Only ``ACP_BACKENDS_STEER``: a steering-request member's steer is reported
        consumed only at a clean end (``_session/steering``) or on its echo
        (goose), so it is not a deny-notice channel.
        """
        return self.backend in ACP_BACKENDS_STEER

    @property
    def steer_needs_loss_recovery(self) -> bool:
        """True when an accepted steer can still be dropped with its turn.

        A ``_session/steering`` steer (``ACP_BACKENDS_STEERING_REQUEST``) is
        reported consumed only when its turn ends cleanly, so a cancelled or
        failed turn leaves it for the caller to queue. goose discards a steer
        still queued when its run ends, so its accepted steer is reported
        consumed only on goose's echo, and the caller requeues it otherwise.
        Only the dashboard composer keeps such an entry; kiro-cli's
        ``_session/steer`` keeps every steer delivered into the turn.
        """
        return self.backend in ACP_BACKENDS_STEERING_REQUEST

    @property
    def _speaks_steering_request(self) -> bool:
        """Whether this client speaks the member's own steer request
        (``_CLIENT_STEERING_REQUEST_METHODS``) rather than ``_session/steering``."""
        return (
            self.backend in ACP_BACKENDS_STEERING_REQUEST
            and self.backend in _CLIENT_STEERING_REQUEST_METHODS
        )

    def turn_finished_cleanly(self) -> bool:
        """Whether the last turn reached its own end boundary uncancelled.

        Asked by a capability that treats the turn's end AS its result -- an inline
        compaction, whose harness emits no status frame, so the boundary is the only
        evidence there is. The test is POSITIVE: the stop reason must BE
        ``end_turn``, because "not cancelled" also admits a refusal, a token limit,
        a reason this build does not recognise, and a turn that reported none.

        ``_cancelled`` is checked as well, because a cancel that never got an ack
        leaves the reason empty while the flag is already set -- and an unacked
        cancel is exactly the case where whatever the turn was for is least likely
        to have happened.

        Declared HERE rather than read off ``_cancelled`` / ``_last_stop_reason``
        from outside: both are this class's private turn state, and a consumer
        reading them across the wrapper boundary would answer this question from a
        shape it does not own -- silently, if either field were ever renamed.
        """
        if self._cancelled:
            return False
        return self._last_stop_reason == STOP_REASON_END_TURN

    async def wait_turn_done(self, timeout: float) -> str:
        """Wait for the current prompt to finish. Returns stop_reason or raises TimeoutError."""
        await asyncio.wait_for(self._turn_done.wait(), timeout=timeout)
        return self._last_stop_reason

    def _note_background_launch(self, msg: JsonRpcMessage) -> None:
        """Record a background launch the harness reports on this frame, if any."""
        params = msg.params if isinstance(msg.params, dict) else {}
        if self._background_launches.note(params.get("update"), time.monotonic()):
            logger.info(
                "ACP: harness launched background work for this session: %s",
                self._background_launches.describe(),
            )
        drift = self._background_launches.take_drift()
        if drift is not None:
            logger.info(
                "ACP: a tool response looks like a background launch but matches no "
                "recognised claude-agent-acp shape (%s); the watchdog will not hold "
                "this session for it. Logged once per session.",
                drift,
            )

    def background_launch(self) -> tuple[float, str] | None:
        """``(seconds since, description)`` of this session's newest background
        launch, or ``None`` when its harness reported none (see LLMProvider)."""
        age = self._background_launches.age(time.monotonic())
        if age is None:
            return None
        return age, self._background_launches.describe()

    def has_active_turn(self) -> bool:
        """True if a prompt is in flight AND has not yet been cancelled.

        Returns False as soon as ``cancel_session()`` has been called, even
        before the agent acknowledges the cancel. Callers that need to force
        a kill regardless of cancel state should skip this check.
        """
        return (
            not self._cancelled
            and not self._turn_done.is_set()
            and self._is_process_alive()
            and not getattr(self, "_stdin_stalled", False)
        )

    def has_unfinished_turn(self) -> bool:
        """True if the native turn has NOT reached its done boundary and the
        process is still alive — INDEPENDENT of cancel state.

        Unlike :meth:`has_active_turn`, this does NOT exclude a turn that has
        already been ``cancel_session()``'d but whose native turn-done ack has
        not yet arrived. That turn still holds kiro-cli's native-session lock
        open, so killing the process now leaves the lock held and reproduces the
        empty-response-after-restart bug. The shutdown drain uses THIS signal so
        it still waits for such a turn's ack before the process is killed.
        """
        return not self._turn_done.is_set() and self._is_process_alive()

    # ── Private Helpers ──

    async def _send_prompt(self, message: str, *, allow_image: bool = True) -> int:
        # Shared with AcpSessionHandle.prompt via prompt_blocks so the two paths
        # cannot drift.
        return await self._send_request(
            METHOD_PROMPT,
            {
                "sessionId": self._session_id,
                # Offloaded: see the note in session_handle.prompt -- image
                # reads and base64 encoding must not block the event loop.
                "prompt": await asyncio.to_thread(
                    build_prompt_blocks, message, allow_image=allow_image
                ),
            },
        )

    async def _read_prompt_response(self, req_id: int, timeout: float) -> str:
        output: list[str] = []
        self.last_prompt_stats = self.last_prompt_stats.carry_over()

        async for action, msg in self._prompt_loop(req_id, timeout):
            if action == "complete":
                reason = ""
                result = msg.result or {}
                if isinstance(result, dict):
                    reason = result.get("stopReason", "") or ""
                self._track_prompt_usage(result)
                # See send_message_stream: settle for the context counts, drop
                # the event this API cannot yield.
                self._settle_claude_compaction(reason)
                self._settle_codex_compaction(reason)
                # Fold a metadata refusal onto the terminal, as the streaming
                # paths do, so a caller reading ``last_stop_reason`` sees the
                # refusal and does not retry a deterministic decline.
                reason, _ = self.last_prompt_stats.terminal_refusal(reason)
                self._last_stop_reason = reason
                self._turn_done.set()
                return "".join(output)
            if action == "error":
                if error_is_refusal_terminal(msg.error, self.last_prompt_stats.refusal):
                    # A content-filter refusal terminating as a bare -32603 (see
                    # _dispatch_events). This API returns the turn's text, so
                    # return what streamed (the canned explanation) under the
                    # refusal stop reason rather than raising: an AcpError here
                    # would discard the reason, feed the retry ladder a
                    # deterministic decline, and retire a healthy worker.
                    reason, _ = self.last_prompt_stats.terminal_refusal("")
                    self._last_stop_reason = reason
                    self._turn_done.set()
                    return "".join(output)
                _raise_acp_error(msg.error, self._advertised_model_ids(), backend=self.backend)
            if action == "permission":
                await self._handle_permission(msg)
            elif action == "server_request_unknown":
                await self._reject_unknown_server_request(msg)
            elif action == "update":
                self._track_usage_update(msg)
                self._note_background_launch(msg)
                # See send_message_stream: settle the codex compaction for the
                # context counts, drop the event this API cannot return.
                self._codex_compaction_event(msg)
                chunk, is_thinking = self._extract_text_chunk(msg)
                if chunk and not is_thinking:
                    # Apply the claude compaction state change, then KEEP the
                    # chunk. This path returns one string callers treat as the
                    # agent's answer, and a caller that wants the notice out of
                    # that string subtracts it with
                    # strip_claude_compaction_notices; dropping it here would
                    # silently truncate a real answer on a misclassification.
                    self._claude_compaction_event(chunk)
                    output.append(chunk)
                    self.last_prompt_stats.text_chunks += 1
                    if _is_tool_interrupted_marker(chunk):
                        self._emit_tool_interrupted_sel("_read_prompt_response")
                        return "".join(output)  # see _dispatch_events for rationale
                self._track_tool_call(msg)
                # Mirror _dispatch_events for the send_message (worker-pool)
                # dispatch path: send_message drives tools through here, not
                # through the stream _dispatch_events, so without this
                # app/worker-pool subagents never reached hook/SEL parity. All
                # gating stays inside the _maybe_* methods (self._audit_source),
                # so main-chat send_message callers (audit_source=None) are no-op.
                tool_event = self._extract_tool_event(msg)
                if tool_event:
                    # Record every observed tool_call so PostToolUse can recover
                    # tool_name from _observed_tool_calls (see _maybe_fire_post_tool_hooks).
                    if tool_event.tool_call_id:
                        self._observed_tool_calls[tool_event.tool_call_id] = (
                            tool_event.title or "unknown",
                            tool_event.tool_kind or "",
                        )
                    await self._maybe_audit_tool_call(tool_event)
                    await self._skill_reads.note(tool_event)
                    await self._maybe_fire_pre_tool_hooks(tool_event)
                await self._tripwire_pi_gate(msg)
                await self._tripwire_goose_mode(msg)
                tool_result_event = self._extract_tool_call_update(msg)
                if tool_result_event:
                    self._skill_reads.credit(tool_result_event)
                    await self._maybe_fire_post_tool_hooks(tool_result_event)
                    self._tripwire_spec_disabled_tool(tool_result_event)
            elif action == "metadata":
                self._track_metadata(msg)
            elif action == "compaction":
                self._handle_compaction_status(msg)

        self._last_stop_reason = ""
        self._turn_done.set()
        raise AcpTimeoutError(partial_output="".join(output))

    def _extract_text_chunk(self, msg: JsonRpcMessage) -> tuple[str | None, bool]:
        """Extract text from an agent_message_chunk or agent_thought_chunk update.

        Returns (text, is_thinking). is_thinking is True when the chunk is an
        ``agent_thought_chunk`` (claude-agent-acp emits reasoning under this
        dedicated update type) or when an ``agent_message_chunk``'s inner
        content block type indicates reasoning (kiro-cli style).
        """
        params = msg.params or {}
        update = params.get("update", {})
        # The update comes straight from the agent process; a non-dict value
        # (null/list/string) would raise AttributeError here, inside the
        # prompt-turn dispatch path — same boundary rule as _track_usage_update.
        if not isinstance(update, dict):
            return None, False
        kind = update.get("sessionUpdate")
        if kind == UPDATE_AGENT_MESSAGE_CHUNK:
            content = update.get("content", {})
            if not isinstance(content, dict):
                return None, False
            text = content.get("text")
            content_type = content.get("type", "text")
            is_thinking = content_type in ("thinking", "reasoning")
            return text, is_thinking
        if kind == UPDATE_AGENT_THOUGHT_CHUNK:
            content = update.get("content", {})
            if not isinstance(content, dict):
                return None, True
            text = content.get("text")
            return text, True
        return None, False

    def _track_usage_update(self, msg: JsonRpcMessage) -> None:
        """Track context usage and config updates from session update notifications."""
        params = msg.params or {}
        update = params.get("update", {})
        kind = update.get("sessionUpdate") if isinstance(update, dict) else None
        if kind == UPDATE_USAGE:
            # used/size come straight from the agent process; a malformed
            # value (string, list, bool, NaN/Infinity, bignum beyond float
            # range) must degrade to "absent" rather than raise mid-turn.
            # parse_usage_update validates both fields at the shared
            # chokepoint used by AcpSessionHandle._handle_update too.
            used, size = parse_usage_update(update)
            if used is not None and size and size > 0:
                self.last_prompt_stats.context_pct = round((used / size) * 100, 1)
                # Keep the raw counts so the dashboard token text uses the real
                # served window (size) instead of re-deriving it from the model id.
                self.last_prompt_stats.context_used_tokens = int(used)
                self.last_prompt_stats.context_window_tokens = int(size)
                # Mark the counts authoritative so a later metadata
                # contextUsagePercentage cannot clobber this token-derived pct.
                self.last_prompt_stats.context_tokens_from_usage = True
                self.last_prompt_stats.note_pct_reported()
            else:
                logger.debug("usage_update missing used/size: %s", update)
            # Session-cumulative billing cost (claude seam); kiro never sends
            # the key so this is None on the kiro path. Delta'd per turn on
            # the stats object (monotonic guard lives there).
            cost = parse_usage_cost(update)
            if cost is not None:
                self.last_prompt_stats.apply_cost_cumulative(cost)
        elif kind == UPDATE_CONFIG_OPTION:
            self._handle_config_option_update(msg)
        elif self._is_claude and kind and kind not in KNOWN_SESSION_UPDATES:
            logger.debug("Unhandled session update type: %s", kind)

    def _track_prompt_usage(self, result: Any) -> None:
        """Fold a PromptResponse's turn-scoped token counts into the stats.

        The claude-agent-acp adapter reports per-turn token counts on the
        prompt response; kiro-cli's response carries only ``stopReason``, so
        ``parse_prompt_token_usage`` returns None there and the stats are
        untouched (harness parity). Validation lives at that shared
        chokepoint, mirroring ``_track_usage_update``.
        """
        tokens = parse_prompt_token_usage(result)
        if tokens is not None:
            self.last_prompt_stats.apply_prompt_token_usage(*tokens)

    async def _maybe_audit_tool_call(self, tool_event: "AcpEvent") -> None:
        """Emit a per-tool-call SEL audit for clients with no external audit loop.

        App/worker-pool clients (code-review-sage, knowledge llm_pool) run tools
        through this AcpClient without going through chat_runner or SubagentManager,
        so their tool calls would otherwise never reach the security audit log.
        Gated on ``audit_source`` (None for chat / subagent clients, so they never
        double-log). Best-effort: a SEL failure must never break tool dispatch, but
        is logged at WARNING so audit-pipeline breakage surfaces to on-call.

        The ``sel().log_tool_invocation`` call is offloaded onto
        ``subprocess_executor()`` (the same dedicated pool the child-record
        offloads in this file use, e.g. ``_capture_child_records``) so that any
        SEL-backend I/O (file write, network, DNS) can never block the event loop
        and freeze the gateway heartbeat. The dedicated pool — rather than the
        default executor — isolates a call that can wedge on a stuck kernel
        resource, so a hung SEL backend cannot starve default-pool users. The
        offload is additionally bounded by ``asyncio.wait_for``: if the SEL call
        hangs, the ``await`` cannot stall this turn's tool dispatch indefinitely
        (a pending executor future never raises on its own) — the timeout raises
        ``TimeoutError`` (an ``Exception`` subclass), which the handler below
        swallows so dispatch always proceeds. Per the fault-isolation guideline a
        leaked worker thread is survivable; a stalled dispatch is not.
        ``tool_name``/``tool_kind`` use the same ``or`` fallbacks as the
        observed-tool-call bookkeeping above, so an audit record is always emitted
        with meaningful values rather than lost.
        """
        if not self._audit_source:
            return
        # Bind the guard-narrowed (non-None) audit_source into a local: mypy does
        # not carry the ``if not self._audit_source`` narrowing into the nested
        # lambda closure below, so referencing the attribute directly there would
        # be seen as ``str | None``.
        audit_source = self._audit_source
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(),
                    lambda: sel().log_tool_invocation(
                        session_key=self._session_key or "",
                        agent=self._agent,
                        source=audit_source,
                        tool_name=tool_event.title or "unknown",
                        tool_kind=tool_event.tool_kind or "",
                        outcome="auto_approved",
                    ),
                ),
                timeout=_SEL_AUDIT_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.warning("ACP-layer SEL audit failed", exc_info=True)

    async def _maybe_fire_pre_tool_hooks(self, tool_event: "AcpEvent") -> None:
        """Fire the PreToolUse HOOK ENGINE for a tool_call, for audit-source clients.

        App/worker-pool clients (code-review-sage, knowledge llm_pool) run their
        tools through this AcpClient without going through chat_runner or
        SubagentManager, so — until this method existed — the script-hook engine
        never fired for them. That silently dropped skill-usage telemetry (a
        PostToolUse hook matching 'Reading *SKILL.md*') for /add_context and every
        other subagent skill load. This brings those clients to hook parity with
        the main agent and SubagentManager subagents.

        Gated on ``audit_source`` (None for chat / subagent clients) so the
        chat/main client is completely unaffected — it fires its own hooks via
        chat_runner and must never double-fire. No-ops if the global hook store is
        not initialized. Best-effort + NON-FATAL: any hook-engine error is caught
        and logged at WARNING (mirroring the SEL audit handler above) and NEVER
        breaks tool dispatch — the hook fire is awaited directly, exactly as
        ``subagent.py`` awaits ``fire_tool_hooks`` (the underlying
        ``run_script_hook`` bounds each script with its own timeout).

        ``fire_tool_hooks`` fires PreToolUse ONLY (see hooks.py) — PostToolUse is
        fired separately in ``_maybe_fire_post_tool_hooks`` once the tool RESULT
        (and its output) is available.
        """
        if not self._audit_source:
            return
        hook_store = get_global_hook_store()
        if hook_store is None:
            return
        try:
            # Redact tool_input before firing user hooks (parity with Post path); addresses security-controls review.
            _redacted_input = tool_event.tool_input
            if isinstance(_redacted_input, str):
                _redacted_input, _ = redact_credentials(_redacted_input)
                _redacted_input, _ = redact_exfiltration_urls(_redacted_input)
            elif _redacted_input is not None:
                # Non-str (dict/list) inputs must ALSO be redacted, not bypassed by
                # the isinstance(str) guard. Serialize to JSON, redact the string,
                # and pass the redacted JSON string (fire_tool_hooks json.loads it,
                # so it expects a str | None — do NOT deserialize back to an object).
                _serialized = json.dumps(_redacted_input)
                _serialized, _ = redact_credentials(_serialized)
                _serialized, _ = redact_exfiltration_urls(_serialized)
                _redacted_input = _serialized
            await fire_tool_hooks(
                hook_store,
                # Fall back to 'unknown' when the event carries no title, matching
                # the Post path's tool_name recovery so a hook matcher sees a
                # consistent name across Pre/Post.
                tool_event.title or "unknown",
                _redacted_input,
                agent_role=self._agent or None,
            )
        except Exception:
            logger.warning("ACP-layer PreToolUse hook failed", exc_info=True)

    async def _maybe_fire_post_tool_hooks(self, tool_result_event: "AcpEvent") -> None:
        """Fire the PostToolUse HOOK ENGINE for a tool RESULT, for audit-source clients.

        Companion to ``_maybe_fire_pre_tool_hooks``. The Pre-vs-Post split is
        forced by the hook engine: ``fire_tool_hooks`` fires PreToolUse ONLY (at
        tool_call time the tool has not run and has no output), so PostToolUse must
        fire here, on the RESULT branch. The output MUST be carried on
        ``tool_response={'output': ...}`` — the IDENTICAL shape used by chat_runner
        (main agent) and subagent.py — because the skill-usage emit.sh reads the
        SKILL.md frontmatter out of ``tool_response.output`` (and matches the
        'Reading *SKILL.md*' tool_name). Without the output payload the telemetry
        hook fires blind and captures nothing.

        Gated on ``audit_source`` and no-ops if the global hook store is
        uninitialized. Best-effort + NON-FATAL: any error is caught + logged and
        never breaks dispatch. The tool RESULT event carries no title (see
        ``_build_tool_result_event``), so the tool_name is recovered from
        ``_observed_tool_calls`` (populated on the tool_call above) with the same
        'Running: ' strip ``fire_tool_hooks`` / subagent.py apply, so Pre and Post
        agree on the tool_name a hook matcher sees. ``tool_output`` is already
        redacted at the ACP boundary by ``_build_tool_result_event``.
        """
        if not self._audit_source:
            return
        hook_store = get_global_hook_store()
        if hook_store is None:
            return
        # Fall back to "unknown" to match the Pre path (Pre/Post tool_name consistency).
        tool_name = (
            self._observed_tool_calls.get(tool_result_event.tool_call_id or "", ("unknown", ""))[0]
            or "unknown"
        )
        if tool_name.startswith("Running: "):
            tool_name = tool_name[9:]
        try:
            # Redact before firing user hooks (parity with chat_runner PostToolUse); addresses security-controls review.
            _redacted_output, _ = redact_credentials(tool_result_event.tool_output or "")
            _redacted_output, _ = redact_exfiltration_urls(_redacted_output)
            # Bound the payload handed to user hook scripts to the first 2000 chars
            # (parity with chat_runner/subagent.py [:2000]). Redact-then-truncate is
            # deliberate: redact the FULL output first so secrets anywhere are scrubbed,
            # only THEN truncate — truncating first could leave a secret past char 2000.
            _redacted_output = _redacted_output[:2000]
            await hook_store.fire(
                HOOK_EVENT_POST_TOOL_USE,
                tool_name=tool_name,
                tool_response={"output": _redacted_output},
                agent_role=self._agent or None,
            )
        except Exception:
            logger.warning("ACP-layer PostToolUse hook failed", exc_info=True)

    def _emit_tool_interrupted_sel(self, site: str) -> None:
        """Emit a SEL audit event when kiro-cli cancels tool uses via its security filter.

        This is a security-relevant permission decision (kiro-cli denied tool execution)
        that KiroCrew observes but does not control.  Logged so the audit trail reflects
        that tools were blocked even though the decision was made outside KiroCrew.
        Also emits a single WARNING log line (grep-friendly for on-call) with session
        correlation — covers all three call sites so none of them fire silently.
        """
        logger.warning(
            "kiro-cli cancelled tool use(s) [site=%s session=%s]", site, self._session_id
        )
        try:
            # Re-imported at call time on purpose: the module-level binding is
            # captured at import time, so only this rebind resolves the CURRENT
            # ``kiro_crew.sel.sel`` and lets a substituted emitter be observed.
            from kiro_crew.sel import sel

            sel().log_tool_invocation(
                session_key=self._session_key or "",
                source="acp",
                tool_name="kiro_cli_security_filter",
                tool_kind="client_built_in",
                outcome="denied",
                metadata={"site": site, "reason": "tool_interrupted_marker"},
            )
        except Exception:
            logger.warning("SEL audit failed for tool_interrupted at %s", site, exc_info=True)

    def _track_tool_call(self, msg: JsonRpcMessage) -> None:
        """Track tool calls in stats (used by send_message/send_message_stream)."""
        params = msg.params or {}
        update = params.get("update", {})
        if not isinstance(update, dict):
            return
        if update.get("sessionUpdate") == UPDATE_TOOL_CALL:
            title = update.get("title", "unknown")
            kind = update.get("kind", "unknown")
            self.last_prompt_stats.tool_calls.append((kind, title))
            logger.debug("ACP tool_call: %s (%s)", title, kind)

    def _extract_tool_event(self, msg: JsonRpcMessage) -> AcpEvent | None:
        params = msg.params or {}
        update = params.get("update", {})
        if not isinstance(update, dict):
            return None
        if update.get("sessionUpdate") == UPDATE_TOOL_CALL:
            title = update.get("title", "unknown")
            _wire_title = title if isinstance(title, str) and title != "unknown" else ""
            kind = update.get("kind", "unknown")
            # First PRESENT key, not first truthy one -- an explicit empty
            # rawInput is a real argument set for the directive digest. Mirrors
            # _dispatch._build_tool_call_event.
            raw_input = next(
                (update[k] for k in ("rawInput", "input", "params") if update.get(k) is not None),
                None,
            )
            purpose = extract_tool_purpose(raw_input)
            logger.debug(
                "ACP tool_call raw: %s",
                {k: v for k, v in update.items() if k != "sessionUpdate"},
            )
            # Build initial tool input string from raw params
            tool_call_id = update.get("toolCallId", "")
            # Start the round-trip clock here rather than at the yield: this is
            # the first moment Kiro Crew sees the call, and the same id's terminal
            # status is stamped in _extract_tool_call_update below. Both of this
            # class's message loops reach this method, so instrumenting it covers
            # them without a second call site in each.
            # One classification for the caches, the event and the metric:
            # kind + adapter-authored MCP markers, never the kind alone (see
            # _dispatch.classify_tool_call).
            identity = classify_tool_call(update)
            note_tool_call_started(
                tool_call_id,
                kind=kind,
                mcp_server_name=identity.mcp_server_name,
                # getattr: a real client always carries _session_id, but this
                # extractor is also driven directly with lightweight test
                # doubles (test_acp_tool_identity), and telemetry must not be
                # the reason such a double stops working.
                scope=getattr(self, "_session_id", "") or "",
            )
            input_str = ""
            if tool_call_id and raw_input:
                input_str = (
                    _dumps_degraded(raw_input, indent=2)
                    if isinstance(raw_input, (dict, list))
                    else str(raw_input)
                )
            # For edit tools with diff content blocks, generate unified diff
            found_diff = False
            _diff_path = ""
            content_blocks = update.get("content", [])
            if isinstance(content_blocks, list):
                for cb in content_blocks:
                    if isinstance(cb, dict) and cb.get("type") == "diff":
                        old = cb.get("oldText") or ""
                        new = cb.get("newText") or ""
                        path = cb.get("path", "")
                        if tool_call_id and path:
                            self._tool_call_diff_path[tool_call_id] = path
                            _diff_path = path
                        diff_str = _make_unified_diff(old, new, path)
                        if diff_str:
                            input_str = diff_str
                            found_diff = True
                        break
            # Fallback when no diff content block was found: derive from the
            # edit args (strReplace pair, create/insert content). Gated on
            # the EDIT kind — "content"-shaped args exist on non-edit tools.
            if not found_diff and (
                kind == "edit"
                or (isinstance(raw_input, dict) and raw_input.get("command") == "strReplace")
            ):
                diff_str = derive_edit_diff(raw_input)
                if diff_str:
                    input_str = diff_str
            # Redact sensitive content before caching/displaying
            input_redacted = False
            if input_str:
                safe_input, _ = redact_exfiltration_urls(input_str)
                safe_input, _ = redact_credentials(safe_input)
                input_redacted = safe_input != input_str
                input_str = safe_input
            if tool_call_id and input_str:
                self._tool_call_inputs[tool_call_id] = input_str
                self._tool_call_input_redacted[tool_call_id] = input_redacted
            # Cache the STRUCTURED raw params (path/url/command) so the later
            # request_permission event can feed the governance gate's arg-derived
            # scopes (filesystem.write / network.egress). Bounded by the same
            # clear() as _tool_call_inputs; capped to avoid unbounded growth on a
            # stream that never sends a matching permission request.
            # Truthy-gated, like _dispatch's raw_params_cache: this cache is the
            # permission event's TRUSTED params source, and an empty ``{}`` here
            # (Claude's initial frame) would be found by ``.get`` and suppress the
            # inline-frame fallback, so the refinement's sensitive path never
            # reached governance. The event's own raw_tool_params keeps the ``{}``.
            if tool_call_id and isinstance(raw_input, dict) and raw_input:
                if len(self._tool_call_params) > _MAX_CACHED_TOOL_PARAMS:
                    self._tool_call_params.clear()
                self._tool_call_params[tool_call_id] = raw_input
            # Redact LLM-influenced fields before dashboard display
            if purpose:
                purpose, _ = redact_exfiltration_urls(purpose)
                purpose, _ = redact_credentials(purpose)
            # Prefer rawInput.description over the SDK-provided title (e.g.
            # some backends' Bash tool emits "List KiroCrew ACP module files"
            # alongside `ls /workplace/...`). For claude-agent-acp this rarely
            # fires here because the initial tool_call has empty rawInput —
            # the refinement path in `_extract_tool_call_refinement` is what
            # the user actually sees. Same helper is used in both places.
            # Capture the canonical shell signal from the raw kind BEFORE
            # redaction so the later permission_request event (which carries no
            # kind) can inherit it via the toolCallId cache below.
            is_shell = identity.is_shell
            if tool_call_id:
                if identity.kind_resolved:
                    self._tool_call_is_shell[tool_call_id] = is_shell
                # An MCP identity is a classification too -- it says "served by a
                # server", which is not a host command. Unclassified means neither
                # the kind, the harness's own shell channel nor an MCP marker said
                # anything (classify_tool_call folds all three; a malformed codex
                # marker resolves nothing and lands here as unclassified).
                #
                # Written through the same compatibility shape as the redaction map
                # below: an instance allocated with ``__new__`` (embedders do this) has
                # no such dict, and assigning into a missing one would raise inside the
                # event builder rather than record anything.
                _unclassified = getattr(self, "_tool_call_unclassified", None)
                if _unclassified is not None:
                    _unclassified[tool_call_id] = not (
                        identity.kind_resolved or bool(identity.mcp_server_name)
                    )
                # Same lifecycle as is_shell: cache the trusted MCP server
                # identity so the later permission event can inherit it.
                self._tool_call_mcp_server[tool_call_id] = identity.mcp_server_name
                # Cache the trusted tool name too, so the permission event can
                # rebuild mcp__<server>__<tool> for per-tool governance.
                self._tool_call_tool_name[tool_call_id] = identity.tool_name
                # The tool's own name, for the permission event's
                # harness_tool_id on a backend with no _meta.kiro.toolId.
                # getattr for the same reason as _tool_call_unclassified.
                _harness_names = getattr(self, "_tool_call_harness_tool_name", None)
                if _harness_names is not None:
                    _harness_names[tool_call_id] = harness_tool_name(update)
                # KAS only (H13): the mark and the refusal it earns are a KAS
                # refusal mode; a first-class harness's frame is never marked.
                if (
                    identity.identity_unreadable
                    and self.backend in ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL
                ):
                    self._tool_call_identity_unreadable[tool_call_id] = True
            title = _select_tool_title(title, raw_input, kind, is_shell=is_shell) or ""
            if title:
                title, _ = redact_exfiltration_urls(title)
                title, _ = redact_credentials(title)
            if kind:
                kind, _ = redact_exfiltration_urls(kind)
                kind, _ = redact_credentials(kind)
            self.last_prompt_stats.tool_calls.append((kind, title))
            # Trusted identity from adapter-authored markers (NOT the
            # LLM-authored title), shared with the dispatch builder so both
            # event paths carry the same classifier verdict.
            return AcpEvent(
                kind=EVENT_TOOL_CALL,
                title=title,
                # The backend's OWN title, before select_tool_title swaps in a
                # shell call's description: the directive claim's tool resolver
                # reads this and only this (see AcpEvent.wire_title).
                wire_title=_wire_title,
                tool_kind=kind,
                tool_purpose=purpose,
                tool_input=input_str,
                tool_input_redacted=input_redacted,
                tool_call_id=tool_call_id,
                raw_tool_params=raw_input if isinstance(raw_input, dict) else None,
                is_shell=is_shell,
                # Trusted identity from the adapter-authored markers (NOT the
                # LLM-authored title). Earned only when an identity pair was
                # actually extracted from such a source: a frame with no marker
                # populates nothing and asserts no provenance.
                tool_name=identity.tool_name,
                mcp_server_name=identity.mcp_server_name,
                tool_identity_trusted=identity.tool_identity_trusted,
                mcp_identity_trusted=identity.identity_trusted,
                diff_path=_diff_path,
            )
        return None

    def _extract_tool_call_update(self, msg: JsonRpcMessage) -> AcpEvent | None:
        """Extract a real-time tool result from a `tool_call_update` session update.

        kiro-cli streams tool completion via ACP `session/update` notifications
        (not just the JSONL session file). Two updates fire per tool:
          1. A `content` array carrying the tool output as text blocks — arrives
             as soon as the tool finishes, often mid-stream during the agent's
             follow-up text.
          2. A `status: completed` update with `rawOutput.items[].Json.stdout`
             for shell-style tools.
        Both carry the same `toolCallId`; we yield an EVENT_TOOL_RESULT when an
        update provides output or a terminal status. A status-only event carries
        no output. Hooking these gives the inline pill its real output the moment
        the tool finishes, instead of waiting for the kiro-cli JSONL flush at the
        next tool_call boundary or message end.
        """
        params = msg.params or {}
        update = params.get("update", {})
        if not isinstance(update, dict) or update.get("sessionUpdate") != "tool_call_update":
            return None
        tool_use_id = update.get("toolCallId", "")
        if not tool_use_id:
            return None
        # A terminal status is useful even when output parsing finds no text: it
        # closes the observed round-trip without claiming a result body. A
        # non-terminal status leaves the clock running for the real completion.
        record_tool_call_finished(
            tool_use_id,
            status=update.get("status"),
            scope=getattr(self, "_session_id", "") or "",
        )

        output_parts: list[str] = []

        # Path 1: `content` blocks (arrive during tool execution / mid-stream)
        content = update.get("content")
        if isinstance(content, list):
            for block in content:
                text = tool_call_content_text(block)
                if text:
                    output_parts.append(text)

        # Path 2: `rawOutput` (arrives with status=completed) — fallback when
        # there were no content blocks (e.g. some tools only emit rawOutput).
        # kiro-cli tool results land here in two shapes:
        #   items[].Text  — fs_read contents, shell-style text, etc.
        #   items[].Json  — structured tool output (use .stdout when present)
        if not output_parts:
            raw_output = update.get("rawOutput")
            if isinstance(raw_output, dict):
                items = raw_output.get("items", [])
                if isinstance(items, list):
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        if "Text" in item and item.get("Text"):
                            output_parts.append(str(item["Text"]))
                            continue
                        j = item.get("Json")
                        if isinstance(j, dict):
                            if "stdout" in j and j.get("stdout"):
                                output_parts.append(str(j["stdout"]))
                            else:
                                output_parts.append(_dumps_degraded(j, default=str))
                # Path 3: an object that is not that envelope at all. Mirrors
                # ``_dispatch._build_tool_result_event`` -- ``rawOutput`` is
                # unstructured passthrough, so ``items[]`` is one producer's
                # wrapper and an unrecognised object is not evidence the tool
                # produced nothing. Returning None here is costlier than in the
                # dispatch parser: both call sites use the result to disarm the
                # stall watchdog and to fire PostToolUse hooks, so a dropped
                # event leaves the watchdog armed and the hooks unfired. Gated on
                # the ABSENCE of ``items`` so no ``items[]`` envelope changes.
                # No per-part cut here or above: parts are collected RAW and
                # the single bound is applied AFTER redaction at the end of this
                # method, because a cut taken before redaction can split a
                # credential into fragments no pattern matches.
                if raw_output and "items" not in raw_output:
                    output_parts.append(_dumps_degraded(raw_output, default=str))

        tool_status = str(update.get("status") or "")
        if not output_parts:
            if tool_status not in TERMINAL_TOOL_STATUSES:
                log_unrenderable_content(logger, tool_use_id, content)
                return None
            return AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id=tool_use_id,
                tool_final=tool_status == "completed",
                tool_status=tool_status,
            )

        final_joined = "\n".join(output_parts)
        # Redact the WHOLE join, then bound -- never the reverse. Bounding first
        # can split a credential across the cut into fragments no pattern
        # matches: with a connection URI whose "@" lands on byte 8000, the head
        # slice keeps "://user:password" and drops the "@" the prefilter needs,
        # so the password reaches the dashboard in clear text. Same ordering as
        # `_dispatch._build_tool_result_event` and as `_compaction_detail` below.
        _redacted = redact_text(final_joined)
        tool_output_digest, tool_output_bytes = _measure_tool_output(_redacted)
        final_output = _redacted[:8000]
        return AcpEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id=tool_use_id,
            tool_output=final_output,
            tool_output_digest=tool_output_digest,
            tool_output_bytes=tool_output_bytes,
            # Same contract as `_dispatch._build_tool_result_event`: only a
            # result the redactor changed can hold a credential to trace.
            tool_output_credentials=(
                tool_output_fingerprints(final_joined) if _redacted != final_joined else ()
            ),
            tool_final=update.get("status") == "completed",
            tool_status=str(update.get("status") or ""),
        )

    def _extract_tool_call_refinement(self, msg: JsonRpcMessage) -> AcpEvent | None:
        """Extract a refined title/kind/input from a `tool_call_update`.

        claude-agent-acp emits two events per tool: an initial `tool_call`
        on streaming `content_block_start` (when `chunk.input` is still empty,
        so the title falls back to the generic tool name like "Terminal" or
        "grep"), then a follow-up `tool_call_update` once `chunk.input` is
        fully streamed — that update carries the populated `rawInput` and a
        refined `title`/`kind` from the upstream `toolInfoFromToolUse`
        (e.g. `"ls /local/home/user/.kiro/crew/workspace"`).

        We yield an EVENT_TOOL_CALL_UPDATE so the dashboard can patch the
        existing pill / persisted message in place — see the matching
        handler in `chat_runner.py`. Returns None when the update only
        carries output (handled separately by `_extract_tool_call_update`).
        """
        params = msg.params or {}
        update = params.get("update", {})
        if not isinstance(update, dict) or update.get("sessionUpdate") != "tool_call_update":
            return None
        tool_use_id = update.get("toolCallId", "")
        if not tool_use_id:
            return None
        title = update.get("title")
        kind = update.get("kind")
        raw_input = update.get("rawInput")
        # Only emit when at least one refinement field is present. Pure-output
        # updates (content/rawOutput only) are handled by the result extractor.
        if title is None and kind is None and not raw_input:
            return None
        # The refinement carries the COMPLETE params (Claude streams an empty
        # rawInput on the initial tool_call). Refresh the permission event's
        # trusted-params cache from it, as _dispatch._build_tool_refinement_event
        # does, so governance's sensitive-path scope reads the real arguments.
        if tool_use_id and isinstance(raw_input, dict) and raw_input:
            if len(self._tool_call_params) > _MAX_CACHED_TOOL_PARAMS:
                self._tool_call_params.clear()
            self._tool_call_params[tool_use_id] = raw_input
        # Build the input string the same way `_extract_tool_event` does so
        # the merged toolLog entry / message meta lines up across both events.
        input_str = ""
        if isinstance(raw_input, (dict, list)) and raw_input:
            input_str = _dumps_degraded(raw_input, indent=2)
        elif isinstance(raw_input, str):
            input_str = raw_input
        # Edit-style diff content blocks: prefer the rendered unified diff over
        # the raw input dict (mirrors `_extract_tool_event`).
        _diff_path = ""
        content_blocks = update.get("content", [])
        if isinstance(content_blocks, list):
            for cb in content_blocks:
                if isinstance(cb, dict) and cb.get("type") == "diff":
                    old = cb.get("oldText") or ""
                    new = cb.get("newText") or ""
                    path = cb.get("path", "")
                    if path:
                        self._tool_call_diff_path[tool_use_id] = path
                        _diff_path = path
                    diff_str = _make_unified_diff(old, new, path)
                    if diff_str:
                        input_str = diff_str
                    break
        input_redacted = False
        if input_str:
            safe_input, _ = redact_exfiltration_urls(input_str)
            safe_input, _ = redact_credentials(safe_input)
            input_redacted = safe_input != input_str
            input_str = safe_input
            self._tool_call_inputs[tool_use_id] = input_str
            self._tool_call_input_redacted[tool_use_id] = input_redacted
        # Refresh the cached shell signal only when this refinement carries a
        # kind. A refinement that omits kind must NOT clobber a True cached by
        # the initial tool_call notification (kind is optional on updates).
        # Cache off the RAW kind, not the redacted kind_str. Resolved BEFORE the
        # title so the label rule sees the real classification rather than a
        # missing kind.
        _identity = classify_tool_call(update)
        if _identity.kind_resolved:
            self._tool_call_is_shell[tool_use_id] = _identity.is_shell
        is_shell = self._tool_call_is_shell.get(tool_use_id, False)
        # Prefer rawInput.description over the SDK-supplied title (e.g.
        # Bash's "List KiroCrew ACP module files" rather than `ls /workplace/...`).
        # Same helper as `_extract_tool_event` so the rule is consistent.
        # A refinement carrying a title but no rawInput still overwrites the
        # pill, so the command has to be recoverable from the params the initial
        # tool_call cached — otherwise a backend that sends a generic title on
        # both events lands that label on a pill the first event got right.
        _title_params: object = raw_input
        if not (isinstance(raw_input, dict) and raw_input):
            _title_params = self._tool_call_params.get(tool_use_id)
        title_source = _select_tool_title(title, _title_params, kind, is_shell=is_shell)
        title_str = ""
        if title_source:
            title_str, _ = redact_exfiltration_urls(title_source)
            title_str, _ = redact_credentials(title_str)
        kind_str = ""
        if isinstance(kind, str) and kind:
            kind_str, _ = redact_exfiltration_urls(kind)
            kind_str, _ = redact_credentials(kind_str)
        # The refinement's rawInput is the COMPLETE params object, so it carries
        # the reserved purpose argument too. Read it here or the purpose is lost
        # whenever the initial tool_call streamed an empty rawInput — and
        # consumers that treat an empty purpose as "fall back to the raw title"
        # (the session list's running-status line) would replace a good purpose
        # with a command. Mirrors `_dispatch._build_tool_refinement_event`.
        purpose = extract_tool_purpose(raw_input)
        if purpose:
            purpose, _ = redact_exfiltration_urls(purpose)
            purpose, _ = redact_credentials(purpose)
        return AcpEvent(
            kind=EVENT_TOOL_CALL_UPDATE,
            title=title_str,
            wire_title=title if isinstance(title, str) else "",
            tool_kind=kind_str,
            tool_purpose=purpose,
            tool_input=input_str,
            tool_input_redacted=input_redacted,
            tool_call_id=tool_use_id,
            raw_tool_params=raw_input if isinstance(raw_input, dict) else None,
            is_shell=is_shell,
            diff_path=_diff_path,
        )

    async def _read_new_tool_results(self) -> list[AcpEvent]:
        results = await asyncio.to_thread(self._read_new_tool_results_sync)
        if results:
            self._active_tool_calls.difference_update(event.tool_call_id or "" for event in results)
            self._tool_dispatched = self._active_tool_calls.possibly_open
            self._stale_eligible = not self._active_tool_calls.possibly_open
        return results

    def _read_new_tool_results_sync(self) -> list[AcpEvent]:
        """Read new ToolResults entries from the kiro-cli session JSONL file."""
        if not self._session_id:
            return []
        jsonl_path = kiro_sessions_dir() / f"{self._session_id}.jsonl"
        if not jsonl_path.exists():
            return []
        results: list[AcpEvent] = []
        try:
            # Read as bytes and decode each record on its own with
            # ``errors="replace"``: kiro-cli writes UTF-8, but a strict text
            # read raises on one bad byte BEFORE the offset below is saved, so
            # every later scan would fail at the same place for the rest of
            # the session. The capped reader bounds one record's memory, and
            # its offsets are byte offsets to resume from.
            with open(jsonl_path, "rb") as f:
                f.seek(self._jsonl_pos)
                for _start, end, record in bounded_raw_records_with_offsets(
                    f, jsonl_path, label="JSONL tool results"
                ):
                    if record is not None and not record.endswith((b"\n", b"\r")):
                        break  # partial line — retry next call
                    # Saved before the line is parsed, so a line this scan
                    # cannot use (an over-cap one included) costs that line
                    # only, and the lines after it are still read.
                    self._jsonl_pos = end
                    if record is None:
                        continue
                    try:
                        results.extend(self._jsonl_tool_results(record))
                    except Exception:  # noqa: BLE001 - one line, not the scan
                        logger.debug("JSONL: skipped a tool-result line", exc_info=True)
        except Exception:
            logger.debug("Failed to read JSONL for tool results", exc_info=True)
        if results:
            logger.debug("JSONL: read %d tool result(s) from %s", len(results), jsonl_path.name)
        return results

    @staticmethod
    def _jsonl_tool_results(line: bytes) -> list[AcpEvent]:
        """The tool results one session JSONL line carries; ``[]`` for any other line.

        Anything that is not the expected shape is skipped at its own level --
        a line that is not a JSON object, a ``data`` or ``content`` that is not
        an object or list, an item that is not an object, a result without a
        non-empty string ``toolUseId``, a ``stdout`` that is not a string.
        """
        entry = parse_json_object_line(line, errors="replace")
        if entry is None or entry.get("kind") != "ToolResults":
            return []
        data = entry.get("data")
        if not isinstance(data, dict):
            return []
        content = data.get("content")
        if not isinstance(content, list):
            return []
        results: list[AcpEvent] = []
        for c in content:
            if not isinstance(c, dict) or c.get("kind") != "toolResult":
                continue
            tr = c.get("data")
            if not isinstance(tr, dict):
                continue
            tool_use_id = tr.get("toolUseId")
            if not isinstance(tool_use_id, str) or not tool_use_id:
                continue
            tr_content = tr.get("content")
            if not isinstance(tr_content, list):
                continue
            output_parts: list[str] = []
            for rc in tr_content:
                if not isinstance(rc, dict):
                    continue
                if rc.get("kind") == "json":
                    d = rc.get("data", {})
                    if isinstance(d, dict) and "stdout" in d:
                        out = d.get("stdout", "")
                        if isinstance(out, str) and out:
                            output_parts.append(out[:4000])
                    else:
                        output_parts.append(_dumps_degraded(d, indent=2)[:4000])
                elif rc.get("kind") == "text":
                    output_parts.append(str(rc.get("data", ""))[:4000])
            if output_parts:
                joined = "\n".join(output_parts)
                results.append(
                    AcpEvent(
                        kind=EVENT_TOOL_RESULT,
                        tool_call_id=tool_use_id,
                        tool_output=joined[:8000],
                        # A kiro-cli result read back from its session
                        # file traces credentials like a streamed one.
                        tool_output_credentials=tool_output_fingerprints(joined),
                    )
                )
        return results

    def _placed_mcp_server_names(self) -> tuple[str, ...]:
        """The server names Crew placed on this session's ``mcpServers`` array.

        Empty when the session has none, including a client built without
        ``__init__``, which has no array cache to read.
        """
        if getattr(self, "_session_mcp_cache", None) is None:
            return ()
        return tuple(
            element["name"]
            for element in self._session_mcp_servers()
            if isinstance(element, dict) and isinstance(element.get("name"), str)
        )

    def _harness_mcp_server_names(self) -> tuple[str, ...]:
        """Every MCP server name this session's harness may state a tool of.

        The servers Crew placed, then those the opencode read-back found in the
        harness's own config, then every server the pi MCP handoff carried -- on pi
        the pooled stubs are not in the placed array, and pi spells each of their
        tools after its name. An instance built without ``__init__`` has none.
        """
        found = getattr(self, "_opencode_config_mcp_servers", ())
        handed_off = getattr(self, "_pi_mcp_server_names", ())
        return tuple(dict.fromkeys([*self._placed_mcp_server_names(), *found, *handed_off]))

    def _backfill_context_window(self, pct: float) -> None:
        """Derive window/used tokens from a percentage-only reading.

        Thin wrapper binding this client's resolved model id; the shared logic
        lives on ``AcpPromptStats.backfill_context_window`` (the AcpSessionHandle
        path delegates to the same method, so the two can no longer drift).
        """
        self.last_prompt_stats.backfill_context_window(pct, self._resolved_model_id or self._model)

    def _track_metadata(self, msg: JsonRpcMessage) -> None:
        params = msg.params or {}
        # Content-filter refusal envelope. Opt-in by membership (H6): a harness
        # that has not demonstrated the payload does not have its metadata
        # frames guessed at. Folded onto the terminal by ``terminal_refusal``.
        if self.backend in ACP_BACKENDS_STRUCTURED_REFUSAL:
            _refusal = parse_refusal(params)
            if _refusal is not None:
                self.last_prompt_stats.refusal = _refusal
        # A real usage_update is authoritative for both the token counts AND the
        # pct derived from them. kiro's metadata percentage can measure a
        # different window, so applying it here would desync the headline % from
        # the "used / total" token text (e.g. 73% shown next to 408K / 1000K).
        # sanitize_pct is the shared coercion (the KAS usagePercentage path uses
        # it too): it clamps NaN/±inf/out-of-range and returns None when absent.
        pct_f = self.last_prompt_stats.sanitize_pct(params.get("contextUsagePercentage"))
        if pct_f is not None and not self.last_prompt_stats.context_tokens_from_usage:
            self.last_prompt_stats.context_pct = pct_f
            self.last_prompt_stats.note_pct_reported()
            self._backfill_context_window(pct_f)
        # kiro streams per-turn billing as meteringUsage entries (unit="credit").
        # Accumulate across the turn's metadata notifications; reset per turn by
        # the AcpPromptStats re-init in _dispatch_events/send_message_stream.
        metering = params.get("meteringUsage")
        if isinstance(metering, list):
            for entry in metering:
                if isinstance(entry, dict) and entry.get("unit") == "credit":
                    try:
                        self.last_prompt_stats.credits += float(entry.get("value", 0) or 0)
                    except (TypeError, ValueError):
                        pass

    def _handle_compaction_status(self, msg: JsonRpcMessage) -> None:
        """Log a ``_kiro.dev/compaction/status`` notification and, on
        completion, drop the now-stale context-usage counts.

        This is the single chokepoint every compaction-status arrival routes
        through (all prompt dispatch loops and ``wait_for_compaction``), so the
        reset cannot be missed by one path. Without it the pre-compaction
        counts survive — and their ``context_tokens_from_usage=True`` flag
        blocks ``_track_metadata`` from applying any fresh percentage — so the
        dashboard's context meter kept showing the old usage after a compact.
        """
        params = msg.params or {}
        status = params.get("status", "")
        logger.info("Compaction status: %s", status)
        # On failure, kiro-cli's notification carries no dedicated error/reason
        # field today (only `status.type` + an optional `summary`, which is
        # populated on success but typically empty on failure). Log the full
        # raw params at WARNING so a future occurrence is actually debuggable
        # instead of surfacing only "unknown error" to the user with nothing
        # to grep for server-side. See Mesh compaction-spam investigation.
        s_type = status.get("type", "") if isinstance(status, dict) else str(status)
        if s_type == "failed":
            if self.memory_mode == "persistent":
                logger.warning("Compaction failed — raw notification params: %s", params)
            # Arm the bounded post-failure wait (see
            # _COMPACTION_FAILED_TURN_BUDGET): kiro-cli may never answer the
            # prompt this compaction was for.
            self._compaction_failed_at = time.monotonic()
            self.last_compaction_transient = compaction_failure_is_transient(params)
        elif s_type == "completed":
            self._compaction_failed_at = None
            self.last_prompt_stats.reset_after_compaction()

    def _claude_compaction_event(self, chunk: str) -> AcpEvent | None:
        """Reclassify a claude-agent-acp compaction notice chunk as an event.

        The Claude adapter reports compaction as plain assistant text rather
        than an out-of-band notification, so this is the claude-side twin of
        ``_handle_compaction_status``: it applies the same state mutations
        (arm/disarm the post-failure budget, drop the stale context counts) and
        returns the EVENT_COMPACTION_STATUS every consumer already understands.
        ``None`` means the chunk is ordinary assistant text and must be yielded
        as-is.

        Backend-gated: the markers are anchored and kiro-cli has no reason to
        emit them, but only the Claude adapter is a KNOWN producer, so no other
        backend's prose can be reinterpreted as a control frame here.

        Callers MUST still forward the text. The event is a SIDE EFFECT, never
        a substitute for the chunk: the adapter ships these notices as ordinary
        assistant text with no marker of any kind, so recognizing one is a guess
        about prose, and a layer that swallowed the chunk would turn any wrong
        guess into deleted model output. A caller that yields structured events
        marks the forwarded chunk ``control_notice`` instead, so a consumer can
        show the text without counting it as the turn's own answer.

        A terminal is only accepted while a compaction is actually in flight.
        ``Compacting completed.`` standing alone is just prose — a user can ask
        for exactly that reply — and swallowing it would delete the answer AND
        reset the context counters against a window nobody summarized.  The
        ``started`` arm cannot be gated the same way: it is what arms the flag.
        """
        parsed = parse_claude_compaction_notice(chunk) if self._is_claude else None
        if parsed is None:
            return None
        status_type, detail = parsed
        if status_type != "started" and not self._claude_compaction_pending:
            return None
        logger.info("Compaction status (claude): %s", status_type)
        self._claude_compaction_pending = status_type == "started"
        if status_type == "completed":
            self._compaction_failed_at = None
            self.last_prompt_stats.reset_after_compaction()
        elif status_type == "failed":
            logger.warning("Compaction failed (claude): %s", detail or "no reason reported")
            # Arm the bounded post-failure wait, exactly as the kiro-cli path
            # does: the backend may never answer the prompt this compaction
            # was for.
            self._compaction_failed_at = time.monotonic()
            # The adapter ships prose, not a payload, so the parsed notice text
            # IS the whole reason — wrap it in the shape the classifier reads
            # rather than teaching it a second input type. ``reason`` is a
            # reason-bearing key, so the scan sees it.
            self.last_compaction_transient = compaction_failure_is_transient(
                {"reason": detail or ""}
            )
        # Backend-echoed text on its way to the dashboard — redact before it can
        # reach any surface (parity with the kiro-cli/KAS compaction summaries).
        return AcpEvent(kind=EVENT_COMPACTION_STATUS, text=status_type, title=redact_text(detail))

    def _codex_compaction_event(self, msg: JsonRpcMessage) -> AcpEvent | None:
        """Reclassify a codex-acp context-compaction frame as an event.

        The codex-side twin of ``_handle_compaction_status`` and
        ``_claude_compaction_event``: it applies the same state mutation (drop the
        stale context counts on a terminal) and returns the
        ``EVENT_COMPACTION_STATUS`` every consumer already understands -- the
        dashboard notice and context-meter reset, the messaging drivers, and
        ``wait_for_compaction``. ``None`` means the frame is an ordinary
        ``tool_call`` and must be handled as one.

        Gated on ``ACP_BACKENDS_INLINE_COMPACTION``, the set of harnesses whose
        compaction lands INSIDE the prompt turn -- so a harness that earns that
        membership inherits the translation by joining the set. The MARKER is what
        actually decides: ``_meta.contextCompaction`` is codex-acp's own, and
        claude (a member) stamps nothing, so the parser declines its frames.

        Reached on this class through the dormant codex seam -- a live codex
        session is served by ``AcpRuntime``, whose ``AcpSessionHandle`` carries the
        same method. Both implementations answer, rather than one, because a
        capability the two transports disagree about is a capability that works on
        whichever one a reader did not test (harness-parity H6).

        Callers MUST still forward the frame. This is a SIDE EFFECT, never a
        substitute: the frame is also a real tool call in the transcript, and a
        layer that swallowed it would drop a row the user watched appear.

        There is no ``failed`` arm because codex-acp sends no such status -- see
        ``parse_codex_compaction_update``. A compaction that errors leaves the
        ``session/prompt`` request unanswered, which the prompt loop's own
        deadline owns; this method neither invents a terminal nor arms the
        post-failure budget on a guess.
        """
        if self.backend not in ACP_BACKENDS_INLINE_COMPACTION:
            return None
        params = msg.params or {}
        update = params.get("update")
        if not isinstance(update, dict):
            return None
        status_type = parse_codex_compaction_update(update)
        if status_type is None:
            return None
        if status_type != "started" and not self._codex_compaction_pending:
            return None
        logger.info("Compaction status (codex): %s", status_type)
        self._codex_compaction_pending = status_type == "started"
        if status_type == "completed":
            self._compaction_failed_at = None
            self.last_prompt_stats.reset_after_compaction()
        # No title: the adapter ships no summary with either frame, and an empty
        # string is what every consumer already renders for "compacted, no
        # summary offered".
        return AcpEvent(kind=EVENT_COMPACTION_STATUS, text=status_type, title="")

    def _settle_codex_compaction(self, reason: str) -> AcpEvent | None:
        """Close out a codex compaction whose terminal never arrived, or None.

        Called at the turn's terminal. ``None`` -- the ordinary case -- means no
        codex compaction was in flight, or one was and it already reported.

        The verdict is ``failed``, and that is the opposite of what the claude
        twin synthesizes. The two harnesses differ in what a MISSING terminal
        means. claude-agent-acp sends its ``Compacting completed.`` text only for
        a MANUAL ``/compact``; an automatic mid-turn compaction takes a different
        path and emits no text at all, so for claude a turn that ends naturally
        after a ``started`` is evidence the compaction finished. codex-acp sends
        its terminal for BOTH -- the capture shows the marked
        ``tool_call_update`` on a manual ``/compact`` and on a native
        ``model_auto_compact_token_limit`` compaction alike -- so here a missing
        terminal is evidence the compaction did NOT finish. Reporting
        ``completed`` would reset the context meter against a window nobody
        summarized.

        One arm for every *reason*, unlike its claude twin, and for the same
        reason the verdict differs: this is not an inference from how the turn
        ended, it is the absence of a frame codex always sends on success. A turn
        cancelled mid-compaction did not compact either.

        Three things it deliberately does NOT do:

        * reset the context counts -- nothing was summarized, so the pre-compaction
          numbers are still the true ones;
        * arm the post-failure budget (``_compaction_failed_at``) -- that budget
          exists to bound a wait for a turn that may never end, and this runs AT
          the end of the turn, so arming it would charge the NEXT turn's idle
          clock for this one's failure;
        * claim the failure is retryable. ``last_compaction_transient`` is set
          False because an inferred failure carries no reason to classify, and
          False is the value that does not promise a user a retry will help.

        The title is text this module authors, not text a backend echoed, so it
        needs no redaction -- and it is there so the surfaces that render a
        failure reason stop collapsing this case to "unknown error".
        """
        if not self._codex_compaction_pending:
            return None
        self._codex_compaction_pending = False
        logger.warning(
            "Compaction status (codex): started with no terminal, turn ended %r",
            reason or "unknown",
        )
        self.last_compaction_transient = False
        return AcpEvent(
            kind=EVENT_COMPACTION_STATUS,
            text="failed",
            title="the turn ended without a compaction result",
            synthesized=True,
        )

    def _settle_claude_compaction(self, reason: str) -> AcpEvent | None:
        """Synthesize the terminal an AUTOMATIC claude compaction never sends.

        The adapter emits ``Compacting completed.`` only from the SDK's
        ``compact_result`` status, which it documents as the manual ``/compact``
        signal; an automatic mid-turn compaction takes its ``compact_boundary``
        case instead and emits only a ``usage_update``.  So the turn ends with a
        dangling ``started``.  Called at the turn's terminal, this closes it out
        so consumers leave their compacting state and the context meter resets.

        Reporting ``completed`` is a statement about what the backend did, not a
        guess — but ONLY for *reason* ``end_turn``.  The turn having reached its
        own natural terminal is the whole evidence that the compaction finished:
        a compaction that had FAILED would have said so (the adapter emits its
        failure text from the same status handler that emits the success text).
        A turn that ends any other way — cancelled by the user's Stop, refused,
        or cut off on a limit — carries no such evidence, so it settles the
        pending flag WITHOUT claiming success: no ``completed`` event, no
        context-counter reset, and no clearing of a recorded failure.  Otherwise
        pressing Stop mid-compaction would fabricate a successful compaction and
        reset the meter against a context that was never actually summarized.
        """
        if not self._claude_compaction_pending:
            return None
        self._claude_compaction_pending = False
        if reason != STOP_REASON_END_TURN:
            logger.info(
                "Compaction status (claude): pending compaction abandoned, turn ended %r",
                reason or "unknown",
            )
            return None
        logger.info("Compaction status (claude): completed (synthesized at turn end)")
        self._compaction_failed_at = None
        self.last_prompt_stats.reset_after_compaction()
        # ``synthesized`` marks this terminal as manufactured at the turn's end
        # rather than observed mid-turn. It arrives AFTER every text chunk of the
        # turn, so a consumer that treats a compaction terminal as a segment
        # boundary would discard the answer a backend produced after compacting.
        return AcpEvent(kind=EVENT_COMPACTION_STATUS, text="completed", title="", synthesized=True)

    async def wait_for_compaction(self, timeout: float = COMPACT_WAIT_TIMEOUT_SECS) -> dict:
        """Read messages until compaction completed/failed arrives. Returns status dict.

        On ``completed``, keeps draining for a short grace window: kiro-cli
        emits a fresh ``_kiro.dev/metadata`` with the REAL post-compaction
        ``contextUsagePercentage`` about a second after the completed status
        (live-probe confirmed). ``_handle_compaction_status`` has already
        dropped the stale counts (clearing the authoritative flag), so that
        metadata re-derives accurate numbers — the caller's ``context_usage``
        broadcast then reports the true compacted size instead of an unknown.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            msg = await self._read_message(timeout=min(remaining, _READ_TIMEOUT))
            if msg is None:
                continue
            if msg.is_method(METHOD_COMPACTION_STATUS):
                self._handle_compaction_status(msg)
                params = msg.params or {}
                status = params.get("status", {})
                s_type = status.get("type", "") if isinstance(status, dict) else str(status)
                if s_type in ("completed", "failed"):
                    self._track_metadata(msg)
                    if s_type == "completed":
                        await self._drain_post_compaction_metadata()
                    # Redact the backend-echoed summary before it reaches callers
                    # (compact() surfaces this to the dashboard and channel
                    # mirrors). Mirrors AcpSessionHandle.wait_for_compaction.
                    summary = redact_text(str(params.get("summary", "") or ""))
                    if s_type == "failed" and not summary:
                        # kiro-cli leaves `summary` empty on failure, so the
                        # manual /compact notice would say nothing — carry the
                        # notification's own reason, read by the same extractor
                        # the dispatch loop uses for the streaming notice.
                        summary = compaction_failure_detail(params)
                    return {"type": s_type, "summary": summary}
            elif msg.is_method(METHOD_METADATA):
                self._track_metadata(msg)
            else:
                # Don't drop — buffer for later processing
                if msg.method and not msg.id:
                    self._mcp_notifications.append(msg)
        return {"type": "timeout"}

    async def _drain_post_compaction_metadata(
        self, grace: float = _POST_COMPACTION_METADATA_GRACE_SECS
    ) -> None:
        """Drain for the post-compaction ``_kiro.dev/metadata`` notification.

        Returns as soon as a metadata frame carrying a real
        ``contextUsagePercentage`` is applied — a credits-only/empty metadata
        frame is consumed but does NOT end the drain, or the usage frame
        behind it would be stranded and the meter would fall back to the
        reset/unknown state. Gives up quietly at the grace deadline (the
        meter then self-corrects on the next turn's telemetry). Non-metadata
        notifications are buffered exactly like the main wait loop. Process
        death (``AcpError``) propagates — the outer ``wait_for_compaction``
        contract lets it, and swallowing it here would report a completed
        compaction on a dead runtime.
        """
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                msg = await self._read_message(timeout=remaining)
            except AcpError:
                raise
            except Exception:
                return
            if msg is None:
                continue
            if msg.is_method(METHOD_METADATA):
                self._track_metadata(msg)
                if (msg.params or {}).get("contextUsagePercentage") is not None:
                    return
                continue
            if msg.method and not msg.id:
                self._mcp_notifications.append(msg)


# --------------------------------------------------------------------------- #
# Compatibility facade. The module-level code for the stdio framing, the error
# taxonomy and its classifiers, the model catalog and the process-tree helpers lives
# in the owner modules imported above, and every name that moved stays readable as
# ``kiro_crew.acp.client.<name>``:
#
# * A moved name this module's own code reads, and that nothing patches through
#   this module, is an ordinary import above: the owner's object, bound here.
# * Every other moved name is FORWARDED. ``__getattr__`` reads it from its owner,
#   and ``_facade_mirror`` sends a write or delete there, so a patch of
#   ``kiro_crew.acp.client.<name>`` reaches the owner's own callers. A forwarded name
#   is absent from this module's namespace on purpose -- a binding here would
#   shadow the owner for every later read -- and this module's code reads it as
#   ``<owner>.<name>``. A public package import only moved code reads is forwarded to
#   the owner that reads it, for the same reason, so a test that reads or patches it
#   through this module reaches that reader; a standard-library or typing import only
#   moved code reads is not re-exported.
# * The modules the moved process-tree helpers probe with (``platform_compat``,
#   ``sys``, ``Path``, ``subprocess_mod``) stay bound here. Each helper listed in the
#   facade test's ``_SEAM_IMPORTS`` imports them from this module when it runs, so a
#   test that rebinds one here reaches the moved helper as it did before the move.
#
# ``test/test_acp_refactor_facade.py`` pins both halves: a name a test patches
# through this module is forwarded, and a forwarded name is never bound here.
# --------------------------------------------------------------------------- #
#: Owner module -> every name this module forwards to it.
_EXPORTS_BY_OWNER: dict[str, tuple[str, ...]] = {
    "kiro_crew.acp.transport_framing": (
        "_STDOUT_BUFFER_LIMIT",
        "_OVERSIZE_DRAIN_MAX_BYTES",
        "OversizeLineUnrecoverable",
        "_drain_oversize_line",
        "_RESPONSE_WRITE_BOUND_SECS",
        "_RESPONSE_WRITE_MIN_PROGRESS_BYTES",
        "_RESPONSE_WRITE_UNOBSERVABLE_BOUND_SECS",
        "_is_proactor_loop",
        "_level_is_progress_signal",
        "_pending_write_bytes",
        "await_under_no_progress_bound",
        "_release_if_acquired",
    ),
    "kiro_crew.acp.transport_errors": (
        "_COMPACTION_DETAIL_MAX_CHARS",
        "_COMPACTION_DETAIL_KEYS",
        "_COMPACTION_DETAIL_PLACEHOLDERS",
        "_COMPACTION_TRANSIENT_MARKERS",
        "_COMPACTION_WALK_MAX_DEPTH",
        "_walk_compaction_payload",
        "AcpConversationBindingMismatch",
        "AcpPermissionNeeded",
        "AcpRegistrationRateLimited",
        "AcpPromptBusy",
        "PiGateExtensionTampered",
        "_RE_MODEL_UNAVAILABLE",
        "_RE_MODEL_TEMP_UNAVAILABLE",
        "_RE_INVALID_MODEL_ID",
        "_RE_THROTTLE_NAMED",
        "_RE_THROTTLE_GENERIC",
        "_RE_AUTH",
        "_5XX_SEP",
        "_RE_5XX_NAMED",
        "_RE_5XX_STATUS",
        "_RE_CONNECTION",
        "_RE_5XX_HINT",
        "_RE_AUTH_STATUS",
        "_RE_SESSION_EXPIRED",
        "_RE_INVALID_BEARER",
        "_is_session_expired",
        "is_auth_failure_output",
        "_RE_SANDBOX_INIT_FAILURE",
        "sandbox_init_failure_for_runtime",
        "_RE_REGISTRATION_FAILED",
        "_RE_REGISTRATION_THROTTLE_MARK",
        "is_registration_throttle_output",
        "_RE_USAGE_LIMIT",
        "_RE_GENERATE_FAILED",
        "_RE_PROCESS_FAILED",
        "_RE_MALFORMED_REQUEST",
        "_RE_CONTEXT_WINDOW_OVERFLOW",
        "_RE_IMAGE_FORMAT_UNSUPPORTED",
        "_PROMPT_BUSY_RE",
        "_RE_STREAM_ENVELOPE",
        "_RE_TRAILING_REQ_ID",
        "_provider_detail",
        "_model_is_unentitled",
        "_is_transient_raw_error",
        "PROVIDER_ERROR_USAGE_LIMIT",
        "PROVIDER_ERROR_MALFORMED_REQUEST",
        "PROVIDER_ERROR_MODEL_UNAVAILABLE",
        "PROVIDER_ERROR_THROTTLE",
        "PROVIDER_ERROR_CREDENTIAL_PROPAGATION",
        "PROVIDER_ERROR_AUTH",
        "PROVIDER_ERROR_SESSION_EXPIRED",
        "PROVIDER_ERROR_CONNECTION",
        "PROVIDER_ERROR_HTTP_5XX",
        "PROVIDER_ERROR_UNKNOWN",
        "ProviderErrorClass",
        "classify_provider_error",
        "_auto_remedy",
        "_format_acp_error",
        "_rejected_model_from_error",
        "corroborate_launcher_refusal",
        "is_credential_propagation_delay",
        "ACP_BACKENDS_HOST_AUTH_CALLBACK",
        "LAUNCHER_EXIT_PREFIXES",
        "SANDBOX_LAYER_CREW",
        "SANDBOX_LAYER_HARNESS",
        "launcher_refusal",
        "sandbox_init_remediation",
    ),
    "kiro_crew.acp.runtime_models": (
        "advertised_model_ids",
        "resolve_pin_spelling",
        "resolve_pin_spelling_on",
        "catalog_row_would_drop",
        "resolve_usable_model",
        "_MODEL_SUBSTITUTION_ADVISORY_RE",
        "_MODEL_SUBSTITUTE_RE",
    ),
    "kiro_crew.acp.runtime_process_tree": (
        "_get_child_pids",
        "_direct_children",
        "_get_start_time",
        "_read_basename",
        "_capture_child_records",
        "_is_our_child",
        "_kill_escaped_children",
    ),
    "kiro_crew.acp.launch": (
        "_ADAPTER_INTERPRETERS",
        "_is_adapter_package_entry",
        "_named_by_override",
        "_adapter_spawn_label",
        "_normalize_exe_casing",
        "_mise_node_installs_dir",
        "_resolve_node_for_script",
        "_UNRESOLVED",
        "_vendored_acp_roots",
        "_resolve_node_adapter_argv",
        "_node_module_search_dirs",
        "_vendored_adapter_entry",
        "_self_served_bin_caches",
        "_resolution_generation",
        "_resolution_epoch",
        "bump_resolution_generation",
        "_resolve_self_served_bin",
        "_SANDBOX_PREFLIGHT_TIMEOUT",
        "_sandbox_preflight",
        "_run_preflight_bounded",
        "_unlink_readback_launcher",
        "_READBACK_STDERR_SCAN_CHARS",
        "_READBACK_FAULT_MAX_SHAPES",
        "_READBACK_FAULT_SHAPES",
        "_readback_stderr_diagnosis",
        "_readback_detail_with_diagnosis",
        "mise_data_dir",
        "RLIMIT_PROFILE_SESSION_HOST",
        "KIROCREW_SPAWNED_ENV",
        "KIROCREW_SPAWNED_VALUE",
    ),
    "kiro_crew.acp.harness.claude": (
        "CLAUDE_ACP_BIN",
        "CLAUDE_CODE_BIN",
        "CLAUDE_ACP_NPM_PKG",
        "_CLAUDE_ACP_PKG_ENTRY",
        "_CLAUDE_ACP_DEP_MARKER",
        "_claude_acp_argv_cache",
        "_resolve_vendored_claude_acp",
        "_resolve_claude_acp_bin",
        "_resolve_claude_code_executable",
        "_claude_adapter_installed_version",
    ),
    "kiro_crew.acp.harness.opencode": (
        "_OPENCODE_LAUNCH",
        "OPENCODE_BIN",
        "_ENV_OPENCODE_CONFIG_CONTENT",
        "OPENCODE_INSTALL_COMMAND",
        "_OPENCODE_CONFIG_READBACK_ARGS",
        "_OPENCODE_READBACK_TIMEOUT_S",
        "_opencode_readback_remedy",
        "_opencode_uniform_permission",
        "_opencode_rules_deny",
        "_opencode_config_mcp_server_names",
        "_opencode_config_mcp_servers_remedy",
        "_opencode_agent_permissions",
        "MAX_HARNESS_CONFIG_MCP_SERVERS",
        "MAX_HARNESS_TOOL_NAME_LEN",
        "opencode_rewrites_name",
    ),
    "kiro_crew.acp.harness.goose": (
        "_ENV_GOOSE_MODE",
        "_GOOSE_BUILTIN_ARG",
        "_GOOSE_BUILTIN_DEVELOPER",
    ),
    "kiro_crew.acp.harness.pi": (
        "PI_ACP_BIN",
        "PI_ACP_NPM_PKG",
        "_PI_ACP_PKG_ENTRY",
        "_PI_ACP_DEP_MARKER",
        "_ENV_PI_ACP_BIN",
        "PI_BIN",
        "PI_NPM_PKG",
        "_ENV_PI_ACP_PI_COMMAND",
        "PI_INSTALL_COMMAND",
        "_PI_RPC_ARGS",
        "_PI_EXTENSION_FLAG",
        "_PI_READBACK_REQUEST",
        "_PI_READBACK_TIMEOUT_S",
        "PI_MIN_VERSION",
        "_PI_NPM_PACKAGE_NAMES",
        "_PI_MANIFEST_SEARCH_DEPTH",
        "_ENV_PI_GATE_SESSION",
        "PI_GATE_EXTENSION_SHA256",
        "_pi_acp_argv_cache",
        "_pi_bin_cache",
        "_pi_gate_launcher_cache",
        "_resolve_pi_acp_bin",
        "_resolve_pi_bin",
        "_pi_installed_version",
        "_pi_version_issue",
        "pi_gate_extension_path",
        "_pi_gate_artifact_dir",
        "_pi_gate_extension_bytes",
        "_seal_pi_gate_extension",
        "_seal_gate_extension",
        "_publish_gate_artifact",
        "_pi_gate_launcher_body",
        "_ensure_pi_gate_launcher",
        "_pi_readback_remedy",
        "_same_file_spelling",
        "_same_file_spelling_all",
        "_pi_commands_from_readback",
        "agent_sdk",
    ),
    "kiro_crew.acp.harness.deepseek": (
        "_DSH_PATCH_FLAG",
        "_ENV_DSH_GATE_SESSION",
        "_ENV_DSH_GATE_MARKER",
        "_ENV_DSH_GATE_SCRUB_NAMES",
        "_DSH_GATE_SCRUB_CANARY_PREFIX",
        "_DSH_GATE_READBACK_TIMEOUT_S",
        "_DSH_GATE_MARKER_POLL_S",
        "_DSH_GATE_PROBE_EXIT_S",
        "_DSH_GATE_MARKER_MAX_BYTES",
        "DEEPSEEK_GATE_EXTENSION_SHA256",
        "_ENV_DEEPSEEK_PERMISSION_MODE",
        "DEEPSEEK_PERMISSION_MODE",
        "_DEEPSEEK_ENV_CHILD_SCRUB_CLASS",
        "_DEEPSEEK_ENV_NAME_GRAMMAR",
        "_DEEPSEEK_ENV_RESERVED_PREFIXES",
        "_DEEPSEEK_ENV_CREW_OWNED_NAMES",
        "deepseek_gate_extension_path",
        "_seal_deepseek_gate_extension",
        "_write_deepseek_gate_patch",
        "_validate_deepseek_env_mapping",
        "_deepseek_vault_env_names",
        "_deepseek_vault_env",
        "SECRET_URI_PREFIX",
        "resolve_secret_uris",
        "agent_env_scrub_prefixes",
    ),
}


def _index_exports() -> dict[str, str]:
    """Invert the owner table into forwarded name -> owner."""
    return {name: owner for owner, names in _EXPORTS_BY_OWNER.items() for name in names}


#: Forwarded name -> the dotted NAME of its owner, never the module object: the owner
#: is read from :data:`sys.modules` on each use, so a module purged and imported again
#: is seen at once instead of this table forwarding to the old copy. Bound empty
#: before the install below, which fills it with every name the mirror serves.
_EXPORTS: dict[str, str] = {}


def _owner(name: str) -> ModuleType:
    """Return the module that owns forwarded *name*, resolved on each access.

    ``importlib.import_module`` answers from :data:`sys.modules`, the one place a
    module is stored, so a purged or replaced owner is seen at once; and it waits on
    that module's import lock while its body is still running, where a bare
    ``sys.modules`` read would hand a second thread a half-built owner.
    """
    return importlib.import_module(_EXPORTS[name])


# Hidden from type checkers: mypy types every unknown attribute of a module that
# defines ``__getattr__`` as ``Any``, so a mistyped ``client.<name>`` would type-check.
# mypy sees the forwarded names through the ``TYPE_CHECKING`` imports below instead.
if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Read a name added to ``_EXPORTS`` after the install from its owner (:pep:`562`).

        ``_facade_mirror.MirroredModule`` answers every name the mirror serves, so this
        declines each of them: ``install`` refuses a resolver that answers one.
        """
        if name not in _EXPORTS or name in _MIRROR.served:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        return getattr(_owner(name), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


# The ``client_*`` owner modules hold code that was this module's own: the
# ``AcpClient`` members the class binds, and the module-level names re-imported above,
# which this module keeps binding. ``_facade_mirror`` keeps the whole patch surface: a
# name in ``_EXPORTS`` is forwarded to its one owner exactly as before (read, write and
# delete there; ``dir()`` lists it), and a write of any other name this module binds
# reaches every client owner binding the same object -- the readers one global of this
# module had before the split. ``surface`` is this module's own namespace, so an
# owner's private helper is never part of the facade. ``test/test_client_bindings.py``
# pins the reach.
#: The client's owner modules, lowest layer first.
_CLIENT_OWNERS: tuple[str, ...] = (
    "kiro_crew.acp.client_timeouts",
    "kiro_crew.acp.client_spawn_env",
    "kiro_crew.acp.client_permissions",
    "kiro_crew.acp.client_claude_session",
    "kiro_crew.acp.client_claude_seed",
    "kiro_crew.acp.client_seed_ownership",
    "kiro_crew.acp.client_model_selection",
)
_MIRROR = _facade_mirror.install(
    sys.modules[__name__],
    _CLIENT_OWNERS,
    excluded=(),
    forwards=_index_exports(),
    forward_reads="import_module",
    surface=frozenset(globals()),
)
_EXPORTS.update(_MIRROR.served)

# Installed last, so the forwarding is live for every caller but never runs while this
# module is still binding its own names.
sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

# ``from kiro_crew.acp.client import *`` binds only the names this list holds: nothing
# can enumerate what ``__getattr__`` would serve, so a forwarded name reaches a star
# importer only by being listed here. The list is DERIVED from what this module binds
# plus the forwarding table, so it is not a third list of names to keep in step.
__all__ = sorted(name for name in set(globals()) | set(_EXPORTS) if not name.startswith("_"))

if TYPE_CHECKING:  # the forwarded names, visible to type checkers and IDEs
    from kiro_crew.acp.harness.claude import (  # noqa: F401
        _CLAUDE_ACP_DEP_MARKER,
        _CLAUDE_ACP_PKG_ENTRY,
        CLAUDE_ACP_BIN,
        CLAUDE_ACP_NPM_PKG,
        CLAUDE_CODE_BIN,
        _claude_acp_argv_cache,
        _claude_adapter_installed_version,
        _resolve_claude_acp_bin,
        _resolve_claude_code_executable,
        _resolve_vendored_claude_acp,
    )
    from kiro_crew.acp.harness.deepseek import (  # noqa: F401
        _DEEPSEEK_ENV_CHILD_SCRUB_CLASS,
        _DEEPSEEK_ENV_CREW_OWNED_NAMES,
        _DEEPSEEK_ENV_NAME_GRAMMAR,
        _DEEPSEEK_ENV_RESERVED_PREFIXES,
        _DSH_GATE_MARKER_MAX_BYTES,
        _DSH_GATE_MARKER_POLL_S,
        _DSH_GATE_PROBE_EXIT_S,
        _DSH_GATE_READBACK_TIMEOUT_S,
        _DSH_GATE_SCRUB_CANARY_PREFIX,
        _DSH_PATCH_FLAG,
        _ENV_DEEPSEEK_PERMISSION_MODE,
        _ENV_DSH_GATE_MARKER,
        _ENV_DSH_GATE_SCRUB_NAMES,
        _ENV_DSH_GATE_SESSION,
        DEEPSEEK_GATE_EXTENSION_SHA256,
        DEEPSEEK_PERMISSION_MODE,
        SECRET_URI_PREFIX,
        _deepseek_vault_env,
        _deepseek_vault_env_names,
        _seal_deepseek_gate_extension,
        _validate_deepseek_env_mapping,
        _write_deepseek_gate_patch,
        agent_env_scrub_prefixes,
        deepseek_gate_extension_path,
        resolve_secret_uris,
    )
    from kiro_crew.acp.harness.goose import (  # noqa: F401
        _ENV_GOOSE_MODE,
        _GOOSE_BUILTIN_ARG,
        _GOOSE_BUILTIN_DEVELOPER,
    )
    from kiro_crew.acp.harness.opencode import (  # noqa: F401
        _ENV_OPENCODE_CONFIG_CONTENT,
        _OPENCODE_CONFIG_READBACK_ARGS,
        _OPENCODE_LAUNCH,
        _OPENCODE_READBACK_TIMEOUT_S,
        MAX_HARNESS_CONFIG_MCP_SERVERS,
        MAX_HARNESS_TOOL_NAME_LEN,
        OPENCODE_BIN,
        OPENCODE_INSTALL_COMMAND,
        _opencode_agent_permissions,
        _opencode_config_mcp_server_names,
        _opencode_config_mcp_servers_remedy,
        _opencode_readback_remedy,
        _opencode_rules_deny,
        _opencode_uniform_permission,
        opencode_rewrites_name,
    )
    from kiro_crew.acp.harness.pi import (  # noqa: F401
        _ENV_PI_ACP_BIN,
        _ENV_PI_ACP_PI_COMMAND,
        _ENV_PI_GATE_SESSION,
        _PI_ACP_DEP_MARKER,
        _PI_ACP_PKG_ENTRY,
        _PI_EXTENSION_FLAG,
        _PI_MANIFEST_SEARCH_DEPTH,
        _PI_NPM_PACKAGE_NAMES,
        _PI_READBACK_REQUEST,
        _PI_READBACK_TIMEOUT_S,
        _PI_RPC_ARGS,
        PI_ACP_BIN,
        PI_ACP_NPM_PKG,
        PI_BIN,
        PI_GATE_EXTENSION_SHA256,
        PI_INSTALL_COMMAND,
        PI_MIN_VERSION,
        PI_NPM_PKG,
        _ensure_pi_gate_launcher,
        _pi_acp_argv_cache,
        _pi_bin_cache,
        _pi_commands_from_readback,
        _pi_gate_artifact_dir,
        _pi_gate_extension_bytes,
        _pi_gate_launcher_body,
        _pi_gate_launcher_cache,
        _pi_installed_version,
        _pi_readback_remedy,
        _pi_version_issue,
        _publish_gate_artifact,
        _resolve_pi_acp_bin,
        _resolve_pi_bin,
        _same_file_spelling,
        _same_file_spelling_all,
        _seal_gate_extension,
        _seal_pi_gate_extension,
        agent_sdk,
        pi_gate_extension_path,
    )
    from kiro_crew.acp.launch import (  # noqa: F401
        _ADAPTER_INTERPRETERS,
        _READBACK_FAULT_MAX_SHAPES,
        _READBACK_FAULT_SHAPES,
        _READBACK_STDERR_SCAN_CHARS,
        _SANDBOX_PREFLIGHT_TIMEOUT,
        _UNRESOLVED,
        KIROCREW_SPAWNED_ENV,
        KIROCREW_SPAWNED_VALUE,
        RLIMIT_PROFILE_SESSION_HOST,
        _adapter_spawn_label,
        _is_adapter_package_entry,
        _mise_node_installs_dir,
        _named_by_override,
        _node_module_search_dirs,
        _normalize_exe_casing,
        _readback_detail_with_diagnosis,
        _readback_stderr_diagnosis,
        _resolution_epoch,
        _resolution_generation,
        _resolve_node_adapter_argv,
        _resolve_node_for_script,
        _resolve_self_served_bin,
        _run_preflight_bounded,
        _sandbox_preflight,
        _self_served_bin_caches,
        _unlink_readback_launcher,
        _vendored_acp_roots,
        _vendored_adapter_entry,
        bump_resolution_generation,
        mise_data_dir,
    )
    from kiro_crew.acp.runtime_models import (  # noqa: F401
        _MODEL_SUBSTITUTE_RE,
        _MODEL_SUBSTITUTION_ADVISORY_RE,
        advertised_model_ids,
        catalog_row_would_drop,
        resolve_pin_spelling,
        resolve_pin_spelling_on,
        resolve_usable_model,
    )
    from kiro_crew.acp.runtime_process_tree import (  # noqa: F401
        _capture_child_records,
        _direct_children,
        _get_child_pids,
        _get_start_time,
        _is_our_child,
        _kill_escaped_children,
        _read_basename,
    )
    from kiro_crew.acp.transport_errors import (  # noqa: F401
        _5XX_SEP,
        _COMPACTION_DETAIL_KEYS,
        _COMPACTION_DETAIL_MAX_CHARS,
        _COMPACTION_DETAIL_PLACEHOLDERS,
        _COMPACTION_TRANSIENT_MARKERS,
        _COMPACTION_WALK_MAX_DEPTH,
        _PROMPT_BUSY_RE,
        _RE_5XX_HINT,
        _RE_5XX_NAMED,
        _RE_5XX_STATUS,
        _RE_AUTH,
        _RE_AUTH_STATUS,
        _RE_CONNECTION,
        _RE_CONTEXT_WINDOW_OVERFLOW,
        _RE_GENERATE_FAILED,
        _RE_IMAGE_FORMAT_UNSUPPORTED,
        _RE_INVALID_BEARER,
        _RE_INVALID_MODEL_ID,
        _RE_MALFORMED_REQUEST,
        _RE_MODEL_TEMP_UNAVAILABLE,
        _RE_MODEL_UNAVAILABLE,
        _RE_PROCESS_FAILED,
        _RE_REGISTRATION_FAILED,
        _RE_REGISTRATION_THROTTLE_MARK,
        _RE_SANDBOX_INIT_FAILURE,
        _RE_SESSION_EXPIRED,
        _RE_STREAM_ENVELOPE,
        _RE_THROTTLE_GENERIC,
        _RE_THROTTLE_NAMED,
        _RE_TRAILING_REQ_ID,
        _RE_USAGE_LIMIT,
        ACP_BACKENDS_HOST_AUTH_CALLBACK,
        LAUNCHER_EXIT_PREFIXES,
        PROVIDER_ERROR_AUTH,
        PROVIDER_ERROR_CONNECTION,
        PROVIDER_ERROR_CREDENTIAL_PROPAGATION,
        PROVIDER_ERROR_HTTP_5XX,
        PROVIDER_ERROR_MALFORMED_REQUEST,
        PROVIDER_ERROR_MODEL_UNAVAILABLE,
        PROVIDER_ERROR_SESSION_EXPIRED,
        PROVIDER_ERROR_THROTTLE,
        PROVIDER_ERROR_UNKNOWN,
        PROVIDER_ERROR_USAGE_LIMIT,
        SANDBOX_LAYER_CREW,
        SANDBOX_LAYER_HARNESS,
        AcpConversationBindingMismatch,
        AcpPermissionNeeded,
        AcpPromptBusy,
        AcpRegistrationRateLimited,
        PiGateExtensionTampered,
        ProviderErrorClass,
        _auto_remedy,
        _format_acp_error,
        _is_session_expired,
        _is_transient_raw_error,
        _model_is_unentitled,
        _provider_detail,
        _rejected_model_from_error,
        _walk_compaction_payload,
        classify_provider_error,
        corroborate_launcher_refusal,
        is_auth_failure_output,
        is_credential_propagation_delay,
        is_registration_throttle_output,
        launcher_refusal,
        sandbox_init_failure_for_runtime,
        sandbox_init_remediation,
    )
    from kiro_crew.acp.transport_framing import (  # noqa: F401
        _OVERSIZE_DRAIN_MAX_BYTES,
        _RESPONSE_WRITE_BOUND_SECS,
        _RESPONSE_WRITE_MIN_PROGRESS_BYTES,
        _RESPONSE_WRITE_UNOBSERVABLE_BOUND_SECS,
        _STDOUT_BUFFER_LIMIT,
        OversizeLineUnrecoverable,
        _drain_oversize_line,
        _is_proactor_loop,
        _level_is_progress_signal,
        _pending_write_bytes,
        _release_if_acquired,
        await_under_no_progress_bound,
    )
