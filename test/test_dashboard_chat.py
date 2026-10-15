"""Tests for dashboard chat session — ACP-death redaction, transient retry, model fallback
and switching, source links, tail fork, reload, close broadcasts, the unflushed tail, the
interrupted-turn marker and stalled readers.

The other dashboard chat tests live in the sibling ``test_dashboard_chat_*.py``
files, one topic each; shared fakes are in ``dashboard_chat_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_app_with_agent_routes, _make_state
from dashboard_chat_helpers import _provider_mock

from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.testing.wait import async_wait_until, await_until

# ── Tests: AcpProcessDied handler in _run_chat ──


class TestAcpProcessDiedRecovery:
    """Verify _run_chat handles AcpProcessDied with retry logic, redaction, and session reset."""

    def _make_state_and_slot(self, tmp_path):
        from kiro_crew.dashboard.chat_runner import _run_chat

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

        slot = state.get_or_create_slot("pipe-death-slot")
        slot.append("user", "hello", "msg msg-u")

        mock_client = state.sessions.get_or_create.return_value[0]
        mock_client.shutdown = AsyncMock()
        return state, slot, mock_client, _run_chat

    def _make_stream_raise(self, mock_client, exc):
        async def _raise(msg):
            raise exc
            yield  # noqa: E501

        mock_client.stream = _raise
        mock_client.stream_command = _raise

    @pytest.mark.asyncio
    async def test_partial_assistant_text_redacted(self, tmp_path: Path) -> None:
        """Pipe death mid-stream preserves redacted output and queues a continuation."""
        from kiro_crew.acp.client import AcpProcessDied
        from kiro_crew.dashboard.chat_utils import (
            _CONN_RECOVER_MSG,
            SYNTHETIC_RECOVERY_KIND,
            RecoveryPayload,
        )
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._titled = True

        async def _stream_then_die(msg):
            yield LLMEvent(
                kind=EVENT_TEXT_CHUNK, text="partial output with AKIA1234567890ABCDEF secret"
            )
            raise AcpProcessDied("pipe broken")

        client.stream = _stream_then_die
        client.stream_command = _stream_then_die

        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await _run_chat(state, slot, "test message")

        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert assistant_msgs, "Expected at least one assistant message with redacted content"
        for m in assistant_msgs:
            assert "AKIA1234567890ABCDEF" not in m.get("content", "")
        assert slot._queue == [
            {
                "id": slot._queue[0]["id"],
                "content": _CONN_RECOVER_MSG,
                "kind": SYNTHETIC_RECOVERY_KIND,
                # A continuation, not the user's request: the turn had emitted, so
                # this text is the runner's and must not mirror as user speech.
                "payload": RecoveryPayload.CONTINUATION,
                # Admission stamp: recovery requeues record the containment
                # that held at requeue so the drain can re-validate the retry.
                "meta": slot._queue[0]["meta"],
            }
        ]

    @pytest.mark.asyncio
    async def test_cancelled_error_redacts_partial_text(self, tmp_path: Path) -> None:
        """CancelledError mid-stream → partial output redacted before display."""
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)

        async def _stream_then_cancel(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial with AKIA1234567890ABCDEF key")
            raise asyncio.CancelledError()

        client.stream = _stream_then_cancel
        client.stream_command = _stream_then_cancel

        await _run_chat(state, slot, "test message")

        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert assistant_msgs, "Expected at least one assistant message with redacted content"
        for m in assistant_msgs:
            assert "AKIA1234567890ABCDEF" not in m.get("content", "")


# ── Transient backend 5xx retry on the interactive chat path ──


class TestRunChatTransientRetry:
    """The interactive _run_chat stream loop reuses the llm_helpers transient
    classifier + backoff to retry a transient
    backend 5xx WITHOUT resetting the live session, guarded so a partial
    (already-streamed) response is never re-run."""

    _TRANSIENT = "Prompt error: {'message': 'Internal error: API Error: Internal server error'}"
    _AUTH = "Bedrock authentication failed. Run 'ada credentials update'"

    @staticmethod
    def _make_state(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.push_refresh = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        return state

    @staticmethod
    def _wire_sessions(state, client):
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.get_pid = MagicMock(return_value=None)
        state.sessions.check_context_usage = MagicMock()
        state.sessions.record_success = MagicMock()
        state.sessions.record_failure = AsyncMock()
        state.sessions.release = MagicMock()
        # reset is an AsyncMock so we can assert it is NEVER awaited — a
        # transient 5xx must NOT reset the (still-alive) session.
        state.sessions.reset = AsyncMock()
        # discard_conversation is the poisoned-conversation escalation (clears
        # the resume sid, keeps the session-map entry with its channel
        # linkage); mocked so tests can assert exactly when it fires.
        state.sessions.discard_conversation = AsyncMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))

    @staticmethod
    def _client(stream):
        client = _provider_mock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        # These are sync accessors on the real provider client; _run_chat
        # calls them without awaiting, so a bare AsyncMock attribute here
        # would leave an unawaited coroutine per turn.
        client.context_window_tokens = MagicMock(return_value=0)
        client.context_used_tokens = MagicMock(return_value=0)
        client.mcp_session_report = MagicMock(return_value=None)
        client.pop_pending_oauth_requests = MagicMock(return_value=[])
        client.available_models = MagicMock(return_value=[])
        # _drain_session_init_oauth_requests reaches pop_pending_oauth_requests
        # via client.client (the nested ACP client), not the front client
        # itself; leaving client.client as a bare AsyncMock attribute would
        # make that nested lookup resolve to another never-awaited coroutine.
        client.client = None
        client.stream = stream
        client.stream_command = stream
        # The poisoned-conversation canary reads the session's served model
        # via the provider's PUBLIC served_model accessor — mock that public
        # seam, not the provider internals it happens to be backed by.
        client.served_model = "claude-test-model"
        return client

    @staticmethod
    async def _drain_bg(state, limit=30):
        """Drive the finally-block re-queue cascade (each retry spawns a
        background _run_chat task) to completion. Bounded by the retry budget."""
        for _ in range(limit):
            pending = [t for t in list(state._background_tasks) if not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    def _err_texts(self, slot):
        return [m["content"] for m in slot.messages if m.get("role") == "error"]

    def _assistant_texts(self, slot):
        return [m["content"] for m in slot.messages if m.get("role") == "assistant"]

    @staticmethod
    def _record_queue_inserts(monkeypatch, slot) -> list[dict]:
        inserts: list[dict] = []
        repo = slot._queue_repository
        real_insert = repo.queue_insert

        def _record(owner, index, content, kind="", *args, **kw):
            inserts.append({"content": content, "kind": kind})
            return real_insert(owner, index, content, kind, *args, **kw)

        monkeypatch.setattr(repo, "queue_insert", _record)
        return inserts

    _BINDING_MISMATCH = (
        "Invalid `signature` in `thinking` block. "
        "The block is bound to a different conversation."
    )

    @staticmethod
    def _binding_failure():
        from kiro_crew.acp.client import _raise_acp_error

        _raise_acp_error(
            {
                "code": -32603,
                "message": "Prompt failed",
                "data": TestRunChatTransientRetry._BINDING_MISMATCH,
            }
        )

    @pytest.mark.asyncio
    async def test_transient_after_thinking_only_retries(self, tmp_path, monkeypatch):
        """A transient 5xx that lands AFTER reasoning streamed but before any
        answer token or tool call is still retried: thinking is ephemeral,
        broadcast-only output, so it does NOT flip _turn_emitted. This pins the
        deliberate decision in the EVENT_THINKING_CHUNK branch — if thinking
        ever starts being persisted, this guard must become a turn-emit to
        avoid a double-emit."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_THINKING_CHUNK,
            LLMEvent,
        )

        call_count = 0
        thinking_seen = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            # Reasoning streams on BOTH attempts (cosmetic re-stream is the
            # accepted cost of retrying a thinking-only turn).
            yield LLMEvent(kind=EVENT_THINKING_CHUNK, text="let me think...")
            if call_count == 1:
                # Transient 5xx after thinking, before any answer token.
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)

        # Count chat_thinking broadcasts to prove reasoning re-streamed.
        _orig_broadcast = state.broadcast_ws

        def _count_thinking(event, payload, *a, **kw):
            nonlocal thinking_seen
            if event == "chat_thinking":
                thinking_seen += 1
            return _orig_broadcast(event, payload, *a, **kw)

        state.broadcast_ws = MagicMock(side_effect=_count_thinking)

        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # Thinking did NOT block the retry: initial transient + one retry.
        assert call_count == 2
        # Reasoning re-streamed on the retry. The streaming redaction buffer
        # (issue 3) may split/coalesce a thinking chunk across broadcasts, so
        # assert "streamed on both attempts" rather than an exact per-chunk count.
        assert thinking_seen >= 2
        # Recovered cleanly on the live session (no reset, no ❌).
        assert any("ok-result" in t for t in self._assistant_texts(slot))
        assert not any(t.startswith("❌") for t in self._err_texts(slot))
        assert any("Connection unstable" in t for t in self._err_texts(slot))
        state.sessions.reset.assert_not_awaited()
        assert slot._transient_5xx_retries == 0

    @pytest.mark.asyncio
    async def test_thinking_only_failures_never_accrue_discard_streak(self, tmp_path, monkeypatch):
        """A turn that streamed REASONING before dying is a mid-generation
        failure — the backend demonstrably serves this conversation — not the
        poisoned pre-stream signature. Repeated thinking-then-transient-death
        turns must never accrue the discard streak (and so can never reach the
        canary/discard), even though they leave _turn_emitted False for retry
        purposes. Locks in the _turn_thought gate on _prestream_exhausted."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_THINKING_CHUNK, LLMEvent

        async def _think_then_die(msg):
            yield LLMEvent(kind=EVENT_THINKING_CHUNK, text="reasoning...")
            raise AcpError(self._TRANSIENT)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_think_then_die)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            # Well past POISONED_SESSION_CYCLES worth of exhausted ladders.
            for i in range(3):
                await _run_chat(state, slot, f"msg-{i}")
                await self._drain_bg(state)

        # Thinking activity keeps the streak at zero every cycle: never
        # eligible, so no canary probe and no discard — the conversation is
        # being served, just dying mid-generation.
        assert slot._prestream_exhausted_cycles == 0
        assert not slot._poisoned_reset_used
        state.sessions.discard_conversation.assert_not_awaited()


# ── Bulk model switch (api_chat_slots_model) ──


class TestRunChatModelFallback:
    """Throttle-exhaustion model fallback (agent.fallback_model) on the
    interactive path: budget exhausts → substitute set_model → visible notice
    card → re-queue on the SAME live session; empty chain is byte-for-byte
    today's terminal error; the sticky swap is restored at the next genuine
    turn start."""

    _TRANSIENT = "Prompt error: {'message': 'Internal error: API Error: Internal server error'}"

    @pytest.mark.asyncio
    async def test_fallback_swap_refreshes_the_served_model_cache(self, tmp_path, monkeypatch):
        """The swap moves the LIVE session onto the candidate without a spawn,
        so the spawn-time cache would keep naming the primary; it must follow
        the provider's served model instead."""
        from kiro_crew.dashboard.chat_runner import _fallback_swap_for_turn

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model",),
        )
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.record_served_model("primary-model")

        class _Client:
            served_model = "primary-model"

            async def set_model(self, target):
                self._model = target
                self.served_model = target

        client = _Client()
        candidate = await _fallback_swap_for_turn(slot, client)
        assert candidate == "fallback-model"
        assert slot.served_model == "fallback-model"

    @pytest.mark.asyncio
    async def test_fallback_swap_serialises_on_the_pick_lock(self, tmp_path, monkeypatch):
        """REGRESSION (GPT finding on c97f2f2d): the swap awaits set_model, and
        an explicit pick landing during that await could be overwritten and
        snapshotted as fallback state. The swap must hold _model_pick_lock —
        with the lock pre-acquired, the swap may not advance until release.
        This closes the LAST writer: explicit pick, bulk pick, restore probe,
        and swap all serialise on the same per-slot lock."""
        import asyncio

        from kiro_crew.dashboard.chat_runner import _fallback_swap_for_turn

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model",),
        )
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        entered = asyncio.Event()

        class _Client:
            async def set_model(self, target):
                entered.set()
                self._model = target

        client = _Client()

        await slot._model_pick_lock.acquire()
        try:
            task = asyncio.create_task(_fallback_swap_for_turn(slot, client))
            await asyncio.sleep(0.05)
            # Lock held ⇒ the swap must not have reached set_model yet.
            assert not entered.is_set()
            assert not task.done()
        finally:
            slot._model_pick_lock.release()
        candidate = await await_until(task, what="the fallback swap finishing")
        assert candidate == "fallback-model"
        assert slot._active_fallback_model == "fallback-model"

    @pytest.mark.asyncio
    async def test_fallback_swap_serialises_on_the_session_lock(self, tmp_path, monkeypatch):
        """The swap awaits set_model and then snapshots the shared client pick
        epoch. The per-slot pick lock is disjoint across aliases, so a pick made
        through a DIFFERENT alias of the same wire session could land in that
        await and be absorbed into the snapshot, after which the restore reads
        not-stale and silently overwrites the user's choice. The swap must hold
        the session-scoped switch lock too — with it pre-acquired, the swap may
        not advance until release. Mirrors the restore probe and refusal path.

        Mutation guard: drop the slot_switch_session_lock acquisition around the
        swap and this test reddens, because the swap reaches set_model while the
        session lock is held elsewhere."""
        import asyncio

        from kiro_crew.dashboard.chat_runner import _fallback_swap_for_turn
        from kiro_crew.dashboard.chat_utils import effective_session_key
        from kiro_crew.llm_helpers import slot_switch_session_lock

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model",),
        )
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        entered = asyncio.Event()

        class _Client:
            async def set_model(self, target):
                entered.set()
                self._model = target

        client = _Client()

        _session_lock = slot_switch_session_lock(effective_session_key(slot))
        await _session_lock.acquire()
        try:
            task = asyncio.create_task(_fallback_swap_for_turn(slot, client))
            await asyncio.sleep(0.05)
            # Session lock held ⇒ the swap must not have reached set_model yet.
            assert not entered.is_set()
            assert not task.done()
        finally:
            _session_lock.release()
        candidate = await await_until(task, what="the fallback swap finishing")
        assert candidate == "fallback-model"
        assert slot._active_fallback_model == "fallback-model"

    @pytest.mark.asyncio
    async def test_stop_during_fallback_backoff_drops_the_requeue(self, tmp_path, monkeypatch):
        """REGRESSION (review finding on 1a61ddcf): a Stop pressed during the
        fallback backoff sleep resolves while no prompt is active; without the
        post-sleep guard the cancelled prompt is requeued and executes on the
        fallback anyway."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model",),
        )
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        async def _stop_during_sleep(_secs):
            # Simulate the user pressing Stop while the backoff sleeps: the
            # monotonic generation increments even though the stop resolves
            # back to "idle" before the sleep returns. Scoped to the backoff this
            # test is about, because the same-model arm re-reads the stop signals
            # after ITS wait too: an unscoped press lands on the FIRST backoff,
            # drops the replay there, and the fallback arm this test measures is
            # then never reached at all.
            if slot._fallback_walked:
                slot._stop_generation = getattr(slot, "_stop_generation", 0) + 1

        with patch("asyncio.sleep", new=AsyncMock(side_effect=_stop_during_sleep)):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # Only the primary's budget ran; the cancelled prompt was NOT replayed
        # on the fallback (unfixed code replays it: +FALLBACK_CANDIDATE_ATTEMPTS).
        assert call_count == TRANSIENT_RETRIES + 1
        assert not any(q for q in getattr(slot, "_queue", []) if "hello" in str(q))
        # The drop arm ends the turn, so it must mirror the landed/terminal
        # per-turn resets: a fresh transient budget and a cleared walk (which
        # re-enables the restore probe). Sticky _active_fallback_model
        # deliberately survives for the probe to heal.
        assert slot._transient_5xx_retries == 0
        assert slot._fallback_candidate_idx == 0
        assert slot._fallback_walked == []

    _make_state = staticmethod(TestRunChatTransientRetry._make_state)
    _wire_sessions = staticmethod(TestRunChatTransientRetry._wire_sessions)
    _drain_bg = staticmethod(TestRunChatTransientRetry._drain_bg)

    def _err_texts(self, slot):
        return [m["content"] for m in slot.messages if m.get("role") == "error"]

    def _notice_texts(self, slot):
        return [m["content"] for m in slot.messages if m.get("role") == "notice"]

    def _assistant_texts(self, slot):
        return [m["content"] for m in slot.messages if m.get("role") == "assistant"]

    @staticmethod
    def _client(stream, advertised=("fallback-model",)):
        client = TestRunChatTransientRetry._client(stream)
        client._model = "claude-test-model"
        client.available_models = MagicMock(return_value=[{"modelId": m} for m in advertised])

        async def _set_model(model_id):
            # Mimic the real substitute path's bookkeeping so read_turn_model
            # (and therefore meta.turn_stats.model) reports the candidate.
            client._model = model_id
            client.served_model = model_id

        client.set_model = AsyncMock(side_effect=_set_model)
        return client

    @pytest.mark.asyncio
    async def test_budget_exhaustion_swaps_and_recovers(self, tmp_path, monkeypatch):
        """After the same-model budget exhausts, the session is moved onto the
        chain's candidate, the swap is ANNOUNCED as a persisted notice card,
        the turn recovers, and turn stats record the candidate."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model",),
        )
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count <= TRANSIENT_RETRIES + 1:
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="fb-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # 4 attempts on the primary, 1 on the candidate (success).
        assert call_count == TRANSIENT_RETRIES + 2
        client.set_model.assert_awaited_once_with("fallback-model")
        # The swap is announced — a persisted notice card, never silent.
        notices = self._notice_texts(slot)
        assert any(
            "claude-test-model" in t and "fallback-model" in t and "throttled" in t for t in notices
        ), f"expected a fallback notice card, got {notices!r}"
        # Recovered on the candidate; no terminal ❌.
        assert any("fb-result" in t for t in self._assistant_texts(slot))
        assert not any(t.startswith("❌") for t in self._err_texts(slot))
        # The live session was never reset — same-session recovery.
        state.sessions.reset.assert_not_awaited()
        # Sticky fallback state armed for the next turn's restore probe...
        assert slot._active_fallback_model == "fallback-model"
        assert slot._fallback_primary_model == "claude-test-model"
        # ...while the per-cycle walk state reset with the budgets.
        assert slot._transient_5xx_retries == 0
        assert slot._fallback_candidate_idx == 0
        assert slot._fallback_walked == []
        # Turn stats carry the model that ACTUALLY served the turn: the
        # substitute set_model updated the provider's bookkeeping (mimicked by
        # this mock exactly as AcpSessionHandle/AcpClient.set_model do), so the
        # read_turn_model seam — the sole source for meta.turn_stats.model
        # (see _attach_turn_stats, pinned by test_turn_stats.py) — reports the
        # candidate. The mocked provider yields no duration, so the stats dict
        # itself is not attached here; the model field's attach contract is
        # already pinned by test_turn_stats.py::test_model_included_when_resolved.
        from kiro_crew.dashboard.handlers.usage import read_turn_model

        assert read_turn_model(client) == "fallback-model"

    @pytest.mark.asyncio
    async def test_empty_chain_is_todays_terminal_error(self, tmp_path, monkeypatch):
        """REGRESSION PIN: with no chain configured, budget exhaustion surfaces
        the terminal ❌ exactly as before — no swap, no notice, no extra
        attempts."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._agent_fallback_chain", lambda: ())
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        assert call_count == TRANSIENT_RETRIES + 1
        client.set_model.assert_not_awaited()
        assert not self._notice_texts(slot)
        assert any(t.startswith("❌") for t in self._err_texts(slot))
        assert slot._active_fallback_model == ""

    @pytest.mark.asyncio
    async def test_chain_exhaustion_surfaces_error_with_story(self, tmp_path, monkeypatch):
        """Every candidate also fails: the terminal ❌ carries the chain's
        story (primary throttled + which fallbacks were tried), and each
        candidate got exactly two attempts."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model",),
        )
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # 4 on the primary + 2 on the candidate (initial + one retry).
        assert call_count == TRANSIENT_RETRIES + 3
        client.set_model.assert_awaited_once_with("fallback-model")
        errs = self._err_texts(slot)
        assert any(
            "fallback-model" in t and "also unavailable" in t for t in errs
        ), f"expected the chain story on the terminal error, got {errs!r}"
        # Per-cycle walk state refreshed for the next user-initiated cycle.
        assert slot._fallback_candidate_idx == 0
        assert slot._fallback_walked == []

    @pytest.mark.asyncio
    async def test_candidate_budget_routes_through_shared_rewind_body(self, tmp_path, monkeypatch):
        """DRIFT PIN: the post-swap counter rewind must come
        from llm_helpers.fallback_rewound_transient_budget — not a re-encoded
        local constant. Patching the shared body to grant no extra pass drops
        the candidate to a single attempt."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model",),
        )
        # Rewind to the exhaustion threshold: the re-queued attempt runs, and
        # its failure immediately re-enters the fallback branch (no same-model
        # retry pass) — one attempt on the candidate instead of two.
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.fallback_rewound_transient_budget",
            lambda: TRANSIENT_RETRIES,
        )
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # 4 on the primary + only 1 on the candidate (vs 2 unpatched — see
        # test_chain_exhaustion_surfaces_error_with_story).
        assert call_count == TRANSIENT_RETRIES + 2
        client.set_model.assert_awaited_once_with("fallback-model")

    @pytest.mark.asyncio
    async def test_restore_probe_fires_on_next_genuine_turn(self, tmp_path, monkeypatch):
        """A sticky fallback from an earlier cycle is probed back to the
        primary at the start of the next genuine user turn — quietly (no
        chat card), clearing the sticky state on success."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        client._model = "fallback-model"
        client.served_model = "fallback-model"
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot._active_fallback_model = "fallback-model"
        slot._fallback_primary_model = "claude-test-model"

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello again")
            await self._drain_bg(state)

        client.set_model.assert_awaited_once_with("claude-test-model")
        assert slot._active_fallback_model == ""
        assert slot._fallback_primary_model == ""
        # Quiet restore: no notice card for recovery.
        assert not self._notice_texts(slot)
        assert any("ok-result" in t for t in self._assistant_texts(slot))

    @pytest.mark.asyncio
    async def test_restore_probe_skipped_when_session_moved_on(self, tmp_path, monkeypatch):
        """If the session is not on our fallback (explicit user pick),
        the stale sticky state is dropped WITHOUT touching the model."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        client._model = "user-picked-model"
        client.served_model = "user-picked-model"
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot._active_fallback_model = "fallback-model"
        slot._fallback_primary_model = "claude-test-model"
        # A stale provider marker from the abandoned fallback must be dropped
        # WITH the slot state — a surviving marker would re-seed the long-dead
        # primary into a later, unrelated fallback walk.
        from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR

        setattr(client, TURN_FALLBACK_ATTR, ("claude-test-model", "fallback-model"))

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello again")
            await self._drain_bg(state)

        client.set_model.assert_not_awaited()
        assert slot._active_fallback_model == ""
        assert getattr(client, TURN_FALLBACK_ATTR) is None

    @pytest.mark.asyncio
    async def test_restore_probe_honors_explicit_pick_of_the_fallback_itself(
        self, tmp_path, monkeypatch
    ):
        """REGRESSION (review finding): a user who EXPLICITLY picks the very
        model the session fell back to changes slot.model but not the served
        model — the probe must drop the sticky state without restoring the
        old primary over their pick."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        client._model = "fallback-model"
        client.served_model = "fallback-model"
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        # Fallback activated while the slot was configured on the primary...
        slot._active_fallback_model = "fallback-model"
        slot._fallback_primary_model = "claude-test-model"
        slot._fallback_slot_model = "claude-test-model"
        slot._fallback_pick_gen = slot._model_pick_gen
        # ...then the user explicitly picked the fallback model themselves
        # (the pick surface writes slot.model AND bumps the pick generation).
        slot.model = "fallback-model"
        slot._model_pick_gen += 1

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello again")
            await self._drain_bg(state)

        # No restore fired — the explicit pick wins — and the sticky state is gone.
        client.set_model.assert_not_awaited()
        assert slot._active_fallback_model == ""
        assert slot._fallback_slot_model == ""
        # The explicit pick itself is untouched.
        assert slot.model == "fallback-model"

    @pytest.mark.asyncio
    async def test_restore_probe_heals_automatic_backfill(self, tmp_path, monkeypatch):
        """REGRESSION (verifier finding): on an UNPINNED slot, a served-
        fallback value that reached slot.model without a pick-generation bump
        (pre-guard pollution) must NOT be misread as an explicit pick: the
        probe restores the primary, drops the sticky state, and heals
        slot.model back to the activation snapshot (else the fallback id would
        be re-sent as a pin on resume)."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        client._model = "fallback-model"
        client.served_model = "fallback-model"
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        # Fallback activated on an UNPINNED slot (empty snapshot, no pick)...
        slot._active_fallback_model = "fallback-model"
        slot._fallback_primary_model = "claude-test-model"
        slot._fallback_slot_model = ""
        slot._fallback_pick_gen = slot._model_pick_gen
        # ...then the automatic backfill wrote the served fallback into
        # slot.model WITHOUT bumping the pick generation.
        slot.model = "fallback-model"

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello again")
            await self._drain_bg(state)

        # The restore fired (backfill is not a pick) and healed the slot pin.
        client.set_model.assert_awaited_once_with("claude-test-model")
        assert slot._active_fallback_model == ""
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_backfill_never_persists_the_fallback_candidate(self, tmp_path, monkeypatch):
        """REGRESSION (review finding, the span invariant): while a fallback is
        ACTIVE, the automatic model backfill must not write the served
        candidate into slot.model — slot.model is PERSISTED, and the in-memory
        sticky state is not, so a gateway restart would turn the temporary
        fallback into a permanent pin. An unpinned slot stays unpinned for the
        fallback's duration."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        # The provider resolves to the FALLBACK candidate (it is serving).
        client._model = "fallback-model"
        client.served_model = "fallback-model"
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot.model = ""  # unpinned
        slot._active_fallback_model = "fallback-model"
        slot._fallback_primary_model = ""  # primary unknown -> no restore
        slot._fallback_pick_gen = slot._model_pick_gen

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # The turn ran, but the persisted pin was never written with the
        # candidate. (The unknown-primary probe clears the sticky state — the
        # pin staying empty is the property under test.)
        assert slot.model == ""


