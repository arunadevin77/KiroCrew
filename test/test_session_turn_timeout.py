"""A session's own turn time limit.

Pinned where each defect would live:

- **Resolution** (``turn_dispatch.resolve_turn_limit``): a session's own limit
  beats ``agent.chat_turn_timeout_secs`` for that session's turns only, a
  cleared limit falls back to the global, and a value that is not an integer
  (the metadata line is agent-writable) is ignored.
- **Dispatch**: ``spawn_guarded_turn`` and ``bounded_chat_turn`` bound the turn
  at the session's limit and the timeout card names which limit applied.
- **Transport**: the limit a turn runs under reaches the ACP prompt wait, so a
  session limit above the transport's default is one the system honours.
- **HTTP endpoint** (``/api/chat/slots/{slot}/turn-timeout``): the global
  setting's range and unit, null clears, and the value persists.
- **Restore**: the metadata codec and the channel surfacing path read it back.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.acp import client as acp_client
from kiro_crew.config.loader import CHAT_TURN_TIMEOUT_MAX, CHAT_TURN_TIMEOUT_MIN
from kiro_crew.dashboard import turn_dispatch as td
from kiro_crew.dashboard.chat import api_chat_slot_turn_timeout
from kiro_crew.dashboard.chat_persistence import _validate_turn_timeout_secs
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.turn_limit import TURN_LIMIT_SECS, current_turn_limit_secs


def _slot(secs: object = None) -> MagicMock:
    slot = MagicMock()
    slot.key = "chat-1-123"
    slot.turn_timeout_secs = secs
    return slot


def _state() -> MagicMock:
    state = MagicMock()
    state._background_tasks = set()
    return state


def _global_limit(monkeypatch, secs: float) -> None:
    monkeypatch.setattr(td, "chat_turn_timeout_secs", lambda: secs)


def _transport_config(monkeypatch, *, turn: int = 14400, subagent: int = 600) -> None:
    """Give ``resolve_prompt_timeout`` a known config without touching disk."""
    from kiro_crew.config import loader

    cfg = MagicMock()
    cfg.agent.chat_turn_timeout_secs = turn
    cfg.agent.subagent_timeout_secs = subagent
    monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda cls: cfg))


class TestResolution:
    def test_a_session_limit_beats_the_global(self, monkeypatch) -> None:
        _global_limit(monkeypatch, 14400.0)
        assert td.resolve_turn_limit(_slot(28800)) == (28800.0, td.TURN_LIMIT_SESSION)

    def test_a_lower_session_limit_beats_the_global_too(self, monkeypatch) -> None:
        _global_limit(monkeypatch, 14400.0)
        assert td.resolve_turn_limit(_slot(600)) == (600.0, td.TURN_LIMIT_SESSION)

    def test_a_cleared_limit_falls_back_to_the_global(self, monkeypatch) -> None:
        _global_limit(monkeypatch, 7200.0)
        assert td.resolve_turn_limit(_slot(None)) == (7200.0, td.TURN_LIMIT_GLOBAL)

    def test_no_slot_follows_the_global(self, monkeypatch) -> None:
        _global_limit(monkeypatch, 7200.0)
        assert td.resolve_turn_limit() == (7200.0, td.TURN_LIMIT_GLOBAL)

    @pytest.mark.parametrize("raw", [True, 3600.0, "3600", [3600], MagicMock()])
    def test_a_value_that_is_not_an_integer_is_ignored(self, monkeypatch, raw: object) -> None:
        _global_limit(monkeypatch, 7200.0)
        assert td.session_turn_timeout_secs(_slot(raw)) is None
        assert td.resolve_turn_limit(_slot(raw)) == (7200.0, td.TURN_LIMIT_GLOBAL)

    def test_an_out_of_range_integer_is_held_to_the_global_range(self) -> None:
        assert td.session_turn_timeout_secs(_slot(1)) == CHAT_TURN_TIMEOUT_MIN
        assert td.session_turn_timeout_secs(_slot(10**9)) == CHAT_TURN_TIMEOUT_MAX

    def test_a_session_limit_is_not_clamped_to_the_transport_default(self, monkeypatch) -> None:
        """The transport follows a session limit, so the clamp the global value
        gets against the transport's own wait must not cut it."""
        monkeypatch.setattr(td, "_acp_prompt_ceiling", lambda: 14400.0)
        assert td.resolve_turn_limit(_slot(CHAT_TURN_TIMEOUT_MAX))[0] == CHAT_TURN_TIMEOUT_MAX


