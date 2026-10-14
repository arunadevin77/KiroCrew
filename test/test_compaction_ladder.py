"""The compaction method ladder (``CompactionCoordinator._run_method_ladder``).

``session.compaction_method`` sets the ceiling walked down when the auto-compact threshold
fires. ``native`` is the in-place ``/compact`` the coordinator always ran; a
rotation method recycles the session on purpose so the successor is re-seeded
from the transcript, and hands the turn on when it cannot run (``unavailable``)
or would not clear the threshold (``insufficient``). The first test pins the
contract everything else rests on: with the default method, dispatch is the
pre-ladder dispatch exactly, and no rotation seam is read.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard.compaction_seed import DashboardSeedWriter
from kiro_crew.messaging.queue_drain import QUEUED_CHANNEL_KEY
from kiro_crew.session import SessionManager
from kiro_crew.session_compaction import (
    COMPACT_OUTCOME_CANCELLED,
    COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS,
)
from kiro_crew.testing.wait import async_wait_until, await_until

KEY = "dashboard:compaction-ladder"


@pytest.fixture
def cfg() -> KiroCrewConfig:
    c = KiroCrewConfig()
    c.session.timeout_secs = 2
    return c


async def _drain_background(mgr: SessionManager) -> None:
    pending = [t for t in mgr._background_tasks if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


@contextlib.asynccontextmanager
async def _managed(cfg: KiroCrewConfig, provider_factory: Any):
    """Manager whose teardown always runs, even on a failed assert."""
    mgr = SessionManager(cfg, provider_factory=provider_factory)
    try:
        yield mgr
    finally:
        await _drain_background(mgr)
        await mgr.close_all()


class _Writer:
    """A ``SeedWriter`` fake: ``supports`` answers *supports*, the write answers *answer*.

    The write is an ``AsyncMock`` so a test can assert whether, and with what,
    the ladder awaited it; *side_effect* makes the write raise instead.
    """

    def __init__(
        self, answer: str = "written", *, supports: bool = True, side_effect: Any = None
    ) -> None:
        self.write = AsyncMock(return_value=answer, side_effect=side_effect)
        self.supported = supports
        self.asked: list[str] = []

    def supports(self, key: str) -> bool:
        self.asked.append(key)
        return self.supported

    async def __call__(self, key: str, method: str, window_tokens: int) -> str:
        return await self.write(key, method, window_tokens)


def _provider_factory(
    *, pct: float = 92.0, window: int = 1_000_000, successor_pct: float | None = None
):
    """Provider at *pct* on a *window*-token model that compacts in place on request.

    ``context_window_tokens`` is a sync ``MagicMock`` (not an ``AsyncMock``
    attribute, which would hand the ladder a coroutine) so a test can both set
    the window and assert whether the ladder read it. *successor_pct*, when
    given, is what every provider after the first reports: the successor of a
    rotation starts at its real usage, not at the predecessor's.
    """
    created = {"n": 0}

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        created["n"] += 1
        my_pct = pct if created["n"] == 1 or successor_pct is None else successor_pct
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.has_active_turn = lambda: False
        state = {"compacted": False}
        m.context_usage_pct = lambda: 0.0 if state["compacted"] else my_pct
        m.context_usage_unknown = lambda: bool(state["compacted"])
        m.manual_compact_unsupported_backend = None
        m.context_window_tokens = MagicMock(return_value=window)

        async def _stream(_cmd):
            for ev in []:
                yield ev  # pragma: no cover - status arrives async, not inline

        m.stream_command = MagicMock(side_effect=_stream)

        async def _wait(timeout=None):
            state["compacted"] = True
            return {"type": "completed"}

        m.wait_for_compaction = AsyncMock(side_effect=_wait)
        return m

    return factory


class _SemaphoreSpy:
    """Counts acquisitions while behaving exactly like the real semaphore."""

    def __init__(self, inner: asyncio.Semaphore) -> None:
        self._inner = inner
        self.acquires = 0

    async def acquire(self) -> bool:
        self.acquires += 1
        return await self._inner.acquire()

    def release(self) -> None:
        self._inner.release()

    def locked(self) -> bool:
        return self._inner.locked()

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, *exc: object) -> None:
        self.release()


async def _live_session(mgr: SessionManager, key: str = KEY):
    """Register a live session for *key* and return (provider, session, spy)."""
    provider, _, _ = await mgr.get_or_create(key)
    mgr.release(key)
    session = mgr._sessions[key]
    spy = _SemaphoreSpy(session.semaphore)
    session.semaphore = spy
    return provider, session, spy


def _callback_recorder() -> tuple[list[tuple[str, float, bool, str, str, bool]], Any]:
    """Record every callback call as ``(key, pct, success, outcome, method, all_kept)``."""
    calls: list[tuple[str, float, bool, str, str, bool]] = []

    async def _cb(
        key: str,
        pct: float,
        *,
        success: bool,
        outcome: str = "compacted",
        method: str,
        all_kept: bool = False,
    ) -> None:
        calls.append((key, pct, success, outcome, method, all_kept))

    return calls, _cb


class _SharedSubagent:
    """One sub-agent live on the parent's process until ``gate`` is set: the slice
    of ``SubagentManager`` the restart's cotenant wait reads and ends."""

    def __init__(self, key: str, gate: asyncio.Event) -> None:
        self.gate = gate
        self.child = SimpleNamespace(
            id="a1", parent_session_key=key, conversation_key="", _stop_origin=""
        )

    @property
    def running(self) -> list[Any]:
        return [] if self.gate.is_set() else [self.child]

    def has_live_shared_session(self, _key: str) -> bool:
        return not self.gate.is_set()

    def snapshot_teardown_children(self, _parent: str) -> tuple[()]:
        return ()

    async def cancel(self, _id: str) -> bool:
        self.gate.set()
        return True

    async def cancel_for_teardown(
        self, ids: Any, *, parent_session_key: str, verb: str = ""
    ) -> int:
        return len(ids)


def _cotenant_wait(mgr: SessionManager, wait_secs: float) -> None:
    mgr._compaction._deps = dataclasses.replace(
        mgr._compaction._deps, cotenant_wait_secs=wait_secs, cotenant_poll_secs=0.01
    )