class TestSlotProbeWrapsSharedRestoreBody:
    """DRIFT PIN: the slot restore probe is a thin adapter over
    llm_helpers.probe_fallback_restore — only the slot pick-gen/heal/clear
    logic lives locally. These tests pin the delegation and the slot-specific
    hooks it hands over; the probe mechanics themselves are pinned in
    test_llm_helpers.py and the end-to-end behavior in
    TestRunChatModelFallback above."""

    @staticmethod
    def _slot(**overrides):
        from types import SimpleNamespace

        defaults = dict(
            key="s1",
            model="",
            _model_pick_lock=None,
            _active_fallback_model="fb-1",
            _fallback_primary_model="primary-m",
            _fallback_slot_model="",
            _model_pick_gen=3,
            _fallback_pick_gen=3,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    @pytest.mark.asyncio
    async def test_delegates_slot_state_to_the_shared_body(self):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR

        slot = self._slot()
        client = MagicMock()
        setattr(client, TURN_FALLBACK_ATTR, ("primary-m", "fb-1"))
        with patch(
            "kiro_crew.dashboard.chat_runner.probe_fallback_restore", new_callable=AsyncMock
        ) as probe:
            await chat_runner._probe_fallback_restore_for_slot(slot, client)

        probe.assert_awaited_once()
        kwargs = probe.await_args.kwargs
        assert kwargs["surface"] == "dashboard"
        assert kwargs["state"] == ("primary-m", "fb-1")
        assert kwargs["stale"] is False
        assert kwargs["log_suffix"] == ", slot=s1"
        # The handed-over clear drops slot fields AND the provider marker as
        # one logical record.
        kwargs["clear"]()
        assert slot._active_fallback_model == ""
        assert slot._fallback_primary_model == ""
        assert getattr(client, TURN_FALLBACK_ATTR) is None

    @pytest.mark.asyncio
    async def test_explicit_pick_generation_bump_rides_in_as_stale(self):
        from kiro_crew.dashboard import chat_runner

        slot = self._slot(_model_pick_gen=4)  # picked after the swap
        with patch(
            "kiro_crew.dashboard.chat_runner.probe_fallback_restore", new_callable=AsyncMock
        ) as probe:
            await chat_runner._probe_fallback_restore_for_slot(slot, MagicMock())
        assert probe.await_args.kwargs["stale"] is True

    @pytest.mark.asyncio
    async def test_heal_hook_restores_the_activation_snapshot(self):
        from kiro_crew.dashboard import chat_runner

        slot = self._slot(model="fb-1", _fallback_slot_model="")  # backfilled pin
        with patch(
            "kiro_crew.dashboard.chat_runner.probe_fallback_restore", new_callable=AsyncMock
        ) as probe:
            await chat_runner._probe_fallback_restore_for_slot(slot, MagicMock())
        probe.await_args.kwargs["on_restored"]()
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_no_active_fallback_never_reaches_the_shared_body(self):
        from kiro_crew.dashboard import chat_runner

        slot = self._slot(_active_fallback_model="")
        with patch(
            "kiro_crew.dashboard.chat_runner.probe_fallback_restore", new_callable=AsyncMock
        ) as probe:
            await chat_runner._probe_fallback_restore_for_slot(slot, MagicMock())
        probe.assert_not_awaited()

    def test_sticky_clear_drops_marker_and_slot_fields(self):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR

        slot = self._slot()
        client = MagicMock()
        setattr(client, TURN_FALLBACK_ATTR, ("primary-m", "fb-1"))
        chat_runner._clear_fallback_sticky_state(slot, client)
        assert getattr(client, TURN_FALLBACK_ATTR) is None
        assert slot._active_fallback_model == ""
        assert slot._fallback_primary_model == ""
        assert slot._fallback_slot_model == ""

    def test_sticky_clear_keeps_slot_record_when_marker_clear_fails(self):
        """DRIFT PIN: a failed provider-marker clear must NOT
        blank the slot fields — the slot probe keys on _active_fallback_model,
        so keeping the record is what makes the clear retryable next turn.
        Blanking the fields around a surviving marker would orphan it."""
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR

        class _HostileClient:
            @property
            def _kc_active_fallback(self):  # TURN_FALLBACK_ATTR
                raise RuntimeError("hostile marker")

        assert TURN_FALLBACK_ATTR == "_kc_active_fallback"
        slot = self._slot()
        chat_runner._clear_fallback_sticky_state(slot, _HostileClient())  # must not raise
        assert slot._active_fallback_model == "fb-1"
        assert slot._fallback_primary_model == "primary-m"


class TestSlotsGetWarmsGitLabAllowlist:
    """GET /api/chat/slots warms the self-managed GitLab allowlist first.

    Slot source-link extraction is synchronous and cannot load the allowlist, so
    a cold direct GET (no WebSocket yet) would serialize against the empty
    snapshot and omit every configured self-hosted MR chip. The WS path has its
    own test; this pins the direct-fetch path, which is a separate entry point.
    """

    @pytest.mark.asyncio
    async def test_cold_get_returns_the_authorized_self_hosted_link(self, monkeypatch) -> None:
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.chat_handlers import api_chat_slots
        from kiro_crew.dashboard.handlers import source_providers as sp

        url = "https://gitlab.acme.internal/team/api/-/merge_requests/7"
        order: list[str] = []

        # Cold snapshot: nothing has loaded the allowlist yet.
        monkeypatch.setattr(sp, "_gitlab_hosts_snapshot", frozenset())
        monkeypatch.setattr(sp, "_gitlab_hosts_loaded_at", 0.0)
        monkeypatch.setattr(sp, "_gitlab_hosts_generation", 0)

        async def fake_ensure() -> frozenset:
            order.append("ensure")
            sp._publish_provider_hosts(frozenset({"gitlab.acme.internal"}), frozenset())
            return frozenset({"gitlab.acme.internal"})

        monkeypatch.setattr(sp, "ensure_gitlab_hosts_loaded", fake_ensure)
        monkeypatch.setattr(sp, "schedule_check_refresh", lambda *a, **k: [])

        state = MagicMock()
        slot = _ChatSlot("s1")
        slot.append("assistant", f"Opened {url}", ts="t1")

        def serialize(**_kwargs):
            order.append("serialize")
            return [slot.to_dict()]

        state.serialize_slots.side_effect = serialize

        @web.middleware
        async def dashboard_auth_marker(request, handler):
            request["app"] = ""
            request["user"] = ""
            return await handler(request)

        app = web.Application(middlewares=[dashboard_auth_marker])
        app["state"] = state
        app.router.add_get("/api/chat/slots", api_chat_slots)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/slots")
            assert resp.status == 200
            payloads = await resp.json()

        # Warm-up strictly precedes serialization, and the authorized MR is a link.
        assert order[:2] == ["ensure", "serialize"]
        links = [link["url"] for p in payloads for link in p.get("source_links", [])]
        assert url in links


class TestSourceLinkUrlsExcludeIssues:
    """The chip-status sweep reaches `gh pr view`, which has no meaning for an
    issue. Issue links must be filtered out before scheduling, not merely
    refused downstream, so a session that mentions only issues never generates
    provider work at all."""

    def test_state_sweep_skips_issue_links(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("source")
        pr = "https://github.com/acme/widgets/pull/12"
        slot.append("assistant", f"{pr} and https://github.com/acme/widgets/issues/13")

        assert state.source_link_urls() == [pr]
        assert state.source_link_urls_for_slot("source") == [pr]

    @pytest.mark.asyncio
    async def test_slots_get_schedules_only_change_links(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import source_providers as sp

        scheduler = MagicMock(return_value=[])
        monkeypatch.setattr(sp, "schedule_check_refresh", scheduler)
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.owner_id = "U_OWNER"
        slot = state.get_or_create_slot("source")
        pr = "https://github.com/acme/widgets/pull/12"
        slot.append("assistant", f"{pr} and https://github.com/acme/widgets/issues/13")

        app = _make_app(state)

        @web.middleware
        async def owner_auth(request, handler):
            request["user"] = "U_OWNER"
            request["app"] = ""
            return await handler(request)

        app.middlewares.append(owner_auth)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/slots")
            assert resp.status == 200
            payload = await resp.json()

        # Both links are serialized for the sidebar...
        links = next(item for item in payload if item["key"] == "source")["source_links"]
        assert len(links) == 2
        # ...but only the pull request is scheduled for a status read.
        assert scheduler.call_args.args[0] == [pr]


class TestBulkModelSwitch:
    """POST /api/chat/slots/model — switch every live slot to one model."""

    @staticmethod
    def _app(state: DashboardState) -> web.Application:
        from kiro_crew.dashboard.chat import api_chat_slots_model

        # Mirror production: token_auth middleware sets request["app"] on
        # every authenticated path ("" = dashboard user). Tests that model an
        # app-token caller insert their own middleware at index 0, which runs
        # first; this default only fills in the marker when absent.
        @web.middleware
        async def dashboard_auth_marker(request, handler):
            if "app" not in request:
                request["app"] = ""
            return await handler(request)

        app = web.Application(middlewares=[dashboard_auth_marker])
        app["state"] = state
        app.router.add_post("/api/chat/slots/model", api_chat_slots_model)
        return app

    @staticmethod
    def _mark_running(slot: _ChatSlot) -> None:
        """Give the slot a live task so the ``running`` property is True."""
        task = MagicMock()
        task.done.return_value = False
        slot.task = task

    @pytest.mark.asyncio
    async def test_switches_all_differing_slots_and_resets_each(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("a", model="claude-opus-4.6")
        state.get_or_create_slot("b", model="claude-sonnet-4.6")
        state.push_slots_update = MagicMock()  # mock after creation: get_or_create_slot pushes

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": "claude-opus-4.8"})
            data = await resp.json()

        assert resp.status == 200
        assert sorted(data["switched"]) == ["a", "b"]
        assert data["skipped_running"] == []
        assert state._slots["a"].model == "claude-opus-4.8"
        assert state._slots["b"].model == "claude-opus-4.8"
        assert state.sessions.reset.await_count == 2
        state.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_running_slot_by_default(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.push_slots_update = MagicMock()
        idle = state.get_or_create_slot("idle", model="claude-opus-4.6")
        busy = state.get_or_create_slot("busy", model="claude-opus-4.6")
        self._mark_running(busy)

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": "claude-opus-4.8"})
            data = await resp.json()

        assert resp.status == 200
        assert data["switched"] == ["idle"]
        assert data["skipped_running"] == ["busy"]
        assert idle.model == "claude-opus-4.8"
        assert busy.model == "claude-opus-4.6"  # untouched mid-turn
        assert state.sessions.reset.await_count == 1

    @pytest.mark.asyncio
    async def test_skip_running_false_forces_running_slot(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.push_slots_update = MagicMock()
        busy = state.get_or_create_slot("busy", model="claude-opus-4.6")
        self._mark_running(busy)

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/model",
                json={"model": "claude-opus-4.8", "skip_running": False},
            )
            data = await resp.json()

        assert resp.status == 200
        assert data["switched"] == ["busy"]
        assert data["skipped_running"] == []
        assert busy.model == "claude-opus-4.8"
        assert state.sessions.reset.await_count == 1

    @pytest.mark.asyncio
    async def test_already_on_target_is_unchanged_not_reset(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()  # mock after creation: get_or_create_slot pushes

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": "claude-opus-4.8"})
            data = await resp.json()

        assert resp.status == 200
        assert data["unchanged"] == ["a"]
        assert data["switched"] == []
        state.sessions.reset.assert_not_awaited()
        state.push_slots_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_reset_failure_is_isolated_and_reported(self, tmp_path):
        state = _make_state(tmp_path)
        # First reset (slot "a") succeeds, second ("b") raises. The failure must
        # be isolated: "a" still switches, "b" lands in `failed` with its old
        # model intact, and partial progress is still broadcast.
        state.sessions.reset = AsyncMock(side_effect=[None, RuntimeError("boom")])
        state.get_or_create_slot("a", model="claude-opus-4.6")
        state.get_or_create_slot("b", model="claude-opus-4.6")
        state.push_slots_update = MagicMock()  # mock after creation: get_or_create_slot pushes

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": "claude-opus-4.8"})
            data = await resp.json()

        assert resp.status == 200
        assert data["switched"] == ["a"]
        assert data["failed"] == ["b"]
        assert state._slots["a"].model == "claude-opus-4.8"
        assert state._slots["b"].model == "claude-opus-4.6"  # untouched on reset failure
        state.push_slots_update.assert_called_once()  # partial progress still broadcast

    @pytest.mark.asyncio
    async def test_app_caller_only_switches_own_slots(self, tmp_path):
        """App Kit ownership isolation: an app token can only bulk-switch its
        own slots -- other apps' and the dashboard user's slots are untouched
        (mirrors api_chat_slots_cleanup)."""
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("mine", model="claude-opus-4.6", app="app-A")
        state.get_or_create_slot("theirs", model="claude-opus-4.6", app="app-B")
        state.get_or_create_slot("dashboard", model="claude-opus-4.6")
        state.push_slots_update = MagicMock()  # mock after creation: get_or_create_slot pushes

        @web.middleware
        async def inject_app(request, handler):
            request["app"] = "app-A"
            return await handler(request)

        app_obj = self._app(state)
        app_obj.middlewares.insert(0, inject_app)

        async with TestClient(TestServer(app_obj)) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": "claude-opus-4.8"})
            data = await resp.json()

        assert resp.status == 200
        assert data["switched"] == ["mine"]
        assert state._slots["mine"].model == "claude-opus-4.8"
        # Non-owned slots: not switched, not reset, not even reported.
        assert state._slots["theirs"].model == "claude-opus-4.6"
        assert state._slots["dashboard"].model == "claude-opus-4.6"
        assert "theirs" not in data["unchanged"] + data["skipped_running"] + data["failed"]
        assert state.sessions.reset.await_count == 1

    @pytest.mark.asyncio
    async def test_missing_auth_marker_is_denied(self, tmp_path):
        """Deny-by-default: if the auth middleware never ran (request["app"]
        absent), the endpoint refuses instead of granting all-slot access."""
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("a", model="claude-opus-4.6")

        from kiro_crew.dashboard.chat import api_chat_slots_model

        bare_app = web.Application()  # deliberately NO auth-marker middleware
        bare_app["state"] = state
        bare_app.router.add_post("/api/chat/slots/model", api_chat_slots_model)

        async with TestClient(TestServer(bare_app)) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 403
        assert state._slots["a"].model == "claude-opus-4.6"
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falsy_non_string_marker_fails_closed(self, tmp_path):
        """A buggy middleware setting request["app"] to a falsy non-string
        (None) must NOT be treated as a dashboard user -- the ownership check
        applies and no slot matches, so nothing is switched."""
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("a", model="claude-opus-4.6")

        from kiro_crew.dashboard.chat import api_chat_slots_model

        @web.middleware
        async def buggy_auth_marker(request, handler):
            request["app"] = None  # falsy, but NOT the explicit "" dashboard marker
            return await handler(request)

        app_obj = web.Application(middlewares=[buggy_auth_marker])
        app_obj["state"] = state
        app_obj.router.add_post("/api/chat/slots/model", api_chat_slots_model)

        async with TestClient(TestServer(app_obj)) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": "claude-opus-4.8"})
            data = await resp.json()

        assert resp.status == 200
        assert data["switched"] == []
        assert state._slots["a"].model == "claude-opus-4.6"
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_boolean_skip_running_is_rejected(self, tmp_path):
        state = _make_state(tmp_path)
        state.get_or_create_slot("a", model="claude-opus-4.6")

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/model",
                json={"model": "claude-opus-4.8", "skip_running": "yes"},
            )

        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_canonical_key_rejected_no_slot_touched(self, tmp_path):
        # A canonical registry key (the /api/models cold-start fallback trap)
        # is rejected 400 for the acp provider; no slot is switched or reset.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("a", model="claude-fable-5")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": "fable-5-1m"})
            data = await resp.json()

        assert resp.status == 400
        assert "fable-5-1m" in data["error"]
        assert state._slots["a"].model == "claude-fable-5"
        state.sessions.reset.assert_not_awaited()


class TestSlotModelGuard:
    """POST /api/chat/slots/{slot}/model — reject canonical registry keys the
    ACP CLI cannot accept (the /api/models cold-start fallback -32603 trap)."""

    @staticmethod
    def _app(state: DashboardState) -> web.Application:
        from kiro_crew.dashboard.chat import api_chat_slot_model

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/model", api_chat_slot_model)
        return app

    @pytest.mark.asyncio
    async def test_canonical_key_rejected_slot_unchanged(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("a", model="claude-fable-5")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "fable-5-1m"})
            data = await resp.json()

        assert resp.status == 400
        assert "fable-5-1m" in data["error"]
        # The slot keeps its valid model and the session is NOT reset.
        assert state._slots["a"].model == "claude-fable-5"
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_string_auto_default_is_allowed(self, tmp_path):
        # The frontend maps 'auto' -> '' before calling; '' is the provider
        # default and always passes.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("a", model="claude-fable-5")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": ""})

        assert resp.status == 200
        assert state._slots["a"].model == ""
        state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_auto_literal_is_allowed(self, tmp_path):
        # A literal 'auto' (registry key, but the safe sentinel) always passes.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("a", model="claude-fable-5")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "auto"})

        assert resp.status == 200
        assert state._slots["a"].model == "auto"

    @pytest.mark.asyncio
    async def test_valid_kiro_alias_is_allowed(self, tmp_path):
        # kiro/acp ids (registry aliases, not top-level keys) pass through.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("a", model="claude-fable-5")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        assert state._slots["a"].model == "claude-opus-4.8"
        state.sessions.reset.assert_awaited_once()


class TestSlotModelLiveSwitch:
    """POST /api/chat/slots/{slot}/model — prefer an in-place session/set_model
    over tearing the session down.

    A reset costs a full process-tree teardown now plus a cold start and
    transcript replay on the next message. kiro-cli's ``session/set_model``
    switches a live session instead (verified against 2.15.1: acked
    synchronously, conversation carried across the switch, sticky over later
    turns), so the reset is only a fallback.
    """

    @staticmethod
    def _app(state: DashboardState) -> web.Application:
        from kiro_crew.dashboard.chat import api_chat_slot_model

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/model", api_chat_slot_model)
        return app

    @staticmethod
    def _provider(
        *,
        claude: bool = False,
        active_turn: bool = False,
        models=("auto",),
        supports_effort: bool = False,
        change_effort: bool = True,
    ):
        """A live AcpProvider double. ``spec=`` keeps isinstance() working.

        Carries the real capability record for the backend it stands in for:
        ``capabilities_of`` requires a genuine ``SessionCapabilities``, so an
        attribute-shaped flag on a spec'd mock would claim every capability at once.
        """
        from kiro_crew.providers.acp import AcpProvider

        provider = MagicMock(spec=AcpProvider)
        provider.capabilities = capabilities_for(ACP_BACKEND_CLAUDE if claude else ACP_BACKEND_KIRO)
        provider.has_active_turn.return_value = active_turn
        provider.available_models.return_value = [{"modelId": m} for m in models]
        provider.supports_effort.return_value = supports_effort
        provider.change_effort = AsyncMock(return_value=change_effort)
        provider.clear_effort = AsyncMock(return_value=False)
        provider.client = MagicMock()
        provider.client.set_model = AsyncMock()
        return provider

    @pytest.mark.asyncio
    async def test_idle_switch_goes_live_without_reset(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        assert state._slots["a"].model == "gpt-5.6-sol"
        provider.client.set_model.assert_awaited_once_with("gpt-5.6-sol")
        # The whole point: the session survives.
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_canonical_key_is_translated_to_the_acp_id(self, tmp_path):
        # slot.model is a canonical/wire value; set_model only accepts kiro ids.
        from kiro_crew import model_registry

        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.6")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        sent = provider.client.set_model.await_args.args[0]
        assert sent == model_registry.to_acp_id("claude-opus-4.8")
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_active_turn_answers_409_without_reset(self, tmp_path):
        # Awaiting a response mid-turn would race the streaming prompt loop on
        # stdout for the non-multiplexed client (same hazard the effort handler
        # documents), and the old reset fallback tore down the in-flight turn
        # mid-stream for any programmatic caller. A turn in flight now answers
        # busy: no live switch, no reset, slot model untouched.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(active_turn=True)
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})
            data = await resp.json()

        assert resp.status == 409
        assert data["code"] == "turn_in_flight"
        provider.client.set_model.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()
        assert state._slots["a"].model == "claude-opus-4.8"

    @pytest.mark.asyncio
    async def test_live_switch_failure_falls_back_to_reset(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        provider.client.set_model = AsyncMock(side_effect=RuntimeError("boom"))
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        # The slot still lands on the new model, via the reset path.
        assert resp.status == 200
        assert state._slots["a"].model == "gpt-5.6-sol"
        state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_auto_uses_the_advertised_auto_id(self, tmp_path):
        # Back-to-default is the most common click; kiro expresses it as a real
        # model id, so it should not cost a teardown either.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(models=("auto", "claude-opus-4.8"))
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": ""})

        assert resp.status == 200
        assert state._slots["a"].model == ""
        provider.client.set_model.assert_awaited_once_with("auto")
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_without_advertised_auto_falls_back_to_reset(self, tmp_path):
        # Never invent an id the backend did not advertise.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(models=("claude-opus-4.8",))
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": ""})

        assert resp.status == 200
        provider.client.set_model.assert_not_awaited()
        state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_claude_backend_default_falls_back_to_reset(self, tmp_path):
        # The claude backend has no "let the server choose" id.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(claude=True)
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": ""})

        assert resp.status == 200
        provider.client.set_model.assert_not_awaited()
        state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_claude_backend_switch_uses_provider_id(self, tmp_path):
        from kiro_crew import model_registry

        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(claude=True)
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.6")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        sent = provider.client.set_model.await_args.args[0]
        assert sent == model_registry.to_provider_id("claude-opus-4.8", "claude_code")
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_live_session_still_routes_through_reset(self, tmp_path):
        # No session to update: the reset is an O(1) no-op teardown, but it also
        # carries _reset_slot_session's pending-wait cleanup, so keep taking it.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.sessions.get_provider = MagicMock(return_value=None)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        assert state._slots["a"].model == "gpt-5.6-sol"
        state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unchanged_model_touches_nothing(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="gpt-5.6-sol")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        provider.client.set_model.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()

    # ── reasoning effort ─────────────────────────────────────────────
    # The kiro effort overlay is written at (re)spawn, so a cold start applies
    # the slot's level for free. An in-place switch never respawns, so it must
    # push the level itself or the new model silently runs at its own default
    # while the UI still reports the slot's level.

    @pytest.mark.asyncio
    async def test_persisted_effort_is_pushed_to_the_new_model(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(supports_effort=True)
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="claude-haiku-4.5")
        slot.reasoning_effort = "high"
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        provider.change_effort.assert_awaited_once_with("high")
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_effort_push_rejected_falls_back_to_reset(self, tmp_path):
        # change_effort returning False means the level never reached the
        # session — reset so the cold start applies it via the overlay.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(supports_effort=True, change_effort=False)
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="claude-haiku-4.5")
        slot.reasoning_effort = "high"
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_effort_push_raising_falls_back_to_reset(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(supports_effort=True)
        provider.change_effort = AsyncMock(side_effect=RuntimeError("rejected"))
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="claude-haiku-4.5")
        slot.reasoning_effort = "high"
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_effort_incapable_target_keeps_the_live_switch(self, tmp_path):
        # Switching to a model with no effort selector is a persisted no-op for
        # effort — it must not cost a teardown.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(supports_effort=False)
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="claude-opus-4.8")
        slot.reasoning_effort = "high"
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-haiku-4.5"})

        assert resp.status == 200
        provider.change_effort.assert_not_awaited()
        # The level stays persisted for a later switch back to a capable model.
        assert state._slots["a"].reasoning_effort == "high"
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_effort_override_re_resolves_without_resetting(self, tmp_path):
        # With no slot override, clear_effort re-resolves any workspace default.
        # Its False return is benign here (nothing to push, nothing stale for a
        # model the user never set a level on), so it must not force a reset.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(supports_effort=True)
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="claude-haiku-4.5")
        slot.reasoning_effort = ""
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        provider.clear_effort.assert_awaited_once()
        provider.change_effort.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unavailable_model_is_4xx_and_keeps_the_session(self, tmp_path):
        """An entitlement refusal must NOT take the reset fallback.

        The generic ``except Exception`` here treats
        every set_model failure as "the call didn't land" and recovers with a
        reset. For a model the account cannot use that recovery is wrong twice
        over — it destroys the live conversation AND cold-starts on a different
        model, while the handler still answers ok:True. The slot must also keep
        its previous model, or the picker asserts a model that was refused.
        """
        from kiro_crew.acp.client import AcpModelUnavailable

        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        provider.client.set_model = AsyncMock(
            side_effect=AcpModelUnavailable(
                "claude-opus-4.8", ["gpt-5.6-sol"], backend=ACP_BACKEND_KIRO
            )
        )
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="gpt-5.6-sol")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})
            body = await resp.json()

        assert resp.status == 400
        assert "not available on your account" in body["error"]
        # Names what the account CAN use.
        assert "gpt-5.6-sol" in body["error"]
        # The conversation survives — no reset fallback.
        state.sessions.reset.assert_not_awaited()
        # And the slot still reports the model that is actually running.
        assert state._slots["a"].model == "gpt-5.6-sol"

    @pytest.mark.asyncio
    async def test_refused_pick_rolls_back_the_pick_generation(self, tmp_path):
        """REGRESSION (review finding): a refused pick changed nothing, so the
        explicit-pick generation must roll back with the model. Leaving the
        bump in place would make the model-fallback restore probe read the
        FAILED attempt as an explicit choice and silently abandon restoring
        the primary — the session would stay on the fallback forever with no
        card and no probe."""
        from kiro_crew.acp.client import AcpModelUnavailable

        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        provider.client.set_model = AsyncMock(
            side_effect=AcpModelUnavailable(
                "claude-opus-4.8", ["gpt-5.6-sol"], backend=ACP_BACKEND_KIRO
            )
        )
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="gpt-5.6-sol")
        state.push_slots_update = MagicMock()
        gen_before = slot._model_pick_gen

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 400
        assert slot._model_pick_gen == gen_before

    @pytest.mark.asyncio
    async def test_refused_pick_rollback_spares_a_newer_concurrent_pick(self, tmp_path):
        """REGRESSION (review finding on eb3cf067): two picks arriving together
        must not roll back each other's state. The pick transaction is
        serialised per slot (_model_pick_lock), so the refused pick unwinds
        cleanly before the next pick's snapshot is taken — the surviving state
        is the later, successful pick's."""
        import asyncio

        from kiro_crew.acp.client import AcpModelUnavailable

        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        slot = state.get_or_create_slot("a", model="gpt-5.6-sol")
        state.push_slots_update = MagicMock()

        release_slow = asyncio.Event()

        async def _set_model(target):
            if target == "claude-opus-4.8":
                # The SLOW, ultimately-refused pick: park inside the locked
                # transaction, then refuse.
                await release_slow.wait()
                raise AcpModelUnavailable(
                    "claude-opus-4.8", ["gpt-5.6-sol"], backend=ACP_BACKEND_KIRO
                )
            return None

        provider.client.set_model = AsyncMock(side_effect=_set_model)
        state.sessions.get_provider = MagicMock(return_value=provider)
        gen_before = slot._model_pick_gen

        async with TestClient(TestServer(self._app(state))) as client:
            slow = asyncio.create_task(
                client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})
            )
            await asyncio.sleep(0.05)  # slow pick holds the lock, parked at its await
            fast = asyncio.create_task(
                client.post("/api/chat/slots/a/model", json={"model": "deepseek-3.2"})
            )
            await asyncio.sleep(0.05)  # fast pick queued on the lock
            release_slow.set()
            slow_resp = await slow
            fast_resp = await fast

        assert slow_resp.status == 400
        assert fast_resp.status == 200
        # The successful pick's state survives: the refused pick's rollback
        # unwound only its own speculative writes.
        assert slot.model == "deepseek-3.2"
        assert slot._model_pick_gen == gen_before + 1

    @pytest.mark.asyncio
    async def test_two_refused_picks_leave_the_slot_untouched(self, tmp_path):
        """Two UNAVAILABLE picks overlapping must not fail out of order: without
        serialisation the later rollback would restore
        the earlier pick's already-refused model, leaving the slot advertising
        a rejected model with a phantom pick generation. Serialised, each
        transaction unwinds itself and the slot ends exactly where it began."""
        import asyncio

        from kiro_crew.acp.client import AcpModelUnavailable

        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        slot = state.get_or_create_slot("a", model="gpt-5.6-sol")
        state.push_slots_update = MagicMock()

        release_first = asyncio.Event()
        release_second = asyncio.Event()

        async def _set_model(target):
            # Park each pick independently so the FIRST refusal can complete
            # while the SECOND is still pending — the out-of-order shape that
            # made the pre-lock CAS restore an already-refused model.
            if target == "claude-opus-4.8":
                await release_first.wait()
            else:
                await release_second.wait()
            raise AcpModelUnavailable(target, ["gpt-5.6-sol"], backend=ACP_BACKEND_KIRO)

        provider.client.set_model = AsyncMock(side_effect=_set_model)
        state.sessions.get_provider = MagicMock(return_value=provider)
        gen_before = slot._model_pick_gen

        async with TestClient(TestServer(self._app(state))) as client:
            first = asyncio.create_task(
                client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})
            )
            await asyncio.sleep(0.05)
            second = asyncio.create_task(
                client.post("/api/chat/slots/a/model", json={"model": "deepseek-3.2"})
            )
            await asyncio.sleep(0.05)
            # First refusal completes while the second pick is still pending.
            release_first.set()
            first_resp = await first
            release_second.set()
            second_resp = await second

        assert first_resp.status == 400
        assert second_resp.status == 400
        # No phantom state: the slot still reports what is actually running.
        assert slot.model == "gpt-5.6-sol"
        assert slot._model_pick_gen == gen_before

    @pytest.mark.asyncio
    async def test_successful_pick_bumps_the_pick_generation(self, tmp_path):
        """The counterpart pin: a pick that LANDS must keep its bump — that is
        what stops the fallback restore probe from overriding the choice."""
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="gpt-5.6-sol")
        state.push_slots_update = MagicMock()
        gen_before = slot._model_pick_gen

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        assert slot._model_pick_gen == gen_before + 1

    @pytest.mark.asyncio
    async def test_same_value_pick_still_bumps_the_pick_generation(self, tmp_path):
        """REGRESSION (review finding): picking the model the slot already
        reports takes the equality early-return, but it is still an EXPLICIT
        affirmation — without the bump, a user who deliberately re-picks the
        very model the session fell back to would have that choice silently
        overridden by the next fallback restore probe."""
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="gpt-5.6-sol")
        state.push_slots_update = MagicMock()
        gen_before = slot._model_pick_gen

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        assert slot._model_pick_gen == gen_before + 1
        # Same-value: no switch, no reset.
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_same_value_pick_during_fallback_takes_the_live_switch_path(self, tmp_path):
        """REGRESSION (review finding on 1a61ddcf): with a fallback actively
        serving the session, a pick of the DISPLAYED primary equals slot.model
        but the wire model is the fallback — the equality early-return would
        record the pick and switch nothing, stranding the session on the
        fallback while usage is attributed to the primary. The live-switch
        path must run instead."""
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="gpt-5.6-sol")
        slot._active_fallback_model = "fallback-model"
        state.push_slots_update = MagicMock()
        gen_before = slot._model_pick_gen

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        # The pick is recorded AND the switch actually went to the wire.
        assert slot._model_pick_gen == gen_before + 1
        provider.client.set_model.assert_awaited_once_with("gpt-5.6-sol")


