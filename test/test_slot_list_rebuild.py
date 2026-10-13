"""Partial slot-list labelling during dashboard restart."""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard import channel_slots
from kiro_crew.dashboard import server as srv
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history_projection import (
    METADATA_LINE_CORRUPT,
    METADATA_LINE_READABLE,
    METADATA_LINE_TRANSIENT,
)
from kiro_crew.testing.wait import await_until


@pytest.fixture
def state(_floor_monkeypatch, tmp_path) -> DashboardState:
    _floor_monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def _slots_broadcasts(broadcast: MagicMock) -> list[dict]:
    return [
        call.args[0] for call in broadcast.call_args_list if call.args[0].get("_type") == "slots"
    ]


class TestPushes:
    def test_a_push_during_rebuild_carries_the_real_partial_list(
        self, state, _floor_monkeypatch
    ) -> None:
        broadcast = MagicMock()
        slots = [{"key": "chat-1-1", "title": "restored"}]
        _floor_monkeypatch.setattr(state, "_broadcast", broadcast)
        _floor_monkeypatch.setattr(
            state, "serialize_slot_views", lambda **_kwargs: (slots, slots, None)
        )
        state.begin_slot_list_rebuild()

        state.push_slots_update()

        frames = _slots_broadcasts(broadcast)
        assert len(frames) == 1
        assert frames[0]["slotsPartial"] is True
        assert json.loads(frames[0]["slots"]) == slots

    def test_finishing_pushes_the_label_flip_once(self, state, _floor_monkeypatch) -> None:
        broadcast = MagicMock()
        _floor_monkeypatch.setattr(state, "_broadcast", broadcast)
        state.begin_slot_list_rebuild()
        state.push_slots_update()

        state.finish_slot_list_rebuild()
        state.finish_slot_list_rebuild()

        frames = _slots_broadcasts(broadcast)
        assert [frame["slotsPartial"] for frame in frames] == [True, False]
        assert state.slot_list_rebuilding is False

    def test_an_unknowable_restore_keeps_the_partial_label(self, state) -> None:
        state.unrestored_slot_notice = {
            "count": 0,
            "keys": [],
            "unknowable": True,
        }
        state.begin_slot_list_rebuild()

        state.finish_slot_list_rebuild()

        assert state.slot_list_rebuilding is True
        assert state._complete_slot_list_rebuild_if_ready() is False

    def test_suspended_construction_keeps_a_revived_dropped_tab_partial(
        self, state, _floor_monkeypatch
    ) -> None:
        broadcast = MagicMock()
        dropped_key = "chat-2-unreadable"
        _floor_monkeypatch.setattr(state, "_broadcast", broadcast)
        _floor_monkeypatch.setattr(state, "_slots_broadcast_lock", None)
        state.unrestored_slot_notice = {
            "count": 1,
            "keys": [dropped_key],
            "unknowable": False,
        }
        state.begin_slot_list_rebuild()
        state.finish_slot_list_rebuild()

        with state.suspend_slots_push():
            slot = state.get_or_create_slot(dropped_key)
            state.begin_slot_construction(slot.key)

        frames = _slots_broadcasts(broadcast)
        assert state.slot_list_rebuilding is True
        assert [frame["slotsPartial"] for frame in frames] == [True]
        assert dropped_key not in {row["key"] for row in json.loads(frames[0]["slots"])}

        state.end_slot_construction(slot.key)
        state.push_slots_update()

        frames = _slots_broadcasts(broadcast)
        assert state.slot_list_rebuilding is False
        assert [frame["slotsPartial"] for frame in frames] == [True, False]
        assert dropped_key in {row["key"] for row in json.loads(frames[-1]["slots"])}

    def test_a_state_that_never_rebuilds_labels_lists_complete(
        self, state, _floor_monkeypatch
    ) -> None:
        broadcast = MagicMock()
        _floor_monkeypatch.setattr(state, "_broadcast", broadcast)

        state.push_slots_update()

        assert _slots_broadcasts(broadcast)[0]["slotsPartial"] is False


class TestSlotsGet:
    @pytest.mark.asyncio
    async def test_get_remains_a_bare_immediate_array(self, state, _floor_monkeypatch) -> None:
        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.dashboard.chat_handlers import api_chat_slots
        from kiro_crew.dashboard.handlers import source_providers

        async def _no_allowlist() -> None:
            return None

        _floor_monkeypatch.setattr(source_providers, "ensure_gitlab_hosts_loaded", _no_allowlist)
        _floor_monkeypatch.setattr(
            source_providers, "is_owner_dashboard_request", lambda _request: False
        )
        restored = [{"key": "chat-1-1", "title": "restored"}]
        _floor_monkeypatch.setattr(state, "serialize_slots", lambda **_kwargs: restored)
        state.begin_slot_list_rebuild()
        request = make_mocked_request("GET", "/api/chat/slots", app={"state": state})

        response = await await_until(asyncio.ensure_future(api_chat_slots(request)))

        assert json.loads(response.text) == restored


