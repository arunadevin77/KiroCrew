"""The work ledger's per-board lock map keeps a board only while someone holds or waits for it."""

import asyncio

import pytest

from kiro_crew import session_ledger
from kiro_crew.dashboard.handlers import work_ledger as routes


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    routes._BOARD_LOCKS.clear()
    yield
    routes._BOARD_LOCKS.clear()


BOUND = 10.0


def _waiting_on(slot: str) -> int:
    """How many tasks wait on *slot*'s board lock, read on the ``asyncio.Lock``
    itself so the count does not depend on how the map stores it."""
    entry = routes._BOARD_LOCKS.get(slot)
    lock = getattr(entry, "lock", entry)
    return len(getattr(lock, "_waiters", None) or ())


async def _until_waiting(slot: str, count: int) -> None:
    """Yield to the loop until *count* tasks wait on *slot*'s lock, for at most BOUND s."""

    async def _poll() -> None:
        while _waiting_on(slot) != count:
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), BOUND)


@pytest.mark.asyncio
async def test_a_finished_session_leaves_no_board_lock_behind():
    for i in range(10_000):
        key = session_ledger.ledger_key(f"dashboard:chat-{i}-1791000000")
        async with routes._board_lock(key):
            pass
    assert len(routes._BOARD_LOCKS) == 0, f"{len(routes._BOARD_LOCKS)} board locks kept"


@pytest.mark.asyncio
async def test_a_use_that_raises_leaves_no_board_lock_behind():
    with pytest.raises(LookupError):
        async with routes._board_lock("chat-refused"):
            raise LookupError("not this caller's ledger")
    assert routes._BOARD_LOCKS == {}


@pytest.mark.asyncio
async def test_two_writers_to_one_board_still_take_turns():
    order: list[str] = []
    first_in = asyncio.Event()
    let_first_go = asyncio.Event()

    async def first() -> None:
        async with routes._board_lock("chat-one"):
            order.append("first in")
            first_in.set()
            await asyncio.wait_for(let_first_go.wait(), BOUND)
            order.append("first out")

    async def second() -> None:
        await asyncio.wait_for(first_in.wait(), BOUND)
        async with routes._board_lock("chat-one"):
            order.append("second in")

    a = asyncio.ensure_future(first())
    b = asyncio.ensure_future(second())
    await asyncio.wait_for(first_in.wait(), BOUND)
    await _until_waiting("chat-one", 1)
    assert order == ["first in"]
    assert len(routes._BOARD_LOCKS) == 1
    let_first_go.set()
    await asyncio.wait_for(asyncio.gather(a, b), BOUND)
    assert order == ["first in", "first out", "second in"]


@pytest.mark.asyncio
async def test_a_cancelled_waiter_takes_nothing_from_the_holder():
    holder_in = asyncio.Event()
    release = asyncio.Event()
    newcomer_in = asyncio.Event()

    async def holder() -> None:
        async with routes._board_lock("chat-one"):
            holder_in.set()
            await asyncio.wait_for(release.wait(), BOUND)

    async def waiter() -> None:
        async with routes._board_lock("chat-one"):
            pass

    async def newcomer() -> None:
        async with routes._board_lock("chat-one"):
            newcomer_in.set()

    h = asyncio.ensure_future(holder())
    await asyncio.wait_for(holder_in.wait(), BOUND)
    w = asyncio.ensure_future(waiter())
    await _until_waiting("chat-one", 1)
    w.cancel()
    with pytest.raises(asyncio.CancelledError):
        await w
    assert len(routes._BOARD_LOCKS) == 1
    n = asyncio.ensure_future(newcomer())
    await _until_waiting("chat-one", 1)
    assert not newcomer_in.is_set()
    release.set()
    await asyncio.wait_for(asyncio.gather(h, n), BOUND)
    assert newcomer_in.is_set()
