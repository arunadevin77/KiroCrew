"""The primitive the ``shake`` seed writer undoes a failed save with.

The writer appends the seed row, then saves it with a guarded
``save_slot_off_loop``. When that save does not happen, ``_ChatSlot.withdraw``
takes the just-appended row back out of the window as if it had never been
appended, so no later rotation carries a digest of rows that were never dropped.
"""

from __future__ import annotations

from chat_test_helpers import _make_state


def _slot_with_history(tmp_path, monkeypatch, name: str = "strict"):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(name)
    slot.append("user", "hello", "msg msg-u")
    slot.append("assistant", "world", "msg msg-a")
    return state, slot


class TestWithdraw:
    def test_removes_the_row_by_identity_and_walks_back_the_counters(self, tmp_path, monkeypatch):
        state, slot = _slot_with_history(tmp_path, monkeypatch)
        before = (len(slot.messages), slot.total_messages)
        row = slot.append("compaction", "digest", "", meta={"kind": "x"}, broadcast=False)
        twin = dict(row)  # equal, not identical: must not be the one removed
        slot.messages.append(twin)
        assert slot.withdraw(row) is True
        assert row not in [m for m in slot.messages if m is row]
        assert any(m is twin for m in slot.messages), "an equal row is left alone"
        assert (len(slot.messages) - 1, slot.total_messages) == before
        assert all(m is not row for m in slot._pending)
        assert slot._dirty is True

    def test_an_unknown_row_is_reported_not_removed(self, tmp_path, monkeypatch):
        state, slot = _slot_with_history(tmp_path, monkeypatch)
        n = len(slot.messages)
        assert slot.withdraw({"role": "user", "content": "hello"}) is False
        assert len(slot.messages) == n
