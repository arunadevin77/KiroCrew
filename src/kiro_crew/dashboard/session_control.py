"""Session control: letting one chat session observe and interrupt another.

Four operations — create a session, stop its turn, close (archive) it, and read
its transcript — plus the authorization that decides whether a caller may address
a target at all. The operations are deliberately thin: they reuse the same
creation, stop, close and history paths the dashboard itself uses, so a controlled
session behaves exactly like one a human is typing into.

**Two verbs here write into another session's conversation: ``session_send`` and
``session_broadcast``.**
Reading returns a transcript tail; stopping cancels an in-flight turn the way the
Stop button does; closing archives the session the way the tab ✕ does (the
conversation is saved to history and can be reopened — closing is not deletion);
creating opens an empty session in the user's sidebar; sending
delivers a message the target runs as its next turn, redacted through
``sanitize_outbound`` and prefixed with a ``[sent by session … via <verb>]``
envelope so it can never render as something the person typed — and the verb is
named in it, so a worker can tell a message addressed to it alone from one every
sibling also received. Broadcasting is not a second write path: it resolves an
audience and hands each target to the same delivery, so every bound below holds
per target unchanged. An IDLE target runs
it under the authorization that admitted it; a BUSY target queues it, and the
generic drain re-asserts the target-side containment before the entry becomes a
turn: producers stamp the constraints that held at admission
(:func:`containment_meta`), and ``chat_runner``'s drain drops — with a visible
notice and an SEL record — any entry for which a constraint holds at delivery
that did not hold at admission. A dropped CROSS-SESSION delivery is reported back
to its sender too, since the target's own notice is on a transcript the sender
does not read: the entry also carries the sending session, as a slot key plus
that slot's tab identity (:func:`send_origin_meta`), and the drop appends a
notice there (:func:`notify_send_origin_dropped`). The two deliberate discards
of a queued entry, a person cancelling its queue card and a hard stop clearing
the queue, report back the same way with their own wording
(:func:`notify_send_origin_discarded`). A human-typed queued message
shares the same window and the same re-check, and carries no sender to report to.

Authorization is deny-by-default and checked in one place
(:func:`authorize_target`) for the three operations that take a target — stop,
close and read — so a guard cannot be present on one verb and missing on another.
``session_create`` has no target; it checks the caller's own eligibility with the
same refusals.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.context_assembly.markers import _apply_marker_spans, _marker_spans
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log.session_tree_projection import projection
from kiro_crew.dashboard.chat_delivery import sanitize_outbound
from kiro_crew.dashboard.chat_folders import _slot_meta_txn_lock, _unhide_folder, note_folder_filed
from kiro_crew.dashboard.chat_persistence import (
    _recent_session_slot_name,
    save_slot_off_loop,
)
from kiro_crew.dashboard.chat_utils import _history_key_for, slot_history_key
from kiro_crew.dashboard.create_rate_limit import SESSION_CREATE, allow_create
from kiro_crew.dashboard.relay_archive import (
    RELAY_ARCHIVE_CODE,
    RELAY_ARCHIVE_ERROR,
    is_relay_archive,
)
from kiro_crew.dashboard.state import (
    MAX_LIVE_SLOTS,
    MAX_SLOTS_PER_CREATOR,
    _normalize_slot_key,
    _safe_folder_tree,
)
from kiro_crew.metrics.sessions import END_REASON_REMOVED
from kiro_crew.metrics.turns import turn_outcome
from kiro_crew.security import redact, redact_and_truncate
from kiro_crew.sel import sel
from kiro_crew.session_summary import derive_state
from kiro_crew.validation import (
    BROADCAST_TARGET_ALLOWANCE_SECS,
    MAX_BROADCAST_TARGETS,
    MAX_LONG_STRING,
    MAX_SESSION_STATUS_ROWS,
    MAX_SESSION_STATUS_TITLE_CHARS,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

import importlib as _importlib
import sys as _sys

from kiro_crew import _facade_mirror
from kiro_crew.dashboard.session_control_parts.caller_access import (
    UNATTENDED_SLOT_PREFIXES,
    SessionControlError,
    _audit,
    _caller_is_ownership_fenced,
    _created_by_other,
    caller_slot_key,
    prewarm_enabled_check,
)
from kiro_crew.dashboard.session_control_parts.delivery_containment import (
    _has_channel_mirror,
    _probe_channel_mirror_for_key,
    containment_meta,
    containment_snapshot,
    newly_held_constraints,
    send_origin_meta,
)
from kiro_crew.dashboard.session_control_parts.model_switch import _pending_pick_view
from kiro_crew.dashboard.session_control_parts.session_status import (
    DEFAULT_READ_MESSAGES,
    MAX_PENDING_APPROVAL_TOOL_CHARS,
    MAX_READ_CONTENT_CHARS,
    MAX_READ_MESSAGES,
    MAX_SUMMARY_CHARS,
    MAX_SUMMARY_INTENTS,
    MAX_SUMMARY_ITEMS,
    MAX_SUMMARY_NOTES,
    TRANSIENT_ROLES,
)
from kiro_crew.dashboard.session_control_parts.session_tree import _slot_tree_parent
from kiro_crew.dashboard.session_control_parts.target_authorization import (
    _broadcast_resolution_slots,
    _check_caller_slot_fields,
    _deny_factory,
    _live_target_refusal,
    _not_creator_reason,
    _resolve_slot,
    authorize_target,
    load_target_window,
    refuse_caller_identity,
    refuse_caller_surface,
)

logger = logging.getLogger(__name__)


#: Cap on one delivered message. Aliased to ``validation.MAX_LONG_STRING`` rather
#: than restated as its own number: a seed prompt is that shape, the MCP schema
#: layer already rejects on that constant, and two spellings of one 50k limit
#: would drift apart the first time either moved.
def _fold_slot_key(key: str) -> str:
    """Fold a transcript stem or session key to the slot key it names.

    ``_normalize_slot_key`` strips ONE transport prefix per call, so a doubled
    ``dashboard:dashboard:member-x`` folds to ``dashboard_member-x``: a key the
    ``member-``/``cron-`` prefix guards do not match, whose history key is
    nevertheless the real ``dashboard:member-x`` transcript the resume core would
    publish. Fold to the fixed point so the guards test the key that is actually
    revived (or, for :func:`list_archived_sessions`, listed as revivable).
    """
    for _ in range(8):
        folded = _normalize_slot_key(key)
        if folded == key:
            return key
        key = folded
    return key


def _scan_archived_candidates(
    log: Any, key_candidate: str, wanted_title: str
) -> list[tuple[str, str, dict[str, Any]]]:
    """The DISK half of archived-target resolution: runs in a worker thread.

    Returns every on-disk dashboard session that *key_candidate* names or whose
    title equals *wanted_title*, as ``(slot_key, history_key, metadata)``. It
    reads the history store only -- metadata lines and the session catalog --
    and never touches the live slot table, which belongs to the event loop and
    is consulted by the caller after this returns.
    """
    found: dict[str, tuple[str, dict[str, Any]]] = {}
    _fold = _fold_slot_key

    def _consider(slot_key: str) -> None:
        if not slot_key or slot_key in found:
            return
        history_key = _history_key_for(slot_key)
        meta, readable = log.get_metadata_status(history_key)
        if readable and meta:
            found[slot_key] = (history_key, dict(meta))

    # Every candidate is taken from the CATALOG's spelling of the filename stem,
    # never echoed from the caller: on a case-insensitive filesystem (APFS, NTFS)
    # ``Chat-7`` opens ``dashboard_chat-7.jsonl`` just as well, so a metadata
    # probe on the caller's spelling would succeed while the live-slot dedup
    # (exact ``_slots`` lookup) and the resume core's own live check (exact
    # session-key compare) both miss the ``chat-7`` that is already open -- and
    # a second slot would be published over the same transcript file. Resolving
    # through the catalog keys ``found`` by the stem the store reports, so the
    # key handed back is the one the live table and the resume compare against.
    rows = [
        row for row in log.list_sessions() if str(row.get("key") or "").startswith("dashboard_")
    ]
    if key_candidate:
        wanted_stem = _fold(key_candidate)
        if not wanted_stem.startswith("dashboard_"):
            wanted_stem = f"dashboard_{wanted_stem}"
        for row in rows:
            stem = str(row.get("key") or "")
            if stem.casefold() == wanted_stem.casefold():
                _consider(_fold(stem))
    if wanted_title:
        for row in rows:
            if (str(row.get("title") or "")).strip().casefold() == wanted_title:
                _consider(_fold(str(row.get("key") or "")))
    return [(k, hk, meta) for k, (hk, meta) in found.items()]


async def _resolve_archived_target(
    state: "DashboardState",
    log: Any,
    target: str,
    deny: "Callable[..., SessionControlError]",
    live_refusal: "Callable[[str], Awaitable[SessionControlError]]",
) -> tuple[str, str, dict[str, Any]]:
    """Find the ARCHIVED dashboard session *target* names.

    Accepts what a caller actually holds -- a slot key (``chat-7-...``), the
    ``dashboard:<slot>`` session key, the ``dashboard_<slot>`` filename stem
    ``list_sessions`` reports, or an exact case-insensitive title -- and returns
    ``(slot_key, history_key, metadata)`` for exactly one on-disk session that
    has no live slot. Mirrors :func:`_resolve_slot`'s doctrine: every form is
    resolved before anything is returned, and two DIFFERENT sessions matching
    across forms is refused as ambiguous rather than silently preferring one.

    The disk scan (metadata lines, the session catalog) runs off the loop in
    :func:`_scan_archived_candidates`; the live-slot table is read here, on the
    loop, both before (a live match is refused with its key) and after the scan
    (a candidate that went live during it is dropped rather than revived twice).

    A target that IS live is handed to *live_refusal* with its live key, and
    that builder decides what the caller learns: :func:`revive_session` authorizes
    the live slot as any live target first, so a protected session answers with
    that refusal and reveals neither its existence nor its key, while one the
    caller may reach is named so the caller can just address it.
    """
    # Circular at runtime: ``members`` imports this module.
    from kiro_crew import members as members_mod

    try:
        live = _resolve_slot(state, target)
    except SessionControlError as exc:
        raise deny(exc.message, exc.code, status=exc.status) from exc
    if live is not None:
        raise await live_refusal(live.key)

    # The key forms fold to one slot key; a bare title never does (it is not a
    # dashboard prefix and ``_normalize_slot_key`` would mangle spaces), so only
    # a target that looks like a key is tried as one.
    stripped = target.strip()
    key_candidate = stripped if stripped and not any(ch.isspace() for ch in stripped) else ""
    candidates = await asyncio.to_thread(
        _scan_archived_candidates, log, key_candidate, stripped.casefold()
    )
    found = [(k, hk, meta) for k, hk, meta in candidates if state.get_slot(k) is None]
    if candidates and not found:
        # Every on-disk match went live during the scan (a human resume click
        # racing this call): answer as the pre-scan probe would have -- through
        # the same authorize-then-name builder -- rather than "not found" for a
        # session the caller can see.
        raise await live_refusal(candidates[0][0])

    if len(found) > 1:
        raise deny(
            f"{len(found)} archived sessions match {target!r} (as a session key, "
            "transcript name, or title) -- address it by its session key instead",
            "ambiguous_target",
            status=409,
        )
    if not found:
        raise deny(f"no archived session matches {target!r}", "target_not_found", status=404)
    slot_key, history_key, meta = found[0]
    if slot_key.casefold().startswith(members_mod.DM_SLOT_KEY_PREFIX):
        # A crew member's DM thread is opened only through its own roster route,
        # which re-checks the member binding; session control has no business
        # resurrecting one under a caller's name.
        raise deny("member threads are not addressable", "member_thread_target")
    return slot_key, history_key, meta


def _archived_session_is_mirrored(state: "DashboardState", history_key: str) -> bool:
    """:func:`_has_channel_mirror` for a session that has no live slot yet.

    Fail-closed like the refusal paths: a store that cannot answer counts as
    mirrored, so an unreadable link never opens the boundary.
    """
    probed = _probe_channel_mirror_for_key(state, history_key)
    return True if probed is None else bool(probed)


def _archived_session_is_channel_linked(state: "DashboardState", history_key: str) -> bool:
    """Whether the GATEWAY's session store records a channel link for an archived
    session -- the inbound origin (``get_origin_link``, the conversation the
    channel dispatcher recorded as this session's own) or the Slack thread binding
    (``get_slack_link``).

    The metadata line's ``linked_session_key`` / ``channel_origin`` say the same
    thing, but that line is a file an agent's tools can edit; the session store is
    gateway-owned, so this is the corroborating read the channel-link boundary
    rests on for a revive. Fail-closed: a store that cannot answer counts as
    linked. A state with no session store answers "not linked", the same posture
    as the mirror probe, since there is no record to consult.
    """
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return False
    try:
        get_origin = getattr(sessions, "get_origin_link", None)
        if get_origin is not None and get_origin(history_key):
            return True
        get_slack = getattr(sessions, "get_slack_link", None)
        if get_slack is not None:
            thread_ts, _channel = get_slack(history_key)
            if thread_ts:
                return True
    except Exception:
        logger.debug("channel-link probe failed", exc_info=True)
        return True
    return False


def _archived_link_and_mirror(state: "DashboardState", history_key: str) -> tuple[bool, bool]:
    """``(channel_linked, mirrored)`` for an archived session -- the WORKER-THREAD half.

    Both probes read the gateway's session store through its guarded getters,
    and the store's off-loop writer holds that same lock across a temp-file write
    and rename, so a probe on the event loop can stall every session behind one
    disk write. :func:`revive_session` therefore runs the pair through
    ``asyncio.to_thread``, the way :func:`_scan_archived_candidates` already runs
    the catalog scan. Each half keeps its own fail-closed reading.
    """
    return (
        _archived_session_is_channel_linked(state, history_key),
        _archived_session_is_mirrored(state, history_key),
    )


def _revived_slot_refusal(slot: "_ChatSlot", caller_slot: "_ChatSlot") -> tuple[str, str] | None:
    """The target-side refusal, if any, that the HYDRATED slot now earns.

    :func:`revive_session` answers the target-side boundaries from the archived
    metadata line before the resume, but the resume core re-reads that line after
    its threaded transcript read and hydrates the slot from the fresh copy. The
    same fields are therefore read back off the live slot -- the reads
    :func:`authorize_target` makes on a live target, same order, same wording and
    codes -- so a line rewritten inside the resume's window cannot publish a
    slot the pre-resume answer would have refused. Returns ``(reason, code)`` or
    ``None`` when every boundary still holds.
    """
    if (refusal := _live_target_refusal(slot)) is not None:
        return refusal
    if (getattr(slot, "workspace", "default") or "default") != (
        getattr(caller_slot, "workspace", "default") or "default"
    ):
        return ("target session belongs to a different workspace", "workspace_mismatch")
    return None


def _archived_target_refusal(
    slot_key: str,
    meta: "Mapping[str, Any]",
    *,
    caller_key: str,
    caller_workspace: str,
    ownership_fenced: bool,
    linked: bool,
    mirrored: bool,
) -> tuple[str, str] | None:
    """Why :func:`revive_session` refuses archived *slot_key* for this caller, or None.

    The ONE statement of revive's target-side refusals, as ``(reason, code)``:
    :func:`revive_session` raises the answer and :func:`_scan_revivable_history`
    skips any row it refuses, so a refusal added here is enforced by the revive
    and hidden from the listing at once. *linked* and *mirrored* are the caller's
    session-store reading (:func:`_archived_link_and_mirror`); passing False for
    both yields the metadata-only refusals, a subset of the full answer, which is
    how the scan avoids a store probe for a row the metadata already refuses.
    The ``not_creator`` reason is generic; revive substitutes its caller-specific
    wording.
    """
    if slot_key.casefold().startswith(UNATTENDED_SLOT_PREFIXES):
        return "unattended sessions (scheduled runs) cannot be controlled", "unattended_target"
    if str(meta.get("memory_mode") or "persistent") != "persistent":
        return "incognito and temporary sessions are not addressable", "ephemeral_target"
    if meta.get("app"):
        return "app-scoped sessions are not addressable", "app_scoped_target"
    if meta.get("linked_session_key") or meta.get("channel_origin") or linked:
        # Both readings: the metadata line AND the gateway-owned session store,
        # so a channel link scrubbed from the editable line is still seen.
        return "channel-linked sessions are not addressable", "linked_session_target"
    if mirrored:
        # The outbound-mirror boundary ``_has_channel_mirror`` holds for a live
        # slot, read on the history key because the link lives in the session
        # store, not in the transcript: an archived dashboard session can still
        # be republishing to a channel the moment it is reopened. Unknown reads
        # as mirrored, the refusal paths' fail-closed direction.
        return "sessions mirrored to a channel are not addressable", "mirrored_target"
    if str(meta.get("workspace") or "default") != caller_workspace:
        return "target session belongs to a different workspace", "workspace_mismatch"
    if ownership_fenced:
        if str(meta.get("created_by") or "") != caller_key:
            return "target session was created by another session", "not_creator"
        # The metadata line is a file an agent's file tools can edit
        # (``chat_persistence`` says so where it restores ``created_by``), so for a
        # FENCED caller the claim above is only a claim: a caller that rewrote
        # ``created_by`` in an archived transcript would otherwise revive a session
        # it never made and then hold it. The ownership is therefore corroborated
        # against the one record those tools cannot reach -- the crew log's
        # session-tree lineage, written by this gateway at the child's first turn
        # from the in-process ``_lineage_minted`` witness and never from disk
        # metadata. Fail closed when that record is not readable (crew log off,
        # projection unseeded or incomplete) or names a different parent: an
        # unverifiable ownership is refused, not assumed. A caller that is NOT
        # fenced (the person's own tab) is not gated on ownership at all, so the
        # human recovery this tool exists for is unaffected.
        tree_known, lineage_parent, _ = _slot_tree_parent(slot_key)
        if not tree_known or lineage_parent != caller_key:
            return (
                "archived session ownership could not be verified from gateway records "
                "(a fenced caller needs the crew log's session-tree lineage, which is "
                "missing when the log is off, its projection is unseeded, or the "
                "session predates it)",
                "ownership_unverified",
            )
    return None


async def revive_session(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    folder_id: str = "",
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """Bring the ARCHIVED session *target* back into the live sidebar.

    The mirror of :func:`close_target`: a close archives a tab to history; this
    pulls one history session back up into a live slot, the same thing the
    History tab's resume click does. It reuses the dashboard's own resume core
    (:func:`chat_handlers.resume_slot_from_history`), so a controlled revive
    and a human click share one materialisation, one set of member-pin and
    delete/recreate barriers and one set of refusal codes.

    Authorization is :func:`authorize_target`'s, applied to the session's
    PERSISTED metadata because there is no live slot yet to read: the same
    caller-side refusals (via the shared helpers), then the target-side ones --
    unattended, ephemeral, app-scoped, channel-linked, a different workspace --
    read from the metadata line, and the same ownership fence: an ownership-
    fenced caller (an agent-created session, a crew member, a scheduled run)
    may revive only a session whose ``created_by`` is itself AND whose crew-log
    lineage names it as parent -- the metadata line is agent-editable, the
    lineage record is not, so the fence fails closed when the record cannot be
    read.

    ``folder_id`` files the revived slot the way ``create_session`` does, after
    the revive has landed; a folder that does not exist refuses the whole call
    BEFORE anything is revived, so a refusal leaves history untouched. The
    revived slot keeps its own creator: reviving is not creating, so ownership
    is never transferred to the caller.
    """
    # Circular at runtime: ``chat_handlers`` imports this module.
    from kiro_crew.dashboard.chat_handlers import ResumeRefusal, resume_slot_from_history

    try:
        await asyncio.to_thread(sel)
    except Exception:  # noqa: BLE001 - a prewarm failure must not fail the revive
        logger.warning("session-control SEL prewarm failed", exc_info=True)
    await prewarm_enabled_check()

    deny = _deny_factory(caller_session_key=caller_session_key, operation="revive", target=target)

    caller_key = refuse_caller_identity(
        state, caller_session_key=caller_session_key, deny=deny, skip_enabled_check=False
    )

    log = state.conversation_log
    if log is None:
        raise deny("session history is unavailable", "history_unavailable")

    # The whole caller side BEFORE the target is resolved: a caller refused for
    # its own identity or containment must learn nothing from the attempt, and
    # resolving first would make the refusal an existence oracle (404 for an
    # absent archived session, 403 for a present one). Off the loop because the
    # caller-side set ends in the session-store mirror probe, whose guarded
    # getter shares the lock the store's writer holds across its disk write.
    caller_slot = await asyncio.to_thread(
        refuse_caller_surface, state, caller_key=caller_key, deny=deny
    )
    # The fence verdict, resolved ONCE and before any target is named: the
    # live-target builder below needs it on the loop, and evaluating it inline
    # there would be a config read (disk on a cache miss) in a synchronous span.
    ownership_fenced = (
        caller_fenced
        if caller_fenced is not None
        else await asyncio.to_thread(_caller_is_ownership_fenced, state, caller_key)
    )

    async def _live_refusal(live_key: str) -> SessionControlError:
        # A target that turns out to be LIVE (before the scan, during it, or
        # by the time the resume ran) is a live slot hydrated from a line this
        # call never checked, so it is authorized exactly as a live target is
        # before anything about it is said: a slot the caller may not reach
        # answers with THAT refusal and reveals neither its existence nor its
        # key; one it may reach is named so the caller can address it.
        #
        # Split the way the resume hook and ``final_check`` are split. The
        # checks answerable from live slot fields run ON the loop, so nothing
        # can mutate the slot between the read and the answer; the two
        # session-store probes (the caller's owner-DM exemption and the target's
        # outbound mirror) go to a worker, because their guarded getters share
        # the lock the store's writer holds across its disk write; and the
        # store-free set runs once more on the loop after those awaits, right
        # before the key is named -- a workspace commit or a link rebind landing
        # inside the worker hop would otherwise be answered from stale fields.
        def _store_free() -> "_ChatSlot":
            live = state.get_slot(live_key)
            if live is None:
                raise deny(f"no open session matches {target!r}", "target_not_found", status=404)
            if live.key == caller_key:
                raise deny("a session cannot control itself", "self_target")
            live_caller = _check_caller_slot_fields(state, caller_key, deny)
            if (refusal := _revived_slot_refusal(live, live_caller)) is not None:
                raise deny(refusal[0], refusal[1])
            if ownership_fenced and _created_by_other(live, caller_key):
                raise deny(
                    _not_creator_reason(state, caller_key, live_caller, caller_fenced),
                    "not_creator",
                )
            return live

        live = _store_free()
        await asyncio.to_thread(refuse_caller_surface, state, caller_key=caller_key, deny=deny)
        if await asyncio.to_thread(_has_channel_mirror, state, live):
            raise deny("sessions mirrored to a channel are not addressable", "mirrored_target")
        _store_free()
        return deny(
            f"{target!r} is already open as `{live_key}`; address it directly",
            "target_already_live",
            status=409,
        )

    slot_key, history_key, meta = await _resolve_archived_target(
        state, log, target, deny, _live_refusal
    )

    # Target-side containment, read from the metadata line the resume would
    # rehydrate the slot from. The session-store probes (inbound link, Slack
    # thread binding, outbound mirror) go through the store's guarded getters,
    # whose lock an off-loop writer holds across its disk write, so they run in
    # a worker thread like the catalog scan does rather than on the loop.
    linked, mirrored = await asyncio.to_thread(_archived_link_and_mirror, state, history_key)
    refusal = _archived_target_refusal(
        slot_key,
        meta,
        caller_key=caller_key,
        caller_workspace=str(getattr(caller_slot, "workspace", "default") or "default"),
        ownership_fenced=ownership_fenced,
        linked=linked,
        mirrored=mirrored,
    )
    if refusal is not None:
        reason, code = refusal
        if code == "not_creator":
            reason = _not_creator_reason(state, caller_key, caller_slot, caller_fenced)
        raise deny(reason, code)

    if folder_id:

        def _exists(folders: list[dict[str, Any]]) -> bool:
            return any(str(f.get("id") or "") == folder_id for f in _safe_folder_tree(folders))

        if not await state.read_folders(_exists):
            raise deny("folder not found", "folder_not_found", status=400)

    # A revive materialises the same resource a create does -- a live slot with
    # a hydrated transcript -- so it spends the same budgets: the per-caller
    # creation window and the per-creator and global slot caps.
    if not allow_create(SESSION_CREATE, caller_key):
        raise deny(
            "too many sessions opened recently; retry shortly", "create_rate_limited", status=429
        )
    if state.live_slot_count() >= MAX_LIVE_SLOTS:
        raise deny(f"slot cap reached ({MAX_LIVE_SLOTS})", "slot_cap_reached", status=429)
    if state.creator_slot_count(caller_key) >= MAX_SLOTS_PER_CREATOR:
        raise deny(
            f"per-caller slot cap reached ({MAX_SLOTS_PER_CREATOR})",
            "creator_slot_cap_reached",
            status=429,
        )

    def _plain(reason: str, code: str, status: int = 403) -> SessionControlError:
        # A non-auditing denier for the hook: its refusal is audited ONCE, by the
        # ``deny`` that raises it after the resume returns.
        return SessionControlError(reason, status=status, code=code)

    async def _containment(built: "_ChatSlot") -> ResumeRefusal | None:
        # The LAST gate, on the slot the resume hydrated and before it is
        # published (the core holds it retracted and under construction while
        # this awaits). Every answer read before the resume came from a metadata
        # snapshot taken before the transcript read and from a caller slot that
        # can change at any moment, so each is re-asserted here on live state:
        # the caller's own eligibility, the four target-side fields off the built
        # slot, the workspace, the gateway store's link and mirror records for the
        # target, the mirror on the built slot itself, and the two ceilings. A
        # refusal discards the built slot inside the core; nothing was published.
        try:
            live_caller = await asyncio.to_thread(
                refuse_caller_surface, state, caller_key=caller_key, deny=_plain
            )
        except SessionControlError as exc:
            return ResumeRefusal(exc.message, exc.code, exc.status)
        refusal = _revived_slot_refusal(built, live_caller)
        if refusal is not None:
            return ResumeRefusal(refusal[0], refusal[1], 403)
        linked, mirrored = await asyncio.to_thread(_archived_link_and_mirror, state, history_key)
        if linked:
            return ResumeRefusal(
                "channel-linked sessions are not addressable", "linked_session_target", 403
            )
        if mirrored or await asyncio.to_thread(_has_channel_mirror, state, built):
            return ResumeRefusal(
                "sessions mirrored to a channel are not addressable", "mirrored_target", 403
            )
        claimed_creator = str(meta.get("created_by") or "")
        if not getattr(built, "_created_by", "") and claimed_creator:
            # Creator attribution restored the way a restart's rehydrate restores
            # it (see ``_rehydrate_slot_from_history``): the ownership fence reads
            # ``_created_by``, and a fenced caller that revived its own worker
            # must still own it, or its next ``session_send`` is refused
            # ``not_creator``. Restored ONLY when the gateway-authored lineage
            # corroborates the claim: ``created_by`` is read from an agent-editable
            # file, and a stamp taken on the line's word alone would let a fenced
            # agent that wrote its own key into an archived transcript be handed
            # session-control reach over the operator's conversation the moment
            # an unfenced tab revived it (the fenced branch above checks the
            # lineage only for a fenced REVIVER, not for the claimed creator). An
            # unknown tree or a different parent leaves the field blank, which
            # matches no caller. Attribution ONLY -- ``_lineage_minted`` stays
            # False, and the reviver is never written here: reviving is not
            # creating.
            tree_known, lineage_parent, _ = _slot_tree_parent(slot_key)
            if tree_known and lineage_parent == claimed_creator:
                built._created_by = claimed_creator
        # Charge the reviver for this slot under the per-caller cap (see
        # ``creator_slot_count``) without touching its ownership. Set before the
        # ceilings are read so the count includes this slot once published.
        built._revived_by = caller_key
        # The ceilings. ``live_slot_count`` counts allocated-but-unpublished slots
        # too, so the built slot IS in it (retracted from the table, still under
        # construction) and the compare is strictly-over, the same exclusive
        # ceiling every other allocation path applies; ``creator_slot_count``
        # walks the table only, so the built slot is not in it and the compare
        # is inclusive.
        if state.live_slot_count() > MAX_LIVE_SLOTS:
            return ResumeRefusal(f"slot cap reached ({MAX_LIVE_SLOTS})", "slot_cap_reached", 429)
        if state.creator_slot_count(caller_key) >= MAX_SLOTS_PER_CREATOR:
            return ResumeRefusal(
                f"per-caller slot cap reached ({MAX_SLOTS_PER_CREATOR})",
                "creator_slot_cap_reached",
                429,
            )
        return None

    def _final_check(built: "_ChatSlot") -> ResumeRefusal | None:
        # SYNCHRONOUS last word, run by the core after its last await and right
        # before the publish: everything the hook answered without a store read
        # is re-asserted here on live state, so nothing can run between these
        # reads and the slot becoming reachable. The store-backed probes (link,
        # mirror) are the hook's; their guarded getters may not run on the loop.
        try:
            live_caller = _check_caller_slot_fields(state, caller_key, _plain)
        except SessionControlError as exc:
            return ResumeRefusal(exc.message, exc.code, exc.status)
        refusal = _revived_slot_refusal(built, live_caller)
        if refusal is not None:
            return ResumeRefusal(refusal[0], refusal[1], 403)
        if state.live_slot_count() > MAX_LIVE_SLOTS:
            return ResumeRefusal(f"slot cap reached ({MAX_LIVE_SLOTS})", "slot_cap_reached", 429)
        if state.creator_slot_count(caller_key) >= MAX_SLOTS_PER_CREATOR:
            return ResumeRefusal(
                f"per-caller slot cap reached ({MAX_SLOTS_PER_CREATOR})",
                "creator_slot_cap_reached",
                429,
            )
        return None

    outcome = await resume_slot_from_history(
        state,
        name=slot_key,
        history_key=history_key,
        caller_label=f"session:{caller_key}",
        containment=_containment,
        final_check=_final_check,
    )
    if outcome.refusal is not None:
        raise deny(outcome.refusal.error, outcome.refusal.code, status=outcome.refusal.status)
    slot = outcome.slot
    assert slot is not None

    if outcome.already_live:
        # Lost a race with a human click or another reviver between the
        # resolution above and the resume: same authorize-then-name answer as
        # the two pre-resume live branches, through the one builder.
        raise await _live_refusal(slot.key)

    def _audit_allowed(filed_now: bool) -> None:
        # The revive is committed the moment the core published the slot, so
        # the allowed record is owed whatever happens to the filing after it:
        # a cancelled request that skipped this row would leave a durable revive
        # with no SEL record, and audit rows are not reconstructible.
        _audit(
            caller_session_key=caller_key,
            operation="revive",
            slot_key=slot.key,
            outcome="allowed",
            detail={
                "messages": str(outcome.total),
                "folder_id": slot.folder_id or "",
                "filed": "true" if filed_now else "false",
                "was_closed": "true" if meta.get("closed") else "false",
            },
        )

    filed = False
    if folder_id and folder_id != slot.folder_id:
        # The revive is already COMMITTED -- the slot is live in the sidebar --
        # so a failure to file it must not propagate as "could not revive": the
        # caller would retry and be told the target is live. Same posture as
        # ``create_session``'s post-commit folder un-hide.
        #
        # Under the state-wide slot-metadata txn lock, the span the folder
        # endpoint (``api_chat_slot_folder``) and the sidebar filing writers
        # serialize their own mutate/save/rollback under: the un-hide and the
        # save below are awaits on an already-published slot, so a folder PATCH
        # could commit inside them and an unconditional ``previous`` restore
        # would then erase an acknowledged placement. Under the lock a rollback
        # can only undo this call's own write; the value compare stays as the
        # defense against the non-endpoint writers that do not take the lock.
        # The lock acquisition itself is an await (``LoopBoundLock`` under
        # contention), so the cancellation guard sits OUTSIDE ``async with``: a
        # cancellation delivered while waiting for the lock has written nothing
        # to roll back but must still leave the allowed audit row for a revive
        # that has already committed. The audit is emitted exactly once: here on
        # the exceptional path, or by the ``_audit_allowed(filed)`` call after
        # the block on the normal one (an ``Exception`` inside is swallowed into
        # the normal path, so it does not reach this arm).
        try:
            async with _slot_meta_txn_lock(state):
                previous = slot.folder_id
                previous_changed = slot._folder_changed
                # Re-read under the lock: filed there meanwhile by another writer
                # means nothing to do, and nothing this call may later roll back.
                if folder_id != previous:
                    slot.folder_id = folder_id
                    slot._folder_changed = True

                    def _roll_back() -> None:
                        if slot.folder_id == folder_id:
                            slot.folder_id = previous
                            slot._folder_changed = previous_changed

                    try:
                        if not await _unhide_folder(state, folder_id):
                            _roll_back()
                        elif await save_slot_off_loop(
                            state,
                            slot,
                            force=True,
                            # Strict, not best-effort: the default swallows a raised
                            # save and answers True (marking the slot dirty for the
                            # periodic flush), which would report ``filed: true`` for
                            # a placement that is not on disk and is lost if the
                            # gateway restarts before that flush. A raise takes the
                            # rollback arm below and reports ``filed: false``.
                            best_effort=False,
                            expected_history_key=slot_history_key(slot),
                        ):
                            note_folder_filed(state, folder_id)
                            filed = True
                        else:
                            _roll_back()
                            slot._dirty = True
                    except Exception:
                        logger.warning(
                            "revive_session: %s revived but filing it into %s failed",
                            slot.key,
                            folder_id,
                            exc_info=True,
                        )
                        _roll_back()
                        slot._dirty = True
                    except BaseException:
                        # A cancellation (gateway shutdown, the MCP client's timeout
                        # closing the socket) inside the two awaits above is not an
                        # ``Exception``: without this arm it would skip the rollback,
                        # leaving the provisional placement live and dirty. Undo this
                        # call's write and let it propagate to the audit arm below.
                        _roll_back()
                        slot._dirty = True
                        state.push_slots_update()
                        raise
        except BaseException:
            _audit_allowed(False)
            raise
        state.push_slots_update()

    _audit_allowed(filed)
    return {
        "ok": True,
        "target": slot.key,
        "title": slot.title or slot.key,
        "messages": outcome.total,
        "folder_id": slot.folder_id or "",
        "filed": filed,
    }


#: Default and ceiling on the rows one ``session_history_list`` call returns. The
#: MCP schema (``SESSION_HISTORY_LIST_SCHEMA``) enforces the same ceiling; the
#: backend clamps too, because the route is reachable without the schema layer.
DEFAULT_HISTORY_LIST_ROWS = 20
MAX_HISTORY_LIST_ROWS = 100
#: How many history catalog entries one scan walks, newest first. It bounds the
#: catalog read (at most this many stems and mtimes) and the ``seen`` set. The
#: walk normally ends earlier, at the first eligible row past ``limit``; when it
#: runs out of entries first, the answer says ``scan_truncated``, because older
#: sessions were never examined. Revivable sessions sit near the top of a
#: newest-first catalog, so a real archive meets the row cap long before this.
MAX_HISTORY_SCAN_ENTRIES = 5000

#: Longest ``folder_id`` a history row carries. Folder ids are 12 hex characters
#: (``chat_folders``), but the value is read from agent-writable transcript
#: metadata, so a longer one is reported as unfiled instead of being retained. It
#: is dropped rather than cut short because a truncated id names a different
#: folder, or none.
MAX_HISTORY_FOLDER_ID_CHARS = 64


def _scan_revivable_history(
    state: "DashboardState",
    log: Any,
    *,
    caller_key: str,
    caller_workspace: str,
    ownership_fenced: bool,
    folder_id: str,
    live_keys: frozenset[str],
    limit: int,
) -> tuple[list[dict[str, Any]], bool, bool]:
    """The DISK half of :func:`list_archived_sessions`: runs in a worker thread.

    Walks the newest ``MAX_HISTORY_SCAN_ENTRIES`` transcripts of the history
    catalog (``newest_session_stems``, stems and mtimes only) newest first and
    keeps each archived dashboard session :func:`revive_session` would accept
    for this caller. The target-side refusals are revive's own
    (:func:`_archived_target_refusal`), asked first on the metadata alone and
    then, for a row that passes, with the gateway session-store link and
    outbound-mirror probe; member DM keys and the *folder_id* filter are the
    listing's own. A row that would be refused is skipped, not reported, so the
    listing never names a session the caller could not revive.

    *live_keys* is the live-slot snapshot the caller took on the loop; the slot
    table itself belongs to the loop and is not read here. Returns
    ``(rows, lineage_unknown, truncated)``. *rows* holds up to ``limit + 1``
    eligible rows: the walk stops at the first one past *limit*, which only
    says more exist, so no metadata read or store probe is spent beyond it.
    *truncated* is set when the walk ran out of its ``MAX_HISTORY_SCAN_ENTRIES``
    budget before that. *lineage_unknown* is set when a fenced caller had
    candidates the lineage could not vouch for (crew log off, unseeded,
    incomplete).
    """
    # Circular at runtime: ``members`` imports this module.
    from kiro_crew import members as members_mod

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    lineage_unknown = False
    caller_facts: dict[str, Any] = {
        "caller_key": caller_key,
        "caller_workspace": caller_workspace,
        "ownership_fenced": ownership_fenced,
    }
    # A bounded catalog read: only the newest ``MAX_HISTORY_SCAN_ENTRIES``
    # transcripts' stems and mtimes, with no title or metadata, so nothing the
    # walk starts from grows with the archive or with a persisted field.
    entries, truncated = log.newest_session_stems(MAX_HISTORY_SCAN_ENTRIES)
    for stem, modified in entries:
        if not stem.startswith("dashboard_"):
            continue
        slot_key = _fold_slot_key(stem)
        if not slot_key or slot_key in seen or slot_key in live_keys:
            continue
        seen.add(slot_key)
        folded = slot_key.casefold()
        if folded.startswith(UNATTENDED_SLOT_PREFIXES) or folded.startswith(
            members_mod.DM_SLOT_KEY_PREFIX
        ):
            continue
        history_key = _history_key_for(slot_key)
        meta, readable = log.get_metadata_status(history_key)
        if not readable or not meta:
            continue
        if folder_id and str(meta.get("folder_id") or "") != folder_id:
            continue

        # Metadata first: the refusals it alone yields are a subset of the full
        # answer, so a row they refuse needs no session-store probe.
        refusal = _archived_target_refusal(
            slot_key, meta, linked=False, mirrored=False, **caller_facts
        )
        if refusal is None:
            linked, mirrored = _archived_link_and_mirror(state, history_key)
            refusal = _archived_target_refusal(
                slot_key, meta, linked=linked, mirrored=mirrored, **caller_facts
            )
        if refusal is not None:
            if refusal[1] == "ownership_unverified" and not _slot_tree_parent(slot_key)[0]:
                lineage_unknown = True
            continue
        last_active = datetime.fromtimestamp(modified, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        rows.append(
            {
                "target": slot_key,
                "title": _bounded_status_title(str(meta.get("title") or slot_key)),
                "last_active": last_active,
                "folder_id": _bounded_folder_id(meta.get("folder_id")),
            }
        )
        if len(rows) > limit:
            # One past the cap answers "more exist"; nothing past it is read.
            return rows, lineage_unknown, False
    return rows, lineage_unknown, truncated


async def list_archived_sessions(
    state: "DashboardState",
    *,
    caller_session_key: str,
    folder_id: str = "",
    limit: int = DEFAULT_HISTORY_LIST_ROWS,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """The archived sessions this caller could :func:`revive_session`, newest first.

    READ-ONLY. The finding half of a revive: ``session_revive`` needs a key or an
    exact title, and nothing else on this surface lists archived sessions. The
    listing is filtered by the rules revive enforces (see
    :func:`_scan_revivable_history`), so every row is a target the caller may
    pass on. The caller side runs first and in full, the same way revive runs
    it before resolving any target, so a refused caller learns nothing about
    what is on disk.

    ``folder_id`` narrows to sessions filed in that folder; an id that is not in
    the tree is refused ``folder_not_found`` rather than answered with an empty
    list that looks like "nothing archived there".
    """
    try:
        await asyncio.to_thread(sel)
    except Exception:  # noqa: BLE001 - a prewarm failure must not fail the read
        logger.warning("session-control SEL prewarm failed", exc_info=True)
    await prewarm_enabled_check()

    deny = _deny_factory(caller_session_key=caller_session_key, operation="history_list", target="")
    caller_key = refuse_caller_identity(
        state, caller_session_key=caller_session_key, deny=deny, skip_enabled_check=False
    )
    log = state.conversation_log
    if log is None:
        raise deny("session history is unavailable", "history_unavailable")
    caller_slot = await asyncio.to_thread(
        refuse_caller_surface, state, caller_key=caller_key, deny=deny
    )
    ownership_fenced = (
        caller_fenced
        if caller_fenced is not None
        else await asyncio.to_thread(_caller_is_ownership_fenced, state, caller_key)
    )
    limit = max(1, min(int(limit), MAX_HISTORY_LIST_ROWS))

    if folder_id:

        def _exists(folders: list[dict[str, Any]]) -> bool:
            return any(str(f.get("id") or "") == folder_id for f in _safe_folder_tree(folders))

        if not await state.read_folders(_exists):
            raise deny("folder not found", "folder_not_found", status=400)

    caller_workspace = str(getattr(caller_slot, "workspace", "default") or "default")
    live_keys = frozenset(slot.key for slot in list(state._slots.values()))
    rows, lineage_unknown, truncated = await asyncio.to_thread(
        _scan_revivable_history,
        state,
        log,
        caller_key=caller_key,
        caller_workspace=caller_workspace,
        ownership_fenced=ownership_fenced,
        folder_id=folder_id,
        live_keys=live_keys,
        limit=limit,
    )

    # The rows carry TITLES of the person's closed sessions. The caller gate ran
    # before the scan, but a channel mirror can be bound onto the calling slot
    # while the scan is on a worker thread, so it is re-asserted before anything
    # is returned -- the reason ``created_session_status`` re-checks too. The
    # workspace is compared because every row was filtered on it. Both reads
    # touch the session store, so they run off the event loop like the first pass.
    caller_slot_now = await asyncio.to_thread(
        refuse_caller_surface, state, caller_key=caller_key, deny=deny
    )
    if str(getattr(caller_slot_now, "workspace", "default") or "default") != caller_workspace:
        raise deny(
            "the calling session moved workspace while history was being read; call again",
            "caller_changed_mid_read",
        )
    # The ownership fence can tighten during the scan too (a channel-DM link bound
    # onto the caller). Rows filtered under the looser rule would then leak other
    # people's archived titles, so a tightened fence withholds the whole answer.
    if caller_fenced is None and not ownership_fenced:
        if await asyncio.to_thread(_caller_is_ownership_fenced, state, caller_key):
            raise deny(
                "the calling session's ownership rules changed while history was being read; "
                "call again",
                "caller_changed_mid_read",
            )
    # A row revived by someone else during the scan is live now; drop it here,
    # where the slot table can be read, so the list stays "archived only".
    # ``more`` is read off the scan, before that drop: a sentinel row revived
    # meanwhile still proves the walk stopped early, so older rows were unread.
    more = len(rows) > limit or truncated
    rows = [row for row in rows if state.get_slot(row["target"]) is None][:limit]

    _audit(
        caller_session_key=caller_session_key,
        operation="history_list",
        slot_key=caller_key,
        outcome="allowed",
        detail={
            "rows": len(rows),
            "more": more,
            "truncated": truncated,
            "folder": bool(folder_id),
        },
    )
    return {
        "ok": True,
        "sessions": rows,
        # More revivable sessions may exist past this page: the walk found one
        # past ``limit``, or it ran out of catalog entries before finishing.
        "more": more,
        # The walk stopped at ``MAX_HISTORY_SCAN_ENTRIES`` catalog entries, so
        # older sessions were not examined at all.
        "scan_truncated": truncated,
        # Only a fenced caller's rows rest on the lineage; for anyone else this
        # is always False.
        "lineage_unknown": lineage_unknown,
    }


MAX_SEND_MESSAGE_CHARS = MAX_LONG_STRING

#: Provenance prefix on every delivered message. The target's transcript renders
#: the message as a user row, and without this line it is indistinguishable from
#: something the person typed — the same reason auto-nudge tags its injected
#: turns ``[auto-nudge cycle N]``. The model in the target session sees it too,
#: so it can weigh the instruction as coming from a peer session, not its user.
_SEND_PROVENANCE = "[sent by session {caller} via {via}]\n\n"

#: A provenance-shaped opener inside a caller's body. The envelope tells the
#: target which peer sent the message, and the body is caller-controlled, so a
#: body line of the same shape must not read as a second envelope.
#:
#: Matched on a normalized view (see ``context_assembly.markers._marker_spans``:
#: NFKC, every ``Cf`` char dropped, dashes folded) rather than by listing
#: spellings, so bidi controls, zero-width chars, soft hyphens and compatibility
#: forms all fold away before the match. Any ``Ps`` opening bracket counts; every
#: ``Ps`` code point sits in the BMP, and scanning only the BMP keeps import cheap.
_PS_OPENERS = "".join(chr(cp) for cp in range(0x10000) if unicodedata.category(chr(cp)) == "Ps")
_FORGED_PROVENANCE_RE = re.compile(
    rf"[{re.escape(_PS_OPENERS)}]\s*sent\s*by\s*session\b", re.IGNORECASE
)
_QUOTED_PROVENANCE = "[quoted: sent by session"


def _neutralize_send_provenance(body: str) -> str:
    """Defang any provenance-shaped opener in a peer-sent *body*.

    Only the matched span in the ORIGINAL text is rewritten, so the rest of the
    body stays byte-exact. Substitution rather than deletion, so a reader still
    sees that the sender wrote something envelope-shaped. Only the genuine
    envelope, prepended by this module, opens with ``[sent by session``.
    """
    # Case is folded by the pattern's IGNORECASE (Unicode simple case folding),
    # which keeps the normalized view length-stable for the span map back.
    spans = _marker_spans(body, (_FORGED_PROVENANCE_RE,))
    return _apply_marker_spans(body, spans, _QUOTED_PROVENANCE)


#: What the envelope names for a broadcast. The verb is part of the message's
#: meaning, not decoration: "the base moved, rebase before you push" addressed to
#: one worker is a fact about that worker's branch, and the same words addressed to
#: eight are a fact about the base. A worker that cannot tell which it received
#: cannot judge whether a sibling is about to make the same change.
BROADCAST_VIA = "session_broadcast"


@dataclass
class _DeliveryProgress:
    """Delivery-call-owned facts that survive cancellation into the caller."""

    steer_await_entered: bool = False


# Shielded steer deliveries whose awaiting frame was cancelled before they
# finished. A shielded task is referenced only by the shield wrapper our frame
# just dropped, so without a strong reference here the event loop may collect it
# MID-RPC -- which is the very interruption the shield exists to prevent. Entries
# remove themselves in the done callback, so the set holds at most the deliveries
# currently outliving their caller.
_ORPHANED_STEER_DELIVERIES: set["asyncio.Future[Any]"] = set()


def _retain_orphaned_steer_delivery(
    task: "asyncio.Future[Any]",
    slot_key: str,
    *,
    unavailable_outcome: str | None = None,
    on_unavailable: "Callable[[], Awaitable[None]] | None" = None,
) -> None:
    """Keep a shielded steer delivery alive after its caller stopped waiting.

    The caller has already reported this delivery as cancelled-with-unknown-outcome,
    so nothing downstream reads the result. Two things still have to happen. The
    delivery coroutine's OWN reconciliation -- popping the per-text steer maps and
    recording the transcript row -- which it does itself once it is not interrupted.
    And the caller's post-outcome handling for ``unavailable_outcome``, which a
    cancelled frame cannot run: that outcome means the text was never handed over,
    so without ``on_unavailable`` queueing it the instruction is lost rather than
    merely delayed. Logged either way, because this is the only trace that a
    delivery completed after its caller moved on.
    """
    _ORPHANED_STEER_DELIVERIES.add(task)

    def _done(finished: "asyncio.Future[Any]") -> None:
        _ORPHANED_STEER_DELIVERIES.discard(finished)
        if finished.cancelled():
            # Only a loop shutdown reaches here: the shield absorbed the caller's
            # cancellation, so nothing else cancels this task.
            logger.warning(
                "session_send: shielded steer delivery to %s was cancelled outright; "
                "its steer bookkeeping may not have reconciled",
                slot_key,
            )
            return
        exc = finished.exception()
        if exc is not None:
            logger.warning(
                "session_send: shielded steer delivery to %s finished with %r after "
                "its caller stopped waiting",
                slot_key,
                exc,
            )
            return
        outcome = finished.result()
        logger.info(
            "session_send: shielded steer delivery to %s finished with outcome=%s "
            "after its caller stopped waiting",
            slot_key,
            outcome,
        )
        if on_unavailable is None or outcome != unavailable_outcome:
            return
        # Scheduled rather than awaited: a done callback runs on the loop and cannot
        # await. Retained in the same set for the same reason the delivery is -- a
        # task referenced only by a local would be collectible mid-queue.
        fallback = asyncio.ensure_future(on_unavailable())
        _ORPHANED_STEER_DELIVERIES.add(fallback)

        def _fallback_done(done: "asyncio.Future[Any]") -> None:
            _ORPHANED_STEER_DELIVERIES.discard(done)
            if done.cancelled():
                logger.warning(
                    "session_send: the queue fallback for the orphaned steer to %s was "
                    "cancelled; that message is not queued",
                    slot_key,
                )
                return
            exc = done.exception()
            if exc is not None:
                logger.warning(
                    "session_send: the queue fallback for the orphaned steer to %s "
                    "failed with %r; that message is not queued",
                    slot_key,
                    exc,
                )

        fallback.add_done_callback(_fallback_done)

    task.add_done_callback(_done)


async def send_to_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    message: str,
    steer: bool = False,
    caller_fenced: bool | None = None,
    via: str = "session_send",
    _delivery_progress: "_DeliveryProgress | None" = None,
) -> dict[str, Any]:
    """Deliver *message* to *target* as its next agent turn.

    ``via`` names the VERB in the provenance envelope the target reads. It exists
    because :func:`broadcast_to_targets` delivers through this function, and a
    worker told something every sibling was also told must be able to see that:
    the same sentence means one thing addressed to one session and another
    addressed to eight. It never affects authorization or delivery.

    The delivery path is the same queue-vs-run decision the dashboard composer
    uses (``enqueue_or_run_prompt``): an idle target starts a turn immediately,
    a busy one queues the message for its next turn. Both outcomes are reported
    distinctly — ``started`` says which happened — because "it ran" and "it will
    run later" must not look the same to a caller coordinating several sessions.
    A queued delivery is re-validated at the drain: the entry
    carries the containment that held here, and a constraint newly held at
    delivery time drops it with a visible notice instead of executing it under
    the weaker authorization that admitted it. The entry also carries THIS
    caller's slot, so that drop appends a notice to the caller's own transcript
    as well: ``started: False`` says the message will run later, and a caller
    told only that would otherwise wait for a reply that can never come.

    ``steer`` asks for a THIRD outcome on a busy target: the message cuts into
    the turn already running (``steer_into_running_turn``) instead of waiting for
    it, so a caller watching a worker go the wrong way can say so while the work
    is still in flight rather than after it lands. Reported as ``steered``, its
    own field, for the same reason ``started`` is one.

    Two properties make that arm safe rather than a hole in the queued arm's
    checks. The delivery happening NOW removes the queue's waiting window and
    leaves a narrower one: the steer RPC suspends on ``stdin.drain()``.

    * Containment is re-validated at the queue drain because a queued prompt runs
      later than the moment it was authorized, so the target can gain a channel
      link or an outbound mirror while it waits. Nothing suspends between
      ``authorize_target`` and the steer call, so the text is committed against the
      containment the gate cleared — but the RPC's own suspension is a window, and
      each outcome closes it differently. The fallback re-runs the gate before
      taking the queue arm and refuses a target that resolves to a different slot
      OBJECT. A requeued steer becomes an ordinary queued entry and meets the drain
      unexempted. A SUCCESSFUL steer cannot be recalled, so the admission
      containment is compared against the post-RPC containment and the turn is
      stopped when a constraint newly holds — the reply path resolves its mirror
      live, so a mirror linked mid-turn is a real audience.
    * Provenance is carried by the text, not by the delivery mode: both arms hand
      over the same redacted, envelope-prefixed prompt, so neither the target
      agent nor a person reading the transcript can read an injected message as
      something the human typed. It also travels to the requeue as
      ``user_origin=False``, so a peer's steer does not inherit the composer's
      exemption from the drain's LINKED drop.

    A steer that cannot be injected falls back to the queue — never dropped, and
    the caller is told it queued rather than steered.

    The turn is NOT charged against the background-turn cap, and deliberately so:
    that cap only binds unattended (app-owned) slots, and every target this
    function can authorize is attended — see the comment at the delivery call.
    """
    # A broadcast passes its own instance so the timeout handler can read facts
    # this delivery recorded before cancellation erased its stack. A plain send
    # needs the same local state even though no outer timeout reads it.
    delivery_progress = (
        _delivery_progress if _delivery_progress is not None else _DeliveryProgress()
    )

    # The delivery appends to the target's rows: a target restored as a
    # sidebar row loads its window first, ahead of the prewarm below.
    await load_target_window(state, target)
    # Same prewarm ordering as `stop_target`, for the same reasons: the SEL
    # write inside `authorize_target`'s deny path must be a cache hit, and the
    # config warm must be the LAST suspension before the synchronous gate.
    try:
        await asyncio.to_thread(sel)
    except Exception:  # noqa: BLE001 - a prewarm failure must not fail the send
        logger.warning("session-control SEL prewarm failed", exc_info=True)
    await prewarm_enabled_check()

    body = message.strip()
    if not body:
        raise SessionControlError("message is empty", code="message_empty", status=400)
    if len(body) > MAX_SEND_MESSAGE_CHARS:
        raise SessionControlError(
            f"message exceeds {MAX_SEND_MESSAGE_CHARS} characters",
            code="message_too_long",
            status=400,
        )

    slot = authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        operation="send",
        precomputed_ownership_fenced=caller_fenced,
    )

    # A relay archive is read-only: the delivery below would run the crew's old
    # session on THIS machine, the same thing the send / regenerate / rewind /
    # continue paths refuse.
    if is_relay_archive(slot):
        raise SessionControlError(RELAY_ARCHIVE_ERROR, code=RELAY_ARCHIVE_CODE, status=409)

    # Cancellation can only enter at an await. Route every await below the
    # authorization through this helper so no authorized cancellation loses its
    # target-level permission trail.
    async def _await_authorized_delivery(awaitable: Any) -> Any:
        try:
            return await awaitable
        except asyncio.CancelledError:
            # `cancelled` means authorization was granted, delivery was cancelled
            # mid-flight, and whether the message landed is unknown.
            _audit(
                caller_session_key=caller_session_key,
                operation="send",
                slot_key=slot.key,
                outcome="cancelled",
                detail={
                    "steer_requested": bool(steer),
                    "chars": len(body),
                    "delivery_result": "unknown",
                },
            )
            raise

    # Deferred for the same import cycle `stop_target` documents.
    from kiro_crew.dashboard.chat_runner import _run_chat

    caller_key = caller_slot_key(state, caller_session_key)
    # Sanitized on the same grounds as the steer path (``chat_delivery`` sanitizes
    # before ``slot.append``): this body comes from ANOTHER session and is persisted
    # into — and broadcast from — the target's transcript, so raw content must never
    # reach that surface. The length gate above deliberately measures the RAW body:
    # redaction can only shrink the text, so validating the raw form is the honest
    # limit and keeps the error keyed to what the caller actually sent.
    prompt = _SEND_PROVENANCE.format(
        caller=caller_key or "unknown", via=via or "session_send"
    ) + _neutralize_send_provenance(sanitize_outbound(body))

    steered = False
    requeued = False
    # Containment names that newly held when a successful steer's turn was stopped;
    # empty on every other path. Read by the audit below.
    steer_audience_changed: list[str] = []
    # True only when the containment stop below raised. The steer was already
    # consumed by the turn at that point, so the failure travels in the audit trail
    # and the caller still sees the delivery it got.
    steer_containment_stop_failed = False
    if steer and slot.running:
        # The mid-turn arm. ONE text is handed to both arms, so what the target
        # reads and what its transcript keeps are the same bytes either way:
        # already redacted, already carrying the provenance envelope, so an
        # injected steer can no more pose as human typing than a queued delivery
        # can. ``chat_delivery`` runs its own ``sanitize_outbound`` before it
        # appends the row, which is a second pass over text that already cleared
        # the same guard.
        #
        # Gated on ``slot.running`` because a steer needs a turn to cut into: the
        # turn publishes the steer-capable client and clears it at teardown, so on
        # an idle slot there is nothing to inject and the queue-or-run arm below is
        # the whole delivery.
        #
        # Deferred import for the cycle `_run_chat` above documents.
        from kiro_crew.dashboard.chat_delivery import (
            STEER_REQUEUED,
            STEER_STEERED,
            STEER_UNAVAILABLE,
            steer_into_running_turn,
        )

        # Captured BEFORE the RPC and synchronous with the gate above -- no await
        # separates `authorize_target` from here -- so this IS the containment the
        # authorization cleared, recorded in the drain's own vocabulary so the
        # comparison below is the same one queued prompts get.
        #
        # The sending slot rides the same dict because the REQUEUE copies it onto
        # the entry verbatim, so a steer that falls back to the queue and is then
        # dropped at the drain reports back to us like any queued delivery. Inert
        # for the two other readers: `newly_held_constraints` reads only the
        # containment key, and so does the audience fence below.
        admission = {**containment_meta(state, slot), **send_origin_meta(state, caller_key)}

        # Which turn this steer is going into. `_turn_generation` increments on every
        # task assignment, so it identifies a turn even if a later task object reuses
        # an address. Captured here, synchronously with the gate, because the stop
        # below must not cancel a DIFFERENT turn: if the steered turn ends during the
        # RPC and a queued prompt starts the next one, `slot` still reads as running
        # and an unguarded stop would cancel work that never received this text.
        steered_turn_generation = slot._turn_generation

        # Set BEFORE the RPC, synchronously with the gate above, because this is the
        # half the post-RPC stop cannot cover: `_deliver_cross_surface_reply` runs
        # from the turn's own completion path and resolves the mirror live, so a turn
        # that finishes while this coroutine is suspended would publish to the new
        # audience before any check of ours resumes. Reacting cannot recall a sent
        # reply; withholding until the check has run can.
        audience_fence = uuid.uuid4().hex
        slot._steer_audience_fences[audience_fence] = admission

        # `user_origin=False`: this text was not typed into the target's own
        # surface. The requeue reads it to decide `directive_user_origin`, and a
        # peer must not inherit the composer's exemption from the LINKED drop.
        async def _steer_delivery() -> str:
            return await steer_into_running_turn(
                state, slot, prompt, user_origin=False, admission=admission
            )

        delivery_progress.steer_await_entered = True

        # Shielded, because THIS await is what `broadcast_to_targets` cancels when a
        # target overruns `BROADCAST_TARGET_ALLOWANCE_SECS`, and
        # `steer_into_running_turn` guards its own `client.steer` with
        # `except Exception` -- which does not catch `CancelledError`. Awaited
        # directly, the cancellation unwinds THROUGH the RPC and skips that
        # coroutine's single reconciliation tail, while the bytes may already have
        # reached kiro-cli: the turn then runs text no `slot.append` recorded, and
        # `_steer_delivery_ids` / `_steer_send_ids` / `_steer_user_origin` /
        # `_steer_admissions` are never popped. Nothing else pops them --
        # `_settle_consumed_steers` clears only the attachment and decision-strip
        # maps, and `_requeue_unconsumed_steers` returns early once settling emptied
        # `_pending_steers`. The surviving `_steer_delivery_ids` entry then refuses
        # this exact text on that slot forever (the one-per-text guard reads that
        # dict) and `retained_steer_count` never falls back below
        # `MAX_PENDING_STEERS`, after which the slot refuses every steer.
        #
        # The shield splits the two halves the cancellation conflated: our frame
        # still receives it, so the broadcast reports its timeout row on schedule
        # and the caller still hears "outcome unknown", while the delivery runs to
        # its own end and reconciles itself. Inert on the direct `session_send`
        # path, which has no per-target budget above it.

        # The fallback a CANCELLED frame cannot reach. `STEER_UNAVAILABLE` means no
        # live steer-capable client, an RPC that lost the text, or an identical steer
        # already in flight -- in every case the text was never handed over and the
        # shielded delivery has cleared its own per-text state, so nothing downstream
        # holds it. The arm below queues it instead of dropping it; once the frame is
        # gone that arm cannot run, and shielding the delivery
        # without this would turn an outcome the caller recovers from into a lost
        # instruction. Re-gated on the same terms as that arm, because the RPC
        # suspended and the queue entry records the containment for the drain to
        # re-assert: a snapshot taken now must not certify a link the authorization
        # never saw.
        async def _queue_after_orphaned_unavailable() -> None:
            await prewarm_enabled_check()
            regated = authorize_target(
                state,
                caller_session_key=caller_session_key,
                target=target,
                operation="send",
                precomputed_ownership_fenced=caller_fenced,
            )
            if regated is not slot:
                # Object identity, for the reason the in-frame arm gives: a target
                # closed and resumed under the same key is a different object whose
                # key still compares equal, and queueing onto the detached one loses
                # the text as surely as dropping it.
                logger.warning(
                    "session_send: orphaned steer to %s came back unavailable, but the "
                    "target resolved to a different session; the message is not queued",
                    slot.key,
                )
                return
            slot.enqueue_or_run_prompt(
                prompt,
                _run_chat,
                state,
                extra_meta=send_origin_meta(state, caller_key),
            )
            logger.info(
                "session_send: orphaned steer to %s came back unavailable and was "
                "queued instead of lost",
                slot.key,
            )

        _steer_task = asyncio.ensure_future(_steer_delivery())
        try:
            outcome = await _await_authorized_delivery(asyncio.shield(_steer_task))
        except asyncio.CancelledError:
            _retain_orphaned_steer_delivery(
                _steer_task,
                slot.key,
                unavailable_outcome=STEER_UNAVAILABLE,
                on_unavailable=_queue_after_orphaned_unavailable,
            )
            raise
        steered = outcome == STEER_STEERED
        # The turn ended while the steer RPC was suspended and its teardown moved
        # the text onto the queue: it WILL run, and taking the queue arm below
        # would deliver it a second time. Reported as a queued delivery, which is
        # what it now is.
        requeued = outcome == STEER_REQUEUED
        if requeued and state._slots.get(slot.key) is not slot:
            # The teardown queued this text on THIS slot object, and the key has
            # since stopped resolving to it -- the target was closed and recreated
            # while the RPC was suspended on `stdin.drain()`. The queue that now
            # holds the prompt belongs to a detached object no drain will ever
            # reach, so the text is gone. Refusing is the only honest answer: the
            # `steered` arm already guards this same replacement (it records
            # `slot_replaced` below) and the fallback arm re-runs the gate, so this
            # was the one outcome that reported success for a prompt that had
            # quietly stopped existing.
            #
            # Object identity, not `key != key`: a fresh object under the same key
            # compares equal by key and would pass a key comparison.
            raise SessionControlError(
                "that target was replaced while the steer was in flight and the "
                "message did not survive the hand-over; re-read the session list "
                "and send again",
                code="target_moved",
                status=409,
            )
        if steered:
            # The text is IN the running turn: `STEER_STEERED` means kiro-cli
            # acknowledged consumption, so unlike the requeue arm there is nothing
            # left to remove and unlike the fallback arm there is nothing left to
            # refuse. What can still be prevented is the turn's remaining output
            # reaching an audience this send was never authorized against -- a
            # mirror linked, retargeted, or a channel link bound while the RPC was
            # suspended on `stdin.drain()`. `_deliver_cross_surface_reply` resolves
            # the mirror LIVE at reply-delivery time, so that audience is real and
            # not fixed at turn start.
            #
            # So: compare, and stop the turn when a constraint newly holds. One
            # cooperative cancel, the same thing the Stop button's first press does,
            # with `escalate=False` because this is one automatic decision and not a
            # person who watched a cancel fail to take.
            #
            # This comparison does NOT decide whether the reply may be published. The
            # admission recorded before the RPC stays on the slot for the whole turn,
            # and the publisher re-evaluates it at delivery -- a mirror bound between
            # this moment and the reply would be just as unauthorized, and only the
            # publisher's own moment can see it.
            steer_audience_changed = newly_held_constraints(
                containment_snapshot(state, slot, on_probe_failure=True), admission
            )
            if state._slots.get(slot.key) is not slot:
                # The slot object was replaced under the same name while the RPC was
                # in flight, so the turn holding this text belongs to a session that
                # the name has stopped resolving to. Reported alongside the containment
                # names rather than folded into them: it is an identity change, not
                # a constraint that newly holds.
                steer_audience_changed = [*steer_audience_changed, "slot_replaced"]
            if steer_audience_changed:
                # Deferred import for the cycle the steer import above documents.
                from kiro_crew.dashboard.chat_handlers import stop_slot_turn

                if slot._turn_generation != steered_turn_generation:
                    # The turn this text entered has already ended and another one
                    # has started on the same slot. Stopping now would cancel work
                    # that never received this steer, which is worse than the
                    # exposure being narrowed: the steered turn is over, so whatever
                    # it published is already published, and the fence recorded
                    # before the RPC still withholds the cross-surface legs for the
                    # remainder of the turn that holds it.
                    steer_audience_changed = [
                        *steer_audience_changed,
                        "turn_rolled_over_stop_skipped",
                    ]
                else:
                    # The text is already consumed by the turn at this point, so this
                    # stop is a best-effort narrowing of what the turn goes on to
                    # publish -- it is NOT part of delivering the message. Letting it
                    # raise would abort before the success audit and the ok:True
                    # return below, reporting a steer the turn has already taken as
                    # failed; the caller's natural response is to send again, which
                    # delivers the same text twice. The turn's own reply legs consult
                    # the audience fence recorded before the RPC, so containment does
                    # not depend on this stop succeeding.
                    try:
                        await _await_authorized_delivery(
                            stop_slot_turn(
                                state,
                                slot,
                                source="session_send_steer_containment",
                                escalate=False,
                            )
                        )
                    except Exception:
                        logger.exception(
                            "Containment stop failed after a steer was consumed "
                            "(slot=%s, newly held=%s); the fence still withholds "
                            "cross-surface publication",
                            slot.key,
                            steer_audience_changed,
                        )
                        steer_containment_stop_failed = True
        else:
            # Not steered: no text of ours is inside the running turn on this path, so
            # there is nothing for the record to protect -- the requeue arm's entry
            # faces the drain and the fallback arm re-runs the gate. Dropped by key so
            # another peer's concurrent steer keeps its own.
            slot._steer_audience_fences.pop(audience_fence, None)
        if not steered and not requeued:
            # STEER_UNAVAILABLE: no live steer-capable client, an RPC that lost the
            # text, or an identical steer already in flight. The message falls back
            # to the queue arm rather than being dropped — but that arm records the
            # containment holding at APPEND time for the drain to re-assert, and
            # the RPC above suspends, so the state it would record can differ from
            # the state this send was authorized against. Re-run the gate so the
            # snapshot cannot certify a link the authorization never saw.
            # The prewarm before the gate at the top of this function predates the
            # steer RPC, which suspends: a config edit during that suspension
            # invalidates the cache, and `authorize_target` -> `session_control_enabled`
            # would then run `KiroCrewConfig.load()` synchronously on the shared loop,
            # stalling every other session while a file is read and validated. Warming
            # again here is what `prewarm_enabled_check`'s own docstring prescribes for
            # a gate that sits after an await.
            async def _prewarm_regate() -> None:
                await prewarm_enabled_check()

            await _await_authorized_delivery(_prewarm_regate())
            regated = authorize_target(
                state,
                caller_session_key=caller_session_key,
                target=target,
                operation="send",
                precomputed_ownership_fenced=caller_fenced,
            )
            if regated is not slot:
                # Object identity, not ``regated.key != slot.key``. The delivery
                # below uses THIS ``slot`` object, so a target closed and resumed
                # under the same key during the RPC yields a fresh object whose key
                # still compares equal: the guard would pass, the message would land
                # on the detached object, and the caller would be told it succeeded.
                raise SessionControlError(
                    "that target resolved to a different session while the steer "
                    "was in flight; re-read the session list and send again",
                    code="target_moved",
                    status=409,
                )

    if steered or requeued:
        # Neither arm starts a turn: a steer runs inside one that is already going,
        # and a requeued steer waits for the next like any queued message.
        started = False
    else:
        # `_run_chat` is passed straight through, NOT wrapped in
        # `state.run_background_turn`: that cap is structurally unreachable here.
        # `run_background_turn` returns the coroutine untouched for an attended slot
        # (`state.py`, "this wrapper is inert"), `_ChatSlot.unattended` is
        # `bool(self._app) and not self._human_seen`, and `authorize_target` refuses
        # every `_app` target above (`app_scoped_target`) — so no target this
        # function can reach is ever unattended, and a wrapper would only add a
        # never-taken timeout arm. The composer's own queued path does the same
        # (`server.py` passes `_run_chat` directly).
        started = bool(
            slot.enqueue_or_run_prompt(
                prompt,
                _run_chat,
                state,
                # Only the QUEUE arm keeps it (the run arm has no entry): a
                # delivery that waits is the one a later drain can drop, and this
                # stamp is what lets that drop be reported back to us instead of
                # ending at the target's own transcript.
                extra_meta=send_origin_meta(state, caller_key),
            )
        )
    try:
        state.push_slots_update()
    except Exception:  # pragma: no cover - sidebar refresh is best-effort
        logger.debug("session_send: push_slots_update failed", exc_info=True)

    _audit(
        caller_session_key=caller_session_key,
        operation="send",
        slot_key=slot.key,
        outcome="allowed",
        # `steer` is what the caller ASKED for and `steered` is what happened, so a
        # fallback to the queue is readable in the trail rather than looking like a
        # caller that never asked.
        detail={
            "started": started,
            "steer_requested": bool(steer),
            "steered": steered,
            "chars": len(body),
            # Only when a steer's turn was actually stopped, so the common shape is
            # unchanged. Names the constraints that newly held, which is what a
            # reader needs to tell this apart from an ordinary delivery: the message
            # WAS delivered into the turn and the turn was then cancelled because
            # its audience had changed under the RPC.
            **({"steer_stopped_on": steer_audience_changed} if steer_audience_changed else {}),
            # The stop raised. Present only in that case, so its absence is not a
            # claim that a stop succeeded on a path where none was attempted. The
            # delivery outcome above is unchanged: the turn had already taken the
            # text, and the audience fence recorded before the RPC still withholds
            # cross-surface publication whether or not the stop took.
            **({"steer_containment_stop_failed": True} if steer_containment_stop_failed else {}),
        },
    )
    return {"ok": True, "target": slot.key, "started": started, "steered": steered}


#: The two broadcast modes, named rather than a boolean. ``queue`` waits for each
#: target's current turn; ``steer`` cuts into it. They are separate names because
#: the choice is not a refinement of one delivery — "tell everyone when they next
#: come up for air" and "interrupt everyone now" are different instructions, and a
#: caller that means the first must not reach the second by defaulting.
BROADCAST_MODES = ("queue", "steer")

#: The per-delivery bound this module ENFORCES, and the MCP client's one request
#: budget, are one name in ``kiro_crew.validation`` rather than two literals that
#: must be remembered together -- the same reason ``MAX_BROADCAST_TARGETS`` lives
#: there. See that definition for why a client budget below this bound would
#: discard the per-target report.

#: The refusal code a timed-out delivery reports. Distinct from
#: ``delivery_failed`` because a timeout is classified from delivery-owned
#: progress plus any exact text the target still retains: pre-authorization,
#: retained text, ambiguous or completed hand-over, and an unavailable target
#: each need different caller guidance. See :func:`broadcast_to_targets`.
BROADCAST_TIMEOUT_CODE = "delivery_timeout"

_TIMEOUT_RETAINED = "retained"
_TIMEOUT_NOT_HANDED_OVER = "not_handed_over"
_TIMEOUT_UNKNOWN = "unknown"


def _timeout_delivery_observation(
    state: "DashboardState",
    slot: Any | None,
    prompt: str,
    delivery_progress: _DeliveryProgress,
) -> str:
    """Classify a cancelled delivery from its own progress and retained text."""
    if slot is None or state._slots.get(slot.key) is not slot:
        return _TIMEOUT_UNKNOWN
    if prompt in slot._pending_steers or any(
        isinstance(entry, dict) and entry.get("content") == prompt for entry in slot._queue
    ):
        return _TIMEOUT_RETAINED
    if delivery_progress.steer_await_entered:
        return _TIMEOUT_UNKNOWN
    return _TIMEOUT_NOT_HANDED_OVER


def broadcast_audience(state: "DashboardState", caller_key: str) -> list[str]:
    """The slot keys *caller_key* created and can still address, sorted.

    The DEFAULT audience of a broadcast: a conductor's own workers. Read from the
    live slots' ``_created_by`` — the same field the ownership fence
    (:func:`_created_by_other`) reads — so the default audience is by construction
    a subset of what the fence would admit, and an audience this returns can still
    be refused per target (a worker that went incognito, gained a channel mirror,
    or is an app's) by the gate each delivery runs. Nothing here authorizes.

    The caller itself is excluded: ``authorize_target`` refuses a self-target, and
    silently collecting a refusal the caller could not have avoided would make
    every broadcast report one failure.

    Sorted by key so the delivery order is stable across calls. A broadcast is not
    atomic — deliveries land one at a time — so a caller reading two reports needs
    the same order in both to compare them.
    """
    out = [
        key
        for key, slot in list(state._slots.items())
        if key != caller_key and not _created_by_other(slot, caller_key)
    ]
    return sorted(out)


def _too_many_targets(count: int) -> SessionControlError:
    """The audience-cap refusal, from one place because it is raised from two.

    Refused rather than truncated. A silently-cut audience is a broadcast the
    caller believes reached everyone, and the sessions past the cut are the ones it
    will never think to check.
    """
    return SessionControlError(
        f"a broadcast reaches at most {MAX_BROADCAST_TARGETS} sessions and this "
        f"one names {count}; send to a named subset instead",
        code="too_many_targets",
        status=400,
    )


async def broadcast_to_targets(
    state: "DashboardState",
    *,
    caller_session_key: str,
    message: str,
    mode: str,
    targets: "list[str] | None" = None,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """Deliver *message* to several sessions at once, one at a time.

    Two modes, and they are the whole point of the verb being separate from a loop
    of :func:`send_to_target` calls the model writes itself: ``queue`` lets every
    target finish what it is doing, ``steer`` cuts into every running turn. A
    conductor telling eight workers "the base moved, rebase before you push" wants
    the first; one telling them "stop, the issue was already fixed" wants the
    second, and the difference is worth eight turns of latency.

    ``targets`` omitted means :func:`broadcast_audience` — the sessions this caller
    created. Naming them explicitly is for the subset case, and every named target
    goes through the same :func:`authorize_target` gate, so an explicit list can
    only ever be as permissive as one send at a time.

    PARTIAL DELIVERY IS THE NORMAL OUTCOME and the return shape says so per
    target. A target closed between the audience read and its own delivery, one
    that went incognito, one bound to a remote crew: each is one row with its
    refusal ``code``, and the remaining deliveries still happen. The alternative —
    abort on the first refusal — would leave a broadcast half-delivered with
    nothing saying which half, which is the one outcome a caller cannot recover
    from.

    Deliveries are SEQUENTIAL, never gathered. Each one takes the same slot locks,
    runs the same containment re-checks and (for a steer) suspends on an RPC, and
    running them concurrently would interleave those windows across sessions for
    no gain a caller can observe — the report is only read once all of them are
    done either way.

    Which is exactly why each delivery carries its own bound
    (``BROADCAST_TARGET_ALLOWANCE_SECS``): sequential delivery means one target
    that never answers is one target that starves every target behind it, and the
    urgent mode is the one that suspends on an RPC with no ceiling under it. On
    expiry that delivery is cancelled, reported as a ``delivery_timeout`` row, and
    the loop continues. The delivery records ONE marker: that it entered the steer
    await. Cancellation before that marker is provably before hand-over, so
    re-sending is safe. Cancellation once it is set is ambiguous -- inside the
    await or after it returned are indistinguishable here, and a second marker for
    the return would not separate them, because it could only be set once this one
    already was. Both read as unknown and must not invite a retry. The target's
    ``_pending_steers`` and queue
    still add useful information when they retain the exact text, but their shared
    absence cannot reconstruct delivery history. A target whose slot disappeared
    or was replaced also gets an unknown row. None of the rows claims delivery or
    certain execution.
    """
    if mode not in BROADCAST_MODES:
        raise SessionControlError(
            f"mode must be one of {', '.join(BROADCAST_MODES)}",
            code="invalid_broadcast_mode",
            status=400,
        )
    body = message.strip()
    if not body:
        raise SessionControlError("message is empty", code="message_empty", status=400)
    if len(body) > MAX_SEND_MESSAGE_CHARS:
        raise SessionControlError(
            f"message exceeds {MAX_SEND_MESSAGE_CHARS} characters",
            code="message_too_long",
            status=400,
        )

    # The caller gate runs ONCE, here, before any audience is read: a caller that
    # may not control sessions at all must be refused without learning how many it
    # created. The per-target gate below re-runs it for each delivery, which is
    # what actually binds every send — this one is the early, cheap refusal.
    await prewarm_enabled_check()
    deny = _deny_factory(caller_session_key=caller_session_key, operation="broadcast", target="")
    caller_key = refuse_caller_identity(state, caller_session_key=caller_session_key, deny=deny)
    caller_slot = refuse_caller_surface(state, caller_key=caller_key, deny=deny)
    # Captured for the re-check before the return below, on the same terms
    # `created_session_status` captures it: this verb suspends many times between
    # here and its payload, and the rows it returns name the caller's own sessions.
    caller_workspace = str(getattr(caller_slot, "workspace", "default"))
    ownership_fenced = (
        _caller_is_ownership_fenced(state, caller_key) if caller_fenced is None else caller_fenced
    )
    resolution_slots = _broadcast_resolution_slots(
        state,
        caller_key=caller_key,
        caller_slot=caller_slot,
        ownership_fenced=ownership_fenced,
    )

    explicit = targets is not None
    if explicit:
        # THE CAP, CHECKED BEFORE ANYTHING RESOLVES. Below the resolution loop this
        # check is correct and useless: reaching it costs one `_resolve_slot` per
        # name -- a copy of the authorized-slot set and two full passes over it,
        # with nothing awaited between them -- so an oversized list does the work
        # the refusal exists to prevent and is refused afterwards. Here the cost of
        # an oversized list is one integer comparison.
        #
        # Counted on the caller's RAW list rather than on the deduplicated audience,
        # so the bound is on what the caller submitted. A list of `cap + 1`
        # duplicate spellings of one session is therefore refused even though it
        # would collapse under the cap, and that is the intended reading: the cap
        # bounds the work this call asks for, and deduplication is that work.
        #
        # The route bounds this too, and both are deliberate: the route is the
        # boundary an in-sandbox shell reaches, this is the boundary every
        # in-process caller reaches.
        if len(targets or []) > MAX_BROADCAST_TARGETS:
            raise _too_many_targets(len(targets or []))
        # De-duplicated by an AUTHORIZED resolved slot while PRESERVING the
        # caller's order, so two spellings of one session still deliver once.
        # Resolution is restricted to slots the caller may address; a private
        # title therefore remains the caller's own string and cannot turn into a
        # slot key in the refusal row. This loses no real target because
        # ``authorize_target`` would refuse every slot outside this set anyway,
        # and every retained key still goes through that gate at delivery.
        # A name that cannot be resolved, or is ambiguous within the authorized
        # set, remains in the audience so the gate can return its refusal row.
        seen: set[tuple[str, str]] = set()
        audience: list[str] = []
        for raw in targets or []:
            name = str(raw).strip()
            if not name:
                continue
            try:
                slot = _resolve_slot(state, name, candidates=resolution_slots)
            except SessionControlError:
                slot = None
            identity = ("slot", slot.key) if slot is not None else ("unresolved", name)
            if identity not in seen:
                seen.add(identity)
                audience.append(slot.key if slot is not None else name)
        if not audience:
            # The caller named targets and not one of them is usable. Both silent
            # answers are wrong here: falling back to the default audience turns a
            # malformed argument into an accidental fleet-wide send, and reporting
            # "delivered 0 of 0" hides a call that never had a chance of working.
            raise SessionControlError(
                "targets was given but names no session; omit it to reach every "
                "session you created",
                code="target_required",
                status=400,
            )
    else:
        audience = broadcast_audience(state, caller_key)

    if len(audience) > MAX_BROADCAST_TARGETS:
        # The DEFAULT audience's own bound. An explicit list was already refused
        # above, before resolution, and deduplication only ever shrinks it -- so
        # this now guards the fence-built audience, whose size the caller did not
        # choose. It should be unreachable: ``broadcast_audience`` returns the live
        # slots this caller created, which ``MAX_SLOTS_PER_CREATOR`` bounds at or
        # below this cap (see ``MAX_BROADCAST_TARGETS``). Kept because "unreachable
        # because two constants are ordered correctly" is a property a later edit
        # can break, and truncating a fleet-wide send is the failure that must not
        # be the fallback.
        raise _too_many_targets(len(audience))

    results: list[dict[str, Any]] = []
    timeout_prompt = _SEND_PROVENANCE.format(
        caller=caller_key or "unknown", via=BROADCAST_VIA
    ) + _neutralize_send_provenance(sanitize_outbound(body))
    for name in audience:
        try:
            timeout_slot = _resolve_slot(state, name)
        except SessionControlError:
            timeout_slot = None
        delivery_progress = _DeliveryProgress()
        try:
            # Each delivery gets its OWN bound. Without one a single unresponsive
            # target starves every target behind it: the steer arm suspends on
            # `client.steer` -> `stdin.drain()` with no `wait_for` anywhere beneath
            # it, so a session whose process is wedged blocks this loop
            # indefinitely and targets 3-8 of a "stop, the issue was already fixed"
            # never hear it. The client's request budget then expires and the
            # caller is told the whole broadcast failed, discarding the per-target
            # report that is the only thing that could have said which half landed.
            # Bounding here and continuing turns that into one refused row.
            #
            # The bound wraps the WHOLE call rather than the steer RPC alone, which
            # is what gives the queue arm the same protection: that arm also
            # suspends before it delivers (`asyncio.to_thread(sel)`,
            # `prewarm_enabled_check`, and the config load that can sit behind it),
            # and a queue broadcast stalled on a config read starves the fleet just
            # as thoroughly as a wedged steer does.
            #
            # The bound wraps two pre-authorization awaits as well as the
            # delivery arms. A cancel at `asyncio.to_thread(sel)` or
            # `prewarm_enabled_check()` happens before `authorize_target`, so it
            # cannot leave this text pending or queued. A cancel at the steer RPC
            # can leave the exact prompt in `_pending_steers`, and turn teardown can
            # move it into a queue entry's `content` while cancellation unwinds.
            #
            # Capture the resolved slot and a delivery-owned progress marker before
            # the call. After cancellation, retained text still proves it may run,
            # but empty target containers prove nothing: consumption and a send that
            # never started both empty them. The progress marker distinguishes a
            # pre-authorization cancellation from the ambiguous steer await and every
            # post-return await. If the object disappeared or was replaced, the
            # outcome is unknown regardless.
            #
            # `send_to_target` records its `cancelled` audit only after
            # authorization. Cancellation at either earlier await therefore leaves
            # no target-level permission row, while cancellation in an authorized
            # delivery keeps that row unchanged.
            sent = await asyncio.wait_for(
                send_to_target(
                    state,
                    caller_session_key=caller_session_key,
                    target=name,
                    message=body,
                    steer=(mode == "steer"),
                    caller_fenced=caller_fenced,
                    via=BROADCAST_VIA,
                    _delivery_progress=delivery_progress,
                ),
                timeout=BROADCAST_TARGET_ALLOWANCE_SECS,
            )
        except asyncio.TimeoutError:
            # A timeout says only that this side stopped waiting. Delivery-owned
            # progress decides whether retry advice is safe; target containers only
            # add the stronger fact that an exact retained copy may still run.
            observation = _timeout_delivery_observation(
                state, timeout_slot, timeout_prompt, delivery_progress
            )
            if observation == _TIMEOUT_RETAINED:
                error = (
                    f"this target did not finish its delivery within "
                    f"{BROADCAST_TARGET_ALLOWANCE_SECS:.3g}s, so the broadcast "
                    "stopped waiting and moved on; whether the message has run is "
                    "UNKNOWN. The same text is pending or queued on that target and "
                    "may still run. Do NOT re-send it: a duplicate could run too"
                )
            elif observation == _TIMEOUT_NOT_HANDED_OVER:
                error = (
                    f"this target did not finish its delivery within "
                    f"{BROADCAST_TARGET_ALLOWANCE_SECS:.3g}s, so the broadcast "
                    "stopped waiting and moved on; the target has neither a pending "
                    "steer nor a queued copy, so the delivery did not reach the "
                    "hand-over. Re-sending the same text is safe"
                )
            else:
                if timeout_slot is not None and state._slots.get(timeout_slot.key) is timeout_slot:
                    error = (
                        f"this target did not finish its delivery within "
                        f"{BROADCAST_TARGET_ALLOWANCE_SECS:.3g}s, so the broadcast "
                        "stopped waiting and moved on; cancellation reached the steer "
                        "await or a later await, so whether the message has run is "
                        "UNKNOWN. Re-sending could execute it twice"
                    )
                else:
                    error = (
                        f"this target did not finish its delivery within "
                        f"{BROADCAST_TARGET_ALLOWANCE_SECS:.3g}s, so the broadcast "
                        "stopped waiting and moved on; the target is unavailable, so "
                        "whether the message has run or is pending or queued is UNKNOWN"
                    )
            logger.warning(
                "session_broadcast: delivery to %s exceeded %.3gs and was "
                "cancelled mid-flight; post-cancel observation=%s",
                name,
                BROADCAST_TARGET_ALLOWANCE_SECS,
                observation,
            )
            results.append(
                {
                    "target": name,
                    "ok": False,
                    "code": BROADCAST_TIMEOUT_CODE,
                    "error": error,
                }
            )
            continue
        except SessionControlError as exc:
            # Collected, not raised. This target is out of reach; the rest are not,
            # and the caller needs to know which is which. The refusal was already
            # audited by the gate that produced it.
            results.append(
                {
                    "target": name,
                    "ok": False,
                    "code": exc.code,
                    "error": exc.message,
                }
            )
            continue
        except Exception:
            # An unexpected failure on ONE delivery must not take the broadcast
            # down with it, and it must not be reported as a refusal either: a
            # refusal has a code the caller can act on, this has none. Logged with
            # its traceback and reported as the internal failure it is.
            logger.exception("session_broadcast: delivery to %s failed", name)
            results.append(
                {
                    "target": name,
                    "ok": False,
                    "code": "delivery_failed",
                    "error": "delivery failed unexpectedly; see the gateway log",
                }
            )
            continue
        results.append(
            {
                "target": sent.get("target", name),
                "ok": True,
                "started": bool(sent.get("started")),
                "steered": bool(sent.get("steered")),
            }
        )

    delivered = sum(1 for row in results if row.get("ok"))
    _audit(
        caller_session_key=caller_session_key,
        operation="broadcast",
        slot_key=caller_key,
        outcome="allowed",
        detail={
            "mode": mode,
            # WHERE the audience came from, because the two are different acts: an
            # explicit list is a caller naming sessions, the default is the fence's
            # own set. A trail that cannot tell them apart cannot answer "who chose
            # these targets".
            "audience": "explicit" if explicit else "created",
            "requested": len(audience),
            "delivered": delivered,
            "chars": len(body),
        },
    )
    # The entry gate RAN, but its verdict does not survive this verb's suspensions,
    # and every `results` row below names one of the caller's own sessions. A channel
    # mirror can be bound onto an already-open dashboard session while a delivery is
    # awaiting -- the channel picker (`messaging/session_resume.py`) and the Slack
    # link route (`slack/interactions.py`) both do it with no idle-slot requirement --
    # so a payload built under the entry gate would reach that channel's audience
    # carrying private session keys. Same re-check, same audited path, as
    # `created_session_status` does after its scan; workspace is compared for the
    # same reason (it IS reassigned on a live slot, while a live key is never rebound
    # to a new object, so a slot-identity check could not fire).
    #
    # Raised AFTER the allowed audit above, deliberately: the deliveries were each
    # individually gated and have already happened, so the trail must record them.
    # What is refused is the report, which is why the message says so -- a caller
    # that reads this as "the broadcast failed" and sends again delivers twice.
    refuse_caller_surface(state, caller_key=caller_key, deny=deny)
    if str(getattr(caller_slot, "workspace", "default")) != caller_workspace:
        raise deny(
            "the calling session moved workspace while this broadcast was in flight, "
            "so the per-target report is withheld. The deliveries already happened -- "
            "do NOT send the same text again; read the targets' own transcripts",
            "caller_changed_mid_broadcast",
        )
    return {
        "ok": True,
        "mode": mode,
        "requested": len(audience),
        "delivered": delivered,
        # True only for the DEFAULT audience: the caller has created nothing that
        # is still open, which is a real state and not an error. An explicit list
        # that names nothing is refused before delivery, so it cannot reach here.
        **({"audience_empty": True} if not audience and not explicit else {}),
        "results": results,
    }


def _created_tree_roster(caller_key: str) -> "tuple[list[str], str]":
    """Slots the crew log says hang under *caller_key*, and how good that read was.

    The DURABLE half of :func:`created_session_status`. ``state._slots`` knows only
    what is open right now, so a worker that was closed, or whose session was lost
    to a crash, disappears from it entirely — and "I dispatched eight and can see
    six" is exactly the question this verb exists to answer. The crew log's session
    tree is written when a session is opened and survives both, so it is what
    supplies the roster while the live slots supply the status.

    The second value is the read's own quality, and it is returned rather than
    folded into the list because the three answers are not interchangeable:

    * ``readable`` — the fold saw every unit the store holds.
    * ``incomplete`` — a unit's bytes could not be read, or the population was
      capped, so rows may be MISSING. The fold is still served: this reader
      DISPLAYS lineage rather than deciding on it, which is the case
      :class:`~kiro_crew.crew_log.session_tree.TreeReading` documents as free to
      ignore the flag — but a caller counting its workers must be told the count
      is a floor.
    * ``unreadable`` — the crew log is off, or the projection is not seeded for the
      store configured now. There is no roster at all and the answer is live-only.

    Reads the in-memory fold and does NO I/O, so it is safe on the event loop. An
    unseeded projection answers ``unreadable`` rather than seeding itself here,
    for the reason :func:`_slot_tree_parent` gives: a seed is a disk read.

    The parent edge it follows is the CURRENT one, so an adopted session is listed
    under whoever holds it now rather than under whoever opened it. That is the
    right answer for this verb — it reports the sessions the caller is answerable
    for — and it cannot widen the caller's reach, because an adoption is itself
    gated by :func:`authorize_target`.
    """
    try:
        if not crew_log_emit.enabled():
            return [], "unreadable"
        proj = projection()
        if not proj.seeded_for_current_store:
            return [], "unreadable"
        reading = proj.reading()
    except Exception:
        logger.debug("session tree roster could not be read", exc_info=True)
        return [], "unreadable"
    children: list[str] = []
    overflow = False
    for slot, node in reading.nodes.items():
        if getattr(node, "parent_slot", None) != caller_key or slot == caller_key:
            continue
        if len(children) < MAX_SESSION_STATUS_ROWS:
            children.append(slot)
        else:
            overflow = True
    children.sort()
    return children, ("incomplete" if reading.incomplete or overflow else "readable")


def _gone_slot_lifecycles(slots: "Sequence[str]") -> dict[str, dict[str, Any]]:
    """Fold each gone slot's lifecycle so a `gone` row can say CLOSED versus LOST.

    The crew log already records a ``session/closed`` edge and the status fold
    already projects it as ``lifecycle``/``closed_at`` (see
    :func:`~kiro_crew.crew_log.projection._status_render`). This reads that
    projection per slot and nothing more -- no new terminal vocabulary, no new
    verb -- so a worker whose tab was closed after it finished is told apart from
    one lost with its process.

    The read is slot-keyed (:func:`~kiro_crew.crew_log.projection.read_slot_projection`),
    so it folds over EVERY unit the slot ran under, newest last, and reports the
    lifecycle of the slot's current session. A slot that was reopened and later
    closed again reads ``closed``; the ``session/opened`` of the reopen resets the
    close the earlier life recorded, which the status fold already handles.

    Returns a map ``{slot: {"status", "closed_at"?}}``. A slot is ``closed`` (and
    carries its ``closed_at``) only when its fold's newest life ended by the
    DELIBERATE tab close -- ``close_reason`` is ``removed`` and no turn is still
    open (see :func:`_lifecycle_is_closed`). Everything else is ``lost``: an
    ``open``/``unknown`` lifecycle, a read that raised, OR a ``closed`` lifecycle
    whose edge came from a process recycle (``reset``: idle expiry, RSS recycle,
    model/provider switch, the watchdogs) or a ``destroy`` (including mid-turn) --
    teardowns the tab and conversation outlive. "Lost" is the fail-safe reading: it
    is the state a patrol re-dispatches, and reporting a worker lost when it may
    have finished is cheaper than reading a recycled or mid-turn worker as
    ``closed`` and telling a conductor to stop looking for it.

    Touches disk (one slot-keyed fold per slot), so it is called off the event loop.
    """
    from kiro_crew.crew_log.projection import read_slot_projection

    out: dict[str, dict[str, Any]] = {}
    for slot in slots:
        try:
            value = read_slot_projection(slot, "status").value
        except Exception:
            # A read fault is not evidence the worker closed cleanly, so it reads as
            # LOST -- the fail-safe answer, the one a patrol acts on by re-dispatching.
            logger.debug("gone row lifecycle could not be folded for %s", slot, exc_info=True)
            out[slot] = {"status": "lost"}
            continue
        if _lifecycle_is_closed(value):
            out[slot] = {"status": "closed", "closed_at": value.get("closed_at")}
        else:
            out[slot] = {"status": "lost"}
    return out


#: Close reasons that mean "the worker FINISHED and its tab was closed" -- the one
#: direction a gone row may call ``closed``. Only the deliberate tab ✕ / ``session_close``
#: teardown (``remove`` -> ``END_REASON_REMOVED``) qualifies. Every OTHER close edge is a
#: process recycle or a mid-turn teardown that the tab and conversation survive --
#: ``reset`` (idle expiry, RSS recycle, model/provider switch, the watchdogs),
#: ``destroyed``/``destroyed_sid_retained`` (``destroy``, including mid-turn) -- and must
#: read ``lost``, the fail-safe state a patrol re-dispatches. Reading one of those as
#: ``closed`` is the fail-OPEN harm this split exists to remove: it would tell a conductor
#: a lost worker finished, so it stops looking for it.
_FINISHED_CLOSE_REASONS = frozenset({END_REASON_REMOVED})


def _lifecycle_is_closed(value: "Mapping[str, Any]") -> bool:
    """Whether a folded slot status means the worker finished and closed its tab.

    ``True`` only when the fold's newest life is closed by the DELIBERATE end
    (``close_reason`` in :data:`_FINISHED_CLOSE_REASONS`), no turn is still open,
    AND the newest completed turn ended cleanly. A ``closed`` lifecycle alone is
    not enough: the ``session/closed`` edge is also written on every process
    recycle (``reset``) and on ``destroy`` (including mid-turn), teardowns the tab
    and conversation outlive.

    The ``last_stop_reason`` guard is the third fail-safe, and it is the one a
    deliberate MID-TURN close needs: closing a tab with a turn in flight cancels
    ``slot.task``, and the cancelled turn's ``finally`` writes a real
    ``turn/completed`` (``stop_reason`` ``cancelled``/``failed``/``error:*``) that
    CLEARS ``turn_open`` -- so the ``turn_open`` guard above does not catch it, and
    a worker killed mid-turn would read ``closed`` ("finished, nothing to
    re-dispatch") when its work did not finish. A newest turn that did not end
    cleanly (:func:`turn_outcome` other than ``"ok"``) therefore reads ``lost``.
    A clean ``end_turn`` and the ``None`` of a tab closed before any turn ran both
    classify ``"ok"`` -- a session that genuinely finished (or never started work)
    stays ``closed``.

    A close whose reason is a recycle, a close with a turn still open, a newest
    turn that did not end cleanly, or any non-closed lifecycle all read as
    not-closed -> the caller reports ``lost``, the fail-safe direction.
    """
    if value.get("lifecycle") != "closed":
        return False
    if value.get("turn_open"):
        # A session cut off with a turn in flight did not finish, however it was
        # torn down -- the projection leaves the open turn set on a close for
        # exactly this reader to see (crew_log/projection.py::_status_render).
        return False
    if turn_outcome(value.get("last_stop_reason")) != "ok":
        # The newest completed turn did not end cleanly. A deliberate mid-turn
        # close cancels the running turn, whose ``finally`` records a
        # ``turn/completed`` with an abnormal ``stop_reason`` and clears
        # ``turn_open`` -- so this is the only guard that still sees the work was
        # cut off. ``None``/``end_turn`` are both ``"ok"``, so a clean finish and a
        # never-worked tab close stay ``closed``.
        return False
    return value.get("close_reason") in _FINISHED_CLOSE_REASONS


def _bounded_folder_id(value: object) -> str:
    """A persisted folder id, or ``""`` when it is longer than any real one."""
    text = str(value or "")
    return text if len(text) <= MAX_HISTORY_FOLDER_ID_CHARS else ""


def _bounded_status_title(value: object) -> str:
    """A display-safe title bounded before it is retained in a status row."""
    return sanitize_outbound(str(value or ""))[:MAX_SESSION_STATUS_TITLE_CHARS]


def _pending_approval_fields(state: "DashboardState", slot: Any) -> dict[str, Any]:
    """``{"pending_approval": True, "pending_approval_tool": ...}`` while *slot* waits.

    A turn parked on a tool approval keeps ``running`` true, so without this a
    creator polling the session sees ``working`` for the whole approval window.
    The answer is the slot projection's own ``pending_approval`` and
    ``pending_approval_info`` -- the fields the dashboard card reads -- so the
    two cannot disagree. ``to_dict`` walks the transcript, so it runs only once
    one of the two registries it consults has an open entry; an ordinary turn
    pays one dict scan and the response shape is unchanged.
    """
    futures = getattr(slot, "_approval_futures", None) or {}
    pending_for = getattr(state, "pending_coordinator_approvals", None)
    if not any(not fut.done() for fut in futures.values()) and not (
        callable(pending_for) and pending_for(slot.key)
    ):
        return {}
    projected = slot.to_dict()
    if not projected.get("pending_approval"):
        return {}
    fields: dict[str, Any] = {"pending_approval": True}
    tool = str((projected.get("pending_approval_info") or {}).get("tool") or "")
    if tool:
        fields["pending_approval_tool"] = redact_and_truncate(
            sanitize_outbound(tool), MAX_PENDING_APPROVAL_TOOL_CHARS
        )
    return fields


def _created_history_roster(
    state: "DashboardState", caller_key: str, caller_workspace: str
) -> tuple[dict[str, dict[str, Any]], str, int]:
    """Persisted birth metadata for sessions created by *caller_key*.

    This is the transcript-meta half of :func:`created_session_status`. It is a
    weaker source than the fenced crew log: ``created_by`` is already trusted by
    the member ownership boundary, but it is not gateway-authored lineage. Rows
    retain that distinction through their ``source`` value.

    The scan is read-only. ``list_sessions`` supplies the catalog and
    ``get_metadata_status`` supplies both the creator edge and a readability
    verdict for each candidate. A bad metadata row or a matching population past
    :data:`MAX_SESSION_STATUS_ROWS` makes the result ``incomplete`` rather than
    disappearing behind a claim of completeness. Only the bounded title needed
    by the response is retained; the full editable metadata dict is not.
    """
    log = state.conversation_log
    if log is None:
        return {}, "unreadable", 0
    try:
        sessions = log.list_sessions()
    except Exception:
        logger.debug("session history roster could not be listed", exc_info=True)
        return {}, "unreadable", 0

    rows: dict[str, dict[str, Any]] = {}
    incomplete = False
    omitted = 0
    for session in sessions:
        history_key = str(session.get("key", ""))
        slot_key = _recent_session_slot_name(history_key)
        if slot_key is None or slot_key == caller_key:
            continue
        try:
            meta, readable = log.get_metadata_status(history_key)
        except Exception:
            logger.debug("session history metadata could not be read", exc_info=True)
            incomplete = True
            continue
        if not readable:
            incomplete = True
            continue
        if str(meta.get("created_by", "")) != caller_key:
            continue
        if str(meta.get("workspace", "default")) != caller_workspace:
            continue
        if len(rows) >= MAX_SESSION_STATUS_ROWS:
            # Keep scanning only to report the exact overflow count. No metadata
            # or title beyond the cap is retained in this request.
            omitted += 1
            incomplete = True
            continue
        rows[slot_key] = {
            "title": _bounded_status_title(meta.get("title") or slot_key),
        }
    return rows, ("incomplete" if incomplete else "readable"), omitted


async def created_session_status(
    state: "DashboardState",
    *,
    caller_session_key: str,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """Every session this caller stood up, and what each one is doing now.

    The roster and the status come from THREE sources because none can answer
    every half. The crew log's session tree is durable and gateway-attested, while
    persisted history metadata records a birth before the first turn can write a
    tree edge. Live slots know exactly what is running but forget a session the
    moment it is gone. The union covers all three windows, and each row says which
    source placed it.

    ``status`` per row, and the five are distinct answers a patrol acts on
    differently:

    * ``working`` — a turn is in flight. Wait.
    * ``queued`` — idle, but messages are waiting to run. Also wait, but nothing is
      happening yet, so a steer would land on nothing.
    * ``idle`` — open and doing nothing. This is the one that needs a decision.
    * ``closed`` — the crew log names it, the dashboard does not hold it, and the
      log records a ``session/closed`` edge: a worker that FINISHED and closed its
      tab. Carries ``closed_at``. Ignore it; there is nothing here to message, and
      :func:`authorize_target` would answer ``target_not_found``.
    * ``lost`` — the crew log names it, the dashboard does not hold it, and no
      qualifying clean deliberate close was recorded: either no ``session/closed``
      edge at all, or the newest one is a ``reset``/``destroy`` teardown or a close
      whose last turn did not end cleanly — none of which means "finished, nothing
      to re-dispatch". Lost with the process that ran it (or its opener was retained
      out of the log). Re-dispatch it. ``closed`` and ``lost`` are the two fates the
      old single ``gone`` status could not tell apart, and they call for opposite
      actions.
    * ``unknown`` — history records the creator but neither a live slot nor an
      attested tree edge exists. The row proves the session was created without
      claiming whether it finished or was lost.

    READ-ONLY. The history catalog and metadata reads touch disk; callers keep the
    operation informational and expose their quality separately from the crew-log
    tree quality.

    The union is bounded at :data:`MAX_SESSION_STATUS_ROWS`, and that cut gets its
    OWN field: ``roster_omitted`` counts the rows dropped, zero when the whole
    union was retained. It is deliberately not expressed by degrading ``history``,
    which describes the transcript scan alone — a union cut is not a read fault in
    any source, and spelling it that way blames a read that completed while
    ``history_omitted`` still reports nothing cut.

    The ownership fence applies to the rows, not just to the verb: a fenced caller
    (a crew member, a cron, an agent-created session) sees the live sessions it
    created and nothing else, so this verb cannot become a way to enumerate the
    user's own sessions by their titles. A ``closed`` or ``lost`` row carries only
    a slot key the caller already knew, and no title, so it is listed either way.
    """
    deny = _deny_factory(caller_session_key=caller_session_key, operation="status", target="")
    caller_key = refuse_caller_identity(state, caller_session_key=caller_session_key, deny=deny)
    caller_slot = refuse_caller_surface(state, caller_key=caller_key, deny=deny)
    ownership_fenced = (
        _caller_is_ownership_fenced(state, caller_key) if caller_fenced is None else caller_fenced
    )
    tree_children, tree_state = _created_tree_roster(caller_key)

    # The first suspension in this verb, deliberately AFTER the complete
    # synchronous caller gate and fence resolution above. The route prewarms the
    # config cache immediately before entering this coroutine, so moving an await
    # into that window would let an intervening config edit put blocking config IO
    # back on the loop. The scan itself globs, stats and reads transcript metadata,
    # so run the complete operation in one worker-thread hop.
    caller_workspace = str(getattr(caller_slot, "workspace", "default"))
    history_children, history_state, history_omitted = await asyncio.to_thread(
        _created_history_roster, state, caller_key, caller_workspace
    )
    # The gate above RAN before the suspension, but its verdict does not survive
    # one: a channel mirror can be bound onto an already-open dashboard session
    # while this scan is on a worker thread -- the channel picker
    # (`messaging/session_resume.py`) and the Slack link route
    # (`slack/interactions.py`) both do it with no idle-slot requirement -- and
    # the rows below carry other sessions' TITLES, the names of the user's
    # private work. Published past a gate that passed, they reach that channel's
    # audience. So re-call the same gate on the same terms and refuse through the
    # same audited path, the way the queue drain re-checks mirror identity
    # between admission and delivery (`_probe_channel_mirror`) rather than
    # trusting its admission.
    #
    # Slot IDENTITY is deliberately NOT compared: `get_or_create_slot` returns the
    # registered slot for an existing key and the single `put_slot` publish runs
    # only where the key was absent, so no live key is rebound to a new object --
    # a check for it could never fire. WORKSPACE is compared, because that one is
    # reassigned on a live slot (`chat_handlers` commits one, `channel_slots`
    # adopts one from metadata) and it is the boundary every row below is filtered
    # on: a workspace that moved mid-scan would filter rows against a boundary the
    # history scan did not use.
    refuse_caller_surface(state, caller_key=caller_key, deny=deny)
    if str(getattr(caller_slot, "workspace", "default")) != caller_workspace:
        raise deny(
            "the calling session moved workspace while its roster was being read; " "call again",
            "caller_changed_mid_read",
        )

    # Live slot state stays on the event loop and is read only after the history
    # worker returns. Reading it inside the worker would let the result go stale
    # before these rows are built. This read only names the caller's own creations
    # (`_created_by`, not workspace), so it is not the workspace-boundary read --
    # that is `resolvable_keys`, built below AFTER the last suspension in this verb.
    live_children = broadcast_audience(state, caller_key)

    rows: list[dict[str, Any]] = []
    roster = set(tree_children) | set(history_children) | set(live_children)
    # The UNION cut is its own fact, with its own field and its own count. It must
    # not be signalled by degrading ``history_state``, which describes the
    # transcript scan alone: a union cut is not a read fault in any source, so that
    # spelling says two false things at once -- it blames a transcript read that
    # completed, and it leaves ``history_omitted`` reporting nothing cut while rows
    # are dropped. Neither is actionable, because re-reading history cannot recover
    # a row the union bound cut.
    #
    # Zero when nothing was cut, so a reader distinguishes "no overflow" from a
    # cut whose size it must know to judge the roster it was handed.
    roster_omitted = max(0, len(roster) - MAX_SESSION_STATUS_ROWS)
    retained = sorted(roster)[:MAX_SESSION_STATUS_ROWS]
    # A `gone` row -- the tree names it and the dashboard does not hold it -- splits
    # into `closed` and `lost`, and the two call for OPPOSITE actions: ignore a
    # worker that finished and closed its tab, re-dispatch one lost with its
    # process. The split is read from the ``session/closed`` edge the status fold
    # already projects, so it is a disk fold per gone slot and runs OFF the loop,
    # the same worker-thread discipline the history scan above keeps. Scoped to the
    # retained, tree-attested, now-absent slots so no fold is paid for a row that
    # will not carry the distinction.
    gone_slots = [key for key in retained if key in tree_children and state.get_slot(key) is None]
    gone_lifecycles = (
        await asyncio.to_thread(_gone_slot_lifecycles, gone_slots) if gone_slots else {}
    )
    # The gone fold above is the LAST suspension in this verb, and the live-row
    # containment must be computed after it, not before. A live worker can be moved
    # across the workspace boundary mid-fold -- `api_chat_slot_workspace` reassigns
    # `_slot.workspace` on a running slot with no idle requirement -- so a
    # `resolvable_keys` built before this await would admit a worker on its
    # pre-move workspace and then carry its `display_title` out of the caller's
    # workspace in the row loop below. So re-call the caller gate on the same terms
    # and REBUILD containment here, after the final await, exactly as the block
    # after the history scan does: the boundary every row is filtered on is read
    # from the live slots as they stand once nothing else can suspend.
    refuse_caller_surface(state, caller_key=caller_key, deny=deny)
    if str(getattr(caller_slot, "workspace", "default")) != caller_workspace:
        raise deny(
            "the calling session moved workspace while its roster was being read; " "call again",
            "caller_changed_mid_read",
        )
    # The SAME containment set `session_broadcast` resolves names against, resolved
    # here once and applied to every live row below. Built AFTER the gone fold's
    # await (and its re-check), so a worker moved across the boundary during that
    # fold is already excluded -- its `workspace` is re-read here as it now stands,
    # and the fail-safe follows: a row the moved worker would carry is dropped by
    # the `resolvable_keys` membership test below rather than leaked.
    resolvable_keys = {
        slot.key
        for slot in _broadcast_resolution_slots(
            state,
            caller_key=caller_key,
            caller_slot=caller_slot,
            ownership_fenced=ownership_fenced,
        )
    }
    for key in retained:
        slot = state.get_slot(key)
        from_tree = key in tree_children
        from_history = key in history_children
        sources = [
            source
            for source, present in (
                ("crew_log", from_tree),
                ("history", from_history),
                ("live", slot is not None),
            )
            if present
        ]
        if slot is None:
            if from_tree:
                # The attested tree still owns this status. History may corroborate
                # the birth, but it does not turn editable metadata into lineage.
                #
                # CLOSED vs LOST, read from the ``session/closed`` edge the status
                # fold projects: a worker that closed its tab after finishing is
                # ``closed`` and carries its ``closed_at``; one gone without a
                # recorded close -- lost with its process, or whose opener was
                # retained out of the log -- is ``lost``. Defaults to ``lost`` when
                # the fold was not made (a slot absent from the map), the fail-safe
                # reading a patrol re-dispatches rather than drops.
                lifecycle = gone_lifecycles.get(key, {"status": "lost"})
                gone_row: dict[str, Any] = {
                    "target": key,
                    "status": lifecycle["status"],
                    "source": "+".join(sources),
                }
                if lifecycle["status"] == "closed":
                    gone_row["closed_at"] = lifecycle.get("closed_at")
                rows.append(gone_row)
                continue
            if not from_history:
                # Neither tree nor history, and no live slot. A row reaches here
                # only because it was in ``retained`` -- read from the creator's
                # live children BEFORE the gone-fold await -- and its slot has since
                # gone: a worker the caller created that never took a first turn (so
                # no tree edge) and left no history row (the history scan filters on
                # workspace) can have its tab closed WHILE the fold runs on its
                # worker thread, landing here with ``slot is None`` and both durable
                # sources absent. Reading ``history_children[key]`` would raise
                # ``KeyError`` and fail the whole roster. Drop the row: the worker is
                # gone with no durable record to report, which is the fail-safe
                # reading -- the same "nothing to say about a slot that vanished with
                # no attested trace" the ``lost`` default elsewhere takes.
                continue
            meta = history_children[key]
            rows.append(
                {
                    "target": key,
                    "title": meta["title"],
                    "status": "unknown",
                    "source": "history",
                }
            )
            continue
        if slot.key not in resolvable_keys:
            # ONE predicate for both verbs. These rows carry `display_title`, which
            # for a channel-linked or mirrored session is derived from a conversation
            # other people are in, and a creator whose worker is linked LATER would
            # otherwise be handed that title on its next roster read. The row is
            # dropped rather than reported title-less for the reason the workspace
            # clause inside the predicate gives: `session_send` refuses this target
            # too (`linked_session_target`, `mirrored_target`, `ephemeral_target`,
            # `app_scoped_target`, `not_creator`), so a row here would be a listing
            # of work the caller cannot message.
            #
            # Membership in `_broadcast_resolution_slots` rather than a second copy
            # of its clauses, because one containment enforced in two places is one
            # containment that can differ between them: a clause restated here is a
            # clause that has to be restated again on every change to the predicate.
            continue
        queue_depth = len(slot._queue)
        running = bool(slot.running)
        rows.append(
            {
                "target": key,
                "title": _bounded_status_title(slot.display_title),
                "status": "working" if running else ("queued" if queue_depth else "idle"),
                "running": running,
                "queue_depth": queue_depth,
                **_pending_approval_fields(state, slot),
                "source": "+".join(sources),
            }
        )

    _audit(
        caller_session_key=caller_session_key,
        operation="status",
        slot_key=caller_key,
        outcome="allowed",
        detail={
            "rows": len(rows),
            "tree": tree_state,
            "history": history_state,
            "history_omitted": history_omitted,
            "roster_omitted": roster_omitted,
        },
    )
    return {
        "ok": True,
        "caller": caller_key,
        # How much of the durable roster this answer rests on. A caller that reads
        # only `sessions` and gets a short list cannot tell "you created three" from
        # "I could read three", and for a patrol deciding whether a worker was lost
        # those are opposite conclusions.
        "tree": tree_state,
        # History metadata is a separate, weaker roster source. Its quality must
        # not be folded into `tree`, whose value describes only the crew-log fold.
        "history": history_state,
        # Exact count of matching transcript rows not retained due to the bound.
        # Kept separate from ``history`` so callers can distinguish a read fault
        # from deliberate overflow while old clients still see ``incomplete``.
        "history_omitted": history_omitted,
        # Exact count of rows the UNION of the three sources lost to
        # ``MAX_SESSION_STATUS_ROWS``. Separate from ``history_omitted`` because it
        # is a different cut with a different remedy: this one is not a read fault
        # in any source, and no source's quality field describes it. Zero when the
        # whole union was retained.
        "roster_omitted": roster_omitted,
        "sessions": rows,
    }


def read_messages(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    limit: int = DEFAULT_READ_MESSAGES,
    since: int | None = None,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """Read *target*'s transcript tail plus enough state to poll it.

    ``next_since`` is the cursor to poll with; passing it back as ``since`` on the
    next call returns only what arrived in between, which is the whole
    wait → read poll loop. ``running`` says whether the target is still
    working, so a caller knows the difference between "nothing new yet" and
    "finished and idle".
    """
    if limit < 1 or limit > MAX_READ_MESSAGES:
        raise SessionControlError(
            f"limit must be between 1 and {MAX_READ_MESSAGES}", code="invalid_limit"
        )
    slot = authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        operation="read",
        precomputed_ownership_fenced=caller_fenced,
    )
    # The reader's own audience was recorded by the caller-side gate inside
    # ``authorize_target`` (:func:`record_audience_admission`), so a mirror gained
    # or retargeted between this read and the reply it feeds withholds the
    # reader's cross-surface publication. No per-verb stamp here: the gate is the
    # one point every verb passes, which is what keeps a read path from missing it.

    # Indexes are ABSOLUTE positions in the session, not offsets into the live
    # window. A slot keeps only the most recent ``_MAX_SLOT_MESSAGES`` in memory
    # and credits each trimmed row to a frozen-prefix counter, so window length
    # stops growing once trimming starts. A cursor derived from that length
    # would freeze at the cap and never see another reply; adding the
    # frozen-prefix count makes it monotonic for the session's whole life.
    raw_window = list(slot.messages)
    # The DURABLE frozen-prefix counter, never ``_disk_older_count``: that one
    # counts every trimmed row, transient ones included, while the positions
    # below are built over the durable rows the filter keeps. Basing on the
    # all-rows counter shifted every position as soon as a transient row was
    # trimmed, and a ``since`` read then served a durable message the caller
    # already had. ``_disk_older_durable_count`` counts exactly the rows the
    # ``TRANSIENT_ROLES`` filter below would have kept, so the two spaces agree
    # for the session's whole life. Defensive ``getattr`` matches how the
    # existing code reads ``_disk_older_count``: a slot restored by an older
    # build simply has no trimmed prefix yet.
    base = int(getattr(slot, "_disk_older_durable_count", 0) or 0)
    # Stop the cursor before the streaming tail (see ``TRANSIENT_ROLES``): those
    # rows are deleted when the segment flushes, so a cursor past them would sit
    # beyond the list that replaces them and never return the finished reply.
    messages = [m for m in raw_window if m.get("role") not in TRANSIENT_ROLES]
    durable_end = len(messages)
    total = base + durable_end
    if since is not None:
        if since < 0:
            raise SessionControlError("since must be >= 0", code="invalid_since")
        if since < base:
            # The cursor points into the trimmed prefix: those rows exist only
            # on disk now, and this read serves the in-memory window. Starting
            # at ``base`` instead would silently skip every row in
            # ``[since, base)`` — a poller that lagged a whole window behind
            # would lose messages with nothing in the response saying so. The
            # refusal is loud and the tail-read fallback recovers, exactly like
            # the past-the-end case below.
            raise SessionControlError(
                "this session is long enough that the messages at your cursor "
                "have been trimmed from memory — read without `since` to get "
                "the latest messages",
                status=409,
                code="cursor_unavailable",
            )
        # A cursor PAST the end is the remaining inexact case, and it is not the
        # same as a stale one: rewind and regenerate shrink a transcript, so
        # `total` can move backwards under a caller that is still holding the old
        # position. Clamping it to `total` would start the read at the end and
        # silently skip every replacement row written below the old cursor, with
        # nothing in the response saying so. So this refuses loudly rather than
        # answer approximately. Reads without `since` are unaffected.
        if since > total:
            raise SessionControlError(
                "this session is shorter than your cursor — it was rewound or "
                "regenerated, so earlier positions no longer line up — read "
                "without `since` to get the latest messages",
                status=409,
                code="cursor_unavailable",
            )
        start = since
        # Positions are absolute; the window slice below is offset-relative, so
        # subtract the durable prefix that is no longer in memory.
        offset = start - base
    else:
        # A tail read never refuses (only a `since` below the trimmed prefix or
        # past the end is), and the two spaces come apart here: slice the
        # in-memory window by OFFSET, but report the index in ABSOLUTE terms so
        # the number still means "position in the session". Conflating them
        # returned an empty window, because `total` counts the frozen prefix
        # the list does not hold.
        offset = max(0, durable_end - limit)
        start = base + offset
    window = messages[offset:][:limit]

    out: list[dict[str, Any]] = []
    for offset, msg in enumerate(window):
        content = str(msg.get("content", "") or "")
        # ``redact_and_truncate`` scans the COMPLETE text before slicing. Cutting
        # first would split a credential straddling the boundary into a prefix
        # that no longer matches the scanner, so the fragment would ship.
        emitted = redact_and_truncate(content, MAX_READ_CONTENT_CHARS)
        row: dict[str, Any] = {
            "index": start + offset,
            "role": str(msg.get("role", "") or ""),
            "content": emitted,
            "ts": str(msg.get("ts", "") or ""),
        }
        if len(content) > MAX_READ_CONTENT_CHARS:
            row["truncated"] = True
        out.append(row)

    _audit(
        caller_session_key=caller_session_key,
        operation="read",
        slot_key=slot.key,
        outcome="allowed",
        detail={"returned": len(out)},
    )
    return {
        "ok": True,
        "target": slot.key,
        "title": sanitize_outbound(slot.display_title),
        # Busy means "more output is coming", which is exactly what a poller needs
        # to decide whether to wait.
        "running": bool(slot.running),
        # True when the target is mid-reply: rows exist that the cursor
        # deliberately does not cover yet, so "nothing new" here does not mean
        # "nothing happening".
        **({"streaming": True} if durable_end < len(raw_window) else {}),
        "queue_depth": len(slot._queue),
        # A turn parked on a tool approval is still ``running``; this says it is
        # waiting on a person rather than working.
        **_pending_approval_fields(state, slot),
        # The model the target's turns use, and a session_set_model pick still
        # waiting for its next turn, so a caller can see whether its pick took.
        # Redacted: the owner's picker stores whatever string it is given.
        # The served model when the backend has reported one (it reflects an
        # inherited default or an active fallback), otherwise the pin.
        "model": redact(slot.served_model or slot.model or ""),
        **_pending_pick_view(slot._pending_model_pick),
        "reasoning_effort": slot.reasoning_effort or "",
        "total": total,
        # The cursor to poll with next. This is NOT `total`: when more than
        # `limit` rows are new, the window stops short of the end, and a caller
        # that polled `since=total` would jump the gap and never see the rows in
        # between. `next_since` is the absolute position just past the last row
        # actually returned, so consecutive polls cover every row exactly once.
        # `total` stays in the response as the backlog depth — the difference
        # from `next_since` is how far behind the caller still is.
        #
        # Returned on trimmed sessions too: positions are based on the
        # durable-only prefix counter, so they stay exact after rows age into
        # the frozen prefix. The refusals above cover the cases that genuinely
        # cannot be exact (a cursor under the trimmed prefix, or past the end of
        # a rewound transcript).
        "next_since": start + len(out),
        "messages": out,
    }


def _summary_text(value: object) -> str:
    """One summary string, redacted and then cut to ``MAX_SUMMARY_CHARS``."""
    text = value if isinstance(value, str) else ""
    # Redact the whole string first (the helper's own order), then cut, so the
    # marker reflects what the redacted text lost.
    redacted = redact_and_truncate(text, len(text) * 4 + 256)
    if len(redacted) <= MAX_SUMMARY_CHARS:
        return redacted
    return redacted[:MAX_SUMMARY_CHARS] + " …[truncated]"


def _bounded_summary(payload: dict) -> dict[str, Any]:
    """The fields a patrol reads, bounded, with a count of what each cut dropped.

    Redaction runs on every emitted string through ``redact_and_truncate``, the
    sink ``read_messages`` uses: the sidecar was redacted when written, and
    re-redacting on the way out covers a scrubber rule added since.
    """
    raw_intents = [i for i in (payload.get("intents") or []) if isinstance(i, dict)]
    intents: list[dict[str, Any]] = []
    for intent in raw_intents[:MAX_SUMMARY_INTENTS]:
        progress = [p for p in (intent.get("progress") or []) if isinstance(p, str)]
        steps = [st for st in (intent.get("next_steps") or []) if isinstance(st, dict)]
        intents.append(
            {
                "title": _summary_text(intent.get("title")),
                # The panel's single word, not the raw progress axis: a
                # ``completed`` intent that was never verified reads
                # ``needs-you``, which is the case a patrol most needs to see.
                "state": _summary_text(
                    intent.get("state")
                    or derive_state(str(intent.get("status") or ""), intent.get("verified"))
                ),
                # The latest progress is what a patrol needs; the next steps in
                # the order the summarizer ranked them.
                "progress": [_summary_text(p) for p in progress[-MAX_SUMMARY_ITEMS:]],
                "progress_omitted": max(0, len(progress) - MAX_SUMMARY_ITEMS),
                "next_steps": [_summary_text(st.get("what")) for st in steps[:MAX_SUMMARY_ITEMS]],
                "next_steps_omitted": max(0, len(steps) - MAX_SUMMARY_ITEMS),
            }
        )
    notes = [n for n in (payload.get("constraints") or []) if isinstance(n, str)]
    return {
        "intents": intents,
        "intents_omitted": max(0, len(raw_intents) - MAX_SUMMARY_INTENTS),
        "constraints": [_summary_text(n) for n in notes[:MAX_SUMMARY_NOTES]],
        "constraints_omitted": max(0, len(notes) - MAX_SUMMARY_NOTES),
    }


async def read_summary(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """Return *target*'s cached intent summary, the one the side panel shows.

    Authorized by the gate :func:`read_messages` uses (``authorize_target``):
    the summary is derived from the transcript, so a caller that may not read
    the transcript may not read its digest either. ``operation`` only labels
    the audit line, so a refusal reads as ``session_control.summary``.

    A cache read only. It never generates, so it never spends a model call:
    summaries are written at turn end by the background pass, or on the panel's
    explicit POST, and neither is reachable from here. Gated on
    ``session_summary.enabled`` like the panel GET, so switching the feature
    off stops serving sidecars written while it was on.

    The ``authorize_target`` gate runs before the first suspension, so the
    handler's ``prewarm_enabled_check`` stays valid across it.
    """
    # Late import: chat_summary pulls in the chat package, which imports this
    # module at load time.
    from kiro_crew.dashboard.chat_summary import read_cached_intent_summary

    def _authorize(*, recheck: bool) -> "_ChatSlot":
        return authorize_target(
            state,
            caller_session_key=caller_session_key,
            target=target,
            operation="summary",
            skip_enabled_check=recheck,
            precomputed_ownership_fenced=caller_fenced,
        )

    slot = _authorize(recheck=False)
    running = bool(slot.running)

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    enabled = bool(cfg.session_summary.enabled)
    payload: dict | None = None
    stale = False
    log = state.conversation_log
    if enabled and log is not None:
        payload, stale = await read_cached_intent_summary(log, slot)
    # Both reads above suspend, the sidecar read for as long as the transcript
    # lock's acquire ceiling. The caller or the target can gain a channel link
    # or a mirror in that window, so the gate runs again, synchronously, before
    # anything is returned, and the answer must still be the same slot.
    if _authorize(recheck=True) is not slot:
        raise SessionControlError(
            "the target session was replaced while its summary was read; try again",
            status=409,
            code="target_replaced",
        )
    bounded = _bounded_summary(payload or {})

    _audit(
        caller_session_key=caller_session_key,
        operation="summary",
        slot_key=slot.key,
        outcome="allowed",
        detail={
            "enabled": enabled,
            "present": payload is not None,
            "intents": len(bounded["intents"]),
        },
    )
    return {
        "ok": True,
        "target": slot.key,
        "title": sanitize_outbound(slot.display_title),
        "running": running,
        "enabled": enabled,
        "stale": stale,
        "generated_at": (payload or {}).get("generated_at"),
        **bounded,
    }


# --------------------------------------------------------------------------- #
# The facade over ``session_control_parts``
# --------------------------------------------------------------------------- #
# ``kiro_crew.dashboard.session_control`` is the import path and the patch seam; the
# code it re-exports lives in the owner modules below, one responsibility each. The owners
# import each other's names, lowest layer first, so a name a test patches here is read in
# more than one module. ``_facade_mirror`` keeps that patch whole: a write or delete of a
# name through this module lands in every owner that binds it (and here, when this module
# binds it too), and a name this module does not bind is read from the first owner that
# does. No owner imports this module.
#: Owner modules, lowest layer first: the first one binding a name answers a read of it.
_OWNERS = (
    "kiro_crew.dashboard.session_control_parts.caller_access",
    "kiro_crew.dashboard.session_control_parts.delivery_containment",
    "kiro_crew.dashboard.session_control_parts.target_authorization",
    "kiro_crew.dashboard.session_control_parts.turn_control",
    "kiro_crew.dashboard.session_control_parts.model_switch",
    "kiro_crew.dashboard.session_control_parts.session_titles",
    "kiro_crew.dashboard.session_control_parts.session_create",
    "kiro_crew.dashboard.session_control_parts.session_tree",
    "kiro_crew.dashboard.session_control_parts.session_status",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Owner-bound name this module does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)

# Installed last, so the forwarding is live for every caller but never runs while this
# module is still binding its own names.
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

# Derived, not listed: what ``from session_control import *`` binds is every public name
# of the family, as it was when the family was one module.
__all__ = sorted(name for name in set(globals()) | set(_EXPORTS) if not name.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name this module forwards, for type checkers
    from kiro_crew.dashboard.session_control_parts.caller_access import (  # noqa: F401
        APP_CRON_OWNER_PREFIX,
        CRON_LINK_PREFIX,
        CRON_SLOT_PREFIX,
        WORKFLOW_SLOT_PREFIX,
        _app_owned_cron_refusal,
        _audit_denials,
        _channel_link_of,
        _cron_caller,
        _delegation_lineage_fenced,
        _member_bypass,
        _member_caller,
        _sel_off_loop,
        _store_is_member_owned,
        member_admitted_to_scoped_surface,
        member_dispatch_enabled,
        member_owns_slot,
        session_control_enabled,
    )
    from kiro_crew.dashboard.session_control_parts.delivery_containment import (  # noqa: F401
        _AUDIENCE_CONSTRAINTS,
        _CONTAINMENT_CHANGE_LABELS,
        _NON_CONSTRAINT_KEYS,
        _SEND_DISCARD_REASONS,
        AUDIENCE_ADMISSION_KEY_PREFIX,
        CHANNEL_RECIPIENT_META_KEY,
        MIRROR_IDENTITY_SEPARATOR,
        QUEUED_CONTAINMENT_META_KEY,
        SEND_DISCARD_HARD_STOP,
        SEND_DISCARD_QUEUE_CANCEL,
        SEND_DROP_EXCERPT_CHARS,
        SEND_ORIGIN_META_KEY,
        _audit_queue_drain,
        _in_runner_turn,
        _meta_from_probe,
        _mirror_identity_of,
        _probe_channel_mirror,
        _read_mirror_binding,
        _send_origin_field,
        _send_origin_recipient,
        _slack_thread_of,
        _snapshot_from_probe,
        audit_queued_allow,
        audit_queued_drop,
        channel_recipient_meta,
        channel_recipient_of,
        describe_containment_change,
        mirror_audience,
        notify_channel_recipient_dropped,
        notify_send_origin_discarded,
        notify_send_origin_dropped,
        publication_withheld,
        record_audience_admission,
        send_drop_excerpt,
        send_origin_slot,
        send_origin_tab,
    )
    from kiro_crew.dashboard.session_control_parts.model_switch import (  # noqa: F401
        _JEV_ROUTE_SPELLINGS,
        PendingModelPick,
        _another_alias_is_mid_turn,
        _caller_tab_id,
        _pick_audit_fields,
        _pick_fields,
        _target_busy_error,
        apply_pending_model_pick,
        arm_model_switch_reset,
        effort_commit_needs_reset,
        legacy_effort_base,
        model_switch_deferred,
        set_model_target,
    )
    from kiro_crew.dashboard.session_control_parts.session_create import (  # noqa: F401
        _dispatching_crewmate,
        _inherited_pin_foreign_to_child,
        _model_pin_refusal,
        _record_spawn,
        create_session,
    )
    from kiro_crew.dashboard.session_control_parts.session_titles import (  # noqa: F401
        _ALLOWED_FORMAT_CHARS,
        _BIDI_CONTROLS,
        _BLANK_GLYPHS,
        _MAX_RENAME_TITLE_CHARS,
        _admit_generate_title,
        _clean_new_session_title,
        _clean_rename_title,
        _is_allowed_format_char,
        _parse_session_color,
        _refuse_hidden_title_characters,
        _shows_nothing,
        generate_title_target,
        rename_target,
        set_color_target,
    )
    from kiro_crew.dashboard.session_control_parts.session_tree import (  # noqa: F401
        _MAX_FORK_TITLE_CHARS,
        _TREE_APPEND_TIMEOUT,
        _fork_refusal,
        _freshest_sid,
        _live_sid_of,
        _recorded_sid_of,
        _refuse_if_append_pending,
        _replay_pending,
        _slot_object,
        _slot_opened_sid,
        _target_replaced,
        _tree_append_landed,
        _tree_append_waiter,
        _tree_mutation_lock,
        _tree_mutation_locks,
        _tree_pending_slots,
        adopt_target,
        fork_session,
        release_target,
    )
    from kiro_crew.dashboard.session_control_parts.target_authorization import (  # noqa: F401
        MIRROR_PEER_NOT_ON_RECORD,
        NOT_THE_SOLE_OWNER,
        ORIGIN_NOT_ON_RECORD,
        OWNER_DM_CONDUCTOR_SURFACES,
        SLACK_THREAD_BESIDE,
        Deny,
        OwnerDmVerdict,
        _owner_dm_clauses,
        _refuse_ineligible_creator,
        _refuse_moved_caller_identity,
        _sole_owner,
        judge_owner_dm,
        owner_dm_refusal,
        session_owner_dm_refusal,
    )
    from kiro_crew.dashboard.session_control_parts.turn_control import (  # noqa: F401
        _continue_turn_of_target,
        _reload_busy_error,
        _reload_route_refusal,
        close_target,
        continue_target,
        end_wait_target,
        reload_target,
        retry_target,
        stop_target,
    )


if TYPE_CHECKING:  # names this module re-exported from other modules
    from kiro_crew import model_registry  # noqa: F401
    from kiro_crew import model_scope  # noqa: F401
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS  # noqa: F401
    from kiro_crew.agent_sdk.capabilities import MODEL_NAMESPACE_ACP  # noqa: F401
    from kiro_crew.agent_sdk.capabilities import capabilities_for  # noqa: F401
    from kiro_crew.agent_sdk.provider_identity import is_claude_code  # noqa: F401
    from kiro_crew.config.loader import _workspace_name_for_dir  # noqa: F401
    from kiro_crew.config.loader import default_project_dir  # noqa: F401
    from kiro_crew.config.loader import member_template_id  # noqa: F401
    from kiro_crew.config.loader import resolve_agent_bindings  # noqa: F401
    from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG  # noqa: F401
    from kiro_crew.config.sections import DEFAULT_MODEL  # noqa: F401
    from kiro_crew.config.sections import KiroCrewAgentConfig  # noqa: F401
    from kiro_crew.config.sections import coerce_effort  # noqa: F401
    from kiro_crew.config.sections import worker_session_defaults  # noqa: F401
    from kiro_crew.dashboard.chat_folders import _folder_declared_project  # noqa: F401
    from kiro_crew.dashboard.chat_folders import _resolve_folder_project_dir  # noqa: F401
    from kiro_crew.dashboard.chat_fork import _FORK_DIRECTION_HEAD  # noqa: F401
    from kiro_crew.dashboard.chat_fork import ForkResult  # noqa: F401
    from kiro_crew.dashboard.chat_fork import ForkSource  # noqa: F401
    from kiro_crew.dashboard.chat_fork import fork_slot  # noqa: F401
    from kiro_crew.dashboard.chat_fork import resolve_fork_source  # noqa: F401
    from kiro_crew.dashboard.chat_persistence import (  # noqa: F401
        _TRANSIENT_ROLES as _PERSISTENCE_TRANSIENT_ROLES,
    )
    from kiro_crew.dashboard.chat_title import apply_manual_title  # noqa: F401
    from kiro_crew.dashboard.chat_title import apply_regenerated_title  # noqa: F401
    from kiro_crew.dashboard.chat_title import generate_manual_title  # noqa: F401
    from kiro_crew.dashboard.chat_utils import _normalize_model  # noqa: F401
    from kiro_crew.dashboard.chat_utils import drained_to_thread  # noqa: F401
    from kiro_crew.dashboard.chat_utils import effective_session_key  # noqa: F401
    from kiro_crew.dashboard.create_rate_limit import GENERATE_TITLE  # noqa: F401
    from kiro_crew.dashboard.create_rate_limit import WINDOW_SECS  # noqa: F401
    from kiro_crew.dashboard.create_rate_limit import has_create_budget  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import _scrub_text  # noqa: F401
    from kiro_crew.dashboard.state import SlotOrigin  # noqa: F401
    from kiro_crew.dashboard.state import load_window_off_loop  # noqa: F401
    from kiro_crew.dashboard.stop_retry import allow_escalation  # noqa: F401
    from kiro_crew.effort import EFFORT_LEVELS  # noqa: F401
    from kiro_crew.effort import is_valid_effort  # noqa: F401
    from kiro_crew.execution_context import ExecutionContext  # noqa: F401
    from kiro_crew.execution_context import MemoryStoreRef  # noqa: F401
    from kiro_crew.execution_context import bind_session_execution  # noqa: F401
    from kiro_crew.execution_context import member_config_for_id  # noqa: F401
    from kiro_crew.execution_context import read_session_execution  # noqa: F401
    from kiro_crew.execution_context import read_vouched_session_execution  # noqa: F401
    from kiro_crew.execution_context import refresh_vouched_session_execution  # noqa: F401
    from kiro_crew.execution_context import resolve_member_execution  # noqa: F401
    from kiro_crew.execution_context import revouch_at_verified_admission  # noqa: F401
    from kiro_crew.history import metadata_now_iso  # noqa: F401
    from kiro_crew.history import transcript_stem  # noqa: F401
    from kiro_crew.llm_helpers import slot_switch_session_lock  # noqa: F401
    from kiro_crew.members import select_provider_backend  # noqa: F401
    from kiro_crew.memory_stores import named_store_or_empty  # noqa: F401
    from kiro_crew.messaging.link import CHAT_TYPE_DIRECT  # noqa: F401
    from kiro_crew.messaging.link import ChannelLink  # noqa: F401
    from kiro_crew.messaging.link import parse_session_key  # noqa: F401
    from kiro_crew.messaging.transport import DM_TARGET_PREFIX  # noqa: F401
    from kiro_crew.messaging.transport import sole_direct_target  # noqa: F401
    from kiro_crew.terminal_safe import _is_invisible  # noqa: F401
    from kiro_crew.validation import _MODEL_NAME_RE  # noqa: F401
    from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN  # noqa: F401
    from kiro_crew.validation import MAX_SHORT_STRING  # noqa: F401