class TestSlotModelSwitchContextBroadcast:
    """POST /api/chat/slots/{slot}/model — one ``context_usage`` event per
    switch so the meter updates immediately.

    Regression: nothing broadcast on a model switch, so the frontend's stored
    ``slotContextTokens`` kept the OLD model's {used, window} until the next
    turn. The event carries ``reset: true`` so the reducer may replace or
    delete the stored entry (per-turn events never delete).
    """

    _app = staticmethod(TestSlotModelLiveSwitch._app)
    _provider = staticmethod(TestSlotModelLiveSwitch._provider)

    @staticmethod
    def _find_context_events(broadcast_mock):
        return [
            call.args[1]
            for call in broadcast_mock.call_args_list
            if call.args and call.args[0] == "context_usage"
        ]

    @pytest.mark.asyncio
    async def test_live_switch_broadcasts_rebased_stats(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.broadcast_ws = MagicMock()
        provider = self._provider()
        # The provider accessors report the freshly rebased stats set_model left.
        provider.context_usage_pct.return_value = 36.8
        provider.context_used_tokens.return_value = 100_000
        provider.context_window_tokens.return_value = 272_000
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        events = self._find_context_events(state.broadcast_ws)
        assert events == [
            {
                "slot": state._slots["a"].key,
                "pct": 36.8,
                "used_tokens": 100_000,
                "window_tokens": 272_000,
                "reset": True,
            }
        ]

    @pytest.mark.asyncio
    async def test_live_switch_to_auto_refreshes_the_served_model_cache(self, tmp_path):
        """REGRESSION (GPT finding on 33ef1122e): ``slot.served_model`` was
        written only at spawn, so selecting Auto on a warm, preserved session
        left the concrete id the session was spawned with in the cache and the
        composer chip kept naming the OLD model as "the default". After the live
        switch lands the cache follows the provider -- ``""`` here, because
        AcpProvider collapses the ``auto`` sentinel (chip reads "auto")."""
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(models=("auto", "claude-opus-4.8"))
        provider.served_model = ""
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="claude-opus-4.8")
        slot.record_served_model("claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "auto"})

        assert resp.status == 200
        provider.client.set_model.assert_awaited_once_with("auto")
        state.sessions.reset.assert_not_awaited()
        assert slot.served_model == ""

    @pytest.mark.asyncio
    async def test_live_switch_to_a_pin_refreshes_the_served_model_cache(self, tmp_path):
        # Pin -> pin: the cache names the model the live session runs NOW.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(models=("auto", "claude-opus-4.8", "gpt-5.6-sol"))
        provider.served_model = "gpt-5.6-sol"
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="claude-opus-4.8")
        slot.record_served_model("claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        provider.client.set_model.assert_awaited_once_with("gpt-5.6-sol")
        assert slot.served_model == "gpt-5.6-sol"

    @pytest.mark.asyncio
    async def test_reset_fallback_broadcasts_token_clearing_event(self, tmp_path):
        # No live provider -> the reset path: no token counts, so the frontend
        # deletes its stored entry and falls back to the model-derived window.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.broadcast_ws = MagicMock()
        state.sessions.get_provider = MagicMock(return_value=None)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        state.sessions.reset.assert_awaited_once()
        events = self._find_context_events(state.broadcast_ws)
        assert events == [{"slot": state._slots["a"].key, "pct": 0.0, "reset": True}]

    @pytest.mark.asyncio
    async def test_broadcast_failure_does_not_fail_the_switch(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.broadcast_ws = MagicMock(side_effect=RuntimeError("ws down"))
        state.sessions.get_provider = MagicMock(return_value=None)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        assert state._slots["a"].model == "gpt-5.6-sol"


def _make_tail_fork_slot(state):
    slot = state.get_or_create_slot("src")
    slot.title = "My Chat"
    slot._titled = True
    slot.append("user", "msg1", "msg msg-u")
    slot.append("assistant", "reply1", "msg msg-a")
    slot.append("user", "msg2", "msg msg-u")
    slot.append("assistant", "reply2", "msg msg-a")
    slot.drain()
    return slot


class TestForkSlotTail:
    """Tests for tail-only fork (direction="tail") on POST /api/chat/slots/{slot}/fork."""

    @pytest.fixture(autouse=True)
    def _tail_fork_enabled(self, monkeypatch):
        """Default tail_fork_enabled=True so these tests exercise the tail path;
        the B1 gate test below overrides this to False for its own assertion."""
        mock_cfg = MagicMock()
        mock_cfg.dashboard.tail_fork_enabled = True
        monkeypatch.setattr("kiro_crew.dashboard.chat_fork.KiroCrewConfig.load", lambda: mock_cfg)

    @pytest.mark.asyncio
    async def test_tail_fork_keeps_messages_after_message_id(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _make_tail_fork_slot(state)
        target_id = slot.messages[1]["meta"]["mid"]

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/fork",
                json={"at_message_id": target_id, "direction": "tail"},
            )

            assert resp.status == 200
            data = await resp.json()
            assert data["messages"] == 2
            assert data["direction"] == "tail"

        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert [m["content"] for m in visible] == ["msg2", "reply2"]

    @pytest.mark.asyncio
    async def test_tail_fork_keeps_messages_after_index(self, tmp_path):
        state = _make_state(tmp_path)
        _make_tail_fork_slot(state)

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/fork",
                json={"at_message_index": 1, "direction": "tail"},
            )

            assert resp.status == 200
            data = await resp.json()
            assert data["messages"] == 2
            assert data["direction"] == "tail"

        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert len(visible) == 2
        assert visible[0]["content"] == "msg2"
        assert visible[-1]["content"] == "reply2"

    @pytest.mark.asyncio
    async def test_tail_fork_discard_has_no_summary(self, tmp_path):
        state = _make_state(tmp_path)
        _make_tail_fork_slot(state)

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/fork",
                json={"at_message_index": 1, "direction": "tail"},
            )

            assert resp.status == 200
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert all((m.get("meta") or {}).get("source") != "head_summary" for m in visible)

    @pytest.mark.asyncio
    async def test_tail_fork_requires_at_index(self, tmp_path):
        state = _make_state(tmp_path)
        _make_tail_fork_slot(state)

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={"direction": "tail"})

            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_tail_fork_nothing_after_last_message(self, tmp_path):
        state = _make_state(tmp_path)
        _make_tail_fork_slot(state)

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/fork",
                json={"at_message_index": 3, "direction": "tail"},
            )

            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_tail_fork_title_and_provenance(self, tmp_path):
        state = _make_state(tmp_path)
        _make_tail_fork_slot(state)

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/fork",
                json={"at_message_index": 1, "direction": "tail"},
            )

            assert resp.status == 200
            data = await resp.json()
            assert data["title"] == "↳ Tail of My Chat"

        new_slot = state._slots.get(data["key"])
        assert new_slot.forked_from == "dashboard:src"

    @pytest.mark.asyncio
    async def test_default_direction_is_head(self, tmp_path):
        state = _make_state(tmp_path)
        _make_tail_fork_slot(state)

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={"at_message_index": 1})

            assert resp.status == 200
            data = await resp.json()
            assert data["direction"] == "head"
            assert data["messages"] == 2

        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert visible[-1]["content"] == "reply1"

    @pytest.mark.asyncio
    async def test_invalid_direction_rejected(self, tmp_path):
        state = _make_state(tmp_path)
        _make_tail_fork_slot(state)

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={"direction": "sideways"})

            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_tail_fork_gated_off_falls_back_to_head(self, tmp_path, monkeypatch):
        """B1 gate (D1): tail_fork_enabled=False silently downgrades tail -> head."""
        mock_cfg = MagicMock()
        mock_cfg.dashboard.tail_fork_enabled = False
        monkeypatch.setattr("kiro_crew.dashboard.chat_fork.KiroCrewConfig.load", lambda: mock_cfg)
        state = _make_state(tmp_path)
        _make_tail_fork_slot(state)

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/fork",
                json={"at_message_index": 1, "direction": "tail"},
            )

            assert resp.status == 200
            data = await resp.json()
            assert data["direction"] == "head"
            assert data["messages"] == 2

        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert visible[-1]["content"] == "reply1"


