"""``session_handoff``: moving WHO MAY DRIVE a session, and nothing else with it.

The ownership fence admits the session that CREATED a target. That is the whole reason
a lead cannot put the workers it opened under the conductor it opened to run them: the
conductor created none of them, so every send, read, stop, close and adopt it makes
comes back ``not_creator``. ``_manager`` is the holder the fence reads instead, and
this verb is its only writer.

Two properties carry the design and each has its own section below.

* With nothing handed over, the fence decides what it always decided. ``_manager`` is
  empty on every slot until the verb writes one, and empty means "the creator holds
  it", so the widened read and the old read return the same value.
* A handoff widens the verbs a manager needs in order to RUN its reports, and not the
  verbs that mint a session. ``create`` and ``fork`` stay on ``_created_by``, which is
  never rewritten, so a manager that was GIVEN a worker cannot turn it into a deputy
  bound to the giver's memory store.

The harness is imported from ``test_session_control_tree`` rather than restated: this
verb needs the same projection and the same emitter stub the tree verbs are driven
through, and a second copy of that setup would drift from the one the adoption tests
exercise. The imported fixtures are autouse there and stay autouse here, which is what
seeds the projection and the session map for this module. The verb itself shares no body
with the tree verbs -- it writes a manager entry and no tree edge.
"""

from __future__ import annotations

import asyncio

import pytest
from chat_test_helpers import _make_state
from test_session_control_tree import (  # noqa: F401 -- imported FIXTURES apply here
    _emitted,
    _enabled,
    _hold,
    _key,
    _live,
    _run,
    _slot,
    _tree_on,
)

from kiro_crew.crew_log import emit, handed_over
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.slot_persistence import metadata_codec as codec
from kiro_crew.history import SLOT_OWNED_META_KEYS
from kiro_crew.members import DM_SLOT_KEY_PREFIX

CRON_SLOT = "cron-2c3b2e25"

#: Every verb a handoff is meant to widen. Spelled out rather than read off the
#: production set, so a verb quietly added to that set fails this file.
#: Every operation a handoff moves. ``summary`` is in here with ``read`` because it is
#: authorized as a transcript read is: a manager allowed the whole transcript is not
#: withheld the cached one-line summary of it.
WIDENED = ("send", "read", "summary", "stop", "close", "adopt", "release", "handoff")


@pytest.fixture(autouse=True)
def _saves(tmp_path, _floor_monkeypatch):
    """Report every slot save as landed, and record which slots were saved.

    The verb's commit is a REAL save on the live history; what these tests are about is
    what it writes to the slot and in what order, so the disk write is stubbed and the
    call is observed. The failure half is driven by overriding this per test.

    Patched through ``_floor_monkeypatch``, which is the rule for an AUTOUSE fixture:
    the shared ``monkeypatch`` stack is the one a test unwinds when it overrides this
    stub, and a floor patch sits under that stack rather than in it.
    """
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew-home"))
    handed_over.reset_caches()
    saved: list[str] = []

    async def _save(state, slot, **kwargs):
        saved.append(slot.key)
        return True

    async def _mark(state, slot):
        # The INDEX entry is the commit's second durable write, after the log entry.
        # Recorded in the same list so a test can read the order of both -- and it
        # writes the real index, because that index is the authority every resolve
        # consults and a stub that skipped it would make every slot read as
        # never-handed-over.
        saved.append(f"index:{slot.key}")
        return bool(handed_over.record_handed_over(slot.key))

    _floor_monkeypatch.setattr(sc, "save_slot_off_loop", _save)
    _floor_monkeypatch.setattr(sc, "_record_handed_over", _mark)
    return saved


def _team(state, *, handed: bool = False):
    """A lead, a conductor and a worker the LEAD created, as a dispatch leaves them.

    With *handed* the worker's ``_manager`` names the conductor, which is the state the
    verb writes -- set directly here so the fence can be read on its own, apart from
    the verb that produces it.
    """
    owner = _live(state, _slot(state, "chat-owner-tab"), "sid-owner")
    lead = _live(state, _slot(state, "chat-lead"), "sid-lead")
    conductor = _live(state, _slot(state, "chat-conductor"), "sid-conductor")
    worker = _live(state, _slot(state, "chat-worker"), "sid-worker")
    # The lead is itself agent-created, so it is FENCED -- which is the caller class
    # the fence is about. ``chat-owner-tab`` stays unattributed and stands in for the
    # person's own tab, the one caller the fence does not apply to at all.
    lead._created_by = owner.key
    conductor._created_by = lead.key
    worker._created_by = lead.key
    if handed:
        worker._manager = conductor.key
        worker._manager_tab = conductor._tab_id
    return lead, conductor, worker


# ── with nothing handed over, the fence is the fence it was ──────────────────


def test_a_session_nobody_handed_over_is_managed_by_its_creator(tmp_path):
    """The default that makes the field free to add: empty reads as the creator."""
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    assert worker._manager == ""
    assert sc.manager_of(worker) == lead.key
    # With the creator's own identity offered, which is what every asking site passes.
    # No record exists to compare it against, and an absent record must not be read as
    # naming somebody other than the asker -- that reading fences a creator out of a
    # session nobody ever handed anywhere.
    assert not sc._managed_by_other(state, worker, lead.key, lead._tab_id)
    assert not sc._created_by_other(worker, lead.key)


def test_the_creator_still_reaches_its_own_worker_on_every_widened_verb(tmp_path):
    """Byte-identical behaviour where no handoff happened, verb by verb."""
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    for operation in WIDENED:
        admitted = sc.authorize_target(
            state, caller_session_key=_key(lead), target=worker.key, operation=operation
        )
        assert admitted is worker, operation


