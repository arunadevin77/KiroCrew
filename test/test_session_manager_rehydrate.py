"""A handoff survives a restart, and an unreadable log falls back to the creator.

The handoff's storage model has two halves and this file is the READ half. The verb
publishes a new manager only once its ``session/manager_set`` entry is on disk, so the
crew log is the authority for who drives a session and the slot's metadata line is a
cache of it. Without the rebuild below, every gateway restart would silently hand each
handed-over session back to whoever created it.

Driven against REAL units on disk -- ``CrewLog.create`` plus real appends -- rather
than a stubbed reader, because what is being tested is whether the bytes a handoff
leaves behind are the bytes a start reads back.

The rebuild RECONCILES rather than fills in, and the difference is the subject of half
the cases here. A cached manager on the metadata line is not evidence and does not win
a disagreement: the log publishes, clears and overrules it. A cache that wins over the
log is how a handoff that was taken away comes back at the next boot, because the line
can be older than the log -- written before the newest handoff, or written and then
superseded by a handoff whose own cache write failed.

The fail-closed direction is the other half: a log that says nothing, a log that states
a revert, and a log that cannot be READ all leave the session with its creator, which
is the reach the gateway had before any handoff.
"""

from __future__ import annotations

import pytest
from chat_test_helpers import _make_state

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, emit, session_tree
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log import store as crew_store
from kiro_crew.crew_log.session_tree import slot_manager
from kiro_crew.dashboard import session_control as sc

GATEWAY = "gateway"

#: The target identity this suite's records and slots share. One incarnation of each
#: name, except where a test deliberately builds a second one.
_TAB = "a1b2c3d4e5f6"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    """Own data home per test, with no write armed on the real pool.

    The same isolation the adopt suite sets up, for the same reasons: the process-wide
    projection binds to ONE store, and the debounced writer saves outside the lock.

    Patched through ``_floor_monkeypatch`` because this fixture is AUTOUSE. The shared
    ``monkeypatch`` is undone as a whole by any ``monkeypatch.undo()`` a test makes to
    drop one of its own patches, which would lift this ``KIROCREW_HOME`` pin mid-test
    and point the reads below at the operator's real data home. The floor instance is
    undone independently, and a test that pins something itself still wins: its own
    ``monkeypatch`` is set up after this and torn down before it.
    """
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    _floor_monkeypatch.setattr(
        "kiro_crew.executors.maintenance_executor",
        lambda: type("_NoPool", (), {"submit": staticmethod(lambda *a, **k: None)}),
    )
    stp.reset_for_tests()
    yield
    stp.reset_for_tests()
    emit.reset_caches()


def _unit(sid: str, slot: str) -> CrewLog:
    """A real session unit for *slot*, with the opening entry a gateway writes."""
    handle = CrewLog.create(lg.KIND_SESSION, sid, owner="raymond", agent="kirocrew", slot=slot)
    handle.append(
        "session/opened",
        {
            "agent": "kirocrew",
            "slot": slot,
            "model": "opus",
            "cwd": "/w",
            "owner": "raymond",
            "resumed": False,
        },
        src=GATEWAY,
    )
    return handle


def _hand_over(
    handle: CrewLog,
    slot: str,
    manager: str,
    previous: str = "",
    *,
    tab: str = _TAB,
    manager_tab: str = "",
) -> None:
    """What the handoff verb appends at its commit point.

    *tab* is the TARGET's identity, required on the entry: a slot key is a name a
    later session can take, so a read qualified by name alone applies the grant to
    whichever session holds the name at the next start.

    *manager_tab* is the OCCUPANCY of the manager's key, which the real verb always
    records. A test that leaves it empty is pinning a record nobody can attribute to a
    live session, and the chokepoint refuses it on the identity before it reaches any
    other property -- so a test about one of those other properties has to supply it.
    """
    data = {"slot": slot, "slot_tab": tab, "manager": manager}
    if manager_tab:
        data["manager_tab"] = manager_tab
    if previous:
        data["previous"] = previous
    handle.append("session/manager_set", data, src=GATEWAY)


def _take_back(handle: CrewLog, slot: str, previous: str = "", *, tab: str = _TAB) -> None:
    """What a refused handoff appends to revert the slot to its creator.

    Spelled out here rather than taken from the emitter, so the bytes the reader folds
    are pinned independently of the code that writes them: a reader that stopped
    recognising this shape would still pass a test that asked the emitter what it emits.
    """
    data: dict[str, object] = {
        "slot": slot,
        "slot_tab": _TAB,
        "manager": "",
        "to_creator": True,
    }
    if previous:
        data["previous"] = previous
    handle.append("session/manager_set", data, src=GATEWAY)


def _slot(state, key: str, creator: str, *, handed_over: bool = True):
    """A slot as a restore builds it, MARKED as handed over unless told otherwise.

    Marked by default because this suite is about what the LOG decides, and the log is
    authoritative only for a slot whose line carries the handed-over marker. An unmarked
    slot is decided as creator-held without reading anything, which the tests that pass
    ``handed_over=False`` pin directly.
    """
    slot = state.get_or_create_slot(key)
    slot._created_by = creator
    if handed_over:
        from kiro_crew.crew_log.handed_over import record_handed_over, reset_caches

        reset_caches()
        assert record_handed_over(key)
        reset_caches()
    # The identity every record about this session is qualified by. Pinned rather
    # than left on the constructor's minted value so the entries written above and
    # the slot reading them name the same incarnation.
    slot._tab_id = _TAB
    return slot


def _manager(state, key: str, *, store: str = ""):
    """A live, ADMISSIBLE manager session under *key*.

    The rebuild re-validates a recorded grant before it publishes it, so a manager the
    log names has to be a session that could still be handed something: open, not a
    member's own DM surface, bound to the same memory store as the session it manages,
    and reachable at all. A test that skipped this would be asserting against the
    re-validation's refusal rather than against the fold it means to pin.
    """
    slot = state.get_or_create_slot(key)
    slot.memory_store = store
    return slot


async def _rebuild(state) -> int:
    return await sc.rebuild_managers_from_log(state)


def test_a_handoff_survives_a_restart(tmp_path, monkeypatch):
    """The case the rebuild exists for: the entry is on disk, so the manager comes back.

    Without this the fence would read the creator after every restart and refuse the
    conductor the worker was handed to -- the handoff would look like it never happened.
    """
    import asyncio

    _hand_over(_unit("sid-worker", "chat-worker"), "chat-worker", "chat-conductor", "chat-lead")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    _manager(state, "chat-conductor")
    assert sc.manager_of(worker) == "chat-lead", "before the rebuild the creator holds it"

    assert asyncio.run(_rebuild(state)) == 1
    assert worker._manager == "chat-conductor"
    assert sc.manager_of(worker) == "chat-conductor"