class TestDispatch:
    @pytest.mark.asyncio
    async def test_spawned_turn_runs_under_the_session_limit(self, monkeypatch) -> None:
        _global_limit(monkeypatch, 14400.0)
        monkeypatch.setattr(td, "native_cancel_ack_timeout_secs", lambda: 0.1)
        seen: list[float | None] = []

        async def _turn() -> None:
            seen.append(current_turn_limit_secs())

        await td.spawn_guarded_turn(_state(), _slot(28800), _turn())
        assert seen == [28800.0]

    @pytest.mark.asyncio
    async def test_bounded_turn_runs_under_the_session_limit(self, monkeypatch) -> None:
        _global_limit(monkeypatch, 14400.0)
        seen: list[float | None] = []

        async def _turn() -> None:
            seen.append(current_turn_limit_secs())

        await td.bounded_chat_turn(_turn(), state=_state(), slot=_slot(900))
        assert seen == [900.0]

    @pytest.mark.asyncio
    async def test_the_published_limit_does_not_outlive_the_turn(self, monkeypatch) -> None:
        _global_limit(monkeypatch, 14400.0)

        async def _turn() -> None:
            return None

        await td.bounded_chat_turn(_turn(), state=_state(), slot=_slot(900))
        assert TURN_LIMIT_SECS.get() is None

    @pytest.mark.asyncio
    async def test_the_card_names_the_session_limit(self, monkeypatch) -> None:
        state, slot = _state(), _slot(28800)
        monkeypatch.setattr(td, "native_cancel_ack_timeout_secs", lambda: 0.1)
        monkeypatch.setattr(td, "session_turn_timeout_secs", lambda s: 0.01)

        async def _forever() -> None:
            await asyncio.Event().wait()

        task = td.spawn_guarded_turn(state, slot, _forever())
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await task
        await asyncio.sleep(0)
        errors = [c for c in slot.append.call_args_list if c.args[0] == "error"]
        assert len(errors) == 1
        assert errors[0].kwargs["meta"] == {
            "kind": td.TURN_TIMEOUT_KIND,
            "limit_scope": td.TURN_LIMIT_SESSION,
        }
        assert td.SESSION_TURN_LIMIT_MENU in errors[0].args[1]

    def test_session_card_points_at_the_session_menu_not_settings(self) -> None:
        card = td.format_turn_timeout_card(28800.0, limit_scope=td.TURN_LIMIT_SESSION)
        assert "8-hour" in card
        assert td.SESSION_TURN_LIMIT_MENU in card
        assert "Settings → Chat → Advanced" not in card

    def test_global_card_still_points_at_settings(self) -> None:
        card = td.format_turn_timeout_card(14400.0)
        assert "Settings → Chat → Advanced" in card
        assert td.SESSION_TURN_LIMIT_MENU not in card

    @pytest.mark.asyncio
    async def test_approval_window_sizes_against_the_running_turn_limit(self, monkeypatch) -> None:
        """A session limit below the global leaves the approval window inside it."""
        _global_limit(monkeypatch, 14400.0)
        holder = MagicMock()
        holder.load.return_value.agent.tool_approval_timeout_secs = 7200
        monkeypatch.setattr(td, "KiroCrewConfig", holder)
        token = TURN_LIMIT_SECS.set(900.0)
        try:
            window = td.tool_approval_timeout_secs()
        finally:
            TURN_LIMIT_SECS.reset(token)
        assert window <= 900.0 - td.APPROVAL_TURN_MARGIN_SECS


