"""A non-owner cannot copy a project-bound cron transcript out of its slot.

Slot detail serves a non-owner only the ``cron:{id}`` rows
``_non_owner_transcript_rows`` can prove unbound. Export and fork carry the WHOLE
transcript out, so :func:`non_owner_cron_transcript_copy_refusal` allows a
non-owner copy only when that filter keeps every row. These tests pin the rule
and that both copy routes consult it before any copy is built.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.cron import non_owner_cron_transcript_copy_refusal

_OWNER = "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request"
_ROWS = "kiro_crew.dashboard.handlers.cron._non_owner_transcript_rows"
_KEY = "kiro_crew.dashboard.chat_utils.slot_history_key"
_TAIL = "kiro_crew.dashboard.chat_handlers._append_unflushed_tail"
_SNAP = "kiro_crew.dashboard.chat_handlers._snapshot_slot_window"
_GUARD = "kiro_crew.dashboard.handlers.cron.non_owner_cron_transcript_copy_refusal"

_TRANSCRIPT = [
    {"role": "assistant", "content": "a", "ts": "t1"},
    {"role": "assistant", "content": "b", "ts": "t2"},
]


def _state(rows=_TRANSCRIPT, *, read_fails=False):
    log = MagicMock()
    if read_fails:
        log.read_messages_chained_full.side_effect = OSError("unreadable")
    else:
        log.read_messages_chained_full.return_value = list(rows)
    return SimpleNamespace(conversation_log=log)


async def _decide(*, owner=False, key="cron:abcd1234", state=None, kept=_TRANSCRIPT):
    state = state or _state()
    with (
        patch(_OWNER, lambda request: owner),
        patch(_KEY, lambda slot: key),
        patch(_SNAP, lambda slot: (0, [])),
        patch(_TAIL, lambda slot, rows, snapshot=None: rows),
        patch(_ROWS, AsyncMock(return_value=(list(kept), True))) as rows_filter,
        patch("kiro_crew.dashboard.handlers.cron._sel"),
    ):
        verdict = await non_owner_cron_transcript_copy_refusal(
            MagicMock(get=lambda *_a: ""),
            state,
            # Identity is the slot's linked transcript key, whatever its name.
            SimpleNamespace(key="report", linked_session_key=key),
            "chat.slot_export",
        )
    return verdict, rows_filter


class TestTheCopyRule:
    @pytest.mark.asyncio
    async def test_a_row_the_filter_withholds_refuses_the_copy(self):
        verdict, _ = await _decide(kept=_TRANSCRIPT[:1])

        assert verdict is not None and verdict.status == 403
        assert b"cron_transcript_owner_only" in verdict.body

    @pytest.mark.asyncio
    async def test_a_transcript_the_filter_keeps_whole_may_be_copied(self):
        verdict, rows_filter = await _decide(kept=_TRANSCRIPT)

        assert verdict is None
        assert rows_filter.await_args.args[1:] == ("abcd1234", _TRANSCRIPT)

    @pytest.mark.asyncio
    async def test_an_unreadable_transcript_refuses_rather_than_proving_nothing(self):
        verdict, rows_filter = await _decide(state=_state(read_fails=True))

        assert verdict is not None and verdict.status == 403
        rows_filter.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_owner_is_not_checked(self):
        state = _state()
        verdict, rows_filter = await _decide(owner=True, state=state, kept=[])

        assert verdict is None
        rows_filter.assert_not_awaited()
        state.conversation_log.read_messages_chained_full.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_slot_that_is_not_a_cron_transcript_is_not_checked(self):
        verdict, rows_filter = await _decide(key="dashboard:chat-1", kept=[])

        assert verdict is None
        rows_filter.assert_not_awaited()


def _refusal():
    return web.json_response({"code": "cron_transcript_owner_only"}, status=403)


class TestBothCopyRoutesConsultTheRule:
    @pytest.mark.asyncio
    async def test_export_refuses_before_building_a_bundle(self):
        from kiro_crew.dashboard import session_export

        slot = MagicMock(memory_mode="persistent", linked_session_key="")
        state = MagicMock(_slots={"cron-abcd1234": slot})
        app = web.Application()
        app["state"] = state
        app.router.add_get("/api/chat/slots/{slot}/export", session_export.api_chat_slot_export)
        with (
            patch(_GUARD, AsyncMock(return_value=_refusal())) as guard,
            patch.object(session_export, "build_transfer_bundle_async", AsyncMock()) as build,
            patch.object(session_export, "sel"),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/chat/slots/cron-abcd1234/export")
                assert resp.status == 403
        assert guard.await_args.args[3] == "chat.slot_export"
        build.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fork_refuses_before_reading_the_parent(self):
        from kiro_crew.dashboard import chat_fork

        slot = MagicMock()
        state = MagicMock(_slots={"cron-abcd1234": slot})
        state.live_slot_count.return_value = 0
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/fork", chat_fork.api_chat_slot_fork)
        with (
            patch(_GUARD, AsyncMock(return_value=_refusal())) as guard,
            patch.object(chat_fork, "resolve_fork_source", AsyncMock()) as source,
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/chat/slots/cron-abcd1234/fork", json={})
                assert resp.status == 403
        assert guard.await_args.args[3] == "chat.slot_fork"
        source.assert_not_awaited()


# ── the rows actually copied ──
#
# The early read above runs before the copy builds its own snapshot, and a bound
# run can append in between. Each route therefore also judges the exact rows it
# copies. These tests let the early read pass and then make the copied snapshot
# carry a row the filter withholds.

_LATE = {"role": "assistant", "content": "bound output", "ts": "t3"}


def _filter_by_call(*verdicts):
    """A rows filter whose Nth call keeps ``verdicts[N](rows)``."""
    calls = iter(verdicts)

    async def _filter(_state, _job_id, rows):
        return next(calls)(rows), True

    return AsyncMock(side_effect=_filter)


def _keep_all(rows):
    return list(rows)


def _drop_late(rows):
    return [r for r in rows if r is not _LATE and r.get("content") != _LATE["content"]]


class TestExportJudgesTheBundleItBuilt:
    async def _export(self, rows_filter, *, owner=False, bundle_rows=(_TRANSCRIPT[0], _LATE)):
        from test_session_export import MSGS, _request, _slot
        from test_session_export import _state as _export_state

        from kiro_crew.dashboard import session_export
        from kiro_crew.dashboard.session_transfer import TransferBundle

        slot = _slot(MSGS, key="cron-abcd1234")
        state = _export_state(MSGS, slots={"cron-abcd1234": slot})
        # The early read sees only the first row; the bundle is built later.
        state.conversation_log.read_messages_chained_full = lambda key: [_TRANSCRIPT[0]]
        bundle = TransferBundle(
            {"bundle_version": 2, "title": "t", "agent": "", "messages": list(MSGS)},
            publication_keys=(),
        )
        bundle.source_rows = tuple(bundle_rows)
        with (
            patch(_OWNER, lambda request: owner),
            patch(_SNAP, lambda slot: (0, [])),
            patch(_TAIL, lambda slot, rows, snapshot=None: rows),
            patch(_ROWS, rows_filter),
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch.object(
                session_export, "build_transfer_bundle_async", AsyncMock(return_value=bundle)
            ),
            patch.object(session_export, "release_bundle_files") as release,
        ):
            resp = await session_export.api_chat_slot_export(_request(state, "cron-abcd1234"))
        return resp, release

    @pytest.mark.asyncio
    async def test_a_row_appended_after_the_early_check_refuses_the_download(self):
        rows_filter = _filter_by_call(_keep_all, _drop_late)

        resp, release = await self._export(rows_filter)

        assert resp.status == 403
        assert b"cron_transcript_owner_only" in resp.body
        # The second call judged the bundle's own rows, not a fresh read.
        assert rows_filter.await_args_list[1].args[2] == [_TRANSCRIPT[0], _LATE]
        release.assert_called()

    @pytest.mark.asyncio
    async def test_a_bundle_whose_rows_all_pass_is_exported(self):
        resp, _ = await self._export(_filter_by_call(_keep_all, _keep_all))

        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_the_owner_is_not_judged_on_the_bundle_either(self):
        rows_filter = _filter_by_call()

        resp, _ = await self._export(rows_filter, owner=True)

        assert resp.status == 200
        rows_filter.assert_not_awaited()


class TestTheCopiedRowsCheck:
    @pytest.mark.asyncio
    async def test_a_copy_that_cannot_name_its_rows_refuses(self):
        from kiro_crew.dashboard.handlers.cron import non_owner_cron_copied_rows_refusal

        with (
            patch(_OWNER, lambda request: False),
            patch(_KEY, lambda slot: "cron:abcd1234"),
            patch(_ROWS, AsyncMock()) as rows_filter,
            patch("kiro_crew.dashboard.handlers.cron._sel"),
        ):
            verdict = await non_owner_cron_copied_rows_refusal(
                MagicMock(get=lambda *_a: ""),
                SimpleNamespace(),
                SimpleNamespace(key="report", linked_session_key="cron:abcd1234"),
                "chat.slot_export",
                None,
            )

        assert verdict is not None and verdict.status == 403
        rows_filter.assert_not_awaited()


class TestForkJudgesTheRowsItCopies:
    async def _fork(self, tmp_path, monkeypatch, rows_filter, payload):
        from chat_test_helpers import _make_app, _make_state

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("forkable")
        slot.append("user", "hello", "msg msg-u", ts="t1")
        slot.append("assistant", "bound output", "msg msg-a", ts="t3")
        slot._resumed_count = len(slot.messages)
        slot._disk_window_len = len(slot.messages)
        slot._dirty = False
        before = set(state._slots)
        with (
            patch(_OWNER, lambda request: False),
            patch(
                "kiro_crew.dashboard.state.cron_slot_job_id",
                lambda s: "abcd1234" if getattr(s, "key", "") == "forkable" else "",
            ),
            patch(_ROWS, rows_filter),
            patch("kiro_crew.dashboard.handlers.cron._sel"),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/forkable/fork", json=payload)
                body = await resp.json()
        return resp.status, body, set(state._slots) - before

    @pytest.mark.asyncio
    async def test_a_row_the_copy_holds_but_the_early_read_missed_refuses_the_fork(
        self, tmp_path, monkeypatch
    ):
        status, body, minted = await self._fork(
            tmp_path, monkeypatch, _filter_by_call(_keep_all, _drop_late), {}
        )

        assert status == 403
        assert body["code"] == "cron_transcript_owner_only"
        assert minted == set()

    @pytest.mark.asyncio
    async def test_the_rows_judged_are_the_rows_the_fork_point_keeps(self, tmp_path, monkeypatch):
        rows_filter = _filter_by_call(_keep_all, _keep_all)

        status, _body, minted = await self._fork(
            tmp_path, monkeypatch, rows_filter, {"at_message_index": 0}
        )

        assert status == 200
        assert len(minted) == 1
        judged = rows_filter.await_args_list[1].args[2]
        assert [m["content"] for m in judged] == ["hello"]
