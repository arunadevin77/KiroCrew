"""Tests for the periodic persistence watchdog (#19463).

The boot preflight catches a data home that cannot persist AT boot; this
watchdog catches one that passed boot and broke later (a seccomp filter whose
sandbox was torn down mid-run). It must: re-run the probe periodically,
re-confirm a single failure before acting, exit non-zero on a confirmed failure
so a supervisor restarts, exit 0 on an operator stop, and let a request handler
poke it to probe immediately.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.dashboard import persistence_watchdog as pw


class _Event:
    """A minimal shutdown signal matching the watchdog's Protocol."""

    def __init__(self) -> None:
        self._e = asyncio.Event()

    def is_set(self) -> bool:
        return self._e.is_set()

    def set(self) -> None:
        self._e.set()

    async def wait(self) -> bool:
        await self._e.wait()
        return True


def _probe_sequence(values: list[str | None]):
    """An async probe that yields ``values`` in order, then repeats the last."""
    calls = {"n": 0}

    async def _probe() -> str | None:
        i = min(calls["n"], len(values) - 1)
        calls["n"] += 1
        return values[i]

    _probe.calls = calls  # type: ignore[attr-defined]
    return _probe


@pytest.mark.asyncio
async def test_exits_nonzero_on_confirmed_failure() -> None:
    shutdown = _Event()
    # Fails, re-confirm also fails -> fire.
    probe = _probe_sequence(["ENOSYS from a basic file syscall"])
    fired = await pw.run_persistence_watchdog(shutdown, probe, interval=0.0, confirm_delay=0.0)
    assert fired is True
    assert shutdown.is_set()
    # The watchdog's own future-outcome mapper yields the restart exit code.
    done: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    done.set_result(True)
    assert pw.shutdown_exit_code(done) == pw.PERSISTENCE_LOST_EXIT_CODE
    assert pw.PERSISTENCE_LOST_EXIT_CODE != 0
    # Must be a RESTARTED exit, never the terminal live-holder code (78), which
    # the Linux unit exempts from Restart= via RestartPreventExitStatus. A
    # persistence failure exiting 78 would never be relaunched — defeating the
    # whole fix. 72 (EX_OSFILE) is restarted like 69 and 75.
    from kiro_crew.gateway_lock import LIVE_HOLDER_EXIT_CODE

    assert pw.PERSISTENCE_LOST_EXIT_CODE != LIVE_HOLDER_EXIT_CODE


@pytest.mark.asyncio
async def test_transient_failure_recovers_without_shutdown() -> None:
    shutdown = _Event()
    # First probe fails, the re-confirm probe passes -> keep running. Stop the
    # loop after the first cycle by setting shutdown once it has looped once.
    probe = _probe_sequence(["transient glitch", None, None])

    async def _stop_soon() -> None:
        # Let the watchdog run its first failed+recovered cycle, then stop it.
        while probe.calls["n"] < 2:  # type: ignore[attr-defined]
            await asyncio.sleep(0)
        shutdown.set()

    stopper = asyncio.ensure_future(_stop_soon())
    fired = await pw.run_persistence_watchdog(shutdown, probe, interval=0.0, confirm_delay=0.0)
    await stopper
    # It did not fire: the re-confirm cleared the one-off failure.
    assert fired is False


@pytest.mark.asyncio
async def test_operator_stop_exits_zero() -> None:
    shutdown = _Event()
    shutdown.set()  # already stopping before the watchdog arms
    probe = _probe_sequence([None])
    fired = await pw.run_persistence_watchdog(shutdown, probe, interval=10.0, confirm_delay=10.0)
    assert fired is False
    # A watchdog that did not fire maps to exit 0 (operator stop stays stopped).
    done: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    done.set_result(False)
    assert pw.shutdown_exit_code(done) == 0


@pytest.mark.asyncio
async def test_recheck_gate_wakes_the_watchdog_early() -> None:
    """A long interval, but a request-path nudge makes it probe at once."""
    from kiro_crew.testing.wait import await_until

    shutdown = _Event()
    probe = _probe_sequence(["ENOSYS"])

    # Interval is huge; without the nudge the watchdog would sleep far past the
    # test's own timeout. The nudge must wake it so it probes and fires.
    task: asyncio.Future[bool] = asyncio.ensure_future(
        pw.run_persistence_watchdog(shutdown, probe, interval=3600.0, confirm_delay=0.0)
    )
    # Let the watchdog bind its gate (its first sleep), then poke it.
    await asyncio.sleep(0.05)
    pw.request_persistence_recheck()

    fired = await await_until(task, what="persistence watchdog to fire after a nudge")
    assert fired is True
    assert shutdown.is_set()


@pytest.mark.asyncio
async def test_recheck_gate_is_a_noop_when_no_watchdog_is_armed() -> None:
    # Must never raise when called outside any watchdog's lifetime.
    pw.request_persistence_recheck()
    pw.request_persistence_recheck()


@pytest.mark.asyncio
async def test_exit_code_mapper_is_defensive() -> None:
    assert pw.shutdown_exit_code(None) == 0
    loop = asyncio.get_event_loop()
    running: asyncio.Future[bool] = loop.create_future()
    assert pw.shutdown_exit_code(running) == 0  # not done yet
    cancelled: asyncio.Future[bool] = loop.create_future()
    cancelled.cancel()
    assert pw.shutdown_exit_code(cancelled) == 0
    crashed: asyncio.Future[bool] = loop.create_future()
    crashed.set_exception(RuntimeError("boom"))
    assert pw.shutdown_exit_code(crashed) == 0


@pytest.mark.asyncio
async def test_probe_data_home_wraps_probe_file_persistence(monkeypatch) -> None:
    from kiro_crew import platform_compat

    seen = {}

    def _fake_probe(directory):
        seen["dir"] = directory
        return "cannot write files in /x: [Errno 38] Function not implemented"

    monkeypatch.setattr(platform_compat, "probe_file_persistence", _fake_probe)
    from pathlib import Path

    probe = pw.probe_data_home(Path("/x"))
    reason = await probe()
    assert "Errno 38" in reason
    assert seen["dir"] == Path("/x")


@pytest.mark.asyncio
async def test_probe_data_home_reports_thread_exhaustion(monkeypatch) -> None:
    async def _boom(*_a, **_k):
        raise RuntimeError("no threads")

    monkeypatch.setattr(pw.asyncio, "to_thread", _boom)
    from pathlib import Path

    probe = pw.probe_data_home(Path("/x"))
    reason = await probe()
    assert reason is not None
    assert "persistence probe" in reason