class _FakeWebSocket:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict] = []
        self._flags: dict = {"_is_dashboard_user": True}

    def __setitem__(self, key: str, value) -> None:
        self._flags[key] = value

    def __getitem__(self, key: str):
        return self._flags[key]

    def get(self, key: str, default=None):
        return self._flags.get(key, default)

    async def prepare(self, request) -> None:
        return None

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def send_str(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class TestConnectSnapshot:
    async def _connect(
        self, _floor_monkeypatch, *, rebuilding: bool
    ) -> tuple[list[dict], MagicMock]:
        from kiro_crew.dashboard import ws as dashboard_ws
        from kiro_crew.dashboard.handlers import source_providers

        state = MagicMock()
        state.owner_id = "U_OWNER"
        state._yolo = False
        state.source_link_urls.return_value = []
        state.serialize_slots.return_value = [{"key": "chat-1-1", "title": "restored"}]
        state._folders = []
        state.folders_generation.return_value = 0
        state.slot_list_rebuilding = rebuilding

        class Request(dict):
            def __init__(self) -> None:
                super().__init__({"user": "U_OWNER", "app": "", "is_dashboard_user": True})
                self.app = {"state": state}

        async def _no_allowlist() -> frozenset:
            return frozenset()

        fake_ws = _FakeWebSocket()
        _floor_monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        _floor_monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws)
        _floor_monkeypatch.setattr(source_providers, "ensure_gitlab_hosts_loaded", _no_allowlist)

        await await_until(asyncio.ensure_future(dashboard_ws.api_ws(Request())))  # type: ignore[arg-type]
        return [frame for frame in fake_ws.sent if frame.get("type") == "slots"], state

    @pytest.mark.asyncio
    @pytest.mark.parametrize("rebuilding", [True, False])
    async def test_snapshot_carries_the_real_list_and_partial_label(
        self, _floor_monkeypatch, rebuilding
    ) -> None:
        frames, state = await self._connect(_floor_monkeypatch, rebuilding=rebuilding)

        assert [row["key"] for row in frames[0]["data"]] == ["chat-1-1"]
        assert frames[0]["slotsPartial"] is rebuilding
        state.serialize_slots.assert_called_once()


def _restore_cfg(*, surface_channel_sessions: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        dashboard=SimpleNamespace(
            restore_window_minutes=30,
            restore_sessions=True,
            idle_slot_sweep_days=0,
            surface_channel_sessions=surface_channel_sessions,
        )
    )


