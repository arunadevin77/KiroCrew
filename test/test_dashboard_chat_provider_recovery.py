"""Dashboard chat: ``_run_chat`` recovering from a busy prompt."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state


class TestPromptBusyRecovery:
    """When kiro-cli returns 'Prompt already in progress', _run_chat must
    reset the session and re-queue the message so the next attempt cold-starts."""

    @pytest.mark.asyncio
    async def test_prompt_busy_resets_session_and_requeues(self, tmp_path: Path) -> None:
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat

        state = _make_state(tmp_path)
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.set_approval_policy = MagicMock()
        state.sessions.check_context_usage = MagicMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.is_yolo_active = MagicMock(return_value=False)
        state._background_tasks = set()

        slot = state.get_or_create_slot("busy-slot")
        slot.append("user", "hello", "msg msg-u")

        # Make client.stream raise "already in progress"
        mock_client = state.sessions.get_or_create.return_value[0]

        async def _raise_busy(msg):
            raise AcpError("Prompt error: {'data': 'Prompt already in progress'}")
            yield  # make it an async generator  # noqa: E501

        mock_client.stream = _raise_busy
        mock_client.stream_command = _raise_busy
        mock_client.shutdown = AsyncMock()

        await _run_chat(state, slot, "test message")

        # Session must be reset (kill the stuck kiro-cli process)
        state.sessions.reset.assert_awaited_once()
        # The finally block drains the re-queued message into a new task
        assert slot.task is not None
        # No ❌ error shown to the user for the busy case
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert not any("already in progress" in m.get("content", "") for m in error_msgs)

    @pytest.mark.asyncio
    async def test_process_exited_resets_session_and_requeues(self, tmp_path: Path) -> None:
        """When ACP subprocess dies (SIGTERM/SIGKILL), _run_chat must reset
        the session and re-queue the message so autonudges land on a fresh
        provider instead of a bare ❌ error card with no work done."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat

        state = _make_state(tmp_path)
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.set_approval_policy = MagicMock()
        state.sessions.check_context_usage = MagicMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.is_yolo_active = MagicMock(return_value=False)
        state._background_tasks = set()

        slot = state.get_or_create_slot("dead-slot")
        slot.append("user", "hello", "msg msg-u")

        mock_client = state.sessions.get_or_create.return_value[0]

        async def _raise_dead(msg):
            raise AcpError("ACP process exited (code=-15)")
            yield  # make it an async generator  # noqa: E501

        mock_client.stream = _raise_dead
        mock_client.stream_command = _raise_dead
        mock_client.shutdown = AsyncMock()

        await _run_chat(state, slot, "test message")

        state.sessions.reset.assert_awaited_once()
        assert slot.task is not None
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert not any("process exited" in m.get("content", "") for m in error_msgs)
