"""The poisoned-conversation escalation has one owner.

The streak of pre-stream-exhausted cycles and the discard one-shot cross turns
and are two cells on a chat slot. Every transition goes through
``PoisonedConversationEscalation``; the runner calls its seams instead of writing
the cells. The first half pins the state machine, the second pins that nothing
in the package but the escalation (and the slot's constructor) writes a cell.
"""

from __future__ import annotations

import ast
from types import SimpleNamespace

import pytest
from source_corpus import parsed_candidates, src_root

from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard.poisoned_escalation import (
    POISONED_SESSION_CYCLES,
    PoisonedConversationEscalation,
    ProbeFence,
)
from kiro_crew.dashboard.recovery_replays import POLICIES, ReplayFamily
from kiro_crew.dashboard.state import _ChatSlot

_CELLS = frozenset({"_prestream_exhausted_cycles", "_poisoned_reset_used"})


def _slot(cycles: int = 0, spent: bool = False) -> SimpleNamespace:
    return SimpleNamespace(_prestream_exhausted_cycles=cycles, _poisoned_reset_used=spent)


def _exhaust(escalation: PoisonedConversationEscalation, times: int) -> None:
    for _ in range(times):
        escalation.record_terminal_failure(emitted=False, thought=False, transient=True)


# ---------------------------------------------------------------- the state machine


def test_the_cells_are_slot_fields_and_the_runner_reads_the_same_threshold() -> None:
    assert _CELLS <= set(_ChatSlot.__slots__)
    assert cr.POISONED_SESSION_CYCLES == POISONED_SESSION_CYCLES == 2


def test_a_silent_transient_failure_extends_the_streak() -> None:
    slot = _slot()
    escalation = PoisonedConversationEscalation(slot)

    _exhaust(escalation, 3)

    assert escalation.exhausted_cycles == slot._prestream_exhausted_cycles == 3


@pytest.mark.parametrize(
    ("emitted", "thought", "transient"),
    [(True, False, True), (False, True, True), (False, False, False)],
    ids=["output", "thinking", "non-transient"],
)
def test_any_other_terminal_failure_breaks_the_streak(
    emitted: bool, thought: bool, transient: bool
) -> None:
    slot = _slot(cycles=1)
    escalation = PoisonedConversationEscalation(slot)

    escalation.record_terminal_failure(emitted=emitted, thought=thought, transient=transient)

    assert slot._prestream_exhausted_cycles == 0
    assert not escalation.should_probe()


def test_a_probe_is_due_at_the_threshold_only_while_the_one_shot_is_unspent() -> None:
    escalation = PoisonedConversationEscalation(_slot())

    _exhaust(escalation, POISONED_SESSION_CYCLES - 1)
    assert not escalation.should_probe()
    _exhaust(escalation, 1)
    assert escalation.should_probe()

    spent = PoisonedConversationEscalation(_slot(cycles=POISONED_SESSION_CYCLES, spent=True))
    assert not spent.should_probe()


@pytest.mark.parametrize(
    ("emitted", "thought", "breaks"),
    [(True, False, True), (False, True, True), (False, False, False)],
    ids=["output", "thinking", "silent"],
)
def test_activity_breaks_the_streak_and_never_rearms(
    emitted: bool, thought: bool, breaks: bool
) -> None:
    slot = _slot(cycles=2, spent=True)

    PoisonedConversationEscalation(slot).record_activity(emitted=emitted, thought=thought)

    assert slot._prestream_exhausted_cycles == (0 if breaks else 2)
    assert slot._poisoned_reset_used is True


def test_a_landed_turn_resets_the_streak_and_rearms_the_one_shot() -> None:
    slot = _slot(cycles=3, spent=True)
    escalation = PoisonedConversationEscalation(slot)

    escalation.record_landed_turn()

    assert (escalation.exhausted_cycles, escalation.one_shot_spent) == (0, False)