class TestBootRestore:
    @pytest.fixture(autouse=True)
    def _quiet_restore(self, _floor_monkeypatch) -> None:
        _floor_monkeypatch.setattr(srv.cautious_boot, "pause_before", AsyncMock(return_value=None))
        _floor_monkeypatch.setattr(srv.chat, "restore_open_slots_async", AsyncMock(return_value=0))
        _floor_monkeypatch.setattr(
            srv.chat, "restore_recent_sessions_async", AsyncMock(return_value=0)
        )
        _floor_monkeypatch.setattr(channel_slots, "RECONCILE_INTERVAL_SECS", 3600)

    @staticmethod
    async def _stop_reconciler(state) -> None:
        task = state._channel_slot_reconciler
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    @pytest.mark.asyncio
    async def test_the_boot_path_does_not_wait_for_the_channel_pass(
        self, state, _floor_monkeypatch
    ) -> None:
        never = asyncio.Event()

        async def _reconcile(_state, _window) -> bool:
            await never.wait()
            return False

        _floor_monkeypatch.setattr(channel_slots, "_reconcile_pass", _reconcile)
        state.begin_slot_list_rebuild()
        try:
            await await_until(
                asyncio.ensure_future(srv._restore_dashboard_sessions(state, _restore_cfg()))
            )

            assert state.slot_list_rebuilding is True
            assert not state._channel_slot_reconciler.done()
        finally:
            await self._stop_reconciler(state)

    @pytest.mark.asyncio
    async def test_first_complete_pass_flips_the_label_and_pushes(
        self, state, _floor_monkeypatch
    ) -> None:
        completed = asyncio.Event()
        broadcast = MagicMock()
        _floor_monkeypatch.setattr(state, "_broadcast", broadcast)

        async def _reconcile(_state, _window) -> bool:
            completed.set()
            return True

        _floor_monkeypatch.setattr(channel_slots, "_reconcile_pass", _reconcile)
        state.begin_slot_list_rebuild()
        try:
            await srv._restore_dashboard_sessions(state, _restore_cfg())
            await await_until(completed)
            for _ in range(5):
                await asyncio.sleep(0)

            assert state.slot_list_rebuilding is False
            assert _slots_broadcasts(broadcast)[-1]["slotsPartial"] is False
        finally:
            await self._stop_reconciler(state)

    @pytest.mark.asyncio
    async def test_complete_channel_pass_keeps_partial_while_an_open_tab_is_unrestored(
        self, state, _floor_monkeypatch
    ) -> None:
        completed = asyncio.Event()

        async def _reconcile(_state, _window) -> bool:
            completed.set()
            return True

        _floor_monkeypatch.setattr(channel_slots, "_reconcile_pass", _reconcile)
        state.unrestored_slot_keys = {"chat-2-unreadable"}
        state.begin_slot_list_rebuild()
        try:
            await srv._restore_dashboard_sessions(state, _restore_cfg())
            await await_until(completed)
            for _ in range(5):
                await asyncio.sleep(0)

            assert state.slot_list_rebuilding is True
        finally:
            await self._stop_reconciler(state)

    @pytest.mark.asyncio
    async def test_complete_channel_pass_clears_partial_on_a_remote_authority_boot(
        self, tmp_path, _floor_monkeypatch
    ) -> None:
        """The routine container boot: a remote-only reopen seed must not hold the label.

        Under ``KIROCREW_CONTAINER_AUTHORITY_RESTORED=1`` a listed tab whose transcript
        is not on local disk stays in ``unrestored_slot_keys`` on EVERY boot, by design
        (its transcript is fetched lazily, if ever). A finish that waited on that set
        kept ``slotsPartial`` for the process lifetime, so the dashboard never drained
        a deleted session's unread dot. The seed and the notice both come from the
        real restore here, so this proves the condition against what a boot produces.
        """
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard import chat_persistence
        from kiro_crew.dashboard.chat_utils import _history_key_for

        _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        _floor_monkeypatch.setenv(chat_persistence.ENV_AUTHORITY_RESTORED, "1")
        seeded = _make_state(tmp_path / "sessions")
        seeded.conversation_log.append(_history_key_for("chat-1-local"), "user", "hello")
        (tmp_path / "open_slots.json").write_text(
            json.dumps({"keys": ["chat-1-local", "chat-9-remote"], "ts": 0.0})
        )
        booted = _make_state(tmp_path / "sessions")
        booted.begin_slot_list_rebuild()
        completed = asyncio.Event()

        async def _reconcile(_state, _window) -> bool:
            completed.set()
            return True

        _floor_monkeypatch.setattr(channel_slots, "_reconcile_pass", _reconcile)

        assert chat_persistence.restore_open_slots(booted) == 1
        assert "chat-9-remote" in booted.unrestored_slot_keys, "the seed the finish must ignore"
        try:
            await srv._restore_dashboard_sessions(booted, _restore_cfg())
            await await_until(completed)
            for _ in range(5):
                await asyncio.sleep(0)

            assert booted.slot_list_rebuilding is False
            assert "chat-9-remote" in booted.unrestored_slot_keys, "the reopen seed is kept"
        finally:
            await self._stop_reconciler(booted)

    @pytest.mark.asyncio
    async def test_complete_channel_pass_keeps_partial_for_a_dropped_tab_only_until_it_is_listed(
        self, state, _floor_monkeypatch
    ) -> None:
        """A dropped tab holds the label; the same tab rebuilt as a slot releases it."""
        completed = asyncio.Event()

        async def _reconcile(_state, _window) -> bool:
            completed.set()
            return True

        _floor_monkeypatch.setattr(channel_slots, "_reconcile_pass", _reconcile)
        state.unrestored_slot_keys = {"chat-2-unreadable", "chat-9-remote"}
        state.unrestored_slot_notice = {
            "count": 1,
            "keys": ["chat-2-unreadable"],
            "unknowable": False,
        }
        state.begin_slot_list_rebuild()
        try:
            await srv._restore_dashboard_sessions(state, _restore_cfg())
            await await_until(completed)
            for _ in range(5):
                await asyncio.sleep(0)
            assert state.slot_list_rebuilding is True, "the dropped tab's dot would drain"

            state.get_or_create_slot("chat-2-unreadable")
            state.finish_slot_list_rebuild()

            assert state.slot_list_rebuilding is False, "listed again, so nothing drains"
        finally:
            await self._stop_reconciler(state)

    @pytest.mark.asyncio
    async def test_incomplete_pass_keeps_the_label_and_lists_flow(
        self, state, _floor_monkeypatch
    ) -> None:
        incomplete = asyncio.Event()
        broadcast = MagicMock()
        _floor_monkeypatch.setattr(state, "_broadcast", broadcast)

        async def _reconcile(_state, _window) -> bool:
            incomplete.set()
            return False

        _floor_monkeypatch.setattr(channel_slots, "_reconcile_pass", _reconcile)
        state.begin_slot_list_rebuild()
        try:
            await srv._restore_dashboard_sessions(state, _restore_cfg())
            await await_until(incomplete)
            state.push_slots_update()

            assert state.slot_list_rebuilding is True
            assert _slots_broadcasts(broadcast)[-1]["slotsPartial"] is True
        finally:
            await self._stop_reconciler(state)

        assert state.slot_list_rebuilding is True

    @pytest.mark.asyncio
    async def test_restore_failure_before_reconciler_clears_the_label(
        self, state, _floor_monkeypatch
    ) -> None:
        _floor_monkeypatch.setattr(
            srv.chat,
            "restore_open_slots_async",
            AsyncMock(side_effect=RuntimeError("restore died")),
        )
        state.begin_slot_list_rebuild()

        with pytest.raises(RuntimeError, match="restore died"):
            await srv._restore_dashboard_sessions(state, _restore_cfg())

        assert state.slot_list_rebuilding is False
        assert state._channel_slot_reconciler is None

    @pytest.mark.asyncio
    async def test_without_channel_surfacing_restore_clears_the_label(self, state) -> None:
        state.begin_slot_list_rebuild()

        await srv._restore_dashboard_sessions(state, _restore_cfg(surface_channel_sessions=False))

        assert state.slot_list_rebuilding is False
        assert state._channel_slot_reconciler is None

    @pytest.mark.asyncio
    async def test_without_channel_surfacing_keeps_partial_for_an_unrestored_open_tab(
        self, state
    ) -> None:
        state.unrestored_slot_keys = {"chat-2-unreadable"}
        state.begin_slot_list_rebuild()

        await srv._restore_dashboard_sessions(state, _restore_cfg(surface_channel_sessions=False))

        assert state.slot_list_rebuilding is True
        assert state._channel_slot_reconciler is None

    @pytest.mark.asyncio
    async def test_without_channel_surfacing_retries_finish_when_dropped_tab_is_listed(
        self, state
    ) -> None:
        state.unrestored_slot_keys = {"chat-2-unreadable"}
        state.begin_slot_list_rebuild()

        await srv._restore_dashboard_sessions(state, _restore_cfg(surface_channel_sessions=False))
        assert state.slot_list_rebuilding is True

        state.get_or_create_slot("chat-2-unreadable")

        assert state.slot_list_rebuilding is False
        assert state._channel_slot_reconciler is None