# ── Session reload endpoint (POST /api/chat/slots/{slot}/reload) ──


class TestSessionReload:
    """api_chat_slot_reload — relaunch the slot's agent process in place.

    The endpoint's contract: refuse while a turn is in flight (409), otherwise
    tear down via reset(skip_if_busy=True), append the session_reload feed
    notice, and eagerly re-arm the resume spawn.
    """

    @staticmethod
    def _idle_provider():
        provider = MagicMock()
        provider.has_active_turn = MagicMock(return_value=False)
        return provider

    @pytest.mark.asyncio
    async def test_reload_unknown_slot_404(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/nope/reload")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_reload_resets_notices_and_respawns(self, tmp_path, monkeypatch):
        """Happy path: atomic reset, feed notice tagged session_reload, eager resume."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        state.sessions.get_provider = MagicMock(return_value=self._idle_provider())
        state.sessions.reset = AsyncMock(return_value=True)
        eager = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", eager)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")
            data = await resp.json()

        assert resp.status == 200
        assert data == {"ok": True}
        state.sessions.reset.assert_awaited_once_with("dashboard:s1", skip_if_busy=True)
        # The feed carries the durable confirmation, tagged so the follow-up
        # options / continuable scans skip it (isSystemNoticeKind frontend twin).
        notice = slot.messages[-1]
        assert notice["role"] == "assistant"
        assert notice["meta"]["kind"] == "session_reload"
        assert "Reloading session" in notice["content"]
        eager.assert_called_once()
        assert eager.call_args.kwargs.get("allow_resume") is True

    @pytest.mark.asyncio
    async def test_reload_refused_while_turn_in_flight(self, tmp_path, monkeypatch):
        """409 with no teardown, no notice, no eager spawn while a turn runs."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        busy = MagicMock()
        busy.has_active_turn = MagicMock(return_value=True)
        state.sessions.get_provider = MagicMock(return_value=busy)
        state.sessions.reset = AsyncMock()
        eager = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", eager)
        before = len(slot.messages)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")

        assert resp.status == 409
        state.sessions.reset.assert_not_awaited()
        assert len(slot.messages) == before
        eager.assert_not_called()

    @pytest.mark.asyncio
    async def test_reload_race_turn_started_after_fast_path(self, tmp_path, monkeypatch):
        """A turn slipping in between the fast path and the atomic guard is a 409.

        reset(skip_if_busy=True) returning False is ambiguous (no session OR
        busy); the re-check of has_active_turn is what disambiguates. Nothing
        may be appended or respawned for the busy case.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        idle = self._idle_provider()
        busy = MagicMock()
        busy.has_active_turn = MagicMock(return_value=True)
        # First lookup (fast path) sees idle, as does the teardown's
        # pre-reset identity capture; the post-reset re-check sees busy.
        state.sessions.get_provider = MagicMock(side_effect=[idle, idle, busy])
        state.sessions.reset = AsyncMock(return_value=False)
        eager = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", eager)
        before = len(slot.messages)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")

        assert resp.status == 409
        assert len(slot.messages) == before
        eager.assert_not_called()

    @pytest.mark.asyncio
    async def test_reload_retries_when_racing_turn_already_finished(self, tmp_path, monkeypatch):
        """A declined reset whose racing turn already FINISHED retries once.

        Falling through would report success while the stale live process
        survives untouched -- the silent false-success the endpoint exists to
        prevent. The retry succeeds here; a second decline is the 409 case
        (covered below).
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        state.sessions.get_provider = MagicMock(return_value=self._idle_provider())
        state.sessions.reset = AsyncMock(side_effect=[False, True])
        eager = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", eager)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")
            data = await resp.json()

        assert resp.status == 200
        assert data == {"ok": True}
        assert state.sessions.reset.await_count == 2
        assert slot.messages[-1]["meta"]["kind"] == "session_reload"
        eager.assert_called_once()

    @pytest.mark.asyncio
    async def test_reload_double_decline_is_turn_in_flight(self, tmp_path, monkeypatch):
        """Two consecutive declines with a live session mean a genuinely racing
        turn: 409, no notice, no respawn -- never a false success."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        state.sessions.get_provider = MagicMock(return_value=self._idle_provider())
        state.sessions.reset = AsyncMock(return_value=False)
        eager = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", eager)
        before = len(slot.messages)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")
            data = await resp.json()

        assert resp.status == 409
        assert data["code"] == "turn_in_flight"
        assert state.sessions.reset.await_count == 2
        assert len(slot.messages) == before
        eager.assert_not_called()

    def test_system_notice_kinds_backend_frontend_parity(self):
        """The two SYSTEM_NOTICE_KINDS twins must hold the same set.

        A kind skipped on one side but not the other leaves the sidebar showing
        notice boilerplate while the chat pane shows the real turn (or vice
        versa) -- the exact divergence the shared predicates exist to prevent.
        Asserted on the TS source because nothing else couples the two files.
        """
        import re
        from pathlib import Path

        from kiro_crew.dashboard.system_notices import SYSTEM_NOTICE_KINDS

        ts_path = Path(__file__).resolve().parents[1] / "website/src/lib/systemNotice.ts"
        ts_src = ts_path.read_text(encoding="utf-8")
        m = re.search(r"new Set\(\[([^\]]*)\]\)", ts_src)
        assert m, "SYSTEM_NOTICE_KINDS Set literal not found in systemNotice.ts"
        frontend = set(re.findall(r"'([^']+)'", m.group(1)))
        assert frontend == set(SYSTEM_NOTICE_KINDS)

    @pytest.mark.asyncio
    async def test_reload_without_live_session_is_ok(self, tmp_path, monkeypatch):
        """No live session: reload is a no-op teardown but still ok + notice + respawn.

        The goal state (next spawn picks up fresh config) already holds, so
        this must not be an error; reloaded=False tells the caller no process
        was actually torn down.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        state.sessions.get_provider = MagicMock(return_value=None)
        state.sessions.reset = AsyncMock(return_value=False)
        eager = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", eager)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")
            data = await resp.json()

        assert resp.status == 200
        assert data == {"ok": True}
        assert slot.messages[-1]["meta"]["kind"] == "session_reload"
        eager.assert_called_once()

    @pytest.mark.asyncio
    async def test_reload_cancels_pending_question_waits(self, tmp_path, monkeypatch):
        """A pending ask_question wait is released by the reload teardown.

        The card lives in dashboard state, not the session — without the
        unblock it would survive the reset and hold its MCP worker until
        timeout with no agent left to receive the answer.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.sessions.get_provider = MagicMock(return_value=self._idle_provider())
        state.sessions.reset = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.schedule_eager_spawn",
            MagicMock(return_value=None),
        )
        cancelled = MagicMock(return_value=1)
        monkeypatch.setattr(state, "cancel_questions_for_slot", cancelled)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")

        assert resp.status == 200
        cancelled.assert_called_once_with("s1")

    @pytest.mark.asyncio
    async def test_reload_app_token_denied_on_foreign_slot(self, tmp_path, monkeypatch):
        """An app token gets the indistinguishable 404, and nothing is torn down.

        Reload is a teardown; without the cancel-route ownership policy, an app
        allowed /api/chat/* could reset a foreign slot's process and cancel its
        pending waits.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._app = "other-app"
        state.sessions.get_provider = MagicMock(return_value=None)
        state.sessions.reset = AsyncMock()
        app = _make_app_with_agent_routes(state)

        @web.middleware
        async def _as_app(request, handler):
            request["app"] = "attacker-app"
            return await handler(request)

        app.middlewares.append(_as_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/reload")
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reload_targets_the_linked_session_key(self, tmp_path, monkeypatch):
        """A channel-born slot reloads its LINKED session, not the phantom
        dashboard-prefixed key — otherwise reload reports success while the
        live process keeps its stale config.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.linked_session_key = "slack:123.456"
        state.sessions.get_provider = MagicMock(return_value=None)
        state.sessions.reset = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.schedule_eager_spawn",
            MagicMock(return_value=None),
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")

        assert resp.status == 200
        state.sessions.reset.assert_awaited_once_with("slack:123.456", skip_if_busy=True)

    @pytest.mark.asyncio
    async def test_reload_refused_while_subagents_attached(self, tmp_path, monkeypatch):
        """409 while children are running/queued/delivering — the reset would
        tear down the shared subagent runtime and discard their work.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        state.sessions.get_provider = MagicMock(return_value=self._idle_provider())
        state.sessions.reset = AsyncMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=["child-1"])
        state.subagents._queued_depth = MagicMock(return_value=0)
        eager = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", eager)
        before = len(slot.messages)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")
            data = await resp.json()

        assert resp.status == 409
        assert data["code"] == "slot_subagents_running"
        state.sessions.reset.assert_not_awaited()
        assert len(slot.messages) == before
        eager.assert_not_called()

    @pytest.mark.asyncio
    async def test_reload_fails_closed_on_unreadable_children_probe(self, tmp_path, monkeypatch):
        """A None running-probe is the probe FAILING, not zero children."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.sessions.get_provider = MagicMock(return_value=None)
        state.sessions.reset = AsyncMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=None)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")

        assert resp.status == 409
        state.sessions.reset.assert_not_awaited()


class TestCloseBroadcastDurability:
    """A slot-list frame must never omit a slot that is coming back.

    Clients treat a frame omitting a slot as licence to drop that slot's cached
    per-slot state, including follow-up and folder-suggestion cards that only
    ever exist in the browser and are never re-sent. So the close may only
    broadcast once the history save is durable: past that point no rollback can
    put the slot back, and the omission can never be retracted.

    Every test records the ORDER of the close steps, because asserting only that
    a broadcast happened passes against any placement and proves nothing.
    """

    @staticmethod
    def _instrument(state, slot, monkeypatch, frames=None):
        """Record close steps in call order; append each frame's slot keys."""
        from kiro_crew.dashboard import chat_handlers

        calls: list[str] = []

        def _push():
            calls.append("broadcast")
            if frames is not None:
                frames.append(set(state._slots))

        state.push_slots_update = MagicMock(side_effect=_push)
        # A successful close announces the removal through push_slot_removed,
        # which is the same slot-list broadcast for this test's purpose.
        state.push_slot_removed = MagicMock(side_effect=lambda _key: _push())

        real_sync = chat_handlers._sync_dashboard_slots

        def _sync(st):
            calls.append("sync")
            return real_sync(st)

        monkeypatch.setattr(chat_handlers, "_sync_dashboard_slots", _sync)

        real_unblock = chat_handlers._unblock_pending_waits

        def _unblock(st, sl):
            calls.append("unblock_pending_waits")
            return real_unblock(st, sl)

        monkeypatch.setattr(chat_handlers, "_unblock_pending_waits", _unblock)

        async def _save(*args, **kwargs):
            calls.append("save")

        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _save)

        state.sessions.remove = AsyncMock(side_effect=lambda *a, **k: calls.append("remove"))

        eager = MagicMock()
        eager.done = MagicMock(return_value=False)
        eager.cancel = MagicMock(side_effect=lambda: calls.append("cancel_eager"))
        slot._eager_spawn_task = eager

        async def _never() -> None:
            await asyncio.sleep(30)

        task = asyncio.get_running_loop().create_task(_never())
        real_cancel = task.cancel

        def _task_cancel(*args, **kwargs):
            calls.append("cancel_task")
            return real_cancel(*args, **kwargs)

        task.cancel = _task_cancel  # type: ignore[method-assign]
        # `running` is derived from `task`, so assigning the task is enough to
        # reach the cancel-and-wait_for branch.
        slot.task = task

        return calls

    @pytest.mark.asyncio
    async def test_failed_close_never_emits_a_frame_without_the_slot(self, tmp_path, monkeypatch):
        """The regression guard: a rollback must not be preceded by an omission.

        A frame omitting the slot is what makes another client drop this slot's
        follow-up and folder-suggestion cards. Those are browser-only and the
        backend re-offers a folder suggestion at most once per slot, so a prune
        on a close that then rolls back loses them permanently.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import chat_handlers

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.drain()
        frames: list[set] = []
        calls = self._instrument(state, slot, monkeypatch, frames=frames)

        async def _boom(*args, **kwargs):
            calls.append("save")
            raise RuntimeError("disk full")

        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _boom)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/chat/slots/s1")
            assert resp.status == 500

        assert frames, "no frame was broadcast at all, so this proves nothing"
        missing = [i for i, f in enumerate(frames) if "s1" not in f]
        assert not missing, (
            f"frame(s) {missing} omitted a slot that the rollback restores, so a "
            f"client pruned its cards for it: frames={frames} calls={calls}"
        )
        assert state._slots.get("s1") is slot, "slot was not restored"

    @pytest.mark.asyncio
    async def test_broadcast_precedes_the_session_teardown(self, tmp_path, monkeypatch):
        """On success, broadcast after the save but before the slow teardown."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.drain()
        calls = self._instrument(state, slot, monkeypatch)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/chat/slots/s1")
            assert resp.status == 200

        for step in ("save", "remove", "broadcast"):
            assert step in calls, f"{step} never ran, so this proves nothing: {calls}"
        assert calls.index("save") < calls.index(
            "broadcast"
        ), f"broadcast ran before the save was durable: {calls}"
        assert calls.index("broadcast") < calls.index(
            "remove"
        ), f"broadcast waited out the session teardown: {calls}"

    @pytest.mark.asyncio
    async def test_broadcast_beats_a_slow_session_teardown(self, tmp_path, monkeypatch):
        """A session teardown that never returns must not hold the broadcast."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.drain()
        calls = self._instrument(state, slot, monkeypatch)
        seen = asyncio.Event()
        state.push_slots_update = MagicMock(
            side_effect=lambda: (calls.append("broadcast"), seen.set())
        )
        state.push_slot_removed = MagicMock(
            side_effect=lambda _key: (calls.append("broadcast"), seen.set())
        )

        async def _hang(*args, **kwargs):
            calls.append("remove")
            await asyncio.sleep(30)

        state.sessions.remove = AsyncMock(side_effect=_hang)

        async with TestClient(TestServer(_make_app(state))) as client:
            close = asyncio.get_running_loop().create_task(client.delete("/api/chat/slots/s1"))
            await await_until(seen, what="the broadcast firing")
            assert "save" in calls and calls.index("save") < calls.index("broadcast")
            assert "s1" not in state._slots
            close.cancel()
            try:
                await close
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_active_slot_set_is_published_only_after_the_teardown(
        self, tmp_path, monkeypatch
    ):
        """_sync_dashboard_slots must stay after the teardown.

        Publishing the shrunken set earlier lets the idle sweep classify this
        session as orphaned and reap it mid-teardown, and that reap releases a
        shared subagent runtime which outlives the turn — the turn semaphore, the
        sweep's only busy guard, is free while subagents run.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.drain()
        calls = self._instrument(state, slot, monkeypatch)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/chat/slots/s1")
            assert resp.status == 200

        assert "sync" in calls, f"the trailing sync never ran: {calls}"
        assert calls.index("save") < calls.index(
            "sync"
        ), f"active-slot set published before the save was durable: {calls}"
        assert calls.index("remove") < calls.index(
            "sync"
        ), f"active-slot set published before the session teardown: {calls}"


class TestUnflushedTailOrderingAndSnapshot:
    """The owed tail must land in window order, from a snapshot taken on the loop."""

    @staticmethod
    def _row(role: str, body: str, mid: str, ts: str) -> dict:
        return {
            "role": role,
            "content": body,
            "cls": f"msg msg-{role[0]}",
            "ts": ts,
            "meta": {"mid": mid},
        }

    def test_owed_row_merges_at_its_window_position_not_after_the_disk_slice(self):
        """A persisted row LATER in window order must not be rendered before an owed one.

        ``_flush_segment`` pulls a ``stop_event`` out of the trailing chunk run and
        re-appends it AFTER the finalized assistant row (chat_runner.py:2686-2687).
        So a stop that reached disk during streaming sits, in window order, behind a
        reply that is still owed. Concatenating the owed rows after the whole disk
        slice therefore renders the pair inverted -- the stop card above the prose it
        terminated.

        Window order is authoritative and every row in this arm carries an id, so the
        position is derivable without the body matching the other arm needs.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._row("user", "run it", "u1", "2026-08-19T10:00:00+00:00")
        reply = self._row("assistant", "done", "a1", "2026-08-19T10:00:03+00:00")
        stop = self._row("assistant", "stopped", "s1", "2026-08-19T10:00:02+00:00")

        window = [q, reply, stop]
        all_msgs = [q, stop]  # the reply has not been flushed yet
        slot = SimpleNamespace(messages=window, _disk_older_count=0)

        disk_mids = {m["meta"]["mid"] for m in all_msgs}
        assert reply["meta"]["mid"] not in disk_mids, "fixture needs the reply un-persisted"
        assert stop["meta"]["mid"] in disk_mids, "fixture needs the stop already on disk"
        assert window.index(reply) < window.index(stop), (
            "fixture must place the owed reply BEFORE the persisted stop, else there "
            "is no inversion to detect"
        )

        out = _append_unflushed_tail(slot, all_msgs)  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]
        assert (
            bodies.count("stopped") == 1 and bodies.count("done") == 1
        ), f"a row was dropped or duplicated; got {bodies}"
        assert bodies.index("done") < bodies.index("stopped"), (
            f"the persisted stop was rendered before the owed reply it follows in the "
            f"window; got {bodies}"
        )

    def test_tail_with_no_inversion_still_appends_after_the_disk_slice(self):
        """Negative control: with no inversion the result must be the old concatenation.

        Fails for the intended reason if the merge reorders rows it should leave alone
        -- every owed row here already follows every persisted one in window order, so
        the answer must be exactly ``all_msgs + owed``.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._row("user", "run it", "u1", "2026-08-19T10:00:00+00:00")
        stop = self._row("assistant", "stopped", "s1", "2026-08-19T10:00:02+00:00")
        reply = self._row("assistant", "done", "a1", "2026-08-19T10:00:03+00:00")

        all_msgs = [q, stop]
        slot = SimpleNamespace(messages=[q, stop, reply], _disk_older_count=0)
        out = _append_unflushed_tail(slot, all_msgs)  # type: ignore[arg-type]
        assert out == [q, stop, reply], (
            f"the no-inversion case must be untouched concatenation; got "
            f"{[m['content'] for m in out]}"
        )

    @pytest.mark.asyncio
    async def test_tail_snapshot_is_captured_on_the_loop_not_inside_the_worker(
        self, tmp_path, monkeypatch
    ):
        """The window snapshot must be taken before the offload, not inside the thread.

        ``_flush_segment`` assigns ``slot.messages = head`` and only THEN appends the
        finalized assistant row, so between those two statements the window is a
        transient chunk-free head missing that row. It is a plain ``def`` that is never
        handed to ``to_thread``, so the event loop cannot interleave with it -- but the
        threaded tail scan can land exactly there and snapshot the hole, and
        ``_disk_older_count`` is untouched by that mutation so the bounded re-read
        cannot detect it.

        Simulated deterministically: the mutation is applied at the moment the worker
        is entered. A snapshot captured on the loop predates it and keeps the row; one
        taken inside the worker sees the hole and drops the row from the response.
        """
        import kiro_crew.dashboard.chat_handlers as ch
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("loopsnap")
        key = "dashboard:loopsnap"

        slot.append("user", "q1")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)
        disk = state.conversation_log.read_messages_chained(key)
        disk_mids = {
            m["meta"]["mid"]
            for m in disk
            if isinstance(m.get("meta"), dict) and isinstance(m["meta"].get("mid"), str)
        }
        assert disk and len(disk_mids) == len(disk), (
            f"fixture needs every disk row to carry an id so the id arm runs; "
            f"disk={len(disk)} mids={len(disk_mids)}"
        )

        owed = {
            "role": "assistant",
            "content": "finalized-reply",
            "cls": "msg msg-a",
            "ts": "2026-08-19T10:00:03+00:00",
            "meta": {"mid": "m-finalizedneverpersisted"},
        }
        slot.messages.append(owed)
        assert owed["meta"]["mid"] not in disk_mids, "fixture row must be un-persisted"

        original = ch._append_unflushed_tail_from_offset
        seen: dict[str, object] = {}

        def worker_entered(slot_arg, all_msgs_arg, **kwargs):
            # The worker is now running. Reproduce the mid-finalization window: the
            # finalized assistant row is not in slot.messages yet.
            seen["snapshot_kwarg"] = "snapshot" in kwargs
            slot_arg.messages = [
                m for m in slot_arg.messages if m.get("content") != "finalized-reply"
            ]
            return original(slot_arg, all_msgs_arg, **kwargs)

        monkeypatch.setattr(ch, "_append_unflushed_tail_from_offset", worker_entered)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/loopsnap?limit=10")
            assert resp.status == 200, await resp.text()
            data = await resp.json()

        assert (
            seen.get("snapshot_kwarg") is not None
        ), "the tail match never ran, so this test asserts nothing"
        assert seen["snapshot_kwarg"] is True, (
            "the caller did not pass a snapshot, so the window is still read inside "
            "the worker thread where a mid-finalization window is observable"
        )
        contents = [m["content"] for m in data["messages"]]
        assert "finalized-reply" in contents, (
            f"the worker snapshotted the transient chunk-free window and dropped the "
            f"finalized row; got {contents}"
        )

    @staticmethod
    def _idless(role: str, body: str, ts: str, cls: str | None = None) -> dict:
        """A row with NO ``meta.mid`` -- forces the id-less body-matching arm."""
        r = {"role": role, "content": body, "ts": ts}
        if cls is not None:
            r["cls"] = cls
        return r

    def test_live_streamed_row_before_a_persisted_row_is_not_dropped(self):
        """An owed transient row must survive the suffix boundary in the id-less arm.

        The matcher skipped a transient row WITHOUT advancing ``start``; a later
        persisted match then set ``start = i + 1``, moving the boundary PAST the skipped
        row, and ``window[start:]`` excluded it. So a live streamed chunk sitting before
        a persisted ``stop_event`` was dropped from the bounded response entirely.

        A disk read omits transient roles, so such a row can never be matched and is
        always owed -- which is precisely the rule the id-based arm already applies.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._idless("user", "run it", "2026-08-19T15:00:00+00:00")
        chunk = self._idless("chunk", "partial an", "2026-08-19T15:00:01+00:00")
        stop = self._idless("assistant", "stopped", "2026-08-19T15:00:02+00:00")

        all_msgs = [q, stop]  # a disk read holds no transient rows
        window = [q, chunk, stop]
        assert not any(
            "meta" in m for m in all_msgs
        ), "fixture must be id-less, else the id arm runs and this asserts nothing"
        assert window.index(chunk) < window.index(stop), (
            "fixture must place the transient BEFORE the persisted row, else the "
            "suffix boundary never moves past it"
        )

        slot = SimpleNamespace(messages=window, _disk_older_count=0)
        out = _append_unflushed_tail(slot, all_msgs)  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]
        assert (
            "partial an" in bodies
        ), f"the live streamed row was dropped by the suffix boundary; got {bodies}"
        assert bodies.index("partial an") < bodies.index("stopped"), (
            f"the owed transient must keep its window position ahead of the persisted "
            f"row; got {bodies}"
        )

    def test_trailing_transient_row_is_emitted_exactly_once(self):
        """Negative control for the merge: an owed transient must not be emitted twice.

        Rows left in ``pending`` sit at or after ``start`` and are therefore already
        carried by ``window[start:]``. Emitting them again as well would re-append a
        live row -- the duplication class this change exists to avoid. Fails for the
        intended reason if the merge and the suffix both claim the same row.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._idless("user", "run it", "2026-08-19T15:00:00+00:00")
        stop = self._idless("assistant", "stopped", "2026-08-19T15:00:02+00:00")
        chunk = self._idless("chunk", "partial an", "2026-08-19T15:00:03+00:00")

        slot = SimpleNamespace(messages=[q, stop, chunk], _disk_older_count=0)
        out = _append_unflushed_tail(slot, [q, stop])  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]
        assert (
            bodies.count("partial an") == 1
        ), f"the trailing transient was emitted more than once; got {bodies}"
        assert bodies == ["run it", "stopped", "partial an"], (
            f"a transient already after the last match must stay where it was; got " f"{bodies}"
        )

    def test_unowed_and_answered_rows_stay_excluded_from_the_idless_tail(self):
        """Negative control: only genuinely owed transients may be merged.

        ``done``/``queued`` are ``_UNOWED_WINDOW_ROLES`` and an answered ``permission``
        has already been dispositioned, so neither is owed. Merging transients
        unconditionally would re-append them, which is the same defect in the opposite
        direction from the drop above. A still-pending permission IS owed, so it is
        included -- that arm of the assertion is what stops the fix from over-narrowing
        into a second drop.
        """
        import json
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._idless("user", "run it", "2026-08-19T15:00:00+00:00")
        stop = self._idless("assistant", "stopped", "2026-08-19T15:00:02+00:00")
        done = self._idless("done", "unowed-marker", "2026-08-19T15:00:01+00:00")
        answered = self._idless(
            "permission",
            "answered-approval",
            "2026-08-19T15:00:01+00:00",
            cls=json.dumps({"resolved": "approve"}),
        )
        pending = self._idless(
            "permission",
            "pending-approval",
            "2026-08-19T15:00:01+00:00",
            cls=json.dumps({}),
        )

        for extra, body, owed in (
            (done, "unowed-marker", False),
            (answered, "answered-approval", False),
            (pending, "pending-approval", True),
        ):
            slot = SimpleNamespace(messages=[q, extra, stop], _disk_older_count=0)
            out = _append_unflushed_tail(slot, [q, stop])  # type: ignore[arg-type]
            bodies = [m["content"] for m in out]
            if owed:
                assert body in bodies, (
                    f"{extra['role']} row {body!r} is owed and must be merged; got " f"{bodies}"
                )
            else:
                assert body not in bodies, (
                    f"{extra['role']} row {body!r} is NOT owed and must not be "
                    f"re-appended; got {bodies}"
                )

    def test_unmatched_row_does_not_strand_the_persisted_rows_behind_it(self):
        """An owed row must not make the id-less arm re-emit later persisted rows.

        The arm forward-scans the disk slice for each window row. When a row was NOT
        found the loop would ``break`` outright, which left ``start`` pointing AT that
        row -- so ``window[start:]`` carried it AND every later window row, including
        rows the disk slice already holds. Those came back a second time, and out of
        order, which is the duplication this function exists to remove.

        The reachable shape is a ``stop_event`` flushed before reply finalization:
        ``_flush_segment`` re-appends the stop AFTER the finalized assistant row, so the
        window reads ``[... unflushed reply, flushed stop]``. The reply misses, and on
        the old code the persisted stop was emitted twice.

        The sibling id-carrying arm already holds an unmatched row and keeps walking, so
        this mirrors that rule rather than inventing a second one.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._idless("user", "run it", "2026-08-19T16:00:00+00:00")
        reply = self._idless("assistant", "the reply", "2026-08-19T16:00:01+00:00")
        stop = self._idless("assistant", "stopped", "2026-08-19T16:00:02+00:00")

        # The reply is still owed; the stop already reached disk ahead of it.
        all_msgs = [q, stop]
        window = [q, reply, stop]
        assert not any(
            "meta" in m for m in all_msgs
        ), "fixture must be id-less, else the id arm runs and this asserts nothing"
        assert window.index(reply) < window.index(stop), (
            "fixture must place the UNMATCHED row before the persisted one, else the "
            "loop never abandons the rows behind it"
        )
        assert not any(
            m["content"] == "the reply" for m in all_msgs
        ), "fixture must keep the reply off disk, else there is nothing unmatched"

        slot = SimpleNamespace(messages=window, _disk_older_count=0)
        out = _append_unflushed_tail(slot, all_msgs)  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]

        assert bodies.count("stopped") == 1, (
            f"the persisted stop was re-emitted because an earlier unmatched row "
            f"abandoned the scan; got {bodies}"
        )
        assert bodies == [
            "run it",
            "the reply",
            "stopped",
        ], f"output must follow window order with each row exactly once; got {bodies}"

    def test_unmatched_row_is_still_emitted_and_not_dropped(self):
        """Negative control: holding the unmatched row must not silently drop it.

        The fix stops an unmatched row from stranding the rows behind it. The opposite
        failure is just as silent -- discarding that row instead of retaining it would
        also stop the duplicate, and would pass a test that only counted the persisted
        row. So assert the owed row itself survives, in both the with-a-later-match and
        no-later-match shapes.
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._idless("user", "ask", "2026-08-19T16:10:00+00:00")
        owed = self._idless("assistant", "owed body", "2026-08-19T16:10:01+00:00")
        persisted = self._idless("assistant", "on disk", "2026-08-19T16:10:02+00:00")

        # (a) a later row DOES match, so the owed row is flushed at that disk position
        slot = SimpleNamespace(messages=[q, owed, persisted], _disk_older_count=0)
        out = _append_unflushed_tail(slot, [q, persisted])  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]
        assert (
            bodies.count("owed body") == 1
        ), f"the owed row must be retained exactly once, not dropped; got {bodies}"
        assert bodies.index("owed body") < bodies.index(
            "on disk"
        ), f"the retained row must keep its window position; got {bodies}"

        # (b) NO later row matches, so the owed row can only arrive via the tail
        slot = SimpleNamespace(messages=[q, owed], _disk_older_count=0)
        out = _append_unflushed_tail(slot, [q])  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]
        assert (
            bodies.count("owed body") == 1
        ), f"with no later match the owed row must still be emitted once; got {bodies}"

    def test_trailing_unowed_and_answered_rows_are_excluded_from_the_idless_tail(self):
        """An unowed row in the LAST window position must not reach the tail.

        ``start`` only advances on a match, and an unowed row takes the transient branch
        and ``continue``s -- skipping ``start = i + 1``. So when such a row TRAILS the last
        match, a raw ``window[start:]`` re-admits it and undoes the exclusion the loop's own
        guard performed. The sibling id-carrying arm has no such hole because it applies
        both exclusions at the top of its loop.

        Two symptoms share that cause. A trailing ``done`` reaches the bounded response and
        ``_prepare_messages`` drops it while rendering, so a page whose only row is that
        ``done`` renders EMPTY and replaces the transcript. A trailing answered
        ``permission`` is instead re-ordered after every persisted row.

        The sibling test above puts the extra row in the MIDDLE, where the later match
        advances ``start`` PAST it -- so the exclusion it observes comes from ``start``
        overshooting, not from any filter, and it cannot see this defect. The whole body
        list is asserted rather than mere membership, because membership alone cannot see a
        mis-ordering.
        """
        import json
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._idless("user", "run it", "2026-08-20T04:00:00+00:00")
        stop = self._idless("assistant", "stopped", "2026-08-20T04:00:01+00:00")
        done = self._idless("done", "unowed-marker", "2026-08-20T04:00:02+00:00")
        queued = self._idless("queued", "queued-marker", "2026-08-20T04:00:02+00:00")
        answered = self._idless(
            "permission",
            "answered-approval",
            "2026-08-20T04:00:02+00:00",
            cls=json.dumps({"resolved": "approve"}),
        )

        for extra, body in (
            (done, "unowed-marker"),
            (queued, "queued-marker"),
            (answered, "answered-approval"),
        ):
            window = [q, stop, extra]
            assert window[-1] is extra, (
                "fixture must place the unowed row LAST, else a later match advances "
                "start past it and the assertion passes without a filter"
            )
            slot = SimpleNamespace(messages=window, _disk_older_count=0)
            out = _append_unflushed_tail(slot, [q, stop])  # type: ignore[arg-type]
            bodies = [m["content"] for m in out]
            assert body not in bodies, (
                f"trailing {extra['role']} row {body!r} was re-admitted by the unfiltered "
                f"tail slice; got {bodies}"
            )
            assert bodies == ["run it", "stopped"], (
                f"the bounded page must hold exactly the persisted rows in order; got " f"{bodies}"
            )

    def test_trailing_owed_rows_survive_the_idless_tail_filter(self):
        """Negative control: the tail filter must not over-narrow into a second drop.

        ``_UNOWED_WINDOW_ROLES`` is ``{done, queued}`` -- ``chunk`` and ``streaming`` are
        subtracted out, and a still-PENDING ``permission`` has not been dispositioned. All
        three are genuinely owed: a disk read carries no transient row, so they can never be
        matched, and dropping them loses a live streamed answer or an approval the user has
        yet to answer. That is the opposite defect from the leak, and it is the one an
        over-eager filter actually produces.
        """
        import json
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_handlers import _append_unflushed_tail

        q = self._idless("user", "run it", "2026-08-20T04:10:00+00:00")
        stop = self._idless("assistant", "stopped", "2026-08-20T04:10:01+00:00")
        pending = self._idless(
            "permission",
            "pending-approval",
            "2026-08-20T04:10:02+00:00",
            cls=json.dumps({}),
        )
        chunk = self._idless("chunk", "partial an", "2026-08-20T04:10:02+00:00")

        slot = SimpleNamespace(messages=[q, stop, pending], _disk_older_count=0)
        out = _append_unflushed_tail(slot, [q, stop])  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]
        assert bodies == ["run it", "stopped", "pending-approval"], (
            f"a still-pending permission is owed and must survive the tail filter; got " f"{bodies}"
        )

        slot = SimpleNamespace(messages=[q, stop, chunk], _disk_older_count=0)
        out = _append_unflushed_tail(slot, [q, stop])  # type: ignore[arg-type]
        bodies = [m["content"] for m in out]
        assert (
            bodies.count("partial an") == 1
        ), f"a trailing live chunk is owed and must be emitted exactly once; got {bodies}"
        assert bodies == [
            "run it",
            "stopped",
            "partial an",
        ], f"the owed chunk must keep its trailing window position; got {bodies}"


class TestHistoryResumeInterruptedTurnMarker:
    """The History resume endpoint shares the startup restore's marker handling."""

    @staticmethod
    def _saved_state(tmp_path, monkeypatch, name: str, rows: list[tuple], generation: int):
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot(name)
        for row in rows:
            slot.append(*row[:3], meta=row[3] if len(row) > 3 else None)
        slot._turn_in_flight_generation = generation
        assert _save_slot_to_history(state, slot, force=True)
        del state._slots[slot.key]
        return state

    @pytest.mark.asyncio
    async def test_resume_reconciles_partial_assistant_and_tool_tail(self, tmp_path, monkeypatch):
        state = self._saved_state(
            tmp_path,
            monkeypatch,
            "resume-interrupted",
            [
                ("user", "inspect it", "msg msg-u"),
                ("assistant", "partial result", "msg msg-a"),
                ("tool", "read complete", "tool", {"done": True}),
            ],
            generation=18,
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            response = await client.post(
                "/api/chat/slots/resume-interrupted/resume",
                json={"key": "dashboard:resume-interrupted"},
            )
            payload = await response.json()

        assert response.status == 200
        restored = state.get_slot("resume-interrupted")
        assert restored is not None
        assert restored.to_dict()["interrupted"] is True
        assert restored.messages[-1]["meta"]["kind"] == "gateway_restart_interruption"
        # The paging cursor counts the recovered row, so the next older page
        # starts where this response's rows end.
        assert payload["total"] == 4
        assert payload["messages"][-1]["role"] == "error"
        assert payload["next_before"] == 0
        assert payload["has_more"] is False

    @pytest.mark.asyncio
    async def test_resume_preserves_a_deliberate_stop(self, tmp_path, monkeypatch):
        state = self._saved_state(
            tmp_path,
            monkeypatch,
            "resume-stopped",
            [
                ("user", "inspect it", "msg msg-u"),
                ("system", "Stopped", json.dumps({"kind": "stop_event"})),
            ],
            generation=19,
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            response = await client.post(
                "/api/chat/slots/resume-stopped/resume",
                json={"key": "dashboard:resume-stopped"},
            )
            payload = await response.json()

        assert response.status == 200
        restored = state.get_slot("resume-stopped")
        assert restored is not None
        assert restored.to_dict()["interrupted"] is False
        assert all(
            (m.get("meta") or {}).get("kind") != "gateway_restart_interruption"
            for m in restored.messages
        )
        assert payload["total"] == 2

    @pytest.mark.asyncio
    async def test_resume_ignores_a_malformed_boolean_marker(self, tmp_path, monkeypatch):
        state = self._saved_state(
            tmp_path,
            monkeypatch,
            "resume-malformed",
            [("user", "hi", "msg msg-u"), ("assistant", "complete", "msg msg-a")],
            generation=0,
        )
        assert state.conversation_log is not None
        state.conversation_log.update_metadata(
            "dashboard:resume-malformed", {"turn_in_flight_generation": True}
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            response = await client.post(
                "/api/chat/slots/resume-malformed/resume",
                json={"key": "dashboard:resume-malformed"},
            )

        assert response.status == 200
        restored = state.get_slot("resume-malformed")
        assert restored is not None
        assert restored.to_dict()["interrupted"] is False
        assert all(
            (m.get("meta") or {}).get("kind") != "gateway_restart_interruption"
            for m in restored.messages
        )


def _long_turn(n_chunks: int, finished: asyncio.Event):
    """A turn that streams *n_chunks* rows of about 1 KB through the real ``append``."""

    async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
        for i in range(n_chunks):
            sl.append("chunk", f"{i:06d}" + "x" * 1024, "chunk")
            if i % 16 == 0:
                await asyncio.sleep(0)
        sl.append("assistant", "final answer", "msg")
        sl.purge_chunks()  # the runner's turn end
        sl.append("done", "", "done")
        finished.set()

    return fake_run_chat


async def _post_and_never_read(client, slot_key: str):
    """A client that sends a whole ``POST /api/chat`` and then never reads its socket."""
    import socket

    host, port = client.server.host, client.server.port
    loop = asyncio.get_running_loop()
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    raw.setblocking(False)
    await loop.sock_connect(raw, (host, port))
    body = json.dumps({"message": "hello", "slot": slot_key}).encode()
    head = (
        f"POST /api/chat HTTP/1.1\r\nHost: {host}:{port}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode()
    await loop.sock_sendall(raw, head + body)
    return raw


@pytest.mark.asyncio
class TestApiChatStalledReader:
    """An SSE reader that stops reading is cut off, so it stops holding its slot."""

    async def test_a_stalled_reader_is_cut_off_and_releases_the_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._SSE_WRITE_DEADLINE_SECS", 0.5, raising=False
        )
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        broadcast: list[str] = []
        slot._on_message = lambda _key, msg: broadcast.append(str(msg.get("content", "")))
        finished = asyncio.Event()
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._run_chat", _long_turn(8000, finished)
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            raw = await _post_and_never_read(client, "s1")
            try:
                await asyncio.wait_for(finished.wait(), 60)
                await async_wait_until(  # the write deadline cuts the stalled reader off
                    lambda: slot._pending_consumers == 0,
                    describe=lambda: f"pending_consumers={slot._pending_consumers}",
                )
                consumers, reader = slot._pending_consumers, slot._has_reader
                held = [m for m in slot._pending if m.get("role") == "chunk"]
                # A row from outside the turn, as a cron or sub-agent notice adds one.
                slot.append("assistant", "a later notice", "msg")
                # The next turn on the slot, sent from a dashboard tab.
                finished.clear()
                sent = await client.post("/api/chat?ws=1", json={"message": "again", "slot": "s1"})
                assert (await sent.json())["ok"] is True
                await asyncio.wait_for(finished.wait(), 60)
                held_next = [m for m in slot._pending if m.get("role") == "chunk"]
            finally:
                raw.close()

        noticed = "a later notice" in broadcast
        assert (consumers, reader, len(held), noticed, len(held_next)) == (0, False, 0, True, 0), (
            f"a stalled SSE reader still holds its slot after the turn: "
            f"pending_consumers={consumers}, _has_reader={reader}, {len(held)} chunk rows "
            f"({sum(len(m['content']) for m in held)} bytes) held, a later row broadcast to other "
            f"tabs={noticed}; after the next turn {len(held_next)} chunk rows "
            f"({sum(len(m['content']) for m in held_next)} bytes) stay held"
        )

    async def test_a_reading_client_gets_the_whole_turn(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        finished = asyncio.Event()
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._run_chat", _long_turn(8000, finished)
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"message": "hello", "slot": "s1"}, timeout=None
            )
            body = await resp.read()
            await asyncio.wait_for(finished.wait(), 60)

        assert body.endswith(b"data: [DONE]\n\n")
        assert all(f"{i:06d}x".encode() in body for i in (0, 3999, 7999))
        assert slot._pending_consumers == 0 and slot._has_reader is False
        assert not [m for m in slot._pending if m.get("role") == "chunk"]
