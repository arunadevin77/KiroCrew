"""The composer's budget cuts are recorded from the decision that made them.

Each budgeted block writes an omission notice into the prompt when it cuts. These
cases pin the structured twin of that notice: the same decision records the same
numbers into :func:`kiro_crew.context_omissions.collect`, nothing is recorded when
nothing is cut or when no composition is collecting, and the record stays inside
its closed vocabulary however it is fed.
"""

from __future__ import annotations

import asyncio
import json
import re
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state, drain_background_tasks

from kiro_crew import context_omissions
from kiro_crew.context import ContextBuilder
from kiro_crew.context_assembly import budget as _budgets
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.dashboard import chat_runner
from kiro_crew.learn import Lesson, LessonStore
from kiro_crew.lesson_validation import render_lesson_tier, render_withheld_tier
from kiro_crew.memory import MemoryStore
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.skills import SkillsLoader

_RULE_NOTICE = re.compile(r"omitted (\d+) of (\d+) retained rules above the (\d+)-character")


def _rule(marker: str, size: int = 400) -> str:
    filler = f"{marker.lower()}pad "
    return (f"{marker} " + (filler * (size // len(filler) + 1))[:size]).strip()


def _store(tmp_path, count: int) -> LessonStore:
    store = LessonStore(base_dir=tmp_path)
    for i in range(count):
        store.save(Lesson(ts=str(i), rule=_rule(f"R{i}"), category="preference", applies="always"))
    return store


def _tier(budget: int, rows: int = 10) -> tuple[str, int]:
    entries = [(i, f"rule number {i} " + "x" * 80) for i in range(rows)]
    return render_lesson_tier(
        entries,
        budget,
        header="[H]",
        footer="[F]",
        omission="[omitted {count} of {total} over {limit}]",
        omission_kind=context_omissions.LESSON_RULES,
    )


class TestTheRecordStatesTheNoticesNumbers:
    def test_an_over_budget_rule_tier_records_the_counts_its_notice_states(self, tmp_path):
        store = _store(tmp_path, 60)
        with context_omissions.collect() as cut:
            out = store.get_context(cap=99_000, directive_budget=7_000)
        match = _RULE_NOTICE.search(out)
        assert match, "the fixture must overflow the rule budget"
        omitted, total, limit = (int(g) for g in match.groups())
        assert cut == {
            context_omissions.LESSON_RULES: {
                "reason": "budget",
                "omitted": omitted,
                "total": total,
                "budget_chars": limit,
            }
        }

    def test_an_under_budget_composition_records_nothing(self, tmp_path):
        store = _store(tmp_path, 3)
        with context_omissions.collect() as cut:
            out = store.get_context(cap=99_000, directive_budget=7_000)
        assert "[Context budget" not in out
        assert cut == {}

    def test_a_render_outside_a_collection_records_nowhere(self):
        block, omitted = _tier(300)
        assert omitted > 0
        with context_omissions.collect() as cut:
            pass
        assert cut == {}

    def test_the_last_render_of_a_tier_is_the_one_recorded(self):
        # Lessons are re-fitted when protected content overruns its ceiling, so one
        # composition can render a tier twice; the prompt carries the second.
        with context_omissions.collect() as cut:
            _tier(600)
            _, omitted = _tier(300)
        assert cut[context_omissions.LESSON_RULES]["omitted"] == omitted
        with context_omissions.collect() as cut:
            _tier(300)
            _tier(0)
        assert cut == {}, "a final render that kept everything clears the earlier cut"

    def test_a_withheld_findings_tier_records_relevance(self):
        with context_omissions.collect() as cut:
            render_withheld_tier(
                7,
                header="[H]",
                footer="[F]",
                notice="[withheld {total}]",
                omission_kind=context_omissions.LESSON_FINDINGS,
            )
        assert cut == {
            context_omissions.LESSON_FINDINGS: {"reason": "relevance", "omitted": 7, "total": 7}
        }

    def test_the_background_pool_records_the_sections_it_left_out(self):
        blocks = _budgets.ContextParts()
        blocks.parts.append("[Skills:]\n" + "s" * 500 + "\n")
        blocks.parts.append("older activity " + "a" * 500 + "\n")
        caps = _budgets._resolve_caps_cached(_budgets._effective_window(None))
        with context_omissions.collect() as cut:
            out = _budgets.admit_background(
                blocks, protected_chars=0, max_context_chars=100, caps=caps, compressed_history=None
            )
        assert "[Context budget: omitted background context, skills;" in out
        assert cut == {
            context_omissions.BACKGROUND: {
                "reason": "budget",
                "sections": ["background_context", "skills"],
            }
        }


class TestTheRecordIsBounded:
    def test_an_unknown_kind_or_reason_is_a_programming_error(self):
        with pytest.raises(ValueError):
            context_omissions.record("lessons", "budget", omitted=1)
        with pytest.raises(ValueError):
            context_omissions.record(context_omissions.LESSON_RULES, "space", omitted=1)

    def test_the_written_record_holds_only_the_declared_vocabulary(self):
        raw = {
            context_omissions.LESSON_RULES: {
                "reason": "budget",
                "omitted": 3,
                "total": 9,
                "rule_text": "never copied",
                "sections": ["skills", "a stored key"],
            },
            "unknown_kind": {"reason": "budget", "omitted": 1},
            context_omissions.BACKGROUND: {"reason": "not-a-reason"},
            context_omissions.PREFERENCES_FILE: {"reason": "cap", "omitted_chars": True},
        }
        assert context_omissions.as_record(raw) == {
            context_omissions.LESSON_RULES: {
                "reason": "budget",
                "omitted": 3,
                "total": 9,
                "sections": ["skills"],
            },
            context_omissions.PREFERENCES_FILE: {"reason": "cap"},
        }

    def test_a_record_with_every_kind_cut_stays_small(self):
        with context_omissions.collect() as cut:
            for kind in context_omissions.KINDS:
                context_omissions.record(
                    kind,
                    "budget",
                    omitted=10**9,
                    total=10**9,
                    budget_chars=10**9,
                    omitted_chars=10**9,
                    sections=list(context_omissions.BACKGROUND_SECTIONS),
                )
        written = json.dumps(context_omissions.as_record(cut), separators=(",", ":"))
        assert len(written) < 2_048

    def test_the_activity_summary_names_each_cut(self):
        summary = context_omissions.summary(
            {
                context_omissions.LESSON_RULES: {"reason": "budget", "omitted": 23, "total": 92},
                context_omissions.BACKGROUND: {"reason": "budget", "sections": ["skills"]},
                context_omissions.MEMBER_BRIEFING: {"reason": "cap", "budget_chars": 4000},
            }
        )
        assert summary == "lesson rules 23 of 92, background (skills), member briefing"
        assert context_omissions.summary({}) == ""


class TestTheTurnWritesTheRecord:
    """The runner gathers the cuts around the build and writes them with the turn."""

    @pytest.mark.asyncio
    async def test_a_cut_made_while_building_reaches_the_entry_and_the_activity_line(
        self, tmp_path, monkeypatch
    ):
        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "workspace"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        state = _make_state(tmp_path, context_builder=builder)

        def build(message, *_args, **_kwargs):
            # Runs on the composer's worker thread, as the real build does.
            context_omissions.record(
                context_omissions.LESSON_RULES, "budget", omitted=3, total=9, budget_chars=500
            )
            return (
                "[Learned corrections — rules]\n- r\n[End of learned corrections]\n\n" + message,
                None,
            )

        state.context_builder.build_message = MagicMock(side_effect=build)
        state.context_builder.ensure_store = AsyncMock(return_value=object())
        provider = MagicMock()

        async def stream(*_args, **_kwargs):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Done.")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider.stream = stream
        state.sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        state.sessions.consume_replay_suppression = MagicMock(return_value=False)
        state.sessions.consume_needs_reinjection = MagicMock(return_value=False)
        state.sessions.record_failure = AsyncMock()
        state.broadcast_ws = MagicMock()
        composed = MagicMock()
        monkeypatch.setattr(crew_log_emit, "on_context_composed", composed)
        monkeypatch.setattr(chat_runner, "title_then_refresh", AsyncMock())
        monkeypatch.setattr(chat_runner, "generate_session_summary", AsyncMock())

        slot = state.get_or_create_slot("omissions-chat")
        slot.append("user", "plan it")
        await asyncio.wait_for(chat_runner._run_chat(state, slot, "plan it"), 30)
        await asyncio.wait_for(drain_background_tasks(state), 10)

        composed.assert_called_once()
        assert composed.call_args.kwargs["omitted"] == {
            context_omissions.LESSON_RULES: {
                "reason": "budget",
                "omitted": 3,
                "total": 9,
                "budget_chars": 500,
            }
        }
        texts = [
            call.args[1].get("text", "")
            for call in state.broadcast_ws.call_args_list
            if call.args and call.args[0] == "activity_event"
        ]
        assert any(text.endswith("; budget omitted lesson rules 3 of 9") for text in texts), texts