def test_a_session_that_was_handed_nothing_still_refuses_a_peer(tmp_path):
    """The refusal the fence exists for is untouched: a peer is not a manager."""
    state = _make_state(tmp_path)
    _lead, conductor, worker = _team(state)
    for operation in WIDENED:
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state, caller_session_key=_key(conductor), target=worker.key, operation=operation
            )
        assert exc.value.code == "not_creator", operation


# ── what a handoff widens ────────────────────────────────────────────────────


def test_the_new_manager_reaches_the_verbs_a_handoff_widens(tmp_path):
    """The point of the field: the conductor can now RUN the worker it was given."""
    state = _make_state(tmp_path)
    _lead, conductor, worker = _team(state, handed=True)
    for operation in WIDENED:
        admitted = sc.authorize_target(
            state, caller_session_key=_key(conductor), target=worker.key, operation=operation
        )
        assert admitted is worker, operation


def test_the_giver_stops_reaching_a_session_it_handed_off_its_own_line(tmp_path):
    """One direct manager at a time: the reach MOVES rather than being shared.

    Handed OFF the giver's line, which is where the move is total: the receiver here is
    the person's own tab, so the lead manages neither it nor the worker under it, and
    the creation record it keeps buys it nothing. A receiver the giver still manages is
    the other case and is pinned beside the line walk -- a lead keeps reaching a worker
    it handed to its own conductor, because it drives that conductor either way.
    """
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    worker._manager = "chat-owner-tab"
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(lead), target=worker.key, operation="send"
        )
    # Handed to the owner's tab with no recorded occupancy, so the base PR's own record
    # check refuses as manager_session_changed before the line is walked: the base
    # cannot confirm who holds that tab. Either way the giver does not reach it,
    # which is the point here -- the reach moved off the giver's line.
    assert exc.value.code == "manager_session_changed"
    assert worker._created_by == lead.key, "the creation record is not what moved"


def test_a_handoff_does_not_widen_fork(tmp_path):
    """A forked child inherits the CALLER's memory binding, so the fork gate stays on
    the creator: a manager that was given a worker must not be able to copy it into a
    session bound to the giver's store."""
    state = _make_state(tmp_path)
    _lead, conductor, worker = _team(state, handed=True)
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(conductor), target=worker.key, operation="fork"
        )
    assert exc.value.code == "not_creator"
    assert "fork" not in sc.HANDOFF_WIDENED_OPERATIONS
    assert "create" not in sc.HANDOFF_WIDENED_OPERATIONS


def test_memory_delegation_reads_the_creator_chain_and_not_the_manager(tmp_path):
    """``_delegation_lineage_fenced`` is the private-member dispatch gate, and a
    handoff must be unable to move it in either direction.

    Both directions are asserted, because reading ``_manager`` there would have shown
    up as one or the other: a member-rooted chain handed to an owner-rooted session
    would have gained dispatch, and an owner-rooted chain handed to a cron would have
    lost it.
    """
    state = _make_state(tmp_path)
    state.get_or_create_slot("chat-owner-tab")  # unattributed: the owner's own tab
    member_rooted = state.get_or_create_slot("chat-from-member")
    member_rooted._created_by = DM_SLOT_KEY_PREFIX + "radar"
    member_rooted._manager = "chat-owner-tab"
    assert sc._delegation_lineage_fenced(state, "chat-from-member")

    owner_rooted = state.get_or_create_slot("chat-from-owner")
    owner_rooted._created_by = "chat-owner-tab"
    owner_rooted._manager = CRON_SLOT
    assert not sc._delegation_lineage_fenced(state, "chat-from-owner")


# ── the verb ─────────────────────────────────────────────────────────────────


def test_the_manager_hands_a_worker_to_a_conductor(tmp_path, monkeypatch, _saves):
    """The happy path, end to end: the record moves, then the field does.

    ONE durable write about the handoff, and it is the manager entry. The tree is not
    touched, so the sidebar still nests the worker under the session that CREATED it --
    the reach moves here and the enumeration does not, which is the state this step
    declares.
    """
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    result = _run(
        sc.handoff_target(state, caller_session_key=_key(lead), target=worker.key, to=conductor.key)
    )
    assert result == {
        "target": "chat-worker",
        "manager": "chat-conductor",
        "previous_manager": "chat-lead",
    }, "no parent in the reply, because the verb writes none"
    # THE durable write, and the only one: the manager entry, which is the commit point
    # and what a restart rebuilds the field from. No tree entry either side of it -- the
    # session keeps the parent it had, so a reader of the log sees the reach move and the
    # lineage stay put.
    assert calls == [
        {
            "op": "manager",
            "sid": "sid-worker",
            "slot": "chat-worker",
            # WHICH session under that name, because the name alone is a name: a
            # closed session's replacement can hold it, and a grant qualified by the
            # key would be restored to the replacement at the next start.
            "slot_tab": worker._tab_id,
            "manager": "chat-conductor",
            "previous": "chat-lead",
            # Both writers of this entry go through one assembly, which names every
            # field: a transfer FORWARD says so by not being a revert, and the
            # occupancy of the manager key lets a rebuild tell the session that was
            # handed this one from a later session that took its name.
            "to_creator": False,
            "manager_tab": conductor._tab_id,
        },
    ]
    assert worker._manager == "chat-conductor"
    assert worker._created_by == "chat-lead", "the creation record is never rewritten"
    # ONE durable write to the slot's line, and it is the marker, never the manager: no
    # slot save runs, so nothing caches who holds the session.
    assert _saves == [f"index:{worker.key}"], "the entry, then only the index write"
    assert worker.key in handed_over.handed_over_slots()[0]