def test_an_unreadable_log_falls_back_to_the_creator(tmp_path, monkeypatch):
    """FAIL CLOSED. A read that could not see the bytes must not be treated as "no
    handoff was recorded" in one direction or "somebody else holds it" in the other:
    the session stays with its creator, which is the reach the gateway already had.

    The read is made to fail at the seam a real unreadable store fails at -- the
    complete per-unit scan -- because the portable ways to make a file unreadable
    differ by platform and this suite runs on all of them.
    """
    import asyncio

    _hand_over(_unit("sid-worker", "chat-worker"), "chat-worker", "chat-conductor")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")

    def _unreadable(directory):
        raise OSError("the unit could not be read")

    # Patched where the reader RESOLVES it: ``session_tree`` imports the name, so the
    # store's own attribute is not the one ``slot_manager`` calls.
    monkeypatch.setattr(session_tree, "find_last_manager_set", _unreadable)
    assert crew_store.find_last_manager_set is not _unreadable, "the store keeps its own"
    manager, decided = slot_manager("chat-worker", _TAB)
    assert (manager, decided) == ("", False), "an unreadable unit decides nothing"

    assert asyncio.run(_rebuild(state)) == 0
    assert worker._manager == ""
    assert sc.manager_of(worker) == "chat-lead"


def test_a_log_with_no_handoff_leaves_the_creator_in_place(tmp_path, monkeypatch):
    """The ordinary session: an entry-less log is DECIDED, and it says the creator.

    Kept beside the unreadable case so the two empties cannot be collapsed: this one
    is a fact the gateway may act on, and the other is a read that failed.
    """
    import asyncio

    _unit("sid-plain", "chat-plain")
    state = _make_state(tmp_path)
    plain = _slot(state, "chat-plain", "chat-lead")

    assert slot_manager("chat-plain", _TAB) == ("", True), "no entry is a decided answer"
    assert asyncio.run(_rebuild(state)) == 0
    assert sc.manager_of(plain) == "chat-lead"


def test_the_newest_handoff_wins(tmp_path, monkeypatch):
    """A session handed on twice is held by the manager the LAST entry names."""
    import asyncio

    handle = _unit("sid-worker", "chat-worker")
    _hand_over(handle, "chat-worker", "chat-conductor", "chat-lead")
    _hand_over(handle, "chat-worker", "chat-second", "chat-conductor")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    _manager(state, "chat-second")

    assert asyncio.run(_rebuild(state)) == 1
    assert sc.manager_of(worker) == "chat-second"


def test_an_entry_naming_another_slot_decides_nothing(tmp_path, monkeypatch):
    """The entry repeats the slot from the unit header, so the two can be compared --
    and a pair that disagrees is unprovable rather than authoritative."""
    import asyncio

    handle = _unit("sid-worker", "chat-worker")
    handle.append(
        "session/manager_set",
        {"slot": "chat-somebody-else", "slot_tab": _TAB, "manager": "chat-conductor"},
        src=GATEWAY,
    )
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")

    assert slot_manager("chat-worker", _TAB) == ("", False)
    assert asyncio.run(_rebuild(state)) == 0
    assert sc.manager_of(worker) == "chat-lead"


def test_a_cached_manager_is_overruled_by_the_log(tmp_path, monkeypatch):
    """A cached value is reconciled, not trusted. The log names a different manager, and
    the log is the authority, so the cache is replaced rather than left standing.

    The line can be OLDER than the log: it is written after the entry and the write can
    fail, so a line naming the manager from before the newest handoff is an ordinary
    outcome rather than a corrupt one.
    """
    import asyncio

    _hand_over(_unit("sid-worker", "chat-worker"), "chat-worker", "chat-second", "chat-first")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    _manager(state, "chat-second")
    worker._manager = "chat-first"

    assert asyncio.run(_rebuild(state)) == 1
    assert worker._manager == "chat-second"


def test_a_cached_manager_is_cleared_when_the_log_reverts_it(tmp_path, monkeypatch):
    """The case a statement that can only name a successor cannot express. The handoff
    was taken back, so the log says "no manager", and the rebuild has to CLEAR the
    cached value rather than find nothing to overwrite it with.

    This is the authority working in the direction that matters for authorization: the
    cache names a session the revert took off this one, and leaving it would re-grant at
    boot exactly the reach that revert removed.
    """
    import asyncio

    handle = _unit("sid-worker", "chat-worker")
    _hand_over(handle, "chat-worker", "chat-conductor", "chat-lead")
    _take_back(handle, "chat-worker", "chat-conductor")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    worker._manager = "chat-conductor"

    assert slot_manager("chat-worker", _TAB) == ("", True), "a revert is a DECIDED empty"
    assert asyncio.run(_rebuild(state)) == 1
    assert worker._manager == "", "the cached manager is gone"
    assert sc.manager_of(worker) == "chat-lead", "and the creator holds it again"


def test_a_cached_manager_is_cleared_when_the_log_holds_no_entry(tmp_path, monkeypatch):
    """A line naming a manager the log cannot show was ever granted does not survive.

    The decided-empty answer of an entry-less log is a fact about the slot, so it is
    acted on: nothing granted this manager durably, and the gateway must not behave as
    though something did.
    """
    import asyncio

    _unit("sid-worker", "chat-worker")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    worker._manager = "chat-never-recorded"

    assert asyncio.run(_rebuild(state)) == 1
    assert worker._manager == ""
    assert sc.manager_of(worker) == "chat-lead"


