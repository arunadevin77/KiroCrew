"""The ``_board_closure_units`` walk, over REAL logs across several slots.

The companion file (``test_crew_log_worktree_fold.py``) drives the fold's placement
rules over hand-built entries, which is the right level for a rule. This file builds
real crew logs -- one per board, each with its own slot header -- so the closure walk
that reaches the logs of MANY SLOTS can be exercised against the arrangement a real
fleet produces.

The tree READ path that renders this walk's units into a bound value lives with its
only consumer, the dashboard page, in that PR's copy of this file; what stays here is
the walk itself, which the ``workstreams`` fold already calls.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, eager, emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log.entry_types import WORK_ENTRY_TYPE

FOLD = crew_log.WORKTREE_FOLD_NAME
GATEWAY = "gateway"

ROOT = "chat-1-root"
MID = "chat-2-mid"
LEAF = "chat-3-leaf"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    """Every test writes into its own data home, never the live one.

    Through the isolation floor's OWN ``MonkeyPatch`` rather than the shared
    ``monkeypatch`` a test also receives, because the two are undone independently.
    ``monkeypatch.undo()`` reverts every record on the instance it is called on, so a
    test that calls it to drop one of its own patches would drop this pin with them and
    whatever ran next would resolve the operator's real data home.
    """
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    eager.stop_for_tests()
    crew_log.forget_slot_folds()
    emit.reset_caches()
    yield
    eager.stop_for_tests()
    crew_log.forget_slot_folds()
    emit.reset_caches()


def _log(unit_id: str, slot: str) -> CrewLog:
    """A session log whose HEADER names *slot*, which is what a slot read joins on."""
    return CrewLog.create(lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew", slot=slot)


def _work(
    handle: CrewLog,
    slot: str,
    action: str,
    *,
    actor: str = "conductor",
    by: str = "",
    **data: Any,
):
    """One real ``work/recorded`` entry, through the store's own validation.

    ``by`` defaults to the board's slot for a conductor action, which is what the
    writer records: a conductor acts as the board. A worker's report passes its own.
    """
    return handle.append(
        WORK_ENTRY_TYPE,
        {"slot": slot, "actor": actor, "by": by or slot, "action": action, **data},
        src=GATEWAY,
    )


def _three_board_fleet() -> None:
    """ROOT conducts MID, MID conducts LEAF -- one log per board, as a real fleet is.

    The shape that matters: every edge is recorded in TWO logs. The parent's log holds
    the ``create`` that says which board owns the item, and the child's own log holds
    the ``goal`` that cites it. Neither log alone places the child.
    """
    root = _log("s-root", ROOT)
    _work(root, ROOT, "goal", goal="ship the fleet", round=1, generation="g-root")
    _work(root, ROOT, "create", item_id="it-mid", title="run the middle", generation="g-root")
    # The bind is what makes MID's log reachable from ROOT: the closure walk follows
    # ``worker_session_key``, and nothing else in ROOT's log names MID.
    _work(root, ROOT, "bind", item_id="it-mid", worker_session_key=MID, generation="g-root")

    mid = _log("s-mid", MID)
    _work(
        mid,
        MID,
        "goal",
        goal="run the middle",
        parent_item="it-mid",
        depth=1,
        generation="g-mid",
    )
    _work(mid, MID, "create", item_id="it-leaf", title="run the leaf", generation="g-mid")
    _work(mid, MID, "bind", item_id="it-leaf", worker_session_key=LEAF, generation="g-mid")
    # MID also reports back against ROOT's item, in its own log. That entry names ROOT's
    # board, and reading it as a claim on ``it-mid`` would invert the edge.
    _work(mid, ROOT, "report", actor="worker", by=MID, item_id="it-mid", status="progress")

    leaf = _log("s-leaf", LEAF)
    _work(
        leaf,
        LEAF,
        "goal",
        goal="run the leaf",
        parent_item="it-leaf",
        depth=2,
        generation="g-leaf",
    )


def test_the_extracted_walk_and_the_workstreams_selector_agree() -> None:
    """The refactor is behaviour-preserving, pinned rather than asserted.

    ``_workstreams_units`` is now a thin caller of ``_board_closure_units``, so the units
    it selects must be the walk's own and in the same order -- otherwise the workstreams
    fold's answer changed under a change that was supposed to be about a different fold.
    """
    _three_board_fleet()
    walked, dropped = crew_log._board_closure_units(ROOT)
    crew_log._WORKSTREAMS_UNITS_DROPPED.clear()
    selected = crew_log._workstreams_units(ROOT)
    assert selected == walked
    assert crew_log._workstreams_units_dropped(ROOT) == dropped