def test_the_reach_moves_and_the_created_roster_does_not_in_this_step(
    tmp_path, monkeypatch, _saves
):
    """A DECLARED INTERMEDIATE STATE, pinned here so a reviewer can check its edges
    rather than infer them.

    Authorization moves with the handoff and enumeration does not. The recipient can
    drive the worker on every verb a handoff widens, while the tree -- which is what
    the durable roster is folded from -- still names the CREATOR as its parent, because
    this verb writes no tree edge.

    That is not an incoherence between two answers to one question. It is two
    questions: ``_created_tree_roster`` answers "what did I create", which the tree is
    the record of, and "what can I reach" is answered by the manager. The roster that
    lists a session by who may reach it arrives with the listing work in the following
    step; until then a recipient holds a worker it cannot see under its own name.

    Safe to ship in that order because it is a NARROWING, not a widening: the gap is a
    capability only for a caller that already holds the key it was handed, and no
    caller gains reach from a row it cannot see.
    """
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    _run(
        sc.handoff_target(state, caller_session_key=_key(lead), target=worker.key, to=conductor.key)
    )

    # THE REACH MOVED: every verb a handoff widens admits the recipient.
    for operation in WIDENED:
        assert (
            sc.authorize_target(
                state, caller_session_key=_key(conductor), target=worker.key, operation=operation
            )
            is worker
        ), operation

    # THE TREE DID NOT. The roster the sidebar and the durable half of
    # ``created_session_status`` are built from still puts the worker under the session
    # that opened it, so the recipient's own "what did I create" answer does not carry
    # it and the giver's still does.
    held, held_state = sc._created_tree_roster(conductor.key)
    created, created_state = sc._created_tree_roster(lead.key)
    assert held_state == "readable" and created_state == "readable", "a real tree read"
    assert worker.key not in held, "the recipient drives it and cannot yet list it"
    assert worker.key in created, "and the creator still lists what it created"


def test_the_new_manager_can_hand_the_session_on(tmp_path, monkeypatch, _saves):
    """A transfer is started by the CURRENT manager, so one handoff cannot be the last."""
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state, handed=True)
    second = _live(state, _slot(state, "chat-second"), "sid-second")
    second._created_by = conductor.key
    _hold(state, worker.key, conductor.key, sid="sid-worker")
    result = _run(
        sc.handoff_target(
            state, caller_session_key=_key(conductor), target=worker.key, to=second.key
        )
    )
    assert result["manager"] == "chat-second"
    assert result["previous_manager"] == "chat-conductor"
    assert worker._manager == "chat-second"
    _ = lead


def test_a_session_cannot_hand_over_one_it_does_not_manage(tmp_path, monkeypatch):
    """Neither the report nor a bystander may start a transfer.

    The conductor here has a recipient it genuinely controls, so the only thing wrong
    with the call is the half the design cares about: it does not hold the target.
    """
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    _lead, conductor, worker = _team(state)
    deputy = _live(state, _slot(state, "chat-deputy"), "sid-deputy")
    deputy._created_by = conductor.key
    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(conductor), target=worker.key, to=deputy.key
            )
        )
    assert exc.value.code == "not_creator"
    assert calls == [], "a refused handoff writes nothing"
    assert worker._manager == ""


def test_the_recipient_must_be_a_session_the_giver_controls(tmp_path, monkeypatch):
    """You move people in your own org to a manager in your own org."""
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    stranger = _live(state, _slot(state, "chat-stranger"), "sid-stranger")
    stranger._created_by = "chat-somebody-else"
    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=stranger.key
            )
        )
    assert exc.value.code == "not_creator"
    assert calls == []
    assert worker._manager == ""


def test_handing_a_session_to_itself_is_refused(tmp_path, monkeypatch):
    """An entry that records a change where none happened is worse than a refusal, and
    a session that is its own parent is a shape the tree cannot present."""
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=worker.key
            )
        )
    assert exc.value.code == "already_manager"
    assert calls == []
    assert worker._manager == ""


def test_handing_a_session_to_the_manager_it_already_has_is_refused(tmp_path, monkeypatch):
    """The owner's own tab is the caller that can reach this: it is unfenced, so it may
    name any session in the workspace -- including the one already holding the target.
    """
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    _lead, conductor, worker = _team(state, handed=True)
    owner = state.get_slot("chat-owner-tab")
    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(owner), target=worker.key, to=conductor.key
            )
        )
    assert exc.value.code == "already_manager"
    assert calls == []
    assert worker._manager == conductor.key


def test_a_handoff_to_the_session_answering_to_a_stale_record_is_not_refused(tmp_path, monkeypatch):
    """The refusal everywhere else says "hand it over again". That has to be runnable.

    A record names a KEY and a SESSION. When the session is gone and another answers to
    the key, every read of that record refuses with ``manager_session_changed``, and the
    way out it names is this verb -- so a gate that asked "is the receiver already the
    manager" by key alone refused the one handoff that could clear the record, and the
    refusal became permanent for the life of the slot.

    The receiver is not the session the record names, so there IS something to hand
    over, and the gate asks that through the same predicate the fence asks it with.
    """
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    _lead, conductor, worker = _team(state, handed=True)
    # The record's key still matches the live conductor; its IDENTITY names a session
    # that is gone. This is what a close-and-reoccupy of a deterministic key leaves.
    worker._manager_tab = "the-session-that-was-given-it"
    owner = state.get_slot("chat-owner-tab")

    _run(
        sc.handoff_target(
            state, caller_session_key=_key(owner), target=worker.key, to=conductor.key
        )
    )

    assert worker._manager == conductor.key
    assert worker._manager_tab == conductor._tab_id, "the record now names the live one"
    assert [call["op"] for call in calls].count("manager") == 1, "the move was recorded"


def test_an_append_that_never_landed_leaves_the_manager_where_it_was(tmp_path, monkeypatch):
    """The record is written FIRST, so a lost write costs the move and not the reach.

    The reverse order would be the unsafe one: a field written first and an append that
    then failed would leave the gateway admitting a manager no record names.
    """
    _emitted(monkeypatch, landed=False)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )
    assert exc.value.code == "handoff_not_recorded"
    assert worker._manager == "", "the reach must not move on a record that is not on disk"


def asyncio_cancelled() -> BaseException:
    """A ``BaseException`` that is not an ``Exception``, which is the case under test."""
    return asyncio.CancelledError()


