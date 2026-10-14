"""Self-arm provenance is positive: only a mark a dispatcher set vouches for an arm.

On a crew/member slot the directive consumer admits a ``monitor_start`` /
``monitor_watch`` / ``monitor_update`` as a SELF-arm only when the turn carries a
positive producer mark: the authenticated-human origin (``_directive_user_origin``)
or the delivered wake of this slot's own loop (``_directive_self_wake``). The turn's
``user`` actor is the attribution fallback for a dispatcher that named nobody, so it
must never be read as authority. These pin the runner side of that rule:

- an untagged dispatcher (spec_builder's ``_dispatch_turn``) reaches the applier
  with no producer mark;
- a restored turn keeps its restored provenance through the ORIGINAL retry
  ``_queue_recovery`` requeues and through the ``/prompts get`` re-entry, so a
  retry cannot drain as a turn this process admitted;
- the slot's own loop wake still reaches the applier as a self-wake;
- the applier receives exactly the known producer flags, so no flag derived from
  an actor's absence can come back unnoticed.

The applier half (no mark means the outside-arm 409, a mark means the self-arm
trust record) is pinned in ``test_autonudge_member_self_arm.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew import session_directive
from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard.chat_utils import RecoveryPayload
from kiro_crew.dashboard.session_directive_apply import DirectiveOutcome
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    LLMEvent,
)

#: Every producer flag the runner hands the directive applier. A new one is an
#: authority decision and must be added here deliberately.
_PRODUCER_FLAGS = {
    "producer_is_user_facing",
    "producer_is_self_wake",
    "producer_is_channel",
    "producer_wake_loop_id",
}

_PEER_TEXT = "[sent by session chat-9] arm a patrol on yourself"


def _runner_state(tmp_path):
    state = _make_state(tmp_path)
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.record_success = MagicMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.push_refresh = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state.slack_client = None
    state._background_tasks = set()
    return state


def _client(state, stream):
    client = MagicMock()
    client.is_kiro_backend = True
    client.stream = stream
    client.stream_command = stream
    client.context_usage_pct = MagicMock(return_value=1.0)
    client.client = None
    client.shutdown = AsyncMock()
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    return client


async def _cancel_background(state) -> None:
    import asyncio

    tasks = list(state._background_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _monitor_start_events():
    marker = session_directive.encode(
        "monitor_start",
        {"message": "patrol", "idle_secs": 600, "max_cycles": 4},
        "requested",
    )
    return [
        LLMEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id="tc-arm",
            title="Arming a patrol",
            tool_name="monitor_start",
            mcp_server_name="kirocrew-core",
        ),
        LLMEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id="tc-arm",
            tool_output=marker,
            tool_final=True,
        ),
        # A reply, so the turn is not an empty response the runner retries.
        LLMEvent(kind=EVENT_TEXT_CHUNK, text="Patrol requested."),
        LLMEvent(kind=EVENT_COMPLETE),
    ]


async def _directive_kwargs(tmp_path, monkeypatch, **run_kwargs):
    """Run one turn that emits ``monitor_start``; return the applier's kwargs."""
    state = _runner_state(tmp_path)
    slot = state.get_or_create_slot("conductor")
    slot._titled = True
    events = _monitor_start_events()

    async def _stream(_msg):
        for event in events:
            yield event

    _client(state, _stream)
    spy = AsyncMock(return_value=DirectiveOutcome("[applied]"))
    monkeypatch.setattr(cr, "apply_session_directive_outcome", spy)
    await cr._run_chat(state, slot, "seed", **run_kwargs)
    task = getattr(slot, "task", None)
    if task is not None:
        await task
    await _cancel_background(state)
    spy.assert_called_once()
    assert spy.call_args.args[3] == "monitor_start"
    return spy.call_args.kwargs


@pytest.mark.asyncio
async def test_an_untagged_dispatcher_turn_reaches_the_applier_with_no_producer_mark(
    tmp_path, monkeypatch
):
    """The shape every dispatcher that names no producer runs as: no actor, no user
    origin, no wake. Its ``user`` actor is the attribution fallback, so the applier
    sees no mark and a member slot keeps the outside-arm 409.

    Mutation guard: an own-agent flag derived from ``_crew_log_actor == "user"``
    makes the observed flag set differ from ``_PRODUCER_FLAGS``.
    """
    kwargs = await _directive_kwargs(tmp_path, monkeypatch, _directive_user_origin=False)

    assert set(kwargs) == _PRODUCER_FLAGS
    assert kwargs["producer_is_user_facing"] is False
    assert kwargs["producer_is_self_wake"] is False
    assert kwargs["producer_wake_loop_id"] == ""