class TestRotationRestart:
    """A rotation ends the process the way every other compaction restart does."""

    @pytest.mark.asyncio
    async def test_a_rotation_waits_for_a_shared_subagent_then_restarts(self, cfg) -> None:
        """The process also hosts the session's sharing sub-agents. The rotation
        holds for them first, as the in-place failure path does, and stops them
        only past the wait budget, so none of them ends with no report."""
        cfg.session.compaction_method = "soft"
        calls, cb = _callback_recorder()
        gate = asyncio.Event()
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            mgr.set_compaction_seed_writer(_Writer())
            provider, _, _ = await _live_session(mgr)
            runs = _SharedSubagent(KEY, gate)
            mgr.set_child_teardown_handler(runs)
            _cotenant_wait(mgr, 0.3)

            assert await mgr.compact_if_needed(KEY) == "recycled"

            assert gate.is_set(), "the shared sub-agent was stopped at the deadline"
            provider.shutdown.assert_awaited_once()
            assert [(c[2], c[3]) for c in calls] == [
                (False, COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS),
                (True, "recycled"),
            ]
            assert calls[-1][4] == "soft"
            # The wait is a rotation's, not a failed compaction's: the waiting
            # notice and the stopped sub-agent's reason say so.
            assert calls[0][4] == "soft"
            assert runs.child._stop_origin == (
                "stopped when an auto-compaction rotated the session (soft)"
            )

    @pytest.mark.asyncio
    async def test_a_hard_stop_during_the_wait_settles_cancelled_and_writes_no_seed(
        self, cfg
    ) -> None:
        """A force Stop inside the cotenant wait already reset the session and
        handed its permit on. The rotation settles cancelled before any seed is
        written, spares the live sub-agent, and does not release the permit a
        second time (the turn semaphore is bounded, so a double release raises)."""
        cfg.session.compaction_method = "shake"
        calls, cb = _callback_recorder()
        gate = asyncio.Event()
        writer = _Writer("written")
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            mgr.set_compaction_seed_writer(writer)
            await mgr.get_or_create(KEY)
            mgr.release(KEY)
            mgr.set_child_teardown_handler(_SharedSubagent(KEY, gate))
            _cotenant_wait(mgr, 30.0)

            task = asyncio.ensure_future(mgr.compact_if_needed(KEY))
            await async_wait_until(
                lambda: any(c[3] == COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS for c in calls),
                describe=lambda: calls,
            )
            assert await mgr.stop_turn(KEY, force=True) == "hard"

            assert await await_until(task, what="the rotation settling") == "cancelled"
            writer.write.assert_not_awaited()
            assert not gate.is_set(), "the live sub-agent was spared"
            assert (calls[-1][2], calls[-1][3]) == (False, COMPACT_OUTCOME_CANCELLED)
            assert KEY not in mgr._compaction.state.pending_rotation
            gate.set()

    @pytest.mark.asyncio
    async def test_a_stop_during_the_seed_write_settles_cancelled_without_a_restart(
        self, cfg
    ) -> None:
        """A Stop that lands while the digest is written: restarting afterwards
        would announce a rotation the user stopped, so the session is kept."""
        cfg.session.compaction_method = "shake"
        calls, cb = _callback_recorder()
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            provider, session, spy = await _live_session(mgr)
            writer = _Writer("written")

            async def stop_then_answer(*_args: object) -> str:
                assert mgr.note_stop(KEY) is True
                return "written"

            writer.write.side_effect = stop_then_answer
            mgr.set_compaction_seed_writer(writer)

            assert await mgr.compact_if_needed(KEY) == "cancelled"

            provider.shutdown.assert_not_awaited()
            assert mgr._sessions[KEY] is session
            assert KEY not in mgr._compaction.state.pending_rotation
            assert (calls[-1][2], calls[-1][3]) == (False, COMPACT_OUTCOME_CANCELLED)
            # Posted inside the rotation, so it names it: the dashboard marks
            # the notice and a later replay leaves it out of the seed's tail.
            assert calls[-1][4] == "shake"
            assert KEY not in mgr._compaction.state.rotating
            assert not spy.locked()

    @pytest.mark.asyncio
    async def test_a_reset_during_the_seed_write_answers_absent(self, cfg) -> None:
        """A reset that is not a Stop (a rewind, a model switch) replaces the
        session while the digest is written. The rotation answers absent: no
        restart of the stale session, no rotation notice, and no verdict armed on
        a successor the rotation never measured."""
        cfg.session.compaction_method = "shake"
        calls, cb = _callback_recorder()
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            _, session, _ = await _live_session(mgr)
            writer = _Writer("written")

            async def reset_then_answer(*_args: object) -> str:
                assert await mgr.reset(KEY) is True
                return "written"

            writer.write.side_effect = reset_then_answer
            mgr.set_compaction_seed_writer(writer)

            assert await mgr.compact_if_needed(KEY) == "absent"

            state = mgr._compaction.state
            assert KEY not in state.pending_verdict and KEY not in state.pending_rotation
            assert KEY not in state.rotating
            assert calls == [], "no notice for a rotation that did not run"
            assert mgr._sessions.get(KEY) is not session

    @pytest.mark.asyncio
    async def test_a_message_queued_behind_the_rotation_moves_to_the_successor(self, cfg) -> None:
        """A message queued while the rotation holds the session lands in the
        successor's queue, as it does behind every other compaction restart."""
        cfg.session.compaction_method = "shake"
        async with _managed(cfg, _provider_factory()) as mgr:
            _, session, _ = await _live_session(mgr)
            writer = _Writer("written")

            async def queue_then_answer(*_args: object) -> str:
                assert mgr.enqueue(
                    KEY, "ts-1", "sent mid-rotation", **{QUEUED_CHANNEL_KEY: "slack"}
                )
                return "written"

            writer.write.side_effect = queue_then_answer
            mgr.set_compaction_seed_writer(writer)

            assert await mgr.compact_if_needed(KEY) == "recycled"

            successor = mgr._sessions[KEY]
            assert successor is not session
            assert [entry[0] for entry in successor.queue] == ["ts-1"]