def test_an_unreadable_log_leaves_the_slot_UNRESOLVED_rather_than_creator_owned(
    tmp_path, monkeypatch
):
    """An undecided read establishes NOTHING, and must not collapse into a decided answer.

    Empty means "the creator holds it", which is a claim -- and a read that could not see
    the bytes cannot make it. Collapsing the two is what lets a revoked creator back in:
    the entry moving the reach away may be sitting in exactly the part the read missed.

    So the slot stays unresolved and session control refuses the verbs whose
    authorization reads the manager, until an authoritative read succeeds. Refusing is
    the only answer available: both decided answers would be guesses.
    """
    import asyncio

    _hand_over(_unit("sid-worker", "chat-worker"), "chat-worker", "chat-conductor")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    _manager(state, "chat-conductor")
    worker._manager = "chat-conductor"

    def _unreadable(directory):
        raise OSError("the unit could not be read")

    monkeypatch.setattr(session_tree, "find_last_manager_set", _unreadable)
    assert slot_manager("chat-worker", _TAB) == ("", False), "the read decides nothing"

    assert asyncio.run(_rebuild(state)) == 0, "nothing is published from a failed read"
    assert worker._manager_unresolved is True, "the slot is unresolved, not creator-owned"
    # The KEY goes with the flag, because the four attributes that say who manages a
    # slot are one statement and ``publish_manager`` is the one writer of it. Keeping a
    # key beside "this was never read" would be half of one fact beside half of
    # another, which is the shape four separate findings on this line came out of.
    # Nothing is lost: every authorization reader is refused on the flag, the write
    # path is refused too, and the re-read the refusal arms is what restores the key.
    assert worker._manager == "", "the unread state carries no key either"
    # The refusal this flag produces is asserted in the compensation suite, which has the
    # live session map ``authorize_target`` needs to get as far as a target.


def test_a_store_that_was_never_written_is_a_decided_answer(tmp_path, monkeypatch):
    """A store that does NOT EXIST is not a failed read. Nothing was ever recorded there,
    so no handoff can exist and the creator holds the slot.

    Kept apart from the unreadable case above because a first run has no crew-log store
    at all: a reader that treated absence as undecided would refuse every slot on a
    gateway that has never used the feature, where the answer is actually known.
    """
    import asyncio

    def _absent(_slot, *, strict=False):
        raise FileNotFoundError("the store has never been written")

    monkeypatch.setattr(session_tree, "session_units_for_slot", _absent)
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")

    assert slot_manager("chat-worker", _TAB) == ("", True), "absence is decided"
    assert asyncio.run(_rebuild(state)) == 0
    assert worker._manager_unresolved is False, "so the slot is servable"
    assert sc.manager_of(worker) == "chat-lead"


def test_a_cache_that_agrees_with_the_log_is_not_counted_as_moved(tmp_path, monkeypatch):
    """The ordinary restart. The line and the log say the same thing, so nothing moves
    and the count says so -- which is what makes the count usable as a signal that a
    cache and the authority disagreed."""
    import asyncio

    _hand_over(_unit("sid-worker", "chat-worker"), "chat-worker", "chat-conductor")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    _manager(state, "chat-conductor")
    worker._manager = "chat-conductor"

    assert asyncio.run(_rebuild(state)) == 0
    assert worker._manager == "chat-conductor"


def test_emission_being_off_does_not_retire_a_recorded_grant(tmp_path, monkeypatch):
    """EMISSION and READABILITY are different facts, and only the second one decides.

    Switching the crew log off stops new entries. It does not move, hide or invalidate
    a single entry already on disk -- and the read below proves that by finding the same
    grant with emission off that it finds with it on. So a rebuild that read the enabled
    flag and concluded "no handoff is recorded" was answering a question nobody asked,
    with the one answer that hands the giver back a reach the log took away. An operator
    turning logging off is an ordinary config change, not a revocation.

    The marker makes the inference visibly wrong rather than merely unsupported: a
    MARKED slot is one whose handoff committed, so "nothing was ever recorded" is
    contradicted by the slot's own line.

    What a gateway with no crew log at all looks like is the test below this one: the
    store is absent, the reader says so, and THAT is decided.
    """
    import asyncio

    _hand_over(_unit("sid-worker", "chat-worker"), "chat-worker", "chat-conductor")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    _manager(state, "chat-conductor")
    plain = _slot(state, "chat-plain", "chat-lead")
    worker._manager = "chat-conductor"
    monkeypatch.setattr(emit, "enabled", lambda: False)
    assert slot_manager("chat-worker", _TAB) == (
        "chat-conductor",
        True,
    ), "the bytes are still there"

    assert asyncio.run(_rebuild(state)) == 0, "the log agrees with the line, so nothing moves"
    assert worker._manager == "chat-conductor", "the recorded grant stands"
    assert sc.manager_of(worker) == "chat-conductor", "and the giver does not get it back"
    assert plain._manager == "", "a slot the log says nothing about is creator-held"


# -- a recorded grant is RE-VALIDATED before it is published ------------------


def test_a_recorded_manager_rebound_to_another_store_is_refused(tmp_path, monkeypatch):
    """The log is the authority on WHAT was recorded, never on whether it is still
    ALLOWED. A recipient admitted because it shared the giver's memory store can be
    rebound to another principal's store while the gateway is DOWN, and no append-only
    entry can know that -- so the admission is re-asked at the one point the entry would
    otherwise take effect.

    This is also what closes the gap the verb's corrective append leaves: a correction
    that never landed, or a crash between the entry and its correction, leaves the log
    stating a grant that was refused. Re-validating catches every one of those without
    depending on a second write having succeeded.
    """
    import asyncio

    _hand_over(
        _unit("sid-worker", "chat-worker"),
        "chat-worker",
        "chat-conductor",
        manager_tab="conductor-tab",
    )
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    worker.memory_store = ""
    # Rebound while the gateway was down: the entry on disk still names it.
    conductor = _manager(state, "chat-conductor", store="member-raymond")
    conductor._tab_id = "conductor-tab"

    assert slot_manager("chat-worker", _TAB) == ("chat-conductor", True), "the log still says so"
    assert asyncio.run(_rebuild(state)) == 1, "the record is published, not discarded"
    assert worker._manager == "chat-conductor", "the field carries what the log records"
    with pytest.raises(sc.SessionControlError) as refused:
        sc.require_manager_of(state, worker)
    assert refused.value.code == "manager_grant_inadmissible", (
        "and the chokepoint refuses it where the record is APPLIED -- the restore "
        "states what the log says and judges nothing"
    )
    assert worker._manager_unresolved is False, "this read succeeded"
    # DENIED, and not reverted to the creator: the log recorded the session going away
    # and never recorded it coming back, so handing it to the giver would invent a
    # transfer nobody made.
    with pytest.raises(sc.SessionControlError) as exc:
        sc.require_manager_of(state, worker)
    assert exc.value.code == "manager_grant_inadmissible"