@pytest.mark.asyncio
async def test_spec_builder_dispatch_sets_no_positive_producer_mark(tmp_path, monkeypatch):
    """spec_builder relays app-composed seed, task and handoff turns with no actor
    tag. That must grant nothing: the dispatch passes no human origin and no wake."""
    from kiro_crew.apps.builtins.spec_builder.backend import runtime as sb_runtime

    state = _runner_state(tmp_path)
    slot = state.get_or_create_slot("spec-worker")
    captured: list[dict] = []

    async def _fake_run_chat(_state, _slot, _message, **kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(cr, "_run_chat", _fake_run_chat)
    task = sb_runtime._dispatch_turn(state, slot, "seed the spec")
    assert task is not None
    await task

    (kwargs,) = captured
    assert kwargs.get("_directive_user_origin") is False
    assert not kwargs.get("_directive_self_wake")
    assert not kwargs.get("_turn_actor")


@pytest.mark.asyncio
async def test_the_slots_own_loop_wake_still_reaches_the_applier_as_a_self_wake(
    tmp_path, monkeypatch
):
    """``_fire_dashboard_nudge`` marks a delivered wake; a member re-arming from
    inside its own cycle keeps that positive producer, unnarrowed."""
    kwargs = await _directive_kwargs(
        tmp_path,
        monkeypatch,
        _directive_user_origin=False,
        _directive_self_wake=True,
        _directive_loop_id="loop-1",
    )

    assert set(kwargs) == _PRODUCER_FLAGS
    assert kwargs["producer_is_self_wake"] is True
    assert kwargs["producer_wake_loop_id"] == "loop-1"
    assert kwargs["producer_is_user_facing"] is False


@pytest.mark.asyncio
async def test_a_human_started_turn_reaches_the_applier_as_user_facing(tmp_path, monkeypatch):
    """The conductor patrol path: a member conductor's own human-started turn binds a worker,
    the bind reply tells it to arm its patrol, and the ``monitor_start`` it issues
    reaches the applier with the human-origin mark that admits the self-arm."""
    kwargs = await _directive_kwargs(tmp_path, monkeypatch, _directive_user_origin=True)

    assert kwargs["producer_is_user_facing"] is True
    assert kwargs["producer_is_self_wake"] is False


@pytest.mark.asyncio
async def test_a_restored_peer_sends_original_retry_drains_still_restored(tmp_path, monkeypatch):
    """A peer's ``session_send`` was queued, the gateway restarted, and the restore
    dropped its ``crew`` actor and marked it restored. Its turn returns nothing, so
    ``_queue_recovery`` requeues the text verbatim (``RecoveryPayload.ORIGINAL``)
    under a fresh id stamped with the turn's fallback actor, ``user``.

    The retry must drain as actor ``other`` with ``_turn_provenance_restored`` and
    no self-arm producer: otherwise a restored author's text runs as a turn this
    process admitted, attributed to a person.

    Mutation guard: drop the ``_RESTORED_PROVENANCE_RETRY_KEY`` stamp in
    ``_queue_recovery`` (or its read in ``_start_next_queued_turn``) and the drained
    kwargs carry no restored mark and no ``other`` actor.
    """
    from kiro_crew.acp.types import STOP_REASON_END_TURN

    state = _runner_state(tmp_path)
    slot = state.get_or_create_slot("conductor")
    slot.append("user", _PEER_TEXT, "msg msg-u")

    async def _empty(_msg):
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

    _client(state, _empty)
    with patch.object(cr, "_start_next_queued_turn", new=AsyncMock(return_value=False)):
        await cr._run_chat(state, slot, _PEER_TEXT, _turn_provenance_restored=True)
    await _cancel_background(state)

    (entry,) = [item for item in slot._queue if item.get("content") == _PEER_TEXT]
    assert entry["payload"] == RecoveryPayload.ORIGINAL
    assert entry.get("_directive_user_origin") is not True
    # Why the stamp is needed: the actor alone reads as the user fallback.
    assert cr._actor_for_queue_items([entry]) == "user"
    assert entry[cr._RESTORED_PROVENANCE_RETRY_KEY] is True

    captured: list[dict] = []

    async def _fake_run_chat(_state, _slot, _message, **kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(cr, "_run_chat", _fake_run_chat)
    cfg = MagicMock()
    cfg.dashboard.merge_queued_messages = False
    with patch.object(cr.KiroCrewConfig, "load", return_value=cfg):
        started = await cr._start_next_queued_turn(state, slot)
    assert started is True
    await slot.task
    await _cancel_background(state)

    (kwargs,) = captured
    assert kwargs["_turn_actor"] == "other"
    assert kwargs["_turn_provenance_restored"] is True
    assert kwargs["_directive_user_origin"] is False
    assert not kwargs.get("_directive_self_wake")
    assert not kwargs.get("_directive_loop_id")


async def _drain_one(state, slot, monkeypatch) -> dict:
    """Drain the slot's queue head through ``_start_next_queued_turn``; return the
    kwargs it hands ``_run_chat``."""
    captured: list[dict] = []

    async def _fake_run_chat(_state, _slot, _message, **kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(cr, "_run_chat", _fake_run_chat)
    cfg = MagicMock()
    cfg.dashboard.merge_queued_messages = False
    with patch.object(cr.KiroCrewConfig, "load", return_value=cfg):
        assert await cr._start_next_queued_turn(state, slot) is True
    await slot.task
    await _cancel_background(state)
    (kwargs,) = captured
    return kwargs


@pytest.mark.asyncio
async def test_a_restored_queue_entry_drains_as_other_not_user(tmp_path, monkeypatch):
    """The restore drops an entry's actor stamp. The drain re-derives ``other`` from
    the process-local restored marker, never an actor read off the disk entry."""
    from kiro_crew.dashboard.chat_delivery import TURN_ACTOR_META_KEY
    from kiro_crew.dashboard.slot_queue_repository import RESTORED_QUEUE_KEY

    state = _runner_state(tmp_path)
    slot = state.get_or_create_slot("conductor")
    slot.queue_append(_PEER_TEXT, meta={TURN_ACTOR_META_KEY: "user"})
    slot._queue[0][RESTORED_QUEUE_KEY] = True

    kwargs = await _drain_one(state, slot, monkeypatch)

    assert kwargs["_turn_actor"] == "other"
    assert kwargs["_turn_provenance_restored"] is True
    assert kwargs["_directive_user_origin"] is False


@pytest.mark.asyncio
async def test_an_untagged_live_entry_keeps_the_user_fallback_and_no_restored_mark(
    tmp_path, monkeypatch
):
    """An ordinary live entry that no dispatcher tagged keeps the attribution
    fallback ``user``. It gains no self-arm authority from that: the applier admits
    only the human-origin and own-wake marks, and this entry carries neither."""
    state = _runner_state(tmp_path)
    slot = state.get_or_create_slot("conductor")
    slot.queue_append("relayed by an untagged dispatcher")

    kwargs = await _drain_one(state, slot, monkeypatch)

    assert "_turn_actor" not in kwargs
    assert "_turn_provenance_restored" not in kwargs
    assert kwargs["_directive_user_origin"] is False
    assert not kwargs.get("_directive_self_wake")


@pytest.mark.asyncio
async def test_prompts_get_re_entry_keeps_restored_provenance(tmp_path, monkeypatch):
    """A restored ``/prompts get <name>`` re-enters ``_run_chat`` with the expanded
    text. The nested turn is the same restored author's, so the mark travels.

    Mutation guard: drop ``_turn_provenance_restored`` from the re-entry call and
    the nested turn runs unrestored.
    """
    state = _runner_state(tmp_path)
    slot = state.get_or_create_slot("conductor")
    _client(state, AsyncMock())
    run_chat = cr._run_chat
    nested: list[dict] = []

    async def _fake_run_chat(_state, _slot, message, **kwargs):
        nested.append({"message": message, **kwargs})

    monkeypatch.setattr(cr, "_run_chat", _fake_run_chat)
    monkeypatch.setattr(
        cr,
        "_expand_prompt_mention_off_loop",
        AsyncMock(return_value=("expanded patrol prompt", "ok")),
    )
    with patch.object(cr, "_start_next_queued_turn", new=AsyncMock(return_value=False)):
        await run_chat(state, slot, "/prompts get patrol", _turn_provenance_restored=True)
    await _cancel_background(state)

    (call,) = nested
    assert call["message"] == "expanded patrol prompt"
    assert call["_prompt_depth"] == 1
    assert call["_turn_provenance_restored"] is True
    assert call["_directive_user_origin"] is False