class TestRotationIdentity:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["shake", "soft"])
    async def test_reset_session_cannot_be_rotated_after_the_wait(
        self, cfg, monkeypatch, method
    ) -> None:
        """A reset can pop the held session while a rotation waits for its permit."""
        cfg.session.compaction_method = method
        calls, cb = _callback_recorder()
        writer = _Writer()
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            # Both rotations ask the surface question first; soft writes no seed.
            mgr.set_compaction_seed_writer(writer)
            provider, session, _ = await _live_session(mgr)
            await session.semaphore.acquire()
            acquire_started = asyncio.Event()
            acquire = session.semaphore.acquire

            async def acquire_after_signal() -> bool:
                acquire_started.set()
                return await acquire()

            monkeypatch.setattr(session.semaphore, "acquire", acquire_after_signal)
            rotation = asyncio.create_task(mgr.compact_if_needed(KEY))
            try:
                await await_until(acquire_started, what="the rotation's permit wait")
                assert await mgr.reset(KEY)
                reset_shutdowns = provider.shutdown.await_count
                mgr.release(KEY)

                assert await await_until(rotation, what="the rotation settling") == "absent"
                writer.write.assert_not_awaited()
                state = mgr._compaction.state
                assert KEY not in state.pending_verdict
                assert KEY not in state.pending_rotation
                assert calls == []
                assert provider.shutdown.await_count == reset_shutdowns == 1
            finally:
                if not rotation.done():
                    rotation.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await rotation
                if mgr._sessions.get(KEY) is session and session.semaphore.locked():
                    mgr.release(KEY)


class TestDefaultOrderIsNative:
    @pytest.mark.asyncio
    async def test_native_only_dispatches_in_place_and_reads_no_rotation_seam(self, cfg) -> None:
        """The default order is ``native`` alone: ``/compact`` is streamed under
        the semaphore, the window is never read, and no seed writer is consulted."""
        assert cfg.session.compaction_method == "native"
        async with _managed(cfg, _provider_factory()) as mgr:
            provider, _, spy = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once_with("/compact")
            provider.context_window_tokens.assert_not_called()
            assert spy.acquires == 1
            assert KEY in mgr._sessions, "an in-place compaction keeps the session"

    @pytest.mark.asyncio
    async def test_native_compaction_names_itself_to_the_callback(self, cfg) -> None:
        """The callback learns which method ran: an in-place summary says ``native``
        (and ``compacted``), so the notice writers can tell it from a rotation
        (below) that reaches them through the same ``success=True`` call."""
        calls, cb = _callback_recorder()
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "ok"

            assert calls == [(KEY, 92.0, True, "compacted", "native", False)]