def test_a_recorded_manager_that_is_no_longer_open_is_refused(tmp_path, monkeypatch):
    """A key names nothing once its session is gone, and a session that is not there
    cannot drive anything. Fail closed rather than publish a manager the gateway could
    not even look at."""
    import asyncio

    _hand_over(_unit("sid-worker", "chat-worker"), "chat-worker", "chat-conductor")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    # No slot for the recorded manager: it was closed before this start.

    assert asyncio.run(_rebuild(state)) == 1, "the record is published, not discarded"
    assert worker._manager == "chat-conductor", "the field carries what the log records"
    with pytest.raises(sc.SessionControlError) as refused:
        sc.require_manager_of(state, worker)
    assert refused.value.code == "manager_grant_inadmissible", (
        "a closed manager is a grant that cannot be held now, which is what that code "
        "says and what the caller must act on -- hand it over again"
    )
    assert worker._manager_unresolved is False, "this read succeeded"
    # DENIED, and not reverted to the creator: the log recorded the session going away
    # and never recorded it coming back, so handing it to the giver would invent a
    # transfer nobody made.
    with pytest.raises(sc.SessionControlError) as exc:
        sc.require_manager_of(state, worker)
    assert exc.value.code == "manager_grant_inadmissible"


def test_a_recorded_manager_that_went_channel_linked_is_refused(tmp_path, monkeypatch):
    """The same target-side containment the admission applied. A channel-linked session
    mirrors its conversation to Slack or Telegram, so it is not addressable at all --
    and a grant that predates the link must not survive it."""
    import asyncio

    _hand_over(
        _unit("sid-worker", "chat-worker"),
        "chat-worker",
        "chat-conductor",
        manager_tab="conductor-tab",
    )
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    conductor = _manager(state, "chat-conductor")
    conductor._tab_id = "conductor-tab"
    conductor.linked_session_key = "slack:C123"

    assert asyncio.run(_rebuild(state)) == 1, "the record is published, not discarded"
    assert worker._manager == "chat-conductor", "the field carries what the log records"
    with pytest.raises(sc.SessionControlError) as refused:
        sc.require_manager_of(state, worker)
    assert refused.value.code == "manager_grant_inadmissible", (
        "and the chokepoint refuses it where the record is APPLIED -- the restore "
        "states what the log says and judges nothing"
    )
    assert worker._manager_unresolved is False, "this read succeeded"
    # DENIED, and not reverted to the creator: the log recorded the session going away
    # and never recorded it coming back, so handing it to the giver would invent a
    # transfer nobody made.
    with pytest.raises(sc.SessionControlError) as exc:
        sc.require_manager_of(state, worker)
    assert exc.value.code == "manager_grant_inadmissible"


def test_a_recorded_manager_on_a_member_dm_surface_is_refused(tmp_path, monkeypatch):
    """Refused by KEY, as it is at admission: a crew member's own DM surface is the
    member's operating surface and not a session work runs in. No entry could make it
    one, so no entry is trusted to."""
    import asyncio

    from kiro_crew.members import DM_SLOT_KEY_PREFIX

    dm = f"{DM_SLOT_KEY_PREFIX}raymond"
    _hand_over(_unit("sid-worker", "chat-worker"), "chat-worker", dm)
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    # No slot is created for it, deliberately: the prefix refusal is read off the KEY,
    # so it has to answer before anything asks whether a session is open there.

    assert slot_manager("chat-worker", _TAB) == (dm, True), "the log records it"
    assert asyncio.run(_rebuild(state)) == 1, "the record is published, not discarded"
    assert worker._manager == dm, "the field carries what the log records"
    with pytest.raises(sc.SessionControlError) as refused:
        sc.require_manager_of(state, worker)
    assert refused.value.code == "manager_grant_inadmissible", (
        "and the chokepoint refuses it where the record is APPLIED -- the restore "
        "states what the log says and judges nothing"
    )
    assert worker._manager_unresolved is False, "this read succeeded"
    # DENIED, and not reverted to the creator: the log recorded the session going away
    # and never recorded it coming back, so handing it to the giver would invent a
    # transfer nobody made.
    with pytest.raises(sc.SessionControlError) as exc:
        sc.require_manager_of(state, worker)
    assert exc.value.code == "manager_grant_inadmissible"


# -- units are ordered by succession, not by the wall clock -------------------


def test_a_backward_clock_does_not_restore_the_revoked_manager(tmp_path, monkeypatch):
    """This read takes the first answer from the NEWEST unit, so which unit is newest
    decides which manager comes back -- and ``createdAt`` is a wall-clock stamp.

    A clock that steps backward between two units of one slot (an NTP correction, a VM
    resume) sorts the genuinely newer unit BEFORE its predecessor. The read then walks
    the real newest first, finds the OLD handoff, and restores a manager a later handoff
    already revoked. The store wrote what followed what: each ``session/opened`` names
    the unit it replaced, so the chain is asked instead of the clock.

    Staged with the predecessor stamped LATER than its successor, which is exactly what
    a backward step produces.
    """
    import asyncio

    first = _unit("sid-old", "chat-worker")
    _hand_over(first, "chat-worker", "chat-first-manager")
    second = CrewLog.create(
        lg.KIND_SESSION, "sid-new", owner="raymond", agent="kirocrew", slot="chat-worker"
    )
    second.append(
        "session/opened",
        {
            "agent": "kirocrew",
            "slot": "chat-worker",
            "model": "opus",
            "cwd": "/w",
            "owner": "raymond",
            "resumed": False,
            # The DURABLE supersede edge: the opening entry names the unit this one
            # replaced, which is the chain the ordering asks instead of the clock.
            "previous": {"sid": "sid-old"},
        },
        src=GATEWAY,
    )
    _hand_over(second, "chat-worker", "chat-second-manager", "chat-first-manager")

    # The clock stepped BACK, so the store's own header-ordered listing comes back with
    # the SUCCESSOR first -- which is what the read would walk as "newest unit" if it
    # trusted that order. Patched at the listing because that is where the clock enters:
    # the units themselves record the chain correctly.
    real_listing = session_tree.session_units_for_slot

    def _clock_inverted(slot_key, *, strict=False):
        listed = real_listing(slot_key, strict=strict)
        if set(listed) == {"sid-old", "sid-new"}:
            return ("sid-new", "sid-old")
        return listed

    monkeypatch.setattr(session_tree, "session_units_for_slot", _clock_inverted)
    assert (
        session_tree.session_units_for_slot("chat-worker", strict=True)[-1] == "sid-old"
    ), "the listing names the predecessor last, which is the inversion under test"

    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    _manager(state, "chat-second-manager")
    _manager(state, "chat-first-manager")

    manager, decided = slot_manager("chat-worker", _TAB)
    assert (manager, decided) == (
        "chat-second-manager",
        True,
    ), "the successor's handoff wins, not the one the clock calls newest"
    assert asyncio.run(_rebuild(state)) == 1
    assert worker._manager == "chat-second-manager"


