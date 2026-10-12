"""Periodic watchdog that detects a data home that has stopped persisting.

The boot preflight (``platform_compat.probe_file_persistence``) refuses to start
a gateway whose data home cannot create, lock, replace and remove files -- the
known cause being a seccomp syscall filter inherited from a sandboxed parent,
which turns basic file syscalls into ``ENOSYS`` while ``stat`` keeps working.
But that preflight runs ONCE, at boot. A filter whose sandbox is still alive at
startup passes the probe legitimately -- the syscalls genuinely work at that
moment -- and breaks minutes later when the sandbox is torn down underneath the
process.

From then on the gateway is in a near-silent, indefinite failure:

* every file ``open`` fails with ``ENOSYS``, so ``aiohttp``'s ``FileResponse``
  turns every static asset into a bare 404 (it swallows the ``OSError``) and the
  SPA never boots;
* ``GET /`` keeps serving a correct ``index.html`` already in memory and
  ``/api/health`` touches no files, so both stay green;
* file logging dies of the same ``ENOSYS`` and drops every record;
* the process never exits, so ``Restart=on-failure`` never fires.

A ``stat``-based check (``stale_asset_watchdog``) cannot see this: the build
tree is still present and ``resolve(strict=True)`` still succeeds. Only an
actual open/write/lock -- exactly what the boot probe does -- distinguishes "the
filesystem went away under us" from "this file is genuinely missing".

This watchdog re-runs that same probe periodically (and can be poked to run
immediately when a static-asset ``open`` fails -- see
``request_persistence_recheck``). On a confirmed failure it logs loudly to
stderr AND the logger (file logging is probably dead, so stderr/journal is the
only surface that survives) and sets the gateway's shutdown event so the process
exits non-zero and a restart-on-failure supervisor relaunches a fresh gateway
with no inherited filter.

Why a shutdown rather than a repair: a seccomp filter cannot be removed from a
live process, so re-staging the frontend, ``systemctl reload`` and the UI's own
"Clear cache and retry" are all incapable of recovering it. Only a new process
clears the filter, and only a non-zero exit makes a supervisor start one.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

# Default check interval (seconds). Long enough to be negligible overhead (one
# mkstemp/write/flock/replace/unlink cycle per tick, off the event loop), short
# enough that a mid-life teardown is caught within a minute rather than the 3.5
# days the originating report (#19463) spent undetected.
_CHECK_INTERVAL_SECS = 30.0

# Delay before re-confirming a failed probe (seconds). A single probe can lose a
# race against a transient filesystem hiccup (an NFS reconnect, a momentary
# ENOSPC that clears); a genuinely-broken data home fails the re-probe too. The
# boot preflight has no equivalent because at boot a single failure is already
# disqualifying, but here a healthy gateway must not be killed by one unlucky
# sample.
_CONFIRM_DELAY_SECS = 2.0

# Process exit status when THIS watchdog initiated the shutdown. Non-zero on
# purpose: the whole point of the shutdown is to be relaunched by a supervisor,
# and ``Restart=on-failure`` (systemd) / ``KeepAlive`` (launchd) only relaunch a
# process that did NOT exit 0. 72 is ``EX_OSFILE`` from ``sysexits.h`` ("critical
# OS file is inaccessible") -- the data home the process needs to persist state
# can no longer be read or written -- and is deliberately NOT 78: the service
# unit sets ``RestartPreventExitStatus=78`` (``LIVE_HOLDER_EXIT_CODE`` in
# ``gateway_lock``) to make the "another gateway already serves this home" exit
# TERMINAL, so a persistence watchdog exiting 78 would never be relaunched --
# the exact opposite of what this fix needs. 72 is restarted like the listener
# guard's 69 and the stale-asset watchdog's 75, and a journal histogram tells
# all four exits apart (69 listener lost, 72 persistence lost, 75 stale assets,
# 78 live-holder terminal).
PERSISTENCE_LOST_EXIT_CODE = 72

# A loud banner prefix for the stderr line, mirroring the cron child's
# ``CHILD_PERSISTENCE_PREFIX`` so operators grepping journals see one shape for
# both. File logging is probably dead by the time we print this, so stderr is
# the surface that survives.
_STDERR_PREFIX = "\u274c Gateway data home stopped persisting: "


class _ShutdownSignal(Protocol):
    """Minimal contract the watchdog needs from a shutdown-signalling event."""

    def is_set(self) -> bool: ...

    def set(self) -> None: ...

    async def wait(self) -> bool: ...


def shutdown_exit_code(watchdog: "asyncio.Future[bool] | None") -> int:
    """Map the watchdog task's outcome onto the gateway's process exit status.

    ``PERSISTENCE_LOST_EXIT_CODE`` iff the watchdog has finished and reported
    ``True`` (it confirmed the data home stopped persisting and set the shutdown
    event itself). Every other state is 0: no watchdog, still running (the event
    was set by SIGTERM/``systemctl stop`` while it slept), cancelled, or crashed
    -- a crashed watchdog must not turn an operator's stop into a restart.
    """
    if watchdog is None or not watchdog.done() or watchdog.cancelled():
        return 0
    try:
        fired = watchdog.result()
    except Exception:
        logger.debug("Persistence watchdog task raised", exc_info=True)
        return 0
    return PERSISTENCE_LOST_EXIT_CODE if fired else 0


class _RecheckGate:
    """A one-shot nudge that lets a request handler wake the watchdog early.

    A static-asset ``open`` that fails with ``OSError`` is the earliest and most
    user-visible symptom of the broken-filesystem state (every asset 404s), and
    it happens on the event loop in the request path. Rather than wait up to a
    full interval for the next periodic probe, the dist handler calls
    :func:`request_persistence_recheck`, which sets this gate; the watchdog's
    interruptible sleep wakes on it and probes immediately. The gate is purely
    an optimisation -- the periodic probe catches the same state regardless, so
    a dropped nudge only costs latency, never a miss.
    """

    def __init__(self) -> None:
        self._event: asyncio.Event | None = None

    def bind(self, event: asyncio.Event) -> None:
        self._event = event

    def unbind(self) -> None:
        self._event = None

    def poke(self) -> None:
        event = self._event
        if event is None:
            return
        loop = getattr(event, "_loop", None)
        if loop is not None and loop.is_running():
            # The dist handler and the watchdog share the gateway loop, so a
            # direct set() is fine; guard the thread-foreign case defensively so
            # a probe poked from a worker thread (future callers) still lands.
            try:
                loop.call_soon_threadsafe(event.set)
                return
            except RuntimeError:  # pragma: no cover - loop torn down mid-call
                return
        event.set()


# Module-level gate so the dist handler (which has no handle on the watchdog
# task) can nudge it without threading one through every call site.
_recheck_gate = _RecheckGate()


def request_persistence_recheck() -> None:
    """Ask the running persistence watchdog to probe now, not at its next tick.

    Safe to call from any request handler at any time: a no-op when no watchdog
    is armed (e.g. a dev install, or before/after the watchdog's lifetime), and
    idempotent within one interval. Never raises.
    """
    try:
        _recheck_gate.poke()
    except Exception:  # pragma: no cover - a nudge must never break a request
        logger.debug("Persistence recheck nudge failed", exc_info=True)


async def run_persistence_watchdog(
    shutdown_event: _ShutdownSignal,
    probe: Callable[[], Awaitable[str | None]],
    *,
    interval: float = _CHECK_INTERVAL_SECS,
    confirm_delay: float = _CONFIRM_DELAY_SECS,
) -> bool:
    """Background loop: re-run the persistence probe, shut down if it fails.

    Returns ``True`` iff this watchdog is the one that set ``shutdown_event``
    (a confirmed, re-confirmed persistence failure). Every other exit -- an
    external SIGTERM/``systemctl stop`` setting the event while it slept, or a
    cancellation -- returns ``False``. The gateway maps ``True`` onto
    :data:`PERSISTENCE_LOST_EXIT_CODE` so the process exits non-zero and a
    restart-on-failure supervisor relaunches it, while an operator stop still
    exits 0 and stays stopped.

    ``probe`` is an async callable returning ``None`` when the data home still
    persists and a human-readable failure description otherwise -- the gateway
    passes an off-loop wrapper of ``platform_compat.probe_file_persistence``, the
    exact check the boot preflight uses. A failed probe is re-confirmed after
    ``confirm_delay`` before acting, so a transient hiccup cannot kill a healthy
    gateway; a genuine seccomp teardown fails the re-probe too.

    The sleep between probes is interruptible by both ``shutdown_event`` (so an
    operator stop is honoured promptly) and the module recheck gate (so a
    static-asset open failure in the request path triggers an immediate probe --
    see :func:`request_persistence_recheck`).
    """
    gate = asyncio.Event()
    _recheck_gate.bind(gate)
    try:
        while not shutdown_event.is_set():
            if not await _sleep_or_wake(shutdown_event, gate, interval):
                return False  # shutdown signalled during the sleep
            gate.clear()

            reason = await probe()
            if reason is None:
                continue

            # One failure might be a transient hiccup. Re-confirm after a short
            # delay, interruptible by an operator stop, before acting.
            if not await _sleep_or_wake(shutdown_event, gate, confirm_delay):
                return False
            if shutdown_event.is_set():
                return False
            reason = await probe()
            if reason is None:
                logger.warning(
                    "Persistence watchdog: data home briefly failed a probe but "
                    "recovered on re-check (%s) -- not shutting down.",
                    "transient",
                )
                continue

            _announce_failure(reason)
            shutdown_event.set()
            return True
        return False
    finally:
        _recheck_gate.unbind()


async def _sleep_or_wake(
    shutdown_event: _ShutdownSignal,
    gate: asyncio.Event,
    timeout: float,
) -> bool:
    """Sleep up to ``timeout``; wake early on the gate. Return ``False`` on stop.

    Returns ``True`` if the full timeout elapsed or the recheck gate fired
    (either way: go probe), ``False`` if ``shutdown_event`` was set (stop). The
    two waiters race; whichever resolves first wins and the other is cancelled.
    """
    stop = asyncio.ensure_future(shutdown_event.wait())
    wake = asyncio.ensure_future(gate.wait())
    try:
        done, _pending = await asyncio.wait(
            {stop, wake}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for fut in (stop, wake):
            if not fut.done():
                fut.cancel()
                try:
                    await fut
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
    # Stop wins over an early wake: if both resolved in the same tick, honour the
    # shutdown rather than running one more probe into a dying loop.
    if stop in done:
        return False
    return True


def _announce_failure(reason: str) -> None:
    """Log the confirmed persistence failure as loudly as the environment allows.

    File logging is almost certainly dead by now (it died of the same ``ENOSYS``
    the probe just re-detected), so the message goes to stderr with ``flush``
    FIRST -- that is the surface systemd/journald and a terminal still capture --
    and to the logger second for any handler that is still alive (a journald
    handler, an already-open fd).
    """
    message = (
        "data home stopped persisting after a healthy boot -- the gateway can no "
        "longer save chat/cron/session state and every static asset 404s "
        "silently. Exiting non-zero so a supervisor restarts a fresh process "
        "(a seccomp filter inherited from a sandboxed parent cannot be removed "
        f"from a live process). {reason}"
    )
    try:
        print(f"{_STDERR_PREFIX}{reason}", file=sys.stderr, flush=True)
    except Exception:  # pragma: no cover - stderr itself is gone
        pass
    logger.critical("Persistence watchdog: %s", message)


def probe_data_home(data_home: Path) -> Callable[[], Awaitable[str | None]]:
    """Build the off-loop async probe the gateway hands to the watchdog.

    ``probe_file_persistence`` does real filesystem I/O (mkstemp + fsync +
    flock + replace + unlink), so it runs on a worker thread exactly as the boot
    preflight does: a stalled filesystem would otherwise wedge the event loop
    this watchdog exists to protect. A thread-pool exhaustion is reported as a
    probe failure -- a gateway that cannot get one worker thread is already in
    the degraded state the watchdog guards against.
    """
    from kiro_crew import platform_compat

    async def _probe() -> str | None:
        try:
            return await asyncio.to_thread(platform_compat.probe_file_persistence, data_home)
        except RuntimeError as exc:
            return f"cannot run the persistence probe: {exc}"

    return _probe