class TestMetadataReadState:
    @staticmethod
    def _channel_log(state, line_state: str) -> SimpleNamespace:
        log = SimpleNamespace()
        state.conversation_log = log
        log.list_sessions_status = lambda: (
            [{"key": "slack_1.1", "title": "", "modified": 10**12}],
            True,
        )
        log.get_metadata_status = lambda _key: ({}, False)
        log.metadata_line_state = lambda _key: line_state
        log.mtime_of = lambda _key: None
        log.read_messages = MagicMock(return_value=[{"role": "user", "content": "hello"}])
        state.sessions.channel_key_for_stem = lambda stem: (
            "slack:1.1" if stem == "slack_1.1" else ""
        )
        return log

    @pytest.mark.asyncio
    async def test_corrupt_metadata_counts_complete_and_can_surface(
        self, state, _floor_monkeypatch
    ) -> None:
        log = self._channel_log(state, METADATA_LINE_CORRUPT)

        async def _no_folder(_state, _namespace) -> str:
            return ""

        _floor_monkeypatch.setattr(channel_slots, "lookup_channel_folder", _no_folder)

        _surfaced, complete = await channel_slots._reconcile_with_status(state, 30)

        assert complete is True
        assert "slack_1.1" in state._slots
        log.read_messages.assert_called_once_with("slack_1.1")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("line_state", [METADATA_LINE_TRANSIENT, METADATA_LINE_READABLE])
    async def test_indeterminate_metadata_keeps_partial_and_is_not_surfaced(
        self, state, line_state
    ) -> None:
        log = self._channel_log(state, line_state)

        _surfaced, complete = await channel_slots._reconcile_with_status(state, 30)

        assert complete is False
        assert "slack_1.1" not in state._slots
        log.read_messages.assert_not_called()