class TestSoft:
    @pytest.mark.asyncio
    async def test_soft_recycles_without_a_model_call(self, cfg) -> None:
        """A dashboard surface (the registered writer answers ``supports``) is the
        one place soft may run: the successor is re-seeded from that transcript,
        so the rotation spends no model call and writes no digest."""
        cfg.session.compaction_method = "soft"
        calls, cb = _callback_recorder()
        writer = _Writer()
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            mgr.set_compaction_seed_writer(writer)
            provider, session, spy = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "recycled"

            provider.stream_command.assert_not_called()
            provider.shutdown.assert_awaited_once()
            # The restart every compaction path takes: the successor starts
            # first and holds the key, but no turn has claimed it yet, so its
            # first real turn still replays the transcript.
            successor = mgr._sessions[KEY]
            assert successor is not session
            assert successor.first_turn.is_new, "the successor replays on its first turn"
            assert spy.acquires == 1, "rotation excludes turns like the in-place path"
            assert not spy.locked(), "the semaphore is released after the recycle"
            assert session.turn_owner is None, "the release clears the holder record"
            assert writer.asked == [KEY], "the surface was asked before the projection"
            writer.write.assert_not_awaited()
            # The callback is told it was a rotation: the successor carries only
            # the tail, and the notice has to say so rather than claim a summary.
            # A rotation is a recycle to the outcome arm as well; ``method`` is
            # what tells the surface it was the intended kind. A plain soft drops
            # the pre-tail history, so it never claims ``all_kept``.
            assert calls == [(KEY, 92.0, True, "recycled", "soft", False)]

    @pytest.mark.asyncio
    async def test_soft_without_a_seed_writer_hands_straight_to_native(self, cfg, caplog) -> None:
        """No writer is registered (a gateway with no dashboard), so no surface
        holds a transcript the successor could be re-seeded from: a soft recycle
        would come back with zero turns. The method is unavailable and native
        summarizes in place; the session is never recycled."""
        cfg.session.compaction_method = "soft"
        calls, cb = _callback_recorder()
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            provider, _, spy = await _live_session(mgr)
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once_with("/compact")
            provider.shutdown.assert_not_awaited()
            assert KEY in mgr._sessions, "no rotation ran, so the session was not recycled"
            assert KEY not in mgr._compaction.state.pending_rotation
            assert not spy.locked()
        attempted = [
            m.split("compaction method ")[1].split(":")[0]
            for m in (rec.getMessage() for rec in caplog.records)
            if "compaction method " in m
        ]
        assert attempted == ["soft", "native"]
        assert any("compaction method soft: unavailable" in r.getMessage() for r in caplog.records)
        assert calls == [(KEY, 92.0, True, "compacted", "native", False)]

    @pytest.mark.asyncio
    async def test_soft_on_a_surface_the_writer_does_not_support_hands_straight_to_native(
        self, cfg, caplog
    ) -> None:
        """The one registered writer holds no transcript for this key (a cron
        job with no tab, a hook, a subagent, a channel-born session): the
        dashboard replay is the only thing that re-seeds a soft successor, so
        the same surface question shake asks gates soft. Asked BEFORE the
        projection, answered False, the method is unavailable, the write is
        never attempted and native keeps the history."""
        cfg.session.compaction_method = "soft"
        calls, cb = _callback_recorder()
        writer = _Writer(supports=False)
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            mgr.set_compaction_seed_writer(writer)
            provider, _, spy = await _live_session(mgr)
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once_with("/compact")
            provider.shutdown.assert_not_awaited()
            assert KEY in mgr._sessions, "no rotation ran, so the session was not recycled"
            assert KEY not in mgr._compaction.state.pending_rotation
            assert not spy.locked()
        assert writer.asked == [KEY]
        writer.write.assert_not_awaited()
        attempted = [
            m.split("compaction method ")[1].split(":")[0]
            for m in (rec.getMessage() for rec in caplog.records)
            if "compaction method " in m
        ]
        assert attempted == ["soft", "native"]
        assert any("compaction method soft: unavailable" in r.getMessage() for r in caplog.records)
        assert calls == [(KEY, 92.0, True, "compacted", "native", False)]

    @pytest.mark.asyncio
    async def test_soft_without_a_seed_writer_is_unavailable_at_any_window(
        self, cfg, caplog
    ) -> None:
        """Availability is a property of the surface and is answered before the
        projection: at a window where the tail alone would not clear the band,
        a no-writer soft still says unavailable, never insufficient, so the log
        names the reason that would have held at every window."""
        cfg.session.compaction_method = "soft"
        cfg.session.autocompact_pct = 15.0
        async with _managed(cfg, _provider_factory(window=100_000)) as mgr:
            provider, _, _ = await _live_session(mgr)
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "ok"
            provider.shutdown.assert_not_awaited()
            provider.stream_command.assert_called_once_with("/compact")
        messages = [rec.getMessage() for rec in caplog.records]
        assert any("compaction method soft: unavailable" in m for m in messages)
        assert not any("compaction method soft: insufficient" in m for m in messages)

    @pytest.mark.asyncio
    async def test_soft_falls_through_to_native_when_the_window_is_unknown(self, cfg) -> None:
        cfg.session.compaction_method = "soft"
        async with _managed(cfg, _provider_factory(window=0)) as mgr:
            mgr.set_compaction_seed_writer(_Writer())
            provider, _, _ = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once_with("/compact")
            provider.shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_soft_falls_through_when_the_tail_would_not_clear_the_threshold(
        self, cfg
    ) -> None:
        """A small window: the replay tail plus startup context leaves the successor in the band.

        The carried tail is the replay's own budget for the window (~4K tokens
        at 100K, floored) and the startup block ~8K more, so the successor would
        start near 12%: inside a 15% threshold's 5-point margin.
        """
        cfg.session.compaction_method = "soft"
        cfg.session.autocompact_pct = 15.0
        writer = _Writer()
        async with _managed(cfg, _provider_factory(window=100_000)) as mgr:
            mgr.set_compaction_seed_writer(writer)
            provider, _, _ = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once_with("/compact")
            provider.shutdown.assert_not_awaited()
            assert writer.asked == [KEY], "the surface was asked before the projection"

    @pytest.mark.asyncio
    async def test_native_always_runs_last_when_every_rotation_steps_aside(self, cfg) -> None:
        cfg.session.compaction_method = "soft"
        async with _managed(cfg, _provider_factory(window=0)) as mgr:
            mgr.set_compaction_seed_writer(_Writer())
            provider, _, _ = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once_with("/compact")

    @pytest.mark.asyncio
    async def test_a_held_turn_defers_the_whole_ladder(self, cfg, monkeypatch) -> None:
        cfg.session.compaction_method = "soft"
        monkeypatch.setattr("kiro_crew.session.COMPACT_WAIT_TIMEOUT_SECS", 0.01)
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compaction_seed_writer(_Writer())
            provider, session, _ = await _live_session(mgr)
            await session.semaphore.acquire()
            try:
                assert await mgr.compact_if_needed(KEY) == "busy"
            finally:
                session.semaphore.release()

            provider.stream_command.assert_not_called()
            provider.shutdown.assert_not_awaited()
            assert KEY in mgr._sessions

    @pytest.mark.asyncio
    async def test_each_attempted_method_logs_its_outcome(self, cfg, caplog) -> None:
        cfg.session.compaction_method = "soft"
        async with _managed(cfg, _provider_factory(window=0)) as mgr:
            mgr.set_compaction_seed_writer(_Writer())
            await _live_session(mgr)
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                await mgr.compact_if_needed(KEY)
        messages = [rec.getMessage() for rec in caplog.records]
        assert any("compaction method soft: unavailable" in m for m in messages)
        assert any(
            "compaction method native: ok" in m for m in messages
        ), "native is an attempted method too and logs its outcome"

    @pytest.mark.asyncio
    async def test_an_unavailable_shake_hands_straight_to_native(self, cfg, caplog) -> None:
        """No window figure: shake cannot write a digest, and the walk does not
        try soft in its place (soft would drop the pre-tail history the operator
        asked to keep); native runs next."""
        cfg.session.compaction_method = "shake"
        async with _managed(cfg, _provider_factory(window=0)) as mgr:
            provider, _, _ = await _live_session(mgr)
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "ok"
        attempted = [
            m.split("compaction method ")[1].split(":")[0]
            for m in (rec.getMessage() for rec in caplog.records)
            if "compaction method " in m
        ]
        assert attempted == ["shake", "native"]
        provider.stream_command.assert_called_once_with("/compact")

    @pytest.mark.asyncio
    async def test_an_insufficient_shake_hands_straight_to_native(self, cfg, caplog) -> None:
        """Shake (tail + digest share) would not clear the threshold, so it steps
        aside, and native runs next: soft's smaller projection could fit, but
        soft would drop the pre-tail history undigested, the loss shake was
        chosen to avoid. The digest is never written."""
        cfg.session.compaction_method = "shake"
        cfg.session.autocompact_pct = 15.0
        writer = _Writer()
        async with _managed(cfg, _provider_factory(window=100_000)) as mgr:
            mgr.set_compaction_seed_writer(writer)
            provider, _, _ = await _live_session(mgr)
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "ok"
        attempted = [
            m.split("compaction method ")[1].split(":")[0]
            for m in (rec.getMessage() for rec in caplog.records)
            if "compaction method " in m
        ]
        assert attempted == ["shake", "native"]
        writer.write.assert_not_awaited()
        assert writer.asked == [KEY], "the surface was asked before the projection"
        provider.stream_command.assert_called_once_with("/compact")


