"""A scheduled job's intent summary stays inside the owner boundary.

A summary restates the transcript it came from, and a ``cron:`` transcript can
carry a project-bound run's output, which is owner-only. Like History's
summaries it cannot be filtered per row, so a non-owner reads none and cannot
generate one -- under the job's own ``cron-<id>`` tab and under any other name a
History resume gives the same transcript.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.config.loader import KiroCrewConfig, SessionSummaryConfig
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat import api_chat_slot_summary, api_chat_slot_summary_generate
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.state import _ChatSlot

pytestmark = pytest.mark.asyncio

OWNER = "U-owner"
SECRET = "rotate the deploy key in the private repo"

# The job's own tab, and a History resume under another name that keeps the
# job's transcript as its link.
_CRON_SLOTS = [("cron-abcd1234", ""), ("report", "cron:abcd1234")]


@pytest.fixture(autouse=True)
def _summaries_on(_floor_monkeypatch):
    def _load():
        cfg = KiroCrewConfig()
        cfg.session_summary = SessionSummaryConfig(enabled=True)
        return cfg

    _floor_monkeypatch.setattr(chat_handlers.KiroCrewConfig, "load", staticmethod(_load))


def _app(state, caller: str) -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def dashboard_user(request, handler):
        request["app"] = ""
        request["user"] = caller
        return await handler(request)

    app.middlewares.append(dashboard_user)
    app.router.add_get("/api/chat/slots/{slot}/summary", api_chat_slot_summary)
    app.router.add_post("/api/chat/slots/{slot}/summary", api_chat_slot_summary_generate)
    return app


def _slot_with_summary(state, key: str, linked: str) -> _ChatSlot:
    slot = _ChatSlot(key)
    slot.linked_session_key = linked
    state._slots[slot.key] = slot
    hkey = slot_history_key(slot)
    log = state.conversation_log
    log.append(hkey, "user", "hello")
    log.set_cached_intent_summary(
        hkey,
        {
            "intents": [{"title": SECRET, "ranges": [[1, 1]], "status": "completed"}],
            "constraints": [SECRET],
            "generated_at": 1_760_000_000.0,
            "user_turns": 1,
            "last_activity": "2026-10-14T10:00:00+00:00",
        },
        log.session_mtime(hkey),
    )
    return slot


def _state(tmp_path):
    state = _make_state(tmp_path)
    state.owner_id = OWNER
    return state


@pytest.mark.parametrize("key, linked", _CRON_SLOTS)
async def test_a_non_owner_reads_no_summary_of_a_cron_transcript(tmp_path, key, linked):
    state = _state(tmp_path)
    _slot_with_summary(state, key, linked)
    async with TestClient(TestServer(_app(state, "U-someone-else"))) as client:
        resp = await client.get(f"/api/chat/slots/{key}/summary")
        assert resp.status == 200
        text = await resp.text()
    body = await resp.json()
    assert SECRET not in text
    assert body["intents"] == [] and body["constraints"] == []
    assert body["generate_state"] == "unavailable"


@pytest.mark.parametrize("key, linked", _CRON_SLOTS)
async def test_a_non_owner_cannot_generate_one(tmp_path, key, linked):
    state = _state(tmp_path)
    _slot_with_summary(state, key, linked)
    async with TestClient(TestServer(_app(state, "U-someone-else"))) as client:
        resp = await client.post(f"/api/chat/slots/{key}/summary")
        assert resp.status == 403
        body = await resp.json()
    assert body["code"] == "summary_owner_only"
    assert SECRET not in str(body)


@pytest.mark.parametrize("key, linked", _CRON_SLOTS)
async def test_the_owner_still_reads_it(tmp_path, key, linked):
    state = _state(tmp_path)
    _slot_with_summary(state, key, linked)
    async with TestClient(TestServer(_app(state, OWNER))) as client:
        body = await (await client.get(f"/api/chat/slots/{key}/summary")).json()
    assert body["intents"][0]["title"] == SECRET


async def test_an_ordinary_chat_slot_is_unchanged_for_a_non_owner(tmp_path):
    state = _state(tmp_path)
    _slot_with_summary(state, "chat-1", "")
    async with TestClient(TestServer(_app(state, "U-someone-else"))) as client:
        body = await (await client.get("/api/chat/slots/chat-1/summary")).json()
    assert body["intents"][0]["title"] == SECRET
