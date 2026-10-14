"""HTTP API handlers for dashboard chat endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math  # noqa: F401
import os
import tempfile  # noqa: F401
import threading
import time
import uuid
import weakref
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime, timezone
from itertools import islice  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import TYPE_CHECKING, Any, NamedTuple

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError

from kiro_crew import members as members_mod
from kiro_crew import model_registry  # noqa: F401
from kiro_crew.acp.client import AcpModelUnavailable
from kiro_crew.agent_discovery import (  # noqa: F401
    cached_project_agent_names,
    warm_project_agent_names,
)
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS  # noqa: F401
from kiro_crew.agent_sdk.capabilities import MODEL_NAMESPACE_ACP, capabilities_of  # noqa: F401
from kiro_crew.agent_sdk.provider_identity import is_claude_code
from kiro_crew.agent_switch_command import switch_announcement  # noqa: F401
from kiro_crew.config.loader import (  # noqa: F401
    AUTOCOMPACT_PCT_MAX,
    AUTOCOMPACT_PCT_MIN,
    CHAT_TURN_TIMEOUT_MAX,
    CHAT_TURN_TIMEOUT_MIN,
    KiroCrewConfig,
    _workspace_name_for_dir,
    config_dir,
    crew_autocompact_pct,
    default_project_dir,
    default_template,
    dispatch_kiro_agent,
    published_autocompact_pct,
    resolve_agent_bindings,
)
from kiro_crew.dashboard import chat_api as _chat_api
from kiro_crew.dashboard.channel_slots import channel_slot_name, note_slot_closed  # noqa: F401
from kiro_crew.dashboard.chat_api import agent_switch as _owner_agent_switch
from kiro_crew.dashboard.chat_api import followup_card as _owner_followup_card
from kiro_crew.dashboard.chat_api import pending_context as _owner_pending_context
from kiro_crew.dashboard.chat_api import project_lists as _owner_project_lists
from kiro_crew.dashboard.chat_api import resume as _owner_resume
from kiro_crew.dashboard.chat_api import session_relaunch as _owner_session_relaunch
from kiro_crew.dashboard.chat_api import slot_approval as _owner_slot_approval
from kiro_crew.dashboard.chat_api import slot_detail as _owner_slot_detail
from kiro_crew.dashboard.chat_api import slot_effort as _owner_slot_effort
from kiro_crew.dashboard.chat_api import slot_guards as _owner_slot_guards
from kiro_crew.dashboard.chat_api import slot_lifecycle as _owner_slot_lifecycle
from kiro_crew.dashboard.chat_api import slot_model as _owner_slot_model
from kiro_crew.dashboard.chat_api import slot_note as _owner_slot_note
from kiro_crew.dashboard.chat_api import slot_settings as _owner_slot_settings
from kiro_crew.dashboard.chat_api import source_links as _owner_source_links
from kiro_crew.dashboard.chat_api.agent_switch import (  # noqa: F401
    SlotAgentSwitchCaller,
    _MemberMemoryRequiresNewConversation,
    agent_switch_outcome,
    api_chat_slot_agent,
    clear_slot_model_binding,
    switch_slot_agent,
)
from kiro_crew.dashboard.chat_api.followup_card import (  # noqa: F401
    _redact_followup_item,
    api_chat_slot_followup,
)
from kiro_crew.dashboard.chat_api.pending_context import (  # noqa: F401
    _build_pending_context_entry,
    _enqueue_pending_context,
    _is_secret_shaped_context,
    _normalize_source,
    _refuse_secret_shaped_context,
    _source_cap_reached,
    _validate_content,
    _validate_max_age,
    _validate_source,
    api_chat_slot_context,
)
from kiro_crew.dashboard.chat_api.project_lists import (  # noqa: F401
    FavoritesUnreadable,
    _add_favorite_project,
    _favorite_projects_path,
    _favorites_unreadable_response,
    _load_favorite_projects,
    _probe_favorite_candidate,
    _read_favorite_projects,
    _recent_projects_path,
    _remove_favorite_project,
    _save_recent_project,
    _write_favorite_projects,
    api_favorite_project_add,
    api_favorite_project_remove,
    api_favorite_projects,
    api_recent_projects,
)
from kiro_crew.dashboard.chat_api.resume import (  # noqa: F401
    _hydrate_slot_from_history,
    _live_slot_for_resume,
    _live_slot_resume_payload,
    _materialise_slot_from_history,
    _normalise_structured_content,
    _reconcile_slot_window,
    _redact_history_rows,
    _resume_refusal_response,
    _resume_session_identity,
    api_chat_slot_resume,
    resume_slot_from_history,
)
from kiro_crew.dashboard.chat_api.session_relaunch import (  # noqa: F401
    api_chat_slot_project,
    api_chat_slot_reload,
    api_chat_slot_workspace,
    reload_slot_session,
)
from kiro_crew.dashboard.chat_api.slot_approval import (  # noqa: F401
    _STRICT_NATIVE_ACTIONS,
    _approval_target_required,
    _deny_approval_mode,
    _deny_trust_pattern,
    _end_trust_scope,
    _end_trust_scopes,
    _get_pattern_from_pending,
    _grant_deadline,
    _internal_session_approval_refusal,
    _still_owning,
    api_chat_mode,
    api_chat_slot_approve,
    apply_approval_mode,
    bare_approval_decide_allowed,
    deny_session_approval_caller,
)
from kiro_crew.dashboard.chat_api.slot_detail import (  # noqa: F401
    _append_unflushed_tail,
    _append_unflushed_tail_from_offset,
    _bounded_slot_page,
    _context_reading,
    _context_snapshot_fields,
    _context_snapshot_fields_inner,
    _durable_prefix_counter,
    _DurablePrefixMismatch,
    _finite_number,
    _is_answered_permission,
    _load_redacted,
    _note_owner_focus,
    _same_persisted_body,
    _snapshot_slot_window,
    api_chat_slot_detail,
    api_chat_slots,
    api_chat_slots_unrestored,
)
from kiro_crew.dashboard.chat_api.slot_effort import (  # noqa: F401
    _configured_backend_for_slot,
    api_chat_slot_reasoning_effort,
    api_chat_slot_selection_capabilities,
)
from kiro_crew.dashboard.chat_api.slot_guards import (  # noqa: F401
    _CommitToken,
    _record_explicit_agent_selection,
    _reset_slot_session_or_warn,
    _slot_replaced_while_queued,
    _subagents_attached_response,
    _switch_target_busy,
)
from kiro_crew.dashboard.chat_api.slot_lifecycle import (  # noqa: F401
    _await_guarded_history_write,
    _close_slot,
    _NudgeRetireFailed,
    _pending_guarded_history_writes,
    _release_closed_execution,
    _restore_slot_nudge_loop,
    _retire_slot_nudge_loop,
    _slot_still_ours,
    api_chat_slot_delete,
    api_chat_slot_reset_conversation,
    api_chat_slots_cleanup,
    close_slot,
)
from kiro_crew.dashboard.chat_api.slot_model import (  # noqa: F401
    _broadcast_context_reset,
    _is_jev_route_pick,
    _model_rejected_reason,
    _reapply_effort_after_live_switch,
    _try_live_model_switch,
    _wire_model_id,
    api_chat_slot_model,
    api_chat_slots_model,
)
from kiro_crew.dashboard.chat_api.slot_note import (  # noqa: F401
    _discard_held_note,
    _note_already_durable,
    _note_delivered_live,
    _persist_deferred_note_hold,
    api_chat_slot_note,
)
from kiro_crew.dashboard.chat_api.slot_settings import (  # noqa: F401
    _autocompact_txn_lock,
    _slot_agent_autocompact_fields,
    _slot_next_agent_autocompact_default,
    _turn_timeout_body_value,
    _turn_timeout_payload,
    api_chat_slot_autocompact,
    api_chat_slot_color,
    api_chat_slot_turn_timeout,
)
from kiro_crew.dashboard.chat_api.source_links import (  # noqa: F401
    _apply_source_link_unlink,
    _audit_source_link_unlink,
    _source_link_txn_lock,
    api_chat_slot_source_link_unlink,
    api_chat_slot_source_links,
)
from kiro_crew.dashboard.chat_auto_tag import maybe_auto_tag
from kiro_crew.dashboard.chat_delivery import (  # noqa: F401
    STEER_REQUEUED,
    STEER_STEERED,
    TURN_ACTOR_META_KEY,
    attachment_meta,
    normalize_send_id,
    queue_entry_is_user_origin,
    queue_entry_view,
    queue_for_next_turn,
    queued_text_for_display,
    quote_meta,
    start_queue_persist,
    steer_into_running_turn,
)
from kiro_crew.dashboard.chat_folders import (
    _unhide_folder,
    resolve_folder_project_dir_off_loop,
)
from kiro_crew.dashboard.chat_persistence import (  # noqa: F401
    _FLUSH_SNAPSHOT_RETRIES,
    _TRANSIENT_ROLES,
    COLOR_HEX_RE,
    _attach_variants,
    _coerce_requested_mode,
    _has_validated_effort_marker,
    _load_restore_cfg,
    _local_turn_generation,
    _local_turn_prompt,
    _rebase_rehydrated_refresh_mark,
    _reconcile_local_turn_marker,
    _rehydrate_slot_title,
    _remember_reasoning_effort_for_restore,
    _restore_dismissed_source_links,
    _restore_model_fields,
    _restored_agent_name,
    _restored_mode,
    _validate_autocompact_pct,
    cap_effort_capability_levels,
    get_reasoning_effort_ordered,
    get_reasoning_effort_values,
    pin_private_agent_store,
    register_reasoning_effort_values,
    release_prewarmed_session,
    remember_unknown_row_fields,
    save_slot_off_loop,
)
from kiro_crew.dashboard.chat_runner import (  # noqa: F401
    _context_usage_payload,
    _run_chat,
    _start_next_queued_turn,
    _sync_served_model,
    context_entry_expired,
    schedule_eager_spawn,
    subagents_hold_user_messages,
)
from kiro_crew.dashboard.chat_slack import maybe_auto_link_slack, slot_is_live
from kiro_crew.dashboard.chat_summary import generate_session_summary, read_cached_intent_summary
from kiro_crew.dashboard.chat_tags import (
    _bump_slot_tags_revision,
    tags_write_lock,
    validate_folder_tag_ids,
)
from kiro_crew.dashboard.chat_title import _maybe_auto_title
from kiro_crew.dashboard.chat_utils import (  # noqa: F401
    _MANUAL_CONTINUE_MSG,
    _MANUAL_RESUME_MSG,
    SESSION_START_FAILED_KIND,
    SLOT_DETAIL_MAX_LIMIT,
    SYNTHETIC_RECOVERY_KIND,
    TURN_END_WIRE_CLS,
    _broadcast_expired_oauth_banners,
    _build_stream_chunk,
    _collapse_wire_rows,
    _edit_queued_by_id,
    _emit_agent_assignment,
    _history_key_for,
    _live_child_instance,
    _normalize_model,
    _prepare_messages,
    _redact_for_display,
    _redact_meta,
    _redact_meta_for_role,
    _remove_queued_by_id,
    _resettle_restricted_key,
    _sync_dashboard_slots,
    chunk_generation,
    drained_to_thread,
    effective_session_key,
    history_corpus_unreadable,
    is_harness_slash_command,
)
from kiro_crew.dashboard.chat_utils import (
    replacement_shares_transcript as _replacement_shares_transcript,
)
from kiro_crew.dashboard.chat_utils import (  # noqa: F401
    restore_replacement_if_handover_did_not_land,
    slot_history_key,
    slot_transcript_key,
    subagents_attached_async,
    tighten_live_slot_memory_mode,
)
from kiro_crew.dashboard.chat_utils import (
    tighten_replacement_to_restricted_original as _tighten_replacement_to_restricted_original,
)
from kiro_crew.dashboard.handlers._shared import (  # noqa: F401
    _SLOT_SCOPED_TRUST_MODES,
    _owner_denial_response,
    cron_creator_admission,
    cron_mode_refusal,
    cron_slot_creator,
    read_bounded_json,
)
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.interaction_coordinator import native_resolution_target  # noqa: F401
from kiro_crew.dashboard.relay_archive import relay_archive_refusal
from kiro_crew.dashboard.request_priority import owner_start_priority
from kiro_crew.dashboard.session_folders import SessionFolderError, create_session_folder
from kiro_crew.dashboard.slot_buffers import (  # noqa: F401
    MAX_DEFERRED_NOTE_CHARS,
    MAX_DEFERRED_NOTES,
    MAX_SOURCE_LABEL_LEN,
    SOURCE_LABEL_CTRL_RE,
    DeferredHoldFull,
    DeferredHoldRebound,
    note_hold_durable,
    persist_deferred_notes_sync,
)
from kiro_crew.dashboard.slot_ownership import (  # noqa: F401
    SESSION_CONTROL_DENIED,
    app_may_control_session,
    app_new_key_refusal,
    app_owns_slot_session,
    app_owns_transcript_meta,
    app_slot_is_local_user_session,
    audit_app_slot_denial,
    deny_app_session_control,
    deny_app_slot_access,
    deny_app_slot_session_access,
    own_session_key,
    read_session_grant,
    session_grant,
    slot_not_found,
    transcript_acquisition_reason,
)
from kiro_crew.dashboard.slot_projection import (  # noqa: F401
    resolved_row_identity,
    stop_declined_armed,
)
from kiro_crew.dashboard.slot_queue_repository import warn_if_not_durable
from kiro_crew.dashboard.slot_retention import (  # noqa: F401
    loop_slot_keys,
    select_idle_slot_keys,
)
from kiro_crew.dashboard.state import (  # noqa: F401
    _MAX_DISMISSED_SOURCE_LINKS,
    DashboardState,
    _ChatSlot,
    _mark_permission_resolved,
    _normalize_slot_key,
    _slots_serialization_note,
    chat_message_frame,
    durable_row_count,
    is_stop_event_row,
    is_turn_interrupted,
    load_window_off_loop,
    note_crew_log_class,
    parse_cls_meta,
    request_slot_origin,
    row_mid,
)
from kiro_crew.dashboard.system_notices import SESSION_RELOAD_KIND, is_system_notice  # noqa: F401
from kiro_crew.dashboard.turn_dispatch import (  # noqa: F401
    chat_turn_timeout_secs,
    spawn_guarded_turn,
)
from kiro_crew.history import (  # noqa: F401
    HUMAN_TURN_META_KEY,
    carry_provenance,
    is_incognito_transcript,
)
from kiro_crew.history_projection import TranscriptRevisionChanged  # noqa: F401
from kiro_crew.jsonl_util import OversizedRecord, SplitlinesBoundaryRecord  # noqa: F401
from kiro_crew.llm_helpers import (  # noqa: F401
    pick_epoch_host,
    provider_model_pin_refused,
    slot_switch_session_lock,
)
from kiro_crew.memory_startup import MemoryStartupUnavailable, wait_for_memory_preparation
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.messaging.link import canonical_key, is_channel_session_key  # noqa: F401
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import LLMProvider
from kiro_crew.safety_override import (  # noqa: F401
    approval_mode_permitted,
    safety_override,
    yolo_policy_permits,
)
from kiro_crew.sandbox import voice_runtime_workspace_conflict
from kiro_crew.security import (
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel
from kiro_crew.session_agent_selection import (  # noqa: F401
    SelectionChange,
    record_agent_selection,
    resolve_session_agent_bindings,
    restore_agent_selection,
    session_agent_selection_name,
)
from kiro_crew.session_lifecycle import compaction_in_flight
from kiro_crew.session_summary import count_user_turns_in_records
from kiro_crew.start_priority import person_priority  # noqa: F401
from kiro_crew.trust_patterns import (  # noqa: F401
    GrantDeadline,
    base_consent_pattern,
    base_trust_patterns,
    exact_trust_pattern,
    grant_deadline,
    parse_grant_ttl,
    record_trust_grant,
)
from kiro_crew.validation import (  # noqa: F401
    ARTIFACT_SLUG_RE,
    SUGGEST_FOLLOWUP_SCHEMA,
    ValidationError,
    is_registered_agent_name,
    normalize_theme_consent_sha,
    validate_tool_args,
)

if TYPE_CHECKING:  # circular at runtime: autonudge -> dashboard.chat -> chat_handlers
    from kiro_crew.autonudge import NudgeLoop  # noqa: F401
    from kiro_crew.config.sections import ResolvedBindings  # noqa: F401

#: The ACP and provider types the owners read through this module's globals. The
#: agent-SDK boundary allows these three imports in this file and in no owner, and
#: binding them here keeps each import line in use, so its baselined text is unchanged.
_ACP_LAYER_TYPES = (AcpModelUnavailable, AcpProvider, LLMProvider)

logger = logging.getLogger(__name__)

# Sentinel: the authorized transcript's identity (created_at) could NOT be read,
# so a source-link unlink write cannot be proven to target it. Distinct from a
# real created_at of None (a metadata line that simply lacks the field).
_UNPINNED: object = object()

# Feed notice appended by api_chat_slot_reload. A constant, not LLM-derived
# text, so it needs no redaction pass.
_SESSION_RELOAD_NOTICE = (
    "Reloading session: relaunching the agent process with a freshly loaded "
    "agent spec, environment, and MCP servers. The conversation is preserved."
)


def _sweep_stale_permissions(slot: "_ChatSlot") -> None:
    """Mark unresolved permissions from prior turns as stale.

    Called once at turn-start, before the new user message is appended.
    Safe: if we're starting a new turn, any prior unresolved permission
    is definitionally orphaned — the LLM that requested it is gone.

    Note: if the same slot is open in multiple tabs, an in-flight pending
    approval in tab A may be marked stale by a turn-start in tab B. The
    failure mode is benign (user re-clicks approve); single-tab use is
    unaffected.
    """
    for msg in slot.messages:
        if msg.get("role") != "permission":
            continue
        try:
            cls = json.loads(msg.get("cls", "{}"))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(cls, dict):
            # Valid JSON but not an object (e.g. [], "x", 123, null) — cannot
            # carry a "resolved" key; skip rather than raise TypeError and
            # abort the whole sweep. Mirrors parse_cls_meta() in state.py.
            continue
        if "resolved" in cls:
            continue
        cls["resolved"] = "stale"
        msg["cls"] = json.dumps(cls)
        slot._dirty = True
        sel().log_api_access(
            caller="gateway",
            operation="permission.resolve_stale",
            outcome="allowed",
            source="turn_start_sweep",
            resources=cls.get("request_id", ""),
        )


async def _app_slot_acquisition_denial(
    request: web.Request,
    state: "DashboardState",
    request_app: str,
    slot_name: str,
    operation: str,
    *,
    session_grant_route: bool,
) -> tuple[web.Response | None, Any]:
    """The app-ownership decision for a request that may CREATE the slot it names.

    ``POST /api/chat`` and ``POST /api/chat/slots`` resolve their slot through
    ``get_or_create_slot``, which refuses a memory-mode mismatch or an
    under-construction slot with a 409. That 409 must not tell an app that a
    session it may not see exists, or what its memory mode is, so for an app
    caller this runs first, on a lookup that creates nothing, and answers the
    same 404 for every refusal:

    * a member, cron or workflow key (:func:`app_reserved_key_reason`): an app
      never owns one, and the cron and workflow binders would link a slot under
      that key to the job's or run's own transcript. So is a ``dashboard_`` key,
      whose transcript is another slot's;
    * a key under construction, in any letter case: a session being imported is
      nobody's to name yet (the person still gets the 409; the app gets the 404,
      with no audit row);
    * a key that differs from another live slot's key, or names its transcript,
      only in letter case (:func:`live_case_alias_reason`): on a case-insensitive
      filesystem both slots would write one file;
    * a live slot: the per-slot decision (owner app on its own session and
      transcript, or the ``sessionApproval`` grant when *session_grant_route*);
    * no live slot but a persisted transcript under that key: the transcript must
      record this app (:func:`transcript_acquisition_reason`). Without it a closed
      user session could be re-created as an app-owned slot that reopens and
      appends to the user's transcript.

    *slot_name* is the RAW requested name, normalized here exactly once, as
    ``get_or_create_slot`` will normalize it. Returns ``(refusal, judged)``:
    *judged* is the live slot the decision was made on, or ``None``. It is an
    early out, not the last word: the caller awaits again before it acquires the
    slot, so it re-checks synchronously that the slot it acquires is *judged*
    (:func:`_acquired_slot_was_judged`) and keeps its own post-create check.
    """
    key = _normalize_slot_key(slot_name)
    history_key = _history_key_for(key)
    granted = False
    if session_grant_route:
        # Read before anything about the slot is looked at, for every app caller,
        # so the cost of the answer does not depend on which session was named.
        granted = await session_grant(request, request_app)
    refused, reason = app_new_key_refusal(state, key, history_key)
    if refused:
        if reason:
            audit_app_slot_denial(request_app, operation, key, reason)
        return slot_not_found(), None
    existing = state._slots.get(key)
    if existing is not None:
        allowed = (
            app_may_control_session(request_app, existing, granted)
            if session_grant_route
            else app_owns_slot_session(request_app, existing)
        )
        if allowed:
            return None, existing
        audit_app_slot_denial(request_app, operation, key, SESSION_CONTROL_DENIED)
        return slot_not_found(), None
    log = state.conversation_log
    if log is None:
        return None, None
    reason = await asyncio.to_thread(transcript_acquisition_reason, log, history_key, request_app)
    if not reason:
        return None, None
    audit_app_slot_denial(request_app, operation, key, reason)
    return slot_not_found(), None


def _send_binds_agent(agent: str, slot: Any) -> bool:
    """Whether a ``POST /api/chat`` naming *agent* writes it onto *slot*."""
    return bool(agent) and slot.agent in (None, "")


def _send_writes_persona(body: dict) -> bool:
    """Whether a ``POST /api/chat`` body writes the slot's persona fields.

    ``color_theme`` carries the write; ``theme_consent`` and
    ``theme_consent_sha`` are applied with it and never alone.
    """
    return "color_theme" in body


async def _send_harness_command(message: str) -> str:
    """The harness slash command *message* opens with, or ``""`` for plain text.

    The same predicate the runner forwards on (``is_harness_slash_command``), so
    what this refuses is exactly what would reach the harness as a command. The
    config is read only for a first word that starts with ``/``.
    """
    first_word = message.split()[0] if message.strip() else ""
    if not first_word.startswith("/"):
        return ""
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    cc_provider = is_claude_code(cfg.agent.provider)
    return first_word if is_harness_slash_command(first_word, cc_provider=cc_provider) else ""


async def _app_slot_acquisition_recheck(
    state: "DashboardState", request_app: str, key: str, judged: Any, operation: str
) -> web.Response | None:
    """Recheck a named acquisition after setup, without publishing an unjudged slot.

    A free key is reserved through the off-loop transcript read. Other acquirers
    respect the construction marker, so a create-and-close cannot hide between
    that read and the loop resuming. The caller must acquire synchronously after
    this returns; releasing the marker does not yield.
    """
    history_key = _history_key_for(key)
    refused, reason = app_new_key_refusal(state, key, history_key)
    if not refused and not _acquired_slot_was_judged(state, key, judged):
        refused, reason = True, "slot was replaced while the request was authorized"
    if not refused and judged is None and state._slots.get(key) is None:
        state.begin_slot_construction(key)
        try:
            log = state.conversation_log
            if log is not None:
                reason = await asyncio.to_thread(
                    transcript_acquisition_reason, log, history_key, request_app
                )
                refused = bool(reason)
        finally:
            state.end_slot_construction(key)
        if not refused:
            refused, reason = app_new_key_refusal(state, key, history_key)
        if not refused and state._slots.get(key) is not None:
            refused, reason = True, "slot was replaced while the request was authorized"
    if refused:
        if reason:
            audit_app_slot_denial(request_app, operation, key, reason)
        return slot_not_found()
    return None


def _acquired_slot_was_judged(state: "DashboardState", key: str, judged: Any) -> bool:
    """Whether the slot about to be acquired under *key* is the one ownership was decided on.

    Synchronous, for the moment right before ``get_or_create_slot``. A slot the
    pre-check approved that has since been closed would otherwise be minted
    afresh, app-owned, on the key of a session the app was only granted to act
    in -- and the post-create check passes on the new object. A slot that
    appeared where none was judged is decided by the post-create check; that
    acquisition performs no transcript-read await.
    """
    return judged is None or state._slots.get(key) is judged


def _app_acquisition_conflict(
    state: "DashboardState", request_app: str, key: str, judged: Any, exc: ValueError
) -> web.Response:
    """An app's answer when ``get_or_create_slot`` refused *key* with a ValueError.

    The 409 text stays only for the app's OWN live slot, the one the pre-check
    judged and that still holds *key*: it tells the app nothing it does not
    already know, and a 404 there would tell it its live session is gone. Every
    other conflict is the uniform 404, because its text names a session the app
    may not see.
    """
    if (
        judged is not None
        and state._slots.get(key) is judged
        and key not in getattr(state, "_slots_under_construction", ())
        and app_owns_slot_session(request_app, judged)
    ):
        return web.json_response({"error": str(exc)}, status=409)
    return slot_not_found()


def _deny_app_yolo(request_app: str, operation: str) -> web.Response:
    """App tokens never arm or revoke the process-global YOLO override."""
    sel().log_api_access(
        caller=request_app,
        operation=operation,
        outcome="denied",
        source="app_isolation",
        error="app tokens cannot arm yolo",
    )
    return web.json_response(
        {"ok": False, "error": "app tokens cannot arm yolo", "code": "app_yolo_forbidden"},
        status=403,
    )


def _deny_app_session_settings(request_app: str, slot_key: str, trigger: str) -> web.Response:
    """App tokens send turns to a user's session but never change its settings.

    *trigger* names what the send would have changed (``agent=<name>``,
    ``field=color_theme`` or ``command=<word>``) and rides the SEL row.
    """
    reason = (
        "app cannot change the agent binding or persona settings of a session it "
        "does not own, or run a harness slash command there"
    )
    sel().log_api_access(
        caller=request_app,
        operation="chat_send",
        outcome="denied",
        source="app_isolation",
        resources=f"slot={slot_key} {trigger}",
        error=reason,
    )
    return web.json_response(
        {"error": reason, "code": "app_session_settings_forbidden"}, status=403
    )


#: Row-meta keys a REQUEST may never supply, because the gateway mints them and a
#: surface reads them as the gateway's own claim. ``decisions_strip`` is a Jev
#: decision receipt with a verdict control attached (``decisions/points/
#: message_steer.py``, ``website/src/pages/chat/SteerDecisionLine.tsx``), so a
#: caller-supplied one would render a decision nobody made. ``HUMAN_TURN_META_KEY``
#: is the gateway's own claim that a PERSON typed a row; a caller-supplied one
#: forges human-turn provenance and advances the last-human-turn ranking stamp
#: (``chat_persistence._newest_human_turn_ts``), so an app token owning its slot
#: could displace human sessions. Stripped here so the gateway re-applies it below
#: only for a genuine human send. ``TURN_ACTOR_META_KEY`` is the gateway's record
#: that an app sent the row, which the title counter reads (``chat_title``).
RESERVED_ROW_META_KEYS = frozenset({"decisions_strip", HUMAN_TURN_META_KEY, TURN_ACTOR_META_KEY})

#: The ``steer`` value that means "let Jev choose between the two paths" rather
#: than naming one. A STRING beside the boolean the two manual modes send, so the
#: manual wire is untouched: ``steer: true`` still steers and an absent flag still
#: queues, byte for byte, whatever this build decides about ``auto``.
STEER_AUTO = "auto"


def steer_is_auto(value: object) -> bool:
    """Whether a send's ``steer`` flag asks Jev to choose the path.

    Only the exact string, case- and space-insensitively. A boolean ``True`` is a
    MANUAL steer and must never read as auto: that flag is what every existing
    client sends, and reading it as a request to decide would put an oracle on a
    path the sender already answered.
    """
    return isinstance(value, str) and value.strip().lower() == STEER_AUTO


async def decided_message_handling(slot: Any, message: str) -> tuple[bool, dict | None]:
    """Ask ``message.steer`` whether *message* queues instead of steering.

    Returns ``(queues, record)``: whether to take the QUEUE path, and the decision
    row to stamp on the persisted user row (``None`` when nothing was decided, or
    when the row itself was refused).

    A BOOLEAN rather than the point's choice string, so the caller holds no copy of
    that vocabulary and cannot drift from it: every refusal -- the seam off, the
    session unsampled, a timeout, an answer outside the two options, a failed
    import -- is ``False``, which is the steer path a manual Steer and the
    composer's default have always taken.

    Called for a send the dashboard's own human made while a turn is running, and
    nowhere else (the busy branch's ``not request_app`` conjunct): an app token, an
    integration or a cron has nobody watching the reply, and every such send
    already falls through to the fail-closed queue. Consent and sampling are the
    seam's own gates, re-checked inside ``decide``.

    Never raises except cancellation. The seam may cost an observation and must
    never cost a send.
    """
    try:
        # Imported HERE, not at module scope: this module is on the gateway's boot
        # path and the decisions package is optional, off by default, and pulls the
        # config loader in behind it.
        from kiro_crew.decisions.points import message_steer

        session_key = effective_session_key(slot)
        decided = await message_steer.steer_or_queue(
            message,
            session_key=session_key,
            # The live list, sliced and read off the loop by the point; nothing is
            # written back.
            rows=getattr(slot, "messages", ()) or (),
        )
        if decided is None:
            return False, None
        # The row is written BEFORE the path is taken, and the receipt is stamped
        # only when it landed: the strip's thumbs POST this turn id, so a receipt
        # from a row `append` refused would invite a verdict about a decision the
        # log does not hold. Off the loop -- the append locks a file.
        record = await asyncio.to_thread(message_steer.record_outcome, session_key, decided)
        return decided.get("choice") == message_steer.CHOICE_QUEUE, record
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("message.steer: taking the shipped default", exc_info=True)
        return False, None


async def api_chat(request: web.Request) -> web.StreamResponse:
    """POST /api/chat — send message to a slot, stream response via SSE."""
    state: DashboardState = request.app["state"]
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    message = body.get("message", "").strip()
    agent = body.get("agent", "")
    slot_name = body.get("slot")
    color_theme = body.get("color_theme", "")
    user_meta = body.get("meta")  # knowledge/files/pastes metadata from frontend
    if not isinstance(user_meta, dict):
        user_meta = None
    else:
        # The row's decision receipt is SERVER-minted and must never be one a
        # caller can write. `meta` rides verbatim onto the persisted user row
        # (`_redact_meta` redacts string values; it is not an allowlist) and onto
        # the queue entry, and the transcript renders `meta.decisions_strip` as a
        # Jev decision with a verdict control whose POST names the turn id in it.
        # So an app token or any other caller could otherwise stamp a decision
        # nobody made and file feedback against it. Dropped HERE, where the field
        # enters, so every downstream user of `user_meta` -- the dispatch row, the
        # busy-slot queue entry, the sub-agent hold -- is covered by one gate
        # rather than each remembering.
        user_meta = {k: v for k, v in user_meta.items() if k not in RESERVED_ROW_META_KEYS}
        # The whole-message quote is bounded HERE, once, for every path below:
        # the immediate row persists `user_meta` verbatim, so a bound applied
        # only where the queue paths read it would leave that row unbounded.
        # A record the bound refuses is dropped whole; the text beside it still
        # carries the blockquote.
        if "quote" in user_meta:
            # Bounded once here; redacted here too when the sender is not the
            # session's own human (an app token), so every later read of
            # `user_meta` -- immediate row, queue entry, hold entry, frames --
            # carries the same form its text gets.
            bounded_quote = quote_meta(user_meta, user_origin=not bool(request.get("app", "")))
            user_meta.pop("quote")
            user_meta.update(bounded_quote)
        if not user_meta:
            user_meta = None
    theme_consent = body.get("theme_consent") is True
    # Content-bound persona consent: the sha256 hex the user
    # granted in the consent modal. Injection is gated on this matching the
    # persona text read from disk server-side; the legacy boolean above is
    # still parsed (backward-compatible bodies + logging) but does not grant
    # injection by itself. Normalize + full-match to 64 lowercase hex here so a
    # malformed value (non-ASCII "é", wrong length, non-str) becomes None
    # (absent) rather than reaching hmac.compare_digest and crashing the turn
    # with a TypeError.
    theme_consent_sha = normalize_theme_consent_sha(body.get("theme_consent_sha"))
    if not isinstance(color_theme, str) or not (
        color_theme == "" or color_theme.startswith("custom-")
    ):
        color_theme = ""
    if not isinstance(slot_name, str) and slot_name is not None:
        slot_name = None  # coerce non-string slot to auto-generate
    _requested_key = _normalize_slot_key(slot_name) if slot_name else ""
    # Which of the owner's tabs is talking in this slot, so a live guide
    # observation asks that tab and no other (``guide_observe``), and which
    # language that tab's dashboard shows (``X-UI-Lang``), so ``find_ui`` quotes
    # labels as that screen spells them. Only the owner's own send, only the
    # tab id and a shipped-catalog tag, only in memory.
    _guide_tab = request.headers.get("X-Guide-Tab")
    _ui_lang = request.headers.get("X-UI-Lang")
    if _requested_key and (_guide_tab or _ui_lang) and not request.get("app", ""):
        from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

        if is_owner_dashboard_request(request):
            from kiro_crew.dashboard.guide_observe import note_chat_sender

            note_chat_sender(state, _requested_key, _guide_tab, _ui_lang)
    # Ownership BEFORE get_or_create_slot below, whose memory-mode and
    # under-construction 409s would otherwise answer an app about a session it
    # may not see. Member, cron and workflow keys are refused here too. The
    # post-create check further down stays authoritative.
    acquisition_judged: Any = None
    request_app = request.get("app", "")
    if request_app and slot_name:
        denied, acquisition_judged = await _app_slot_acquisition_denial(
            request,
            state,
            request_app,
            slot_name,
            "chat_send",
            session_grant_route=True,
        )
        if denied is not None:
            return denied
    # A tab restored as a sidebar row loads its window here, with the transcript
    # read off the loop, before anything below reads its rows. The live slot is
    # read again after the await, so a close or a replacement during the load is
    # what the rest of this handler sees.
    if _requested_key and (_pending := state._slots.get(_requested_key)) is not None:
        await load_window_off_loop(_pending)
    existing = state._slots.get(_requested_key) if _requested_key else None
    # An app's auto-created slot must not land on a transcript it does not own:
    # its first save would stamp the app onto that transcript's metadata line.
    if (
        _requested_key
        and existing is None
        and await _app_claim_refused(
            state, request.get("app", ""), "chat_send", (_history_key_for(_requested_key),)
        )
    ):
        return slot_not_found()
    if (
        existing is not None
        and existing.mode == members_mod.DM_SLOT_MODE
        and not members_mod.is_dispatchable_member_name(existing.agent)
    ):
        sel().log_api_access(
            caller=request.remote or "",
            operation="chat_send",
            outcome="denied",
            source="member_pin",
            resources=f"slot={existing.key}",
            error="stored member pin is not dispatchable",
        )
        return web.json_response(
            {
                "error": "this thread's crew name cannot be dispatched",
                "code": "member_pin_mismatch",
            },
            status=409,
        )
    member_pin_match = members_mod.member_pin_matches(
        getattr(existing, "mode", None), getattr(existing, "agent", None), agent
    )
    if not isinstance(agent, str) or not (
        agent == ""
        or is_registered_agent_name(agent)
        or member_pin_match
        # A free-form display name is admitted only as the configured member it
        # names (config read off-loop, on this miss path alone); a non-member
        # string that fails the grammar is not a template and stays refused.
        or await asyncio.to_thread(members_mod.is_configured_dispatchable_member, agent)
    ):
        _emit_agent_assignment(str(slot_name or ""), str(agent), outcome="denied_invalid")
        return web.json_response({"error": "invalid agent name"}, status=400)

    # Honor memory_mode from the body when auto-creating a slot (e.g. AgentRock
    # skill dispatch defaults to "temporary"). Only validated values are passed
    # through; anything else is dropped so get_or_create_slot uses its default.
    # If the slot already exists, get_or_create_slot raises on a memory_mode
    # mismatch, matching POST /api/chat/slots semantics.
    requested_memory_mode = body.get("memory_mode")
    if requested_memory_mode not in ("persistent", "incognito", "temporary"):
        requested_memory_mode = None

    # Honor mode from the body when auto-creating a slot, mirroring memory_mode
    # above: an app whose worker slot lives only in gateway memory (e.g. Design
    # Critique) repeats mode on send(), so a slot recreated here after a
    # gateway restart keeps its non-"" surface and stays out of the chat
    # sidebar's surface allowlist. Only creation-allowlisted values pass;
    # anything else is dropped so get_or_create_slot uses its default. Unlike
    # memory_mode there is no mismatch error: get_or_create_slot ignores mode
    # for an already-existing slot.
    requested_mode = body.get("mode")
    if not isinstance(requested_mode, str) or requested_mode not in _CREATABLE_MODES:
        requested_mode = ""

    # member-* keys are RESERVED for member DM threads, which are born only
    # through POST /api/members/{slug}/thread. Auto-creating one here (e.g. a
    # send racing a gateway restart that dropped the live slot, or an app
    # token naming the key) would mint an ordinary unpinned slot on the
    # member key — every pin guard is conditioned on mode=="member", so the
    # squatter bypasses all of them AND 409s the real thread opener forever.
    # Refused, not dropped: the caller must re-open through the member route.
    if _requested_key:
        if _requested_key.casefold().startswith(members_mod.DM_SLOT_KEY_PREFIX):
            if _requested_key not in state._slots:
                return web.json_response(
                    {
                        "error": "member thread slots are created only via the member thread endpoint",
                        "code": "member_slot_reserved",
                    },
                    status=409,
                )

    # `relay=1` was the retired turn relay: an older owner gateway asking this
    # peer to run a turn in one of its slots. Refused whole, before slot
    # creation, so such a send can never auto-create a blank session here.
    if request.query.get("relay") == "1":
        return web.json_response(
            {"error": "the remote turn relay is retired", "code": "remote_relay_retired"},
            status=410,
        )
    cron_creator = await cron_slot_creator(request)
    if cron_creator:
        refusal, _, _ = await cron_creator_admission(request, state, slot_name, cron_creator)
        if refusal is not None:
            return refusal

    if request_app and _requested_key:
        denied = await _app_slot_acquisition_recheck(
            state, request_app, _requested_key, acquisition_judged, "chat_send"
        )
        if denied is not None:
            return denied
    created_in_send = slot_name is None or _normalize_slot_key(slot_name) not in state._slots
    try:
        slot = state.get_or_create_slot(
            slot_name,
            app=request.get("app", ""),
            origin=request_slot_origin(request.get("app", ""), cron_creator=cron_creator),
            mode=requested_mode,
            memory_mode=requested_memory_mode,
            # Human request-layer path: a person sending a chat message. The
            # origin conjunct in state.py still excludes app-token callers. A
            # slot an attested cron opens is not a person's and is not counted.
            count_user_session=not cron_creator,
        )
    except ValueError as exc:
        sel().log_api_access(
            caller=request.get("app", ""),
            operation="chat_send",
            outcome="denied",
            source="memory_mode_mismatch",
            resources=f"slot={slot_name}",
            error=str(exc),
        )
        if request_app:
            return _app_acquisition_conflict(
                state, request_app, _requested_key, acquisition_judged, exc
            )
        return web.json_response({"error": str(exc)}, status=409)
    if cron_creator and created_in_send:
        # Attribute the slot to the cron that opened it, inside the synchronous
        # window after the mint, as session_control.create_session does for an
        # agent cron's child. Never re-stamped on an existing slot a cron names.
        slot._created_by = cron_creator

    # App ownership check (App Kit §5.2): deny-by-default for app tokens.
    # Apps keep access to their own slots. The sessionApproval grant lets an
    # enabled app send a turn into an existing user-owned slot. A response
    # option click is one such turn. The grant never crosses into another
    # app's slot. The grant verdict is the one this request already read.
    denied = await deny_app_session_control(request, request_app, slot, slot.key, "chat_send")
    if denied is not None:
        return denied
    # The dashboard user and the slot's owning app may change the slot's settings
    # through this route; the sessionApproval grant reaches a user's session to
    # send a turn and nothing else.
    may_configure = not request_app or bool(getattr(slot, "_app", ""))
    steer = body.get("steer") if may_configure else None
    if not may_configure:
        # The dedicated agent route refuses an app on a slot it does not own, and
        # persona consent is the user's own grant from the consent modal. Refused
        # rather than dropped, like the agent mismatch below: running the turn on
        # an agent other than the one named would be a silent substitution. A
        # harness slash command is refused here, above the busy branch, so a
        # queued copy cannot run it later.
        command = await _send_harness_command(message)
        # The slot may have closed, or a cron or channel binder may have
        # re-linked it, during the permission and config reads; the grant
        # reaches only a local user session.
        if (
            state._slots.get(slot.key) is not slot
            or slot.is_closing
            or not app_slot_is_local_user_session(slot)
        ):
            audit_app_slot_denial(
                request_app,
                "chat_send",
                slot.key,
                "slot closed or re-linked during the permission read",
            )
            return slot_not_found()
        # No await sits between this check and the agent write below for a slot
        # this grant reaches: the member awaits never run for one, and the
        # mismatch branch's awaits run only on a slot that already has an agent.
        # The persona condition reads only the request body.
        if command:
            return _deny_app_session_settings(request_app, slot.key, f"command={command[:64]}")
        if _send_binds_agent(agent, slot):
            return _deny_app_session_settings(request_app, slot.key, f"agent={agent}")
        if _send_writes_persona(body):
            return _deny_app_session_settings(request_app, slot.key, "field=color_theme")
        sel().log_api_access(
            caller=request_app,
            operation="chat_send",
            outcome="allowed",
            source="app_isolation",
            resources=f"permissions.sessionApproval|slot={slot.key}",
        )
        # The gateway mints the row id for a turn sent onto another's session.
        if user_meta is not None and "mid" in user_meta:
            user_meta = {k: v for k, v in user_meta.items() if k != "mid"} or None
    # The member-pin refusal sits AFTER the app-ownership 404s (a 409 here
    # for an app would be an existence oracle for slots it may not see) and
    # BEFORE the _human_seen attendance mark, so a denied request leaves the
    # slot exactly as it found it.
    if slot.mode == "member" and agent and agent != slot.agent:
        # Member DM threads are pinned to their crew. The generic mismatch
        # branch below would also refuse this, but the pin deserves its own
        # machine-readable refusal — and it must hold even for a member slot
        # whose agent is somehow empty (the elif below would otherwise adopt
        # the request's agent onto the pinned thread).
        _emit_agent_assignment(slot.key, agent, outcome="denied_member_pin")
        return web.json_response(
            {"error": "member thread agent is pinned", "code": "member_thread_agent_pinned"},
            status=409,
        )
    if slot.mode == "member":
        _member_cfg = await asyncio.to_thread(KiroCrewConfig.load)
        if slot.agent not in _member_cfg.agents:
            sel().log_api_access(
                caller=request.remote or "",
                operation="chat_send",
                outcome="denied",
                source="member_pin",
                resources=f"slot={slot.key}",
                error=f"registry no longer names {slot.agent}",
            )
            return web.json_response(
                {
                    "error": "this thread's crew no longer exists",
                    "code": "member_pin_mismatch",
                },
                status=409,
            )
        if slot.key.startswith(members_mod.DM_SLOT_KEY_PREFIX):
            _send_binding = await asyncio.to_thread(members_mod.read_dm_binding_for_slot, slot.key)
            if _send_binding is None or _send_binding.get("member", "") != slot.agent:
                sel().log_api_access(
                    caller=request.remote or "",
                    operation="chat_send",
                    outcome="denied",
                    source="member_pin",
                    resources=f"slot={slot.key}",
                    error="member binding missing or mismatched",
                )
                return web.json_response(
                    {
                        "error": "this thread's binding is missing or no longer matches",
                        "code": "member_binding_missing",
                    },
                    status=409,
                )
    if not request_app:
        # A dashboard user (no app token) typed into this slot, so a human
        # demonstrably has it open. That restores the full 2h approval
        # window even on an app-owned tab — the deny-fast window is for slots
        # nobody is watching. Only a caller with an EMPTY request_app reaches
        # here, so an app cannot forge attendance for its own worker.
        slot._human_seen = True

    if slot.agent not in (None, ""):
        # Slot already has an agent — only reject explicit mismatches (non-empty different agent).
        # Empty agent in request means "use existing" (e.g. follow-up messages from frontend).
        if agent and slot.agent != agent:
            # Different NAMES can still be the same BINDING: slots record the
            # resolved default ALIAS at creation, while a client may send the
            # underlying kiro agent name (the E2E smoke tests do exactly this).
            # 409 only when the two names resolve to different dispatch targets.
            # Identity is EVERY dispatch-relevant binding field — kiro agent,
            # workspace, memory store, and model — because aliases configure
            # memory stores and model pins independently of workspace, and a
            # request landing in another alias's memory store is the exact
            # cross-scoping this guard exists to prevent. An unknown requested
            # name (requested_resolved=False) still 409s — the resolver would
            # silently fall back to the default, which is the same lie.
            # Resolution failure falls back to the strict name comparison
            # (fail closed), reported as its own outcome so a config-load
            # blip is not triaged as an agent-naming problem.
            same_binding = False
            resolution_failed = False
            compared_binding = (
                slot.agent,
                slot.project,
                slot.memory_store,
                effective_session_key(slot),
                slot.workspace,
                slot._app,
                slot.agent_kind,
            )
            try:
                # Config load is file IO (stat + read + jsonschema validate on a
                # cache miss), so it rides a thread like the member-slot load
                # above — never the event loop.
                _cfg = await asyncio.to_thread(KiroCrewConfig.load)
                # Resolve within the slot's PROJECT scope, exactly as dispatch
                # does (see the agent-switch path below): a project-scoped agent
                # exists only inside slot.project, so resolving without it would
                # fall back to default bindings and falsely equate a
                # project-agent slot with a request naming the default alias.
                # Keep both lookups on one captured selection and off-loop:
                # private store validation reads ownership files even on cache hits.
                await warm_project_agent_names(
                    compared_binding[1] or None, operation="api_chat", source="dashboard"
                )

                def _compare_bindings():
                    return (
                        resolve_session_agent_bindings(
                            resolve_agent_bindings,
                            _cfg,
                            compared_binding[3],
                            compared_binding[0],
                            compared_binding[1] or None,
                            agent_kind=compared_binding[6],
                        ),
                        resolve_agent_bindings(_cfg, agent, compared_binding[1] or None),
                    )

                _stored, _requested = await asyncio.to_thread(_compare_bindings)
                # Identity itself lives on ResolvedBindings, next to the field
                # set, so a new dispatch-relevant field cannot silently widen
                # this bypass. requested_resolved stays a separate caller-side
                # check: an unknown name would resolve to the default and MATCH
                # a default-bound slot, which is the lie this guard prevents.
                same_binding = _requested.requested_resolved and _stored.same_dispatch_binding(
                    _requested
                )
            except Exception:
                resolution_failed = True
                logger.warning(
                    "agent-conflict binding resolution failed; using strict name comparison",
                    exc_info=True,
                )
            if state._slots.get(slot.key) is not slot or compared_binding != (
                slot.agent,
                slot.project,
                slot.memory_store,
                effective_session_key(slot),
                slot.workspace,
                slot._app,
                slot.agent_kind,
            ):
                return web.json_response(
                    {"error": "slot changed during agent resolution", "code": "session_rebound"},
                    status=409,
                )
            if not same_binding:
                _emit_agent_assignment(
                    slot.key,
                    agent or "",
                    outcome=(
                        "denied_resolution_failed" if resolution_failed else "denied_mismatch"
                    ),
                )
                return web.json_response({"error": "slot agent mismatch"}, status=409)
            # The one outcome of this guard that overrides a 409 boundary must be
            # auditable alongside the denials and adoptions it sits between.
            _emit_agent_assignment(slot.key, agent, outcome="allowed_same_binding")
            logger.debug(
                "agent names differ but resolve to the same binding: slot=%s stored=%s requested=%s",
                slot.key,
                slot.agent,
                agent,
            )
        else:
            logger.debug("agent match for slot=%s agent=%s", slot.key, agent)
    elif _send_binds_agent(agent, slot):
        # Slot has no agent — set it if not running
        if slot.running:
            _emit_agent_assignment(slot.key, agent, outcome="denied_running")
            return web.json_response(
                {"error": "cannot set agent on running slot"},
                status=409,
            )
        slot.agent = agent
        _emit_agent_assignment(slot.key, agent)
    else:
        # No agent on slot, no agent in request — nothing to enforce.
        pass

    if _send_writes_persona(body):
        slot.color_theme = color_theme
        slot.theme_consent = theme_consent
        slot.theme_consent_sha = theme_consent_sha

    if not message:
        # One guard, above every dispatch branch. An empty wire text reaches
        # here only from programmatic callers (app tokens, curl, integrations)
        # — the dashboard composer always inlines staged files into the
        # message text. Such a send may still carry attachments in `meta`:
        # nothing downstream queues or broadcasts it, so any success receipt
        # would report work that was silently dropped. Refusing here keeps
        # every branch below (steer/queue, crew, subagent-hold, new turn)
        # unable to bypass the check. A guard placed below the busy branch is
        # bypassable, and that is exactly how a false `queued: true` receipt
        # happens. `message_required` is the backend-owned code already used
        # for this refusal (handlers/messaging.py).
        return web.json_response(
            {"error": "message is required", "code": "message_required"}, status=400
        )

    if slot.turn_running or slot._turn_admission_reserved:
        # Mid-turn steer: inject into the RUNNING turn instead of queueing for
        # the next turn. Gated on an explicit `steer` flag + a live, steer-capable
        # inner AcpClient that _run_chat published on the slot. App-authenticated
        # sends cannot steer because doing so would inherit the live turn's human
        # provenance; they fall through to the fail-closed queue below.
        # Fire-and-forget —
        # the inline steer card materializes when kiro-cli echoes steering_consumed
        # (EVENT_STEER_CONSUMED). If steer is requested but unavailable (no live
        # client / unsupported backend / RPC error), fall through to the queue
        # path so the user's text is NEVER silently dropped.
        #
        # `steer: "auto"` is the composer's third mode: the sender asked Jev which
        # of the two shipped paths this message takes. Decided HERE, above both
        # branches, because the answer chooses between them -- and only here, where
        # a turn IS running (this branch's own condition) and the send is the
        # session's own human, are the point's preconditions already established.
        #
        # Resolved before the steer `if` rather than inside it, so the steer block
        # below keeps its exact shape: the only thing `auto` changes about it is one
        # more conjunct on its condition and the receipt it stamps.
        _auto_strip: dict | None = None
        _auto_queues = False
        if steer_is_auto(steer) and not request_app:
            # The turn the question is ABOUT, captured before the await. The
            # decision is a provider round-trip, so the turn it describes can end
            # while it is in flight -- and an answer about a turn that is gone is
            # not an answer about this send: "interrupt what it is doing" names
            # work that finished, and a successor turn is a different subject.
            # On a change the send takes the manual steer path (this branch's own
            # default) and carries NO receipt, because the decision that was made
            # is not about the turn the message now reaches. The row is still in
            # the log, where it belongs -- the receipt is what would misattribute
            # it.
            _turn_before = slot.task
            _auto_queues, _auto_strip = await decided_message_handling(slot, message)
            if slot.task is not _turn_before:
                _auto_queues = False
                _auto_strip = None
        if steer and not request_app and not _auto_queues:
            # Client-minted send correlation id (the same `meta.sendId`
            # convention the plain send path persists): thread it through the
            # steer so the persisted row and the steer_push echo can be matched
            # back to the optimistic bubble by id rather than by text.
            # Raw client input — the sink (`normalize_send_id` at the top of
            # `steer_into_running_turn`) type-checks and length-bounds it,
            # treating anything unusable as absent (the old-client shape).
            # circular import: session_control imports this package's modules at module level.
            from kiro_crew.dashboard.session_control import containment_meta as _containment_meta

            outcome = await steer_into_running_turn(
                state,
                slot,
                message,
                send_id=user_meta.get("sendId") if user_meta else None,
                # This branch IS the composer: the text was typed into this
                # session's own surface by its authenticated human, which is what
                # earns a requeued entry the exemption from the drain's LINKED drop.
                # Stated rather than defaulted, because the default fails closed.
                user_origin=not bool(request_app),
                # Captured HERE, before the RPC suspends, for the same reason the peer
                # path captures it: the requeue runs in the turn's teardown and a slot
                # read there folds a mirror linked during the suspension into the
                # entry's own admission baseline. The LINKED exemption does not cover
                # that -- a new outbound mirror is never exempt, because the author
                # does not control mirror links -- so the composer needs the stamp too.
                admission=_containment_meta(state, slot),
                # The receipt for an `auto` send that was decided; absent for a
                # manual steer, which is what keeps that row byte-identical.
                decision_strip=_auto_strip,
                attachments=user_meta,
            )
            if outcome == STEER_STEERED:
                return web.json_response({"ok": True, "steered": True})
            if outcome == STEER_REQUEUED:
                # The turn's teardown moved it into the queue while the steer RPC
                # was suspended — queueing again would deliver the same text twice.
                return web.json_response({"ok": True, "queued": True})
            # steer requested but unavailable -> fall through to queue below.
        # Queue the message - return JSON immediately (no SSE needed).
        # The existing SSE reader will pick up queued messages as _run_chat
        # processes the queue in its finally block. The message is non-empty
        # here (hoisted guard above the busy branch), so `queued: true`
        # always reports a real enqueue. `queue_id` lets the sender bind its
        # pre-send composer state to THIS entry (the dashboard's cancel-queued
        # restore), which no content-based key can do: serialization is not
        # injective and other tabs can queue colliding content.
        #
        # The client's `meta.sendId` rides on the entry too (same gate as the
        # steer path above: `normalize_send_id` treats anything unusable as
        # absent). The drain unions entry meta onto the row it writes, so the
        # queued send's row ends up carrying the same id a dispatched send's row
        # gets from `slot.append(..., meta=user_meta)` below -- the only way a
        # sender can prove ITS message landed without matching by text.
        # The attachment lists (`meta.files` / `meta.dirs`) ride the same way:
        # the renderer resolves `[attached_file N]` markers against them, and a
        # drained row without them truncates a spaced path at its first space.
        qid = queue_for_next_turn(
            state,
            slot,
            message,
            directive_user_origin=not bool(request_app),
            # A queued turn reaches the runner through the DRAIN, so the dispatch
            # keyword this handler passes for an IMMEDIATE send cannot carry the
            # actor here. It rides the entry's meta instead, which is what
            # `_actor_for_queue_items` reads; unstamped, the drain falls back to
            # `user` and files an app's send as a person's.
            turn_actor="app" if request_app else "",
            send_id=normalize_send_id(user_meta.get("sendId")) if user_meta else None,
            attachments=attachment_meta(user_meta),
            quote=quote_meta(user_meta).get("quote"),
            # The receipt travels whichever way the send went, including the one
            # case where the two disagree: `auto` answered steer and the steer was
            # UNAVAILABLE, so this path runs with a record saying steer. That is the
            # truth of the decision, and the row's own `steerState` is what says how
            # the delivery ended -- a receipt withheld there would lose the only
            # record that a decision was made at all.
            decision_strip=_auto_strip,
        )
        return web.json_response({"ok": True, "queued": True, "queue_id": qid})

    # Queue a message typed while background sub-agents are still running for
    # this slot. The slot.running queue path above covers the mid-turn case;
    # this covers the idle case (spawn_run is fire-and-forget, so the main slot
    # goes idle while children run). Without the hold, this message would start a
    # main turn immediately and interleave with the [Subagent completion event]
    # injections. Queue it instead (reusing the slot queue) — the queue drain
    # releases it after the last sub-agent finishes (see chat_runner _hold_users).
    # Opt-out: if the user explicitly chose steer mode, honour it — start a new
    # turn immediately so the message is processed without waiting for children.
    # An app on a session it does not own has no opt-out (`steer` is None there).
    _sub_key = effective_session_key(slot)
    _held = subagents_hold_user_messages(state, _sub_key)
    # Messages parked while a child was live stay parked once it is flagged
    # stalled (nothing starts a drain then). A send behind them joins the queue
    # and drains it, so it is answered after them rather than ahead of them.
    _behind_parked = bool(
        not _held
        and slot._queue
        and state.subagents is not None
        and state.subagents.running_agents_for(_sub_key)
    )
    if not steer and (_held or _behind_parked):
        # circular import: session_control imports this package's modules at module level.
        from kiro_crew.dashboard.session_control import containment_meta

        # Same entry-meta contract as the busy-slot branch: the client's `sendId`
        # and attachment lists ride on the queue entry so the drained row
        # carries them.
        _hold_meta: dict = containment_meta(state, slot)
        _hold_sid = normalize_send_id(user_meta.get("sendId")) if user_meta else None
        if _hold_sid:
            _hold_meta["sendId"] = _hold_sid
        _hold_attachments = attachment_meta(user_meta)
        _hold_meta.update(_hold_attachments)
        _hold_meta.update(quote_meta(user_meta))
        if request_app:
            # Same reason as the busy-slot queue above: this entry is drained
            # later, so only its meta can name the actor.
            _hold_meta[TURN_ACTOR_META_KEY] = "app"
        qid = slot.queue_append(
            message,
            meta=_hold_meta,
            directive_user_origin=not bool(request_app),
        )
        _redacted = queued_text_for_display(message, user_origin=not bool(request_app))
        warn_if_not_durable(slot._queue, qid, slot.key)
        # Start the durable write here too, not only in the busy-slot branch.
        # This branch holds an IDLE slot, so no drain is coming to write the
        # prompt's transcript row and no turn-end flush is scheduled: the queue
        # is the only record of the user's words until the last sub-agent
        # finishes, which is unbounded. Waiting for the periodic flush would
        # leave a window as wide as its interval, so the accept and the write
        # start from the same place. Same single-flight and same self-limiting
        # skip as the other caller.
        start_queue_persist(state, slot)
        _hold_push: dict[str, Any] = {
            "slot": slot.key,
            "content": _redacted,
            "ts": datetime.now(timezone.utc).isoformat(),
            "queue_id": qid,
        }
        if _hold_attachments:
            # Same as the busy-slot frame: the card is a cancel's restore source.
            _hold_push["meta"] = _hold_attachments
        if _hold_meta.get("quote"):
            _hold_push.setdefault("meta", {})["quote"] = _hold_meta["quote"]
        state.broadcast_ws("queue_push", _hold_push)
        if _behind_parked:
            await _start_next_queued_turn(state, slot)
        # Same receipt contract as the busy-slot queue branch: `queue_id` binds
        # the sender's pre-send composer state to this exact entry. An entry the
        # durable bounds refuse is reported in the log by the call above, not on
        # the receipt: the on-screen marker belongs with its consumer.
        return web.json_response({"ok": True, "queued": True, "queue_id": qid})

    # WS mode: return JSON immediately, chunks delivered via WebSocket
    ws_mode = request.query.get("ws") == "1"

    # Only block the global broadcast for an HTTP SSE reader that IS the slot's
    # own client. An app streaming a turn on a user's session is not the user's
    # dashboard, which keeps receiving its rows.
    slot._has_reader = not ws_mode and may_configure
    # That app's stream ends with its own turn (`TURN_END_WIRE_CLS`), so the
    # user's queued follow-ups never reach it.
    turn_scoped_stream = not may_configure
    slot._file_changes = []  # Reset file-change accumulator for the new turn
    # ── Sweep orphaned permissions from prior turns ──
    _sweep_stale_permissions(slot)

    # Refuse a relay archive BEFORE the send is recorded: a refusal that appended
    # first would leave a user row in local history for a turn that never ran.
    # Every turn-refusing validation above (app ownership, member reserve,
    # busy/steer/queue) has already run, so their answers still win.
    refusal = relay_archive_refusal(slot)
    if refusal is not None:
        return refusal

    # No per-message browse marker: browsing is a capability, not a per-turn
    # gate. The agent drives a browser by running `playwright-cli` shell
    # commands, so the capability is simply whether that binary is on PATH. The
    # agent itself decides whether to operate a browser or read with web_fetch
    # (the system prompt and the kirocrew-commands / web-browse skills tell it
    # how), so the backend injects nothing here.

    # A slot created by this send binds to its member's private store BEFORE
    # the user row is appended: a store failure then returns with nothing
    # persisted, and the assignment snapshot (agent, project, workspace,
    # session, message count) proves no other request rebound the slot while
    # the store was being resolved.
    if created_in_send:
        from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

        if is_owner_dashboard_request(request):
            async with slot._lock:
                assignment = (
                    slot.agent,
                    slot.project,
                    slot.workspace,
                    effective_session_key(slot),
                    len(slot.messages),
                )
                selection_change = None
                try:
                    cfg = await asyncio.to_thread(KiroCrewConfig.load)
                    assigned_store = await pin_private_agent_store(
                        state, assignment[3], assignment[0], cfg, memory_mode=slot.memory_mode
                    )
                    chosen = await asyncio.to_thread(
                        resolve_agent_bindings,
                        cfg,
                        assignment[0],
                        assignment[1] or None,
                        validate_memory_files=False,
                    )
                    selection_change = await _record_explicit_agent_selection(
                        assignment[3],
                        assignment[0],
                        chosen,
                        config=cfg,
                        memory_mode=slot.memory_mode,
                        app=slot._app or "",
                    )
                except Exception as exc:
                    from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

                    return _store_unavailable_response(slot.memory_store, exc)
                if (
                    state._slots.get(slot.key) is not slot
                    or slot.running
                    or assignment
                    != (
                        slot.agent,
                        slot.project,
                        slot.workspace,
                        effective_session_key(slot),
                        len(slot.messages),
                    )
                ):
                    await drained_to_thread(
                        restore_agent_selection, assignment[3], selection_change
                    )
                    return web.json_response(
                        {
                            "error": "Could not save the member assignment. Try again.",
                            "code": "session_rebound",
                        },
                        status=409,
                    )
                if assigned_store:
                    slot.memory_store = assigned_store

    # A dashboard's busy snapshot can suppress its optimistic user bubble even
    # when this send starts a turn. Echo correlated sends BEFORE starting the
    # reply so every pane sees the user row in order, independently of when the
    # HTTP receipt arrives. sendId/mid reconcile an existing optimistic bubble;
    # callers without a correlation id keep their existing delivery contract.
    _user_row_meta = _redact_meta(user_meta) if user_meta else {}
    if user_meta and not request_app and isinstance(user_meta.get("quote"), dict):
        # The human's own quote record stays as typed, like the row's content
        # it must byte-match (`_redact_meta_for_role` keeps the same rule on
        # every later persist and emit); an app's was redacted at the bound.
        _user_row_meta["quote"] = user_meta["quote"]
    if not request_app:
        # A PERSON typed this. Marked explicitly rather than inferred, because the
        # row's role and presentation class cannot tell it apart from a turn the
        # gateway drives on its own (see history.HUMAN_TURN_META_KEY). An app
        # token reaches this same handler, so the marker rides the same
        # app-origin signal as `user_origin` and `turn_actor` above: an app's
        # send is not a human turn and must not advance the ranking stamp.
        _user_row_meta[HUMAN_TURN_META_KEY] = True
    else:
        # The queued path stamps the same actor on its entry, which the drain
        # unions onto the row; the title counter reads it off either row.
        _user_row_meta[TURN_ACTOR_META_KEY] = "app"
    _user_row = slot.append("user", message, "msg msg-u", meta=_user_row_meta)
    _user_mid = _user_row.get("meta", {}).get("mid")
    if not may_configure or (ws_mode and user_meta and user_meta.get("sendId")):
        # Raw user content belongs on the per-client slot-authorized WS path.
        # The global SSE queues have no slot gate. In-band sends keep
        # their existing stream contract and must not gain an extra WS echo.
        # An app's turn on a user's session always reaches the user's open tabs,
        # whose composer never drew it.
        state.broadcast_ws(
            "chat_message",
            chat_message_frame({**_user_row, "slot": slot.key}, include_metadata=True),
        )

    # Note: untitled slots display as "New Session…" via _ChatSlot.display_title
    # (serialization layer), so there's no bare chat-N flash to patch here. The
    # LLM titling is kicked off below, before _run_chat.

    # ── AutoNudge: user input cancels any pending nudge timer (user wins). ──
    try:
        from kiro_crew.autonudge import (
            get_instance as _autonudge_get,  # circular: autonudge -> dashboard.chat -> chat_handlers
        )

        _autonudge = _autonudge_get()
        if _autonudge is not None:
            # A person typing also ends an approval hold. Only on positive
            # evidence of a dashboard user: an app, or the internal loopback
            # secret a cron script or MCP tool sends with, is not one.
            _autonudge.notify_user_input(
                slot.key,
                human=request.get("is_dashboard_user") is True
                and request.get("internal_auth") is not True,
            )
    except Exception:
        logger.warning("autonudge.notify_user_input failed", exc_info=True)

    # Drain stale pending messages from previous turns that completed
    # after their SSE reader disconnected. Must happen BEFORE _run_chat
    # so we don't discard the new turn's output.
    slot.drain()

    # Kick off LLM titling now, from the first user message, so the title lands
    # *during* the first turn instead of waiting for the whole response to
    # finish (chat_done). Runs on an isolated background kiro-cli session
    # concurrent with the turn. No-ops once titled / in-flight; the instant
    # 60-char provisional stays as the fallback if the LLM SKIPs or errors.
    # Not from an app's text on a user's session: the user's own turns name it.
    if may_configure and not slot._titled and not slot._title_in_flight:
        _tt = asyncio.create_task(_maybe_auto_title(state, slot))
        # Expose the handle so chat_done's chained title→refresh pass can wait
        # for this attempt to settle (see _title_then_refresh in chat_runner).
        slot._title_task = _tt
        state._background_tasks.add(_tt)
        _tt.add_done_callback(state._background_tasks.discard)

    # Auto-tag: derive a tag from the session's project directory (deterministic,
    # no LLM). Fire-and-forget, same pattern as auto-title.
    if not getattr(slot, "_auto_tagged", False):
        _at = asyncio.create_task(maybe_auto_tag(state, slot))
        state._background_tasks.add(_at)
        _at.add_done_callback(state._background_tasks.discard)

    _reserve_turn_admission = not request_app
    if _reserve_turn_admission:
        slot._turn_admission_reserved = True
    try:
        # Optional Slack thread for a NEW session (slack.auto_link_sessions), made
        # here rather than in the turn: the runner's echo below reads the link at
        # turn start, so the link has to exist before dispatch or the first message
        # never reaches the thread. Awaited, not fire-and-forget, for the same
        # reason; bounded inside. A link that lands after the hold still links,
        # and the thread receives the turns that start after it: this turn is
        # not replayed into it. Only a person's own send qualifies: an app-token
        # send is an injection into the slot, not the person opening it, and the
        # eligibility test rules out every non-dashboard origin besides.
        if _reserve_turn_admission:
            # Counts Stop presses; read before the await so one that lands inside it
            # is seen even though it found no task to cancel and settled at once.
            _stop_gen_before_hold = slot._stop_generation
            await maybe_auto_link_slack(state, slot)
            # The await above is the one suspension between accepting the row and
            # dispatching the turn; a close that lands inside it must not have its
            # turn run on the detached slot.
            if not slot_is_live(state, slot):
                return web.json_response(
                    {"error": "session closed", "code": "slot_closed"}, status=409
                )
            # A Stop pressed during the hold was aimed at this turn: it does not
            # start, and the row stays as a message that was stopped unanswered.
            # A send queued behind it during the hold starts now, as it would at
            # the end of a stopped turn.
            if slot._stop_generation != _stop_gen_before_hold:
                # The reservation stays up across the drain's awaits (the
                # `finally` below drops it), so a send arriving meanwhile queues
                # instead of dispatching a second turn beside the drained one.
                if slot._queue:
                    await _start_next_queued_turn(state, slot)
                return web.json_response({"ok": True, "slot": slot.key, "stopped": True})

        # Edition message observer (CPP seam). Fire-and-forget, fail-safe: a
        # companion uses this to auto-ingest doc links pasted into chat. The public
        # Default is a no-op. Guarded so an observer error never blocks the turn;
        # deferred context read via the sel.py pattern (no platform import at load).
        try:
            from kiro_crew.platform.context import current_context, safe_context_call

            safe_context_call(
                lambda: current_context().dashboard.on_user_message(request.app, message),
                fallback=None,
                log_message="dashboard.on_user_message observer failed",
            )
        except Exception:
            logger.debug("on_user_message observer raised; ignoring", exc_info=True)

        # An unattended app-owned turn runs under the background concurrency
        # cap; run_background_turn passes an attended slot straight through, so the
        # interactive path is unchanged (no semaphore is even created).
        # The attachment ids this handler just accepted, so the ledger names the file
        # instead of leaving the turn's input unexplained. Passed only when there ARE
        # some: an ordinary send then calls `_run_chat` with exactly the arguments it
        # always did, which is what keeps the many test doubles of it valid.
        _accepted_attachment_meta = attachment_meta(user_meta)
        _accepted_attachments = [
            path for paths in _accepted_attachment_meta.values() for path in paths
        ]
        # ``request_app`` is stamped by the app-token auth middleware, not read from
        # the request body, so it is a fact about the caller a person cannot write --
        # which is what lets the turn's actor come from it. Passing it is what keeps
        # an app-authored send out of the ledger's ``user`` bucket: the actor
        # resolver's fallback is ``user``, so a site that observes an app and stays
        # silent records a person who never typed anything.
        _turn_kwargs: dict = {"_directive_user_origin": not bool(request_app)}
        if request_app:
            _turn_kwargs["_turn_actor"] = "app"
        if _accepted_attachments:
            _turn_kwargs["_attachments"] = _accepted_attachments
            # Typed form for the refusal replay: keeps ``dirs`` entries as folders.
            _turn_kwargs["_attachment_meta"] = _accepted_attachment_meta
        task = spawn_guarded_turn(
            state,
            slot,
            state.run_background_turn(slot, _run_chat(state, slot, message, **_turn_kwargs)),
        )
        slot.task = task
    finally:
        if _reserve_turn_admission:
            slot._turn_admission_reserved = False
    slot.recovery_retrigger_count = 0
    state.push_slots_update()

    if ws_mode:
        # Carry the server-minted user-row `mid` back (see the append above). A
        # confirmed dashboard send reconciles it onto the optimistic bubble so
        # the message-pin control lights up immediately instead of only after
        # the chat_done refresh. Omitted when absent so the receipt shape is
        # unchanged for callers that never minted one.
        _receipt: dict[str, Any] = {"ok": True, "slot": slot.key}
        if _user_mid:
            _receipt["mid"] = _user_mid
        return web.json_response(_receipt)

    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    # Declare this reader as the owner of `slot._pending` for as long as it is
    # draining, from before the first await after dispatch. A turn-end chunk
    # release must not run while an SSE reader still has undelivered tokens
    # queued, and `_has_reader` alone cannot carry that: the `done` branch below
    # clears it before this scope ends, and a turn-scoped reader never sets it.
    with slot.pending_consumer():
        try:
            await resp.prepare(request)
        except BaseException:
            # Release the broadcast this reader suppressed; the dispatched turn
            # keeps running, exactly as it does when the reader disconnects
            # mid-stream.
            slot._has_reader = False
            raise
        try:
            while True:
                pending = slot.drain()
                for msg in pending:
                    # Fail closed even if a dispatch path misses turn_end: only
                    # this request's gateway-minted user row belongs to its SSE.
                    foreign_user_row = msg.get("role") == "user" and (
                        not _user_mid or row_mid(msg) != _user_mid
                    )
                    if msg["cls"] == "done" or (
                        turn_scoped_stream and (msg["cls"] == TURN_END_WIRE_CLS or foreign_user_row)
                    ):
                        await write_sse_or_abort(request, resp, b"data: [DONE]\n\n")
                        slot._has_reader = False
                        return resp
                    if msg["cls"] == TURN_END_WIRE_CLS:
                        # A boundary for turn-scoped readers, not a row.
                        continue
                    if turn_scoped_stream and msg.get("role") == "queued":
                        # A placeholder for a later turn (a cron notification or
                        # an MCP-App message queued meanwhile), never this one.
                        continue
                    chunk = _build_stream_chunk(msg)
                    await write_sse_or_abort(request, resp, f"data: {chunk}\n\n".encode())
                try:
                    await asyncio.wait_for(slot.event.wait(), timeout=30)
                except asyncio.TimeoutError:
                    await write_sse_or_abort(request, resp, b": keepalive\n\n")
        except (ConnectionResetError, ClientConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            slot.drain()
            slot._has_reader = False
    return resp


_source_link_unlink_tasks: set[asyncio.Task] = set()


async def api_chat_slot_summary(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot}/summary — intent summary for the panel.

    Read-only: it never triggers generation. Summaries are produced at turn end
    by the background pass, deliberately, so that opening the panel cannot spend
    tokens and repeated opening cannot turn into a refresh loop.

    Responses:
      - 200 with ``{enabled, generated_at, stale, intents, constraints, ...}``
      - 200 with ``intents: []`` and ``enabled: false`` when the feature is off,
        so the panel can render an explanatory empty state rather than an error
      - 404 ``slot_not_found`` for an unknown slot, or for a slot an app caller
        does not own (App Kit §5.2 isolation; 404 not 403 for anti-enumeration)
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    # App ownership check (App Kit §5.2), mirroring api_chat_slot_delete: a
    # summary is derived conversation content, so a slot merely existing must
    # not make it readable. Dashboard users carry an explicit empty request_app
    # and are unaffected; an app token may only read summaries for slots it
    # created, never for unscoped slots.
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_summary_read")
    if denied is not None:
        return denied

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    enabled = bool(cfg.session_summary.enabled)

    payload: dict | None = None
    stale = False
    log = state.conversation_log
    # Gate the cache read on the flag as well: turning the feature off has to
    # stop serving summaries, not just stop producing them, or a sidecar written
    # during an earlier opt-in keeps being returned after opt-out.
    if enabled and log is not None:
        payload, stale = await read_cached_intent_summary(log, slot)

    body: dict = {
        "enabled": enabled,
        "stale": stale,
        "intents": (payload or {}).get("intents", []),
        "constraints": (payload or {}).get("constraints", []),
        "generated_at": (payload or {}).get("generated_at"),
        "user_turns": (payload or {}).get("user_turns"),
        "last_activity": (payload or {}).get("last_activity"),
        "generate_state": _generate_state(cfg, slot),
    }
    return web.json_response(body)


def _generate_state(cfg: KiroCrewConfig, slot: Any) -> str:
    """Which on-demand affordance the panel should offer for *slot*.

    Three values, because the panel has three honest things to say and a bool
    could only carry two: ``ready`` (offer the button), ``too_few_turns`` (say so
    plainly and offer nothing -- a click could only fail), and ``unavailable``
    (the feature is off, a pass is already running, or the session is incognito
    and must never leave a durable artifact). Collapsing the last two into
    "not enough messages" would print a reason that is simply untrue for an
    incognito session.

    The turn count is an ESTIMATE from the slot's IN-MEMORY messages, not a
    transcript read: this runs on every panel mount and tab switch, and reading a
    thousand-message session from disk to answer a yes/no question is waste. A
    restored slot keeps only a window of its transcript, and the window is NOT a
    safe proxy for the whole session -- a tail made mostly of assistant replies
    and injected automation messages can hold fewer than the minimum genuine user
    turns while the file holds dozens. So `too_few_turns` is only claimed when the
    window IS the whole session (`_disk_older_count == 0`); a truncated window
    reports `ready` and lets the POST's disk-backed count decide.

    The authoritative gate lives in the generator and reads disk; if this estimate
    is wrong the POST refuses and says why, so the cost is a refused click, never
    a wasted call.

    A turn in flight is deliberately NOT one of these values, even though the
    generator refuses one. This field is only refreshed when a summary is written,
    so a state that begins and ends mid-turn would arrive stale and stay stale: a
    turn that ends without producing a summary (stopped, or gated by cadence)
    pushes no event, and the panel would sit on a dead verdict until it remounted.
    The panel already holds a live per-slot turn signal, so it owns that
    presentation and this field stays limited to what only the server knows.
    """
    if not cfg.session_summary.enabled:
        return "unavailable"
    if getattr(slot, "_summary_in_flight", False):
        return "unavailable"
    if is_incognito_transcript(getattr(slot, "memory_mode", "")):
        return "unavailable"
    turns = count_user_turns_in_records(getattr(slot, "messages", []) or [])
    if turns < cfg.session_summary.min_user_turns and not getattr(slot, "_disk_older_count", 0):
        return "too_few_turns"
    return "ready"


async def api_chat_slot_summary_generate(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/summary — summarize this session on request.

    The companion to the read-only GET. Generation stays off the read path so
    that opening the panel can never spend tokens; this route exists because the
    turn-end trigger alone leaves every session that predates the feature -- or
    that simply has not been touched since it was switched on -- permanently
    empty, with nothing a person can do about it from the panel.

    Explicit consent is the whole justification for the spend, so there is no
    batch form: one request summarizes one session.

    Responses:
      - 200 with the same body as the GET, once a summary exists
      - 409 ``summary_disabled`` / ``summary_in_flight`` / ``summary_unavailable``
        when no summary could be produced, so the panel can say which
      - 404 ``slot_not_found`` for an unknown slot, or one an app does not own
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    # Same App Kit §5.2 isolation as the GET: generating is strictly more
    # privileged than reading, so it can never be the laxer of the two.
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_summary_generate")
    if denied is not None:
        return denied

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    if request.get("app", "") and state._slots.get(name) is not slot:
        return slot_not_found()
    if not cfg.session_summary.enabled:
        return web.json_response(
            {"error": "session summaries are switched off", "code": "summary_disabled"},
            status=409,
        )
    log = state.conversation_log
    if log is None:
        return web.json_response(
            {"error": "no conversation log", "code": "summary_unavailable"},
            status=409,
        )
    # Reported separately from the generic failure because it is the one the
    # panel can explain as "already working" rather than "could not".
    if getattr(slot, "_summary_in_flight", False):
        return web.json_response(
            {"error": "a summary is already being written", "code": "summary_in_flight"},
            status=409,
        )
    # Likewise distinct: a turn in flight is a wait-and-retry, not a refusal. The
    # generator would decline anyway; saying so here keeps the panel from
    # reporting a transient state as a failure.
    if getattr(slot, "running", False):
        return web.json_response(
            {"error": "this session has a turn in progress", "code": "summary_turn_running"},
            status=409,
        )

    if request.get("app", ""):
        await generate_session_summary(
            state,
            slot,
            cfg=cfg,
            force=True,
            still_current=lambda: state._slots.get(name) is slot,
        )
        if state._slots.get(name) is not slot:
            return slot_not_found()
    else:
        await generate_session_summary(state, slot, cfg=cfg, force=True)

    # Read back rather than trusting the return value: a forced pass returns
    # False both when it produced nothing AND when the cached summary was
    # already current, and those are opposite outcomes for the panel. Through
    # the same gated read as the GET: a pass the generator skipped for
    # ``memory_mode`` must not be answered with a sidecar left over from the
    # key's earlier persistent life.
    payload, stale = await read_cached_intent_summary(log, slot)
    if request.get("app", "") and state._slots.get(name) is not slot:
        return slot_not_found()
    if payload is None:
        return web.json_response(
            {"error": "could not summarize this session", "code": "summary_unavailable"},
            status=409,
        )
    return web.json_response(
        {
            "enabled": True,
            "stale": stale,
            "intents": payload.get("intents", []),
            "constraints": payload.get("constraints", []),
            "generated_at": payload.get("generated_at"),
            "user_turns": payload.get("user_turns"),
            "last_activity": payload.get("last_activity"),
            "generate_state": _generate_state(cfg, slot),
        }
    )


#: Window rows a bounded read must NOT hand back. ``_TRANSIENT_ROLES`` documents
#: itself as being about a window-region DISK line
#: (``chat_persistence.py:1320-1322``) and ``chat_persistence.py:1571`` uses it that
#: way. A bounded read answers a different question — which WINDOW rows does the
#: client still need — and three of those roles are still needed. ``permission``:
#: a pending approval is actionable and the client reads it out of the transcript,
#: so dropping it hides the approval bar while the server is still waiting.
#: ``chunk``/``streaming``: ``_prepare_messages`` does not discard a chunk run, it
#: collapses one into a single ``streaming`` row, and that is the only way in-flight
#: assistant text reaches this endpoint — the client filters raw ``chunk`` itself.
#: ``done`` is discarded by ``_prepare_messages`` regardless, and ``queued`` stays
#: listed because the client rebuilds those bubbles from the payload's ``queue``.
_UNOWED_WINDOW_ROLES = _TRANSIENT_ROLES - {"permission", "chunk", "streaming"}


# Modes a slot may be CREATED with. A deliberate superset of the mode-SWITCH
# allowlist (chat_folders._VALID_MODES) and the fork override allowlist
# (chat_fork): "design-critique" is an app-worker mode assigned at birth by the
# Design Critique app's openSlot() — the custom mode keeps its throwaway dc-*
# slots off the chat sidebar, which renders only plain "" slots
# (ChatPage.tsx filteredSlots). Switching an existing session INTO an app-worker
# mode, or forking one with it as an override, is not a real flow, so those two
# allowlists deliberately stay narrower — do not "sync" them to this one.
_CREATABLE_MODES = ("", "design-critique")

# Deferral is an optimization, so a request shape added later must stay on the
# synchronous path until its publication ordering has been reviewed explicitly.
_DEFERRED_PLAIN_CREATE_KNOWN_KEYS = frozenset(
    {
        "name",
        "agent",
        "agent_kind",
        "model",
        "folder_id",
        "memory_mode",
        "mode",
        "title",
        "artifact",
        "ephemeral",
    }
)


def _default_template_stamp(cfg: KiroCrewConfig) -> str:
    """The name an agent-less slot is stamped with: the default custom agent (a
    template), as kiro-cli DECLARES it.

    Runs OFF the event loop (``asyncio.to_thread``) on purpose: ``agent.default_agent``
    may spell a spec's file stem, and :func:`dispatch_kiro_agent` can only warm a
    cold materialized snapshot when called off-loop -- on the loop it returns its
    input unchanged, which would stamp the stem during the boot window and strand
    the slot once the default moves on. The stamp outlives a later change of the
    default, so it has to be the declared name from the first write.
    """
    return dispatch_kiro_agent(default_template(cfg))


async def api_chat_slot_create(request: web.Request) -> web.Response:
    """POST /api/chat/slots — create a new chat slot."""
    state: DashboardState = request.app["state"]
    body, body_err = await read_bounded_json(request, allow_absent=True)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    # A session on a connected crew is created ON that crew and opened here
    # through its window; this hub never mints or adopts a relay binding.
    # Refused before anything is read or written, so a stale client cannot stamp
    # a remote executor onto a new local slot. The answer is the same for every
    # caller and every state, so it discloses nothing.
    if body.get("instance_id") or body.get("adopt_remote_slot"):
        return web.json_response(
            {
                "error": "sessions on a connected crew are created on that crew, not here",
                "code": "remote_create_retired",
            },
            status=400,
        )
    name = body.get("name")
    if name is not None and not isinstance(name, str):
        # `get_or_create_slot` normalizes the key with string operations, so a
        # non-string name would surface there as an unhandled 500.
        name = str(name)
    agent = body.get("agent", "")
    # The selection NAMESPACE, when the caller states one. "member" names a
    # configured crew, "template" a shared provider template; an omitted kind
    # keeps the legacy name-only resolution. The kind is selection input, never
    # authority: every owner, app and private-memory gate below still applies.
    agent_kind = body.get("agent_kind", "")
    if agent_kind not in ("", "member", "template"):
        return web.json_response(
            {"error": "invalid agent kind", "code": "invalid_agent_kind"}, status=400
        )
    # A palette command naming its app's own agent stamps the owning app here, so
    # every turn can refuse to run when the bare name resolves to any OTHER spec
    # (see _ChatSlot.app_agent_owner). Only meaningful beside an explicit template
    # pick of that agent; anything else is refused rather than half-applied.
    app_agent_owner_raw = body.get("app_agent_owner", "")
    app_agent_owner = ""
    if app_agent_owner_raw:
        from kiro_crew.apps.manifest import app_name_error

        if (
            not isinstance(app_agent_owner_raw, str)
            or app_name_error(app_agent_owner_raw)
            or not isinstance(agent, str)
            or not agent
            or agent_kind != "template"
        ):
            return web.json_response(
                {"error": "invalid app agent owner", "code": "invalid_app_agent_owner"},
                status=400,
            )
        app_agent_owner = f"{app_agent_owner_raw}/{agent}"
    model = body.get("model", "")
    # Folder membership at BIRTH. Assigning it afterwards (client PATCH) is
    # visibly too late: get_or_create_slot broadcasts the new slot before this
    # handler returns, so the dashboard renders it at the top level for a frame
    # or two and it then jumps into the folder. Validated exactly as
    # PATCH /api/chat/slots/{slot}/folder validates it.
    folder_id = str(body.get("folder_id") or "")
    if folder_id and not any(f["id"] == folder_id for f in state._folders):
        return web.json_response(
            {"error": "folder not found", "code": "folder_not_found"}, status=400
        )
    existing_slot = state._slots.get(_normalize_slot_key(str(name))) if name else None
    request_app = request.get("app", "")
    # Ownership of a NAMED slot for an app caller, before any refusal below can
    # answer about it (get_or_create_slot's memory-mode 409 among them) and
    # before anything is written. The post-create check stays authoritative.
    acquisition_judged: Any = None
    if request_app and name:
        denied, acquisition_judged = await _app_slot_acquisition_denial(
            request, state, request_app, str(name), "chat_slot_create", session_grant_route=False
        )
        if denied is not None:
            return denied
        existing_slot = state._slots.get(_normalize_slot_key(str(name)))
    memory_mode = body.get("memory_mode", "persistent")
    if memory_mode not in ("persistent", "incognito", "temporary"):
        return web.json_response({"error": "invalid memory_mode"}, status=400)
    _mode = _coerce_requested_mode(body.get("mode", ""))
    if _mode not in _CREATABLE_MODES:
        return web.json_response({"error": "invalid mode", "code": "invalid_mode"}, status=400)
    # A member-* name is RESERVED for DM threads (born only through the member
    # thread endpoint); `get_or_create_slot` below rejects it with a ValueError
    # that becomes a 409; refused here so the error carries its code. Checked on the
    # normalized key, the form the slot store is built from.
    if name and _normalize_slot_key(str(name)).casefold().startswith(
        members_mod.DM_SLOT_KEY_PREFIX
    ):
        return web.json_response(
            {
                "error": "member thread slots are created only via the member thread endpoint",
                "code": "member_slot_reserved",
            },
            status=409,
        )
    folder_project = ""
    if folder_id and (existing_slot is None or not existing_slot.project):
        folder_snapshot = await state.read_folders(
            lambda folders: [dict(folder) for folder in folders]
        )
        # The chain walk runs on the loop; only a declared project's ``stat``
        # hops to a worker thread (see `resolve_folder_project_dir_off_loop`).
        folder_project, folder_project_error = await resolve_folder_project_dir_off_loop(
            folder_snapshot, folder_id
        )
        if folder_project_error:
            return web.json_response(
                {
                    "error": f"invalid folder project: {folder_project_error}",
                    "code": "folder_project_invalid",
                },
                status=400,
            )
    # Binding resolution checks memory readiness. A fresh conversation gets
    # the same bounded recovery grace as its first turn, before any slot or
    # protected assignment is published.
    if existing_slot is None and is_owner_dashboard_request(request):
        try:
            await wait_for_memory_preparation(getattr(state, "memory_startup_task", None))
        except MemoryStartupUnavailable as exc:
            return web.json_response({"error": str(exc), "code": "store_unavailable"}, status=503)

    # Resolve workspace from agent bindings
    workspace = "default"
    cfg = None
    try:
        cfg = KiroCrewConfig.load()
    except Exception:
        # Infra failure loading config must not block slot creation outright, so
        # validation below is skipped rather than failing closed.
        logger.warning("Failed to load config for slot create", exc_info=True)
    # An agent-less create means "the default TEMPLATE": stamp it into the slot
    # in the template namespace instead of storing "", so the slot's metadata
    # records what will actually answer — otherwise the dashboard footer chip
    # renders its literal 'default' fallback while dispatch quietly resolves
    # the real default. The default template, never the `default` crewmate
    # alias: a plain session is a template on the default folder and
    # memory store, and stamping the alias made every plain chat that
    # crewmate's — its workspace, its model pin, its roster recency. Placed
    # BEFORE the normalization below so the stamped template also gets its
    # workspace resolved by the existing binding path.
    # Stamped as the name kiro-cli DECLARES (`_default_template_stamp`), not a
    # file stem `agent.default_agent` may spell.
    if cfg is not None and not agent:
        agent = await asyncio.to_thread(_default_template_stamp, cfg)
        agent_kind = "template"
    # Normalize an agent nothing will dispatch to the one that WILL answer.
    # Otherwise the name is stored verbatim and resolve_agent_bindings silently
    # falls back to the default agent: the sidebar advertises the requested agent
    # while a different one answers, with none of its tools. Storing the real
    # agent keeps the slot honest, and a caller that requires a specific binding
    # (an app panel verifying the returned agent) can see the mismatch instead of
    # discovering it turns later.
    if cfg is not None and agent:
        resolving_key = _normalize_slot_key(str(name)) if name else ""
        resolving_slot = state._slots.get(resolving_key) if resolving_key else None
        resolving_fields = (
            (
                resolving_slot.agent,
                resolving_slot.project,
                resolving_slot.workspace,
                resolving_slot.memory_store,
                resolving_slot._app,
                effective_session_key(resolving_slot),
            )
            if resolving_slot is not None
            else None
        )
        try:
            # Resolved in the STATED namespace, so a template pick takes the
            # template's workspace rather than a same-name member's. No project
            # scope here: a create carries no slot yet, and the catalog offers a
            # slot-less chat global rows only, so this is the whole choice set.
            bindings = await asyncio.to_thread(
                resolve_agent_bindings,
                cfg,
                agent,
                validate_memory_files=False,
                selection_kind=agent_kind,
            )
            workspace = _workspace_name_for_dir(cfg, bindings.workspace_dir)
            if agent_kind and not bindings.requested_resolved:
                # A stated namespace never falls back to whoever answers by
                # default -- and it is refused HERE, before get_or_create_slot,
                # so a refused create registers no slot. The legacy name-only
                # path below keeps its store-verbatim-and-log behaviour.
                return web.json_response(
                    {
                        "error": "the selected agent choice is not available",
                        "code": "agent_choice_unavailable",
                    },
                    status=409,
                )
            if not bindings.requested_resolved:
                # Log only — the requested binding is the user's intent and is
                # stored VERBATIM. Rewriting it to whatever currently answers was
                # destructive: the resolution behind that decision can be
                # momentarily stale while the overwrite is permanent, so a valid
                # binding could be silently rebound to the default forever, where a
                # verbatim name recovers as soon as it resolves. Surfacing the
                # effective agent to the UI is a separate, non-destructive change.
                logger.info(
                    "Slot %s requested agent %r, which currently resolves to %r",
                    name,
                    agent,
                    bindings.resolved_alias or "(default)",
                )
        except Exception:
            logger.warning("Failed to resolve bindings for slot create", exc_info=True)
        if resolving_key and (
            state._slots.get(resolving_key) is not resolving_slot
            or (
                resolving_slot is not None
                and resolving_fields
                != (
                    resolving_slot.agent,
                    resolving_slot.project,
                    resolving_slot.workspace,
                    resolving_slot.memory_store,
                    resolving_slot._app,
                    effective_session_key(resolving_slot),
                )
            )
        ):
            return web.json_response(
                {"error": "slot changed during agent resolution", "code": "session_rebound"},
                status=409,
            )
    # Resolved before the mint decision below: nothing may await between that
    # read and `get_or_create_slot`, and these are the two awaits this path adds.
    cron_creator = await cron_slot_creator(request)
    persisted_cron_slot = False
    persisted_cron_meta: dict[str, Any] = {}
    if cron_creator and name:
        refusal, persisted_cron_slot, persisted_cron_meta = await cron_creator_admission(
            request, state, str(name), cron_creator
        )
        if refusal is not None:
            return refusal

    # Whether this request will MINT a new live slot, decided before
    # get_or_create_slot runs. `name` can address an already-open slot, which
    # returns unchanged. A persisted key with no live slot still follows the
    # normal create path. Its saved title is handled separately below.
    # Computed on the normalized key, which is the key the slot store is built
    # from; an omitted or degenerate name is always a mint. The creator fence
    # already read any saved cron state before this decision.
    _requested_key = _normalize_slot_key(str(name)) if name else ""
    reopening_persisted_cron = bool(
        cron_creator
        and _requested_key
        and persisted_cron_slot
        and _requested_key not in state._slots
    )
    is_new_slot = not _requested_key or _requested_key not in state._slots
    # A named NEW app slot must not land on a transcript the app does not own:
    # the slot's first save would stamp the app onto that metadata line, which is
    # what /api/sessions trusts.
    if (
        is_new_slot
        and _requested_key
        and await _app_claim_refused(
            state, request_app, "chat_slot_create", (_history_key_for(_requested_key),)
        )
    ):
        return slot_not_found()

    # Coalesce every push inside into ONE broadcast at exit, so the first frame
    # any client sees already carries the folder, title, artifact binding and
    # project. Otherwise each of those is a separate post-create correction the
    # UI renders as a jump.
    #
    # A successful ordinary local New Chat is different: the POST response itself
    # carries the complete slot and the client inserts it before activating the
    # tab. Serializing every open slot before returning only makes that first paint
    # wait. The state-owned 10 ms handoff moves its coalesced full-list frame past
    # the response. Only known request keys may qualify; metadata-bearing,
    # app-owned, mode-specific and nonpersistent creates remain
    # synchronous. Errors before a successful handoff retain request-visible
    # publication semantics; delayed publication failures are logged and retried
    # once by the guarded callback.
    defer_plain_create_broadcast = bool(
        set(body).issubset(_DEFERRED_PLAIN_CREATE_KNOWN_KEYS)
        and is_new_slot
        and not request_app
        and not folder_id
        and not _mode
        and not body.get("title")
        and not body.get("artifact")
        and memory_mode == "persistent"
        and not body.get("ephemeral")
    )
    async with contextlib.AsyncExitStack() as creation_stack:
        request_deferred_flush = creation_stack.enter_context(state.suspend_slots_push())
        if request_app and _requested_key:
            denied = await _app_slot_acquisition_recheck(
                state, request_app, _requested_key, acquisition_judged, "chat_slot_create"
            )
            if denied is not None:
                return denied
            is_new_slot = _requested_key not in state._slots
        # The binding is stamped only on the owner's own fresh local create below;
        # any request that would skip that branch is refused, never left unbound.
        if app_agent_owner and not (
            is_new_slot and cfg is not None and is_owner_dashboard_request(request)
        ):
            return web.json_response(
                {"error": "invalid app agent owner", "code": "invalid_app_agent_owner"},
                status=400,
            )
        try:
            slot = state.get_or_create_slot(
                name,
                agent=agent,
                workspace=workspace,
                model=model,
                mode=_mode,
                memory_mode=memory_mode,
                ephemeral=body.get("ephemeral"),
                app=request.get("app", ""),
                origin=request_slot_origin(request.get("app", ""), cron_creator=cron_creator),
                # Human request-layer path: the dashboard new-chat tab. The
                # origin conjunct in state.py still excludes app-token callers.
                # A slot an attested cron opens is not a person's and is not
                # counted.
                count_user_session=not cron_creator,
            )
        except ValueError as exc:
            if request_app:
                return _app_acquisition_conflict(
                    state,
                    request_app,
                    _normalize_slot_key(str(name)) if name else "",
                    acquisition_judged,
                    exc,
                )
            return web.json_response({"error": str(exc)}, status=409)
        if is_new_slot and agent_kind and not slot.agent_kind:
            # The namespace the name was stated (or stamped) in travels with the
            # slot from birth, for every caller: the owner branch below re-records
            # it from the committed resolution, and a non-owner create would
            # otherwise leave a template-stamped slot reading as a bare name.
            slot.agent_kind = agent_kind
        if reopening_persisted_cron:
            # The creator fence already loaded and authorized this metadata
            # snapshot. Reuse it so a closed-session reopen restores the user's
            # saved title provenance without a second store read.
            persisted_title = persisted_cron_meta.get("title")
            if isinstance(persisted_title, str) and persisted_title:
                _rehydrate_slot_title(
                    slot,
                    persisted_title,
                    titled=True,
                    metadata=persisted_cron_meta,
                )
                _rebase_rehydrated_refresh_mark(slot)
        if cron_creator and is_new_slot:
            # Same attribution as the send auto-create: a cron-opened slot
            # carries its creator and never reads as a person's own tab.
            slot._created_by = cron_creator
        if is_new_slot and cfg is not None:
            if is_owner_dashboard_request(request):
                # Take the slot lock before the newborn's first await: a later
                # same-name member pick changes its namespace without changing
                # the agent/project strings checked after publication.
                await creation_stack.enter_async_context(slot._lock)
        if slot.is_restricted:
            logger.info("Slot %s created with memory_mode=%s", slot.key, slot.memory_mode)
        # App ownership check (App Kit §5.2), same deny-by-default rule as
        # api_chat_send. It matters HERE because `name` can address an
        # ALREADY-EXISTING slot: get_or_create_slot returns that slot without
        # consulting ownership, and everything below mutates it (folder, title,
        # artifact binding). Without this an app token could refile or retitle
        # another app's — or the dashboard's — session. A slot this request just
        # created carries `_app == request_app`, so it passes unless
        # get_or_create_slot linked it to another conversation's session (the
        # session half, same rule as every per-slot route); a dashboard caller
        # (empty app) keeps full access.
        # `request_app` is read once at the top of the handler, because the remote
        # binding gate up there needs the same value before the peer is touched.
        # One body for every reason on purpose: a distinct code per reason would
        # turn this 404 into an existence oracle for slots the caller may not
        # know about. The reason stays in the audit row.
        denied = deny_app_slot_session_access(request_app, slot, slot.key, "chat_slot_create")
        if denied is not None:
            return denied
        # Pin title if explicitly provided (prevents auto-title from overwriting).
        # A cron caller uses this route as both create and reopen. Its title names
        # the new session, but a later reopen must not undo a user's rename.
        title = (body.get("title") or "").strip()[:200] if isinstance(body, dict) else ""
        if cron_creator and (not is_new_slot or reopening_persisted_cron):
            title = ""
        if title:
            title, _ = redact_exfiltration_urls(title)
            title, _ = redact_credentials(title)
            slot.title = title
        if title:
            # A pinned title is caller-explicit: record origin "user" so the
            # background title refresh never rewrites it (this endpoint can
            # address an ALREADY-auto-titled slot whose origin would otherwise
            # stay "auto"), and bump the epoch so an in-flight background
            # attempt stands down instead of clobbering the pin.
            slot._titled = True
            slot._title_origin = "user"
            slot._title_epoch += 1
        # Bind to an artifact if provided (companion chat). Validate
        # against the artifact slug grammar so an injection-shaped value can never
        # land on the slot; anything invalid is silently dropped. Uniqueness (≤1
        # active bound session per slug) is a frontend-flow convention, not
        # enforced here.
        artifact_slug = body.get("artifact") if isinstance(body, dict) else None
        if isinstance(artifact_slug, str) and ARTIFACT_SLUG_RE.match(artifact_slug):
            slot._artifact = artifact_slug
        # File the slot before the coalesced broadcast, so its first appearance
        # in every client is already inside the folder.
        folder_applied = False
        if folder_id:
            # Mirror PATCH /api/chat/slots/{slot}/folder: a CHANGED folder must
            # re-inject the [FOLDER] breadcrumb on the next turn. `is_new` alone
            # is not enough — `name` can address an already-used slot, whose
            # turn is `is_new=False`, so moving it would otherwise leave the
            # model believing the session is still in its old folder.
            # Harmless on the new-slot path: that turn is `is_new`, so the
            # breadcrumb fires regardless and the flag is consumed there.
            previous_folder = slot.folder_id
            previous_changed = slot._folder_changed
            if folder_id != slot.folder_id:
                slot._folder_changed = True
            slot.folder_id = folder_id
            # Existence is only reliable inside the store lock. If the folder
            # went away, abandon THIS assignment and leave the slot as it was —
            # `name` can address an already-used slot, so clearing outright would
            # unfile a conversation that was sitting in a perfectly good folder
            # of its own. This is a chat turn, so declining the move beats
            # failing the turn. A person opening a chat in the folder (no
            # internal secret: the browser) claims it, so an agent's
            # chat_folder_delete refuses it from then on.
            if not await _unhide_folder(
                state,
                folder_id,
                claim_for_person=(
                    folder_id != previous_folder
                    and request.headers.get("X-Internal-Secret") is None
                ),
            ):
                slot.folder_id = previous_folder
                slot._folder_changed = previous_changed
            else:
                folder_applied = True
                if is_new_slot:
                    # Folder-tag inheritance, creation-only. A brand-new
                    # chat filed into a folder copies that folder's tags by value onto
                    # its own tag list — the folder's tags are an organizational
                    # default for chats started inside it. Follows chat_fork.py's
                    # copy-by-value style.
                    #
                    # Gated on is_new_slot so re-opening an existing session inside
                    # the folder never re-stamps tags, and confirmed only after
                    # _unhide_folder reported the folder EXISTS (its read is under the
                    # store lock, the only race-free place to look it up). Direct
                    # folder only: no ancestor/subfolder transitivity. Ids are
                    # re-validated against the live vocabulary and appended only when
                    # not already present, so a stale id on the folder is dropped
                    # rather than written onto the slot.
                    def _read_folder_tags(folders: list[dict[str, Any]]) -> list[str]:
                        f = next((x for x in folders if x["id"] == folder_id), None)
                        tags = f.get("tags") if f else None
                        return list(tags) if isinstance(tags, list) else []

                    # One shared definition of "an inheritable folder tag id"
                    # (string, in the live vocabulary) — see validate_folder_tag_ids
                    # for why each guard exists. The READ, the intersection AND the
                    # apply all sit under tags_write_lock (the invariant every
                    # consumer follows, matching the channel-filing path): a folder
                    # PATCH or tag deletion committing after an earlier read would
                    # otherwise stamp a stale tag set or resurrect a deleted id onto
                    # the new slot. Lock ordering (tags_write_lock → folder-store
                    # lock) matches the folder create/PATCH paths.
                    async with tags_write_lock(state):
                        inherited = await state.read_folders(_read_folder_tags)
                        appended = False
                        for tid in validate_folder_tag_ids(inherited, state):
                            if tid not in slot.tags:
                                slot.tags.append(tid)
                                appended = True
                        # "tags changed => revision changed": the awaited folder
                        # read above is a window in which a concurrent slots GET
                        # can snapshot the empty newborn under its birth revision;
                        # the inherited list must not ship under that same one.
                        if appended:
                            _bump_slot_tags_revision(slot)
        # A slot with no project filed into a project-linked folder inherits
        # from the nearest configured ancestor before its first broadcast. The
        # server owns this fallback because the client folder cache can be
        # temporarily stale; existing named slots with an explicit project keep
        # it and continue to use the project endpoint for scope changes.
        if folder_project and folder_applied and not slot.project:
            slot.project = folder_project
        # A fresh folder per new chat, when the owner turned it on. Only for a
        # brand-new unnamed dashboard chat: a named create can reopen a saved
        # session, and apps, crons and ephemeral helpers keep their own rules. A
        # project-linked folder (applied above) wins, an artifact companion chat
        # keeps the default, and a dashboard caller opts out with
        # `no_session_folder` when it sets the project itself right after the
        # create or opens a purpose-built chat (an app, an intake, a hand-off).
        # A refused root or a failed create falls through to the default below
        # and is reported in the response, so the chat still opens and the user is
        # told why it has no folder of its own.
        session_folder_notice = ""
        session_folder_root = getattr(cfg.dashboard, "session_folder_root", "") if cfg else ""
        if (
            isinstance(session_folder_root, str)
            and session_folder_root
            and not slot.project
            and is_new_slot
            and not _requested_key
            and body.get("no_session_folder") is not True
            and not body.get("artifact")
            and not request_app
            and not cron_creator
            and not body.get("ephemeral")
            and is_owner_dashboard_request(request)
        ):
            try:
                slot.project = await asyncio.to_thread(create_session_folder, session_folder_root)
            except SessionFolderError as exc:
                logger.warning("Slot %s: no session folder (%s)", slot.key, exc)
                session_folder_notice = "session_folder_unavailable"
        # Default project to workspace directory so file search works out of the box
        if not slot.project:
            cfg_proj = cfg.dashboard.default_project if cfg else ""
            if isinstance(cfg_proj, str) and cfg_proj:
                resolved = os.path.realpath(os.path.expanduser(cfg_proj))
                eligible = os.path.isdir(resolved) and not is_sensitive_path(resolved)
                if eligible:
                    # A configured default that overlaps the data home
                    # would be refused at spawn anyway — skip it here like
                    # a sensitive path, falling back to the workspace
                    # default instead of wedging every new slot. Off the
                    # loop, because the shared scan primes runtime paths
                    # (realpath/mkdir) on first use.
                    eligible = (
                        await asyncio.to_thread(voice_runtime_workspace_conflict, resolved)
                    ) is None
                cfg_proj = resolved if eligible else ""
            else:
                cfg_proj = ""
            slot.project = cfg_proj or default_project_dir(workspace)
        if is_new_slot and cfg is not None:
            if is_owner_dashboard_request(request):
                assignment_key = effective_session_key(slot)
                assignment_agent = slot.agent
                assignment_project = slot.project
                await creation_stack.enter_async_context(_slot_switch_session_lock(assignment_key))
                selection_change = None
                try:
                    # An explicit template choice is the shared template even when
                    # a member carries the same name: it never pins member memory.
                    assigned_store = (
                        ""
                        if agent_kind == "template"
                        else await pin_private_agent_store(
                            state, assignment_key, agent, cfg, memory_mode=slot.memory_mode
                        )
                    )
                    chosen = await asyncio.to_thread(
                        resolve_agent_bindings,
                        cfg,
                        assignment_agent,
                        assignment_project or None,
                        validate_memory_files=False,
                        selection_kind=agent_kind,
                    )
                    # Availability was settled before the mint; this records
                    # the namespace the pick was committed in.
                    slot.agent_kind = chosen.selection_kind
                    if app_agent_owner:
                        slot.app_agent_owner = app_agent_owner
                    selection_change = await _record_explicit_agent_selection(
                        assignment_key,
                        assignment_agent,
                        chosen,
                        config=cfg,
                        memory_mode=slot.memory_mode,
                        app=slot._app or "",
                    )
                except Exception as exc:
                    from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

                    return _store_unavailable_response(slot.memory_store, exc)
                if (
                    state._slots.get(slot.key) is not slot
                    or effective_session_key(slot) != assignment_key
                    or slot.agent != assignment_agent
                    or slot.project != assignment_project
                ):
                    await drained_to_thread(
                        restore_agent_selection, assignment_key, selection_change
                    )
                    return web.json_response(
                        {
                            "error": "Could not save the member assignment. Try again.",
                            "code": "session_rebound",
                        },
                        status=409,
                    )
                if assigned_store:
                    slot.memory_store = assigned_store
        _sync_dashboard_slots(state)
        # Persist INSIDE the suspension, ahead of the coalesced broadcast, the
        # same ordering `session_control.py`'s create span uses ("the whole
        # allocation-to-persist span runs under `suspend_slots_push`", so "a
        # slot whose birth write fails is never broadcast at all"). Two paths
        # mint a slot through this context manager; leaving the durable write
        # outside it is what makes the failure below reachable:
        #
        # `suspend_slots_push`'s `__exit__` flushes the owed push, and on the
        # coalescing window's LEADING edge that flush broadcasts synchronously
        # (`state.push_slots_update`). An exception there — a non-serializable
        # value reaching `json.dumps` is the evidenced shape —
        # escapes `__exit__`, so with the write out here it skipped a metadata
        # mutation the request had already acknowledged: this `force=True` save
        # is the ONLY durable record of a recreate's folder filing or pinned
        # title (see `_save_slot_to_history`'s message-less merge), and the slot
        # itself survives in memory, so nothing later reconciles the two. Every
        # client repairs a dropped FRAME on its next read; none of them repairs
        # a write that never happened.
        #
        # The cost this ordering accepts, named by the comment it replaces: the
        # suspension is process-wide, so other clients' slot updates coalesce
        # (they defer — no caller blocks) until this off-loop write completes,
        # and a contended history lock takes the patient acquire. Accepted for
        # the same reason the twin accepts it, which awaits a cross-process
        # metadata write inside its own suspension; this span already suspends
        # on the workspace-conflict probe above, so it was never await-free.
        #
        # A pinned title must persist too (not just a folder move): without the
        # write, a restart rehydrates the previous title with a refreshable
        # "auto" origin and the background refresh may rewrite the pin.
        if app_agent_owner:
            # The app-agent binding is what makes every turn re-check the agent,
            # and nothing else records it: the selection record omits it, and a
            # message-less newborn has no metadata line, so the save path's
            # empty-window merge writes nothing and a restart would restore the
            # slot unbound. The birth line is written here, awaited and checked,
            # and the create fails rather than hand the palette a session that
            # would lose its binding. The newborn is retracted on that failure,
            # the same retraction `session_control.py`'s create uses: left in
            # `state._slots` it would still go out in the coalesced broadcast
            # this suspension owes, so every client would show a session the
            # caller was told did not get created, a retry would mint a second
            # one, and -- message-less, so the dirty re-save writes no line -- a
            # restart would bring it back without its binding. Retracted only
            # while it is still provably the newborn: a turn that started on it
            # during the write keeps the slot (liveness over tidiness).
            try:
                persisted = await _persist_app_agent_owner(state, slot)
            except asyncio.CancelledError:
                # A shutdown mid-write is the same unbound newborn; retract it on
                # the way out, as the twin does.
                if not slot.running and not slot.messages and state._slots.get(slot.key) is slot:
                    state._slots.pop(slot.key, None)
                raise
            if not persisted:
                if not slot.running and not slot.messages and state._slots.get(slot.key) is slot:
                    state._slots.pop(slot.key, None)
                    # The pick recorded above and the dashboard-tab registry both
                    # name a key no slot holds now; undo them with the slot, as
                    # the `session_rebound` branch does.
                    await drained_to_thread(
                        restore_agent_selection, assignment_key, selection_change
                    )
                    _sync_dashboard_slots(state)
                else:
                    slot._dirty = True
                return web.json_response(
                    {
                        "error": "Could not save the new session. Try again.",
                        "code": "app_agent_owner_unpersisted",
                    },
                    status=503,
                )
        if folder_id or title:
            # The create/recreate request has been authorized against this
            # transcript.  Do not let a rebind while the off-loop write waits on
            # the history lock redirect its newly supplied metadata to another
            # session.
            #
            # No slot retraction on failure, unlike the twin: there the write is
            # the newborn's only record, so an unpersisted slot would vanish on
            # restart and retracting is the lesser evil. Here the slot already
            # has a metadata line and `best_effort` (default) logs the failure
            # and marks the slot dirty so the periodic flush retries it, which
            # is the retry the metadata mutation routes rely on.
            await save_slot_off_loop(
                state,
                slot,
                force=True,
                expected_history_key=slot_history_key(slot),
            )
        # Guarantee a frame. get_or_create_slot pushes for a NEW slot, but
        # returns an existing named slot without pushing — and this handler is
        # now the only thing that files a slot (the client sends no follow-up
        # PATCH to supply that push). Without this, re-creating an
        # existing slot name with a different folder_id would move it for the
        # requester while every other connected client kept the stale
        # placement. Inside the suspension this only marks a push owed, so the
        # new-slot path still emits exactly ONE coalesced frame.
        state.push_slots_update()
        if defer_plain_create_broadcast:
            request_deferred_flush(f"create slot {slot.key!r}")
    # Speculative session creation: overlap the ACP handshake with the user's
    # think-time before their first message. No-op unless session.eager_spawn.
    #
    # Skipped for a relay archive: it never runs, so a spawned kiro-cli would
    # idle until it timed out.
    if slot.executor != "remote":
        schedule_eager_spawn(state, slot, start_priority=owner_start_priority(request))
    created = state.serialize_slot(slot)
    if session_folder_notice:
        created["session_folder_notice"] = session_folder_notice
    return web.json_response(created)


def _reject_pending_approvals(slot: _ChatSlot) -> None:
    """Reject all pending approval futures so the chat runner unblocks.

    When a stop/interrupt is triggered while the agent is waiting for tool
    approval, the chat runner is suspended on the approval future. Without
    resolving it, the stream generator stays paused, _turn_done never fires,
    and the cooperative cancel times out — forcing a hard kill.

    Resolving the future is not enough on its own: the ``permission`` message
    the UI renders the approval bar from must ALSO be marked resolved.
    Otherwise the future is gone while the message still reads pending, so the
    bar survives a history reload and every button on it answers
    ``404 no pending approval`` — an approval card the user cannot action.
    """
    for aid, fut in list(slot._approval_futures.items()):
        if not fut.done():
            # Mark BEFORE resolving. Resolving can wake the runner immediately,
            # and the runner reads this set where it records who decided; marking
            # after would race its own reader. The provenance is already known
            # here -- the SEL line below calls it ``rejected_on_stop`` -- and the
            # resolved value stays a plain "rejected" so no caller of this future
            # has to learn a new one.
            slot._approval_stopped.add(aid)
            fut.set_result("rejected")
            if _mark_permission_resolved(slot.messages, aid, "rejected"):
                slot._dirty = True
            sel().log_tool_invocation(
                session_key=effective_session_key(slot),
                agent=getattr(slot, "agent", "") or "kirocrew",
                source="dashboard",
                tool_name=f"approval_reject:{aid}",
                tool_kind="permission",
                outcome="rejected_on_stop",
            )


class _HandoverDrainResult(NamedTuple):
    """What a hand-over drain did with the state it is the last reader of.

    ``rows_committed`` answers for the transcript rows: True when nothing was
    owed or the write committed, False when rows were owed and did not reach
    disk. ``prompts_lost`` counts the durable-eligible queued prompts whose only
    copy dies with the popped slot — zero when none were owed or when the
    durable line holds them, whichever writer put them there.

    A NamedTuple is always truthy, so a bare ``if not result:`` silently passes
    over a failed drain. Read ``rows_committed``; never test the result itself.
    """

    rows_committed: bool
    prompts_lost: int


async def _owed_prompts_lost_on_line(
    state: DashboardState, name: str, slot: _ChatSlot, history_key: str
) -> int:
    """Count owed queued prompts with no durable future on the shared line.

    The per-slot persistence signature answers "did THIS slot's last commit
    carry the queue"; it cannot see a replacement's own full save rebuilding
    the shared line and clearing ``queued_prompts`` (an owned field, so an
    emptied queue is cleared by absence — and ``POST /api/chat/slots`` persists
    at birth, inside the very window a hand-over spans). Nor is a point-in-time
    read of the line enough on its own: a line still showing the ORIGINAL's
    entries is one full save away from losing them whenever a live
    transcript-sharing holder exists, because that holder's save rebuilds the
    whole ``queued_prompts`` value from its own queue. So survival demands a
    stable owner, not a lucky read:

    * a live holder shares this transcript — an owed entry survives only when
      that holder's own durable queue carries it (a rehydrated holder restores
      the entries as queue cards and re-persists them; a fresh recreate does
      not), because the holder's next save decides the line;
    * no live sharing holder — the line is at rest, so an entry it holds stays
      until an ordinary restore hands it back.

    A line that cannot be read cannot prove survival, so every owed entry
    counts as lost: over-reporting is recoverable by the reader, while silence
    over a real loss is the failure this count exists to end.

    The store read is synchronous file I/O, so it goes through
    ``drained_to_thread`` rather than running on the gateway event loop —
    the same seam every other blocking read this module performs takes.
    """
    owed = slot.durable_queue_entries()
    if not owed:
        return 0
    holder = state._slots.get(name)
    if holder is not None and _replacement_shares_transcript(state, name, slot):
        surviving = {entry.get("id") for entry in holder.durable_queue_entries()}
        return sum(1 for entry in owed if entry.get("id") not in surviving)
    log = state.conversation_log
    if log is None:
        return len(owed)
    persisted, readable = await drained_to_thread(log.get_metadata_status, history_key)
    if not readable:
        return len(owed)
    on_line = persisted.get("queued_prompts")
    if not isinstance(on_line, list):
        return len(owed)
    line_ids = {entry.get("id") for entry in on_line if isinstance(entry, dict)}
    return sum(1 for entry in owed if entry.get("id") not in line_ids)


def _report_lost_queued_prompts(
    state: DashboardState, name: str, count: int, history_key: str
) -> None:
    """Log and post the user-visible notice that a hand-over lost queued prompts.

    The gateway log alone is not reachable by the person whose words were
    dropped; the notification feed is, so the loss is told in both. The body
    carries the COUNT and the slot, never the prompt text: the entries may
    belong to a restricted session, and a notice about losing words must not
    be the thing that leaks them.

    Failure to deliver must not fail the drain — the hand-over has to complete
    for the replacement holding the key either way — so this swallows and logs,
    the same posture every other lifecycle notice takes.
    """
    logger.warning(
        "Slot %s: %d queued prompt(s) were not carried by the hand-over write to "
        "%s; they are lost with the original slot",
        name,
        count,
        history_key,
    )
    try:
        state.notify(
            "agent",
            "Queued prompts lost in a tab hand-over",
            (
                f"{count} queued prompt(s) on tab {name!r} could not be carried "
                f"to {history_key} when the tab was replaced mid-close; they are "
                "not recoverable."
            ),
            meta={"slot": name, "count": count, "history_key": history_key},
        )
    except Exception:
        logger.error(
            "Slot %s: the lost-queued-prompts notification failed to deliver; "
            "the gateway log is the only remaining report of the loss",
            name,
            exc_info=True,
        )


async def _persist_handover_tail(
    state: DashboardState, name: str, slot: _ChatSlot
) -> _HandoverDrainResult:
    """Write a handed-over original's still-unsaved rows before its object is dropped.

    A teardown that yields ``name`` to a concurrent same-key recreate stops
    referencing the original slot: it is out of ``state._slots``, and the periodic
    flush iterates exactly that map, so nothing retries the write for it. Anything
    the original held past its last commit — ``messages[_disk_window_len:]``, plus
    any note the cleanup path is still carrying in ``_deferred_notes`` — would be
    unreachable and never persist. Those rows belong to the ORIGINAL's own
    transcript, whether or not the replacement happens to share it, and this frame
    is the last moment anything can put them there.

    The target is ``slot_history_key(slot)``, never ``_history_key_for(name)``. A
    slot carrying a ``linked_session_key`` — cron-, channel- or workflow-injected —
    stores its conversation under that key, and the forced save resolves its own
    write target the same way and REFUSES the whole write when the caller's
    ``expected_history_key`` names a different transcript. Authorizing
    ``dashboard:{name}`` there would make this drain a silent no-op for exactly the
    slots whose transcript is shared with something outside the dashboard, and would
    name a row-less file in the report.

    Deliberately NOT ``closed=True``. What this frame is finishing is the close of
    the ORIGINAL; the KEY is open, because a live replacement holds it, so the
    durable line has to say so. Stamping ``closed`` on a key someone is still using
    is the harm the surrounding guard exists to prevent. Open-shaped is not the same
    as un-closing, though: on a line the replacement published, ``closed`` is that
    holder's own dismissal, so the save defers it rather than erasing it (see
    ``ROWS_ONLY_OWNED_META_KEYS``). It is only on a line THIS slot published — where
    there is no other holder's flag to lose — that the write clears a stale
    ``closed`` an earlier close of the reused key left behind.

    Non-destructive against the replacement's own rows in both directions. The save
    re-serializes the ORIGINAL's window over the on-disk window region, and the
    save's foreign-append scan classifies every on-disk line that window does not
    represent as another writer's append and carries it through verbatim, so rows a
    replacement already committed survive.

    ``rows_only``, and that is the whole of what this frame claims. The rows are
    owed to the transcript; the METADATA line may not be this slot's to move.
    ``_save_slot_to_history`` is otherwise authoritative for every
    ``SLOT_OWNED_META_KEYS`` field and REBUILDS the line from whichever slot it is
    handed, so a default save here would revert a folder, pinned title, tag or pin
    the replacement had already published (``POST /api/chat/slots`` persists both at
    birth) — silently undoing an acknowledged edit, and for a tab nobody types in
    again undoing it for good. ``rows_only`` keeps the on-disk value for each of
    those — the close flags included — and leaves this write owning only the file's
    identity and accounting, which it carries forward from disk anyway.

    It rides with the write rather than being a caller's choice because the save
    scopes the deferral itself, by the line's ``tab_id``: it holds back only fields
    on a line ANOTHER slot published, and rebuilds normally from a line this slot
    published or from no line at all. That distinction is what keeps the flag from
    costing the original its own uncommitted metadata — a rename, re-file, tag or
    pin is acknowledged the moment it lands in memory and persists on a later
    flush, and this frame is past the pop, so no flush will ever visit this slot
    again.

    Returns a :class:`_HandoverDrainResult`. ``rows_committed`` is True when
    nothing was owed or the write committed, False when rows were owed and did
    not reach disk. Callers MUST honour it: nothing in the process can reach
    these rows again, so a caller that discards the answer reports a close that
    succeeded while the rows became unreachable. The log line names the exact
    count for the same reason.

    ``prompts_lost`` is the other half of the answer. A committed write can
    still defer the queue (the metadata line belongs to the replacement holding
    this key), a failed one leaves the line as it was, and a replacement's own
    full save can rebuild the shared line without the original's entries at any
    moment it remains alive — so survival is judged by who writes the line
    next (see :func:`_owed_prompts_lost_on_line`), not by this slot's
    persistence signature, which only answers for this slot's own last commit.
    An owed entry with no durable future dies with the popped object; the count
    comes back where a caller can surface or tally it, and the drain posts a
    dashboard notification alongside the warning log — the log is not reachable
    by the person whose words were dropped. Carrying the prompts instead would
    mean making ``queued_prompts`` a merge field on the rows-only path, which is
    a change to what a durable metadata line MEANS for a key two slots share;
    the write stays as it is, and the loss is reported rather than silent.
    """
    try:
        tightening = _tighten_replacement_to_restricted_original(state, name, slot)
    except UnknownMemoryStore:
        tightening = None
        logger.warning(
            "Slot %s: replacement rebound twice during tightening; writing the tail "
            "under the ratcheted line without tightening the live replacement",
            name,
        )
    try:
        slot.flush_deferred_notes()
    except Exception:
        # The flush puts the unwritten suffix back before raising, so this count is
        # what is still held. The hold also has a durable copy in the slot's
        # metadata line, so these notes are re-delivered after the NEXT gateway
        # restart rather than dying with the popped object — but nothing
        # in THIS process will visit this slot again, so for this lifetime they
        # are undeliverable and the log must still say so.
        logger.error(
            "Slot %s: %d held note(s) could not be flushed before the key was handed "
            "to a concurrent recreate; they are undeliverable until the persisted "
            "hold replays on the next restart",
            name,
            len(slot._deferred_notes),
            exc_info=True,
        )
    # ``_disk_window_len`` is how much of the current window the last committed save
    # covered, so the difference is exactly what has never reached disk. ``_dirty``
    # covers the other shape of unsaved state: an in-place edit to a row already
    # persisted leaves the length unchanged.
    # A restored sidebar row whose window never loaded holds no row that is not
    # already on disk, so its window as held (empty) is the whole answer, and
    # reading ``messages`` would load its transcript on the loop to learn it.
    window = (
        slot.loaded_messages() if getattr(slot, "window_pending", False) is True else slot.messages
    )
    unsaved = max(0, len(window) - slot._disk_window_len)
    # A queued prompt is unsaved state that changes NEITHER of those: its row is
    # written by the drain, so the window length is unchanged, and an enqueue does
    # not dirty the slot. Reporting a clean hand-over over that state would send
    # the prompt's only copy away with the discarded object, unremarked.
    history_key = slot_history_key(slot)
    if not unsaved and not slot._dirty and not slot.queue_persist_pending:
        # No write is needed, but "this slot committed its queue" is not the
        # same fact as "the entries have a durable future": a same-key recreate
        # persists at birth, and its full save rebuilds the shared line and
        # clears ``queued_prompts`` by absence. Survival is decided by whoever
        # writes the line next, so that is what gets checked.
        lost = await _owed_prompts_lost_on_line(state, name, slot, history_key)
        if lost:
            _report_lost_queued_prompts(state, name, lost, history_key)
        return _HandoverDrainResult(rows_committed=True, prompts_lost=lost)
    try:
        committed = await save_slot_off_loop(
            state,
            slot,
            closed=False,
            best_effort=False,
            expected_history_key=history_key,
            rows_only=True,
            # This frame IS the retraction's drain, so the close's guarded-write
            # fence does not apply to it: the close raised that fence, sequenced
            # this write itself, and nothing in the process can reach these rows
            # again. Refusing here would drop exactly the rows this function
            # exists to save.
            issued_by_the_retraction=True,
        )
    except Exception:
        await restore_replacement_if_handover_did_not_land(state, name, tightening, history_key)
        logger.error(
            "Slot %s: %d unpersisted row(s) could not be written to %s while handing "
            "the key to a concurrent recreate; they are lost with the original slot",
            name,
            unsaved,
            history_key,
            exc_info=True,
        )
        # A failed write leaves the line as it was; whether an owed entry still
        # has a durable future is decided by who writes that line next, not by
        # this slot's own persistence signature, which cannot see a
        # replacement's rebuild clearing the shared key's queue.
        lost = await _owed_prompts_lost_on_line(state, name, slot, history_key)
        if lost:
            _report_lost_queued_prompts(state, name, lost, history_key)
        return _HandoverDrainResult(rows_committed=False, prompts_lost=lost)
    if not committed:
        await restore_replacement_if_handover_did_not_land(state, name, tightening, history_key)
        # The save declined without writing: the session was permanently deleted
        # while this write awaited the lock, or the slot's routing moved off the
        # transcript this frame authorized. Neither leaves anywhere for these rows
        # to go, and the object holding them is about to be dropped.
        logger.warning(
            "Slot %s: %d unpersisted row(s) were not written to %s while handing the "
            "key to a concurrent recreate; the save declined the write",
            name,
            unsaved,
            history_key,
        )
        lost = await _owed_prompts_lost_on_line(state, name, slot, history_key)
        if lost:
            _report_lost_queued_prompts(state, name, lost, history_key)
        return _HandoverDrainResult(rows_committed=False, prompts_lost=lost)
    # Only a replacement that WRITES THIS TRANSCRIPT follows the committed rows'
    # mode: a slot at ``name`` whose rows route elsewhere (a task-review or
    # workflow slot with a divergent ``linked_session_key``) shares no file with
    # the original and must not be ratcheted for rows it will never read.
    if _replacement_shares_transcript(state, name, slot):
        tighten_live_slot_memory_mode(state, name, slot.memory_mode)
    # The write committed, and it can still leave owed entries with no durable
    # future: a rows-only save over a line another live slot published defers
    # every slot-owned field, ``queued_prompts`` among them
    # (``queue_line_is_ours`` keeps them owed rather than falsely credited),
    # and a live sharing replacement rebuilds the line on its every full save.
    # Survival is decided by who writes the line next — an entry a rehydrated
    # replacement carries in its own queue lives on as a queue card and is not
    # lost. Nothing in this process will visit this slot again, so a loss is
    # said with the count — the same obligation the held-note arm above
    # carries, and for the same reason: these are the user's own words and this
    # frame is their last reader.
    #
    # Carrying them instead would mean making ``queued_prompts`` a merge
    # field on the rows-only path, which is a change to what a durable
    # metadata line MEANS for a key two slots share, not a loop-side
    # ordering fix. The write stays rows-only; the remedy is the report, in
    # every register that can still carry it — the warning log, the
    # notification, and the count in the returned result.
    lost = await _owed_prompts_lost_on_line(state, name, slot, history_key)
    if lost:
        _report_lost_queued_prompts(state, name, lost, history_key)
    return _HandoverDrainResult(rows_committed=True, prompts_lost=lost)


def _unblock_pending_waits(state: DashboardState, slot: _ChatSlot) -> None:
    """Unblock EVERY thing a stop/interrupt could leave the runner waiting on.

    Two independent blocking waits exist per slot and both must be released or
    the cooperative cancel times out into a hard kill:

    * pending tool approvals (:func:`_reject_pending_approvals`)
    * pending agent questions that have a server-side wait
      (:meth:`DashboardState.cancel_questions_for_slot`). The blocking MCP
      ``ask_question`` registers its card in ``_pending_questions`` under the
      slot, so cancelling resolves its future as ``withdrawn`` and the tool call
      returns; the legacy ``POST /api/ask-question`` wait is released the same way.

    They are combined here deliberately: a new blocking wait added later must
    be released from every stop path, and three separate call sites each
    needing their own second line is how one of them gets missed.
    """
    _reject_pending_approvals(slot)
    cancelled = state.cancel_questions_for_slot(slot.key)
    if cancelled:
        logger.info("Stop: cancelled %d pending question(s) on slot %s", cancelled, slot.key)


# Test-only scheduling seam for the session-teardown races. Production leaves it
# None, so each point below costs one global read and an identity comparison, and
# no coroutine is created. It is reachable from no env var and no config key on
# purpose: an operator-facing knob that can suspend a teardown mid-pop is a way to
# wedge a live session, and nothing outside the test suite has a reason to want
# one.
#
# The interleavings it exists to make reachable cannot be driven from outside the
# process. Which of two teardowns lands inside the other's span is decided by
# which coroutine holds the event loop between two awaits; an HTTP client can only
# issue both requests and hope. A test awaits a named point, drives the other
# racer while suspended there, and so fixes the interleaving as a property of the
# test rather than of the scheduler -- the shape-determinism the async-flake rules
# ask for, with no sleep to tune.
#
# The names are the contract. Each marks a boundary the race actually crosses, and
# the comment at each call site says what suspending there is positioned to
# intercept; a point whose boundary no test can otherwise reach is the only kind
# worth adding.
#
# Assign it with monkeypatch, which reverts on teardown even when the test fails.
# ``_no_leaked_interleave_hook`` in test/conftest.py fails any test that leaves it
# set, because nothing legitimately does.
_test_interleave: Callable[[str], Awaitable[None]] | None = None


async def _reset_slot_session(
    state: DashboardState,
    slot: _ChatSlot,
    session_key: str,
    *,
    skip_if_busy: bool = False,
) -> bool:
    """Reset a slot's agent session, releasing anything blocked on the old one.

    The switch handlers (agent, model, bulk model, reasoning effort, workspace)
    and the reload endpoint reset the session so the next message starts under
    the new setting. That tears down the agent process — but a pending question
    card lives in dashboard state, not in the session, so without this it
    survives the reset: the card stays on screen inviting an answer, and if it
    is the blocking kind (``POST /api/ask-question``) the open HTTP request
    holds an MCP worker until its own timeout, with no agent left to receive
    the answer it eventually returns.

    Routing every reset through one helper rather than adding a second call at
    each site is deliberate, and is the same reasoning as
    :func:`_unblock_pending_waits`: six call sites each having to remember an
    extra line is how one of them gets missed.

    ``skip_if_busy`` forwards to :meth:`SessionManager.reset`, which evaluates
    busyness atomically with the session pop; False means the reset was
    declined or there was no live session to tear down. The unblock still runs
    first even then: a wait can only be pending from a turn old enough to have
    completed an LLM round-trip, and such a turn is visible to any caller's
    has_active_turn() fast path — so a decline here implies a turn that started
    microseconds ago, which cannot have posted a card yet.

    A successful reset also drops the slot's MCP session report, for the same
    reason the pending card goes: it describes the session being torn down.
    Every caller here changes what the next session will mount (agent, model,
    workspace) or restarts it outright, so keeping the old report would leave
    the UI presenting a dead session's server list as the live one's — the
    stale-evidence failure that report exists to remove.
    """
    _unblock_pending_waits(state, slot)
    if _test_interleave is not None:
        # The near side of the pop. Every teardown in the process funnels through
        # this one await, and the pop is what decides a race between two of them,
        # so this is the position from which a test can hold one teardown open and
        # put a second in flight over the same key.
        await _test_interleave("reset:pre_pop")
    try:
        reloaded = await state.sessions.reset(session_key, skip_if_busy=skip_if_busy)
    except BaseException:
        # Raised or cancelled mid-teardown: the session is in a state this slot
        # cannot vouch for, so neither is its verdict. Unknown fails open.
        slot.forget_session_model_state()
        raise
    if _test_interleave is not None:
        # The far side of the pop, ahead of the verdict-gated bookkeeping below.
        # That bookkeeping describes the session this call just tore down, and a
        # concurrent teardown can have registered and popped a successor under the
        # same key by the time it runs -- reachable only by suspending here,
        # because the pop and the bookkeeping are otherwise adjacent.
        await _test_interleave("reset:post_pop")
    if reloaded:
        # The withhold verdict describes the session that advertised the model
        # list, not the slot, so it goes with the session. Routed through this one
        # funnel for the reason above: the switch handlers that reset a session
        # are exactly the ones that can change which models the next session will
        # advertise (agent, workspace, and the model pick itself), and a verdict
        # surviving that would label the new session from the old one's
        # entitlement.
        #
        # Gated on the reset having HAPPENED. What decides this is whether the
        # session the verdict describes still exists: `skip_if_busy` DECLINES
        # while a turn is in flight, leaving that session -- and therefore its
        # verdict -- alive and accurate, while a completed teardown ends it. The
        # membership heuristic the frontend falls back to on `null` is not itself
        # the defect this carries a verdict to remove; inferring entitlement from
        # that heuristic WHILE an authoritative answer exists is. Dropping on a
        # decline would throw the authoritative answer away and re-create exactly
        # that.
        slot.forget_session_model_state()
        # The MCP session report rides the same gate for the same reason: it
        # describes the session that was just torn down. Clearing is a courtesy
        # delta push -- correctness rests on the identity projector in
        # serialize_slots -- so it only fires when something was recorded.
        if slot.clear_mcp_report():
            state.broadcast_ws("mcp_report_update", {"slot": slot.key, "mcp_report": None})
    # Freshness push for open tabs, OUTSIDE the `reloaded` gate on purpose: the
    # helper is verdict-driven (it re-resolves the live child and applies the
    # read gate's own predicate), so after a declined or failed teardown the
    # still-live child's banners match and nothing is broadcast. See
    # `_broadcast_expired_oauth_banners` for why no snapshot is needed.
    _broadcast_expired_oauth_banners(state, slot)
    return reloaded


# Advisory response field for a committed switch whose old-session teardown
# raised. One literal shared by every switch handler (agent, reasoning effort,
# model, workspace) so a frontend that ever starts reading it never has to
# match per-handler spellings.
_TEARDOWN_INCOMPLETE_WARNING = "old session teardown incomplete"


def _resolve_stop_event(slot: _ChatSlot, outcome: str) -> None:
    """Update the in-flight stop_event message in place with final state."""
    stop_id = slot._stop_event_id
    logger.debug("_resolve_stop_event: outcome=%s stop_id=%r", outcome, stop_id)
    if not stop_id:
        return
    now_ts = datetime.now(tz=timezone.utc).isoformat()
    if outcome == "soft":
        final_state = "stopped"
    elif outcome == "compacting":
        # Nothing was stopped: the session's own /compact turn held it and a
        # cooperative Stop was declined. The card becomes the notice,
        # so the row the press opened tells the user what happened to it.
        final_state = "stop_declined_compacting"
    else:
        final_state = "stop_failed_reset"
    found = False
    for msg in reversed(slot.messages):
        cls_val = msg.get("cls", "")
        if not cls_val:
            continue
        try:
            cls_data = json.loads(cls_val) if isinstance(cls_val, str) else None
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(cls_data, dict) or cls_data.get("kind") != "stop_event":
            continue
        if cls_data.get("id") != stop_id:
            continue
        cls_data["state"] = final_state
        cls_data["outcome"] = outcome
        cls_data["ts_end"] = now_ts
        serialized = json.dumps(cls_data)
        msg["cls"] = serialized
        msg["content"] = serialized
        slot.invalidate_source_links()
        slot._dirty = True
        found = True
        # Re-broadcast updated stop_event so frontend StopEventCard
        # transitions from "stopping" → "stopped"/"stop_failed_reset".
        on_msg = getattr(slot, "_on_message", None)
        if on_msg:
            try:
                on_msg(slot.key, msg)
            except Exception:
                logger.debug("stop_event re-broadcast failed", exc_info=True)
        break
    if not found:
        logger.debug("_resolve_stop_event: no matching message for stop_id=%s", stop_id)
    slot._stop_event_id = None


def _rearm_stop_event(slot: _ChatSlot, stop_data: dict[str, Any]) -> bool:
    """Reset an orphaned stop card back to "stopping" in place, same id.

    A new press that finds an orphan must not sweep it and append a fresh row:
    the pane upserts stop cards by ``meta.id``, so the settled old row plus the
    new row render as TWO "[Stopped]" chips for one press. Re-arming
    the existing row keeps the id — and therefore the chip — stable, the same
    reuse the escalation path performs via ``slot._stop_escalated_card_id``.

    Returns False when no row carries the id (e.g. the window was trimmed), in
    which case the caller appends the press's one card instead.
    """
    stop_id = stop_data["id"]
    serialized = json.dumps(stop_data)
    for msg in reversed(slot.messages):
        cls_val = msg.get("cls", "")
        if not cls_val:
            continue
        try:
            cls_data = json.loads(cls_val) if isinstance(cls_val, str) else None
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(cls_data, dict) or cls_data.get("kind") != "stop_event":
            continue
        if cls_data.get("id") != stop_id:
            continue
        msg["cls"] = serialized
        msg["content"] = serialized
        slot.invalidate_source_links()
        slot._dirty = True
        # Re-broadcast so connected panes transition the existing chip back to
        # "stopping" — the same channel _resolve_stop_event settles it on.
        on_msg = getattr(slot, "_on_message", None)
        if on_msg:
            try:
                on_msg(slot.key, msg)
            except Exception:
                logger.debug("stop_event re-arm re-broadcast failed", exc_info=True)
        return True
    logger.debug("_rearm_stop_event: no matching message for stop_id=%s", stop_id)
    return False


#: Roles that OPEN a turn in the transcript grouping. Mirrors
#: ``TURN_OPENER_ROLES`` in ``website/src/pages/chat/groupDisplayItems.ts`` —
#: the two must agree, or a stop chip re-armed "in the same turn" here lands
#: in a different visual turn there. A ``subagent`` row that is not a parsable
#: completion is hidden client-side rather than turn-opening; treating it as a
#: boundary anyway only errs toward append-fresh (the sweep-and-append shape), never
#: toward a wrong-turn re-arm.
_TURN_OPENER_ROLES = frozenset({"user", "nudge", "subagent"})


def _orphan_in_current_turn(slot: _ChatSlot, stop_id: str) -> bool:
    """Whether the orphaned stop card is part of the CURRENT turn.

    Walks the window tail: hitting the orphan first means no turn-opening row
    follows it (same turn — the adjacent-chips shape); hitting a
    turn-opener first means the next turn began below the orphan. An orphan
    whose row is gone from the window answers False, which routes the caller
    to the append fallback it already has.
    """
    for msg in reversed(slot.messages):
        if msg.get("role") in _TURN_OPENER_ROLES:
            return False
        # The grouping's SECOND turn-flushing path is not role-based: a
        # synthesis injection (role "inject" stamped meta.injectKind ==
        # "synthesis" by _run_pending_synthesis) closes the open batch too —
        # mirrors isSynthesisInjection in groupDisplayItems.ts, keyed on the
        # meta wire contract exactly as it is. Plain inject rows
        # (cron/recovery notes) are passive on both sides and walked past.
        if msg.get("role") == "inject" and (msg.get("meta") or {}).get("injectKind") == "synthesis":
            return False
        cls_val = msg.get("cls", "")
        if not cls_val or not isinstance(cls_val, str) or "stop_event" not in cls_val:
            continue
        try:
            cls_data = json.loads(cls_val)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(cls_data, dict) and cls_data.get("id") == stop_id:
            return True
    return False


def _open_stop_event_card(slot: _ChatSlot, state_label: str) -> str:
    """Open this press's ONE stop card and return its id.

    An orphaned card from a prior attempt is REUSED, not swept. The old
    "defensive stale-card sweep" resolved the orphan AND appended a fresh row,
    so one press put two ``stop_event`` rows on the wire and the pane — which
    upserts by ``meta.id`` — renders two "[Stopped]" chips. Re-arming
    the existing row in place keeps the single-card-per-press invariant the
    peer-bound branch of ``stop_slot_turn`` documents, mirroring the
    escalation path's reuse of the open card rather than minting a second one.

    One helper for both cancel routes (``stop_slot_turn`` and
    ``api_chat_slot_interrupt``) so the reuse rule cannot drift between them.

    Reuse is scoped to a SAME-TURN orphan: when a ``user`` row follows the
    orphan, the next turn has begun and re-arming would mutate a row sitting
    in the previous turn's block — the press's chip would then appear (and
    transition) in earlier scrollback, attributing the stop to the wrong turn.
    A cross-turn orphan is settled where it lies and this press's card is
    appended fresh, which never renders two
    ADJACENT chips.
    """
    stale_id = slot._stop_event_id
    if stale_id and not _orphan_in_current_turn(slot, stale_id):
        _resolve_stop_event(slot, "soft")  # settle it where it lies
        stale_id = None
    stop_id = stale_id or f"stop-{uuid.uuid4().hex}"
    slot._stop_event_id = stop_id
    now_ts = datetime.now(tz=timezone.utc).isoformat()
    stop_data = {
        "kind": "stop_event",
        "id": stop_id,
        "state": state_label,
        "outcome": None,
        "ts_start": now_ts,
    }
    # cls must be JSON-encoded so parse_cls_meta() populates meta on the wire.
    # content mirrors the data for backward-compat with any consumer that only
    # reads content.
    stop_msg = json.dumps(stop_data)
    if not (stale_id and _rearm_stop_event(slot, stop_data)):
        if stale_id:
            # The re-arm found no row (lost a race with window trimming):
            # appending under the REUSED id would upsert into a client still
            # holding the old row and land the chip in old scrollback — the
            # failure mode reuse exists to avoid. Mint fresh for the append.
            stop_id = f"stop-{uuid.uuid4().hex}"
            slot._stop_event_id = stop_id
            stop_data["id"] = stop_id
            stop_msg = json.dumps(stop_data)
        # No same-turn orphan to re-arm: this press's one card is a fresh
        # append (a cross-turn or vanished orphan was settled above).
        slot.append("system", stop_msg, stop_msg)
    if stale_id and slot._stop_escalated_card_id == stale_id:
        # A stale escalation marker scoped to the REUSED id would make this
        # press's cooperative ack defer to a hard callback that already fired
        # (or never will), stranding the re-armed card at "stopping" — the
        # exact failure the id-scoped marker exists to remove. A swept card
        # never hit this because the fresh id could not match; reuse must
        # clear it explicitly.
        slot._stop_escalated_card_id = None
    return stop_id


def _make_stop_resolver(
    state: DashboardState, slot: _ChatSlot, outcome: str, card_id: str | None
) -> Callable[[], Awaitable[None]]:
    """Build the stop_turn on_soft/on_hard callback that settles the stop card.

    Key the guard on `_stop_event_id`, not on `_stop_state`. The card id is
    already the idempotency token: `_resolve_stop_event` no-ops when it is None
    and clears it once it has settled the card, so a state gate buys nothing
    there. What the state gate did buy was a bug. A turn tearing down
    concurrently drives `_stop_state` back to "idle" (`_finish_queue_cycle` in
    chat_runner.py, through the `_stopping` setter in state.py), and that
    teardown races the escalation. When teardown won, the hard callback bailed,
    `_resolve_stop_event` never ran, and the card pulsed at "stopping" for the
    rest of the session instead of settling to "stop_failed_reset".

    Precedence needs its own non-racy marker. A cooperative ack that arrives
    after the user escalated must not relabel a hard kill as a clean stop, and
    `_stop_state` cannot carry that fact because the same teardown resets it to
    "idle" from `killing` just as readily as from `soft_pending`. Reading it
    here would reproduce the bug one dimension over: teardown erases the
    escalation, the late soft callback sees a neutral state, and the card
    settles as "stopped" for a session that was killed. So the escalation path
    sets `slot._stop_escalated_card_id`, which teardown never touches, and only
    the soft callback defers on it. `hard` is terminal and nothing outranks it.
    The marker holds an id rather than a flag so it cannot leak onto a later
    card: a bare boolean left set would make the NEXT card's cooperative ack
    defer to a hard callback that never fires, stranding that card at
    "stopping", which is the failure this marker exists to remove.

    Bind to `card_id`, the specific card this callback was created for, and not
    to whatever card happens to be in flight when it fires. `stop_turn` awaits
    these callbacks, so one can still be pending when teardown resets the stop
    posture, a new turn starts, and a second stop opens a card of its own —
    usually a NEW id, but a same-turn orphan is RE-ARMED under this very id
    (`_open_stop_event_card`), which is why the id comparison alone is
    not per-attempt identity; see the generation paragraph below. Reading
    `slot._stop_event_id` at call time would settle the newer stop's card with
    this older outcome and clear its posture, so the newer stop's own callback
    would find nothing left to settle. Callers pass the id they just assigned.

    `card_id` may be None, for a stop that escalated before any card existed.
    Such a callback still releases the stop posture; it simply has no card to
    label. Only a mismatching non-None current id means "someone else owns
    this", so only that case returns without touching the slot.

    Also bind `slot._stop_generation`, captured at creation. Card REUSE
    (`_open_stop_event_card`) makes the id comparison insufficient by
    construction: a press that re-arms an orphaned card carries the SAME id the
    prior press's still-pending callback was bound to, so matching ids no
    longer prove matching stops — the old callback would settle the re-armed
    card with the old outcome and release the new stop's posture. The
    generation counts stop INITIATIONS (the `_stop_state` setter bumps it on
    every idle → active edge and teardown never rewinds it), so "a newer stop
    has initiated since this callback was created" is exactly `generation !=
    slot._stop_generation` — and that newer stop's own callbacks own both the
    card and the posture, including the cardless-posture-release duty above
    (an initiation that rolls back before binding callbacks, like /interrupt's
    refused-body branch, resets the posture itself — its CARD, if a prior
    press's orphan was in flight, can stay at "stopping" until the next press
    sweeps or re-arms it: the generation is monotonic and never rewound, so
    the prior resolver bails. That residual is accepted deliberately — the
    posture is safe, the strand self-corrects on the next press, and settling
    the card from the rollback would label it with an outcome the still-
    pending cancel has not produced).
    """
    generation = slot._stop_generation

    async def _resolve() -> None:
        logger.debug(
            "stop resolver (%s): card_id=%r current=%r stop_state=%r escalated=%r gen=%d/%d",
            outcome,
            card_id,
            slot._stop_event_id,
            slot._stop_state,
            slot._stop_escalated_card_id,
            generation,
            slot._stop_generation,
        )
        # A newer stop initiated after this callback was created: everything —
        # the (possibly re-armed, same-id) card AND the posture — belongs to
        # that stop's own callbacks now. See the generation paragraph above.
        if generation != slot._stop_generation:
            return
        # Bail only when a DIFFERENT card is genuinely in flight, because that
        # card belongs to a later stop that owns the posture. Do not bail merely
        # because this attempt has no card: settling a card and releasing the
        # stop posture are separate jobs, and the posture must be released even
        # when there was never a card to settle. A stop can reach a callback
        # with `card_id` None: `api_chat_slot_interrupt` claims
        # `_stop_state = "soft_pending"` before it awaits the request body and
        # only then opens its card, so a concurrent `/stop` escalates against a
        # slot that has none yet. Skipping the reset there strands `_stop_state`
        # at "killing", which permanently suppresses re-queue
        # (`_should_suppress_requeue`) and rejects every later interrupt. That
        # wedges the slot, which is worse than the mislabel this guard prevents.
        if slot._stop_event_id is not None and slot._stop_event_id != card_id:
            return
        # `card_id is None` cannot mean "escalated": the marker holds a real
        # card id, so comparing None to None would defer a callback that no
        # hard kill will ever follow, and the posture would never be released.
        if outcome == "soft" and card_id is not None and slot._stop_escalated_card_id == card_id:
            logger.debug("stop resolver (soft): escalated to hard kill, deferring to hard")
            return
        # No-ops when there is no card, which is exactly the case above.
        _resolve_stop_event(slot, outcome)
        slot._stop_state = "idle"
        if card_id is not None and slot._stop_escalated_card_id == card_id:
            slot._stop_escalated_card_id = None
        state.push_slots_update()

    return _resolve


def _slot_not_found() -> web.Response:
    """The uniform per-slot 404, :func:`slot_ownership.slot_not_found`."""
    return slot_not_found()


async def _app_may_send_to_slot(request_app: str, slot: Any) -> bool:
    """Whether *request_app* may control *slot* through the session APIs.

    The session-control rule (:func:`app_may_control_session`) on a live read of
    the ``sessionApproval`` grant, judged after that read completes.
    """
    if not request_app:
        return True
    granted = await read_session_grant(request_app)
    return app_may_control_session(request_app, slot, granted)


async def _app_claim_refused(
    state: DashboardState, request_app: str, operation: str, keys: Iterable[str]
) -> bool:
    """True, audited, when an app's NEW slot would bind to a transcript it does not own.

    Any of *keys* (the transcripts the new slot would read or save) that exists
    and does not record *request_app* refuses it; see
    ``handlers.sessions._app_may_claim_transcript``. The caller answers with the
    uniform ``slot_not_found`` 404. A dashboard caller (no app) is never refused.
    """
    log = state.conversation_log
    if not request_app or log is None:
        return False
    # Imported here: this module imports the handlers package at module scope.
    from kiro_crew.dashboard.handlers.sessions import (
        _NOT_TRANSCRIPT_OWNER,
        _app_may_claim_transcript,
        _audit_app_allow,
        _audit_app_denial,
    )

    claimed = list(dict.fromkeys(keys))
    for key in claimed:
        if not await asyncio.to_thread(_app_may_claim_transcript, log, request_app, key):
            _audit_app_denial(request_app, operation, f"session={key}", _NOT_TRANSCRIPT_OWNER)
            return True
    for key in claimed:
        _audit_app_allow(request_app, operation, f"session={key}")
    return False


def _cancel_target(slot: _ChatSlot) -> str:
    """The session a cancel on *slot* must address.

    Never ``_history_key_for(name)``: every slot carrying a
    ``linked_session_key`` — a cron-born tab (``cron:<job_id>``), a channel-born
    tab (``slack:<ts>``), a workflow-born tab — runs its turns under that key,
    while the dashboard-prefixed spelling names a session that never existed.
    ``SessionManager.stop_turn`` then finds nothing and returns ``"idle"``, the
    handler settles the card as "stopped", and the turn keeps streaming, so Stop
    is a silent no-op that reports success once per press.

    Routing alone is not enough either. A running turn owns a stable identity:
    ``_run_chat`` captures the key it acquires and keeps using that one for the
    whole turn, while
    ``linked_session_key`` remains mutable underneath it — a cron injection
    binds an already-live slot with no ``running`` gate. Re-deriving the key at
    cancel time therefore names wherever the slot routes the NEXT turn, which
    after a mid-turn rebind is not the turn the operator is trying to stop.

    Falls back to the routing when no turn is in flight (nothing to have
    captured an identity), which is also what a slot restored from disk answers
    — the field is runtime-only and empty after a restart.
    """
    return getattr(slot, "_active_turn_session_key", "") or effective_session_key(slot)


def _app_cancel_denied(
    request: web.Request | Mapping[str, Any], slot: _ChatSlot, operation: str, target_key: str
) -> web.Response | None:
    """Whether *request* may cancel *target_key*, as an indistinguishable 404.

    Two conditions for an app token, because slot ownership does NOT imply
    ownership of the session the cancel would land on:

    1. the app owns the slot (App Kit §5.2, deny-by-default), and
    2. the session about to be cancelled is still the slot's own session
       (``slot_ownership.own_session_key``), not one the app has no claim on.

    Condition 2 is load-bearing. ``get_or_create_slot`` takes ``app`` and, for a
    name shaped like a channel session stem, resolves ``linked_session_key``
    from the session map in the same call — so an app that names a live channel
    thread ends up owning a slot bound to a conversation it has no claim on.
    Ownership alone would then authorize cancelling that channel's turn, turning
    a slot binding into capability escalation.

    It tests *target_key* — the key the caller will actually cancel — rather
    than re-reading the slot, so authorization and action cannot disagree. That
    is not only a TOCTOU guard: for a turn that started on the app's own session
    and was rebound mid-flight, re-reading would DENY the app its own running
    turn, because the routing now points somewhere it does not own.

    A dashboard caller has no app scope and may cancel either kind.

    Shared by the cancel routes so /stop and /interrupt cannot drift onto two
    policies. Condition 1 is :func:`deny_app_slot_access`, the decision the
    per-slot checkpoint already made; condition 2 is layered on top of it.
    """
    request_app = request.get("app", "")
    denied = deny_app_slot_access(request_app, slot, slot.key, operation)
    if denied is not None or not request_app:
        return denied
    if target_key == own_session_key(slot):
        return None
    audit_app_slot_denial(
        request_app, operation, slot.key, "app does not own the session this slot is linked to"
    )
    return slot_not_found()


async def _settle_discarded_stage_deliveries(
    state: "DashboardState",
    slot: "_ChatSlot",
    contents: list[str],
) -> None:
    """Settle the queued completion debt a discarded queue owed."""
    manager = getattr(state, "subagents", None)
    if manager is None:
        return
    owed = slot.take_pending_subagent_deliveries(contents)
    if owed:
        try:
            settlement = manager.settle_queued_delivery(owed)
            if asyncio.iscoroutine(settlement):
                await settlement
        except Exception:
            logger.warning(
                "Could not settle discarded stage deliveries for slot %s",
                slot.key,
                exc_info=True,
            )


async def stop_slot_turn(
    state: "DashboardState",
    slot: "_ChatSlot",
    *,
    force: bool = False,
    source: str = "dashboard",
    cancel_key: str = "",
    escalate: bool = True,
) -> dict[str, Any]:
    """Stop the slot's turn: cooperative cancel, hard kill on a second call.

    First call: soft cancel. A second call while the first is still pending
    escalates to a hard kill, regardless of *force* — the caller's view of the
    stop state can lag the backend's, so the backend's own ``_stop_state`` is
    what decides.

    *escalate* is how a caller says its second call may not be a second
    DECISION. It defaults to True because that is true of the Stop button this
    function was written for: a person pressing again has watched the cooperative
    stop fail to take. It is not true of an RPC, where a client that timed out
    re-sends the same request — so ``session_control.stop_target`` passes False
    for a call it cannot distinguish from a retry, and the repeat falls through to
    the no-op below instead of discarding the target's queue. It
    withholds only the ESCALATION: a stop that finds the slot running still stops
    it either way.

    Inserts a ``stop_event`` card into the slot transcript so whoever is
    watching the session sees the stop, and returns the JSON body the route
    would have sent. *source* labels the SEL audit line with who asked.

    *cancel_key* is the session the stop must land on, resolved ONCE by the
    caller. A caller that authorizes the stop has to pass the very key it
    authorized: re-deriving it here could name a different session if a rebind
    lands between the check and the cancel, which is the whole reason the route
    resolves it up front. Omitted only by callers with nothing to authorize
    against, which fall back to the slot's own routing.
    """
    name = slot.key
    cancel_key = cancel_key or _cancel_target(slot)

    # The second press after a DECLINED Stop (the session was compacting) is
    # the user's escape hatch and takes the escalation branch below, which is
    # the only path to a hard kill. The decline itself leaves ``_stop_state``
    # idle -- the queue drain reads that machine as "a stop is in progress" and
    # would persist a false "Session reset" row on the next queued turn -- so the
    # arming lives in its own marker, consumed here: the claim it makes lasts
    # exactly as long as the escalation needs it, on a press that IS a stop.
    # Gated on the caller's own ``escalate``: a caller that says its call may be
    # a retry (a board stop re-sent by a timed-out browser, the steer-containment
    # stop) must never be turned into a hard kill by a marker it did not set.
    # ...and on the compaction still holding the session: the marker is a
    # memory of a refusal, not proof the refusal still applies. Once the
    # compaction has ended, the next press is an ordinary first press and
    # takes the cooperative path; the stale marker is simply dropped.
    # Set when this press is the ESCAPE from a compaction decline (the armed
    # second press, or an explicit ``?force=true``). The hard kill below then
    # keeps the SESSION queue: on a channel-linked slot that queue is the linked
    # channel's, holding other people's messages the compaction, not the user,
    # is what the Stop is aimed at; ``stop_turn`` parks it for the successor.
    # The slot's own dashboard queue is still discarded, as on any hard kill.
    compaction_escape = False
    if slot.running and slot._stop_state == "idle" and stop_declined_armed(slot):
        if escalate and _compaction_in_flight(state, cancel_key):
            # Consumed by the press it armed. A non-escalating call (a retry, the
            # steer-containment stop) must not spend the user's hatch, and a
            # marker for a compaction that has ended is dropped as stale.
            slot._stop_declined_at = 0.0
            slot._stop_state = "soft_pending"
            compaction_escape = True
        elif not _compaction_in_flight(state, cancel_key):
            slot._stop_declined_at = 0.0
    elif (
        force
        and escalate
        and slot.running
        and slot._stop_state == "idle"
        and _compaction_in_flight(state, cancel_key)
    ):
        # An explicit ``?force=true`` on an idle stop state is the escape hatch
        # from the DECLINE only: it must reach the hard-stop escalation below
        # rather than be declined. Without a compaction, a ``force`` arriving on
        # an idle state is a retried first press whose original was lost on the
        # wire, and it keeps the cooperative path it always had.
        slot._stop_state = "soft_pending"
        compaction_escape = True

    # Escalation path: a second stop press while a cooperative cancel is
    # already pending hard-kills. We escalate on ANY second press — not only
    # when the client computed force=true — because the client derives force
    # from the WS-echoed stop_state, which may lag behind the actual state on a
    # slow connection. The backend's own _stop_state is the authoritative
    # "already soft_pending" signal, so a second press always means "kill it".
    #
    # Unless the caller told us this call may not be a second press at all
    # (*escalate*): a re-sent RPC carries no new intent, and the caller is the
    # only layer that can know whether its second call was a decision or a
    # timeout retry. A withheld escalation falls into the no-op branch below.
    if escalate and slot._stop_state == "soft_pending":
        slot._stop_state = "killing"
        # A decline settled its own card and left no open one; the hard kill
        # that follows needs a row of its own, or the last stop row the user
        # sees still reads "nothing was stopped" for a session that was reset.
        if not slot._stop_event_id:
            _open_stop_event_card(slot, "stopping")
        # Survives turn teardown, which resets _stop_state to "idle". Without
        # it a cooperative ack from the first press could still land and label
        # this hard kill a clean stop. Scoped to this card so it cannot defer
        # a later card's ack.
        slot._stop_escalated_card_id = slot._stop_event_id
        # Take the queue and empty it in one step, then settle what was taken. The
        # settle can suspend (it waits on a sub-agent's teardown and writes its
        # tombstones on a worker thread), and a send that lands meanwhile is
        # acknowledged as queued; a clear after the await would discard it with
        # nothing settled and nothing said. The kill discards what was queued
        # when it was pressed, and a later send stays queued for the next turn.
        discarded = list(slot._queue)
        slot._queue.clear()
        # A cross-session delivery among the taken entries has a sender holding a
        # "queued, will run later" receipt; nothing else here reaches that sender.
        # Told before the settle can suspend, so a cancellation of this stop
        # cannot leave a discarded entry unreported.
        # circular import: session_control imports this package's modules at module level.
        from kiro_crew.dashboard.session_control import (
            SEND_DISCARD_HARD_STOP,
            notify_send_origin_discarded,
        )

        for _entry in discarded:
            notify_send_origin_discarded(
                state,
                entry_meta=_entry.get("meta"),
                target_slot=slot,
                text=_entry.get("content") or "",
                cause=SEND_DISCARD_HARD_STOP,
            )
        await _settle_discarded_stage_deliveries(
            state,
            slot,
            [str(entry.get("content", "")) for entry in discarded],
        )
        # Hard kill = "discard everything": drop unconsumed steers too, so the
        # end-of-turn requeue (chat_runner finally) has nothing to resurrect.
        # Mirrors the queue clear above; a soft stop preserves both.
        #
        # Their delivery ids go with them, and that is load-bearing rather than
        # tidiness: `steer_into_running_turn` reconciles an in-flight steer by
        # asking what removed its registration, and a CONSUMED steer leaves its
        # `_steer_delivery_ids` entry in place. Dropping the entry here is
        # therefore what tells the two apart -- absence means this hard kill
        # discarded the text, so the caller is told it was not delivered instead
        # of having a row persisted for a message that never ran.
        for _discarded in slot._pending_steers:
            slot._steer_delivery_ids.pop(_discarded, None)
            # Lockstep with the line above (see `_ChatSlot._steer_send_ids`): a hard
            # kill discards the text, so there is no requeued entry left to carry
            # the client's send id onto.
            slot._steer_send_ids.pop(_discarded, None)
            slot._steer_user_origin.pop(_discarded, None)
            slot._steer_channel_origin.pop(_discarded, None)
            slot._steer_admissions.pop(_discarded, None)
            slot._steer_attachment_meta.pop(_discarded, None)
            slot._steer_decision_strips.pop(_discarded, None)
            slot._steer_possibly_delivered.discard(_discarded)
        slot._pending_steers.clear()
        state.push_slots_update()
        logger.info("Stop (force): hard-killing session for slot %s", name)

        # Escalation reuses the card the first press opened, so bind to it.
        _on_hard_force = _make_stop_resolver(state, slot, "hard", slot._stop_event_id)

        # Unblock chat runner if it's suspended waiting for tool approval or on
        # a pending ask_question card.
        _unblock_pending_waits(state, slot)
        # Stop addresses the SESSION, so it resolves through
        # effective_session_key: a channel-linked slot's turns run under its
        # linked_session_key (slack:<ts>), and handing stop_turn the
        # dashboard:<slot> key names a session no running turn owns — the stop
        # reports success and cancels nothing. The SEL record below stays on the
        # slot-derived key, which identifies the tab the operator pressed.
        await state.sessions.stop_turn(
            cancel_key,
            force=True,
            preserve_queue=compaction_escape,
            on_hard=_on_hard_force,
        )
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_stop",
            tool_kind="command",
            outcome="hard",
            # Record what the client requested (force flag) vs. the escalation
            # the backend actually performed (always a hard kill here).
            metadata={"slot": name, "via": source, "force": force, "escalated": True},
        )
        return {"ok": True}

    # Already stopping or not running — no-op (idempotent repeat press guard)
    if slot._stop_state != "idle" or not slot.running:
        if not slot.running:
            logger.info("Stop: slot %s not running, ignoring", name)
            _info = "not running"
        else:
            _info = "stop already in progress"
        _meta: dict[str, Any] = {"slot": name, "via": source, "reason": _info}
        if not escalate and slot._stop_state == "soft_pending":
            # The branch a de-duplicated retry lands on. Recorded so the audit
            # shows an escalation was WITHHELD rather than never asked for --
            # without it there is no record that a retry rather than a decision
            # caused the outcome, and an absorbed retry has to be visible too.
            _meta["escalation_withheld"] = True
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_stop",
            tool_kind="command",
            outcome="noop",
            metadata=_meta,
        )
        # ``already_stopping`` separates the two facts this branch merges: a
        # target that was never running has nothing to stop, while one whose
        # cooperative cancel is still in flight IS stopping. Both answer
        # ``info``, and a caller that renders them alike tells the second one the
        # opposite of what happened — which the de-duplicated retry above now
        # reaches routinely.
        return {"ok": True, "info": _info, "already_stopping": bool(slot.running)}

    # A cooperative Stop while the session's own automatic /compact holds it is
    # DECLINED, before any of the soft-stop side effects below run. Cancelling
    # that turn fails the compaction, and the failure arm recycles the session:
    # a user who pressed Stop on what looked like a stalled turn would lose the
    # session's memory to a restart the notice then blames on compaction.
    # The card the press would have opened is opened and settled in
    # one step, so the press still leaves a visible answer in the transcript.
    # A force stop (second press, or ?force=true) is the escape hatch and is
    # never declined: the force branch above runs first, and the decline below
    # arms ``_stop_declined_at`` so the NEXT press reaches that branch instead
    # of being declined again -- a live turn that shares the session with a
    # compaction must stay stoppable. ``_stop_state`` stays idle: nothing is
    # stopping. ``stop_turn`` repeats the compacting check for the race in
    # which a compaction starts between here and the cancel.
    if _compaction_in_flight(state, cancel_key):
        stop_id = _open_stop_event_card(slot, "stopping")
        _resolve_stop_event(slot, "compacting")
        slot._stop_event_id = None
        if escalate:
            # Only a press that could itself escalate arms the second press; a
            # caller that said "this may be a retry" must not arm a hard kill
            # for the user's next ordinary Stop.
            slot._stop_declined_at = time.monotonic()
        state.push_slots_update()
        logger.info("Stop: declined for slot %s — compaction in flight", name)
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_stop",
            tool_kind="command",
            outcome="compacting",
            metadata={"slot": name, "via": source, "force": False, "stop_id": stop_id},
        )
        return {"ok": True, "info": "compacting", "compacting": True}

    # First press: soft stop
    slot._stop_state = "soft_pending"
    # NOTE: Do NOT clear the queue here — stop should only cancel the
    # currently running turn, leaving queued messages intact for the user
    # to process or dismiss individually.

    # One card per press: re-arm an orphaned card in place or append a fresh
    # one (see _open_stop_event_card for why sweeping the orphan rendered two
    # chips).
    stop_id = _open_stop_event_card(slot, "stopping")
    state.push_slots_update()
    logger.info("Stop: cooperative cancel for slot %s (queue=%d)", name, len(slot._queue))

    _on_soft = _make_stop_resolver(state, slot, "soft", stop_id)
    _on_hard = _make_stop_resolver(state, slot, "hard", stop_id)

    # Unblock chat runner if it's suspended waiting for tool approval or on a
    # pending ask_question card.
    _unblock_pending_waits(state, slot)

    outcome = await state.sessions.stop_turn(
        cancel_key,
        force=False,
        preserve_queue=True,
        on_soft=_on_soft,
        on_hard=_on_hard,
    )
    # A genuine in-flight turn whose cooperative cancel does not confirm within
    # the budget answers ``stop_turn`` with a non-acked outcome, which that
    # method escalates to a hard reset on its own -- a dispatched, mid-execution
    # tool call that never acks is reaped there. ``"idle"`` is the opposite
    # signal: the provider holds no active turn to cancel (its model stream
    # reached the done boundary, or no session is registered for the key). When
    # the slot still reads running at that point its turn ended at the provider
    # but the slot has not seen the terminal event settle it. The orphaned card
    # is resolved and the reply names the honest state -- there is no running
    # provider turn here to report as "stopped", and no provider turn to kill.
    _idle_running = outcome == "idle" and slot.running
    if outcome == "idle" and slot._stop_event_id:
        _resolve_stop_event(slot, "soft")
        slot._stop_state = "idle"
        state.push_slots_update()
    elif outcome == "compacting":
        # The race the pre-check above cannot close: the compaction committed
        # between that read and the cancel. Same answer, and the soft-stop side
        # effects taken above are undone: ``_stop_state`` back to idle (nothing
        # is stopping), the decline marker armed so the next press escalates,
        # as on the pre-check path.
        _resolve_stop_event(slot, "compacting")
        slot._stop_event_id = None
        slot._stop_state = "idle"
        if escalate:
            slot._stop_declined_at = time.monotonic()
        state.push_slots_update()
    sel().log_tool_invocation(
        session_key=_history_key_for(name),
        agent=getattr(slot, "agent", "") or "kirocrew",
        source="dashboard",
        tool_name="dashboard_stop",
        tool_kind="command",
        outcome=outcome,
        metadata={"slot": name, "via": source, "force": False},
    )
    if outcome == "compacting":
        return {"ok": True, "info": "compacting", "compacting": True}
    if _idle_running:
        # The provider held no active turn to cancel while the slot still read
        # running: the turn ended at the provider but the slot had not settled.
        # Naming it keeps the reply from reading as "stopped a running turn".
        return {"ok": True, "info": "no active turn"}
    return {"ok": True}


def _compaction_in_flight(state: DashboardState, cancel_key: str) -> bool:
    """The shared pre-stop probe (``session_lifecycle.compaction_in_flight``)."""
    return compaction_in_flight(state.sessions, cancel_key)


async def api_chat_slot_stop(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/stop — cooperative stop with kill fallback.

    The route is where authorization lives, because it is the only layer holding
    the ``request`` an app token rides on. ``stop_slot_turn`` is the mechanism
    and takes a slot, so every caller that reaches it by another path (session
    control) has to establish its own authority — the guard cannot be inherited
    by accident.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_stop")
    if denied is not None:
        return denied
    # Before ANY side effect — the escalation branch inside stop_slot_turn clears
    # the queue and drops pending steers, so a guard placed later would still let
    # a foreign caller mutate the slot. One target, resolved once: the session the
    # in-flight turn actually runs on, so authorization and the stop cannot
    # disagree across a mid-turn rebind.
    cancel_key = _cancel_target(slot)
    denied = _app_cancel_denied(request, slot, "chat_stop", cancel_key)
    if denied is not None:
        return denied
    force = request.query.get("force", "").lower() == "true"
    return web.json_response(await stop_slot_turn(state, slot, force=force, cancel_key=cancel_key))


async def api_chat_slot_continue(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/continue — hand the thread back to the agent.

    Two callers, one mechanism: picking up a turn that was cut short, and asking
    a slot that finished cleanly to carry on. They are one endpoint because they
    are indistinguishable from the transcript — a force-quit runs no ``finally``,
    so a killed turn leaves no error row behind and reads exactly like a
    completed one. ``_has_conversation`` authorizes; ``_is_interrupted`` only
    chooses which of the two continuation bodies the model receives.

    Runs the same synthetic-continuation machinery the runner already uses for
    its own post-transient recovery: queue the continuation at the head, then let
    ``_start_next_queued_turn`` land it as an ``inject`` row and dispatch the
    turn. No bespoke dispatch path, and the row folds into the existing recovery
    card instead of printing machine prose as a user bubble.

    The frontend decides whether to OFFER this (it has the transcript, `running`
    and the queue locally, so it needs no server field for that). This endpoint
    re-checks under ``slot._lock`` because the client's view is a WS snapshot and
    therefore lagging: a press landing in the instant a turn starts, or a second
    browser tab acting on a stale cache, would otherwise dispatch a duplicate
    turn against one slot — real tokens, real tool calls, real repo writes. Every
    other dispatch route guards the same way (see ``api_chat_slot_regenerate``).

    NOT readiness-gated, and that is deliberate — see
    ``kiro_readiness.reject_if_kiro_unverified``. Continue is an ordinary send: it
    queues one synthetic message and lets the runner dispatch it, mutating nothing
    durable up front, so the ACP attempt is its authority and a signed-out install
    reports ``AcpAuthRequired`` in the transcript. Gating it instead put the
    button behind a latch that is refreshed by re-probing ``kiro-cli``, and a
    probe that merely TIMES OUT reads as signed-out: on a host where that probe is
    slow the press was refused with a 503 forever while typing the same request by
    hand worked. The unequal treatment of two paths that dispatch the same turn is
    the bug; the transcript's own error card is the report either way.

    The one refusal that reads the transcript's content is a different thing
    from a readiness gate: ``session_start_repeat`` fires only when the slot's
    OWN tail holds two ``session_start_failed`` error rows with nothing but
    recovery rows between them -- the same start, re-issued by Resume, failed
    twice. That is not a probe that can be wrong forever; it is the record of
    what this endpoint itself just did twice. A typed message stays allowed and
    resets the count, so the refusal never latches the slot.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    # App ownership check (App Kit §5.2): deny-by-default for app tokens, mirroring
    # api_chat. Without it an app token holding /api/chat could resume ANY
    # interrupted slot — including a dashboard user's — and that is not a read: it
    # dispatches an agent turn that runs tools and writes to the repo. Same
    # indistinguishable 404 as the send path, so the response cannot be used to
    # probe which foreign slots exist.
    request_app = request.get("app", "")
    denied = deny_app_slot_access(request_app, slot, slot.key, "chat_continue")
    if denied is not None:
        return denied

    # A relay archive never runs. AFTER the app-ownership 404 above: a foreign
    # app must not be able to tell an archive apart from a missing slot.
    refusal = relay_archive_refusal(slot)
    if refusal is not None:
        return refusal

    try:
        await continue_slot_turn(
            state,
            slot,
            directive_user_origin=not bool(request.get("app", "")),
            # Same-name registration can change while the lock or child probe waits.
            replaced=lambda: _slot_replaced_while_queued(
                state, slot, name, request, "chat.slot_continue"
            ),
        )
    except SlotContinueRefusal as exc:
        if exc.code == "slot_not_found":
            return _slot_not_found()
        return web.json_response({"error": exc.message, "code": exc.code}, status=exc.status)
    return web.json_response({"ok": True, "slot": slot.key})


class SlotContinueRefusal(Exception):
    """A Continue that was refused, carrying the 409 the endpoint renders.

    Raised by :func:`continue_slot_turn` so the Continue endpoint and
    session-control's ``retry_target`` map the same refusals the same way.
    ``code`` is the machine-readable contract; ``message`` is advisory prose.
    """

    def __init__(self, message: str, code: str, status: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


async def continue_slot_turn(
    state: DashboardState,
    slot: _ChatSlot,
    *,
    directive_user_origin: bool,
    via: str = "",
    require_interrupted: bool = False,
    before_dispatch: Callable[[], None] | None = None,
    extra_meta: dict[str, Any] | None = None,
    replaced: Callable[[], bool] | None = None,
) -> str:
    """Queue the Resume/Continue body at the head of *slot* and dispatch it.

    The mechanism behind ``api_chat_slot_continue``, shared with
    ``session_control.retry_target`` and ``session_control.continue_target`` so
    the button and the ``session_retry`` / ``session_continue`` tools run one
    path and refuse the same states. Returns which body was queued:
    ``"resumed"`` for the interrupted pick-up, ``"continued"`` for the carry-on
    message a cleanly finished slot gets. Authorization is the
    caller's: this function checks only whether the slot can take a
    continuation right now, and raises :class:`SlotContinueRefusal` when it
    cannot.

    ``require_interrupted`` refuses a slot whose last turn did not fail
    (``turn_not_failed``); Continue leaves it off. ``before_dispatch`` runs
    synchronously under the slot lock immediately before the queue insert and
    may raise :class:`SlotContinueRefusal`. ``via`` is recorded on the
    ``dashboard_continue`` audit line when set. ``extra_meta`` is merged into
    the queue entry's ``meta`` (session control stamps the sending slot there,
    so a drain-time drop is reported back to it). ``replaced`` is checked after
    each await (the lock, the sub-agent probe) and refuses with a 404
    ``slot_not_found`` when it returns True: the Continue endpoint passes
    ``_slot_replaced_while_queued`` for its slot name.
    """
    async with slot._lock:
        # Same-name registration can change while the lock or child probe waits.
        if replaced is not None and replaced():
            raise SlotContinueRefusal("not found", "slot_not_found", status=404)
        if slot.running:
            raise SlotContinueRefusal("slot is running", "slot_running")
        if slot._stopping or slot._stop_state != "idle":
            raise SlotContinueRefusal("a stop is in progress", "slot_stopping")
        if slot.queue_depth:
            # The runner is about to pick the thread back up on its own; adding a
            # continuation would double-fire.
            raise SlotContinueRefusal("queued messages pending", "slot_queue_pending")
        if any(not f.done() for f in slot._approval_futures.values()):
            raise SlotContinueRefusal("approval pending", "slot_approval_pending")
        # Background sub-agents are still running (or waiting to start) for this
        # slot. `slot.running` is False here — the parent turn ENDS while its
        # children keep going — so nothing above catches this, and the widened
        # gate below makes it the common shape rather than the rare one (before
        # this endpoint accepted a settled transcript, a parent that finished
        # cleanly after `spawn_run` was refused only incidentally, by
        # `_is_interrupted`).
        #
        # It has to be refused HERE rather than left to the queue: a synthetic
        # recovery entry satisfies `is_system_injection_item`, so
        # `_dequeue_next_system_message` drains it straight through the
        # `hold_users` gate that exists to stop exactly this (chat_runner) — the
        # hold only holds plain USER messages. A parent turn would start and
        # interleave tool calls and repository writes with its own children's
        # completion injections. `api_chat` queues instead of dispatching for the
        # same reason; Continue has nowhere to queue to, so it refuses.
        #
        # Children guard — see _subagents_attached_response for the three
        # probes and why each is load-bearing. `effective_session_key`, never
        # `f"dashboard:{slot.key}"`: a channel-born slot's children register
        # under the channel key, and the dashboard-prefixed form silently
        # matches nothing — `_history_key_for`'s own docstring says as much.
        subagents_denied = await _subagents_attached_response(
            state, slot, effective_session_key(slot), "continue"
        )
        if replaced is not None and replaced():
            raise SlotContinueRefusal("not found", "slot_not_found", status=404)
        if subagents_denied is not None:
            raise SlotContinueRefusal("sub-agents are running", "slot_subagents_running")
        if not _has_conversation(slot):
            raise SlotContinueRefusal("nothing to continue", "slot_empty")
        # The same session start has already failed twice in a row with nothing
        # but Resume presses between the attempts. Continue would re-issue the
        # identical ``session/new`` a third time: the turn has no registered
        # session, so it starts one, and nothing about the request changed
        # since the last two walls. This is the one refusal that reads the
        # transcript's CONTENT rather than its shape, and it is not a
        # readiness gate (see the docstring): the evidence is the slot's own
        # two tagged rows, not a probe that can be wrong forever, and typing a
        # message is still allowed -- a user row ends the streak, so a typed
        # retry that fails once gets its Resume back. The first failure keeps
        # today's behaviour exactly: one Resume, same continuation, same words.
        failures = session_start_failure_streak(slot.messages)
        if failures >= _SESSION_START_REPEAT_REFUSAL_AT:
            raise SlotContinueRefusal(
                f"the agent session failed to start {failures} times in a "
                "row; Resume would run the same start again. Restart the "
                "gateway (kirocrew restart), then send your message again.",
                "session_start_repeat",
            )

        # _is_interrupted does not AUTHORIZE the continue — it only picks which
        # body to inject. Both are true statements about their own case, and
        # getting this wrong is not cosmetic: telling a model that finished
        # cleanly that it was "interrupted before it finished" sends it looking
        # for half-done work that does not exist.
        interrupted = _is_interrupted(slot)
        if require_interrupted and not interrupted:
            # session_retry's own refusal: a turn that finished, or that the user
            # ended with Stop, is not a failure to re-run. Continue would carry on
            # from it, but a retry verb must never become a way for another
            # session to make this one produce a fresh answer on top of a good one.
            raise SlotContinueRefusal(
                "the last turn did not fail; there is nothing to retry",
                "turn_not_failed",
            )
        resume = _MANUAL_RESUME_MSG if interrupted else _MANUAL_CONTINUE_MSG
        if before_dispatch is not None:
            # Synchronous, under the lock, with no await between it and the
            # insert below: a caller whose authority was decided before the lock
            # (session control) re-checks it at the last point where refusing
            # still changes nothing.
            before_dispatch()
        # circular import: session_control imports this package's modules at module level.
        from kiro_crew.dashboard.session_control import containment_meta

        # Admission stamp + provenance: recovery-kind entries are subject
        # to drain re-validation like any other externally admitted content, and
        # provenance follows the CALLER — the same request-identity split as
        # api_chat. An app hitting Continue on its own slot must not gain the
        # authenticated-human flag that gates session-mutating effects.
        slot.queue_insert(
            0,
            resume,
            kind=SYNTHETIC_RECOVERY_KIND,
            meta={**containment_meta(state, slot), **(extra_meta or {})},
            directive_user_origin=directive_user_origin,
        )

    # The queue entry is already committed; audit failure must not strand it.
    try:
        sel().log_tool_invocation(
            session_key=_history_key_for(slot.key),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_continue",
            tool_kind="command",
            outcome="ok",
            metadata={"slot": slot.key, **({"via": via} if via else {})},
        )
    except Exception:
        logger.warning("continue: SEL audit failed (slot %s)", slot.key, exc_info=True)
    started = await _start_next_queued_turn(state, slot)
    if not started:
        # Lost a race for the queue entry (a concurrent dequeue consumed it).
        # The turn is running either way, so this is not an error for the caller.
        logger.info("continue: queue entry consumed by a concurrent dequeue (slot %s)", slot.key)
    state.push_slots_update()
    return "resumed" if interrupted else "continued"


#: Consecutive tagged session-start failures at which Continue stops re-running
#: the start. Two, not one: a single timed-out start is host weather and the
#: first Resume is exactly the retry it deserves; the second identical failure
#: is the signal that nothing a retry can change is wrong. Mirrored by
#: ``SESSION_START_REPEAT_REFUSAL_AT`` in ``website/src/pages/chat/ErrorCard.tsx``.
_SESSION_START_REPEAT_REFUSAL_AT = 2

#: ``inject`` kinds that begin a turn of their own rather than continuing the
#: one above them -- the mirror of ``INJECT_KIND_OPENS_TURN`` in
#: ``website/src/pages/chat/RecoveryCard.tsx`` (``recovery`` and
#: ``user_replay`` continue the same turn and are deliberately absent).
_TURN_OPENING_INJECT_KINDS = frozenset({"cron", "synthesis"})


def session_start_failure_streak(messages: list[dict]) -> int:
    """How many session starts in a row failed at the tail of *messages*.

    Walks back from the newest row counting ``error`` rows stamped with
    ``SESSION_START_FAILED_KIND`` (the structural tag ``chat_runner`` writes
    from the exception, never from the prose). Every row that is not the
    conversation's floor is walked past -- the ``inject`` row a Resume press
    lands as, tool rows, notices -- so two failures separated only by the user
    pressing Resume are consecutive. The walk stops at the first row that IS
    new information: a user or assistant row with content (a typed retry is a
    new attempt and starts the count over), an error row of any OTHER kind (a
    connection-lost row is a different failure, not a third start), or a row
    that OPENS a turn of its own -- a nudge, a sub-agent completion, or an
    ``inject`` whose kind begins new work (``_TURN_OPENING_INJECT_KINDS``,
    the mirror of ``INJECT_KIND_OPENS_TURN`` in ``RecoveryCard.tsx``) -- since
    a failure before such a row belongs to a different turn and must not cost
    this turn its first Resume. A ``recovery`` inject resumes the SAME turn and
    is walked past, which is what makes two Resume-separated failures
    consecutive.

    The transcript is the count because nothing else can hold it: a start
    that never answered registered no session, so the per-session
    ``consecutive_failures`` counter in ``SessionManager.record_failure`` is
    never reached for it. Mirrors ``sessionStartFailureStreak`` in
    ``website/src/pages/chat/ErrorCard.tsx``; the two must agree, or the card
    hides a Resume the server would have honoured (or offers one it refuses).
    """
    streak = 0
    for m in reversed(messages):
        role = m.get("role")
        meta = m.get("meta")
        if role == "error":
            kind = meta.get("kind") if isinstance(meta, dict) else None
            if kind == SESSION_START_FAILED_KIND:
                streak += 1
                continue
            break
        if is_stop_event_row(m):
            break
        if role in ("nudge", "subagent"):
            break
        if role == "inject" and (
            isinstance(meta, dict) and meta.get("injectKind") in _TURN_OPENING_INJECT_KINDS
        ):
            break
        if role in ("user", "assistant") and m.get("content") and not is_system_notice(role, meta):
            break
    return streak


def _has_conversation(slot: _ChatSlot) -> bool:
    """True when the transcript holds a real turn to continue FROM.

    The authorization check behind Continue. It is deliberately weak — anything
    a person could look at and say "carry on with that" qualifies — because a
    hard-killed gateway writes no error row, so an interrupted turn is often
    shape-identical to a completed one and no predicate can separate them. The
    button is therefore offered on any idle slot with a transcript, and this
    guard only refuses the one case with nothing to reason about at all: an empty
    slot (or one holding only scaffolding rows such as a compaction notice),
    where a continuation would reach the model with no conversation under it.

    Rows are walked with the same skip rules as ``_is_interrupted`` so the two
    cannot disagree about what counts as the conversation's floor.
    """
    for m in slot.messages:
        if is_system_notice(m.get("role"), m.get("meta")):
            continue
        if m.get("role") in ("user", "assistant") and m.get("content"):
            return True
    return False


def _is_stop_event(m: dict) -> bool:
    """True when *m* is the card recorded because the user pressed Stop.

    Thin alias over ``state.is_stop_event_row`` — the predicate lives there
    (next to ``parse_cls_meta``, its one dependency) so the slot-summary
    builder can share it without importing this handler module.
    """
    return is_stop_event_row(m)


def _is_interrupted(slot: _ChatSlot) -> bool:
    """True when the transcript shows a turn that ended without a reply.

    Thin adapter over ``state.is_turn_interrupted``, which owns the scan and
    its contract (see its docstring). Shared with the slot-summary builder so
    the Continue endpoint, the composer's Resume gate, and the sidebar's
    ``interrupted`` field can never disagree about what an interruption is.
    """
    return is_turn_interrupted(slot.messages)


async def api_chat_slot_end_wait(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/end-wait — ask the sleeping `wait` tool to
    return early. Body: ``{"wait_id": "..."}``.

    Cooperative, and deliberately NOT a cancel. The tool sleeps in a separate
    MCP subprocess that runs no listener, so there is nothing to signal: the
    request is parked on the slot and collected by the tool on its next
    keepalive poll (see WAIT_PING_SECS — bounded at 5s). The turn then continues
    with a normal tool result, which is the whole point of not routing this
    through /stop: /stop can only end a wait as collateral of killing the
    session, losing in-flight results and paying a respawn.

    ``wait_id`` is required and must match the sleep currently in flight. That
    rejects the two races a slot-scoped flag would have accepted: a click landing
    after the wait already elapsed, and a click from a stale tab still showing a
    previous wait's countdown.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_end_wait")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request, allow_absent=True)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    wait_id = str(body.get("wait_id") or "").strip()
    if not wait_id:
        return web.json_response(
            {"error": "wait_id required", "code": "wait_id_required"}, status=400
        )
    current = slot._wait_state or {}
    if current.get("wait_id") != wait_id:
        return web.json_response(
            {"error": "no such wait in flight", "code": "wait_not_in_flight"}, status=409
        )
    slot._end_wait_request = wait_id
    # The button, not a session: clears any requester a session_end_wait left
    # behind, so the woken tool reports the user as the one who ended it.
    slot._end_wait_by = ""
    sel().log_tool_invocation(
        session_key=_history_key_for(name),
        agent=getattr(slot, "agent", "") or "kirocrew",
        source="dashboard",
        tool_name="dashboard_end_wait",
        tool_kind="command",
        outcome="success",
        metadata={"slot": name, "wait_id": wait_id},
    )
    return web.json_response({"ok": True})


async def api_chat_slot_interrupt(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/interrupt — run a selected queued message.

    A running parent turn is stopped while its queue is preserved for the normal
    tail drain. An idle parent requires ``{"queue_id": "..."}`` and dispatches
    that selected queue card directly; this is the explicit override for a user
    who does not want to wait for attached subagents. A running parent accepts
    ``queue_id`` optionally to promote one card before the preserved queue drains.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    # Before the _stop_state claim and the queue promotion below, both of which
    # mutate the slot ahead of stop_turn.
    # Resolved once, before the request-body await below, and used for both the
    # guard and the cancel — see api_chat_slot_stop for the same rule.
    cancel_key = _cancel_target(slot)
    denied = _app_cancel_denied(request, slot, "chat_interrupt", cancel_key)
    if denied is not None:
        return denied
    if not slot.running:
        if not slot._queue:
            return web.json_response({"ok": True, "info": "not running"})
        refusal = relay_archive_refusal(slot)
        if refusal is not None:
            return refusal
        body, body_err = await read_bounded_json(request, allow_absent=True)
        if body_err is not None:
            return body_err
        assert body is not None  # read_bounded_json returns (dict, None) on success
        raw_queue_id = body.get("queue_id")
        if raw_queue_id is not None and not isinstance(raw_queue_id, str):
            return web.json_response(
                {"error": "queue_id must be a string", "code": "invalid_queue_id"},
                status=400,
            )
        queue_id = (raw_queue_id or "").strip() or None
        # The idle bypass is the "run THIS selected card while attached
        # subagents keep going" action, and nothing else. Requiring an explicit
        # queue_id keeps `allow_user_during_subagents=True` and the dispatch
        # itself bound to a card the user picked: without one there is no
        # selection to justify bypassing the child-work hold, and dispatching
        # whatever sits at the queue front would run — and acknowledge —
        # unselected work the user never chose. Reject rather than fall back.
        if queue_id is None:
            return web.json_response(
                {"error": "queue_id required for idle interrupt", "code": "invalid_queue_id"},
                status=400,
            )
        async with slot._lock:
            # The request body and lock acquisition both yield. A close followed
            # by same-name recreation during either await must not let this stale
            # object dispatch work into the replacement's session namespace.
            if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_interrupt"):
                return slot_not_found()
            if slot.running:
                return web.json_response(
                    {"error": "slot started running", "code": "slot_running"}, status=409
                )
            if slot._stopping or slot._stop_state != "idle":
                return web.json_response(
                    {"error": "a stop is in progress", "code": "slot_stopping"}, status=409
                )
            # The body read above can race a cron/workflow rebind on this same
            # live slot. Re-authorize the session the queued turn will use while
            # holding the dispatch lock, immediately before starting it.
            denied = _app_cancel_denied(
                request, slot, "chat_interrupt", effective_session_key(slot)
            )
            if denied is not None:
                return denied
            started = await _start_next_queued_turn(
                state,
                slot,
                allow_user_during_subagents=True,
                required_queue_id=queue_id,
            )
        if not started:
            return web.json_response(
                {
                    "error": "queued message is no longer available",
                    "code": "queue_item_unavailable",
                },
                status=409,
            )
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_interrupt",
            tool_kind="command",
            outcome="started",
            metadata={"slot": name, "queue_id": queue_id},
        )
        state.push_slots_update()
        return web.json_response({"ok": True, "outcome": "started"})
    # Idempotent guard: interrupt already in progress. State alone decides —
    # do NOT also require _stop_event_id: after the early soft_pending claim
    # below, a concurrent request can arrive before the stop card is created
    # (event id still None), and a compound condition would let it through.
    if slot._stop_state != "idle":
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_interrupt",
            tool_kind="command",
            outcome="noop",
            metadata={"slot": name, "reason": "stop already in progress"},
        )
        return web.json_response({"ok": True, "info": "stop already in progress"})
    if not slot._queue:
        return web.json_response({"error": "queue empty, use /stop instead"}, status=400)

    # An automatic compaction holds the session: the interrupt is declined
    # BEFORE the claim below mutates the running turn (``_stop_state``) and
    # before ``_unblock_pending_waits`` rejects its pending
    # approvals -- none of that may happen to a turn that is not being stopped.
    # No escalation marker: an interrupt is "run the next queued message", not
    # a Stop, and must not turn the user's next Stop press into a hard kill.
    # ``_stop_state`` stays idle.
    if _compaction_in_flight(state, cancel_key):
        stop_id = _open_stop_event_card(slot, "interrupting")
        _resolve_stop_event(slot, "compacting")
        slot._stop_event_id = None
        state.push_slots_update()
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_interrupt",
            tool_kind="command",
            outcome="compacting",
            metadata={"slot": name, "stop_id": stop_id},
        )
        return web.json_response({"ok": True, "outcome": "compacting", "compacting": True})

    # Claim the stop slot synchronously BEFORE the await below: the
    # idempotency guard above is check-then-act, and a concurrent /interrupt
    # arriving during the awaited body read below would otherwise still see
    # _stop_state == "idle" and slip past the guard (double stop_turn +
    # double SEL audit for one logical press). /stop is race-safe because it
    # has no await between guard and claim; this makes /interrupt match.
    slot._stop_state = "soft_pending"
    # Per-attempt identity for the claim itself. The stand-down guard below
    # cannot rely on the state VALUE alone: a concurrent /stop can escalate,
    # settle to idle, and a further press can re-claim "soft_pending" — a
    # LATER stop wearing the same value. The generation tells the two apart
    # (`_make_stop_resolver` already establishes it as the only per-attempt
    # identity that survives card reuse); the claim above bumped it, so any
    # later initiation moves it again.
    claim_generation = slot._stop_generation

    # Optionally promote a specific queue item to front. The except is not a
    # parse guard (read_bounded_json owns that): it rolls the claimed stop
    # state back when the body read fails in transit, and the refused-body
    # branch below rolls it back the same way. The rollback is
    # conditional on our claim being intact: a concurrent /stop arriving
    # during the body await may escalate _stop_state (e.g. to "killing"),
    # and an unconditional reset to "idle" would erase that escalation and
    # admit another stop while the hard kill is still running.
    try:
        body, body_err = await read_bounded_json(request, allow_absent=True)
    except Exception:
        # Generation-guarded like the stand-down below: "soft_pending" alone
        # cannot prove the claim is OURS — an escalate-settle-repress sequence
        # during the await leaves a LATER press's live claim wearing the same
        # value, and rolling that back would idle its stop mid-cancel and
        # re-enable auto-run under a real stop.
        if slot._stop_state == "soft_pending" and slot._stop_generation == claim_generation:
            slot._stop_state = "idle"
        raise
    if body_err is not None:
        if slot._stop_state == "soft_pending" and slot._stop_generation == claim_generation:
            slot._stop_state = "idle"
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    queue_id = body.get("queue_id")
    if queue_id and slot._stop_generation == claim_generation:
        # Wire-side field is `queue_id`; stored items carry `id` (the key
        # queue_append/queue_insert write and every *_by_id helper matches).
        # The previous inline loop compared item.get("queue_id"), which is
        # None on every production item — a silent no-op that made the
        # "run this next" click land on whatever happened to be at the
        # front of the queue instead of the selected message.
        #
        # BEFORE the supersede guard below, and GENERATION-GATED, because the
        # supersessions differ: a claim superseded benignly (the running turn
        # ends during the body read, teardown resets the posture, generation
        # unmoved) must still land the user's "run this next" choice; a claim
        # superseded by a LATER stop (generation moved) must NOT — that stop's
        # own /interrupt may have promoted ITS selection, and a stale write
        # here would overwrite it. Escalation keeps the same generation and a
        # cleared queue, so promotion there is a harmless no-op.
        slot.queue_promote_by_id(queue_id)

    # A concurrent /stop can supersede our claim during the body await:
    # escalate it (soft_pending → killing), or escalate-settle-and-be-followed
    # by a FURTHER press whose fresh claim wears the same "soft_pending" value
    # — which is why this compares the GENERATION, our claim's per-attempt
    # identity, not just the state value. Continuing on a superseded claim
    # would open/reuse a card owned by the other stop — and the reuse path's
    # marker-clear would erase a LIVE escalation marker, letting a late
    # cooperative ack relabel the hard kill as a clean stop. The other stop
    # owns the posture now: stand down and answer like the idempotent-repeat
    # branch above. This also fires when the superseding stop has ALREADY settled
    # (state back to "idle"), including the benign case where the running
    # turn simply ended during the body read; queue promotion already
    # happened above, so nothing of the user's intent is dropped.
    if slot._stop_state != "soft_pending" or slot._stop_generation != claim_generation:
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_interrupt",
            tool_kind="command",
            outcome="noop",
            metadata={"slot": name, "reason": "stop claim superseded during body read"},
        )
        return web.json_response({"ok": True, "info": "stop already in progress"})

    # The probe again, AFTER the body await and with no await between here and
    # the pending-wait rejection below. A compaction that committed during the
    # body read would otherwise have the user's pending approval or question
    # rejected (irreversible) by a Stop the compaction then declines. Same
    # answer as the pre-check: the claim is released, nothing is touched.
    if _compaction_in_flight(state, cancel_key):
        slot._stop_state = "idle"
        stop_id = _open_stop_event_card(slot, "interrupting")
        _resolve_stop_event(slot, "compacting")
        slot._stop_event_id = None
        state.push_slots_update()
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_interrupt",
            tool_kind="command",
            outcome="compacting",
            metadata={"slot": name, "stop_id": stop_id, "reason": "committed during body read"},
        )
        return web.json_response({"ok": True, "outcome": "compacting", "compacting": True})

    # Stop current turn but preserve the queue so dequeue loop fires
    # (soft_pending already claimed above, before the request-body await)

    # One card per press: re-arm an orphaned card in place or append a fresh
    # one (see _open_stop_event_card for why sweeping the orphan rendered two
    # chips).
    stop_id = _open_stop_event_card(slot, "interrupting")
    state.push_slots_update()

    # Built after the card exists so each resolver is bound to this card.
    _on_soft = _make_stop_resolver(state, slot, "soft", stop_id)
    _on_hard = _make_stop_resolver(state, slot, "hard", stop_id)

    # Unblock chat runner if it's suspended waiting for tool approval or on a
    # pending ask_question card.
    _unblock_pending_waits(state, slot)

    outcome = await state.sessions.stop_turn(
        cancel_key,
        force=False,
        preserve_queue=True,
        on_soft=_on_soft,
        on_hard=_on_hard,
    )
    # Resolve orphaned card when provider reports no active turn
    if outcome == "idle" and slot._stop_event_id:
        _resolve_stop_event(slot, "soft")
        slot._stop_state = "idle"
        state.push_slots_update()
    elif outcome == "compacting":
        # The window the two probes above cannot close: a compaction commits
        # between the second probe and ``stop_turn`` taking the registry lock,
        # with no await of ours in between, so only another task's tick can
        # land here. Nothing was interrupted. The pending waits
        # ``_unblock_pending_waits`` rejected cannot be un-rejected, which is
        # why both probes run first. ``_stop_state`` goes back to idle; no
        # escalation marker, for the reason the pre-check gives.
        _resolve_stop_event(slot, "compacting")
        slot._stop_event_id = None
        slot._stop_state = "idle"
        state.push_slots_update()
    sel().log_tool_invocation(
        session_key=_history_key_for(name),
        agent=getattr(slot, "agent", "") or "kirocrew",
        source="dashboard",
        tool_name="dashboard_interrupt",
        tool_kind="command",
        outcome=outcome,
        metadata={"slot": name, "queue_id": queue_id},
    )
    return web.json_response({"ok": True, "outcome": outcome})


async def api_chat_slot_queue_cancel(request: web.Request) -> web.Response:
    """DELETE /api/chat/slots/{slot}/queue/{queue_id} — cancel a queued message.

    Removes the message from the backend queue and broadcasts a
    ``queue_cancel`` WebSocket event so the frontend can move the
    text back to the input box.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    queue_id = request.match_info["queue_id"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_queue_cancel")
    if denied is not None:
        return denied
    # Read the entry's origin before removing it: a cancel puts the text back in
    # the composer, so a redacted copy of the user's own words would replace the
    # link they typed with a placeholder.
    _entry = next((i for i in slot._queue if i["id"] == queue_id), None)
    _user_origin = queue_entry_is_user_origin(_entry)
    content = slot.queue_remove_by_id(queue_id)
    if content is None:
        return web.json_response({"error": "queue item not found"}, status=404)
    _remove_queued_by_id(slot.messages, queue_id)
    slot.invalidate_source_links()
    # A cross-session delivery has a sender holding a "queued, will run later"
    # receipt, and the retracted card below is on this session, which the sender
    # does not read. Its own transcript is told the message will never run.
    # circular import: session_control imports this package's modules at module level.
    from kiro_crew.dashboard.session_control import (
        SEND_DISCARD_QUEUE_CANCEL,
        notify_send_origin_discarded,
    )

    notify_send_origin_discarded(
        state,
        entry_meta=_entry.get("meta") if _entry is not None else None,
        target_slot=slot,
        text=content,
        cause=SEND_DISCARD_QUEUE_CANCEL,
    )
    _redacted = queued_text_for_display(content, user_origin=_user_origin)
    state.broadcast_ws("queue_cancel", {"slot": name, "queue_id": queue_id, "content": _redacted})
    state.push_slots_update()
    sel().log_tool_invocation(
        session_key=f"dashboard:{name}",
        agent="kirocrew",
        source="dashboard",
        tool_name="queue_cancel",
        tool_kind="permission",
        outcome="allowed",
        metadata={"queue_id": queue_id, "slot": name},
    )
    return web.json_response({"ok": True, "content": _redacted})


async def api_chat_slot_queue_edit(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/queue/{queue_id} — edit a queued message.

    Accepts ``{"content": "new text"}`` and replaces the content of the
    matching queue item in place (order preserved).  Broadcasts a
    ``queue_edit`` WebSocket event so all connected clients update in sync.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    queue_id = request.match_info["queue_id"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_queue_edit")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        return web.json_response({"error": "content must be a non-empty string"}, status=400)
    if not slot.queue_edit_by_id(
        queue_id,
        content,
        directive_user_origin=not bool(request.get("app", "")),
    ):
        return web.json_response({"error": "queue item not found"}, status=404)
    # The stored text is what the edit normalized to (attachment markers are
    # renumbered when the edit dropped one), so the row and the broadcast echo
    # the ENTRY, not the request body.
    entry = next((i for i in slot._queue if i["id"] == queue_id), None)
    stored = entry.get("content") if entry is not None else None
    if isinstance(stored, str):
        content = stored
    _edit_queued_by_id(slot.messages, queue_id, content)
    slot.invalidate_source_links()
    _redacted = queued_text_for_display(content, user_origin=queue_entry_is_user_origin(entry))
    frame: dict[str, Any] = {"slot": name, "queue_id": queue_id, "content": _redacted}
    # The edit prunes and renumbers the entry's attachment lists alongside the
    # text (`prune_attachment_meta`), so the frame carries the lists the
    # renumbered markers now index -- the client replaces the row's lists from
    # it. Same `meta` shape and redaction as `queue_entry_view`, read straight
    # off the entry so the content is not redacted a second time. Absent when
    # the entry has none left (or never had any): the client reads absence on
    # THIS frame as "no lists", so a row whose markers the edit all removed
    # drops its stale lists too.
    _edit_attachments = attachment_meta(entry.get("meta")) if entry is not None else {}
    if _edit_attachments:
        frame["meta"] = _edit_attachments
    state.broadcast_ws("queue_edit", frame)
    state.push_slots_update()
    sel().log_tool_invocation(
        session_key=f"dashboard:{name}",
        agent="kirocrew",
        source="dashboard",
        tool_name="queue_edit",
        tool_kind="permission",
        outcome="allowed",
        metadata={"queue_id": queue_id, "slot": name},
    )
    return web.json_response({"ok": True, "content": _redacted})


async def api_chat_slot_queue_reorder(request: web.Request) -> web.Response:
    """PUT /api/chat/slots/{slot}/queue/order — reorder queued messages.

    Accepts ``{"order": ["qid1", "qid2", ...]}`` and rearranges the slot's
    ``_queue`` to match the given id sequence.  Broadcasts a ``queue_reorder``
    WebSocket event so all connected clients update in sync.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_queue_reorder")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    order = body.get("order")
    if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
        return web.json_response({"error": "order must be a list of queue id strings"}, status=400)
    # Build lookup of current queue items by id
    by_id = {item["id"]: item for item in slot._queue}
    # Validate all ids exist
    missing = [qid for qid in order if qid not in by_id]
    if missing:
        return web.json_response({"error": f"unknown queue ids: {missing}"}, status=400)
    # Reorder: place requested ids first in given order, then any remaining
    reordered = [by_id[qid] for qid in order if qid in by_id]
    remaining = [item for item in slot._queue if item["id"] not in set(order)]
    slot._queue[:] = reordered + remaining
    # Reorder the queued messages in the messages list to match
    queued_msgs = [m for m in slot.messages if m.get("role") == "queued"]
    other_msgs = [m for m in slot.messages if m.get("role") != "queued"]
    queued_by_id: dict[str | None, dict] = {}
    for m in queued_msgs:
        try:
            cls = json.loads(m.get("cls", "{}"))
            queued_by_id[cls.get("queue_id")] = m
        except (json.JSONDecodeError, TypeError):
            pass
    reordered_msgs = [queued_by_id[qid] for qid in order if qid in queued_by_id]
    remaining_msgs = [m for m in queued_msgs if m not in reordered_msgs]
    slot.messages[:] = other_msgs + reordered_msgs + remaining_msgs
    slot.invalidate_source_links()
    state.broadcast_ws(
        "queue_reorder", {"slot": name, "order": [item["id"] for item in slot._queue]}
    )
    state.push_slots_update()
    sel().log_tool_invocation(
        session_key=f"dashboard:{name}",
        agent="kirocrew",
        source="dashboard",
        tool_name="queue_reorder",
        tool_kind="permission",
        outcome="allowed",
        metadata={"slot": name, "order_len": len(order)},
    )
    return web.json_response({"ok": True})


class SlotCloseError(Exception):
    """A close that could not complete, carrying the response the tab-✕ path
    would have rendered.

    Extracted alongside :func:`close_slot` so the DELETE endpoint and
    session-control's ``close_target`` map the SAME four failures the same way.
    ``code`` is the machine-readable contract; ``message`` is advisory prose;
    ``status`` is 500 for every close failure (each leaves the tab open and
    every partial step rolled back — a state the user can see and retry).
    """

    def __init__(self, message: str, code: str, status: int = 500) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


# Ceiling on how long a close waits for this slot's guarded history writes to
# finish. The wait is bounded so a genuinely stuck write cannot hang the tab
# close: on breach the close is REFUSED and rolled back, which returns promptly
# and leaves the tab the user still has open, rather than retracting the name
# while a worker thread is still on its way to the rename.
_GUARDED_WRITE_WAIT_SECS = 5.0


async def _persist_app_agent_owner(state: DashboardState, slot: _ChatSlot) -> bool:
    """Durably record *slot*'s app-agent binding before its create is answered.

    A line-less newborn gets the full metadata line a first save would write;
    a slot that already has a line takes the force save, whose merge carries
    the binding. False when neither write committed.
    """
    log = state.conversation_log
    if log is None:
        return False
    history_key = slot_history_key(slot)
    from kiro_crew.dashboard.slot_persistence import metadata_codec

    line = {
        "_type": "metadata",
        **metadata_codec.encode(
            slot,
            folds=metadata_codec.SaveFolds(
                memory_mode=getattr(slot, "memory_mode", "persistent"),
                created_at=slot.created_at,
                tab_id=getattr(slot, "_tab_id", None),
            ),
        ),
    }
    try:
        if await drained_to_thread(
            log.update_metadata_if, history_key, line, lambda meta: not meta
        ):
            return True
        return await save_slot_off_loop(
            state,
            slot,
            force=True,
            best_effort=False,
            expected_history_key=history_key,
        )
    except Exception:
        logger.warning(
            "chat_slot_create: could not persist the app agent owner for slot=%s",
            slot.key,
            exc_info=True,
        )
        return False


# Serializes slot SWITCH transactions that share one session, keyed by
# ``effective_session_key``. The per-slot locks the switch handlers take
# (``slot._lock``, ``slot._model_pick_lock``) are created per ``_ChatSlot``,
# so two switches arriving through DIFFERENT alias slots that resolve onto
# ONE session take disjoint locks and neither waits for the other: both
# commit, both reset the shared session, and the two slots' committed
# settings can end up disagreeing with each other and with the live
# provider. Same shape and same reason as ``_autocompact_txn_locks`` below
# ("channel-linked aliases resolve distinct slot names onto one file"), keyed
# by the SESSION the switch handlers probe and reset rather than by the
# transcript.
#
# LOCK ORDER — the one place it is written down. ``slot._lock``, then the
# session lock, then ``slot._model_pick_lock``. Every switch handler acquires
# them in that order and nothing acquires them in the opposite one, so two
# aliases contending on one session cannot cycle: a holder of the session lock
# already holds its own ``slot._lock`` and never waits for another slot's.
# Unrelated slots resolve to DIFFERENT keys and so take different locks: this
# serializes aliases of ONE session, never one slot against another session's
# switch. A WeakValueDictionary so a session's lock is collected once no
# request holds it.
#
# WHY THE SESSION LOCK IS ENTERED SECOND, THROUGH AN ExitStack. Its key is
# ``effective_session_key(slot)``, and that value is only trustworthy once
# ``slot._lock`` is held: a channel/cron rebind can land while a request
# queues, which is why every handler deliberately resolves the key INSIDE its
# lock (pinned by
# ``test_binding_that_lands_while_queued_on_the_lock_is_the_one_switched`` --
# the binding that lands is the one switched). Keying the session lock on any
# EARLIER read would be unsound in exactly that case: the handler would hold
# the lock for the PREVIOUS session while probing and resetting the new one,
# so a concurrent alias switch on the new session would not be serialized
# against it -- and the handlers' post-await re-checks cannot catch it,
# because they compare ``effective_session_key(slot) != session_key`` and
# session_key would already BE the new key. Entering the lock after the
# in-lock read makes the lock key and the acted-on key THE SAME VALUE BY
# CONSTRUCTION, so there is no window to guard and no new decision point to
# get wrong. The ExitStack is what lets a lock be acquired mid-block without
# nesting the whole remaining transaction one level deeper.
#
# The lock itself lives in ``kiro_crew.llm_helpers``
# (``slot_switch_session_lock``) so the chat runner's refusal-fallback
# restore can take the SAME lock — this module imports from the runner, so
# the runner cannot import it from here without a cycle. The local name is
# kept for the acquisition sites below.
_slot_switch_session_lock = slot_switch_session_lock


#: The chat picker's "Auto (Jev)" entry, as the id a client sends for it. NOT a
#: provider model id and never stored in ``slot.model``: it asks the
#: ``model.route`` decision point to pick a difficulty tier for each turn, and
#: until it answers the session runs on the backend default. The prefix is
#: ``auto`` so a client too old to know the entry (or a backend without the point)
#: reads it as the Auto it behaves like, and the colon keeps it outside the model
#: namespace -- no advertised id carries one.
JEV_ROUTE_MODEL = "auto:jev"


# Per-slot transaction locks for the autocompact endpoint. The write span
# below contains awaits (body read, forced save), so two concurrent POSTs for
# one slot can interleave: each captures the other's value as its rollback
# snapshot, and a failed request's compare-and-swap rollback can then erase a
# newer request's acknowledged write (value equality cannot identify
# ownership when both requests carry the same pct). Serializing the whole
# reauthorize -> pin -> mutate -> persist -> rollback -> live-map span per
# slot makes the rollback unambiguous: only one request is ever inside the
# span, so a rollback can only undo its own write. WeakValueDictionary so an
# idle slot's lock is reclaimed with its last reference. Keyed by the
# TRANSCRIPT (slot_history_key), not the slot: channel-linked aliases resolve
# distinct slot names onto one file, and two requests through different alias
# slots must serialize against each other or the loser's rollback/flush can
# overwrite the winner's acknowledged durable write. A rebind mid-request is
# handled by the expected_history_key pin and the post-persist
# reauthorization, not by the lock key.
_autocompact_txn_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = (
    weakref.WeakValueDictionary()
)


# Same per-transcript transaction lock, for the source-link unlink write. A
# dismissal is persisted into the shared transcript metadata, so concurrent
# unlinks (or an unlink racing a sibling flush) on alias slots that resolve onto
# one transcript must serialize or a loser's rollback / a stale sibling can
# overwrite the winner's acknowledged commit. Keyed by transcript, like above.
_source_link_txn_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = (
    weakref.WeakValueDictionary()
)


# Fields carried per follow-up item on the wire. Kept explicit so a future
# schema addition has to be added here deliberately rather than leaking
# whatever the model happened to send into the broadcast payload.
_FOLLOWUP_TEXT_FIELDS = ("title", "description", "prompt")


def deny_non_dashboard_caller(request: web.Request, operation: str) -> web.Response | None:
    """403 unless this is the dashboard OWNER's own request, else None.

    Deny-by-default, matching ``api_chat_slots_model``'s reasoning: the auth
    middleware sets ``request["app"]`` on every authenticated path (``""`` for
    dashboard users, the app name for app tokens), so an ABSENT key means the
    middleware did not run and must refuse rather than fall through.

    An app claim of ``""`` is necessary but NOT sufficient. Every surface guarded
    here acts on owner-scoped resources — the card renders in the owner's composer,
    and the worktree allow-list is built from every slot's project — so identity
    is checked with ``is_owner_dashboard_request``, the same predicate the source
    provider mutations use: the caller must match the configured ``owner_id``, or
    be a signed local bootstrap subject when no owner is configured (the
    standalone-local case, where the browser's own token is minted for
    ``local-app``). A dashboard token issued for a different subject would
    otherwise mutate repositories it does not own.

    ONE exception, and it is the path every MCP call arrives on: a request that
    presented a valid ``X-Internal-Secret`` from loopback is granted by the
    middleware WITHOUT an app claim (there is no app identity to set), so it
    carries ``request["internal_auth"] is True`` instead. Refusing that would
    403 ``suggest_followup`` outright — the tool could never raise a card.
    """
    if request.get("internal_auth") is True:
        return None
    # Imported here, not at module scope: source_providers imports chat state
    # helpers, so a top-level import would close a cycle (same pattern as the
    # owner-only check-status gate in api_chat_slots).
    from kiro_crew.dashboard.handlers._shared import _owner_denial_response  # noqa: F811
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    if not is_owner_dashboard_request(request):
        # Domain-specific audit kept here rather than delegated to
        # ``require_owner_dashboard_request``: this record carries its own
        # ``error`` reason and no ``resources``, and the ``sel`` it reaches is
        # this module's, which is what the coverage tests patch. Only the denial
        # TAIL (stale-session relabel + 403) is shared.
        try:
            sel().log_api_access(
                caller=str(request.get("user") or "anonymous"),
                operation=operation,
                outcome="denied",
                source="dashboard",
                error="not the dashboard owner",
            )
        except Exception:  # pragma: no cover - audit is best-effort
            logger.debug("SEL audit failed for %s denial", operation, exc_info=True)
        # Deny decision made above; only the response label changes for a
        # signed pre-owner bootstrap subject (see stale_owner_session_response).
        return _owner_denial_response(request, "forbidden")
    return None


_MAX_RECENT_PROJECTS = 100


# Favourites are a SEPARATE file from the recents list, not a flag on its rows, because
# the two have different lifetimes: recents is a rolling window capped at
# _MAX_RECENT_PROJECTS and rewritten on every project switch, so a favourite stored
# there would fall off the end of a busy week. There is no cap: only the dashboard owner
# can write the file (both write verbs are owner-gated), so a bound would have no caller
# to defend against, and a list the user curates by hand should not refuse an entry.
#
# Both write verbs are a read-modify-write of one file, run on worker threads, so two
# overlapping requests (two dashboard tabs) would each write back the snapshot they read
# and one change would be lost. One lock serialises the whole transaction for both.
_favorite_projects_lock = threading.Lock()


# Bound for normalising the non-string ``content`` a legacy or hand-edited
# transcript row can carry (nested dict/list of multi-part content). We do NOT
# redact-in-place and keep the structure: two independent problems make that
# unsafe. (1) Redacting only string LEAVES leaves dict KEYS unscrubbed, so a
# credential sitting in a key reaches the broadcaster. (2) The downstream
# persistence/display paths (``_build_message_entry_uncached``, ``_prepare_messages``)
# call the string-only redactors on ``content`` directly and raise ``TypeError``
# on a non-string, so a structured row the slot accepted cannot be saved -- the
# crash the parent revision had is only moved, not removed. Instead we NORMALISE
# such content to a single JSON string and redact THAT, which scrubs keys and
# values alike and yields a row every downstream path can serialise. Bounded so
# a corrupt/hostile row cannot blow the stack: ``json.dumps`` with a depth-safe
# default; on any failure we fall back to a fixed placeholder rather than raise.
# Cap on the serialised size of a normalised structured-content row. A single
# legitimate transcript row is far below this; a serialisation larger than the
# transfer-bounds ceiling is a malformed/hostile row and is dropped to the
# placeholder rather than run through the GIL-held redactors.
_STRUCTURED_CONTENT_MAX_CHARS = 20_000_000
_STRUCTURED_CONTENT_PLACEHOLDER = "[unsupported structured content removed]"

#: Extra off-loop re-reads a resume gives its OWN post-reopen metadata reads
#: (the ``clear_closed`` verification read and the final identity re-read) when
#: they come back unreadable. Those reads open a file this resume just rewrote,
#: and on Windows a just-written file is briefly unopenable while an indexer or
#: AV scanner holds it; on a loaded runner that hold can outlast
#: ``get_metadata_status``'s own bounded retry. An unreadable read stays
#: fail-closed (the handler refuses, never publishes blind), so these extra
#: tries only widen the window the transient has to clear before that refusal —
#: they never accept an unreadable line. Sized well above the few-ms transient
#: without stalling a genuinely-gone or corrupt line for long: 8 tries at 25 ms
#: is a 200 ms ceiling, paid only on the rare unreadable path.
_REOPEN_REREAD_EXTRA_ATTEMPTS = 8
_REOPEN_REREAD_RETRY_SECS = 0.025


class ResumeRefusal(NamedTuple):
    """One refusal of :func:`resume_slot_from_history`, shaped for the wire.

    Every refusal carries a machine-readable ``code`` (the error-code contract),
    including the missing conversation log (``no_conversation_log``) and the
    app-isolation 404 on a live slot (``slot_not_found``), so the wire wrapper
    can emit one transparent coded body for every status.
    """

    error: str
    code: str
    status: int


#: The one refusal an app gets from resume for a session it may not open. It is
#: the body the per-slot checkpoint answers, so resume cannot tell an app "not
#: yours" from "does not exist" either.
_RESUME_APP_NOT_FOUND = ResumeRefusal("not found", "slot_not_found", 404)


async def _app_resume_refusal(
    state: "DashboardState", request_app: str, name: str, history_key: str, meta: dict
) -> ResumeRefusal | None:
    """Whether an app may resume *history_key* into a slot published as *name*.

    Two transcripts are at stake, and both must be the app's. The one READ is
    *history_key*, whose metadata line *meta* must record this app. The one the
    new slot WRITES is ``dashboard:<name>``, because a resumed slot saves under
    its own key: when that is a different key, the same acquisition rule as
    send and create applies to it (no member, cron or workflow name; no
    transcript there that records anyone else). Without the second check an app
    could load its own transcript under the name of a person's closed session
    and be handed that session's live slot.

    A publish name under construction is the same 404, with no audit row, as on
    send and create: a session being imported is nobody's to name yet. So is one
    that aliases another live slot by letter case, recorded as on send and create.
    """
    log = state.conversation_log
    if not app_owns_transcript_meta(meta, request_app):
        # Re-read to file the refusal under its real reason: an unreadable line
        # is a read fault, not an isolation breach, and no transcript at all is a
        # missing session, which is answered without an audit row.
        reason = ""
        if log is not None:
            reason = await asyncio.to_thread(
                transcript_acquisition_reason, log, history_key, request_app
            )
        if reason:
            audit_app_slot_denial(request_app, "slot_resume", name, reason)
        return _RESUME_APP_NOT_FOUND
    publish_key = _history_key_for(name)
    refused, reason = app_new_key_refusal(state, name, publish_key)
    if not refused and publish_key != history_key and log is not None:
        reason = await asyncio.to_thread(
            transcript_acquisition_reason, log, publish_key, request_app
        )
        refused = bool(reason)
    if reason:
        audit_app_slot_denial(request_app, "slot_resume", name, reason)
    return _RESUME_APP_NOT_FOUND if refused else None


class ResumeOutcome(NamedTuple):
    """What :func:`resume_slot_from_history` decided.

    Exactly one of ``refusal`` / ``slot`` is set. ``already_live`` marks the
    dedup arm: the session was already open, so ``slot`` is the EXISTING slot
    and nothing was hydrated; ``total`` is the effective hydrated length on the
    hydrate arm (durable rows plus a recovered interruption row when one was
    appended), what the wrapper's ``next_before`` is derived from.
    """

    refusal: ResumeRefusal | None = None
    slot: "_ChatSlot | None" = None
    already_live: bool = False
    total: int = 0


MAX_COLOR_INDEX = 20


_MAX_CONTEXT_PER_SOURCE = 10
_MAX_CONTEXT_CONTENT = 40000
# Default expiry for a note's context half: if the user never sends a follow-up
# within 24h, the stale entry is dropped at drain rather than attaching itself to
# some far-future unrelated message. The visible transcript line has no maxAge.
_NOTE_CONTEXT_MAX_AGE = 86400
# Bounds the visible lines a caller can park on one in-flight turn. Matches the
# per-source context cap so neither half of /note outlives the other by much.
# Shared with the persistence restore path (which enforces the same cap on notes
# read back from disk), so the value lives in slot_buffers.
_MAX_DEFERRED_NOTES = MAX_DEFERRED_NOTES

# Distinguishes "key absent" from an explicit JSON null, which `body.get("maxAge")`
# alone cannot: both yield None, so the two cannot mean different things without it.
_UNSET = object()

# Source label bounds. The label is interpolated into the
# ``[Background context from "{source}"]`` prompt frame at drain, so disallow
# control chars and newlines to keep a crafted label from breaking out of the
# frame line, and cap the length. Defense-in-depth: the real free-form surface
# is ``content``, not ``source``. The bound lives once in ``slot_buffers``
# (admit side here, restore side there read the SAME pair) so they cannot drift.
_MAX_SOURCE_LEN = MAX_SOURCE_LABEL_LEN
_SOURCE_CTRL_RE = SOURCE_LABEL_CTRL_RE


def _reauthorize_after_await(
    state: DashboardState, slot: _ChatSlot, name: str, request_app: str, operation: str
) -> web.Response | None:
    """Re-authorize *slot* after an await, immediately before touching it.

    The ownership gate necessarily runs before the request body is read, and
    that ``await`` is a window rather than a formality: ``linked_session_key``
    is rebound on ALREADY-LIVE slots with no ``running`` gate -- a cron
    completion (``cron_inject.py:96``), a workflow injection
    (``workflow_inject.py:156``) -- so a slow caller can be authorized against
    its own session and land on somebody else's conversation. The same identity
    check ``_app_cancel_denied`` makes for /stop, moved to the point of use.

    Requires the same slot OBJECT, not just the same name: a delete and
    re-create under one name would pass an ownership re-check while being a
    different conversation. Callers must run this before the first read of slot
    state too, since ``running`` and the hold queue belong to whichever
    conversation the slot now routes to.
    """
    if state._slots.get(name) is not slot:
        if request_app:
            sel().log_api_access(
                caller=request_app,
                operation=operation,
                outcome="denied",
                source="app_isolation",
                resources=f"slot={name}",
                error="slot was replaced while the request body was read",
            )
        return slot_not_found()
    return deny_app_slot_session_access(request_app, slot, name, operation)


# Every function the owners define runs on this module's globals, so a patch of
# ``kiro_crew.dashboard.chat_handlers.<name>`` reaches it wherever it lives; see
# ``kiro_crew.dashboard.chat_api``. Run once, after this body has bound every name.
_chat_api.compose(
    globals(),
    (
        _owner_agent_switch,
        _owner_followup_card,
        _owner_pending_context,
        _owner_project_lists,
        _owner_resume,
        _owner_session_relaunch,
        _owner_slot_approval,
        _owner_slot_detail,
        _owner_slot_effort,
        _owner_slot_guards,
        _owner_slot_lifecycle,
        _owner_slot_model,
        _owner_slot_note,
        _owner_slot_settings,
        _owner_source_links,
    ),
)


# How long one SSE write may wait for the client to take the bytes. aiohttp parks
# a write once the socket and its own buffer are full, and resumes it when the
# client has read the buffer down by about 48 KiB, so a reader that is merely
# slow (a few hundred bytes a second) finishes every write well inside this.
_SSE_WRITE_DEADLINE_SECS = 120.0


async def write_sse_or_abort(request: web.Request, resp: web.StreamResponse, data: bytes) -> None:
    """Write one SSE frame; abort the connection when the client stops reading.

    ``resp.write`` has no deadline of its own. A client that stops reading without
    closing its socket would park the write, and with it the stream loop and the
    slot state that loop owns (the pending queue and the broadcast hold), for as
    long as the socket stays open. Past the deadline the transport is aborted and
    the write raises ``ConnectionResetError``, so the caller's existing disconnect
    handling releases that state.
    """
    try:
        await asyncio.wait_for(resp.write(data), timeout=_SSE_WRITE_DEADLINE_SECS)
    except asyncio.TimeoutError:
        transport = request.transport
        if transport is not None:
            transport.abort()
        logger.info(
            "SSE client stopped reading for %.0fs; connection aborted", _SSE_WRITE_DEADLINE_SECS
        )
        raise ConnectionResetError("SSE client stopped reading") from None
