"""The transcript side of the ``shake`` compaction method, for dashboard sessions.

The compaction coordinator owns no transcript, so it hands ``shake`` to the
surface through ``SessionManager.set_compaction_seed_writer``: this module reads
the rows the successor's replay would admit, splits them at the recent tail,
digests the dropped slice with large fenced blocks elided, and leaves the digest
as a ``compaction`` row in the slot's live window, written to disk before the
coordinator hears of it (a guarded ``chat_persistence.save_slot_off_loop``,
which a tab close waits for): the answer lets
the native conversation be dropped, so the row must survive a restart first.
The successor's first prompt merges the live window with the disk transcript,
so ``build_session_replay`` sees the seed either way.

The writer runs with the session's turn semaphore held (the coordinator takes
it first), so the rows it reads are quiescent. It answers the coordinator in
four words: ``written``; ``nothing`` when the complete conversation fits the
tail; ``tail_only`` when replay admission cut the conversation at its quota or
when no digest was written but rows fall outside that tail; or ``unsupported``
when no transcript lives here for this key. Only ``nothing`` recycles as
``soft`` under the same hold. ``tail_only`` hands to ``native`` because the
writer cannot account for every conversation row. The coordinator asks
``supports`` first, before it projects either rotation, so a channel-born
session or a key with no tab hands straight to ``native``: no transcript here
means nothing would re-seed a ``soft`` successor either.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from kiro_crew.autonudge import is_channel_key
from kiro_crew.context import replay_walk_options, rotation_rows
from kiro_crew.session_compaction_methods import (
    SEED_BUDGET_DIVISOR,
    SEED_META_KIND,
    SEED_NOTHING,
    SEED_ROLE,
    SEED_TAIL_ONLY,
    SEED_UNSUPPORTED,
    SEED_WRITTEN,
    shake_elide,
    split_tail,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)


def build_seed(
    rows: Sequence[Mapping[str, Any]], *, window_tokens: int | None, method: str
) -> tuple[tuple[str, dict[str, Any]] | None, bool]:
    """The seed row's ``(content, meta)`` for *rows* (or ``None``), and whether the split dropped a row.

    *rows* are chronological and already admitted by the replay's own rules
    (``context.rotation_rows``). The conversation rows are split with the
    replay's own walk options for a *window_tokens* model
    (``context.replay_walk_options``: budget, inject clipping, inject reserve),
    so the tail left alone is exactly the tail the successor's replay carries;
    everything older is digested into at most a ``SEED_BUDGET_DIVISOR``-th of
    that same budget. The digest names the exact row it covers through
    (``through_row``), not just its stamp.

    An earlier seed row is not split with them. The replay renders the newest
    seed on its own share and walks only the conversation, so the split does the
    same; and when a new digest is written, the earlier one is folded into it
    first, wherever it sorts (it was appended after the rows it kept, so it sorts
    among them, and a new seed makes the replay skip it). The seed is ``None``
    when the tail keeps every conversation row (an earlier seed then stays the
    newest and goes on standing in for what it covers), or when the dropped rows
    render no digest text; the flag tells those two apart.
    """
    options = replay_walk_options(window_tokens)
    budget = options["budget_chars"]
    prior = [row for row in rows if str(row.get("role", "")) == SEED_ROLE][-1:]
    conversation = [row for row in rows if str(row.get("role", "")) != SEED_ROLE]
    split = split_tail(conversation, **options)
    if not split.dropped:
        return None, False
    digest = shake_elide(
        [*prior, *split.dropped], budget_chars=max(1, budget // SEED_BUDGET_DIVISOR)
    )
    if not digest.text:
        return None, True
    meta: dict[str, Any] = {
        "kind": SEED_META_KIND,
        "method": method,
        "through_row": digest.through_row,
        "through_ts": digest.through_ts,
        "dropped_rows": digest.rows_in,
    }
    return (digest.text, meta), True


def _seed_slot(state: DashboardState, key: str) -> Any | None:
    """The slot whose transcript a seed for *key* would be written to, or ``None``.

    The channel check comes first: a Slack or Discord conversation keeps its own
    session key even while a dashboard tab mirrors it, so ``dashboard_slot_key``
    alone would answer with that tab and digest a channel session's history into
    a tab its user may never open. A key with no tab (a cron, subagent or
    background session) has no transcript here either.
    """
    if is_channel_key(key):
        return None
    # circular import: chat_utils imports dashboard.state at module scope, and
    # state registers this writer, so a module-scope import here would close
    # state -> compaction_seed -> chat_utils -> state.
    from kiro_crew.dashboard.chat_utils import dashboard_slot_key

    slot_key = dashboard_slot_key(key)
    if not slot_key:
        return None
    return state.get_slot(slot_key)


def supports_seed(state: DashboardState, key: str) -> bool:
    """Whether this dashboard holds a transcript it can digest for *key* (no transcript read)."""
    return _seed_slot(state, key) is not None


async def write_seed(
    state: DashboardState, key: str, method: str, window_tokens: int | None
) -> str:
    """Append the ``shake`` seed row to the slot showing *key*; one of the ``SEED_*`` answers.

    ``SEED_UNSUPPORTED`` when no transcript for *key* lives here (see
    ``_seed_slot``), ``SEED_NOTHING`` when the complete conversation fits the
    successor's tail, ``SEED_TAIL_ONLY`` when replay admission cut the
    conversation at its quota or when no seed was written despite rows outside
    that tail, and ``SEED_WRITTEN`` once the seed row is in the slot. The
    transcript read runs off the event loop: a large session's chained read is
    a file parse.
    """
    slot = _seed_slot(state, key)
    if slot is None:
        return SEED_UNSUPPORTED
    from kiro_crew.dashboard.chat_utils import slot_history_key

    expected_slot_name = slot.key
    expected_history_key = slot_history_key(slot)
    rows = await asyncio.to_thread(
        rotation_rows,
        state.conversation_log,
        expected_history_key,
        pending_messages=list(slot.messages),
    )
    if getattr(rows, "conversation_quota_cut", True):
        return SEED_TAIL_ONLY
    seed, split_dropped_rows = build_seed(rows, window_tokens=window_tokens, method=method)
    if seed is None:
        if split_dropped_rows:
            return SEED_TAIL_ONLY
        return SEED_NOTHING
    content, meta = seed
    # Hidden by contract: the transcript registries claim the role undrawn, so
    # the row must not go out as a live ``chat_message`` frame the transcript
    # view would hold. No presentation class: the row draws nothing on every
    # surface, and a collapsed marker for it is follow-up work.
    row = slot.append(SEED_ROLE, content, "", meta=meta, broadcast=False)
    # The answer lets the coordinator drop the native conversation, so the
    # digest must be on disk before it is given: a gateway restart between the
    # recycle and the periodic flush would otherwise reload a transcript with
    # no seed, and the successor would carry the tail alone. A write that does
    # not happen (a guarded metadata save in flight, a refused save, an error)
    # withdraws the row, so no later rotation carries a digest of rows that
    # were never dropped, and answers unsupported: native keeps the history.
    # ``save_slot_off_loop`` with both pins is a guarded write: it refuses once
    # the tab is fenced for close, registers its worker future so a close waits
    # for it before it gives the name away, and refuses at the locked commit
    # when the tab was replaced or re-routed during the read above.
    # circular import: chat_persistence imports the state module at scope.
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    saved = False
    if getattr(slot, "_metadata_persist_inflight", 0) or not state.conversation_log:
        logger.warning(
            "Compaction seed for %s not saved: a guarded metadata write is in flight "
            "or no transcript is open",
            key,
        )
    else:
        try:
            saved = await save_slot_off_loop(
                state,
                slot,
                best_effort=False,
                expected_slot_name=expected_slot_name,
                expected_history_key=expected_history_key,
            )
        except Exception:
            logger.warning("Compaction seed save failed for %s", key, exc_info=True)
    if not saved:
        slot.withdraw(row)
        logger.warning(
            "Compaction seed for %s could not be made durable; withdrawn, handing to native",
            key,
        )
        return SEED_UNSUPPORTED
    logger.info(
        "Compaction seed written for %s: %d rows digested into %d chars",
        key,
        meta["dropped_rows"],
        len(content),
    )
    return SEED_WRITTEN


class DashboardSeedWriter:
    """The ``SeedWriter`` the dashboard registers: ``supports_seed`` and ``write_seed`` bound to one state."""

    __slots__ = ("_state",)

    def __init__(self, state: DashboardState) -> None:
        self._state = state

    def supports(self, key: str) -> bool:
        return supports_seed(self._state, key)

    async def __call__(self, key: str, method: str, window_tokens: int) -> str:
        return await write_seed(self._state, key, method, window_tokens)
