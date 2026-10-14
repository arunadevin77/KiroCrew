"""Mid-turn steers in the interrupted-turn restore a Resume prepends.

A steer is a ``user`` row marked ``meta.steer`` that joined a turn already
running. The restore must name the row that OPENED the turn as the interrupted
request and carry each steer, in order, with the partial output.
"""

from __future__ import annotations

from kiro_crew.context import build_interrupted_turn_preamble
from kiro_crew.dashboard.state import TURN_OPENING_INJECT_KINDS


def _steer(text: str) -> dict:
    return {"role": "user", "content": text, "meta": {"steer": True, "steerState": "consumed"}}


def _resume(rows: list[dict]) -> str:
    current = {"role": "inject", "content": "resume", "meta": {"injectKind": "recovery"}}
    rows.append(current)
    return build_interrupted_turn_preamble(
        rows, current=current, opener_inject_kinds=TURN_OPENING_INJECT_KINDS
    )


def _earlier_turn() -> list[dict]:
    return [
        {"role": "user", "content": "earlier answered request"},
        {"role": "assistant", "content": "earlier answer"},
    ]


def test_a_steer_is_not_taken_as_the_interrupted_request():
    rows = _earlier_turn() + [
        {"role": "user", "content": "deploy the canary to us-west-2"},
        {"role": "assistant", "content": "Starting the canary deploy"},
        _steer("use the blue fleet instead"),
        {"role": "tool", "content": "", "meta": {"tool": "shell"}},
        {"role": "assistant", "content": "Switching to the blue fleet"},
        {"role": "error", "content": "connection lost"},
    ]

    out = _resume(rows)

    assert "Interrupted request:\ndeploy the canary to us-west-2" in out
    assert "the user's most recent request" in out
    assert "earlier answer" not in out
    partial = out.split("Partial response before the interruption:\n", 1)[1]
    assert partial.index("Starting the canary deploy") < partial.index(
        "[Steered mid-turn] use the blue fleet instead"
    )
    assert partial.index("[Steered mid-turn] use the blue fleet instead") < partial.index(
        "Switching to the blue fleet"
    )


def test_several_steers_are_carried_in_the_order_they_were_sent():
    rows = [
        {"role": "user", "content": "write the release notes"},
        {"role": "assistant", "content": "Drafting"},
        _steer("first steer"),
        _steer("second steer"),
        {"role": "assistant", "content": "Adjusted"},
        _steer("third steer"),
    ]

    out = _resume(rows)

    assert "Interrupted request:\nwrite the release notes" in out
    partial = out.split("Partial response before the interruption:\n", 1)[1]
    lines = partial.split("\n[END INTERRUPTED TURN]", 1)[0].split("\n")
    assert lines == [
        "Drafting",
        "[Steered mid-turn] first steer",
        "[Steered mid-turn] second steer",
        "Adjusted",
        "[Steered mid-turn] third steer",
    ]


def test_a_turn_with_no_steers_restores_the_same_frame():
    rows = _earlier_turn() + [
        {"role": "user", "content": "rotate the keys"},
        {"role": "assistant", "content": "Rotating"},
        {"role": "error", "content": "connection lost"},
    ]

    out = _resume(rows)

    assert out == "\n".join(
        [
            "[INTERRUPTED TURN — context restore]",
            "The turn below was cut off when the agent process serving this "
            "conversation stopped, so the restored conversation may not include it. "
            "It is the user's most recent request, the one you are being asked to "
            "carry on with. Tool calls it made may already have taken effect.",
            "",
            "Interrupted request:\nrotate the keys",
            "",
            "Partial response before the interruption:\nRotating",
            "[END INTERRUPTED TURN]",
        ]
    )
    assert "[Steered mid-turn]" not in out


def test_a_steered_automated_opener_stays_the_boundary():
    # A steer into a turn a nudge, a sub-agent completion or a cron delivery
    # opened belongs to that turn: the walk stops at that opener and never
    # reaches the user request answered before it.
    openers = [
        ({"role": "nudge", "content": "cycle 3"}, "a monitor loop cycle"),
        ({"role": "subagent", "content": "review done"}, "a sub-agent completion"),
        (
            {"role": "inject", "content": "rotate the logs", "meta": {"injectKind": "cron"}},
            "a scheduled job",
        ),
    ]
    for opener, kind in openers:
        rows = _earlier_turn() + [
            opener,
            {"role": "assistant", "content": "Working on it"},
            _steer("skip the archive step"),
        ]

        out = _resume(rows)

        assert f"Interrupted automated delivery:\n{opener['content']}" in out, kind
        assert kind in out and "the user's most recent request" not in out, kind
        assert "[Steered mid-turn] skip the archive step" in out, kind
        assert "earlier answered request" not in out, kind


def test_a_steer_after_an_earlier_resume_press_still_finds_the_request():
    rows = _earlier_turn() + [
        {"role": "user", "content": "migrate the table"},
        {"role": "assistant", "content": "Copying rows"},
        {"role": "inject", "content": "resume (first press)", "meta": {"injectKind": "recovery"}},
        _steer("batch it in thousands"),
        {"role": "assistant", "content": "Batching"},
    ]

    out = _resume(rows)

    assert "Interrupted request:\nmigrate the table" in out
    assert "[Steered mid-turn] batch it in thousands" in out
    assert "resume (first press)" not in out


def test_a_window_holding_only_steers_restores_nothing():
    # The opener fell outside the window: a steer is never promoted to the
    # interrupted request in its place.
    rows = [
        _steer("use the blue fleet instead"),
        {"role": "assistant", "content": "Switching"},
    ]

    assert _resume(rows) == ""


def test_only_a_true_steer_flag_marks_a_steer():
    for meta in ({"steer": False}, {"steer": "true"}, {}, None):
        row = {"role": "user", "content": "plain request"}
        if meta is not None:
            row["meta"] = meta
        out = _resume(_earlier_turn() + [row])
        assert "Interrupted request:\nplain request" in out, meta