@pytest.mark.parametrize(("clear_streak", "left"), [(False, 2), (True, 0)])
def test_a_discard_spends_the_one_shot_and_clears_the_streak_only_when_asked(
    clear_streak: bool, left: int
) -> None:
    slot = _slot(cycles=2)
    escalation = PoisonedConversationEscalation(slot)

    escalation.arm_discard(clear_streak=clear_streak)

    assert escalation.one_shot_spent is True
    assert slot._prestream_exhausted_cycles == left
    assert not escalation.should_probe()


def test_the_probe_fence_sees_a_stop_on_either_counter() -> None:
    fence = PoisonedConversationEscalation(_slot()).begin_probe(
        stop_generation=4, session_stop_generation=7
    )

    assert fence == ProbeFence(4, 7)
    assert not fence.stopped_since(stop_generation=4, session_stop_generation=7)
    assert fence.stopped_since(stop_generation=5, session_stop_generation=7)
    assert fence.stopped_since(stop_generation=4, session_stop_generation=8)


@pytest.mark.parametrize("family", [ReplayFamily.IMAGE_HISTORY, ReplayFamily.THINKING_BINDING])
def test_a_cancelled_replay_refunds_the_escalations_one_shot(family: ReplayFamily) -> None:
    """The ledger re-arms by slot attribute name; it must name the escalation's cell."""
    slot = _slot(cycles=0, spent=True)
    escalation = PoisonedConversationEscalation(slot)
    policy = POLICIES[family]

    for refunds in (policy.drain_refunds, policy.consume_refunds):
        escalation.arm_discard(clear_streak=True)
        for attr, value in refunds:
            setattr(slot, attr, value)
        assert escalation.one_shot_spent is False


# ------------------------------------------------------------------- the one owner

_OWNER = "dashboard/poisoned_escalation.py"
_CONSTRUCTOR = "dashboard/state.py"


def _cell_writes(tree: ast.AST) -> list[tuple[str, int]]:
    """Every statement in *tree* that stores to a cell, as ``(cell, line)``."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _CELLS:
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                found.append((node.attr, node.lineno))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"setattr", "delattr"}
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _CELLS
        ):
            found.append((node.args[1].value, node.lineno))
    return sorted(found)


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("slot._poisoned_reset_used = True", True),
        ("slot._prestream_exhausted_cycles += 1", True),
        ("slot._prestream_exhausted_cycles: int = 0", True),
        ("a, slot._poisoned_reset_used = 1, True", True),
        ("x = y = slot._prestream_exhausted_cycles = 0", True),
        ("del slot._poisoned_reset_used", True),
        ("setattr(slot, '_poisoned_reset_used', False)", True),
        ("if slot._poisoned_reset_used: pass", False),
        ("n = slot._prestream_exhausted_cycles + 1", False),
        ("setattr(slot, attr, False)", False),
    ],
)
def test_the_write_scan_sees_every_spelling(source: str, flagged: bool) -> None:
    assert bool(_cell_writes(ast.parse(source))) is flagged


def _constructor_span(tree: ast.Module) -> range:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "_ChatSlot":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    return range(item.lineno, (item.end_lineno or item.lineno) + 1)
    raise AssertionError("_ChatSlot.__init__ not found")


def test_only_the_escalation_and_the_slot_constructor_write_the_cells() -> None:
    root = src_root()
    strays: list[str] = []
    scanned = 0
    for path, _text, tree in parsed_candidates(
        require_any=tuple(sorted(_CELLS)), skip_syntax_errors=False
    ):
        scanned += 1
        rel = path.relative_to(root).as_posix()
        writes = _cell_writes(tree)
        if rel == _OWNER:
            assert writes, "the scan found no write in the escalation itself"
            continue
        if rel == _CONSTRUCTOR:
            span = _constructor_span(tree)
            writes = [(cell, line) for cell, line in writes if line not in span]
        strays.extend(f"{rel}:{line} writes {cell}" for cell, line in writes)

    assert scanned >= 3  # the escalation, the slot and the runner at least
    assert strays == []
