"""Owner gates for project-bound cron output on dashboard WebSockets."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.testing.wait import async_wait_until, await_until


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


class TestProjectBoundOutputWebSocketGate:
    @staticmethod
    def _ws() -> MagicMock:
        ws = MagicMock(closed=False)
        ws.get.side_effect = lambda key, default=None: {
            "_is_dashboard_user": True,
        }.get(key, default)
        return ws

    @pytest.mark.parametrize(
        "event,payload",
        [
            ("notification", {"kind": "cron", "body": "private", "project_bound": True}),
            (
                "chat_message",
                {"slot": "cron-j1", "content": "private", "meta": {"project_bound": True}},
            ),
        ],
    )
    def test_non_owner_is_denied_project_bound_output(
        self, state: DashboardState, event: str, payload: dict
    ) -> None:
        ws = self._ws()
        state.register_ws(ws)
        assert state._ws_client_allowed(ws, event, payload) is False

    @pytest.mark.parametrize("bad", ["false", 0, None, [], {}])
    def test_malformed_provenance_is_denied(self, state: DashboardState, bad: object) -> None:
        ws = self._ws()
        state.register_ws(ws)
        assert (
            state._ws_client_allowed(
                ws,
                "notification",
                {"kind": "cron", "body": "private", "project_bound": bad},
            )
            is False
        )

    def test_owner_still_receives_project_bound_output(self, state: DashboardState) -> None:
        ws = self._ws()
        state.register_ws(ws, owner=True)
        assert (
            state._ws_client_allowed(
                ws,
                "notification",
                {"kind": "cron", "body": "private", "project_bound": True},
            )
            is True
        )

    def test_explicitly_unbound_output_is_unchanged(self, state: DashboardState) -> None:
        ws = self._ws()
        state.register_ws(ws)
        assert (
            state._ws_client_allowed(
                ws,
                "notification",
                {"kind": "cron", "body": "safe", "project_bound": False},
            )
            is True
        )


class TestAResumedCronTranscriptsUnstampedFramesStayWithTheOwner:
    """History resumes a cron transcript under any name, so the gate judges the
    live slot's transcript identity -- never the ``cron-`` display name alone.

    An owner who reopens a project-bound job's transcript and types a follow-up
    produces unstamped ``chat_chunk`` / ``chat_message`` frames. Those carry
    unknown provenance inside a cron transcript and are withheld from non-owner
    sockets, as they are on the job's own ``cron-<id>`` tab.
    """

    _RESUMED = [("report", "cron:abcd1234"), ("cron_abcd1234", "")]

    @staticmethod
    def _ws() -> MagicMock:
        return TestProjectBoundOutputWebSocketGate._ws()

    @pytest.mark.parametrize("event", ["chat_chunk", "chat_message"])
    @pytest.mark.parametrize("key, linked", _RESUMED)
    def test_a_non_owner_is_denied_unstamped_frames(
        self, state: DashboardState, event: str, key: str, linked: str
    ) -> None:
        state.get_or_create_slot(key).linked_session_key = linked
        ws = self._ws()
        state.register_ws(ws)
        assert state._ws_client_allowed(ws, event, {"slot": key, "content": "private"}) is False

    @pytest.mark.parametrize("key, linked", _RESUMED)
    def test_the_owner_still_receives_them(
        self, state: DashboardState, key: str, linked: str
    ) -> None:
        state.get_or_create_slot(key).linked_session_key = linked
        ws = self._ws()
        state.register_ws(ws, owner=True)
        assert state._ws_client_allowed(ws, "chat_chunk", {"slot": key, "content": "x"}) is True

    @pytest.mark.parametrize("key, linked", _RESUMED)
    def test_an_explicitly_unbound_row_still_reaches_a_non_owner(
        self, state: DashboardState, key: str, linked: str
    ) -> None:
        state.get_or_create_slot(key).linked_session_key = linked
        ws = self._ws()
        state.register_ws(ws)
        frame = {"slot": key, "content": "safe", "meta": {"project_bound": False}}
        assert state._ws_client_allowed(ws, "chat_message", frame) is True

    def test_an_ordinary_chat_slot_is_unchanged(self, state: DashboardState) -> None:
        state.get_or_create_slot("chat-1")
        ws = self._ws()
        state.register_ws(ws)
        assert state._ws_client_allowed(ws, "chat_chunk", {"slot": "chat-1", "content": "x"})

    def test_a_cron_transcript_name_is_withheld_before_the_slot_exists(
        self, state: DashboardState
    ) -> None:
        # The name alone already addresses a cron transcript file, so a frame
        # that races the slot's registration is still withheld.
        ws = self._ws()
        state.register_ws(ws)
        assert not state._ws_client_allowed(ws, "chat_chunk", {"slot": "cron_abcd1234"})


class TestTheSseGateJudgesTheSameIdentity:
    """``/api/stream`` reads the same producer notes, so it must ask the same
    question: a resumed cron transcript's unstamped frame stays with the owner."""

    @pytest.mark.asyncio
    async def test_a_non_owner_stream_skips_a_resumed_cron_transcripts_frame(
        self, state: DashboardState, monkeypatch
    ) -> None:
        import asyncio

        from kiro_crew.dashboard.handlers import updates

        state.get_or_create_slot("report").linked_session_key = "cron:abcd1234"
        state.get_or_create_slot("chat-1")
        queue: asyncio.Queue[dict] = asyncio.Queue()
        for slot in ("report", "chat-1"):
            queue.put_nowait(
                {
                    "_type": "chat_message",
                    "slot": slot,
                    "role": "assistant",
                    "content": f"from {slot}",
                }
            )
        monkeypatch.setattr(state, "register_sse", lambda: queue)
        monkeypatch.setattr(state, "unregister_sse", lambda q: None)
        monkeypatch.setattr(state, "status_snapshot", lambda *a, **k: {})
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: False,
        )
        writes: list[bytes] = []

        class _Resp:
            content_type = ""
            headers: dict = {}

            async def prepare(self, request):
                return None

            async def write(self, data: bytes):
                writes.append(data)

        monkeypatch.setattr(updates.web, "StreamResponse", _Resp)
        request = MagicMock()
        request.app = {"state": state}
        request.get = {"is_dashboard_user": True}.get
        updates.shutdown_event.clear()
        task = asyncio.ensure_future(updates.api_stream(request))
        await async_wait_until(
            lambda: any(b"from chat-1" in w for w in writes),
            describe=lambda: f"writes={writes!r}",
        )
        updates.shutdown_event.set()
        await await_until(task, what="the stream handler returning on shutdown")
        updates.shutdown_event.clear()
        text = b"".join(writes).decode()
        assert "from chat-1" in text, "the control frame never arrived"
        assert "from report" not in text