# ── One damaged unit must not take out an unrelated slot ─────────────────────


def _break_the_header(sid: str) -> str:
    """Corrupt one unit's header line while leaving its entries intact.

    What an established unit looks like when its header will not prove: the
    directory is there, the segments are there and hold entries, and line 1 does not
    parse. ``create`` publishes the header atomically, so this is the fault a
    transient read error or a damaged first line leaves behind, not a torn write.
    """
    directory = crew_store.crew_log_dir(lg.KIND_SESSION, sid)
    segment = crew_store.oldest_segment(directory)
    assert segment is not None
    lines = segment.read_text(encoding="utf-8").split("\n")
    lines[0] = "{not json at all"
    segment.write_text("\n".join(lines), encoding="utf-8")
    crew_store._slot_index = None
    return directory.name


def test_a_corrupt_unit_elsewhere_does_not_refuse_an_unrelated_slots_listing(tmp_path):
    """The blast radius, which is the half of this that is not about convergence.

    The strict listing exists so a caller that VALIDATES against it cannot pass
    against a record that is quietly short. Refused store-wide, one damaged directory
    makes every slot's listing incomplete -- so a slot whose own log reads perfectly
    comes up unresolved and its manager-dependent verbs are refused until somebody
    repairs an unrelated session. The incompleteness has nothing to do with that slot.

    An unaccounted unit is skipped only when its own opening entry attributes it
    ELSEWHERE, which is a line-2 read and does not need the header that failed.
    """
    _unit("sid-worker", "chat-worker")
    _unit("sid-stranger", "chat-stranger")
    _break_the_header("sid-stranger")

    # The cross-slot caller still refuses: validating across slots is what the
    # store-wide refusal is for.
    with pytest.raises(Exception):
        crew_store.session_units_by_slot(strict=True)

    # The per-slot caller is unaffected, because the damage is attributable elsewhere.
    assert crew_store.session_units_for_slot("chat-worker", strict=True) == ("sid-worker",)
    assert slot_manager("chat-worker", _TAB) == ("", True), "decided, so the slot is servable"


def test_a_corrupt_unit_of_the_slots_own_log_still_refuses(tmp_path):
    """The narrowing must not become a way to ignore the caller's own damage."""
    _unit("sid-worker", "chat-worker")
    _unit("sid-worker-2", "chat-worker")
    _break_the_header("sid-worker-2")

    with pytest.raises(Exception):
        crew_store.session_units_for_slot("chat-worker", strict=True)
    assert slot_manager("chat-worker", _TAB) == ("", False), "undecided, so the slot is refused"


def test_an_unattributable_corrupt_unit_refuses_every_slot(tmp_path):
    """Fail closed where attribution itself fails.

    A unit whose header will not prove AND whose opening entry cannot be read says
    nothing about which slot it belongs to -- and "which slot is this" is exactly the
    question the narrowing answers. It may hold the entry this caller came for, so it
    refuses, which is the same posture every unreadable state on this path takes.
    """
    _unit("sid-worker", "chat-worker")
    _unit("sid-mystery", "chat-mystery")
    directory = crew_store.crew_log_dir(lg.KIND_SESSION, "sid-mystery")
    segment = crew_store.oldest_segment(directory)
    assert segment is not None
    # Both the header AND the opening entry unreadable: nothing attributes it.
    segment.write_text("{not json at all\n{nor is this\n", encoding="utf-8")
    crew_store._slot_index = None

    with pytest.raises(Exception):
        crew_store.session_units_for_slot("chat-worker", strict=True)


def test_a_header_of_undecodable_bytes_still_lets_line_two_attribute_the_unit(tmp_path):
    """A line is damaged in BYTES, not only in syntax, and the lines fail separately.

    "Will not prove" covers a header that is not valid UTF-8 at all, which is what a
    flipped bit or a truncated write leaves. Attribution reads line 2, and line 2 is
    intact -- so a reader that decodes the whole head at once fails on line 1 and
    takes the readable line down with it. Every strict per-slot listing in the store
    then raises instead of answering, and the manager reads above it turn an
    attributable stranger into a refusal for a slot whose own log is perfect.

    Which is the opposite of the narrowing's purpose, so the bytes are read first and
    decoded a line at a time: a line that cannot be decoded does not answer, exactly
    like a line that cannot be parsed, and the next line still gets its turn.
    """
    _unit("sid-worker", "chat-worker")
    _unit("sid-stranger", "chat-stranger")
    directory = crew_store.crew_log_dir(lg.KIND_SESSION, "sid-stranger")
    segment = crew_store.oldest_segment(directory)
    assert segment is not None
    opening = segment.read_bytes().split(b"\n")[1]
    assert b"session/opened" in opening, "line 2 is the entry that attributes the unit"
    segment.write_bytes(b"\xff\xfe{not even utf-8\n" + opening + b"\n")
    crew_store._slot_index = None

    # The stranger is attributable from line 2, so this slot's own listing answers.
    assert crew_store.session_units_for_slot("chat-worker", strict=True) == ("sid-worker",)
    assert slot_manager("chat-worker", _TAB) == ("", True), "decided, so the slot is servable"
    # And the cross-slot caller still refuses, because the unit is still unprovable.
    with pytest.raises(Exception):
        crew_store.session_units_by_slot(strict=True)


# ── An unreadable RECORD is undecided, like an unreadable index ──────────────
#
# The read walks a unit's entries newest first and takes the first ``manager_set`` it
# finds. A line that does not PARSE cannot be asked what type it was, so passing over
# one treats bytes nobody could read as "not a handoff" -- and when such a line sits
# newer than a readable entry, the older entry comes back as the file's newest
# statement. That entry is the one a later handoff superseded, so the fail-open reading
# does not lose a manager: it RESTORES the revoked one, with nothing visibly wrong
# anywhere above it. One flipped bit does that; no attacker is required.
#
# It is the same rule the handed-over index is read by -- an index that exists and
# cannot be read refuses every slot -- and the two cannot differ, because they are two
# faces of one question asked of the same store. The blast radius is the same too: the
# verbs a handoff widens refuse, retryably, until the log is repaired.


def _log_of(sid: str):
    """The unit's own log file, which is where these tests do their damage."""
    directory = crew_store.crew_log_dir(lg.KIND_SESSION, sid)
    segment = crew_store.oldest_segment(directory)
    assert segment is not None, sid
    return segment