class TestTransportFollowsTheTurnLimit:
    def test_outside_a_turn_the_transport_keeps_its_default(self, monkeypatch) -> None:
        _transport_config(monkeypatch)
        assert acp_client.resolve_prompt_timeout() == acp_client._DEFAULT_PROMPT_TIMEOUT

    def test_a_turn_limit_above_the_default_lengthens_the_wait(self, monkeypatch) -> None:
        _transport_config(monkeypatch)
        token = TURN_LIMIT_SECS.set(28800.0)
        try:
            wait = acp_client.resolve_prompt_timeout()
        finally:
            TURN_LIMIT_SECS.reset(token)
        assert wait == 28800.0 + acp_client._PROMPT_TIMEOUT_MARGIN_SECS

    def test_a_turn_limit_below_the_default_never_shortens_it(self, monkeypatch) -> None:
        _transport_config(monkeypatch)
        token = TURN_LIMIT_SECS.set(600.0)
        try:
            wait = acp_client.resolve_prompt_timeout()
        finally:
            TURN_LIMIT_SECS.reset(token)
        assert wait == acp_client._DEFAULT_PROMPT_TIMEOUT

    def test_unreadable_config_still_honours_the_turn_limit(self, monkeypatch) -> None:
        from kiro_crew.config import loader

        def _boom(cls):  # type: ignore[no-untyped-def]
            raise RuntimeError("no config here")

        monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(_boom))
        token = TURN_LIMIT_SECS.set(28800.0)
        try:
            wait = acp_client.resolve_prompt_timeout()
        finally:
            TURN_LIMIT_SECS.reset(token)
        assert wait == 28800.0 + acp_client._PROMPT_TIMEOUT_MARGIN_SECS

    @pytest.mark.asyncio
    async def test_the_prompt_wait_inside_a_session_turn_follows_its_limit(
        self, monkeypatch
    ) -> None:
        """End to end through the off-loop resolution every prompt uses."""
        _transport_config(monkeypatch)
        _global_limit(monkeypatch, 14400.0)
        waits: list[float] = []

        async def _turn() -> None:
            waits.append(await acp_client._effective_prompt_timeout_async(None))

        await td.bounded_chat_turn(_turn(), state=_state(), slot=_slot(43200))
        assert waits == [43200.0 + acp_client._PROMPT_TIMEOUT_MARGIN_SECS]


class TestWatchdogFollowsTheTurnLimit:
    """Watchdog windows are bounded against the limit the turn runs under."""

    def _settings(self, monkeypatch, *, turn: int, hard_cap: float):
        from kiro_crew.acp.session_handle import _load_watchdog_settings
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.sections import AgentConfig
        from kiro_crew.config.service_sections import WatchdogConfig

        cfg = KiroCrewConfig(
            agent=AgentConfig(chat_turn_timeout_secs=turn),
            watchdog=WatchdogConfig(tool_stall_hard_cap_secs=hard_cap),
        )
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
        return _load_watchdog_settings()

    def test_a_longer_turn_limit_lifts_a_window_the_global_ceiling_cut(self, monkeypatch) -> None:
        wd = self._settings(monkeypatch, turn=14400, hard_cap=40000.0)
        cut = acp_client._DEFAULT_PROMPT_TIMEOUT * 0.9
        assert wd.tool_stall_hard_cap_secs == cut
        lifted = wd.for_turn_limit(86400.0)
        assert lifted.tool_stall_hard_cap_secs == 40000.0
        # Every other window keeps its configured value.
        assert lifted.stale_window_secs == wd.stale_window_secs

    def test_the_lifted_window_stays_inside_the_turn_limit(self, monkeypatch) -> None:
        wd = self._settings(monkeypatch, turn=14400, hard_cap=80000.0)
        lifted = wd.for_turn_limit(28800.0)
        budget = (28800.0 + acp_client._PROMPT_TIMEOUT_MARGIN_SECS) * 0.9
        assert lifted.tool_stall_hard_cap_secs == budget

    def test_a_shorter_or_absent_turn_limit_changes_nothing(self, monkeypatch) -> None:
        wd = self._settings(monkeypatch, turn=14400, hard_cap=40000.0)
        assert wd.for_turn_limit(None) is wd
        assert wd.for_turn_limit(600.0) is wd
        assert wd.for_turn_limit(14400.0) is wd

    def test_a_snapshot_with_no_configured_windows_is_left_alone(self) -> None:
        from kiro_crew.acp.session_handle import WatchdogSettings

        wd = WatchdogSettings()
        assert wd.for_turn_limit(86400.0) is wd


