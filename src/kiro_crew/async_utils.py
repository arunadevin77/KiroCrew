"""Cancellation-safe helpers for asynchronous transactions."""

from __future__ import annotations

import asyncio


async def run_to_completion(aw):
    """Await *aw* so that a cancellation cannot abandon it part-way.

    The awaitable runs as its own task behind ``asyncio.shield``; a
    ``CancelledError`` delivered to the CALLER is remembered and the caller
    keeps waiting until the task finishes, then the cancellation is re-raised.
    The loop, not a single re-await, is what makes that hold under repeated
    cancellation (a graceful shutdown escalating after its timeout): each
    re-shield absorbs one more cancel, and only a finished task ends it.

    For a multi-phase write -- a channel saver's config.json commit followed by
    its ``.env`` credential write, the MCP gateway toggle's persist followed by
    its live apply -- a cancellation between the phases would leave the stored
    state and the effective state disagreeing; wrapping the whole transaction
    here is what keeps the pair consistent. Cancellation is deferred, never
    swallowed: the caller still unwinds with ``CancelledError`` afterwards, and
    an exception from the task propagates as usual.
    """
    task = asyncio.ensure_future(aw)
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            # OUR await was cancelled, not the task: remember it, keep waiting
            # for the still-running work.
            cancelled = exc
    if cancelled is not None:
        raise cancelled
    return result
