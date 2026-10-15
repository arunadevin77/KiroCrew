"""Each split facade keeps resolving every public name it bound before its owners moved out.

A facade whose rules moved into owner modules stays the import path callers, tests and
docs use, so a public name it bound before the split -- one it defined, or one it
imported from the rest of ``kiro_crew`` -- is part of its surface even when no in-repo
caller reads it today. Standard-library, typing and third-party imports and private
names are not surface. The inventories below are frozen at the split: a name leaves one
only when the facade deliberately stops offering it.

Each name a facade binds again by import is listed with the module that holds it, and
a fresh interpreter checks the facade's binding is that module's own object, so no
reload or eviction elsewhere in the suite can decide the result.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import kiro_crew
from kiro_crew.subprocess_utf8 import UTF8_TEXT

#: Facade -> the public project names it bound before its split; each must keep resolving.
_BASE_PUBLIC_NAMES: dict[str, frozenset[str]] = {
    "kiro_crew.acp.client": frozenset("""
        ACP_BACKENDS_ADVERTISED_MODEL_SELECTION ACP_BACKENDS_HARNESS_OWNED_SESSIONS
        ACP_BACKENDS_HOST_AUTH_CALLBACK ACP_BACKENDS_INLINE_COMPACTION
        ACP_BACKENDS_INTERNAL_SANDBOX ACP_BACKENDS_LOAD_WITHOUT_MODES
        ACP_BACKENDS_MEMBER_DISPATCH ACP_BACKENDS_MEMBER_PANEL ACP_BACKENDS_META_IDENTITY
        ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION
        ACP_BACKENDS_POD_HOME_REMAP ACP_BACKENDS_RESUME_WITHOUT_LOAD
        ACP_BACKENDS_SEED_LOCAL_SETTINGS ACP_BACKENDS_SESSION_MCP_ARRAY ACP_BACKENDS_STEER
        ACP_BACKENDS_STRUCTURED_REFUSAL ACP_BACKEND_CLAUDE ACP_BACKEND_CODEX
        ACP_BACKEND_DEEPSEEK ACP_BACKEND_GOOSE ACP_BACKEND_KAS ACP_BACKEND_KIRO
        ACP_BACKEND_LAUNCH ACP_BACKEND_NODE_ADAPTER_PACKAGES ACP_BACKEND_OPENCODE
        ACP_BACKEND_PI ACP_BACKEND_PROCESS_NAMES ACP_CLIENT_CAPABILITIES AcpAuthRequired
        AcpClient AcpError AcpEvent AcpModelUnavailable AcpPermissionNeeded AcpProcessDied
        AcpPromptBusy AcpPromptStats AcpRegistrationRateLimited AcpSandboxInitFailed
        AcpTimeoutError AcpToolGateUnroutable BoundWorkspaceMismatch CLAUDE_ACP_BIN
        CLAUDE_ACP_NPM_PKG CLAUDE_CODE_BIN CLIENT_NAME CLIENT_VERSION CODEX_ACP_BIN
        CODEX_ACP_NPM_PKG COMPACT_WAIT_TIMEOUT_SECS CREDENTIAL_POINTER_ENV_VARS ChildRecord
        DEEPSEEK_GATE_EXTENSION_SHA256 DEEPSEEK_PERMISSION_MODE DEFAULT_MODEL
        DRAIN_YIELD_AFTER_S DerivedSpecSnapshot DerivedSpecStale EVENT_AGENT_SWITCHED
        EVENT_CLEAR_STATUS EVENT_COMPACTION_STATUS EVENT_COMPLETE EVENT_MCP_OAUTH_REQUEST
        EVENT_MCP_SERVER_INITIALIZED EVENT_MCP_SERVER_INIT_FAILURE EVENT_SUBAGENT_ACTIVITY
        EVENT_SUBAGENT_LIST EVENT_TEXT_CHUNK EVENT_THINKING_CHUNK EVENT_TOOL_CALL
        EVENT_TOOL_CALL_UPDATE EVENT_TOOL_RESULT EVIDENCE_SAMPLING ForkGovernanceUnresolved
        GOOSE_VERIFIED_VERSION_PREFIX HOOK_EVENT_POST_TOOL_USE IDENTITY_STORE_ROOTS
        IDENTITY_STORE_ROOT_ENV_VARS JSONRPC_METHOD_NOT_FOUND JsonRpcMessage JsonRpcRequest
        KIROCREW_SPAWNED_ENV KIROCREW_SPAWNED_VALUE KIRO_CLI_BIN KIRO_CLI_SUBCMD
        KNOWN_SESSION_UPDATES LAUNCHER_EXIT_PREFIXES LivenessOracle MCP_ROSTER_COMPLETE_NOTE
        METHOD_AGENT_SWITCHED METHOD_CANCEL METHOD_CLEAR_STATUS METHOD_COMMANDS_EXECUTE
        METHOD_COMPACTION_STATUS METHOD_INITIALIZE METHOD_KIRO_SESSION_UPDATE
        METHOD_MCP_OAUTH_REQUEST METHOD_MCP_SERVER_INITIALIZED
        METHOD_MCP_SERVER_INIT_FAILURE METHOD_METADATA METHOD_PROMPT
        METHOD_REQUEST_PERMISSION METHOD_SESSION_LOAD METHOD_SESSION_NEW
        METHOD_SESSION_RESUME METHOD_SESSION_UPDATE METHOD_SET_MODE METHOD_SET_MODEL
        METHOD_SUBAGENT_LIST_UPDATE MIRRORS MODEL_CONFIG_ID McpSessionReport
        NODE_ADAPTER_ENTRY_SEGMENTS OPENCODE_BIN OPENCODE_INSTALL_COMMAND
        OPTION_ALLOW_ALWAYS OPTION_ALLOW_ONCE OUTCOME_CANCELLED OUTCOME_SELECTED
        OversizeLineUnrecoverable PI_ACP_BIN PI_ACP_NPM_PKG PI_ACP_VERIFIED_VERSION PI_BIN
        PI_GATE_EXTENSION_SHA256 PI_INSTALL_COMMAND PI_MIN_VERSION PI_NPM_PKG
        PROTOCOL_VERSION PROTOCOL_VERSION_CLAUDE PROTOCOL_VERSION_DEEPSEEK
        PROTOCOL_VERSION_GOOSE PROTOCOL_VERSION_OPENCODE PROTOCOL_VERSION_PI
        PROVIDER_ERROR_AUTH PROVIDER_ERROR_CONNECTION PROVIDER_ERROR_CREDENTIAL_PROPAGATION
        PROVIDER_ERROR_HTTP_5XX PROVIDER_ERROR_MALFORMED_REQUEST
        PROVIDER_ERROR_MODEL_UNAVAILABLE PROVIDER_ERROR_SESSION_EXPIRED
        PROVIDER_ERROR_THROTTLE PROVIDER_ERROR_UNKNOWN PROVIDER_ERROR_USAGE_LIMIT
        PiGateExtensionTampered ProviderErrorClass RLIMIT_PROFILE_SESSION_HOST
        SANDBOX_LAYER_CREW SANDBOX_LAYER_HARNESS SECRET_URI_PREFIX
        STOP_REASON_COMPACTION_FAILED STOP_REASON_END_TURN STUB_SESSION_TOKEN_ENV
        TERMINAL_TOOL_STATUSES UPDATE_AGENT_MESSAGE_CHUNK UPDATE_AGENT_THOUGHT_CHUNK
        UPDATE_CONFIG_OPTION UPDATE_CURRENT_MODE UPDATE_TOOL_CALL UPDATE_USAGE
        VERDICT_WORKING acp_tool_gate advertised_model_ids agent_env_scrub_prefixes
        agent_scratch agent_sdk agent_spec_snapshot agent_version_from_init
        apply_pod_bundle_spawn apply_windows_resource_ceiling
        assert_voice_runtime_outside_agent_workspace atomic_write attach_stub_session_token
        augmented_path await_under_no_progress_bound bind_voice_safe_agent_workspace_async
        browser_session_env browser_socket_env build_permission_event build_prompt_blocks
        bump_resolution_generation catalog_row_would_drop cgroup_scope_argv
        classify_provider_error classify_tool_call codex_acp_not_found_message
        compaction_failure_detail compaction_failure_is_transient config_dir
        consult_offloaded corroborate_launcher_refusal create_subprocess_limited
        deepseek_gate_extension_path delegated_workspace_exposes_sealed_target
        derive_edit_diff describe_search_path effort_config_option_id
        effort_config_option_value ensure_agent_materialized error_is_refusal_terminal
        extract_tool_purpose finish_suspended_spawn fire_tool_hooks format_command_result
        gate_envelope get_global_hook_store get_global_skill_read_observer host_auth
        inject_xdist_auto_cap injection_server_names is_auth_failure_output
        is_credential_propagation_delay is_registration_throttle_output
        is_sandbox_init_failure_output is_sensitive_path kiro_cli_not_found_message
        kiro_sessions_dir known_kiro_cli_dirs launch_for launcher_refusal
        log_unrenderable_content logger make_unified_diff meta_builtin_server_names
        mint_stub_session_token mirror_for mise_data_dir model_is_unusable
        model_refusal_phrase model_registry model_registry_namespace model_scope
        note_tool_call_started overlay_project_scope parse_claude_compaction_notice
        parse_codex_compaction_update parse_prompt_token_usage parse_refusal
        parse_session_modes parse_slash_command parse_usage_cost parse_usage_update
        permission_floor pi_gate_extension_path pick_served_default pinned_fs
        platform_compat pooled_session_servers prompt_timeout_for_ceiling record_frame
        record_tool_call_finished redact_credentials redact_exfiltration_urls
        redact_log_via_context redact_text registration_rate_limited_error
        registration_throttle_line release_bound_agent_workspace require_fork_governance
        require_fresh_derived_spec require_unchanged_derived_spec
        resolve_bound_session_workspace resolve_kiro_cli resolve_krb5_ccname
        resolve_pin_spelling resolve_prompt_timeout resolve_secret_uris resolve_usable_model
        response_write_window_secs sandbox_init_failure sandbox_init_failure_for_runtime
        sandbox_init_remediation schedule_claim schedule_session_token_publish
        scrub_agent_subprocess_env seed_provenance sel sel_module session_mcp_deny_rules
        subprocess_executor tool_call_content_text tool_output_fingerprints
        warn_unresolved_server_refs wrap_argv wrap_argv_async wrapped_by_crew_sandbox
        write_notification_best_effort write_response_frame_bounded
        """.split()),
    "kiro_crew.cron": frozenset("""
        CHANNEL_MAX_LEN CRON_FIRES CronFolderLookup CronHistoryStore CronJob
        CronLoopSafetyError CronPendingMismatch CronRunRecord CronSchedule CronService
        CronStoreBusy CronStoreUnreadable KiroCrewConfig MAX_CRON_MESSAGE MAX_SHORT_STRING
        ProcessHandle add_handle admission_check agent_sequence_dispatches
        authorize_runtime_kill bind_cron_memory build_cron_session_context
        compute_next_run_ts config_dir cron_expr_matches cron_gate_budget cron_inflight
        cron_job_id_from_session_key cron_owner_matches cron_script
        cron_session_key_is_stable cron_store_lock data_home dispatched_agents_from_disk
        effective_wake_budget emit_counter enabled_count_from_disk ending_fence
        env_flag_enabled failure_name format_schedule get_local_tz is_valid_skip_date
        is_valid_timezone job_agent_names_from_disk job_pause_state_from_disk join_failures
        kill_each kill_set kill_verified_process load_cron_folders logger
        lookup_cron_folder_id parse_time_string platform_compat process_handle_of
        process_survived_async published_config_timezone referenced_skill_names
        release_teardown_lease resolve_cron_memory sel shutdown_event spawn_in_flight
        stall_attribution subprocess_executor teardown_barriers teardown_capture
        unhealthy_jobs_from_disk validate_cron_expr with_kill_failure
        """.split()),
    "kiro_crew.autonudge": frozenset("""
        ADDRESSING_FIELDS APPROVAL_STALL_REASON AUTONUDGE_STOP_REASON AutoNudgeService
        AutoNudgeStaleBaseline AutoNudgeStoreUnvetted MANUAL_STOP_REASON MAX_BANNER_CHARS
        MONITOR_BUSY_RETRY_SECS MONITOR_COMPLETION_EVIDENCE_TIMEOUT_SECS
        MONITOR_STATE_VERSION MONITOR_STOP_APPROVAL_STALL
        MONITOR_STOP_COMPLETION_UNAVAILABLE MONITOR_STOP_SESSION_CLOSE
        MONITOR_STOP_SESSION_UNAVAILABLE MONITOR_STOP_UNSUPPORTED_VERSION MONITOR_STOP_USER
        MONITOR_TERMINAL_REASON MonitorActionCompletion MonitorActionDisposition
        MonitorBudgets MonitorCreationSurface MonitorDecision MonitorDispatchResult
        MonitorObservationStatus MonitorOutcome MonitorProbeResult MonitorState
        MonitorUpdateConflict MonitorVerdict NUDGE_RENEW_DUE_SHARE NudgeAdmissionRefused
        NudgeLoop PlatformCompositionError REVIEW_READY SENTINEL_DROPPED_REASON
        SESSION_START_FAILURE_REASON STRUCTURAL_TERMINAL_REASON autonudge_stop_log
        binding_key_for config_dir data_home decide_monitor enabled fsync_dir get_instance
        infer_monitor infer_subject irq is_channel_key is_sensitive_path
        is_structured_monitor_loop is_unattempted_probe kind_supports_objective legacy_home
        logger loop_subject monitor_budget_reason monitor_stall_reason
        monitor_state_from_dict monitor_state_to_dict new_goal_token nudge_cycle_header
        platform_compat probes quarantine_monitor_state redact_credentials
        redact_exfiltration_urls redact_log_via_context redact_store_value
        redact_via_context repair_sentinel_path replace_with_retry
        retained_outcome_blocks_rearm runtime_budget_exceeded scrub_loop_text
        scrubbed_judge_spec shutdown_event stamp_monitor_alerted
        structured_monitor_binding_key_for targets terminal_notification_delivery_matches
        validate_runtime_secs validation
        """.split()),
    "kiro_crew.apps.builtins.ops_mission_control.backend.routes": frozenset("""
        APP_NAME CLAIMED_BY_OPERATOR CronStoreUnreadable DEFAULT_VERIFY_AFTER_SECS
        EXPIRING_ACTIONS Handler LedgerEntry MAX_INCIDENTS_RESPONSE MODE_ORDER STATE_FIRING
        STATE_OK STATE_SUPPRESSED STATUS_NEEDS_HUMAN Signal UnknownFieldError VALID_ACTIONS
        VERIFIABLE_ACTIONS VERIFY_NOT_CHECKABLE VERIFY_PENDING canonical_slot_key companion
        delete_secret describe_secrets dispatch get_registry handover is_app_enabled ledger
        logger merge_provider_config notify_out policy_store provider_config put_secret
        redact_tokens redact_via_context register_routes resolve_silence_secs rotation sel
        set_top_level slack_out slot_watch store utc_now_iso webhook_mod
        """.split()),
    "kiro_crew.apps.builtins.issue_radar.backend.routes": frozenset("""
        GhCliError GhInvalidInputError GhPermissionError GhSetupError KiroCrewConfig
        LIST_POLL_MAX_STALENESS_SEC LoopBoundLock MAX_ASSIGNEES MAX_ITEM_NUMBER MAX_RUN_ID
        PrSearchError github_client is_app_enabled logger normalize_ui_language_tag
        pipeline_routes provider register_routes sel store ui_language_tag watch
        """.split()),
    "kiro_crew.mcp_gateway.gatewayd": frozenset("""
        Admission Backend BackendGone BackendPool BackendUnavailable CANONICAL_TEMP_KEYS
        CONTROL_PLANE_BACKENDS CallerContext CircuitBreaker DEFAULT_CAPACITY DEFAULT_CEILING
        DEFAULT_FLOOR DRAIN_DEADLINE_SECS DRAIN_SECS HostBudget HostBudgetExhausted
        HostBudgetLimits HostCharge HotKeyStore INTERNAL_STUB_PREFIXES IS_WINDOWS
        KIROCREW_BIN_MCP_SERVERS KiroCrewConfig OUTCOME_FAILURE OUTCOME_NEUTRAL OnQueued
        POOL_SHUTDOWN_SECS Permit PoolAtCapacity PoolKey READ_BUFFER_LIMIT_BYTES
        REGISTERED_CAPABILITIES REJECT_CLASS_CAPACITY REJECT_CLASS_COMPAT
        REJECT_CLASS_ISOLATION SECRET_URI_PREFIX STUB_KEEPALIVE_TYPE SecurityEventLog
        SpawnGate SpawnGateClosed SpawnGateTimeout TargetResolver apps_sweep_spool
        classify_declared_temp_env cleanup_old_spill_files code_fingerprint
        configure_default_executor credwatch declared_temp_refusal_reasons
        default_hot_keys_path env_sidecar_dir env_sidecar_name env_target_resolver
        format_declared_temp_refusals forward_declared_env_enabled get_recorder hash_command
        hash_effective_env hazards is_credential_env_key kiro_crew launch_approval logger
        main maintenance_executor mcp_search_path new_tenant_nonce non_secret_env
        pool_identity_env_keys prewarm_from_payloads records_dir redact
        resolvable_target_stems resolve_limits resolve_once_resolver resolve_overlay_dir
        resolve_peer_identity resolve_secret_uris resolved_launch run_gatewayd socketsec
        spawn_backend spec_path_key stub_fallback_counts subprocess_executor
        sweep_all_backend_tmp tool_surface transport warm_backend warm_code_fingerprint
        """.split()),
    "kiro_crew.crew_log.projection": frozenset("""
        APPROVAL_TYPES CODE_BAD_DATA CONTEXT_ORDINAL_MEMO_LIMIT CONTEXT_SOURCES_PER_TURN_LIMIT
        CONTEXT_TURNS_LIMIT CREDIT_SOURCES Checkpoint CrewLog CrewLogError
        DASHBOARD_AGENTIC_ENTRY_TYPE DASHBOARD_CODE_LIMIT DASHBOARD_FIELD_LIMIT
        DASHBOARD_FOLD_NAME DASHBOARD_REASON_LIMIT DASHBOARD_REFUSED_ENTRY_TYPE
        DASHBOARD_TYPE_LIMIT DASHBOARD_VALUE_BYTES DASHBOARD_VALUE_LIMIT
        DEFAULT_SESSION_FOLD_CACHE_BYTES DEFAULT_SLOT_FOLD_CACHE_BYTES EAGER_FOLD_NAMES
        EAGER_SESSION_FOLD_NAMES EAGER_SLOT_FOLD_NAMES Entry FOLD_NAMES ID_LIMIT
        INTERNAL_PROJECTION_NAMES KIND_SESSION KNOWN_TYPES LAZY_FOLD_REASONS
        LEDGER_ARTIFACT_KEY_LIMIT LEDGER_ARTIFACT_LIMIT LEDGER_ENTRY_TYPE LEDGER_EVENT_KINDS
        LEDGER_EVENT_LIMIT LEDGER_FOLD_NAME LEDGER_PHASE_LIMIT LEDGER_SCHEMA_VERSION
        LEDGER_TERMINAL_PHASES LEDGER_TEXT_LIMIT LEDGER_TRIED_LIMIT MAX_ENTRY_BYTES
        MISTAKES_FOLD_NAME MISTAKE_CORRECTED_CODE MISTAKE_CORRECTION_LIMIT MISTAKE_GROUP_LIMIT
        MODEL_LIMIT OPEN_LIST_LIMIT OPEN_RETAIN_LIMIT OUTLINE_PROMPT_CHARS OUTLINE_REPLY_CHARS
        OUTLINE_TURN_LIMIT OUTLINE_TYPES OVERSIZE_SLOT_CELL_LIMIT OWNER_SERVED_SLOT_PROJECTION
        OpenedRecord PANEL_CREW_KEY_LIMIT PANEL_ENTRY_TYPE PANEL_FOLD_NAME PANEL_HISTORY_LIMIT
        PANEL_OWNER_LIMIT PANEL_SCHEMA_VERSION PANEL_TEMPLATE_LIMIT PANEL_TITLE_LIMIT
        PHASE_SESSION_START PROJECTION_NAMES Projection ProjectionRegistry RADAR_CI_BOUNDS
        RADAR_CI_KEYS RADAR_CLEARABLE_FIELDS RADAR_CREW_LEVEL_EVENT_KIND
        RADAR_DEFAULT_SKIP_SCOPE RADAR_EDITING_PHASES RADAR_ENTRY_TYPE RADAR_EVENT_KINDS
        RADAR_EVENT_LIMIT RADAR_ITEM_LIMIT RADAR_LABELS_LIMIT RADAR_NUMBER_BOUNDS RADAR_PHASES
        RADAR_PHASE_LINE_LIMIT RADAR_SCHEMA_VERSION RADAR_SKIP_LIMIT RADAR_SKIP_SCOPES
        RADAR_TERMINAL_PHASES RADAR_TEXT_LIMIT RADAR_TRIED_LIMIT ReadCursor ResumePoint
        SERVERS_PER_TOOL_LIMIT SESSION_ENTRY_TYPES SESSION_FOLD_CACHE_BYTES_ENV
        SESSION_FOLD_NAMES SLOT_FOLD_CACHE_BYTES_ENV SLOT_PROJECTION_NAMES SUBAGENT_TYPES
        Savepoint SessionProjections TEXT_LIMIT TIMELINE_LIMIT TIMELINE_TYPES TOKEN_DIMENSIONS
        TOOL_NAME_LIMIT TOOL_TYPES TREE_PROJECTION_NAMES USAGE_TYPES WORKSTREAMS_BOARD_LIMIT
        WORKSTREAMS_DECISION_LIMIT WORKSTREAMS_EVENT_BUDGET WORKSTREAMS_EVENT_LIMIT
        WORKSTREAMS_FOLD_NAME WORKSTREAMS_SERIES_LIMIT WORKSTREAMS_SPENDER_LIMIT
        WORKSTREAMS_TASK_LIMIT WORKSTREAMS_TYPES WORKSTREAMS_UNIT_LIMIT WORKTREE_BOARD_LIMIT
        WORKTREE_FOLD_NAME WORKTREE_GOAL_CHARS WORKTREE_OWNER_LIMIT WORK_BLOCKED_REASONS
        WORK_CLOSED_WINDOW WORK_CONDUCTOR_FIELDS WORK_ENTRY_TYPE WORK_EVENT_LIMIT
        WORK_EVENT_TEXT_LIMIT WORK_ITEM_LIMIT WORK_PARKED_LIMIT WORK_STORED_ITEM_LIMIT
        WORK_TOMBSTONE_LIMIT WarmSession WorkBoardForgotten WorkBoardItem WorkBoardView advance
        attribute_seq data_home empty_session fold fold_approvals fold_citations fold_session
        fold_session_warm fold_slot fold_slot_checkpoint fold_slot_warm fold_slot_warm_revised
        fold_state_version fold_status fold_timeline fold_tools fold_tree_checkpoint fold_usage
        forget_session_folds forget_slot_folds initial log_exception_text log_origin log_rank_of
        logger open_session_log projection_of radar_ci_state read_projection
        read_slot_projection read_tree_projection require_name segment_paths
        session_fold_cache_bytes session_fold_memo_bytes session_fold_state_bytes session_ledger
        session_units_by_slot session_units_for_slot slot_fold_cache_bytes slot_fold_cell_bytes
        slot_fold_over_ceiling slot_fold_row_bytes slot_of_session state_is_serializable
        tree_fold_units unit_header_created_at unit_opened_previous units_in_succession
        work_slots_naming_board
        """.split()),
    "kiro_crew.slack.gateway": frozenset("""
        APPROVAL_INTERACTIVE APPROVAL_STALL_REASON AUTO_EFFECT_INSTALL AUTO_EFFECT_MANDATORY
        AUTO_EFFECT_NOTIFY AUTO_ROUTE_GIT AUTO_ROUTE_PROVIDER AUTO_ROUTE_WHEEL AcpError
        AcpProcessDied AgentTurnUsage AutoNudgeService AutoUpdateEffect BOOT_RESTART_HINT
        CHANNEL_ID_RE CHANNEL_SESSION_NAMESPACES CHAT_TYPE_DIRECT CHECK_SUCCEEDED
        CHECK_UNCHECKED CONSECUTIVE_FAILURE_REASON CRED_DISCORD_BOT_TOKEN CRED_FEISHU_APP_ID
        CRED_FEISHU_APP_SECRET CRED_MICROSOFT_APP_ID CRED_MICROSOFT_APP_PASSWORD
        CRED_MICROSOFT_APP_TENANT_ID CRED_OWNER_ID CRED_SLACK_APP_TOKEN CRED_SLACK_BOT_TOKEN
        CRED_TELEGRAM_BOT_TOKEN CRED_WEBEX_BOT_TOKEN CRED_WECOM_BOT_ID CRED_WECOM_SECRET
        CRED_WEIXIN_TOKEN CRON_NOTIFICATION_KIND ChannelHistory ChannelLink ConfigChange
        ConfigDeferred ContextBuilder ConversationLog CronClaimAbandoned CronClaimTimeDenied
        CronJob CronQueueTimeout CronService CronStoreBusy CronStoreUnreadable
        CronVetOverran DEFAULT_MONITOR_STALL_MIN_SECS DEFAULT_MONITOR_STALL_TICKS
        DIGEST_HOLD_SECS DM_SCOPE_UNIFIED DashboardState EXTERNALLY_MANAGED_STAMPS
        FINISHED_LOOP_REASONS GRACEFUL_SHUTDOWN_SECS GatewayManager GatewayOrchestrator
        GatewaySpec HEARTBEAT_KEY HEARTBEAT_TASK_TIMEOUT_SECS
        HOST_SESSION_KEY HeartbeatService HistoryConsolidator HookManager HooksConfig
        INJECTION_TIMEOUT InboundMessage KiroCrewConfig LLMEvent LaunchApprovals LessonStore
        MANAGED_BY_COMMAND MAX_PROMPT_BYTES MAX_SESSION_TTL_SECS MODEL_REFUSED_REASON
        MONITOR_CHANNEL MONITOR_STOP_APPROVAL_STALL MONITOR_STOP_COMPLETION_UNAVAILABLE
        MONITOR_STOP_INVALID_RECORD MONITOR_STOP_SESSION_UNAVAILABLE
        MONITOR_STOP_UNSUPPORTED_VERSION MONITOR_STOP_VERDICT_STALL MONITOR_TERMINAL_REASON
        MemoryStore MonitorActionDisposition MonitorCompletionHook MonitorDispatchResult
        MonitorOutcome NO_TOOL_CALLS_NOTE NudgeLoop ORPHAN_NOTICE_OWED OUTCOME_FAILED
        OUTCOME_OK OUTCOME_STOPPED PATH_ONLY_INSTALL_NOTE PlatformCompositionError
        PostedOptions PromptBusyExhaustedError QUEUED_WAIT_EXPIRED_TEXT RealSlackClient
        SESSION_NOT_FOUND_GIVE_UP_TEXT SESSION_NOT_FOUND_NOT_REPLAYED_TEXT SLACK_NAMESPACE
        STRUCTURAL_TERMINAL_REASON SUBAGENT_BATCH_COMPLETION_PREFIX SUBAGENT_COMPLETION_KIND
        SUBAGENT_COMPLETION_META_KEY SUBAGENT_COMPLETION_PREFIX SUBAGENT_DIGEST_CHUNK_SIZE
        SandboxUnavailableError SessionBusyError SessionClosingError SessionManager
        SilentRenderer SkillsLoader SlackTransport SpawnApprovalUnreachable SubagentDelivery
        SubagentInfo SubagentManager Subscription TOOL_ACTIVITY_ATTR TaskRunner
        ToolApprovalCallback ToolApprovalPolicy TurnDriver UPDATE_INSTALLER_STOP_SECS
        acp_error_after_tool_activity acp_error_is_session_not_found acp_error_is_transient
        agent_scratch agent_sequence_dispatches annotate_model_fallback
        append_fallback_story apply_kas_cli_floor audit_governance_degraded
        auto_update_effect autonudge_enabled autonudge_selfarm beacon boot_platform
        bounded_chat_turn build_cron_ack_block build_cron_session_context
        build_dashboard_url build_directive_consumer build_frontend_async
        build_options_blocks build_provider_factory build_timing_footer build_tool_gate
        builtin_channel_descriptors cautious_boot channel_egress_parts
        channel_inbound_permitted channel_namespace_of check_bundle_freshness
        chunk_for_transport claim_vet_bound commits_ahead compose_nudge_body config_dir
        configure_default_executor configured_fallback_chain consume_reinjection
        context_meter_reading crash_guard create_subprocess_limited cron_gate_budget
        cron_job_env_without_reserved current_context dashboard_slot_key data_home
        deliver_spawn_approval delivery_fingerprint dep_sync display_safe
        disposition_for_stop_reason distribution drain_idle_parent_queue_for_stall
        drain_sign_in_refresher effective_wake_budget embed_executor
        embedding_model_is_custom ensure_agents_slice_limits ensure_cron_slot escape_mrkdwn
        extract_options filter_target_env flush_audit_queue_before_hard_exit
        flush_breadcrumb_writes format_dashboard_urls format_subagent_usage generate_token
        get_shared_embedder git_command_env governance_permits hidden_worktree_edits
        hooks_config_from_config_dict inbound_spool inject_cron_result_to_dashboard
        is_bundled_kiro_cli is_channel_key is_gateway_supported is_keep_response
        is_legacy_slack_key is_local_only is_monitor_completion_evidence is_primary_branch
        is_read_only_bash
        is_structured_monitor_loop is_thread_incognito is_thread_temporary
        is_tracked_channel kiro_agents_dir kiro_crew listener_guard_exit_code live
        load_approvals load_window_off_loop log_probe_failure loggable_path logger
        maintenance_executor make_sync_embed_fn mint_options_token model_file_present
        monitor_state_public_dict name_grant nudge_cycle_header open_dm_with_retry
        parse_dashboard_url parse_kiro_cli_version parse_session_key
        peek_ready_shared_embedder persist_token_record_async persistence_lost_exit_code
        pin_kiro_cli platform_compat prefetch_cron_history probe_data_home
        provider_fallback_active provider_last_turn_usage provider_model_pin_partial
        provider_model_pin_refused publish_turn_identity read_context_tokens
        read_effective_agent read_turn_model rearm_reinjection reason_in
        reconcile_store_embedding_space record_auto_update_effect redact redact_and_truncate
        redact_credentials redact_exfiltration_urls redact_for_display
        redact_log_via_context redact_via_context reembed_progress registry
        rehydrate_slot_from_history_async remember_slack_options render_for_slack
        render_nudge_message repo_exec_config_reason resolve_dashboard_host
        resolve_max_subagents resolve_prefetch resolve_remote_url resolve_restart_launcher
        resolve_session_start_sizing restart_command_hint rewrite_agents rewrite_kwargs
        rollback_skill_bodies run_command_sandboxed run_gateway run_in_cron_gate_pool
        run_in_cron_pool run_in_embed_pool run_persistence_watchdog run_pre_exec_stops
        run_script_sandboxed run_sign_in_refresher run_stale_asset_watchdog
        runtime_budget_exceeded runtime_death safe_context_call safety_override
        sandboxed_spawn_argv sandboxed_spawn_argv_async save_conversation_turn_off_loop
        save_pass sel send_channel_parts session_store_for_turn session_work_dir
        set_publish_disabled shutdown_event shutdown_exit_code signing_key_preflight
        single_completion_meta spawn_guarded_turn start_background_model_download
        start_dashboard stop_reason_landed store_embedding_space_is_stale stream_and_collect
        strip_keep_sentinel subagent_event_slot subprocess_executor summarize_result
        supervisor_reentry sweep_agents_dir terminal_notification_delivery_matches
        token_secret tracks_upstream transient_retry_delay turn_ceiling turn_stats_meta
        update_blocked_reason update_ownership vet_and_audit vet_job_at_fire_time
        wait_for_unwinding_turns warm_backend warn_if_channel_uncredentialed
        warn_unreadable_tracked_channels wave_chunk_meta wave_final_meta work_root
        workspace_root
        """.split()),
}

#: Facade -> ``{name: module holding it}`` for each base name the facade re-binds by import.
_REBOUND: dict[str, dict[str, str]] = {
    "kiro_crew.acp.client": {
        "ACP_BACKENDS_HOST_AUTH_CALLBACK": "kiro_crew.acp.types",
        "LAUNCHER_EXIT_PREFIXES": "kiro_crew.sandbox",
        "SANDBOX_LAYER_CREW": "kiro_crew.sandbox",
        "SANDBOX_LAYER_HARNESS": "kiro_crew.sandbox",
        "launcher_refusal": "kiro_crew.sandbox",
        "sandbox_init_remediation": "kiro_crew.sandbox",
    },
    "kiro_crew.cron": {
        "CHANNEL_MAX_LEN": "kiro_crew.validation",
        "MAX_CRON_MESSAGE": "kiro_crew.validation",
        "MAX_SHORT_STRING": "kiro_crew.validation",
        "cron_gate_budget": "kiro_crew.executors",
        "platform_compat": "kiro_crew",
    },
    "kiro_crew.autonudge": {
        "MONITOR_BUSY_RETRY_SECS": "kiro_crew.monitoring.models",
        "MONITOR_COMPLETION_EVIDENCE_TIMEOUT_SECS": "kiro_crew.monitoring.models",
        "MONITOR_STOP_APPROVAL_STALL": "kiro_crew.monitoring.models",
        "MONITOR_STOP_SESSION_CLOSE": "kiro_crew.monitoring.models",
        "MONITOR_STOP_USER": "kiro_crew.monitoring.models",
        "MonitorActionCompletion": "kiro_crew.monitoring.models",
        "MonitorActionDisposition": "kiro_crew.monitoring.models",
        "MonitorBudgets": "kiro_crew.monitoring.models",
        "MonitorCreationSurface": "kiro_crew.monitoring.models",
        "MonitorDecision": "kiro_crew.monitoring.models",
        "MonitorObservationStatus": "kiro_crew.monitoring.models",
        "MonitorProbeResult": "kiro_crew.monitoring.models",
        "MonitorVerdict": "kiro_crew.monitoring.models",
        "REVIEW_READY": "kiro_crew.monitoring.registry",
        "data_home": "kiro_crew.config.loader",
        "decide_monitor": "kiro_crew.monitoring.decision",
        "is_unattempted_probe": "kiro_crew.monitoring.github_provider_errors",
        "kind_supports_objective": "kiro_crew.monitoring.registry",
        "monitor_budget_reason": "kiro_crew.monitoring.decision",
        "monitor_stall_reason": "kiro_crew.monitoring.decision",
        "monitor_state_to_dict": "kiro_crew.monitoring.models",
        "platform_compat": "kiro_crew",
        "probes": "kiro_crew",
        "retained_outcome_blocks_rearm": "kiro_crew.monitoring.models",
        "shutdown_event": "kiro_crew",
        "stamp_monitor_alerted": "kiro_crew.monitoring.decision",
        "targets": "kiro_crew.probes",
        "validate_runtime_secs": "kiro_crew.monitoring.limits",
    },
    "kiro_crew.apps.builtins.ops_mission_control.backend.routes": {
        "CLAIMED_BY_OPERATOR": "kiro_crew.apps.builtins.ops_mission_control.backend.models",
        "CronStoreUnreadable": "kiro_crew.cron",
        "DEFAULT_VERIFY_AFTER_SECS": "kiro_crew.apps.builtins.ops_mission_control.backend.models",
        "EXPIRING_ACTIONS": "kiro_crew.apps.builtins.ops_mission_control.backend.models",
        "LedgerEntry": "kiro_crew.apps.builtins.ops_mission_control.backend.models",
        "MODE_ORDER": "kiro_crew.apps.builtins.ops_mission_control.backend.models",
        "STATE_FIRING": "kiro_crew.apps.builtins.ops_mission_control.backend.models",
        "STATE_OK": "kiro_crew.apps.builtins.ops_mission_control.backend.models",
        "STATE_SUPPRESSED": "kiro_crew.apps.builtins.ops_mission_control.backend.models",
        "STATUS_NEEDS_HUMAN": "kiro_crew.apps.builtins.ops_mission_control.backend.models",
        "Signal": "kiro_crew.apps.builtins.ops_mission_control.backend.models",
        "UnknownFieldError": "kiro_crew.apps.builtins.ops_mission_control.backend.models",
        "VALID_ACTIONS": "kiro_crew.apps.builtins.ops_mission_control.backend.models",
        "VERIFIABLE_ACTIONS": "kiro_crew.apps.builtins.ops_mission_control.backend.models",
        "VERIFY_NOT_CHECKABLE": "kiro_crew.apps.builtins.ops_mission_control.backend.models",
        "VERIFY_PENDING": "kiro_crew.apps.builtins.ops_mission_control.backend.models",
        "describe_secrets": "kiro_crew.apps.builtins.ops_mission_control.backend.secrets",
        "provider_config": "kiro_crew.apps.builtins.ops_mission_control.backend.providers",
        "resolve_silence_secs": "kiro_crew.apps.builtins.ops_mission_control.backend.models",
        "set_top_level": "kiro_crew.apps.builtins.ops_mission_control.backend.providers",
        "utc_now_iso": "kiro_crew.apps.builtins.ops_mission_control.backend.models",
    },
    "kiro_crew.apps.builtins.issue_radar.backend.routes": {
        "KiroCrewConfig": "kiro_crew.config.loader",
        "normalize_ui_language_tag": "kiro_crew.context",
        "ui_language_tag": "kiro_crew.context",
    },
    "kiro_crew.mcp_gateway.gatewayd": {
        "BackendGone": "kiro_crew.mcp_gateway.backend",
        "BackendUnavailable": "kiro_crew.mcp_gateway.pool",
        "CallerContext": "kiro_crew.mcp_caller",
        "DRAIN_DEADLINE_SECS": "kiro_crew.mcp_gateway.pool",
        "HostBudgetExhausted": "kiro_crew.mcp_gateway.host_budget",
        "KIROCREW_BIN_MCP_SERVERS": "kiro_crew.mcp_cleanup",
        "PoolAtCapacity": "kiro_crew.mcp_gateway.pool",
        "SpawnGateClosed": "kiro_crew.mcp_gateway.admission",
        "SpawnGateTimeout": "kiro_crew.mcp_gateway.admission",
        "code_fingerprint": "kiro_crew.code_fingerprint",
        "configure_default_executor": "kiro_crew.executors",
        "env_sidecar_dir": "kiro_crew.mcp_gateway.rewriter",
        "env_sidecar_name": "kiro_crew.mcp_gateway.rewriter",
        "hash_command": "kiro_crew.mcp_gateway.hashing",
        "hash_effective_env": "kiro_crew.mcp_gateway.hashing",
        "is_credential_env_key": "kiro_crew.mcp_gateway.manager",
        "new_tenant_nonce": "kiro_crew.mcp_caller",
        "non_secret_env": "kiro_crew.mcp_gateway.hashing",
        "prewarm_from_payloads": "kiro_crew.mcp_gateway.prewarm",
        "resolve_overlay_dir": "kiro_crew.mcp_gateway.rewriter",
        "resolve_peer_identity": "kiro_crew.peer_resolve",
        "subprocess_executor": "kiro_crew.executors",
        "tool_surface": "kiro_crew.mcp_gateway",
    },
    "kiro_crew.crew_log.projection": {
        "CODE_BAD_DATA": "kiro_crew.crew_log.errors",
        "CrewLog": "kiro_crew.crew_log.store",
        "CrewLogError": "kiro_crew.crew_log.errors",
        "DASHBOARD_AGENTIC_ENTRY_TYPE": "kiro_crew.crew_log.entry_types",
        "DASHBOARD_CODE_LIMIT": "kiro_crew.crew_log.entry_types",
        "DASHBOARD_FIELD_LIMIT": "kiro_crew.crew_log.entry_types",
        "DASHBOARD_FOLD_NAME": "kiro_crew.crew_log.entry_types",
        "DASHBOARD_REASON_LIMIT": "kiro_crew.crew_log.entry_types",
        "DASHBOARD_REFUSED_ENTRY_TYPE": "kiro_crew.crew_log.entry_types",
        "DASHBOARD_TYPE_LIMIT": "kiro_crew.crew_log.entry_types",
        "DASHBOARD_VALUE_BYTES": "kiro_crew.crew_log.entry_types",
        "DASHBOARD_VALUE_LIMIT": "kiro_crew.crew_log.entry_types",
        "Entry": "kiro_crew.crew_log.schema",
        "KIND_SESSION": "kiro_crew.crew_log.schema",
        "LEDGER_ENTRY_TYPE": "kiro_crew.session_ledger",
        "MAX_ENTRY_BYTES": "kiro_crew.crew_log.schema",
        "MISTAKES_FOLD_NAME": "kiro_crew.crew_log.entry_types",
        "MISTAKE_CORRECTED_CODE": "kiro_crew.crew_log.entry_types",
        "MISTAKE_CORRECTION_LIMIT": "kiro_crew.crew_log.entry_types",
        "MISTAKE_GROUP_LIMIT": "kiro_crew.crew_log.entry_types",
        "OpenedRecord": "kiro_crew.crew_log.session_tree",
        "PANEL_CREW_KEY_LIMIT": "kiro_crew.crew_log.entry_types",
        "PANEL_ENTRY_TYPE": "kiro_crew.crew_log.entry_types",
        "PANEL_FOLD_NAME": "kiro_crew.crew_log.entry_types",
        "PANEL_HISTORY_LIMIT": "kiro_crew.crew_log.entry_types",
        "PANEL_OWNER_LIMIT": "kiro_crew.crew_log.entry_types",
        "PANEL_TEMPLATE_LIMIT": "kiro_crew.crew_log.entry_types",
        "PANEL_TITLE_LIMIT": "kiro_crew.crew_log.entry_types",
        "PHASE_SESSION_START": "kiro_crew.context_blocks",
        "ProjectionRegistry": "kiro_crew.projection",
        "RADAR_CI_BOUNDS": "kiro_crew.crew_log.entry_types",
        "RADAR_CI_KEYS": "kiro_crew.crew_log.entry_types",
        "RADAR_CLEARABLE_FIELDS": "kiro_crew.crew_log.entry_types",
        "RADAR_CREW_LEVEL_EVENT_KIND": "kiro_crew.crew_log.entry_types",
        "RADAR_DEFAULT_SKIP_SCOPE": "kiro_crew.crew_log.entry_types",
        "RADAR_EDITING_PHASES": "kiro_crew.crew_log.entry_types",
        "RADAR_ENTRY_TYPE": "kiro_crew.crew_log.entry_types",
        "RADAR_EVENT_KINDS": "kiro_crew.crew_log.entry_types",
        "RADAR_LABELS_LIMIT": "kiro_crew.crew_log.entry_types",
        "RADAR_NUMBER_BOUNDS": "kiro_crew.crew_log.entry_types",
        "RADAR_PHASES": "kiro_crew.crew_log.entry_types",
        "RADAR_SKIP_SCOPES": "kiro_crew.crew_log.entry_types",
        "RADAR_TERMINAL_PHASES": "kiro_crew.crew_log.entry_types",
        "ReadCursor": "kiro_crew.crew_log.store",
        "ResumePoint": "kiro_crew.crew_log.store",
        "SESSION_ENTRY_TYPES": "kiro_crew.crew_log.entry_types",
        "Savepoint": "kiro_crew.projection",
        "WORK_BLOCKED_REASONS": "kiro_crew.work_vocab",
        "WORK_CLOSED_WINDOW": "kiro_crew.work_vocab",
        "WORK_CONDUCTOR_FIELDS": "kiro_crew.work_vocab",
        "WORK_ENTRY_TYPE": "kiro_crew.crew_log.entry_types",
        "WORK_STORED_ITEM_LIMIT": "kiro_crew.work_vocab",
        "WORK_TOMBSTONE_LIMIT": "kiro_crew.work_vocab",
        "WorkBoardForgotten": "kiro_crew.work_vocab",
        "WorkBoardItem": "kiro_crew.work_vocab",
        "WorkBoardView": "kiro_crew.work_vocab",
        "attribute_seq": "kiro_crew.projection",
        "data_home": "kiro_crew.config.paths",
        "fold_citations": "kiro_crew.crew_log.session_tree",
        "log_exception_text": "kiro_crew.crew_log.store",
        "log_rank_of": "kiro_crew.crew_log.session_tree",
        "segment_paths": "kiro_crew.crew_log.store",
        "session_ledger": "kiro_crew",
        "session_units_by_slot": "kiro_crew.crew_log.store",
        "session_units_for_slot": "kiro_crew.crew_log.store",
        "unit_header_created_at": "kiro_crew.crew_log.store",
        "unit_opened_previous": "kiro_crew.crew_log.store",
    },
    "kiro_crew.slack.gateway": {
        "CronClaimAbandoned": "kiro_crew.slack.gateway_runtime.cron_dispatch",
        "CronClaimTimeDenied": "kiro_crew.slack.gateway_runtime.cron_dispatch",
        "CronVetOverran": "kiro_crew.slack.gateway_runtime.cron_dispatch",
        "claim_vet_bound": "kiro_crew.slack.gateway_runtime.cron_dispatch",
        "cron_job_env_without_reserved": "kiro_crew.slack.gateway_runtime.cron_dispatch",
        "run_gateway": "kiro_crew.slack.gateway_runtime.process_entry",
        "signing_key_preflight": "kiro_crew.slack.gateway_runtime.process_entry",
    },
}

_PROBE = """
import importlib, json, sys
rebound = json.loads(sys.argv[1])
checked, wrong = 0, []
for facade, names in rebound.items():
    module = importlib.import_module(facade)
    for name, home in names.items():
        checked += 1
        if getattr(module, name, None) is not getattr(importlib.import_module(home), name):
            wrong.append(f"{facade}.{name}")