def _make_app(state: DashboardState, request_app: str | None = None) -> web.Application:
    app = web.Application()
    app["state"] = state
    if request_app is not None:

        @web.middleware
        async def _tag_app(request: web.Request, handler):  # type: ignore[no-untyped-def]
            request["app"] = request_app
            return await handler(request)

        app.middlewares.append(_tag_app)
    app.router.add_get("/api/chat/slots/{slot}/turn-timeout", api_chat_slot_turn_timeout)
    app.router.add_post("/api/chat/slots/{slot}/turn-timeout", api_chat_slot_turn_timeout)
    return app


def _mock_state(*slots: _ChatSlot) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot for slot in slots}
    state.sessions = MagicMock()
    state.conversation_log = MagicMock()
    return state


_SAVE = "kiro_crew.dashboard.chat_handlers.save_slot_off_loop"
_GLOBAL = "kiro_crew.dashboard.chat_handlers.chat_turn_timeout_secs"


class TestEndpoint:
    @pytest.mark.asyncio
    async def test_get_reports_the_session_limit_the_global_and_the_range(self) -> None:
        slot = _ChatSlot("test")
        slot.turn_timeout_secs = 28800
        with patch(_GLOBAL, return_value=14400.0):
            async with TestClient(TestServer(_make_app(_mock_state(slot)))) as client:
                resp = await client.get("/api/chat/slots/test/turn-timeout")
                assert resp.status == 200
                assert await resp.json() == {
                    "secs": 28800,
                    "global_secs": 14400,
                    "min": CHAT_TURN_TIMEOUT_MIN,
                    "max": CHAT_TURN_TIMEOUT_MAX,
                }

    @pytest.mark.asyncio
    async def test_post_sets_the_session_limit_and_persists_it(self) -> None:
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        with (
            patch(_SAVE, new=AsyncMock(return_value=True)) as saver,
            patch(_GLOBAL, return_value=14400.0),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/test/turn-timeout", json={"secs": 28800})
                assert resp.status == 200
                data = await resp.json()
        assert data["ok"] is True and data["secs"] == 28800 and data["global_secs"] == 14400
        assert slot.turn_timeout_secs == 28800
        # The commit, then the confirm save that re-orders the durable record
        # after any alias flush; both confirmed and pinned to the transcript the
        # authorization covered.
        assert saver.await_count == 2
        for call in saver.await_args_list:
            assert call.args == (state, slot)
            assert call.kwargs == {
                "force": True,
                "best_effort": False,
                "expected_history_key": slot_history_key(slot),
            }

    @pytest.mark.asyncio
    async def test_post_null_returns_the_session_to_the_global(self) -> None:
        slot = _ChatSlot("test")
        slot.turn_timeout_secs = 28800
        with patch(_SAVE, new=AsyncMock(return_value=True)), patch(_GLOBAL, return_value=14400.0):
            async with TestClient(TestServer(_make_app(_mock_state(slot)))) as client:
                resp = await client.post("/api/chat/slots/test/turn-timeout", json={"secs": None})
                assert resp.status == 200
                assert (await resp.json())["secs"] is None
        assert slot.turn_timeout_secs is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("secs", "code"),
        [
            (CHAT_TURN_TIMEOUT_MIN - 1, "secs_out_of_range"),
            (CHAT_TURN_TIMEOUT_MAX + 1, "secs_out_of_range"),
            (3600.5, "secs_not_an_integer"),
            (3600.0, "secs_not_an_integer"),
            ("3600", "secs_not_an_integer"),
            (True, "secs_not_an_integer"),
            ([3600], "secs_not_an_integer"),
        ],
    )
    async def test_post_refuses_what_the_global_setting_would(
        self, secs: object, code: str
    ) -> None:
        slot = _ChatSlot("test")
        with patch(_SAVE, new=AsyncMock(return_value=True)) as saver:
            async with TestClient(TestServer(_make_app(_mock_state(slot)))) as client:
                resp = await client.post("/api/chat/slots/test/turn-timeout", json={"secs": secs})
                assert resp.status == 400
                assert (await resp.json())["code"] == code
        assert slot.turn_timeout_secs is None
        saver.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_post_without_secs_is_refused(self) -> None:
        slot = _ChatSlot("test")
        async with TestClient(TestServer(_make_app(_mock_state(slot)))) as client:
            resp = await client.post("/api/chat/slots/test/turn-timeout", json={})
            assert resp.status == 400
            assert (await resp.json())["code"] == "secs_required"

    @pytest.mark.asyncio
    async def test_unknown_slot_is_not_found(self) -> None:
        async with TestClient(TestServer(_make_app(_mock_state()))) as client:
            resp = await client.post("/api/chat/slots/nope/turn-timeout", json={"secs": 3600})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_a_failed_persist_rolls_the_limit_back(self) -> None:
        slot = _ChatSlot("test")
        slot.turn_timeout_secs = 3600
        with patch(_SAVE, new=AsyncMock(side_effect=OSError("disk full"))):
            async with TestClient(TestServer(_make_app(_mock_state(slot)))) as client:
                resp = await client.post("/api/chat/slots/test/turn-timeout", json={"secs": 7200})
                assert resp.status == 500
                assert (await resp.json())["code"] == "persist_failed"
        assert slot.turn_timeout_secs == 3600

    @pytest.mark.asyncio
    async def test_a_refused_save_leaves_the_limit_unchanged(self) -> None:
        slot = _ChatSlot("test")
        with patch(_SAVE, new=AsyncMock(return_value=False)):
            async with TestClient(TestServer(_make_app(_mock_state(slot)))) as client:
                resp = await client.post("/api/chat/slots/test/turn-timeout", json={"secs": 7200})
                assert resp.status == 409
                assert (await resp.json())["code"] == "session_gone"
        assert slot.turn_timeout_secs is None

    @pytest.mark.asyncio
    async def test_a_failed_confirm_save_undoes_the_alias_mirror(self) -> None:
        slot = _ChatSlot("test")
        alias = _ChatSlot("alias")
        alias.turn_timeout_secs = 3600
        state = _mock_state(slot, alias)
        with (
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key", return_value="one-transcript"
            ),
            patch(_SAVE, new=AsyncMock(side_effect=[True, OSError("disk full")])),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/test/turn-timeout", json={"secs": 7200})
                assert resp.status == 500
        assert slot.turn_timeout_secs is None
        assert alias.turn_timeout_secs == 3600

    @pytest.mark.asyncio
    async def test_a_committed_limit_reaches_alias_slots_of_the_same_transcript(self) -> None:
        slot = _ChatSlot("test")
        alias = _ChatSlot("alias")
        state = _mock_state(slot, alias)
        with (
            patch(
                "kiro_crew.dashboard.chat_handlers.slot_history_key", return_value="one-transcript"
            ),
            patch(_SAVE, new=AsyncMock(return_value=True)),
            patch(_GLOBAL, return_value=14400.0),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/test/turn-timeout", json={"secs": 7200})
                assert resp.status == 200
        assert slot.turn_timeout_secs == 7200
        assert alias.turn_timeout_secs == 7200

    @pytest.mark.asyncio
    async def test_an_app_cannot_set_a_slot_it_does_not_own(self) -> None:
        slot = _ChatSlot("test")
        with patch(_SAVE, new=AsyncMock(return_value=True)) as saver:
            async with TestClient(
                TestServer(_make_app(_mock_state(slot), request_app="some-app"))
            ) as client:
                resp = await client.post("/api/chat/slots/test/turn-timeout", json={"secs": 7200})
                assert resp.status in (403, 404)
        assert slot.turn_timeout_secs is None
        saver.assert_not_awaited()


class TestRestore:
    @pytest.mark.parametrize(
        ("raw", "want"),
        [
            (28800, 28800),
            (1, CHAT_TURN_TIMEOUT_MIN),
            (10**9, CHAT_TURN_TIMEOUT_MAX),
            (None, None),
            (True, None),
            (3600.0, None),
            ("3600", None),
        ],
    )
    def test_a_persisted_value_is_validated(self, raw: object, want: int | None) -> None:
        assert _validate_turn_timeout_secs(raw) == want

    def test_channel_surfacing_restores_the_limit(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard import channel_slots
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        sessions = MagicMock(count=0)
        sessions.channel_key_for_stem = lambda stem: ""
        state = DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )
        slot = channel_slots.surface_channel_session(
            state,
            {"key": "slack:1785370133.085471", "title": "t", "modified": 0.0},
            {"turn_timeout_secs": 28800},
            [{"role": "user", "content": "hi"}],
        )
        assert slot is not None
        assert slot.turn_timeout_secs == 28800