def _flip_last_byte_of(sid: str, *, nth_from_newest: int = 0) -> bytes:
    """Damage one record of *sid*'s log past parsing. Returns the bytes it held.

    A SINGLE BYTE, and the last one of the line, so the damage is the shape bit decay
    leaves rather than a rewrite: the closing brace becomes a comma and the JSON object
    is unterminated. *nth_from_newest* picks which record to hit, counting back from the
    end, so a test can put the damage newer or older than the entry under test.
    """
    segment = _log_of(sid)
    lines = segment.read_bytes().split(b"\n")
    records = [index for index, line in enumerate(lines) if line.strip()]
    target = records[-1 - nth_from_newest]
    held = lines[target]
    assert held.endswith(b"}"), held[-40:]
    lines[target] = held[:-1] + b","
    segment.write_bytes(b"\n".join(lines))
    crew_store._slot_index = None
    return held


def test_an_unreadable_record_newer_than_a_handoff_does_not_restore_the_revoked_manager(
    tmp_path,
):
    """The finding, driven. A damaged newer record must not resurrect the older grant.

    Two handoffs: the session goes to A, then to B, so A's grant is REVOKED and B's is
    the log's statement. Damage B's record and the question is which way the read fails.
    Passing over the line answers A -- the manager the second handoff took the session
    away from -- and nothing anywhere reports a problem, because from the reader's side
    A's entry is simply the newest ``manager_set`` it could see.

    So the record is undecided, the slot comes up unresolved, and the manager-dependent
    verbs refuse with a retryable code until the log is repaired. The assertion that
    matters is the NEGATIVE one: not that the answer is empty, but that it is not A.
    """
    import asyncio

    handle = _unit("sid-worker", "chat-worker")
    _hand_over(handle, "chat-worker", "chat-a", "chat-lead")
    _hand_over(handle, "chat-worker", "chat-b", "chat-a")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    _manager(state, "chat-a")
    _manager(state, "chat-b")
    assert slot_manager("chat-worker", _TAB) == ("chat-b", True), "the premise: B holds it"

    damaged = _flip_last_byte_of("sid-worker")
    assert b"chat-b" in damaged, "the record damaged is the one that revoked A's grant"

    manager, decided = slot_manager("chat-worker", _TAB)
    assert manager != "chat-a", "a damaged record must not hand the grant back to A"
    assert (manager, decided) == ("", False), "it is undecided, not an answer"

    assert asyncio.run(_rebuild(state)) == 0
    assert worker._manager == "", "nothing was published from a read that did not decide"
    assert worker._manager_unresolved is True, "the slot waits on its own log"
    with pytest.raises(sc.SessionControlError) as exc:
        sc.require_manager_of(state, worker)
    assert exc.value.code == "manager_unresolved", "and the refusal is the retryable one"


def test_an_unreadable_record_older_than_the_newest_handoff_still_resolves(tmp_path):
    """The other direction, so undecided does not become "any damage anywhere refuses".

    A record OLDER than the newest ``manager_set`` cannot change which statement is
    newest, so it cannot restore anything: the reader meets the answer first and stops.
    Refusing on it would spend availability and buy no containment, and this is the
    common case on a long-lived log -- damage lands wherever it lands, and most of a
    log is older than its last handoff.
    """
    import asyncio

    handle = _unit("sid-worker", "chat-worker")
    _hand_over(handle, "chat-worker", "chat-a", "chat-lead")
    _hand_over(handle, "chat-worker", "chat-b", "chat-a")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    _manager(state, "chat-b")

    # The record A's grant was written in, which the newer one already superseded.
    damaged = _flip_last_byte_of("sid-worker", nth_from_newest=1)
    assert b"chat-a" in damaged

    assert slot_manager("chat-worker", _TAB) == ("chat-b", True), "the newest entry answers"
    assert asyncio.run(_rebuild(state)) == 1
    assert worker._manager == "chat-b"
    assert worker._manager_unresolved is False


def test_a_damaged_record_in_another_sessions_unit_leaves_this_one_resolving(tmp_path):
    """The blast radius, stated as a measurement. One session's damage is one session's.

    A store-wide refusal would fence the whole gateway on one session's bad byte, which
    is an availability cliff rather than a containment gain: the grant a damaged log
    puts at risk is the one recorded in THAT log. The read is scoped to the units of the
    slot being asked about, so this holds by construction -- and that is worth pinning,
    because the natural way to implement the refusal above is a flag on the store.
    """
    import asyncio

    _hand_over(_unit("sid-worker", "chat-worker"), "chat-worker", "chat-conductor", "chat-lead")
    other = _unit("sid-stranger", "chat-stranger")
    _hand_over(other, "chat-stranger", "chat-someone-else")
    damaged = _flip_last_byte_of("sid-stranger")
    assert b"chat-someone-else" in damaged

    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    _manager(state, "chat-conductor")

    assert slot_manager("chat-worker", _TAB) == ("chat-conductor", True), "its own log is clean"
    assert asyncio.run(_rebuild(state)) == 1
    assert worker._manager == "chat-conductor"
    assert worker._manager_unresolved is False
    # And the damaged session is the one that is refused.
    assert slot_manager("chat-stranger", _TAB) == ("", False)