def test_the_caller_rule_is_re_asked_inside_the_lock(tmp_path, monkeypatch):
    """The lock wait is unbounded, so a rule decided before it is decided on stale
    state -- and the way it goes stale here is the way that matters: another handoff
    lands first, so somebody else holds the target by the time the lock opens.

    Driven through the second warm, which is the suspension the in-lock gate sits
    behind: the first warm is the pre-lock gate's, the second is the re-check's.
    """
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    warms: list[int] = []

    async def _hand_it_away_on_the_final_warm():
        warms.append(1)
        if len(warms) == 2:
            # A real handoff landing in the wait publishes BOTH the key and its
            # occupancy, so the fixture does too -- a manager with no identifiable
            # occupant is a different refusal, and this test is about the caller rule.
            faster = _live(state, _slot(state, "chat-somebody-faster"), "sid-faster")
            worker._manager = faster.key
            worker._manager_tab = faster._tab_id

    monkeypatch.setattr(sc, "prewarm_enabled_check", _hand_it_away_on_the_final_warm)
    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )
    assert exc.value.code == "not_creator"
    assert len(warms) == 2, "the re-check has to be behind its own warm"
    assert calls == [], "nothing is appended once the re-check refuses"
    assert worker._manager == "chat-somebody-faster"


def test_a_recipient_replaced_under_the_same_key_is_refused(tmp_path, monkeypatch):
    """A key is not an identity on the RECIPIENT side either: a close plus a same-key
    reopen inside the lock wait would hand the session to a conversation that was
    never named."""
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    warms: list[int] = []

    async def _replace_the_recipient_on_the_final_warm():
        warms.append(1)
        if len(warms) == 2:
            del state._slots[conductor.key]
            replacement = _live(state, _slot(state, conductor.key), "sid-replacement")
            replacement._created_by = lead.key

    monkeypatch.setattr(sc, "prewarm_enabled_check", _replace_the_recipient_on_the_final_warm)
    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )
    assert exc.value.code == "recipient_replaced"
    assert calls == []
    assert worker._manager == ""


def test_a_handoff_is_refused_when_the_sessions_are_not_recorded(tmp_path, monkeypatch):
    """With no crew log there is nowhere to write the grant, and a verb whose record
    cannot be written has not done anything -- so the field is not written either."""
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    monkeypatch.setattr(emit, "enabled", lambda: False)
    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )
    assert exc.value.code == "handoff_unavailable"
    assert worker._manager == ""


def test_an_empty_recipient_is_refused_before_anything_is_read(tmp_path, monkeypatch):
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    with pytest.raises(sc.SessionControlError) as exc:
        _run(sc.handoff_target(state, caller_session_key=_key(lead), target=worker.key, to="  "))
    assert exc.value.code == "recipient_required"


# ── the manager survives a restart ───────────────────────────────────────────


def test_the_slot_line_carries_no_manager_at_all(tmp_path, monkeypatch):
    """The crew log is the ONLY store. A save writes no manager onto the slot's line, so
    there is no second copy to prefer, to reconcile, or to be wrong -- and no line can
    hand a session to anybody.

    A copy here would be written on every save and read on every restore, and then
    overwritten by the log every time, because the start establishes the field from the
    log on the same path that restores these slots. It was also the way a revoked
    manager came back: a line outlives the grant it names.
    """
    state = _make_state(tmp_path)
    source = _slot(state, "chat-worker")
    source._created_by = "chat-lead"
    source._manager = "chat-conductor"
    meta = codec.encode(
        source, folds=codec.SaveFolds(memory_mode=source.memory_mode, created_at=source.created_at)
    )
    assert "manager" not in meta, "the live field is not persisted"
    assert meta["created_by"] == "chat-lead", "and the creator still is"