class TestRotationVerdict:
    """A rotation is judged before it runs, by a projection; the successor's
    first confirmed reading is the check after the fact, through the same
    deferred verdict the native path uses."""

    @pytest.mark.asyncio
    async def test_a_rotation_arms_the_deferred_verdict(self, cfg) -> None:
        cfg.session.compaction_method = "soft"
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compaction_seed_writer(_Writer())
            await _live_session(mgr)
            assert await mgr.compact_if_needed(KEY) == "recycled"
            state = mgr._compaction.state
            assert state.pending_verdict[KEY] == 92.0, "the pre-rotation reading is the baseline"
            assert state.pending_rotation[KEY][0] == "soft"
            assert KEY not in state.rotation_hold

    @pytest.mark.asyncio
    async def test_an_effective_rotation_settles_clean_on_the_successor(self, cfg) -> None:
        """The successor starts well under the threshold: the verdict settles,
        no cooldown is armed, nothing is held."""
        cfg.session.compaction_method = "soft"
        async with _managed(cfg, _provider_factory(successor_pct=30.0)) as mgr:
            mgr.set_compaction_seed_writer(_Writer())
            await _live_session(mgr)
            assert await mgr.compact_if_needed(KEY) == "recycled"
            await _live_session(mgr)  # the successor's first turn
            assert await mgr.compact_if_needed(KEY) == "below_threshold"
            state = mgr._compaction.state
            assert KEY not in state.pending_verdict
            assert KEY not in state.pending_rotation
            assert KEY not in state.rotation_hold
            assert KEY not in state.cooldown_until

    @pytest.mark.asyncio
    async def test_an_ineffective_rotation_cools_down_then_runs_native(self, cfg, caplog) -> None:
        """The successor starts inside the band the projection said it would
        clear (the projection omits provider-side overhead the real figure
        includes). Rotating again would repeat the same arithmetic every turn,
        so the verdict arms the failure cooldown and the walk after it runs
        native outright, holding the key under the inputs the projection read."""
        cfg.session.compaction_method = "soft"
        async with _managed(cfg, _provider_factory(successor_pct=92.0)) as mgr:
            mgr.set_compaction_seed_writer(_Writer())
            await _live_session(mgr)
            assert await mgr.compact_if_needed(KEY) == "recycled"
            successor, _, spy = await _live_session(mgr)
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "cooldown"
            state = mgr._compaction.state
            assert KEY not in state.pending_verdict and KEY not in state.pending_rotation
            assert state.rotation_hold[KEY] == ("soft", 1_000_000, cfg.session.autocompact_pct)
            assert state.cooldown_until[KEY] > 0
            assert any(
                "rotation soft left context at 92.0%" in r.getMessage() for r in caplog.records
            )
            # The cooldown expires: the next compaction skips every rotation
            # method and compacts in place; the hold stays for the next crossing.
            state.cooldown_until.pop(KEY)
            caplog.clear()
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "ok"
            successor.stream_command.assert_called_once_with("/compact")
            successor.shutdown.assert_not_awaited()
            assert KEY in state.rotation_hold
            assert not spy.locked()
        attempted = [
            m.split("compaction method ")[1].split(":")[0]
            for m in (rec.getMessage() for rec in caplog.records)
            if "compaction method " in m
        ]
        assert attempted == ["native"], "the held walk tries no rotation"
        assert any("last rotation judged ineffective" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_the_hold_outlives_one_native_run(self, cfg) -> None:
        """The projection reads the method, the window and the threshold, never
        the session's content, so a later crossing under the same three would
        project the same and rotate into the same band: the key stays on native
        instead of alternating an ineffective rotation with native."""
        cfg.session.compaction_method = "soft"
        async with _managed(cfg, _provider_factory(successor_pct=92.0)) as mgr:
            mgr.set_compaction_seed_writer(_Writer())
            await _hold_after_an_ineffective_rotation(mgr)
            for _ in range(2):
                provider = _cross_again(mgr)
                assert await mgr.compact_if_needed(KEY) == "ok"
                provider.shutdown.assert_not_awaited()
            hold = mgr._compaction.state.rotation_hold[KEY]
            assert hold == ("soft", 1_000_000, cfg.session.autocompact_pct)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("change", ["method", "window", "threshold"])
    async def test_a_changed_input_releases_the_hold(self, cfg, change) -> None:
        """The hold names the inputs the ineffective projection read; new inputs
        get their own verdict, so the ladder is walked again."""
        cfg.session.compaction_method = "soft"
        async with _managed(cfg, _provider_factory(successor_pct=92.0)) as mgr:
            # One writer serves both rotations: soft reads it for the surface
            # question, and the ``method`` branch below rotates through shake.
            mgr.set_compaction_seed_writer(_Writer())
            await _hold_after_an_ineffective_rotation(mgr)
            provider = _cross_again(mgr)
            if change == "method":
                cfg.session.compaction_method = "shake"
            elif change == "window":
                provider.context_window_tokens.return_value = 2_000_000
            else:
                mgr.set_autocompact_pct(KEY, cfg.session.autocompact_pct + 10.0)
            assert await mgr.compact_if_needed(KEY) == "recycled"
            assert KEY not in mgr._compaction.state.rotation_hold

    @pytest.mark.asyncio
    async def test_the_hold_records_the_inputs_the_projection_read(self, cfg) -> None:
        """The method changes after a shake rotation but before its successor's
        first reading. The verdict judges the shake rotation, so the hold names
        shake: the soft now configured was never tried, and the next crossing
        walks it instead of running native outright."""
        cfg.session.compaction_method = "shake"
        async with _managed(cfg, _provider_factory(successor_pct=92.0)) as mgr:
            mgr.set_compaction_seed_writer(_Writer())
            await _live_session(mgr)
            assert await mgr.compact_if_needed(KEY) == "recycled"
            state = mgr._compaction.state
            judged = ("shake", 1_000_000, cfg.session.autocompact_pct)
            assert state.pending_rotation[KEY] == ("shake", judged)
            cfg.session.compaction_method = "soft"
            await _live_session(mgr)  # the successor's first turn
            assert await mgr.compact_if_needed(KEY) == "cooldown"
            assert state.rotation_hold[KEY] == judged
            _cross_again(mgr)
            assert await mgr.compact_if_needed(KEY) == "recycled", "soft is tried, not held"
            assert state.pending_rotation[KEY][0] == "soft"


async def _hold_after_an_ineffective_rotation(mgr: SessionManager) -> None:
    """Rotate once, then let the successor's first reading judge it ineffective."""
    await _live_session(mgr)
    assert await mgr.compact_if_needed(KEY) == "recycled"
    await _live_session(mgr)
    assert await mgr.compact_if_needed(KEY) == "cooldown"
    assert KEY in mgr._compaction.state.rotation_hold


def _cross_again(mgr: SessionManager) -> Any:
    """Expire the cooldown and put the live session back over the threshold.

    The provider reads 92% until its next in-place compaction completes, then
    an unconfirmed 0%, as ``_provider_factory`` does for a first crossing.
    """
    state = mgr._compaction.state
    state.cooldown_until.pop(KEY, None)
    state.pending_verdict.pop(KEY, None)
    provider = mgr._sessions[KEY].provider
    compacted = {"done": False}
    provider.context_usage_pct = lambda: 0.0 if compacted["done"] else 92.0
    provider.context_usage_unknown = lambda: compacted["done"]

    async def _wait(timeout=None):
        compacted["done"] = True
        return {"type": "completed"}

    provider.wait_for_compaction = AsyncMock(side_effect=_wait)
    return provider


class TestShake:
    @pytest.mark.asyncio
    async def test_shake_without_a_seed_writer_hands_straight_to_native(self, cfg) -> None:
        """No surface can write a digest here (channels register no writer), so
        the model call runs: the tail-only rotation would drop the pre-tail
        history undigested where users cannot see the loss."""
        cfg.session.compaction_method = "shake"
        async with _managed(cfg, _provider_factory()) as mgr:
            provider, _, _ = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once_with("/compact")
            provider.shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shake_without_a_seed_writer_is_unavailable_at_any_window(
        self, cfg, caplog
    ) -> None:
        """Availability is a property of the surface and is answered before the
        projection: at a window where the tail alone would not clear the band,
        a no-writer shake still says unavailable, so soft is skipped."""
        cfg.session.compaction_method = "shake"
        cfg.session.autocompact_pct = 15.0
        async with _managed(cfg, _provider_factory(window=100_000)) as mgr:
            provider, _, _ = await _live_session(mgr)
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "ok"
            provider.shutdown.assert_not_awaited()
            provider.stream_command.assert_called_once_with("/compact")
        attempted = [
            m.split("compaction method ")[1].split(":")[0]
            for m in (rec.getMessage() for rec in caplog.records)
            if "compaction method " in m
        ]
        assert attempted == ["shake", "native"]
        assert any(
            "compaction method shake: unavailable" in m
            for m in (rec.getMessage() for rec in caplog.records)
        )

    @pytest.mark.asyncio
    async def test_seed_nothing_recycles_as_soft_before_a_queued_turn(self, cfg) -> None:
        """``nothing`` permits a soft recycle without opening a turn race.

        The seed writer queues a turn while the semaphore is held. The
        tail-only recycle must finish before that turn enters, or the transcript
        cut the writer approved can move before the old session is removed.

        The callback hears ``soft`` (that is what ran, and what the counter and
        the verdict key on) together with ``all_kept``: the writer established
        that the tail covers every row, so the notice must say the whole
        conversation was carried rather than announce soft's loss.
        """
        cfg.session.compaction_method = "shake"
        calls, cb = _callback_recorder()
        entered_while_registered: list[bool] = []
        queued: asyncio.Task[None] | None = None
        writer = _Writer("nothing")
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            provider, session, spy = await _live_session(mgr)

            async def answer_nothing(*_args: object) -> str:
                nonlocal queued
                waiting = asyncio.Event()

                async def queued_turn() -> None:
                    waiting.set()
                    async with session.semaphore:
                        entered_while_registered.append(mgr._sessions.get(KEY) is session)

                queued = asyncio.create_task(queued_turn())
                await waiting.wait()
                return "nothing"

            writer.write.side_effect = answer_nothing
            mgr.set_compaction_seed_writer(writer)

            assert await mgr.compact_if_needed(KEY) == "recycled"
            assert queued is not None
            await queued

            writer.write.assert_awaited_once_with(KEY, "shake", 1_000_000)
            provider.stream_command.assert_not_called()
            provider.shutdown.assert_awaited_once()
            assert entered_while_registered == [False], "the queued turn enters after recycle"
            assert calls == [(KEY, 92.0, True, "recycled", "soft", True)]
            assert mgr._compaction.state.pending_rotation[KEY][0] == "soft"
            # The kept-all marker lives exactly as long as ``rotating`` does.
            assert KEY not in mgr._compaction.state.rotation_kept_all
            assert KEY not in mgr._compaction.state.rotating
            assert spy.acquires == 2, "one rotation hold plus the queued turn"
            assert not spy.locked()

    @staticmethod
    def _real_writer_state(monkeypatch, row_count: int) -> tuple[Any, MagicMock]:
        """A dashboard state holding *row_count* small conversation rows for ``KEY``'s tab."""
        rows = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"row-{i}"}
            for i in range(row_count)
        ]
        slot = SimpleNamespace(key="compaction-ladder", messages=[])
        saved = AsyncMock(return_value=True)
        state: Any = SimpleNamespace(
            get_slot=lambda name: slot if name == slot.key else None,
            conversation_log=SimpleNamespace(read_messages_chained=lambda key: list(rows)),
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_persistence.save_slot_off_loop", saved)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_utils.dashboard_slot_key", lambda key: slot.key
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_utils.slot_history_key", lambda slot: "hist")
        return state, saved

    @pytest.mark.asyncio
    async def test_real_writer_marks_a_transcript_that_fits_the_tail_kept(
        self, cfg, monkeypatch
    ) -> None:
        """Every admitted row fits the tail and the replay read dropped none: the
        rotation recycles as soft and tells the callback ``all_kept=True``."""
        cfg.session.compaction_method = "shake"
        state, saved = self._real_writer_state(monkeypatch, 3)
        calls, cb = _callback_recorder()
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            mgr.set_compaction_seed_writer(DashboardSeedWriter(state))
            provider, _, _ = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "recycled"

            provider.stream_command.assert_not_called()
            provider.shutdown.assert_awaited_once()
        assert saved.call_count == 0, "the admitted rows all fit the tail, so no seed is written"
        assert calls == [(KEY, 92.0, True, "recycled", "soft", True)]

    @pytest.mark.asyncio
    async def test_real_writer_hands_a_quota_truncated_transcript_to_native(
        self, cfg, monkeypatch
    ) -> None:
        """501 conversation rows: the replay read's row quota leaves the oldest out,
        so no digest can cover them and a soft recycle would drop them undigested.
        The session is never recycled; native summarizes in place and keeps them."""
        cfg.session.compaction_method = "shake"
        state, saved = self._real_writer_state(monkeypatch, 501)
        calls, cb = _callback_recorder()
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            mgr.set_compaction_seed_writer(DashboardSeedWriter(state))
            provider, _, _ = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once_with("/compact")
            provider.shutdown.assert_not_awaited()
            assert KEY not in mgr._compaction.state.pending_rotation
        assert saved.call_count == 0, "no seed is written for a truncated read"
        assert calls == [(KEY, 92.0, True, "compacted", "native", False)]

    @pytest.mark.asyncio
    async def test_a_surface_the_writer_does_not_support_hands_straight_to_native(
        self, cfg, caplog
    ) -> None:
        """The one registered writer holds no transcript for this key (a
        channel-born session, a key with no tab): asked BEFORE the projection,
        it answers ``supports`` False, the method is unavailable, soft is
        skipped and the write is never attempted. At this window the shake
        projection is insufficient, so this pin holds only if the surface
        question is asked first: an insufficient projection alone would walk to
        soft with the writer never asked."""
        cfg.session.compaction_method = "shake"
        cfg.session.autocompact_pct = 15.0
        writer = _Writer(supports=False)
        async with _managed(cfg, _provider_factory(window=100_000)) as mgr:
            mgr.set_compaction_seed_writer(writer)
            provider, _, spy = await _live_session(mgr)
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "ok"
            provider.stream_command.assert_called_once_with("/compact")
            provider.shutdown.assert_not_awaited()
            assert not spy.locked()
        assert writer.asked == [KEY]
        writer.write.assert_not_awaited()
        attempted = [
            m.split("compaction method ")[1].split(":")[0]
            for m in (rec.getMessage() for rec in caplog.records)
            if "compaction method " in m
        ]
        assert attempted == ["shake", "native"]

    @pytest.mark.asyncio
    async def test_a_surface_gone_at_write_time_hands_to_native_not_soft(self, cfg, caplog) -> None:
        """``supports`` said yes, then the tab closed under the rotation and the
        write answers ``unsupported``: no digest exists, so the walk skips soft
        (which would drop the pre-tail history undigested) and native keeps it."""
        cfg.session.compaction_method = "shake"
        writer = _Writer("unsupported")
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compaction_seed_writer(writer)
            provider, _, spy = await _live_session(mgr)
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "ok"
            writer.write.assert_awaited_once_with(KEY, "shake", 1_000_000)
            provider.stream_command.assert_called_once_with("/compact")
            provider.shutdown.assert_not_awaited()
            assert KEY in mgr._sessions, "no rotation ran, so the session was not recycled"
            assert not spy.locked()
        attempted = [
            m.split("compaction method ")[1].split(":")[0]
            for m in (rec.getMessage() for rec in caplog.records)
            if "compaction method " in m
        ]
        assert attempted == ["shake", "native"], "soft never ran"
        assert any("compaction method shake: unavailable" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_an_answer_the_contract_does_not_name_is_read_as_unavailable(self, cfg) -> None:
        """Only the named no-seed answers turn the rotation into soft. A writer that
        answers with a truthy stray value (a bare ``True``, a typo) has written
        no digest the coordinator can vouch for, so the fail-safe reading is
        the one that keeps the history: native."""
        cfg.session.compaction_method = "shake"
        writer = _Writer(True)  # type: ignore[arg-type]  # a bool, not a SEED_* answer
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compaction_seed_writer(writer)
            provider, _, spy = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once_with("/compact")
            provider.shutdown.assert_not_awaited()
            assert KEY in mgr._sessions, "no rotation ran, so the session was not recycled"
            assert not spy.locked()

    @pytest.mark.asyncio
    async def test_a_raising_rotation_method_still_reaches_native(self, cfg, caplog) -> None:
        # The promise is that a session at the threshold is always compacted:
        # a rotation that raises (an unreadable transcript under the writer)
        # is one more way of stepping aside, not the end of the walk.
        cfg.session.compaction_method = "shake"
        writer = _Writer(side_effect=OSError("transcript unreadable"))
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compaction_seed_writer(writer)
            provider, _, spy = await _live_session(mgr)
            with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
                # shake raises -> no digest could be written -> the walk skips
                # soft (it would drop the pre-tail history undigested) and
                # native, the last step, compacts in place.
                assert await mgr.compact_if_needed(KEY) == "ok"
            writer.write.assert_awaited_once()
            provider.stream_command.assert_called_once_with("/compact")
            provider.shutdown.assert_not_awaited()
            assert any(
                "compaction method shake raised" in r.getMessage() for r in caplog.records
            ), "the raise is logged, not swallowed"
            assert not spy.locked(), "the semaphore is released on the raising path"

    @pytest.mark.asyncio
    async def test_a_raise_in_every_rotation_step_ends_in_native(self, cfg, monkeypatch) -> None:
        cfg.session.compaction_method = "shake"
        async with _managed(cfg, _provider_factory()) as mgr:
            provider, session, spy = await _live_session(mgr)
            coordinator = mgr._compaction

            async def _boom(method, key, session, pct):
                raise RuntimeError(f"{method} exploded")

            monkeypatch.setattr(coordinator, "_rotate", _boom)
            in_place = AsyncMock(return_value="compacted")
            monkeypatch.setattr(mgr, "_compact_in_place", in_place)

            assert await mgr.compact_if_needed(KEY) == "compacted"

            in_place.assert_awaited_once()
            assert KEY in mgr._sessions, "no rotation ran, so the session was not recycled"

    @pytest.mark.asyncio
    async def test_shake_writes_the_seed_under_the_semaphore_then_recycles(self, cfg) -> None:
        cfg.session.compaction_method = "shake"
        calls, cb = _callback_recorder()
        held_during_write: list[bool] = []
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            provider, session, spy = await _live_session(mgr)

            class _Observing(_Writer):
                async def __call__(self, key: str, method: str, window_tokens: int) -> str:
                    held_during_write.append(spy.locked())
                    assert KEY in mgr._sessions, "the seed is written before the recycle"
                    return "written"

            mgr.set_compaction_seed_writer(_Observing())

            assert await mgr.compact_if_needed(KEY) == "recycled"

            assert held_during_write == [True]
            provider.stream_command.assert_not_called()
            provider.shutdown.assert_awaited_once()
            assert mgr._sessions[KEY] is not session
            assert mgr._sessions[KEY].first_turn.is_new
            assert spy.acquires == 1
            assert not spy.locked()
            # A digest was written, so the pre-tail history survives only as that
            # digest: the callback hears ``shake`` and never ``all_kept``.
            assert calls == [(KEY, 92.0, True, "recycled", "shake", False)]


class TestSeedWriterRegistration:
    @pytest.mark.asyncio
    async def test_replacing_a_registered_writer_warns(self, cfg, caplog) -> None:
        async with _managed(cfg, _provider_factory()) as mgr:
            first = _Writer()
            second = _Writer()
            mgr.set_compaction_seed_writer(first)
            with caplog.at_level("WARNING", logger="kiro_crew.session"):
                mgr.set_compaction_seed_writer(second)
            assert mgr._compaction.state.seed_writer is second
        assert any("seed writer already registered" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_clearing_the_writer_does_not_warn(self, cfg, caplog) -> None:
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compaction_seed_writer(_Writer())
            with caplog.at_level("WARNING", logger="kiro_crew.session"):
                mgr.set_compaction_seed_writer(None)
            assert mgr._compaction.state.seed_writer is None
        assert not [r for r in caplog.records if "seed writer" in r.getMessage()]


@pytest.mark.asyncio
async def test_a_seedable_quota_cut_read_hands_to_native(cfg, monkeypatch) -> None:
    from kiro_crew.context import rotation_rows
    from kiro_crew.dashboard import compaction_seed

    cfg.session.compaction_method = "shake"
    rows = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"row-{i} " + ("x" * 500),
        }
        for i in range(501)
    ]
    appended = MagicMock()
    saved = AsyncMock(return_value=True)
    slot = SimpleNamespace(key="compaction-ladder", messages=[], append=appended)
    conversation_log = SimpleNamespace(read_messages_chained=lambda key: list(rows))
    state = SimpleNamespace(
        get_slot=lambda name: slot if name == slot.key else None,
        conversation_log=conversation_log,
    )
    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence.save_slot_off_loop", saved)
    monkeypatch.setattr("kiro_crew.dashboard.chat_utils.dashboard_slot_key", lambda key: slot.key)
    monkeypatch.setattr("kiro_crew.dashboard.chat_utils.slot_history_key", lambda slot: "hist")
    admitted = rotation_rows(conversation_log, "hist", pending_messages=[])
    assert admitted.conversation_quota_cut is True
    assert compaction_seed.build_seed(admitted, window_tokens=1_000_000, method="shake")[0]

    async with _managed(cfg, _provider_factory()) as mgr:
        mgr.set_compaction_seed_writer(DashboardSeedWriter(state))
        provider, _, _ = await _live_session(mgr)

        assert await mgr.compact_if_needed(KEY) == "ok"

        provider.stream_command.assert_called_once_with("/compact")
        provider.shutdown.assert_not_awaited()
        assert KEY not in mgr._compaction.state.pending_rotation
    appended.assert_not_called()
    saved.assert_not_called()


@pytest.mark.asyncio
async def test_cancellation_finishes_seed_save_and_rotation_before_releasing(
    cfg, monkeypatch
) -> None:
    import threading

    cfg.session.compaction_method = "shake"
    rows = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"row-{i} " + ("x" * 500),
        }
        for i in range(300)
    ]
    messages: list[dict[str, Any]] = []
    save_started = threading.Event()
    save_finished = threading.Event()
    finish_save = threading.Event()

    def append(role, content, cls="", ts="", *, broadcast=True, meta=None, **kwargs):
        row = {"role": role, "content": content, "meta": meta}
        messages.append(row)
        return row

    def withdraw(row) -> bool:
        messages.remove(row)
        return True

    slot = SimpleNamespace(
        key="compaction-ladder",
        messages=messages,
        append=append,
        withdraw=withdraw,
    )

    async def save_slot_off_loop(state, target, messages=None, **_kwargs) -> bool:
        save_started.set()
        if not await asyncio.to_thread(finish_save.wait, 5.0):
            raise AssertionError("the seed-save release handshake did not arrive")
        save_finished.set()
        return True

    state = SimpleNamespace(
        get_slot=lambda name: slot if name == slot.key else None,
        conversation_log=SimpleNamespace(read_messages_chained=lambda key: list(rows)),
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_persistence.save_slot_off_loop", save_slot_off_loop
    )
    monkeypatch.setattr("kiro_crew.dashboard.chat_utils.dashboard_slot_key", lambda key: slot.key)
    monkeypatch.setattr("kiro_crew.dashboard.chat_utils.slot_history_key", lambda slot: "hist")

    async with _managed(cfg, _provider_factory()) as mgr:
        mgr.set_compaction_seed_writer(DashboardSeedWriter(state))
        provider, _, spy = await _live_session(mgr)
        before = set(mgr._background_tasks)
        assert mgr._trigger_compaction(KEY, "context at 92%", 92.0, provider) is None
        created = mgr._background_tasks - before
        assert len(created) == 1
        compaction = created.pop()
        try:
            assert await asyncio.to_thread(
                save_started.wait, 2.0
            ), "the compaction did not enter the seed save"
            compaction.cancel()
            await asyncio.sleep(0)
            assert not compaction.done(), "cancellation must wait for the transaction"
            assert spy.locked(), "the semaphore stays held while the seed save is in flight"
            provider.shutdown.assert_not_awaited()
        finally:
            finish_save.set()

        with pytest.raises(asyncio.CancelledError):
            await compaction

        assert save_finished.is_set()
        provider.shutdown.assert_awaited_once()
        compaction_state = mgr._compaction.state
        assert compaction_state.pending_verdict[KEY] == 92.0
        assert compaction_state.pending_rotation[KEY][0] == "shake"
        assert not spy.locked(), "the semaphore is released after the transaction"