def test_the_complete_scan_refuses_a_damaged_record_the_window_never_reached(tmp_path):
    """The same rule on the OTHER read, which is the one a long log actually uses.

    Past the tail window the bounded read answers nothing and the complete scan runs
    instead. It streams forward and keeps the last match, so "newer than the answer" is
    "met after it" -- and a scan that passed over a damaged line there would serve the
    superseded entry with the same confidence the bounded read would have.

    Driven at the scan rather than through a 72 KiB log: what is being pinned is the
    scan's own answer to a damaged line, and the reader above it already turns a raise
    into undecided.
    """
    segment = tmp_path / "log.jsonl"
    # Line 1 is the HEADER every real segment carries, and the scan skips it BY
    # POSITION: it is a header object, not an entry, so a strict reader that judged it
    # as a record would refuse every unit. A synthetic file without one would not be
    # the shape under test.
    header = '{"type":"session","version":1,"id":"sid-worker","slot":"chat-worker"}'
    filler = '{"type":"turn/started","seq":9,"time":1,"src":"gateway","data":{"text":"%s"}}' % (
        "x" * 200,
    )
    older_manager = (
        '{"type":"session/manager_set","seq":1,"time":1,"src":"gateway",'
        '"data":{"slot":"chat-worker","slot_tab":"%s","manager":"chat-a"}}' % _TAB
    )
    newest_manager = (
        '{"type":"session/manager_set","seq":2,"time":2,"src":"gateway",'
        '"data":{"slot":"chat-worker","slot_tab":"%s","manager":"chat-b"}}' % _TAB
    )
    records = [header, older_manager, *[filler] * 400, newest_manager, *[filler] * 400]
    newest_at = records.index(newest_manager)
    segment.write_text("\n".join(records) + "\n", encoding="utf-8")
    assert segment.stat().st_size > crew_store._TAIL_WINDOW, "the premise: past the window"

    found = crew_store._scan_whole_for_types(segment, crew_store._MANAGER_TYPES, strict=True)
    assert found is not None and found.data["manager"] == "chat-b", "the premise: B is newest"

    # Damage B's record, leaving A's readable and older.
    damaged = records.copy()
    damaged[newest_at] = damaged[newest_at][:-1] + ","
    segment.write_text("\n".join(damaged) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        crew_store._scan_whole_for_types(segment, crew_store._MANAGER_TYPES, strict=True)

    # And damage A's instead: older than the answer, so the answer still stands.
    older = records.copy()
    older[1] = older[1][:-1] + ","
    segment.write_text("\n".join(older) + "\n", encoding="utf-8")
    still = crew_store._scan_whole_for_types(segment, crew_store._MANAGER_TYPES, strict=True)
    assert still is not None and still.data["manager"] == "chat-b"


def test_retention_and_the_tree_edge_keep_their_own_reading_of_a_damaged_line():
    """The strictness is the MANAGER read's, not the shared helper's default.

    Three readers share the window walk and each brings its own type set. Only the
    manager read restores a revoked authorization by stopping one line short, so only it
    is strict; retention's open-versus-closed answer authorizes a DELETE and the tree
    edge decides where a session hangs, and flipping their direction here would be a
    change to two subsystems this line never examined. Pinned so the default cannot be
    switched for all three by someone who read only this file.
    """
    import inspect

    source = inspect.getsource(crew_store)
    for reader, expected in (
        ("_last_entry_of_types(tail, _LIFECYCLE_TYPES)", True),
        ("_last_entry_of_types(tail, _TREE_EDGE_TYPES)", True),
        ("_last_entry_of_types(tail, _MANAGER_TYPES, strict=True)", True),
    ):
        assert (reader in source) is expected, reader
    signature = inspect.signature(crew_store._last_entry_of_types)
    assert signature.parameters["strict"].default is False, (
        "the skipping reading stays the default: the two readers that keep it are not "
        "the ones whose fail-open answer restores a grant"
    )


def _rewrite_newest_record(sid: str, replacement: str) -> bytes:
    """Replace the newest record of *sid*'s log with *replacement*. Returns what it held.

    For the damage shapes a byte flip cannot produce: a record that still parses as JSON
    but is not a well-formed entry, or one whose ``type`` field is the damaged part.
    """
    segment = _log_of(sid)
    lines = segment.read_bytes().split(b"\n")
    records = [index for index, line in enumerate(lines) if line.strip()]
    target = records[-1]
    held = lines[target]
    lines[target] = replacement.encode("utf-8")
    segment.write_bytes(b"\n".join(lines))
    crew_store._slot_index = None
    return held


def test_a_torn_tail_does_not_restore_the_revoked_manager(tmp_path):
    """The layer BEFORE the strict judgement: bytes dropped before anything judged them.

    ``strict`` decides which records in the window account for themselves, and the
    scanner cuts unterminated trailing bytes that do not parse OUT of the window first
    -- so no strictness applied inside the window can see them. They are also the
    NEWEST bytes in the file, which is exactly where a statement superseding every
    other one would sit: a half-written ``manager_set`` passed over hands back the
    entry it replaced.

    Produced the way a crash produces it, by truncating the last record mid-line rather
    than by rewriting it: this is the one damage shape that needs no editor.
    """
    import asyncio

    handle = _unit("sid-worker", "chat-worker")
    _hand_over(handle, "chat-worker", "chat-a", "chat-lead", manager_tab="a-tab")
    _hand_over(handle, "chat-worker", "chat-b", "chat-a", manager_tab="b-tab")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    _manager(state, "chat-a")
    _manager(state, "chat-b")
    assert slot_manager("chat-worker", _TAB) == ("chat-b", True), "the premise: B holds it"

    segment = _log_of("sid-worker")
    raw = segment.read_bytes()
    body, _, last = raw.rstrip(b"\n").rpartition(b"\n")
    assert b"chat-b" in last, "the record being torn is the one that revoked A's grant"
    # Half a line, unterminated: what a crash mid-append leaves behind.
    segment.write_bytes(body + b"\n" + last[: len(last) // 2])
    crew_store._slot_index = None

    manager, decided = slot_manager("chat-worker", _TAB)
    assert manager != "chat-a", "the torn tail must not hand the grant back to A"
    assert (manager, decided) == ("", False), "it is undecided, not an answer"

    assert asyncio.run(_rebuild(state)) == 0
    assert worker._manager == ""
    assert worker._manager_unresolved is True


@pytest.mark.parametrize(
    ("shape", "replacement"),
    [
        # PARSES AS JSON, refused by the entry schema. ``seq`` must be a positive int,
        # so a zero reads as a record that is syntactically fine and is not an entry --
        # which a reader that only asked "does this parse" passed over exactly as the
        # skipping reading passed over bytes that did not parse.
        (
            "a seq the schema refuses",
            '{"type":"session/manager_set","seq":0,"time":2,"src":"gateway",'
            '"data":{"slot":"chat-worker","slot_tab":"%s","manager":"chat-b"}}' % _TAB,
        ),
        # The envelope is incomplete rather than wrong: no ``src``.
        (
            "a missing envelope field",
            '{"type":"session/manager_set","seq":2,"time":2,'
            '"data":{"slot":"chat-worker","slot_tab":"%s","manager":"chat-b"}}' % _TAB,
        ),
        # ``data`` is not an object, so nothing about the statement can be read.
        (
            "a data that is not an object",
            '{"type":"session/manager_set","seq":2,"time":2,"src":"gateway","data":"chat-b"}',
        ),
        # THE TYPE FIELD IS THE DAMAGED PART. ``Entry.from_dict`` checks only that it is
        # a non-empty string, so this is a valid entry that simply fails to match the
        # manager set -- and "it did not match" then reads as "it is not a manager
        # entry", which is the same defect wearing a third face.
        (
            "a type damaged out of the vocabulary",
            '{"type":"session/manager_se","seq":2,"time":2,"src":"gateway",'
            '"data":{"slot":"chat-worker","slot_tab":"%s","manager":"chat-b"}}' % _TAB,
        ),
        # THE IDENTITY FIELD'S KEY IS THE DAMAGED PART, and this one passes the schema:
        # ``slot_tab`` is not required by the envelope, so the entry is well formed and
        # simply carries no identity the reader can find. A two-valued comparison then
        # reads it as "written for a different incarnation" and passes it over, which is
        # the same defect a layer deeper than the type field.
        (
            "the identity field's key damaged",
            '{"type":"session/manager_set","seq":2,"time":2,"src":"gateway",'
            '"data":{"slot":"chat-worker","slot_tac":"%s","manager":"chat-b"}}' % _TAB,
        ),
        # The identity is PRESENT and empty, which is the same unprovable position
        # reached without damaging a name at all.
        (
            "an empty recorded identity",
            '{"type":"session/manager_set","seq":2,"time":2,"src":"gateway",'
            '"data":{"slot":"chat-worker","slot_tab":"","manager":"chat-b"}}',
        ),
    ],
)
def test_a_record_that_does_not_establish_itself_does_not_restore_the_revoked_manager(
    tmp_path, shape, replacement
):
    """Every way a record fails to say what it is, driven to the same answer.

    "Unparseable" was too narrow a word for this rule, and each narrowing left the same
    defect alive one layer down: a record the reader cannot account for, sitting newer
    than a readable ``manager_set``, makes the older entry read as the file's newest
    statement -- and that entry is the one a later handoff revoked.

    The shapes below are every layer found so far: JSON that the entry schema refuses,
    an envelope missing a required field, a ``data`` that is not an object, a ``type``
    damaged out of the declared vocabulary, and -- past the schema entirely -- an entry
    that is well formed and whose recorded IDENTITY cannot be established, either
    because the field's key is damaged or because the field is empty. None of them is
    skippable, because none of them can be asked whether it was the handoff.

    The last two are why the identity comparison answers three values rather than two
    (:func:`~kiro_crew.crew_log.identity.compare_identity`): a record that cannot say
    which session it is about is not a record about somebody else, and only the second
    may be passed over.
    """
    import asyncio

    handle = _unit("sid-worker", "chat-worker")
    _hand_over(handle, "chat-worker", "chat-a", "chat-lead")
    _hand_over(handle, "chat-worker", "chat-b", "chat-a")
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker", "chat-lead")
    _manager(state, "chat-a")
    _manager(state, "chat-b")
    assert slot_manager("chat-worker", _TAB) == ("chat-b", True), "the premise: B holds it"

    held = _rewrite_newest_record("sid-worker", replacement)
    assert b"chat-b" in held, "the record replaced is the one that revoked A's grant"

    manager, decided = slot_manager("chat-worker", _TAB)
    assert manager != "chat-a", f"{shape} must not hand the grant back to A"
    assert (manager, decided) == ("", False), f"{shape} is undecided, not an answer"

    assert asyncio.run(_rebuild(state)) == 0
    assert worker._manager == ""
    assert worker._manager_unresolved is True
    with pytest.raises(sc.SessionControlError) as exc:
        sc.require_manager_of(state, worker)
    assert exc.value.code == "manager_unresolved"


def test_a_declared_entry_of_another_type_is_not_a_record_that_failed():
    """The line between "cannot account for" and "accounted for, and not a handoff".

    A log is mostly other entry types, so a rule that refused on any non-match would
    refuse everything. A well-formed entry of a DECLARED type says what it is, and that
    is the whole difference: the reader skips it because the record answered, not
    because the reader gave up on it.
    """
    established = crew_store._entry_if_established(
        b'{"type":"turn/started","seq":2,"time":2,"src":"gateway","data":{}}'
    )
    assert established is not None and established.type == "turn/started"

    assert crew_store._entry_if_established(b"{not json") is None
    assert (
        crew_store._entry_if_established(
            b'{"type":"turn/started","seq":0,"time":2,"src":"gateway","data":{}}'
        )
        is None
    ), "the schema refused it, so it established nothing"
    assert (
        crew_store._entry_if_established(
            b'{"type":"turn/starteD","seq":2,"time":2,"src":"gateway","data":{}}'
        )
        is None
    ), "an undeclared type is a record this reader cannot account for"


def test_a_writer_that_marks_an_entry_ignorable_keeps_a_newer_reader_working():
    """The forward-compatibility seam, which is the writer's promise and not a guess.

    Refusing every type this reader does not know would make an older gateway refuse
    manager resolution on a log a newer one wrote. ``ignorable`` is the format's own
    answer: the WRITER states that nothing depends on interpreting the entry, so a
    reader that does not know the type may skip it. An undeclared type WITHOUT the
    marker gets no such promise and is refused.
    """
    marked = crew_store._entry_if_established(
        b'{"type":"session/from_a_newer_writer","seq":2,"time":2,"src":"gateway",'
        b'"data":{},"ignorable":true}'
    )
    assert marked is not None and marked.type == "session/from_a_newer_writer"

    # Only a literal ``true`` relaxes the guard, which is the schema's own rule.
    assert (
        crew_store._entry_if_established(
            b'{"type":"session/from_a_newer_writer","seq":2,"time":2,"src":"gateway",'
            b'"data":{},"ignorable":"yes"}'
        )
        is None
    )


def test_a_type_damaged_into_another_declared_type_is_out_of_a_readers_reach():
    """The residual, pinned so it is not mistaken for coverage.

    A ``type`` damaged into a DIFFERENT DECLARED type produces a well-formed,
    self-consistent record of that type. No reader can tell it from a genuine one, so
    this is not something the rule above closes -- only per-record integrity would, and
    that is a format change rather than a reader's to make. Written down as a test
    because the vocabulary strings share long prefixes, which makes this residual the
    likelier half of type damage rather than the remote one.
    """
    looks_fine = crew_store._entry_if_established(
        b'{"type":"turn/started","seq":2,"time":2,"src":"gateway",'
        b'"data":{"slot":"chat-worker","manager":"chat-b"}}'
    )
    assert looks_fine is not None, "it establishes itself, because it is well formed"
    assert looks_fine.type not in crew_store._MANAGER_TYPES, "and it is skipped as another type"