def test_a_restored_slot_holds_no_manager_until_the_log_says_so(tmp_path):
    """A slot comes up with NO manager, whatever its line says, because the line says
    nothing about one: the codec has no row for it, so the attribute is only ever set by
    the log reconstruction. Empty means the creator holds the session -- the reach the
    gateway had before any handoff."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-restored")
    codec.apply(
        state,
        slot,
        {"created_by": "chat-lead"},
        codec.Restore(name="chat-restored"),
        # The index this slot's own hydrate is given: nothing was handed over on this
        # store. Handed in as a value because the codec reads no file -- the read
        # belongs to the pass above it, once, off the loop.
        handoff_index=handed_over.handed_over_slots(),
    )
    assert slot._manager == ""
    assert sc.manager_of(slot) == "chat-lead"


# ── the verb is reachable the way every other session verb is ────────────────


def test_the_tool_the_route_and_the_schema_name_one_verb():
    """Four declarations have to agree or the verb is unreachable in production.

    The path has to be in the strict internal set, or the MCP caller's header is
    ignored and the route falls through to the cookie branch while handler tests still
    pass. The tool has to declare that route, and the argument schema has to exist
    under the tool's own name, because an unvalidated tool call is refused before the
    handler sees it.
    """
    from kiro_crew.dashboard.handlers import session_control as handlers
    from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS
    from kiro_crew.mcp_dashboard import TABLE
    from kiro_crew.validation import MCP_DASHBOARD_SCHEMAS

    assert "/api/session-control/handoff" in _STRICT_INTERNAL_API_PATHS
    assert callable(handlers.api_session_control_handoff)
    tool = next(entry for entry in TABLE if entry.name == "session_handoff")
    assert tool.routes == ("POST /api/session-control/handoff",)
    assert sorted(tool.schema["required"]) == ["target", "to"]
    assert sorted(field.name for field in MCP_DASHBOARD_SCHEMAS["session_handoff"].fields) == [
        "target",
        "to",
    ]


def test_a_channel_agent_cannot_hand_sessions_around():
    """A channel agent acts on words from a thread other people are in, and this verb
    decides who may drive a session -- the same containment the tree verbs have."""
    from kiro_crew.channel import CHANNEL_AGENT_BLOCKED_TOOLS

    assert "session_handoff" in CHANNEL_AGENT_BLOCKED_TOOLS


# ── the record is the commit point; the slot line is its cache ───────────────
#
# What makes a handoff durable is the crew log entry: append-only, and awaited before
# any reach moves. ``_manager`` is set only after that, and the slot line is encoded
# from ``_manager`` alone -- so no writer, this verb's save or a turn's own, can
# persist a manager the gateway has not put in force. Each case asserts BOTH answers:
# the live one the fence reads, and the one a restart reads back off the line.


def _restored(state, slot) -> str:
    """The manager a restart would read back for *slot*, through the real codec."""
    line = codec.encode(
        slot, folds=codec.SaveFolds(memory_mode=slot.memory_mode, created_at=slot.created_at)
    )
    fresh = state.get_or_create_slot(f"restored-{slot.key}")
    fresh._created_by = slot._created_by
    codec.apply(
        state,
        fresh,
        line,
        codec.Restore(name=f"restored-{slot.key}"),
        handoff_index=handed_over.handed_over_slots(),
    )
    return sc.manager_of(fresh)


def test_an_entry_that_never_landed_leaves_both_managers_unchanged(tmp_path, monkeypatch):
    """The commit point is the ENTRY. A write the crew log gave up on is a handoff that
    did not happen, so neither the live manager nor the restored one moves."""
    _emitted(monkeypatch, landed=False)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    saves: list[str] = []

    async def _save(state_, slot, **kwargs):
        saves.append(slot.key)
        return True

    monkeypatch.setattr(sc, "save_slot_off_loop", _save)
    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )
    assert exc.value.code == "handoff_not_recorded"
    assert worker._manager == ""
    assert sc.manager_of(worker) == "chat-lead"
    assert _restored(state, worker) == "chat-lead"
    assert saves == [], "no line is written for a handoff that never committed"


def test_an_entry_that_landed_moves_the_manager_and_saves_nothing(tmp_path, monkeypatch, _saves):
    """The entry is on disk, so the reach moves -- and that is the whole operation.

    No slot save follows it and the reply carries no ``cached`` key, because there is
    no second copy whose landing could be reported. A handoff that returns at all is a
    handoff the crew log carries.
    """
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    result = _run(
        sc.handoff_target(state, caller_session_key=_key(lead), target=worker.key, to=conductor.key)
    )
    assert result["manager"] == "chat-conductor"
    assert "cached" not in result, "nothing to report the landing of"
    assert worker._manager == "chat-conductor"
    assert sc.manager_of(worker) == "chat-conductor"
    assert _saves == [f"index:{worker.key}"], "no slot save, only the index write"


def test_no_writer_can_persist_a_manager_at_all(tmp_path, monkeypatch):
    """The property the storage model rests on, asserted on the codec itself.

    A concurrent writer -- a turn's own save, the periodic flush -- encodes the slot's
    whole line, and the manager is not on it in either form. So there is nothing any
    writer can put on disk that a later start would read as a grant, which is a
    stronger statement than one about staging: it does not depend on the published field
    being the only staging point, because the field reaches disk by no path.

    The key is not in ``SLOT_OWNED_META_KEYS`` either, so nothing claims it on a save.
    It needs no tolerance: no shipped build ever wrote it, so no line in the field
    carries it, and an undeclared key is ignored on read in any case.
    """
    state = _make_state(tmp_path)
    _lead, _conductor, worker = _team(state)
    folds = codec.SaveFolds(memory_mode=worker.memory_mode, created_at=worker.created_at)
    worker._manager = "chat-conductor"
    assert "manager" not in codec.encode(worker, folds=folds)
    assert "manager" not in codec.encode(worker, merge=True, folds=folds)
    assert "manager" not in SLOT_OWNED_META_KEYS, "and no writer claims the key"
    assert sc.manager_of(worker) == "chat-conductor", "the live field still authorizes"


def test_a_recipient_in_another_memory_store_is_refused(tmp_path, monkeypatch):
    """A memory store is a separate silo, so a recipient bound to one the giver is not
    runs under another principal -- which is what the member refusal is really about.

    Established POSITIVELY from the two slots' own bindings. Asking the config whether
    the recipient is a crew member would answer False for every unreadable or
    incomplete record, and that direction admits the member: a V2 store whose record
    lost ``memory_version`` reads as a plain session.
    """
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    elsewhere = _live(state, _slot(state, "chat-elsewhere"), "sid-elsewhere")
    elsewhere._created_by = lead.key
    elsewhere.memory_store = "member-radar-private"
    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=elsewhere.key
            )
        )
    assert exc.value.code == "recipient_other_store"
    assert calls == []
    assert worker._manager == ""


def test_a_recipient_sharing_the_givers_store_is_accepted(tmp_path, monkeypatch, _saves):
    """The positive half, so the rule above is not just a refusal of everything: a
    session the giver created inherits the giver's binding, which is the shape this
    verb exists for."""
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    lead.memory_store = "crew-alpha"
    conductor.memory_store = "crew-alpha"
    worker.memory_store = "crew-alpha"
    _hold(state, worker.key, lead.key, sid="sid-worker")
    result = _run(
        sc.handoff_target(state, caller_session_key=_key(lead), target=worker.key, to=conductor.key)
    )
    assert result["manager"] == "chat-conductor"
    assert worker._manager == "chat-conductor"


def test_the_member_dm_refusal_needs_no_config_record(tmp_path, monkeypatch):
    """A DM slot is the member's own operating surface, and the prefix is part of the
    slot KEY -- so the refusal holds whatever the config record says or fails to say."""
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    member = _live(state, _slot(state, "chat-stand-in"), "sid-member")
    member._created_by = lead.key
    monkeypatch.setattr(sc, "_member_caller", lambda state_, key: False)
    monkeypatch.setattr(sc, "_store_is_member_owned", lambda store: False)
    monkeypatch.setattr(member, "key", DM_SLOT_KEY_PREFIX + "radar", raising=False)
    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to="chat-stand-in"
            )
        )
    assert exc.value.code == "member_recipient"
    assert calls == []
    assert worker._manager == ""


# ── the recipient is re-established against the live object before the reach moves ──


@pytest.mark.parametrize(
    ("what", "change"),
    [
        ("rebound to another store", lambda state, slot: setattr(slot, "memory_store", "other")),
        (
            "channel-linked",
            lambda state, slot: setattr(slot, "linked_session_key", "slack:C1:123.45"),
        ),
        ("closed and reopened", lambda state, slot: state._slots.pop(slot.key, None)),
    ],
)
def test_a_recipient_that_changes_before_the_commit_is_not_given_the_session(
    what, change, tmp_path, monkeypatch
):
    """The gate's verdict about the recipient is formed before the entry is awaited, and
    that await suspends. The recipient's own routes take its slot lock and not this
    verb's, so inside that window its store can be rebound to another principal's, a
    channel link attached, or the session closed. Each changes WHO would hold the handed
    session, so the reach may not move on the earlier verdict.

    Injected from the entry's own settle callback, which is the suspension between the
    last gate and the commit step.
    """
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    saves: list[str] = []

    async def _record_save(state_, slot, **kwargs):
        saves.append(slot.key)
        return True

    def _change_then_settle(sid, **kw):
        change(state, conductor)
        settle = kw.get("on_settled")
        if settle is not None:
            settle(True)

    monkeypatch.setattr(sc, "save_slot_off_loop", _record_save)
    monkeypatch.setattr(emit, "on_manager_set", _change_then_settle)
    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )
    assert exc.value.code == "recipient_replaced", what
    assert worker._manager == "", what
    assert saves == [], "a refused commit writes no line"


def test_the_live_recipient_check_admits_an_unchanged_recipient(tmp_path, monkeypatch, _saves):
    """Control for the parametrized refusals above: with nothing changing, the same
    check publishes. Without this a predicate that always refused would pass them."""
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    result = _run(
        sc.handoff_target(state, caller_session_key=_key(lead), target=worker.key, to=conductor.key)
    )
    assert result["manager"] == "chat-conductor"
    assert worker._manager == "chat-conductor"


def test_the_previous_manager_is_read_from_the_target_not_from_the_caller(
    tmp_path, monkeypatch, _saves
):
    """An owner's own tab is unfenced, so it can hand over a worker it never managed --
    and then the caller is not the manager being replaced. Read from the target."""
    _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    owner = state.get_slot("chat-owner-tab")
    _hold(state, worker.key, lead.key, sid="sid-worker")
    result = _run(
        sc.handoff_target(
            state, caller_session_key=_key(owner), target=worker.key, to=conductor.key
        )
    )
    assert result["previous_manager"] == "chat-lead", "the lead created it, so the lead held it"
    assert result["manager"] == "chat-conductor"


# ── the widened set and the broadcast audience agree with each other ─────────


def test_the_widened_set_is_exactly_what_the_fence_reads(tmp_path):
    """The set and this file's own list are pinned against each other, because the two
    ways they drift are opposite failures. An operation missing from the set is a reach
    the manager needs and does not get, and one that should not be there is a reach a
    handoff was never meant to move -- and neither shows up as a test failure anywhere
    else, because every other case in this file reads the set it is checking.
    """
    assert sc.HANDOFF_WIDENED_OPERATIONS == frozenset(WIDENED)


def test_the_giver_stops_reading_the_summary_of_what_it_handed_over(tmp_path):
    """``summary`` moves with ``read``. It is authorized as a transcript read is, and the
    summary is generated FROM the transcript -- so the set has to carry it: left out, the
    giver keeps a window onto a session it cannot open, and the manager is refused the
    one-line form of a transcript it is allowed in full.
    """
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state, handed=True)

    admitted = sc.authorize_target(
        state, caller_session_key=_key(conductor), target=worker.key, operation="summary"
    )
    assert admitted is worker, "the manager reads the summary"

    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(lead), target=worker.key, operation="summary"
        )
    assert exc.value.code == "not_creator", "and the giver does not"


def test_the_default_broadcast_audience_follows_the_handoff(tmp_path):
    """The audience has to agree with the fence that judges each delivery, and ``send``
    is one of the verbs a handoff widens.

    Read from the creator instead, the audience was wrong in both directions at once: a
    handed-away session stayed in its GIVER's default audience, where every delivery is
    then refused, and it was missing from the audience of the only session that can
    actually reach it. The caller is excluded from its own audience either way, because
    a self-target is refused and a broadcast must not collect a refusal nobody could
    have avoided.
    """
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)

    assert worker.key in sc.broadcast_audience(state, lead.key), "the creator's, to start"
    assert worker.key not in sc.broadcast_audience(state, conductor.key)

    worker._manager = conductor.key

    worker._manager_tab = conductor._tab_id

    assert worker.key in sc.broadcast_audience(state, conductor.key), "the manager's, after"
    assert worker.key not in sc.broadcast_audience(state, lead.key), "and not the giver's"


def test_the_broadcast_audience_is_unchanged_where_nothing_was_handed_over(tmp_path):
    """The reading only differs once something has been handed over: ``manager_of``
    answers ``_created_by`` until a handoff writes the field, so a gateway that has
    never used the verb gets exactly the audience it got before the field existed."""
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)

    assert all(not slot._manager for slot in state._slots.values())
    assert sc.broadcast_audience(state, lead.key) == sorted(
        key
        for key, slot in state._slots.items()
        if key != lead.key and not sc._created_by_other(slot, lead.key)
    )
    assert conductor.key in sc.broadcast_audience(state, lead.key)


def test_a_target_with_queued_work_is_not_handed_over(tmp_path, monkeypatch):
    """The seam the queued-send work leaves behind, closed by one rule.

    An entry waiting on a busy session was authorized when it was ADMITTED, and what
    admitted it is the sender's reach -- which this verb moves. Nothing re-checks that
    relation at the drain: the snapshot it compares describes the TARGET alone, while
    this is a relation between the target's manager and one entry's sender. Driven
    rather than argued: with the entry queued, the handoff is refused and NOTHING is
    appended, so the manager the entry was accepted for is still the manager when it
    runs.

    The remedy is named in the refusal because this state does not always clear itself:
    a queue restored from disk at boot waits for its session's next send
    (``_arm_parked_queue_drain`` says so), so a caller told only "retry" could retry
    forever. Sending to the target is what starts the turn that drains it, and the
    caller receiving this refusal is the one session that may send.
    """
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    worker.queue_append("accepted while the lead still managed it")

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "queue_not_drained"
    assert exc.value.status == 409
    assert "send to it" in str(exc.value), "the refusal names the way through"
    assert calls == [], "nothing is appended for a handoff that would strand a queued entry"
    assert worker._manager == "", "and the manager did not move"
    assert len(worker._queue) == 1, "the entry is untouched -- not delivered, not destroyed"

    # Drained, and the same call goes through.
    worker.queue_pop(0)
    result = _run(
        sc.handoff_target(state, caller_session_key=_key(lead), target=worker.key, to=conductor.key)
    )
    assert result["manager"] == conductor.key


def test_a_send_cannot_land_inside_the_commit_because_admission_refuses_it(tmp_path, monkeypatch):
    """The window is closed at the SOURCE, which is what lets the queue be read once.

    A queue read placed after the append would be past the commit point, and a refusal
    there owes a correction -- which is the reason not to put it there. Instead the
    commit is marked in flight and
    ``session_send`` -- the only verb that can add an entry to a target's queue --
    refuses while the mark is up. So no entry can arrive between the queue read and the
    publish, and the read happens once, ahead of the commit, where its refusal writes
    nothing.

    Driven through the real verb from inside the append's own suspension, which is the
    instant the old post-append read existed for.
    """
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    calls = _emitted(monkeypatch)

    seen: "dict[str, object]" = {}
    real_settle = emit.awaiting_settlement

    async def _send_from_inside_the_append(emit_one, *, what, **kwargs):
        if "handoff of" in what and "send" not in seen:
            seen["marked"] = sc.manager_commit_pending(worker.key)
            try:
                await sc.send_to_target(
                    state,
                    caller_session_key=_key(lead),
                    target=worker.key,
                    message="landed while the entry was being written",
                )
                seen["send"] = "accepted"
            except sc.SessionControlError as exc:
                seen["send"] = exc.code
        return await real_settle(emit_one, what=what, **kwargs)

    monkeypatch.setattr(emit, "awaiting_settlement", _send_from_inside_the_append)

    result = _run(
        sc.handoff_target(state, caller_session_key=_key(lead), target=worker.key, to=conductor.key)
    )

    assert seen["marked"] is True, "this test says nothing unless the send ran inside the mark"
    assert seen["send"] == "manager_change_in_flight", (
        "a send accepted inside the commit would run under whichever session ends up "
        f"managing the target; admission answered {seen['send']!r}"
    )
    assert not worker._queue, "and nothing reached the queue, so there is nothing stranded"

    # The handoff itself completes, with ONE entry and no correction: a queue refusal
    # placed after the commit point is what would owe one.
    assert result["manager"] == conductor.key
    assert [call["op"] for call in calls] == ["manager"], "the grant alone, no undo"


def test_a_send_is_served_again_the_moment_the_commit_is_over(tmp_path, monkeypatch):
    """The refusal is scoped to the commit, so it cannot outlive one.

    ``manager_change_in_flight`` tells the caller to retry in a moment, and that is only
    honest if the mark is down when the verb returns -- on the success path and on a
    raise. A send after a completed handoff is accepted, by the session that manages it
    now.
    """
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    _emitted(monkeypatch)

    _run(
        sc.handoff_target(state, caller_session_key=_key(lead), target=worker.key, to=conductor.key)
    )
    assert sc.manager_commit_pending(worker.key) is False, "the mark did not outlive the commit"

    # The new manager's send is admitted -- it gets past the in-flight check, which is
    # all this asserts: the delivery itself is another surface.
    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.send_to_target(
                state,
                caller_session_key=_key(lead),
                target=worker.key,
                message="the giver no longer manages it",
            )
        )
    assert exc.value.code == "not_creator", (
        "the giver is refused for the reason the handoff moved, not for an in-flight "
        f"commit; it answered {exc.value.code!r}"
    )


def test_a_steer_still_waiting_on_the_turn_refuses_the_handoff_too(tmp_path, monkeypatch):
    """A pending steer is queued work that has not reached the queue yet.

    The window the queue-only read missed. A steer handed to a running turn waits in
    ``_pending_steers`` until the turn confirms it, so a slot can hold an EMPTY queue
    and still be holding an instruction admitted for its current manager. Every turn
    exit requeues an unconfirmed steer at the head of ``_queue``, which puts it under
    whatever manager this verb left behind -- and the drain re-checks containment for
    the target, never the sender against the manager.

    Nothing contrived is required to reach it: a lead steers a worker, then hands that
    worker to the conductor that should run it. Driven to the requeue, so the entry
    arriving in the queue is the real teardown's doing and not this test's.
    """
    # Through the FACADE, which is where the owner's names are bound: the requeue is
    # composed onto `chat_runner`, so calling it on its owning module would run it with
    # half its globals missing.
    from kiro_crew.dashboard import chat_runner

    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hold(state, worker.key, lead.key, sid="sid-worker")
    worker._pending_steers.append("stop what you are doing and read this instead")
    assert not worker._queue, "the queue is empty, which is what made this window quiet"

    with pytest.raises(sc.SessionControlError) as exc:
        _run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=conductor.key
            )
        )

    assert exc.value.code == "queue_not_drained"
    assert exc.value.status == 409
    assert calls == [], "nothing is appended for a handoff that would strand a steer"
    assert worker._manager == "", "and the manager did not move"

    # The hazard, shown rather than asserted about: the real teardown turns that steer
    # into an ordinary queue entry, which would have run under the new manager.
    chat_runner._requeue_unconsumed_steers(state, worker)
    assert len(worker._queue) == 1, "the steer became a queue entry at the turn's exit"
    assert not worker._pending_steers

    # Drained, and the same call goes through.
    worker.queue_pop(0)
    result = _run(
        sc.handoff_target(state, caller_session_key=_key(lead), target=worker.key, to=conductor.key)
    )
    assert result["manager"] == conductor.key


# ── "X is non-empty" is a rule about every container that holds X ────────────
#
# The rule was "refuse while the target still has queued work". The implementation read
# `_queue`, and `_pending_steers` holds the same work one step earlier -- so a slot with
# an empty queue passed the gate while holding an instruction that became a queue entry
# at the next turn exit. The rule was right and it was applied to one of the two places
# the thing it names can live.
#
# So the containers are ENUMERATED and the count is pinned, the same mechanism the key
# comparisons and the identity verdicts on this line already use. A container that holds
# a slot's unrun work and is not in the first map reds here; one that merely looks like
# it costs whoever adds it a written reason in the second.

#: Containers on a slot that hold WORK ADMITTED FOR IT THAT HAS NOT RUN. The queued-work
#: refusal must read every one of them.
_PENDING_WORK_CONTAINERS = {
    "_queue": (
        "entries waiting to run, each admitted when it was sent and drained as the "
        "input of a later turn -- the container the rule was written about"
    ),
    "_pending_steers": (
        "a steer handed to a RUNNING turn and not yet confirmed consumed. The turn's "
        "exit requeues an unconfirmed one at the head of _queue, so this holds the same "
        "admitted work one step earlier and an empty _queue says nothing about it"
    ),
}

#: Containers that read as pending work and hold none, each with the reason. A new
#: ``_pending_*`` or ``*_queue`` on a slot has to land in one map or the other.
_NOT_PENDING_WORK = {
    "_pending": (
        "the streamed turn's own transcript chunk rows, held as the same dicts the "
        "message window carries. Nobody sent them and nothing runs them"
    ),
    "_pending_subagent_failures": (
        "notices about subagent results that timed out, PREPENDED to whatever message "
        "the next turn already has. They do not cause a turn and carry no sender's "
        "reach -- the gateway wrote them about its own work"
    ),
    "_subagent_delivery_pending": (
        "the delivery debt for those same notices, which is bookkeeping about whether "
        "they were handed over, not an instruction waiting to run"
    ),
    "_pending_context": (
        "silent background context from apps and subagents, prepended to a turn that "
        "runs for another reason. It is never itself why a turn runs"
    ),
    "_pending_variants": (
        "regenerate variants waiting to attach to a finalized assistant message, which "
        "is render state about a turn that already happened"
    ),
    "_question_pending": (
        "questions the AGENT is waiting on a person to answer. The direction is the "
        "opposite one: nothing here is work somebody sent this session"
    ),
}


def test_every_container_that_holds_unrun_work_is_read_by_the_queue_refusal():
    """The mechanism, not the reminder.

    Two halves, because either alone is a convention. The containers are DISCOVERED
    from a real slot and each must be accounted for, so a third one cannot appear
    unnoticed; and the refusal's own source must read every container the first map
    names, so accounting for one is not the same as checking it.

    The discovery reads the OBJECT, not the turn's source. This file is budgeted to
    read none of that source, and the rule behind the budget holds here anyway: what a
    slot carries is a property of the slot, so the slot is what answers. It is the
    better reading too -- a container the constructor spells one way and the class
    holds under another would satisfy a source scan and fail this.

    The name filter is the part that stays a convention. "Holds unrun work" is not a
    type (all of these are lists or dicts) and not a value, so what is mechanised is
    the thing that can be: a container whose name says pending or queue has to be
    classified by somebody, with a reason, before this passes.
    """
    import inspect

    from kiro_crew.dashboard.chat_runner import _ChatSlot

    slot = _ChatSlot("chat-enumerate-containers")
    found = {
        name
        for name in _ChatSlot.__slots__
        if ("queue" in name or "pending" in name)
        and isinstance(getattr(slot, name, None), (list, dict))
    }
    assert found, "no containers were discovered, so this test is asserting nothing"
    unaccounted = found - set(_PENDING_WORK_CONTAINERS) - set(_NOT_PENDING_WORK)
    assert not unaccounted, (
        f"a slot grew {sorted(unaccounted)}, and whether a handoff may move the manager "
        "out from under it has not been decided. Put each in _PENDING_WORK_CONTAINERS "
        "(and read it in _queued_work_refusal) or in _NOT_PENDING_WORK with the reason "
        "it holds no admitted work"
    )

    # The count, pinned: the rule was broken by being applied to one of two.
    assert len(_PENDING_WORK_CONTAINERS) == 2, (
        "the number of containers holding a slot's unrun work changed; the refusal "
        "reads a fixed list, so this is the line that has to notice"
    )

    refusal = inspect.getsource(sc._queued_work_refusal)
    for container in _PENDING_WORK_CONTAINERS:
        assert container in refusal, (
            f"_queued_work_refusal does not read {container!r}, so a handoff is allowed "
            "while it holds work that was admitted for the manager being replaced"
        )
    # And it reads them as EMPTINESS, together: one of them non-empty has to refuse.
    assert (
        "if not slot._queue and not" in refusal
    ), "the refusal no longer returns None only when every container is empty"