print(json.dumps({"checked": checked, "wrong": wrong}))
"""


@pytest.mark.parametrize("facade", sorted(_BASE_PUBLIC_NAMES))
def test_every_base_public_name_resolves_on_its_facade(facade: str) -> None:
    module = importlib.import_module(facade)
    assert sorted(name for name in _BASE_PUBLIC_NAMES[facade] if not hasattr(module, name)) == []


def test_every_rebound_name_is_a_base_public_name_of_its_facade() -> None:
    assert set(_REBOUND) == set(_BASE_PUBLIC_NAMES)
    for facade, names in _REBOUND.items():
        assert names and set(names) <= _BASE_PUBLIC_NAMES[facade], facade


def test_a_rebound_name_is_the_object_its_home_module_holds(tmp_path: Path) -> None:
    """Checked in a fresh interpreter, so no module another test reloaded or evicted
    can make a copy look like the original, or the original look like a copy. The child
    runs in ``tmp_path``, so anything an import writes by a relative path stays there."""
    env = dict(os.environ)
    src = str(Path(kiro_crew.__file__).resolve().parents[1])
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    out = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(_REBOUND)],
        capture_output=True,
        check=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
        **UTF8_TEXT,
    )
    result = json.loads(out.stdout.splitlines()[-1])
    assert result == {"checked": sum(map(len, _REBOUND.values())), "wrong": []}, out.stderr
