"""A lesson's write origin is visible wherever lessons are listed.

A lesson the user saved and one the extractor inferred from chat history carry
different authority, so both the agent's ``learn_list`` tool and the dashboard's
``GET /api/lessons`` payload report each row's ``source``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.vector_memory import VectorMemoryStore


def test_learn_list_shows_each_lessons_source(monkeypatch):
    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools import learn as mcp_learn

    monkeypatch.setattr(
        mcp_core,
        "_get",
        lambda *a, **k: {
            "lessons": [
                {"category": "preference", "rule": "use tabs", "source": "user_explicit"},
                {"category": "knowledge", "rule": "cache needs a flush", "source": "consolidation"},
                {"category": "knowledge", "rule": "row with no origin"},
            ]
        },
    )
    out = mcp_learn.learn_list("learn_list", {})
    lines = out.splitlines()
    assert lines[0] == "[preference] use tabs (source: user_explicit)"
    assert lines[1] == "[knowledge] cache needs a flush (source: consolidation)"
    # A row whose store recorded no origin renders bare rather than inventing one.
    assert lines[2] == "[knowledge] row with no origin"


def test_source_suffix_ignores_blank_and_non_string_values():
    from kiro_crew.mcp_tools.learn import _source_suffix

    assert _source_suffix({"source": "  "}) == ""
    assert _source_suffix({"source": None}) == ""
    assert _source_suffix({"source": 3}) == ""
    assert _source_suffix({}) == ""


@pytest.mark.asyncio
async def test_lessons_api_reports_the_vector_rows_source(tmp_path):
    from kiro_crew.dashboard.handlers import cron

    store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
    store.init()
    try:
        assert (
            store.set_semantic(
                "lesson.userrow",
                {"rule": "always run the formatter", "category": "preference"},
                1.0,
                "user_explicit",
            )
            is None
        )
        request = MagicMock()
        state = MagicMock()
        request.app = {"state": state}
        request.headers = {"X-Session-Key": "dashboard:ui"}
        request.query = {}
        with (
            patch.object(cron, "_blocks_reads_session", return_value=False),
            patch.object(
                cron,
                "resolve_lesson_memory_store",
                new=AsyncMock(return_value=(None, None)),
            ),
            patch.object(
                cron,
                "_prepare_member_lesson_store",
                new=AsyncMock(return_value=None),
            ),
            patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=store)),
        ):
            resp = await cron.api_lessons(request)

        body = json.loads(resp.text)
        assert body["lessons"][0]["rule"] == "always run the formatter"
        assert body["lessons"][0]["source"] == "user_explicit"
    finally:
        store.close()
